"""The conditions under which the controller may really send register writes (RC4: commissioning is the only change between shadow and live).

Going live is root-owned configuration, never a code change. Owner, 12 November 2026: commissioning is a simple checklist kept by the owner
(COMMISSIONING_PLAN.md), not enforced in code. The gate is the two files the owner sets:
  1. the status flag: the control-mode file says `live`;
  2. the write-switch file, which lists the write classes (`mode`, `setpoint`) that may be sent.
With either missing the controller runs in `logged` mode (it journals what it would write and sends nothing) and says which one is missing.
The commissioning record (`commission pass`, `commission run`) is the owner's notebook: it lists each test with its date and note, and the
fingerprint of the code it was last written on. A code change no longer wipes it, and it no longer blocks writes.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

ALERT_DIR = "/run/solarbank-cost-optimiser/alerts"
RESET_REQUEST = "/etc/solarbank-cost-optimiser/control-reset-request.json"        # root-owned folder: the request is an authorisation (GitHub 57)
RESET_USED = "/var/lib/solarbank-cost-optimiser/control-reset-used.json"          # the controller records the ids it has answered here (it cannot delete a root-owned file)

REQUIRED_TESTS = (
    "read-registers",           # the mode and set-point registers read back sensibly and match the vendor's map
    "mode-write",               # a mode write is accepted and read back
    "setpoint-zero-write",      # a 0 W set-point write is accepted (run by `commission run`)
    "setpoint-both-signs",      # T2, the sign test: `commission run setpoint-both-signs --confirm` (GitHub issue 75) checks 20 set-points on the Shelly and records it
    "release",                  # on stop the controller writes 0 W and then the default mode, and the device follows
    "default-mode",             # the default mode (Dynamic Tariff, value 6) is accepted by this device
    "hands-off-latch",          # the HANDS_OFF latch survives a restart
    # GitHub issue 65 (D16: with a car, nothing is waived): the specification's own mandatory commissioning tests (section 13), none of which the
    # gate checked for before. Every one is the owner's own sign-off with a note (MANUAL, below): none can be driven by `commission run` against
    # the real device, only observed on it.
    "t3a-pv-not-full",           # daylight, SoC < 95 %, mode 3 at 0 W: PV charges the battery
    "t3b-pv-full-car-charging",  # daylight, SoC 100 %, mode 3 at 0 W, car charging: PV follows demand and supplies the car
    "t5-mode-change-timing",     # 3 to 0 and 0 to 3: the time until behaviour changes, recorded
    "t6-comms-loss",             # mode 3, -300 W, controller stopped for 10 minutes, independent monitoring: does charging continue
    "t8-garage-regulation",      # full-rate charge; start the car, switch the freezer, add a 2 kW load, delay meter readings, then remove each:
                                  # I_garage_avg stays at or under 45 A after every step; peak and settling time recorded
    "t9-garage-ct-sign",         # a known garage load switched on and off, with branch protection and third-party PV monitoring off: the reading is
                                  # present over Modbus and P_garage rises positive with the import
    "t10-smart-slot",            # a real Octopus dispatch observed starting, moving, shortening, cancelling and ending, plus a fetch failure, an
                                  # empty list mid-session and a clock change: SMARTSLOT behaves as specification 6.2. To be observed on
                                  # a release that carries GitHub issue 67 (the dispatches reaching the controller); before that there was nothing to
                                  # observe. Not waived (D16).
    "t11-rms-current",           # I_garage_now compared with a clamp meter at several loads: within 0.5 A
    "t12-mode-reversion",        # the mode changed in the Anker app while running: detected, re-asserted once, HANDS_OFF after the limit
    "supervised-run",           # a supervised run with the owner present (gate G1) completed
)
# GitHub issue 76 (owner, 11 November 2026): one stage per write class. Seven of the tests above can only be observed while the controller drives the battery,
# so requiring all of them before anything may be sent made commissioning impossible in any order. A write class is sent only when it is in the write-switch
# AND every test of its stage is recorded for this code. The complete list (REQUIRED_TESTS) is the evidence for unattended running (G3).
STAGE_TESTS = {
    "mode": ("read-registers", "hands-off-latch", "mode-write", "default-mode", "setpoint-zero-write", "release", "setpoint-both-signs",
             "t9-garage-ct-sign", "t11-rms-current"),                      # stage A: mode writes and the protective 0 W (holds the battery while the car charges)
}
STAGE_TESTS["setpoint"] = STAGE_TESTS["mode"] + ("t3a-pv-not-full", "t5-mode-change-timing", "t12-mode-reversion")   # stage B: charging, supervised until T8 and T6 pass
MANUAL = ("setpoint-both-signs", "t3a-pv-not-full", "t3b-pv-full-car-charging", "t5-mode-change-timing", "t6-comms-loss", "t8-garage-regulation",
          "t9-garage-ct-sign", "t10-smart-slot", "t11-rms-current", "t12-mode-reversion", "supervised-run")   # the owner's own sign-off with a note; `commission run` cannot do them
ROOTS = ("control_loop", "live_gate", "live_transport", "action_machine", "devices/modbus_write", "trusted_files")


class GateClosed(RuntimeError):
    pass


def _local_imports(base, name):
    """The package modules that module `name` (a path under `base`, without .py) imports, by relative or absolute `solarbank_optimiser` imports."""
    import ast
    path = base / (name + ".py")
    tree = ast.parse(path.read_text())
    package = Path(name).parent.parts
    found = set()

    def add(parts):
        for candidate in ("/".join(parts), "/".join(parts) + "/__init__"):
            if (base / (candidate + ".py")).exists():
                found.add(candidate)
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = list(package[:len(package) - (node.level - 1)]) if node.level > 1 else list(package)
            elif node.module and node.module.split(".")[0] == "solarbank_optimiser":
                anchor, node = [], ast.ImportFrom(module=".".join(node.module.split(".")[1:]) or None, names=node.names, level=0)
            else:
                continue
            if node.module:
                add(anchor + node.module.split("."))
            for alias in node.names:
                add(anchor + (node.module.split(".") if node.module else []) + [alias.name])
    return found


def guarded_files(root=None):
    """Every module the controller's decision and write path can reach (the closure of its imports from ROOTS), sorted."""
    base = Path(root) if root else Path(__file__).parent
    seen, queue = set(), list(ROOTS)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(_local_imports(base, name) - seen)
    return sorted(seen)


def fingerprint(root=None):
    """SHA-256 over the source of every file the controller's decisions and writes depend on, so the record names this exact code."""
    base = Path(root) if root else Path(__file__).parent
    digest = hashlib.sha256()
    for name in guarded_files(root):
        digest.update(name.encode() + b"\0" + (base / (name + ".py")).read_bytes() + b"\0")
    return digest.hexdigest()


def read_record(path):
    from .trusted_files import read_trusted
    try:
        record = json.loads(read_trusted(path) or "")
        return record if isinstance(record, dict) else {}
    except ValueError:
        return {}


def _valid_entry(entry):
    return (isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)) and not isinstance(entry.get("at"), bool) and entry["at"] > 0
            and isinstance(entry.get("note"), str))


def _passed(record, code):
    """The tests validly recorded for this code (none when the record is missing or was made for other code)."""
    if not record or (code is not None and record.get("fingerprint") != code):
        return set()
    passed = record.get("passed") if isinstance(record.get("passed"), dict) else {}
    return {t for t in REQUIRED_TESTS if _valid_entry(passed.get(t))}


def missing_for(record, code):
    """{stage: [tests still to record]} for each write class and for `complete` (all of REQUIRED_TESTS)."""
    done = _passed(record, code)
    stages = {**STAGE_TESTS, "complete": REQUIRED_TESTS}
    return {stage: [t for t in tests if t not in done] for stage, tests in stages.items()}


def commissioned_classes(record_path, print_code=None):
    """The write classes whose stage is fully written in the owner's notebook (informational only since 12 November 2026: it does not gate writes)."""
    missing = missing_for(read_record(record_path), print_code)
    return frozenset(c for c in STAGE_TESTS if not missing[c])


def check(record_path, switch_path, print_code=None):
    """(open, reasons). `open` is True when the write-switch lists at least one write class (owner, 12 November 2026: the record does not gate)."""
    from .live_transport import read_switch
    switch = read_switch(switch_path)
    reasons = [] if switch else ["the write-switch allows no write class"]
    return bool(switch), reasons


def status(record_path, switch_path, mode_flag):
    from .live_transport import read_switch
    ok, reasons = check(record_path, switch_path)
    record = read_record(record_path)
    missing = missing_for(record, None)
    switch = read_switch(switch_path)
    return {"flag": mode_flag, "gate_open": ok and mode_flag == "live", "blocked_by": reasons if mode_flag == "live" else ["the status flag is not `live`"] + reasons,
            "passed": sorted((record.get("passed") or {})), "required": list(REQUIRED_TESTS), "write_classes": sorted(switch),
            "commissioned_classes": sorted(c for c in STAGE_TESTS if not missing[c]),
            "sent_classes": sorted(switch),                                          # what would actually be sent with the flag at `live`: the switch alone decides
            "notebook_code_matches": bool(record) and record.get("fingerprint") == fingerprint(),
            "still_needed": {stage: tests for stage, tests in missing.items() if tests}}


def record_pass(record_path, test, now=None, note=""):
    """Write a passed test into the owner's notebook (root only: the file is root-owned). Earlier entries are kept when the code changes."""
    if test not in REQUIRED_TESTS:
        raise ValueError(f"unknown test '{test}'; the tests are: " + ", ".join(REQUIRED_TESTS))
    if test in MANUAL and not note.strip():
        raise ValueError(f"'{test}' is your own sign-off: say what you saw with --note")
    code = fingerprint()
    record = read_record(record_path)
    record = {"fingerprint": code, "passed": dict(record.get("passed") or {})}
    record["passed"][test] = {"at": time.time() if now is None else now, "note": note[:200]}
    path = Path(record_path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(record, indent=1))
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    return record


def make_factory(record_path, switch_path, journal_path, latch_path, fallback_latch_path, device_info, solarbank_host, writer_factory=None, reset_path=RESET_REQUEST, reset_used_path=RESET_USED, host_of=None, target_ok=None):
    """The factory control_loop.resolve_mode calls for `live`. Raises GateClosed (resolve_mode then runs `logged`) unless every condition holds."""
    def factory(run_id=""):
        ok, reasons = check(record_path, switch_path)
        if not ok:
            raise GateClosed("; ".join(reasons))
        from .action_machine import ActionMachine
        from .live_transport import LiveTransport, WriteJournal
        if writer_factory is None:
            from .devices.modbus_write import GuardedModbusWriter
            writer = GuardedModbusWriter(host_of or solarbank_host)
        else:
            writer = writer_factory(host_of or solarbank_host)
        machine = ActionMachine(latch_path=latch_path, fallback_latch_path=fallback_latch_path, default_proven=True, verify=True)
        return LiveTransport(machine, WriteJournal(journal_path), writer=writer, switch_path=switch_path, device_info=device_info, send=True, run_id=run_id, alert_dir=ALERT_DIR, reset_path=reset_path, reset_used_path=reset_used_path, target_ok=target_ok,
                             commissioned=None)                     # the write-switch alone decides (owner, 12 November 2026)
    return factory
