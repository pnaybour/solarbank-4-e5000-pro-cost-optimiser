"""C1: the read-only look at the Solarbank's settings registers. Reads only; every address comes from the vendor's pinned read map."""
import inspect
import json
import re
from pathlib import Path

import pytest

from solarbank_optimiser.devices import settings_probe as probe
from solarbank_optimiser.devices.errors import DeviceError
from solarbank_optimiser.devices.modbus import ReadOnlyModbusTcp
from fake_devices import FakeModbus

PIN = Path(__file__).resolve().parents[1] / "reference/anker-solix-official/58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml"


def device_registers(mode=1, mask=0b1111111, capability=0b1111, limits=(100, 5, 10, 0)):
    registers = {32768 + i: w for i, w in enumerate((0x4445, 0x4D31, 0x0000, 0x0000, 0x0000))}          # "DEM1"
    registers[32774] = mask
    registers[32775] = capability
    registers[10064] = mode
    registers.update({10060 + i: 0 for i in range(13)} | {10074 + i: i for i in range(8)} | {10064: mode})
    registers.update({60000 + i: v for i, v in enumerate(limits)})
    return registers


@pytest.fixture
def server():
    s = FakeModbus(device_registers())
    yield s
    s.stop()


def client_for(server):
    return ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1.0)


def test_every_address_read_is_in_the_vendors_pinned_read_map():
    yaml = pytest.importorskip("yaml")          # the Pi's environment does not carry PyYAML; the local gate does
    pin = yaml.safe_load(PIN.read_text())
    ranges = {kind: [tuple(map(int, r.split("-"))) for r in pin["batch_read_ranges"][kind]] for kind in ("input", "holding")}
    inside = lambda kind, a, n: any(lo <= a and a + n - 1 <= hi for lo, hi in ranges[kind])
    for name, (function, address, count) in probe.READS.items():
        kind = "input" if function == 4 else "holding"
        if name == "capability_mask":
            assert address == 0x8007 and function == 4          # the vendor reads this one on its own, as an input register
            continue
        assert inside(kind, address, count), name


def test_the_vendors_labels_are_the_ones_used():
    yaml = pytest.importorskip("yaml")
    pin = yaml.safe_load(PIN.read_text())
    options = pin["write_quantities"]["enumeration_selection"]["operating_mode"]["options"]
    assert {int(k): v for k, v in options.items()} == probe.OPERATING_MODES
    bits = pin["write_quantities"]["enumeration_selection"]["operating_mode"]["option_capability_bits"]
    for code, bit in bits.items():
        assert probe.MODE_MASK_BITS[bit] == probe.OPERATING_MODES[int(code)]


def test_a_snapshot_decodes_the_mode_masks_limits_and_model(server):
    snapshot = probe.read_settings(client_for(server), now=100.0)
    d = snapshot["decoded"]
    assert d["device_model"] == "DEM1" and d["operating_mode"] == {"code": 1, "name": "tou_mode"}
    assert d["ems_mode_mask"]["modes_enabled"] == ["self_consumption", "tou_mode", "custom_mode", "smart_mode", "socket_overlay_mode", "third_party_control", "dynamic_pricing"]
    assert d["capability_mask"]["supported"] == ["charging_limit_soc", "discharge_limit_soc", "backup_reserve_soc", "backup_soc_enable"]
    assert d["limits"] == {"charging_limit_soc": 100, "discharge_limit_soc": 5, "backup_reserve_soc": 10, "backup_soc_enable": 0}
    assert snapshot["items"]["holding_10074_10081"]["raw"] == list(range(8)) and snapshot["at"] == 100.0
    # GitHub issue 72 (specification 14 item 4): register 10080, the X1's communication watchdog, is surfaced raw and never interpreted
    assert d["register_10080"]["raw"] == 6 and "undocumented" in d["register_10080"]["note"]


def test_only_reads_are_sent_function_codes_3_and_4(server):
    probe.read_settings(client_for(server))
    assert set(server.functions) == {3, 4}


def test_an_unimplemented_register_is_reported_not_fatal(server):
    del server.registers[32775]
    snapshot = probe.read_settings(client_for(server))
    assert snapshot["items"]["capability_mask"]["ok"] is False and snapshot["items"]["capability_mask"]["error"] == "exception"
    assert "may not implement it" in snapshot["decoded"]["capability_mask"]["note"]
    assert snapshot["decoded"]["operating_mode"]["name"] == "tou_mode" and snapshot["decoded"]["limits"]["charging_limit_soc"] == 100


def test_an_unknown_mode_code_is_shown_not_guessed(server):
    server.registers[10064] = 9
    assert probe.read_settings(client_for(server))["decoded"]["operating_mode"]["name"] == "unknown (9)"


def test_the_log_writes_changes_and_an_hourly_heartbeat_only(tmp_path, server):
    log = probe.SettingsLog(tmp_path / "s.jsonl", heartbeat=3600.0)
    client = client_for(server)
    assert log.record(probe.read_settings(client, now=0.0)) == "changed"                # the first snapshot
    assert log.record(probe.read_settings(client, now=60.0)) is None                    # nothing changed
    server.registers[10064] = 6                                                          # the owner switches the app to Smart mode
    assert log.record(probe.read_settings(client, now=120.0)) == "changed"
    assert log.record(probe.read_settings(client, now=3000.0)) is None
    assert log.record(probe.read_settings(client, now=3721.0)) == "heartbeat"
    lines = [json.loads(l) for l in (tmp_path / "s.jsonl").read_text().splitlines()]
    assert [l["event"] for l in lines] == ["changed", "changed", "heartbeat"]
    assert [l["decoded"]["operating_mode"]["name"] for l in lines] == ["tou_mode", "smart_mode", "smart_mode"]


def test_the_log_rotates_when_large(tmp_path, server):
    log = probe.SettingsLog(tmp_path / "s.jsonl", heartbeat=0.0, max_bytes=500)
    client = client_for(server)
    for i in range(6):
        log.record(probe.read_settings(client, now=float(i)))
    assert (tmp_path / "s.jsonl.1").exists()


def test_the_probe_module_cannot_write_to_the_device():
    source = inspect.getsource(probe)
    assert not re.search(r"write_(registers?|coils?)|mask_write|readwrite|10071|ModbusTcpClient", source, re.IGNORECASE)
    calls = set(re.findall(r"client\.(\w+)|read = client\.(\w+)|else client\.(\w+)", source))
    used = {name for group in calls for name in group if name}
    assert used <= {"read_input_registers", "read_holding_registers"}


# ===== through the collector's own connection (the Solarbank serves only a few clients) =========================================================================
def test_the_collector_reader_looks_at_the_settings_one_item_a_second_and_never_disturbs_the_readings(tmp_path):
    from solarbank_optimiser.devices import solarbank
    registers = device_registers(mode=6) | {10000 + i: 0 for i in range(40)} | {10014: 72, 10208: 0, 10209: 0, 10250: 0, 10251: 50, 10262: 0, 10263: 0, 10264: 0, 10265: 0}
    registers.update({10018: 0, 10019: 0, 10036: 0, 10037: 2000, 10038: 0, 10039: 2000})
    server = FakeModbus(registers)
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1.0)
        clock = [0.0]
        seen = []
        log = probe.SettingsLog(tmp_path / "s.jsonl")
        reader = solarbank.Reader(client, slow_every=10.0, monotonic=lambda: clock[0], settings_every=7.0, settings_sink=lambda snap: (seen.append(snap), log.record(snap)), wall=lambda: 1000.0 + clock[0])
        for step in range(20):
            clock[0] = float(step)
            sample = reader()
            assert sample["soc"] == 72                                                  # the live reading is unaffected on every poll
        assert len(seen) >= 2 and seen[0]["decoded"]["operating_mode"]["name"] == "smart_mode"
        assert set(seen[0]["items"]) == set(probe.READS) and reader.settings_errors == 0
        assert {3, 4} == set(server.functions)
        assert (tmp_path / "s.jsonl").exists() and len((tmp_path / "s.jsonl").read_text().splitlines()) == 1        # logged once; nothing changed
        client.close()
    finally:
        server.stop()


def test_a_failing_settings_read_never_breaks_the_fast_readings():
    from solarbank_optimiser.devices import solarbank

    class Client:
        def __init__(self):
            self.calls = 0
            self.holding_calls = 0

        def read_input_registers(self, address, count):
            return [0] * count if address != 10000 else [0, 0] + [0] * 12 + [72, 0]

        def read_holding_registers(self, address, count):
            self.holding_calls += 1
            raise DeviceError("closed", "link dropped")

    clock = [0.0]
    client = Client()
    reader = solarbank.Reader(client, slow_every=1000.0, monotonic=lambda: clock[0], settings_every=7.0, settings_sink=lambda snap: None)
    for step in range(30):
        clock[0] = float(step)
        assert reader()["soc"] == 72
    assert client.holding_calls == 1 and reader.settings_errors == 1                    # one failed look, then the device is left alone for ten minutes
    for step in range(700, 715):                                                         # ten minutes on it looks again (the holding items come after three input ones)
        clock[0] = float(step)
        reader()
    assert client.holding_calls >= 2


def test_the_apparent_device_clock_is_not_a_change(tmp_path, server):
    log = probe.SettingsLog(tmp_path / "s.jsonl", heartbeat=10**9)
    client = client_for(server)
    assert log.record(probe.read_settings(client, now=0.0)) == "changed"
    server.registers[10061] = 7                                                         # the clock word moves every second
    assert log.record(probe.read_settings(client, now=60.0)) is None
    server.registers[10062] = 1                                                         # but any other word in that block is a real change
    assert log.record(probe.read_settings(client, now=120.0)) == "changed"
