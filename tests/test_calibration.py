"""A11: the battery must reach 100 % about weekly (BMS calibration); the planner forces a full charge in the next cheap window when it is due."""
import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser import calibration as cal
from solarbank_optimiser.models import FakeClock, Observation
from solarbank_optimiser.planning import BatteryModel
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app
from test_shadow import NOW, ShadowConfig, ShadowWorker, assemble, job, normal_history, put, site, snapshot, solve, validate_result  # noqa: F401  (site is a fixture)

DAY = 86400.0


@pytest.fixture(autouse=True)
def fresh_cache():
    cal._cache.clear()
    yield
    cal._cache.clear()


def soc_rows(store, *points):
    """points: (age in days, level). One reading each, stamped that long before NOW."""
    store.add_observations([Observation("fixture", "soc", NOW - age * DAY, level, "fraction", source_at=NOW - age * DAY - 1, window_samples=60, window_ok=60,
                                        window_mean=level, window_max=level) for age, level in points])


# ===== the setting =======================================================================================================================================
@pytest.mark.parametrize("value,expected", [(7, 7), (3, 3), (14, 14), (7.0, 7), (2, None), (15, None), (7.5, None), (True, None), ("7", None), (None, None), (float("nan"), None)])
def test_the_number_of_days_is_a_whole_number_between_3_and_14(value, expected):
    assert cal.valid_days(value) == expected


def test_the_default_is_seven_days():
    assert cal.stored_days(None) == 7 and cal.stored_days({"days": 9}) == 9 and cal.stored_days({"days": 99}) == 7


# ===== when was it last full =============================================================================================================================
def test_the_last_full_time_comes_from_the_one_minute_readings(site):
    store, _ = site
    soc_rows(store, (6.5, 0.5), (4.0, 1.0), (3.0, 0.7), (0.5, 0.8))
    newest, first = cal.last_full(store, NOW)
    assert newest == pytest.approx(NOW - 4 * DAY) and first == pytest.approx(NOW - 6.5 * DAY)


def test_a_reading_just_under_full_does_not_count_and_the_window_maximum_does(site):
    store, _ = site
    soc_rows(store, (5.0, 0.985))
    assert cal.last_full(store, NOW)[0] is None
    cal._cache.clear()
    store.add_observations([Observation("fixture", "soc", NOW - 2 * DAY, 0.9, "fraction", source_at=NOW - 2 * DAY - 1, window_samples=60, window_ok=60, window_mean=0.95, window_max=1.0)])
    assert cal.last_full(store, NOW)[0] == pytest.approx(NOW - 2 * DAY)


def test_older_days_come_from_the_daily_record(site):
    store, _ = site
    from datetime import datetime
    from solarbank_optimiser.energy import LONDON, day_bounds
    soc_rows(store, (12.0, 0.5))
    when = NOW - 9 * DAY
    day = datetime.fromtimestamp(when, LONDON).date()
    put(store, "daily_record:" + day.isoformat(), {"solar": {"first_full_at": when}})
    assert cal.last_full(store, NOW)[0] == pytest.approx(when)


def test_the_lookup_is_remembered_for_ten_minutes(site):
    store, _ = site
    soc_rows(store, (3.0, 1.0))
    first = cal.last_full(store, NOW)
    soc_rows(store, (0.1, 1.0))
    assert cal.last_full(store, NOW + 60) == first
    assert cal.last_full(store, NOW + 700)[0] == pytest.approx(NOW - 0.1 * DAY)


# ===== the state ==========================================================================================================================================
def test_ok_due_and_unknown(site):
    store, _ = site
    soc_rows(store, (7.0, 0.5), (2.0, 1.0))
    assert cal.state(store, NOW, 7)["state"] == "ok" and cal.state(store, NOW, 7)["days_since"] == pytest.approx(2.0)
    cal._cache.clear()
    assert cal.state(store, NOW + 4.2 * DAY, 7)["state"] == "due"                       # 6.2 days since the last full charge
    cal._cache.clear()
    assert cal.state(store, NOW + 4.2 * DAY, 9)["state"] == "ok"                        # the owner allowed nine days


def test_never_seen_full_is_unknown_until_we_have_watched_long_enough(site):
    store, _ = site
    soc_rows(store, (2.0, 0.5))
    assert cal.state(store, NOW, 7)["state"] == "unknown"
    cal._cache.clear()
    soc_rows(store, (7.0, 0.5))
    assert cal.state(store, NOW, 7)["state"] == "due"                                    # watched for a week and never full


# ===== forcing the next window ========================================================================================================================
def windows(hours=6.0, night="2026-10-25"):
    return {"kind": "full_overnight", "windows": [{"night_date": night, "target": 1.0, "intervals": [{"start": NOW, "end": NOW + hours * 3600}]}]}


def test_a_long_enough_window_is_forced_to_a_full_charge():
    model = BatteryModel(reserve=0.10)
    quiet = {"kind": "forecast_based", "overnight_target": None, "windows": []}
    policy, forced, why = cal.force_first_night(quiet, windows(), 0.3, model)
    assert forced and why is None and policy["kind"] == "full_overnight" and policy["windows"][0]["target"] == 1.0 and policy["status"] == "calibration_charge"


def test_a_window_that_is_too_short_is_not_forced():
    model = BatteryModel(reserve=0.10)
    policy, forced, why = cal.force_first_night({"kind": "forecast_based", "windows": []}, windows(hours=1.0), 0.1, model)
    assert not forced and "too short" in why and policy["windows"] == []
    assert cal.force_first_night({"kind": "forecast_based", "windows": []}, {"windows": []}, 0.3, model)[1:] == (False, "no cheap-rate window in the next 48 hours")


def test_the_forced_window_joins_a_high_demand_policy():
    model = BatteryModel(reserve=0.10)
    high = {"kind": "full_overnight", "windows": [{"night_date": "2026-10-26", "target": 1.0, "intervals": [{"start": NOW + DAY, "end": NOW + DAY + 6 * 3600}]}], "reason": "High"}
    policy, forced, _ = cal.force_first_night(high, windows(), 0.3, model)
    assert forced and [w["night_date"] for w in policy["windows"]] == ["2026-10-25", "2026-10-26"] and policy["reason"] == "High"


# ===== inside the planner =================================================================================================================================
def make_due(store):
    soc_rows(store, (7.0, 0.5), (6.5, 1.0))                                              # last full 6.5 days ago


def test_a_due_calibration_makes_the_next_window_a_checked_full_charge(site):
    store, _ = site
    normal_history(store)
    make_due(store)
    value = job(site)
    assert value["calibration"]["state"] == "due" and value["calibration"]["forced"] is True
    assert value["charge_policy"]["kind"] == "full_overnight" and value["charge_policy"]["windows"][0]["target"] == 1.0
    result = solve(value)
    validate_result(value, result)
    assert result["next_target"] == 1.0


def test_when_it_is_not_due_the_planner_is_free(site):
    store, _ = site
    normal_history(store)
    soc_rows(store, (7.0, 0.5), (2.0, 1.0))
    value = job(site)
    assert value["calibration"]["state"] == "ok" and value["calibration"]["forced"] is False and value["charge_policy"]["kind"] == "forecast_based"


def test_the_owner_can_lengthen_the_interval(site):
    store, _ = site
    normal_history(store)
    make_due(store)
    put(store, "calibration", {"days": 10})
    assert job(site)["calibration"]["state"] == "ok"


def test_a_forced_charge_that_cannot_be_reached_falls_back_to_a_plan_without_it(site):
    store, clock = site
    normal_history(store)
    make_due(store)
    calls = []

    def solver(value):
        calls.append(bool(value["calibration"]["forced"]))
        if value["calibration"]["forced"]:
            return {"status": "infeasible", "controls_enabled": False, "inputs_fingerprint": "x"}
        return solve(value)
    attempt = ShadowWorker(store, clock=clock, solver=solver).once()
    assert calls == [True, False] and attempt["status"] == "published"
    published = store.get_setting("shadow_plan")[1]
    assert published["calibration"]["forced"] is False and "could not be reached" in published["calibration"]["note"]


def test_the_published_plan_says_why_it_charges_fully(site):
    store, clock = site
    normal_history(store)
    make_due(store)
    attempt = ShadowWorker(store, clock=clock, solver=solve).once()
    assert attempt["status"] == "published"
    plan = store.get_setting("shadow_plan")[1]
    assert plan["calibration"]["forced"] is True and plan["grid_target"] == 1.0 and plan["calibration"]["days_since"] == pytest.approx(6.5, abs=0.1)


# ===== the website ======================================================================================================================================
def test_the_setting_and_the_status_through_the_website(tmp_path):
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    soc_rows(store, (7.0, 0.5), (1.0, 1.0))
    app = create_app(WebConfig(tmp_path / "c.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), FakeClock(NOW), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as c:
        status = c.get("/api/status").json()
        c.headers.update({"Origin": "http://solarbank-pi.local", "X-CSRF-Token": status["csrf_token"]})
        assert status["calibration"]["max_days"] == 7 and status["calibration"]["state"] == "ok" and status["calibration"]["days_since"] == pytest.approx(1.0)
        assert c.put("/api/calibration", json={"revision": status["calibration_revision"], "days": 10}).status_code == 200
        assert c.get("/api/status").json()["calibration"]["max_days"] == 10
        for bad in (2, 15, 7.5):
            assert c.put("/api/calibration", json={"revision": 1, "days": bad}).status_code == 422
        assert c.put("/api/calibration", json={"revision": 0, "days": 8}).status_code == 409           # a stale revision
        page = c.get("/settings").text
        assert 'id="calibration-days"' in page and 'id="calibration-form"' in page
