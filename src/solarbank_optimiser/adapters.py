"""Pure read-response decoders. No device transport or write methods.

Adapted from R01/R02/R03; historical excerpts remain unchanged.
"""

from .models import Observation, Quality, number


def registers(words, count):
    if not isinstance(words, (list, tuple)) or len(words) != count:
        raise ValueError("Short or malformed register reply")
    if any(type(word) is not int or not 0 <= word <= 65535 for word in words):
        raise ValueError("Expected unsigned 16-bit registers")
    return words


def signed(word, bits):
    number(word, "word", 0, (1 << bits) - 1)
    if type(word) is not int:
        raise ValueError("Word must be an integer")
    return word - (1 << bits) if word & (1 << (bits - 1)) else word


def power_kw(words):
    hi, lo = registers(words, 2)
    return signed((hi << 16) | lo, 32) / 1000


def counter_kwh(words):
    hi, lo = registers(words, 2)
    return ((hi << 16) | lo) / 10


def current_a(words):
    return signed(registers(words, 1)[0], 16) / 100


def shelly_observations(payload, retrieved_at):
    """Validate channels independently. Timestamp is retrieval, not device time."""
    results = []
    for channel, metric in ((0, "ac_power"), (1, "ev_power")):
        value = None
        quality = Quality.INVALID
        try:
            data = payload[f"em1:{channel}"]
            if not isinstance(data, dict) or data.get("errors"):
                raise ValueError("Channel errors")
            value = number(data["act_power"], "act_power")
            quality = Quality.OK
        except (KeyError, TypeError, ValueError):
            pass
        results.append(Observation(
            f"shelly:em1:{channel}", metric, retrieved_at,
            None if value is None else value / 1000, "kW", quality,
            raw_value=value, raw_unit="W", provenance="uncommissioned-channel-map",
        ))
    return results

