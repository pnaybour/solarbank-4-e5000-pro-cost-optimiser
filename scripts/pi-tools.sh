#!/bin/bash
# Short commands for the Pi, so nothing long has to be pasted on a phone.   sudo bash pi-tools.sh <command>
# Reads and reports. Only `backup-drill` and `backfill` run anything that changes files, and they say so first.
set -u
ROOT=/opt/solarbank-cost-optimiser/current
STATE=/var/lib/solarbank-cost-optimiser
BIN="$ROOT/.venv/bin/solarbank"
DB="$STATE/controller.sqlite3"
SERVICES="control web collector providers shadow"
as_service() { sudo -u solarbank-optimiser "$@"; }

status() {
  echo "== services"
  for s in $SERVICES; do printf "%-10s %s\n" "$s" "$(systemctl is-active solarbank-optimiser-$s)"; done
  echo "== errors today (journal, priority error or worse)"
  for s in $SERVICES; do printf "%-10s %s\n" "$s" "$(journalctl -u solarbank-optimiser-$s --since today -p err --no-pager | grep -vc '^-- ')"; done
  echo "== automatic restarts since each service last started"
  for s in control collector shadow providers web; do printf "%-10s %s\n" "$s" "$(systemctl show -p NRestarts --value solarbank-optimiser-$s.service)"; done
  echo "== disk"; df -h / | tail -1; du -sh "$STATE" 2>/dev/null
  echo "== alerts"; ls /run/solarbank-cost-optimiser/alerts 2>/dev/null | grep . || echo "none"
  echo "== mode"; cat /etc/solarbank-cost-optimiser/control-mode 2>/dev/null || cat "$STATE/control-mode" 2>/dev/null || echo "(no mode file)"
  echo "== release"; readlink "$ROOT"
}

overnight() {
  status
  echo "== controller commands since 23:00 yesterday"
  since=$(date -d "yesterday 23:00" +%s)
  python3 - "$STATE/control-commands.jsonl" "$since" <<'PY'
import json, sys, collections
path, since = sys.argv[1], float(sys.argv[2])
count = collections.Counter()
try:
    for line in open(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("at", 0) >= since and row.get("event") == "command":
            count[(row["command"]["channel"], row["command"]["action"])] += 1
except OSError:
    print("no command log")
for (channel, action), n in sorted(count.items()):
    print(f"  {channel:10s} {action:12s} {n}")
if not count:
    print("  none")
PY
  echo "== slot report"
  as_service "$ROOT/.venv/bin/python" "$ROOT/scripts/slot_report.py" 2>&1 | tail -25
}

logs() {
  s="${1:-control}"
  case " $SERVICES " in *" $s "*) ;; *) echo "service must be one of: $SERVICES"; exit 2;; esac
  journalctl -u "solarbank-optimiser-$s" --since "6 hours ago" -p warning --no-pager | tail -40
}

telemetry() {
  d="$STATE/telemetry"
  [ -d "$d" ] || { echo "no telemetry folder yet (this release does not write one)"; exit 0; }
  echo "files: $(ls "$d"/*.wsp 2>/dev/null | wc -l)   size: $(du -sh "$d" | cut -f1)"
  now=$(date +%s)
  for f in household_power soc pv_power battery_power grid_power ev_power; do
    if [ -f "$d/$f.wsp" ]; then
      age=$(( now - $(stat -c %Y "$d/$f.wsp") ))
      printf "%-16s last written %s s ago\n" "$f" "$age"
    else
      printf "%-16s missing\n" "$f"
    fi
  done
}

commission() { "$BIN" commission status; }

backup_drill() {
  echo "Running the off-site backup as a local drill: builds and verifies an archive, uploads nothing."
  as_service env RCLONE_CONFIG="$STATE/rclone.conf" "$BIN" --database "$DB" offsite-backup --local-only
}

backfill() {
  echo "Copying the SQLite minute rows into the telemetry files, then comparing the two. Nothing is deleted. Safe to repeat."
  if as_service "$BIN" --database "$DB" telemetry backfill; then echo "RESULT: OK - the stores agree"; else echo "RESULT: FAILED (exit $?) - do NOT rely on the switch; send me this output"; exit 1; fi
}

db_report() {
  as_service "$BIN" --database "$DB" db report | python3 -c 'import json,sys; d=json.load(sys.stdin); print("file %.0f MB, wal %.0f MB" % (d["file_bytes"]/1e6, d["wal_bytes"]/1e6)); [print("%-22s %10d rows %8.0f MB %s" % (t["name"], t["rows"], t["bytes"]/1e6, "(kept)" if t["protected"] else "")) for t in d["tables"]]'
}

db_clean() {
  echo "Dry run: says what the clean-out would remove and changes nothing. To do it: sudo bash pi-tools.sh db-clean-apply"
  as_service "$BIN" --database "$DB" db clean
}

db_clean_apply() {
  echo "Cleaning out the database: a verified backup first, then removal of what nothing reads, then compaction. Needs free disk space about the size of the database."
  as_service "$BIN" --database "$DB" db clean --apply && echo "RESULT: OK" || { echo "RESULT: NOT CLEANED (exit $?) - nothing was removed before the backup; send me this output"; exit 1; }
}

writes() {
  as_service "$BIN" control writes --last "${1:-8}"
}

set_mode() {
  [ "$(id -u)" = 0 ] || { echo "run this with sudo"; exit 2; }
  case "$1" in shadow|logged) ;; *) echo "this command only sets shadow or logged; going live is a separate, deliberate step (OPERATIONS.md, commissioning)"; exit 2;; esac
  printf '%s\n' "$1" > /etc/solarbank-cost-optimiser/control-mode
  chown root:root /etc/solarbank-cost-optimiser/control-mode; chmod 644 /etc/solarbank-cost-optimiser/control-mode
  systemctl restart solarbank-optimiser-control
  sleep 5
  echo "Mode file now: $(cat /etc/solarbank-cost-optimiser/control-mode). The controller restarted. What it says it is doing:"
  python3 -c "import json; d=json.load(open('/run/solarbank-cost-optimiser/control-loop.json')); e=d.get('execution',{}); print(' ', e.get('mode'), '(asked for', str(e.get('requested'))+')', '-', e.get('note'))" 2>/dev/null || echo "  (its state file is not written yet: look again in a minute with: sudo bash pi-tools.sh status)"
  echo "Nothing is ever sent in shadow or logged mode."
}

verify_clean() {
  newest=$(sudo ls -t "$STATE"/backups/before-clean-*.sqlite3 2>/dev/null | head -1)
  [ -n "$newest" ] || { echo "no before-clean backup found in $STATE/backups"; exit 1; }
  echo "Comparing every metric in $newest with the telemetry files (reads only):"
  as_service "$BIN" --database "$DB" db verify-backup --backup "$newest"
}

ops_io() { as_service "$BIN" ops io --seconds "${1:-120}"; }
ops_memory() { as_service "$BIN" ops memory; echo; free -m | head -3; }
ops_profile() { as_service "$BIN" --database "$DB" ops profile-status; }
ops_prune() {
  echo "Old release folders (each holds its own virtual environment). Dry run: nothing is deleted unless you add --apply."
  sudo "$BIN" ops prune-releases ${1:+"$1"}
}

before_deploy() {
  bad=0
  hour=$(TZ=Europe/London date +%H%M)
  if [ "$hour" -ge 2330 ] || [ "$hour" -lt 0530 ]; then echo "NOT OK: it is inside the cheap window (23:30 to 05:30)"; bad=1; else echo "ok: outside the cheap window ($hour)"; fi
  for s in $SERVICES; do [ "$(systemctl is-active solarbank-optimiser-$s)" = active ] || { echo "NOT OK: $s is not active"; bad=1; }; done
  free=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
  [ "$free" -ge 20 ] && echo "ok: ${free} GB free" || { echo "NOT OK: only ${free} GB free"; bad=1; }
  newest=$(ls -t "$STATE"/backups/before-*.sqlite3 2>/dev/null | head -1)
  if [ -n "$newest" ]; then echo "ok: newest release backup $(( ($(date +%s) - $(stat -c %Y "$newest")) / 3600 )) hours old"; else echo "note: no release backup yet (the deploy takes one)"; fi
  [ $bad = 0 ] && echo "READY to deploy" || echo "NOT READY"
  exit $bad
}

case "${1:-}" in
  status) status;;
  overnight) overnight;;
  logs) logs "${2:-}";;
  telemetry) telemetry;;
  commission) commission;;
  backup-drill) backup_drill;;
  backfill) backfill;;
  db-report) db_report;;
  db-clean) db_clean;;
  db-clean-apply) db_clean_apply;;
  writes) writes "${2:-}";;
  logged-on) set_mode logged;;
  logged-off) set_mode shadow;;
  verify-clean) verify_clean;;
  io) ops_io "${2:-}";;
  memory) ops_memory;;
  profile-status) ops_profile;;
  prune-releases) ops_prune "${2:-}";;
  before-deploy) before_deploy;;
  *) echo "usage: sudo bash pi-tools.sh status | overnight | logs <service> | telemetry | commission | backup-drill | backfill | db-report | db-clean | db-clean-apply | io [seconds] | memory | profile-status | prune-releases [--apply] | verify-clean | writes [n] | logged-on | logged-off | before-deploy"; exit 2;;
esac
