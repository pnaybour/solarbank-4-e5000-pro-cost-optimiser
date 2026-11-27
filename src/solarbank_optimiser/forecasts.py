"""Coverage-aware Normal-day selection. No synthetic weather fallback."""

from datetime import date, datetime, timedelta
from .energy import LONDON, day_bounds
from .models import DemandMode, Observation, Quality, number


# A day may be used as the Normal reference although a few minutes are missing (typically a service restart during a release). The
# limits are deliberately small, and every filled minute is counted and reported with the forecast. Provisional: owner may tighten them.
MAX_FILLED_MINUTES = 10
MAX_FILLED_RUN = 5
LEVEL_CLAMP = 0.30                  # the learned daily level may move the profile by at most 30 % either way


class DayRows(list):
    """The observations of the chosen day, plus the minutes that had to be filled: {sample_at: kW}. Empty for a complete day."""
    filled = {}


def _runs(minutes):
    longest, run, previous = 0, 0, None
    for t in minutes:
        run = run + 1 if previous is not None and t - previous == 60 else 1
        longest, previous = max(longest, run), t
    return longest


def _fill(missing, ordered):
    """Each missing minute takes the mean of the nearest present minute before and after it (one side at the day's edges)."""
    times = [o.sample_at for o in ordered]
    values = {o.sample_at: o.value for o in ordered}
    out = {}
    for t in missing:
        before = max((x for x in times if x < t), default=None)
        after = min((x for x in times if x > t), default=None)
        neighbours = [values[x] for x in (before, after) if x is not None]
        out[t] = sum(neighbours) / len(neighbours)
    return out


def normal_day_candidates(observations, cutoff, max_age_days=7, commissioned_since=date(2026, 10, 10), max_missing=0, max_run=0):
    """Every complete, valid Normal day (London calendar) in the window, newest first. With max_missing / max_run above zero a day with
    a few missing minutes also qualifies; its rows then carry `.filled` (see DayRows). Duplicates, extra times and any invalid, non-Normal
    or non-60-second minute still disqualify a day."""
    number(cutoff, "cutoff", 0)
    if type(max_age_days) is not int or max_age_days < 1:
        raise ValueError("Maximum age must be positive whole days")
    today = datetime.fromtimestamp(cutoff, LONDON).date()
    by_day = {}
    for item in observations:
        o = item if isinstance(item, Observation) else Observation(**item)
        if o.metric != "household_power" or o.sample_at >= cutoff or o.retrieved_at > cutoff or o.coverage_seconds != 60:
            continue
        day = datetime.fromtimestamp(o.sample_at, LONDON).date()
        if not max(today - timedelta(days=max_age_days), commissioned_since) <= day < today:
            continue
        by_day.setdefault(day, []).append(o)
    for day, rows in sorted(by_day.items(), reverse=True):
        start, end = day_bounds(day)
        expected = set(range(int(start), int(end), 60))
        times = [o.sample_at for o in rows]
        present = set(times)
        missing = sorted(expected - present)
        if (end <= cutoff and len(times) == len(present) and present <= expected
                and len(missing) <= max_missing and (not missing or _runs(missing) <= max_run)
                and all(o.quality == Quality.OK and o.coverage_seconds == 60
                        and o.demand_mode == DemandMode.NORMAL and o.value >= 0 for o in rows)):
            ordered = DayRows(sorted(rows, key=lambda o: o.sample_at))
            ordered.filled = _fill(missing, ordered) if missing else {}
            yield day, ordered


def latest_normal_day(observations, cutoff, **options):
    """The most recent qualifying day (see normal_day_candidates), or ValueError."""
    for found in normal_day_candidates(observations, cutoff, **options):
        return found
    raise ValueError("No recent complete Normal day with verified coverage and demand tags")



def demand_horizon(observations, selection, revision, issued_at, start, end, average_days=1, level_kwh=None, default_kwh=None):
    """Minute profile over a UTC horizon, including scheduled visits and London DST.

    Repeated source clock minutes are averaged. High demand adds exactly the
    configured allowance over a complete London day (23/24/25 hours).
    No Normal day is needed when the entire horizon is Away.
    """
    from .settings import DemandSelection
    number(issued_at, "issued_at", 0)
    number(start, "start", issued_at)
    number(end, "end", start + 60, start + 72 * 3600)

    selection = DemandSelection(**selection) if isinstance(selection, dict) else selection
    boundaries = {start, end}
    boundaries.update(range((int(start)//60+1)*60, int(end)+1, 60))
    if selection:
        boundaries.update(t for t in (selection.start, selection.end) if t is not None and start < t < end)
    boundaries = sorted(boundaries)
    points = boundaries[:-1]
    modes = [selection.active_mode(t) if selection else DemandMode.NORMAL for t in points]
    reference = None
    filled = 0
    profile = {}
    level_scale = None
    default_used = False
    if any(mode != DemandMode.AWAY for mode in modes):
        # A spring-clock day lacks one clock hour; do not invent it. Try the next suitable day instead of giving up on the first.
        needed = {datetime.fromtimestamp(t, LONDON).hour * 60 + datetime.fromtimestamp(t, LONDON).minute
                  for t, mode in zip(points, modes) if mode != DemandMode.AWAY}
        candidates = list(normal_day_candidates(observations, issued_at, max_missing=MAX_FILLED_MINUTES, max_run=MAX_FILLED_RUN))
        default_used = False
        if not candidates:
            if not default_kwh:
                raise ValueError("No recent complete Normal day with verified coverage and demand tags")
            # Owner, 9 November: a Normal default. With no complete Normal day to copy (a return from Away, a fresh start) the day is the seeded level, flat across the day,
            # and the forecast says so; the first complete Normal day replaces it.
            profile = {minute: [default_kwh / 24.0] for minute in range(1440)}
            reference, default_used = f"default {default_kwh:.1f} kWh a day, flat (no complete Normal day yet)", True
        usable = []
        for reference_day, rows in candidates:
            day_profile = {}
            for o in rows:
                local = datetime.fromtimestamp(o.sample_at, LONDON)
                day_profile.setdefault(local.hour * 60 + local.minute, []).append(o.value)
            for t, kw in rows.filled.items():                         # clearly counted below; never zero, never silent
                local = datetime.fromtimestamp(t, LONDON)
                day_profile.setdefault(local.hour * 60 + local.minute, []).append(kw)
            if needed.issubset(day_profile):
                usable.append((reference_day, day_profile, len(rows.filled)))
                if len(usable) >= max(1, average_days):
                    break
        if not usable and not default_used:
            raise ValueError("Normal reference day lacks clock minutes required by this horizon")
        if usable:
            reference, profile, filled = usable[0]                  # the newest suitable day; with `average_days` above 1 the profile is the mean of that many suitable days
        if len(usable) > 1:
            merged = {}
            for _day, day_profile, _filled in usable:
                for minute, values in day_profile.items():
                    merged.setdefault(minute, []).append(sum(values) / len(values))
            profile = {minute: values for minute, values in merged.items() if all(minute in d for _x, d, _y in usable)}
            reference = f"{usable[0][0]} (mean of {len(usable)} days)"
        if level_kwh and not default_used:
            # The learned daily level (seeded, see demand_learning.normal_level) sets how much the day totals; the profile only supplies its shape. Held within LEVEL_CLAMP of
            # the shape's own total so one odd level can never reshape the plan by much.
            shape_kwh = sum(sum(v) / len(v) for v in profile.values()) / 60 * (1440 / len(profile))
            if shape_kwh > 0:
                scale = max(1 - LEVEL_CLAMP, min(1 + LEVEL_CLAMP, level_kwh / shape_kwh))
                profile = {minute: [x * scale for x in values] for minute, values in profile.items()}
                level_scale = round(scale, 4)
    result = []
    for t, next_t, mode in zip(points, boundaries[1:], modes):
        local = datetime.fromtimestamp(t, LONDON)
        if mode == DemandMode.AWAY:
            kw = selection.away_watts / 1000
        else:
            values = profile[local.hour * 60 + local.minute]
            kw = sum(values) / len(values)
            if mode == DemandMode.HIGH and selection.high_extra_kwh is not None:
                left, right = day_bounds(local.date())
                kw += selection.high_extra_kwh / ((right - left) / 3600)
        result.append({"start": t, "end": next_t, "demand_kw": kw,
                       "demand_mode": str(mode), "demand_revision": revision})
    return {"model": "normal-clock-profile-v1", "issued_at": issued_at,
            "reference_day": str(reference) if reference else None, "gap_filled_minutes": filled,
            "selection": selection.model_dump() if selection else None,
            "revision": revision, "intervals": result, "level_kwh": level_kwh if level_scale is not None else None, "level_scale": level_scale, "default_used": default_used,
            "demand_kwh": sum(r["demand_kw"] * (r["end"]-r["start"]) / 3600 for r in result),
            "assumption": "User allowances provisional; no learned uplift; forecast only" + ("; Normal demand is the DEFAULT, flat, because no complete Normal day exists yet" if default_used else "")
                      + (f"; reference day has {filled} minute(s) filled from neighbouring minutes" if filled else "")}
