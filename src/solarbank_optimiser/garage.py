"""Garage-feed regulation: owner specification v1.3 section 9 (decision D14). Standard library plus `machines.py`. No I/O, no clock of its own.

    I_eff   = max(I_garage_avg, I_garage_now)               the higher of the 3 s average and the latest sample: a sudden rise cuts the rate at once
    I_other = I_eff - I_batt                                what the garage draws apart from the Solarbank
    P_avail = min(P_MAX, P_MAX_LIVE, max(0, (limit - I_other) * V))
    P_set   = 0 if P_avail < P_MIN else P_avail rounded down to 10 W

Write rules: a decrease of any size is written at once; an increase only if it is at least SETPOINT_DEADBAND_W and RAMP_UP_MIN_INTERVAL_S has passed since the
last increase; never more than one write a second. There is NO recovery timer, so a protective command being unconfirmed for a moment cannot erase any
history of how long the current has been low (#18): an unconfirmed command only blocks increases while it is unconfirmed.

I_batt is the AC input INTO the Solarbank on this installation (the Shelly's negative readings on the Solarbank channel, so I_batt = -P / V when P < 0).
It is subtracted only when the garage and battery readings were taken within ALIGN_S of each other, because each is valid on its own for up to STALE_S: a
fresh 40 A garage reading beside an 8-second-old battery reading that still showed charging would otherwise infer 30 A of other load and permit more
charging. When they are not aligned I_batt is not subtracted (the conservative direction) and no increase is written on that cycle.

Charging starts only after the garage reading has been valid for VALID_HOLD_S (a start-up rule, not a recovery timer: it is measured from the first valid
sample, restarts only when the reading is lost, and nothing about command confirmation touches it).

The over-current alert is a small machine: ok -> over_limit -> alert once the average has stayed above the limit for ALERT_S while the set-point is already 0 W
(the excess is not the battery's; this program cannot reduce other garage loads).

GitHub issue 69 (specification 11.2, owner decision A, 9/10 November, with an email alert): two further validity checks, neither implemented before. Both
are about the smart meter's own reading, never the Shelly's: `voltage_invalid` names an out-of-range or missing meter voltage; `Reconciliation` is the
small machine ok -> low -> invalid for P_unmetered = P_garage - P_EV - P_batt_ac (specification 5.2) staying below RECON_LOW_W for RECON_PERSIST_S. Both
use only readings within ALIGN_S of each other; the caller (`control_logic.Supervisor._garage`) treats either as 'garage current unknown' (AC charging
stopped) and raises an alert, same as a missing garage reading."""

from collections import deque
import math

from .machines import Spec, build, dwell_at_least, fire

GARAGE_LIMIT_A = 45.0
AVG_WINDOW_S = 3.0
P_MAX_W = 2500.0
P_MIN_W = 100.0
SETPOINT_DEADBAND_W = 50.0
RAMP_UP_MIN_INTERVAL_S = 5.0
WRITE_MIN_S = 0.95                  # one write a second (a 1 s cycle period jitters a little)
ALIGN_S = 1.0
ALERT_S = 10.0
VALID_HOLD_S = 30.0                 # v1.3 11.1: normal operation (re)starts once the data has been valid for this long; measured from the first valid sample only
V_MIN, V_MAX, V_NOMINAL = 207.0, 253.0, 230.0

ALERT_SPEC = Spec("garage_alert", ("ok", "over_limit", "alert"), "ok", (
    ("exceed", "ok", "over_limit"),
    ("confirm", "over_limit", "alert", dwell_at_least(ALERT_S)),
    ("clear", ["over_limit", "alert"], "ok"),
))

RECON_LOW_W = -230.0                 # specification 11.2: about -1 A
RECON_PERSIST_S = 30.0

RECONCILE_SPEC = Spec("reconcile", ("ok", "low", "invalid"), "ok", (
    ("drop", "ok", "low"),
    ("confirm", "low", "invalid", dwell_at_least(RECON_PERSIST_S)),
    ("clear", ["low", "invalid"], "ok"),
))


def voltage_invalid(volts):
    """Specification 11.2: VALID is FALSE when the smart meter's own voltage (5.1) is outside V_MIN-V_MAX, or missing. Unlike the Shelly's voltage
    elsewhere in this module, a missing reading here is unknown, not the nominal V_NOMINAL: this check exists precisely to not paper over it."""
    return not (isinstance(volts, (int, float)) and not isinstance(volts, bool) and volts == volts and V_MIN <= volts <= V_MAX)


class Reconciliation:
    """Specification 11.2: VALID is FALSE while P_unmetered = P_garage - P_EV - P_batt_ac (5.2) stays below RECON_LOW_W for RECON_PERSIST_S, using only
    readings within ALIGN_S of each other. Guards against a wrong or reversed current-transformer channel making the garage regulation believe there
    is more headroom than there is."""

    def __init__(self):
        self.life = build(RECONCILE_SPEC, _Model(), now=0.0)

    @property
    def invalid(self):
        return self.life.state == "invalid"

    def update(self, p_garage, p_ev, p_batt_ac, aligned, now):
        """`p_batt_ac` in the specification's own sign (positive = the Solarbank drawing from the grid): this installation's raw Shelly reading is the
        opposite, so callers negate it first. Readings that are not aligned, or missing, cannot be judged this cycle: the state simply holds (neither
        clearing nor raising), the same conservative choice the alignment check elsewhere in this module makes."""
        if not aligned or p_garage is None or p_batt_ac is None:
            return self.invalid
        low = (p_garage - (p_ev or 0.0) - p_batt_ac) < RECON_LOW_W
        if low:
            if self.life.state == "ok":
                fire(self.life, "drop", now=now)
            else:
                fire(self.life, "confirm", _tick=True, now=now)
        elif self.life.state != "ok":
            fire(self.life, "clear", now=now)
        return self.invalid


class _Model:
    pass


def p_avail(i_now, i_avg, i_batt, volts, p_max=P_MAX_W, p_max_live=None, limit_a=GARAGE_LIMIT_A):
    """The charge power the garage feed can carry. `i_batt` None means 'not subtracted'. Returns watts, 0 below P_MIN, rounded down to 10 W."""
    i_eff = max(i_avg, i_now)
    i_other = i_eff if i_batt is None else max(0.0, i_eff - i_batt)
    ceiling = min(p_max, p_max_live) if p_max_live is not None else p_max
    avail = min(ceiling, max(0.0, (limit_a - i_other) * volts))
    return 0.0 if avail < P_MIN_W else math.floor(avail / 10.0) * 10.0


def binding_constraint(i_now, i_avg, i_batt, volts, p_max=P_MAX_W, p_max_live=None, limit_a=GARAGE_LIMIT_A):
    """Which of `p_avail`'s two ceilings actually governs (GitHub issue 73, found 10 November): the garage feed's headroom under the 45 A limit, or the
    installation/device ceiling (P_MAX_W or the device's own live charge limit). 'garage', 'device', or None when they are equal. Mirrors `p_avail`'s
    own arithmetic so the two never disagree about which is smaller. In service 'device' is what a taper looks like: the request already carries the
    device's limit, and `p_avail` then rounds it down to 10 W, so the set-point falls a few watts short of the request with the feed nowhere near 45 A."""
    i_eff = max(i_avg, i_now)
    i_other = i_eff if i_batt is None else max(0.0, i_eff - i_batt)
    headroom_w = max(0.0, (limit_a - i_other) * volts)
    device_w = min(p_max, p_max_live) if p_max_live is not None else p_max
    if headroom_w < device_w:
        return "garage"
    if device_w < headroom_w:
        return "device"
    return None


class GarageRegulator:
    def __init__(self):
        self.setpoint_w = 0.0
        self.over_limit = False
        self.bound_by = None                    # 'garage', 'device' or None (issue 73): which ceiling last governed the set-point, for the reason text
        self.samples = deque()                  # (time, garage amps) over the last AVG_WINDOW_S
        self._write_at = None
        self._increase_at = None
        self._valid_since = None
        self.alert = build(ALERT_SPEC, _Model(), now=0.0)

    @property
    def alerting(self):
        return self.alert.state == "alert"

    def average(self, now, i_now):
        self.samples.append((now, i_now))
        while self.samples and self.samples[0][0] < now - AVG_WINDOW_S:
            self.samples.popleft()
        return sum(a for _, a in self.samples) / len(self.samples)

    def reset(self):
        self.setpoint_w = 0.0
        self.bound_by = None
        self.samples.clear()
        self._write_at = self._increase_at = self._valid_since = None
        if self.alert.state != "ok":                 # called every second while the garage reading is unknown: an event the machine does not allow from
            fire(self.alert, "clear", now=0.0)       # "ok" was logged as a warning each time (a line a second in the journal)

    def update(self, now, want_w, i_now, battery_ac_w, volts, garage_age, battery_age, max_live_w=None, control_ok=True):
        """Returns (set-point W, notes). `want_w` is what the plan alone would charge. `battery_ac_w`: the Solarbank channel's power (negative = AC input)."""
        notes = []
        i_avg = self.average(now, i_now)
        self.over_limit = max(i_avg, i_now) > GARAGE_LIMIT_A
        volts_ok = volts is None or (isinstance(volts, (int, float)) and V_MIN <= volts <= V_MAX)      # no voltage reading: the nominal 230 V; out of range: not trusted
        v = volts if volts is not None and volts_ok else V_NOMINAL
        aligned = (isinstance(battery_ac_w, (int, float)) and volts_ok and garage_age is not None and battery_age is not None
                   and abs(garage_age - battery_age) <= ALIGN_S)
        i_batt = max(0.0, -battery_ac_w) / v if aligned else None
        if not aligned:
            notes.append("unaligned")
        if self._valid_since is None:
            self._valid_since = now
        starting = now - self._valid_since < VALID_HOLD_S
        if starting:
            notes.append("starting")
        if aligned or max(i_avg, i_now) > GARAGE_LIMIT_A:
            available = p_avail(i_now, i_avg, i_batt, v, p_max_live=max_live_w)
        else:
            # GitHub issue 80 (found 12 November): with the readings not from the same moment the battery's own current cannot be subtracted, and
            # counting it twice cut the charge to about 550 W with the car charging, then the next aligned cycle put it back: 100+ flips a night at
            # 42.5 A against the 45 A limit. The measured garage total already includes the battery, so while it is within the limit the set-point
            # is held (never increased: see below); only a total over the limit falls back to the conservative headroom above.
            ceiling = min(P_MAX_W, max_live_w) if max_live_w is not None else P_MAX_W
            available = min(self.setpoint_w, ceiling)
            notes.append("held")
        target = 0.0 if starting else min(available, want_w)
        target = math.floor(target / 10.0) * 10.0 if target > 0 else 0.0
        # GitHub issue 73: which ceiling actually governs, so the reason text names the real cause (the 45 A feed, or the installation/device's own
        # limit) instead of always blaming the garage current. Only meaningful when `available` itself is what's capping (not `want_w`, and not "starting").
        self.bound_by = (binding_constraint(i_now, i_avg, i_batt, v, p_max_live=max_live_w) if not starting and available < want_w and "held" not in notes
                         else None)
        writable = self._write_at is None or now - self._write_at >= WRITE_MIN_S
        if target < self.setpoint_w:
            if writable:
                self.setpoint_w, self._write_at = target, now         # a decrease of any size, at once
        elif target > self.setpoint_w:
            if not control_ok:
                notes.append("control_unverified")
            elif not aligned:
                pass                                                   # no increase on a cycle whose readings cannot be reconciled
            elif (target - self.setpoint_w >= SETPOINT_DEADBAND_W and writable
                  and (self._increase_at is None or now - self._increase_at >= RAMP_UP_MIN_INTERVAL_S)):
                self.setpoint_w, self._write_at, self._increase_at = target, now, now
        if i_avg > GARAGE_LIMIT_A and self.setpoint_w == 0.0:
            if self.alert.state == "ok":
                fire(self.alert, "exceed", now=now)
            else:
                fire(self.alert, "confirm", _tick=True, now=now)
        elif self.alert.state != "ok":
            fire(self.alert, "clear", now=now)
        return self.setpoint_w, notes
