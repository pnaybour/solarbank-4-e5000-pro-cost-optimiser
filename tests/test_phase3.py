"""Phase 3 acceptance cases: all provider access is injected and offline."""
from contextlib import closing
from datetime import date, datetime, timezone
import base64
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.energy import day_bounds
from solarbank_optimiser.forecasts import demand_horizon
from solarbank_optimiser.models import FakeClock, Observation, encoded
from solarbank_optimiser.providers import (OctopusAccount, ProviderError, public_prices,
                                         prices_path, solar_forecast)
from solarbank_optimiser.services import (WEATHER_MAX_AGE_S, ProviderConfig, ProviderWorker, SecretFile, cache_identity,
                                         provider_view, slot_intervals, tariff_horizon)
from solarbank_optimiser.store import APPLICATION_ID, MIGRATION_2, SCHEMA, Store
from solarbank_optimiser.web.app import WebConfig, create_app

NOW = datetime(2026, 10, 25, 8, tzinfo=timezone.utc).timestamp()
PASSWORD = "phase-3-fixture-password"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "controller.sqlite3")
    s.initialise()
    return s


def put(s, name, value, now=NOW):
    return s.set_setting(name, value, now, s.get_setting(name)[0])


def fallback(s, **changes):
    value = dict(day_pence=30., cheap_pence=8., valid_from=0., start_minute=1410, end_minute=330)
    put(s, "fallback_tariff", value | changes)


def normal(day=date(2026, 10, 24), kw=1.):
    start, end = day_bounds(day)
    return [Observation("verified-fixture", "household_power", t+60, kw, "kW", interval_start=t,
                        coverage_seconds=60., demand_mode="Normal consumption", demand_revision=0)
            for t in range(int(start), int(end), 60)]


def config(**changes):
    return ProviderConfig(**(dict(account_enabled=True, account="A-FIXTURE", device="fixture-device",
                                 household_rate_verified=True, eligibility_until=NOW+86400,
                                 eligibility_source="Fixture account terms only") | changes))


def cache(s, name, cfg, intervals, acquired=NOW):
    put(s, "cache_"+name, {"identity": cache_identity(cfg,name), "acquired_at": acquired, "intervals": intervals})


def test_48_hour_profile_anticipates_future_visit_and_returns_normal():
    selection = dict(mode="High demand", start=NOW+12*3600, end=NOW+36*3600, high_extra_kwh=6.)
    result = demand_horizon(normal(), selection, 7, NOW, NOW, NOW+48*3600)
    assert result["reference_day"] == "2026-10-24"
    assert result["demand_kwh"] == pytest.approx(53.96)
    assert result["intervals"][0]["demand_kw"] == 1
    assert result["intervals"][12*60]["demand_kw"] == 1.24
    assert result["intervals"][36*60]["demand_mode"] == "Normal consumption"
    assert all(r["demand_revision"] == 7 for r in result["intervals"])


def test_away_without_normal_data_and_return_requires_normal():
    selection = dict(mode="Away from home", start=NOW, away_watts=200.)
    assert demand_horizon([],selection,1,NOW,NOW,NOW+48*3600)["demand_kwh"] == pytest.approx(9.6)
    with pytest.raises(ValueError, match="No recent complete"):
        demand_horizon([], selection | {"end": NOW+3600}, 1, NOW, NOW, NOW+48*3600)


@pytest.mark.parametrize("day,hours", [(date(2026,10,25),25), (date(2027,3,28),23)])
def test_high_allowance_is_exact_on_london_clock_change(day,hours):
    left,right = day_bounds(day)
    # A complete autumn reference includes all clock minutes; spring source does not.
    source = normal(date(2026,10,24) if day.year==2026 else date(2027,3,27))
    selection = dict(mode="High demand",start=left,high_extra_kwh=5.)
    result = demand_horizon(source,selection,2,left,left,right)
    assert result["demand_kwh"] == pytest.approx(hours+5)


def test_reference_never_uses_future_or_partial_rows():
    rows = normal()
    rows[-1] = Observation("fixture","household_power",NOW+1,99.,"kW",interval_start=rows[-1].sample_at,
                           coverage_seconds=60.,demand_mode="Normal consumption")
    # The future row is never used. Its minute is now one missing minute, which the (small, visible) gap tolerance fills from the
    # previous minute; the value 99 kW must appear nowhere.
    forecast = demand_horizon(rows,None,0,NOW,NOW,NOW+3600)
    assert forecast["gap_filled_minutes"] == 1 and max(r["demand_kw"] for r in forecast["intervals"]) < 90
    # Too many future/partial rows is still "no complete day".
    many = normal()
    for i in range(1, 13):
        many[-i] = Observation("fixture","household_power",NOW+1,1.,"kW",interval_start=many[-i].sample_at,
                               coverage_seconds=60.,demand_mode="Normal consumption")
    with pytest.raises(ValueError,match="No recent complete"):
        demand_horizon(many,None,0,NOW,NOW,NOW+3600)


def test_settings_chronology_and_immutability(store):
    put(store,"demand",dict(mode="Away from home",start=0.,away_watts=100.),NOW)
    put(store,"demand",dict(mode="High demand",start=0.,high_extra_kwh=5.),NOW+100)
    assert store.setting_at("demand",NOW-1)==(0,None)
    assert store.setting_at("demand",NOW+50)[1]["mode"]=="Away from home"
    with closing(store.connect()) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE setting_versions SET payload='{}'")


def test_schema2_migration_backup_and_rollback_on_failure(tmp_path,monkeypatch):
    import solarbank_optimiser.store as module
    path=tmp_path/"old.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        for sql in SCHEMA+MIGRATION_2:
            db.execute(sql)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=2")
        db.execute("INSERT INTO settings VALUES('demand',1,'{}')")
        db.execute("INSERT INTO events(time,kind,payload) VALUES(10,'settings_changed',?)",(encoded({"name":"demand","revision":1}),))
    original=module.MIGRATION_3
    monkeypatch.setattr(module,"MIGRATION_3",original+("INVALID SQL",))
    with pytest.raises(sqlite3.OperationalError):
        Store(path).initialise()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0]==2
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='setting_versions'").fetchall()
    monkeypatch.setattr(module,"MIGRATION_3",original)
    s=Store(path);s.initialise()
    assert s.setting_at("demand",9)==(0,None)
    assert s.setting_at("demand",10)==(1,{})
    backup=s.backup(tmp_path/"backup.sqlite3")
    restored=Store(tmp_path/"restored.sqlite3");restored.restore(backup)
    assert restored.setting_at("demand",10)==(1,{})


def test_price_interval_fallback_cache_expiry_and_identity(store):
    cfg=ProviderConfig(prices_enabled=True,product="FIXTURE",tariff="E-1R-FIXTURE-A")
    put(store,"providers",cfg.model_dump());fallback(store)
    cache(store,"prices",cfg,[{"start":NOW,"end":NOW+1800,"price":.2}])
    put(store,"provider_prices",{"status":"ok"})
    result=tariff_horizon(store,NOW,NOW,NOW+3600)
    assert [(r["price"],r["source"]) for r in result["intervals"]]==[(.2,"API"),(.3,"Fallback defaults")]
    put(store,"provider_prices",{"status":"failed"})
    assert tariff_horizon(store,NOW,NOW,NOW+1)["intervals"][0]["source"]=="cache"
    assert not provider_view(store,NOW+86401)[2]["prices"]["cache_valid"]
    cfg=cfg.model_copy(update={"tariff":"E-1R-FIXTURE-B"});put(store,"providers",cfg.model_dump())
    assert tariff_horizon(store,NOW,NOW,NOW+1)["intervals"][0]["price"]==.3


def test_api_price_precedes_fallback_even_in_night_window(store):
    cfg=ProviderConfig(prices_enabled=True,product="FIXTURE",tariff="E-1R-FIXTURE-A")
    put(store,"providers",cfg.model_dump());fallback(store,start_minute=0,end_minute=1439)
    cache(store,"prices",cfg,[{"start":NOW,"end":NOW+1800,"price":-.02}])
    assert tariff_horizon(store,NOW,NOW,NOW+1)["intervals"][0]["price"]==-.02
    assert tariff_horizon(store,NOW,NOW,NOW+1)["intervals"][0]["cheap"]


def test_expired_fallback_stays_missing_not_zero(store):
    fallback(store,valid_until=NOW+30)
    r=tariff_horizon(store,NOW,NOW,NOW+60)
    assert not r["complete"]
    assert r["intervals"][-1]["price"] is None


def test_keyless_worker_does_not_disable_defaults(store):
    cfg=config(household_rate_verified=False)
    put(store,"providers",cfg.model_dump());fallback(store)
    worker=ProviderWorker(store,clock=FakeClock(NOW),transport=lambda *a:pytest.fail("No key must not connect"))
    worker.once()
    assert store.get_setting("provider_slots")[1]["reason"]=="no_api_key"
    assert tariff_horizon(store,NOW,NOW,NOW+48*3600)["complete"]
    assert store.get_setting("forecast_summary")[1]["demand_status"]=="ok"              # no Normal day: the seeded default stands in (9 November); the missing key only affects slots
    assert store.get_setting("forecast_summary")[1]["pv_kwh"] is None


def test_valid_empty_cancels_future_but_retains_only_established_current_interval(store):
    cfg=config();put(store,"providers",cfg.model_dump());fallback(store)
    current={"start":NOW-60,"end":NOW+600};future={"start":NOW+3600,"end":NOW+4200}
    cache(store,"slots",cfg,[current,future])
    put(store,"established_slots",{"identity":cache_identity(cfg,"slots"),"intervals":[current]})
    cache(store,"slots",cfg,[])
    assert slot_intervals(store,cfg,NOW)==[current]
    assert slot_intervals(store,cfg,NOW+600)==[]
    # Revoked eligibility invalidates even the established interval immediately.
    assert not slot_intervals(store,cfg.model_copy(update={"household_rate_verified":False}),NOW)


def test_future_slot_cache_never_extends_after_expiry(store):
    cfg=config();cache(store,"slots",cfg,[{"start":NOW+100,"end":NOW+600}])
    assert slot_intervals(store,cfg,NOW+119)
    assert not slot_intervals(store,cfg,NOW+121)


def account_transport(rows, device="fixture-device", errors=False):
    calls=[]
    payload=base64.urlsafe_b64encode(json.dumps({"exp":NOW+600}).encode()).decode().rstrip("=")
    def transport(host,path,body=None,token=None):
        calls.append((body,token))
        query=body["query"]
        if "obtainKrakenToken" in query:
            expiry = NOW + 600 * sum("obtainKrakenToken" in c[0]["query"] for c in calls)
            payload = base64.urlsafe_b64encode(json.dumps({"exp":expiry}).encode()).decode().rstrip("=")
            return encoded({"data":{"obtainKrakenToken":{"token":"x."+payload+".x"}}}).encode()
        if "devices(" in query:
            return encoded({"data":{"devices":[{"id":device,"deviceType":"ELECTRIC_VEHICLES"}]}}).encode()
        return encoded({"data":{"flexPlannedDispatches":rows},**({"errors":[{"message":"SECRET from upstream"}]} if errors else {})}).encode()
    return transport,calls


def dispatch(kind="SMART",start=NOW,end=NOW+600):
    stamp=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    return {"start":stamp(start),"end":stamp(end),"type":kind,"energyAddedKwh":"1.0"}


def test_graphql_current_fields_token_reuse_and_refresh():
    transport,calls=account_transport([dispatch(),dispatch("BOOST"),dispatch("TEST")])
    client=OctopusAccount(transport)
    assert client.dispatches("test-fixture-key","A-FIXTURE","fixture-device",NOW)==[{"start":NOW,"end":NOW+600}]
    client.dispatches("test-fixture-key","A-FIXTURE","fixture-device",NOW+100)
    assert len(calls)==5
    client.dispatches("test-fixture-key","A-FIXTURE","fixture-device",NOW+550)
    assert len(calls)==8
    assert calls[0][0]["variables"]=={"input":{"APIKey":"test-fixture-key"}}
    assert "flexPlannedDispatches" in calls[2][0]["query"]
    assert "deviceId" in calls[2][0]["variables"]


@pytest.mark.parametrize("wrong_device,errors,reason",[("other-device",False,"linked_device_not_verified"),("fixture-device",True,"graphql_error")])
def test_wrong_account_device_and_error_bearing_graphql(wrong_device,errors,reason):
    transport,_=account_transport([],device=wrong_device,errors=errors)
    with pytest.raises(ProviderError,match=reason):
        OctopusAccount(transport).dispatches("test-fixture-key","A-WRONG","fixture-device",NOW)


def test_empty_account_list_is_success_and_secret_not_archived(store):
    cfg=config();put(store,"providers",cfg.model_dump())
    transport,_=account_transport([])
    worker=ProviderWorker(store,clock=FakeClock(NOW),transport=transport)
    worker.secrets.write("test-fixture-key");worker.once()
    assert store.get_setting("provider_slots")[1]["status"]=="ok"
    assert store.get_setting("cache_slots")[1]["intervals"]==[]
    with closing(store.connect()) as db:
        serialized=str(db.execute("SELECT payload,raw FROM issuances").fetchall())
    assert "test-fixture-key" not in serialized


@pytest.mark.parametrize("reason",["connection_failed","rate_limited","invalid_response"])
def test_failures_backoff_recovery_and_no_exception_secret_leak(store,reason):
    cfg=ProviderConfig(prices_enabled=True,product="FIXTURE",tariff="E-1R-FIXTURE-A")
    put(store,"providers",cfg.model_dump())
    calls=[]
    def transport(*args):
        calls.append(args)
        if len(calls)==1:
            if reason=="invalid_response":raise ValueError("private-key-should-not-appear")
            raise ProviderError(reason)
        return encoded({"results":[{"valid_from":datetime.fromtimestamp(NOW-1,timezone.utc).isoformat(),
                          "valid_to":datetime.fromtimestamp(NOW+72*3600,timezone.utc).isoformat(),
                          "value_inc_vat":30.}],"next":None}).encode()
    clock=FakeClock(NOW);worker=ProviderWorker(store,clock=clock,transport=transport)
    worker.once();worker.once()
    assert len(calls)==1
    stored=store.get_setting("provider_prices")[1]["reason"]
    assert stored==(reason+":ValueError" if reason=="invalid_response" else reason)      # the exception CLASS is kept (where decoding broke), never its message
    assert "private-key" not in stored
    clock.advance(61);worker.once()
    assert len(calls)==3                     # the prices, then the product description (where Octopus states the cheap window)
    assert store.get_setting("provider_prices")[1]["status"]=="ok"


def weather_fixture():
    from pathlib import Path
    return json.loads((Path(__file__).parent/"fixtures/weather-two-plane.json").read_text())


def test_pvlib_weather_retains_hour_averaging_and_two_planes():
    result=solar_forecast(encoded(weather_fixture()).encode(),NOW)
    assert result["intervals"][0]["start"]==NOW
    assert result["intervals"][0]["end"]==NOW+3600
    assert len(result["planes"])==2
    assert result["planes"][0]["tilt"]==80
    assert any(r["pv_kw"]>0 for r in result["intervals"])
    assert all(r["pv_kw"]>=0 for r in result["intervals"])
    assert result["acquired_at"]==NOW
    assert result["provider_model_issue_at"] is None


@pytest.mark.parametrize("failure",["location","units","missing","time","short"])
def test_weather_bad_inputs_no_synthetic_solar(failure):
    data=weather_fixture()
    if failure=="location":data["latitude"]=0
    if failure=="units":data["hourly_units"]["wind_speed_10m"]="km/h"
    if failure=="missing":data["hourly"]["shortwave_radiation"][5]=None
    if failure=="time":data["hourly"]["time"][5]+=1
    if failure=="short":
        for key in data["hourly"]:data["hourly"][key]=data["hourly"][key][:10]
    with pytest.raises((ValueError,TypeError)):
        solar_forecast(encoded(data).encode(),NOW)


def test_failed_weather_preserves_cache_then_expires_without_refresh_storm(store):
    cfg=ProviderConfig(weather_enabled=True);put(store,"providers",cfg.model_dump())
    clock=FakeClock(NOW);calls=[]
    raw=encoded(weather_fixture()).encode()
    def transport(*args):
        calls.append(args)
        if len(calls)>1:raise ProviderError("connection_failed")
        return raw
    worker=ProviderWorker(store,clock=clock,transport=transport)
    worker.once()
    first=store.get_setting("cache_weather")[1]["issuance_id"]
    put(store,"provider_refresh",{"requested_at":NOW+1})
    clock.advance(10);worker.once();assert len(calls)==1
    clock.advance(3590);worker.once()
    assert store.get_setting("cache_weather")[1]["issuance_id"]==first
    assert provider_view(store,clock.utc())[2]["weather"]["cache_valid"]
    clock.advance(6*3600)
    # Issue #32: a forecast more than 6 hours old is still used (reported as stale) until WEATHER_MAX_AGE_S, rather than stopping all planning.
    assert provider_view(store,clock.utc())[2]["weather"]["cache_valid"]
    clock.advance(WEATHER_MAX_AGE_S)
    assert not provider_view(store,clock.utc())[2]["weather"]["cache_valid"]
    with closing(store.connect()) as db:
        assert bytes(db.execute("SELECT raw FROM issuances WHERE id=?",(first,)).fetchone()[0])==raw


def test_price_decoder_rejects_overlap_pagination_nonfinite_and_mismatched_identity():
    row={"valid_from":"2026-10-25T00:00Z","valid_to":"2026-10-25T01:00Z","value_inc_vat":-2.}
    assert public_prices(encoded({"results":[row],"next":None}).encode())[0]["price"]==-.02
    for data in ({"results":[row,row]}, {"results":[row],"next":"page2"},
                 {"results":[row|{"value_inc_vat":True}]}, {"results":[row|{"valid_from":"2026-10-25T00:00"}]}):
        with pytest.raises(ValueError):public_prices(encoded(data).encode())
    with pytest.raises(ValueError):prices_path("FIXTURE","E-1R-OTHER-A",NOW)


def web(s,clock=None):
    client=TestClient(create_app(WebConfig(s.path,PASSWORD, status_cache_seconds=0),clock or FakeClock(NOW)),base_url="https://testserver")
    client.auth=("solarbank",PASSWORD)
    return client


def headers(client):
    return {"Origin":"https://testserver","X-CSRF-Token":client.get("/api/status").json()["csrf_token"]}


def test_website_london_repeated_clock_and_outage_expiry(store):
    clock=FakeClock(NOW);client=web(store,clock)
    body={"revision":0,"mode":"Away from home","away_watts":100.,"start_local":"2026-10-25T01:30",
          "end_local":"2026-10-25T02:00"}
    assert client.put("/api/demand-schedule",json=body,headers=headers(client)).status_code==422
    body["start_fold"]=1
    assert client.put("/api/demand-schedule",json=body,headers=headers(client)).status_code==200
    assert client.get("/api/status").json()["demand_dates"]["start_fold"]==1
    assert client.get("/api/status").json()["active_demand"]=="Normal consumption"
    body|={"revision":1,"start_local":"2027-03-28T01:30","end_local":None}
    assert client.put("/api/demand-schedule",json=body,headers=headers(client)).status_code==422


def test_credential_endpoint_never_echoes_and_exports_exclude_keys(store):
    client=web(store)
    secret="private-fixture-credential"
    result=client.put("/api/octopus-key",json={"api_key":secret},headers=headers(client))
    assert result.status_code==200
    assert SecretFile(store.path.parent/".env").read()==secret
    assert (store.path.parent/".env").stat().st_mode&0o077==0
    invalid=client.put("/api/octopus-key",json={"api_key":secret,"unexpected":secret},headers=headers(client))
    assert invalid.status_code==422 and secret not in invalid.text
    for path in ["/api/status","/history.csv?start=2026-10-24&end=2026-10-25","/api/forecast"]:
        assert secret not in client.get(path).text
    with closing(store.connect()) as db:
        assert secret not in " ".join(str(tuple(r)) for r in db.execute("SELECT payload FROM settings"))


def test_slow_provider_cannot_hold_website_requests(store):
    cfg=ProviderConfig(weather_enabled=True);put(store,"providers",cfg.model_dump())
    entered,release=threading.Event(),threading.Event()
    def slow(*args):
        entered.set();release.wait(5);raise ProviderError("connection_failed")
    worker=ProviderWorker(store,clock=FakeClock(NOW),transport=slow)
    thread=threading.Thread(target=worker.once);thread.start()
    try:
        assert entered.wait(1)
        client=web(store)
        assert client.get("/health").status_code==200
        assert client.get("/api/status").status_code==200
        assert client.put("/api/provider-refresh",json={},headers=headers(client)).json()["queued"]
    finally:
        release.set();thread.join(5)
    assert not thread.is_alive()


def test_total_transport_deadline_and_secret_only_in_pipe(monkeypatch):
    import subprocess
    from solarbank_optimiser.providers import request
    seen={}
    def timeout(args, **kwargs):
        seen.update(args=args,**kwargs)
        raise subprocess.TimeoutExpired(args,12)
    monkeypatch.setattr(subprocess,"run",timeout)
    with pytest.raises(ProviderError,match="connection_failed"):
        request("api.octopus.energy","/v1/graphql/",{"secret":"fixture-key"},"fixture-token")
    assert seen["timeout"]==12
    assert "fixture-key" not in str(seen["args"])
    assert "fixture-token" not in str(seen["args"])
    assert b"fixture-key" in seen["input"]


def test_remote_plain_http_cannot_accept_key(store):
    client=TestClient(create_app(WebConfig(store.path,PASSWORD, status_cache_seconds=0),FakeClock(NOW)))
    client.auth=("solarbank",PASSWORD)
    h={"Origin":"http://testserver","X-CSRF-Token":client.get("/api/status").json()["csrf_token"]}
    assert client.put("/api/octopus-key",json={"api_key":"fixture-private"},headers=h).status_code==403
    assert not (store.path.parent/".env").exists()


def test_sensitive_values_in_malformed_settings_are_not_echoed(store):
    client=web(store)
    result=client.put("/api/providers",json={"revision":0,"config":{"api_key":"fixture-secret-pasted-wrongly"}},headers=headers(client))
    assert result.status_code==422
    assert "fixture-secret" not in result.text


def test_full_forecast_archives_inputs_from_known_settings_and_weather(store):
    fallback(store)
    cfg=ProviderConfig(weather_enabled=True);put(store,"providers",cfg.model_dump())
    store.add_observations(normal())
    put(store,"demand_learning",{"enabled":False})                      # this test is about archiving the inputs: the profile as measured, without the seeded level
    put(store,"demand",{"mode":"High demand","start":NOW+12*3600,"end":NOW+36*3600,"high_extra_kwh":6.})
    worker=ProviderWorker(store,clock=FakeClock(NOW),transport=lambda *args:encoded(weather_fixture()).encode())
    worker.once()
    summary=store.get_setting("forecast_summary")[1]
    assert summary["demand_kwh"]==pytest.approx(53.96)
    assert summary["solar_status"]=="ok"
    assert summary["pv_kwh"]>0 and summary["price_complete"]
    assert summary["target"] is None and not summary["equipment_writes"]
    with closing(store.connect()) as db:
        payload=json.loads(db.execute("SELECT payload FROM issuances WHERE id=?",(summary["issuance_id"],)).fetchone()[0])
    assert len(payload["demand"]["intervals"])==2880
    assert payload["weather_issuance_id"]==store.get_setting("cache_weather")[1]["issuance_id"]


def test_configured_half_hour_rule_retains_whole_current_billing_interval(store):
    cfg=config(billing_rule="whole_half_hours");put(store,"providers",cfg.model_dump())
    transport,_=account_transport([dispatch(start=NOW+1,end=NOW+100)])
    clock=FakeClock(NOW+60);worker=ProviderWorker(store,clock=clock,transport=transport)
    worker.secrets.write("test-fixture-key");worker.once()
    from conftest import seed_minutes
    seed_minutes(store,"ev_power",NOW,NOW+300,7.0)                      # the car has been charging in this half-hour (issue 34: a slot counts only while it does)
    assert slot_intervals(store,cfg,NOW+500)==[{"start":NOW,"end":NOW+1800}]
    assert not slot_intervals(store,cfg,NOW+1800)


def test_unverified_household_eligibility_never_grants_cheap_slot(store):
    cfg=config(household_rate_verified=False);put(store,"providers",cfg.model_dump());fallback(store)
    cache(store,"slots",cfg,[{"start":NOW,"end":NOW+600}])
    result=tariff_horizon(store,NOW,NOW,NOW+60)
    assert result["intervals"][0]["price"]==.3
    assert not result["extra_slots"]


def test_partial_minute_visit_boundaries_and_raw_observations():
    rows=normal()+[Observation("raw-fixture","household_power",NOW-10,2.,"kW")]
    selection=dict(mode="High demand",start=NOW+30,end=NOW+45,high_extra_kwh=6.)
    r=demand_horizon(rows,selection,1,NOW,NOW,NOW+60)
    assert [i["end"]-i["start"] for i in r["intervals"]]==[30,15,15]
    assert r["demand_kwh"]==pytest.approx(1/60+.24*15/3600)


def test_schema2_backup_can_be_restored_and_migrated(tmp_path):
    path=tmp_path/"schema2.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        for sql in SCHEMA+MIGRATION_2:db.execute(sql)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=2")
        db.execute("INSERT INTO settings VALUES('operating_state',1,?)",(encoded({"mode":"handover_pending"}),))
    s=Store(tmp_path/"restored.sqlite3");s.restore(path)
    assert s.get_setting("operating_state")[1]["mode"]=="handover_pending"
    with closing(s.connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0]==4


def test_provider_worker_rides_out_a_busy_database(tmp_path, monkeypatch):
    import sqlite3, threading
    from solarbank_optimiser.services import ProviderWorker
    store = Store(tmp_path / "p.sqlite3"); store.initialise()
    worker = object.__new__(ProviderWorker)
    worker.store = store
    calls = {"n": 0}

    def busy():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")
    worker.once = busy
    worker.run(threading.Event(), count=1)           # must return normally rather than exit
    assert calls["n"] == 1


def test_an_odd_boost_or_test_row_does_not_discard_the_smart_slots_beside_it():
    """8 November: the slot feed failed ('invalid_response') as soon as a bump charge began. Only smart rows are used, so only smart rows are judged."""
    odd = {"start": None, "end": "not a time", "type": "BOOST", "energyAddedKwh": None}
    transport, _ = account_transport([dispatch(), odd, {**odd, "type": "TEST"}])
    assert OctopusAccount(transport).dispatches("test-fixture-key", "A-FIXTURE", "fixture-device", NOW) == [{"start": NOW, "end": NOW + 600}]


def test_a_type_nobody_knows_is_skipped_never_turned_into_a_slot():
    transport, _ = account_transport([dispatch(), dispatch("BUMP_CHARGE"), dispatch("MANUAL", start=NOW + 700, end=NOW + 900)])
    assert OctopusAccount(transport).dispatches("test-fixture-key", "A-FIXTURE", "fixture-device", NOW) == [{"start": NOW, "end": NOW + 600}]


def test_a_smart_row_that_is_malformed_still_rejects_the_whole_list_with_its_own_reason():
    for rows, reason in (([{**dispatch(), "energyAddedKwh": None}], "invalid_dispatch_energy"), ([dispatch(start=NOW + 100, end=NOW)], "invalid_dispatch_interval")):
        transport, _ = account_transport(rows)
        with pytest.raises(Exception) as caught:
            OctopusAccount(transport).dispatches("test-fixture-key", "A-FIXTURE", "fixture-device", NOW)
        assert reason in str(caught.value), (rows, caught.value)


def test_the_real_octopus_slots_of_9_november_are_accepted_negative_energy_and_all():
    """The first real dispatch list (9 November 04:55) had energyAddedKwh as NEGATIVE strings ('-0.6154166666666666', '-22.155'); the decoder demanded a non-negative value and the
    whole feed failed. The same shape, at fixture times."""
    real = [{**dispatch(start=NOW + 300, end=NOW + 600), "energyAddedKwh": "-0.6154166666666666"},
            {**dispatch(start=NOW + 600, end=NOW + 11400), "energyAddedKwh": "-22.155"}]
    transport, _ = account_transport(real)
    assert OctopusAccount(transport).dispatches("test-fixture-key", "A-FIXTURE", "fixture-device", NOW) == [{"start": NOW + 300, "end": NOW + 600}, {"start": NOW + 600, "end": NOW + 11400}]
    for bad in (None, "not a number", "1e9", True):
        transport, _ = account_transport([{**real[0], "energyAddedKwh": bad}])
        with pytest.raises(Exception) as caught:
            OctopusAccount(transport).dispatches("test-fixture-key", "A-FIXTURE", "fixture-device", NOW)
        assert "invalid_dispatch_energy" in str(caught.value), (bad, caught.value)
