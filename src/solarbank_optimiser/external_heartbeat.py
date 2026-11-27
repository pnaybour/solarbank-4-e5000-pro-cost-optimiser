"""An independent dead-man's switch (GitHub issue 89): the Pi pings an outside service every five minutes, and that service alerts the owner when the pings stop.

Every alert the system raises itself is emailed by the Pi itself, so none can be sent if the Pi loses power, its disk fails or the internet is down. This is read-only and
outside the control loop (a systemd oneshot on a five-minute timer, run as the service user; no incoming port, nothing written to any device).

Up to three checks, whose ping URLs are SECRETS: whoever holds one can keep a check green. They are in a private file, `healthchecks.json` in the data folder (mode 600, never
in Git, a log or a message), as {"pi": "...", "optimiser": "...", "backup": "..."}; `backup` is optional.
  pi         pinged on every run: the Pi is up, on the network, and systemd is running timers. Healthchecks.io period 5 minutes, grace 10.
  optimiser  pinged only when the controller and the collector are demonstrably working (below). When they are not, the ping is WITHHELD and the outside service raises the
             alarm when its grace runs out; a Pi too dead to run this is caught by the first check. Same schedule.
  backup     pinged by the off-site backup after each verified upload (`ping_backup`): period 1 day, grace 1 hour or more.
Nothing here ever logs a URL: a failure prints only the kind of error."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

RUN = Path("/run/solarbank-cost-optimiser")
STATE = Path("/var/lib/solarbank-cost-optimiser")
CONFIG_NAME = "healthchecks.json"
MODE_FILE = Path("/etc/solarbank-cost-optimiser/control-mode")
CONTROLLER_MAX_AGE_S = 30.0         # the controller rewrites its status every second
LIVE_MAX_AGE_S = 60.0               # the newest device reading the collector has published
FUTURE_S = 60.0
HOST = "hc-ping.com"
KEYS = ("pi", "optimiser", "backup")


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def optimiser_healthy(now=None, run_dir=RUN, mode_file=MODE_FILE):
    """(healthy, reason). Healthy means: the controller's status is fresh and cycling in the mode the control-mode file asks for (and sending, if that is live), and the
    collector has published a device reading recently. A device that is switched off is not judged here beyond that: only a collector with NO fresh reading at all fails."""
    now = time.time() if now is None else now
    try:
        expected = Path(mode_file).read_text().strip()
        controller = _read_json(Path(run_dir) / "control-loop.json")
        live = _read_json(Path(run_dir) / "live.json")
        execution = controller["execution"]
        age = now - float(controller["at"])
        written = float(live["written_at"])
        readings = live["observations"]
        cycle = controller["cycle"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False, "a status file is missing or unreadable"
    if not -FUTURE_S <= age <= CONTROLLER_MAX_AGE_S:
        return False, f"the controller's status is {age:.0f} s old"
    if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle <= 0:
        return False, "the controller has not completed a cycle"
    if not isinstance(execution, dict) or execution.get("mode") != expected:
        return False, f"the controller runs in {execution.get('mode') if isinstance(execution, dict) else None!r} but control-mode says {expected!r}"
    if expected == "live" and execution.get("sends_commands") is not True:
        return False, "control-mode is live but the controller is not sending"
    if not -FUTURE_S <= now - written <= LIVE_MAX_AGE_S or not isinstance(readings, list) or not readings:
        return False, f"the collector's newest reading is {now - written:.0f} s old"
    return True, "ok"


def valid_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == HOST and bool(parsed.path.strip("/")) and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError, AttributeError):
        return False


def ping(url, opener=urlopen, attempts=2):
    """True when the ping was delivered. Never raises and never prints the URL."""
    if not valid_url(url):
        print("Heartbeat URL is not a valid https://hc-ping.com/... address", file=sys.stderr)
        return False
    for attempt in range(attempts):
        try:
            with opener(Request(url, method="GET"), timeout=8) as response:
                if getattr(response, "status", 200) == 200:
                    return True
        except Exception as error:                                       # noqa: BLE001 -- the kind of error only
            if attempt + 1 == attempts:
                print(f"Heartbeat not delivered: {error.__class__.__name__}", file=sys.stderr)
    return False


def load_config(state_dir=STATE):
    config = _read_json(Path(state_dir) / CONFIG_NAME)
    if not isinstance(config, dict):
        raise ValueError("not an object")
    return config


def ping_backup(state_dir=STATE, opener=urlopen):
    """Called by the off-site backup after a verified upload. Does nothing without a configured `backup` check, and can never fail the backup."""
    try:
        url = load_config(state_dir).get("backup")
        return bool(url) and ping(url, opener=opener)
    except Exception:                                                    # noqa: BLE001 -- a monitoring ping must not break the job it reports on
        return False


def main(state_dir=STATE, run_dir=RUN, mode_file=MODE_FILE, now=None, opener=urlopen):
    try:
        config = load_config(state_dir)
        pi_url, optimiser_url = config["pi"], config["optimiser"]
    except (OSError, ValueError, KeyError, TypeError):
        print("Heartbeat configuration is missing or unreadable", file=sys.stderr)
        return 1
    delivered = ping(pi_url, opener=opener)
    healthy, reason = optimiser_healthy(now, run_dir, mode_file)
    if healthy:
        delivered = ping(optimiser_url, opener=opener) and delivered
    else:
        print(f"Optimiser heartbeat withheld: {reason}", file=sys.stderr)      # the outside service raises the alarm when its grace runs out
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
