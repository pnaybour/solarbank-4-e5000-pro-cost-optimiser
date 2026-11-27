"""Learned household demand for the planner (owner's request of 3 November; GitHub issue 16 line of work, POST_SHADOW_PLAN section 8 item 7).

What the planner assumes the house will use is, today, a typed figure (Away: the base load in watts) or a copy of the last good day (Normal). This module checks those
figures against what the house actually used and, only when a learned figure has been better over several days, moves the planner's figure towards it, slowly.

* Every finished day is tagged with its household mode from the settings history, so Away and Normal days are never mixed. The car is not household demand: each minute is
  the household reading less the car charger (GitHub issue 74; it was the car's minutes left out, which dropped every day with a long charge below MIN_MINUTES), and a
  minute with no car reading is left out. A day needs at least MIN_MINUTES usable minutes.
* Away: candidates are the mean and the 80th percentile of the last WINDOW Away days. Normal: the candidate is the mean of the last few good days against the current rule,
  the last good day.
* Each morning every candidate is scored on the days it would have predicted, using only the days before each one, with the planner's asymmetric cost: a shortfall (the house
  used more than planned) is bought at about 28p a kWh more than a surplus costs.
* A candidate replaces the typed or last-day figure only after it beats it over PROMOTE_AFTER scored days. The typed Away figure stays the starting value and the CAP; the
  planner's figure moves at most STEP of the typed value a day; High demand stays a typed instruction and is never learned.
* Normal (owner, 6 November): the forecast starts from the owner's reference of SEED_KWH a day (sd SEED_SD_KWH over SEED_DAYS days, October 2026) and is learned from there. The
  daily LEVEL is the mean of the last LEVEL_DAYS good Normal days, the seed standing in for any of the first SEED_DAYS that have not happened yet; the SHAPE is the mean of the
  last PROFILE_DAYS good days, scaled so the day totals the level. Heating and hot water are gas, so there is no temperature term. A lighting term (dusk to 22:30) is measured
  as a diagnostic only (`evening`), to be promoted if a real trend shows over the winter.
* One switch (`demand_learning` setting, `enabled`) returns everything to the typed figures at once. The dashboard shows both figures.
Nothing here sends a command to equipment.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from .daylight import sunset_utc
from .energy import LONDON, day_bounds
from .minute_source import MIN_COVERED, DemandTimeline
from .models import DemandMode

SETTING = "demand_learning"
SETTLE_S = 600                  # a day is scored this long after it ends, so its last minutes are in
LOOKBACK_DAYS = 14
KEEP_DAYS = 30
MIN_MINUTES = 1000              # usable minutes a day needs (of 1,440)
WINDOW = 7                      # Away days in the rolling candidates
PROMOTE_AFTER = 3               # scored days a candidate must win over before it is used
NORMAL_AVERAGE = 3              # good days averaged by the Normal candidate when it is scored against the last good day (a diagnostic since 6 November)
PROFILE_DAYS = 7                # good Normal days whose minute profile is averaged for the planner (the planner reads 8 days of rows)
SEED_KWH, SEED_SD_KWH, SEED_DAYS = 12.24, 2.66, 12     # the owner's October reference: 12 days, 6-17 October 2026
LEVEL_DAYS = 14                 # recent good Normal days the daily level is the mean of
STEP = 0.10                     # the planner's Away figure moves at most this fraction of the typed figure per day
FLOOR_KW = 0.10
SHORTFALL_PENCE, SURPLUS_PENCE = 28.0, 1.0


def pence(actual_kwh, predicted_kwh):
    """The planner's cost of being wrong by this much over a day, in pence (the unit only matters in comparison)."""
    gap = actual_kwh - predicted_kwh
    return SHORTFALL_PENCE * gap if gap > 0 else -SURPLUS_PENCE * gap


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


# ---- one day ------------------------------------------------------------------------------------------------------------------------------------
def day_mode(timeline, day):
    """The household mode that applied for the whole London day, or None when it changed during the day or cannot be told."""
    a, b = day_bounds(day)
    modes = set()
    for t in (a, a + 6 * 3600, a + 12 * 3600, a + 18 * 3600, b - 60):
        mode, _revision, broken = timeline.tag((int(t) // 60) * 60)
        if broken or mode is None:
            return None
        modes.add(str(mode))
    return modes.pop() if len(modes) == 1 else None


def day_mean_kw(source, day):
    """The mean household power over the day in kW, the car taken out minute by minute (GitHub issue 74); None when the day is too thin."""
    a, b = day_bounds(day)
    house = source.house_without_car(a, b)
    values = [x.mean for x in house.values() if x.mean is not None and x.covered >= MIN_COVERED]
    return sum(values) / len(values) if len(values) >= MIN_MINUTES else None


def evening_figures(source, day):
    """What the lighting question needs from one day: the household energy between sunset and 22:30 (London), its share of the day, and the sunset time. None when the sun
    does not set, or the day's data is too thin. The car is taken out minute by minute, as for the day's mean."""
    sunset = sunset_utc(day)
    if sunset is None:
        return None
    a, b = day_bounds(day)
    house = source.house_without_car(a, b)
    end = datetime.combine(day, time(22, 30), LONDON).timestamp()
    total = evening = counted = evening_counted = 0
    for minute, x in house.items():
        if x.mean is None or x.covered < MIN_COVERED:
            continue
        counted += 1
        total += x.mean / 60
        if sunset <= minute < end:
            evening_counted += 1
            evening += x.mean / 60
    if counted < MIN_MINUTES or total <= 0:
        return None
    expected = max(0, (end - sunset) / 60)
    if expected and evening_counted < 0.9 * expected:
        return None                                                   # the evening itself has gaps: no figure rather than a low one
    local_sunset = datetime.fromtimestamp(sunset, LONDON)
    return {"kwh": round(evening, 3), "share": round(evening / total, 4), "sunset": local_sunset.strftime("%H:%M"), "sunset_minute": local_sunset.hour * 60 + local_sunset.minute}


def normal_level(state):
    """(kWh a day the planner should expect on a Normal day, good days learned so far). The mean of the last LEVEL_DAYS good days, the owner's seed standing in for each of the
    first SEED_DAYS that have not happened yet, so the seed fades out over those days and the level then follows the house."""
    days = state["normal"]["days"]
    recent = [days[d] * 24 for d in sorted(days)[-LEVEL_DAYS:]]
    values = recent + [SEED_KWH] * max(0, SEED_DAYS - len(recent))
    return sum(values) / len(values), len(recent)


def _correlation(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return None if sx == 0 or sy == 0 else round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 2)


# ---- the state ------------------------------------------------------------------------------------------------------------------------------------
def load(store):
    state = store.get_setting(SETTING, {})[1] or {}
    state.setdefault("enabled", True)
    for mode in ("away", "normal"):
        state.setdefault(mode, {})
        state[mode].setdefault("days", {})
    state.setdefault("seen", [])
    return state


def save(store, state, now):
    store.set_setting(SETTING, state, now, store.get_setting(SETTING)[0])


def score(values, typed_rule):
    """Costs of each rule over the days that had at least one earlier day: `typed_rule(prior)` is the current rule, the others are the candidates.
    Returns ({rule: pence}, scored_days)."""
    totals = {"current": 0.0, "mean": 0.0, "p80": 0.0}
    scored = 0
    for i in range(1, len(values)):
        prior = values[max(0, i - WINDOW):i]
        actual = values[i]
        totals["current"] += pence(actual, typed_rule(prior))
        totals["mean"] += pence(actual, sum(prior) / len(prior))
        totals["p80"] += pence(actual, _percentile(prior, 0.8))
        scored += 1
    return totals, scored


def _evaluate_away(away, typed_kw):
    dates = sorted(away["days"])
    values = [away["days"][d] * 24 for d in dates]                       # kWh per day
    totals, scored = score(values, lambda prior: typed_kw * 24)
    best = min(("mean", "p80"), key=lambda k: totals[k]) if scored else None
    away["scores"] = {"days_scored": scored, **{k: round(v, 1) for k, v in totals.items()}, "best": best}
    away["promoted"] = bool(scored >= PROMOTE_AFTER and best and totals[best] < totals["current"])
    recent = [away["days"][d] for d in dates[-WINDOW:]]
    away["target_kw"] = None
    if recent and best:
        away["target_kw"] = (sum(recent) / len(recent)) if best == "mean" else _percentile(recent, 0.8)
    return away


def _evaluate_normal(normal):
    dates = sorted(normal["days"])
    values = [normal["days"][d] * 24 for d in dates]
    totals = {"current": 0.0, "mean": 0.0}
    scored = 0
    for i in range(1, len(values)):
        prior = values[max(0, i - NORMAL_AVERAGE):i]
        totals["current"] += pence(values[i], values[i - 1])               # the current rule: yesterday's good day
        totals["mean"] += pence(values[i], sum(prior) / len(prior))
        scored += 1
    normal["scores"] = {"days_scored": scored, **{k: round(v, 1) for k, v in totals.items()}}
    normal["promoted"] = bool(scored >= PROMOTE_AFTER and totals["mean"] < totals["current"])
    normal["average_days"] = PROFILE_DAYS                                     # the profile is the mean of the last good days from the start (owner, 6 November)
    return normal


def learn(store, now, source=None):
    """Score the finished days that have not been looked at yet and update the learned figures. Returns the days processed. Safe to call often."""
    state = load(store)
    source = source or store.minute_source()
    today = datetime.fromtimestamp(now, LONDON).date()
    selection_raw = store.get_setting("demand")[1] or {}
    typed_kw = (selection_raw.get("away_watts") or 0) / 1000 if selection_raw.get("mode") == DemandMode.AWAY or selection_raw.get("away_watts") else None
    done, added_away = [], 0
    for back in range(LOOKBACK_DAYS, 0, -1):
        day = today - timedelta(days=back)
        key = day.isoformat()
        a, b = day_bounds(day)
        if key in state["seen"] or now < b + SETTLE_S:
            continue
        timeline = DemandTimeline(store, a, b)
        mode = day_mode(timeline, day)
        mean = day_mean_kw(source, day) if mode in (DemandMode.AWAY, DemandMode.NORMAL) else None
        if mean is None and mode in (DemandMode.AWAY, DemandMode.NORMAL) and now - b < 3 * 86400:
            continue                                         # the day's data may still be arriving: look again later
        state["seen"].append(key)
        done.append(day)
        if mean is None:
            continue
        state["away" if mode == DemandMode.AWAY else "normal"]["days"][key] = round(mean, 4)
        if mode == DemandMode.NORMAL:
            figures = evening_figures(source, day)
            if figures:
                state["normal"].setdefault("evening", {})[key] = figures
        added_away += mode == DemandMode.AWAY
    state["seen"] = sorted(state["seen"])[-KEEP_DAYS:]
    for mode in ("away", "normal"):
        bucket = state[mode]["days"]
        for old in sorted(bucket)[:-KEEP_DAYS]:
            del bucket[old]
    evening = state["normal"].setdefault("evening", {})
    for old in sorted(evening)[:-KEEP_DAYS]:
        del evening[old]
    away = state["away"]
    if typed_kw:
        _evaluate_away(away, typed_kw)
        previous = away.get("effective_kw") or typed_kw
        if away["promoted"] and away.get("target_kw") is not None:
            goal = max(FLOOR_KW, min(typed_kw, away["target_kw"]))        # the typed figure is the cap
            for _ in range(added_away):                                    # one slow step per newly scored Away day
                previous += max(-STEP * typed_kw, min(STEP * typed_kw, goal - previous))
            away["effective_kw"] = round(min(typed_kw, max(FLOOR_KW, previous)), 4)
        else:
            away["effective_kw"] = round(typed_kw, 4)                       # nothing learned yet, or the typed figure is still the better one
        away["typed_kw"] = round(typed_kw, 4)
    _evaluate_normal(state["normal"])
    if done or state.get("computed_at") is None:
        state["computed_at"] = now
        save(store, state, now)
    return done


# ---- what the planner uses and the dashboard shows ------------------------------------------------------------------------------------------------
def planner_inputs(store, selection):
    """(selection with the learned Away figure, number of Normal days to average, note). With the switch off or nothing learned: the selection unchanged, 1, None."""
    try:
        state = load(store)
    except Exception:
        return selection, 1, None
    if not state.get("enabled", True) or not selection:
        return selection, 1, None
    chosen, days, notes = selection, 1, []
    effective = state["away"].get("effective_kw")
    typed = state["away"].get("typed_kw")
    raw = selection if isinstance(selection, dict) else selection.model_dump()
    if raw.get("mode") == DemandMode.AWAY and effective and typed and effective < typed - 1e-9 and raw.get("away_watts"):
        chosen = {**raw, "away_watts": round(effective * 1000, 1)}
        notes.append(f"Away base load learned: {effective * 1000:.0f} W instead of the typed {typed * 1000:.0f} W")
    if state["normal"].get("average_days", 1) > 1:
        days = state["normal"]["average_days"]
        notes.append(f"Normal demand: profile from the mean of up to {days} good days, scaled to the learned level")
    return chosen, days, ("; ".join(notes) or None)


def _evening_summary(evening):
    """The lighting diagnostic: does the evening's share of the day move with the time of sunset? A correlation near -1 would say it grows as it gets dark earlier. Nothing uses it yet."""
    days = sorted(evening)
    xs = [evening[d]["sunset_minute"] for d in days]
    return {"days": len(days), "mean_share": round(sum(evening[d]["share"] for d in days) / len(days), 3) if days else None,
            "correlation_with_sunset": _correlation(xs, [evening[d]["share"] for d in days]), "used_by_planner": False}


def planner_level(store):
    """The daily Normal level (kWh) the planner scales its profile to, or None with the learning switch off (then the profile is used as it is)."""
    try:
        state = load(store)
    except Exception:
        return None
    return round(normal_level(state)[0], 3) if state.get("enabled", True) else None


def describe(store):
    """What the dashboard shows: both figures side by side and whether learning is on."""
    state = load(store)
    away, normal = state["away"], state["normal"]
    return {"enabled": state.get("enabled", True), "computed_at": state.get("computed_at"),
            "away": {"typed_w": round(away["typed_kw"] * 1000) if away.get("typed_kw") else None,
                     "learned_target_w": round(away["target_kw"] * 1000) if away.get("target_kw") else None,
                     "in_use_w": round(away["effective_kw"] * 1000) if away.get("effective_kw") else None,
                     "days": len(away["days"]), "promoted": bool(away.get("promoted")), "scores": away.get("scores")},
            "normal": {"days": len(normal["days"]), "average_days": normal.get("average_days", 1), "promoted": bool(normal.get("promoted")), "scores": normal.get("scores"),
                       "level_kwh": round(normal_level(state)[0], 2), "seed_kwh": SEED_KWH, "seed_sd_kwh": SEED_SD_KWH, "seed_days_left": max(0, SEED_DAYS - len(normal["days"])),
                       "evening": _evening_summary(normal.get("evening", {}))}}


def set_enabled(store, enabled, now):
    state = load(store)
    state["enabled"] = bool(enabled)
    save(store, state, now)
    return state["enabled"]
