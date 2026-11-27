# Solarbank Cost Optimiser Software Specification

**Version:** 1.8 (revised approach recorded 2 November 2026; wording corrected after the independent review of 2 November; the owner's answers of 2 November recorded in REQUIREMENTS_AMENDMENTS.md; clarified by the owner on 28 October 2026; first issued 24 October as 1.6)  
**Date:** 2 November 2026  
**Intended implementation:** end of October 2026  
**Platform:** existing Raspberry Pi, independent of Home Assistant  
**Status:** specification only; no application build or hardware changes

## 1. Purpose and scope

Build a small application that does one job well: **automatically operate the Anker battery to minimise the expected cost of imported electricity**, using measured household demand, solar forecasts, actual tariffs and feedback from previous decisions.

The owner wants automatic scheduling with a clear manual override and three simple household-demand settings: Away from home, Normal consumption and High demand. The app must adapt to changing consumption and seasons without needing daily attention. Keep the interface and operation substantially simpler than the existing energy monitor.

Include forecasting, cost-based planning, verified battery control, Shelly-measured car-charging status/history, EV discharge inhibition, garage-feed current limiting, local device discovery and a concise explanation of decisions. Exclude appliance scheduling, EV charger control, solarbank-pi/payback modelling, general home automation and multiple competing live advisers from the first release.

The owner is comfortable with engineering concepts but is not a modern software developer. Use plain-language setup, status and recovery instructions.

### 1.1 Primary objective and division of labour (owner clarification, 28 October 2026)

**The key feature of this application is to decide, each evening, how full to charge the battery from cheap electricity**, so that tomorrow's demand is met by the most economic mix of solar (free), cheap overnight electricity and as little peak-rate import as possible. The same decision is repeated when Octopus issues an extra cheap slot. Everything else in this document supports that decision or protects the installation while it is applied.

* **Today** the battery is charged fully every night. That is mostly acceptable in winter, when solar rarely fills the battery (although some solar is lost even on sunny winter days), and wasteful in summer, when a sunny day's solar can refill it and the right overnight charge may be very low.
* **The choice is an economic one, not "expected demand minus expected solar".** Buying peak-rate electricity (about 35p/kWh at the time of writing) costs far more than buying surplus cheap electricity (about 7p/kWh) and wasting some solar or carrying some charge over to the next day. The target must therefore be chosen to minimise probability-weighted cost under forecast uncertainty (section 5), which pushes it towards charging more, not less, when the forecast is doubtful.
* **The Anker firmware stays in control of the moment-to-moment behaviour.** It matches house load, grid and solar, discharges to the house when appropriate and holds idle once the battery is full inside the cheap window (the owner wants that kept: the house then runs on cheap grid power). This application does not dispatch the battery second by second.
* **This application's controller does three things only:** (1) applies the chosen maximum charge level; (2) inhibits battery discharge while the car is charging; (3) monitors the garage supply (a 50 A supply, so a car charger plus the battery's 2.5 kW AC charging should not reach the 45 A operating limit) and reduces or stops AC charging if that limit is exceeded. Anything else the Anker does by itself is left alone.
* **Timing consequence.** The one-second supervisory loop is required for the car-charging inhibition and is a desirable safeguard for garage monitoring. The charge level itself is a low-rate setting decided and applied a few times a day, not a continuously controlled quantity.
* **Success** is judged weekly in pounds saved against a home with neither solar nor battery (an estimate, labelled as such), with an optional running total of cumulative savings counted from the day the application goes live (shadow-mode running does not count). The comparison with "always charge fully" remains a secondary, matched-period analysis (section 14).

## 2. Confirmed installation and policy

| Item | Requirement or known configuration |
| --- | --- |
| Battery | Anker SOLIX Solarbank 4 E5000 Pro, AE1032Z1; nominal 10 kWh, comprising 5 + 5 kWh |
| Minimum SOC | 5%, equivalent to 0.5 kWh at nominal capacity. **The owner has set 10 % (1 kWh) as the default, kinder to the battery, and it is adjustable on the settings page** (not hard-coded). The app's Backup Reserve is not used |
| Solar ceiling | 100% SOC |
| Grid-charge target | The maximum charge level set each evening from the forecast (section 1.1); manually overridable. How a level below the vendor register's 80 % minimum is realised is open (section 10.1) |
| AC inverter | 2.5 kW shared AC limit; verify directional limits and simultaneous-flow behaviour on the actual firmware |
| PV | Four 500 W panels, 2 kWp total, all south-facing |
| Wall panels | Two panels, 1 kWp total; approximately 80° from horizontal, reported range 76–84° |
| Ground panels | Two panels, 1 kWp total; approximately 20° from horizontal |
| Location | an example location in central London, 51.5074, -0.1278 (set your own: see CUSTOMISING.md) |
| Shading | East/west obstructions reported; seasonal extent remains to be characterised |
| Tariff | Intelligent Octopus Go; optional Octopus API for effective-dated prices and qualifying smart slots, with user-configured tariff/window defaults when API data is unavailable or invalid |
| Standard cheap window | 23:30–05:30 Europe/London; verify continued applicability at commissioning |
| Extra cheap slots | Qualifying Intelligent Octopus Go smart-charging slots also give the house the cheap rate; automatically use them to charge the Anker battery towards its current grid-charge target, including outside the standard night window |
| Export | Disabled for ordinary operation; remuneration exactly £0/kWh |
| Household | Retired occupants; no assumed weekday/weekend demand uplift |
| Demand setting | User selects Away from home (base load), Normal consumption (default) or High demand (family expected home); apply the selected demand forecast to overnight and extra-slot charging plans |
| EV | An EV now exists (owner, 2 November 2026); the charger is monitored on Shelly `em1:1` |
| Charger standby exclusion | Dedicated Shelly car-charger readings below 10 W do not count as active charging or car-charging energy; retain the raw measurements and actual standby consumption in household/garage totals |
| Garage feed | Entire garage supply measured by Anker Sub CT, also labelled PV / ESS CT |
| Feed operating limit | 45 A; reduce Anker grid charging if exceeded. The garage supply is 50 A, so car charger plus battery AC charging should not reach it in normal use: the limiter is a safeguard, not a routine constraint |
| Circuit protection | User reports a 50 A protective breaker; software does not replace it |
| Commissioned configuration | Use 10 October 2026 onwards for installed-system performance training |


Do not hard-code October tariff prices. Historical rates must stay attached to their effective dates; future planning uses the tariff applicable to the planned intervals, or explicitly labelled configured defaults under section 7.2. 

## 3. Architecture recommendation

Use a **small modular Python application**, SQLite and systemd. Avoid microservices, a message broker and a large front-end framework in the first release.

Separate the fast control path from slow planning, provider requests and browser requests:

```text
Anker battery + Anker meter + Shelly
                  |
        Device adapters / shared polling
                  |
         Validated live state --------> telemetry store (Whisper) + SQLite records
                  |
          Control supervisor <-------- latest valid economic plan
                  |                             ^
         Single command writer            Planning worker
                                                ^
                              Tariff + demand + solar forecasts
                                                ^
                                   Daily learning and evaluation

Browser dashboard -> authenticated local API -> supervisor requests
```

Recommended process boundaries:

1. **Controller service:** owns device connections, reads live measurements, applies the current/EV limits and the chosen charge level, and is the only process allowed to write battery settings. Its fast loop exists for the car-charging inhibition and garage monitoring; the charge level is applied as a setting a few times a day (section 1.1). Its decision logic is a set of explicit state machines built with the `transitions` library (section 7.3).
2. **Planning worker:** produces timestamped plans; performs forecasting, simulation and EMHASS solves outside the control loop. Publish plans atomically with validity times and input/model fingerprints.
3. **Web service:** FastAPI serves the lightweight dashboard and validated settings API; no direct equipment writes and no synchronous solver calls. Use server-rendered HTML templates, CSS and a small amount of JavaScript for live updates.
4. **Daily review job:** updates error statistics and evaluates candidate forecasting models. It may share worker code, but cannot bypass the supervisor.
5. **Telemetry logger (part of the collector):** the only writer of the fixed-size telemetry store (section 14). The controller never writes to it; the planner, web service and daily review only read it.

Use one connection owner per physical Modbus device where possible. The existing installation has encountered limited concurrent Modbus clients. Define how the existing recorder will share readings or relinquish its device connection before running the new controller; do not simply add more pollers.

Keep a clean new project and state database. Read/import the existing recorder without modifying its history. An adapter can use existing telemetry during development, but the final app must not require the old website or Home Assistant to operate.

## 4. Required data and measurement boundaries

Collect SOC; PV, household, battery and grid power; signed Solarbank AC circuit power; operating mode; charge/discharge counters; garage-feed current; and dedicated EV charger power. Preserve raw readings with source, units, UTC time, retrieval status and device update time when genuinely available.

Use canonical internal units: **kW, kWh, A, V, GBP/kWh and SOC fraction**. Make conversions explicit at external interfaces. Store UTC timestamps; use Europe/London for display, tariff windows and calendar days.

- Main Anker CT measures the property grid connection.
- Sub / PV / ESS CT measures the entire garage feed: Solarbank, charger and other garage appliances.
- Shelly `em1:0` measures the whole Solarbank AC circuit, including possible operating consumption. Its sign: positive is Solarbank output to the house; negative is AC input into the Solarbank, whether charging from the grid or its own standby draw (verified in the 15 October sign review, `history/source/idle-sign-review-15-oct.json`; the owner identified the -46 W seen at 02:06 on 2 November as that standby draw).
- Shelly `em1:1` measures the EV charger; the original monitor, its website and its payload count from 1 (`ch1` is the Shelly's channel 0, the Solarbank, and `ch2` its channel 1, the car), so the code names the two circuits by role (`solarbank_ac` and `car`, in `src/solarbank_optimiser/shelly_channels.py`) and translates the monitor's names in one place. Use its validated active power in watts for car status/history, with the below-10 W standby exclusion in section 8.1; read available energy counters without resetting them.
- Household power register 10010 is a derived Anker balance, not an independent household meter.

Do not add the garage CT to household consumption or compare total Shelly circuit output directly with the battery discharge counter. Keep counter-derived consumption as a separately labelled diagnostic. Do not scale household power to match Anker app totals.

Validate signs with known operating states. Preserve counter resets, possible rollovers, quantisation and missing coverage rather than converting them into zero. Retrieval count, payload change and independent device measurement updates are different concepts.

Actual Octopus metered imports and supplied billing information take financial precedence. Retain estimated-cost labels from supplied exports. Public tariff schedules are not proof of every historical applied Intelligent rate; a zero-import interval cannot reveal its applied price.

When enabled and available, obtain account-specific Intelligent smart-charging dispatches and qualifying status through a verified read-only Octopus source. Record slot start/end, retrieval time, revisions/cancellations and the evidence for the household rate. Ordinary published tariff schedules and Shelly charger power alone cannot identify extra cheap household slots. Missing credentials or failed/invalid responses must leave default-window operation available as specified in section 7.2.

## 5. Expected monetary value and battery model

For every candidate **executable** policy `a`, calculate:

```text
Expected cost(a) = sum over scenarios s of:
    probability(s) * [sum over intervals t:
        import_power_kw(a,s,t) * interval_hours(t) * import_price_gbp_per_kwh(t)
        - ending_stored_energy_value_gbp(a,s)]
```

Use equal initial battery state and a common, documented terminal-inventory valuation across policies. A starting implementation can value usable ending stored energy at the next cheap replenishment cost divided by charge efficiency, with sensitivity checks. Actual bills never receive an inventory credit.

**Cheap charging, overnight or in an extra smart slot, can be preferable to expensive later under-purchase.** Select the lowest probability-weighted monetary cost, even when that means some subsequent solar is curtailed. Do not minimise curtailment, maximise cycling or force headroom as independent objectives. Resolve genuinely equal-cost plans using fewer commands and then solar headroom; do not disguise a material cost tolerance as a mathematical tie.

Constraints must include:

- Continuous energy state through midnight, 5% reserve and 100% maximum SOC.
- Charging/discharging efficiencies and verified power/conversion boundaries, without counting the same loss twice.
- Shared AC inverter limits, device charging limits and available cheap-window duration.
- No simultaneous battery charging/discharging or artificial import/export loops.
- No remuneration or intentional export under the normal policy; curtail surplus PV where necessary.
- EV discharge inhibition and garage-feed charging reductions, including manual operation.
- Reachability from **actual current SOC**, rather than assuming each night starts at 5%.

Existing charge/discharge efficiencies of 0.86/0.90 are **provisional modelling assumptions**, not validated measurements. Record model versions and uncertainty. Do not add grid-supplied standby consumption twice. Any degradation cost must be explicitly configured and shown; do not invent one.

The 2.5 kW limit is an **AC interface** limit, not proof of the same limit on every internal PV/DC charging path. Keep AC grid charging, direct PV-to-home and PV-to-battery flows separate and verify their simultaneous limits.

Use EMHASS with supplied site forecasts as the proposed planning engine. It must be wrapped by our physical checks and executable-policy evaluation. If only target/schedule controls are supported, evaluate a common grid-charge target across scenarios for each confirmed cheap opportunity under actual Anker behaviour. Do not average independently optimal scenario plans that secretly choose different actions before the uncertainty is observed. Advanced scenario planning must share the same first action until new observations become available.

Native perfect-information dispatch is an upper-bound analysis, not evidence that target-only control can realise the same savings. Reject non-optimal/invalid solves; check power balance, SOC reconstruction, export, constraints and objective reconciliation independently.

## 6. Forecasting, uncertainty and seasonal learning

### 6.1 Initial demand profile

For Normal consumption at the end-of-October launch, start with the **most recent complete London calendar day's time-of-day household profile** representing normal use. Do not use today's partial day or a day containing a tagged Away from home or High demand period. If yesterday is incomplete or unsuitable, use an explicitly identified earlier complete normal-use day within a configurable maximum age; otherwise do not issue a new economic plan. Apply the selected demand setting below when constructing the forecast.

Check actual observation coverage and freshness, not merely row counts. Correctly handle 23/25-hour DST days and repeated/skipped clock times; do not assume every day contains 48 half-hours. The older three-day mean remains a comparison candidate, not the new default requested by the owner.

### 6.2 Solar prediction

Use Open-Meteo irradiance/weather and pvlib for the two separate panel planes. Preserve provider timestamps, irradiance averaging conventions and exact forecast acquisition time. Cache weather responses and archive forecasts immutably. Never substitute clear-sky or historic-average production for a weather forecast.

Track confirmed/suspected export-limit or full-battery curtailment separately. Such observations are censored generation and must not train available-PV potential downward. Full SOC alone does not prove curtailment, and forecast error alone must never be used to decide that an observation was curtailed. Exclude unsuitable or ambiguous periods from available-PV calibration and report the exclusions.

### 6.3 Feedback loop

Each completed day, compare the archived forecasts and parameters available at each decision with measured demand, PV, SOC, imports and costs. Track errors by time of day, solar elevation, weather and season. Detect persistent background-load changes without automatically attributing them to seasonality.

Start with transparent provisional solar/demand scenarios and probabilities. The previous solar 70/100/120% and load 90/100/115% cases may be used as sensitivity cases, but are not measured quantiles or validated probabilities. Replace them progressively with observed paired forecast errors; preserve correlations and separate different forecast lead times.

Reject non-finite scenario inputs, negative probabilities or weights that do not sum to one. Record which forecasts, scenarios and parameter version produced each decision.

Weekly, compare the active forecast with persistence, recent-day averages and, when justified by sufficient history, a simple regression model. Test chronologically using only prior information. Promote a version only after documented improvement in expected control cost, acceptable prediction errors and no constraint regressions; retain rollback. Do not promote machine learning merely because it is more complex.

Track 7/30-day consumption and generation trends, monthly/seasonal profiles, measured forecast bias and the monetary cost of shortfalls versus unnecessary cheap charging. Use weather and sun position to distinguish seasonal effects from household changes. One autumn dataset does not establish annual behaviour.

### 6.4 User demand settings

Provide one clearly labelled **Household demand** selector with exactly these three choices:

| Setting | Demand forecast and charging behaviour |
| --- | --- |
| **Away from home** | Optimise charging to cover the home's essential base load while unoccupied, including appliances and services left running. Exclude normal occupied-home activities. Allow for forecast solar, conversion losses and uncertainty; do not assume that being away means zero consumption or always means no grid charging. |
| **Normal consumption** | Default setting. Use the normal household forecast from section 6.1 and its subsequent validated learning, without an automatic weekday/weekend uplift. |
| **High demand** | Use the normal forecast plus an explicit additional household-energy allowance for family visits and other expected extra use. Plan enough cheap charging to cover that higher demand where economic and physically reachable; do not simply force a fixed SOC or 100% charging. |

Estimate the away base load from suitable complete, valid observations of confirmed away or known essential-load-only periods, accounting for cycling appliances rather than using a single minimum power reading. Provide an editable base-load value in watts and high-demand allowance in extra kWh per day, with an optional time window for the extra use. Show each value's source and whether it is provisional; establish initial values during commissioning rather than presenting an arbitrary percentage uplift as learned behaviour. These are occasional setup adjustments: the main interaction is selecting one of the three settings.

Apply a selection immediately by default, with an optional start and end/return date and time for an absence or family visit. Without an end, retain it until changed; at a specified end return automatically to Normal consumption. Show the duration explicitly and allow cancellation. Use Europe/London for user input/display, persist UTC boundaries and the selection across restarts, and honour an expiry that occurred while the Pi was off.

Replan promptly when a selection, its values or its dates change or take effect. Apply the appropriate demand profile to each part of the planning horizon, including a planned return home or family visit, so the preceding cheap window can prepare the battery before people arrive. Recalculate the grid-charge target for both night and qualifying extra Octopus slots. Show forecast household kWh and the resulting target/reason. A demand setting changes the forecast, not the priority of pause, a direct grid-target override, EV discharge inhibition or the 45 A limiter.

Tag observations and archived forecasts with the demand setting and parameter version effective at the time. Keep Away and High demand periods separate when learning normal consumption; use them to improve their own profiles as evidence accumulates. Historical replays must use only selections and planned dates known at the decision time. Missing or poor data must remain visible rather than producing an invented confident target.

## 7. Scheduling and operating states

Proposed defaults, to be validated on the Pi:

- Look ahead 36–48 hours, including the next charging opportunity.
- Plan in 30-minute intervals; validate shorter-duration power and current behaviour separately.
- Plan before 23:30 and refresh every 30 minutes or on material changes (the controller itself is told every few minutes that the plan still stands; see 0.5.36).
- Refresh provider weather only on its configured schedule; reuse cache between planning runs.
- Execute only the next valid action; supersede the remaining schedule after replanning.

States: **observe**, **shadow**, **automatic**, **manual override**, **constrained**, **fault/recovery**. Show the active reasons independently: EV detected, garage limit, data missing, override or no valid plan. An override has an explicit expiry and never bypasses hard operating constraints.

Use current SOC and validated tariff inputs, identifying any configured fallback prices/windows separately from API-confirmed data. Standing charge is shown for billing but excluded from dispatch decisions.

### 7.1 Extra Intelligent Octopus Go charging slots

**Automatically charge the Anker battery during qualifying extra Intelligent Octopus Go slots**, including daytime/evening slots outside 23:30–05:30, towards the current forecast-based grid-charge target. Recalculate the target from actual SOC, expected demand/PV, the slot's remaining duration and the next confirmed cheap opportunity; respect a current manual override or pause. Do not force 100% SOC regardless of need or assume that extra slots will be offered before they are issued.

Octopus confirms that qualifying smart charging outside the normal night window also gives the house the off-peak rate. Its published explanation describes half-hour billing and account-dependent smart-charging allowances. Verify the installed account's applicable rules at commissioning; keep household and EV prices distinct where they differ. See [Intelligent Octopus Go](https://octopus.energy/smart/intelligent-octopus-go/) and [Octopus's smart-charging and Charge Cap explanation](https://octopus.energy/blog/intelligent-octopus-go-smarter-charging-for-a-greener-grid/), checked 24 October 2026.

- Use current Octopus dispatch/qualifying evidence to establish cheap household intervals. A plugged-in car, detected charger load or manual/bump charge alone does not establish an extra cheap slot.
- Replan promptly when a slot is issued, starts, changes, is cancelled or ends, and when its qualifying status changes; do not wait for the routine 15-minute refresh. Check tariff eligibility at execution and split planning intervals at actual price boundaries where necessary.
- If a planned slot is withdrawn, expires or cannot be verified, remove its authority for extra-slot battery charging and replan. Honour any already-established cheap billing interval through its verified end, including when qualifying EV charging stops part-way through that interval; do not extend it by guessing. The standard verified night window remains independently available.
- Simultaneous EV and Anker charging must obey the 45 A garage-feed limiter. Reduce or stop Anker AC charging when headroom is insufficient, keep EV discharge inhibition active, and recalculate any unmet battery target at the next valid opportunity.

### 7.2 Optional Octopus API and default operation

Provide an optional **Use Octopus API** setting, initially off until configured, with secure API-key entry, required account/tariff identifiers and a **Test connection / Refresh** action. The system must remain usable without an API key. Disabling the option stops automatic Octopus requests and selects saved defaults; an already-established cheap billing interval may retain its verified end under section 7.1. Handle published tariff prices and authenticated Intelligent dispatch/household-eligibility data as separate inputs: published prices can be available without a key, but do not establish personal extra slots. Verify the applicable interfaces and permissions during implementation; see the [Octopus REST documentation](https://developer.octopus.energy/guides/rest/api-endpoints/) and [GraphQL documentation](https://developer.octopus.energy/graphql/), checked 24 October 2026. Keep keys server-side in protected configuration; never return them through status, exports or logs.

**Documented smart-slot interface, checked 24 October 2026:** Octopus's [GraphQL query reference](https://docs.octopus.energy/graphql/reference/queries/#flexplanneddispatches) documents `flexPlannedDispatches(deviceId: ...)` at `https://api.octopus.energy/v1/graphql/`, returning `start`, `end`, `type` and `energyAddedKwh`. Discover the registered car/charger ID with `devices(accountNumber: ...)`. Use the documented [API-key/JWT authentication](https://docs.octopus.energy/graphql/reference/mutations/#obtainkrakentoken) and handle token expiry without storing an account password. The older `plannedDispatches(accountNumber: ...)` query is deprecated and must not be the new integration's foundation.

These are planned device slots, not a final household bill or Shelly-measured charging energy. Use qualifying smart slots, not manual/boost requests, and verify account-specific eligibility. A plugged-in car does not guarantee that a schedule has already been issued; refresh within provider limits and track revisions. The connection test must verify the account's permitted linked device and a valid dispatch response, including a valid empty list, rather than reporting full success from public tariff prices alone. Account API permissions and actual schedule availability still require commissioning on the owner's account; documentation checks do not constitute a live account test.

**Octopus's published rules for Intelligent Octopus Go (supplied by the owner, 30 October 2026, from Octopus's own blog post "Intelligent Octopus Go: smarter charging for a greener grid"):** there are four rates. (1) The **home off-peak rate** applies to the whole house every day from **23:30 to 05:30**. (2) The **car allowance** is up to **6 hours of off-peak charging per day, measured midday to midday**; Octopus may place it inside the home's off-peak window or at other times when the grid is greenest. (3) The **bonus home rate**: when the car is scheduled outside 23:30 to 05:30, the whole home also gets the off-peak rate for each half-hour that falls within the car allowance. (4) The **standard rate** applies at all other times, and the car reverts to it when it charges for more than 6 hours (or the owner uses Boost), even at 2 a.m. while the house is still on the off-peak rate. So: the allowance period is **midday to midday** (not a rolling 24 hours), time in the home's own window counts towards the 6 hours, and an extra home-wide cheap period exists only for scheduled (SMART) dispatches outside the window and within the allowance. The owner has no EV yet (30 October), so no extra slots occur; the planner keeps ignoring dispatches until a car exists and the rule can be commissioned. Requiring that the car is actually charging, as the owner's control specification does, is a sensible extra check because a dispatch can remain listed after the car is unplugged.

Persist these editable **Fallback tariff** settings independently of live API responses:

| Setting | Default-operation requirement |
| --- | --- |
| Day and cheap import prices | User-confirmed values including VAT, entered/displayed in p/kWh and converted explicitly to GBP/kWh internally; record effective date and any known expiry. Establish values at setup from the account, not invented national prices. |
| Regular cheap window | Initially 23:30–05:30 Europe/London, editable; apply overnight boundaries and DST correctly. Outside this window use the configured day price. |
| Extra cheap slots | None assumed from defaults. A separately verified eligible smart-charging/billing interval remains usable only within its verified validity. |

Use the following selection and recovery rules:

1. Use valid API prices for their covered intervals, then still-valid cached prices, then the configured fallback day/cheap prices for uncovered intervals. Validate source/account/product, currency, VAT/units, finite prices, timezone-aware interval bounds, effective dates, coverage and conflicting records. Record the source of each interval; never convert a missing price to zero. Cached prices need explicit validity and a configured maximum age, not indefinite reuse after failure.
2. Missing/invalid credentials, timeouts, connection/authentication/rate-limit/server errors, malformed data and incomplete/error-bearing responses trigger fallback for the affected input or interval. An HTTP success alone is insufficient. Validate usable components separately so a dispatch failure does not discard valid tariff data, or vice versa. An empty but valid current dispatch list means no extra slots and supersedes obsolete future dispatches.
3. If extra-slot data is unavailable or stale, assume no additional cheap slots beyond the valid regular tariff schedule. Retain independently valid regular API/cached tariff data, using configured window/rates only where regular coverage is unavailable; never infer extra slots from EV power or reuse an unconfirmed future dispatch. Preserve any separately established cheap billing interval only through its verified end, as in section 7.1. If that interval is valid but its price source fails, use the configured cheap price with fallback provenance. Tariff/dispatch failure alone does not release EV discharge inhibition or suspend the 45 A limiter.
4. Continue cost-based planning with the selected household-demand setting, current SOC and otherwise valid forecast/device inputs. Label resulting costs as estimated from defaults. Missing Octopus access alone must not stop normal automatic operation. If the defaults themselves are missing, expired or invalid, show the required settings and inhibit new economic charging commands until a valid price/window source is available; existing protective actions remain active.
5. Fetch/retry outside the controller loop using bounded timeouts and capped backoff. Retain the last good data without replacing it with a bad response, but honour its freshness/validity limits. Replan promptly on failure, expiry, configuration changes and validated recovery; preserve pause and manual-override priorities. Apply recovered data only to appropriate future/current intervals, without rewriting archived decisions or extending a command past its eligible interval.

Show **API data**, **Cached data** or **Fallback defaults** for tariff and extra-slot availability independently, with the last successful retrieval, reason for fallback, next retry and current/next cheap window. An absent API key is a supported setup choice. Explain `Using configured night window; extra slots unavailable` when appropriate. Successful API responses must not silently overwrite the user's saved fallback settings.

### 7.3 Controller state machines (revised approach, 2 November 2026)

The controller's decision logic is built as explicit state machines using the [`transitions`](https://github.com/pytransitions/transitions) library (MIT licence, pinned; the controller's one approved third-party library, owner approval 2 November 2026). The state table is the decision table of the owner's functional specification v1.3 (`reference/owner-research/SOLARBANK_CONTROL_SPEC-v1.3-2026-10-30.md`, sections 5 to 11), which the owner confirmed is correct: `decide(valid, cheap, below_target, ev)` chooses CHARGE, ZERO_OUTPUT, DISCHARGE or FAULT; actions write registers 10064 and 10071 in the order of its section 10; HOLD and HANDS_OFF follow its section 11.

- Each machine is a declared table (states, triggers, guards) built through one shared helper. Transitions are pure: no device I/O, file writes or database calls. Only declared transitions can happen; an undeclared one is rejected and logged, never taken, and never stops the loop. Every state change is written to the change log with its trigger. Elapsed-time rules use the monotonic clock. Each machine's diagram is generated from its table.
- The machines are: action (STARTUP, CHARGE, ZERO_OUTPUT, DISCHARGE, HOLD, HANDS_OFF), mode supervision, the EV signal, the below-target signal, the smart slot, the command lifecycle (requested, sent, acknowledged, read-back, confirmed, failed, superseded), garage regulation, the system and run modes (including hand-over), and owner automation (automatic, paused, override with an explicit expiry). The daily record has its own small machine on the records side (provisional, needs recalculation, final). "Constrained" above is any restricting state in these machines, with each reason shown independently.
- **In a cheap period (the regular off-peak window or an active smart slot) the battery never discharges.** It is either charging or held at zero output in third-party control, whether or not the car is charging (owner rule, 1 November 2026). "Cheap" is derived from the clock and the verified smart slots, never from the plan's window. Car detection works at any tariff (section 8).
- The priority order of section 10 applies: protections, then fault recovery, then manual override, then the economic plan. A stale plan or a manual request never clears an inhibition.
- The detailed design, open decisions and work packages are in `POST_SHADOW_PLAN.md` and GitHub issues #26 and #29.

## 8. EV charger monitoring and discharge inhibition

**Amendment (owner, 2 November 2026):** car detection uses the owner specification v1.3 thresholds everywhere (the controller, the car card and the history): charging is detected above 1,000 W for 3 s and ends below 300 W for 120 s. Where this section and 8.1 say 10 W or a 60 s release delay, the amendment applies (`REQUIREMENTS_AMENDMENTS.md`; `POST_SHADOW_PLAN.md` D7 and WP26). Missing data stays missing data (D15).

Detect activity from dedicated Shelly `em1:1`, never from the combined garage CT. Use the 10 W cutoff in section 8.1; establish configurable confirmation/release delays through commissioning without silently changing that cutoff. Detect at any tariff, not only in cheap periods. Charger standby below 10 W alone must not trigger EV discharge inhibition; retain the documented release delay after real charging. **Missing data is missing data (owner decision, 30 October 2026):** a charger reading that is absent, stale or invalid never means "car charging". A gap of up to 10 seconds changes nothing (the last reading carries on); a longer gap inhibits discharge only while the car was known to be charging when last seen (for up to 10 minutes), or during the first release delay after a restart while the charger has not yet been seen idle. Otherwise discharge stays with the Anker, because losing cheap battery energy to a flaky meter costs more than a few minutes of the battery feeding a car; the gap is shown as a note, not as a restriction.

While charger load is detected, inhibit **battery discharge**. Direct PV can still supply the house/charger and PV can charge the battery. Allow AC battery charging when permitted by tariff, target and the garage current limiter, both overnight and in qualifying extra cheap slots. During an extra slot, charge the Anker battery alongside the car towards its grid-charge target. Loss of extra-slot eligibility does not release EV discharge inhibition. There is no requirement to control or reduce the charger itself.

When charging ends, wait for a configurable stable low-power period and replan from actual SOC before restoring normal discharge. Keep inhibition during short communications gaps following detected charging; do not interpret missing data as charger-off. Define and test the conservative unknown-charger policy before automatic operation.

Display and log `Battery discharge paused: EV charging`. A manual override cannot enable discharge into an active charger under the normal policy.

### 8.1 Car-charging status and measured history

Show a **Car charging (Shelly)** card on the main dashboard, using the verified dedicated charger channel and its current active power, last successful reading and data freshness. Use the same threshold/confirmed activity state for display, history and EV discharge inhibition:

| Status | Meaning |
| --- | --- |
| **Standby / not charging** | **(Amended 2 November 2026, D7, built in WP26: a session starts above 1,000 W for 3 s and ends below 300 W for 120 s; in between nothing changes; the definition is `ev_policy.py`. Where this table says 10 W, read those thresholds. Energy accounting still excludes sub-10 W standby.)** Fresh valid charger-import power below **10 W**, including zero, once any post-charging release delay is satisfied. This power alone never starts a charging session and contributes no car-charging kWh. |
| **Charging detected** | Fresh valid charger-import power **at least 10 W**, including exactly 10 W, after the commissioned confirmation delay. Show measured W/kW, current session charging kWh and elapsed active charging time. During the stable-low-power release delay retain the confirmed state with `Confirming charging has stopped`; exclude those sub-10 W periods from charging energy/time. |
| **Unknown / data unavailable** | Missing, stale, invalid or failed Shelly readings. Show the last known state/time separately; never label missing data as zero or confirmed standby. |

Use [Shelly EM1 active power](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EM1/), not apparent power or the aggregate garage CT. Verify channel identity, import direction and error flags at commissioning. Power alone cannot establish whether a car is plugged in, fully charged or waiting, or reveal the car's battery percentage; use only the measured status labels above.

Record persistent charging sessions with a stable ID, start/end times, active duration, measured charging energy and peak power, plus daily totals and a date-filtered power/energy history. Support today, recent days and a custom date range, with a CSV download. Group short pauses using the commissioned end delay so threshold flicker does not create many sessions, but exclude all sub-10 W periods from active duration and charging energy even while the session/inhibition remains latched. At or above the cutoff count the full measured power; do not subtract 10 W from every active reading.

Calculate charging kWh from valid adjacent observations with explicit coverage and a maximum permitted sampling gap. Do not integrate across communications outages, restarts or missing readings; mark affected sessions/totals as partial. Reconcile available [Shelly EM1Data counters](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EM1Data/) separately: a raw counter delta can include standby or unobserved energy and is not automatically a standby-filtered charging total. Never reset device counters as part of collection. Label the result as measured charger-circuit energy while charging, including charger losses, rather than energy added to the vehicle battery.

Preserve raw readings and actual charger standby energy in household/garage measurements, base-load forecasting and bill accounting. The exclusion identifies active car charging for status, history and EV discharge inhibition; it does not remove real load from the 45 A limiter or create a second household-energy total. Persist session state/coverage across app and Pi restarts without duplicate sessions or energy. Keep midnight-crossing sessions linked while splitting daily energy by Europe/London date, handling DST correctly. Do not reconstruct older sessions where the necessary observations are unavailable; imported coarse/counter-only history must keep its limitations visible.

## 9. Garage-feed 45 A limiter

Read the actual garage **Sub / PV / ESS CT current**, on its commissioned phase. Use verified current magnitude/semantics and measured current for feedback, not watts divided by a fixed 230 V.

Run this limiter independently of the solver and web interface. A proposed one-second poll must be checked against real device refresh and actuator response; successful polling does not prove a new measurement every second.

1. If valid garage current exceeds **45 A**, immediately request a reduction in Anker **AC grid-charging power**.
2. Continue reducing until measured current is below the operating limit; stop AC charging if required.
3. Verify acknowledgement, setpoint readback and observed response. Use bounded reductions and avoid control oscillation.
4. Restore gradually only after current stays below a lower recovery threshold. A proposed starting point is 43 A for 30 seconds; tune through commissioning, not guesswork. **Superseded (owner, 2 November 2026, D14; built in WP12, `garage.py`):** owner specification v1.3 section 9 applies instead. The charge power is `P_avail = min(2500 W, device maximum, (45 A − (I_eff − I_batt)) × V)`, a decrease is written at once, an increase needs 50 W and 5 s, there is no recovery timer, and charging starts once the garage reading has been valid for 30 s.
5. If other garage loads exceed 45 A with Anker AC charging already stopped, retain the stop and alert. Do not claim the app can reduce the other appliances.

This limiter overrides both the economic schedule and manual charging requests. Do not increase battery discharge to solve a garage overload while EV discharge inhibition is active. Do not apply its limit as a global SOC cap or unnecessarily restrict PV charging. Recalculate any unmet grid-charge target after a constrained interval, whether overnight or during an extra cheap slot.

Missing/invalid/stale garage current prevents initiating or increasing AC charging and requests an acknowledged stop of existing AC charging. An unacknowledged stop is **unverified control state**, not confirmed zero charging. Log and display it. The 50 A breaker remains the circuit protection; software cannot guarantee avoidance of every transient overload or trip.

## 10. Local control, priorities and recovery

Use verified local Anker commands. The current integration is deliberately patched read-only; its existence and the app's separate grid target do not prove our code can write that target or continuously limit AC charging. Do not infer undocumented write registers.

Before enabling automatic control, document and demonstrate: device identity; read/write register semantics; units; ranges; charge-target behaviour; AC charge-limit behaviour; discharge inhibition; mode transitions; readback; command expiry/heartbeat; and a tested fallback/release procedure.

Priority order:

1. Device protections and fixed operating constraints: reserve, verified power limits, export policy, 45 A charging limit and EV discharge inhibition.
2. Fault recovery and an explicitly verified controllable state.
3. Manual override, within those constraints.
4. Economic plan.

Use one writer, explicit command IDs, issue/expiry times, input-state sequence, idempotent retries and bounded timeouts. Never allow a stale planner result to release an active limiter. Record requested, acknowledged, read-back and observed states separately.

On restart, acquire the controller lock, discover/verify devices, read actual settings, restore live constraints and generate a fresh plan. Never replay an expired schedule. Test fallback under lost Pi, network, CT, Shelly and device communication. Returning to Anker autonomous mode is acceptable only if its resulting behaviour is understood: it might otherwise resume unwanted EV discharge or full-power charging.

If communications are lost, report what is unknown; the app cannot guarantee an action it cannot confirm. Preserve household grid supply and do not command unrelated relays or garage appliances.

### 10.1 How the charge level is applied: open until commissioning

**What the owner reports from the Anker app (28 October 2026):** the charge limit applies to **grid charging only**; solar still fills the battery to 100 %. In **Time of Use mode** the maximum SOC has a minimum of 80 % (it exists to protect battery life). In **Dynamic Tariff mode** and in **Smart (AI) mode** the maximum charge can be set to any level. The full range must be available in summer. The owner does not use the app's Backup Reserve setting, and suggests a 10 % minimum SOC for battery life (section 2). **Added 29 October:** the Anker **app** (a software limit, not the battery's) accepts **whole-hour slots only**, so the Anker's own scheduled grid charge starts at 00:00 and cannot cover the 23:30 to 00:00 half hour of the cheap rate (nor end at 05:30). Planning and the shadow comparison (B3) must treat the half-hour as unavailable to the Anker's own schedule; only a controller that applies the level itself can use it.

**What the vendor documents** (integration commit 21c9483; Anker's Home Assistant guide, https://anker-webview-eu.anker.com/home-assistant, checked 28 October 2026):

| Candidate | What is documented | Limits and open questions |
| --- | --- | --- |
| **Charging Limit** (register 60000) | Readable and writable, 80 to 100 %, not tied to the third-party mode. Almost certainly the Time of Use maximum SOC | Cannot go below 80 %. The register profile contains no separate setting for the Dynamic Tariff or Smart mode limit, so a low summer level cannot be reached through it |
| **Third-party control with a specified charging power** (mode register 10064, set-point 10071) | Anker's guide: enable the third-party control switch in the app (this also enables Modbus access); the device then follows commands, which take priority over the app; "Specified Charging Power Adjustment" sets grid-to-battery watts; switching the app switch off returns the device to the mode last set in the app | Any level is possible, but this application would have to stop charging at the target itself (a short loop while charging), restore the previous Anker mode afterwards, and avoid fighting the app. Behaviour if commands stop is undocumented. Turning the app switch off also blocks Modbus, so it is a manual fallback, not a software release |
| **Owner sets the level in the Anker app** (Dynamic Tariff or Smart mode maximum charge) | Not exposed over Modbus in the vendor profile | Keeps the Anker firmware in complete control; this application advises the number and the owner enters it. No automation |

**The vendor's control code, re-read in full on 29 October 2026** (ANKER_INTEGRATION_NOTES.md has the full account, the list of functions we copy into `devices/anker_vendor.py`, and how to take an upstream update) (repository HEAD is still commit 21c9483, the one we pin; the Solarbank 4 E5000 Pro map is byte-identical to our copy). This is everything the vendor's integration can write to this device; nothing else is documented:

| Control | Register and form | What the code shows |
| --- | --- | --- |
| **Operating mode** | 10064, one register (function 06), values 0 self-consumption, 1 Time of Use, 3 third-party control, 4 custom, 5 socket overlay, 6 Smart, 7 dynamic pricing; only the modes the device lists in its mask 0x8006 are offered (this device offers self-consumption, Time of Use, custom, Smart and third-party control, and **not** dynamic pricing or socket overlay) | The vendor's own integration **sets mode 3 (third-party control) once, the first time it connects** (`auto_mode_on_connect: 3`, remembered). Third-party commands only apply in that mode. Writing another mode value is how the vendor offers a return to Time of Use, Smart and so on |
| **Battery power set-point** | 10071, INT32 over two registers (function 16), big-endian, magnitude 0 to 10,000 W, **negative = charge, positive = discharge** (the integration applies the sign from a separate direction choice and rewrites the register when the direction changes) | Marked `never_read_device`: the device is never read for it, so the commanded value cannot be read back; verify by measured battery power (register 10008) and the mode register. The integration refuses a charge or discharge request if the device's own maximum (10036/10038) is below the minimum. It writes the value once: **there is no refresh, keep-alive or expiry anywhere in the vendor code**, so what the device does when commands simply stop is not documented |
| **Charging limit SOC** | 60000, 80 to 100 % | Written only through the integration's checks (must stay above the discharge limit and backup reserve when backup is on) |
| **Discharge limit SOC** | 60001, 0 to 20 % | This device reads 10 %, matching the owner's chosen minimum |
| **Backup reserve SOC** and its switch | 60002 (0 to 100 %, writable only while 60003 = 1), 60003 | The owner does not use it (switch off, confirmed 29 October) |

Not in the vendor map at all: the Time of Use and Smart-mode schedules, any per-mode maximum SOC, and an AC charge-power limit. They cannot be read or written over Modbus (confirmed by switching the app between Smart and Time of Use on 29 October: only register 10064 changed). **The vendor's FAQ lists four app-side protections that outrank third-party commands and make the battery charge at full power: Backup Reserve, SOC Calibration, Fast Charging and Storm Guard.** All four must be off before a controlled trial, or the device may ignore the commanded level. The device is also less exact at low power (a 50 W command can land between 40 and 60 W), which is irrelevant at charging powers near 2.5 kW.

**Owner's answers, 29 October 2026:** Backup Reserve, Fast Charging and Storm Guard are off. **SOC Calibration is on** and can be turned off, but the battery management system needs a full charge to recalibrate: if the battery does not reach 100 % for about a week, this application must replicate that, ideally on cheap electricity, before SOC Calibration is turned off. The controller is to do **all** the grid charging, with the Anker left to discharge to the house all day and with no scheduled grid charge in the mode it is left in. The app was switched to Time of Use on the evening of 29 October for a night's charge to 100 %.

**Owner's decisions after reviewing the control specification (30 October 2026):** after each charge the Anker is handed back to **mode 0 (self-consumption)**, not the dynamic mode, because this application handles tariff-aware charging itself; the Smart Meter's branch protection and third-party PV monitoring are **off** in the app; there is **no EV yet**; if the controller or the Pi fails mid-charge the owner will **prove the recovery in trials first** (no power backup for the Pi for now); the controller is to be **extended from the existing controller logic**, not replaced; and the **minimum battery level setting is limited to 5 to 20 %** (the device's own discharge-limit register accepts 0 to 20 %). **Superseded in part (owner, 2 November 2026):** a car now exists; when the controller stops or faults it writes the default mode, Dynamic Tariff (value 6 on this device), instead of mode 0, because the Anker app's own settings can only be changed by hand; and the firmware owns the minimum level (`POST_SHADOW_PLAN.md` D13, D17, LC1).

**Charging sequence in each cheap-rate window (owner, 30 October 2026):** charge at full power to the level the planner chose; when that level is reached, hold the battery idle (third-party control at 0 W) until the cheap window ends; at the end of the window hand the battery back to self-consumption (mode 0) until the next window. The charge is **not** spread over the window at a low power: the owner's reasoning is that idling uses less energy than running the inverter at low power for hours.

*What this means for the choice above:* third-party control is the only documented way to set an arbitrary level, and it replaces the Anker's own behaviour while it is active. To keep the Anker's own house matching and idle-when-full, the controller would use mode 3 only for the charge (a negative set-point, stopping at the target by watching the battery level) and then write the previous Anker mode back. Two things must be shown on the real device, in a trial with the owner present, before this can run unattended: what the device does if the controller stops mid-charge (the vendor code gives no expiry), and that writing the previous mode back is prompt and complete.

Commissioning must establish which mechanism realises the evening's chosen level, and show that it covers the whole range in summer, before automatic operation is enabled. Until then the application publishes the recommended level as shadow advice, and the planned sequence is: advice only for several weeks, then a controlled trial of the chosen mechanism.

## 11. Router reset, power cut and IP discovery

Provide automatic rediscovery at boot and after repeated connection failures, plus **Find devices** and manual-address controls in the UI.

- Persist identities, not just IPs: device serial/model and available unique Shelly/Anker identifiers, with known MAC/mDNS names where available.
- Try the previous endpoint and saved hostname first. Use mDNS/DNS-SD and vendor-supported discovery where verified. Anker mDNS support must be checked, not assumed.
- If necessary, use a bounded, rate-limited **read-only** search on the configured local LAN for expected Modbus/HTTP services. Do not scan outside the configured network or change router settings.
- A router reset can change the whole subnet. Re-evaluate the Pi's active LAN interface and route instead of searching only the old `192.168.1.x` range. Distinguish the intended LAN from VPN/other interfaces; require selection if the local-network scope is ambiguous.
- Verify the intended device identity at a candidate endpoint before changing its binding. An open Modbus port or a plausible SOC value is insufficient proof.
- Do not auto-select an ambiguous replacement or issue configuration writes as a discovery side effect. Keep writes disabled until the configured device is positively identified.
- Persist verified new endpoints atomically, record old/new IP and discovery time, collect fresh state and replan before resuming.
- Distinguish loss of internet weather/tariff access from loss of local device access; use suitably valid cached inputs and the explicit Octopus tariff/window defaults in section 7.2. Tariff fallback does not supply missing weather or device measurements.

Show `Searching`, `Found and verified`, `Multiple matches`, or `Not found`. Use a local hostname for the Pi dashboard where available, and relative links rather than embedding a fixed Pi IP. Recommend DHCP reservations as an optional convenience, not a dependency.

## 12. Technical integration starting points

These are **dated existing configuration values**, not verified current endpoints. Rediscover and verify at commissioning.

| Device/source | Existing address or field | Notes |
| --- | --- | --- |
| Pi | SSH alias `solarbank-pi`; `solarbank-pi.local`; old LAN address 192.168.1.155 | Existing monitor uses port 8080; choose a separate configurable free port for the new UI |
| Solarbank | 192.168.1.101:502 | Existing local monitoring; verify product and firmware before any control |
| Smart Meter Gen 2 | 192.168.1.20:502, device ID 1 | Existing reader uses FC04 input registers |
| Shelly Pro EM-50 | 192.168.1.103, `/rpc/Shelly.GetStatus` | `em1:0` Solarbank; `em1:1` charger; use required authentication |
| Garage/Sub active power | 10675–10676 | INT32, high word first, W |
| Garage/Sub phase currents | 10666–10668 | Documented INT16 divided by 100 A; verify active phase and current semantics |
| Phase voltages | 10632–10634 | UINT16 divided by 10 V |
| Main total active power | 10644–10645 | INT32, high word first, W |
| Solarbank power registers | PV 10002/10004; battery 10008; household 10010; grid 10012 | Existing decoder uses signed multiword values; confirm actual map and boundaries |
| SOC | 10014 | Verify profile encoding/scaling |
| Battery energy counters | 10262–10263 charge; 10264–10265 discharge | UINT32 divided by 10 kWh; 0.1 kWh resolution |

Addresses above are taken from the existing documented reader without +/-1 adjustment. The existing meter map has no cumulative household-energy register or device-update timestamp. In the current website, the normal meter poller reads power rather than all phase currents: **current acquisition is an explicit new requirement**.

The official profile can change operating mode on connect through register 10064. Discovery and observation must not trigger that behaviour. Do not provide write addresses or assume that writing zero power relinquishes control until validated against the exact product profile and firmware.

## 13. Libraries and deployment

| Purpose | Recommendation/reference |
| --- | --- |
| Economic planning | [EMHASS](https://emhass.readthedocs.io/en/latest/main_core_concepts.html), with [externally supplied forecasts](https://emhass.readthedocs.io/en/latest/forecasts.html) |
| LP/MIP solver | [HiGHS](https://highs.dev/) through the tested EMHASS interface |
| Solar modelling | [pvlib](https://pvlib-python.readthedocs.io/en/stable/) |
| Weather | [Open-Meteo API](https://open-meteo.com/en/docs) |
| Local Modbus | [PyModbus](https://pymodbus.readthedocs.io/en/stable/) |
| Anker definitions | [Official Anker SOLIX integration](https://github.com/anker-charging/ha-anker-solix-official); review licence and pin a reviewed profile |
| Shelly measurements | [Shelly EM1 RPC documentation](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EM1/) |
| Discovery | [python-zeroconf](https://python-zeroconf.readthedocs.io/en/latest/), where the device advertises supported services |
| Tariff/slot/import data | Optional [Octopus API](https://developer.octopus.energy/guides/rest/api-endpoints/), a verified account-specific smart-dispatch interface, configured fallback tariff/window settings and supplied cost exports |
| UI/API | [FastAPI](https://fastapi.tiangolo.com/) with [Jinja2 HTML templates](https://fastapi.tiangolo.com/advanced/templates/), CSS and small JavaScript updates; run with [Uvicorn](https://fastapi.tiangolo.com/deployment/manually/) under systemd |
| Responsive control | Standard-library [asyncio](https://docs.python.org/3/library/asyncio.html) for bounded non-blocking device I/O and [time.monotonic](https://docs.python.org/3/library/time.html#time.monotonic) for elapsed-time deadlines in the separate controller process |
| Control state machines | [transitions](https://github.com/pytransitions/transitions) (pytransitions), pinned (tested 0.9.3, MIT); the controller's one approved third-party library apart from the device readers |
| Telemetry store | [Whisper](https://github.com/graphite-project/whisper) (the Graphite project's fixed-size round-robin database; tested 1.1.10, Apache 2.0), pinned; written only by the collector |
| Processing/storage | pandas/NumPy as needed; standard-library SQLite and zoneinfo for settings, plans, sessions, half-hour and daily records and events |
| Later learning | scikit-learn only if its tested benefit justifies the extra model |

The existing project used EMHASS 0.18.5 and PyModbus 3.11.3. These are reproducibility starting points, not a promise that newer versions or private integration APIs are compatible. Pin and record tested dependencies; verify the Pi's actual Python version/architecture. Do not upgrade existing environments as part of a new installation.

Suggested implementation layout, when authorised:

```text
src/solarbank_optimiser/
  adapters/       # read-only discovery, Anker, meter, Shelly, Octopus
  observations/  # typed measurements, validation, retention
  forecasts/     # demand, PV, scenario errors
  planning/      # EMHASS adapter, executable replay, monetary objective
  control/       # supervisor, limits, state machines (transitions helper, signals, command/readback)
  telemetry/     # fixed-size Whisper store; the collector is the only writer
  learning/      # chronological evaluation and model promotion
  web/           # status, override and configuration
tests/           # pure model, replay and fault-injection tests
```

Use a dedicated virtual environment, unprivileged systemd services, persistent state under a new `/var/lib/solarbank-cost-optimiser` directory, rotated logs and a tested backup/restore procedure. Bound solver CPU/time so it cannot starve the limiter. Store credentials in protected environment/config files, never source control, exports or browser JavaScript. Protect state-changing UI endpoints with authentication and CSRF/origin checks; do not expose equipment commands publicly.

Use FastAPI for presentation and settings, with the control supervisor and planner in their separate processes. Do not start a hardware poller or command writer for each web worker. Use production Uvicorn configuration without development auto-reload. Browser requests must remain responsive while a forecast or optimisation is running.

The proposed one-second loop is supervisory, not hard real-time: standard Python on the Pi's general-purpose OS does not guarantee a maximum response time. Use bounded I/O timeouts, monotonic elapsed-time measurement and recorded loop/measurement/command latency; detect missed deadlines and apply the documented restricted state. Keep blocking work outside the control loop. Verify end-to-end device refresh and response under realistic Pi load; a real-time library cannot make stale telemetry fresh or replace the 50 A circuit protection.

A proposed local API needs only: status, current plan, history summary including car-charging sessions/totals/CSV, data quality, household-demand settings and dates, override/pause/resume requests and device discovery status. Expose no raw arbitrary-register write endpoint. Validate configuration before atomic replacement and log changes.

### 13.1 Automated Raspberry Pi deployment and startup

The application must run unattended on the existing Raspberry Pi without an open laptop or SSH session. Provide a one-command initial installation/redeployment tool and a configured release workflow that automatically deploys a designated tested release over authenticated SSH. Document the selected release trigger and target Pi during setup; do not silently track arbitrary latest code or dependencies. This section specifies future delivery behaviour only; no installation or deployment is performed by this handoff.

- **Preflight:** verify the intended host, OS/CPU architecture, Python/dependency compatibility, free storage/memory, active services and available port. Keep the existing Debian installation, energy monitor, Home Assistant and their data in place. Use a separate application directory, virtual environment, state database and service names.
- **Install/update:** automatically transfer the versioned release, install pinned compatible dependencies, validate configuration, take a consistent database/configuration backup and apply tested migrations. Keep credentials outside source, build artifacts and deployment logs. Re-running the same deployment must be safe and must retain household-demand settings, their dates, history and local configuration.
- **Control handover:** serialise deployments and stop the previous writer using the commissioned fallback before switching releases. Retain a single device connection owner/writer. First installation starts in observe/shadow with equipment writes disabled. Updates preserve pause, shadow/automatic state and manual overrides, respecting expiry; they must not silently enable automatic control. Re-read actual device identity/state and active constraints before resuming control, and require a fresh valid plan before resumed economic charging/discharging commands. In a commissioned installation, verified stop, inhibition and fallback actions must not wait for a weather request or planner result; observe/shadow remains without equipment writes.
- **Automatic startup/recovery:** install and enable the controller, planner and FastAPI web services under systemd, with bounded restart-on-failure behaviour. Start after boot and recover from a power cut without user login. Keep persistent settings/history outside replaceable release directories, and show clock, device or network readiness problems explicitly.
- **Verification:** check the deployed version, systemd active/enabled state, local HTTP health, the browser dashboard/settings, telemetry freshness and the intended operating mode. Distinguish a running website from ready battery control. Verify startup after a controlled commissioning reboot and verify the existing monitor remains available.
- **Failure/rollback:** stage releases before switching, retain the previous compatible version and automatically roll back a failed update when safe. Preserve newly accumulated observations and settings; never blindly replace the live database with an old snapshot. If migration compatibility or equipment state cannot be verified, keep control in its documented restricted state and show the recovery action. Report deployment success only after the required checks pass.

## 14. Storage, evidence and interface

Store typed observations and quality; half-hour energy; charger sessions, daily charging totals, coverage and threshold/confirmation parameters; effective-dated prices and billing records; fallback tariff/window versions and per-interval price provenance; API/cache validation and recovery status without secrets; immutable Intelligent dispatch revisions and household-rate eligibility evidence; demand selections, planned start/end times and parameter versions; immutable forecast issuances; scenarios/model versions; plans; command acknowledgements; overrides; constraints/events; daily outcomes; model evaluations; and verified device identities/endpoints.

**Telemetry logger (revised approach, 2 November 2026).** High-rate measurement history (per-second readings, one-minute observations and charger coverage) is stored in a fixed-size Whisper store: one file per metric in `/var/lib/solarbank-cost-optimiser/telemetry/`, with 1-second resolution for 2 days, 1-minute for 90 days and 30-minute (aligned with Octopus settlement periods) for 10 years. Each file is about 5.7 MB and never grows; about 15 metrics, with a count file for each whose coverage matters, come to roughly 125 MB. The collector is the only writer: it buffers readings in memory and writes them in batches outside the control loop, and the controller never touches the store. Missing readings stay empty and are never stored as zero (an interval needs at least half of its readings, `xFilesFactor` 0.5), and the count of valid seconds kept beside each such metric means a partly measured minute is never taken for a complete one (`POST_SHADOW_PLAN.md`, TC1). A late reading corrects the coarser averages while it is within the one-second archive; the collector refuses older points, and a day is finalised within 22 hours of its end (`POST_SHADOW_PLAN.md`, TC5). SQLite keeps settings, plans and their archive, charger sessions, half-hour and daily records, events and everything else irregular or transactional. Until the migration (`POST_SHADOW_PLAN.md`, WP23a to WP23c) is complete the minute rows stay in SQLite with their 7-day purge; afterwards minute detail is kept for 90 days in the store. Real-time control still uses live readings, never the logger or the database.

Keep high-frequency raw data for a configurable bounded period and retain minute/half-hour summaries, daily outcomes and decision evidence long term. Preserve enough inputs to reproduce every recommendation. Choose retention based on Pi storage; do not copy the current 24-hour diagnostic retention blindly into seasonal learning.

Use transactional writes and schema migrations, a bounded queue and a suitable SQLite journal configuration for concurrent readers. Use SQLite's consistent backup/export facilities rather than copying a live database without its journal. Document the maximum buffered observation loss after power failure. Use a monotonic clock for live timeouts/leases and UTC for persisted events; inhibit time-dependent execution until the Pi's clock is trustworthy after boot. Telemetry files are copied for backups under the Whisper file lock; a point written with an earlier time than a previous one overwrites it instead of failing.

One main light-theme, mobile-friendly dashboard should show mode, SOC, the current/next **grid** target, solar ceiling, current garage amps, EV inhibition, forecast demand/PV, expected cost and the reason for the decision. Include the Car charging (Shelly) card, today's measured charging kWh and a link to charging history. Show the active household rate, whether it comes from the night window or an extra smart slot, its verified end and data freshness. Use a clear reason such as `Charging battery: extra Octopus cheap slot`, or explain a target/current-limit restriction. Show one combined plan/actual chart. Put settings, charging history, data quality and detailed diagnostics in one secondary view, with consistent navigation.

Provide the Household demand selector, its active duration and any planned change alongside Pause automation, Override grid target until…, Return to automatic and Find devices. Keep the demand selection distinct from the automation operating state: for example, automatic control can be active while Away from home is selected. Acknowledge when a requested setting is overridden by a constraint. Show limited/unknown capability explicitly, not a misleading green success state.

Report actual Octopus cost separately from estimated/modelled cost. Track **savings against a home with neither solar nor battery** by week, with an optional cumulative total from a stated start date: the counterfactual cost is the measured household consumption priced at the tariff applicable to each interval, compared with the actual import cost, and both are labelled as estimates unless backed by billing data. Also report the incremental simulated benefit versus full overnight charging and recorded Anker operation. Match comparison periods and ending inventory. Do not claim actual causal savings solely from a replay. Exclude partial days from daily scores and label unknowns. Keep annual/seasonal projections provisional until supported.

## 15. Historical evidence included

See `history/README.md` (not in this public copy) and `history/manifest.json`. The handoff includes existing household minute profiles, operating-flow snapshots, Octopus exported costs, archived forecast evidence and the earlier forecasting/measurement findings.

History is a **seed dataset**, not a live source. Refresh it read-only at end-of-October implementation. Retain pre-10 October records as billing/context only; do not treat them as the fully commissioned PV configuration. Preserve 17 October export-enabled observations rather than rewriting them to fit today's export-disabled policy.

Do not use the old purchase model, Anker earnings, retrospective forecasts, a fitted clear-sky envelope or a historical maximum composite as independent evidence of prospective savings or tomorrow's generation.

Existing useful source modules, in the parent project, are `target_advisor.py`, `independent_forecast.py`, `independent_forecast_store.py`, `load_forecast_fit.py`, `observation_store.py`, `energy_reconciliation.py`, `emhass_advice.py` and `emhass_advice_status.py`. Reuse reviewed pure logic and tests selectively; do not import the whole website. In particular, the current native EMHASS overnight experiment is not a full controller using actual starting SOC. Do not inherit the existing trial's 21 October stop date into the permanent app.

## 16. Required verification and delivery stages

1. **Offline:** import history, verify units/coverage, replay physics and expected-cost decisions; no equipment writes.
2. **Observe/shadow:** verify identity, refresh rates and live boundaries; archive plans without applying them.
3. **Commission:** explicitly validate every control operation, operating priority and recovery behaviour on the installed equipment before unattended control.
4. **Automatic:** enable only supported tested controls, retain override and review daily outcomes; extend capabilities incrementally.

Acceptance tests must demonstrate:

- An 85% grid target permits 100% solar SOC; alternative targets remain available.
- The 5% reserve, inverter limits and grid-charge reachability hold continuously.
- Expected monetary value can choose extra cheap charging despite forecast curtailment when peak-shortfall risk makes it cheaper; it can also choose a lower target when justified.
- A qualifying daytime/evening Intelligent slot applies the cheap household rate and automatically charges the battery towards its updated grid target while the car charges; pause/override, actual SOC, available solar and current limits remain effective.
- Slot revisions, cancellation, expiry, lost eligibility and stale data cannot extend charging at an assumed cheap price; manual/bump charging alone does not authorise it. Test half-hour eligibility when EV charging ends early, exact boundaries including DST, and continued use of the independent night window.
- Simultaneous car/battery charging above 45 A reduces/stops Anker charging while battery discharge remains inhibited, including if the extra slot is cancelled.
- With the Octopus option disabled or a required key absent, all three household-demand settings still produce valid default-window plans from configured day/cheap rates; no extra slots are invented. Keyless published tariff access, when configured, remains independent of authenticated dispatch availability.
- The Octopus connection test distinguishes public tariff access from authenticated device/dispatch access, handles token expiry and a schedule not yet issued after plug-in, and uses the current `flexPlannedDispatches` query rather than depending on the deprecated `plannedDispatches` query. Planned kWh is never substituted for Shelly-measured charging history.
- API timeout, authentication/rate-limit/server failure, malformed/error-bearing or wrong-account data, non-finite prices, stale cache, gaps and conflicting intervals fall back visibly for affected inputs without blocking the controller. A valid empty dispatch list removes obsolete future slots; known valid billing intervals retain only their verified end, including with a fallback cheap price.
- Default-window midnight/DST boundaries, validity/expiry, missing defaults, preserved settings across restart, bounded retries and validated API recovery are tested. Missing prices never become zero; default-based costs remain estimated and API keys never appear in responses/logs/exports.
- Away from home plans for measured essential base load, Normal consumption uses the normal-use forecast, and High demand adds the configured family-use allowance. With other inputs held fixed and a positive allowance, High demand predicts more household energy than Normal; all three show their forecast and target without guaranteeing a different SOC when solar or battery constraints dominate.
- Demand selections and scheduled start/end times trigger replanning of night and extra-slot targets, survive restart and return to Normal at an explicit expiry, including an expiry while the Pi was off. Planned returns/visits affect the relevant future intervals and use correct UTC/London/DST boundaries.
- Away/High demand observations do not contaminate the normal-use profile; historical evaluation cannot use later selections. Pause, grid-target overrides, EV inhibition and the 45 A limit retain their priorities in every demand setting.
- No future observations enter a historical forecast, calibration or first action.
- Recent-complete-day selection, gaps, UTC/London alignment and DST are handled without silent zero-filling.
- Curtailed solar is not mistaken for available solar; counters, signs and conversion losses are not double-counted.
- Charger detection inhibits battery discharge at any rate while allowing direct PV, including simultaneous garage overcurrent.
- **(Amended 2 November 2026, D7/WP26: the acceptance boundaries are now 299, 300, 999 and 1,000 W against the 300 W and 1,000 W thresholds, as in `tests/test_charger.py`.)** For an idle/confirmed-standby charger, dedicated Shelly readings of 0 W and 9.9 W produce standby with no charging sessions/kWh; exactly 10 W and 10.1 W qualify for detection after confirmation. After active charging, the display/inhibition honours the release delay without counting sub-10 W energy/time. Threshold flicker does not fragment sessions, and standby-only periods do not trigger EV inhibition.
- Car status/history use the dedicated charger channel, retain raw/whole-house standby consumption and show charger-circuit energy. Stale/invalid readings show Unknown; no vehicle battery percentage, plugged-in or fully-charged claim is inferred from power.
- Session/daily totals and CSV agree within documented integration accuracy, preserve coverage, handle midnight/DST and restart without double-counting, and never bridge outages or present mixed standby counter deltas as filtered charging energy.
- Above 45 A, AC battery charge is reduced/stopped and confirmed; gradual recovery does not oscillate.
- Other loads alone above 45 A produce an alert rather than a false claim of control.
- Failed/stale CT or charger readings and unacknowledged commands produce the documented restricted/unknown state.
- Manual requests cannot override constraints; stale plans cannot clear an inhibition.
- Router restart/IP reassignment, including a changed LAN subnet, rediscovers the same device without writes during discovery; ambiguous identities remain blocked.
- Power loss, solver hang, internet loss, Pi restart and competing-controller attempts have tested outcomes.
- The configured release trigger performs an automated Pi deployment; repeated installation/update preserves data/settings, and interrupted deployment or failed health checks produce a verified compatible rollback or an explicit restricted state.
- Systemd services are active and enabled, the FastAPI dashboard/settings work, and a controlled commissioning reboot restores the intended operating/demand settings without login or stale equipment commands. Existing Pi services remain available.
- A slow/stalled planner or browser request cannot stall the limiter; recorded loop/device/command timing under Pi load meets the commissioned response criteria, and missed deadlines are visible.
- A local browser user can understand and override the plan without editing code.
- The new installation preserves existing monitor history and supports rollback.
- The controller's state machines accept only declared transitions: an unknown event name or an event not allowed in the current state is rejected, logged and counted without changing state or stopping the loop; every state change appears in the change log with its trigger; `decide()` matches the decision table for all 16 input combinations; and in a cheap period no sequence of plan changes, expiries, withdrawals, reached targets, restarts or data gaps allows battery discharge.
- The telemetry store keeps a constant size after a simulated year of one-second writes, leaves missing intervals empty at every resolution, corrects a minute average when the rest of the minute arrives in a later batch, and agrees with the half-hour records over a two-week parallel run before the SQLite minute writes stop.

**Success criterion:** reliable automatic operation with low user effort and a defensible reduction in imported-electricity cost, judged weekly against a home with neither solar nor battery and, as a secondary check, against always charging fully, using comparable periods and executable behaviour rather than theoretical optimisation alone.
