"""How old a reading may be before the controller treats it as unknown (owner decision, 8 November 2026, option C).

Owner specification v1.3 says STALE_S = 10 s ("review after commissioning"); the controller had used 2.5 s everywhere, which made one slightly late sample (7 November 09:33) look
like a lost reading. Two limits now, both settings: while a charge is under way or requested the garage current drives the charge power, so the tight limit applies; otherwise the
specification's 10 s. The planner writes the current values into the plan file (as it does the minimum level) and the controller reads them there. Standard library only."""

SETTING = "freshness"
DEFAULT_CHARGE_S = 2.5          # while charging (or about to): the garage current sets the charge power
DEFAULT_IDLE_S = 10.0           # otherwise: owner specification v1.3 STALE_S
CHARGE_RANGE = (1.0, 10.0)
IDLE_RANGE = (2.5, 30.0)


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


def valid(value):
    """{"charge_s", "idle_s"} if both are real numbers inside their ranges and the idle limit is not tighter than the charging one, else None."""
    if not isinstance(value, dict):
        return None
    charge, idle = _number(value.get("charge_s")), _number(value.get("idle_s"))
    if charge is None or idle is None:
        return None
    if not CHARGE_RANGE[0] <= charge <= CHARGE_RANGE[1] or not IDLE_RANGE[0] <= idle <= IDLE_RANGE[1] or idle < charge:
        return None
    return {"charge_s": charge, "idle_s": idle}


def stored(payload):
    """What the setting holds, or the defaults when it is absent or invalid."""
    return valid(payload) or {"charge_s": DEFAULT_CHARGE_S, "idle_s": DEFAULT_IDLE_S}
