"""Code-review findings against release 0.5.7: records must not be lost behind a slow save, and a reading's age is from its arrival."""
import json
import threading
import time

from solarbank_optimiser.collector import Collector, CollectorConfig
from solarbank_optimiser.devices import solarbank
from solarbank_optimiser.live import DeviceLoop, LiveState
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store
from test_collector import payload
from test_devices import FIXTURES_RAW, FakeModbus, split_registers
from solarbank_optimiser.devices.modbus import ReadOnlyModbusTcp


def collector(tmp_path, record_seconds, flush=1, **limits):
    store = Store(tmp_path / "r.sqlite3"); store.initialise()
    clock = FakeClock(1_800_000_000)
    worker = Collector(store, CollectorConfig(record_seconds=record_seconds, flush_interval_seconds=flush, confirmation_seconds=0, **limits),
                       clock, fetch=lambda _: json.dumps(payload(int(clock.utc()))).encode())
    return store, clock, worker


def test_cycles_arriving_during_a_slow_save_are_not_deleted_when_it_finishes(tmp_path):
    store, clock, worker = collector(tmp_path, 1, flush=300, max_pending=5, max_pending_bytes=10_000_000)
    for _ in range(3):
        clock.advance(1); worker.once()
    assert len(worker.pending) >= 1
    template = worker.pending[0]
    real = store.record_snapshots
    late = []

    def slow_save(batch, *args, **kwargs):
        for i in range(8):                                  # while the database is busy, more arrive and the old front is trimmed
            entry = (template[0] + b" ", template[1], template[2] + 1 + i, template[3]); late.append(entry)
            worker._enqueue(*entry)
        return real(batch, *args, **kwargs)
    store.record_snapshots = slow_save
    worker.flush()
    store.record_snapshots = real
    assert len(late) == 8 and len(worker.pending) == 5
    assert [e[2] for e in worker.pending] == [e[2] for e in late[-5:]], "unsaved cycles vanished after the save"
    assert worker.pending_bytes == sum(len(e[0]) for e in worker.pending)


def test_minute_rows_arriving_during_a_slow_save_are_kept(tmp_path):
    store, clock, worker = collector(tmp_path, 60, max_pending_rows=5)
    real = store.record_snapshots
    late = []

    def slow_save(*args, **kwargs):
        from solarbank_optimiser.models import Observation, Quality
        for i in range(8):                                  # the sink sheds its oldest rows while the save is running
            row = Observation("fx", "soc", 1.0 + i, .5, "fraction", Quality.OK)
            worker.sink.add_observations([row]); late.append(row)
        return real(*args, **kwargs)
    worker.sink.add_observations([])
    from solarbank_optimiser.models import Observation, Quality
    worker.sink.add_observations([Observation("fx", "soc", 0.5, .5, "fraction", Quality.OK)])
    store.record_snapshots = slow_save
    worker.flush(final=True)
    kept = worker.sink.rows
    assert [r for r in late[-5:]] == kept[-5:] and len(kept) == 5


class SlowAfterwards:
    """A reader whose fast data arrives at once and which then spends a while on slower requests."""
    def __init__(self, pause): self.pause, self.received_at = pause, None

    def __call__(self):
        self.received_at = time.monotonic()
        time.sleep(self.pause)
        return {"soc": 0.5}


def test_a_reading_is_stamped_when_its_fast_data_arrived_not_when_the_slow_requests_finished():
    live, stop = LiveState(), threading.Event()
    loop = DeviceLoop("dev", SlowAfterwards(0.4), 5.0, live, stop, backoff=(0, 0.02)); loop.start()
    time.sleep(0.6); stop.set(); loop.join(2)
    reading, _ = live.snapshot()["dev"]
    assert time.monotonic() - reading.mono >= 0.55 - 0.2 and time.monotonic() - reading.mono > 0.4 + 0.05, "reading looks fresher than it is"
    assert abs(time.time() - reading.utc - (time.monotonic() - reading.mono)) < 0.05


def test_solarbank_reader_reports_the_arrival_of_the_fast_reply_before_it_reads_the_slow_blocks():
    regs = split_registers(FIXTURES_RAW()["solarbank"][0]["raw"])
    server = FakeModbus(regs)
    stamps = []
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        original = client.read_input_registers

        def watching(start, count):
            result = original(start, count)
            if (start, count) == solarbank.FAST_BLOCK:
                time.sleep(0.0)
            else:
                stamps.append(reader.received_at)            # when a slow block is requested, the fast reply's stamp is already set
            return result
        client.read_input_registers = watching
        reader = solarbank.Reader(client, slow_every=10)
        reader()
        assert stamps and all(s is not None for s in stamps)
        assert reader.received_at <= time.monotonic()
        client.close()
    finally:
        server.stop()


def test_later_history_pages_seek_to_the_cursor_instead_of_rescanning(tmp_path):
    """Review: the paging query must be a pure index range from the cursor, so page N costs the same as page 1."""
    import sqlite3
    store = Store(tmp_path / "p.sqlite3"); store.initialise()
    plan_sql = ("EXPLAIN QUERY PLAN SELECT sample_at,id,payload FROM observations WHERE metric=? AND sample_at>=? AND sample_at<? "
                "AND sample_at>=? AND (sample_at>? OR id>?) ORDER BY sample_at,id LIMIT ?")
    with sqlite3.connect(store.path) as db:
        plan = " ".join(row[3] for row in db.execute(plan_sql, ["soc", 0, 10, 5, 5, "x", 10]))
    assert "SEARCH observations USING INDEX observations_lookup (metric=? AND sample_at>? AND sample_at<?)" in plan, plan
    assert "SCAN" not in plan                                                # never a full table scan or a rescan from the start


def test_paging_through_records_that_share_a_timestamp_misses_and_repeats_nothing(tmp_path):
    from solarbank_optimiser.models import Observation, Quality
    store = Store(tmp_path / "q.sqlite3"); store.initialise()
    rows = [Observation(f"fx{n}", "soc", 100 + (n // 4), n / 100, "fraction", Quality.OK, source_at=100 + (n // 4)) for n in range(40)]
    store.add_observations(rows)
    seen, cursor = [], None
    while True:
        page, cursor = store.observations_page(0, 1000, limit=7, cursor=cursor)
        seen += [(r["source_at"], r["value"]) for r in page]
        if cursor is None:
            break
    assert sorted(seen) == sorted((100 + (n // 4), n / 100) for n in range(40)) and len(seen) == len(set(seen)) == 40


def test_car_history_summary_has_no_minute_series_and_minutes_are_paged(tmp_path):
    from datetime import date
    from solarbank_optimiser.car_history import car_history, car_power_minutes
    from solarbank_optimiser.energy import day_bounds
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    day = date(2026, 10, 20); start = day_bounds(day)[0]
    from conftest import seed_minutes
    seed_minutes(store, "ev_power", start, start + 100 * 60, 7.0)                              # 100 minutes of charger power in the telemetry files
    assert "power_minutes" not in car_history(store, day, day)
    seen, after, pages = [], None, 0
    while True:
        rows, after = car_power_minutes(store, day, day, limit=30, after=after)
        seen += [r["minute_utc"] for r in rows]; pages += 1
        assert len(rows) <= 30
        if after is None:
            break
    assert pages == 4 and seen == [start + 60 * n for n in range(100)]
    for bad in (0, 5001, True):
        try:
            car_power_minutes(store, day, day, limit=bad); raise AssertionError("accepted a bad page size")
        except ValueError:
            pass


def test_car_minutes_endpoint_needs_login_and_rejects_bad_input(tmp_path):
    from fastapi.testclient import TestClient
    from solarbank_optimiser.web.app import WebConfig, create_app
    app = create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", status_cache_seconds=0), FakeClock(1_800_000_000))
    with TestClient(app) as client:
        query = "?start=2026-10-20&end=2026-10-20"
        assert client.get("/api/car-history/minutes" + query).status_code == 401
        client.auth = ("solarbank", "a-long-test-password")
        ok = client.get("/api/car-history/minutes" + query)
        assert ok.status_code == 200 and ok.json() == {"power_minutes": [], "next_after": None}
        assert client.get("/api/car-history/minutes" + query + "&limit=0").status_code == 422
        assert client.get("/api/car-history/minutes?start=2026-10-20&end=2025-01-01").status_code == 422
        assert "power_minutes" not in client.get("/api/car-history" + query).json()


def test_a_reading_taken_while_the_planner_loads_its_inputs_is_fresh_but_a_future_one_is_not():
    """On the Pi the planner noted the time, spent ~2 s loading, and then saw a live battery reading stamped later: it said 'no fresh SOC'."""
    from solarbank_optimiser.models import Observation, Quality
    from solarbank_optimiser.shadow import PlanningUnavailable, state_ready, LOAD_SKEW_S
    now = 1_800_000_000.0
    make = lambda at: Observation("monitor:solarbank:soc", "soc", at, .5, "fraction", Quality.OK, source_at=at - .4)
    assert make(now + 2.0).fresh(now, 5, skew=LOAD_SKEW_S) and not make(now + 2.0).fresh(now, 5)
    state_ready({"soc": make(now + 2.0).__dict__}, now)
    assert not make(now + 60).fresh(now, 5, skew=LOAD_SKEW_S)                    # a clock problem is still refused
    assert not make(now - 6).fresh(now, 5, skew=LOAD_SKEW_S)                      # and so is an old reading
    try:
        state_ready({"soc": make(now + 3600).__dict__}, now); raise AssertionError("accepted a reading from the future")
    except PlanningUnavailable:
        pass
