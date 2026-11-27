from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import seed_minutes
from solarbank_optimiser.car_history import car_history
from solarbank_optimiser.charger import ChargerTracker
from solarbank_optimiser.collector import Collector, CollectorConfig
from solarbank_optimiser.models import FakeClock, Observation
from solarbank_optimiser.settings import london_input
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "controller.sqlite3")
    result.initialise()
    return result


def sample(t, watts, acquired=None):
    return Observation("synthetic:dedicated-charger", "ev_power", t if acquired is None else acquired,
                       watts / 1000, "kW", source_at=t if acquired is not None else None)


def test_midnight_sessions_daily_totals_and_25_hour_day(store):
    tracker = ChargerTracker(store, 0, 2, 60)
    start = london_input("2026-10-24T23:45")
    for t in range(int(start), int(start + 1800) + 1, 60):
        tracker.observe(sample(t, 2000))
    seed_minutes(store, "ev_power", start, start + 1800, 2.0)
    history = car_history(store, date(2026, 10, 24), date(2026, 10, 25))
    assert len(history["sessions"]) == 1
    assert history["charging_kwh"] == pytest.approx(1)
    assert history["sessions"][0]["energy_kwh"] == pytest.approx(1)
    assert [d["charging_kwh"] for d in history["daily"]] == pytest.approx([.5, .5])
    assert [d["covered_seconds"] for d in history["daily"]] == [900, 900]
    assert history["daily"][1]["expected_seconds"] == 90000
    assert history["partial"]


def test_standby_has_covered_zero_but_no_data_is_unknown(store):
    day = date(2026, 10, 24)
    assert car_history(store, day, day)["charging_kwh"] is None
    tracker = ChargerTracker(store, 0, 2, 60)
    start = london_input("2026-10-24T12:00")
    for t in range(int(start), int(start + 1800) + 1, 60):
        tracker.observe(sample(t, 7))
    seed_minutes(store, "ev_power", start, start + 1800, 0.007)
    result = car_history(store, day, day)
    assert result["sessions"] == []
    assert result["charging_kwh"] == 0
    assert result["daily"][0]["active_seconds"] == 0
    assert result["daily"][0]["covered_seconds"] == 1800
    assert result["partial"]


def test_upstream_times_determine_energy_not_network_fetch_times(store):
    tracker = ChargerTracker(store, 0, 2, 60)
    for t in range(100, 1901, 60):
        tracker.observe(sample(t, 2000, acquired=t + (t % 7) / 10))
    assert tracker.sessions()[0]["energy_kwh"] == pytest.approx(1)
    assert tracker.sessions()[0]["active_seconds"] == 1800


def test_history_api_csv_and_login_agree(store):
    tracker = ChargerTracker(store, 0, 2, 60)
    start = london_input("2026-10-24T12:00")
    for t in range(int(start), int(start + 1800) + 1, 60):
        tracker.observe(sample(t, 2000))
    seed_minutes(store, "ev_power", start, start + 1800, 2.0)
    password = "test-only-password-12345"
    client = TestClient(create_app(WebConfig(store.path, password, status_cache_seconds=0), FakeClock(start + 1801)))
    client.auth = ("solarbank", password)
    query = "?start=2026-10-24&end=2026-10-24"
    assert client.get("/api/car-history" + query, auth=None).status_code == 401
    data = client.get("/api/car-history" + query).json()
    assert data["charging_kwh"] == pytest.approx(1)
    import csv, io
    rows = list(csv.DictReader(io.StringIO(client.get("/car-charging.csv" + query).text)))
    assert float(rows[0]["charging_kwh"]) == data["daily"][0]["charging_kwh"]
    assert float(rows[0]["covered_seconds"]) == 1800
    assert rows[0]["partial"] == "True"
    for path in ("/api/car-history", "/car-charging.csv", "/api/history"):
        assert client.get(path + "?start=9999-12-31&end=9999-12-31").status_code == 422

