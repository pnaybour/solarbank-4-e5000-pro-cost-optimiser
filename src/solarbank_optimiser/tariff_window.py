"""The cheap-rate window (for example 23:30 to 05:30), taken from the Octopus product where it states one. Standard library only.

Octopus publishes the day and night RATES as machine-readable data but states the night WINDOW only in the product's description text
("... a super low electricity rate between 23:30 - 05:30 every night ..."). The provider worker reads that text, accepts it only when it names exactly one
unambiguous half-hour-aligned window, and saves it with the price cache. Everything that needs the window asks `effective_tariff`, which prefers it over the
window typed on the Settings page (kept as the fallback for when the product does not state one, or the lookup fails)."""
import re

_RANGE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})\s*(?:-|–|—|to|and|until)\s*(\d{1,2}):(\d{2})(?![\d:])", re.IGNORECASE)
MIN_HOURS, MAX_HOURS = 1, 12


def parse_night_window(text):
    """(start_minute, end_minute) of the one time range named in `text`, minutes after midnight London, or None when there is none, more than one, or it is implausible."""
    if not isinstance(text, str):
        return None
    found = set()
    for h1, m1, h2, m2 in _RANGE.findall(text):
        h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
        if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and m1 in (0, 30) and m2 in (0, 30)):
            continue
        found.add((h1 * 60 + m1, h2 * 60 + m2))
    if len(found) != 1:
        return None
    start, end = next(iter(found))
    length = (end - start) % 1440
    return (start, end) if MIN_HOURS * 60 <= length <= MAX_HOURS * 60 else None


def api_window(prices_cache, product=None):
    """{"start_minute","end_minute","text"} saved with the price cache, when it was read for this product and is well formed; else None."""
    window = (prices_cache or {}).get("night_window") if isinstance(prices_cache, dict) else None
    if not isinstance(window, dict):
        return None
    start, end = window.get("start_minute"), window.get("end_minute")
    if not all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x < 1440 for x in (start, end)) or start == end:
        return None
    if product is not None and window.get("product") != product:
        return None
    return window


def effective_tariff(tariff, prices_cache, product=None):
    """(tariff dict with the window from the Octopus product if there is one, where the window came from). `tariff` is the saved fallback-tariff payload or None."""
    if not isinstance(tariff, dict):
        return tariff, "none"
    window = api_window(prices_cache, product)
    if window is None:
        return tariff, "settings"
    return {**tariff, "start_minute": window["start_minute"], "end_minute": window["end_minute"]}, "octopus"


def unused_reason(prices_cache, product, usable_cache):
    """Why a window saved by the provider worker is NOT being used, or None when there is none or it is in use (#19). `usable_cache` is what services.window_cache returned."""
    if api_window(prices_cache, product) is None or usable_cache:
        return None
    return "An Octopus window is saved but not used: the price provider is off, or its data is older than a day or was built for another product or tariff"


def describe(tariff, source, saved=None, reason=None):
    clock = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    if not isinstance(tariff, dict) or tariff.get("start_minute") is None:
        return None
    where = {"octopus": "from the Octopus product",
             "settings": "as set on the Settings page" + ("" if reason else " (the Octopus product does not state one)")}.get(source, "")
    out = {"start_minute": tariff["start_minute"], "end_minute": tariff["end_minute"], "source": source,
           "text": f"{clock(tariff['start_minute'])} to {clock(tariff['end_minute'])} London, {where}".strip(", ")}
    if isinstance(saved, dict) and saved.get("start_minute") is not None:         # what is typed on the Settings page, shown beside the one in use
        out["saved"] = {"start_minute": saved["start_minute"], "end_minute": saved["end_minute"],
                        "text": f"{clock(saved['start_minute'])} to {clock(saved['end_minute'])} London"}
    if reason:
        out["note"] = reason
    return out
