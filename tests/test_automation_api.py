"""A9: pause, override and return-to-automatic through the website's API, plus the new page controls."""
import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.settings import london_input
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app

NOW = london_input("2026-10-29T12:00")


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "a.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "a.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), FakeClock(NOW), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as c:
        c.store = store
        c.headers.update({"Origin": "http://solarbank-pi.local", "X-CSRF-Token": c.get("/api/status").json()["csrf_token"]})
        yield c


def put(client, **body):
    return client.put("/api/automation", json=body)


def test_pause_until_a_time_then_resume(client):
    reply = put(client, action="pause", until_local="2026-10-30T09:00")
    assert reply.status_code == 200 and reply.json()["automation"]["paused"] is True and reply.json()["equipment_writes"] is False
    status = client.get("/api/status").json()["automation"]
    assert status["paused"] is True and status["paused_until"] == london_input("2026-10-30T09:00") and status["override"] is None
    reply = put(client, action="resume")
    assert reply.status_code == 200 and client.get("/api/status").json()["automation"] == {"paused": False, "paused_until": None, "override": None}


def test_pause_with_no_end_lasts_until_resumed(client):
    put(client, action="pause")
    assert client.get("/api/status").json()["automation"]["paused"] is True


def test_override_sets_a_level_until_a_time_and_resume_clears_it(client):
    reply = put(client, action="override", target_percent=80, until_local="2026-10-30T06:00")
    assert reply.status_code == 200
    assert client.store.get_setting("manual_override")[1]["target"] == 0.8
    assert client.get("/api/status").json()["automation"]["override"] == {"target": 0.8, "valid_until": london_input("2026-10-30T06:00")}
    put(client, action="resume")
    assert client.get("/api/status").json()["automation"]["override"] is None


@pytest.mark.parametrize("body,code", [
    ({"action": "override", "target_percent": 80}, 422),                                                  # no end time
    ({"action": "override", "until_local": "2026-10-30T06:00"}, 422),                                     # no level
    ({"action": "override", "target_percent": 8, "until_local": "2026-10-30T06:00"}, 422),                # below the 10 % minimum
    ({"action": "override", "target_percent": 150, "until_local": "2026-10-30T06:00"}, 422),
    ({"action": "override", "target_percent": 80, "until_local": "2026-10-29T11:00"}, 422),               # in the past
    ({"action": "override", "target_percent": 80, "until_local": "2026-12-30T11:00"}, 422),               # more than 14 days ahead
    ({"action": "pause", "until_local": "not a date"}, 422),
    ({"action": "explode"}, 422),
    ({"action": "pause", "surprise": 1}, 422),
])
def test_bad_requests_are_refused_and_change_nothing(client, body, code):
    assert client.put("/api/automation", json=body).status_code == code
    assert client.get("/api/status").json()["automation"] == {"paused": False, "paused_until": None, "override": None}


def test_the_override_floor_follows_the_minimum_level_setting(client):
    client.put("/api/min-soc", json={"revision": client.store.get_setting("min_soc")[0], "value": 0.2})
    assert put(client, action="override", target_percent=15, until_local="2026-10-30T06:00").status_code == 422
    assert put(client, action="override", target_percent=20, until_local="2026-10-30T06:00").status_code == 200


def test_the_dashboard_has_the_controls(client):
    html = client.get("/").text
    for needle in ('id="pause"', 'id="resume"', 'id="override-set"', 'id="override-target"', 'id="override-until"', 'id="pause-until"', 'id="plan-save"', 'id="plan-mode"',
                   'id="plan-from"', 'id="plan-until"', "Plan a visit or time away", 'id="automation-state"'):
        assert needle in html, needle


def test_a_planned_visit_goes_through_the_existing_schedule_api(client):
    revision = client.get("/api/status").json()["demand_revision"]
    reply = client.put("/api/demand-schedule", json={"revision": revision, "mode": "Away from home", "start_local": "2026-11-02T08:00", "end_local": "2026-11-09T18:00",
                                                       "start_fold": None, "end_fold": None, "away_watts": 300, "high_extra_kwh": None})
    assert reply.status_code == 200
    schedule = client.store.get_setting("demand")[1]
    assert schedule["mode"] == "Away from home" and schedule["start"] == london_input("2026-11-02T08:00") and schedule["end"] == london_input("2026-11-09T18:00")


# ===== a busy database is a polite 503, not a server error =================================================================================================
def test_a_locked_database_answers_503_with_a_plain_message_and_other_errors_stay_errors(client, monkeypatch):
    import sqlite3

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(client.store, "set_setting", locked)
    monkeypatch.setattr(client.store, "set_settings", locked)           # return-to-automatic now writes through the one-transaction method (WP18)
    reply = client.put("/api/provider-refresh", json={})
    assert reply.status_code == 503 and reply.json()["detail"] == "The database is busy for a moment. Please try again." and reply.headers["retry-after"] == "2"
    assert put(client, action="pause").status_code == 503                                     # the same for every writing endpoint


def test_an_unrelated_database_error_is_not_dressed_up_as_busy(tmp_path, monkeypatch):
    import sqlite3
    store = Store(tmp_path / "e.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "e.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), FakeClock(NOW), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234), raise_server_exceptions=False) as c:
        c.headers.update({"Origin": "http://solarbank-pi.local", "X-CSRF-Token": c.get("/api/status").json()["csrf_token"]})
        monkeypatch.setattr(store, "set_setting", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("no such table: settings")))
        assert c.put("/api/provider-refresh", json={}).status_code == 500
