"""Local operations. No implicit network access and no equipment commands."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
import secrets
import signal
import threading

from .history import import_household, verify_manifest
from .store import StorageTuning, Store

RUNTIME_LIVE_FILE = Path("/run/solarbank-cost-optimiser/live.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Solarbank isolated observation foundation")
    parser.add_argument("--database", type=Path, default=Path("runtime/controller.sqlite3"))
    parser.add_argument("--synchronous", choices=("normal", "full"), default="normal",
                        help="SQLite durability. NORMAL (default) is safe in WAL mode; FULL also syncs every commit")
    parser.add_argument("--wal-autocheckpoint", type=int, default=4000, metavar="PAGES",
                        help="Copy the write-ahead log into the main file after this many 4 KB pages")
    parser.add_argument("--live-file", type=Path, default=None,
                        help=f"RAM-backed latest-readings file shared by collector, shadow and dashboard (default {RUNTIME_LIVE_FILE} if its directory exists)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    check = sub.add_parser("verify-history")
    check.add_argument("--root", type=Path, default=Path.cwd())
    importer = sub.add_parser("import-history")
    importer.add_argument("--root", type=Path, default=Path.cwd())
    backup = sub.add_parser("backup")
    backup.add_argument("destination", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("source", type=Path)
    collect = sub.add_parser("collect", help="Observe the existing monitor without equipment connections")
    collect.add_argument("--endpoint", default="http://127.0.0.1:8080/api/readings")
    collect.add_argument("--count", type=int, default=300, help="Bounded development capture; default 300 requests")
    collect.add_argument("--interval", type=float, default=1)
    collect.add_argument("--flush-interval", type=float, default=60,
                         help="Seconds between database commits (default 60; 0 commits every reading). Without a live file it is capped at 2 so the planner still sees fresh readings")
    collect.add_argument("--record-seconds", type=int, choices=(1, 60), default=60,
                         help="Stored resolution: 60 (default) keeps one summary row per minute; 1 stores every second. Needs the live file; otherwise 1 is used")
    collect.add_argument("--meter-feed", default=None, help="read the meter from the controller's RAM file (the controller owns the meter; "
                         "it allows one Modbus client) instead of connecting to it")
    collect.add_argument("--source", choices=("monitor", "direct"), default="monitor",
                         help="monitor (default): readings from the existing monitor's page. direct: read the Solarbank, smart meter and Shelly "
                              "directly (read-only). The meter allows one Modbus client, so direct meter reads need the monitor stopped")
    collect.add_argument("--direct-devices", default="none", metavar="LIST",
                         help="Mixed mode: read only these devices directly, the rest from the monitor. Comma list of solarbank,meter,shelly "
                              "(for example --direct-devices shelly,solarbank). Ignored with --source direct")
    collect.add_argument("--solarbank-host", default=None, help="overrides devices.conf and SOLARBANK_HOST")
    collect.add_argument("--settings-log", default=None, help="C1: log the Solarbank's mode, capability and limit registers here when they change (read-only, through the collector's own connection)")
    collect.add_argument("--meter-host", default=None, help="overrides devices.conf and SOLARBANK_METER_HOST")
    collect.add_argument("--shelly-host", default=None, help="overrides devices.conf and SOLARBANK_SHELLY_HOST")
    collect.add_argument("--continuous", action="store_true", help="Run until stopped by the service manager")
    collect.add_argument("--telemetry-dir", default=None, help="the telemetry folder; omit it. It is always next to the database (the collector and every reader use the same one) and a different value is refused")
    providers = sub.add_parser("providers", help="Separate read-only provider and forecast worker")
    providers.add_argument("--count", type=int, default=1, help="Bounded development run, at five-second intervals")
    providers.add_argument("--continuous", action="store_true")
    providers.add_argument("--dispatch-feed", default=str(RUNTIME_LIVE_FILE.with_name("tariff.json")),
                           help="where the Octopus SMART dispatches are shared for SMARTSLOT (GitHub issue 67); the controller reads this")
    shadow = sub.add_parser("shadow", help="Run independent shadow planning; no equipment commands")
    shadow.add_argument("--count", type=int, default=1)
    shadow.add_argument("--solver-python", type=Path, default=Path(".emhass-venv/bin/python"))
    shadow.add_argument("--without-emhass", action="store_true", help="Target-policy checks only; no native dispatch comparison")
    shadow.add_argument("--continuous", action="store_true")
    compare = sub.add_parser("compare", help="Compare the direct readers with the existing monitor (run / report); read-only")
    compare.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("heartbeat", help="GitHub 89: ping the outside dead-man's-switch checks (the heartbeat timer runs this every five minutes); read-only; needs healthchecks.json in the data folder")
    alert = sub.add_parser("alert", help="WP5: the one alert path (email through Resend): --run (checks and sends; the timer runs this), --test, or --unit-failed UNIT")
    alert.add_argument("--run", action="store_true", help="check the system, then send any alert not sent within the cooldown")
    alert.add_argument("--test", action="store_true", help="send a test email now and say whether it worked")
    alert.add_argument("--unit-failed", default=None, help="a unit that entered the failed state (systemd OnFailure passes its name)")
    alert.add_argument("--alert-dir", type=Path, default=Path("/run/solarbank-cost-optimiser/alerts"))
    sub.add_parser("octopus-key", help="Enter an optional API key privately; never pass it as a shell argument")
    records = sub.add_parser("daily-record", help="Score finished days: savings against no solar/battery and the lost-solar measure; no equipment commands")
    records.add_argument("--days", type=int, default=3, help="how many finished days back to score if they have no record (default 3)")
    probe = sub.add_parser("probe-settings", help="C1: read-only look at the Solarbank's mode, capability and charge-limit registers; never writes")
    probe.add_argument("--solarbank-host", default=None, help="overrides devices.conf and SOLARBANK_HOST")
    probe.add_argument("--watch", type=float, default=None, help="keep reading every N seconds (10 to 3600) and log every change to --log")
    probe.add_argument("--log", default=None, help="JSON-lines file for --watch (default: device-settings.jsonl next to the database)")
    check = sub.add_parser("validation-report", help="B3: compare what the Anker did each minute with what the controller would have asked; lists every difference (read-only)")
    check.add_argument("--days", type=int, default=7, help="how many finished days to report (default 7)")
    tel = sub.add_parser("telemetry", help="WP23b: move the SQLite one-minute rows into the telemetry files (backfill) and check the two agree (check); never deletes anything")
    tel.add_argument("action", choices=("backfill", "check"))
    tel.add_argument("--telemetry-dir", type=Path, default=None, help="default: the telemetry folder next to the database")
    tel.add_argument("--dry-run", action="store_true", help="backfill: report what would be written")
    com = sub.add_parser("commission", help="RC4: record commissioning tests passed on the real device (root only) and show what keeps the controller from sending")
    com.add_argument("action", choices=("status", "pass", "run"))
    com.add_argument("test", nargs="?", help="pass: the test name")
    com.add_argument("--note", default="")
    com.add_argument("--record", type=Path, default=Path("/etc/solarbank-cost-optimiser/commissioning.json"))
    com.add_argument("--switch", type=Path, default=Path("/etc/solarbank-cost-optimiser/write-switch"))
    com.add_argument("--mode-file", type=Path, default=Path("/etc/solarbank-cost-optimiser/control-mode"))
    com.add_argument("--solarbank-host", default=None)
    com.add_argument("--confirm", action="store_true", help="run: I am at the battery and the controller is stopped")
    ctl = sub.add_parser("control", help="`reset`: ask a controller stopped in HANDS_OFF to start again (it still runs its start-up checks first; root only). `writes`: summarise the register write journal (every write the controller made or would make); exits 1 if any write was actually sent")
    ctl.add_argument("action", choices=("reset", "writes"))
    ctl.add_argument("--journal", type=Path, default=Path("/var/lib/solarbank-cost-optimiser/control-writes.jsonl"))
    ctl.add_argument("--last", type=int, default=8, help="writes: how many of the newest entries to show")
    ctl.add_argument("--request", type=Path, default=Path("/etc/solarbank-cost-optimiser/control-reset-request.json"))
    off = sub.add_parser("offsite-backup", help="WP8: build a verified archive of the database and telemetry files and copy it to the remote with rclone; the outcome is in offsite-status.json")
    off.add_argument("--remote", default=None, help="rclone destination, for example gdrive:solarbank-backups (default: REMOTE in /etc/solarbank-cost-optimiser/offsite.conf)")
    off.add_argument("--config", type=Path, default=Path("/etc/solarbank-cost-optimiser/offsite.conf"), help="the file that names the remote (REMOTE=...)")
    off.add_argument("--keep", type=int, default=3)
    off.add_argument("--local-only", action="store_true", help="build and verify the archive but do not upload (a restore drill)")
    dev = sub.add_parser("devices", help="WP21: show where the devices are and whether each is recognised; `accept NAME` forgets that device's commissioned identity so the next device to answer there becomes the commissioned one (use after replacing a device on purpose)")
    dev.add_argument("action", choices=("show", "accept", "confirm"))
    dev.add_argument("name", nargs="?", choices=("solarbank", "meter", "shelly"))
    dbc = sub.add_parser("db", help="`report`: what the database is made of (read-only). `clean`: remove what nothing reads any more after a verified backup and compact the file; a dry run unless --apply")
    dbc.add_argument("action", choices=("report", "clean", "verify-backup"))
    dbc.add_argument("--backup", type=Path, default=None, help="verify-backup: the database backup to compare with the telemetry files (read-only)")
    dbc.add_argument("--apply", action="store_true", help="clean: really do it (the controller's other services keep running; run it in the quiet hours)")
    dbc.add_argument("--backup-dir", type=Path, default=None, help="default: backups next to the database")
    ops = sub.add_parser("ops", help="read-only operating measurements: `io` (disk writes per service), `memory` (per service), `profile-status` (where the dashboard's status page spends its time); `prune-releases` lists old release folders and deletes them only with --apply")
    ops.add_argument("action", choices=("io", "memory", "profile-status", "prune-releases"))
    ops.add_argument("--seconds", type=float, default=120.0, help="io: how long to sample")
    ops.add_argument("--apply", action="store_true", help="prune-releases: really delete")
    ops.add_argument("--root", type=Path, default=Path("/opt/solarbank-cost-optimiser"))
    ops.add_argument("--state", type=Path, default=Path("/var/lib/solarbank-cost-optimiser"))
    purge = sub.add_parser("retention", help="Purge old detail (archived plan/forecast snapshots and one-minute rows) once the half-hour record for the day exists")
    purge.add_argument("--keep-days", type=int, default=7, help="days of detail to keep (not less than 7; the planner needs the last seven)")
    purge.add_argument("--issuance-hours", type=float, default=None, help="keep archived snapshots only this many hours (default: same as --keep-days)")
    purge.add_argument("--dry-run", action="store_true", help="say what would be removed, remove nothing")
    start = sub.add_parser("savings-start", help="Set the day the optimiser went live: savings are counted from this day")
    start.add_argument("date", nargs="?", help="YYYY-MM-DD (London); omit with --clear to stop counting")
    start.add_argument("--clear", action="store_true")
    review = sub.add_parser("shadow-review", help="Review completed shadow horizons without claiming measured savings")
    review.add_argument("--date", required=True, help="Completed London date containing horizon ends")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8091)
    serve.add_argument("--password-file", type=Path, default=Path("runtime/dashboard-password"))
    serve.add_argument("--ssl-certfile", type=Path)
    serve.add_argument("--ssl-keyfile", type=Path)
    serve.add_argument('--allowed-host',action='append',default=[],metavar='NAME',help='Extra host name the login-free dashboard answers to (repeatable); IP addresses and *.ts.net names are always accepted')
    serve.add_argument('--trusted-network',action='store_true',help='No login on the configured home LAN/loopback/private Tailscale ranges')
    args = parser.parse_args(argv)
    if args.command == "verify-history":
        print(json.dumps({"verified_files": verify_manifest(args.root)}))
        return
    if args.command == "control" and args.action == "writes":
        from .control_report import summarise_writes
        text, sent = summarise_writes(args.journal, args.last)
        print(text)
        if sent:
            raise SystemExit(1)
        return
    if args.command == "control":
        import uuid
        if os.geteuid() != 0:
            parser.error("resetting the controller needs root: run it with sudo")
        args.request.write_text(json.dumps({"id": uuid.uuid4().hex, "at": time.time()}))
        os.chmod(args.request, 0o644)
        print("Reset requested. The controller answers within a few seconds; look at `sudo bash pi-tools.sh status` (alerts) to see whether it took.")
        return
    if args.command == "commission":
        from . import live_gate
        from .control_loop import read_mode_file
        if args.action == "run":
            from . import commission_tests
            if os.geteuid() != 0:
                parser.error("running a commissioning test needs root: run it with sudo")
            if not args.test or args.test not in commission_tests.TESTS:
                parser.error("name the test: " + ", ".join(commission_tests.TESTS))
            if args.test in commission_tests.NEEDS_DEVICE and not args.confirm:
                parser.error("this test writes to the battery: stop the controller (systemctl stop solarbank-optimiser-control), be at the battery, and add --confirm")
            client = writer = reader = None
            if args.test in commission_tests.NEEDS_DEVICE:
                from .devices.modbus import ReadOnlyModbusTcp
                from .devices.modbus_write import GuardedModbusWriter
                from .devices.settings_probe import read_settings as reader
                from .devices_config import DeviceConfigError, require, resolve
                try:
                    host = require(resolve(overrides={"solarbank": args.solarbank_host}), ("solarbank",))["solarbank"]
                except DeviceConfigError as problem:
                    parser.error(str(problem))
                client, writer = ReadOnlyModbusTcp(host), GuardedModbusWriter(host, extra_modes=commission_tests.EXTRA_MODES.get(args.test, ()))
            try:
                note = commission_tests.run(args.test, reader, client, writer)
            except commission_tests.TestFailed as failure:
                print(f"FAILED {args.test}: {failure}")
                raise SystemExit(6)
            finally:
                for closable in (client, writer):
                    if closable is not None:
                        closable.close()
            print(f"PASSED {args.test}: {note}")
            if args.test in live_gate.REQUIRED_TESTS:
                live_gate.record_pass(args.record, args.test, note=note)
            else:
                print("(An exploratory test: not one of the commissioning tests, so nothing is written to the commissioning record.)")
        elif args.action == "pass":
            if os.geteuid() != 0:
                parser.error("recording a commissioning test needs root: run it with sudo")
            if not args.test:
                parser.error("name the test: " + ", ".join(live_gate.REQUIRED_TESTS))
            live_gate.record_pass(args.record, args.test, note=args.note)
        print(json.dumps(live_gate.status(args.record, args.switch, read_mode_file(args.mode_file)), indent=1))
        return
    if args.command == "heartbeat":                       # before the store is built: this touches no database and no device
        from . import external_heartbeat
        raise SystemExit(external_heartbeat.main(state_dir=args.database.parent))
    if args.command == "compare":
        from .comparison import main as compare_main
        raise SystemExit(compare_main(args.rest))
    if args.command == "ops" and args.action in ("io", "memory", "prune-releases"):
        # These read /proc and the release folders only: they must not touch (or create) the database folder.
        from . import ops_report
        if args.action == "prune-releases":
            if args.apply and os.geteuid() != 0:
                parser.error("deleting releases needs root: run it with sudo")
            print(ops_report.format_release_plan(*ops_report.prune_releases(args.root, args.state, apply=args.apply), args.apply))
            return
        pids = ops_report.service_pids()
        if not pids:
            print("No Solarbank service is running here.")
        elif args.action == "io":
            print(ops_report.format_io(ops_report.io_rates(pids, args.seconds), args.seconds))
        else:
            print(ops_report.format_memory(ops_report.memory_figures(pids)))
        return
    live = args.live_file or (RUNTIME_LIVE_FILE if RUNTIME_LIVE_FILE.parent.is_dir() else None)
    store = Store(args.database, StorageTuning(args.synchronous.upper(), args.wal_autocheckpoint), live)
    if args.command == "restore":
        store.restore(args.source)
        print("Backup restored to a new database. Equipment writes remain disabled.")
        return
    store.initialise()
    if args.command == "init":
        print("Controller database ready. Equipment writes disabled.")
    elif args.command == "import-history":
        print(json.dumps({"imported": import_household(store, args.root), "coverage": "unverified historical minute means"}))
    elif args.command == "backup":
        print(store.backup(args.destination))
    elif args.command == "collect":
        from .collector import Collector, CollectorConfig
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        from .acquirer import DEVICES, DirectAcquirer, DirectConfig, parse_devices
        try:
            devices = DEVICES if args.source == "direct" else parse_devices(args.direct_devices)
        except ValueError as problem:
            parser.error(str(problem))
        acquirer = None
        if devices:
            from .devices_config import DeviceConfigError, resolve
            try:
                hosts = resolve(overrides={"solarbank": args.solarbank_host, "meter": args.meter_host, "shelly": args.shelly_host})
            except DeviceConfigError as problem:
                parser.error(str(problem))
            acquirer = DirectAcquirer(DirectConfig(devices=devices, solarbank_host=hosts["solarbank"],
                                                   meter_host=hosts["meter"], shelly_host=hosts["shelly"],
                                                   meter_feed=args.meter_feed, settings_log=args.settings_log,
                                                   device_book=str(Path(args.database).parent / "device-book.json")))
        if args.telemetry_dir and Path(args.telemetry_dir).resolve() != Path(store.telemetry_dir).resolve():
            parser.error(f"--telemetry-dir must be the folder every reader uses, {store.telemetry_dir} (next to the database); there is one telemetry directory")
        worker = Collector(store, CollectorConfig(endpoint=args.endpoint, interval_seconds=args.interval,
                                              record_seconds=args.record_seconds if live else 1, background_flush=args.continuous,
                                              flush_interval_seconds=args.flush_interval if live else min(args.flush_interval, 2),
                                              telemetry_dir=args.telemetry_dir),
                           acquirer=acquirer)
        if not 1 <= args.count <= 3600:
            parser.error("Use 1–3600 requests for a bounded development capture")
        print("Observation-only collector started. Equipment writes absent. Device identity unverified. "
              + ("Direct read-only readers: " + ", ".join(devices) + "." if devices else "Readings from the existing monitor."))
        # The device readers start only once this process owns the installation (a second collector is refused first).
        worker.run(stop, None if args.continuous else args.count,
                   before=acquirer.start if acquirer is not None else None,
                   after=acquirer.stop if acquirer is not None else None)
    elif args.command == "alert":
        from . import alerts
        state = args.database.parent
        sender = alerts.ResendSender(state / "resend-key", state / "alert.json")
        if args.test:
            try:
                sender.send("Test alert", "This is a test from the Solarbank optimiser. If you can read it, alert email works.")
                print("Test alert sent.")
            except RuntimeError as problem:
                print(f"Test alert NOT sent: {problem}")
                raise SystemExit(1)
            return
        if args.unit_failed:
            alerts.raise_alert(args.alert_dir, "unit-failed-" + args.unit_failed, f"{args.unit_failed} failed",
                               f"systemd reports that {args.unit_failed} entered the failed state and will not be restarted automatically (for example the original monitor still owns the meter, or another controller holds the lock). Look at: journalctl -u {args.unit_failed} --since '1 hour ago'")
        if args.run or args.unit_failed:
            if args.run:
                alerts.check(args.alert_dir, state / "alert-seen.json", args.alert_dir.parent, state)
            sent, failed = alerts.process(args.alert_dir, state / "alert-sent.json", sender)
            print(json.dumps({"sent": sent, "failed": failed}))
        else:
            parser.error("Use --run, --test or --unit-failed")
    elif args.command == "octopus-key":
        import getpass
        from .services import SecretFile
        SecretFile(args.database.parent / ".env").write(getpass.getpass("Octopus API key (hidden): "))
        print("API key saved privately. No account request has been made.")
    elif args.command == "providers":
        from .services import ProviderWorker
        if not 1 <= args.count <= 3600:
            parser.error("Use 1–3600 iterations for a bounded development run")
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        ProviderWorker(store, dispatch_feed_path=args.dispatch_feed).run(stop, None if args.continuous else args.count)
        print("Provider/forecast run complete. Equipment writes remain disabled.")
    elif args.command == "shadow":
        from .shadow import ShadowWorker
        from .emhass_adapter import EmhassReference
        if not 1 <= args.count <= 1440:
            parser.error("Use 1–1440 bounded shadow runs")
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        native = None
        if not args.without_emhass:
            if not args.solver_python.exists():
                parser.error("Install the isolated solver with scripts/bootstrap-solver.sh or use --without-emhass")
            # Resolving a venv's Python symlink bypasses that virtual environment.
            native = EmhassReference(args.solver_python.absolute())
        plan_file = RUNTIME_LIVE_FILE.with_name("plan.json") if RUNTIME_LIVE_FILE.parent.is_dir() else None     # the controller reads this
        ShadowWorker(store, emhass=native, controller_plan_path=plan_file).run(stop, None if args.continuous else args.count)
        print("Shadow run complete. No equipment commands sent; see dashboard for readiness.")
    elif args.command == "daily-record":
        from .daily_record import record_finished_days, snapshot_demand_forecast, snapshot_forecasts
        from .models import Clock
        if not 1 <= args.days <= 60:
            parser.error("Use 1 to 60 days")
        now = Clock().utc()
        saved = snapshot_forecasts(store, now) + snapshot_demand_forecast(store, now)
        done = record_finished_days(store, now, lookback=args.days)
        print("Forecast saved for:", ", ".join(map(str, saved)) or "none needed")
        print("Days scored:", ", ".join(map(str, done)) or "none (all finished days already have a record)")
    elif args.command == "probe-settings":
        from .devices.modbus import ReadOnlyModbusTcp
        from .devices.settings_probe import SettingsLog, read_settings
        from .devices_config import DeviceConfigError, require, resolve
        try:
            host = require(resolve(overrides={"solarbank": args.solarbank_host}), ("solarbank",))["solarbank"]
        except DeviceConfigError as problem:
            parser.error(str(problem))
        client = ReadOnlyModbusTcp(host)
        try:
            if args.watch is None:
                print(json.dumps(read_settings(client), indent=2))
            else:
                if not 10 <= args.watch <= 3600:
                    parser.error("--watch takes 10 to 3600 seconds")
                log = SettingsLog(args.log or Path(args.database).parent / "device-settings.jsonl")
                print(f"Reading the Solarbank's settings every {args.watch:g} s; changes go to {log.path}. Nothing is written to the device.", flush=True)
                while True:
                    snapshot = read_settings(client)
                    event = log.record(snapshot)
                    if event == "changed":
                        print(time.strftime("%H:%M:%S"), "settings changed:", json.dumps(snapshot["decoded"]), flush=True)
                    time.sleep(args.watch)
        finally:
            client.close()
    elif args.command == "validation-report":
        from datetime import datetime
        from .energy import LONDON
        from .models import Clock
        from .validation import KINDS, default_log_paths, record_validation_days, report
        now = Clock().utc()
        done = record_validation_days(store, now, default_log_paths(store), lookback=args.days)
        print("Days scored:", ", ".join(map(str, done)) or "none (all finished days already have a record)")
        data = report(store, datetime.fromtimestamp(now, LONDON).date(), days=args.days)
        print(f"Agreement over {len(data['days'])} day(s): {data['agreement_pct']}% of {data['minutes_compared']} minutes" if data["minutes_compared"] else "No minutes compared yet.")
        for day in data["days"]:
            print(f"  {day['date']}: {day['agreement_pct']}% agree ({day['disagreeing_minutes']} min differ, {day['minutes_without_controller_record']} min with no controller record)")
        for d in data["differences"]:
            print(f"  {d['date']} {datetime.fromtimestamp(d['start'], LONDON):%H:%M}-{datetime.fromtimestamp(d['end'], LONDON):%H:%M} {d['minutes']} min: {KINDS[d['kind']]} (Anker: {d['actual']}, battery {d['mean_battery_kw']:+.2f} kW)")
    elif args.command == "telemetry":
        import time as _time
        from .telemetry_backfill import agreement, backfill
        now = _time.time()
        args.telemetry_dir = args.telemetry_dir or store.telemetry_dir
        if args.action == "backfill":
            done = backfill(store, args.telemetry_dir, now, dry_run=args.dry_run)
            print(json.dumps({"backfill": done, "dry_run": args.dry_run}, indent=1))
        result = agreement(store, args.telemetry_dir, now) if (args.action == "check" or not args.dry_run) else None
        if result is not None:
            print(json.dumps(result, indent=1, default=str))
            if not result["ok"]:
                raise SystemExit(5)
    elif args.command == "offsite-backup":
        from . import offsite
        remote = args.remote
        if not remote:
            try:
                remote = next(line.split("=", 1)[1].strip() for line in args.config.read_text().splitlines() if line.startswith("REMOTE="))
            except (OSError, StopIteration):
                print("Off-site backup is not set up (no REMOTE in /etc/solarbank-cost-optimiser/offsite.conf): nothing to do. See OPERATIONS.md, 'Off-site backup to Google Drive'.")
                return                      # the nightly timer must not fail, or email an alert, before the owner has set the remote up
        result = offsite.run(store, store.telemetry_dir, store.path.parent, remote, keep=args.keep, upload=not args.local_only)
        print(json.dumps(result, indent=1))
        if result.get("ok") and result.get("uploaded"):
            from . import external_heartbeat
            external_heartbeat.ping_backup(store.path.parent)               # a verified upload: tell the outside service (does nothing unless a backup check is set up)
        if not result["ok"]:
            raise SystemExit(7)
    elif args.command == "devices":
        from .device_book import DeviceBook
        from .devices_config import resolve
        book = DeviceBook(Path(args.database).parent / "device-book.json")
        if args.action == "accept":
            if not args.name:
                parser.error("name the device: solarbank, meter or shelly")
            book.forget_identity(args.name)
            print(f"The commissioned identity of the {args.name} is forgotten; the next device that answers at its address will be learned as the commissioned one.")
        if args.action == "confirm":
            if not args.name:
                parser.error("name the device: solarbank, meter or shelly")
            learned = book.confirm_identity(args.name)
            print(f"Confirmed the {args.name}: {json.dumps(learned)}" if learned else f"The {args.name} has not been recognised yet: wait until the services have read it, then confirm.")
        data, configured = book.read(), resolve()
        for name in ("solarbank", "meter", "shelly"):
            found = data["addresses"].get(name)
            print(f"{name:10s} configured {configured.get(name) or '-':16s} in use {book.effective_host(name, configured.get(name)) if configured.get(name) else '-':16s} "
                  f"{('confirmed by you' if book.is_confirmed(name) else 'recognised, NOT yet confirmed by you (sudo solarbank devices confirm ' + name + ')') if name in data['identities'] else 'not yet recognised'}" + (f"  moved (found {found['host']})" if found and found.get('superseded') == configured.get(name) else ""))
    elif args.command == "ops":
        from .ops_status import profile_status
        print(profile_status(store))
    elif args.command == "db":
        from . import db_clean
        if args.action == "report":
            print(json.dumps(db_clean.report(store), indent=1))
        elif args.action == "verify-backup":
            if not args.backup:
                parser.error("verify-backup needs --backup FILE")
            result = db_clean.verify_backup(args.backup, store.telemetry_dir, time.time())
            for metric, entry in result["metrics"].items():
                print(f"{metric:18s} compared {entry['compared']:6d}  missing {entry['missing']:4d}  differences {entry['mean_differences']:4d}" + (f"  explained {entry.get('explained_mean_differences', 0) + entry.get('explained_coverage_differences', 0)}" if entry.get("explained_mean_differences") or entry.get("explained_coverage_differences") else ""))
            print("RESULT:", "OK, the files hold what the backup held" if result["ok"] else "DIFFERENCES: see above; the backup still has the rows")
            if not result["ok"]:
                raise SystemExit(1)
        else:
            args.telemetry_dir = store.telemetry_dir
            try:
                result = db_clean.clean(store, store.telemetry_dir, time.time(), args.backup_dir or (Path(store.path).parent / "backups"), apply=args.apply)
            except (RuntimeError, ValueError) as problem:
                print(f"Not cleaned: {problem}", file=sys.stderr)
                raise SystemExit(5)
            print(json.dumps(result, indent=1, default=str))
            if not result["applied"]:
                print("Dry run: nothing was changed. Add --apply to do it.")
    elif args.command == "retention":
        from .models import Clock
        from .retention import purge_detail
        if args.issuance_hours is not None and args.issuance_hours < 1:
            parser.error("Keep archived snapshots for at least one hour")
        result = purge_detail(store, Clock().utc(), args.keep_days, args.issuance_hours, args.dry_run)
        print(("Would remove" if args.dry_run else "Removed") + f": {result['issuances_deleted']} archived snapshots and {result['observations_deleted']} one-minute rows older than {result['cutoff']}.")
        if result["days_kept_no_half_hour_record"]:
            print("Kept because their half-hour record is not on file yet:", ", ".join(result["days_kept_no_half_hour_record"]))
    elif args.command == "savings-start":
        from datetime import date as _date
        from .daily_record import set_savings_start
        from .models import Clock
        if args.clear == bool(args.date):
            parser.error("Give a date, or --clear")
        set_savings_start(store, None if args.clear else _date.fromisoformat(args.date), Clock().utc())
        print("Savings are counted from", "nowhere (cleared)" if args.clear else args.date)
    elif args.command == "shadow-review":
        from .shadow_review import review_completed
        from .models import Clock
        result = review_completed(store, args.date, Clock().utc())
        revision, _ = store.get_setting("shadow_review")
        store.set_setting("shadow_review", result, Clock().utc(), revision)
        print(json.dumps({"day": result["day"], "completed_shadow_plans": result["completed_shadow_plans"],
                          "measured_savings_gbp": None, "model_promoted": False}))
    elif args.command == "serve":
        import uvicorn
        from .web.app import WebConfig, create_app
        if not args.password_file.exists():
            args.password_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(args.password_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_urlsafe(24) + "\n")
            print(f"Dashboard user: solarbank. Password saved in {args.password_file}; it is not written to logs.")
        if args.password_file.stat().st_mode & 0o077:
            parser.error("Dashboard password file must be private: chmod 600")
        app = create_app(WebConfig(args.database, args.password_file.read_text().strip(), trusted_network=args.trusted_network,
                                  allowed_hosts=tuple(sorted({'solarbank-pi.local','solarbank-pi','localhost',*[h.lower() for h in args.allowed_host]})),
                                  control_state_path=RUNTIME_LIVE_FILE.with_name("control-loop.json") if RUNTIME_LIVE_FILE.parent.is_dir() else None), store=store)
        if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
            parser.error("Supply both TLS certificate and private key")
        uvicorn.run(app, host=args.host, port=args.port, access_log=False,
                    ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
                    ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None)


if __name__ == "__main__":
    main()
