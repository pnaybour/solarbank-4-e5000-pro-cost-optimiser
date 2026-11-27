"""Working records held in RAM and copied to the database at most every half hour (disk-write reduction after the 31 Oct 2026 stall)."""
from contextlib import closing
import json

import pytest

from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.services import ProviderConfig, ProviderWorker
from solarbank_optimiser.store import Store

T0 = 1_793_000_000.0


def make(tmp_path, live=True):
    store = Store(tmp_path / "c.sqlite3", None, tmp_path / "live.json" if live else None)
    store.initialise()
    return store


def database_rows(store, name):
    with closing(store.connect()) as db:
        return {"settings": db.execute("SELECT revision FROM settings WHERE name=?", (name,)).fetchall(),
                "versions": db.execute("SELECT count(*) FROM setting_versions WHERE name=?", (name,)).fetchone()[0],
                "events": db.execute("SELECT count(*) FROM events WHERE kind='settings_changed' AND payload LIKE ?", (f'%"{name}"%',)).fetchone()[0]}


def save(store, name, value, now):
    return store.set_setting(name, value, now, store.get_setting(name)[0])


def test_many_changes_reach_the_database_once_per_half_hour(tmp_path):
    store = make(tmp_path)
    for minute in range(30):                                         # one change a minute for half an hour
        save(store, "cache_slots", {"n": minute}, T0 + 60 * minute)
    rows = database_rows(store, "cache_slots")
    assert rows["versions"] == 1 and rows["events"] == 1             # the first write only
    assert store.get_setting("cache_slots") == (30, {"n": 29})       # yet every reader sees the newest value and a counting revision
    save(store, "cache_slots", {"n": 30}, T0 + 1800)                 # half an hour after the last database copy
    assert database_rows(store, "cache_slots")["versions"] == 2


def test_other_processes_see_the_ram_copy_and_the_database_stays_older(tmp_path):
    first, second = make(tmp_path), Store(tmp_path / "c.sqlite3", None, tmp_path / "live.json")
    save(first, "shadow_attempt", {"status": "published"}, T0)
    save(first, "shadow_attempt", {"status": "unavailable"}, T0 + 60)
    assert second.get_setting("shadow_attempt") == (2, {"status": "unavailable"})
    with closing(first.connect()) as db:
        assert json.loads(db.execute("SELECT payload FROM settings WHERE name='shadow_attempt'").fetchone()[0]) == {"status": "published"}


def test_stale_revision_is_still_refused(tmp_path):
    store = make(tmp_path)
    save(store, "provider_slots", {"a": 1}, T0)
    with pytest.raises(ValueError, match="Settings changed"):
        store.set_setting("provider_slots", {"a": 2}, T0 + 1, 0)


def test_flush_copies_the_newest_value_when_a_service_stops(tmp_path):
    store = make(tmp_path)
    save(store, "established_slots", {"v": 1}, T0)
    save(store, "established_slots", {"v": 2}, T0 + 60)
    assert store.flush_ram_settings() == ["established_slots"]
    with closing(store.connect()) as db:
        assert json.loads(db.execute("SELECT payload FROM settings WHERE name='established_slots'").fetchone()[0]) == {"v": 2}
    assert store.flush_ram_settings() == []                   # nothing new to copy the second time


def test_after_a_reboot_the_database_copy_is_used_and_counting_continues(tmp_path):
    store = make(tmp_path)
    save(store, "cache_slots", {"n": 1}, T0)
    save(store, "cache_slots", {"n": 2}, T0 + 60)
    store.flush_ram_settings()
    (tmp_path / "ram-cache_slots.json").unlink()                     # /run is emptied by a reboot
    assert store.get_setting("cache_slots") == (2, {"n": 2})
    assert save(store, "cache_slots", {"n": 3}, T0 + 3600) == 3


def test_a_ram_file_from_another_database_is_ignored(tmp_path):
    store = make(tmp_path)
    save(store, "cache_slots", {"n": 1}, T0)
    other = Store(tmp_path / "other.sqlite3", None, tmp_path / "live.json")
    other.initialise()
    assert other.get_setting("cache_slots") == (0, None)


def test_without_a_live_file_every_change_is_written_as_before(tmp_path):
    store = make(tmp_path, live=False)
    for minute in range(3):
        save(store, "cache_slots", {"n": minute}, T0 + 60 * minute)
    assert database_rows(store, "cache_slots")["versions"] == 3


def test_ordinary_settings_are_never_held_back(tmp_path):
    store = make(tmp_path)
    for n in range(3):
        save(store, "demand", {"n": n}, T0 + n)
    assert database_rows(store, "demand")["versions"] == 3


def test_planner_snapshot_sees_the_newest_slot_list(tmp_path):
    from solarbank_optimiser.shadow import snapshot
    store = make(tmp_path)
    save(store, "cache_slots", {"intervals": [], "acquired_at": T0}, T0)
    save(store, "cache_slots", {"intervals": [], "acquired_at": T0 + 60}, T0 + 60)
    assert snapshot(store, T0 + 61)["settings"]["cache_slots"][1]["acquired_at"] == T0 + 60


def test_an_unchanged_slot_list_is_archived_once_not_every_minute(tmp_path):
    store = make(tmp_path)
    clock = FakeClock(T0)
    worker = ProviderWorker(store, clock=clock, transport=lambda *a: pytest.fail("no network"))
    worker.secrets = type("Key", (), {"read": lambda self: "fixture-key"})()
    worker.account.dispatches = lambda *a: []
    config = ProviderConfig(account_enabled=True, account="A-FIXTURE", device="fixture-device")
    for minute in range(10):
        clock.wall = T0 + 60 * minute
        worker.refresh("slots", config, clock.utc())
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM issuances").fetchone()[0] == 1
    cache = store.get_setting("cache_slots")[1]
    assert cache["acquired_at"] == T0 + 540 and cache["archived_at"] == T0     # still fresh for the planner, evidence archived once
    clock.wall = T0 + 1800
    worker.refresh("slots", config, clock.utc())
    with closing(store.connect()) as db:
        assert db.execute("SELECT count(*) FROM issuances").fetchone()[0] == 2


def test_refreshing_slots_also_publishes_the_dispatch_feed_for_the_controller(tmp_path):
    """GitHub issue 67 (specification 6.2, owner decision A): the controller never sees this database, so the providers service shares the
    Octopus SMART dispatches as their own small file, like the plan file, every time it actually asks Octopus -- even when the answer (here,
    an empty list) is unchanged, since that is still a fresh fetch and SMARTSLOT's freshness rules are about Octopus being asked, not about
    the evidence record (which an unchanged answer does not re-archive) changing."""
    store = make(tmp_path)
    clock = FakeClock(T0)
    feed = tmp_path / "tariff.json"
    worker = ProviderWorker(store, clock=clock, transport=lambda *a: pytest.fail("no network"), dispatch_feed_path=feed)
    worker.secrets = type("Key", (), {"read": lambda self: "fixture-key"})()
    worker.account.dispatches = lambda *a: [{"start": T0 + 1000, "end": T0 + 2000}]
    config = ProviderConfig(account_enabled=True, account="A-FIXTURE", device="fixture-device")
    worker.refresh("slots", config, clock.utc())
    assert json.loads(feed.read_text()) == {"dispatches": [[T0 + 1000, T0 + 2000, "SMART"]], "fetched_at": T0}
    clock.wall = T0 + 600
    worker.account.dispatches = lambda *a: []                                     # Octopus re-plans and currently shows nothing
    worker.refresh("slots", config, clock.utc())
    assert json.loads(feed.read_text()) == {"dispatches": [], "fetched_at": T0 + 600}
