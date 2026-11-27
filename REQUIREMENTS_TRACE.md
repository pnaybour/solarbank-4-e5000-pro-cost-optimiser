# Code against the owner's control specification v1.3 (8 November 2026)

First pass, made by reading the specification (`reference/owner-research/SOLARBANK_CONTROL_SPEC-v1.3-2026-10-30.md`) beside the code and its tests. No earlier copy of a
requirements-against-code check could be found in the repository, its git history, the session notes or the owner's Drive, so this one starts fresh. It covers the
parameters (section 4), the decision table (7), garage regulation (9), mode management (10), fault handling (11), prerequisites (12) and the implementation notes (15).
It does NOT yet cover `SOFTWARE_SPECIFICATION.md` item by item (the dashboard, history, planner and records requirements): that is the next pass.

## Matches (value or behaviour found in code, and a test)

| Specification | Code |
|---|---|
| P_MAX_W 2500, P_MIN_W 100, GARAGE_LIMIT_A 45, AVG_WINDOW_S 3, voltage 207–253 V | `garage.py`, `control_logic.py` |
| SOC_HYST_PCT 3, EV 1,000 W / 3 s on, 300 W / 120 s off | `signals.py` (D7) |
| SETPOINT_DEADBAND_W 50, RAMP_UP_MIN_INTERVAL_S 5, MODE_SETTLE_S 2, MODE_CHECK_S 60, MODE_REVERT_LIMIT 3 per hour, ALIGN_S 1, VERIFY_S 20 | `garage.py`, `action_machine.py` |
| OCTOPUS_START_FRESH_S 300, OCTOPUS_STALE_S 1800, SMART_CAP 6 h (12 half-hours), OFFPEAK 23:30–05:30 | `signals.py` (issue 34) |
| Decision table, all 16 combinations, written from the specification not the function | `tests/test_signals.py` |
| Write order: set-point 0 W, mode, settle, set-point; mode written only on change; only registers 10064 and 10071 can be written | `action_machine.py`, `modbus_write.py`, `tests/test_live_transport.py` |
| Garage regulation only while the action is CHARGE; decreases at once, increases limited | `garage.py`, and `live_transport.py` after GitHub issue 62 |
| Prerequisites: Backup Reserve and the mode mask are read and checked before any write | `solarbank.py` reader, `live_transport.py` (issue 51) |
| Startup checks, shutdown hand-back (0 W then the default mode), HANDS_OFF latch until a person resets it | `action_machine.py`, `live_transport.py` |

## Differences from the specification, all deliberate and recorded

| Specification | Code | Where decided |
|---|---|---|
| DECISION_S 10 s | the table is evaluated every second, which includes every change at once | `OPERATIONS.md`, review of 7 November |
| STALE_S 10 s | now a setting: 2.5 s while charging, 10 s otherwise | `REQUIREMENTS_AMENDMENTS.md`, 8 November (owner, option C) |
| Invalid data: HOLD, hand back after HOLD_TIMEOUT_S (900 s) | reset to the default mode (Dynamic Tariff, value 6) | D9, D17 (owner, 2–3 November) |
| RUN_MODE dry_run / commissioning / production | shadow / logged / live, chosen by three root-owned files | RC1, G1 to G3 |
| Minimum level adjustable | the firmware owns the floor (register 60001); the controller clamps to it | D13 |
| OCTOPUS_POLL_S 120 s | the providers service asks for the dispatches every 60 s: two requests a minute, inside Octopus's limit of five, and fresher for the 300 s start rule | GitHub issue 72, 10 November |
| 11.4 secondary check: CHARGE "charging", ZERO_OUTPUT "not discharging above 100 W" | both read as "not discharging above 100 W" on register 10008: a tapering or full battery may show no DC charging at all, so requiring it would fail good commands; a battery plainly discharging still fails | GitHub issue 72, 10 November |

## Gaps: in the specification, not in the code, and not recorded as a decision

1. **Reconciliation validity check (sections 5.2 and 11.2).** VALID is FALSE when `P_unmetered = P_garage − P_EV − P_batt_ac` stays below −230 W for 30 s
   (RECON_PERSIST_S), using only readings within 1 s of each other. Nothing in the code computes `P_unmetered` or applies this test. It guards against a wrong or reversed
   current-transformer channel making the garage regulation believe there is more headroom than there is. **Needs a decision: build it before commissioning, or waive it with a reason.**
2. **Register 10080, the communication watchdog (section 14, item 4).** The specification says to read it and not write it. The code does not read it. Informational, but it
   is how a timeout fallback would show.

## Open items the specification itself lists (section 14), still to be settled at commissioning

Sign of register 10071 (T2), PV behaviour in mode 3 at 0 W (T3a/T3b), whether the unit rests at 0 W (T4), loss-of-communication behaviour (T6), the sign of the PV/ESS
current-transformer reading (T9), the SmartFlex device id, the Modbus unit ids.

# Peer review of the control path against the requirements (9 November 2026, commit 8991ee59)

Scope: every section of the owner's control specification v1.3 read beside `signals.py`, `garage.py`, `control_logic.py` (the Supervisor), `action_machine.py`, `live_transport.py`, `devices/modbus_write.py` and the controller's reader in `devices/solarbank.py`, with the owner's decisions D3 to D19; the acceptance list of `SOFTWARE_SPECIFICATION.md` section 16 only where it touches that path. **Not reviewed:** the planner's economics and forecasting (specification sections 5 and 6), the car history (8.1), storage and the dashboard (14). Findings marked *(run)* were reproduced by driving the real modules against the test fakes, not only read. Nothing here affects shadow or logged mode: every finding is about what would be SENT in live mode. This review supersedes the "Matches" table above where they disagree.

## What matches

The decision table and all 16 cases; the EV thresholds and dwell times; BELOW_TARGET with 3 % hysteresis; the off-peak window on the London clock; the garage formula, its write rules and the over-current alert; the write order (0 W, mode, settle, set-point) and the pass through 0 W; DISCHARGE as mode 0 with no set-point; CHARGE at 0 W staying in mode 3; the writer's two registers with value guards and one attempt; the start-up checks (mask bits 0 and 5, Backup Reserve off) and their re-check while running; the HANDS_OFF latch and its reset; the release on exit; mode supervision with the firmware-protection exception and the three-an-hour limit.

## Findings to fix before any live write

1. **A write the device refuses is retried every second for ever, with no fault and no alert** *(run: 59 failed mode writes and 59 repeated 0 W writes in 60 s; the machine stays `running`; no alert)*. Specification 11.2 makes two failed writes in succession invalid data (a fault), and 9 and 10 limit writes. `LiveTransport._perform` calls `machine.forget()` on a failure, so the next tick plans and sends the same sequence again. Needs: a count of consecutive failed writes, the fault path after two, a back-off, and an alert.
2. **The controller still enforces the minimum level itself: at or below the Settings level (10 %) it inhibits discharge, which the transport sends as mode 3 at 0 W, and at 11 % it allows again** *(run)*. Specification section 7 leaves the floor "entirely to the firmware", and D13 (2 November) decided the same: the controller only reads the device's limit and clamps the target. As built, every evening the battery reaches its floor the controller would write mode 3, hold it there through the next day unless PV charges it in mode 3 at 0 W (unproven: test T3a), and flip between mode 3 and mode 0 around the 10/11 % boundary. `control_logic.Supervisor.decide`, the `soc <= floor` branch.
3. **The software gate does not list the specification's mandatory tests.** `live_gate.REQUIRED_TESTS` has eight entries (register read-back, mode write, 0 W write, both signs, release, default mode, latch, supervised run). The specification's mandatory T3a, T3b, T5, T6, T8, T9, T10, T11 and T12 are not in it, so a commissioning record could open live control with the garage regulation (T8), the garage current transformer's sign (T9) and its accuracy (T11) never recorded. Section 13 and D16 ("nothing is waived") require all of them; POST_SHADOW_PLAN RC2 says so in words only.
4. **The taper near a full battery is treated as a failed command** *(run: commanded 2,500 W, 1,500 W measured: the set-point alternates −2,500 W and 0 W, 189 s of 600 in HOLD, repeating)*. Specification 11.4 relaxes the magnitude check when the level is within 3 % of the charging limit or the device's live limit is below the set-point. `ActionMachine.tick` has a `relax_magnitude` argument that nothing passes, and `LiveTransport.effect` applies the strict tolerance. The weekly calibration charge to 100 % would hit this every time.

## Findings that fail in the safe direction, or need a decision

5. **A smart slot never reaches the controller.** `Supervisor.decide` reads dispatches from `inputs["tariff"]`, and nothing supplies that input (the planner prices the slots; the controller is never told). CHEAP is therefore the clock window only, so in a real slot outside 23:30 to 05:30 with the car charging the controller holds the battery (rule 3) and never charges it. Specification 6.2, test T10 and the acceptance item "a qualifying daytime/evening Intelligent slot ... automatically charges the battery". The 05:00 to 08:00 slot of 9 November is the first live example.
6. **A Shelly reading missing for a few seconds during a charge releases the battery** *(run: 0 W and the default mode at once, mode 3 and the set-point again 34 s later)*. `LiveTransport._valid` requires the Shelly; D15 meant a short Shelly miss, which happens every 30 to 60 minutes, to change nothing, and D9's release was for data other than the charger. The Shelly carries both the car and the Solarbank's AC reading, so the two decisions meet here and need the owner's ruling.
7. **Voltage.** The garage calculation takes the voltage from the Shelly, not the smart meter's primary voltage (5.1), and a voltage outside 207 to 253 V does not make the data invalid (11.2): it only blocks increases and uses 230 V.
8. **The reconciliation check (5.2, 11.2) and register 10080 (14.4) are not implemented** (already recorded above).
9. **The device's own limit registers do not reach the controller.** The reader reads 60000 to 60003 but publishes only the Backup Reserve switch, so `clamp_target` clamps to 0 to 100 %: the second half of D13 is not in force (the Settings page shows the limit from the collector's log, but the controller does not use it).
10. **Two connections to the Solarbank from the controller** (its reader and the writer's own client), plus the collector's. Specification 10.5 asks for one connection and one lock per device; section 2 says only one client may talk to each Anker device. Observed so far: the Solarbank serves a few clients. To prove at commissioning, or the writer must share the reader's connection.

## Smaller points

11. The secondary verification on the battery's DC power (direction), and the "possible sign inversion" and "possible firmware override" records of 11.4, are absent; a mode reversion records the drawn power but not the battery status and DC power (10.4).
12. The smart-slot allowance counts any half-hour in which the car charges, bump charges included (the specification counts smart charging), and is kept in memory only, so a restart forgets the day's use.
13. Stale notes in the code: `signals.py` says its signals are "NOT yet wired into `control_logic.Supervisor`" (they are); `modbus_write.py` says it is "not imported by anything that runs today" (the live transport imports it).

## GitHub issues for these findings (logged 9 November)

| Finding | Issue | Label |
|---|---|---|
| 1 Refused write retried every second | #63 | go-live blocker |
| 2 Controller enforces the minimum level itself | #64 | go-live blocker |
| 3 Software gate lacks the mandatory tests | #65 | go-live blocker |
| 4 Taper treated as a failed command | #66 | go-live blocker |
| 5 Smart slots never reach the controller | #67 | owner decision |
| 6 Shelly gap during a charge releases the battery | #68 | owner decision |
| 7 and 8 Voltage and reconciliation validity checks | #69 | owner decision |
| 9 Device limit registers do not reach the controller | #70 | |
| 10 Two controller connections to the Solarbank | #71 | owner decision |
| 11 to 13 and register 10080: smaller points | #72 | |

# Notes of explanation, for future reference (9 November 2026)

Written for someone opening this months later: the owner, or a new session with no memory of the review. Each GitHub issue (#63 to #72) carries its own note as a comment; this section holds what they share.

## The words used

* **Mode 0, mode 3, mode 6** are values of the Solarbank's operating-mode register (10064). 0 is self-consumption: the Anker's firmware runs the battery and covers the house load. 3 is third-party control: the battery does what the set-point register says. 6 is the Anker's Dynamic Tariff mode, which the owner chose as the **default mode** (D17): where the battery is put when the controller stops or has bad data.
* **Set-point** is register 10071, in watts, obeyed only in mode 3. Negative charges from the grid, 0 holds the battery (no grid charging, no discharge), positive would discharge. The controller never writes a positive value.
* **CHARGE, ZERO_OUTPUT, DISCHARGE, FAULT** are the four outputs of the owner's decision table (specification section 7). ZERO_OUTPUT is mode 3 at 0 W. DISCHARGE is mode 0 with no set-point.
* **Release** is "set-point 0 W, then the default mode": what the controller writes on exit, and when its data is invalid (D9). **HOLD** is mode 3 at 0 W after a command failed verification twice. **HANDS_OFF** is the latched stop: one protective 0 W, then no writes until the owner resets it.
* **VALID** (specification 11.2) says whether the readings can be trusted. **CHEAP** is the 23:30 to 05:30 window or a smart slot. **Smart slot** is a period Octopus schedules for the car, billed at the cheap rate for the whole house while the car charges.
* **Shadow, logged, live** are the run modes: decide only; decide and journal the register writes it would make; send. Only live sends. Everything on the Pi so far is shadow or logged.
* **The gate** is `live_gate.py`: live sends only when three root-owned files in `/etc/solarbank-cost-optimiser` agree (`control-mode`, `write-switch`, `commissioning.json`).
* **D-numbers** are the owner's decisions in `POST_SHADOW_PLAN.md` section 7. **T-numbers** are the commissioning tests of specification section 13.

## Why these were not found earlier

* **The paths never run in shadow or logged mode.** Failed writes, command verification, mode supervision, the gate and the taper only exist when something is sent. Three logged nights (6 to 9 November) could not show them; GitHub issue 58 already says a logged night proves the decisions and the nominal write sequence only.
* **Some tests assert the old rule.** `tests/test_min_soc.py` checks that the controller inhibits discharge at the minimum level, which was the 28 October rule; D13 (2 November) changed the rule and the Settings page, but not that branch or its test. A green suite therefore says nothing about finding 2.
* **Some tests stop too early.** `test_a_failed_write_is_journaled_as_failed_and_the_sequence_stops` looks at the first two journal entries of an eight-second run; the retry every second begins with the third.
* **Some parts were built in slices and the last hop was not built.** `signals.SmartSlot` is tested on its own, and the Supervisor reads `inputs["tariff"]`, but no test drives the Supervisor with a dispatch list and nothing in the running system supplies one. `ActionMachine.tick` accepts `relax_magnitude`, tested in the machine, never passed by the transport.
* **The first requirements pass (8 November, top of this file) compared names and constants**, not behaviour. It called several of these areas a match. This review read the code paths and ran four of them.

## How the four runs were made

All four use the fakes in `tests/test_live_transport.py` (`build`, `drive`, `cmd`, `inputs`, `FakeWriter`), run from the `tests` folder with the project's Python. Nothing touches a device.

```python
import test_live_transport as t                        # run inside tests/
# 1 (issue 63): a mode write that always fails, both write classes allowed, 60 s
tr, ex, j = t.build(d, send=True, switch=["mode", "setpoint"], writer=t.FakeWriter(fail="mode"))
t.drive(ex, 60, {1: [t.cmd("ac_charge", "CHARGE_LIMIT", 1, 1.0, watts=1500, target_soc=.9)]})
# count the journal lines by (kind, register): 59 failed on 10064, 59 wrote on 10071; tr.machine.state == "running"

# 2 (issue 64): the Supervisor at midday with no car and a plan with no charge target
from solarbank_optimiser.control_logic import Supervisor
s = Supervisor()                                       # call s.decide(inputs, plan, None, mono, True, utc=...) for 200 s first (the garage start-up rule)
# soc 10 -> Decision.discharge == "INHIBIT" ("at or below the 10% minimum level"); soc 11 -> "ALLOW"

# 4 (issue 66): commanded 2,500 W, the fake meter shows 1,500 W whenever a non-zero set-point is in force, 600 s
#   ex.execute(cmds, t.inputs(1500.0 if tr.machine.setpoint not in (None, 0) else 0.0, mode), t.UTC + i, float(i))
# the set-point writes alternate -2500 and 0; 189 of 600 states are "hold"

# 6 (issue 68): a 1,500 W charge, then the same loop with inputs.pop("shelly") for cycles 30 to 33
# writes: t=30 10071<-0, t=30 10064<-6, t=64 10064<-3, t=66 10071<--1500
```

## What to check after each fix

Each issue names its tests. Beyond the tests, the evidence that closes an issue comes from the Pi: a logged night for anything that changes decisions (issues 64, 67, 68), and the supervised commissioning run for anything that only exists when writes are sent (issues 63, 65, 66, 69, 71). A fix to the control path changes the fingerprint in the commissioning record, so it must land before commissioning, not after.

## One correction to the issues as first written

Issue 69 said the smart meter's voltage "is already read". It is not: `devices/meter.py` reads the currents and powers only, and the only voltage in the system is the Shelly's. Using the meter's voltage as the specification says would mean adding register 10632 to the meter's read.

## Additional finding from the first real taper (10 November 2026)

**14. The "garage limit" reason text is shown even when the device's own charge taper is the real cause, not the 45 A feed.** First seen overnight 9 to 10 November, the first night a calibration charge reached 100% in logged mode: the commanded power fell from 2,500 W to 0 W over six minutes while the garage current itself fell from 10.8 A to 2.8 A (nowhere near the 45 A limit), yet every step's reason read "Garage feed … against the 45 A limit." The real cause was almost certainly the device's own live charge limit (register 10036) tapering near 100%, which the code correctly follows — only the log message is wrong. Logged as issue 73 (in the author's private tracker); not a safety issue, but pollutes the evidence commissioning test T8 depends on.

## Fixes built and locally tested, 10 November 2026 (issues 64, 70, 73)

Per the owner's decisions of 9 November and the instruction to work the open issues while away from the Pi: all three built and tested on `docs/plan-single-store`, not yet deployed (deploys wait for the owner's return and supervision; nothing runs in `live` mode on the Pi regardless).

* **Issue 70** (device's own charging/discharge limit registers reach the controller): `devices/solarbank.py`'s safety-register read now publishes `charging_limit_soc` and `discharge_limit_soc` (60000, 60001) alongside `backup_soc_enable`, not just the mask. `control_logic.Supervisor.decide` clamps the plan's target to them (`clamp_target`), trusting a register value only inside its documented range (80-100, 0-20); outside that range it is treated as unknown and the full range is used, as D13 intends for a misread. `hold_reason` now names the *effective* (clamped) target reached, not the plan's raw one.
* **Issue 64** (the controller inhibited discharge at the minimum level itself): removed. D13 (2 November) already said the firmware alone owns the discharge floor and sleeps the unit there; the controller's own `elif soc <= floor: inhibit = True` branch was the pre-D13 rule, never removed when D13 landed. It is gone now — the controller still reads the stored minimum (and the device's register 60001) to clamp the plan's target and to show a mismatch on Settings, but a low SoC with no charge target now gets `discharge == "ALLOW"` from this controller, same as any other ordinary discharge. Several tests asserted the old INHIBIT-at-floor behaviour (`test_min_soc.py`, `test_control_loop.py`, `test_after_target.py`, `test_ev_missing_data.py`, `test_plan_memory.py`) and were updated to assert ALLOW with no `"reserve"` restriction; one new test (`test_the_controller_never_inhibits_discharge_for_the_minimum_level_itself`) pins this down directly across soc 10, 5, 1, 0.
* **Issue 73** (misleading "garage limit" reason text): `garage.py` gained a pure function, `binding_constraint`, that compares the two ceilings `p_avail` already takes the smaller of — the 45 A feed's headroom and `min(P_MAX_W, p_max_live)` — and reports which one is actually smaller ("garage", "device", or `None` for a tie or an unknown `i_batt`). `GarageRegulator` records this each cycle as `self.bound_by` whenever the set-point is actually capped (never while "starting"). `control_logic.Supervisor._garage` now picks the reason text from `bound_by`: the existing "Garage feed X A against the 45 A limit" wording when the feed's own headroom is smaller, a new "The installation's own charge limit… " wording when the device/installation ceiling is smaller.
  * **Corrected by the second peer review (11 November)**: the first version of this note said the "device" case could not be reached from today's
    wiring and that the logged night's message would not change. That was wrong. `want_w` is `min(max_charge_w, AC_CHARGE_LIMIT_W)` and is NOT
    rounded, while the set-point is rounded down to 10 W; so a device limit such as 2,315 W gives a set-point of 2,310 W, below `want_w`, and that is
    what set the `garage_limit` tag during the taper. The first fix changed only the sentence and kept the tag. Now, when the device's own limit is
    the binding term and the garage feed is not over its limit, no `garage_limit` tag is set and the reason names the battery's own charge limit.
    Test: `test_the_devices_own_taper_is_not_reported_as_a_garage_limit` in `tests/test_control_loop.py`.
* Full suite: 1510 passed, 2 skipped after the fixes above and the test corrections (two other tests, `test_ev_missing_data.py` and `test_plan_memory.py`, also asserted the old floor-inhibit behaviour and needed the same correction as issue 64 — not noticed until a full-suite run, since the targeted test files alone did not catch them).

## Fix built and locally tested, 10 November 2026 (issue 68)

Per the owner's decision A on 9/10 November ("a short Shelly gap (up to 10 s) changes nothing in the transport either"). Built and tested on `docs/plan-single-store`, not yet deployed.

`control_loop.ControlLoop.cycle` tightened the freshness limit to `charge_s` (2.5 s) for **every** device while a charge was under way, not just the garage current reading the 8 November "option C" decision was actually about ("while a charge is under way... the garage current drives the charge power, so the tight limit applies" — the comment named the right reason but the code applied it to the wrong readings). Since `LiveTransport._valid` requires both `solarbank` and `shelly` to be present in `inputs`, and `inputs` is built from exactly this age-filtered set, a Shelly (or Solarbank) reading only 3-4 s old during a charge was already being dropped as unknown, which is what let a routine missed Shelly poll invalidate the whole device and trigger a release (set-point 0 W, then the default mode) — the real-world 00:48:26 event from the overnight log matches this exactly.

Fixed by tightening only the `meter` device (the garage current) while charging; `solarbank` and `shelly` now always use the specification's ordinary `idle_s` (10 s) limit, charging or not — matching D15's "a gap up to 10 s changes nothing" and D9's general staleness rule, without the accidental extra tightening #68 found. No change was needed in `live_transport.py` itself: `LiveTransport._valid` already does the right thing once `inputs` itself is no longer dropping a fresh-enough Shelly reading.

Tests: rewrote `test_the_freshness_limit_is_tight_while_charging_and_the_specifications_ten_seconds_otherwise` (now `..._tight_for_the_meter_while_charging_...`) to use a `meter` device and show the tightening landing there instead of on `solarbank`/`shelly`; added `test_a_short_shelly_gap_during_a_charge_changes_nothing_a_longer_one_still_invalidates` covering the issue's own two cases (a 9 s gap during a charge: still known; 11 s: invalid, same boundary as idle). Both in `tests/test_control_loop.py`. Full suite: 1511 passed, 2 skipped.

Keeping this open until a logged night (or, once live, a supervised run) shows a Shelly gap during a charge no longer producing a release in the real log.

## Fix built and locally tested, 10 November 2026 (issue 69)

Per the owner's decision A, with an email alert, 9/10 November. Built and tested on `docs/plan-single-store`, not yet deployed.

Both of specification 11.2's two unimplemented validity conditions, neither previously in the code:

* **Voltage.** `devices/meter.py` now also reads the smart meter's own primary phase 1 voltage (register 10632, specification 5.1's V), widening its
  one request from 33 to 45 registers (10632-10676, still a single read; the pinned vendor map's two documented ranges touch and cover it). A new pure
  function, `garage.voltage_invalid(volts)`, is TRUE for anything outside 207-253 V, or missing — not the nominal 230 V this module falls back to for
  the Shelly's own voltage elsewhere; that fallback is deliberately left alone (it is the existing, narrower, "don't subtract the battery" heuristic,
  not this check).
* **Reconciliation.** A new pure state machine, `garage.Reconciliation` (ok -> low -> invalid, mirroring the existing over-current alert machine),
  computes P_unmetered = P_garage - P_EV - P_batt_ac (specification 5.2, using the meter's "sub" reading, the Shelly's car channel, and the Shelly's
  Solarbank channel negated into the specification's own sign) and goes invalid once it has stayed below -230 W for RECON_PERSIST_S (30 s). Readings
  not within ALIGN_S of each other, or missing, are simply not used that cycle (the state holds, same conservative choice `GarageRegulator` already
  makes for I_batt); a short run of unusable cycles does not reset the 30 s dwell timer, only extend how long it takes to accumulate.

`control_logic.Supervisor._garage` treats either one, once the garage current itself is known, exactly like a missing garage reading: it tags it
(`meter_voltage_invalid` or `reconciliation_invalid`), stops AC charging and resets the regulator — which means, as a deliberate and correct side
effect, that specification 11.1's "return to normal once VALID has been TRUE for 30 s" applies here too (the same 30 s start-up hold a fresh
commissioning goes through). Neither check is evaluated, or tagged, on a cycle where there is no meter reading at all (`current is None`):
"garage_unknown" already says charging has stopped, and there is no meter voltage or P_garage of theirs left to judge.

**The email alert.** The Supervisor stays pure (no I/O, per its own and `garage.py`'s long-standing rule): it only tags. `control_loop.ControlLoop`
gained a new `_raise_validity_alerts` method, called once a cycle right after `decide()`, that turns the two tags into a raised or cleared alert file
via the existing `alerts.raise_alert`/`clear_alert` (the same RAM-folder, no-network path the machine alerts already use; the separate alert unit,
which does have outbound access, mails it through Resend). `main()` passes the usual `RUN_DIR / "alerts"`.

**A design note, since two different things both call themselves "voltage" here.** The garage calculation's OWN voltage (used in the `P_avail`
arithmetic itself) is still, deliberately, the Shelly's, not the meter's — finding 7's other half ("the garage calculation takes the voltage from the
Shelly, not the smart meter's") was read, and left as is: changing which voltage the arithmetic itself uses is a different, larger change than adding
the validity check the issue actually asked for, and the two would be easy to conflate. This fix only adds the meter's voltage as its own, additional,
specification-11.2 signal.

**A fixture-only correction found while testing.** Every test fixture across the suite that builds a "meter" device reading (`tests/control_rig.py`,
`tests/test_control_loop.py`'s `fresh()`, and the raw register maps in `test_acquirer.py`, `test_acquirer_faults.py`, `test_devices.py` and
`test_meter_cutover.py`) had to be given a plausible voltage (230.0 V, or the matching raw register) once this landed — a meter reading with no
voltage at all is exactly the "missing, so invalid" case the fix now correctly refuses to run on, and nearly every existing test that exercises the
garage limiter builds its meter reading without one (voltage did not exist before). A first pass at the fix also forgot `devices/feed.py`'s
`NUMBERS` whitelist (used when the controller reads the meter from the file the acquirer shares, one Modbus client only): without it, the voltage
silently never survived that hop, which `test_meter_cutover.py` caught.

Tests: `tests/test_garage.py` (direct, pure: `voltage_invalid` and `Reconciliation`, including the 29 s/31 s dwell boundary and that unaligned or
missing readings hold rather than reset), `tests/test_control_loop.py` (through `Supervisor.decide`: both checks stopping and clearing charging, and
the 30 s re-validation hold on recovery; and through `ControlLoop.cycle`: the alert file raised and cleared). Full suite: 1518 passed, 2 skipped.

Keeping this open until a logged night shows the meter's voltage being read as expected and (all being well) no alert, and until a deliberate test at
commissioning confirms an out-of-range or reversed reading is actually caught.

## Fix built and locally tested, 10 November 2026 (issue 63, go-live blocker)

Built and tested on `docs/plan-single-store`, not yet deployed. Live mode only: nothing is sent in shadow or logged mode, so this has no effect on
anything the Pi has done so far.

`LiveTransport._perform` caught a failed write and called `self.machine.forget()` with no register, wiping BOTH the mode and the set-point belief,
whatever write actually failed. Two things followed, both now fixed:

* **The whole sequence replanned from scratch every cycle.** `ActionMachine._plan` sends a write through 0 W first whenever the mode is about to
  change, but only when `self.setpoint != 0`; forgetting the set-point too (even though the 0 W write itself had just succeeded, the same cycle,
  over the same connection) made every retry believe the set-point unknown and reinsert that already-acknowledged 0 W again. Fixed: `forget` now
  takes the register that actually failed, `forget(w.register)`, the same call already used for the two write-switch-blocked cases just above it in
  the same method, with the same reasoning (`forget`'s own docstring): whatever was sent earlier in the sequence really did go out.
* **No count, no fault, no alert.** Nothing in the transport counted consecutive failures or told the machine the data was no longer trustworthy
  (specification 11.2's fourth condition: "a Modbus write has failed twice in succession"). A new counter, `LiveTransport._write_failures`
  (distinct from the existing `failures`/`FAILURES_BEFORE_RELEASE`, GitHub issue 41, which counts `observe()` itself raising — a different failure,
  a bug or a dropped connection, not a clean device refusal), increments on each failed write and resets on the next success. Once it reaches 2, an
  alert is raised once (naming the register, its class and the error) and `_observe` folds it into the same `valid` flag the readings' own freshness
  already computes: `self._valid = all(... REQUIRED_DEVICES ...) and self._write_failures < 2`. This reuses the machine's existing, already-tested
  D9 release path (`ActionMachine.tick`'s own `not valid` branch) rather than inventing a new one: one release episode, retried no more often than
  `RELEASE_RETRY_S` (30 s), returning to normal once `valid` has been true again for `RETURN_VALID_S` (30 s) — specification 11.1 exactly.
* **One interaction this surfaced:** `LiveTransport._release` (the EXPLICIT release path, used when the Supervisor's own decision, not a write
  failure, asks for a release) refused to do anything once `self.machine.state` was no longer `"running"`. Two consecutive write failures during an
  ordinary charge can now put the machine into `"released"` on their own, before any explicit release is asked for; when both happen together the
  explicit path must still carry out its own writes and retries, so its guard now accepts `"running"` or `"released"` rather than `"running"` only.
  `ActionMachine.exit_writes()` already behaved correctly for either state (it does not branch on `"running"` specifically), so no change was
  needed there.

Tests added to `tests/test_live_transport.py`, reproducing the issue's own evidence and its three "Tests to add": a mode write that always fails
(stops after at most 5 attempts in 60 s, not 59; one alert naming the register; the machine ends up `"released"`), a set-point write that fails once
and then works (no fault, no alert), and that a persistently refused write does not resend an already-acknowledged 0 W every second (under 10 times
in 60 s, not 59). `LiveTransport.health()` now also reports `write_failures`, alongside the existing `transport_failures`; `control_loop.py`'s status
file carries it too. Full suite: 1521 passed, 2 skipped.

Keeping this open until a supervised live run shows a genuinely refused write (or a deliberately injected one) producing exactly this: one alert, a
release, and recovery once the write succeeds again — not something that can be seen in shadow or logged mode.

## Fix built and locally tested, 10 November 2026 (issue 65, go-live blocker)

Built and tested on `docs/plan-single-store`, not yet deployed; this changes only what `live_gate.check` requires before `live` mode can send
anything, so it has no effect on shadow or logged mode either.

`live_gate.REQUIRED_TESTS` held eight entries; the specification's section 13 lists eleven as mandatory (T1 and T2 were already covered, the rest
were not). Added nine entries — `t3a-pv-not-full`, `t3b-pv-full-car-charging`, `t5-mode-change-timing`, `t6-comms-loss`, `t8-garage-regulation`,
`t9-garage-ct-sign`, `t10-smart-slot`, `t11-rms-current`, `t12-mode-reversion` — to `REQUIRED_TESTS` and to `MANUAL` (the owner's own sign-off with a
note; none of these can be driven by `commission run` against the real device, every one needs it observed behaving). `commission status` and
`pi-tools.sh commission` already report whichever tests are missing generically (nothing in that path is specific to the eight original names), so
no change was needed there; the issue's own third proposed-fix bullet turned out to already be satisfied.

**T10 is a deliberate exception to "add it and it's done".** It cannot be recorded as passed before GitHub issue 67 (smart slots reaching the
controller) is built: `signals.SmartSlot` exists and the Supervisor holds one, but nothing feeds it real dispatches yet, so there is nothing to
observe behaving correctly or not. This is not a waiver (D16: with a car, nothing is waived) — the gate stays closed on this one test until #67
lands and a real dispatch has actually been watched going through its paces, which is what T10 asks for. The code does not special-case this; it is
simply true that the test cannot honestly be recorded yet, and `REQUIRED_TESTS` says so with a comment.

The issue's "staged gates" idea (RC2's G1/G2/G3, so a limited first step is not blocked by a test that cannot yet be run) was considered and not
built: `live_gate` has one flat list for one gate (shadow/logged vs. live), and POST_SHADOW_PLAN's own G1 description already lists every mandatory
test, T10 included, as needed before G1 itself is complete — there is no sub-gate in the existing design that T10 could be deferred to without
inventing one, and doing that is a larger architectural question than this issue asked for. Simpler and no less correct: T10 blocks the gate like
every other mandatory test, and in practice it will not be satisfiable until #67 ships, which is the right order anyway.

Tests added to `tests/test_live_gate.py`: a record missing `t8-garage-regulation`, `t9-garage-ct-sign` and `t11-rms-current` keeps the gate closed
and names exactly those three (the issue's own "Tests to add"); the existing generic tests (looping over `REQUIRED_TESTS`) needed only
`supervised-run` to stay the last entry, which it still is. `OPERATIONS.md` gained a table of the nine new sign-off commands and their abbreviated
procedures. Full suite: 1522 passed, 2 skipped.

Keeping this open until every one of the nine is actually recorded at commissioning (T10 last, once #67 is built and deployed) and the gate opens
with the real write-switch and mode-file steps that follow in OPERATIONS.md.

## Fix built and locally tested, 10 November 2026 (issue 66, go-live blocker)

Built and tested on `docs/plan-single-store`, not yet deployed. Live mode only.

`ActionMachine.tick` already had a `relax_magnitude` argument for the CHARGE magnitude check, but nothing ever passed it, and
`LiveTransport.effect` (the separate check used right after a command is sent, before the machine's own 20 s verification even runs) applied the
strict tolerance unconditionally. Neither had the data to decide anyway: the device's own charging limit and live charge limit only reached the
controller with #70.

Added one small, shared rule, `LiveTransport._relax_magnitude(inputs, commanded_w)`: relax when the battery is within 3 % of the device's own
`charging_limit_soc` (the same range check as #70's clamp: an absurd register value is ignored, the full 100 % used instead) or the device's own
live charge limit (`max_charge`, register 10036) is below what was commanded. Used at both places specification 11.4 names: `_observe` now computes
it and passes it into `machine.tick`, and `effect` applies it directly.

Relaxing is not dropping the check: `ActionMachine._verified` and `LiveTransport.effect` both used to read `relax or abs(...) <= tolerance`, which
made a RELAXED check pass however little (or nothing) was actually drawn. Fixed to `drawn_w > 0.0` when relaxed: the firmware's own taper still
draws some power, so a battery showing no current at all, or an apparent discharge (which this installation's sign convention also reads as 0 W
drawn, never negative), is still a failure even when relaxed. This is "still require that the battery is charging and not discharging" from the
issue's own proposed fix.

Reproduced the issue's own evidence (2,500 W commanded, 1,500 W measured, 600 s, now with the battery reported at 98 % of a 100 % limit): the
set-point is written exactly twice (0 W through, then −2,500 W) and the machine stays `running` for the whole run — previously 189 of 600 s were
spent in HOLD, repeating.

Tests added, matching the issue's own three: `tests/test_live_transport.py` (a tapered charge at 98 % of a 100 % limit passes; the same shortfall
at 60 % still fails; no current at all, relaxed or not, still fails; a six-hundred-cycle run near the limit never holds) and
`tests/test_action_machine.py` (relaxed but showing nothing at all is still a `hold`, alongside the existing test that a genuine taper is allowed
to fall short). Full suite: 1525 passed, 2 skipped.

Keeping this open until a supervised live run actually reaches the taper (the weekly calibration charge to 100 %, or a high target) and shows no
HOLD and no `verify-failed` alert where the old code would have produced both.

## Fix built and locally tested, 10 November 2026 (issue 67, owner decision A: wire it in)

Built and tested on `docs/plan-single-store`, not yet deployed. Changes the `providers` and `control` services; nothing in `shadow` or the planner.

`control_logic.Supervisor.decide` already read `inputs["tariff"]` and ORs `signals.SmartSlot`'s result into `cheap` correctly (`cheap = ... or smart`,
line ~286) -- the entire decision-table side of this was already right. The only thing missing was that nothing ever put anything into
`inputs["tariff"]`: the providers service fetches and prices the dispatches, the controller is a separate process that never opens the database,
and the two were never connected.

Added `services.publish_dispatch_feed(path, intervals, fetched_at)`, called from `ProviderWorker.refresh` every time the "slots" fetch actually
runs (about once a minute, even when the answer is unchanged -- an unchanged "no slots" answer is still a fresh ask), writing a small file like
the plan file: `{"dispatches": [[start, end, "SMART"], ...], "fetched_at": ...}`. `ControlLoop._read_tariff` reads it every cycle and only includes
a `dispatches` key in `inputs["tariff"]["value"]` on a cycle where the file's own `fetched_at` has moved on from the one last seen -- otherwise
`signals.SmartSlot`'s own 300 s/1,800 s freshness timer would never age at all, since every call that supplies `fetch_ok=True` with a list resets
it to "now" regardless of how old the underlying fetch actually was. (The first version accepted one imprecision: a restarted controller treated an old file as fresh. The second peer review removed it: the
file's own `fetched_at` is now passed to `SmartSlot.update`, so the 300 s and 1,800 s rules count from the real fetch.)

New CLI flag on both sides: `--dispatch-feed`, defaulting to `/run/solarbank-cost-optimiser/tariff.json` on both the `providers` and `control`
commands. `deployment/solarbank-optimiser-control.service`'s `ExecStart` now names it explicitly (matching every other path there); `providers.service`
gained `RuntimeDirectory=solarbank-cost-optimiser` and `RuntimeDirectoryPreserve=yes` (it had neither: under `ProtectSystem=strict` it could not have
written into `/run/solarbank-cost-optimiser/` at all without them) alongside the same explicit flag.

Deliberately NOT done: the controller gets the raw, unfiltered SMART dispatch list (after the non-SMART-type filter already in
`OctopusAccount.dispatches`), not the planner's own billing-eligibility-filtered `apply_slot_rules`/`established_slots` output. `signals.SmartSlot`
has always been designed to take `planned_dispatches` directly ("never from a derived on/off flag") and apply its own car-charging and allowance
rules independently -- conflating it with the planner's separate billing-eligibility question would be a second, divergent implementation of the
same idea, not a fix. The allowance count inside `SmartSlot` staying in-memory-only (lost on a controller restart) is unchanged and tracked
separately as a small-points item (#72); this issue only had to wire the feed through, not fix that.

Tests: `tests/test_ram_settings.py` (the feed file is written on every genuine "slots" refresh, including an unchanged empty answer, from the
existing `worker.refresh("slots", ...)` fixture); `tests/test_control_loop.py` (`_read_tariff` only reports `dispatches` on a changed fetch; the
issue's own live example end to end -- a SMART dispatch outside the 23:30-05:30 window with the car charging reaches `economic == "charge_to"`
through a real `ControlLoop.cycle()`, which the clock window alone never could; a failed fetch, an empty list and a dispatch of another type all
leave it unchanged). Staleness aging itself was already thoroughly tested in `tests/test_signals.py` and is untouched here. Full suite: 1531
passed, 2 skipped.

Keeping this open until a logged night actually shows a real smart slot outside the clock window charging the battery (the 9 November 05:00-08:00
dispatch was the live example that found the gap in the first place) -- this cannot be told apart from the clock-window path in shadow evidence
taken before this fix existed.

## Fix built and locally tested, 10 November 2026 (issue 72, the smaller points)

Built and tested on `docs/plan-single-store`, not yet deployed. With this, every finding of the 9 November peer review (#63 to #72) and the
10 November one (#73) has a fix or a recorded decision.

1. **Verification records (11.4, 10.4).**
   * *Secondary check on the DC power* (register 10008, the reader's sign: positive = discharging): `ActionMachine._verified` and `LiveTransport.effect`
     now also require the battery not to be discharging above 100 W for CHARGE and ZERO_OUTPUT. Direction only, and deliberately lenient (recorded in
     the differences table above): the specification's "charging" for CHARGE is read as "not discharging", because a tapering or full battery may show
     no DC charging at all. An unknown DC reading leaves the AC check alone, as before.
   * *"possible sign inversion" and "possible firmware override"* are now written to the write journal as `record` lines, once a condition has lasted
     `VERIFY_S` (20 s, so a command not yet in effect is not recorded) and again only after it has cleared. Live mode only.
   * *Mode reversion*: `ActionMachine.supervise` now records the mode found, the battery status, the DC power and the AC power (10.4) every time the device
     is not in the required mode, and the firmware-override alert names them too. The transport journals these records.
   * `control_report.summarise_writes` (`pi-tools.sh writes`) lists records separately; they have no register and must not read as writes.
2. **Smart-slot allowance (6.2 item 3).**
   * *Persistence*: `signals.SmartSlot` keeps a revision counter and can save and restore the day's use; `ControlLoop` writes
     `/var/lib/solarbank-cost-optimiser/smart-slot-allowance.json` only when a new half-hour is counted and takes it back at start (only the current
     allowance day; anything malformed means starting afresh). `--slot-allowance` on the controller, named in its unit file.
   * *Bump charges*: first kept as they were (every half-hour the car charged in counted), then changed the same day once the owner confirmed what
     Octopus does: **a bump charge is billed at the peak rate** (seen on Sunday 9 November). So it is not smart charging, and specification 6.2 is
     followed as written: a half-hour counts toward the cap only if the car charged in it inside the cheap window or inside a listed SMART slot (or while
     a slot already under way is held to the end of its half-hour). Both sides apply it: `signals.SmartSlot` (the controller, given its own cheap
     window) and `services._charged_half_hours` (the planner, which reads the slot lists the providers service archived to tell a listed slot from a
     bump charge; where no list speaks for a half-hour, it still counts, the safe direction). Tested in `tests/test_signals.py` and
     `tests/test_review_codex3.py`.
3. **Register 10080 (14.4).** It was in fact already being read: the collector's settings probe reads the holding block 10074-10081. It was only never
   surfaced. `settings_probe.decode` now names it (`register_10080`, raw, "undocumented on the Solarbank 4"), so it appears in `device-settings.jsonl`
   and can be watched during T6. Never written; not interpreted, because the vendor's map does not name it.
4. **Stale notes**: `signals.py` and `devices/modbus_write.py` module docstrings corrected.
5. **Poll interval**: kept at 60 s and recorded in the differences table (two requests a minute, inside Octopus's limit of five).

Tests: `tests/test_action_machine.py` (the DC check: charging verified, AC right but DC discharging holds, small DC noise at ZERO_OUTPUT is fine; the
reversion record's fields), `tests/test_live_transport.py` (the DC check in `effect`; each record once after 20 s; none in logged mode; a reversion
journaled), `tests/test_signals.py` (the allowance survives a restart, only the current day, junk ignored, revision bumps only on a new half-hour),
`tests/test_control_loop.py` (a restarted controller keeps the count; the file is not rewritten every second), `tests/test_settings_probe.py`
(register 10080 surfaced), `tests/test_control_report.py` (records listed as records).

Keeping this open until commissioning shows the DC check agreeing with the AC check on the real device (T2 confirms the sign of 10008 in practice).

## Second peer review of the fixes, 11 November 2026 (issues 63 to 73)

Asked for by the owner: each fix read again against what its issue asked for, the original reproductions re-run, and the real modules driven end to
end where a unit test could hide a wiring gap. Built and tested on `docs/plan-single-store`, not deployed.

**Defects found in the fixes, now corrected**

| Issue | What was wrong with the first fix | Correction |
|---|---|---|
| #73 | Only the sentence changed; the `garage_limit` tag was still set during the battery's own taper (cause: the request is not rounded, the set-point is rounded down to 10 W). The note above claimed the case was unreachable | No tag when the device limit governs and the feed is within its limit; note above corrected |
| #63 | One failure count for every register: an acknowledged 0 W write reset it, so a mode write that kept failing let the machine return to running about every 32 s. The alert was never withdrawn after recovery, so it would have been mailed again every 6 hours | Failures counted per register (`_failed_writes`); the alert is withdrawn once no register has two failures in a row |
| #63 | In the released state a failed default-mode write was never retried (only the set-point was checked), and the "default mode refused" fallback (issue 42) applied only while running | Retry when either the mode or the set-point is unconfirmed; the fallback also applies in the released state |
| #67 | A restarted controller treated an hours-old dispatch file as just fetched and could start a slot from it | `fetched_at` carried through to `SmartSlot.update` |
| #72 | With no dispatch list at all (never fetched, or older than 1,800 s) a half-hour of car charging outside the window was not counted, where the planner counts it | Counted, the same as the planner: never more slots than the cap |
| #69 | `GarageRegulator.reset()` logged a warning every second while the garage reading was unknown | Clears the alert state only when it is set |
| #72 | The planner read the whole issuance table every minute to tell bump charges from slots | Ten-minute cache |

Wording that described behaviour since changed: the Settings page said the controller stops the battery at the minimum level (#64: the firmware
alone does) and that the tight freshness limit applies to every reading while charging (#68: the garage current only); the dashboard carried a
branch for a "reserve" restriction that can no longer occur; the T10 notes said issue 67 was not built; the connection-sharing procedure (#71) did
not say that it exercises two of the three connections, the third being covered in the supervised run. All corrected.

**No defect found**: #64, #65, #66, #68, #70. #68 was driven end to end with the real `ControlLoop` and `LiveTransport`: Shelly gaps of 4 s and 9 s
during a charge write nothing; a 12 s gap releases at about 10 s.

**Points for the owner, no code change**

1. *#67 and the "household rate verified" setting.* The controller now knows a daytime smart slot is cheap. The battery is only CHARGED in it when
   the plan also names a charge target for that time, and the planner prices a slot as cheap only when Settings has "household rate verified" on.
   With it off, a daytime slot holds the battery at zero output ("Cheap period with no charge target") and does not charge it.
2. *#69, two checks right after deploying.* The meter is now read as one request of 45 registers (10632 to 10676), not yet tried on the real
   meter: confirm meter, grid and garage readings keep arriving. The sign of the garage power reading is unconfirmed until T9: watch the first
   logged night for `reconciliation_invalid` or an alert email.
3. *#72, the DC check.* Anything drawing more than 100 W from the Solarbank's own socket while the battery is held at 0 W would fail verification
   (specification 11.4 as written). Needs the owner's answer on whether anything is plugged in.
4. *#66.* At the very top of a charge the relaxed check still needs some charge power to be seen. To observe at commissioning.
5. *A 4 s gap in the METER reading during a charge* (not the Shelly) writes 0 W and costs about 31 s of charging: the 2.5 s garage-current limit,
   then the 30 s start-up hold. This is the behaviour chosen on 8 November (option C); recorded here because the run showed its cost.
6. *Machine alerts other than the write-refused one are never withdrawn* until the controller restarts, so each is mailed again every 6 hours.
   Older than this review; not changed.
