"""Disposable, network-free target-policy search. No equipment output exists."""
import json
import math
import sys

from solarbank_optimiser.models import ForecastInterval, PriceInterval, encoded, fingerprint
from solarbank_optimiser.planning import BatteryModel, Scenario, replay


def feasible(result,prices,capacity,horizon_end,policy=None):
    groups=[]
    for row,price in zip(result["flows"],prices):
        if not price.cheap:
            continue
        if groups and groups[-1]["end"]==row["start"] and groups[-1]["target"]==row["grid_target"]:
            groups[-1].update(end=row["end"],stored=row["after_kwh"])
        else:
            groups.append({"end":row["end"],"target":row["grid_target"],"stored":row["after_kwh"]})
    if policy:
        for night in policy.get("windows",[]):
            for window in night["intervals"]:
                if window["end"]<=horizon_end:
                    row=next((r for r in result["flows"] if r["end"]==window["end"]),None)
                    if row is None or row["after_kwh"]+1e-7<night["target"]*capacity:return False
    return all(g["end"]==horizon_end or g["stored"]+1e-7>=g["target"]*capacity for g in groups)


MAX_PASSES=4


def cheap_windows(prices):
    """How many separate cheap windows the horizon holds."""
    count, previous = 0, False
    for price in prices:
        if price.cheap and not previous:
            count += 1
        previous = price.cheap
    return count


def evaluate(model,cases,prices,job,target):
    """Expected cost and reachability of one target (a number, or one number per cheap window) over every scenario."""
    plans=[replay(model,s,prices,job["initial_soc"],target,job["terminal_price"],job["charge_policy"]) for s in cases]
    cost=math.fsum(s.probability*r["objective_gbp"] for s,r in zip(cases,plans))
    valid=all(feasible(r,prices,model.capacity_kwh,job["horizon_end"],job["charge_policy"]) for r in plans)
    return cost,valid,plans


def refine(model,cases,prices,job,start):
    """Coordinate search: from the best single target, try every grid level for one window at a time and keep any strict improvement, until a full pass changes nothing.
    A tie keeps the lower level (cheaper to stay away from a full battery). Returns (targets, evaluation count)."""
    windows=cheap_windows(prices)
    if windows<2:return [start],0
    current=[start]*windows
    cost,valid,_=evaluate(model,cases,prices,job,current)
    count=1
    for _ in range(MAX_PASSES):
        changed=False
        for index in range(windows):
            for target in job["targets"]:
                if target==current[index]:continue
                trial=current[:index]+[target]+current[index+1:]
                trial_cost,trial_valid,_=evaluate(model,cases,prices,job,trial);count+=1
                if trial_valid and (not valid or trial_cost<cost-1e-12 or (abs(trial_cost-cost)<=1e-12 and target<current[index])):
                    current,cost,valid,changed=trial,trial_cost,trial_valid,True
        if not changed:break
    return current,count


def solve(job):
    import hashlib
    from pathlib import Path
    expected={"planning","forecasts","household_policy","shadow_solver","shadow","emhass_child","emhass_adapter"}
    if set(job["implementation_sha256"])!=expected:raise ValueError("Missing implementation fingerprints")
    for name in expected:
        if hashlib.sha256(Path(__file__).with_name(name+".py").read_bytes()).hexdigest()!=job["implementation_sha256"][name]:
            raise ValueError("Archived solver version differs from installed code")
    model=BatteryModel(**job["model"])
    cases=[Scenario(r["probability"],tuple(ForecastInterval(**v) for v in r["intervals"])) for r in job["scenarios"]]
    if not cases or not math.isclose(math.fsum(s.probability for s in cases),1.,abs_tol=1e-9):
        raise ValueError("Invalid scenario weights")
    prices=[PriceInterval(**p) for p in job["prices"]]
    candidates=[];best=None
    for target in job["targets"]:
        plans=[replay(model,s,prices,job["initial_soc"],target,job["terminal_price"],job["charge_policy"]) for s in cases]
        cost=math.fsum(s.probability*r["objective_gbp"] for s,r in zip(cases,plans))
        valid=all(feasible(r,prices,model.capacity_kwh,job["horizon_end"],job["charge_policy"]) for r in plans)
        candidates.append({"target":target,"expected_cost_gbp":cost,"feasible":valid})
        if valid and (best is None or (cost,target)<(best["expected_cost_gbp"],best["target"])):
            best={"target":target,"expected_cost_gbp":cost,"scenarios":plans}
    if best is not None:
        targets,evaluations=refine(model,cases,prices,job,best["target"])
        if len(targets)>1:
            cost,valid,plans=evaluate(model,cases,prices,job,targets)
            if valid and cost<best["expected_cost_gbp"]-1e-12:
                best={"target":targets[0],"targets":targets,"expected_cost_gbp":cost,"scenarios":plans}
        best.setdefault("targets",[best["target"]])
    if best is None:
        return {"status":"infeasible","reason":"No candidate can reach its requested target in all provisional scenarios",
                "controls_enabled":False,"inputs_fingerprint":fingerprint(job)}
    baseline=[replay(model,s,prices,job["initial_soc"],model.reserve,job["terminal_price"],job["baseline_policy"]) for s in cases]
    basecost=math.fsum(s.probability*r["objective_gbp"] for s,r in zip(cases,baseline))
    central=best["scenarios"][1 if len(cases)>1 else 0]["flows"]
    next_window=None
    for row,price in zip(central,prices):
        if price.cheap:
            if next_window is None:next_window={"start":row["start"],"end":row["end"],"target":row["grid_target"]}
            elif next_window["end"]==row["start"] and next_window["target"]==row["grid_target"]:next_window["end"]=row["end"]
            else:break
        elif next_window is not None:break
    if next_window is None:
        return {"status":"infeasible","controls_enabled":False,"inputs_fingerprint":fingerprint(job)}
    chart=[{"time":job["decision_at"],"soc":job["initial_soc"]}]+[
        {"time":r["end"],"soc":r["after_kwh"]/model.capacity_kwh} for r in central]
    return {"status":"completed_grid","controls_enabled":False,"inputs_fingerprint":fingerprint(job),
        "best":best,"candidate_costs":candidates,"baseline":baseline,"baseline_cost_gbp":basecost,
        "next_target":next_window["target"],"next_window":next_window,"chart":chart,
        "scope":"Exact search over the declared 5% target grid, one level per cheap window (coordinate search from the best single level); not a continuous global optimum"}


def main():
    # A solver child must never retrieve provider data or speak to Home Assistant.
    import socket
    def blocked(*a,**k):raise RuntimeError("Solver networking prohibited")
    socket.socket.connect=blocked;socket.socket.connect_ex=blocked;socket.create_connection=blocked
    try:
        raw=sys.stdin.buffer.read(2_000_001)
        if len(raw)>2_000_000:raise ValueError()
        result=solve(json.loads(raw))
        sys.stdout.write(encoded(result))
    except Exception:
        sys.stdout.write('{"status":"failed","controls_enabled":false}')
        return 2
    return 0


if __name__=="__main__":raise SystemExit(main())
