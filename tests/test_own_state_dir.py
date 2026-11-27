"""GitHub issue 86: the deploy's ownership step must survive SQLite removing its -wal/-shm files, and still fail on a real fault."""
import getpass
import grp
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "own-state-dir.sh"


@pytest.fixture
def stand_ins(tmp_path):
    """A stand-in `find` and `chown`, so the control flow can be tested without being root.
    LIST holds the files that still have the wrong owner. `find ... -exec chown` hands them to chown; a plain `find` prints those that still exist.
    The stand-in chown fixes a file by dropping it from LIST, removes (and fails on) any file named *-wal or *-shm when VANISH is set,
    and fixes nothing at all when REFUSE is set. `id` and `getent` say the user and group exist."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    state = tmp_path / "state"; state.mkdir()
    listing = tmp_path / "wrong.list"
    calls = tmp_path / "chown.calls"
    (bin_dir / "find").write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        f'  *-exec*) [ -s "{listing}" ] && chown -h u:u $(cat "{listing}"); exit $?;;\n'
        f'  *) while read -r f; do [ -e "$f" ] && echo "$f"; done < "{listing}"; exit 0;;\n'
        'esac\n')
    (bin_dir / "chown").write_text(
        '#!/bin/sh\n'
        f'echo "$*" >> "{calls}"\n'
        'status=0\n'
        'for f in "$@"; do\n'
        '  case "$f" in -h|u:u) continue;; esac\n'
        '  [ -n "${REFUSE:-}" ] && { echo "chown: cannot access $f: Read-only file system" >&2; status=1; continue; }\n'
        '  case "$f" in *-wal|*-shm) [ -n "${VANISH:-}" ] && { rm -f "$f"; echo "chown: $f: No such file or directory" >&2; status=1; continue; };; esac\n'
        f'  grep -vxF "$f" "{listing}" > "{listing}.new"; mv "{listing}.new" "{listing}"\n'
        'done\n'
        'exit $status\n')
    (bin_dir / "id").write_text('#!/bin/sh\nexit 0\n')                    # the service user and its group "exist" on this machine
    (bin_dir / "getent").write_text('#!/bin/sh\nexit 0\n')
    for name in ("find", "chown", "id", "getent"):
        (bin_dir / name).chmod(0o755)
    return bin_dir, state, listing, calls


def run(stand_ins, files, **flags):
    bin_dir, state, listing, calls = stand_ins
    paths = []
    for name in files:
        p = state / name; p.write_text("x"); paths.append(str(p))
    listing.write_text("\n".join(paths) + "\n")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", **{k: "1" for k, v in flags.items() if v}}
    return subprocess.run(["sh", str(SCRIPT), str(state), "solarbank-optimiser"], env=env, capture_output=True, text=True)


def test_files_that_vanish_during_the_ownership_step_are_not_a_failure(stand_ins):
    result = run(stand_ins, ["controller.sqlite3", "controller.sqlite3-wal", "controller.sqlite3-shm"], VANISH=True)
    assert result.returncode == 0, result.stderr
    assert not (stand_ins[1] / "controller.sqlite3-wal").exists()          # it really did vanish mid-run, as in the issue


def test_a_real_failure_still_stops_the_deploy_and_names_the_files(stand_ins):
    result = run(stand_ins, ["controller.sqlite3", "dashboard-password"], REFUSE=True)
    assert result.returncode == 1
    assert "Could not give these files to solarbank-optimiser" in result.stderr
    assert "controller.sqlite3" in result.stderr and "dashboard-password" in result.stderr


def test_a_vanished_file_does_not_hide_a_real_failure_beside_it(stand_ins):
    """The wal vanishes, but the database itself cannot be changed: that must still fail."""
    bin_dir, state, listing, calls = stand_ins
    (bin_dir / "chown").write_text(
        '#!/bin/sh\nstatus=0\nfor f in "$@"; do\n  case "$f" in -h|u:u) continue;; esac\n'
        '  case "$f" in *-wal) rm -f "$f"; status=1;; *) status=1;; esac\ndone\nexit $status\n')
    result = run(stand_ins, ["controller.sqlite3", "controller.sqlite3-wal"])
    assert result.returncode == 1 and "controller.sqlite3" in result.stderr and "-wal" not in result.stderr


def test_only_the_wrong_files_are_changed_and_a_second_pass_follows(stand_ins):
    result = run(stand_ins, ["a", "b"])
    assert result.returncode == 0, result.stderr
    first = stand_ins[3].read_text().splitlines()[0]
    assert str(stand_ins[1] / "a") in first and str(stand_ins[1] / "b") in first and first.startswith("-h u:u")


def test_a_folder_that_is_already_right_is_left_alone(tmp_path):
    """The real `find`: nothing has the wrong owner, so chown is never called."""
    me = getpass.getuser()
    if grp.getgrgid(os.getgid()).gr_name != me:
        pytest.skip("needs a user whose primary group has the same name")
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    calls = tmp_path / "chown.calls"
    (bin_dir / "chown").write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n'); (bin_dir / "chown").chmod(0o755)
    state = tmp_path / "state"; state.mkdir(); (state / "controller.sqlite3").write_text("x"); (state / "sub").mkdir()
    result = subprocess.run(["sh", str(SCRIPT), str(state), me], env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not calls.exists()


def test_the_activation_uses_it_and_no_longer_walks_the_data_folder_with_chown_r():
    text = (ROOT / "scripts" / "activate-pi-shadow.sh").read_text()
    assert 'own-state-dir.sh" "$state_dir" solarbank-optimiser' in text
    assert 'chown -R solarbank-optimiser' not in text
    assert 'chown -R root:root "$release_dir"' in text                      # the new release folder: nothing else has it open, so unchanged


@pytest.mark.parametrize("user,folder,message", [("no-such-user-86", None, "No such user"), ("root", "missing", "No such folder")])
def test_an_unknown_user_or_folder_is_refused_not_passed_quietly(tmp_path, user, folder, message):
    """An unknown user would make `find` fail quietly and the final check pass for the wrong reason."""
    target = tmp_path / (folder or "state"); (target.mkdir() if folder is None else None)
    result = subprocess.run(["sh", str(SCRIPT), str(target), user], capture_output=True, text=True)
    assert result.returncode == 1 and message in result.stderr
