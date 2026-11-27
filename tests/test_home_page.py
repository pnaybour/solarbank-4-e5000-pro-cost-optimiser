"""The Anker-style home page (owner, 19 November 2026): the plain words at the top come from the same status as the rest of the page, nothing on it sends a
command, and every element the existing page fills in is still there (behind the Settings menu, or hidden where the new top repeats it)."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web"
STATIC, TEMPLATES = ROOT / "static", ROOT / "templates"

SCRIPT = r"""
const fs=require("fs"),vm=require("vm");
const ids={};const element=()=>({textContent:"",attrs:{},setAttribute(k,v){this.attrs[k]=v;},disabled:false});
const document={getElementById(id){return ids[id]||(ids[id]=element());}};
const context={document,Math,Number,Date,String,JSON,console,el:id=>document.getElementById(id),pounds:v=>"£"+Number(v).toFixed(2),
 isLive:s=>!!(s.realtime&&s.realtime.controller&&s.realtime.controller.present&&s.realtime.controller.mode==="live")};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
const input=JSON.parse(process.argv[2]);context.state=input.state;
vm.runInContext("renderHome()",context);
console.log(JSON.stringify(Object.fromEntries(Object.entries(ids).map(([k,v])=>[k,v.textContent||v.attrs["stroke-dasharray"]||""]))));
"""


def reading(value):
    return {"value": value}


def status(battery=0.74, soc=0.22, decision=None, live=True, window_start=None, car="standby"):
    start = window_start if window_start is not None else 1795131000          # 23:30 London on 19 November 2026
    return {"readings": {"soc": reading(soc), "battery_power": reading(battery), "pv_power": reading(0.0), "grid_power": reading(0.0), "ev_power": reading(0.0)},
            "min_soc": 0.1, "charger_state": car, "anker_now": {"from_grid": True},
            "tariff": {"start_minute": 1410, "end_minute": 330},
            "shadow_plan": {"grid_target": 1.0, "next_window": {"start": start, "end": 1795152600, "target": 1.0}, "expected_cost_gbp": 4.12},
            "savings": {"last_day": {"date": "2026-11-18", "saving_gbp": 1.84}, "last_7_days": {"saving_gbp": 11.62, "days_included": 7},
                        "cumulative_counted": {"saving_gbp": 14.92, "days": 7}},
            "realtime": {"controller": {"present": True, "mode": "live" if live else "shadow",
                                        "decision": decision or {"discharge": "ALLOW", "charge_limit_w": 0, "economic": "handed_over", "restrictions": []}}}}


def home(state):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", SCRIPT, str(STATIC / "home.js"), json.dumps({"state": state})], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_discharging_reads_as_powering_the_house_with_the_figures():
    page = home(status())
    assert page["home-soc"] == "22%" and page["home-ring"] == "22 100"
    assert page["home-state"] == "Powering the house" and page["home-power"] == "740 W from the battery"
    assert page["home-mode"] == "Self-consumption · handed back to the Anker"
    assert page["home-tonight-main"] == "Charge to 100% from 23:30" and "smart slot" not in page["home-tonight-detail"]
    assert page["home-saving"] == "+£1.84" and page["home-week"] == "£11.62" and page["home-live"] == "£14.92"


def test_charging_and_holding_are_told_apart():
    charging = home(status(battery=-2.5, decision={"discharge": "INHIBIT", "charge_limit_w": 2500, "charge_target_soc": 1.0, "restrictions": []}))
    assert charging["home-state"] == "Charging" and charging["home-power"] == "2.50 kW from the grid"
    assert charging["home-mode"] == "Charging at the cheap rate · up to 2.5 kW to 100%"
    car = home(status(battery=0.0, decision={"discharge": "INHIBIT", "charge_limit_w": 0, "restrictions": ["ev_active"]}))
    assert car["home-state"] == "Holding" and "car" in car["home-power"] and car["home-mode"] == "Holding the battery at 0 W · third-party control"


def test_a_smart_slot_says_the_battery_charges_only_while_the_car_does():
    page = home(status(window_start=1795107660))                                # 19:01 London: not the 23:30 window
    assert page["home-tonight-label"] == "Next cheap slot" and "only while the car is charging" in page["home-tonight-detail"]


def test_a_controller_that_is_not_live_says_the_anker_is_in_charge():
    assert home(status(live=False))["home-mode"] == "Watching only · the Anker app is in charge"


def test_no_reading_is_shown_as_a_dash_not_a_zero():
    page = home(status(battery=None, soc=None))
    assert page["home-soc"] == "—" and page["home-state"] == "No fresh reading" and page["home-ring"] == "0 100"


def test_every_element_the_existing_page_fills_in_is_still_on_it():
    html = (TEMPLATES / "dashboard.html").read_text()
    script = (STATIC / "dashboard.js").read_text()
    wanted = set(re.findall(r'el\("([a-z0-9-]+)"\)', script))
    present = set(re.findall(r'id="([a-z0-9-]+)"', html + (TEMPLATES / "base.html").read_text()))
    assert wanted - present == set()
    assert html.index('id="home-hero"') < html.index('<div id="more" class="legacy-pages">')
    assert 'class="legacy-hidden"' not in html                   # the permanently hidden duplicate cards are gone, and the page no longer fills them in
    assert not any(i in html for i in ('id="tonight"', 'id="savings"', 'id="soc"', 'id="rate"', 'id="car-state"', 'id="control-notice"'))


def test_every_settings_row_into_the_home_page_opens_one_of_its_pages():
    settings = (TEMPLATES / "settings.html").read_text()
    html = (TEMPLATES / "dashboard.html").read_text()
    pages = html[html.index('<div id="more" class="legacy-pages">'):]
    targets = re.findall(r'<a href="/#([a-z-]+)"><span>', settings)
    assert sorted(targets) == ["automation", "control", "forecast", "household"]
    assert all(re.search(rf'<section class="panel[^"]*" id="{t}"', pages) for t in targets)


def test_the_home_page_script_sends_nothing():
    code = (STATIC / "home.js").read_text()
    assert "save(" not in code and "PUT" not in code and "POST" not in code and code.count("fetch(") == 1 and '"/api/day' in code


def test_every_settings_menu_row_opens_a_settings_screen_and_every_screen_has_a_row():
    html = (TEMPLATES / "settings.html").read_text()
    rows = re.findall(r'<a href="#([a-z-]+)"><span>', html)
    screens = re.findall(r'<section class="panel" id="([a-z-]+)"', html)
    assert sorted(rows) == sorted(screens) and len(rows) == 9
    menu = (STATIC / "settings-menu.js").read_text()
    assert all(f'"{r}"' in menu or f"{r}:" in menu for r in rows)                  # each row has its value line (empty for device addresses)
    assert "save(" not in menu and "fetch(" not in menu                           # the menu only chooses what is shown


def test_the_phone_tab_bar_style_is_kept_to_the_page_header():
    css = (STATIC / "app.css").read_text()
    assert "header nav{position:fixed" in css and not re.search(r"(?<!header )nav\{position:fixed", css)


ARROWS = r"""
const fs=require("fs"),vm=require("vm");
const ids={};const element=()=>({textContent:"",attrs:{},setAttribute(k,v){this.attrs[k]=v;},disabled:false});
const document={getElementById(id){return ids[id]||(ids[id]=element());}};
const waiting=[];                                   // each fetch waits here until the test answers it
const fetch=(url)=>new Promise((resolve,reject)=>waiting.push({url,resolve,reject}));
const context={document,Math,Number,Date,String,JSON,console,fetch,AbortSignal:{timeout:()=>undefined},el:id=>document.getElementById(id)};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
vm.runInContext("drawDay=function(){HOME.drawn=HOME.data.date;setArrows();}",context);
const day=(date,previous,next)=>({ok:true,json:async()=>({date,previous,next})});
const tick=()=>new Promise(r=>setTimeout(r,0));
const out={};const run=c=>vm.runInContext(c,context);
(async()=>{
 run("loadDay(null,false)");await tick();waiting.shift().resolve({ok:false});await tick();
 out.firstFails=run("el('home-chart-read').textContent");                                   // no chart at all: say so, even for the quiet load
 run("loadDay(null,false)");await tick();waiting.shift().resolve(day("2026-11-20","2026-11-19",null));await tick();
 out.start=run("HOME.drawn");out.afterRecovery=run("el('home-chart-read').textContent");
 run("el('home-chart-read').textContent='02:55: 2.50 kW in'");                           // the person is reading a value...
 run("loadDay(null,false)");await tick();waiting.shift().resolve(day("2026-11-20","2026-11-19",null));await tick();
 out.readingKept=run("el('home-chart-read').textContent");                                  // ...and today's five-minute refresh leaves it alone
 run("loadDay('2026-11-19',true)");out.heldWhileLoading=run("el('home-prev').disabled&&el('home-next').disabled");
 // an older answer arriving after a newer request is dropped
 run("loadDay('2026-11-18',true)");await tick();
 const first=waiting.shift(),second=waiting.shift();
 second.resolve(day("2026-11-18","2026-11-17","2026-11-19"));await tick();
 first.resolve(day("2026-11-19","2026-11-18","2026-11-20"));await tick();
 out.newestKept=run("HOME.drawn");out.freeAfter=run("!el('home-prev').disabled");
 // a failed day says so, keeps the arrows free, and a later success clears the message
 run("loadDay('2026-11-17',true)");await tick();waiting.shift().resolve({ok:false});await tick();
 out.failMessage=run("el('home-chart-read').textContent");out.freeAfterFail=run("!el('home-prev').disabled");
 run("loadDay('2026-11-17',true)");await tick();waiting.shift().resolve(day("2026-11-17","2026-11-16","2026-11-18"));await tick();
 out.cleared=run("el('home-chart-read').textContent");
 // the five-minute refresh waits while a request is in flight
 run("loadDay('2026-11-16',true)");
 out.pendingSet=run("HOME.pending>0");
 console.log(JSON.stringify(out));
})();
"""


def test_the_day_arrows_ignore_stale_answers_hold_while_loading_and_report_failure():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    run = subprocess.run(["node", "-e", ARROWS, str(STATIC / "home.js")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    seen = json.loads(run.stdout)
    assert "could not be loaded" in seen["firstFails"] and seen["afterRecovery"] == "Tap the chart to read a value."
    assert seen["readingKept"] == "02:55: 2.50 kW in"
    assert seen["start"] == "2026-11-20"
    assert seen["heldWhileLoading"] is True                  # no second tap while a day is loading
    assert seen["newestKept"] == "2026-11-18"                # the slower, older answer did not overwrite the newest request
    assert seen["freeAfter"] is True
    assert "could not be loaded" in seen["failMessage"] and "Tap the arrow to try again" in seen["failMessage"]
    assert seen["freeAfterFail"] is True                     # a failure never strands the arrows
    assert seen["cleared"] == "Tap the chart to read a value."
    assert seen["pendingSet"] is True


def test_the_tonight_card_says_there_is_no_plan_and_why_instead_of_guessing():
    state = status()
    state["shadow_plan"] = {"status": "unavailable", "grid_target": None, "next_window": None, "reason": "Household forecast unavailable"}
    shown = home(state)
    assert shown["home-tonight-main"] == "No plan yet" and "Household forecast unavailable" in shown["home-tonight-detail"]


def test_the_charging_advice_is_shown_once_on_the_home_page_and_not_repeated_in_the_settings_screens():
    """Owner, 20 November 2026: the advice appeared several times. The Tonight card is the one place; the old Charging advice panel, the hidden duplicate
    cards and the repeated target box in 'Next 48 hours' are gone, and so is the work of building them."""
    html = (TEMPLATES / "dashboard.html").read_text()
    settings = (TEMPLATES / "settings.html").read_text()
    script = (STATIC / "dashboard.js").read_text()
    assert html.count('id="home-tonight-main"') == 1
    assert not any(word in html + settings for word in ("Charging advice", "shadow-panel", "charge-policy", "plan-status", "legacy-hidden"))
    assert not any(word in script for word in ("renderTonight(", "renderSavings(", "drawShadowChart", "charge-policy", "shadow-refresh"))


DRAW = r"""
const fs=require("fs"),vm=require("vm");
const ids={};const element=()=>({textContent:"",attrs:{},disabled:false,setAttribute(k,v){this.attrs[k]=v;},replaceChildren(){},append(){},getBoundingClientRect(){return {left:0,width:340};}});
const document={getElementById(id){return ids[id]||(ids[id]=element());},createElementNS(){return element();},querySelectorAll(){return [];}};
const context={document,Math,Number,Date,String,JSON,console,el:id=>document.getElementById(id)};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
const day=(next,out)=>({date:"2026-11-20",start:1795132800,end:1795219200,step_s:21600,next,previous:"2026-11-18",cheap:[],
 series:{battery_kw:[1,-1,0.5,null],house_kw:[1,1,1,null],solar_kw:[0,1,0,null],grid_kw:[1,0,0,null],soc:[0.5,0.6,0.5,null]},
 totals:{battery_out_kwh:out,battery_in_kwh:1,house_kwh:2,solar_kwh:1,grid_import_kwh:1,grid_export_kwh:0}});
const run=c=>vm.runInContext(c,context),out={};
context.input=day(null,3.0);run("HOME.data=input;drawDay()");out.today=ids["home-used"].textContent;
context.input=day("2026-11-20",8.0);run("HOME.data=input;drawDay()");out.afterViewingYesterday=ids["home-used"].textContent;out.chartTotal=ids["home-day-total"].textContent;
console.log(JSON.stringify(out));
"""


def test_battery_used_today_stays_today_while_the_arrows_show_another_day():
    """Review, 20 November 2026: the money card's "Battery used today" followed the day being viewed (8.0 kWh under 'today' for 19 November)."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    run = subprocess.run(["node", "-e", DRAW, str(STATIC / "home.js")], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    seen = json.loads(run.stdout)
    assert seen["today"] == "3.0 kWh" and seen["afterViewingYesterday"] == "3.0 kWh" and seen["chartTotal"].startswith("8.0 kWh out")


def test_the_next_48_hours_chart_is_gone_and_the_status_no_longer_builds_its_data(tmp_path):
    """Owner, 20 November 2026: "remove the Next 48 hours chart". The screen keeps its two figures; the page has no chart or chart code, and the status
    neither reads the last 24 hours of power from the telemetry files nor sends the forecast's half-hour series (the daily record reads that itself)."""
    from fastapi.testclient import TestClient
    from solarbank_optimiser.models import FakeClock
    from solarbank_optimiser.store import Store
    from solarbank_optimiser.web.app import WebConfig, create_app
    html = (TEMPLATES / "dashboard.html").read_text()
    screen = html[html.index('id="forecast"'):html.index("</section>", html.index('id="forecast"'))]
    assert 'id="forecast-demand"' in screen and 'id="forecast-solar"' in screen and "<svg" not in screen and "power-chart" not in html
    assert not any(word in (STATIC / "dashboard.js").read_text() for word in ("drawPowerChart", "power_history", "power-chart"))
    now = 1_795_150_000.0
    store = Store(tmp_path / "s.sqlite3"); store.initialise()
    store.set_setting("forecast_summary", {"issued_at": now - 60, "demand_revision": 0, "demand_kwh": 12.0, "pv_kwh": 3.0,
                                           "series": [{"time": now + 1800 * k, "demand_kw": 0.5, "pv_kw": 0.1} for k in range(96)]}, now - 60, 0)
    app = create_app(WebConfig(tmp_path / "s.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0), FakeClock(now), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        status = client.get("/api/status").json()
    assert "power_history" not in status and "series" not in status["forecast"] and status["forecast"]["demand_kwh"] == 12.0
    assert store.get_setting("forecast_summary")[1]["series"]                                  # the stored forecast keeps it for the daily record


def test_the_household_card_has_its_own_answer_line_and_a_short_placeholder():
    html = (TEMPLATES / "dashboard.html").read_text()
    card = html[html.index('class="home-card home-modes"'):html.index('id="home-tonight"')]
    assert 'id="household-note"' in card and 'aria-live="polite"' in card and 'placeholder="For example 350"' in card
