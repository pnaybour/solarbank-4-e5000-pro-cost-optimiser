# Adapting the optimiser to another installation

This guide is for you **and for the AI coding agent** (Claude Code or Codex) helping you change the optimiser for a different house. Give it to the agent with the change you want, for example:

> Read CLAUDE.md, README.md and CUSTOMISING.md. I want to change the garage limit to 32 A. Follow the "How to make any change" steps in CUSTOMISING.md and tell me your plan before you edit anything.

The recipes below say where each feature lives and what has to change. They were written from the code as it stands (November 2026); none of them has been tried on another installation, so expect the agent to find details this guide misses.

## How to make any change

1. **Read first.** The agent reads the files named in the recipe and the tests that cover them, and explains its plan in plain words before editing.
2. **Tests with the change.** Every change comes with tests that show the new behaviour, and the whole suite must pass: `.venv/bin/python -m pytest tests -q -p no:cacheprovider`, then `.venv/bin/python scripts/check-docs.py`.
3. **Controller files are the riskiest.** These decide what is written to the battery: `control_loop.py`, `control_logic.py`, `action_machine.py`, `live_transport.py`, `live_gate.py`, `signals.py`, `garage.py` (all in `src/solarbank_optimiser/`). The agent should say when it touches one.
4. **Record the decision.** Add a dated entry to `REQUIREMENTS_AMENDMENTS.md` saying what changed and why.
5. **Deploy while you are there**, never late in the evening, and watch the next night in **logged** mode before going back to **live**: `logged` records every write the controller would make without sending it, and `sudo bash scripts/pi-tools.sh overnight` shows them.
6. **The agent never** changes `/etc/solarbank-cost-optimiser/control-mode` or `write-switch`, never writes to the battery itself, and never skips the test gate to get a deploy through. Those are yours.

Safety belongs to the battery's firmware, its wired link to the meter and your circuit breakers, not to this software (see `CLAUDE.md`). Keep changes simple and proportionate to what they save.

## Recipes

### Set your location

* **Why:** sunset and the weather forecast use latitude and longitude. The copy in this repository uses central London (51.5074, -0.1278) as an example, so the solar forecast is wrong for anywhere else.
* **Where:** `LATITUDE, LONGITUDE` in `src/solarbank_optimiser/daylight.py`, the four places in `src/solarbank_optimiser/providers.py` (the weather request, a plausibility check on the reply, and the solar-position calculation), and the same numbers in `tests/test_pv_adjust.py`. The weather fixture `tests/fixtures/weather-two-plane.json` carries a latitude and longitude too (51.51, -0.13), and `tests/test_demand_learning.py` expects London's sunset times, so change those to match. Search for `51.5074` to find the first group.
* **Size:** small. Change every occurrence to your own coordinates and run the tests.

### Your home network range

* **Why:** the dashboard only answers to computers on `192.168.1.0/24` and a few other ranges. On any other home range you are locked out until yours is added. Add yours next to the existing one in each place; do not replace it, so the tests keep working.
* **Where:** `allow 192.168.1.0/24;` in `deployment/solarbank-optimiser.nginx`; `IPAddressAllow=192.168.1.0/24` in `deployment/solarbank-optimiser-web.service`; and the list of ranges that includes `'192.168.1.0/24'` in `src/solarbank_optimiser/web/app.py`. The collector and control units allow `192.168.0.0/16`: if your home range is `10.x.x.x` or `172.16.x.x`, add it to `IPAddressAllow=` in `solarbank-optimiser-collector.service` and `solarbank-optimiser-control.service` as well. An IPv6 prefix goes in `/etc/solarbank-cost-optimiser/lan-prefixes` (see OPERATIONS.md), not in the code.
* **Size:** small. Deploy again afterwards.

### Your solar panels

* **Why:** the solar forecast models the panels. This repository's version assumes two planes of 1 kWp each, facing south, tilted 80 and 20 degrees from flat.
* **Where:** `solar_forecast` in `src/solarbank_optimiser/providers.py`: the loop `for tilt in (80, 20)` (the azimuth is fixed at 180, south, in pvlib's convention; the `1000` passed to `pvwatts_dc` is each plane's peak power in watts), the `planes` list in the record it returns, and the model name `two-plane-pvlib-v1`. `services.py` also refers to the planes (search for `kwp`), and `tests/test_phase3.py` with `tests/fixtures/weather-two-plane.json` test it.
* **Size:** medium. Making the planes a list of (kWp, tilt, azimuth) near the top of the file is the tidy change. Expect to adjust the test data.

### Change the garage (feed) current limit

* **Where:** `GARAGE_LIMIT_A = 45.0` in `garage.py`. The battery's charging power is cut so that the battery plus everything else on the feed stays under it.
* **What else:** the tests in `tests/test_garage.py` use the 45 A figure; the README and the specification mention it.
* **Size:** small. One constant and its tests. A setting on the Settings page instead of a constant is a bigger change (the controller would read it from the plan or a file in `/etc/solarbank-cost-optimiser/`).

### Remove the garage limit (no shared feed, or no second CT on the meter)

* **Why it is not just a big number:** the controller treats a missing garage reading as "current unknown" and **stops charging** until the reading returns (`control_logic.Supervisor._garage`). Two validity checks (`voltage_invalid` and `Reconciliation` in `garage.py`) also use the meter's second CT.
* **What has to change:** an explicit "no garage limit" option (a root-owned file in `/etc/solarbank-cost-optimiser/`, like `devices.conf`) that makes `_garage` skip the regulator and the two checks and charge at the battery's own maximum (`P_MAX_W` in `garage.py`).
* **Size:** medium, in a controller file. Tests must cover both settings.

### Battery size and charging power

* **Planner:** `BatteryModel` in `planning.py` (`capacity_kwh = 10`, `ac_limit_kw = 2.5`, the efficiencies).
* **Controller:** `P_MAX_W = 2500.0` in `garage.py` is the highest charging set-point it will send.
* **Size:** small; keep the two in step. An extra BP5000 module would mean `capacity_kwh = 15`.

### A tariff without an API (any fixed cheap window)

* **No code needed for one cheap window a day.** Leave the Octopus connection off and type the prices and the window on the Settings page ("fallback tariff": day price, cheap price, start and end time). The planner publishes that window and the controller follows it (`control_logic`, `_offpeak_window`). Before the first plan the controller assumes 23:30 to 05:30 (`OFFPEAK_START`, `OFFPEAK_END` in `signals.py`); change those if your window differs.
* **Smart-charging slots** exist only with Intelligent Octopus and the Octopus account connection; without them nothing changes.
* **Two or more cheap windows a day** (for example a tariff with an afternoon window as well as a night one): the fallback tariff (`FallbackTariff` in `settings.py`) and `signals.offpeak` hold **one** window. They would need a list of windows, in the planner and the controller. Size: medium, touches a controller file.
* **Prices that change every half-hour** (an Agile-style tariff): the planner would need the price series instead of two fixed prices, and a rule for which half-hours to charge in. Size: large; a new feature rather than a setting.

### Working without the Shelly

The Shelly gives the controller two things, and each needs a replacement:

| From the Shelly | Used for | Possible replacement |
|---|---|---|
| The car charger's power (`car`, channel `em1:1`) | Knowing the car is charging: smart slots start only then, and the battery is held so it does not discharge into the car (`EvSignal`, thresholds in `ev_policy.py`) | **No car:** an explicit "no car" option that keeps the car signal off. **A car on its own circuit:** the charger's own API, or the Octopus dispatch alone (less accurate) |
| The Solarbank's AC power and voltage (`solarbank_ac`) | The garage limit (what the battery itself draws) and the reconciliation check | The Solarbank's own AC power register over Modbus, and the meter's voltage |

* **Why it is not just switching it off:** a Shelly reading older than 2.5 s counts as unknown (`MAX_AGE_S` in `control_logic.py`), and unknown car data makes the controller hold the battery rather than guess.
* **Where:** `control_logic.py` (the Supervisor), `shelly_channels.py`, `devices_config.py` (the `SOLARBANK_SHELLY_HOST` address), the collector and the dashboard.
* **Size:** large, mostly in controller files. Do it with "no car" first if that applies: it is the simpler half.

### No car

* Combine "no car" from the Shelly recipe with leaving the Octopus account connection off. Smart slots then never start, and the night window works as before.

### Which mode the battery is handed back to

* After each charge the controller hands the battery back in mode 0 (self-consumption). The battery also offers mode 7 (Anker's own dynamic-tariff mode), which would keep it doing something sensible if the Pi failed. Test it first with the exploratory `dynamic-mode` commissioning test (`COMMISSIONING_PLAN.md`, section 6a) before changing the hand-back mode; the change touches `action_machine.py` and `live_transport.py`.

### A different Anker battery or meter

* The register addresses come from Anker's official Home Assistant integration (`reference/anker-solix-official/`). Another model may use different registers or offer different modes. The agent should check the model against that reference before anything else, and run the read-only `solarbank probe-settings` command to see what the device reports. Nothing should be written to a new model until its registers are confirmed.
