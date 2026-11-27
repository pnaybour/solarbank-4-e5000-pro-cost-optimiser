"""Control-path building blocks: live state, fixed-cadence device loops and a fan-out that cannot push back.

Memory is the source of truth for decisions. Every reading carries a monotonic arrival stamp; a failed read
never replaces or refreshes a good value, so consumers always see how old a value is. Nothing here touches
the disk or the database (see REALTIME_ACQUISITION_GUIDE.md section 5).
"""
from dataclasses import dataclass
import os
import queue
import socket
import threading
import time

from .devices.errors import DeviceError


@dataclass(frozen=True)
class Reading:
    value: dict          # decoded fields for one device read (never mutated)
    mono: float          # time.monotonic() when the reply arrived
    utc: float           # time.time() at the same moment, for records only
    seq: int             # increases by one per successful read
    latency: float       # seconds the read took


def _new_health():
    return {"fail_streak": 0, "failures": 0, "missed": 0, "last_error": None, "successes": 0}


class LiveState:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest, self._health = {}, {}

    def publish(self, device, reading):
        with self._lock:
            self._latest[device] = reading
            health = self._health.setdefault(device, _new_health())
            health["fail_streak"], health["last_error"] = 0, None
            health["successes"] += 1

    def fail(self, device, error):
        with self._lock:        # a failure never touches the last good value
            health = self._health.setdefault(device, _new_health())
            health["fail_streak"] += 1
            health["failures"] += 1
            health["last_error"] = getattr(error, "kind", "error")

    def note_missed_deadline(self, device):
        with self._lock:
            self._health.setdefault(device, _new_health())["missed"] += 1

    def snapshot(self):
        """Cheap copy for the control tick: {device: (Reading or None, health)}."""
        with self._lock:
            names = set(self._latest) | set(self._health)
            return {name: (self._latest.get(name), dict(self._health.get(name, _new_health()))) for name in names}


class Fanout:
    """Bounded hand-off to slower consumers (logging). Offering never blocks; the oldest item is dropped."""

    def __init__(self, maxsize=600):
        self.q, self.dropped = queue.Queue(maxsize), 0

    def offer(self, item):
        while True:
            try:
                self.q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self.q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass


class DeviceLoop(threading.Thread):
    """Polls one device at a fixed cadence with absolute deadlines. One attempt per tick; backoff after failures."""

    BACKOFF = (0, 0, 1, 2, 4, 8, 10)     # extra wait by consecutive failures: an isolated one is retried on the very next tick

    def __init__(self, name, read_fn, period, live, stop, fanout=None, settle=0.0, backoff=None):
        super().__init__(name=f"dev-{name}", daemon=True)
        self.dev, self.read_fn, self.period = name, read_fn, period
        self.live, self.stop_event, self.fanout, self.settle = live, stop, fanout, settle
        self.backoff = backoff or self.BACKOFF

    def run(self):
        seq, streak, next_tick = 0, 0, time.monotonic()
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                value = self.read_fn()                       # one attempt; raises DeviceError
                now = time.monotonic()
                done = getattr(self.read_fn, "received_at", None)      # a reader that does more work after its data arrives says when
                if not isinstance(done, (int, float)) or not now - 60 <= done <= now:      # a reader may report an EARLIER moment (a fed value)
                    done = now
                seq += 1
                reading = Reading(value, done, time.time() - (now - done), seq, done - started)
                self.live.publish(self.dev, reading)
                if self.fanout is not None:
                    self.fanout.offer((self.dev, reading))
                if streak and self.settle:                   # first success after a fault: be gentle
                    self.stop_event.wait(self.settle)
                streak = 0
            except DeviceError as error:
                self.live.fail(self.dev, error)
                streak += 1
            except Exception as error:                       # a bug in a reader must not kill acquisition
                self.live.fail(self.dev, error)
                streak += 1
            next_tick += self.period
            delay = next_tick - time.monotonic()
            if delay < 0:                                    # missed the deadline: record, skip forward, never burst
                self.live.note_missed_deadline(self.dev)
                next_tick, delay = time.monotonic(), 0
            delay = max(delay, self.backoff[min(streak, len(self.backoff) - 1)])
            self.stop_event.wait(delay)


def control_tick(live, limits, now=None):
    """Inputs younger than their limit, and the devices whose data is too old to use. Reports only."""
    now = time.monotonic() if now is None else now
    inputs, unknown = {}, []
    snapshot = live.snapshot()
    for device, limit in limits.items():
        reading, _health = snapshot.get(device, (None, None))
        if reading is None or now - reading.mono > limit:
            unknown.append(device)             # stale or never read means unknown, not "the last value"
        else:
            inputs[device] = {"value": reading.value, "age": now - reading.mono, "seq": reading.seq}
    return inputs, unknown


def sd_notify(message):
    """Tell systemd we are alive (watchdog) without any extra dependency."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect("\0" + path[1:] if path.startswith("@") else path)
            sock.sendall(message.encode())
        return True
    except OSError:
        return False
