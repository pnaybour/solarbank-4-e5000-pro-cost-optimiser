"""Independent shadow worker, immutable input assembly and guarded publication.

This module has no device/provider transport and no equipment writer. Calculated
advice remains provisional until actual capabilities and commissioning are verified.
"""
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import os
import subprocess
import sys

from .collector import PROVENANCE
from .minute_source import demand_rows
from .control import SingleOwner
from .energy import LONDON
from .forecasts import demand_horizon
from .household_policy import household_policy
from .models import Clock, ForecastInterval, Observation, Plan, PriceInterval, encoded, fingerprint, number
from .planning import BatteryModel, Scenario, validate_replay, replay
from .services import WEATHER_FRESH_S, WEATHER_MAX_AGE_S, ProviderConfig, cache_identity, tariff_horizon, window_cache

RELEVANT = ("demand", "fallback_tariff", "providers", "cache_weather", "cache_prices", "cache_slots",
            "established_slots", "provider_prices", "operating_state", "device_capabilities", "planner_model", "manual_override", "automation_pause", "min_soc", "calibration")


class PlanningUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ShadowConfig:
    horizon_hours: int = 48
    step_minutes: int = 30
    ttl_seconds: int = 300
    timeout_seconds: float = 20

    def __post_init__(self):
        if not 36 <= number(self.horizon_hours) <= 48 or not 1 <= number(self.step_minutes) <= 30:
            raise ValueError("Use a 36–48 hour horizon with at most half-hour steps")
        number(self.ttl_seconds,"plan life",1,300)
        number(self.timeout_seconds,"solver deadline",.1,60)


def newest_soc(store, committed=None):
    """The battery level the plan starts from: the collector's live reading, or the last valid one in the telemetry files (the database holds no readings).
    `committed` is a reading the caller already has; the newer of the two is returned."""
    for provenance in (PROVENANCE, "live"):
        live = store.latest_observations(provenance).get("soc")
        if live and (committed is None or live["retrieved_at"] > committed["retrieved_at"]):
            committed = live
    return committed


def snapshot(store, now):
    """One SQLite read snapshot. No network and no long-running writer transaction."""
    with closing(store.connect()) as db:
        db.execute("BEGIN")
        settings = {r["name"]: (r["revision"], json.loads(r["payload"])) for r in db.execute("SELECT * FROM settings") if r["name"] in RELEVANT}
        store.with_ram_settings(settings)      # working records held in RAM are newer than the database copy
        settings = {k: v for k, v in settings.items() if k in RELEVANT}
    rows = demand_rows(store, store.minute_source(), now-8*86400, now)          # complete, unbroken Normal-or-tagged minutes from the telemetry files
    return {"settings":settings,"soc":newest_soc(store,None),"demand_rows":rows}


def implementation():
    return {name:hashlib.sha256(Path(__file__).with_name(name+".py").read_bytes()).hexdigest()
            for name in ("planning","forecasts","household_policy","shadow_solver","shadow","emhass_child","emhass_adapter")}


LOAD_SKEW_S = 15.0     # the live reading may be stamped this long after the planner noted the time, while it was loading its inputs


SOC_BUCKET=0.05          # a plan is not withdrawn for a 1 % tick of the battery level, only when it moves to another 5 % band
REPLAN_MARGIN_S=30       # replan this long before the published plan expires
REPLAN_SECONDS=240       # and it is refreshed at least this often (plans live 5 minutes)


# Settings the providers rewrite every minute with the same content. Their revision numbers change constantly, so the planner judges them by what they SAY.
VOLATILE={"cache_slots":lambda p:(p or {}).get("intervals",[]),"established_slots":lambda p:(p or {}).get("intervals",[]),"provider_prices":lambda p:(p or {}).get("status")}


def setting_marker(data,name):
    revision,payload=data["settings"].get(name,(0,None))
    return VOLATILE[name](payload) if name in VOLATILE else revision


def reference_marker(rows):
    """Which London days have almost complete minute coverage. It changes about once a day, not every minute as the raw list of rows does."""
    counts={}
    for r in rows:
        t=r.get("source_at") or r.get("retrieved_at")
        if isinstance(t,(int,float)):
            day=datetime.fromtimestamp(t,LONDON).date().isoformat();counts[day]=counts.get(day,0)+1
    return sorted(d for d,c in counts.items() if c>=1430)


def guard(data, now, band=True):
    """What a plan was made from. `band=False` leaves out the battery's 5 % band: the 'core' that must still match for a plan to stay in force inside its own
    window (#1). The calendar date is NOT part of it: a plan crossing midnight is the same plan (the daily evidence is keyed to the window, not the date)."""
    row=data["soc"]
    state = {k:row.get(k) for k in ("source","metric","unit","quality","provenance")} if row else None
    if state is not None and band:
        value=row.get("value");state["band"]=round(value/SOC_BUCKET) if isinstance(value,(int,float)) and not isinstance(value,bool) else None
    return fingerprint({"implementation":implementation(),"revisions":{k:setting_marker(data,k) for k in RELEVANT},
                        "state":state,"soc_fresh":bool(row and Observation(**row).fresh(now,5,LOAD_SKEW_S)),"demand_evidence":fingerprint(reference_marker(data["demand_rows"]))})


def holds_in_window(plan,current,now):
    """A plan whose own charging window is open stays in force when only the battery's 5 % band moved (#1): charging raises the level through a band every few minutes,
    and withdrawing the plan each time left the controller without a target. Anything else that changed (settings, tariff, demand evidence, a stale reading) still withdraws it."""
    window=plan.get("next_window") or {}
    start,end=window.get("start"),window.get("end")
    return (None not in (start,end) and start<=now<end and plan.get("guard_core") is not None
            and guard(current,now,band=False)==plan["guard_core"])


def state_ready(data, now):
    row=data["soc"]
    if row is None or not Observation(**row).fresh(now,5,LOAD_SKEW_S):
        raise PlanningUnavailable("A fresh battery SOC reading is needed")


from .tariff_window import effective_tariff
from .battery_limits import DEFAULT_MIN_SOC, valid_min_soc
from .freshness import stored as stored_freshness


def stored_min_soc(payload):
    """The configured minimum level from a stored setting payload, or the 10 % default if it is missing or invalid."""
    return valid_min_soc((payload or {}).get("value")) or DEFAULT_MIN_SOC


def setting(data, name, default=None):
    return data["settings"].get(name,(0,default))


def weighted(rows, left, right, key):
    pieces=[]
    for r in rows:
        begin=max(left,r["start"]);end=min(right,r["end"])
        if end>begin:pieces.append((begin,end,r[key]))
    pieces.sort()
    cursor=left;total=0.
    for begin,end,value in pieces:
        if abs(begin-cursor)>1e-6:
            raise PlanningUnavailable("Forecast interval coverage is incomplete or overlapping")
        total+=(end-begin)*number(value,key,0)
        cursor=end
    if abs(cursor-right)>1e-6:
        raise PlanningUnavailable("Forecast interval coverage is incomplete or overlapping")
    return total/(right-left)


def assemble(store, data, now, config, calibration_floor=True):
    state_ready(data,now)
    _, operating=setting(data,"operating_state",{"mode":"observe"})
    if operating["mode"] not in {"observe","shadow"}:
        raise PlanningUnavailable("Planning paused by the operator; Anker handover or fault takes priority")
    from .automation import override_state
    override=override_state(setting(data,"manual_override")[1],now,stored_min_soc(setting(data,"min_soc",{})[1]))
    revision,selection=setting(data,"demand")
    end=now+config.horizon_hours*3600
    try:
        from .demand_learning import planner_inputs, planner_level
        learned_selection,average_days,learning_note=planner_inputs(store,selection)
        demand=demand_horizon(data["demand_rows"],learned_selection,revision,now,now,end,average_days,level_kwh=planner_level(store),default_kwh=planner_level(store))
    except ValueError:
        raise PlanningUnavailable("Household forecast unavailable: a recent complete Normal-use day with verified coverage and tags is required") from None
    _, raw_providers=setting(data,"providers",{})
    providers=ProviderConfig(**raw_providers)
    _, weather=setting(data,"cache_weather")
    solar_fallback=None
    if not providers.weather_enabled:solar_fallback="weather forecasts are switched off"
    elif not weather:solar_fallback="no weather forecast has been fetched yet"
    elif weather["identity"]!=cache_identity(providers,"weather"):solar_fallback="the saved forecast is for different weather settings"
    elif not 0<=now-weather["acquired_at"]<=WEATHER_MAX_AGE_S:solar_fallback=f"the weather forecast is {(now-weather['acquired_at'])/3600:.0f} hours old"
    if solar_fallback:
        # Issue #32: planning does not stop for want of a forecast. It assumes NO solar output, the pessimistic case: the battery is charged from the grid at the cheap rate
        # rather than being left short, and the plan says so. The next fresh forecast replaces it.
        first=int(now)//3600*3600
        solar=[{"start":float(t),"end":float(t+3600),"pv_kw":0.0} for t in range(first,math.ceil(end)+3600,3600)]
        weather={"issuance_id":None,"acquired_at":now}
    else:
        solar=weather["intervals"]
    # Price building reads local storage only. The publication guard rejects any
    # setting/cache changes that race this snapshot.
    price_data=tariff_horizon(store,now,now,now+72*3600)
    if not price_data["complete"]:
        raise PlanningUnavailable("Valid price coverage and a next-cheap ending value are needed")
    prices=price_data["intervals"]
    terminal=next((r["price"] for r in prices if r["cheap"] and r["end"]>end),None)
    if terminal is None:
        raise PlanningUnavailable("The next confirmed cheap price after the horizon is missing")
    _,tariff=setting(data,"fallback_tariff")
    tariff,_=effective_tariff(tariff,window_cache(store,now),setting(data,"providers",{})[1].get("product"))
    policy=household_policy(selection,now,tariff)
    baseline=household_policy({"mode":"High demand","start":now},now,tariff)
    _, capabilities=setting(data,"device_capabilities",{"status":"unverified"})
    _, model_data=setting(data,"planner_model",{})
    model=BatteryModel(**{**model_data,"reserve":stored_min_soc(setting(data,"min_soc",{})[1])})       # the owner's adjustable minimum level
    if data["soc"]["value"]<model.reserve:
        raise PlanningUnavailable("SOC is below the modelled reserve; recovery must be checked first")
    from .calibration import force_first_night,state as calibration_state,stored_days
    calibration=calibration_state(store,now,stored_days(setting(data,"calibration",{})[1]))
    calibration["forced"]=False;calibration["note"]=None
    if calibration["state"]=="due" and calibration_floor:
        policy,calibration["forced"],calibration["note"]=force_first_night(policy,baseline,data["soc"]["value"],model)
    elif calibration["state"]=="due":
        calibration["note"]="a full charge is due but could not be reached in the next cheap window, so it was planned without it"
    boundaries={now,end}
    step=config.step_minutes*60
    boundaries.update(range((int(now)//step+1)*step,math.ceil(end),step))
    for rows in (prices,solar):
        for r in rows:
            boundaries.update(t for t in (r["start"],r["end"]) if now<t<end)
    for p in (policy,baseline):
        for night in p["windows"]:
            for r in night["intervals"]:
                boundaries.update(t for t in (r["start"],r["end"]) if now<t<end)
    if selection:
        boundaries.update(t for t in (selection["start"],selection.get("end")) if t is not None and now<t<end)
    intervals=[];tariffs=[]
    times=sorted(boundaries)
    for left,right in zip(times,times[1:]):
        interval={"start":left,"end":right,"demand_kw":weighted(demand["intervals"],left,right,"demand_kw"),
                  "pv_kw":weighted(solar,left,right,"pv_kw")}
        middle=(left+right)/2
        matches=[r for r in prices if r["start"]<=middle<r["end"]]
        if len(matches)!=1:
            raise PlanningUnavailable("Price intervals overlap or have a gap")
        r=matches[0]
        intervals.append(interval)
        tariffs.append({"start":left,"end":right,"price":r["price"],"source":r["source"],"cheap":r["cheap"]})
    cases=[]
    for name,prob,load_scale,pv_scale in (("low solar / higher use",.25,1.15,.7),("central",.5,1.,1.),("higher solar / lower use",.25,.9,1.2)):
        cases.append({"name":name,"probability":prob,"intervals":[{**r,"demand_kw":r["demand_kw"]*load_scale,"pv_kw":r["pv_kw"]*pv_scale} for r in intervals]})
    sources=implementation()
    return {"version":"shadow-target-v1","implementation_sha256":sources,"decision_at":now,"horizon_end":end,"initial_soc":data["soc"]["value"],
            "initial_observation":data["soc"],"model":asdict(model),"scenarios":cases,"prices":tariffs,
            "targets":[override["target"]] if override else [round(i/100,2) for i in range(5,101,5) if i/100>=model.reserve-1e-9],       # nothing below the minimum level; an owner override leaves one choice
            "terminal_price":terminal,
            "charge_policy":policy,"baseline_policy":baseline,"demand_revision":revision,
            "normal_reference_day":demand["reference_day"],"weather_issuance_id":weather.get("issuance_id"),
            "capabilities":capabilities,"guard":guard(data,now),"guard_core":guard(data,now,band=False),"owner_override":override,"calibration":calibration,
            "assumptions":["Paired scenarios and probabilities are provisional, not measured quantiles",
                "PV potential and conversion boundaries are provisional; no validated DC/AC device model",
                "Future EV/current constraints are not commissioned; this is economic shadow analysis only",
                "No account credentials or assumed extra slots; each interval retains price provenance"]
                +([f"Weather forecast is {(now-weather['acquired_at'])/3600:.1f} hours old (the weather service is failing); solar estimates are less reliable"]
                  if not solar_fallback and now-weather["acquired_at"]>WEATHER_FRESH_S else [])
                +([f"No usable weather forecast ({solar_fallback}): the plan assumes no solar output, which charges more from the grid rather than risk running short"]
                  if solar_fallback else [])
                +([learning_note] if learning_note else [])
                +([f"Normal demand is the DEFAULT, {demand['reference_day']}: no complete Normal day exists yet, so the household forecast is a flat estimate, not measured use"]
                  if demand.get("default_used") else []),
            "solar_fallback":solar_fallback}


def hourly_series(flows,prices,capacity_kwh):
    from .plan_series import hourly
    try:return hourly(flows,prices,capacity_kwh)
    except (KeyError,TypeError,ValueError,ZeroDivisionError):return []         # a presentation failure never withdraws a checked plan


def cheap_window_targets(flows,prices):
    """Each cheap window in the horizon with the level the plan charges it to: [{start,end,target}], in time order."""
    out=[]
    for row,price in zip(flows,prices):
        if not price["cheap"]:continue
        if out and out[-1]["end"]==row["start"] and out[-1]["target"]==row["grid_target"]:out[-1]["end"]=row["end"]
        else:out.append({"start":row["start"],"end":row["end"],"target":row["grid_target"]})
    return out


def run_child(job,timeout,python=None,script=None):
    command=[str(python or sys.executable),str(script or Path(__file__).with_name("shadow_solver.py"))]
    try:
        result=subprocess.run(command,input=encoded(job).encode(),capture_output=True,timeout=timeout,check=False)
    except (OSError,subprocess.TimeoutExpired):
        raise PlanningUnavailable("Planner process failed or exceeded its deadline") from None
    if result.returncode or len(result.stdout)>4_000_000:
        raise PlanningUnavailable("Planner returned a failed or oversized result")
    try:
        return json.loads(result.stdout)
    except (ValueError,UnicodeError):
        raise PlanningUnavailable("Planner returned invalid JSON") from None


def validate_result(job,result):
    if result.get("status")=="infeasible" and result.get("inputs_fingerprint")==fingerprint(job) and result.get("controls_enabled") is False:
        raise PlanningUnavailable("Requested target is not reachable in the available cheap time")
    if (result.get("status")!="completed_grid" or result.get("inputs_fingerprint")!=fingerprint(job)
            or result.get("controls_enabled") is not False):
        raise PlanningUnavailable("Planner result is failed, mismatched or not shadow-only")
    model=BatteryModel(**job["model"])
    prices=[PriceInterval(**r) for r in job["prices"]]
    if len(result["best"]["scenarios"])!=len(job["scenarios"]) or len(result["baseline"])!=len(job["scenarios"]):
        raise PlanningUnavailable("Scenario coverage differs from inputs")
    target=result["best"]["target"]
    targets=result["best"].get("targets",[target])
    if not targets or targets[0]!=target or any(x not in job["targets"] for x in targets):
        raise PlanningUnavailable("Planner used an undeclared target")
    probability=sum(r["probability"] for r in job["scenarios"])
    if abs(probability-1)>1e-9:raise PlanningUnavailable("Invalid scenario probabilities")
    from .shadow_solver import feasible
    expected=0.;baseline=0.
    for scenario,plan,base in zip(job["scenarios"],result["best"]["scenarios"],result["baseline"]):
        forecasts=[ForecastInterval(**r) for r in scenario["intervals"]]
        validate_replay(model,plan,job["initial_soc"],job["terminal_price"],forecasts=forecasts,prices=prices,target=targets,charge_policy=job["charge_policy"])
        validate_replay(model,base,job["initial_soc"],job["terminal_price"],forecasts=forecasts,prices=prices,target=model.reserve,charge_policy=job["baseline_policy"])
        expected+=scenario["probability"]*plan["objective_gbp"]
        baseline+=scenario["probability"]*base["objective_gbp"]
    if abs(expected-result["best"]["expected_cost_gbp"])>1e-7 or abs(baseline-result["baseline_cost_gbp"])>1e-7:
        raise PlanningUnavailable("Expected cost failed independent reconciliation")
    costs=result["candidate_costs"]
    if [r["target"] for r in costs]!=job["targets"]:
        raise PlanningUnavailable("The declared target search did not complete")
    # Reconcile every candidate's claimed cost/feasibility, not only the winner.
    for candidate in costs:
        calculated=[]
        for scenario in job["scenarios"]:
            case=Scenario(scenario["probability"],tuple(ForecastInterval(**r) for r in scenario["intervals"]))
            calculated.append(replay(model,case,prices,job["initial_soc"],candidate["target"],job["terminal_price"],job["charge_policy"]))
        cost=sum(s["probability"]*r["objective_gbp"] for s,r in zip(job["scenarios"],calculated))
        valid_candidate=all(feasible(r,prices,model.capacity_kwh,job["horizon_end"],job["charge_policy"]) for r in calculated)
        if abs(cost-candidate["expected_cost_gbp"])>1e-7 or valid_candidate!=candidate["feasible"]:
            raise PlanningUnavailable("Candidate cost or reachability failed reconciliation")
    valid=[r for r in costs if r["feasible"]]
    if not valid:
        raise PlanningUnavailable("Planner did not select its declared least-cost feasible target")
    constant=min(valid,key=lambda r:(r["expected_cost_gbp"],r["target"]))
    if len(targets)==1:
        if constant["target"]!=target:raise PlanningUnavailable("Planner did not select its declared least-cost feasible target")
    else:
        # One level per cheap window: it must beat the best single level, be reachable, and no one-window change on the grid may improve it.
        if result["best"]["expected_cost_gbp"]>=constant["expected_cost_gbp"]-1e-12:
            raise PlanningUnavailable("Per-window targets do not improve on the best single target")
        from .shadow_solver import cheap_windows,evaluate
        cases=[Scenario(s["probability"],tuple(ForecastInterval(**r) for r in s["intervals"])) for s in job["scenarios"]]
        if len(targets)!=cheap_windows(prices):raise PlanningUnavailable("Planner used the wrong number of window targets")
        if not evaluate(model,cases,prices,job,targets)[1]:raise PlanningUnavailable("Per-window targets are not reachable")
        for index in range(len(targets)):
            for other in job["targets"]:
                trial=targets[:index]+[other]+targets[index+1:]
                trial_cost,trial_valid,_=evaluate(model,cases,prices,job,trial)
                if trial_valid and trial_cost<result["best"]["expected_cost_gbp"]-1e-9:
                    raise PlanningUnavailable("A single-window change would be cheaper than the published targets")
    for r in valid:number(r["expected_cost_gbp"],"candidate cost")
    expected_window=None
    central=result["best"]["scenarios"][1 if len(job["scenarios"])>1 else 0]["flows"]
    for row,price in zip(central,prices):
        if price.cheap:
            if expected_window is None:expected_window={"start":row["start"],"end":row["end"],"target":row["grid_target"]}
            elif expected_window["end"]==row["start"] and expected_window["target"]==row["grid_target"]:expected_window["end"]=row["end"]
            else:break
        elif expected_window is not None:break
    if result["next_window"]!=expected_window or result["next_target"]!=expected_window["target"]:
        raise PlanningUnavailable("Published next target differs from checked physical policy")
    expected_chart=[{"time":job["decision_at"],"soc":job["initial_soc"]}]+[{"time":r["end"],"soc":r["after_kwh"]/model.capacity_kwh} for r in central]
    if result["chart"]!=expected_chart:
        raise PlanningUnavailable("Chart differs from independently checked SOC")


PLAN_ARCHIVE_SECONDS=1800       # an unchanged plan is archived at most this often


def plan_key(summary):
    """What makes one plan different from the last for anyone acting on it: the planned window, the level it charges to, the per-window levels,
    and whether the owner's override or a calibration charge applies."""
    window=summary.get("next_window") or {}
    targets=tuple((w["start"],w["end"],round(w["target"],3)) for w in summary.get("window_targets") or ())
    calibration=summary.get("calibration") or {}
    return (window.get("start"),window.get("end"),round(summary.get("grid_target") or 0,3),targets,
            json.dumps(summary.get("owner_override"),sort_keys=True),calibration.get("forced"),calibration.get("state"))


def plan_unchanged(previous,summary,now):
    return bool(previous and previous.get("issuance_id") and 0<=now-previous.get("archived_at",-1e18)<PLAN_ARCHIVE_SECONDS
                and plan_key(previous)==plan_key(summary))


def publish(store,job,result,now,config):
    """Archive and active pointer commit together after a final state/revision guard."""
    validate_result(job,result)
    current=snapshot(store,now)
    state_ready(current,now)
    if guard(current,now)!=job["guard"] or not 0<=now-job["decision_at"]<=config.timeout_seconds+2:
        raise PlanningUnavailable("Inputs changed or the planning decision is too old; replan required")
    expires=min(now+config.ttl_seconds,result["next_window"]["end"])
    _,weather=setting(current,"cache_weather")
    if weather and not job.get("solar_fallback"):expires=min(expires,weather["acquired_at"]+WEATHER_MAX_AGE_S)
    for name,max_age in (("cache_prices",86400),("cache_slots",120)):
        _,cached=setting(current,name)
        # Stale slot data means "assume no extra slots" (specification 7.2), which is exactly what a plan with an empty slot list already assumes:
        # only a plan that USES a slot has to expire with the slot data.
        if name=="cache_slots" and not (cached or {}).get("intervals"):continue
        if cached and cached["acquired_at"]+max_age>now:expires=min(expires,cached["acquired_at"]+max_age)
    _,fallback=setting(current,"fallback_tariff")
    if fallback and fallback.get("valid_until") and fallback["valid_until"]>now:expires=min(expires,fallback["valid_until"])
    for row in (job["charge_policy"].get("windows",[])):
        for interval in row["intervals"]:
            expires=min(expires,interval["end"]) if now<interval["end"] else expires
    if job.get("owner_override"):expires=min(expires,job["owner_override"]["valid_until"])
    selection=setting(current,"demand")[1]
    if selection:
        for t in (selection["start"],selection.get("end")):
            if t is not None and now<t:expires=min(expires,t)
    if expires<=now:raise PlanningUnavailable("The charging opportunity or an input expired during planning")
    identity=fingerprint(job)
    plan=Plan(now,expires,identity,result["next_target"])
    summary={**asdict(plan),"plan":asdict(plan),"status":"checked_shadow","controls_enabled":False,"equipment_writes":False,
        "guard":job["guard"],"guard_core":job.get("guard_core"),"initial_soc":job["initial_soc"],"initial_observation_at":Observation(**job["initial_observation"]).sample_at,
        "horizon_start":job["decision_at"],"horizon_end":job["horizon_end"],"next_window":result["next_window"],
        "expected_cost_gbp":result["best"]["expected_cost_gbp"],"full_night_cost_gbp":result["baseline_cost_gbp"],
        "replay_difference_gbp":result["baseline_cost_gbp"]-result["best"]["expected_cost_gbp"],
        "capabilities_verified":job["capabilities"].get("status")=="verified",
        "reason":"Checked provisional shadow advice; no commands sent", "assumptions":job["assumptions"],
        "chart":result["chart"],"series":hourly_series(result["best"]["scenarios"][1 if len(result["best"]["scenarios"])>1 else 0]["flows"],job["prices"],job["model"]["capacity_kwh"]),"emhass":result.get("emhass",{"status":"not_run"}),
        "owner_override":job.get("owner_override"),"calibration":job.get("calibration"),"window_targets":cheap_window_targets(result["best"]["scenarios"][1 if len(result["best"]["scenarios"])>1 else 0]["flows"],job["prices"])}
    archive={"kind":"shadow_plan","inputs":job,"result":result,"summary":dict(summary)}
    raw=encoded(archive).encode()
    issuance=fingerprint({"acquired_at":now,"raw":hashlib.sha256(raw).hexdigest(),"model":archive})
    summary["issuance_id"]=issuance
    previous=store.get_setting("shadow_plan",{})
    if plan_unchanged(previous[1],summary,now):
        # Nothing the controller or the owner would notice has changed since the plan that was archived: keep that archive as the evidence and refresh only the
        # RAM copy (new validity), instead of writing a ~240 KB archive, a plan row and an event every few minutes.
        summary["issuance_id"]=previous[1]["issuance_id"];summary["archived_at"]=previous[1]["archived_at"]
        store.set_setting("shadow_plan",summary,now,previous[0])
        return summary
    summary["archived_at"]=now
    with closing(store.connect()) as db,db:
        db.execute("BEGIN IMMEDIATE")
        # Guard setting revisions again under the publication writer lock.
        latest={"settings":store.with_ram_settings({r["name"]:(r["revision"],json.loads(r["payload"])) for r in db.execute("SELECT name,revision,payload FROM settings") if r["name"] in RELEVANT})}
        latest["settings"]={k:v for k,v in latest["settings"].items() if k in RELEVANT}
        if any(setting_marker(latest,k)!=setting_marker(current,k) for k in RELEVANT):          # by content for the settings rewritten every minute
            raise PlanningUnavailable("Settings changed during publication")
        locked={**current,"soc":newest_soc(store,None)}
        locked["demand_rows"]=demand_rows(store,store.minute_source(),now-8*86400,now)
        state_ready(locked,now)
        if guard(locked,now)!=job["guard"]:
            raise PlanningUnavailable("Battery state changed during publication")
        # payload already holds these exact canonical bytes; keep only their digest in raw.
        db.execute("INSERT OR IGNORE INTO issuances VALUES(?,?,?,?)",(issuance,now,raw.decode(),hashlib.sha256(raw).hexdigest().encode()))
        db.execute("INSERT INTO settings VALUES('shadow_plan',1,?) ON CONFLICT(name) DO UPDATE SET revision=revision+1,payload=excluded.payload",(encoded(summary),))
        plan_revision=db.execute("SELECT revision FROM settings WHERE name='shadow_plan'").fetchone()[0]
        log_forecast(db,now,summary["chart"])
        db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",(now,"shadow_published",encoded({"issuance_id":issuance,"inputs_fingerprint":identity})))
    store.ram_sync("shadow_plan",plan_revision,summary,now)
    return summary


FORECAST_LOG_NAME="soc_forecast_log"
FORECAST_LOG_EVERY=3600
FORECAST_LOG_KEEP=36*3600
FORECAST_LOG_STEP=1800

def log_forecast(db,now,chart):
    """Keep what the planner expected the battery level to do, once an hour for 36 hours, so the dashboard can show it against what happened.
    Half-hour points only (about 4 KB an entry). Written inside the publication transaction, outside the settings that trigger a replan."""
    row=db.execute("SELECT payload FROM settings WHERE name=?",(FORECAST_LOG_NAME,)).fetchone()
    try:entries=json.loads(row[0])["entries"] if row else []
    except (ValueError,KeyError,TypeError):entries=[]
    entries=[e for e in entries if isinstance(e,dict) and now-FORECAST_LOG_KEEP<=e.get("issued_at",0)<=now]
    if entries and now-entries[-1]["issued_at"]<FORECAST_LOG_EVERY:return
    points,last=[],None
    for p in chart or ():
        if last is None or p["time"]-last>=FORECAST_LOG_STEP:points.append({"time":round(p["time"]),"soc":round(p["soc"],3)});last=p["time"]
    entries.append({"issued_at":now,"chart":points})
    db.execute("INSERT INTO settings VALUES(?,1,?) ON CONFLICT(name) DO UPDATE SET revision=revision+1,payload=excluded.payload",(FORECAST_LOG_NAME,encoded({"entries":entries})))


def plan_view(store,now):
    _,plan=store.get_setting("shadow_plan")
    _,attempt=store.get_setting("shadow_attempt")
    if not plan:return {"status":"unavailable","reason":attempt.get("reason") if attempt else "Shadow worker has not run", "last_attempt":attempt,"controls_enabled":False,"equipment_writes":False}
    reason=None
    if not plan["issued_at"]<=now<plan["valid_until"]:reason="Shadow plan expired; a fresh plan is needed"
    else:
        current=snapshot(store,now)
        try:state_ready(current,now)
        except ValueError:reason="Battery SOC is no longer fresh"
        if guard(current,now)!=plan["guard"] and not holds_in_window(plan,current,now):reason="Inputs changed; shadow plan withdrawn pending replan"
    actual=[]
    if not reason:
        for minute,item in sorted(store.minute_source().minutes("soc",plan["horizon_start"],now+1).items()):
            if item.mean is not None and minute<=now and minute+60<=now+60:actual.append({"time":minute,"soc":item.mean})
    return {**plan,"actual":actual,"status":"unavailable" if reason else "checked_shadow","reason":reason or plan["reason"],"last_attempt":attempt,
            "grid_target":None if reason else plan["grid_target"]}


def controller_plan_document(summary,now,operating_mode="shadow",min_soc=DEFAULT_MIN_SOC):
    """The small file the controller reads (control_logic.read_plan), derived from the checked advice. The mapping is provisional: the document
    carries the planned charging WINDOW (start, end, target) and the controller decides for itself, every second, whether it is inside it. That way
    charging begins at the exact start of the window instead of waiting up to a replan interval for a new file. 'action' says what applies right now."""
    window=summary.get("next_window") or {}
    start,end=window.get("start"),window.get("end")
    target=summary.get("grid_target")
    valid_until=summary["valid_until"]
    inside=None not in (start,end,target) and start<=now<end
    document={"mode":"anker_manual" if operating_mode in ("handover_pending","anker_manual") else "shadow",
              "action":"charge_to" if inside else "normal","target_soc":target if inside else None,"issued_at":summary["issued_at"],"valid_until":valid_until,
              "source":"shadow_plan","issuance_id":summary.get("issuance_id"),"min_soc":min_soc}
    if None not in (start,end,target):
        document["window"]={"start":start,"end":end,"target_soc":target}
    return document


PLAN_FILE_LIFE_S=120    # the planner re-publishes at least every minute; a file this old means the planner has stopped


def paused_by_owner(store,now):
    from .automation import PAUSE_SETTING,pause_state
    return pause_state(store.get_setting(PAUSE_SETTING,{})[1],now)[0]


def handover_requested(store):
    return store.get_setting("operating_state",{})[1].get("mode") in ("handover_pending","anker_manual")


def cheap_window(store,now):
    """The cheap period in force, {"start_minute","end_minute"} London, from the same source and validity test as the planner (WP17); None when unknown."""
    from .tariff_window import effective_tariff
    try:
        tariff,_=effective_tariff(store.get_setting("fallback_tariff")[1],window_cache(store,now),store.get_setting("providers",{})[1].get("product"))
    except Exception:
        return None
    if not isinstance(tariff,dict) or tariff.get("start_minute") is None or tariff.get("end_minute") is None:return None
    return {"start_minute":tariff["start_minute"],"end_minute":tariff["end_minute"]}


def checked_window_open(path,now):
    """True when the plan file holds a checked planner plan whose charging window has not yet ended. Anything unreadable is False."""
    try:
        document=json.loads(Path(path).read_text())
        window=document.get("window") or {}
        return (document.get("source")=="shadow_plan" and document.get("mode")=="shadow"
                and isinstance(window.get("end"),(int,float)) and now<window["end"])
    except (OSError,ValueError,AttributeError,TypeError):
        return False


def keep_window_under_way(path,document,now):
    """GitHub issue 85 (owner, 12 November 2026): once a charging window has started, a replan may raise its target but never lower it, and may not
    drop it, until it ends. Each replan solves afresh from the current level; when the window under way and a later one cost the same (a daytime
    smart slot and the night window, both 6.6p) the best target tipped between charging and not charging, and the controller started and stopped
    with it (12 November, 14:39 to 14:47). The worst case of keeping it is a little more charge at the cheap rate."""
    try:
        old=json.loads(Path(path).read_text())
        window=old.get("window") or {}
        start,end,target=window["start"],window["end"],float(window["target_soc"])
        if old.get("source")!="shadow_plan" or old.get("mode")!="shadow" or not start<=now<end:return document
    except (OSError,ValueError,AttributeError,TypeError,KeyError):
        return document
    new=document.get("window")
    if new and new["start"]<=now<new["end"]:
        if new["target_soc"]<target:
            document["window"]={**new,"target_soc":target}
    else:
        document["window"]={"start":start,"end":end,"target_soc":target}
    document["action"],document["target_soc"]="charge_to",document["window"]["target_soc"]
    return document


def publish_controller_plan(path,store,summary,now):
    """Write the one small file the controller obeys, atomically. Three cases, all explicit:
    * the owner asked for a hand-over to the Anker app: a document with mode 'anker_manual' (planning is paused, but the release must still reach the controller);
    * a checked plan exists: its charging authority (controller_plan_document);
    * no checked plan: a 'normal' document in shadow mode. That is an explicit statement that control is the controller's again, with
      no grid-charging request (the Anker runs the house as normal), and it is what ends a hand-over."""
    if path is None:return
    path=Path(path)
    try:
        floor=stored_min_soc(store.get_setting("min_soc",{})[1])
        if handover_requested(store):
            document={"mode":"anker_manual","action":"hold","target_soc":None,"issued_at":now,"valid_until":now+PLAN_FILE_LIFE_S,
                      "source":"handover_request","min_soc":floor}
        elif paused_by_owner(store,now):
            document={"mode":"shadow","action":"normal","target_soc":None,"issued_at":now,"valid_until":now+PLAN_FILE_LIFE_S,
                      "source":"owner_pause","min_soc":floor}
        elif summary is None:
            if checked_window_open(path,now):
                # Issue #31: a failed planning attempt is not an instruction. Leave the last checked plan to expire on its own, so the
                # controller's plan memory can carry its charging window through a planner outage (PLAN_HOLD_S), instead of being
                # told "normal" and dropping the window at once.
                return
            document={"mode":"shadow","action":"normal","target_soc":None,"issued_at":now,"valid_until":now+PLAN_FILE_LIFE_S,
                      "source":"no_checked_plan","min_soc":floor}
        else:
            document=keep_window_under_way(path,controller_plan_document(summary,now,"shadow",floor),now)
        document["freshness"]=stored_freshness(store.get_setting("freshness",{})[1])
        window=cheap_window(store,now)
        if window:document["offpeak"]=window            # the controller keeps the cheap period even when a plan goes missing (WP24)
        temporary=path.with_name(path.name+".tmp")
        temporary.write_text(json.dumps(document,separators=(",",":")))
        os.replace(temporary,path)
    except OSError:
        pass          # the plan file is how the controller hears from the planner; its absence leaves it restricted


class ShadowWorker:
    def __init__(self,store,config=None,clock=None,solver=None,emhass=None,controller_plan_path=None):
        self.store=store;self.config=config or ShadowConfig();self.clock=clock or Clock()
        self.solver=solver or (lambda job:run_child(job,self.config.timeout_seconds))
        self.emhass=emhass;self.controller_plan_path=controller_plan_path
        self._failure_logged=(None,-1e18)

    def _log_failure(self,reason,now):
        """Issue #31: say in the journal when planning fails, once per new reason and at most every 30 minutes while it repeats."""
        last_reason,last_at=self._failure_logged
        if reason!=last_reason or now-last_at>=1800:
            print(f"Planner: no plan published: {reason}",file=sys.stderr,flush=True)
            self._failure_logged=(reason,now)

    def once(self):
        now=self.clock.utc();started=self.clock.monotonic()
        try:
            data=snapshot(self.store,now)
            job=assemble(self.store,data,now,self.config)
            job["native_request"]="EMHASS reference (accepted versions 0.18.4/0.18.5)" if self.emhass else "not requested"
            result=self.solver(job)
            if result.get("status")=="infeasible" and (job.get("calibration") or {}).get("forced"):
                # The calibration charge could not be reached after all: plan without it rather than lose the plan, and try again at the next replan.
                job=assemble(self.store,data,now,self.config,calibration_floor=False)
                job["native_request"]="EMHASS reference (accepted versions 0.18.4/0.18.5)" if self.emhass else "not requested"
                result=self.solver(job)
            if self.emhass:
                result["emhass"]=self.emhass(job)
                if result["emhass"]["status"]=="optimal":
                    from .emhass_adapter import validate_native
                    result["emhass"]=validate_native(job,result["emhass"])
                elif result["emhass"]["status"]=="unsupported_grid":
                    rows=job["scenarios"][1]["intervals"]
                    first=rows[0]["end"]-rows[0]["start"]
                    if all(abs(r["end"]-r["start"]-first)<1e-7 for r in rows):
                        raise PlanningUnavailable("Native comparison unexpectedly skipped a regular grid")
                else:raise PlanningUnavailable("EMHASS comparison failed independent checks")
            plan=publish(self.store,job,result,self.clock.utc(),self.config)
            attempt={"status":"published","reason":plan["reason"],"issuance_id":plan["issuance_id"]}
            publish_controller_plan(self.controller_plan_path,self.store,plan,self.clock.utc())
        except Exception as exc:
            publish_controller_plan(self.controller_plan_path,self.store,None,self.clock.utc())      # no valid plan: no authority
            reason=str(exc) if isinstance(exc,PlanningUnavailable) else "Planning inputs or independent validation failed"
            # Exceptions from external solvers must not leak arbitrary text.
            attempt={"status":"unavailable","reason":reason}
            self._log_failure(reason,now)
        attempt.update({"time":self.clock.utc(),"elapsed_seconds":self.clock.monotonic()-started,"equipment_writes":False})
        revision,_=self.store.get_setting("shadow_attempt")
        self.store.set_setting("shadow_attempt",attempt,self.clock.utc(),revision)
        from datetime import timedelta
        from .shadow_review import review_completed
        review_day=datetime.fromtimestamp(self.clock.utc(),LONDON).date()-timedelta(days=1)
        review_revision,old_review=self.store.get_setting("shadow_review",{})
        if old_review.get("day")!=review_day.isoformat():
            review=review_completed(self.store,review_day,self.clock.utc())
            self.store.set_setting("shadow_review",review,self.clock.utc(),review_revision)
        return attempt

    def next_replan(self):
        """Replan at the usual interval, but always before the published plan expires (#23): a plan that uses smart slots lives only as long as the slot data (120 s), which is
        shorter than REPLAN_SECONDS, and a plan must be replaced before it lapses, not after."""
        now=self.clock.utc()
        due=now+REPLAN_SECONDS
        try:
            plan=self.store.get_setting("shadow_plan")[1] or {}
            valid_until=plan.get("valid_until")
            if isinstance(valid_until,(int,float)) and valid_until-now>REPLAN_MARGIN_S+15:due=min(due,valid_until-REPLAN_MARGIN_S)          # (a plan about to end anyway, such as at the window end, is not chased every 15 s)
        except (sqlite3.Error,OSError,ValueError,TypeError):pass
        return due

    def run(self,stop,count=1):
        with SingleOwner(str(self.store.path.resolve())+".planner.lock"):
            try:
                previous=None;next_run=0.;runs=0;next_records=0.;next_purge=0.
                while (count is None or runs<count) and not stop.is_set():
                    now=self.clock.utc()
                    if now>=next_records:
                        next_records=now+300
                        try:
                            from .daily_record import record_finished_days,snapshot_demand_forecast,snapshot_forecasts
                            snapshot_forecasts(self.store,now);snapshot_demand_forecast(self.store,now);record_finished_days(self.store,now)
                            from .demand_learning import learn
                            learn(self.store,now)
                            from .validation import default_log_paths,record_validation_days
                            record_validation_days(self.store,now,default_log_paths(self.store))
                        except (sqlite3.Error,ValueError,KeyError,TypeError,OSError):pass     # the daily records never get in the way of planning
                        try:                                                                   # GitHub issue 94: the learned solar correction, beside the forecast, not used
                            python=getattr(self.emhass,"python",None)
                            if python is not None:
                                from .pv_adjust import save_adjusted_forecast
                                save_adjusted_forecast(self.store,now,python)
                        except (sqlite3.Error,ValueError,KeyError,TypeError,OSError):pass
                    if now>=next_purge:
                        next_purge=now+6*3600
                        try:
                            from .retention import purge_detail
                            purge_detail(self.store,now)                                       # detail older than seven days, never before the half-hour record exists
                        except (sqlite3.Error,ValueError,KeyError,TypeError,OSError):pass
                    current=snapshot(self.store,now)
                    token=guard(current,now)
                    revision,_=self.store.get_setting("planner_refresh")
                    key=(token,revision)
                    if now>=next_run or key!=previous:
                        try:self.once()
                        except sqlite3.OperationalError:pass   # database busy: try again on the next cycle instead of exiting
                        runs+=1;previous=key;next_run=self.next_replan()
                    if (count is None or runs<count) and stop.wait(15):break
            finally:
                try:self.store.flush_ram_settings()
                except (sqlite3.Error,OSError,ValueError):pass
