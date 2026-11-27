"""Household selections and effective-dated fallback tariff configuration."""

from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import DemandMode


class DemandSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    mode: str = DemandMode.NORMAL
    start: float = Field(ge=0)
    end: float | None = Field(default=None, ge=0)
    away_watts: float | None = Field(default=None, ge=0, le=25000)
    high_extra_kwh: float | None = Field(default=None, ge=0, le=100)
    parameter_source: str = Field(default="User setting, provisional", max_length=160)

    @model_validator(mode="after")
    def validate_selection(self):
        DemandMode(self.mode)
        if self.end is not None and self.end <= self.start:
            raise ValueError("Return/end must be later than start")
        if self.mode == DemandMode.AWAY and self.away_watts is None:
            raise ValueError("Set the essential base load in watts first")
        return self

    def active_mode(self, at):
        return DemandMode(self.mode) if at >= self.start and (self.end is None or at < self.end) else DemandMode.NORMAL


class FallbackTariff(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    name: str = Field(default="Manual fallback", max_length=80)
    source: str = Field(default="User-entered prices", max_length=300)
    confirmed: bool = True
    day_pence: float
    cheap_pence: float
    valid_from: float = Field(ge=0)
    valid_until: float | None = Field(default=None, ge=0)
    start_minute: int = Field(default=1410, ge=0, lt=1440)
    end_minute: int = Field(default=330, ge=0, lt=1440)

    @model_validator(mode="after")
    def validate_tariff(self):
        if self.start_minute == self.end_minute:
            raise ValueError("Cheap window must have different start and end")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("Tariff expiry must follow its effective date")
        return self

    def at(self, epoch):
        if epoch < self.valid_from or self.valid_until is not None and epoch >= self.valid_until:
            raise ValueError("Fallback tariff is missing or expired for this interval")
        local = datetime.fromtimestamp(epoch, ZoneInfo("Europe/London"))
        minute = local.hour * 60 + local.minute
        cheap = (self.start_minute <= minute < self.end_minute if self.start_minute < self.end_minute
                 else minute >= self.start_minute or minute < self.end_minute)
        return (self.cheap_pence if cheap else self.day_pence) / 100, cheap


def london_input(text, fold=None):
    """Reject skipped clocks; require an explicit choice for repeated clocks."""
    naive = datetime.fromisoformat(text)
    if naive.tzinfo is not None:
        return naive.timestamp()
    zone = ZoneInfo("Europe/London")
    candidates = []
    for candidate_fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=candidate_fold)
        if datetime.fromtimestamp(candidate.timestamp(), zone).replace(tzinfo=None) == naive:
            candidates.append(candidate)
    if not candidates:
        raise ValueError("This London clock time does not exist because the clocks change")
    if len({d.timestamp() for d in candidates}) > 1 and fold is None:
        raise ValueError("This London time occurs twice; specify first or second occurrence")
    if fold not in (None, 0, 1):
        raise ValueError("fold must be 0 or 1")
    return naive.replace(tzinfo=zone, fold=fold or 0).timestamp()

