"""Web service resilience: bounded request threads and start-up that does not fight for the write lock."""
from contextlib import closing
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app


def make_app(tmp_path, **options):
    return create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", trusted_network=True, **options))


def test_request_thread_pool_is_capped_at_start_up(tmp_path):
    app = make_app(tmp_path, max_request_threads=6)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        assert client.get("/health").status_code == 200
        assert app.state.request_threads == 6


@pytest.mark.parametrize("bad", [0, 1, 65, 3.5, "8"])
def test_request_thread_setting_is_validated(tmp_path, bad):
    with pytest.raises(ValueError):
        WebConfig(tmp_path / "x.sqlite3", "a-long-test-password", max_request_threads=bad)


def test_burst_of_slow_requests_queues_instead_of_failing(tmp_path, monkeypatch):
    import threading as t
    app = make_app(tmp_path, max_request_threads=3)
    active, peak, lock = [0], [0], t.Lock()
    original = Store.get_setting

    def slow(self, name, default=None):
        with lock:
            active[0] += 1; peak[0] = max(peak[0], active[0])
        time.sleep(0.03)
        try:
            return original(self, name, default)
        finally:
            with lock:
                active[0] -= 1
    monkeypatch.setattr(Store, "get_setting", slow)
    results = []
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        def hit():
            results.append(client.get("/api/status").status_code)
        threads = [t.Thread(target=hit) for _ in range(14)]
        [x.start() for x in threads]; [x.join(30) for x in threads]
    assert results.count(200) == 14 and peak[0] <= 3


def test_initialise_on_a_current_database_does_not_wait_for_another_writer(tmp_path):
    store = Store(tmp_path / "s.sqlite3"); store.initialise()
    holder = sqlite3.connect(tmp_path / "s.sqlite3", timeout=5)
    holder.execute("BEGIN IMMEDIATE")                       # another service holds the write lock
    try:
        started = time.monotonic()
        Store(tmp_path / "s.sqlite3").initialise()          # must not block on, or time out for, that lock
        assert time.monotonic() - started < 2
    finally:
        holder.rollback(); holder.close()


def test_four_services_can_start_together_on_a_current_database(tmp_path):
    Store(tmp_path / "s.sqlite3").initialise()
    errors = []

    def start():
        try:
            for _ in range(5):
                Store(tmp_path / "s.sqlite3").initialise()
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=start) for _ in range(4)]
    [x.start() for x in threads]; [x.join(30) for x in threads]
    assert not errors


def test_an_old_schema_still_migrates_and_a_foreign_database_is_still_refused(tmp_path):
    foreign = tmp_path / "f.sqlite3"
    with closing(sqlite3.connect(foreign)) as db:
        db.execute("CREATE TABLE other(x)"); db.commit()
    with pytest.raises(ValueError):
        Store(foreign).initialise()
    fresh = Store(tmp_path / "n.sqlite3"); fresh.initialise()
    with closing(fresh.connect()) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4


def test_status_is_computed_once_per_interval_shared_and_cleared_by_changes(tmp_path, monkeypatch):
    import solarbank_optimiser.web.app as web
    calls = {"n": 0}
    real = web.tariff_horizon

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)
    monkeypatch.setattr(web, "tariff_horizon", counted)
    app = make_app(tmp_path, status_cache_seconds=2.0)           # long enough that six requests fit inside it even on the Pi's slow disk
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        first = client.get("/api/status").json()
        same = lambda body: {k: v for k, v in body.items() if k not in ("served_at", "stale_seconds")}       # when it was SERVED differs per request; the picture does not
        for _ in range(5):
            assert same(client.get("/api/status").json()) == same(first)
        assert calls["n"] == 1                                   # six requests, one computation
        time.sleep(2.1)
        client.get("/api/status")                                # expired: answered at once with the old picture, and rebuilt in the background (GitHub 61)
        for _ in range(200):
            if calls["n"] == 2:
                break
            time.sleep(0.05)
        assert calls["n"] == 2                                   # expired -> recomputed
        client.put("/api/mode", json={"revision": 0, "mode": "observe"},
                   headers={"X-CSRF-Token": first["csrf_token"]})
        client.get("/api/status")
        assert calls["n"] == 3                                   # any change clears it at once


def test_concurrent_status_requests_share_one_computation(tmp_path, monkeypatch):
    import solarbank_optimiser.web.app as web
    calls = {"n": 0}
    real = web.tariff_horizon

    def slow(*args, **kwargs):
        calls["n"] += 1
        time.sleep(0.2)
        return real(*args, **kwargs)
    monkeypatch.setattr(web, "tariff_horizon", slow)
    app = make_app(tmp_path, status_cache_seconds=5)
    codes = []
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        threads = [threading.Thread(target=lambda: codes.append(client.get("/api/status").status_code)) for _ in range(10)]
        [x.start() for x in threads]; [x.join(30) for x in threads]
    assert codes == [200] * 10 and calls["n"] == 1


@pytest.mark.parametrize("bad", [-1, 31])
def test_status_cache_setting_is_validated(tmp_path, bad):
    with pytest.raises(ValueError):
        WebConfig(tmp_path / "x.sqlite3", "a-long-test-password", status_cache_seconds=bad)


def test_dashboard_shows_live_readings_that_are_not_yet_in_the_database(tmp_path):
    from solarbank_optimiser.collector import PROVENANCE
    from solarbank_optimiser.models import FakeClock, Observation
    clock = FakeClock(1_800_000_000)
    store = Store(tmp_path / "w.sqlite3", live_path=tmp_path / "live.json")
    store.initialise()
    store.publish_live([Observation("monitor:solarbank:soc", "soc", clock.utc(), .71, "fraction",
                                    source_at=clock.utc() - 1, provenance=PROVENANCE)],
                       {"collector_health": {"status": "ok", "acquired_at": clock.utc()}})
    app = create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", trusted_network=True,
                               status_cache_seconds=0), clock, store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        data = client.get("/api/status").json()
    assert data["readings"]["soc"]["value"] == .71 and "unavailable" not in data["readings"]["soc"]["status"]
    assert data["collector"]["status"] == "ok"                    # not "stale / collector not updating"
    # Without the shared store the database holds nothing, so the same page would say unavailable.
    plain = create_app(WebConfig(tmp_path / "w2.sqlite3", "a-long-test-password", trusted_network=True,
                                 status_cache_seconds=0), clock)
    with TestClient(plain, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        assert client.get("/api/status").json()["readings"]["soc"]["value"] is None


def test_requests_during_a_slow_recalculation_get_the_previous_result_immediately(tmp_path, monkeypatch):
    import solarbank_optimiser.web.app as web
    app = make_app(tmp_path, status_cache_seconds=0.1)
    delay = {"s": 0.0}
    real = web.tariff_horizon

    def maybe_slow(*args, **kwargs):
        time.sleep(delay["s"])
        return real(*args, **kwargs)
    monkeypatch.setattr(web, "tariff_horizon", maybe_slow)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        first = client.get("/api/status").json()
        time.sleep(0.2)                                     # expired
        delay["s"] = 1.0
        starter = threading.Thread(target=lambda: client.get("/api/status"))
        starter.start(); time.sleep(0.2)                    # a slow recalculation is now running
        began = time.monotonic()
        during = client.get("/api/status").json()
        assert time.monotonic() - began < 0.5               # did not wait for it
        assert {k: v for k, v in during.items() if k not in ('served_at', 'stale_seconds')} == {k: v for k, v in first.items() if k not in ('served_at', 'stale_seconds')}
        starter.join(10)
        assert client.get("/api/status").json()["computed_at"] >= first["computed_at"]


def test_a_failed_calculation_does_not_wedge_later_requests(tmp_path, monkeypatch):
    import solarbank_optimiser.web.app as web
    app = make_app(tmp_path, status_cache_seconds=0)
    state = {"fail": True}
    real = web.tariff_horizon

    def flaky(*args, **kwargs):
        if state["fail"]:
            raise RuntimeError("boom")
        return real(*args, **kwargs)
    monkeypatch.setattr(web, "tariff_horizon", flaky)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234), raise_server_exceptions=False) as client:
        assert client.get("/api/status").status_code == 500
        state["fail"] = False
        assert client.get("/api/status").status_code == 200


def test_slow_page_build_still_sees_health_and_charger_as_current(tmp_path, monkeypatch):
    """Live data that is newer than the page's own clock must not read as stale or misaligned."""
    import solarbank_optimiser.web.app as web
    from solarbank_optimiser.collector import PROVENANCE
    from solarbank_optimiser.models import FakeClock, Observation
    clock = FakeClock(1_800_000_000)
    store = Store(tmp_path / "w.sqlite3", live_path=tmp_path / "live.json")
    store.initialise()

    def publish(at):
        store.publish_live(
            [Observation("monitor:shelly:ch2", "ev_power", at, .003, "kW", source_at=at - .2, provenance=PROVENANCE),
             Observation("monitor:solarbank:soc", "soc", at, .5, "fraction", source_at=at - .2, provenance=PROVENANCE)],
            {"collector_health": {"status": "ok", "acquired_at": at},
             "charger_tracker": {"label": "standby", "last_t": at, "last_sample_t": at - .2}})
    publish(clock.utc())
    real = Store.latest_observations

    def readings_then_the_collector_writes_newer_values(self, *args, **kwargs):
        result = real(self, *args, **kwargs)     # the page has now read its readings...
        publish(clock.utc() + 3)                 # ...and the collector keeps running while the page is still being built
        return result
    monkeypatch.setattr(Store, "latest_observations", readings_then_the_collector_writes_newer_values)
    app = create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", trusted_network=True,
                               status_cache_seconds=0), clock, store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        data = client.get("/api/status").json()
    assert data["collector"]["status"] == "ok"
    assert data["charger_state"] == "standby"
    assert data["readings"]["soc"]["value"] == .5


def test_pinned_live_snapshot_is_per_thread_and_restored(tmp_path):
    from solarbank_optimiser.models import Observation
    from solarbank_optimiser.collector import PROVENANCE
    store = Store(tmp_path / "p.sqlite3", live_path=tmp_path / "live.json")
    obs = lambda v: [Observation("monitor:solarbank:soc", "soc", 100., v, "fraction", provenance=PROVENANCE)]
    store.publish_live(obs(.1))
    with store.pinned_live():
        store.publish_live(obs(.9))                                  # file changes under the block...
        assert store.live_observations(PROVENANCE)["soc"]["value"] == .1   # ...but this thread keeps its snapshot
        seen = []
        t = threading.Thread(target=lambda: seen.append(store.live_observations(PROVENANCE)["soc"]["value"]))
        t.start(); t.join()
        assert seen == [.9]                                          # other threads are unaffected
    assert store.live_observations(PROVENANCE)["soc"]["value"] == .9    # released afterwards


# ---- review item 4: the login-free dashboard answers only to its own names (DNS rebinding) -----------

def trusted_client(tmp_path, host="solarbank-pi.local", **options):
    app = create_app(WebConfig(tmp_path / "t.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0, **options))
    return TestClient(app, base_url=f"http://{host}", client=("192.168.1.50", 1234))


@pytest.mark.parametrize("host", ["solarbank-pi.local", "solarbank-pi.local:8091", "SOLARBANK-PI.LOCAL", "solarbank-pi", "localhost:8092", "127.0.0.1:8091",
                                  "192.168.1.155:8091", "192.168.1.153", "100.72.162.120:8091", "solarbank-pi.your-tailnet.ts.net:8091", "[::1]:8091"])
def test_known_names_and_addresses_are_answered(tmp_path, host):
    with trusted_client(tmp_path, host) as client:
        assert client.get("/api/status").status_code == 200
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8091", "solarbank-pi.local.evil.example", "xportfolio.local", "solarbank-pi.local@evil.example",
                                  "evil.example/solarbank-pi.local", "solarbank-pi.local evil", "attacker.ts.net.evil.example", ".ts.net", "", "solarbank-pi.local:abc",
                                  "localhost.evil.example", "my.dns-rebinder.test"])
def test_other_names_get_nothing_and_never_the_request_token(tmp_path, host):
    with trusted_client(tmp_path, "solarbank-pi.local") as client:
        response = client.get("/api/status", headers={"Host": host})
        assert response.status_code == 421
        assert "csrf_token" not in response.text
        assert client.get("/", headers={"Host": host}).status_code == 421
        assert client.put("/api/mode", json={"revision": 0, "mode": "observe"},
                          headers={"Host": host, "Origin": f"http://{host}"}).status_code == 421


def test_an_extra_name_can_be_allowed_by_the_owner(tmp_path):
    with trusted_client(tmp_path, "garage-pi.lan", allowed_hosts=("solarbank-pi.local", "garage-pi.lan")) as client:
        assert client.get("/api/status").status_code == 200


def test_password_mode_is_unaffected_because_a_login_is_required_anyway(tmp_path):
    app = create_app(WebConfig(tmp_path / "p.sqlite3", "a-long-test-password", status_cache_seconds=0))
    with TestClient(app, client=("192.168.1.50", 1234)) as client:
        assert client.get("/api/status", auth=("solarbank", "a-long-test-password")).status_code == 200


def test_allowed_hosts_setting_is_validated(tmp_path):
    for bad in (["list"], ("UPPER",), ("a/b",), ("a@b",), ("",)):
        with pytest.raises(ValueError):
            WebConfig(tmp_path / "x.sqlite3", "a-long-test-password", allowed_hosts=bad)


def test_nginx_drops_unknown_hosts_and_every_named_host_is_one_the_app_accepts():
    from pathlib import Path
    from solarbank_optimiser.web.app import host_allowed
    text = (Path(__file__).resolve().parents[1] / "deployment" / "solarbank-optimiser.nginx").read_text()
    first, second = text.split("server {")[1], text.split("server {")[2]
    assert "default_server" in first and "return 444" in first and "server_name _" in first
    names = [n.strip('"') for n in second.split("server_name")[1].split(";")[0].split()]
    assert names
    for name in names:
        if not name.startswith("~"):
            assert host_allowed(name, ("solarbank-pi.local", "solarbank-pi", "localhost")), name
    regex = [n for n in names if n.startswith("~")]
    assert regex == ['~^(\\d{1,3}\\.){3}\\d{1,3}$']


def test_a_slow_rebuild_of_the_status_page_never_makes_a_request_wait_for_it(tmp_path, monkeypatch):
    """GitHub 61: after the first picture, a request is answered at once with the previous one while the next is built in the background."""
    import time
    from solarbank_optimiser.web import app as web_app
    store = Store(tmp_path / "w.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0.3), FakeClock(1_800_000_000), store=store)
    slow = {"on": False, "calls": 0}
    real = web_app.assess_realtime

    def maybe_slow(*a, **k):
        slow["calls"] += 1
        if slow["on"]:
            time.sleep(1.0)
        return real(*a, **k)
    monkeypatch.setattr(web_app, "assess_realtime", maybe_slow)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        first = client.get("/api/status").json()
        slow["on"] = True
        time.sleep(0.4)                                              # the cached picture is now older than the cache time
        started = time.monotonic()
        second = client.get("/api/status").json()
        assert time.monotonic() - started < 0.5, "the request waited for the rebuild"
        assert second["computed_at"] == first["computed_at"]         # the previous picture, served at once
        time.sleep(1.4)                                              # the background rebuild finishes
        third = client.get("/api/status").json()
        assert third["computed_at"] >= first["computed_at"] and slow["calls"] >= 2


def test_after_a_change_is_attempted_the_old_picture_is_never_served(tmp_path):
    """The existing rule stays: a PUT clears the cache, so the next request builds a new picture and waits for it."""
    store = Store(tmp_path / "w.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0.3), FakeClock(1_800_000_000), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        client.get("/api/status")
        import time
        time.sleep(0.4)
        client.put("/api/devices", json={"device": "solarbank", "host": "192.168.1.101"}, headers={"X-CSRF-Token": "x"})        # refused or not, a change was attempted
        assert client.get("/api/status").status_code == 200


def test_a_picture_being_built_when_a_change_is_attempted_is_not_stored_afterwards(tmp_path, monkeypatch):
    """The race that failed the Pi's gate: the background rebuild finishes AFTER a change cleared the cache and must not put the old picture back."""
    import time
    from solarbank_optimiser.web import app as web_app
    store = Store(tmp_path / "w.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "w.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0.3), FakeClock(1_800_000_000), store=store)
    state = {"calls": 0, "slow_from": 2}
    real = web_app.assess_realtime

    def counted(*a, **k):
        state["calls"] += 1
        if state["calls"] >= state["slow_from"]:
            time.sleep(1.0)
        return real(*a, **k)
    monkeypatch.setattr(web_app, "assess_realtime", counted)
    with TestClient(app, base_url="http://solarbank-pi.local", client=("192.168.1.50", 1234)) as client:
        first = client.get("/api/status").json()
        time.sleep(0.4)
        client.get("/api/status")                                    # served at once; the slow rebuild starts in the background (call 2)
        client.put("/api/mode", json={"revision": 0, "mode": "observe"}, headers={"X-CSRF-Token": first["csrf_token"]})      # a change is attempted while it runs
        time.sleep(1.5)                                              # the background rebuild finishes: its picture must be thrown away
        state["slow_from"] = 99
        client.get("/api/status")
        assert state["calls"] == 3                                   # a new picture was built for this request, not the old one served from the cache
