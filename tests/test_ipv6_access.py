"""The dashboard is limited to the home network, Tailscale and this machine, over IPv4 and IPv6."""
import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app


@pytest.mark.parametrize("peer,expected", [
    ("192.168.1.50", 200), ("100.72.1.2", 200), ("127.0.0.1", 200), ("::1", 200),
    ("2001:db8:abcd:a:0:0:0:10", 200), ("fe80::1", 200), ("fd12::5", 200), ("fd7a:115c:a1e0::fb3a:a278", 200),
    ("8.8.8.8", 403), ("192.168.2.5", 403), ("2001:db8:abcd:b::1", 403), ("2001:db8::1", 403),
])
def test_who_may_open_the_dashboard(tmp_path, peer, expected):
    (tmp_path / "lan").write_text("2001:db8:abcd:a::/64\n")
    store = Store(tmp_path / "a.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "a.sqlite3", "a-long-test-password", trusted_network=True, lan_prefixes_path=str(tmp_path / "lan")), FakeClock(1_793_000_000.0), store=store)
    with TestClient(app, base_url="http://solarbank-pi.local", client=(peer, 1234)) as client:
        assert client.get("/api/status").status_code == expected


def test_the_home_prefix_comes_from_the_one_configuration_file(tmp_path):
    from solarbank_optimiser.web.app import lan_prefixes
    path = tmp_path / "lan-prefixes"
    path.write_text("# comment\n2001:db8:1::/64   # the ISP prefix\nnot-a-network\n\n")
    assert lan_prefixes(path) == ["2001:db8:1::/64"] and lan_prefixes(tmp_path / "missing") == []
    store = Store(tmp_path / "a.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "a.sqlite3", "a-long-test-password", trusted_network=True, lan_prefixes_path=str(path)), FakeClock(1_793_000_000.0), store=store)
    for peer, expected in (("2001:db8:1::5", 200), ("2001:db8:abcd:a::5", 403)):
        with TestClient(app, base_url="http://solarbank-pi.local", client=(peer, 1234)) as client:
            assert client.get("/api/status").status_code == expected
