"""WP9: the full loop (real Supervisor, Preparer, Executor and control_verified()) against a fake device.

The regression tests were written RED first (xfail strict). #17 turned green with WP11 (the command lifecycle) and #18 with WP12 (garage regulation
without a recovery timer), and the markers were removed."""
import pytest

from control_rig import FakeDevice, Rig, UTC
from test_control_loop import plan


def test_the_rig_itself_charges_and_obeys_when_nothing_goes_wrong():
    """Sanity check of the rig: with observation in the same cycle the loop charges at the installation limit and the device follows."""
    rig = Rig(observe_lag=0)
    rig.run(400, plan("charge_to", .8))
    assert rig.decision.charge_limit_w == 2500.0 and rig.device.charge_w == 2500.0 and rig.exec.control_verified()



def test_17_a_rejected_stop_is_not_hidden_by_a_later_confirmed_inhibit():
    device = FakeDevice()
    rig = Rig(device, current=20.0, observe_lag=0)
    rig.run(400, plan("charge_to", .8))                      # healthy: charging at full power
    assert device.charge_w == 2500.0
    device.rejects.add(("ac_charge", "STOP"))               # from now on the device refuses to stop charging
    rig.current, rig.car_w = 48.0, 7000.0                   # garage overload while the car charges: reduce, then STOP (refused by the device)
    rig.run(8, plan("charge_to", .8))
    rig.current = 20.0
    start = len(rig.history)
    rig.run(120, plan("charge_to", .8))
    after = rig.history[start:]
    assert any(call[2] == "STOP" for call in device.calls)
    assert device.charge_w > 0                               # the device never stopped
    assert not any(verified for _, _, _, verified in after), "control was reported verified while the stop had never taken effect"
    assert max(limit for _, limit, _, _ in after) == 0.0     # and charging must not have been raised in the meantime


def test_18_charging_starts_under_steady_healthy_garage_current():
    rig = Rig(current=30.0, soc=50, observe_lag=1)           # every command acknowledged; its effect is seen on the following cycle
    rig.run(600, plan("charge_to", .8))
    assert max(limit for _, limit, _, _ in rig.history) > 0.0, "600 one-second cycles at 30 A and the charge request never rose above 0 W"
