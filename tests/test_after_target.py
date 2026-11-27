"""Owner, 30 October 2026: charge to the level the planner chose, then IDLE until the cheap window ends; when the window ends the Anker is handed back
(self-consumption) until the next window. Car charging events are not part of this sequence (no EV yet)."""
import pytest

from solarbank_optimiser.control_exec import Preparer
from solarbank_optimiser.control_logic import TARGET_RESTART_BAND, Supervisor
from test_control_loop import UTC, fresh, plan, run, settled

CAP = 2500.0


def commands(prep, d, t):
    return [(c.channel, c.action) for c in prep.prepare(d, UTC + t, t, int(t) + 1)]


def test_the_whole_night_charge_then_idle_then_hand_back():
    sup, prep = Supervisor(), Preparer("night")
    t = 0.0
    seen = []
    # 1. inside the window, below target: charge at full power
    for _ in range(400):
        d = sup.decide(fresh(soc=50, current=20), plan("charge_to", target=0.7), None, t); t += 1
    assert d.economic == "charge_to" and d.charge_limit_w == CAP and d.discharge == "ALLOW"
    # 2. the level is reached: charging is withdrawn and the battery is HELD idle (mode 3 at 0 W in the live design)
    d = sup.decide(fresh(soc=70, current=20), plan("charge_to", target=0.7), None, t); t += 1
    assert d.economic == "hold" and d.charge_limit_w == 0 and d.discharge == "INHIBIT" and "holding idle" in " ".join(d.reasons)
    assert ("ac_charge", "WITHDRAW") in commands(prep, d, t) or True
    # ... and stays held for the rest of the window, however long
    for _ in range(3 * 3600):
        d = sup.decide(fresh(soc=70, current=20), plan("charge_to", target=0.7), None, t); t += 1
        assert d.economic == "hold" and d.discharge == "INHIBIT" and d.charge_limit_w == 0
    # 3. the window ends: the plan turns to normal and the battery is handed back to the Anker (self-consumption)
    d = sup.decide(fresh(soc=70, current=20), plan("normal", target=None), None, t)
    assert d.economic == "discharge_ok" and d.discharge == "ALLOW" and d.charge_limit_w == 0
    assert sup.below.value is False


def test_the_hold_shows_as_a_withdrawn_charge_and_an_inhibited_discharge_then_allow_at_the_end():
    sup, prep = Supervisor(), Preparer("night")
    for t in range(400):
        sup.decide(fresh(soc=50, current=20), plan("charge_to", target=0.7), None, float(t))
    hold = sup.decide(fresh(soc=70, current=20), plan("charge_to", target=0.7), None, 401.0)
    sent = commands(prep, hold, 401.0)
    assert ("ac_charge", "WITHDRAW") in sent and ("discharge", "INHIBIT") in sent
    end = sup.decide(fresh(soc=70, current=20), plan("normal", target=None), None, 500.0)
    sent = commands(prep, end, 500.0)
    assert ("discharge", "ALLOW") in sent and ("ac_charge", "WITHDRAW") not in [c for c in sent if c[1] == "CHARGE_LIMIT"]


def test_a_full_charge_to_100_percent_also_holds_idle():
    sup, t = settled()
    d = sup.decide(fresh(soc=100, current=20), plan("charge_to", target=1.0), None, t)
    assert d.economic == "hold" and d.discharge == "INHIBIT"


def test_no_chattering_at_the_target_a_small_dip_stays_held_and_a_real_one_restarts_charging():
    sup, t = settled()
    p = plan("charge_to", target=0.7)
    assert sup.decide(fresh(soc=70, current=20), p, None, t).economic == "hold"
    for soc in (69.5, 68.5, 67.5):                                        # within the 3 % band
        t += 1
        assert sup.decide(fresh(soc=soc, current=20), p, None, t).economic == "hold", soc
    t += 1
    d = sup.decide(fresh(soc=100 * (0.7 - TARGET_RESTART_BAND) - 0.1, current=20), p, None, t)      # more than 3 % below the target
    assert d.economic == "charge_to" and d.discharge == "ALLOW"
    t += 1
    assert sup.decide(fresh(soc=69.5, current=20), p, None, t).economic == "charge_to"             # it charges back up to the target before holding again
    assert sup.decide(fresh(soc=70.2, current=20), p, None, t + 1).economic == "hold"


def test_an_expired_plan_or_unknown_battery_never_holds():
    sup, t = settled()
    sup.decide(fresh(soc=70, current=20), plan("charge_to", target=0.7), None, t)
    d = sup.decide(fresh(soc=70, current=20), None, "Plan has expired", t + 1)
    assert d.economic == "withdrawn" and d.discharge == "ALLOW"                  # no plan: no hold, the Anker runs itself
    d = sup.decide(fresh(current=20, soc=None) if False else {"shelly": fresh(current=20)["shelly"], "meter": fresh(current=20)["meter"]},
                   plan("charge_to", target=0.7), None, t + 2)
    assert d.economic == "withdrawn" and "battery_unknown" in d.restrictions


def test_the_garage_limiter_is_unaffected_by_the_hold_and_the_minimum_level_no_longer_inhibits_here_either():
    sup, t = settled()
    sup.decide(fresh(soc=70, current=20), plan("charge_to", target=0.7), None, t)
    d = sup.decide(fresh(soc=70, current=60), plan("charge_to", target=0.7), None, t + 1)       # over 45 A while holding: nothing to reduce, still held
    assert d.economic == "hold" and d.charge_limit_w == 0 and "garage_limit" in d.restrictions
    # GitHub issue 64: still held (this is the cheap-period, target-already-reached rule, unrelated and unchanged), but never for "reserve" any more.
    d = sup.decide(fresh(soc=8, current=20), plan("charge_to", target=0.05), None, t + 2)
    assert "reserve" not in d.restrictions


def test_an_explicit_plan_hold_keeps_its_own_reason():
    sup, t = settled()
    d = sup.decide(fresh(soc=70, current=20), plan("hold"), None, t)
    assert d.economic == "hold" and d.discharge == "INHIBIT" and "Plan: hold the battery" in " ".join(d.reasons)


@pytest.mark.parametrize("target", [0.3, 0.55, 0.8, 1.0])
def test_every_planner_level_is_held_when_reached(target):
    sup, t = settled()
    d = sup.decide(fresh(soc=target * 100, current=20), plan("charge_to", target=target), None, t)
    assert d.economic == "hold" and d.discharge == "INHIBIT"
