# Solarbank 4 E5000 Pro cost optimiser: read this first

This file is for Claude Code (or another coding agent) working in this repository for its owner, the person who runs the installation. It is a copy of one person's working installation, published as a worked example: it is unofficial, not affiliated with Anker, and works only with the Anker SOLIX Solarbank 4 E5000 Pro.

## What this is
A Raspberry Pi (Debian, Python 3.11) reads an Anker Solarbank 4 E5000 Pro, an Anker smart meter and a Shelly energy meter, plans cheap-rate charging (Intelligent Octopus Go, cheap window 23:30 to 05:30 London, plus Octopus smart slots while an electric car charges) and controls the battery over Modbus. `README.md` has the picture; `CUSTOMISING.md` has recipes for changing it to suit another installation, and you should read it before editing anything.

## Hard rules
1. Never change `/etc/solarbank-cost-optimiser/control-mode` or `write-switch`, and never run anything that writes to the Solarbank. `solarbank commission run` writes to the battery: never run it. Whether the controller goes live is the owner's decision, made in person.
2. Never bypass the Pi's test gate (`SOLARBANK_ALLOW_UNTESTED`) and never edit the deploy scripts to get a deploy through.
3. Never print or commit secrets: the GitHub token, the Resend key, `rclone.conf`, the Octopus key.
4. Do not deploy unless the owner is present. Commit and push only when asked. Finish any deploy well before the cheap window starts at 23:30.
5. `reference/owner-research/SOLARBANK_CONTROL_SPEC-v1.3-2026-10-30.md` is the frozen control specification. Never edit it.
6. Never claim a tool action happened unless it did.

## Scope: this software saves money, it does not keep anyone safe
Safety belongs to the hardware and firmware, not to this software. The battery is wired to the smart meter by RS-485 and its firmware does the metering, the safe operation and the tariff locally. The garage supply limit in this installation (45 A) exists only to avoid inconvenient breaker trips. Whatever this software does, or fails to do, the system is meant to stay safe. The aim is to cut the electricity bill. So: do not build safety machinery, extra guards or defensive layers to protect the installation; keep every change simple and proportionate to a cost (a trip, a missed cheap charge), and say so if a proposal is growing beyond that.

## Where things are
* `INSTALL_WITH_CLAUDE_CODE.md`: if you are installing this for a new owner, follow it: the first-time Pi setup, the questions to ask (Part 2) and the order of work with the points where you stop for the owner (Part 3).
* `README.md`: what it does, the hardware it assumes, how the services fit together, installing on a Pi.
* `CUSTOMISING.md`: how to adapt it to another installation, with recipes. Read it first.
* `OPERATIONS.md`: running, deploying and commissioning (the gates and the tests T1 to T12). `deployment/README.md`: the two deploy routes. `scripts/pi-tools.sh`: the Pi's status, overnight, writes and before-deploy commands.
* `COMMISSIONING_PLAN.md`: the ordered commissioning checklist.
* `SOFTWARE_SPECIFICATION.md`, `DEVELOPMENT_PLAN.md`, `POST_SHADOW_PLAN.md` (the numbered decisions D1 to D19), `REQUIREMENTS_TRACE.md`, `REQUIREMENTS_AMENDMENTS.md` (dated changes) and `TESTING_REPORT_2026-11-01.md`: the design and its history. Record a dated entry in `REQUIREMENTS_AMENDMENTS.md` for any change of behaviour.
* `src/solarbank_optimiser/`: the controller is `control_loop.py`, `control_logic.py`, `action_machine.py`, `live_transport.py`, `live_gate.py`, `signals.py` and `garage.py`; the planner and providers are in `services.py`. These controller files decide what is written to the battery, so say so whenever you touch one.
* References in the documents to "GitHub issue NN" point to the original author's private tracker.

## Working here
```
python3 -m venv .venv          # needs Python 3.11 or newer
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/python -m pytest tests -q -p no:cacheprovider      # several minutes
.venv/bin/python scripts/check-docs.py                        # must end "no problems found"
```
Node 22 is needed for some dashboard tests (they skip if `node` is absent). The lint test needs `ruff` on PATH (`PATH="$PWD/.venv/bin:$PATH"`). Run the full suite before each commit.

Minute readings are in Whisper files under `/var/lib/solarbank-cost-optimiser/telemetry/`, not in SQLite. To analyse them, copy the files with `sudo cat` and decode them locally with the pinned `whisper` module.
