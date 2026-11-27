"""Offline comparison of database write activity: old behaviour versus tuned.

Replays N seconds of 1 Hz collection with a fake clock (no network, no waiting)
against a throw-away database and reports what the filesystem was asked to do.
It counts what SQLite asks the disk to do by reading the write-ahead log
itself: commits, bytes appended to the log, and bytes copied into the main
file at each checkpoint (the real checkpoint threshold of each profile is
honoured; the log is read back at every connection close and at the end).
On the Pi also watch `iotop -oPa` and `iostat -x 5` with the real services,
because kernel write-back timing and sync costs are not reproduced here.

    .venv/bin/python scripts/benchmark-writes.py [seconds]
"""
import json
import struct
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from solarbank_optimiser.collector import Collector, CollectorConfig
from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import StorageTuning, Store
import solarbank_optimiser.models as _models
_models.TIME_KEYED = _models.time_keyed

def payload(at):
    return {"ok": True, "pad": "x" * 700, "sample": {"time": at, "main": 500 + at % 7, "sub": 100},
            "shelly": {"ok": True, "sample": {"time": at, "ch1": 5., "ch2": 7.}},
            "solarbank": {"ok": True, "sample": {"time": at, "soc": 50 + (at // 300) % 5, "pv": 400 + at % 11,
                "load": 800 + at % 13, "grid": 400, "battery": 0, "charge_total_kwh": 12.3, "discharge_total_kwh": 9.8}}}


# 700 bytes of padding above makes the response about the size of the real monitor reply
     # makes the response about the size of the real monitor reply
PAGE = 4096
FRAME = 24 + PAGE


class WalWatcher:
    """Accumulates log frames and detects checkpoint-and-restart of the log."""
    def __init__(self, db, threshold):
        self.wal = Path(str(db) + "-wal")
        self.threshold = threshold
        self.salt = None
        self.read = 0
        self.log_bytes = 0
        self.main_bytes = 0
        self.checkpoints = 0
        self.segment = set()
        self.segment_frames = 0

    def _close_segment(self):
        if self.segment:
            self.main_bytes += len(self.segment) * PAGE
            self.checkpoints += 1
        self.segment = set()
        self.segment_frames = 0

    def scan(self):
        if not self.wal.exists():
            self._close_segment()
            self.salt, self.read = None, 0
            return
        data = self.wal.read_bytes()
        if len(data) < 32:
            return
        salt = data[16:24]
        if salt != self.salt:                  # log was restarted: previous one was checkpointed
            self._close_segment()
            self.salt, self.read = salt, 32
        while self.read + FRAME <= len(data):
            frame_salt = data[self.read + 8:self.read + 16]
            if frame_salt != salt:
                break
            self.segment.add(struct.unpack(">I", data[self.read:self.read + 4])[0])
            self.log_bytes += FRAME
            self.segment_frames += 1
            self.read += FRAME
            if self.segment_frames >= self.threshold:   # autocheckpoint would have fired
                self._close_segment()

    def finish(self):
        self.scan()
        self._close_segment()


def old_keys(enabled):
    """Profile A must also use the original random hash keys, as the old code did."""
    import solarbank_optimiser.models as models, solarbank_optimiser.store as store_module, solarbank_optimiser.charger as charger
    replacement = (lambda at, value: models.fingerprint(value)) if enabled else models.TIME_KEYED
    for module in (models, store_module, charger):
        module.time_keyed = replacement


def run(name, seconds, tuning, flush, live, record=1):
    old_keys(name.startswith("A"))
    root = Path(tempfile.mkdtemp(prefix="bench-"))
    store = Store(root / "c.sqlite3", tuning, (root / "live.json") if live else None)
    store.initialise()
    stats = {"commits": 0}
    watch = WalWatcher(root / "c.sqlite3", tuning.wal_autocheckpoint_pages)
    real = store.connect

    def counting():
        db = real()
        db.set_trace_callback(lambda sql: stats.__setitem__("commits", stats["commits"] + sql.lstrip().upper().startswith("COMMIT")))
        original = db.close
        # Scan the log just before a close that may checkpoint and delete it.
        db_close = lambda: (watch.scan(), original())
        return _Proxy(db, db_close)
    store.connect = counting
    clock = FakeClock(1_800_000_000)
    worker = Collector(store, CollectorConfig(flush_interval_seconds=flush, record_seconds=record), clock,
                       fetch=lambda _: json.dumps(payload(int(clock.utc()))).encode())
    for _ in range(seconds):
        worker.once()
        clock.advance(1)
        if not tuning.hold_open:
            watch.scan()
    worker.flush()
    watch.finish()
    db = root / "c.sqlite3"
    with sqlite3.connect(db) as check:
        rows = check.execute("SELECT count(*) FROM collector_snapshots").fetchone()[0]
        soc = check.execute("SELECT count(*) FROM observations WHERE metric='soc'").fetchone()[0]
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    total = (watch.log_bytes + watch.main_bytes) / seconds
    print(f"{name:34s} commits/s={stats['commits']/seconds:5.2f} log={watch.log_bytes/seconds/1024:6.1f}KB/s "
          f"checkpointed={watch.main_bytes/seconds/1024:6.1f}KB/s total={total/1024:6.1f}KB/s "
          f"({total*86400/1e9:5.2f}GB/day) checkpoints={watch.checkpoints:5d} snapshots={rows}/{seconds} soc={soc} {ok}")


class _Proxy:
    """Sqlite connection with a hook on close(); everything else passes through."""
    def __init__(self, db, close):
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "close", close)

    def __getattr__(self, name):
        return getattr(self._db, name)

    def __enter__(self):
        return self._db.__enter__()

    def __exit__(self, *args):
        return self._db.__exit__(*args)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1800
    run("A before (FULL, per-call, 1/s)", n, StorageTuning.legacy(), 0, False)
    run("B tuned, still commit every 1 s", n, StorageTuning(), 0, False)
    run("C tuned, commit every 2 s", n, StorageTuning(), 2, True)
    run("D tuned, commit every 30 s + live", n, StorageTuning(), 30, True)
    run("E tuned, commit every 60 s + live", n, StorageTuning(), 60, True)
    run("F one-minute log, commit every 60 s", n, StorageTuning(), 60, True, record=60)
