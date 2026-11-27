"""The home page's day chart (owner, 19 November 2026: "make it look more like the Anker app"). One London day of the minute readings in the telemetry files,
averaged into five-minute steps, with the day's totals and the cheap-rate periods to shade. Read-only; nothing here is used by the planner or the controller.

Signs follow the readings and the Anker app: battery_power is positive when the battery gives power out (discharging) and negative when it takes power in
(charging); grid_power is positive when importing."""
from datetime import date, datetime, timedelta

from .daily_record import ADJUSTED_FORECAST_PREFIX, FORECAST_PREFIX, tariff_pricer
from .energy import LONDON, day_bounds

STEP_S = 300
KEEP_DAYS = 90                  # the telemetry files keep one-minute data this long
SERIES = {"battery_kw": "battery_power", "house_kw": "household_power", "solar_kw": "pv_power", "grid_kw": "grid_power", "soc": "soc"}
MIN_MINUTES = 3                 # of the five in a step, for the step to be drawn


def _mean(values):
    return round(sum(values) / len(values), 4) if len(values) >= MIN_MINUTES else None


def day_series(store, day, now):
    """{date, start, end, step_s, series: {name: [value or None per step]}, totals: {...}, cheap: [[start, end], ...]} for one London day."""
    today = datetime.fromtimestamp(now, LONDON).date()
    if not isinstance(day, date) or day > today or (today - day).days > KEEP_DAYS:
        raise ValueError("Choose a London date within the last 90 days")
    start, end = day_bounds(day)
    source = store.minute_source()
    minutes = {name: source.usable(metric, start, end) for name, metric in SERIES.items()}
    steps = range(int(start), int(end), STEP_S)
    series = {name: [_mean([data[t] for t in range(s, s + STEP_S, 60) if t in data]) for s in steps] for name, data in minutes.items()}
    hour = 1 / 60

    def total(name, keep):
        return round(sum(keep(v) * hour for v in minutes[name].values()), 2)
    totals = {"battery_out_kwh": total("battery_kw", lambda v: max(v, 0.0)), "battery_in_kwh": total("battery_kw", lambda v: max(-v, 0.0)),
              "house_kwh": total("house_kw", lambda v: max(v, 0.0)), "solar_kwh": total("solar_kw", lambda v: max(v, 0.0)),
              "grid_import_kwh": total("grid_kw", lambda v: max(v, 0.0)), "grid_export_kwh": total("grid_kw", lambda v: max(-v, 0.0))}
    price_at, _label = tariff_pricer(store, now, at=end)
    cheap = []
    for t in range(int(start), int(end), 1800):
        price = price_at(t + 60)
        if price is not None and price[1]:
            if cheap and cheap[-1][1] == t:
                cheap[-1][1] = t + 1800
            else:
                cheap.append([t, t + 1800])
    expected = expectations(store, start, end, now)
    return {**expected, "date": day.isoformat(), "start": start, "end": end, "step_s": STEP_S, "series": series, "totals": totals, "cheap": cheap,
            "previous": (day - timedelta(days=1)).isoformat() if (today - day).days < KEEP_DAYS else None,
            "next": (day + timedelta(days=1)).isoformat() if day < today else None}


SOC_PLAN_LOG = "soc_forecast_log"       # shadow.FORECAST_LOG_NAME: what each hour's plan expected the battery level to do, kept 36 hours


def _hourly(saved, start, end):
    """[{start, end, kw}] of a saved solar forecast within the day, or [] when none was saved."""
    rows = (saved or {}).get("hourly") or []
    return [{"start": r["start"], "end": r["end"], "kw": round(r["pv_kw"], 4)} for r in rows
            if isinstance(r.get("pv_kw"), (int, float)) and r.get("end", 0) > start and r.get("start", end) < end]


def expectations(store, start, end, now):
    """What was expected of the day, to draw dashed beside what happened: the solar forecast saved the evening before, the learned correction (GitHub
    issue 94) when one was made, and the battery level the plan expected. The plan's battery line is the plan made before the day began (the one the
    night's charge followed); for today, also the newest plan from when it was made. Plans are kept only 36 hours, so older days have no battery line."""
    from datetime import datetime as _dt
    day = _dt.fromtimestamp(start + 43200, LONDON).date().isoformat()
    forecast = store.get_setting(FORECAST_PREFIX + day)[1]
    corrected = store.get_setting(ADJUSTED_FORECAST_PREFIX + day)[1]
    entries = ((store.get_setting(SOC_PLAN_LOG)[1] or {}).get("entries")) or []
    entries = sorted((e for e in entries if isinstance(e, dict) and isinstance(e.get("issued_at"), (int, float)) and isinstance(e.get("chart"), list)),
                     key=lambda e: e["issued_at"])

    def points(entry, after):
        return [{"t": p["time"], "soc": p["soc"]} for p in entry["chart"]
                if isinstance(p, dict) and isinstance(p.get("time"), (int, float)) and isinstance(p.get("soc"), (int, float))
                and max(start, after) <= p["time"] <= end]
    before = [e for e in entries if e["issued_at"] <= start]
    planned = {"issued_at": before[-1]["issued_at"], "points": points(before[-1], start)} if before else None
    latest = None
    if entries and start <= now < end and entries[-1]["issued_at"] > start:
        latest = {"issued_at": entries[-1]["issued_at"], "points": points(entries[-1], entries[-1]["issued_at"])}
    return {"solar_forecast": _hourly(forecast, start, end), "solar_forecast_lead": (forecast or {}).get("lead"),
            "solar_corrected": _hourly(corrected, start, end) if (corrected or {}).get("status") == "ok" else [],
            "soc_planned": planned, "soc_plan_latest": latest}
