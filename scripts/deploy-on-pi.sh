#!/bin/sh
# Deploy a branch straight onto the Pi, when the Mac (and so scripts/deploy-pi-shadow.sh) is not available. Run ON THE PI:
#
#     read -rs GITHUB_TOKEN; export GITHUB_TOKEN      # a read-only token for this one repository (Contents: read); or keep it in ~/.solarbank-deploy-token (chmod 600)
#     sh deploy-on-pi.sh fix/wp26-car-signal
#
# It does what deploy-pi-shadow.sh does, in the same order, except that the source comes from GitHub instead of the Mac and the test gate is the
# suite run on the Pi by install-pi-shadow.sh: stage a release, build its virtual environment from requirements.lock, run the suite, back up the database,
# stop the services, activate, check health. It stops at the first failure; nothing is switched unless the suite passed on the Pi.
# SOLARBANK_SKIP_BACKUP=1 skips the backup (use only when a recent verified backup exists).
set -eu
umask 022          # a restrictive umask in the owner's terminal once made a release unreadable to the service user (GitHub issue 47)
branch="${1:-}"
[ -n "$branch" ] || { echo "Give the branch to deploy, e.g.  sh deploy-on-pi.sh fix/wp26-car-signal" >&2; exit 2; }
token_file="${SOLARBANK_TOKEN_FILE:-$HOME/.solarbank-deploy-token}"      # optional: a read-only token kept in a file only you can read (chmod 600)
if [ -z "${GITHUB_TOKEN:-}" ] && [ -r "$token_file" ]; then
  [ "$(stat -c %a "$token_file")" = 600 ] || { echo "$token_file must be readable by you only (chmod 600)." >&2; exit 1; }
  GITHUB_TOKEN=$(cat "$token_file"); export GITHUB_TOKEN
fi
: "${GITHUB_TOKEN:?Set GITHUB_TOKEN first (read -rs GITHUB_TOKEN; export GITHUB_TOKEN) or save it in $token_file}"
repo="${SOLARBANK_REPO:-OWNER/solarbank-4-e5000-pro-cost-optimiser}"          # your GitHub repository as owner/name (a fork of this one)
root=/opt/solarbank-cost-optimiser
state=/var/lib/solarbank-cost-optimiser
safe_branch=$(printf '%s' "$branch" | tr -c '0-9A-Za-z.-' '-')
release_id="pi-$(date -u +%Y%m%dT%H%M%SZ)-$safe_branch"
case "$release_id" in *[!0-9A-Za-z.-]*) echo "Unusable release id: $release_id" >&2; exit 1;; esac

# One spinning disk serves the collector, the dashboard and this install: two heavy jobs at once stall the live services (measured 6 November: disk wait up to 71 %, swap full).
# Refuse to start while a backup, a database clean-out or another install is running (SOLARBANK_ALLOW_OVERLAP=1 to override).
if [ "${SOLARBANK_ALLOW_OVERLAP:-0}" != 1 ] && pgrep -f 'solarbank.* offsite-backup|solarbank.* db clean|switch-release\.sh|install-pi-shadow\.sh' >/dev/null 2>&1; then
  echo "Another heavy job (backup, database clean-out or install) is running: not deploying. Wait for it, then run this again." >&2; exit 1
fi
# The install and the Pi's tests run at the lowest priority for CPU and disk, so the controller, collector and dashboard keep the disk.
if command -v ionice >/dev/null 2>&1; then low="nice -n 10 ionice -c3"; else low="nice -n 10"; fi

# Preflight, as the Mac script does: the state and the solver environment exist.
sudo test -f "$state/controller.sqlite3" && test -x "$root/solver-venv/bin/python" || { echo "State or solver environment missing: not deploying." >&2; exit 1; }

archive="/tmp/solarbank-$release_id.tar.gz"
trap 'rm -f "$archive"' EXIT
curl --fail --silent --show-error --location -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
     -o "$archive" "https://api.github.com/repos/$repo/tarball/$branch"
unset GITHUB_TOKEN        # not needed again
sudo install -d -o pi -g pi "$root/releases/$release_id"
sudo tar -xzf "$archive" --strip-components=1 -C "$root/releases/$release_id"
sudo chown -R pi:pi "$root/releases/$release_id"
(cd "$root/releases/$release_id" && $low sh scripts/install-pi-shadow.sh)
# A second gate (issue 47): read the test report itself, and switch only if it shows no failures and no errors.
python3 - "$root/releases/$release_id/pi-test-results.xml" <<'PY' || { echo "The Pi's test report is missing or shows failures: not switching." >&2; exit 1; }
import sys, xml.etree.ElementTree as ET
try:
    root = ET.parse(sys.argv[1]).getroot()
except (OSError, ET.ParseError):
    sys.exit(1)
suites = [root] if root.tag == "testsuite" else list(root)
failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
tests = sum(int(s.get("tests", 0)) for s in suites)
sys.exit(1 if failed or tests < 100 else 0)
PY
SOLARBANK_SKIP_BACKUP="${SOLARBANK_SKIP_BACKUP:-0}" sh "$root/releases/$release_id/scripts/switch-release.sh" "$release_id"
# The web service may still be starting just after the restart (7 November: a 502 right after activation, "ok" a minute later): ask the service itself, a few times.
health=""; for _try in 1 2 3 4 5 6; do
  health=$(curl --silent --fail --max-time 5 http://127.0.0.1:8091/health) && break
  health=""; sleep 5
done
if [ -n "$health" ]; then echo "$health"; else echo "The dashboard did not answer /health within 30 s after the restart: check 'systemctl is-active solarbank-optimiser-web' and its journal." >&2; fi
echo "Deployed $release_id. Roll back by pointing 'current' at the previous release (see deployment/README.md) and restarting the services."
