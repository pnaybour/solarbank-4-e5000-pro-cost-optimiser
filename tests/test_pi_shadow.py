from threading import Event
import pytest
from solarbank_optimiser.models import Observation,Quality,FakeClock
from solarbank_optimiser.store import Store
from solarbank_optimiser.minutes import MinuteRecorder,PROVENANCE
from solarbank_optimiser.services import ProviderWorker
from solarbank_optimiser.shadow import ShadowWorker


def sample(t,value=1.,quality=Quality.OK,mode='Normal consumption',revision=0):
    return Observation('monitor:load','household_power',t,value,'kW',quality,
        source_at=t,demand_mode=mode,demand_revision=revision,provenance='shared-monitor-observation')


def test_minute_mean_and_coverage_survive_reopening(tmp_path):
    store=Store(tmp_path/'db.sqlite3');store.initialise();worker=MinuteRecorder(store)
    for t in range(0,121,2):worker.observe(sample(t,1+t/60))
    rows=Store(store.path).observations(metric='household_power',covered_only=True)
    assert len(rows)==2
    assert rows[0]['value']==pytest.approx(1.5)
    assert rows[1]['value']==pytest.approx(2.5)
    assert all(r['provenance']==PROVENANCE and r['coverage_seconds']==60 for r in rows)


@pytest.mark.parametrize('fault',['gap','invalid','restart','tag','repeated'])
def test_gaps_and_restarts_never_become_full_minutes(tmp_path,fault):
    store=Store(tmp_path/'db.sqlite3');store.initialise();worker=MinuteRecorder(store)
    for t in range(0,61,2):
        if fault=='gap' and 26<=t<=34:continue
        if fault=='restart' and t==30:worker=MinuteRecorder(store)
        mode='Away from home' if fault=='tag' and t==30 else 'Normal consumption'
        worker.observe(sample(t,None if fault=='invalid' and t==30 else 1.,Quality.ERROR if fault=='invalid' and t==30 else Quality.OK,mode))
        if fault=='repeated':worker.observe(sample(t))
    rows=store.observations(metric='household_power',covered_only=True)
    assert len(rows)==(1 if fault=='repeated' else 0)


def test_scheduled_short_visit_inside_sample_gap_excludes_minute(tmp_path):
    store=Store(tmp_path/'db.sqlite3');store.initialise()
    store.set_setting('demand',{'mode':'Away from home','start':30.5,'end':31.5,'away_watts':200.},0,0)
    recorder=MinuteRecorder(store)
    for t in range(0,61,2):recorder.observe(sample(t))
    assert not store.observations(covered_only=True)


def test_continuous_provider_stops_on_signal(tmp_path,monkeypatch):
    store=Store(tmp_path/'db.sqlite3');store.initialise();stop=Event();worker=ProviderWorker(store)
    calls=[]
    def once():calls.append(True);stop.set()
    monkeypatch.setattr(worker,'once',once)
    worker.run(stop,None)
    assert calls==[True]


def test_continuous_shadow_stops_on_signal(tmp_path,monkeypatch):
    store=Store(tmp_path/'db.sqlite3');store.initialise();stop=Event();worker=ShadowWorker(store,clock=FakeClock(100))
    calls=[]
    def once():calls.append(True);stop.set()
    monkeypatch.setattr(worker,'once',once)
    worker.run(stop,None)
    assert calls==[True]


def test_env_storage_private_no_expansion_and_legacy_fallback(tmp_path):
    from solarbank_optimiser.services import SecretFile
    from solarbank_optimiser.providers import ProviderError
    legacy=SecretFile(tmp_path/'octopus-api-key');legacy.write('fixture-legacy-key')
    secret=SecretFile(tmp_path/'.env')
    assert secret.read()=='fixture-legacy-key'
    secret.write('fixture-new-key')
    assert secret.read()=='fixture-new-key'
    assert secret.path.read_text()=='OCTOPUS_API_KEY=fixture-new-key\n'
    assert not secret.path.stat().st_mode&0o077
    secret.path.write_text("OTHER=$(touch should-never-run)\nOCTOPUS_API_KEY='fixture-quoted-key'\n")
    assert secret.read()=='fixture-quoted-key'
    secret.write('fixture-replaced-key')
    assert 'OTHER=$(touch should-never-run)' in secret.path.read_text()
    secret.path.chmod(0o644)
    with pytest.raises(ProviderError):secret.read()


def test_connect_octopus_discovers_one_device_and_does_not_verify_billing(tmp_path):
    from test_phase3 import account_transport,NOW
    from solarbank_optimiser.services import ProviderConfig
    store=Store(tmp_path/'db.sqlite3');store.initialise()
    store.set_setting('providers',ProviderConfig(account='A-FIXTURE').model_dump(),NOW,0)
    store.set_setting('octopus_connect_request',{'requested_at':NOW},NOW,0)
    transport,calls=account_transport([])
    worker=ProviderWorker(store,clock=FakeClock(NOW),transport=transport)
    worker.secrets.write('fixture-test-key')
    worker.once()
    config=store.get_setting('providers')[1]
    assert config['device']=='fixture-device' and config['account_enabled']
    assert not config['household_rate_verified']
    assert store.get_setting('provider_slots')[1]['status']=='ok'
    assert store.get_setting('octopus_connection')[1]['status']=='connected'
    assert all('obtainKrakenToken' in c[0]['query'] or c[0]['query'].startswith('query') for c in calls)
    before=len(calls);worker.once();assert len(calls)==before
    with store.connect() as db:
        assert 'fixture-test-key' not in str([tuple(r) for r in db.execute('SELECT payload FROM settings')])


def test_connect_multiple_devices_requires_choice(tmp_path):
    from solarbank_optimiser.services import ProviderConfig
    store=Store(tmp_path/'db.sqlite3');store.initialise()
    store.set_setting('providers',ProviderConfig(account='A-FIXTURE').model_dump(),100,0)
    store.set_setting('octopus_connect_request',{},100,0)
    worker=ProviderWorker(store,clock=FakeClock(100));worker.secrets.write('fixture-test-key')
    worker.account.linked_devices=lambda *args:[{'id':'one','deviceType':'CHARGE_POINTS'},{'id':'two','deviceType':'ELECTRIC_VEHICLES'}]
    worker.connect_octopus(100)
    assert store.get_setting('octopus_connection')[1]['status']=='choose_device'
    assert not store.get_setting('providers')[1]['account_enabled']


def test_connect_without_key_is_explicit_and_web_is_guarded(tmp_path):
    from fastapi.testclient import TestClient
    from solarbank_optimiser.web.app import create_app,WebConfig
    store=Store(tmp_path/'db.sqlite3');store.initialise()
    with TestClient(create_app(WebConfig(store.path,'fixture-password', status_cache_seconds=0),FakeClock(100))) as client:
        assert client.put('/api/octopus-connect').status_code==401
        client.auth=('solarbank','fixture-password')
        assert client.put('/api/octopus-connect').status_code==403
        csrf=client.get('/api/status').json()['csrf_token']
        assert client.put('/api/octopus-connect',headers={'Origin':'http://testserver','X-CSRF-Token':csrf}).status_code==200
    worker=ProviderWorker(store,clock=FakeClock(100));worker.connect_octopus(100)
    assert store.get_setting('octopus_connection')[1]['reason']=='no_api_key'


def register_raw(price,start='2026-10-01T00:00:00Z',end=None):
    import json
    return json.dumps({'results':[{'value_inc_vat':price,'valid_from':start,'valid_to':end}],'next':None}).encode()


def test_register_rates_live_day_night_and_default_window_changes(tmp_path):
    import json
    from solarbank_optimiser.services import ProviderConfig,tariff_horizon
    from solarbank_optimiser.settings import london_input
    now=london_input('2026-10-25T22:00')
    store=Store(tmp_path/'db.sqlite3');store.initialise()
    cfg=ProviderConfig(prices_enabled=True,product='FIXTURE',tariff='E-1R-FIXTURE-E')
    store.set_setting('providers',cfg.model_dump(),now,0)
    tariff={'day_pence':35.,'cheap_pence':7.,'valid_from':0.,'confirmed':True,'start_minute':1410,'end_minute':330}
    store.set_setting('fallback_tariff',tariff,now,0)
    def transport(host,path,*args):
        if '/day-unit-rates/' in path:return register_raw(34.9561)
        if '/night-unit-rates/' in path:return register_raw(6.6)
        return b'{"results":[],"next":null}'
    worker=ProviderWorker(store,clock=FakeClock(now),transport=transport);worker.once()
    result=tariff_horizon(store,now,now,now+8*3600)
    assert result['intervals'][0]['price']==pytest.approx(.349561)
    assert result['intervals'][0]['source'].startswith('API')
    assert any(r['cheap'] and r['price']==.066 for r in result['intervals'])
    # The archived register prices are mapped using current confirmed settings,
    # not a hidden frozen copy of the window from acquisition time.
    tariff['start_minute']=1380
    store.set_setting('fallback_tariff',tariff,now,1)
    period=london_input('2026-10-25T23:10')
    assert tariff_horizon(store,now,period,period+60)['intervals'][0]['price']==.066


def test_register_rates_reject_overlap_and_missing_partner():
    import json
    from solarbank_optimiser.providers import register_prices,ProviderError
    from solarbank_optimiser.settings import london_input
    start=london_input('2026-10-25T12:00');end=start+3600
    raw=register_raw(34.9561)
    assert register_prices(raw,register_raw(6.6),start,end)==[{'start':start,'end':end,'day_price':.349561,'night_price':.066}]
    duplicate=json.loads(raw);duplicate['results']*=2
    with pytest.raises(ProviderError):register_prices(json.dumps(duplicate).encode(),raw,start,end)
    with pytest.raises(ProviderError):register_prices(raw,b'{"results":[],"next":null}',start,end)


@pytest.mark.parametrize('peer,expected',[('192.168.1.30',200),('100.72.1.20',200),('127.0.0.1',200),('203.0.113.4',403)])
def test_private_network_has_no_login_and_blocks_other_peers(tmp_path,peer,expected):
    from fastapi.testclient import TestClient
    from solarbank_optimiser.web.app import WebConfig,create_app
    config=WebConfig(tmp_path/'db.sqlite3','fixture-password',trusted_network=True, status_cache_seconds=0)
    with TestClient(create_app(config),base_url='http://solarbank-pi.local',client=(peer,1234)) as client:
        response=client.get('/api/status')
        assert response.status_code==expected
        assert 'www-authenticate' not in response.headers
        if expected==200:
            assert client.get('/').status_code==200
            assert client.get('/api/status',headers={'Authorization':'Basic stale-invalid-header'}).status_code==200
            assert client.put('/api/shadow-refresh').status_code==403
            headers={'Origin':'http://solarbank-pi.local','X-CSRF-Token':response.json()['csrf_token']}
            assert client.put('/api/shadow-refresh',headers=headers).status_code==200


def test_daily_reference_is_a_sense_check_and_never_fills_missing_profile():
    from solarbank_optimiser.sense_check import compare_daily
    from solarbank_optimiser.energy import day_bounds
    from datetime import date
    reference={'mean_kwh':12.24,'sample_sd_kwh':2.66,'sample_days':12}
    assert compare_daily(reference,None)['status']=='awaiting_complete_normal_forecast'
    start,end=day_bounds(date(2026,10,26))
    demand={'intervals':[{'start':start,'end':end,'demand_kw':.5,'demand_mode':'Normal consumption'}]}
    result=compare_daily(reference,demand)
    assert result['days'][0]['forecast_kwh']==12.
    assert result['status']=='within_reference_range'
    demand['intervals'][0]['demand_kw']=1.
    assert compare_daily(reference,demand)['status']=='check_needed'
    demand['intervals'][0]['demand_mode']='High demand'
    assert not compare_daily(reference,demand)['days']
