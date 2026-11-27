"""The adjustable minimum battery level, defined once. Standard library only (the controller imports it).

Owner decision, 28 October 2026: 10 % by default, changeable on the settings page, to be kinder to the battery than the 5 %
the specification started with. The planner uses it as the reserve it must not plan below, and it clamps the controller's charge
target (D13, 2 November, GitHub issue 64 and 70: the firmware alone owns the discharge floor and stops the battery itself; the
controller only reads the device's own register and shows a mismatch; it does not inhibit discharge at this level itself)."""

DEFAULT_MIN_SOC = 0.10
MIN_SOC_LOWEST = 0.05      # never plan or allow discharge below this
MIN_SOC_HIGHEST = 0.20     # the device's own discharge-limit register (60001) accepts 0 to 20 %, so a higher floor could not be matched by the firmware (owner, 30 Oct)
SETTING = "min_soc"


def valid_min_soc(value):
    """The value if it is a real number inside the allowed range, otherwise None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value) if MIN_SOC_LOWEST <= value <= MIN_SOC_HIGHEST else None
