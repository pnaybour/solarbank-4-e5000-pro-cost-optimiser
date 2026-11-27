"""Versioned SQLite storage, consistent backup and immutable evidence."""

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from .models import Observation, Quality, UNITS, encoded, fingerprint, number, time_keyed

APPLICATION_ID = 0x53424F50
SCHEMA_VERSION = 4

MIGRATION_2 = (
    "CREATE TABLE collector_snapshots (id TEXT PRIMARY KEY, acquired_at REAL NOT NULL, status TEXT NOT NULL, raw BLOB NOT NULL)",
    "CREATE INDEX collector_snapshot_time ON collector_snapshots(acquired_at)",
    "CREATE INDEX observation_provenance ON observations(json_extract(payload,'$.provenance'),metric,json_extract(payload,'$.retrieved_at') DESC)",
    "CREATE TABLE charger_coverage (start REAL NOT NULL, end REAL NOT NULL, PRIMARY KEY(start,end))",
)

MIGRATION_3 = (
    "CREATE TABLE setting_versions (name TEXT NOT NULL, revision INTEGER NOT NULL, known_at REAL NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(name,revision))",
    "CREATE INDEX setting_chronology ON setting_versions(name,known_at,revision)",
    "CREATE TRIGGER setting_versions_no_update BEFORE UPDATE ON setting_versions BEGIN SELECT RAISE(ABORT,'immutable setting version'); END",
    "CREATE TRIGGER setting_versions_no_delete BEFORE DELETE ON setting_versions BEGIN SELECT RAISE(ABORT,'immutable setting version'); END",
)

MIGRATION_4 = (
    # WP3 (#12): events are filtered by kind and time (settings history, the purge of collector_snapshot events); without this every purge batch scanned the table.
    "CREATE INDEX events_kind_time ON events(kind,time)",
)

SCHEMA = (
    "CREATE TABLE observations (id TEXT PRIMARY KEY, source TEXT NOT NULL, metric TEXT NOT NULL, sample_at REAL NOT NULL, payload TEXT NOT NULL)",
    "CREATE INDEX observations_lookup ON observations(metric,sample_at)",
    "CREATE TABLE settings (name TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE events (id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE issuances (id TEXT PRIMARY KEY, acquired_at REAL NOT NULL, payload TEXT NOT NULL, raw BLOB NOT NULL)",
    "CREATE TRIGGER issuances_no_update BEFORE UPDATE ON issuances BEGIN SELECT RAISE(ABORT,'immutable issuance'); END",
    "CREATE TRIGGER issuances_no_delete BEFORE DELETE ON issuances BEGIN SELECT RAISE(ABORT,'immutable issuance'); END",
    "CREATE TABLE imports (sha256 TEXT PRIMARY KEY, payload TEXT NOT NULL)",
    "CREATE TABLE charger_sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, payload TEXT NOT NULL)",
    "CREATE TABLE charger_samples (id TEXT PRIMARY KEY, time REAL NOT NULL, power_kw REAL, state TEXT NOT NULL)",
    "CREATE TABLE charger_energy (start REAL NOT NULL, end REAL NOT NULL, session_id TEXT NOT NULL REFERENCES charger_sessions(id), kwh REAL NOT NULL, active_seconds REAL NOT NULL, PRIMARY KEY(start,end,session_id))",
)


@dataclass(frozen=True)
class StorageTuning:
    """Disk-activity settings applied to every connection (see OPERATIONS.md).

    synchronous NORMAL in WAL mode keeps the database intact after a crash or
    power cut; at worst the last few commits are lost. FULL also syncs the log at
    every commit. Never OFF: that can corrupt the file on power loss.
    """
    synchronous: str = "NORMAL"
    wal_autocheckpoint_pages: int = 4000     # about 16 MB of log before copying to the main file
    journal_size_limit_bytes: int = 67_108_864
    hold_open: bool = True                   # stops SQLite checkpointing and deleting the log whenever a short-lived connection closes

    def __post_init__(self):
        if self.synchronous not in ("NORMAL", "FULL"):
            raise ValueError("synchronous must be NORMAL or FULL")
        if type(self.wal_autocheckpoint_pages) is not int or not 100 <= self.wal_autocheckpoint_pages <= 100_000:
            raise ValueError("WAL checkpoint interval must be 100 to 100000 pages")

    @classmethod
    def legacy(cls):
        """The behaviour before the disk-write review; used only for measurement."""
        return cls("FULL", 1000, -1, False)


class Store:
    def __init__(self, path, tuning=None, live_path=None):
        self.path = Path(path)
        self.tuning = tuning or StorageTuning()
        self._anchor = None
        # Newest readings only, kept on RAM-backed storage (e.g. /run). It lets
        # real-time readers see current values while the database is written
        # in rare batches. Never a record of history; the database remains that.
        self.live_path = Path(live_path) if live_path else None
        self._pinned = threading.local()
        # Minute detail lives in the telemetry files next to the database (WP23b, single store). `telemetry_clock` is what "now" means to
        # the readers; only tests replace it.
        self.telemetry_dir = self.path.parent / "telemetry"
        self.telemetry_clock = time.time

    def minute_source(self):
        from .minute_source import MinuteSource
        return MinuteSource(self.telemetry_dir, clock=lambda: self.telemetry_clock())

    # Status records the dashboard needs within seconds; value: the field that says which copy is newer.
    LIVE_SETTINGS = {"collector_health": "acquired_at", "charger_tracker": "last_t"}

    # Working records that only ever matter as their latest copy (provider polling results and the planner's last attempt). They are rewritten
    # every minute or few minutes, so each change is held on RAM-backed storage (next to the live file) and the database receives a copy at most
    # every RAM_PERSIST_SECONDS, and when a service stops. A reboot loses at most that much; the providers and planner refill them within a minute.
    RAM_SETTINGS = ("cache_slots", "established_slots", "provider_slots", "shadow_attempt", "shadow_plan")
    RAM_PERSIST_SECONDS = 1800

    def publish_live(self, observations, settings=None):
        if self.live_path is None:
            return
        body = encoded({"written_at": max(o.retrieved_at for o in observations),
                        "observations": [asdict(o) for o in observations],
                        "settings": settings or {}})
        temporary = self.live_path.with_name(self.live_path.name + ".tmp")
        try:
            temporary.write_text(body)
            os.replace(temporary, self.live_path)   # readers never see a half-written file
        except OSError:
            pass   # live view is an optimisation; the database path still works

    def _live_document(self):
        """The live file's content, or {} if absent or damaged. Honours pinned_live() for this thread."""
        pinned = getattr(self._pinned, "data", None)
        if pinned is not None:
            return pinned
        if self.live_path is None:
            return {}
        try:
            document = json.loads(self.live_path.read_text())
            return document if isinstance(document, dict) else {}
        except (OSError, ValueError):
            return {}

    @contextmanager
    def pinned_live(self):
        """Read the live file once and use that single snapshot for everything in this block (this thread).

        A slow page build otherwise reads live readings, health and charger state at different moments,
        and newer values than the page's own clock make fresh data look stale or misaligned."""
        previous = getattr(self._pinned, "data", None)
        self._pinned.data = self._live_document() if self.live_path is not None else {}
        try:
            yield
        finally:
            self._pinned.data = previous

    def live_written_at(self):
        """When the collector last wrote the live file (None if there is none or it is unreadable)."""
        value = self._live_document().get("written_at")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def live_observations(self, provenance):
        """Newest published reading per metric, or {} if absent, damaged or foreign."""
        try:
            result = {}
            for row in self._live_document().get("observations", []):
                Observation(**row)    # reject anything that is not a valid observation
                if row["provenance"] == provenance and row["metric"] in UNITS:
                    result[row["metric"]] = row
            return result
        except (ValueError, KeyError, TypeError):
            return {}

    def connect(self):
        if self.tuning.hold_open and self._anchor is None:
            # An idle connection that is never closed while the process runs. SQLite
            # only checkpoints and removes the write-ahead log when the *last*
            # connection closes; without this every short-lived connection did both.
            anchor = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            anchor.execute("PRAGMA busy_timeout=5000")
            self._anchor = anchor
            self._attach_anchor()
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute(f"PRAGMA synchronous={self.tuning.synchronous}")
        db.execute(f"PRAGMA wal_autocheckpoint={self.tuning.wal_autocheckpoint_pages}")
        db.execute(f"PRAGMA journal_size_limit={self.tuning.journal_size_limit_bytes}")
        db.execute("PRAGMA temp_store=MEMORY")
        return db

    def _attach_anchor(self):
        # A connection only counts as "open" to SQLite's log once it has read the file.
        if self._anchor is not None:
            self._anchor.execute("SELECT count(*) FROM sqlite_master").fetchone()

    def close(self):
        """Release the held-open connection; the last close checkpoints the log."""
        anchor, self._anchor = self._anchor, None
        if anchor is not None:
            anchor.close()

    def initialise(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            # Several services start together. When the database is already at the current schema,
            # validate it with a read-only transaction so nobody waits on (or times out for) the write lock.
            db.execute("BEGIN")
            try:
                current = (db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
                           and db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION)
                if current:
                    self._check_tables(db)
            finally:
                db.rollback()
            if not current:
                self._migrate(db)
            db.execute("PRAGMA journal_mode=WAL")          # no lock needed when it is already WAL
        self._attach_anchor()
        self.path.chmod(0o600)

    @staticmethod
    def _check_tables(db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(observations)")}
        if columns != {"id", "source", "metric", "sample_at", "payload"}:
            raise ValueError("Incomplete or incompatible observation schema")
        for table, expected in {
            "setting_versions": {"name", "revision", "known_at", "payload"},
            "collector_snapshots": {"id", "acquired_at", "status", "raw"},
            "charger_coverage": {"start", "end"},
            "charger_sessions": {"id", "started_at", "ended_at", "payload"},
            "charger_samples": {"id", "time", "power_kw", "state"},
            "charger_energy": {"start", "end", "session_id", "kwh", "active_seconds"},
        }.items():
            actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise ValueError("Incomplete or incompatible collection schema")

    def _migrate(self, db):
        db.execute("BEGIN IMMEDIATE")
        try:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id != APPLICATION_ID and (tables or app_id):
                raise ValueError("Refusing to modify an existing non-controller database")
            if version > SCHEMA_VERSION or version < 0:
                raise ValueError("Unsupported database version; restricted state required")
            if version and app_id != APPLICATION_ID:
                raise ValueError("Unrecognised versioned database")
            if app_id == APPLICATION_ID and version == 0:
                raise ValueError("Incomplete controller schema")
            if version == 0:
                for statement in SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                version = 1
            if version == 1:
                for statement in MIGRATION_2:
                    db.execute(statement)
                version = 2
            if version == 2:
                for statement in MIGRATION_3:
                    db.execute(statement)
                # Recover current values at genuine save times. Earlier values
                # were not retained by 0.2: preserve that uncertainty explicitly.
                db.execute("""INSERT INTO setting_versions
                    SELECT json_extract(e.payload,'$.name'),json_extract(e.payload,'$.revision'),
                    e.time,CASE WHEN s.revision=json_extract(e.payload,'$.revision')
                    THEN s.payload ELSE '{"_history_unavailable":true}' END
                    FROM events e LEFT JOIN settings s ON s.name=json_extract(e.payload,'$.name')
                    WHERE e.kind='settings_changed'""")
                version = 3
            if version == 3:
                for statement in MIGRATION_4:
                    db.execute(statement)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._check_tables(db)
            db.commit()
        except Exception:
            db.rollback()
            raise

    def add_observations(self, observations):
        rows = [(o.id, o.source, o.metric, o.sample_at, encoded(asdict(o))) for o in observations]
        with closing(self.connect()) as db, db:
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)", rows)
            return db.total_changes - before

    def observations(self, start=0, end=1e20, metric=None, covered_only=False):
        sql = "SELECT payload FROM observations WHERE sample_at>=? AND sample_at<?"
        args = [number(start), number(end)]
        if metric:
            sql += " AND metric=?"
            args.append(metric)
        if covered_only:
            sql += " AND json_extract(payload,'$.coverage_seconds')=60"
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute(sql + " ORDER BY sample_at,id", args)]

    PAGE_WINDOW_S = 2 * 86400            # the history export reads the telemetry files this much at a time, so a year of it never sits in memory

    def observations_page(self, start, end, limit=2000, cursor=None):
        """One bounded page of one-minute readings, metric by metric then by time, for the history export.

        The readings are in the telemetry files (the one store of them); the rows the database still holds (imported history, and the minute summaries of earlier
        releases) are merged in, and a minute the files have is given once, from the files. Returns (rows, next_cursor); next_cursor is None after the last page. The
        cursor is (metric_index, sample_at, id): keyset paging, so a page costs the same however far into a year of records it is."""
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("Page size must be 1 to 10000 rows")
        metrics = sorted(UNITS)
        if cursor is None:
            index, at, last_id = 0, None, None
        else:
            if not (isinstance(cursor, (list, tuple)) and len(cursor) == 3 and type(cursor[0]) is int
                    and isinstance(cursor[1], (int, float)) and not isinstance(cursor[1], bool) and isinstance(cursor[2], str)):
                raise ValueError("Invalid cursor")
            index, at, last_id = cursor
        if not 0 <= index < len(metrics):
            raise ValueError("Invalid cursor")
        start, end = number(start), number(end)
        source = self.minute_source()
        rows = []
        with closing(self.connect()) as db:
            while index < len(metrics):
                metric = metrics[index]
                window = start if at is None else max(start, at)
                while window < end:
                    window_end = min(end, window + self.PAGE_WINDOW_S)
                    filed = []
                    try:
                        for minute, (mean, covered) in sorted(source.means(metric, window - 60, window_end - 60).items()):
                            taken, key = float(minute + 60), f"t{int(minute)}"
                            if not window <= taken < window_end or (at is not None and (taken, key) <= (at, last_id)):
                                continue
                            filed.append((taken, key, {"source": "telemetry-files", "metric": metric, "retrieved_at": taken, "value": float(mean), "unit": UNITS[metric],
                                                       "quality": "ok", "device_at": None, "raw_value": None, "raw_unit": None,
                                                       "coverage_seconds": float(covered) if covered is not None else None, "demand_mode": None,
                                                       "provenance": "telemetry-files", "interval_start": float(minute), "source_at": None, "demand_revision": None,
                                                       "window_samples": None, "window_ok": int(covered) if covered is not None else None,
                                                       "window_mean": float(mean), "window_max": None, "id": key}))
                    except Exception:
                        filed = []                                  # an unreadable file is not a reason to refuse the rows that are in the database
                    have = {int(f[2]["interval_start"]) for f in filed}
                    sql = "SELECT sample_at,id,payload FROM observations WHERE metric=? AND sample_at>=? AND sample_at<?"
                    args = [metric, window, window_end]
                    if at is not None:
                        sql += " AND (sample_at>? OR id>?)"
                        args += [at, last_id]
                    room = limit - len(rows)
                    stored = []
                    for sample_at, row_id, payload in db.execute(sql + " ORDER BY sample_at,id", args):        # lazily: a page never holds more than it can return
                        row = json.loads(payload)
                        if row.get("window_mean") is not None and int((sample_at - 1) // 60 * 60) in have:
                            continue                                # the collector's old minute summary of a minute the files hold
                        stored.append((sample_at, row_id, row))
                        if len(stored) > room:
                            break
                    merged = sorted(filed + stored, key=lambda r: (r[0], r[1]))
                    rows.extend(r[2] for r in merged[:room])
                    if len(merged) >= room and room > 0 and len(rows) >= limit:
                        last = merged[room - 1]
                        return rows, [index, last[0], last[1]]
                    window = window_end
                index, at, last_id = index + 1, None, None          # this metric is finished: move to the next one
        return rows, None

    def latest_observations(self, provenance, quality=None):
        """The last known reading of each metric. The collector's live file (RAM) has the newest reading of every metric, errors included, so a newer error is never hidden
        behind an older OK one; for a metric it does not have (the Pi has just restarted and the file is empty) the last valid reading in the telemetry files is used.
        Readings are no longer written to the database: any reading rows found there were written by an earlier release, and are used only when they are newer than both."""
        result = {}
        with closing(self.connect()) as db:
            predicate = "json_extract(payload,'$.provenance')=?"
            args = [provenance]
            if quality is not None:
                predicate += " AND json_extract(payload,'$.quality')=?"
                args.append(quality)
            for metric in UNITS:           # indexed top-one queries: cheap on every dashboard refresh, and empty once the old rows have been retired
                row = db.execute("SELECT payload FROM observations WHERE " + predicate +
                                 " AND metric=? ORDER BY json_extract(payload,'$.retrieved_at') DESC,rowid DESC LIMIT 1", args + [metric]).fetchone()
                if row:
                    result[metric] = json.loads(row[0])
        live = self.live_observations(provenance)
        for metric, row in live.items():
            if quality is not None and row["quality"] != quality:
                continue
            if metric not in result or row["retrieved_at"] > result[metric]["retrieved_at"]:
                result[metric] = row
        if provenance != "live" and quality in (None, "ok"):
            # The files are asked only for a metric whose last good reading is not already here: when the live file is empty or gone (the Pi has restarted), or when the live
            # file's newest reading of that metric is an error (the last GOOD one is then the newest valid second in the files). A metric the live file never carries is not asked.
            source, now = None, self.telemetry_clock()
            for metric in UNITS:
                if live and (metric in result or metric not in live):
                    continue                  # the live file speaks for this metric (or never carries it): the files are asked only for a last GOOD reading behind a newer error
                # the live file is empty or gone: the files are asked for every metric, and the newer of what they hold and any row an earlier release left is used (GitHub 59)
                try:
                    source = source or self.minute_source()
                    found = source.last_known(metric, now)
                except Exception:
                    found = None
                if found and (metric not in result or found[0] > result[metric]["retrieved_at"]):
                    at, value = found
                    result[metric] = asdict(Observation("telemetry-files", metric, at, value, UNITS[metric], Quality.OK, provenance=provenance, source_at=at))
        return result

    def record_snapshot(self, raw, observations, acquired_at, health):
        """Commit raw response, typed readings and collection health together."""
        return self.record_snapshots([(raw, observations, acquired_at, health)])[0]

    def record_snapshots(self, entries, tracker=None, ev_metric="ev_power", extra_observations=(), keep_observations=True):
        """Commit one or more collection cycles in a single transaction.

        Entries are (raw, observations, acquired_at, health) in time order. Every
        row is keyed by content, so writing the same entry twice (after a failed
        or interrupted commit) adds nothing. When a charger tracker is supplied it
        sees each entry's EV reading in order, inside the same transaction; a
        tracker with persist()/committed() (MemoryCharger) is instead asked to
        write whatever it has accumulated. Only the newest health summary is
        stored as the current collector health. `keep_observations=False` (the collector) commits the raw response, the event and the health but no
        reading rows: readings live in the telemetry files only.
        """
        import hashlib
        prepared = []
        for raw, observations, acquired_at, health in entries:
            number(acquired_at, "acquired_at", 0)
            if not isinstance(raw, bytes):
                raise ValueError("Exact response bytes required")
            identity = time_keyed(acquired_at, {"time": acquired_at, "sha256": hashlib.sha256(raw).hexdigest()})
            prepared.append((identity, raw, observations, acquired_at, health))
        if not prepared and not extra_observations and not hasattr(tracker, "persist"):
            return []
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for identity, raw, observations, acquired_at, health in prepared:
                inserted = db.execute("INSERT OR IGNORE INTO collector_snapshots VALUES(?,?,?,?)",
                                      (identity, acquired_at, health["status"], raw)).rowcount
                if inserted:
                    db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)",
                               (acquired_at, "collector_snapshot", encoded({"snapshot_id": identity, **health})))
                if keep_observations:
                    db.executemany("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                                   [(o.id, o.source, o.metric, o.sample_at, encoded(asdict(o))) for o in observations])
                if tracker is not None and not hasattr(tracker, "persist"):
                    tracker.observe_in(db, next(o for o in observations if o.metric == ev_metric))
            if keep_observations:
                db.executemany("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                               [(o.id, o.source, o.metric, o.sample_at, encoded(asdict(o))) for o in extra_observations])
            if hasattr(tracker, "persist"):
                tracker.persist(db)
            if prepared:
                db.execute("INSERT INTO settings VALUES('collector_health',1,?) ON CONFLICT(name) DO UPDATE SET revision=revision+1,payload=excluded.payload", (encoded(prepared[-1][4]),))
        if hasattr(tracker, "committed"):
            tracker.committed()
        return [p[0] for p in prepared]

    EVENT_PURGE_BATCH = 5000

    def prune_raw(self, before, max_bytes=67_108_864):
        """Only raw snapshots expire; typed observations/history are retained."""
        number(max_bytes, "raw byte budget", 1)
        with closing(self.connect()) as db, db:
            deleted = db.execute("DELETE FROM collector_snapshots WHERE acquired_at<?", (number(before),)).rowcount
            # The ranking query sorts every retained snapshot; run it only when over budget.
            if db.execute("SELECT total(length(raw)) FROM collector_snapshots").fetchone()[0] > max_bytes:
                deleted += db.execute("""DELETE FROM collector_snapshots WHERE id IN (
                    SELECT id FROM (SELECT id, sum(length(raw)) OVER
                        (ORDER BY acquired_at DESC,rowid DESC) AS retained_bytes
                        FROM collector_snapshots) WHERE retained_bytes>?)""", (max_bytes,)).rowcount
            # The event written with each snapshot goes with it (#12): nothing keeps them any longer than the snapshot they describe.
            oldest = db.execute("SELECT min(acquired_at) FROM collector_snapshots").fetchone()[0]
            cutoff = 1e20 if oldest is None else oldest
        while True:                                  # in small batches, each its own transaction, so the first purge of an old database never holds the write lock for long
            with closing(self.connect()) as db, db:
                done = db.execute("DELETE FROM events WHERE id IN (SELECT id FROM events WHERE kind='collector_snapshot' AND time<? ORDER BY time LIMIT ?)",
                                  (cutoff, self.EVENT_PURGE_BATCH)).rowcount
            if done < self.EVENT_PURGE_BATCH:
                break
        return deleted

    def _ram_enabled(self, name):
        return self.live_path is not None and name in self.RAM_SETTINGS

    def _ram_file(self, name):
        return self.live_path.with_name("ram-" + name + ".json")

    def _ram_read(self, name):
        """The RAM copy of a working record, or None if absent, damaged or made for another database."""
        try:
            document = json.loads(self._ram_file(name).read_text())
            if document.get("db") == str(self.path) and isinstance(document.get("revision"), int):
                return document
        except (OSError, ValueError, AttributeError):
            pass
        return None

    @contextmanager
    def _ram_lock(self):
        handle = open(self.live_path.with_name("ram.lock"), "a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            handle.close()

    def _ram_write(self, name, document):
        temporary = self._ram_file(name).with_suffix(".tmp")
        temporary.write_text(encoded(document))
        os.replace(temporary, self._ram_file(name))      # readers never see a half-written file

    def _persist(self, db, name, revision, body, now):
        db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,payload=excluded.payload", (name, revision, body))
        db.execute("INSERT OR IGNORE INTO setting_versions VALUES(?,?,?,?)", (name, revision, now, body))
        # Events contain validated settings only; no provider credentials accepted here.
        db.execute("INSERT INTO events(time,kind,payload) VALUES(?,?,?)", (now, "settings_changed", encoded({"name": name, "revision": revision})))

    def with_ram_settings(self, settings):
        """`settings` as {name: (revision, payload)} read straight from the database, with the RAM-held working records laid over it."""
        if self.live_path is not None:
            for name in self.RAM_SETTINGS:
                document = self._ram_read(name)
                if document is not None:
                    settings[name] = (document["revision"], document["payload"])
        return settings

    def get_setting(self, name, default=None):
        if self._ram_enabled(name):
            document = self._ram_read(name)
            if document is not None:
                return document["revision"], document["payload"]
        with closing(self.connect()) as db:
            row = db.execute("SELECT revision,payload FROM settings WHERE name=?", (name,)).fetchone()
        revision, value = (0, default) if row is None else (row[0], json.loads(row[1]))
        if name in self.LIVE_SETTINGS:
            try:
                live = (self._live_document().get("settings") or {}).get(name)
                key = self.LIVE_SETTINGS[name]
                if live and (not value or live.get(key, 0) > value.get(key, 0)):
                    return revision, live
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        return revision, value

    def set_setting(self, name, value, now, expected_revision):
        """Compare-and-swap prevents competing browser tabs overwriting settings."""
        body = encoded(value)
        number(now, "now", 0)
        if self._ram_enabled(name):
            return self._ram_set(name, value, body, now, expected_revision)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM settings WHERE name=?", (name,)).fetchone()
            revision = 0 if row is None else row[0]
            if revision != expected_revision:
                raise ValueError("Settings changed; refresh before saving")
            self._persist(db, name, revision + 1, body, now)
            return revision + 1

    def set_settings(self, updates, now):
        """Several settings changed in ONE transaction (WP18, #20): `updates` is {name: (value, expected_revision)}. Every revision is checked first; if any is wrong, or
        any write fails, nothing is written. Returns {name: new revision}. For ordinary (not RAM-held) settings only."""
        number(now, "now", 0)
        bodies = {name: encoded(value) for name, (value, _revision) in updates.items()}
        if any(self._ram_enabled(name) for name in updates):
            raise ValueError("A RAM-held setting cannot be changed together with others")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = {}
            for name, (_value, expected) in updates.items():
                row = db.execute("SELECT revision FROM settings WHERE name=?", (name,)).fetchone()
                current[name] = 0 if row is None else row[0]
                if current[name] != expected:
                    raise ValueError("Settings changed; refresh before saving")
            for name in updates:
                self._persist(db, name, current[name] + 1, bodies[name], now)
            return {name: current[name] + 1 for name in updates}

    def _ram_set(self, name, value, body, now, expected_revision):
        with self._ram_lock():
            document = self._ram_read(name)
            if document is None:
                with closing(self.connect()) as db:
                    row = db.execute("SELECT revision FROM settings WHERE name=?", (name,)).fetchone()
                revision, persisted_at = (0 if row is None else row[0]), 0.0
            else:
                revision, persisted_at = document["revision"], document.get("persisted_at", 0.0)
            if revision != expected_revision:
                raise ValueError("Settings changed; refresh before saving")
            revision += 1
            if now - persisted_at >= self.RAM_PERSIST_SECONDS or now < persisted_at:
                with closing(self.connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    self._persist(db, name, revision, body, now)
                persisted_at = now
            self._ram_write(name, {"db": str(self.path), "revision": revision, "payload": value, "known_at": now, "persisted_at": persisted_at})
            return revision

    def ram_sync(self, name, revision, value, now):
        """Record that the database now holds `value` as revision `revision` (written by the caller's own transaction); readers get the RAM copy from here on."""
        if self._ram_enabled(name):
            with self._ram_lock():
                self._ram_write(name, {"db": str(self.path), "revision": revision, "payload": value, "known_at": now, "persisted_at": now})

    def flush_ram_settings(self):
        """Copy any working record the database has not yet received (a service is stopping). Returns the names written."""
        written = []
        if self.live_path is None:
            return written
        for name in self.RAM_SETTINGS:
            with self._ram_lock():
                document = self._ram_read(name)
                if document is None or document.get("persisted_at", 0.0) >= document.get("known_at", 0.0):
                    continue
                with closing(self.connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    self._persist(db, name, document["revision"], encoded(document["payload"]), document["known_at"])
                self._ram_write(name, {**document, "persisted_at": document["known_at"]})
                written.append(name)
        return written

    def setting_at(self, name, known_at, default=None):
        """What was saved by this decision/sample time; never read future settings."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT revision,payload FROM setting_versions WHERE name=? AND known_at<=? ORDER BY known_at DESC,revision DESC LIMIT 1", (name, number(known_at))).fetchone()
            return (0, default) if row is None else (row[0], json.loads(row[1]))

    def archive(self, acquired_at, raw, model):
        number(acquired_at, "acquired_at", 0)
        if not isinstance(raw, bytes):
            raise ValueError("Exact raw bytes required")
        import hashlib
        identity = fingerprint({"acquired_at": acquired_at, "raw": hashlib.sha256(raw).hexdigest(), "model": model})
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO issuances VALUES(?,?,?,?)",
                       (identity, acquired_at, encoded(model), raw))
        return identity

    def backup(self, destination):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValueError("Backup must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents accidental replacement of a database or old backup.
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        try:
            with closing(self.connect()) as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity check failed")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def restore(self, source):
        """Restore into a new path; never overwrite current observations/settings."""
        source = Path(source).resolve()
        if self.path.exists():
            raise FileExistsError("Restore requires a new database path")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
            if (db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                    or db.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, SCHEMA_VERSION)
                    or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                raise ValueError("Not a compatible, intact controller backup")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            try:
                with closing(sqlite3.connect(self.path)) as destination:
                    db.backup(destination)
            except Exception:
                self.path.unlink(missing_ok=True)
                raise
        self.initialise()

