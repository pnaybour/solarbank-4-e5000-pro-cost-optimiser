"""The helpers adapted from Anker's official integration behave as the vendor's do, and agree with the vendor's pinned register map. Also: our reference copies are intact."""
import hashlib
import importlib.util
import inspect
import json
import re
from pathlib import Path

import pytest

from solarbank_optimiser.devices import anker_vendor as v

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "reference" / "anker-solix-official"
MAP = json.loads((REF / "control-map-extract.json").read_text())["map"]


# ===== the constants agree with the pinned map ====================================================================================================================
def test_the_extract_was_made_from_the_pinned_yaml():
    extract = json.loads((REF / "control-map-extract.json").read_text())
    assert hashlib.sha256((REF / extract["source"]).read_bytes()).hexdigest() == extract["source_sha256"]


def test_addresses_types_and_ranges_match_the_map():
    reads, controls, writes = MAP["read_quantities"], MAP["control_items"], MAP["write_quantities"]["enumeration_selection"]
    assert reads["device_model"]["address"] == v.ADDR_DEVICE_PN and reads["device_model"]["count"] == 5 and reads["device_model"]["data_type"] == "STRING"
    assert reads["device_sn"]["address"] == v.ADDR_DEVICE_SN and reads["device_sn"]["count"] == 12
    assert reads["ems_mode_mask"]["address"] == v.ADDR_EMS_MODE_MASK and reads["parallel_capability_mask"]["address"] == v.ADDR_CAPABILITY_MASK
    assert reads["backup_soc_enable"]["address"] == v.ADDR_BACKUP_SOC_ENABLE
    assert writes["operating_mode"]["address"] == v.ADDR_OPERATING_MODE and writes["operating_mode"]["data_type"] == "UINT16"
    setpoint = controls["battery_power_setpoint"]
    assert (setpoint["address"], setpoint["data_type"], setpoint["min_value"], setpoint["max_value"], setpoint["never_read_device"]) == (v.ADDR_POWER_SETPOINT, "INT32", v.SETPOINT_MIN_W, v.SETPOINT_MAX_W, True)
    for name, address in (("charging_limit_soc", v.ADDR_CHARGING_LIMIT_SOC), ("discharge_limit_soc", v.ADDR_DISCHARGE_LIMIT_SOC), ("backup_reserve_soc", v.ADDR_BACKUP_RESERVE_SOC)):
        item = controls[name]
        assert item["address"] == address and item["data_type"] == "UINT16" and (item["min_value"], item["max_value"]) == v.SOC_LIMITS[name]


def test_modes_bits_and_rules_match_the_map():
    mode = MAP["write_quantities"]["enumeration_selection"]["operating_mode"]
    assert {int(k): name for k, name in mode["options"].items()} == v.OPERATING_MODES
    assert {int(k): bit for k, bit in mode["option_capability_bits"].items()} == v.MODE_CAPABILITY_BIT
    assert MAP["product_info"]["auto_mode_on_connect"] == v.MODE_THIRD_PARTY
    controls = MAP["control_items"]
    for name, rule in v.SOC_RULES.items():
        assert controls[name]["soc_validation"] == rule
    assert controls["backup_reserve_soc"]["write_condition"] == {**v.WRITE_CONDITIONS["backup_reserve_soc"]}
    assert set(MAP["product_info"]["product_code_mapping"]) == {"DN7M", "DPM4"}


def test_the_capability_bit_names_match_the_maps_comment():
    text = (REF / "58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml").read_text()
    for bit, name in v.CAPABILITY_BITS.items():
        assert f"BIT{bit}: {name}" in text


# ===== reading ===========================================================================================================================================
@pytest.mark.parametrize("kind,words,expected", [
    ("UINT16", [72], 72), ("INT16", [0xFFFF], -1), ("INT16", [0x7FFF], 32767), ("INT32", [0xFFFF, 0xF63C], -2500), ("INT32", [0, 2500], 2500), ("INT32", [0x8000, 0], -2147483648),
    ("UINT32", [0xFFFF, 0xFFFF], 4294967295), ("UINT32", [1, 0], 65536), ("VERSION", [0x0000, 0x0100], "0.0.1.0"), ("STRING", [0x4145, 0x3130, 0x3300, 0, 0], "AE103"),
])
def test_decoding_matches_the_vendors(kind, words, expected):
    assert v.decode_registers(kind, words) == expected


@pytest.mark.parametrize("kind,words", [("UINT16", []), ("INT32", [1]), ("UINT32", [5]), ("INT32", [1, None])])
def test_too_little_data_is_an_error_like_the_vendors(kind, words):
    with pytest.raises(ValueError):
        v.decode_registers(kind, words)


def test_the_device_part_number_registers_decode_to_the_text_we_read_from_the_pi():
    assert v.parse_part_number([16709, 12592, 13056, 0, 0]) == "AE103"


# ===== writing, as data ===================================================================================================================================
@pytest.mark.parametrize("value,kind,expected", [
    (1, "UINT16", (0x06, [1])), (3, "UINT16", (0x06, [3])), (70000, "UINT16", (0x06, [70000 & 0xFFFF])),
    (-2500, "INT32", (0x10, [0xFFFF, 0xF63C])), (2500, "INT32", (0x10, [0, 2500])), (0, "INT32", (0x10, [0, 0])), (-1, "INT32", (0x10, [0xFFFF, 0xFFFF])),
    (65536, "UINT32", (0x10, [1, 0])),
])
def test_encoding_matches_the_vendors(value, kind, expected):
    assert v.encode_registers(value, kind) == expected


def test_encode_then_decode_round_trips_signed_and_unsigned_values():
    for value in (-10000, -2500, -1, 0, 1, 250, 10000, 2147483647, -2147483648):
        assert v.decode_registers("INT32", v.encode_registers(value, "INT32")[1]) == value
    for value in (0, 1, 65535, 65536, 4294967295):
        assert v.decode_registers("UINT32", v.encode_registers(value, "UINT32")[1]) == value


def test_the_setpoint_sign_follows_the_direction():
    assert v.signed_setpoint(2500, "charge") == -2500 and v.signed_setpoint(2500, "discharge") == 2500 and v.signed_setpoint(0, "charge") == 0
    assert v.setpoint_direction(-1) == "charge" and v.setpoint_direction(5) == "discharge" and v.setpoint_direction(0) is None
    for bad in ((10001, "charge"), (-1, "charge")):
        with pytest.raises(v.VendorRuleError):
            v.signed_setpoint(*bad)
    with pytest.raises(ValueError):
        v.signed_setpoint(100, "sideways")


def test_the_documented_charge_sequence_is_mode_three_then_a_negative_setpoint():
    steps = v.charge_sequence(2500, previous_mode=1)
    assert [(s["register"], s["function"], s["words"]) for s in steps] == [(10064, 0x06, [3]), (10071, 0x10, [0xFFFF, 0xF63C])]
    back = v.stop_and_restore_sequence(6)
    assert [(s["register"], s["function"], s["words"]) for s in back] == [(10071, 0x10, [0, 0]), (10064, 0x06, [6])]
    for bad in (3, 2, 99, None):
        with pytest.raises(ValueError):
            v.charge_sequence(2500, bad)
        with pytest.raises(ValueError):
            v.stop_and_restore_sequence(bad)


# ===== identity (vendor tests: tests/test_product_mapping.py) ====================================================================================================
@pytest.mark.parametrize("sn,code", [("123DMWH4567890123", "DMWH"), ("123QNA4567890123", "QNA"), ("  123QNA4567890123  ", "QNA"), ("123456789012345", None), ("123DMWH45678901234", None),
                                     ("", None), (None, None), (12345, None)])
def test_product_code_from_the_serial_number(sn, code):
    assert v.extract_product_code_from_sn(sn) == code


def test_product_name_lookup_and_fallbacks():
    info = MAP["product_info"]
    assert v.product_name("123DN7M4567890123", info) == "Anker SOLIX Solarbank 4 E5000 Pro"
    assert v.product_name("123XXXX4567890123", info) == "Anker SOLIX Solarbank 4 E5000 Pro"       # unknown code: the map's default name
    assert v.product_name("bad", None, "PN-from-register") == "PN-from-register" and v.product_name("bad", None) == "Unknown Device"


def test_this_solarbanks_part_number_selects_exactly_the_map_we_pinned():
    name = v.config_file_name_for_part_number("AE103")                                   # the part number read from register 0x8000 on our unit, 29 Oct 2026
    assert name == "58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml" and (REF / name).exists()
    assert v.config_file_name_for_part_number("AE104") != name


# ===== what the vendor lets you request ==========================================================================================================================
def test_the_mode_mask_filters_the_offered_modes_the_way_the_vendors_select_does():
    assert v.offered_modes(47) == {0: "self_consumption", 1: "tou_mode", 3: "third_party_control", 4: "custom_mode", 6: "smart_mode"}      # this unit's 0x8006 value
    assert 7 not in v.offered_modes(47) and 5 not in v.offered_modes(47)
    options, bits = {"0": "a", "1": "b", "9": "c"}, {"0": 0, "1": 1}
    assert v.filter_options_by_mask(options, bits, 0b01) == ["a", "c"]                           # no bit requirement: always offered
    assert v.filter_options_by_mask(options, bits, None) == ["a", "b", "c"] and v.filter_options_by_mask(options, {}, 0) == ["a", "b", "c"]


@pytest.mark.parametrize("value,operator,target,expected", [
    (1, "eq", 1, True), (1.2, "eq", 1, True), (2, "eq", 1, False), (1, "ne", 2, True), (1, "ne", 1, False), (5, "gt", 4, True), (4, "gt", 4, False), (4, "gte", 4, True),
    (3, "lt", 4, True), (4, "lte", 4, True), (2, "in", [1, 2], True), (3, "in", [1, 2], False), (3, "not_in", [1, 2], True), (1, "eq", None, True), (1, "mystery", 1, True),
    (1, "gt", "text", True),
])
def test_operator_evaluation_matches_the_vendors(value, operator, target, expected):
    assert v.evaluate_operator(value, operator, target) is expected


def test_write_conditions():
    condition = v.WRITE_CONDITIONS["backup_reserve_soc"]
    assert v.write_condition_passes({"backup_soc_enable": 1}, condition) == (True, None)
    assert v.write_condition_passes({"backup_soc_enable": 0}, condition) == (False, "enable_backup_soc_first")
    assert v.write_condition_passes({}, condition) == (False, "enable_backup_soc_first")             # an unreadable condition register blocks the write
    assert v.write_condition_passes({"backup_soc_enable": "x"}, condition)[0] is False and v.write_condition_passes({}, None) == (True, None)


def test_limit_requests_follow_the_vendors_ranges_and_ordering_rules():
    backup_on = {"backup_soc_enable": 1, "discharge_limit_soc": 10, "backup_reserve_soc": 20, "charging_limit_soc": 100}
    assert v.check_limit_request("charging_limit_soc", 85, backup_on) == 85
    for name, value in (("charging_limit_soc", 79), ("charging_limit_soc", 101), ("discharge_limit_soc", 21), ("discharge_limit_soc", -1)):
        with pytest.raises(v.VendorRuleError) as caught:
            v.check_limit_request(name, value, backup_on)
        assert caught.value.key == "value_out_of_range"
    assert v.check_limit_request("discharge_limit_soc", 15, backup_on) == 15                          # 15 is within 0 to 20 and not above the reserve of 20


def test_ordering_rules_only_apply_while_the_backup_switch_is_on():
    on = {"backup_soc_enable": 1, "discharge_limit_soc": 10, "backup_reserve_soc": 20, "charging_limit_soc": 90}
    with pytest.raises(v.VendorRuleError) as caught:
        v.validate_soc_constraints("discharge_limit_soc", 25, on)                                    # must be at most the backup reserve (20)
    assert caught.value.key == "soc_must_be_less_than_or_equal"
    with pytest.raises(v.VendorRuleError) as caught:
        v.validate_soc_constraints("backup_reserve_soc", 5, on)                                      # must be at least the discharge limit (10)
    assert caught.value.key == "soc_must_be_greater_than_or_equal"
    with pytest.raises(v.VendorRuleError) as caught:
        v.validate_soc_constraints("backup_reserve_soc", 95, on)                                     # must be below the charging limit (90)
    assert caught.value.key == "soc_must_be_less_than"
    with pytest.raises(v.VendorRuleError) as caught:
        v.validate_soc_constraints("charging_limit_soc", 20, on)                                     # must be above discharge limit and reserve
    assert caught.value.key == "soc_must_be_greater_than"
    off = {**on, "backup_soc_enable": 0}
    v.validate_soc_constraints("discharge_limit_soc", 25, off)                                       # no rule while backup is off
    v.validate_soc_constraints("discharge_limit_soc", 25, {})                                        # unknown switch: skipped
    v.validate_soc_constraints("backup_reserve_soc", 5, {"backup_soc_enable": 1})                    # unknown neighbours: skipped


def test_the_reserve_cannot_be_written_while_backup_is_off():
    with pytest.raises(v.VendorRuleError) as caught:
        v.check_limit_request("backup_reserve_soc", 20, {"backup_soc_enable": 0})
    assert caught.value.key == "enable_backup_soc_first"


def test_the_power_capacity_check_is_as_permissive_as_the_vendors():
    v.check_power_capacity("charge", 2500, {})                                                       # nothing read: allowed
    v.check_power_capacity("charge", 2500, {"max_charge_power": 5000})
    v.check_power_capacity("discharge", 2500, {"max_discharge_power": -3000})                       # the vendor compares magnitudes against the item's minimum (0 here)


# ===== this module cannot talk to a device ========================================================================================================================
def test_the_module_has_no_io_and_sends_nothing():
    source = inspect.getsource(v)
    assert not re.search(r"^\s*(import|from)\s+(socket|pymodbus|asyncio|requests|urllib|subprocess|os|pathlib)\b", source, re.MULTILINE)
    assert not re.search(r"write_registers?\(|\.connect\(|\.send\(", source)


# ===== the reference copies are intact ===========================================================================================================================
MANIFEST = json.loads((REF / "source-manifest.json").read_text())


def test_every_reference_copy_matches_its_recorded_hash():
    for relative, info in MANIFEST["files"].items():
        data = (REF / "source" / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == info["sha256"] and len(data) == info["bytes"], relative
    for name, info in MANIFEST["pinned_config_files"].items():
        assert hashlib.sha256((REF / name).read_bytes()).hexdigest() == info["sha256"], name
    assert (REF / "LICENSE").exists() and "MIT" in (REF / "LICENSE").read_text()
    assert re.fullmatch(r"[0-9a-f]{40}", MANIFEST["commit"])


def test_the_vendors_own_integration_still_marks_the_setpoint_unreadable_and_sets_mode_three_on_connect():
    code = (REF / "source/custom_components/anker_solix_official/coordinator.py").read_text()
    assert 'product_info.get("auto_mode_on_connect")' in code
    number = (REF / "source/custom_components/anker_solix_official/number.py").read_text()
    assert "-abs(write_value)" in number and "abs(write_value)" in number                           # charge is negative, discharge positive


# ===== the upstream checker's comparison (offline) ===============================================================================================================
def load_checker():
    spec = importlib.util.spec_from_file_location("check_anker_upstream", ROOT / "scripts" / "check_anker_upstream.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_upstream_comparison_reports_changes_removals_and_new_files():
    checker = load_checker()
    prefix = "custom_components/anker_solix_official/"
    tree = {f: info["git_blob_sha1"] for f, info in MANIFEST["files"].items()}
    tree.update({prefix + "config/" + n: info["git_blob_sha1"] for n, info in MANIFEST["pinned_config_files"].items()})
    tree.update({p: "x" for p in MANIFEST["not_copied"] + MANIFEST["other_device_maps"]})
    assert checker.compare(MANIFEST, tree) == {"changed": [], "removed": [], "new": []}
    tree[prefix + "number.py"] = "different"
    tree[prefix + "config/58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml"] = "different"
    del tree[prefix + "select.py"]
    tree[prefix + "brand_new.py"] = "y"
    tree[prefix + "config/aaaa.yaml"] = "z"
    result = checker.compare(MANIFEST, tree)
    assert set(result["changed"]) == {prefix + "number.py", prefix + "config/58f0132b5f7979b2cfa43a0eb1fca770053288032386ff6a4da5ed2d72d4ea35.yaml"}
    assert result["removed"] == [prefix + "select.py"] and set(result["new"]) == {prefix + "brand_new.py", prefix + "config/aaaa.yaml"}


def test_the_blob_hash_is_the_one_git_uses():
    checker = load_checker()
    assert checker.git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"          # `echo hello | git hash-object --stdin`
