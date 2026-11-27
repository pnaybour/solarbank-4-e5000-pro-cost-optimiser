"""One source of truth for readings (5 November): the last known reading comes from the live file, then the telemetry files; the database holds none."""
import time

from solarbank_optimiser.collector import PROVENANCE
from solarbank_optimiser.models import Observation, Quality
from solarbank_optimiser.store import Store
from solarbank_optimiser.telemetry import Telemetry


def record(store, now, seconds, metrics, value=0.5):
    """Write `seconds` one-second readings ending at `now` for each metric given as {metric: last_second_offset} into the telemetry files."""
    telemetry = Telemetry(store.telemetry_dir)
    units = {"soc": "fraction", "household_power": "kW"}
    for second in range(seconds, 0, -1):
        t = now - second
        rows = [Observation("fixture", m, t + 0.5, value, units[m], source_at=t + 0.4, provenance=PROVENANCE) for m, stop in metrics.items() if second > stop]
        telemetry.add(rows, t + 0.5)
    telemetry.flush(now)


def test_after_a_restart_each_metric_reports_its_own_last_valid_second_from_the_files(tmp_path):
    store = Store(tmp_path / "d.sqlite3", live_path=tmp_path / "live.json"); store.initialise()
    now = int(time.time())
    record(store, now, 40, {"soc": 0, "household_power": 20})      # the household reading stopped 20 s before the soc one
    latest = store.latest_observations(PROVENANCE)
    assert latest["soc"]["value"] == 0.5 and latest["household_power"]["value"] == 0.5
    assert latest["soc"]["source"] == "telemetry-files" and latest["soc"]["quality"] == "ok"
    assert 5 < latest["soc"]["retrieved_at"] - latest["household_power"]["retrieved_at"] <= 25       # not both stamped with the same time
    assert store.latest_observations("live") == {}                  # the files answer for the collector's provenance only
    assert "ac_power" not in latest and "voltage" not in latest      # nothing is made up for a metric the files do not hold


def test_a_newer_error_in_the_live_file_is_shown_and_the_last_good_reading_comes_from_the_files(tmp_path):
    store = Store(tmp_path / "d.sqlite3", live_path=tmp_path / "live.json"); store.initialise()
    now = int(time.time())
    record(store, now, 30, {"soc": 0})
    store.publish_live([Observation("collector", "soc", now, None, "fraction", Quality.ERROR, provenance=PROVENANCE)])
    newest = store.latest_observations(PROVENANCE)["soc"]
    assert newest["quality"] != "ok" and newest["value"] is None                    # the newest word is the error
    good = store.latest_observations(PROVENANCE, "ok")["soc"]
    assert good["quality"] == "ok" and good["value"] == 0.5 and good["retrieved_at"] < now          # the last good reading is older and named as such


def test_the_database_holds_no_readings_after_the_collector_runs(tmp_path):
    from contextlib import closing
    store = Store(tmp_path / "d.sqlite3", live_path=tmp_path / "live.json"); store.initialise()
    store.record_snapshots([(b"{}", [Observation("fixture", "soc", 100.0, 0.5, "fraction")], 100.0, {"status": "ok"})], keep_observations=False)
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM collector_snapshots").fetchone()[0] == 1


def test_a_recent_legacy_database_row_does_not_hide_a_newer_reading_in_the_files_when_the_live_file_is_gone(tmp_path):
    """GitHub 59."""
    from contextlib import closing
    from dataclasses import asdict
    from solarbank_optimiser.models import encoded
    store = Store(tmp_path / "d.sqlite3", live_path=tmp_path / "live.json"); store.initialise()
    now = int(time.time())
    old = Observation("earlier-release", "soc", now - 7200, 0.40, "fraction", provenance=PROVENANCE, source_at=now - 7201)
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO observations VALUES(?,?,?,?,?)", (old.id, old.source, old.metric, old.sample_at, encoded(asdict(old))))
    record(store, now, 30, {"soc": 0}, value=0.55)
    latest = store.latest_observations(PROVENANCE)["soc"]
    assert latest["value"] == 0.55 and latest["retrieved_at"] > now - 60


def test_end_to_end_the_collector_writes_the_files_and_after_a_restart_the_last_known_readings_come_from_them(tmp_path):
    """GitHub 60: collector -> telemetry files -> last-known reading, with the live file deleted (the Pi has restarted)."""
    import json
    from solarbank_optimiser.collector import Collector, CollectorConfig
    from solarbank_optimiser.models import FakeClock
    from test_collector import payload
    t0 = int(time.time()) - 120
    store = Store(tmp_path / "c.sqlite3", live_path=tmp_path / "live.json"); store.initialise()
    clock = FakeClock(t0 + 0.5)
    store.telemetry_clock = clock.utc
    worker = Collector(store, CollectorConfig(telemetry_flush_seconds=0), clock, lambda _config: json.dumps(payload(t0)).encode())
    for i in range(20):
        worker.fetch = lambda _c, t=t0 + i: json.dumps(payload(t)).encode()
        worker.once()
        clock.advance(1)
    worker.telemetry.flush(clock.utc())
    assert store.latest_observations(PROVENANCE)["soc"]["value"] == 0.5                  # while the live file is there
    (tmp_path / "live.json").unlink()                                                        # the restart
    latest = store.latest_observations(PROVENANCE)
    assert latest["soc"]["value"] == 0.5 and latest["soc"]["source"] == "telemetry-files"
    assert latest["household_power"]["value"] == 0.8 and t0 <= latest["soc"]["retrieved_at"] <= t0 + 21
    with __import__("contextlib").closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0           # and the database never held a reading
