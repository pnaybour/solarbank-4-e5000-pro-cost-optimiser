"""Owner-provided daily statistics are a benchmark, never an hourly profile."""
from datetime import datetime,timedelta
from .energy import LONDON,day_bounds
from .models import number


def compare_daily(reference,demand):
    if not reference:return None
    try:
        mean=number(reference['mean_kwh'],'mean',0)
        sd=number(reference['sample_sd_kwh'],'sample SD',0)
        count=reference['sample_days']
        if sd<=0 or type(count) is not int or count<2:raise ValueError()
    except (KeyError,TypeError,ValueError):
        return {'status':'invalid_reference','days':[]}
    result={'status':'awaiting_complete_normal_forecast','mean_kwh':mean,'sample_sd_kwh':sd,
            'sample_days':count,'period_start':reference.get('period_start'),'period_end':reference.get('period_end'),
            'source':'Owner-provided analysis of Pi monitor readings','days':[],
            'meaning':'Sample standard deviation describes past variation, not a prediction interval or a hard consumption limit. This benchmark does not supply an hourly demand profile.'}
    if not demand:return result
    rows=demand['intervals']
    if not rows:return result
    first=datetime.fromtimestamp(rows[0]['start'],LONDON).date()
    last=datetime.fromtimestamp(rows[-1]['end'],LONDON).date()
    day=first
    while day<=last:
        start,end=day_bounds(day)
        selected=[r for r in rows if start<=r['start'] and r['end']<=end]
        if (selected and selected[0]['start']==start and selected[-1]['end']==end
            and all(r['demand_mode']=='Normal consumption' for r in selected)
            and all(a['end']==b['start'] for a,b in zip(selected,selected[1:]))):
            energy=sum(r['demand_kw']*(r['end']-r['start'])/3600 for r in selected)
            z=(energy-mean)/sd
            result['days'].append({'day':day.isoformat(),'forecast_kwh':energy,'difference_kwh':energy-mean,
                                  'sample_sd_from_mean':z,'check_needed':abs(z)>2})
        day+=timedelta(days=1)
    if result['days']:result['status']='check_needed' if any(r['check_needed'] for r in result['days']) else 'within_reference_range'
    return result
