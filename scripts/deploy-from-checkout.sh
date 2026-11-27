#!/bin/sh
# Run ON THE PI from a git checkout (for example ~/solarbank-src) to stage, test and switch to a release built from the committed HEAD.
# The same steps as deploy-pi-shadow.sh, without needing the Mac: stage -> install-pi-shadow.sh (builds the venv and runs the FULL test
# suite, stopping on any failure) -> switch-release.sh (backup, stop, activate) -> health check. Shadow mode is unchanged.
#   sh scripts/deploy-from-checkout.sh                         # with the usual pre-switch database backup
#   SOLARBANK_SKIP_BACKUP=1 sh scripts/deploy-from-checkout.sh  # only if a recent verified backup exists
set -eu
cd "$(dirname "$0")/.."
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "This checkout has uncommitted changes; commit or discard them first." >&2; exit 1
fi
version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
release_id="$version-$(date -u +%Y%m%dT%H%M%SZ)"
case "$release_id" in *[!0-9A-Za-z.-]*) echo "Bad release id: $release_id" >&2; exit 1;; esac
root=/opt/solarbank-cost-optimiser
state=/var/lib/solarbank-cost-optimiser

# Storage guard headroom (issue #11): the switch saves a full database backup into $state/backups, and the hourly guard stops the
# collector, planner and website once $state passes 16 GiB. Refuse to deploy if the backup would come within 10% of that limit.
used=$(sudo du -sb "$state" | cut -f1)
db=$(sudo sh -c "du -cb $state/controller.sqlite3*" | tail -1 | cut -f1)
limit=$((16 * 1024 * 1024 * 1024))
if [ "${SOLARBANK_SKIP_BACKUP:-0}" != 1 ] && [ $((used + db)) -gt $((limit * 9 / 10)) ]; then
  echo "A backup would take $state to within 10% of the 16 GiB storage-guard limit." >&2
  echo "Remove old files from $state/backups first, or rerun with SOLARBANK_SKIP_BACKUP=1." >&2
  exit 1
fi

echo "Staging release $release_id from $(git rev-parse --short HEAD) ($(git rev-parse --abbrev-ref HEAD))"
sudo install -d -o pi -g pi "$root/releases/$release_id"
git archive HEAD | tar -x -C "$root/releases/$release_id"
cd "$root/releases/$release_id"
sh scripts/install-pi-shadow.sh
SOLARBANK_SKIP_BACKUP="${SOLARBANK_SKIP_BACKUP:-0}" sh scripts/switch-release.sh "$release_id"
sleep 10
systemctl --no-pager is-active solarbank-optimiser-control solarbank-optimiser-collector solarbank-optimiser-providers \
  solarbank-optimiser-shadow solarbank-optimiser-web
curl --silent --show-error --fail http://solarbank-pi.local:8091/health && echo
echo "Release $release_id is running. To roll back: sh $root/releases/<previous release>/scripts/switch-release.sh <previous release>"
