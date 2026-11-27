"""Two fixes from 29 October: charging starts at the exact start of the window, and the forecast panel stays populated for its whole refresh interval."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.control_logic import read_plan
from solarbank_optimiser.shelly_channels import CAR, SOLARBANK_AC
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.services import FORECAST_FRESH_SECONDS, FORECAST_REFRESH_SECONDS
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app
from test_control_loop import Clock, UTC, build, live_with, plan

START = UTC + 120


def with_window(**changes):
    return {**plan("normal", issued_at=UTC - 200, valid_until=UTC + 7200), "window": {"start": START, "end": START + 3600, "target_soc": 1.0}, **changes}


# ===== reading the window =========================================================================================================================
def test_the_window_decides_the_action_to_the_second(tmp_path):
    f = tmp_path / "plan.json"; f.write_text(json.dumps(with_window()))
    action = lambda t: read_plan(f, t)[0]
    assert action(START - 1)["action"] == "normal" and action(START - 1)["target_soc"] is None
    assert action(START)["action"] == "charge_to" and action(START)["target_soc"] == 1.0
    assert action(START + 3599)["action"] == "charge_to"
    assert action(START + 3600)["action"] == "normal"                          # the window has ended: the request is withdrawn without a new plan


def test_hold_and_hand_over_are_not_overridden_by_a_window(tmp_path):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(with_window(action="hold")))
    assert read_plan(f, START + 5)[0]["action"] == "hold"
    f.write_text(json.dumps(with_window(mode="anker_manual")))
    assert read_plan(f, START + 5)[0]["mode"] == "anker_manual"


@pytest.mark.parametrize("window", [{"start": 10, "end": 5, "target_soc": .5}, {"start": 1, "end": 2, "target_soc": 1.5}, {"start": "x", "end": 2, "target_soc": .5},
                                    {"start": 1, "end": 2}, {"start": True, "end": 2, "target_soc": .5}, [], "x", {"start": 1e300, "end": 1e301, "target_soc": .5}])
def test_a_malformed_window_makes_the_plan_invalid_not_charging(tmp_path, window):
    f = tmp_path / "plan.json"; f.write_text(json.dumps(with_window(window=window)))
    got, why = read_plan(f, START + 5)
    assert got is None and "valid plan" in why


# ===== the controller, end to end ===========================================================================================================
def test_charging_starts_within_seconds_of_the_window_opening_with_no_new_plan(tmp_path):
    clock = Clock(); f = tmp_path / "plan.json"; f.write_text(json.dumps(with_window(offpeak={"start_minute": 482, "end_minute": 600})))     # the cheap period the planner publishes: 08:02 to 10:00 (the test's clock is 08:00)
    lp = build(tmp_path, clock, live_with(clock), plan_path=f, devices=("solarbank", "shelly", "meter"))
    first_charge = reached = None
    for second in range(0, 200):
        clock.t += 1
        lp.live.publish("solarbank", __import__("solarbank_optimiser.live", fromlist=["Reading"]).Reading({"soc": 50, "max_charge": 3600.0}, clock.t, UTC, 1, .02))
        lp.live.publish("shelly", __import__("solarbank_optimiser.live", fromlist=["Reading"]).Reading({SOLARBANK_AC: 1.0, CAR: 0.0}, clock.t, UTC, 1, .02))
        lp.live.publish("meter", __import__("solarbank_optimiser.live", fromlist=["Reading"]).Reading({"main": 0, "sub": 0, "voltage": 230.0, "garage_current_a": 1.0}, clock.t, UTC, 1, .02))
        decision, _ = lp.cycle()
        elapsed = second + 1
        if decision.charge_limit_w > 0 and first_charge is None:
            first_charge = elapsed
        if decision.charge_limit_w >= 2500 and reached is None:
            reached = elapsed
        if elapsed < 120:
            assert decision.charge_limit_w == 0 and decision.economic != "charge_to"           # nothing before the window opens
    start_second = START - UTC
    assert first_charge is not None and start_second <= first_charge <= start_second + 2        # the very first second of the window
    assert reached is not None and reached <= start_second + 25                                # full 2.5 kW within about 20 seconds, as last night's ramp


def test_the_planner_file_for_a_future_window_is_not_cut_short_but_still_expires(tmp_path):
    from solarbank_optimiser.shadow import controller_plan_document
    summary = {"issued_at": UTC - 5, "valid_until": UTC + 240, "grid_target": .85, "issuance_id": "abc", "next_window": {"start": UTC + 100, "end": UTC + 3700, "target": .85}}
    document = controller_plan_document(summary, UTC)
    assert document["valid_until"] == UTC + 240 and document["action"] == "normal" and document["window"]["target_soc"] == .85
    assert controller_plan_document(summary, UTC + 150)["action"] == "charge_to"


# ===== the forecast panel ====================================================================================================================
def site(tmp_path, clock):
    store = Store(tmp_path / "f.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "f.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), clock, store=store)
    return TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)), store


def test_the_forecast_stays_shown_for_its_whole_refresh_interval_and_a_bit_more(tmp_path):
    assert FORECAST_FRESH_SECONDS > FORECAST_REFRESH_SECONDS                                    # the cause of 29 October's blank panel: 300 s freshness, 900 s refresh
    clock = FakeClock(UTC)
    client, store = site(tmp_path, clock)
    summary = {"issued_at": UTC, "valid_until": UTC + FORECAST_FRESH_SECONDS, "demand_status": "ok", "demand_reason": None, "demand_kwh": 26.5, "demand_revision": 0,
               "provider_revision": 0, "fallback_revision": 0, "solar_status": "ok", "pv_kwh": 11.4}
    store.set_setting("forecast_summary", summary, UTC, 0)
    with client:
        for age, fresh in ((10, True), (400, True), (FORECAST_REFRESH_SECONDS, True), (FORECAST_FRESH_SECONDS, True), (FORECAST_FRESH_SECONDS + 5, False)):
            clock.wall = UTC + age
            assert client.get("/api/status").json()["forecast"]["fresh"] is fresh, age


def test_the_forecast_job_writes_a_validity_that_matches_the_page(tmp_path):
    from test_phase3 import NOW, fallback, normal, weather_fixture, put as put3
    from solarbank_optimiser.models import encoded
    from solarbank_optimiser.services import ProviderConfig, ProviderWorker
    store = Store(tmp_path / "g.sqlite3"); store.initialise()
    fallback(store); put3(store, "providers", ProviderConfig(weather_enabled=True).model_dump()); store.add_observations(normal())
    ProviderWorker(store, clock=FakeClock(NOW), transport=lambda *a: encoded(weather_fixture()).encode()).once()
    summary = store.get_setting("forecast_summary")[1]
    assert summary["valid_until"] - summary["issued_at"] == FORECAST_FRESH_SECONDS
