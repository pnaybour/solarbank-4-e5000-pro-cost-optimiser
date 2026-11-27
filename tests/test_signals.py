"""WP24 (first slice): the decision table's inputs and the pure decision function, against owner specification v1.3 sections 5.2, 6.2 and 7."""
from datetime import datetime
from itertools import product

import pytest
from hypothesis import given, strategies as st

from solarbank_optimiser import signals as sg
from solarbank_optimiser.signals import BelowTarget, EvSignal, SmartSlot, decide, offpeak


# ---- the decision table: all 16 combinations, written out from the specification table, not from the function --------------------------
TABLE = {  # (valid, cheap, below_target, ev) -> action
    **{(False, c, b, e): "FAULT" for c, b, e in product((False, True), repeat=3)},
    **{(True, True, True, e): "CHARGE" for e in (False, True)},
    **{(True, True, False, e): "ZERO_OUTPUT" for e in (False, True)},
    **{(True, False, b, True): "ZERO_OUTPUT" for b in (False, True)},
    **{(True, False, b, False): "DISCHARGE" for b in (False, True)},
}


def test_all_sixteen_input_combinations_match_the_specification_table():
    assert len(TABLE) == 16
    for combo, action in TABLE.items():
        assert decide(*combo) == action, combo


@given(st.booleans(), st.booleans(), st.booleans())
def test_in_a_cheap_period_discharge_is_never_allowed_whatever_else_is_true(below_target, ev, cheap_only):
    assert decide(True, True, below_target, ev) != "DISCHARGE"
    assert decide(False, True, below_target, ev) == "FAULT"


# ---- OFFPEAK ----------------------------------------------------------------------------------------------------------------------------
def utc(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=sg.ZoneInfo("UTC")).timestamp()


def test_offpeak_is_2330_to_0530_london_time_with_inclusive_start_and_exclusive_end():
    assert not offpeak(utc(2026, 1, 15, 23, 29, 59)) and offpeak(utc(2026, 1, 15, 23, 30)) and offpeak(utc(2026, 1, 16, 5, 29, 59)) and not offpeak(utc(2026, 1, 16, 5, 30))


def test_offpeak_follows_the_london_clock_through_both_clock_changes():
    assert offpeak(utc(2026, 7, 15, 22, 30)) and not offpeak(utc(2026, 7, 15, 22, 29))        # summer: 23:30 BST is 22:30 UTC
    assert offpeak(utc(2026, 7, 16, 4, 29)) and not offpeak(utc(2026, 7, 16, 4, 30))          # ... and 05:30 BST is 04:30 UTC
    assert not offpeak(utc(2026, 10, 25, 22, 30)) and offpeak(utc(2026, 10, 25, 23, 30))     # the night clocks go back (01:00 UTC): 23:30 is now GMT
    assert offpeak(utc(2026, 10, 26, 5, 29)) and not offpeak(utc(2026, 10, 26, 5, 30))
    assert not offpeak(utc(2026, 3, 29, 5, 30)) and offpeak(utc(2026, 3, 29, 4, 29))          # clocks go forward: 05:30 BST on that day is 04:30 UTC


# ---- EV ---------------------------------------------------------------------------------------------------------------------------------
def feed(signal, readings, t0=0.0):
    out = []
    for i, p in enumerate(readings):
        out.append(signal.update(p, t0 + i))
    return out


def test_ev_turns_on_after_3_s_above_1000_w_and_not_before():
    ev = EvSignal()
    seen = feed(ev, [1001] * 5)
    assert seen == [False, False, False, True, True]               # rises at t=0, confirmed at t=3


def test_ev_1000_w_exactly_is_not_charging_and_a_short_peak_changes_nothing():
    assert not any(feed(EvSignal(), [1000] * 20))
    ev = EvSignal(); feed(ev, [5000, 5000, 200] + [5] * 10)
    assert ev.value is False and ev.state == "off"


def test_ev_standby_and_the_band_between_the_thresholds_change_nothing():
    assert not any(feed(EvSignal(), [3] * 500))
    ev = EvSignal(); feed(ev, [7000] * 10)
    assert ev.value is True
    feed(ev, [350] * 500, t0=10)                                     # between 300 and 1,000 W: unchanged, however long
    assert ev.value is True and ev.state == "on"


def test_ev_turns_off_after_120_s_below_300_w_and_300_exactly_does_not_count():
    ev = EvSignal(); feed(ev, [7000] * 10)
    seen = feed(ev, [299] * 125, t0=10)
    assert seen[:120] == [True] * 120 and seen[120] is False         # confirming_off from t=10; off at t=130
    ev = EvSignal(); feed(ev, [7000] * 10)
    assert all(feed(ev, [300] * 500, t0=10))


def test_ev_a_dip_below_300_w_that_recovers_inside_120_s_keeps_it_on():
    ev = EvSignal(); feed(ev, [7000] * 10)
    feed(ev, [100] * 100, t0=10); feed(ev, [5000] * 5, t0=110)
    feed(ev, [100] * 100, t0=115)                                    # the 120 s start again
    assert ev.value is True


def test_ev_a_gap_up_to_10_s_changes_nothing_and_a_longer_one_holds_only_a_charging_car_for_10_minutes():
    ev = EvSignal(); feed(ev, [7000] * 10)
    assert ev.update(None, 15.0) is True and ev.known                          # 6 s since the last good reading
    assert ev.update(None, 19.0) is True and ev.known                          # 10 s
    assert ev.update(None, 20.0) is True and ev.known                          # longer, but the car was charging
    assert ev.update(None, 9 + 600.0) is True and ev.known                     # exactly 10 minutes
    assert ev.update(None, 9 + 601.0) is False and not ev.known                # past it the reading is unknown, which is not a charging car


def test_ev_a_gap_longer_than_10_s_while_not_charging_is_unknown_not_held():
    ev = EvSignal(); feed(ev, [3] * 5)
    assert ev.update(None, 8.0) is False and ev.known                          # 4 s: nothing changes
    assert ev.update(None, 20.0) is False and not ev.known


def test_ev_after_an_unknown_period_the_car_must_be_confirmed_again():
    ev = EvSignal(); feed(ev, [7000] * 10); ev.update(None, 9 + 700.0)
    assert not ev.known
    assert ev.update(7000, 800.0) is False and ev.known and ev.state == "confirming_on"
    assert ev.update(7000, 803.0) is True


@pytest.mark.parametrize("bad", [None, -5.0, "x", float("nan"), True])
def test_ev_ignores_an_invalid_reading(bad):
    ev = EvSignal(); feed(ev, [7000] * 10)
    assert ev.update(bad, 10.0) is True and ev.state == "on"


def test_ev_periodic_dwell_checks_are_not_counted_as_refused_events():
    from solarbank_optimiser.machines import counters
    before = counters("ev").as_dict(); feed(EvSignal(), [7000] * 3)
    assert counters("ev").as_dict() == before


# ---- BELOW_TARGET -----------------------------------------------------------------------------------------------------------------------
def test_below_target_hysteresis_is_3_percent():
    b = BelowTarget()
    assert b.update(78.0, 80.0, 0) is False                    # 2 below: not yet
    assert b.update(77.0, 80.0, 1) is True                     # target - 3: on
    assert b.update(79.9, 80.0, 2) is True and b.update(78.0, 80.0, 3) is True       # unchanged in between
    assert b.update(80.0, 80.0, 4) is False                    # at target: off
    assert b.update(78.0, 80.0, 5) is False and b.update(77.0, 80.0, 6) is True


def test_below_target_is_unchanged_when_a_value_is_missing():
    b = BelowTarget(); b.update(50, 80, 0)
    assert b.update(None, 80, 1) is True and b.update(50, None, 2) is True


def test_the_target_is_clamped_to_the_limit_registers():
    assert sg.clamp_target(95, 10, 90) == 90 and sg.clamp_target(5, 10, 90) == 10 and sg.clamp_target(60, 10, 90) == 60


# ---- SMARTSLOT --------------------------------------------------------------------------------------------------------------------------
T0 = utc(2026, 11, 3, 23, 30)                                   # a half-hour boundary


def test_a_smart_dispatch_with_the_car_charging_switches_the_slot_on():
    s = SmartSlot()
    assert s.update(T0 + 60, True, [(T0, T0 + 3600, "SMART")]) is True


def test_a_boost_or_bump_charge_never_counts():
    s = SmartSlot()
    assert s.update(T0 + 60, True, [(T0, T0 + 3600, "BOOST")]) is False


def test_the_car_not_charging_means_no_slot_even_with_a_listed_dispatch():
    s = SmartSlot()
    assert s.update(T0 + 60, False, [(T0, T0 + 3600, "SMART")]) is False


def test_a_dispatch_that_spans_the_2330_boundary_keeps_the_slot_on_across_it():
    s = SmartSlot(); d = [(T0 - 1800, T0 + 1800, "SMART")]
    seen = [s.update(T0 - 120 + i * 10, True, d if i == 0 else None) for i in range(24)]      # 2 minutes before to 2 minutes after
    assert all(seen)


def test_once_on_it_holds_to_the_end_of_the_half_hour_even_if_the_dispatch_vanishes_or_the_fetch_fails():
    s = SmartSlot(); s.update(T0 + 60, True, [(T0, T0 + 3600, "SMART")])
    assert s.update(T0 + 120, True, [], fetch_ok=True) is True                  # an empty list while Octopus re-plans
    assert s.update(T0 + 600, True, None, fetch_ok=False) is True               # a failed fetch
    assert s.update(T0 + 1799, True, None, fetch_ok=False) is True
    assert s.update(T0 + 1800, True, None, fetch_ok=False) is False             # the half-hour is over and nothing supports a new slot


def test_the_slot_continues_into_the_next_half_hour_without_a_gap_while_the_dispatch_still_stands():
    s = SmartSlot(); d = [(T0, T0 + 3600, "SMART")]
    assert [s.update(T0 + 1790 + i, True, d if i == 0 else None) for i in range(20)] == [True] * 20


def test_it_runs_to_the_end_of_the_half_hour_when_the_car_stops_early():
    """Owner, 18 November 2026: the bill of 16 November (car 06:30 to 06:50) charged the whole 06:30 half-hour at the cheap rate, so the battery keeps
    charging until the half-hour ends, not until the car stops (17 November: the car stopped at 07:42 and the battery stopped with it)."""
    s = SmartSlot(); s.update(T0 + 60, True, [(T0, T0 + 3600, "SMART")])
    assert s.update(T0 + 70, True) is True
    assert all(s.update(T0 + t, False) for t in range(720, 1800, 10))           # the car stopped at 12 minutes: still cheap to 30 minutes
    assert s.update(T0 + 1800, False) is False                                  # the next half-hour needs the car charging again


def test_a_car_that_stops_does_not_carry_the_slot_into_the_next_half_hour():
    s = SmartSlot(); d = [(T0, T0 + 3600, "SMART")]
    s.update(T0 + 1700, True, d)
    assert s.update(T0 + 1750, False) is True and s.update(T0 + 1800, False) is False and s.update(T0 + 1900, False) is False


def test_a_car_that_starts_again_in_the_next_half_hour_starts_a_new_slot():
    s = SmartSlot(); d = [(T0, T0 + 3600, "SMART")]
    s.update(T0 + 60, True, d); s.update(T0 + 600, False)
    assert s.update(T0 + 1800, False) is False and s.update(T0 + 1900, True, d) is True


def test_a_new_slot_may_not_start_on_data_older_than_300_s():
    s = SmartSlot(); s.update(T0 + 10, False, [(T0, T0 + 7200, "SMART")])         # a good fetch, car idle
    assert s.update(T0 + 311, True, None, fetch_ok=False) is False                # 301 s old: too old to start


def test_data_up_to_300_s_old_can_start_a_slot():
    s = SmartSlot(); s.update(T0 + 10, False, [(T0, T0 + 7200, "SMART")])
    assert s.update(T0 + 310, True, None, fetch_ok=False) is True                 # exactly 300 s


def test_a_failed_fetch_is_not_an_empty_list_and_old_data_is_discarded_after_1800_s():
    s = SmartSlot(); s.update(T0 + 10, True, [(T0, T0 + 7200, "SMART")])
    assert s.dispatches is not None
    s.update(T0 + 10 + 1801, False, None, fetch_ok=False)
    assert s.dispatches is None


def test_a_dispatch_that_appears_minutes_late_starts_the_slot_when_it_appears():
    s = SmartSlot()
    assert s.update(T0 + 60, True, []) is False and s.update(T0 + 400, True, []) is False
    assert s.update(T0 + 420, True, [(T0 + 300, T0 + 3600, "SMART")]) is True


def test_no_allowance_is_counted_a_long_listed_slot_stays_cheap_while_the_car_charges():
    """GitHub issue 79 (owner, 12 November): Octopus manages the 6-hour allowance; its list is the authority. The night of 11 to 12 November the
    controller's own count reached 13 half-hours and called the 06:00 slot expensive: now nothing is counted."""
    s = SmartSlot()
    d = [(T0, T0 + 8 * 3600, "SMART")]                                           # an 8-hour listed slot: longer than any allowance
    seen = [s.update(T0 + 1 + i, True, d if i % 600 == 0 else None) for i in range(0, 8 * 3600 - 10, 10)]
    assert all(seen) and not hasattr(s, "used_half_hours")


def test_a_bump_charge_outside_any_listed_slot_is_never_cheap():
    """Owner, seen 9 November: a bump charge is billed at the peak rate. Without counting, it is simply outside every listed SMART slot."""
    day = utc(2026, 11, 4, 14, 0)                                                # 14:00 London: outside the 23:30 to 05:30 window
    bump = SmartSlot()
    assert not any(bump.update(day + i, True, [], fetch_ok=True) for i in range(0, 600, 10))
    other = SmartSlot()
    assert not any(other.update(day + i, True, [(day - 60, day + 3600, "BOOST")], fetch_ok=True) for i in range(0, 600, 10))


def test_a_listed_slot_is_cheap_only_while_the_car_charges():
    s = SmartSlot()
    d = [(T0, T0 + 3600, "SMART")]
    assert s.update(T0 + 10, False, d) is False                                 # listed, the car not charging: not cheap
    assert s.update(T0 + 20, True, d) is True                                   # the car starts: cheap


