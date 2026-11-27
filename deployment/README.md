# Pi shadow installation

The initial installation uses `/opt/solarbank-cost-optimiser/releases/0.4.5`
with a `current` link. Persistent private state lives in
`/var/lib/solarbank-cost-optimiser`, owned by the unprivileged
`solarbank-optimiser` service account. Code becomes read-only to that account.
*As first installed (0.4.5):* the existing monitor was the only equipment poller and
the new collector read `http://127.0.0.1:8080/api/readings` on the Pi.
*Since the 28 October cutover (release 0.5.11):* the controller owns the smart meter
and shares its readings with the collector through
`/run/solarbank-cost-optimiser/meter.json`, the collector reads the other devices
directly, and the original monitor no longer reads the meter (OPERATIONS.md,
"Retiring the original energy monitor").

Five separate systemd services run the controller, website, collector,
providers and shadow target planner (the first installation had the last four).
Each has its own restart policy and resource limits. The collector's network
access is restricted to localhost and the Solarbank and Shelly addresses (only
the controller also reaches the meter); the planner has no network access.
The website accepts the current home LAN and Tailscale ranges;
no public proxy, router or port-forward setting is installed. Providers contact
only the two explicitly supported HTTPS hosts. Root is used only for installation
and the storage guard, never for the application services.

The Pi shadow worker uses its own isolated EMHASS environment at
`/opt/solarbank-cost-optimiser/solver-venv`, built from `solver-requirements.lock`
(`scripts/bootstrap-solver.sh /opt/solarbank-cost-optimiser/solver-venv`, run as root
from a release directory). It lives outside the release folders so it survives
updates, and nothing depends on the original energy monitor's folder any more. The
adapter accepts explicitly tested 0.18.4 and 0.18.5 versions and records the actual
engine version. Native results are
independently checked. Irregular exact interval grids remain explicitly
unsupported by the native comparison, while target-policy analysis continues.

The owner requested ordinary HTTP, matching the other Pi applications, at
`http://solarbank-pi.local:8091/`. There is no self-signed certificate or HTTPS
redirect on this port. This removes the certificate warning page; browsers can
still label HTTP as “Not secure” because LAN traffic is not encrypted. Login is
disabled for this private installation, and source addresses are restricted to
the home LAN, loopback and private Tailscale ranges. The old password file is
retained privately as the CSRF signing seed; other installations still default
to password access. Existing certificate files are retained only for rollback.

Heimdall opens `http://solarbank-pi.local:8091/?access=local`. The query also gives
browsers a fresh URL if they cached the previous permanent HTTPS redirect.
Replace old HTTPS bookmarks with this HTTP link; do not disable browser warnings.

## Octopus key

The current key remains in `/var/lib/solarbank-cost-optimiser/.env` with mode
600. The parser reads just `OCTOPUS_API_KEY` and never evaluates shell expressions.
The file is not loaded into the service environment or returned by the website.
An older private `octopus-api-key` file is supported for read-only migration.
The local Mac equivalent is `runtime/.env`; `.env.example` contains no key.

Remote HTTP clients cannot submit a new API key through the website. To replace
it, connect over SSH and edit the private file on the Pi:

```sh
ssh -t solarbank-pi 'sudo -u solarbank-optimiser nano /var/lib/solarbank-cost-optimiser/.env'
```

Keep the line in the form `OCTOPUS_API_KEY=your-key`, preserve any other entries,
and do not paste the key into chat, command arguments or Git. Secure HTTPS and
localhost installations can still use the website key-entry form.

After saving the key, click **Connect Octopus (read only)**. The provider worker
checks authentication and lists account-linked EV/charger devices. One matching
or uniquely available device is selected; multiple devices require a choice.
Only then is read-only smart-slot retrieval enabled. Household cheap-rate
eligibility is a separate setting, and is never inferred from login success.
No vehicle, charger or battery configuration is sent to Octopus.

## Operation and recovery

```sh
ssh solarbank-pi 'systemctl status solarbank-optimiser-{control,web,collector,providers,shadow}.service --no-pager'
ssh solarbank-pi 'sudo journalctl -u solarbank-optimiser-shadow -n 30 --no-pager'
ssh solarbank-pi 'sudo journalctl -u solarbank-optimiser-control -n 30 --no-pager'
ssh solarbank-pi 'sudo systemctl restart solarbank-optimiser-shadow'
```

The services are enabled for boot. Restarting does not change stored mode,
demand, prices, credentials or history. The planner publishes nothing when
fresh SOC, eligible demand history, weather or prices are missing. A real
complete Normal-use day is still required; a partial first day is not training
history. The aggregation counts recorder coverage, not verified device refresh.

Use `sh scripts/deploy-pi-shadow.sh` for a subsequent compatible release from
the Mac. It runs local and Pi tests before switching, preserves private Pi state,
and saves a database backup. It does not promote the operating mode or supply
an automatic database rollback across schema changes. Keep the previous release
and its matching backup until the replacement has passed checks. The initial
Pi install and future redeploy scripts do not constitute full Phase 6 acceptance:
a physical reboot, automatic health-failure rollback and destructive recovery
exercise have not been demonstrated.

When the Mac is not available, run `sh deploy-on-pi.sh <branch>` on the Pi itself (`scripts/deploy-on-pi.sh`). It downloads the branch from GitHub with a read-only

The activation gives the data folder to the service user with `scripts/own-state-dir.sh`, not `chown -R`: SQLite deletes its `-wal` and `-shm` files when the last connection closes, and `chown -R` failed a deploy (GitHub issue 86, 12 November) when one vanished mid-walk. The script changes only files with the wrong owner, never follows a symbolic link, ignores files that vanish, and fails (naming up to five files) only if something that still exists has the wrong owner. If a deploy ever stops with "Could not give these files to solarbank-optimiser", that is a real fault, not this race.
token that is kept in memory only, then does the same steps in the same order: stage, build the environment, run the suite on the Pi, back up, stop, activate,
check health. Nothing is switched unless the suite passed on the Pi.
The token can instead be kept in `~/.solarbank-deploy-token` (mode 600, which the script insists on); regenerate it on GitHub when it expires.

Raw response retention is checked each minute, with a 64 MiB target budget.
Covered demand/PV minutes, typed readings, plans, settings and histories are
retained. Provider forecasts are archived at most every four minutes unless
an input revision changes. Long-term compaction is still pending. An hourly
storage guard raises an email alert, and never stops a service, if the data folder passes 20 GiB, free space is low, the folder grows abnormally or the fixed-size telemetry
store changes size (WP19, reduced; see OPERATIONS.md). There is no hidden calendar expiry.

## Lightweight code update

Release 0.4.5 installs its pure Python wheel under its own `app` folder and
reuses the unchanged, read-only 0.4.1 dependency environment through a `.venv`
link. Systemd sets `PYTHONPATH` to the current release's `app`, so website,
worker children and package metadata select the new version. Retain 0.4.1
while this dependency link is used. No existing monitor environment is modified.
The regular deploy script can still build a fresh isolated environment when
dependencies change; a missing `app` folder then falls back to that environment's
installed application normally.

Home Assistant's one container was stopped at the owner's explicit request.
Its data and configuration were retained. Restart only on request with
`sudo docker start homeassistant`. Heimdall and the energy monitor stayed running.
The previous Tailscale Funnel was explicitly replaced, with owner approval, by
private Tailscale Serve to Heimdall on port 80. The separate 8503 application
stays running. The saved pre-change route is in the Heimdall backup directory.


The Nginx frontend serves HTTP on port 8091. The website backend listens on
loopback 8092. Original Nginx routes remain unchanged. The frontend forwards
the original Host and request scheme, while same-origin/CSRF validation remains
enforced. Existing HTTPS bookmarks must be changed to HTTP.

The daily consumption reference is an owner-supplied benchmark only. The
provider worker compares complete Normal forecast days and leaves missing
hourly demand unavailable. Normal, High and Away planning policies are unchanged.


## Disk activity (deployed 26 October 2026)

The collector, planner and website now share a small RAM-backed live-readings file in
`/run/solarbank-cost-optimiser/` (created by `RuntimeDirectory=` in the units, so reinstall the
units and run `systemctl daemon-reload`). The collector writes one summary row per minute to the
database by default. Before deploying, take a consistent backup:

```sh
ssh solarbank-pi 'sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 backup /var/lib/solarbank-cost-optimiser/backups/before-diskwrites-$(date +%Y%m%d%H%M).sqlite3'
```

Check afterwards: dashboard readings update every second, `ls -l /run/solarbank-cost-optimiser/`
shows `live.json`, and `sudo iotop -oPa -d 10` shows far fewer writes. To roll back, install the
previous release and restart the services; the database is compatible in both directions.

## Dependencies and the telemetry store (planned, revised approach of 2 November 2026)

Not installed yet. When the work in [POST_SHADOW_PLAN.md](../POST_SHADOW_PLAN.md) ships:

* `transitions` (controller state machines), `whisper` (telemetry store) and, for the test gate, Hypothesis are pinned in `requirements.lock`. `scripts/install-pi-shadow.sh` installs that file into a fresh `.venv` for each staged release and runs the suite on the Pi, so a release that adds a dependency cannot use the lightweight code update above.
* The telemetry store lives in `/var/lib/solarbank-cost-optimiser/telemetry/` (one file per metric, plus a count file for each metric whose coverage matters; fixed size, about 125 MB in total). The collector is its only writer, and its unit already allows writes under `/var/lib/solarbank-cost-optimiser`. The controller does not use it.
* Backups include the telemetry folder, copied file by file under the Whisper lock.
* Rolling back after the telemetry switch: the SQLite minute rows keep being written until a later release, and `switch-release.sh` will then refuse a release older than the switch release (POST_SHADOW_PLAN.md, RC1).
* The storage guard (issue #8) now alerts and never stops services; the emergency purge of SQLite detail is deferred until the disk is tight, and the telemetry folder is fixed-size.
