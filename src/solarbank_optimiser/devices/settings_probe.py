"""C1: a read-only look at the Solarbank's settings registers, to learn how the Anker app's modes and limits appear on the device. Nothing is ever written.

Every address read here is in the vendor's own pinned read map (reference/anker-solix-official/): the holding-register batches 10060-10072, 10074-10081 and
60000-60003, the input-register range 32768-32774, and the single input read of 0x8007 (32775). Registers the vendor names are decoded with the vendor's own
labels; everything else is kept as raw numbers and never interpreted. Each item is read on its own, so one refused or unimplemented register cannot hide the
others; a refusal is itself a finding (for example, firmware that does not implement 0x8007)."""
import json
import os
import time
from pathlib import Path

from . import anker_vendor
from .errors import DeviceError

# The vendor's labels and bits live in anker_vendor.py (adapted from the vendor's own map); the probe reads them from there.
OPERATING_MODES = anker_vendor.OPERATING_MODES
MODE_MASK_BITS = {bit: anker_vendor.OPERATING_MODES[value] for value, bit in sorted(anker_vendor.MODE_CAPABILITY_BIT.items(), key=lambda item: item[1])}     # 0x8006 bit -> the mode it enables
CAPABILITY_BITS = anker_vendor.CAPABILITY_BITS                                                                              # 0x8007 bit -> the capability it reports
LIMIT_NAMES = ("charging_limit_soc", "discharge_limit_soc", "backup_reserve_soc", "backup_soc_enable")

# name: (function code, address, count)
READS = {
    "device_model": (4, 32768, 5),
    "ems_mode_mask": (4, 32774, 1),
    "capability_mask": (4, 32775, 1),
    "operating_mode": (3, 10064, 1),
    "limits": (3, 60000, 4),
    "holding_10060_10072": (3, 10060, 13),
    "holding_10074_10081": (3, 10074, 8),
}


def _text(words):
    raw = bytes(b for w in words for b in (w >> 8, w & 0xFF)).rstrip(b"\x00 ")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return None


def _bits(value, names):
    return [name for bit, name in names.items() if value >> bit & 1]


def read_item(client, name):
    """One item's registers: {"ok": True, "raw": [...]} or {"ok": False, "error": kind, "detail": text}. Never raises for a refused or failed read."""
    function, address, count = READS[name]
    read = client.read_input_registers if function == 4 else client.read_holding_registers
    try:
        return {"ok": True, "raw": read(address, count)}
    except DeviceError as exc:
        return {"ok": False, "error": exc.kind, "detail": str(exc)}


def read_settings(client, now=None):
    """One snapshot: {"at", "items": {name: {"ok", "raw" | "error"}}, "decoded": {...}}. Never raises for a refused or failed item."""
    items = {name: read_item(client, name) for name in READS}
    return {"at": time.time() if now is None else now, "items": items, "decoded": decode(items)}


def decode(items):
    ok = lambda name: items.get(name, {}).get("ok")
    raw = lambda name: items[name]["raw"]
    out = {}
    if ok("device_model"):
        out["device_model"] = _text(raw("device_model"))
    if ok("operating_mode"):
        code = raw("operating_mode")[0]
        out["operating_mode"] = {"code": code, "name": OPERATING_MODES.get(code, f"unknown ({code})")}
    if ok("ems_mode_mask"):
        out["ems_mode_mask"] = {"raw": raw("ems_mode_mask")[0], "modes_enabled": _bits(raw("ems_mode_mask")[0], MODE_MASK_BITS)}
    if ok("capability_mask"):
        out["capability_mask"] = {"raw": raw("capability_mask")[0], "supported": _bits(raw("capability_mask")[0], CAPABILITY_BITS)}
    else:
        out["capability_mask"] = {"raw": None, "supported": None, "note": "not answered (" + items.get("capability_mask", {}).get("error", "not read") + "): this firmware may not implement it"}
    if ok("limits"):
        out["limits"] = dict(zip(LIMIT_NAMES, raw("limits")))
    if ok("holding_10074_10081") and len(raw("holding_10074_10081")) > 10080 - 10074:
        # Specification 14 item 4, GitHub issue 72: on the Anker X1, 10080 is a communication watchdog; on the Solarbank 4 it is undocumented. Read it, never
        # write it, and do not interpret it: the raw word only, so commissioning test T6 (loss of communication) can see whether it ever changes.
        out["register_10080"] = {"raw": raw("holding_10074_10081")[10080 - 10074], "note": "undocumented on the Solarbank 4 (a communication watchdog on the X1); read only"}
    return out


# Registers 10060 and 10061 together read as Unix seconds and advance one a second (seen 29 Oct: 1793309598 against the Pi's 1793309607): apparently the device's own clock.
# That is an observation, not a documented meaning, so the words stay in the raw record but are left out of change detection (or every minute would be a "change").
CLOCK_WORDS = {"holding_10060_10072": (0, 1)}


def comparable(snapshot):
    """What counts as 'changed': the raw register values and errors, not the time and not the apparent device clock."""
    items = {name: dict(item) for name, item in snapshot["items"].items()}
    for name, indexes in CLOCK_WORDS.items():
        raw = items.get(name, {}).get("raw")
        if raw:
            items[name]["raw"] = [None if i in indexes else w for i, w in enumerate(raw)]
    return json.dumps(items, sort_keys=True)


class SettingsLog:
    """One JSON line when the settings change, plus a heartbeat line every `heartbeat` seconds, so a quiet week is still visibly alive. Size-capped with one rotation."""

    def __init__(self, path, heartbeat=3600.0, max_bytes=2 * 1024 * 1024):
        self.path, self.heartbeat, self.max_bytes = Path(path), heartbeat, max_bytes
        self._last, self._last_written = None, None

    def record(self, snapshot):
        """Appends when the settings differ from the last snapshot seen by this object, or the heartbeat is due. Returns "changed", "heartbeat" or None."""
        key = comparable(snapshot)
        due = self._last_written is None or snapshot["at"] - self._last_written >= self.heartbeat
        changed = key != self._last
        self._last = key
        if not (changed or due):
            return None
        if self.path.exists() and self.path.stat().st_size > self.max_bytes:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        with open(self.path, "a") as handle:
            handle.write(json.dumps({**snapshot, "event": "changed" if changed else "heartbeat"}, separators=(",", ":")) + "\n")
        self._last_written = snapshot["at"]
        return "changed" if changed else "heartbeat"
