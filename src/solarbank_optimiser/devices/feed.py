"""The meter feed: the controller owns the meter (it allows ONE Modbus client) and writes each reading to a small RAM file;
anything else that wants the meter reads that file instead of opening a second connection. Standard library only.

A reading is judged by when the METER was read (recorded in the file), not by when this process read the file: a controller that has
stopped makes the feed go stale and the reading becomes unavailable, never an old value that looks fresh."""
import json
import math
import time

from .errors import DeviceError

MAX_BYTES = 8192
NUMBERS = ("main", "sub", "garage_current_a", "voltage")      # GitHub issue 69: the meter's own voltage, for the reconciliation and voltage validity checks


class FileFeedReader:
    """A reader with the same shape as the real meter reader: call it for a reading, or get a DeviceError."""

    def __init__(self, path, max_age=2.5, clock=time.time, monotonic=time.monotonic):
        self.path, self.max_age, self._clock, self._mono = path, max_age, clock, monotonic
        self.received_at = None          # monotonic time of the METER reading, which the device loop uses as the reading's stamp

    def __call__(self):
        self.received_at = None
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        except FileNotFoundError:
            raise DeviceError("connect", "Meter feed missing: the controller is not publishing the meter") from None
        except OSError as error:
            raise DeviceError("closed", f"Meter feed unreadable ({error.__class__.__name__})") from None
        try:
            if len(raw) > MAX_BYTES:
                raise ValueError("size")
            document = json.loads(raw.decode("utf-8"))
            at, values = document["at"], document["values"]
            if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or not isinstance(values, dict):
                raise ValueError("shape")
            result = {}
            for key in NUMBERS:
                value = values[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(key)
                result[key] = value
            phases = values.get("garage_phase_currents_a")
            if isinstance(phases, list) and len(phases) <= 3 and all(isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p) for p in phases):
                result["garage_phase_currents_a"] = list(phases)
        except (ValueError, KeyError, TypeError, RecursionError, UnicodeDecodeError):
            raise DeviceError("invalid", "Meter feed is not a valid reading") from None
        age = self._clock() - at
        if age < -1.0:
            raise DeviceError("invalid", "Meter feed is dated in the future (clock problem)")
        if age > self.max_age:
            raise DeviceError("timeout", f"Meter feed is {age:.1f} s old: the controller has stopped reading the meter")
        self.received_at = self._mono() - max(0.0, age)
        return result
