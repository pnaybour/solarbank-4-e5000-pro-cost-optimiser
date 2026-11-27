"""WP12: the pure garage calculation and regulator (owner specification v1.3 section 9)."""
import pytest

from solarbank_optimiser.garage import GarageRegulator, Reconciliation, binding_constraint, p_avail, voltage_invalid


def test_p_avail_is_the_headroom_times_the_voltage_rounded_down_to_10_w_and_zero_below_100_w():
    assert p_avail(30.0, 30.0, 0.0, 230.0) == 2500.0                       # 15 A x 230 V = 3,450 W: capped at P_MAX
    assert p_avail(41.0, 41.0, 0.0, 230.0) == 920.0                        # 4 A x 230 = 920
    assert p_avail(44.6, 44.6, 0.0, 230.0) == 0.0                  # 0.4 A x 230 = 92 W: below P_MIN, so 0
    assert p_avail(40.123, 40.123, 0.0, 230.0) == 1120.0                   # 4.877 x 230 = 1,121.7 -> 1,120
    assert p_avail(50.0, 50.0, 0.0, 230.0) == 0.0


def test_the_higher_of_the_latest_sample_and_the_average_is_used_and_the_battery_is_subtracted():
    assert p_avail(44.0, 20.0, 0.0, 230.0) == 230.0                        # I_eff = 44
    assert p_avail(20.0, 44.0, 0.0, 230.0) == 230.0
    assert p_avail(44.0, 44.0, 10.0, 230.0) == 2500.0                      # 34 A other, 11 A of headroom
    assert p_avail(44.0, 44.0, 50.0, 230.0) == 2500.0                      # a battery reading above the total cannot make 'other' negative
    assert p_avail(44.0, 44.0, None, 230.0) == 230.0                       # not subtracted: conservative


def test_p_avail_is_capped_by_the_device_maximum():
    assert p_avail(20.0, 20.0, 0.0, 230.0, p_max_live=1800.0) == 1800.0


def test_the_over_current_alert_needs_10_s_above_the_limit_with_the_set_point_already_zero():
    reg = GarageRegulator()
    for i in range(0, 10):
        reg.update(float(i), 2500.0, 47.0, 0.0, 230.0, 0.3, 0.3)
    assert not reg.alerting
    reg.update(10.0, 2500.0, 47.0, 0.0, 230.0, 0.3, 0.3); reg.update(11.0, 2500.0, 47.0, 0.0, 230.0, 0.3, 0.3)
    assert reg.alerting
    reg.update(12.0, 2500.0, 30.0, 0.0, 230.0, 0.3, 0.3); reg.update(13.0, 2500.0, 30.0, 0.0, 230.0, 0.3, 0.3)
    reg.update(14.0, 2500.0, 30.0, 0.0, 230.0, 0.3, 0.3); reg.update(15.0, 2500.0, 30.0, 0.0, 230.0, 0.3, 0.3)
    assert not reg.alerting


def charging(reg, seconds=40):
    for i in range(0, seconds):
        reg.update(float(i), 2500.0, 20.0, 0.0, 230.0, 0.3, 0.3)
    assert reg.setpoint_w == 2500.0
    return float(seconds)


def test_a_voltage_outside_207_to_253_v_is_not_trusted_so_the_battery_is_not_subtracted_and_the_set_point_is_only_held():
    """GitHub issue 80: not subtracting the battery is no reason to cut while the measured total (which includes it) is within the limit."""
    reg = GarageRegulator(); t = charging(reg)
    _, notes = reg.update(t, 2500.0, 44.0, -2500.0, 300.0, 0.3, 0.3)       # 44 A in total, battery 10.9 A, but 300 V is not credible
    assert "unaligned" in notes and "held" in notes and reg.setpoint_w == 2500.0
    reg.update(t + 1, 2500.0, 46.0, -2500.0, 300.0, 0.3, 0.3)             # over the limit: the conservative headroom, a cut at once
    assert reg.setpoint_w < 2500.0


def test_the_garage_reading_age_gap_decides_alignment():
    reg = GarageRegulator(); t = charging(reg)
    _, notes = reg.update(t, 2500.0, 44.0, -2500.0, 230.0, 0.3, 8.3)      # 8 s apart
    assert "unaligned" in notes and reg.setpoint_w == 2500.0               # held: 44 A measured, within the limit (issue 80)


def test_unaligned_readings_over_the_limit_still_cut_and_never_allow_an_increase():
    reg = GarageRegulator(); t = charging(reg)
    reg.update(t, 2500.0, 46.5, -2500.0, 230.0, 0.3, 8.3)                  # 46.5 A measured, readings 8 s apart
    cut = reg.setpoint_w
    assert cut < 2500.0
    for i in range(1, 30):
        reg.update(t + i, 2500.0, 30.0, -cut, 230.0, 0.3, 8.3)             # back well within the limit, still unaligned: held at the cut, never raised
    assert reg.setpoint_w == cut


def test_the_night_of_11_to_12_november_no_longer_flip_flops():
    """GitHub issue 80: the car at about 31 A and the battery at about 11 A (42.5 A measured, under 45 A), the alignment flickering between cycles.
    The set-point used to jump between 2500 W and about 550 W every few seconds; it now stays at 2500 W."""
    reg = GarageRegulator(); t = charging(reg)
    seen = set()
    for i in range(120):
        aligned = i % 3 != 1                                               # every third cycle the battery reading is a second older
        reg.update(t + i, 2500.0, 42.5, -2500.0, 236.0, 0.3, 0.3 if aligned else 1.6)
        seen.add(reg.setpoint_w)
    assert seen == {2500.0}


def test_binding_constraint_names_the_smaller_of_the_two_ceilings():
    # GitHub issue 73 (found 10 November): which ceiling actually governs the set-point, so the reason text can blame the real cause.
    assert binding_constraint(44.0, 44.0, 0.0, 230.0) == "garage"                          # 1 A headroom x 230 V = 230 W < the 2,500 W device ceiling
    assert binding_constraint(20.0, 20.0, 0.0, 230.0, p_max_live=500.0) == "device"        # 25 A headroom = 5,750 W > the 500 W live device ceiling
    assert binding_constraint(44.0, 44.0, 0.0, 230.0, p_max_live=230.0) is None             # both ceilings are 230 W: a tie, neither named
    assert binding_constraint(20.0, 20.0, None, 230.0, p_max_live=500.0) == "device"        # i_batt unknown: still mirrors p_avail's own arithmetic


def test_the_regulator_records_which_ceiling_bound_the_setpoint():
    garage_bound = GarageRegulator()
    for i in range(0, 40):
        garage_bound.update(float(i), 2500.0, 44.0, 0.0, 230.0, 0.3, 0.3)       # 44 A: the 45 A feed is the smaller ceiling
    assert garage_bound.setpoint_w < 2500.0 and garage_bound.bound_by == "garage"

    device_bound = GarageRegulator()
    for i in range(0, 40):
        device_bound.update(float(i), 2500.0, 20.0, 0.0, 230.0, 0.3, 0.3, max_live_w=500.0)   # plenty of garage headroom; the device's live limit is smaller
    assert device_bound.setpoint_w == 500.0 and device_bound.bound_by == "device"

    not_capped = GarageRegulator()
    for i in range(0, 40):
        not_capped.update(float(i), 230.0, 20.0, 0.0, 230.0, 0.3, 0.3)          # wants only 230 W, well within every ceiling: nothing is binding
    assert not_capped.bound_by is None

    starting = GarageRegulator()
    starting.update(0.0, 2500.0, 44.0, 0.0, 230.0, 0.3, 0.3)                    # still inside VALID_HOLD_S: too soon to attribute a ceiling
    assert starting.bound_by is None


# ===== specification 11.2 validity checks (GitHub issue 69, owner decision A, with an email alert) ================================================
def test_voltage_invalid_flags_out_of_range_or_a_missing_reading_but_not_one_in_range():
    assert voltage_invalid(206.0) and voltage_invalid(254.0) and voltage_invalid(None)       # a missing reading is unknown, not the nominal 230 V
    assert not voltage_invalid(230.0) and not voltage_invalid(207.0) and not voltage_invalid(253.0)


def test_reconciliation_needs_30_s_of_a_low_p_unmetered_before_invalid():
    recon = Reconciliation()
    assert recon.update(0.0, 0.0, 300.0, True, 0.0) is False            # P_unmetered = 0 - 0 - 300 = -300 W: drops to "low", not yet invalid
    assert recon.update(0.0, 0.0, 300.0, True, 29.0) is False           # 29 s since the drop: still not invalid
    assert recon.update(0.0, 0.0, 300.0, True, 31.0) is True            # 31 s: confirmed invalid


def test_reconciliation_clears_at_once_once_p_unmetered_recovers():
    recon = Reconciliation()
    recon.update(0.0, 0.0, 300.0, True, 0.0)
    assert recon.update(0.0, 0.0, 300.0, True, 31.0) is True
    assert recon.update(0.0, 0.0, 0.0, True, 32.0) is False              # back above -230 W: clears at once, no dwell needed to clear


def test_reconciliation_holds_through_unaligned_or_missing_readings_rather_than_resetting():
    recon = Reconciliation()
    recon.update(0.0, 0.0, 300.0, True, 0.0)                             # drop, at t=0
    assert recon.update(0.0, 0.0, 300.0, False, 20.0) is False            # not aligned at t=20: not used (readings more than ALIGN_S apart)
    assert recon.update(None, 0.0, 300.0, True, 20.0) is False            # P_garage missing: also not used
    assert recon.update(0.0, 0.0, 300.0, True, 31.0) is True              # 31 s after the original drop (the gaps did not restart the timer)
