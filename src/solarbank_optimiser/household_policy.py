"""Owner-requested household policy. Requests are not commissioned commands."""
from datetime import datetime, timedelta

from .energy import LONDON, day_bounds
from .models import DemandMode
from .settings import DemandSelection, FallbackTariff


def household_policy(selection, now, tariff=None):
    selection = DemandSelection(**selection) if isinstance(selection, dict) else selection
    tariff = FallbackTariff(**tariff) if isinstance(tariff, dict) else tariff
    if not selection or selection.mode != DemandMode.HIGH or selection.end is not None and selection.end <= now:
        return {"kind": "forecast_based", "overnight_target": None, "windows": [], "equipment_writes": False}
    first = tariff.start_minute if tariff else 1410
    last = tariff.end_minute if tariff else 330
    # Group actual UTC cheap minutes by their London overnight start date.
    # This retains both autumn clock occurrences and splits an unusual editable
    # window if the clock change makes it discontinuous.
    day = datetime.fromtimestamp(now, LONDON).date()
    begin = int(day_bounds(day - timedelta(days=1))[0])
    finish = int(day_bounds(day + timedelta(days=4))[0])
    groups = {}
    for t in range(begin, finish, 60):
        local = datetime.fromtimestamp(t, LONDON)
        minute = local.hour * 60 + local.minute
        cheap = first <= minute < last if first < last else minute >= first or minute < last
        if not cheap:
            continue
        anchor = local.date() - timedelta(days=1) if first > last and minute < last else local.date()
        intervals = groups.setdefault(anchor, [])
        if intervals and intervals[-1]["end"] == t:
            intervals[-1]["end"] = t + 60
        else:
            intervals.append({"start": t, "end": t + 60})
    nights = sorted(groups.items())
    windows = []
    for (anchor, intervals), (_, following) in zip(nights, nights[1:]):
        # This night's charge prepares for use until the following night starts.
        # A daytime visit therefore affects the preceding charging opportunity.
        if selection.start >= following[0]["start"] or selection.end is not None and selection.end <= intervals[0]["start"]:
            continue
        remaining = [{"start": max(now, r["start"]), "end": min(r["end"],selection.end) if selection.end is not None else r["end"]}
                     for r in intervals if r["end"] > now and r["start"] < now + 48 * 3600
                     and (selection.end is None or r["start"] < selection.end)]
        if remaining:
            windows.append({"night_date": str(anchor), "target": 1., "intervals": remaining})
    return {"kind": "full_overnight", "overnight_target": 1., "windows": windows,
            "start_minute": first, "end_minute": last,
            "selection_start": selection.start, "selection_end": selection.end,
            "status": "requested_policy_only", "equipment_writes": False,
            "reason": "High demand: request a full battery during the regular overnight window, including before a planned visit",
            "limits": "Requires valid prices and commissioned control; manual handover, EV inhibition, current limits and available charging time still apply"}
