#!/bin/sh
# The way back after the original energy monitor has been retired. Run ON THE PI:  sh rollback-cutover.sh <previous-release-id>
# 1. stop the controller, which frees the meter (it allows only ONE Modbus client);
# 2. start the original monitor again and enable it for boot;
# 3. switch back to the previous release, whose collector reads the monitor again (mixed mode).
# Nothing is deleted: the monitor's data and service were only stopped and disabled. Takes about a minute.
set -eu
release_id="${1:-}"
case "$release_id" in ""|*[!0-9A-Za-z.-]*) echo "Give the previous release id (see: ls /opt/solarbank-cost-optimiser/releases)." >&2; exit 2;; esac
root="${SOLARBANK_ROOT:-/opt/solarbank-cost-optimiser}"
test -d "$root/releases/$release_id" || { echo "No such release: $release_id" >&2; exit 2; }
sudo systemctl stop solarbank-optimiser-control
sudo systemctl enable --now anker-energy-monitor
echo "Original monitor started; waiting for it to answer."
n=0
until curl --silent --fail --max-time 3 http://127.0.0.1:8080/api/readings >/dev/null; do
  n=$((n + 1)); [ "$n" -ge 30 ] && { echo "The monitor did not answer within a minute: check journalctl -u anker-energy-monitor." >&2; exit 1; }
  sleep 2
done
SOLARBANK_SKIP_BACKUP=1 sh "$root/releases/$release_id/scripts/switch-release.sh" "$release_id"
echo "Back on release $release_id with the original monitor. Check the dashboard."
