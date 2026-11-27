"""The plan as an hourly time series, in the same four quantities the Anker app's strategy chart shows, so the two can be compared side by side.

For each London hour of the horizon (energy in kWh over the hour, from the checked central scenario): solar produced, battery charging (positive) or discharging (negative), the
house's use, energy bought from the grid, and the battery level at the end of the hour; plus the purchase price in force. Nothing here sends a command; it only presents the
plan that the planner already checked.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from .energy import LONDON

HEADER = ("hour_start_utc", "hour_start_london", "solar_kwh", "battery_kwh", "home_kwh", "grid_import_kwh", "grid_export_kwh", "soc_end", "price_gbp_per_kwh")


def hourly(flows, prices, capacity_kwh):
    """Aggregate the plan's flow intervals into whole-hour rows. Partial hours at the ends of the horizon are kept as they are (they say how many seconds they cover)."""
    rows = {}
    for row, price in zip(flows, prices):
        hour = int(row["start"]) // 3600 * 3600
        h = (row["end"] - row["start"]) / 3600
        entry = rows.setdefault(hour, {"hour": hour, "seconds": 0.0, "solar": 0.0, "charge": 0.0, "discharge": 0.0, "home": 0.0, "import": 0.0,
                                       "soc_end": None, "cost": 0.0, "kwh_priced": 0.0})
        entry["seconds"] += row["end"] - row["start"]
        entry["solar"] += row["pv_kw"] * h
        entry["charge"] += (row["pv_charge_kw"] + row["grid_charge_kw"]) * h
        entry["discharge"] += row["discharge_kw"] * h
        entry["home"] += row["demand_kw"] * h
        entry["import"] += row["import_kw"] * h
        entry["soc_end"] = row["after_kwh"] / capacity_kwh
        entry["cost"] += price["price"] * row["import_kw"] * h
        entry["kwh_priced"] += row["import_kw"] * h
    out = []
    for hour in sorted(rows):
        e = rows[hour]
        local = datetime.fromtimestamp(hour, LONDON)
        out.append({"hour_start": hour, "london": local.strftime("%Y-%m-%d %H:%M"), "seconds": round(e["seconds"]),
                    "solar_kwh": round(e["solar"], 3), "battery_kwh": round(e["charge"] - e["discharge"], 3), "home_kwh": round(e["home"], 3),
                    "grid_import_kwh": round(e["import"], 3), "grid_export_kwh": 0.0, "soc_end": round(e["soc_end"], 3),
                    "price_gbp_per_kwh": round(e["cost"] / e["kwh_priced"], 4) if e["kwh_priced"] > 1e-9 else round(price_at(prices, hour), 4)})
    return out


def price_at(prices, hour):
    for p in prices:
        if p["start"] <= hour < p["end"]:
            return p["price"]
    return prices[-1]["price"] if prices else 0.0


def to_csv(series):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADER)
    for r in series:
        writer.writerow([r["hour_start"], r["london"], r["solar_kwh"], r["battery_kwh"], r["home_kwh"], r["grid_import_kwh"], r["grid_export_kwh"], r["soc_end"], r["price_gbp_per_kwh"]])
    return out.getvalue()
