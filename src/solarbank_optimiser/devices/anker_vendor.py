"""Core helpers adapted from Anker's official Home Assistant integration, so we follow the vendor's behaviour exactly and can diff against it when it changes.

Source: https://github.com/anker-charging/ha-anker-solix-official, commit 21c94833993cc14dbb03fd1a945106f7b1afaafe (MIT licence, Copyright (c) 2026 Anker
Innovations; licence text in reference/anker-solix-official/LICENSE). The vendor's own files are kept byte-for-byte in reference/anker-solix-official/source/.
Every function below names the vendor function it comes from. They were rewritten as pure functions: no Home Assistant, no asyncio, no logging, no network and
**no way to send anything to a device**; they only turn values into register words and decide whether a request is allowed. Writing to the equipment stays
disabled until the commissioning stage (DEVELOPMENT_PLAN.md C3), and this module never opens a connection.

ANKER_INTEGRATION_NOTES.md lists what is copied, what is deliberately not, and how to take an upstream update. tests/test_anker_vendor.py checks the constants
here against the pinned register map and the helpers against the vendor's behaviour.

Standard library only.
"""

# ---- registers and values, from the pinned Solarbank 4 E5000 Pro map (config/58f0132b...yaml) -----------------------------------------------------------
ADDR_DEVICE_PN = 32768             # read_quantities.device_model, STRING, 5 registers (the part number, "AE103" on this unit)
ADDR_DEVICE_SN = 10100             # read_quantities.device_sn, STRING, 12 registers
ADDR_OPERATING_MODE = 10064        # write_quantities.operating_mode, UINT16
ADDR_POWER_SETPOINT = 10071        # control_items.battery_power_setpoint, INT32 (two registers), never read from the device
ADDR_CHARGING_LIMIT_SOC = 60000    # control_items.charging_limit_soc, UINT16, 80 to 100
ADDR_DISCHARGE_LIMIT_SOC = 60001   # 0 to 20
ADDR_BACKUP_RESERVE_SOC = 60002    # 0 to 100, writable only while 60003 = 1
ADDR_BACKUP_SOC_ENABLE = 60003
ADDR_EMS_MODE_MASK = 32774         # 0x8006
ADDR_CAPABILITY_MASK = 32775       # 0x8007, parallel_capability_mask (its own single FC04 read)

OPERATING_MODES = {0: "self_consumption", 1: "tou_mode", 3: "third_party_control", 4: "custom_mode", 5: "socket_overlay_mode", 6: "smart_mode", 7: "dynamic_pricing"}
MODE_THIRD_PARTY = 3
# 0x8006 bit that enables each mode value (operating_mode.option_capability_bits)
MODE_CAPABILITY_BIT = {0: 0, 1: 1, 3: 5, 4: 2, 5: 4, 6: 3, 7: 6}
CAPABILITY_BITS = {0: "charging_limit_soc", 1: "discharge_limit_soc", 2: "backup_reserve_soc", 3: "backup_soc_enable"}
SETPOINT_MIN_W, SETPOINT_MAX_W = 0, 10000
SOC_LIMITS = {"charging_limit_soc": (80, 100), "discharge_limit_soc": (0, 20), "backup_reserve_soc": (0, 100)}
DIRECTIONS = ("charge", "discharge")

# the vendor's soc_validation rules, keyed by control item (control_items.*.soc_validation)
SOC_RULES = {
    "charging_limit_soc": {"condition_entity": "backup_soc_enable", "condition_value": 1, "greater_than": ["discharge_limit_soc", "backup_reserve_soc"]},
    "discharge_limit_soc": {"condition_entity": "backup_soc_enable", "condition_value": 1, "less_than_or_equal": "backup_reserve_soc", "less_than": "charging_limit_soc"},
    "backup_reserve_soc": {"condition_entity": "backup_soc_enable", "condition_value": 1, "greater_than_or_equal": "discharge_limit_soc", "less_than": "charging_limit_soc"},
}
# control_items.backup_reserve_soc.write_condition: the reserve may only be written while the backup switch is on
WRITE_CONDITIONS = {"backup_reserve_soc": {"entity": "backup_soc_enable", "operator": "eq", "value": 1, "hint": "enable_backup_soc_first"}}


class VendorRuleError(ValueError):
    """A request the vendor's own rules would refuse. `key` is the vendor's translation key (soc_must_be_greater_than, charge_power_too_low, ...)."""

    def __init__(self, key, message):
        super().__init__(message)
        self.key = key


# ---- reading: modbus_client.AnkerSolixModbusClient._decode_register_value ---------------------------------------------------------------------------------
def decode_registers(data_type, registers):
    """Registers (16-bit words, big-endian, high word first) -> a Python value. Raises ValueError for empty input or too few words, like the vendor's _RegisterDecodeError."""
    if not registers or any(r is None for r in registers):
        raise ValueError("no register data")
    if data_type == "UINT16":
        return registers[0]
    if data_type == "INT16":
        raw = registers[0] & 0xFFFF
        return raw if raw < 0x8000 else raw - 0x10000
    if data_type in ("INT32", "UINT32"):
        if len(registers) < 2:
            raise ValueError(f"{data_type} needs 2 registers, got {len(registers)}")
        unsigned = ((registers[0] & 0xFFFF) << 16) | (registers[1] & 0xFFFF)
        if data_type == "INT32" and unsigned & 0x80000000:
            return -((~unsigned & 0xFFFFFFFF) + 1)
        return unsigned
    if data_type == "VERSION":
        parts = []
        for reg in registers[:2]:
            parts += [(reg >> 8) & 0xFF, reg & 0xFF]
        return ".".join(str(p) for p in parts) if len(parts) >= 4 else ""
    if data_type == "STRING":
        raw = bytes(b for reg in registers for b in ((reg >> 8) & 0xFF, reg & 0xFF))
        return raw.decode("utf-8", errors="ignore").rstrip("\x00")
    return registers[0]


# ---- writing, as DATA ONLY: modbus_client.AnkerSolixModbusClient.write_register ---------------------------------------------------------------------------
def encode_registers(value, data_type):
    """(Modbus function code, [words]) the vendor would send for `value`: function 6 for one register, function 16 for two (INT32/UINT32, high word first, negatives in
    two's complement). Nothing is sent."""
    if data_type == "INT32":
        number = int(value)
        if number < 0:
            number += 0x100000000
        return 0x10, [(number >> 16) & 0xFFFF, number & 0xFFFF]
    if data_type == "UINT32":
        number = int(value)
        return 0x10, [(number >> 16) & 0xFFFF, number & 0xFFFF]
    return 0x06, [int(value) & 0xFFFF]                      # UINT16 and anything else


def signed_setpoint(power_w, direction):
    """The INT32 value for the battery power set-point (number.py: the sign comes from the chosen direction): negative = charge, positive = discharge."""
    if direction not in DIRECTIONS:
        raise ValueError("direction must be 'charge' or 'discharge'")
    if not SETPOINT_MIN_W <= power_w <= SETPOINT_MAX_W:
        raise VendorRuleError("value_out_of_range", f"power must be {SETPOINT_MIN_W} to {SETPOINT_MAX_W} W")
    magnitude = abs(int(power_w))
    return -magnitude if direction == "charge" else magnitude


def setpoint_direction(raw):
    """'charge', 'discharge' or None (zero) for a set-point value."""
    return None if raw == 0 else ("charge" if raw < 0 else "discharge")


# ---- identity: product_mapping.py and modbus_client.read_device_pn ---------------------------------------------------------------------------------------
def extract_product_code_from_sn(sn):
    """product_mapping.extract_product_code_from_sn: characters 4 to 6 of a 16-character serial, characters 4 to 7 of a 17-character one; else None."""
    if not sn or not isinstance(sn, str):
        return None
    sn = sn.strip()
    if len(sn) == 16:
        return sn[3:6]
    if len(sn) == 17:
        return sn[3:7]
    return None


def product_name(sn, product_info, fallback=None):
    """product_mapping.get_product_name_from_config: the product-code mapping first, then the map's default name, then the fallback, then 'Unknown Device'."""
    info = product_info or {}
    code = extract_product_code_from_sn(sn)
    mapping = info.get("product_code_mapping", {})
    if code and code in mapping:
        return mapping[code]
    return info.get("default_name") or fallback or "Unknown Device"


def parse_part_number(registers):
    """modbus_client.read_device_pn: the five registers at 0x8000 as text, spaces and NULs removed (the vendor then hashes it; we keep the text for our own identity record)."""
    return decode_registers("STRING", registers).replace(" ", "").replace("\x00", "").strip()


PN_HASH_SALT = "anker_solix_ha_2024"            # modbus_client.read_device_pn


def config_file_name_for_part_number(part_number):
    """coordinator._get_config_file_path: the vendor picks a device's register map by the file name sha256(salt + part number) + '.yaml'. For this Solarbank's
    part number (AE103) that is exactly the name of the map we pinned, which is a documented way of confirming the device is the model the map describes."""
    import hashlib
    return hashlib.sha256((PN_HASH_SALT + part_number).encode()).hexdigest() + ".yaml"


# ---- what the vendor lets you request: select.py, number.py, base_entity.py -----------------------------------------------------------------------------
def filter_options_by_mask(options, option_bits, mask):
    """select._get_filtered_options: the options whose capability bit is set in `mask`; with no mask, all of them. `options` maps value -> name; `option_bits` value -> bit."""
    if mask is None or not option_bits:
        return list(options.values())
    out = []
    for value, name in options.items():
        bit = option_bits.get(value)
        if bit is None or mask & (1 << bit):
            out.append(name)
    return out


def offered_modes(ems_mode_mask):
    """The operating modes this device offers, given its 0x8006 mask (value -> name), using the vendor's option bits."""
    return {value: OPERATING_MODES[value] for value, bit in MODE_CAPABILITY_BIT.items() if ems_mode_mask & (1 << bit)}


def evaluate_operator(value, operator, target):
    """base_entity._evaluate_operator: eq/ne compare whole numbers within half a unit, in/not_in too; anything unrecognised or with no target passes."""
    if target is None:
        return True
    if operator in ("in", "not_in"):
        targets = target if isinstance(target, (list, tuple, set)) else [target]
        hit = any(abs(value - float(t)) < 0.5 for t in targets)
        return hit if operator == "in" else not hit
    if not isinstance(target, (int, float)):
        try:
            target = float(target)
        except (ValueError, TypeError):
            return True
    whole = isinstance(target, int) or target == int(target)
    if operator == "eq":
        return abs(value - target) < 0.5 if whole else abs(value - target) < 1e-6
    if operator == "ne":
        return abs(value - target) >= 0.5 if whole else abs(value - target) >= 1e-6
    return {"gt": value > target, "gte": value >= target, "lt": value < target, "lte": value <= target}.get(operator, True)


def write_condition_passes(readings, condition):
    """base_entity._check_write_condition: (passed, hint). A missing reading fails the condition, as in the vendor code."""
    if not condition:
        return True, None
    key = condition.get("entity")
    if not key:
        return True, None
    current = (readings or {}).get(key)
    if current is None:
        return False, condition.get("hint")
    try:
        current = float(current)
    except (ValueError, TypeError):
        return False, condition.get("hint")
    passed = evaluate_operator(current, condition.get("operator", "eq"), condition.get("value"))
    return passed, None if passed else condition.get("hint")


def validate_soc_constraints(name, value, readings, rule=None):
    """number._validate_soc_constraints for one limit register: raises VendorRuleError with the vendor's translation key when the request breaks an ordering rule
    (charging limit above discharge limit and reserve, and so on). The rules only apply while the backup switch (60003) is on; unknown neighbours are skipped."""
    rule = SOC_RULES.get(name) if rule is None else rule
    if not rule:
        return
    readings = readings or {}
    condition = rule.get("condition_entity")
    if condition:
        current = readings.get(condition)
        try:
            if current is None or int(current) != int(rule.get("condition_value")):
                return
        except (ValueError, TypeError):
            return

    def other(key):
        try:
            return float(readings[key])
        except (KeyError, ValueError, TypeError):
            return None

    checks = []
    targets = rule.get("greater_than", [])
    for key in ([targets] if isinstance(targets, str) else targets):
        checks.append((key, lambda v, t: v > t, "soc_must_be_greater_than"))
    for rule_key, test, error in (("greater_than_or_equal", lambda v, t: v >= t, "soc_must_be_greater_than_or_equal"),
                                  ("less_than", lambda v, t: v < t, "soc_must_be_less_than"),
                                  ("less_than_or_equal", lambda v, t: v <= t, "soc_must_be_less_than_or_equal")):
        if rule_key in rule:
            checks.append((rule[rule_key], test, error))
    for key, test, error in checks:
        target = other(key)
        if target is not None and not test(value, target):
            raise VendorRuleError(error, f"{name} {int(value)} must satisfy {error.replace('soc_must_be_', '').replace('_', ' ')} {key} ({int(target)})")


def check_limit_request(name, value, readings):
    """The whole vendor check for a charge/discharge limit write: range, write condition, then ordering. Raises VendorRuleError; returns the value when allowed."""
    low, high = SOC_LIMITS[name]
    if not low <= value <= high:
        raise VendorRuleError("value_out_of_range", f"{name} must be {low} to {high}")
    passed, hint = write_condition_passes(readings, WRITE_CONDITIONS.get(name))
    if not passed:
        raise VendorRuleError(hint or "write_condition_failed", f"{name} cannot be set yet ({hint})")
    validate_soc_constraints(name, value, readings)
    return int(value)


def check_power_capacity(direction, power_w, readings):
    """number.async_set_native_value's capacity check: refuse a charge (discharge) request when the device's own maximum charge (discharge) power, registers 10036
    (10038), is below the request's minimum of SETPOINT_MIN_W. With no reading for it the vendor lets the request through."""
    key = {"charge": "max_charge_power", "discharge": "max_discharge_power"}[direction]
    raw = (readings or {}).get(key)
    if raw is None:
        return
    if abs(int(raw)) < SETPOINT_MIN_W:
        raise VendorRuleError("charge_power_too_low" if direction == "charge" else "discharge_power_too_low", f"device maximum {abs(int(raw))} W is too low")


# ---- our own, built from the vendor's documented behaviour: the command sequences as data (never sent here) ----------------------------------------------------
def charge_sequence(power_w, previous_mode):
    """The documented way to charge from outside the Anker app: put the device in third-party control, then give a negative battery power. Returned as a list of
    {register, data_type, value, function, words, why} dictionaries for the commissioning trial and the tests; nothing is sent."""
    if previous_mode not in OPERATING_MODES or previous_mode == MODE_THIRD_PARTY:
        raise ValueError("previous_mode must be the Anker mode to return to (not third-party control)")
    return [_step(ADDR_OPERATING_MODE, "UINT16", MODE_THIRD_PARTY, "third-party control: the device follows commands"),
            _step(ADDR_POWER_SETPOINT, "INT32", signed_setpoint(power_w, "charge"), f"charge at {int(power_w)} W (negative = charge)")]


def stop_and_restore_sequence(previous_mode):
    """Stop charging and return the device to the Anker's own mode, as data: a zero set-point first, then the previous operating mode."""
    if previous_mode not in OPERATING_MODES or previous_mode == MODE_THIRD_PARTY:
        raise ValueError("previous_mode must be the Anker mode to return to (not third-party control)")
    return [_step(ADDR_POWER_SETPOINT, "INT32", 0, "stop charging"),
            _step(ADDR_OPERATING_MODE, "UINT16", previous_mode, f"return to {OPERATING_MODES[previous_mode]}")]


def _step(address, data_type, value, why):
    function, words = encode_registers(value, data_type)
    return {"register": address, "data_type": data_type, "value": value, "function": function, "words": words, "why": why}
