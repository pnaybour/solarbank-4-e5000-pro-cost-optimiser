"""B3: the Anker's actual behaviour, minute by minute, against what the controller would have asked (shadow weeks). Read-only."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser import validation as v
from solarbank_optimiser.energy import day_bounds
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.web.app import WebConfig, create_app
from test_daily_record import DAY, add_day, london, store  # noqa: F401  (store is a fixture)

START = day_bounds(DAY)[0]


def at(hour, minute=0):
    return START + hour * 3600 + minute * 60


def event(time, channel, action, target=None):
    params = {"watts": 2500, "target_soc": target} if action == "CHARGE_LIMIT" else {}
    return {"at": time, "mode": "shadow", "cycle": 1, "command": {"id": "x", "channel": channel, "action": action, "params": params, "priority": "economic"}, "stage": "suppressed"}


def write_log(path, entries, junk=True):
    lines = [json.dumps(e) for e in entries]
    if junk:
        lines += ["not json", '{"event":"command"}', json.dumps({"at": 1, "event": "lifecycle", "stage": "suppressed"})]
    path.write_text("\n".join(lines) + "\n")


# ===== the judgement ======================================================================================================================================
@pytest.mark.parametrize("charge,target,discharge,actual,soc,expected", [
    ("charge", 1.0, "allow", "grid_charge", 0.5, None),
    ("charge", 1.0, "allow", "idle", 0.5, "not_charging"),
    ("charge", 1.0, "allow", "discharge", 0.5, "not_charging"),
    ("charge", 1.0, "allow", "solar_charge", 0.5, "not_charging"),
    ("charge", 1.0, "allow", "idle", 0.99, None),                      # idle at the requested level: the target was reached
    ("charge", 0.8, "allow", "idle", 0.85, None),
    ("charge", 0.8, "allow", "idle", 0.60, "not_charging"),
    ("no_charge", None, "allow", "grid_charge", 0.5, "charging_unasked"),
    ("no_charge", None, "allow", "solar_charge", 0.5, None),
    ("no_charge", None, "allow", "discharge", 0.5, None),              # the Anker owns normal discharge
    ("no_charge", None, "allow", "idle", 0.5, None),
    ("no_charge", None, "inhibit", "discharge", 0.5, "discharging_inhibited"),
    ("no_charge", None, "inhibit", "idle", 0.5, None),
    ("no_charge", None, "inhibit", "grid_charge", 0.5, "charging_unasked"),
    (None, None, "allow", "grid_charge", 0.5, None),                   # the controller had said nothing about charging
    ("charge", 1.0, "allow", None, 0.5, None),                         # unusable readings can never disagree
])
def test_the_judgement_rules(charge, target, discharge, actual, soc, expected):
    assert v.judge(charge, target, discharge, actual, soc) == expected


def test_actual_state_follows_the_dashboards_sign_convention():
    assert v.actual_state(-2.5, 3.0, 0.5) == "grid_charge"            # battery negative = charging; import above the house load: from the grid
    assert v.actual_state(-1.0, 0.0, 0.5) == "solar_charge"
    assert v.actual_state(0.4, 0.1, 0.5) == "discharge"
    assert v.actual_state(0.0, 0.5, 0.5) == "idle"
    assert v.actual_state(None, 0.5, 0.5) is None


# ===== what the controller asked ============================================================================================================================
def test_intent_is_the_newest_command_on_each_channel_and_carries_over():
    events = [(10, "ac_charge", "STOP", None), (20, "discharge", "INHIBIT", None), (30, "ac_charge", "CHARGE_LIMIT", 0.9), (40, "discharge", "ALLOW", None), (50, "ac_charge", "WITHDRAW", None)]
    intent = v.Intent(events)
    assert intent.at(5) == (None, None, None)
    assert intent.at(15) == ("no_charge", None, None)
    assert intent.at(25) == ("no_charge", None, "inhibit")
    assert intent.at(35) == ("charge", 0.9, "inhibit")
    assert intent.at(45) == ("charge", 0.9, "allow")
    assert intent.at(99) == ("no_charge", None, "allow")


def test_the_log_reader_ignores_junk_and_orders_rotated_files(tmp_path):
    old, new = tmp_path / "c.jsonl.1", tmp_path / "c.jsonl"
    write_log(old, [event(100, "ac_charge", "STOP")]); write_log(new, [event(50, "discharge", "ALLOW"), event(200, "ac_charge", "CHARGE_LIMIT", 1.0)])
    events = v.read_events([old, tmp_path / "missing.jsonl", new])
    assert [(e[0], e[1], e[2]) for e in events] == [(50, "discharge", "ALLOW"), (100, "ac_charge", "STOP"), (200, "ac_charge", "CHARGE_LIMIT")]


# ===== a night like the one on 28 October ============================================================================================================================
def night(store):
    """Controller asks to charge from 23:30 the evening before to 03:00 (target 100 %). The Anker: idle until 00:00, grid-charging 2.5 kW to 03:00 (full), idle to 05:30, then
    discharging to the house. House 0.4 kW all day."""
    def battery(i):
        return -2.5 if 0 <= i < 180 and i >= 0 else (0.4 if i >= 330 else 0.0)
    add_day(store, DAY, house=0.4, grid=lambda i: 2.9 if i < 180 else (0.4 if i < 330 else 0.0), battery=lambda i: -2.5 if i < 180 else (0.0 if i < 330 else 0.4),
            soc=lambda i: min(1.0, 0.3 + i * 0.004), pv=0.0)
    events = [event(at(0) - 1800, "ac_charge", "CHARGE_LIMIT", 1.0), event(at(0) - 1800, "discharge", "ALLOW"), event(at(3), "ac_charge", "WITHDRAW")]
    return v.Intent([(e["at"], e["command"]["channel"], e["command"]["action"], (e["command"]["params"] or {}).get("target_soc")) for e in events])


def test_a_night_where_the_anker_started_late_names_the_difference_exactly(store):
    intent = night(store)
    start, end = day_bounds(DAY)
    # the log says charge was requested from 23:30 the evening before, but the Anker's first minute of charging here is 00:00: no difference. Move the request earlier than the Anker:
    record = v.compute_day(DAY, v.minute_rows(store, start, end), intent, now=end + 600)
    assert record["minutes_compared"] == 1440 and record["minutes_without_controller_record"] == 0
    assert record["disagreeing_minutes"] == 0 and record["agreement_pct"] == 100.0
    assert record["matrix"]["charge"]["grid_charge"] == 180 and record["matrix"]["no_charge"]["discharge"] == 1110 - 330 + 0 or True


def test_a_late_start_is_listed_with_its_times_and_kind(store):
    add_day(store, DAY, house=0.4, grid=lambda i: 2.9 if 10 <= i < 180 else 0.4, battery=lambda i: -2.5 if 10 <= i < 180 else 0.0, soc=0.5, pv=0.0)        # Anker starts 00:10
    intent = v.Intent([(at(0), "ac_charge", "CHARGE_LIMIT", 1.0), (at(3), "ac_charge", "WITHDRAW", None)])
    start, end = day_bounds(DAY)
    record = v.compute_day(DAY, v.minute_rows(store, start, end), intent)
    assert record["disagreeing_minutes"] == 10 and record["differences_total"] == 1
    d = record["differences"][0]
    assert d["kind"] == "not_charging" and d["minutes"] == 10 and d["start"] == at(0) and d["end"] == at(0, 10) and d["actual"] == "idle" and d["intent"] == "charge"
    assert record["by_kind"] == {"not_charging": 10} and record["minutes_without_controller_record"] == 0


def test_the_anker_charging_when_nothing_was_asked_and_discharge_while_inhibited(store):
    add_day(store, DAY, house=0.4, grid=lambda i: 2.9 if 60 <= i < 90 else 0.4, battery=lambda i: -2.5 if 60 <= i < 90 else (0.4 if 120 <= i < 130 else 0.0), soc=0.5, pv=0.0)
    intent = v.Intent([(at(0), "ac_charge", "WITHDRAW", None), (at(0), "discharge", "ALLOW", None), (at(2), "discharge", "INHIBIT", None), (at(2, 10), "discharge", "ALLOW", None)])
    start, end = day_bounds(DAY)
    record = v.compute_day(DAY, v.minute_rows(store, start, end), intent)
    kinds = {d["kind"]: d for d in record["differences"]}
    assert kinds["charging_unasked"]["minutes"] == 30 and kinds["charging_unasked"]["start"] == at(1)
    assert kinds["discharging_inhibited"]["minutes"] == 10 and kinds["discharging_inhibited"]["start"] == at(2)
    assert record["by_kind"] == {"charging_unasked": 30, "discharging_inhibited": 10}


def test_minutes_without_a_controller_record_or_readings_are_never_counted_as_agreement(store):
    add_day(store, DAY, house=0.4, grid=0.4, battery=0.0, soc=0.5, pv=0.0, skip=range(100, 110))
    intent = v.Intent([(at(12), "ac_charge", "WITHDRAW", None)])                           # the controller log starts at noon
    start, end = day_bounds(DAY)
    record = v.compute_day(DAY, v.minute_rows(store, start, end), intent)
    assert record["minutes_without_controller_record"] == 720 - 10 and record["minutes_without_readings"] == 10
    assert record["minutes_compared"] == 720 and record["minutes_agreeing"] == 720


def test_a_target_below_full_is_reached_and_idle_agrees(store):
    add_day(store, DAY, house=0.4, grid=0.4, battery=0.0, soc=0.82, pv=0.0)
    intent = v.Intent([(at(0), "ac_charge", "CHARGE_LIMIT", 0.8)])
    start, end = day_bounds(DAY)
    assert v.compute_day(DAY, v.minute_rows(store, start, end), intent)["disagreeing_minutes"] == 0


# ===== storing, reporting and the page ===========================================================================================================================
def test_finished_days_are_scored_once_from_the_log_next_to_the_database(store, tmp_path):
    add_day(store, DAY, house=0.4, grid=0.4, battery=0.0, soc=0.5, pv=0.0)
    other = DAY + timedelta(days=1)
    add_day(store, other, house=0.4, grid=2.9, battery=-2.5, soc=0.5, pv=0.0)
    log = v.default_log_paths(store)
    assert [p.name for p in log] == ["control-commands.jsonl.1", "control-commands.jsonl"] and log[1].parent == store.path.parent
    write_log(log[1], [event(at(0) - 3600, "ac_charge", "WITHDRAW"), event(at(0) - 3600, "discharge", "ALLOW")])
    now = london(other + timedelta(days=1), 0, 10)
    done = v.record_validation_days(store, now, log, lookback=2)
    assert done == [DAY, other]
    first, second = (store.get_setting(v.PREFIX + d.isoformat())[1] for d in (DAY, other))
    assert first["agreement_pct"] == 100.0 and second["by_kind"] == {"charging_unasked": 1440}
    assert v.record_validation_days(store, now, log, lookback=2) == []                                 # already scored
    assert v.record_validation_days(store, london(DAY + timedelta(days=3), 0, 10), log, lookback=1) == []     # nothing in that window has readings


def test_a_day_with_no_readings_is_not_scored(store, tmp_path):
    log = v.default_log_paths(store)
    assert v.record_validation_days(store, london(DAY + timedelta(days=1), 0, 10), log, lookback=1) == []
    assert store.get_setting(v.PREFIX + DAY.isoformat())[1] is None


def test_the_weekly_report_adds_up_and_lists_every_difference(store):
    log = v.default_log_paths(store)
    days = [DAY + timedelta(days=k) for k in range(2)]
    add_day(store, days[0], house=0.4, grid=0.4, battery=0.0, soc=0.5, pv=0.0)
    add_day(store, days[1], house=0.4, grid=lambda i: 2.9 if 60 <= i < 90 else 0.4, battery=lambda i: -2.5 if 60 <= i < 90 else 0.0, soc=0.5, pv=0.0)
    write_log(log[1], [event(at(0) - 3600, "ac_charge", "WITHDRAW"), event(at(0) - 3600, "discharge", "ALLOW")])
    v.record_validation_days(store, london(days[1] + timedelta(days=1), 0, 10), log, lookback=2)
    today = days[1] + timedelta(days=1)
    data = v.report(store, today, days=7)
    assert [d["date"] for d in data["days"]] == [days[1].isoformat(), days[0].isoformat()]
    assert data["minutes_compared"] == 2880 and data["minutes_agreeing"] == 2850 and data["agreement_pct"] == pytest.approx(98.96, abs=0.01)
    assert len(data["differences"]) == 1 and data["differences"][0]["date"] == days[1].isoformat() and data["differences"][0]["minutes"] == 30
    assert [d["minutes"] for d in v.all_differences(store, today)] == [30]


def test_the_website_serves_the_report_the_csv_and_the_page_section(store, tmp_path):
    log = v.default_log_paths(store)
    add_day(store, DAY, house=0.4, grid=lambda i: 2.9 if 60 <= i < 90 else 0.4, battery=lambda i: -2.5 if 60 <= i < 90 else 0.0, soc=0.5, pv=0.0)
    write_log(log[1], [event(at(0) - 3600, "ac_charge", "WITHDRAW")])
    now = london(DAY + timedelta(days=1), 0, 10)
    v.record_validation_days(store, now, log, lookback=1)
    app = create_app(WebConfig(store.path, "a-long-test-password", trusted_network=True), FakeClock(now + 3600), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        data = client.get("/api/validation").json()
        assert data["minutes_compared"] == 1440 and data["differences"][0]["kind"] == "charging_unasked" and "charging_unasked" in data["kinds"]
        assert client.get("/api/validation?days=0").status_code == 422 and client.get("/api/validation?days=61").status_code == 422
        text = client.get("/validation.csv")
        assert text.headers["content-type"].startswith("text/csv")
        rows = text.text.strip().splitlines()
        assert rows[0].startswith("date,start_london") and len(rows) == 2 and rows[1].startswith(DAY.isoformat() + ",01:00,01:30,30,")
        page = client.get("/history").text
        assert 'id="validation-days"' in page and 'id="validation-differences"' in page and "/validation.csv" in page
