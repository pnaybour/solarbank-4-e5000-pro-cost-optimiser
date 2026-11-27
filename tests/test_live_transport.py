"""Stage C: the live transport, its journal and the commissioning switch, against a fake device. The real writer is tested against a fake Modbus client."""
import json

import pytest

from solarbank_optimiser import action_machine as am
from solarbank_optimiser.action_machine import ActionMachine
from solarbank_optimiser.control_exec import Command, Executor, Preparer
from solarbank_optimiser.devices.errors import DeviceError
from solarbank_optimiser.devices.modbus_write import GuardedModbusWriter
from solarbank_optimiser.live_transport import LiveTransport, WriteJournal, read_switch
from solarbank_optimiser.shelly_channels import SOLARBANK_AC

UTC = 1_800_000_000.0
INFO = {"mask": 47, "backup_soc_enable": 0, "mode": 6}


class FakeWriter:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def write_mode(self, value):
        if self.fail == "mode":
            raise DeviceError("timeout", "no answer")
        self.calls.append(("mode", value))

    def write_setpoint(self, watts):
        if self.fail == "setpoint":
            raise DeviceError("exception", "refused")
        self.calls.append(("setpoint", watts))


def inputs(drawn=0.0, mode=None):
    value = {"soc": 50} if mode is None else {"soc": 50, "mode": mode, "mode_age_s": 1.0}          # `mode` is what the controller's own reader last read (GitHub issue 36)
    return {"solarbank": {"value": value, "age": .3}, "shelly": {"value": {SOLARBANK_AC: -drawn, "car": 0.0}, "age": .3}}


def cmd(channel, action, seq, now=0.0, **params):
    return Command(f"c{seq}", seq, channel, action, params, "economic", UTC + now, UTC + now + 30, "t", seq, now + 30)


def build(tmp_path, send=False, switch=None, writer=None, default_mode=None, **kw):
    journal = WriteJournal(tmp_path / "writes.jsonl")
    extra = {} if default_mode is None else {"default_mode": default_mode}        # a default other than mode 0, to drive the refused-default fall-back (LC5)
    machine = ActionMachine(latch_path=tmp_path / "latch.json", verify=send, default_proven=True, **extra)
    switch_path = tmp_path / "write-switch"
    if switch is not None:
        switch_path.write_text("\n".join(switch))
    transport = LiveTransport(machine, journal, writer or FakeWriter(), switch_path, device_info=lambda: dict(INFO), send=send, **kw)
    return transport, Executor(transport, effect=transport.effect), journal


def lines(tmp_path):
    path = tmp_path / "writes.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def drive(executor, seconds, commands_at=None, start=0.0, drawn=0.0, mode=None):
    for i in range(int(seconds)):
        t = start + i
        executor.execute((commands_at or {}).get(i, []), inputs(drawn, mode() if callable(mode) else mode), UTC + t, t)


# ---- logged: nothing is sent, everything is journaled ------------------------------------------------------------------------------------------------
def test_in_logged_mode_the_register_writes_are_journaled_and_nothing_is_sent(tmp_path):
    transport, executor, journal = build(tmp_path, send=False)
    drive(executor, 8, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9), cmd("discharge", "ALLOW", 2, 1.0)]})
    rows = lines(tmp_path)
    assert [(r["register"], r["value"], r["kind"]) for r in rows] == [(10071, 0, "would_write"), (10064, 3, "would_write"), (10071, -1500, "would_write")]
    assert all(r["why_not_sent"] == "logged mode" for r in rows) and transport.writer.calls == []
    assert executor.records["c1"]["stage"] == "suppressed"                    # the controller sees exactly what it sees in shadow
    assert executor.control_verified() is True


def test_the_journal_times_show_the_two_second_settle(tmp_path):
    transport, executor, journal = build(tmp_path, send=False)
    drive(executor, 8, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    times = [r["t"] for r in lines(tmp_path)]
    assert times[2] - times[1] >= am.MODE_SETTLE_S


def test_a_release_journals_zero_watts_then_the_default_mode(tmp_path):
    transport, executor, journal = build(tmp_path, send=False)
    drive(executor, 4, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]})
    drive(executor, 3, {0: [cmd("control", "RELEASE", 9, 4.0)]}, start=4.0)
    tail = [(r["register"], r["value"]) for r in lines(tmp_path)[-2:]]
    assert tail == [(10071, 0), (10064, 0)]


def test_the_machine_not_starting_because_the_device_settings_are_unknown_writes_nothing(tmp_path):
    transport, executor, journal = build(tmp_path, send=False)
    transport.device_info = lambda: None
    drive(executor, 5, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]})
    assert lines(tmp_path) == []


def test_a_refused_startup_check_rejects_commands_and_writes_nothing(tmp_path):
    transport, executor, journal = build(tmp_path, send=False)
    transport.device_info = lambda: {"mask": 46, "backup_soc_enable": 0, "mode": 6}
    drive(executor, 5, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]})
    assert lines(tmp_path) == [] and transport.machine.state == "refused"
    assert executor.records["c1"]["stage"] in ("suppressed", "rejected")


# ---- live: only what the commissioning switch allows is sent ---------------------------------------------------------------------------------
def test_with_the_switch_off_even_a_live_transport_sends_nothing_and_says_why(tmp_path):
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=[], writer=writer)
    drive(executor, 6, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    rows = lines(tmp_path)
    assert writer.calls == [] and rows and all(r["kind"] == "would_write" and "commissioning switch" in r["why_not_sent"] for r in rows)
    assert transport.blocked >= 1


def test_a_missing_or_unreadable_switch_file_allows_nothing(tmp_path):
    assert read_switch(tmp_path / "missing") == frozenset()
    (tmp_path / "bad").write_bytes(b"\xff\xfe\x00junk")
    assert read_switch(tmp_path / "bad") == frozenset()
    (tmp_path / "words").write_text("MODE\nsetpoint\nsomething else\n")
    assert read_switch(tmp_path / "words") == {"mode", "setpoint"}


def test_with_both_classes_allowed_the_writes_are_sent_in_order_and_journaled_as_wrote(tmp_path):
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 8, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    assert writer.calls == [("setpoint", 0), ("mode", 3), ("setpoint", -1500)]
    assert [r["kind"] for r in lines(tmp_path)] == ["wrote", "wrote", "wrote"]


def test_switching_a_class_off_mid_run_stops_those_writes_at_once_and_the_machine_replans_from_zero_when_it_is_back(tmp_path):
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 5, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]}, drawn=1500.0)
    (tmp_path / "write-switch").write_text("")                               # the owner turns the writes off
    before = len(writer.calls)
    drive(executor, 5, {0: [cmd("ac_charge", "CHARGE_LIMIT", 2, 5.0, watts=900, target_soc=.9)]}, start=5.0, drawn=1500.0)
    assert len(writer.calls) == before and transport.blocked >= 1             # a changed set-point was held back
    (tmp_path / "write-switch").write_text("mode\nsetpoint\n")                # and turned back on
    drive(executor, 8, start=10.0, drawn=1500.0)
    assert writer.calls[before:][0] == ("setpoint", 0)                        # it starts again from 0 W, not from a guess


def test_only_the_mode_class_allowed_blocks_the_setpoint_and_never_sends_half_a_sequence(tmp_path):
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode"], writer=writer)
    drive(executor, 8, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    # a zero set-point is protective and allowed with `mode`; the mode change goes out; the non-zero set-point (class `setpoint`) is held back
    assert writer.calls == [("setpoint", 0), ("mode", 3)]
    assert [r["kind"] for r in lines(tmp_path)][:3] == ["wrote", "wrote", "would_write"] and lines(tmp_path)[2]["value"] == -1500


def test_a_failed_write_is_journaled_as_failed_and_the_sequence_stops(tmp_path):
    writer = FakeWriter(fail="mode")
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 8, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    kinds = [(r["register"], r["kind"]) for r in lines(tmp_path)]
    assert kinds[:2] == [(10071, "wrote"), (10064, "failed")] and ("setpoint", -1500) not in writer.calls


# ---- GitHub 63: a refused write is counted, alerted and leads to a release, not retried for ever -------------------------------------------------
def test_a_write_that_always_fails_stops_after_two_attempts_with_one_alert_and_is_not_retried_every_second(tmp_path):
    writer = FakeWriter(fail="mode")
    alert_dir = tmp_path / "alerts"
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer, alert_dir=str(alert_dir))
    drive(executor, 60, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    failed_mode_writes = [r for r in lines(tmp_path) if r["kind"] == "failed" and r["register"] == 10064]
    assert len(failed_mode_writes) <= 5                                     # the old bug: 59, one every second for the whole run
    assert transport.machine.state == "released"
    alert_path = alert_dir / "machine-write-refused.json"
    assert alert_path.exists()
    body = json.loads(alert_path.read_text())["body"]
    assert "10064" in body and "mode" in body


def test_the_write_refused_alert_is_withdrawn_once_a_write_succeeds_again(tmp_path):
    writer = FakeWriter(fail="setpoint")
    alert_dir = tmp_path / "alerts"
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer, alert_dir=str(alert_dir))
    drive(executor, 10, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    assert (alert_dir / "machine-write-refused.json").exists()
    writer.fail = None                                                       # the device answers again
    drive(executor, 40, start=10.0)
    assert not (alert_dir / "machine-write-refused.json").exists() and transport._write_failures == 0


def test_a_setpoint_write_that_fails_once_then_works_causes_no_fault_and_no_alert(tmp_path):
    class FailsOnce(FakeWriter):
        def __init__(self):
            super().__init__()
            self._failed_already = False

        def write_setpoint(self, watts):
            if not self._failed_already:
                self._failed_already = True
                raise DeviceError("timeout", "no answer")
            self.calls.append(("setpoint", watts))

    writer = FailsOnce()
    alert_dir = tmp_path / "alerts"
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer, alert_dir=str(alert_dir))
    drive(executor, 5, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    assert transport.machine.state == "running" and transport._write_failures == 0
    assert not (alert_dir / "machine-write-refused.json").exists()
    assert ("setpoint", -1500) in writer.calls                              # it did, eventually, take effect


def test_a_persistently_refused_write_does_not_resend_an_already_acknowledged_zero_every_second(tmp_path):
    writer = FakeWriter(fail="mode")
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 60, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    wrote_zero = [r for r in lines(tmp_path) if r["kind"] == "wrote" and r["register"] == 10071 and r["value"] == 0]
    assert len(wrote_zero) < 10                                             # the old bug: 59, repeating the already-acknowledged 0 W every second


def test_the_exit_writes_zero_then_the_default_mode_through_the_same_gate(tmp_path):
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 8, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]}, drawn=1500.0)
    writer.calls.clear()
    transport.on_exit(10.0)
    assert writer.calls == [("setpoint", 0), ("mode", 0)]


# ---- the effect check -------------------------------------------------------------------------------------------------------------------------
def test_the_effect_check_reads_the_measured_ac_power_with_this_installations_sign(tmp_path):
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"])
    charge = {"action": "CHARGE_LIMIT", "params": {"watts": 1500}}
    assert transport.effect(charge, inputs(drawn=1450.0)) is True and transport.effect(charge, inputs(drawn=0.0)) is False
    assert transport.effect({"action": "STOP", "params": {}}, inputs(drawn=1500.0)) is False and transport.effect({"action": "STOP", "params": {}}, inputs(drawn=40.0)) is True


# ---- GitHub 66: the firmware's own taper near the charging limit is relaxed, not treated as a failed command -------------------------------------
def near_limit(drawn, soc=98.0, limit=100.0, max_live=None):
    value = inputs(drawn)
    value["solarbank"]["value"]["soc"] = soc
    value["solarbank"]["value"]["charging_limit_soc"] = limit
    if max_live is not None:
        value["solarbank"]["value"]["max_charge"] = max_live
    return value


def test_effect_relaxes_the_magnitude_check_within_3_percent_of_the_charging_limit_but_not_at_60_percent(tmp_path):
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"])
    charge = {"action": "CHARGE_LIMIT", "params": {"watts": 2500}}
    assert transport.effect(charge, near_limit(1500.0, soc=98.0, limit=100.0)) is True              # within 3 % of the limit: tapering is expected
    assert transport.effect(charge, near_limit(1500.0, soc=60.0, limit=100.0)) is False             # the same shortfall at 60 %: still a failure
    assert transport.effect(charge, near_limit(0.0, soc=98.0, limit=100.0)) is False                # relaxed, but no current at all (or a discharge): still a failure
    assert transport.effect(charge, near_limit(1500.0, soc=50.0, limit=100.0, max_live=1500.0)) is True   # the device's own live limit is below what was commanded


def test_a_tapered_charge_near_the_limit_does_not_hold_the_battery_over_many_cycles(tmp_path):
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"])
    for i in range(300):
        executor.execute([cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=2500, target_soc=.9)] if i == 0 else [],
                          near_limit(1500.0, soc=98.0, limit=100.0), UTC + i, float(i))
    rows = [r for r in lines(tmp_path) if r["register"] == 10071]
    assert len(rows) == 2 and transport.machine.state == "running"                 # 0 W through, then -2,500 W, once: no HOLD, no repeat


# ---- GitHub 72: specification 11.4's secondary check and records, and 10.4's record of a mode reversion -------------------------------------------
def with_ac(ac_w, mode=3, dc_w=None):
    value = inputs(0.0, mode)
    value["shelly"]["value"][SOLARBANK_AC] = ac_w                                   # this installation's sign: negative = drawing from the grid, positive = supplying
    if dc_w is not None:
        value["solarbank"]["value"]["battery"] = dc_w                               # register 10008: positive = discharging
    return value


def records(tmp_path, kind):
    return [r for r in lines(tmp_path) if r.get("kind") == "record" and r.get("record") == kind]


def test_effect_also_requires_the_battery_not_to_be_discharging(tmp_path):
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"])
    charge = {"action": "CHARGE_LIMIT", "params": {"watts": 1500}}
    assert transport.effect(charge, with_ac(-1500.0, dc_w=-1450.0)) is True
    assert transport.effect(charge, with_ac(-1500.0, dc_w=900.0)) is False          # the AC power looks right, the battery itself is discharging
    assert transport.effect(charge, with_ac(-1500.0)) is True                       # no DC reading: the AC check alone, as before
    assert transport.effect({"action": "INHIBIT", "params": {}}, with_ac(0.0, dc_w=600.0)) is False


def test_a_charge_that_shows_the_solarbank_supplying_is_recorded_once_as_a_possible_sign_inversion(tmp_path):
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"])
    for i in range(35):
        executor.execute([cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)] if i == 0 else [], with_ac(+800.0), UTC + i, float(i))
    found = records(tmp_path, "possible sign inversion")
    assert len(found) == 1 and found[0]["t"] >= 20.0 and found[0]["ac_supplied_w"] == 800.0      # once, after VERIFY_S, not every second


def test_grid_charging_while_held_at_zero_output_is_recorded_as_a_possible_firmware_override(tmp_path):
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"])
    for i in range(30):
        executor.execute([cmd("discharge", "INHIBIT", 1, 0.0)] if i == 0 else [], with_ac(-600.0), UTC + i, float(i))
    assert len(records(tmp_path, "possible firmware override")) == 1


def test_logged_mode_writes_no_verification_records(tmp_path):
    transport, executor, journal = build(tmp_path, send=False)
    for i in range(35):
        executor.execute([cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)] if i == 0 else [], with_ac(+800.0), UTC + i, float(i))
    assert records(tmp_path, "possible sign inversion") == []                       # nothing was really commanded: nothing to compare with


def test_a_mode_reversion_is_journaled_with_the_mode_found(tmp_path):
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"])
    drive(executor, 75, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]}, drawn=1500.0, mode=3)
    drive(executor, 70, start=75.0, drawn=1500.0, mode=0)                           # the Anker app put it back to self-consumption (checked every MODE_CHECK_S)
    found = records(tmp_path, "mode_reversion")
    assert found and found[0]["mode_found"] == 0 and found[0]["mode_required"] == 3


# ---- the real writer, against a fake Modbus client ----------------------------------------------------------------------------------------------
class FakeClient:
    sent = []

    def __init__(self, *a, **k):
        pass

    def connect(self):
        return True

    def close(self):
        pass

    def write_register(self, address, value, device_id=1):
        FakeClient.sent.append((6, address, [value]))
        return type("R", (), {"address": address, "isError": lambda self: False})()

    def write_registers(self, address, values, device_id=1):
        FakeClient.sent.append((16, address, list(values)))
        return type("R", (), {"address": address, "count": len(values), "isError": lambda self: False})()


def test_the_real_writer_writes_exactly_two_registers_the_way_the_vendor_does():
    FakeClient.sent = []
    writer = GuardedModbusWriter("192.0.2.1", client_factory=FakeClient)
    writer.write_mode(3); writer.write_setpoint(-1500); writer.write_setpoint(0); writer.write_setpoint(2000)
    assert FakeClient.sent == [(6, 10064, [3]), (16, 10071, [0xFFFF, 0xFA24]), (16, 10071, [0, 0]), (16, 10071, [0, 2000])]      # -1500 as 32-bit two's complement, high word first


@pytest.mark.parametrize("call,arg", [("write_mode", 1), ("write_mode", 7), ("write_mode", "3"), ("write_mode", True), ("write_setpoint", 3601), ("write_setpoint", -3601), ("write_setpoint", 1.5), ("write_setpoint", "0")])
def test_the_real_writer_refuses_every_other_value_before_sending(call, arg):
    FakeClient.sent = []
    writer = GuardedModbusWriter("192.0.2.1", client_factory=FakeClient)
    with pytest.raises(ValueError):
        getattr(writer, call)(arg)
    assert FakeClient.sent == []


def test_mode_7_is_written_only_by_a_writer_built_for_the_dynamic_mode_test():
    """Owner, 15 November 2026: the commissioning test `dynamic-mode` may write the Anker's dynamic pricing (7). The controller's writer never may."""
    FakeClient.sent = []
    test_writer = GuardedModbusWriter("192.0.2.1", client_factory=FakeClient, extra_modes=(7,))
    test_writer.write_mode(7); test_writer.write_mode(0)
    assert FakeClient.sent == [(6, 10064, [7]), (6, 10064, [0])]
    for bad in ((1,), (4,), (5,), (7, 4), ("7",), (True,)):                     # no other mode can be added this way
        with pytest.raises(ValueError):
            GuardedModbusWriter("192.0.2.1", client_factory=FakeClient, extra_modes=bad)
    from solarbank_optimiser import live_gate
    import inspect
    assert "extra_modes" not in inspect.getsource(live_gate)                    # the controller's factory builds the plain writer


def test_the_real_writer_has_no_way_to_write_any_other_register():
    writer = GuardedModbusWriter("192.0.2.1", client_factory=FakeClient)
    public = [n for n in dir(writer) if not n.startswith("_")]
    assert sorted(public) == ["close", "write_mode", "write_setpoint"]
    assert not hasattr(writer, "client") and not hasattr(writer, "_GuardedModbusWriter__client_public")


def test_a_refusal_or_a_mismatched_reply_is_a_device_error_not_success():
    class Refusing(FakeClient):
        def write_register(self, address, value, device_id=1):
            return type("R", (), {"address": address, "isError": lambda self: True, "exception_code": 2})()

    class Wrong(FakeClient):
        def write_registers(self, address, values, device_id=1):
            return type("R", (), {"address": address + 1, "count": 2, "isError": lambda self: False})()
    with pytest.raises(DeviceError, match="refused"):
        GuardedModbusWriter("192.0.2.1", client_factory=Refusing).write_mode(3)
    with pytest.raises(DeviceError, match="does not match"):
        GuardedModbusWriter("192.0.2.1", client_factory=Wrong).write_setpoint(-100)


# ---- how the controller chooses ----------------------------------------------------------------------------------------------------------------
def test_the_mode_file_chooses_shadow_logged_or_live_and_live_cannot_send_without_an_installed_factory():
    from solarbank_optimiser import control_loop as cl
    made = []

    def logged_factory():
        made.append(1)
        return type("T", (), {"sends": False, "simulated": False, "send": lambda *a: None})()
    assert cl.resolve_mode("shadow", None, logged_factory)[0] == "shadow" and made == []
    mode, transport, note = cl.resolve_mode("logged", None, logged_factory)
    assert mode == "logged" and "nothing is sent" in note
    mode, transport, note = cl.resolve_mode("live", None, logged_factory)             # live with no installed factory runs logged: the same safety, more information
    assert mode == "logged" and "no commissioned command transport" in note and transport.sends is False
    assert cl.resolve_mode("live", None, None)[0] == "shadow" and cl.resolve_mode("logged", None, None)[0] == "shadow"
    assert cl.resolve_mode("nonsense", None, logged_factory)[0] == "shadow"
    assert cl.LIVE_TRANSPORT_FACTORY is None                                          # nothing here installs a transport that sends


def test_a_logged_factory_that_fails_to_start_falls_back_to_shadow():
    from solarbank_optimiser import control_loop as cl

    def broken():
        raise OSError("no disk")
    mode, transport, note = cl.resolve_mode("logged", None, broken)
    assert mode == "shadow" and "could not start" in note


def test_the_whole_loop_in_logged_mode_decides_as_in_shadow_and_journals_the_register_writes(tmp_path):
    import json as _json
    from solarbank_optimiser import control_loop as cl
    from solarbank_optimiser.control_loop import ControlLoop
    from solarbank_optimiser.live import LiveState, Reading
    from test_control_loop import Clock, UTC as U, plan

    clock = Clock()
    (tmp_path / "plan.json").write_text(_json.dumps(plan()))
    (tmp_path / "settings.jsonl").write_text(_json.dumps({"decoded": {"ems_mode_mask": {"raw": 47}, "operating_mode": {"code": 6}, "limits": {"backup_soc_enable": 0}}}) + "\n")
    from solarbank_optimiser.live_transport import device_info_from_log
    assert device_info_from_log(tmp_path / "settings.jsonl") == {"mask": 47, "backup_soc_enable": 0, "mode": 6}

    def factory(run_id):
        machine = ActionMachine(latch_path=tmp_path / "latch.json", verify=False)
        return LiveTransport(machine, WriteJournal(tmp_path / "writes.jsonl"), None, tmp_path / "switch", device_info=lambda: device_info_from_log(tmp_path / "settings.jsonl"), send=False, run_id=run_id)
    live = LiveState()
    loop = cl.build(live, tmp_path / "plan.json", tmp_path / "out.json", requested="logged", logged_factory=factory, devices=("solarbank", "shelly", "meter"),
                    monotonic=lambda: clock.t, clock=lambda: clock.t)
    assert loop.mode == "logged"
    for second in range(60):
        clock.t += 1
        live.publish("solarbank", Reading({"soc": 50, "max_charge": 3600.0}, clock.t, U, 1, .02))
        live.publish("shelly", Reading({SOLARBANK_AC: 1.0, "car": 0.0}, clock.t, U, 1, .02))
        live.publish("meter", Reading({"main": 0, "sub": 0, "garage_current_a": 1.0}, clock.t, U, 1, .02))
        loop.cycle()
    state = _json.loads((tmp_path / "out.json").read_text())
    assert state["execution"]["mode"] == "logged"
    rows = lines(tmp_path)
    assert rows and all(r["kind"] == "would_write" for r in rows)
    assert {r["register"] for r in rows} <= {10064, 10071}



# ---- the review of 3 November: revocation, uncertain writes, truthful confirmation ---------------------------------------------------------------------
def test_emptying_the_switch_between_two_writes_stops_the_second(tmp_path):
    switch = tmp_path / "write-switch"

    class Revoking(FakeWriter):
        def write_setpoint(self, watts):
            super().write_setpoint(watts)
            switch.write_text("")                                             # the owner empties the switch while the first write is in progress

    writer = Revoking()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 8, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
    assert writer.calls == [("setpoint", 0)]


def test_a_lost_acknowledgement_still_gets_a_release_on_exit(tmp_path):
    class LosesTheAnswer(FakeWriter):
        def write_setpoint(self, watts):
            super().write_setpoint(watts)
            if watts != 0:
                raise DeviceError("timeout", "no answer")                     # the device applied it, the reply was lost

    writer = LosesTheAnswer()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 8, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]}, drawn=1500.0)
    assert ("setpoint", -1500) in writer.calls
    writer.calls.clear()
    transport.on_exit(20.0)
    assert writer.calls == [("setpoint", 0), ("mode", 0)]


def test_a_failed_first_release_write_is_tried_again(tmp_path):
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 6, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]}, drawn=1500.0)
    machine = transport.machine
    writer.calls.clear(); writer.fail = "setpoint"
    transport._perform(100.0, machine.tick(100.0, False, "DISCHARGE"))         # invalid data: the release begins and its first write fails
    assert machine.state == "released" and machine.setpoint is None and writer.calls == []
    writer.fail = None
    transport._perform(101.0, machine.tick(101.0, False, "DISCHARGE"))         # the next tick does not wait for valid data: it tries the release again
    assert writer.calls == [("setpoint", 0), ("mode", 0)]


def test_inhibit_and_release_are_confirmed_only_by_what_the_device_does(tmp_path):
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    discharging = {"solarbank": {"value": {"soc": 50}, "age": .3}, "shelly": {"value": {SOLARBANK_AC: 2000.0, "car": 0.0}, "age": .3}}
    idle = inputs(0.0)
    inhibit = {"action": "INHIBIT", "params": {}}
    assert transport.effect(inhibit, discharging) is False and transport.effect(inhibit, idle) is True
    release = {"action": "RELEASE", "params": {}}
    assert transport.effect(release, idle) is False                          # nothing has been written yet
    transport._release_done = True
    transport.machine.mode = 0
    transport._mode_now = 6                                                   # released to mode 0 but the device reports 6: not followed
    assert transport.effect(release, idle) is False
    transport._mode_now = None                                                # mode unknown (no fresh reading): never confirmed
    assert transport.effect(release, idle) is False
    transport.machine.mode, transport._mode_now = 6, 6
    assert transport.effect(release, idle) is True


def test_a_release_is_a_hand_over_only_when_the_device_is_seen_in_the_release_mode(tmp_path):
    seen = {"mode": 3}
    writer = FakeWriter()
    journal = WriteJournal(tmp_path / "writes.jsonl")
    machine = ActionMachine(latch_path=tmp_path / "latch.json", verify=True, default_proven=True)
    (tmp_path / "write-switch").write_text("mode\nsetpoint\n")
    transport = LiveTransport(machine, journal, writer, tmp_path / "write-switch", device_info=lambda: {**INFO, **seen}, send=True)
    executor = Executor(transport, effect=transport.effect)
    drive(executor, 4, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]}, drawn=1500.0, mode=lambda: seen["mode"])
    drive(executor, 12, {0: [cmd("control", "RELEASE", 2, 4.0)]}, start=4.0, drawn=0.0, mode=lambda: seen["mode"])
    assert writer.calls[-2:] == [("setpoint", 0), ("mode", 0)]
    assert executor.ownership() != "anker"                                    # the writes returned, but the device still reports mode 3
    seen["mode"] = 0                                                          # now it follows (the release mode, 0: #78)
    drive(executor, 5, {0: [cmd("control", "RELEASE", 3, 16.0)]}, start=16.0, mode=lambda: seen["mode"])
    assert executor.ownership() == "anker"


def test_a_release_held_back_by_an_empty_switch_is_never_a_hand_over(tmp_path):
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 4, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]}, drawn=1500.0)
    (tmp_path / "write-switch").write_text("")
    drive(executor, 12, {0: [cmd("control", "RELEASE", 2, 4.0)]}, start=4.0, drawn=1500.0)
    assert executor.ownership() == "handover_pending"


def test_the_devices_own_floor_is_read_from_its_settings_log_and_a_mismatch_is_flagged(tmp_path):
    from solarbank_optimiser.live_transport import device_limits_from_log
    log = tmp_path / "settings.jsonl"
    log.write_text(json.dumps({"decoded": {"limits": {"charging_limit_soc": 100, "discharge_limit_soc": 10, "backup_reserve_soc": 10, "backup_soc_enable": 0}}}) + "\n" + "garbage\n")
    assert device_limits_from_log(log)["discharge_limit_soc"] == 10
    assert device_limits_from_log(tmp_path / "missing") == {}



# ---- GitHub issue 36: the mode comes from the controller's own reader, not the collector's log ------------------------------------------------------------
def test_a_stale_settings_log_does_not_cause_false_mode_reversions(tmp_path):
    """The log still says mode 0 for a minute after the controller wrote mode 3; the live reading says 3 within seconds. No reversion may be counted."""
    writer = FakeWriter()
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    transport.device_info = lambda: {**INFO, "mode": 0}                       # the collector's log: stale, still the old mode
    live = {"mode": 0}
    def follow():                                                             # the device takes mode 3 as soon as it is written
        if ("mode", 3) in writer.calls:
            live["mode"] = 3
        return live["mode"]
    drive(executor, 140, {0: [cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1500, target_soc=.9)]}, drawn=1500.0, mode=follow)
    assert transport.machine.state == "running" and len(transport.machine._reversions) == 0
    assert writer.calls.count(("mode", 3)) == 1                               # written once, never re-asserted


def test_an_unknown_or_old_live_mode_is_treated_as_unknown():
    from solarbank_optimiser.live_transport import LiveTransport
    assert LiveTransport._live_mode(inputs(0.0, 3)) == 3
    assert LiveTransport._live_mode(inputs(0.0)) is None
    old = inputs(0.0, 3); old["solarbank"]["value"]["mode_age_s"] = 40.0
    assert LiveTransport._live_mode(old) is None


def test_the_start_up_check_refuses_a_settings_log_that_is_too_old(tmp_path):
    from solarbank_optimiser.live_transport import device_info_from_log
    log = tmp_path / "s.jsonl"
    log.write_text(json.dumps({"at": 1000.0, "decoded": {"ems_mode_mask": {"raw": 47}, "operating_mode": {"code": 6}, "limits": {"backup_soc_enable": 0}}}) + "\n")
    assert device_info_from_log(log, clock=lambda: 1000.0 + 600) == {"mask": 47, "backup_soc_enable": 0, "mode": 6}
    assert device_info_from_log(log, clock=lambda: 1000.0 + 3800) is not None          # inside the hourly heartbeat plus a margin
    assert device_info_from_log(log, clock=lambda: 1000.0 + 4000) is None              # older than that says nothing (GitHub 51)


def test_the_solarbank_reader_reads_the_mode_itself_and_never_publishes_an_old_one():
    from solarbank_optimiser.devices import solarbank as sb
    class Client:
        def __init__(self): self.mode, self.fail, self.calls = 6, False, 0
        def read_input_registers(self, address, count): return [0] * count if (address, count) != sb.FAST_BLOCK else [0] * 16
        def read_holding_registers(self, address, count):
            self.calls += 1
            if self.fail:
                raise sb.DeviceError("timeout", "no answer")
            assert (address, count) == (10064, 1)
            return [self.mode]
    clock = [100.0]
    client = Client()
    reader = sb.Reader(client, monotonic=lambda: clock[0], mode_every=5.0)
    value = reader()
    assert value["mode"] == 6 and value["mode_age_s"] == 0.0
    clock[0] += 2; reader()
    assert client.calls == 1                                                  # not every poll: every 5 s
    client.fail = True
    clock[0] += 4; assert reader()["mode"] == 6                               # a failed read keeps the recent one
    clock[0] += 20
    assert reader().get("mode") is None                                       # and an old one is dropped, never published as fresh


def test_machine_alerts_are_raised_as_files(tmp_path):
    from solarbank_optimiser.live_transport import LiveTransport

    class Machine:
        state = "running"
        alerts = [("hands-off", "stopped"), ("hands-off", "stopped again"), ("verify-failed", "no effect")]

    transport = LiveTransport(Machine(), journal=None, alert_dir=str(tmp_path))
    assert transport.observe({}, 1.0) == []              # not heard yet: nothing runs, but the alerts still go out
    names = sorted(p.name for p in tmp_path.glob("*.json"))
    assert names == ["machine-hands-off.json", "machine-verify-failed.json"]
    assert transport.machine.alerts == []


def test_reset_request_file_restarts_a_stopped_machine_only_when_newer_and_checks_pass(tmp_path):
    import json, time
    from solarbank_optimiser.action_machine import ActionMachine
    from solarbank_optimiser.live_transport import LiveTransport

    latch = tmp_path / "latch.json"
    latch.write_text(json.dumps({"version": 1, "cause": "mode_reversions", "at": 10.0, "wall": time.time()}))
    machine = ActionMachine(latch_path=latch)
    assert machine.state == "hands_off"
    info = {"mask": 0b100001, "backup_soc_enable": 0}
    request, used = tmp_path / "request.json", tmp_path / "used.json"
    transport = LiveTransport(machine, journal=type("J", (), {"record": lambda self, e: None})(), device_info=lambda: info, alert_dir=str(tmp_path / "alerts"),
                              reset_path=str(request), reset_used_path=str(used))
    transport._valid = True
    request.write_text(json.dumps({"id": "a", "at": time.time() - 3600}))
    transport.observe({}, 20.0)
    assert machine.state == "hands_off" and "a" in json.loads(used.read_text())        # older than the stop: refused, and answered once
    request.write_text(json.dumps({"id": "b", "at": time.time() + 5}))
    info["backup_soc_enable"] = 1
    transport.observe({}, 21.0)
    assert machine.state == "hands_off"                                                  # the device checks fail
    request.write_text(json.dumps({"id": "c", "at": time.time() + 5}))
    info["backup_soc_enable"] = 0
    transport._heard = True
    transport.observe({}, 22.0)
    assert machine.state != "hands_off" and not latch.exists()


def test_a_reset_request_that_is_not_root_s_to_write_is_ignored_and_a_used_one_is_not_honoured_again(tmp_path, monkeypatch):
    import json, os, time
    from solarbank_optimiser import trusted_files
    monkeypatch.setattr(trusted_files, "REQUIRE_ROOT", True)                     # the real rules, with 'owned by root' read as 'owned by whoever runs the test'
    monkeypatch.setattr(trusted_files, "_owner_ok", lambda status: True)
    os.chmod(tmp_path, 0o755)
    from solarbank_optimiser.action_machine import ActionMachine
    from solarbank_optimiser.live_transport import LiveTransport

    def stopped():
        latch = tmp_path / "latch.json"
        latch.write_text(json.dumps({"version": 1, "cause": "mode_reversions", "at": 10.0, "wall": time.time()}))
        return ActionMachine(latch_path=latch)

    request, used = tmp_path / "request.json", tmp_path / "used.json"
    def transport_for(machine):
        t = LiveTransport(machine, journal=type("J", (), {"record": lambda self, e: None})(), device_info=lambda: {"mask": 0b100001, "backup_soc_enable": 0},
                          reset_path=str(request), reset_used_path=str(used))
        t._valid, t._heard = True, True
        return t
    request.write_text(json.dumps({"id": "x", "at": time.time() + 5}))
    os.chmod(request, 0o666)                                                              # writable by everyone: not an authorisation
    machine = stopped()
    transport_for(machine).observe({}, 20.0)
    assert machine.state == "hands_off" and not used.exists()
    os.chmod(request, 0o644)
    machine = stopped()
    transport_for(machine).observe({}, 21.0)
    assert machine.state != "hands_off" and "x" in json.loads(used.read_text())
    machine = stopped()                                                                   # the same request after a controller restart and a new stop
    transport_for(machine).observe({}, 22.0)
    assert machine.state == "hands_off"


def test_a_refused_default_mode_falls_back_to_self_consumption(tmp_path):
    from solarbank_optimiser.action_machine import ActionMachine, MODE_REGISTER, Write
    from solarbank_optimiser.devices.errors import DeviceError
    from solarbank_optimiser.live_transport import LiveTransport

    class Writer:
        def __init__(self):
            self.modes = []

        def write_mode(self, value):
            self.modes.append(value)
            if value == 6:
                raise DeviceError("exception", "Device refused the write (Modbus exception 3)")

        def write_setpoint(self, value):
            pass

    switch = tmp_path / "switch"
    switch.write_text("mode\nsetpoint\n")
    machine = ActionMachine(latch_path=tmp_path / "latch.json", default_proven=True, default_mode=6)      # a non-zero default to refuse (#78: the default is 0)
    machine.startup(0.0, 0b100001, 0, 3)
    writer = Writer()
    journal = type("J", (), {"record": lambda self, e: None})()
    transport = LiveTransport(machine, journal, writer=writer, switch_path=str(switch), send=True)
    transport._perform(1.0, [Write(MODE_REGISTER, 6, "release")])
    assert writer.modes == [6, 0]
    assert any(k == "default-mode-refused" for k, _ in machine.alerts)


def test_a_transport_failure_is_alerted_and_logged_not_swallowed(tmp_path, caplog):
    from solarbank_optimiser.live_transport import LiveTransport

    class Machine:
        state = "running"
        alerts = []

        def startup(self, *a):
            raise RuntimeError("boom")

    transport = LiveTransport(Machine(), journal=None, device_info=lambda: {"mask": 33, "backup_soc_enable": 0}, alert_dir=str(tmp_path))
    transport._heard = True
    try:
        transport.observe({}, 1.0)
    except RuntimeError:
        pass
    assert (tmp_path / "machine-transport-error.json").exists()


# ---- GitHub 52: one release episode, verified, not rewritten every second ------------------------------------------------------------------------------
def _release_drive(executor, seconds, mode, start=0.0):
    """Send the controller's RELEASE once and then run the loop with nothing new to say (the state a day outside the windows is in)."""
    executor.execute([cmd("control", "RELEASE", int(start) + 1, start)], inputs(0.0, mode), UTC + start, start)
    for i in range(1, int(seconds)):
        executor.execute([], inputs(0.0, mode), UTC + start + i, start + i)


def test_a_release_is_written_once_and_then_left_alone_while_the_readback_confirms_it(tmp_path):
    writer = FakeWriter()
    transport, executor, _ = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 5, {0: [cmd("ac_charge", "CHARGE_LIMIT", 0, watts=1000)]}, mode=3)          # something to release
    writer.calls.clear()
    _release_drive(executor, 120, mode=0, start=10.0)
    assert writer.calls == [("setpoint", 0), ("mode", 0)]


def test_a_release_that_was_acknowledged_but_not_taken_falls_back_to_mode_0_once_then_retries_slowly(tmp_path):
    writer = FakeWriter()
    transport, executor, _ = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer, alert_dir=str(tmp_path / "alerts"), default_mode=6)   # a non-zero default (#78)
    drive(executor, 5, {0: [cmd("ac_charge", "CHARGE_LIMIT", 0, watts=1000)]}, mode=3)
    writer.calls.clear()
    _release_drive(executor, 29, mode=3, start=10.0)                       # the device keeps reporting mode 3
    assert writer.calls == [("setpoint", 0), ("mode", 6)]                  # nothing more inside RELEASE_RETRY_S
    for i in range(29, 31):
        executor.execute([], inputs(0.0, 3), UTC + 10 + i, 10.0 + i)
    assert writer.calls[2:] == [("mode", 0)]                               # then the default mode counts as refused: mode 0, once
    assert (tmp_path / "alerts" / "machine-default-mode-refused.json").exists()                  # and the owner is told
    for i in range(31, 58):
        executor.execute([], inputs(0.0, 3), UTC + 10 + i, 10.0 + i)
    assert writer.calls[2:] == [("mode", 0)]                               # and not again for another RELEASE_RETRY_S


def test_a_release_that_did_not_get_through_is_retried_no_faster_than_every_30_seconds(tmp_path):
    writer = FakeWriter(fail="setpoint")
    transport, executor, journal = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer)
    drive(executor, 3, {0: [cmd("ac_charge", "CHARGE_LIMIT", 0, watts=1000)]}, mode=3)
    _release_drive(executor, 65, mode=3, start=10.0)
    attempts = [l for l in lines(tmp_path) if l.get("reason", "").startswith("release") or l["register"] == 10071 and l["value"] == 0 and l["t"] >= 10]
    assert 2 <= len(attempts) <= 6, len(attempts)                          # about one attempt per 30 s, not 65


def test_in_logged_mode_a_release_is_journaled_once_per_episode_not_every_second(tmp_path):
    transport, executor, _ = build(tmp_path, send=False)
    drive(executor, 3, {0: [cmd("ac_charge", "CHARGE_LIMIT", 0, watts=1000)]}, mode=3)
    before = len(lines(tmp_path))
    _release_drive(executor, 600, mode=0, start=10.0)
    new = lines(tmp_path)[before:]
    assert [(l["register"], l["value"]) for l in new] == [(10071, 0), (10064, 0)] and all(l["kind"] == "would_write" for l in new)
    drive(executor, 2, {0: [cmd("ac_charge", "CHARGE_LIMIT", 9, 700.0, watts=500)]}, start=700.0, mode=0)       # a new command ends the episode
    count = len(lines(tmp_path))
    _release_drive(executor, 5, mode=0, start=720.0)
    assert len(lines(tmp_path)) == count + 2                               # and the next release is a new episode: written again, once


# ---- GitHub 49: the protective 0 W of HANDS_OFF is owed until it is acknowledged ---------------------------------------------------------------------
def _latched_machine(tmp_path, setpoint=-1000):
    machine = ActionMachine(latch_path=tmp_path / "latch.json", default_proven=True)
    assert machine.startup(0.0, 47, 0, 3)
    machine.setpoint, machine.mode = setpoint, 3
    am.fire(machine.model, "too_many_reversions", now=1.0)
    return machine, machine._enter_hands_off(1.0, "test")


def test_a_failed_protective_zero_is_retried_until_it_is_acknowledged_and_nothing_else_is_written(tmp_path):
    machine, first = _latched_machine(tmp_path)
    assert [(w.register, w.value) for w in first] == [(am.SETPOINT_REGISTER, 0)]
    machine.forget()                                                       # the transport's reaction to a failed write
    assert machine.state == "hands_off" and machine.tick(2.0, True, am.CHARGE, 1500.0, 0.0) == []        # inside ZERO_RETRY_S
    retry = machine.tick(1.0 + am.ZERO_RETRY_S, True, am.CHARGE, 1500.0, 0.0)
    assert [(w.register, w.value) for w in retry] == [(am.SETPOINT_REGISTER, 0)]
    machine.forget()
    assert machine.tick(1.0 + am.ZERO_RETRY_S + 1, True, am.CHARGE, 1500.0, 0.0) == []
    again = machine.tick(1.0 + 2 * am.ZERO_RETRY_S, True, am.CHARGE, 1500.0, 0.0)
    assert [(w.register, w.value) for w in again] == [(am.SETPOINT_REGISTER, 0)]
    machine.sent(again[0])                                                 # acknowledged
    assert machine.tick(100.0, True, am.CHARGE, 1500.0, 0.0) == [] and machine.exit_writes() == []


def test_exit_while_latched_with_the_zero_still_owed_tries_it_and_never_writes_a_mode_or_a_non_zero(tmp_path):
    machine, _ = _latched_machine(tmp_path)
    machine.forget()
    exit_writes = machine.exit_writes()
    assert [(w.register, w.value) for w in exit_writes] == [(am.SETPOINT_REGISTER, 0)]


def test_a_confirmed_zero_on_entry_owes_nothing_and_a_restart_that_finds_the_latch_writes_nothing(tmp_path):
    machine, first = _latched_machine(tmp_path, setpoint=0)
    assert first == [] and machine.tick(50.0, True, am.CHARGE, 1500.0, 0.0) == []
    restarted = ActionMachine(latch_path=tmp_path / "latch.json", default_proven=True)
    assert restarted.state == "hands_off" and restarted.tick(60.0, True, am.CHARGE, 1500.0, 0.0) == [] and restarted.exit_writes() == []


# ---- GitHub 51: the start-up checks and the running check use the controller's own reading of the mask and Backup Reserve -------------------------------
def with_safety(base, mask=0b100001, backup=0, age=1.0):
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    out["solarbank"] = {**base["solarbank"], "value": {**base["solarbank"]["value"], "mask": mask, "backup_soc_enable": backup, "safety_age_s": age}}
    return out


def test_a_live_backup_reserve_reading_beats_a_log_that_says_it_is_off(tmp_path):
    machine = ActionMachine(latch_path=tmp_path / "latch.json", verify=False, default_proven=True)
    transport = LiveTransport(machine, WriteJournal(tmp_path / "w.jsonl"), None, tmp_path / "sw", device_info=lambda: dict(INFO), send=False)
    transport.send(cmd("ac_charge", "CHARGE_LIMIT", 1, watts=1000), 1)
    transport.observe(with_safety(inputs(0.0, 3), backup=1), 0.0)           # the log says 0; the controller itself just read 1
    assert machine.state == "refused" and "backup reserve" in machine.refused_reason


def test_without_a_live_reading_a_log_older_than_the_heartbeat_is_not_believed(tmp_path):
    from solarbank_optimiser.live_transport import device_info_from_log
    log = tmp_path / "s.jsonl"
    log.write_text(json.dumps({"at": 1000.0, "decoded": {"ems_mode_mask": {"raw": 47}, "operating_mode": {"code": 6}, "limits": {"backup_soc_enable": 0}}}) + "\n")
    machine = ActionMachine(latch_path=tmp_path / "latch.json", verify=False, default_proven=True)
    transport = LiveTransport(machine, WriteJournal(tmp_path / "w.jsonl"), None, tmp_path / "sw", device_info=lambda: device_info_from_log(log, clock=lambda: 1000.0 + 5000), send=False)
    transport.send(cmd("ac_charge", "CHARGE_LIMIT", 1, watts=1000), 1)
    assert transport.observe(inputs(0.0, 3), 0.0) == [] and machine.state == "startup"      # no usable settings: it does not start


def test_a_stale_live_reading_is_treated_as_unknown():
    from solarbank_optimiser.live_transport import LiveTransport
    assert LiveTransport._live_safety(with_safety(inputs(0.0, 3))) == {"mask": 0b100001, "backup_soc_enable": 0}
    assert LiveTransport._live_safety(with_safety(inputs(0.0, 3), age=300.0)) is None
    assert LiveTransport._live_safety(inputs(0.0, 3)) is None


def test_backup_reserve_switched_on_while_running_stops_the_controller_with_the_protective_zero_and_an_alert(tmp_path):
    writer = FakeWriter()
    transport, executor, _ = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer, alert_dir=str(tmp_path / "alerts"))
    for i in range(6):
        executor.execute([cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1000)] if i == 0 else [], with_safety(inputs(0.0, 3)), UTC + i, float(i))
    assert transport.machine.state == "running" and ("setpoint", -1000) in writer.calls
    writer.calls.clear()
    for i in range(6, 12):
        executor.execute([], with_safety(inputs(0.0, 3), backup=1), UTC + i, float(i))      # the owner enables Backup Reserve in the Anker app
    assert transport.machine.state == "hands_off"
    assert writer.calls == [("setpoint", 0)]                                              # the protective 0 W, once, and nothing else
    assert (tmp_path / "alerts" / "machine-safety-violated.json").exists()


# ---- GitHub 41: repeated transport failures are counted, shown, and end in a release ---------------------------------------------------------------
def test_five_failing_cycles_in_a_row_release_the_battery_once_and_a_good_cycle_resets_the_count(tmp_path, monkeypatch):
    writer = FakeWriter()
    transport, executor, _ = build(tmp_path, send=True, switch=["mode", "setpoint"], writer=writer, alert_dir=str(tmp_path / "alerts"))
    for i in range(4):
        executor.execute([cmd("ac_charge", "CHARGE_LIMIT", 1, 0.0, watts=1000)] if i == 0 else [], inputs(0.0, 3), UTC + i, float(i))
    assert ("setpoint", -1000) in writer.calls and transport.failures == 0
    real = transport._observe

    def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(transport, "_observe", broken)
    writer.calls.clear()
    for i in range(4, 8):
        executor.execute([], inputs(0.0, 3), UTC + i, float(i))
    assert transport.failures == 4 and writer.calls == []                       # counted, not yet released
    for i in range(8, 14):
        executor.execute([], inputs(0.0, 3), UTC + i, float(i))
    assert writer.calls == [("setpoint", 0), ("mode", 0)]                       # released ONCE: 0 W, then the default mode
    assert (tmp_path / "alerts" / "machine-transport-failing.json").exists() and transport.health() == {"transport_failures": 10, "write_failures": 0}
    monkeypatch.setattr(transport, "_observe", real)
    executor.execute([], inputs(0.0, 3), UTC + 20, 20.0)
    assert transport.failures == 0


def test_a_stop_while_nothing_is_charging_writes_no_mode_change_but_a_stop_during_a_charge_still_holds_at_once(tmp_path):
    """GitHub issue 62: a one-second stale garage reading raised a protective STOP and the machine wrote mode 3 then mode 0 although nothing was charging."""
    transport, executor, journal = build(tmp_path, send=False)
    drive(executor, 8, {1: [cmd("discharge", "ALLOW", 1, 1.0)], 3: [cmd("ac_charge", "STOP", 2, 3.0)], 4: [cmd("ac_charge", "STOP", 3, 4.0)],
                        5: [cmd("ac_charge", "WITHDRAW", 4, 5.0)]})
    assert [r["value"] for r in lines(tmp_path) if r["register"] == 10064] == [0]                 # only the start-up self-consumption write: no mode 3 for an idle STOP
    assert transport._stopped is False and transport._charge_w == 0.0
    # while a charge is running, the same STOP holds the battery straight away (unchanged protective behaviour)
    other = tmp_path / "running"
    other.mkdir()
    transport, executor, journal = build(other, send=False)
    drive(executor, 9, {1: [cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)], 5: [cmd("ac_charge", "STOP", 2, 5.0)], 6: [cmd("ac_charge", "STOP", 3, 6.0)]})
    assert transport._stopped is True and transport._charge_w == 0.0
    written = [(r["register"], r["value"]) for r in lines(other)]
    assert written[-2:] == [(10071, -1500), (10071, 0)]                                           # charging at 1,500 W, then held at 0 W the moment the STOP arrived
