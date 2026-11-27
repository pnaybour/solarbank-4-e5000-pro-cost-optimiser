"""The staleness limits as a setting (owner decision 8 November 2026, option C): validation, the plan file carries them, the controller reads them."""
import json

import pytest

from solarbank_optimiser import freshness
from solarbank_optimiser.control_logic import read_plan


def test_valid_limits_pass_and_anything_else_falls_back_to_the_defaults():
    assert freshness.valid({"charge_s": 2.5, "idle_s": 10}) == {"charge_s": 2.5, "idle_s": 10.0}
    for bad in (None, {}, {"charge_s": 2.5}, {"charge_s": "2.5", "idle_s": 10}, {"charge_s": True, "idle_s": 10}, {"charge_s": 0.5, "idle_s": 10},
                {"charge_s": 11, "idle_s": 20}, {"charge_s": 2.5, "idle_s": 60}, {"charge_s": 5, "idle_s": 3}, {"charge_s": float("nan"), "idle_s": 10}):
        assert freshness.valid(bad) is None, bad
        assert freshness.stored(bad) == {"charge_s": 2.5, "idle_s": 10.0}


def test_the_plan_file_carries_the_limits_and_a_bad_value_never_reaches_the_controller(tmp_path):
    path = tmp_path / "plan.json"
    base = {"mode": "shadow", "action": "normal", "target_soc": None, "issued_at": 100.0, "valid_until": 400.0}
    path.write_text(json.dumps({**base, "freshness": {"charge_s": 3, "idle_s": 12}}))
    plan, _ = read_plan(path, 110.0)
    assert plan["freshness"] == {"charge_s": 3.0, "idle_s": 12.0}
    path.write_text(json.dumps({**base, "freshness": {"charge_s": 0.01, "idle_s": 1}}))
    plan, _ = read_plan(path, 110.0)
    assert plan["freshness"] == {"charge_s": 2.5, "idle_s": 10.0}                         # out of range: the defaults
    path.write_text(json.dumps(base))
    assert read_plan(path, 110.0)[0]["freshness"] == {"charge_s": 2.5, "idle_s": 10.0}   # an older planner that does not write it


def test_the_settings_page_saves_validates_and_publishes_the_limits(tmp_path):
    from fastapi.testclient import TestClient
    from solarbank_optimiser.models import FakeClock
    from solarbank_optimiser.shadow import controller_plan_document  # noqa: F401  (the plan file is written by publish_controller_plan)
    from solarbank_optimiser.store import Store
    from solarbank_optimiser.web.app import WebConfig, create_app
    from test_control_loop import UTC
    store = Store(tmp_path / "f.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "f.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), FakeClock(UTC), store=store)
    client = TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234))
    with client:
        state = client.get("/api/status").json()
        assert state["freshness"] == {"charge_s": 2.5, "idle_s": 10.0} and state["freshness_revision"] == 0
        headers = {"Origin": "http://solarbank-pi.local", "X-CSRF-Token": state["csrf_token"]}
        put = lambda body: client.put("/api/freshness", json={"revision": 0, **body}, headers=headers)
        assert put({"charge_s": 0.5, "idle_s": 10}).status_code == 422                 # tighter than allowed
        assert put({"charge_s": 5, "idle_s": 3}).status_code == 422                    # idle tighter than charging
        assert put({"charge_s": 3, "idle_s": 12}).status_code == 200
        assert client.get("/api/status").json()["freshness"] == {"charge_s": 3.0, "idle_s": 12.0}
    assert freshness.stored(store.get_setting("freshness")[1]) == {"charge_s": 3.0, "idle_s": 12.0}


def test_the_planner_writes_the_stored_limits_into_the_plan_file(tmp_path):
    from solarbank_optimiser.shadow import publish_controller_plan
    from solarbank_optimiser.store import Store
    store = Store(tmp_path / "g.sqlite3"); store.initialise()
    store.set_setting("freshness", {"charge_s": 4.0, "idle_s": 15.0}, 100.0, 0)
    path = tmp_path / "plan.json"
    publish_controller_plan(path, store, None, 1000.0)
    assert json.loads(path.read_text())["freshness"] == {"charge_s": 4.0, "idle_s": 15.0}
