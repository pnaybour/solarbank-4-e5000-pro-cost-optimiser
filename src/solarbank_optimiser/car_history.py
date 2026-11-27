"""London-date charger reports. Coverage is recorder coverage, not device updates."""

from contextlib import closing
from datetime import timedelta
import json

from .energy import day_bounds


def car_history(store, first, last):
    """Sessions and one summary row per London day. The per-minute power series is separate (car_power_minutes), paged,
    because a year of it is half a million rows and must never be built in memory in one go."""
    if not 0 <= (last - first).days <= 366:
        raise ValueError("Choose an ordered range of at most 367 days")
    start, end = day_bounds(first)[0], day_bounds(last + timedelta(days=1))[0]
    source = store.minute_source()
    with closing(store.connect()) as db:
        sessions = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM charger_sessions WHERE started_at<? AND (ended_at IS NULL OR ended_at>=?) ORDER BY started_at",
            (end, start))]
        daily = []
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            a, b = day_bounds(day)
            coverage = source.covered_seconds("ev_power", a, b)          # valid seconds of charger power, from the telemetry count file
            energy, active = db.execute("SELECT coalesce(sum(kwh),0),coalesce(sum(active_seconds),0) FROM charger_energy WHERE start>=? AND end<=?", (a, b)).fetchone()
            partial_session = any(s["partial"] and s["started_at"] < b and (s["ended_at"] is None or s["ended_at"] >= a) for s in sessions)
            daily.append({"date": day.isoformat(), "charging_kwh": energy if coverage else None,
                          "active_seconds": active if coverage else None, "covered_seconds": coverage,
                          "expected_seconds": b-a, "partial": coverage < b-a-1e-6 or partial_session})
    available = [d["charging_kwh"] for d in daily if d["charging_kwh"] is not None]
    return {"sessions": sessions, "daily": daily,
            "charging_kwh": sum(available) if available else None,
            "partial": any(d["partial"] for d in daily),
            "description": "Measured charger-circuit energy while charging; includes charger losses. Sub-10 W standby excluded.",
            "coverage_basis": "Adjacent valid upstream recorder samples, with gaps excluded; independent device refresh unverified"}



def car_power_minutes(store, first, last, limit=1440, after=None):
    """One bounded page of per-minute charger power for the London dates first..last. Returns (rows, next_after);
    next_after is the last minute returned (pass it back as `after`) or None when the range is finished."""
    if not 0 <= (last - first).days <= 366:
        raise ValueError("Choose an ordered range of at most 367 days")
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("Page size must be 1 to 5000 minutes")
    start, end = day_bounds(first)[0], day_bounds(last + timedelta(days=1))[0]
    if after is not None:
        if isinstance(after, bool) or not isinstance(after, (int, float)):
            raise ValueError("Invalid cursor")
        start = max(start, after + 60)
    source = store.minute_source()
    from .minute_source import floor_minute
    start = max(start, floor_minute(store.telemetry_clock()))          # per-minute power is kept 90 days (89 are read)
    found = source.minutes("ev_power", start, end)
    rows = [{"minute_utc": minute, "mean_kw": item.mean, "covered_seconds": item.covered} for minute, item in sorted(found.items())[:limit]]
    return rows, (rows[-1]["minute_utc"] if len(rows) == limit else None)
