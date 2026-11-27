#!/bin/sh
# Install only this application. No SSH, service changes or equipment access.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/python -m pytest -q
.venv/bin/solarbank init
printf '%s\n' 'Local foundation ready. Run .venv/bin/solarbank serve to open the dashboard.'
