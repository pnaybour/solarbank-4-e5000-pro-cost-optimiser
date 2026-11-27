"""Read-only operating measurements for the Pi, and the one tidy-up that deletes anything (old releases, dry run first). Run through `solarbank ops ...` and `pi-tools.sh`.

`io`      what each Solarbank service (and the other busiest writers) actually wrote to disk over a sampling period, from /proc/<pid>/io, with the per-day equivalent.
`memory`  resident, proportional and swapped memory of each service, from /proc/<pid>/smaps_rollup (PSS counts shared pages once, so the figures add up).
`prune-releases`  old release folders (each carries its own virtual environment, about a gigabyte): lists what could go and how much space that frees; deletes only with --apply.
Nothing here writes while it measures: a monitor that wrote to disk would disturb what it measures."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

SERVICES = ("collector", "control", "providers", "shadow", "web")
KEEP_NEWEST = 3                  # releases kept besides the current one and the rollback floor
SAFE_AGE_S = 2 * 3600            # a folder touched in the last two hours may be a deploy in progress: never deleted


def _kv(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def service_pids(runner=None):
    """{service name: main pid} for the Solarbank units that are running."""
    run = runner or (lambda unit: subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit], capture_output=True, text=True, timeout=10).stdout.strip())
    pids = {}
    for name in SERVICES:
        try:
            pid = int(run(f"solarbank-optimiser-{name}.service") or 0)
        except (ValueError, OSError, subprocess.SubprocessError):
            pid = 0
        if pid > 0:
            pids[name] = pid
    return pids


def write_bytes(pid, proc="/proc"):
    """Bytes the process has caused to be written to the storage layer (not merely passed to write()), or None."""
    value = _kv(Path(proc) / str(pid) / "io").get("write_bytes")
    return int(value) if value is not None and value.isdigit() else None


def io_rates(pids, seconds, proc="/proc", sleep=time.sleep, clock=time.monotonic):
    """[(name, KB/s, GB per day if it stayed like this)] over `seconds`, busiest first."""
    first = {name: write_bytes(pid, proc) for name, pid in pids.items()}
    t0 = clock()
    sleep(seconds)
    elapsed = max(clock() - t0, 1e-6)
    out = []
    for name, pid in pids.items():
        a, b = first[name], write_bytes(pid, proc)
        if a is None or b is None or b < a:
            continue
        rate = (b - a) / elapsed
        out.append((name, rate / 1024, rate * 86400 / 1e9))
    return sorted(out, key=lambda r: -r[1])


def memory_figures(pids, proc="/proc"):
    """[(name, rss MB, pss MB, swap MB)] from smaps_rollup, largest PSS first."""
    out = []
    for name, pid in pids.items():
        data = _kv(Path(proc) / str(pid) / "smaps_rollup")
        try:
            out.append((name, int(data["Rss"].split()[0]) / 1024, int(data["Pss"].split()[0]) / 1024, int(data.get("Swap", "0 kB").split()[0]) / 1024))
        except (KeyError, ValueError, IndexError):
            continue
    return sorted(out, key=lambda r: -r[2])


def format_io(rows, seconds):
    lines = [f"Disk writes over {seconds:.0f} s (what each service caused to reach the disk; page-cache merging included):"]
    lines += [f"  {name:10s} {kbs:8.1f} KB/s   {gb:6.2f} GB/day if it stayed like this" for name, kbs, gb in rows]
    total = sum(r[1] for r in rows)
    lines.append(f"  {'total':10s} {total:8.1f} KB/s   {total * 1024 * 86400 / 1e9:6.2f} GB/day")
    return "\n".join(lines)


def format_memory(rows):
    lines = ["Memory per service (RSS is what the process holds; PSS counts shared pages once, so PSS adds up; swap is what has been pushed out):"]
    lines += [f"  {name:10s} RSS {rss:6.0f} MB   PSS {pss:6.0f} MB   swap {swap:6.0f} MB" for name, rss, pss, swap in rows]
    lines.append(f"  {'total':10s} RSS {sum(r[1] for r in rows):6.0f} MB   PSS {sum(r[2] for r in rows):6.0f} MB   swap {sum(r[3] for r in rows):6.0f} MB")
    return "\n".join(lines)


# ---- old releases -------------------------------------------------------------------------------------------------------------------------------
def _size(path):
    total = 0
    for base, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def release_plan(root, state, now=None, keep_newest=KEEP_NEWEST):
    """(keep, remove): lists of (name, bytes, why) for the release folders under root/releases. Kept: the one `current` points at, the rollback floor named in the state folder,
    the newest few that are not newer than current, and anything touched in the last two hours."""
    now = time.time() if now is None else now
    releases = Path(root) / "releases"
    names = sorted(p.name for p in releases.iterdir() if p.is_dir() and not p.is_symlink()) if releases.is_dir() else []
    current = Path(os.path.realpath(Path(root) / "current")).name if (Path(root) / "current").exists() else None
    try:
        floor = (Path(state) / "oldest-compatible-release").read_text().strip()
    except OSError:
        floor = None
    older = [n for n in names if current is None or n <= current]                    # names start with a timestamp, so they sort by time
    newest_old = set(older[-(keep_newest + 1):])
    keep, remove = [], []
    for name in names:
        path = releases / name
        reason = None
        if name == current:
            reason = "current"
        elif name == floor:
            reason = "the rollback floor"
        elif name in newest_old:
            reason = "one of the newest to roll back to"
        elif now - path.stat().st_mtime < SAFE_AGE_S:
            reason = "touched in the last two hours: possibly a deploy in progress"
        (keep if reason else remove).append((name, _size(path), reason or ("never activated (newer than current)" if current and name > current else "old")))
    return keep, remove


def format_release_plan(keep, remove, apply):
    mb = lambda b: f"{b / 1e6:8.0f} MB"
    lines = [f"Kept ({len(keep)}):"] + [f"  {n}  {mb(b)}  {why}" for n, b, why in keep]
    lines += [f"{'Removed' if apply else 'Would remove'} ({len(remove)}, {sum(b for _n, b, _w in remove) / 1e9:.1f} GB):"] + [f"  {n}  {mb(b)}  {why}" for n, b, why in remove]
    if not apply:
        lines.append("Nothing was deleted. Add --apply to delete the ones listed (run it with sudo: the releases belong to root).")
    return "\n".join(lines)


def prune_releases(root, state, apply=False, now=None, keep_newest=KEEP_NEWEST):
    keep, remove = release_plan(root, state, now, keep_newest)
    if apply:
        for name, _b, _w in remove:
            shutil.rmtree(Path(root) / "releases" / name)
    return keep, remove
