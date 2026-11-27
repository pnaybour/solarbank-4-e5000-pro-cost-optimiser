"""The live transport (Stage C): turns the controller's channel commands into the register writes of the action machine, and keeps a journal of every write.

THREE LEVELS, each stricter than the one before, so nothing is sent by accident:
  1. `logged`   the machine runs, every register write it would make is journaled as `would_write`, and nothing is sent. `sends` is False, so the controller decides exactly as in shadow.
  2. `live`     writes are sent, but ONLY for the write classes that the commissioning switch lists. A write the switch does not allow is journaled as `would_write` with the reason, never sent,
                and the machine forgets its belief about the device so that re-enabling the switch re-plans from 0 W.
  3. the `live` transport is built by live_gate.make_factory and is part of every release, but it is only returned when the three root-owned conditions hold (the mode file says `live`, the write-switch lists the class, commissioning.json is valid for this code); otherwise the controller runs `logged`.

The commissioning switch is a small file only root can edit (`write-switch` in the state folder, one word per line): `mode` allows writes to register 10064, `setpoint` allows writes to 10071.
A missing, empty or unreadable file allows nothing. It is read every cycle, so removing a word (or emptying the file) takes effect on the next write: that is the off switch.

Mapping from channel commands to the machine's action (the table of owner specification v1.3 section 7): an `ac_charge` command CHARGE_LIMIT with watts above 0 is CHARGE; otherwise a `discharge`
INHIBIT (or an ac_charge STOP) is ZERO_OUTPUT; otherwise DISCHARGE; `control` RELEASE is the release (set-point 0 W, then the default mode). Standard library only here; the writer is injected."""
import json
import os
from pathlib import Path
import time

from .action_machine import (CHARGE, DC_DISCHARGE_TOLERANCE_W, P_MIN_W, VERIFY_S, ActionMachine, DISCHARGE, MODE_REGISTER, RELEASE_RETRY_S,
                             SETPOINT_REGISTER, ZERO_OUTPUT)
from .control_exec import Outcome
from .shelly_channels import SOLARBANK_AC

CLASSES = {MODE_REGISTER: "mode", SETPOINT_REGISTER: "setpoint"}
SAFETY_MAX_AGE_S = 120.0       # the mask and Backup Reserve readings (GitHub issue 51) older than this are treated as unknown
SETTINGS_LOG_MAX_AGE_S = 3900.0   # the collector's settings log is a fallback only: it logs on change and hourly, so a record older than the heartbeat plus a margin says nothing
FAILURES_BEFORE_RELEASE = 5      # consecutive cycles in which the transport raised before the controller releases the battery itself (GitHub issue 41)
MODE_MAX_AGE_S = 15.0         # a mode reading older than this is treated as unknown
REQUIRED_DEVICES = ("solarbank", "shelly")


def read_switch(path):
    """The set of write classes the commissioning switch allows. Anything wrong with the file means none."""
    from .trusted_files import read_trusted
    text = read_trusted(path)
    if text is None:
        return frozenset()
    words = {line.strip().lower() for line in text.splitlines()}
    return frozenset(w for w in words if w in CLASSES.values())


class WriteJournal:
    """One JSON line per write attempt (`would_write`, `wrote`, `failed`), size-capped with one rotation."""

    def __init__(self, path, max_bytes=4 * 1024 * 1024):
        self.path, self.max_bytes, self.errors = Path(path) if path else None, max_bytes, 0

    def record(self, entry):
        if self.path is None:
            return
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            self.errors += 1


class LiveTransport:
    simulated = False

    def __init__(self, machine, journal, writer=None, switch_path=None, device_info=None, clock=time.time, send=False, run_id="", alert_dir=None, reset_path=None, target_ok=None, reset_used_path=None,
                 commissioned=None):
        self.target_ok = target_ok                  # () -> the address the Solarbank was just recognised at (or True), or None/False (GitHub 38, 50)
        self.reset_path = reset_path
        self.alert_dir = alert_dir
        self.reset_used_path = reset_used_path     # the ids of reset requests already answered (the request file itself is root's and is never deleted by the controller)
        self.machine, self.journal, self.writer, self.switch_path = machine, journal, writer, switch_path
        self.commissioned = commissioned            # callable -> the write classes the commissioning record allows (GitHub issue 76); None: the switch alone decides
        self.device_info, self.clock, self.run_id = device_info, clock, run_id
        self.sends = bool(send)                     # False: logged. The Executor then treats every command as suppressed and verifies nothing.
        self._charge_w, self._inhibit, self._stopped, self._released = 0.0, False, False, False
        self._valid, self._drawn_w = False, 0.0
        self._started = False
        self._heard = False
        self.blocked = 0
        self._release_done = False
        self.failures, self._failure_release_done = 0, False             # consecutive cycles in which observe() raised (GitHub issue 41)
        self._failed_writes, self._write_fault_alerted = {}, False       # register -> consecutive REFUSED or failed writes (GitHub issue 63); distinct from `failures`
                                                                          # above, which counts observe() itself raising. Counted per register: a 0 W that still gets
                                                                          # through must not hide a mode write that keeps failing beside it
        self._release_at, self._release_fallback = None, False          # one release episode: when it was last issued, and whether the mode-0 fallback was used
        self._next_supervision = 0.0
        self._mode_now = None
        self._record_since = {}                    # GitHub issue 72: record kind -> when its condition began (cleared when it ends); recorded once it has lasted VERIFY_S

    # ---- what the Executor calls ---------------------------------------------------------------------------------------------------------
    def send(self, command, timeout):
        self._heard = True                                   # the controller has said what it wants: until then the machine writes nothing (no guessing at start-up)
        if command.channel == "control" and command.action == "RELEASE":
            if not self._released:                           # a new release episode: a refresh of the same release is not a new one
                self._release_at, self._release_fallback, self._release_done = None, False, False
            self._released = True
        else:
            self._released = False
            self._release_at, self._release_fallback = None, False
            if command.channel == "ac_charge":
                was_charging = self._charge_w > 0
                self._charge_w = float(command.params.get("watts", 0)) if command.action == "CHARGE_LIMIT" else 0.0
                # A protective STOP holds the battery (mode 3, 0 W) only when a charge was under way or the hold is already in force. A STOP while nothing was charging (GitHub issue 62:
                # a one-second stale garage reading) leaves the mode alone, so a reading gap never writes mode 3 and then mode 0. Charging stays blocked all the same: charge_w is 0.
                self._stopped = command.action == "STOP" and (was_charging or self._stopped)
            elif command.channel == "discharge":
                self._inhibit = command.action == "INHIBIT"
        if self.machine.state in ("hands_off", "refused"):
            return Outcome("rejected", f"the action machine is {self.machine.state}")
        if not self.sends:
            return Outcome("suppressed", "logged: register writes are journaled, nothing is sent")
        return Outcome("acknowledged", "accepted by the action machine; its register writes follow")

    @property
    def _write_failures(self):
        """The most consecutive failures of any one register."""
        return max(self._failed_writes.values(), default=0)

    def effect(self, record, inputs):
        """Did the command take effect? From the measured AC power of the Solarbank (11.4). Used only when `sends` is True."""
        drawn = self._drawn(inputs)
        action, params = record["action"], record["params"]
        if action in ("STOP", "WITHDRAW"):
            return drawn <= 150.0
        if action == "CHARGE_LIMIT":
            wanted = float(params.get("watts", 0))
            if self._relax_magnitude(inputs, wanted):
                return drawn > 0.0 and self._not_discharging(inputs)      # GitHub issue 66: tapering still charges, at some rate; a discharge or nothing is still a failure
            return abs(drawn - wanted) <= max(150.0, 0.2 * wanted) and self._not_discharging(inputs)
        if action == "INHIBIT":
            return self._output(inputs) <= 150.0 and drawn <= 150.0 and self._not_discharging(inputs)    # discharge must actually have stopped (and nothing is charging)
        if action == "RELEASE":
            if not self._release_done:                                        # a held-back or failed release is never "observed"
                return False
            return self._mode_now is not None and self._mode_now == self.machine.mode      # and the writes returning is not enough: the device must report the mode we released it to
        return True

    # ---- every cycle ---------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _drawn(inputs):
        value = (inputs.get("shelly") or {}).get("value") or {}
        watts = value.get(SOLARBANK_AC)
        return max(0.0, -float(watts)) if isinstance(watts, (int, float)) else 0.0         # this installation's sign: AC input into the Solarbank is negative

    @staticmethod
    def _dc(inputs):
        """The battery's DC power (register 10008; the reader's sign: positive = discharging), or None when unknown."""
        value = (inputs.get("solarbank") or {}).get("value") or {}
        watts = value.get("battery")
        return float(watts) if isinstance(watts, (int, float)) and not isinstance(watts, bool) and watts == watts else None

    @classmethod
    def _not_discharging(cls, inputs):
        """Specification 11.4's secondary check, direction only (GitHub issue 72). An unknown DC reading leaves the primary check alone."""
        dc = cls._dc(inputs)
        return dc is None or dc <= DC_DISCHARGE_TOLERANCE_W

    def _records(self, now, action, p_set, inputs):
        """Specification 11.4's two records (GitHub issue 72), written to the journal once a condition has lasted VERIFY_S (a command just sent is not yet in
        effect) and again only after it has cleared: "possible sign inversion" when a charge is commanded and the AC power shows the Solarbank supplying,
        "possible firmware override" when it draws from the grid during ZERO_OUTPUT. Live only: nothing is commanded in logged mode."""
        if not self.sends or self.machine.state != "running":
            self._record_since.clear()
            return
        conditions = {
            "possible sign inversion": action == CHARGE and p_set >= P_MIN_W and self._output(inputs) > 150.0,
            "possible firmware override": action == ZERO_OUTPUT and self._drawn(inputs) > 150.0,
        }
        for kind, holds in conditions.items():
            if not holds:
                self._record_since.pop(kind, None)
                continue
            began = self._record_since.setdefault(kind, now)
            if began is not None and now - began >= VERIFY_S:
                self._record_since[kind] = None                        # recorded: not again until the condition has cleared
                self._journal_record({"record": kind, "action": action, "p_set_w": p_set, "ac_drawn_w": round(self._drawn(inputs), 1),
                                      "ac_supplied_w": round(self._output(inputs), 1), "dc_w": self._dc(inputs), "at": now})

    def _journal_record(self, record):
        self.journal.record({"t": round(record.get("at", 0.0), 3), "wall": round(self.clock(), 3), "run": self.run_id, "kind": "record",
                             "state": self.machine.state, **record})

    @staticmethod
    def _relax_magnitude(inputs, commanded_w):
        """Specification 11.4, GitHub issue 66: the firmware tapers charging near its own limit, so the magnitude check is relaxed (not dropped: a
        charge that shows no current at all, or a discharge, is still a failure either way) when the battery is within 3 % of the device's own
        charging limit (D13's own range check: an absurd register value is ignored, the full 100 % used instead) or the device's own live charge
        limit (register 10036) is below what was commanded."""
        if not (isinstance(commanded_w, (int, float)) and not isinstance(commanded_w, bool) and commanded_w > 0):
            return False
        value = (inputs.get("solarbank") or {}).get("value") or {}
        soc, limit, max_live = value.get("soc"), value.get("charging_limit_soc"), value.get("max_charge")
        num = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else None
        soc, limit, max_live = num(soc), num(limit), num(max_live)
        limit = limit if limit is not None and 80 <= limit <= 100 else 100.0
        return (soc is not None and soc >= limit - 3.0) or (max_live is not None and max_live < commanded_w)

    @staticmethod
    def _live_mode(inputs):
        """The Solarbank's operating mode as the controller's own reader last read it (a few seconds old at most), or None when unknown (GitHub issue 36)."""
        value = (inputs.get("solarbank") or {}).get("value") or {}
        mode = value.get("mode")
        return mode if isinstance(mode, int) and not isinstance(mode, bool) and (value.get("mode_age_s") or 0) <= MODE_MAX_AGE_S else None

    @staticmethod
    def _live_safety(inputs):
        """The mode mask and the Backup Reserve switch as the controller's own reader last read them (GitHub issue 51), or None when unknown or old."""
        value = (inputs.get("solarbank") or {}).get("value") or {}
        mask, backup = value.get("mask"), value.get("backup_soc_enable")
        if all(isinstance(x, int) and not isinstance(x, bool) for x in (mask, backup)) and (value.get("safety_age_s") or 0) <= SAFETY_MAX_AGE_S:
            return {"mask": mask, "backup_soc_enable": backup}
        return None

    def _settings_now(self, inputs):
        """The device settings the start-up checks run on: what the controller itself read a few seconds ago; only when it has no such reading, the collector's log (refused
        when older than SETTINGS_LOG_MAX_AGE_S). A live reading always wins over the log: they cannot disagree and both be believed."""
        safety = self._live_safety(inputs)
        if safety is not None:
            info = self.device_info() if self.device_info else None
            mode = self._mode_now if self._mode_now is not None else (info or {}).get("mode")
            return {**safety, "mode": mode}
        return self.device_info() if self.device_info else None

    @staticmethod
    def _output(inputs):
        value = (inputs.get("shelly") or {}).get("value") or {}
        watts = value.get(SOLARBANK_AC)
        return max(0.0, float(watts)) if isinstance(watts, (int, float)) else 0.0        # positive: the Solarbank is supplying power

    def observe(self, inputs, now):
        """Called by the Executor each cycle with the fresh measurements: start the machine when the device settings are known, run it, and perform or journal its writes.
        Whatever the machine wants the owner told is raised as an alert file every cycle, so a latch or a refusal is never silent."""
        try:
            self._honour_reset(now, inputs)
            written = self._observe(inputs, now)
            self.failures, self._failure_release_done = 0, False
            return written
        except Exception as error:
            self.failures += 1
            self.machine.alerts.append(("transport-error", f"The live transport failed ({self.failures} cycles in a row) and wrote nothing this cycle: {error.__class__.__name__}: {error}"))
            self._release_after_repeated_failures(now)
            raise
        finally:
            self._raise_machine_alerts()
            while getattr(self.machine, "records", None):
                self._journal_record(self.machine.records.popleft())

    def _release_after_repeated_failures(self, now):
        """After FAILURES_BEFORE_RELEASE failing cycles in a row, put the battery back (set-point 0 W, then the default mode) once, whatever state the transport is in, and say so:
        a controller that cannot run its own machine must not leave the battery on its last command (GitHub issue 41)."""
        if self.failures < FAILURES_BEFORE_RELEASE or self._failure_release_done:
            return
        self._failure_release_done = True
        self.machine.alerts.append(("transport-failing", f"The live transport has failed {self.failures} cycles in a row: releasing the battery (0 W, then the default mode)"))
        try:
            done = self._perform(now, self.machine.exit_writes())
            if self.sends and done and not self._last_failed:
                self.machine.may_be_driving = False
        except Exception:
            self.machine.alerts.append(("transport-failing", "The release after repeated transport failures also failed"))

    def health(self):
        return {"transport_failures": self.failures, "write_failures": self._write_failures}

    def _used_reset_ids(self):
        try:
            data = json.loads(Path(self.reset_used_path).read_text()) if self.reset_used_path else []
            return {str(x) for x in data} if isinstance(data, list) else set()
        except (OSError, ValueError):
            return set()

    def _mark_reset_used(self, request_id):
        if not self.reset_used_path:
            return
        used = sorted(self._used_reset_ids() | {request_id})[-50:]
        try:
            temporary = Path(self.reset_used_path).with_name(Path(self.reset_used_path).name + ".tmp")
            temporary.write_text(json.dumps(used))
            os.replace(temporary, self.reset_used_path)
        except OSError:
            pass

    def _honour_reset(self, now, inputs):
        """The owner's `solarbank control reset` leaves a request file, owned by root in root's folder and read like the other authorisation files (an untrustworthy one is
        ignored, GitHub 57). While HANDS_OFF the machine honours it only if it is newer than the latch, not used before, the start-up checks hold on the device's settings and
        the measurements are valid. The request's id is recorded as answered either way, so one request is answered once."""
        if not self.reset_path or self.machine.state != "hands_off":
            return
        from .trusted_files import read_trusted
        text = read_trusted(self.reset_path)
        if text is None:
            return
        try:
            request = json.loads(text)
            request_id, request_at = str(request["id"]), float(request["at"])
        except (ValueError, KeyError, TypeError):
            return
        if request_id in self._used_reset_ids():
            return
        info = self._settings_now(inputs)
        mask = (info or {}).get("mask")
        checks = bool(info) and ActionMachine.mask_ok(mask) and (info or {}).get("backup_soc_enable") == 0
        done = self.machine.reset(now, request_id, request_at, checks, self._valid)
        if done:
            self._started = False                            # run the start-up checks again before any write
        if done or request_at <= self.clock():
            self._mark_reset_used(request_id)
        if not done:
            self.machine.alerts.append(("reset-refused", "The reset request was not honoured: it must be newer than the stop, the device settings must pass the checks and the readings must be valid"))

    def _raise_machine_alerts(self):
        alerts = getattr(self.machine, "alerts", [])
        pending = list(alerts)
        alerts.clear()
        if not pending or not self.alert_dir:
            return
        from .alerts import raise_alert
        seen = set()
        for key, text in pending[-20:]:
            if key not in seen:
                seen.add(key)
                raise_alert(self.alert_dir, f"machine-{key}", f"Solarbank controller: {key}", text)

    def _observe(self, inputs, now):
        if not self._heard:
            return []
        # GitHub issue 63: specification 11.2's own fourth condition for VALID being FALSE, "a Modbus write has failed twice in succession", folded
        # into the same flag the readings' own freshness already drives: the machine's existing D9 release path, and its RELEASE_RETRY_S/RETURN_VALID_S
        # retry and recovery timing, then apply unchanged.
        self._valid = all(name in inputs for name in REQUIRED_DEVICES) and self._write_failures < 2
        self._drawn_w = self._drawn(inputs)
        self._mode_now = self._live_mode(inputs)
        if not self._started:
            info = self._settings_now(inputs)
            live = self._mode_now if self._mode_now is not None else (info or {}).get("mode")
            if not info or not self.machine.startup(now, info.get("mask"), info.get("backup_soc_enable"), live):
                return []
            self._started = True
        else:
            safety = self._live_safety(inputs)                       # while running: the owner enabling Backup Reserve in the Anker app overrides third-party control
            if safety is not None and not (ActionMachine.mask_ok(safety["mask"]) and safety["backup_soc_enable"] == 0):
                return self._perform(now, self.machine.safety_violated(now, f"the device's settings changed while running (mask {safety['mask']}, backup reserve enable {safety['backup_soc_enable']})"))
        if self._released:
            return self._release(now)
        self._release_done = False
        supervised = []
        if self.sends and self.device_info and now >= self._next_supervision:
            self._next_supervision = now + 10.0
            status = ((inputs.get("solarbank") or {}).get("value") or {}).get("status_code")
            supervised = self._perform(now, self.machine.supervise(now, self._mode_now, self._drawn_w, self._charge_w > 0, status=status, dc_w=self._dc(inputs)))
        if self._charge_w > 0:
            action, p_set = CHARGE, self._charge_w
        else:
            action, p_set = (ZERO_OUTPUT if (self._inhibit or self._stopped) else DISCHARGE), 0.0
        relax = self._relax_magnitude(inputs, p_set)              # GitHub issue 66
        self._records(now, action, p_set, inputs)                 # GitHub issue 72
        return supervised + self._perform(now, self.machine.tick(now, self._valid, action, p_set, self._drawn_w, relax, self._dc(inputs)))

    def _release(self, now):
        """One release EPISODE (GitHub issue 52). The sequence 0 W then the default mode is written ONCE; after that nothing is written while the read-back confirms the mode.
        If both writes were acknowledged but the device still reports another mode after RELEASE_RETRY_S, the default mode is treated as refused (mode 0, once, with an alert);
        a release that did not get through is attempted again no more often than RELEASE_RETRY_S. Logged mode issues it once per episode and verifies nothing."""
        # GitHub issue 63: the machine may already be in "released" on its own (two consecutive write failures made the data invalid, D9) by the time
        # an explicit release is also asked for; that is still an episode this method can carry out, with its own retry bookkeeping below.
        if self.machine.state not in ("running", "released"):
            return []
        confirmed = self.sends and self._release_done and self._mode_now is not None and self._mode_now == self.machine.mode
        if confirmed:
            return []
        if self._release_at is None:
            writes = self.machine.exit_writes()
        elif not self.sends or now - self._release_at < RELEASE_RETRY_S:
            return []                                                    # logged: once per episode; sending: wait for the read-back before anything else
        elif self._release_done and not self._release_fallback:
            self._release_fallback = True                                # acknowledged, yet the device did not enter the default mode: mode 0, once
            writes = self.machine.device_refused_default_mode()
        elif self._release_fallback:
            writes = self.machine.device_refused_default_mode()          # the mode-0 fallback did not show either: try it again, still no faster than every RELEASE_RETRY_S
        else:
            writes = self.machine.exit_writes()                          # the release did not get through (a failed or held-back write): try it again
        self._release_at = now
        done = self._perform(now, writes)
        self._release_done = self.sends and bool(writes) and len(done) == len(writes) and not self._last_failed
        if self._release_done:
            self.machine.may_be_driving = False
        return done

    def _allows(self, w):
        """Re-read the commissioning switch for THIS write (an owner emptying it takes effect before the next register operation). A zero set-point is protective:
        it is allowed when either class is, so the first live step (`mode` only) can make its transitions through 0 W; a non-zero set-point needs `setpoint`."""
        allowed = read_switch(self.switch_path) if self.switch_path else frozenset()
        if self.commissioned is not None:
            try:
                allowed = allowed & self.commissioned()             # in the switch AND commissioned for this code (GitHub issue 76)
            except Exception:
                allowed = frozenset()                               # the record cannot be read: nothing is sent
        kind = CLASSES[w.register]
        if kind in allowed:
            return True
        return kind == "setpoint" and w.value == 0 and "mode" in allowed

    def _perform(self, now, writes, fallback=False):
        done, self._last_failed = [], False
        for w in writes:
            kind = CLASSES[w.register]
            target = None
            entry = {"t": round(now, 3), "wall": round(self.clock(), 3), "run": self.run_id, "register": w.register, "class": kind, "value": w.value, "reason": w.reason, "state": self.machine.state}
            if not self.sends:
                self.journal.record({**entry, "kind": "would_write", "why_not_sent": "logged mode"})
            elif self.target_ok is not None and not (target := self.target_ok()):
                self.blocked += 1
                self.journal.record({**entry, "kind": "would_write", "why_not_sent": "the Solarbank at its current address has not been recognised as the commissioned device"})
                self.machine.forget(w.register)
                self._last_failed = True
                break
            elif self.writer is None or not self._allows(w):
                self.blocked += 1
                self.journal.record({**entry, "kind": "would_write", "why_not_sent": f"'{kind}' writes are not allowed: not in the commissioning switch, or that stage is not yet commissioned (sudo solarbank commission status)"})
                self.machine.forget(w.register)
                self._last_failed = True
                break                                                                           # the rest of this sequence depends on this write: do not send a half sequence
            else:
                self.machine.may_be_driving = True                                              # from here the device may be doing what we asked, acknowledged or not
                try:
                    write = self.writer.write_mode if w.register == MODE_REGISTER else self.writer.write_setpoint
                    write(w.value, host=target) if isinstance(target, str) else write(w.value)         # the address that was just verified, when the gate names one (GitHub 50)
                    self.journal.record({**entry, "kind": "wrote"})
                    self.machine.sent(w)
                    self._failed_writes.pop(w.register, None)                       # GitHub issue 63: THIS register answers again
                    if self._write_fault_alerted and self._write_failures < 2:      # and no register is failing any more: the fault is over, and so is its alert
                        self._write_fault_alerted = False                           # (nothing else withdraws a machine alert: it would be mailed again every six hours)
                        if self.alert_dir:
                            from .alerts import clear_alert
                            clear_alert(self.alert_dir, "machine-write-refused")
                except Exception as error:
                    self.journal.record({**entry, "kind": "failed", "error": f"{error.__class__.__name__}: {error}"})
                    # GitHub issue 63: only THIS register's belief is dropped (same reasoning as the two blocked-write cases above: whatever was sent
                    # before this write in the sequence really did go out, so it is not repeated every second while this one keeps failing).
                    self.machine.forget(w.register)
                    self._last_failed = True
                    self._failed_writes[w.register] = self._failed_writes.get(w.register, 0) + 1
                    if self._failed_writes[w.register] >= 2 and not self._write_fault_alerted:
                        self._write_fault_alerted = True
                        self.machine.alerts.append(("write-refused", f"Register {w.register} ({kind}) has been refused or failed twice in a row "
                                                     f"({error.__class__.__name__}: {error}): treating the data as invalid until a write succeeds again"))
                    if (w.register == MODE_REGISTER and w.value == getattr(self.machine, "default_mode", None) and getattr(error, "kind", "") == "exception"
                            and self.machine.state in ("running", "released") and not fallback):     # "released": the machine's own release (invalid data, D9)
                        fallback = True                                                         # the device itself refused the default mode (LC5): self-consumption (mode 0) instead
                        done += self._perform(now, self.machine.device_refused_default_mode(), fallback=True)
                    break
            done.append(w)
        return done

    _last_failed = False

    def on_exit(self, now=None):
        """LC2: write the release (0 W, then the default mode). Returns the writes performed or journaled. Skipped only when nothing this run wrote can still be acting
        on the device; a write whose acknowledgement was lost counts as one that may be."""
        if not getattr(self.machine, "may_be_driving", False):
            return []
        done = self._perform(self.clock() if now is None else now, self.machine.exit_writes())
        if self.sends and not self._last_failed and done:
            self.machine.may_be_driving = False
        return done


def device_info_from_log(path, max_age_s=SETTINGS_LOG_MAX_AGE_S, clock=time.time):
    """The device's settings as the collector last logged them (device-settings.jsonl), without opening another connection to the Solarbank (it allows only a few):
    {"mask": ems_mode_mask, "backup_soc_enable": ..., "mode": operating mode code}, or None if the file has nothing usable."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
            if isinstance(record.get("at"), (int, float)) and clock() - record["at"] > max_age_s:
                return None                          # the last look at the device's settings is too old to start on (the collector logs on change and hourly)
            decoded = record.get("decoded") or {}
            mask, mode = (decoded.get("ems_mode_mask") or {}).get("raw"), (decoded.get("operating_mode") or {}).get("code")
            backup = (decoded.get("limits") or {}).get("backup_soc_enable")
            if mask is not None and mode is not None and backup is not None:
                return {"mask": mask, "backup_soc_enable": backup, "mode": mode}
        except (ValueError, AttributeError):
            continue
    return None


def device_limits_from_log(path):
    """The charge, discharge and reserve limits the Solarbank itself reports (device-settings.jsonl, last usable line), in percent, or {}."""
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    for line in reversed(text.splitlines()):
        try:
            limits = (json.loads(line).get("decoded") or {}).get("limits")
            if isinstance(limits, dict) and limits:
                return {k: v for k, v in limits.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        except (ValueError, AttributeError):
            continue
    return {}
