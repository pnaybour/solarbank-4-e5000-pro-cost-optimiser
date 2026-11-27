from copy import deepcopy

import pytest

from solarbank_optimiser.models import ForecastInterval, PriceInterval
from solarbank_optimiser.planning import BatteryModel, Scenario, compare_targets, replay, validate_replay


def scenario(demand, solar, probability=1):
    return Scenario(probability, tuple(ForecastInterval(i * 3600, (i + 1) * 3600, d, p) for i, (d, p) in enumerate(zip(demand, solar))))


def prices(values, cheap):
    return [PriceInterval(i * 3600, (i + 1) * 3600, p, "fixture", c) for i, (p, c) in enumerate(zip(values, cheap))]


def test_extra_cheap_charge_can_win_despite_later_curtailment():
    model = BatteryModel()
    # Demand is uncertain before sunshine arrives. The same target must be used
    # in both scenarios, rather than selecting it after the outcome is known.
    cases = [scenario([0, 2, 2, 0, 0], [0, 0, 0, 8, 8], .8),
             scenario([0, .1, .1, 0, 0], [0, 0, 0, 8, 8], .2)]
    tariff = prices([.08, .35, .35, .35, .35], [True, False, False, False, False])
    result = compare_targets(model, cases, tariff, .05, [.05, .30], .08)
    assert result["best"]["target"] == .30
    assert any(row["curtailed_kw"] > 0 for row in result["best"]["scenarios"][1]["flows"])
    assert not result["controls_enabled"]
    assert all(s["target"] == .30 for s in result["best"]["scenarios"])


def test_lower_target_cheaper_when_sun_arrives_before_load():
    model = BatteryModel()
    case = scenario([0] * 8, [0, 8, 8, 8, 8, 8, 8, 8])
    result = compare_targets(model, [case], prices([.08] + [.35] * 7, [True] + [False] * 7), .05, [.05, 1.], .08)
    assert result["best"]["target"] == .05


def test_grid_target_is_separate_from_solar_ceiling():
    model = BatteryModel()
    case = scenario([0] * 6, [0, 0, 0, 2, 2, 2])
    result = replay(model, case, prices([.08] * 3 + [.35] * 3, [True] * 3 + [False] * 3), .5, .85, .08)
    assert result["flows"][2]["after_kwh"] == pytest.approx(8.5)
    assert result["flows"][-1]["after_kwh"] == pytest.approx(10)


def test_actual_initial_soc_and_infeasible_target():
    model = BatteryModel()
    result = replay(model, scenario([0], [0]), prices([.08], [True]), .05, 1., .08)
    assert result["flows"][0]["after_kwh"] == pytest.approx(.5 + 2.5 * .86)
    high = replay(model, scenario([0], [0]), prices([.08], [True]), .95, .85, .08)
    assert high["flows"][0]["grid_charge_kw"] == 0


@pytest.mark.parametrize("probabilities", [[.8], [.7, .4], [-.1, 1.1], [float("nan")]])
def test_invalid_probability_sets(probabilities):
    with pytest.raises(ValueError):
        cases = [scenario([0], [0], p) for p in probabilities]
        compare_targets(BatteryModel(), cases, prices([.08], [True]), .5, [.5], .08)


def test_soc_and_energy_reconstruction_detects_tampering():
    model = BatteryModel()
    result = replay(model, scenario([3, 3], [0, 0]), prices([.08, .35], [True, False]), .5, .85, .08)
    for key in ("after_kwh", "import_kw", "cost", "pv_charge_kw"):
        bad = deepcopy(result)
        bad["flows"][0][key] += .1
        with pytest.raises(ValueError):
            validate_replay(model, bad, .5, .08)


def test_tariff_gaps_and_non_finite_inputs_rejected():
    with pytest.raises(ValueError):
        replay(BatteryModel(), scenario([0], [0]), [], .5, .5, .08)
    with pytest.raises(ValueError):
        ForecastInterval(0, 1800, float("nan"), 1)
    with pytest.raises(ValueError):
        Scenario(1, (ForecastInterval(0, 1800, 1, 0), ForecastInterval(1900, 3600, 1, 0)))

