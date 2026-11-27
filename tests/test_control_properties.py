"""WP10: Hypothesis drives the full-loop rig (tests/control_rig.py) with random conditions and checks invariants that must always hold.

Known-bad behaviour (#17 and #18) is excluded here by construction, so the skeleton runs green on the current code: the device never refuses
a command and observation is immediate. WP11 and WP12 widen the rules to include them. The deploy gate uses a small example budget
(profile "gate", the default); a development run sets HYPOTHESIS_PROFILE=dev for a larger one."""
import os

from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from control_rig import Rig
from solarbank_optimiser import control_logic as cl
from solarbank_optimiser.garage import p_avail
from test_control_loop import plan

settings.register_profile("gate", max_examples=15, stateful_step_count=40, deadline=None, suppress_health_check=list(HealthCheck))
settings.register_profile("dev", max_examples=300, stateful_step_count=120, deadline=None, suppress_health_check=list(HealthCheck))
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "gate"))

PLANS = {"charge": plan("charge_to", .8), "normal": plan("normal"), "hold": plan("hold"), "none": None}


class Loop(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.rig = Rig(observe_lag=0, current=20.0)
        self.plan = PLANS["charge"]
        self.decided_on = None            # the plan the latest decision was made with

    @rule(seconds=st.integers(1, 90))
    def run_on(self, seconds):
        for _ in range(seconds):
            self.rig.step(self.plan, None if self.plan else "Plan has expired")
            d = self.rig.decision
            self.decided_on = self.plan
            # a charge request never rises above the installation limit or the device's own maximum, and never goes negative
            assert 0.0 <= d.charge_limit_w <= min(cl.AC_CHARGE_LIMIT_W, 3600.0)
            # the request never exceeds what the garage feed can carry beside the battery's own measured draw (v1.3 section 9)
            assert d.charge_limit_w <= p_avail(self.rig.current, self.rig.current, self.rig.device.charge_w / 230.0, 230.0)

    @rule(amps=st.floats(0, 70, allow_nan=False))
    def garage_current(self, amps):
        self.rig.current = amps

    @rule(kw=st.sampled_from([0.0, 0.0, 7000.0]))
    def car(self, kw):
        self.rig.car_w = kw

    @rule(soc=st.integers(5, 100))
    def soc(self, soc):
        self.rig.soc = soc

    @rule(name=st.sampled_from(sorted(PLANS)))
    def publish(self, name):
        self.plan = PLANS[name]

    @invariant()
    def no_charge_request_without_a_plan_that_wants_one(self):
        d = self.rig.decision
        if d is not None and self.decided_on is None:
            assert d.charge_limit_w == 0.0

    @invariant()
    def the_device_never_charges_above_the_installation_limit(self):
        assert self.rig.device.charge_w <= cl.AC_CHARGE_LIMIT_W

    @invariant()
    def a_hold_plan_inhibits_discharge(self):
        d = self.rig.decision
        if d is not None and self.decided_on is PLANS["hold"]:
            assert d.discharge == "INHIBIT"


TestLoop = Loop.TestCase


def test_healthy_conditions_mean_charging_eventually_starts():
    """The 'eventually' invariant, as a fixed scenario: healthy readings, a plan that wants charge, no refusals."""
    rig = Rig(observe_lag=0, current=25.0, soc=40)
    rig.run(200, PLANS["charge"])
    assert rig.decision.charge_limit_w > 0.0
