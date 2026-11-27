from contextlib import closing
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import sqlite3

import pytest

from solarbank_optimiser.adapters import counter_kwh, current_a, power_kw, registers, shelly_observations
from solarbank_optimiser.control import NoEquipmentWriter, SingleOwner, request_handover
from solarbank_optimiser.energy import day_bounds, integrate
from solarbank_optimiser.forecasts import latest_normal_day
from solarbank_optimiser.history import verify_manifest
from solarbank_optimiser.models import CommandResult, DemandMode, FakeClock, Observation, Plan, Quality, number
from solarbank_optimiser.settings import DemandSelection, FallbackTariff, london_input
from solarbank_optimiser.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "controller.sqlite3")
    result.initialise()
    return result


@pytest.mark.parametrize("bad", [True, None, "12", float("nan"), float("inf"), -float("inf")])
def test_invalid_numbers(bad):
    with pytest.raises(ValueError):
        number(bad)


@pytest.mark.parametrize("words", [[], [1], [1, 2, 3], [True, 0], [65536, 0], [-1, 0], [1.0, 0], "xx"])
def test_invalid_registers(words):
    with pytest.raises(ValueError):
        registers(words, 2)


def test_signed_units_counters():
    assert power_kw([65535, 64536]) == -1
    assert power_kw([0, 2500]) == 2.5
    assert counter_kwh([1, 2]) == 6553.8
    assert current_a([4501]) == 45.01
    assert current_a([65535]) == -0.01


def test_shelly_channel_failure_is_independent():
    results = shelly_observations({"em1:0": {"act_power": True}, "em1:1": {"act_power": 2000}}, 100)
    assert results[0].quality == Quality.INVALID
    assert results[0].value is None
    assert results[1].metric == "ev_power"
    assert results[1].value == 2
    assert results[1].device_at is None
    assert results[1].raw_value == 2000


def test_shelly_error_flags():
    readings = shelly_observations({"em1:0": {"act_power": 0, "errors": ["out_of_range"]}}, 100)
    assert all(o.quality == Quality.INVALID for o in readings)


def test_observation_validation_freshness():
    o = Observation("fixture", "soc", 100, 0.5, "fraction")
    assert o.fresh(105, 5)
    assert not o.fresh(106, 5)
    assert not o.fresh(99, 5)
    assert o.device_at is None
    with pytest.raises(ValueError):
        replace(o, value=50)
    with pytest.raises(ValueError):
        replace(o, unit="percent")
    with pytest.raises(ValueError):
        replace(o, quality=Quality.ERROR)


def test_clock_elapsed_does_not_follow_wall_clock():
    clock = FakeClock(100)
    clock.advance(4)
    clock.wall -= 200
    assert clock.monotonic() == 4


def test_plan_expiry_fingerprint_and_command_evidence():
    plan = Plan(100, 200, "a" * 64, .85)
    assert plan.current(100, "a" * 64)
    assert not plan.current(200, "a" * 64)
    assert not plan.current(150, "b" * 64)
    with pytest.raises(ValueError):
        Plan(100, 200, "a" * 64, .85, status="automatic")
    command = CommandResult("fixture-command", 100, 110, acknowledged_at=101)
    assert command.readback_at is None
    assert command.observed_at is None
    with pytest.raises(ValueError):
        CommandResult("bad", 100, 90)


def test_new_schema_idempotence_and_deduplication(store):
    o = Observation("fixture", "household_power", 100, 2, "kW")
    assert store.add_observations([o]) == 1
    assert store.add_observations([o]) == 0
    store.initialise()
    assert store.observations()[0]["device_at"] is None
    assert len(store.observations()) == 1


def test_fresh_retrieval_is_not_device_update(store):
    o = Observation("fixture", "soc", 100, 0.5, "fraction")
    store.add_observations([o, replace(o, retrieved_at=101)])
    rows = store.observations()
    assert len(rows) == 2
    assert all(row["device_at"] is None for row in rows)


def test_existing_monitor_database_is_rejected_byte_for_byte(tmp_path):
    path = tmp_path / "monitor.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE readings (value REAL)")
        db.execute("INSERT INTO readings VALUES(42)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="non-controller"):
        Store(path).initialise()
    assert path.read_bytes() == before


def test_future_schema_rejected(store):
    with closing(store.connect()) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="Unsupported"):
        store.initialise()


def test_failed_initial_migration_rolls_back_atomically(tmp_path, monkeypatch):
    import solarbank_optimiser.store as module
    path = tmp_path / "migration.sqlite3"
    original = module.SCHEMA
    monkeypatch.setattr(module, "SCHEMA", original + ("NOT VALID SQL",))
    with pytest.raises(sqlite3.OperationalError):
        Store(path).initialise()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
    monkeypatch.setattr(module, "SCHEMA", original)
    Store(path).initialise()


def test_incomplete_controller_schema_is_rejected(store):
    with closing(store.connect()) as db, db:
        db.execute("ALTER TABLE observations RENAME COLUMN sample_at TO wrong_column")
    with pytest.raises(ValueError, match="incompatible"):
        store.initialise()


def test_backup_restore_preserves_settings_and_records(store, tmp_path):
    o = Observation("fixture", "soc", 100, 0.7, "fraction")
    store.add_observations([o])
    store.set_setting("test", {"mode": "shadow"}, 100, 0)
    backup = store.backup(tmp_path / "backup.sqlite3")
    restored = Store(backup)
    restored.initialise()
    assert restored.observations() == store.observations()
    assert restored.get_setting("test") == (1, {"mode": "shadow"})
    with pytest.raises(FileExistsError):
        store.backup(backup)
    with pytest.raises(ValueError):
        store.backup(store.path)
    recovered = Store(tmp_path / "recovered.sqlite3")
    recovered.restore(backup)
    assert recovered.observations() == store.observations()
    assert recovered.get_setting("test") == store.get_setting("test")
    with pytest.raises(FileExistsError):
        recovered.restore(backup)


def test_settings_competing_updates_rejected(store):
    assert store.set_setting("test", {"value": 1}, 100, 0) == 1
    with pytest.raises(ValueError, match="refresh"):
        store.set_setting("test", {"value": 2}, 101, 0)
    assert store.get_setting("test")[1] == {"value": 1}


def test_immutable_forecast_archive_exact_bytes(store):
    raw = b'{ "original": [1, 2] }'
    identity = store.archive(100, raw, {"intervals": []})
    assert store.archive(100, raw, {"intervals": []}) == identity
    with closing(store.connect()) as db:
        assert db.execute("SELECT raw FROM issuances").fetchone()[0] == raw
        for statement in ("UPDATE issuances SET acquired_at=101", "DELETE FROM issuances"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)


def test_tampered_manifest_evidence_rejected(tmp_path):
    (tmp_path / "history").mkdir()
    (tmp_path / "history/data").write_bytes(b"changed")
    (tmp_path / "history/manifest.json").write_text(json.dumps({"files": [{"path": "history/data", "bytes": 7, "sha256": "bad"}]}))
    with pytest.raises(ValueError, match="failed verification"):
        verify_manifest(tmp_path)


def test_integration_threshold_gaps_and_signs():
    pieces = integrate(0, 1800, 2, 2, 1800, .01)
    assert sum(p.positive_kwh for p in pieces) == pytest.approx(1)
    assert sum(p.active_seconds for p in pieces) == 1800
    assert len(pieces) == 30
    assert sum(p.positive_kwh for p in integrate(0, 1800, .007, .007, 1800, .01)) == 0
    assert integrate(0, 6, 2, 2, 5) == []
    assert integrate(1, 1, 2, 2, 5) == []
    assert integrate(2, 1, 2, 2, 5) == []
    pieces = integrate(0, 60, -1, 1, 60)
    assert sum(p.positive_kwh for p in pieces) == pytest.approx(1 / 240)
    assert sum(p.negative_kwh for p in pieces) == pytest.approx(1 / 240)


def test_threshold_crossing_counts_full_power():
    pieces = integrate(0, 10, 0, .02, 10, .01)
    assert sum(p.active_seconds for p in pieces) == 5
    assert sum(p.positive_kwh for p in pieces) == pytest.approx(.015 * 5 / 3600)


@pytest.mark.parametrize("day,minutes", [(date(2026, 3, 29), 1380), (date(2026, 10, 25), 1500), (date(2026, 10, 20), 1440)])
def test_complete_london_days(day, minutes):
    start, end = day_bounds(day)
    rows = [Observation("fixture", "household_power", t, 1, "kW", coverage_seconds=60, demand_mode=DemandMode.NORMAL)
            for t in range(int(start), int(end), 60)]
    assert len(rows) == minutes
    selected, result = latest_normal_day(rows, end + 3600, commissioned_since=date(2026, 1, 1))
    assert selected == day
    assert len(result) == minutes
    for invalid in (rows[:-1], rows + [rows[0]], [replace(o, coverage_seconds=59) for o in rows],
                    [replace(rows[0], demand_mode=DemandMode.AWAY)] + rows[1:]):
        with pytest.raises(ValueError):
            latest_normal_day(invalid, end + 3600)
    with pytest.raises(ValueError):
        latest_normal_day(rows, end - 1)
    with pytest.raises(ValueError):
        latest_normal_day(rows, end + 8 * 86400)


def test_demand_expiry_and_future_visit():
    selected = DemandSelection(mode="High demand", start=200., end=400., high_extra_kwh=5.)
    assert selected.active_mode(199) == DemandMode.NORMAL
    assert selected.active_mode(200) == DemandMode.HIGH
    assert DemandSelection.model_validate_json(selected.model_dump_json()).active_mode(500) == DemandMode.NORMAL
    with pytest.raises(ValueError):
        DemandSelection(mode="Away from home", start=100.)


def test_london_ambiguous_and_skipped_clocks():
    with pytest.raises(ValueError, match="occurs twice"):
        london_input("2026-10-25T01:30")
    assert london_input("2026-10-25T01:30", 1) - london_input("2026-10-25T01:30", 0) == 3600
    with pytest.raises(ValueError, match="does not exist"):
        london_input("2026-03-29T01:30")


def test_fallback_tariff_boundaries_and_dst():
    tariff = FallbackTariff(day_pence=30., cheap_pence=8., valid_from=0.)
    assert tariff.at(london_input("2026-10-24T23:29")) == (.30, False)
    assert tariff.at(london_input("2026-10-24T23:30")) == (.08, True)
    assert tariff.at(london_input("2026-10-25T01:30", 0)) == (.08, True)
    assert tariff.at(london_input("2026-10-25T01:30", 1)) == (.08, True)
    assert tariff.at(london_input("2026-10-25T05:30")) == (.30, False)
    with pytest.raises(ValueError):
        FallbackTariff(day_pence=float("nan"), cheap_pence=8., valid_from=0.)


def test_no_equipment_commands_even_with_eligible_decision():
    with pytest.raises(RuntimeError, match="not implemented"):
        NoEquipmentWriter().send("charge", 2.5)


def test_single_owner_and_handover_persistence(store, tmp_path):
    with SingleOwner(tmp_path / "owner.lock"):
        with pytest.raises(RuntimeError, match="Another controller"):
            with SingleOwner(tmp_path / "owner.lock"):
                pass
    with SingleOwner(tmp_path / "owner.lock"):
        pass
    request_handover(store, 100, 0)
    store.initialise()
    assert store.get_setting("operating_state")[1]["mode"] == "handover_pending"

