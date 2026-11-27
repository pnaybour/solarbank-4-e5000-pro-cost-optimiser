#!/bin/sh
# The one command to run before every push (GitHub 60): lint, the documents check, and the whole test suite including the JavaScript tests.
# The Pi's deploy gate runs the tests only (no ruff, no node): this is where lint and the JavaScript tests are enforced.
set -eu
cd "$(dirname "$0")/.."
command -v ruff >/dev/null || { echo "ruff is not installed: pip install ruff"; exit 2; }
command -v node >/dev/null || { echo "node is not installed: the JavaScript tests would be skipped, and they must run before a push"; exit 2; }
ruff check --select F src
python3 scripts/check-docs.py
python3 -m pytest tests -q
