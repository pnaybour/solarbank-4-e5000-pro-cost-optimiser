"""4a.5 comparison: the report may only be satisfied by evidence that is recent, new, paired and complete."""
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from solarbank_optimiser import comparison as c

LONDON = ZoneInfo("Europe/London")
DIRECT = ["solarbank", "shelly"]


def epoch(text):
    return datetime.fromisoformat(text).replace(tzinfo=LONDON).timestamp()


class Sim:
    """Generates one second of both sources at a time, the way the live file and the monitor's page would look."""

    def __init__(self, start, **fixed):
        self.start, self.fixed, self.acc, self.rows = start, fixed, c.Accumulator(), []

    def second(self, i, *, devices=DIRECT, frozen_times=False, frozen_seq=False, frozen_values=False, monitor_frozen=False,
               load=None, soc=71, failures=0, missed=0, streak=0, shelly_present=True):
        now = self.start + i
        t_read = self.start if frozen_times else now - 0.2
        wobble = 0 if frozen_values else (i % 3)
        rows = {"soc": soc / 100, "pv_power": .06, "household_power": (.38 if load is None else load) + wobble * .0,
                "grid_power": 0.0, "battery_power": .32, "ac_power": .38 + wobble * .001, "ev_power": .003 + (i % 5) * .0001}
        if not shelly_present:
            rows = {k: v for k, v in rows.items() if k not in ("ac_power", "ev_power")}
        observations = {k: {"value": v, "quality": "ok", "retrieved_at": t_read} for k, v in rows.items()}
        seq = 1 if frozen_seq else i + 1
        health = {d: {"seq": seq, "latency": .05, "failures": failures, "missed": missed, "fail_streak": streak} for d in devices}
        t_mon = self.start if monitor_frozen else now - 0.3
        monitor = {"ok": True, "sample": {"time": t_mon, "main": 10, "sub": -324},
                   "shelly": {"ok": True, "sample": {"time": t_mon, "ch1": rows.get("ac_power", 0) * 1000, "ch2": rows.get("ev_power", 0) * 1000}},
                   "solarbank": {"ok": True, "sample": {"time": t_mon, "soc": soc, "pv": 60, "load": (.38 if load is None else load) * 1000,
                                                        "grid": 0, "battery": 320}}}
        done = self.acc.add(now, observations, health, monitor, devices)
        if done:
            self.rows.append(done)

    def run(self, seconds, **options):
        for i in range(seconds):
            self.second(i, **options)
        last = self.acc.record()
        if last:
            self.rows.append(last)
        return self.rows


def write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows))


def reported(path, capsys, **options):
    c.report(path, now=options.pop("now", 2_000_000_000), **options)
    return capsys.readouterr().out


T0 = epoch("2026-10-27 12:00")


# ---- the review finding: frozen or replayed data must never produce a pass --------------------------

def test_replayed_frozen_readings_produce_no_evidence_however_long_they_are(tmp_path, capsys):
    rows = Sim(T0).run(3 * 3600, frozen_times=True, frozen_seq=True, monitor_frozen=True)     # three hours of the same stale reading
    write(tmp_path / "f.jsonl", rows)
    text = reported(tmp_path / "f.jsonl", capsys)
    assert "0.0 h of evidence" in text and "FAIL" in text and "PASS" not in text.split("Conditions still missing")[0].replace("Conditions", "")
    assert "24 hours of qualifying minutes (have 0.0 h)" in text
    assert all(not c.minute_qualifies(r) for r in rows)


def test_the_same_run_with_live_data_does_qualify(tmp_path, capsys):          # the control for the test above
    rows = Sim(T0).run(3 * 3600)
    assert sum(c.minute_qualifies(r) for r in rows) >= 179
    write(tmp_path / "l.jsonl", rows)
    text = reported(tmp_path / "l.jsonl", capsys)
    assert "PASS soc" in text and "PASS solarbank" in text and "PASS shelly" in text and "3.0 h of evidence" in text or "2.98 h" in text or "2.97 h" in text or "3.0" in text


def test_old_readings_with_a_moving_clock_are_stale_not_fresh():
    sim = Sim(T0)
    sim.run(180, frozen_times=True)                                  # timestamps never advance but the clock does
    stale = sim.rows[1]["metrics"]["household_power"]
    assert stale["n"] == 0 and stale["direct_stale"] >= 50 and stale["new_readings"] == 0


def test_replayed_readings_with_a_frozen_sequence_number_count_no_reads():
    sim = Sim(T0)
    sim.run(180, frozen_seq=True)
    assert all(r["devices"]["solarbank"]["reads"] <= 1 for r in sim.rows) and not any(c.minute_qualifies(r) for r in sim.rows)


def test_a_stale_monitor_cannot_make_a_minute_count():
    sim = Sim(T0)
    sim.run(180, monitor_frozen=True)
    assert not any(c.minute_qualifies(r) for r in sim.rows)
    assert sim.rows[1]["monitor_ok"] == 0 and sim.rows[1]["metrics"]["soc"]["monitor_stale"] > 50


def test_one_device_alone_cannot_satisfy_the_comparison(tmp_path, capsys):
    rows = Sim(T0).run(2 * 3600, devices=["solarbank"], shelly_present=False)           # the Shelly was never read
    write(tmp_path / "o.jsonl", rows)
    text = reported(tmp_path / "o.jsonl", capsys)
    assert "FAIL shelly: no qualifying data from this required device" in text
    assert "0.0 h of evidence" in text


def test_values_that_never_move_on_a_live_shelly_are_flagged_as_possibly_frozen(tmp_path, capsys):
    rows = Sim(T0).run(2 * 3600, frozen_values=True)                 # timestamps and reads advance, but the numbers never change
    write(tmp_path / "v.jsonl", rows)
    text = reported(tmp_path / "v.jsonl", capsys)
    assert "CHECK ac_power: the value changed only" in text and "may be frozen" in text


def test_duplicate_future_and_malformed_lines_are_ignored_and_counted(tmp_path, capsys):
    rows = Sim(T0).run(600)
    line = rows[2]
    noisy = rows + [dict(line)] * 1440 + [{**line, "minute": int(time.time()) + 10_000}, {**line, "minute": "soon"}, {**line, "minute": T0 + 17}, "junk"]
    write(tmp_path / "d.jsonl", noisy)
    text = reported(tmp_path / "d.jsonl", capsys, now=T0 + 3600)
    assert "1440 duplicate" in text and "malformed or future-dated" in text
    assert "10 qualifying minutes" in text or "9 qualifying minutes" in text            # ten real minutes, not a thousand copies


def test_lines_from_before_the_freshness_checks_are_not_counted(tmp_path, capsys):
    rows = Sim(T0).run(1800)
    legacy = [{k: v for k, v in r.items() if k != "v"} for r in rows]
    write(tmp_path / "g.jsonl", legacy)
    text = reported(tmp_path / "g.jsonl", capsys)
    assert "No usable evidence yet" in text and "30 " in text or "from before the freshness checks" in text


def test_a_clock_that_steps_back_never_writes_an_earlier_minute():
    sim = Sim(T0)
    sim.run(130)
    n = len(sim.rows)
    assert sim.acc.add(T0 - 500, {}, {}, None, DIRECT) is None
    assert len(sim.rows) == n


# ---- minute qualification --------------------------------------------------------------------------

def test_a_healthy_minute_qualifies_and_a_thin_one_does_not():
    rows = Sim(T0).run(180)
    assert c.minute_qualifies(rows[1])
    thin = json.loads(json.dumps(rows[1])); thin["devices"]["shelly"]["reads"] = 20
    assert not c.minute_qualifies(thin)
    thin = json.loads(json.dumps(rows[1])); thin["metrics"]["ev_power"]["n"] = 10
    assert not c.minute_qualifies(thin)
    thin = json.loads(json.dumps(rows[1])); del thin["devices"]["solarbank"]
    assert not c.minute_qualifies(thin)


def test_cheap_window_boundaries_in_london_time():
    assert c.in_cheap_window(epoch("2026-10-27 23:30")) and c.in_cheap_window(epoch("2026-10-27 05:29"))
    assert not c.in_cheap_window(epoch("2026-10-27 05:30")) and not c.in_cheap_window(epoch("2026-10-27 23:29"))
    assert c.in_cheap_window(epoch("2026-07-01 00:10"))


def test_the_cheap_window_requirement_counts_only_qualifying_minutes(tmp_path, capsys):
    day = Sim(epoch("2026-10-27 12:00")).run(2 * 3600)
    night = Sim(epoch("2026-10-28 00:00")).run(4 * 3600 + 60)
    write(tmp_path / "n.jsonl", day + night)
    text = reported(tmp_path / "n.jsonl", capsys)
    assert "cheap-window minutes 24" in text or "cheap-window minutes 25" in text          # 4 h = 240 minutes: 240 or 241
    assert "the cheap-rate window" not in text.split("Conditions still missing:")[1].split("Deferred")[0]
    short = Sim(epoch("2026-10-28 00:00")).run(2 * 3600)
    write(tmp_path / "s.jsonl", short)
    assert "the cheap-rate window (120" in reported(tmp_path / "s.jsonl", capsys) or "the cheap-rate window (119" in reported(tmp_path / "s.jsonl", capsys)


# ---- agreement ---------------------------------------------------------------------------------------

def test_agreement_and_context_in_one_record():
    sim = Sim(T0)
    rows = sim.run(120)
    r = rows[0]
    assert r["v"] == c.RECORD_VERSION and r["samples"] == 60 and r["cheap_window"] is False and r["ev_active"] is False
    m = r["metrics"]["household_power"]
    assert m["n"] == 60 and m["exact"] == 60 and m["new_readings"] == 60 and "grid_ct_power" not in r["metrics"]
    d = r["devices"]["solarbank"]
    assert d["reads"] >= 59 and d["failures"] == 0 and d["lat_p50_ms"] == 50.0


def test_charging_minutes_are_flagged():
    sim = Sim(T0)
    original = sim.second

    def second(i, **kw):
        original(i, **kw)
    # raise EV power above the 10 W threshold for the whole run
    sim.acc.add(T0, {"ev_power": {"value": 2.0, "quality": "ok", "retrieved_at": T0 - .1}}, {"shelly": {"seq": 1, "latency": .05, "failures": 0, "missed": 0, "fail_streak": 0}},
                {"ok": True, "shelly": {"ok": True, "sample": {"time": T0 - .2, "ch1": 0, "ch2": 2000.0}}}, ["shelly"])
    assert sim.acc.record()["ev_active"] is True


def stepped(path, wrong_every, error_steps):
    sim = Sim(T0)
    original = sim.acc.add

    def add(now, observations, health, monitor, devices):
        i = int(now - T0)
        if wrong_every and i % wrong_every == 0:
            monitor = json.loads(json.dumps(monitor)); monitor["solarbank"]["sample"]["soc"] += error_steps
        return original(now, observations, health, monitor, devices)
    sim.acc.add = add
    write(path, sim.run(1800))


def test_a_one_step_soc_transition_is_tolerated_but_a_real_gap_is_not(tmp_path, capsys):
    stepped(tmp_path / "a.jsonl", 40, 1)
    assert "PASS soc" in reported(tmp_path / "a.jsonl", capsys)
    stepped(tmp_path / "b.jsonl", 40, 5)
    assert "CHECK soc" in reported(tmp_path / "b.jsonl", capsys)
    stepped(tmp_path / "c.jsonl", 5, 1)
    assert "CHECK soc" in reported(tmp_path / "c.jsonl", capsys)


def test_timing_skew_alone_is_not_a_disagreement_but_an_offset_is(tmp_path, capsys):
    def run(offset_kw):
        sim = Sim(T0)
        original = sim.acc.add

        def add(now, observations, health, monitor, devices):
            i = int(now - T0)
            level = lambda s: 300 + 200 * ((s // 10) % 2)
            observations = {k: dict(v) for k, v in observations.items()}
            observations["household_power"]["value"] = level(i) / 1000 + offset_kw
            monitor = json.loads(json.dumps(monitor)); monitor["solarbank"]["sample"]["load"] = level(max(0, i - 1))     # the monitor sees each step a second later
            return original(now, observations, health, monitor, devices)
        sim.acc.add = add
        return sim.run(1800)
    write(tmp_path / "a.jsonl", run(0.0))
    line = [l for l in reported(tmp_path / "a.jsonl", capsys).splitlines() if "household_power" in l and "within timing" in l][0]
    assert line.startswith("PASS") and "100.00% match within timing" in line and "raw:" in line
    write(tmp_path / "b.jsonl", run(0.05))                           # 50 W high all the time
    line = [l for l in reported(tmp_path / "b.jsonl", capsys).splitlines() if "household_power" in l and "within timing" in l][0]
    assert line.startswith("CHECK")


# ---- devices ---------------------------------------------------------------------------------------

def test_device_failures_are_judged_by_rate_and_run_length(tmp_path, capsys):
    rows = Sim(T0).run(1800, failures=3, missed=6, streak=1)
    write(tmp_path / "d.jsonl", rows)
    assert "PASS solarbank" in reported(tmp_path / "d.jsonl", capsys)
    rows[0]["devices"]["solarbank"]["max_fail_streak"] = 9
    write(tmp_path / "e.jsonl", rows)
    assert "CHECK solarbank" in reported(tmp_path / "e.jsonl", capsys)


def test_a_collector_restart_mid_run_never_produces_negative_counts():
    sim = Sim(T0)
    acc, records = sim.acc, []
    for i, seq in enumerate(list(range(1, 31)) + list(range(1, 31))):                   # counters restart after 30 reads
        failures = 2 if 20 <= i < 30 else (0 if i < 20 else 1)
        done = acc.add(T0 + i, {"soc": {"value": .5, "quality": "ok", "retrieved_at": T0 + i - .1}},
                       {"solarbank": {"seq": seq, "latency": .05, "failures": failures, "missed": failures * 2, "fail_streak": 0}}, None, ["solarbank"])
        if done: records.append(done)
    records.append(acc.record())
    totals = {"reads": 0, "failures": 0, "missed": 0}
    for r in records:
        for k in totals:
            assert r["devices"]["solarbank"][k] >= 0
            totals[k] += r["devices"]["solarbank"][k]
    assert totals["reads"] == 60 and totals["failures"] == 3 and totals["missed"] == 6


def test_old_lines_with_a_negative_delta_are_counted_as_zero(tmp_path, capsys):
    rows = Sim(T0).run(1800)
    rows[3]["devices"]["solarbank"]["missed"] = -1
    write(tmp_path / "o.jsonl", rows)
    assert "-1 missed" not in reported(tmp_path / "o.jsonl", capsys)


# ---- the car-charge deferral, running and safety ------------------------------------------------------

def test_a_car_charge_is_deferred_until_required(tmp_path, capsys):
    write(tmp_path / "c.jsonl", Sim(T0).run(1800))
    text = reported(tmp_path / "c.jsonl", capsys)
    assert "Deferred by the owner (no electric car yet)" in text and "real car charge" not in text.split("Conditions still missing:")[1].split("Deferred")[0]
    assert "a real car charge" in reported(tmp_path / "c.jsonl", capsys, require_ev=True).split("Conditions still missing:")[1]


def test_run_writes_versioned_lines_and_keeps_the_partial_minute(tmp_path):
    base = epoch("2026-10-27 12:00:50")
    live = tmp_path / "live.json"
    rows = [dict(metric=k, value=v, quality="ok", retrieved_at=base) for k, v in
            dict(soc=.71, pv_power=.06, household_power=.38, grid_power=0.0, battery_power=.32, ac_power=.38, ev_power=.003).items()]
    live.write_text(json.dumps({"observations": rows, "settings": {"collector_health": {"direct_devices": DIRECT, "devices": {
        d: {"seq": 1, "latency": .05, "failures": 0, "missed": 0, "fail_streak": 0} for d in DIRECT}}}}))
    fake = {"t": base}

    def clock():
        fake["t"] += .5
        return fake["t"]
    c.fetch_monitor = lambda url, timeout=3: {"ok": True, "sample": {"time": fake["t"], "main": 1, "sub": 2},
                                              "shelly": {"ok": True, "sample": {"time": fake["t"], "ch1": 380.0, "ch2": 3.0}},
                                              "solarbank": {"ok": True, "sample": {"time": fake["t"], "soc": 71, "pv": 60, "load": 380, "grid": 0, "battery": 320}}}
    stop = threading.Event()
    threading.Timer(.5, stop.set).start()
    c.run(str(live), "http://unused", tmp_path / "out" / "compare.jsonl", interval=0.0, stop=stop, clock=clock)
    lines = c.load(tmp_path / "out" / "compare.jsonl")
    assert lines and all(r["v"] == c.RECORD_VERSION for r in lines)


def test_comparison_module_cannot_write_to_equipment():
    import inspect
    source = inspect.getsource(c).lower()
    for word in ("write_register", "sendall", "modbus", ".post(", "method=\"put\""):
        assert word not in source


def solar_ramp(path, transform=lambda kw: kw):
    """A morning: solar power climbs 10 W every second; the monitor reports each value one second after the direct reader."""
    sim = Sim(T0)
    original = sim.acc.add

    def add(now, observations, health, monitor, devices):
        i = int(now - T0)
        level = lambda s: 100 + 10 * (s % 300)                             # watts
        observations = {k: dict(v) for k, v in observations.items()}
        observations["pv_power"]["value"] = transform(level(i) / 1000)
        monitor = json.loads(json.dumps(monitor)); monitor["solarbank"]["sample"]["pv"] = level(max(0, i - 1))
        return original(now, observations, health, monitor, devices)
    sim.acc.add = add
    write(path, sim.run(1800))


def pv_line(path, capsys):
    return [l for l in reported(path, capsys).splitlines() if "pv_power" in l and not l.strip().startswith("coverage")][0]


def test_solar_power_moving_second_by_second_is_judged_by_timing_not_by_exact_steps(tmp_path, capsys):
    solar_ramp(tmp_path / "a.jsonl")
    line = pv_line(tmp_path / "a.jsonl", capsys)
    assert line.startswith("PASS") and "within timing" in line and "raw:" in line and "at most one step" not in line


@pytest.mark.parametrize("name,transform", [("a constant offset", lambda kw: kw + 0.05), ("a wrong scale", lambda kw: kw * 0.5),
                                            ("a sign error", lambda kw: -kw), ("a unit error", lambda kw: kw * 1000)])
def test_real_solar_errors_still_fail(tmp_path, capsys, name, transform):
    solar_ramp(tmp_path / "b.jsonl", transform)
    assert pv_line(tmp_path / "b.jsonl", capsys).startswith("CHECK"), name


def test_battery_percentage_remains_a_stepped_quantity(tmp_path, capsys):
    stepped(tmp_path / "s.jsonl", 40, 1)
    assert "at most one step apart" in reported(tmp_path / "s.jsonl", capsys)
