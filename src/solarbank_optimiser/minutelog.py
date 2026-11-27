"""One-minute history from one-second collection, with live charger tracking in memory.

Control and the dashboard use the one-second readings (held in memory and the
live file). Only a per-minute summary reaches the disk: the last sample of each
minute, with the sample count, OK count, mean and maximum of the second-by-second
values. A change of collection status is kept at full resolution as evidence.
"""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import threading

from .charger import ChargerTracker
from .models import Quality


class MinuteLog:
    def __init__(self, max_keep=30, on_drop=None):
        self.buckets = {}
        self.previous_status = None
        self.max_keep, self.on_drop = max_keep, on_drop or (lambda *args: None)

    def add(self, raw, observations, acquired, health):
        entry = (raw, observations, acquired, health)
        bucket = self.buckets.setdefault(int(acquired) // 60 * 60, {"stats": {}, "last": None, "keep": []})
        for o in observations:
            st = bucket["stats"].setdefault(o.metric, {"n": 0, "ok": 0, "sum": 0., "max": None})
            st["n"] += 1
            if o.quality == Quality.OK:
                st["ok"] += 1
                st["sum"] += o.value
                st["max"] = o.value if st["max"] is None else max(st["max"], o.value)
        bucket["last"] = entry
        if self.previous_status is not None and health["status"] != self.previous_status:
            bucket["keep"].append(entry)
            while len(bucket["keep"]) > self.max_keep:           # a flapping source cannot grow one minute without limit
                lost = bucket["keep"].pop(0)
                self.on_drop("status_change_evidence", 1, lost[2], lost[2])
        self.previous_status = health["status"]

    def entries(self, now, final=False):
        """(minute starts, reduced entries) for finished minutes (all of them when final)."""
        current = int(now) // 60 * 60
        minutes = sorted(m for m in self.buckets if final or m < current)
        result = []
        for minute in minutes:
            bucket = self.buckets[minute]
            result.extend(bucket["keep"])
            raw, observations, acquired, health = bucket["last"]
            thinned = []
            for o in observations:
                st = bucket["stats"][o.metric]
                thinned.append(replace(o, window_samples=st["n"], window_ok=st["ok"],
                                       window_mean=st["sum"] / st["ok"] if st["ok"] else None, window_max=st["max"]))
            result.append((raw, thinned, acquired, {**health, "minute": minute, "samples": bucket["stats"]["soc"]["n"]}))
        return minutes, result

    def discard(self, minutes):
        for minute in minutes:
            self.buckets.pop(minute, None)


class MinuteSink:
    """Stands in for the store inside MinuteRecorder so its rows join the next commit."""
    def __init__(self, store, max_rows=10_000, on_drop=None):
        self.store, self.rows = store, []
        self.max_rows, self.on_drop = max_rows, on_drop or (lambda *args: None)

    def setting_at(self, *args, **kwargs):
        return self.store.setting_at(*args, **kwargs)

    def remove(self, saved):
        """Forget rows that were committed, by identity: rows may have been shed from the front or added at the back meanwhile."""
        gone = {id(row) for row in saved}
        self.rows[:] = [row for row in self.rows if id(row) not in gone]

    def add_observations(self, observations):
        self.rows.extend(observations)
        excess = len(self.rows) - self.max_rows
        if excess > 0:                                           # the database has been away for a very long time: shed the oldest
            lost = self.rows[:excess]
            del self.rows[:excess]
            self.on_drop("minute_observations", excess, min(o.sample_at for o in lost), max(o.sample_at for o in lost))


TABLES = (
    "CREATE TABLE settings (name TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)",
    "CREATE TABLE charger_sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, payload TEXT NOT NULL)",
    "CREATE TABLE charger_samples (id TEXT PRIMARY KEY, time REAL NOT NULL, power_kw REAL, state TEXT NOT NULL)",
    "CREATE TABLE charger_energy (start REAL NOT NULL, end REAL NOT NULL, session_id TEXT NOT NULL, kwh REAL NOT NULL, active_seconds REAL NOT NULL, PRIMARY KEY(start,end,session_id))",
    "CREATE TABLE charger_coverage (start REAL NOT NULL, end REAL NOT NULL, PRIMARY KEY(start,end))",
)


class MemoryCharger:
    """The normal charger tracker run every second against a private in-memory
    database, so live state is always current. Results are copied to the real
    database in the collector's batch commit (persist), one sample row per minute."""

    # table -> its time column, for describing what was discarded
    TIME_COLUMN = {"charger_samples": "time", "charger_energy": "start", "charger_coverage": "start"}

    def __init__(self, store, confirmation, release, max_gap, max_rows=200_000, on_drop=None, trim_every=500):
        self.store = store
        self.max_rows, self.on_drop, self.trim_every = max_rows, on_drop or (lambda *args: None), trim_every
        self._since_trim = 0
        self._lock = threading.RLock()          # the tick (observe) and the flush thread (persist) share one memory database
        self.mem = sqlite3.connect(":memory:", check_same_thread=False)
        for statement in TABLES:
            self.mem.execute(statement)
        with closing(store.connect()) as real:
            row = real.execute("SELECT payload FROM settings WHERE name='charger_tracker'").fetchone()
            if row:
                self.mem.execute("INSERT INTO settings VALUES('charger_tracker',1,?)", (row[0],))
                session_id = json.loads(row[0]).get("session_id")
                session = real.execute("SELECT * FROM charger_sessions WHERE id=?", (session_id,)).fetchone() if session_id else None
                if session:
                    self.mem.execute("INSERT INTO charger_sessions VALUES(?,?,?,?)", tuple(session))
            real.rollback()
        self.tracker = ChargerTracker(store, confirmation, release, max_gap)
        self.persisted_sessions = {}
        self.marks = {"charger_energy": 0, "charger_coverage": 0, "charger_samples": 0}
        self.pending_marks = dict(self.marks)
        self._sessions_seen = {}

    def observe(self, observation):
        with self._lock:
            self.tracker.observe_in(self.mem, observation)
            self.mem.commit()
            self._since_trim += 1
            if self._since_trim >= self.trim_every:
                self._since_trim = 0
                self._trim()

    def _trim(self):
        """Bound the in-memory charger tables if the database has not taken their contents for a very long time."""
        for table, column in self.TIME_COLUMN.items():
            count = self.mem.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            excess = count - self.max_rows
            if excess > 0:
                first, last = self.mem.execute(f"SELECT min({column}),max({column}) FROM (SELECT {column} FROM {table} ORDER BY rowid LIMIT ?)", (excess,)).fetchone()
                self.mem.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY rowid LIMIT ?)", (excess,))
                self.mem.commit()
                self.on_drop(table, excess, first, last)

    def state(self):
        with self._lock:
            row = self.mem.execute("SELECT payload FROM settings WHERE name='charger_tracker'").fetchone()
        return json.loads(row[0]) if row else {}

    def persist(self, db):
        with self._lock:
            self._persist(db)

    def _persist(self, db):
        """Write everything new since the last successful commit (inside the caller's transaction)."""
        m = self.mem
        for sid, started, ended, payload in m.execute("SELECT * FROM charger_sessions"):
            if self.persisted_sessions.get(sid) != payload:
                db.execute("""INSERT INTO charger_sessions VALUES(?,?,?,?) ON CONFLICT(id)
                    DO UPDATE SET ended_at=excluded.ended_at,payload=excluded.payload""", (sid, started, ended, payload))
        sessions = {r[0]: r[3] for r in m.execute("SELECT * FROM charger_sessions")}
        for table in ("charger_coverage", "charger_energy"):
            top = m.execute(f"SELECT coalesce(max(rowid),0) FROM {table}").fetchone()[0]
            rows = m.execute(f"SELECT * FROM {table} WHERE rowid>? AND rowid<=?", (self.marks[table], top)).fetchall()
            width = len(rows[0]) if rows else 0
            if rows:
                db.executemany(f"INSERT OR IGNORE INTO {table} VALUES({','.join('?' * width)})", rows)
            self.pending_marks[table] = top
        # The charger's power over time is the `ev_power` series in the telemetry files (one source of truth for samples): the per-minute sample rows that used to be copied
        # to the database here were read by nothing and are no longer written. The in-memory table is still trimmed as before.
        self.pending_marks["charger_samples"] = m.execute("SELECT coalesce(max(rowid),0) FROM charger_samples").fetchone()[0]
        state = m.execute("SELECT payload FROM settings WHERE name='charger_tracker'").fetchone()
        if state:
            db.execute("INSERT INTO settings VALUES('charger_tracker',1,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,revision=revision+1", (state[0],))
        self._sessions_seen = sessions

    def committed(self):
        with self._lock:
            self._committed()

    def _committed(self):
        """Called after the commit succeeded: forget what is now safely on disk."""
        self.persisted_sessions = dict(self._sessions_seen)
        self.marks = dict(self.pending_marks)
        for table, mark in self.marks.items():
            # Keep the newest row of each table (so row numbers never restart and the
            # tracker's duplicate check still sees the last sample); drop older, saved ones.
            self.mem.execute(f"DELETE FROM {table} WHERE rowid<?", (mark,))
        self._forget_saved_sessions()
        self.mem.commit()

    def _forget_saved_sessions(self):
        """Finished sessions that are safely on disk need not stay in memory for ever: keep the current session and the most
        recent finished one (the tracker may still refer to it); drop the rest, and forget their saved copies."""
        state = self.mem.execute("SELECT payload FROM settings WHERE name='charger_tracker'").fetchone()
        current = json.loads(state[0]).get("session_id") if state else None
        newest = self.mem.execute("SELECT id FROM charger_sessions WHERE ended_at IS NOT NULL ORDER BY ended_at DESC,started_at DESC LIMIT 1").fetchone()
        keep = {current, newest[0] if newest else None}
        for sid, payload in self.mem.execute("SELECT id,payload FROM charger_sessions WHERE ended_at IS NOT NULL").fetchall():
            if sid not in keep and self.persisted_sessions.get(sid) == payload:
                self.mem.execute("DELETE FROM charger_sessions WHERE id=?", (sid,))
                self.persisted_sessions.pop(sid, None)
