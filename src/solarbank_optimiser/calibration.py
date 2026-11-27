"""A11: keep the battery management system calibrated by making sure the battery reaches 100 % at least once a week.

The Anker app's own SOC Calibration does this, at full power and regardless of the electricity price. The owner will turn it off once this exists, so the
planner takes over: when the battery has not been full for about `max_days - 1` days it is told to charge fully in the next cheap-rate window (the same mechanism
as the High household mode's full overnight charge), whatever the cost-optimal level would have been. Solar filling the battery to 100 % counts, so in summer this
almost never triggers. Standard library only."""
from datetime import timedelta

SETTING = "calibration"
DEFAULT_DAYS = 7
MIN_DAYS, MAX_DAYS = 3, 14
FULL = 0.99                    # a minute at or above this counts as full
DAY = 86400.0
RECHECK_S = 600.0              # the last-full lookup is repeated at most this often per database


def valid_days(value):
    """The number of days if it is a whole number in range, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value != int(value):
        return None
    return int(value) if MIN_DAYS <= value <= MAX_DAYS else None


def stored_days(payload):
    return valid_days((payload or {}).get("days")) or DEFAULT_DAYS


_cache = {}


def last_full(store, now, lookback_days=MAX_DAYS + 1):
    """(time the battery was last at or above 100 %, earliest time we have any battery reading from), either may be None.
    The one-minute detail covers 90 days; older days come from the daily record's `first_full_at`."""
    key = str(getattr(store, "path", id(store)))
    cached = _cache.get(key)
    if cached and 0 <= now - cached[0] < RECHECK_S:
        return cached[1]
    start = now - lookback_days * DAY
    source = store.minute_source()
    peaks = source.peaks("soc.max.wsp", start, now + 1)
    newest = max((m for m, v in peaks.items() if v >= FULL), default=None)
    seen = source.minutes("soc", start, now + 1)
    first = min(seen, default=None)          # earliest battery reading within the look-back (the minute detail is kept 90 days)
    from .energy import LONDON
    from datetime import datetime
    today = datetime.fromtimestamp(now, LONDON).date()
    for back in range(0, lookback_days + 1):
        record = store.get_setting("daily_record:" + (today - timedelta(days=back)).isoformat())[1]
        stamp = ((record or {}).get("solar") or {}).get("first_full_at")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp <= now and (newest is None or stamp > newest):
            newest = float(stamp)
    result = (newest, first)
    _cache[key] = (now, result)
    return result


def state(store, now, max_days):
    """The calibration position: {"state": "ok"|"due"|"unknown", "last_full_at", "days_since", "max_days", "due_after_days", "coverage_days"}.
    `due` once the last full charge is at least max_days - 1 days old; `unknown` while we have not watched long enough to say."""
    newest, first = last_full(store, now)
    due_after = max_days - 1
    coverage = None if first is None else max(0.0, (now - first) / DAY)
    out = {"max_days": max_days, "due_after_days": due_after, "last_full_at": newest, "days_since": None if newest is None else round((now - newest) / DAY, 2),
           "coverage_days": None if coverage is None else round(coverage, 2)}
    if newest is not None:
        out["state"] = "due" if (now - newest) / DAY >= due_after else "ok"
    elif coverage is not None and coverage >= due_after:
        out["state"] = "due"                                    # watched for long enough and never saw it full
    else:
        out["state"] = "unknown"
    return out


def first_window_reachable(windows, soc, capacity_kwh, ac_limit_kw, efficiency, margin=0.9):
    """Whether the first cheap window is long enough to take the battery from `soc` to 100 % at the AC limit (with a margin for the house load sharing the interface)."""
    if not windows:
        return False
    hours = sum(r["end"] - r["start"] for r in windows[0]["intervals"]) / 3600
    return hours * ac_limit_kw * efficiency * margin >= max(0.0, 1.0 - soc) * capacity_kwh


def force_first_night(policy, baseline, soc, model):
    """The charge policy with the next cheap window set to a full charge, plus whether that was done and why not if not. `baseline` is the policy that asks
    for a full charge in every upcoming night; its first window is used. The forced window is added to any High-demand windows already in `policy`."""
    windows = baseline.get("windows") or []
    if not windows:
        return policy, False, "no cheap-rate window in the next 48 hours"
    if not first_window_reachable(windows, soc, model.capacity_kwh, model.ac_limit_kw, model.grid_charge_efficiency):
        return policy, False, "the next cheap-rate window is too short for a full charge; trying the next one"
    first = windows[0]
    existing = [w for w in (policy.get("windows") or []) if w["night_date"] != first["night_date"]]
    merged = sorted(existing + [first], key=lambda w: w["night_date"])
    if policy.get("kind") == "full_overnight":
        forced = {**policy, "windows": merged}
    else:
        forced = {**baseline, "windows": merged, "reason": "Battery calibration: a full charge is due, so the next cheap window charges to 100 %",
                  "status": "calibration_charge"}
    return forced, True, None
