"""A full-loop rig for the controller: decide -> prepare -> execute against a fake device, once per simulated second.

The unit tests drive each layer alone with hand-made inputs. The two bugs found in shadow review (#17 a rejected stop hidden by a later
acknowledged command, #18 a charge request that never starts under steady load) only show when the layers run TOGETHER, so this rig runs the real
Supervisor, Preparer and Executor against a device that applies what it accepts and reports what it really does. Standard library only."""
from solarbank_optimiser.control_exec import Executor, Outcome, Preparer
from solarbank_optimiser.control_logic import Supervisor
from solarbank_optimiser.shelly_channels import CAR, SOLARBANK_AC

UTC = 1_800_000_000.0


class FakeDevice:
    """Accepts or rejects commands, applies the accepted ones, and reports measured values. `ack_after` delays the effect by that many seconds."""
    simulated = True
    sends = True

    def __init__(self, rejects=(), ack_after=1):
        self.rejects = set(rejects)         # (channel, action) pairs the device refuses
        self.ack_after = ack_after
        self.charge_w = 0.0                 # measured AC charging power
        self.inhibited = False
        self.now = 0.0
        self._due = []                      # (time the effect appears, command)
        self.calls = []

    def send(self, command, timeout):
        self.calls.append((self.now, command.channel, command.action, dict(command.params)))
        if (command.channel, command.action) in self.rejects:
            return Outcome("rejected", "device refused")
        self._due.append((self.now + self.ack_after, command))
        return Outcome("acknowledged")

    def advance(self, now):
        self.now = now
        for due, command in [d for d in self._due if d[0] <= now]:
            self._due.remove((due, command))
            if command.action == "CHARGE_LIMIT":
                self.charge_w = float(command.params["watts"])
            elif command.action in ("STOP", "WITHDRAW"):
                self.charge_w = 0.0
            elif command.action == "INHIBIT":
                self.inhibited = True
            elif command.action == "ALLOW":
                self.inhibited = False


def make_effect(lag):
    return lambda record, inputs: _effect(record, inputs, lag)


def _effect(record, inputs, lag):
    """What a live transport would check: did the measured values change as the command said? A measurement taken in the same second as the
    acknowledgement cannot show the effect yet: with `lag` 1 it is seen on the following cycle."""
    if inputs["t"] < record["acked_mono"] + lag:
        return None
    charge = -inputs["shelly"]["value"][SOLARBANK_AC]          # on this installation AC input into the Solarbank is a NEGATIVE reading
    discharge = inputs["solarbank"]["value"]["battery_discharge_w"]
    action, params = record["action"], record["params"]
    if action in ("STOP", "WITHDRAW"):
        return charge < 150
    if action == "CHARGE_LIMIT":
        return abs(charge - params["watts"]) <= max(150, .2 * params["watts"])
    if action == "INHIBIT":
        return discharge < 100
    return True


class Rig:
    def __init__(self, device=None, soc=50, current=20.0, car_w=0.0, observe_lag=1, clock_utc=1_799_974_800.0):
        self.device = device or FakeDevice()
        self.exec = Executor(self.device, effect=make_effect(observe_lag))
        self.prep = Preparer("rig")
        self.sup = Supervisor()
        self.soc, self.current, self.car_w = soc, current, car_w
        self.t, self.cycle = 0, 0
        self.clock_utc = clock_utc          # the wall clock at t=0: by default 01:00 London, inside the standard cheap window
        self.decision = None
        self.history = []                   # (t, charge_limit_w, measured charge_w, control_verified)

    def inputs(self):
        return {"t": float(self.t), "solarbank": {"value": {"soc": self.soc, "max_charge": 3600.0, "battery_discharge_w": 0.0}, "age": .3},
                "shelly": {"value": {SOLARBANK_AC: -self.device.charge_w, SOLARBANK_AC + "_voltage": 230.0, CAR: self.car_w}, "age": .3},
                "meter": {"value": {"garage_current_a": self.current, "voltage": 230.0}, "age": .3}}      # GitHub issue 69: a normal, valid meter reading

    def step(self, plan, note=None):
        self.device.advance(self.t)
        inputs = self.inputs()
        self.decision = self.sup.decide(inputs, plan, note, float(self.t), self.exec.control_verified(), utc=self.clock_utc + self.t)
        self.cycle += 1
        self.exec.execute(self.prep.prepare(self.decision, UTC + self.t, float(self.t), self.cycle), inputs, UTC + self.t, float(self.t))
        self.history.append((self.t, self.decision.charge_limit_w, self.device.charge_w, self.exec.control_verified()))
        self.t += 1
        return self.decision

    def run(self, seconds, plan, note=None, until=None):
        for _ in range(seconds):
            self.step(plan, note)
            if until and until(self):
                break
        return self.decision
