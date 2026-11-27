"""Storage guard (WP19, reduced): it ALERTS and never stops anything. Standard library only; runs hourly as the service user.

It used to stop the collector, planner, providers and website when the data folder passed 16 GiB. On a large disk that is worse than the problem: stopping the collector stops the evidence and the
telemetry. It now raises an alert (the one alert path of WP5, which emails you) and clears it when the condition goes away. Four conditions:
  * the data folder (everything except `backups/`, which switch-release.sh bounds) is above DATA_ALERT_BYTES;
  * free space is below the larger of MIN_FREE_BYTES and a tenth of the disk;
  * the data folder grew by more than GROWTH_ALERT_BYTES since the last hourly check (abnormal growth);
  * the fixed-size telemetry store changed size (it never should: same files, same sizes) or passed TELEMETRY_CAP_BYTES.
The emergency purge of SQLite detail that WP19 first described is not built: with a disk this size it is not needed, and it returns if the data moves to a small one."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import time

ROOT = Path("/var/lib/solarbank-cost-optimiser")
ALERTS = Path("/run/solarbank-cost-optimiser/alerts")
GIB = 1024 ** 3
DATA_ALERT_BYTES = 20 * GIB
MIN_FREE_BYTES = 10 * GIB
FREE_FRACTION = 0.10
GROWTH_ALERT_BYTES = 2 * GIB
TELEMETRY_CAP_BYTES = 200 * 1024 ** 2


def _load_alerts():
    path = Path(__file__).resolve().parents[1] / "src" / "solarbank_optimiser" / "alerts.py"       # stdlib only, so it can be loaded without the package or its dependencies
    spec = importlib.util.spec_from_file_location("solarbank_alerts", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def measure(root):
    data = telemetry = count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).parts
        if relative[:1] == ("backups",):
            continue
        size = path.stat().st_size
        data += size
        if relative[:1] == ("telemetry",) and path.suffix == ".wsp":
            telemetry += size; count += 1
    return data, telemetry, count


def main(root=ROOT, usage=shutil.disk_usage, alerts_dir=ALERTS, now=time.time, alerts=None, out=print):
    alerts = alerts or _load_alerts()
    root = Path(root)
    state_path = root / "storage-guard.json"
    try:
        before = json.loads(state_path.read_text())
    except (OSError, ValueError):
        before = {}
    data, telemetry, count = measure(root)
    disk = usage(root)
    total, free = getattr(disk, "total", None), disk.free
    low_free = max(MIN_FREE_BYTES, int(FREE_FRACTION * total)) if total else MIN_FREE_BYTES
    when = now()
    problems = {}
    if data > DATA_ALERT_BYTES:
        problems["storage-data-large"] = (f"The data folder is {data / GIB:.1f} GiB", f"The Solarbank data folder holds {data / GIB:.1f} GiB (alert level {DATA_ALERT_BYTES // GIB} GiB, backups not counted). Nothing has been stopped.")
    if free < low_free:
        problems["storage-low-free"] = ("Free disk space is low", f"Only {free / GIB:.1f} GiB is free on the data disk (alert below {low_free / GIB:.1f} GiB). Nothing has been stopped.")
    grew = data - before.get("data", data)
    if grew > GROWTH_ALERT_BYTES:
        problems["storage-growth"] = ("The data folder is growing unusually fast", f"The data folder grew by {grew / GIB:.1f} GiB since the last hourly check.")
    changed = before.get("telemetry_count") == count and before.get("telemetry_bytes") not in (None, telemetry)
    if changed or telemetry > TELEMETRY_CAP_BYTES:
        problems["storage-telemetry-size"] = ("The telemetry store changed size", f"The fixed-size telemetry store is {telemetry / 1024 ** 2:.1f} MB in {count} files; it should never change size (it was {before.get('telemetry_bytes', telemetry) / 1024 ** 2:.1f} MB).")
    for key in ("storage-data-large", "storage-low-free", "storage-growth", "storage-telemetry-size"):
        if key in problems:
            alerts.raise_alert(alerts_dir, key, problems[key][0], problems[key][1], when)
        else:
            alerts.clear_alert(alerts_dir, key)
    try:
        temporary = state_path.with_name(state_path.name + ".tmp")
        temporary.write_text(json.dumps({"at": when, "data": data, "telemetry_bytes": telemetry, "telemetry_count": count}))
        os.replace(temporary, state_path)
    except OSError:
        pass
    out("Storage: " + (", ".join(sorted(problems)) + " raised (nothing stopped)" if problems else f"within limits ({data / GIB:.2f} GiB of data, {free / GIB:.0f} GiB free)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
