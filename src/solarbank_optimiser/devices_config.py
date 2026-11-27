"""Where the three devices are (WP20, #15). Standard library only: the controller, collector and probe all import it.

The addresses are NOT in the code. They live in one small file outside the release folders, `/etc/solarbank-cost-optimiser/devices.conf`, so a release never carries an address and a
router change is one edit, not a deployment:

    SOLARBANK_HOST=...          the Solarbank 4 (Modbus, port 502)
    SOLARBANK_METER_HOST=...    the Smart Meter Gen 2 (Modbus, port 502)
    SOLARBANK_SHELLY_HOST=...   the Shelly Pro EM-50 (HTTP)

Precedence, highest first: an explicit command-line value, the environment, the file. A missing address for a device that was asked for is never guessed: the caller gets a
`DeviceConfigError` that names the setting and the file, and the controller refuses to start (exit 4) instead of talking to a made-up address.
Only IPv4 or IPv6 addresses and plain host names are accepted, so nothing in the file can become a command, a port or a URL."""

import ipaddress
import os
from pathlib import Path
import re

DEFAULT_PATH = Path("/etc/solarbank-cost-optimiser/devices.conf")
KEYS = {"solarbank": "SOLARBANK_HOST", "meter": "SOLARBANK_METER_HOST", "shelly": "SOLARBANK_SHELLY_HOST"}
_HOSTNAME = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class DeviceConfigError(ValueError):
    pass


def valid_host(text):
    if not isinstance(text, str) or not text or text != text.strip():
        return False
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(text))


def config_path(env=None):
    env = os.environ if env is None else env
    return Path(env.get("SOLARBANK_DEVICES_FILE") or DEFAULT_PATH)


def read_file(path):
    """{key: value} for the known keys in `path`; a missing file is {}. Unknown keys and lines that are not KEY=VALUE are ignored; a known key with a bad value is an error."""
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    found = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key not in KEYS.values():
            continue
        if not valid_host(value):
            raise DeviceConfigError(f"{path} line {number}: {key} must be an IP address or a host name, not {value!r}")
        found[key] = value
    return found


def resolve(env=None, path=None, overrides=None):
    """{'solarbank': host-or-None, 'meter': ..., 'shelly': ...} from `overrides` (the command line), then the environment, then the file."""
    env = os.environ if env is None else env
    path = config_path(env) if path is None else Path(path)
    stored = read_file(path)
    hosts = {}
    for device, key in KEYS.items():
        value = (overrides or {}).get(device) or env.get(key) or stored.get(key)
        if value is not None and not valid_host(value):
            raise DeviceConfigError(f"{key} must be an IP address or a host name, not {value!r}")
        hosts[device] = value
    return hosts


def require(hosts, devices, path=None):
    """Raise a plain message if any device in `devices` has no address."""
    missing = [name for name in devices if hosts.get(name) is None]
    if missing:
        where = config_path() if path is None else Path(path)
        raise DeviceConfigError("No address for " + ", ".join(f"{name} ({KEYS[name]})" for name in missing) + f". Set it in {where} (one line, for example {KEYS[missing[0]]}=<address>).")
    return hosts
