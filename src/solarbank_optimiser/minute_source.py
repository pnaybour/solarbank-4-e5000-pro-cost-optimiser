"""One-minute history from the telemetry files: the single store for minute detail (TC2, WP23b).

`MinuteSource` reads per-minute means and covered seconds. A minute inside the one-second archive is rebuilt from the
seconds; an older one comes from the one-minute archive. Both give the same answer: the mean is empty when fewer than
MIN_COVERED seconds were valid (the file's xFilesFactor of 0.5), and `covered` is the number of valid seconds.
`demand_rows` turns household-power minutes into the tagged rows the planner reads, using the settings history.
"""
from __future__ import annotations

import json
import os
import math
import time
from pathlib import Path
from typing import NamedTuple

import whisper

from .settings import DemandSelection
from .telemetry import COUNTED

MIN_COVERED = 30                  # a minute needs this many valid seconds to have a mean (same as daily_record.MIN_OK_SAMPLES)
MINUTE_KEEP_S = 89 * 86400          # a day inside the 90 days the minute archive holds, so a read never reaches the 30-minute archive
PROVENANCE = "shared-monitor-minute"


class Minute(NamedTuple):
    mean: float | None
    covered: int


def floor_minute(now):
    """The oldest minute a request can reach, aligned and a little inside the minute archive so that Whisper never falls back to the 30-minute archive."""
    return (int(now) - MINUTE_KEEP_S) // 60 * 60 + 120


def _aligned(start, end, now):
    first = max(int(start) // 60 * 60, floor_minute(now))          # a range reaching further back is clipped to what is kept, never dropped whole
    last = min(int(math.ceil(end / 60.0)) * 60, int(now) // 60 * 60 + 60)
    return first, last


class MinuteSource:
    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.clock = clock

    # ---- raw reads --------------------------------------------------------------------------------------------------------------------------
    def _fetch(self, name, first, last, now):
        path = self.root / name
        if not path.exists():
            return None
        if first < floor_minute(now):
            return None                  # minute detail is kept for 90 days (89 are read); older history is in the daily and half-hour records
        # Whisper returns the points after `from` up to and including `until`, so ask one minute early and one second short.
        return whisper.fetch(str(path), first - 60, last - 1, now=int(now))

    def _groups(self, name, first, last, now):
        """(step, {minute: [valid values]}) as Whisper returned them. Whether a minute is made of seconds (step 1) or is a stored one-minute point (step 60) is decided by the
        archive Whisper actually used for the request, never by a guess at its age."""
        got = self._fetch(name, first, last, now)
        if not got:
            return None, {}
        (t0, _t1, step), values = got
        if step not in (1, 60):
            raise ValueError("Unexpected archive step")
        out = {}
        for i, v in enumerate(values):
            if v is None:
                continue
            minute = (t0 + i * step) // 60 * 60
            if first <= minute < last:
                out.setdefault(minute, []).append(v)
        return step, out

    # ---- public -----------------------------------------------------------------------------------------------------------------------------
    def minutes(self, metric, start, end):
        """{minute_start: Minute(mean, covered)} for the minutes in [start, end) that have at least one valid second."""
        if metric not in COUNTED:
            raise ValueError(f"{metric} has no coverage count")
        now = self.clock()
        first, last = _aligned(start, end, now)
        if last <= first:
            return {}
        count_step, counts = self._groups(f"{metric}.count.wsp", first, last, now)
        value_step, values = self._groups(f"{metric}.wsp", first, last, now)
        out = {}
        for minute, seen in counts.items():
            n = int(round(sum(seen)))
            if n <= 0 or not start <= minute < end:
                continue
            got = values.get(minute)
            if not got:
                mean = None
            elif value_step == 1:
                mean = sum(got) / len(got) if len(got) >= MIN_COVERED else None
            else:
                mean = got[0]
            out[minute] = Minute(mean, min(n, 60))
        return out

    def means(self, metric, start, end):
        """{minute_start: (mean, covered_seconds or None)} for the minutes in [start, end) of ANY metric, for the history export: the coverage count exists only for the
        counted metrics, and a minute with fewer than MIN_COVERED valid seconds has no mean (it is left out)."""
        if metric in COUNTED:
            return {m: (x.mean, x.covered) for m, x in self.minutes(metric, start, end).items() if x.mean is not None}
        now = self.clock()
        first, last = _aligned(start, end, now)
        if last <= first:
            return {}
        step, groups = self._groups(f"{metric}.wsp", first, last, now)
        return {m: (sum(v) / len(v), None) for m, v in groups.items() if start <= m < end and (step != 1 or len(v) >= MIN_COVERED)}

    def last_known(self, metric, now, within=6 * 3600):
        """(time, value) of the newest valid reading of `metric` in the last `within` seconds, or None. When the newest data is made of seconds this is the last second
        that has a value, so a metric that stopped reporting is not given the time of one that did not; a one-minute point is stamped at the end of its minute. Used for the
        dashboard's "last known reading" when the live file in RAM is empty."""
        now = int(now)
        first, last = _aligned(now - within, now + 60, now)
        if last <= first:
            return None
        got = self._fetch(f"{metric}.wsp", first, last, now)
        if not got:
            return None
        (t0, _t1, step), values = got
        if step not in (1, 60):
            raise ValueError("Unexpected archive step")
        for i in range(len(values) - 1, -1, -1):
            if values[i] is not None:
                return float(min(t0 + i * step + (step if step == 60 else 0), now)), float(values[i])
        return None

    def covered_seconds(self, metric, start, end):
        """Valid seconds recorded for `metric` in [start, end), at any age: the count file at whatever resolution holds the range
        (one second, one minute, or 30 minutes beyond 90 days). `start` and `end` must be multiples of 30 minutes beyond 90 days."""
        if metric not in COUNTED:
            raise ValueError(f"{metric} has no coverage count")
        path = self.root / f"{metric}.count.wsp"
        now = int(self.clock())
        end = min(end, now + 1)
        if not path.exists() or end <= start:
            return 0.0
        got = whisper.fetch(str(path), int(start) - 1, int(end) - 1, now=now)
        if not got:
            return 0.0
        (t0, _t1, step), values = got
        return float(sum(v for i, v in enumerate(values) if v is not None and start <= t0 + i * step < end))

    def peaks(self, name, start, end):
        """{minute: maximum} from one of the `.max.wsp` files (for example `soc.max.wsp`)."""
        if not name.endswith(".max.wsp"):
            raise ValueError("Not a maximum file")
        now = self.clock()
        first, last = _aligned(start, end, now)
        if last <= first:
            return {}
        step, groups = self._groups(name, first, last, now)
        return {m: max(v) for m, v in groups.items() if start <= m < end}

    def usable(self, metric, start, end):
        """{minute: mean} for minutes with at least MIN_COVERED valid seconds: the daily record's own rule."""
        return {m: x.mean for m, x in self.minutes(metric, start, end).items() if x.mean is not None and x.covered >= MIN_COVERED}

    def house_without_car(self, start, end):
        """{minute: Minute(mean, covered)}: the household reading less the car charger, minute by minute (GitHub issue 74). See `house_without_car`."""
        return house_without_car(self.minutes("household_power", start, end), self.minutes("ev_power", start, end))


def house_without_car(house, car):
    """Household demand without the car (GitHub issue 74). On this installation `household_power` is the Solarbank's own load figure, which includes the car
    charger; the car is measured on its own (`ev_power`). A minute keeps the household reading's coverage and is the house less the car, never below zero.
    A minute whose car reading has no mean is left out: a missing car reading is never taken as "car off" (D15), so it is missing demand, not low demand."""
    out = {}
    for minute, h in house.items():
        c = car.get(minute)
        if h.mean is None or c is None or c.mean is None:
            continue
        out[minute] = Minute(max(0.0, h.mean - max(0.0, c.mean)), h.covered)
    return out


# ---- household-power rows for the planner ---------------------------------------------------------------------------------------------------
class DemandTimeline:
    """The settings history for the household-demand selection over a range: which mode and revision applied to a minute, and
    whether the selection changed inside it (such a minute is broken and never complete)."""

    def __init__(self, store, start, end):
        from contextlib import closing
        with closing(store.connect()) as db:
            before = db.execute("SELECT revision,known_at,payload FROM setting_versions WHERE name='demand' AND known_at<=? "
                                "ORDER BY known_at DESC,revision DESC LIMIT 1", (start,)).fetchall()
            inside = db.execute("SELECT revision,known_at,payload FROM setting_versions WHERE name='demand' AND known_at>? AND known_at<? "
                                "ORDER BY known_at,revision", (start, end)).fetchall()
        self.versions = [(r[1], r[0], json.loads(r[2])) for r in [*before, *inside]]
        self.versions.sort(key=lambda v: (v[0], v[1]))

    def _active(self, t):
        current = None
        for known_at, revision, selection in self.versions:
            if known_at <= t:
                current = (revision, selection)
            else:
                break
        return current if current else (0, None)

    def tag(self, minute):
        """(mode or None, revision, broken)."""
        revision, selection = self._active(minute)
        inner = [k for k, *_ in self.versions if minute < k < minute + 60]
        if selection and not selection.get("_history_unavailable"):
            sel = DemandSelection(**selection)
            mode = sel.active_mode(minute)
            edges = [t for t in (selection.get("start"), selection.get("end")) if t is not None and minute < t < minute + 60]
            if edges:
                inner.append(edges[0])
        else:
            mode = None if selection else "Normal consumption"
        return mode, revision, bool(inner)


ROW_CACHE_ENTRIES = 2        # this minute's rows and the last one: each list is about 6.5 MB, and the key changes every minute, so older ones are never asked for again (was 6: about 26 MB too many in each of the web and planner processes)
_ROW_CACHE = {}          # (database, whole minute, demand revision) -> rows: the same minute's answer is reused, whoever asks (the dashboard asks every few seconds)


def _row(minute, mean, mode, revision):
    """The dict `json.loads(encoded(asdict(Observation(...))))` gives for a complete household-power minute, built directly (a test keeps the two identical)."""
    return {"source": "monitor-minute:household_power", "metric": "household_power", "retrieved_at": minute + 60, "value": max(0.0, mean), "unit": "kW",
            "quality": "ok", "device_at": None, "raw_value": None, "raw_unit": None, "coverage_seconds": 60.0, "demand_mode": str(mode),
            "provenance": PROVENANCE, "interval_start": minute, "source_at": None, "demand_revision": revision,
            "window_samples": None, "window_ok": None, "window_mean": None, "window_max": None}


def demand_rows(store, source, start, end):
    """Complete (60 covered seconds), unbroken household-power minutes in [start, end), less the car (GitHub issue 74), as the row dicts the planner reads.
    Cached for the minute: building 11,000 rows took seconds on the Pi and the dashboard asked for them on every refresh (the dashboard showed "connection unavailable")."""
    revision = store.get_setting("demand")[0] if hasattr(store, "get_setting") else 0
    stamp = tuple(os.stat(source.root / name).st_mtime_ns if (source.root / name).exists() else 0
                  for name in ("household_power.wsp", "household_power.count.wsp", "ev_power.wsp", "ev_power.count.wsp"))       # new data refreshes it
    key = (str(getattr(store, "path", "")), int(end // 60), int(start // 60), revision, stamp)
    hit = _ROW_CACHE.get(key)
    if hit is not None:
        return hit
    means = source.house_without_car(start, end)
    timeline = DemandTimeline(store, start, end)
    rows = []
    for minute in sorted(means):
        item = means[minute]
        if item.covered < 60 or item.mean is None:
            continue
        mode, revision_at, broken = timeline.tag(minute)
        if broken or mode is None:
            continue
        rows.append(_row(minute, item.mean, mode, revision_at))
    if len(_ROW_CACHE) >= ROW_CACHE_ENTRIES:
        _ROW_CACHE.pop(next(iter(_ROW_CACHE)))
    _ROW_CACHE[key] = rows
    return rows
