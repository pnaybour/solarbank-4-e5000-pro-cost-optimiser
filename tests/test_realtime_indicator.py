"""The website's 'is the real-time back end running safely?' indicator."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock, Observation
from solarbank_optimiser.realtime_status import LIMITS, assess
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app

NOW = 1_800_000_000.0


def device(age=.3, streak=0, latency=.06, failures=0, **extra):
    return {"age": age, "fail_streak": streak, "latency": latency, "failures": failures, "last_error": "timeout" if streak else None, **extra}


def healthy(**changes):
    health = {"status": "ok", "equipment_writes": False, "direct_devices": ["shelly", "solarbank"],
              "devices": {"shelly": device(), "solarbank": device(latency=.02, slow_error_streak=0, slow_stale=False)},
              "storage_errors": 0, "last_storage_error_at": None, "discarded_total": {}, "discarded_recent": []}
    health.update(changes)
    return health


def level(health, live=NOW - .4):
    return assess(health, NOW, live)["level"]


def texts(health, live=NOW - .4):
    return [r["text"] for r in assess(health, NOW, live)["reasons"]]


def test_healthy_system_is_safe_with_no_reasons():
    result = assess(healthy(), NOW, NOW - .4)
    assert result["level"] == "safe" and result["reasons"] == [] and result["headline"] == "Running safely"
    assert [d["name"] for d in result["devices"]][:1] == ["Solarbank"] and any("Shadow mode" in n for n in result["notes"])


@pytest.mark.parametrize("age,expected", [(.4, "safe"), (2.4, "safe"), (3, "watch"), (5.1, "unsafe"), (60, "unsafe")])
def test_live_readings_age(age, expected):
    assert level(healthy(), NOW - age) == expected


def test_no_collector_output_at_all_is_unsafe_with_a_plain_reason():
    result = assess(None, NOW, None)
    assert result["level"] == "unsafe" and "not publishing anything" in result["reasons"][0]["text"]
    assert level(healthy(), None) == "unsafe" and "file is missing" in texts(healthy(), None)[0]


@pytest.mark.parametrize("status,expected", [("ok", "safe"), ("partial", "watch"), ("unavailable", "unsafe")])
def test_collection_status(status, expected):
    assert level(healthy(status=status)) == expected


@pytest.mark.parametrize("age,expected", [(.2, "safe"), (3, "watch"), (6, "unsafe")])
def test_a_direct_devices_newest_reading(age, expected):
    health = healthy(); health["devices"]["shelly"] = device(age=age)
    assert level(health) == expected


@pytest.mark.parametrize("streak,expected", [(0, "safe"), (1, "safe"), (2, "safe"), (3, "watch"), (5, "watch"), (6, "unsafe")])
def test_consecutive_failed_reads(streak, expected):
    health = healthy(); health["devices"]["solarbank"] = device(streak=streak, slow_error_streak=0)
    assert level(health) == expected


def test_a_device_not_yet_read_is_watch_while_starting():
    health = healthy(); health["devices"] = {"shelly": device()}
    assert level(health) == "watch" and "no reading yet" in texts(health)[0]


def test_solarbank_totals_not_refreshing_is_watch():
    health = healthy(); health["devices"]["solarbank"] = device(slow_error_streak=4, slow_stale=True)
    assert level(health) == "watch" and "withheld" in texts(health)[0]


@pytest.mark.parametrize("ago,expected", [(30, "watch"), (599, "watch"), (700, "safe")])
def test_a_recent_database_hiccup_keeps_it_amber_for_ten_minutes_only(ago, expected):
    assert level(healthy(last_storage_error_at=NOW - ago, storage_errors=3)) == expected


@pytest.mark.parametrize("ago,expected", [(120, "unsafe"), (1700, "unsafe"), (7200, "watch"), (200_000, "safe")])
def test_discarded_data_is_unsafe_while_recent_and_a_note_afterwards(ago, expected):
    health = healthy(discarded_total={"minute_summaries": 4}, discarded_recent=[{"kind": "minute_summaries", "count": 4, "from": 1, "to": 2, "at": NOW - ago}])
    assert level(health) == expected
    if expected == "unsafe":
        assert "discarded" in texts(health)[0]


def test_equipment_writes_must_be_reported_off():
    assert level(healthy(equipment_writes=True)) == "unsafe" and level(healthy(equipment_writes=None)) == "unsafe"


def test_overall_level_is_the_worst_and_the_summary_explains_it():
    health = healthy(status="partial"); health["devices"]["shelly"] = device(age=9)
    result = assess(health, NOW, NOW - .4)
    assert result["level"] == "unsafe" and "Shelly" in result["summary"] and "9 s" in result["summary"]
    assert [r["level"] for r in result["reasons"]] == ["watch", "unsafe"]


def test_devices_read_by_the_monitor_are_listed_and_not_judged():
    result = assess(healthy(), NOW, NOW - .4)
    meter = [d for d in result["devices"] if d["name"] == "Smart meter"][0]
    assert meter["source"] == "original monitor" and meter["state"] == "via the monitor"
    assert any("original energy monitor" in n for n in result["notes"])
    assert "All readings come from the original energy monitor" in assess(healthy(direct_devices=[], devices={}), NOW, NOW - .4)["notes"][0]


def test_age_wording_is_plain():
    health = healthy(); health["devices"]["shelly"] = device(age=125)
    assert "2 min" in texts(health)[0]


# ---- through the website ----------------------------------------------------------------------------

def site(tmp_path, live=True, health=None):
    store = Store(tmp_path / "r.sqlite3", live_path=tmp_path / "live.json"); store.initialise()
    clock = FakeClock(NOW)
    if live:
        store.publish_live([Observation("monitor:solarbank:soc", "soc", NOW - .3, .5, "fraction", source_at=NOW - .5, provenance="shared-monitor-observation")],
                           {"collector_health": {**(health or healthy()), "acquired_at": NOW - .3}})
    app = create_app(WebConfig(tmp_path / "r.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), clock, store=store)
    return TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234))


def test_status_carries_the_judgement(tmp_path):
    with site(tmp_path) as client:
        body = client.get("/api/status").json()
    assert body["realtime"]["level"] == "safe" and body["realtime"]["headline"] == "Running safely"
    assert {"level", "headline", "summary", "reasons", "devices", "notes", "checked_at"} <= set(body["realtime"])


def test_status_says_not_safe_when_nothing_is_being_collected(tmp_path):
    with site(tmp_path, live=False) as client:
        body = client.get("/api/status").json()
    assert body["realtime"]["level"] == "unsafe" and "not publishing" in body["realtime"]["summary"]


def test_status_flags_a_stalled_collector(tmp_path):
    stale = healthy(); stale["devices"]["solarbank"] = device(age=30)
    with site(tmp_path, health=stale) as client:
        body = client.get("/api/status").json()
    assert body["realtime"]["level"] == "unsafe" and "Solarbank" in body["realtime"]["summary"]


def test_the_page_has_the_pill_on_every_page_and_the_panel_on_the_settings_diagnostics_screen(tmp_path):
    with site(tmp_path) as client:
        for path in ("/", "/settings", "/history"):
            html = client.get(path).text
            assert 'id="rt-pill"' in html and 'id="rt-text"' in html, path
        dashboard, settings = client.get("/").text, client.get("/settings").text
    assert 'id="realtime"' in settings and 'id="rt-reasons"' in settings and 'id="rt-devices"' in settings and '<a href="#realtime">' in settings
    assert 'id="realtime"' not in dashboard and 'href="/settings#realtime"' in dashboard          # owner, 20 November 2026: only the pill stays


def test_the_styles_exist_for_every_level():
    css = (Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static/dashboard.css").read_text()
    for name in ("rt-safe", "rt-watch", "rt-unsafe", "rt-unknown", "rt-pill"):
        assert name in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pill_script_paints_the_right_words_icons_and_classes():
    static = Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static"
    script = r"""
const fs=require("fs"),vm=require("vm");
const nodes={};
function node(){return {className:"",textContent:"",title:"",querySelector(){return nodes["icon"]||(nodes["icon"]={textContent:""});}};}
const document={getElementById(id){return id==="rt-pill"||id==="rt-text"?(nodes[id]||(nodes[id]=node())):null;}};
const context={document,AbortSignal:{timeout(){}},console};vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
const out=[];
for(const rt of [{level:"safe",headline:"Running safely",summary:"ok"},{level:"watch",headline:"Running, with something to watch",summary:"x"},{level:"unsafe",headline:"Not running safely",summary:"y"},null]){
  vm.runInContext("paintRealtime("+JSON.stringify(rt)+")",context);
  out.push([nodes["rt-pill"].className,nodes["rt-text"].textContent,nodes["icon"].textContent,nodes["rt-pill"].title]);
}
console.log(JSON.stringify(out));
"""
    result = subprocess.run(["node", "-e", script, str(static / "common.js")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    safe, watch, unsafe, unreachable = json.loads(result.stdout)
    assert safe[:3] == ["rt-pill rt-safe", "Real-time: SAFE", "●"]
    assert watch[:3] == ["rt-pill rt-watch", "Real-time: WATCH", "▲"]
    assert unsafe[:3] == ["rt-pill rt-unsafe", "Real-time: NOT SAFE", "■"]
    assert unreachable[0] == "rt-pill rt-unsafe" and unreachable[1] == "Real-time: NOT SAFE" and "cannot reach" in unreachable[3]


@pytest.mark.parametrize("ahead,expected", [(.5, "safe"), (1.5, "unsafe"), (3600, "unsafe")])
def test_a_live_file_dated_in_the_future_is_never_shown_as_safe(ahead, expected):
    """Review 0.5.7: after the clock is set back, a future-dated file must not read as fresh."""
    assert level(healthy(), NOW + ahead) == expected
    if expected == "unsafe":
        assert any("future" in text for text in texts(healthy(), NOW + ahead))


@pytest.mark.parametrize("age,expected", [(-.5, "safe"), (-2, "unsafe"), (-3600, "unsafe")])
def test_a_device_reading_dated_in_the_future_is_never_shown_as_safe(age, expected):
    health = healthy(devices={"shelly": device(age=age), "solarbank": device()})
    assert level(health) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_one_slow_or_failed_status_poll_keeps_the_picture_and_two_in_a_row_show_unavailable():
    """The status page can take a few seconds on a busy disk: a single miss must not flash NOT SAFE, but a real outage must still show it within two polls."""
    static = Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static"
    script = r"""
const fs=require("fs"),vm=require("vm");
const events=[],timeouts=[];
const outcomes=["ok","fail","ok","fail","fail"];let n=0,t=0;
const node=()=>({className:"",textContent:"",title:"",querySelector(){return {textContent:""};},classList:{toggle(){}}});
const document={getElementById:()=>node()};
const context={document,console,events,
  AbortSignal:{timeout(ms){timeouts.push(ms);return undefined;}},
  fetch:async()=>{const o=outcomes[n++];if(o==="fail")throw new Error("timeout");t+=1;return {ok:true,json:async()=>({computed_at:t,served_at:t,status_max_age_seconds:30,realtime:{level:"safe"}})};},
  setTimeout(f){context.next=f;}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
vm.runInContext("paintRealtime=function(rt){events.push(rt===null?'unsafe':'painted');};render=function(){};disconnected=function(){events.push('disconnected');};",context);
(async()=>{
  vm.runInContext("startPolling()",context);
  for(let i=0;i<outcomes.length;i++){await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));if(i<outcomes.length-1)context.next();}
  console.log(JSON.stringify({events,timeouts:[...new Set(timeouts)]}));
})();
"""
    result = subprocess.run(["node", "-e", script, str(static / "common.js")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["events"] == ["painted", "painted", "unsafe", "disconnected"], out          # ok, miss (nothing), ok, miss (nothing), miss -> unavailable
    assert out["timeouts"] == [12000]
