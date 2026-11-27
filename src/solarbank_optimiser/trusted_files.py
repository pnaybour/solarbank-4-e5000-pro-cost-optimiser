"""Files that authorise writes (the status flag, the write-switch, the commissioning record) are only believed when root owns them and nothing the service runs as
could have replaced them: a regular file (not a link) owned by root that nobody else can write, in a directory that is also root-owned and not writable by others.
They live in /etc/solarbank-cost-optimiser, not in the service's own data folder, where the service could swap a root-owned file for its own."""
from __future__ import annotations

import os
import stat
from pathlib import Path

AUTH_DIR = Path("/etc/solarbank-cost-optimiser")
REQUIRE_ROOT = True            # the test suite turns this off (it does not run as root on every machine) and tests it separately


def _owner_ok(status):
    return status.st_uid == 0


def trusted(path):
    """True when `path` may be believed as an authorisation."""
    if not REQUIRE_ROOT:
        return True
    try:
        status = os.lstat(path)
        parent = os.lstat(Path(path).parent)
    except OSError:
        return False
    if not stat.S_ISREG(status.st_mode) or not _owner_ok(status) or status.st_mode & 0o022:
        return False
    return stat.S_ISDIR(parent.st_mode) and _owner_ok(parent) and not parent.st_mode & 0o022


def read_trusted(path):
    """The text of an authorisation file, or None if it is missing, unreadable or not trustworthy."""
    if not trusted(path):
        return None
    try:
        return Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return None
