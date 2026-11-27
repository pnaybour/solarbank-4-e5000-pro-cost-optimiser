"""T2, the sign test runner (GitHub issue 75): the specification's sequence, the pre-checks that send nothing, the stops, and the hand-back that always happens."""
import json

import pytest

from solarbank_optimiser import commission_tests as t



class Battery:
    """A pretend Solarbank and the collector's live readings around it. `invert` swaps the sign (the fault T2 exists to catch)."""
    def __init__(self, mode=6, invert=False, soc=0.5, house_kw=0.8, car_kw=0.0, garage_a=8.0):
        self.mode, self.invert, self.writes, self.setpoint = mode, invert, [], 0
        self.readings = {"soc": soc, "household_power": house_kw, "ev_power": car_kw, "garage_current": garage_a}
        self.now, self.after = 0.0, {}                          # after: {seconds into the run: changes to the readings}

    def settings(self, _client):
        return {"decoded": {"operating_mode": {"code": self.mode}, "ems_mode_mask": {"raw": 47}}}

    def write_mode(self, value):
        self.writes.append(("mode", value)); self.mode = value

    def write_setpoint(self, value):
        self.writes.append(("setpoint", value)); self.setpoint = value

    def sleep(self, seconds):
        self.now += seconds
        for at in [a for a in self.after if a <= self.now]:
            self.readings.update(self.after.pop(at))

    def clock(self):
        return self.now

    def live(self):
        watts = -self.setpoint if self.invert else self.setpoint     # positive set-point: power out of the Solarbank, as the Shelly signs it
        return {**self.readings, "ac_power": watts / 1000.0}


def run(battery, running=False):
    return t.setpoint_both_signs(battery.settings, object(), battery, battery.sleep, live=battery.live,
                                 controller_may_write=lambda: "the controller is running in 'live' mode" if running else None,
                                 clock=battery.clock, say=lambda _text: None)


def test_the_sequence_is_the_specifications():
    seq = t.SIGN_SEQUENCE
    assert len(seq) >= 20 and all(abs(w) <= t.SIGN_MAX_W for w in seq) and 0 not in seq
    assert {-500, 300, -600, -300, -200} <= set(seq)
    assert any(a == -600 and b == -300 for a, b in zip(seq, seq[1:]))                 # -600 then -300
    assert any(a > 0 and b == -200 for a, b in zip(seq, seq[1:]))                     # -200 after a positive value
    assert sum(1 for a, b in zip(seq, seq[1:]) if (a > 0) != (b > 0)) >= 10          # alternating
    assert any(a == b or (a < 0) == (b < 0) for a, b in zip(seq, seq[1:]))            # and repeated
    assert t.SIGN_HOLD_S == 20.0


def test_a_well_behaved_battery_passes_and_is_handed_back_to_self_consumption():
    battery = Battery(mode=6)
    note = run(battery)
    assert battery.writes[:2] == [("setpoint", 0), ("mode", 3)]
    assert [v for k, v in battery.writes[2:-2]] == list(t.SIGN_SEQUENCE)
    assert battery.writes[-2:] == [("setpoint", 0), ("mode", 0)]
    assert note.startswith(f"{len(t.SIGN_SEQUENCE)} of {len(t.SIGN_SEQUENCE)} steps right") and "mode 0" in note
    assert battery.now >= len(t.SIGN_SEQUENCE) * t.SIGN_HOLD_S


@pytest.mark.parametrize("start, back", [(0, 0), (6, 0), (3, 0), (7, 0), (4, 0)])
def test_the_hand_back_is_always_self_consumption_whatever_mode_the_battery_was_in(start, back):
    battery = Battery(mode=start)
    run(battery)
    assert battery.writes[-1] == ("mode", back)


def test_a_wrong_sign_stops_at_once_and_still_hands_back():
    battery = Battery(invert=True)
    with pytest.raises(t.TestFailed, match=r"step 1: set -500 W but the Shelly showed \+500 W"):
        run(battery)
    assert battery.writes == [("setpoint", 0), ("mode", 3), ("setpoint", -500), ("setpoint", 0), ("mode", 0)]


def test_a_response_too_small_to_tell_the_direction_fails():
    battery = Battery()
    battery.live = lambda: {**battery.readings, "ac_power": battery.setpoint / 10000.0}   # a tenth of the set-point: 50 W for -500 W
    with pytest.raises(t.TestFailed, match="at least 100 W"):
        run(battery)
    assert battery.writes[-2:] == [("setpoint", 0), ("mode", 0)]


@pytest.mark.parametrize("change, reason", [({"ev_power": 7.0}, "car is charging"), ({"garage_current": 41.0}, "garage current"),
                                            ({"soc": None}, "soc reading is missing")])
def test_the_car_the_garage_feed_or_a_lost_reading_stops_the_run_and_hands_back(change, reason):
    battery = Battery()
    battery.after = {65.0: {k: v for k, v in change.items() if v is not None}}
    if change.get("soc", 0) is None:
        battery.after = {65.0: {}}
        original = battery.live
        battery.live = lambda: {k: v for k, v in original().items() if not (k == "soc" and battery.now > 65.0)}
    with pytest.raises(t.TestFailed, match=reason):
        run(battery)
    assert battery.writes[-2:] == [("setpoint", 0), ("mode", 0)] and len(battery.writes) < 2 + len(t.SIGN_SEQUENCE) + 2


@pytest.mark.parametrize("battery, running, reason", [
    (Battery(), True, "'live' mode"),
    (Battery(car_kw=7.0), False, "car is charging"),
    (Battery(soc=0.95), False, "needs 20% to 90%"),
    (Battery(soc=0.12), False, "needs 20% to 90%"),
    (Battery(house_kw=0.2), False, "switch on a heater"),
    (Battery(garage_a=42.0), False, "garage current"),
])
def test_nothing_is_written_when_a_pre_check_fails(battery, running, reason):
    with pytest.raises(t.TestFailed, match=reason) as failure:
        run(battery, running=running)
    assert battery.writes == [] and "nothing was written" in str(failure.value)


def test_missing_readings_before_the_start_write_nothing():
    battery = Battery()
    battery.live = lambda: {}
    with pytest.raises(t.TestFailed, match="missing"):
        run(battery)
    assert battery.writes == []


def test_ctrl_c_hands_back_and_says_how_far_it_got():
    battery = Battery()
    original = battery.sleep
    def sleep(seconds):
        original(seconds)
        if battery.now > 50:
            raise KeyboardInterrupt
    battery.sleep = sleep
    with pytest.raises(t.TestFailed, match=r"stopped by you after 2 step"):
        t.setpoint_both_signs(battery.settings, object(), battery, sleep, live=battery.live, controller_may_write=lambda: None, clock=battery.clock, say=lambda _t: None)
    assert ("setpoint", 0) in battery.writes[-3:] and battery.mode == 0


def test_a_failed_hand_back_says_plainly_what_to_do():
    battery = Battery(invert=True)
    def refuse(_value):
        raise OSError("no reply")
    real = battery.write_setpoint
    battery.write_setpoint = lambda v: real(v) if len(battery.writes) < 3 else refuse(v)
    with pytest.raises(t.TestFailed, match="HAND-BACK FAILED.*Anker app"):
        run(battery)


def test_a_battery_that_will_not_take_mode_3_is_handed_back_without_a_set_point():
    battery = Battery()
    battery.write_mode = lambda v: battery.writes.append(("mode", v))                       # accepted, but the mode never changes
    with pytest.raises(t.TestFailed, match="read back 6"):
        run(battery)
    assert ("setpoint", -500) not in battery.writes and battery.writes[-2:] == [("setpoint", 0), ("mode", 0)]


def test_the_live_file_gives_only_fresh_good_readings(tmp_path):
    path = tmp_path / "live.json"
    path.write_text(json.dumps({"observations": [
        {"metric": "ac_power", "value": -0.48, "retrieved_at": 1000.0, "quality": "ok"},
        {"metric": "ev_power", "value": 0.0, "retrieved_at": 990.0, "quality": "ok"},          # 10 s old
        {"metric": "soc", "value": 0.5, "retrieved_at": 1000.0, "quality": "invalid"},
        {"metric": "garage_current", "value": None, "retrieved_at": 1000.0, "quality": "ok"}]}))
    assert t.live_readings(path, now=1001.0) == {"ac_power": -0.48}
    assert t.live_readings(tmp_path / "absent.json", now=1001.0) == {}


def test_the_runner_records_t2_under_the_gate_entry():
    from solarbank_optimiser import live_gate
    assert "setpoint-both-signs" in t.TESTS and "setpoint-both-signs" in t.NEEDS_DEVICE and "setpoint-both-signs" in live_gate.REQUIRED_TESTS


@pytest.mark.parametrize("active, state, age, verdict", [
    ("inactive", None, 0, None),                                                                      # stopped: fine
    ("active", {"mode": "logged", "sends_commands": False}, 2, None),                                  # logged never writes: fine, and it keeps the meter read
    ("active", {"mode": "shadow", "sends_commands": False}, 2, None),
    ("active", {"mode": "live", "sends_commands": True}, 2, "'live' mode"),
    ("active", {"mode": "logged", "sends_commands": True}, 2, "may write"),
    ("active", {"mode": "logged", "sends_commands": False}, 120, "120 s old"),
    ("active", "unreadable", 0, "cannot be read"),
])
def test_t2_runs_beside_a_logged_controller_but_never_beside_one_that_may_write(tmp_path, monkeypatch, active, state, age, verdict):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=active + "\n"))
    path = tmp_path / "control-loop.json"
    if state == "unreadable":
        path.write_text("{")
    elif state is not None:
        path.write_text(json.dumps({"execution": state, "at": 1000.0}))
    reason = t._controller_may_write(path, now=1000.0 + age)
    assert (reason is None) if verdict is None else (verdict in reason)


class Connection:
    """A pretend Modbus client that knows whether it is open: opened by use, closed by close()."""
    def __init__(self, pool, name):
        self.pool, self.name = pool, name

    def use(self):
        self.pool.add(self.name)
        assert len(self.pool) <= 1, f"the test held two connections of its own at once: {sorted(self.pool)}"

    def close(self):
        self.pool.discard(self.name)


def test_every_runner_test_holds_one_connection_of_its_own_at_a_time():
    """The Solarbank accepts three Modbus clients; beside the collector and a running controller, a test has room for one."""
    pool = set()
    reader, writer_link = Connection(pool, "reader"), Connection(pool, "writer")
    battery = Battery(mode=0)
    def read_settings(client):
        reader.use()
        return battery.settings(client)
    class Writer:
        def write_mode(self, v):
            writer_link.use(); battery.write_mode(v)
        def write_setpoint(self, v):
            writer_link.use(); battery.write_setpoint(v)
        def close(self):
            writer_link.close()
    for name in ("read-registers", "mode-write", "default-mode", "setpoint-zero-write", "release"):
        battery.mode = 0
        t.run(name, read_settings, reader, Writer(), sleep=lambda _s: None)
    read, writer = t.one_connection_at_a_time(read_settings, reader, Writer())
    battery.mode = 0
    t.setpoint_both_signs(read, reader, writer, battery.sleep, live=battery.live, controller_may_write=lambda: None, clock=battery.clock, say=lambda _t: None)
    assert battery.writes[-1] == ("mode", 0)


def test_the_stand_alone_check_reads_each_device_directly_in_the_live_files_units():
    live = t.direct_readings(lambda: {"soc": 42, "load": 850}, lambda: {"garage_current_a": 9.5}, lambda: {"solarbank_ac": -480.0, "car": -3.0})
    assert live() == {"soc": 0.42, "household_power": 0.85, "garage_current": 9.5, "ac_power": -0.48, "ev_power": 0.0}


def test_a_device_that_does_not_answer_leaves_its_readings_out_so_t2_stops():
    def silent():
        raise OSError("no reply")
    live = t.direct_readings(lambda: {"soc": 42, "load": 850}, silent, lambda: {"solarbank_ac": -480.0, "car": 0.0})
    assert "garage_current" not in live() and "soc" in live()
    battery = Battery()
    battery.live = lambda: {k: v for k, v in Battery().live().items() if k != "garage_current"}
    with pytest.raises(t.TestFailed, match="garage_current reading is missing"):
        run(battery)
    assert battery.writes == []


@pytest.mark.parametrize("states, still", [({}, []), ({"collector": "active"}, ["collector"]), ({"web": "activating"}, ["web"]),
                                           ({"control": "failed"}, [])])
def test_the_stand_alone_check_needs_every_optimiser_service_stopped(states, still):
    assert t.services_running(lambda unit: states.get(unit.removeprefix("solarbank-optimiser-"), "inactive")) == still


def test_the_stand_alone_script_refuses_while_a_service_runs(monkeypatch, capsys):
    import importlib.util, os
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("sign_check", Path(__file__).resolve().parents[1] / "scripts/battery_sign_check.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(t, "services_running", lambda: ["collector"])
    assert module.main(["--confirm"]) == 2 and "still running: collector" in capsys.readouterr().out
