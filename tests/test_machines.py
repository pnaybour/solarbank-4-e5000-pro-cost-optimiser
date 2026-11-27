"""WP11: the state-machine helper (machines.py) and the command lifecycle built on it."""
import logging

import pytest

from solarbank_optimiser import control_exec as ce
from solarbank_optimiser import machines as m
from solarbank_optimiser.control_exec import Executor, Outcome
from test_control_loop import Scripted, cmd, UTC

SPEC = m.Spec("toy", ("idle", "armed", "fired"), "idle", (
    ("arm", "idle", "armed"),
    ("fire_it", "armed", "fired"),
    ("settle", "armed", "idle", m.dwell_at_least(30)),
    ("explode", "armed", "fired", lambda event: 1 / 0),
))


def toy():
    return m.build(SPEC, type("Toy", (), {})(), now=0.0)


def test_a_declared_event_changes_the_state_and_is_logged_in_the_change_list():
    t = toy()
    assert m.fire(t, "arm", now=1.0) is True and t.state == "armed"
    assert t.changes == [{"trigger": "arm", "from": "idle", "to": "armed", "now": 1.0}]


def test_an_undeclared_event_name_is_refused_counted_and_leaves_the_state(caplog):
    t = toy(); before = m.counters("toy").unknown_event
    with caplog.at_level(logging.ERROR):
        assert m.fire(t, "to_fired") is False and m.fire(t, "typo") is False       # auto-transitions are off: to_fired does not exist
    assert t.state == "idle" and m.counters("toy").unknown_event == before + 2 and "undeclared event" in caplog.text


def test_a_declared_event_not_allowed_from_this_state_is_refused_counted_and_leaves_the_state():
    t = toy(); before = m.counters("toy").not_allowed
    assert m.fire(t, "fire_it") is False and t.state == "idle" and m.counters("toy").not_allowed == before + 1


def test_a_false_guard_refuses_the_transition_and_is_counted_separately():
    t = toy(); m.fire(t, "arm", now=0.0); before = m.counters("toy").guard_false
    assert m.fire(t, "settle", now=10.0) is False and t.state == "armed" and m.counters("toy").guard_false == before + 1


def test_a_dwell_timer_runs_on_the_callers_clock_not_a_thread():
    t = toy(); m.fire(t, "arm", now=100.0)
    assert m.fire(t, "settle", now=129.9) is False
    assert m.fire(t, "settle", now=130.0) is True and t.state == "idle" and t.entered_at == 130.0


def test_an_exception_inside_a_guard_is_not_swallowed():
    """Only the two declared refusals are caught; a programming error in a guard must surface, not disappear."""
    t = toy(); m.fire(t, "arm", now=0.0)
    with pytest.raises(ZeroDivisionError):
        m.fire(t, "explode", now=1.0)


def test_events_fired_inside_a_callback_are_queued_until_the_transition_completes():
    t = toy(); seen = []
    t.machine.get_transitions("arm")[0].after.append(lambda event: seen.append((event.model.state, m.fire(event.model, "fire_it"))))
    m.fire(t, "arm", now=0.0)
    assert seen == [("armed", None)] and t.state == "fired"


def test_the_diagram_is_generated_from_the_same_table():
    text = m.diagram(SPEC)
    assert text.startswith("%% toy\nstateDiagram-v2\n    [*] --> idle\n") and "    idle --> armed: arm" in text and "    armed --> idle: settle" in text
    assert all(f": {event}" in text for event in SPEC.events())
    lifecycle = m.diagram(ce.LIFECYCLE)
    assert "acknowledged --> observed: observe" in lifecycle and "prepared --> superseded: supersede" in lifecycle


# ---- the command lifecycle ----------------------------------------------------------------------------------------------------------------
def test_a_finished_command_cannot_be_moved_again_by_a_late_acknowledgement_or_observation():
    ex = Executor(Scripted([Outcome("rejected", "no")]), effect=lambda r, i: True)
    ex.execute([cmd(seq=1)], {}, UTC, 0.0)
    record = ex.records["c1"]; assert record["stage"] == "rejected"
    assert ex._move(record, "acknowledge", UTC + 1) is False and ex._move(record, "observe", UTC + 1) is False
    assert record["stage"] == "rejected"


def test_a_superseded_commands_acknowledgement_never_confirms_its_replacement():
    ex = Executor(Scripted([Outcome("timeout")]), effect=lambda r, i: True, max_attempts=5)
    ex.execute([cmd("ac_charge", "STOP", "protective", seq=1, cycle=1)], {}, UTC, 0.0)
    ex.execute([cmd("ac_charge", "STOP", "protective", seq=2, cycle=2)], {}, UTC + 1, 1.0)
    assert ex.records["c1"]["stage"] == "superseded"
    ex.transport.script = [Outcome("acknowledged")]
    ex.execute([], {}, UTC + 2, 2.0)
    assert ex.records["c1"]["stage"] == "superseded" and ex.records["c2"]["stage"] == "observed" and ex.control_verified()


def test_success_on_one_channel_never_clears_a_failure_on_another():
    ex = Executor(Scripted([Outcome("rejected", "no"), Outcome("acknowledged")]), effect=lambda r, i: True)
    ex.execute([cmd("ac_charge", "STOP", "protective", seq=1, cycle=1), cmd("discharge", "INHIBIT", "protective", seq=2, cycle=1)], {}, UTC, 0.0)
    assert ex.records["c1"]["stage"] == "rejected" and ex.records["c2"]["stage"] == "observed"
    assert ex.control_verified() is False                 # the confirmed INHIBIT (newest) does not excuse the refused STOP


def test_a_refused_stop_is_cleared_by_a_newer_confirmed_restriction_on_the_same_channel_only():
    ex = Executor(Scripted([Outcome("rejected", "no")]), effect=lambda r, i: True)
    ex.execute([cmd("ac_charge", "STOP", "protective", seq=1, cycle=1)], {}, UTC, 0.0)
    assert ex.control_verified() is False
    ex.transport.script = [Outcome("acknowledged")]
    ex.execute([cmd("ac_charge", "CHARGE_LIMIT", "economic", seq=2, cycle=2, watts=500, target_soc=.8)], {}, UTC + 1, 1.0)
    assert ex.control_verified() is False                 # a charge request is not a restriction
    ex.execute([cmd("ac_charge", "WITHDRAW", "economic", seq=3, cycle=3)], {}, UTC + 2, 2.0)
    assert ex.control_verified() is True                  # a confirmed withdraw shows the channel obeys: no deadlock
