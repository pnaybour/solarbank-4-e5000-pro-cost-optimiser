"""A5 (lost solar) and A7 (savings against no solar and no battery): exact arithmetic, honest gaps, estimates labelled as such."""
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser import daily_record as dr
from solarbank_optimiser.energy import LONDON, day_bounds
from solarbank_optimiser.models import FakeClock, Observation
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app

DAY = date(2026, 10, 27)
STATIC = Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static"
TARIFF = {"day_pence": 30.0, "cheap_pence": 7.0, "valid_from": 0.0, "confirmed": True, "start_minute": 1410, "end_minute": 330}
UNITS = {"soc": "fraction", "battery_power": "kW", "grid_power": "kW", "pv_power": "kW", "household_power": "kW", "ev_power": "kW"}


def put(store, name, value, now=1.0):
    store.set_setting(name, value, now, store.get_setting(name)[0])


def add_day(store, day, house=0.5, grid=0.2, pv=0.0, battery=0.3, soc=0.5, skip=(), ok=60, car=0.0, car_skip=()):
    """One log row per minute for the five metrics and the car (`car_skip`: minutes with no car reading). Each value may be a number or a function of the minute
    index (0 = midnight)."""
    start, end = day_bounds(day)
    value = lambda x, i: x(i) if callable(x) else x
    rows = []
    for i, t in enumerate(range(int(start), int(end), 60)):
        if i in set(skip):
            continue
        for metric, x in (("soc", soc), ("battery_power", battery), ("grid_power", grid), ("pv_power", pv), ("household_power", house), ("ev_power", car)):
            if metric == "ev_power" and i in set(car_skip):
                continue
            v = value(x, i)
            rows.append(Observation("fixture", metric, t + 60, v, UNITS[metric], source_at=t + 59, window_samples=60, window_ok=ok, window_mean=v, window_max=v))
    store.add_observations(rows)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "d.sqlite3"); result.initialise()
    put(result, "fallback_tariff", TARIFF)
    return result


def score(store, day=DAY, forecast=None):
    start, end = day_bounds(day)
    price_at, _ = dr.tariff_pricer(store)
    return dr.compute_day(day, dr.minute_rows(store, start, end), price_at, forecast, now=end + 600)


# ===== A7: the arithmetic =========================================================================================================================
def test_savings_are_the_difference_between_the_no_solar_no_battery_bill_and_the_actual_import_bill(store):
    add_day(store, DAY)                                           # 0.5 kW used all day, 0.2 kW imported all day
    record = score(store)
    cheap_hours, day_hours = 6, 18                                # 00:00 to 05:30 and 23:30 to 24:00 on 27 October
    per_kw = cheap_hours * 0.07 + day_hours * 0.30
    assert record["complete"] and record["minutes_covered"] == 1440 and record["estimated"] is True
    assert record["consumption_kwh"] == pytest.approx(12.0) and record["import_kwh"] == pytest.approx(4.8)
    assert record["baseline_cost_gbp"] == pytest.approx(0.5 * per_kw, abs=1e-3)
    assert record["actual_cost_gbp"] == pytest.approx(0.2 * per_kw, abs=1e-3)
    assert record["saving_gbp"] == pytest.approx(0.3 * per_kw, abs=1e-3)
    assert record["cheap_rate_import_kwh"] == pytest.approx(0.2 * cheap_hours)


def test_solar_and_battery_energy_are_totalled_with_their_signs(store):
    add_day(store, DAY, house=0.4, grid=0.1, pv=lambda i: 1.0 if 600 <= i < 780 else 0.0, battery=lambda i: -0.5 if 600 <= i < 720 else (0.3 if 1000 <= i < 1120 else 0.0))
    record = score(store)
    assert record["solar_kwh"] == pytest.approx(3.0) and record["battery_charged_kwh"] == pytest.approx(1.0) and record["battery_discharged_kwh"] == pytest.approx(0.6)


def test_a_day_with_export_does_not_count_export_as_import(store):
    add_day(store, DAY, grid=lambda i: -0.3 if i < 60 else 0.1)
    record = score(store)
    assert record["export_kwh"] == pytest.approx(0.3) and record["import_kwh"] == pytest.approx(0.1 * 23)


# ===== honest gaps ===================================================================================================================================
@pytest.mark.parametrize("skipped,complete", [(10, True), (11, False)])
def test_ten_missing_minutes_still_count_but_eleven_do_not(store, skipped, complete):
    add_day(store, DAY, skip=range(100, 100 + skipped, 2) if skipped <= 5 else [100 + 7 * k for k in range(skipped)])
    record = score(store)
    assert record["complete"] is complete and record["minutes_covered"] == 1440 - skipped


def test_a_long_gap_is_not_complete_even_with_few_minutes_missing(store):
    add_day(store, DAY, skip=range(300, 306))
    assert score(store)["complete"] is False                         # a run of six minutes


def test_minutes_with_too_few_good_samples_are_treated_as_missing(store):
    add_day(store, DAY, ok=29)
    record = score(store)
    assert record["minutes_covered"] == 0 and record["complete"] is False and record["consumption_kwh"] == 0


def test_without_a_confirmed_tariff_no_pound_figure_is_invented(store):
    put(store, "fallback_tariff", {**TARIFF, "confirmed": False}, 2.0)
    add_day(store, DAY)
    record = score(store)
    assert record["saving_gbp"] is None and record["baseline_cost_gbp"] is None and record["consumption_kwh"] == pytest.approx(12.0)


# ===== A5: lost solar ================================================================================================================================
def sunny_day(store, forecast_kw=2.0):
    add_day(store, DAY, house=0.4, grid=0.0,
            pv=lambda i: 0.4 if 600 <= i < 900 else 0.0,                  # 10:00 to 15:00: solar tracking the load
            battery=lambda i: 0.5 if 1020 <= i < 1140 else 0.0,           # 17:00 to 19:00: the battery supplies the evening
            soc=lambda i: 1.0 if 600 <= i < 900 else 0.5)
    start, _ = day_bounds(DAY)
    return {"issued_at": start - 3600, "lead": "evening_before", "hourly": [{"start": start + h * 3600, "end": start + (h + 1) * 3600, "pv_kw": forecast_kw if 8 <= h < 16 else 0.0} for h in range(24)]}


def test_lost_solar_is_a_labelled_candidate_worked_out_from_the_saved_forecast(store):
    forecast = sunny_day(store)
    record = score(store, forecast=forecast)
    solar = record["solar"]
    start, _ = day_bounds(DAY)
    assert solar["first_full_at"] == start + 600 * 60 and solar["full_minutes"] == 300 and solar["full_sun_minutes"] == 300
    assert solar["throttled_minutes"] == 300                                   # solar equal to the load, battery idle, nothing imported
    assert solar["pv_kwh_while_full"] == pytest.approx(2.0)
    assert solar["possible_lost_kwh"] == pytest.approx((2.0 - 0.4) * 5) and solar["evidenced_lost_kwh"] == pytest.approx(8.0)
    assert solar["storable_kwh"] == pytest.approx(1.0)                         # limited by the 1.0 kWh the battery actually supplied that evening
    assert solar["lower_target_saving_gbp"] == pytest.approx(1.0 / 0.86 * 0.07, abs=1e-3)
    assert "Candidate" in solar["label"] and solar["forecast_saved"] is True


def test_without_a_saved_forecast_the_measured_facts_stay_and_the_potential_is_unknown(store):
    sunny_day(store)
    solar = score(store)["solar"]
    assert solar["full_sun_minutes"] == 300 and solar["throttled_minutes"] == 300 and solar["first_full_at"] is not None
    assert solar["possible_lost_kwh"] is None and solar["evidenced_lost_kwh"] is None and solar["lower_target_saving_gbp"] is None
    assert solar["forecast_saved"] is False


def test_solar_that_is_not_held_back_is_not_counted_as_evidence(store):
    add_day(store, DAY, house=0.4, grid=-0.3, pv=0.9, battery=0.0, soc=1.0)    # full, but solar far above the load and exporting
    start, _ = day_bounds(DAY)
    forecast = {"issued_at": start - 1, "hourly": [{"start": start, "end": start + 86400, "pv_kw": 2.0}]}
    solar = score(store, forecast=forecast)["solar"]
    assert solar["throttled_minutes"] == 0 and solar["evidenced_lost_kwh"] == 0 and solar["possible_lost_kwh"] > 0


def test_a_day_the_battery_never_fills_has_no_lost_solar(store):
    add_day(store, DAY, soc=0.8, pv=1.0)
    solar = score(store)["solar"]
    assert solar["first_full_at"] is None and solar["full_minutes"] == 0 and solar["throttled_minutes"] == 0


# ===== storing and scheduling ===========================================================================================================================
def london(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LONDON).timestamp()


def test_only_finished_days_are_scored_once_and_a_new_version_rescores(store):
    add_day(store, DAY); add_day(store, DAY + timedelta(days=1))
    morning = london(DAY + timedelta(days=1), 0, 2)                           # two minutes after the day ended: not yet settled
    assert dr.record_finished_days(store, morning, lookback=1) == []
    settled = london(DAY + timedelta(days=1), 0, 10)
    assert dr.record_finished_days(store, settled, lookback=1) == [DAY]
    assert store.get_setting(dr.RECORD_PREFIX + DAY.isoformat())[1]["saving_gbp"] is not None
    assert dr.record_finished_days(store, settled, lookback=1) == []                       # idempotent
    later = london(DAY + timedelta(days=2), 0, 10)
    assert dr.record_finished_days(store, later, lookback=1) == [DAY + timedelta(days=1)]
    old = store.get_setting(dr.RECORD_PREFIX + DAY.isoformat())[1]
    put(store, dr.RECORD_PREFIX + DAY.isoformat(), {**old, "version": 0}, 5.0)
    assert DAY in dr.record_finished_days(store, later, lookback=2)                         # an older version is recomputed


def weather(day, hours=96):
    start = day_bounds(day)[0]
    return {"acquired_at": start, "intervals": [{"start": start + h * 3600, "end": start + (h + 1) * 3600, "pv_kw": 1.0} for h in range(hours)]}


def test_tomorrows_forecast_is_saved_once_from_the_evening_and_never_overwritten(store):
    put(store, "cache_weather", weather(DAY))
    afternoon, evening = london(DAY, 15), london(DAY, 18, 5)
    assert dr.snapshot_forecasts(store, afternoon) == [DAY]                       # today's, as a fallback
    assert store.get_setting(dr.FORECAST_PREFIX + DAY.isoformat())[1]["lead"] == "same_day"
    assert dr.snapshot_forecasts(store, evening) == [DAY + timedelta(days=1)]
    saved = store.get_setting(dr.FORECAST_PREFIX + (DAY + timedelta(days=1)).isoformat())[1]
    assert saved["lead"] == "evening_before" and 20 <= len(saved["hourly"]) <= 26
    put(store, "cache_weather", {**weather(DAY), "acquired_at": 999.0}, 9.0)
    assert dr.snapshot_forecasts(store, london(DAY, 21)) == []                    # already saved: a later, better forecast does not rewrite history
    assert store.get_setting(dr.FORECAST_PREFIX + (DAY + timedelta(days=1)).isoformat())[1]["issued_at"] == saved["issued_at"]


def test_a_forecast_that_covers_too_little_of_the_day_is_not_saved(store):
    put(store, "cache_weather", weather(DAY, hours=10))
    assert dr.snapshot_forecasts(store, london(DAY, 9)) == []


# ===== the summary ===========================================================================================================================================
def seed_days(store, days):
    for d in days:
        add_day(store, d)
        dr.record_finished_days(store, london(d + timedelta(days=1), 0, 10), lookback=1)


def test_the_seven_day_total_adds_only_complete_days_and_counting_starts_at_go_live(store):
    days = [DAY + timedelta(days=k) for k in range(3)]
    add_day(store, days[0]); add_day(store, days[1], skip=[100 + 7 * k for k in range(12)]); add_day(store, days[2])     # the middle day is partial
    for d in days:
        dr.record_finished_days(store, london(d + timedelta(days=1), 0, 10), lookback=1)
    today = days[2] + timedelta(days=1)
    result = dr.summary(store, today)
    per_day = 0.3 * (6 * 0.07 + 18 * 0.30)
    assert result["last_7_days"]["days_included"] == 2 and result["last_7_days"]["saving_gbp"] == pytest.approx(round(2 * per_day, 2), abs=0.02)
    assert [d["complete"] for d in result["days"]] == [True, False, True] and result["cumulative_counted"] is None
    assert result["since_records_began"]["days"] == 2 and "Anker app alone" in result["since_records_began"]["note"]
    dr.set_savings_start(store, days[2], 100.0)
    counted = dr.summary(store, today)
    assert counted["counting_from"] == days[2].isoformat() and counted["cumulative_counted"]["days"] == 1
    dr.set_savings_start(store, None, 101.0)
    assert dr.summary(store, today)["cumulative_counted"] is None


# ===== the pages and commands ================================================================================================================================
def test_the_status_carries_the_savings_and_the_pages_have_their_places(store, tmp_path):
    seed_days(store, [DAY])
    clock = FakeClock(london(DAY + timedelta(days=1), 12))
    app = create_app(WebConfig(store.path, "a-long-test-password", trusted_network=True, status_cache_seconds=0), clock, store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        savings = client.get("/api/status").json()["savings"]
        home, history = client.get("/").text, client.get("/history").text
    assert savings["estimated"] is True and savings["last_7_days"]["days_included"] == 1 and savings["days"][0]["date"] == DAY.isoformat()
    for element in ("home-saving", "home-week", "home-live"):
        assert f'id="{element}"' in home                     # the home page's money card is the one place the savings are shown
    assert 'id="daily-table"' in history and "Estimates, not a bill" in history


def test_the_command_line_scores_days_and_sets_the_go_live_day(tmp_path, capsys):
    from solarbank_optimiser import cli
    db = str(tmp_path / "c.sqlite3")
    cli.main(["--database", db, "daily-record", "--days", "2"]); assert "none" in capsys.readouterr().out
    cli.main(["--database", db, "savings-start", "2026-11-01"])
    assert Store(Path(db)).get_setting("savings_start")[1] == {"date": "2026-11-01"}
    cli.main(["--database", db, "savings-start", "--clear"])
    assert Store(Path(db)).get_setting("savings_start")[1] == {"date": None}
    with pytest.raises(SystemExit):
        cli.main(["--database", db, "savings-start"])


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
# ===== A6: forecast accuracy =================================================================================================================================
def demand_forecast(day, kw=0.4, slots=48, lead="evening_before"):
    start = day_bounds(day)[0]
    return {"issued_at": start - 3600, "lead": lead, "half_hours": [{"t": start + 1800 * k, "kw": kw} for k in range(slots)]}


def review_of(store, solar=None, demand=None, day=DAY):
    start, end = day_bounds(day)
    price_at, _ = dr.tariff_pricer(store)
    return dr.compute_day(day, dr.minute_rows(store, start, end), price_at, solar, now=end + 600, demand_forecast=demand)["forecast_review"]


def test_the_review_compares_forecast_and_measured_totals_for_solar_and_use(store):
    add_day(store, DAY, house=0.5, pv=0.5)                                 # 12 kWh used, 12 kWh solar... measured 0.5 kW all day each
    solar = {"issued_at": 1.0, "lead": "evening_before", "hourly": weather(DAY, 24)["intervals"]}          # 1 kW forecast all day: 24 kWh
    review = review_of(store, solar, demand_forecast(DAY, kw=0.4))
    assert review["solar"]["forecast_kwh"] == pytest.approx(24.0) and review["solar"]["measured_kwh"] == pytest.approx(12.0)
    assert review["solar"]["error_kwh"] == pytest.approx(12.0) and review["solar"]["error_pct"] == pytest.approx(100.0)
    assert review["demand"]["forecast_kwh"] == pytest.approx(9.6) and review["demand"]["measured_kwh"] == pytest.approx(12.0)
    assert review["demand"]["error_kwh"] == pytest.approx(-2.4) and review["demand"]["error_pct"] == pytest.approx(-20.0)


def test_no_review_for_a_missing_short_or_incomplete_forecast_or_day(store):
    add_day(store, DAY, house=0.5, pv=0.5)
    assert review_of(store) == {"solar": None, "demand": None}
    short = {"issued_at": 1.0, "hourly": weather(DAY, 10)["intervals"]}
    assert review_of(store, short, demand_forecast(DAY, slots=20)) == {"solar": None, "demand": None}
    other = DAY + timedelta(days=1)
    add_day(store, other, house=0.5, pv=0.5, skip=[100 + 7 * k for k in range(12)])                         # a partial day is never reviewed
    assert review_of(store, {"issued_at": 1.0, "hourly": weather(other, 24)["intervals"]}, demand_forecast(other), day=other) == {"solar": None, "demand": None}


def test_demand_forecast_is_saved_once_from_the_evening_and_never_overwritten(store):
    tomorrow = DAY + timedelta(days=1)
    start = day_bounds(DAY)[0]
    series = [{"time": start + 1800 * k, "demand_kw": 0.5, "pv_kw": 0.0} for k in range(96)]
    put(store, "forecast_summary", {"series": series})
    assert dr.snapshot_demand_forecast(store, london(DAY, 15)) == [DAY]
    assert dr.snapshot_demand_forecast(store, london(DAY, 18, 5)) == [tomorrow]
    saved = store.get_setting(dr.DEMAND_FORECAST_PREFIX + tomorrow.isoformat())[1]
    assert saved["lead"] == "evening_before" and len(saved["half_hours"]) == 48
    put(store, "forecast_summary", {"series": [{**r, "demand_kw": 9.9} for r in series]}, 9.0)
    assert dr.snapshot_demand_forecast(store, london(DAY, 21)) == []
    assert store.get_setting(dr.DEMAND_FORECAST_PREFIX + tomorrow.isoformat())[1]["half_hours"][0]["kw"] == 0.5
    put(store, "forecast_summary", {"series": series[:10]}, 10.0)
    assert dr.snapshot_demand_forecast(store, london(DAY + timedelta(days=5), 9)) == []                  # too little of that day covered


def test_the_summary_averages_the_accuracy_over_the_days_with_a_review(store):
    days = [DAY + timedelta(days=k) for k in range(3)]
    for d in days:
        add_day(store, d, house=0.5, pv=0.5)
    put(store, dr.FORECAST_PREFIX + days[0].isoformat(), {"issued_at": 1.0, "lead": "evening_before", "hourly": weather(days[0], 24)["intervals"]})            # +100 %
    put(store, dr.DEMAND_FORECAST_PREFIX + days[0].isoformat(), demand_forecast(days[0], kw=0.4))                                                              # -20 %
    put(store, dr.DEMAND_FORECAST_PREFIX + days[1].isoformat(), demand_forecast(days[1], kw=0.6))                                                              # +20 %
    for d in days:
        dr.record_finished_days(store, london(d + timedelta(days=1), 0, 10), lookback=1)
    summary = dr.summary(store, days[-1] + timedelta(days=1))
    assert summary["forecast_accuracy"]["solar"]["days"] == 1 and summary["forecast_accuracy"]["solar"]["mean_error_pct"] == pytest.approx(100.0)
    demand = summary["forecast_accuracy"]["demand"]
    assert demand["days"] == 2 and demand["mean_error_pct"] == pytest.approx(0.0, abs=0.1) and demand["mean_abs_error_pct"] == pytest.approx(20.0, abs=0.1)
    first = [d for d in summary["days"] if d["date"] == days[0].isoformat()][0]
    assert first["forecast_review"]["solar"]["forecast_kwh"] == pytest.approx(24.0)


def test_history_page_has_the_accuracy_section(store):
    app = create_app(WebConfig(store.path, "a-long-test-password", trusted_network=True), FakeClock(1_793_000_000.0), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        html = client.get("/history").text
    assert 'id="accuracy-solar"' in html and 'id="accuracy-demand"' in html and "forecast vs measured" in html


# ---- the second review: #21 late data repairs an incomplete record, #25 lifetime totals use every saved day --------------------------------------------------
def test_an_incomplete_record_is_recalculated_when_more_minutes_arrive_and_a_complete_one_is_left_alone(store):
    day = DAY
    start, end = day_bounds(day)
    now = end + 3600
    add_day(store, day, skip=range(1000, 1006))                             # six minutes missing
    first = dr.record_finished_days(store, now, lookback=10)
    assert day in first
    record = store.get_setting(dr.RECORD_PREFIX + day.isoformat())[1]
    assert record["minutes_covered"] == 1434 and not record["complete"]
    assert dr.record_finished_days(store, now + 60, lookback=10) == []          # nothing new: not rewritten
    add_day(store, day)                                                       # the six late minutes (and the rest, identical) arrive
    assert day in dr.record_finished_days(store, now + 120, lookback=10)
    record = store.get_setting(dr.RECORD_PREFIX + day.isoformat())[1]
    assert record["minutes_covered"] == 1440 and record["complete"]
    assert dr.record_finished_days(store, now + 180, lookback=10) == []          # complete is final


def test_lifetime_totals_include_days_older_than_the_14_shown(store):
    today = DAY + timedelta(days=40)
    for back in (1, 3, 30):                                                   # one recent, one in the window, one far outside it
        day = today - timedelta(days=back)
        put(store, dr.RECORD_PREFIX + day.isoformat(), {"date": day.isoformat(), "complete": True, "saving_gbp": 1.0, "minutes_covered": 1440,
                                                         "baseline_cost_gbp": 2.0, "actual_cost_gbp": 1.0, "consumption_kwh": 1, "import_kwh": 1, "solar_kwh": 1,
                                                         "solar": {"first_full_at": None, "full_sun_minutes": 0, "throttled_minutes": 0, "evidenced_lost_kwh": 0,
                                                                   "possible_lost_kwh": 0, "lower_target_saving_gbp": 0, "forecast_saved": False}})
    dr.set_savings_start(store, today - timedelta(days=35), 1.0)
    result = dr.summary(store, today)
    assert result["since_records_began"]["days"] == 3 and result["since_records_began"]["saving_gbp"] == 3.0
    assert result["cumulative_counted"]["days"] == 3                           # go-live 35 days ago: all three days count


def test_a_day_is_priced_from_the_tariff_in_force_at_its_end_not_todays(store):
    """GitHub 44 (TC6): a tariff saved after the day does not re-price it."""
    put(store, "fallback_tariff", TARIFF, london(DAY, 12))                       # in force on the day
    add_day(store, DAY)
    put(store, "fallback_tariff", {**TARIFF, "day_pence": 99.0, "cheap_pence": 50.0}, london(DAY + timedelta(days=1), 6))   # a new tariff, saved the next morning
    now = london(DAY + timedelta(days=1), 12)
    assert dr.record_finished_days(store, now, lookback=1) == [DAY]
    record = store.get_setting(dr.RECORD_PREFIX + DAY.isoformat())[1]
    assert "day 30p" in record["price_source"] and "cheap 7p" in record["price_source"] and "revision" in record["price_source"]


# ===== the car, recorded apart (record version 4; GitHub issue 74) =================================================================================
def test_the_car_is_recorded_apart_from_the_house_by_day_and_by_half_hour(store):
    car = lambda i: 7.0 if i < 120 else 0.0                                     # 7 kW from 00:00 to 02:00: 14 kWh, all in the cheap window
    add_day(store, DAY, house=lambda i: 0.5 + car(i), car=car)                  # the house reading includes the car, as the Solarbank's load figure does
    record = score(store)
    assert record["consumption_kwh"] == pytest.approx(12.0 + 14.0)              # the whole house, car included, as before
    assert record["car_kwh"] == pytest.approx(14.0) and record["car_cheap_rate_kwh"] == pytest.approx(14.0) and record["car_minutes_missing"] == 0
    assert record["house_without_car_kwh"] == pytest.approx(12.0) and record["house_without_car_minutes"] == 1440
    start, end = day_bounds(DAY)
    price_at, _ = dr.tariff_pricer(store)
    slots = dr.half_hours(DAY, dr.minute_rows(store, start, end), price_at)
    assert slots[0]["car_kwh"] == pytest.approx(3.5) and slots[0]["car_minutes_covered"] == 30 and slots[0]["house_without_car_kwh"] == pytest.approx(0.25)
    assert slots[4]["car_kwh"] == pytest.approx(0.0) and slots[0]["consumption_kwh"] == pytest.approx(3.75)


def test_a_car_charge_at_the_peak_rate_is_not_counted_as_cheap(store):
    car = lambda i: 7.0 if 18 * 60 <= i < 19 * 60 else 0.0                     # a bump charge, 18:00 to 19:00
    add_day(store, DAY, house=lambda i: 0.5 + car(i), car=car)
    record = score(store)
    assert record["car_kwh"] == pytest.approx(7.0) and record["car_cheap_rate_kwh"] == pytest.approx(0.0)


def test_missing_car_minutes_are_counted_never_taken_as_zero_and_do_not_spoil_the_house_figures(store):
    add_day(store, DAY, car_skip=range(600, 660))                               # an hour with no car reading
    record = score(store)
    assert record["complete"] and record["consumption_kwh"] == pytest.approx(12.0)               # every other figure still counts the hour
    assert record["car_minutes_missing"] == 60 and record["house_without_car_minutes"] == 1380
    assert record["house_without_car_kwh"] == pytest.approx(0.5 * 1380 / 60)


def test_the_demand_review_compares_the_forecast_with_the_house_without_the_car(store):
    car = lambda i: 7.0 if i < 120 else 0.0
    add_day(store, DAY, house=lambda i: 0.5 + car(i), car=car)
    review = review_of(store, None, demand_forecast(DAY, kw=0.5))
    assert review["demand"]["measured_kwh"] == pytest.approx(12.0) and review["demand"]["error_kwh"] == pytest.approx(0.0)


def test_no_demand_review_when_the_car_reading_is_too_thin_to_take_the_car_out(store):
    add_day(store, DAY, car_skip=range(600, 611))                               # eleven minutes without a car reading: more than the ten allowed
    assert review_of(store, None, demand_forecast(DAY, kw=0.5))["demand"] is None
    add_day(store, DAY + timedelta(days=1), car_skip=range(600, 610))           # ten: still reviewed
    assert review_of(store, None, demand_forecast(DAY + timedelta(days=1), kw=0.5), day=DAY + timedelta(days=1))["demand"] is not None
