"""Clean out the SQLite database before go live (GitHub issue 46; POST_SHADOW_PLAN RC2 G1, first step).

`report` is read-only: rows and bytes per table, so nobody guesses what the file is made of.
`clean` is a dry run unless asked to apply. Applying it: refuses unless the telemetry files agree with SQLite's minute rows, takes a verified backup of the database FIRST
(and refuses if it cannot), then removes only what the system no longer reads:
  * archived provider/plan snapshots older than 48 hours (protected ones stay) and old operational setting versions (the existing retention rules);
  * raw collector snapshots older than a day;
  * the SQLite one-minute rows that the telemetry files hold identically (verified by the agreement check), except the last hour;
  * the `charger_coverage` and `charger_samples` rows of the old charger tracker (the car's power over time is the `ev_power` telemetry series).
then compacts the file (checkpoint + VACUUM). Never touched: settings, setting versions of owner settings, daily and half-hour records (they are settings), charger sessions and
energy, imports, protected archived snapshots, the telemetry files and the backups.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

PROTECTED = ("settings", "setting_versions", "charger_sessions", "charger_energy", "imports")
KEEP_MINUTE_ROWS_S = 3600       # the collector no longer writes reading rows (5 November): what is left is older, and the telemetry files hold it; the last hour is left as a margin
KEEP_RAW_S = 86400
KEEP_ISSUANCE_HOURS = 48          # archived plan and forecast snapshots: about 160 KB each and one a minute, so seven days is most of the database (the clean-out keeps two)


def report(store):
    """{"file_bytes", "wal_bytes", "tables": [{"name", "rows", "bytes"}]} largest first; `bytes` is from dbstat when SQLite has it, else the payload lengths."""
    path = Path(store.path)
    out = {"file_bytes": path.stat().st_size, "wal_bytes": (path.with_name(path.name + "-wal").stat().st_size if path.with_name(path.name + "-wal").exists() else 0), "tables": []}
    with closing(store.connect()) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        try:
            sizes = dict(db.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name").fetchall())
        except Exception:
            sizes = {}
        for name in names:
            rows = db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            out["tables"].append({"name": name, "rows": rows, "bytes": int(sizes.get(name) or 0), "protected": name in PROTECTED})
    out["tables"].sort(key=lambda t: -t["bytes"])
    return out


def _count(db, sql, args=()):
    return db.execute(sql, args).fetchone()[0]


def plan(store, root, now):
    """What `clean --apply` would remove, without touching anything."""
    from .retention import purge_detail
    from .telemetry_backfill import agreement, _count_range
    detail = purge_detail(store, now, issuance_hours=KEEP_ISSUANCE_HOURS, dry_run=True)
    first, last = _count_range(store)
    cutoff = now - KEEP_MINUTE_ROWS_S
    # Only the rows that will be deleted have to agree with the files: the last hour stays either way, and a minute cut by a restart or a busy disk at the live edge
    # must not stand in the way of removing last week's rows.
    agree = agreement(store, root, now, last=cutoff // 60 * 60) if first is not None else {"ok": True, "metrics": {}}
    with closing(store.connect()) as db:
        minute_rows = _count(db, "SELECT count(*) FROM observations WHERE json_extract(payload,'$.window_mean') IS NOT NULL AND sample_at<?", (cutoff,)) if agree["ok"] else 0
        raw = _count(db, "SELECT count(*) FROM collector_snapshots WHERE acquired_at<?", (now - KEEP_RAW_S,))
        coverage = _count(db, "SELECT count(*) FROM charger_coverage") + _count(db, "SELECT count(*) FROM charger_samples")
    checked = [m for m, e in agree["metrics"].items() if not (e.get("missing") or e.get("mean_differences") or e.get("eligibility_changes"))] if agree["ok"] else []
    return {"detail": detail, "agreement_ok": bool(agree["ok"]), "metrics_checked": checked, "minute_rows": minute_rows, "raw_snapshots": raw, "charger_coverage_rows": coverage}


def clean(store, root, now, backup_dir, apply=False):
    result = {"applied": False, "plan": plan(store, root, now)}
    if not apply:
        return result
    if not result["plan"]["agreement_ok"]:
        raise RuntimeError("The telemetry files do not agree with SQLite's minute rows: run `pi-tools.sh backfill` until it says OK before cleaning")
    destination = Path(backup_dir) / f"before-clean-{int(now)}.sqlite3"
    result["backup"] = str(store.backup(destination))               # verified (integrity check) or it raises; nothing below runs without it
    from .retention import purge_detail
    result["retention"] = purge_detail(store, now, issuance_hours=KEEP_ISSUANCE_HOURS)
    store.prune_raw(now - KEEP_RAW_S)
    cutoff = now - KEEP_MINUTE_ROWS_S
    checked = sorted(result["plan"]["metrics_checked"])                    # only the metrics the files were compared for are removed (GitHub 53)
    marks = ",".join("?" * len(checked))
    while True:
        with closing(store.connect()) as db, db:
            done = db.execute(f"DELETE FROM observations WHERE rowid IN (SELECT rowid FROM observations WHERE json_extract(payload,'$.window_mean') IS NOT NULL AND sample_at<? "
                              f"AND metric IN ({marks}) LIMIT 5000)", (cutoff, *checked)).rowcount
        if done < 5000:
            break
    with closing(store.connect()) as db, db:
        db.execute("DELETE FROM charger_coverage")
        db.execute("DELETE FROM charger_samples")             # the car's power over time is the `ev_power` series in the telemetry files; nothing reads these rows
    before = Path(store.path).stat().st_size
    with closing(store.connect()) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.isolation_level = None
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    result.update(applied=True, bytes_before=before, bytes_after=Path(store.path).stat().st_size, after=report(store))
    return result


class _ReadOnlyDb:
    """Just enough of a Store to read a backup file without touching it (journal, WAL and all)."""

    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        import sqlite3
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)


def verify_backup(backup, telemetry_dir, now, hours_back=1):
    """Compare the minute rows in a database backup (for example the one `db clean` took before it removed anything) with the telemetry files, for every metric. Reads both,
    changes nothing. The newest `hours_back` hours of the backup are left out (the live edge). Returns the agreement result."""
    from .telemetry_backfill import _count_range, agreement
    db = _ReadOnlyDb(backup)
    first, last = _count_range(db)
    if first is None:
        return {"ok": True, "metrics": {}, "note": "no minute rows in that backup"}
    return agreement(db, telemetry_dir, now, last=last - hours_back * 3600)
