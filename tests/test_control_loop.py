"""4b.1: one controller with complete logic, run in shadow now and switched to live later by a flag - not by new code.

The expected results come from the specification (sections 8, 9, 10) and the second code review, not from the implementation."""
import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from solarbank_optimiser import control_logic as cl
from solarbank_optimiser.shelly_channels import CAR, SOLARBANK_AC
from solarbank_optimiser import control_exec as ce
from solarbank_optimiser import control_loop as loop_mod
from solarbank_optimiser.control import SingleOwner
from solarbank_optimiser.control_exec import Executor, LogTransport, Outcome, Preparer
from solarbank_optimiser.control_logic import Supervisor, read_plan
from solarbank_optimiser.live import LiveState, Reading

UTC = 1_800_000_000.0
MAXW = 3600.0                      # what the device says it can do ...
CAP = cl.AC_CHARGE_LIMIT_W         # ... and what the installation allows (2.5 kW shared AC limit)


def fresh(soc=50, ev_w=0.0, current=None, max_charge=MAXW, devices=("solarbank", "shelly")):
    inputs = {}
    if "solarbank" in devices:
        inputs["solarbank"] = {"value": {"soc": soc, **({"max_charge": max_charge} if max_charge is not None else {})}, "age": .3}
    if "shelly" in devices:
        inputs["shelly"] = {"value": {SOLARBANK_AC: 100.0, CAR: ev_w}, "age": .3}
    if current is not None:
        inputs["meter"] = {"value": {"garage_current_a": current, "voltage": 230.0}, "age": .3}      # GitHub issue 69: a normal, valid meter reading
    return inputs


def plan(action="charge_to", target=.9, mode="shadow", **extra):
    return {"mode": mode, "action": action, "target_soc": target, "issued_at": UTC, "valid_until": UTC + 36000, **extra}


def run(sup, steps, inputs, p=None, note=None, t0=0.0):
    """Run the supervisor once a second for `steps` seconds; return the last decision."""
    d = None
    for i in range(steps):
        d = sup.decide(inputs, p, note, t0 + i)
    return d


def settled(current=20.0, **kw):
    """A supervisor that has been charging steadily at full power with the car idle for long enough."""
    sup = Supervisor()
    inputs = fresh(current=current, **kw)
    d = run(sup, 400, inputs, plan())
    assert d.charge_limit_w == CAP, d.charge_limit_w
    return sup, 400.0


# ===== garage regulation: owner specification v1.3 section 9 (decision D14) =======================================================================
def charging(current, watts=CAP, **kw):
    """Inputs with the garage at `current` amps in total and the Solarbank AC input drawing `watts` (a NEGATIVE Shelly reading on this installation)."""
    inputs = fresh(current=current, **kw)
    inputs["shelly"]["value"][SOLARBANK_AC] = -watts
    return inputs


def test_the_battery_is_not_counted_against_itself_so_a_total_of_44_9_a_with_it_charging_is_fine():
    sup, t = settled()
    d = sup.decide(charging(44.9), plan(), None, t)                      # 2.5 kW at 230 V is 10.9 A of the total: the other loads are 34 A
    assert d.charge_limit_w == CAP and "garage_limit" not in d.restrictions


def test_over_the_limit_the_request_drops_at_once_to_what_the_other_loads_leave_not_by_halves():
    sup, t = settled()
    d = sup.decide(charging(48.0), plan(), None, t)                      # 48 A in total, 10.87 A of it the battery: 37.13 A other, 7.87 A of headroom
    assert d.charge_limit_w == pytest.approx(1810, abs=20) and "garage_limit" in d.restrictions
    assert d.charge_limit_w < CAP
    # GitHub issue 73: the garage feed's own headroom is what's binding here, so the reason names it, not the installation's own charge limit
    assert any("Garage feed" in r and "45 A limit" in r for r in d.reasons)
    assert not any("installation's own charge limit" in r for r in d.reasons)


def test_the_devices_own_taper_is_not_reported_as_a_garage_limit():
    """GitHub issue 73, the night of 9 to 10 November: the commanded power fell from 2,500 W to 0 W with the garage at 3 to 11 A, and every step was
    logged as "Garage feed ... against the 45 A limit". The cause: the device's own live charge limit (10036) was falling, the request follows it
    exactly, and the set-point is rounded down to 10 W, so it sat a few watts under the request."""
    sup, t = settled()
    d = sup.decide(charging(10.8, watts=2315.0, max_charge=2315.0), plan(), None, t)
    assert d.charge_limit_w == 2310.0 and "garage_limit" not in d.restrictions and not any("45 A limit" in r for r in d.reasons)
    assert any("battery's own charge limit (2315 W)" in r for r in d.reasons)
    d = sup.decide(charging(10.8, watts=2310.0, max_charge=2310.0), plan(), None, t + 2)      # a limit that is a multiple of 10 W: nothing to say at all
    assert d.restrictions == () and not any("limit" in r for r in d.reasons)


def test_other_loads_alone_over_the_limit_stop_the_charge_and_after_10_s_raise_an_alert():
    sup, t = settled()
    d = None
    for i in range(0, 15):
        d = sup.decide(fresh(current=47.0), plan(), None, t + i)         # the battery draws nothing: the 47 A is other loads
        if i == 0:
            assert d.charge_limit_w == 0 and "garage_alert" not in d.restrictions
    assert d.charge_limit_w == 0 and "garage_alert" in d.restrictions and any("alert" in r for r in d.reasons)


def test_a_sudden_rise_is_taken_from_the_latest_sample_not_the_slower_average():
    sup, t = settled(current=20.0)
    for i in range(0, 5):
        sup.decide(charging(20.0), plan(), None, t + i)
    d = sup.decide(charging(55.0), plan(), None, t + 5)                  # the 3 s average is still about 28 A: the latest sample decides
    assert d.charge_limit_w < CAP


def test_there_is_no_recovery_timer_the_charge_comes_back_as_soon_as_the_write_rules_allow():
    sup, t = settled()
    sup.decide(charging(50.0), plan(), None, t)                           # down at once to about 1,350 W
    assert sup.charge_w < CAP
    d = sup.decide(charging(30.0, watts=sup.charge_w), plan(), None, t + 1.5)
    assert d.charge_limit_w == CAP                                        # no 30 s under 43 A first (that timer was the cause of #18)


def test_an_increase_smaller_than_50_w_is_not_written_and_one_within_5_s_of_the_last_increase_waits():
    sup = Supervisor(); run(sup, 60, charging(20.0, watts=0.0), plan())   # started: charging at the full 2,500 W
    assert sup.charge_w == CAP
    sup.decide(charging(50.0, watts=CAP), plan(), None, 100.0)            # 50 A in total, 10.87 A of it the battery: 5.87 A of headroom, about 1,350 W
    low = sup.charge_w; assert low == pytest.approx(1350, abs=20)
    sup.decide(charging(30.0, watts=low), plan(), None, 100.5)            # more room, but never more than one write a second
    assert sup.charge_w == low
    sup.decide(charging(30.0, watts=low), plan(), None, 101.5)            # an increase (the first one since starting is long ago): written
    assert sup.charge_w == CAP
    sup.decide(charging(50.0, watts=CAP), plan(), None, 102.5)            # down at once again
    assert sup.charge_w == pytest.approx(1350, abs=20)
    sup.decide(charging(30.0, watts=sup.charge_w), plan(), None, 103.5)   # an increase 2 s after the last one: waits for RAMP_UP_MIN_INTERVAL_S
    assert sup.charge_w < CAP
    sup.decide(charging(30.0, watts=sup.charge_w), plan(), None, 106.6)
    assert sup.charge_w == CAP
    held = sup.charge_w
    sup.decide(charging(44.8, watts=held), plan(), None, 120.0)           # 44.8 A total: 34 A other, a target of 2,500 W is still met; no change
    assert sup.charge_w == held
    sup.decide(charging(50.0, watts=held), plan(), None, 121.0)
    reduced = sup.charge_w
    sup.decide(charging(44.8, watts=reduced), plan(), None, 130.0)         # headroom only 40 W more than the set-point: below the 50 W deadband
    assert sup.charge_w == reduced


def test_unaligned_readings_never_raise_the_set_point_and_within_the_limit_never_cut_it():
    """A fresh garage reading beside a battery reading 8 s old (valid on its own): no increase is written; and (GitHub issue 80) while the measured total,
    which already includes the battery, is within the limit, the set-point is held, not cut."""
    sup = Supervisor(); run(sup, 60, charging(20.0, watts=0.0), plan())
    sup.decide(charging(44.0, watts=0.0), plan(), None, 100.0); low = sup.charge_w          # 44 A with the battery idle: about 230 W of headroom
    assert 0 < low < CAP
    stale_battery = charging(30.0, watts=low); stale_battery["shelly"]["age"] = 8.0
    for t in range(110, 140):
        d = sup.decide(stale_battery, plan(), None, float(t))
    assert d.charge_limit_w == low                                                         # neither raised (unaligned) nor cut (30 A is within 45 A)


def test_an_unconfirmed_protective_command_blocks_increases_but_never_erases_how_long_the_current_has_been_low():
    """#18: the control_ok flag only stops an increase while it is false."""
    sup = Supervisor(); run(sup, 40, charging(30.0, watts=0.0), plan(), None)
    assert sup.charge_w == CAP
    sup2 = Supervisor()
    for i in range(0, 100):
        d = sup2.decide(charging(30.0, watts=0.0), plan(), None, float(i), control_ok=(i % 10 != 0))     # a refresh every 10 s makes it briefly False
    assert d.charge_limit_w == CAP


def test_missing_garage_current_keeps_charging_restricted_and_says_so():
    sup = Supervisor()
    d = run(sup, 600, fresh(), plan())                                      # no meter input at all (the current today)
    assert d.charge_limit_w == 0 and "garage_unknown" in d.restrictions and "unknown" in " ".join(d.reasons)
    assert d.preview_charge_w == CAP                                        # what the plan alone would do remains visible
    assert d.discharge == "INHIBIT"                                         # v1.3 section 8: a charge held at 0 W stays in mode 3, so the battery is not released at the cheap rate


def test_a_plan_can_never_relax_the_limit():
    sup, t = settled()
    d = sup.decide(fresh(current=60), plan(target=1.0), None, t)
    assert d.charge_limit_w < CAP and "garage_limit" in d.restrictions


# ===== specification 11.2 validity checks (GitHub issue 69, owner decision A, with an email alert) ================================================
def test_an_out_of_range_or_missing_meter_voltage_stops_charging_and_says_so():
    sup, t = settled()
    inputs = fresh(current=20)
    inputs["meter"]["value"]["voltage"] = 206.0                              # out of the 207-253 V range
    d = sup.decide(inputs, plan(), None, t)
    assert d.charge_limit_w == 0 and "meter_voltage_invalid" in d.restrictions and "garage_unknown" not in d.restrictions
    inputs["meter"]["value"].pop("voltage")                                  # missing: also invalid, not the nominal 230 V fallback used elsewhere
    d = sup.decide(inputs, plan(), None, t + 1)
    assert d.charge_limit_w == 0 and "meter_voltage_invalid" in d.restrictions
    inputs["meter"]["value"]["voltage"] = 230.0                              # back in range: the check itself clears at once
    d = sup.decide(inputs, plan(), None, t + 2)
    assert "meter_voltage_invalid" not in d.restrictions
    # v1.3 11.1: like any other fault recovery, normal operation resumes only once the reading has been valid again for 30 s
    assert d.charge_limit_w == 0 and "garage_starting" in d.restrictions


def test_p_unmetered_below_230_w_needs_30_s_before_it_stops_charging():
    sup, t = settled()
    inputs = fresh(current=20)
    inputs["meter"]["value"]["sub"] = -400.0                                 # P_garage: as if a current-transformer channel is wrong or reversed
    d = sup.decide(inputs, plan(), None, t)                                  # P_unmetered = -400 - 0 - (-100) = -300 W: drops to "low"
    assert d.charge_limit_w > 0 and "reconciliation_invalid" not in d.restrictions
    d = sup.decide(inputs, plan(), None, t + 29)
    assert d.charge_limit_w > 0 and "reconciliation_invalid" not in d.restrictions        # still under 30 s
    d = sup.decide(inputs, plan(), None, t + 31)
    assert d.charge_limit_w == 0 and "reconciliation_invalid" in d.restrictions
    inputs["meter"]["value"]["sub"] = 0.0                                     # recovers: the check itself clears at once, no dwell needed to clear
    d = sup.decide(inputs, plan(), None, t + 32)
    assert "reconciliation_invalid" not in d.restrictions
    assert d.charge_limit_w == 0 and "garage_starting" in d.restrictions      # v1.3 11.1: 30 s valid again before normal operation resumes


# ===== reserve and EV inhibition ====================================================================================================
@pytest.mark.parametrize("soc", [10, 9, 5, 0, 10.1, 50])
def test_the_minimum_level_never_inhibits_discharge_by_itself(soc):
    """GitHub issue 64 (D13, owner 2 November): the minimum SoC floor is left entirely to the firmware; the controller no longer stops discharge
    at or below the stored level (the 28 October rule, superseded). It still clamps the charge target and is shown on Settings (issue 70)."""
    sup = Supervisor()
    d = run(sup, 400, fresh(soc=soc, current=20), plan("discharge"))
    assert d.discharge == "ALLOW" and "reserve" not in d.restrictions


@pytest.mark.parametrize("ev,inhibited", [(7000.0, True), (1001.0, True), (1000.0, False), (350.0, False), (10.0, False), (3.0, False)])
def test_a_charging_car_above_1000_w_for_3_s_holds_the_battery_in_an_expensive_period(ev, inhibited):
    """Owner specification v1.3 (D7): on above 1,000 W for 3 s. The 10 W cutoff of the old requirements is retired."""
    sup, t = settled()
    inputs = fresh(current=20)
    inputs["shelly"]["value"][CAR] = ev
    seen = [sup.decide(inputs, plan("discharge"), None, t + i).discharge for i in range(0, 6)]
    assert (seen[-1] == "INHIBIT") is inhibited
    assert seen[0] == "ALLOW" and seen[2] == "ALLOW"                     # never before the 3 s have passed


@pytest.mark.parametrize("ev", [None, -5.0, float("nan"), True, "7000"])
def test_a_missing_or_invalid_charger_reading_is_missing_data_not_a_car(ev):
    """Owner, 30 October: missing data is missing data. With the charger last seen idle, a missing or invalid reading never inhibits discharge."""
    sup, t = settled()
    inputs = fresh(current=20)
    if ev is None:
        inputs.pop("shelly")
    else:
        inputs["shelly"]["value"][CAR] = ev
    for i in range(0, 120):                                              # short dropout and long outage alike
        d = sup.decide(inputs, plan("discharge"), None, t + i)
        assert d.discharge == "ALLOW", i
    assert "ev_data_missing" in d.restrictions and "not as a car charging" in " ".join(d.reasons)


def test_the_car_needs_120_s_below_300_w_to_count_as_stopped_and_the_band_in_between_changes_nothing():
    sup, t = settled()
    inputs = fresh(current=20)
    inputs["shelly"]["value"][CAR] = 7000.0
    for i in range(0, 5):
        d = sup.decide(inputs, plan("discharge"), None, t + i)
    assert d.discharge == "INHIBIT" and "ev_active" in d.restrictions
    inputs["shelly"]["value"][CAR] = 350.0                                 # tapering, between the thresholds: still charging
    for i in range(5, 400):
        assert sup.decide(inputs, plan("discharge"), None, t + i).discharge == "INHIBIT"
    inputs["shelly"]["value"][CAR] = 100.0                                 # below 300 W: the 120 s start
    for i in range(400, 519):
        assert sup.decide(inputs, plan("discharge"), None, t + i).discharge == "INHIBIT"
    assert sup.decide(inputs, plan("discharge"), None, t + 521).discharge == "ALLOW"


def test_a_car_known_to_be_charging_stays_held_through_an_outage_for_ten_minutes_then_is_released():
    """D15: a gap up to 10 s changes nothing; a longer gap holds only a car known to be charging, for up to 10 minutes."""
    sup, t = settled()
    car = fresh(current=20); car["shelly"]["value"][CAR] = 7000.0
    for i in range(0, 5):
        sup.decide(car, plan("discharge"), None, t + i)
    no_shelly = fresh(current=20); no_shelly.pop("shelly")
    d = sup.decide(no_shelly, plan("discharge"), None, t + 9)                       # a short dropout: carried on exactly as before
    assert d.discharge == "INHIBIT" and "ev_active" in d.restrictions
    d = sup.decide(no_shelly, plan("discharge"), None, t + 60)                      # a longer outage: the car was known to be charging
    assert d.discharge == "INHIBIT"
    assert sup.decide(no_shelly, plan("discharge"), None, t + 4 + 600).discharge == "INHIBIT"
    d = sup.decide(no_shelly, plan("discharge"), None, t + 4 + 601)                 # after ten minutes of nothing, missing data is just missing
    assert d.discharge == "ALLOW" and "ev_data_missing" in d.restrictions and sup.ev_state == "lost"
    zero = fresh(current=20)
    d = sup.decide(zero, plan("discharge"), None, t + 700)                          # the charger is read again, idle: no waiting
    assert d.discharge == "ALLOW" and sup.ev_state == "standby"


def test_every_decision_is_explicit_there_is_no_no_change():
    sup = Supervisor()
    for inputs, p in ((fresh(), None), (fresh(current=20), plan()), ({}, plan()), (fresh(soc=3, current=20), plan("hold"))):
        d = sup.decide(inputs, p, "n/a", 1.0)
        assert d.discharge in ("INHIBIT", "ALLOW") and isinstance(d.charge_limit_w, float) and d.economic in (
            "charge_to", "discharge_ok", "hold", "withdrawn", "handed_over")


def test_anker_manual_hand_over_issues_only_a_release_and_taking_control_back_starts_restricted():
    sup, prep = settled()[0], Preparer("r")
    d = run(sup, 3, fresh(current=60), plan(mode="anker_manual"), t0=1000.0)
    commands = prep.prepare(d, UTC, 1000.0, 1)
    assert d.economic == "handed_over" and [(c.channel, c.action) for c in commands] == [("control", "RELEASE")]
    back = sup.decide(fresh(current=20), plan(), None, 1010.0)                  # control returns: nothing from before is trusted
    assert back.charge_limit_w == 0 and "garage_starting" in back.restrictions


def test_a_hand_over_cancels_every_pending_command_and_the_release_must_be_confirmed():
    transport = Scripted([Outcome("timeout")])
    ex = Executor(transport, max_attempts=100, effect=lambda r, i: True)
    ex.execute([cmd("ac_charge", "CHARGE_LIMIT", "economic", 1), cmd("discharge", "ALLOW", "economic", 2),
                cmd("discharge", "INHIBIT", "protective", 3, cycle=1)], {}, UTC, 0.0)
    assert ex.ownership() == "controller"
    transport.script = [Outcome("timeout")]
    release = ce.Command("rel", 4, "control", "RELEASE", {}, "protective", UTC + 1, UTC + 31, "hand-over", 4, 31.0)
    before = len(transport.calls)
    ex.execute([release], {}, UTC + 1, 1.0)
    assert {ex.records[i]["stage"] for i in ("c1", "c2", "c3")} == {"superseded"}      # nothing old is retried: not charging, not discharge permission
    assert {c.id for c in transport.calls[before:]} == {"rel"}
    assert ex.ownership() == "handover_pending"                                          # requested, not confirmed
    transport.script = [Outcome("acknowledged")]
    ex.execute([], {}, UTC + 2, 2.0)
    assert ex.ownership() == "anker"
    ex.execute([ce.Command("n1", 5, "ac_charge", "STOP", {}, "protective", UTC + 5, UTC + 35, "back", 5, 35.0)], {}, UTC + 5, 5.0)
    assert ex.ownership() == "controller"                                                # control was taken back


def test_a_failed_protective_command_blocks_any_increase_until_control_is_verified():
    sup, t = settled()
    ex = Executor(Scripted([Outcome("rejected", "no")]), effect=lambda r, i: True)
    prep = Preparer("run")
    d = sup.decide(fresh(current=48), plan(), None, t, ex.control_verified())
    ex.execute(prep.prepare(d, UTC, t, 1), {}, UTC, t)                                   # the reduction is rejected by the device
    assert ex.control_verified() is False
    for k in range(1, 60):                                                                # 60 s of perfectly healthy current ...
        d = sup.decide(fresh(current=20), plan(), None, t + k, ex.control_verified())
    assert d.charge_limit_w < CAP and "control_unverified" in d.restrictions            # ... and still no increase
    ex.transport.script = [Outcome("acknowledged")]
    ex.execute(prep.prepare(d, UTC + 60, t + 60, 2), {}, UTC + 60, t + 60)
    ex.execute([], {}, UTC + 61, t + 61)
    assert ex.control_verified() is True                                                  # confirmed (acknowledged and effect seen)
    for k in range(61, 200):
        d = sup.decide(fresh(current=20), plan(), None, t + k, ex.control_verified())
    assert d.charge_limit_w == CAP


def test_without_an_effect_check_an_acknowledgement_never_becomes_verified_by_waiting():
    ex = Executor(Scripted([Outcome("acknowledged")]), effect=None)
    ex.execute([cmd("ac_charge", "STOP", "protective", 1)], {}, UTC, 0.0)
    assert ex.records["c1"]["stage"] == "acknowledged"
    ex.execute([], {}, UTC + ce.OBSERVE_WITHIN_S + 1, ce.OBSERVE_WITHIN_S + 1)
    assert ex.records["c1"]["stage"] == "unverified" and "no effect check" in ex.records["c1"]["outcome"] and ex.control_verified() is False


def test_discharge_belongs_to_the_anker_unless_an_exception_applies():
    sup = Supervisor()
    inputs = fresh(current=20)
    d = run(sup, 700, inputs, plan("normal"))
    assert d.discharge == "ALLOW" and d.economic == "discharge_ok"
    d = sup.decide(inputs, None, "Plan has expired", 701.0)                 # no plan: charge request withdrawn, discharge untouched
    assert d.economic == "withdrawn" and d.discharge == "ALLOW" and "no_authority" not in d.restrictions
    assert sup.decide(inputs, plan("charge_to"), None, 702.0).discharge == "ALLOW"        # the Anker decides how to charge and serve the house
    assert sup.decide(inputs, plan("hold"), None, 703.0).discharge == "INHIBIT"           # only an explicit hold restricts it
    # GitHub issue 64: the minimum level is the firmware's alone now, not an exception this function inhibits discharge for (issue 70 covers the clamp).
    assert sup.decide(fresh(soc=11, current=20), plan("normal"), None, 704.0).discharge == "ALLOW"
    assert sup.decide(fresh(soc=10, current=20), plan("normal"), None, 705.0).discharge == "ALLOW"


def test_the_charge_request_is_capped_by_the_installations_ac_limit_as_well_as_the_device():
    sup = Supervisor()
    d = run(sup, 400, fresh(current=20, max_charge=10000.0), plan())
    assert d.charge_limit_w == cl.AC_CHARGE_LIMIT_W == 2500.0 and d.preview_charge_w == 2500.0
    d = run(sup, 400, fresh(current=20, max_charge=1200.0), plan(), t0=1000.0)
    assert d.charge_limit_w == 1200.0                                                    # the lower of the two


def test_unknown_battery_state_withdraws_charging_but_does_not_restrict_discharge():
    sup = Supervisor()
    d = run(sup, 400, fresh(devices=("shelly",), current=20), plan())
    assert d.discharge == "ALLOW" and d.charge_limit_w == 0 and d.economic == "withdrawn" and "battery_unknown" in d.restrictions


# ===== starts restricted ===========================================================================================================
def test_a_fresh_controller_starts_restricted_whatever_the_measurements_say():
    sup = Supervisor()
    inputs = fresh(current=10)
    first = sup.decide(inputs, plan(), None, 0.0)
    assert first.charge_limit_w == 0 and "garage_starting" in first.restrictions     # no charging until the garage reading has been valid for 30 s
    assert sup.decide(inputs, plan(), None, 29).charge_limit_w == 0
    assert sup.decide(inputs, plan(), None, 31).charge_limit_w > 0                    # charging restored only after 30 s
    assert Supervisor().decide(inputs, plan("discharge"), None, 0.0).discharge == "ALLOW"      # discharge is the Anker's from the first cycle (v1.3 11.5: valid data, then the table)


# ===== the plan file ===============================================================================================================
def test_missing_expired_future_oversized_corrupt_and_nested_plans_are_no_plan(tmp_path):
    f = tmp_path / "plan.json"
    assert read_plan(f, UTC)[0] is None and "No plan" in read_plan(f, UTC)[1]
    cases = [(json.dumps({**plan(), "issued_at": UTC - 100, "valid_until": UTC - 1}), "expired"),
             (json.dumps({**plan(), "issued_at": UTC - 100, "valid_until": UTC}), "expired"),            # the expiry instant itself is expired
             (json.dumps({**plan(), "issued_at": UTC + 3600, "valid_until": UTC + 7200}), "future"),
             (json.dumps({**plan(), "issued_at": UTC, "valid_until": UTC - 5}), "valid plan"),           # inverted validity window
             (json.dumps({**plan(), "valid_until": UTC}), "valid plan"),                                 # zero-length window
             ('{"mode":"shadow","action":"hold","issued_at":' + "9" * 400 + ',"valid_until":' + "9" * 401 + "}", "valid plan"),   # OverflowError
             ('{"mode":"shadow","action":"hold","issued_at":1e300,"valid_until":1e301}', "valid plan"),
             ("{broken", "valid plan"), (json.dumps({**plan(), "action": "launch"}), "valid plan"),
             (json.dumps({**plan(), "target_soc": 7}), "valid plan"), (json.dumps({**plan(), "valid_until": True}), "valid plan"),
             (json.dumps({"mode": "shadow"}), "valid plan"), ("[]", "valid plan"), ("NaN", "valid plan"),
             ('{"issued_at": NaN, "valid_until": 1e999}', "valid plan"),
             ("[" * 30000 + "]" * 30000, "valid plan"), ('{"a":' * 9000 + "1" + "}" * 9000, "valid plan"),
             ("[" * 200000 + "]" * 200000, "too large"),
             (" " * (cl.PLAN_MAX_BYTES + 10), "too large")]
    for text, why in cases:
        f.write_text(text)
        got, note = read_plan(f, UTC)
        assert got is None and why in note, (text[:40], note)
    f.write_bytes(b"\xff\xfe\x00 not text")
    assert read_plan(f, UTC)[0] is None
    f.write_text(json.dumps(plan())); assert read_plan(f, UTC)[0]["target_soc"] == .9


def test_a_corrupt_plan_gives_a_restricted_decision_and_the_process_keeps_running(tmp_path):
    f = tmp_path / "plan.json"; f.write_text("[" * 30000 + "]" * 30000)
    clock = Clock(); lp = build(tmp_path, clock, live_with(clock), plan_path=f)
    for _ in range(3):
        decision, _ = lp.cycle(); clock.t += 1
    assert decision.economic == "withdrawn" and decision.charge_limit_w == 0 and lp.cycles == 3


# ===== commands: preparation, withdrawal, expiry, supersession ====================================================================
def prepare_for(inputs, p, note=None, t=0.0, sup=None, prep=None):
    sup, prep = sup or Supervisor(), prep or Preparer("run")
    d = sup.decide(inputs, p, note, t)
    return d, prep.prepare(d, UTC + t, t), sup, prep


def test_target_reached_holds_idle_and_a_plan_that_expires_explicitly_withdraws_the_previous_economic_command():
    sup, prep = Supervisor(), Preparer("run")
    inputs = fresh(current=20)
    t = 0.0
    while True:
        d = sup.decide(inputs, plan(), None, t)
        commands = prep.prepare(d, UTC + t, t, int(t) + 1); t += 1
        if d.charge_limit_w == CAP:
            break
    assert d.economic == "charge_to"
    done = fresh(soc=95, current=20)                                      # target (90 %) reached
    d = sup.decide(done, plan(), None, t); commands = prep.prepare(d, UTC + t, t, 99)
    assert d.economic == "hold" and [c.action for c in commands if c.channel == "ac_charge"] == ["WITHDRAW"]     # target reached: charge withdrawn, battery held idle
    assert d.discharge == "INHIBIT" and "holding idle" in " ".join(d.reasons)      # (the INHIBIT command itself may already be in force from the restart hold)
    t += 20; sup2, prep2 = settled()[0], Preparer("run2")                 # and the same when the plan simply expires
    prep2.prepare(sup2.decide(fresh(current=20), plan(), None, t), UTC, t)
    d = sup2.decide(fresh(current=20), None, "Plan has expired", t + 1)
    assert [c.action for c in prep2.prepare(d, UTC + 1, t + 1) if c.channel == "ac_charge"] == ["WITHDRAW"]


def test_unchanged_commands_are_not_repeated_until_the_refresh_then_get_a_new_id_and_expiry():
    sup, prep = Supervisor(), Preparer("run")
    inputs = fresh(current=20)
    seen = []
    for t in range(0, 700):
        d = sup.decide(inputs, plan("discharge"), None, float(t))
        seen += prep.prepare(d, UTC + t, float(t), t)
    steady = [c for c in seen if c.channel == "discharge" and c.action == "ALLOW"]
    assert len(steady) >= 3 and len({c.id for c in steady}) == len(steady)
    assert all(b.issued_at - a.issued_at >= ce.COMMAND_REFRESH_S for a, b in zip(steady, steady[1:]))      # not every second
    assert len(seen) < 170                                                                                  # ~ one refresh per channel per 10 s, not one per second
    assert all(c.expires_at == c.issued_at + ce.COMMAND_TTL_S for c in seen)


def test_a_protective_command_is_marked_protective_and_a_release_is_not():
    d, commands, *_ = prepare_for(fresh(), plan())                          # unknown garage current + unknown charger
    kinds = {(c.channel, c.action): c.priority for c in commands}
    assert kinds[("ac_charge", "STOP")] == "protective"
    held = {(c.channel, c.action): c.priority for c in prepare_for(fresh(current=20), plan("hold"))[1]}
    assert held[("discharge", "INHIBIT")] == "protective"


class Scripted:
    """A fake device transport. `script` is a list of outcomes; the last repeats. Simulated: never mistaken for a real device."""
    simulated = True
    sends = True
    effect = staticmethod(lambda record, inputs: True)           # a live transport must come with a way to see the effect

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def send(self, command, timeout):
        self.calls.append(command)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def cmd(channel="ac_charge", action="CHARGE_LIMIT", priority="economic", seq=1, issued=UTC, ttl=30.0, cycle=None, **params):
    """Monotonic time in these tests starts at 0, so the monotonic deadline is simply the lifetime."""
    return ce.Command(f"c{seq}", seq, channel, action, params or {"watts": 1000, "target_soc": .9}, priority, issued, issued + ttl, "test",
                      seq if cycle is None else cycle, ttl)


def test_acknowledged_rejected_and_timed_out_commands_are_recorded_separately_and_flagged_simulated():
    for outcome, stage in ((Outcome("acknowledged"), "acknowledged"), (Outcome("rejected", "illegal value"), "rejected"),
                           (Outcome("failed", "boom"), "failed")):
        ex = Executor(Scripted([outcome]))
        ex.execute([cmd()], {}, UTC, 0.0)
        record = ex.records["c1"]
        assert record["stage"] == stage and record["simulated"] is True and record["attempts"] == 1
    ex = Executor(Scripted([Outcome("timeout")]), max_attempts=3)
    for i in range(5):
        ex.execute([cmd()] if i == 0 else [], {}, UTC + i, float(i))
    assert ex.records["c1"]["stage"] == "timeout" and ex.records["c1"]["attempts"] == 3          # bounded retries, then it stops
    ex = Executor(Scripted([Exception("transport bug")]))
    ex.execute([cmd()], {}, UTC, 0.0)
    assert ex.records["c1"]["stage"] == "failed"                                                  # a transport error never escapes


def test_a_timeout_followed_by_an_acknowledgement_is_retried_once_and_then_acknowledged():
    ex = Executor(Scripted([Outcome("timeout"), Outcome("acknowledged")]))
    ex.execute([cmd()], {}, UTC, 0.0); assert ex.records["c1"]["stage"] == "attempted"
    ex.execute([], {}, UTC + 1, 1.0)
    assert ex.records["c1"]["stage"] == "acknowledged" and ex.records["c1"]["attempts"] == 2


def test_observed_needs_a_measured_effect_and_acknowledged_alone_is_not_observed():
    seen = {"v": False}
    ex = Executor(Scripted([Outcome("acknowledged")]), effect=lambda record, inputs: seen["v"])
    ex.execute([cmd()], {}, UTC, 0.0)
    ex.execute([], {}, UTC + 5, 5.0); assert ex.records["c1"]["stage"] == "acknowledged"
    seen["v"] = True
    ex.execute([], {}, UTC + 6, 6.0)
    assert ex.records["c1"]["stage"] == "observed" and ex.records["c1"]["effect_simulated"] is True
    ex2 = Executor(Scripted([Outcome("acknowledged")]), effect=lambda r, i: False)
    ex2.execute([cmd()], {}, UTC, 0.0); ex2.execute([], {}, UTC + 12, ce.OBSERVE_WITHIN_S + 1)
    assert ex2.records["c1"]["stage"] == "unverified"


def test_expired_queued_commands_are_discarded_and_never_executed_late():
    transport = Scripted([Outcome("timeout")])
    ex = Executor(transport, max_attempts=100)
    ex.execute([cmd(ttl=3.0)], {}, UTC, 0.0)
    for i in range(1, 8):
        ex.execute([], {}, UTC + i, float(i))
    assert ex.records["c1"]["stage"] == "expired" and len(transport.calls) == 3         # tried only while it was valid


def test_expiry_and_ordering_do_not_depend_on_the_wall_clock():
    transport = Scripted([Outcome("timeout")])
    ex = Executor(transport, max_attempts=100)
    ex.execute([cmd("ac_charge", "CHARGE_LIMIT", "economic", 1, issued=UTC + 1000)], {}, UTC + 1000, 0.0)
    back = ce.Command("c2", 2, "ac_charge", "STOP", {}, "protective", UTC, UTC + 30, "clock was set back", 2, 30.0)   # a LATER command with an EARLIER time
    before = len(transport.calls)
    ex.execute([back], {}, UTC, 1.0)
    assert ex.records["c1"]["stage"] == "superseded"
    assert {c.id for c in transport.calls[before:]} == {"c2"}
    ex.execute([], {}, UTC - 5000, 40.0)                                                # the wall clock jumps again: the monotonic deadline still decides
    assert ex.records["c2"]["stage"] == "expired"


def test_an_old_economic_command_cannot_run_after_a_newer_protective_restriction():
    transport = Scripted([Outcome("timeout")])
    ex = Executor(transport, max_attempts=100)
    ex.execute([cmd("ac_charge", "CHARGE_LIMIT", "economic", 1), cmd("discharge", "ALLOW", "economic", 2)], {}, UTC, 0.0)
    before = len(transport.calls)
    ex.execute([ce.Command("c3", 3, "ac_charge", "STOP", {}, "protective", UTC + 1, UTC + 31, "garage limit", 3, 31.0)], {}, UTC + 1, 1.0)
    assert ex.records["c1"]["stage"] == "superseded" and ex.records["c2"]["stage"] == "superseded"
    sent = [c.id for c in transport.calls[before:]]
    assert sent == ["c3"] * len(sent) and sent                                           # only the protective command goes on


def test_shadow_transport_suppresses_everything_and_never_claims_the_equipment_obeyed():
    ex = Executor(LogTransport())
    ex.execute([cmd(), cmd("discharge", "INHIBIT", "protective", 2, cycle=1)], {}, UTC, 0.0)
    for record in ex.records.values():
        assert record["stage"] == "suppressed" and record["simulated"] is False and record["attempts"] == 0
        assert set(record["stages"]) == {"prepared", "suppressed"}
    assert LogTransport.sends is False


# ===== shadow and live run the same decisions and prepared commands ==============================================================
def trace():
    """A recorded sequence of (inputs, plan) covering ramp-up, a car, the limiter, recovery, target reached and an outage."""
    steps = []
    for t in range(0, 1400):
        current = 20.0
        ev = 0.0
        if 100 <= t < 300:
            ev = 7000.0
        if 400 <= t < 410:
            current = 48.0
        if 600 <= t < 620:
            ev = None
        soc = 50 if t < 1000 else 95
        inputs = fresh(soc=soc, ev_w=ev if ev is not None else 0.0, current=current)
        if ev is None:
            inputs.pop("shelly")
        steps.append((float(t), inputs, plan() if t < 1200 else None, None if t < 1200 else "Plan has expired"))
    return steps


def pipeline(executor):
    sup, prep = Supervisor(), Preparer("same-run")
    decisions, commands = [], []
    for t, inputs, p, note in trace():
        d = sup.decide(inputs, p, note, t)
        prepared = prep.prepare(d, UTC + t, t, int(t) + 1)
        executor.execute(prepared, inputs, UTC + t, t)
        decisions.append(d.as_dict()); commands += [c.as_dict() for c in prepared]
    return decisions, commands


def test_the_same_recorded_trace_gives_identical_decisions_and_prepared_commands_in_shadow_and_live_test_modes():
    shadow = pipeline(Executor(LogTransport()))
    live_like = pipeline(Executor(Scripted([Outcome("acknowledged")])))
    assert shadow[0] == live_like[0] and shadow[1] == live_like[1] and len(shadow[1]) > 20
    kinds = {(c["channel"], c["action"]) for c in shadow[1]}
    assert {("ac_charge", "CHARGE_LIMIT"), ("ac_charge", "STOP"), ("ac_charge", "WITHDRAW"),
            ("discharge", "INHIBIT"), ("discharge", "ALLOW")} <= kinds             # the trace really exercised every command


# ===== the controller process ======================================================================================================
class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def live_with(clock, soc=50, ev_w=0.0, max_charge=MAXW):
    live = LiveState()
    live.publish("solarbank", Reading({"soc": soc, "max_charge": max_charge}, clock.t, UTC, 1, .02))
    live.publish("shelly", Reading({SOLARBANK_AC: 1.0, CAR: ev_w}, clock.t, UTC, 1, .03))
    return live


def build(tmp_path, clock, live, notes=None, plan_path=None, requested="shadow", factory=None, period=1.0, **extra):
    notes = notes if notes is not None else []
    utc = (lambda: UTC + (clock.t - 1000.0)) if hasattr(clock, "t") else time.time
    return loop_mod.build(live, plan_path or tmp_path / "plan.json", tmp_path / "out.json", requested=requested, factory=factory,
                          log_path=tmp_path / "commands.jsonl", period=period, notify=notes.append, monotonic=clock,
                          clock=utc, log=lambda text: None, **extra)


def test_the_status_file_reports_each_command_stage_and_never_says_the_equipment_obeyed_in_shadow(tmp_path):
    clock = Clock(); (tmp_path / "plan.json").write_text(json.dumps(plan()))
    lp = build(tmp_path, clock, live_with(clock))
    lp.cycle()
    state = json.loads((tmp_path / "out.json").read_text())
    assert state["execution"]["mode"] == "shadow" and state["execution"]["sends_commands"] is False
    assert state["commands"] and all(c["stage"] == "suppressed" and c["simulated"] is False for c in state["commands"].values())
    assert "equipment_writes" not in json.dumps(state["decision"])                  # not hard-coded into the decision any more
    assert state["plan"]["valid"] is True and state["decision"]["restrictions"]
    assert not list(tmp_path.glob("*.tmp"))


def test_the_command_log_gets_a_line_only_when_a_command_changes(tmp_path):
    clock = Clock(); (tmp_path / "plan.json").write_text(json.dumps(plan()))
    lp = build(tmp_path, clock, live_with(clock))
    for _ in range(120):
        clock.t += 1; lp.executor.transport  # steady state, readings kept fresh
        lp.live.publish("solarbank", Reading({"soc": 50, "max_charge": MAXW}, clock.t, UTC, 1, .02))
        lp.live.publish("shelly", Reading({SOLARBANK_AC: 1.0, CAR: 0.0}, clock.t, UTC, 2, .02))
        lp.cycle()
    lines = (tmp_path / "commands.jsonl").read_text().splitlines()
    assert 2 <= len(lines) < 12                                                      # changes and a couple of refreshes, not 240
    assert all(json.loads(l)["mode"] == "shadow" and json.loads(l)["stage"] == "suppressed" for l in lines)


def test_the_command_log_is_size_capped_with_one_rotation(tmp_path):
    log = loop_mod.ChangeLog(tmp_path / "c.jsonl", max_bytes=2000)
    for i in range(200):
        log.record({"i": i, "pad": "x" * 50})
    assert (tmp_path / "c.jsonl.1").exists() and (tmp_path / "c.jsonl").stat().st_size < 4000


def test_stale_device_data_becomes_unknown_not_the_last_value(tmp_path):
    clock = Clock(); lp = build(tmp_path, clock, live_with(clock, ev_w=7000))
    assert lp.cycle()[0].inputs["fresh"] == ["shelly", "solarbank"]
    clock.t += 11                                                   # older than the idle limit (the specification's STALE_S, 10 s)
    d = lp.cycle()[0]
    assert d.inputs["fresh"] == [] and d.discharge == "ALLOW" and d.charge_limit_w == 0 and "battery_unknown" in d.restrictions


def test_the_freshness_limit_is_tight_for_the_meter_while_charging_and_the_specifications_ten_seconds_otherwise(tmp_path):
    """8 November, option C, as corrected by GitHub issue 68 (D15 against D9, owner decision A, 9/10 November): the tight limit while a charge is
    under way belongs to the garage current reading alone (it sets the charge power); the Solarbank and the Shelly keep the ordinary idle_s limit
    even while charging, so a short gap in either (D15's 'up to 10 s changes nothing') no longer makes the whole device's data invalid."""
    clock = Clock()
    live = live_with(clock, ev_w=7000)
    live.publish("meter", Reading({"garage_current_a": 20.0, "voltage": 230.0}, clock.t, UTC, 1, .02))
    lp = build(tmp_path, clock, live, devices=("solarbank", "shelly", "meter"))
    assert lp.cycle()[0].inputs["fresh"] == ["meter", "shelly", "solarbank"]
    clock.t += 5
    assert lp.cycle()[0].inputs["fresh"] == ["meter", "shelly", "solarbank"]                 # idle: 5 s old is fine for every device
    lp.supervisor.charge_w = 1500.0                                                          # a charge under way
    assert lp.cycle()[0].inputs["fresh"] == ["shelly", "solarbank"]                          # only the meter reading is now too old for regulating the charge
    lp.freshness = {"charge_s": 10.0, "idle_s": 30.0}                                        # the owner relaxed both
    assert lp.cycle()[0].inputs["fresh"] == ["meter", "shelly", "solarbank"]


def test_a_short_shelly_gap_during_a_charge_changes_nothing_a_longer_one_still_invalidates(tmp_path):
    """GitHub issue 68: a Shelly (and Solarbank) gap up to 10 s during a charge no longer makes the device data invalid (D15); only a longer gap,
    past the ordinary idle_s limit, still does (D9) -- the same boundary as when idle, not the old 2.5 s while charging."""
    clock = Clock(); lp = build(tmp_path, clock, live_with(clock, ev_w=0.0))
    lp.cycle()
    lp.supervisor.charge_w = 1500.0
    clock.t += 9                                                        # a 9 s gap during a charge: under the specification's 10 s idle_s limit
    d = lp.cycle()[0]
    assert d.inputs["fresh"] == ["shelly", "solarbank"] and "battery_unknown" not in d.restrictions
    clock.t += 2                                                        # 11 s total: now stale even by the ordinary idle_s limit
    d = lp.cycle()[0]
    assert d.inputs["fresh"] == [] and "battery_unknown" in d.restrictions


def test_the_two_new_validity_checks_raise_and_clear_an_alert_file(tmp_path):
    """GitHub issue 69, with an email alert (owner decision A): the Supervisor only tags; this is the one place (the control process itself, which
    already has outbound-network-free access to the alert directory, same as the machine alerts) that raises or clears the alert file for it."""
    clock = Clock()
    live = live_with(clock)
    live.publish("meter", Reading({"garage_current_a": 20.0, "voltage": 206.0}, clock.t, UTC, 1, .02))     # out of the 207-253 V range
    alert_dir = tmp_path / "alerts"
    lp = build(tmp_path, clock, live, devices=("solarbank", "shelly", "meter"), alert_dir=str(alert_dir))
    lp.cycle()
    assert (alert_dir / "meter-voltage.json").exists()
    live.publish("meter", Reading({"garage_current_a": 20.0, "voltage": 230.0}, clock.t, UTC, 2, .02))
    lp.cycle()
    assert not (alert_dir / "meter-voltage.json").exists()


# ===== GitHub issue 67: the Octopus SMART dispatches reach the controller for SMARTSLOT (specification 6.2, owner decision A) ======================
def test_read_tariff_passes_dispatches_through_only_on_a_genuinely_new_fetch(tmp_path):
    clock = Clock(); lp = build(tmp_path, clock, live_with(clock))
    feed = tmp_path / "tariff.json"
    assert lp._read_tariff(UTC) is None                                          # no feed file yet: nothing to say, same as a missing device
    feed.write_text(json.dumps({"dispatches": [[UTC - 60, UTC + 3600, "SMART"]], "fetched_at": UTC}))
    lp.dispatch_feed_path = feed
    seen = lp._read_tariff(UTC)
    assert seen["value"]["dispatches"] == [[UTC - 60, UTC + 3600, "SMART"]]
    again = lp._read_tariff(UTC + 1)                                             # the same fetch, read a second time: nothing new to report
    assert "dispatches" not in again["value"]
    feed.write_text(json.dumps({"dispatches": [], "fetched_at": UTC + 60}))      # a later, genuinely new fetch (even an empty answer)
    fresh = lp._read_tariff(UTC + 60)
    assert fresh["value"]["dispatches"] == []
    feed.write_text("not json")
    assert lp._read_tariff(UTC + 120) is None


def test_the_controller_charges_in_a_real_smart_slot_the_clock_window_misses(tmp_path):
    """GitHub issue 67's own live example: a SMART dispatch outside the 23:30-05:30 window, with the car charging, must still charge the battery."""
    clock = Clock()
    live = live_with(clock, ev_w=7000)                                          # the car is charging; UTC (08:00 London) is an expensive clock period
    feed = tmp_path / "tariff.json"
    feed.write_text(json.dumps({"dispatches": [[UTC - 60, UTC + 3600, "SMART"]], "fetched_at": UTC}))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"mode": "shadow", "action": "charge_to", "target_soc": .9, "issued_at": UTC - 5, "valid_until": UTC + 240}))
    lp = build(tmp_path, clock, live, plan_path=plan_path, dispatch_feed_path=str(feed))
    for _ in range(5):                                                         # EV_ON_DWELL_S: the car signal needs a few seconds to confirm "on"
        d, _ = lp.cycle()
        clock.t += 1
    assert d.economic == "charge_to"                                            # SMARTSLOT made CHEAP true; the clock window alone never would


def test_the_controller_does_not_charge_in_a_listed_smart_slot_while_the_car_is_not_charging(tmp_path):
    """15 November 2026: a listed slot is billed cheap only when Octopus's charger supplies power in it. On 13 and 14 November the battery charged in listed slots with
    the car idle (13 November 15:30, 14 November 06:30, 07:30 and 23:00) and every one of those half-hours was billed at the standard rate."""
    clock = Clock()
    live = live_with(clock, ev_w=0)                                             # the car is not drawing; UTC (08:00 London) is an expensive clock period
    feed = tmp_path / "tariff.json"
    feed.write_text(json.dumps({"dispatches": [[UTC - 60, UTC + 3600, "SMART"]], "fetched_at": UTC}))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"mode": "shadow", "action": "charge_to", "target_soc": .9, "issued_at": UTC - 5, "valid_until": UTC + 240}))
    lp = build(tmp_path, clock, live, plan_path=plan_path, dispatch_feed_path=str(feed))
    for _ in range(40):
        d, _ = lp.cycle()
        clock.t += 1
    assert d.economic != "charge_to" and lp.supervisor.smart.value is False     # the slot is listed and the plan has a window, but nothing is billed cheap yet


@pytest.mark.parametrize("age_s", [400, 3600])
def test_a_controller_that_starts_on_an_old_dispatch_file_does_not_start_a_slot_from_it(tmp_path, age_s):
    """Second peer review: the providers service may have been down for hours while the file stayed in /run. Specification 6.2: a slot may start only
    on a fetch no older than 300 s, counted from the fetch itself, not from when this controller first read the file."""
    clock = Clock()
    feed = tmp_path / "tariff.json"
    feed.write_text(json.dumps({"dispatches": [[UTC - 7200, UTC + 3600, "SMART"]], "fetched_at": UTC - age_s}))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"mode": "shadow", "action": "charge_to", "target_soc": .9, "issued_at": UTC - 5, "valid_until": UTC + 240}))
    lp = build(tmp_path, clock, live_with(clock, ev_w=7000), plan_path=plan_path, dispatch_feed_path=str(feed))
    for _ in range(6):
        d, _ = lp.cycle()
        clock.t += 1
    assert d.economic != "charge_to" and lp.supervisor.smart.value is False


def test_the_slot_allowance_option_is_accepted_and_nothing_is_counted_or_saved(tmp_path):
    """GitHub issue 79: no allowance is counted. The installed unit still passes --slot-allowance, so the option is accepted and ignored."""
    clock = Clock()
    path = tmp_path / "smart-slot-allowance.json"
    feed = tmp_path / "tariff.json"
    feed.write_text(json.dumps({"dispatches": [[UTC - 60, UTC + 3600, "SMART"]], "fetched_at": UTC}))
    lp = build(tmp_path, clock, live_with(clock, ev_w=7000), slot_allowance_path=str(path), dispatch_feed_path=str(feed))
    for _ in range(6):
        lp.cycle(); clock.t += 1
    assert lp.supervisor.smart.value is True and not path.exists()
    import inspect
    from solarbank_optimiser import control_loop
    assert '"--slot-allowance"' in inspect.getsource(control_loop.main)          # still declared, so the installed unit's command line still parses


@pytest.mark.parametrize("tariff_value", [
    {"fetch_ok": False},                                 # a failed fetch
    {"dispatches": []},                                  # an empty list
    {"dispatches": [(UTC - 60, UTC + 3600, "BOOST")]},   # a dispatch of another type
])
def test_these_tariff_shapes_never_make_cheap_true_outside_the_clock_window(tariff_value):
    sup = Supervisor()
    inputs = fresh(soc=50, ev_w=7000, current=20)
    inputs["tariff"] = {"value": tariff_value, "age": 0.0}
    d = sup.decide(inputs, plan(target=0.9), None, 0.0, utc=UTC)
    assert d.economic != "charge_to"


# ---- timing: every stage counts, and the watchdog follows whole cycles -------------------------------------------------------------
DELAY = 0.25


@pytest.mark.parametrize("stage", ["inputs", "decide", "prepare", "execute", "output", "log"])
def test_a_delay_in_any_stage_is_measured_in_the_cycle_and_withholds_the_heartbeat(tmp_path, stage, monkeypatch):
    notes = []
    lp = build(tmp_path, time.monotonic, live_with(Clock()), notes=notes, period=0.05)
    lp.clock = time.time
    lp.live.publish("solarbank", Reading({"soc": 50, "max_charge": MAXW}, time.monotonic(), UTC, 1, .02))
    lp.live.publish("shelly", Reading({SOLARBANK_AC: 1.0, CAR: 0.0}, time.monotonic(), UTC, 1, .02))
    slow = lambda fn: (lambda *a, **k: (time.sleep(DELAY), fn(*a, **k))[1])
    if stage == "inputs":
        monkeypatch.setattr(loop_mod, "control_tick", slow(loop_mod.control_tick))
    elif stage == "decide":
        lp.supervisor.decide = slow(lp.supervisor.decide)
    elif stage == "prepare":
        lp.preparer.prepare = slow(lp.preparer.prepare)
    elif stage == "execute":
        lp.executor.execute = slow(lp.executor.execute)
    elif stage == "output":
        monkeypatch.setattr(loop_mod, "write_atomic", slow(loop_mod.write_atomic))
    elif stage == "log":
        lp.changelog.record = slow(lp.changelog.record)
    started = time.monotonic()
    _, total = lp.cycle()
    assert total >= DELAY * .9 and time.monotonic() - started >= DELAY * .9
    assert lp.late_cycles >= 1 and lp.prev_total_ms >= DELAY * 900 and lp.worst_total_ms >= DELAY * 900
    assert lp.healthy_cycle(total) is False                                                    # 0.25 s > 3 x 0.05 s
    lp.cycle()
    state = json.loads((tmp_path / "out.json").read_text())
    assert state["timing_ms"]["previous_total"] >= DELAY * 900 and state["late_cycles"] >= 1


def test_the_heartbeat_follows_prompt_cycles_only_and_a_stalled_loop_sends_none(tmp_path):
    notes = []
    lp = build(tmp_path, time.monotonic, live_with(Clock()), notes=notes, period=0.2)       # "prompt" means under 0.6 s
    lp.clock = time.time
    stop, stalled, release = threading.Event(), threading.Event(), threading.Event()
    real, count = lp.cycle, {"n": 0}

    def cycle():
        count["n"] += 1
        if count["n"] == 3:
            stalled.set()
            release.wait(10)                      # a complete stall: this cycle does not return
        out = real()
        if count["n"] == 6:
            stop.set()
        return out
    lp.cycle = cycle
    worker = threading.Thread(target=lp.run, args=(stop,), daemon=True); worker.start()
    assert stalled.wait(10)
    time.sleep(0.3)
    assert notes.count("WATCHDOG=1") == 2          # one beat per completed cycle; none while stalled (systemd would restart it here)
    release.set(); worker.join(10)
    assert notes[0] == "READY=1" and count["n"] >= 6 and notes.count("WATCHDOG=1") == 6      # beating again once the stall ends


def test_missing_equipment_data_gives_the_restricted_decision_and_keeps_the_heartbeat(tmp_path):
    notes = []; clock = Clock()
    lp = build(tmp_path, clock, LiveState(), notes=notes)                     # no device has ever answered
    stop = threading.Event(); stop.wait = lambda delay: None
    lp.run(stop, count=5)
    assert notes.count("WATCHDOG=1") == 5                                      # restarting would not mend a dead sensor
    d = lp.last_decision
    assert d.discharge == "ALLOW" and d.charge_limit_w == 0 and d.inputs["fresh"] == [] and "battery_unknown" in d.restrictions
    state = json.loads((tmp_path / "out.json").read_text())
    assert state["decision"]["inputs"]["unknown"] == ["solarbank", "shelly", "meter"]


def test_a_decision_bug_restricts_everything_and_the_controller_keeps_running(tmp_path):
    clock = Clock(); lp = build(tmp_path, clock, live_with(clock))
    lp.supervisor.decide = lambda *a, **k: (_ for _ in ()).throw(KeyError("boom"))
    d, _ = lp.cycle()
    assert d.discharge == "INHIBIT" and d.charge_limit_w == 0 and "controller_fault" in d.restrictions
    actions = {c["action"] for c in json.loads((tmp_path / "out.json").read_text())["commands"].values()}
    assert actions == {"STOP", "INHIBIT"}


# ---- ownership and restart ------------------------------------------------------------------------------------------------------
def test_a_second_controller_is_refused_before_any_equipment_connection_or_output_file(tmp_path):
    lock = tmp_path / "run" / "control.lock"
    out = tmp_path / "run" / "out.json"
    opened = []
    with SingleOwner(lock):
        code = loop_mod.main(["--lock", str(lock), "--out", str(out), "--mode-file", str(tmp_path / "mode"), "--log", str(tmp_path / "log")],
                             acquirer_factory=lambda devices, hosts: opened.append("equipment") or (_ for _ in ()).throw(AssertionError("opened")))
    assert code == 3 and opened == [] and not out.exists()
    assert not (tmp_path / "log").exists() and not (tmp_path / "mode").exists()


def test_the_meter_is_refused_while_the_original_monitor_owns_it(tmp_path):
    opened = []
    code = loop_mod.main(["--lock", str(tmp_path / "l"), "--out", str(tmp_path / "o"), "--devices", "solarbank,meter,shelly"],
                         acquirer_factory=lambda *a: opened.append(1))
    assert code == 2 and opened == [] and not (tmp_path / "l").exists()


def test_the_lock_is_released_when_the_controller_stops(tmp_path, monkeypatch):
    for key in ("SOLARBANK_HOST", "SOLARBANK_SHELLY_HOST"):
        monkeypatch.setenv(key, "127.0.0.1")
    monkeypatch.setenv("SOLARBANK_DEVICES_FILE", str(tmp_path / "none.conf"))
    class Fake:
        live = LiveState()
        def start(self): return self
        def stop(self): pass
    stopper = {}
    real_run = loop_mod.ControlLoop.run
    loop_mod.ControlLoop.run = lambda self, stop, count=None: real_run(self, stop, count=2)
    try:
        args = ["--lock", str(tmp_path / "l"), "--out", str(tmp_path / "o.json"), "--plan", str(tmp_path / "p.json"),
                "--mode-file", str(tmp_path / "m"), "--log", str(tmp_path / "log")]
        assert loop_mod.main(args, acquirer_factory=lambda d, h: Fake()) == 0
        assert loop_mod.main(args, acquirer_factory=lambda d, h: Fake()) == 0                  # the lock was free again
    finally:
        loop_mod.ControlLoop.run = real_run


def test_restart_under_a_restriction_starts_restricted_and_replays_nothing(tmp_path):
    clock = Clock(); (tmp_path / "plan.json").write_text(json.dumps(plan()))
    first = build(tmp_path, clock, live_with(clock, ev_w=7000))
    for _ in range(3):
        clock.t += 1
        first.live.publish("solarbank", Reading({"soc": 50, "max_charge": MAXW}, clock.t, UTC, 1, .02))
        first.live.publish("shelly", Reading({SOLARBANK_AC: 1.0, CAR: 7000.0}, clock.t, UTC, 1, .02))
        d, _ = first.cycle()
    second = build(tmp_path, clock, live_with(clock, ev_w=0.0))                 # a "restart": new process state, car now reads idle
    d, _ = second.cycle()
    assert d.charge_limit_w == 0                                                    # charging restricted from the very first cycle (no garage reading here)
    assert second.run_id != first.run_id and second.executor.pending == [] and second.cycles == 1
    assert all(r["id"].startswith(second.run_id) for r in second.executor.records.values())


# ---- the mode switch ------------------------------------------------------------------------------------------------------------
def test_live_is_refused_fail_closed_until_a_commissioned_transport_exists(tmp_path):
    clock = Clock()
    for requested in ("live", "LIVE\n", "yes please", "", None):
        lp = build(tmp_path, clock, live_with(clock), requested=requested)
        assert lp.mode == "shadow" and lp.executor.transport.sends is False
    lp = build(tmp_path, clock, live_with(clock), requested="live")
    lp.cycle()
    state = json.loads((tmp_path / "out.json").read_text())
    assert state["execution"]["requested"] == "live" and state["execution"]["mode"] == "shadow" and "staying in shadow" in state["execution"]["note"]
    assert loop_mod.LIVE_TRANSPORT_FACTORY is None


def test_the_mode_file_is_read_safely(tmp_path):
    f = tmp_path / "mode"
    assert loop_mod.read_mode_file(f) == "shadow"
    f.write_text(""); assert loop_mod.read_mode_file(f) == "shadow"
    f.write_text("live\n# comment"); assert loop_mod.read_mode_file(f) == "live"
    f.write_bytes(b"\xff\xfe"); assert loop_mod.read_mode_file(f) == "shadow"


def test_with_a_transport_available_live_mode_prepares_exactly_what_shadow_prepares(tmp_path):
    results = {}
    for requested, factory in (("shadow", None), ("live", lambda: Scripted([Outcome("acknowledged")]))):
        clock = Clock(); (tmp_path / "plan.json").write_text(json.dumps(plan()))
        lp = build(tmp_path, clock, live_with(clock), requested=requested, factory=factory)
        decisions, prepared = [], []
        for _ in range(40):
            clock.t += 1
            lp.live.publish("solarbank", Reading({"soc": 50, "max_charge": MAXW}, clock.t, UTC, 1, .02))
            lp.live.publish("shelly", Reading({SOLARBANK_AC: 1.0, CAR: 0.0}, clock.t, UTC, 1, .02))
            d, _ = lp.cycle(); decisions.append(d.as_dict())
            prepared += [r for r in lp.executor.records.values()]
        results[requested] = (lp.mode, decisions, sorted((r["channel"], r["action"], str(r["params"])) for r in lp.executor.records.values()))
    assert results["shadow"][0] == "shadow" and results["live"][0] == "live"
    assert results["shadow"][1] == results["live"][1] and results["shadow"][2] == results["live"][2]


# ---- nothing here can command equipment on its own, and the imports stay small --------------------------------------------------------
def test_no_write_names_sockets_or_network_code_in_the_controller_modules():
    for module in (cl, ce, loop_mod):
        source = Path(module.__file__).read_text()
        for forbidden in ("write_register", "write_registers", "socket.", "http.client", "urllib", "requests", "ModbusTcpClient", "import sqlite3"):
            assert forbidden not in source, (module.__name__, forbidden)


def test_importing_the_controller_pulls_in_no_database_web_solver_or_data_science_code():
    code = ("import sys; import solarbank_optimiser.control_loop; "
            "bad=[m for m in sys.modules if m.split('.')[0] in ('sqlite3','fastapi','starlette','pydantic','uvicorn','numpy','pandas','scipy','h5py','pvlib','requests','jinja2','httpx') "
            "or m in ('solarbank_optimiser.store','solarbank_optimiser.collector','solarbank_optimiser.planning','solarbank_optimiser.web','solarbank_optimiser.services')]; "
            "print(bad)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                            env={"PYTHONPATH": str(Path(cl.__file__).resolve().parents[1]), "PATH": ""})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout


def test_the_unit_is_a_notify_service_with_a_watchdog_a_mode_file_and_only_device_addresses():
    unit = (Path(cl.__file__).resolve().parents[2] / "deployment/solarbank-optimiser-control.service").read_text()
    assert "Type=notify" in unit and "WatchdogSec=" in unit and "NotifyAccess=main" in unit and "RestartPreventExitStatus=2 3" in unit
    assert "IPAddressDeny=any" in unit and "localhost" not in unit.split("IPAddressAllow=")[1].splitlines()[0]
    assert "write" not in unit.lower().replace("pythondontwritebytecode", "").replace("readwritepaths", "")
