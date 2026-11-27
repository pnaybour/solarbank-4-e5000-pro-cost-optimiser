"""Owner, 30 October 2026, kept by decision D15 (2 November): missing data is missing data, never "a car is charging". A charger gap up to 10 s changes nothing; a longer
gap holds only a car known to be charging, for up to 10 minutes. The car signal itself is owner specification v1.3 (D7): on above 1,000 W for 3 s, off below 300 W for 120 s."""
import solarbank_optimiser.control_logic as cl
from solarbank_optimiser.shelly_channels import CAR
from solarbank_optimiser.control_logic import Supervisor
from test_control_loop import fresh, plan, run, settled


def no_shelly():
    inputs = fresh(current=20)
    inputs.pop("shelly")
    return inputs


def car(watts):
    inputs = fresh(current=20)
    inputs["shelly"]["value"][CAR] = watts
    return inputs


def test_the_twenty_nine_october_pattern_one_missed_reading_every_half_hour_never_inhibits():
    sup, t = settled()
    good = fresh(current=20)
    inhibited = 0
    for minute in range(0, 600):                                       # ten hours, a missed reading every 30 minutes
        for second in range(60):
            now = t + minute * 60 + second
            missing = minute % 30 == 0 and second == 0
            d = sup.decide(no_shelly() if missing else good, plan("discharge"), None, now)
            inhibited += d.discharge == "INHIBIT"
    assert inhibited == 0


def test_a_dropout_inside_the_allowance_changes_nothing_not_even_a_note():
    sup = Supervisor()
    for s in range(0, 20):
        sup.decide(fresh(current=20), plan("discharge"), None, float(s))
    for s in range(20, 30):                                               # the last good reading was at 19: gaps of 1 to 10 s
        d = sup.decide(no_shelly(), plan("discharge"), None, float(s))
        assert d.discharge == "ALLOW" and "ev_data_missing" not in d.restrictions
    d = sup.decide(no_shelly(), plan("discharge"), None, 31.0)             # longer than 10 s with the car not charging: unknown, noted, discharge still the Anker's
    assert d.discharge == "ALLOW" and "ev_data_missing" in d.restrictions


def test_a_car_charging_through_a_short_dropout_stays_held_with_no_gap():
    sup, t = settled()
    for i in range(0, 5):
        sup.decide(car(7000.0), plan("discharge"), None, t + i)
    for s in range(5, 14):
        assert sup.decide(no_shelly(), plan("discharge"), None, t + s).discharge == "INHIBIT"
    assert sup.decide(car(7000.0), plan("discharge"), None, t + 14).discharge == "INHIBIT"


def test_after_a_restart_the_charger_is_read_and_missing_data_is_not_a_car():
    sup = Supervisor()
    for s in range(0, 120):
        d = sup.decide(no_shelly(), plan("discharge"), None, float(s))
        assert d.discharge == "ALLOW" and (s <= 10 or "ev_data_missing" in d.restrictions)
    assert Supervisor().decide(fresh(current=20), plan("discharge"), None, 0.0).discharge == "ALLOW"          # a working idle charger: nothing to wait for


def test_a_car_already_charging_at_a_restart_is_held_after_3_s():
    sup = Supervisor()
    seen = [sup.decide(car(7000.0), plan("discharge"), None, float(s)).discharge for s in range(0, 6)]
    assert seen[:3] == ["ALLOW"] * 3 and seen[3:] == ["INHIBIT"] * 3       # v1.3: confirmed after 3 s above 1,000 W


def test_a_low_soc_is_unaffected_by_the_missing_charger_reading():
    # GitHub issue 64 (D13): the minimum level is the firmware's alone now, not something this controller inhibits discharge for.
    sup, t = settled()
    inputs = fresh(soc=8, current=20)
    inputs.pop("shelly")
    d = sup.decide(inputs, plan("discharge"), None, t)
    assert d.discharge == "ALLOW" and "reserve" not in d.restrictions


def test_other_inputs_missing_do_not_change_the_charger_rule():
    sup, t = settled()
    inputs = fresh(current=20)
    inputs.pop("solarbank")
    assert sup.decide(inputs, plan("discharge"), None, t).discharge == "ALLOW"
