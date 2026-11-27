#!/bin/sh
# Repeatable local source release. Existing Pi state/credentials are preserved.
set -eu
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
release_id="$(.venv/bin/python -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')-$(date -u +%Y%m%dT%H%M%SZ)"
case "$release_id" in *[!0-9A-Za-z.-]*) exit 1;; esac
mkdir -p runtime/deployment
.venv/bin/python - <<'PY'
import tarfile
with tarfile.open('runtime/deployment/source.tar.gz','w:gz') as archive:
 for name in ('src','tests','scripts','deployment','.env.example','pyproject.toml','requirements.lock','solver-requirements.lock','SOFTWARE_SPECIFICATION.md','DEVELOPMENT_PLAN.md','reference','README.md','OPERATIONS.md','REQUIREMENTS_AMENDMENTS.md','POST_SHADOW_PLAN.md','TESTING_REPORT_2026-11-01.md'):
  archive.add(name,filter=lambda i:None if '__pycache__' in i.name or '.egg-info' in i.name else i)
PY
# Preflight: our own state and solver environment exist. (The original energy monitor was retired on 28 Oct 2026; it is no longer required to be running.)
ssh solarbank-pi 'sudo test -f /var/lib/solarbank-cost-optimiser/controller.sqlite3 && test -x /opt/solarbank-cost-optimiser/solver-venv/bin/python'
scp runtime/deployment/source.tar.gz "solarbank-pi:/tmp/solarbank-shadow-$release_id.tar.gz"
ssh solarbank-pi "sudo install -d -o pi -g pi /opt/solarbank-cost-optimiser/releases/$release_id; sudo tar -xzf /tmp/solarbank-shadow-$release_id.tar.gz -C /opt/solarbank-cost-optimiser/releases/$release_id; sudo chown -R pi:pi /opt/solarbank-cost-optimiser/releases/$release_id; cd /opt/solarbank-cost-optimiser/releases/$release_id && sh scripts/install-pi-shadow.sh"
# Preserve state before switching. The switch runs on the Pi from the staged release and stops at the first failure,
# so a failed backup never leads on to stopping services. SOLARBANK_SKIP_BACKUP=1 skips the (slow, integrity-checked)
# backup; use only when a recent verified backup exists.
ssh solarbank-pi "SOLARBANK_SKIP_BACKUP=${SOLARBANK_SKIP_BACKUP:-0} sh /opt/solarbank-cost-optimiser/releases/$release_id/scripts/switch-release.sh $release_id"
ssh solarbank-pi 'curl --silent --show-error --fail http://solarbank-pi.local:8091/health'
