"""One plain judgement of "is the real-time back end running safely?" for the website.

It reads only what the collector already publishes every second (device ages, failures, database trouble, discarded data) and
returns a level (safe / watch / unsafe), a headline and the reasons in plain English. Pure and offline-testable.
Thresholds are provisional, like the rest of the real-time work, and are listed in LIMITS.
"""

LIMITS = {
    "live_watch_s": 2.5, "live_unsafe_s": 5.0,          # how old the live readings file may be
    "device_watch_s": 2.5, "device_unsafe_s": 5.0,      # how old a directly read device's newest reading may be
    "streak_watch": 3, "streak_unsafe": 6,              # consecutive failed reads of one device
    "storage_error_recent_s": 600,                      # a recent database hiccup keeps the light amber for ten minutes
    "discard_unsafe_s": 1800, "discard_watch_s": 86400,  # data shed because the database stayed away
}
CONTROLLER_LIMITS = {"heartbeat_watch_s": 2.5, "heartbeat_stale_s": 5.0, "future_s": 1.0}
NOT_MONITORED = object()        # assess(): the website was not given a controller status path, so it says nothing about one
ORDER = {"safe": 0, "watch": 1, "unsafe": 2}
HEADLINE = {"safe": "Running safely", "watch": "Running, with something to watch", "unsafe": "Not running safely"}
NAMES = {"solarbank": "Solarbank", "shelly": "Shelly (battery line and car charger)", "meter": "Smart meter"}


def _ago(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds} s" if seconds < 90 else f"{seconds // 60} min" if seconds < 5400 else f"{seconds // 3600} h"


FUTURE_TOLERANCE_S = 1.0          # a reading may look this far ahead through clock granularity; more means the clock moved


FAILED_STAGES = ("rejected", "timeout", "failed", "unverified", "expired")


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) < 1e15 else None


def _text(value, limit=120):
    return value[:limit] if isinstance(value, str) else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _clean_controller(state):
    """Keep only the fields the page shows, each checked for type and size. The status file is data from another process: whatever it
    contains (wrong types, nesting, huge strings) must not be able to break the website."""
    if not isinstance(state, dict) or _num(state.get("at")) is None:
        return None
    ex, dec, plan, timing, ctl = (_dict(state.get(k)) for k in ("execution", "decision", "plan", "timing_ms", "controller"))
    commands = {}
    for channel, record in list(_dict(state.get("commands")).items())[:8]:
        record = _dict(record)
        if isinstance(channel, str):
            commands[channel[:24]] = {
                "action": _text(record.get("action"), 24), "priority": _text(record.get("priority"), 16), "stage": _text(record.get("stage"), 24),
                "simulated": record.get("simulated") is True, "issued_at": _num(record.get("issued_at")), "outcome": _text(record.get("outcome")),
                "params": {k[:16]: _num(v) for k, v in list(_dict(record.get("params")).items())[:6] if isinstance(k, str)}}
    listing = lambda value: [_text(x, 80) for x in value[:12] if isinstance(x, str)] if isinstance(value, list) else []
    return {
        "at": _num(state["at"]), "cycle": int(_num(state.get("cycle")) or 0), "period_s": _num(state.get("period_s")) or 1.0,
        "late_cycles": int(_num(state.get("late_cycles")) or 0),
        "execution": {"mode": _text(ex.get("mode"), 12), "requested": _text(ex.get("requested"), 12), "note": _text(ex.get("note"), 240),
                      "ownership": _text(ex.get("ownership"), 24), "sends_commands": ex.get("sends_commands") is True},
        "controller": {"run_id": _text(ctl.get("run_id"), 24), "started_at": _num(ctl.get("started_at"))},
        "timing_ms": {k: _num(timing.get(k)) for k in ("previous_total", "worst_total", "previous_output", "inputs", "plan_and_decide", "prepare", "execute")},
        "decision": {"discharge": _text(dec.get("discharge"), 12), "charge_limit_w": _num(dec.get("charge_limit_w")),
                     "charge_target_soc": _num(dec.get("charge_target_soc")), "economic": _text(dec.get("economic"), 20),
                     "preview_charge_w": _num(dec.get("preview_charge_w")), "restrictions": listing(dec.get("restrictions")),
                     "reasons": listing(dec.get("reasons"))},
        "plan": {"valid": plan.get("valid") is True, "note": _text(plan.get("note")), "action": _text(plan.get("action"), 16),
                 "target_soc": _num(plan.get("target_soc")), "valid_until": _num(plan.get("valid_until"))},
        "commands": commands}


def assess_controller(state, now):
    """Judge the controller from its own status file alone. `state` is the parsed file, or None if it is missing or unreadable.
    A controller that has stopped is stale here even while the collector carries on: its heartbeat is its own."""
    clean = _clean_controller(state)
    try:
        return _assess_controller(clean, now)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):      # belt and braces: never let this break the page
        reasons = [{"level": "watch", "text": "The controller's status could not be read."}]
        return {"present": False, "state": "missing", "level": "watch", "mode": None, "requested": None, "reasons": reasons}


def _assess_controller(state, now):
    reasons = []
    if state is None:
        reasons.append({"level": "watch", "text": "The controller is not running: there is no status from it. Collection and the dashboard carry on, but no decisions are being made or recorded."})
        return {"present": False, "state": "missing", "level": "watch", "mode": None, "requested": None, "reasons": reasons}
    execution = state["execution"]
    mode, requested = execution.get("mode"), execution.get("requested")
    age = now - state["at"]
    stale_level = "unsafe" if mode == "live" else "watch"
    if age < -CONTROLLER_LIMITS["future_s"]:
        status = "clock"
        reasons.append({"level": "unsafe", "text": f"The controller's status is dated {_ago(-age)} in the future: the clock was set back, so its freshness cannot be judged."})
    elif age > CONTROLLER_LIMITS["heartbeat_stale_s"]:
        status = "stale"
        reasons.append({"level": stale_level, "text": f"The controller has not reported for {_ago(age)}: it has stopped or stalled (last status is shown below, not current)."})
    elif age > CONTROLLER_LIMITS["heartbeat_watch_s"]:
        status = "slow"
        reasons.append({"level": "watch", "text": f"The controller's last report is {_ago(age)} old (normally under a second)."})
    else:
        status = "running"
    if requested == "live" and mode != "live":
        reasons.append({"level": "watch", "text": execution.get("note") or "Live was requested but the controller is in shadow."})
    timing = state["timing_ms"]
    period = state["period_s"]
    if isinstance(timing.get("previous_total"), (int, float)) and timing["previous_total"] > 3000 * period and status == "running":
        reasons.append({"level": "watch", "text": f"The controller's last cycle took {timing['previous_total'] / 1000:.1f} s against a {period:.0f} s period."})
    commands = []
    for channel, record in sorted(state["commands"].items()):
        stage = record.get("stage")
        commands.append({"channel": channel, "action": record.get("action"), "params": record.get("params"), "priority": record.get("priority"),
                         "stage": stage, "simulated": record.get("simulated"), "issued_at": record.get("issued_at"),
                         "outcome": record.get("outcome")})
        if stage in FAILED_STAGES and mode == "live":
            reasons.append({"level": "unsafe" if record.get("priority") == "protective" else "watch",
                            "text": f"{channel} command {record.get('action')} was not confirmed ({stage}): an unconfirmed stop is an unverified control state, not a confirmed one."})
    decision = state["decision"]
    if execution.get("ownership") in ("handover_pending", "anker"):
        reasons.append({"level": "watch", "text": ("Control has been handed to the Anker app and the release is confirmed." if execution["ownership"] == "anker"
                        else "Handover to the Anker app was requested but is not confirmed yet: do not treat the Anker app as in control.")})
    level = max((r["level"] for r in reasons), key=ORDER.get, default="safe")
    return {"present": True, "state": status, "level": level, "mode": mode, "requested": requested, "mode_note": execution.get("note"),
            "ownership": execution.get("ownership"), "sends_commands": execution.get("sends_commands"),
            "heartbeat_age_s": round(max(0.0, age), 1), "cycle": state["cycle"],
            "run_id": state["controller"]["run_id"], "started_at": state["controller"]["started_at"],
            "late_cycles": state["late_cycles"], "timing_ms": timing,
            "decision": {k: decision.get(k) for k in ("discharge", "charge_limit_w", "charge_target_soc", "economic", "preview_charge_w")},
            "restrictions": decision["restrictions"], "decision_reasons": decision["reasons"],
            "plan": state["plan"], "commands": commands, "reasons": reasons}


def assess(health, now, live_written_at, controller=NOT_MONITORED):
    """health: the collector's published health record (or None); live_written_at: when the live file was last written.
    controller: the controller's parsed status file or None if missing (omit it where the controller is not monitored)."""
    reasons, notes, devices = [], [], []

    def add(level, text):
        reasons.append({"level": level, "text": text})

    result_controller = None
    if controller is not NOT_MONITORED:                       # judged first so it is reported even when the collector is silent
        result_controller = assess_controller(controller, now)

    if not health:
        add("unsafe", "The collector is not publishing anything: live readings are unavailable.")
        if result_controller:
            reasons.extend(result_controller["reasons"])
        return _result(reasons, notes, devices, now, result_controller)

    if live_written_at is None:
        add("unsafe", "The live-readings file is missing: the collector is not running or cannot write it.")
    else:
        age = now - live_written_at
        if age < -FUTURE_TOLERANCE_S:
            add("unsafe", f"The live-readings file is dated {_ago(-age)} in the future: the clock has been set back, so freshness cannot be judged.")
        elif age > LIMITS["live_unsafe_s"]:
            add("unsafe", f"Live readings stopped {_ago(age)} ago: the collector has stalled or stopped.")
        elif age > LIMITS["live_watch_s"]:
            add("watch", f"Live readings are {_ago(age)} old (normally under a second).")

    status = health.get("status")
    if status == "partial":
        add("watch", "Some readings are missing or unavailable right now.")
    elif status != "ok":
        add("unsafe", "No valid fresh readings are arriving from the equipment.")

    if health.get("equipment_writes") is not False:
        add("unsafe", "Unexpected: the collector is not reporting that equipment writes are disabled.")

    direct = list(health.get("direct_devices") or [])
    health_devices = health.get("devices") or {}
    for name in ("solarbank", "meter", "shelly"):
        if name not in direct:
            devices.append({"name": NAMES[name], "source": "original monitor", "state": "via the monitor", "age_s": None, "latency_ms": None, "failures": None})
            continue
        d = health_devices.get(name)
        label = NAMES[name]
        if not d or d.get("age") is None:
            add("watch", f"{label}: no reading yet (starting up).")
            devices.append({"name": label, "source": "direct", "state": "starting", "age_s": None, "latency_ms": None, "failures": None})
            continue
        level, state = "safe", "ok"
        if d["age"] < -FUTURE_TOLERANCE_S:
            level, state = "unsafe", "clock"
            add("unsafe", f"{label}: reading is dated {_ago(-d['age'])} in the future; the clock has been set back.")
        elif d["age"] > LIMITS["device_unsafe_s"]:
            level, state = "unsafe", "silent"
            add("unsafe", f"{label}: no new reading for {_ago(d['age'])}.")
        elif d["age"] > LIMITS["device_watch_s"]:
            level, state = "watch", "slow"
            add("watch", f"{label}: newest reading is {_ago(d['age'])} old.")
        streak = d.get("fail_streak", 0)
        if streak >= LIMITS["streak_unsafe"]:
            level, state = "unsafe", "failing"
            add("unsafe", f"{label}: the last {streak} reads failed ({d.get('last_error') or 'error'}).")
        elif streak >= LIMITS["streak_watch"] and level == "safe":
            level, state = "watch", "failing"
            add("watch", f"{label}: the last {streak} reads failed ({d.get('last_error') or 'error'}).")
        if name == "solarbank" and d.get("slow_error_streak", 0) >= 3:
            add("watch", "Solarbank: battery totals and limits have stopped refreshing (they are withheld rather than shown stale).")
        devices.append({"name": label, "source": "direct", "state": state, "age_s": round(d["age"], 1),
                        "latency_ms": None if d.get("latency") is None else round(d["latency"] * 1000), "failures": d.get("failures")})

    last_error = health.get("last_storage_error_at")
    if last_error is not None and now - last_error <= LIMITS["storage_error_recent_s"]:
        add("watch", f"The database was busy or unavailable {_ago(now - last_error)} ago; readings were held in memory and retried.")
    recent = [r for r in (health.get("discarded_recent") or []) if r.get("at") is not None]
    if recent:
        newest = min(now - r["at"] for r in recent)              # how long ago the most recent discard happened
        total = sum((health.get("discarded_total") or {}).values())
        if newest <= LIMITS["discard_unsafe_s"]:
            add("unsafe", f"Data is being discarded because the database has been away too long ({total} items so far; see the collector log).")
        elif newest <= LIMITS["discard_watch_s"]:
            add("watch", f"Some data was discarded {_ago(newest)} ago while the database was away ({total} items); the gap is on record.")
    elif health.get("discarded_total"):
        add("watch", "Some data was discarded earlier while the database was away; the gap is on record.")

    if direct and len(direct) < 3:
        notes.append("Read directly: " + ", ".join(NAMES[n].split(" (")[0] for n in direct) + ". The rest comes from the original energy monitor.")
    elif not direct:
        notes.append("All readings come from the original energy monitor; the new direct readers are not in use.")
    live = isinstance(controller, dict) and _dict(controller.get("execution")).get("mode") == "live"
    notes.append("Live: the controller sends commands to the battery (never to the charger)." if live
                 else "Shadow mode: nothing is sent to the battery or the charger. Equipment control is not commissioned.")
    if result_controller:
        reasons.extend(result_controller["reasons"])
    return _result(reasons, notes, devices, now, result_controller)


def _result(reasons, notes, devices, now, controller=None):
    level = max((r["level"] for r in reasons), key=ORDER.get, default="safe")
    summary = "Everything the real-time system checks is healthy." if level == "safe" else "; ".join(r["text"] for r in reasons if r["level"] == level)
    return {"level": level, "headline": HEADLINE[level], "summary": summary, "reasons": reasons, "devices": devices, "notes": notes, "checked_at": now, "controller": controller}
