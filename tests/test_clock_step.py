"""WP4 (#10): a collector that restarts, or whose clock steps back, keeps running and marks the charging session partial."""
import json

from solarbank_optimiser.collector import Collector, CollectorConfig
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store

T0 = 1_800_000_000


def charging(at):
    return json.dumps({"ok": True, "sample": {"time": at, "main": 500, "sub": 100, "garage_current_a": 1.3},
                       "shelly": {"ok": True, "sample": {"time": at, "ch1": 5., "ch2": 7000.}},
                       "solarbank": {"ok": True, "sample": {"time": at, "soc": 50, "pv": 400, "load": 800, "grid": 400, "battery": 0,
                                                            "charge_total_kwh": 12.3, "discharge_total_kwh": 9.8}}}).encode()


def run(store, start, seconds):
    clock = FakeClock(start + 0.5)
    worker = Collector(store, CollectorConfig(confirmation_seconds=3), clock, lambda _c: b"")
    for i in range(seconds):
        worker.fetch = lambda _c, t=start + i: charging(t)
        worker.once()
        clock.advance(1)
    worker.flush()
    return worker


def test_a_collector_that_restarts_30_minutes_behind_keeps_running_and_the_session_is_partial(tmp_path):
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    run(store, T0, 90)                                                      # runs 90 s, flushes
    worker = run(store, T0 - 1800, 30)                                      # restarts with the clock 30 minutes behind: must not raise
    sessions = worker.tracker.sessions() if hasattr(worker.tracker, "sessions") else None
    from contextlib import closing
    with closing(store.connect()) as db:
        payloads = [json.loads(r[0]) for r in db.execute("SELECT payload FROM charger_sessions")]
    assert payloads and any(p["partial"] for p in payloads)


def test_a_clock_that_steps_back_while_running_does_not_stop_the_collector(tmp_path):
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    clock = FakeClock(T0 + 0.5)
    worker = Collector(store, CollectorConfig(confirmation_seconds=3), clock, lambda _c: b"")
    for i in range(20):
        worker.fetch = lambda _c, t=T0 + i: charging(t)
        worker.once(); clock.advance(1)
    clock.wall = T0 - 3600 + 0.5                                            # NTP corrected the clock an hour back
    for i in range(20):
        worker.fetch = lambda _c, t=T0 - 3600 + i: charging(t)
        health = worker.once(); clock.advance(1)
    assert health["status"] in ("ok", "partial")


def test_every_unit_waits_for_the_clock_to_be_synchronised():
    from pathlib import Path
    for unit in Path(__file__).resolve().parents[1].joinpath("deployment").glob("solarbank-optimiser-*.service"):
        text = unit.read_text()
        if "After=network-online.target" in text:
            assert "time-sync.target" in text.split("[Service]")[0], unit.name
