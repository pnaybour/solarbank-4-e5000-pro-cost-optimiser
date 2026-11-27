"""WP19 (reduced): the storage guard alerts and never stops anything."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts/pi-storage-guard.py"
GIB = 1024 ** 3


def load():
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def disk(free_gib=500, total_gib=917):
    return lambda _: SimpleNamespace(free=free_gib * GIB, total=total_gib * GIB)


def run(guard, tmp_path, free_gib=500, total_gib=917, now=1000.0):
    out = []
    guard.main(tmp_path / "data", usage=disk(free_gib, total_gib), alerts_dir=tmp_path / "alerts", now=lambda: now, out=out.append)
    return {json.loads(p.read_text())["key"] for p in (tmp_path / "alerts").glob("*.json")} if (tmp_path / "alerts").exists() else set(), out[0]


@pytest.fixture
def data(tmp_path):
    (tmp_path / "data").mkdir()
    return tmp_path / "data"


def test_the_guard_has_no_way_to_stop_a_service():
    text = GUARD.read_text()
    assert "systemctl" not in text and "subprocess" not in text and "SERVICES" not in text


def test_a_healthy_disk_raises_nothing(tmp_path, data):
    (data / "controller.sqlite3").write_bytes(b"x" * 1000)
    raised, line = run(load(), tmp_path)
    assert raised == set() and "within limits" in line


def test_the_backups_folder_is_not_counted_but_the_data_is(tmp_path, data, monkeypatch):
    guard = load()
    monkeypatch.setattr(guard, "DATA_ALERT_BYTES", 1000)
    (data / "backups").mkdir(); (data / "backups" / "before-x.sqlite3").write_bytes(b"x" * 5000)
    (data / "controller.sqlite3").write_bytes(b"x" * 100)
    assert run(guard, tmp_path)[0] == set()
    (data / "controller.sqlite3").write_bytes(b"x" * 5000)
    assert run(guard, tmp_path)[0] == {"storage-data-large"}


def test_low_free_space_scales_with_the_disk_and_nothing_is_stopped(tmp_path, data):
    guard = load()
    assert run(guard, tmp_path, free_gib=80, total_gib=917)[0] == {"storage-low-free"}            # a tenth of 917 GiB is 91.7 GiB
    assert run(guard, tmp_path, free_gib=80, total_gib=250)[0] == set()                            # on a 250 GiB disk 80 GiB free is fine (the floor is 25 GiB)
    assert run(guard, tmp_path, free_gib=20, total_gib=250)[0] == {"storage-low-free"}
    assert run(guard, tmp_path, free_gib=8, total_gib=60)[0] == {"storage-low-free"}               # the floor is 10 GiB on a small disk


def test_a_condition_that_clears_clears_its_alert(tmp_path, data):
    guard = load()
    assert run(guard, tmp_path, free_gib=10)[0] == {"storage-low-free"}
    assert run(guard, tmp_path, free_gib=500)[0] == set()


def test_abnormal_growth_between_hourly_checks_is_an_alert(tmp_path, data, monkeypatch):
    guard = load()
    monkeypatch.setattr(guard, "GROWTH_ALERT_BYTES", 1000)
    (data / "controller.sqlite3").write_bytes(b"x" * 100)
    run(guard, tmp_path, now=1000.0)
    (data / "controller.sqlite3").write_bytes(b"x" * 5000)
    assert run(guard, tmp_path, now=4600.0)[0] == {"storage-growth"}
    assert run(guard, tmp_path, now=8200.0)[0] == set()                                           # unchanged since the last check: the alert clears


def test_the_fixed_size_telemetry_store_changing_size_is_an_alert_but_a_first_run_is_not(tmp_path, data):
    guard = load()
    (data / "telemetry").mkdir()
    for n in range(3):
        (data / "telemetry" / f"m{n}.wsp").write_bytes(b"x" * 1000)
    assert run(guard, tmp_path, now=1000.0)[0] == set()                                           # the first time there is nothing to compare with
    assert run(guard, tmp_path, now=4600.0)[0] == set()                                           # the same sizes: fine
    (data / "telemetry" / "m0.wsp").write_bytes(b"x" * 2000)
    assert run(guard, tmp_path, now=8200.0)[0] == {"storage-telemetry-size"}
    (data / "telemetry" / "m3.wsp").write_bytes(b"x" * 1000)                                       # a new file (a new metric) is a new baseline, not an alarm
    assert run(guard, tmp_path, now=11800.0)[0] == set()


def test_the_telemetry_store_over_its_hard_cap_is_always_an_alert(tmp_path, data, monkeypatch):
    guard = load()
    monkeypatch.setattr(guard, "TELEMETRY_CAP_BYTES", 500)
    (data / "telemetry").mkdir(); (data / "telemetry" / "m.wsp").write_bytes(b"x" * 1000)
    assert run(guard, tmp_path)[0] == {"storage-telemetry-size"}


def test_the_unit_runs_as_the_service_user_and_can_only_write_the_state_and_alert_folders():
    text = (ROOT / "deployment/solarbank-optimiser-storage.service").read_text()
    assert "User=solarbank-optimiser" in text and "ReadWritePaths=/var/lib/solarbank-cost-optimiser /run/solarbank-cost-optimiser" in text and "alerts only" in text


def test_the_guard_can_load_the_alert_module_by_path_without_the_package(tmp_path):
    guard = load()
    alerts = guard._load_alerts()
    assert callable(alerts.raise_alert) and callable(alerts.clear_alert)
