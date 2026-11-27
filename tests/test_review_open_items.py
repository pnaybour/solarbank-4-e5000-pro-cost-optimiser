"""Open review items: finished charger sessions do not accumulate in memory."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from solarbank_optimiser.minutelog import MemoryCharger
from solarbank_optimiser.store import Store

GUARD = Path(__file__).resolve().parents[1] / "scripts/pi-storage-guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_an_unsaved_finished_session_is_never_forgotten(tmp_path):
    store = Store(tmp_path / "s.sqlite3"); store.initialise()
    charger = MemoryCharger(store, 0, 0, 10)
    charger.mem.executemany("INSERT INTO charger_sessions VALUES(?,?,?,?)",
                            [("old", 1, 2, '{"v":1}'), ("mid", 3, 4, '{"v":2}'), ("new", 5, 6, '{"v":3}')])
    charger.persisted_sessions = {"old": '{"v":0}'}                       # 'old' changed since it was saved; 'mid' was never saved
    charger.pending_marks = dict(charger.marks); charger._sessions_seen = dict(charger.persisted_sessions)
    charger.committed()
    assert {r[0] for r in charger.mem.execute("SELECT id FROM charger_sessions")} == {"old", "mid", "new"}
