# Solarbank 4 E5000 Pro Cost Optimiser

A small computer (a Raspberry Pi) that decides when your Anker home battery charges, so you buy electricity at the cheap overnight rate and use it in the day, instead of leaving that decision to the Anker app.

It was built for one house: an **Anker SOLIX Solarbank 4 E5000 Pro** battery, solar panels, an electric car and the **Intelligent Octopus Go** tariff in the UK. It has run that house's battery every night since 12 November 2026.

## What it does

* **Plans each night's charge.** Every evening it looks at how much electricity your house usually uses and how sunny tomorrow will be, and works out how much to buy at the cheap rate.
* **Charges when electricity is cheap.** That means the overnight cheap window and, on Intelligent Octopus Go, the extra cheap "smart slots" Octopus gives while your car charges. A small energy meter (a Shelly) on the car charger tells it when the car really is charging.
* **Looks after the car's charge.** It stops the battery emptying itself into the car.
* **Avoids tripping the garage breaker.** It slows the battery's charging so that the battery and the car together stay under your garage supply limit.
* **Lets the Anker run the rest of the time.** Outside the cheap periods it hands the battery back to the Anker's normal mode, powering the house from the battery and the sun.
* **Shows you what it's doing** on a web page on your phone: the battery level, tonight's plan, charts of the day, estimated savings, and three buttons for the house (Normal, Away, High).
* **Keeps an eye on itself.** It emails you if something stops, and backs itself up every day.

## Is it right for you?

It could be, if all of these are true:

* Your battery is the **Anker SOLIX Solarbank 4 E5000 Pro** (part number AE103). It works with **no other battery**, Anker or otherwise.
* You have the **Anker Smart Meter Gen 2** that goes with it.
* You have, or will have fitted, a **Shelly two-channel energy meter**. The controller uses it to check every command it sends to the battery, so it is needed even without an electric car.
* You are in the **UK**, on **Intelligent Octopus Go** or on a tariff with one cheap period a day.
* You are happy to spend an afternoon or two setting up a Raspberry Pi, with an AI assistant explaining each step.

Please read this before you start:

* It is **not yet ready for a complete beginner**. The first install has not been tried from start to finish on a fresh Pi, and fitting it to a different house means changing the code. Claude does the changing, but it needs your judgement and some patience.
* It is a **personal project**, shared as a worked example. It is **unofficial and not affiliated with Anker**, it is not a product, and nobody supports it.
* It has only ever been installed on **one house**. Expect rough edges.
* It controls a real battery, so **use it at your own risk**. It starts in a watch-only mode that changes nothing, and it only takes control when you decide.
* **Safety stays with the battery and your fuse box.** The battery's own firmware and your circuit breakers keep the installation safe. This software only decides *when* to charge, to save money.

## What you need

* A **Raspberry Pi 4 or 5** with its power supply, a memory card or (better) a small SSD drive, and a network cable to your router.
* The **Anker battery and its smart meter**, set up and working in the Anker app.
* A **Shelly two-channel energy meter**, with one clamp on the battery's cable and one on the car charger's. The battery channel is needed even if you have no car. I had a **Shelly Pro EM-50** fitted in the fuse box when the Anker battery was installed, and the code is written for it. A [Shelly EM Gen4](https://shellystore.co.uk/product/shelly-em-gen4/), which you can fit yourself, using both of its channels, should be fairly easy to use instead: it reports its readings in the same way, and Claude Code should be able to make the changes needed to read it rather than the Pro EM-50. It hasn't been tried yet. On Wi-Fi it needs a good signal, because a late reading pauses charging for a moment. It connects to the mains, so if you are not confident with mains wiring, have an electrician fit it.
* A **Windows PC or a Mac** at home.
* A **Claude subscription**, for Claude Code, the AI assistant that helps you install it, and a free **GitHub account** for your own copy of the project.
* Some **free or low-cost accounts** if you want the extras: Octopus (to read your prices and smart slots), email alerts, an off-site backup and an outside "is it still running?" check. The assistant explains what each is for before you decide.

## How to install it

You don't need to know how to program. The AI assistant reads the instructions, explains each step in plain English, and asks before it does anything.

1. **Set up the Raspberry Pi.** Follow Part 1 of the [install guide](https://github.com/pnaybour/solarbank-4-e5000-pro-cost-optimiser/blob/main/INSTALL_WITH_CLAUDE_CODE.md). It takes you through it step by step.
2. **Get the Claude desktop app** for Windows or Mac from [claude.ai/download](https://claude.ai/download) and sign in.
3. **Make your own copy on GitHub.** Sign in to GitHub, come back to this page and press **Fork**. Your Pi installs from your copy, and it is where Claude keeps the changes for your house. A fork is public; the install guide says how to make a private copy instead.
4. **Open it in Claude.** Make an empty folder on your computer. In the Claude app, open the **Code** tab, choose that folder and say: *Clone my copy, github.com/YOUR-GITHUB-NAME/solarbank-4-e5000-pro-cost-optimiser, into this folder.*
5. **Tell Claude what you want.** Copy this in:

   > Read README.md, CLAUDE.md, CUSTOMISING.md and INSTALL_WITH_CLAUDE_CODE.md. I have set up my Raspberry Pi as in Part 1. Ask me the questions in Part 2 one group at a time, and do not run anything that changes the Pi until I have answered them. Follow Part 3, stop at every point marked owner, and never ask me to paste a key into this chat.

6. **Answer its questions.** It asks about your battery, your car, your tariff, roughly where you live (the nearest town is enough) and your solar panels. Then it changes the project to suit your house.
7. **Approve each step.** Claude shows you every command before running it. Whenever a password or key is needed, it tells you where to type it yourself. **Never type a password or key into the chat.**
8. **Let it watch first.** For the first week it only watches and records what it would do. When you are happy, **you** decide when it takes control of the battery.

If you want to change something later, the [customising guide](https://github.com/pnaybour/solarbank-4-e5000-pro-cost-optimiser/blob/main/CUSTOMISING.md) has recipes Claude can follow.

---

## Technical details

Everything below is for the curious, and for the AI assistant.

**Set your location before using it** (the sunset and weather calculations use latitude and longitude): see the first recipe in `CUSTOMISING.md`.

In these documents "the owner" means whoever runs the installation, and references such as "GitHub issue 79" point to the original author's private tracker.

## Status

Version 0.5.37, live on the original installation since 12 November 2026. A few of its commissioning tests are still to do (`COMMISSIONING_PLAN.md`).

## How it fits together

The controller talks to the battery and the meter over Modbus TCP. It charges in the cheap window (23:30 to 05:30) and in smart slots while the car charges, keeps the garage feed under 45 A, and records every reading, plan and command it sends.

```
 Anker Solarbank ──Modbus──┐
 Anker smart meter ─Modbus─┼──> collector ──> readings (Whisper files) ──> web page
 Shelly (battery, car) ────┘        │
                                    └──> controller ──Modbus──> Solarbank (mode, charge rate)
 Octopus API, weather ──> providers ──> planner (overnight target) ──┘
```

Five services run on the Pi (`deployment/`): the **collector** reads the devices, the **providers** fetch the tariff, smart slots and weather, the **planner** sets the overnight target, the **controller** sends the commands, and the **web** service shows the page. Timers run the alerts, the off-site backup, the heartbeat and storage housekeeping.

The code is in `src/solarbank_optimiser/`. The controller's decisions are explicit state machines (`action_machine.py`, `machines.py`, built on the `transitions` library); the command path is `live_transport.py`, and it writes only when two root-owned files on the Pi allow it: `control-mode` says `live`, and `write-switch` lists what may be sent.

## Hardware and tariff it assumes

| Part | Here |
|---|---|
| Battery | Anker SOLIX Solarbank 4 E5000 Pro, two 5 kWh units (10 kWh), Modbus TCP |
| Meter | Anker smart meter (grid connection and garage feed), Modbus TCP, one client only |
| Car and battery circuits | Shelly Pro EM-50, HTTP (`em1:0` the Solarbank's AC circuit, `em1:1` the car charger); a Shelly EM Gen4 should adapt easily, untried |
| Solar | 2 kWp: two panels on a wall, two on the ground, all facing south |
| Tariff | Intelligent Octopus Go: cheap 23:30 to 05:30 and in smart slots in which the car charges |
| Computer | Raspberry Pi 4 (2 GB), Debian, Python 3.11 |

## Getting started (development)

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/python -m pytest tests -q -p no:cacheprovider
.venv/bin/python scripts/check-docs.py
```

Python 3.11 or newer. The suite takes several minutes. Node 22 is needed for one dashboard test, which is skipped without it. Running and deploying are in [OPERATIONS.md](OPERATIONS.md) and [deployment/README.md](deployment/README.md).

## Installing on a Raspberry Pi

These steps are gathered from the install scripts and [OPERATIONS.md](OPERATIONS.md). This installation grew release by release, so the steps have **not yet been tried in one go on a fresh Pi**. Read each script before you run it.

### With an AI coding agent

The plain-English steps at the top of this page are the recommended route. Technically, the agent (Claude Code, or OpenAI's Codex) runs on your PC or Mac and works on the Pi over SSH, using an SSH key set up as in Part 1 of `INSTALL_WITH_CLAUDE_CODE.md`. Codex and other agents read `AGENTS.md`, which sends them to the same rules in `CLAUDE.md`. Whichever agent you use, you type every key and password yourself on the Pi, you approve each command, and only you switch control on.

### 1. Prepare the equipment in the Anker app

The controller drives the battery through Anker's **third-party control** (Modbus TCP). In the Anker app:

1. Turn on **Three-Party Control** (Modbus TCP) for the Solarbank and for the Smart Meter Gen 2.
2. Turn off Backup Reserve, SOC Calibration, Fast Charging and Storm Guard. Any of them can override third-party control and force a full-power grid charge.
3. Set the discharge limit (minimum level), for example 10 %, and the charge limit to 100 %.
4. Leave **branch protection current** and **third-party PV monitoring** off (Settings, CT Monitoring Settings).
5. In your router, reserve fixed addresses for the Solarbank, the meter and the Shelly.

Each Anker device accepts only a few Modbus clients (the meter only one). Do not run another Modbus client, such as the Home Assistant Anker integration, against the same devices.

To take control back at any time, turn third-party control off in the Anker app. The battery then returns to the mode last set in the app.

### 2. Accounts and keys

What each account and key is for, how to get it and where it goes on the Pi is in `INSTALL_WITH_CLAUDE_CODE.md` (Part 2, group 6). The weather forecast (Open-Meteo) needs no key. Never commit the files that hold the keys: the alerts, backup and heartbeat timers stay off until their file exists.

### 3. Install

**The first install is done by hand.** Follow "First install" at the end of `INSTALL_WITH_CLAUDE_CODE.md`. `scripts/deploy-on-pi.sh` cannot do a first install as it stands: it expects a database that only the installed software can create, and its switch step backs up with, and stops the services of, a release that does not exist yet on a new Pi.

**Updates**, once the first install is running. On the Pi, check it is a good moment (`sudo bash /opt/solarbank-cost-optimiser/current/scripts/pi-tools.sh before-deploy`), then:

```
SOLARBANK_REPO=YOUR-GITHUB-NAME/solarbank-4-e5000-pro-cost-optimiser sh /opt/solarbank-cost-optimiser/current/scripts/deploy-on-pi.sh <full-commit-id>
```

`SOLARBANK_REPO` names your own copy as owner/name; without it the script tries a placeholder and fails. The script needs a read-only GitHub token (`~/.solarbank-deploy-token`, mode 600, or `read -rs GITHUB_TOKEN; export GITHUB_TOKEN`). It downloads the code, builds the release, runs the whole test suite on the Pi and switches over only if every test passes (details in [deployment/README.md](deployment/README.md)).

The device addresses live in `/etc/solarbank-cost-optimiser/devices.conf` (edit it with `sudoedit`, then restart the services). The dashboard is at `http://solarbank-pi.local:8091/` from your home network.

### 4. From watching to controlling

A new installation starts in **shadow** mode: it reads, plans and shows what it would do, and writes nothing. Two root-owned files in `/etc/solarbank-cost-optimiser/` decide when it may send commands:

* `control-mode`: `shadow`, `logged` (the controller runs and records every write it would make, without sending) or `live`.
* `write-switch`: the kinds of command allowed, one per line: `mode`, then `setpoint`. Empty means nothing is sent.

Run in `logged` mode for a few nights and read the overnight report (`sudo bash scripts/pi-tools.sh overnight`). Then work through the commissioning tests (OPERATIONS.md, tests T1 to T12; this installation's checklist is `COMMISSIONING_PLAN.md`) before setting `live`.

## Repository layout

| Folder | What is in it |
|---|---|
| `src/solarbank_optimiser/` | The application |
| `tests/` | The test suite |
| `scripts/` | Deploy, status and report scripts for the Pi (`pi-tools.sh` is the everyday one) |
| `deployment/` | The systemd units, the nginx site and the deploy guide |
| `reference/` | Device register maps and the owner's control specification (frozen) |

## Documents

Current:

* [OPERATIONS.md](OPERATIONS.md): running, deploying, alerts, backups and troubleshooting. It grew release by release, so read the newest sections first.
* [REQUIREMENTS_AMENDMENTS.md](REQUIREMENTS_AMENDMENTS.md): dated changes to what the software must do. The newest entry wins.
* [SOFTWARE_SPECIFICATION.md](SOFTWARE_SPECIFICATION.md): the product requirements (version 1.8), as amended by the file above.
* `COMMISSIONING_PLAN.md`: the commissioning checklist for this installation.
* `CUSTOMISING.md`: how to adapt it to another house (garage limit, tariffs without an API, no Shelly, battery size), written for you and an AI coding agent.
* `CLAUDE.md`: the working rules for the AI coding assistant (Claude Code) that helps maintain it.

Historical, kept for the record: [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md), [POST_SHADOW_PLAN.md](POST_SHADOW_PLAN.md) (the plan before going live, with decisions D1 to D19), [TESTING_REPORT_2026-11-01.md](TESTING_REPORT_2026-11-01.md) and `REQUIREMENTS_TRACE.md`. Some of them cite files that were never copied into this repository (a `reports/` folder and a real-time acquisition plan); those references are historical. The same goes for `HANDOVER.md`, `CODE_REUSE.md`, `PROJECT_BRIEF.md`, `LOCAL_SESSION_BRIEF.md` and the `history/` and `development/` folders (the author's hand-over notes, code-reuse notes and October readings), which are not in this public copy.

## Names used in the code

| In the code | What it is |
|---|---|
| `em1:0`, `solarbank_ac` | The Solarbank's AC circuit on the Shelly: positive is output to the house, negative is charging |
| `em1:1`, `car` | The car charger on the Shelly |
| `main`, `grid_ct_power` | The meter's main CT: the house's grid connection |
| `sub`, `garage_power`, `garage_current_a` | The meter's second CT: the whole garage feed (battery and car) |
| Register 10064 | The battery's operating mode: 0 self-consumption, 3 third-party control (used while charging), 6 smart, 7 dynamic tariff |
| Register 10071 | The battery's power set-point in third-party mode: negative charges |

## Licence and third-party code

* This project is released under the MIT licence (see [LICENSE](LICENSE)).
* `reference/anker-solix-official/` holds a copy of Anker's official Home Assistant integration, which is MIT licensed, Copyright (c) 2026 Anker Innovations (its licence is in that folder). It is kept for the Solarbank 4 E5000 Pro's register map, and `src/solarbank_optimiser/devices/anker_vendor.py` adapts parts of it into pure functions.
* The pinned libraries in `requirements.lock` and `solver-requirements.lock` (including EMHASS, pvlib, scikit-learn, FastAPI, pymodbus, transitions and whisper) keep their own licences.
