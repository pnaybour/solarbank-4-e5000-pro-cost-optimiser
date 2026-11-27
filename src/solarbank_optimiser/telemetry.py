"""The fixed-size telemetry store (WP23a, contract TC1 and TC5): one Whisper file per metric, written alongside SQLite by the collector only. The controller never touches it.

Every file is created once at its final size and never grows: 1 s for 2 days, 1 min for 90 days and 30 min for 10 years (5.73 MB each). A year of 1 s writes leaves the size unchanged,
and a gap stays empty at every resolution (an empty point is "no data", never zero).

What is stored, per metric (TC1):
  * `<metric>.wsp`         the value, aggregated by `average` (powers, SoC, currents) or `max` (monotonic energy counters), xFilesFactor 0.5
  * `<metric>.count.wsp`   for every metric with a coverage consumer: a 1 at every second a valid value was written, aggregated by `sum` (xFilesFactor 0). Its one-minute point is the
                           number of valid seconds (0 to 60). Whisper keeps only the aggregate of an interval, so a minute built from 30 valid seconds has the same mean as a full one;
                           the count is what tells them apart. An invalid or missing reading writes nothing to either file.
  * `soc.max.wsp`          the SoC maximum per interval, for the calibration check
  * `household_power.max.wsp` and `garage_current.max.wsp`: the PEAK per interval. An average hides the peak inside it, and the garage limit and the load shape are about peaks.
  * `snapshots/`           short 1-second records (about 2 minutes before and after) around events worth tuning from, saved before they age out of the one-second archive:
                           the garage current going over 40 A, the car starting or stopping, a change of household mode. Bounded: the newest 200 are kept.

Late data (TC5): Whisper merges a late point into the coarser averages only while the point is still inside the one-second archive (2 days). A point older than that stores only its
last value and a count of 1, which would look like a complete, wrong minute. So a point older than the one-second archive less a one-hour margin is REFUSED, counted and reported;
only a repair command (not built here) may write older data, with complete values and counts together.

Writes are buffered in memory and flushed in batches with `update_many()`; a failed flush is logged, counted and retried, and never blocks the caller. The buffer is bounded."""

from collections import deque
import json
import logging
import math
import os
from pathlib import Path
import threading
import time

import whisper

whisper.LOCK = True                   # one writer per file at a time (the collector is the only writer; the lock also protects a repair command run beside it)

log = logging.getLogger(__name__)

ARCHIVES = ((1, 2 * 86400), (60, 90 * 1440), (1800, 10 * 365 * 48))        # (seconds per point, points): 2 days at 1 s, 90 days at 1 min, 10 years at 30 min
ONE_SECOND_KEEP_S = 2 * 86400
LATE_MARGIN_S = 3600
MAX_AGE_S = ONE_SECOND_KEEP_S - LATE_MARGIN_S
MAX_BUFFER = 200_000
SOC = "soc"
PEAKED = ("household_power", "garage_current")          # also keep the peak per interval
SNAPSHOT_BEFORE_S, SNAPSHOT_AFTER_S, SNAPSHOT_KEEP = 120, 120, 200
HOLD_GAP_S = 5          # a gap of up to this many seconds between two valid readings counts as covered, holding the earlier value (the rule the old minute recorder used)
GARAGE_TRIGGER_A, GARAGE_REARM_A = 40.0, 35.0
CAR_ON_KW, CAR_OFF_KW = 1.0, 0.3
# metric -> aggregation of its value file. Everything the collector records; the cumulative energy counters are monotonic, so their interval value is the maximum.
METRICS = {"soc": "average", "pv_power": "average", "household_power": "average", "grid_power": "average", "battery_power": "average",
           "charge_energy": "max", "discharge_energy": "max", "ac_power": "average", "ev_power": "average", "grid_ct_power": "average",
           "garage_power": "average", "garage_current": "average"}
COUNTED = ("soc", "battery_power", "grid_power", "pv_power", "household_power", "ev_power")      # the metrics with a coverage consumer (daily record, charger history)


def files_for(metric):
    """[(file name, aggregation, xFilesFactor)] for a metric."""
    out = [(f"{metric}.wsp", METRICS[metric], 0.5)]
    if metric in COUNTED:
        out.append((f"{metric}.count.wsp", "sum", 0.0))
    if metric == SOC:
        out.append(("soc.max.wsp", "max", 0.0))
    if metric in PEAKED:
        out.append((f"{metric}.max.wsp", "max", 0.0))
    return out


class Telemetry:
    SPARSE = False          # production files are written out in full at creation (fixed size on disk); the test suite sets this to keep its temporary files small

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._buffer = {}              # file name -> {second: value}
        self.dropped_late = self.dropped_overflow = self.write_errors = 0
        self.last_error = None
        self.last_flush_at = None
        self.written_points = 0
        self._ring = deque(maxlen=SNAPSHOT_BEFORE_S + 1)      # the last couple of minutes of every metric, one entry per second, for the snapshot pre-roll
        self._captures = []                                    # [{kind, at, end, rows}] still collecting their post-roll
        self._snapshots_ready = []
        self._last_valid = {}          # metric -> (second, value) of the newest valid reading, for the short-gap hold
        self._garage_armed, self._car_on = True, False
        self.snapshots_saved = 0
        (self.root / "snapshots").mkdir(exist_ok=True)
        for metric in METRICS:
            for name, aggregation, xff in files_for(metric):
                self._create(name, aggregation, xff)

    def _create(self, name, aggregation, xff):
        path = self.root / name
        if path.exists():
            return
        temporary = path.with_name(path.name + ".tmp")
        whisper.create(str(temporary), list(ARCHIVES), xFilesFactor=xff, aggregationMethod=aggregation, sparse=self.SPARSE)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)       # a file is either absent or complete, never half-created

    def path(self, name):
        return self.root / name

    def add(self, observations, now):
        """Buffer the VALID readings (quality OK, finite). A point older than the one-second archive less the margin is refused and counted. Never raises, never blocks on a file."""
        with self._lock:
            for o in observations:
                metric = o.metric
                if metric not in METRICS or getattr(o, "quality", "ok") != "ok":
                    continue
                value = o.value
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    continue
                second = int(o.sample_at)
                if now - second > MAX_AGE_S:
                    self.dropped_late += 1
                    continue
                row = self._row(second)
                row[metric] = float(value)
                previous = self._last_valid.get(metric)
                if previous is not None and 1 < second - previous[0] <= HOLD_GAP_S and metric in COUNTED:
                    for held in range(previous[0] + 1, second):          # the device refreshes less often than once a second: the seconds between are covered
                        for name, _aggregation, _xff in files_for(metric):
                            self._buffer.setdefault(name, {})[held] = 1.0 if name.endswith(".count.wsp") else (previous[1] if name == f"{metric}.wsp" else None)
                            if self._buffer[name][held] is None:
                                del self._buffer[name][held]
                if previous is None or second > previous[0]:
                    self._last_valid[metric] = (second, float(value))
                for name, _aggregation, _xff in files_for(metric):
                    if name.endswith(".count.wsp"):
                        point = 1.0
                    else:
                        point = float(value)
                    self._buffer.setdefault(name, {})[second] = point
                if sum(len(v) for v in self._buffer.values()) > MAX_BUFFER:
                    self._shed()
            self._events(now)

    def _row(self, second):
        """The one-second row for `second` in the ring (created in order; late arrivals for a recent second join their row)."""
        if self._ring and self._ring[-1][0] == second:
            return self._ring[-1][1]
        for entry in reversed(self._ring):
            if entry[0] == second:
                return entry[1]
        row = {}
        self._ring.append((second, row))
        for capture in self._captures:
            capture["rows"].append((second, row))
        return row

    def _events(self, now):
        """Edge triggers on the newest second (garage current over 40 A, the car starting or stopping). Called with the lock held."""
        if not self._ring:
            return
        second, row = self._ring[-1]
        garage, car = row.get("garage_current"), row.get("ev_power")
        if garage is not None:
            if self._garage_armed and garage > GARAGE_TRIGGER_A:
                self._start_capture("garage_over_40a", second); self._garage_armed = False
            elif garage < GARAGE_REARM_A:
                self._garage_armed = True
        if car is not None:
            if not self._car_on and car > CAR_ON_KW:
                self._start_capture("car_start", second); self._car_on = True
            elif self._car_on and car < CAR_OFF_KW:
                self._start_capture("car_stop", second); self._car_on = False
        for capture in [c for c in self._captures if second >= c["end"]]:
            self._captures.remove(capture)
            self._snapshots_ready.append(capture)

    def note_event(self, kind, at):
        """An event the caller knows about (a change of household mode): save a snapshot around it."""
        with self._lock:
            self._start_capture(kind, int(at))

    def _start_capture(self, kind, second):
        if any(c["kind"] == kind and second - c["at"] < SNAPSHOT_AFTER_S for c in self._captures):
            return                                             # one capture per kind at a time
        rows = [(s, r) for s, r in self._ring if s >= second - SNAPSHOT_BEFORE_S]
        self._captures.append({"kind": kind, "at": second, "end": second + SNAPSHOT_AFTER_S, "rows": rows})

    def _shed(self):
        """Over the bound (the disk has been away for a long time): drop the oldest seconds, counted."""
        for points in self._buffer.values():
            if len(points) > MAX_BUFFER // (len(self._buffer) or 1):
                for second in sorted(points)[: len(points) // 4]:
                    del points[second]
                    self.dropped_overflow += 1

    def pending(self):
        with self._lock:
            return sum(len(v) for v in self._buffer.values())

    def flush(self, now):
        """Write everything buffered with update_many(). A file that fails keeps its points for the next flush; the others are written. Returns the number of points written."""
        with self._lock:
            work, self._buffer = self._buffer, {}
        written, failed = 0, {}
        for name, points in work.items():
            ordered = sorted(points.items())
            try:
                whisper.update_many(str(self.root / name), ordered, now=int(now))
                written += len(ordered)
            except (OSError, whisper.WhisperException, ValueError) as error:
                self.write_errors += 1
                self.last_error = f"{name}: {error.__class__.__name__}"
                log.warning("telemetry: could not write %s (%s); will retry", name, error)
                failed[name] = points
        if failed:
            with self._lock:
                for name, points in failed.items():
                    merged = self._buffer.setdefault(name, {})
                    for second, value in points.items():
                        merged.setdefault(second, value)           # anything newer for the same second wins
        self._save_snapshots()
        self.written_points += written
        self.last_flush_at = now
        return written

    def _save_snapshots(self):
        with self._lock:
            ready, self._snapshots_ready = self._snapshots_ready, []
        for capture in ready:
            name = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(capture['at']))}-{capture['kind']}.jsonl"
            path = self.root / "snapshots" / name
            try:
                temporary = path.with_name(name + ".tmp")
                with open(temporary, "w") as handle:
                    for second, row in sorted(capture["rows"], key=lambda item: item[0]):
                        handle.write(json.dumps({"t": second, **row}, separators=(",", ":")) + "\n")
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
                self.snapshots_saved += 1
            except OSError as error:
                self.write_errors += 1
                self.last_error = f"snapshot: {error.__class__.__name__}"
        files = sorted((self.root / "snapshots").glob("*.jsonl"))
        for old in files[:-SNAPSHOT_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass

    def status(self):
        return {"files": sum(len(files_for(m)) for m in METRICS), "pending_points": self.pending(), "written_points": self.written_points,
                "dropped_late": self.dropped_late, "dropped_overflow": self.dropped_overflow, "write_errors": self.write_errors, "last_error": self.last_error,
                "snapshots_saved": self.snapshots_saved, "snapshots_pending": len(self._captures) + len(self._snapshots_ready)}

    # ---- reading (used by tests now, by minute_source in WP23b) -------------------------------------------------------------------
    def fetch(self, name, start, end, now=None):
        """((start, end, step), values) from the best archive for the range, as Whisper returns it (None where there is no data)."""
        return whisper.fetch(str(self.root / name), int(start), int(end), now=None if now is None else int(now))
