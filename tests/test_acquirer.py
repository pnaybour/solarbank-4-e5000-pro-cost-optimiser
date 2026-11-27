"""4a.3b: the direct acquirer presents device readings in the monitor's shape; the collector runs on it unchanged."""
import json
import threading
import time

import pytest

from fake_devices import FakeModbus, FakeShelly
from solarbank_optimiser.acquirer import DEVICES, DirectAcquirer, DirectConfig, parse_devices
from solarbank_optimiser.shelly_channels import from_monitor
from solarbank_optimiser.collector import Collector, CollectorConfig, PROVENANCE, decode_snapshot
from solarbank_optimiser.live import Reading
from solarbank_optimiser.models import Clock
from solarbank_optimiser.store import Store

FIXTURES = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "monitor-raw" / "monitor-raw.json").read_text())
SB = FIXTURES["solarbank"][0]["expected"]
SHELLY = from_monitor(FIXTURES["shelly"][0]["expected"])      # the recorded monitor sample, in role names
T = 1_800_000_000.0


def summary(observations):
    return sorted((o.metric, o.value, str(o.quality)) for o in observations)


def monitor_document(at=T):
    return {"ok": True, "sample": {"time": at, "main": 10, "sub": -324},
            "shelly": {"ok": True, "sample": {"time": at, **SHELLY}},
            "solarbank": {"ok": True, "sample": {"time": at, **SB}}}


def acquirer_with(values, devices=DEVICES, now=T, max_age=5.0):
    clock = {"mono": 1000.0}
    acquirer = DirectAcquirer(DirectConfig(devices=devices, max_age_seconds=max_age, solarbank_host="127.0.0.1", meter_host="127.0.0.1", shelly_host="127.0.0.1"), readers={d: (lambda: {}) for d in devices},
                              monotonic=lambda: clock["mono"])
    for name, value in values.items():
        acquirer.live.publish(name, Reading(value, mono=clock["mono"], utc=now, seq=1, latency=.02))
    return acquirer, clock


@pytest.mark.parametrize("text,expected", [("none", ()), ("", ()), ("all", DEVICES), ("shelly", ("shelly",)),
                                           ("shelly,solarbank", ("shelly", "solarbank")), (" Meter , shelly ", ("meter", "shelly"))])
def test_device_list_parsing(text, expected):
    assert parse_devices(text) == expected


@pytest.mark.parametrize("bad", ["fridge", "shelly,shelly", "shelly,,meter", "solarbank;meter"])
def test_bad_device_lists_are_refused(bad):
    with pytest.raises(ValueError):
        parse_devices(bad)


def test_direct_document_decodes_to_the_same_observations_as_the_monitor_document():
    acquirer, _ = acquirer_with({"solarbank": SB, "meter": {"main": 10, "sub": -324}, "shelly": SHELLY})
    direct = decode_snapshot(acquirer.snapshot_json(), T + 1)
    bridge = decode_snapshot(monitor_document(), T + 1)
    assert summary(direct) == summary(bridge)
    assert sum(o.quality == "ok" for o in direct) == 11


def test_mixed_mode_takes_only_the_chosen_devices_from_the_direct_readers():
    other = monitor_document()
    other["solarbank"]["sample"] = {**other["solarbank"]["sample"], "soc": 10}
    other["sample"] = {"time": T, "main": 99, "sub": 99}
    acquirer, _ = acquirer_with({"solarbank": SB}, devices=("solarbank",))
    document = acquirer.snapshot_json(other)
    assert document["solarbank"]["sample"]["soc"] == 71                  # direct
    assert document["sample"]["main"] == 99 and document["shelly"] == other["shelly"]   # still the monitor's


def test_mixed_mode_survives_a_dead_monitor_and_direct_groups_stay_valid():
    acquirer, _ = acquirer_with({"solarbank": SB}, devices=("solarbank",))
    document = json.loads(acquirer.fetch(None, monitor_fetch=lambda config: (_ for _ in ()).throw(OSError("monitor down"))))
    assert document["solarbank"]["ok"] is True
    observations = decode_snapshot(document, T + 1)
    soc = [o for o in observations if o.metric == "soc"][0]
    ev = [o for o in observations if o.metric == "ev_power"][0]
    assert soc.quality == "ok" and ev.quality != "ok"                    # the rest is unknown, not guessed


def test_pure_direct_never_contacts_the_monitor():
    acquirer, _ = acquirer_with({"solarbank": SB, "meter": {"main": 1, "sub": 2}, "shelly": SHELLY})
    called = []
    acquirer.fetch(None, monitor_fetch=lambda config: called.append(1) or b"{}")
    assert called == []


def test_old_readings_are_reported_as_unavailable_not_as_fresh():
    acquirer, clock = acquirer_with({"solarbank": SB}, devices=("solarbank",), max_age=5)
    assert acquirer.group("solarbank")["ok"] is True
    clock["mono"] += 5.5
    group = acquirer.group("solarbank")
    assert group["ok"] is False and "stale" in group["error"] and group["sample"] is None
    observations = decode_snapshot(acquirer.snapshot_json({}), T + 1)
    assert [o for o in observations if o.metric == "soc"][0].quality != "ok"


def test_a_device_never_read_is_unavailable_with_its_reason():
    acquirer, _ = acquirer_with({}, devices=("meter",))
    assert acquirer.group("meter") == {"ok": False, "error": "no reading yet", "sample": None}


def test_acquirer_has_no_write_capability():
    import inspect
    from solarbank_optimiser import acquirer as module
    source = inspect.getsource(module).lower()
    for word in ("write_register", "write_coil", "post(", "put(", "sendall"):
        assert word not in source


def registers_for_solarbank():
    raw = FIXTURES["solarbank"][0]["raw"]
    registers = {}
    for start, values in raw.items():
        registers.update({int(start) + i: v for i, v in enumerate(values)})
    return registers


def encode32(value):
    word = value & 0xFFFFFFFF
    return word >> 16, word & 0xFFFF


@pytest.fixture
def lab():
    meter_regs = {}
    meter_regs[10632] = 2300                                            # primary phase 1 voltage, UINT16 / 10 (GitHub issue 69): 230.0 V
    meter_regs[10644], meter_regs[10645] = encode32(-8)
    meter_regs[10675], meter_regs[10676] = encode32(-289)
    meter_regs.update({10632 + i: 0 for i in range(45) if 10632 + i not in meter_regs})
    shelly_status = {f"em1:{i}": {"act_power": [363.1, 3.1][i], "voltage": 235.6, "current": [1.557, .03][i]} for i in range(2)}
    servers = (FakeModbus(registers_for_solarbank()), FakeModbus(meter_regs),
               FakeShelly({"/rpc/Shelly.GetStatus": (200, shelly_status),
                           "/rpc/EM1.GetStatus?id=0": (200, shelly_status["em1:0"]), "/rpc/EM1.GetStatus?id=1": (200, shelly_status["em1:1"])}))
    yield servers
    for server in servers:
        server.stop()


def lab_config(servers, **options):
    return DirectConfig(devices=DEVICES, solarbank_host="127.0.0.1", meter_host="127.0.0.1", shelly_host="127.0.0.1",
                        solarbank_port=servers[0].port, meter_port=servers[1].port, shelly_port=servers[2].port,
                        timeout_seconds=.5, period_seconds=.1, **options)


def test_collector_runs_end_to_end_on_the_direct_readers(tmp_path, lab):
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    acquirer = DirectAcquirer(lab_config(lab)).start()
    try:
        worker = Collector(store, CollectorConfig(max_age_seconds=5), Clock(), acquirer=acquirer)
        time.sleep(.6)
        health = worker.once()
        latest = store.latest_observations(PROVENANCE)
    finally:
        acquirer.stop()
    assert health["status"] == "ok" and health["direct_devices"] == list(DEVICES)
    assert "Direct read-only" in health["source"]
    assert latest["soc"]["value"] == pytest.approx(.71) and latest["battery_power"]["value"] == pytest.approx(.32)
    assert latest["household_power"]["value"] == pytest.approx(.38) and latest["grid_power"]["value"] == 0
    assert latest["grid_ct_power"]["value"] == pytest.approx(-.008) and latest["garage_power"]["value"] == pytest.approx(-.289)
    assert latest["ev_power"]["value"] == pytest.approx(.0031)
    for name in DEVICES:
        device = health["devices"][name]
        assert device["seq"] >= 2 and device["fail_streak"] == 0 and 0 <= device["age"] < 1 and device["latency"] < .5
    # Only reads reached the devices.
    assert set(lab[0].functions) == {4} and set(lab[1].functions) == {4} and set(lab[2].methods) == {"GET"}


def test_an_unplugged_device_becomes_unknown_while_the_others_continue(tmp_path, lab):
    store = Store(tmp_path / "c.sqlite3", live_path=tmp_path / "live.json"); store.initialise()          # readings live in the live file and the telemetry files, not the database
    acquirer = DirectAcquirer(lab_config(lab, max_age_seconds=.6)).start()
    try:
        worker = Collector(store, CollectorConfig(max_age_seconds=5), Clock(), acquirer=acquirer)
        time.sleep(.5)
        assert worker.once()["status"] == "ok"
        lab[0].mode = "silent"                                   # the Solarbank stops answering
        time.sleep(2.0)
        health = worker.once()
        latest = store.latest_observations(PROVENANCE, "ok")
    finally:
        lab[0].mode = "normal"
        acquirer.stop()
    assert health["status"] == "partial"
    assert health["devices"]["solarbank"]["fail_streak"] >= 1 and health["devices"]["shelly"]["fail_streak"] == 0
    assert "ev_power" in latest and latest["ev_power"]["retrieved_at"] > latest["soc"]["retrieved_at"]    # Shelly newer than the last good SOC


def test_stop_ends_every_device_thread_and_closes_connections(tmp_path, lab):
    own = lambda: [t for t in threading.enumerate() if t.name.startswith("dev-")]
    acquirer = DirectAcquirer(lab_config(lab)).start()
    time.sleep(.3)
    assert sorted(t.name for t in own()) == ["dev-meter", "dev-shelly", "dev-solarbank"]
    acquirer.stop()
    assert own() == []
    assert acquirer.stop_event.is_set()


def test_command_line_refuses_an_unknown_direct_device(tmp_path):
    from solarbank_optimiser.cli import main
    with pytest.raises(SystemExit):
        main(["--database", str(tmp_path / "x.sqlite3"), "collect", "--direct-devices", "fridge", "--count", "1"])


# ---- review item 7: ownership first, equipment second ----------------------------------------------

def test_a_second_collector_is_refused_before_it_contacts_any_device(tmp_path, lab):
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    first_acquirer = DirectAcquirer(lab_config(lab))
    first = Collector(store, CollectorConfig(), Clock(), acquirer=first_acquirer)
    stop = threading.Event()
    owner = threading.Thread(target=lambda: first.run(stop, None, before=first_acquirer.start, after=first_acquirer.stop))
    owner.start()
    try:
        time.sleep(.8)
        assert lab[1].connections == 1                                   # the meter has exactly one client: the owner
        before_connections = [s.connections for s in lab[:2]]
        second_acquirer = DirectAcquirer(lab_config(lab))
        started = []
        second = Collector(store, CollectorConfig(), Clock(), acquirer=second_acquirer)
        with pytest.raises(RuntimeError, match="Another controller owns this installation"):
            second.run(threading.Event(), 1, before=lambda: (started.append(1), second_acquirer.start()), after=second_acquirer.stop)
        time.sleep(.5)
        assert started == []                                             # the start-up hook never ran
        assert [s.connections for s in lab[:2]] == before_connections    # and no device saw a new connection
        assert not any(t.name.startswith("dev-") and t is not None and t in second_acquirer._loops for t in threading.enumerate())
    finally:
        stop.set(); owner.join(10)


def test_after_hook_runs_even_when_the_loop_fails(tmp_path):
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    worker = Collector(store, CollectorConfig(), Clock(), fetch=lambda config: b"{}")
    worker.once = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    worker.MAX_CONSECUTIVE_TICK_ERRORS = 1                       # a persistently failing loop stops (and systemd restarts it); the after-hook still runs
    ran = []
    with pytest.raises(RuntimeError, match="boom"):
        worker.run(threading.Event(), 1, before=lambda: ran.append("before"), after=lambda: ran.append("after"))
    assert ran == ["before", "after"]


def test_one_failing_tick_does_not_end_the_collector_it_is_counted_and_the_next_tick_runs(tmp_path):
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    worker = Collector(store, CollectorConfig(), Clock(), fetch=lambda config: b"{}")
    real, calls = worker.once, {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("one bad tick")
        return real()
    worker.once = flaky
    worker.run(threading.Event(), 4)
    assert calls["n"] == 4 and worker.tick_errors == 1


def test_the_command_line_hands_the_start_up_to_the_locked_run():
    import inspect
    from solarbank_optimiser import cli
    source = inspect.getsource(cli)
    assert "acquirer.start()" not in source and "before=acquirer.start" in source


def test_acquirer_health_shows_the_solarbank_slow_register_age_and_failures(lab):
    acquirer = DirectAcquirer(lab_config(lab)).start()
    try:
        time.sleep(.6)
        health = acquirer.health()["solarbank"]
        assert {"slow_age", "slow_errors", "slow_error_streak", "slow_stale"} <= set(health)
        assert health["slow_stale"] is False and health["slow_errors"] == 0
        assert "slow_age" not in acquirer.health()["shelly"]
    finally:
        acquirer.stop()
