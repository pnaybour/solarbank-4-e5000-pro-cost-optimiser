"""WP24: the owner's decision table (v1.3 section 7) as the Supervisor now applies it, with the clock passed in explicitly."""
from datetime import datetime

import pytest
from hypothesis import given, settings, strategies as st

from solarbank_optimiser.control_logic import Supervisor, read_plan
from solarbank_optimiser.energy import LONDON
from test_control_loop import CAP, fresh, plan, run

ZONE = LONDON


def london(hour, minute=0, day=15, month=1, year=2027):
    return datetime(year, month, day, hour, minute, tzinfo=ZONE).timestamp()


def decide_at(sup, hour, minute, inputs, p, note=None, now=0.0):
    return sup.decide(inputs, p, note, now, utc=london(hour, minute) + now)


def warm(sup, hour, minute, inputs, p, seconds=60):
    d = None
    for i in range(seconds):
        d = sup.decide(inputs, p, None, float(i), utc=london(hour, minute) + i)
    return d, float(seconds)


@pytest.mark.parametrize("p,note", [(None, "No plan file yet"), (None, "Plan has expired"), (plan("normal", target=None), None), (plan("hold"), None)])
def test_a_plan_that_is_missing_expired_or_withdrawn_at_0200_still_holds_the_battery(p, note):
    sup = Supervisor()
    d = None
    for i in range(60):
        d = sup.decide(fresh(current=20), p, note, float(i), utc=london(2, 0) + i)
    assert d.discharge == "INHIBIT" and d.charge_limit_w == 0 and d.economic in ("hold", "withdrawn")


def test_outside_the_cheap_period_with_no_plan_the_anker_runs_the_house():
    sup = Supervisor()
    d, _ = warm(sup, 14, 0, fresh(current=20), None)
    assert d.discharge == "ALLOW" and d.charge_limit_w == 0


@pytest.mark.parametrize("hour,minute,cheap", [(23, 29, False), (23, 30, True), (2, 0, True), (5, 29, True), (5, 30, False), (12, 0, False)])
def test_cheap_follows_the_london_clock_not_the_plan(hour, minute, cheap):
    sup = Supervisor()
    d, _ = warm(sup, hour, minute, fresh(current=20), plan("normal", target=None))
    assert (d.discharge == "INHIBIT") is cheap


def test_the_planner_can_publish_its_own_cheap_window_and_a_missing_plan_keeps_it():
    sup = Supervisor()
    saved = plan("normal", target=None); saved["offpeak"] = {"start_minute": 0, "end_minute": 420}        # 00:00 to 07:00
    d, _ = warm(sup, 6, 0, fresh(current=20), saved)
    assert d.discharge == "INHIBIT"                                          # 06:00 is cheap under the published window
    d = sup.decide(fresh(current=20), None, "Plan has expired", 61.0, utc=london(6, 30) + 61)
    assert d.discharge == "INHIBIT"                                          # and the plan going missing does not change that
    d = sup.decide(fresh(current=20), None, "Plan has expired", 62.0, utc=london(23, 45) + 62)
    assert d.discharge == "ALLOW"                                            # 23:45 is NOT cheap under it (standard window not in force)


def test_read_plan_accepts_a_well_formed_offpeak_and_rejects_a_malformed_one(tmp_path):
    import json
    good = {**plan("normal", target=None), "offpeak": {"start_minute": 1410, "end_minute": 330}}
    f = tmp_path / "p.json"; f.write_text(json.dumps(good))
    assert read_plan(f, good["issued_at"] + 1)[0]["offpeak"] == {"start_minute": 1410, "end_minute": 330}
    for bad in ({"start_minute": 5, "end_minute": 5}, {"start_minute": 2000, "end_minute": 5}, {"start_minute": "x", "end_minute": 5}, 7):
        f.write_text(json.dumps({**good, "offpeak": bad}))
        assert read_plan(f, good["issued_at"] + 1)[0] is None


def test_a_car_charging_outside_the_cheap_period_holds_the_battery_and_inside_it_charging_goes_on():
    sup = Supervisor()
    car = fresh(current=20); car["shelly"]["value"]["car"] = 7000.0
    d, _ = warm(sup, 14, 0, car, plan("normal", target=None))
    assert d.discharge == "INHIBIT" and d.charge_limit_w == 0 and "ev_active" in d.restrictions       # rule 3: no discharge into the car


@given(hour=st.integers(0, 23), minute=st.integers(0, 59), soc=st.integers(11, 100), car=st.booleans(),
       action=st.sampled_from(["normal", "charge_to", "hold", None]), target=st.floats(0.2, 1.0))
@settings(max_examples=60, deadline=None)
def test_in_a_cheap_period_the_battery_is_never_released_to_discharge_unless_it_is_charging(hour, minute, soc, car, action, target):
    sup = Supervisor()
    inputs = fresh(soc=soc, current=20)
    inputs["shelly"]["value"]["car"] = 7000.0 if car else 0.0
    p = None if action is None else plan(action, target=target if action == "charge_to" else None)
    d = None
    for i in range(45):
        d = sup.decide(inputs, p, None if p else "Plan has expired", float(i), utc=london(hour, minute) + i)
    cheap = hour * 60 + minute >= 23 * 60 + 30 or hour * 60 + minute < 5 * 60 + 30
    if cheap:
        charging_wanted = p is not None and action == "charge_to" and soc / 100.0 <= target - 0.03
        assert d.discharge == "INHIBIT" or charging_wanted
