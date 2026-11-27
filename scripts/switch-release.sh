#!/bin/sh
# Run ON THE PI to switch to a staged release:  SOLARBANK_SKIP_BACKUP=1 sh switch-release.sh <release-id>
# It stops at the FIRST failure. In particular a failed backup never leads on to stopping services or switching
# releases, and if activation fails after the services were stopped, it tries to start them again on whatever release is current.
set -eu
umask 022          # a restrictive umask in the owner's terminal once made a whole release unreadable to the service user (GitHub issue 47)
release_id="${1:-}"
case "$release_id" in ""|*[!0-9A-Za-z.-]*) echo "Give a release id made of letters, digits, dots and hyphens." >&2; exit 2;; esac
root="${SOLARBANK_ROOT:-/opt/solarbank-cost-optimiser}"
state="${SOLARBANK_STATE:-/var/lib/solarbank-cost-optimiser}"
services="solarbank-optimiser-web solarbank-optimiser-collector solarbank-optimiser-providers solarbank-optimiser-shadow"
# The controller is stopped and restarted with the others once its unit exists (the first release that brings it has nothing to stop).
if [ -f "${SOLARBANK_UNITS:-/etc/systemd/system}/solarbank-optimiser-control.service" ]; then
  services="solarbank-optimiser-control $services"
fi
stopped=0

# RC1: the release that moved minute history into the telemetry files names itself as the oldest one that can still read the data. An older release
# is refused unless SOLARBANK_ALLOW_OLDER=1 (a last resort: it sees minute history only up to the switch).
marker="$state/oldest-compatible-release"
if [ -r "$marker" ] && [ "${SOLARBANK_ALLOW_OLDER:-0}" != 1 ]; then
  oldest=$(cat "$marker")
  stamp_of() { printf '%s' "$1" | grep -o '[0-9]\{8\}T[0-9]\{6\}Z' | head -n 1; }
  wanted=$(stamp_of "$release_id"); floor=$(stamp_of "$oldest")
  refuse() {
    echo "Refused: $1" >&2
    echo "To force it anyway (minute history then ends at the switch to $oldest): SOLARBANK_ALLOW_OLDER=1 sh $0 $release_id" >&2
    exit 3
  }
  # A name or marker that cannot be read is a refusal, never a pass: the guard must not fail open.
  if [ -z "$wanted" ] || [ -z "$floor" ]; then
    refuse "cannot compare $release_id with the compatibility marker '$oldest' (no YYYYMMDDTHHMMSSZ stamp in one of them)."
  fi
  if [ "$wanted" != "$floor" ] && [ "$(printf '%s\n%s\n' "$wanted" "$floor" | sort | head -n 1)" = "$wanted" ]; then
    refuse "$release_id is older than $oldest, the oldest release that can read the minute history now kept in the telemetry files. Switch to $oldest or newer."
  fi
fi

restore_previous() {
  # Put back what the activation changed: the release `current` points at, and the rollback floor (the activation may have raised it, and this release did not stay).
  if [ -n "${previous:-}" ] && [ "$previous" != "$root/releases/$release_id" ]; then
    sudo ln -sfn "$previous" "$root/current.next" && sudo mv -Tf "$root/current.next" "$root/current"
  fi
  if [ "${marker_before:-0}" = 0 ]; then
    sudo rm -f "$state/oldest-compatible-release" "$state/oldest-compatible-generation"
  else
    printf '%s\n' "$marker_was" | sudo tee "$state/oldest-compatible-release" >/dev/null
    if [ -n "$generation_was" ]; then printf '%s\n' "$generation_was" | sudo tee "$state/oldest-compatible-generation" >/dev/null; else sudo rm -f "$state/oldest-compatible-generation"; fi
  fi
}

activation_started=0

recover() {
  code=$?
  if [ "$code" -ne 0 ] && [ "$stopped" = 1 ]; then
    echo "Switch failed (exit $code) after the services were stopped: trying to start them again." >&2
    if [ "$activation_started" = 1 ]; then          # the activation began and did not finish: go back to the previous release and its floor first (GitHub 54)
      echo "The activation did not finish: putting the previous release and the compatibility floor back." >&2
      restore_previous
    fi
    # shellcheck disable=SC2086
    sudo systemctl start $services || echo "Could not restart the services; start them by hand." >&2
  elif [ "$code" -ne 0 ] && [ "$code" -ne 3 ]; then
    echo "Switch stopped before touching any service (exit $code)." >&2
  fi
  exit "$code"
}
trap recover EXIT

# The new release must be readable by the service user BEFORE anything is stopped or switched (issue 47).
sudo chmod -R go+rX "$root/releases/$release_id"
sudo -u solarbank-optimiser test -x "$root/releases/$release_id/.venv/bin/solarbank" \
  || { echo "Refused: the service user cannot run $root/releases/$release_id/.venv/bin/solarbank (permissions)." >&2; exit 4; }
# The Pi's own test report is read HERE too, the last common step before anything is stopped (GitHub 56): a release whose tests failed is never switched to, whichever
# script was used. A release with no report (one from before this gate, or a rollback target) needs SOLARBANK_ALLOW_UNTESTED=1.
report="$root/releases/$release_id/pi-test-results.xml"
if [ -f "$report" ]; then
  python3 - "$report" <<'PY' || { echo "Refused: the Pi's test report for $release_id shows failures or errors (or too few tests)." >&2; exit 5; }
import sys, xml.etree.ElementTree as ET
try:
    top = ET.parse(sys.argv[1]).getroot()
except (OSError, ET.ParseError):
    sys.exit(1)
suites = [top] if top.tag == "testsuite" else list(top)
failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
tests = sum(int(s.get("tests", 0)) for s in suites)
sys.exit(1 if failed or tests < 100 else 0)
PY
elif [ "${SOLARBANK_ALLOW_UNTESTED:-0}" != 1 ]; then
  echo "Refused: there is no test report for $release_id. To switch to a release that was never tested on the Pi (an older one you are rolling back to): SOLARBANK_ALLOW_UNTESTED=1 sh $0 $release_id" >&2
  exit 5
fi
previous=$(readlink -f "$root/current" 2>/dev/null || true)
marker_before=0; [ -f "$state/oldest-compatible-release" ] && marker_before=1
marker_was=$(cat "$state/oldest-compatible-release" 2>/dev/null || true); generation_was=$(cat "$state/oldest-compatible-generation" 2>/dev/null || true)

if [ "${SOLARBANK_SKIP_BACKUP:-0}" = 1 ]; then
  echo "Backup skipped on request (SOLARBANK_SKIP_BACKUP=1)."
else
  sudo -u solarbank-optimiser "$root/current/.venv/bin/solarbank" --database "$state/controller.sqlite3" backup "$state/backups/before-$release_id.sqlite3"
  # The new backup exists and passed the integrity check (the backup command checks it and fails otherwise), so older ones may go: keep the newest few.
  keep="${SOLARBANK_KEEP_BACKUPS:-3}"
  case "$keep" in ""|*[!0-9]*|0) keep=3;; esac
  sudo sh -c 'cd "$1/backups" 2>/dev/null && ls -1t before-*.sqlite3 | tail -n +"$(($2 + 1))" | while read -r old; do rm -f -- "$old"; done' prune-backups "$state" "$keep" \
    || echo "Could not prune old backups (not fatal): remove the oldest by hand if the disk fills." >&2
fi
stopped=1
# shellcheck disable=SC2086
sudo systemctl stop $services
activation_started=1
sudo sh "$root/releases/$release_id/scripts/activate-pi-shadow.sh"
activation_started=0
stopped=0

# Check the services really came up, and go back to the previous release if they did not (issue 47). Two checks in a row, a few seconds apart.
wait="${SOLARBANK_HEALTH_WAIT:-90}"; step="${SOLARBANK_HEALTH_STEP:-5}"; good=0; tries=0
if [ "$step" -gt 0 ]; then limit=$((wait / step + 2)); else limit=3; fi
while :; do
  all=1
  for unit in $services; do
    sudo systemctl is-active --quiet "$unit" || all=0
  done
  tries=$((tries + 1))
  if [ "$all" = 1 ]; then good=$((good + 1)); else good=0; fi
  [ "$good" -ge 2 ] && break
  [ "$tries" -ge "$limit" ] && break
  sleep "$step"
done
if [ "$good" -lt 2 ]; then
  echo "Release $release_id was activated but its services did not come up: going back to ${previous:-the previous release}." >&2
  restore_previous
  # shellcheck disable=SC2086
  sudo systemctl restart $services || true
  echo "Rolled back. Look at: journalctl -u solarbank-optimiser-control --since '10 min ago'" >&2
  exit 3
fi
echo "Release $release_id activated and its services are running."
