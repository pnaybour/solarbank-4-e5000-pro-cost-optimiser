"""Pinned EMHASS computation only; no HTTP server, HA client or publishing."""
import contextlib
import io
import json
import logging
from pathlib import Path
import socket
import sys


def solve(job):
    import importlib.metadata
    version=importlib.metadata.version('emhass')
    if version not in {'0.18.4','0.18.5'}:raise ValueError("Unpinned EMHASS")
    import pandas as pd
    import emhass
    from emhass.optimization import Optimization
    defaults=json.loads((Path(emhass.__file__).parent/'data/config_defaults.json').read_text())
    model=job["model"]
    central=job["scenarios"][1 if len(job["scenarios"])>1 else 0]["intervals"]
    seconds=central[0]["end"]-central[0]["start"]
    if any(abs(r["end"]-r["start"]-seconds)>1e-7 for r in central):raise ValueError("Irregular grid")
    config={**defaults,"set_use_pv":True,"set_use_battery":True,"number_of_batteries":1,
        "number_of_deferrable_loads":0,"def_load_config":[],"set_total_pv_sell":False,
        "set_nocharge_from_grid":False,"set_nodischarge_to_grid":True,"set_battery_dynamic":False,
        "set_battery_first_priority":False,"weight_battery_charge":0.,"weight_battery_discharge":0.,
        "inverter_is_hybrid":True,"compute_curtailment":True,
        "inverter_ac_output_max":model["ac_limit_kw"]*1000,"inverter_ac_input_max":model["ac_limit_kw"]*1000,
        "inverter_efficiency_dc_ac":1.,"inverter_efficiency_ac_dc":1.,
        "inverter_stress_cost":0.,"battery_stress_cost":0.,"battery_soc_deficit_cost":0.,"battery_soc_surplus_cost":0.,
        "battery_charge_power_max":min(model["ac_limit_kw"],model["pv_charge_limit_kw"])*1000*model["pv_charge_efficiency"],
        "battery_discharge_power_max":model["ac_limit_kw"]*1000/model["discharge_efficiency"],
        "battery_charge_efficiency":model["pv_charge_efficiency"],"battery_discharge_efficiency":model["discharge_efficiency"],
        "battery_nominal_energy_capacity":model["capacity_kwh"]*1000,
        "battery_minimum_state_of_charge":model["reserve"],"battery_maximum_state_of_charge":1.,
        "battery_target_state_of_charge":model["reserve"],"maximum_power_from_grid":1e6,"maximum_power_to_grid":0,
        "lp_solver":"Highs","lp_solver_timeout":10,"lp_solver_mip_rel_gap":0.,"num_threads":1,
        "delta_forecast_daily":(job["horizon_end"]-job["decision_at"])/86400}
    retrieve={**config,"optimization_time_step":pd.Timedelta(seconds=seconds),"time_zone":"UTC"}
    index=pd.to_datetime([r["start"] for r in central],unit='s',utc=True)
    frame=pd.DataFrame({"import_price":[p["price"] for p in job["prices"]],"export_price":0.},index=index)
    pv=pd.Series([r["pv_kw"]*1000 for r in central],index=index)
    load=pd.Series([r["demand_kw"]*1000 for r in central],index=index)
    logger=logging.getLogger('solarbank-emhass');logger.handlers=[logging.NullHandler()];logger.propagate=False
    logger.setLevel(logging.CRITICAL)
    optimizer=Optimization(retrieve,config.copy(),config.copy(),'import_price','export_price','profit',{},logger,
                           num_timesteps=len(central))
    result=optimizer.perform_dayahead_forecast_optim(frame,pv,load,soc_init=job["initial_soc"],soc_final=model["reserve"])
    if str(optimizer.optim_status).lower()!='optimal':raise ValueError("Non-optimal native result")
    values=lambda key:optimizer.vars[key].value
    charge=-optimizer.vars['p_sto_neg'][0].value/1000
    discharge=optimizer.vars['p_sto_pos'][0].value/1000
    output=[]
    for i,r in enumerate(central):
        output.append({"start":r["start"],"end":r["end"],"soc":float(result['SOC_opt'].iloc[i]),
          "charge_kw":float(charge[i]),"discharge_kw":float(discharge[i]),
          "inverter_out_kw":float(values('p_dc_ac')[i])/1000,"inverter_in_kw":float(values('p_ac_dc')[i])/1000,
          "import_kw":float(result['P_grid_pos'].iloc[i])/1000,"export_kw":-float(result['P_grid_neg'].iloc[i])/1000,
          "curtailed_kw":float(result['P_PV_curtailment'].iloc[i])/1000})
    return {"status":"optimal","engine":f"EMHASS {version} / HiGHS","initial_soc":job["initial_soc"],
        "final_soc":model["reserve"],"controls_enabled":False,"flows":output,
        "role":"Separate freely-dispatchable conservative hybrid reference; not an executable target policy or a savings bound",
        "charge_input_cap_kw":min(model["ac_limit_kw"],model["pv_charge_limit_kw"]),
        "pv_boundary":"Available DC potential; unity provisional inverter efficiency; battery losses applied once"}


def main():
    def blocked(*a,**k):raise RuntimeError("Solver network access prohibited")
    socket.socket.connect=blocked;socket.socket.connect_ex=blocked;socket.create_connection=blocked
    try:
        raw=sys.stdin.buffer.read(2_000_001)
        if len(raw)>2_000_000:raise ValueError()
        # Native libraries occasionally print setup information; never mix it into JSON.
        with contextlib.redirect_stdout(io.StringIO()):result=solve(json.loads(raw))
        sys.stdout.write(json.dumps(result,allow_nan=False))
        return 0
    except Exception:
        sys.stdout.write('{"status":"failed","controls_enabled":false}')
        return 2


if __name__=='__main__':raise SystemExit(main())
