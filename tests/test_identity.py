import pytest

from fake_devices import FakeModbus
from solarbank_optimiser.devices import DeviceError, identity
from solarbank_optimiser.devices.modbus import ReadOnlyModbusTcp


def words(text, count):
    data = text.encode().ljust(count * 2, b"\x00")
    return [(data[i] << 8) | data[i + 1] for i in range(0, count * 2, 2)]


def server_with(**fields):
    registers = {}
    for address, count, text in fields["strings"]:
        registers.update({address + i: w for i, w in enumerate(words(text, count))})
    return FakeModbus(registers)


def test_string_decoding_high_byte_first_and_nul_padded():
    assert identity.decode_string([0x4142, 0x4300, 0x0000]) == "ABC"
    assert identity.decode_string([0x2020, 0x4458, 0x0000]) == "DX"


def test_solarbank_identity_keeps_only_a_digest_of_the_serial():
    server = server_with(strings=[(0x8000, 5, "DN7M"), (10100, 12, "SB-TEST-12345678")])
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        result = identity.read_solarbank(client)
        client.close()
    finally:
        server.stop()
    assert result["product"] == "DN7M" and "serial" not in result
    assert result["serial_digest"] == identity.digest("SB-TEST-12345678")
    assert "SB-TEST-12345678" not in str(result)
    assert set(server.functions) == {4}


def test_meter_identity_and_matching():
    server = server_with(strings=[(10620, 10, "Smart Meter Gen 2"), (10702, 10, "MT-TEST-0001")])
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        actual = identity.read_meter(client)
        client.close()
    finally:
        server.stop()
    expected = {"serial_digest": identity.digest("MT-TEST-0001")}
    assert identity.matches(expected, actual) and not identity.matches({"serial_digest": "wrong"}, actual)
    assert not identity.matches({}, actual)               # nothing expected means nothing proven


def test_empty_identity_field_is_an_error_not_a_match():
    server = server_with(strings=[(0x8000, 5, ""), (10100, 12, "X")])
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        with pytest.raises(DeviceError):
            identity.read_solarbank(client)
        client.close()
    finally:
        server.stop()


def test_refused_identity_register_surfaces_as_unsupported():
    server = server_with(strings=[(10100, 12, "X")])        # 0x8000 absent -> Modbus exception
    try:
        client = ReadOnlyModbusTcp("127.0.0.1", server.port, timeout=1)
        with pytest.raises(DeviceError) as caught:
            identity.read_solarbank(client)
        assert caught.value.kind == "exception"
        client.close()
    finally:
        server.stop()
