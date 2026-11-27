"""4a.5: side-by-side comparison of the direct readers with the existing monitor (read-only, light on the disk).

`run` samples the live file (direct readings, in RAM) and the monitor's page about twice a second and appends ONE short JSON
line per minute. `report` summarises the lines and says which of the plan's conditions the run has covered so far. Metrics
for devices still supplied by the monitor are not compared (they would trivially agree).
"""
import argparse
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import signal
import statistics
import threading
import time
import urllib.request
from zoneinfo import ZoneInfo

from .shelly_channels import CAR, SOLARBANK_AC, from_monitor

LONDON = ZoneInfo("Europe/London")
# metric -> (device, function from the monitor's document to the same unit as the stored metric)
PAIRS = {
    "soc": ("solarbank", lambda d: d["solarbank"]["sample"]["soc"] / 100),
    "pv_power": ("solarbank", lambda d: d["solarbank"]["sample"]["pv"] / 1000),
    "household_power": ("solarbank", lambda d: d["solarbank"]["sample"]["load"] / 1000),
    "grid_power": ("solarbank", lambda d: d["solarbank"]["sample"]["grid"] / 1000),
    "battery_power": ("solarbank", lambda d: d["solarbank"]["sample"]["battery"] / 1000),
    "ac_power": ("shelly", lambda d: from_monitor(d["shelly"]["sample"])[SOLARBANK_AC] / 1000),     # the monitor says ch1; from_monitor translates
    "ev_power": ("shelly", lambda d: from_monitor(d["shelly"]["sample"])[CAR] / 1000),            # the monitor says ch2
    "grid_ct_power": ("meter", lambda d: d["sample"]["main"] / 1000),
    "garage_power": ("meter", lambda d: d["sample"]["sub"] / 1000),
}
CHEAP_START, CHEAP_END = 23 * 60 + 30, 5 * 60 + 30       # the regular London night window
EV_ACTIVE_KW = 0.01                                        # the project's 10 W charging threshold
RECORD_VERSION = 2                                         # lines without it predate the freshness checks and are not counted as evidence
# Only battery percentage is a slowly stepping value (1% steps). Solar power is NOT: once the sun is up it moves second by second, so it is judged
# like the other power readings with the timing-tolerant match (checked on 74,321 real morning samples: 99.99% within timing). A stepped value may differ by exactly one step for a second or two
# while the two sources poll a moment apart; anything larger, or too many of them, is a real disagreement.
PROVISIONAL = {"stepped_metrics": ("soc",), "step": 0.0101, "stepped_min_exact_pct": 95, "power_mean_abs_kw": 0.01,
               "lag_match_pct": 99, "lag_samples": 4, "device_failure_pct": 1.0, "device_max_failure_run": 2,
               # evidence rules: a sample counts only if it is recent and new; a minute only if both sources really delivered
               "direct_fresh_s": 3.0, "monitor_fresh_s": 5.0, "min_reads_per_minute": 45, "min_pairs_per_minute": 45,
               "required_hours": 24, "required_coverage_pct": 95, "cheap_window_minutes": 240, "moving_metrics": ("ac_power", "ev_power"),
               "min_changes_per_minute": 1.0, "min_verdict_pairs": 600, "required_devices": ("solarbank", "shelly"), "max_future_s": 120}
# Two pollers a fraction of a second apart see a changing load at slightly different moments. A sample "matches within timing" if the direct value
# equals any of the monitor's last few values, or the other way round, so only genuine offsets, scaling or sign errors fail it.


def monitor_group(document, device):
    """The monitor's page keeps the meter at the top level and the other devices in their own groups."""
    return document if device == "meter" else (document or {}).get(device)


def monitor_fresh(document, device, now):
    group = monitor_group(document, device)
    try:
        return bool(group) and group.get("ok") is True and 0 <= now - float(group["sample"]["time"]) <= PROVISIONAL["monitor_fresh_s"]
    except (KeyError, TypeError, ValueError):
        return False


def in_cheap_window(epoch):
    t = datetime.fromtimestamp(epoch, LONDON)
    minutes = t.hour * 60 + t.minute
    return minutes >= CHEAP_START or minutes < CHEAP_END


class Accumulator:
    """Folds samples into per-minute records."""

    def __init__(self):
        self.minute = None
        self.last_counters = {}              # per device, kept across minutes (and across restarts of the collector)
        self._reset()

    def _reset(self):
        self.samples = 0
        self.monitor_ok = 0
        self.metrics = {}
        self.devices = {}
        self.ev_active = False
        self.times = {}                       # metric -> the distinct reading times seen this minute
        self.prev_direct, self.prev_monitor = {}, {}
        self.hist_direct = {k: deque(maxlen=PROVISIONAL["lag_samples"]) for k in PAIRS}
        self.hist_monitor = {k: deque(maxlen=PROVISIONAL["lag_samples"]) for k in PAIRS}

    def add(self, now, observations, devices_health, monitor_doc, direct_devices):
        """observations: {metric: row} from the live file; devices_health: its collector_health['devices'].

        A direct value counts only if it is OK and recent; a monitor value only if the monitor reported it OK with a recent
        timestamp. Stale or replayed data therefore produces no paired samples, no new readings and no qualifying minute."""
        minute = int(now // 60 * 60)
        if self.minute is not None and minute < self.minute:
            return None                                          # the clock stepped back: never write an earlier minute
        finished = None
        if self.minute is not None and minute != self.minute:
            finished = self.record()
            keep = (self.prev_direct, self.prev_monitor, self.hist_direct, self.hist_monitor)
            self._reset(); self.prev_direct, self.prev_monitor, self.hist_direct, self.hist_monitor = keep
        self.minute = minute
        self.samples += 1
        direct_devices = [d for d in direct_devices if d in ("solarbank", "meter", "shelly")]
        if direct_devices and all(monitor_fresh(monitor_doc, d, now) for d in direct_devices):
            self.monitor_ok += 1
        for metric, (device, monitor_value) in PAIRS.items():
            if device not in direct_devices:
                continue
            m = self.metrics.setdefault(metric, {"n": 0, "exact": 0, "lag_match": 0, "sum_abs": 0.0, "max_abs": 0.0, "direct_missing": 0,
                                                 "direct_stale": 0, "monitor_missing": 0, "monitor_stale": 0, "new_readings": 0,
                                                 "changes_direct": 0, "changes_monitor": 0})
            row = observations.get(metric)
            direct = None
            if row and row.get("quality") == "ok" and row.get("value") is not None:
                age = now - row.get("retrieved_at", -1e18)
                if -1.0 <= age <= PROVISIONAL["direct_fresh_s"]:
                    direct = row["value"]
                    seen = self.times.setdefault(metric, set())
                    if row["retrieved_at"] not in seen:          # a reading we have not seen before
                        seen.add(row["retrieved_at"]); m["new_readings"] += 1
                else:
                    m["direct_stale"] += 1
            other = None
            try:
                if monitor_fresh(monitor_doc, device, now):
                    other = monitor_value(monitor_doc)
                elif monitor_doc:
                    m["monitor_stale"] += 1
            except (KeyError, TypeError):
                other = None
            if direct is None and not (row and row.get("quality") == "ok" and row.get("value") is not None):
                m["direct_missing"] += 1
            if other is None and not monitor_doc:
                m["monitor_missing"] += 1
            if direct is not None and other is not None:
                diff = abs(direct - other)
                m["n"] += 1; m["sum_abs"] += diff; m["max_abs"] = max(m["max_abs"], diff); m["exact"] += diff < 1e-9
                near = lambda a, values: any(abs(a - v) < 1e-9 for v in values)
                m["lag_match"] += diff < 1e-9 or near(direct, self.hist_monitor[metric]) or near(other, self.hist_direct[metric])
            if direct is not None and metric in self.prev_direct and direct != self.prev_direct[metric]:
                m["changes_direct"] += 1
            if other is not None and metric in self.prev_monitor and other != self.prev_monitor[metric]:
                m["changes_monitor"] += 1
            if direct is not None:
                self.prev_direct[metric] = direct; self.hist_direct[metric].append(direct)
            if other is not None:
                self.prev_monitor[metric] = other; self.hist_monitor[metric].append(other)
            if metric == "ev_power" and direct is not None and direct > EV_ACTIVE_KW:
                self.ev_active = True
        for device in direct_devices:
            h = (devices_health or {}).get(device)
            if h:
                d = self.devices.setdefault(device, {"reads": 0, "failures": 0, "missed": 0, "latencies": {}, "max_fail_streak": 0})
                last = self.last_counters.get(device)
                for key, field in (("reads", "seq"), ("failures", "failures"), ("missed", "missed")):
                    value = h[field] or 0
                    if last is None:
                        delta = 1 if key == "reads" and value else 0       # first sight: count the read we are looking at, not history
                    elif value >= last[field]:
                        delta = value - last[field]
                    else:
                        delta = value                                    # the collector restarted: counters began again from zero
                    d[key] += delta
                self.last_counters[device] = {"seq": h["seq"] or 0, "failures": h["failures"] or 0, "missed": h["missed"] or 0}
                d["max_fail_streak"] = max(d["max_fail_streak"], h["fail_streak"])
                if h["seq"] is not None and h["latency"] is not None:
                    d["latencies"][h["seq"]] = h["latency"]
        return finished

    def record(self):
        if self.minute is None or not self.samples:
            return None
        devices = {}
        for name, d in self.devices.items():
            lat = sorted(d["latencies"].values())
            devices[name] = {"reads": d["reads"], "failures": d["failures"], "missed": d["missed"],
                             "max_fail_streak": d["max_fail_streak"],
                             "lat_p50_ms": round(statistics.median(lat) * 1000, 1) if lat else None,
                             "lat_max_ms": round(lat[-1] * 1000, 1) if lat else None}
        return {"v": RECORD_VERSION, "minute": self.minute, "london": datetime.fromtimestamp(self.minute, LONDON).strftime("%Y-%m-%d %H:%M"),
                "cheap_window": in_cheap_window(self.minute), "ev_active": self.ev_active, "samples": self.samples,
                "monitor_ok": self.monitor_ok,
                "metrics": {k: {**v, "sum_abs": round(v["sum_abs"], 6), "max_abs": round(v["max_abs"], 6)} for k, v in self.metrics.items()},
                "devices": devices}


# ---- running ----------------------------------------------------------------------------------------

def fetch_monitor(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read(1_048_576))
    except (OSError, ValueError):
        return None


def read_live(path):
    try:
        document = json.loads(Path(path).read_text())
        health = (document.get("settings") or {}).get("collector_health") or {}
        return {o["metric"]: o for o in document["observations"]}, health.get("devices") or {}, health.get("direct_devices") or []
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}, []


def run(live, monitor_url, out, interval=0.5, stop=None, clock=time.time):
    stop = stop or threading.Event()
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    acc = Accumulator()

    def write(record):
        if record:
            with out.open("a") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    while not stop.is_set():
        started = clock()
        observations, devices, direct = read_live(live)
        if direct:
            write(acc.add(started, observations, devices, fetch_monitor(monitor_url), direct))
        stop.wait(max(0, interval - (clock() - started)))
    write(acc.record())          # keep the partial minute on a clean stop


# ---- reporting ----------------------------------------------------------------------------------------

def load(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def minute_qualifies(row, required=None):
    """Did both sources genuinely deliver this minute? Needs enough NEW reads from every required device, enough fresh paired
    samples for every metric of those devices, and a monitor that kept answering with recent data."""
    required = required or PROVISIONAL["required_devices"]
    try:
        if row.get("v") != RECORD_VERSION or row["monitor_ok"] < PROVISIONAL["min_pairs_per_minute"]:
            return False
        for device in required:
            if row["devices"][device]["reads"] < PROVISIONAL["min_reads_per_minute"]:
                return False
        for metric, (device, _fn) in PAIRS.items():
            if device in required:
                m = row["metrics"][metric]
                if m["n"] < PROVISIONAL["min_pairs_per_minute"] or m["new_readings"] < PROVISIONAL["min_reads_per_minute"]:
                    return False
        return True
    except (KeyError, TypeError):
        return False


def summarise(rows, required=None, now=None):
    """Judge only what the file can prove: unique, plausible, current-format minutes in which both sources really delivered."""
    required = tuple(required or PROVISIONAL["required_devices"])
    now = time.time() if now is None else now
    seen, clean, rejected, duplicates, legacy = set(), [], 0, 0, 0
    for row in rows:
        minute = row.get("minute") if isinstance(row, dict) else None
        if type(minute) is not int or minute % 60 or minute > now + PROVISIONAL["max_future_s"] or minute < 1_000_000_000:
            rejected += 1; continue
        if minute in seen:
            duplicates += 1; continue
        seen.add(minute)
        if row.get("v") != RECORD_VERSION:
            legacy += 1; continue
        clean.append(row)
    if not clean:
        return {"empty": True, "legacy": legacy, "duplicates": duplicates, "rejected": rejected, "rows_read": len(rows)}
    clean.sort(key=lambda r: r["minute"])
    qualifying = [r for r in clean if minute_qualifies(r, required)]
    span = (clean[-1]["minute"] - clean[0]["minute"]) // 60 + 1
    summary = {"empty": False, "required": required, "legacy": legacy, "duplicates": duplicates, "rejected": rejected, "rows_read": len(rows),
               "minutes": len(clean), "first": clean[0]["london"], "last": clean[-1]["london"], "elapsed_minutes": span,
               "elapsed_hours": round(span / 60, 2), "qualifying_minutes": len(qualifying), "hours": round(len(qualifying) / 60, 2),
               "coverage_pct": round(100 * len(qualifying) / span, 1), "cheap_minutes": sum(r["cheap_window"] for r in qualifying),
               "ev_minutes": sum(r["ev_active"] for r in qualifying), "metrics": {}, "devices": {},
               "devices_present": sorted({d for r in qualifying for d in r["devices"]})}
    total = sum(r["samples"] for r in clean)
    summary["monitor_ok_pct"] = round(100 * sum(r["monitor_ok"] for r in clean) / max(1, total), 2)
    good = {r["minute"] for r in qualifying}
    for r in clean:
        for metric, m in r["metrics"].items():
            t = summary["metrics"].setdefault(metric, {"n": 0, "exact": 0, "sum_abs": 0.0, "max_abs": 0.0, "direct_missing": 0, "direct_stale": 0,
                                                       "monitor_missing": 0, "monitor_stale": 0, "new_readings": 0, "changes_direct": 0,
                                                       "changes_monitor": 0, "lag_match": 0, "lag_n": 0, "pairs_seen": 0})
            # What was wrong is counted over every minute so it can never be hidden...
            for k in ("direct_missing", "direct_stale", "monitor_missing", "monitor_stale"):
                t[k] += m.get(k, 0)
            t["pairs_seen"] += m["n"]
            # ...but agreement is judged only on minutes in which both sources really delivered.
            if r["minute"] in good:
                for k in ("n", "exact", "sum_abs", "new_readings", "changes_direct", "changes_monitor", "lag_match"):
                    t[k] += m.get(k, 0)
                t["lag_n"] += m["n"]
                t["max_abs"] = max(t["max_abs"], m["max_abs"])
        for device, d in r["devices"].items():
            t = summary["devices"].setdefault(device, {"reads": 0, "failures": 0, "missed": 0, "max_fail_streak": 0, "p50": [], "max_ms": 0.0})
            t["reads"] += max(0, d["reads"]); t["failures"] += max(0, d["failures"]); t["missed"] += max(0, d["missed"])
            t["max_fail_streak"] = max(t["max_fail_streak"], d["max_fail_streak"])
            if d["lat_p50_ms"] is not None:
                t["p50"].append(d["lat_p50_ms"]); t["max_ms"] = max(t["max_ms"], d["lat_max_ms"])
    return summary


def verdict(summary, require_ev=False):
    """Plain-language findings against the plan's provisional criteria (owner to confirm the thresholds)."""
    findings, missing = [], []
    P = PROVISIONAL
    if summary["hours"] < P["required_hours"]:
        missing.append(f"{P['required_hours']} hours of qualifying minutes (have {summary['hours']} h)")
    if summary["elapsed_minutes"] >= 60 and summary["coverage_pct"] < P["required_coverage_pct"]:
        missing.append(f"coverage of at least {P['required_coverage_pct']}% of the elapsed time (have {summary['coverage_pct']}%)")
    if summary["cheap_minutes"] < P["cheap_window_minutes"]:
        missing.append(f"the cheap-rate window ({summary['cheap_minutes']} of {P['cheap_window_minutes']} qualifying minutes seen)")
    if summary["ev_minutes"] < 5 and require_ev:
        missing.append("a real car charge (fewer than 5 minutes above 10 W seen)")
    absent = [d for d in summary["required"] if d not in summary["devices_present"]]
    for device in absent:
        findings.append(f"FAIL {device}: no qualifying data from this required device")
    for metric, (device, _fn) in PAIRS.items():
        if device not in summary["required"]:
            continue
        m = summary["metrics"].get(metric)
        if not m or m["n"] < P["min_verdict_pairs"]:
            seen = m["pairs_seen"] if m else 0
            findings.append(f"FAIL {metric}: only {m['n'] if m else 0} paired fresh samples in qualifying minutes ({seen} pairs seen overall; "
                            f"{P['min_verdict_pairs']} needed for a verdict)"); continue
        mean = m["sum_abs"] / m["n"]
        if metric in P["stepped_metrics"]:
            pct = 100 * m["exact"] / m["n"]
            ok = m["max_abs"] <= P["step"] and pct >= P["stepped_min_exact_pct"]
            findings.append(f"{'PASS' if ok else 'CHECK'} {metric}: {pct:.1f}% exactly equal, otherwise at most one step apart"
                            if ok else f"CHECK {metric}: {pct:.1f}% exactly equal, largest difference {m['max_abs']:.4f} (more than one step, or too many mismatches)")
        else:
            lag = 100 * m["lag_match"] / m["lag_n"]
            ok = lag >= P["lag_match_pct"]
            findings.append(f"{'PASS' if ok else 'CHECK'} {metric}: {lag:.2f}% match within timing (needs {P['lag_match_pct']}%); "
                            f"raw: {100 * m['exact'] // m['n']}% exactly equal, mean difference {mean:.4f}, largest {m['max_abs']:.4f} (single samples during load changes)")
        offered = m["pairs_seen"] + m["direct_stale"] + m["direct_missing"]
        findings.append(f"     coverage {metric}: {100 * m['pairs_seen'] / max(1, offered):.1f}% of all samples were fresh on both sides "
                        f"(direct stale {m['direct_stale']}, direct missing {m['direct_missing']}, monitor stale {m['monitor_stale']}, monitor missing {m['monitor_missing']})")
        if metric in P["moving_metrics"] and summary["qualifying_minutes"]:
            rate = m["changes_direct"] / summary["qualifying_minutes"]
            if rate < P["min_changes_per_minute"]:
                findings.append(f"CHECK {metric}: the value changed only {rate:.2f} times a minute; a live measurement always moves, so the source may be frozen")
    for device in summary["required"]:
        d = summary["devices"].get(device)
        if not d:
            continue
        if d["reads"] < P["min_verdict_pairs"]:
            findings.append(f"FAIL {device}: only {d['reads']} new reads recorded ({P['min_verdict_pairs']} needed for a verdict); the device may not be delivering fresh readings")
            continue
        p50 = statistics.median(d["p50"]) if d["p50"] else float("nan")
        rate = 100 * d["failures"] / max(1, d["reads"] + d["failures"])
        ok = rate <= P["device_failure_pct"] and d["max_fail_streak"] <= P["device_max_failure_run"]
        findings.append(f"{'PASS' if ok else 'CHECK'} {device}: {d['reads']} reads, {d['failures']} failures ({rate:.2f}%), {d['missed']} missed deadlines, "
                        f"latency typically {p50:.0f} ms (worst {d['max_ms']:.0f} ms), longest failure run {d['max_fail_streak']}")
    return findings, missing


def report(path, require_ev=False, required=None, now=None):
    summary = summarise(load(path), required, now)
    if summary["empty"]:
        print(f"No usable evidence yet ({summary['rows_read']} lines read; {summary['legacy']} from before the freshness checks, "
              f"{summary['duplicates']} duplicates, {summary['rejected']} rejected as malformed or future-dated)."); return 1
    print(f"Comparison {summary['first']} to {summary['last']} London: {summary['elapsed_hours']} h elapsed, {summary['qualifying_minutes']} qualifying minutes "
          f"= {summary['hours']} h of evidence ({summary['coverage_pct']}% of the time); cheap-window minutes {summary['cheap_minutes']}; "
          f"car-charging minutes {summary['ev_minutes']}; monitor delivering recent data {summary['monitor_ok_pct']}%")
    notes = []
    if summary["legacy"]:
        notes.append(f"{summary['legacy']} minute(s) from before the freshness checks existed are not counted")
    if summary["duplicates"] or summary["rejected"]:
        notes.append(f"{summary['duplicates']} duplicate and {summary['rejected']} malformed or future-dated line(s) ignored")
    if notes:
        print("Note: " + "; ".join(notes) + ".")
    print(f"Required devices: {', '.join(summary['required'])} (the meter cannot be compared side by side: it serves only one client at a time).")
    findings, missing = verdict(summary, require_ev)
    print("\n".join(findings))
    print("Conditions still missing: " + ("; ".join(missing) if missing else "none"))
    if not require_ev:
        note = "seen so far: %d minute(s) above 10 W" % summary["ev_minutes"] if summary["ev_minutes"] else "none seen; the charger channel has been compared on standby only"
        print("Deferred by the owner (no electric car yet): a real car-charging comparison, " + note + ". Use --require-ev to count it again.")
    print("Thresholds are the plan's provisional ones; the owner confirms or amends them. This report is not a sign-off.")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog="solarbank compare")
    sub = parser.add_subparsers(dest="action", required=True)
    r = sub.add_parser("run")
    r.add_argument("--live", default="/run/solarbank-cost-optimiser/live.json")
    r.add_argument("--monitor", default="http://127.0.0.1:8080/api/readings")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--interval", type=float, default=0.5)
    p = sub.add_parser("report")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--require-ev", action="store_true", help="Count a real car charge as a required condition (off while there is no car)")
    args = parser.parse_args(argv)
    if args.action == "report":
        return report(args.file, args.require_ev)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set()); signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(args.live, args.monitor, args.out, args.interval, stop)
    return 0
