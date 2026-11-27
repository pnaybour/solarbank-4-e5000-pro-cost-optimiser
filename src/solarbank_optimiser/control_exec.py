"""From a decision to commands, and from commands to outcomes. Standard library plus `machines.py` (which wraps `transitions`).

A decision (control_logic) is turned into PREPARED COMMANDS by one function that does not know whether the controller is in shadow
or live mode. Only the transport at the very end differs: the shadow transport records "suppressed"; a live transport would
send, wait for an acknowledgement and be checked for an observed effect. Each command keeps its stages separate and honest:

    prepared -> suppressed                                  (shadow: nothing was sent, nothing obeyed)
    prepared -> attempted -> acknowledged -> observed       (live: the device said yes, then the effect was measured)
    prepared -> attempted -> rejected | timeout | failed    (live: not accepted)
    prepared -> expired | superseded                        (never sent: too old, or overtaken by a newer protective command)

"Suppressed" is never "acknowledged". Results from a simulated transport are flagged `simulated` and never look like real ones.

Each command's stage is a small state machine (LIFECYCLE below, built by `machines.py`): the stage changes only through a declared event, so a late
acknowledgement or observation that arrives for a command already finished (superseded, expired, rejected) is refused and counted instead of
overwriting its record. An acknowledgement belongs to one command id and is not a confirmation: `control_verified()` needs the effect observed.
"""

from dataclasses import dataclass
import time

from .machines import Spec, build, fire

COMMAND_TTL_S = 30.0          # PROVISIONAL: a command not acted on within this is dropped, never executed late
COMMAND_REFRESH_S = 10.0      # PROVISIONAL: an unchanged command is re-issued (new id, fresh expiry) this often
MAX_ATTEMPTS = 3              # PROVISIONAL: bounded retries while the command is still valid and not superseded
OBSERVE_WITHIN_S = 10.0       # PROVISIONAL: how long after an acknowledgement the effect may take to appear

PROTECTIVE, ECONOMIC = "protective", "economic"
# The command lifecycle. Plan names: REQUESTED = prepared, SENT = attempted, ACKNOWLEDGED, READ_BACK/CONFIRMED = observed, FAILED = rejected | timeout | failed,
# SUPERSEDED = superseded (expired and unverified keep their own names). The stage strings are unchanged, so the status files and the log keep their format.
_OPEN = ["prepared", "attempted"]
LIFECYCLE = Spec("command", ("prepared", "attempted", "suppressed", "acknowledged", "observed", "unverified", "rejected", "timeout", "failed", "expired",
                             "superseded"), "prepared", (
    ("send", _OPEN, "attempted"),
    ("suppress", _OPEN, "suppressed"),
    ("acknowledge", _OPEN, "acknowledged"),
    ("reject", _OPEN, "rejected"),
    ("time_out", _OPEN, "timeout"),
    ("fail", _OPEN, "failed"),
    ("expire", _OPEN, "expired"),
    ("supersede", _OPEN, "superseded"),
    ("observe", "acknowledged", "observed"),
    ("unverify", "acknowledged", "unverified"),
))
OUTCOME_EVENT = {"suppressed": "suppress", "acknowledged": "acknowledge", "rejected": "reject", "timeout": "time_out", "failed": "fail"}
RESTRICTIVE = ("STOP", "WITHDRAW", "INHIBIT")      # a confirmed one of these shows the channel obeys a restriction

CHANNELS = ("ac_charge", "discharge", "control")      # "control" carries the hand-over (RELEASE)
UNCONFIRMED = ("prepared", "attempted", "rejected", "timeout", "failed", "unverified", "expired")


@dataclass(frozen=True)
class Command:
    id: str
    seq: int
    channel: str            # "ac_charge" | "discharge" | "control"
    action: str             # ac_charge: CHARGE_LIMIT | STOP | WITHDRAW ; discharge: INHIBIT | ALLOW ; control: RELEASE
    params: dict
    priority: str           # protective commands overtake economic ones; economic ones never overtake protective ones
    issued_at: float        # UTC (for the record only: ordering and expiry never depend on the wall clock)
    expires_at: float       # UTC (for the record)
    reason: str
    cycle: int = 0          # the controller cycle that prepared it: commands are ordered by this, not by time
    expires_mono: float = float("inf")    # the real deadline, on the monotonic clock

    def key(self):
        return (self.channel, self.action, tuple(sorted(self.params.items())))

    def as_dict(self):
        return {"id": self.id, "seq": self.seq, "cycle": self.cycle, "channel": self.channel, "action": self.action, "params": self.params, "priority": self.priority,
                "issued_at": self.issued_at, "expires_at": self.expires_at, "reason": self.reason}


class Preparer:
    """Turns each decision into the commands that express it. Identical for shadow and live."""

    def __init__(self, run_id, ttl=COMMAND_TTL_S, refresh=COMMAND_REFRESH_S):
        self.run_id, self.ttl, self.refresh = run_id, ttl, refresh
        self.seq = 0
        self._last = {}              # channel -> (key, mono time issued)

    def _desired(self, d):
        if d.economic == "handed_over":
            return [("control", "RELEASE", {}, PROTECTIVE)]               # the one command during a hand-over; it must be confirmed
        protective_charge = any(t.startswith("garage") or t == "controller_fault" for t in d.restrictions)
        if d.charge_limit_w > 0:
            charge = ("CHARGE_LIMIT", {"watts": round(d.charge_limit_w), "target_soc": d.charge_target_soc},
                      PROTECTIVE if protective_charge and d.charge_limit_w < d.preview_charge_w else ECONOMIC)
        elif protective_charge:
            charge = ("STOP", {}, PROTECTIVE)
        else:
            charge = ("WITHDRAW", {}, ECONOMIC)       # the economic charge request is explicitly taken back
        if d.discharge == "INHIBIT":
            discharge = ("INHIBIT", {}, PROTECTIVE)
        else:
            discharge = ("ALLOW", {}, ECONOMIC)       # lifting an inhibition is not allowed to overtake a newer protective one
        return [("ac_charge",) + charge, ("discharge",) + discharge]

    def prepare(self, decision, now_utc, now_mono, cycle=0):
        commands = []
        if decision.economic == "handed_over":
            self._last.pop("ac_charge", None); self._last.pop("discharge", None)       # on taking control back, say everything afresh
        else:
            self._last.pop("control", None)
        for channel, action, params, priority in self._desired(decision):
            key = (channel, action, tuple(sorted(params.items())))
            last = self._last.get(channel)
            if last and last[0] == key and now_mono - last[1] < self.refresh:
                continue
            self.seq += 1
            reason = "; ".join(decision.reasons) or "no restriction"
            commands.append(Command(f"{self.run_id}-{self.seq}", self.seq, channel, action, params, priority,
                                    now_utc, now_utc + self.ttl, reason, cycle, now_mono + self.ttl))
            self._last[channel] = (key, now_mono)
        return commands


@dataclass
class Outcome:
    kind: str                # suppressed | acknowledged | rejected | timeout | failed
    detail: str = ""


class LogTransport:
    """The shadow transport. It sends nothing anywhere: it only says the command was suppressed."""
    simulated = False
    sends = False

    def send(self, command, timeout):
        return Outcome("suppressed", "shadow mode: command recorded, not sent")


class _Life:
    """The model a command's machine is attached to."""


class Executor:
    """Runs prepared commands through a transport and keeps one honest record per command."""

    def __init__(self, transport, effect=None, max_attempts=MAX_ATTEMPTS, timeout=1.0):
        self.transport, self.effect, self.max_attempts, self.timeout = transport, effect, max_attempts, timeout
        self.records = {}          # id -> record
        self.order = []            # ids, newest last
        self.pending = []          # commands still to be sent (live only)
        self.lives = {}            # id -> the command's state machine

    def _record(self, command, now_utc):
        record = {**command.as_dict(), "stage": "prepared", "stages": {"prepared": now_utc}, "attempts": 0, "outcome": None,
                  "simulated": bool(getattr(self.transport, "simulated", False))}
        self.records[command.id] = record
        self.lives[command.id] = build(LIFECYCLE, _Life())
        self.order.append(command.id)
        del self.order[:-50]
        for gone in [i for i in self.records if i not in set(self.order)]:
            del self.records[gone]
            self.lives.pop(gone, None)
        return record

    def _move(self, record, event, now_utc, detail=""):
        """Fire a lifecycle event for this command. A refused event (not allowed from the stage the command is in) changes nothing."""
        life = self.lives.get(record["id"])
        if life is None or not fire(life, event):
            return False
        record["stage"], record["outcome"] = life.state, detail or life.state
        record["stages"][life.state] = now_utc
        return True

    def _finish(self, record, stage, now_utc, detail=""):
        self._move(record, {"expired": "expire", "superseded": "supersede", "observed": "observe", "unverified": "unverify"}[stage], now_utc, detail)

    def _supersede(self, new, now_utc):
        """A newer command replaces every older pending one on its channel; a newer PROTECTIVE one also cancels pending
        economic commands on any channel; a hand-over (RELEASE) cancels everything. Ordering is by controller cycle, never by
        wall-clock time, so a clock set backwards cannot keep an obsolete command alive."""
        kept = []
        for old in self.pending:
            if old.cycle >= new.cycle:                       # commands of the same cycle are one decision and never cancel each other
                kept.append(old); continue
            if (new.action == "RELEASE" or old.channel == new.channel
                    or (old.action == "RELEASE" and new.action != "RELEASE")      # control came back: an old release must not run
                    or (new.priority == PROTECTIVE and old.priority == ECONOMIC)):
                self._finish(self.records[old.id], "superseded", now_utc, f"overtaken by {new.id}")
            else:
                kept.append(old)
        self.pending = kept

    def execute(self, commands, inputs, now_utc, now_mono=None):
        """Returns the records touched this cycle. `inputs` are the fresh measurements, for observing effects.
        Deadlines use the monotonic clock (`now_mono`); the UTC times are only recorded."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        touched = []
        for command in commands:
            record = self._record(command, now_utc)
            self._supersede(command, now_utc)
            self.pending.append(command)
            touched.append(record)
        still = []
        for command in self.pending:
            record = self.records[command.id]
            if now_mono >= command.expires_mono:
                self._finish(record, "expired", now_utc, "expired before it could be acted on; discarded, never executed late")
                touched.append(record); continue
            record["attempts"] += 1
            record["stages"].setdefault("attempted", now_utc)
            try:
                outcome = self.transport.send(command, self.timeout)
            except Exception as error:                      # a transport bug must not stop the controller
                outcome = Outcome("failed", error.__class__.__name__)
            if outcome.kind == "timeout" and record["attempts"] < self.max_attempts:
                self._move(record, "send", now_utc); still.append(command)       # retried next cycle if still valid
            elif self._move(record, OUTCOME_EVENT.get(outcome.kind, "fail"), now_utc, outcome.detail):
                if outcome.kind == "suppressed":
                    record["stages"].pop("attempted", None); record["attempts"] = 0
                elif outcome.kind == "acknowledged":
                    record["acked_mono"] = now_mono
            touched.append(record)
        self.pending = still
        observe = getattr(self.transport, "observe", None)
        if observe is not None:                              # a transport that runs its own machine (the live transport) is told what was just measured, after this cycle's commands
            try:
                observe(inputs, now_mono)
            except Exception:                                # a transport bug must not stop the controller, but it is never silent
                import logging
                logging.getLogger(__name__).exception("the transport failed while observing this cycle")
        for cid in self.order:                              # look for the physical effect of acknowledged commands
            record = self.records.get(cid)
            if not record or record["stage"] != "acknowledged":
                continue
            seen = self.effect(record, inputs) if self.effect else None
            if seen is True:
                if self._move(record, "observe", now_utc, "effect measured"):
                    record["effect_simulated"] = record["simulated"]
                touched.append(record)
            elif now_mono - record.get("acked_mono", now_mono) > OBSERVE_WITHIN_S:
                # also when there is no way to check: an acknowledgement alone never becomes "verified" by waiting
                self._finish(record, "unverified", now_utc, "acknowledged but the effect was not seen in time" if self.effect
                             else "acknowledged, and no effect check is available")
                touched.append(record)
        return touched

    def control_verified(self):
        """True only when the equipment is known to be obeying us. In shadow nothing is sent, so there is nothing to verify. In live, on EVERY channel
        that has had a protective requirement, the newest protective command must have been OBSERVED taking effect: an acknowledgement alone is the device
        saying yes, not the battery doing it. Pending, rejected, timed-out, expired, unverified or merely acknowledged all mean 'not verified', and
        the supervisor then refuses to increase charging. Success on one channel never clears a failure on another (a confirmed INHIBIT does not
        excuse a refused STOP, #17). On the same channel a failure is cleared by a newer protective command that is observed, or by a newer
        observed restriction (STOP, WITHDRAW, INHIBIT): that shows the channel obeys. A newer CHARGE_LIMIT or ALLOW does not."""
        if not getattr(self.transport, "sends", False):
            return True
        newest = {}
        for r in self.records.values():
            if r["priority"] == PROTECTIVE and (r["channel"] not in newest or r["seq"] > newest[r["channel"]]["seq"]):
                newest[r["channel"]] = r
        for channel, record in newest.items():
            if record["stage"] == "observed":
                continue
            if any(r["channel"] == channel and r["seq"] > record["seq"] and r["stage"] == "observed" and r["action"] in RESTRICTIVE
                   for r in self.records.values()):
                continue
            return False
        return True

    def ownership(self):
        """Who has the equipment: 'controller', 'handover_pending' (RELEASE requested, not confirmed) or 'anker' (RELEASE acknowledged)."""
        latest = self.latest()
        release = latest.get("control")
        newest_other = max((r["seq"] for c, r in latest.items() if c != "control"), default=0)
        if release is None or newest_other > release["seq"]:      # no hand-over, or control was taken back afterwards
            return "controller"
        # Where commands really go, a hand-over counts only once the device was seen to follow it ("observed"): a request accepted, or writes that returned without error, are not
        # the battery being released. In shadow nothing is sent, so the acknowledgement stands in as before.
        done = ("observed",) if getattr(self.transport, "sends", False) else ("acknowledged", "observed")
        return "anker" if release["stage"] in done else "handover_pending"

    def latest(self):
        """The newest record per channel, for the status file."""
        out = {}
        for cid in self.order:
            record = self.records.get(cid)
            if record:
                out[record["channel"]] = record
        return out
