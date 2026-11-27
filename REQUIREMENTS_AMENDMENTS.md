# Owner-approved requirements amendments

These decisions supersede the corresponding statements in the original dated
specification and development plan. Those transferred documents and historical
source files remain unchanged for provenance.

## 25 October 2026 — High demand

Owner instruction: for extra use in High demand, simply fully charge the battery
overnight.

- High demand requests a **100% grid-charge target during the regular overnight
  cheap window**, currently 23:30–05:30 Europe/London. It no longer requires the
  owner to estimate extra kWh. Normal and Away retain their existing behaviour.
- A scheduled daytime visit affects the preceding overnight opportunity. An end
  date returns to Normal; an expired selection cannot request another full night.
- Apply the full target to the regular night window; it does not authorise peak
  charging or turn an unverified extra smart slot into a cheap interval.
- Manual handover/overrides, the 45 A limit, EV inhibition, battery limits, valid
  prices and commissioned controls still take priority. A 100% request is not a
  guarantee of reaching 100% when available charging time/power is insufficient.
- New selections use no extra-energy allowance. The old optional field remains
  readable for historical forecast compatibility; the website clears it when
  saving a new selection. Historical forecast issuances remain immutable.
- Requests are visible and archived separately from validated executable targets.
  Equipment commands remain disabled until the existing commissioning gates pass.

## 25 October 2026 — confirmed account prices

Owner confirmed **34.96p/kWh peak** and **6.60p/kWh off-peak**. These replace the
unconfirmed older export rates in the working fallback settings. The regular
23:30–05:30 London window is retained. No new quote or account API connection was
used to establish these owner-supplied prices.

## 25 October 2026 — private-network access and forecast reference

The owner requested removal of the dashboard login for the private home-network
Pi installation. Pi deployments now use explicit trusted-network access for
loopback, the verified 192.168.1.0/24 home LAN and private Tailscale addresses.
The backend listens on loopback, the frontend restricts source addresses, and
same-origin/CSRF checks remain required. HTTPS was used initially; the later
private HTTP decision below supersedes that transport choice.
Public Tailscale Funnel was disabled with explicit owner approval. Password
access remains the default for installations without the private-network flag.
This supersedes the original mandatory-login requirement for this installation.

The owner supplied a Normal household-consumption reference of 12.24 kWh/day,
sample standard deviation 2.66 kWh over 12 days, 6–17 October 2026, excluding the
18–24 October holiday, unfinished days and earlier days with large gaps. Use it
to sense-check complete Normal forecast days. This is an owner-provided daily
benchmark, not an hourly training profile, prediction interval or a target that
forecasts must be forced to match. Electricity supplied by PV/battery is included
in the household-consumption quantity. Missing eligible hourly history remains
unavailable; the benchmark does not close that acceptance gate.

## 25 October 2026 — HTTP access like the other Pi apps

The owner requested removal of the certificate warnings and confirmed that the
other apps use `http://solarbank-pi.local/`. The Solarbank frontend now uses HTTP
on port 8091 with no HTTPS redirect. Private-network access and request checks
continue to apply. HTTP has no certificate warning page, but the browser may
still display its ordinary “Not secure” label and LAN traffic is unencrypted.
The existing Octopus key stays private on disk. Replacement keys are entered
through SSH into `.env`; the API continues rejecting remote HTTP key submission.
This change affects website access only; equipment control remains disabled.

## 26 October 2026 — one-minute stored history

Owner instruction: control must be real time, but recorded history can be much less frequent,
to stop continuous mechanical-disk writes on the Pi.

- Collection and control stay at one second in memory and a RAM-backed live file; the
  database stores a one-minute log (last value, sample count, valid count, mean, maximum per
  channel). Energy accounting is unchanged; short peaks remain visible as the minute maximum.
- Status changes are kept at full resolution. Older one-second records are unchanged.
- Up to about two minutes of log can be lost on a power cut; the database remains intact.
  The future control loop must use the live readings, never the minute log.
- Shadow plans are stored once (with a digest) instead of twice. See OPERATIONS.md,
  "Disk activity and durability".

## 26 October 2026 — direct real-time acquisition

Owner instruction: this is a real-time control system. It must read the hardware directly and must
not depend on a database, or on the old energy monitor, to collect its data. Shadow mode must test this.

- The collector's HTTP bridge to the monitor was a development stopgap. It is replaced, as the data source,
  by direct read-only acquisition from the Solarbank, smart meter (including garage current) and Shelly.
  The monitor bridge remains only as a comparison and rollback source until cutover.
- Real-time decisions use in-memory live state only. The database is a log, never part of the control path.
- The monitor must keep running until the cutover gate in `REALTIME_ACQUISITION_PLAN.md` passes.
- This supersedes the Phase 2 wording in `DEVELOPMENT_PLAN.md` about adding a poller after resolving
  connection sharing; the plan and its pass points are in `REALTIME_ACQUISITION_PLAN.md`.
- Equipment writes remain absent; this change adds read-only acquisition only.
- Owner decision, same day: commissioning of the **garage current limit** (the meter's secondary CT, which is the PV/ESS
  feed to the garage) is deferred to the commissioning stage (Phase 5). The real-time acquisition work reads power only;
  garage current stays unavailable and the 45 A limiter stays inactive until then. This changes no safety claim.

## 28 October 2026 — comparison signed off

The owner confirmed the 24-hour side-by-side comparison (26 Oct 22:47 to 27 Oct 22:48) as passed against the plan's provisional thresholds. The original energy monitor keeps running until a cutover window and an independence period are agreed; the collector must first be switched to read the meter directly.

## 28 October 2026 — Normal reference day may have a few filled minutes

Specification 6.1 asks for the most recent complete London day. Releases and restarts leave small holes in an otherwise complete day: 27 October had 1 436 of 1 440 minutes, the
missing four being exactly the service restarts at 09:41 to 09:42, 19:53 and 23:00. With the owner's agreement a day now qualifies if at most **10 minutes in total** are missing
and **no gap is longer than 5 minutes**. Each missing minute takes the mean of the nearest present minutes before and after it (never zero), the number of filled minutes is
carried with the forecast (`gap_filled_minutes`, and in its assumption text), and a duplicate, extra, invalid, non-Normal or partly covered minute still counts against the day.
Both limits are provisional and the owner may tighten them. The owner also approved the 10 to 17 October seed history as a labelled fallback; it is not built because a live day now
qualifies, and it remains available if a longer outage ever leaves no eligible day within the seven-day window.

## 28 October 2026 — primary objective clarified; specification updated to version 1.7

The owner clarified that the key feature is deciding each evening how full to charge the battery (today it is charged fully every night; that is right in winter and
wasteful in summer). The Anker firmware keeps matching house load, grid and solar, and discharges to the house; the controller only applies the chosen maximum charge,
inhibits discharge while the car charges, and monitors the garage supply (50 A supply, 45 A operating limit; a safeguard). The one-second loop is required only for the car
and desirable for the garage. Holding idle once full inside the cheap window is wanted. The planner stays the optimiser (peak import costs far more than surplus cheap
energy), not a simple "demand minus solar" rule. Success is weekly pounds saved against a home with neither solar nor battery, with an optional cumulative total.
Consequences recorded in the code and plans: the controller no longer inhibits discharge for a missing plan, a "charging" plan or an unknown battery state (only for the
reserve, the car and an explicit hold). The specification now carries sections 1.1 and 10.1; version 1.6 is kept at `reports/` history (original text unchanged elsewhere).

### 28 October 2026 (later) — owner's answers on the charge level

The Anker app's charge limit applies to grid charging only (solar still reaches 100 %). Time of Use mode: maximum SOC minimum 80 %. Dynamic Tariff and Smart (AI) mode: any maximum
charge level. The summer needs the full range. Backup Reserve is not used. A 10 % minimum SOC may be kinder to the battery (to be confirmed before the model and controller change).
Cumulative savings count from the day the application goes live. The owner accepts several weeks of shadow running, noting that some solar is lost even on sunny winter days, so the
shadow comparison should measure that too. The specification (10.1) records the candidate mechanisms and that the choice is a commissioning decision.

### 28 October 2026 (evening) — three owner decisions

1. Minimum SOC is **10 %** by default, and must be changeable in the web interface (one stored value used by the planner and the controller).
2. After a controller restart, discharge is inhibited until the charger has been seen idle for **1 minute** (not 5). The same 60 s is used for the car card/history release delay, provisionally, until a real car charge can confirm it.
3. The original energy monitor is to be retired **as soon as possible**: it only existed to characterise the system. This requires the meter readings and the garage current to be read directly first (development plan B4).

### 28 October 2026 (afternoon) — keep the old monitor, without the meter

The owner wants the original energy monitor kept running for its savings history and its forecast-versus-actual solar comparisons, but it must never read the smart meter (those reads were only
for commissioning) and the two applications must have no dependencies on, or interference with, each other. Implemented on this side: the solver environment moved to `/opt/solarbank-cost-optimiser/solver-venv`,
the deploy script no longer requires the monitor, and a kernel-level block (`IPAddressDeny` for the meter's address) was installed on the monitor's unit. Still to do in the monitor's own project: deploy its
`METER_READS_ENABLED` guard (written and tested on its Mac copy) to the Pi. Only then is the monitor re-enabled.

### 28 October 2026 (later) — meter removed from the monitor's code; the rule stays until proven

The owner decided it is simpler to remove the smart meter from the original monitor's code altogether, since the Solarbank gives the monitor all the data it needs, rather than guard against it. This replaces
the earlier plan to deploy a `METER_READS_ENABLED` switch. The monitor stays stopped and disabled until that code is deployed. The kernel-level rule that blocks the monitor from the meter's address stays installed until
the cleaned-up monitor has been proven, and is then removed.

### 28 October 2026 (evening) — retention: half-hour record long term, detail purged after several days

The owner decided that a long-term half-hour consumption record matching the Octopus smart meter's periods is enough for analysis of overall performance, and that the detail records can be purged after several days.
Implemented as a permanent half-hour record plus a purge of detail older than seven days (the planner needs the last seven days of minute rows for its Normal reference day). This relaxes the specification's wish (section 14)
to keep "decision evidence" long term for every archived plan: the evening-before solar forecast, the daily record and the half-hour record are kept; the per-minute plan and forecast snapshots are not. Also recorded: the archive had
been growing by about 1 GB a day for want of a change detector that ignored the clock minute; that is fixed.

## 1 November 2026 — foundations, off-peak rule, car rule, fixed-size telemetry

Owner decisions from the post-shadow review (the ordered work is in POST_SHADOW_PLAN.md; designs in issues #16, #26 and #29):

- **Foundations first.** The controller as explicit state machines (#26) and database and record integrity come before other work.
- **Off-peak rule.** In a cheap period (the regular off-peak window or an active smart slot) the battery never discharges: it is either charging or held at zero output in third-party control, whether or not the car is charging. These are rules 1 and 2 of the decision table in the owner's functional specification v1.3. This supersedes the 28 October statement that the controller inhibits discharge only for the reserve, the car and an explicit hold.
- **Car rule.** Car detection works at any tariff (specification section 8). At the peak rate, while the car charges, the battery is held at zero output; off-peak the car and the battery may both charge, within the 45 A limit.
- **Fixed-size telemetry (#16).** High-rate measurement history uses Whisper fixed-size files instead of growing SQLite tables.

## 2 November 2026 — revised approach: `transitions` for control, Whisper for telemetry

- **State machines with `transitions`.** The controller's decision logic is rebuilt as explicit state machines using the `transitions` library, pinned. This is the one approved exception to the 27 October design rule that the controller uses the standard library only, apart from the device readers (DEVELOPMENT_PLAN.md, Phase 4b). The state table is the decision table of the owner's functional specification v1.3 (sections 5 to 11), which the owner confirmed is correct. Where the requirements or the code differ from it, the difference is an open decision in POST_SHADOW_PLAN.md (D7, D9, D13 to D16), not decided here. The `protective_decision()` function in `control.py` is an early offline model and is not authoritative.
- **Fixed-size data logger on Whisper.** Per-second readings, one-minute observations and charger coverage move from SQLite to Whisper files, written only by the collector. The 26 October one-minute stored history and the 28 October seven-day purge continue to apply until the migration (POST_SHADOW_PLAN.md, WP23a to WP23c) is complete. Afterwards minute detail is kept for 90 days in the fixed-size store, the SQLite minute and coverage tables and their retention code are removed, and the half-hour and daily records stay in SQLite as the permanent record. The real-time rule of 26 October is unchanged: control uses live readings, never the logger or the database.
- **Implementation notes for the functional specification v1.3.** Its behaviour is unchanged. Its section 15 layout (`logic/signals.py`, `logic/decision.py`, `control/faults.py`) is realised as declared `transitions` tables built through one helper, with `decide()` kept as a pure function. "Use the existing logger" applies to events, warnings and alerts; telemetry history goes to the Whisper logger.
- **Shelly channels and sign (established by the documents and the owner; formal commissioning is still pending, and the code labels the map `uncommissioned-channel-map` until then).** `em1:0` (`solarbank_ac` in the code) measures the Solarbank AC circuit and `em1:1` (`car`) the car charger (Shelly channels 0 and 1: the owner's commissioning note of 16 October, confirmed again on 2 November; `devices/shelly.py`). On the Solarbank channel positive power is output to the house and negative is AC input into the Solarbank, whether charging from the grid or its own standby draw (15 October sign review; the owner identified the -46 W seen at 02:06 on 2 November as that standby draw). Owner specification v1.3 (sections 2, 5.1 and 11.4) calls the car "CT 1", which matches the Shelly's channel 1, but calls the Solarbank "CT 2" where the Shelly's own number is 0, and it states the opposite sign for `P_batt_ac`; the code names the circuits by role (`shelly_channels.py`, 2 November) and only the original monitor's payload still says `ch1` and `ch2`, counting from 1, translated in one place; its checks are implemented with this installation's convention, `P_batt_ac` = minus the Shelly reading, so that CHARGE passes when `P_batt_ac` is about the set-point and ZERO_OUTPUT when it is below 150 W (a standby draw of about 46 W is inside that).

## 2 November 2026 — the owner's answers to the post-shadow decisions

Answers to the decisions in POST_SHADOW_PLAN.md section 7 (the ordered work, the reasoning and the questions still open are there):

- **A car now exists.** This supersedes "No EV yet" (30 October). Car rules, extra smart slots and the car-dependent commissioning tests (T3b, T8, T10) are no longer deferred, and no waiver of them is needed (D16). The car's link to Intelligent Octopus Go, which the smart-slot test T10 needs, is still to be confirmed.
- **No hand-back to the Anker app; the default mode is Dynamic Tariff (D17).** The Anker app's own settings, including its third-party-control switch, are changed by hand, so software cannot hand control back to the app. When the controller releases the battery it writes the default mode, Dynamic Tariff (register value 6 on this device), instead of mode 0. Whether the device accepts that write and then behaves as the app's Dynamic Tariff does is to be proved at commissioning; until then the fallback is mode 0 with an alert.
- **Invalid or stale data other than the charger (D9):** the controller resets the battery to the default mode. This replaces v1.3's hold at 0 W and the 28 October rule that an unknown battery state restricts nothing.
- **Car detection (D7):** the owner specification v1.3 thresholds apply to everything: charging is detected above 1,000 W for 3 s and ends below 300 W for 120 s, for the controller, the car card and the history. This supersedes the 10 W cutoff and the 60 s release delay of specification 8.1 and of the 28 October restart rule.
- **Minimum battery level (D13):** v1.3. The firmware owns the floor (register 60001, set in the app); the controller reads it and clamps the target; Settings shows it beside the setting and flags a mismatch. This supersedes the 28 October "adjustable in the web interface" and the 30 October 5 to 20 % setting as far as the controller is concerned.
- **Garage regulation (D14):** v1.3 section 9.
- **Charger reading gaps (D15):** the 30 October rule stays for the charger reading only (a gap up to 10 s changes nothing; a longer gap holds only while the car was known to be charging, for up to 10 minutes). Every other reading follows v1.3 11.2.
- **HANDS_OFF survives a restart (D18),** through a small latch file written by the controller and reset by the owner.
- **Three go-live gates (D19):** supervised commissioning, a limited first live step, and unattended operation.
- **Alerts (D1):** email through Resend, to be confirmed when the alert path is built.
- **Unchanged:** `PLAN_HOLD_S` stays at 1 hour (D4); APScheduler, SQLAlchemy Core and an EMHASS assessment stay deferred (D8).

## 3 November 2026 — the owner's answers on the hand-over (D17)

- **The evening hand-over stays mode 0 (self-consumption)** (D17 (a)). The default mode, Dynamic Tariff (value 6 on this device), is written only when the controller stops, restarts or faults, or the owner presses the release button.
- **A release in a cheap period, or while the car charges, is accepted** (D17 (b)): while the controller is not running the Anker's own mode decides, as it did before the controller existed. No hold at 0 W is added; the alert email reports a long outage.
- **Octopus schedules the charger, not the car.** Intelligent Octopus Go controls the charger, so it always lists about six hours of smart slots whether or not the car uses them (owner, 3 November). A slot counts as cheap only while the car is measured charging; what happens to the listed slots once the car reaches its target charge is not yet known and is to be measured (issue 34).
- **No mid-window target rule for now** (D3): the live target with 3 % hysteresis stands; a rule that lets a target only rise inside a window is a future enhancement, to be considered after the first shadow nights show whether the target still wanders.

## 6 November 2026 — Normal household demand: seeded start and an evening-lighting seasonal term

- **Gas heating and hot water.** Heating and hot water are gas, so household electricity does not follow outdoor temperature. A heating-degree term is NOT used. The seasonal effect that matters is lighting, which follows how early it gets dark.
- **Seasonal window: dusk to 22:30.** The seasonal adjustment is learned from the household energy between dusk and 22:30 (London time) on good Normal days. Outside that window the Normal profile is not seasonally adjusted.
- **Seed.** Normal demand starts from the owner's reference of 12.24 kWh/day (sample standard deviation 2.66 kWh over 12 days, 6–17 October 2026, holiday and gappy days excluded) and is then learned from measured good Normal days. This supersedes the 25 October wording that the figure is only a sense-check and not a training value: it is now the starting level, carrying the weight of the 12 days it came from, so measured days take over gradually. It is still not a prediction interval or a target the forecast must match.
- **Status:** requirement recorded; the build is part of DEVELOPMENT_PLAN E3 (weekly model comparison with promotion and rollback) and is not yet built.
- **Review the same evening (owner):** the Anker app's daily charts for 27 and 28 October (15.53 and 12.66 kWh) show no evening peak; the load is the washing machine, the electric shower and cooking, at irregular times. The evening-lighting seasonal term is therefore ON HOLD: first measure, from the telemetry files, how much of each good Normal day falls between dusk and 22:30 and whether that share moves with dusk. The seed (12.24 kWh/day) and the learned daily level stand.
- **Built (6 November):** the seeded daily level, the 7-day averaged profile and the dusk-to-22:30 diagnostic (diagnostic only, not used by the planner). The lighting term itself stays on hold. See OPERATIONS 'Normal household demand: seeded level'.

## 7 November 2026 — returning from Away: the reference-day age stays at 7 days for now

- The house has been on Away since about 1 November and the owner returns on 8 November. The Normal forecast needs a complete Normal day no more than 7 days old, so for about one to two days after the switch back to Normal the planner will report the household forecast as unavailable (the Normal days of 26 to 28 October are too old).
- **Decision (owner):** do not build a longer reference age. Leave the battery on the Anker's own control meanwhile (logged mode, nothing sent), and observe how the current algorithm behaves on the first days at home, including the first car charges. Revisit (a reference up to about 30 days old, scaled to the seeded level) only if the gap proves a problem.

## 8 November 2026 — how old a reading may be (owner, option C)

- Owner specification v1.3 sets `STALE_S` = 10 s ("review after commissioning"). The controller had used 2.5 s for every device, four times stricter, and one slightly late garage sample (7 November 09:33) was treated as a lost reading.
- **Decision (owner): make it a setting, defaulting to two limits:** 2.5 s while a charge is under way or requested (the garage current sets the charge power) and 10 s (the specification's value) otherwise. Settings page "How fresh a reading must be"; allowed 1 to 10 s and 2.5 to 30 s, the idle limit never tighter than the charging one. The planner writes the values into the plan file (as for the minimum level); a plan file without them means the defaults.
- Garage regulation itself runs only while the action is CHARGE (specification section 9), as GitHub issue 62's fix already does for the mode.

## 9 November 2026 — a Normal default

- The house stays occupied (the owner's wife is at home while the owner is away), so it stays on Normal, and the planner showed "Waiting: a complete Normal day of household use" because the return from Away left no complete Normal day.
- **Decision (owner): "can we set a normal default".** With household learning on and no complete Normal day to copy, the Normal day is the seeded level (12.24 kWh a day, `demand_learning.SEED_KWH`), **flat across the day**, and every plan built on it says so in its assumptions ("Normal demand is the DEFAULT …") and in the forecast's reference day. The first complete Normal day replaces it at once. With the learning switch off, the old rule stands: no complete Normal day, no forecast.
- This relaxes the 25 October rule that missing hourly history stays unavailable. The default carries the weight of the owner's own 12-day reference (sd 2.66 kWh); it is not measured use, and the dashboard and plan assumptions say so.

## 10 November 2026 — three Modbus connections to the Solarbank: prove it, don't change it (GitHub issue 71)

- The controller's reader, the writer (`devices/modbus_write.GuardedModbusWriter`, its own connection) and the collector's reader are three separate Modbus clients to the same device; specification sections 2 and 10.5 want one connection and one lock per device, and nothing today serialises a write on one connection against a read on another.
- **Decision (owner): A, not B.** Prove it at commissioning rather than make the writer share the reader's connection and lock. The device has already been seen to serve several clients at once (GitHub issue 37); this records that tolerance as measured, not assumed, before relying on it with a write in flight.
- **Procedure** (no code, no new mandatory gate test — a one-off proof, recorded here once performed): with the controller stopped, run the collector at its normal poll rate alongside several of the controller's own commissioning writes (repeating `commission run setpoint-zero-write --confirm` a few seconds apart for a few minutes) and watch both for a refused, delayed or missing read or write. See OPERATIONS.md, "Connection sharing: prove it at commissioning".
- **Result:** not yet run. To be recorded here, dated, once performed at commissioning. If anything is lost, refused or delayed, option B (the writer joining the reader's connection and lock) is to be revisited before going further.
- **First measurement, 11 November 2026 (Pi, release 6225428, logged mode):** the Solarbank accepts **at most three Modbus TCP clients at once**. With the collector and the controller connected, a third read-only connection read the mode and a fourth failed. The T2 runner, holding its own reader and writer beside them, was a fourth connection and every write failed at once (twice); nothing reached the battery. A single write (`setpoint-zero-write`) as the third client passed. Anker publishes no limit (its Home Assistant page and integration, and the ioBroker and openHAB threads, say nothing on it). Consequence: live, the controller's reader and writer plus the collector are exactly three, so **nothing else may connect to the Solarbank while it runs** (no test tool, diagnostic read, other app or integration). The runner tests now hold one connection of their own at a time. Still to prove at commissioning: no write refused or delayed with all three connected under load.

## 10 November 2026 — a bump charge is billed at the peak rate (GitHub issues 72 and 34)

- **Owner, from the bill for Sunday 9 November:** a bump charge is billed at the peak rate.
- This confirms specification 6.2 as written ("Bump charges are billed at the day rate"; the cap counts smart charging). A bump charge is therefore not smart charging and does not use the six-hour allowance. Until now both the controller and the planner counted every half-hour the car charged in, bump charges included.
- **Now:** a half-hour counts toward the allowance only if the car charged in it inside the cheap window or inside a listed SMART slot. The planner tells the two apart from the slot lists the providers service archives; a half-hour with no list to judge it by still counts (the safe direction).
- Unchanged: the controller treats a moment as cheap only inside the cheap window or a listed SMART slot. Any other dispatch type (the 8 November feed showed a bump charge arriving as one) is dropped, so during a peak-rate bump charge the battery is held, not discharged into the car.

## 11 November 2026 — a smart charge outside the night window makes that half-hour off-peak for the whole home (GitHub issue 34)

- **Owner, from Octopus:** when Octopus schedules the car to charge outside 23:30 to 05:30, the whole home also gets the off-peak rate for that half-hour.
- This confirms, and does not change, the rule already recorded: `SOFTWARE_SPECIFICATION.md` (Octopus's rules supplied on 30 October, rate 3, the "bonus home rate") and GitHub issue 34 (rule 3, within the six-hour midday-to-midday allowance). A bump charge is still billed at the peak rate (10 November, above); the owner confirmed on 11 November that the car charge of Sunday 8 November, 18:02 to 23:30 (about 38 kWh, no smart slot listed), was a bump charge.
- **First real case, 11 November 12:30 to 13:00:** a SMART slot listed by Octopus. The Pi measured the car at about 7 kW, 3.5 kWh between 12:30 and 13:02; the battery kept discharging into the house (about 2.3 kW) under the Anker's own control. The Octopus app shows 15.17 kWh for the charge; the difference is not yet explained.
- **What the Pi did with it:** "household rate verified" is off in Settings, so `slot_intervals()` (`services.py`) passes no slots to the planner and every daytime slot is priced at the peak rate. The controller (logged mode, nothing sent) held the battery ("car charging in an expensive period") and did not charge it. To use the bonus home rate, the owner turns on "household rate verified" with the account evidence and an expiry; that is a setting, not a code change. **Decision (owner, 11 November):** turn it on now, so the next nights test GitHub issue 67 with real slots.
- Still open in issue 34: proving at commissioning, against the bill, that idle dispatched half-hours outside the window are or are not billed off-peak for the house.

## 11 November 2026 — the off-site backup stays on Google Drive

- **Decision (owner):** keep the off-site backup on Google Drive (`REMOTE=gdrive:solarbank-backups`, WP8); the switch to Dropbox, deferred until now, is dropped. The Drive backup has run and verified nightly since 10 November (about 95 MB an archive, three kept). A move later needs no code: `rclone` takes any remote named in `/etc/solarbank-cost-optimiser/offsite.conf`.

## 11 November 2026 — the commissioning gate is staged by write class (GitHub issue 76)

- **Found:** the gate required all nineteen commissioning tests before anything could be sent, but seven of them (T3a, T3b, T5, T6, T8, T12, the supervised run) can only be observed while the controller drives the battery. Commissioning could not be completed in any order. Meanwhile the Anker empties the battery into the car whenever it charges.
- **Decision (owner): stage the gate.** Mode writes (stage A, the car-charge block of GitHub issue 29: hold the battery at 0 W while the car charges) after the six runner tests, T2, T9 and T11. Set-points (stage B) after those plus T3a, T5 and T12, observed during stage A; charging supervised until T8 and T6 pass. All nineteen remain required for complete commissioning and unattended running (G3). D16 stands: nothing is waived, only ordered.

## 11 November 2026 — when the controller is frozen (GitHub issues 71 and 77)

- **Decision (owner, corrected the same evening):** the controller is frozen **once it has been tested, not before**. Until then the controller files (the 40 of `live_gate.guarded_files()`) may still change, and any passes recorded meanwhile are trial runs that a change resets. When the controller has passed its trial runs, its files are frozen and the formal commissioning run is recorded on that code.
- Planned before the freeze: option B of GitHub issue 71 (the writer shares the controller's reader connection, one lock per device, as specification sections 2 and 10.5 ask), so the Solarbank has two Modbus clients and one spare. T2 is tried first on the current code to prove the battery's sign.
- Test runners stay outside the controller files, beside the logged controller (T2 built; T5, T3a/T3b and T6 to build, issue 77); T12, T8 and the supervised run use the live controller in stages A and B.

## 11 November 2026 — the hand-back mode is self-consumption (mode 0), correcting D17

- **Decision (owner):** in operation the controller uses only **mode 3** (third-party control: charging, and holding at 0 W) and **mode 0** (self-consumption: the Anker supplies the house). When the controller lets go of the battery (release, exit, fault) it hands over to **mode 0**. On a fault the owner is emailed (the existing alerts: HANDS_OFF, a safety setting changed, a refused write, a stopped service, invalid readings) and resets the battery by hand in the Anker app. Anker's dynamic pricing (mode 7) is not used by the controller.
- **Correction:** D17 recorded the default as "Dynamic Tariff (6)", and the code's `MODE_DYNAMIC_TARIFF = 6` follows it; but the vendor's register map (`devices/anker_vendor.OPERATING_MODES`) names **6 smart mode** (and 7 dynamic pricing). **Smart mode fails on this installation:** set from the app or over Modbus on 11 November it raised the Anker's "smart meter missing, disconnected or not properly configured" error and fell back to self-consumption, very probably because the smart meter's one Modbus connection is held by the controller. Modes 0 and 3, written over Modbus the same evening, worked at once (the register followed within 3 s).
- **To change before the trial runs** (the controller files are not yet frozen): the default mode 6 to 0 (`action_machine`); the runners `default-mode` and `release` to expect 0; `read-registers` to check the specification's T1 bits (0 and 5) instead of exactly 47. The guarded writer already allows mode 0, and the start-up check already requires bits 0 and 5.

## 11 November 2026 — T11 by cross-check with the Shelly; T9 shown with the battery as the garage load

- **Decision (owner): the measurement chain is proven; T11 is done by cross-check instead of a clamp meter** (the owner has none). Specification section 13's T11 compares I_garage_now with a clamp meter at several loads, pass within 0.5 A. The same comparison was made with the Shelly Pro EM's own RMS current (an independent instrument on the Solarbank and car circuits), amps against amps, 11 November 21:44 to 21:50 (log: `t11-currents.csv` in the owner's analysis folder):

| Load | Anker meter, garage CT | Shelly (Solarbank + car channels) | Difference |
|---|---|---|---|
| Idle | 0.27–0.28 A | 0.21 A | 0.07 A |
| Battery supplying 2.45 kW | 10.41 A | 10.50 A | 0.09 A |
| Car 7 kW with the battery supplying | 20.14–20.35 A | car 30.9 A − battery 10.8 A ≈ 20.1 A | 0.1–0.2 A |
| Car 7 kW alone (battery held) | 31.28–31.65 A | 31.13–31.47 A + 0.18 A | 0.0–0.2 A |

  Within 0.2 A at every load against the 0.5 A criterion. The whole-house import also agreed with the Octopus utility smart meter: Anker main CT 7345–7371 W against 7397 W read by the owner (under 1 %). Voltage: 232 V idle, sagging to about 227 V (meter) and 223 V (Shelly) with the car at 31 A; amps worked out from power at a fixed 234.5 V under-read by about 0.7 A for that reason, so the comparison is made in measured amps. The gate entry `t11-rms-current` is signed by the owner with this note; nothing in the controller changes.
- **T9 (garage CT sign):** with branch protection and third-party PV monitoring off (owner, 11 November), the battery served as the known garage load: Fast Charging 19:12–19:24 gave meter garage power +2.11 kW (importing) against the Shelly's −2.04 kW into the battery; discharging gave the mirror image within 0.03–0.05 kW. Readings arrive over Modbus (the controller reads the meter every second). Signed by the owner after the deploy that follows (the controller change of 12 November restarts the record).
- Both are recorded by the owner (`commission pass`); `commissioning.json` is changed only by the owner (CLAUDE.md rule 1).

## 12 November 2026 — smart slots: no allowance counting; a cheap slot charges the battery (GitHub issues 34 and 67)

- **Decision (owner): no allowance counting.** A moment is a cheap smart slot when the car is measured charging and the time is inside a SMART slot on Octopus's current list, nothing else. Octopus manages the 6-hour allowance and its list is the authority. This replaces the 6-hour midday-to-midday tracker (issue 34's "6-hour allowance tracker", the controller's `SmartSlot` cap and the planner's `apply_slot_rules` cap) and supersedes the counting parts of the 10 November entry on bump charges (a bump charge is outside any listed SMART slot, so it is not cheap without counting anything).
- **Why (found 12 November):** the night of 11 to 12 November the car charged in listed slots for about five hours, yet the controller's count reached 13 half-hours by 06:00 and called the 06:00 to 06:30 slot "expensive". The car signal's 2-minute switch-off delay (D7) spilled each stop into the next half-hour (00:30, 03:00, 05:30 were counted with the car essentially off), and the 12:30 slot of 11 November was missing (the release then running did not save the count).
- **Decision (owner): a cheap slot charges the battery.** In a cheap period (the night window or a cheap smart slot) with the battery below its target, the controller charges; it holds only when the battery has reached the target or the car is charging outside any cheap period. To check: the planner names a charge target for every listed slot, so a daytime slot charges and does not only hold.

## 12 November 2026 — T10, T3b and the SSD deferred to the summer; unsupervised overnight charging after T12, T3a, T8, T6 and the supervised run

- **Decision (owner):** to charge overnight unsupervised this winter, the controller needs T12, T3a, T8, T6 and the supervised run, on top of stage A (commissioned 12 November: the six runner tests, T2 20 of 20, T9, T11, T5, the three devices confirmed). **Deferred to the summer:** T10 (smart-slot behaviours, including the clock change of 28 March 2027), T3b (solar with a full battery while the car charges: rare in winter) and the SSD of gate G3. They stay in `live_gate.REQUIRED_TESTS` (the "complete" list); the gate's write classes do not depend on them, so nothing in the code changes. This narrows G3 for the winter; D16 stands in that nothing is waived, only scheduled.
- **Found the same day:** in stage A (mode writes only) the controller cannot send a charging rate, so in the cheap window it would hold the battery in mode 3 at 0 W instead of charging, and it overrides the Anker app's own mode. Stage A alone is therefore a daytime step; the overnight charge needs stage B (set-points).

## 12 November 2026 — every listed smart slot is cheap, whether or not the car charges (to be checked against the bill)

- **Decision (owner):** a SMART slot on Octopus's current list is a cheap period for the battery whether or not the car is drawing (for example when the car is full): the battery charges in it like the night window. This sets aside specification v1.3 section 6.2 condition 2 (the car measured charging) and the "plugged in but not charging does not count" rule recorded in GitHub issue 34, **provisionally**.
- **Why (owner):** Octopus sees the charger, not the car's state of charge, and schedules about 6 hours of slots every day; its post "Intelligent Octopus Go: smarter charging for a greener grid" (7 May 2026) says that when it schedules the car to charge outside 23:30 to 05:30 the home also gets the off-peak rate for that half-hour. The same post also says "you only pay when the car starts actively charging" and that only actual smart-charging time counts toward the 6 hours; it does not cover a car that is plugged in but full. The bill decides.
- **To check:** the owner compares a night's Octopus half-hourly bill for a listed slot in which the car did not charge (the first was 12 November 05:30 to 06:00) with the import price. If such half-hours are billed at the peak rate, this decision is reversed (the car-charging condition returns) before the next billing period; the cost of finding out is at most about 2.5 kW for each idle listed half-hour at the difference between the two rates (about 35p each).
- Unchanged: SMART dispatches only (never BOOST or other types), the list's freshness rules, the hold to the end of a half-hour under way, and the car rule outside any cheap period (the battery is held, never discharged into the car).

## 12 November 2026 — commissioning is a simple checklist kept by the owner, not enforced in code

- **Decision (owner):** the software no longer requires a commissioning record for this exact code before it sends. The gate is the control-mode flag (`live`) and the write-switch (`mode`, `setpoint`), both root-owned files the owner sets. The tests themselves are unchanged and still done: their checklist and state are in `COMMISSIONING_PLAN.md`, and `commission pass` / `commission run` keep writing them, with date and note, into `commissioning.json` as the owner's notebook. A code change no longer wipes it.
- **Why (owner):** a change that does not alter how the controller writes (such as the slot rule above) should not mean repeating every test; keep it simple.
- **Consequence, accepted:** RC4's "a change to any controller file closes the gate until the tests are repeated" and issue 76's per-stage software check are withdrawn. With the switches set, a newly deployed release writes as soon as it starts; the owner is present for every deploy (CLAUDE.md rule 4) and decides after each change which tests, if any, to repeat. The specification's tests (section 13) and the steps G1 to G3 stand as the checklist.

## 15 November 2026 — a listed smart slot is cheap only while the car charges in it (the 12 November decision is reversed)

- **Decision (owner):** the half-hourly bills of 13 and 14 November show that a listed SMART slot is billed at the standard rate when the car does not draw in it. The condition of specification v1.3 section 6.2 (the car measured charging) and the rule recorded in GitHub issue 34 return. The 12 November decision above set them aside provisionally and said it would be reversed if such half-hours were billed at the peak rate.
- **Evidence:** on 14 November the car was full and every half-hour in which the controller charged the battery in a listed slot was billed at the standard rate: 06:30 to 07:00 (£0.47), 07:30 to 08:00 (£0.52) and 23:00 to 23:30 (£0.45), as was 13 November 15:30 to 16:00 (no car). Together with the 05:30 to 06:00 half-hour, in which the battery was held at zero while the house ran on the grid, about 60p to £1 a day of avoidable cost on 14 November (an estimate from the bar heights, not an exact figure). Octopus's charger metering and the controller's measurement of the car are the same evidence: no power in the slot, no cheap rate.
- **What changed:** `signals.SmartSlot` starts only while the car is measured charging and ends when it stops, or at the end of the half-hour (as before 12 November). The planner's `apply_slot_rules` prices the half-hour in progress as cheap only if the car is charging now or has charged in it (a slot just begun has five minutes of grace); a FUTURE listed slot is still assumed to be used, because the plan needs its window and target for the controller to charge in when the car does draw. No allowance is counted (issue 79 stands).
- **Not settled:** why the 07:30 slot on 13 November was billed at the standard rate although the car did charge in it (the 6-hour allowance is the guess); the possible rule that the whole half-hour is billed cheap once the car has drawn in it, so the battery could carry on after the car stops; and whether the planner sets too low an overnight target by counting on slot energy that does not arrive (watch for a night target below about 95% on a day with listed slots). GitHub issue 87 (charge in every slot at full power) rested on the 12 November rule and needs re-scoping.

## 15 November 2026 — scope: economic return, not safety (owner statement)

- **Statement (owner):** nothing in this software is safety critical. The battery is hardwired to the smart meter over RS-485, and its firmware handles the metering, safe operation and the tariff, which is held in firmware and runs locally without internet. The garage supply is rated 50 A on 10 mm three-core cable, so the 45 A garage limit only prevents inconvenient breaker trips. Whatever the software does, the system continues to operate safely. The software's aim is to maximise the efficient use of the battery and the economic return.
- **Consequence:** a failure of the Pi or the software costs money or causes a nuisance trip; it is never a hazard. No safety-style guard is needed, including a mode-independent garage guard. The garage limit stays where it is (mode 3 charging) unless trips are seen. Reliability work (heartbeat, backups, alerts) is justified by the money saved, and proposals should be sized to that.
- **Effect on plans:** the "configurable idle mode (0 or 7)" and the "mode-independent garage guard" are no longer assumed. Mode 7 as a fall-back is judged on cost alone and only by the dynamic-mode test results. Simple options come first, and the owner decides whether anything more is worth building.

## 18 November 2026 — evidence: a half-hour in which the car drew for 20 minutes was billed cheap throughout

- **Evidence (owner, Octopus app, Monday 16 November):** a SMART dispatch ran 06:30 to 06:50 and the car drew 2.1 kWh (charger report 2.1 kWh, £0.15; the Pi measured 2.1 kWh). The 06:30 to 07:00 half-hour showed 2.27 kWh in total: EV low rate £0.14, home low rate £0.01, **nothing at the standard rate**. At the confirmed 6.60p/kWh, 2.27 kWh is 15.0p, which matches. So the house's 0.17 kWh, including the ten minutes after the dispatch ended, was billed cheap as well as the car's energy.
- **Reading:** Octopus appears to bill a half-hour at the cheap rate when any part of it falls inside a dispatch in which the car drew. This supports the "whole half-hour once the car has drawn" reading left open on 15 November. It is **one sample**, and the battery was already full, so it says nothing about the battery charging after the car stopped.
- **Still unexplained:** 13 November 07:30, where the car charged but the half-hour was billed at the standard rate.
- **Decision (Claude's recommendation, not yet the owner's):** change nothing in the controller. The most the whole-half-hour reading could add is a few minutes of charging at the end of a slot (about 0.4 kWh, about 11p) each time the battery is not full and the car stops mid-half-hour. Collect more half-hours (especially one in which the battery was not full and the car stopped early) before deciding.

## 18 November 2026 — a smart slot runs to the end of its half-hour once the car has drawn in it

- **Decision (owner):** once the car has been measured charging inside a listed SMART dispatch, the battery treats the rest of that half-hour as cheap, even if the car stops early. This replaces the 15 November rule that ended the slot the moment the car stopped. The car must still be charging for a slot to **start**, and a new half-hour needs the car charging again.
- **Why:** the 16 November bill (entry above) charged the whole 06:30 half-hour at the cheap rate although the car drew for only 20 minutes. On 17 November the car stopped at 07:42 and the battery stopped with it, leaving about 18 minutes (about 0.75 kWh at 2.5 kW, roughly 20p) unused.
- **If a half-hour turns out to be billed at the standard rate** (as 13 November 07:30 was): the battery stores at most about 0.75 kWh of 35p power that it would otherwise have bought later at 35p. The loss is the round trip, about 3p. So the change is worth making before the evidence is complete. The 17 November 07:30 bill is the next check.
- **Where:** `signals.SmartSlot` (a controller file). The planner's `apply_slot_rules` already priced the half-hour in progress as cheap once the car had charged in it, so the planner and the controller now agree.
- **Side effect, accepted:** with a full battery the controller now holds at 0 W (mode 3) to the end of the half-hour instead of handing back when the car stops, so the house runs on cheap grid power for those minutes instead of on the battery.

## 19 November 2026 — a learned solar correction, compared but not used (GitHub issue 94)

- **Decision (owner):** the solar forecast should learn from what the panels actually produce, using known libraries rather than custom code. Built now as **step 2** of issue 94: the corrected forecast is made every evening beside the current one and scored against the measured solar, but the planner does not use it. Using it (step 3) is a later decision, made on the History page's figures.
- **Method:** EMHASS's adjusted-PV-forecast method (scikit-learn Lasso, time-ordered cross-validation, forecast plus hour of day plus sun elevation from pvlib), run in the solver environment. EMHASS's own function was not called because the pinned 0.18.4 could not be checked against the 0.18.7 code that was read; scikit-learn and pvlib are the libraries EMHASS itself uses for it and are pinned in `solver-requirements.lock`.
- **Specification point kept:** hours with the battery full are excluded from the training (curtailed solar must not train the forecast downward).

## 20 November 2026 — evidence: the whole half-hour is billed cheap once the car has drawn in it, and the 13 November 07:30 case is cleared

- **Evidence (owner, Octopus app, read on 20 November).** All four half-hours below had the standard-rate line empty. The cheap rate is the confirmed 6.60p/kWh.

  | Half-hour | What the Pi did | Octopus app |
  |---|---|---|
  | Mon 16 Nov 06:30 | car 20 minutes (dispatch 06:30 to 06:50) | EV low £0.14, home low £0.01 (already recorded on 18 November) |
  | Tue 17 Nov 07:30 | car and battery 07:30 to 07:42 | EV low £0.07, home low £0.05, total £0.13 with the standing charge |
  | Fri 13 Nov 07:30 | car charged | EV low £0.22, home low £0.13, total £0.37 with the standing charge |
  | Thu 19 Nov 13:30 | car 13:30 to 13:34 (0.23 kWh); **battery held charging to 14:00:00** (the 18 November change) | EV low 0.23 kWh, home low 2.48 kWh, nothing at the standard rate; £0.02 + £0.16 = £0.18 for 2.71 kWh, which is 6.6p/kWh |

- **Settled:** a half-hour that the car draws in is billed cheap **throughout**. On 19 November the battery charged for the 26 minutes after the car stopped and that was billed cheap too, so the 18 November change (a slot runs to the end of its half-hour) pays as intended, outside the normal window. The 14:00 half-hour, after the hand-back, was ordinary standard-rate usage.
- **13 November 07:30 is cleared.** The 15 November entry and the 18 November entry record that half-hour as billed at the standard rate although the car charged (and guess the 6-hour allowance). The Octopus app now shows it cheap. What is not known is why the two disagree: the same chart has a standard-rate bar (about £0.25) one half-hour earlier, at 06:30, which may have been misread as 07:30, or Octopus may have re-rated the slot since. Either way no allowance rule is needed to explain it, and the cases billed at the standard rate (14 November 06:30, 07:30 and 23:00; 13 November 15:30) are all half-hours in which the car did not draw.
- **Still open:** whether the planner sets too low an overnight target by counting on slot energy that does not arrive (watch for a target below about 95% on a day with listed slots). GitHub issue 87 still needs re-scoping.
- **Not evidence of a fault:** the large standard-rate bars on 17 November about 06:30 and 18 November 10:00 are the electric shower, and those on 16 November from about 14:30 to 21:00 are the electric heating (owner). The battery cannot carry loads that size, so the grid covers the rest.

