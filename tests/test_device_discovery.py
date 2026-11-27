"""WP21 / GitHub issues 14 and 15: identity on every reconnect, finding a moved device, owner address requests, and following the shared book."""
import threading
import time

import pytest

from solarbank_optimiser.device_book import DeviceBook
from solarbank_optimiser.device_discovery import DiscoveryThread, IdentityGuard, Rediscovery, neighbours, subnet_hosts
from solarbank_optimiser.devices.errors import DeviceError

A = {"product": "Solarbank 4", "serial_digest": "aaa"}
B = {"product": "Solarbank 4", "serial_digest": "bbb"}


@pytest.fixture
def book(tmp_path):
    return DeviceBook(tmp_path / "device-book.json")


def guard(book, answers, reader=lambda: {"soc": 50}, host="10.0.0.5"):
    calls = []
    def probe(name, h):
        calls.append(h)
        if isinstance(answers[h], Exception):
            raise answers[h]
        return answers[h]
    clock = [0.0]
    g = IdentityGuard("solarbank", reader, probe, book, lambda n: host, clock=lambda: clock[0])
    return g, calls, clock


def test_the_first_read_learns_the_identity_and_a_different_device_is_never_data(book):
    answers = {"10.0.0.5": A}
    g, calls, _ = guard(book, answers)
    assert g() == {"soc": 50} and book.identity("solarbank") == A
    answers["10.0.0.5"] = B                                                # another unit now answers at that address
    g._failed = True                                                       # after any failure the device is checked again
    with pytest.raises(DeviceError, match="not the device"):
        g()
    assert g.health()["identity_mismatch"] is True


def test_identity_is_rechecked_after_a_failure_and_periodically_not_on_every_read(book):
    answers = {"10.0.0.5": A}
    g, calls, clock = guard(book, answers)
    g(); g(); g()
    assert len(calls) == 1                                                 # one check, then plain reads
    clock[0] += 700
    g()
    assert len(calls) == 2                                                 # the periodic recheck
    broken = {"n": 0}
    def flaky():
        broken["n"] += 1
        if broken["n"] == 1:
            raise DeviceError("timeout", "no answer")
        return {"soc": 1}
    g2, calls2, _ = guard(book, answers, reader=flaky)
    with pytest.raises(DeviceError):
        g2()
    g2()
    assert len(calls2) == 2                                                # a failure forces a fresh identity check on the next attempt


def test_the_book_prefers_a_found_address_only_while_the_configured_one_is_unchanged(book):
    book.record_found("shelly", "192.168.1.200", "192.168.1.103", 1.0)
    assert book.effective_host("shelly", "192.168.1.103") == "192.168.1.200"
    assert book.effective_host("shelly", "192.168.1.99") == "192.168.1.99"      # the owner edited devices.conf afterwards: obeyed


def test_a_moved_device_is_found_by_the_neighbour_table_then_the_scan_and_only_if_its_identity_matches(book):
    book.learn_identity("solarbank", A)
    answers = {"192.168.1.170": A, "192.168.1.171": B}
    d = Rediscovery(book, probe_fn=lambda n, h: answers[h], neighbours_fn=lambda: ["192.168.1.171"], open_fn=lambda h, p: h in answers)
    assert d.find("solarbank", "192.168.1.101", force=True) == "192.168.1.170"      # the neighbour was the wrong device; the scan found the right one
    assert d.find("solarbank", "192.168.1.101") is None                               # not again within the scan interval
    book2 = DeviceBook(book.path.with_name("other.json"))
    assert Rediscovery(book2, probe_fn=lambda n, h: A, neighbours_fn=lambda: [], open_fn=lambda h, p: True).find("solarbank", "192.168.1.101", force=True) is None   # nothing commissioned: nothing to recognise


def test_the_scan_stays_inside_the_24_and_skips_the_configured_address():
    hosts = subnet_hosts("192.168.1.101")
    assert len(hosts) == 253 and "192.168.1.101" not in hosts and all(h.startswith("192.168.1.") for h in hosts)
    assert subnet_hosts("solarbank-pi.local") == [] and subnet_hosts("::1") == []


def test_the_neighbour_table_is_read_for_ipv4_hosts_that_have_been_seen():
    out = "192.168.1.5 dev eth0 lladdr aa:bb REACHABLE\n192.168.1.9 dev eth0  FAILED\n192.168.1.7 dev eth0 lladdr cc STALE\n"
    class R:  stdout = out
    assert neighbours(lambda *a, **k: R) == ["192.168.1.5", "192.168.1.7"]


def make_thread(book, health, moves, alerts, found=None, search=True):
    class D:
        probe_fn = staticmethod(lambda n, h: A)
        def find(self, name, configured, force=False):
            return found
    clock = [0.0]
    t = DiscoveryThread(book, {"solarbank": "192.168.1.101"}, lambda: health, lambda n, h: moves.append((n, h)), threading.Event(),
                        alert=lambda kind, n, h: alerts.append((kind, n, h)), discoverer=D(), wall=lambda: 5.0, clock=lambda: clock[0], search=search)
    return t, clock


def test_an_unreachable_device_is_searched_for_after_a_minute_recorded_and_alerted(book):
    book.learn_identity("solarbank", A)
    moves, alerts = [], []
    health = {"solarbank": {"fail_streak": 5}}
    t, clock = make_thread(book, health, moves, alerts, found="192.168.1.170")
    t.step()
    assert moves == [("solarbank", "192.168.1.101")] and alerts == []        # just went down: not yet
    clock[0] = 61
    t.step()
    assert ("solarbank", "192.168.1.170") in moves and ("moved", "solarbank", "192.168.1.170") in alerts
    assert book.effective_host("solarbank", "192.168.1.101") == "192.168.1.170"
    health["solarbank"] = {"fail_streak": 0}
    clock[0] = 61 + 10
    t.step()                                                                  # healthy again: the clear waits for ten quiet minutes
    assert alerts[-1][0] == "moved"
    clock[0] = 61 + 10 + 700
    t.step()
    assert alerts[-1] == ("clear", "solarbank", None)


def test_a_device_that_cannot_be_found_raises_a_missing_alert_and_the_collector_only_follows(book):
    book.learn_identity("solarbank", A)
    moves, alerts = [], []
    t, clock = make_thread(book, {"solarbank": {"fail_streak": 9}}, moves, alerts, found=None)
    t.step(); clock[0] = 61; t.step(); clock[0] = 200; t.step()
    assert ("missing", "solarbank", None) in alerts
    book.record_found("solarbank", "192.168.1.180", "192.168.1.101", 1.0)
    follower, _ = make_thread(book, {"solarbank": {"fail_streak": 9}}, (m2 := []), (a2 := []), search=False)
    follower.step()
    assert m2 == [("solarbank", "192.168.1.180")] and a2 == []               # follows the book, never searches or alerts


def test_an_address_the_owner_asks_for_is_adopted_only_when_the_identity_matches(book):
    book.learn_identity("solarbank", A)
    moves, alerts = [], []
    t, _ = make_thread(book, {}, moves, alerts)
    book.request_address("solarbank", "192.168.1.50", 1.0)
    t.step()
    assert moves[-1] == ("solarbank", "192.168.1.50") and book.read()["requests"]["solarbank"]["status"] == "adopted"
    t.discoverer.probe_fn = lambda n, h: B                                    # now the device there is a different one
    book.request_address("solarbank", "192.168.1.51", 2.0)
    t.step()
    request = book.read()["requests"]["solarbank"]
    assert request["status"] == "rejected" and "identity differs" in request["reason"]
    with pytest.raises(ValueError):
        book.request_address("solarbank", "bad host;rm", 3.0)


def test_the_acquirer_moves_its_readers_to_the_new_address(tmp_path):
    from solarbank_optimiser.acquirer import DirectAcquirer, DirectConfig
    calls = []
    class Client:
        def __init__(self, host, *a, **k): self.host = host
        def set_host(self, h): calls.append(h)
        def close(self): pass
    cfg = DirectConfig(devices=("shelly",), shelly_host="192.168.1.103", device_book=str(tmp_path / "b.json"))
    acquirer = DirectAcquirer(cfg, readers={"shelly": lambda: {}})
    acquirer.move("shelly", "192.168.1.200")
    assert acquirer.hosts["shelly"] == "192.168.1.200"
    acquirer.move("shelly", "192.168.1.200")
    DeviceBook(tmp_path / "b.json").record_found("shelly", "192.168.1.201", "192.168.1.103", 1.0)
    assert DirectAcquirer(cfg, readers={"shelly": lambda: {}}).hosts["shelly"] == "192.168.1.201"          # a restart starts from where the book says it is


def test_the_devices_command_shows_state_and_accept_forgets_one_identity(tmp_path, capsys, monkeypatch):
    from solarbank_optimiser.cli import main
    monkeypatch.setenv("SOLARBANK_DEVICES_FILE", str(tmp_path / "devices.conf"))
    (tmp_path / "devices.conf").write_text("SOLARBANK_HOST=192.168.1.101\n")
    book = DeviceBook(tmp_path / "device-book.json")
    book.learn_identity("solarbank", A)
    book.record_found("solarbank", "192.168.1.170", "192.168.1.101", 1.0)
    main(["--database", str(tmp_path / "d.sqlite3"), "devices", "show"])
    out = capsys.readouterr().out
    assert "recognised" in out and "192.168.1.170" in out and "moved" in out
    main(["--database", str(tmp_path / "d.sqlite3"), "devices", "accept", "solarbank"])
    assert book.identity("solarbank") is None


# ---- GitHub issue 37: identity is read on the reader's own connection ---------------------------------------------------------------------------------
class OneClientModbus:
    """A fake Modbus device that allows ONE client at a time and answers identity registers; counts every connection object made."""
    made = []

    def __init__(self, host, port=502, unit=1, timeout=2.0, **kwargs):
        self.host = host
        OneClientModbus.made.append(self)
        self.closed = False

    def read_input_registers(self, address, count):
        if sum(1 for c in OneClientModbus.made if not c.closed) > 1:
            raise DeviceError("connect", "the meter allows one client")
        text = {10620: "ModelX", 10702: "SERIAL-1"}.get(address, "")
        words = [(ord(a) << 8) | ord(b) for a, b in zip(text.ljust(count * 2, "\0")[0::2], text.ljust(count * 2, "\0")[1::2])]
        return words[:count]

    def read_holding_registers(self, address, count):
        return [0] * count

    def close(self):
        self.closed = True

    def set_host(self, host):
        self.host = host


def test_the_identity_check_uses_the_readers_own_connection_and_never_a_second_one(tmp_path, monkeypatch):
    from solarbank_optimiser import acquirer as acq
    OneClientModbus.made.clear()
    monkeypatch.setattr(acq, "ReadOnlyModbusTcp", OneClientModbus)
    monkeypatch.setattr(acq.meter, "read", lambda client: client.read_input_registers(100, 1) or {"main": 1.0})
    cfg = acq.DirectConfig(devices=("meter",), meter_host="192.168.1.20", device_book=str(tmp_path / "b.json"))
    acquirer = acq.DirectAcquirer(cfg)
    reader = acquirer._readers["meter"]
    clock = [0.0]
    reader._clock = lambda: clock[0]
    for step in range(5):
        reader()
        clock[0] += 700                                              # past the periodic re-check every time
    assert len(OneClientModbus.made) == 1                            # one connection object for the reads and the identity checks together
    assert DeviceBook(tmp_path / "b.json").identity("meter") is not None
    acquirer.stop()


def test_an_owner_request_for_the_address_already_in_use_does_not_probe_the_device(book):
    book.learn_identity("solarbank", A)
    probed = []
    class D:
        probe_fn = staticmethod(lambda n, h: probed.append(h) or A)
    t = DiscoveryThread(book, {"solarbank": "192.168.1.101"}, lambda: {}, lambda n, h: None, threading.Event(), discoverer=D(), search=True)
    book.request_address("solarbank", "192.168.1.101", 1.0)
    t.step()
    assert probed == [] and book.read()["requests"]["solarbank"]["status"] == "adopted"


def _hammer(path, name, n):
    from solarbank_optimiser.device_book import DeviceBook
    book = DeviceBook(path)
    for i in range(n):
        book.update(lambda data, i=i: data["addresses"].__setitem__(f"{name}{i}", {"host": "192.168.1.2", "superseded": "x", "at": i}))


def test_two_processes_updating_the_book_lose_nothing(tmp_path):
    import multiprocessing
    from solarbank_optimiser.device_book import DeviceBook
    path = tmp_path / "device-book.json"
    workers = [multiprocessing.Process(target=_hammer, args=(path, name, 200)) for name in ("a", "b")]
    for w in workers:
        w.start()
    for w in workers:
        w.join(120)
        assert w.exitcode == 0
    assert len(DeviceBook(path).read()["addresses"]) == 400


def test_a_write_target_counts_as_verified_only_shortly_after_a_matching_check_and_not_after_a_move(book):
    answers = {"10.0.0.5": A}
    g, _calls, clock = guard(book, answers)
    assert g.verified_within(900) is False                  # nothing has been checked yet
    g()
    assert g.verified_within(900) is True
    clock[0] += 1000
    assert g.verified_within(900) is False                  # too long ago
    clock[0] = 0
    g.invalidate()                                          # the device moved: the next write waits for a fresh check
    assert g.verified_within(900) is False
    g()
    assert g.verified_within(900) is True


def test_the_writer_follows_the_devices_current_address_and_a_move_drops_the_old_connection():
    from solarbank_optimiser.devices.modbus_write import GuardedModbusWriter

    made = []

    class Client:
        def __init__(self, host, **kw):
            self.host, self.closed = host, False
            made.append(self)

        def connect(self):
            return True

        def write_register(self, address, value, device_id=1):
            return type("R", (), {"isError": lambda self: False, "address": address})()

        def close(self):
            self.closed = True

    host = ["10.0.0.5"]
    writer = GuardedModbusWriter(lambda: host[0], client_factory=Client)
    writer.write_mode(0)
    host[0] = "10.0.0.9"
    writer.write_mode(0)
    assert [c.host for c in made] == ["10.0.0.5", "10.0.0.9"] and made[0].closed and not made[1].closed


def test_the_transport_sends_nothing_until_the_solarbank_is_recognised(tmp_path):
    from solarbank_optimiser.action_machine import MODE_REGISTER, Write
    from solarbank_optimiser.live_transport import LiveTransport

    class Machine:
        state = "running"
        alerts = []

        def forget(self, *a):
            pass

    sent = []
    writer = type("W", (), {"write_mode": lambda self, v: sent.append(v)})()
    journal = type("J", (), {"records": [], "record": lambda self, e: self.records.append(e)})()
    ok = [False]
    transport = LiveTransport(Machine(), journal, writer=writer, send=True, target_ok=lambda: ok[0])
    assert transport._perform(1.0, [Write(MODE_REGISTER, 0, "test")]) == [] and sent == []
    assert "not been recognised" in journal.records[-1]["why_not_sent"]


def test_a_write_waits_for_the_owner_to_confirm_what_was_learned(book):
    from solarbank_optimiser.acquirer import DirectAcquirer, DirectConfig
    answers = {"10.0.0.5": A}
    g, _calls, _clock = guard(book, answers)
    g()                                                                      # learned at the first read
    assert book.identity("solarbank") == A and not book.is_confirmed("solarbank")
    acquirer = DirectAcquirer.__new__(DirectAcquirer)
    acquirer.book, acquirer._readers = book, {"solarbank": g}
    assert acquirer.verified("solarbank", 900.0) is False                    # recognised, but never confirmed by the owner
    assert book.confirm_identity("solarbank") == A and acquirer.verified("solarbank", 900.0) is True
    book.forget_identity("solarbank")                                        # replacing the device on purpose starts again
    assert not book.is_confirmed("solarbank") and book.confirm_identity("solarbank") is None


def test_a_write_goes_to_the_address_that_was_verified_and_to_no_other(book):
    """GitHub 50: verification is bound to the address it was made at. If the device moves between the check and the write, the write waits."""
    from solarbank_optimiser.acquirer import DirectAcquirer
    from solarbank_optimiser.device_discovery import IdentityGuard
    answers = {"10.0.0.5": A, "10.0.0.9": A}
    host = ["10.0.0.5"]
    clock = [0.0]
    guard = IdentityGuard("solarbank", lambda: {"soc": 50}, lambda n, h: answers[h], book, lambda n: host[0], clock=lambda: clock[0])
    guard()
    book.confirm_identity("solarbank")
    acquirer = DirectAcquirer.__new__(DirectAcquirer)
    acquirer.book, acquirer._readers, acquirer.hosts = book, {"solarbank": guard}, {"solarbank": "10.0.0.5"}
    assert acquirer.verified_host("solarbank", 660.0) == "10.0.0.5"
    host[0] = "10.0.0.9"; acquirer.hosts["solarbank"] = "10.0.0.9"            # rediscovery moves the device; nothing has been checked at the new address
    assert acquirer.verified_host("solarbank", 660.0) is None
    guard.invalidate(); guard()                                               # the next read verifies it at the new address
    assert acquirer.verified_host("solarbank", 660.0) == "10.0.0.9"


def test_the_transport_hands_the_verified_address_to_the_writer_which_uses_it_and_not_its_own_lookup(tmp_path):
    from solarbank_optimiser.action_machine import MODE_REGISTER, Write
    from solarbank_optimiser.devices.modbus_write import GuardedModbusWriter
    from solarbank_optimiser.live_transport import LiveTransport
    made = []

    class Client:
        def __init__(self, host, **kw):
            self.host = host
            made.append(host)

        def connect(self):
            return True

        def write_register(self, address, value, device_id=1):
            return type("R", (), {"isError": lambda self: False, "address": address})()

        def close(self):
            pass

    writer = GuardedModbusWriter(lambda: "10.0.0.99", client_factory=Client)          # its own lookup would say .99: it must not be used
    switch = tmp_path / "sw"; switch.write_text("mode\nsetpoint\n")

    class Machine:
        state = "running"
        alerts = []

        def sent(self, w):
            pass

        def forget(self, *a):
            pass

    journal = type("J", (), {"record": lambda self, e: None})()
    transport = LiveTransport(Machine(), journal, writer=writer, switch_path=str(switch), send=True, target_ok=lambda: "10.0.0.5")
    transport._perform(1.0, [Write(MODE_REGISTER, 0, "test")])
    assert made == ["10.0.0.5"]
