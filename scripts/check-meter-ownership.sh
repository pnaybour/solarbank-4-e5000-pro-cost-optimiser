#!/bin/sh
# Run ON THE PI:  sh check-meter-ownership.sh
# The smart meter allows ONE Modbus client and the optimiser's controller owns it. This fails if anything else is connected to it,
# or if the kernel-level guard that keeps the original energy monitor away from the meter is missing. Read-only.
set -eu
meter="${METER_ADDRESS:-$(sed -n 's/^SOLARBANK_METER_HOST=//p' /etc/solarbank-cost-optimiser/devices.conf 2>/dev/null | head -n 1)}"
[ -n "$meter" ] || { echo "No meter address: set METER_ADDRESS or SOLARBANK_METER_HOST in /etc/solarbank-cost-optimiser/devices.conf" >&2; exit 2; }
failed=0
for pid in $(sudo ss -tnpH "dst $meter" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u); do
  unit=$(sed -n 's|.*/\([^/]*\.service\)$|\1|p' "/proc/$pid/cgroup" 2>/dev/null | head -1)
  if [ "$unit" = "solarbank-optimiser-control.service" ]; then
    echo "OK    meter connection held by the controller (pid $pid)"
  else
    echo "FAIL  meter connection held by '${unit:-unknown}' (pid $pid): only the controller may read the meter" >&2; failed=1
  fi
done
if systemctl show anker-energy-monitor -p IPAddressDeny 2>/dev/null | grep -q "$meter"; then
  echo "OK    the original monitor is blocked from the meter by systemd (IPAddressDeny)"
else
  echo "FAIL  the original monitor is not blocked from the meter: install deployment/monitor-no-meter.conf" >&2; failed=1
fi
exit "$failed"
