"""4a.3a: live state, device loops, fan-out and control tick, with fake devices."""
import threading
import time

import pytest

from solarbank_optimiser.devices import DeviceError
from solarbank_optimiser.shelly_channels import CAR
from solarbank_optimiser.live import DeviceLoop, Fanout, LiveState, Reading, control_tick, sd_notify


def run_loop(read_fn, period=0.02, duration=0.3, fanout=None, backoff=(0, 0.02), settle=0.0):
    live, stop = LiveState(), threading.Event()
    loop = DeviceLoop("dev", read_fn, period, live, stop, fanout=fanout, backoff=backoff, settle=settle)
    loop.start()
    time.sleep(duration)
    stop.set()
    loop.join(2)
    assert not loop.is_alive()
    return live


def test_steady_polling_publishes_increasing_sequence_and_latency():
    count = {"n": 0}

    def read():
        count["n"] += 1
        return {"n": count["n"]}
    live = run_loop(read)
    reading, health = live.snapshot()["dev"]
    assert reading.seq >= 8 and reading.value["n"] == reading.seq and health["fail_streak"] == 0
    assert 0 <= reading.latency < 0.25 and health["missed"] <= max(2, reading.seq // 20)


def test_a_failed_read_never_refreshes_or_replaces_the_last_good_value():
    state = {"ok": True}

    def read():
        if state["ok"]:
            return {"v": 1}
        raise DeviceError("timeout", "silent")
    live, stop = LiveState(), threading.Event()
    loop = DeviceLoop("dev", read, 0.02, live, stop, backoff=(0, 0.02))
    loop.start(); time.sleep(0.1)
    good = live.snapshot()["dev"][0]
    state["ok"] = False
    time.sleep(0.15)
    reading, health = live.snapshot()["dev"]
    stop.set(); loop.join(2)
    assert reading == good and reading.mono == good.mono          # same value, same arrival stamp
    assert health["fail_streak"] >= 3 and health["last_error"] == "timeout"


def test_recovery_clears_the_streak_and_sequence_continues():
    state = {"ok": False}

    def read():
        if not state["ok"]:
            raise DeviceError("closed", "down")
        return {"v": 1}
    live, stop = LiveState(), threading.Event()
    loop = DeviceLoop("dev", read, 0.02, live, stop, backoff=(0, 0.02)); loop.start()
    time.sleep(0.1); state["ok"] = True; time.sleep(0.15)
    reading, health = live.snapshot()["dev"]
    stop.set(); loop.join(2)
    assert reading is not None and health["fail_streak"] == 0 and health["failures"] >= 2


def test_unexpected_reader_bug_is_contained_and_counted():
    def read():
        raise KeyError("bug")
    live = run_loop(read)
    reading, health = live.snapshot()["dev"]
    assert reading is None and health["failures"] >= 3 and health["last_error"] == "error"


def test_slow_reads_count_missed_deadlines_without_bursting():
    stamps = []

    def read():
        stamps.append(time.monotonic()); time.sleep(0.06)
        return {}
    live = run_loop(read, period=0.02, duration=0.4)
    _, health = live.snapshot()["dev"]
    assert health["missed"] >= 3
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.055               # one read at a time, no catch-up bursts


def test_failure_backoff_limits_attempts_against_a_dead_device():
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        raise DeviceError("connect", "down")
    live, stop = LiveState(), threading.Event()
    DeviceLoop("dev", read, 0.01, live, stop, backoff=(0, 0.1, 0.1)).start()
    time.sleep(0.5); stop.set()
    assert calls["n"] <= 8                   # without backoff a 10 ms tick would make about 50


def test_stalled_consumer_never_delays_the_loop_and_oldest_items_are_dropped():
    fanout = Fanout(maxsize=5)               # nothing ever drains it
    live = run_loop(lambda: {"v": 1}, fanout=fanout, duration=0.3)
    reading, health = live.snapshot()["dev"]
    assert reading.seq >= 8 and health["missed"] <= 2
    assert fanout.q.qsize() == 5 and fanout.dropped == reading.seq - 5
    newest = [fanout.q.get_nowait()[1].seq for _ in range(5)]
    assert newest == sorted(newest) and newest[-1] == reading.seq


def test_loop_stops_promptly_even_during_a_long_backoff():
    live, stop = LiveState(), threading.Event()
    loop = DeviceLoop("dev", lambda: (_ for _ in ()).throw(DeviceError("connect", "x")), 1.0, live, stop, backoff=(0, 30))
    loop.start(); time.sleep(0.1)
    started = time.monotonic(); stop.set(); loop.join(2)
    assert not loop.is_alive() and time.monotonic() - started < 0.5


def test_control_tick_treats_old_and_missing_data_as_unknown():
    live = LiveState()
    live.publish("solarbank", Reading({"soc": 70}, mono=100.0, utc=0, seq=1, latency=0.02))
    live.publish("shelly", Reading({CAR: 3.0}, mono=90.0, utc=0, seq=1, latency=0.02))
    inputs, unknown = control_tick(live, {"solarbank": 5, "shelly": 5, "meter": 5}, now=102.0)
    assert inputs["solarbank"]["value"] == {"soc": 70} and inputs["solarbank"]["age"] == pytest.approx(2.0)
    assert sorted(unknown) == ["meter", "shelly"]        # stale (12 s) and never read


def test_snapshot_is_a_copy_so_consumers_cannot_corrupt_state():
    live = LiveState()
    live.publish("d", Reading({"v": 1}, 1.0, 0, 1, 0.01))
    live.snapshot()["d"][1]["fail_streak"] = 99
    assert live.snapshot()["d"][1]["fail_streak"] == 0


def test_sd_notify_is_a_safe_no_op_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("WATCHDOG=1") is False
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/socket")
    assert sd_notify("WATCHDOG=1") is False


def test_modules_contain_no_database_or_disk_access():
    import inspect
    from solarbank_optimiser import live
    source = inspect.getsource(live)
    for forbidden in ("sqlite3", "open(", "write_text", "Store"):
        assert forbidden not in source


def test_one_isolated_failure_costs_one_sample_not_two():
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        if calls["n"] == 5:
            raise DeviceError("timeout", "one slow reply")
        return {"n": calls["n"]}
    live, stop = LiveState(), threading.Event()
    loop = DeviceLoop("dev", read, 0.05, live, stop)           # the production backoff, not a test one
    loop.start(); time.sleep(0.62); stop.set(); loop.join(2)
    reading, health = live.snapshot()["dev"]
    assert health["failures"] == 1 and health["fail_streak"] == 0
    assert calls["n"] >= 11                                     # about 12 ticks fit; the old one-second wait would have allowed only ~6
    assert health["missed"] <= 2                                # a stray late tick from scheduling jitter is not what this test is about


def test_repeated_failures_still_back_off():
    calls = {"n": 0}

    def read():
        calls["n"] += 1
        raise DeviceError("connect", "down")
    live, stop = LiveState(), threading.Event()
    DeviceLoop("dev", read, 0.01, live, stop).start()
    time.sleep(1.6); stop.set()
    assert calls["n"] <= 12                                     # 10 ms ticks would make ~160 without backoff
