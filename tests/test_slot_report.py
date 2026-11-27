"""The slot report (#34): what happened to the listed Octopus slots when a charge ended."""
from datetime import datetime
import importlib.util
import json
from pathlib import Path

import pytest

from solarbank_optimiser.store import Store

SPEC = importlib.util.spec_from_file_location("slot_report", Path(__file__).resolve().parents[1] / "scripts/slot_report.py")
sr = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(sr)
LONDON = sr.LONDON
NIGHT = datetime(2026, 11, 3, 23, 30, tzinfo=LONDON).timestamp()          # 23:30


@pytest.fixture
def db(tmp_path):
    store = Store(tmp_path / "s.sqlite3"); store.initialise()
    import sqlite3
    connection = sqlite3.connect(tmp_path / "s.sqlite3")
    yield connection
    connection.close()


def snapshot(db, at, intervals):
    payload = json.dumps({"kind": "slots", "intervals": [{"start": s, "end": e} for s, e in intervals]})
    db.execute("INSERT INTO issuances VALUES(?,?,?,?)", (f"s{at}", at, payload, b"x"))


def session(db, start, end, kwh=20.0):
    db.execute("INSERT INTO charger_sessions VALUES(?,?,?,?)", (f"c{start}", start, end, json.dumps({"energy_kwh": kwh, "peak_kw": 7.2, "partial": False})))


def car(db, at, kw):
    payload = json.dumps({"value": kw, "quality": "ok", "window_max": kw})
    db.execute("INSERT INTO observations VALUES(?,?,?,?,?)", (f"o{at}", "t", "ev_power", at, payload))


def lines(db, now, **kw):
    out = []
    sr.report(db, now, out=out.append, **kw)
    return "\n".join(out)


NOW = datetime(2026, 11, 4, 9, 0, tzinfo=LONDON)


def test_slots_still_listed_after_the_car_stopped_are_reported_as_kept(db):
    end = NIGHT + 2 * 3600                                                    # the car stops at 01:30 (it reached its target)
    for at in range(int(NIGHT), int(end) + 5000, 900):
        snapshot(db, at, [(NIGHT, NIGHT + 6 * 3600)])                       # six hours listed throughout, whatever the car did
    session(db, NIGHT, end)
    text = lines(db, NOW)
    assert "KEPT (listed although the car had stopped)" in text and "4.0 h of slots still listed after the stop" in text


def test_slots_removed_when_the_charge_ends_are_reported_as_removed(db):
    end = NIGHT + 2 * 3600
    for at in range(int(NIGHT), int(end), 900):
        snapshot(db, at, [(NIGHT, NIGHT + 6 * 3600)])
    for at in range(int(end) + 300, int(end) + 7200, 900):
        snapshot(db, at, [])                                                # the list is empty after the stop
    session(db, NIGHT, end)
    text = lines(db, NOW)
    assert "REMOVED" in text


def test_slots_cut_short_at_the_stop_are_reported_as_shortened(db):
    end = NIGHT + 2 * 3600
    for at in range(int(NIGHT), int(end), 900):
        snapshot(db, at, [(NIGHT, NIGHT + 6 * 3600)])
    for at in range(int(end) + 300, int(end) + 7200, 900):
        snapshot(db, at, [(NIGHT, end + 900)])                              # now ends 15 minutes after the stop
    session(db, NIGHT, end)
    assert "SHORTENED" in lines(db, NOW)


def test_a_gap_in_the_slot_snapshots_is_unknown_not_assumed_unchanged(db):
    end = NIGHT + 2 * 3600
    snapshot(db, NIGHT, [(NIGHT, NIGHT + 6 * 3600)])                         # then nothing for hours: the collector or the account was away
    session(db, NIGHT, end)
    assert "unknown" in lines(db, NOW)


def test_with_nothing_listed_beyond_the_stop_it_says_so(db):
    end = NIGHT + 2 * 3600
    for at in range(int(NIGHT), int(end) + 5000, 900):
        snapshot(db, at, [(NIGHT, end)])
    session(db, NIGHT, end)
    assert "nothing was listed beyond the stop" in lines(db, NOW)


def test_the_daily_part_counts_listed_hours_and_the_hours_the_car_really_charged(db):
    for at in range(int(NIGHT), int(NIGHT) + 8 * 3600, 900):
        snapshot(db, at, [(NIGHT, NIGHT + 6 * 3600)])
    for at in range(int(NIGHT), int(NIGHT) + 3600, 60):                      # the car charged for one hour of the six listed
        car(db, at, 7.2)
    text = lines(db, NOW)
    assert "dispatched 6.0 h" in text and "car charged 1.0 h" in text and "dispatched but car idle 5.0 h" in text


def test_no_sessions_is_stated_plainly(db):
    assert "no finished charging session in this period" in lines(db, NOW)


def test_the_report_opens_the_database_read_only():
    text = (Path(__file__).resolve().parents[1] / "scripts/slot_report.py").read_text()
    assert "mode=ro" in text and "INSERT" not in text and "UPDATE" not in text and "DELETE" not in text


def test_car_charging_is_read_from_the_telemetry_files_not_only_the_database(tmp_path):
    """8 November: the report said 'car readings cover 0 half-hours' during a 7 kW charge, because it read only the database rows that the single-store change stopped writing."""
    from conftest import seed_minutes
    import sqlite3
    store = Store(tmp_path / "t.sqlite3"); store.initialise()
    start = datetime(2026, 11, 4, 6, 0, tzinfo=LONDON).timestamp()
    seed_minutes(store, "ev_power", start, start + 3600, 7.0)                     # an hour at 7 kW, none of it in the database
    connection = sqlite3.connect(tmp_path / "t.sqlite3")
    try:
        text = lines(connection, NOW, source=store.minute_source())
        without = lines(connection, NOW)
    finally:
        connection.close()
    assert "car readings cover 2 half-hours" in text and "car charged 1.0 h" in text
    assert "car readings cover 0 half-hours" in without                             # the old behaviour, for the record
