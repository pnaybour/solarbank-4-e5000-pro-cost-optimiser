# Solarbank controller development plan

**Version:** 2.0 (rewritten around the clarified objective; version 1.0 is kept in `reports/DEVELOPMENT_PLAN-v1.0-2026-10-24.md`)  
**Date:** 28 October 2026 (updated 2 November 2026: revised approach, see below)  
**Requirements:** SOFTWARE_SPECIFICATION.md version 1.8  
**Status:** observation, forecasting, shadow planning and a shadow controller are running on the Pi; the nightly charge level is advised but not yet applied; no equipment write exists

Build a small Raspberry Pi application in stages. The first useful delivery is a working website with reliable measurements and history. Add forecasts and charging advice next, prove the control logic without sending commands, and then commission the supported battery controls. Automatic operation follows successful equipment and recovery tests.

**Revised approach (2 November 2026).** After the first shadow review, the controller's decision logic is being rebuilt as explicit state machines using the `transitions` library, following the decision table of the owner's functional specification v1.3, and high-rate telemetry is moving to a fixed-size data logger built on Whisper. The ordered work is in [POST_SHADOW_PLAN.md](POST_SHADOW_PLAN.md) (items B7 and B8 below point to it); this plan keeps the stages and pass points. Where the two differ for the controller or for storage, POST_SHADOW_PLAN.md and REQUIREMENTS_AMENDMENTS.md (1 and 2 November 2026) apply.

The application will use FastAPI for the website, SQLite for persistent records, Whisper fixed-size files for high-rate telemetry, a separate Python controller process and a separate planning worker. It will support Away from home, Normal consumption and High demand; optional Octopus tariff and smart-slot access with default operation; Shelly car-charging history excluding readings below 10 W; and the 45 A garage-feed limit.

The companion **CODE_REUSE.md** contains actual excerpts from the energy monitor, their dependencies and the changes needed before reuse. Portable excerpt files and source hashes are under **development/**. They are reference material, not an installed controller.

## 0. The objective, in one paragraph, and how to stay on course

**This application decides, each evening, how full to charge the battery from cheap electricity, so that tomorrow's demand is met by the most economic mix of solar, cheap
night power and as little peak-rate import as possible.** The Anker firmware keeps matching house, grid and solar moment to moment (including discharging to the house and
holding idle once full inside the cheap window). The controller we build applies the chosen charge level and handles three exceptions: the car (inhibit discharge while it
charges), the garage supply (50 A supply, 45 A operating limit) and the minimum SOC. Peak import costs far more than surplus cheap energy, so uncertainty pushes the target
**up**, and the planner remains an expected-cost optimiser, not a "demand minus solar" rule. Success is weekly pounds saved against a home with neither solar nor battery, with
an optional running total counted from go-live. (SOFTWARE_SPECIFICATION.md section 1.1.)

**Rules so we do not get lost**
1. Every piece of work below has an ID (`A1`, `B3`, `C2`, ...), a deliverable, a pass point and a status. Work that has no ID is not started.
2. Stage A (the nightly decision) comes first. Stages B to E exist to protect, apply or prove it. If a task cannot be tied to the objective above, ask before doing it.
3. Nothing sends a command to the battery or charger until Stage C has passed with the owner present. Until then everything runs in shadow and says so on the dashboard.
4. The Anker app/firmware is the reference for normal behaviour. The controller is validated by comparing what it would do with what the Anker actually does (B3).
5. A status is `Done` only with evidence recorded in a report; otherwise it is `Built`, `Running in shadow`, `Open` or `Blocked on <owner/hardware>`.

## 0.1 Where we are (28 October 2026)

| Phase | Status | Evidence / what is missing |
| --- | --- | --- |
| 0 Capabilities | Partly | Devices read read-only; vendor control points documented from the Anker integration and Anker's Home Assistant guide, **none verified on the battery** (section 0.3) |
| 1 Foundation | Done locally | `reports/phase-1-foundation.md` |
| 2 Measurements and car history | Done except garage current | Direct Solarbank and Shelly readers passed a 24-hour comparison with the original monitor (signed off 28 Oct, `REALTIME_ACQUISITION_PLAN.md` 6.11). The meter is still read through the original monitor, so **garage current is not read** (until the 28 Oct cutover: now read directly via the controller and shown on the dashboard) |
| 3 Forecasts, settings, tariffs, website | Built; live account tests pending | Away/Normal/High buttons, fallback tariff, Octopus adapters (not tested on the live account; no EV yet) |
| 4 Planner in shadow | **Running; first checked plan 28 Oct** | Tonight's advice: 100 % over 23:30 to 05:30, cost equal to a full night (right for October). Limits: one target for the whole horizon (A3), minimum SOC still 5 % (A4) |
| 4b Controller in shadow | Running since 27 Oct; review fixes in progress | Decides every second, prepares commands, sends none. Six tests await rewriting for the new rule that the Anker owns normal discharge. 2 Nov: being rebuilt as `transitions` state machines (B7) |
| 5 Commissioning | **Not started** | No writer exists, by design |
| 6 Deployment and recovery | Partly | One-command deploy with test gates; open: post-deploy readiness check, rollback drill, reboot test |
| 7 Automatic operation | Not started | |

## 0.2 Work packages from here

### Stage A: make the nightly decision right (shadow advice; the primary deliverable)
| ID | Work | Deliverable and pass point | Status |
| --- | --- | --- | --- |
| A1 | Usable Normal reference day despite restart gaps | Yesterday accepted with up to 10 filled minutes, shown on the forecast | **Done** (0.5.10) |
| A2 | Planner publishes a checked nightly target | A plan with target, window, expected cost and full-night cost on the dashboard | **Done** 28 Oct |
| A3 | One target per cheap opportunity | Planner chooses separate targets for successive nights/slots; test on the case where one constant target costs twice as much; label the search honestly until then | **Built** (0.5.21, 29 Oct): one level per cheap window, coordinate search from the best single level, checked independently; shown as "then X% (Fri)" |
| A4 | Minimum SOC as a setting, **default 10 %, changeable on the settings page** | One value (stored with a revision) read by the planner, written into the plan file for the controller, and shown on the dashboard; bounds-checked; tests at the boundary | **Done** (0.5.11, 28 Oct) |
| A5 | Measure lost solar | Daily record: when the battery was full in daylight, how many minutes show solar held back to the house load, and (when the evening-before forecast was saved) an upper-bound candidate for solar not captured and what a lower overnight charge might have saved; always labelled a candidate (specification 6.2) | **Built** (0.5.13); first forecasts are saved from the evening of 28 Oct, so the potential figure starts the day after |
| A6 | Forecast accuracy review | Each morning: yesterday's demand and solar forecast against measured; weekly persistence/regression comparison later (specification 6.3) | **Built** (0.5.21): each finished day's saved evening-before solar and demand forecasts against measured, on the history page and dashboard; the weekly model comparison stays in E3 |
| A7 | Savings tracker | Weekly pounds saved against no solar and no battery, optional cumulative total, labelled estimated; counting starts at go-live, shadow shows "would have" figures separately | **Built** (0.5.13): `solarbank savings-start YYYY-MM-DD` sets the go-live day; the dashboard and history page show it |
| A8 | Dashboard leads with the decision | Headline: "Charge to X %", the cheap window, expected cost against a full charge (with the saving), the inputs behind it, and what the Anker is doing now. Controller internals sit below | **Done** (0.5.12, 28 Oct) |
| A10 | Retention: long-term half-hour record, purge detail after 7 days, and stop the archive growing for nothing | Half-hour record aligned to Octopus periods with CSV; daily purge of detail; forecast and plan archived only on material change; measured write rate before and after | **Done** (0.5.14 and 0.5.15, 28 Oct): optimiser disk writes fell from 36.6 to 11.3 KB/s (table in OPERATIONS.md) |
| A9 | Pause, timed grid-target override, return to automatic, visit/return dates in the browser | Owner can do each without editing code (specification section 14) | **Built** (0.5.21): Pause, timed Override of the charge level, Return to automatic, and visit/return dates on the dashboard |
| A11 | BMS recalibration full charge | The battery must reach 100 % at least once every 7 days (owner-adjustable), preferably from cheap electricity in an off-peak window, because the Anker's own SOC Calibration (which does this) is to be turned off. The planner forces a 100 % target in the next cheap window once the last full charge is about 6 days old (solar filling it to 100 % counts, so summer needs nothing); a dashboard line shows days since the last full charge; the plan says why when it overrides the cost-optimal level | **Built** (0.5.28, 29 Oct). Needed before SOC Calibration is turned off in the app, so before C3. Shows on the dashboard and Settings; the interval (3 to 14 days, default 7) is adjustable |

### Stage B: protect and validate (controller in shadow)
| ID | Work | Deliverable and pass point | Status |
| --- | --- | --- | --- |
| B1 | Controller built and deployed in shadow | Decisions, prepared commands, command log, dashboard panel, watchdog | **Running** |
| B2 | Finish the second and third review fixes (most are in 0.5.11; the rest of the list below is still open) | Verified-control recovery, explicit durable hand-over, pricing precedence, connection test checks a dispatch, spring clock fallback, one shared car-charging delay, outcome evidence on disk, post-deploy readiness check. Rewrite the six tests that still assume the old discharge rule; deploy as 0.5.11 | In progress |
| B3 | Validate against the Anker's own behaviour | Each minute record what the Anker actually did (charging from grid, discharging, idle) beside what the controller would have asked; weekly agreement report listing every difference. Known baseline from 28 Oct: grid charge about 2.5 kW from around midnight, 100 % by 02:30, idle until 05:30, then discharging to the house | **Built** (0.5.22): `validation.py` scores each finished day from the controller's change log and the one-minute readings; history page, `/api/validation`, `/validation.csv`, `solarbank validation-report`. Evidence accrues over the shadow weeks |
| B4 | Cutover from the original monitor (step 4a.6) | The controller owns the meter (one Modbus client only) and shares its readings with the collector through a RAM file; garage current decoded (register 10666, secondary CT, divide by 100, magnitude) and shown; collector runs `--source direct`; comparison service and monitor stopped and disabled so a reboot cannot start the monitor and take the meter; one-minute way back kept; 24 h independence check | **Cut over 28 Oct 2026 at 09:31 UTC** (0.5.11): monitor and comparison service stopped and disabled, everything read directly, lamp safe, garage current 1.36 A shown. **24-hour independence check running**; way back: `scripts/rollback-cutover.sh 0.5.10-20261028T080617Z` |
| B5 | Car-charging inhibition proven | Simulated now; real demonstration with the car at supervised commissioning (gate G1) | Open (a car now exists, 2 Nov) |
| B6 | Garage monitoring | After B4: current shown and logged; 45 A limiter decisions validated in shadow | Open (after B4) |
| B7 | Controller as explicit state machines (`transitions`), following the decision table of the owner specification v1.3 | The action, mode-supervision, signal (EV, below-target, smart slot), command-lifecycle, garage-regulation, system-mode and owner-automation machines, with Hypothesis stateful tests against simulated devices and clock; the battery never discharges in a cheap period; the regressions for issues #17, #18 and #2 to #5 fixed through them | Open. Ordered in POST_SHADOW_PLAN.md (WP9 to WP14, WP24; the lifecycle contract LC for shutdown, hand-back and the HANDS_OFF latch is proposed, decisions D17 and D18); issues #26 and #29 |
| B8 | Fixed-size Whisper telemetry logger and record integrity | One Whisper file per metric (1 s for 2 days, 1 min for 90 days, 30 min for 10 years), single writer (the collector), written beside SQLite for two weeks, then the readers switched and the SQLite minute and coverage tables retired; daily records recalculated when late data arrives; backups and the storage guard updated | Open. POST_SHADOW_PLAN.md (WP15, WP23a to WP23c, WP8, WP19; data contract TC and rollback contract RC1); issues #12, #16, #21, #24, #25 |

### Stage C: choose and prove the way the charge level is applied (Phase 5)
| ID | Work | Deliverable and pass point | Status |
| --- | --- | --- | --- |
| C1 | Read-only look at the device settings | Read, never write: operating mode (10064), capability masks (32774/32775), charging limit and reserve registers (60000 to 60003). Record the values while the owner uses each app mode (Time of Use, Dynamic Tariff, Smart) on successive days, to see whether the app's maximum charge in Dynamic Tariff and Smart mode appears in any register | **Done 30 Oct** (evidence: `device-settings.jsonl`, 29 to 30 Oct). Only register 10064 changes when the app mode changes; the owner's usual dynamic mode reads as 6 (`smart_mode`) and Time of Use as 1; the limit registers stay at charging 100 % / discharge 10 % / reserve 10 % / backup off in both; the app's schedules and per-mode maximum SOC are not visible over Modbus. The collector keeps logging changes. Third-party control (value 3) is offered by the device and is the subject of C2/C3 |
| C2 | Decide the mechanism (decision gate with the owner) | One of: advice only (owner sets the level in the app); third-party control with a specified charging power and our own stop at the target; or the charging-limit register where its 80 to 100 % range is enough. Written down with the reasons | Open. Background and the vendor's exact control behaviour: ANKER_INTEGRATION_NOTES.md (adapted helpers in `devices/anker_vendor.py`, including the documented charge and restore sequences as data) |
| C2b | Extend the controller for live use with the behaviours of the owner's control specification (reports/control-spec-review-2026-10-30.md section 9) | A state combiner (CHARGE, ZERO_OUTPUT, DISCHARGE from the two abstract channels; since 2 Nov this is the `transitions` action machine of B7); a `ModbusTransport` for the live mode built on `devices/anker_vendor.py` (write sequencing, verification from the Shelly AC channel, 60 s mode check, start-up reconciliation, hand-back to mode 0), tested only against the fake device; 3 % restart hysteresis; the after-target behaviour once the owner has chosen between the readings in section 10 of the review | Open (shadow-safe; nothing connects to the real device). Design inputs from the vendor integration's own fixes: POST_SHADOW_PLAN.md section 12 (H2 to H7) |
| C3 | Controlled trial on one night, owner present | Apply the chosen level once; read back; observe the battery; stop commands and see what the battery does; restore the Anker mode. Record sign, units, limits, response time, behaviour when commands stop | Open. Owner first confirms in the Anker app that Backup Reserve, SOC Calibration, Fast Charging and Storm Guard are all off (they override third-party commands). The set-point (10071) cannot be read back, so verify through the mode register and measured battery power |
| C4 | Failure drills | Lost network, controller killed, Pi power cycle during a charge, app switch toggled; the battery ends in a known, acceptable state | Open. Must include the vendor-undocumented case: controller stopped or killed **mid-charge in third-party mode** (the vendor code never refreshes or expires a set-point), and the write-back of the previous mode |
| C5 | Garage limiter test | Safe, controlled load steps; no deliberate overload | Open (after B4) |

### Stage D: go live
| ID | Work | Deliverable and pass point | Status |
| --- | --- | --- | --- |
| D1 | Flip the execution mode | One word in the mode file, only after C3 and C4 pass (gate G1, supervised commissioning) and the conditions of gate G2, the limited first live step, are met (POST_SHADOW_PLAN.md, RC2); the live transport is the one thing still to write and is written in C3 | Blocked on Stage C |
| D2 | First weeks live | Daily review; savings counted from this day; a way back to the Anker's own mode at any time. Running without the owner nearby is a further gate, G3 (unattended operation), which needs Stage E1 and E2 and Phase 6 | Blocked on D1 |

### Stage E: resilience and learning (can proceed alongside; E1 and E2 do not block supervised commissioning or the limited first live step, but they block unattended operation, gate G3; E3 to E5 block nothing)
E1 device discovery and Find devices (specification 11); E2 controlled reboot, rollback drill and power-loss test; E3 weekly forecast model comparison with promotion and rollback; E4 live Octopus account test and extra smart slots (the EV now exists; it must be linked to Intelligent Octopus Go); E5 the one-second loop's remaining items.

## 0.3 Facts established and facts still to establish

| Fact | Source | Status |
| --- | --- | --- |
| The Anker's charge limit applies to **grid charging only**; solar still fills to 100 % | Owner, 28 Oct | Established |
| Time of Use mode: maximum SOC minimum 80 % (battery life). Dynamic Tariff and Smart (AI) mode: any maximum charge | Owner, 28 Oct | Established |
| The Anker **app** (not the battery) accepts whole-hour slots only, so its own grid charge starts at 00:00, not at the 23:30 start of the cheap rate (and cannot end at 05:30). Seen 28 Oct: charging began at 00:00. The controller, once it applies the level itself, can use the full 23:30 to 05:30 window | Owner, 29 Oct; matches the B3 report (28 min at 23:32 to 00:00 with the Anker not charging) | Established |
| Today the battery is charged fully each night; some solar is lost even on sunny winter days | Owner, 28 Oct | Established |
| Anker overnight behaviour: grid charge about 2.5 kW from around midnight, 100 % by about 02:30, idle (house on grid) until 05:30, then discharges to the house | Recorded readings of 27/28 Oct | Established; the owner wants "idle when full inside the cheap window" kept |
| Vendor register 60000 "Charging Limit" 80 to 100 %, readable and writable; probably the Time of Use maximum SOC | Anker integration commit 21c9483 | Documented, unverified on the battery |
| Third-party control: enabled by a switch in the app; commands then take priority; "Specified Charging Power Adjustment" sets grid-to-battery watts; switching the app switch off returns to the app's mode and blocks Modbus | Anker Home Assistant guide, 28 Oct | Documented, unverified |
| Backup Reserve is not used; 10 % minimum SOC suggested | Owner, 28 Oct | Established / awaiting confirmation (A4) |
| Savings count from go-live; shadow running for several weeks accepted | Owner, 28 Oct | Established |
| 24-hour comparison with the original monitor passed | Report 6.11, owner sign-off 28 Oct | Established |
| Shelly `em1:0` (channel 0, `solarbank_ac` in the code) is the Solarbank AC circuit and `em1:1` (channel 1, `car`) the car charger. On the Solarbank channel positive is output to the house and negative is AC input into the Solarbank (charging, or its own standby draw: about 46 W seen at 02:06 on 2 Nov) | Owner 16 Oct; sign review 15 Oct (`history/source/idle-sign-review-15-oct.json`); owner 2 Nov; live shadow dashboard 2 Nov (car card about 3 W against 2.6 W in the Shelly app) | Established; formal commissioning of the channel map pending |
| Does the Dynamic Tariff / Smart maximum charge appear in any Modbus register? | C1 | **To establish** |
| Does a power set-point persist, time out or revert when commands stop? Sign and units? | C3 | **To establish** |
| Is the 2.5 kW limit a hard AC limit when solar is also charging? | C3 | **To establish** |
| Garage current register meaning on the commissioned phase | B4 / C5 | **To establish** (read once as evidence on 26 Oct) |

## 0.4 Owner decisions

| Decision | Status |
| --- | --- |
| Primary objective is the nightly charge level; Anker keeps moment-to-moment control | Made 28 Oct |
| Planner stays the expected-cost optimiser, not a simple rule | Made 28 Oct |
| Shadow running for several weeks before any live step | Made 28 Oct |
| Savings baseline is no solar and no battery; start at go-live | Made 28 Oct |
| Gap tolerance for the Normal day (10 minutes, no gap over 5) | Made 28 Oct |
| Minimum SOC: **10 %, adjustable in the web interface** (bounds to be set, default 10 %) | Made 28 Oct (A4); **superseded for the controller on 2 Nov: the firmware owns the floor and the controller reads it (v1.3, D13)** |
| Retire the original monitor from the control path **as soon as the direct readings cover everything it supplies** | Made 28 Oct; **done 28 Oct 09:31 UTC** (B4) |
| Keep the old monitor running afterwards for its savings history and forecast comparisons, **without reading the smart meter**, with no dependency or interference either way | Made 28 Oct; our side done (solver moved, deploy no longer needs it, kernel-level block installed); meter removed from its code and deployed; **re-enabled 28 Oct 10:04 UTC** and verified (one connection, to the Solarbank; nothing but the controller touches the meter); the kernel-level block on the meter's address stays until it has run clean, then is removed |
| After a controller restart, discharge is inhibited until the charger has been seen idle for **1 minute** (shared with the car card and history as the provisional release delay, to be confirmed on a real car charge) | Made 28 Oct; **superseded on 2 Nov by the v1.3 car signal: released after 120 s below 300 W (D7)** |
| In the Anker app (29 Oct): Backup Reserve off, Fast Charging off, Storm Guard off; **SOC Calibration is on** and can be turned off, but the BMS needs a full charge at least weekly, so this application must replicate it before it is turned off | Made 29 Oct |
| **The controller does all the grid charging; the Anker discharges to the house all day with no scheduled grid charging** (so, in the app, no charge schedule in the mode the Anker is left in) | Made 29 Oct |
| Anker app switched to **Time of Use** on the evening of 29 Oct for a night's charge to 100 % (the C1/B3 comparison night) | Done 29 Oct |
| After each charge the Anker is handed back to **mode 0 (self-consumption)**; this application does the tariff-aware charging (the Anker's dynamic mode is not used while it runs) | Made 30 Oct (subject to test T5: time for the mode change to take effect); **2 Nov: when the controller stops or faults, the default mode is Dynamic Tariff, not mode 0 (D17)** |
| Smart Meter branch protection and third-party PV monitoring are off in the app | Confirmed 30 Oct |
| No EV yet; EV rules, extra slots and the car tests wait for one | Confirmed 30 Oct; **superseded on 2 Nov: a car now exists** |
| If the controller or Pi fails mid-charge: **prove the recovery in trials first**, no power backup for the Pi now | Made 30 Oct (C4) |
| Extend the existing controller logic with the behaviours of the owner's control specification (SOLARBANK_CONTROL_SPEC v1.3), do not replace it | Made 30 Oct |
| Minimum battery level limited to **5 to 20 %** (the device's own limit register range) | Made 30 Oct; done 0.5.30; **superseded for the controller on 2 Nov (D13)** |
| Octopus rules for the car allowance and bonus home rate (midday to midday, 6 hours, in-window time counts) recorded in the specification section 7.2 | 30 Oct |
| **Charging sequence in each window (owner, 30 Oct):** charge to the level the planner chose (at full power), then IDLE (mode 3 at 0 W) until the cheap window ends, then hand back to **self-consumption (mode 0)** until the next window; car charging events are not part of this sequence (no EV yet). Code for the idle after the target: written and tested, not yet deployed | Made 30 Oct |
| **Do NOT spread the charge over the window** (e.g. 1 kW for six hours): the battery and inverter use less energy idling than charging at low power for hours, so charging at full power and then idling is the more efficient way | Made 30 Oct (spreading was built and removed the same day) |
| **Missing data is missing data**: a missing, stale or invalid car-charger reading never means "car charging". Gaps up to 10 s change nothing; a longer gap inhibits discharge only if the car was known to be charging (up to 10 minutes) or during the restart hold | Made 30 Oct; done 0.5.31 |
| Which mechanism applies the charge level | Pending, decided at C2. Leaning (29 Oct): third-party control (mode 3, negative set-point) for the charge only, then write the previous Anker mode back |
| State machines and database and record integrity come before other work | Made 1 Nov |
| In a cheap period (off-peak window or active smart slot) the battery never discharges: it charges, or is held at 0 W in mode 3, with or without the car. At peak, while the car charges, it is held at 0 W. Off-peak the car and the battery may both charge | Made 1 Nov |
| High-rate telemetry goes to fixed-size Whisper files (issue #16) | Made 1 Nov |
| Revised approach: the controller's state logic is built with the `transitions` library (an approved exception to "standard library only"), the state table is the decision table of the owner's functional specification v1.3, and telemetry uses a Whisper fixed-size data logger; documentation to be kept consistent with it | Made 2 Nov |
| **A car now exists (owner, 2 Nov).** Supersedes "No EV yet" (30 Oct): EV rules, extra smart slots and the car tests are no longer deferred. Its link to Intelligent Octopus Go is to be confirmed | Made 2 Nov |
| **No hand-back to the Anker app; the default mode is Dynamic Tariff (owner, 2 Nov).** The app's own settings are changed by hand. The controller releases by writing the default mode (value 6 on this device) instead of mode 0 when it stops or faults, and invalid data resets to it. The two sub-questions were answered on 3 November: the evening hand-over stays mode 0, and a release in a cheap period or during a car charge is accepted (POST_SHADOW_PLAN.md D17) | Made 2 and 3 Nov |
| **The owner's answers to the plan's decisions (2 Nov):** car thresholds, minimum level and garage regulation as in owner specification v1.3 (D7, D13, D14); charger gaps as the 30 Oct rule (D15); the HANDS_OFF latch (D18); the three go-live gates (D19); email alerts through Resend, to confirm (D1); `PLAN_HOLD_S` stays 1 hour (D4); library options deferred (D8). See POST_SHADOW_PLAN.md section 7 | Made 2 Nov |

## 0.5 The next three actions

**Superseded (2 November 2026):** the current ordered work is in [POST_SHADOW_PLAN.md](POST_SHADOW_PLAN.md) (Step 0, then WP1 to WP3 and the two foundations). The list below is the 28 October list, kept for history.

1. **B2:** finish the review fixes and the six tests, redeploy as 0.5.11 (no change in behaviour toward the equipment: still shadow).
2. **A8 and A5:** the dashboard leads with tonight's decision, and the daily lost-solar measurement starts, so the shadow weeks produce the evidence that justifies the whole project.
3. **C1 (read-only) and B3:** extend the Solarbank reader read-only to the mode and limit registers, and start recording the Anker's own behaviour beside the controller's.

## Delivery phases and pass points

| Phase | Delivery | Pass point |
| --- | --- | --- |
| 0 | Confirm equipment capabilities and measurement boundaries | Known devices, candidate controls and outstanding control limitations are recorded; discovery performs no writes. |
| 1 | Build the isolated project and test foundation | Recorded fixtures, configuration and database tests pass without equipment or provider access. |
| 2 | Run measurements and car-charging history on the Pi | Correct readings, honest missing-data status and persistent history; no equipment writes. |
| 3 | Add forecasts, household settings, Octopus access and the website | All settings and fallback paths work, including future visits, missing keys and changed slots. |
| 4 | Run the planner in shadow mode | Every published plan passes independent checks from actual SOC; no commands reach equipment. |
| 4b | Run the control loop in shadow mode | Simple standalone loop reading hardware directly; commands recorded, never sent; watchdog drill passed. |
| 5 | Commission battery control and operating limits | Each required command and protective action has a measured, repeatable end-to-end result. |
| 6 | Prove automatic deployment and recovery | Tested releases install automatically, preserve data/settings and recover or roll back correctly. |
| 7 | Introduce automatic operation and review | Mandatory controls, failure handling and operator demonstrations pass for the recorded commissioning conditions. |

Prepare deployment tooling in phases 1–2 and use it to install the read-only application. Phase 6 proves the complete update and recovery process before unattended automatic control. Forecasting, Octopus adapters and website work can proceed in parallel once the shared measurement/configuration interfaces exist.

Record each pass point in a short phase report containing the release/configuration versions, test inputs, expected and observed results, failures and remaining limitations. A running website, fresh measurements, a valid plan and commissioned equipment control are separate results. The checks in this plan are future acceptance work unless explicitly listed in the existing-code baseline below.

## Phase 0 Confirm the installation and available controls

Create a capability table for the actual Solarbank model and firmware. Cover grid-charge target, separate solar ceiling, AC charging reduction/stop, discharge inhibition, mode selection, readback and release/fallback behaviour. Identify the main CT, garage CT phase and dedicated Shelly charger channel. Review the licences and pin the device definitions/dependencies that will be used.

The existing Anker integration is deliberately patched read-only. Its presence does not prove that these control operations are available. Some official integration connection paths can select an operating mode; exclude those side effects from discovery and observation. Do not infer write registers from nearby read registers.

**Testing point:** identify devices and collect read-only evidence without changing settings. Record each proposed control as documented, awaiting controlled commissioning, or unavailable. Define the freshness and response measurements that commissioning must collect; do not invent a guaranteed hardware response time.

**Pass point:** the read-only architecture and measurement boundaries are verified, control gaps are explicit and the required commissioning tests are ready. Offline and observation work can continue while a control gap is investigated. Full automatic control cannot be declared complete while a mandatory target, charging-stop/limit, discharge-inhibit or fallback capability is unverified. If no EV is available, simulated charger tests remain software tests until a real charging demonstration is possible.

## Phase 1 Build the isolated foundation

Create the new application package, its own virtual environment/database and typed interfaces for observations, forecasts, tariff intervals, household-demand settings, plans and command results. Keep equipment writes disabled by default. Use simulated adapters and a clock that tests can control. Add migrations, a consistent database backup procedure, source/version fingerprints and structured events without secrets.

Import selected history read-only and verify the supplied historical manifest before using it. Preserve raw values, source units, UTC timestamps, retrieval status and genuinely available device timestamps. Convert once at adapter boundaries to kW, kWh, A, V, GBP/kWh and SOC fraction. Store London calendar dates only as derived reporting fields.

**Testing point:** run decoding, unit conversion, finite-number, schema migration, backup/restore, clock and coverage fixtures. Confirm that missing values remain missing, identical completed requests are not treated as fresh device updates, and imports leave the old monitor database unchanged. Use temporary databases and fake network/device transports in automated tests.

**Pass point:** a clean checkout can run the offline tests, create and migrate its own database, reproduce an imported observation and restore a backup. No test connects to equipment or a live account. Reuse R01, R04 and R07 for decoding, validation, integration and complete-day selection after the adaptations listed in the companion guide.

## Phase 2 Deliver read only measurements and car history

Implement one connection owner per device, verified discovery and shared snapshots. Resolve connection sharing or transfer of ownership with the existing monitor before adding a poller. FastAPI reads the controller snapshot and database; browser requests must not open their own device connections. Install the separate read-only services on a free Pi port and check the existing applications remain available.

Add actual garage-current acquisition. The present normal meter poller collects main/sub power; the diagnostic current-reading example is a starting point, not an already running current limiter. Verify the commissioned phase, current semantics and CT direction instead of deriving amps from watts divided by 230 V.

Implement the Shelly `em1:1` charger card and persistent sessions. Below 10 W is standby, exactly 10 W qualifies for charging after confirmation, and invalid/stale readings are Unknown. Keep the confirmed state during the configured release delay while excluding every sub-10 W period from charging energy and active time. Count full power at or above the cutoff, rather than subtracting 10 W.

**Testing point:** exercise channel selection, errors, threshold flicker, gaps, midnight/DST and restart. A synthetic 7 W standby period must add no car-charging kWh; a fully covered 2 kW charging period lasting 30 minutes must add 1 kWh. Raw standby consumption must remain in household/garage totals. Reconcile session, daily and CSV totals over the same covered intervals; never turn raw mixed standby/charging counter deltas into filtered charging kWh.

**Pass point:** live readings have verified identities/units/signs and visible coverage, charging history survives restart without duplicates, discovery causes no writes and a browser can distinguish standby from unavailable data. Reuse R02–R04 and R10. Device refresh and end-to-end latency measurements begin here.

## Phase 3 Add forecasts settings tariffs and the website

Implement the following features against the common interfaces:

- **Household demand:** Normal uses the latest complete eligible Normal day, with a maximum age. Away uses an explicit essential base-load estimate; High demand adds a visible extra-energy allowance. Exclude Away/High periods from Normal training. Support immediate selection, a future start and an optional end returning to Normal. Planned returns/visits must affect the preceding charging opportunity.
- **Solar:** retain the two panel planes and use archived weather forecasts with their actual acquisition times. Separate weather forecasts from retrospective provider data. Keep curtailment exclusions and provisional efficiencies visible; do not train available solar downward from a full battery without independent evidence of curtailment.
- **Octopus:** separate published tariff access from authenticated device/dispatch access. Implement the current `flexPlannedDispatches` interface and token handling. A valid empty list means no extra slots. Manual/bump charging or detected charger load alone does not establish cheap household electricity.
- **Fallback:** save user-confirmed day/cheap prices and the editable London night window independently of API results. Use valid API/cache coverage and fill unavailable intervals from defaults. A missing key or failed response must not prevent default operation. Do not invent extra slots, zero prices or indefinitely valid cache entries.
- **FastAPI:** provide the main status page, demand selector, pause/override controls, tariff/default configuration, car history/CSV and a concise diagnostics view. Validate authenticated state-changing requests, reject invalid CSRF/origin requests and keep credentials out of browser responses and logs.

**Testing point:** use saved provider fixtures for normal, partial, invalid, wrong-account, stale, timeout, rate-limit and recovery responses. Test zero slots separately from failed retrieval. Preserve a genuinely established cheap billing interval only through its verified end. Exercise all three demand settings, future return, expiry during a Pi outage and London DST. Slow provider/forecast work must leave the website and supervisor responsive.

**Pass point:** a non-programmer can select the household setting, see the expected demand/target assumptions and understand whether prices come from API, cache or defaults. Missing weather or device measurements are not disguised by tariff fallback. Reuse R05–R08; add the new GraphQL, demand-setting and web logic explicitly.

## Phase 4 Prove the planner in shadow mode

Build an executable battery simulator and EMHASS adapter for a continuous 36–48-hour horizon starting at actual SOC. Include demand and solar during cheap periods as well as the daytime, variable interval prices/extra slots, reserve, conversion losses, verified AC/DC limits and the actions the actual Anker control interface can execute. Keep a target-only policy separate from an ideal fully dispatchable analysis.

Evaluate paired demand/solar scenarios with visible provisional probabilities and a common first action. Compare expected import cost with common initial SOC and ending-inventory valuation. Independently reconstruct SOC, power balance, constraint compliance and cost before publishing a time-limited plan. Fingerprint actual state, input forecasts, demand settings, tariff/slot revisions, device capabilities and model versions.

Run live plans without equipment commands and archive what was known at each decision. Compare with full overnight charging and recorded Anker operation on equivalent periods. Start daily error reviews and chronological candidate-model comparisons; do not promote a model merely because it is more complex.

**Testing point:** prove cases where extra cheap charging is economical despite later solar curtailment, where a lower target is cheaper, and where an 85% grid target still permits 100% solar SOC. Reject intentional export under the normal policy, simultaneous battery charging/discharging and artificial import/export loops. Include low/high initial SOC, an infeasible target, extra-slot cancellation, manual override, partial inputs, invalid scenario probabilities and failed/non-optimal/late solver results. A stalled solver must not stall telemetry or the independent control supervisor.

**Pass point:** every published plan passes the independent checks and is reproducible from archived inputs. Invalid/expired plans cannot become commands. Complete the recorded shadow observation period and required operating cases; do not equate theoretical replay savings with measured causal savings. Reuse R09's publication/validation pattern, with the model corrections in CODE_REUSE.md.

## Phase 4b One controller, complete logic, run in shadow (commands logged, not sent)

**Scope after the 28 October clarification:** the controller's fast loop exists for the car (inhibit discharge while charging) and for garage monitoring; normal discharge to the house stays with the Anker firmware (so a missing plan or an unknown battery state does not inhibit discharge), and the charge level is a low-rate setting. Work package IDs: B1 to B6 in section 0.2.

One real-time controller process with the **complete** control logic, run live on the Pi in shadow mode. Going live is changing an execution-mode switch, not
writing control code. Status 27 October: built and tested offline (see `reports/control-loop-review-2026-10-27.md`); deployed to the Pi in shadow.

**Design rules (owner, 27 Oct):** as simple as possible; standard library only apart from the existing read-only device readers (exception approved 2 Nov: `transitions`, pinned); no database, web framework, solver or provider code; reads the hardware directly on its own clock.
* **Own process and unit,** separate from the web app, collector, planner and comparison. It takes an exclusive lock before opening any equipment connection or output file; a second instance is refused. The meter is refused until the original monitor is retired.
* **One decision function** (`control_logic.py`; being rebuilt since 2 Nov as the `transitions` machines of B7), identical in shadow and live, in the specification's priority order: reserve (5 %) and the 45 A garage limiter (since WP12: owner specification v1.3 section 9, a continuous calculation from the measured currents with immediate decreases and no recovery timer), EV inhibition (10 W cutoff, release only after continuous low readings, restarted by any outage), hand-over to the Anker app, then the plan. Every output is explicit; there is no "no change". It **starts restricted** after every restart and keeps no persisted timestamps.
* **Prepared commands** (`control_exec.py`): each decision becomes commands with id, issue and expiry time and a protective or economic priority. Expired commands are discarded; an older economic command can never run after a newer protective one; an ended economic request is explicitly withdrawn.
* **Execution mode** (the `control-mode` file, `shadow` or `live`; root-owned): shadow records every command as *suppressed*; live would send through a commissioned transport and track *attempted, acknowledged, observed* separately. Until that transport exists and is commissioned, a request for live is refused and the controller stays in shadow, saying so on the dashboard. Suppressed is never reported as acknowledged.
* **Watchdog:** `WatchdogSec` with a heartbeat only after a whole cycle (inputs, decision, preparation, execution, recording, logging) finishes promptly. A stall is restarted by systemd. Unavailable equipment gives the restricted decision and an unhealthy status, not a restart.
* **Command log:** one JSON line per command change in `control-commands.jsonl` (size-capped, one rotation), plus a journal line, so a week of shadow running can be reviewed.
* **Dashboard:** the controller's own heartbeat, mode, decision, restrictions, plan validity and each command's stage. A stopped controller goes stale while collection continues.

**Testing point:** boundary and recovery cases at 44.9, 45.0 and 45.1 A; reserve at 5 %; EV active, unknown, invalid and outage; corrupt, nested and expired plans; a delay in each stage; a stalled loop; duplicate controller; restart under restriction; expired and superseded commands; fake-transport acknowledgements, rejections, timeouts and observed effects (flagged simulated); identical decisions and prepared commands in both modes on one recorded trace.

**Pass point:** a week of shadow decisions with no unexplained difference from what the Anker system actually did, no missed deadline attributable to other services, watchdog drill passed on the Pi, and zero writes (wire capture shows read functions only). Not passed yet: the live transport and every physical check below belong to Phase 5.

## Phase 5 Commission battery control and constraints

**Order of work (28 October):** C1 read-only look at the settings registers, C2 choose the mechanism with the owner, C3 one controlled trial with the owner present, C4 failure drills, C5 garage limiter test (section 0.2). The mechanism candidates and what is and is not known are in SOFTWARE_SPECIFICATION.md section 10.1. The text below remains the acceptance standard.

Implement one command writer with issue/expiry times, idempotent retries, bounded timeouts and separate requested, acknowledged, read-back and observed states. Enforce the specification's priorities: fixed limits and EV inhibition, verified recovery state, manual override and then the economic plan. Start with the smallest documented command set and test each operation on the verified device before enabling it unattended. The vendor's documented control points (mode 10064, set-point 10071, SOC limits 60000-60002, overrides, no read-back of the set-point) are recorded in REALTIME_ACQUISITION_PLAN.md section 6.10 and are verified, not assumed, here.

Commission grid target versus solar ceiling, AC charge reduction/stop, EV discharge inhibition and the fallback/release procedure. The 45 A limiter and charger inhibition run independently of FastAPI, providers and the solver. Unknown garage current blocks charging increases and requests a verified stop; a failed acknowledgement remains an unknown control state. A lost extra slot cannot release EV inhibition.

**Testing point:** inject 44.9, 45.0 and 45.1 A frames, stale/invalid CT data, active-EV frames, missed deadlines, lost readbacks, writer contention and delayed commands. Demonstrate bounded reduction/stop, commissioned recovery hysteresis and protection against a stale plan clearing an active limit. Test the unknown-charger policy: retain inhibition through short Shelly gaps after charging, never treat missing power as charger-off, and require stable release and replanning from actual SOC. Use safe controlled load steps for installed-equipment tests; do not deliberately overload the garage circuit. In a commissioned installation, protective stop/inhibit/fallback commands must not wait for weather or a solver result.

**Pass point:** all mandatory control operations have verified semantics, acknowledgement, readback, physical response and recovery evidence within the measured commissioning criteria. Record unsupported cases rather than showing a green success state. No software claim replaces the circuit breaker. Household settings and manual requests must never bypass the fixed limits.

## Phase 6 Prove automatic deployment and recovery

Finish the versioned deployment tools prepared earlier. A configured, tested release trigger should deploy automatically over authenticated SSH; provide one-command bootstrap/redeployment as well. Pin compatible Pi dependencies, verify the target/architecture/port/resources and stage a release before switching. Use separate unprivileged systemd services with bounded restart-on-failure behaviour and persistent state outside replaceable release directories. The first installation remains observe/shadow.

Serialise updates and use the commissioned fallback before handing over the writer. Retain demand settings/dates, fallback prices, history, pause and valid overrides. An update must not promote shadow or paused operation into automatic control. Re-read identity, device state and constraints before economic commands resume; require a fresh plan for those commands while allowing verified protective actions in a commissioned installation.

**Testing point:** repeat installation/update, interrupt transfer, fail a migration, fail an HTTP/telemetry check and demonstrate automatic rollback when a compatible rollback is safe. Keep new observations/settings during rollback rather than blindly restoring an old database; incompatible rollback must produce a visible restricted state. Demonstrate crash recovery, a controlled reboot, an untrusted clock, lost network, changed device IP/subnet and ambiguous discovery. Check release identity, systemd enabled/active, HTTP health, browser settings, data freshness and operating mode separately.

**Pass point:** the selected release deploys without manual file copying, starts without a laptop/login, and either passes the complete health checks or reports a safe restricted/rollback result. The previous monitor and other Pi services remain available. R10 is a service-pattern example only; historical installers must not be run unchanged.

## Phase 7 Introduce automatic operation and daily review

Enable automatic operation only when its mandatory controls and recovery paths have passed phase 5 and the release/restart process has passed phase 6. A limited first live step with the owner reachable daily (gate G2) may come first; running unattended is gate G3 (POST_SHADOW_PLAN.md, RC2). Record the commissioning conditions and review period, covering the available tariff, demand, solar, charging and failure cases. Until missing equipment cases can be demonstrated, identify exactly which behaviour remains uncommissioned.

Review daily demand/PV forecast errors, unmet targets, constraints, command failures, partial coverage, actual Octopus costs and estimated/default-based costs. Check that backups can be restored. Evaluate forecast changes chronologically and retain rollback. Report annual/seasonal projections as provisional until enough evidence exists.

**Testing point:** the owner can demonstrate Away, Normal, High demand, a planned visit, pause, a time-limited override, return to automatic, missing Octopus credentials, charger standby/active/unknown history and device rediscovery without editing code. Verify useful recovery instructions for Pi power loss and a failed update.

**Pass point:** the commissioned application runs automatically with documented limitations, understandable status and reproducible daily outcomes. Savings reporting separates actual bills, estimates and policy replays, with matched periods and ending inventory. Close only the gates whose evidence is complete.

## Test coverage and evidence records

| Test group | Required coverage | Phases |
| --- | --- | --- |
| T01 Decoding and quality | Signed Modbus values, word order, units, null/NaN/Infinity/bool rejection, error flags, real retrieval versus device update | 1–2 |
| T02 Energy and history | Covered energy, gaps, minute/day boundaries, counters/reset/rollover, partial totals, CSV and restart consistency | 1–2 |
| T03 Car charging | Dedicated channel, 9.9/10/10.1 W, confirmation/release, full active power, standby retention, Unknown and session persistence | 2, 5 |
| T04 Time and settings | 23/25-hour London days, repeated clocks, future visits, expiry during outage, Normal-day eligibility and model provenance | 1, 3–4 |
| T05 Tariffs and fallback | API off/no key, independent price/slot status, token expiry, valid empty slots, malformed/stale/partial data, default validity and recovery | 3–4 |
| T06 Forecast chronology | Exact acquisition, immutable issuances, complete-day selection, no future leakage, curtailment exclusions and failed refresh | 3–4 |
| T07 Battery and economics | Actual initial SOC, AC/DC boundaries, continuous reserve, separate solar ceiling, no intentional export/simultaneous charge-discharge/flow loops, reachable targets, scenarios and cost reconciliation | 1, 4 |
| T08 Supervisor and commands | Single writer, priorities, expiry/idempotency, EV plus 45 A, unknown-charger policy, stale sensors/commands, readback, physical response and fallback; state machines accept only declared transitions, and `decide()` matches the v1.3 table for all 16 combinations (Hypothesis stateful invariants) | 4–5 |
| T09 Deployment and faults | Compatible migrations, interrupted release, unchanged services, mode preservation, reboot, clock/network/IP recovery and rollback | 2, 6–7 |
| T10 Website and access | Validated settings, responsive UI, source/quality labels, usable mobile views, authentication plus CSRF/origin protection and no secret disclosure | 2–3, 6–7 |
| T11 Telemetry store | Constant file size, gaps stay empty, partial minutes corrected by later data, single writer, backup under the lock, agreement with the half-hour records over a two-week parallel run | 4b, 5 |

Use exact expected values for deterministic decoding/state/coverage fixtures and documented numerical tolerances for integration and solver reconciliation. Establish physical response/freshness criteria from the real devices before commissioning a limiter; fixture success alone does not establish installed-equipment performance. Keep financial estimates and actual billed costs in separate fields.

Store evidence against each release: test results, fixture hashes, source/model/configuration versions, observation/plan IDs, command/readback traces and the owner-facing demonstrations. Phase reports should state **Pass**, **Fail** or **Pending**, with the remaining action. Do not pre-fill pending hardware/API/deployment tests as passed.

## Existing code baseline and reuse limits

On 24 October 2026, the following existing monitor command completed with **53 tests passed, no failures, errors or skips**:

```text
python3 -m unittest -v test_energy_store test_octopus test_observation_store test_live_dashboard test_independent_forecast_store test_emhass_advice_worker
```

The tests use fixtures/mocks and temporary databases. They establish a useful baseline for recorder, public REST, quality, forecast archive and advice-publication behaviour. They do not test the new controller, live account permissions, a real EV, Anker writes, the 45 A response or production deployment. The machine-readable result and test-module hashes are in **development/baseline-checks.json**.

Use the tests as examples and port those that match the new interfaces. Do not retain tests that require an obsolete model assumption merely to keep a suite green. The new package will need its own automated tests for each test group above, followed by the stated Pi and equipment demonstrations.

The important changes from the existing monitor are:

- Raw Shelly energy currently includes standby; filtered charging sessions and the below-10 W rule are new.
- The regular meter poller reads power; actual garage-current acquisition and the independent limiter are new.
- Existing load helpers use recent-day averaging or a daytime-only profile. Normal must use the most recent complete eligible Normal day, and planning needs the full horizon.
- The old native EMHASS comparison assumes initial reserve SOC, zero overnight load/PV and a fixed cheap window. Replace those assumptions before using it for actual control.
- The REST client supplies public prices and meter data, not the new authenticated GraphQL smart-slot adapter or the complete fallback policy.
- Current forecast workers can run with `stop_at: null`; older installer examples contain historical trial deadlines. Inspect the selected worker/configuration and remove inherited trial-only behaviour from the permanent application.
- Existing integration/publication helpers use blocking operations and shared website state. Extract reviewed logic; do not import the whole website into the controller or run disk/provider work on the protective loop.

## Development handoff

Start with phase 0 and the reusable-code guide. Deliver a working read-only Pi application before commissioning writes, then a shadow plan with recorded explanations, then the tested automatic controller and deployment/recovery workflow. Keep a short completed/pending list in the phase reports rather than treating the entire specification as one build task.

The implementation can be scheduled once the phase 0 capability review has exposed the hardware uncertainties. The end-of-October intention is not evidence that unverified controls or missing operating cases can be completed by that date.
