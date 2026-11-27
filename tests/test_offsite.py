"""WP8: the verified off-site copy, its rotation, failure reporting and the two-day alert."""
import json
import subprocess
import tarfile

import pytest

from solarbank_optimiser import offsite
from solarbank_optimiser.store import Store
from solarbank_optimiser.telemetry import Telemetry

NOW = 1_800_000_000.0


class Remote:
    """A fake `rclone` that keeps files in a dict and records every call."""
    def __init__(self, fail_on=None, short=False):
        self.files, self.calls, self.fail_on, self.short = {}, [], fail_on, short

    def __call__(self, command, **kwargs):
        # rclone is given patience options before the verb (a stalled upload must fail and retry within minutes): this fake reads the verb and its arguments after them
        from solarbank_optimiser.offsite import RCLONE_PATIENCE
        assert tuple(command[1:1 + len(RCLONE_PATIENCE)]) == RCLONE_PATIENCE, command
        command = [command[0], *command[1 + len(RCLONE_PATIENCE):]]
        self.calls.append(command[1])
        if command[1] == self.fail_on:
            return subprocess.CompletedProcess(command, 1, "", "quota exceeded")
        if command[1] == "copyto":
            data = open(command[2], "rb").read()
            self.files[command[3].rsplit("/", 1)[1]] = data[:-1] if self.short else data
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1] == "lsjson":
            return subprocess.CompletedProcess(command, 0, json.dumps([{"Name": n, "Size": len(d)} for n, d in self.files.items()]), "")
        if command[1] == "deletefile":
            self.files.pop(command[2].rsplit("/", 1)[1])
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from solarbank_optimiser.telemetry import Telemetry as T
    monkeypatch.setattr(T, "SPARSE", True)
    store = Store(tmp_path / "state" / "controller.sqlite3"); store.initialise()
    Telemetry(tmp_path / "state" / "telemetry")
    return store, tmp_path / "state"


def test_a_run_builds_verifies_uploads_and_records_success(setup):
    store, state = setup
    remote = Remote()
    status = offsite.run(store, state / "telemetry", state, "gdrive:backups", now=NOW, runner=remote)
    assert status["ok"] and status["uploaded"] and status["telemetry_files"] == 21
    assert remote.calls == ["copyto", "lsjson"]
    assert offsite.read_status(state)["last_success"] == NOW
    assert not list(state.glob(".offsite-*"))                          # the temporary folder is gone


def test_only_the_newest_three_copies_are_kept(setup):
    store, state = setup
    remote = Remote()
    for day in range(5):
        offsite.run(store, state / "telemetry", state, "r:b", now=NOW + day * 86400, runner=remote)
    assert len(remote.files) == 3 and sorted(remote.files) == sorted(remote.files)[-3:]
    assert offsite.archive_name(NOW + 4 * 86400) in remote.files and offsite.archive_name(NOW) not in remote.files


def test_a_failed_upload_or_a_short_copy_is_a_failure_and_keeps_the_last_success(setup):
    store, state = setup
    remote = Remote()
    offsite.run(store, state / "telemetry", state, "r:b", now=NOW, runner=remote)
    bad = offsite.run(store, state / "telemetry", state, "r:b", now=NOW + 86400, runner=Remote(fail_on="copyto"))
    assert not bad["ok"] and "quota exceeded" in bad["error"] and bad["last_success"] == NOW
    short = offsite.run(store, state / "telemetry", state, "r:b", now=NOW + 2 * 86400, runner=Remote(short=True))
    assert not short["ok"] and "expected" in short["error"]


def test_the_restore_check_rejects_a_damaged_copy(setup, tmp_path):
    store, state = setup
    work = tmp_path / "w"; work.mkdir()
    archive = offsite.build_archive(store, state / "telemetry", work, NOW)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "controller.sqlite3" in names and "telemetry/soc.wsp" in names
    broken = work / "broken.tar.gz"
    with tarfile.open(broken, "w:gz") as tar:
        (work / "x.txt").write_text("not a database")
        tar.add(work / "x.txt", arcname="controller.sqlite3")
    with pytest.raises(Exception):
        offsite.verify_archive(broken)


def test_two_days_without_a_success_is_stale_only_once_the_remote_is_set_up(setup, tmp_path):
    store, state = setup
    conf = tmp_path / "offsite.conf"
    assert offsite.stale(state, conf, NOW) == (False, "")              # not set up: never an alert
    conf.write_text("REMOTE=gdrive:backups\n")
    import os
    os.utime(conf, (NOW, NOW))
    assert offsite.stale(state, conf, NOW + 86400)[0] is False
    stale, text = offsite.stale(state, conf, NOW + 3 * 86400)
    assert stale and "no run has succeeded" in text
    offsite.run(store, state / "telemetry", state, "r:b", now=NOW + 3 * 86400, runner=Remote())
    assert offsite.stale(state, conf, NOW + 3.5 * 86400)[0] is False


def test_the_alert_check_raises_and_clears_the_offsite_alert(setup, tmp_path):
    from solarbank_optimiser import alerts
    store, state = setup
    conf, directory = tmp_path / "offsite.conf", tmp_path / "alerts"
    conf.write_text("REMOTE=x:y\n")
    import os
    os.utime(conf, (NOW, NOW))
    kwargs = dict(runtime_dir=tmp_path / "run", state_dir=state, is_active=lambda unit: True, disk_free=10 ** 12, services=(), offsite_conf=conf)
    alerts.check(directory, tmp_path / "seen.json", now=NOW + 3 * 86400, **kwargs)
    assert any("offsite-backup" in p.name for p in directory.iterdir())
    offsite.run(store, state / "telemetry", state, "r:b", now=NOW + 3 * 86400, runner=Remote())
    alerts.check(directory, tmp_path / "seen.json", now=NOW + 3 * 86400 + 60, **kwargs)
    assert not any("offsite-backup" in p.name for p in directory.iterdir())


def test_a_truncated_telemetry_file_or_a_missing_member_fails_the_check_and_nothing_is_uploaded(setup, tmp_path):
    store, state = setup
    (state / "telemetry" / "household_power.wsp").write_bytes(b"\0" * 10)                     # a truncated file
    remote = Remote()
    status = offsite.run(store, state / "telemetry", state, "r:b", now=NOW, runner=remote)
    assert not status["ok"] and "household_power" in status["error"] and remote.files == {} and remote.calls == []
    (state / "telemetry" / "household_power.wsp").unlink()                                      # a missing file
    status = offsite.run(store, state / "telemetry", state, "r:b", now=NOW + 1, runner=remote)
    assert not status["ok"] and "lacks telemetry files" in status["error"]


def test_an_archive_without_a_real_database_is_rejected(tmp_path):
    empty = tmp_path / "e.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    with pytest.raises(offsite.BackupError, match="no database"):
        offsite.verify_archive(empty)
    (tmp_path / "controller.sqlite3").write_bytes(b"")
    stranger = tmp_path / "s.tar.gz"
    with tarfile.open(stranger, "w:gz") as tar:
        tar.add(tmp_path / "controller.sqlite3", arcname="controller.sqlite3")
    with pytest.raises(offsite.BackupError, match="not a Solarbank database"):
        offsite.verify_archive(stranger)


def test_a_local_drill_does_not_clear_the_overdue_alarm(setup, tmp_path):
    store, state = setup
    conf = tmp_path / "offsite.conf"; conf.write_text("REMOTE=x:y\n")
    import os
    os.utime(conf, (NOW, NOW))
    offsite.run(store, state / "telemetry", state, "r:b", now=NOW, runner=Remote())
    assert offsite.stale(state, conf, NOW + 3 * 86400)[0] is True
    drill = offsite.run(store, state / "telemetry", state, "r:b", now=NOW + 3 * 86400, upload=False)
    assert drill["ok"] and drill["uploaded"] is False and drill["last_success"] == NOW and drill["last_local_check"] == NOW + 3 * 86400
    assert offsite.stale(state, conf, NOW + 3 * 86400)[0] is True


def test_the_backup_check_works_on_a_python_without_extraction_filters_and_refuses_unsafe_members(setup, tmp_path, monkeypatch):
    """The Pi runs Debian's Python 3.11.2, whose tarfile has no `filter=` (the first Pi deploy failed here, 4 November)."""
    original = tarfile.TarFile.extractall
    def old_extractall(self, path=".", members=None, *, numeric_owner=False, **kw):
        if "filter" in kw:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        return original(self, path, members, numeric_owner=numeric_owner)
    monkeypatch.setattr(tarfile.TarFile, "extractall", old_extractall)          # `data_filter` is left in place on purpose: Debian's patched tarfile has it but not a working `filter=`
    store, state = setup
    work = tmp_path / "w"; work.mkdir()
    archive = offsite.build_archive(store, state / "telemetry", work, NOW)
    assert offsite.verify_archive(archive)["telemetry_files"] == 21
    evil = work / "evil.tar.gz"
    (work / "x.txt").write_text("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(work / "x.txt", arcname="../escaped.txt")
    with pytest.raises(offsite.BackupError, match="unsafe member"):
        offsite.verify_archive(evil)
    assert not (tmp_path / "escaped.txt").exists()


def _archived_database(archive, folder):
    import sqlite3
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(folder)
    return sqlite3.connect(f"file:{folder}/controller.sqlite3?mode=ro", uri=True)


def test_the_off_site_copy_leaves_out_old_plan_snapshots_but_never_the_live_database(setup, tmp_path):
    """12 and 14 November: the plan snapshots were 800 MB of a 1 GB database and the uploads kept hitting Google's rate limit."""
    import sqlite3
    store, state = setup
    old1, old2, linked, recent = "a" * 64, "b" * 64, "c" * 64, "d" * 64
    with sqlite3.connect(store.path) as db:
        for ident, age_h in ((old1, 48), (old2, 30), (linked, 100), (recent, 1)):
            db.execute("INSERT INTO issuances VALUES(?,?,?,?)", (ident, NOW - age_h * 3600, "{}", b"x" * 1000))
        db.execute("INSERT INTO settings VALUES(?,?,?)", ("active_plan", 1, json.dumps({"issuance": linked})))
    folder = tmp_path / "out"; folder.mkdir()
    archive = offsite.build_archive(store, state / "telemetry", folder, NOW)
    db = _archived_database(archive, tmp_path / "unpacked")
    try:
        kept = {row[0] for row in db.execute("SELECT id FROM issuances")}
        assert kept == {linked, recent}                                        # new enough, or still pointed to by a setting
        assert db.execute("SELECT count(*) FROM settings").fetchone()[0] == 1   # everything else is untouched
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='issuances_no_delete'").fetchone()[0] == 1   # protection put back in the copy
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
    with sqlite3.connect(store.path) as live:
        assert live.execute("SELECT count(*) FROM issuances").fetchone()[0] == 4      # the live database lost nothing


def test_slimming_shrinks_the_copy_and_reports_what_it_removed(setup, tmp_path):
    import sqlite3
    store, state = setup
    with sqlite3.connect(store.path) as db:
        for i in range(60):
            db.execute("INSERT INTO issuances VALUES(?,?,?,?)", (f"{i:064x}", NOW - 72 * 3600 - i, "{}", b"y" * 100_000))
    work = tmp_path / "w"; work.mkdir()
    copy = work / "c.sqlite3"
    store.backup(copy)
    before = copy.stat().st_size
    removed, kept = offsite.slim_database(store, copy, NOW)
    assert (removed, kept) == (60, 0)
    assert copy.stat().st_size < before / 5                                   # VACUUM gave the space back
