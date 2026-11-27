"""The one alert path (WP5, decision D1: email through Resend). Standard library only.

Three parts, deliberately separate so the controller never needs the internet (its unit has `IPAddressDeny=any`):
  * `raise_alert` / `clear_alert` write or remove one small file per alert key in a RAM folder. Anything may call them; they need no network.
  * `process` is run by the alert unit (the only unit allowed outbound HTTPS to the mail service). It sends each alert file that has not been sent within the cooldown, keeps
    the file if the send fails (retried on the next run), and remembers what it sent so one incident is not mailed every two minutes.
  * `check` looks at the system the way a person would (is each service active? is the controller's heartbeat fresh? is the disk filling?) and raises or clears alerts.
    A restart loop never reaches systemd's "failed" state once the start limit is off, so this is what notices it.

Secrets: the Resend key lives in its own private file (mode 600) in the state folder, never in SQLite, logs or arguments; the addresses are in `alert.json` beside it."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
import urllib.request

RESEND_URL = "https://api.resend.com/emails"
COOLDOWN_S = 6 * 3600
CONSECUTIVE_BEFORE_ALERT = 2            # a service must be seen not active on two checks (about four minutes) before it is an alert: a deploy stops them for a minute
HEARTBEAT_STALE_S = 180.0
LIVE_STALE_S = 300.0
MISSING_GRACE_S = 300.0                 # a heartbeat file that does not exist is an alert only once its service has been active (and the Pi up) this long: after a boot /run starts empty
FUTURE_S = 60.0                         # a heartbeat file dated further ahead than this means a clock fault, not a healthy service
LOW_DISK_BYTES = 20 * 1024 ** 3
SERVICES = ("control", "web", "collector", "providers", "shadow")


def _safe_key(key):
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in str(key))[:80] or "alert"


def raise_alert(directory, key, subject, body, now=None):
    """Write (or refresh) the alert file for `key`. Never raises: an alert path that fails must not take the caller down."""
    try:
        directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe_key(key)}.json"
        document = {"key": _safe_key(key), "subject": str(subject)[:200], "body": str(body)[:4000], "raised_at": time.time() if now is None else now}
        if path.exists():
            try:
                document["raised_at"] = json.loads(path.read_text()).get("raised_at", document["raised_at"])      # an unchanged incident keeps its first time
            except (OSError, ValueError):
                pass
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(document))
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def clear_alert(directory, key):
    try:
        (Path(directory) / f"{_safe_key(key)}.json").unlink()
    except OSError:
        pass


def pending(directory):
    out = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            document = json.loads(path.read_text())
            if isinstance(document, dict) and document.get("key"):
                out.append(document)
        except (OSError, ValueError):
            continue
    return out


class ResendSender:
    """Sends through Resend. `transport(request)` is injectable for tests; it returns the HTTP status."""

    def __init__(self, key_path, config_path, transport=None):
        self.key_path, self.config_path = Path(key_path), Path(config_path)
        self.transport = transport or self._http

    @staticmethod
    def _http(request):
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status

    def configured(self):
        """(ok, why-not). The key file must be private and the addresses present."""
        try:
            if not self.key_path.exists():
                return False, "no Resend key file"
            if self.key_path.is_symlink() or self.key_path.stat().st_mode & 0o077:
                return False, "the Resend key file must be private (chmod 600)"
            config = json.loads(self.config_path.read_text())
            if not (isinstance(config.get("to"), str) and "@" in config["to"] and isinstance(config.get("from"), str) and "@" in config["from"]):
                return False, "alert.json needs 'to' and 'from' addresses"
            return True, None
        except (OSError, ValueError):
            return False, "alert.json missing or unreadable"

    def send(self, subject, body):
        ok, why = self.configured()
        if not ok:
            raise RuntimeError(why)
        config = json.loads(self.config_path.read_text())
        key = self.key_path.read_text().strip()
        payload = json.dumps({"from": config["from"], "to": [config["to"]], "subject": "[Solarbank] " + subject, "text": body}).encode()
        request = urllib.request.Request(RESEND_URL, data=payload, method="POST",
                                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "solarbank-cost-optimiser-alerts"})
        try:
            status = self.transport(request)
        except (urllib.error.URLError, OSError) as error:
            raise RuntimeError(f"send failed ({error.__class__.__name__})") from None
        if not 200 <= status < 300:
            raise RuntimeError(f"the mail service answered {status}")


def process(directory, state_path, sender, now=None, cooldown=COOLDOWN_S):
    """Send every pending alert not sent within `cooldown`. Returns (sent keys, failed {key: reason}). A failed send leaves the file and is retried on the next run."""
    now = time.time() if now is None else now
    state_path = Path(state_path)
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    sent, failed = [], {}
    live = {a["key"] for a in pending(directory)}
    state = {k: v for k, v in state.items() if k in live or k.startswith("_")}              # a cleared incident is forgotten, so a repeat is mailed again
    for alert in pending(directory):
        last = state.get(alert["key"])
        if last is not None and now - last < cooldown:
            continue
        try:
            sender.send(alert["subject"], alert["body"] + f"\n\nRaised {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(alert['raised_at']))}. This message repeats at most every {cooldown // 3600} hours while the problem lasts.")
            state[alert["key"]] = now
            sent.append(alert["key"])
        except RuntimeError as error:
            failed[alert["key"]] = str(error)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_name(state_path.name + ".tmp")
        temporary.write_text(json.dumps(state)); os.replace(temporary, state_path)
    except OSError:
        pass
    return sent, failed


def _is_active(unit):
    return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip() == "active"


def _is_failed(unit):
    """True while systemd still holds the unit in the failed state. A timer-run job (the off-site backup) is 'inactive' after a good run, never 'active'."""
    return subprocess.run(["systemctl", "is-failed", unit], capture_output=True, text=True).stdout.strip() == "failed"


def _boot_time():
    try:
        return time.time() - float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def check(directory, state_path, runtime_dir, state_dir, now=None, is_active=_is_active, is_failed=None, disk_free=None, services=SERVICES, offsite_conf="/etc/solarbank-cost-optimiser/offsite.conf",
          boot_time=None):
    """Raise or clear alerts from what can be observed. `runtime_dir` holds the controller's heartbeat; `state_dir` is the data folder."""
    now = time.time() if now is None else now
    boot_time = _boot_time() if boot_time is None else boot_time
    state_path = Path(state_path)
    try:
        seen = json.loads(state_path.read_text())
    except (OSError, ValueError):
        seen = {}
    for name in services:
        unit = f"solarbank-optimiser-{name}.service"
        key = f"service-{name}"
        if is_active(unit):
            seen[key] = 0
            seen.setdefault(f"since-{name}", now)                           # when this run of "active" was first seen (the grace for a heartbeat file that is not there yet)
            clear_alert(directory, key)
        else:
            seen[key] = seen.get(key, 0) + 1
            seen.pop(f"since-{name}", None)
            if seen[key] >= CONSECUTIVE_BEFORE_ALERT:
                raise_alert(directory, key, f"{name} is not running",
                            f"The {name} service ({unit}) has not been active on {seen[key]} checks in a row. systemd keeps restarting it; look at: journalctl -u {unit} --since '1 hour ago'", now)
    for item in pending(directory):
        if item["key"].startswith("unit-failed-"):                   # raised once by systemd's OnFailure hook; nothing else clears it. The unit is running again: the incident is over
            unit = item["key"][len("unit-failed-"):]
            full = unit if unit.endswith(".service") else unit + ".service"
            if is_failed is not None:
                failed = is_failed(full)
            elif is_active is _is_active:
                failed = _is_failed(full)                       # the real check: failed state in systemd
            else:
                failed = not is_active(full)                    # a test that injects `is_active` only
            if not failed:                                       # running again, or finished well since (a timer-run job is inactive, not active, after a good run)
                clear_alert(directory, item["key"])
    for key, path, limit, label in (("heartbeat-control", Path(runtime_dir) / "control-loop.json", HEARTBEAT_STALE_S, "The controller's heartbeat"),
                                    ("heartbeat-collector", Path(runtime_dir) / "live.json", LIVE_STALE_S, "The collector's live readings")):
        service = key.split("-")[1]
        if seen.get(f"service-{service}", 0) > 0:
            clear_alert(directory, key)                                      # the service is not active: its own alert says so
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            age = None
        # 15 November 2026 (GitHub issue 88): a file that is MISSING used to clear the alert, so an active service that never wrote its heartbeat looked healthy.
        if age is None:
            settled = now - max(seen.get(f"since-{service}", now), boot_time) >= MISSING_GRACE_S
            problem = "missing" if settled else "wait"
        elif age < -FUTURE_S:
            problem = "future"
        elif age > limit:
            problem = "stale"
        else:
            problem = None
        if problem == "stale":
            raise_alert(directory, key, f"{label} is stale", f"{label} ({path.name}) was last written {age:.0f} s ago although its service is active: it may be stuck.", now)
        elif problem == "missing":
            raise_alert(directory, key, f"{label} is missing", f"{label} ({path.name}) does not exist or cannot be read although its service has been active for "
                        f"{now - max(seen.get(f'since-{service}', now), boot_time):.0f} s: it may be stuck, or never have started writing.", now)
        elif problem == "future":
            raise_alert(directory, key, f"{label} is dated in the future", f"{label} ({path.name}) is dated {-age:.0f} s ahead of now: the Pi's clock may be wrong.", now)
        elif problem is None:
            clear_alert(directory, key)
        # "wait": just started and the file is not there yet; an alert already raised is left as it is
    free = shutil.disk_usage(state_dir).free if disk_free is None else disk_free
    if free < LOW_DISK_BYTES:
        raise_alert(directory, "disk-low", "Disk space is low", f"Only {free / 1024 ** 3:.1f} GiB is free on the data disk.", now)
    else:
        clear_alert(directory, "disk-low")
    from . import offsite
    late, text = offsite.stale(state_dir, offsite_conf, now)
    if late:
        raise_alert(directory, "offsite-backup", "No off-site backup for two days", text + " Look at: journalctl -u solarbank-optimiser-offsite.service --since '2 days ago'", now)
    else:
        clear_alert(directory, "offsite-backup")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_name(state_path.name + ".tmp")
        temporary.write_text(json.dumps(seen)); os.replace(temporary, state_path)
    except OSError:
        pass
