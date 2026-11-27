#!/bin/sh
# Reviewed activation after the staged package/tests pass. No equipment writer.
set -eu
release_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$release_dir" in /opt/solarbank-cost-optimiser/releases/*) ;; *) exit 1;; esac
state_dir=/var/lib/solarbank-cost-optimiser
getent passwd solarbank-optimiser >/dev/null || useradd --system --home-dir "$state_dir" --shell /usr/sbin/nologin solarbank-optimiser
install -d -m 700 -o solarbank-optimiser -g solarbank-optimiser "$state_dir" "$state_dir/backups"
test -f "$state_dir/controller.sqlite3"
test -f "$state_dir/dashboard-password"
# The planner's isolated solver environment (outside the releases so updates keep it). Build it once with scripts/bootstrap-solver.sh.
if [ ! -x /opt/solarbank-cost-optimiser/solver-venv/bin/python ]; then
 echo "Missing /opt/solarbank-cost-optimiser/solver-venv: run  sudo sh $release_dir/scripts/bootstrap-solver.sh /opt/solarbank-cost-optimiser/solver-venv  first." >&2
 exit 1
fi
# The private LAN frontend uses HTTP. Keep any older certificate files private
# for rollback, but do not generate or require a self-signed certificate.
chmod 600 "$state_dir/dashboard-password"
# Not `chown -R`: it failed a deploy when SQLite removed its -wal/-shm files mid-walk (GitHub issue 86). This tolerates files that vanish and still fails on a real fault.
sh "$release_dir/scripts/own-state-dir.sh" "$state_dir" solarbank-optimiser
# The authorisation files (RC4): the status flag, the write-switch and the commissioning record. They live in /etc/solarbank-cost-optimiser, a folder only root can change, NOT in the
# service's own data folder: the service owns that folder and could swap a root-owned file in it for its own. The controller also refuses to believe any of them unless root owns it and
# nobody else can write it. An existing choice is kept across releases; a new installation starts in shadow with nothing allowed.
auth_dir=/etc/solarbank-cost-optimiser
install -d -m 755 -o root -g root "$auth_dir"
chown root:root "$auth_dir"; chmod 755 "$auth_dir"
if [ ! -f "$auth_dir/control-mode" ]; then echo shadow > "$auth_dir/control-mode"; fi
if [ ! -f "$auth_dir/write-switch" ]; then : > "$auth_dir/write-switch"; fi
for name in control-mode write-switch commissioning.json; do
 if [ -f "$auth_dir/$name" ]; then chown root:root "$auth_dir/$name"; chmod 644 "$auth_dir/$name"; fi
 # the old copies in the data folder are no longer read; remove them so nobody edits the wrong file
 rm -f "$state_dir/$name"
done
# Where the devices are (WP20): one small file outside the releases. Created ONCE from the addresses this installation has used so far, and never overwritten, so a release carries
# no address and a router change is a one-line edit. Edit it with: sudoedit /etc/solarbank-cost-optimiser/devices.conf, then: sudo systemctl restart solarbank-optimiser-control solarbank-optimiser-collector
install -d -m 755 /etc/solarbank-cost-optimiser
if [ ! -f /etc/solarbank-cost-optimiser/devices.conf ]; then
 printf '%s\n' '# The three devices (WP20). One address or host name per line.' 'SOLARBANK_HOST=CHANGE-ME-solarbank' 'SOLARBANK_METER_HOST=CHANGE-ME-meter' 'SOLARBANK_SHELLY_HOST=CHANGE-ME-shelly' > /etc/solarbank-cost-optimiser/devices.conf
 chmod 644 /etc/solarbank-cost-optimiser/devices.conf
fi
# The home network's own IPv6 prefix (the ISP's /64): one file, read by the dashboard and turned into nginx's allow list. Created ONCE, empty (IPv6 access is optional: add your own /64 to use it), never overwritten.
if [ ! -f /etc/solarbank-cost-optimiser/lan-prefixes ]; then
 printf '%s\n' '# Home network prefixes allowed to open the dashboard, one per line (IPv6 /64 from the ISP). Edit, then re-run the deploy or: sudo systemctl restart solarbank-optimiser-web' > /etc/solarbank-cost-optimiser/lan-prefixes
 chmod 644 /etc/solarbank-cost-optimiser/lan-prefixes
fi
grep -Ev '^[[:space:]]*(#|$)' /etc/solarbank-cost-optimiser/lan-prefixes | grep -E '^[0-9A-Fa-f:.]+/[0-9]{1,3}$' | sed 's/.*/allow &;/' > /etc/nginx/solarbank-optimiser-lan.conf
chmod 644 /etc/nginx/solarbank-optimiser-lan.conf
# RC1: a release that carries deployment/OLDEST_COMPATIBLE names itself as the oldest release that can read the data when its `generation=N` is newer than the one recorded
# (the file `oldest-compatible-generation`; none recorded counts as 0). switch-release.sh refuses anything older than the release named in the marker.
if [ -f "$release_dir/deployment/OLDEST_COMPATIBLE" ]; then
 new_generation=$(sed -n 's/^generation=\([0-9][0-9]*\)$/\1/p' "$release_dir/deployment/OLDEST_COMPATIBLE" | head -n 1)
 old_generation=$(cat "$state_dir/oldest-compatible-generation" 2>/dev/null || echo 0)
 case "$old_generation" in ""|*[!0-9]*) old_generation=0;; esac
 if [ ! -f "$state_dir/oldest-compatible-release" ] || [ "${new_generation:-1}" -gt "$old_generation" ]; then
  basename "$release_dir" > "$state_dir/oldest-compatible-release"
  chmod 644 "$state_dir/oldest-compatible-release"
  printf '%s\n' "${new_generation:-1}" > "$state_dir/oldest-compatible-generation"
  chmod 644 "$state_dir/oldest-compatible-generation"
 fi
fi
# Code is read-only to the unprivileged runtime user.
chown -R root:root "$release_dir"
chmod -R go-w "$release_dir"
ln -sfn "$release_dir" /opt/solarbank-cost-optimiser/current.next
mv -Tf /opt/solarbank-cost-optimiser/current.next /opt/solarbank-cost-optimiser/current
for role in control web collector providers shadow; do
 install -m 644 "$release_dir/deployment/solarbank-optimiser-$role.service" /etc/systemd/system/
done
install -m 644 "$release_dir/deployment/solarbank-optimiser-offsite.service" "$release_dir/deployment/solarbank-optimiser-offsite.timer" /etc/systemd/system/
install -m 644 "$release_dir/deployment/solarbank-optimiser-storage.service" "$release_dir/deployment/solarbank-optimiser-storage.timer" /etc/systemd/system/
install -m 644 "$release_dir/deployment/solarbank-optimiser-alert.service" "$release_dir/deployment/solarbank-optimiser-alert@.service" "$release_dir/deployment/solarbank-optimiser-alert.timer" /etc/systemd/system/
# The independent heartbeat (GitHub issue 89): read-only pings to an outside dead-man's switch. The units are always installed; the timer is turned on below only once its private config exists.
install -m 644 "$release_dir/deployment/solarbank-optimiser-heartbeat.service" "$release_dir/deployment/solarbank-optimiser-heartbeat.timer" /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify "$release_dir"/deployment/solarbank-optimiser-*.service "$release_dir"/deployment/solarbank-optimiser-storage.timer "$release_dir"/deployment/solarbank-optimiser-alert.timer "$release_dir"/deployment/solarbank-optimiser-offsite.timer "$release_dir"/deployment/solarbank-optimiser-heartbeat.timer
if [ -f "$release_dir/deployment/solarbank-optimiser.nginx" ]; then
 install -m 644 "$release_dir/deployment/solarbank-optimiser.nginx" /etc/nginx/sites-available/solarbank-optimiser
 ln -sfn /etc/nginx/sites-available/solarbank-optimiser /etc/nginx/sites-enabled/solarbank-optimiser
 nginx -t
fi
systemctl enable --now solarbank-optimiser-control.service solarbank-optimiser-web.service solarbank-optimiser-collector.service solarbank-optimiser-providers.service solarbank-optimiser-shadow.service solarbank-optimiser-storage.timer solarbank-optimiser-alert.timer
# The off-site backup timer is turned on only once a remote exists (the owner's rclone configuration): a deploy without one leaves the timer off.
if [ -s /var/lib/solarbank-cost-optimiser/rclone.conf ] && ! systemctl is-enabled --quiet solarbank-optimiser-offsite.timer 2>/dev/null; then
 systemctl enable --now solarbank-optimiser-offsite.timer
fi
# The heartbeat timer is turned on only once its private config (the ping addresses, typed by the owner on the Pi) exists: a deploy without one leaves it off.
if [ -s /var/lib/solarbank-cost-optimiser/healthchecks.json ] && ! systemctl is-enabled --quiet solarbank-optimiser-heartbeat.timer 2>/dev/null; then
 systemctl enable --now solarbank-optimiser-heartbeat.timer
fi
if [ -f "$release_dir/deployment/solarbank-optimiser.nginx" ]; then
 systemctl reload nginx
fi
