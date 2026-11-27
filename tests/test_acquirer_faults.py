"""4a.3c: the control path under faults. Devices vanish and return, links flap, the logger stalls, the database locks,
the CPU is busy. The device loops must keep their cadence, stale data must never look fresh, and nothing is lost or doubled."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from fake_devices import FakeModbus, FakeShelly
from solarbank_optimiser.acquirer import DEVICES, DirectAcquirer, DirectConfig
from solarbank_optimiser.collector import Collector, CollectorConfig, PROVENANCE
from solarbank_optimiser.live import Fanout
from solarbank_optimiser.models import Clock
from solarbank_optimiser.store import Store

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "monitor-raw" / "monitor-raw.json").read_text())


def sb_registers():
    registers = {}
    for start, values in FIXTURE["solarbank"][0]["raw"].items():
        registers.update({int(start) + i: v for i, v in enumerate(values)})
    return registers


def encode32(value):
    word = value & 0xFFFFFFFF
    return word >> 16, word & 0xFFFF


def meter_registers():
    r = {}
    r[10632] = 2300                                      # primary phase 1 voltage: 230.0 V (GitHub issue 69)
    r[10644], r[10645] = encode32(-8)
    r[10675], r[10676] = encode32(-289)
    r.update({10632 + i: 0 for i in range(45) if 10632 + i not in r})
    return r


def shelly_routes():
    raw = FIXTURE["shelly"][0]["raw"]
    return {"/rpc/EM1.GetStatus?id=0": (200, raw["em1:0"]), "/rpc/EM1.GetStatus?id=1": (200, raw["em1:1"])}


@pytest.fixture
def lab():
    servers = [FakeModbus(sb_registers()), FakeModbus(meter_registers()), FakeShelly(shelly_routes())]
    yield servers
    for s in servers:
        try:
            s.stop()
        except Exception:
            pass


def config(servers, **options):
    return DirectConfig(devices=DEVICES, solarbank_host="127.0.0.1", meter_host="127.0.0.1", shelly_host="127.0.0.1",
                        solarbank_port=servers[0].server_address[1], meter_port=servers[1].server_address[1],
                        shelly_port=servers[2].server_address[1], timeout_seconds=.4, period_seconds=.1, **options)


def steady(health):
    """The loop kept its rhythm: a stray late tick from scheduling jitter on a busy machine is not a stall (the Pi runs these tests beside the live services)."""
    return health["missed"] <= max(2, (health["seq"] or 0) // 20)


def wait_for(condition, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(.05)
    return False


def test_device_unplugged_then_plugged_back_in_recovers_without_a_restart(lab):
    acquirer = DirectAcquirer(config(lab, max_age_seconds=.6)).start()
    try:
        # All three read at least once before the snapshot: waiting for the Solarbank alone left the Shelly sometimes not yet read, so `before` had no
        # "shelly" entry and the comparison at the end failed with KeyError (intermittent, worse with the whole suite loaded on the Mint box).
        assert wait_for(lambda: all(acquirer.group(name)["ok"] for name in ("solarbank", "meter", "shelly")))
        port = lab[0].server_address[1]
        before = acquirer.health()                                 # how far the other devices had got
        lab[0].stop()                                              # unplugged
        assert wait_for(lambda: not acquirer.group("solarbank")["ok"], 4)
        stale = acquirer.group("solarbank")
        assert stale["sample"] is None and ("stale" in stale["error"] or stale["error"])
        h = acquirer.health()
        assert h["solarbank"]["fail_streak"] >= 1 and h["shelly"]["fail_streak"] == 0 and h["meter"]["fail_streak"] == 0
        seq_before = acquirer.health()["solarbank"]["seq"]
        lab[0] = FakeModbus(sb_registers(), port=port)             # plugged back in
        assert wait_for(lambda: acquirer.group("solarbank")["ok"], 15)
        health = acquirer.health()["solarbank"]
        assert health["fail_streak"] == 0 and health["seq"] > seq_before
        others = acquirer.health()
        assert steady(others["shelly"]) and steady(others["meter"])                                  # the others never noticed
        assert others["shelly"]["failures"] == 0 and others["meter"]["failures"] == 0               # and never saw an error
        assert others["shelly"]["seq"] >= before["shelly"]["seq"] + 5                                # and kept reading throughout
        assert others["meter"]["seq"] >= before["meter"]["seq"] + 5
    finally:
        acquirer.stop()


@pytest.mark.parametrize("fault", ["reset", "close_after_request", "silent", "wrong_txn", "short"])
def test_every_fault_kind_is_survived_and_cleared(lab, fault):
    acquirer = DirectAcquirer(config(lab, max_age_seconds=.6)).start()
    try:
        assert wait_for(lambda: acquirer.group("meter")["ok"])
        lab[1].mode = fault
        assert wait_for(lambda: not acquirer.group("meter")["ok"], 4)
        assert acquirer.group("meter")["sample"] is None            # unknown, never the old value
        lab[1].mode = "normal"
        assert wait_for(lambda: acquirer.group("meter")["ok"], 15)
        assert acquirer.group("meter")["sample"]["sub"] == -289
        assert all(t.is_alive() for t in acquirer._loops)           # no loop died
    finally:
        acquirer.stop()


def test_a_flapping_link_never_kills_a_loop_or_shows_stale_data_as_fresh(lab):
    acquirer = DirectAcquirer(config(lab, max_age_seconds=.5)).start()
    try:
        assert wait_for(lambda: acquirer.group("solarbank")["ok"])
        modes = ["reset", "normal", "close_after_request", "normal", "silent", "normal"]
        for mode in modes * 2:
            lab[0].mode = mode
            time.sleep(.3)
            group = acquirer.group("solarbank")
            if group["ok"]:
                assert group["sample"]["soc"] == 71                  # whenever it claims fresh, the data is right
        lab[0].mode = "normal"
        assert wait_for(lambda: acquirer.group("solarbank")["ok"], 15)
        assert all(t.is_alive() for t in acquirer._loops)
    finally:
        acquirer.stop()


def test_a_stalled_logger_costs_the_loops_nothing(lab):
    fanout = Fanout(maxsize=5)                                      # nobody ever drains it
    acquirer = DirectAcquirer(config(lab), fanout=fanout).start()
    try:
        time.sleep(2.0)
        health = acquirer.health()
        for name in DEVICES:
            assert health[name]["seq"] >= 12 and steady(health[name]) and health[name]["fail_streak"] == 0
        assert fanout.q.qsize() == 5 and fanout.dropped > 10          # bounded, oldest dropped, counted
    finally:
        acquirer.stop()


def test_busy_cpu_does_not_stop_the_loops(lab):
    # Lowest priority and only two burners: on the Pi this suite runs beside the live services and must not starve them.
    burners = [subprocess.Popen([sys.executable, "-c", "import os; os.nice(19)\nwhile True: pass"]) for _ in range(2)]
    acquirer = DirectAcquirer(config(lab)).start()
    try:
        time.sleep(3.0)
        health = acquirer.health()
        for name in DEVICES:
            assert health[name]["seq"] >= 15 and health[name]["fail_streak"] == 0
            assert health[name]["age"] < 1.0
    finally:
        acquirer.stop()
        for b in burners:
            b.kill()


@pytest.mark.parametrize("record_seconds", [1, 60])
def test_a_locked_database_never_delays_the_tick_or_the_live_file(tmp_path, lab, record_seconds):
    live = tmp_path / "live.json"
    store = Store(tmp_path / "c.sqlite3", live_path=live); store.initialise()
    acquirer = DirectAcquirer(config(lab)).start()
    worker = Collector(store, CollectorConfig(max_age_seconds=5, record_seconds=record_seconds, flush_interval_seconds=.3,
                                              background_flush=True), Clock(), acquirer=acquirer)
    holder = sqlite3.connect(tmp_path / "c.sqlite3", timeout=1)
    try:
        time.sleep(.4)
        worker.once()                                               # normal start
        holder.execute("BEGIN IMMEDIATE")                           # another process now holds the database write lock
        durations, stamps = [], []
        began = time.monotonic()
        while time.monotonic() - began < 3.0:
            t = time.monotonic(); worker.once(); durations.append(time.monotonic() - t)
            stamps.append(json.loads(live.read_text())["written_at"])
            time.sleep(.2)
        assert max(durations) < .3                                  # the tick never waited for the database
        assert stamps[-1] > stamps[0] + 2                           # the live file kept moving the whole time
        health = acquirer.health()
        assert all(steady(health[d]) for d in DEVICES)
    finally:
        holder.rollback(); holder.close()
        acquirer.stop()
    # After the lock is released the backlog reaches the database exactly once.
    assert wait_for(lambda: worker._flusher is None or not worker._flusher.is_alive(), 10)
    stop = threading.Event(); stop.set()
    worker.flush(final=True)
    with store.connect() as db:
        snapshots = db.execute("SELECT count(*) FROM collector_snapshots").fetchone()[0]
        distinct = db.execute("SELECT count(DISTINCT id) FROM collector_snapshots").fetchone()[0]
    assert snapshots == distinct and snapshots >= (1 if record_seconds == 60 else 10)


def test_graceful_stop_waits_for_the_background_commit_and_loses_nothing(tmp_path, lab):
    store = Store(tmp_path / "c.sqlite3"); store.initialise()
    acquirer = DirectAcquirer(config(lab)).start()
    worker = Collector(store, CollectorConfig(record_seconds=1, flush_interval_seconds=5, background_flush=True), Clock(), acquirer=acquirer)
    stop = threading.Event()
    threading.Timer(1.5, stop.set).start()
    try:
        worker.run(stop, count=None)
    finally:
        acquirer.stop()
    with store.connect() as db:
        rows = db.execute("SELECT count(*) FROM collector_snapshots").fetchone()[0]
    assert rows >= 2 and not worker.pending


def test_soak_with_rolling_faults_ends_healthy(tmp_path, lab):
    store = Store(tmp_path / "c.sqlite3", live_path=tmp_path / "live.json"); store.initialise()
    acquirer = DirectAcquirer(config(lab, max_age_seconds=1.0), fanout=Fanout(50)).start()
    worker = Collector(store, CollectorConfig(record_seconds=60, flush_interval_seconds=.5, background_flush=True), Clock(), acquirer=acquirer)
    started = time.monotonic()
    schedule = [(0, "silent"), (1, "normal"), (2, "reset"), (3, "normal"), (4, "close_after_request"), (5, "normal")]
    try:
        for second in range(8):
            for at, mode in schedule:
                if at == second:
                    lab[second % 2].mode = mode
            worker.once()
            time.sleep(max(0, started + second + 1 - time.monotonic()))
        for s in lab[:2]:
            s.mode = "normal"
        assert wait_for(lambda: all(acquirer.group(n)["ok"] for n in DEVICES), 20)
        worker.once()
        health = acquirer.health()
        assert all(health[d]["fail_streak"] == 0 for d in DEVICES)
        assert all(t.is_alive() for t in acquirer._loops)
        # A healthy end state is something to wait for, not to sample at one instant: under load a reading can momentarily be older than the limit.
        assert wait_for(lambda: worker.once()["status"] == "ok", 15)
    finally:
        acquirer.stop()
