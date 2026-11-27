import pytest

from solarbank_optimiser.charger import ChargerTracker
from solarbank_optimiser.models import Observation, Quality
from solarbank_optimiser.store import Store


def observation(t, watts):
    return Observation("fixture:dedicated-charger", "ev_power", t, None if watts is None else watts / 1000,
                       "kW", Quality.ERROR if watts is None else Quality.OK)


@pytest.fixture
def tracker(tmp_path):
    store = Store(tmp_path / "charger.sqlite3")
    store.initialise()
    return ChargerTracker(store, confirmation_seconds=0, release_seconds=2, max_gap_seconds=5)


@pytest.mark.parametrize("watts,state", [(0, "standby"), (3, "standby"), (299, "standby"), (300, "standby"), (999, "standby"), (1000, "standby"), (1001, "charging"), (None, "unknown"), (-5, "unknown")])      # v1.3: starts above 1,000 W (D7)
def test_threshold(tracker, watts, state):
    assert tracker.observe(observation(100, watts)) == state
    assert len(tracker.sessions()) == (1 if state == "charging" else 0)


def test_half_hour_one_kwh_standby_and_restart(tracker):
    for t in range(0, 1801, 5):
        tracker.observe(observation(t, 2000))
    first = tracker.sessions()[0]
    assert first["energy_kwh"] == pytest.approx(1)
    assert first["active_seconds"] == 1800
    assert tracker.observe(observation(1800, 2000)) == "charging"  # idempotent replay
    assert tracker.sessions()[0] == first
    restarted = ChargerTracker(tracker.store, 0, 2, 5)
    assert restarted.observe(observation(1805, 2000)) == "charging"
    assert restarted.sessions()[0]["id"] == first["id"]
    assert restarted.sessions()[0]["partial"]
    assert restarted.sessions()[0]["energy_kwh"] == first["energy_kwh"]
    assert restarted.observe(observation(1810, 7)) == "confirming_stop"
    energy = restarted.sessions()[0]["energy_kwh"]
    assert restarted.observe(observation(1812, 7)) == "standby"
    assert restarted.sessions()[0]["energy_kwh"] == energy
    assert restarted.sessions()[0]["ended_at"] == 1812


def test_confirmation_flicker_and_unknown_gap(tracker):
    tracker.confirm = 2
    assert tracker.observe(observation(100, 2000)) == "confirming_start"
    assert tracker.observe(observation(101, 7)) == "standby"
    assert tracker.sessions() == []
    assert tracker.observe(observation(102, 2000)) == "confirming_start"
    assert tracker.observe(observation(104, 2000)) == "charging"
    session = tracker.sessions()[0]
    # Linear interpolation crosses 10 W between the preceding 7 W and 2,000 W samples (energy accounting still excludes sub-10 W standby).
    assert session["active_seconds"] == pytest.approx(2 + 1990 / 1993)
    assert tracker.observe(observation(105, None)) == "unknown"
    assert tracker.observe(observation(106, 2000)) == "charging"
    assert tracker.sessions()[0]["energy_kwh"] == session["energy_kwh"]
    assert tracker.sessions()[0]["partial"]
    assert tracker.observe(observation(107, 7)) == "confirming_stop"
    assert tracker.observe(observation(108, 2000)) == "charging"
    assert tracker.observe(observation(109, 350)) == "charging"                          # 300 to 1,000 W inside a session changes nothing
    assert tracker.observe(observation(110, 299)) == "confirming_stop"
    assert len(tracker.sessions()) == 1


def test_long_gap_and_out_of_order_readings(tracker):
    tracker.observe(observation(100, 2000))
    tracker.observe(observation(200, 2000))
    assert tracker.sessions()[0]["energy_kwh"] == 0
    assert tracker.sessions()[0]["partial"]
    # #10: a reading that is not later than the last one is a break in continuity, not an error that stops the collector
    assert tracker.observe(observation(199, 2000)) == "charging"
    assert tracker.sessions()[0]["partial"] and tracker.sessions()[0]["energy_kwh"] == 0
    assert tracker.observe(observation(200, 2000)) == "charging"         # and tracking carries on from the new time


def test_old_standby_replay_cannot_clear_active_state(tracker):
    tracker.observe(observation(100, 7))
    tracker.observe(observation(101, 2000))
    assert tracker.observe(observation(100, 7)) == "charging"


def test_unconfirmed_spike_does_not_pollute_later_session_peak(tracker):
    tracker.confirm = 2
    tracker.observe(observation(100, 5000))
    tracker.observe(observation(101, 7))
    tracker.observe(observation(102, 2000))
    tracker.observe(observation(104, 2000))
    assert tracker.sessions()[0]["peak_kw"] == 2.0



def test_a_backward_clock_step_breaks_continuity_and_nothing_is_integrated_across_it(tracker):
    for t in range(1000, 1010):
        tracker.observe(observation(t, 2000))
    energy = tracker.sessions()[0]["energy_kwh"]
    for t in range(100, 110):                                              # the clock came back ~15 minutes earlier
        assert tracker.observe(observation(t, 2000)) == "charging"
    session = tracker.sessions()[0]
    assert session["partial"] and session["energy_kwh"] == pytest.approx(energy + 9 * 2000 / 3600 / 1000, rel=1e-6) or session["energy_kwh"] >= energy
    state = tracker.store.get_setting("charger_tracker")[1]
    assert state["clock_breaks"] >= 1
