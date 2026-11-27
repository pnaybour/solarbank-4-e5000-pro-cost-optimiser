"""Stand-alone battery sign check: the specification's T2 sequence with EVERY optimiser service stopped, so this program alone talks to the
Solarbank, the smart meter and the Shelly (owner's request, 11 November 2026). It confirms how the battery responds; it records nothing.

It refuses unless all five optimiser services are stopped, then runs exactly the T2 runner's sequence and protections (commission_tests): the car
not charging, battery 20 to 90 %, house at least 400 W, garage under 40 A, readings fresh, 20 set-points of 200 to 600 W held 20 s each, and the
hand-back (0 W, then the mode found) on every path, Ctrl-C included.

On the Pi, with the owner at the battery:
    sudo systemctl stop solarbank-optimiser-control solarbank-optimiser-collector solarbank-optimiser-shadow solarbank-optimiser-providers solarbank-optimiser-web
    sudo /opt/solarbank-cost-optimiser/current/.venv/bin/python /opt/solarbank-cost-optimiser/current/scripts/battery_sign_check.py --confirm
    sudo systemctl start solarbank-optimiser-collector solarbank-optimiser-control solarbank-optimiser-shadow solarbank-optimiser-providers solarbank-optimiser-web
"""
import argparse
import os
import sys
import time

from solarbank_optimiser import commission_tests as t
from solarbank_optimiser.devices import meter, shelly, solarbank
from solarbank_optimiser.devices.modbus import ReadOnlyModbusTcp
from solarbank_optimiser.devices.modbus_write import GuardedModbusWriter
from solarbank_optimiser.devices.settings_probe import read_settings
from solarbank_optimiser.devices_config import DeviceConfigError, require, resolve


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stand-alone battery sign check (T2) with every optimiser service stopped")
    parser.add_argument("--confirm", action="store_true", help="I am at the battery and every optimiser service is stopped")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("run it with sudo")
    if not args.confirm:
        parser.error("this writes to the battery: stop every optimiser service, be at the battery, and add --confirm")
    running = t.services_running()
    if running:
        print("NOT STARTED: these optimiser services are still running: " + ", ".join(running) + ". Stop them first; nothing was written.")
        return 2
    try:
        hosts = require(resolve(), ("solarbank", "meter", "shelly"))
    except DeviceConfigError as problem:
        print(f"NOT STARTED: {problem}; nothing was written.")
        return 2
    sb, mt = ReadOnlyModbusTcp(hosts["solarbank"]), ReadOnlyModbusTcp(hosts["meter"])
    writer = GuardedModbusWriter(hosts["solarbank"])
    live = t.direct_readings(lambda: solarbank.read(sb), lambda: meter.read(mt), lambda: shelly.read_channels(hosts["shelly"]))
    try:
        note = t.setpoint_both_signs(read_settings, sb, writer, time.sleep, live=live, controller_may_write=lambda: None,
                                     say=lambda text: print(text, flush=True))
        print(f"PASSED: {note}")
        result = 0
    except t.TestFailed as failure:
        print(f"FAILED: {failure}")
        result = 6
    finally:
        for c in (sb, mt, writer):
            c.close()
    print("Start the services again: sudo systemctl start solarbank-optimiser-collector solarbank-optimiser-control "
          "solarbank-optimiser-shadow solarbank-optimiser-providers solarbank-optimiser-web")
    return result


if __name__ == "__main__":
    sys.exit(main())
