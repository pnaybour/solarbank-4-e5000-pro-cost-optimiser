"""What the Anker is doing, as the status describes it, and the one live-class switch for the fixed wording. (The Tonight's-charge panel these tests once drove was a hidden duplicate of the home page's Tonight card and was removed on 20 November 2026.)"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.anker_view import describe
from solarbank_optimiser.collector import decode_snapshot
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app

STATIC = Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static"


# ---- what the Anker is doing (the 27/28 October readings are the fixtures) -----------------------------------------------------------
@pytest.mark.parametrize("battery,grid,house,state,from_grid", [
    (-2.47, 2.71, 0.24, "charging", True),       # 00:30: grid charge at 2.5 kW
    (0.17, 0.0, 0.17, "discharging", False),     # 23:35: serving the house
    (0.0, 0.31, 0.31, "idle", False),            # 02:30: full, house on the grid
    (-0.8, 0.0, 0.3, "charging", False),         # solar charging
    (-0.04, 0.3, 0.3, "idle", False),
    (None, 0.0, 0.1, "unknown", None)])
def test_what_the_anker_is_doing(battery, grid, house, state, from_grid):
    view = describe(battery, grid, house)
    assert view["state"] == state and view["from_grid"] == from_grid and view["text"]


def test_junk_never_breaks_the_description():
    for value in (True, "x", float("nan"), [], {}):
        assert describe(value, 0, 0)["state"] == "unknown"
    assert describe(-1.0, "x", None)["from_grid"] is False


def test_the_status_carries_the_anker_activity(tmp_path):
    store = Store(tmp_path / "t.sqlite3"); store.initialise()
    clock = FakeClock(101)
    app = create_app(WebConfig(tmp_path / "t.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), clock, store=store)
    payload = {"ok": True, "sample": {"time": 100, "main": 500, "sub": 100, "garage_current_a": 1.0},
               "solarbank": {"ok": True, "sample": {"time": 100, "soc": 58, "pv": 0, "load": 240, "grid": 2710, "battery": -2470}},
               "shelly": {"ok": True, "sample": {"time": 100, "ch1": 0.0, "ch2": 0.0}}}
    store.add_observations(decode_snapshot(payload, 101))
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        view = client.get("/api/status").json()["anker_now"]
    assert view["state"] == "charging" and view["from_grid"] is True and "from the grid" in view["text"]


def test_the_pages_fixed_wording_switches_on_one_live_class():
    """Every fixed not-live phrase on the dashboard sits in a shadow-only span with a live-only partner, and the stylesheet hides one of each."""
    page = (STATIC.parent / "templates" / "dashboard.html").read_text()
    css = (STATIC / "dashboard.css").read_text()
    assert "body:not(.control-live) .live-only,body.control-live .shadow-only{display:none}" in css
    for phrase in ("Neither sends commands while the controller is in shadow mode", "Automatic control is unavailable"):
        before = page[:page.index(phrase)]
        assert before.rindex('<span class="shadow-only">') > before.rfind("</span>") - 1, phrase
    assert page.count('class="live-only"') == page.count('class="shadow-only"') >= 2
    assert "Shadow advice</span>" not in page and "Refresh shadow advice" not in page
