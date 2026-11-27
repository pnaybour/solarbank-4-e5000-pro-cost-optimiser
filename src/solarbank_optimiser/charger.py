"""Persistent charger history (the v1.3 car signal: above 1,000 W for 3 s starts a session, below 300 W for 120 s ends it), for fixture replay before commissioning.

Each processor lifetime has its own run ID. Restart explicitly breaks energy
adjacency while retaining the session and marking its coverage partial.
"""

from contextlib import closing
import json
import uuid

from .energy import integrate
from .ev_policy import EV_OFF_KW, EV_ON_KW
from .models import Observation, Quality, encoded, number, time_keyed


class ChargerTracker:
    def __init__(self, store, confirmation_seconds, release_seconds, max_gap_seconds):
        self.store = store
        self.confirm = number(confirmation_seconds, "confirmation", 0)
        self.release = number(release_seconds, "release", 0)
        self.max_gap = number(max_gap_seconds, "maximum gap", 0)
        if self.max_gap == 0:
            raise ValueError("Maximum gap must be positive")
        self.run_id = str(uuid.uuid4())

    def observe(self, observation):
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self.observe_in(db, observation)

    def observe_in(self, db, observation):
        """Process one reading inside the caller's open write transaction."""
        if not isinstance(observation, Observation) or observation.metric != "ev_power":
            raise ValueError("Dedicated EV power observation required")
        t = observation.retrieved_at
        sample_t = observation.sample_at
        sample_id = time_keyed(sample_t, {"source": observation.source, "time": sample_t,
                                          "value": observation.value, "quality": observation.quality})
        valid = observation.quality == Quality.OK and observation.value >= 0
        kw = observation.value if valid else None
        existing = db.execute("SELECT state FROM charger_samples WHERE id=?", (sample_id,)).fetchone()
        row = db.execute("SELECT payload FROM settings WHERE name='charger_tracker'").fetchone()
        state = json.loads(row[0]) if row else {}
        if existing:
            # An old duplicate must never replay an old standby state over
            # the current charging latch.
            return state.get("label", existing[0])
        last_t = state.get("last_t")
        # A reading that is not later than the last one means the clock stepped back (a reboot before the time sync, a restore, a bad NTP step). That is a break in
        # continuity, never a reason to stop the collector (#10): the session is marked partial, nothing is integrated across the break and tracking carries on from here.
        backwards = last_t is not None and t <= last_t
        if backwards:
            state["clock_breaks"] = state.get("clock_breaks", 0) + 1
        session = None
        if state.get("session_id"):
            session = json.loads(db.execute("SELECT payload FROM charger_sessions WHERE id=?", (state["session_id"],)).fetchone()[0])
        contiguous = bool(not backwards and state.get("run_id") == self.run_id and last_t is not None
                          and state.get("last_kw") is not None and valid
                          and 0 < sample_t - state.get("last_sample_t", last_t) <= self.max_gap)
        if not contiguous:
            if session:
                session["partial"] = True
            state["high_since"] = None
            state["low_since"] = None
            state["pending"] = []
            state["pending_peak"] = 0
        pieces = integrate(state.get("last_sample_t", last_t), sample_t, state["last_kw"], kw, self.max_gap, .01) if contiguous else []
        # Covered seconds are no longer written here: the telemetry count file for the charger power holds them (WP23b, single store).
        # A session starts above 1,000 W; inside one only a reading BELOW 300 W counts towards ending it, and the band between changes nothing (D7).
        active = valid and ((kw >= EV_OFF_KW) if session else (kw > EV_ON_KW))
        if not valid:
            label = "unknown"
        elif active:
            state["low_since"] = None
            if session:
                label = "charging"
            else:
                if state.get("high_since") is None:
                    state["high_since"] = t
                    state["candidate_partial"] = not contiguous
                state.setdefault("pending", []).extend([p.__dict__ for p in pieces])
                state["pending_peak"] = max(state.get("pending_peak", 0), kw)
                if t - state["high_since"] >= self.confirm:
                    active_starts = [p["start"] for p in state["pending"] if p["active_seconds"] > 0]
                    session = {"id": str(uuid.uuid4()), "started_at": min(active_starts, default=sample_t), "ended_at": None,
                               "energy_kwh": 0., "active_seconds": 0., "peak_kw": state["pending_peak"], "partial": state.get("candidate_partial", True)}
                    state["session_id"] = session["id"]
                    # Insert parent before its interval evidence in this transaction.
                    db.execute("INSERT INTO charger_sessions VALUES(?,?,?,?)",
                               (session["id"], session["started_at"], None, encoded(session)))
                    self._pieces(db, session, state["pending"])
                    state["pending"] = []
                    pieces = []
                    label = "charging"
                else:
                    label = "confirming_start"
        else:
            state["high_since"] = None
            state["pending"] = []
            if session:
                if state.get("low_since") is None:
                    state["low_since"] = t
                label = "confirming_stop"
                if t - state["low_since"] >= self.release:
                    label = "standby"
                    session["ended_at"] = t
                    state["session_id"] = None
            else:
                state["pending_peak"] = 0
                label = "standby"
        if session:
            self._pieces(db, session, [p.__dict__ for p in pieces])
            if valid:
                session["peak_kw"] = max(session["peak_kw"], kw)
            db.execute("UPDATE charger_sessions SET ended_at=?,payload=? WHERE id=?",
                       (session["ended_at"], encoded(session), session["id"]))
        state.update(last_t=t, last_sample_t=sample_t, last_kw=kw, run_id=self.run_id, label=label)
        if label in ("charging", "standby"):
            state.update(last_confirmed_label=label, last_confirmed_at=sample_t)
        db.execute("INSERT INTO charger_samples VALUES(?,?,?,?)", (sample_id, t, kw, label))
        db.execute("INSERT INTO settings VALUES('charger_tracker',1,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,revision=revision+1", (encoded(state),))
        return label

    @staticmethod
    def _pieces(db, session, pieces):
        for piece in pieces:
            if piece["active_seconds"] <= 0:
                continue
            db.execute("INSERT INTO charger_energy VALUES(?,?,?,?,?)", (piece["start"], piece["end"], session["id"], piece["positive_kwh"], piece["active_seconds"]))
            session["energy_kwh"] += piece["positive_kwh"]
            session["active_seconds"] += piece["active_seconds"]

    def sessions(self):
        with closing(self.store.connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM charger_sessions ORDER BY started_at")]

