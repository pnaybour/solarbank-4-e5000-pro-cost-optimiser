"""The Shelly's two channels are named by what they measure, not by a number.

The Shelly counts its channels from 0 (em1:0, em1:1); the original monitor and its website counted from 1 (ch1, ch2). So "channel 1" is the
car in the Shelly's numbering and the Solarbank line in the monitor's. These tests keep the two circuits from ever being swapped silently.
"""
import json
from pathlib import Path
import re

import pytest

from solarbank_optimiser.collector import decode_snapshot
from solarbank_optimiser.devices import shelly
from solarbank_optimiser.devices.errors import DeviceError
from solarbank_optimiser.shelly_channels import CAR, CHANNELS, SOLARBANK_AC, from_monitor, normalise_document
from test_control_loop import fresh, plan, settled

# What the live Shelly app showed at 02:06 on 2 November 2026: the battery line at -46 W (the Solarbank's own standby draw) and the car charger at 2.6 W.
STATUS = {"em1:0": {"act_power": -46.0, "voltage": 236.9, "current": 0.2},
          "em1:1": {"act_power": 2.6, "voltage": 236.9, "current": 0.02}}


def test_the_channels_are_read_by_the_shellys_own_numbers():
    assert CHANNELS == (SOLARBANK_AC, CAR)                        # the position is the Shelly's number: em1:0, then em1:1
    sample = shelly.parse_status(STATUS)
    assert sample[SOLARBANK_AC] == -46.0 and sample[CAR] == 2.6
    assert sample[SOLARBANK_AC + "_voltage"] == 236.9 and sample[CAR + "_current"] == 0.02
    assert set(sample) == {f"{name}{suffix}" for name in CHANNELS for suffix in ("", "_voltage", "_current")}


def test_an_error_names_the_circuit_and_the_shelly_channel():
    bad = {**STATUS, "em1:1": {**STATUS["em1:1"], "errors": ["overpower"]}}
    with pytest.raises(DeviceError) as caught:
        shelly.parse_status(bad)
    assert CAR in str(caught.value) and "em1:1" in str(caught.value)
    missing = {**STATUS, "em1:0": {**STATUS["em1:0"], "voltage": None}}
    with pytest.raises(DeviceError) as caught:
        shelly.parse_status(missing)
    assert SOLARBANK_AC in str(caught.value) and "em1:0" in str(caught.value)


def test_the_monitors_old_names_are_translated_in_one_place():
    old = {"time": 100.0, "ch1": 363.1, "ch1_voltage": 235.6, "ch1_current": 1.557, "ch2": 3.1, "ch2_voltage": 235.6, "ch2_current": 0.03}
    new = from_monitor(old)
    assert new == {"time": 100.0, SOLARBANK_AC: 363.1, SOLARBANK_AC + "_voltage": 235.6, SOLARBANK_AC + "_current": 1.557,
                   CAR: 3.1, CAR + "_voltage": 235.6, CAR + "_current": 0.03}
    assert from_monitor(new) == new                                # role names pass through unchanged
    assert from_monitor({"ch1": 1.0, SOLARBANK_AC: 2.0})[SOLARBANK_AC] == 2.0     # a role name already present is never overwritten
    assert from_monitor(None) is None and from_monitor({}) == {}


def test_a_document_is_translated_without_changing_the_original():
    document = {"ok": True, "shelly": {"ok": True, "sample": {"time": 1.0, "ch1": 5.0, "ch2": 7.0}}, "solarbank": {"ok": True, "sample": {"soc": 50}}}
    result = normalise_document(document)
    assert result["shelly"]["sample"] == {"time": 1.0, SOLARBANK_AC: 5.0, CAR: 7.0}
    assert document["shelly"]["sample"] == {"time": 1.0, "ch1": 5.0, "ch2": 7.0}       # the input is untouched
    assert result["solarbank"] is document["solarbank"]
    assert normalise_document({}) == {} and normalise_document(None) is None
    assert normalise_document({"shelly": {"ok": False, "sample": None}}) == {"shelly": {"ok": False, "sample": None}}


def test_the_recorded_monitor_sample_is_what_the_direct_reader_produces():
    recorded = json.loads((Path(__file__).parent / "fixtures" / "monitor-raw" / "monitor-raw.json").read_text())["shelly"][0]
    assert shelly.parse_status(recorded["raw"]) == from_monitor(recorded["expected"])
    # the recorded readings themselves: the Solarbank serving the house on em1:0 and the car charger on standby on em1:1
    assert recorded["raw"]["em1:0"]["act_power"] == 363.1 and recorded["raw"]["em1:1"]["act_power"] == 3.1


@pytest.mark.parametrize("sample", [
    {"time": 100.0, "ch1": -46.0, "ch2": 2.6},                    # the original monitor's names
    {"time": 100.0, SOLARBANK_AC: -46.0, CAR: 2.6},               # the direct reader's names
], ids=["monitor names", "role names"])
def test_the_collector_stores_each_circuit_under_its_own_metric(sample):
    document = {"ok": True, "sample": {"time": 100.0, "main": 1, "sub": 2}, "shelly": {"ok": True, "sample": sample},
                "solarbank": {"ok": True, "sample": {"time": 100.0, "soc": 71, "pv": 60, "load": 380, "grid": 0, "battery": 320}}}
    result = {o.metric: o for o in decode_snapshot(document, 101.0)}
    assert result["ac_power"].quality == "ok" and result["ac_power"].value == pytest.approx(-0.046)      # the Solarbank taking 46 W in
    assert result["ev_power"].quality == "ok" and result["ev_power"].value == pytest.approx(0.0026)      # the car charger on standby


def test_the_controller_reads_the_car_from_the_car_channel_only():
    sup, t = settled()
    battery_line_busy = fresh(current=20)
    battery_line_busy["shelly"]["value"] = {SOLARBANK_AC: 7000.0, CAR: 0.0}      # 7 kW on the battery line, nothing on the charger
    assert sup.decide(battery_line_busy, plan("discharge"), None, t + 1).discharge == "ALLOW"
    car_charging = fresh(current=20)
    car_charging["shelly"]["value"] = {SOLARBANK_AC: 0.0, CAR: 7000.0}
    for i in range(2, 8):
        d = sup.decide(car_charging, plan("discharge"), None, t + i)
    assert d.discharge == "INHIBIT" and "ev_active" in d.restrictions


def test_no_old_channel_names_remain_in_the_code_outside_the_translator():
    root = Path(__file__).resolve().parent.parent / "src" / "solarbank_optimiser"
    old_name = re.compile(r"""["']ch[12](_voltage|_current)?["']|\bch\{""")
    offenders = [f"{path.relative_to(root)}:{number}" for path in root.rglob("*.py") if path.name != "shelly_channels.py"
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if old_name.search(line)]
    assert offenders == []
