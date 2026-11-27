"""WP7 (tests only): the three behaviours behind issues #2, #3 and #5, run through the whole controller path (Supervisor, Preparer) at the London clock.

On the shadow release 0.5.36 the plan decided whether it was cheap, so a target that moved, a plan re-published at an interval boundary or a battery that was full
early each let the Anker discharge inside the cheap window. Since WP24 the clock decides (owner specification v1.3 section 7), so these hold by construction;
the tests keep it that way."""
from datetime import datetime

import pytest

from solarbank_optimiser.control_exec import Preparer
from solarbank_optimiser.control_logic import Supervisor
from solarbank_optimiser.energy import LONDON
from test_control_loop import CAP, fresh, plan


def london(hour, minute=0, second=0, day=15, month=1, year=2027):
    return datetime(year, month, day, hour, minute, second, tzinfo=LONDON).timestamp()


def drive(steps, start_utc):
    """steps: (seconds, soc, plan or None). Returns every decision with its time and the discharge commands the Preparer would have issued."""
    sup, prep = Supervisor(), Preparer("w")
    out, t = [], 0.0
    for seconds, soc, p in steps:
        for _ in range(seconds):
            utc = start_utc + t
            d = sup.decide(fresh(soc=soc(t) if callable(soc) else soc, current=20), p(utc) if callable(p) else p, None if p else "Plan has expired", t, utc=utc)
            sent = [(c.channel, c.action) for c in prep.prepare(d, utc, t, int(t) + 1)]
            out.append((utc, d, sent)); t += 1
    return out


def test_2_a_target_that_moves_while_the_battery_rises_never_lets_discharge_through():
    """31 October: 65, 70, 65, 75, 70 then 55 % within two hours, the battery climbing from 40 %."""
    targets = [0.65, 0.70, 0.65, 0.75, 0.70, 0.55]
    start = london(0, 0)
    steps = [(600, (lambda t, k=k: min(0.40 * 100 + t / 36.0, 99.0)), plan("charge_to", target=target)) for k, target in enumerate(targets)]
    results = drive(steps, start)
    # discharge is permitted only while grid charging is actually under way (mode 3 with a negative set-point cannot discharge); never while the battery is held
    assert all(d.discharge == "INHIBIT" or d.charge_limit_w > 0 for _, d, _ in results), "discharge was allowed inside the cheap window"
    assert not any(("discharge", "ALLOW") in sent and d.charge_limit_w == 0 for _, d, sent in results)
    assert any(d.discharge == "INHIBIT" for _, d, _ in results)                      # and it is held once a target is reached


def test_3_a_plan_republished_at_an_interval_boundary_gives_no_allow():
    """A new plan whose window starts after its issue time (or is split at a row boundary) used to put the controller briefly 'outside' the window."""
    start = london(2, 59, 0)
    inside = plan("charge_to", target=0.7)
    outside = plan("normal", target=None)                       # the freshly published plan: not yet in its own window
    results = drive([(30, 70.0, inside), (40, 70.0, outside), (40, 70.0, None), (60, 70.0, inside)], start)
    assert all(d.discharge == "INHIBIT" for _, d, _ in results)
    assert not any(("discharge", "ALLOW") in sent for _, _, sent in results)


def test_5_a_battery_full_before_the_end_of_a_100_percent_window_is_held_at_zero_to_the_window_end():
    start = london(3, 0, 0)
    held = drive([(3600, 100.0, plan("charge_to", target=1.0))], start)             # full at 03:00; the window runs to 05:30
    assert all(d.discharge == "INHIBIT" and d.charge_limit_w == 0 for _, d, _ in held[60:])
    late = drive([(120, 100.0, plan("normal", target=None))], london(5, 29, 0))     # the clock reaches 05:30 and the Anker takes over
    assert late[0][1].discharge == "INHIBIT" and late[-1][1].discharge == "ALLOW"
    switch = next(i for i, (_, d, _) in enumerate(late) if d.discharge == "ALLOW")
    assert 60 <= switch <= 62                                                       # at 05:30:00, not before
