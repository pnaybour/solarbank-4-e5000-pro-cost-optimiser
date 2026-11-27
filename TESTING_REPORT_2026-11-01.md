# Shadow testing report: 1 November 2026

Release under test: 0.5.36 (deployed 31 October, 13:12), running in shadow mode on the Pi. The Anker AI was in control of the battery throughout. The house was empty from 1 November, so these days should not count in any cost comparison with the Anker AI.

This report records what was observed and where the code is likely responsible. No code has been changed. Each item lists the evidence, a suspected cause with file references, and a test to add before fixing.

## Summary

| # | Issue | Priority | Effect if live |
|---|-------|----------|----------------|
| 1 | Charging withdrawn across midnight for about an hour | High | Loses cheap charging on short or post-midnight windows |
| 2 | Plan target drifts and the hold uses a different target | High | Unpredictable charge levels |
| 3 | 30-second discharge flickers during a hold | Medium | Brief discharge at the cheap rate |
| 4 | Same command issued many times a minute | Medium | Repeated Modbus writes to the Anker |
| 5 | 05:30 release reached by "Plan has expired" fallback | Low | Correct outcome, wrong route |
| 6 | Full battery stays in charge mode in the cheap window | Low | Small discharges before 05:30 |

System health was good: one-second control cycle, no late cycles, all inputs fresh, database writing normally, storage guard within budget. The difference report stops at midnight only because it covers completed days.

## Observations: night of 31 October to 1 November

Shadow decisions from control-commands.jsonl (London time):

```
Sat 23:30 ac_charge CHARGE_LIMIT  Plan: charge to 65%   (x10 within the minute)
Sat 23:41 ac_charge CHARGE_LIMIT  Plan: charge to 70%
Sat 23:45 ac_charge CHARGE_LIMIT  Plan: charge to 65%
Sat 23:59 ac_charge WITHDRAW      Plan: no grid charging; the Anker runs the house as normal
Sun 00:59 ac_charge CHARGE_LIMIT  Plan: charge to 75%   (x10 between 00:59 and 01:00)
Sun 01:15 ac_charge CHARGE_LIMIT  Plan: charge to 70%
Sun 01:19 ac_charge WITHDRAW      Battery at 69%, target 55% reached: holding idle until the cheap window ends
Sun 01:19 discharge INHIBIT       (same reason)
Sun 02:59 discharge ALLOW         Plan: no grid charging; the Anker runs the house as normal
Sun 03:00 discharge INHIBIT       Battery at 99%, target 55% reached
Sun 04:04 discharge ALLOW         Plan: no grid charging
Sun 04:04 discharge INHIBIT       Battery at 99%, target 55% reached
Sun 05:30 discharge ALLOW         Plan has expired: economic request withdrawn
```

The Anker AI charged to 99% by 03:00. The battery was still at 92% at 17:45 because the house was empty.

## 1. Midnight gap (high)

Evidence: charging withdrawn at 23:59, inside the 23:30 to 05:30 window, and not resumed until 00:59. Similar mismatches between 23:54 and 00:05 on 30 and 31 October. The PlanMemory docstring in control_logic.py already records a planner stall at 00:05 on 31 October.

Suspected cause: the planner's input guard includes the London date. In shadow.py, `guard()` fingerprints `"london_day"`, and `plan_view()` withdraws the active plan with "Inputs changed; shadow plan withdrawn pending replan" whenever the guard changes. At midnight every plan is invalidated at once, and `publish()` also rejects any plan whose guard changed during solving. If replanning then fails or is slow, the controller sees no valid charging window. PlanMemory (PLAN_HOLD_S = 3600) only bridges "Plan has expired" and missing-file cases, not a valid plan that says "normal". The hour-long gap is suspiciously close to PLAN_HOLD_S.

Check first:
- `journalctl -u solarbank-optimiser-shadow --since "2026-10-31 23:45" --until "2026-11-01 01:05"` for PlanningUnavailable reasons.
- The issuances table for plans issued in that period, comparing window start, end and target.

Test to add: a cheap window from 23:30 to 05:30 with planning at 23:58, 00:00 and 00:02. Assert that the published window and target persist across midnight, and that the controller stays in charge_to throughout.

Fix options: remove `london_day` from the guard, or make the day roll-over refresh the plan without withdrawing it. If the date is needed for daily evidence, key it to the start of the cheap window, not the calendar day.

## 2. Target drift and mismatch (high)

Evidence: targets of 65, 70, 65, 75 and 70% within two hours. Then the hold said "target 55% reached" while the last charge command said 70%.

Suspected cause: when the controller is inside a window, the decision uses `window["target_soc"]` from the current plan file (control_logic.py, `read_plan`). The planner re-solves every few minutes, and the guard includes the SoC band (`round(value/SOC_BUCKET)`), so each band crossing during charging forces a replan with a new target. `_target_reached` is sticky, so once a lower target such as 55% is issued, the hold latches even if later plans raise the target.

Test to add: a sequence of plans whose window target changes while SoC rises. Assert a defined rule, such as the target only rising within a window, or a minimum change before re-planning.

Fix options: freeze the window target once charging starts, apply hysteresis to target changes, or remove the SoC band from the guard during an active window.

## 3. 30-second discharge flickers (medium)

Evidence: discharge ALLOW at 02:59:35, then INHIBIT at 03:00:06. Again ALLOW then INHIBIT at 04:04:04.

Suspected cause: a newly published plan briefly says "normal" because `controller_plan_document()` (shadow.py) sets the window from `next_window`. That is built from contiguous cheap rows with the same `grid_target`, and its start can fall after the issue time or be split at a row boundary. Either way, for a few seconds the controller is outside the window and allows discharge. Commands expire after COMMAND_TTL_S = 30 s, which matches the flicker length.

Test to add: re-publish a plan mid-window at an interval boundary and assert that the hold continues without an ALLOW.

Fix option: once the target is reached inside a window, hold until the window end recorded at the start, not the end in the latest plan.

## 4. Duplicate commands (medium)

Evidence: the same CHARGE_LIMIT logged 10 times at 23:30 and about 10 times between 00:59 and 01:00.

Suspected cause: `Preparer` in control_exec.py re-issues when the command key changes or the refresh period passes. The charge request is `min(max_charge_w, AC_CHARGE_LIMIT_W)`, so a fluctuating `max_charge` reading from the device, or rapid plan re-publication, would produce new commands. In shadow mode nothing is acknowledged, which may also drive re-issue.

Test to add: a steady decision held for 60 seconds should produce one command plus refreshes at the documented interval only.

## 5. 05:30 release via fallback (low)

Evidence: "Plan has expired: economic request withdrawn" at 05:30:00.

Cause: `publish()` caps plan expiry at `next_window["end"]`, so the plan file expires exactly when the window ends. The outcome is correct, but the release depends on expiry rather than a deliberate end-of-window decision, and it hides genuine planner failures at that moment.

Fix option: let the plan outlive the window and have the controller release on reaching the window end.

## 6. Full battery in charge mode (low)

Evidence: on 30 October at 05:25 the Anker discharged for two minutes at 100% while the controller would have inhibited discharge.

Fix option: when SoC reaches 100% inside a cheap window, move to hold at 0 W. This already happens when the target is reached; check that it also covers a 100% target.

## Design notes for the comparison with the Anker AI

- On Intelligent Octopus Go with no solar, over-charging is cheap: stored 7p energy carries over, and the cost is round-trip loss and wear. Under-charging is expensive, because the shortfall is bought at about 27p. The controller should aim low only when it is confident about the load.
- Add an occupancy input, either a manual away switch or a calendar entry. The Anker AI has no away setting, which explains its 99% charge on 1 November.
- Compare cost, not decisions: the Anker's actual cost from the meter against the controller's decisions replayed through a battery model (2.5 kW, 10% floor, about 10 to 15% round-trip loss). Value the end-of-day SoC at the cheap rate, and use at least two weeks of normal (not away) days.

## Daily check used during testing

Run on the Pi. It is read-only.

```bash
sudo /opt/solarbank-cost-optimiser/current/.venv/bin/python -c "import json,time,datetime as d;t=time.time()-16*3600;L=[json.loads(x) for x in open('/var/lib/solarbank-cost-optimiser/control-commands.jsonl') if '\"event\":\"command\"' in x];[print(d.datetime.fromtimestamp(e['at']).strftime('%a %H:%M'),e['command']['channel'],e['command']['action'],str(e['command'].get('reason',''))[:90]) for e in L if e['at']>t];s=json.load(open('/run/solarbank-cost-optimiser/control-loop.json'))['decision']['inputs'];print('NOW soc',s['soc'],'ev_kw',s['ev_kw'],'garage_A',s['garage_current_a'])"
```
