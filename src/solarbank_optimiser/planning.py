"""Deterministic target-policy evaluation for offline fixtures only.

This is deliberately separate from native EMHASS dispatch. Its idealised flow
model must be characterised against the actual device before it can represent
an executable installed policy. Nothing in this module can publish commands.
"""

from dataclasses import asdict, dataclass
import math

from .models import ForecastInterval, fingerprint, number


@dataclass(frozen=True)
class BatteryModel:
    capacity_kwh: float = 10
    reserve: float = .05
    ac_limit_kw: float = 2.5
    pv_charge_limit_kw: float = 2
    grid_charge_efficiency: float = .86
    pv_charge_efficiency: float = .86
    discharge_efficiency: float = .90
    version: str = "provisional-offline-v1"

    def __post_init__(self):
        for name in ("capacity_kwh", "ac_limit_kw", "pv_charge_limit_kw"):
            if number(getattr(self, name), name, 0) == 0:
                raise ValueError("Capacity and power limits must be positive")
        number(self.reserve, "reserve", .05, 1)
        for name in ("grid_charge_efficiency", "pv_charge_efficiency", "discharge_efficiency"):
            if number(getattr(self, name), name, 0, 1) == 0:
                raise ValueError("Efficiencies must be positive")


@dataclass(frozen=True)
class Scenario:
    probability: float
    intervals: tuple[ForecastInterval, ...]

    def __post_init__(self):
        number(self.probability, "probability", 0, 1)
        if not self.intervals:
            raise ValueError("Scenario needs intervals")
        if any(a.end != b.start for a, b in zip(self.intervals, self.intervals[1:])):
            raise ValueError("Forecast coverage must be continuous")


def window_targets(prices, target):
    """The grid-charge target in force for each interval. `target` is one number (every cheap window) or a sequence with one number per cheap window,
    in time order (the last repeats if there are more windows than numbers). Between windows the NEXT window's target is shown."""
    if isinstance(target, (int, float)):
        return [target] * len(prices)
    targets = list(target)
    if not targets:
        raise ValueError("No target")
    out, window, previous_cheap = [], 0, False
    for price in prices:
        if previous_cheap and not price.cheap:
            window += 1
        previous_cheap = price.cheap
        out.append(targets[min(window, len(targets) - 1)])
    return out


def replay(model, scenario, prices, initial_soc, target, terminal_price, charge_policy=None):
    """One target per cheap opportunity (a single number applies to all), across all scenarios.

    PV is supplied at an AC-equivalent modelling boundary. Simultaneous flows
    here are provisional; their actual firmware semantics remain unverified.
    """
    number(initial_soc, "initial SOC", model.reserve, 1)
    for single in ([target] if isinstance(target, (int, float)) else target):
        number(single, "target", model.reserve, 1)
    number(terminal_price, "next cheap replenishment price")
    if len(prices) != len(scenario.intervals):
        raise ValueError("Price and forecast intervals must match")
    per_interval = window_targets(prices, target)
    stored = initial_soc * model.capacity_kwh
    reserve = model.reserve * model.capacity_kwh
    output = []
    for forecast, price, interval_target in zip(scenario.intervals, prices, per_interval):
        if (forecast.start, forecast.end) != (price.start, price.end):
            raise ValueError("Split forecasts and prices at every tariff boundary")
        hours = (forecast.end - forecast.start) / 3600
        effective_target = interval_target
        if charge_policy and charge_policy.get("kind") == "full_overnight":
            for night in charge_policy["windows"]:
                for window in night["intervals"]:
                    if any(forecast.start < boundary < forecast.end for boundary in (window["start"], window["end"])):
                        raise ValueError("Split forecasts at overnight policy boundaries")
                    if window["start"] <= forecast.start and forecast.end <= window["end"]:
                        effective_target = 1.
        before = stored
        direct = min(forecast.demand_kw, forecast.pv_kw, model.ac_limit_kw)
        surplus = forecast.pv_kw - direct
        pv_charge = min(surplus, model.pv_charge_limit_kw,
                        max(0, model.capacity_kwh - stored) / (model.pv_charge_efficiency * hours))
        stored += pv_charge * hours * model.pv_charge_efficiency
        discharge = grid_charge = 0.
        if price.cheap:
            # Shared AC interface: imported grid charging and PV AC output share
            # a conservative throughput budget until the device is characterised.
            grid_charge = min(max(0, model.ac_limit_kw - direct),
                              max(0, effective_target * model.capacity_kwh - stored) / (model.grid_charge_efficiency * hours),
                              max(0, model.capacity_kwh - stored) / (model.grid_charge_efficiency * hours))
            stored += grid_charge * hours * model.grid_charge_efficiency
        elif pv_charge == 0:
            discharge = min(max(0, forecast.demand_kw - direct), max(0, model.ac_limit_kw - direct),
                            max(0, stored - reserve) * model.discharge_efficiency / hours)
            stored -= discharge * hours / model.discharge_efficiency
        imported = max(0, forecast.demand_kw - direct - discharge) + grid_charge
        output.append({"start": forecast.start, "end": forecast.end, "before_kwh": before, "after_kwh": stored,
                       "grid_target": effective_target, "demand_kw": forecast.demand_kw, "pv_kw": forecast.pv_kw, "direct_kw": direct,
                       "pv_charge_kw": pv_charge, "grid_charge_kw": grid_charge, "discharge_kw": discharge,
                       "curtailed_kw": max(0, surplus - pv_charge), "import_kw": imported,
                       "price": price.price, "cost": imported * hours * price.price})
    import_cost = math.fsum(row["cost"] for row in output)
    inventory = max(0, stored - reserve) * terminal_price / model.grid_charge_efficiency
    result = {"flows": output, "import_cost_gbp": import_cost, "terminal_inventory_gbp": inventory,
              "objective_gbp": import_cost - inventory, "target": target}
    validate_replay(model, result, initial_soc, terminal_price, forecasts=scenario.intervals, prices=prices, target=target, charge_policy=charge_policy)
    return result


def validate_replay(model, result, initial_soc, terminal_price, tolerance=1e-8, *, forecasts=None, prices=None, target=None, charge_policy=None):
    """Reconstruct SOC and reconcile both physical boundaries independently."""
    stored = initial_soc * model.capacity_kwh
    if forecasts is not None and (len(result["flows"]) != len(forecasts) or len(prices) != len(forecasts)):
        raise ValueError("Published flow coverage differs from inputs")
    previous_end = None
    per_interval = window_targets(prices, target) if prices is not None and target is not None else None
    for index, row in enumerate(result["flows"]):
        if previous_end is not None and row["start"] != previous_end:
            raise ValueError("Discontinuous flow times")
        previous_end = row["end"]
        if forecasts is not None:
            f, p = forecasts[index], prices[index]
            expected = {"start": f.start, "end": f.end, "demand_kw": f.demand_kw, "pv_kw": f.pv_kw, "price": p.price}
            if any(abs(row[k]-value) > tolerance for k,value in expected.items()):
                raise ValueError("Published flow differs from immutable input")
            requested = per_interval[index]
            if charge_policy and charge_policy.get("kind") == "full_overnight":
                for night in charge_policy["windows"]:
                    for window in night["intervals"]:
                        if any(f.start < edge < f.end for edge in (window["start"],window["end"])):
                            raise ValueError("Unsplit policy boundary")
                        if window["start"] <= f.start and f.end <= window["end"]:
                            requested = 1.
            if abs(row["grid_target"]-requested) > tolerance:
                raise ValueError("Grid target differs from common policy")
            if not p.cheap and row["grid_charge_kw"] > tolerance:
                raise ValueError("Grid charging outside a confirmed cheap interval")
        h = (row["end"] - row["start"]) / 3600
        if h <= 0:
            raise ValueError("Invalid duration")
        for key, value in row.items():
            number(value, key)
        for key in ("direct_kw", "pv_charge_kw", "grid_charge_kw", "discharge_kw", "curtailed_kw", "import_kw"):
            if row[key] < -tolerance:
                raise ValueError("Negative flow or intentional export")
        if row["discharge_kw"] > tolerance and row["grid_charge_kw"] + row["pv_charge_kw"] > tolerance:
            raise ValueError("Simultaneous charge and discharge")
        if row["direct_kw"] + row["discharge_kw"] + row["grid_charge_kw"] > model.ac_limit_kw + tolerance:
            raise ValueError("Shared AC interface exceeded")
        if (row["direct_kw"] > min(row["demand_kw"],row["pv_kw"],model.ac_limit_kw)+tolerance
                or row["pv_charge_kw"] > max(0,row["pv_kw"]-row["direct_kw"])+tolerance
                or row["discharge_kw"] > max(0,row["demand_kw"]-row["direct_kw"])+tolerance):
            raise ValueError("Flow exceeds available demand or solar")
        if row["grid_charge_kw"] > tolerance:
            after_solar = stored + row["pv_charge_kw"] * h * model.pv_charge_efficiency
            if row["grid_charge_kw"]*h*model.grid_charge_efficiency > max(0,row["grid_target"]*model.capacity_kwh-after_solar)+tolerance:
                raise ValueError("Grid charge exceeds requested target")
        if row["pv_charge_kw"] > model.pv_charge_limit_kw + tolerance:
            raise ValueError("PV charge path exceeded")
        solar = row["direct_kw"] + row["pv_charge_kw"] + row["curtailed_kw"]
        demand = row["direct_kw"] + row["discharge_kw"] + row["import_kw"] - row["grid_charge_kw"]
        if abs(solar - row["pv_kw"]) > tolerance or abs(demand - row["demand_kw"]) > tolerance:
            raise ValueError("Power balance does not reconcile")
        if abs(stored - row["before_kwh"]) > tolerance:
            raise ValueError("Discontinuous stored energy")
        stored += h * (row["pv_charge_kw"] * model.pv_charge_efficiency
                       + row["grid_charge_kw"] * model.grid_charge_efficiency
                       - row["discharge_kw"] / model.discharge_efficiency)
        if not model.reserve * model.capacity_kwh - tolerance <= stored <= model.capacity_kwh + tolerance:
            raise ValueError("SOC reserve/ceiling exceeded")
        if abs(stored - row["after_kwh"]) > tolerance:
            raise ValueError("SOC reconstruction failed")
        if abs(row["cost"] - row["import_kw"] * h * row["price"]) > tolerance:
            raise ValueError("Interval cost failed reconciliation")
    imported = math.fsum(row["cost"] for row in result["flows"])
    inventory = max(0, stored - model.reserve * model.capacity_kwh) * terminal_price / model.grid_charge_efficiency
    if (abs(imported - result["import_cost_gbp"]) > tolerance
            or abs(inventory - result["terminal_inventory_gbp"]) > tolerance
            or abs(imported - inventory - result["objective_gbp"]) > tolerance):
        raise ValueError("Objective reconciliation failed")


def compare_targets(model, scenarios, prices, initial_soc, targets, terminal_price, charge_policy=None):
    if not scenarios or not math.isclose(math.fsum(s.probability for s in scenarios), 1, rel_tol=0, abs_tol=1e-9):
        raise ValueError("Scenario probabilities must sum to one")
    if not targets:
        raise ValueError("No candidate target")
    candidates = []
    for target in targets:
        results = [replay(model, scenario, prices, initial_soc, target, terminal_price, charge_policy) for scenario in scenarios]
        expected = math.fsum(s.probability * r["objective_gbp"] for s, r in zip(scenarios, results))
        candidates.append({"target": target, "expected_cost_gbp": expected, "scenarios": results})
    # Compare actual monetary values; no hidden cost tolerance disguising a tie.
    best = min(candidates, key=lambda candidate: (candidate["expected_cost_gbp"], candidate["target"]))
    inputs = {"model": asdict(model), "scenarios": [asdict(s) for s in scenarios], "prices": [asdict(p) for p in prices],
              "initial_soc": initial_soc, "targets": targets, "terminal_price": terminal_price, "charge_policy": charge_policy}
    return {"status": "offline_analysis", "controls_enabled": False, "executable_on_device": False,
            "inputs_fingerprint": fingerprint(inputs), "inputs": inputs, "best": best, "candidates": candidates,
            "limitations": "Provisional flow model; EMHASS adapter, device constraints, EV/current forecasts and commissioning pending"}

