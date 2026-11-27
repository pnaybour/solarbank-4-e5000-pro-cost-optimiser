"""WP3 (#12): the event written with each collector snapshot is purged with it, and the events table is indexed by kind and time."""
from contextlib import closing
import json

import pytest

from solarbank_optimiser.store import Store, SCHEMA_VERSION, MIGRATION_4


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "e.sqlite3"); s.initialise()
    return s


def snapshot(store, n, at):
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO collector_snapshots VALUES(?,?,?,?)", (f"s{n}", at, "ok", b"x" * 100))
        db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)", (at, "collector_snapshot", json.dumps({"snapshot_id": f"s{n}", "status": "ok"})))


def events(store, kind="collector_snapshot"):
    with closing(store.connect()) as db:
        return db.execute("SELECT count(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]


def test_a_new_database_has_the_index_and_the_current_schema_version(store):
    with closing(store.connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
        assert "events_kind_time" in {r[1] for r in db.execute("PRAGMA index_list(events)")}


def test_a_version_3_database_gains_the_index_and_keeps_its_data(tmp_path):
    s = Store(tmp_path / "old.sqlite3"); s.initialise()
    with closing(s.connect()) as db, db:
        db.execute("DROP INDEX events_kind_time"); db.execute("PRAGMA user_version=3")
        db.execute("INSERT INTO events(time,kind,payload) VALUES(1,'keepme','{}')")
    Store(tmp_path / "old.sqlite3").initialise()
    with closing(s.connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "events_kind_time" in {r[1] for r in db.execute("PRAGMA index_list(events)")}
        assert db.execute("SELECT count(*) FROM events WHERE kind='keepme'").fetchone()[0] == 1


def test_a_failing_migration_leaves_a_version_3_database_at_version_3(tmp_path, monkeypatch):
    import solarbank_optimiser.store as module
    s = Store(tmp_path / "old.sqlite3"); s.initialise()
    with closing(s.connect()) as db, db:
        db.execute("DROP INDEX events_kind_time"); db.execute("PRAGMA user_version=3")
    monkeypatch.setattr(module, "MIGRATION_4", MIGRATION_4 + ("INVALID SQL",))
    with pytest.raises(Exception):
        Store(tmp_path / "old.sqlite3").initialise()
    with closing(s.connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_the_purge_removes_the_events_of_the_snapshots_it_removes_and_only_those(store):
    for n in range(6):
        snapshot(store, n, 100 + n)
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO events(time,kind,payload) VALUES(50,'settings_changed','{}')")
    assert store.prune_raw(103) == 3                       # snapshots at 100, 101, 102
    assert events(store) == 3 and events(store, "settings_changed") == 1


def test_the_byte_budget_purge_takes_its_events_too(store):
    for n in range(5):
        snapshot(store, n, 100 + n)
    assert store.prune_raw(0, max_bytes=250) == 3          # 100-byte snapshots: the newest two fit
    assert events(store) == 2


def test_events_older_than_any_snapshot_are_removed_by_the_first_purge_in_batches(store):
    store.EVENT_PURGE_BATCH = 7
    with closing(store.connect()) as db, db:
        db.executemany("INSERT INTO events(time,kind,payload) VALUES(?,?,?)", [(i, "collector_snapshot", "{}") for i in range(100)])
    snapshot(store, 1, 500)
    store.prune_raw(0)
    assert events(store) == 1                               # the legacy events had no snapshot left; the live one stays


def test_with_no_snapshots_left_every_snapshot_event_goes(store):
    snapshot(store, 1, 100)
    assert store.prune_raw(1000) == 1 and events(store) == 0


def test_each_purge_batch_uses_the_index(store):
    with closing(store.connect()) as db:
        plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN SELECT id FROM events WHERE kind='collector_snapshot' AND time<? ORDER BY time LIMIT 5", (10,)))
    assert "events_kind_time" in plan and "TEMP B-TREE" not in plan
