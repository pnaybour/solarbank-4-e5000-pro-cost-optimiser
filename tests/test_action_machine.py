"""WP14: the action machine against a fake device. It sends nothing: every test applies the machine's writes to a simulated pair of registers."""
import json

import pytest

from solarbank_optimiser import action_machine as am
from solarbank_optimiser.action_machine import ActionMachine, Write

MASK_OK = 47


class Device:
    """Two registers and a battery that obeys them: mode 3 charges at -setpoint W, anything else draws nothing."""

    def __init__(self, mode=am.MODE_SMART, setpoint=0):
        self.mode, self.setpoint, self.log, self.refuse_default = mode, setpoint, [], False

    def apply(self, writes):
        for w in writes:
            if w.register == am.MODE_REGISTER and w.value == am.MODE_SMART and self.refuse_default:
                continue
            self.log.append((w.register, w.value))
            if w.register == am.MODE_REGISTER:
                self.mode = w.value
            else:
                self.setpoint = w.value

    @property
    def drawn_w(self):
        return float(-self.setpoint) if self.mode == am.MODE_THIRD_PARTY and self.setpoint < 0 else 0.0


def started(tmp_path, **kw):
    machine = ActionMachine(latch_path=tmp_path / "latch.json", **kw)
    assert machine.startup(0.0, MASK_OK, 0, mode=am.MODE_SMART)
    return machine


def run(machine, device, seconds, action, p_set=0.0, valid=True, start=0.0, step=1.0, lie=None, relax_magnitude=False, dc_w=None):
    t = start
    while t < start + seconds:
        drawn = device.drawn_w if lie is None else lie
        device.apply(machine.tick(t, valid, action, p_set, drawn, relax_magnitude, dc_w))
        t += step
    return t


# ---- startup (11.5) -----------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mask,backup,why", [(46, 0, "mode mask"), (15, 0, "mode mask"), (None, 0, "mode mask"), (MASK_OK, 1, "backup reserve")])
def test_startup_refuses_without_mode_bits_0_and_5_or_with_backup_reserve_on(tmp_path, mask, backup, why):
    machine = ActionMachine()
    assert machine.startup(0.0, mask, backup, mode=6) is False and machine.state == "refused" and why in machine.refused_reason
    assert machine.tick(1.0, True, am.CHARGE, 1000, 0) == [] and any(a[0] == "startup-refused" for a in machine.alerts)


def test_startup_with_the_right_mask_runs(tmp_path):
    assert started(tmp_path).state == "running"


# ---- write order (10.2) ------------------------------------------------------------------------------------------------------------------
def test_starting_a_charge_writes_zero_then_the_mode_then_waits_then_the_setpoint(tmp_path):
    machine, device = started(tmp_path), Device()
    seen = []
    t = 0.0
    while t < 6:
        writes = machine.tick(t, True, am.CHARGE, 1500.0, device.drawn_w)
        seen += [(t, w.register, w.value) for w in writes]; device.apply(writes); t += 0.5
    assert seen == [(0.0, am.SETPOINT_REGISTER, 0), (0.0, am.MODE_REGISTER, 3), (2.0, am.SETPOINT_REGISTER, -1500)]       # 0 W, mode, 2 s settle, then the new set-point


def test_the_mode_is_written_only_when_it_changes_and_a_setpoint_change_alone_is_one_write(tmp_path):
    machine, device = started(tmp_path), Device()
    run(machine, device, 10, am.CHARGE, 1500.0)
    before = len(device.log)
    run(machine, device, 5, am.CHARGE, 1500.0, start=10)                       # nothing changed: nothing written
    assert len(device.log) == before
    run(machine, device, 3, am.CHARGE, 900.0, start=15)                        # a lower set-point: one write, no mode write
    assert device.log[before:] == [(am.SETPOINT_REGISTER, -900)]


def test_discharge_writes_mode_0_after_zero_and_no_setpoint(tmp_path):
    machine, device = started(tmp_path), Device()
    run(machine, device, 10, am.CHARGE, 1500.0)
    start = len(device.log)
    run(machine, device, 5, am.DISCHARGE, 0.0, start=10)
    assert device.log[start:] == [(am.SETPOINT_REGISTER, 0), (am.MODE_REGISTER, 0)] and device.mode == 0


def test_a_charge_of_less_than_p_min_stays_in_mode_3_at_zero(tmp_path):
    machine, device = started(tmp_path), Device()
    run(machine, device, 10, am.CHARGE, 60.0)
    assert device.mode == 3 and device.setpoint == 0                            # 11.2: below P_MIN the command is 0 W, still in mode 3


def test_every_action_change_passes_through_zero_watts(tmp_path):
    machine, device = started(tmp_path), Device()
    t = 0.0
    previous = None
    for action, p in ((am.CHARGE, 1500.0), (am.ZERO_OUTPUT, 0.0), (am.CHARGE, 900.0), (am.DISCHARGE, 0.0), (am.CHARGE, 2000.0), (am.DISCHARGE, 0.0)):
        t = run(machine, device, 30, action, p, start=t)
    setpoints = [v for r, v in device.log if r == am.SETPOINT_REGISTER]
    assert all(abs(a - b) == 0 or a == 0 or b == 0 for a, b in zip(setpoints, setpoints[1:]))          # no jump from a charge directly to another non-zero value across a mode change


# ---- verification (11.4) ---------------------------------------------------------------------------------------------------------------
def test_a_command_that_takes_effect_is_verified_and_keeps_running(tmp_path):
    machine, device = started(tmp_path), Device()
    run(machine, device, 60, am.CHARGE, 1500.0)
    assert machine.state == "running" and machine._failures == 0


def test_two_failed_verifications_in_a_row_hold_at_zero_and_a_third_failure_is_hands_off(tmp_path):
    machine, device = started(tmp_path), Device()
    t = run(machine, device, 60, am.CHARGE, 1500.0, lie=0.0)                    # the meter never sees the charge: it did not take effect
    assert machine.state == "hold" and device.mode == 3 and device.setpoint == 0
    assert any(a[0] == "verify-failed" for a in machine.alerts)
    t = run(machine, device, 80, am.CHARGE, 1500.0, start=t, lie=700.0)         # and even the hold cannot be verified (it draws 700 W at 0 W)
    assert machine.state == "hands_off" and (tmp_path / "latch.json").exists()


def test_a_tapering_charge_is_allowed_to_fall_short_when_the_caller_says_so(tmp_path):
    machine, device = started(tmp_path), Device()
    t = 0.0
    while t < 80:
        device.apply(machine.tick(t, True, am.CHARGE, 1500.0, 700.0, relax_magnitude=True)); t += 1
    assert machine.state == "running"


def test_relaxed_or_not_a_charge_that_shows_no_current_at_all_is_still_a_failure(tmp_path):
    """GitHub issue 66: relaxing the magnitude check is not the same as dropping it -- the firmware's own taper still draws SOME power."""
    machine, device = started(tmp_path), Device()
    t = run(machine, device, 60, am.CHARGE, 1500.0, lie=0.0, relax_magnitude=True)
    assert machine.state == "hold"


def test_the_battery_dc_power_must_not_show_a_discharge_while_charging_or_held(tmp_path):
    """GitHub issue 72: specification 11.4's secondary check on register 10008, direction only (positive = discharging)."""
    machine, device = started(tmp_path / "a"), Device()
    run(machine, device, 60, am.CHARGE, 1500.0, dc_w=-1450.0)                  # charging, as the AC power also says: verified
    assert machine.state == "running"
    machine, device = started(tmp_path / "b"), Device()
    run(machine, device, 60, am.CHARGE, 1500.0, dc_w=900.0)                    # the AC power looks right, but the battery itself is discharging
    assert machine.state == "hold"
    machine, device = started(tmp_path / "c"), Device()
    run(machine, device, 60, am.ZERO_OUTPUT, dc_w=80.0)                         # a few tens of watts is losses or PV noise, not a discharge
    assert machine.state == "running"


# ---- mode supervision (10.4) ----------------------------------------------------------------------------------------------------------
def test_a_mode_reverted_in_the_app_is_reasserted_once_and_too_many_reversions_are_hands_off(tmp_path):
    machine, device = started(tmp_path), Device()
    t = run(machine, device, 30, am.CHARGE, 1500.0)
    for k in range(3):                                                           # three reversions in the hour are re-asserted
        device.mode = 0
        device.apply(machine.supervise(t, 0, 0.0, True)); t += 61
        assert device.mode == 3 and machine.state == "running", k
    device.mode = 0
    writes = machine.supervise(t, 0, 0.0, True)                                  # the fourth is HANDS_OFF
    assert machine.state == "hands_off" and not any(w.register == am.MODE_REGISTER for w in writes)


def test_a_mode_reversion_records_the_mode_found_the_battery_status_and_both_powers(tmp_path):
    """GitHub issue 72: specification 10.4 asks for the mode found, battery_status, P_batt_dc and P_batt_ac; only the AC power was kept, and only for the firmware case."""
    machine, device = started(tmp_path), Device()
    t = run(machine, device, 30, am.CHARGE, 1500.0)
    machine.supervise(t, 0, 40.0, True, status=2, dc_w=350.0)
    record = machine.records[-1]
    assert record["record"] == "mode_reversion" and record["mode_found"] == 0 and record["mode_required"] == 3
    assert record["battery_status"] == 2 and record["dc_w"] == 350.0 and record["ac_drawn_w"] == 40.0


def test_high_power_grid_charging_nobody_commanded_is_the_firmware_and_is_not_fought(tmp_path):
    machine, device = started(tmp_path), Device()
    t = run(machine, device, 30, am.ZERO_OUTPUT)
    assert machine.supervise(t, 0, 2000.0, False) == [] and machine.state == "running" and any(a[0] == "firmware-override" for a in machine.alerts)


# ---- the release and the exit (LC1, LC2, D9) ---------------------------------------------------------------------------------------
def test_invalid_data_releases_at_once_and_normal_operation_returns_after_30_s_of_valid_data(tmp_path):
    machine, device = started(tmp_path, default_proven=True), Device()
    t = run(machine, device, 30, am.CHARGE, 1500.0)
    t = run(machine, device, 3, am.CHARGE, 1500.0, valid=False, start=t)
    assert machine.state == "released" and device.mode == am.MODE_DEFAULT and device.setpoint == 0
    t = run(machine, device, 29, am.CHARGE, 1500.0, start=t)
    assert machine.state == "released"
    t = run(machine, device, 5, am.CHARGE, 1500.0, start=t)
    assert machine.state == "running" and device.mode == 3


def test_an_unproven_default_mode_releases_to_mode_0_and_says_so(tmp_path):
    machine, device = started(tmp_path, default_mode=am.MODE_SMART), Device()          # a default other than mode 0 (#78 made 0 the default)
    run(machine, device, 20, am.CHARGE, 1500.0)
    device.apply(machine.exit_writes())
    assert device.mode == 0 and device.setpoint == 0 and any(a[0] == "default-mode-unproven" for a in machine.alerts)


def test_the_default_mode_is_self_consumption_and_needs_no_unproven_alert(tmp_path):
    """GitHub issues 78 and 83: the hand-back is mode 0, so there is nothing unproven to fall back from and no alert to email every six hours."""
    assert am.MODE_DEFAULT == am.MODE_SELF_CONSUMPTION == 0
    machine, device = started(tmp_path), Device()                                         # unproven, as in logged mode
    run(machine, device, 20, am.CHARGE, 1500.0)
    device.apply(machine.exit_writes())
    assert device.mode == 0 and device.setpoint == 0 and not any(a[0] == "default-mode-unproven" for a in machine.alerts)


@pytest.mark.parametrize("action,p", [(am.CHARGE, 2000.0), (am.ZERO_OUTPUT, 0.0), (am.DISCHARGE, 0.0), ("HOLD", 0.0)])
def test_exit_at_each_action_writes_zero_first_then_the_default_mode_and_never_leaves_a_charge_running(tmp_path, action, p):
    machine, device = started(tmp_path, default_proven=True), Device()
    run(machine, device, 30, action, p)
    writes = machine.exit_writes()
    assert [w.register for w in writes] == [am.SETPOINT_REGISTER, am.MODE_REGISTER] and writes[0].value == 0 and writes[1].value == am.MODE_DEFAULT
    device.apply(writes)
    assert not (device.mode == 3 and device.setpoint != 0)


def test_when_the_device_refuses_the_default_mode_mode_0_is_written_and_an_alert_raised(tmp_path):
    machine, device = started(tmp_path, default_proven=True, default_mode=am.MODE_SMART), Device()      # a non-zero default to refuse (#78)
    run(machine, device, 20, am.CHARGE, 1500.0)
    device.refuse_default = True
    device.apply(machine.exit_writes())
    assert device.mode == 3                                                      # still in mode 3 at 0 W: the default mode did not take
    device.apply(machine.device_refused_default_mode())
    assert device.mode == 0 and device.setpoint == 0 and any(a[0] == "default-mode-refused" for a in machine.alerts)


# ---- the HANDS_OFF latch (LC4, D18) -------------------------------------------------------------------------------------------------
def into_hands_off(tmp_path):
    machine, device = started(tmp_path), Device()
    t = run(machine, device, 30, am.CHARGE, 1500.0)
    for k in range(4):
        device.mode = 0
        machine.supervise(t, 0, 0.0, True); t += 61
    assert machine.state == "hands_off"
    return machine, t


def test_hands_off_survives_a_restart_nothing_is_written_until_a_reset_request(tmp_path):
    machine, t = into_hands_off(tmp_path)
    latch = json.loads((tmp_path / "latch.json").read_text())
    assert latch["version"] == 1 and latch["cause"] == "mode_reversions"
    restarted = ActionMachine(latch_path=tmp_path / "latch.json")
    assert restarted.state == "hands_off"
    assert restarted.startup(t, MASK_OK, 0, mode=3) is False
    for tt in range(0, 100):
        assert restarted.tick(t + tt, True, am.CHARGE, 1500.0, 0.0) == []        # fresh, valid readings change nothing
    assert restarted.reset(t + 200, "r1", latch["wall"] - 5, True, True) is False  # older than the latch: refused
    assert restarted.reset(t + 200, "r1", latch["wall"] + 150, False, True) is False        # the 11.5 checks must pass
    assert restarted.reset(t + 200, "r1", latch["wall"] + 150, True, True) is True
    assert not (tmp_path / "latch.json").exists() and restarted.state == "startup"
    assert restarted.startup(t + 201, MASK_OK, 0, mode=3) and restarted.state == "running"
    assert restarted.reset(t + 300, "r1", latch["wall"] + 250, True, True) is False          # a used request is never honoured twice


@pytest.mark.parametrize("content", ["not json {", json.dumps({"version": 99, "at": 1.0}), ""])
def test_a_corrupt_or_unknown_latch_counts_as_latched(tmp_path, content):
    (tmp_path / "latch.json").write_text(content)
    machine = ActionMachine(latch_path=tmp_path / "latch.json")
    assert machine.state == "hands_off" and machine.tick(1.0, True, am.CHARGE, 1500.0, 0.0) == []


def test_a_latch_that_cannot_be_written_is_held_in_memory_alerted_and_retried(tmp_path):
    blocker = tmp_path / "file"; blocker.write_text("x")
    machine = ActionMachine(latch_path=blocker / "latch.json")                    # the folder cannot be created
    machine.startup(0.0, MASK_OK, 0, mode=3)
    machine._enter_hands_off(5.0, "test")
    assert machine._latch_pending is not None and any(a[0] == "latch-not-saved" for a in machine.alerts)
    machine.latch_path = tmp_path / "ok" / "latch.json"                           # the disk comes back
    machine.tick(30.0, True, am.CHARGE, 0.0, 0.0)
    assert machine._latch_pending is None and (tmp_path / "ok" / "latch.json").exists()


def test_nothing_is_written_on_exit_when_latched_or_refused(tmp_path):
    machine, _ = into_hands_off(tmp_path)
    assert machine.exit_writes() == []
    refused = ActionMachine(); refused.startup(0.0, 46, 0, mode=3)
    assert refused.exit_writes() == []


# ---- the property of LC5 --------------------------------------------------------------------------------------------------------------
from hypothesis import given, settings, strategies as st


@given(steps=st.lists(st.tuples(st.sampled_from([am.CHARGE, am.ZERO_OUTPUT, am.DISCHARGE, am.FAULT]), st.floats(0, 2500), st.booleans(), st.integers(1, 40)), min_size=1, max_size=12),
       proven=st.booleans(), refuse=st.booleans())
@settings(max_examples=60, deadline=None)
def test_after_any_run_and_an_exit_the_unit_is_never_left_in_mode_3_with_a_non_zero_setpoint(tmp_path_factory, steps, proven, refuse):
    folder = tmp_path_factory.mktemp("prop")
    machine = ActionMachine(latch_path=folder / "latch.json", default_proven=proven, default_mode=am.MODE_SMART if refuse else am.MODE_DEFAULT)
    machine.startup(0.0, MASK_OK, 0, mode=am.MODE_SMART)
    device = Device()
    device.refuse_default = refuse
    t = 0.0
    for action, p, valid, seconds in steps:
        t = run(machine, device, seconds, action, p, valid=valid, start=t)
    device.apply(machine.exit_writes())
    if machine.state not in ("hands_off", "refused") and device.mode == 3:
        device.apply(machine.device_refused_default_mode())                        # the caller's reaction to a refused default mode (LC5)
    if machine.state not in ("hands_off", "refused"):
        assert not (device.mode == 3 and device.setpoint != 0)


def test_a_ram_only_latch_keeps_retrying_until_the_durable_file_is_written(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the folder should be")
    durable, ram = blocker / "latch.json", tmp_path / "ram-latch.json"
    machine = ActionMachine(latch_path=durable, fallback_latch_path=ram, default_proven=True)
    machine._enter_hands_off(10.0, "test")
    assert ram.exists() and not durable.exists() and machine._latch_pending is not None
    blocker.unlink()                                                      # durable storage recovers
    for t in range(11, 200):
        machine.tick(float(t), True, "ZERO_OUTPUT")
    assert durable.exists() and machine._latch_pending is None
