"""One-off, repeatable move of the SQLite one-minute rows into the telemetry files, with an agreement check (WP23b).

Single store (owner, 3 November): after the switch, minute detail lives only in the telemetry files. The command copies every
SQLite minute row (value, valid-second count and maximum) into them, fills only minutes the files do not already hold (the live
feed is the truth), never touches the one-second archive of a minute that has data, and then compares the two stores. A second
run changes nothing. The old SQLite rows are never altered or deleted.
"""
from __future__ import annotations

import json
import math
from contextlib import closing

import whisper

from .minute_source import MIN_COVERED, MinuteSource, floor_minute
from .telemetry import COUNTED, METRICS, ONE_SECOND_KEEP_S, Telemetry, files_for

YOUNG_S = ONE_SECOND_KEEP_S                 # Whisper puts a point into the one-second archive when it is no older than that archive's retention: such a minute is written second by second;
                                            # an older one goes straight into the minute archive as one point (the same comparison Whisper itself makes)
TOLERANCE = 1e-6
HOLD_FILL_ALLOWANCE_S = 10      # most extra valid seconds the live feed's hold rule can add to a minute (two gaps of up to 5 s)
EDGE_TOLERANCE = 0.01        # kW: one second at a minute's edge filed in the neighbouring minute, or a minute whose seconds differ because the live feed recorded only part of it (never for the battery level)
ABS_TOLERANCE = 0.002        # kW (or 0.2 % of battery level): the live feed holds a reading across a gap of up to 5 s, so a minute it recorded itself can differ from SQLite's by a watt or so


def sqlite_windows(store, metric, start, end):
    """{minute: (mean, ok_seconds, maximum)} from the collector's one-minute log rows for `metric`."""
    out = {}
    with closing(store.connect()) as db:
        cursor = db.execute("SELECT sample_at,payload FROM observations WHERE metric=? AND sample_at>=? AND sample_at<? "
                            "AND json_extract(payload,'$.window_mean') IS NOT NULL ORDER BY sample_at", (metric, start, end))
        for sample_at, payload in cursor:
            row = json.loads(payload)
            mean, ok = row.get("window_mean"), row.get("window_ok") or 0
            if row.get("quality") == "ok" and isinstance(mean, (int, float)) and math.isfinite(mean) and ok > 0:
                out[int(sample_at // 60 * 60)] = (float(mean), min(int(ok), 60), row.get("window_max"))
    return out


def _count_range(store):
    with closing(store.connect()) as db:
        row = db.execute("SELECT min(sample_at),max(sample_at) FROM observations WHERE json_extract(payload,'$.window_mean') IS NOT NULL").fetchone()
    return (None, None) if row[0] is None else (int(row[0]) // 60 * 60, int(row[1]) // 60 * 60 + 60)


def backfill(store, root, now, metrics=None, dry_run=False):
    """Copy SQLite minute rows into the telemetry files at `root`. Returns {metric: {"written": n, "skipped_existing": n, "skipped_old": n}}."""
    Telemetry(root)                                  # creates any missing file (fixed size)
    source = MinuteSource(root, clock=lambda: now)
    first, last = _count_range(store)
    report = {}
    if first is None:
        return report
    oldest = floor_minute(now)
    fine_from = int(now) - ONE_SECOND_KEEP_S + 300           # inside the one-second archive, where a partly recorded recent minute can still be seen
    for metric in (metrics or [m for m in METRICS if m != "garage_current"] + ["garage_current"]):
        rows = sqlite_windows(store, metric, first, last)
        counted = metric in COUNTED
        primary = f"{metric}.count.wsp" if counted else f"{metric}.wsp"
        present = set()                                   # minutes the files already hold, however little: the live feed is the truth
        if rows:
            present |= set(source._groups(primary, max(first, oldest), last, int(now))[1])
            if last > fine_from:                          # a recent minute with only some seconds has no consolidated average yet, so look at the seconds themselves
                present |= set(source._groups(primary, max(first, fine_from), last, int(now))[1])
        peak_files = [n for n, _a, _x in files_for(metric) if n.endswith(".max.wsp")]
        peak_present = {n: set(source._groups(n, max(first, oldest), last, int(now))[1]) for n in peak_files} if rows else {}
        done = {"written": 0, "skipped_existing": 0, "skipped_old": 0}
        value_points, count_points, max_points = [], [], {n: {} for n in peak_files}
        for minute, (mean, ok, peak) in sorted(rows.items()):
            if minute < oldest:
                done["skipped_old"] += 1            # older than the minute archive: the daily and half-hour records already hold it
                continue
            for name in peak_files:                 # a peak is filled in wherever it is missing, whether or not the value was
                if peak is not None and minute not in peak_present[name]:
                    max_points[name][minute] = float(peak)
            if minute in present:
                done["skipped_existing"] += 1
                continue
            if int(now) - minute <= YOUNG_S:
                value_points += [(minute + i, mean) for i in range(ok)]
                count_points += [(minute + i, 1.0) for i in range(ok)]
            else:
                value_points.append((minute, mean))
                count_points.append((minute, float(ok)))
            done["written"] += 1
        if not dry_run:
            for name, _aggregation, _xff in files_for(metric):
                path = str(source.root / name)
                if name == f"{metric}.wsp":
                    points = value_points
                elif name == f"{metric}.count.wsp":
                    points = count_points
                else:                                   # a `.max.wsp` file: one point per minute
                    points = sorted(max_points[name].items())
                if points:
                    whisper.update_many(path, points, now=int(now))
        report[metric] = done
    return report


def agreement(store, root, now, first=None, last=None):
    """Compare the SQLite minute rows with the telemetry files. Returns {"ok": bool, "metrics": {metric: {...}}}.

    A minute counts as usable when it has at least MIN_COVERED valid seconds (the daily record's rule). `missing` is a usable SQLite
    minute the files lack; `mean_differences` and `coverage_differences` are minutes in both whose mean or valid seconds differ."""
    source = MinuteSource(root, clock=lambda: now)
    f, l = _count_range(store)
    if f is None:
        return {"ok": True, "metrics": {}, "note": "no SQLite minute rows"}
    first = max(f, floor_minute(now)) if first is None else first
    last = l if last is None else last
    result, ok = {}, True
    for metric in COUNTED:
        rows = sqlite_windows(store, metric, first, last)
        files = source.minutes(metric, first, last)
        entry = {"compared": 0, "missing": 0, "mean_differences": 0, "coverage_differences": 0, "only_in_files": 0, "examples": []}
        for minute, (mean, n, _peak) in rows.items():
            if n < MIN_COVERED:
                continue
            got = files.get(minute)
            if got is None or got.mean is None:
                entry["missing"] += 1
                entry["examples"].append(("missing", minute))
                continue
            entry["compared"] += 1
            if got.covered != n:
                entry["coverage_differences"] += 1
                hold_filled = n < got.covered <= n + HOLD_FILL_ALLOWANCE_S                  # the live feed holds a reading across a gap of up to 5 s (TC1): a second SQLite called missing
                partial_live = got.covered < n and int(now) - minute <= YOUNG_S             # a minute the live feed recorded only in part (a restart inside it): the backfill never overwrites live seconds
                changes_eligibility = (n >= MIN_COVERED) != (got.covered >= MIN_COVERED) or (n == 60) != (got.covered == 60)        # a minute the planner or the record would count, or stop counting
                if changes_eligibility and (hold_filled or partial_live):
                    entry["explained_coverage_differences"] = entry.get("explained_coverage_differences", 0) + 1
                elif changes_eligibility:
                    entry["eligibility_changes"] = entry.get("eligibility_changes", 0) + 1
                entry["examples"].append(("coverage", minute, n, got.covered))
            gap = abs(got.mean - mean)
            if gap > max(ABS_TOLERANCE, TOLERANCE * max(1.0, abs(mean))):
                if metric != "soc" and gap <= EDGE_TOLERANCE:
                    entry["explained_mean_differences"] = entry.get("explained_mean_differences", 0) + 1       # edge second or partly recorded minute: a few watts, reported not failed
                else:
                    entry["mean_differences"] += 1
                entry["examples"].append(("mean", minute, mean, got.mean))
        entry["only_in_files"] = sum(1 for m, x in files.items() if m not in rows and (x.covered >= MIN_COVERED))
        entry["examples"] = entry["examples"][:5]
        if metric == "soc":
            entry["peak_differences"] = _peak_differences(store, root, now, first, last)
            if entry["peak_differences"]:
                ok = False
        result[metric] = entry
        if entry["missing"] or entry["mean_differences"] or entry.get("eligibility_changes"):
            ok = False
    for metric in METRICS:                       # the metrics without a coverage count are checked too (GitHub 53): nothing is removed from SQLite that the files were not compared for
        if metric in COUNTED:
            continue
        rows = sqlite_windows(store, metric, first, last)
        files = source.means(metric, first, last)
        entry = {"compared": 0, "missing": 0, "mean_differences": 0, "examples": []}
        for minute, (mean, n, peak) in rows.items():
            if n < MIN_COVERED:
                continue
            got = files.get(minute)
            if got is None:
                entry["missing"] += 1
                entry["examples"].append(("missing", minute))
                continue
            entry["compared"] += 1
            value = got[0]
            tolerance = max(ABS_TOLERANCE, TOLERANCE * max(1.0, abs(mean)))
            if METRICS[metric] == "max":         # a counter kept as the largest value of the minute: the file's value must lie between SQLite's mean and its maximum
                gap = max(0.0, mean - value, value - (peak if peak is not None else mean))
            else:
                gap = abs(value - mean)
            if gap > tolerance:
                if gap <= EDGE_TOLERANCE:
                    entry["explained_mean_differences"] = entry.get("explained_mean_differences", 0) + 1
                else:
                    entry["mean_differences"] += 1
                entry["examples"].append(("mean", minute, mean, value))
        entry["examples"] = entry["examples"][:5]
        result[metric] = entry
        if entry["missing"] or entry["mean_differences"]:
            ok = False
    return {"ok": ok, "metrics": result}


def _peak_differences(store, root, now, first, last):
    """The battery-level maximum per minute (used by the calibration check) against SQLite's window maximum."""
    source = MinuteSource(root, clock=lambda: now)
    rows = sqlite_windows(store, "soc", first, last)
    files = source.peaks("soc.max.wsp", first, last)
    return sum(1 for minute, (_m, _n, peak) in rows.items() if peak is not None and (minute not in files or abs(files[minute] - peak) > TOLERANCE))
