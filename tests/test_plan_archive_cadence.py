"""An unchanged plan is archived once per half hour, not on every replan (disk-write reduction after 31 Oct 2026)."""
from contextlib import closing

import pytest

from solarbank_optimiser.models import FakeClock, Observation
from solarbank_optimiser.shadow import PLAN_ARCHIVE_SECONDS, ShadowWorker, plan_view
from solarbank_optimiser.shadow_solver import solve
from solarbank_optimiser.store import Store
from test_shadow import NOW, site as _site   # noqa: F401  (the same fixture data, but on a store with a RAM live file)
import test_shadow


@pytest.fixture
def live_site(tmp_path):
    store = Store(tmp_path / "shadow.sqlite3", None, tmp_path / "live.json")
    store.initialise()
    base_dir = tmp_path / "plain"
    base_dir.mkdir()
    plain, clock = test_shadow.site.__wrapped__(base_dir)
    with closing(plain.connect()) as source, closing(store.connect()) as target:
        source.backup(target)
    return store, clock


def counts(store):
    with closing(store.connect()) as db:
        return (db.execute("SELECT count(*) FROM issuances WHERE json_extract(payload,'$.kind')='shadow_plan'").fetchone()[0],
                db.execute("SELECT count(*) FROM events WHERE kind='shadow_published'").fetchone()[0])


def replan(store, clock, seconds):
    clock.advance(seconds)
    store.add_observations([Observation(f"fixture-soc-{clock.utc()}", "soc", clock.utc() - 1, .5, "fraction")])
    return ShadowWorker(store, clock=clock, solver=solve).once()


def test_unchanged_replans_share_one_archive_and_refresh_only_the_ram_copy(live_site):
    store, clock = live_site
    assert replan(store, clock, 0)["status"] == "published"
    first = plan_view(store, clock.utc())
    assert counts(store) == (1, 1)
    for _ in range(3):
        assert replan(store, clock, 240)["status"] == "published"
    assert counts(store) == (1, 1)                                   # three more plans, no more archive rows or events
    latest = plan_view(store, clock.utc())
    assert latest["issuance_id"] == first["issuance_id"] and latest["issued_at"] > first["issued_at"]
    assert latest["valid_until"] > first["valid_until"]               # the controller still gets a fresh validity every time


def test_the_archive_is_renewed_after_half_an_hour(live_site):
    store, clock = live_site
    replan(store, clock, 0)
    replan(store, clock, PLAN_ARCHIVE_SECONDS + 1)
    assert counts(store) == (2, 2)


def test_a_changed_target_is_archived_straight_away(live_site, monkeypatch):
    store, clock = live_site
    replan(store, clock, 0)
    import solarbank_optimiser.shadow as shadow
    original = shadow.plan_key
    monkeypatch.setattr(shadow, "plan_key", lambda summary: (original(summary), summary["issued_at"]))
    replan(store, clock, 240)
    assert counts(store) == (2, 2)
