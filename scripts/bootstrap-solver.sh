#!/bin/sh
# Build the isolated EMHASS solver environment from the pinned lock file.
#   development:  sh scripts/bootstrap-solver.sh                      (creates ./.emhass-venv)
#   on the Pi:    sudo sh scripts/bootstrap-solver.sh /opt/solarbank-cost-optimiser/solver-venv
# The environment is separate from the application's own, never upgraded in place, and read-only to the services.
set -eu
cd "$(dirname "$0")/.."
target="${1:-.emhass-venv}"
python3 -m venv "$target"
"$target/bin/python" -m pip install --no-cache-dir -r solver-requirements.lock
"$target/bin/python" -m pip check
"$target/bin/python" -c "import emhass; print('EMHASS environment ready:', emhass.__file__)"
