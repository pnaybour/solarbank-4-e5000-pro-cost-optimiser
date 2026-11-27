"""Contract: every register our readers use must agree with the pinned vendor map."""
import hashlib
import json
from pathlib import Path

import pytest

from solarbank_optimiser.devices import meter, solarbank

ROOT = Path(__file__).resolve().parents[1] / "reference" / "anker-solix-official"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
EXTRACT = json.loads((ROOT / "read-map-extract.json").read_text())


def test_snapshot_files_match_their_recorded_hashes():
    assert len(MANIFEST["commit"]) == 40 and MANIFEST["licence"].startswith("MIT")
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == MANIFEST["licence_sha256"]
    for entry in MANIFEST["maps"].values():
        assert hashlib.sha256((ROOT / entry["file"]).read_bytes()).hexdigest() == entry["sha256"]


def test_extract_contains_reads_only():
    text = json.dumps(EXTRACT).lower()
    for word in ("write", "control_items", "auto_mode", "setpoint", "operating_mode"):
        assert word not in text


@pytest.mark.parametrize("role,layout", [("solarbank", solarbank.LAYOUT), ("meter", meter.LAYOUT)])
def test_every_register_we_read_matches_the_pinned_vendor_map(role, layout):
    quantities = EXTRACT[role]["read_quantities"]
    for name, (address, kind, divisor, vendor_key) in layout.items():
        vendor = quantities[vendor_key]
        assert vendor["address"] == address, name
        assert vendor["data_type"] == kind, name
        assert vendor.get("gain", 1) == divisor, name


def test_our_block_reads_sit_inside_documented_input_ranges():
    inside = lambda role, start, count: any(
        r["type"] == "input" and r["start"] <= start and start + count - 1 <= r["end"]
        for r in EXTRACT[role]["batch_read_ranges"])
    for start, count in solarbank.BLOCKS + (solarbank.FAST_BLOCK,) + solarbank.SLOW_BLOCKS:
        assert inside("solarbank", start, count), (start, count)
    for name, (address, *_rest) in meter.LAYOUT.items():
        assert inside("meter", address, 2), name
    # The wide meter read spans two documented ranges that touch; together they must cover it with no gap.
    start, count = meter.WIDE
    covered = set()
    for r in EXTRACT["meter"]["batch_read_ranges"]:
        if r["type"] == "input":
            covered.update(range(r["start"], r["end"] + 1))
    assert set(range(start, start + count)) <= covered


def test_decoder_reads_the_registers_the_layout_names():
    # Distinct values in every register: each field must come from the address the layout says.
    main = list(range(1000, 1040)); ac = [2000, 2001]; rated = [3000, 3001]; totals = [4000, 4001, 4002, 4003]
    main[14] = 55
    result = solarbank.decode(main, ac, rated, totals)
    word = lambda block, start, address: (block[address - start] << 16) | block[address - start + 1]
    assert result["soc"] == 55 and result["status_code"] == main[1]
    assert result["pv_direct"] == word(main, 10000, 10002) and result["pv_external"] == word(main, 10000, 10004)
    assert result["battery"] == word(main, 10000, 10008) and result["load"] == word(main, 10000, 10010)
    assert result["grid"] == word(main, 10000, 10012) and result["max_charge"] == word(main, 10000, 10036)
    assert result["ac_output"] == word(ac, 10208, 10208)
    assert result["rated_kwh"] == word(rated, 10250, 10250) / 10
    assert result["charge_total_kwh"] == word(totals, 10262, 10262) / 10
    assert result["discharge_total_kwh"] == word(totals, 10262, 10264) / 10


def test_identity_registers_match_the_pinned_vendor_map_and_ranges():
    from solarbank_optimiser.devices import identity
    for role, layout in (("solarbank", identity.SOLARBANK), ("meter", identity.METER)):
        for name, (address, count, key) in layout.items():
            vendor = EXTRACT[role]["read_quantities"][key]
            assert vendor["address"] == address and vendor["count"] == count and vendor["data_type"] == "STRING", name
            assert any(r["type"] == "input" and r["start"] <= address and address + count - 1 <= r["end"]
                       for r in EXTRACT[role]["batch_read_ranges"]), name
