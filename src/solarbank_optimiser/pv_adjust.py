"""GitHub issue 94, step 2: a learned correction of tomorrow's solar forecast, made BESIDE the current one and never used by the planner.

Every evening after 18:00, once tomorrow's forecast has been saved (`daily_record.snapshot_forecasts`), this builds the training history from the saved
evening-before forecasts and the measured solar in the telemetry files, one row per hour, and asks `pv_adjust_child.py` (in the solver environment, where
scikit-learn lives) for a corrected forecast. The result is saved as `adjusted_forecast_for_day:<date>`; when the day is over, the daily record scores it beside
the uncorrected one, and the History page shows both. Whether the planner should switch to it is the owner's decision (issue 94, step 3).

Hours in which the battery was full at any minute are left out of the training: with export switched off, solar is held back then, so the measured output is
below what the panels could give (the curtailment the specification says must never train the forecast down). Hours with neither forecast nor measured sun are
left out too. One attempt a day: a failure or too little history is saved as such and not retried until the next evening."""
from datetime import datetime, timedelta
import math
from pathlib import Path

from .daily_record import ADJUSTED_FORECAST_PREFIX, FORECAST_PREFIX, FULL_SOC, _put
from .energy import LONDON, day_bounds
from .daylight import LATITUDE, LONGITUDE

TRAIN_DAYS = 60                 # history looked at
MIN_DAYS, MIN_HOURS = 7, 48     # less than this is "not enough data yet"
MIN_MINUTES = 50                # of the 60 in an hour, for the hour to count
SUN_KW = 0.02                   # below this forecast and measured, the hour is night
KWP = 2.0                       # the installed panels (two 1 kWp planes)
MAX_KW = 1.1 * KWP              # no corrected hour may exceed this
ELEVATION_THRESHOLD = 10.0      # degrees: EMHASS's default for the sunrise and sunset fade
TIMEOUT_S = 180.0               # importing scikit-learn on the Pi is slow
CHILD = Path(__file__).with_name("pv_adjust_child.py")


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def training_rows(store, today, days=TRAIN_DAYS):
    """[{"t": hour midpoint, "forecast_kw", "actual_kw"}], oldest first, and the number of days that gave at least one row."""
    source = store.minute_source()
    rows, used = [], 0
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        saved = store.get_setting(FORECAST_PREFIX + day.isoformat())[1]
        if not saved or saved.get("lead") != "evening_before" or not saved.get("hourly"):
            continue
        a, b = day_bounds(day)
        pv, soc = source.usable("pv_power", a, b), source.usable("soc", a, b)
        before = len(rows)
        for hour in saved["hourly"]:
            start, end, forecast = hour.get("start"), hour.get("end"), hour.get("pv_kw")
            if not (_finite(start) and _finite(end) and _finite(forecast)) or end <= start or start < a or end > b:
                continue
            minutes = range(int(start), int(end), 60)
            values = [pv[t] for t in minutes if t in pv and _finite(pv[t])]
            if len(values) < MIN_MINUTES * (end - start) / 3600:
                continue
            if any(_finite(soc.get(t)) and soc[t] >= FULL_SOC for t in minutes):
                continue                                                            # battery full: solar may have been held back
            actual = sum(max(v, 0.0) for v in values) / len(values)
            if forecast <= SUN_KW and actual <= SUN_KW:
                continue                                                            # night
            rows.append({"t": (start + end) / 2, "forecast_kw": float(forecast), "actual_kw": actual})
        used += len(rows) > before
    return rows, used


def _checked(result, hourly):
    """The child's corrected hours, or None if they do not match the forecast they correct or fall outside what the panels can give."""
    if not isinstance(result, dict) or result.get("status") != "ok" or not isinstance(result.get("hourly"), list) or len(result["hourly"]) != len(hourly):
        return None
    for got, want in zip(result["hourly"], hourly):
        if got.get("start") != want["start"] or got.get("end") != want["end"] or not _finite(got.get("pv_kw")) or not 0.0 <= got["pv_kw"] <= MAX_KW:
            return None
    return result["hourly"]


def save_adjusted_forecast(store, now, python, runner=None):
    """Make and save tomorrow's corrected forecast, once, after 18:00. Returns what was saved, or None when there was nothing to do."""
    local = datetime.fromtimestamp(now, LONDON)
    if local.hour < 18:
        return None
    tomorrow = local.date() + timedelta(days=1)
    key = ADJUSTED_FORECAST_PREFIX + tomorrow.isoformat()
    if store.get_setting(key)[1] is not None:
        return None
    base = store.get_setting(FORECAST_PREFIX + tomorrow.isoformat())[1]
    if not base or not base.get("hourly"):
        return None
    hourly = [h for h in base["hourly"] if _finite(h.get("start")) and _finite(h.get("end")) and _finite(h.get("pv_kw"))]
    rows, days = training_rows(store, local.date())
    record = {"issued_at": now, "lead": "evening_before", "training_hours": len(rows), "training_days": days}
    if days < MIN_DAYS or len(rows) < MIN_HOURS:
        record["status"] = "not_enough_data"
    else:
        if runner is None:
            from .shadow import run_child as runner
        from .shadow import PlanningUnavailable
        job = {"latitude": LATITUDE, "longitude": LONGITUDE, "elevation_threshold": ELEVATION_THRESHOLD, "max_kw": MAX_KW,
               "train": rows, "predict": [{"start": h["start"], "end": h["end"], "pv_kw": h["pv_kw"]} for h in hourly]}
        try:
            result = runner(job, TIMEOUT_S, python, CHILD)
        except PlanningUnavailable:
            result = None
        corrected = _checked(result, hourly)
        if corrected is None:
            record["status"] = "unavailable"
        else:
            record.update(status="ok", hourly=corrected, model={k: result.get(k) for k in ("cv_rmse_kw", "alpha", "engine")})
    _put(store, key, record, now)
    return record
