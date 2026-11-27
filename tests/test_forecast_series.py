"""The forecast worker's half-hour series of expected household use and solar output (services.forecast_series). The daily record keeps the one made
the evening before to compare with what happened. (The "Next 48 hours" chart that also drew it was removed on 20 November 2026.)"""
from solarbank_optimiser.services import forecast_series


def test_forecast_series_gives_half_hour_means_of_each_source():
    demand = {"intervals": [{"start": 1800 * 100 + 60 * k, "end": 1800 * 100 + 60 * (k + 1), "demand_kw": 1.0 if k < 15 else 3.0} for k in range(30)]}
    pv = [{"start": 1800 * 100, "end": 1800 * 102, "pv_kw": 2.0}]
    series = forecast_series(demand, pv, 1800 * 100, 1800 * 102)
    assert series == [{"time": 180000, "demand_kw": 2.0, "pv_kw": 2.0}, {"time": 181800, "demand_kw": None, "pv_kw": 2.0}]


def test_forecast_series_copes_with_either_source_missing():
    assert forecast_series(None, None, 0, 3600) == []
    pv = [{"start": 0, "end": 3600, "pv_kw": 1.5}]
    assert [p["demand_kw"] for p in forecast_series(None, pv, 0, 3600)] == [None, None]
