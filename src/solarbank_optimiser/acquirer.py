"""Direct, read-only acquisition for the collector (4a.3b).

Runs one DeviceLoop per device (Solarbank, smart meter, Shelly) into a LiveState and presents the newest
readings in exactly the JSON shape of the existing monitor's /api/readings page. The collector's decoding,
live file, charger tracking and one-minute log therefore work unchanged, and the direct decode matches the
monitor decode by construction. Devices not read directly can still come from the monitor ("mixed" mode),
which is how the Pi dry run proceeds one device at a time. Nothing here can write to equipment.
"""
from dataclasses import dataclass
import http.client
import json
import threading
import time

from .devices import meter, shelly, solarbank
from .devices.feed import FileFeedReader
from .devices.modbus import ReadOnlyModbusTcp
from .live import DeviceLoop, LiveState

DEVICES = ("solarbank", "meter", "shelly")


def parse_devices(text):
    """'all', 'none', or a comma list such as 'shelly,solarbank' -> a validated tuple."""
    text = (text or "").strip().lower()
    if text in ("", "none"):
        return ()
    if text == "all":
        return DEVICES
    names = tuple(part.strip() for part in text.split(","))
    unknown = [name for name in names if name not in DEVICES]
    if unknown or len(set(names)) != len(names):
        raise ValueError(f"Direct devices must be a list of {', '.join(DEVICES)} (or all/none)")
    return names


@dataclass(frozen=True)
class DirectConfig:
    devices: tuple = ()
    solarbank_host: str | None = None           # no address is built in (WP20): it comes from devices.conf, the environment or the command line
    meter_host: str | None = None
    shelly_host: str | None = None
    solarbank_port: int = 502
    meter_port: int = 502
    shelly_port: int = 80
    timeout_seconds: float = 1.0
    period_seconds: float = 1.0
    max_age_seconds: float = 5.0
    settings_log: str | None = None         # C1: log the Solarbank's mode, capability and limit registers here (read-only), looked at every `settings_every` seconds
    settings_every: float = 60.0
    device_book: str | None = None          # the shared record of device identities and found addresses (WP21); None leaves identity checks and rediscovery off
    discover: bool = False                  # search for a moved device (only the controller does; everything else just follows the book)
    alert_dir: str | None = None
    safety_every: float | None = None       # the controller reads the Solarbank's mode mask and Backup Reserve switch this often (GitHub issue 51); None for everything else
    mode_every: float | None = None         # the controller reads the Solarbank's operating mode this often (GitHub issue 36); None for everything else
    meter_feed: str | None = None           # read the meter from the controller's RAM file instead of opening a (second) Modbus connection

    def __post_init__(self):
        for name in self.devices:
            if name not in DEVICES:
                raise ValueError("Unknown direct device")
        from .devices_config import require
        hosts = {"solarbank": self.solarbank_host, "meter": self.meter_host if not self.meter_feed else "feed", "shelly": self.shelly_host}
        require(hosts, self.devices)                 # a requested device with no address is a clear error, never a guess
        if not 0 < self.timeout_seconds <= 10 or not 0.1 <= self.period_seconds <= 60 or not 0 < self.max_age_seconds <= 60:
            raise ValueError("Timeout, period and age limits out of range")


class DirectAcquirer:
    def __init__(self, config, readers=None, live=None, fanout=None, monotonic=time.monotonic):
        self.config, self.live, self.fanout, self._mono = config, live or LiveState(), fanout, monotonic
        self.stop_event = threading.Event()
        self._clients, self._loops = [], []
        self.hosts = {"solarbank": config.solarbank_host, "meter": config.meter_host, "shelly": config.shelly_host}
        self._modbus = {}
        self.book = None
        if config.device_book:
            from .device_book import DeviceBook
            self.book = DeviceBook(config.device_book)
            for name in self.hosts:
                if self.hosts[name]:
                    self.hosts[name] = self.book.effective_host(name, self.hosts[name])
        self._readers = readers if readers is not None else self._default_readers()
        self._discovery = None

    def _default_readers(self):
        cfg, readers = self.config, {}

        def client(host, port, name):
            c = ReadOnlyModbusTcp(self.hosts[name] or host, port, timeout=cfg.timeout_seconds)
            self._clients.append(c)
            self._modbus[name] = c
            return c
        if "solarbank" in cfg.devices:
            sink = None
            if cfg.settings_log:
                from .devices.settings_probe import SettingsLog
                sink = SettingsLog(cfg.settings_log).record
            readers["solarbank"] = solarbank.Reader(client(cfg.solarbank_host, cfg.solarbank_port, "solarbank"), settings_every=cfg.settings_every if sink else None, settings_sink=sink, mode_every=cfg.mode_every, safety_every=cfg.safety_every)
        if "meter" in cfg.devices and cfg.meter_feed:
            readers["meter"] = FileFeedReader(cfg.meter_feed)     # the controller owns the meter: it allows only one Modbus client
        elif "meter" in cfg.devices:
            mt = client(cfg.meter_host, cfg.meter_port, "meter"); readers["meter"] = lambda: meter.read(mt)
        if "shelly" in cfg.devices:
            readers["shelly"] = lambda: shelly.read_channels(self.hosts["shelly"], timeout=cfg.timeout_seconds, port=cfg.shelly_port)
        if self.book is not None:
            from .device_discovery import IdentityGuard, probe
            from .devices import identity as ident
            for name in ("solarbank", "meter", "shelly"):
                if name in readers and not isinstance(readers[name], FileFeedReader):
                    if name in self._modbus:
                        # The identity registers are read through the connection the reader already holds. A second connection would be refused by the meter (one client) or take one of
                        # the Solarbank's few (GitHub issue 37). Only rediscovery, with the device already unreachable, opens a connection of its own.
                        shared, read = self._modbus[name], (ident.read_solarbank if name == "solarbank" else ident.read_meter)
                        probe_fn = (lambda n, h, shared=shared, read=read: read(shared))
                    else:
                        probe_fn = probe                          # the Shelly: a separate HTTP request, no connection limit
                    readers[name] = IdentityGuard(name, readers[name], probe_fn, self.book, lambda n: self.hosts[n])
        return readers

    def move(self, name, host):
        """A device is now at `host` (found by rediscovery, or asked for by the owner): the next read goes there."""
        if not host or self.hosts.get(name) == host:
            return
        self.hosts[name] = host
        guard = self._readers.get(name) if isinstance(self._readers, dict) else None
        if hasattr(guard, "invalidate"):
            guard.invalidate()                              # a write waits for a fresh identity check at the new address (GitHub 38)
        client = self._modbus.get(name)
        if client is not None:
            client.set_host(host)

    def current_host(self, name):
        return self.hosts.get(name)

    def verified_host(self, name, seconds):
        """The address the device was recognised at, if it was recognised AT ITS CURRENT ADDRESS within `seconds` (and the owner has confirmed what was learned); else None.
        The writer is given this address and no other (GitHub 50). Without a device book there is nothing to check: the current address is returned."""
        host = self.hosts.get(name)
        guard = self._readers.get(name) if isinstance(self._readers, dict) else None
        if not host:
            return None
        if not hasattr(guard, "verified_for"):
            return host
        if guard.verified_for(host, seconds) and (self.book is None or self.book.is_confirmed(name)):
            return host
        return None

    def verified(self, name, seconds):
        """Has this device been recognised at its current address within `seconds`? Without a device book there is no identity to check, so the answer is True."""
        guard = self._readers.get(name) if isinstance(self._readers, dict) else None
        if not hasattr(guard, "verified_within"):
            return True
        return guard.verified_within(seconds) and (self.book is None or self.book.is_confirmed(name))      # recognised now, AND the owner has confirmed what was learned

    def _alert(self, kind, name, host):
        if not self.config.alert_dir:
            return
        from .alerts import clear_alert, raise_alert
        if kind == "clear":
            for key in ("moved", "missing"):
                clear_alert(self.config.alert_dir, f"device-{name}-{key}")
        elif kind == "moved":
            raise_alert(self.config.alert_dir, f"device-{name}-moved", f"The {name} moved to {host}",
                        f"The {name} stopped answering at its saved address and was found at {host} (identity checked). The saved address in devices.conf is unchanged and is overridden only while it stays as it was; update it when convenient.")
        elif kind == "missing":
            raise_alert(self.config.alert_dir, f"device-{name}-missing", f"The {name} cannot be found",
                        f"The {name} has been unreachable for several minutes and was not found by the neighbour table or a scan of its network. Check it is powered and on the network.")

    # -- lifecycle ---------------------------------------------------------------------------------------
    def start(self):
        if self.book is not None:
            from .device_discovery import DiscoveryThread
            configured = {n: h for n, h in (("solarbank", self.config.solarbank_host), ("meter", self.config.meter_host), ("shelly", self.config.shelly_host))
                          if h and n in self.config.devices and not (n == "meter" and self.config.meter_feed)}
            self._discovery = DiscoveryThread(self.book, configured, self.health, self.move, self.stop_event, alert=self._alert, search=self.config.discover)
            self._discovery.start()
        for name in self.config.devices:
            loop = DeviceLoop(name, self._readers[name], self.config.period_seconds, self.live, self.stop_event, fanout=self.fanout)
            loop.start()
            self._loops.append(loop)
        return self

    def stop(self, timeout=5):
        self.stop_event.set()
        for loop in self._loops:
            loop.join(timeout)
        for client in self._clients:
            client.close()

    # -- the monitor-shaped view -------------------------------------------------------------------------
    def group(self, name):
        """One device in the monitor's JSON shape: {"ok", "error", "sample"}; never a stale value presented as fresh."""
        reading, health = self.live.snapshot().get(name, (None, {}))
        if reading is None:
            error = (health or {}).get("last_error") or "no reading yet"
            return {"ok": False, "error": error, "sample": None}
        age = self._mono() - reading.mono
        if age > self.config.max_age_seconds:
            return {"ok": False, "error": f"stale ({age:.1f} s old; {(health or {}).get('last_error') or 'no new reading'})", "sample": None}
        return {"ok": True, "error": None, "sample": {"time": reading.utc, **reading.value}}

    def snapshot_json(self, monitor_data=None):
        """The /api/readings document: monitor data for devices not read directly, direct data for the rest."""
        data = dict(monitor_data or {})
        direct = self.config.devices
        if "solarbank" in direct:
            data["solarbank"] = self.group("solarbank")
        if "shelly" in direct:
            data["shelly"] = self.group("shelly")
        if "meter" in direct:
            data.pop("ok", None); data.pop("sample", None); data.pop("error", None)
            data.update(self.group("meter"))                  # the monitor puts the meter at the top level
        return data

    def fetch(self, config, monitor_fetch=None):
        """Drop-in for the collector's fetch(config): bytes of the assembled readings document."""
        monitor = {}
        if len(self.config.devices) < len(DEVICES):         # something still comes from the monitor
            try:
                from .collector import fetch_snapshot, strict_json
                monitor = strict_json((monitor_fetch or fetch_snapshot)(config))
            except (OSError, ValueError, http.client.HTTPException):
                monitor = {}                                  # direct groups stay valid; the rest read as unavailable
        return json.dumps(self.snapshot_json(monitor), allow_nan=False).encode()

    # -- health for the live file and the log ------------------------------------------------------------
    def health(self):
        now, result = self._mono(), {}
        for name, (reading, health) in self.live.snapshot().items():
            extra = getattr(self._readers.get(name), "health", None)         # for example the Solarbank's slow-register age and failures
            result[name] = {**(extra() if extra else {}), "age": None if reading is None else round(now - reading.mono, 3),
                            "seq": None if reading is None else reading.seq,
                            "latency": None if reading is None else round(reading.latency, 4),
                            "fail_streak": health["fail_streak"], "failures": health["failures"],
                            "missed": health["missed"], "last_error": health["last_error"]}
        return result
