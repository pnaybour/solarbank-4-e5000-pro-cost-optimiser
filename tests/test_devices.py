"""4a.2: read-only device readers, proven against the monitor's recorded raw data and fake devices."""
import inspect
import json
from pathlib import Path
import socket
import struct

import pytest

from fake_devices import FakeModbus, FakeShelly
from solarbank_optimiser.devices import DeviceError, meter, modbus, shelly, solarbank
from solarbank_optimiser.shelly_channels import CAR, SOLARBANK_AC, from_monitor
from solarbank_optimiser.devices.modbus import ReadOnlyModbusTcp

FIXTURES = Path(__file__).parent / "fixtures" / "monitor-raw" / "monitor-raw.json"


@pytest.fixture(scope="module")
def recorded():
    return json.loads(FIXTURES.read_text())


def blocks_from(raw):
    return raw["10000"], raw["10208"], raw["10250"], raw["10262"]


# ---- real recorded samples, with independently worked-out expected values ----------------------

def test_solarbank_decoder_matches_real_recorded_sample(recorded):
    for row in recorded["solarbank"]:
        assert solarbank.decode(*blocks_from(row["raw"])) == row["expected"]


def test_shelly_parser_matches_real_recorded_sample(recorded):
    for row in recorded["shelly"]:
        assert shelly.parse_status(row["raw"]) == from_monitor(row["expected"])


def test_meter_decoder_matches_recorded_main_and_sub_power(recorded):
    for row in recorded["meter"]:
        for key in ("main_w", "sub_w"):
            watts = row["raw"][key]
            word = watts & 0xFFFFFFFF                     # how the device stores INT32, high word first
            assert meter.decode_power([word >> 16, word & 0xFFFF]) == watts


def encode32(value):
    word = value & 0xFFFFFFFF
    return [word >> 16, word & 0xFFFF]


def build_main(**fields):
    main = [0] * 40
    main[1] = fields.get("status", 0)
    main[2:4] = encode32(fields.get("pv_direct", 0))
    main[4:6] = encode32(fields.get("pv_external", 0))
    main[8:10] = encode32(fields.get("battery", 0))
    main[10:12] = encode32(fields.get("load", 0))
    main[12:14] = encode32(fields.get("grid", 0))
    main[14] = fields.get("soc", 50)
    main[36:38] = encode32(fields.get("max_charge", 0))
    main[38:40] = encode32(fields.get("max_discharge", 0))
    return main


@pytest.mark.parametrize("fields", [
    dict(status=1, battery=-1500, grid=0, load=300, pv_direct=1800, soc=40),                  # charging
    dict(status=2, battery=2200, grid=0, load=2300, pv_direct=100, soc=95),                   # discharging
    dict(status=0, battery=0, grid=2300, load=2300, soc=5),                                   # grid import, standby
    dict(status=1, battery=-3000, grid=3500, load=500, soc=0),                                # grid charging, SOC 0
    dict(status=0, battery=0, grid=-800, load=200, pv_direct=600, pv_external=400, soc=100),  # export, both PV inputs
    dict(status=3, soc=100, max_charge=10000, max_discharge=10000),                           # sleep, limits
    dict(battery=-2_147_483_648, grid=2_147_483_647),                                         # extremes
])
def test_generated_registers_round_trip_through_the_decoder(fields):
    main = build_main(**fields)
    result = solarbank.decode(main, encode32(-250), [0, 100], [0, 0, 0, 0])
    for name in ("battery", "load", "grid", "pv_direct", "pv_external", "soc", "max_charge", "max_discharge"):
        if name in fields:
            assert result[name] == fields[name], name
    assert result["pv"] == fields.get("pv_direct", 0) + fields.get("pv_external", 0)
    assert result["status_code"] == fields.get("status", 0)
    assert result["ac_output"] == -250


def test_energy_counters_scale_by_ten():
    result = solarbank.decode(build_main(), [0, 0], [0, 100], [1, 0, 0, 25])
    assert result["charge_total_kwh"] == 6553.6 and result["discharge_total_kwh"] == 2.5 and result["rated_kwh"] == 10.0


@pytest.mark.parametrize("watts", [0, 1, -1, 32767, -32768, 65536, -65536, 2_147_483_647, -2_147_483_648])
def test_int32_word_order_and_sign(watts):
    word = watts & 0xFFFFFFFF
    assert meter.decode_power([word >> 16, word & 0xFFFF]) == watts


# ---- decoder validation ------------------------------------------------------------------------

def good_blocks():
    main = [0] * 40
    main[14] = 50
    return main, [0, 0], [0, 100], [0, 0, 0, 0]


def test_invalid_soc_and_block_sizes_are_errors_not_values():
    main, ac, rated, totals = good_blocks()
    main[14] = 101
    with pytest.raises(DeviceError) as caught:
        solarbank.decode(main, ac, rated, totals)
    assert caught.value.kind == "invalid"
    with pytest.raises(DeviceError):
        solarbank.decode(main[:39], ac, rated, totals)


def test_unknown_status_code_is_labelled_not_guessed():
    main, ac, rated, totals = good_blocks()
    main[1] = 9
    assert solarbank.decode(main, ac, rated, totals)["status"] == "Unknown (9)"


@pytest.mark.parametrize("change", [
    lambda d: d["em1:1"].update(errors=["overpower"]),
    lambda d: d["em1:0"].update(act_power=float("nan")),
    lambda d: d["em1:0"].update(voltage=True),
    lambda d: d["em1:0"].update(current="1.5"),
    lambda d: d.pop("em1:1"),
])
def test_shelly_bad_channel_is_an_error(change):
    data = {f"em1:{i}": {"act_power": 1.0, "voltage": 230.0, "current": 0.1} for i in range(2)}
    change(data)
    with pytest.raises(DeviceError):
        shelly.parse_status(data)


# ---- Modbus client over a real socket ---------------------------------------------------------

@pytest.fixture
def modbus_server():
    registers = {10014: 72, 10632: 2300, 10644: 0xFFFF, 10645: 0xFFF7, 10675: 0, 10676: 5}
    registers.update({10000 + i: i for i in range(40) if 10000 + i not in registers})
    registers.update({10632 + i: 0 for i in range(45) if 10632 + i not in registers})
    server = FakeModbus(registers)
    yield server
    server.stop()


def client_for(server, timeout=1.0):
    return ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=timeout)


def test_reads_registers_and_reuses_one_connection(modbus_server):
    client = client_for(modbus_server)
    assert client.read_input_registers(10014, 1) == [72]
    reading = meter.read(client)
    assert (reading["main"], reading["sub"]) == (-9, 5) and reading["voltage"] == 230.0
    assert set(reading) == {"main", "sub", "voltage", "garage_current_a", "garage_phase_currents_a"}
    assert modbus_server.connections == 1
    client.close()


def test_only_function_code_4_is_ever_sent(modbus_server):
    client = client_for(modbus_server)
    for _ in range(3):
        client.read_input_registers(10000, 40)
    client.close()
    assert set(modbus_server.functions) == {4}
    public = {n for n in dir(ReadOnlyModbusTcp) if not n.startswith("_")}
    assert public == {"read_input_registers", "read_holding_registers", "close", "set_host"}          # two reads, an address change (not a register operation), no way to reach the PyModbus client
    assert not [n for n in vars(client) if "write" in n.lower()]
    source = inspect.getsource(modbus)
    assert "write_" not in source and ".write" not in source


def test_holding_register_reads_use_function_code_3_only_when_asked(modbus_server):
    modbus_server.registers.update({60000: 80, 60001: 5})
    client = client_for(modbus_server)
    assert client.read_holding_registers(60000, 2) == [80, 5]
    assert client.read_input_registers(10014, 1) == [72]
    client.close()
    assert modbus_server.functions == [3, 4]
    for bad in ((-1, 1), (0, 0), (0, 126), (0xFFFF, 2)):
        with pytest.raises(ValueError):
            client.read_holding_registers(*bad)
    with pytest.raises(DeviceError) as caught:
        client.read_holding_registers(61000, 1)
    assert caught.value.kind == "exception"


def test_illegal_address_is_an_exception_and_keeps_the_connection(modbus_server):
    client = client_for(modbus_server)
    with pytest.raises(DeviceError) as caught:
        client.read_input_registers(20000, 1)
    assert caught.value.kind == "exception"
    assert client.read_input_registers(10014, 1) == [72]
    assert modbus_server.connections == 1
    client.close()


@pytest.mark.parametrize("mode", ["wrong_txn", "wrong_unit", "short", "wrong_function",
                                  "close_after_request", "reset", "partial_then_close", "silent", "trickle"])
def test_faults_give_a_typed_error_never_a_value(modbus_server, mode):
    modbus_server.mode = mode
    client = client_for(modbus_server, timeout=0.5)
    with pytest.raises(DeviceError) as caught:
        client.read_input_registers(10000, 3)
    assert caught.value.kind in {"protocol", "closed", "timeout"}
    modbus_server.mode = "normal"
    assert client.read_input_registers(10014, 1) == [72]       # reconnects cleanly afterwards
    client.close()


def test_an_unusual_protocol_id_still_returns_only_the_correct_values(modbus_server):
    # PyModbus ignores the protocol-id field; the values it returns must still be the right ones.
    modbus_server.mode = "bad_protocol"
    client = client_for(modbus_server)
    try:
        assert client.read_input_registers(10014, 1) == [72]
    except DeviceError:
        pass
    client.close()


def test_silence_is_reported_as_a_timeout(modbus_server):
    modbus_server.mode = "silent"
    client = client_for(modbus_server, timeout=0.5)
    with pytest.raises(DeviceError) as caught:
        client.read_input_registers(10000, 1)
    assert caught.value.kind == "timeout"
    client.close()


def test_unreachable_endpoint_is_a_connect_error():
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
    with pytest.raises(DeviceError) as caught:
        ReadOnlyModbusTcp("127.0.0.1", port, timeout=0.5).read_input_registers(10000, 1)
    assert caught.value.kind == "connect"


@pytest.mark.parametrize("address,count", [(-1, 1), (0, 0), (0, 126), (65535, 2), (1.5, 1), (True, 1), ("1", 1)])
def test_request_limits_are_enforced_before_anything_is_sent(modbus_server, address, count):
    client = client_for(modbus_server)
    with pytest.raises(ValueError):
        client.read_input_registers(address, count)
    assert modbus_server.connections == 0


def test_solarbank_read_issues_exactly_the_four_documented_reads():
    registers = {}
    for start, count in solarbank.BLOCKS:
        registers.update({start + i: (50 if start + i == 10014 else 0) for i in range(count)})
    server = FakeModbus(registers)
    try:
        client = client_for(server)
        assert solarbank.read(client)["soc"] == 50
        assert server.requests == list(solarbank.BLOCKS) and set(server.functions) == {4}
        client.close()
    finally:
        server.stop()


# ---- Shelly over HTTP -------------------------------------------------------------------------

STATUS = {f"em1:{i}": {"act_power": 363.1 if i == 0 else 3.1, "voltage": 235.6, "current": 1.5} for i in range(2)}
INFO = {"id": "shellyproem50-test", "mac": "AABBCCDDEEFF", "model": "SPEM-002CEBEU50", "gen": 2, "ver": "2.1.0", "auth_en": False}


@pytest.fixture
def shelly_server():
    server = FakeShelly({"/rpc/Shelly.GetStatus": (200, STATUS), "/rpc/Shelly.GetDeviceInfo": (200, INFO),
                         "/redirect": (302, b"", {"Location": "/rpc/Shelly.GetStatus"})})
    yield server
    server.stop()


def test_shelly_status_and_identity_use_get_only(shelly_server):
    sample = shelly.read("127.0.0.1", port=shelly_server.port)
    assert sample[SOLARBANK_AC] == 363.1 and sample[CAR] == 3.1
    info = shelly.device_info("127.0.0.1", port=shelly_server.port)
    assert info["model"] == "SPEM-002CEBEU50" and info["auth_enabled"] is False
    assert shelly_server.methods == ["GET", "GET"]


def test_shelly_http_errors_redirects_garbage_and_oversize_are_errors(shelly_server):
    shelly_server.routes["/rpc/Shelly.GetStatus"] = (500, b"oops")
    with pytest.raises(DeviceError):
        shelly.read("127.0.0.1", port=shelly_server.port)
    for body in (b"not json", b"x" * (shelly.MAX_BYTES + 10)):
        shelly_server.routes["/rpc/Shelly.GetStatus"] = (200, body)
        with pytest.raises(DeviceError):
            shelly.read("127.0.0.1", port=shelly_server.port)
    shelly_server.routes["/rpc/Shelly.GetStatus"] = (302, b"", {"Location": "/redirect"})
    with pytest.raises(DeviceError):
        shelly.read("127.0.0.1", port=shelly_server.port)


def test_shelly_timeout_and_refused_connection(shelly_server):
    shelly_server.delay = 1.0
    with pytest.raises(DeviceError) as caught:
        shelly.read("127.0.0.1", timeout=0.2, port=shelly_server.port)
    assert caught.value.kind == "timeout"
    probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
    with pytest.raises(DeviceError) as caught:
        shelly.read("127.0.0.1", timeout=0.5, port=port)
    assert caught.value.kind == "closed"


# ---- 4a.3d: fast/slow Solarbank reads and per-channel Shelly ------------------------------------

def split_registers(raw):
    registers = {}
    for start, values in raw.items():
        registers.update({int(start) + i: v for i, v in enumerate(values)})
    return registers


def test_fast_plus_slow_equals_the_full_decode_on_the_real_sample(recorded):
    raw = recorded["solarbank"][0]["raw"]
    regs = split_registers(raw)
    fast = solarbank.decode_fast([regs[10000 + i] for i in range(16)])
    slow = solarbank.decode_slow([[regs[start + i] for i in range(count)] for start, count in solarbank.SLOW_BLOCKS])
    assert {**fast, **slow} == recorded["solarbank"][0]["expected"]


def test_reader_makes_one_request_per_poll_and_rotates_the_slow_registers():
    regs = split_registers(FIXTURES_RAW()["solarbank"][0]["raw"])
    server = FakeModbus(regs)
    clock = {"t": 0.0}
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        reader = solarbank.Reader(client, slow_every=10, monotonic=lambda: clock["t"])
        first = reader()
        first_requests = len(server.requests)
        assert first_requests == 1 + len(solarbank.SLOW_BLOCKS)           # start: fast + every slow block
        server.requests.clear()
        for _ in range(3):
            clock["t"] += 1; again = reader()
        assert server.requests == [solarbank.FAST_BLOCK] * 3               # one 16-register request per poll
        strip = lambda d: {k: v for k, v in d.items() if k != "slow_age_s"}
        assert strip(again) == strip(first) and again["slow_age_s"] == 3.0   # slow values reused, with their age shown
        clock["t"] += 10; reader()
        assert len(server.requests) == 3 + 1 + len(solarbank.SLOW_BLOCKS)  # slow refreshed after 10 s
        assert set(server.functions) == {4}
        client.close()
    finally:
        server.stop()


def test_a_failing_slow_refresh_never_blocks_the_fast_data():
    regs = split_registers(FIXTURES_RAW()["solarbank"][0]["raw"])
    for address in (10036, 10037, 10038, 10039):
        regs.pop(address)                                                 # the device refuses the limits block
    server = FakeModbus(regs)
    clock = {"t": 0.0}
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        reader = solarbank.Reader(client, slow_every=10, monotonic=lambda: clock["t"])
        result = reader()
        assert result["soc"] == 71 and result["battery"] == 320            # control data present
        assert "charge_total_kwh" not in result and reader.slow_errors == 1   # slow data absent, not invented
        server.requests.clear(); clock["t"] += 1; reader()
        assert server.requests == [solarbank.FAST_BLOCK]                   # does not retry the refusing block every second
        client.close()
    finally:
        server.stop()


def FIXTURES_RAW():
    return json.loads(FIXTURES.read_text())


def test_shelly_per_channel_reads_match_the_full_status_parse(recorded):
    expected = from_monitor(recorded["shelly"][0]["expected"])
    raw = recorded["shelly"][0]["raw"]
    server = FakeShelly({"/rpc/EM1.GetStatus?id=0": (200, raw["em1:0"]), "/rpc/EM1.GetStatus?id=1": (200, raw["em1:1"])})
    try:
        assert shelly.read_channels("127.0.0.1", port=server.port) == expected
        assert server.methods == ["GET", "GET"]
    finally:
        server.stop()


def test_shelly_per_channel_error_makes_the_whole_read_an_error():
    ok = {"act_power": 1.0, "voltage": 230.0, "current": .1}
    server = FakeShelly({"/rpc/EM1.GetStatus?id=0": (200, ok), "/rpc/EM1.GetStatus?id=1": (200, {**ok, "errors": ["overpower"]})})
    try:
        with pytest.raises(DeviceError):
            shelly.read_channels("127.0.0.1", port=server.port)
    finally:
        server.stop()


def test_meter_makes_one_wide_request_and_decodes_the_garage_current():
    regs = {10632 + i: 0 for i in range(45)}
    regs[10632] = 2300                                  # primary phase 1 voltage: 230.0 V (GitHub issue 69)
    regs[10644], regs[10645] = 0xFFFF, 0xFFFB          # -5 W
    regs[10675], regs[10676] = 0xFFFF, 0xFEDB          # -293 W
    regs[10666] = 129                                  # secondary phase 1: 1.29 A (the 26 Oct evidence)
    regs[10667] = 0xFFB0                               # phase 2: a signed -0.80 A is read as the magnitude 0.80
    regs[10668] = 4510                                 # phase 3: 45.10 A: the limiter takes the largest of the three
    server = FakeModbus(regs)
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        result = meter.read(client)
        assert (result["main"], result["sub"]) == (-5, -293) and result["voltage"] == 230.0
        assert result["garage_phase_currents_a"] == [1.29, 0.8, 45.1] and result["garage_current_a"] == 45.1
        assert server.requests == [(10632, 45)] and set(server.functions) == {4}
        client.close()
    finally:
        server.stop()


# ---- review item 6: old slow values must not ride along with a fresh timestamp ------------------------

def test_slow_totals_stop_being_published_once_too_old_and_the_failures_are_visible():
    regs = split_registers(FIXTURES_RAW()["solarbank"][0]["raw"])
    server = FakeModbus(regs)
    clock = {"t": 0.0}
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        reader = solarbank.Reader(client, slow_every=10, monotonic=lambda: clock["t"])   # slow values allowed to be 30 s old
        first = reader()
        assert first["charge_total_kwh"] == 179.5 and first["slow_age_s"] == 0.0 and reader.health()["slow_stale"] is False
        for address in range(10262, 10266):
            server.registers.pop(address)                                   # the totals block starts refusing from now on
        seen_age = []
        for second in range(1, 61):
            clock["t"] = float(second)
            result = reader()
            assert result["soc"] == 71 and result["battery"] == 320          # control data is never affected
            if "charge_total_kwh" in result:
                seen_age.append(result["slow_age_s"])
        assert max(seen_age) <= 30.0                                         # never published older than the limit
        assert "charge_total_kwh" not in result and "slow_age_s" not in result   # gone by the end
        health = reader.health()
        assert health["slow_stale"] is True and health["slow_errors"] >= 5 and health["slow_error_streak"] >= 5
        client.close()
    finally:
        server.stop()


def test_without_the_totals_the_energy_observations_are_unavailable_not_stale():
    from solarbank_optimiser.collector import decode_snapshot
    sample = {"time": 100.0, "soc": 71, "pv": 60, "load": 380, "grid": 0, "battery": 320}      # no charge/discharge totals
    document = {"ok": True, "sample": {"time": 100.0, "main": 1, "sub": 2}, "shelly": {"ok": True, "sample": {"time": 100.0, "ch1": 1.0, "ch2": 2.0}},
                "solarbank": {"ok": True, "sample": sample}}
    result = {o.metric: o for o in decode_snapshot(document, 101.0)}
    assert result["soc"].quality == "ok" and result["battery_power"].quality == "ok"
    assert result["charge_energy"].quality != "ok" and result["discharge_energy"].quality != "ok"
    assert result["charge_energy"].value is None


def test_the_reader_publishes_the_mode_mask_and_backup_reserve_it_read_itself_with_their_age_and_forgets_them_when_they_stop_arriving():
    """GitHub 51: the start-up and running checks use the controller's own recent reading, not a log up to an hour old."""
    regs = split_registers(FIXTURES_RAW()["solarbank"][0]["raw"])
    regs.update({32774: 0b100001, 60000: 100, 60001: 10, 60002: 10, 60003: 0})
    server = FakeModbus(regs)
    clock = {"t": 0.0}
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        reader = solarbank.Reader(client, slow_every=10, monotonic=lambda: clock["t"], safety_every=30)
        value = reader()
        assert value["mask"] == 0b100001 and value["backup_soc_enable"] == 0 and value["safety_age_s"] == 0.0
        regs[60003] = 1                                                       # the owner switches Backup Reserve on in the Anker app
        clock["t"] += 10; assert reader()["backup_soc_enable"] == 0 and reader()["safety_age_s"] == 10.0     # not read again until the interval is up
        clock["t"] += 25; assert reader()["backup_soc_enable"] == 1
        regs.pop(32774)                                                         # the device now refuses the mask register: the old values must not be passed on as current
        for _ in range(4):
            clock["t"] += 25
            value = reader()
        assert "mask" not in value and "backup_soc_enable" not in value and reader.safety_errors >= 1
        client.close()
    finally:
        server.stop()
