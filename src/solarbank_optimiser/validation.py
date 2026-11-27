"""B3: what the Anker actually did, minute by minute, beside what the controller would have asked for (shadow weeks).

Two records are compared. The Anker's behaviour comes from the one-minute readings (battery, grid and household power, battery level), classified with the same
rules as the dashboard's "Anker now" line. The controller's intent comes from its command change log (one line per change of command, written by the controller
in shadow mode): for each minute, the newest 'ac_charge' command (CHARGE_LIMIT = wants grid charging; WITHDRAW or STOP = does not) and the newest 'discharge'
command (INHIBIT or ALLOW) in force. A minute counts as agreeing when the Anker did what the controller's request would have produced:

* the controller asked for grid charging: the Anker should be charging from the grid (or sitting idle at the requested level, which is the target reached);
* the controller did not ask for charging: the Anker should NOT be charging from the grid (solar charging, discharging and idle are all fine);
* the controller inhibited discharge: the Anker should NOT be discharging.

Every run of disagreeing minutes is listed with its start, end, what each side did and the battery level, so the weekly report can name every difference.
Nothing here sends a command; it only reads."""
import bisect
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from .anker_view import describe
from .daily_record import minute_rows, _put, _rounded
from .energy import LONDON, day_bounds

VERSION = 1
PREFIX = "validation:"
MAX_DIFFERENCES = 200            # per day; the count beyond is kept
TARGET_REACHED_MARGIN = 0.02     # idle within this of the requested level counts as "reached it"
CHARGE_ACTIONS = {"CHARGE_LIMIT"}
NO_CHARGE_ACTIONS = {"STOP", "WITHDRAW"}
KINDS = {
    "not_charging": "Controller asked for grid charging; the Anker was not charging from the grid",
    "charging_unasked": "The Anker was charging from the grid; the controller had not asked for charging",
    "discharging_inhibited": "The Anker was discharging while the controller would have inhibited discharge",
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ---- the controller's side --------------------------------------------------------------------------------------------------------------------
def read_events(paths):
    """Command-change events from the controller's JSON-lines log files (oldest file first): [(time, channel, action, target_soc)], sorted."""
    events = []
    for path in paths:
        try:
            with open(path, "r", errors="replace") as handle:
                for line in handle:
                    if '"command"' not in line:          # the first lines of an older log have no "event" field, so look for the command itself
                        continue
                    try:
                        entry = json.loads(line)
                        command = entry["command"]
                        at = float(entry["at"])
                        events.append((at, command["channel"], command["action"], (command.get("params") or {}).get("target_soc")))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            continue
    events.sort(key=lambda e: e[0])
    return events


class Intent:
    """The controller's request in force at any moment, from its command-change events."""

    def __init__(self, events):
        self.charge = [(at, ("charge" if action in CHARGE_ACTIONS else "no_charge", target)) for at, channel, action, target in events
                       if channel == "ac_charge" and (action in CHARGE_ACTIONS or action in NO_CHARGE_ACTIONS)]
        self.discharge = [(at, action.lower()) for at, channel, action, _ in events if channel == "discharge" and action in ("ALLOW", "INHIBIT")]
        self._ct, self._dt = [at for at, _ in self.charge], [at for at, _ in self.discharge]

    def at(self, t):
        """(charge intent, target level or None, discharge intent) at time t; None where the controller had said nothing yet."""
        i, j = bisect.bisect_right(self._ct, t) - 1, bisect.bisect_right(self._dt, t) - 1
        charge, target = self.charge[i][1] if i >= 0 else (None, None)
        return charge, target, (self.discharge[j][1] if j >= 0 else None)


# ---- the arithmetic (pure) --------------------------------------------------------------------------------------------------------------------
def actual_state(battery_kw, grid_kw, house_kw):
    """'grid_charge', 'solar_charge', 'discharge' or 'idle' (None when the readings are unusable)."""
    view = describe(battery_kw, grid_kw, house_kw)
    if view["state"] == "charging":
        return "grid_charge" if view["from_grid"] else "solar_charge"
    return {"discharging": "discharge", "idle": "idle"}.get(view["state"])


def judge(charge, target, discharge, actual, soc):
    """The kind of disagreement for one minute, or None when they agree (or nothing can be said)."""
    if actual is None:
        return None
    if charge == "charge" and actual != "grid_charge":
        reached = actual == "idle" and _finite(soc) and _finite(target) and soc >= target - TARGET_REACHED_MARGIN
        return None if reached else "not_charging"
    if charge == "no_charge" and actual == "grid_charge":
        return "charging_unasked"
    if discharge == "inhibit" and actual == "discharge":
        return "discharging_inhibited"
    return None


def compute_day(day, minutes, intent, now=0.0):
    start, end = day_bounds(day)
    matrix, differences = {}, []
    compared = agreeing = no_intent = no_actual = 0
    open_run = None
    for t in range(int(start), int(end), 60):
        if not all(t in minutes[m] for m in ("battery_power", "grid_power", "household_power")):
            no_actual += 1
            open_run = _close(open_run, differences)
            continue
        charge, target, discharge = intent.at(t + 30)
        if charge is None and discharge is None:
            no_intent += 1
            open_run = _close(open_run, differences)
            continue
        actual = actual_state(minutes["battery_power"][t], minutes["grid_power"][t], minutes["household_power"][t])
        soc = minutes["soc"].get(t)
        label = (charge or "unknown") + ("+inhibit" if discharge == "inhibit" else "")
        matrix.setdefault(label, {})
        matrix[label][actual or "unknown"] = matrix[label].get(actual or "unknown", 0) + 1
        if actual is None:
            no_actual += 1
            open_run = _close(open_run, differences)
            continue
        compared += 1
        kind = judge(charge, target, discharge, actual, soc)
        if kind is None:
            agreeing += 1
            open_run = _close(open_run, differences)
            continue
        if open_run and open_run["kind"] == kind and open_run["actual"] == actual and open_run["end"] == t:
            open_run["end"] = t + 60; open_run["minutes"] += 1; open_run["kw_sum"] += minutes["battery_power"][t]
            if soc is not None:
                open_run["soc_end"] = soc
        else:
            open_run = _close(open_run, differences)
            open_run = {"kind": kind, "start": t, "end": t + 60, "minutes": 1, "intent": label, "actual": actual, "kw_sum": minutes["battery_power"][t],
                        "soc_start": soc, "soc_end": soc, "target": target}
    _close(open_run, differences)
    record = {
        "version": VERSION, "date": day.isoformat(), "computed_at": now, "minutes_in_day": int((end - start) // 60), "minutes_compared": compared,
        "minutes_agreeing": agreeing, "minutes_without_controller_record": no_intent, "minutes_without_readings": no_actual,
        "agreement_pct": (agreeing / compared * 100) if compared else None,
        "disagreeing_minutes": compared - agreeing, "matrix": matrix,
        "differences": differences[:MAX_DIFFERENCES], "differences_total": len(differences),
        "by_kind": {k: sum(d["minutes"] for d in differences if d["kind"] == k) for k in KINDS if any(d["kind"] == k for d in differences)},
    }
    return _rounded(record)


def _close(run, differences):
    if run:
        run["mean_battery_kw"] = run.pop("kw_sum") / run["minutes"]
        differences.append(run)
    return None


# ---- storing and reporting ------------------------------------------------------------------------------------------------------------------
def default_log_paths(store):
    """The controller writes its change log next to the database (control-commands.jsonl, with one rotated file)."""
    base = Path(store.path).parent / "control-commands.jsonl"
    return [base.with_name(base.name + ".1"), base]


def record_validation_days(store, now, log_paths, lookback=3):
    """Score every finished day in the last few that has no current record. Reads the controller's change log (all the files given) once.
    Returns the dates computed. Needs the day's minute rows, so it must run while they exist (the daily-record pass calls it right after scoring)."""
    computed, events, intent = [], None, None
    today = datetime.fromtimestamp(now, LONDON).date()
    for back in range(lookback, 0, -1):
        day = today - timedelta(days=back)
        start, end = day_bounds(day)
        if now < end + 300:
            continue
        existing = store.get_setting(PREFIX + day.isoformat())[1]
        if existing and existing.get("version") == VERSION:
            continue
        minutes = minute_rows(store, start, end)
        if not any(minutes[m] for m in minutes):
            continue
        if intent is None:
            events = read_events(log_paths)
            intent = Intent(events)
        _put(store, PREFIX + day.isoformat(), compute_day(day, minutes, intent, now), now)
        computed.append(day)
    return computed


def report(store, today, days=7):
    """The weekly report: the last `days` finished days, newest first, with every difference listed."""
    records = []
    for back in range(1, days + 1):
        record = store.get_setting(PREFIX + (today - timedelta(days=back)).isoformat())[1]
        if record:
            records.append(record)
    compared = sum(r["minutes_compared"] for r in records)
    agreeing = sum(r["minutes_agreeing"] for r in records)
    return {
        "days": [{k: r[k] for k in ("date", "minutes_compared", "minutes_agreeing", "agreement_pct", "disagreeing_minutes", "differences_total",
                                    "minutes_without_controller_record", "minutes_without_readings", "by_kind", "matrix")} for r in records],
        "minutes_compared": compared, "minutes_agreeing": agreeing, "agreement_pct": round(agreeing / compared * 100, 2) if compared else None,
        "differences": [{**d, "date": r["date"]} for r in records for d in r["differences"]],
        "kinds": KINDS,
    }


def all_differences(store, today, days=366):
    """Every stored difference, oldest first, for the CSV."""
    rows = []
    for back in range(days, 0, -1):
        record = store.get_setting(PREFIX + (today - timedelta(days=back)).isoformat())[1]
        if record:
            rows += [{**d, "date": record["date"]} for d in record["differences"]]
    return rows
