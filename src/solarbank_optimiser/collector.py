"""Observation-only bridge to the existing recorder during development.

This transport never connects to Modbus or Home Assistant. It is deliberately a
temporary observation adapter, not the final independent equipment interface.
"""

from dataclasses import dataclass, replace
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from urllib.parse import urlsplit

from .ev_policy import EV_OFF_DWELL_S, EV_ON_DWELL_S
from .charger import ChargerTracker
from .control import SingleOwner
from .models import Clock, Observation, Quality, UNITS, number
from .shelly_channels import CAR, SOLARBANK_AC, normalise_document
from .settings import DemandSelection
from .minutelog import MemoryCharger, MinuteLog, MinuteSink

PROVENANCE = "shared-monitor-observation"

# Names and units were checked against the approved live /api/readings payload.
# No CT amps exist there. Never substitute Shelly circuit amps or W/230 for it.
FIELDS = (
    ("solarbank", "soc", "soc", "percent", .01),
    ("solarbank", "pv", "pv_power", "W", .001),
    ("solarbank", "load", "household_power", "W", .001),
    ("solarbank", "grid", "grid_power", "W", .001),
    ("solarbank", "battery", "battery_power", "W", .001),
    ("solarbank", "charge_total_kwh", "charge_energy", "kWh", 1.),
    ("solarbank", "discharge_total_kwh", "discharge_energy", "kWh", 1.),
    ("shelly", SOLARBANK_AC, "ac_power", "W", .001),      # Shelly channel 0, em1:0: positive is output to the house, negative is AC input
    ("shelly", CAR, "ev_power", "W", .001),               # Shelly channel 1, em1:1
    ("anker", "main", "grid_ct_power", "W", .001),
    ("anker", "sub", "garage_power", "W", .001),
    ("anker", "garage_current_a", "garage_current", "A", 1.),     # the secondary CT's largest phase current, a magnitude
)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate response key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("Non-finite JSON value")

    data = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def decode_snapshot(data, acquired_at, max_age=5):
    number(acquired_at, "acquired_at", 0)
    data = normalise_document(data)           # the original monitor's Shelly names (ch1, ch2) become role names here and nowhere else
    result = []
    for group, field, metric, raw_unit, factor in FIELDS:
        source_at = raw = value = None
        quality = Quality.INVALID
        container = data if group == "anker" else data.get(group)
        try:
            if not isinstance(container, dict) or container.get("ok") is not True:
                quality = Quality.ERROR
                raise ValueError("Source unavailable")
            sample = container["sample"]
            if not isinstance(sample, dict):
                raise ValueError("Invalid sample")
            source_at = number(sample["time"], "upstream retrieval", 0, acquired_at)
            raw = number(sample[field], field)
            value = raw * factor
            quality = Quality.OK if acquired_at - source_at <= max_age else Quality.MISSING
            if metric in ("ev_power", "household_power", "pv_power") and value < 0:
                quality = Quality.INVALID
            if metric == "soc" and not 0 <= value <= 1:
                quality = Quality.INVALID
            if metric == "garage_current" and not 0 <= value <= 200:
                quality = Quality.INVALID
            if metric in ("charge_energy", "discharge_energy") and value < 0:
                quality = Quality.INVALID
        except (KeyError, TypeError, ValueError) as problem:
            if quality == Quality.OK:
                quality = Quality.INVALID
            elif isinstance(problem, KeyError) and metric == "garage_current":
                quality = Quality.MISSING                    # the source simply does not provide it (for example the meter feed is down)
        result.append(Observation(
            f"monitor:{group}:{field}", metric, acquired_at, value if quality == Quality.OK else None,
            UNITS[metric], quality, raw_value=raw, raw_unit=raw_unit,
            source_at=source_at, provenance=PROVENANCE,
        ))
    return result


@dataclass(frozen=True)
class CollectorConfig:
    endpoint: str = "http://127.0.0.1:8080/api/readings"
    timeout_seconds: float = 2
    interval_seconds: float = 1
    max_bytes: int = 1_048_576
    max_age_seconds: float = 5
    confirmation_seconds: float = EV_ON_DWELL_S
    release_seconds: float = EV_OFF_DWELL_S         # the same delay the controller uses (v1.3: 120 s below 300 W)
    max_gap_seconds: float = 5
    raw_retention_days: float = 7
    # Seconds to hold readings in memory and write them in one commit. 0 commits
    # every cycle. The shadow planner rejects a battery reading older than 5 s,
    # so keep this at about 2 s or less while the planner runs from this database.
    flush_interval_seconds: float = 0
    max_pending: int = 3600
    max_pending_bytes: int = 33_554_432
    # Every in-memory queue is bounded; anything shed is counted and described in the collector health (and so stored with it).
    max_pending_rows: int = 10_000          # minute summaries waiting for the database (about a week)
    max_charger_rows: int = 200_000         # in-memory charger samples / energy pieces (about two days)
    max_keep_per_minute: int = 30           # status-change evidence kept per minute
    prune_interval_seconds: float = 600
    # 1: every one-second reading is stored. 60: one summary row per minute is stored
    # (last value, count, mean, maximum) while live readings and charger tracking stay
    # one-second in memory and the live file. Use 60 only with a live file in place.
    record_seconds: int = 1
    # True: the database commit and the raw-snapshot pruning run in background threads, so a locked or
    # slow database can never delay the one-second tick or the live file. The service turns this on;
    # the default keeps single-threaded behaviour for tests and one-off captures.
    background_flush: bool = False
    # WP23a: the fixed-size telemetry store (Whisper files) written alongside SQLite. None leaves it off. Its flush runs off the tick like the database one and a failure never blocks.
    telemetry_dir: str = None
    telemetry_flush_seconds: float = 20

    def __post_init__(self):
        parts = urlsplit(self.endpoint)
        if (parts.scheme != "http" or not parts.hostname or parts.username or parts.password
                or parts.path != "/api/readings" or parts.query or parts.fragment):
            raise ValueError("Use an explicit HTTP monitor /api/readings endpoint without credentials")
        for name in ("timeout_seconds", "interval_seconds", "max_age_seconds", "max_gap_seconds", "raw_retention_days"):
            if number(getattr(self, name), name, 0) == 0:
                raise ValueError(f"{name} must be positive")
        for name in ("confirmation_seconds", "release_seconds"):
            number(getattr(self, name), name, 0)
        number(self.flush_interval_seconds, "flush_interval_seconds", 0, 300)
        number(self.prune_interval_seconds, "prune_interval_seconds", 1)
        if self.record_seconds not in (1, 60):
            raise ValueError("record_seconds must be 1 or 60")
        for name in ("max_pending", "max_pending_bytes", "max_pending_rows", "max_charger_rows", "max_keep_per_minute"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Pending-data limits must be positive integers")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 10_485_760:
            raise ValueError("Response bound must be 1 byte to 10 MiB")


def fetch_snapshot(config, connection_factory=http.client.HTTPConnection):
    """GET one fixed read-only endpoint. Never follow redirects or retry here."""
    parts = urlsplit(config.endpoint)
    connection = connection_factory(parts.hostname, parts.port or 80, timeout=config.timeout_seconds)
    try:
        connection.request("GET", "/api/readings", headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Monitor HTTP response was not successful")
        raw = response.read(config.max_bytes + 1)
        if len(raw) > config.max_bytes:
            raise ValueError("Monitor response exceeds configured size limit")
        return raw
    finally:
        connection.close()


class Collector:
    def __init__(self, store, config, clock=None, fetch=None, acquirer=None):
        self.store, self.config = store, config
        self.acquirer = acquirer          # direct acquirer, if any: supplies fetch and device health
        self.clock = clock or Clock()
        self.fetch = fetch or (acquirer.fetch if acquirer is not None else fetch_snapshot)
        self.failures = 0
        self._lock = threading.RLock()            # guards the in-memory queues shared with the flush thread
        self._logged = {}
        self.discarded_total, self.discarded_recent = {}, []     # what was shed because the database stayed away (see _note_discard)
        if config.record_seconds == 60:
            self.minute_log = MinuteLog(config.max_keep_per_minute, self._note_discard)
            self.sink = MinuteSink(store, config.max_pending_rows, self._note_discard)
            self.tracker = MemoryCharger(store, config.confirmation_seconds, config.release_seconds, config.max_gap_seconds,
                                         config.max_charger_rows, self._note_discard)
            self.minutes = None          # household and solar minute means come from the telemetry files (WP23b); nothing is recorded here
        else:
            # Per-second storage. Completed-minute averages still go through the in-memory sink and are committed with the batch:
            # the tick itself must never write to (or wait on) the database.
            self.minute_log = None
            self.sink = MinuteSink(store, config.max_pending_rows, self._note_discard)
            self.tracker = ChargerTracker(store, config.confirmation_seconds, config.release_seconds, config.max_gap_seconds)
            self.minutes = None          # household and solar minute means come from the telemetry files (WP23b); nothing is recorded here
        # The telemetry files are the only home of minute history (WP23b), so the collector cannot run without them: the directory defaults to the one the readers use
        # (next to the database), and a failure to open it stops the collector, which systemd restarts and the alert path reports, instead of silently recording nothing.
        from .telemetry import Telemetry
        directory = config.telemetry_dir or store.telemetry_dir
        if config.telemetry_dir:
            store.telemetry_dir = Path(config.telemetry_dir)
        self.telemetry = Telemetry(directory)
        self._telemetry_flusher, self._telemetry_flushed, self._last_mode = None, None, None
        self.next_prune = 0
        self.pending = []
        self.pending_bytes = 0
        self.last_flush = None
        self.storage_errors = 0
        self.tick_errors = 0
        self.dropped = 0
        self._flusher = self._pruner = None
        self._background_error = None
        self.last_storage_error_at = None

    MAX_CONSECUTIVE_TICK_ERRORS = 120      # about two minutes of nothing but failures

    def once(self):
        if self._background_error is not None:         # a background commit hit a non-database fault: surface it here
            error, self._background_error = self._background_error, None
            raise error
        started = self.clock.monotonic()
        raw = b""
        try:
            raw = self.fetch(self.config)
            data = strict_json(raw)
            acquired = self.clock.utc()
            observations = decode_snapshot(data, acquired, self.config.max_age_seconds)
            good = sum(o.quality == Quality.OK for o in observations)
            status = "ok" if good == len(FIELDS) else "partial" if good else "unavailable"
            reason = None if good else "No valid fresh source measurements"
            self.failures = 0
        except (OSError, ValueError, http.client.HTTPException):
            acquired = self.clock.utc()
            observations = decode_snapshot({}, acquired, self.config.max_age_seconds)
            status, reason = "unavailable", "Monitor request or response invalid"
            self.failures += 1
        tagged = []
        for o in observations:
            if o.metric == "household_power":
                revision, selection = self.store.setting_at("demand", o.sample_at)
                o = replace(o, demand_mode=DemandSelection(**selection).active_mode(o.sample_at)
                            if selection and not selection.get("_history_unavailable") else
                            None if selection else "Normal consumption", demand_revision=revision)
            tagged.append(o)
        observations = tagged
        if self.telemetry is not None:
            mode = next((o.demand_mode for o in observations if o.metric == "household_power" and o.demand_mode), None)
            if mode is not None and self._last_mode is not None and mode != self._last_mode:
                self.telemetry.note_event("mode_change", acquired)          # a snapshot of the seconds around a change of household mode
            self._last_mode = mode if mode is not None else self._last_mode
            self.telemetry.add(observations, acquired)
            if self._telemetry_flushed is None or self.clock.monotonic() - self._telemetry_flushed >= self.config.telemetry_flush_seconds:
                self._telemetry_flushed = self.clock.monotonic()
                self._run_maybe_background("_telemetry_flusher", lambda: self.telemetry.flush(self.clock.utc()))

        health = {"status": status, "reason": reason, "acquired_at": acquired,
                  "duration_seconds": max(0, self.clock.monotonic() - started),
                  "failure_count": self.failures, "mode": "observe", "equipment_writes": False,
                  "source": self._source_text(),
                  "garage_current": "From the smart meter via the controller; unavailable until read", "confirmation_seconds": self.config.confirmation_seconds,
                  "release_seconds": self.config.release_seconds, "max_gap_seconds": self.config.max_gap_seconds,
                  "flush_interval_seconds": self.config.flush_interval_seconds,
                  "storage_errors": self.storage_errors, "tick_errors": self.tick_errors, "last_storage_error_at": self.last_storage_error_at, "dropped_snapshots": self.dropped,
                  "discarded_total": dict(self.discarded_total), "discarded_recent": list(self.discarded_recent)}
        if self.telemetry is not None:
            health["telemetry"] = self.telemetry.status()
        if self.acquirer is not None:
            health["direct_devices"] = list(self.acquirer.config.devices)
            health["devices"] = self.acquirer.health()
        if self.minute_log is not None:
            # Live charger state is computed every second in memory and published with the readings.
            self.tracker.observe(next(o for o in observations if o.metric == "ev_power"))
            self.store.publish_live(observations, {"collector_health": health, "charger_tracker": self.tracker.state()})
            with self._lock:
                self.minute_log.add(raw, observations, acquired, health)
                self._bound_minutes()
        else:
            self.store.publish_live(observations, {"collector_health": health})
            with self._lock:
                self._enqueue(raw, observations, acquired, health)
        with self._lock:
            if self.minutes is not None:
                for observation in observations:
                    self.minutes.observe(observation)
        if (self.last_flush is None or self.clock.monotonic() - self.last_flush >= self.config.flush_interval_seconds):
            self._run_maybe_background("_flusher", self.flush)
        if acquired >= self.next_prune:
            self.next_prune = acquired + self.config.prune_interval_seconds
            self._run_maybe_background("_pruner", lambda: self.store.prune_raw(acquired - self.config.raw_retention_days * 86400))
        return health

    def _run_maybe_background(self, slot, work):
        """Run `work` now, or (background_flush) in one thread per slot; a still-running one is not started twice."""
        if not self.config.background_flush:
            work()
            return
        running = getattr(self, slot)
        if running is not None and running.is_alive():
            return

        def guarded():
            try:
                work()
            except sqlite3.Error as exc:
                self._storage_error()
                self._log_throttled("background", f"Collector: background database work failed, will retry: {exc}")
            except Exception as exc:            # a non-database fault: raise it from the next tick, as the single-threaded path would
                self._background_error = exc
        thread = threading.Thread(target=guarded, name="db" + slot, daemon=True)
        setattr(self, slot, thread)
        thread.start()

    def _source_text(self):
        direct = self.acquirer.config.devices if self.acquirer is not None else ()
        if not direct:
            return "Existing monitor bridge; device identity and update times unverified"
        if len(direct) == 3:
            return "Direct read-only device readers (Solarbank, smart meter, Shelly); device identity and update times unverified"
        return "Mixed: direct read-only readers for " + ", ".join(direct) + "; the rest from the existing monitor; identity unverified"

    def _storage_error(self):
        self.storage_errors += 1
        self.last_storage_error_at = self.clock.utc()

    def _log_throttled(self, key, message):
        """At most one line per `key` a minute: a long outage must not flood the journal (which is itself disk writing)."""
        now = time.monotonic()
        with self._lock:
            if now - self._logged.get(key, -1e9) < 60:
                return
            self._logged[key] = now
        print(message, file=sys.stderr, flush=True)

    def _note_discard(self, kind, count, first=None, last=None):
        """Record data shed to keep memory bounded: cumulative totals plus the latest few with their time ranges.
        The health record carrying this is stored with every snapshot, so the lost coverage is on record once the database returns."""
        with self._lock:
            self.discarded_total[kind] = self.discarded_total.get(kind, 0) + count
            self.discarded_recent.append({"kind": kind, "count": count, "from": first, "to": last, "at": self.clock.utc()})
            del self.discarded_recent[:-20]
            if kind in ("minute_summaries", "cycles"):
                self.dropped += count
        self._log_throttled("discard:" + kind, f"Collector: memory limit reached while the database is away; discarding {kind} "
                            f"(total so far {self.discarded_total[kind]}; latest {first} to {last}).")

    def _enqueue(self, raw, observations, acquired, health):
        self.pending.append((raw, observations, acquired, health))
        self.pending_bytes += len(raw)
        # Bounded memory: if the database stays unavailable, shed the oldest cycles.
        while len(self.pending) > 1 and (len(self.pending) > self.config.max_pending
                                         or self.pending_bytes > self.config.max_pending_bytes):
            lost = self.pending.pop(0)
            self.pending_bytes -= len(lost[0])
            self._note_discard("cycles", 1, lost[2], lost[2])

    def _remove_saved(self, saved):
        """Drop committed cycles by identity. The queue may have been trimmed at the front, or grown at the back, while the
        database write was in progress, so positions counted earlier no longer mean the same entries."""
        gone = {id(entry) for entry in saved}
        kept = [entry for entry in self.pending if id(entry) not in gone]
        self.pending_bytes -= sum(len(entry[0]) for entry in self.pending if id(entry) in gone)
        self.pending[:] = kept

    def _bound_minutes(self):
        limit = max(2, self.config.max_pending // 60 + 1)
        while len(self.minute_log.buckets) > limit:
            oldest = min(self.minute_log.buckets)
            self.minute_log.discard([oldest])
            self._note_discard("minute_summaries", 1, oldest, oldest + 60)

    def _flush_minutes(self, final):
        with self._lock:                         # take a copy; the database wait below must not hold this lock
            minutes, entries = self.minute_log.entries(self.clock.utc(), final)
            extra = list(self.sink.rows)
        try:
            self.store.record_snapshots(entries, self.tracker, extra_observations=extra, keep_observations=False)
        except sqlite3.Error as exc:
            self._storage_error()
            self._log_throttled("write", f"Collector: database write failed, {len(minutes)} minute(s) kept for retry: {exc}")
            return 0
        with self._lock:
            self.minute_log.discard(minutes)
            self.sink.remove(extra)
            self.last_flush = self.clock.monotonic()
        return len(entries)

    def flush(self, final=False):
        """Commit every pending cycle (and its charger tracking) in one transaction.

        A database error leaves the readings queued for the next attempt; the
        commit is all-or-nothing and content-keyed, so a retry cannot duplicate.
        Returns the number of cycles written.
        """
        if self.minute_log is not None:
            return self._flush_minutes(final)
        with self._lock:
            batch = list(self.pending)
            extra = list(self.sink.rows)
        if not batch and not extra:
            return 0
        try:
            self.store.record_snapshots(batch, self.tracker, extra_observations=extra, keep_observations=False)
        except sqlite3.Error as exc:
            self._storage_error()
            self._log_throttled("write", f"Collector: database write failed, {len(batch)} cycle(s) kept for retry: {exc}")
            return 0
        except ValueError as problem:
            # One unusable entry must not block the rest: save each cycle alone,
            # keeping the raw evidence of the bad one, then report the problem.
            saved_rows = extra
            for entry in batch:
                try:
                    self.store.record_snapshots([entry], self.tracker, extra_observations=extra, keep_observations=False)
                except ValueError:
                    self.store.record_snapshots([entry], extra_observations=extra, keep_observations=False)
                extra = []                                  # the minute rows go in once, with the first entry
                with self._lock:
                    self._remove_saved([entry])
            with self._lock:
                self.sink.remove(saved_rows)
            self.last_flush = self.clock.monotonic()
            raise problem
        with self._lock:
            self._remove_saved(batch)
            self.sink.remove(extra)
            self.last_flush = self.clock.monotonic()
        return len(batch)

    def run(self, stop, count=None, before=None, after=None):
        """Run the collection loop as the only owner of this installation.

        `before` (for example starting the device readers) is called only AFTER the exclusive lock is held, so a second,
        accidental collector is refused without having contacted any equipment; `after` always runs on the way out."""
        if count is not None and (type(count) is not int or count < 1):
            raise ValueError("Count must be a positive integer")
        lock = self.store.path.resolve().with_suffix(".collector.lock")
        with SingleOwner(lock):
            attempts = 0
            try:
                if before is not None:
                    before()
                consecutive = 0
                while not stop.is_set() and (count is None or attempts < count):
                    started = self.clock.monotonic()
                    try:
                        self.once()
                        consecutive = 0
                    except Exception as error:          # WP5 (#9): one bad tick must not end the collector; it is logged, counted and the next tick tries again
                        consecutive += 1
                        self.tick_errors += 1
                        self._log_throttled("tick", f"Collector: a tick failed ({error.__class__.__name__}: {error}); continuing")
                        if consecutive >= self.MAX_CONSECUTIVE_TICK_ERRORS:
                            raise                        # persistently broken: stop, so systemd restarts it and the alert check notices
                    attempts += 1
                    if count is None or attempts < count:
                        # Stop-event wait allows a prompt systemd/terminal shutdown.
                        stop.wait(max(0, self.config.interval_seconds - (self.clock.monotonic() - started)))
            finally:
                # Graceful stop (SIGTERM/SIGINT/error): let background work finish, then leave nothing buffered.
                for slot in ("_flusher", "_pruner"):
                    thread = getattr(self, slot)
                    if thread is not None:
                        thread.join(15)
                try:
                    self.flush(final=True)
                except sqlite3.Error:
                    pass
                if after is not None:
                    after()
