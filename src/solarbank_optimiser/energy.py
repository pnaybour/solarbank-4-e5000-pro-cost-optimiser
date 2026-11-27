"""Pure signed/threshold integration with explicit coverage, adapted from R04."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import number

LONDON = ZoneInfo("Europe/London")


def day_bounds(day):
    return (datetime.combine(day, time.min, LONDON).timestamp(),
            datetime.combine(day + timedelta(days=1), time.min, LONDON).timestamp())


@dataclass(frozen=True)
class EnergyPiece:
    start: float
    end: float
    positive_kwh: float
    negative_kwh: float
    active_seconds: float


def integrate(start, end, first_kw, last_kw, max_gap, threshold_kw=None):
    """Linear adjacent samples; split all minute, zero and threshold crossings.

    For EV history, count full power only on the >=10 W side of a crossing.
    The classifier's release delay never adds standby energy or active time.
    A caller must break adjacency at restart or any invalid observation.
    """
    start, end = number(start), number(end)
    first_kw, last_kw = number(first_kw), number(last_kw)
    max_gap = number(max_gap, "max_gap", 0)
    if threshold_kw is not None:
        threshold_kw = number(threshold_kw, "threshold", 0)
    if not 0 < end - start <= max_gap:
        return []
    cuts = {start, end}
    boundary = (int(start) // 60 + 1) * 60
    while boundary < end:
        cuts.add(boundary)
        boundary += 60
    for level in (0, threshold_kw):
        if level is not None and (first_kw - level) * (last_kw - level) < 0:
            cuts.add(start + (end - start) * (level - first_kw) / (last_kw - first_kw))
    pieces = []
    ordered = sorted(cuts)
    for a, b in zip(ordered, ordered[1:]):
        va = first_kw + (last_kw - first_kw) * (a - start) / (end - start)
        vb = first_kw + (last_kw - first_kw) * (b - start) / (end - start)
        active = threshold_kw is None or (va + vb) / 2 >= threshold_kw
        energy = (va + vb) / 2 * (b - a) / 3600 if active else 0
        pieces.append(EnergyPiece(a, b, max(energy, 0), max(-energy, 0), b - a if active else 0))
    return pieces

