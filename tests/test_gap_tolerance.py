"""The Normal reference day may have a few missing minutes (service restarts during releases), but only a few, only visibly, never zero."""
from dataclasses import replace
from datetime import date

import pytest

from solarbank_optimiser.energy import day_bounds
from solarbank_optimiser.forecasts import MAX_FILLED_MINUTES, MAX_FILLED_RUN, demand_horizon, latest_normal_day
from solarbank_optimiser.models import DemandMode, Observation
from solarbank_optimiser.settings import DemandSelection, london_input

DAY = date(2026, 10, 27)


def day_rows(day=DAY, value=lambda minute: 0.5 + minute / 10000, drop=()):
    start, end = day_bounds(day)
    return [Observation("fixture", "household_power", t, value((t - start) // 60), "kW", coverage_seconds=60, demand_mode=DemandMode.NORMAL)
            for t in range(int(start), int(end), 60) if (t - start) // 60 not in set(drop)]


def pick(rows, **kw):
    end = day_bounds(DAY)[1]
    return latest_normal_day(rows, end + 3600, max_missing=MAX_FILLED_MINUTES, max_run=MAX_FILLED_RUN, **kw)


def test_the_strict_rule_is_still_the_default():
    with pytest.raises(ValueError):
        latest_normal_day(day_rows(drop=[100]), day_bounds(DAY)[1] + 3600)


def test_yesterdays_real_pattern_four_missing_minutes_in_three_places_is_accepted_and_counted():
    drop = [9 * 60 + 41, 9 * 60 + 42, 19 * 60 + 53, 23 * 60]                  # the four restart minutes found on the Pi
    day, rows = pick(day_rows(drop=drop))
    start = day_bounds(DAY)[0]
    assert day == DAY and len(rows) == 1436 and len(rows.filled) == 4
    assert set(rows.filled) == {start + m * 60 for m in drop}
    for t, kw in rows.filled.items():
        assert kw > 0                                                         # never zero-filled
        minute = (t - start) // 60
        assert abs(kw - (0.5 + minute / 10000)) < 0.001                       # a neighbouring-minute value: close to the truth here


@pytest.mark.parametrize("drop,ok", [
    (list(range(300, 305)), True),            # a run of 5 is the limit
    (list(range(300, 306)), False),           # a run of 6 is too long
    ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], True),     # 10 isolated minutes is the limit
    ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110], False),
    ([0], True), ([1439], True)])             # a gap at either end of the day is filled from the one neighbour
def test_the_limits(drop, ok):
    if ok:
        day, rows = pick(day_rows(drop=drop))
        assert len(rows.filled) == len(drop)
    else:
        with pytest.raises(ValueError):
            pick(day_rows(drop=drop))


def test_everything_else_still_disqualifies_a_day():
    rows = day_rows(drop=[100])
    for bad in (rows + [rows[0]],                                            # a duplicate
                [replace(o, demand_mode=DemandMode.AWAY) if i == 50 else o for i, o in enumerate(rows)],
                [replace(o, value=-1.0) if i == 50 else o for i, o in enumerate(rows)],
                rows + [replace(rows[0], retrieved_at=rows[0].retrieved_at + 30)]):  # a time that is not a minute of this day
        with pytest.raises(ValueError):
            pick(bad)


def test_a_minute_that_was_not_fully_covered_counts_as_a_missing_minute_not_as_data():
    rows = [replace(o, coverage_seconds=59) if i == 50 else o for i, o in enumerate(day_rows())]
    day, chosen = pick(rows)
    assert len(chosen.filled) == 1                                           # treated as missing and filled, never used as a full minute
    many = [replace(o, coverage_seconds=59) if i < 11 else o for i, o in enumerate(day_rows())]
    with pytest.raises(ValueError):
        pick(many)


def test_the_filled_minutes_are_reported_with_the_forecast_and_the_profile_has_no_holes():
    drop = [9 * 60 + 41, 19 * 60 + 53]
    now = london_input("2026-10-28T08:00")
    forecast = demand_horizon(day_rows(drop=drop), DemandSelection(mode="Normal consumption", start=now), 1, now, now, now + 48 * 3600)
    assert forecast["gap_filled_minutes"] == 2 and "2 minute(s) filled" in forecast["assumption"]
    assert forecast["reference_day"] == "2026-10-27"
    assert all(r["demand_kw"] > 0 for r in forecast["intervals"])
    complete = demand_horizon(day_rows(), DemandSelection(mode="Normal consumption", start=now), 1, now, now, now + 48 * 3600)
    assert complete["gap_filled_minutes"] == 0 and "filled" not in complete["assumption"]
    assert forecast["demand_kwh"] == pytest.approx(complete["demand_kwh"], rel=1e-3)
