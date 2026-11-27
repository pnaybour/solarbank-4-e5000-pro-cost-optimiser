from contextlib import closing
from dataclasses import asdict
import json
import sqlite3
import threading

import pytest

from solarbank_optimiser.collector import Collector, CollectorConfig, PROVENANCE, decode_snapshot, fetch_snapshot, strict_json
from solarbank_optimiser.models import FakeClock, Observation, Quality
from solarbank_optimiser.store import APPLICATION_ID, SCHEMA, Store


def payload(at=100):
    # Synthetic fixture with the shape of the approved shared endpoint. No real
    # household samples, identifiers or addresses are stored in this test.
    return {"ok": True, "sample": {"time": at, "main": 500, "sub": 100, "garage_current_a": 1.3},
            "shelly": {"ok": True, "sample": {"time": at, "ch1": 5., "ch2": 7.}},
            "solarbank": {"ok": True, "sample": {"time": at, "soc": 50, "pv": 400,
                "load": 800, "grid": 400, "battery": 0, "charge_total_kwh": 12.3, "discharge_total_kwh": 9.8}}}


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "controller.sqlite3", live_path=tmp_path / "live.json")          # readings are in the live file and the telemetry files, not the database
    result.initialise()
    return result


def test_shared_mapping_garage_current_and_its_absence():
    rows = {o.metric: o for o in decode_snapshot(payload(), 101)}
    assert rows["soc"].value == .5
    assert rows["household_power"].value == .8
    assert rows["grid_power"].value == .4
    assert rows["grid_ct_power"].value == .5
    assert rows["garage_power"].value == .1
    assert rows["ev_power"].value == .007
    assert rows["charge_energy"].value == 12.3
    assert rows["garage_current"].quality == Quality.OK and rows["garage_current"].value == 1.3 and rows["garage_current"].unit == "A"
    assert rows["ev_power"].source_at == 100
    assert rows["ev_power"].device_at is None
    assert rows["ev_power"].sample_at == 100


def test_a_source_without_garage_current_leaves_it_missing_not_zero():
    data = payload(); del data["sample"]["garage_current_a"]
    row = {o.metric: o for o in decode_snapshot(data, 101)}["garage_current"]
    assert row.quality == Quality.MISSING and row.value is None
    for bad in (-1.0, 500.0, float("nan"), True, "1.3"):
        data = payload(); data["sample"]["garage_current_a"] = bad
        row = {o.metric: o for o in decode_snapshot(data, 101)}["garage_current"]
        assert row.quality == Quality.INVALID and row.value is None


def test_upstream_staleness_not_hidden_by_refetch():
    old = {o.metric: o for o in decode_snapshot(payload(), 106)}
    assert old["soc"].quality == Quality.MISSING
    assert old["soc"].value is None
    assert old["soc"].raw_value == 50
    o = Observation("fixture", "soc", 110, .5, "fraction", source_at=100)
    assert not o.fresh(110, 5)


@pytest.mark.parametrize("bad", [True, float("nan"), None, -1, "500"])
def test_invalid_soc_preserves_other_channels(bad):
    data = payload()
    data["solarbank"]["sample"]["soc"] = bad
    rows = {o.metric: o for o in decode_snapshot(data, 101)}
    assert rows["soc"].value is None
    assert rows["ev_power"].value == .007


def test_source_failure_future_time_and_negative_charger_are_unknown():
    data = payload()
    data["shelly"]["ok"] = False
    assert next(o for o in decode_snapshot(data, 101) if o.metric == "ev_power").quality == Quality.ERROR
    data = payload(at=102)
    assert all(o.value is None for o in decode_snapshot(data, 101))
    data = payload()
    data["shelly"]["sample"]["ch2"] = -1
    assert next(o for o in decode_snapshot(data, 101) if o.metric == "ev_power").quality == Quality.INVALID


@pytest.mark.parametrize("raw", [b'[]', b'{"ok":true,"ok":false}', b'{"power":NaN}', b'{"power":Infinity}', b'broken'])
def test_malformed_json(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


@pytest.mark.parametrize("url", ["https://example.com/api/readings", "http://user:secret@localhost/api/readings",
                                "http://localhost/api/write", "http://localhost/api/readings?token=x"])
def test_only_explicit_read_endpoint(url):
    with pytest.raises(ValueError):
        CollectorConfig(endpoint=url)


def test_transport_has_timeout_limit_no_redirect_and_closes():
    seen = []

    class Response:
        status = 200
        def read(self, maximum):
            assert maximum == 101
            return b"x" * 101

    class Connection:
        def __init__(self, host, port, timeout):
            seen.append((host, port, timeout))
        def request(self, method, path, headers):
            assert (method, path) == ("GET", "/api/readings")
        def getresponse(self):
            return Response()
        def close(self):
            seen.append("closed")

    config = CollectorConfig(max_bytes=100)
    with pytest.raises(ValueError, match="size limit"):
        fetch_snapshot(config, Connection)
    assert seen == [("127.0.0.1", 8080, 2), "closed"]
    Response.status = 302
    with pytest.raises(ValueError, match="not successful"):
        fetch_snapshot(config, Connection)


def test_collector_repeated_snapshot_cannot_add_charging_energy(store):
    clock = FakeClock(101)
    data = payload()
    data["shelly"]["sample"]["ch2"] = 2000
    raw = json.dumps(data).encode()
    worker = Collector(store, CollectorConfig(confirmation_seconds=0), clock, fetch=lambda _: raw)
    assert worker.once()["status"] == "ok"
    clock.advance(1)
    worker.once()
    assert len(worker.tracker.sessions()) == 1
    assert worker.tracker.sessions()[0]["energy_kwh"] == 0
    assert store.latest_observations(PROVENANCE)["ev_power"]["source_at"] == 100
    # Fresh network requests do not keep the upstream sample fresh indefinitely.
    clock.advance(5)
    worker.once()
    assert store.get_setting("charger_tracker")[1]["label"] == "unknown"


def test_network_failure_records_missing_values_then_recovers(store):
    clock = FakeClock(101)
    responses = [json.dumps(payload()).encode(), OSError("private detail must not be logged"), json.dumps(payload(102)).encode()]
    def fetch(_):
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    worker = Collector(store, CollectorConfig(), clock, fetch)
    worker.once()
    clock.advance(1)
    failed = worker.once()
    assert failed["status"] == "unavailable"
    assert "private" not in str(failed)
    assert store.latest_observations(PROVENANCE)["ev_power"]["value"] is None
    clock.advance(1)
    assert worker.once()["status"] == "ok"
    assert store.latest_observations(PROVENANCE)["soc"]["value"] == .5


def test_latest_new_error_beats_older_success_with_same_source_time(store):
    store.add_observations([Observation("fixture", "soc", 101, .5, "fraction", source_at=100, provenance=PROVENANCE),
                            Observation("fixture", "soc", 102, None, "fraction", Quality.ERROR, provenance=PROVENANCE)])
    assert store.latest_observations(PROVENANCE)["soc"]["value"] is None


def test_schema_1_migration_preserves_records_settings_and_rejects_rollback(tmp_path):
    path = tmp_path / "previous.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        for statement in SCHEMA:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO settings VALUES('operating_state',1,?)", (json.dumps({"mode": "handover_pending"}),))
    store = Store(path)
    store.initialise()
    assert store.get_setting("operating_state") == (1, {"mode": "handover_pending"})
    with closing(store.connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    store.initialise()


def test_failed_schema_2_upgrade_preserves_schema_1(tmp_path, monkeypatch):
    import solarbank_optimiser.store as module
    path = tmp_path / "previous.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        for statement in SCHEMA:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO settings VALUES('operating_state',3,?)", (json.dumps({"mode": "handover_pending"}),))
    original = module.MIGRATION_2
    monkeypatch.setattr(module, "MIGRATION_2", original + ("NOT VALID SQL",))
    with pytest.raises(sqlite3.OperationalError):
        Store(path).initialise()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='collector_snapshots'").fetchone()[0] == 0
    monkeypatch.setattr(module, "MIGRATION_2", original)
    store = Store(path)
    store.initialise()
    assert store.get_setting("operating_state") == (3, {"mode": "handover_pending"})
    assert store.get_setting("operating_state")[1]["mode"] == "handover_pending"
    with closing(store.connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    store.initialise()


def test_raw_snapshot_archive_and_retention_preserves_typed_records(store):
    raw = json.dumps(payload()).encode()
    worker = Collector(store, CollectorConfig(), FakeClock(101), lambda _: raw)
    worker.once()
    with closing(store.connect()) as db:
        assert db.execute("SELECT raw FROM collector_snapshots").fetchone()[0] == raw
        event = json.loads(db.execute("SELECT payload FROM events WHERE kind='collector_snapshot'").fetchone()[0])
        assert event["duration_seconds"] == 0
        assert event["equipment_writes"] is False
    assert store.prune_raw(102) == 1
    assert store.observations() == []                                  # readings are not kept in the database: only the raw response, the event and the health
    assert store.latest_observations(PROVENANCE)["soc"]["value"] == .5       # they are in the live file


def test_raw_byte_budget_and_freshness_expiry_in_dashboard(store):
    from fastapi.testclient import TestClient
    from solarbank_optimiser.web.app import WebConfig, create_app
    clock = FakeClock(101)
    raw = json.dumps(payload()).encode()
    worker = Collector(store, CollectorConfig(), clock, lambda _: raw)
    worker.once()
    assert store.prune_raw(0, max_bytes=len(raw)-1) == 1
    assert store.observations() == []
    client = TestClient(create_app(WebConfig(store.path, "test-only-password-12345", status_cache_seconds=0), clock, store=store))
    client.auth = ("solarbank", "test-only-password-12345")
    before = client.get("/api/status").json()
    assert before["charger_state"] == "standby"
    assert before["readings"]["soc"]["value"] == .5
    assert before["readings"]["garage_current"]["value"] == 1.3
    clock.advance(10)
    after = client.get("/api/status").json()
    assert after["charger_state"] == "unknown"
    assert after["readings"]["soc"]["value"] is None
    assert after["readings"]["soc"]["last_good_value"] == .5
    assert after["readings"]["garage_current"]["value"] is None and after["readings"]["garage_current"]["last_good_value"] == 1.3
    assert after["charger_last_known"]["state"] == "standby"
    assert "stale" in after["collector"]["status"]
    assert not after["control_ready"]


def test_restart_preserves_handover_intent_and_writes_no_coverage_rows(store):
    store.set_setting("operating_state", {"mode": "handover_pending"}, 100, 0)
    clock = FakeClock(101)
    worker = Collector(store, CollectorConfig(), clock, lambda _: json.dumps(payload(100)).encode())
    worker.once()
    clock.advance(1)
    worker.fetch = lambda _: json.dumps(payload(101)).encode()
    worker.once()
    restarted = Collector(store, CollectorConfig(), clock, lambda _: json.dumps(payload(101)).encode())
    restarted.once()
    assert store.get_setting("operating_state")[1]["mode"] == "handover_pending"
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM charger_coverage").fetchone()[0] == 0          # covered seconds live in the telemetry count file (WP23b)


def test_missing_collection_table_is_not_a_healthy_schema(store):
    with closing(store.connect()) as db, db:
        db.execute("DROP TABLE collector_snapshots")
    with pytest.raises(ValueError, match="incompatible collection"):
        store.initialise()

