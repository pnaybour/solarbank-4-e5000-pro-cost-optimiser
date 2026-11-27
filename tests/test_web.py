
import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock, Observation
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app

PASSWORD = "test-only-password-12345"


@pytest.fixture
def client(tmp_path):
    config = WebConfig(tmp_path / "web.sqlite3", PASSWORD, status_cache_seconds=0)
    result = TestClient(create_app(config, FakeClock(1792839600)))
    result.auth = ("solarbank", PASSWORD)
    return result


def headers(client):
    return {"Origin": "http://testserver", "X-CSRF-Token": client.get("/api/status").json()["csrf_token"]}


def test_dashboard_requires_auth_and_health_does_not(client):
    assert client.get("/health", auth=None).json()["control_ready"] is False
    assert client.get("/", auth=None).status_code == 401
    assert client.get("/api/status", auth=None).status_code == 401
    assert client.get("/api/status", auth=("solarbank", "wrong")).status_code == 401
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="home-hero"' in page.text
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]


def test_status_never_reports_ready_or_leaks_credentials(client):
    result = client.get("/api/status")
    assert PASSWORD not in result.text
    assert not result.json()["equipment_writes"]
    assert not result.json()["control_ready"]
    assert all(item["value"] is None for item in result.json()["readings"].values())


def test_settings_origin_csrf_validation_and_persistence(client):
    body = {"revision": 0, "selection": {"mode": "High demand", "start": 0., "high_extra_kwh": 4.}}
    assert client.put("/api/demand", json=body).status_code == 403
    assert client.put("/api/demand", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.put("/api/demand", json=body, headers={"Origin": "http://testserver"}).status_code == 403
    good = headers(client)
    assert client.put("/api/demand", json=body, headers=good).status_code == 200
    assert client.get("/api/status").json()["active_demand"] == "High demand"
    assert client.put("/api/demand", json=body, headers=good).status_code == 409


def test_bad_demand_settings_rejected(client):
    for selection in ({"mode": "Away from home", "start": 0.}, {"mode": "invalid", "start": 0.},
                      {"mode": "Normal consumption", "start": 100., "end": 99.},
                      {"mode": "Normal consumption", "start": 0., "api_key": "not-accepted"}):
        assert client.put("/api/demand", json={"revision": 0, "selection": selection}, headers=headers(client)).status_code == 422


def test_saved_future_visit_and_expiry(client):
    body = {"revision": 0, "selection": {"mode": "High demand", "start": 1900000000., "end": 1900001000., "high_extra_kwh": 4.}}
    assert client.put("/api/demand", json=body, headers=headers(client)).status_code == 200
    assert client.get("/api/status").json()["active_demand"] == "Normal consumption"


def test_tariff_no_missing_zero_price_or_extra_slots(client):
    assert client.get("/api/status").json()["price_gbp_per_kwh"] is None
    body = {"revision": 0, "tariff": {"day_pence": 30., "cheap_pence": 8., "valid_from": 0.}}
    assert client.put("/api/tariff", json=body, headers=headers(client)).status_code == 200
    status = client.get("/api/status").json()
    assert status["price_gbp_per_kwh"] == .30
    assert "Unavailable" in status["extra_slots"]
    assert "Fallback" in status["tariff_status"]


def test_handover_is_pending_automatic_is_blocked(client):
    good = headers(client)
    assert client.put("/api/mode", json={"revision": 0, "mode": "automatic"}, headers=good).status_code == 409
    assert client.put("/api/mode", json={"revision": 0, "mode": "anker_manual"}, headers=good).status_code == 409
    assert client.put("/api/mode", json={"revision": 0, "mode": "handover_pending"}, headers=good).status_code == 200
    status = client.get("/api/status").json()
    assert status["mode"] == "handover_pending"
    assert not status["control_ready"]


def test_history_date_range_validation_and_csv(client):
    assert client.get("/api/history?start=2026-10-17&end=2026-10-10").status_code == 422
    assert client.get("/api/history?start=invalid&end=2026-10-10").status_code == 422
    response = client.get("/history.csv?start=2026-10-10&end=2026-10-17")
    assert response.status_code == 200
    assert "coverage_seconds" in response.text
    assert "text/csv" in response.headers["Content-Type"]


def test_live_measurement_does_not_turn_equipment_ready(tmp_path):
    db = tmp_path / "live.sqlite3"
    client = TestClient(create_app(WebConfig(db, PASSWORD, status_cache_seconds=0), FakeClock(100)))
    client.auth = ("solarbank", PASSWORD)
    Store(db).add_observations([Observation("fixture", "soc", 99, .5, "fraction")])
    state = client.get("/api/status").json()
    assert state["readings"]["soc"]["value"] == .5
    assert not state["control_ready"]


@pytest.mark.parametrize("watts", [-2500, 0, 2500])
def test_garage_feed_uses_signed_sub_ct_power_without_inventing_current(tmp_path, watts):
    from solarbank_optimiser.collector import decode_snapshot

    clock = FakeClock(101)
    database = tmp_path / "garage.sqlite3"
    client = TestClient(create_app(WebConfig(database, PASSWORD, status_cache_seconds=0), clock))
    client.auth = ("solarbank", PASSWORD)
    payload = {"ok": True, "sample": {"time": 100, "main": 500, "sub": watts},
               "solarbank": {"ok": True, "sample": {"time": 100, "load": 800}}}
    Store(database).add_observations(decode_snapshot(payload, 101))

    state = client.get("/api/status").json()
    garage = state["readings"]["garage_power"]
    assert garage["value"] == watts / 1000
    assert garage["unit"] == "kW"
    assert garage["source_at"] == 100
    assert state["readings"]["garage_current"]["value"] is None
    assert state["readings"]["household_power"]["value"] == .8
    assert state["equipment_writes"] is state["control_ready"] is False
    home = client.get("/").text
    assert 'id="home-grid"' in home

    clock.advance(5)
    stale = client.get("/api/status").json()["readings"]["garage_power"]
    assert stale["value"] is None
    assert stale["status"] == "unavailable"
    assert stale["last_good_value"] == watts / 1000


def test_missing_garage_feed_stays_unavailable(tmp_path):
    from solarbank_optimiser.collector import decode_snapshot

    database = tmp_path / "garage.sqlite3"
    client = TestClient(create_app(WebConfig(database, PASSWORD, status_cache_seconds=0), FakeClock(101)))
    client.auth = ("solarbank", PASSWORD)
    assert client.get("/api/status").json()["readings"]["garage_power"]["value"] is None
    Store(database).add_observations(decode_snapshot({"ok": False}, 101))
    readings = client.get("/api/status").json()["readings"]
    assert readings["garage_power"]["value"] is None
    assert readings["garage_power"]["status"] == "unavailable"
    assert readings["garage_current"]["value"] is None


def test_weak_password_rejected(tmp_path):
    with pytest.raises(ValueError):
        WebConfig(tmp_path / "db", "short", status_cache_seconds=0)



def test_dashboard_settings_and_history_are_separate_authenticated_pages(client):
    home = client.get("/").text
    settings = client.get("/settings")
    history = client.get("/history")
    assert settings.status_code == history.status_code == 200
    assert 'id="home-soc"' in home and 'id="home-tonight-main"' in home
    for form in ('tariff-form', 'providers-form'):
        assert f'id="{form}"' not in home
        assert f'id="{form}"' in settings.text
    assert 'id="demand-form"' not in home and 'id="demand-form"' not in settings.text      # household demand is three buttons on the dashboard now
    assert 'id="key-form"' not in settings.text
    assert 'id="car-history-form"' in history.text
    assert 'id="car-history-form"' not in home
    assert 'href="/settings"' in home and 'href="/history"' in home
    assert client.get("/settings", auth=None).status_code == 401
    assert client.get("/history", auth=None).status_code == 401


@pytest.mark.parametrize('scheme,peer,allowed', [
    ('http', '192.168.1.30', False),
    ('http', '100.72.1.20', False),
    ('https', '192.168.1.30', True),
    ('http', '127.0.0.1', True),
    ('http', '::1', True),
])
def test_key_entry_page_and_endpoint_share_transport_policy(tmp_path, scheme, peer, allowed):
    from solarbank_optimiser.services import SecretFile

    secret_file = SecretFile(tmp_path / '.env')
    existing_key = 'existing-fixture-api-key'
    replacement_key = 'replacement-fixture-api-key'
    secret_file.write(existing_key)
    config = WebConfig(tmp_path / 'web.sqlite3', PASSWORD, trusted_network=True, status_cache_seconds=0)
    origin = f'{scheme}://solarbank-pi.local'
    with TestClient(create_app(config), base_url=origin, client=(peer, 1234)) as client:
        page = client.get('/settings')
        assert page.status_code == 200
        assert ('id="key-form"' in page.text) is allowed
        assert ('type="password"' in page.text) is allowed
        assert ('id="key-entry-guidance"' in page.text) is not allowed
        if not allowed:
            assert 'saved Octopus key continues to work' in page.text
            assert 'SSH' in page.text and 'private .env file' in page.text
        state = client.get('/api/status').json()
        assert state['api_key_configured'] is True
        response = client.put('/api/octopus-key', json={'api_key': replacement_key},
                              headers={'Origin': origin, 'X-CSRF-Token': state['csrf_token']})
        assert response.status_code == (200 if allowed else 403)
        assert secret_file.read() == (replacement_key if allowed else existing_key)
        for result in (page, response, client.get('/api/status')):
            assert existing_key not in result.text
            assert replacement_key not in result.text


def test_recorded_default_rates_are_labelled_unconfirmed(client):
    body = {"revision": 0, "tariff": {"name": "Intelligent Octopus Go", "source": "Account export: 13 October 2026",
            "confirmed": False, "day_pence": 27.4894, "cheap_pence": 6.5714, "valid_from": 0.}}
    assert client.put("/api/tariff", json=body, headers=headers(client)).status_code == 200
    status = client.get("/api/status").json()
    assert status["tariff"]["day_pence"] == 27.4894
    assert not status["tariff"]["confirmed"]
    assert "confirmation" in status["tariff_status"]
    assert status["price_gbp_per_kwh"] == pytest.approx(.274894)
