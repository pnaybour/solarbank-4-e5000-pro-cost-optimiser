"""A3: the planner chooses a separate charge level for each cheap window (tonight and tomorrow night), not one level for all."""
import math

import pytest

from solarbank_optimiser.models import ForecastInterval, PriceInterval
from solarbank_optimiser.planning import BatteryModel, Scenario, replay, validate_replay, window_targets
from solarbank_optimiser.shadow_solver import cheap_windows, evaluate, feasible, refine

T0 = 1_793_000_000 // 3600 * 3600
GRID = [round(i / 100, 2) for i in range(10, 101, 5)]


def day_pair(sunny_first=True):
    """48 hourly intervals. Cheap 00:00-06:00 and 24:00-30:00. Day one is sunny (solar refills the battery) and day two is dark with a steady load."""
    prices, forecasts = [], []
    for hour in range(48):
        cheap = hour % 24 < 6
        prices.append(PriceInterval(T0 + hour * 3600, T0 + (hour + 1) * 3600, 0.07 if cheap else 0.28, "fixture", cheap))
        sun = 3.0 if (sunny_first and 8 <= hour < 16) else 0.0
        forecasts.append(ForecastInterval(T0 + hour * 3600, T0 + (hour + 1) * 3600, 0.6 if hour < 24 else 1.2, sun))
    return prices, forecasts


def job_for(prices):
    return {"initial_soc": 0.3, "terminal_price": 0.07, "targets": GRID, "charge_policy": {"kind": "none", "windows": []},
            "horizon_end": prices[-1].end}


def test_window_targets_gives_each_cheap_window_its_own_number():
    prices, _ = day_pair()
    assert cheap_windows(prices) == 2
    per = window_targets(prices, [0.4, 0.9])
    assert per[0] == 0.4 and per[5] == 0.4 and per[24] == 0.9 and per[29] == 0.9 and per[10] == 0.9      # between windows: the next window's level
    assert window_targets(prices, 0.6) == [0.6] * 48
    assert window_targets(prices, [0.5]) == [0.5] * 48
    with pytest.raises(ValueError):
        window_targets(prices, [])


def test_separate_levels_beat_the_best_single_level_and_are_valid_flows():
    model = BatteryModel(reserve=0.10)
    prices, forecasts = day_pair()
    cases = [Scenario(1.0, tuple(forecasts))]
    job = job_for(prices)
    constant = min(((evaluate(model, cases, prices, job, t)[0], t) for t in GRID if evaluate(model, cases, prices, job, t)[1]))
    targets, evaluations = refine(model, cases, prices, job, constant[1])
    cost, valid, plans = evaluate(model, cases, prices, job, targets)
    assert valid and len(targets) == 2 and targets[0] < targets[1]
    assert cost < constant[0] - 0.01                                                                  # a real saving, not rounding
    assert evaluations < 400
    validate_replay(model, plans[0], job["initial_soc"], job["terminal_price"], forecasts=forecasts, prices=prices, target=targets)
    assert feasible(plans[0], prices, model.capacity_kwh, job["horizon_end"], job["charge_policy"])


def test_no_single_window_change_improves_the_result():
    model = BatteryModel(reserve=0.10)
    prices, forecasts = day_pair()
    cases = [Scenario(1.0, tuple(forecasts))]
    job = job_for(prices)
    start = min((evaluate(model, cases, prices, job, t)[0], t) for t in GRID if evaluate(model, cases, prices, job, t)[1])[1]
    targets, _ = refine(model, cases, prices, job, start)
    best = evaluate(model, cases, prices, job, targets)[0]
    for index in range(2):
        for other in GRID:
            cost, valid, _ = evaluate(model, cases, prices, job, targets[:index] + [other] + targets[index + 1:])
            assert not valid or cost >= best - 1e-9


def test_one_cheap_window_stays_one_number():
    model = BatteryModel(reserve=0.10)
    prices, forecasts = day_pair()
    prices, forecasts = prices[:24], forecasts[:24]
    cases = [Scenario(1.0, tuple(forecasts))]
    targets, count = refine(model, cases, prices, job_for(prices), 0.7)
    assert targets == [0.7] and count == 0


def test_two_scenarios_are_weighed_together():
    model = BatteryModel(reserve=0.10)
    prices, sunny = day_pair()
    _, dull = day_pair(sunny_first=False)
    cases = [Scenario(0.5, tuple(sunny)), Scenario(0.5, tuple(dull))]
    job = job_for(prices)
    start = min((evaluate(model, cases, prices, job, t)[0], t) for t in GRID if evaluate(model, cases, prices, job, t)[1])[1]
    targets, _ = refine(model, cases, prices, job, start)
    cost = evaluate(model, cases, prices, job, targets)[0]
    assert math.isfinite(cost) and cost <= evaluate(model, cases, prices, job, start)[0] + 1e-12
    assert targets[0] >= start - 0.5 and all(t in GRID for t in targets)                              # dull days push the first level up relative to the sunny-only case
