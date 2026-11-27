"""Retrospective checks of completed shadow horizons; never measured savings."""
from contextlib import closing
from datetime import date
import json

from .energy import day_bounds


def review_completed(store,day,cutoff):
    if isinstance(day,str):day=date.fromisoformat(day)
    left,right=day_bounds(day)
    if right>cutoff:raise ValueError("Review requires a completed London day")
    plans=[]
    with closing(store.connect()) as db:
        rows=db.execute("""SELECT id,payload FROM issuances WHERE json_extract(payload,'$.kind')='shadow_plan'
            AND json_extract(payload,'$.inputs.horizon_end')>=? AND json_extract(payload,'$.inputs.horizon_end')<?""",(left,right))
        evidence=[(r['id'],json.loads(r['payload'])) for r in rows]
    for identity,record in evidence:
        inputs=record['inputs'];summary=record['summary']
        if inputs['horizon_end']>cutoff:continue
        chart=record['result']['chart']
        source=store.minute_source()
        errors=[]
        for minute,item in sorted(source.minutes('soc',inputs['decision_at'],inputs['horizon_end']+.00001).items()):
            if item.mean is None or minute+60>cutoff or minute>inputs['horizon_end']:continue
            pair=next(((a,b) for a,b in zip(chart,chart[1:]) if a['time']<=minute<=b['time']),None)
            if pair:
                a,b=pair
                fraction=(minute-a['time'])/(b['time']-a['time'])
                predicted=a['soc']+(b['soc']-a['soc'])*fraction
                errors.append(abs(predicted-item.mean))
        central=inputs['scenarios'][1 if len(inputs['scenarios'])>1 else 0]['intervals']
        forecast_errors={}
        for metric,key in (('household_power','demand_kw'),('pv_power','pv_kw')):
            values=[]
            for minute,item in sorted(source.minutes(metric,inputs['decision_at'],inputs['horizon_end']).items()):
                if item.covered!=60 or item.mean is None or minute+60>cutoff:continue
                matches=[r for r in central if r['start']<=minute and minute+60<=r['end']]
                if len(matches)==1:values.append(abs(matches[0][key]-item.mean))
            forecast_errors[metric]={'covered_minutes':len(values),'mae_kw':sum(values)/len(values) if values else None}
        plans.append({'issuance_id':identity,'initial_soc':inputs['initial_soc'],'recorder_soc_points':len(errors),
            'soc_mae_fraction':sum(errors)/len(errors) if errors else None,'forecast_errors':forecast_errors,
            'expected_policy_difference_gbp':summary['replay_difference_gbp'],'measured_savings_gbp':None})
    return {'day':day.isoformat(),'reviewed_at':cutoff,'completed_shadow_plans':len(plans),'plans':plans,
        'measured_savings_gbp':None,'model_promoted':False,'controls_enabled':False,
        'limitations':'Recorder points do not prove device refresh or continuous SOC coverage. Modelled policy differences are not causal savings; billed costs and matched recorded Anker operation remain required.'}
