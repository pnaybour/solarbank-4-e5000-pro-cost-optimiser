"""Owner controls over the nightly automation: pause it, force a charge level for a while, or return to automatic. Standard library only.

Both live in ordinary settings (so they carry a revision and survive restarts):
* `automation_pause`  {"paused": bool, "until": epoch or None}  - while paused the optimiser asks for no charging at all; the Anker runs the house as normal.
  The planner still works out its advice so the dashboard can show what it WOULD do.
* `manual_override`   {"target": fraction, "valid_until": epoch} - while valid the planner may use only this charge level for every cheap window.
Neither can lower the battery below the minimum level, and neither is ever applied after its expiry."""
PAUSE_SETTING = "automation_pause"
OVERRIDE_SETTING = "manual_override"
MAX_OVERRIDE_S = 14 * 86400


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def pause_state(payload, now):
    """(paused, until) for a saved pause setting at time `now`."""
    if not isinstance(payload, dict) or payload.get("paused") is not True:
        return False, None
    until = payload.get("until")
    if until is None:
        return True, None
    if _number(until) and now < until:
        return True, until
    return False, None


def override_state(payload, now, minimum=0.05):
    """The override in force at `now` as {"target", "valid_until"}, or None (absent, expired or malformed)."""
    if not isinstance(payload, dict):
        return None
    target, until = payload.get("target"), payload.get("valid_until")
    if not (_number(target) and _number(until)) or not minimum - 1e-9 <= target <= 1 or until <= now:
        return None
    return {"target": round(float(target), 2), "valid_until": float(until)}


def describe(pause_payload, override_payload, now, minimum=0.05):
    """What the dashboard shows: {"paused", "paused_until", "override"}."""
    paused, until = pause_state(pause_payload, now)
    return {"paused": paused, "paused_until": until, "override": override_state(override_payload, now, minimum)}
