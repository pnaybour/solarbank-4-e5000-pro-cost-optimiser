"""WP23b: minute_source, the backfill and its agreement check (TC2, TC3)."""
from types import SimpleNamespace

import pytest

from solarbank_optimiser.energy import day_bounds
from solarbank_optimiser.minute_source import MIN_COVERED, MinuteSource, demand_rows
from solarbank_optimiser.store import Store
from solarbank_optimiser.telemetry import Telemetry
from solarbank_optimiser.telemetry_backfill import agreement, backfill, sqlite_windows

from test_daily_record import DAY, add_day, put

START, END = day_bounds(DAY)
OLD = END + 6 * 86400          # six days after the day: only the one-minute archive holds it
YOUNG = END + 3600             # an hour after: inside the one-second archive


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "d.sqlite3"); s.initialise()
    return s


def obs(metric, t, value):
    return SimpleNamespace(metric=metric, sample_at=float(t), value=value, quality="ok")


def varied(i):
    return 0.2 + (i % 7) * 0.1


def test_backfill_of_an_old_day_reads_back_the_same_and_the_check_passes(store, tmp_path):
    add_day(store, DAY, house=varied, soc=lambda i: 0.3 + (i % 50) / 100, ok=57)
    root = tmp_path / "t"
    report = backfill(store, root, OLD)
    assert report["household_power"]["written"] == 1440
    source = MinuteSource(root, clock=lambda: OLD)
    want = sqlite_windows(store, "household_power", START, END)
    got = source.minutes("household_power", START, END)
    assert len(got) == 1440
    assert all(got[m].covered == 57 and abs(got[m].mean - want[m][0]) < 1e-9 for m in want)
    assert agreement(store, root, OLD)["ok"]


def test_a_second_backfill_changes_nothing(store, tmp_path):
    add_day(store, DAY, house=varied)
    root = tmp_path / "t"
    backfill(store, root, OLD)
    again = backfill(store, root, OLD)
    assert again["household_power"]["written"] == 0 and again["household_power"]["skipped_existing"] == 1440


def test_young_minutes_are_rebuilt_from_seconds_and_a_thin_minute_has_no_mean(store, tmp_path):
    add_day(store, DAY, house=0.4, skip=range(600, 1440))
    add_day(store, DAY, house=0.4, skip=range(0, 600), ok=20)             # a stretch of thin minutes (20 valid seconds)
    root = tmp_path / "t"
    backfill(store, root, YOUNG)
    got = MinuteSource(root, clock=lambda: YOUNG).minutes("household_power", START, END)
    full = got[START + 120]
    assert full.covered == 60 and abs(full.mean - 0.4) < 1e-9
    thin = got[START + 60 * 700]
    assert thin.covered == 20 and thin.mean is None
    assert MIN_COVERED == 30


def test_history_older_than_two_days_gives_the_same_minutes_as_fresh_data(store, tmp_path):
    """TC3: the reader gives the same eligibility and values whether the day is still in the one-second archive or not."""
    add_day(store, DAY, house=varied, ok=45)
    fresh, aged = tmp_path / "fresh", tmp_path / "aged"
    backfill(store, fresh, YOUNG)
    backfill(store, aged, OLD)
    a = MinuteSource(fresh, clock=lambda: YOUNG).usable("household_power", START, END)
    b = MinuteSource(aged, clock=lambda: OLD).usable("household_power", START, END)
    assert a.keys() == b.keys() and len(a) == 1440
    assert all(abs(a[m] - b[m]) < 1e-9 for m in a)


def test_the_live_feed_is_never_overwritten_by_the_backfill(store, tmp_path):
    add_day(store, DAY, house=0.9)
    root = tmp_path / "t"
    live = Telemetry(root)
    live.add([obs("household_power", START + 60 + i, 0.1) for i in range(60)], START + 130)
    live.flush(START + 100)
    backfill(store, root, YOUNG)
    got = MinuteSource(root, clock=lambda: YOUNG).minutes("household_power", START, START + 180)
    assert abs(got[START + 60].mean - 0.1) < 1e-9 and got[START + 60].covered == 60          # the live minute, not the SQLite 0.9
    assert abs(got[START + 120].mean - 0.9) < 1e-9


def test_agreement_catches_a_wrong_or_missing_minute(store, tmp_path):
    add_day(store, DAY, house=0.5)
    root = tmp_path / "t"
    backfill(store, root, OLD)
    import whisper
    whisper.update_many(str(root / "household_power.wsp"), [(START + 600, 3.0)], now=OLD)
    result = agreement(store, root, OLD)
    assert not result["ok"] and result["metrics"]["household_power"]["mean_differences"] == 1


def test_demand_rows_carry_the_mode_from_the_settings_history(store, tmp_path):
    add_day(store, DAY, house=0.5)
    root = tmp_path / "t"
    backfill(store, root, OLD)
    away_from = START + 3600
    put(store, "demand", {"mode": "Away from home", "start": away_from + 30, "end": None, "away_watts": 300.0}, now=START + 10)
    rows = demand_rows(store, MinuteSource(root, clock=lambda: OLD), START, START + 2 * 3600)
    by = {r["interval_start"]: r for r in rows}
    assert by[START + 120]["demand_mode"] == "Normal consumption" and by[START + 120]["coverage_seconds"] == 60
    assert by[away_from + 120]["demand_mode"] == "Away from home"
    assert away_from not in by                                         # the selection started inside this minute: broken, never complete


def test_the_command_backfills_then_checks_and_fails_when_the_files_disagree(store, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("time.time", lambda: OLD)
    from solarbank_optimiser.cli import main
    add_day(store, DAY, house=0.5)
    db, root = str(tmp_path / "d.sqlite3"), str(tmp_path / "t")
    main(["--database", db, "telemetry", "backfill", "--telemetry-dir", root])
    assert '"ok": true' in capsys.readouterr().out
    import whisper
    whisper.update_many(root + "/household_power.wsp", [(START + 600, 3.0)], now=int(OLD))
    with pytest.raises(SystemExit) as stop:
        main(["--database", db, "telemetry", "check", "--telemetry-dir", root])
    assert stop.value.code == 5


# ---- the review of 3 November: archive boundary, uncounted metrics, coverage in the agreement check ------------------------------------------------------
def window_row(metric, minute, mean, ok, peak=None):
    from solarbank_optimiser.models import UNITS, Observation
    return Observation("fixture", metric, minute + 60, mean, UNITS[metric], source_at=minute + 59, window_samples=60, window_ok=ok, window_mean=mean,
                       window_max=mean if peak is None else peak)


def test_a_minute_just_inside_the_one_second_archive_reads_as_seconds_not_as_one_point(tmp_path):
    root = tmp_path / "t"
    live = Telemetry(root)
    now = START + 3 * 86400
    minute = (int(now) - 47 * 3600 - 58 * 60) // 60 * 60
    live.add([obs("household_power", minute + i, float(i + 1)) for i in range(60)], minute + 60)
    live.flush(minute + 61)
    got = MinuteSource(root, clock=lambda: now).minutes("household_power", minute, minute + 60)[minute]
    assert got.covered == 60 and abs(got.mean - 30.5) < 1e-9


def test_a_backfilled_minute_keeps_its_mean_when_the_one_second_archive_ages_out(store, tmp_path):
    now = START + 3 * 86400
    minute = (int(now) - 47 * 3600 - 58 * 60) // 60 * 60
    store.add_observations([window_row("household_power", minute, 7.0, 60)])
    root = tmp_path / "t"
    backfill(store, root, now)
    got = MinuteSource(root, clock=lambda: now).minutes("household_power", minute, minute + 60)[minute]
    assert got.covered == 60 and abs(got.mean - 7.0) < 1e-9
    later = now + 600                                             # the one-second archive has now let go of that minute
    got = MinuteSource(root, clock=lambda: later).minutes("household_power", minute, minute + 60)[minute]
    assert got.covered == 60 and abs(got.mean - 7.0) < 1e-9


def test_a_row_older_than_the_minute_archive_does_not_stop_the_recent_rows_being_copied(store, tmp_path):
    now = START + 100 * 86400
    old, recent = int(now) - 91 * 86400, int(now) - 86400
    store.add_observations([window_row("household_power", old // 60 * 60, 1.0, 60), window_row("household_power", recent // 60 * 60, 2.0, 60)])
    root = tmp_path / "t"
    report = backfill(store, root, now)
    assert report["household_power"]["skipped_old"] == 1 and report["household_power"]["written"] == 1
    assert agreement(store, root, now)["ok"]


def test_the_backfill_never_overwrites_live_values_of_a_metric_without_a_count_file(store, tmp_path):
    now = START + 3 * 86400
    minute = (int(now) - 3600) // 60 * 60
    root = tmp_path / "t"
    live = Telemetry(root)
    live.add([obs("ac_power", minute + i, 9.0) for i in range(60)], minute + 61)
    live.flush(minute + 62)
    store.add_observations([window_row("ac_power", minute, 1.0, 60)])
    first = backfill(store, root, now)
    again = backfill(store, root, now)
    assert first["ac_power"]["written"] == 0 and again["ac_power"]["written"] == 0
    import whisper
    (_a, _b, _c), values = whisper.fetch(str(root / "ac_power.wsp"), minute - 1, minute + 59, now=int(now))
    assert set(v for v in values if v is not None) == {9.0}


def test_a_coverage_change_that_removes_a_complete_minute_fails_the_agreement(store, tmp_path):
    now = START + 6 * 86400
    minute = START + 3600
    store.add_observations([window_row("household_power", minute, 1.0, 60)])
    root = tmp_path / "t"
    backfill(store, root, now)
    assert agreement(store, root, now)["ok"]
    import whisper
    whisper.update_many(str(root / "household_power.count.wsp"), [(minute, 30.0)], now=int(now))          # the mean is the same, the coverage is not
    result = agreement(store, root, now)
    assert not result["ok"] and result["metrics"]["household_power"]["eligibility_changes"] == 1


def test_a_partly_recorded_live_minute_is_not_overwritten_by_the_backfill(store, tmp_path):
    now = START + 4 * 86400 + 17.3
    minute = (int(now) - 1800) // 60 * 60
    root = tmp_path / "t"
    live = Telemetry(root)
    live.add([obs("ac_power", minute + i, 9.0) for i in range(10)], minute + 11)               # only 10 real seconds: no consolidated average yet
    live.flush(minute + 12)
    store.add_observations([window_row("ac_power", START + 3600, 2.0, 60), window_row("ac_power", minute, 1.0, 60)])   # old history makes the read span the minute archive
    report = backfill(store, root, now)
    assert report["ac_power"]["written"] == 1 and report["ac_power"]["skipped_existing"] == 1
    import whisper
    (_a, _b, _c), values = whisper.fetch(str(root / "ac_power.wsp"), minute - 1, minute + 59, now=int(now))
    assert set(v for v in values if v is not None) == {9.0}


def test_a_request_reaching_past_the_retained_history_keeps_its_recent_part(store, tmp_path):
    from datetime import timedelta
    now = START + 100 * 86400 + 17.3                                                         # not minute aligned
    recent = (int(now) - 86400) // 60 * 60
    store.add_observations([window_row("household_power", (int(now) - 91 * 86400) // 60 * 60, 1.0, 60), window_row("household_power", recent, 2.0, 60)])
    root = tmp_path / "t"
    backfill(store, root, now)
    assert agreement(store, root, now)["ok"]
    source = MinuteSource(root, clock=lambda: now)
    got = source.minutes("household_power", now - 100 * 86400, now)                          # a 100-day request
    assert recent in got and abs(got[recent].mean - 2.0) < 1e-9


def test_a_watt_of_difference_in_a_live_recorded_minute_is_not_a_migration_failure_but_a_real_one_is(store, tmp_path):
    now = START + 6 * 86400
    minute = START + 3600
    store.add_observations([window_row("household_power", minute, 1.0, 60)])
    root = tmp_path / "t"
    backfill(store, root, now)
    import whisper
    whisper.update_many(str(root / "household_power.wsp"), [(minute, 1.0008)], now=int(now))      # 0.8 W, as the 5 s hold produces
    assert agreement(store, root, now)["ok"]
    whisper.update_many(str(root / "household_power.wsp"), [(minute, 1.05)], now=int(now))        # 50 W: a real difference
    assert not agreement(store, root, now)["ok"]


def test_the_nightly_backup_command_does_nothing_and_succeeds_until_a_remote_is_set_up(tmp_path, capsys):
    from solarbank_optimiser.cli import main
    # an explicit, empty configuration: the test must not read the machine's own /etc file (it exists on the Pi once Drive is set up)
    main(["--database", str(tmp_path / "d.sqlite3"), "offsite-backup", "--config", str(tmp_path / "no-such-offsite.conf")])
    assert "not set up" in capsys.readouterr().out


def test_the_directly_built_planner_rows_are_identical_to_the_validated_observation_rows():
    import json
    from dataclasses import asdict
    from solarbank_optimiser.minute_source import PROVENANCE, _row
    from solarbank_optimiser.models import Observation, encoded
    for mode in ("Normal consumption", "Away from home"):
        o = Observation("monitor-minute:household_power", "household_power", 1_800_000_060.0, 0.4321, "kW", coverage_seconds=60.0, interval_start=1_800_000_000.0,
                        demand_mode=mode, demand_revision=3, provenance=PROVENANCE)
        assert _row(1_800_000_000, 0.4321, mode, 3) == json.loads(encoded(asdict(o)))
    assert _row(1_800_000_000, -0.2, "Normal consumption", 0)["value"] == 0.0


def test_the_planner_rows_are_computed_once_a_minute_and_again_when_the_demand_setting_or_the_data_changes(store, tmp_path):
    from solarbank_optimiser import minute_source as ms
    ms._ROW_CACHE.clear()
    add_day(store, DAY, house=0.5)
    root = tmp_path / "t"
    backfill(store, root, OLD)
    source = MinuteSource(root, clock=lambda: OLD)
    calls = []
    original = source.minutes
    source.minutes = lambda *a, **k: calls.append(1) or original(*a, **k)
    first = demand_rows(store, source, START, START + 7200)
    again = demand_rows(store, source, START, START + 7200 + 5)             # a few seconds later, the same minute: no recomputation
    assert first is again and len(calls) == 2                               # one computation reads two series: the house and the car (issue 74)
    put(store, "demand", {"mode": "Away from home", "start": START + 30, "end": None, "away_watts": 300.0}, now=START + 10)
    changed = demand_rows(store, source, START, START + 7200)
    assert len(calls) == 4 and changed is not first
    import whisper, time as _t
    _t.sleep(0.01)
    whisper.update_many(str(root / "household_power.wsp"), [(START + 120, 0.9)], now=int(OLD))             # new data arrives
    demand_rows(store, source, START, START + 7200)
    assert len(calls) == 6


def test_a_second_filled_by_the_hold_rule_and_a_minute_cut_by_a_restart_are_explained_not_failures(store, tmp_path):
    import whisper
    now = START + 6 * 86400
    minute = START + 3600
    store.add_observations([window_row("household_power", minute, 1.0, 59)])
    root = tmp_path / "t"
    backfill(store, root, now)
    whisper.update_many(str(root / "household_power.count.wsp"), [(minute, 60.0)], now=int(now))           # the live feed filled the one-second gap
    result = agreement(store, root, now)
    entry = result["metrics"]["household_power"]
    assert result["ok"] and entry["coverage_differences"] == 1 and entry["explained_coverage_differences"] == 1 and not entry.get("eligibility_changes")

    young = (int(now) - 1800) // 60 * 60
    store.add_observations([window_row("household_power", young, 1.0, 60)])
    backfill(store, root, now)
    whisper.update_many(str(root / "household_power.count.wsp"), [(young + i, 0.0) for i in range(52, 60)], now=int(now))      # a restart inside the minute: only 52 seconds were recorded live
    result = agreement(store, root, now)
    entry = result["metrics"]["household_power"]
    assert result["ok"] and entry["explained_coverage_differences"] == 2 and not entry.get("eligibility_changes")


def test_a_few_watts_from_an_edge_second_is_explained_but_a_larger_gap_and_any_battery_level_gap_still_fail(store, tmp_path):
    import whisper
    now = START + 6 * 86400
    minute = START + 3600
    store.add_observations([window_row("household_power", minute, 1.0, 60), window_row("soc", minute, 0.5, 60)])
    root = tmp_path / "t"
    backfill(store, root, now)
    whisper.update_many(str(root / "household_power.wsp"), [(minute, 1.003)], now=int(now))             # 3 W
    result = agreement(store, root, now)
    entry = result["metrics"]["household_power"]
    assert result["ok"] and entry["mean_differences"] == 0 and entry["explained_mean_differences"] == 1
    whisper.update_many(str(root / "household_power.wsp"), [(minute, 1.05)], now=int(now))              # 50 W
    assert not agreement(store, root, now)["ok"]
    whisper.update_many(str(root / "household_power.wsp"), [(minute, 1.0)], now=int(now))
    whisper.update_many(str(root / "soc.wsp"), [(minute, 0.503)], now=int(now))                         # the battery level is held to the strict tolerance
    assert not agreement(store, root, now)["ok"]
