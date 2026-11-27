"""Retiring the original monitor: the controller owns the meter (one Modbus client only) and shares each reading through a RAM file;
the collector and dashboard read that file. Stale or missing means unavailable, never an old value that looks fresh."""
import json
import time
from pathlib import Path

import pytest

from solarbank_optimiser.acquirer import DirectAcquirer, DirectConfig
from solarbank_optimiser.collector import decode_snapshot
from solarbank_optimiser.devices import DeviceError
from solarbank_optimiser.devices.feed import FileFeedReader, MAX_BYTES
from solarbank_optimiser.live import LiveState, Reading
from solarbank_optimiser.models import Quality
from test_control_loop import MAXW, UTC, Clock, build, live_with, plan

METER = {"main": -5, "sub": -293, "voltage": 230.0, "garage_current_a": 1.29, "garage_phase_currents_a": [1.29, 0.0, 0.0]}
ROOT = Path(__file__).resolve().parents[1]


def feed(path, at, values=None, seq=1):
    path.write_text(json.dumps({"at": at, "seq": seq, "values": values if values is not None else METER}))


# ---- the reader ---------------------------------------------------------------------------------------------------------------
def test_a_fresh_feed_is_read_and_stamped_with_when_the_meter_was_read(tmp_path):
    f = tmp_path / "meter.json"
    feed(f, 1000.0)
    reader = FileFeedReader(f, clock=lambda: 1000.8, monotonic=lambda: 50.0)
    assert reader() == METER
    assert reader.received_at == pytest.approx(50.0 - 0.8)             # the METER's moment, not the moment this file was opened


@pytest.mark.parametrize("age,problem", [(2.6, "timeout"), (600, "timeout"), (-5, "invalid")])
def test_a_stale_or_future_feed_is_unavailable_never_an_old_value(tmp_path, age, problem):
    f = tmp_path / "meter.json"; feed(f, 1000.0)
    reader = FileFeedReader(f, clock=lambda: 1000.0 + age)
    with pytest.raises(DeviceError) as error:
        reader()
    assert error.value.kind == problem and reader.received_at is None


@pytest.mark.parametrize("content", [b"", b"{broken", b"[]", b'{"at": 1}', b'{"at": true, "values": {}}', b'{"at": "x", "values": {}}',
                                     b'{"at": 1000, "values": {"main": 1, "sub": 2}}',
                                     b'{"at": 1000, "values": {"main": 1, "sub": 2, "garage_current_a": NaN}}',
                                     b'{"at": 1000, "values": {"main": 1, "sub": 2, "garage_current_a": true}}',
                                     b'{"at": 1000, "values": {"main": 1, "sub": "2", "garage_current_a": 1}}',
                                     b"[" * 30000, b"\xff\xfe", b" " * (MAX_BYTES + 5)])
def test_a_damaged_feed_is_refused_without_breaking_the_reader(tmp_path, content):
    f = tmp_path / "meter.json"; f.write_bytes(content)
    with pytest.raises(DeviceError):
        FileFeedReader(f, clock=lambda: 1000.0)()


def test_a_missing_feed_means_the_controller_is_not_publishing(tmp_path):
    with pytest.raises(DeviceError) as error:
        FileFeedReader(tmp_path / "none.json")()
    assert error.value.kind == "connect" and "controller" in str(error.value)


# ---- the controller publishes it -------------------------------------------------------------------------------------------------
def controller_with_meter(tmp_path, clock, reading=METER):
    live = live_with(clock)
    live.publish("meter", Reading(dict(reading), clock.t, UTC, 1, .04))
    (tmp_path / "plan.json").write_text(json.dumps(plan()))
    lp = build(tmp_path, clock, live, devices=("solarbank", "shelly", "meter"), meter_feed_path=tmp_path / "meter.json")
    return lp


def test_the_controller_writes_each_new_meter_reading_once_and_acts_on_the_current(tmp_path):
    clock = Clock(); lp = controller_with_meter(tmp_path, clock)
    lp.cycle()
    document = json.loads((tmp_path / "meter.json").read_text())
    assert document["values"]["garage_current_a"] == 1.29 and document["seq"] == 1 and document["at"] == pytest.approx(UTC)
    stamp = (tmp_path / "meter.json").stat().st_mtime_ns
    clock.t += 1; lp.cycle()                                                      # no new reading: not rewritten
    assert (tmp_path / "meter.json").stat().st_mtime_ns == stamp
    assert not list(tmp_path.glob("*.tmp"))


def test_without_a_meter_reading_nothing_is_published_and_charging_stays_stopped(tmp_path):
    clock = Clock(); live = live_with(clock)
    (tmp_path / "plan.json").write_text(json.dumps(plan()))
    lp = build(tmp_path, clock, live, devices=("solarbank", "shelly", "meter"), meter_feed_path=tmp_path / "meter.json")
    d, _ = lp.cycle()
    assert not (tmp_path / "meter.json").exists() and "garage_unknown" in d.restrictions and d.charge_limit_w == 0


def test_the_garage_current_now_limits_charging(tmp_path):
    clock = Clock(); lp = controller_with_meter(tmp_path, clock, dict(METER, garage_current_a=48.0))
    d, _ = lp.cycle()
    assert "garage_limit" in d.restrictions and "garage_unknown" not in d.restrictions


# ---- end to end: meter -> controller -> file -> collector -> observations ---------------------------------------------------------------
def test_the_collector_gets_the_meter_only_through_the_controllers_file(tmp_path):
    clock = Clock(); lp = controller_with_meter(tmp_path, clock)
    now = time.time()
    lp.cycle()
    document = json.loads((tmp_path / "meter.json").read_text())
    document["at"] = now                                                         # make the controller's reading current in real time
    (tmp_path / "meter.json").write_text(json.dumps(document))
    acquirer = DirectAcquirer(DirectConfig(devices=("meter",), meter_feed=str(tmp_path / "meter.json"), period_seconds=0.1))
    acquirer.start()
    try:
        deadline = time.time() + 3
        while time.time() < deadline and acquirer.group("meter")["ok"] is not True:
            time.sleep(0.05)
        group = acquirer.group("meter")
        assert group["ok"] is True and group["sample"]["garage_current_a"] == 1.29
        data = acquirer.snapshot_json({})
        rows = {o.metric: o for o in decode_snapshot(data, group["sample"]["time"] + 1)}
        assert rows["garage_current"].quality == Quality.OK and rows["garage_current"].value == 1.29
        assert rows["garage_power"].value == pytest.approx(-0.293) and rows["grid_ct_power"].value == pytest.approx(-0.005)
        assert acquirer._clients == []                                           # the collector opened no Modbus connection for the meter
        document["at"] = now - 60                                                # the controller stops: the feed goes stale
        (tmp_path / "meter.json").write_text(json.dumps(document))
        deadline = time.time() + 5
        while time.time() < deadline and acquirer.group("meter")["ok"] is True:
            time.sleep(0.05)
        stale = acquirer.group("meter")
        assert stale["ok"] is False and stale["sample"] is None                 # unavailable, not the old 1.29 A
    finally:
        acquirer.stop()


def test_the_collector_command_line_accepts_the_meter_feed(tmp_path, monkeypatch):
    from solarbank_optimiser import cli
    for key in ("SOLARBANK_HOST", "SOLARBANK_SHELLY_HOST"):
        monkeypatch.setenv(key, "127.0.0.1")
    monkeypatch.setenv("SOLARBANK_DEVICES_FILE", str(tmp_path / "none.conf"))
    seen = {}
    class Fake:
        def __init__(self, store, config, acquirer=None): seen["acquirer"] = acquirer
        def run(self, *a, **k): pass
    import solarbank_optimiser.collector as collector
    monkeypatch.setattr(collector, "Collector", Fake)
    cli.main(["--database", str(tmp_path / "c.sqlite3"), "collect", "--source", "direct", "--meter-feed", str(tmp_path / "meter.json"), "--count", "1"])
    assert seen["acquirer"].config.meter_feed == str(tmp_path / "meter.json") and set(seen["acquirer"].config.devices) == {"solarbank", "meter", "shelly"}


# ---- the deployed units -----------------------------------------------------------------------------------------------------------------
def test_the_units_split_the_meter_ownership_correctly():
    collector = (ROOT / "deployment/solarbank-optimiser-collector.service").read_text()
    control = (ROOT / "deployment/solarbank-optimiser-control.service").read_text()
    assert "--source direct" in collector and "--meter-feed /run/solarbank-cost-optimiser/meter.json" in collector
    assert "8080" not in collector and "--endpoint" not in collector                      # no dependence on the original monitor
    assert "--meter-feed" in collector                                                    # the collector never contacts the meter: it reads the controller's feed (the unit may reach the home network, WP20)
    assert "--devices solarbank,shelly,meter" in control and "--meter-after-cutover" in control
    assert "192.168.0.0/16" in control.split("IPAddressAllow=")[1].splitlines()[0]        # the controller may reach the home network, where the meter is (its address is in devices.conf)


def test_the_rollback_script_is_valid_shell_and_refuses_bad_input(tmp_path):
    import subprocess
    script = ROOT / "scripts/rollback-cutover.sh"
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
    for argument in ([], ["bad id; rm -rf /"], ["no-such-release"]):
        result = subprocess.run(["sh", str(script), *argument], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "SOLARBANK_ROOT": str(tmp_path)})
        assert result.returncode == 2
    text = script.read_text()
    assert text.index("systemctl stop solarbank-optimiser-control") < text.index("enable --now anker-energy-monitor") < text.index("switch-release.sh")


def test_nothing_the_application_runs_depends_on_the_old_monitors_folder():
    import subprocess
    for path in (ROOT / "deployment").glob("*.service"):
        assert "/home/pi/" not in path.read_text(), path.name                       # units run only from /opt and /var/lib
    shadow = (ROOT / "deployment/solarbank-optimiser-shadow.service").read_text()
    assert "--solver-python /opt/solarbank-cost-optimiser/solver-venv/bin/python" in shadow
    activate = (ROOT / "scripts/activate-pi-shadow.sh").read_text()
    assert "solver-venv/bin/python" in activate and activate.index("solver-venv") < activate.index("systemctl enable --now")   # refuses to start without it
    bootstrap = ROOT / "scripts/bootstrap-solver.sh"
    assert subprocess.run(["sh", "-n", str(bootstrap)]).returncode == 0 and "${1:-.emhass-venv}" in bootstrap.read_text()
    for script in (ROOT / "scripts").glob("*.sh"):
        if script.name != "rollback-cutover.sh":                                    # the rollback script names the monitor on purpose
            assert "anker-energy-monitor/" not in script.read_text(), script.name


def test_the_deploy_script_no_longer_requires_the_retired_monitor_but_does_require_the_solver_environment():
    text = (ROOT / "scripts/deploy-pi-shadow.sh").read_text()
    assert "is-active anker-energy-monitor" not in text and "solver-venv/bin/python" in text


def test_the_monitor_guard_and_the_ownership_check_are_in_the_repository():
    import subprocess
    conf = (ROOT / "deployment/monitor-no-meter.conf").read_text()
    assert "IPAddressDeny=192.168.1.20" in conf and "[Service]" in conf and "Remove this file to undo" in conf
    script = ROOT / "scripts/check-meter-ownership.sh"
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert "solarbank-optimiser-control.service" in text and "IPAddressDeny" in text and "exit \"$failed\"" in text
    assert "write" not in text.lower() and "systemctl stop" not in text and "systemctl start" not in text       # a check only
