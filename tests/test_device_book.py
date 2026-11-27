"""GitHub issue 84: the device book written by root (`sudo solarbank devices confirm`) must stay readable and updatable by the service user; the write is atomic."""
import json
import multiprocessing
import os

import pytest

from solarbank_optimiser import device_book
from solarbank_optimiser.device_book import DeviceBook

IDENTITY = {"model": "AE103", "serial_digest": "abc"}


def as_root(monkeypatch):
    """Pretend to be root and record every chown (the real call needs root and the real service user)."""
    calls = []
    monkeypatch.setattr(device_book.os, "geteuid", lambda: 0)
    monkeypatch.setattr(device_book.os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))
    monkeypatch.setattr(device_book, "_service_ids", lambda: (4242, 4343))
    return calls


def test_root_gives_the_book_and_its_lock_to_the_service_user(tmp_path, monkeypatch):
    calls = as_root(monkeypatch)
    book = DeviceBook(tmp_path / "device-book.json")
    book.learn_identity("solarbank", IDENTITY)
    assert book.confirm_identity("solarbank") == IDENTITY
    assert calls and set(calls) == {(4242, 4343)}                              # the new JSON file and the lock file, every time
    assert len(calls) >= 4                                                    # at least: lock + file for the learn, lock + file for the confirm
    assert oct((tmp_path / "device-book.json").stat().st_mode & 0o777) == oct(0o640)     # the mode is unchanged: the review's 600 is not needed
    assert oct((tmp_path / "device-book.json.lock").stat().st_mode & 0o777) == oct(0o640)


def test_a_non_root_process_does_not_chown_and_still_works(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(device_book.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(device_book.os, "fchown", lambda *a: called.append(a))
    book = DeviceBook(tmp_path / "device-book.json")
    book.learn_identity("meter", IDENTITY)
    assert called == [] and book.identity("meter") == IDENTITY


def test_root_on_a_machine_without_the_service_user_still_works(tmp_path, monkeypatch):
    monkeypatch.setattr(device_book.os, "geteuid", lambda: 0)
    monkeypatch.setattr(device_book, "_service_ids", lambda: None)
    book = DeviceBook(tmp_path / "device-book.json")
    book.learn_identity("shelly", IDENTITY)
    assert book.identity("shelly") == IDENTITY


def test_a_lock_file_that_cannot_be_changed_does_not_stop_an_update(tmp_path, monkeypatch):
    """A service process that does not own an old root-owned lock file cannot chmod it: it must carry on (the deploy repairs ownership)."""
    def refuse(fd, mode):
        raise PermissionError("not the owner")
    monkeypatch.setattr(device_book.os, "fchmod", refuse)
    book = DeviceBook(tmp_path / "device-book.json")
    book.learn_identity("solarbank", IDENTITY)
    assert book.identity("solarbank") == IDENTITY


def test_an_interrupted_write_leaves_the_old_book_whole(tmp_path, monkeypatch):
    book = DeviceBook(tmp_path / "device-book.json")
    book.learn_identity("solarbank", IDENTITY)
    before = (tmp_path / "device-book.json").read_text()

    def die(fd):
        raise OSError("disk full")
    monkeypatch.setattr(device_book.os, "fsync", die)
    with pytest.raises(OSError):
        book.learn_identity("meter", IDENTITY)
    monkeypatch.undo()
    assert (tmp_path / "device-book.json").read_text() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]      # no half-written file left behind


def test_data_that_cannot_be_written_replaces_nothing(tmp_path):
    book = DeviceBook(tmp_path / "device-book.json")
    book.learn_identity("solarbank", IDENTITY)
    before = (tmp_path / "device-book.json").read_text()
    with pytest.raises(TypeError):
        book.update(lambda data: data["identities"].update(bad=object()))
    assert (tmp_path / "device-book.json").read_text() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def _learn(path, name):
    DeviceBook(path).learn_identity(name, {"model": name})


def test_updates_from_several_processes_are_not_lost(tmp_path):
    path = tmp_path / "device-book.json"
    names = [f"device{i}" for i in range(12)]
    with multiprocessing.get_context("fork").Pool(4) as pool:
        pool.starmap(_learn, [(path, n) for n in names])
    assert set(DeviceBook(path).read()["identities"]) == set(names)


def test_identities_and_confirmations_survive_a_restart(tmp_path):
    book = DeviceBook(tmp_path / "device-book.json")
    book.learn_identity("solarbank", IDENTITY)
    book.confirm_identity("solarbank")
    again = DeviceBook(tmp_path / "device-book.json")                           # a new process, a new object
    assert again.identity("solarbank") == IDENTITY and again.is_confirmed("solarbank")
    assert json.loads((tmp_path / "device-book.json").read_text())["confirmed"]["solarbank"] == IDENTITY
