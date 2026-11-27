"""The action machine (WP14): what the controller does to the battery's two control registers, in what order, and when it must stop. Owner specification v1.3
sections 7 (the table's actions), 8, 10 (write sequencing and mode supervision) and 11 (faults, verification, startup, shutdown), with the owner's answers of 2 and 3 November
(D9 reset to the default mode on bad data, D17 how a release is written, D18 the HANDS_OFF latch) and contract LC1 to LC5 of POST_SHADOW_PLAN.md.

THIS MODULE SENDS NOTHING. It is pure logic: `tick()` and the other methods return `Write` steps, the live transport (Stage C) would perform them, and the caller reads the registers back and
passes what it saw. Every test drives it against a fake device. It uses the WP11 machine helper for the one state machine that matters:

    startup -> running            the 11.5 checks passed and the data is valid
    startup -> refused            a check failed (mode mask bits 0 and 5, backup reserve off): nothing runs
    running -> released           data other than the charger is invalid or stale (D9): set-point 0 W, then the default mode
    released -> running           the data has been valid for 30 s (11.1)
    running -> hold               a command failed verification twice: 0 W in mode 3
    hold -> running               verification passes again and the data has been valid for 30 s
    hold -> hands_off             the hold itself cannot be verified
    running -> hands_off          unexplained mode reversions above the limit
    any -> hands_off              a stored latch is found at start (LC4)
    hands_off -> startup          the owner's reset request, honoured only when newer than the latch and used once

Write order (10.2): set-point 0 W, then the mode (if it changes), wait MODE_SETTLE_S, then the new set-point. A direction change always passes through 0 W. The mode is written only when it
changes. The set-point is signed: negative charges from the grid (the AC input), zero holds, and DISCHARGE writes mode 0 and no set-point."""

from collections import deque
from dataclasses import dataclass
import json
import time
import os
from pathlib import Path

from .machines import Spec, build, fire

MODE_REGISTER, SETPOINT_REGISTER = 10064, 10071
MODE_SELF_CONSUMPTION, MODE_THIRD_PARTY, MODE_SMART = 0, 3, 6     # 6 is the vendor's smart mode (anker_vendor.OPERATING_MODES), not "Dynamic Tariff"
MODE_DEFAULT = MODE_SELF_CONSUMPTION     # the hand-back mode (owner, 11 November 2026, GitHub issue 78): smart mode fails on this installation
P_MIN_W = 100.0
MODE_SETTLE_S = 2.0
RELEASE_RETRY_S = 30.0               # a release that did not get through is attempted again this often
VERIFY_S = 20.0
MODE_CHECK_S = 60.0
MODE_REVERT_LIMIT = 3                 # per hour; one more than this is HANDS_OFF
RETURN_VALID_S = 30.0
VERIFY_TOLERANCE_W, VERIFY_TOLERANCE_PCT = 150.0, 0.20
DC_DISCHARGE_TOLERANCE_W = 100.0       # 11.4 secondary check, direction only: register 10008 (positive = discharging) above this is a discharge
FIRMWARE_PROTECTION_W = 500.0         # high-power grid charging that nobody commanded is the firmware's own doing: do not fight it (10.4)
LATCH_VERSION = 1
LATCH_RETRY_S = 10.0
ZERO_RETRY_S = 5.0                 # a protective 0 W that was not confirmed is attempted again this often while HANDS_OFF holds (GitHub 49)

CHARGE, ZERO_OUTPUT, DISCHARGE, FAULT = "CHARGE", "ZERO_OUTPUT", "DISCHARGE", "FAULT"

SPEC = Spec("action", ("startup", "running", "released", "hold", "hands_off", "refused"), "startup", (
    ("checks_pass", "startup", "running"),
    ("checks_fail", "startup", "refused"),
    ("release", "running", "released"),
    ("valid_again", ["released", "hold"], "running"),
    ("verify_failed", "running", "hold"),
    ("hold_failed", "hold", "hands_off"),
    ("too_many_reversions", "running", "hands_off"),
    ("latched", ["startup", "running", "released", "hold"], "hands_off"),
    ("reset", "hands_off", "startup"),
))


@dataclass(frozen=True)
class Write:
    register: int
    value: int
    reason: str


class _Model:
    pass


class ActionMachine:
    def __init__(self, latch_path=None, fallback_latch_path=None, default_mode=MODE_DEFAULT, default_proven=False, now=0.0, verify=True, wall=time.time):
        self.wall = wall                        # wall-clock seconds: the latch records it so the owner's reset request (a file stamped in wall time) can be compared with it
        self.verify = verify                   # False in the logged rehearsal: nothing is sent, so there is no effect to verify and no mode to supervise
        self.model = build(SPEC, _Model(), now=now)
        self.latch_path = Path(latch_path) if latch_path else None
        self.fallback_latch_path = Path(fallback_latch_path) if fallback_latch_path else None
        self.default_mode, self.default_proven = default_mode, default_proven
        self.alerts = deque(maxlen=50)         # (key, text) for the caller to raise through alerts.raise_alert; bounded, the oldest fall off
        self.records = deque(maxlen=50)        # observations for the write journal (10.4, 11.4; GitHub issue 72): not alerts, nothing is mailed
        self._queue = deque()                  # (due time, Write)
        self.mode = None                       # the mode we believe the device is in (last read, or last written)
        self.setpoint = None                   # the last set-point we wrote
        self._target = None                    # (mode, setpoint or None)
        self._failures = 0
        self._verify_due = None
        self._reversions = deque()
        self._valid_since = None
        self._last_mode_check = None
        self._used_resets = set()
        self._latch_pending = None
        self._latch_retry_at = None
        self.refused_reason = None
        self._acked = {}                         # register -> the last value that really went out
        self.may_be_driving = False              # True from the first write that may have reached the device until a release has been completed
        self._release_retry_at = 0.0
        self._zero_due, self._zero_retry_at = False, 0.0       # HANDS_OFF's protective 0 W is owed until a 0 W write has been acknowledged
        if self._read_latch() is not None:                # LC4: a latch (present, unreadable, corrupt or of another version) means latched; nothing is written
            fire(self.model, "latched", now=now)
            self.alerts.append(("hands-off-latched", "The controller found its HANDS_OFF latch and is not writing anything until it is reset"))

    # ---- state ------------------------------------------------------------------------------------------------------------------------------
    @property
    def state(self):
        return self.model.state

    def sent(self, write):
        self._acked[write.register] = write.value
        if write.register == SETPOINT_REGISTER and write.value == 0:
            self._zero_due = False                                 # the protective 0 W went out

    def forget(self, register=None):
        """What was last written may not have reached the device: believe nothing, so the next tick plans from 0 W again. With `register`, only that register's belief is dropped:
        the commissioning switch held ONE write back, and the writes already sent before it (the 0 W) are still true, so they are not repeated every second."""
        if register == MODE_REGISTER:
            self.mode = None
        elif register == SETPOINT_REGISTER:
            self.setpoint = 0 if self._acked.get(SETPOINT_REGISTER) == 0 else None      # a 0 W that really went out is still true; anything else is unknown
        else:
            self.mode = self.setpoint = None
        self._target = None
        self._queue.clear()
        self._verify_due = None

    # ---- the latch (LC4) -----------------------------------------------------------------------------------------------------------------
    def _read_latch(self):
        """None if there is no latch; else a dict (a corrupt, unreadable or unknown-version file is the latch `latch_unreadable`)."""
        for path in (self.latch_path, self.fallback_latch_path):
            if path is None or not path.exists():
                continue
            try:
                document = json.loads(path.read_text())
                if isinstance(document, dict) and document.get("version") == LATCH_VERSION and "at" in document:
                    return document
            except (OSError, ValueError):
                pass
            return {"version": LATCH_VERSION, "cause": "latch_unreadable", "at": 0.0}
        return None

    def _write_latch(self, document):
        """"durable" when the main file was written, "ram" when only the fallback was, None when neither was. Only "durable" ends the retries."""
        for index, path in enumerate((self.latch_path, self.fallback_latch_path)):
            if path is None:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + ".tmp")
                with open(temporary, "w") as handle:
                    handle.write(json.dumps(document)); handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary, path)
                self._sync_directory(path.parent)
                return "durable" if index == 0 else "ram"
            except OSError:
                continue
        return None

    @staticmethod
    def _sync_directory(directory):
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _enter_hands_off(self, now, cause):
        self._queue.clear()
        document = {"version": LATCH_VERSION, "cause": cause, "at": now, "wall": self.wall()}
        written = self._write_latch(document)
        if written != "durable":
            self._latch_pending, self._latch_retry_at = document, now + LATCH_RETRY_S      # held in memory; retried until the durable file is written; the controller alerts and does not exit
            self.alerts.append(("latch-not-saved", "HANDS_OFF holds, but the durable latch file could not be written" + (" (a copy is in RAM)" if written else "")))
        self.alerts.append(("hands-off", f"The controller stopped writing to the battery (HANDS_OFF: {cause}) and will stay that way until you reset it"))
        self._zero_due = self.setpoint != 0                       # a 0 W not yet known to have gone out is OWED: retried until acknowledged, and it is the only write HANDS_OFF ever makes
        self._zero_retry_at = now + ZERO_RETRY_S
        return [Write(SETPOINT_REGISTER, 0, "HANDS_OFF: set-point 0 W, then no further writes")] if self._zero_due else []

    def _owed_zero(self, now):
        """The protective 0 W while HANDS_OFF holds and it has not been acknowledged (a dropped connection, a held-back write): this and nothing else, at most every ZERO_RETRY_S."""
        if self.state != "hands_off" or not self._zero_due or now < self._zero_retry_at:
            return []
        self._zero_retry_at = now + ZERO_RETRY_S
        self.alerts.append(("hands-off-zero-not-confirmed", "HANDS_OFF holds but the protective 0 W set-point has not been confirmed: retrying it (nothing else is written)"))
        return [Write(SETPOINT_REGISTER, 0, "HANDS_OFF: the protective 0 W was not confirmed, trying again")]

    def reset(self, now, request_id, request_at, checks_ok, valid):
        """The owner's reset (LC4): honoured only if the request is newer than the latch and unused, the 11.5 checks pass and the data is valid."""
        latch = self._read_latch() or self._latch_pending
        if self.state != "hands_off" or request_id in self._used_resets:
            return False
        if latch is not None and request_at <= latch.get("wall", latch.get("at", 0.0)):
            return False
        if not (checks_ok and valid):
            return False
        self._used_resets.add(request_id)
        for path in (self.latch_path, self.fallback_latch_path):
            try:
                if path is not None:
                    path.unlink()
            except OSError:
                pass
        self._latch_pending = None
        self._zero_due = False
        fire(self.model, "reset", now=now)
        self.mode = self.setpoint = self._target = None
        return True

    # ---- startup (11.5) ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def mask_ok(mode_mask):
        """The device allows the modes the controller drives: bit 0 and bit 5 of the mode mask."""
        return isinstance(mode_mask, int) and not isinstance(mode_mask, bool) and bool(mode_mask & 1 and mode_mask & (1 << 5))

    def safety_violated(self, now, reason):
        """A start-up condition stopped holding while running (Backup Reserve switched on in the Anker app, a mode no longer allowed): HANDS_OFF, with the protective 0 W owed."""
        if self.state not in ("running", "released", "hold"):
            return []
        fire(self.model, "latched", now=now)
        self.alerts.append(("safety-violated", f"The controller stopped: {reason}"))
        return self._enter_hands_off(now, "safety_register")

    def startup(self, now, mode_mask, backup_soc_enable, mode):
        """Run the checks. Returns True when the machine may run. A refusal says why in `refused_reason`."""
        if self.state != "startup":
            return self.state == "running"
        if not self.mask_ok(mode_mask):
            self.refused_reason = "the device's mode mask lacks bit 0 or bit 5"
        elif backup_soc_enable != 0:
            self.refused_reason = "backup reserve is enabled in the Anker app: it overrides third-party control"
        if self.refused_reason:
            fire(self.model, "checks_fail", now=now)
            self.alerts.append(("startup-refused", f"The controller refuses to run: {self.refused_reason}"))
            return False
        self.mode = mode
        fire(self.model, "checks_pass", now=now)
        return True

    # ---- the release (LC1, LC2, D9) ------------------------------------------------------------------------------------------------------
    def release_writes(self):
        """The two writes of a release or an exit: set-point 0 W, then the default mode (mode 0 and an alert while a default other than mode 0 is
        unproven). With the default mode 0 itself (GitHub issue 78) there is nothing unproven to fall back from, so no alert (issue 83)."""
        mode = self.default_mode if self.default_proven else MODE_SELF_CONSUMPTION
        if not self.default_proven and self.default_mode != MODE_SELF_CONSUMPTION:
            self.alerts.append(("default-mode-unproven", "The default mode has not been proven on this device yet: released to mode 0 instead"))
        return [Write(SETPOINT_REGISTER, 0, "release: set-point 0 W first"), Write(MODE_REGISTER, mode, "release: the default mode")]

    def exit_writes(self):
        """What a stopping controller writes (LC2). Nothing when refused; when latched only the protective 0 W, and only if it is still owed."""
        if self.state == "hands_off":
            return [Write(SETPOINT_REGISTER, 0, "HANDS_OFF at exit: the protective 0 W was not confirmed")] if self._zero_due else []
        if self.state == "refused":
            return []
        self._queue.clear()
        writes = self.release_writes()
        self.setpoint, self.mode = 0, writes[1].value
        return writes

    def device_refused_default_mode(self):
        """The device did not take the default mode: write mode 0 instead and say so (LC5)."""
        self.alerts.append(("default-mode-refused", "The device refused the default mode: released to mode 0"))
        self.mode = MODE_SELF_CONSUMPTION
        return [Write(MODE_REGISTER, MODE_SELF_CONSUMPTION, "the default mode was refused")]

    # ---- targets and sequencing (10.2) --------------------------------------------------------------------------------------------------
    @staticmethod
    def target_for(action, p_set_w):
        if action == CHARGE:
            return MODE_THIRD_PARTY, -int(p_set_w) if p_set_w >= P_MIN_W else 0
        if action in (ZERO_OUTPUT, "HOLD"):
            return MODE_THIRD_PARTY, 0
        return MODE_SELF_CONSUMPTION, None                                         # DISCHARGE: the firmware matches house demand

    def _plan(self, now, target, why):
        """Replace whatever is queued with the steps that take the device from what we believe it is now to `target` (10.2): set-point 0 W, then the mode if it changes, wait the settle
        time, then the new set-point. A direction change passes through 0 W. An unknown set-point is written as 0 W before a mode change."""
        mode, setpoint = target
        steps = []
        if self.mode != mode:
            if self.setpoint != 0:
                steps.append((now, Write(SETPOINT_REGISTER, 0, f"{why}: through 0 W first")))
            steps.append((now, Write(MODE_REGISTER, mode, why)))
            if setpoint not in (None, 0):
                steps.append((now + MODE_SETTLE_S, Write(SETPOINT_REGISTER, setpoint, why)))
        elif setpoint is not None and setpoint != self.setpoint:
            if setpoint != 0 and (self.setpoint is None or (self.setpoint != 0 and (setpoint < 0) != (self.setpoint < 0))):          # an unknown set-point is passed through 0 W too
                steps.append((now, Write(SETPOINT_REGISTER, 0, f"{why}: through 0 W first")))
            steps.append((now, Write(SETPOINT_REGISTER, setpoint, why)))
        self._queue = deque(steps)
        self._target = target
        self._verify_due = steps[-1][0] + VERIFY_S if steps else None
        return steps

    def _due(self, now):
        out = []
        while self._queue and self._queue[0][0] <= now:
            _, write = self._queue.popleft()
            if write.register == MODE_REGISTER:
                self.mode = write.value
            else:
                self.setpoint = write.value
            out.append(write)
        return out

    # ---- verification (11.4) ------------------------------------------------------------------------------------------------------------
    def _verified(self, drawn_w, relax=False, dc_w=None):
        mode, setpoint = self._target
        if mode == MODE_SELF_CONSUMPTION:
            return True                                                            # DISCHARGE: no check
        # 11.4's secondary check, direction only (GitHub issue 72): CHARGE and ZERO_OUTPUT both mean the battery is not discharging. Lenient on purpose
        # (PV charging and conversion losses move the DC figure; a full battery may show nothing), so it only catches a battery that is plainly discharging.
        # Unknown (no reading) is primary-only, as before.
        secondary = dc_w is None or dc_w <= DC_DISCHARGE_TOLERANCE_W
        wanted = -setpoint if setpoint else 0
        if wanted >= P_MIN_W:
            if relax:
                return drawn_w > 0.0 and secondary     # GitHub issue 66: tapering still draws SOME power; a discharge or nothing is still a failure even relaxed
            return abs(drawn_w - wanted) <= max(VERIFY_TOLERANCE_W, VERIFY_TOLERANCE_PCT * wanted) and secondary
        return drawn_w <= VERIFY_TOLERANCE_W and secondary

    # ---- the tick -------------------------------------------------------------------------------------------------------------------------
    def tick(self, now, valid, action, p_set_w=0.0, drawn_w=0.0, relax_magnitude=False, dc_w=None):
        """One control step. `valid` is VALID (11.2), `action` the decision table's output, `p_set_w` the garage regulator's set-point,
        `drawn_w` the AC power the Solarbank draws from the grid (positive; this installation's sign is applied by the caller). Returns the writes due now."""
        writes = []
        if self._latch_pending is not None and now >= (self._latch_retry_at or 0):
            if self._write_latch(self._latch_pending) == "durable":
                self._latch_pending = None
            else:
                self._latch_retry_at = now + LATCH_RETRY_S
        if self.state == "hands_off":
            return self._owed_zero(now)
        if self.state in ("refused", "startup"):
            return []
        if valid:
            self._valid_since = self._valid_since if self._valid_since is not None else now
        else:
            self._valid_since = None
        if self.state == "running" and (not valid or action == FAULT):
            fire(self.model, "release", now=now)
            self._queue.clear(); self._target = None
            writes = self.release_writes()
            self.setpoint, self.mode = 0, writes[1].value
            return writes
        if self.state == "released":
            if valid and now - self._valid_since >= RETURN_VALID_S:
                fire(self.model, "valid_again", now=now)
                self._target = None
            else:
                if (self.setpoint is None or self.mode is None) and now >= self._release_retry_at:      # the release did not get through (a failed or held-back
                    # write, of either register: GitHub issue 63 found a failed default-mode write was never tried again): try it again
                    self._release_retry_at = now + RELEASE_RETRY_S
                    writes = self.release_writes()
                    self.setpoint, self.mode = 0, writes[1].value
                    return writes
                return []
        if self.state == "hold":
            action, p_set_w = "HOLD", 0.0
            if valid and self._valid_since is not None and now - self._valid_since >= RETURN_VALID_S and self._verified_ok_recently:
                fire(self.model, "valid_again", now=now); self._failures = 0
        target = self.target_for(action, p_set_w)
        if target != self._target:
            self._plan(now, target, f"{action}" if self.state != "hold" else "HOLD")
            self._failures = 0
        writes += self._due(now)
        if self.verify and self._target is not None and not self._queue and self._verify_due is not None and now >= self._verify_due:
            if self._verified(drawn_w, relax_magnitude, dc_w):
                self._failures, self._verify_due = 0, None
                self._verified_ok_recently = True
            else:
                self._failures += 1
                self._verified_ok_recently = False
                self._verify_due = now + VERIFY_S
                if self._failures >= 2:
                    self._failures = 0
                    if self.state == "running":
                        fire(self.model, "verify_failed", now=now)
                        self.alerts.append(("verify-failed", "A command did not take effect twice in a row: holding at 0 W"))
                        self._plan(now, self.target_for("HOLD", 0.0), "HOLD")
                    elif self.state == "hold":
                        fire(self.model, "hold_failed", now=now)
                        writes += self._enter_hands_off(now, "hold_failed")
        return writes

    _verified_ok_recently = False

    # ---- mode supervision (10.4) -------------------------------------------------------------------------------------------------------
    def supervise(self, now, mode_read, drawn_w, commanded_charge, status=None, dc_w=None):
        """Every MODE_CHECK_S the caller reads 10064 and passes it. Returns writes (a re-assertion) or []. A reversion with high grid charging nobody commanded is the firmware's own
        protection: it is reported, not fought."""
        if not self.verify or self.state != "running" or self._target is None or mode_read is None:
            return []
        if self._last_mode_check is not None and now - self._last_mode_check < MODE_CHECK_S:
            return []
        self._last_mode_check = now
        required = self._target[0]
        if mode_read == required:
            return []
        # 10.4: record the mode found, the battery status, P_batt_dc and P_batt_ac (GitHub issue 72: only the AC power was kept, and only when it was the firmware)
        found = f"mode {mode_read} found (required {required}), battery status {status}, DC {dc_w} W, AC drawn {drawn_w:.0f} W"
        self.records.append({"record": "mode_reversion", "mode_found": mode_read, "mode_required": required, "battery_status": status, "dc_w": dc_w,
                             "ac_drawn_w": round(drawn_w, 1), "at": now})
        if drawn_w > FIRMWARE_PROTECTION_W and not commanded_charge:
            self.alerts.append(("firmware-override", f"The battery is charging at {drawn_w:.0f} W from the grid with no command: a firmware protection (not overridden); {found}"))
            return []
        while self._reversions and now - self._reversions[0] > 3600:
            self._reversions.popleft()
        self._reversions.append(now)
        self.mode = mode_read                                                     # what the device says it is in, so the plan starts from the truth
        if len(self._reversions) > MODE_REVERT_LIMIT:
            fire(self.model, "too_many_reversions", now=now)
            return self._enter_hands_off(now, "mode_reversions")
        steps = self._plan(now, self._target, "re-assert the mode")
        return self._due(now) if steps else []
