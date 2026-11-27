"""WP18 (#20, #22): an owner action that changes two settings changes both or neither, and the status picture is never served as live when it is old."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.settings import london_input
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app

NOW = london_input("2026-10-29T12:00")


def make(tmp_path, **config):
    store = Store(tmp_path / "a.sqlite3"); store.initialise()
    clock = FakeClock(NOW)
    app = create_app(WebConfig(tmp_path / "a.sqlite3", "a-long-test-password", trusted_network=True, **config), clock, store=store)
    client = TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234))
    client.__enter__()
    client.store, client.clock = store, clock
    client.headers.update({"Origin": "http://solarbank-pi.local", "X-CSRF-Token": client.get("/api/status").json()["csrf_token"]})
    return client


@pytest.fixture
def client(tmp_path):
    c = make(tmp_path, status_cache_seconds=0)
    yield c
    c.__exit__(None, None, None)


# ---- #20 ---------------------------------------------------------------------------------------------------------------------------------
def test_20_a_failure_while_returning_to_automatic_leaves_pause_and_override_both_unchanged(client):
    client.put("/api/automation", json={"action": "override", "target_percent": 80, "until_local": "2026-10-30T06:00"})
    client.put("/api/automation", json={"action": "pause"})
    before = (client.store.get_setting("automation_pause"), client.store.get_setting("manual_override"))
    assert before[0][1]["paused"] is True and before[1][1]["target"] == 0.8
    real, calls = client.store._persist, {"n": 0}

    def failing(db, name, revision, body, now):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the disk went away between the two writes")
        return real(db, name, revision, body, now)
    client.store._persist = failing
    with pytest.raises(RuntimeError):
        client.put("/api/automation", json={"action": "resume"})
    client.store._persist = real
    after = (client.store.get_setting("automation_pause"), client.store.get_setting("manual_override"))
    assert after == before, "return to automatic changed only one of the two settings"


def test_20_the_store_changes_several_settings_in_one_transaction_or_none(client):
    store = client.store
    store.set_setting("a_setting", {"v": 1}, NOW, 0)
    with pytest.raises(ValueError):
        store.set_settings({"a_setting": ({"v": 2}, 1), "b_setting": ({"v": 9}, 5)}, NOW)       # the second revision is wrong: nothing is written
    assert store.get_setting("a_setting") == (1, {"v": 1}) and store.get_setting("b_setting")[0] == 0
    revisions = store.set_settings({"a_setting": ({"v": 2}, 1), "b_setting": ({"v": 3}, 0)}, NOW)
    assert revisions == {"a_setting": 2, "b_setting": 1} and store.get_setting("b_setting")[1] == {"v": 3}


def test_20_a_competing_change_between_reading_and_saving_is_refused_not_overwritten(client):
    client.put("/api/automation", json={"action": "pause"})
    revision = client.store.get_setting("automation_pause")[0]
    client.store.set_setting("automation_pause", {"paused": True, "set_at": NOW, "until": None}, NOW, revision)       # another tab got in first
    with pytest.raises(ValueError):
        client.store.set_settings({"automation_pause": ({"paused": False}, revision), "manual_override": ({}, 0)}, NOW)
    assert client.store.get_setting("automation_pause")[1]["paused"] is True


# ---- #22 ---------------------------------------------------------------------------------------------------------------------------------
def test_22_every_status_says_how_old_it_may_be_and_when_it_was_computed_and_served(client):
    data = client.get("/api/status").json()
    assert data["status_max_age_seconds"] == 30 and data["computed_at"] == NOW and data["served_at"] == NOW and data.get("stale") is False


def test_22_while_one_computation_is_blocked_a_concurrent_request_older_than_the_limit_is_marked_stale(tmp_path):
    c = make(tmp_path, status_cache_seconds=0, status_max_age_seconds=30)
    try:
        first = c.get("/api/status").json()                              # a good picture exists, computed at NOW
        gate, entered = threading.Event(), threading.Event()
        import solarbank_optimiser.web.app as module
        real = c.store.pinned_live

        def slow():
            entered.set(); gate.wait(5)
            return real()
        c.store.pinned_live = slow                                       # the next computation blocks
        results = {}
        worker = threading.Thread(target=lambda: results.update(blocked=c.get("/api/status").json()))
        worker.start(); assert entered.wait(5)
        c.clock.advance(45)                                              # 45 s pass while it is stuck
        stale = c.get("/api/status").json()                              # a concurrent request is served the previous picture ...
        assert stale["computed_at"] == first["computed_at"] and stale["stale"] is True and stale["served_at"] == NOW + 45        # ... and says it is old
        assert stale["stale_seconds"] == 45
        gate.set(); worker.join(5)
        assert results["blocked"]["stale"] is False                      # the finished computation is current again
    finally:
        c.__exit__(None, None, None)


def test_22_a_picture_inside_the_limit_is_not_marked_stale(tmp_path):
    c = make(tmp_path, status_cache_seconds=0, status_max_age_seconds=30)
    try:
        c.get("/api/status")
        gate, entered = threading.Event(), threading.Event()
        real = c.store.pinned_live
        c.store.pinned_live = lambda: (entered.set(), gate.wait(5), real())[2]
        t = threading.Thread(target=lambda: c.get("/api/status")); t.start(); assert entered.wait(5)
        c.clock.advance(10)
        assert c.get("/api/status").json()["stale"] is False
        gate.set(); t.join(5)
    finally:
        c.__exit__(None, None, None)


def test_22_the_browser_applies_the_same_limit_and_ignores_an_older_response():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static/common.js").read_text()
    assert "status_max_age_seconds" in js and "stale" in js and "computed_at" in js
    assert "lastComputed" in js                                           # a response older than the one already shown is dropped
