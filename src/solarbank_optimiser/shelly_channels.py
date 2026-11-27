"""The Shelly's two channels, named by what they measure and defined ONCE. Standard library only: the controller imports it.

The Shelly Pro EM-50 numbers its channels from 0 (`em1:0`, `em1:1`). The original energy monitor, its website and this code's
older sample keys (`ch1`, `ch2`) counted from 1, so "channel 1" meant a different circuit depending on who was speaking, and a
mix-up would make the controller block on the battery's own power. The keys below say what is measured instead:

    Shelly channel 0, em1:0, "Battery" in the Shelly app      -> SOLARBANK_AC   the whole Solarbank AC circuit
    Shelly channel 1, em1:1, "Car charger" in the Shelly app  -> CAR            the car charger

On the Solarbank channel positive power is output to the house and negative is AC input into the Solarbank (charging from the
grid, or its own standby draw). `from_monitor` is the only place that knows the monitor's old names."""

SOLARBANK_AC = "solarbank_ac"
CAR = "car"
CHANNELS = (SOLARBANK_AC, CAR)                      # the position is the Shelly's own channel number: em1:0 and em1:1
MONITOR_NAMES = {"ch1": SOLARBANK_AC, "ch2": CAR}   # the original monitor's names, which count from 1


def from_monitor(sample):
    """The monitor's Shelly sample with its old key names (ch1, ch2 and their _voltage and _current variants) replaced by the role names.
    Keys already in role names, and every other key (such as "time"), are kept. A role name that is already present is never overwritten."""
    if not isinstance(sample, dict):
        return sample
    result = {key: value for key, value in sample.items() if key.partition("_")[0] not in MONITOR_NAMES}
    for key, value in sample.items():
        base, underscore, rest = key.partition("_")
        if base in MONITOR_NAMES:
            result.setdefault(MONITOR_NAMES[base] + underscore + rest, value)
    return result


def normalise_document(document):
    """A monitor-shaped readings document with the Shelly group's sample in role names. Anything else is returned unchanged."""
    group = document.get("shelly") if isinstance(document, dict) else None
    if isinstance(group, dict) and isinstance(group.get("sample"), dict):
        return {**document, "shelly": {**group, "sample": from_monitor(group["sample"])}}
    return document
