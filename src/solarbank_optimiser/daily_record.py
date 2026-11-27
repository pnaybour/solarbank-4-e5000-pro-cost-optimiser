"""Daily records: the savings figure (A7) and the lost-solar measure (A5), computed once per finished London day.

Everything here is an ESTIMATE and says so. Sources are the one-minute log rows that already exist (battery level, battery power, grid, solar, household load) and the
user's confirmed fallback tariff (the real Octopus bill takes precedence when it can be read; that is parked). Nothing here can change what the Anker or the controller do.

SAVINGS (A7): for each minute, the household load priced at that minute's tariff is what a home with NEITHER solar NOR battery would have paid. What was actually paid is the
grid import priced the same way. The difference is the saving. Only days with (almost) complete minute coverage count; partial days are shown but never added up.

LOST SOLAR (A5): while the battery is full and the sun is up, extra solar has nowhere to go (export is off), so the panels are throttled. That cannot be measured directly,
because the measured output is already throttled. This module therefore reports separately: how long the battery was full in daylight, how many of those minutes show the
throttled pattern (solar tracking the household load), and, only when the evening-before forecast of that day was saved, an upper-bound estimate of the solar that was available but
not produced. Forecast error alone never proves curtailment (specification 6.2), so every figure is labelled a candidate. The "a lower overnight charge would have saved" figure is the
part of that solar that could have been stored and used (limited by what the battery actually supplied afterwards), priced at the night rate.
"""

from datetime import datetime, timedelta
import json
import math
import time

from .tariff_window import effective_tariff
from .energy import LONDON, day_bounds
from .forecasts import MAX_FILLED_MINUTES, _runs, MAX_FILLED_RUN
from .settings import FallbackTariff

VERSION = 4                  # 4 (11 November 2026): the car charger recorded beside the house, for checking against Octopus's own figures
METRICS = ("soc", "battery_power", "grid_power", "pv_power", "household_power")
CAR = "ev_power"             # recorded, never required: a minute without it still counts for every other figure
MIN_OK_SAMPLES = 30          # a minute needs at least this many good one-second samples behind it
FULL_SOC = 0.99
IDLE_KW = 0.05
SUN_KW = 0.02                # solar above this means the sun is up
TRACKING_KW = 0.15           # solar within this of the load looks throttled to the load
PV_CHARGE_EFFICIENCY = GRID_CHARGE_EFFICIENCY = 0.86      # provisional, the planner's own model values
SETTLE_SECONDS = 300         # score a day only this long after it ends, so its last minutes are in
RECORD_PREFIX, FORECAST_PREFIX, START_SETTING = "daily_record:", "forecast_for_day:", "savings_start"
DEMAND_FORECAST_PREFIX = "demand_forecast_for_day:"
ADJUSTED_FORECAST_PREFIX = "adjusted_forecast_for_day:"      # the learned correction, made beside the forecast and not used (GitHub issue 94)
HALF_HOUR_PREFIX = "half_hours:"


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---- reading the minute rows ------------------------------------------------------------------------------------------------------------------
def minute_rows(store, start, end):
    """{metric: {minute_start: mean}} for the minutes in [start, end) from the telemetry files (TC2). Minutes with too few valid seconds are left out."""
    source = store.minute_source()
    return {metric: source.usable(metric, start, end) for metric in (*METRICS, CAR)}


# ---- the arithmetic (pure) --------------------------------------------------------------------------------------------------------------------
def _hourly_lookup(forecast):
    """minute -> forecast solar kW, from the saved evening-before forecast; None when no forecast was saved."""
    if not forecast or not forecast.get("hourly"):
        return None
    spans = [(r["start"], r["end"], r["pv_kw"]) for r in forecast["hourly"] if _finite(r.get("pv_kw")) and r["end"] > r["start"]]
    return lambda t: next((kw for a, b, kw in spans if a <= t < b), None)


def _error(forecast_kwh, measured_kwh):
    """Forecast minus measured, in kWh and as a percentage of what was measured (None when nothing was measured)."""
    return {"forecast_kwh": forecast_kwh, "measured_kwh": measured_kwh, "error_kwh": forecast_kwh - measured_kwh,
            "error_pct": (forecast_kwh - measured_kwh) / measured_kwh * 100 if measured_kwh > 0.05 else None}


def forecast_review(day, solar_forecast, demand_forecast, solar_kwh, consumption_kwh, adjusted_forecast=None):
    """Yesterday's forecasts against what was measured, as daily kWh totals: {"solar": {...} or None, "demand": {...} or None}.
    Solar comes from the saved hourly forecast, demand from the saved half-hour forecast; each is used only when it covered nearly the whole day.
    `consumption_kwh` is the house without the car (the planner forecasts that, GitHub issue 74), or None when the car reading was too thin to take it out."""
    a, b = day_bounds(day)
    out = {"solar": None, "demand": None}

    def solar(forecast):
        if not forecast or not forecast.get("hourly"):
            return None
        spans = [(max(a, r["start"]), min(b, r["end"]), r["pv_kw"]) for r in forecast["hourly"] if _finite(r.get("pv_kw")) and r["end"] > a and r["start"] < b]
        if sum(y - x for x, y, _ in spans) < 20 * 3600:
            return None
        return {**_error(sum(kw * (y - x) / 3600 for x, y, kw in spans), solar_kwh), "lead": forecast.get("lead")}

    out["solar"] = solar(solar_forecast)
    if adjusted_forecast and adjusted_forecast.get("status") == "ok":
        out["solar_adjusted"] = solar(adjusted_forecast)
    if demand_forecast and demand_forecast.get("half_hours") and consumption_kwh is not None:
        slots = [r for r in demand_forecast["half_hours"] if _finite(r.get("kw")) and a <= r["t"] < b]
        if len(slots) * 1800 >= 20 * 3600:
            out["demand"] = {**_error(sum(r["kw"] * 0.5 for r in slots), consumption_kwh), "lead": demand_forecast.get("lead")}
    return out


def compute_day(day, minutes, price_at, forecast=None, now=0.0, demand_forecast=None, adjusted_forecast=None):
    start, end = day_bounds(day)
    keys = list(range(int(start), int(end), 60))
    valid = [t for t in keys if all(t in minutes[m] for m in METRICS)]
    missing = [t for t in keys if t not in set(valid)]
    complete = len(missing) <= MAX_FILLED_MINUTES and (not missing or _runs(missing) <= MAX_FILLED_RUN)
    hour = 1 / 60
    total = {name: 0.0 for name in ("consumption", "imported", "exported", "solar", "batt_in", "batt_out", "baseline_cost", "actual_cost", "cheap_import",
                                    "car", "car_cheap", "house_without_car")}
    car_minutes = minutes.get(CAR, {})
    car_missing = 0
    for t in keys:                                            # the car on its own, from every minute it was read (Octopus's figures count every minute too)
        car = car_minutes.get(t)
        if car is None:
            car_missing += 1
            continue
        total["car"] += max(car, 0.0) * hour
        price = price_at(t + 30)
        if price is not None and price[1]:
            total["car_cheap"] += max(car, 0.0) * hour
    car_free_minutes = 0
    priced = True
    lookup = _hourly_lookup(forecast)
    solar = {"full_minutes": 0, "full_sun_minutes": 0, "throttled_minutes": 0, "pv_kwh_while_full": 0.0, "load_kwh_while_full": 0.0,
             "possible_lost_kwh": 0.0, "evidenced_lost_kwh": 0.0}
    first_full, batt_out_after_full = None, 0.0
    for t in valid:
        soc, battery, grid = minutes["soc"][t], minutes["battery_power"][t], minutes["grid_power"][t]
        pv, house = max(0.0, minutes["pv_power"][t]), max(0.0, minutes["household_power"][t])
        imported, exported = max(grid, 0.0), max(-grid, 0.0)
        total["consumption"] += house * hour; total["imported"] += imported * hour; total["exported"] += exported * hour
        total["solar"] += pv * hour
        total["batt_out"] += max(battery, 0.0) * hour; total["batt_in"] += max(-battery, 0.0) * hour
        if t in car_minutes:
            total["house_without_car"] += max(house - max(car_minutes[t], 0.0), 0.0) * hour
            car_free_minutes += 1
        price = price_at(t + 30)
        if price is None:
            priced = False
        else:
            total["baseline_cost"] += house * hour * price[0]; total["actual_cost"] += imported * hour * price[0]
            if price[1]:
                total["cheap_import"] += imported * hour
        if soc >= FULL_SOC:
            solar["full_minutes"] += 1
            if first_full is None:
                first_full = t
            potential = lookup(t) if lookup else None
            if pv > SUN_KW or (potential or 0) > 0.05:
                solar["full_sun_minutes"] += 1
                solar["pv_kwh_while_full"] += pv * hour; solar["load_kwh_while_full"] += house * hour
                throttled = abs(battery) < IDLE_KW and imported < IDLE_KW and abs(pv - house) <= TRACKING_KW
                solar["throttled_minutes"] += throttled
                if potential is not None:
                    lost = max(0.0, potential - pv) * hour
                    solar["possible_lost_kwh"] += lost
                    if throttled:
                        solar["evidenced_lost_kwh"] += lost
        if first_full is not None and t >= first_full:
            batt_out_after_full += max(battery, 0.0) * hour
    record = {
        "version": VERSION, "date": day.isoformat(), "computed_at": now, "complete": bool(complete), "minutes_expected": len(keys),
        "minutes_covered": len(valid), "estimated": True,
        "consumption_kwh": total["consumption"], "import_kwh": total["imported"], "export_kwh": total["exported"], "solar_kwh": total["solar"],
        "battery_charged_kwh": total["batt_in"], "battery_discharged_kwh": total["batt_out"],
        "baseline_cost_gbp": total["baseline_cost"] if priced else None, "actual_cost_gbp": total["actual_cost"] if priced else None,
        "saving_gbp": (total["baseline_cost"] - total["actual_cost"]) if priced else None,
        "cheap_rate_import_kwh": total["cheap_import"] if priced else None,
        # The car (Shelly channel 1), kept apart so a day can be checked against Octopus's charge history. `household_power` includes the car on this
        # installation, so consumption_kwh is the whole house, car included; house_without_car_kwh counts only the minutes that also had a car reading.
        "car_kwh": total["car"], "car_cheap_rate_kwh": total["car_cheap"] if priced else None, "car_minutes_missing": car_missing,
        "house_without_car_kwh": total["house_without_car"], "house_without_car_minutes": car_free_minutes,
    }
    solar_out = {"label": "Candidate only: not proven curtailment (forecast error alone cannot prove it)",
                 "first_full_at": first_full, "full_minutes": solar["full_minutes"], "full_sun_minutes": solar["full_sun_minutes"],
                 "throttled_minutes": solar["throttled_minutes"], "pv_kwh_while_full": solar["pv_kwh_while_full"],
                 "load_kwh_while_full": solar["load_kwh_while_full"],
                 "forecast_saved": lookup is not None, "forecast_issued_at": (forecast or {}).get("issued_at"),
                 "forecast_lead": (forecast or {}).get("lead"),
                 "possible_lost_kwh": solar["possible_lost_kwh"] if lookup else None,
                 "evidenced_lost_kwh": solar["evidenced_lost_kwh"] if lookup else None}
    cheap = next((price_at(t + 30) for t in keys if (price_at(t + 30) or (None, False))[1]), None)
    if lookup and cheap is not None:
        storable = min(solar["evidenced_lost_kwh"] * PV_CHARGE_EFFICIENCY, batt_out_after_full)
        solar_out["storable_kwh"] = storable
        solar_out["lower_target_saving_gbp"] = storable / GRID_CHARGE_EFFICIENCY * cheap[0]
    else:
        solar_out["storable_kwh"] = solar_out["lower_target_saving_gbp"] = None
    record["solar"] = solar_out
    car_free = total["house_without_car"] if len(valid) - car_free_minutes <= MAX_FILLED_MINUTES else None      # too many minutes without a car reading: no demand review
    record["forecast_review"] = (forecast_review(day, forecast, demand_forecast, total["solar"], car_free, adjusted_forecast) if complete
                                 else {"solar": None, "demand": None})
    return _rounded(record)


def half_hours(day, minutes, price_at):
    """The long-term record: one entry per half hour of the London day, aligned to the :00 and :30 settlement periods Octopus bills in
    (so it can be set beside the smart meter's own half-hourly figures). 46, 48 or 50 entries on a clock-change day. Energy is summed only from minutes
    that have all their data; `covered` says how many of the 30 minutes that was."""
    start, end = day_bounds(day)
    hour = 1 / 60
    slots = []
    for t0 in range(int(start), int(end), 1800):
        acc = dict.fromkeys(("used", "imported", "exported", "solar", "batt_in", "batt_out", "baseline", "actual", "car", "house_without_car"), 0.0)
        covered = car_covered = 0
        price = price_at(t0 + 900)
        car_minutes = minutes.get(CAR, {})
        for t in range(t0, min(t0 + 1800, int(end)), 60):
            car = car_minutes.get(t)
            if car is not None:
                car_covered += 1
                acc["car"] += max(car, 0.0) * hour
            if not all(t in minutes[m] for m in METRICS):
                continue
            covered += 1
            grid, battery = minutes["grid_power"][t], minutes["battery_power"][t]
            house, pv = max(0.0, minutes["household_power"][t]), max(0.0, minutes["pv_power"][t])
            acc["used"] += house * hour; acc["imported"] += max(grid, 0.0) * hour; acc["exported"] += max(-grid, 0.0) * hour; acc["solar"] += pv * hour
            acc["batt_in"] += max(-battery, 0.0) * hour; acc["batt_out"] += max(battery, 0.0) * hour
            if car is not None:
                acc["house_without_car"] += max(house - max(car, 0.0), 0.0) * hour
            if price is not None:
                acc["baseline"] += house * hour * price[0]; acc["actual"] += max(grid, 0.0) * hour * price[0]
        slots.append(_rounded({
            "t": t0, "london": datetime.fromtimestamp(t0, LONDON).strftime("%H:%M"), "minutes_covered": covered,
            "consumption_kwh": acc["used"], "import_kwh": acc["imported"], "export_kwh": acc["exported"], "solar_kwh": acc["solar"],
            "battery_charged_kwh": acc["batt_in"], "battery_discharged_kwh": acc["batt_out"],
            "price_gbp_per_kwh": price[0] if price else None, "cheap_rate": price[1] if price else None,
            "baseline_cost_gbp": acc["baseline"] if price else None, "actual_cost_gbp": acc["actual"] if price else None,
            "car_kwh": acc["car"], "car_minutes_covered": car_covered, "house_without_car_kwh": acc["house_without_car"]}))
    return slots


def _rounded(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return value


# ---- storing ----------------------------------------------------------------------------------------------------------------------------------
def _put(store, name, value, now):
    revision, _ = store.get_setting(name)
    store.set_setting(name, value, now, revision)


def tariff_pricer(store, now=None, at=None):
    """price_at(epoch) -> (GBP per kWh, is_cheap) or None, from the confirmed fallback tariff; plus a label of what was used.
    With `at` (the end of a day being scored) the tariff version and cheap window in force at that moment are used, not today's (GitHub 44, TC6); a day older than
    every saved version falls back to the current one and says so."""
    revision, raw = store.get_setting("fallback_tariff")
    providers = store.get_setting("providers", {})[1]
    late = ""
    if at is not None and hasattr(store, "setting_at"):
        then_revision, then_raw = store.setting_at("fallback_tariff", at)
        if then_raw is not None:
            revision, raw, providers = then_revision, then_raw, store.setting_at("providers", at, {})[1] or providers
        else:
            late = " (saved after this day: no earlier version existed)"
    from .services import window_cache          # imported here: services imports modules that import this one
    raw, _ = effective_tariff(raw, window_cache(store, time.time() if now is None else now), (providers or {}).get("product"))
    if not raw:
        return (lambda t: None), "no tariff saved"
    try:
        tariff = FallbackTariff(**raw)
    except ValueError:
        return (lambda t: None), "tariff saved but invalid"
    if not tariff.confirmed:
        return (lambda t: None), "tariff not confirmed"

    def price_at(t):
        try:
            return tariff.at(t)
        except ValueError:
            return None
    return price_at, f"your fallback tariff (revision {revision}): day {tariff.day_pence:g}p, cheap {tariff.cheap_pence:g}p per kWh{late}"


def snapshot_forecasts(store, now):
    """Save, once and never overwritten, the solar forecast the system had for a day: tomorrow's from 18:00 the evening before, and today's if nothing was saved."""
    _, weather = store.get_setting("cache_weather", {})
    intervals = [r for r in (weather or {}).get("intervals", []) if _finite(r.get("pv_kw")) and _finite(r.get("start")) and _finite(r.get("end"))]
    if not intervals:
        return []
    local = datetime.fromtimestamp(now, LONDON)
    wanted = [(local.date(), "same_day")] + ([(local.date() + timedelta(days=1), "evening_before")] if local.hour >= 18 else [])
    saved = []
    for day, lead in wanted:
        if store.get_setting(FORECAST_PREFIX + day.isoformat())[1] is not None:
            continue
        a, b = day_bounds(day)
        hourly = [{"start": r["start"], "end": r["end"], "pv_kw": r["pv_kw"]} for r in intervals if r["end"] > a and r["start"] < b]
        if sum(min(r["end"], b) - max(r["start"], a) for r in hourly) < 20 * 3600:        # needs nearly the whole day
            continue
        _put(store, FORECAST_PREFIX + day.isoformat(), {"issued_at": now, "weather_acquired_at": weather.get("acquired_at"), "lead": lead, "hourly": hourly}, now)
        saved.append(day)
    return saved


def snapshot_demand_forecast(store, now):
    """The household-demand forecast for a day, saved once like the solar one (tomorrow's from 18:00, today's if nothing was saved), as half-hour kW."""
    _, summary_ = store.get_setting("forecast_summary", {})
    series = [r for r in (summary_ or {}).get("series", []) if _finite(r.get("time")) and _finite(r.get("demand_kw"))]
    if not series:
        return []
    local = datetime.fromtimestamp(now, LONDON)
    wanted = [(local.date(), "same_day")] + ([(local.date() + timedelta(days=1), "evening_before")] if local.hour >= 18 else [])
    saved = []
    for day, lead in wanted:
        if store.get_setting(DEMAND_FORECAST_PREFIX + day.isoformat())[1] is not None:
            continue
        a, b = day_bounds(day)
        slots = [{"t": r["time"], "kw": r["demand_kw"]} for r in series if a <= r["time"] < b]
        if len(slots) * 1800 < 20 * 3600:
            continue
        _put(store, DEMAND_FORECAST_PREFIX + day.isoformat(), {"issued_at": now, "lead": lead, "half_hours": slots}, now)
        saved.append(day)
    return saved


def record_finished_days(store, now, lookback=3):
    """Score every finished day in the last few that has no record (or an older version). Returns the dates computed."""
    computed = []
    today = datetime.fromtimestamp(now, LONDON).date()
    for back in range(lookback, 0, -1):
        day = today - timedelta(days=back)
        start, end = day_bounds(day)
        if now < end + SETTLE_SECONDS:
            continue
        price_at, label = tariff_pricer(store, now, at=end)               # the tariff in force at the end of THIS day, whatever has been saved since (GitHub 44)
        existing = store.get_setting(RECORD_PREFIX + day.isoformat())[1]
        if existing and existing.get("version") == VERSION and existing.get("complete"):
            continue                                          # a complete record is final
        minutes = minute_rows(store, start, end)
        record = compute_day(day, minutes, price_at, store.get_setting(FORECAST_PREFIX + day.isoformat())[1], now,
                             store.get_setting(DEMAND_FORECAST_PREFIX + day.isoformat())[1],
                             store.get_setting(ADJUSTED_FORECAST_PREFIX + day.isoformat())[1])
        if existing and existing.get("version") == VERSION and (record.get("minutes_covered") or 0) <= (existing.get("minutes_covered") or 0):
            continue                                          # incomplete, and no new data has arrived since it was scored (#21): late readings repair it, nothing else rewrites it
        record["price_source"] = label
        # The half-hour record goes in FIRST: the retention job only ever purges a day's minute detail once this exists.
        _put(store, HALF_HOUR_PREFIX + day.isoformat(), {"version": VERSION, "date": day.isoformat(), "computed_at": now, "price_source": label,
                                                          "slots": half_hours(day, minutes, price_at)}, now)
        _put(store, RECORD_PREFIX + day.isoformat(), record, now)
        computed.append(day)
    return computed


def set_savings_start(store, day, now):
    """The day the optimiser goes live: savings are counted from here. None clears it."""
    _put(store, START_SETTING, {"date": day.isoformat() if day else None}, now)


# ---- summaries --------------------------------------------------------------------------------------------------------------------------------
def _accuracy(records):
    """Over the days in view: how far the saved forecasts were from what was measured, for solar and for household use. Bias is forecast minus measured
    (positive means the forecast was too high); the absolute figure ignores the sign."""
    out = {}
    for name in ("solar", "demand", "solar_adjusted"):
        rows = [r["forecast_review"][name] for r in records if (r.get("forecast_review") or {}).get(name)]
        pct = [r["error_pct"] for r in rows if r.get("error_pct") is not None]
        out[name] = {"days": len(rows), "bias_kwh": round(sum(r["error_kwh"] for r in rows) / len(rows), 2) if rows else None,
                     "mean_error_pct": round(sum(pct) / len(pct), 1) if pct else None,
                     "mean_abs_error_pct": round(sum(abs(p) for p in pct) / len(pct), 1) if pct else None}
    return out


def all_records(store):
    """Every saved daily record, newest first (the lifetime totals need them all, not only the days on show)."""
    from contextlib import closing as _closing
    out = []
    with _closing(store.connect()) as db:
        for _name, payload in db.execute("SELECT name,payload FROM settings WHERE name LIKE ? ORDER BY name DESC", (RECORD_PREFIX + "%",)):
            try:
                record = json.loads(payload)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("date"):
                out.append(record)
    return out


def summary(store, today, days=14):
    records = []
    for back in range(1, days + 1):
        record = store.get_setting(RECORD_PREFIX + (today - timedelta(days=back)).isoformat())[1]
        if record and record.get("minutes_covered"):          # a day with no data at all is on file (so it is not rescanned) but not shown
            records.append(record)
    start_text = (store.get_setting(START_SETTING)[1] or {}).get("date")
    scored = [r for r in records if r.get("complete") and r.get("saving_gbp") is not None]
    week = [r for r in scored if r["date"] >= (today - timedelta(days=7)).isoformat()]
    lifetime = [r for r in all_records(store) if r.get("complete") and r.get("saving_gbp") is not None and r["date"] < today.isoformat()]          # every saved day, not just the 14 shown (#25)
    counted = [r for r in lifetime if start_text and r["date"] >= start_text]
    last = records[0] if records else None
    return {
        "forecast_accuracy": _accuracy([r for r in records if r.get("complete")]),
        "estimated": True, "counting_from": start_text,
        "last_7_days": {"saving_gbp": round(sum(r["saving_gbp"] for r in week), 2), "days_included": len(week), "days_possible": 7,
                        "baseline_cost_gbp": round(sum(r["baseline_cost_gbp"] for r in week), 2), "actual_cost_gbp": round(sum(r["actual_cost_gbp"] for r in week), 2)},
        "cumulative_counted": {"saving_gbp": round(sum(r["saving_gbp"] for r in counted), 2), "days": len(counted)} if start_text else None,
        "since_records_began": {"saving_gbp": round(sum(r["saving_gbp"] for r in lifetime), 2), "days": len(lifetime),
                                "note": "For information: includes days before the optimiser went live, when the Anker app alone was in charge."},
        "last_day": ({"date": last["date"], "complete": last["complete"], "saving_gbp": last.get("saving_gbp"),
                      "import_kwh": last["import_kwh"], "consumption_kwh": last["consumption_kwh"], "solar": last["solar"],
                      "forecast_review": last.get("forecast_review") or {"solar": None, "demand": None}} if last else None),
        "days": [{"date": r["date"], "complete": r["complete"], "saving_gbp": r.get("saving_gbp"), "baseline_cost_gbp": r.get("baseline_cost_gbp"),
                  "actual_cost_gbp": r.get("actual_cost_gbp"), "consumption_kwh": r["consumption_kwh"], "import_kwh": r["import_kwh"],
                  "solar_kwh": r["solar_kwh"], "first_full_at": r["solar"]["first_full_at"],
                  "full_sun_minutes": r["solar"]["full_sun_minutes"], "throttled_minutes": r["solar"]["throttled_minutes"],
                  "evidenced_lost_kwh": r["solar"]["evidenced_lost_kwh"], "possible_lost_kwh": r["solar"]["possible_lost_kwh"],
                  "lower_target_saving_gbp": r["solar"]["lower_target_saving_gbp"], "forecast_saved": r["solar"]["forecast_saved"],
                  "forecast_review": r.get("forecast_review") or {"solar": None, "demand": None}} for r in records],
    }
