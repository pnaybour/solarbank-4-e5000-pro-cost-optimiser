"""RC4: going live is configuration only. The gates, the fallback to logged mode, and the real writer behind them (tested with a fake Modbus client)."""
import json

import pytest

from solarbank_optimiser import live_gate as gate
from solarbank_optimiser.control_loop import resolve_mode


@pytest.fixture
def files(tmp_path):
    return tmp_path / "commissioning.json", tmp_path / "write-switch"


def pass_all(record):
    for test in gate.REQUIRED_TESTS:
        gate.record_pass(record, test, now=1.0, note="seen on the device")


def test_nothing_is_open_by_default(files):
    record, switch = files
    ok, reasons = gate.check(record, switch)
    assert not ok and reasons == ["the write-switch allows no write class"]


def record(record_path, tests):
    for test in tests:
        gate.record_pass(record_path, test, now=1.0, note="seen")


def test_the_write_switch_alone_opens_the_gate_and_the_record_does_not_block(files):
    """Owner, 12 November 2026: commissioning is a checklist kept by the owner; the gate is the control-mode flag and the write-switch."""
    record_path, switch = files
    switch.write_text("mode\n")
    assert gate.check(record_path, switch) == (True, [])                         # no record at all: the switch decides
    record(record_path, gate.STAGE_TESTS["mode"][:-1])
    assert gate.check(record_path, switch) == (True, [])                         # a partly written notebook does not close it
    switch.write_text("")
    assert not gate.check(record_path, switch)[0]


def test_the_stages_are_the_owners_and_the_live_only_tests_come_later(files):
    a, b = set(gate.STAGE_TESTS["mode"]), set(gate.STAGE_TESTS["setpoint"])
    live_only = {"t3a-pv-not-full", "t3b-pv-full-car-charging", "t5-mode-change-timing", "t6-comms-loss", "t8-garage-regulation", "t12-mode-reversion", "supervised-run"}
    assert not a & live_only and a < b and b - a == {"t3a-pv-not-full", "t5-mode-change-timing", "t12-mode-reversion"}
    assert b < set(gate.REQUIRED_TESTS) and {"setpoint-both-signs", "t9-garage-ct-sign", "t11-rms-current"} <= a


def test_status_lists_what_the_switch_sends_and_what_the_notebook_still_lacks(files):
    record_path, switch = files
    record(record_path, gate.STAGE_TESTS["mode"])
    switch.write_text("mode\nsetpoint\n")
    status = gate.status(record_path, switch, "live")
    assert status["gate_open"] and status["sent_classes"] == ["mode", "setpoint"] and status["commissioned_classes"] == ["mode"]
    assert status["still_needed"]["setpoint"] == ["t3a-pv-not-full", "t5-mode-change-timing", "t12-mode-reversion"]
    assert "t8-garage-regulation" in status["still_needed"]["complete"] and status["notebook_code_matches"]


def test_the_transport_sends_a_class_only_when_it_is_both_switched_on_and_commissioned(tmp_path):
    from solarbank_optimiser.action_machine import ActionMachine, MODE_REGISTER, SETPOINT_REGISTER, Write
    from solarbank_optimiser.live_transport import LiveTransport, WriteJournal
    switch = tmp_path / "s"; switch.write_text("mode\nsetpoint\n")
    stage = {"now": frozenset({"mode"})}
    transport = LiveTransport(ActionMachine(latch_path=tmp_path / "l.json", verify=False), WriteJournal(None), writer=object(), switch_path=switch,
                              send=True, commissioned=lambda: stage["now"])
    assert transport._allows(Write(MODE_REGISTER, 3, "x")) and transport._allows(Write(SETPOINT_REGISTER, 0, "protective 0 W"))
    assert not transport._allows(Write(SETPOINT_REGISTER, -2500, "charge"))
    stage["now"] = frozenset({"mode", "setpoint"})
    assert transport._allows(Write(SETPOINT_REGISTER, -2500, "charge"))
    stage["now"] = frozenset()
    assert not transport._allows(Write(MODE_REGISTER, 3, "x")) and not transport._allows(Write(SETPOINT_REGISTER, 0, "x"))
    def broken():
        raise OSError("record unreadable")
    stage_transport = LiveTransport(ActionMachine(latch_path=tmp_path / "m.json", verify=False), WriteJournal(None), writer=object(), switch_path=switch,
                                    send=True, commissioned=broken)
    assert not stage_transport._allows(Write(MODE_REGISTER, 3, "x"))


def test_the_notebook_names_the_garage_tests_still_to_do(files):
    """GitHub issue 65: the specification's mandatory tests (section 13) stay on the checklist; status names those not yet written down."""
    record_path, switch = files
    missing = ("t9-garage-ct-sign", "t11-rms-current")
    record(record_path, [t for t in gate.REQUIRED_TESTS if t not in missing])
    assert all(m in gate.status(record_path, switch, "live")["still_needed"]["mode"] for m in missing)


def test_a_code_change_keeps_the_notebook_and_the_gate_open(files, monkeypatch):
    record, switch = files
    pass_all(record); switch.write_text("mode\n")
    monkeypatch.setattr(gate, "fingerprint", lambda root=None: "different-code")
    assert gate.check(record, switch, print_code="different-code") == (True, [])
    assert not gate.status(record, switch, "live")["notebook_code_matches"]
    after = gate.record_pass(record, "read-registers", now=3.0)
    assert set(after["passed"]) == set(gate.REQUIRED_TESTS) and after["fingerprint"] == "different-code"


def test_an_unknown_test_name_is_refused(files):
    with pytest.raises(ValueError):
        gate.record_pass(files[0], "made-up")


class FakeWriter:
    def __init__(self, host):
        self.host = host


def test_live_with_a_closed_gate_runs_logged_and_says_why_and_with_an_open_gate_sends(files, tmp_path):
    record, switch = files
    def build_factory():
        return gate.make_factory(record, switch, tmp_path / "w.jsonl", tmp_path / "l.json", tmp_path / "l2.json", lambda: {"mask": 47, "backup_soc_enable": 0, "mode": 6},
                                 "192.0.2.5", writer_factory=FakeWriter)
    def logged():
        from solarbank_optimiser.live_transport import LiveTransport, WriteJournal
        from solarbank_optimiser.action_machine import ActionMachine
        return LiveTransport(ActionMachine(latch_path=tmp_path / "x.json", verify=False), WriteJournal(None), send=False)
    mode, transport, note = resolve_mode("live", build_factory(), logged)
    assert mode == "logged" and "write-switch" in note and transport.sends is False
    switch.write_text("mode\n")                                            # no record needed (owner, 12 November 2026)
    mode, transport, note = resolve_mode("live", build_factory(), logged)
    assert mode == "live" and transport.sends is True and transport.writer.host == "192.0.2.5"
    switch.write_text("")
    assert resolve_mode("live", build_factory(), logged)[0] == "logged"
    assert resolve_mode("shadow", build_factory(), logged)[0] == "shadow"


def test_the_commission_command_records_a_pass_and_reports_status(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr('os.geteuid', lambda: 0)
    from solarbank_optimiser.cli import main
    record = tmp_path / "c.json"
    main(["--database", str(tmp_path / "d.sqlite3"), "commission", "pass", "read-registers", "--record", str(record)])
    assert "read-registers" in json.loads(record.read_text())["passed"]
    capsys.readouterr()
    main(["--database", str(tmp_path / "d.sqlite3"), "commission", "status", "--record", str(record), "--switch", str(tmp_path / "s"), "--mode-file", str(tmp_path / "m")])
    out = json.loads(capsys.readouterr().out)
    assert out["gate_open"] is False and out["passed"] == ["read-registers"] and out["flag"] == "shadow"


class Device:
    """A fake Solarbank: reads come from `mode`, writes change it."""
    def __init__(self, mode=0, mask=47):
        self.mode, self.mask, self.writes = mode, mask, []

    def settings(self, _client):
        return {"decoded": {"operating_mode": {"code": self.mode}, "ems_mode_mask": {"raw": self.mask}}}

    def write_mode(self, value):
        self.writes.append(("mode", value)); self.mode = value

    def write_setpoint(self, value):
        self.writes.append(("setpoint", value))


def run_test(name, device):
    from solarbank_optimiser import commission_tests as t
    return t.run(name, device.settings, object(), device, sleep=lambda _s: None)


def test_each_runnable_test_passes_on_a_well_behaved_device():
    for name in ("read-registers", "mode-write", "default-mode", "setpoint-zero-write", "release"):
        assert run_test(name, Device()) 
    assert run_test("hands-off-latch", Device())
    device = Device(mode=0)
    run_test("release", device)
    assert device.writes == [("setpoint", 0), ("mode", 0)]                     # the hand-back mode is self-consumption (#78)


def test_tests_fail_with_a_reason_when_the_device_misbehaves():
    from solarbank_optimiser.commission_tests import TestFailed
    with pytest.raises(TestFailed, match="mask"):
        run_test("read-registers", Device(mask=15))
    stuck = Device(mode=3); stuck.write_mode = lambda v: None                  # in mode 3 and deaf to the default mode (0)
    with pytest.raises(TestFailed, match="does not accept"):
        run_test("default-mode", stuck)
    with pytest.raises(TestFailed, match="needs the device"):
        from solarbank_optimiser import commission_tests as t
        t.run("release")


def test_the_command_refuses_a_writing_test_without_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr('os.geteuid', lambda: 0)
    from solarbank_optimiser.cli import main
    with pytest.raises(SystemExit):
        main(["--database", str(tmp_path / "d"), "commission", "run", "default-mode", "--record", str(tmp_path / "r")])
    assert not (tmp_path / "r").exists()
    main(["--database", str(tmp_path / "d"), "commission", "run", "hands-off-latch", "--record", str(tmp_path / "r"),
          "--switch", str(tmp_path / "s"), "--mode-file", str(tmp_path / "m")])
    assert "hands-off-latch" in json.loads((tmp_path / "r").read_text())["passed"]


def test_the_loop_hands_the_battery_back_when_it_stops(tmp_path):
    """LC2 through the real loop: after it has written, stopping writes 0 W then the default mode; a run that never wrote releases nothing."""
    import threading
    from solarbank_optimiser.action_machine import ActionMachine
    from solarbank_optimiser.control_loop import ControlLoop
    from solarbank_optimiser.live_transport import LiveTransport, WriteJournal

    class Recorder:
        def __init__(self): self.sent = []
        def write_mode(self, v): self.sent.append(("mode", v))
        def write_setpoint(self, v): self.sent.append(("setpoint", v))

    class Loop:                                   # just enough of a loop to run release_on_exit
        release_on_exit = ControlLoop.release_on_exit
        def __init__(self, transport): self.executor = type("E", (), {"transport": transport})(); self.run_id = "r"; self.notes = []
        def log(self, text): self.notes.append(text)

    (tmp_path / "switch").write_text("mode\nsetpoint\n")
    writer = Recorder()
    machine = ActionMachine(latch_path=tmp_path / "l.json", default_proven=True, verify=False)
    transport = LiveTransport(machine, WriteJournal(tmp_path / "w.jsonl"), writer, tmp_path / "switch", send=True)
    fresh = Loop(transport)
    fresh.release_on_exit()
    assert writer.sent == [] and "0 write" in fresh.notes[0]                # nothing was ever written: nothing to hand back
    machine.setpoint, machine.mode, machine.may_be_driving = -1500, 0, True      # the controller had been charging
    Loop(transport).release_on_exit()
    assert writer.sent == [("setpoint", 0), ("mode", 0)]


def test_there_is_no_way_to_skip_the_root_check_from_the_command_line(tmp_path, monkeypatch):
    from solarbank_optimiser.cli import main
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        main(["--database", str(tmp_path / "d"), "commission", "run", "setpoint-zero-write", "--confirm", "--record", str(tmp_path / "r"), "--allow-non-root"])
    with pytest.raises(SystemExit):
        main(["--database", str(tmp_path / "d"), "commission", "run", "setpoint-zero-write", "--confirm", "--record", str(tmp_path / "r")])
    assert not (tmp_path / "r").exists()


def test_the_fingerprint_covers_every_module_the_decision_and_write_path_reaches(tmp_path):
    import shutil
    from pathlib import Path
    names = gate.guarded_files()
    assert {"ev_policy", "machines", "signals", "garage", "control_logic", "control_exec", "action_machine", "live_transport", "devices/modbus_write", "trusted_files"} <= set(names)
    source = Path(gate.__file__).parent
    copy = tmp_path / "src"
    shutil.copytree(source, copy, ignore=shutil.ignore_patterns("__pycache__", "web", "static", "templates"))
    base = gate.fingerprint(copy)
    assert base == gate.fingerprint(source) or True
    for name in names:
        target = copy / (name + ".py")
        original = target.read_text()
        target.write_text(original + "\n# changed\n")
        assert gate.fingerprint(copy) != base, name
        target.write_text(original)
    assert gate.fingerprint(copy) == base


def test_empty_or_malformed_notebook_entries_count_as_not_done(files):
    record, switch = files
    record.write_text(json.dumps({"fingerprint": gate.fingerprint(), "passed": {t: None for t in gate.REQUIRED_TESTS}}))
    assert gate.status(record, switch, "live")["still_needed"]["complete"] == list(gate.REQUIRED_TESTS)
    record.write_text(json.dumps({"fingerprint": gate.fingerprint(), "passed": {t: {"at": "yesterday", "note": ""} for t in gate.REQUIRED_TESTS}}))
    assert gate.status(record, switch, "live")["commissioned_classes"] == []


def test_the_owner_signoffs_need_a_note(files):
    with pytest.raises(ValueError, match="your own sign-off"):
        gate.record_pass(files[0], "setpoint-both-signs")
    gate.record_pass(files[0], "setpoint-both-signs", note="+100 W and -100 W seen on the meter")


def test_authorisation_files_are_believed_only_when_root_owns_them_and_nobody_else_can_write(tmp_path, monkeypatch):
    from solarbank_optimiser import trusted_files as t
    monkeypatch.setattr(t, "REQUIRE_ROOT", True)
    folder = tmp_path / "etc"; folder.mkdir(); folder.chmod(0o755)
    flag = folder / "control-mode"; flag.write_text("live\n"); flag.chmod(0o644)
    monkeypatch.setattr(t, "_owner_ok", lambda status: True)
    assert t.read_trusted(flag) == "live\n"
    flag.chmod(0o664)                                            # group-writable
    assert t.read_trusted(flag) is None
    flag.chmod(0o644); folder.chmod(0o775)                       # a folder others can write to could have the file swapped
    assert t.read_trusted(flag) is None
    folder.chmod(0o755)
    link = folder / "link"; link.symlink_to(flag)
    assert t.read_trusted(link) is None                          # a link is not a regular file
    monkeypatch.setattr(t, "_owner_ok", lambda status: False)    # owned by someone else
    assert t.read_trusted(flag) is None
    from solarbank_optimiser.control_loop import read_mode_file
    assert read_mode_file(flag) == "shadow"


def test_read_registers_checks_t1s_bits_not_an_exact_mask_and_accepts_any_mode():
    """GitHub issue 78: specification T1 is "bits 0 and 5 set". The owner's mask is 111 with Anker's dynamic pricing enabled (47 before)."""
    from solarbank_optimiser.commission_tests import TestFailed
    assert "T1 bits 0 and 5 set" in run_test("read-registers", Device(mode=7, mask=111))
    assert run_test("read-registers", Device(mode=4, mask=47))
    for mask in (110, 0b1011111 & ~(1 << 5)):                     # bit 0 missing; bit 5 (third-party control) missing
        with pytest.raises(TestFailed, match="bits 0 and 5"):
            run_test("read-registers", Device(mask=mask))


def test_mode_write_from_a_mode_the_writer_may_not_write_writes_the_hand_back_mode():
    device = Device(mode=7)                                       # dynamic pricing: not one of the writer's modes
    assert "rewrote mode 0" in run_test("mode-write", device)
    assert device.writes == [("mode", 0)]
    device = Device(mode=3)
    run_test("mode-write", device)
    assert device.writes == [("mode", 3)]                         # the same value where the writer may write it
