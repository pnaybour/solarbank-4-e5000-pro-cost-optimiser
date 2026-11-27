"""4b.1: the real-time controller process. One controller, complete control logic, and an execution-mode switch.

    shadow  (default): every decision and every command is prepared exactly as in live mode, then recorded as SUPPRESSED.
    live            : the same prepared commands go to a commissioned transport.  No such transport is installed yet, so a
                      request for live is refused (fail-closed) and the controller stays in shadow, saying so on the dashboard.

Switching is a change to one small file (the mode file) and a restart; no code changes. The decisions (control_logic) and the command
preparation (control_exec) do not know which mode is running, so what is proved in shadow is what runs live.

Small on purpose: standard library, the state-machine helper (`machines.py`, which wraps `transitions`) and the existing read-only device readers. No database, web, solver or provider code.
It reads the hardware directly, owns the equipment exclusively (a second controller is refused before anything is opened), and it
starts RESTRICTED after every restart. The systemd watchdog is fed only after a whole cycle - inputs, decision, preparation,
execution, recording and logging - has finished promptly; unavailable equipment gives the restricted decision, not a restart.
"""

import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

from .acquirer import DirectAcquirer, DirectConfig, parse_devices
from .control import SingleOwner
from .control_exec import Executor, LogTransport, Preparer
from .freshness import stored as freshness_stored
from .control_logic import MAX_AGE_S, PlanMemory, Supervisor, read_plan, safe_decision
from .live import control_tick, sd_notify

RUN_DIR = Path("/run/solarbank-cost-optimiser")
STATE_DIR = Path("/var/lib/solarbank-cost-optimiser")

# The installed live transport. None until a transport has been written AND commissioned against the real device
# (register semantics, sign, read-back, expiry behaviour, release procedure: SOFTWARE_SPECIFICATION.md section 10).
from .trusted_files import AUTH_DIR

LIVE_TRANSPORT_FACTORY = None


def resolve_mode(requested, factory=None, logged_factory=None):
    """(effective mode, transport, note). Anything but a clear, supported request means shadow. `logged` runs the action machine and journals every register write it would make, sending nothing."""
    requested = (requested or "shadow").strip().lower()
    if requested not in ("shadow", "logged", "live"):
        return "shadow", LogTransport(), f"Unrecognised mode '{requested}' in the mode file: staying in shadow"

    def logged(prefix=""):
        try:
            transport = logged_factory()
            return "logged", transport, prefix + "Logged: the action machine runs and every register write it would make is journaled; nothing is sent"
        except Exception as error:
            return "shadow", LogTransport(), f"{prefix}Logged mode could not start ({error.__class__.__name__}): staying in shadow"
    if requested == "logged":
        if logged_factory is None:
            return "shadow", LogTransport(), "Logged mode was requested but is not available here: staying in shadow"
        return logged()
    if requested == "live":
        if factory is None:
            if logged_factory is not None:
                return logged("Live was requested but no commissioned command transport is installed: ")
            return "shadow", LogTransport(), "Live was requested but no commissioned command transport is installed: staying in shadow"
        try:
            transport = factory()
            if not callable(getattr(transport, "effect", None)):
                return "shadow", LogTransport(), ("Live was requested but the transport has no effect check, so no command could ever be verified: "
                                                  "staying in shadow")
            return "live", transport, "Live: commands go to the commissioned transport"
        except Exception as error:
            reason = str(error) if error.__class__.__name__ == "GateClosed" else error.__class__.__name__
            if logged_factory is not None:
                return logged(f"Live was requested but is not open ({reason}): ")
            return "shadow", LogTransport(), f"Live was requested but the transport could not start ({reason}): staying in shadow"
    return "shadow", LogTransport(), "Shadow: commands are prepared and recorded, never sent"


def read_mode_file(path):
    from .trusted_files import read_trusted
    text = read_trusted(path)                    # an untrustworthy file (not root's, or writable by others) means shadow
    lines = text.strip().splitlines() if text else []
    return lines[0] if lines else "shadow"


def write_atomic(path, text):
    """Replace a small file in one step (a reader sees the old or the new, never half of it)."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def read_ownership(path):
    """'handed_over' or 'controller'. Anything unreadable means 'controller': the controller then starts restricted as always."""
    try:
        return "handed_over" if Path(path).read_text().strip() == "handed_over" else "controller"
    except (OSError, UnicodeDecodeError, TypeError):
        return "controller"


class AsyncLog:
    """Diagnostic text goes through a small bounded queue to a thread that does the (possibly slow) printing, so a slow journal or
    terminal can never delay a control cycle. If the queue is full the line is dropped and counted: control matters more than logs."""

    def __init__(self, sink=None, size=200):
        import queue
        self._queue, self.dropped = queue.Queue(maxsize=size), 0
        self._sink = sink or (lambda text: print(text, file=sys.stderr, flush=True))
        threading.Thread(target=self._drain, name="control-log", daemon=True).start()

    def _drain(self):
        while True:
            self._sink(self._queue.get())

    def log(self, text):
        import queue
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            self.dropped += 1


class ChangeLog:
    """One JSON line per command CHANGE (not per second), size-capped with one rotation: a week of shadow evidence, few writes."""

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


class ControlLoop:
    def __init__(self, live, plan_path, out_path, executor, preparer, mode="shadow", mode_note="", requested="shadow", period=1.0,
                 monotonic=time.monotonic, clock=time.time, notify=sd_notify, devices=("solarbank", "shelly"), changelog=None,
                 log=lambda text: print(text, file=sys.stderr, flush=True), supervisor=None, run_id=None, ownership_path=None, meter_feed_path=None,
                 alert_dir=None, dispatch_feed_path=None, slot_allowance_path=None):
        self.live, self.plan_path, self.out_path, self.period = live, plan_path, Path(out_path) if out_path else None, period
        self.executor, self.preparer, self.mode, self.mode_note, self.requested = executor, preparer, mode, mode_note, requested
        self._mono, self._clock, self._notify, self.log = monotonic, clock, notify, log
        self.alert_dir = alert_dir           # GitHub issue 69: the reconciliation and smart-meter-voltage validity alerts
        self.ownership_path = Path(ownership_path) if ownership_path else None
        self.meter_feed_path, self._meter_seq = (Path(meter_feed_path) if meter_feed_path else None), None
        self.dispatch_feed_path = Path(dispatch_feed_path) if dispatch_feed_path else None   # GitHub issue 67: the Octopus SMART dispatches for SMARTSLOT
        self._tariff_fetched_at = None
        self._persisted_owner = read_ownership(self.ownership_path)
        self.supervisor = supervisor or Supervisor(handed_over=self._persisted_owner == "handed_over")   # a restart keeps a hand-over
        # slot_allowance_path: accepted and unused. No smart-slot allowance is counted any more (GitHub issue 79: Octopus manages it).
        self.changelog = changelog or ChangeLog(None)
        self.plan_memory = PlanMemory()    # the last valid plan, in memory only (see PlanMemory)
        self._interesting = set()          # command ids whose whole lifecycle is kept on disk
        self._stage_seen = {}
        self.limits = {d: MAX_AGE_S[d] for d in devices}
        self.freshness = freshness_stored(None)            # until a plan file says otherwise: 2.5 s while charging, 10 s (the specification's STALE_S) otherwise
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.started_at = clock()
        self.cycles = self.late_cycles = 0
        self.prev_total_ms = self.prev_output_ms = 0.0
        self.worst_total_ms = 0.0
        self._logged = {}
        self.last_decision = None

    # -- one complete cycle; its elapsed time includes execution, recording and logging ----------------------------------------
    def cycle(self):
        t0 = self._mono()
        utc = self._clock()
        # GitHub issue 68 (D15 against D9, owner decision A, 9/10 November): the tight limit is for the garage current reading alone ("meter": it
        # directly sets the charge power); the Solarbank and Shelly readings keep the specification's ordinary idle_s limit even while charging, so
        # a short gap in either (D15's "up to 10 s changes nothing") no longer makes the whole device's data invalid and releases the battery.
        charging = self.supervisor.charge_w > 0
        self.limits = {d: (self.freshness["charge_s"] if charging and d == "meter" else self.freshness["idle_s"]) for d in self.limits}
        inputs, _stale = control_tick(self.live, self.limits, t0)
        tariff = self._read_tariff(utc)
        if tariff is not None:
            inputs["tariff"] = tariff
        t1 = self._mono()
        plan, note = self.plan_memory.resolve(*read_plan(self.plan_path, utc), utc)
        if plan is not None and plan.get("freshness"):
            self.freshness = plan["freshness"]
        try:
            decision = self.supervisor.decide(inputs, plan, note, t0, self.executor.control_verified(), utc=utc)
        except Exception as error:                                   # a bug must restrict, never stop the controller
            decision = safe_decision(f"Controller could not decide ({error.__class__.__name__}): everything restricted")
        self._raise_validity_alerts(decision, utc)
        t2 = self._mono()
        commands = self.preparer.prepare(decision, utc, t0, self.cycles + 1)
        t3 = self._mono()
        touched = self.executor.execute(commands, inputs, utc, t0)
        t4 = self._mono()
        self.cycles += 1
        self.last_decision = decision
        state = {
            "controller": {"run_id": self.run_id, "pid": os.getpid(), "started_at": self.started_at},
            "execution": {"mode": self.mode, "requested": self.requested, "note": self.mode_note, "ownership": self.executor.ownership(),
                          "sends_commands": bool(getattr(self.executor.transport, "sends", False)),
                          "transport_failures": int(getattr(self.executor.transport, "failures", 0) or 0),
                          "write_failures": int(getattr(self.executor.transport, "_write_failures", 0) or 0),      # GitHub issue 63
                          "simulated_transport": bool(getattr(self.executor.transport, "simulated", False))},
            "at": utc, "cycle": self.cycles, "period_s": self.period,
            "timing_ms": {"inputs": round((t1 - t0) * 1000, 1), "plan_and_decide": round((t2 - t1) * 1000, 1),
                          "prepare": round((t3 - t2) * 1000, 1), "execute": round((t4 - t3) * 1000, 1),
                          "previous_output": self.prev_output_ms, "previous_total": self.prev_total_ms,
                          "worst_total": self.worst_total_ms},
            "late_cycles": self.late_cycles,
            "plan": {"valid": plan is not None, "held": bool(plan and plan.get("held")), "note": note, "action": plan and plan["action"], "target_soc": plan and plan["target_soc"],
                     "valid_until": plan and plan["valid_until"]},
            "decision": decision.as_dict(),
            "commands": self.executor.latest(),
        }
        for command in commands:                                     # the command log: changes only ...
            if self._logged.get(command.channel) != command.key():
                self._logged[command.channel] = command.key()
                self._interesting.add(command.id)
                record = self.executor.records.get(command.id, {})
                line = ("[%s] %s %s %s (%s): %s" % (self.mode.upper(), command.channel, command.action, command.params or "",
                                                   record.get("stage", "prepared"), command.reason))
                self.log(line)
                self.changelog.record({"at": utc, "mode": self.mode, "cycle": self.cycles, "event": "command", "command": command.as_dict(),
                                       "stage": record.get("stage"), "restrictions": list(decision.restrictions)})
        for record in touched:                                       # ... plus every later step of those commands, and any failure of any command
            previous = self._stage_seen.get(record["id"])
            stage = record["stage"]
            if stage != previous:
                self._stage_seen[record["id"]] = stage
                routine = stage in ("prepared", "suppressed", "attempted", "acknowledged", "observed", "superseded")
                if record["id"] in self._interesting or not routine:
                    self.changelog.record({"at": utc, "mode": self.mode, "cycle": self.cycles, "event": "lifecycle", "id": record["id"],
                                           "channel": record["channel"], "action": record["action"], "stage": stage,
                                           "attempts": record["attempts"], "outcome": record["outcome"], "simulated": record["simulated"]})
        if len(self._stage_seen) > 400:
            self._stage_seen = {i: self._stage_seen[i] for i in list(self._stage_seen)[-200:]}
            self._interesting &= set(self._stage_seen)
        owner = "handed_over" if self.supervisor.handed_over else "controller"
        if self.ownership_path is not None and owner != self._persisted_owner:      # durable, and only when it changes
            try:
                write_atomic(self.ownership_path, owner + "\n")
                self._persisted_owner = owner
                self.log(f"Controller: ownership is now '{owner}'")
            except OSError as error:
                self.log(f"Controller: could not record ownership ({error.__class__.__name__})")
        self._publish_meter(inputs, utc)
        t5 = self._mono()
        if self.out_path is not None:
            try:
                write_atomic(self.out_path, json.dumps(state, separators=(",", ":"), default=str))
            except OSError as error:
                self.log(f"Controller: could not write the status file ({error.__class__.__name__})")
        t6 = self._mono()
        total = t6 - t0
        self.prev_total_ms, self.prev_output_ms = round(total * 1000, 1), round((t6 - t5) * 1000, 1)
        self.worst_total_ms = max(self.worst_total_ms, self.prev_total_ms)
        if total > self.period:
            self.late_cycles += 1
            self.log(f"Controller: cycle {self.cycles} took {total * 1000:.0f} ms against a {self.period * 1000:.0f} ms period")
        return decision, total

    def _raise_validity_alerts(self, decision, utc):
        """GitHub issue 69 (specification 11.2, with an email alert, owner decision A): the two new validity checks live in the Supervisor as pure
        tags (no I/O there, consistent with the rest of it); this is the one place that turns a tag into a raised or cleared alert file (the alert
        unit, which has outbound network access, mails it; this process never does)."""
        if not self.alert_dir:
            return
        from . import alerts
        if "reconciliation_invalid" in decision.restrictions:
            alerts.raise_alert(self.alert_dir, "reconciliation",
                                "Garage power reconciliation failed",
                                "P_unmetered (garage power minus the car and the battery) has been below -230 W for 30 s or more. "
                                "A current-transformer reading may be wrong or reversed. AC charging has been stopped.", utc)
        else:
            alerts.clear_alert(self.alert_dir, "reconciliation")
        if "meter_voltage_invalid" in decision.restrictions:
            alerts.raise_alert(self.alert_dir, "meter-voltage",
                                "Smart meter voltage out of range",
                                "The smart meter's own voltage reading is outside 207-253 V, or missing. AC charging has been stopped.", utc)
        else:
            alerts.clear_alert(self.alert_dir, "meter-voltage")

    def _publish_meter(self, inputs, utc):
        """This process owns the meter (one Modbus client only). Share each new reading, stamped with when the METER was read, so that the
        collector and dashboard need no connection of their own. A failure to write is logged and never stops the cycle."""
        item = inputs.get("meter")
        if self.meter_feed_path is None or item is None or item["seq"] == self._meter_seq:
            return
        try:
            write_atomic(self.meter_feed_path, json.dumps({"at": utc - item["age"], "seq": item["seq"], "values": item["value"]},
                                                          separators=(",", ":")))
            self._meter_seq = item["seq"]
        except (OSError, TypeError, ValueError) as error:
            self.log(f"Controller: could not publish the meter feed ({error.__class__.__name__})")

    def _read_tariff(self, utc):
        """GitHub issue 67 (specification 6.2): the Octopus SMART dispatches for SMARTSLOT, shared by the providers service like the plan file.
        `dispatches` is included only on a cycle where the file's own `fetched_at` has moved on from the last one seen here, with that time, so
        `signals.SmartSlot` counts its 300 s/1,800 s freshness from when Octopus was actually asked: not from how often this loop re-reads an
        unchanged file, and not from when a restarted controller first sees a file that may be hours old.
        Returns None (no `tariff` key at all) when there is nothing to say: the missing-reading path in `decide` is as safe as this one."""
        if self.dispatch_feed_path is None:
            return None
        try:
            document = json.loads(self.dispatch_feed_path.read_text())
            fetched_at = document["fetched_at"]
            if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if fetched_at == self._tariff_fetched_at:
            return {"value": {}, "age": utc - fetched_at}          # unchanged: SmartSlot's own freshness timer runs from the last real fetch
        dispatches = document.get("dispatches")
        if not isinstance(dispatches, list):
            return None
        self._tariff_fetched_at = fetched_at
        return {"value": {"dispatches": dispatches, "fetched_at": fetched_at}, "age": utc - fetched_at}

    def healthy_cycle(self, total):
        return total < 3 * self.period

    def run(self, stop, count=None):
        self._notify("READY=1")
        next_cycle = self._mono()
        try:
            while not stop.is_set() and (count is None or self.cycles < count):
                _decision, total = self.cycle()
                if self.healthy_cycle(total):
                    self._notify("WATCHDOG=1")           # only a whole cycle that finished promptly proves the loop is alive
                next_cycle += self.period
                delay = next_cycle - self._mono()
                if delay < 0:
                    next_cycle, delay = self._mono(), 0
                stop.wait(delay)
        finally:
            self.release_on_exit()                       # also when the loop dies with an error: the battery is handed back before the process ends

    def release_on_exit(self):
        """LC2: a stopping controller hands the battery back (0 W, then the default mode) through the same transport and gates. Never raises."""
        hand_back = getattr(self.executor.transport, "on_exit", None)
        if not callable(hand_back):
            return
        try:
            writes = hand_back()
            self.log(f"Controller {self.run_id}: released on exit ({len(writes)} write(s))")
        except Exception as error:
            self.log(f"Controller {self.run_id}: the release on exit failed ({error.__class__.__name__}): {error}")


def _takes_run_id(factory):
    import inspect
    try:
        return len(inspect.signature(factory).parameters) >= 1
    except (TypeError, ValueError):
        return False


def build(live, plan_path, out_path, requested="shadow", factory=None, log_path=None, devices=("solarbank", "shelly"),
          period=1.0, notify=sd_notify, monotonic=time.monotonic, clock=time.time, log=None, effect=None, ownership_path=None, meter_feed_path=None,
          logged_factory=None, alert_dir=None, dispatch_feed_path=None, slot_allowance_path=None):
    run_id = uuid.uuid4().hex[:12]
    mode, transport, note = resolve_mode(requested, ((lambda: factory(run_id)) if _takes_run_id(factory) else factory) if factory else None, (lambda: logged_factory(run_id)) if logged_factory else None)
    kwargs = {"log": log} if log else {}
    effect = effect or getattr(transport, "effect", None)
    return ControlLoop(live, plan_path, out_path, Executor(transport, effect=effect), Preparer(run_id), mode=mode, mode_note=note,
                       requested=(requested or "shadow"), period=period, notify=notify, monotonic=monotonic, clock=clock,
                       devices=devices, changelog=ChangeLog(log_path), run_id=run_id, ownership_path=ownership_path, meter_feed_path=meter_feed_path,
                       alert_dir=alert_dir, dispatch_feed_path=dispatch_feed_path, slot_allowance_path=slot_allowance_path, **kwargs)


def main(argv=None, acquirer_factory=None):
    import argparse
    parser = argparse.ArgumentParser(description="Controller: decides every second, prepares commands, sends them only in live mode.")
    parser.add_argument("--out", default=str(RUN_DIR / "control-loop.json"))
    parser.add_argument("--plan", default=str(RUN_DIR / "plan.json"))
    parser.add_argument("--lock", default=str(RUN_DIR / "control-loop.lock"))
    parser.add_argument("--mode-file", default=str(AUTH_DIR / "control-mode"))
    parser.add_argument("--log", default=str(STATE_DIR / "control-commands.jsonl"))
    parser.add_argument("--meter-feed", default=str(RUN_DIR / "meter.json"),
                        help="where the meter readings are shared (the controller owns the meter; the collector reads this file)")
    parser.add_argument("--dispatch-feed", default=str(RUN_DIR / "tariff.json"),
                        help="where the providers service shares the Octopus SMART dispatches for SMARTSLOT (GitHub issue 67)")
    parser.add_argument("--slot-allowance", default=str(STATE_DIR / "smart-slot-allowance.json"),
                        help="no longer used (GitHub issue 79: no smart-slot allowance is counted); accepted so the installed service unit still starts")
    parser.add_argument("--ownership-file", default=str(STATE_DIR / "control-ownership"),
                        help="remembers a hand-over to the Anker app across restarts until an explicit resume")
    parser.add_argument("--devices", default="solarbank,shelly")
    parser.add_argument("--writes-log", default=str(STATE_DIR / "control-writes.jsonl"), help="the journal of every register write the action machine makes or would make")
    parser.add_argument("--write-switch", default=str(AUTH_DIR / "write-switch"), help="the commissioning switch: a root-owned file listing the write classes (mode, setpoint) that may really be sent")
    parser.add_argument("--commissioning-record", default=str(AUTH_DIR / "commissioning.json"), help="root-owned record of the commissioning tests passed for this exact controller code (RC4)")
    parser.add_argument("--device-settings", default=str(STATE_DIR / "device-settings.jsonl"), help="the collector's log of the device's settings, read for the start-up checks")
    parser.add_argument("--meter-after-cutover", action="store_true",
                        help="allow the meter: only once the original monitor has been retired (the meter allows one Modbus client)")
    for name in ("solarbank-host", "meter-host", "shelly-host"):
        parser.add_argument("--" + name, default=None)
    args = parser.parse_args(argv)
    devices = parse_devices(args.devices)
    if "meter" in devices and not args.meter_after_cutover:
        print("Controller: the meter allows one Modbus client and the original monitor still owns it; refusing.", file=sys.stderr)
        return 2
    try:
        owner = SingleOwner(args.lock).__enter__()           # BEFORE any equipment connection and before touching the output files
    except RuntimeError as error:
        print(f"Controller: {error}; not starting.", file=sys.stderr)
        return 3
    try:
        from .devices_config import DeviceConfigError, require, resolve
        try:
            found = require(resolve(overrides={"solarbank": args.solarbank_host, "meter": args.meter_host, "shelly": args.shelly_host}), tuple(devices))
        except DeviceConfigError as problem:
            print(f"Controller: {problem} Not starting.", file=sys.stderr)
            return 4                  # the unit never restarts on 4 (a restart would only repeat this) and raises an alert
        hosts = {f"{name}_host": host for name, host in found.items() if host}
        acquirer = (acquirer_factory or (lambda d, h: DirectAcquirer(DirectConfig(devices=d, mode_every=5.0, safety_every=30.0, device_book=str(STATE_DIR / "device-book.json"), discover=True, alert_dir=str(RUN_DIR / "alerts"), **h))))(devices, hosts).start()
        stop = threading.Event()
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: stop.set())
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        from .live_transport import device_info_from_log
        def logged_factory(run_id):
            from .action_machine import ActionMachine
            from .live_transport import LiveTransport, WriteJournal, device_info_from_log
            machine = ActionMachine(latch_path=STATE_DIR / "control-latch.json", fallback_latch_path=RUN_DIR / "control-latch.json", default_proven=False, verify=False)
            return LiveTransport(machine, WriteJournal(args.writes_log), writer=None, switch_path=args.write_switch,
                                 device_info=lambda: device_info_from_log(args.device_settings), send=False, run_id=run_id, alert_dir=str(RUN_DIR / "alerts"))
        from .live_gate import make_factory
        live_factory = LIVE_TRANSPORT_FACTORY or make_factory(args.commissioning_record, args.write_switch, args.writes_log, STATE_DIR / "control-latch.json",
                                                              RUN_DIR / "control-latch.json", lambda: device_info_from_log(args.device_settings), hosts.get("solarbank_host"),
                                                              host_of=(lambda: acquirer.current_host("solarbank")) if hasattr(acquirer, "current_host") else None,
                                                              target_ok=(lambda: acquirer.verified_host("solarbank", 660.0)) if hasattr(acquirer, "verified_host") else None)
        loop = build(acquirer.live, args.plan, args.out, requested=read_mode_file(args.mode_file), factory=live_factory, logged_factory=logged_factory,
                     log_path=args.log, devices=devices, log=AsyncLog().log, ownership_path=args.ownership_file, meter_feed_path=args.meter_feed,
                     alert_dir=str(RUN_DIR / "alerts"), dispatch_feed_path=args.dispatch_feed, slot_allowance_path=args.slot_allowance)
        loop.log(f"Controller {loop.run_id}: {loop.mode_note}")
        try:
            loop.run(stop)
        finally:
            acquirer.stop()
    finally:
        owner.__exit__()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
