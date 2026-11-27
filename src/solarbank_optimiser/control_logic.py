"""The control decisions: one pure, complete function from measured inputs and the plan to a desired battery envelope.

Standard library only (control_exec.py is the one module that uses the state-machine helper). No I/O, no clock of its own, no mode flag: shadow and live run exactly this code, so what is tested in
shadow is what runs live. Every output is explicit (there is no "no change"): each tick says whether discharge is inhibited or
allowed, how many watts of AC charging are permitted, and whether an economic charge request is active or withdrawn.

Values marked PROVISIONAL are placeholders to be set from commissioning measurements; the others come from the specification.
Priority order (specification section 10): fixed limits and reserve, EV inhibition, fault recovery, hand-over, then the plan.
"""

from dataclasses import dataclass
import json
import math
import time

from .garage import ALIGN_S, GARAGE_LIMIT_A, GarageRegulator, Reconciliation, voltage_invalid
from .signals import SOC_HYST_PCT, CHARGE, FAULT, ZERO_OUTPUT, BelowTarget, EvSignal, SmartSlot, clamp_target, decide as table, offpeak
from .shelly_channels import CAR, SOLARBANK_AC                                          # the Shelly's car-charger channel (em1:1), by role, never by number

# ---- fixed limits (specification) ----------------------------------------------------------------------------------------
from .freshness import stored as freshness_stored
from .battery_limits import DEFAULT_MIN_SOC as MIN_SOC, valid_min_soc     # the adjustable minimum level (10 % by default); the plan file carries the current value
AC_CHARGE_LIMIT_W = 2500.0             # the installation's shared AC limit (specification section 2): no charge request exceeds it
# ---- PROVISIONAL ---------------------------------------------------------------------------------------------------------
TARGET_RESTART_BAND = SOC_HYST_PCT / 100.0   # v1.3: BELOW_TARGET turns on 3 % under the target (signals.BelowTarget); kept as a fraction for the tests and the status text
MAX_AGE_S = {"solarbank": 2.5, "shelly": 2.5, "meter": 2.5}      # same limits as the safety lamp; older means unknown
PLAN_CLOCK_SKEW_S = 60.0               # a plan issued further ahead than this means a clock problem
PLAN_MAX_BYTES = 64 * 1024

MODES = ("shadow", "anker_manual")     # the plan's own mode field (hand-over to the Anker app)
ACTIONS = ("normal", "charge_to", "hold", "discharge")      # "normal" (alias "discharge"): the Anker runs itself; "hold" is an explicit instruction to hold SOC


@dataclass(frozen=True)
class Decision:
    discharge: str                     # "INHIBIT" or "ALLOW"
    charge_limit_w: float              # AC charging permitted right now; 0 = stopped
    charge_target_soc: float | None    # the plan's target while charging is requested
    economic: str                      # "charge_to" | "discharge_ok" | "hold" | "withdrawn" | "handed_over"
    restrictions: tuple                # machine-readable tags of every protection that applies
    reasons: tuple                     # the same in plain English
    preview_charge_w: float            # what the plan alone would allow, ignoring protections (for judging shadow runs)
    inputs: dict
    layer: dict                        # which rule decided each output

    def intent(self):
        return (self.discharge, round(self.charge_limit_w), self.charge_target_soc, self.economic)

    def as_dict(self):
        """The decision only. Whether anything was sent is reported separately, per command (control_exec)."""
        return {"discharge": self.discharge, "charge_limit_w": self.charge_limit_w, "charge_target_soc": self.charge_target_soc,
                "economic": self.economic, "restrictions": list(self.restrictions), "reasons": list(self.reasons),
                "preview_charge_w": self.preview_charge_w, "inputs": self.inputs, "layer": self.layer}


def _number(value):
    """A finite, sane number or None. Huge integers (which make isfinite raise OverflowError) and absurd magnitudes are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and abs(value) < 1e15 else None
    except OverflowError:
        return None


def read_plan(path, now_utc):
    """(plan or None, why-not). Never raises: a missing, oversized, corrupt, nested, stale or future plan is no plan."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(PLAN_MAX_BYTES + 1)
    except FileNotFoundError:
        return None, "No plan file yet"
    except OSError as error:
        return None, f"Plan file unreadable ({error.__class__.__name__})"
    if len(raw) > PLAN_MAX_BYTES:
        return None, "Plan file is too large to be a plan"
    try:
        plan = json.loads(raw.decode("utf-8"))
        mode, action = plan.get("mode", "shadow"), plan.get("action", "hold")
        issued, valid_until, target = _number(plan["issued_at"]), _number(plan["valid_until"]), plan.get("target_soc")
        if issued is None or valid_until is None or mode not in MODES or action not in ACTIONS or not issued < valid_until:
            raise ValueError("fields")
        if action == "charge_to" and (_number(target) is None or not 0 <= target <= 1):
            raise ValueError("target")
        window = plan.get("window")
        if window is not None:                                          # the planned charging window: the controller judges "inside it?" itself, every second
            w_start, w_end, w_target = _number(window["start"]), _number(window["end"]), _number(window["target_soc"])
            if None in (w_start, w_end, w_target) or not w_start < w_end or not 0 <= w_target <= 1:
                raise ValueError("window")
            window = {"start": w_start, "end": w_end, "target_soc": w_target}
        cheap = plan.get("offpeak")                                     # the cheap period the planner uses (minutes after midnight London); optional
        if cheap is not None:
            c_start, c_end = cheap["start_minute"], cheap["end_minute"]
            if not all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 1440 for x in (c_start, c_end)) or c_start == c_end:
                raise ValueError("offpeak")
            cheap = {"start_minute": c_start, "end_minute": c_end}
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError, MemoryError, OverflowError, UnicodeDecodeError):
        return None, "Plan file is not a valid plan"
    if issued > now_utc + PLAN_CLOCK_SKEW_S:
        return None, "Plan is dated in the future (clock problem)"
    if now_utc >= valid_until:                                         # the expiry instant itself is already expired
        return None, "Plan has expired"
    if window is not None and action in ("charge_to", "normal", "discharge"):
        inside = window["start"] <= now_utc < window["end"]               # exact to the second: no waiting for a new plan file
        action, target = ("charge_to", window["target_soc"]) if inside else ("normal", None)
    return {"mode": mode, "action": action, "target_soc": target, "issued_at": issued, "valid_until": valid_until,
            "min_soc": valid_min_soc(plan.get("min_soc")) or MIN_SOC, "window": window, "offpeak": cheap,
            "freshness": freshness_stored(plan.get("freshness"))}, None


PLAN_HOLD_S = 3600.0       # how long past its expiry a plan's charging window is still followed if the planner has gone silent (owner decision pending)
HOLDABLE = ("Plan has expired", "No plan file yet", "Plan file unreadable")


class PlanMemory:
    """The controller's own copy of the last valid plan, kept in memory only (nothing is persisted: a restart starts with none, restricted).

    The plan file is renewed every few minutes by the planner. If the planner or the disk stalls (31 Oct 2026, 00:05) the file simply expires, and
    withdrawing a charge that is already under way would waste the cheap window. The window, its target and the minimum level are all known, so for
    a charge that is INSIDE its window the controller keeps following the last plan until the window ends or PLAN_HOLD_S after the plan expired,
    whichever is first. Every other safeguard (garage limit, car, minimum level, stale readings) is applied by the Supervisor as usual. A corrupt,
    oversized or future-dated file is never held over, and neither is a plan without a window (a hand-over or pause)."""

    def __init__(self):
        self._plan = None

    def resolve(self, plan, note, now_utc):
        if plan is not None:
            self._plan = plan if plan.get("window") and plan["mode"] != "anker_manual" else None
            return plan, note
        held = self._plan
        if held is None or not (note or "").startswith(HOLDABLE):
            return plan, note
        window = held["window"]
        if not window["start"] <= now_utc < window["end"] or now_utc >= held["valid_until"] + PLAN_HOLD_S:
            ended = now_utc >= window["end"]
            self._plan = None if ended else self._plan
            if ended and note.startswith("Plan has expired"):
                note = "Cheap window ended"             # WP6: the planner deliberately does not renew a plan that ends with its window, so say what happened
            return plan, note
        until = min(window["end"], held["valid_until"] + PLAN_HOLD_S)
        return ({**held, "action": "charge_to", "target_soc": window["target_soc"], "valid_until": until, "held": True},
                f"{note}; planner silent, following the last plan's window until {time.strftime('%H:%M', time.localtime(until))}")


def safe_decision(reason, inputs=None):
    """What to do when the controller itself cannot decide: everything restricted."""
    return Decision("INHIBIT", 0.0, None, "withdrawn", ("controller_fault",), (reason,), 0.0, inputs or {},
                    {"discharge": "fault", "charge": "fault"})


class Supervisor:
    """Holds the few things that must persist between ticks. It starts RESTRICTED after every (re)start: charging is off until the
    garage current has been measured and low for the recovery time, and discharge is inhibited until the charger has been
    seen idle for the release time. Nothing is persisted across runs, so no old timestamp or restriction can be replayed."""

    def __init__(self, handed_over=False):
        self.charge_w = 0.0
        self._garage_reg = GarageRegulator()
        self._reconcile = Reconciliation()   # v1.3 11.2, GitHub issue 69: P_unmetered against the garage, car and battery readings
        self.ev = EvSignal()                 # v1.3 5.2: on above 1,000 W for 3 s, off below 300 W for 120 s (D7), charger gaps as D15
        self.below = BelowTarget()           # SoC against the plan's target with 3 % hysteresis
        self.smart = SmartSlot()             # an Octopus smart-charging half-hour (needs the car charging and a fresh SMART dispatch)
        self._offpeak_window = None          # the last cheap window the planner published (minutes after midnight London); the standard 23:30-05:30 until then
        self._handed_over = handed_over       # ownership is explicit: it changes only on an explicit plan, never because a file went missing

    @property
    def handed_over(self):
        return self._handed_over

    def _restrict(self, handed_over=False):
        """Back to the state of a fresh start: charging off, discharge inhibited, every confirmation to be earned again."""
        self.__init__(handed_over)

    @property
    def ev_state(self):
        """For the status file: unknown | charging | confirming | standby | lost."""
        if not self.ev.known:
            return "lost" if self.ev.last_good is not None else "unknown"
        return {"off": "standby", "confirming_on": "standby", "on": "charging", "confirming_off": "confirming"}[self.ev.state]

    def _cheap_window(self):
        from datetime import time as dtime
        if self._offpeak_window is None:
            return None
        start, end = self._offpeak_window
        return dtime(start // 60, start % 60), dtime(end // 60, end % 60)

    # -- garage-feed limiter (owner specification v1.3 section 9; the calculation and the write rules are in garage.py) --------------------------
    def _garage(self, current, want_w, now, tags, reasons, control_ok=True, shelly=None, ages=None, max_live_w=None, meter_value=None):
        """Sets self.charge_w. `shelly` is the Shelly value dict (Solarbank AC power and voltage); `ages` the garage and Shelly reading ages;
        `meter_value` the smart meter's own reading (GitHub issue 69: its voltage, and P_garage for the reconciliation check)."""
        shelly = shelly or {}
        ages = ages or {}
        meter_value = meter_value or {}
        battery_w = _number(shelly.get(SOLARBANK_AC))
        # GitHub issue 69 (specification 11.2): the reconciliation machine runs every cycle, known garage current or not, so its dwell timer tracks
        # real elapsed time; it naturally holds (neither raises nor clears) when its own readings are missing. Both checks are tagged only when the
        # garage current itself is known: when it is not, "garage_unknown" below already says charging has stopped, and there is no meter reading
        # of theirs left to judge either (no "meter" device at all means no P_garage and no meter voltage, not specifically a voltage problem).
        aligned = (ages.get("meter") is not None and ages.get("shelly") is not None and abs(ages["meter"] - ages["shelly"]) <= ALIGN_S)
        p_batt_ac = None if battery_w is None else -battery_w          # the specification's own sign: positive = the Solarbank drawing from the grid
        reconciliation_invalid = self._reconcile.update(_number(meter_value.get("sub")), _number(shelly.get(CAR)), p_batt_ac, aligned, now)
        meter_voltage_invalid = voltage_invalid(_number(meter_value.get("voltage")))
        if current is not None:
            if reconciliation_invalid:
                tags.append("reconciliation_invalid")
                reasons.append("Garage power does not reconcile with the car and the battery (a current-transformer reading may be wrong or reversed): AC charging stopped")
            if meter_voltage_invalid:
                tags.append("meter_voltage_invalid")
                reasons.append("The smart meter's own voltage reading is outside 207-253 V, or missing: AC charging stopped")
        if current is None or reconciliation_invalid or meter_voltage_invalid:
            self._garage_reg.reset()
            self.charge_w = 0.0
            if current is None:
                tags.append("garage_unknown")
                reasons.append("Garage current unknown (not yet commissioned or not fresh): AC charging stopped")
            return
        volts = _number(shelly.get(SOLARBANK_AC + "_voltage"))
        setpoint, notes = self._garage_reg.update(now, want_w if want_w is not None else 0.0, current, battery_w, volts, ages.get("meter"), ages.get("shelly"),
                                                  max_live_w, control_ok)
        self.charge_w = setpoint
        if "control_unverified" in notes:
            tags.append("control_unverified")
            reasons.append("A protective command has not been confirmed (rejected, timed out or no effect seen): charging cannot be increased until control is verified")
        if "starting" in notes and want_w > 0:
            tags.append("garage_starting")
            reasons.append("Garage reading valid for less than 30 s: charging starts once it has been valid for 30 s")
        # GitHub issue 73 (the taper of 10 November). The set-point is rounded down to 10 W and the request is not, so whenever the device's own live
        # charge limit (register 10036) is not a multiple of 10 W the set-point sits a few watts under the request. That is the device tapering, not the
        # garage feed: no "garage_limit" restriction (it would pollute the evidence of commissioning test T8), only a plain statement of what governs.
        device_governs = self._garage_reg.bound_by == "device" and not self._garage_reg.over_limit
        if device_governs and setpoint < want_w and "starting" not in notes:
            reasons.append(f"The battery's own charge limit ({want_w:.0f} W) sets the charge power, not the {GARAGE_LIMIT_A:.0f} A garage feed")
        elif self._garage_reg.over_limit or (setpoint < want_w and "starting" not in notes):
            tags.append("garage_limit")
            reasons.append(f"Garage feed {current:.1f} A against the {GARAGE_LIMIT_A:.0f} A limit: AC charging limited to {setpoint:.0f} W")
        if "unaligned" in notes and want_w > setpoint:
            tags.append("garage_unaligned")
            reasons.append("Garage and battery readings are not from the same moment: the battery's own current is not subtracted and charging is not increased")
        if self._garage_reg.alerting:
            tags.append("garage_alert")
            reasons.append("Garage feed over the limit for 10 s with Anker charging already at 0 W: other garage loads are the cause (alert, not controllable here)")
        if self.charge_w > want_w:           # target reached or plan gone: the economic request is withdrawn at once
            self.charge_w = want_w
            self._garage_reg.setpoint_w = want_w

    def decide(self, inputs, plan, plan_note, now, control_ok=True, utc=None):
        """inputs: {device: {"value": {...}, "age": s}} for FRESH devices only. now: monotonic seconds.
        control_ok: False while a protective command is unconfirmed (fed back from the executor). utc: the wall clock (epoch seconds), for OFFPEAK."""
        tags, reasons, layer = [], [], {}
        floor = (plan or {}).get("min_soc") or MIN_SOC               # the owner's adjustable minimum level, carried in the plan file
        sb = inputs.get("solarbank", {}).get("value") or {}
        sh = inputs.get("shelly", {}).get("value") or {}
        mt = inputs.get("meter", {}).get("value") or {}
        soc_percent = _number(sb.get("soc"))
        soc = soc_percent / 100.0 if soc_percent is not None else None
        ev_w = _number(sh.get(CAR))
        ev_kw = ev_w / 1000.0 if ev_w is not None and ev_w >= 0 else None      # negative or non-numeric is invalid = unknown
        current = _number(mt.get("garage_current_a"))
        current = abs(current) if current is not None else None
        max_charge_w = _number(sb.get("max_charge"))
        summary = {"soc": soc, "ev_kw": ev_kw, "garage_current_a": current, "max_charge_w": max_charge_w,
                   "plan": plan_note or "valid", "fresh": sorted(inputs), "unknown": [d for d in MAX_AGE_S if d not in inputs], "min_soc": floor,
                   "ev_state": self.ev_state}

        if (plan and plan["mode"] == "anker_manual") or (self._handed_over and plan is None):
            # Handed over: the Anker app owns the equipment. It stays that way until a valid plan EXPLICITLY says shadow again;
            # a plan that merely expired or disappeared never hands control back.
            self._restrict(handed_over=True)
            why = ("Control handed to the Anker app: pending commands are cancelled and only the release is issued" if plan
                   else "Control stays with the Anker app until an explicit resume (a valid plan in shadow mode) arrives")
            return Decision("ALLOW", 0.0, None, "handed_over", ("handed_over",), (why,), 0.0, summary,
                            {"discharge": "manual", "charge": "manual"})
        if self._handed_over:                                            # an explicit resume: nothing from before is trusted
            self._restrict()

        # -- the owner's decision table (v1.3 section 7): VALID, CHEAP, BELOW_TARGET and EV, then the table itself ---------------------
        window = (plan or {}).get("offpeak")
        if window:
            self._offpeak_window = (window["start_minute"], window["end_minute"])      # remembered: a plan that goes missing at 02:00 does not make the night day-rate
        ev_value = self.ev.update(ev_w, now)
        tariff = inputs.get("tariff", {}).get("value") or {}
        cheap_window = self._cheap_window()
        in_window = bool(utc is not None and (offpeak(utc, *cheap_window) if cheap_window else offpeak(utc)))
        smart = (self.smart.update(utc, ev_value, tariff.get("dispatches"), tariff.get("fetch_ok", "dispatches" in tariff), in_window=in_window,
                                   fetched_at=tariff.get("fetched_at")) if utc is not None else False)
        cheap = in_window or smart
        target_pct = None
        if plan and plan["action"] == "charge_to" and plan.get("target_soc") is not None and soc is not None:
            # GitHub issue 70 (D13): the device's own limits (registers 60000, 60001), published by the reader's safety block. A value outside the
            # register's documented range (section 3.2: 80-100 and 0-20) is not trusted and the full range is used instead, as if the register were unknown.
            charging_limit = _number(sb.get("charging_limit_soc"))
            charging_limit = charging_limit if charging_limit is not None and 80 <= charging_limit <= 100 else 100.0
            discharge_limit = _number(sb.get("discharge_limit_soc"))
            discharge_limit = discharge_limit if discharge_limit is not None and 0 <= discharge_limit <= 20 else 0.0
            target_pct = clamp_target(plan["target_soc"] * 100.0, discharge_limit, charging_limit)
        if target_pct is None:
            self.below = BelowTarget()                  # no target, nothing to be below
            below_target = False
        else:
            below_target = self.below.update(soc_percent, target_pct, now)
        action = table(soc is not None, cheap, below_target, ev_value)
        if utc is None:
            reasons.append("No wall-clock time given: the cheap period cannot be known")

        economic, want_w, target, hold_reason = "withdrawn", 0.0, None, None
        if plan is None:
            reasons.append(f"{plan_note}: economic request withdrawn")
        if action == FAULT:
            reasons.append("Battery state unknown: economic request withdrawn")
        elif action == CHARGE:
            target = plan["target_soc"]
            if max_charge_w is None or max_charge_w <= 0:
                economic = "hold"
                reasons.append("Device charge-power limit unknown: charging not requested"); target = None
            else:
                economic, want_w = "charge_to", min(max_charge_w, AC_CHARGE_LIMIT_W)     # the device's limit AND the installation's
                reasons.append(f"Plan: charge to {target:.0%}")
        elif action == ZERO_OUTPUT:
            economic = "hold"
            if cheap and plan and plan["action"] == "charge_to":
                # GitHub issue 70: name the EFFECTIVE (clamped) target that was actually reached, not the plan's own figure, which the device's
                # own charging limit may have capped below (target_pct is what below_target was judged against).
                effective_target_pct = target_pct if target_pct is not None else plan['target_soc'] * 100
                hold_reason = f"Battery at {soc:.0%}, target {effective_target_pct:.0f}% reached: holding idle until the cheap window ends"
            elif cheap:
                hold_reason = "Cheap period with no charge target: the battery is held, never discharged at the cheap rate"
            else:
                hold_reason = "Car charging in an expensive period: the battery is held so that it does not discharge into the car"
            if plan and plan["action"] == "hold":
                hold_reason = None                      # the plan's own explicit hold keeps its own reason, below
            if hold_reason:
                reasons.append(hold_reason)
        else:                                            # DISCHARGE: the firmware matches house demand
            if plan is not None:
                economic = "discharge_ok"
                reasons.append("Plan: no grid charging; the Anker runs the house as normal")
        if plan and plan["action"] == "hold" and economic != "hold":
            economic = "hold"                           # an explicit hold from the plan, whatever the table says
        preview = want_w

        # -- 1. fixed limits: the garage limiter decides how much of that can be charged ---------------------------------------
        before = len(tags)
        self._garage(current, want_w, now, tags, reasons, control_ok, sh, {"meter": inputs.get("meter", {}).get("age"), "shelly": inputs.get("shelly", {}).get("age")}, max_charge_w, mt)
        layer["charge"] = "limits" if len(tags) > before else ("plan" if self.charge_w > 0 else "default")

        # -- 1b./2. discharge: EV, the minimum level being the firmware's alone (GitHub issue 64, D13) ------------------------------
        inhibit = False
        if soc is None:
            # The Anker firmware stays in control whether or not we can read it, and we could not send it anything anyway: an unknown
            # battery state restricts nothing about discharge. It is flagged, and no grid-charge request is made without knowing the SOC.
            tags.append("battery_unknown"); reasons.append("Battery state unknown: no charge request; the Anker runs itself")
        # GitHub issue 64 (D13, owner 2 November): the minimum SoC floor is "left entirely to the firmware" (specification section 7); "when the
        # battery reaches its floor in DISCHARGE, the firmware puts the unit to sleep by itself". The controller no longer inhibits discharge at
        # `floor` itself (the 28 October rule, superseded): it only reads the device's own floor (register 60001) to clamp the plan's target (see
        # `clamp_target` above) and to show it, with any mismatch, on the Settings page. `floor` is kept below only for the status summary.
        if not self.ev.known:
            tags.append("ev_data_missing"); reasons.append("Charger reading missing or not yet seen: treated as missing data, not as a car charging")
        elif ev_value:
            tags.append("ev_active" if self.ev.state == "on" else "ev_hold")
            reasons.append(f"Car charging ({(ev_w or 0) / 1000:.2f} kW): battery held" if self.ev.state == "on"
                           else "Charger below 300 W: waiting 120 s before the car counts as stopped")
        if action == ZERO_OUTPUT or (action == CHARGE and self.charge_w == 0):
            # v1.3 section 8: CHARGE with a zero set-point stays in mode 3 (no grid charging, no discharge); the battery is not released to discharge at the cheap rate
            inhibit = True
            layer["discharge"] = "cheap" if cheap else "ev"
        # -- 3. the plan may only add an explicit hold. Otherwise discharge is the Anker's own business: its firmware matches house load,
        #       grid and PV, and this controller restricts discharge only for the exceptions above (reserve, car, unknown battery).
        if not inhibit and economic == "hold":
            inhibit = True; layer["discharge"] = "plan"
            if hold_reason is None:
                reasons.append("Plan: hold the battery: discharge inhibited")
        layer.setdefault("discharge", "default")

        return Decision("INHIBIT" if inhibit else "ALLOW", self.charge_w, target, economic,
                        tuple(tags), tuple(reasons), preview, summary, layer)
