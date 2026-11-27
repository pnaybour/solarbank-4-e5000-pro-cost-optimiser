# Running the local development application

**Note (2 November 2026):** this file has grown release by release. The current release is 0.5.36 (release sections below; planned work at the end). The first sections describe release 0.4.3, which was observation/history, scheduled settings, optional
provider/forecast work and shadow planning in separate bounded processes. It sends **no equipment commands**,
opens no additional equipment connections and installs no Pi services.
The existing monitor and Home Assistant have not been changed.
It is not yet the automatic controller described by the full specification.

## Start the application on this Mac

From a terminal in this project folder:

```sh
sh scripts/bootstrap.sh
.venv/bin/solarbank import-history
.venv/bin/solarbank serve
```

The first command installs pinned dependencies in this project's own environment,
builds the application, runs its offline tests and creates its database. It needs
internet access for the Python packages. Rerun it after source changes to install
the new application version. Python 3.11 or newer is required. These dependencies
have been tested on the Mac; Pi installation is a separate acceptance check.

Open [the local dashboard](http://127.0.0.1:8091). Login name: `solarbank`.
The generated password is in `runtime/dashboard-password`. Read that file locally;
do not put it in Git, URLs or logs. The server stops with Ctrl+C. Start it again
with the same `serve` command; records and settings survive.

The home page is the simple overview. Use **Settings** for household selections,
tariff/default prices, weather/Octopus connections and control requests. Use
**History** for charging records and CSV downloads. All three pages use the same
login, and navigation is available at the top of each page.

The current confirmed Intelligent Octopus Go prices are **34.96p/kWh peak** and
**6.60p/kWh off-peak**, supplied by the owner on 25 October 2026. The regular
23:30–05:30 London window is retained. Settings and calculations preserve these
values; no supplier quote or account API was needed for this update.
To change them, open Settings → Electricity tariff and save the current prices.
This does not enable API access or establish extra cheap slots.

The default listens only on this computer. Pi/LAN access and any reverse proxy
will be configured during the deployment phase, including authenticated access
and protected transport. Do not expose this development server to the internet.

## What works now

- Import the supplied household history after checking all 14 evidence files.
  Repeat imports are harmless. The 11,520 minute means preserve source values,
  source capture time and minute start time separately. Device update time,
  occupancy tags and full-minute coverage remain unknown.
- Save the three household settings and explicit provisional base-load/extra-use
  values. Save fallback prices and a London-time cheap window. These settings inform shadow planning; they cause no equipment action.
- Download observations as CSV using London calendar dates.
- Request Observe, Shadow or an Anker-app handover. The separate shadow worker can produce provisional advice in Observe or Shadow mode. Handover remains **pending**
  because the actual release/readback operation is uncommissioned.
- Run the independent offline tests, including charger sessions and target-policy
  comparisons. There is no equipment transport or writer.
- Capture current recorder readings, with original recorder retrieval times kept
  separate from our request times. View persistent charging sessions, daily kWh,
  coverage and a charging CSV. Sub-10 W standby contributes no charging energy.

Without a fresh bounded capture, the dashboard shows unavailable live readings.
A current website does not mean the battery is under this application's control.
Providers start off. The new forecast/provider features are described below;
Live discovery, the independent control service and automatic deployment
remain pending under the development plan.

## Capture observations during development

Keep the existing monitor running. In a second terminal:

```sh
.venv/bin/solarbank collect --endpoint http://solarbank-pi.local:8080/api/readings --count 300
```

This makes at most 300 requests, at a proposed one-second interval, then stops.
The maximum count is 3,600; Ctrl+C stops it earlier. No unattended collector
service is installed. Each acquisition has a two-second socket timeout and a
1 MiB response limit, without redirects. One collector may own each database.

The dashboard refreshes about every two seconds. When the original recorder
retrieval is more than five seconds old, live values disappear and charger
status becomes Unknown. Last confirmed charger state/time is labelled separately.
A new HTTP request never makes an old recorder sample fresh. Device measurement
update times and physical identity remain unverified.

The bridge supplies no garage current. That field stays unavailable; the app
never derives garage amps from watts or substitutes a Shelly circuit's current.
Neither the 45 A limiter nor EV discharge protection is active.

Raw responses stay in the ignored local database for at most seven days, with
an additional 64 MiB raw-response budget. Oldest raw responses expire first.
Typed observations, charger samples, coverage and charging energy are retained.
Long-term compaction/storage budgeting remains required before any permanent
collector service is enabled. Real telemetry and runtime files must not enter Git.

Charger confirmation/release/gap values are provisional (3 s / 60 s / 5 s; the release delay is shared with the controller's discharge inhibition, `ev_policy.py`),
not commissioned response criteria. Restart or invalid/gapped readings break
energy adjacency and mark affected sessions partial. Charging kWh means covered
charger-circuit energy, including charger losses, not energy added to a vehicle
battery. Recorder coverage does not establish independent device refresh.

## Forecasts, planned visits and optional Octopus access

The household form accepts a start and optional end in **London time**. Blank
start means now; the end returns to Normal even if the computer was off at the
end time. On the autumn clock change, choose first or second occurrence for a
repeated time. A skipped spring time is rejected. One selection is saved at a
time: a new future selection replaces the previous one, with Normal outside
its dates. Away needs your essential watts. **High demand requests a full battery
during the regular overnight window, without an extra-kWh estimate.** A future
daytime visit affects the preceding night. Both autumn clock occurrences are
included, and the shorter spring night is preserved. No peak charging is added.
Manual handover, EV protection and the 45 A limit still take priority. A short
night or limited charging power may prevent reaching 100%.

The 48-hour forecast includes a future visit before it starts. The Normal demand profile, also used as the baseline for High demand forecasting,
requires a recent complete Normal-use day with full coverage and reliable tags.
The supplied historical means and present temporary raw capture cannot prove
that, so **unavailable demand is expected** until the observation pipeline can
supply suitable minute records. Away can forecast without a Normal profile if
there is no return inside the horizon. The High demand overnight request remains visible separately when demand forecasts are unavailable. A forecast or requested target is not a battery command.

In Forecast and Octopus settings, choose the optional services and save. Weather
uses Open-Meteo and sends the configured site coordinates to that provider.
Public prices need your exact product/import-tariff codes, but no API key.
Keep confirmed fallback day/cheap prices, the London night window and optional
expiry independently. Missing/expired prices stay unavailable, rather than zero.
Negative published prices are supported. Costs here are estimates, not bills.

For optional smart slots, enter the account number and linked EV/charger ID.
Enter the API key in the password field **on this computer or through HTTPS**.
The browser never retrieves it again. It is saved in `runtime/.env` (as `OCTOPUS_API_KEY`)
with private permissions; it is absent from the database and CSV. Alternatively:

```sh
.venv/bin/solarbank octopus-key
```

This prompts privately. Do not place the key in a command, URL, screenshot or Git.
A successful account request checks that the selected device is linked and that
its dispatch response is valid, including an empty list. To treat a dispatch as
whole-house cheap electricity, separately record the applicable account terms,
eligibility expiry and verified billing rule. BOOST/TEST/manual charging and
charger power do not establish eligibility. Disable account access if that
eligibility is uncertain. A current verified billing interval is retained only
to its established end; future cancelled slots lose authority.

Run the independent development worker in a second terminal:

```sh
.venv/bin/solarbank providers --count 120
```

This performs at most 120 iterations, five seconds apart, and stops. Default
count is one; maximum is 3,600. Ctrl+C stops it. It is not a permanent service.
The website remains responsive while the worker waits for a provider. The
connection-test/refresh button queues a request; it cannot start a stopped
worker or bypass provider request limits. Weather requests are at least one
hour apart; price/account requests at least one minute apart, with backoff.
Each provider request has a 12-second total deadline and 2 MB response cap.

Diagnostics show last success, failure reason, cache validity and next permitted
attempt. Valid prices/cache are preferred to fallback prices per interval. Smart
slots use the confirmed fallback cheap price with an explicit estimate label.
Weather cache lasts at most six hours, public price cache one day and future
slot cache two minutes, and each must still cover the required interval.
A valid empty dispatch list means no future slots; a failed request is shown as
a failure. Missing weather/measurements are not repaired by a valid tariff.

Forecast summaries expire after five minutes and are marked out of date after
a settings revision. Detailed inputs and actual acquisition times are archived
in the application's private database. No clear-sky or retrospective forecast is
presented as a current weather prediction. Shadow advice requires the separate planning worker described below. Archive retention/compaction and permanent-service
recovery remain pending, so use bounded runs during development.

## Run shadow planning without battery commands

Install the isolated solver environment once, independently of the website:

```sh
sh scripts/bootstrap-solver.sh
.venv/bin/solarbank shadow --count 10
```

The pinned EMHASS 0.18.4 dependencies are large and stay in `.emhass-venv`.
The lock has been checked on this Mac with Python 3.11; Pi acceptance is pending.
For just the independently checked target-policy search, use
`.venv/bin/solarbank shadow --without-emhass --count 10`.
No solver starts Home Assistant, fetches a forecast, or sends equipment commands.
Octopus account access remains OFF at the owner's request; confirmed fallback
prices support planning without the key. No extra cheap slots are inferred.

The worker normally plans once a minute, checking input changes/refresh requests
at five-second intervals. Default count is one; maximum is 1,440 planning attempts.
Ctrl+C stops it. Only one planning worker may own a database. The dashboard's
**Refresh shadow advice** queues a request for a running worker; it cannot start
one. Collection and providers remain separate bounded processes. No permanent
services or unattended collection have been installed.

Advice needs SOC no more than five seconds old, a complete 48-hour household
forecast, current weather-based solar and valid prices. Normal/High require a
recent completely covered and tagged Normal-use day. The supplied historical
means do not establish this. Missing inputs show a reason, without a target.
The approved short live capture is not continuous collection. Do not use a
synthetic demonstration as household advice.

The provisional model searches 5% target increments, with a common target across
three paired demand/solar cases. Their probabilities and equipment efficiencies
are assumptions, not learned or commissioned values. High demand retains its
full regular-night request, including the night before a daytime visit, and an
unreachable request is rejected. Targets limit grid charging; solar may still
fill to 100%. Ending battery energy receives the same next-cheap-price value in
both target and full-night comparisons. Displayed differences are simulations,
not measured savings.

Each accepted plan archives its exact inputs, actual initial SOC, code hashes,
scenario weights, candidate costs, reconstructed flows and independent checks.
Publication and its archive commit together. Changed inputs, a handover request,
stale SOC or expiry withdraw the displayed advice. Maximum lifetime is five
minutes and may be shorter when inputs expire. The graph compares predicted SOC
with available recorder observations; sparse points do not prove continuous
coverage or independent device accuracy.

EMHASS is a separate freely dispatchable diagnostic with conservative hybrid
limits, not an executable Anker target or a savings bound. Its native fixed-step
solver cannot represent a fractional first interval or other irregular exact
boundaries: this is explicitly marked unsupported while the target-policy
analysis continues. Requested native failures/timeouts or failed independent
checks prevent publication. The economic model does not commission the 45 A
limiter or EV protection.

The worker also records a daily review of completed archived horizons. An
explicit review is available with:

```sh
.venv/bin/solarbank shadow-review --date 2026-10-24
```

Choose an already completed London date containing the horizon end. Reviews
report covered demand/PV errors and available SOC observations, leaving absent
data unavailable. They never promote a model automatically or claim causal
savings. Chronological alternative-model comparisons, matched actual Anker
operation, long-term storage budgeting and the real observation period remain
Phase 4 acceptance work. Keep runs bounded until deployment/recovery is proven.

## Backup and recovery

```sh
.venv/bin/solarbank backup runtime/backups/first-check.sqlite3
```

Choose a new backup filename each time. The command uses SQLite's consistent
backup operation, including committed data held in its journal. It refuses to
overwrite another backup. The database stores household settings and history;
keep separate protected copies of the dashboard password and optional Octopus API-key files as well.

To check a backup without replacing the running database:

```sh
.venv/bin/solarbank --database runtime/recovered.sqlite3 restore runtime/backups/first-check.sqlite3
.venv/bin/solarbank --database runtime/recovered.sqlite3 serve --port 8092
```

Restore refuses an existing destination and rejects an incompatible or corrupt
backup. The restored dashboard still cannot control equipment. The application
migrates an empty database or schema-1/2 controller database to schema 3 and rejects
unrecognised databases and future versions. The upgrade is transactional and
preserves settings/history; a failed upgrade rolls back. No upgrade of the old
monitor database is supported. Do not run an older application against a schema-3 database:
automatic backwards rollback has not been established.
Do not copy a live SQLite file with ordinary file-copy commands.

**Planned (revised approach, 2 November 2026):** once the Whisper telemetry store is in service (POST_SHADOW_PLAN.md, WP23a), its folder `/var/lib/solarbank-cost-optimiser/telemetry/` (fixed size, about 125 MB) is part of the off-device backup, copied file by file under the Whisper lock.

The collector reads every second but writes the history once a minute (see
"Disk activity and durability" below): control and the dashboard use the
one-second readings held in memory, and the database holds a one-minute log. A clean
stop or restart writes everything first; a power cut or `kill -9` can lose up to about
two minutes of log (never corrupt the database). Only raw-response retention is automatic;
typed history has no compaction job.
The Pi shadow installation has a storage guard described below.

## Pause / hand control to Anker

**Note (owner, 2 November 2026):** software cannot hand control back to the Anker app, because the app's own settings, including its third-party-control switch, are changed by hand. What the button will do is release the battery by writing the default mode, Dynamic Tariff, after set-point 0 W, and say so (`POST_SHADOW_PLAN.md`, LC1). The text below keeps the wording of the interface as first agreed.

The agreed meaning is: stop optimiser ownership and let you operate the battery
through the Anker app until you explicitly resume automation. The final interface
will use **Hand control to Anker app** and **Resume automation**.

The implementation must first cancel economic actions, perform the verified
release sequence and confirm the resulting mode/setpoints. A request, a stopped
Python process or a zero-power command is not proof of a handover. Until release
is confirmed, show `Handover pending / control state unknown` rather than success.
The pending request persists through restart.

In a successfully commissioned Anker-manual state, the optimiser will continue
observation where possible but will not compete with your Anker-app settings.
Its 45 A limiting and EV discharge inhibition are then inactive; the dashboard
must say so. This is separate from a manual grid-target override within optimiser
control, which must always obey both protections.

Resuming will require an explicit request, verified device identity and current
settings, fresh CT/Shelly/SOC data, restored protections and a new valid plan.
Neither restart nor deployment may resume automatic control on its own from an
Anker-manual or pending-handover state. These physical transitions are pending
phase 5 commissioning; this release cannot perform them.

## Pi shadow installation

The separate Pi installation runs the website, collector, providers and target
planner as four unprivileged services, enabled for boot. The private database
and credentials live in `/var/lib/solarbank-cost-optimiser`, outside versioned
application releases. See [deployment instructions](deployment/README.md).

Open [Solarbank Shadow on the Pi](http://solarbank-pi.local:8091/?access=local),
or its tile in [Heimdall](http://solarbank-pi.local/). This uses HTTP like the other
local Pi apps and opens without a login. The certificate warning page is gone;
browsers may still label an HTTP page “Not secure”. Only the home LAN, loopback
and private Tailscale ranges are accepted. The fresh link avoids the previous
cached HTTPS redirect; replace old HTTPS bookmarks.

The existing Octopus key continues working from the private
`/var/lib/solarbank-cost-optimiser/.env` file. Key entry through remote HTTP is
disabled. To replace it, use the SSH instructions in
[the deployment guide](deployment/README.md#octopus-key). After saving, use
**Connect Octopus (read only)** in Settings. Authentication and linked-device
discovery are separate from confirmation of whole-house cheap-rate eligibility.

Hourly Open-Meteo forecasts are enabled with the owner's explicit approval.
The collector builds covered minute records from the existing monitor's read-only
HTTP endpoint. It does not add equipment connections. A complete eligible
Normal-use day is still needed before household advice can be published.

The native comparison reuses the Pi’s existing EMHASS 0.18.5 environment,
without reinstalling it. Irregular exact interval grids remain explicitly
unsupported by that native comparison; target-policy analysis continues.
An hourly storage guard **alerts and never stops anything** (WP19, reduced, 3 November): it raises an email alert if the data folder (backups not counted) passes 20 GiB, if free space falls below the larger of 10 GiB and a tenth of the disk, if the folder grew by more than 2 GiB since the last hourly check, or if the fixed-size telemetry store changes size or passes 200 MB. It clears an alert when the condition goes away. The old guard stopped the collector and the other services at 16 GiB; that is gone. The emergency purge of SQLite detail described in the plan is not built, because the disk is large and the telemetry store is fixed; it returns if the data moves to a small disk. No battery control is installed.

The account's current tariff was identified through its authenticated read-only
account endpoint. This product publishes separate day/night prices rather than
standard rates. The price worker reads both feeds and uses your confirmed London
night window to map them onto time. Current supplier values are 34.9561p/kWh by
day and 6.60p/kWh at night; the dashboard rounds the day rate to 34.96p.
Ongoing supplier rates are clipped to a 72-hour horizon and refreshed every
30 minutes. Labels distinguish API/cache prices from your saved fallback values.
Successful key authentication does not establish whole-house smart-slot billing.


The owner-provided Normal-use benchmark is **12.24 kWh/day**, with sample SD
**2.66 kWh** over 12 days (6–17 October). The home page shows it and compares
complete Normal forecast days when available. A difference beyond two sample
SDs is a review flag, never an automatic adjustment or a claimed confidence
interval. The benchmark does not manufacture missing hourly demand history.


### Garage feed display

The home page shows signed garage-feed power in kW from the Anker Sub / PV-ESS
CT, using the existing monitor feed. It covers the entire garage circuit, not
just the battery or charger. The supplied sign is preserved without assuming
a physically verified import/export direction. Stale or missing data shows a
dash. Actual current remains unavailable in this feed and is never estimated
from power. The card explicitly says the 45 A protection is not active.


## Disk activity and durability

The Pi uses a mechanical disk, so the application writes little and in large batches.

**Planned change (revised approach, 2 November 2026).** The one-second readings and the one-minute log move from SQLite to a fixed-size Whisper store (issue #16, POST_SHADOW_PLAN.md WP23a to WP23c): one file per metric, 1 second resolution for 2 days, 1 minute for 90 days and 30 minutes for 10 years, written only by the collector in batches of 10 to 30 seconds and never touched by the controller. It will be written alongside SQLite for two weeks and compared before anything is switched. Until then this section describes the current behaviour; afterwards the minute rows, `charger_coverage` and their 7-day purge go away and the table below changes. Each metric whose coverage matters also keeps a count of its valid seconds, so a partly measured minute is never taken for a complete one, and the SQLite rows keep being written for at least four weeks after the readers switch so the previous release still works (POST_SHADOW_PLAN.md, TC and RC1). Real-time decisions still never depend on the logger or the database.

### What is stored, and what is not

| | One-second data | One-minute log |
|---|---|---|
| Where | memory and `/run/solarbank-cost-optimiser/live.json` (RAM-backed; lost on reboot) | the SQLite database on disk |
| Used for | the live dashboard, shadow planner (battery reading must be under 5 s old), charger start/stop detection, and future real-time control | history, forecasts, reviews, charts, energy accounting |
| Content | every reading of every channel | per channel per minute: the **last** sample, how many samples there were, how many were valid, the **mean** and the **maximum** of the second-by-second values |

Stored rows are marked with `window_samples`, `window_ok`, `window_mean` and `window_max`.
Older one-second rows are kept unchanged and need no conversion. Also stored each minute: one
raw monitor response, one `collector_snapshot` event and one charger sample (mean power, state).
A change in collection status (a failure starting or ending) is kept at full one-second
resolution as evidence. The exactly-tiled minute averages used for forecasting are unchanged.

What you give up: second-by-second detail older than the live file (it is not kept after the
minute is summarised). Energy is unaffected (the cumulative counters are stored as last value;
the minute mean preserves kWh). Short peaks are still visible through `window_max`. If you
need second-level detail for a fault-finding period, run with `--record-seconds 1`.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `collect --record-seconds 1\|60` | 60 | 60 stores one summary row per minute; 1 stores every reading (the previous behaviour). Falls back to 1 if there is no live file, because the planner would otherwise see stale readings. |
| `collect --flush-interval SECONDS` | 60 | How often the held data is committed to the database. `0` commits every cycle. Capped at 2 with no live file. |
| `--synchronous normal\|full` | normal | `normal` with the write-ahead log never corrupts the database on a power cut; at worst the last commits are lost. `full` also forces a disk sync at every commit. `OFF` is deliberately not offered. |
| `--wal-autocheckpoint PAGES` | 4000 | The log is copied into the main file after this many 4 KB pages (about 16 MB); the log file can reach about that size. |
| `--live-file PATH` | `/run/solarbank-cost-optimiser/live.json` | Latest readings, collector health and charger state on RAM-backed storage. |

### Durability trade-off

* Up to one flush interval of the minute log (and the charger state) is lost on a power cut
  or `kill -9`: with the defaults, up to about two minutes. The charger is re-detected on
  restart; an open session is continued and marked partial, as after any restart.
* Real-time decisions must not depend on the database. The planner and dashboard read the
  live file; if it is missing or damaged they fall back to the database (up to a minute old,
  which the planner then reports as "A fresh battery SOC reading is needed", never as
  a wrong plan). The control loop, when built, must read the live readings.
* If the database is busy or fails, the minute log stays queued (bounded; excess oldest minutes
  are dropped and counted as `dropped_snapshots` in collector health) and is retried with
  no duplicates. A clean stop (SIGTERM/Ctrl-C) writes the partial last minute.

### Other changes in this release

* Each process now keeps one idle database connection open. SQLite copies the log into the
  main file and deletes it whenever the *last* connection closes; with a new connection per
  call that happened after every commit.
* New rows use time-prefixed keys so inserts append to the end of the indexes instead of
  landing on a different page each time. Older rows keep their keys.
* New shadow plans store the full record once, plus a SHA-256 digest in `raw`, not two
  identical copies. Older plans are untouched.
* Raw-snapshot pruning runs every 10 minutes and ranks the table only when over budget.

### Measuring and rolling back

Compare before and after on the Pi with `sudo iotop -oPa -d 10` and `iostat -x 10`, and
with the offline model `.venv/bin/python scripts/benchmark-writes.py` (results in
`reports/disk-writes-benchmark.md`). The database format is unchanged, so old and new
versions open the same file. To restore the previous behaviour, start the collector with
`collect --continuous --record-seconds 1 --flush-interval 0 --synchronous full`
(`--synchronous` goes before `collect`).


## Reading the devices directly (built, not yet in service)

By default the collector still takes every reading from the existing monitor's page. The collector can instead read the
devices itself, read-only (function code 4 and HTTP GET only; there is no write code):

```sh
solarbank --database ... collect --continuous --direct-devices shelly,solarbank     # mixed: these two direct, the meter from the monitor
solarbank --database ... collect --continuous --source direct                       # all three direct (monitor must be stopped)
```

* `--direct-devices` takes `solarbank`, `meter`, `shelly` in any combination; unlisted devices come from the monitor.
* Addresses default to the specification's dated values; override with `--solarbank-host`, `--meter-host`, `--shelly-host`
  or the environment variables `SOLARBANK_HOST`, `SOLARBANK_METER_HOST`, `SOLARBANK_SHELLY_HOST`.
* **The smart meter allows only one Modbus client.** Reading it directly means the monitor must not be holding it, so use
  the meter in direct mode only in an agreed monitor-stopped window or after cutover.
* A device that stops answering becomes "unavailable" after 5 seconds; its last value is never shown as fresh. Collector
  health carries `devices` with each device's age, latency, sequence number and failure counts.
* Rollback: start the collector without these options (the default) and keep the monitor running.
* The unit file now allows the collector to reach the three device addresses. Reinstall the units
  (`systemctl daemon-reload`) before using direct mode on the Pi.


### Faster, steadier direct reads (4a.3d) and background commits (4a.3c)

* The Shelly is read with the per-channel call (about 30 ms) and the Solarbank with one 16-register request per second; its slow registers
  (totals, limits, AC output) are refreshed every 10 seconds and a refused slow block is not retried every second.
* The service's database commit and raw-snapshot pruning run in background threads (`background_flush`, on for `collect --continuous`), so a locked or
  slow database can never delay the one-second tick or the live file. The commit is retried until it succeeds, once, and a clean stop waits for it.
* The Shelly refreshes once per second, so polling faster gains nothing.

## Comparing the direct readers with the monitor (step 4a.5)

```sh
solarbank --database ... compare run --out /var/lib/solarbank-cost-optimiser/comparison/compare.jsonl   # temporary service: solarbank-optimiser-compare
solarbank --database ... compare report --file /var/lib/solarbank-cost-optimiser/comparison/compare.jsonl
```

On the Pi `solarbank` is not on the search path and the output file is private to the optimiser user. Use `sh /opt/solarbank-cost-optimiser/current/scripts/compare-report.sh`
(or `sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank --database ... compare report --file ...`).

`run` samples the direct readings (live file) and the monitor's page twice a second and writes one short line a minute (a few KB a day). `report`
summarises agreement, coverage, latency, failures and missed ticks, flags whether the run has seen the cheap-rate window and a real car charge, and
states which conditions are still missing. Thresholds are the plan's provisional ones; the report is not a sign-off. Only devices read directly are compared.
To stop: `sudo systemctl stop solarbank-optimiser-compare`. Output stays in the state folder and counts toward its storage guard.

The report does not require a real car charge while there is no electric car (owner decision, 26 October 2026); it lists it as deferred. When a car is available,
run the report with `--require-ev` (add it to the command, or `sh .../compare-report.sh --require-ev`) so a charge of at least 5 minutes above 10 W is required again.


## Changes from the 26 October code review (release 0.5.5)

* **History downloads** stream in small pages and cannot exhaust memory; `/api/history` is paged (`limit`, `cursor`/`next_cursor`). Every stored field is exported.
* **Login-free dashboard** answers only to its own names and IP addresses (421 otherwise); nginx drops other names. Add a name with `serve --allowed-host NAME`.
* **Deploying** uses `scripts/switch-release.sh`: it stops at the first failed step (a failed backup never reaches shutdown) and restarts the services if activation fails.
  `SOLARBANK_SKIP_BACKUP=1` still skips the backup, and says so.
* **Collector health** now shows `discarded_total` and `discarded_recent` (what was shed, how much, which times) if the database was away long enough to hit a memory limit,
  and Solarbank health shows `slow_age`, `slow_errors`, `slow_stale`. Totals older than 30 s are not published.
* **A second collector** is refused before it contacts any device.
* **Comparison report** counts only recent, new, paired evidence from both devices; "hours" means qualifying hours. Lines written before this release are listed but not counted.

## Dashboard changes in release 0.5.7

* **Real-time indicator:** a pill in the header and a panel on the dashboard show whether the back-end real-time code is running safely (Running safely / Running, with something to watch / Not running safely), with the reasons and each device's age.
* **Household demand is now three buttons on the dashboard:** Away, Normal and High. A tap applies from now with no end time and replaces the previous choice. Away needs the base load in watts. The start/stop times and the "Save household setting" button are gone from Settings.

## The controller (release 0.5.8): shadow mode

One controller process (`solarbank-optimiser-control.service`) makes the control decisions every second from the hardware readings and the plan file, prepares the
commands it would send, and **records them as suppressed**. It sends nothing to the battery or the charger. Collection, the dashboard and the planner do not depend
on it, and it does not depend on them.

**Planned (revised approach, 2 November 2026):** the decision logic described below is to be rebuilt as explicit state machines using the `transitions` library, following the decision table of the owner's functional specification v1.3, with the battery never discharging in a cheap period (POST_SHADOW_PLAN.md, WP9 to WP14 and WP24; issues #26 and #29). When the controller stops or restarts, or its data is invalid, it would release the battery by writing the default mode, Dynamic Tariff, after set-point 0 W (the Anker app's own settings can only be changed by hand), and a HANDS_OFF fault would survive a restart until the owner resets it (the owner's answers of 2 November; POST_SHADOW_PLAN.md LC1 to LC4). Until that ships, this section describes the running code.

| What | Where |
| --- | --- |
| Live status (RAM, replaced every second) | `/run/solarbank-cost-optimiser/control-loop.json` and the "Controller" part of the dashboard's Real-time system panel |
| Command log (one line per command *change*, about 4 MB cap, one rotation) | `/var/lib/solarbank-cost-optimiser/control-commands.jsonl` |
| Execution-mode switch | `/etc/solarbank-cost-optimiser/control-mode` (one word: `shadow` or `live`; owned by root) |
| Journal | `journalctl -u solarbank-optimiser-control` |

Read the last commands the controller decided:

```bash
ssh solarbank-pi 'sudo tail -n 20 /var/lib/solarbank-cost-optimiser/control-commands.jsonl'
```

What the dashboard shows: the controller's own heartbeat (it goes stale within seconds if the controller stops, even while collection carries on), the mode, the
decision (discharge inhibited or allowed, AC charging permitted or stopped, the request active or withdrawn), the restrictions that apply and why, whether a valid
plan exists, and each command's stage. *Suppressed* means recorded and not sent; it is never shown as acknowledged.

What to expect today:
* "AC charging stopped, garage current unknown" is normal in shadow: the meter allows one Modbus client and the original monitor still holds it. The line beside it
  shows what the plan alone would allow.
* "No plan file yet" until the planner writes one.
* After every restart the controller starts with charging off for 30 s (it starts only once the garage reading has been valid for that long). Since the WP24 release, discharge is no longer held for a restart period: the decision table applies from the first cycle, so the battery is held inside the cheap period (23:30 to 05:30) and while the car charges in an expensive one, and the Anker runs the house otherwise.

If the controller stops: `ssh solarbank-pi 'sudo systemctl restart solarbank-optimiser-control'`. Nothing else is affected. A second copy is refused (exit 3), and a request
to use the meter before the monitor is retired is refused (exit 2); neither restarts in a loop.

**Going live is gated, not absent.** The sending transport is part of every release. Writing `live` into the mode file and restarting makes the controller send only if all three
root-owned conditions hold (the write-switch lists the class, the commissioning record is valid for this exact code, and the mode file is `live`); otherwise it runs `logged` and
the dashboard says which condition is missing. Return to the default at any time with `echo shadow | sudo tee /etc/solarbank-cost-optimiser/control-mode` and a restart.

### Controller changes in release 0.5.9

* **Hand-over:** when the plan says `anker_manual`, every queued command is cancelled and the only command issued is `RELEASE`. The dashboard shows the hand-over as
  *requested, not confirmed* until the release is acknowledged. Taking control back starts restricted again.
* **Unconfirmed protective commands hold the controller back:** while the newest stop or inhibit command is rejected, timed out, expired or acknowledged without its
  effect being seen, charging cannot be increased. In shadow nothing is sent, so this never triggers there.
* **No plan, no discharge permission:** with no valid plan (missing, expired, corrupt) the controller inhibits discharge ("no authority") as well as withdrawing the charge
  request. "Hold" and "charging" plans also inhibit discharge; only a valid "discharge" plan allows it (subject to the 5 % reserve and the car).
* **2.5 kW AC cap:** the charge request never exceeds the installation's AC limit, whatever the device reports.
* **The planner now writes the controller's plan file** (`/run/solarbank-cost-optimiser/plan.json`, replaced atomically; removed when there is no valid plan). Inside the planned
  charging window it says "charge to the grid target"; outside it, "the battery may discharge", ending where the window begins.
* **Ordering and expiry** use the controller's cycle number and the monotonic clock, not the wall clock; diagnostic logging is queued so a slow journal cannot delay a cycle.
* **Dashboard:** the status page judges file ages against a time taken after reading them (this removed a false "Clock problem"), and a malformed status file can no longer break it.

### Release 0.5.10: a Normal reference day may have a few filled minutes

A day now qualifies as the Normal reference if at most 10 minutes are missing in total and no gap is longer than 5 minutes (restarts during releases cause these).
Each missing minute is filled from the minutes either side, never with zero, and the forecast reports `gap_filled_minutes` and says so in its assumption text.
See REQUIREMENTS_AMENDMENTS.md (28 October). Any other defect in a day (duplicate, invalid, not Normal, partly covered beyond the limits) still disqualifies it.

### Release 0.5.11: minimum battery level and a one-minute car delay

* **Minimum battery level** is now a setting on the Settings page (default **10 %**, allowed 5 % to 30 %). The planner never plans below it; the plan file carries it to the
  controller, which stops the battery discharging at or below it. The dashboard states the current level. Changing it makes the planner replan.
* **Car charger delay:** after the car stops charging, and after a controller restart, discharge is inhibited until the charger has been idle for **60 seconds** (it was 300).
  The car card and history use the same 60 seconds, so the card and the controller agree. Provisional until a real car charge can confirm it.
* **The Anker owns normal discharge.** The controller no longer inhibits discharge for a missing plan, a charging plan or an unreadable battery; it does so only for the
  minimum level, the car, and an explicit "hold".
* **Planner and controller hand-over:** the planner always writes the plan file: a checked plan, or an explicit "normal" (no grid-charging request), or a hand-over request.

## Retiring the original energy monitor (cutover, release 0.5.11)

The original monitor only existed to characterise the system. After this release the Pi reads everything directly:
* **The controller owns the smart meter.** The meter allows only one Modbus client, so the controller reads it once a second and shares each reading through
  `/run/solarbank-cost-optimiser/meter.json`. The collector (and so the dashboard) reads that file; it never opens a connection to the meter.
* **Garage current** is the largest of the three secondary-CT phase currents (registers 10666 to 10668, divided by 100, a magnitude). It feeds the 45 A decisions and the dashboard.
* **A stale or missing file means unavailable**, never an old value: if the controller stops, the grid and garage readings go blank within a few seconds and the safety lamp says why.
* **The collector no longer calls the monitor** (`--source direct`).

The cutover is two steps done together: deploy this release, then stop and *disable* the monitor (so a reboot cannot start it and take the meter) and stop the comparison service
(its reference was the monitor). Before stopping, the monitor's last readings are recorded so the meter, grid and garage numbers can be compared afterwards.

**The way back (about a minute):** on the Pi, `sh /opt/solarbank-cost-optimiser/current/scripts/rollback-cutover.sh <previous release id>` stops the controller (freeing the meter),
starts and enables the monitor, and switches back to the previous release. Nothing was deleted: the monitor's service and records are untouched, only stopped and disabled.

### Release 0.5.12: the dashboard leads with tonight's decision

The top panel of the dashboard now says what the planner advises, in plain words: **"Charge to X %"**, the cheap window (London time), the expected import cost over the next 48 hours
against a full charge (and the saving, or "no saving tonight"), the inputs behind it (battery now, expected use and solar, minimum level) and what the Anker is doing at this moment
(discharging to the house, charging from the grid or from solar, or idle). It is advice only: nothing is sent to the battery. The plan briefly shows "refreshing" when it is being
recomputed because the battery level ticked, instead of disappearing; with no usable plan it says why.

## The original energy monitor alongside the optimiser (owner decision, 28 October)

The monitor is useful to keep: it holds the savings history and compares forecast against actual solar by several methods. It was only needed for **commissioning** the meter
readings. The rule is **no dependency and no interference** between the two applications:
* **Nothing of the optimiser depends on the monitor.** The collector reads the devices directly (`--source direct`), the planner's solver has its own environment
  (`/opt/solarbank-cost-optimiser/solver-venv`), the deploy script no longer needs the monitor to be running, and no unit of ours runs anything from `/home/pi`.
* **The monitor never reads the smart meter.** The owner's decision (28 October): the meter is removed from the monitor's code altogether (the Solarbank gives it everything it needs); that
  change is made in the monitor's own project. Until the cleaned-up monitor is on the Pi and **proven** (no connection to the meter; savings and forecast comparisons still working), a kernel-level
  rule stays on the Pi: `/etc/systemd/system/anker-energy-monitor.service.d/90-no-meter.conf` (`IPAddressDeny=192.168.1.20`), installed 28 October. Once it is proven, delete that file and
  `sudo systemctl daemon-reload`.
* **Check at any time:** `sh /opt/solarbank-cost-optimiser/current/scripts/check-meter-ownership.sh` fails if anything other than the controller is connected to the meter, or if barrier (2) is missing.
* **Re-enabling the monitor** (only after its meter-free code is on the Pi): `sudo systemctl enable --now anker-energy-monitor`, then run the check above. Its old EMHASS advice timer stays disabled unless you ask.
* **Two readers share the Solarbank** and the Shelly (the collector and the controller); adding the monitor makes three. That is allowed by the devices, but the monitor's own disk writing returns
  with it, so fixing that in the monitor's project (the earlier write-reduction prompt) matters.

### Release 0.5.13: savings and lost-solar records

Each finished London day is scored a few minutes after midnight from the one-minute record and **your confirmed fallback tariff**; the result is a small record kept in the database
(`daily_record:<date>`). Everything is an **estimate**, not a bill.
* **Saving** = what the household load would have cost at that minute's tariff with neither solar nor battery, minus what was actually imported at the same prices. A day with more than
  10 missing minutes (or a gap over 5 minutes) is shown as *Partial* and is never added up; a day with no data is not shown.
* **Lost solar** (candidate only): when the battery was full in daylight, how many minutes show solar held back to the house load, and, if that day's evening-before solar forecast was saved,
  an upper-bound estimate of solar available but not produced, plus what a lower overnight charge might have saved (the solar that could have been stored and used, at the night rate).
  A forecast miss alone does not prove curtailment, so the figure is always labelled a candidate. The evening-before forecast is saved once and never rewritten; the first one is saved on the evening of 28 October.
* **Where to see it:** the Savings panel on the dashboard and the "Daily savings and lost solar" table on the History page.
* **Counting from go-live:** nothing is counted towards the running total until you set the day the optimiser goes live: `sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 savings-start 2026-11-15`
  (use `--clear` to stop). Until then the panel shows the figure "for information, since records began", when the Anker app alone was in charge.
* **To score days by hand:** `... solarbank --database ... daily-record --days 3`.

### Release 0.5.13: the household buttons latch at once

Pressing Away, Normal or High now shows the button as selected immediately and "Saving…"; it goes back to the server's state if the save is refused. Before, it waited for the save and a full page refresh.

### Release 0.5.14: retention (what is kept, what is purged) and the archive stopped growing for nothing

**What went wrong first.** Once the planner began publishing on 28 October it archived a 233 KB plan roughly every 35 seconds, and the household-forecast job archived a 343 KB snapshot about every
minute: together about 44 MB an hour, and about 37 KB/s of disk writes (the household-forecast job alone was 27 KB/s) against about 8 KB/s before. Two causes, both fixed:
the forecast job treated every refresh of the (empty) extra-slot list as a new input; and the planner's change detector included the full list of recent minute rows, so it changed every minute.
Now a forecast is archived when its content changes or every 15 minutes, and a plan is recomputed when something material changes (the battery moving to another 5 % band, a new complete reference day, a setting)
or every 4 minutes.

**The policy (owner, 28 October: a long-term half-hour record is enough for analysis; purge the detail after several days).**
* **Kept for good:** the half-hour record (one entry per Octopus settlement period, :00 and :30 London, 46 to 50 on clock-change days: used, imported, exported, solar, battery in and out, the price and both costs,
  with how many of the 30 minutes were covered), the daily records, the saved evening-before solar forecasts, all your own settings and their full history, car-charging sessions, and imported history.
* **Purged after 7 days** (run every 6 hours by the planner service): the archived plan, forecast and slot snapshots; the one-minute rows; and the old versions of the constantly rewritten operational settings with their
  "settings changed" events. Seven days is the minimum because the planner chooses its Normal reference day from the last seven days of minute rows.
* **Safety rules:** a day's minute rows are purged only if that day's half-hour record is on file; an archive snapshot that a current setting points to is never purged; imported history is never touched; the append-only
  protection on the archive is lifted only inside the purge's own transaction and restored with it.
* **Download:** History page, "Download the half-hour record (CSV)", or `/half-hours.csv?start=YYYY-MM-DD&end=YYYY-MM-DD`.
* **By hand:** `solarbank --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 retention --dry-run` shows what would go; without `--dry-run` it purges; `--issuance-hours 6` clears a burst of archived snapshots sooner
  (never the minute rows). Deleting rows frees space inside the database file for reuse; the file does not shrink.

**Measured write rates, 28 October 2026** (per process, bytes written to disk per second by `/proc/<pid>/io`, 10 to 12 minute samples after the restart settled):

| Process | Before (planner publishing every minute) | After the forecast and planner fixes (0.5.14) | After the change detectors judged the slot lists by content (0.5.15) |
| --- | --- | --- | --- |
| providers | 26.9 KB/s | 10.0 KB/s | **4.1 KB/s** |
| shadow (planner) | 5.7 KB/s | 4.0 KB/s | **2.3 KB/s** |
| collector | 4.0 KB/s | 4.5 KB/s | 4.8 KB/s |
| **Total** | **36.6 KB/s** | 18.5 KB/s | **11.3 KB/s** |

The planner now archives a plan about every 4 to 5 minutes and a household forecast when its content changes or every 15 minutes; what remains each minute is a tiny (about 55 byte) slot snapshot and the small settings writes.
Trade-off: a plan can now be up to four minutes older than the newest battery reading before it is replaced, unless the battery moves to another 5 % band or an input changes; for a decision about tonight's overnight charge that does not matter.

## The telemetry store (WP23a, built, not yet in service)

The collector now also writes a fixed-size telemetry store beside the database, in `/var/lib/solarbank-cost-optimiser/telemetry/` (the collector unit passes `--telemetry-dir`). It is 21 files of 5.73 MB each, about 120 MB in total, and **never grows**: one file per measurement, keeping 2 days at 1 second, 90 days at 1 minute and 10 years at 30 minutes. A gap stays empty; it is never filled with zero. A second set of "count" files records how many valid seconds each minute held, so a half-measured minute can be told from a complete one, and two "peak" files keep the highest garage current and household power in each interval (an average hides them). The `snapshots/` folder keeps about two minutes of 1-second readings either side of events worth tuning from (the garage current over 40 A, the car starting or stopping, a change of household mode); the newest 200 are kept. `whisper==1.1.10` is pinned. Nothing reads it yet: SQLite carries on unchanged, and the readers move over in WP23b after two weeks of agreement. The controller never touches it.

* A point more than 47 hours old is refused, counted and reported (`telemetry.dropped_late` in the collector health), because the store can only merge a late point correctly while it is still inside the one-second archive.
* A failed write is logged, counted (`write_errors`) and retried; it never stops the collector. If the directory cannot be used the collector runs without it and says so in the journal.
* To check it: `ls -l /var/lib/solarbank-cost-optimiser/telemetry | head` (every file the same size, unchanged from day to day) and the collector health on the dashboard.

## Clock synchronisation (WP4, #10)

After a power cut the Pi can start with a clock that is wrong until the network time arrives; the collector and the charger history used to stop on a reading "earlier than the last". Now such a reading is treated as a break in continuity: the charging session is marked partial, nothing is added across the break and tracking carries on (`clock_breaks` in the charger state counts them). Every optimiser unit also waits for `time-sync.target`. That target is reached early unless the wait service is enabled, so **once, on the Pi:** `sudo systemctl enable systemd-time-wait-sync.service` (it makes `time-sync.target` mean "the clock is synchronised"). Check with `timedatectl` (it should say `System clock synchronized: yes`).

## Alerts by email (WP5, #9, decision D1)

Every long-running unit now restarts forever (`StartLimitIntervalSec=0`, a restart after 5 s for the controller and 30 s for the rest) and names an `OnFailure=` alert unit. A unit that stops for good, for example the controller refusing to start because the original monitor still owns the meter (exit 2) or another controller holds the lock (exit 3), raises an alert. A timer runs `solarbank alert --run` every two minutes: it checks that each service is active (two misses in a row before it counts, because a deploy stops them for a minute), that the controller's heartbeat and the collector's live readings are fresh, and that the disk is not nearly full, then emails any alert not sent in the last 6 hours. Only the alert units may use the internet; the controller still has none, and raises nothing itself.

**One-off setup on the Pi (two small private files in `/var/lib/solarbank-cost-optimiser/`):**
1. The Resend key: `sudo -u solarbank-optimiser bash -c 'umask 077; read -rs KEY; printf "%s" "$KEY" > /var/lib/solarbank-cost-optimiser/resend-key'` (paste the key; it is not shown. It must be `bash`: the Pi's `sh` has no silent `read -s`.)
2. The addresses: `sudo -u solarbank-optimiser sh -c 'umask 077; printf "{\"to\": \"you@example.com\", \"from\": \"alerts@your-verified-domain\"}" > /var/lib/solarbank-cost-optimiser/alert.json'` (the sender must be an address on a domain verified in Resend).
3. Test it: `sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 alert --test`. It says `Test alert sent.` or why not.

Until both files exist alerts still collect in `/run/solarbank-cost-optimiser/alerts/` and the alert run reports why it cannot send; nothing else is affected. A tick that raises in the collector is logged, counted (`tick_errors` in the collector health) and the next tick runs; only 120 failures in a row stop it, so that systemd restarts it.

## The dashboard never shows an old picture as current (WP18, #20, #22)

`/api/status` now carries `computed_at`, `served_at`, `stale` and the limit it applies (`status_max_age_seconds`, 30 s). If a recalculation is stuck and a request is handed the previous picture, the server marks it `stale` once it is older than the limit, and the page applies the same limit from the same numbers: it refuses a stale or out-of-order response and shows its usual "connection unavailable" state, never the old numbers as if live. "Return to automatic" clears the pause and the override in one transaction, so a failure cannot leave one of them behind.

## Where the devices are (WP20, #15)

No device address is written into the code any more. The three addresses live in `/etc/solarbank-cost-optimiser/devices.conf`, outside the release folders, so a release never carries one and a router change is a one-line edit:

```
SOLARBANK_HOST=192.168.1.101
SOLARBANK_METER_HOST=192.168.1.20
SOLARBANK_SHELLY_HOST=192.168.1.103
```

The first deploy of this release creates the file from the addresses that were in use, and later deploys never overwrite it. Precedence is the command line, then the environment, then the file. Only IP addresses and plain host names are accepted. **If a device moves:** `sudoedit /etc/solarbank-cost-optimiser/devices.conf`, then `sudo systemctl restart solarbank-optimiser-control solarbank-optimiser-collector`. A device that is asked for but has no address is a clear message naming the setting and the file: the controller then refuses to start with exit 4, which its unit never restarts on and which raises an alert. The controller and collector units may now reach the whole `192.168.0.0/16` home network (still nothing outside it for the controller), so a device that moves inside it is found without a unit change; a different private range needs the two `IPAddressAllow=` lines edited. Not yet built (WP21): editing the addresses on the Settings page, and verifying a device's identity at each address before trusting it.

## Commissioning and going live: one status flag (RC4)

The sending transport is part of every release, but it cannot send until all three root-owned conditions hold. The code you commission is the code that goes live, so going live changes files, never code.

| Condition | Where | Default |
| --- | --- | --- |
| The status flag: the word `live` | `/etc/solarbank-cost-optimiser/control-mode` | `shadow` |
| A commissioning record for this exact controller code with every test of that write class's stage passed (GitHub issue 76) | `/etc/solarbank-cost-optimiser/commissioning.json` | none |
| The write classes allowed (`mode`, then `setpoint`) | `/etc/solarbank-cost-optimiser/write-switch` | empty |

With the flag at `live` and a condition missing, the controller runs in `logged` mode (journals what it would write, sends nothing) and its start-up line says which condition is missing. Check at any time:

```
sudo /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank commission status
```

Record the tests on the Pi at the battery with the controller stopped (`sudo systemctl stop solarbank-optimiser-control`), in this order. Each prints `PASSED` or `FAILED` with the reason, and records a pass only for the current code:

```
sudo .../solarbank commission run read-registers --confirm
sudo .../solarbank commission run hands-off-latch
sudo .../solarbank commission run mode-write --confirm
sudo .../solarbank commission run default-mode --confirm
sudo .../solarbank commission run setpoint-zero-write --confirm
sudo .../solarbank commission run release --confirm
```

`setpoint-both-signs` is T2, the specification's sign test, run like the others (GitHub issue 75): `sudo .../solarbank commission run setpoint-both-signs --confirm`. It runs beside the controller in logged mode (never beside a live one: a logged controller writes nothing and keeps the smart meter's one connection, so the garage current stays fresh; with the controller stopped it goes stale), and needs the car not charging, the battery between 20 % and 90 % and the house using at least 400 W (export is off, so a discharge needs a load); it writes nothing unless all of those hold. It then takes the battery for about eight minutes: 0 W, mode 3, and 20 set-points in the specification's pattern (−500, +300, −600 then −300, −200 after a positive; never above 600 W), each held 20 s and judged on the Shelly's Solarbank channel after an 8 s settle (at least 100 W in the right direction). It stops at the first wrong sign, if the car starts, if the garage current reaches 40 A, if a reading goes stale, or on Ctrl-C, and always hands back: 0 W, then self-consumption (mode 0, the hand-back mode since 12 November, GitHub issue 78). It reads the collector's live file, so it opens no extra connection to the Shelly or the meter. `supervised-run` is your own sign-off, with a note saying what you saw: a supervised session with the car (gate G1). Any change to the controller code closes the gate until the tests are repeated; `commission status` shows the missing ones.

### The specification's own mandatory tests (GitHub issue 65, D16: with a car, nothing is waived)

Nine more sign-offs, none of them things `commission run` can drive (they need the real device behaving, in daylight, with the car, or with another load) — specification section 13, each with `commission pass <test> --note "what you saw"`:

| Test | Procedure (abbreviated; see the specification for the full wording) |
|---|---|
| `t3a-pv-not-full` | Daylight, SoC under 95 %, mode 3 at 0 W: confirm PV charges the battery |
| `t3b-pv-full-car-charging` | Daylight, SoC 100 %, mode 3 at 0 W, car charging: confirm PV follows demand and supplies the car |
| `t5-mode-change-timing` | Change 3 to 0, then 0 to 3: note the time until behaviour changes |
| `t6-comms-loss` | Mode 3, -300 W, stop the controller for 10 minutes with independent monitoring and the Anker app ready to switch off third-party control: note whether charging continues |
| `t8-garage-regulation` | Full-rate charge; start the car, switch the freezer, add a 2 kW load, delay meter readings, then remove each: confirm I_garage_avg stays at or under 45 A after every step; note the peak and the settling time |
| `t9-garage-ct-sign` | With branch protection and third-party PV monitoring off, switch a known garage load on and off: confirm the reading is present over Modbus and P_garage rises positive with the import |
| `t10-smart-slot` | Observe a real Octopus dispatch start, move, shorten, cancel and end, plus a fetch failure, an empty list mid-session and a clock change: confirm SMARTSLOT behaves as specification 6.2. Needs a release that carries GitHub issue 67 (the dispatches reaching the controller) deployed first; logged mode is enough to watch it |
| `t11-rms-current` | Compare I_garage_now with a clamp meter at several loads: confirm within 0.5 A |
| `t12-mode-reversion` | Change the mode in the Anker app while the controller is running: confirm it is detected, re-asserted once, and HANDS_OFF after the limit |

The three files live in `/etc/solarbank-cost-optimiser`, a folder only root can change, and the controller ignores any of them that root does not own or that others can write; they are not in the service's data folder, where the service could replace them. The old copies there are removed on activation (the flag restarts at `shadow`). Writing a zero set-point is protective: it is allowed when `mode` is, so the first step (`mode` only) can make its transitions through 0 W; a non-zero set-point needs `setpoint`.

Going live is then: put `mode` in the write-switch (the limited first step, G2), set the flag with `echo live | sudo tee /etc/solarbank-cost-optimiser/control-mode`, and restart the controller. Add `setpoint` when the mode writes have behaved. To stop sending: **release first, then disable.** Putting `shadow` in the flag and restarting makes the controller write 0 W and the default mode on the way out (check the journal shows both `wrote`); only then empty the write-switch. An empty write-switch blocks the release writes too, and the controller then reports the release as not done. The switch is read before every register write, so emptying it also stops a sequence already under way.

### Connection sharing: prove it at commissioning (GitHub issue 71)

Not one of the gated tests above, and no code change (owner decision A, 9/10 November): the controller's reader, `devices/modbus_write.GuardedModbusWriter`'s own connection for writes, and the collector's separate reader are three Modbus clients, and nothing serialises a write on one against a read on another. Specification sections 2 and 10.5 want one connection and one lock per device; this installation has been seen to tolerate several clients on the Solarbank (the smart meter does not, and already gets one client only), but never proven with a write in flight.

Prove it once, with the controller stopped and the real commissioning writes already passed (`setpoint-both-signs`): start the collector so it polls the Solarbank at its normal rate, then run several of the controller's own register writes (for example, repeat `commission run setpoint-zero-write --confirm` a dozen times a few seconds apart) while the collector keeps polling. Watch the collector's own log and the write command's own result for any read or write that is refused, delayed noticeably, or simply missing. A few minutes at this is enough to see whether the device's client limit is ever reached. That exercises two of the three connections (the controller is stopped, so its own reader is not polling). The third is covered during the supervised run that follows: while the controller is live, look in `pi-tools.sh writes` for any `failed` entry and in the collector's log for refused reads, and record both results.

There is nothing to run `commission pass` for here (owner decision A keeps this as a one-off, recorded proof, not a mandatory gate): record what was measured in `REQUIREMENTS_AMENDMENTS.md`, dated, under GitHub issue 71, with a plain statement of whether anything was lost, refused or delayed. If something was, that is the moment to revisit option B (the writer joining the reader's own connection and lock) before relying on it further.

## Off-site backup to Google Drive (WP8)

A nightly timer (12:40 London, outside the cheap window) builds one compressed archive of a consistent database copy and the telemetry files (the copy leaves out plan and forecast snapshots older than six hours, except any a current setting points to: they were about 800 MB of the 1 GB database and the large uploads hit Google Drive's rate limit on 12 and 14 November 2026; the live database is untouched and keeps seven days of them), unpacks it again to prove it opens and passes its integrity check, uploads it with rclone, checks the remote reports the same size, and keeps the newest three. The outcome is in `/var/lib/solarbank-cost-optimiser/offsite-status.json`; two days without a verified copy raises an alert. It does nothing until you set it up once:

1. `sudo apt install rclone` on the Pi.
2. Create the Drive remote as the service user (rclone's headless login prints a command to run on a computer with a browser): `sudo -u solarbank-optimiser env RCLONE_CONFIG=/var/lib/solarbank-cost-optimiser/rclone.conf rclone config` and call the remote `gdrive`.
3. `echo 'REMOTE=gdrive:solarbank-backups' | sudo tee /etc/solarbank-cost-optimiser/offsite.conf`
4. Try it: `sudo -u solarbank-optimiser env RCLONE_CONFIG=/var/lib/solarbank-cost-optimiser/rclone.conf /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 offsite-backup`. Add `--local-only` for a restore drill that uploads nothing. It exits 7 and says why if anything fails.

The archive holds the whole database (about 2 GB uncompressed), so three copies fit comfortably in a free Drive account. To restore, download an archive, unpack it, and use `solarbank restore` on the database file; copy the `telemetry` folder back into the data folder.

### Cloudflare R2 instead of Google Drive (in use since 14 November 2026)

Google Drive refused the uploads with `RATE_LIMIT_EXCEEDED` on 12 and 14 November (once at the upload, once at the listing that checks it), so the destination moved to a Cloudflare R2 bucket (S3-compatible, free tier of 10 GB a month and one million writes; three copies of about 150 MB are a small fraction of it). Only the config changed, not the code: the backup uses `copyto`, `lsjson` and `deletefile`, which rclone does for any remote.

1. In the Cloudflare dashboard create a bucket (here `solarbank`, Standard, public access left disabled) and an API token with Object Read & Write limited to that bucket. Note the Access Key ID, the Secret Access Key and the account endpoint `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`. Never paste them into chat or Git.
2. Add this block to `/var/lib/solarbank-cost-optimiser/rclone.conf` (as the service user; the keys are typed on the Pi only):
   ```
   [r2]
   type = s3
   provider = Cloudflare
   access_key_id = <KEY ID>
   secret_access_key = <SECRET>
   endpoint = https://<ACCOUNT_ID>.r2.cloudflarestorage.com
   acl = private
   no_check_bucket = true
   ```
   `no_check_bucket` is needed because a bucket-limited token cannot check or create the bucket. A one-line way to add it without an editor, prompting for the two keys: `read -rp "Key ID: " K; read -rsp "Secret: " S; echo; printf '...' "$K" "$S" | sudo -u solarbank-optimiser tee -a /var/lib/solarbank-cost-optimiser/rclone.conf > /dev/null; unset K S`.
3. Test with a file, not a stream: `rclone copyto /tmp/file r2:solarbank/test.txt`. `rclone rcat` (an upload of unknown size) answers `501 Not Implemented` on R2 even though the setup is right.
4. `echo 'REMOTE=r2:solarbank' | sudo tee /etc/solarbank-cost-optimiser/offsite.conf`, then `sudo systemctl start solarbank-optimiser-offsite.service` (it waits until the backup finishes; the first run took about ten minutes). To go back to Drive set `REMOTE=gdrive:solarbank-backups`; the Drive block can stay in `rclone.conf`.

Known quirk with the Pi's rclone 1.60.1: an upload of a large file (over rclone's 200 MiB multipart cutoff) reaches 100%, then reports `501 Not Implemented` once; the retry finds the object complete and passes. The backup's own size check against the listing still decides success. The first R2 run (14 November, 20:09) uploaded `solarbank-backup-20261114T200944Z.tar.gz`, 159,623,207 bytes, and the Cloudflare dashboard listed it.

## Independent heartbeat, alert fixes and the device book (GitHub 88, 89 and 84; built 15 November 2026)

**Why:** every alert the system raises is emailed by the Pi itself, so none can be sent if the Pi loses power, its disk fails or the internet is down. A free outside service (Healthchecks.io) is pinged every five minutes and alerts you when the pings stop. It is read-only, outside the control loop, opens no incoming port, and writes nothing to any device.

**The checks** (create them at healthchecks.io, with your email as the notification method):

| Check | Pinged | Period | Grace |
|---|---|---|---|
| Pi alive | every run of the heartbeat timer | 5 minutes | 10 minutes |
| Optimiser healthy | only when the controller and the collector are demonstrably working; otherwise the ping is **withheld** and the alarm comes when the grace runs out | 5 minutes | 10 minutes |
| Backup (optional) | by the off-site backup after each verified upload | 1 day | 1 hour or more |

"Healthy" means: the controller's status file is under 30 s old and cycling, in the mode `/etc/solarbank-cost-optimiser/control-mode` asks for (and sending, if that is `live`); and the collector's newest reading is under 60 s old. A device that is switched off is not a fault here unless **no** reading at all is fresh.

**The ping addresses are secrets** (whoever holds one can keep a check green). They go in `/var/lib/solarbank-cost-optimiser/healthchecks.json`, typed on the Pi, never in Git, a log or a message. This one line asks for them with the input hidden (leave the third blank if there is no backup check):

```
read -rsp "Pi URL: " A; echo; read -rsp "Optimiser URL: " B; echo; read -rsp "Backup URL (or blank): " C; echo; printf '{"pi": "%s", "optimiser": "%s"%s}\n' "$A" "$B" "${C:+, \"backup\": \"$C\"}" | sudo -u solarbank-optimiser tee /var/lib/solarbank-cost-optimiser/healthchecks.json > /dev/null; sudo chmod 600 /var/lib/solarbank-cost-optimiser/healthchecks.json; unset A B C
```

**Turning it on:** a deploy installs the units and turns the timer on only if that file exists. If you create the file after a deploy: `sudo systemctl enable --now solarbank-optimiser-heartbeat.timer`.

**Checking it:** `systemctl list-timers solarbank-optimiser-heartbeat.timer` and `journalctl -u solarbank-optimiser-heartbeat.service -n 10 --no-pager`; the check's page at healthchecks.io shows the last ping. A line `Optimiser heartbeat withheld: …` names the reason (never an address). Expect one to two false alarms at first from a deploy or reboot that takes longer than the grace; if they recur, widen the grace.

**What it cannot see:** the battery itself. If the Pi dies mid-charge the Solarbank keeps the last mode and set-point written (commissioning test T6, still to do).

**Also in this release**
* **Heartbeat alert (issue 88).** A heartbeat file that is **missing** now raises an alert once its service has been active, and the Pi up, for five minutes; before this a missing file cleared the alert. A file dated more than a minute in the future (a clock fault) raises one too.
* **Device book (issue 84).** `sudo solarbank devices confirm` no longer leaves `device-book.json` or its lock file owned by root: as root the new file is given to the service user. The write is atomic (a unique temporary file, flushed to disk, then renamed), so an interrupted write leaves the old book whole. The mode stays 640.

## Moving minute history into the telemetry store (WP23b, single store)

From this release minute detail is kept in one place, the telemetry files. The planner's demand history, the dashboard charts, calibration, the daily and half-hour records, validation, shadow review and the charger history all read them. The collector no longer writes `shared-monitor-minute` rows or `charger_coverage` rows to SQLite. Settings, events, sessions and the permanent records stay in SQLite. The one-minute log rows the collector already wrote are purged after 7 days as before; nothing is deleted by this release.

Before the switch, with the collector running for at least a day so the files hold live data:

```
sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 telemetry backfill --telemetry-dir /var/lib/solarbank-cost-optimiser/telemetry
```

It copies SQLite minute rows into minutes the files do not already hold (the live feed is never overwritten), then compares the two stores and prints `"ok": true` or exits with code 5 listing the differences. It is safe to run again; `telemetry check` repeats the comparison alone. Do not switch readers if it fails.

The release writes `/var/lib/solarbank-cost-optimiser/oldest-compatible-release`. `switch-release.sh` refuses an older release (exit 3) unless `SOLARBANK_ALLOW_OLDER=1`, because an older release would see minute history only up to the switch. Per-minute charger power is kept 90 days; the charger history API now reports `covered_seconds` instead of `valid_retrievals` and `invalid_retrievals`, and a minute with fewer than 30 valid seconds has no mean.

## Learned demand, device addresses and the hourly plan (built before commissioning)

**Learned household demand.** Each finished day is tagged Away or Normal from the settings history, with the minutes in which the car charges left out. Away: once a learned figure (the mean or the 80th percentile of the last 7 Away days) has beaten the typed base load over 3 scored days, with the planner's own cost (a shortfall costs 28p a kWh more than a surplus), the planner's Away figure moves towards it by at most a tenth of the typed value a day. The typed figure stays the starting value and the cap. Normal: a mean of the last 3 good days replaces "the last good day" only after beating it over 3 days. The Settings page shows both figures, "Household demand learning", with one button that switches learning off and returns the planner to the typed figures at once. High demand is never learned. `GET /api/demand-learning` shows the same data.

**Device addresses and identity.** The shared record is `/var/lib/solarbank-cost-optimiser/device-book.json`. Each device is recognised the first time it is read (serial digests and the Shelly's MAC, never a serial in clear) and checked again after any failure and every 10 minutes, through the connection the reader already holds (the meter allows one client and the Solarbank only a few, so no extra connection is opened): a different device at that address is "unavailable", never data. If a device has been unreachable for a minute the controller looks for it, the neighbour table first and then a bounded scan of its /24 (port 502 for the Solarbank and meter, 80 for the Shelly, at most one scan per 10 minutes, only a host with the commissioned identity is accepted), records where it went and raises an alert. A found address overrides `devices.conf` only while `devices.conf` still says what it said when the device was found, so an edit you make afterwards is obeyed. The collector only follows the record. On the Settings page, "Device addresses" lets you ask for an address; the controller probes it and adopts it only if the identity matches, and the page shows the answer. After replacing a device on purpose, run `solarbank devices accept <name>` so the new one is learned. `solarbank devices show` prints the state. mDNS is not used.

**The minimum level.** The Settings page now shows the Solarbank's own limit (its discharge-limit register, set in the Anker app) beside the setting and says when they differ; the battery's own limit wins.

**The plan hour by hour.** Each plan carries an hourly series in the same four quantities as the Anker app's Smart Mode chart (solar, battery, home usage, from grid), the price and the battery level at the end of each hour. It is a table in the planning section of the Settings page and `/plan-series.csv` downloads it, so you can lay it beside the Anker's chart. EMHASS stays a reference alongside our own checked policy search.

## Planner behaviour added after the second review (GitHub issues 32 and 34)

* **No forecast, no stop (GitHub issue 32).** When the weather forecast is missing, switched off, for other settings or older than 20 hours, the planner still plans: it assumes no solar output (so it charges from the grid at the cheap rate rather than risk running short) and the plan's assumptions say so. The next fresh forecast replaces it.
* **Smart-slot rules in the planner (GitHub issue 34).** A listed slot is priced as cheap only if the car is measured charging in the half-hour in progress (a slot that has only just begun has five minutes of grace), and only up to 12 half-hours per midday-to-midday London day, counting half-hours the car has already charged in (from the telemetry files). Later slots are assumed to be used.

## What Octopus does with the smart slots (GitHub issue 34)

The charger is scheduled by Intelligent Octopus Go, not by the car, so about six hours of SMART slots are listed every day whether or not the car uses them, and what happens to them when the car reaches its target charge is not yet known. The controller only treats the slot as cheap while the car is measured charging (GitHub issue 67, below): a slot being listed is never enough by itself. To measure what actually happens, run once a day on the Pi and keep the output (the evidence is kept for about a week):

```
sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/python /opt/solarbank-cost-optimiser/current/scripts/slot_report.py --days 3
```

It is read-only. For each allowance day (12:00 to 12:00 London) it prints the hours listed, the hours the car charged and the hours listed while the car was idle; and for each finished charge it says whether the slots still listed beyond the stop were KEPT, SHORTENED or REMOVED, by comparing the slot list just before the stop with 10 minutes and 1 hour after. It needs the Octopus account connection in Settings (the API key and the charger's device ID), because nothing else records dispatches. Then compare one day with the Octopus app or bill: were the half-hours in which the car drew nothing still billed at the cheap rate for the house?

### SMARTSLOT reaches the controller (GitHub issue 67, owner decision A)

The planner prices smart slots, but the controller is a separate process and never reads the database. The providers service shares the dispatches it just fetched from Octopus
(the SMART ones only; a bump charge or other type is dropped before this point) as a small file, like the plan file:

* **`/run/solarbank-cost-optimiser/tariff.json`**, written by the `providers` service every time it asks Octopus (about once a minute, even when the answer is unchanged — an
  unchanged "no slots" answer is still evidence Octopus was just asked), read by the `control` service every cycle. Owned by the service account; a stale or missing file means
  SMARTSLOT is false, the safe direction, same as every other reading this controller depends on.
* **`signals.SmartSlot`** does the rest exactly as specification 6.2 describes: a slot counts only while the car is measured charging, for the allowed six hours (12 half-hours) a
  day, and a slot already under way holds to the end of its half-hour even if Octopus's list changes mid-way. The controller keeps the day's allowance use in
  `/var/lib/solarbank-cost-optimiser/smart-slot-allowance.json` (written only when a new half-hour is counted), so a restart or a deploy no longer starts the day's count from
  zero (GitHub issue 72). Only smart charging uses the allowance (specification 6.2): a half-hour counts when the car charges in it inside the cheap window or
  inside a listed SMART slot. A bump charge outside both is billed at the peak rate (owner, 9 November) and does not count, in the controller and the planner alike.
* **CHEAP** (specification 5.2) is OFFPEAK **or** SMARTSLOT: a real smart slot outside the 23:30 to 05:30 window, with the car charging, now charges the battery, which the clock
  window alone could never do — this was the live example that found the gap (9 November: a SMART dispatch 05:00 to 08:00, half of it outside the window).

## Logged mode and the commissioning switch (Stage C)

The controller now has three execution modes, chosen by the one word in `/etc/solarbank-cost-optimiser/control-mode` (owned by root, so the service cannot change it) and applied when the controller restarts:

* `shadow` (today's mode): commands are prepared and recorded as "suppressed". Nothing else happens.
* `logged`: the action machine runs as it would live and **every register write it would make** is journaled in `control-writes.jsonl` (kind `would_write`: the register, the value, why, and that it was not sent). The controller decides exactly as in shadow and nothing is sent to the battery. This is the rehearsal: it shows the write order (0 W, the mode, a 2-second settle, the set-point), the release and the exit. Start with `echo logged | sudo tee /etc/solarbank-cost-optimiser/control-mode` and `sudo systemctl restart solarbank-optimiser-control`. It needs the collector's settings log (`device-settings.jsonl`) for the start-up checks (mode mask and Backup Reserve); without it nothing is journaled and the dashboard says why.
* `live`: **sends to the battery when, and only when, both root-owned conditions hold:** the mode file says `live` and the write-switch lists the write class (owner, 12 November 2026: `commissioning.json` is the owner's notebook and no longer gates; see the 12 November section below). With either missing the controller runs `logged` and says which. The transport is part of every release: going live changes files, never code. **Treat `control-mode` = `live` as a live switch.**

**The commissioning switch** is the file `/etc/solarbank-cost-optimiser/write-switch`, owned by root and created empty (nothing allowed). When a sending transport exists, a write is sent only if its class is listed there, one word per line: `mode` (register 10064) and `setpoint` (register 10071). A missing, empty or unreadable file allows nothing. It is read on every write, so **emptying the file is the off switch** and takes effect at once; anything it holds back is journaled as `would_write` with the reason, and the controller then re-plans from 0 W when the switch is turned back on. Every real write, and every failed one, is journaled too (`wrote`, `failed`).
In live mode the journal also carries `record` lines (GitHub issue 72), which are observations, never writes: `possible sign inversion` (a charge was commanded and
the Solarbank was seen supplying), `possible firmware override` (it drew from the grid while held at 0 W), each once a condition has lasted 20 s; and `mode_reversion`
(the mode found, the battery status and the DC and AC power, whenever the device was not in the mode the controller set). `pi-tools.sh writes` lists them separately.
The collector's settings log (`device-settings.jsonl`) now also names register 10080 (`register_10080`, raw): a communication watchdog on the Anker X1, undocumented
on the Solarbank 4. It is read only, never written, and worth watching during commissioning test T6 (loss of communication).

## Release 0.5.17

- Dashboard: the Real-time system panel moved to the bottom of the page; the decision, savings, household and charging advice come first.

## Release 0.5.18

- The "Next 48 hours" panel now has a graph of household use (orange) and solar output (gold): measured over the last 24 hours (solid) and forecast for the next 48 hours (dashed), with the cheap-rate bands and a "now" line. The forecast summary carries a half-hour series; the status carries half-hour means of the measured powers (read at most once a minute).

## Release 0.5.19

- The dashboard is reachable over IPv6 (browsers on the home network often reach solarbank-pi.local that way). nginx listens on [::]:8091 and both nginx and the application allow IPv6 loopback, link-local, private (fc00::/7), Tailscale (fd7a:115c:a1e0::/48) and the home /64 (2001:db8:abcd:a::/64). If the ISP changes the home prefix, update that /64 in deployment/solarbank-optimiser.nginx and web/app.py. The previous nginx file is kept on the Pi as solarbank-optimiser.bak.

## Release 0.5.20

- Battery chart: what the plan expected over the last 24 hours is drawn as a dotted blue line beside the solid recorded line. The planner keeps a small hourly log of its battery forecasts (setting `soc_forecast_log`, half-hour points, 36 hours, about 4 KB an entry, written inside the plan-publication transaction and not part of the replan triggers). The dotted line appears once a forecast about 24 hours old has been logged, so it starts one day after this release.

## Release 0.5.21 (Stage A completion: A3, A6, A9)

- **A3, a charge level per cheap window.** The planner first finds the best single level, then tries every 5 % level for one window at a time and keeps any strict improvement, repeating until a pass changes nothing (at most four passes). The independent check recomputes the published levels and refuses them unless they beat the best single level, are reachable, and no one-window change is cheaper. The plan carries `window_targets`; the dashboard shows "then 85 % (Fri 23:30)" after tonight's level. The plan file still carries only the next window.
- **A6, forecast accuracy.** At 18:00 the system saves, once and never overwritten, tomorrow's solar forecast (already done) and now also tomorrow's household-demand forecast (`demand_forecast_for_day:<date>`, half-hour kW). When a day finishes, its record gains `forecast_review` (solar and demand: forecast kWh, measured kWh, error kWh and %). Records are version 3, so the last three days are rescored on first run. The history page shows each day and a rolling average (bias and typical miss); the dashboard savings panel shows yesterday's figures. Demand reviews start the day after this release (the evening-before forecast has to exist first).
- **A9, owner controls on the dashboard.** *Pause automation* (until a time, or until resumed): the plan file says "normal" with source `owner_pause`, so no charging is requested; advice is still worked out and shown. *Override* a charge level (not below the minimum level, up to 14 days): the planner's only choice is that level for every cheap window until the end time; the plan expires with the override. *Return to automatic* clears both. *Plan a visit or time away*: Away or High between two London times, using the existing schedule setting (it replaces the current household choice; outside the dates the house is Normal). Settings: `automation_pause`, `manual_override`. None of this sends commands in shadow mode, and the 45 A and car-charging protections are never paused.

## Release 0.5.22 (B3: agreement with the Anker)

- Each finished day (scored with the daily record, shortly after midnight, last three days) the shadow worker compares, minute by minute, what the Anker actually did (from the battery, grid and house readings: grid charge, solar charge, discharge, idle) with what the controller would have asked (from its command change log `control-commands.jsonl` and its rotated `.1` file next to the database). A minute agrees when a requested grid charge was happening (or the requested level had been reached), when no charge was requested and the Anker was not charging from the grid, and when discharge was not being inhibited while the Anker discharged. Minutes with no controller record or no readings are counted separately, never as agreement.
- Stored as `validation:<date>` (small, kept long term; the minute detail is purged after a week). Every run of differing minutes is listed with start, end, kinds, what each side did, battery kW and level.
- Where to look: History page, "Agreement with the Anker"; `/api/validation?days=7`; `/validation.csv` (every stored difference); `solarbank validation-report --days 7` on the Pi (also scores any unscored days). The controller's change log keeps about 4 MB plus one rotated file (weeks of shadow evidence).
- Expected first findings: a start later or earlier than the Anker's own on the first night, and the minutes when the Anker tops up while the controller has already withdrawn its request.

### Known Anker app limitation (owner, 29 October)

The Anker app (software, not the battery) cannot take half-hour slots. Its own scheduled grid charge therefore starts on the hour (00:00 on the 23:30 cheap rate), which is why the B3 report shows the Anker not charging from 23:32 to 00:00 while the controller would have asked for it. That difference is expected under the Anker app's schedule and is not a fault; it disappears only when the controller applies the level itself, because the battery has no such limit (Stage C, to be decided). Until then the planner's window (23:30 to 05:30) is slightly more generous than the Anker can use: the cost estimates assume charging can begin at 23:30.

## Release 0.5.23 (cheap window from the Octopus API)

- The cheap-rate window (23:30 to 05:30 for Intelligent Octopus Go) is now read from the Octopus product, not only from the Settings page. Octopus publishes the day and night rates as data but states the window only in the product description, so the provider worker fetches `/v1/products/<product>/` (public, no key, once per price refresh) and accepts a window only when the text names exactly one half-hour-aligned range of 1 to 12 hours. It is saved with the price cache (`night_window`). A failed lookup or unclear text changes nothing: the window typed on Settings keeps applying.
- One helper (`tariff_window.effective_tariff`) is used everywhere: the planner's price intervals, the High-demand overnight policy, the savings pricing, the dashboard chart bands and the Settings page, which now shows where the window came from. The saved Settings tariff is never overwritten.
- The planner still assumes charging from the start of the Octopus window (23:30), as decided; the Anker app's whole-hour limit is a separate matter for Stage C.

## Release 0.5.24

- A write that finds the database locked for longer than its wait now answers 503 "The database is busy for a moment. Please try again." (with Retry-After) instead of a bare server error, for every saving endpoint. The pages retry once automatically after 1.5 seconds and show a plain message if the server fails for any other reason. Other database errors are still reported as errors.

## Release 0.5.25 (C1: a read-only look at the Solarbank's settings)

- New command `solarbank probe-settings`. One read prints the device model, the operating mode (register 10064, vendor labels: self_consumption, tou_mode, third_party_control, custom_mode, socket_overlay_mode, smart_mode, dynamic_pricing), which modes the device offers (0x8006, 32774), the capability mask (0x8007, 32775: a refusal is reported as "this firmware may not implement it"), and the four limit registers (60000 charging limit, 60001 discharge limit, 60002 backup reserve, 60003 backup enable), plus the raw documented holding blocks 10060-10072 and 10074-10081 (never interpreted). `--watch 60` keeps reading every 60 s and appends only changes (plus an hourly heartbeat line) to `device-settings.jsonl` next to the database.
- Every address is in the vendor's pinned read map (a test checks it). The Modbus wrapper gained one more read, function code 3; it still has no way to write. Nothing writes to the device, and the setpoint register 10071 is never touched.
- **Important (found 29 Oct):** the Solarbank serves only a few Modbus clients, and the Pi already holds three connections to it (controller, collector and the old monitor), so a separate fourth connection for `probe-settings` was closed by the device. The one-shot command therefore only works when a slot is free (for example once the old monitor is retired). The always-on look runs through the collector's own connection instead (release 0.5.26).
- How to run on the Pi (needs a free connection slot): `sudo -u solarbank-optimiser env PYTHONPATH=/opt/solarbank-cost-optimiser/current/app /opt/solarbank-cost-optimiser/current/.venv/bin/solarbank --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 probe-settings`.

## Release 0.5.26 (C1 through the collector's own connection)

- The collector now looks at the Solarbank's settings registers (mode, mode mask, capability mask, limits, the two raw documented blocks) through the connection it already has: one register item per poll in rotation, a complete look every 60 s, and a line in `/var/lib/solarbank-cost-optimiser/device-settings.jsonl` only when something changes (plus an hourly heartbeat). It cannot affect the live readings: failures are caught, and after a link-level failure of a settings read the collector leaves the settings alone for ten minutes. Unit flag: `collect --settings-log PATH`.
- To use it: switch the Anker app between modes and read `device-settings.jsonl` (`sudo tail` on the Pi); each mode change shows up as one "changed" line with the decoded names.

## Release 0.5.27

- First real readings of the Solarbank's settings (29 Oct 21:30 UTC; the Anker app was reportedly in its AI/Smart mode): model `AE103`; operating mode 6 = `smart_mode`; the device offers self_consumption, tou_mode, custom_mode, smart_mode and **third_party_control** (mask 0x8006 = 47; it does not list dynamic_pricing or socket_overlay); the capability mask (0x8007) = 15, so all four limit registers are supported; charging limit 100 %, discharge limit 10 %, backup reserve 10 %, backup enable off. Registers 10060 and 10061 together advance one a second and read as Unix time (apparently the device's own clock, about 9 s behind the Pi when read): kept in the raw record, left out of change detection, so the log only records real changes.

### C1 test plan (owner, 29 October)

Switch the Anker app to Time of Use, note the time, then back to Smart (AI) and note that time. Dynamic Tariff is not tested: changing it makes the owner re-enter the Octopus key in the Anker app, and the device's mode mask does not list it. The log to read is `/var/lib/solarbank-cost-optimiser/device-settings.jsonl`.

### C1 test plan (owner, 29 October)

Switch the Anker app to Time of Use, note the time, then back to Smart (AI) and note that time. Dynamic Tariff is not tested: changing it makes the owner re-enter the Octopus key in the Anker app, and the device's mode mask does not list it. The log to read is `/var/lib/solarbank-cost-optimiser/device-settings.jsonl`.

## Release 0.5.28 (A11: weekly full charge for battery calibration)

- The planner now tracks when the battery was last at 100 % (the one-minute readings for the last week, then the daily record's `first_full_at` for older days; solar fills count). When that is `max_days - 1` days ago or more (default 6 of 7), the first cheap-rate window is treated like the High household mode's full overnight charge: a forced 100 % target that the independent checker verifies like any other. If the window is too short or the full charge proves unreachable, the plan is made without it and tried again at the next replan (the plan records why). Until the system has watched for a week it says "unknown" and forces nothing; never having seen a full charge after a week's watching counts as due.
- Settings page: "Battery calibration", full charge at least every 3 to 14 days (default 7). Dashboard: the Tonight line shows "last full charge N days ago (due)" or "full charge for battery calibration". `calibration.py`, setting `calibration`.
- Why: SOC Calibration in the Anker app also does this, at full power regardless of price. The owner will turn it off once this has run reliably, which has to happen before the controlled trial (C3), because SOC Calibration outranks third-party commands. Leave it on until then.

## Release 0.5.29 (vendor reference and adapted helpers)

- Documentation: `ANKER_INTEGRATION_NOTES.md` explains how Anker's official Home Assistant integration works, lists every function we copy from it and where it came from, and gives the procedure for taking an upstream update. `CODE_REUSE.md` points to it.
- Reference: the vendor's 26 files are kept byte-for-byte in `reference/anker-solix-official/source/` with hashes (`source-manifest.json`); `scripts/check_anker_upstream.py` reports whether upstream has changed (it needs the network and changes nothing).
- Code: `devices/anker_vendor.py`, pure functions adapted from the vendor (register decode/encode, set-point sign, identity from the part number and serial number, mode and limit rules, the charge and restore sequences as data). It cannot open a connection or send anything. The settings probe now takes its mode and capability labels from it. Our Solarbank's part number (`AE103`) hashes to exactly the pinned map's file name, which confirms the map matches the device.
- Nothing about the live behaviour of the equipment, planner or controller changes.

### Night of 29 to 30 October (first Time of Use test), read from the logs

Mode changes by the owner: Time of Use 22:31, back to dynamic (register value 6) 22:41, Time of Use 02:08 and 02:14, dynamic again 04:34 (owner to confirm that last one). In dynamic mode the Anker began charging at 00:00 at 2.49 kW (the app takes whole hours), slowed to about 0.5 kW from 02:00 to 02:08, and returned to 2.49 kW when switched to Time of Use; it reached 100 % at 02:50, then stayed idle (no discharge, no import beyond the house load) until about 05:10. The controller in shadow asked for 2.5 kW from 23:30, tracked the Anker's taper from 02:44 and recorded "target reached" at 02:49:17. The dashboard's decision line reads "Discharge allow" next to the AC charge state: this means discharge is not being blocked, not that the battery is discharging (wording to be improved).

## Release 0.5.30

- The minimum battery level setting is now limited to **5 to 20 %** (it was 5 to 30 %): the device's own discharge-limit register accepts only 0 to 20 %, so a higher floor could not be matched by the firmware. A stored value above 20 % is ignored and the 10 % default applies. Settings page, API and tests updated.
- Documentation only otherwise: the owner's control-specification review (`reports/control-spec-review-2026-10-30.md`), the Octopus car-allowance and bonus-rate rules in the specification (section 7.2), and the owner's decisions in the plan.

## Release 0.5.31 (missing data is missing data)

- The car-charger rule changed on the owner's instruction. The Shelly occasionally misses a reading (about every 30 to 60 minutes on 29 October), and each miss used to block discharge for a full minute: 21 minutes on 29 October, all from this cause, and in live mode each would have switched the battery to hold and back. Now a missing, stale or invalid charger reading never means "car charging". A gap of up to 10 seconds changes nothing (the last reading carries on, with no note); a longer gap inhibits discharge only if the car was known to be charging when last seen (for up to 10 minutes, then released), or during the first 60 seconds after a restart while the charger has not been seen idle. Otherwise discharge stays with the Anker and the dashboard shows a note ("treated as missing data, not as a car charging") instead of a restriction. A returning reading after a long gap still starts the 60 second idle confirmation. The battery reserve floor and the garage limiter are unchanged.

## Release 0.5.32 (hold idle after the target)

- Shadow controller only: inside a charging window, once the battery reaches the level the planner chose, the controller now **holds it idle until the window ends** (charge withdrawn and discharge inhibited; in the live design this is third-party control at 0 W) instead of handing it straight back to the Anker. When the window ends the plan turns to "normal" and discharge is allowed again (self-consumption in the live design). A 3 % band below the target prevents restarting and stopping around it. This is what the planner's cost model already assumed (no discharge at the cheap rate). The reasons shown on the dashboard read "target reached: holding idle until the cheap window ends".
- Owner decisions behind it (30 October): charge at full power then idle, never spread the charge over the window (idle uses less energy than low-power charging); the sequence ignores car charging events (no EV yet). The agreement report will now show the Anker discharging during the held period as "discharge inhibited" differences, expected until control is live.

## Release 0.5.33

- Dashboard battery card: the fixed text "Reserve 5%" and "Solar ceiling 100%" is replaced by "Minimum level N%", filled from your setting (10 % by default). The planner and controller always used your setting; only this label was out of date. The "Solar ceiling" figure had no data behind it and is removed.

## Release 0.5.34

- Dashboard controller panel: the "Decision" line now says what the battery is (or, in shadow mode, would be) doing in plain words and which device mode that is: "Charging from the grid at up to 2,500 W, to 70% (third-party control, mode 3)", "Held idle: ... (third-party control at 0 W, mode 3)" with the reason (planned level reached, minimum battery level, car charging), or "Self-consumption (mode 0): the Anker runs the house and discharges as needed", or "Handed to the Anker app". It replaces "Discharge allow · AC charging stopped · request discharge ok", which described internal command names and read as if the battery were both charging and discharging. The command table below it is unchanged.

## Release 0.5.35

- Charging advice chart: the dotted blue line (what the plan expected) now uses the logged forecast closest to 24 hours old that is at least three hours old, instead of waiting for one within half an hour of 24 hours. With the log only a day old it therefore shows from the first usable forecast (20 hours old at release time) and will lengthen to a full day by the next afternoon. A line under the chart says which forecast it is ("the plan made at Thu 15:38"). A forecast younger than three hours is not drawn against what happened.

## Release 0.5.36 (deployed 31 October 2026, 13:12)

Fewer disk writes, and the controller no longer depends on the planner renewing its plan every few minutes. Prepared after the 30–31 Oct storage stall; it
changes how often things are written, not what the controller decides.

- **Working records held in RAM.** The slot lists (`cache_slots`, `established_slots`, `provider_slots`), the planner's last attempt (`shadow_attempt`) and its
  current plan (`shadow_plan`) are kept in `/run/solarbank-cost-optimiser/` (RAM) and copied to the database at most every 30 minutes and when the providers or
  planner service stops. Before: about 2,100 settings rewrites a day (each also a version row and an event) from the slot lists alone. A reboot loses at most 30
  minutes of these records; the services refill them within a minute.
- **Unchanged slot lists and plans are archived once per half hour** instead of every minute / every ~4 minutes (a plan archive is about 240 KB, a forecast archive
  350 KB). A plan is archived straight away when its window, target, per-window levels, owner override or calibration charge changes.
- **Forecast refresh every 30 minutes** (was 15); forecast shown as current for 40 minutes (was 20).
- **The controller keeps the last valid plan in memory.** If the plan file expires or goes missing (planner or disk stalled, as at 00:05 on 31 Oct) while a charge is
  inside its planned window, the controller keeps following that window and target until the window ends or one hour after the plan expired, whichever is first.
  All other safeguards still apply (45 A garage limit, minimum level, car, stale readings). A corrupt, oversized or future-dated plan file is never held over, and
  neither is a hand-over. A restarted controller starts with no plan and stays restricted, as before. **The one-hour limit (`PLAN_HOLD_S`) is a proposal for the
  owner to confirm.**
- Not changed yet: the collector still writes once a minute, because the planner reads the battery level from the database. Moving that to the RAM copy would allow
  half-hourly collector writes; proposed as the next step.
- Check after deploying: `ls /run/solarbank-cost-optimiser/` shows `ram-*.json` files; `sudo cat /proc/<pid>/io` write_bytes for the providers and planner services
  grows far more slowly than before (before: about 12 kB/min planner, 35 kB/min collector, plus the archives).

## Pi storage incident, 30–31 Oct 2026

Overnight the Pi's single USB disk (WD Passport 1 TB, 5400 rpm) stalled from about 02:18: PostgreSQL logged
24 "autovacuum worker took too long to start" warnings, "too many clients" at 03:37/04:48/05:54/06:06, and a
7.5 minute checkpoint at 05:03. The Pi was hard-reset at 10:57. The night's charge cannot be judged: no usable
readings between the 23:30 ramp (to 2,500 W for the 75 % target) and 04:48, and the plan showed "expired" at 00:05.

Checked afterwards (baseline 31 Oct 11:09): SMART PASSED, no bad sectors or interface errors, 17,091 power-on
hours, Power_Cycle_Count 3685, Load_Cycle_Count 869,918 (health value 1; rated about 300,000), APM already 254.
Throttling flags clear. Changes made on the Pi: smartmontools and sysstat installed and enabled, journal made
persistent (100 MB cap, `/etc/systemd/journald.conf.d/persistent.conf`). To check later: whether Load_Cycle_Count
is still rising (`sudo smartctl -A -d sat /dev/sda`), and `sar -d` around any further stall.
Open: replace the disk with an SSD (or move the controller database onto one) before unattended control;
check the disk's USB power path.

## Planned work after release 0.5.36 (not released)

The ordered work is in [POST_SHADOW_PLAN.md](POST_SHADOW_PLAN.md). Nothing below is installed yet; the sections above describe what runs today.

* **Shelly sample keys by role (WP25, built on the branch, not released).** `solarbank_ac` (Shelly channel 0, `em1:0`) and `car` (channel 1, `em1:1`) replace `ch1` and `ch2` in the code (`shelly_channels.py`); the original monitor's own names are translated in one place. The stored source label of the two Shelly observations changes from `monitor:shelly:ch1` and `ch2` to `monitor:shelly:solarbank_ac` and `monitor:shelly:car`; nothing selects on it. After deploying, check that the car card shows the charger's standby (a few watts) and the Solarbank AC figure matches the Shelly app's Battery reading.
* **Controller state machines (revised approach, 2 November 2026).** The decision logic is rebuilt as explicit state machines using the `transitions` library, following the decision table of the owner's functional specification v1.3. The battery never discharges in a cheap period. Issues #26 and #29.
* **Fixed-size telemetry logger.** Per-second readings, one-minute observations and charger coverage move to Whisper files in `/var/lib/solarbank-cost-optimiser/telemetry/` (work packages WP23a to WP23c, issue #16). The first release to add `transitions` or `whisper` installs a fresh environment on the Pi, because the lightweight code update cannot add dependencies.
* **Storage guard replaced** by an alert and an emergency purge that never stops the controller (issue #8), and **off-device backups** (issue #13).

### Controller stopped in HANDS_OFF (GitHub 39, 40, 41, 42)

* The controller raises an alert file for anything its action machine wants you told (`machine-hands-off`, `machine-verify-failed`, `machine-default-mode-refused`, `machine-transport-error` and so on); `sudo bash pi-tools.sh status` lists them.
* To restart a controller that stopped itself: `sudo solarbank control reset`. The controller honours the request only if it is newer than the stop, unused, the device settings pass the start-up checks and the readings are valid; it then runs the start-up checks again before any write. A refused request raises `machine-reset-refused`.
* If the device refuses the default mode at release, the controller writes mode 0 (self-consumption) instead and says so.

### Cleaning out the SQLite database before go live (GitHub 46)

1. `sudo bash pi-tools.sh db-report`: rows and megabytes per table, so you can see what the file is made of.
2. `sudo bash pi-tools.sh db-clean`: a dry run. It says how many archived snapshots, raw snapshots, one-minute rows and old setting versions would go, and whether the telemetry files agree with SQLite (the clean-out refuses if they do not: run the backfill until it says OK).
3. `sudo bash pi-tools.sh db-clean-apply` in the quiet hours: a verified backup (`backups/before-clean-<time>.sqlite3`), then the removal, then compaction (it needs free disk about the size of the database; the Pi has 800 GB free). Settings, setting history of owner settings, daily and half-hour records, charger sessions and energy, imports and the telemetry files are never touched. The last two days of minute rows and a day of raw snapshots stay.

### Confirming the devices and the home network prefix (GitHub 45)

* **Device identity.** The first successful read of each device is remembered as "recognised", but the controller writes nothing to the battery until you have confirmed what it learned: `sudo solarbank devices show` lists each device ("recognised, NOT yet confirmed by you" until you confirm), `sudo solarbank devices confirm solarbank` confirms the Solarbank. The Settings page shows the same. After replacing a device on purpose, `sudo solarbank devices accept NAME` forgets the old identity; confirm the new one when it has been read. Do the confirmation before the commissioning tests (it is a step in G1).
* **IPv6 prefix.** The home network's IPv6 /64 lives in one file, `/etc/solarbank-cost-optimiser/lan-prefixes` (one prefix per line). The dashboard reads it, and the deploy turns it into nginx's allow list (`/etc/nginx/solarbank-optimiser-lan.conf`). If the ISP changes the prefix: edit the file, then `sudo systemctl restart solarbank-optimiser-web` and re-run the deploy (or `sudo nginx -s reload` after regenerating the include).
* **Not built, and retired (owner, 5 November):** `solarbank db drop-minute-tables` (WP23c). `db clean` removes the old minute rows after a verified backup, and the collector writes no reading rows any more, so nothing is left for it to do; the empty tables stay in the schema.

### Readings are in the telemetry files only (5 November)

The database holds **no readings**. The collector commits the raw response, the event and its health to SQLite; the readings (every metric, every second) go to the live file in RAM and to the telemetry files. "Last known reading" on the dashboard and the battery level the planner starts from come from the live file, and, when the Pi has just restarted and the file is empty, from the last valid second in the telemetry files. The history export (`/api/history`, `/history.csv`) reads the telemetry files, with any rows the database still holds from earlier releases merged in (a minute in both is given once).

Rows an earlier release wrote are removed by `solarbank db clean` (all but the last hour, after the agreement check passes) and by the retention job. This release records itself as the oldest one `switch-release.sh` will accept (generation 2 in `deployment/OLDEST_COMPATIBLE`): an older release would find no readings in the database and show none.


### Logged-mode soak: seeing the register writes before anything is sent

In `shadow` mode (the normal one today) the controller prepares commands and never reaches the register level, so there is no register journal. `logged` mode runs the real action machine and writes every register write it **would** make to `/var/lib/solarbank-cost-optimiser/control-writes.jsonl`. Nothing is sent: the writer is not attached, and `live` is still closed behind its three root-owned files.

* Start: `sudo bash pi-tools.sh logged-on` (sets the mode file to `logged`, restarts the controller, and prints what the controller says it is doing). It only ever sets `logged` or `shadow`; going live is a separate step.
* Read it the next morning: `sudo bash pi-tools.sh writes` (or `writes 20` for more). It says how many would-be writes there were and in which registers (10064 operating mode: 0, 3 or 6; 10071 battery set-point in watts), why none were sent, and the newest few. **A `wrote` or `failed` line is flagged loudly and the command exits 1:** that would mean a write reached the battery, and go-live stops until it is understood.
* Stop: `sudo bash pi-tools.sh logged-off` (back to `shadow`).
* The machine starts only when the controller has been told what to do and the battery's settings are in `device-settings.jsonl` (the collector writes it, and the controller refuses one older than two hours; GitHub 51 tracks making this check stricter before any live write). If `logged-on` shows "staying in shadow", the note says why.
* **What a clean night proves, and what it does not (GitHub 58).** It shows the controller's decisions and the nominal register sequence (0 W, then mode 3 and a set-point while charging; 0 W, then mode 6 on release). Logged mode sends nothing, so it does **not** exercise: the identity check before a write, the write-switch gate, mode-reversion supervision and HANDS_OFF, the physical-effect checks, or the handling of a failed Modbus write. Those are commissioning tests on the real device. A power cut leaves the battery on its last set-point (no software can run then): that too is a commissioning test, not a software protection.
* What to check in the morning: the mode writes follow the plan (3 only in the cheap window while charging or inhibiting, and 6 or 0 on release); set-points stay within 0 to 2,500 W; no write repeats every second; the set-point returns to 0 W before every mode change.

### Review fixes of 6 November (GitHub 41, 46, 49 to 60)

* **Start-up and running checks read the battery themselves (51).** The controller reads the mode mask and the Backup Reserve switch from the Solarbank every 30 s on its own connection and uses those readings, not the collector's settings log. The log is only a fallback and is refused when older than 3,900 s. If Backup Reserve is switched on in the Anker app while the controller is running, it stops (HANDS_OFF, the protective 0 W) and raises `machine-safety-violated`.
* **Repeated transport failures end in a release (41).** The number of consecutive failing cycles is in `control-loop.json` (`execution.transport_failures`); after five the controller releases the battery once (0 W, then the default mode) and raises `machine-transport-failing`.
* **A write goes to the address that was verified (50).** The identity check is bound to the address it was made at; the writer is handed that address and uses it, and never looks the device up again.
* **The HANDS_OFF reset request is an authorisation (57).** `sudo solarbank control reset` writes `/etc/solarbank-cost-optimiser/control-reset-request.json` (root's folder); the controller believes it only if root owns it and nobody else can write it, and records the id it answered in `control-reset-used.json`. This protects against accident and faulty processes, not against a compromised service account (the latch itself lives in the service's folder).
* **`db clean` compares every metric it can delete (53).** Counted metrics against their count files, the others by value (a counter kept as the largest value of the minute is judged between SQLite's mean and maximum). `sudo bash pi-tools.sh verify-clean` compares the newest `before-clean-*` backup with the telemetry files, read-only, to show that what the first clean-out removed is in the files.
* **`switch-release.sh` guards itself (54, 56).** It reads the Pi's test report and refuses a release whose tests failed (exit 5) or that has no report (unless `SOLARBANK_ALLOW_UNTESTED=1`, for a rollback to an older release); and if the activation fails part-way it puts `current` and the rollback floor back before starting the services.
* **`db clean` and the other services (46).** The clean-out lets the collector and the planner keep running (SQLite in WAL mode with a busy timeout): this was decided for availability, and the cost is that a very busy moment may make it wait. Run it in the quiet hours.
* **Before every push (60):** `sh scripts/check-all.sh` runs lint, the documents check and the whole suite including the JavaScript tests. The Pi's deploy gate runs the tests only: no `ruff`, and the JavaScript tests need `node`, which the Pi does not have.

### Performance review (6 November 2026): what was measured, what was found, what to run

**Disk writes.** The write sources were measured twice. On the Pi on 28 October (above, `/proc/<pid>/io`): collector 4.8, providers 4.1, planner 2.3, **11.3 KB/s in all, about 1 GB a day**, after the change detectors were fixed; the live `iostat` of 6 November (8 to 14 KB/s) agrees. In the collector alone, replayed with `strace` on 30 simulated minutes of production settings (one commit a minute, telemetry flushed every 20 s): **about 1.7 MB an hour into the SQLite log, 1 MB an hour into the telemetry files, 2 fsync an hour**; the live file is 21 MB an hour but lives in `/run` (RAM). So a sustained 5 MB/s from the collector is not what steady running does. The since-boot average of about 250 KB/s is dominated by one-off jobs: every deploy (a new virtual environment of about a gigabyte, then 18 minutes of tests), every backup (a copy, a compressed archive, an unpacked copy and an integrity check), the clean-out's vacuum, and the first creation of the 21 telemetry files (120 MB, in one burst). The disk is a spinning one with no wear limit; what it needs is that those jobs do not overlap, not that the steady write rate falls further.
**Memory.** Importing the web, planner, collector and controller code costs 24 to 43 MB each; the large figures on the Pi (web 213 MB, providers 161 MB, planner 113 MB) are runtime. The providers service imports pandas, scipy and pvlib the first time it computes the solar forecast (`providers.solar_forecast`) and keeps them (about 100 MB); the demand-rows cache held six lists of about 6.5 MB in each of the web and planner processes and now holds two (about 26 MB less in each).
**The status page.** It makes about 56 separate settings reads per build and takes 0.3 s on a quiet Pi, more when the disk is busy. It is now served from the previous picture while the next is built (GitHub 61); where the build's time goes is measured on the Pi, not guessed: `sudo bash pi-tools.sh profile-status`.
**Run, in this order, when the Pi is quiet:** `sudo bash pi-tools.sh io 120` (disk writes per service over two minutes, with the per-day equivalent), `sudo bash pi-tools.sh memory` (RSS, PSS and swap per service), `sudo bash pi-tools.sh profile-status`, and `sudo bash pi-tools.sh prune-releases` (a dry run: lists old release folders and the space they hold; `prune-releases --apply` deletes them, keeping the current release, the rollback floor and the newest three, and never a folder touched in the last two hours). These measure only: nothing here writes to disk while it measures.
**The old Anker energy monitor.** Nothing in the running system needs it (the controller owns the meter and writes `meter.json`). `scripts/rollback-cutover.sh` and `scripts/check-meter-ownership.sh` still mention it: stopping it is fine, deleting it would remove the documented way back to the original monitor.


## Normal household demand: seeded level, averaged profile, lighting diagnostic (6 November 2026, owner's instruction)

* **Seed.** Normal demand starts from the owner's October reference of 12.24 kWh a day (standard deviation 2.66 kWh over 12 days, 6-17 October). The planner's daily level is the mean of the last 14 good Normal days, the seed standing in for each of the first 12 days that have not happened yet, so the seed fades out over the first twelve measured days and the level then follows the house.
* **Shape.** The minute profile is the mean of the last 7 good Normal days (it was the last good day), scaled so the day totals the level; the scale is held within 30 % either way. Away and High are as before; High adds its allowance to this Normal forecast.
* **No temperature term.** Heating and hot water are gas. The owner's daily charts (26, 27, 28 October) show appliance events (washing machine, electric shower, cooking), not an evening lighting peak.
* **Lighting diagnostic only.** Each good Normal day also records the energy between sunset and 22:30 and its share of the day (`GET /api/demand-learning`, `normal.evening`: days, mean share, correlation of the share with the time of sunset). Nothing uses it. If a real trend shows over the winter it can be promoted through the E3 comparison.
* **The switch.** "Household demand learning" off returns the planner to the last good day, unscaled, at once.

**Stop timeouts (7 November).** At the 6 November 20:50 deploy the collector and providers were killed by systemd after exactly 30 s ("State 'stop-sigterm' timed out"). The collector's stop path joins two background threads for up to 15 s each before its final flush, so 30 s could never be enough in the worst case; the providers had printed "run complete" 29 s earlier and still had not exited (cause not yet found: look at what the process waits on at exit if it happens again). The collector, providers, shadow and web units now allow 90 s; the controller keeps 15 s on purpose (it must release the battery, not wait). Check after a deploy: `sudo journalctl -u 'solarbank-optimiser-*' --since '30 min ago' | grep -c stop-sigterm` should print 0. The `unit-failed-…` alert files are written by systemd's OnFailure hook and are not cleared by a later successful start.

## Review of the remaining pre-commissioning code (7 November 2026)

* **WP6, plan lifecycle:** built. The planner renews a plan before it lapses (`next_replan`: the earlier of four minutes and the plan's expiry less a margin) and deliberately does not chase a plan that ends with its window. The controller's own memory follows the last plan to the window end. Only the words were wrong: at 05:30 the log said "Plan has expired"; it now says "Cheap window ended".
* **WP7 / WP13, tests for issues 2, 3 and 5:** built and green (`tests/test_window_holds.py`, `tests/test_after_target.py`); no `xfail` remains. The three issues can be closed with those tests as the evidence.
* **D13, the device's own minimum level:** built. Settings shows the level the Solarbank itself holds beside the setting and flags a difference of a point or more (`tests/test_live_transport.py`).
* **DECISION_S:** the owner's specification evaluates the decision table every 10 s and at once when CHEAP, EV, VALID or BELOW_TARGET changes. The controller evaluates it every second, which includes every change at once. This is faster than the specification and is kept: the commands are logged only when they change, so the faster rate adds no writes.
* **WP21, identity, rediscovery and address editing (issues 14 and 15):** built. Identity is checked on the reader's own connection; a moved device is found through the neighbour table and a bounded scan and adopted only if its identity matches; Settings "Device addresses" asks for an address. mDNS is not used (see above): the neighbour table and scan find a moved device without it.

**A stale "unit failed" email (7 November).** The mail "solarbank-optimiser-collector.service entered the failed state" arrived at 08:50 although the collector had been running since 20:50:06 the evening before. It was the 20:50 stop timeout, repeated: the alert file written by systemd's OnFailure hook was never cleared, and the sender repeats a live alert every six hours (20:50, 02:50, 08:50, 14:50). The check that runs every two minutes now clears a `unit-failed-…` alert once that unit is active again. Until a release with this change is running, a mail of this kind can be checked with `systemctl is-active` and `NRestarts`, and the file removed with `sudo rm /run/solarbank-cost-optimiser/alerts/unit-failed-*.json`.

**Disk health check, 7 November 2026 (against the 31 October baseline).** Power_On_Hours 17,269 (17,091); Power_Cycle_Count 3,685 (unchanged: no unclean power loss since the 31 October reset); Load_Cycle_Count 869,921 (869,918: three in a week, so the APM setting has stopped the wear); Reallocated_Sector_Ct, Reallocated_Event_Count and Current_Pending_Sector all 0. `dmesg` since that boot shows no USB reset, I/O error or under-voltage line (only the `uas` driver registering at boot), and `vcgencmd get_throttled` is `0x0`. The drive is old (about two years of running) and past its rated load cycles, so the SSD stays a requirement before unattended operation (gate G3); it is not needed for supervised commissioning. Re-check with `sudo smartctl -A -d sat /dev/sda | grep -i -E "Load_Cycle|Power_On|Reallocated|Pending|Power_Cycle"`; a Load_Cycle_Count rising by more than a few a week, or any non-zero reallocated or pending sector, brings the SSD forward.

**A stale garage reading must not flip the mode (GitHub issue 62, 8 November).** On 7 November at 09:33:13 one garage-current reading was older than 2.5 s; the controller raised a protective `ac_charge STOP` ("Garage current unknown ... AC charging stopped") and the logged writes showed mode 3 then, a second later, mode 0, with nothing charging. A STOP now holds the battery (mode 3, 0 W) only when a charge was under way or the hold is already in force; with nothing charging it only keeps charging blocked until the reading is fresh again. A STOP during a charge is unchanged and immediate. Check on the next soak night: the `writes` journal shows no mode 3 / mode 0 pair outside a plan-driven change.

**Staleness limits are a setting (8 November, owner's option C).** `freshness.py`; Settings, "How fresh a reading must be"; `PUT /api/freshness`; carried to the controller in the plan file (`freshness`). Defaults: 2.5 s while a charge is under way or requested, 10 s (the specification's STALE_S) otherwise. A value out of range, or an idle limit tighter than the charging one, is ignored and the defaults apply. The status line "unknown" for a device uses the limit in force that second.

**Slot feed and slot report fixes (8 November).** (1) During the first bump charge the Octopus slot feed went from "ok" to `failed`, reason `invalid_response`, and stayed there (8 failures, retry hourly). The decoder rejected the whole dispatch list if any row looked odd, including the boost and test rows it never uses; now only smart rows are judged (any other type, including one Octopus may use for a bump charge, is skipped and never becomes a slot), and the four dispatch rejection codes (`invalid_dispatch_list`, `invalid_dispatch_interval`, `unknown_dispatch_type`, `invalid_dispatch_energy`) are kept as the stored reason instead of the generic one. If it still fails after the next release, the reason now says which rule. (2) `scripts/slot_report.py` read car power only from database rows that the single-store change stopped writing, so it said "car readings cover 0 half-hours" during a 7 kW charge; it now reads the telemetry files (and still any older rows). The slot report for the 8 November charge has to be rerun after the release.

**The "unit failed" alert for a timer-run job (8 November).** The 7 November fix cleared a `unit-failed-…` alert when the unit was active again. The off-site backup is a timer-run job: it is inactive, never active, after a good run, so its alert could not clear and would be mailed every six hours. The check now clears a `unit-failed-…` alert as soon as systemd no longer holds the unit in the failed state (`systemctl is-failed`).

**Off-site backup stall and slot-feed diagnosis (8 November).** (1) The 12:45 off-site backup uploaded nothing: `rclone copyto` of the 73 MB archive sat until the one-hour limit (`TimeoutExpired … timed out after 3600 seconds`); the 7 November upload of the same size took minutes. rclone is now given `--timeout 120s --contimeout 30s --retries 3 --low-level-retries 5`, so a stalled connection fails and retries within minutes while a slow upload that is still moving is never cut off. The next timer run is 12:46 the next day; a run can be started by hand with `sudo systemctl start --no-block solarbank-optimiser-offsite.service` and followed with `journalctl -fu solarbank-optimiser-offsite.service`. (2) On the first release carrying the dispatch-row fix, the Octopus slot feed still failed with the generic `invalid_response`, which means the failure is not one of the dispatch rejection rules (those are stored by name). The stored reason now includes the exception class for every provider, `invalid_response:<ClassName>` (never the message, which may hold account data), so the next failure says where decoding broke.

**The slot feed's real cause (9 November).** The feed failed whenever Octopus listed any dispatch (first at the 8 November bump charge, again when two real smart slots appeared at 04:55 on 9 November) and recovered whenever the list was empty. The raw rows, read from the Pi, were `{"start": "2026-11-09T04:55:00+00:00", "end": "…05:00:00+00:00", "type": "SMART", "energyAddedKwh": "-0.6154166666666666"}` and `{"start": "…05:00…", "end": "…08:00…", "type": "SMART", "energyAddedKwh": "-22.155"}`: **Octopus reports the energy added as a negative number**, and the decoder rejected anything below zero, so the whole list was discarded. (The earlier explanation, that a bump-charge row of another type broke it, was a guess and was wrong; skipping non-smart rows is kept because it is harmless.) The decoder now checks only the size of the energy (±1,000 kWh), never its sign, and keeps the named reason `invalid_dispatch_energy` for a value that is missing or not a number. The official `SmartFlexDispatch` reference gives `energyAddedKwh` as a Decimal with an example of "1.0" and says nothing about its sign; the real data is the authority. A 05:00 to 08:00 smart slot extends 2.5 hours beyond the 05:30 end of the standard cheap window: it is the car's slot, listed by Octopus at the cheap rate.

**The Normal default (9 November, owner's request).** `forecasts.demand_horizon(..., default_kwh=…)`: when the learning switch is on and no complete Normal day exists, the Normal profile is the seeded level (12.24 kWh a day) spread flat over the day; `reference_day` reads "default 12.2 kWh a day, flat (no complete Normal day yet)", the forecast's `default_used` is true, and the plan's assumptions carry a line starting "Normal demand is the DEFAULT". It stops the moment one complete Normal day exists (the profile, the 7-day average and the learned level take over as before). Switching "Household demand learning" off restores the strict rule.

**The car is not household demand, and the car is recorded apart (11 November, GitHub issue 74; owner's request).** On this installation `household_power` is the Solarbank's own load figure, which includes the car charger; the car is measured on its own on the Shelly's channel 1 (`ev_power`). The planner's Normal-day shape used the raw figure, so a long charge (63 kWh on 8 November) became "house demand" in the next plans. Now one rule, `minute_source.house_without_car`, gives household demand everywhere it is learned: the household reading less the car, minute by minute, never below zero; a minute with no car reading is left out (missing, never "car off", D15). It feeds the planner's demand rows (`demand_rows`, so the shape) and the learned level and evening figures (`demand_learning`). Leaving the car's minutes out instead was rejected: a reference day may have only 10 missing minutes, so every day with a long charge would have stopped counting. Replayed on the Pi's telemetry for 8 to 10 November: the planner's house use falls from 52.8 to 10.8 kWh (8 November) and from 74.4 to 35.2 kWh (9 November), every day still a Normal reference day, level and shape agreeing.

The daily record (version 4, so the last three days are re-scored once) and the half-hour record now carry the car beside the house, to check against Octopus's charge history and half-hourly figures: per half-hour `car_kwh`, `car_minutes_covered` and `house_without_car_kwh` (also the last three columns of `/half-hours.csv`); per day `car_kwh`, `car_cheap_rate_kwh`, `car_minutes_missing`, `house_without_car_kwh` and `house_without_car_minutes`. `consumption_kwh` stays the whole house, car included. The demand forecast review compares the forecast with the house without the car, and is left out when more than 10 minutes lacked a car reading. Expect one odd review: the forecast saved at 18:00 by the previous release still had the car in it. Not yet changed: the dashboard's measured household line still includes the car.

**The gate is staged by write class (11 November, GitHub issue 76; owner decision).** Seven of the required tests (T3a, T3b, T5, T6, T8, T12 and the supervised run) can only be observed while the controller drives the battery, so requiring all nineteen before anything may be sent made commissioning impossible in any order. Now each write class has its own stage (`live_gate.STAGE_TESTS`): `mode` needs read-registers, hands-off-latch, mode-write, default-mode, setpoint-zero-write, release, setpoint-both-signs (T2), t9-garage-ct-sign and t11-rms-current; `setpoint` needs those plus t3a-pv-not-full, t5-mode-change-timing and t12-mode-reversion. A write is sent only when its class is in the write-switch **and** its stage is recorded for this exact code; `LiveTransport._allows` checks both before every write, so listing `setpoint` early sends nothing more than `mode` (the protective 0 W still goes with `mode`). The full list is complete commissioning, the evidence for running unattended (G3, with the SSD). `commission status` gives `commissioned_classes`, `sent_classes` and `still_needed` per stage. The fingerprint, the three root-owned files and release-first-then-disable are unchanged.

**The Solarbank accepts three Modbus connections at most (measured 11 November, GitHub issue 71).** The collector and the controller hold two; live, the controller's writer is the third. Anything more is refused: a fourth read failed, and the T2 runner's writes failed while it held its own reader and writer beside a running controller. So **nothing else may connect to the Solarbank while the controller runs**: no extra diagnostic reads, other apps or integrations. The runner tests (`commission run`) now hold one connection of their own at a time (each read closes the test's writer first, each write closes its reader), so they fit beside the collector and a logged controller. Anker publishes no limit; worth confirming with Anker support, and rechecking after firmware updates.

**12 November: hand back to self-consumption; no smart-slot counting; the garage regulation holds instead of flip-flopping (GitHub issues 78 to 83).** These supersede the earlier sections on the default mode 6 and on the smart-slot allowance.

* **Hand-back mode 0 (issue 78; owner, 11 November).** In operation the controller uses only mode 3 (charge, hold at 0 W) and mode 0 (self-consumption). Every release, exit and fault hands the battery to **mode 0** (`action_machine.MODE_DEFAULT`); the owner is emailed about faults and resets the battery by hand in the Anker app. The old default, 6, is the vendor's **smart mode** (the code called it "Dynamic Tariff"), which fails on this installation with the Anker's smart-meter error. With mode 0 as the default there is nothing unproven to fall back from, so the `default-mode-unproven` alert is no longer raised (issue 83). The runners: `default-mode` and `release` expect mode 0; `read-registers` checks the specification's T1 (mode mask bits 0 and 5) instead of exactly 47 (the mask is 111 with Anker's dynamic pricing enabled) and accepts any operating mode; `mode-write` rewrites mode 0 or 3 as found, otherwise writes mode 0; T2 always hands back to mode 0.
* **No smart-slot allowance counting (issue 79; owner, 12 November).** A moment is a cheap smart slot when the car is measured charging inside a SMART slot on Octopus's current list, nothing else: Octopus manages the 6-hour allowance. Removed from the controller (`signals.SmartSlot`; `smart-slot-allowance.json` is no longer read or written, and `--slot-allowance` is accepted and ignored so the installed unit still starts) and from the planner (`apply_slot_rules`). The night of 11 to 12 November the count reached 13 half-hours for about five hours of charging (the car signal's 2-minute switch-off delay spilled each stop into the next half-hour): the controller called the 06:00 slot expensive and the planner dropped the 07:30 slot entirely, so it made no charge window for it (issue 81). The planner still counts the half-hour in progress only while the car charges, and assumes future listed slots are used.
* **The garage regulation holds when readings are not aligned (issue 80).** With the battery's reading not from the same moment as the garage's, its own current cannot be subtracted, and counting it twice cut the charge to about 550 W whenever the car charged, then put it back: over 100 flips a night at 42.5 A against the 45 A limit. Now, while the measured garage total (which already includes the battery) is within the limit, the set-point is held: never increased on an unaligned cycle, never cut. Over the limit, the conservative cut is unchanged.

**12 November: commissioning is a checklist, not a software gate (owner decision).** The gate is now the two root-owned files the owner sets: `control-mode` (`live`) and `write-switch` (`mode`, `setpoint`). `commissioning.json` stays as the owner's notebook: `commission pass … --note` and `commission run` still write each test with its date and note, a code change keeps the entries (the file records the fingerprint of the code it was last written on, and `commission status` shows `notebook_code_matches`), and nothing in it blocks a write. The checklist and its state are in `COMMISSIONING_PLAN.md`. **Consequence:** with the switches set, a newly deployed release sends writes as soon as it starts; the owner is present for every deploy and `sudo bash $T logged-on` stops writes. This supersedes "the fingerprint" and "its stage is recorded for this exact code" in the 11 November section on the staged gate.

**12 November: a charging window under way keeps its target (GitHub issue 85).** In the 14:30 daytime slot the battery started and stopped three times: each replan solved afresh, and with the slot and the night window both at 6.6p the least-cost target tipped between 30 % and 10 %. The planner's `plan.json` writer (`shadow.keep_window_under_way`) now keeps a window that has started: a replan may raise its target, never lower it, and may not drop the window before it ends. Pause and hand-over still act at once. The website now says "Live control" when the controller is live (it said "not commissioned" whatever the mode).

## Learned solar correction, shown beside the forecast (GitHub issue 94, step 2; built 19 November 2026)

**What it is:** every evening after 18:00, once tomorrow's solar forecast has been saved, the planner service asks a separate process in the solver environment for a **corrected** forecast, learned from how the saved evening-before forecasts compared with the solar actually measured. It is the method of EMHASS's "adjusted PV forecast" built from the same libraries (scikit-learn Lasso with time-ordered cross-validation; the forecast, the hour of day and the sun's elevation from pvlib), because the pinned EMHASS 0.18.4 could not be checked. Code: `pv_adjust.py` (history and checks) and `pv_adjust_child.py` (the learning, run by the solver environment's Python).

**What it is not (yet):** the planner does **not** use it. It is saved as `adjusted_forecast_for_day:<date>`, scored beside the uncorrected forecast when the day is over, and shown on the History page under "Forecast accuracy" as "Solar with the learned correction". Switching the planner over is a separate decision (issue 94, step 3), once the correction has beaten the forecast for a few weeks.

**History used:** up to 60 days of evening-before forecasts, hour by hour, from the 90-day minute archive. An hour is left out if fewer than 50 of its minutes were measured, if neither the forecast nor the measurement shows sun, or if the battery was full (99 % or more) at any minute in it: with export off, solar is held back then, and those hours must not teach the forecast to be low. It needs at least 7 days and 48 hours; until then the day is saved as `not_enough_data`. In winter that is quickly met, but the hours are few and dull, so expect the correction to mean little before spring.

**Safety of the result:** the corrected hours must match tomorrow's forecast hour for hour and lie between 0 and 2.2 kW (1.1 times the 2 kWp of panels), or the day is saved as `unavailable`. One attempt each evening; a failure does not stop the other evening jobs or the planning.

**To look at a saved correction on the Pi:**
```
sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/current/.venv/bin/python -c "import sqlite3; c = sqlite3.connect('file:/var/lib/solarbank-cost-optimiser/controller.sqlite3?mode=ro', uri=True); [print(n, p[:300]) for n, p in c.execute(\"select name, payload from settings where name like 'adjusted_forecast_for_day:%' order by name desc limit 3\")]"
```

## The website in the Anker style (owner, 19 November 2026)

**The home page** now opens on what the Anker app shows first: a battery ring with the level and what the battery is doing in plain words, small solar, grid and car tiles, one line for the controller's mode, tonight's charge in one sentence, the money (the last scored day, the last 7 days, since going live), and a day chart like the Anker's (Solar, House, Battery or Grid; tap to read a value; the cheap-rate periods shaded green; the battery level underneath; step back up to 90 days). Away, Normal and High are on the home page itself. The other sections the page showed before (pause and override, the controller and real-time safety checks, handing control to the Anker app, household plans and visits, the charging advice chart and the next 48 hours) are reached from the Settings menu (groups "Controller" and "Plans and forecasts"), each opening as its own screen with "‹ Settings" to go back; they still live on the home page, whose script fills them. The old cards that the new top repeats (battery level, price, car, tonight, savings) are no longer shown.

**On a phone** the links become a bottom tab bar (Home, History, Settings) and each page's heading is just its name, with the real-time pill in the corner. History keeps its content in the same rounded-card style. **Settings** is a menu like the Anker app's "Home Setup": grouped rows (Battery, Prices, Planning, System), each with its current value and an arrow, opening one screen at a time with "‹ Settings" to go back (`static/settings-menu.js`; the forms and how they save are unchanged).

**Expected against actual (19 November):** the Solar chart draws the forecast saved the evening before as a dashed line (and the learned correction, issue 94, dotted, once one exists for that day); its total reads "made · forecast", counting the forecast only up to the latest reading on a day not yet over. The battery-level chart draws, dashed, the level the plan made before that day expected, and for today, dotted, the newest plan. Plans are kept 36 hours (`soc_forecast_log`), so older days show no plan line.

**What is new behind it:** one read-only feed, `GET /api/day?day=YYYY-MM-DD` (`day_view.py`), giving a London day in five-minute steps from the telemetry files, its totals and its cheap periods. The top of the page is painted by `static/home.js` from the same `/api/status` as the rest; it sends nothing. The style is `static/app.css`. The planner and the controller are untouched.

