"""GitHub issue 94, step 2: the learned solar correction is made beside the forecast, from uncurtailed history only, checked, saved once a day, scored beside the
uncorrected forecast and never used by the planner."""
from datetime import date, datetime, time, timedelta
import json
import subprocess
import sys

import pytest

from solarbank_optimiser import pv_adjust
from solarbank_optimiser.daily_record import ADJUSTED_FORECAST_PREFIX, FORECAST_PREFIX, _accuracy, forecast_review
from solarbank_optimiser.energy import LONDON, day_bounds
from solarbank_optimiser.shadow import PlanningUnavailable

TODAY = date(2026, 6, 20)
EVENING = datetime.combine(TODAY, time(18, 30), LONDON).timestamp()
SUNNY_HOURS = range(9, 17)                                  # London hours with sun in the synthetic days


class Source:
    def __init__(self):
        self.data = {"pv_power": {}, "soc": {}}

    def usable(self, metric, start, end):
        return {t: v for t, v in self.data[metric].items() if start <= t < end}


class Store:
    def __init__(self):
        self.settings, self.source = {}, Source()

    def get_setting(self, name, default=None):
        return self.settings.get(name, (0, default))

    def set_setting(self, name, value, now, expected_revision):
        assert self.settings.get(name, (0, None))[0] == expected_revision
        self.settings[name] = (expected_revision + 1, json.loads(json.dumps(value)))

    def minute_source(self):
        return self.source


def hour_span(day, hour):
    start = datetime.combine(day, time(hour), LONDON).timestamp()
    return start, start + 3600


def add_day(store, day, forecast_kw=1.0, actual_kw=0.7, soc=0.5, full_hour=None, lead="evening_before"):
    hourly = []
    for hour in range(24):
        start, end = hour_span(day, hour)
        sunny = hour in SUNNY_HOURS
        hourly.append({"start": start, "end": end, "pv_kw": forecast_kw if sunny else 0.0})
        for t in range(int(start), int(end), 60):
            store.source.data["pv_power"][t] = actual_kw if sunny else 0.0
            store.source.data["soc"][t] = 0.995 if hour == full_hour else soc
    store.settings[FORECAST_PREFIX + day.isoformat()] = (1, {"lead": lead, "hourly": hourly})


def history(store, days=10, **options):
    for back in range(1, days + 1):
        add_day(store, TODAY - timedelta(days=back), **options)
    add_day(store, TODAY + timedelta(days=1))                 # tomorrow's forecast, saved at 18:00


def test_training_uses_daylight_hours_and_leaves_out_hours_with_the_battery_full():
    store = Store()
    history(store, days=3, full_hour=12)
    rows, days = pv_adjust.training_rows(store, TODAY)
    assert days == 3
    assert len(rows) == 3 * (len(SUNNY_HOURS) - 1)           # night hours and the full-battery hour are out
    assert all(r["forecast_kw"] == 1.0 and abs(r["actual_kw"] - 0.7) < 1e-9 for r in rows)
    assert [r["t"] for r in rows] == sorted(r["t"] for r in rows)


def test_training_skips_thin_hours_and_forecasts_that_were_not_made_the_evening_before():
    store = Store()
    add_day(store, TODAY - timedelta(days=1), lead="same_day")
    add_day(store, TODAY - timedelta(days=2))
    start, _ = hour_span(TODAY - timedelta(days=2), 10)
    for t in range(int(start), int(start) + 20 * 60, 60):    # 20 of the hour's minutes missing
        del store.source.data["pv_power"][t]
    rows, days = pv_adjust.training_rows(store, TODAY)
    assert days == 1 and len(rows) == len(SUNNY_HOURS) - 1


def test_nothing_happens_before_18_00_or_without_tomorrows_forecast():
    store = Store()
    history(store)
    assert pv_adjust.save_adjusted_forecast(store, EVENING - 3600, "python", runner=lambda *a: pytest.fail("ran")) is None
    del store.settings[FORECAST_PREFIX + (TODAY + timedelta(days=1)).isoformat()]
    assert pv_adjust.save_adjusted_forecast(store, EVENING, "python", runner=lambda *a: pytest.fail("ran")) is None


def test_too_little_history_is_saved_as_such_and_not_retried_that_evening():
    store = Store()
    history(store, days=3)
    saved = pv_adjust.save_adjusted_forecast(store, EVENING, "python", runner=lambda *a: pytest.fail("ran"))
    assert saved["status"] == "not_enough_data" and saved["training_days"] == 3
    assert pv_adjust.save_adjusted_forecast(store, EVENING + 300, "python", runner=lambda *a: pytest.fail("ran")) is None


def corrected(job, factor=0.7):
    return {"status": "ok", "hourly": [{**r, "pv_kw": round(r["pv_kw"] * factor, 4)} for r in job["predict"]], "cv_rmse_kw": 0.05, "alpha": 0.01,
            "engine": "test"}


def test_a_checked_correction_is_saved_with_its_model_details():
    store = Store()
    history(store)
    seen = {}

    def runner(job, timeout, python, script):
        seen.update(job=job, python=python, script=script.name)
        return corrected(job)
    saved = pv_adjust.save_adjusted_forecast(store, EVENING, "/opt/solver/python", runner=runner)
    assert saved["status"] == "ok" and saved["model"]["cv_rmse_kw"] == 0.05
    assert seen["python"] == "/opt/solver/python" and seen["script"] == "pv_adjust_child.py"
    assert len(seen["job"]["train"]) == 10 * len(SUNNY_HOURS) and len(seen["job"]["predict"]) == 24
    stored = store.get_setting(ADJUSTED_FORECAST_PREFIX + (TODAY + timedelta(days=1)).isoformat())[1]
    assert stored["status"] == "ok" and max(r["pv_kw"] for r in stored["hourly"]) == 0.7


@pytest.mark.parametrize("bad", [
    lambda job: {"status": "failed"},
    lambda job: {**corrected(job), "hourly": corrected(job)["hourly"][:-1]},                                       # an hour missing
    lambda job: {**corrected(job), "hourly": [{**r, "pv_kw": -0.1} for r in corrected(job)["hourly"]]},             # negative
    lambda job: {**corrected(job), "hourly": [{**r, "pv_kw": 9.0} for r in corrected(job)["hourly"]]},              # beyond the panels
    lambda job: {**corrected(job), "hourly": [{**r, "start": r["start"] + 60} for r in corrected(job)["hourly"]]},  # moved
])
def test_a_correction_that_does_not_check_out_is_saved_as_unavailable(bad):
    store = Store()
    history(store)
    saved = pv_adjust.save_adjusted_forecast(store, EVENING, "python", runner=lambda job, *a: bad(job))
    assert saved["status"] == "unavailable" and "hourly" not in saved


def test_a_failed_child_process_is_saved_as_unavailable():
    store = Store()
    history(store)

    def runner(*args):
        raise PlanningUnavailable("Planner process failed or exceeded its deadline")
    assert pv_adjust.save_adjusted_forecast(store, EVENING, "python", runner=runner)["status"] == "unavailable"


def test_the_daily_record_scores_the_correction_beside_the_forecast_only_when_it_was_made():
    day = TODAY
    a, b = day_bounds(day)
    flat = {"lead": "evening_before", "hourly": [{"start": a + h * 3600, "end": a + (h + 1) * 3600, "pv_kw": 0.5} for h in range(24)]}
    adjusted = {**flat, "status": "ok", "hourly": [{**r, "pv_kw": 0.25} for r in flat["hourly"]]}
    review = forecast_review(day, flat, None, 6.0, None, adjusted)
    assert review["solar"]["forecast_kwh"] == 12.0 and review["solar_adjusted"]["forecast_kwh"] == 6.0 and review["solar_adjusted"]["error_kwh"] == 0
    assert "solar_adjusted" not in forecast_review(day, flat, None, 6.0, None, {"status": "not_enough_data"})
    accuracy = _accuracy([{"forecast_review": review}])
    assert accuracy["solar_adjusted"]["days"] == 1 and accuracy["solar_adjusted"]["bias_kwh"] == 0
    assert _accuracy([{"forecast_review": {"solar": None, "demand": None}}])["solar_adjusted"]["days"] == 0


def test_the_correction_is_never_given_to_the_planner():
    """Step 2 of issue 94 is comparison only: nothing outside the daily record (scoring) and the home page's day chart (display, day_view.py) reads the
    corrected forecast; in particular nothing the planner or the controller runs."""
    from pathlib import Path
    package = Path(pv_adjust.__file__).parent
    readers = sorted(p.name for p in package.rglob("*.py") if "adjusted_forecast_for_day" in p.read_text() or "ADJUSTED_FORECAST_PREFIX" in p.read_text())
    assert readers == ["daily_record.py", "day_view.py", "pv_adjust.py"]


# ---- the child itself (needs scikit-learn and pvlib, which the solver environment has; skipped where they are missing) --------------------------------
def child_job(factor=0.7, days=14):
    train, predict = [], []
    for back in range(days, 0, -1):
        for hour in SUNNY_HOURS:
            start, end = hour_span(TODAY - timedelta(days=back), hour)
            train.append({"t": (start + end) / 2, "forecast_kw": 1.0 + 0.05 * (hour % 3), "actual_kw": factor * (1.0 + 0.05 * (hour % 3))})
    for hour in range(24):
        start, end = hour_span(TODAY + timedelta(days=1), hour)
        predict.append({"start": start, "end": end, "pv_kw": 1.0 if hour in SUNNY_HOURS else 0.0})
    return {"latitude": 51.5074, "longitude": -0.1278, "elevation_threshold": 10.0, "max_kw": 2.2, "train": train, "predict": predict}


def test_the_child_learns_a_systematic_over_forecast_and_keeps_night_at_zero():
    pytest.importorskip("sklearn")
    pytest.importorskip("pvlib")
    from solarbank_optimiser.pv_adjust_child import fit_predict
    result = fit_predict(child_job())
    by_hour = {datetime.fromtimestamp(r["start"], LONDON).hour: r["pv_kw"] for r in result["hourly"]}
    assert result["status"] == "ok" and result["training_hours"] == 14 * len(SUNNY_HOURS)
    assert all(by_hour[h] == 0.0 for h in (0, 1, 2, 3, 22, 23))                 # the sun is down
    assert all(0.55 <= by_hour[h] <= 0.85 for h in range(11, 15))              # about 70 % of the 1 kW forecast, learned from the history
    assert all(0.0 <= v <= 2.2 for v in by_hour.values())


def solver_python():
    """The Python the child really runs under: the Pi's solver environment (so the deploy's test gate runs this), or one named in SOLARBANK_SOLVER_PYTHON;
    else this interpreter if it happens to have scikit-learn."""
    import os
    from pathlib import Path
    for candidate in (os.environ.get("SOLARBANK_SOLVER_PYTHON"), "/opt/solarbank-cost-optimiser/solver-venv/bin/python", sys.executable):
        if candidate and Path(candidate).exists() and subprocess.run([candidate, "-c", "import sklearn, pvlib"], capture_output=True, timeout=120).returncode == 0:
            return candidate
    pytest.skip("no Python with scikit-learn and pvlib (the solver environment)")


def test_the_child_runs_as_a_process_in_the_solver_environment_and_reports_failure_without_a_traceback():
    python, script = solver_python(), pv_adjust.CHILD
    ok = subprocess.run([python, str(script)], input=json.dumps(child_job()), capture_output=True, text=True, timeout=300)
    assert ok.returncode == 0, ok.stdout
    result = json.loads(ok.stdout)
    by_hour = {datetime.fromtimestamp(r["start"], LONDON).hour: r["pv_kw"] for r in result["hourly"]}
    assert result["status"] == "ok" and all(0.55 <= by_hour[h] <= 0.85 for h in range(11, 15)) and by_hour[0] == 0.0
    bad = subprocess.run([python, str(script)], input='{"train": []}', capture_output=True, text=True, timeout=300)
    assert bad.returncode == 2 and json.loads(bad.stdout) == {"status": "failed"}
