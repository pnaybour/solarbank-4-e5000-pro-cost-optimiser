"""The shared record of the three devices: who each one is, and where it was last found (WP21, GitHub issues 14 and 15). Standard library only.

One small JSON file in the data folder, written atomically, read by the controller, the collector and the dashboard:
* `identities`: what each device is (digests and MACs, never a serial in clear). Learned the first time a device is read successfully, then checked on every reconnect.
  A device at an address that does not match is "unavailable", never data. `forget_identity` (the `devices accept` command) lets the owner replace a device on purpose.
* `addresses`: where rediscovery found a device after it moved. It overrides `devices.conf` only while `devices.conf` still says what it said when the device was found
  (`superseded`): an owner who edits the file afterwards is obeyed.
* `requests`: addresses the owner typed on the Settings page. The controller probes them and adopts them only if the identity matches (`status`: pending, adopted, rejected).
"""
from __future__ import annotations

import fcntl
import grp
import json
import os
import pwd
import tempfile
import threading
from pathlib import Path

from .devices_config import KEYS, valid_host

DEVICES = tuple(KEYS)
_LOCK = threading.Lock()
SERVICE_USER = "solarbank-optimiser"
FILE_MODE = 0o640


def _service_ids():
    """(uid, gid) of the service user, or None where it does not exist (a development machine)."""
    try:
        return pwd.getpwnam(SERVICE_USER).pw_uid, grp.getgrnam(SERVICE_USER).gr_gid
    except KeyError:
        return None


def _own(fd):
    """GitHub issue 84: `sudo solarbank devices confirm` runs as root and used to leave the book (and its lock file) owned by root, so the services could no longer read or
    update it. As root, give the file to the service user; always set the mode. Best effort for anyone else: a process that does not own an old root-owned file cannot
    change it, and must not fail for that (the deploy repairs ownership, scripts/own-state-dir.sh)."""
    try:
        if os.geteuid() == 0:
            ids = _service_ids()
            if ids is not None:
                os.fchown(fd, *ids)
        os.fchmod(fd, FILE_MODE)
    except OSError:
        pass


class DeviceBook:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        for key in ("identities", "addresses", "requests", "confirmed"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _write(self, data):
        """Atomic: the new content goes to a uniquely named file in the same folder, is flushed to disk, and replaces the book only when complete, so an interrupted write
        leaves the old book whole. The new file is made the service user's when written by root (GitHub issue 84)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=1, sort_keys=True)                # a value that cannot be written fails here, before anything exists on disk
        name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as handle:
                name = handle.name
                _own(handle.fileno())
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            name = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if name is not None:
                try:
                    os.unlink(name)
                except OSError:
                    pass

    def update(self, change):
        """Read, change and write under an exclusive file lock as well as the thread lock: the controller, the collector and the web service are separate processes
        and two of them updating at once must not lose either update (GitHub 43)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, open(self.path.with_name(self.path.name + ".lock"), "a") as lock:
            _own(lock.fileno())                                           # a lock file made by root must still be the service user's (GitHub issue 84)
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            change(data)
            self._write(data)
            return data

    # ---- identities ---------------------------------------------------------------------------------------------------------------------------------
    def identity(self, name):
        return self.read()["identities"].get(name)

    def learn_identity(self, name, identity):
        """Remember what this device is the first time it is read. Never overwrites: a different device is a mismatch, not an update."""
        def change(data):
            data["identities"].setdefault(name, identity)
        self.update(change)

    def forget_identity(self, name):
        def change(data):
            data["identities"].pop(name, None)
            data["confirmed"].pop(name, None)
        self.update(change)

    def confirm_identity(self, name):
        """The owner has seen what was learned and says it is the right device (`devices confirm NAME`). Nothing is written to the battery until this has been done for it
        (GitHub 45): a wrong device present at the first read would otherwise become the commissioned one unnoticed. Returns the identity confirmed, or None if none is learned."""
        found = []
        def change(data):
            identity = data["identities"].get(name)
            if identity is not None:
                data["confirmed"][name] = identity
                found.append(identity)
        self.update(change)
        return found[0] if found else None

    def is_confirmed(self, name):
        data = self.read()
        return data["identities"].get(name) is not None and data["confirmed"].get(name) == data["identities"].get(name)

    # ---- addresses -----------------------------------------------------------------------------------------------------------------------------------
    def effective_host(self, name, configured):
        """The address to use: the one rediscovery found, but only while `devices.conf` (or the command line) still gives the value it replaced."""
        found = self.read()["addresses"].get(name)
        if found and found.get("superseded") == configured and valid_host(found.get("host", "")):
            return found["host"]
        return configured

    def record_found(self, name, host, configured, now):
        def change(data):
            data["addresses"][name] = {"host": host, "superseded": configured, "at": now}
        self.update(change)

    # ---- requests from the Settings page ----------------------------------------------------------------------------------------------------------------
    def request_address(self, name, host, now):
        if name not in DEVICES or not valid_host(host):
            raise ValueError("Give an IP address or host name for solarbank, meter or shelly")
        def change(data):
            data["requests"][name] = {"host": host, "at": now, "status": "pending", "reason": None}
        self.update(change)

    def pending_requests(self):
        return {n: r for n, r in self.read()["requests"].items() if r.get("status") == "pending"}

    def settle_request(self, name, status, reason=None):
        def change(data):
            if name in data["requests"]:
                data["requests"][name].update(status=status, reason=reason)
        self.update(change)
