"""Shadow acceptance: supplied fixtures only; never equipment/account access."""
from contextlib import closing
from copy import deepcopy
from datetime import date
import json
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.energy import day_bounds
from solarbank_optimiser.models import FakeClock, Observation, encoded, fingerprint
from solarbank_optimiser.services import ProviderConfig, cache_identity
from solarbank_optimiser.settings import london_input
from solarbank_optimiser.shadow import (PlanningUnavailable, ShadowConfig, ShadowWorker, assemble,
                                       guard, plan_view, publish, run_child, snapshot, validate_result, weighted)
from solarbank_optimiser.shadow_solver import solve
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app

NOW=london_input('2026-10-25T12:00')


def put(store,key,value,now=NOW):
    return store.set_setting(key,value,now,store.get_setting(key)[0])


@pytest.fixture
def site(tmp_path):
    store=Store(tmp_path/'shadow.sqlite3');store.initialise()
    clock=FakeClock(NOW)
    put(store,'demand',{'mode':'Away from home','start':0.,'away_watts':400.})
    put(store,'fallback_tariff',{'day_pence':34.96,'cheap_pence':6.6,'valid_from':0.,'confirmed':True})
    cfg=ProviderConfig(weather_enabled=True)
    put(store,'providers',cfg.model_dump())
    intervals=[]
    for i in range(96):
        start=NOW+i*3600
        hour=((start-NOW)/3600+12)%24
        intervals.append({'start':start,'end':start+3600,'pv_kw':max(0,1-abs(hour-12)/5)})
    model={'kind':'weather','identity':cache_identity(cfg,'weather'),'intervals':intervals}
    identity=store.archive(NOW-60,encoded(model).encode(),model)
    put(store,'cache_weather',{**model,'acquired_at':NOW-60,'issuance_id':identity})
    store.add_observations([Observation('fixture-actual-soc','soc',NOW-1,.5,'fraction')])
    return store,clock


def job(site):
    store,clock=site
    return assemble(store,snapshot(store,clock.utc()),clock.utc(),ShadowConfig())


def normal_history(store):
    first,last=day_bounds(date(2026,10,24))
    store.add_observations([Observation('fixture','household_power',t+60,.4,'kW',interval_start=t,
        coverage_seconds=60.,demand_mode='Normal consumption',demand_revision=0) for t in range(int(first),int(last),60)])


def test_48_hours_from_actual_soc_includes_night_demand_pv_and_prices(site):
    value=job(site)
    assert value['initial_soc']==.5
    assert value['decision_at']==NOW and value['horizon_end']==NOW+48*3600
    rows=value['scenarios'][1]['intervals']
    assert rows[0]['start']==NOW and rows[-1]['end']==NOW+48*3600
    assert all(a['end']==b['start'] for a,b in zip(rows,rows[1:]))
    assert all(r['demand_kw']==pytest.approx(.4) for r in rows)
    assert sum(r['probability'] for r in value['scenarios'])==1
    assert any(r['pv_kw']>0 for r in rows)
    assert any(p['cheap'] for p in value['prices'])
    assert value['terminal_price']==.066
    assert '"account":' not in encoded(value) and '"api_key":' not in encoded(value)


def test_fractional_start_preserves_initial_partial_interval(site):
    store,clock=site;clock.advance(13.5)
    store.add_observations([Observation('fixture-actual-soc','soc',clock.utc()-1,.5,'fraction')])
    value=job(site)
    assert value['scenarios'][1]['intervals'][0]['start']==NOW+13.5
    assert sum(r['end']-r['start'] for r in value['prices'])==48*3600


def test_checked_plan_archive_and_active_pointer_are_reproducible(site):
    store,clock=site
    attempt=ShadowWorker(store,clock=clock,solver=solve).once()
    assert attempt['status']=='published'
    view=plan_view(store,NOW)
    assert view['status']=='checked_shadow' and not view['controls_enabled']
    assert view['initial_soc']==.5 and .05<=view['grid_target']<=1
    with closing(store.connect()) as db:
        row=db.execute('SELECT payload,raw FROM issuances WHERE id=?',(view['issuance_id'],)).fetchone()
    evidence=json.loads(row['payload'])
    import hashlib
    assert row['raw']==hashlib.sha256(row['payload'].encode()).hexdigest().encode()
    assert evidence['inputs']['initial_soc']==.5
    assert solve(evidence['inputs'])['best']==evidence['result']['best']
    assert not view['capabilities_verified']


@pytest.mark.parametrize('mutation',['soc','flow_cost','input_forecast','fingerprint','status','next_target','chart','candidate'])
def test_independent_checks_reject_tampered_solver_results(site,mutation):
    value=job(site);result=solve(value)
    if mutation=='soc':result['best']['scenarios'][0]['flows'][0]['after_kwh']+=.1
    if mutation=='flow_cost':result['best']['scenarios'][0]['flows'][0]['cost']+=.1
    if mutation=='input_forecast':result['best']['scenarios'][0]['flows'][0]['demand_kw']+=.1
    if mutation=='fingerprint':result['inputs_fingerprint']='0'*64
    if mutation=='status':result['status']='timed_out'
    if mutation=='next_target':result['next_target']=1 if result['next_target']!=1 else .05
    if mutation=='chart':result['chart'][0]['soc']+=.01
    if mutation=='candidate':result['candidate_costs'][0]['expected_cost_gbp']+=10
    with pytest.raises(ValueError):validate_result(value,result)


@pytest.mark.parametrize('source',['demand','fallback_tariff','providers','operating_state','soc'])
def test_changed_inputs_withdraw_previous_target_immediately(site,source):
    store,clock=site
    assert ShadowWorker(store,clock=clock,solver=solve).once()['status']=='published'
    if source=='soc':store.add_observations([Observation('fixture-actual-soc','soc',NOW,.6,'fraction')])      # a move to another 5 % band
    elif source=='operating_state':put(store,source,{'mode':'handover_pending'})
    else:
        _,value=store.get_setting(source)
        put(store,source,value)
    view=plan_view(store,NOW)
    assert view['status']=='unavailable' and view['grid_target'] is None


def test_new_identical_soc_is_fresh_but_expiry_and_stale_state_are_not(site):
    store,clock=site
    ShadowWorker(store,clock=clock,solver=solve).once()
    store.add_observations([Observation('fixture-actual-soc','soc',NOW+3,.5,'fraction')])
    assert plan_view(store,NOW+4)['status']=='checked_shadow'
    assert plan_view(store,NOW+9)['grid_target'] is None
    assert plan_view(store,NOW+301)['grid_target'] is None


def test_settings_or_soc_change_during_solver_never_publishes(site):
    store,clock=site
    def stale(value):
        result=solve(value)
        put(store,'fallback_tariff',{'day_pence':35.,'cheap_pence':7.,'valid_from':0.})
        return result
    assert ShadowWorker(store,clock=clock,solver=stale).once()['status']=='unavailable'
    assert store.get_setting('shadow_plan')==(0,None)


def test_failed_solve_retains_only_previous_still_valid_archive(site):
    store,clock=site
    ShadowWorker(store,clock=clock,solver=solve).once()
    original=store.get_setting('shadow_plan')
    bad=lambda value:{'status':'failed','controls_enabled':False}
    assert ShadowWorker(store,clock=clock,solver=bad).once()['status']=='unavailable'
    assert store.get_setting('shadow_plan')==original
    assert plan_view(store,NOW)['status']=='checked_shadow'


@pytest.mark.parametrize('missing',['soc','prices','normal_day','pause'])      # normal_day: with the learning switch off (the default below is covered separately)
def test_missing_partial_and_operator_priorities_produce_no_plan(site,missing):
    store,clock=site
    if missing=='soc':clock.advance(10)
    if missing=='prices':put(store,'fallback_tariff',{'day_pence':34.96,'cheap_pence':6.6,'valid_from':0.,'valid_until':NOW-1})
    if missing=='normal_day':put(store,'demand',{'mode':'Normal consumption','start':0.});put(store,'demand_learning',{'enabled':False})
    if missing=='pause':put(store,'operating_state',{'mode':'handover_pending'})
    def forbidden(value):pytest.fail('Missing inputs must not enter solver')
    attempt=ShadowWorker(store,clock=clock,solver=forbidden).once()
    assert attempt['status']=='unavailable'
    assert store.get_setting('shadow_plan')==(0,None)


def test_gap_and_overlap_cannot_cancel_into_fake_complete_coverage():
    rows=[{'start':0,'end':10,'value':1.},{'start':0,'end':10,'value':1.}]
    with pytest.raises(PlanningUnavailable):weighted(rows,0,20,'value')


def test_high_visit_preceding_night_and_unreachable_goal(site):
    store,clock=site;normal_history(store)
    put(store,'demand',{'mode':'High demand','start':NOW+86400,'end':NOW+86400+3600})
    value=job(site);result=solve(value)
    assert result['next_target']==1
    validate_result(value,result)
    put(store,'demand',{'mode':'High demand','start':NOW,'end':london_input('2026-10-25T23:40')})
    attempt=ShadowWorker(store,clock=clock,solver=solve).once()
    assert attempt['status']=='unavailable' and 'reachable' in attempt['reason']


def test_subprocess_deadline_and_no_credentials_in_arguments(monkeypatch):
    seen={}
    def timeout(arguments,**kwargs):
        seen.update(arguments=arguments,**kwargs)
        raise subprocess.TimeoutExpired(arguments,1)
    monkeypatch.setattr(subprocess,'run',timeout)
    with pytest.raises(PlanningUnavailable):run_child({'private':'fixture'},1)
    assert seen['timeout']==1 and 'fixture' not in str(seen['arguments'])


def test_failed_archive_transaction_does_not_install_active_pointer(site):
    store,clock=site
    with closing(store.connect()) as db,db:
        db.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON issuances BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    assert ShadowWorker(store,clock=clock,solver=solve).once()['status']=='unavailable'
    assert store.get_setting('shadow_plan')==(0,None)


def test_stalled_solver_does_not_hold_website_or_settings(site):
    store,clock=site;entered=threading.Event();release=threading.Event()
    def stalled(value):entered.set();release.wait(5);return solve(value)
    worker=ShadowWorker(store,clock=clock,solver=stalled)
    thread=threading.Thread(target=worker.once);thread.start()
    try:
        assert entered.wait(1)
        password='shadow-fixture-test-password'
        client=TestClient(create_app(WebConfig(store.path,password, status_cache_seconds=0),clock));client.auth=('solarbank',password)
        assert client.get('/health').status_code==200
        status=client.get('/api/status').json()
        headers={'Origin':'http://testserver','X-CSRF-Token':status['csrf_token']}
        assert client.put('/api/shadow-refresh',json={},headers=headers).json()['queued']
        assert client.put('/api/shadow-refresh',json={}).status_code==403
    finally:release.set();thread.join(5)
    assert not thread.is_alive()


def test_plan_evidence_endpoint_requires_login_and_only_returns_plan_kind(site):
    store,clock=site
    ShadowWorker(store,clock=clock,solver=solve).once()
    identity=store.get_setting('shadow_plan')[1]['issuance_id']
    password='shadow-fixture-test-password'
    client=TestClient(create_app(WebConfig(store.path,password, status_cache_seconds=0),clock));client.auth=('solarbank',password)
    assert client.get('/api/shadow-plan',auth=None).status_code==401
    assert client.get('/api/shadow-plan/'+identity,auth=None).status_code==401
    assert client.get('/api/shadow-plan/'+identity).json()['kind']=='shadow_plan'
    weather=store.get_setting('cache_weather')[1]['issuance_id']
    assert client.get('/api/shadow-plan/'+weather).status_code==404


def test_late_solver_rejects_result_even_with_new_identical_fresh_soc(site):
    store,clock=site
    def late(value):
        result=solve(value);clock.advance(30)
        store.add_observations([Observation('fixture-actual-soc','soc',clock.utc(),.5,'fraction')])
        return result
    assert ShadowWorker(store,clock=clock,solver=late).once()['status']=='unavailable'
    assert store.get_setting('shadow_plan')==(0,None)


def test_invalid_scenario_probabilities_never_publish(site):
    value=job(site);value['scenarios'][0]['probability']=-.25
    with pytest.raises(ValueError):solve(value)


def test_native_nonoptimal_or_false_skip_never_publishes(site):
    store,clock=site
    bad=lambda value:{'status':'optimal_inaccurate','controls_enabled':False}
    assert ShadowWorker(store,clock=clock,solver=solve,emhass=bad).once()['status']=='unavailable'
    assert store.get_setting('shadow_plan')==(0,None)


def test_native_irregular_grid_is_explicit_and_does_not_fake_a_native_optimum(site):
    from solarbank_optimiser.emhass_adapter import EmhassReference
    store,clock=site;clock.advance(13.5)
    store.add_observations([Observation('fixture-actual-soc','soc',clock.utc(),.5,'fraction')])
    value=job(site)
    result=EmhassReference('/does/not/exist')(value)
    assert result['status']=='unsupported_grid' and not result['controls_enabled']


def test_completed_day_review_keeps_missing_coverage_and_measured_savings_unknown(site):
    from solarbank_optimiser.shadow_review import review_completed
    store,clock=site
    ShadowWorker(store,clock=clock,solver=solve).once()
    end=NOW+48*3600
    day=__import__('datetime').datetime.fromtimestamp(end,__import__('solarbank_optimiser.energy',fromlist=['LONDON']).LONDON).date()
    review=review_completed(store,day,end+86400)
    assert review['completed_shadow_plans']==1
    assert review['plans'][0]['forecast_errors']['household_power']['covered_minutes']==0
    assert review['plans'][0]['forecast_errors']['household_power']['mae_kw'] is None
    assert review['measured_savings_gbp'] is None and not review['model_promoted']


def test_review_rejects_partial_calendar_day(site):
    from solarbank_optimiser.shadow_review import review_completed
    store,clock=site
    with pytest.raises(ValueError):review_completed(store,date(2026,10,25),NOW)


def test_missing_normal_history_has_plain_readiness_reason(site):
    store,clock=site
    put(store,'demand',{'mode':'Normal consumption','start':0.})
    put(store,'demand_learning',{'enabled':False})            # the Normal default is part of the learning switch: off means the old, strict rule
    result=ShadowWorker(store,clock=clock,solver=solve).once()
    assert result['status']=='unavailable'
    assert 'recent complete Normal-use day' in result['reason']


def test_with_learning_on_and_no_normal_day_the_plan_uses_the_flat_default_and_says_so(site):
    """9 November, owner: a Normal default. A return from Away leaves no complete Normal day; the planner used to wait."""
    store,clock=site
    put(store,'demand',{'mode':'Normal consumption','start':0.})
    result=ShadowWorker(store,clock=clock,solver=solve).once()
    assert result['status']!='unavailable'
    summary=store.get_setting('shadow_plan')[1]
    assert summary is not None and any('DEFAULT' in line for line in summary['assumptions'])


def test_worker_rides_out_a_busy_database_instead_of_exiting(site,monkeypatch):
    import sqlite3,threading
    store,clock=site
    worker=ShadowWorker(store,clock=clock,solver=solve)
    calls={'n':0}
    def busy():
        calls['n']+=1
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(worker,'once',busy)
    stop=threading.Event()
    clock_waits=[]
    stop.wait=lambda t:(clock_waits.append(t),True)[1]
    worker.run(stop,count=1)                  # must return normally rather than raise
    assert calls['n']==1


def test_a_one_percent_tick_of_the_battery_does_not_withdraw_the_plan_but_a_five_percent_move_does(site):
    store,clock=site
    assert ShadowWorker(store,clock=clock,solver=solve).once()['status']=='published'
    store.add_observations([Observation('fixture-actual-soc','soc',NOW,.51,'fraction')])
    assert plan_view(store,NOW)['status']=='checked_shadow'
    store.add_observations([Observation('fixture-actual-soc','soc',NOW,.56,'fraction')])
    assert plan_view(store,NOW)['status']=='unavailable'


def test_new_minute_rows_do_not_change_the_planning_fingerprint_but_a_new_complete_day_does(site):
    from solarbank_optimiser.shadow import guard,snapshot as snap
    store,clock=site
    normal_history(store)
    before=guard(snap(store,clock.utc()),clock.utc())
    store.add_observations([Observation('fixture','household_power',NOW-30,.4,'kW',interval_start=NOW-90,coverage_seconds=60.,demand_mode='Normal consumption',demand_revision=0)])
    assert guard(snap(store,clock.utc()),clock.utc())==before                         # one more minute of data: same decision
    first,last=day_bounds(date(2026,10,23))
    store.add_observations([Observation('fixture','household_power',t+60,.4,'kW',interval_start=t,coverage_seconds=60.,demand_mode='Normal consumption',demand_revision=0) for t in range(int(first),int(last),60)])
    assert guard(snap(store,clock.utc()),clock.utc())!=before                         # a newly complete day can change the reference


def test_the_planner_replans_every_four_minutes_not_every_minute_when_nothing_material_changes(site):
    store,clock=site
    store.add_observations([Observation('fixture-actual-soc','soc',NOW-1,.5,'fraction')])
    calls=[]
    worker=ShadowWorker(store,clock=clock,solver=solve)
    original=worker.once
    def counted():
        calls.append(clock.utc());return original()
    worker.once=counted
    stop=threading.Event()
    def tick(seconds):
        clock.advance(seconds);return False
    stop.wait=tick                                                                   # each 15 s wait advances the fake clock
    worker.run(stop,count=6)
    assert len(calls)==6 and all(b-a>=240 for a,b in zip(calls[1:],calls[2:]))       # (the first publication may settle once; after that, four minutes apart)


def test_the_minute_by_minute_slot_refreshes_neither_change_the_fingerprint_nor_abort_a_publication(site):
    from solarbank_optimiser.shadow import guard,snapshot as snap
    store,clock=site
    put(store,'cache_slots',{'identity':'x','acquired_at':NOW-60,'intervals':[]})
    put(store,'established_slots',{'identity':'x','intervals':[]})
    put(store,'provider_prices',{'status':'ok','attempted_at':NOW-60})
    before=guard(snap(store,clock.utc()),clock.utc())
    put(store,'cache_slots',{'identity':'x','acquired_at':NOW,'intervals':[]})              # the same content, a newer clock
    put(store,'established_slots',{'identity':'x','intervals':[]})
    put(store,'provider_prices',{'status':'ok','attempted_at':NOW})
    assert guard(snap(store,clock.utc()),clock.utc())==before
    put(store,'cache_slots',{'identity':'x','acquired_at':NOW,'intervals':[{'start':NOW+7200,'end':NOW+9000}]})   # but a real slot changes it
    assert guard(snap(store,clock.utc()),clock.utc())!=before
    # a refresh that lands while the solver is running does not abort the publication
    put(store,'cache_slots',{'identity':'x','acquired_at':NOW,'intervals':[]})
    calls=[]
    def solver_with_refresh(job):
        put(store,'cache_slots',{'identity':'x','acquired_at':NOW+5,'intervals':[]})
        calls.append(1);return solve(job)
    assert ShadowWorker(store,clock=clock,solver=solver_with_refresh).once()['status']=='published' and calls


def test_a_plan_with_no_extra_slots_does_not_expire_with_the_slot_cache_but_one_that_uses_a_slot_does(site):
    store,clock=site
    put(store,'cache_slots',{'identity':'x','acquired_at':NOW-100,'intervals':[]})
    plan=ShadowWorker(store,clock=clock,solver=solve).once()
    assert plan['status']=='published'
    summary=store.get_setting('shadow_plan')[1]
    assert summary['valid_until']-summary['issued_at']>200                                    # not cut to the 20 seconds left on the slot cache


def test_per_window_targets_end_to_end_and_tampering_is_caught(site):
    from solarbank_optimiser.models import PriceInterval
    from solarbank_optimiser.shadow_solver import cheap_windows
    value=job(site);result=solve(value);validate_result(value,result)
    windows=cheap_windows([PriceInterval(**p) for p in value['prices']])
    best=result['best']
    assert best['targets'][0]==best['target']==result['next_target'] and len(best['targets'])==(windows if len(best['targets'])>1 else 1)
    if len(best['targets'])>1:
        bad=deepcopy(result);bad['best']['targets'][-1]=1 if best['targets'][-1]!=1 else .05
        with pytest.raises(ValueError):validate_result(value,bad)


# ---- A9: owner pause and override -----------------------------------------------------------------------------------------------------
def test_an_owner_override_leaves_the_planner_one_charge_level_and_it_is_checked(site):
    store,clock=site;normal_history(store)
    put(store,'manual_override',{'target':.8,'valid_until':NOW+3600})
    value=job(site)
    assert value['targets']==[.8] and value['owner_override']=={'target':.8,'valid_until':NOW+3600}
    result=solve(value);validate_result(value,result)
    assert result['best']['targets']==[.8] and result['next_target']==.8


@pytest.mark.parametrize('payload',[{'target':.8,'valid_until':NOW-1},{'target':.01,'valid_until':NOW+3600},{'target':1.5,'valid_until':NOW+3600},
                                    {'target':'x','valid_until':NOW+3600},{'target':.8},{}])
def test_an_expired_or_malformed_override_is_ignored(site,payload):
    store,clock=site;normal_history(store)
    put(store,'manual_override',payload)
    value=job(site)
    assert value['owner_override'] is None and len(value['targets'])>5


def test_an_override_ends_the_plan_when_it_ends(site):
    from solarbank_optimiser.shadow import publish
    store,clock=site;normal_history(store)
    put(store,'manual_override',{'target':.8,'valid_until':NOW+1800})
    value=job(site);result=solve(value)
    summary=publish(store,value,result,clock.utc(),ShadowConfig())
    assert summary['valid_until']<=NOW+1800 and summary['owner_override']['target']==.8


def test_a_pause_makes_the_controller_plan_ask_for_no_charging_but_advice_continues(site,tmp_path):
    import json as _json
    from solarbank_optimiser.shadow import controller_plan_document,publish_controller_plan
    store,clock=site;normal_history(store)
    value=job(site);result=solve(value)
    from solarbank_optimiser.shadow import publish
    summary=publish(store,value,result,clock.utc(),ShadowConfig())
    path=tmp_path/'plan.json'
    publish_controller_plan(path,store,summary,NOW)
    assert _json.loads(path.read_text())['source']=='shadow_plan'
    put(store,'automation_pause',{'paused':True,'until':NOW+600})
    publish_controller_plan(path,store,summary,NOW)
    document=_json.loads(path.read_text())
    assert document['source']=='owner_pause' and document['action']=='normal' and document['target_soc'] is None and 'window' not in document
    publish_controller_plan(path,store,summary,NOW+601)                                  # the pause has run out
    assert _json.loads(path.read_text())['source']=='shadow_plan'
    put(store,'automation_pause',{'paused':True,'until':None})
    publish_controller_plan(path,store,summary,NOW+10**6)
    assert _json.loads(path.read_text())['source']=='owner_pause'                       # no end time: paused until resumed
    put(store,'automation_pause',{'paused':False})
    publish_controller_plan(path,store,summary,NOW+10**6+1)
    assert _json.loads(path.read_text())['source']=='shadow_plan'


def test_the_next_replan_comes_before_a_short_lived_plan_expires():
    """#23: a plan that uses smart slots lives 120 s, shorter than the 240 s replan interval."""
    from types import SimpleNamespace
    from solarbank_optimiser.shadow import ShadowWorker, REPLAN_SECONDS
    plan = {"valid_until": 1000.0 + 120}
    worker = SimpleNamespace(clock=SimpleNamespace(utc=lambda: 1000.0), store=SimpleNamespace(get_setting=lambda name: (1, plan)))
    assert ShadowWorker.next_replan(worker) == 1000.0 + 90                    # 30 s before it lapses
    plan["valid_until"] = 1000.0 + 5000
    assert ShadowWorker.next_replan(worker) == 1000.0 + REPLAN_SECONDS         # a long plan keeps the usual interval
    plan["valid_until"] = 1000.0 + 10
    assert ShadowWorker.next_replan(worker) == 1000.0 + REPLAN_SECONDS         # a plan about to end anyway is not chased every 15 s
    worker.store.get_setting = lambda name: (0, None)
    assert ShadowWorker.next_replan(worker) == 1000.0 + REPLAN_SECONDS


@pytest.mark.parametrize('how',['missing','too_old','switched_off'])
def test_planning_continues_without_a_usable_weather_forecast_assuming_no_solar(site,how):
    """#32: no forecast no longer stops planning: the plan assumes NO solar output (charges more from the grid rather than risk running short) and says so."""
    store,clock=site
    seen={}
    def capture(job):
        seen['job']=job
        return solve(job)
    if how=='missing':put(store,'cache_weather',{})
    elif how=='too_old':
        old=store.get_setting('cache_weather')[1]
        put(store,'cache_weather',{**old,'acquired_at':NOW-30*3600})
    else:put(store,'providers',{**store.get_setting('providers')[1],'weather_enabled':False})
    attempt=ShadowWorker(store,clock=clock,solver=capture).once()
    assert attempt['status']!='unavailable',attempt
    job=seen['job']
    assert job['solar_fallback'] and any('no solar output' in text for text in job['assumptions'])
    assert all(r['pv_kw']==0 for case in job['scenarios'] for r in case['intervals'])
    assert store.get_setting('shadow_plan')[1] is not None


def test_the_plan_carries_an_hourly_series_in_the_ankers_four_quantities_and_a_csv(site):
    from solarbank_optimiser.plan_series import HEADER, hourly, to_csv
    store,clock=site
    ShadowWorker(store,clock=clock,solver=solve).once()
    plan=store.get_setting('shadow_plan')[1]
    series=plan['series']
    assert series and all(set(r)>={'solar_kwh','battery_kwh','home_kwh','grid_import_kwh','grid_export_kwh','soc_end','price_gbp_per_kwh'} for r in series)
    assert all(0<=r['soc_end']<=1.0001 for r in series) and all(r['grid_import_kwh']>=-1e-9 for r in series)
    text=to_csv(series)
    assert text.splitlines()[0]==','.join(HEADER) and len(text.splitlines())==len(series)+1


def test_the_hourly_series_adds_up_to_the_flows_it_came_from():
    from solarbank_optimiser.plan_series import hourly
    flows=[{'start':0.,'end':1800.,'pv_kw':1.,'pv_charge_kw':.4,'grid_charge_kw':0.,'discharge_kw':0.,'demand_kw':.6,'import_kw':0.,'after_kwh':5.},
           {'start':1800.,'end':3600.,'pv_kw':1.,'pv_charge_kw':0.,'grid_charge_kw':2.,'discharge_kw':0.,'demand_kw':.6,'import_kw':2.6,'after_kwh':6.}]
    prices=[{'start':0.,'end':1800.,'price':.35},{'start':1800.,'end':3600.,'price':.066}]
    row=hourly(flows,prices,10.)[0]
    assert row['solar_kwh']==1.0 and row['home_kwh']==.6 and row['battery_kwh']==1.2 and row['grid_import_kwh']==1.3 and row['soc_end']==.6
