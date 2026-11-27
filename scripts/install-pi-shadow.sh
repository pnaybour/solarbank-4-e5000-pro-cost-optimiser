#!/bin/sh
# Run on the Pi in an unpacked, tested release. No equipment commands.
set -eu
umask 022          # never inherit a restrictive umask: the service user must be able to read the release (GitHub issue 47)
release_dir=$(pwd)
case "$release_dir" in /opt/solarbank-cost-optimiser/releases/*) ;; *) echo 'Run inside a staged release'; exit 1;; esac
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q --junitxml=pi-test-results.xml
