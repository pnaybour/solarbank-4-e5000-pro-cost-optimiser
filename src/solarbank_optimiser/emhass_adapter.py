"""Isolated optional native reference and independent DC/AC reconstruction."""
from pathlib import Path
from .models import fingerprint, number
from .shadow import PlanningUnavailable, run_child


def validate_native(job,result,tolerance=1e-5):
    if result.get('status')!='optimal' or result.get('controls_enabled') is not False:
        raise PlanningUnavailable('Native EMHASS did not return an optimal shadow result')
    rows=job['scenarios'][1 if len(job['scenarios'])>1 else 0]['intervals']
    if len(result['flows'])!=len(rows) or result['initial_soc']!=job['initial_soc']:
        raise PlanningUnavailable('Native EMHASS coverage or initial SOC differs')
    model=job['model'];stored=job['initial_soc']*model['capacity_kwh'];cost=0.
    for f,p,row in zip(rows,job['prices'],result['flows']):
        for value in row.values():number(value)
        h=(f['end']-f['start'])/3600
        if row['start']!=f['start'] or row['end']!=f['end']:raise ValueError('Native timestamps differ')
        for key in ('charge_kw','discharge_kw','inverter_out_kw','inverter_in_kw','import_kw','export_kw','curtailed_kw'):
            if row[key]<-tolerance:raise ValueError('Negative native physical flow')
        if row['charge_kw']>tolerance and row['discharge_kw']>tolerance:raise ValueError('Native simultaneous battery flow')
        if row['inverter_in_kw']>tolerance and row['inverter_out_kw']>tolerance:raise ValueError('Native AC/DC loop')
        if row['export_kw']>tolerance:raise ValueError('Native intentional export')
        if row['charge_kw']>min(model['ac_limit_kw'],model['pv_charge_limit_kw'])+tolerance:raise ValueError('Native DC charge cap exceeded')
        if max(row['inverter_out_kw'],row['inverter_in_kw'])>model['ac_limit_kw']+tolerance:raise ValueError('Native inverter cap exceeded')
        if not -tolerance<=row['curtailed_kw']<=f['pv_kw']+tolerance:raise ValueError('Native curtailment differs')
        dc=f['pv_kw']-row['curtailed_kw']+row['discharge_kw']-row['charge_kw']-row['inverter_out_kw']+row['inverter_in_kw']
        ac=row['import_kw']-row['export_kw']+row['inverter_out_kw']-row['inverter_in_kw']-f['demand_kw']
        if abs(dc)>tolerance or abs(ac)>tolerance:raise ValueError('Native power balance differs')
        stored+=h*(row['charge_kw']*model['pv_charge_efficiency']-row['discharge_kw']/model['discharge_efficiency'])
        if not model['reserve']*model['capacity_kwh']-tolerance<=stored<=model['capacity_kwh']+tolerance:raise ValueError('Native SOC bounds violated')
        if abs(stored/model['capacity_kwh']-row['soc'])>tolerance:raise ValueError('Native SOC reconstruction differs')
        cost+=row['import_kw']*h*p['price']
    if abs(stored/model['capacity_kwh']-model['reserve'])>tolerance:raise ValueError('Native final inventory differs')
    return {**result,'import_cost_gbp':cost,'inputs_fingerprint':fingerprint(job),'independently_checked':True}


class EmhassReference:
    def __init__(self,python,timeout=20):self.python=Path(python).absolute();self.timeout=timeout
    def __call__(self,job):
        rows=job['scenarios'][1 if len(job['scenarios'])>1 else 0]['intervals']
        duration=rows[0]['end']-rows[0]['start']
        if any(abs(r['end']-r['start']-duration)>1e-7 for r in rows):
            return {'status':'unsupported_grid','reason':'Native fixed-step reference cannot represent these exact interval boundaries; target-policy analysis remains separate','controls_enabled':False}
        result=run_child(job,self.timeout,self.python,Path(__file__).with_name('emhass_child.py'))
        return validate_native(job,result)
