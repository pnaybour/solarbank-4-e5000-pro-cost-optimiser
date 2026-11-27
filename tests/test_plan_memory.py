"""31 Oct 2026, 00:05: the plan file expired in the middle of a charge because the planner stalled. The controller now keeps the last valid plan in
memory and follows its charging window (bounded) instead of withdrawing a charge that is under way."""
import json

from solarbank_optimiser.control_logic import PLAN_HOLD_S, PlanMemory, Supervisor, read_plan
from test_control_loop import fresh, settled

START, END, ISSUED = 1_000_000.0, 1_000_000.0 + 6 * 3600, 1_000_000.0 + 100


def write(path, **changes):
    document = {"mode": "shadow", "action": "charge_to", "target_soc": 0.75, "issued_at": ISSUED, "valid_until": ISSUED + 300,
                "window": {"start": START, "end": END, "target_soc": 0.75}, "min_soc": 0.1} | changes
    path.write_text(json.dumps(document))


def tick(memory, path, now):
    return memory.resolve(*read_plan(path, now), now)


def test_a_fresh_plan_passes_through_unchanged(tmp_path):
    path = tmp_path / "plan.json"
    write(path)
    plan, note = tick(PlanMemory(), path, ISSUED + 10)
    assert note is None and plan["action"] == "charge_to" and "held" not in plan


def test_an_expired_plan_inside_its_window_is_followed_until_the_window_ends(tmp_path):
    path, memory = tmp_path / "plan.json", PlanMemory()
    write(path)
    tick(memory, path, ISSUED + 10)
    plan, note = tick(memory, path, ISSUED + 301)                    # the planner has not renewed the file
    assert plan["held"] and plan["action"] == "charge_to" and plan["target_soc"] == 0.75 and plan["min_soc"] == 0.1
    assert "planner silent" in note and "Plan has expired" in note


def test_the_hold_is_bounded_by_the_hold_time_and_by_the_window_end(tmp_path):
    path, memory = tmp_path / "plan.json", PlanMemory()
    write(path)
    tick(memory, path, ISSUED + 10)
    assert tick(memory, path, ISSUED + 300 + PLAN_HOLD_S - 1)[0]["held"]
    plan, note = tick(memory, path, ISSUED + 300 + PLAN_HOLD_S)      # too long without a planner: the old withdrawal rule applies
    assert plan is None and note == "Plan has expired"
    write(path, valid_until=END - 100)
    tick(memory, path, ISSUED + 10)
    assert tick(memory, path, END + 1)[0] is None                    # the window is over


def test_nothing_is_held_before_the_window_starts_or_without_a_plan_ever_seen(tmp_path):
    path = tmp_path / "plan.json"
    assert tick(PlanMemory(), path, START + 10) == (None, "No plan file yet")          # a restarted controller starts with nothing
    memory = PlanMemory()
    write(path, issued_at=START - 1000, valid_until=START - 700)
    tick(memory, path, START - 990)
    assert tick(memory, path, START - 600)[0] is None                # outside the window there is nothing to continue


def test_a_deleted_plan_file_is_held_over_but_a_corrupt_one_is_not(tmp_path):
    path, memory = tmp_path / "plan.json", PlanMemory()
    write(path)
    tick(memory, path, ISSUED + 10)
    path.unlink()
    assert tick(memory, path, ISSUED + 400)[0]["held"]
    path.write_text("{not json")
    assert tick(memory, path, ISSUED + 401) == (None, "Plan file is not a valid plan")


def test_a_hand_over_or_a_plan_without_a_window_is_never_held(tmp_path):
    path, memory = tmp_path / "plan.json", PlanMemory()
    write(path, mode="anker_manual", action="hold", target_soc=None, window=None)
    tick(memory, path, ISSUED + 10)
    assert tick(memory, path, ISSUED + 400)[0] is None
    write(path)                                                      # a charge plan, then an explicit hand-over replaces it in memory
    tick(memory, path, ISSUED + 500)
    write(path, mode="anker_manual", action="hold", target_soc=None, window=None, issued_at=ISSUED + 510, valid_until=ISSUED + 810)
    tick(memory, path, ISSUED + 520)
    assert tick(memory, path, ISSUED + 900)[0] is None


def test_the_supervisor_still_applies_its_own_limits_to_a_held_plan(tmp_path):
    path, memory = tmp_path / "plan.json", PlanMemory()
    write(path)
    tick(memory, path, ISSUED + 10)
    held, note = tick(memory, path, ISSUED + 400)
    sup, t = settled()
    ok = sup.decide(fresh(soc=50, current=20), held, note, t)
    assert ok.economic == "charge_to" and ok.charge_limit_w > 0 and "planner silent" in json.dumps(ok.as_dict())
    over = sup.decide(fresh(soc=50, current=44), held, note, t + 1)   # the 45 A garage limit still wins
    assert over.restrictions and over.layer["charge"] == "limits" and not ok.restrictions
    # GitHub issue 64 (D13): the minimum level is the firmware's alone now; the controller no longer inhibits discharge for it.
    low = sup.decide(fresh(soc=8, current=20), held, note, t + 2)
    assert low.discharge == "ALLOW" and "reserve" not in low.restrictions


def test_at_the_window_end_the_reason_is_the_window_ended_not_an_expired_plan(tmp_path):
    """WP6: 7 November 05:30 logged 'Plan has expired' when the cheap window simply ended. The release is the same; the words now say what happened."""
    path, memory = tmp_path / "plan.json", PlanMemory()
    write(path, valid_until=END)                                     # the planner's last plan ends with the window and is not renewed
    tick(memory, path, END - 60)
    plan, note = tick(memory, path, END)
    assert plan is None and note == "Cheap window ended"
    plan, note = tick(PlanMemory(), path, END + 10)                  # a restart after the window has no held plan: the ordinary wording stays
    assert plan is None and note.startswith("Plan has expired")
