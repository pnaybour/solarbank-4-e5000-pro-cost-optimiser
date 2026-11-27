"""Finding a device that has moved, and refusing one that is not the device (WP21, GitHub issues 14 and 15).

* `probe(name, host)`: read the device's identity (the Solarbank and the meter by Modbus, read-only; the Shelly by HTTP GetDeviceInfo) as a small dict of digests and MACs.
* `IdentityGuard`: wraps a device reader. After any failure, and every IDENTITY_RECHECK_S, it checks the device is the one the book remembers; a mismatch raises
  DeviceError('identity') so the reading is unavailable, never data from the wrong device.
* `Rediscovery`: when a device has been unreachable for a while, look for it: the neighbour table first, then a bounded scan of the /24 around the configured address
  (the right port, short timeouts, a limited number of connections at once, at most one scan per SCAN_EVERY_S), accepting only a host whose identity matches.
  It runs on its own thread and never on the control path. A found address goes into the DeviceBook and an alert is raised.
mDNS is not used: it needs multicast and a new dependency, and the neighbour table plus the bounded scan find a moved device on this network without them.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .devices import identity as ident
from .devices import shelly
from .devices.errors import DeviceError
from .devices.modbus import ReadOnlyModbusTcp

IDENTITY_RECHECK_S = 600.0
SCAN_EVERY_S = 600.0
UNREACHABLE_BEFORE_SEARCH_S = 60.0
PORTS = {"solarbank": 502, "meter": 502, "shelly": 80}
SCAN_WORKERS = 24
CONNECT_TIMEOUT_S = 0.4


def probe(name, host, timeout=1.5, modbus=ReadOnlyModbusTcp, http_info=shelly.device_info):
    """The identity of whatever answers at `host` as `name`, or DeviceError."""
    if name == "shelly":
        info = http_info(host, timeout=timeout)
        return {"id": info["id"], "mac": info["mac"]}
    client = modbus(host, PORTS[name], timeout=timeout)
    try:
        return ident.read_solarbank(client) if name == "solarbank" else ident.read_meter(client)
    finally:
        client.close()


class IdentityGuard:
    """A reader that never returns data from the wrong device."""

    def __init__(self, name, reader, probe_fn, book, host_of, clock=time.monotonic):
        self.name, self.reader, self.probe_fn, self.book, self.host_of, self._clock = name, reader, probe_fn, book, host_of, clock
        self._verified_at, self._failed, self._verified_host = None, True, None
        self.mismatch = False

    def health(self):
        inner = getattr(self.reader, "health", None)
        return {**(inner() if inner else {}), "identity_mismatch": self.mismatch}

    @property
    def received_at(self):
        return getattr(self.reader, "received_at", None)

    def __call__(self):
        now = self._clock()
        if self._failed or self._verified_at is None or now - self._verified_at >= IDENTITY_RECHECK_S:
            self._verify(now)
        try:
            return self.reader()
        except Exception:
            self._failed = True                                    # re-verify on the next attempt: the device may have been replaced while it was away
            raise

    def verified_for(self, host, seconds):
        """True only when the identity check that is still good was made AT THIS ADDRESS (GitHub 50): a write binds to the address that was checked, not to wherever the
        device is thought to be a moment later."""
        return self._verified_host == host and self.verified_within(seconds)

    def verified_within(self, seconds):
        """True only when the device at the current address was checked against the commissioned identity in the last `seconds` and nothing has failed since (a write
        must never go to a device that has not been recognised, GitHub 38)."""
        return (not self._failed and not self.mismatch and self._verified_at is not None and self._clock() - self._verified_at < seconds)

    def invalidate(self):
        """The address changed: nothing counts as verified until the next check at the new address."""
        self._failed = True

    def _verify(self, now):
        host = self.host_of(self.name)
        try:
            found = self.probe_fn(self.name, host)
        except DeviceError:
            self._failed = True
            raise
        expected = self.book.identity(self.name)
        if expected is None:
            self.book.learn_identity(self.name, found)
            expected = found
        if not ident.matches(expected, found):
            self.mismatch, self._failed = True, True
            raise DeviceError("identity", f"The {self.name} at {self.host_of(self.name)} is not the device that was commissioned")
        self.mismatch, self._failed, self._verified_at, self._verified_host = False, False, now, host


def neighbours(runner=subprocess.run):
    """IPv4 addresses in the neighbour (ARP) table that are reachable or stale: where something has recently been on the network."""
    try:
        out = runner(["ip", "-4", "neigh", "show"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[0]) and parts[-1] in ("REACHABLE", "STALE", "DELAY", "PROBE"):
            found.append(parts[0])
    return found


def _open(host, port, timeout=CONNECT_TIMEOUT_S):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def subnet_hosts(configured):
    """The other addresses in the /24 of `configured` (empty when it is a host name or not IPv4)."""
    try:
        address = ipaddress.ip_address(configured)
    except ValueError:
        return []
    if address.version != 4:
        return []
    network = ipaddress.ip_network(f"{address}/24", strict=False)
    return [str(h) for h in network.hosts() if h != address]


class Rediscovery:
    def __init__(self, book, expected_ports=PORTS, probe_fn=probe, neighbours_fn=neighbours, open_fn=_open, clock=time.monotonic, wall=time.time):
        self.book, self.ports, self.probe_fn, self.neighbours_fn, self.open_fn = book, expected_ports, probe_fn, neighbours_fn, open_fn
        self._clock, self._wall, self._last_scan = clock, wall, {}

    def candidates_first(self, name, configured):
        """Neighbour-table hosts first (cheap), then the rest of the /24."""
        first = [h for h in self.neighbours_fn() if h != configured]
        rest = [h for h in subnet_hosts(configured) if h not in first]
        return first, rest

    def _matches(self, name, host, expected):
        try:
            return ident.matches(expected, self.probe_fn(name, host))
        except (DeviceError, ValueError, OSError, KeyError):
            return False

    def find(self, name, configured, force=False):
        """The host where the commissioned `name` now answers, or None. Needs a commissioned identity: without one nothing can be recognised."""
        expected = self.book.identity(name)
        if expected is None:
            return None
        now = self._clock()
        if not force and now - self._last_scan.get(name, -1e18) < SCAN_EVERY_S:
            return None
        self._last_scan[name] = now
        first, rest = self.candidates_first(name, configured)
        port = self.ports[name]
        for host in first:
            if self.open_fn(host, port) and self._matches(name, host, expected):
                return host
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            open_hosts = [h for h, ok in zip(rest, pool.map(lambda h: self.open_fn(h, port), rest)) if ok]
        for host in open_hosts:
            if self._matches(name, host, expected):
                return host
        return None


class DiscoveryThread(threading.Thread):
    """Keeps the acquirer pointed at the right address. Every step it (1) follows the book: a new address recorded by anyone is adopted; (2) with `search`, when a device has been
    unreachable for UNREACHABLE_BEFORE_SEARCH_S, looks for it, records where it went and raises an alert; (3) with `search`, adopts addresses the owner asked for on the
    Settings page, after checking the identity. `move(name, host)` is how the acquirer is told. Only the controller searches; the collector only follows."""

    CLEAR_AFTER_S = 600.0

    def __init__(self, book, configured, health_fn, move, stop, alert=None, discoverer=None, period=15.0, wall=time.time, clock=time.monotonic, search=True):
        super().__init__(name="device-discovery", daemon=True)
        self.book, self.configured, self.health_fn, self.move, self.stop_event = book, dict(configured), health_fn, move, stop
        self.alert, self.discoverer, self.period, self._wall, self._clock, self.search = alert, discoverer or Rediscovery(book), period, wall, clock, search
        self._down_since, self._up_since = {}, {}

    def step(self):
        for name, configured in self.configured.items():
            self.move(name, self.book.effective_host(name, configured))          # follow the book (a no-op when nothing changed)
        if not self.search:
            return
        health = self.health_fn() or {}
        for name, info in health.items():
            down = (info or {}).get("fail_streak", 0) >= 3 or (info or {}).get("identity_mismatch")
            if not down:
                self._down_since.pop(name, None)
                since = self._up_since.setdefault(name, self._clock())
                if self.alert and self._clock() - since >= self.CLEAR_AFTER_S:
                    self.alert("clear", name, None)
                continue
            self._up_since.pop(name, None)
            since = self._down_since.setdefault(name, self._clock())
            if self._clock() - since < UNREACHABLE_BEFORE_SEARCH_S or name not in self.configured:
                continue
            host = self.discoverer.find(name, self.book.effective_host(name, self.configured[name]))
            if host:
                self.book.record_found(name, host, self.configured[name], self._wall())
                self.move(name, host)
                self._down_since.pop(name, None)
                if self.alert:
                    self.alert("moved", name, host)
            elif self.alert and self._clock() - since >= 3 * UNREACHABLE_BEFORE_SEARCH_S:
                self.alert("missing", name, None)
        self._adopt_requests()

    def _adopt_requests(self):
        for name, request in self.book.pending_requests().items():
            if request["host"] == self.book.effective_host(name, self.configured.get(name)):
                self.book.settle_request(name, "adopted")          # already the address in use: its identity is checked on the reader's own connection, no second one is opened
                continue
            expected = self.book.identity(name)
            try:
                found = self.discoverer.probe_fn(name, request["host"])
            except (DeviceError, ValueError, OSError, KeyError) as error:
                self.book.settle_request(name, "rejected", f"nothing usable answered at {request['host']}: {error}")
                continue
            if expected is not None and not ident.matches(expected, found):
                self.book.settle_request(name, "rejected", f"the device at {request['host']} is not the one commissioned (identity differs)")
                continue
            if expected is None:
                self.book.learn_identity(name, found)
            self.book.record_found(name, request["host"], self.configured.get(name), self._wall())
            self.move(name, request["host"])
            self.book.settle_request(name, "adopted")

    def run(self):
        while not self.stop_event.wait(self.period):
            try:
                self.step()
            except Exception:                                      # discovery must never take acquisition down
                pass
