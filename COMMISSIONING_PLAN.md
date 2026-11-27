# Commissioning plan: one installation, kept simple

Written 11 November 2026 from what the documents already say; nothing here changes a decision. The authority for each test is the owner's control specification
(`reference/owner-research/SOLARBANK_CONTROL_SPEC-v1.3-2026-10-30.md`, sections 12 to 14, frozen). The software's side is in `OPERATIONS.md`
("Commissioning and going live"). The gates are in `POST_SHADOW_PLAN.md` (D19: G1 supervised, G2 a limited first live step, G3 unattended).

**Rules for the whole plan.** The owner is at the Pi and the battery for every step that writes. Do it in daylight, outside the cheap window (23:30 to 05:30)
unless a step says otherwise. If anything looks wrong: stop, release (section 6), and write down what you saw. A test passes only for the code that is
running, so deploy every pending change **before** starting, then change no code until the end.

**Commissioning is a checklist, not a software gate** (owner, 12 November 2026). The controller sends when `control-mode` says `live` and `write-switch` lists the class, nothing more. `commission pass` and `commission run` still write each result into `commissioning.json` as a notebook, and a code change keeps it. After each change the owner decides which tests, if any, to repeat: a change that does not alter how the controller writes (for example which slots count as cheap) needs none. The checklist below, and the table of results, is the record.

The commands use these two names; paste them first in each session on the Pi:
```
S=/opt/solarbank-cost-optimiser/current/.venv/bin/solarbank; T=/opt/solarbank-cost-optimiser/current/scripts/pi-tools.sh
```

## Results so far (11 November 2026, evening; recorded by the owner on the final code)

All on the real battery, owner present. Passes in `commissioning.json` are restarted by any controller change, so each is recorded again on the code that is commissioned.

| Test | Result | Evidence |
|---|---|---|
| T2 sign | **proven twice, 20 of 20 steps** | 20:46, all services stopped (`scripts/battery_sign_check.py`); 21:02, beside the running logged services. Charging −594 to −196 W, discharging +201 to +301 W, within about 1 % of each set-point; handed back cleanly both times. Not recorded (confirmation runs) |
| Runner tests | **5 passed, `read-registers` failed on the mask (111, expects 47)** | 21:20 and 21:25: hands-off-latch, default-mode, mode-write, setpoint-zero-write, release. Then on the old hand-back mode (6); rerun after the mode 0 change |
| T5 mode-change timing | **measured** | written over Modbus 21:31–21:32: the register followed within 3 s every time; 3→0 the battery went from 0 to +2.5 kW within 13 s; 0→3 it settled at 0 W within 14 s, with a brief −0.36 kW blip at 4 s |
| T9 garage CT sign | **shown** | battery as the garage load: meter +2.11 kW importing against Shelly −2.04 kW charging; mirror image when discharging (REQUIREMENTS_AMENDMENTS.md, 11 November) |
| T11 current accuracy | **shown by Shelly cross-check** | within 0.2 A at 0.3, 10, 20 and 31 A; house import within 1 % of the Octopus meter (owner amendment, 11 November) |
| Modes | 0 and 3 work at once; **6 (smart mode) fails here** with the Anker's smart-meter error | hand-back changed to mode 0 (owner amendment, 11 November) |
| Connections | **the Solarbank accepts three Modbus clients**, the meter one | runners now hold one connection of their own at a time |

## 12 November: stage A commissioned; what is left for unsupervised overnight charging

Recorded by the owner on release `35a3367`: the six runner tests, T2 (20 of 20), T9, T11, T5, and the three devices confirmed; `commission status` lists `mode` under `commissioned_classes`. **Left before unsupervised overnight charging:** T12 and T3a (they open stage B), then T8 and the supervised run (the first supervised charge), and T6. **Deferred to the summer (owner):** T10, T3b and the SSD. Stage A alone is a daytime step: without `setpoint` the controller cannot charge in the cheap window (REQUIREMENTS_AMENDMENTS.md, 12 November).

## The checklist (12 November 2026)
| Step | Status |
|---|---|
| Runner tests (six), T2 sign, T9, T11, T5, devices confirmed | done 11 to 12 November |
| T12 mode reversion, T3a PV not full | done 12 November |
| Stage A (`mode`) and stage B (`setpoint`) live | done 12 November (the owner) |
| T8 garage regulation (first live charge) | to do |
| Supervised run | to do |
| T6 comms loss (needs the −300 W runner, issue 77) | to do |
| Smart-slot rule checked against the bill | done 15 November: a listed slot is cheap only while the car charges in it (REQUIREMENTS_AMENDMENTS.md, 15 November); the 13 November 07:30 case is cleared and the whole half-hour is billed cheap once the car has drawn (REQUIREMENTS_AMENDMENTS.md, 20 November) |
| T10, T3b, the SSD | deferred to the summer |

## 0. Decide first (open)
1. **T2, the sign test:** built (GitHub issue 75), not yet run on the battery: `commission run setpoint-both-signs --confirm` (section 3). T6's −300 W
   hold still needs the same kind of runner.
2. **The gate is staged (GitHub issue 76, owner decision of 11 November).** Each write class needs only its own tests: **mode writes (stage A)** after the
   section 3 tests, T2, T9 and T11; **set-points (stage B)** after those plus T3a, T5 and T12, which are watched during stage A. The rest (T3b, T6, T8, T10,
   the supervised run) complete commissioning and are the evidence for running unattended. `sudo $S commission status` shows `sent_classes` (what would
   really be sent) and `still_needed` per stage.
3. **T10 includes a clock change.** The next one is 28 March 2027. It is needed only for complete commissioning, not for stages A or B.
4. **D6, the first live step:** stage A is the car-charge block (GitHub issue 29): the controller holds the battery at 0 W while the car charges.
5. **T3b** needs a full battery in daylight with the car charging; in winter that may take a while to happen. Not needed for stages A or B.

## 1. Before the day (any time, nothing written to the battery)
1. A few clean nights in logged mode on the release you will commission: each morning `sudo bash $T overnight`, no alert email, no `reconciliation_invalid`.
2. **Anker app** (specification section 12). **Owner confirmed on 11 November 2026 that the app settings are correct**; check again on the day, as an app update can change them:
   * Modbus TCP (Three-Party Control) on for the Solarbank and the Smart Meter Gen 2.
   * **Off:** Smart Mode, Backup Reserve, SOC Calibration, Fast Charging, Storm Guard.
   * Discharge limit (minimum level) **10 %**; charge limit **100 %**.
   * CT Monitoring: branch protection current **not** enabled; third-party PV monitoring **not** selected; "a third-party PV system has been installed" **unticked**.
   * Nothing over 100 W plugged into the Solarbank's own socket (it would fail the DC check). Owner, 11 November 2026: nothing is plugged into
     the Solarbank's own socket, and the Solarbank is on its own dedicated circuit.
3. **Router:** reserve the IP addresses of the Solarbank, the smart meter and the Shelly.
4. **Confirm the device:** `sudo $S devices confirm solarbank`
5. **Clean the database** (GitHub issue 46, the first step of G1): `sudo bash $T db-clean` (a dry run), then in the quiet hours `sudo bash $T db-clean-apply`.
6. **Have ready:** the Anker app on the phone (to switch off third-party control at once), a clamp meter, a known 2 kW load (heater or kettle), the car.

## 2. Watching only: tests that need no writes (logged mode is enough)
| Test | What to do | Pass, and record |
|---|---|---|
| T9 garage CT | Switch a known garage load on and off; watch garage power on the dashboard | Reading present; garage power rises positive with the import |
| T11 RMS current | Clamp meter on the garage feed at a few loads; compare with the dashboard's garage current | Within 0.5 A each time |
| T10 smart slot | Over several nights with "household rate verified" on: a slot starting, moving, shortening, cancelling and ending; a fetch failure; an empty list mid-session | The writes journal treats the slot as section 6.2 says (`sudo bash $T writes 60`) |

Record each with a note: `sudo $S commission pass t9-garage-ct-sign --note "what you saw"` (and `t11-rms-current`, `t10-smart-slot`).

## 3. Day 1: the runner tests (controller stopped, at the battery)
```
sudo systemctl stop solarbank-optimiser-control
```
Then, one at a time, each prints PASSED or FAILED and leaves the battery in its default mode at 0 W:
```
sudo $S commission run read-registers --confirm
```
```
sudo $S commission run hands-off-latch
```
```
sudo $S commission run mode-write --confirm
```
```
sudo $S commission run default-mode --confirm
```
```
sudo $S commission run setpoint-zero-write --confirm
```
```
sudo $S commission run release --confirm
```
`read-registers` also covers T1 (the mode mask). Then **T2, the sign test**, with the controller started again and in logged mode (it must keep reading the smart meter: about 8 minutes; the car not charging, the battery between 20 % and 90 %, and
a heater or similar on so the house uses at least 400 W). It prints each step and hands the battery back at the end, pass or fail; Ctrl-C stops it safely:
```
sudo $S commission run setpoint-both-signs --confirm
```
Then start the controller again in logged mode:
```
sudo systemctl start solarbank-optimiser-control
```
Check what is still missing at any time: `sudo $S commission status`

## 4. The limited first live step (G2), stage A: mode writes only
Needs stage A recorded: section 3 (the six runner tests and T2) and, from section 2, T9 and T11. `sudo $S commission status` must list `mode` under
`commissioned_classes`.
```
echo mode | sudo tee /etc/solarbank-cost-optimiser/write-switch
```
```
echo live | sudo tee /etc/solarbank-cost-optimiser/control-mode && sudo systemctl restart solarbank-optimiser-control
```
The controller can now switch the mode and hold 0 W, nothing more: when the car charges, it holds the battery at 0 W instead of letting it empty into the car.
Watch `sudo bash $T writes 20` for `wrote` lines, and record these (each `commission pass <test> --note "..."`); the first three open stage B:
| Test | When | Pass, and record |
|---|---|---|
| T3a PV, not full | Daylight, battery under 95 %, controller holding mode 3 at 0 W | Solar charges the battery |
| T5 mode change | When the controller changes 3 to 0 and 0 to 3 | Time until the battery's behaviour changes (note it) |
| T12 mode reversion | Change the mode in the Anker app while the controller runs | Detected, put back once, then HANDS_OFF after the limit |
| T3b PV, full | Daylight, battery 100 %, mode 3 at 0 W, car charging | Solar follows demand and supplies the car |

## 5. Stage B, set-points: a supervised charge
Needs T3a, T5 and T12 recorded as well (`setpoint` under `commissioned_classes`). Until T8 and T6 pass, charge only with the owner present.
```
printf 'mode\nsetpoint\n' | sudo tee /etc/solarbank-cost-optimiser/write-switch
```
Do this when a charge is due (the cheap window, or a daytime smart slot), with the owner present:
| Test | What to do | Pass, and record |
|---|---|---|
| T8 garage regulation | During a full-rate charge: start the car, switch the freezer, add the 2 kW load, then remove each | Garage average at or under 45 A after every step; note the peak and settling time |
| T6 comms loss | Battery at −300 W in mode 3, independent monitoring, the app ready: stop the controller for 10 minutes | Note whether charging continues (needs section 0, item 1 for the −300 W) |
| Supervised run | A whole supervised session with the car | `sudo $S commission pass supervised-run --note "..."` |

Issue 71's one-off proof (three Modbus connections) is done in this stage too: `OPERATIONS.md`, "Connection sharing". Record it in `REQUIREMENTS_AMENDMENTS.md`.

## 6. Stopping, at any point: release first, then disable
```
sudo bash $T logged-on
```
The controller that stops writes 0 W and the default mode on its way out; check the journal shows both as `wrote` (`sudo bash $T writes 6`). **Only then** empty the switch:
```
sudo truncate -s 0 /etc/solarbank-cost-optimiser/write-switch
```
If the battery behaves oddly at any time: switch off third-party control in the Anker app first, then do the above.

## 6a. Exploratory: can the battery be switched to the Anker's Dynamic Tariff mode and back? (owner, 15 November 2026)

**Why:** if the Pi fails, the battery stays in whatever mode was written last. Anker's own Dynamic Tariff (mode 7 in the register map, "dynamic pricing") would be the best mode to leave it in, if the battery accepts it over Modbus and stays in it. On 11 November the Anker's *smart* mode (6) was accepted and then fell back to self-consumption with a "smart meter missing" error, very probably because the smart meter's one connection is held by the Pi; mode 7 may or may not do the same. This test finds out. It is **not** one of the commissioning tests and nothing in the controller writes mode 7.

**What it does:** writes 0 W, then mode 7; reads the mode every 10 s for 3 minutes; then **always** writes mode 0 (self-consumption) and checks it. It passes only if the battery took mode 7, stayed in it for 3 minutes and came back to mode 0.

**Before you start:** Dynamic Tariff set up in the Anker app (the test checks the device offers it: mode mask bit 6); no smart slot or cheap window for the next 15 minutes; the Anker app open so you can see any error it shows.

1. Put the controller into logged mode, so it does not put the mode back (a live controller re-asserts its mode within a minute, and the test refuses to run beside one):
```
sudo bash $T logged-on
```
2. Run the test (about 4 minutes; it prints the mode, the battery level and the powers every 10 s):
```
sudo $S commission run dynamic-mode --confirm
```
3. Put the controller back to live, the way it was set on 12 November, and check:
```
echo live | sudo tee /etc/solarbank-cost-optimiser/control-mode && sudo systemctl restart solarbank-optimiser-control && sleep 40 && sudo bash $T overnight | sed -n '/== mode/,/== release/p'
```
It must say `live`. **If the test says it could not hand the battery back, set Self-Consumption in the Anker app at once.**

**What the result means:** PASSED: mode 7 can be written and held, so it is a candidate hand-back mode; what the battery then *does* in that mode (does it charge at the cheap rate by itself?) is a second question, to watch over a night before any change to the controller. FAILED with "did not stay in dynamic pricing": the same fault as smart mode; it may still work when the Pi is really off (the meter's connection is then free), which only a test with the Pi's services stopped can show.

## 7. Afterwards
* Close GitHub issues #63 to #73 with the evidence, one comment each.
* Running unattended (G3) waits for the SSD in place of the USB disk.
