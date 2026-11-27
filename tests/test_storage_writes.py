"""Disk-write reduction: tuning, batching, recovery and live readings."""
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading

import pytest

from solarbank_optimiser.collector import Collector, CollectorConfig, PROVENANCE
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import StorageTuning, Store
from test_collector import payload


def make(tmp_path, tuning=None, live=False, flush=30, **config):
    store = Store(tmp_path / "c.sqlite3", tuning, tmp_path / "live.json" if live else None)
    store.initialise()
    clock = FakeClock(101)
    cycle = {"n": 0}

    def fetch(_):
        cycle["n"] += 1
        return json.dumps(payload(int(clock.utc()))).encode()
    worker = Collector(store, CollectorConfig(flush_interval_seconds=flush, **config), clock, fetch=fetch)
    return store, clock, worker


def counts(store):
    with closing(store.connect()) as db:
        return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("collector_snapshots", "observations", "events", "charger_samples")}


def run_cycles(clock, worker, n):
    for _ in range(n):
        worker.once()
        clock.advance(1)


def test_readings_are_held_then_committed_in_one_batch(tmp_path):
    store, clock, worker = make(tmp_path, flush=30)
    run_cycles(clock, worker, 1)           # first cycle is written at once so a restart is visible immediately
    first = counts(store)["collector_snapshots"]
    assert first == 1
    run_cycles(clock, worker, 20)
    assert counts(store)["collector_snapshots"] == 1 and len(worker.pending) == 20
    clock.advance(30)
    worker.once()
    after = counts(store)
    assert after["collector_snapshots"] == 22 and after["events"] == 22 and after["charger_samples"] == 22
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0          # readings are not kept in the database
    assert not worker.pending
    assert store.get_setting("collector_health")[0] == 2    # one health write per commit, not per reading


def test_timestamps_survive_batching_exactly(tmp_path):
    store, clock, worker = make(tmp_path, flush=30)
    run_cycles(clock, worker, 10)
    worker.flush()
    with closing(store.connect()) as db:
        times = [r[0] for r in db.execute("SELECT acquired_at FROM collector_snapshots ORDER BY acquired_at")]
    assert times == [101 + i for i in range(10)]


def test_graceful_stop_flushes_everything(tmp_path):
    store, clock, worker = make(tmp_path, flush=300)
    stop = threading.Event()
    clock_waits = []
    stop.wait = lambda t: clock_waits.append(t) or clock.advance(1)
    worker.run(stop, count=7)
    assert counts(store)["collector_snapshots"] == 7 and not worker.pending


def test_failed_commit_keeps_readings_and_retry_has_no_duplicates(tmp_path, monkeypatch):
    store, clock, worker = make(tmp_path, flush=0)
    real = store.record_snapshots
    state = {"fail": 3}

    def flaky(*args, **kwargs):
        if state["fail"]:
            state["fail"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)
    monkeypatch.setattr(store, "record_snapshots", flaky)
    run_cycles(clock, worker, 5)
    assert worker.storage_errors == 3 and not worker.pending
    c = counts(store)
    assert c["collector_snapshots"] == 5 and c["observations"] == 0     # nothing lost, nothing doubled, and no reading rows


def test_replaying_a_committed_entry_adds_nothing(tmp_path):
    store, clock, worker = make(tmp_path, flush=30)
    run_cycles(clock, worker, 4)
    entries = list(worker.pending)
    worker.flush()
    before = counts(store)
    store.record_snapshots(entries, keep_observations=False)
    assert counts(store) == before


def test_memory_is_bounded_when_the_database_stays_unavailable(tmp_path, monkeypatch):
    store, clock, worker = make(tmp_path, flush=0, max_pending=5)
    monkeypatch.setattr(store, "record_snapshots", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    run_cycles(clock, worker, 20)
    assert len(worker.pending) == 5 and worker.dropped == 15


def test_unflushed_readings_are_lost_cleanly_after_abrupt_stop(tmp_path):
    script = textwrap.dedent(f"""
        import json, os, sys
        sys.path.insert(0, {str(os.path.dirname(__file__))!r})
        from test_collector import payload
        from solarbank_optimiser.collector import Collector, CollectorConfig
        from solarbank_optimiser.models import FakeClock
        from solarbank_optimiser.store import Store
        store = Store({str(tmp_path / 'k.sqlite3')!r}); store.initialise()
        clock = FakeClock(101)
        w = Collector(store, CollectorConfig(flush_interval_seconds=30), clock,
                      fetch=lambda _: json.dumps(payload(int(clock.utc()))).encode())
        for i in range(10):
            w.once(); clock.advance(1)
            if i == 4: clock.advance(30)      # forces a commit at cycle 6
        os.kill(os.getpid(), 9)
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")})
    assert result.returncode == -9
    store = Store(tmp_path / "k.sqlite3"); store.initialise()
    with closing(store.connect()) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    committed = counts(store)["collector_snapshots"]
    assert 1 <= committed <= 10
    clock = FakeClock(500)
    worker = Collector(store, CollectorConfig(flush_interval_seconds=30), clock, fetch=lambda _: json.dumps(payload(500)).encode())
    worker.once(); worker.flush()
    assert counts(store)["collector_snapshots"] == committed + 1


def test_concurrent_writers_and_readers_do_not_fail_or_lose_rows(tmp_path):
    store, clock, worker = make(tmp_path, flush=0)
    errors = []
    stop = threading.Event()

    def web():
        other = Store(tmp_path / "c.sqlite3")
        try:
            while not stop.is_set():
                other.latest_observations(PROVENANCE)
                other.get_setting("collector_health")
        except Exception as exc:
            errors.append(exc)

    def settings():
        other = Store(tmp_path / "c.sqlite3")
        try:
            for i in range(30):
                other.set_setting("x", {"i": i}, 1000 + i, i)
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=web), threading.Thread(target=settings)]
    for t in threads:
        t.start()
    run_cycles(clock, worker, 30)
    threads[1].join()
    stop.set()
    threads[0].join()
    assert not errors and counts(store)["collector_snapshots"] == 30 and store.get_setting("x")[0] == 30


def test_tuning_is_applied_to_every_connection(tmp_path):
    store = Store(tmp_path / "t.sqlite3", StorageTuning("NORMAL", 2500)); store.initialise()
    with closing(store.connect()) as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 2500
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 2
    full = Store(tmp_path / "t.sqlite3", StorageTuning("FULL"))
    with closing(full.connect()) as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    for bad in ("OFF", "off", ""):
        with pytest.raises(ValueError):
            StorageTuning(bad)


def test_log_file_persists_between_short_connections_only_when_held_open(tmp_path):
    held = Store(tmp_path / "h.sqlite3"); held.initialise()
    held.set_setting("a", 1, 1, 0)
    assert (tmp_path / "h.sqlite3-wal").stat().st_size > 0      # not checkpointed/deleted at connection close
    held.close()
    assert not (tmp_path / "h.sqlite3-wal").exists() or (tmp_path / "h.sqlite3-wal").stat().st_size == 0
    legacy = Store(tmp_path / "l.sqlite3", StorageTuning.legacy()); legacy.initialise()
    legacy.set_setting("a", 1, 1, 0)
    assert not (tmp_path / "l.sqlite3-wal").exists()            # the old per-commit checkpoint-and-delete


def test_checkpoint_happens_automatically_and_preserves_data(tmp_path):
    store = Store(tmp_path / "w.sqlite3", StorageTuning("NORMAL", 100)); store.initialise()
    for i in range(60):
        store.archive(float(i), os.urandom(20_000), {"i": i})
    # 100-page threshold: the log was copied into the main file, which therefore holds data.
    assert (tmp_path / "w.sqlite3").stat().st_size > 400_000
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM issuances").fetchone()[0] == 60
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_database_written_by_the_old_configuration_opens_and_continues(tmp_path):
    old = Store(tmp_path / "o.sqlite3", StorageTuning.legacy()); old.initialise()
    clock = FakeClock(101)
    first = Collector(old, CollectorConfig(), clock, fetch=lambda _: json.dumps(payload(101)).encode())
    first.once()
    new = Store(tmp_path / "o.sqlite3"); new.initialise()
    clock.advance(1)
    second = Collector(new, CollectorConfig(flush_interval_seconds=30), clock, fetch=lambda _: json.dumps(payload(102)).encode())
    second.once(); second.flush()
    assert counts(new)["collector_snapshots"] == 2
    with closing(new.connect()) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_still_works_with_held_connection_and_pending_batches(tmp_path):
    store, clock, worker = make(tmp_path, flush=30)
    run_cycles(clock, worker, 5)
    worker.flush()
    copy = store.backup(tmp_path / "backup.sqlite3")
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute("SELECT count(*) FROM collector_snapshots").fetchone()[0] == 5


def test_live_readings_stay_current_between_commits_and_bad_file_is_ignored(tmp_path):
    store, clock, worker = make(tmp_path, live=True, flush=60)
    worker.once()                       # committed (first cycle)
    clock.advance(10)
    worker.once()                       # held in memory only
    assert counts(store)["collector_snapshots"] == 1
    latest = store.latest_observations(PROVENANCE)
    assert latest["soc"]["retrieved_at"] == 111
    (tmp_path / "live.json").write_text("{not json")
    assert store.latest_observations(PROVENANCE).get("soc") is None             # a damaged live file is ignored: no reading is invented, and the database holds none
    (tmp_path / "live.json").write_text(json.dumps({"observations": [{"bogus": 1}]}))
    assert store.live_observations(PROVENANCE) == {}


def test_shadow_planner_sees_fresh_soc_while_database_commit_is_pending(tmp_path):
    from solarbank_optimiser.shadow import snapshot
    store, clock, worker = make(tmp_path, live=True, flush=60)
    worker.once(); clock.advance(20); worker.once()
    assert snapshot(store, clock.utc())["soc"]["retrieved_at"] == 121
    # Without the live file there is no reading to plan from: the database holds none.
    plain = Store(tmp_path / "c.sqlite3")
    assert snapshot(plain, clock.utc())["soc"] is None
