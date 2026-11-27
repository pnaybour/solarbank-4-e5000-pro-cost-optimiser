"""What the Anker battery is doing right now, in plain words, from the measured powers. Standard library only.

Signs (checked against the 27/28 October readings): battery power is POSITIVE while the battery discharges and NEGATIVE while it
charges; grid power is positive for import. Charging counts as 'from the grid' when the grid import exceeds the household load,
because only then is some of the import going into the battery."""

IDLE_KW = 0.05          # below this the battery is idle
GRID_MARGIN_KW = 0.05


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


def describe(battery_kw, grid_kw, household_kw):
    battery, grid, house = _number(battery_kw), _number(grid_kw), _number(household_kw)
    if battery is None:
        return {"state": "unknown", "text": "The battery's current activity is unknown (no fresh reading).", "battery_kw": None, "from_grid": None}
    if battery > IDLE_KW:
        return {"state": "discharging", "battery_kw": battery, "from_grid": False,
                "text": f"The Anker is discharging {battery:.2f} kW to the house."}
    if battery < -IDLE_KW:
        from_grid = grid is not None and house is not None and grid - house > GRID_MARGIN_KW
        return {"state": "charging", "battery_kw": battery, "from_grid": from_grid,
                "text": f"The Anker is charging at {-battery:.2f} kW " + ("from the grid." if from_grid else "from solar.")}
    return {"state": "idle", "battery_kw": battery, "from_grid": False,
            "text": "The Anker is idle (neither charging nor discharging)."}
