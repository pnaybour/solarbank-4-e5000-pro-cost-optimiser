"""Review item 2: with the database away for a very long time, nothing in memory may grow without limit, and what was shed is on record."""
import json
import sqlite3

import pytest

from solarbank_optimiser.collector import Collector, CollectorConfig
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store
from test_collector import payload


def outage(tmp_path, record_seconds, **limits):
    store = Store(tmp_path / "o.sqlite3"); store.initialise()
    clock = FakeClock(1_800_000_000)
    state = {"i": 0}

    def fetch(_):
        state["i"] += 1
        if state["i"] % 2:                                        # every other cycle the source fails: a status change each time
            raise OSError("monitor flapping")
        data = payload(int(clock.utc()))
        data["shelly"]["sample"]["ch2"] = 2000.0                  # a car charging: charger samples and energy pieces every second
        return json.dumps(data).encode()
    worker = Collector(store, CollectorConfig(record_seconds=record_seconds, flush_interval_seconds=1,
                                              confirmation_seconds=0, **limits), clock, fetch=fetch)
    if worker.minute_log is not None:
        worker.tracker.trim_every = 20
    store.record_snapshots = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk is full"))
    return store, clock, worker


def run_outage(clock, worker, seconds):
    for _ in range(seconds):
        worker.once()
        clock.advance(1)


def rows_in_memory(worker):
    return {t: worker.tracker.mem.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("charger_samples", "charger_energy", "charger_coverage")}


def test_minute_mode_every_queue_stays_within_its_limit_through_a_long_outage(tmp_path):
    store, clock, worker = outage(tmp_path, 60, max_pending=120, max_pending_rows=40, max_charger_rows=400, max_keep_per_minute=5)
    run_outage(clock, worker, 4800)                                # eighty minutes; the limits are tiny so this is a strong test
    assert len(worker.sink.rows) <= 40
    assert len(worker.minute_log.buckets) <= max(2, 120 // 60 + 1)
    assert all(len(b["keep"]) <= 5 for b in worker.minute_log.buckets.values())
    assert all(n <= 400 + 20 for n in rows_in_memory(worker).values())
    health = worker.once()
    kinds = health["discarded_total"]
    assert kinds["minute_summaries"] > 30 and kinds["status_change_evidence"] > 0 and kinds["charger_samples"] > 0
    assert health["dropped_snapshots"] == kinds["minute_summaries"]
    recent = health["discarded_recent"]
    assert 1 <= len(recent) <= 20 and all(r["count"] >= 1 and r["from"] is not None for r in recent)


def test_legacy_per_second_mode_is_bounded_and_records_the_loss(tmp_path):
    store, clock, worker = outage(tmp_path, 1, max_pending=50, max_pending_bytes=10_000_000)
    run_outage(clock, worker, 400)
    assert len(worker.pending) <= 50
    health = worker.once()
    assert health["discarded_total"]["cycles"] >= 340 and health["dropped_snapshots"] >= 340
    assert health["discarded_recent"][-1]["kind"] == "cycles" and health["discarded_recent"][-1]["from"] is not None


def test_the_record_of_what_was_lost_is_stored_once_the_database_returns(tmp_path):
    store, clock, worker = outage(tmp_path, 60, max_pending=120, max_pending_rows=40, max_charger_rows=400, max_keep_per_minute=5)
    run_outage(clock, worker, 1500)
    del store.record_snapshots                                     # the database comes back
    clock.advance(1); worker.once()
    assert worker.flush(final=True) >= 1
    with store.connect() as db:
        events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='collector_snapshot' ORDER BY time")]
    assert events and events[-1]["discarded_total"]["minute_summaries"] > 0            # the gap is on record beside the data that followed it


def test_limits_are_validated():
    for name in ("max_pending_rows", "max_charger_rows", "max_keep_per_minute"):
        with pytest.raises(ValueError):
            CollectorConfig(**{name: 0})


def test_minute_observation_queue_sheds_the_oldest_and_says_what_and_when():
    from solarbank_optimiser.minutelog import MinuteSink
    from solarbank_optimiser.models import Observation
    dropped = []
    sink = MinuteSink(store=None, max_rows=5, on_drop=lambda *a: dropped.append(a))
    rows = [Observation("m", "household_power", 1000.0 + 60 * i + 60, .4, "kW", interval_start=1000.0 + 60 * i) for i in range(12)]
    sink.add_observations(rows[:8]); sink.add_observations(rows[8:])
    assert len(sink.rows) == 5 and sink.rows == rows[-5:]               # the newest survive
    assert sum(d[1] for d in dropped) == 7 and dropped[0][0] == "minute_observations"
    assert min(d[2] for d in dropped) == 1000.0 and max(d[3] for d in dropped) == 1000.0 + 60 * 6


def test_a_healthy_run_discards_nothing_and_logs_nothing(tmp_path, capsys):
    store = Store(tmp_path / "h.sqlite3"); store.initialise()
    clock = FakeClock(1_800_000_000)
    worker = Collector(store, CollectorConfig(record_seconds=60, flush_interval_seconds=1), clock,
                       fetch=lambda _: json.dumps(payload(int(clock.utc()))).encode())
    for _ in range(300):
        worker.once(); clock.advance(1)
    health = worker.once()
    assert health["discarded_total"] == {} and health["discarded_recent"] == [] and health["dropped_snapshots"] == 0
    assert "memory limit" not in capsys.readouterr().err


def test_outage_logging_is_throttled_not_one_line_per_second(tmp_path, capsys):
    store, clock, worker = outage(tmp_path, 60, max_pending=120, max_pending_rows=40, max_charger_rows=400, max_keep_per_minute=5)
    run_outage(clock, worker, 1200)                                   # 1,200 failed commits and many discards, all within a few real seconds
    lines = capsys.readouterr().err.strip().splitlines()
    assert 1 <= len(lines) <= 12, len(lines)                           # a handful (one per kind), not thousands


@pytest.mark.parametrize("record_seconds", [1, 60])
def test_the_tick_never_writes_to_the_database_even_across_minute_boundaries(tmp_path, record_seconds):
    """Regression: in per-second mode the tick itself wrote each completed-minute average and crashed on a locked database."""
    store = Store(tmp_path / "t.sqlite3"); store.initialise()
    clock = FakeClock(1_800_000_020)                                # 40 s before a minute boundary; the run crosses three of them
    worker = Collector(store, CollectorConfig(record_seconds=record_seconds, flush_interval_seconds=300),
                       clock, fetch=lambda _: json.dumps(payload(int(clock.utc()))).encode())
    worker.once()                                                    # the first flush happens now (nothing blocks yet)

    def forbidden(*args, **kwargs):
        raise AssertionError("the tick wrote to the database")
    store.add_observations = forbidden
    store.record_snapshots = forbidden
    store.prune_raw = forbidden
    for _ in range(190):
        clock.advance(1)
        worker.once()                                                # never raises, never touches the database
    assert not worker.sink.rows                                      # no minute rows are recorded in SQLite any more (WP23b)


@pytest.mark.parametrize("record_seconds", [1, 60])
def test_minute_averages_reach_the_database_with_the_next_commit(tmp_path, record_seconds):
    store = Store(tmp_path / "m.sqlite3"); store.initialise()
    clock = FakeClock(1_800_000_020)
    worker = Collector(store, CollectorConfig(record_seconds=record_seconds, flush_interval_seconds=300),
                       clock, fetch=lambda _: json.dumps(payload(int(clock.utc()))).encode())
    for _ in range(190):
        worker.once(); clock.advance(1)
    before = len(store.observations(metric="household_power"))
    worker.flush(final=True)
    rows = [r for r in store.observations(metric="household_power") if r["provenance"] == "shared-monitor-minute"]
    assert rows == [] and not worker.sink.rows                       # minute means are no longer written to SQLite (WP23b)
