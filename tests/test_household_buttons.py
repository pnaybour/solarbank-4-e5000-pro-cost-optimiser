"""Household demand: three buttons on the dashboard (Away, Normal, High); no start/stop times and no save button."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app

STATIC = Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static"
NOW = 1_800_000_000.0


def site(tmp_path):
    store = Store(tmp_path / "h.sqlite3"); store.initialise()
    clock = FakeClock(NOW)
    app = create_app(WebConfig(tmp_path / "h.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), clock, store=store)
    return TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)), clock


def press(client, mode, away=None):
    """Exactly the request the page sends when a button is tapped."""
    state = client.get("/api/status").json()
    body = {"revision": state["demand_revision"], "mode": mode, "start_local": None, "end_local": None, "start_fold": None, "end_fold": None,
            "away_watts": away, "high_extra_kwh": None}
    return client.put("/api/demand-schedule", json=body, headers={"Origin": "http://solarbank-pi.local", "X-CSRF-Token": state["csrf_token"]})


# ---- the pages --------------------------------------------------------------------------------------

def test_dashboard_has_exactly_the_three_buttons(tmp_path):
    client, _ = site(tmp_path)
    with client:
        html = client.get("/").text
    for mode, word in (("Away from home", "Away"), ("Normal consumption", "Normal"), ("High demand", "High")):
        assert f'data-mode="{mode}"' in html and f">{word}</button>" in html
    assert html.count('class="mode-btn"') == 3
    assert 'id="away"' in html                                                     # the base load used by Away
    for gone in ('id="demand-start"', 'id="demand-end"', 'Save household setting', 'id="demand-form"', 'id="start-fold"'):
        assert gone not in html


def test_settings_page_no_longer_has_the_household_form(tmp_path):
    client, _ = site(tmp_path)
    with client:
        html = client.get("/settings").text
    for gone in ('id="demand-start"', 'id="demand-end"', 'Save household setting', 'id="demand-form"', 'id="demand"', 'id="away"', 'id="start-fold"', 'id="end-fold"'):
        assert gone not in html, gone
    assert 'id="household"' not in html and 'id="control"' not in html           # household lives on the dashboard; control moved there too
    scripts = (STATIC / "settings.js").read_text()
    assert 'el("demand")' not in scripts and "demand-form" not in scripts and "demand-start" not in scripts


# ---- what a tap does on the server -------------------------------------------------------------------

def test_each_button_applies_from_now_with_no_end_and_replaces_the_previous_choice(tmp_path):
    client, clock = site(tmp_path)
    with client:
        assert press(client, "Away from home", 300.0).status_code == 200
        state = client.get("/api/status").json()
        assert state["active_demand"] == "Away from home" and state["demand"]["away_watts"] == 300.0
        assert state["demand"]["end"] is None and state["demand"]["start"] == NOW and state["demand"]["high_extra_kwh"] is None
        clock.advance(600)
        assert press(client, "High demand", 300.0).status_code == 200
        state = client.get("/api/status").json()
        assert state["active_demand"] == "High demand" and state["demand"]["start"] == NOW + 600 and state["demand"]["end"] is None
        assert state["demand"]["away_watts"] == 300.0                              # the base load is remembered for next time
        clock.advance(600)
        assert press(client, "Normal consumption", 300.0).status_code == 200
        assert client.get("/api/status").json()["active_demand"] == "Normal consumption"


def test_away_without_a_base_load_is_refused_clearly(tmp_path):
    client, _ = site(tmp_path)
    with client:
        response = press(client, "Away from home", None)
        assert response.status_code in (409, 422)
        assert client.get("/api/status").json()["active_demand"] == "Normal consumption"      # nothing changed


def test_a_tap_needs_the_same_protections_as_every_other_change(tmp_path):
    client, _ = site(tmp_path)
    with client:
        state = client.get("/api/status").json()
        body = {"revision": state["demand_revision"], "mode": "High demand"}
        assert client.put("/api/demand-schedule", json=body).status_code == 403                                           # no token
        assert client.put("/api/demand-schedule", json=body, headers={"Origin": "http://evil.example", "X-CSRF-Token": state["csrf_token"]}).status_code in (403, 421)
        assert client.get("/api/status").json()["active_demand"] == "Normal consumption"


def test_two_browsers_cannot_overwrite_each_other_silently(tmp_path):
    client, _ = site(tmp_path)
    with client:
        stale = client.get("/api/status").json()
        assert press(client, "High demand").status_code == 200
        body = {"revision": stale["demand_revision"], "mode": "Away from home", "away_watts": 200.0}
        response = client.put("/api/demand-schedule", json=body, headers={"Origin": "http://solarbank-pi.local", "X-CSRF-Token": stale["csrf_token"]})
        assert response.status_code == 409 and client.get("/api/status").json()["active_demand"] == "High demand"


# ---- what a tap does in the page (the real script, run under Node) ---------------------------------------

SCRIPT = r"""
const fs=require("fs"),vm=require("vm");
const saved=[],messages=[],buttons=[];
function element(extra={}){return Object.assign({value:"",dataset:{},textContent:"",hidden:false,className:"",classList:{toggle(){},contains(){return false;}},
  addEventListener(){},focus(){},setAttribute(name,value){this[name]=value;},querySelector(){return element();},replaceChildren(){},appendChild(){}},extra);}
const ids={};
const document={getElementById(id){return ids[id]||(ids[id]=element());},createElement(){return element();},activeElement:null,
  querySelectorAll(selector){return selector===".mode-btn"?buttons:[];}};
for(const mode of ["Away from home","Normal consumption","High demand"])buttons.push(element({dataset:{mode},"aria-pressed":"false"}));
const context={document,console,Date,Number,Math,Error,Promise,setTimeout:()=>0,clearTimeout(){},
 message:(text)=>messages.push(text),action:(fn)=>fn,save:async(path,body)=>{saved.push([path,body]);},
 londonTime:()=>"",startPolling(){},paintRealtime(){},el:(id)=>document.getElementById(id)};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
async function scenario(active,demand,typed,mode){
  saved.length=0;messages.length=0;
  context.state={active_demand:active,demand,demand_revision:7};
  ids["away"]=element({value:typed});ids["household-note"]=element();
  let error=null;
  try{await vm.runInContext("chooseMode("+JSON.stringify(mode)+")",context);}catch(e){error=e.message;}
  return {saved:saved.map(s=>s[1]),path:saved.map(s=>s[0]),messages:messages.slice(),note:ids["household-note"].textContent,error};
}
(async()=>{
 const out={};
 out.awayNoLoad=await scenario("Normal consumption",null,"","Away from home");
 out.awayTyped=await scenario("Normal consumption",null,"300","Away from home");
 out.highRemembers=await scenario("Normal consumption",{away_watts:300},"","High demand");
 out.alreadyNormal=await scenario("Normal consumption",{away_watts:300},"","Normal consumption");
 out.normalFromHigh=await scenario("High demand",{away_watts:300},"300","Normal consumption");
 out.awaySame=await scenario("Away from home",{away_watts:300},"300","Away from home");
 out.awayChanged=await scenario("Away from home",{away_watts:300},"450","Away from home");
 out.awayBad=await scenario("Normal consumption",null,"-5","Away from home");
 context.state={active_demand:"High demand",demand:{away_watts:300}};
 vm.runInContext("renderHousehold()",context);
 out.pressed=buttons.map(b=>[b.dataset.mode,b["aria-pressed"]]);
 console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_script_sends_the_right_request_for_each_tap():
    result = subprocess.run(["node", "-e", SCRIPT, str(STATIC / "dashboard.js")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # 20 November 2026: the answer to a tap is in the Household card, where the finger is (the page's message line is off-screen by then)
    assert out["awayNoLoad"]["saved"] == [] and "Away base load" in out["awayNoLoad"]["note"] and out["awayNoLoad"]["error"] is None
    assert out["awayBad"]["saved"] == [] and "Away base load" in out["awayBad"]["note"]
    sent = out["awayTyped"]["saved"][0]
    assert out["awayTyped"]["path"] == ["/api/demand-schedule"]
    assert sent == {"revision": 7, "mode": "Away from home", "start_local": None, "end_local": None, "start_fold": None, "end_fold": None,
                    "away_watts": 300, "high_extra_kwh": None}
    assert out["highRemembers"]["saved"][0]["away_watts"] == 300 and out["highRemembers"]["saved"][0]["start_local"] is None
    assert out["alreadyNormal"]["saved"] == [] and "already set" in out["alreadyNormal"]["note"]
    assert out["normalFromHigh"]["saved"][0]["mode"] == "Normal consumption" and "Normal from now" in out["normalFromHigh"]["note"]
    assert not any(out[k]["messages"] for k in out if k != "pressed")                     # nothing goes to the off-screen message line
    assert out["awaySame"]["saved"] == [] and out["awayChanged"]["saved"][0]["away_watts"] == 450
    assert out["pressed"] == [["Away from home", "false"], ["Normal consumption", "false"], ["High demand", "true"]]


LATCH = r"""
const fs=require("fs"),vm=require("vm");
const buttons=[],ids={};
function element(extra={}){const e=Object.assign({dataset:{},textContent:"",hidden:false,className:"",classList:{toggle(){},contains(){return false;}},
  addEventListener(){},focus(){},setAttribute(name,value){this[name]=value;},querySelector(){return element();},replaceChildren(){},appendChild(){}},extra);
  let v=String(extra.value??"");Object.defineProperty(e,"value",{get(){return v;},set(x){v=String(x);}});return e;}      // an input's value is always text
const document={getElementById(id){return ids[id]||(ids[id]=element());},createElement(){return element();},activeElement:null,
  querySelectorAll(selector){return selector===".mode-btn"?buttons:[];}};
for(const mode of ["Away from home","Normal consumption","High demand"])buttons.push(element({dataset:{mode},"aria-pressed":"false"}));
const context={document,console,Date,Number,Math,Error,Promise,setTimeout:()=>0,clearTimeout(){},message:()=>{},action:f=>f,londonTime:()=>"",startPolling(){},paintRealtime(){},el:id=>document.getElementById(id)};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
const pressed=()=>buttons.map(b=>b["aria-pressed"]==="true" ? b.dataset.mode.split(" ")[0] : null).filter(Boolean).join(",");
const tick=()=>new Promise(r=>setTimeout(r,0));
(async()=>{
 const out={};
 context.state={active_demand:"Normal consumption",demand:{away_watts:300},demand_revision:3};
 vm.runInContext("renderHousehold()",context);out.start=pressed();
 let release,fail;
 context.save=()=>new Promise((resolve,reject)=>{release=resolve;fail=reject;});          // the server has not answered yet
 ids["away"]=element({value:"300"});
 const first=vm.runInContext('chooseMode("High demand")',context);await tick();
 out.whileSaving=pressed();                                                               // must already show High
 context.state.active_demand="Normal consumption";vm.runInContext("renderHousehold()",context);   // a poll re-renders with the old server state
 out.afterPoll=pressed();
 out.note=ids["household-schedule"].textContent;
 context.state.active_demand="High demand";release();await first;
 out.confirmed=pressed();out.noteAfter=ids["household-schedule"].textContent;
 // a refused save must put the buttons back
 context.state.active_demand="Normal consumption";vm.runInContext("renderHousehold()",context);
 const second=vm.runInContext('chooseMode("Away from home")',context);await tick();
 out.away=pressed();
 fail(new Error("conflict"));await second;
 out.reverted=pressed();out.error=ids["household-note"].textContent;                     // a refusal is said in the card
 console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_button_latches_at_once_survives_a_poll_and_goes_back_if_the_save_is_refused():
    result = subprocess.run(["node", "-e", LATCH, str(STATIC / "dashboard.js")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["start"] == "Normal"
    assert out["whileSaving"] == "High" and out["afterPoll"] == "High" and out["note"] == "Saving…"      # immediate, and a poll cannot flip it back
    assert out["confirmed"] == "High" and out["noteAfter"] == ""
    assert out["away"] == "Away" and out["reverted"] == "Normal" and out["error"] == "conflict"
