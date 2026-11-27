"""Offline supervisor decisions. This release deliberately has no equipment writer.

Tests can exercise protective priorities and handover states without sending a
Modbus write or importing Home Assistant. Commissioning is a separate phase.
"""

import fcntl
from pathlib import Path



class SingleOwner:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another controller owns this installation") from None
        return self

    def __exit__(self, *args):
        if self.handle:
            self.handle.close()
            self.handle = None


class NoEquipmentWriter:
    def send(self, *args, **kwargs):
        raise RuntimeError("Equipment writes are not implemented or commissioned in this release")


def request_handover(store, now, expected_revision):
    """A request is not proof of release. Never display successful handover here."""
    return store.set_setting("operating_state", {
        "mode": "handover_pending", "requested_at": now,
        "reason": "Anker release/readback capability has not been commissioned; use the Anker app to check current operation",
    }, now, expected_revision)

