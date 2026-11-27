"""Covered recorder-minute means; never invent device measurement refresh.

A restart, invalid sample, repeated timestamp or gap breaks adjacency. Only
exactly tiled minutes enter Normal training; partial minutes remain partial.
"""
from .energy import integrate
from .models import Observation, Quality

PROVENANCE = 'shared-monitor-minute'


class MinuteRecorder:
    def __init__(self,store,max_gap=5):
        self.store=store;self.max_gap=max_gap;self.previous={};self.buckets={}

    def observe(self,current):
        if current.metric not in {'household_power','pv_power'}:return
        key=current.metric
        previous=self.previous.pop(key,None)
        if current.quality!=Quality.OK:return
        # Repeated retrieval of one recorder sample must not break or extend it.
        if previous and current.sample_at==previous.sample_at:
            self.previous[key]=previous
            return
        self.previous[key]=current
        if not previous:return
        if current.metric=='household_power' and (previous.demand_mode is None or
            previous.demand_mode!=current.demand_mode or previous.demand_revision!=current.demand_revision):return
        for piece in integrate(previous.sample_at,current.sample_at,previous.value,current.value,self.max_gap):
            minute=int(piece.start)//60*60
            if current.metric=='household_power':
                _,selection=self.store.setting_at('demand',piece.start)
                if selection and any(piece.start<t<piece.end for t in (selection.get('start'),selection.get('end')) if t is not None):
                    continue
            token=(key,minute)
            bucket=self.buckets.setdefault(token,{'end':piece.start,'seconds':0.,'kwh':0.,'mode':current.demand_mode,'revision':current.demand_revision})
            if abs(bucket['end']-piece.start)>1e-6 or bucket['mode']!=current.demand_mode or bucket['revision']!=current.demand_revision:
                bucket['broken']=True
            bucket['end']=piece.end;bucket['seconds']+=piece.end-piece.start;bucket['kwh']+=piece.positive_kwh
            if abs(piece.end-minute-60)<1e-6:
                covered=bucket['seconds']
                full=not bucket.get('broken') and abs(covered-60)<1e-6
                o=Observation('monitor-minute:'+key,key,current.retrieved_at,
                    bucket['kwh']*3600/covered,'kW',coverage_seconds=60. if full else min(covered,59.999999),
                    interval_start=minute,demand_mode=bucket['mode'],demand_revision=bucket['revision'],provenance=PROVENANCE)
                self.store.add_observations([o])
                del self.buckets[token]
        # Gapped/invalid minutes cannot later be completed by an unrelated segment.
        for token in list(self.buckets):
            if token[0]==key and token[1]+60<current.sample_at:del self.buckets[token]
