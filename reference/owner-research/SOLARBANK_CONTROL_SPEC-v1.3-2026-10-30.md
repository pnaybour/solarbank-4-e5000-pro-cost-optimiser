# Solarbank 4 Controller – Functional Specification

Version 1.3, 30 October 2026. Owner: Paul Naybour.

This document specifies a Python controller for an Anker SOLIX Solarbank 4 E5000 Pro on an Intelligent Octopus Go tariff, with an EV charger on the same garage circuit. It is written to be implemented by Claude Code. Where a behaviour is not yet confirmed on the real hardware, it is marked **TBC** and listed in section 14.

Changes from v1.1: simplified decision table (IDLE, NO_PV and AT_MIN removed); fault policy split by cause; Octopus API changed to `flexPlannedDispatches`; extra smart slots count only while the car is charging and within the 6-hour cap; garage current taken from RMS current registers; command verification moved to the Shelly AC measurement; run modes for commissioning; extended commissioning tests.

Change in v1.3: the Smart Meter's branch-current protection is not used. It assumes the Solarbank is the only load on the branch and is limited to 16 A, so it cannot protect a garage feed shared with a 32 A EV charger.

---

## 1. Purpose and scope

The controller decides, at tariff and load events, whether the battery should charge from the grid, hold, or discharge. It also keeps the garage feed within its current limit while the battery is charging from the grid.

Division of responsibility:

| Function | Owner |
|---|---|
| Real-time demand matching (discharge to house load) | Anker firmware |
| Solar charging, MPPT, PV reduction when full | Anker firmware |
| Minimum SoC floor (10 %) | Anker firmware (register 60001) |
| Standby and sleep | Anker firmware |
| Tariff periods and Octopus smart slots | Python |
| Forecast charge target | Python (existing forecast module, input only) |
| Choice of operating mode | Python |
| Grid charge power, limited by the garage feed | Python |
| Fault handling | Python |

Python must not try to do real-time demand matching. In discharge it hands control to the firmware (mode 0).

## 2. Hardware and interfaces

| Device | Interface | Notes |
|---|---|---|
| Solarbank 4 E5000 Pro | Modbus TCP, port 502, unit ID 1 (TBC) | Modbus enabled in the Anker app |
| Anker Smart Meter Gen 2 | Modbus TCP, port 502, own IP | Main CT on the grid. The second CT (labelled PV/ESS) is on the garage feed, for measurement only |
| Shelly energy meter, 2 CTs | Local HTTP RPC | CT 1 = EV charger, CT 2 = Solarbank AC |
| Octopus Energy | GraphQL API (see 6.3) | Planned dispatches for the SmartFlex device |
| Garage circuit | 50 A MCB, 10 mm² SWA, ~20 m | Python limit 45 A |

Single-phase supply, nominal 230 V. There is no third-party PV.

Only one Modbus client may talk to each Anker device. Do not run the official Home Assistant integration (`anker_solix_official`) against the same devices at the same time.

## 3. Register map

Source: `anker-charging/ha-anker-solix-official`, device configuration files of 23 October 2026. INT32 and UINT32 values are big-endian (high word first). Implement the map in a single module so addresses can be corrected without code changes.

### 3.1 Solarbank 4 – read

| Name | Address | FC | Type | Scale | Notes |
|---|---|---|---|---|---|
| battery_status | 10001 | 04 | UINT16 | 1 | 0 standby, 1 charging, 2 discharging, 3 sleep |
| pv_power | 10002 | 04 | INT32 | 1 W | Solarbank PV |
| battery_power | 10008 | 04 | INT32 | 1 W | Negative = charging, positive = discharging. Includes PV charging |
| load_power | 10010 | 04 | INT32 | 1 W | |
| grid_power | 10012 | 04 | INT32 | 1 W | Positive = import, negative = export |
| battery_soc | 10014 | 04 | UINT16 | 1 % | |
| max_charge_power | 10036 | 04 | INT32 | 1 W | Live limit set by firmware |
| max_discharge_power | 10038 | 04 | INT32 | 1 W | |
| ac_grid_output_power | 10208 | 04 | INT32 | 1 W | |
| ems_mode_mask | 32774 | 04 | UINT16 | bitmask | Bit 0 self-consumption, bit 5 third-party. Read singly, not in a batch |

### 3.2 Solarbank 4 – read/write

| Name | Address | FC read/write | Type | Range | Notes |
|---|---|---|---|---|---|
| operating_mode | 10064 | 03 / 06 | UINT16 | 0,1,3,4,5,6,7 | 0 self-consumption, 3 third-party control |
| battery_power_setpoint | 10071–10072 | 03 / 16 | INT32 | ± W | Negative = charge, positive = discharge. Acts only in mode 3. **Sign TBC (test T2)** |
| charging_limit_soc | 60000 | 03 / 06 | UINT16 | 80–100 % | Read only for this controller |
| discharge_limit_soc | 60001 | 03 / 06 | UINT16 | 0–20 % | Read only. Set to 10 % in the app |
| backup_reserve_soc | 60002 | 03 / 06 | UINT16 | 0–100 % | Read only. Backup reserve must be disabled (section 12) |
| backup_soc_enable | 60003 | 03 | UINT16 | 0/1 | Must read 0 |

Known issue: on a Solarbank 4, writing a new setpoint has been reported to reverse the actual charge direction, not just the read-back of 10071 (official repo issue #82, unresolved). Never rely on reading back 10071. Verify every command from measured power (section 11.4). Automatic writes are forbidden until test T2 passes.

### 3.3 Smart Meter Gen 2 – read (FC 04)

| Name | Address | Type | Scale | Use |
|---|---|---|---|---|
| primary_phase_1_voltage | 10632 | UINT16 | ÷10 V | V |
| primary_phase_1_current | 10635 | INT16 | ÷100 A | Grid current (RMS) |
| primary_phase_1_active_power | 10638 | INT32 | 1 W | Grid power, sign = direction |
| secondary_phase_1_current | 10666 | INT16 | ÷100 A | Garage current (RMS) |
| secondary_phase_1_active_power | 10669 | INT32 | 1 W | Garage power, sign = direction |

The mapping is fixed by the wiring: primary (main CT) = grid, secondary (PV/ESS CT) = garage feed.

Use the current registers (magnitude) for circuit loading, because current derived from power under-reads loads with a power factor below one. Use the active power registers for direction and for reconciliation. Confirm the sign convention of the PV/ESS CT in test T9.

## 4. Configuration parameters

| Parameter | Default | Meaning |
|---|---|---|
| RUN_MODE | dry_run | dry_run, commissioning or production (section 13) |
| GARAGE_LIMIT_A | 45 | Garage feed current limit |
| AVG_WINDOW_S | 3 | Rolling average for garage current |
| P_MAX_W | 2500 | Absolute maximum grid charge power |
| P_MIN_W | 100 | Below this, command 0 W. Confirm in T2 |
| SOC_HYST_PCT | 3 | BELOW_TARGET hysteresis |
| EV_ON_W / EV_ON_DWELL_S | 1000 / 3 | EV charging detection |
| EV_OFF_W / EV_OFF_DWELL_S | 300 / 120 | EV stopped detection |
| METER_POLL_S | 1 | Smart meter and Shelly poll interval |
| SB_POLL_S | 5 | Solarbank poll interval |
| DECISION_S | 10 | Decision table evaluation interval |
| SETPOINT_DEADBAND_W | 50 | Ignore smaller setpoint increases |
| RAMP_UP_MIN_INTERVAL_S | 5 | Minimum time between setpoint increases |
| MODE_SETTLE_S | 2 | Wait after a mode write before writing a setpoint |
| MODE_CHECK_S | 60 | Interval to verify the mode |
| MODE_REVERT_LIMIT | 3 per hour | Above this, stop writing (section 10) |
| STALE_S | 10 | Reading older than this is invalid. Review after commissioning |
| ALIGN_S | 1 | Maximum timestamp difference for reconciliation |
| RECON_PERSIST_S | 30 | Reconciliation error must persist this long |
| HOLD_TIMEOUT_S | 900 | Fault hold before handing back to firmware (section 11.1) |
| OCTOPUS_POLL_S | 120 | Planned dispatch refresh. API limit is 5 requests per minute |
| OCTOPUS_START_FRESH_S | 300 | A slot may start only on data this fresh |
| OCTOPUS_STALE_S | 1800 | Older data cannot hold a slot open |
| SMART_CAP_H | 6 | Discounted smart charging per 24 h |
| OFFPEAK_START / OFFPEAK_END | 23:30 / 05:30 | Standard IOG window, Europe/London |
| VERIFY_S | 20 | Time allowed for a command to take effect |

## 5. Signals

### 5.1 Measured

Every reading carries the timestamp at which it was taken.

| Signal | Source | Unit |
|---|---|---|
| V | Smart meter primary phase 1 voltage | V |
| I_garage_now | Smart meter secondary phase 1 current, magnitude, latest sample | A |
| I_garage_avg | I_garage_now, 3 s rolling average | A |
| P_garage | Smart meter secondary phase 1 power | W, positive = import into garage (confirm T9) |
| P_grid | Smart meter primary phase 1 power | W |
| I_EV, P_EV | Shelly CT 1 current and power | A, W |
| I_batt, P_batt_ac | Shelly CT 2 current and power | A, W, positive = Solarbank drawing from the grid |
| P_batt_dc | Solarbank 10008 | W, negative = charging |
| P_pv | Solarbank 10002 | W |
| SoC | Solarbank 10014 | % |
| P_MAX_LIVE | Solarbank 10036 | W |
| MODE | Solarbank 10064 | enum |
| TARGET_SOC | Forecast module | % |

### 5.2 Derived

| Signal | Definition |
|---|---|
| OFFPEAK | Local time within OFFPEAK_START–OFFPEAK_END |
| SMARTSLOT | Section 6.2 |
| CHEAP | OFFPEAK ∨ SMARTSLOT |
| EV | TRUE when P_EV > EV_ON_W for EV_ON_DWELL_S. FALSE when P_EV < EV_OFF_W for EV_OFF_DWELL_S. Otherwise unchanged |
| BELOW_TARGET | TRUE when SoC ≤ TARGET_SOC − SOC_HYST_PCT. FALSE when SoC ≥ TARGET_SOC. Otherwise unchanged |
| P_unmetered | P_garage − P_EV − P_batt_ac, using readings within ALIGN_S of each other |
| VALID | Section 11.2 |

TARGET_SOC is clamped to the range discharge_limit_soc to charging_limit_soc.

## 6. Tariff inputs

### 6.1 Standard off-peak

23:30–05:30 Europe/London local time for the whole home, every day. Handle the clock changes correctly.

### 6.2 Extra smart slots

Octopus applies the discounted rate to the whole home when it schedules smart charging outside the off-peak window. Bump charges are billed at the day rate. Only the first SMART_CAP_H hours of smart charging in 24 hours are discounted. Smart charging inside the off-peak window counts towards the cap.

SMARTSLOT is TRUE only when all of the following hold:

1. Now is within a planned dispatch of type `SMART`. Other types, such as `BOOST`, do not count.
2. The car is actually charging (EV = TRUE). Planned dispatches can remain listed for hours after the car is unplugged, and will not be billed at the discounted rate.
3. Cumulative smart-charging time in the last 24 hours, including time inside the off-peak window, is no more than SMART_CAP_H. The reference period for the cap is **TBC**. Until confirmed, use a rolling 24 hours.
4. To start: the dispatch was confirmed by a successful fetch no older than OCTOPUS_START_FRESH_S.

Once SMARTSLOT has become TRUE, it stays TRUE until the end of the current half-hour, provided the car is still charging. This holds even if the dispatch disappears from the list or a fetch fails. Octopus bills in half-hours, and it can return an empty list for several minutes while it re-plans a running session.

A failed fetch is not the same as an empty list. An error response means "unknown": keep the last good plan, but do not start a new slot from it once it is older than OCTOPUS_START_FRESH_S, and discard it once it is older than OCTOPUS_STALE_S.

### 6.3 Data source

Implement the tariff source behind an interface with two adapters. Choose one by configuration.

1. Octopus GraphQL API (`https://api.octopus.energy/v1/graphql/`). Use `flexPlannedDispatches(deviceId)`, which returns `start`, `end`, `type` and `energyAddedKwh`. The older `plannedDispatches(accountNumber)` is deprecated and must not be used. The query takes the SmartFlex device ID, not the account number. Obtain the device ID once and store it in configuration. The rate limit is 5 requests per minute.
2. Home Assistant REST API, reading `binary_sensor.octopus_energy_*_intelligent_dispatching` and its `planned_dispatches` attribute from the BottlecapDave Octopus Energy integration.

## 7. Decision table

Evaluate every DECISION_S, and also immediately when CHEAP, EV, VALID or BELOW_TARGET changes. Rules are ordered. The first match wins.

| Order | VALID | CHEAP | BELOW_TARGET | EV | Action |
|---|---|---|---|---|---|
| 0 | FALSE | any | any | any | FAULT (section 11) |
| 1 | TRUE | TRUE | TRUE | any | CHARGE |
| 2 | TRUE | TRUE | FALSE | any | ZERO_OUTPUT |
| 3 | TRUE | FALSE | any | TRUE | ZERO_OUTPUT |
| 4 | TRUE | FALSE | any | FALSE | DISCHARGE |

Intent of each rule:

1. Cheap power and the battery below its forecast target: charge, within the garage limit.
2. Cheap power and the target already met: hold the battery. Do not discharge at the cheap rate.
3. Car charging outside a cheap period, for example a bump charge, or before a new dispatch has been confirmed: hold the battery so it does not discharge into the car. Smart slots never reach this rule, because CHEAP is TRUE.
4. Expensive period, car not charging: the firmware matches house demand.

PV, the minimum SoC floor and standby are left entirely to the firmware. In particular, when the battery reaches its floor in DISCHARGE, the firmware puts the unit to sleep by itself (observed on this installation). Whether the unit also rests in ZERO_OUTPUT is recorded in test T4. It does not change the logic.

### 7.1 PV during smart slots and car charging

Python does not divert PV. In every state other than DISCHARGE, the firmware decides where PV goes:

- If the battery has room, PV charges the battery (**TBC for mode 3 at 0 W, test T3a**).
- If the battery is full, the firmware reduces PV output to match measured demand, to prevent grid export. This has been observed on this installation. The car is part of that demand, so PV supplies the car before any grid import (**TBC for mode 3 at 0 W, test T3b**).

During a smart slot the whole home is on the cheap rate. Storing PV in the battery for use at the day rate is therefore worth more than sending it to the car, which can draw cheap grid power. No PV diversion logic is required.

### 7.2 Decision function

```python
def decide(valid: bool, cheap: bool, below_target: bool, ev: bool) -> str:
    if not valid:
        return "FAULT"
    if cheap and below_target:
        return "CHARGE"
    if cheap or ev:
        return "ZERO_OUTPUT"
    return "DISCHARGE"
```

Unit tests must cover all 16 input combinations and check each against the table.

## 8. Actions

| Action | Register 10064 | Register 10071 | Expected behaviour |
|---|---|---|---|
| CHARGE | 3 | −P_set (section 9) | Grid charging at P_set |
| ZERO_OUTPUT | 3 | 0 | No grid charging, no discharge. PV handled by firmware (**TBC, T3**) |
| DISCHARGE | 0 | not written | Firmware matches house demand |
| FAULT | see 11.1 | see 11.1 | Depends on the cause |

When CHARGE is active and P_set is 0 W, stay in mode 3. Charging resumes when headroom returns, without waiting for a new tariff event.

## 9. Charge power (garage regulation)

Runs every METER_POLL_S, only while the action is CHARGE.

```
I_eff   = max(I_garage_avg, I_garage_now)
I_other = I_eff − I_batt
P_avail = min(P_MAX_W, P_MAX_LIVE, max(0, (GARAGE_LIMIT_A − I_other) × V))
P_set   = 0          if P_avail < P_MIN_W
P_set   = P_avail    otherwise, rounded down to 10 W
```

Taking the higher of the latest sample and the 3 s average means a sudden rise, such as the car starting, cuts the charge rate on the next poll. Increases still follow the average, so brief dips do not cause the rate to hunt.

Write rules:

1. A decrease of any size is written immediately.
2. An increase is written only if it is at least SETPOINT_DEADBAND_W and at least RAMP_UP_MIN_INTERVAL_S has passed since the last increase.
3. Never write more than once per second.

The 50 A MCB is the only hardware protection. There is no independent firmware backstop for the garage feed.

This is acceptable because the worst case without Python is modest. With the car at 32 A and the battery stuck at 2,500 W, the feed carries about 43–44 A plus unmetered loads, within the MCB rating. A sustained overload needs a large extra garage load as well, and the MCB covers that.

If I_garage_avg stays above GARAGE_LIMIT_A for 10 s while P_set is already 0 W, the excess is not caused by the battery. Raise an alert. Python cannot reduce EV or other garage loads.

## 10. Mode management and write sequencing

1. Write 10064 only when the required mode changes.
2. On every action change, write in this order: setpoint 0 W, then the mode (if it changes), wait MODE_SETTLE_S, then the new setpoint.
3. Never change direction directly. Always pass through 0 W.
4. Every MODE_CHECK_S, read 10064. If it differs from the required mode:
   - Record the mode found, battery_status, P_batt_dc and P_batt_ac.
   - If the battery is grid charging at high power with no command to do so, treat it as a firmware protection or maintenance function (for example calibration). Do not override it. Raise an alert and wait for it to end.
   - Otherwise, re-assert the mode once, using the sequence in step 2.
   - If unexplained reversions exceed MODE_REVERT_LIMIT in an hour, enter HANDS_OFF (section 11.1).
5. All Modbus reads and writes for one device go through one connection and one lock.

## 11. Fault handling

### 11.1 Fault policy by cause

| Cause | Action |
|---|---|
| Invalid data, Modbus to the Solarbank working | HOLD: 0 W, mode 3. After HOLD_TIMEOUT_S, if the EV reading is valid and EV is FALSE, hand back (0 W, then mode 0). Otherwise stay in HOLD |
| Command verification failed twice | HOLD, then HANDS_OFF |
| Unexplained mode reversions above the limit | HANDS_OFF |
| Loss of Modbus to the Solarbank | No write is possible. Raise an alert. On reconnection, enter HOLD and verify the measured power before resuming |
| Planned shutdown, or unhandled exception with Modbus available | 0 W, then mode 0 (hand back to firmware) |
| Garage over-current with P_set already 0 W | Alert only |

HOLD prevents both grid charging and discharge. It is the safe state while Python is alive, because mode 0 could discharge into the car.

HANDS_OFF writes 0 W, then makes no further writes until a person resets it. The firmware keeps whatever mode it is in.

Return from HOLD to normal operation automatically once VALID has been TRUE for 30 s. Return from HANDS_OFF only by manual reset.

### 11.2 VALID

VALID is FALSE if any of these holds:

- Any required reading is older than STALE_S.
- P_unmetered < −230 W (about −1 A) continuously for RECON_PERSIST_S. Use only readings within ALIGN_S of each other.
- Voltage is outside 207–253 V.
- A Modbus write has failed twice in succession.

Stale Octopus data does not make VALID FALSE. It makes SMARTSLOT FALSE (section 6.2), which is the safe direction.

### 11.3 Loss of Modbus to the Solarbank

If Python cannot write, it cannot command anything. The unit is expected to keep running its last setpoint (reported on the Solarbank Max AC; **TBC on the Solarbank 4, test T6**). The garage is then protected only by the MCB (see section 9).

### 11.4 Command verification

Primary check: the Shelly CT 2 measurement of the Solarbank's AC connection (P_batt_ac). Secondary check: register 10008 (P_batt_dc), for direction only, allowing for PV charging and conversion losses.

Check VERIFY_S after any setpoint or mode write, and then every SB_POLL_S:

| Action | Primary pass condition (P_batt_ac) | Secondary (P_batt_dc) |
|---|---|---|
| CHARGE, P_set ≥ P_MIN_W | Drawing from grid, within ±max(150 W, 20 %) of P_set | Charging |
| CHARGE, P_set = 0, or ZERO_OUTPUT | Not drawing from grid above 150 W | Not discharging above 100 W |
| DISCHARGE | No check | No check |

Relax the CHARGE magnitude check when SoC is within 3 % of charging_limit_soc or P_MAX_LIVE is below P_set, because the firmware tapers charging.

If P_batt_ac shows the opposite direction to the command, record "possible sign inversion". If the Solarbank draws from the grid during ZERO_OUTPUT, record "possible firmware override". Two consecutive failures trigger the policy in 11.1.

### 11.5 Startup

1. Connect to all devices. Read every register in section 3.
2. Check that ems_mode_mask has bits 0 and 5 set. If either is missing, refuse to run.
3. Check that backup_soc_enable = 0. If not, refuse to run.
4. Wait until VALID is TRUE, then evaluate the decision table and apply the action.

### 11.6 Shutdown

On SIGTERM, SIGINT or any unhandled exception: write 10071 = 0, then 10064 = 0, then exit. Use try/finally and signal handlers so this runs in every exit path that allows it.

## 12. Prerequisites in the Anker app

1. Enable Modbus TCP (Three-Party Control Settings) on the Solarbank and on the Smart Meter Gen 2. Reserve their IP addresses in DHCP.
2. Disable Backup Reserve, SOC Calibration, Fast Charging and Storm Guard. Any of these can override third-party control and force full-power grid charging.
3. Set the discharge limit (minimum SoC) to 10 %.
4. Set the charge limit to 100 %, or the chosen ceiling.
5. In Settings → CT Monitoring Settings, do not enable branch protection current. It assumes the Solarbank is the only load on the branch and is limited to 16 A. With the EV charger on the same branch, the firmware would try to hold the whole feed below 16 A by adjusting the battery, which it cannot achieve and which could cause unwanted discharge. Do not select third-party PV monitoring either, and leave "A third-party PV system has been installed" unticked. Confirm in T9 that the PV/ESS CT readings remain available over Modbus with neither option selected.

## 13. Run modes and commissioning

| RUN_MODE | Behaviour |
|---|---|
| dry_run | Reads everything, computes decisions, writes nothing |
| commissioning | Writes only from test scripts in `tests/commissioning/`. Each write requires manual confirmation |
| production | Automatic writes. Refuses to start unless every mandatory test is recorded as passed |

Record results in a commissioning file that production mode checks at startup.

| Test | Mandatory | Procedure | Pass / record |
|---|---|---|---|
| T1 Mode mask | Yes | Read 32774 | Bits 0 and 5 set |
| T2 Sign | Yes | Mode 3. At least 20 alternating and repeated changes, including −500, +300, −600 then −300, and −200 after a positive value. Hold each for 20 s | Every negative value charges and every positive value discharges, measured by the Shelly |
| T3a PV, battery not full | Yes | Daylight, SoC < 95 %, mode 3, 0 W | PV charges the battery |
| T3b PV, battery full | Yes | Daylight, SoC 100 %, mode 3, 0 W, car charging | PV follows demand and supplies the car |
| T4 Rest | No | Night, mode 3, 0 W | Record whether 10001 goes to 0 or 3, and the delay to start charging on the next command |
| T5 Mode change | Yes | 3 → 0 and 0 → 3 | Record the time until behaviour changes |
| T6 Comms loss | Yes | Mode 3, −300 W, with independent monitoring and the app ready to switch off third-party control. Stop the controller for 10 minutes | Record whether charging continues |
| T7 Overrides | No | Run 24 h in production | No unexplained grid charging |
| T8 Garage regulation | Yes | CHARGE at full rate, then: start the car, switch the freezer, add a 2 kW load, delay meter readings, then remove each | I_garage_avg ≤ 45 A after each step. Record peak and settling time |
| T9 Garage CT | Yes | With branch protection and third-party PV monitoring off, switch a known garage load on and off | Readings present over Modbus. P_garage rises positive with garage import |
| T10 Smart slot | Yes | Observe a dispatch start, move, shorten, cancel and end. Also a fetch failure, an empty list during a session, and a clock change | SMARTSLOT behaves as section 6.2 |
| T11 RMS current | Yes | Compare I_garage_now with a clamp meter at several loads | Within 0.5 A |
| T12 Mode reversion | Yes | Change the mode in the Anker app while running | Detected, re-asserted once, HANDS_OFF after the limit |

## 14. Open items (TBC)

1. Sign of 10071 on this firmware (T2).
2. PV behaviour in mode 3 at 0 W (T3a, T3b). If PV is curtailed to zero when the battery is full, accept the loss for now. It occurs only with a full battery during a cheap period or a bump charge. Do not switch to mode 0 in those states, because mode 0 could discharge into the car.
3. Whether the unit rests in mode 3 at 0 W (T4). Informational only.
4. Behaviour on loss of communication (T6). Anker has said a timeout fallback for third-party control will be adopted. Register 10080 lies in the holding range the official integration reads. On the X1 it is a communication watchdog. Read it, but do not write it until Anker documents it for the Solarbank 4.
5. Sign of the PV/ESS CT power reading, and that its readings are available with branch protection and third-party PV monitoring both off (T9).
6. P_MIN_W. 100 W follows Anker's guidance on control accuracy.
7. The reference period for the 6-hour smart charging cap.
8. The SmartFlex device ID for the charger or car.
9. Modbus unit ID for each device.

## 15. Implementation notes for Claude Code

- Logging and alerting already exist in the project. Use the existing logger for every event, warning and alert named in this spec. Do not add a new logging system.
- Python 3.11 or later, asyncio, pymodbus 3.x. Local HTTP to the Shelly using aiohttp or httpx. Confirm the Shelly RPC method for the installed model.
- Suggested layout: `config.py`, `registers.py`, `devices/solarbank.py`, `devices/meter.py`, `devices/shelly.py`, `tariff/octopus.py`, `tariff/homeassistant.py`, `logic/signals.py`, `logic/decision.py`, `control/charge_power.py`, `control/faults.py`, `supervisor.py`.
- `logic/decision.py` must be a pure function with no I/O, tested over all 16 combinations.
- Signal hysteresis, dwell timers, the smart-slot rules and the half-hour hold belong in `logic/signals.py` and `tariff/`, tested with simulated time, including clock changes.
- Every device class needs a fake implementation for tests.
- RUN_MODE defaults to dry_run. Build and test dry_run first: monitoring, tariff integration, decision logic and simulated devices.
- In normal operation, never write registers other than 10064 and 10071.
- Run as a systemd service with restart on failure. The shutdown handler (11.6) must run before the restart.
