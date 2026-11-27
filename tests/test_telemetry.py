"""WP23a: the fixed-size telemetry store."""
import os

import pytest

from types import SimpleNamespace

from solarbank_optimiser.telemetry import ARCHIVES, MAX_AGE_S, METRICS, Telemetry, files_for

T0 = 1_800_000_000          # a whole minute (1_800_000_000 % 60 == 0)


def obs(metric, t, value, quality="ok"):
    """Telemetry reads only metric, sample_at, value and quality, so a plain record stands in for an Observation (the collector test below uses real ones)."""
    return SimpleNamespace(metric=metric, sample_at=float(t), value=value, quality=quality)


@pytest.fixture
def tel(tmp_path):
    return Telemetry(tmp_path / "telemetry")


def sizes(t):
    return {p.name: p.stat().st_size for p in t.root.glob('*.wsp')}


def test_every_file_is_created_at_its_final_size_and_holds_the_documented_archives(tel):
    names = sorted(sizes(tel))
    assert len(names) == sum(len(files_for(m)) for m in METRICS) == 21
    assert all(5_700_000 < size < 5_800_000 for size in sizes(tel).values())                    # 5.73 MB each
    assert 110e6 < sum(sizes(tel).values()) < 130e6                                              # roughly 120 MB in total, fixed
    assert ARCHIVES == ((1, 172800), (60, 129600), (1800, 175200))


def test_files_are_private_and_never_half_created(tel):
    assert all(os.stat(p).st_mode & 0o777 == 0o600 for p in tel.root.glob('*.wsp'))
    assert not list(tel.root.glob("*.tmp"))


def test_the_size_does_not_change_after_a_long_run_of_writes(tel):
    before = sizes(tel)
    now = T0 + 3 * 86400
    for day in range(0, 3):
        base = T0 + day * 86400 + 2 * 86400 - 3 * 86400 + 3 * 86400            # inside the one-second archive relative to `now`
    for chunk in range(0, 20):
        tel.add([obs("household_power", now - 3000 + chunk * 100 + i, 0.4) for i in range(100)], now)
        tel.flush(now)
    assert sizes(tel) == before


def test_gaps_stay_empty_at_every_resolution(tel):
    now = T0 + 600
    tel.add([obs("household_power", T0 + i, 1.0) for i in range(0, 60)] + [obs("household_power", T0 + 300 + i, 1.0) for i in range(0, 60)], now)
    tel.flush(now)
    (start, end, step), values = tel.fetch("household_power.wsp", T0, T0 + 360, now)
    at = lambda second: values[(second - start) // step]                      # a fetched series starts at `start`, one value per `step`
    assert step == 1 and at(T0 + 10) == 1.0 and at(T0 + 100) is None and at(T0 + 299) is None and at(T0 + 310) == 1.0
    (start, end, step), minutes = tel.fetch("household_power.wsp", T0 - 3 * 86400, T0 + 360, now)  # a coarser archive
    assert None in minutes


def test_a_minute_gives_the_same_mean_whatever_the_flush_point(tel, tmp_path):
    """A second at 2.0 kW then 59 seconds at 2.98 kW, written in two batches cut at different points, is the same minute: the mean of all 60 seconds."""
    series = [(T0, 2.0)] + [(T0 + i, 2.98) for i in range(1, 60)]
    expected = sum(v for _, v in series) / 60
    now = T0 + 120
    for cut in (1, 7, 30, 59):
        t = Telemetry(tmp_path / f"c{cut}")
        t.add([obs("household_power", s, v) for s, v in series[:cut]], now); t.flush(now)
        t.add([obs("household_power", s, v) for s, v in series[cut:]], now); t.flush(now)
        (start, _, step), values = t.fetch("household_power.wsp", T0 - 7 * 86400, T0 + 120, now)      # the one-minute archive
        minute = [v for v in values if v is not None]
        assert step == 60 and minute and minute[0] == pytest.approx(expected, abs=1e-6), cut


def test_a_minute_of_30_valid_seconds_has_the_same_mean_but_a_count_of_30_and_is_not_complete(tel):
    now = T0 + 300
    tel.add([obs("household_power", T0 + i, 1.0) for i in range(0, 30)], now)                   # half a minute
    tel.add([obs("household_power", T0 + 60 + i, 1.0) for i in range(0, 60)], now)              # a full minute
    tel.flush(now)
    far = T0 - 7 * 86400
    (_, _, step), counts = tel.fetch("household_power.count.wsp", far, T0 + 200, now)
    assert step == 60 and [c for c in counts if c is not None][:2] == [30.0, 60.0]
    (_, _, _), means = tel.fetch("household_power.wsp", far, T0 + 200, now)
    assert [m for m in means if m is not None][:2] == [1.0, 1.0]                                 # the same mean: only the count tells them apart


def test_invalid_or_missing_readings_write_nothing_to_either_file(tel):
    now = T0 + 120
    tel.add([obs("household_power", T0, 1.0, quality="error"), obs("household_power", T0 + 1, float("nan")), obs("ev_power", T0 + 2, float("inf")),
             obs("not_a_metric", T0 + 3, 1.0)], now)
    assert tel.pending() == 0 and tel.flush(now) == 0


def test_a_point_older_than_the_one_second_archive_less_the_margin_is_refused_and_counted(tel):
    now = T0 + 5 * 86400
    tel.add([obs("household_power", now - 3 * 86400, 1.0), obs("household_power", now - MAX_AGE_S - 1, 1.0), obs("household_power", now - MAX_AGE_S + 5, 1.0)], now)
    assert tel.dropped_late == 2 and tel.pending() == 3                                          # the 3-day-old and the just-too-old one are refused; the last has a value, a count and a peak
    tel.flush(now)
    (_, _, _), values = tel.fetch("household_power.wsp", now - 3 * 86400, now - 3 * 86400 + 600, now)
    assert all(v is None for v in values)


def test_the_count_file_holds_one_per_valid_second_and_soc_also_has_a_max_file(tel):
    now = T0 + 200
    tel.add([obs("soc", T0 + i, 0.5 + i / 1000) for i in range(0, 60)], now); tel.flush(now)
    far = T0 - 7 * 86400
    assert [c for c in tel.fetch("soc.count.wsp", far, T0 + 100, now)[1] if c is not None][0] == 60.0
    assert [c for c in tel.fetch("soc.max.wsp", far, T0 + 100, now)[1] if c is not None][0] == pytest.approx(0.559)


def test_a_failed_write_is_counted_retried_and_never_raises(tel, monkeypatch):
    import solarbank_optimiser.telemetry as module
    now = T0 + 100
    tel.add([obs("household_power", T0, 1.0)], now)
    real = module.whisper.update_many
    monkeypatch.setattr(module.whisper, "update_many", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert tel.flush(now) == 0 and tel.write_errors >= 1 and tel.pending() == 3 and "OSError" in tel.last_error
    monkeypatch.setattr(module.whisper, "update_many", real)
    assert tel.flush(now) == 3 and tel.pending() == 0


def test_the_buffer_is_bounded(tel, monkeypatch):
    import solarbank_optimiser.telemetry as module
    monkeypatch.setattr(module, "MAX_BUFFER", 100)
    now = T0 + 100000
    for chunk in range(0, 40):
        tel.add([obs("household_power", now - 5000 + chunk * 100 + i, 1.0) for i in range(100)], now)
    assert tel.pending() <= 400 and tel.dropped_overflow > 0


# ---- the collector writes it alongside SQLite --------------------------------------------------------------------------------------------------
def collector_payload(at):
    return {"ok": True, "sample": {"time": at, "main": 500, "sub": 100, "garage_current_a": 1.3},
            "shelly": {"ok": True, "sample": {"time": at, "ch1": 5., "ch2": 7.}},
            "solarbank": {"ok": True, "sample": {"time": at, "soc": 50, "pv": 400, "load": 800, "grid": 400, "battery": 0,
                                                 "charge_total_kwh": 12.3, "discharge_total_kwh": 9.8}}}


def test_the_collector_writes_real_observations_to_the_store_and_reports_its_status(tmp_path):
    import json
    from solarbank_optimiser.collector import Collector, CollectorConfig
    from solarbank_optimiser.models import FakeClock
    from solarbank_optimiser.store import Store
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    clock = FakeClock(T0 + 0.5)
    worker = Collector(store, CollectorConfig(telemetry_dir=str(tmp_path / "tel"), telemetry_flush_seconds=0), clock,
                       lambda _config: json.dumps(collector_payload(T0)).encode())
    health = None
    for i in range(0, 5):
        clock.advance(1) if i else None
        worker.fetch = lambda _c, t=T0 + i: json.dumps(collector_payload(t)).encode()
        health = worker.once()
    assert health["telemetry"]["files"] == 21 and health["telemetry"]["write_errors"] == 0
    worker.telemetry.flush(clock.utc())
    far = T0 - 3600
    values = [v for v in worker.telemetry.fetch("household_power.wsp", far, T0 + 60, clock.utc())[1] if v is not None]
    assert values and all(v == pytest.approx(0.8) for v in values)
    counts = [v for v in worker.telemetry.fetch("household_power.count.wsp", far, T0 + 60, clock.utc())[1] if v is not None]
    assert counts and sum(counts) >= 5 and worker.telemetry.status()["dropped_late"] == 0


def test_an_unusable_telemetry_directory_stops_the_collector_instead_of_silently_recording_nothing(tmp_path):
    """Minute history now lives only in these files (WP23b): the collector must not run without them."""
    import json
    from solarbank_optimiser.collector import Collector, CollectorConfig
    from solarbank_optimiser.models import FakeClock
    from solarbank_optimiser.store import Store
    blocker = tmp_path / "file"; blocker.write_text("x")                      # a file where the directory should be
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    with pytest.raises(OSError):
        Collector(store, CollectorConfig(telemetry_dir=str(blocker / "tel")), FakeClock(T0 + 0.5), lambda _c: json.dumps(collector_payload(T0)).encode())


def test_the_default_telemetry_folder_is_the_one_the_readers_use(tmp_path):
    import json
    from solarbank_optimiser.collector import Collector, CollectorConfig
    from solarbank_optimiser.models import FakeClock
    from solarbank_optimiser.store import Store
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    worker = Collector(store, CollectorConfig(), FakeClock(T0 + 0.5), lambda _c: json.dumps(collector_payload(T0)).encode())
    assert worker.telemetry is not None and worker.telemetry.root == store.telemetry_dir


# ---- peak files and event snapshots ------------------------------------------------------------------------------------------------------------
def test_the_peak_inside_an_interval_survives_in_the_max_file_while_the_average_hides_it(tel):
    now = T0 + 300
    tel.add([obs("garage_current", T0 + i, 10.0 if i != 30 else 44.0) for i in range(0, 60)], now); tel.flush(now)
    far = T0 - 7 * 86400
    mean = [v for v in tel.fetch("garage_current.wsp", far, T0 + 100, now)[1] if v is not None][0]
    peak = [v for v in tel.fetch("garage_current.max.wsp", far, T0 + 100, now)[1] if v is not None][0]
    assert mean == pytest.approx(10.0 + 34.0 / 60) and peak == 44.0


def snapshots(tel):
    return sorted((tel.root / "snapshots").glob("*.jsonl"))


def test_a_garage_peak_saves_a_one_second_snapshot_with_the_seconds_before_and_after(tel):
    import json
    for i in range(0, 300):
        value = 45.0 if 150 <= i < 160 else 10.0
        tel.add([obs("garage_current", T0 + i, value), obs("household_power", T0 + i, 2.0)], T0 + i)
    tel.flush(T0 + 300)
    (path,) = snapshots(tel)
    assert "garage_over_40a" in path.name
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    times = [r["t"] for r in rows]
    assert min(times) <= T0 + 150 - 100 and max(times) >= T0 + 150 + 100          # about two minutes either side
    assert any(r.get("garage_current") == 45.0 for r in rows) and all("household_power" in r for r in rows)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_one_snapshot_per_event_not_one_per_second_over_the_limit_and_it_rearms_below_35_a(tel):
    for i in range(0, 400):
        tel.add([obs("garage_current", T0 + i, 45.0 if i < 100 else 10.0 if i < 200 else 45.0 if i < 210 else 10.0)], T0 + i)
    tel.flush(T0 + 400)
    assert len([p for p in snapshots(tel) if "garage_over_40a" in p.name]) == 2


def test_the_car_starting_and_stopping_each_save_a_snapshot(tel):
    for i in range(0, 500):
        tel.add([obs("ev_power", T0 + i, 7.0 if 100 <= i < 200 else 0.003)], T0 + i)
    tel.flush(T0 + 500)
    kinds = sorted(p.name.split("-", 1)[1] for p in snapshots(tel))
    assert kinds == ["car_start.jsonl", "car_stop.jsonl"]


def test_a_caller_can_note_an_event_such_as_a_mode_change(tel):
    for i in range(0, 300):
        tel.add([obs("household_power", T0 + i, 1.0)], T0 + i)
        if i == 100:
            tel.note_event("mode_change", T0 + i)
    tel.flush(T0 + 300)
    assert any("mode_change" in p.name for p in snapshots(tel))


def test_only_the_newest_snapshots_are_kept(tel, monkeypatch):
    import solarbank_optimiser.telemetry as module
    monkeypatch.setattr(module, "SNAPSHOT_KEEP", 3)
    for k in range(0, 6):
        tel.note_event("mode_change", T0 + k * 1000)
        for i in range(0, 130):
            tel.add([obs("household_power", T0 + k * 1000 + i, 1.0)], T0 + k * 1000 + i)
        tel.flush(T0 + k * 1000 + 130)
    assert len(snapshots(tel)) == 3 and "mode_change" in snapshots(tel)[-1].name


def test_the_collector_saves_a_snapshot_when_the_household_mode_changes(tmp_path):
    import json
    from solarbank_optimiser.collector import Collector, CollectorConfig
    from solarbank_optimiser.models import FakeClock
    from solarbank_optimiser.store import Store
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    clock = FakeClock(T0 + 0.5)
    worker = Collector(store, CollectorConfig(telemetry_dir=str(tmp_path / "tel"), telemetry_flush_seconds=0), clock, lambda _c: b"")
    modes = {"now": "Normal consumption"}
    real = worker.store.setting_at
    worker.store.setting_at = lambda name, at: (1, {"mode": modes["now"], "start": 0.0, "away_watts": 400.0}) if name == "demand" else real(name, at)
    for i in range(0, 200):                                                    # past the 120 s after the change, so the snapshot completes
        if i == 60:
            modes["now"] = "Away from home"
        worker.fetch = lambda _c, t=T0 + i: json.dumps(collector_payload(t)).encode()
        clock.advance(1) if i else None
        worker.once()
    worker.telemetry.flush(clock.utc())
    assert any("mode_change" in p.name for p in (tmp_path / "tel" / "snapshots").glob("*.jsonl"))


def test_a_device_that_refreshes_every_two_seconds_still_covers_the_whole_minute(tel):
    """The old recorder counted the time between two adjacent valid readings (up to 5 s) as covered; so does the telemetry store."""
    now = T0 + 300
    tel.add([obs("household_power", T0 + i, 1.0) for i in range(0, 60, 2)], now)           # a reading every other second
    tel.add([obs("household_power", T0 + 60 + i, 1.0) for i in range(0, 20, 8)], now)       # gaps of 8 s are NOT bridged
    tel.flush(now)
    (_s, _e, _step), counts = tel.fetch("household_power.count.wsp", T0 - 1, T0 + 120, now=now)
    assert sum(v for v in counts[:60] if v is not None) >= 58
    assert sum(v for v in counts[60:120] if v is not None) == 3
