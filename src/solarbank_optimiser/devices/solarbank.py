"""Solarbank 4 E5000 Pro input registers (FC04 only), decoded like the existing monitor.

Register map: vendor integration config for the Solarbank 4 E5000 Pro (MIT licence), pinned in
reference/anker-solix-official/. Power is INT32 high word first, SOC and status UINT16, energy
counters UINT32 high word first divided by 10. Sign convention: battery positive = discharging,
grid positive = import. Read-only: the vendor's connect-time mode change (register 10064) is
deliberately not implemented. LAYOUT is the single table of addresses; tests compare it with the pin.
"""
import math
import time

from .errors import DeviceError

BLOCKS = ((10000, 40), (10208, 2), (10250, 2), (10262, 4))         # everything, as the monitor reads it
MODE_REGISTER = 10064
MASK_REGISTER = 32774                                              # the modes the device allows third parties to use (input register)
LIMITS_REGISTER = 60000                                            # charge limit, discharge limit, backup reserve, backup reserve enable (holding registers)                                                # the operating mode (holding register), read-only here
FAST_BLOCK = (10000, 16)                                            # status, PV, battery, load, grid, SOC: read every poll
SLOW_BLOCKS = ((10018, 2), (10036, 4), (10208, 2), (10250, 2), (10262, 4))   # totals, limits, AC output: rotated
SETTINGS_COOLDOWN_S = 600.0                                                 # after a link-level failure of a settings read
STATUS = {0: "Standby", 1: "Charging", 2: "Discharging", 3: "Sleep"}

# name: (address, type, divisor, vendor key in the pinned map)
LAYOUT = {
    "status_code": (10001, "UINT16", 1, "battery_status"),
    "pv_direct": (10002, "INT32", 1, "pv_power"),
    "pv_external": (10004, "INT32", 1, "third_party_pv_power"),
    "battery": (10008, "INT32", 1, "battery_charging_power"),
    "load": (10010, "INT32", 1, "load_power"),
    "grid": (10012, "INT32", 1, "grid_import_power"),
    "soc": (10014, "UINT16", 1, "battery_soc"),
    "pv_total_kwh": (10018, "UINT32", 10, "pv_total_generation"),
    "max_charge": (10036, "INT32", 1, "max_charge_power"),
    "max_discharge": (10038, "INT32", 1, "max_discharge_power"),
    "ac_output": (10208, "INT32", 1, "ac_grid_output_power"),
    "rated_kwh": (10250, "UINT32", 10, "rated_energy"),
    "charge_total_kwh": (10262, "UINT32", 10, "cumulative_charge_energy"),
    "discharge_total_kwh": (10264, "UINT32", 10, "cumulative_discharge_energy"),
}


def signed(raw, bits):
    return raw - (1 << bits) if raw & (1 << (bits - 1)) else raw


def _value(blocks, name):
    address, kind, divisor, _ = LAYOUT[name]
    for start, registers in blocks:
        if start <= address and address - start + (1 if kind == "UINT16" else 2) <= len(registers):
            words = registers[address - start:address - start + (1 if kind == "UINT16" else 2)]
            break
    else:
        raise DeviceError("invalid", f"No register block covers {name}")
    if kind == "UINT16":
        return words[0]
    raw = (words[0] << 16) | words[1]
    if kind == "INT32":
        return signed(raw, 32)
    value = raw / divisor
    if not math.isfinite(value) or value < 0:
        raise DeviceError("invalid", "Invalid energy counter")
    return value


def decode(main, ac_out, rated, totals):
    """Registers -> the monitor's sample fields (without a time stamp)."""
    if len(main) != 40 or len(ac_out) != 2 or len(rated) != 2 or len(totals) != 4:
        raise DeviceError("invalid", "Unexpected register block sizes")
    blocks = list(zip((start for start, _ in BLOCKS), (main, ac_out, rated, totals)))
    get = lambda name: _value(blocks, name)
    soc = get("soc")
    if not 0 <= soc <= 100:
        raise DeviceError("invalid", "Solarbank charge percentage unavailable")
    code = get("status_code")
    return dict(soc=soc, status_code=code, status=STATUS.get(code, f"Unknown ({code})"),
                pv=get("pv_direct") + get("pv_external"), pv_direct=get("pv_direct"), pv_external=get("pv_external"),
                battery=get("battery"), load=get("load"), grid=get("grid"), ac_output=get("ac_output"),
                pv_total_kwh=get("pv_total_kwh"), charge_total_kwh=get("charge_total_kwh"),
                discharge_total_kwh=get("discharge_total_kwh"), rated_kwh=get("rated_kwh"),
                max_charge=get("max_charge"), max_discharge=get("max_discharge"))


def read(client):
    """One poll: four FC04 reads on the shared read-only client."""
    return decode(*(client.read_input_registers(address, count) for address, count in BLOCKS))


def decode_fast(main):
    """The control-critical fields from the 16-register fast block."""
    if len(main) != FAST_BLOCK[1]:
        raise DeviceError("invalid", "Unexpected register block size")
    blocks = [(FAST_BLOCK[0], main)]
    get = lambda name: _value(blocks, name)
    soc = get("soc")
    if not 0 <= soc <= 100:
        raise DeviceError("invalid", "Solarbank charge percentage unavailable")
    code = get("status_code")
    return dict(soc=soc, status_code=code, status=STATUS.get(code, f"Unknown ({code})"),
                pv=get("pv_direct") + get("pv_external"), pv_direct=get("pv_direct"), pv_external=get("pv_external"),
                battery=get("battery"), load=get("load"), grid=get("grid"))


def decode_slow(registers):
    """Totals, limits and AC output from the slow blocks (same order as SLOW_BLOCKS)."""
    if [len(r) for r in registers] != [count for _, count in SLOW_BLOCKS]:
        raise DeviceError("invalid", "Unexpected register block sizes")
    blocks = list(zip((start for start, _ in SLOW_BLOCKS), registers))
    get = lambda name: _value(blocks, name)
    return dict(pv_total_kwh=get("pv_total_kwh"), max_charge=get("max_charge"), max_discharge=get("max_discharge"),
                ac_output=get("ac_output"), rated_kwh=get("rated_kwh"), charge_total_kwh=get("charge_total_kwh"),
                discharge_total_kwh=get("discharge_total_kwh"))


class Reader:
    """One fast request per poll; the slow registers are refreshed every `slow_every` seconds.

    A failed slow refresh never blocks the control-critical fast data. The previous slow values (totals, limits, AC output)
    are reused only while they are still recent: after `slow_max_age` seconds they are dropped, so the quantities that
    depend on them read as unavailable instead of an old value riding along with a fresh timestamp. The age and the
    number of failed refreshes are always visible through health()."""

    def __init__(self, client, slow_every=10.0, slow_max_age=None, monotonic=time.monotonic, settings_every=None, settings_sink=None, wall=time.time, mode_every=None, safety_every=None):
        self.client, self.slow_every, self._mono = client, slow_every, monotonic
        # GitHub issue 36: the controller reads the operating mode (holding register 10064) itself, on this connection, every `mode_every` seconds, so supervision, the start-up
        # check and the release confirmation work from a reading a few seconds old, not from the collector's settings log (up to a minute stale). Read-only.
        self.mode_every, self._mode, self._mode_at, self._next_mode, self.mode_errors = mode_every, None, None, None, 0
        # GitHub issue 51: the two settings that decide whether third-party control is allowed at all (the mode mask, input register 32774, and the Backup Reserve
        # switch, holding register 60003) are read by the controller itself, on this connection, every `safety_every` seconds, not taken from a log that may be an hour old.
        self.safety_every, self._safety, self._safety_at, self._next_safety, self.safety_errors = safety_every, None, None, None, 0
        # C1: a read-only look at the device's settings through THIS connection (the Solarbank serves only a few clients, so no extra one is opened):
        # one register item per poll in rotation, a complete snapshot every `settings_every` seconds, handed to `settings_sink`. Never affects the fast data.
        self.settings_every, self.settings_sink, self._wall = settings_every, settings_sink, wall
        self._settings_items, self._settings_index, self._settings_next = {}, 0, None
        self.settings_errors = 0
        self.slow_max_age = slow_max_age if slow_max_age is not None else 3 * slow_every
        self._slow, self._slow_at, self._next_slow = {}, None, None
        self.slow_errors = 0
        self.slow_error_streak = 0
        self.received_at = None          # monotonic time the fast reply arrived; the caller stamps the reading with this

    def slow_age(self):
        return None if self._slow_at is None else self._mono() - self._slow_at

    def health(self):
        age = self.slow_age()
        return {"slow_age": None if age is None else round(age, 1), "slow_errors": self.slow_errors,
                "slow_error_streak": self.slow_error_streak, "slow_stale": age is None or age > self.slow_max_age}

    def __call__(self):
        self.received_at = None
        fast = decode_fast(self.client.read_input_registers(*FAST_BLOCK))
        now = self.received_at = self._mono()
        if self._next_slow is None or now >= self._next_slow:
            self._next_slow = now + self.slow_every          # success or failure, the next attempt waits the full interval
            try:
                self._slow = decode_slow([self.client.read_input_registers(start, count) for start, count in SLOW_BLOCKS])
                self._slow_at = now
                self.slow_error_streak = 0
            except DeviceError:
                self.slow_errors += 1
                self.slow_error_streak += 1
        if self.mode_every:
            if self._next_mode is None or now >= self._next_mode:
                self._next_mode = now + self.mode_every
                try:
                    self._mode, self._mode_at = self.client.read_holding_registers(MODE_REGISTER, 1)[0], now
                except DeviceError:
                    self.mode_errors += 1
            if self._mode_at is not None and now - self._mode_at > 3 * self.mode_every:
                self._mode, self._mode_at = None, None            # too old to publish: unknown, never an old value that looks fresh
        if self.safety_every:
            if self._next_safety is None or now >= self._next_safety:
                self._next_safety = now + self.safety_every
                try:
                    mask = self.client.read_input_registers(MASK_REGISTER, 1)[0]
                    limits = self.client.read_holding_registers(LIMITS_REGISTER, 4)
                    # GitHub issue 70 (D13, second half): the device's own charging and discharge limits (60000, 60001), from the same read as the safety
                    # registers, so the controller can clamp the target to them instead of only showing them on the Settings page.
                    self._safety, self._safety_at = {"mask": mask, "charging_limit_soc": limits[0], "discharge_limit_soc": limits[1],
                                                      "backup_soc_enable": limits[3]}, now
                except DeviceError:
                    self.safety_errors += 1
            if self._safety_at is not None and now - self._safety_at > 3 * self.safety_every:
                self._safety, self._safety_at = None, None         # too old to publish: unknown, never an old value that looks fresh
        if self.settings_every:
            self._settings_step(now)
        if self._slow_at is not None and now - self._slow_at > self.slow_max_age:
            self._slow, self._slow_at = {}, None             # too old to publish alongside a fresh reading
        extra = {"mode": self._mode, "mode_age_s": round(now - self._mode_at, 1)} if self._mode is not None else {}
        if self._safety is not None:
            extra = {**extra, **self._safety, "safety_age_s": round(now - self._safety_at, 1)}
        return {**fast, **self._slow, **extra, **({"slow_age_s": round(now - self._slow_at, 1)} if self._slow_at is not None else {})}

    def _settings_step(self, now):
        from . import settings_probe
        names = list(settings_probe.READS)
        if self._settings_next is None:
            self._settings_next = now
        if now < self._settings_next:
            return
        name = names[self._settings_index]
        try:
            self._settings_items[name] = settings_probe.read_item(self.client, name)
        except Exception:                      # nothing about this look may ever disturb the readings
            self.settings_errors += 1
        item = self._settings_items.get(name, {})
        if not item.get("ok", True) and item.get("error") != "exception":
            # The link itself failed (not a polite refusal of one register): leave the device alone for a while rather than risk repeating a
            # disturbance to the live readings every minute.
            self.settings_errors += 1
            self._settings_index, self._settings_next = 0, now + SETTINGS_COOLDOWN_S
            return
        self._settings_index += 1
        self._settings_next = now + self.settings_every / len(names)           # the next item is due after one n-th of the interval
        if self._settings_index >= len(names):
            self._settings_index = 0
            if self.settings_sink is not None and len(self._settings_items) == len(names):
                try:
                    self.settings_sink({"at": self._wall(), "items": dict(self._settings_items), "decoded": settings_probe.decode(self._settings_items)})
                except Exception:
                    self.settings_errors += 1
