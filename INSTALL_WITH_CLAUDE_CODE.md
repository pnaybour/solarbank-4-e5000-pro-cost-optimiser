# Installing with Claude Code: set up the Pi, then answer the questions

This guide has two jobs: it takes you through setting up a Raspberry Pi for the first time (Part 1), and it gives Claude Code the list of questions to ask you before it installs anything, with the reason for each and where each answer ends up (Part 2). Part 3 is the order of work and the points where Claude Code must stop and hand over to you.

Read `README.md` first. This project is unofficial, is not affiliated with Anker, and works **only with the Anker SOLIX Solarbank 4 E5000 Pro** (part number AE103). The install steps have **not yet been tried end to end on a fresh Pi**, so expect to meet something this guide misses, and tell Claude Code to explain any error before it tries a fix.

## Part 1. Setting up the Raspberry Pi for the first time

Do this before Claude Code touches anything. Claude Code can talk you through it, but you do it with your own hands.

**What you need**

* A Raspberry Pi 4 or 5 with its official power supply and a case (the author's is a Pi 4 with 2 GB).
* Storage. The system writes readings every second and backs up daily. The author's Pi boots from a USB SSD; a good SD card works to start but wears out sooner, so use an SSD if you can. The deploy check wants at least 20 GB free.
* A network cable from the Pi to your router. Readings that are more than a few seconds old stop the battery charging, so a wired connection is best.
* A Windows PC or a Mac on the same home network. Claude Code runs there, not on the Pi, and works on the Pi over SSH (see the README for the desktop app).

**Steps**

1. On your computer, install **Raspberry Pi Imager** from raspberrypi.com/software.
2. In Imager choose your Pi model, then the operating system **Raspberry Pi OS Lite (64-bit)** (under "Raspberry Pi OS (other)"; it has no desktop), then your SD card or SSD.
3. When Imager offers to customise the settings, set:
   * **Hostname:** `solarbank-pi`. The dashboard answers to `solarbank-pi.local` and `solarbank-pi` by default. A different name needs `--allowed-host` added to the web service.
   * **Username and password:** username `pi` (the commands in these documents use `pi@solarbank-pi.local`) and a password of your own.
   * **Wi-Fi:** only if you cannot use a cable.
   * **Time zone** `Europe/London` and keyboard layout `gb`. The software is written for UK time and the UK tariff clock.
   * **Services:** turn on SSH. If you already have an SSH key on your computer, paste the public key in; otherwise use the password for now.
4. Write the card or SSD, put it in the Pi, connect the cable and the power, and wait two or three minutes for the first start.
5. From a terminal (PowerShell on Windows 10 and 11, Terminal on a Mac) connect:
   ```
   ssh pi@solarbank-pi.local
   ```
   Accept the fingerprint and type your password. If `.local` names do not work on your computer, use the Pi's address from your router's list of devices.
6. Update it, then reconnect after it restarts:
   ```
   sudo apt update && sudo apt full-upgrade -y && sudo reboot
   ```
7. Check three things:
   ```
   python3 --version
   timedatectl | grep "Time zone"
   df -h /
   ```
   Python must be 3.11 or newer, the time zone must be `Europe/London`, and you need at least 20 GB free.
8. Install what the install steps need (the web server, the backup tool and the build tools):
   ```
   sudo apt install -y nginx rclone git curl python3-venv
   ```
9. In your **router**, reserve fixed addresses (a "DHCP reservation") for the Pi, the Solarbank, the smart meter and the Shelly, and write the four addresses down. Claude Code will ask for them.
10. Set up an SSH key, so Claude Code can run commands on the Pi without your password. If you pasted a public key into Imager in step 3, skip to the check. In the same terminal on your computer:
    * **Mac:** run `ssh-keygen -t ed25519` and press Enter at each question, then run `ssh-copy-id pi@solarbank-pi.local` and type the Pi's password once.
    * **Windows (PowerShell):** run `ssh-keygen -t ed25519` and press Enter at each question, then run this and type the Pi's password once:
      ```
      type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh pi@solarbank-pi.local "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
      ```
    * **Check:** `ssh pi@solarbank-pi.local` should now log you in without asking for a password. Type `exit` to leave.
11. Optional: remote access with Tailscale. Install it from tailscale.com's Linux instructions. The dashboard accepts Tailscale addresses, but the web server's name list in `deployment/solarbank-optimiser.nginx` holds a placeholder (`your-tailnet.ts.net`) that must be changed to your own. **Never forward a port from your router to the Pi.**

Now make your own copy of the project on GitHub (press **Fork** on the project's page), make an empty folder on your computer, open it in the Claude app's **Code** tab, ask Claude to clone your copy into it, and then say:

> Read README.md, CLAUDE.md, CUSTOMISING.md and INSTALL_WITH_CLAUDE_CODE.md. I have set up my Raspberry Pi as in Part 1. Ask me the questions in Part 2 one group at a time, and do not run anything that changes the Pi until I have answered them. Follow Part 3, stop at every point marked **owner**, and never ask me to paste a key into this chat.

## Part 2. The questions Claude Code asks

**For Claude Code:** ask these in plain language, one group at a time. Say why you are asking. Write the answers (never any key or password) to `install-answers.md` in the repository, which is ignored by git, so that later steps and later sessions can use them. If an answer is "I don't know", say how to find out and wait.

**Secrets.** Never ask for a key, a token or a password in the chat, and never put one in a file in the repository. Show the owner the command, and let them type the secret in their own SSH window on the Pi at the prompt the command gives. Anything typed into the chat is not private.

### Group 1. Your equipment

| # | Ask | Why | Where the answer goes |
|---|---|---|---|
| 1 | "Which battery do you have, exactly? Please read the model on the label." | This software works only with the Anker SOLIX Solarbank 4 E5000 Pro (part number AE103); its Modbus register map is pinned to that model. **If it is anything else, stop and say so plainly.** Nothing else has been tried. | Nothing; this is the gate. |
| 2 | "How many 5 kWh battery units are in it: one or two?" | The planner needs the capacity (10 kWh here) to work out how much to buy. | `BatteryModel` in `planning.py` and `P_MAX_W` in `garage.py` (see CUSTOMISING.md, "Battery size and charging power"). |
| 3 | "Do you have the Anker Smart Meter Gen 2, and is its second clamp (labelled PV/ESS) on the feed to your garage or car charger?" | The controller reads the whole garage feed (battery and car together) from that clamp and trims the charge rate to stay under your limit. Without it the limit cannot work. | The garage-limit recipes in CUSTOMISING.md ("Remove the garage limit" if there is no second clamp). |
| 4 | "Have you turned on Three-Party Control (Modbus TCP) for the battery and the meter in the Anker app, and turned off Backup Reserve, SOC Calibration, Fast Charging and Storm Guard?" | The controller drives the battery through Anker's third-party control, and those four features can override it. **Claude Code cannot do this for you.** The README, section 1, lists the exact settings. | Nothing in the code; **owner** does it in the app. |
| 5 | "What are the addresses of the battery, the meter and the Shelly, and have you reserved them in your router?" | They go in a file before the first deploy. If the file is missing the first deploy writes placeholders (`CHANGE-ME-...`) that will not work. | `/etc/solarbank-cost-optimiser/devices.conf` (one `SOLARBANK_HOST=`, `SOLARBANK_METER_HOST=` and `SOLARBANK_SHELLY_HOST=` line each), written **before** the first deploy. |

### Group 2. The car

| # | Ask | Why | Where the answer goes |
|---|---|---|---|
| 6 | "Which Shelly two-channel energy meter do you have, and are its clamps on the battery's cable and the car charger's? Do you have an electric car on Intelligent Octopus Go?" (The battery channel is needed even without a car.) The original installation uses a Shelly Pro EM-50, fitted with the battery (channel 0 on the battery's AC cable, channel 1 on the car charger's). A Shelly EM Gen4 with both channels used the same way should need little or no change: check what it reports, adapt the reader in `src/solarbank_optimiser/devices/shelly.py` if needed (with tests), and check readings arrive within 2.5 seconds, especially on Wi-Fi. | The Shelly tells the controller when the car is really charging, and its battery channel is how the controller checks each charge command took effect. Octopus smart slots then start the battery charging too, only while the car draws, and the battery is held so it does not empty into the car. | The Shelly address in `devices.conf`. No car, or a single-channel Shelly, is a bigger change: see CUSTOMISING.md ("No car", "Working without the Shelly"). |

### Group 3. Electricity

| # | Ask | Why | Where the answer goes |
|---|---|---|---|
| 7 | "Which tariff are you on? Intelligent Octopus Go, or something else?" | Intelligent Octopus Go gives a cheap window of 23:30 to 05:30 and smart slots. One fixed cheap window a day works with no code change. Two or more windows, or prices that change every half-hour, are a large change. | Intelligent Octopus Go: the Octopus key (below). Fixed window: the "fallback tariff" on the Settings page. |
| 8 | "What are your day and cheap prices in pence per kWh, and when does your cheap window start and end?" | The planner prices the night's charge against the day rate. | The Settings page, "Electricity tariff" (fallback tariff). |
| 9 | "What current limit do you want for the garage feed, in amps?" | It keeps the battery and the car together under a limit so the breaker does not trip. **It is a nuisance-trip limit, not a safety device**: this software provides no protection, and if you are unsure what your supply and cable can carry, ask an electrician. | `GARAGE_LIMIT_A` in `garage.py` (CUSTOMISING.md, "Change the garage (feed) current limit"). |

### Group 4. Where you are and your solar panels

| # | Ask | Why | Where the answer goes |
|---|---|---|---|
| 10 | "Which town are you near? I only need latitude and longitude to about two decimal places, so please do not give your address." The software is written for the UK (Europe/London time): if you are elsewhere, say so and expect larger changes. | Sunset and the weather forecast use your latitude and longitude. The copy in this repository uses central London as an example, so the solar forecast is wrong for anywhere else. | The recipe "Set your location" in CUSTOMISING.md (several files, plus the test data). |
| 11 | "Describe your solar panels: the total power in kWp, and for each group of panels the tilt from flat and the compass direction." | The forecast models the panels. This repository's version is fixed at two planes of 1 kWp each (tilts 80 and 20, facing south). | The recipe "Your solar panels" in CUSTOMISING.md (`providers.py` and its test data). |

### Group 5. Your network and your Pi

| # | Ask | Why | Where the answer goes |
|---|---|---|---|
| 12 | "What is your home network's address range, for example 192.168.0.x or 192.168.1.x?" | The dashboard only answers to computers in `192.168.1.0/24` and a few other ranges. On any other home range you will be locked out until your range is added. | The recipe "Your home network range" in CUSTOMISING.md. |
| 13 | "What is the Pi's name, which model and how much memory, and does it start from an SD card or an SSD?" | The name is part of the dashboard address (`solarbank-pi` is the default). Memory and storage tell us whether the solver and the backups will fit. | Nothing in the code unless the name differs (`--allowed-host`). |
| 14 | "Do you want to see the dashboard when you are away from home?" | If yes, use Tailscale (Part 1, step 11). Never open a router port to the Pi. | `deployment/solarbank-optimiser.nginx` (your tailnet name). |
| 15 | "Who should be able to open the dashboard?" | As installed, any phone or computer on your home network (and on Tailscale, if you add it) can open the dashboard and change its settings with no password, because the web service runs with `--trusted-network`. It cannot switch on live control: that needs the two root-owned files. Anyone you let on your Wi-Fi can see it. If that matters, Claude can remove `--trusted-network` from `deployment/solarbank-optimiser-web.service`, and the page then asks for the password (user `solarbank`). | `dashboard-password` in the data folder. The first install makes a random one; the owner reads it on the Pi if they need it. |

### Group 6. Accounts and keys

Ask which of these the owner wants, say what each is for, and **do not collect any secret in the chat**. Each key goes in on the Pi, typed by the owner. The table below is the whole list.

| What | Needed? | Why | How to get it | Where it goes |
|---|---|---|---|---|
| **Octopus API key** | Only for Intelligent Octopus Go. Without it, use the fixed cheap window. | The planner reads your prices, your cheap window and the smart-charging slots (read only). | Octopus account, Personal details, **API access** (the key starts `sk_live_`). Your account number is on the same page. You do not need the charger's device ID: after you connect, the Settings page lists your linked chargers. | `OCTOPUS_API_KEY=` in `/var/lib/solarbank-cost-optimiser/.env` (mode 600), typed by the owner over SSH: `sudo -u solarbank-optimiser nano /var/lib/solarbank-cost-optimiser/.env`. The website refuses a new key over the home network's plain HTTP. Then use "Connect Octopus (read only)" on the Settings page. |
| **Resend API key**, and a sending domain you have verified | Optional but recommended. | Emails you if a service stops or the readings go stale. A stopped controller means a missed cheap charge. | resend.com: create an account, add and verify a domain you control, then create an API key with sending access. | `resend-key` and `alert.json` in the data folder (OPERATIONS.md, alerts). |
| **Healthchecks.io** ping addresses | Optional but recommended. | A heartbeat from outside your house: it tells you if the whole Pi has gone quiet, which no email from the Pi itself can. | healthchecks.io: create an account and a check for each of the Pi, the optimiser and the backup. | `healthchecks.json` in the data folder (OPERATIONS.md, independent heartbeat). **The ping addresses are secrets.** |
| **Cloudflare R2** (or another rclone remote) | Optional but recommended. | A daily copy of the database off the Pi, in case the SD card or SSD fails. | Cloudflare dashboard: create a bucket, then an R2 API token with Object Read and Write limited to that bucket. Note the access key, the secret and your account's endpoint address. Check Cloudflare's current pricing. | `rclone.conf` in the data folder (OPERATIONS.md, "Cloudflare R2 instead of Google Drive"). |
| **GitHub token** (read only) | Needed by `scripts/deploy-on-pi.sh`, even for a public repository. | The deploy script downloads your code from GitHub with it. | GitHub, Settings, Developer settings, fine-grained personal access token: your own copy of the repository only, permission **Contents: Read-only**. | `~/.solarbank-deploy-token` for the `pi` user, mode 600. Updates also need `SOLARBANK_REPO` set to your copy (README, "Install"). The first install of a public copy needs no token. The other deploy route, `scripts/deploy-pi-shadow.sh` from a Mac, needs no token (`deployment/README.md`). |
| **Open-Meteo** weather | **No key.** | The solar and temperature forecast. | Nothing to do. | Nothing. |

The alerts, backup and heartbeat timers stay off until their file exists, so the owner can add them one at a time.

### Group 7. Control

| # | Ask | Why |
|---|---|---|
| 16 | "Are you happy for it to start in shadow mode, then move to logged mode for a few nights, and for you alone to decide when it goes live?" | A new installation reads, plans and writes nothing. `logged` records every write the controller would make without sending it. Going live needs the owner at the Pi after the commissioning tests. **Claude Code never changes `control-mode` or `write-switch` and never writes to the battery.** |

## Part 3. Order of work, and where Claude Code stops

1. Read the documents and ask the questions in Part 2. **Stop** if the battery is not the E5000 Pro.
2. **Owner:** make your own copy on GitHub: **Fork** for a public copy, or, to keep your changes private, GitHub's **Import repository** (github.com/new/import) with this project's address, as a private repository. A fork is public, so the changes Claude makes for your house (your town-level location, your panels, your home network range) would be visible. Clone your copy onto your computer.
3. Make the changes the answers need, using the recipes in CUSTOMISING.md (location, solar panels, home network range, and battery size or garage limit if they differ). Each change comes with tests; run the whole suite on your computer; add a dated entry to `REQUIREMENTS_AMENDMENTS.md`. **Owner:** reviews and commits.
4. **Owner:** the Anker app settings (question 4) and the router reservations (Part 1, step 9).
5. Do the **first install** below. It replaces the first deploy, which cannot work on a new Pi.
6. For every later update, use `scripts/deploy-on-pi.sh` with `SOLARBANK_REPO` set to your copy (README, Technical details, "Install"). Never skip its test gate. **Owner:** provides the GitHub token, typed on the Pi.
7. **Owner:** adds the optional keys one at a time (Group 6), each typed on the Pi. After each one Claude Code checks that the matching timer works.
8. Open the dashboard at `http://solarbank-pi.local:8091/`. The planner needs about a day of readings before it publishes a plan.
9. **Owner:** sets `control-mode` to `logged` and lets it run for a few nights. Read the overnight report together: `sudo bash scripts/pi-tools.sh overnight`.
10. Work through the commissioning tests (OPERATIONS.md, T1 to T12). Going live is **the owner's decision**, made at the Pi, in daylight and well before the cheap window starts.

### First install: how to avoid the deploy script's first-install problem

**For Claude Code.** `scripts/deploy-on-pi.sh` is for updates. On a new Pi it fails, for three reasons: it refuses to start without a database (`controller.sqlite3`) that only the installed software can create; its switch step, `switch-release.sh`, backs up the database with the *previous* release's program (`/opt/solarbank-cost-optimiser/current/.venv/bin/solarbank`), which does not exist yet; and it then stops services that are not installed yet, which `systemctl` treats as an error. Do **not** edit the deploy scripts to get round this, and do not skip the test gate. Do the first install by hand, as below, explaining each step to the owner and waiting for their approval. Everything the switch step would do after its backup and its stop is done by `scripts/activate-pi-shadow.sh`, and on a new Pi there is nothing to back up and nothing to stop. These steps have not yet been tried on a fresh Pi: if one fails, read the error with the owner before changing anything.

Run these on the Pi as the `pi` user. Replace `YOUR-GITHUB-NAME` and `COMMIT` (the full commit id of your copy, from GitHub or `git rev-parse HEAD`).

1. **Stage the release** (the same folder layout the deploy uses):
   ```
   release=pi-$(date -u +%Y%m%dT%H%M%SZ)-first
   sudo install -d -o pi -g pi /opt/solarbank-cost-optimiser/releases/$release
   curl --fail -L https://github.com/YOUR-GITHUB-NAME/solarbank-4-e5000-pro-cost-optimiser/archive/COMMIT.tar.gz | tar -xz --strip-components=1 -C /opt/solarbank-cost-optimiser/releases/$release
   ```
   For a private copy, the owner types the GitHub token first (`read -rs GITHUB_TOKEN`), and the download uses the address and header that `deploy-on-pi.sh` uses: `curl --fail -L -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/repos/YOUR-GITHUB-NAME/solarbank-4-e5000-pro-cost-optimiser/tarball/COMMIT`. Keep the shell window open: `$release` is used in every step below.
2. **Build the solver environment**, once (it takes a while):
   ```
   sudo sh /opt/solarbank-cost-optimiser/releases/$release/scripts/bootstrap-solver.sh /opt/solarbank-cost-optimiser/solver-venv
   ```
3. **Install the release and run the whole test suite on the Pi**. This is the test gate, and it takes a while:
   ```
   cd /opt/solarbank-cost-optimiser/releases/$release && nice -n 10 sh scripts/install-pi-shadow.sh
   python3 -c "import sys,xml.etree.ElementTree as E;r=E.parse('pi-test-results.xml').getroot();s=[r] if r.tag=='testsuite' else list(r);f=sum(int(x.get('failures',0))+int(x.get('errors',0)) for x in s);t=sum(int(x.get('tests',0)) for x in s);print(t,'tests,',f,'failed');sys.exit(1 if f or t<100 else 0)" && echo "TESTS PASSED"
   ```
   **Stop here unless it says TESTS PASSED.** Find and fix the cause with the owner; never activate a release whose tests failed.
4. **Create the service account, its data folder, the database and the dashboard password** (what the deploy expects to exist already):
   ```
   getent passwd solarbank-optimiser >/dev/null || sudo useradd --system --home-dir /var/lib/solarbank-cost-optimiser --shell /usr/sbin/nologin solarbank-optimiser
   sudo install -d -m 700 -o solarbank-optimiser -g solarbank-optimiser /var/lib/solarbank-cost-optimiser /var/lib/solarbank-cost-optimiser/backups
   sudo -u solarbank-optimiser /opt/solarbank-cost-optimiser/releases/$release/.venv/bin/solarbank --database /var/lib/solarbank-cost-optimiser/controller.sqlite3 init
   sudo -u solarbank-optimiser sh -c 'umask 077; head -c 18 /dev/urandom | base64 > /var/lib/solarbank-cost-optimiser/dashboard-password'
   ```
5. **Write the device addresses** from question 5, before activation (otherwise it writes `CHANGE-ME` placeholders). Put the owner's three addresses in place of the examples:
   ```
   sudo install -d -m 755 /etc/solarbank-cost-optimiser
   printf 'SOLARBANK_HOST=%s\nSOLARBANK_METER_HOST=%s\nSOLARBANK_SHELLY_HOST=%s\n' 192.168.1.101 192.168.1.20 192.168.1.103 | sudo tee /etc/solarbank-cost-optimiser/devices.conf
   sudo chmod 644 /etc/solarbank-cost-optimiser/devices.conf
   ```
6. **Activate.** This creates the control files (shadow mode, nothing allowed to be sent), points `current` at the new release, and installs and starts the services, the web server and the timers:
   ```
   sudo sh /opt/solarbank-cost-optimiser/releases/$release/scripts/activate-pi-shadow.sh
   ```
   If it stops part-way, read the error, fix the cause and run it again: it only creates what is missing.
7. **Check** that all five services say `active` and the dashboard answers:
   ```
   systemctl is-active solarbank-optimiser-control solarbank-optimiser-web solarbank-optimiser-collector solarbank-optimiser-providers solarbank-optimiser-shadow
   curl -s http://127.0.0.1:8091/health
   ```

From now on `current` exists and the services are installed, so every update goes through `scripts/deploy-on-pi.sh` as normal, with its backup and its test gate.
