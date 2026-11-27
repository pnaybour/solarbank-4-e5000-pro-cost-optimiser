"""The commissioning tests (RC4), run by the owner on the real device with `solarbank commission run <test>`.

Each test is small, says what it does before it does it, leaves the device in the default mode (Dynamic Tariff, 6) with a 0 W set-point, and records a pass
for the current controller code only when every check held. Reads go through the read-only client; writes only through GuardedModbusWriter.
`hands-off-latch` needs no device. `setpoint-both-signs` is T2, the sign test (GitHub issue 75): it takes the battery for about eight minutes and checks
every set-point's direction on the Shelly. `supervised-run` and the other manual tests are the owner's own sign-offs: `commission pass <test> --note \"what I saw\"`.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .action_machine import ActionMachine, MODE_DEFAULT, MODE_REGISTER, SETPOINT_REGISTER

T1_BITS = (0, 5)            # specification T1: "Read 32774. Bits 0 and 5 set" (self-consumption and third-party control), whatever else is enabled


class TestFailed(RuntimeError):
    pass


def _need(condition, message):
    if not condition:
        raise TestFailed(message)


def _mode(read_settings, client):
    decoded = read_settings(client)["decoded"]
    return (decoded.get("operating_mode") or {}).get("code"), (decoded.get("ems_mode_mask") or {}).get("raw")


def read_registers(read_settings, client, writer, sleep):
    mode, mask = _mode(read_settings, client)
    _need(isinstance(mode, int) and not isinstance(mode, bool), f"the operating mode register read {mode!r}")
    _need(ActionMachine.mask_ok(mask), f"the mode mask read {mask!r}; specification T1 needs bits 0 and 5 set (self-consumption and third-party control)")
    return f"mode {mode}, mask {mask} (T1 bits 0 and 5 set)"


def mode_write(read_settings, client, writer, sleep):
    found, _ = _mode(read_settings, client)
    _need(isinstance(found, int) and not isinstance(found, bool), f"cannot read the mode ({found!r})")
    mode = found if found in (MODE_DEFAULT, 3) else MODE_DEFAULT     # the same value where the writer may write it; otherwise the hand-back mode (#78)
    writer.write_mode(mode)
    sleep(2.0)
    after, _ = _mode(read_settings, client)
    _need(after == mode, f"wrote mode {mode} and read back {after!r}")
    return f"rewrote mode {mode}, read back {after}"


def default_mode(read_settings, client, writer, sleep):
    writer.write_setpoint(0)
    sleep(2.0)
    writer.write_mode(MODE_DEFAULT)
    sleep(2.0)
    after, _ = _mode(read_settings, client)
    _need(after == MODE_DEFAULT, f"wrote the default mode {MODE_DEFAULT} and read back {after!r}: this device does not accept it")
    return f"default mode {MODE_DEFAULT} accepted"


def setpoint_zero_write(read_settings, client, writer, sleep):
    """Only the 0 W write. Non-zero set-points of each sign are `setpoint-both-signs`, T2 (below)."""
    writer.write_setpoint(0)
    return "set-point 0 W accepted"


def release(read_settings, client, writer, sleep):
    machine = ActionMachine(default_proven=True, verify=False)
    writes = machine.exit_writes()
    _need([w.register for w in writes] == [SETPOINT_REGISTER, MODE_REGISTER], "the exit does not write the set-point first and the mode second")
    for w in writes:
        (writer.write_mode if w.register == MODE_REGISTER else writer.write_setpoint)(w.value)
        sleep(2.0)
    after, _ = _mode(read_settings, client)
    _need(after == MODE_DEFAULT, f"after the release the device reads mode {after!r}, expected {MODE_DEFAULT}")
    return "release wrote 0 W then the default mode and the device followed"


def hands_off_latch(read_settings, client, writer, sleep):
    with tempfile.TemporaryDirectory() as directory:
        latch = Path(directory) / "latch.json"
        first = ActionMachine(latch_path=latch, now=0.0)
        first._enter_hands_off(1.0, "commissioning test")
        second = ActionMachine(latch_path=latch, now=2.0)
        _need(second.state == "hands_off", f"a restarted machine started in '{second.state}', not hands_off")
        _need(second.exit_writes() == [], "a latched machine still wants to write on exit")
    return "the HANDS_OFF latch survives a restart"


# ---- T2, the sign test (GitHub issue 75; specification section 13) ---------------------------------------------------------------------------------
# "Mode 3. At least 20 alternating and repeated changes, including -500, +300, -600 then -300, and -200 after a positive value. Hold each for 20 s.
# Every negative value charges and every positive value discharges, measured by the Shelly." Negative is charging, as everywhere in this code.
SIGN_SEQUENCE = (-500, 300, -600, -300, 300, -200, -500, 200, -300, 300, -600, -300, 200, -200, 300, -500, -300, 200, -200, -500)
SIGN_HOLD_S = 20.0           # each set-point is held this long
SIGN_SETTLE_S = 8.0          # the Shelly is judged on the readings after this (the device's response time is T5's question, not this one)
SIGN_MIN_W = 100.0           # the direction counts once the Shelly sees at least this much going in or coming out
SIGN_MAX_W = 600             # nothing larger is ever written by this test
SIGN_SOC = (0.20, 0.90)      # room to go both ways
SIGN_GARAGE_A = 40.0         # stop well before the 45 A feed limit
SIGN_HOUSE_KW = 0.4          # export is off: a +300 W discharge needs a house load to go into
LIVE_FILE = Path("/run/solarbank-cost-optimiser/live.json")
LIVE_MAX_AGE_S = 5.0
CAR_ON_KW = 1.0              # ev_policy.EV_ON_KW: the battery never discharges into a charging car


def live_readings(path=LIVE_FILE, now=None):
    """{metric: value} for the fresh, good readings in the collector's live file (powers in kW, the battery level a fraction). Reading the file opens no
    connection to any device: the Shelly and the meter keep their one client each, the collector."""
    import json
    now = time.time() if now is None else now
    try:
        document = json.loads(Path(path).read_text())
        rows = document["observations"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out = {}
    for o in rows:
        try:
            if o.get("quality", "ok") == "ok" and isinstance(o["value"], (int, float)) and now - float(o["retrieved_at"]) <= LIVE_MAX_AGE_S:
                out[o["metric"]] = float(o["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


CONTROL_STATE = Path("/run/solarbank-cost-optimiser/control-loop.json")
CONTROL_STATE_MAX_AGE_S = 30.0


def _controller_may_write(state_path=CONTROL_STATE, now=None):
    """The reason T2 must not run now, or None. The test must be the only writer, but a controller in logged or shadow mode never writes, and it keeps
    the smart meter's one connection (the garage current) alive, which T2 needs: so T2 runs beside it (found on the Pi, 11 November: with the controller
    stopped nobody read the meter and the garage current went stale at step 1). Live, or a status that cannot be read or is old, refuses."""
    import json
    import subprocess
    try:
        active = subprocess.run(["systemctl", "is-active", "solarbank-optimiser-control"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "cannot tell whether the controller is running"
    if active != "active":
        return None
    try:
        state = json.loads(Path(state_path).read_text())
        execution = state["execution"]
        age = (time.time() if now is None else now) - float(state["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return "the controller is running and its status cannot be read"
    if age > CONTROL_STATE_MAX_AGE_S:
        return f"the controller is running and its status is {age:.0f} s old"
    if execution.get("sends_commands") or execution.get("mode") not in ("logged", "shadow"):
        return f"the controller is running in {execution.get('mode')!r} mode and may write to the battery: put it back to logged first"
    return None


def _sign_safe(now_readings):
    """The reason to stop now, or None. Missing readings are a reason: missing is never 'car off' (D15)."""
    for metric in ("ac_power", "ev_power", "garage_current", "soc"):
        if metric not in now_readings:
            return f"the {metric} reading is missing or older than {LIVE_MAX_AGE_S:g} s (is the collector running?)"
    if now_readings["ev_power"] >= CAR_ON_KW:
        return f"the car is charging ({now_readings['ev_power']:.1f} kW): the battery must not discharge into it"
    if now_readings["garage_current"] >= SIGN_GARAGE_A:
        return f"the garage current is {now_readings['garage_current']:.1f} A, at or above {SIGN_GARAGE_A:g} A"
    return None


def setpoint_both_signs(read_settings, client, writer, sleep, live=None, controller_may_write=None, clock=time.monotonic, say=print):
    """T2. Takes the battery for about eight minutes: 0 W, mode 3, then SIGN_SEQUENCE, each held SIGN_HOLD_S and judged on the Shelly's Solarbank channel.
    Hands the battery back whatever happens (0 W, then the mode it was in before; mode 6 if that was 3). Passes only if every step had the right sign."""
    live = live or live_readings
    controller_may_write = controller_may_write or _controller_may_write
    assert all(abs(w) <= SIGN_MAX_W for w in SIGN_SEQUENCE)
    conflict = controller_may_write()
    if conflict:
        raise TestFailed(conflict + "; nothing was written")
    start = live()
    problem = _sign_safe(start)
    if problem is None and not SIGN_SOC[0] <= start["soc"] <= SIGN_SOC[1]:
        problem = f"the battery is at {start['soc']:.0%}; the test needs {SIGN_SOC[0]:.0%} to {SIGN_SOC[1]:.0%} so it can go both ways"
    if problem is None and start.get("household_power", 0.0) < SIGN_HOUSE_KW:
        problem = (f"the house is using {start.get('household_power', 0.0) * 1000:.0f} W; a +300 W discharge needs at least {SIGN_HOUSE_KW * 1000:.0f} W "
                   "to go into (export is off): switch on a heater or similar")
    if problem:
        raise TestFailed(problem + "; nothing was written")
    before, _ = _mode(read_settings, client)
    _need(isinstance(before, int) and not isinstance(before, bool), f"cannot read the mode ({before!r}); nothing was written")
    back_to = MODE_DEFAULT                 # always the hand-back mode (GitHub issue 78), whatever the battery was in
    say(f"T2: taking the battery (it was in mode {before}); {len(SIGN_SEQUENCE)} steps of {SIGN_HOLD_S:g} s, about "
        f"{len(SIGN_SEQUENCE) * SIGN_HOLD_S / 60 + 0.5:.0f} minutes. Ctrl-C stops it and hands the battery back.")
    rows, failure = [], None
    try:
        writer.write_setpoint(0)
        sleep(2.0)
        writer.write_mode(3)
        sleep(2.0)
        mode, _ = _mode(read_settings, client)
        _need(mode == 3, f"wrote mode 3 and read back {mode!r}")
        for step, watts in enumerate(SIGN_SEQUENCE, 1):
            writer.write_setpoint(watts)
            began, seen = clock(), []
            while clock() - began < SIGN_HOLD_S:
                sleep(1.0)
                now = live()
                problem = _sign_safe(now)
                if problem:
                    raise TestFailed(f"stopped at step {step} ({watts:+d} W): {problem}")
                if clock() - began >= SIGN_SETTLE_S:
                    seen.append(now["ac_power"] * 1000.0)          # Shelly, Solarbank channel: negative is going in (charging), positive coming out
            measured = sum(seen) / len(seen) if seen else float("nan")
            ok = measured <= -SIGN_MIN_W if watts < 0 else measured >= SIGN_MIN_W
            rows.append((watts, measured, ok))
            say(f"  step {step:2d}: set {watts:+5d} W   Shelly {measured:+7.0f} W   {'right' if ok else 'WRONG'}")
            if not ok:
                raise TestFailed(f"step {step}: set {watts:+d} W but the Shelly showed {measured:+.0f} W "
                                 f"({'expected charging' if watts < 0 else 'expected discharging'}, at least {SIGN_MIN_W:g} W)")
    except KeyboardInterrupt:
        failure = TestFailed(f"stopped by you after {len(rows)} step(s)")
    except TestFailed as problem:
        failure = problem
    finally:
        def pause(seconds):                               # a second Ctrl-C must never cut the hand-back short
            try:
                sleep(seconds)
            except KeyboardInterrupt:
                pass
        try:
            writer.write_setpoint(0)
            pause(2.0)
            writer.write_mode(back_to)
            pause(2.0)
            say(f"T2: battery handed back (0 W, then mode {back_to}).")
        except Exception as error:                        # the one thing that must be said plainly
            raise TestFailed(f"THE HAND-BACK FAILED ({error}): switch off third-party control in the Anker app now") from error
    if failure:
        raise failure
    charge = [m for w, m, _ in rows if w < 0]
    discharge = [m for w, m, _ in rows if w > 0]
    return (f"{len(rows)} of {len(SIGN_SEQUENCE)} steps right on the Shelly: charging {min(charge):+.0f} to {max(charge):+.0f} W, "
            f"discharging {min(discharge):+.0f} to {max(discharge):+.0f} W; handed back to mode {back_to}")


OPTIMISER_SERVICES = ("control", "collector", "shadow", "providers", "web")


def services_running(run=None):
    """The optimiser services that are not stopped (any answer other than 'inactive' or 'failed' counts as running, so nothing is assumed)."""
    import subprocess
    run = run or (lambda unit: subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10).stdout.strip())
    out = []
    for name in OPTIMISER_SERVICES:
        try:
            state = run(f"solarbank-optimiser-{name}")
        except Exception:
            state = "unknown"
        if state not in ("inactive", "failed"):
            out.append(name)
    return out


def direct_readings(read_solarbank, read_meter, read_shelly):
    """live() for T2 with every optimiser service stopped: the readings straight from the devices, one connection each, in the live file's units
    (powers in kW, the battery level a fraction). A device that fails to answer leaves its readings out, so T2 stops as for a stale reading."""
    def live():
        out = {}
        try:
            sb = read_solarbank()
            out["soc"], out["household_power"] = sb["soc"] / 100.0, sb["load"] / 1000.0
        except Exception:
            pass
        try:
            out["garage_current"] = float(read_meter()["garage_current_a"])
        except Exception:
            pass
        try:
            channels = read_shelly()
            out["ac_power"], out["ev_power"] = channels["solarbank_ac"] / 1000.0, max(channels["car"], 0.0) / 1000.0
        except Exception:
            pass
        return out
    return live


DYNAMIC_MODE, DYNAMIC_MASK_BIT = 7, 6   # the vendor's dynamic pricing ("Dynamic Tariff" in the Anker app) and its bit in the mode mask (anker_vendor.MODE_CAPABILITY_BIT)
DYNAMIC_HOLD_S = 180.0                  # how long the battery must stay in the mode (on 11 November smart mode, 6, was accepted and then fell back to self-consumption)
DYNAMIC_STEP_S = 10.0


def dynamic_mode(read_settings, client, writer, sleep, controller_may_write=None, live=None, say=print):
    """Can the battery be switched to the Anker's dynamic pricing mode (7) and back over Modbus? (owner, 15 November 2026: a possible fall-back for when the Pi fails.)
    An exploratory test: it is not one of the required commissioning tests and nothing in the controller writes mode 7.
    It writes 0 W, then mode 7, reads the mode every 10 s for 3 minutes, and ALWAYS writes mode 0 (the hand-back mode) at the end, whatever happened.
    Passes only if the device accepted mode 7, stayed in it for the whole 3 minutes, and then returned to mode 0. The test must be the only writer: it refuses
    to run beside a live controller (which would put the mode back within a minute); a logged or shadow controller is fine, as for T2."""
    blocked = (controller_may_write or _controller_may_write)()
    _need(blocked is None, blocked)
    before, mask = _mode(read_settings, client)
    _need(isinstance(before, int) and not isinstance(before, bool), f"cannot read the mode ({before!r})")
    _need(isinstance(mask, int) and not isinstance(mask, bool) and mask & (1 << DYNAMIC_MASK_BIT),
          f"the device does not offer dynamic pricing (mode mask {mask!r}, bit {DYNAMIC_MASK_BIT} clear): set up Dynamic Tariff in the Anker app first")
    _need(before != 3, "the battery is in third-party control (mode 3): something is driving it now; try again when it is in self-consumption")
    readings = live or live_readings

    def seen():
        now = readings()
        parts = [f"{name} {now[key] * scale:.{digits}f} {unit}" for name, key, scale, digits, unit in
                 (("battery level", "soc", 100, 0, "%"), ("Solarbank AC", "ac_power", 1, 2, "kW"), ("grid", "grid_power", 1, 2, "kW")) if key in now]
        return "; ".join(parts) or "no live readings"

    say(f"Starting in mode {before} ({seen()}). Writing 0 W, then mode {DYNAMIC_MODE}; watching for {DYNAMIC_HOLD_S:g} s; then back to mode {MODE_DEFAULT}.")
    problem, held = None, 0.0
    try:
        writer.write_setpoint(0)
        sleep(2.0)
        writer.write_mode(DYNAMIC_MODE)
        while held < DYNAMIC_HOLD_S:
            sleep(DYNAMIC_STEP_S)
            held += DYNAMIC_STEP_S
            now, _ = _mode(read_settings, client)
            say(f"  {held:>4.0f} s: mode {now} ({seen()})")
            if now != DYNAMIC_MODE:
                problem = (f"wrote mode {DYNAMIC_MODE} and the device read mode {now!r} after {held:g} s: it did not stay in dynamic pricing "
                           "(look in the Anker app for an error such as 'smart meter missing')")
                break
    except TestFailed:
        raise
    except Exception as error:                                 # a refused or failed write, or a read that failed: still hand the battery back below
        problem = f"switching to mode {DYNAMIC_MODE} failed after {held:g} s: {error.__class__.__name__}: {error}"
    finally:
        try:
            writer.write_mode(MODE_DEFAULT)
            sleep(2.0)
            after, _ = _mode(read_settings, client)
        except Exception as error:
            raise TestFailed(f"COULD NOT HAND THE BATTERY BACK to mode {MODE_DEFAULT} ({error.__class__.__name__}: {error}): set Self-Consumption in the Anker app now"
                             + (f"; before that, {problem}" if problem else "")) from None
    _need(after == MODE_DEFAULT, f"wrote mode {MODE_DEFAULT} to hand back and the device reads {after!r}: set Self-Consumption in the Anker app now"
          + (f"; before that, {problem}" if problem else ""))
    _need(problem is None, f"{problem}. The battery is back in mode {MODE_DEFAULT}")
    return f"mode {DYNAMIC_MODE} (dynamic pricing) held for {DYNAMIC_HOLD_S:g} s, then back to mode {MODE_DEFAULT}; it was in mode {before} before"


EXTRA_MODES = {"dynamic-mode": (DYNAMIC_MODE,)}         # the only test whose writer may write a mode the controller never writes
TESTS = {"read-registers": read_registers, "mode-write": mode_write, "default-mode": default_mode, "setpoint-zero-write": setpoint_zero_write,
         "release": release, "hands-off-latch": hands_off_latch, "setpoint-both-signs": setpoint_both_signs, "dynamic-mode": dynamic_mode}
NEEDS_DEVICE = {name for name in TESTS if name != "hands-off-latch"}


def _close(thing):
    close = getattr(thing, "close", None)
    if callable(close):
        close()


class _OneConnection:
    """The writer, closing the test's reader before every write (and passing everything else through)."""
    def __init__(self, writer, client):
        self._writer, self._client = writer, client

    def write_mode(self, value):
        _close(self._client)
        return self._writer.write_mode(value)

    def write_setpoint(self, watts):
        _close(self._client)
        return self._writer.write_setpoint(watts)

    def close(self):
        _close(self._writer)


def one_connection_at_a_time(read_settings, client, writer):
    """(read_settings, writer) that never hold two connections of the test's own at once. The Solarbank accepts at most THREE Modbus clients
    (measured on the Pi, 11 November 2026: with the collector and the controller connected, a third read worked and a fourth failed), so a test's own
    reader and writer together, beside a running controller, made a fourth and every write failed. Each read now closes the writer first and the
    reader after; each write closes the reader first. Both reconnect by themselves on their next use."""
    def read(c):
        _close(writer)
        try:
            return read_settings(c)
        finally:
            _close(client)
    return read, _OneConnection(writer, client)


def run(name, read_settings=None, client=None, writer=None, sleep=time.sleep):
    """Run one test; returns the note to record. Raises TestFailed with the reason."""
    if name not in TESTS:
        raise ValueError(f"'{name}' cannot be run here; the runnable tests are: " + ", ".join(TESTS))
    if name in NEEDS_DEVICE and (client is None or writer is None or read_settings is None):
        raise TestFailed("this test needs the device")
    if name in NEEDS_DEVICE:
        read_settings, writer = one_connection_at_a_time(read_settings, client, writer)
    return TESTS[name](read_settings, client, writer, sleep)
