from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from solarbank_optimiser.forecasts import demand_horizon
from solarbank_optimiser.household_policy import household_policy
from solarbank_optimiser.models import FakeClock, ForecastInterval, PriceInterval
from solarbank_optimiser.planning import BatteryModel, Scenario, replay
from solarbank_optimiser.settings import DemandSelection, london_input
from solarbank_optimiser.web.app import WebConfig, create_app


def test_high_selection_needs_no_extra_energy_and_applies_before_daytime_visit():
    now = london_input("2026-10-25T12:00")
    selection = DemandSelection(mode="High demand", start=london_input("2026-10-26T12:00"), end=london_input("2026-10-26T18:00"))
    policy = household_policy(selection, now)
    assert policy["overnight_target"] == 1
    assert len(policy["windows"]) == 1
    assert policy["windows"][0]["intervals"] == [{"start": london_input("2026-10-25T23:30"), "end": london_input("2026-10-26T05:30")}]
    assert not policy["equipment_writes"]
    assert household_policy(selection, selection.end)["overnight_target"] is None


@pytest.mark.parametrize("night,hours", [("2026-10-24",7), ("2027-03-27",5)])
def test_full_overnight_policy_handles_london_clock_changes(night,hours):
    now = london_input(night+"T22:00")
    policy = household_policy(DemandSelection(mode="High demand",start=now),now)
    first = policy["windows"][0]
    assert sum((r["end"]-r["start"])/3600 for r in first["intervals"]) == hours


def test_policy_only_requests_remaining_night_and_never_peak_hours():
    now = london_input("2026-10-25T02:00")
    selection = DemandSelection(mode="High demand",start=now,end=london_input("2026-10-25T18:00"))
    intervals = household_policy(selection,now)["windows"][0]["intervals"]
    assert intervals == [{"start":now,"end":london_input("2026-10-25T05:30")}]
    assert household_policy(selection,london_input("2026-10-25T12:00"))["windows"] == []


def test_offline_policy_requests_full_charge_but_obeys_power_and_price_bounds():
    now=london_input("2026-10-25T12:00")
    policy=household_policy(DemandSelection(mode="High demand",start=now),now)
    window=policy["windows"][0]["intervals"][0]
    start,end=window["start"],window["end"]
    model=BatteryModel()
    scenario=Scenario(1.,(ForecastInterval(start,end,1.,0.),))
    cheap=[PriceInterval(start,end,.066,"fixture",True)]
    result=replay(model,scenario,cheap,.2,.2,.066,policy)
    assert result["flows"][0]["grid_target"]==1
    assert result["flows"][0]["after_kwh"]==pytest.approx(10)
    assert result["flows"][0]["grid_charge_kw"]<=2.5
    expensive=[PriceInterval(start,end,.3496,"fixture",False)]
    assert replay(model,scenario,expensive,.2,.2,.066,policy)["flows"][0]["grid_charge_kw"]==0
    short=Scenario(1.,(ForecastInterval(start,start+60,1.,0.),))
    short_prices=[PriceInterval(start,start+60,.066,"fixture",True)]
    assert replay(model,short,short_prices,.2,.2,.066,policy)["flows"][0]["after_kwh"]<10


def test_high_without_allowance_can_save_through_scheduled_form(tmp_path):
    password="fixture-high-mode-password"
    now=london_input("2026-10-25T12:00")
    client=TestClient(create_app(WebConfig(tmp_path/"db.sqlite3",password, status_cache_seconds=0),FakeClock(now)))
    client.auth=("solarbank",password)
    token=client.get("/api/status").json()["csrf_token"]
    headers={"Origin":"http://testserver","X-CSRF-Token":token}
    body={"revision":0,"mode":"High demand","start_local":None,"end_local":None}
    assert client.put("/api/demand-schedule",json=body,headers=headers).status_code==200
    status=client.get("/api/status").json()
    assert status["household_policy"]["overnight_target"]==1
    assert status["demand"]["high_extra_kwh"] is None
    assert not status["equipment_writes"]


def test_new_high_forecast_requires_no_extra_kwh_estimate():
    from solarbank_optimiser.models import Observation
    from solarbank_optimiser.energy import day_bounds
    from datetime import date
    now=london_input("2026-10-25T12:00")
    first,last=day_bounds(date(2026,10,24))
    rows=[Observation("fixture","household_power",t+60,1.,"kW",interval_start=t,
          coverage_seconds=60.,demand_mode="Normal consumption",demand_revision=0)
          for t in range(int(first),int(last),60)]
    selection=DemandSelection(mode="High demand",start=now)
    forecast=demand_horizon(rows,selection,1,now,now,now+48*3600)
    assert forecast["demand_kwh"]==pytest.approx(48)
    assert household_policy(selection,now)["overnight_target"]==1
