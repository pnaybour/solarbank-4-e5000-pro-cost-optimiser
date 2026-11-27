"""Learned household demand: scoring, slow promotion, the typed cap, the switch, and what the planner receives."""
from datetime import date, datetime, timedelta

import pytest

from conftest import seed_minutes
from solarbank_optimiser import demand_learning as dl
from solarbank_optimiser.energy import LONDON, day_bounds
from solarbank_optimiser.store import Store

TODAY = date(2026, 11, 10)
NOW = day_bounds(TODAY)[0] + 3600.0


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "d.sqlite3"); s.initialise()
    return s


def away(store, typed_w=350.0, since=None):
    since = since or day_bounds(TODAY - timedelta(days=20))[0]
    store.set_setting("demand", {"mode": "Away from home", "start": since, "end": None, "away_watts": typed_w}, since, 0)


def put_day(store, day, kw, car_kw=None):
    a, b = day_bounds(day)
    seed_minutes(store, "household_power", a, b, kw)
    seed_minutes(store, "ev_power", a, b, 0.0)                           # the car channel is read all day; idle is about 0 kW, never missing (issue 74)
    if car_kw is not None:
        seed_minutes(store, "ev_power", a + 3600, a + 7200, car_kw)


def test_the_typed_figure_is_used_until_a_learned_one_has_won_three_days(store):
    away(store, 350.0)
    for back in (4, 3):                                              # two Away days at 200 W: only one scored day
        put_day(store, TODAY - timedelta(days=back), 0.200)
    dl.learn(store, NOW)
    state = dl.load(store)
    assert state["away"]["scores"]["days_scored"] == 1 and not state["away"]["promoted"] and state["away"]["effective_kw"] == pytest.approx(0.350)
    selection, days, note = dl.planner_inputs(store, store.get_setting("demand")[1])
    assert selection["away_watts"] == 350.0 and "Away base load learned" not in (note or "")


def test_after_three_scored_days_the_figure_moves_a_tenth_of_the_typed_figure_a_day_and_never_above_it(store):
    away(store, 350.0)
    for back in range(6, 0, -1):                                     # six Away days at 200 W
        put_day(store, TODAY - timedelta(days=back), 0.200)
    dl.learn(store, NOW)
    state = dl.load(store)["away"]
    assert state["promoted"] and state["scores"]["best"] in ("mean", "p80")
    steps = len(state["days"]) - 1 and 6                              # six days seen: six slow steps at most
    assert state["effective_kw"] < 0.350 and state["effective_kw"] >= 0.200 - 1e-9
    assert state["effective_kw"] >= 0.350 - 6 * 0.1 * 0.350 - 1e-9       # never faster than 10 % of the typed figure per day
    selection, _days, note = dl.planner_inputs(store, store.get_setting("demand")[1])
    assert selection["away_watts"] == pytest.approx(state["effective_kw"] * 1000, abs=0.1) and "Away base load learned" in note


def test_a_house_that_uses_more_than_typed_never_raises_the_figure_above_the_typed_cap(store):
    away(store, 200.0)
    for back in range(6, 0, -1):
        put_day(store, TODAY - timedelta(days=back), 0.400)
    dl.learn(store, NOW)
    away_state = dl.load(store)["away"]
    assert away_state["effective_kw"] == pytest.approx(0.200)          # the typed figure is the cap and the start value
    assert dl.planner_inputs(store, store.get_setting("demand")[1])[0]["away_watts"] == 200.0


def test_the_switch_returns_everything_to_the_typed_figures_at_once(store):
    away(store, 350.0)
    for back in range(6, 0, -1):
        put_day(store, TODAY - timedelta(days=back), 0.200)
    dl.learn(store, NOW)
    assert dl.planner_inputs(store, store.get_setting("demand")[1])[0]["away_watts"] < 350.0
    dl.set_enabled(store, False, NOW + 1)
    selection, days, note = dl.planner_inputs(store, store.get_setting("demand")[1])
    assert selection["away_watts"] == 350.0 and days == 1 and note is None and dl.describe(store)["enabled"] is False


def test_the_car_is_taken_out_of_household_demand_minute_by_minute(store):
    day = TODAY - timedelta(days=2)
    a, b = day_bounds(day)
    seed_minutes(store, "household_power", a, b, 0.2)
    seed_minutes(store, "ev_power", a, b, 0.0)
    seed_minutes(store, "household_power", a + 3600, a + 7200, 7.2)       # an hour in which the house (with the car) draws 7.2 kW
    seed_minutes(store, "ev_power", a + 3600, a + 7200, 7.0)
    away(store)
    assert dl.day_mean_kw(store.minute_source(), day) == pytest.approx(0.2, abs=1e-6)


def test_a_day_with_a_mode_change_is_not_learned_from(store):
    away(store, 350.0, since=day_bounds(TODAY - timedelta(days=2))[0] + 12 * 3600)         # Away began at midday: that day is mixed
    for back in (3, 2):
        put_day(store, TODAY - timedelta(days=back), 0.2)
    dl.learn(store, NOW)
    state = dl.load(store)
    assert (TODAY - timedelta(days=2)).isoformat() not in state["away"]["days"] and (TODAY - timedelta(days=2)).isoformat() not in state["normal"]["days"]


def test_the_normal_profile_averages_the_last_good_days_from_the_start_and_scores_the_old_rule_only_as_a_diagnostic(store):
    store.set_setting("demand", {"mode": "Normal consumption", "start": 0.0, "end": None, "away_watts": None}, 1.0, 0)
    levels = [0.5, 0.9, 0.4, 1.0, 0.45, 0.95, 0.5]                    # a house whose days swing: yesterday is a poor guide, the mean is better
    for back, kw in zip(range(7, 0, -1), levels):
        put_day(store, TODAY - timedelta(days=back), kw)
    dl.learn(store, NOW)
    normal = dl.load(store)["normal"]
    assert normal["scores"]["days_scored"] == 6 and normal["promoted"] and normal["average_days"] == dl.PROFILE_DAYS
    assert dl.planner_inputs(store, store.get_setting("demand")[1])[1] == dl.PROFILE_DAYS
    steady = Store(store.path.parent / "s.sqlite3"); steady.initialise()
    steady.set_setting("demand", {"mode": "Normal consumption", "start": 0.0, "end": None, "away_watts": None}, 1.0, 0)
    for back in range(7, 0, -1):
        put_day(steady, TODAY - timedelta(days=back), 0.5)
    dl.learn(steady, NOW)
    assert not dl.load(steady)["normal"]["promoted"] and dl.planner_inputs(steady, steady.get_setting("demand")[1])[1] == dl.PROFILE_DAYS
    dl.set_enabled(steady, False, NOW)
    assert dl.planner_inputs(steady, steady.get_setting("demand")[1])[1] == 1 and dl.planner_level(steady) is None      # the switch returns the old rule


def test_the_level_starts_at_the_owners_seed_and_hands_over_to_the_measured_days():
    state = {"normal": {"days": {}}}
    assert dl.normal_level(state) == (pytest.approx(12.24), 0)
    state["normal"]["days"] = {f"2026-10-{d:02d}": 0.6 for d in range(1, 7)}            # six days at 0.6 kW = 14.4 kWh: half measured, half seed
    level, learned = dl.normal_level(state)
    assert learned == 6 and level == pytest.approx((6 * 14.4 + 6 * 12.24) / 12)
    state["normal"]["days"] = {f"2026-10-{d:02d}": 0.6 for d in range(1, 21)}           # twenty days: the seed is gone and only the last 14 count
    assert dl.normal_level(state) == (pytest.approx(14.4), 14)


def test_the_planner_profile_is_scaled_to_the_level_within_the_clamp(store):
    from solarbank_optimiser.forecasts import LEVEL_CLAMP, demand_horizon
    from solarbank_optimiser.minute_source import demand_rows
    store.set_setting("demand", {"mode": "Normal consumption", "start": 0.0, "end": None, "away_watts": None}, 1.0, 0)
    for back in range(5, 0, -1):
        put_day(store, TODAY - timedelta(days=back), 0.5)                                # 12 kWh a day
    rows = demand_rows(store, store.minute_source(), NOW - 8 * 86400, NOW)
    revision, selection = store.setting_at("demand", NOW)
    plain = demand_horizon(rows, selection, revision, NOW, NOW, NOW + 3600)
    scaled = demand_horizon(rows, selection, revision, NOW, NOW, NOW + 3600, 7, level_kwh=13.2)    # 10 % up
    wild = demand_horizon(rows, selection, revision, NOW, NOW, NOW + 3600, 7, level_kwh=40.0)     # held at +30 %
    assert plain["level_scale"] is None
    assert scaled["demand_kwh"] == pytest.approx(plain["demand_kwh"] * 1.1, rel=1e-3) and scaled["level_scale"] == pytest.approx(1.1, abs=1e-3)
    assert wild["level_scale"] == pytest.approx(1 + LEVEL_CLAMP)


def test_sunset_is_right_to_a_few_minutes_through_the_year():
    from solarbank_optimiser.daylight import sunset_utc
    for day, expected in ((date(2026, 10, 27), "16:42"), (date(2026, 12, 21), "15:53"), (date(2026, 6, 21), "21:21")):
        got = datetime.fromtimestamp(sunset_utc(day), LONDON)
        want = datetime.strptime(expected, "%H:%M")
        assert abs((got.hour * 60 + got.minute) - (want.hour * 60 + want.minute)) <= 4


def test_the_evening_diagnostic_records_the_energy_between_sunset_and_half_past_ten_and_is_not_used_by_the_planner(store):
    store.set_setting("demand", {"mode": "Normal consumption", "start": 0.0, "end": None, "away_watts": None}, 1.0, 0)
    for back in range(4, 0, -1):
        day = TODAY - timedelta(days=back)
        put_day(store, day, 0.5)
        a, _b = day_bounds(day)
        seed_minutes(store, "household_power", a + 18 * 3600, a + 22 * 3600, 1.5)     # an evening of 1.5 kW instead of 0.5
    dl.learn(store, NOW)
    evening = dl.load(store)["normal"]["evening"]
    assert len(evening) == 4
    one = next(iter(evening.values()))
    assert 0.3 < one["share"] < 0.5 and one["sunset"].startswith("16:")
    summary = dl.describe(store)["normal"]
    assert summary["evening"]["days"] == 4 and summary["evening"]["used_by_planner"] is False
    assert summary["seed_kwh"] == 12.24 and summary["level_kwh"] > 12


def test_with_no_complete_normal_day_the_planner_gets_the_seeded_default_flat_and_says_so(store):
    """9 November, owner: 'can we set a normal default'. After a return from Away there is no complete Normal day; the dashboard said 'Waiting'. Now the day is the seeded level, flat."""
    from solarbank_optimiser.forecasts import demand_horizon
    store.set_setting("demand", {"mode": "Normal consumption", "start": 0.0, "end": None, "away_watts": None}, 1.0, 0)
    revision, selection = store.setting_at("demand", NOW)
    with pytest.raises(ValueError):                                                          # the old behaviour is still what you get with the learning switch off
        demand_horizon([], selection, revision, NOW, NOW, NOW + 24 * 3600)
    level = dl.planner_level(store)
    assert level == pytest.approx(12.24)
    got = demand_horizon([], selection, revision, NOW, NOW, NOW + 24 * 3600, 7, level_kwh=level, default_kwh=level)
    assert got["default_used"] is True and got["demand_kwh"] == pytest.approx(12.24, rel=0.01)
    assert "default" in got["reference_day"] and "DEFAULT" in got["assumption"]
    assert all(abs(r["demand_kw"] - 12.24 / 24) < 1e-6 for r in got["intervals"])


def test_a_complete_normal_day_takes_over_from_the_default_at_once(store):
    from solarbank_optimiser.forecasts import demand_horizon
    from solarbank_optimiser.minute_source import demand_rows
    store.set_setting("demand", {"mode": "Normal consumption", "start": 0.0, "end": None, "away_watts": None}, 1.0, 0)
    put_day(store, TODAY - timedelta(days=1), 0.4)
    rows = demand_rows(store, store.minute_source(), NOW - 8 * 86400, NOW)
    revision, selection = store.setting_at("demand", NOW)
    got = demand_horizon(rows, selection, revision, NOW, NOW, NOW + 3600, 7, level_kwh=dl.planner_level(store), default_kwh=dl.planner_level(store))
    assert got["default_used"] is False and "default" not in got["reference_day"]


# ===== GitHub issue 74: the planner's demand shape leaves the car out ==============================================================================
def _normal_day_with_a_car_charge(store, day, hours=range(0, 3), car_kw=7.0, house_kw=0.3):
    """A Normal day: the house at `house_kw`, plus the car at `car_kw` in `hours` (the household reading includes the car, as the Solarbank's load figure does)."""
    a, b = day_bounds(day)
    seed_minutes(store, "household_power", a, b, house_kw)
    seed_minutes(store, "ev_power", a, b, 0.0)
    for h in hours:
        seed_minutes(store, "household_power", a + h * 3600, a + (h + 1) * 3600, house_kw + car_kw)
        seed_minutes(store, "ev_power", a + h * 3600, a + (h + 1) * 3600, car_kw)


def test_the_planner_rows_are_the_house_less_the_car(store):
    from solarbank_optimiser.minute_source import _ROW_CACHE, demand_rows
    _ROW_CACHE.clear()
    day = TODAY - timedelta(days=1)
    _normal_day_with_a_car_charge(store, day)
    a, b = day_bounds(day)
    rows = demand_rows(store, store.minute_source(), a, b)
    assert len(rows) == 1440
    assert all(r["value"] == pytest.approx(0.3) for r in rows)                  # 00:00 to 03:00 included: 7.3 kW less the car's 7.0


def test_a_day_with_a_long_car_charge_is_still_a_normal_reference_day(store):
    from solarbank_optimiser.forecasts import normal_day_candidates
    from solarbank_optimiser.minute_source import _ROW_CACHE, demand_rows
    _ROW_CACHE.clear()
    day = TODAY - timedelta(days=1)
    _normal_day_with_a_car_charge(store, day, hours=range(15, 24))                # a nine-hour charge, as on 8 November
    a, b = day_bounds(day)
    rows = demand_rows(store, store.minute_source(), a - 86400, b)
    found = list(normal_day_candidates(rows, b + 60))
    assert [d for d, _ in found] == [day] and all(o.value == pytest.approx(0.3) for o in found[0][1])
    assert dl.day_mean_kw(store.minute_source(), day) == pytest.approx(0.3, abs=1e-6)    # the level agrees with the shape: the same rule


def test_a_minute_without_a_car_reading_is_missing_demand_never_car_off(store):
    from solarbank_optimiser.minute_source import Minute, house_without_car
    house = {0: Minute(7.3, 60), 60: Minute(7.3, 60), 120: Minute(0.2, 60), 180: Minute(None, 10)}
    car = {0: Minute(7.0, 60), 60: Minute(None, 12), 120: Minute(0.3, 60)}         # 60: too few car seconds; 120: the car reads a little above the house
    out = house_without_car(house, car)
    assert set(out) == {0, 120}                                                 # 60 and 180 are missing, not 7.3 kW of house demand
    assert out[0] == Minute(pytest.approx(0.3), 60) and out[120].mean == 0.0        # never below zero; the house reading's coverage is kept
