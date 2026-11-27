"""Issues #31 and #32 (2 November 2026): a failed planning attempt must not cancel a charging window, and a weather outage must not stop planning."""
import json

import pytest

from solarbank_optimiser.control_logic import PLAN_HOLD_S, PlanMemory, read_plan
from solarbank_optimiser.models import FakeClock, encoded
from solarbank_optimiser.providers import ProviderError, first_missing, solar_forecast
from solarbank_optimiser.services import WEATHER_MAX_AGE_S, WEATHER_RETRY_S, ProviderConfig, ProviderWorker
from solarbank_optimiser.shadow import ShadowWorker, checked_window_open, publish_controller_plan
from solarbank_optimiser.store import Store

from test_phase3 import NOW, put, weather_fixture

WINDOW_START, WINDOW_END = 1_000_000.0, 1_000_000.0 + 6 * 3600


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "controller.sqlite3")
    s.initialise()
    return s


def checked_plan(issued, target=0.75):
    return {"mode": "shadow", "action": "charge_to", "target_soc": target, "issued_at": issued, "valid_until": issued + 300,
            "source": "shadow_plan", "issuance_id": "x", "min_soc": 0.1,
            "window": {"start": WINDOW_START, "end": WINDOW_END, "target_soc": target}}


# ----- #31: failures leave the last checked plan alone -----------------------------------------------------------------------------
def test_a_failed_attempt_inside_the_window_keeps_the_last_checked_plan(tmp_path, store):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(checked_plan(WINDOW_START + 600)))
    before = path.read_text()
    publish_controller_plan(path, store, None, WINDOW_START + 700)
    assert path.read_text() == before                       # not replaced by a 'normal' document


def test_without_an_open_checked_window_a_failure_still_writes_normal(tmp_path, store):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(checked_plan(WINDOW_START)))
    publish_controller_plan(path, store, None, WINDOW_END + 1)                  # the window has ended
    assert json.loads(path.read_text())["source"] == "no_checked_plan"
    path.write_text(json.dumps({"mode": "anker_manual", "action": "hold", "issued_at": 1.0, "valid_until": 2.0, "source": "handover_request"}))
    publish_controller_plan(path, store, None, WINDOW_START)                    # ending a hand-over still works
    assert json.loads(path.read_text())["source"] == "no_checked_plan"
    (tmp_path / "missing.json").unlink(missing_ok=True)
    publish_controller_plan(tmp_path / "missing.json", store, None, WINDOW_START)
    assert json.loads((tmp_path / "missing.json").read_text())["action"] == "normal"


def test_checked_window_open_rejects_damaged_files(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert not checked_window_open(bad, WINDOW_START)
    assert not checked_window_open(tmp_path / "absent.json", WINDOW_START)


def test_the_controller_carries_the_window_through_a_planner_failure(tmp_path, store):
    """The whole chain: a checked plan, then the planner fails; the plan expires; plan memory keeps charging inside the window."""
    path = tmp_path / "plan.json"
    issued = WINDOW_START + 600
    publish_controller_plan(path, store, None, issued)                          # no file yet: a 'normal' document
    path.write_text(json.dumps(checked_plan(issued)))
    memory = PlanMemory()
    plan, note = memory.resolve(*read_plan(path, issued + 10), issued + 10)
    assert plan["action"] == "charge_to"
    publish_controller_plan(path, store, None, issued + 400)                    # planning fails: nothing is written
    later = issued + 900                                                        # the checked plan has expired (valid 300 s)
    plan, note = memory.resolve(*read_plan(path, later), later)
    assert plan is not None and plan["action"] == "charge_to" and plan.get("held") and "planner silent" in note
    assert later < issued + 300 + PLAN_HOLD_S


def test_planner_failures_are_logged_once_per_reason(store, capsys):
    worker = ShadowWorker(store, clock=FakeClock(NOW))
    worker._log_failure("A current weather-based solar forecast is needed", NOW)
    worker._log_failure("A current weather-based solar forecast is needed", NOW + 60)
    worker._log_failure("Something else", NOW + 120)
    worker._log_failure("Something else", NOW + 120 + 1800)
    lines = [l for l in capsys.readouterr().err.splitlines() if l.startswith("Planner: no plan published")]
    assert len(lines) == 3


# ----- #32: weather outages ----------------------------------------------------------------------------------------------------
def test_a_missing_hour_late_in_the_forecast_shortens_it_instead_of_rejecting_it():
    data = weather_fixture()
    data["hourly"]["shortwave_radiation"][90] = None                            # day 4: beyond the 48-hour horizon
    result = solar_forecast(encoded(data).encode(), NOW)
    assert result["intervals"][-1]["end"] >= NOW + 48 * 3600
    assert result["intervals"][-1]["end"] <= data["hourly"]["time"][89]          # nothing at or after the missing hour


def test_a_missing_hour_early_in_the_forecast_is_still_refused():
    data = weather_fixture()
    data["hourly"]["temperature_2m"][20] = None
    with pytest.raises(ProviderError, match="weather_horizon_incomplete"):
        solar_forecast(encoded(data).encode(), NOW)


def test_first_missing():
    assert first_missing([1, 2.0, 3]) == 3
    assert first_missing([1, None, 3]) == 1
    assert first_missing([1, float("nan")]) == 1
    assert first_missing([True, 2]) == 0


def test_weather_failures_retry_within_minutes_and_keep_their_reason(store):
    put(store, "providers", ProviderConfig(weather_enabled=True).model_dump())
    clock = FakeClock(NOW)
    calls = []

    def transport(host, path, *args):
        calls.append(path)
        raise ValueError("shortwave_radiation must be a finite number")
    worker = ProviderWorker(store, clock=clock, transport=transport)
    worker.once()
    health = store.get_setting("provider_weather")[1]
    assert health["status"] == "failed"
    assert health["reason"].startswith("invalid_response:ValueError") and "shortwave_radiation" in health["reason"]
    assert health["next_retry"] == NOW + WEATHER_RETRY_S
    first = len(calls)
    clock.advance(WEATHER_RETRY_S + 1)
    worker.once()
    assert len(calls) > first                                                   # retried after 10 minutes, not an hour


def test_a_stale_but_usable_weather_forecast_is_accepted_by_the_age_rule():
    assert 6 * 3600 < WEATHER_MAX_AGE_S <= 20 * 3600
