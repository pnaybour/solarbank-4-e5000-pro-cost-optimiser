"""WP8: a nightly verified copy of the database and the telemetry files to Google Drive (rclone), with a restore check.

The copy is built in a private temporary folder: a consistent SQLite backup (the backup command already integrity-checks it) without the old plan snapshots (`slim_database`), every telemetry file read
under its shared lock, one compressed archive. It is uploaded, its size is compared with what the remote reports, and only then are old copies pruned
(the newest KEEP stay). The outcome is written to `offsite-status.json`; two days without a success raises an alert (alerts.check).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from contextlib import closing
from pathlib import Path

KEEP = 3
STALE_S = 2 * 86400
STATUS_FILE = "offsite-status.json"
PREFIX = "solarbank-backup-"


class BackupError(RuntimeError):
    pass


def archive_name(now):
    return PREFIX + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + ".tar.gz"


def _copy_locked(source, destination):
    """Copy a telemetry file while holding its shared lock, so an update in progress cannot tear the copy."""
    with open(source, "rb") as src:
        fcntl.flock(src.fileno(), fcntl.LOCK_SH)
        try:
            with open(destination, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        finally:
            fcntl.flock(src.fileno(), fcntl.LOCK_UN)


SLIM_ISSUANCE_HOURS = 6          # plan and forecast snapshots kept in the off-site copy (12 and 14 November 2026). The live database keeps seven days of them: about 800 MB of its 1 GB, and
                                 # the big uploads kept hitting Google Drive's rate limit (12 and 14 November). They are history the planner regenerates every minute, not records.


def slim_database(store, database, now, keep_hours=SLIM_ISSUANCE_HOURS):
    """Take the old plan and forecast snapshots (`issuances`) out of the COPY at `database`, never out of the live database. Kept: anything newer than `keep_hours`, and every
    snapshot a current setting still points to (the same protection the seven-day retention uses). Everything else in the database, every setting, record and observation, is
    left exactly as it was. Returns (removed, kept)."""
    from .retention import _delete, protected_issuance_ids
    protected = protected_issuance_ids(store)
    cutoff = now - keep_hours * 3600
    with closing(sqlite3.connect(database)) as db:
        old = [row[0] for row in db.execute("SELECT id FROM issuances WHERE acquired_at<?", (cutoff,)) if row[0] not in protected]
        for start in range(0, len(old), 400):
            chunk = old[start:start + 400]
            with db:
                db.execute("BEGIN IMMEDIATE")          # the append-only trigger is dropped and put back inside this one transaction (retention._delete)
                _delete(db, "issuances", f"DELETE FROM issuances WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        kept = db.execute("SELECT count(*) FROM issuances").fetchone()[0]
        db.isolation_level = None
        db.execute("VACUUM")                           # without this the file keeps its size
    return len(old), kept


def build_archive(store, telemetry_dir, work, now):
    """Make the archive in `work`; returns its path. Raises BackupError if the database copy does not verify."""
    work = Path(work)
    database = work / "controller.sqlite3"
    store.backup(database)
    slim_database(store, database, now)
    tmp = work / "telemetry"
    tmp.mkdir()
    for path in sorted(Path(telemetry_dir).glob("*.wsp")):
        _copy_locked(path, tmp / path.name)
    archive = work / archive_name(now)
    with tarfile.open(archive, "w:gz", compresslevel=3) as tar:
        tar.add(database, arcname="controller.sqlite3")
        for path in sorted(tmp.iterdir()):
            tar.add(path, arcname="telemetry/" + path.name)
    verify_archive(archive)
    return archive


def _extract(tar, folder):
    """Unpack a backup archive safely, without the tarfile `filter=` feature. Python's extraction filters differ between versions and Debian's patched 3.11.2 on the Pi (the first two Pi
    deploys failed their test gate here), so every member is checked by hand instead: only plain files and folders, no absolute path, no `..`, nothing that resolves outside the folder."""
    root = Path(folder).resolve()
    for member in tar.getmembers():
        target = (root / member.name).resolve()
        if not (member.isfile() or member.isdir()) or member.name.startswith("/") or ".." in Path(member.name).parts or root not in (target, *target.parents):
            raise BackupError(f"the archive holds an unsafe member: {member.name!r}")
    tar.extractall(folder)


def verify_archive(archive):
    """The restore check: unpack to a temporary folder; require the database and EVERY telemetry file the system writes; open the database read-only and check it is ours and passes its
    integrity check; read the header of every telemetry file. Anything missing or damaged raises BackupError."""
    from .telemetry import METRICS, files_for
    expected = sorted(name for metric in METRICS for name, _a, _x in files_for(metric))
    with tempfile.TemporaryDirectory() as folder:
        with tarfile.open(archive, "r:gz") as tar:
            _extract(tar, folder)
        database = Path(folder) / "controller.sqlite3"
        if not database.is_file():
            raise BackupError("the archive has no database")
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"settings", "observations", "events", "setting_versions"} <= tables:
                raise BackupError("the database copy is not a Solarbank database (its tables are missing)")
            db.execute("SELECT count(*) FROM settings").fetchone()
        finally:
            db.close()
        if result != "ok":
            raise BackupError(f"the database copy failed its integrity check: {result}")
        have = sorted(p.name for p in (Path(folder) / "telemetry").glob("*.wsp"))
        missing = [name for name in expected if name not in have]
        if missing:
            raise BackupError("the archive lacks telemetry files: " + ", ".join(missing[:5]))
        import whisper
        for name in expected:
            try:
                whisper.info(str(Path(folder) / "telemetry" / name))
            except Exception as error:
                raise BackupError(f"telemetry file {name} is damaged: {error}") from None
    return {"user_version": version, "telemetry_files": len(expected)}


RCLONE_PATIENCE = ("--timeout", "120s", "--contimeout", "30s", "--retries", "3", "--low-level-retries", "5")


def _rclone(args, runner):
    # A stalled connection must fail and be retried within minutes, not sit for the whole hour (8 November: one upload of a 73 MB file hung until the one-hour limit).
    # --timeout is rclone's idle limit (no data moving), so a slow but moving upload is never cut off.
    done = runner(["rclone", *RCLONE_PATIENCE, *args], capture_output=True, text=True, timeout=3600)
    if done.returncode != 0:
        raise BackupError(f"rclone {args[0]} failed: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout


def upload_and_prune(archive, remote, keep=KEEP, runner=subprocess.run):
    size = Path(archive).stat().st_size
    _rclone(["copyto", str(archive), f"{remote}/{Path(archive).name}"], runner)
    listing = json.loads(_rclone(["lsjson", remote], runner) or "[]")
    mine = {e["Name"]: e.get("Size") for e in listing if e.get("Name", "").startswith(PREFIX)}
    if mine.get(Path(archive).name) != size:
        raise BackupError(f"the remote holds {mine.get(Path(archive).name)} bytes of {Path(archive).name}, expected {size}")
    for old in sorted(mine)[:-keep] if len(mine) > keep else []:
        _rclone(["deletefile", f"{remote}/{old}"], runner)
    return size, sorted(mine)[-keep:]


def write_status(state_dir, ok, now, uploaded=False, **extra):
    """Only a verified UPLOAD advances `last_success` (what the two-day alert watches); a local drill records `last_local_check` and leaves the alarm as it was."""
    path = Path(state_dir) / STATUS_FILE
    previous = read_status(state_dir)
    document = {"at": now, "ok": ok, "uploaded": uploaded,
                "last_success": now if (ok and uploaded) else previous.get("last_success"),
                "last_local_check": now if (ok and not uploaded) else previous.get("last_local_check"), **extra}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document))
    os.replace(temporary, path)
    return document


def read_status(state_dir):
    try:
        return json.loads((Path(state_dir) / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}


def run(store, telemetry_dir, state_dir, remote, now=None, keep=KEEP, runner=subprocess.run, upload=True):
    """One nightly run. Never raises: the outcome is in the status file (and the return value)."""
    now = time.time() if now is None else now
    try:
        with tempfile.TemporaryDirectory(dir=state_dir, prefix=".offsite-") as work:
            archive = build_archive(store, telemetry_dir, work, now)
            checked = verify_archive(archive)
            if not upload:
                return write_status(state_dir, True, now, uploaded=False, name=archive.name, bytes=archive.stat().st_size, **checked)
            size, kept = upload_and_prune(archive, remote, keep, runner)
            return write_status(state_dir, True, now, uploaded=True, name=archive.name, bytes=size, kept=kept, **checked)
    except (BackupError, OSError, sqlite3.Error, subprocess.TimeoutExpired, ValueError) as error:
        return write_status(state_dir, False, now, error=f"{error.__class__.__name__}: {error}"[:300])


def stale(state_dir, conf_path, now):
    """(is_stale, text). Only meaningful once the owner has set up the remote (the conf file exists)."""
    try:
        since = Path(conf_path).stat().st_mtime
    except OSError:
        return False, ""
    status = read_status(state_dir)
    last = status.get("last_success") or since
    if now - last > STALE_S:
        why = status.get("error") or "no run has succeeded"
        return True, f"No verified off-site backup for {(now - last) / 86400:.1f} days ({why})."
    return False, ""
