"""Sunset for the installation, from the date alone (no network, no heavy imports). NOAA's general solar-position equations: good to about a minute in the UK, which is far
finer than the use needs (the evening window of the demand diagnostic, `demand_learning`)."""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

LATITUDE, LONGITUDE = 51.5074, -0.1278          # the installation (the same site the weather provider is asked about)
SUNSET_ZENITH = 90.833                              # the sun's upper edge on the horizon, allowing for refraction


def sunset_utc(day: date, latitude: float = LATITUDE, longitude: float = LONGITUDE) -> float | None:
    """Sunset as a UTC epoch for the calendar `day` at the site; None when the sun does not set that day."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1 + (18 - 12) / 24)                       # fractional year at about 18:00 UTC
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat = math.radians(latitude)
    cos_ha = math.cos(math.radians(SUNSET_ZENITH)) / (math.cos(lat) * math.cos(decl)) - math.tan(lat) * math.tan(decl)
    if not -1 <= cos_ha <= 1:
        return None
    hour_angle = math.degrees(math.acos(cos_ha))                           # positive in the evening
    minutes_utc = 720 - 4 * (longitude - hour_angle) - eqtime              # minutes after 00:00 UTC
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return midnight + minutes_utc * 60
