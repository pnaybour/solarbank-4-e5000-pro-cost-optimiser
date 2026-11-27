"""Smart Meter Gen 2: total active power of the two CT groups, the garage-feed current and the primary (grid) voltage (FC04, INT32 high word first).

Primary CT (10644) is the grid connection; secondary CT (10675) is the PV/ESS feed to the garage. The garage current is the
secondary CT's phase currents (10666-10668, INT16, divided by 100): a MAGNITUDE (direction comes from the power sign). The
installation is single-phase (L1 only carries current, 26 Oct evidence) but the limiter takes the largest of the three so it
can never under-read if the phase assignment turns out to differ. The primary (grid) CT current is not used (it picks up noise;
the Solarbank manages the grid).

The primary phase 1 voltage (10632, GitHub issue 69) is this meter's own reading, specification section 5.1's V: used for the reconciliation and
voltage validity checks (11.2), never for the garage power calculation itself, which keeps using the Shelly's voltage on the Solarbank's own channel.
"""
from .solarbank import signed

# name: (address, type, divisor, vendor key in the pinned map)
LAYOUT = {
    "voltage": (10632, "UINT16", 10, "primary_phase_1_voltage"),
    "main": (10644, "INT32", 1, "primary_total_active_power"),
    "sub": (10675, "INT32", 1, "secondary_total_active_power"),
    "sub_l1_current": (10666, "INT16", 100, "secondary_phase_1_current"),
    "sub_l2_current": (10667, "INT16", 100, "secondary_phase_2_current"),
    "sub_l3_current": (10668, "INT16", 100, "secondary_phase_3_current"),
}
CURRENTS = ("sub_l1_current", "sub_l2_current", "sub_l3_current")


# One request covers the voltage and both CT powers from the same instant (4a.4c: 33 registers returned intact, 42 ms against 84 ms for two reads;
# widened by 12 registers for the voltage at 10632, still one request, GitHub issue 69). The secondary-phase currents (10666-10668) lie inside the
# same block, so garage current costs no extra request.
WIDE = (10632, 45)


def decode_power(registers):
    return signed((registers[0] << 16) | registers[1], 32)


def decode_current(register):
    return abs(signed(register, 16)) / 100.0


def decode_voltage(register):
    return register / 10.0


def read(client):
    registers = client.read_input_registers(*WIDE)
    at = lambda name: registers[LAYOUT[name][0] - WIDE[0]:LAYOUT[name][0] - WIDE[0] + (2 if LAYOUT[name][1] == "INT32" else 1)]
    currents = [decode_current(at(name)[0]) for name in CURRENTS]
    return {"main": decode_power(at("main")), "sub": decode_power(at("sub")), "voltage": decode_voltage(at("voltage")[0]),
            "garage_current_a": max(currents), "garage_phase_currents_a": currents}
