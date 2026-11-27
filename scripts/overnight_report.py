"""Read-only overnight report for the Pi: what the shadow controller decided, what the battery and grid actually did, and yesterday's daily record.

Run on the Pi (needs root to read the private state folder):
    sudo /opt/solarbank-cost-optimiser/current/.venv/bin/python ~/solarbank-src/scripts/overnight_report.py

Options: --night YYYY-MM-DD (the evening the night starts; default: last night), --start HH:MM and --end HH:MM (London, default 22:00 to 07:00).
Nothing here writes to the database, the logs or the equipment. Standard library only.
"""
import argparse
import json
import sqlite3
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
STATE = Path("/var/lib/solarbank-cost-optimiser")


def london(ts, fmt="%a %H:%M:%S"):
    return datetime.fromtimestamp(ts, LONDON).strftime(fmt)


def window(night, start, end):
    first = datetime.combine(night, start, LONDON)
    last = datetime.combine(night + timedelta(days=1) if end <= start else night, end, LONDON)
    return first.timestamp(), last.timestamp()


def commands(state, t0, t1):
    """Controller command changes in the window, oldest first, from the change log and its rotated copy."""
    rows = []
    for path in (state / "control-commands.jsonl.1", state / "control-commands.jsonl"):
        try:
            with open(path) as handle:
                for line in handle:
                    if '"command"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("event") == "command" and t0 <= entry.get("at", 0) < t1:
                        rows.append(entry)
        except OSError:
            pass
    return sorted(rows, key=lambda e: e["at"])


def print_commands(rows):
    print("== CONTROLLER DECISIONS (shadow: recorded, not sent)")
    if not rows:
        print("  none in this period")
        return
    i = 0
    while i < len(rows):
        e = rows[i]
        c = e["command"]
        params = c.get("params") or {}
        if c["channel"] == "ac_charge" and c["action"] == "CHARGE_LIMIT":
            j = i                                            # collapse the 250 W soft-start ramp into one line
            while (j + 1 < len(rows) and rows[j + 1]["command"]["channel"] == "ac_charge" and rows[j + 1]["command"]["action"] == "CHARGE_LIMIT"
                   and rows[j + 1]["at"] - rows[j]["at"] <= 5 and (rows[j + 1]["command"].get("params") or {}).get("target_soc") == params.get("target_soc")):
                j += 1
            last = rows[j]["command"].get("params") or {}
            ramp = f"{params.get('watts')} W" if j == i else f"ramp {params.get('watts')} to {last.get('watts')} W over {rows[j]['at'] - e['at']:.0f} s"
            target = params.get("target_soc")
            print(f"  {london(e['at'])}  charge  {ramp}" + (f", target {target:.0%}" if isinstance(target, (int, float)) else ""))
            i = j + 1
            continue
        print(f"  {london(e['at'])}  {c['channel']:<9} {c['action']:<9} {str(c.get('reason', ''))[:80]}")
        i += 1


def half_hours(db, t0, t1):
    """Per half-hour: SoC at start and end, mean grid import (meter), mean EV power, mean house load."""
    metrics = ("soc", "grid_ct_power", "grid_power", "ev_power", "household_power", "battery_power")
    data = {m: [] for m in metrics}
    for metric in metrics:
        for at, value in db.execute("SELECT sample_at, json_extract(payload,'$.value') FROM observations "
                                    "WHERE metric=? AND sample_at>=? AND sample_at<? AND json_extract(payload,'$.quality')='ok' ORDER BY sample_at",
                                    (metric, t0, t1)):
            if isinstance(value, (int, float)):
                data[metric].append((at, value))
    grid = "grid_ct_power" if data["grid_ct_power"] else "grid_power"
    print(f"== HALF-HOURS (London; grid from {'the smart meter' if grid == 'grid_ct_power' else 'the Solarbank'}; positive = import)")
    print("  slot    SoC start>end   grid kW   ~kWh   car kW   house kW   battery kW")
    total, slot = 0.0, t0
    while slot < t1:
        end = slot + 1800
        part = {m: [v for at, v in data[m] if slot <= at < end] for m in metrics}
        mean = lambda m: sum(part[m]) / len(part[m]) if part[m] else None
        fmt = lambda v, w=7: f"{v:>{w}.2f}" if v is not None else " " * (w - 1) + "-"
        soc = f"{part['soc'][0]:>4.0%}>{part['soc'][-1]:<4.0%}" if part["soc"] else "    -    "
        g = mean(grid)
        if g is not None:
            total += g * 0.5
        print(f"  {london(slot, '%H:%M')}   {soc:<13} {fmt(g)}  {fmt(g * 0.5 if g is not None else None, 5)}  {fmt(mean('ev_power'))}   {fmt(mean('household_power'))}   {fmt(mean('battery_power'))}")
        slot = end
    print(f"  Estimated grid energy over the period: {total:.2f} kWh (from mean power; the meter's own half-hour totals are definitive)")


def plans(db, t0, t1):
    """Archived planner plans issued in the period. Unchanged plans are re-archived at most every 30 minutes, so this shows each change."""
    print("== PLANS ARCHIVED (window London, target)")
    rows = db.execute("SELECT acquired_at, json_extract(payload,'$.summary.next_window'), json_extract(payload,'$.summary.grid_target'), "
                      "json_extract(payload,'$.summary.window_targets'), json_extract(payload,'$.summary.valid_until') FROM issuances "
                      "WHERE acquired_at>=? AND acquired_at<? AND json_extract(payload,'$.kind')='shadow_plan' ORDER BY acquired_at",
                      (t0 - 3600, t1)).fetchall()
    if not rows:
        print("  none")
    for at, window_json, target, targets_json, valid in rows:
        window = json.loads(window_json) if window_json else None
        span = f"{london(window['start'], '%H:%M')}-{london(window['end'], '%H:%M')} to {window['target']:.0%}" if window else "no window"
        others = json.loads(targets_json) if targets_json else []
        extra = "; ".join(f"{london(w['start'], '%a %H:%M')}-{london(w['end'], '%H:%M')} {w['target']:.0%}" for w in others)
        print(f"  {london(at)}  valid to {london(valid, '%H:%M:%S') if valid else '-'}  next {span}" + (f"  [all: {extra}]" if extra else ""))


def attempts(db, t0, t1):
    """Planner attempt outcomes. The latest attempt lives in RAM and is saved to the database about every 30 minutes, so these are samples."""
    print("== PLANNER ATTEMPTS (sampled about every 30 minutes)")
    rows = db.execute("SELECT known_at, payload FROM setting_versions WHERE name='shadow_attempt' AND known_at>=? AND known_at<? ORDER BY known_at",
                      (t0 - 3 * 3600, t1)).fetchall()
    if not rows:
        print("  none saved")
    for known_at, payload in rows:
        try:
            a = json.loads(payload)
        except ValueError:
            continue
        at = a.get("time") or known_at
        print(f"  {london(at)}  {a.get('status', '?'):<11} {str(a.get('reason', ''))[:100]}")


def providers(db, t0, t1):
    """Every recorded provider attempt that did not succeed, from 12 hours before the period (weather, prices and smart slots)."""
    print("== PROVIDER FAILURES (weather, prices, slots; from 12 hours before)")
    found = False
    for name in ("provider_weather", "provider_prices", "provider_slots"):
        for known_at, payload in db.execute("SELECT known_at, payload FROM setting_versions WHERE name=? AND known_at>=? AND known_at<? ORDER BY known_at",
                                            (name, t0 - 12 * 3600, t1)):
            try:
                h = json.loads(payload)
            except ValueError:
                continue
            if h.get("status") != "ok":
                found = True
                print(f"  {london(h.get('last_attempt') or known_at)}  {name[9:]:<8} {h.get('status')}  {h.get('reason')}  (failures in a row: {h.get('failures')})")
    if not found:
        print("  none recorded")


def daily(db, day):
    print(f"== DAILY RECORD {day.isoformat()}")
    row = db.execute("SELECT payload FROM settings WHERE name=?", ("daily_record:" + day.isoformat(),)).fetchone()
    if not row:
        print("  not recorded yet")
        return
    r = json.loads(row[0])
    money = lambda v: f"£{v:.2f}" if isinstance(v, (int, float)) else "-"
    kwh = lambda v: f"{v:.2f} kWh" if isinstance(v, (int, float)) else "-"
    print(f"  complete: {r.get('complete')} ({r.get('minutes_covered')} of {r.get('minutes_expected')} minutes)")
    print(f"  used {kwh(r.get('consumption_kwh'))}, imported {kwh(r.get('import_kwh'))}, exported {kwh(r.get('export_kwh'))}")
    print(f"  cost {money(r.get('actual_cost_gbp'))} against baseline {money(r.get('baseline_cost_gbp'))}: saving {money(r.get('saving_gbp'))} (estimated)")


def planner_log(t0, t1):
    print("== PLANNER AND CONTROLLER LOG (warnings, withdrawals, expiries)")
    since, until = london(t0, "%Y-%m-%d %H:%M:%S"), london(t1, "%Y-%m-%d %H:%M:%S")
    try:
        out = subprocess.run(["journalctl", "-u", "solarbank-optimiser-shadow", "-u", "solarbank-optimiser-control", "--since", since, "--until", until,
                              "--no-pager", "-o", "short"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        print("  journal not available")
        return
    keys = ("unavailable", "withdrawn", "replan", "expired", "error", "fail", "traceback", "took")
    lines = [l for l in out.splitlines() if any(k in l.lower() for k in keys)]
    for line in lines[-25:]:
        print("  " + line[:160])
    if not lines:
        print("  nothing notable")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only overnight report")
    parser.add_argument("--night", type=date.fromisoformat, default=None, help="evening the night starts (default: last night)")
    parser.add_argument("--start", type=time.fromisoformat, default=time(22, 0))
    parser.add_argument("--end", type=time.fromisoformat, default=time(7, 0))
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args(argv)
    today = datetime.now(LONDON).date()
    night = args.night or today - timedelta(days=1)
    t0, t1 = window(night, args.start, args.end)
    print(f"Overnight report: {london(t0, '%a %d %b %H:%M')} to {london(t1, '%a %d %b %H:%M')} (London)")
    print_commands(commands(args.state, t0, t1))
    db = sqlite3.connect(f"file:{args.state / 'controller.sqlite3'}?mode=ro", uri=True, timeout=10)
    try:
        half_hours(db, t0, t1)
        plans(db, t0, t1)
        attempts(db, t0, t1)
        providers(db, t0, t1)
        daily(db, night)
    finally:
        db.close()
    planner_log(t0, t1)


if __name__ == "__main__":
    main()
