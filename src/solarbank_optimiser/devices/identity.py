"""Positive device identity from read-only registers (so an address alone is never trusted).

Serial numbers are never stored or logged in clear: only a salted SHA-256 digest is kept.
"""
import hashlib

from .errors import DeviceError

SALT = "solarbank-cost-optimiser:identity:v1"

# name: (address, register count, vendor key in the pinned map)
SOLARBANK = {"product": (0x8000, 5, "device_model"), "serial": (10100, 12, "device_sn")}
METER = {"model": (10620, 10, "meter_model"), "serial": (10702, 10, "meter_sn")}


def decode_string(registers):
    """Two bytes per register, high byte first, NUL padded."""
    raw = b"".join(bytes(((r >> 8) & 0xFF, r & 0xFF)) for r in registers)
    return raw.decode("utf-8", errors="ignore").replace("\x00", "").strip()


def digest(serial):
    if not serial:
        raise DeviceError("invalid", "Empty serial number")
    return hashlib.sha256((SALT + serial).encode()).hexdigest()


def _read(client, layout, serials):
    result = {}
    for name, (address, count, _key) in layout.items():
        text = decode_string(client.read_input_registers(address, count))
        if not text:
            raise DeviceError("invalid", f"Identity field {name} is empty")
        result[name + ("_digest" if name in serials else "")] = digest(text) if name in serials else text
    return result


def read_solarbank(client):
    return _read(client, SOLARBANK, {"serial"})


def read_meter(client):
    return _read(client, METER, {"serial"})


def matches(expected, actual):
    """True only if every expected field is present and equal."""
    return bool(expected) and all(actual.get(key) == value for key, value in expected.items())
