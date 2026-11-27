"""One-minute stored history with one-second live readings and charger tracking."""
from contextlib import closing
import json
import sqlite3
import threading

import pytest

from solarbank_optimiser.collector import Collector, CollectorConfig, PROVENANCE
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store
from test_collector import payload

START = 120    # a minute boundary


def build(tmp_path, record=60, flush=60, ev=lambda t: 7., fetch=None, **config):
    store = Store(tmp_path / "m.sqlite3", live_path=tmp_path / "live.json")
    store.initialise()
    clock = FakeClock(START)

    def default_fetch(_):
        t = int(clock.utc())
        data = payload(t)
        data["solarbank"]["sample"]["soc"] = 50 + (t - START) / 100
        data["solarbank"]["sample"]["pv"] = 400 + (t % 60)
        data["shelly"]["sample"]["ch2"] = ev(t)
        return json.dumps(data).encode()
    worker = Collector(store, CollectorConfig(record_seconds=record, flush_interval_seconds=flush,
                                              confirmation_seconds=0, **config), clock, fetch=fetch or default_fetch)
    return store, clock, worker


def cycles(clock, worker, n):
    for _ in range(n):
        worker.once()
        clock.advance(1)


def rows(store, sql, *args):
    with closing(store.connect()) as db:
        return db.execute(sql, args).fetchall()


def test_stores_one_raw_snapshot_and_event_per_minute_and_no_reading_rows(tmp_path):
    """Readings live in the live file and the telemetry files only (one source of truth): the database gets the raw response, the event and the health."""
    store, clock, worker = build(tmp_path)
    cycles(clock, worker, 185)                      # 3 full minutes plus 5 s
    worker.flush()
    assert rows(store, "SELECT count(*) FROM observations")[0][0] == 0
    assert rows(store, "SELECT count(*) FROM collector_snapshots")[0][0] == 3
    assert rows(store, "SELECT count(*) FROM events WHERE kind='collector_snapshot'")[0][0] == 3
    assert store.latest_observations(PROVENANCE)["soc"]["quality"] == "ok"           # the newest reading is in the live file


def test_final_flush_keeps_the_partial_minute_and_nothing_twice(tmp_path):
    store, clock, worker = build(tmp_path)
    stop = threading.Event()
    stop.wait = lambda t: clock.advance(1)
    worker.run(stop, count=95)
    assert rows(store, "SELECT count(*) FROM observations")[0][0] == 0
    worker.flush(final=True)
    assert rows(store, "SELECT count(*) FROM collector_snapshots")[0][0] == 2


def test_live_status_is_current_while_the_database_is_minutes_old(tmp_path):
    store, clock, worker = build(tmp_path, ev=lambda t: 2000.)
    cycles(clock, worker, 20)
    assert rows(store, "SELECT count(*) FROM collector_snapshots")[0][0] == 0
    revision, health = store.get_setting("collector_health")
    assert health["acquired_at"] == START + 19
    _, tracker = store.get_setting("charger_tracker", {})
    assert tracker["label"] == "charging" and tracker["last_t"] == START + 19
    assert store.latest_observations(PROVENANCE)["soc"]["retrieved_at"] == START + 19
    assert Store(tmp_path / "m.sqlite3").get_setting("collector_health")[1] is None    # without the live file only the database is seen


def test_charger_energy_matches_one_second_storage(tmp_path):
    def ev(t):
        return 2000. if 150 <= t < 400 else 5.
    results = []
    for record, flush, sub in ((1, 0, "a"), (60, 60, "b")):
        (tmp_path / sub).mkdir()
        store, clock, worker = build(tmp_path / sub, record=record, flush=flush, ev=ev, release_seconds=10)
        cycles(clock, worker, 700)
        worker.flush(final=True)
        sessions = [json.loads(r[0]) for r in rows(store, "SELECT payload FROM charger_sessions")]
        energy = rows(store, "SELECT sum(kwh),sum(active_seconds),count(*) FROM charger_energy")[0]
        results.append((len(sessions), round(sessions[0]["energy_kwh"], 9), round(energy[0], 9), energy[1], energy[2],
                        sessions[0]["ended_at"] is not None))
    assert results[0] == results[1] and results[0][0] == 1 and results[0][1] > 0


def test_charger_samples_are_no_longer_written_to_the_database_but_the_session_and_its_energy_are(tmp_path):
    """The car's power over time is the ev_power series in the telemetry files; the database keeps the records derived from it (sessions, energy)."""
    store, clock, worker = build(tmp_path, ev=lambda t: 2000.)
    cycles(clock, worker, 180)
    worker.flush(final=True)
    assert rows(store, "SELECT count(*) FROM charger_samples")[0][0] == 0
    assert rows(store, "SELECT count(*) FROM charger_sessions")[0][0] == 1 and rows(store, "SELECT sum(kwh) FROM charger_energy")[0][0] > 0


def test_failed_commit_is_retried_without_loss_or_duplicates(tmp_path, monkeypatch):
    store, clock, worker = build(tmp_path, ev=lambda t: 2000. if t > 130 else 5.)
    real = store.record_snapshots
    state = {"fail": 2}

    def flaky(*a, **k):
        if state["fail"]:
            state["fail"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(store, "record_snapshots", flaky)
    cycles(clock, worker, 130)
    worker.flush(); worker.flush(); worker.flush(final=True)
    assert worker.storage_errors == 2
    assert rows(store, "SELECT count(*) FROM collector_snapshots")[0][0] == 3
    assert rows(store, "SELECT count(*) FROM observations")[0][0] == 0
    assert rows(store, "SELECT count(*) FROM charger_sessions")[0][0] == 1
    first = rows(store, "SELECT sum(kwh) FROM charger_energy")[0][0]
    worker.flush(final=True)
    assert rows(store, "SELECT sum(kwh) FROM charger_energy")[0][0] == first


def test_outage_start_is_kept_at_full_resolution(tmp_path):
    state = {"down": False}
    clock_box = {}

    def fetch(_):
        if state["down"]:
            raise OSError("monitor down")
        return json.dumps(payload(int(clock_box["c"].utc()))).encode()
    store, clock, worker = build(tmp_path, fetch=fetch)
    clock_box["c"] = clock
    cycles(clock, worker, 30)
    state["down"] = True
    cycles(clock, worker, 10)
    state["down"] = False
    cycles(clock, worker, 10)
    worker.flush(final=True)
    statuses = [json.loads(r[0])["status"] for r in rows(store, "SELECT payload FROM events WHERE kind='collector_snapshot' ORDER BY time")]
    assert "unavailable" in statuses and statuses.count("ok") >= 2     # failure and recovery both on record


def test_restart_continues_an_open_session_and_marks_it_partial(tmp_path):
    store, clock, worker = build(tmp_path, ev=lambda t: 2000.)
    cycles(clock, worker, 90)
    worker.flush(final=True)
    session_id = rows(store, "SELECT id FROM charger_sessions")[0][0]
    store2 = Store(tmp_path / "m.sqlite3", live_path=tmp_path / "live.json")
    worker2 = Collector(store2, CollectorConfig(record_seconds=60, flush_interval_seconds=60, confirmation_seconds=0), clock,
                        fetch=lambda _: json.dumps({**payload(int(clock.utc())), "shelly": {"ok": True, "sample": {"time": int(clock.utc()), "ch1": 5., "ch2": 2000.}}}).encode())
    cycles(clock, worker2, 30)
    worker2.flush(final=True)
    assert [r[0] for r in rows(store2, "SELECT id FROM charger_sessions")] == [session_id]
    assert json.loads(rows(store2, "SELECT payload FROM charger_sessions")[0][0])["partial"] is True


def test_storage_is_far_smaller_than_one_second_logging(tmp_path):
    sizes = {}
    for record, flush, sub in ((1, 0, "one"), (60, 60, "min")):
        (tmp_path / sub).mkdir()
        store, clock, worker = build(tmp_path / sub, record=record, flush=flush)
        cycles(clock, worker, 600)
        worker.flush(final=True)
        sizes[sub] = rows(store, "SELECT count(*) FROM observations")[0][0], rows(store, "SELECT count(*) FROM collector_snapshots")[0][0]
    assert sizes["one"][0] == sizes["min"][0] == 0 and sizes["one"][1] == 600 and sizes["min"][1] == 10          # no reading rows either way; raw snapshots 600 against 10


def test_unknown_resolution_rejected():
    with pytest.raises(ValueError):
        CollectorConfig(record_seconds=30)
