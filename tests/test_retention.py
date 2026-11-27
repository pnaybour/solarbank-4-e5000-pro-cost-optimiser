"""Retention (owner, 28 Oct): a long-term half-hour record aligned to Octopus; detail purged after several days; and the archive stops growing for nothing."""
import csv
import io
import json
from contextlib import closing
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser import daily_record as dr
from solarbank_optimiser import retention
from solarbank_optimiser.energy import LONDON, day_bounds
from solarbank_optimiser.models import FakeClock, Observation, encoded
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app
from test_daily_record import DAY, TARIFF, add_day, london, put

NOW = london(DAY + timedelta(days=9), 12)           # 5 November 2026, noon: day D is nine days old
ROWS_PER_MINUTE = 6                                  # the five metrics of the daily record and the car (add_day, record version 4)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "r.sqlite3"); result.initialise()
    put(result, "fallback_tariff", TARIFF)
    return result


def issuance_count(store):
    with closing(store.connect()) as db:
        return db.execute("SELECT count(*) FROM issuances").fetchone()[0]


def observation_count(store, start=0, end=1e12):
    with closing(store.connect()) as db:
        return db.execute("SELECT count(*) FROM observations WHERE sample_at>=? AND sample_at<?", (start, end)).fetchone()[0]


def archive(store, at, content="x"):
    return store.archive(at, encoded({"n": at, "c": content}).encode(), {"n": at, "c": content})


# ===== the half-hour record ===================================================================================================================
def test_the_half_hour_record_has_forty_eight_octopus_periods_with_exact_sums(store):
    add_day(store, DAY, house=0.5, grid=0.2, pv=0.0, battery=0.3)
    dr.record_finished_days(store, london(DAY + timedelta(days=1), 0, 10), lookback=1)
    record = store.get_setting(dr.HALF_HOUR_PREFIX + DAY.isoformat())[1]
    slots = record["slots"]
    assert len(slots) == 48 and slots[0]["london"] == "00:00" and slots[1]["london"] == "00:30" and slots[-1]["london"] == "23:30"
    assert all(s["t"] % 1800 == 0 for s in slots)                                    # on the :00 and :30 boundaries
    first = slots[0]
    assert first["minutes_covered"] == 30 and first["consumption_kwh"] == pytest.approx(0.25) and first["import_kwh"] == pytest.approx(0.1)
    assert first["cheap_rate"] is True and first["price_gbp_per_kwh"] == 0.07            # 00:00 is inside 23:30 to 05:30
    assert slots[20]["cheap_rate"] is False and slots[20]["price_gbp_per_kwh"] == 0.30    # 10:00
    assert sum(s["consumption_kwh"] for s in slots) == pytest.approx(12.0, abs=0.05)      # the day's total is the sum of its half hours
    assert sum(s["actual_cost_gbp"] for s in slots) == pytest.approx(store.get_setting(dr.RECORD_PREFIX + DAY.isoformat())[1]["actual_cost_gbp"], abs=0.02)


@pytest.mark.parametrize("day,expected", [(date(2026, 3, 29), 46), (date(2026, 10, 25), 50), (date(2026, 10, 27), 48)])
def test_clock_change_days_have_forty_six_or_fifty_periods(store, day, expected):
    start, end = day_bounds(day)
    rows = {m: {t: 0.1 for t in range(int(start), int(end), 60)} for m in dr.METRICS}
    slots = dr.half_hours(day, rows, lambda t: (0.3, False))
    assert len(slots) == expected and all(s["minutes_covered"] == 30 for s in slots)


def test_missing_minutes_show_in_the_half_hour_coverage(store):
    add_day(store, DAY, skip=range(0, 10))
    dr.record_finished_days(store, london(DAY + timedelta(days=1), 0, 10), lookback=1)
    slots = store.get_setting(dr.HALF_HOUR_PREFIX + DAY.isoformat())[1]["slots"]
    assert slots[0]["minutes_covered"] == 20 and slots[1]["minutes_covered"] == 30


def test_the_half_hour_csv_download(store):
    add_day(store, DAY)
    dr.record_finished_days(store, london(DAY + timedelta(days=1), 0, 10), lookback=1)
    app = create_app(WebConfig(store.path, "a-long-test-password", trusted_network=True, status_cache_seconds=0), FakeClock(london(DAY + timedelta(days=1), 12)), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        response = client.get(f"/half-hours.csv?start={DAY}&end={DAY}")
        assert response.status_code == 200 and "text/csv" in response.headers["content-type"]
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 48 and rows[0]["period_start_london"] == f"{DAY} 00:00" and rows[0]["period_start_utc"].endswith("Z")
        assert float(rows[0]["consumption_kwh"]) == pytest.approx(0.25) and rows[0]["cheap_rate"] == "True"
        assert float(rows[0]["car_kwh"]) == pytest.approx(0.0) and rows[0]["car_minutes_covered"] == "30" and float(rows[0]["house_without_car_kwh"]) == pytest.approx(0.25)
        assert client.get(f"/half-hours.csv?start={DAY}&end=2025-01-01").status_code == 422
        assert client.get("/half-hours.csv?start=x&end=y").status_code == 422
        assert 'id="half-hours-link"' in client.get("/history").text


# ===== retention ===================================================================================================================================
def test_detail_older_than_seven_days_is_purged_once_the_half_hour_record_exists(store):
    add_day(store, DAY)
    recent = DAY + timedelta(days=8)
    add_day(store, recent)
    dr.record_finished_days(store, london(DAY + timedelta(days=1), 0, 10), lookback=1)          # day D has its record
    before_old, before_recent = observation_count(store, *day_bounds(DAY)), observation_count(store, *day_bounds(recent))
    assert before_old == 1440 * ROWS_PER_MINUTE
    result = retention.purge_detail(store, NOW)
    assert result["observations_deleted"] == before_old and result["days_purged"] == [DAY.isoformat()] and result["cutoff"] == (DAY + timedelta(days=2)).isoformat()
    assert observation_count(store, *day_bounds(DAY)) == 0 and observation_count(store, *day_bounds(recent)) == before_recent      # recent days untouched
    assert store.get_setting(dr.HALF_HOUR_PREFIX + DAY.isoformat())[1]["slots"] and store.get_setting(dr.RECORD_PREFIX + DAY.isoformat())[1]    # the long-term records stay
    assert store.get_setting(retention.SETTING)[1]["observations_deleted"] == before_old
    assert retention.purge_detail(store, NOW)["observations_deleted"] == 0                      # nothing more to do


def test_a_day_without_a_half_hour_record_is_never_purged(store):
    add_day(store, DAY)
    result = retention.purge_detail(store, NOW)
    assert result["observations_deleted"] == 0 and DAY.isoformat() in result["days_kept_no_half_hour_record"]
    assert observation_count(store) == 1440 * ROWS_PER_MINUTE


def test_imported_history_and_other_provenance_are_never_purged(store):
    add_day(store, DAY)
    dr.record_finished_days(store, london(DAY + timedelta(days=1), 0, 10), lookback=1)
    start, _ = day_bounds(DAY)
    store.add_observations([Observation("seed", "household_power", start + 60, 0.4, "kW", provenance="historical-seed", source_at=start + 59)])
    retention.purge_detail(store, NOW)
    with closing(store.connect()) as db:
        left = db.execute("SELECT count(*) FROM observations WHERE json_extract(payload,'$.provenance')='historical-seed'").fetchone()[0]
    assert left == 1


def test_the_seven_most_recent_days_are_always_kept_whatever_is_asked(store):
    add_day(store, DAY)
    dr.record_finished_days(store, london(DAY + timedelta(days=1), 0, 10), lookback=1)
    soon = london(DAY + timedelta(days=5), 12)                                         # day D is only five days old
    assert retention.purge_detail(store, soon, keep_days=1)["observations_deleted"] == 0     # keep_days is held at seven or more
    assert observation_count(store) == 1440 * ROWS_PER_MINUTE


def test_archived_snapshots_are_purged_by_age_but_never_the_ones_a_setting_points_to(store):
    old = [archive(store, NOW - 9 * 86400 + k) for k in range(5)]
    mid = archive(store, NOW - 3 * 86400)
    put(store, "shadow_plan", {"issuance_id": old[2], "status": "checked_shadow"}, 5.0)
    put(store, "cache_weather", {"issuance_id": old[4], "intervals": []}, 6.0)
    assert retention.purge_detail(store, NOW, dry_run=True)["issuances_deleted"] == 3 and issuance_count(store) == 6      # a dry run removes nothing
    result = retention.purge_detail(store, NOW)
    assert result["issuances_deleted"] == 3 and issuance_count(store) == 3
    with closing(store.connect()) as db:
        kept = {r[0] for r in db.execute("SELECT id FROM issuances")}
    assert kept == {old[2], old[4], mid}


def test_a_shorter_window_for_archived_snapshots_can_clear_a_burst_without_touching_minute_rows(store):
    add_day(store, DAY + timedelta(days=8))
    now = london(DAY + timedelta(days=9), 12)
    for k in range(10):
        archive(store, now - 3600 * k - 10)
    result = retention.purge_detail(store, now, issuance_hours=4)
    assert result["issuances_deleted"] == 6 and issuance_count(store) == 4 and observation_count(store) == 1440 * ROWS_PER_MINUTE


def test_the_purge_runs_in_small_batches(store):
    for k in range(25):
        archive(store, NOW - 9 * 86400 + k)
    assert retention.purge_detail(store, NOW, batch=4)["issuances_deleted"] == 25 and issuance_count(store) == 0


def test_the_command_line_reports_and_purges(store, tmp_path, capsys):
    from solarbank_optimiser import cli
    archive(store, 1000.0)
    cli.main(["--database", str(store.path), "retention", "--dry-run"])
    out = capsys.readouterr().out
    assert "Would remove: 1 archived snapshots" in out and issuance_count(store) == 1
    cli.main(["--database", str(store.path), "retention"])
    assert "Removed: 1 archived snapshots" in capsys.readouterr().out and issuance_count(store) == 0
    with pytest.raises(SystemExit):
        cli.main(["--database", str(store.path), "retention", "--issuance-hours", "0"])


# ===== the archive stops growing for nothing =====================================================================================================
def test_refreshing_the_extra_slot_list_with_the_same_content_does_not_archive_a_new_forecast(store):
    from test_phase3 import NOW as PNOW, fallback, normal, weather_fixture
    from solarbank_optimiser.services import ProviderConfig, ProviderWorker, cache_identity
    fallback(store)
    cfg = ProviderConfig(weather_enabled=True); put(store, "providers", cfg.model_dump())
    store.add_observations(normal())
    clock = FakeClock(PNOW)
    worker = ProviderWorker(store, clock=clock, transport=lambda *args: encoded(weather_fixture()).encode())
    worker.once()
    first = issuance_count(store)
    def refresh_slots(intervals):
        revision, _ = store.get_setting("cache_slots")
        store.set_setting("cache_slots", {"identity": "x", "acquired_at": clock.utc(), "intervals": intervals}, clock.utc(), revision)
        revision, _ = store.get_setting("established_slots")          # the provider rewrites this one every minute too
        store.set_setting("established_slots", {"identity": "x", "intervals": []}, clock.utc(), revision)
    refresh_slots([]); clock.advance(60)
    worker.forecast(); count_after_first_slots = issuance_count(store)
    for _ in range(5):                                                          # five more minutes of identical, empty slot refreshes
        refresh_slots([]); clock.advance(60); worker.forecast()
    assert issuance_count(store) == count_after_first_slots
    refresh_slots([{"start": PNOW + 7200, "end": PNOW + 9000}]); worker.forecast()          # a real change is picked up straight away
    assert issuance_count(store) == count_after_first_slots + 1
    from solarbank_optimiser.services import FORECAST_REFRESH_SECONDS
    clock.advance(FORECAST_REFRESH_SECONDS); worker.forecast()                  # and the periodic refresh is every half hour
    assert issuance_count(store) == count_after_first_slots + 2


# ===== the append-only protection comes back ======================================================================================================
def triggers(store):
    with closing(store.connect()) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}


def test_the_append_only_triggers_are_restored_after_a_purge_and_after_a_failure(store):
    import sqlite3
    before = triggers(store)
    assert {"issuances_no_delete", "setting_versions_no_delete"} <= before
    archive(store, NOW - 9 * 86400)
    retention.purge_detail(store, NOW)
    assert triggers(store) == before
    archive(store, NOW - 60)                                                           # a recent row: the protection must still hold for it
    with closing(store.connect()) as db:
        with pytest.raises(sqlite3.IntegrityError):
            with db:
                db.execute("DELETE FROM issuances")                                    # outside the purge, deletion is still refused
    with pytest.raises(sqlite3.OperationalError):                                      # a failing batch rolls the trigger drop back too
        with closing(store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            retention._delete(db, "issuances", "DELETE FROM no_such_table", ())
    assert triggers(store) == before


def test_old_versions_of_operational_settings_and_their_events_go_but_the_users_settings_history_stays(store):
    old = NOW - 9 * 86400
    for k in range(4):
        store.set_setting("cache_slots", {"intervals": [], "k": k}, old + k, k)                 # operational: rewritten every minute
        store.set_setting("min_soc", {"value": 0.10 + k / 100}, old + k, k)                     # the owner's own setting
    store.set_setting("cache_slots", {"intervals": [], "k": 9}, NOW - 100, 4)
    with closing(store.connect()) as db:
        versions = lambda n: db.execute("SELECT count(*) FROM setting_versions WHERE name=?", (n,)).fetchone()[0]
        events = lambda n: db.execute("SELECT count(*) FROM events WHERE kind='settings_changed' AND json_extract(payload,'$.name')=?", (n,)).fetchone()[0]
        assert (versions("cache_slots"), versions("min_soc"), events("cache_slots")) == (5, 4, 5)
    dry = retention.purge_detail(store, NOW, dry_run=True)
    assert dry["setting_versions_deleted"] == 4 and dry["events_deleted"] == 4
    result = retention.purge_detail(store, NOW)
    assert result["setting_versions_deleted"] == 4 and result["events_deleted"] == 4
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM setting_versions WHERE name='cache_slots'").fetchone()[0] == 1       # only the recent one
        assert db.execute("SELECT count(*) FROM setting_versions WHERE name='min_soc'").fetchone()[0] == 4          # the owner's history is kept for good
        assert db.execute("SELECT count(*) FROM events WHERE kind='settings_changed' AND json_extract(payload,'$.name')='min_soc'").fetchone()[0] == 4
    assert store.setting_at("min_soc", old + 1)[1] == {"value": 0.11}                                               # and still answers "what was it then"
