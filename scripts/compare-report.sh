#!/bin/sh
# Print the 4a.5 comparison report. Run on the Pi:  sh /opt/solarbank-cost-optimiser/current/scripts/compare-report.sh
exec sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank \
  --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 \
  compare report --file /var/lib/solarbank-cost-optimiser/comparison/compare.jsonl "$@"
