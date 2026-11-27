"""The dashboard reports on the controller itself: its own heartbeat, mode, decision, restrictions, plan and command outcomes."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.realtime_status import NOT_MONITORED, assess, assess_controller
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app
from test_control_loop import Clock, MAXW, UTC, build, live_with, plan
from test_realtime_indicator import NOW, healthy

STATIC = Path(__file__).resolve().parents[1] / "src/solarbank_optimiser/web/static"


def state_file(tmp_path, steps=3, **extra):
    """A genuine status file written by a genuine controller cycle."""
    clock = Clock(); (tmp_path / "plan.json").write_text(json.dumps(plan()))
    lp = build(tmp_path, clock, live_with(clock), **extra)
    for _ in range(steps):
        lp.cycle(); clock.t += 1
    return json.loads((tmp_path / "out.json").read_text()), lp


def test_a_running_shadow_controller_is_reported_with_everything_the_owner_asked_for(tmp_path):
    state, lp = state_file(tmp_path)
    ctl = assess_controller(state, state["at"] + .4)
    assert ctl["state"] == "running" and ctl["level"] == "safe" and ctl["mode"] == "shadow" and ctl["sends_commands"] is False
    assert ctl["heartbeat_age_s"] == .4 and ctl["cycle"] == 3 and ctl["run_id"] == lp.run_id
    assert ctl["decision"]["discharge"] in ("INHIBIT", "ALLOW") and ctl["restrictions"] and ctl["plan"]["valid"] is True
    assert ctl["commands"] and all(c["stage"] == "suppressed" and c["simulated"] is False for c in ctl["commands"])


@pytest.mark.parametrize("age,expected_state,shadow_level,live_level", [
    (.4, "running", "safe", "safe"), (3, "slow", "watch", "watch"), (5.1, "stale", "watch", "unsafe"), (3600, "stale", "watch", "unsafe"),
    (-0.5, "running", "safe", "safe"), (-30, "clock", "unsafe", "unsafe")])
def test_a_stopped_or_stalled_controller_goes_stale_and_a_future_dated_one_is_never_safe(tmp_path, age, expected_state, shadow_level, live_level):
    state, _ = state_file(tmp_path)
    shadow = assess_controller(state, state["at"] + age)
    assert shadow["state"] == expected_state and shadow["level"] == shadow_level
    live = json.loads(json.dumps(state)); live["execution"]["mode"] = "live"
    assert assess_controller(live, state["at"] + age)["level"] == live_level


def test_a_missing_controller_is_reported_not_ignored():
    for state in (None, {}, {"at": "yesterday"}, {"at": None}):
        ctl = assess_controller(state, NOW)
        assert ctl["present"] is False and ctl["state"] == "missing" and ctl["level"] == "watch" and "not running" in ctl["reasons"][0]["text"]


def test_controller_stopped_while_the_collector_is_healthy_changes_the_overall_status_but_not_the_collector_checks(tmp_path):
    state, _ = state_file(tmp_path)
    alone = assess(healthy(), NOW, NOW - .4)                                                  # not monitored: as before
    assert alone["level"] == "safe" and alone["controller"] is None
    running = assess(healthy(), NOW, NOW - .4, controller={**state, "at": NOW - .3})
    assert running["level"] == "safe" and running["controller"]["state"] == "running"
    stopped = assess(healthy(), NOW, NOW - .4, controller={**state, "at": NOW - 120})
    assert stopped["level"] == "watch" and stopped["controller"]["state"] == "stale"
    assert [d for d in stopped["devices"]] == [d for d in running["devices"]]                  # collection still looks healthy
    assert any("controller has not reported" in r["text"] for r in stopped["reasons"])
    gone = assess(healthy(), NOW, NOW - .4, controller=None)
    assert gone["level"] == "watch" and gone["controller"]["state"] == "missing"


def test_requested_live_that_is_not_available_is_shown(tmp_path):
    state, _ = state_file(tmp_path, requested="live")
    ctl = assess_controller(state, state["at"] + .1)
    assert ctl["mode"] == "shadow" and ctl["requested"] == "live" and ctl["level"] == "watch" and "staying in shadow" in ctl["reasons"][0]["text"]


def test_a_slow_cycle_and_unconfirmed_live_commands_are_flagged(tmp_path):
    state, _ = state_file(tmp_path)
    state["timing_ms"]["previous_total"] = 4200.0
    assert any("took 4.2 s" in r["text"] for r in assess_controller(state, state["at"] + .1)["reasons"])
    live = json.loads(json.dumps(state)); live["execution"]["mode"] = "live"
    live["commands"]["ac_charge"].update(stage="timeout", priority="protective", action="STOP", simulated=True)
    ctl = assess_controller(live, live["at"] + .1)
    assert ctl["level"] == "unsafe" and any("unverified control state" in r["text"] for r in ctl["reasons"])
    assert ctl["commands"][0]["simulated"] is True                                             # simulated results stay labelled


# ---- through the website ---------------------------------------------------------------------------------------------------------------
def site(tmp_path, control_path):
    store = Store(tmp_path / "w.sqlite3"); store.initialise()
    clock = FakeClock(UTC)
    app = create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0,
                               control_state_path=control_path), clock, store=store)
    return TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)), clock


def test_the_status_page_follows_the_controllers_own_heartbeat(tmp_path):
    state, _ = state_file(tmp_path)
    path = tmp_path / "out.json"
    client, clock = site(tmp_path, path)
    with client:
        clock.wall = state["at"] + .3
        ctl = client.get("/api/status").json()["realtime"]["controller"]
        assert ctl["state"] == "running" and ctl["mode"] == "shadow"
        clock.wall = state["at"] + 60                                                              # the controller stopped; nothing else did
        stale = client.get("/api/status").json()["realtime"]
        assert stale["controller"]["state"] == "stale" and stale["level"] != "safe"
        path.unlink()
        assert client.get("/api/status").json()["realtime"]["controller"]["state"] == "missing"
        for junk in (b"\xff\xfe", b"[1,2,3]", b"{" * 5000, b"x" * 400000):
            path.write_bytes(junk)
            assert client.get("/api/status").json()["realtime"]["controller"]["state"] == "missing"


def test_a_site_without_a_controller_path_says_nothing_about_a_controller(tmp_path):
    client, _ = site(tmp_path, None)
    with client:
        assert client.get("/api/status").json()["realtime"]["controller"] is None


def test_the_dashboard_page_has_the_controller_panel(tmp_path):
    client, _ = site(tmp_path, None)
    with client:
        html = client.get("/settings").text          # the controller panel is on the Real-time diagnostics screen (20 November 2026)
    for element in ('id="controller"', 'id="ctl-state"', 'id="ctl-heartbeat"', 'id="ctl-decision"', 'id="ctl-restrictions"', 'id="ctl-plan"', 'id="ctl-commands"'):
        assert element in html, element


SCRIPT = r"""
const fs=require("fs"),vm=require("vm");
const ids={};
function element(){return {textContent:"",hidden:false,className:"",dataset:{},children:[],classList:{toggle(){}},
  setAttribute(){},addEventListener(){},querySelector(){return element();},replaceChildren(){this.children=[];},appendChild(c){this.children.push(c);}};}
const document={getElementById(id){return ids[id]||(ids[id]=element());},createElement(){return element();},querySelectorAll(){return [];}};
const context={document,console,Math,Number,Date,Object,String,message(){},action:f=>f,save:async()=>{},londonTime:()=>"",startPolling(){},paintRealtime(){},
 el:id=>document.getElementById(id),RT_ICON:{safe:"●",watch:"▲",unsafe:"■",unknown:"●"},RT_WORD:{safe:"SAFE",watch:"WATCH",unsafe:"NOT SAFE"}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
const input=JSON.parse(process.argv[2]);
vm.runInContext("renderController("+JSON.stringify(input)+")",context);
const rows=ids["ctl-commands"] ? ids["ctl-commands"].querySelector("tbody") : null;
const t=id=>ids[id] ? ids[id].textContent : null;
console.log(JSON.stringify({hidden:ids["controller"].hidden,state:t("ctl-state"),mode:t("ctl-mode"),beat:t("ctl-heartbeat"),decision:t("ctl-decision"),
 restrictions:t("ctl-restrictions"),plan:t("ctl-plan"),cycle:t("ctl-cycle"),why:t("ctl-reasons")}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_script_shows_suppressed_stale_and_missing_honestly(tmp_path):
    state, _ = state_file(tmp_path)
    ctl = assess_controller(state, state["at"] + .4)
    def render(value):
        out = subprocess.run(["node", "-e", SCRIPT, str(STATIC / "diagnostics.js"), json.dumps(value)], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    shown = render(ctl)
    assert shown["hidden"] is False and "Running" in shown["state"] and "none is sent" in shown["mode"] and shown["beat"] == "0.4 s ago"
    assert shown["decision"].startswith("Would be: ") and "mode" in shown["decision"] and shown["restrictions"] != "none" and shown["plan"].startswith("Valid") and shown["cycle"].startswith("#3")
    stale = render(assess_controller(state, state["at"] + 60))
    assert "Stale" in stale["state"]
    gone = render(assess_controller(None, NOW))
    assert "Not running" in gone["state"] and gone["beat"] == "—"
    assert render(None)["hidden"] is True


# ---- review 3 (28 Oct): the page clock, hostile status files, hand-over display, the planner's plan file ----------------------------------
class StepClock:
    """A clock that moves on each time it is read, like a page that takes seconds to build."""
    def __init__(self, start, step): self.t, self.step = start, step
    def utc(self):
        value = self.t; self.t += self.step; return value
    def monotonic(self): return 0.0


def test_a_controller_file_written_while_the_page_was_being_built_is_not_called_a_clock_problem(tmp_path):
    state, _ = state_file(tmp_path)
    clock = StepClock(state["at"] - 2.5, 2.5)                   # the page notes the time, then spends a few seconds before reading the file
    store = Store(tmp_path / "w.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0,
                               control_state_path=tmp_path / "out.json"), clock, store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        controller = client.get("/api/status").json()["realtime"]["controller"]
    assert controller["state"] in ("running", "slow", "stale") and controller["state"] != "clock" and controller["heartbeat_age_s"] >= 0


HOSTILE = [{"at": 1.0, "execution": []}, {"at": 1.0, "execution": {"mode": ["x"]}, "commands": [1, 2]}, {"at": 1.0, "commands": {"a": "x", "b": [1]}},
           {"at": 1.0, "decision": {"restrictions": "garage", "reasons": [1, {"a": 1}]}, "plan": 5, "timing_ms": "fast"},
           {"at": 1.0, "commands": {"ac_charge": {"stage": {"x": 1}, "params": {"watts": "lots"}, "action": 7}}},
           {"at": 1e300}, {"at": float("nan")}, {"at": True}, {"at": 1.0, "cycle": "ten", "late_cycles": [], "period_s": "slow"},
           {"at": 1.0, "execution": {"mode": "x" * 100000, "note": "y" * 100000}}, {"at": 1.0, "commands": {str(i): {} for i in range(500)}}]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_malformed_status_files_cannot_break_the_assessment(hostile):
    ctl = assess_controller(hostile, 5.0)
    assert ctl["state"] in ("missing", "running", "slow", "stale", "clock") and ctl["level"] in ("safe", "watch", "unsafe")
    json.dumps(ctl)                                                                           # and it is always something the page can send
    assert len(json.dumps(ctl)) < 20000


def test_a_hostile_status_file_through_the_website_still_gives_a_page(tmp_path):
    path = tmp_path / "out.json"
    client, clock = site(tmp_path, path)
    with client:
        for hostile in HOSTILE:
            path.write_text(json.dumps(hostile, allow_nan=True))
            assert client.get("/api/status").status_code == 200


def test_a_handover_is_shown_as_requested_until_it_is_confirmed(tmp_path):
    state, _ = state_file(tmp_path)
    for ownership, text in (("handover_pending", "not confirmed"), ("anker", "confirmed")):
        s = json.loads(json.dumps(state)); s["execution"]["ownership"] = ownership
        ctl = assess_controller(s, s["at"] + .1)
        assert ctl["ownership"] == ownership and any(text in r["text"] for r in ctl["reasons"])
    assert assess_controller(state, state["at"] + .1)["ownership"] == "controller"


def test_the_planner_publishes_the_file_the_controller_reads(tmp_path):
    """The actual publisher feeding the actual reader: not two schemas that happen to look alike."""
    from solarbank_optimiser.control_logic import read_plan
    from solarbank_optimiser.shadow import controller_plan_document, publish_controller_plan
    store = Store(tmp_path / "p.sqlite3"); store.initialise()
    now = UTC
    summary = {"issued_at": now - 5, "valid_until": now + 240, "grid_target": .85, "issuance_id": "abc",
               "next_window": {"start": now - 600, "end": now + 3600, "target": .85}}
    path = tmp_path / "plan.json"
    publish_controller_plan(path, store, summary, now)
    plan, why = read_plan(path, now)
    assert why is None and plan["action"] == "charge_to" and plan["target_soc"] == .85 and plan["mode"] == "shadow"
    assert plan["valid_until"] == now + 240 and not list(tmp_path.glob("*.tmp"))
    # before the window opens the file says "normal" but carries the window, and the controller acts at its exact start without waiting for a new file
    early = {**summary, "next_window": {"start": now + 100, "end": now + 3700, "target": .85}}
    path.unlink()                                                                              # no window under way (issue 85 keeps one that is)
    publish_controller_plan(path, store, early, now)
    plan, why = read_plan(path, now)
    assert why is None and plan["action"] == "normal" and plan["target_soc"] is None and plan["valid_until"] == now + 240        # not cut short at the window's start
    assert plan["window"] == {"start": now + 100, "end": now + 3700, "target_soc": .85}
    assert read_plan(path, now + 99)[0]["action"] == "normal"
    assert read_plan(path, now + 100)[0]["action"] == "charge_to" and read_plan(path, now + 100)[0]["target_soc"] == .85
    assert read_plan(path, now + 239)[0]["action"] == "charge_to" and read_plan(path, now + 240)[0] is None                     # the plan itself still expires on time
    # a hand-over request reaches the controller
    store.set_setting("operating_state", {"mode": "handover_pending"}, now, 0)
    publish_controller_plan(path, store, summary, now)
    assert read_plan(path, now)[0]["mode"] == "anker_manual"
    # no plan, no file: the controller has no authority
    store.set_setting("operating_state", {"mode": "shadow"}, now + 1, 1)
    publish_controller_plan(path, store, None, now)                                           # no checked plan: an explicit "normal", not a deletion
    document, why = read_plan(path, now)
    assert why is None and document["mode"] == "shadow" and document["action"] == "normal" and document["target_soc"] is None
    publish_controller_plan(None, store, summary, now)                                         # no path configured: nothing happens


def test_the_status_note_says_live_only_when_the_controller_is_live(tmp_path):
    """Owner, 12 November 2026: the site said "not commissioned" while the controller was live."""
    state, _ = state_file(tmp_path)
    shadow = {**state, "at": NOW - .3, "execution": {**state["execution"], "mode": "shadow"}}
    live = {**state, "at": NOW - .3, "execution": {**state["execution"], "mode": "live"}}
    assert any("not commissioned" in n for n in assess(healthy(), NOW, NOW - .4, controller=shadow)["notes"])
    notes = assess(healthy(), NOW, NOW - .4, controller=live)["notes"]
    assert any(n.startswith("Live:") for n in notes) and not any("not commissioned" in n for n in notes)


def test_a_window_under_way_keeps_its_target_and_is_not_dropped_by_a_replan(tmp_path):
    """GitHub issue 85 (12 November 2026, 14:39 to 14:47): replans in a daytime slot tipped the target between 30 % and 10 %, so the battery
    started and stopped. Once a window has started, a replan may raise its target, never lower it, and may not drop the window before it ends."""
    from solarbank_optimiser.control_logic import read_plan
    from solarbank_optimiser.shadow import publish_controller_plan
    store = Store(tmp_path / "p.sqlite3"); store.initialise()
    now, path = UTC, tmp_path / "plan.json"
    def plan(target, start=now - 600, end=now + 1200, at=now):
        return {"issued_at": at - 5, "valid_until": at + 240, "grid_target": target, "issuance_id": "x", "next_window": {"start": start, "end": end, "target": target}}
    publish_controller_plan(path, store, plan(.30), now)
    publish_controller_plan(path, store, plan(.10, at=now + 60), now + 60)                    # a replan says 10 %: the 30 % stands
    assert read_plan(path, now + 60)[0]["target_soc"] == .30
    publish_controller_plan(path, store, plan(.40, at=now + 120), now + 120)                  # a higher target is taken
    assert read_plan(path, now + 120)[0]["target_soc"] == .40
    night = plan(1.0, start=now + 30000, end=now + 50000, at=now + 180)                       # the replan moves on to the night window
    publish_controller_plan(path, store, night, now + 180)
    kept = read_plan(path, now + 180)[0]
    assert kept["action"] == "charge_to" and kept["target_soc"] == .40 and kept["window"]["end"] == now + 1200
    publish_controller_plan(path, store, {**night, "issued_at": now + 1195, "valid_until": now + 1440}, now + 1200)   # the slot has ended: the night window is published
    after = read_plan(path, now + 1200)[0]
    assert after["action"] == "normal" and after["window"]["start"] == now + 30000 and after["window"]["target_soc"] == 1.0

