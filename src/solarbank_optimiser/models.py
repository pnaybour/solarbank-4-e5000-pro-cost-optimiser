"""Validated interfaces shared by the separate application processes.

UTC epoch seconds are persisted. Elapsed time uses a separate monotonic clock.
No adapter is permitted to manufacture a device update time from retrieval time.
"""

from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import math
import time


def number(value, name="value", minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} outside permitted range")
    return float(value)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def time_keyed(at, value):
    """Content hash prefixed by time, for primary keys of ever-growing tables.

    A pure hash scatters each new row across the whole index, dirtying one
    page per row; a time prefix makes inserts append at the end. The 'g' sorts
    after every older all-hex key, and equal content still gives an equal key.
    """
    return f"g{int(at):012x}{fingerprint(value)}"


class Quality(StrEnum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    ERROR = "error"


class DemandMode(StrEnum):
    NORMAL = "Normal consumption"
    AWAY = "Away from home"
    HIGH = "High demand"


UNITS = {
    "household_power": "kW", "pv_power": "kW", "battery_power": "kW",
    "grid_power": "kW", "ac_power": "kW", "ev_power": "kW",
    "garage_current": "A", "voltage": "V", "soc": "fraction",
    "charge_energy": "kWh", "discharge_energy": "kWh",
    "grid_ct_power": "kW", "garage_power": "kW",
}


@dataclass(frozen=True)
class Observation:
    source: str
    metric: str
    retrieved_at: float
    value: float | None
    unit: str
    quality: Quality = Quality.OK
    device_at: float | None = None
    raw_value: float | None = None
    raw_unit: str | None = None
    coverage_seconds: float | None = None
    demand_mode: DemandMode | None = None
    provenance: str = "live"
    interval_start: float | None = None
    source_at: float | None = None
    demand_revision: int | None = None
    # Present only on one-minute log rows: summary of the 1 Hz samples behind this row.
    # `value` stays the last sample of the minute; the mean is a plain average of OK samples.
    window_samples: int | None = None
    window_ok: int | None = None
    window_mean: float | None = None
    window_max: float | None = None

    def __post_init__(self):
        if not self.source or self.metric not in UNITS or self.unit != UNITS[self.metric]:
            raise ValueError("Unknown metric, source or canonical unit")
        Quality(self.quality)
        number(self.retrieved_at, "retrieved_at", 0)
        if self.device_at is not None:
            number(self.device_at, "device_at", 0, self.retrieved_at)
        if self.quality == Quality.OK and self.value is None:
            raise ValueError("An OK observation requires a value")
        if self.quality != Quality.OK and self.value is not None:
            raise ValueError("Unavailable observations must not carry a usable value")
        if self.value is not None:
            number(self.value)
            if self.metric == "soc":
                number(self.value, "SOC", 0, 1)
            if self.metric in ("voltage", "charge_energy", "discharge_energy", "pv_power"):
                number(self.value, self.metric, 0)
        if self.raw_value is not None:
            number(self.raw_value, "raw_value")
        if self.coverage_seconds is not None:
            number(self.coverage_seconds, "coverage_seconds", 0, 60)
        if self.interval_start is not None:
            number(self.interval_start, "interval_start", 0, self.retrieved_at)
        if self.source_at is not None:
            number(self.source_at, "source_at", 0, self.retrieved_at)
        if self.demand_revision is not None and (type(self.demand_revision) is not int or self.demand_revision < 0):
            raise ValueError("Invalid demand revision")
        if self.demand_mode is not None:
            DemandMode(self.demand_mode)

    def fresh(self, now, max_age, skew=0.0):
        """OK and no older than max_age. `skew` is how far AFTER `now` the reading may be stamped: a caller that notes the time
        and then spends seconds loading its data will meet readings taken meanwhile. Anything further ahead is still rejected."""
        oldest = min(t for t in (self.retrieved_at, self.source_at, self.device_at) if t is not None)
        age = number(now) - oldest
        return self.quality == Quality.OK and -number(skew, "skew", 0) <= age <= number(max_age, "max_age", 0)

    @property
    def id(self):
        return time_keyed(self.retrieved_at, asdict(self))

    @property
    def sample_at(self):
        if self.interval_start is not None:
            return self.interval_start
        return self.source_at if self.source_at is not None else self.retrieved_at


@dataclass(frozen=True)
class ForecastInterval:
    start: float
    end: float
    demand_kw: float
    pv_kw: float

    def __post_init__(self):
        number(self.start, "start", 0)
        number(self.end, "end", self.start)
        if self.end == self.start:
            raise ValueError("Empty interval")
        number(self.demand_kw, "demand", 0)
        number(self.pv_kw, "PV", 0)


@dataclass(frozen=True)
class PriceInterval:
    start: float
    end: float
    price: float
    source: str
    cheap: bool = False

    def __post_init__(self):
        number(self.start, "start", 0)
        number(self.end, "end", self.start)
        number(self.price, "GBP/kWh")
        if self.start == self.end or not self.source or type(self.cheap) is not bool:
            raise ValueError("Invalid tariff interval")


@dataclass(frozen=True)
class Plan:
    issued_at: float
    valid_until: float
    inputs_fingerprint: str
    grid_target: float
    status: str = "shadow"

    def __post_init__(self):
        number(self.issued_at, "issued_at", 0)
        number(self.valid_until, "valid_until", self.issued_at)
        number(self.grid_target, "grid_target", .05, 1)
        if self.valid_until == self.issued_at or self.status != "shadow":
            raise ValueError("Only time-limited shadow plans are supported in this release")
        if len(self.inputs_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in self.inputs_fingerprint):
            raise ValueError("A SHA-256 input fingerprint is required")

    def current(self, now, expected_fingerprint):
        return self.issued_at <= now < self.valid_until and self.inputs_fingerprint == expected_fingerprint


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    requested_at: float
    expires_at: float
    acknowledged_at: float | None = None
    readback_at: float | None = None
    observed_at: float | None = None

    def __post_init__(self):
        if not self.command_id:
            raise ValueError("Command ID required")
        number(self.requested_at, "requested_at", 0)
        number(self.expires_at, "expires_at", self.requested_at)
        if self.expires_at == self.requested_at:
            raise ValueError("A command needs an expiry after its issue time")
        for value in (self.acknowledged_at, self.readback_at, self.observed_at):
            if value is not None:
                number(value, "command evidence time", self.requested_at)


class Clock:
    def utc(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()


@dataclass
class FakeClock:
    wall: float
    elapsed: float = 0

    def utc(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        seconds = number(seconds, "seconds", 0)
        self.wall += seconds
        self.elapsed += seconds

