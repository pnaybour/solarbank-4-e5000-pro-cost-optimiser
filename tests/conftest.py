"""The automated suite must never connect to equipment or provider accounts.

Only loopback (the tests' own fake devices) is allowed; every other address is refused.
"""

import socket
from dataclasses import replace
import pytest

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _host(address):
    return address[0] if isinstance(address, tuple) and address else address


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect, real_connect_ex, real_create = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

    def guard(address):
        if isinstance(address, (str, bytes)):      # a Unix-domain socket path is local, not a network address
            return
        if _host(address) not in LOOPBACK:
            raise AssertionError("Network access is prohibited in offline tests")

    def connect(self, address):
        guard(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        guard(address)
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        guard(address)
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)


# ---- the controller's wall clock in tests ----------------------------------------------------------------------------------------------------
# Supervisor.decide() needs the wall clock (`utc`) to know whether it is a cheap period. Most tests exercise something else and call it without one;
# they get a clock that behaves as the real system does: a plan that charges is only ever issued inside the cheap window (01:00 London), any other plan by day
# (13:00 London). A test that is about the clock passes `utc=` itself.
CHEAP_UTC = 1_799_974_800.0       # 2027-01-15 01:00 UTC = 01:00 London (winter)
DAY_UTC = 1_800_000_000.0 + 5 * 3600.0     # 13:00 UTC


@pytest.fixture(autouse=True)
def default_controller_clock(monkeypatch):
    from solarbank_optimiser.control_logic import Supervisor
    real = Supervisor.decide

    def decide(self, inputs, plan, plan_note, now, control_ok=True, utc=None):
        if utc is None:
            charging = bool(plan) and plan.get("action") == "charge_to"
            utc = (CHEAP_UTC if charging else DAY_UTC) + now
        return real(self, inputs, plan, plan_note, now, control_ok, utc)
    monkeypatch.setattr(Supervisor, "decide", decide)


# ---- minute detail lives in the telemetry files (WP23b) ----------------------------------------------------------------------------------------
# Many tests seed one-minute rows through Store.add_observations. The readers now look in the telemetry files, so the suite mirrors those rows
# there (the same values, counts and maxima) and fixes the readers' idea of "now" three days after the newest seeded minute, so the data is
# read from the one-minute archive. The files are sparse in tests. Tests of the live (one-second) path write through Telemetry itself.
@pytest.fixture(autouse=True)
def sparse_telemetry(monkeypatch):
    from solarbank_optimiser.telemetry import Telemetry
    monkeypatch.setattr(Telemetry, "SPARSE", True)


@pytest.fixture(autouse=True)
def mirror_minute_rows(monkeypatch):
    from solarbank_optimiser.store import Store
    real = Store.add_observations

    def add(self, observations):
        changed = real(self, observations)
        rows = [o for o in observations if (o.window_mean is not None and o.window_ok) or (o.interval_start is not None and o.coverage_seconds and o.metric in ("household_power", "pv_power"))]
        # The planner's house demand is the house less the car, and a minute with no car reading is missing (GitHub issue 74). On the Pi the car channel is read
        # every second (idle is about 0 kW), so a planner-style household minute in a fixture comes with an idle car reading unless the test gives one itself.
        given = {(o.interval_start if o.interval_start is not None else o.sample_at) // 60 for o in observations if o.metric == "ev_power"}
        rows += [replace(o, metric="ev_power", value=0.0, source="fixture-idle-car") for o in rows
                 if o.metric == "household_power" and o.interval_start is not None and o.interval_start // 60 not in given]
        if rows:
            mirror_minutes(self, rows)
        return changed

    monkeypatch.setattr(Store, "add_observations", add)


def mirror_minutes(store, rows):
    import whisper
    from solarbank_optimiser.telemetry import METRICS, Telemetry, files_for
    Telemetry(store.telemetry_dir)
    newest = max(int(o.sample_at) // 60 * 60 for o in rows)
    now = max(newest + 3 * 86400, getattr(store, "_mirror_now", 0))
    store._mirror_now = now
    store.telemetry_clock = lambda: now
    points = {}
    for o in rows:
        if o.metric not in METRICS:
            continue
        minute = int(o.sample_at) // 60 * 60
        mean, ok, peak = (o.window_mean, o.window_ok, o.window_max) if o.window_mean is not None else (o.value, int(o.coverage_seconds), o.value)
        for name, _aggregation, _xff in files_for(o.metric):
            value = mean if name == f"{o.metric}.wsp" else float(min(ok, 60)) if name.endswith(".count.wsp") else peak
            if value is not None:
                points.setdefault(name, {})[minute] = value
    for name, series in points.items():
        whisper.update_many(str(store.telemetry_dir / name), sorted(series.items()), now=now)


def seed_minutes(store, metric, start, end, value, covered=60):
    """Put one telemetry minute per minute of [start, end) for `metric` (value in the metric's unit, `covered` valid seconds each)."""
    from solarbank_optimiser.models import UNITS, Observation
    first = int(start) // 60 * 60
    rows = [Observation("fixture", metric, t + 60, value, UNITS[metric], source_at=t + 59, window_samples=60, window_ok=covered, window_mean=value, window_max=value)
            for t in range(first, int(end), 60)]
    mirror_minutes(store, rows)


@pytest.fixture(autouse=True)
def authorisation_files_need_not_be_root_owned(monkeypatch):
    """The suite does not run as root everywhere; tests/test_live_gate.py tests the ownership rules themselves."""
    from solarbank_optimiser import trusted_files
    monkeypatch.setattr(trusted_files, "REQUIRE_ROOT", False)
