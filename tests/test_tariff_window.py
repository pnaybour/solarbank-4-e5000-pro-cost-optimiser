"""The cheap-rate window comes from the Octopus product (its description), with the Settings-page window as the fallback."""
import json

import pytest

from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.services import ProviderConfig, ProviderWorker, cache_identity, tariff_horizon
from solarbank_optimiser.store import Store
from solarbank_optimiser.tariff_window import api_window, describe, effective_tariff, parse_night_window
from test_phase3 import NOW, fallback, put

REAL_TEXT = ("With Intelligent Octopus Go EV tariff, you have access to a super low electricity rate between 23:30 - 05:30 every night, "
             "plus it smart-charges your car at the cheapest and greenest times overnight.")


# ===== reading the text ================================================================================================================================
@pytest.mark.parametrize("text,expected", [
    (REAL_TEXT, (1410, 330)),
    ("cheap from 00:30 to 04:30 daily", (30, 270)),
    ("off-peak 22:00 – 06:00", (1320, 360)),
    ("between 23:30 and 05:30", (1410, 330)),
    ("23:30-05:30 and again 23:30 - 05:30", (1410, 330)),            # the same window stated twice is still one window
    ("cheap 23:30 - 05:30, peak 16:00 - 19:00", None),               # two different ranges: ambiguous
    ("no times here", None), ("", None), (None, None), (5, None),
    ("rate 23:15 - 05:30", None),                                    # not half-hour aligned
    ("rate 25:00 - 05:30", None), ("rate 01:00 - 01:00", None),
    ("rate 23:30 - 23:59", None), ("rate 00:00 - 20:00", None),      # implausibly short or long
])
def test_the_window_is_read_only_when_the_text_names_exactly_one_plausible_range(text, expected):
    assert parse_night_window(text) == expected


def test_a_saved_window_is_used_only_when_well_formed_and_for_this_product():
    good = {"start_minute": 1410, "end_minute": 330, "product": "P", "source": "x"}
    assert api_window({"night_window": good}, "P") == good and api_window({"night_window": good}) == good
    assert api_window({"night_window": good}, "OTHER") is None
    for bad in ({"start_minute": 1410}, {"start_minute": "a", "end_minute": 5}, {"start_minute": 5, "end_minute": 5}, {"start_minute": 1500, "end_minute": 5}, True, None, []):
        assert api_window({"night_window": bad}) is None
    assert api_window(None) is None and api_window({}) is None


def test_effective_tariff_prefers_the_octopus_window_and_keeps_the_prices():
    tariff = {"day_pence": 30.0, "cheap_pence": 8.0, "start_minute": 1320, "end_minute": 240}
    merged, source = effective_tariff(tariff, {"night_window": {"start_minute": 1410, "end_minute": 330, "product": "P"}}, "P")
    assert source == "octopus" and (merged["start_minute"], merged["end_minute"]) == (1410, 330) and merged["day_pence"] == 30.0
    assert tariff["start_minute"] == 1320                                           # the saved setting itself is untouched
    assert effective_tariff(tariff, {}, "P") == (tariff, "settings") and effective_tariff(None, {}, "P") == (None, "none")
    assert describe(merged, source)["text"] == "23:30 to 05:30 London, from the Octopus product"
    assert "does not state one" in describe(tariff, "settings")["text"]


# ===== the provider worker and the planner ================================================================================================================
def day_night_transport(description=REAL_TEXT, product_fails=False):
    calls = []

    def transport(host, path):
        calls.append(path)
        if path == "/v1/products/FIXTURE/":
            if product_fails:
                raise OSError("down")
            return json.dumps({"code": "FIXTURE", "display_name": "Fixture Go", "description": description}).encode()
        if "standard-unit-rates" in path:
            return json.dumps({"results": [], "next": None}).encode()
        value = 30.0 if "day-unit-rates" in path else 7.0
        return json.dumps({"results": [{"valid_from": "2026-01-01T00:00:00Z", "valid_to": None, "value_inc_vat": value}], "next": None}).encode()
    return transport, calls


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "w.sqlite3"); result.initialise()
    return result


def run_worker(store, transport):
    cfg = ProviderConfig(prices_enabled=True, product="FIXTURE", tariff="E-1R-FIXTURE-A")
    put(store, "providers", cfg.model_dump())
    ProviderWorker(store, clock=FakeClock(NOW), transport=transport).once()
    return cfg


def test_the_worker_saves_the_window_from_the_product_description(store):
    transport, calls = day_night_transport()
    run_worker(store, transport)
    cache = store.get_setting("cache_prices")[1]
    assert cache["night_window"] == {"start_minute": 1410, "end_minute": 330, "product": "FIXTURE", "source": "Octopus product description"}
    assert cache["night_window_source"] == "Octopus product description" and "/v1/products/FIXTURE/" in calls


def test_no_window_in_the_text_or_a_failed_lookup_leaves_the_configured_one(store):
    for transport in (day_night_transport("A great tariff.")[0], day_night_transport(product_fails=True)[0]):
        s = Store(store.path.parent / f"x{id(transport)}.sqlite3"); s.initialise()
        run_worker(s, transport)
        cache = s.get_setting("cache_prices")[1]
        assert "night_window" not in cache and cache["night_window_source"] == "Owner-confirmed London night window"
        assert s.get_setting("provider_prices")[1]["status"] == "ok"                      # the prices themselves still worked


def test_the_planner_uses_the_octopus_window_over_a_different_configured_one(store):
    cfg = run_worker(store, day_night_transport()[0])
    fallback(store, start_minute=22 * 60, end_minute=4 * 60)                               # a stale window typed on the Settings page: 22:00 to 04:00
    put(store, "provider_prices", {"status": "ok"})
    cache = store.get_setting("cache_prices")[1]
    assert cache["identity"] == cache_identity(cfg, "prices")
    horizon = tariff_horizon(store, NOW, NOW, NOW + 24 * 3600)
    cheap = [(r["start"], r["end"]) for r in horizon["intervals"] if r["cheap"]]
    assert cheap, "some cheap time is expected in 24 hours"
    from datetime import datetime
    from solarbank_optimiser.energy import LONDON
    minutes = {datetime.fromtimestamp(a + 1, LONDON).hour * 60 + datetime.fromtimestamp(a + 1, LONDON).minute for a, _ in cheap}
    assert 1410 in minutes or 0 in minutes                                                  # the 23:30 start is cheap
    assert all(m not in minutes for m in (22 * 60, 22 * 60 + 30, 23 * 60))                 # 22:00 to 23:30 is NOT cheap, whatever Settings says
    assert "from the Octopus product" in horizon["intervals"][0]["source"] or "from the Octopus product" in " ".join(r["source"] for r in horizon["intervals"])


def test_without_an_api_window_the_configured_one_still_applies(store):
    run_worker(store, day_night_transport("A great tariff.")[0])
    fallback(store, start_minute=22 * 60, end_minute=4 * 60)
    put(store, "provider_prices", {"status": "ok"})
    horizon = tariff_horizon(store, NOW, NOW, NOW + 24 * 3600)
    from datetime import datetime
    from solarbank_optimiser.energy import LONDON
    minutes = {datetime.fromtimestamp(r["start"] + 1, LONDON).hour * 60 + datetime.fromtimestamp(r["start"] + 1, LONDON).minute for r in horizon["intervals"] if r["cheap"]}
    assert 22 * 60 in minutes or 22 * 60 + 30 in minutes


# ===== #19: a disabled provider or a stale cache must not override the saved window ==================================================================
def _cheap_minutes(horizon):
    from datetime import datetime
    from solarbank_optimiser.energy import LONDON
    return {datetime.fromtimestamp(r["start"] + 1, LONDON).hour * 60 + datetime.fromtimestamp(r["start"] + 1, LONDON).minute for r in horizon["intervals"] if r["cheap"]}


def _london_ts(hour, minute, base=None):
    from datetime import datetime
    from solarbank_optimiser.energy import LONDON
    day = datetime.fromtimestamp(base if base is not None else NOW, LONDON)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def _saved_window_scenario(store, later_by, provider_enabled):
    """The reviewer's example: the product states 23:30 to 05:30, the owner saved 00:00 to 07:00, and the cache is old or the provider is off."""
    cfg = run_worker(store, day_night_transport()[0])
    fallback(store, start_minute=0, end_minute=7 * 60)
    put(store, "provider_prices", {"status": "ok"})
    if not provider_enabled:
        put(store, "providers", {**cfg.model_dump(), "prices_enabled": False})
    return NOW + later_by


@pytest.mark.parametrize("later_by,provider_enabled", [(100 * 86400, False), (100 * 86400, True), (0, False)])
def test_19_a_disabled_provider_or_a_100_day_old_cache_gives_the_saved_window_so_23_45_is_the_day_rate(store, later_by, provider_enabled):
    now = _saved_window_scenario(store, later_by, provider_enabled)
    horizon = tariff_horizon(store, now, now, now + 24 * 3600)
    minutes = _cheap_minutes(horizon)
    assert 23 * 60 + 45 - 15 not in minutes and 23 * 60 + 30 not in minutes          # 23:30 to 00:00 is NOT cheap: the saved window starts at 00:00
    assert 0 in minutes or 30 in minutes                                              # the saved 00:00 to 07:00 is
    assert not any("from the Octopus product" in r["source"] for r in horizon["intervals"])


def test_19_every_reader_of_the_window_agrees_that_an_unusable_cache_does_not_count(store):
    from solarbank_optimiser.services import window_cache
    now = _saved_window_scenario(store, 100 * 86400, True)
    assert window_cache(store, now) == {}
    assert window_cache(store, NOW) != {}                                              # the same cache, fresh and enabled, is used
    from solarbank_optimiser.daily_record import tariff_pricer
    price_at, label = tariff_pricer(store, now=now)
    at = lambda hour, minute: price_at(_london_ts(hour, minute, base=now))
    assert at(23, 45)[1] is False and at(0, 30)[1] is True                           # the history prices use the saved window too


def test_19_settings_shows_the_saved_window_beside_the_one_in_use_and_says_why(store):
    from solarbank_optimiser.services import window_cache
    from solarbank_optimiser.tariff_window import unused_reason
    now = _saved_window_scenario(store, 100 * 86400, True)
    usable = window_cache(store, now)
    cache = store.get_setting("cache_prices")[1]
    tariff, source = effective_tariff(store.get_setting("fallback_tariff")[1], usable, "FIXTURE")
    shown = describe(tariff, source, store.get_setting("fallback_tariff")[1], unused_reason(cache, "FIXTURE", usable))
    assert shown["source"] == "settings" and shown["text"].startswith("00:00 to 07:00") and shown["saved"]["text"] == "00:00 to 07:00 London"
    assert "saved but not used" in shown["note"]
    fresh = describe(*effective_tariff(store.get_setting("fallback_tariff")[1], window_cache(store, NOW), "FIXTURE"), store.get_setting("fallback_tariff")[1],
                     unused_reason(cache, "FIXTURE", window_cache(store, NOW)))
    assert fresh["source"] == "octopus" and fresh["saved"]["text"] == "00:00 to 07:00 London" and "note" not in fresh
