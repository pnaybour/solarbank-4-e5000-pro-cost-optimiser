"""The home page's day chart (owner, 19 November 2026): one London day in five-minute steps from the telemetry files, with totals and the cheap periods."""
from datetime import date, datetime, time, timedelta

import pytest

from solarbank_optimiser import day_view
from solarbank_optimiser.energy import LONDON, day_bounds

DAY = date(2026, 11, 18)
NOW = datetime.combine(DAY + timedelta(days=1), time(19, 0), LONDON).timestamp()


class Source:
    def __init__(self, data):
        self.data = data

    def usable(self, metric, start, end):
        return {t: v for t, v in self.data.get(metric, {}).items() if start <= t < end}


class Store:
    def __init__(self, data, settings=None):
        self.source, self.settings = Source(data), settings or {}

    def minute_source(self):
        return self.source

    def get_setting(self, name, default=None):
        return (1, self.settings[name]) if name in self.settings else (0, default)


@pytest.fixture(autouse=True)
def night_is_cheap(monkeypatch):
    def pricer(store, now=None, at=None):
        def price_at(t):
            hour = datetime.fromtimestamp(t, LONDON).hour + datetime.fromtimestamp(t, LONDON).minute / 60
            return (0.066, True) if hour >= 23.5 or hour < 5.5 else (0.3496, False)
        return price_at, "test"
    monkeypatch.setattr(day_view, "tariff_pricer", pricer)


def readings():
    """Charging at 2.5 kW 00:00-03:00, then the battery powering the house at 0.5 kW 08:00-20:00; a gap 12:00-12:30."""
    a, _ = day_bounds(DAY)
    data = {m: {} for m in ("battery_power", "household_power", "pv_power", "grid_power", "soc")}
    for t in range(int(a), int(a) + 86400, 60):
        hour = (t - a) / 3600
        if 12 <= hour < 12.5:
            continue
        battery = -2.5 if hour < 3 else 0.5 if 8 <= hour < 20 else 0.0
        data["battery_power"][t] = battery
        data["household_power"][t] = 0.5
        data["pv_power"][t] = 0.0
        data["grid_power"][t] = 0.5 - battery if battery <= 0 else 0.0
        data["soc"][t] = 0.3
    return data


def test_a_day_comes_in_five_minute_steps_with_its_totals():
    out = day_view.day_series(Store(readings()), DAY, NOW)
    assert out["date"] == "2026-11-18" and out["step_s"] == 300 and len(out["series"]["battery_kw"]) == 288
    assert out["series"]["battery_kw"][0] == -2.5 and out["series"]["battery_kw"][12 * 9] == 0.5      # 00:00 charging, 09:00 discharging
    assert out["series"]["battery_kw"][12 * 12] is None                                              # the gap is not drawn as zero
    assert out["totals"]["battery_in_kwh"] == 7.5 and out["totals"]["battery_out_kwh"] == 5.75
    assert out["previous"] == "2026-11-17" and out["next"] == "2026-11-19"


def test_the_cheap_periods_are_joined_into_bands():
    out = day_view.day_series(Store(readings()), DAY, NOW)
    a, b = day_bounds(DAY)
    assert out["cheap"] == [[a, a + 5.5 * 3600], [a + 23.5 * 3600, b]]


def test_a_future_day_or_one_older_than_the_minute_archive_is_refused():
    with pytest.raises(ValueError):
        day_view.day_series(Store({}), DAY + timedelta(days=2), NOW)
    with pytest.raises(ValueError):
        day_view.day_series(Store({}), DAY - timedelta(days=95), NOW)


def test_today_has_no_next_day_and_an_empty_day_has_empty_steps():
    today = DAY + timedelta(days=1)
    out = day_view.day_series(Store({}), today, NOW)
    assert out["next"] is None and all(v is None for v in out["series"]["soc"]) and out["totals"]["house_kwh"] == 0


def test_the_api_answers_with_a_day_and_refuses_a_bad_date(tmp_path):
    from fastapi.testclient import TestClient
    from solarbank_optimiser.web.app import WebConfig, create_app
    from test_web import PASSWORD
    from solarbank_optimiser.models import FakeClock
    client = TestClient(create_app(WebConfig(tmp_path / "web.sqlite3", PASSWORD, status_cache_seconds=0), FakeClock(NOW)))
    client.auth = ("solarbank", PASSWORD)
    good = client.get("/api/day", params={"day": "2026-11-18"})
    assert good.status_code == 200 and good.json()["date"] == "2026-11-18" and len(good.json()["series"]["soc"]) == 288
    assert client.get("/api/day").json()["date"] == "2026-11-19"
    assert client.get("/api/day", params={"day": "not-a-date"}).status_code == 422
    assert client.get("/api/day", params={"day": "2026-12-25"}).status_code == 422
    assert client.get("/api/day", auth=None).status_code == 401


def hourly(day, kw):
    a, _ = day_bounds(day)
    return [{"start": a + h * 3600, "end": a + (h + 1) * 3600, "pv_kw": kw if 9 <= h < 15 else 0.0} for h in range(24)]


def test_the_solar_forecast_and_the_learned_correction_come_with_the_day():
    settings = {"forecast_for_day:2026-11-18": {"lead": "evening_before", "hourly": hourly(DAY, 0.4)},
                "adjusted_forecast_for_day:2026-11-18": {"status": "ok", "hourly": hourly(DAY, 0.3)}}
    out = day_view.day_series(Store(readings(), settings), DAY, NOW)
    assert len(out["solar_forecast"]) == 24 and max(r["kw"] for r in out["solar_forecast"]) == 0.4 and out["solar_forecast_lead"] == "evening_before"
    assert max(r["kw"] for r in out["solar_corrected"]) == 0.3
    settings["adjusted_forecast_for_day:2026-11-18"] = {"status": "not_enough_data"}
    assert day_view.day_series(Store(readings(), settings), DAY, NOW)["solar_corrected"] == []
    assert day_view.day_series(Store(readings()), DAY, NOW)["solar_forecast"] == []


def test_the_battery_line_is_the_plan_made_before_the_day_and_today_also_shows_the_newest_plan():
    a, b = day_bounds(DAY)
    def entry(issued, soc):
        return {"issued_at": issued, "chart": [{"time": t, "soc": soc} for t in range(int(issued), int(issued) + 48 * 3600, 1800)]}
    log = {"entries": [entry(a - 6 * 3600, 0.5), entry(a - 3600, 0.6), entry(a + 10 * 3600, 0.7)]}
    store = Store(readings(), {"soc_forecast_log": log})
    past = day_view.day_series(store, DAY, NOW)
    assert past["soc_planned"]["issued_at"] == a - 3600 and {p["soc"] for p in past["soc_planned"]["points"]} == {0.6}
    assert all(a <= p["t"] <= b for p in past["soc_planned"]["points"]) and past["soc_plan_latest"] is None      # not today: no newest plan
    today = day_view.day_series(store, DAY, a + 12 * 3600)
    assert today["soc_plan_latest"]["issued_at"] == a + 10 * 3600 and min(p["t"] for p in today["soc_plan_latest"]["points"]) >= a + 10 * 3600
    assert day_view.day_series(Store(readings()), DAY, NOW)["soc_planned"] is None                            # plans are kept 36 hours only
