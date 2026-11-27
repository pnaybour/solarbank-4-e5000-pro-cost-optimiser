"""Read-only test of Octopus smart slots against what the car actually did (issue #34).

The charger is scheduled by Octopus (Intelligent Octopus Go), not by the car, so about six hours of SMART slots are listed every day whether or not the car uses them.
This report answers three questions from what the system recorded:
  1. Is about six hours listed every day, whatever the car needs?              (per allowance day, 12:00 to 12:00 London)
  2. How much of the listed time does the car actually charge in?               (per allowance day and per slot)
  3. When a charge ends, what happens to the slots still listed after it?       (per charging session: kept, shortened or removed, and when)

Run on the Pi:
    sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/python /opt/solarbank-cost-optimiser/current/scripts/slot_report.py            # last 7 days
    ... slot_report.py --days 3

Dispatches come from the archived slot snapshots (one is kept when the list changes, and at least every half hour), car power from the stored readings, and the charging
sessions from the charger history; all are kept for about a week, so run it daily and keep what it prints. Nothing is written anywhere. Standard library only."""
import argparse
import json
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
STATE = Path("/var/lib/solarbank-cost-optimiser")
HALF = 1800
SNAPSHOT_MAX_AGE_S = 2400            # a slot snapshot is re-archived at least every half hour, so a list older than this is a gap in the evidence, not "unchanged"
PROBES = (("before the stop", -60), ("10 min after", 600), ("1 h after", 3600))


def london(ts, fmt="%a %d %b %H:%M"):
    return datetime.fromtimestamp(ts, LONDON).strftime(fmt)


def merge(intervals):
    out = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def in_house_window(ts):
    local = datetime.fromtimestamp(ts + 1, LONDON)          # +1 s: a half-hour starting at 23:30 is inside
    minute = local.hour * 60 + local.minute
    return minute >= 23 * 60 + 30 or minute < 5 * 60 + 30


def load_snapshots(db, t0, t1):
    """[(acquired_at, [(start, end), ...])] oldest first, from the archived slot lists."""
    out = []
    for acquired, payload in db.execute("SELECT acquired_at, payload FROM issuances WHERE acquired_at>=? AND acquired_at<? AND json_extract(payload,'$.kind')='slots' "
                                        "ORDER BY acquired_at", (t0, t1)):
        out.append((acquired, [(r["start"], r["end"]) for r in (json.loads(payload).get("intervals") or [])]))
    return out


def state_at(snapshots, when):
    """The list of slots known at `when`: the newest snapshot not after it, or None if there is none or it is too old to speak for that moment."""
    best = None
    for acquired, intervals in snapshots:
        if acquired <= when:
            best = (acquired, intervals)
        else:
            break
    if best is None or when - best[0] > SNAPSHOT_MAX_AGE_S:
        return None
    return best[1]


def remaining_after(intervals, end):
    """Seconds of listed slot time from `end` onwards."""
    return sum(max(0.0, e - max(s, end)) for s, e in intervals)


def sessions(db, t0, t1):
    out = []
    for sid, start, end, payload in db.execute("SELECT id, started_at, ended_at, payload FROM charger_sessions WHERE ended_at IS NOT NULL AND ended_at>=? AND ended_at<? "
                                               "ORDER BY ended_at", (t0, t1)):
        info = json.loads(payload)
        out.append({"id": sid, "start": start, "end": end, "kwh": info.get("energy_kwh"), "peak_kw": info.get("peak_kw"), "partial": info.get("partial")})
    return out


def after_the_stop(snapshots, end):
    """For one session end: the slot time still listed beyond it at each probe, and a one-word verdict."""
    seen = []
    for label, offset in PROBES:
        state = state_at(snapshots, end + offset)
        seen.append((label, None if state is None else remaining_after(state, end)))
    before = seen[0][1]
    later = [value for _, value in seen[1:] if value is not None]
    if before is None or not later:
        verdict = "unknown (no slot snapshot close enough)"
    elif before <= 0:
        verdict = "nothing was listed beyond the stop"
    elif later[0] <= 0:
        verdict = "REMOVED"
    elif later[0] < before - HALF / 2:
        verdict = "SHORTENED"
    else:
        verdict = "KEPT (listed although the car had stopped)"
    return seen, verdict


def car_halves(db, t0, t1, source=None):
    """half-hour start -> the highest car power (kW) seen in it. Readings live in the telemetry files since the single-store change (6 November); `source` is their minute reader.
    Rows an older release left in the database are still read, so a window that spans the change is complete."""
    charging = {}
    if source is not None:
        for minute, item in source.minutes("ev_power", t0, t1).items():
            if item.mean is not None:
                slot = int(minute // HALF) * HALF
                charging[slot] = max(charging.get(slot, 0.0), item.mean)
    for at, value in db.execute("SELECT sample_at, COALESCE(json_extract(payload,'$.window_max'), json_extract(payload,'$.value')) FROM observations "
                                "WHERE metric='ev_power' AND sample_at>=? AND sample_at<? AND json_extract(payload,'$.quality')='ok'", (t0, t1)):
        if isinstance(value, (int, float)):
            slot = int(at // HALF) * HALF
            charging[slot] = max(charging.get(slot, 0.0), value)
    return charging


def report(db, now, days=7, threshold_kw=1.0, out=print, source=None):
    first_noon = datetime.combine((now - timedelta(days=days)).date(), time(12, 0), LONDON)
    t0, t1 = first_noon.timestamp(), now.timestamp()
    snapshots = load_snapshots(db, t0 - 86400, t1)
    seen = {(s, e) for _, intervals in snapshots for s, e in intervals}
    slots = merge(seen)
    charging = car_halves(db, t0, t1, source)
    out(f"Smart slots against measured car charging, {london(t0)} to {london(t1)} (London)")
    out(f"Slot snapshots read: {len(snapshots)}; distinct SMART dispatches seen: {len(seen)}; car readings cover {len(charging)} half-hours")
    out(f"Car counts as charging in a half-hour if it reached {threshold_kw} kW.\n")
    day = first_noon
    while day.timestamp() < t1:
        d0, d1 = day.timestamp(), (day + timedelta(days=1)).timestamp()
        halves = range(int(d0), int(min(d1, t1)), HALF)
        dispatched = [h for h in halves if any(s < h + HALF and e > h for s, e in slots)]          # any overlap: billed per half-hour
        charged = [h for h in halves if charging.get(h, 0.0) >= threshold_kw]
        both = [h for h in dispatched if h in charged]
        out(f"== Allowance day from {london(d0)}")
        out(f"  dispatched {len(dispatched) / 2:.1f} h ({sum(in_house_window(h) for h in dispatched) / 2:.1f} h inside 23:30-05:30); "
            f"car charged {len(charged) / 2:.1f} h; charged during a dispatch {len(both) / 2:.1f} h; dispatched but car idle {(len(dispatched) - len(both)) / 2:.1f} h")
        for s, e in slots:
            if s < d1 and e > d0:
                inside = [h for h in dispatched if s < h + HALF and e > h]
                used = sum(1 for h in inside if h in charged)
                out(f"    slot {london(s, '%a %H:%M')}-{london(e, '%H:%M')}  {(e - s) / 3600:.1f} h  car charging in {used} of {len(inside)} half-hours")
        if not dispatched and not charged:
            out("    no dispatches and no car charging")
        day += timedelta(days=1)
    found = sessions(db, t0, t1)
    out(f"\nWhat happened to the listed slots when a charge ended ({len(found)} finished sessions)")
    if not found:
        out("  no finished charging session in this period")
    for s in found:
        seen_after, verdict = after_the_stop(snapshots, s["end"])
        kwh = "?" if s["kwh"] is None else f"{s['kwh']:.1f}"
        out(f"  charge {london(s['start'], '%a %H:%M')} to {london(s['end'], '%a %H:%M')}  {kwh} kWh" + ("  (partial record)" if s["partial"] else ""))
        for label, value in seen_after:
            out(f"      {label:>15}: " + ("no snapshot close enough" if value is None else f"{value / 3600:.1f} h of slots still listed after the stop"))
        out(f"      -> {verdict}")
    return {"sessions": found, "snapshots": len(snapshots)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Smart slots against measured car charging (read-only)")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--threshold-kw", type=float, default=1.0, help="car counts as charging in a half-hour if its power reaches this")
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args(argv)
    db = sqlite3.connect(f"file:{args.state / 'controller.sqlite3'}?mode=ro", uri=True, timeout=10)
    from solarbank_optimiser.store import Store               # the car readings are in the telemetry files next to the database (read only)
    source = Store(args.state / "controller.sqlite3").minute_source()
    try:
        report(db, datetime.now(LONDON), args.days, args.threshold_kw, source=source)
    finally:
        db.close()


if __name__ == "__main__":
    main()
