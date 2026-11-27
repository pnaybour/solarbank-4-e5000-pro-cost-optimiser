"""The inputs of the owner's decision table (specification v1.3 sections 5.2, 6.2 and 7), each a small state machine built with `machines.py`, and the
pure decision function itself. No I/O and no clock of its own: every function takes `now`.

    EvSignal      the car is charging: on above 1,000 W for 3 s, off below 300 W for 120 s, unchanged in between (D7); charger gaps follow D15
    BelowTarget   the battery is under its target: on at SoC <= target - 3 %, off at SoC >= target, unchanged in between
    SmartSlot     an Octopus smart-charging half-hour is in force (6.2): never from the plan's window, always from the dispatch list and the car
    offpeak()     the standard 23:30 to 05:30 window on the London clock
    decide()      the table, rule by rule, first match wins

All of these are wired into `control_logic.Supervisor`; SMARTSLOT's dispatches reach it through the providers service's dispatch feed (GitHub issue 67)."""

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from .ev_policy import EV_OFF_DWELL_S, EV_OFF_W, EV_ON_DWELL_S, EV_ON_W        # one definition, shared with the car card and the history
from .machines import Spec, build, dwell_at_least, fire

LONDON = ZoneInfo("Europe/London")
EV_GAP_S, EV_HOLD_S = 10.0, 600.0                  # D15: a gap up to 10 s changes nothing; a longer gap holds only a known-charging state, up to 10 minutes
SOC_HYST_PCT = 3.0
OFFPEAK_START, OFFPEAK_END = dtime(23, 30), dtime(5, 30)
OCTOPUS_START_FRESH_S, OCTOPUS_STALE_S = 300.0, 1800.0      # no allowance cap is counted (GitHub issue 79): Octopus manages the 6 hours
HALF_HOUR_S = 1800.0
FAULT, CHARGE, ZERO_OUTPUT, DISCHARGE = "FAULT", "CHARGE", "ZERO_OUTPUT", "DISCHARGE"


class _Model:
    pass


# ---- the decision table ------------------------------------------------------------------------------------------------------------------
def decide(valid, cheap, below_target, ev):
    """Rules in order, first match wins (v1.3 section 7.2). Rule 0: not valid is a fault. Rule 1: cheap and below target charges. Rules 2 and 3: cheap, or the
    car charging in an expensive period, holds the battery at zero output. Rule 4: otherwise the firmware matches house demand."""
    if not valid:
        return FAULT
    if cheap and below_target:
        return CHARGE
    if cheap or ev:
        return ZERO_OUTPUT
    return DISCHARGE


# ---- OFFPEAK ----------------------------------------------------------------------------------------------------------------------------
def offpeak(now_utc, start=OFFPEAK_START, end=OFFPEAK_END):
    """True inside the window on the Europe/London clock. `now_utc` is an aware datetime or epoch seconds. The window crosses midnight; the clock
    changes need no special case because the comparison is on London wall-clock time."""
    local = (datetime.fromtimestamp(now_utc, LONDON) if isinstance(now_utc, (int, float)) else now_utc.astimezone(LONDON)).time()
    return local >= start or local < end if start > end else start <= local < end


# ---- EV ---------------------------------------------------------------------------------------------------------------------------------
EV_SPEC = Spec("ev", ("off", "confirming_on", "on", "confirming_off"), "off", (
    ("rise", "off", "confirming_on"),
    ("fall_back", "confirming_on", "off"),
    ("confirm_on", "confirming_on", "on", dwell_at_least(EV_ON_DWELL_S)),
    ("drop", "on", "confirming_off"),
    ("recover", "confirming_off", "on"),
    ("confirm_off", "confirming_off", "off", dwell_at_least(EV_OFF_DWELL_S)),
))


class EvSignal:
    """`value` is the EV signal. `known` is False until a first valid reading, and after a gap that may no longer be bridged (the caller then treats the
    charger reading as invalid). Pass None, a negative number or a non-number for a missing reading."""

    def __init__(self):
        self.life = build(EV_SPEC, _Model(), now=0.0)
        self.last_good = None
        self.known = False

    @property
    def state(self):
        return self.life.state

    @property
    def value(self):
        return self.known and self.life.state in ("on", "confirming_off")      # an unknown charger is not a charging car

    def update(self, power_w, now):
        good = isinstance(power_w, (int, float)) and not isinstance(power_w, bool) and power_w >= 0 and power_w == power_w
        if not good:
            gap = None if self.last_good is None else now - self.last_good
            if gap is not None and gap <= EV_GAP_S:
                return self.value                                  # a short miss changes nothing
            if gap is not None and gap <= EV_HOLD_S and self.value:
                return self.value                                  # a longer miss holds only a state known to be charging, for up to 10 minutes
            self.known = False
            return self.value
        if not self.known and self.last_good is not None:
            self.life = build(EV_SPEC, _Model(), now=now)           # after a lost period nothing from before is trusted: confirm afresh
        self.known, self.last_good = True, now
        state = self.life.state
        if state == "off" and power_w > EV_ON_W:
            fire(self.life, "rise", now=now)
        elif state == "confirming_on":
            fire(self.life, "confirm_on" if power_w > EV_ON_W else "fall_back", _tick=True, now=now)
        elif state == "on" and power_w < EV_OFF_W:
            fire(self.life, "drop", now=now)
        elif state == "confirming_off":
            fire(self.life, "confirm_off" if power_w < EV_OFF_W else "recover", _tick=True, now=now)
        return self.value


# ---- BELOW_TARGET -------------------------------------------------------------------------------------------------------------------------
BELOW_SPEC = Spec("below_target", ("at_target", "below_target"), "at_target", (
    ("fall_below", "at_target", "below_target"),
    ("reach", "below_target", "at_target"),
))


def clamp_target(target_pct, discharge_limit_pct, charging_limit_pct):
    """TARGET_SOC is clamped to the range of the device's own limit registers (v1.3 5.2)."""
    return max(discharge_limit_pct, min(charging_limit_pct, target_pct))


class BelowTarget:
    """True when SoC <= target - 3 %, false when SoC >= target, unchanged in between. Percent units throughout."""

    def __init__(self):
        self.life = build(BELOW_SPEC, _Model(), now=0.0)

    @property
    def value(self):
        return self.life.state == "below_target"

    def update(self, soc_pct, target_pct, now):
        if soc_pct is None or target_pct is None:
            return self.value
        if soc_pct <= target_pct - SOC_HYST_PCT and not self.value:
            fire(self.life, "fall_below", now=now)
        elif soc_pct >= target_pct and self.value:
            fire(self.life, "reach", now=now)
        return self.value


# ---- SMARTSLOT --------------------------------------------------------------------------------------------------------------------------
SMART_SPEC = Spec("smartslot", ("off", "on"), "off", (
    ("start", "off", "on", lambda event: event.kwargs["may_start"]),
    ("lapse", "on", "off", lambda event: event.kwargs["must_end"]),
))


class SmartSlot:
    """Section 6.2. Dispatches are (start_utc, end_utc, type) with epoch seconds, read from the `planned_dispatches` list and never from a derived
    on/off flag. Once on, the slot holds to the end of the current half-hour, even if the car stops early, the dispatch disappears or a fetch
    fails (the whole half-hour is billed cheap once the car has drawn in it: owner, 18 November 2026). A failed fetch is unknown, not empty: the last good list is kept, may not START a slot once older than 300 s, and is discarded after 1,800 s.
    No allowance is counted (owner, 12 November 2026, GitHub issue 79): Octopus manages the 6-hour allowance and its list is the authority, so a slot
    is cheap when the car is measured charging inside a SMART slot on the current list, nothing else. A bump charge is outside any listed SMART slot,
    so it is never cheap."""

    def __init__(self, cap_half_hours=None):
        self.life = build(SMART_SPEC, _Model(), now=0.0)
        self.dispatches, self.fetched_at = None, None
        self.hold_until = None

    @property
    def value(self):
        return self.life.state == "on"

    def update(self, now, ev, dispatches=None, fetch_ok=True, in_window=None, fetched_at=None):
        """`dispatches`: the list from a SUCCESSFUL fetch (an empty list is an answer); `fetch_ok=False`: the fetch failed (unknown).
        `fetched_at`: when that fetch was made, if not now (the controller reads the list from a file another service wrote: the freshness rules
        count from the fetch, not from the reading of the file). `in_window`: whether now is inside the house cheap window (the caller's own
        window); None means the standard 23:30 to 05:30."""
        if fetch_ok and dispatches is not None:
            known = isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool) and fetched_at == fetched_at
            self.dispatches, self.fetched_at = list(dispatches), (min(now, fetched_at) if known else now)
        age = None if self.fetched_at is None else now - self.fetched_at
        if age is not None and age > OCTOPUS_STALE_S:
            self.dispatches = None
        inside = bool(self.dispatches) and any(kind == "SMART" and start <= now < end for start, end, kind in self.dispatches)
        fresh = age is not None and age <= OCTOPUS_START_FRESH_S
        if self.value:
            # Octopus bills the whole half-hour cheap once the car has drawn in it (owner, 18 November 2026, bill of 16 November), so a slot that has
            # started runs to the end of its half-hour even if the car stops early. If a half-hour is billed at the standard rate after all, the battery
            # stores at most a few minutes of 35p power that it would otherwise have bought later at 35p: the loss is the round trip, about 3p.
            ends = (now >= self.hold_until) if self.hold_until is not None else not ev
            if fire(self.life, "lapse", _tick=True, now=now, must_end=ends):
                self.hold_until = None
        if not self.value and fire(self.life, "start", _tick=True, now=now, may_start=bool(ev and inside and fresh)):
            self.hold_until = (now // HALF_HOUR_S + 1) * HALF_HOUR_S
        return self.value
