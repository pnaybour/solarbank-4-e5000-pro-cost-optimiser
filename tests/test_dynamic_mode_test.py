"""The exploratory commissioning test `dynamic-mode` (owner, 15 November 2026): can the battery be switched to the Anker's dynamic pricing (mode 7) and back?"""
import pytest

from solarbank_optimiser import commission_tests as t

Failed = t.TestFailed          # not named Test*, so pytest does not try to collect it as a test class


class Device:
    """A fake Solarbank. `falls_back_after`: reads after which the device leaves mode 7 by itself (as smart mode did on 11 November)."""
    def __init__(self, mode=0, mask=111, falls_back_after=None, refuse=None, stuck=False):
        self.mode, self.mask, self.writes, self.reads_in_7 = mode, mask, [], 0
        self.falls_back_after, self.refuse, self.stuck = falls_back_after, refuse, stuck

    def settings(self, _client):
        if self.mode == 7:
            self.reads_in_7 += 1
            if self.falls_back_after is not None and self.reads_in_7 > self.falls_back_after:
                self.mode = 0
        return {"decoded": {"operating_mode": {"code": self.mode}, "ems_mode_mask": {"raw": self.mask}}}

    def write_mode(self, value):
        self.writes.append(("mode", value))
        if value == self.refuse:
            raise RuntimeError("Device refused the write (exception 3)")
        if not (self.stuck and value == 0):
            self.mode = value

    def write_setpoint(self, value):
        self.writes.append(("setpoint", value))


def run(device, may_write=lambda: None):
    said = []
    note = t.dynamic_mode(device.settings, object(), device, lambda _s: None, controller_may_write=may_write, live=lambda: {"soc": 0.62, "ac_power": 0.0}, say=said.append)
    return note, said


def test_a_device_that_takes_mode_7_and_holds_it_passes_and_is_handed_back():
    device = Device()
    note, said = run(device)
    assert device.writes == [("setpoint", 0), ("mode", 7), ("mode", 0)] and device.mode == 0
    assert "held for 180 s" in note and "back to mode 0" in note
    assert len([line for line in said if "mode 7" in line and " s:" in line]) == 18          # read every 10 s for 3 minutes
    assert "battery level 62 %" in said[0]


def test_a_device_that_falls_back_by_itself_fails_and_is_still_handed_back():
    device = Device(falls_back_after=3)
    with pytest.raises(Failed, match="did not stay in dynamic pricing"):
        run(device)
    assert device.writes[-1] == ("mode", 0) and device.mode == 0


def test_a_refused_mode_7_fails_with_the_reason_and_is_still_handed_back():
    device = Device(refuse=7)
    with pytest.raises(Failed, match="switching to mode 7 failed.*refused"):
        run(device)
    assert device.writes == [("setpoint", 0), ("mode", 7), ("mode", 0)] and device.mode == 0


def test_it_says_so_loudly_if_the_battery_cannot_be_handed_back():
    with pytest.raises(Failed, match="set Self-Consumption in the Anker app now"):
        run(Device(stuck=True))
    with pytest.raises(Failed, match="COULD NOT HAND THE BATTERY BACK"):
        run(Device(refuse=0))


@pytest.mark.parametrize("device,reason", [(Device(mask=47), "does not offer dynamic pricing"), (Device(mode=3), "third-party control")])
def test_it_writes_nothing_when_it_should_not_start(device, reason):
    with pytest.raises(Failed, match=reason):
        run(device)
    assert device.writes == []


def test_it_refuses_to_run_beside_a_controller_that_may_write():
    device = Device()
    with pytest.raises(Failed, match="may write to the battery"):
        run(device, may_write=lambda: "the controller is running in 'live' mode and may write to the battery: put it back to logged first")
    assert device.writes == []


def test_it_is_runnable_gets_the_only_writer_that_may_write_mode_7_and_is_not_a_required_test():
    from solarbank_optimiser import live_gate
    assert "dynamic-mode" in t.TESTS and "dynamic-mode" in t.NEEDS_DEVICE
    assert t.EXTRA_MODES == {"dynamic-mode": (7,)}
    assert "dynamic-mode" not in live_gate.REQUIRED_TESTS                       # exploratory: nothing is written to the commissioning record


# ---- the other hand-back: the Pi comes back and finds the battery in dynamic pricing (mode 7) --------------------------------------------------------
def _controller_finds_mode_7(tmp_path, action, p_set):
    from solarbank_optimiser import action_machine as am
    from test_action_machine import Device, run
    machine = am.ActionMachine(latch_path=tmp_path / "latch.json")
    assert machine.startup(0.0, 47, 0, mode=7)                                    # any mode is accepted at start-up: only the mask and backup reserve are checked
    device = Device(mode=7)
    run(machine, device, 45, action, p_set)
    return machine, device


def test_a_restarted_controller_takes_the_battery_back_from_mode_7_into_self_consumption(tmp_path):
    from solarbank_optimiser import action_machine as am
    machine, device = _controller_finds_mode_7(tmp_path, am.DISCHARGE, 0.0)
    assert device.log[:2] == [(am.SETPOINT_REGISTER, 0), (am.MODE_REGISTER, 0)]   # 0 W first, then mode 0
    assert device.mode == 0 and machine.state == "running" and not machine.records


def test_a_restarted_controller_takes_the_battery_back_from_mode_7_to_charge(tmp_path):
    from solarbank_optimiser import action_machine as am
    machine, device = _controller_finds_mode_7(tmp_path, am.CHARGE, 1500.0)
    assert device.log[:2] == [(am.SETPOINT_REGISTER, 0), (am.MODE_REGISTER, 3)] and device.setpoint == -1500
    assert machine.state == "running" and not machine.records and not [a for a in machine.alerts if a[0] != "startup-refused"]
