#!/bin/sh
# Give the data folder to the service user (called by activate-pi-shadow.sh, as root):  sh own-state-dir.sh <folder> <user>
#
# This replaces `chown -R`, which failed a deploy on 12 November 2026 (GitHub issue 86): SQLite removes controller.sqlite3-wal and -shm when the
# last connection closes, and one of them can vanish between chown listing the folder and changing the file, which makes chown exit 1 and the
# activation (set -e) treat it as a failed switch.
#
# It changes only files whose owner or group is wrong (so a folder that is already right is left untouched), never follows a symbolic link
# (chown -h), and does not trust chown's own exit status: a file that vanished mid-run is not a failure. Instead it looks again afterwards and
# fails, naming up to five files, only if something that still exists has the wrong owner. A real fault (read-only disk, no permission)
# therefore still stops the deploy.
set -eu
dir="${1:?usage: own-state-dir.sh <folder> <user>}"
owner="${2:?usage: own-state-dir.sh <folder> <user>}"

# An unknown user or group would make `find` fail quietly and the check below pass for the wrong reason: refuse instead.
id -u "$owner" >/dev/null 2>&1 || { echo "No such user: $owner" >&2; exit 1; }
getent group "$owner" >/dev/null 2>&1 || { echo "No such group: $owner" >&2; exit 1; }
[ -d "$dir" ] || { echo "No such folder: $dir" >&2; exit 1; }

wrong() { find "$dir" \( ! -user "$owner" -o ! -group "$owner" \) 2>/dev/null; }

fix() { find "$dir" \( ! -user "$owner" -o ! -group "$owner" \) -exec chown -h "$owner:$owner" {} + 2>/dev/null || true; }

fix
fix          # a second pass catches a file that appeared during the first (SQLite creates -wal/-shm again when it reopens)
left=$(wrong | head -n 5 || true)
if [ -n "$left" ]; then
  echo "Could not give these files to $owner:" >&2
  echo "$left" >&2
  exit 1
fi
