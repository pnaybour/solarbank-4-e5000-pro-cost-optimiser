"""WP1 (#1): the planner keeps the active plan across the date change and across 5 % battery-band changes inside its own window."""
from solarbank_optimiser.models import FakeClock, Observation
from solarbank_optimiser.settings import london_input
from solarbank_optimiser.shadow import ShadowWorker, controller_plan_document, plan_view
from solarbank_optimiser.shadow_solver import solve
from test_shadow import NOW, normal_history, put, site   # noqa: F401  (the fixture)


def at(store, clock, text, soc=.5):
    """Move to a London wall-clock time with a fresh battery reading and fresh weather, as the running system would have."""
    now = london_input(text)
    clock.wall = now
    store.add_observations([Observation("fixture-actual-soc", "soc", now - 1, soc, "fraction")])
    return now


def start(site, first="2026-10-25T23:30"):
    store, clock = site
    cache = store.get_setting("cache_weather")[1]            # one fresh forecast, taken just before the scenario; it is not touched again (a new forecast is a real input change)
    store.set_setting("cache_weather", {**cache, "acquired_at": london_input(first) - 60}, london_input(first), store.get_setting("cache_weather")[0])
    put(store, "fallback_tariff", {"day_pence": 34.96, "cheap_pence": 6.6, "valid_from": 0., "confirmed": True, "start_minute": 1410, "end_minute": 330})
    normal_history(store)
    return store, clock


def test_a_plan_made_at_2358_is_still_in_force_at_0002_and_the_replan_keeps_the_window_and_target(site):
    store, clock = start(site)
    t1 = at(store, clock, "2026-10-25T23:58")
    attempt = ShadowWorker(store, clock=clock, solver=solve).once()
    assert attempt["status"] == "published", attempt
    first = plan_view(store, t1)
    assert first["status"] == "checked_shadow" and first["next_window"]
    t2 = at(store, clock, "2026-10-26T00:00:30")                     # the date has changed, nothing else
    assert plan_view(store, t2)["status"] == "checked_shadow"
    t3 = at(store, clock, "2026-10-26T00:02")
    assert ShadowWorker(store, clock=clock, solver=solve).once()["status"] == "published"
    third = plan_view(store, t3)
    assert third["status"] == "checked_shadow" and third["next_window"]["end"] == first["next_window"]["end"]
    assert third["next_window"]["start"] <= t3 < third["next_window"]["end"]               # the window is still the one in force (its start is the plan's own time)
    assert third["next_window"]["target"] == first["next_window"]["target"]
    document = controller_plan_document(third, t3)
    assert document["action"] == "charge_to" and document["target_soc"] == first["next_window"]["target"]


def test_a_five_percent_band_change_inside_the_active_window_does_not_withdraw_the_plan(site):
    store, clock = start(site)
    t1 = at(store, clock, "2026-10-25T23:40")
    assert ShadowWorker(store, clock=clock, solver=solve).once()["status"] == "published"
    assert plan_view(store, t1)["next_window"]["start"] <= t1 < plan_view(store, t1)["next_window"]["end"]
    t2 = at(store, clock, "2026-10-25T23:42", soc=.58)               # charging has moved the battery into another 5 % band
    assert plan_view(store, t2)["status"] == "checked_shadow"


def test_a_band_change_outside_a_window_still_withdraws_and_so_does_any_other_change_inside_one(site):
    store, clock = start(site, "2026-10-25T12:00")
    t1 = at(store, clock, "2026-10-25T12:00")
    assert ShadowWorker(store, clock=clock, solver=solve).once()["status"] == "published"
    t2 = at(store, clock, "2026-10-25T12:05", soc=.58)
    assert plan_view(store, t2)["status"] == "unavailable"            # not in the window: as before
    store.set_setting("cache_weather", {**store.get_setting("cache_weather")[1], "acquired_at": london_input("2026-10-25T23:39")}, london_input("2026-10-25T12:06"), store.get_setting("cache_weather")[0])
    t3 = at(store, clock, "2026-10-25T23:40")
    assert ShadowWorker(store, clock=clock, solver=solve).once()["status"] == "published"
    put(store, "fallback_tariff", {"day_pence": 40.0, "cheap_pence": 6.6, "valid_from": 0., "confirmed": True, "start_minute": 1410, "end_minute": 330}, t3)
    assert plan_view(store, t3)["status"] == "unavailable"            # a settings change inside the window still withdraws it
