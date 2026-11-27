"""The adjustable minimum battery level (owner, 28 Oct): 10 % by default, one stored value, used by the planner and the controller."""
import json

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.battery_limits import DEFAULT_MIN_SOC, MIN_SOC_HIGHEST, MIN_SOC_LOWEST, valid_min_soc
from solarbank_optimiser.control_logic import Supervisor, read_plan
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app
from test_control_loop import UTC, fresh, plan, run
from test_shadow import NOW, ShadowConfig, assemble, job, put, site, snapshot  # noqa: F401  (site is a fixture)


def web(tmp_path):
    store = Store(tmp_path / "m.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "m.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), FakeClock(UTC), store=store)
    return TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)), store


def put_value(client, revision, value, token=True):
    state = client.get("/api/status").json()
    headers = {"Origin": "http://solarbank-pi.local", **({"X-CSRF-Token": state["csrf_token"]} if token else {})}
    return client.put("/api/min-soc", json={"revision": revision, "value": value}, headers=headers)


def test_the_default_is_ten_percent_and_the_range_is_five_to_twenty():
    assert DEFAULT_MIN_SOC == 0.10 and (MIN_SOC_LOWEST, MIN_SOC_HIGHEST) == (0.05, 0.20)
    for bad in (None, True, "0.1", float("nan"), 0.04, 0.31, -1, 10):
        assert valid_min_soc(bad) is None
    assert valid_min_soc(0.05) == 0.05 and valid_min_soc(0.20) == 0.20 and valid_min_soc(0.21) is None and valid_min_soc(0.30) is None and valid_min_soc(1) is None


def test_the_web_setting_saves_validates_and_is_protected(tmp_path):
    client, store = web(tmp_path)
    with client:
        status = client.get("/api/status").json()
        assert status["min_soc"] == 0.10 and status["min_soc_range"] == [0.05, 0.20] and status["min_soc_revision"] == 0
        assert put_value(client, 0, 0.15).status_code == 200
        assert client.get("/api/status").json()["min_soc"] == 0.15 and store.get_setting("min_soc")[1] == {"value": 0.15}
        for bad in (0.04, 0.31, "x"):
            assert put_value(client, 1, bad).status_code == 422
        assert put_value(client, 0, 0.2).status_code == 409                       # stale revision: someone else changed it
        assert put_value(client, 1, 0.2, token=False).status_code == 403          # needs the page's token like every change
        assert client.get("/api/status").json()["min_soc"] == 0.15


def test_the_settings_page_has_the_field_and_the_dashboard_states_the_level(tmp_path):
    client, _ = web(tmp_path)
    with client:
        settings = client.get("/settings").text
        assert 'id="min-soc-form"' in settings and 'id="min-soc"' in settings and 'min="5" max="20"' in settings
        assert 'id="home-min"' in client.get("/").text                         # the home page's battery card states the level


def test_the_planner_models_the_stored_level(site):
    store, clock = site
    assert job(site)["model"]["reserve"] == 0.10                                  # the default
    put(store, "min_soc", {"value": 0.20}, clock.utc())
    assert job(site)["model"]["reserve"] == 0.20
    put(store, "min_soc", {"value": 0.01}, clock.utc())                           # an invalid stored value falls back safely
    assert job(site)["model"]["reserve"] == 0.10


def test_a_new_level_changes_the_planning_fingerprint_so_the_old_plan_is_withdrawn(site):
    from solarbank_optimiser.shadow import guard, snapshot as snap
    store, clock = site
    before = guard(snap(store, clock.utc()), clock.utc())
    put(store, "min_soc", {"value": 0.25}, clock.utc())
    assert guard(snap(store, clock.utc()), clock.utc()) != before


def test_the_plan_file_carries_the_level_and_the_controller_obeys_it(tmp_path):
    from solarbank_optimiser.shadow import publish_controller_plan
    store = Store(tmp_path / "p.sqlite3"); store.initialise()
    file = tmp_path / "plan.json"
    summary = {"issued_at": UTC - 5, "valid_until": UTC + 240, "grid_target": .85, "next_window": {"start": UTC - 600, "end": UTC + 3600, "target": .85}}
    for stored, expected in ((None, 0.10), (0.20, 0.20), (0.07, 0.07)):
        if stored is not None:
            store.set_setting("min_soc", {"value": stored}, UTC, store.get_setting("min_soc")[0])
        publish_controller_plan(file, store, summary, UTC)
        document, why = read_plan(file, UTC)
        assert why is None and document["min_soc"] == expected
    # GitHub issue 64 (D13, owner 2 November): the level is still what the planner never plans below and what Settings shows and clamps the
    # target to, but the controller itself no longer inhibits discharge at or below it: the firmware's own floor (register 60001) does that.
    sup = Supervisor()
    run(sup, 100, fresh(soc=50, current=20), document)
    for soc in (8, 7, 6):
        decision = sup.decide(fresh(soc=soc, current=20), document, None, 200.0 + soc)
        assert decision.discharge == "ALLOW" and "reserve" not in decision.restrictions


def test_without_a_plan_or_with_a_bad_level_the_controller_uses_ten_percent(tmp_path):
    f = tmp_path / "plan.json"
    for level in (None, "x", 0.01, 2, True, float("nan")):
        body = {**plan(), "issued_at": UTC - 10, "valid_until": UTC + 100}
        if level is not None:
            body["min_soc"] = level
        f.write_text(json.dumps(body, allow_nan=True))
        document, why = read_plan(f, UTC)
        assert why is None and document["min_soc"] == 0.10
    sup = Supervisor()
    run(sup, 100, fresh(soc=50, current=20), None)
    # GitHub issue 64: no plan means no target to hold, so DISCHARGE either way; the 10 % default is shown and clamps, never inhibits here.
    assert sup.decide(fresh(soc=10, current=20), None, "No plan", 300.0).discharge == "ALLOW"
    assert sup.decide(fresh(soc=11, current=20), None, "No plan", 301.0).discharge == "ALLOW"


def test_candidate_targets_never_go_below_the_minimum_level(site):
    store, clock = site
    assert min(job(site)["targets"]) == 0.10 and max(job(site)["targets"]) == 1.0
    put(store, "min_soc", {"value": 0.18}, clock.utc())
    assert min(job(site)["targets"]) == 0.20 and 0.15 not in job(site)["targets"]
    put(store, "min_soc", {"value": 0.25}, clock.utc())                         # above the device's own 20 % limit: not accepted, the 10 % default applies
    assert min(job(site)["targets"]) == 0.10


def test_the_battery_card_shows_the_real_minimum_level_not_a_fixed_reserve(tmp_path):
    client, store = web(tmp_path)
    with client:
        page = client.get("/").text
        assert "Reserve <strong>5%</strong>" not in page and "Solar ceiling" not in page
        assert 'id="home-min"' in page                                                  # the home page's battery card fills in the minimum level
        assert client.get("/api/status").json()["min_soc"] == 0.10                       # the value the card fills in from


def test_the_controller_never_inhibits_discharge_for_the_minimum_level_itself(tmp_path):
    """GitHub issue 64 (D13, owner 2 November): specification section 7, 'the minimum SoC floor [is] left entirely to the firmware... when the battery
    reaches its floor in DISCHARGE, the firmware puts the unit to sleep by itself'. At or below the stored level, in an expensive period with no car and
    no charge target, the controller must still ALLOW (DISCHARGE, mode 0): it is never the controller's own write that stops the battery here."""
    sup = Supervisor()
    run(sup, 100, fresh(soc=50, current=20), None)
    for soc in (10, 5, 1, 0):
        decision = sup.decide(fresh(soc=soc, current=20), None, "No plan", 1000.0 + soc)
        assert decision.discharge == "ALLOW" and "reserve" not in decision.restrictions, (soc, decision.restrictions)


def test_the_devices_own_charging_limit_clamps_the_target_and_an_absurd_register_value_is_ignored():
    """GitHub issue 70 (D13, second half): the controller reads the device's own charging_limit_soc/discharge_limit_soc (registers 60000, 60001) from
    the solarbank reading and clamps the plan's target to them, instead of only showing them on the Settings page."""
    from test_control_loop import fresh as base_fresh

    def with_limits(charging=None, discharge=None, **kw):
        inputs = base_fresh(**kw)
        if charging is not None:
            inputs["solarbank"]["value"]["charging_limit_soc"] = charging
        if discharge is not None:
            inputs["solarbank"]["value"]["discharge_limit_soc"] = discharge
        return inputs

    document = plan(action="charge_to", target=1.0)                                 # the plan wants 100 %
    sup = Supervisor()
    run(sup, 100, with_limits(90, 5, soc=50, current=20), document)
    # the app's own 90 % charging limit clamps the controller's target below the plan's 100 %, so the battery is held once it reaches 90 %, not 100 %
    held = sup.decide(with_limits(90, 5, soc=90, current=20), document, None, 200.0)
    assert held.discharge == "INHIBIT" and "target 90%" in " ".join(held.reasons)
    still_below = sup.decide(with_limits(90, 5, soc=85, current=20), document, None, 201.0)
    assert still_below.charge_limit_w > 0                                            # below the clamped 90 %: still charging requested

    # an absurd register value (a misread, or the device not reporting the expected range) is ignored: the plan's 100 % is used unclamped
    ignored = sup.decide(with_limits(0, 50, soc=99, current=20), document, None, 202.0)
    assert "target 100%" in " ".join(ignored.reasons) or ignored.charge_limit_w > 0
