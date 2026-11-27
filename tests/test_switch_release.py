"""Review item 5: a failed backup must never lead on to stopping services or switching releases."""
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "switch-release.sh"


@pytest.fixture(autouse=True)
def untested_releases_allowed(monkeypatch):
    """The stand-in release folders have no Pi test report; only the report-gate tests below turn this off."""
    monkeypatch.setenv("SOLARBANK_ALLOW_UNTESTED", "1")


@pytest.fixture
def shim(tmp_path):
    """A stand-in `sudo` that records each command and fails on demand (FAIL_ON is a word that appears in the failing command)."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "calls.log"
    sudo = bin_dir / "sudo"
    sudo.write_text(f'#!/bin/sh\necho "$*" >> {log}\ncase "$*" in *"${{FAIL_ON:-@@never@@}}"*) exit 7;; esac\nexit 0\n')
    sudo.chmod(0o755)
    return bin_dir, log


def run(shim, fail_on=None, skip=False, release="0.9.9-test", untested=True):
    bin_dir, log = shim
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SOLARBANK_ROOT": "/root", "SOLARBANK_STATE": "/state", "SOLARBANK_HEALTH_WAIT": "0", "SOLARBANK_HEALTH_STEP": "0"}
    env.pop("FAIL_ON", None); env.pop("SOLARBANK_SKIP_BACKUP", None); env.pop("SOLARBANK_ALLOW_UNTESTED", None)
    if untested:
        env["SOLARBANK_ALLOW_UNTESTED"] = "1"          # the shim has no release folder: only the report-gate tests turn this off
    if fail_on:
        env["FAIL_ON"] = fail_on
    if skip:
        env["SOLARBANK_SKIP_BACKUP"] = "1"
    result = subprocess.run(["sh", str(SCRIPT), release], env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_normal_switch_backs_up_then_stops_then_activates(shim):
    result, calls = run(shim)
    assert result.returncode == 0
    kinds = ["prune" if "prune-backups" in c else "backup" if "backup" in c else "stop" if "systemctl stop" in c else "activate" if "activate-pi-shadow" in c else "other" for c in calls]
    assert [k for k in kinds if k != "other"] == ["backup", "prune", "stop", "activate"]
    before_backup = calls[:kinds.index("backup")]                          # and nothing unexpected runs ahead of the backup: only the two readability steps (GitHub 56 review)
    assert len(before_backup) == 2 and "chmod" in before_backup[0] and "test -x" in before_backup[1]
    assert any("/state/backups/before-0.9.9-test.sqlite3" in c for c in calls)


def test_a_failed_backup_stops_everything_before_any_service_is_touched(shim):
    result, calls = run(shim, fail_on="backup")
    assert result.returncode != 0
    assert "backup" in calls[-1]                                          # nothing after the failed backup was attempted: in particular no old backup was pruned
    assert "systemctl stop" not in " ".join(calls) and "activate" not in " ".join(calls) and not any("prune-backups" in c for c in calls)
    assert "before touching any service" in result.stderr


def test_skip_backup_is_explicit_and_leaves_the_rest_unchanged(shim):
    result, calls = run(shim, skip=True)
    assert result.returncode == 0 and "Backup skipped on request" in result.stdout
    assert not any("backup" in c for c in calls) and any("systemctl stop" in c for c in calls) and any("activate" in c for c in calls)


def test_if_activation_fails_the_services_are_started_again(shim):
    result, calls = run(shim, fail_on="activate-pi-shadow")
    assert result.returncode != 0
    assert calls[-1].startswith("systemctl start") and "solarbank-optimiser-collector" in calls[-1]
    assert "trying to start them again" in result.stderr


@pytest.mark.parametrize("bad", ["", "../etc", "a b", "x;rm -rf /", "a/b"])
def test_a_bad_release_id_is_refused_before_anything_runs(shim, bad):
    result, calls = run(shim, release=bad)
    assert result.returncode == 2 and calls == []


def test_the_deploy_script_uses_the_stop_at_first_failure_switch():
    text = (SCRIPT.parent / "deploy-pi-shadow.sh").read_text()
    assert "switch-release.sh" in text
    assert "backup_step" not in text and "; sudo systemctl stop" not in text      # the old fall-through line is gone


def test_the_controller_is_stopped_with_the_others_once_its_unit_is_installed_and_not_before(shim, tmp_path):
    units = tmp_path / "units"; units.mkdir()
    bin_dir, log = shim
    base = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SOLARBANK_ROOT": "/root", "SOLARBANK_STATE": "/state",
            "SOLARBANK_SKIP_BACKUP": "1", "SOLARBANK_UNITS": str(units)}
    subprocess.run(["sh", str(SCRIPT), "0.9.9-test"], env=base, capture_output=True, text=True)
    assert "solarbank-optimiser-control" not in log.read_text().split("systemctl stop")[1].splitlines()[0]
    (units / "solarbank-optimiser-control.service").write_text("[Unit]\n")
    log.unlink()
    subprocess.run(["sh", str(SCRIPT), "0.9.9-test"], env=base, capture_output=True, text=True)
    assert "solarbank-optimiser-control" in log.read_text().split("systemctl stop")[1].splitlines()[0]


# ---- WP2 (#11): backups are bounded -------------------------------------------------------------------------------------------------------------
def real_shim(tmp_path):
    """A stand-in sudo that really runs `sh -c` commands, and 'backs up' by creating the target file the way a successful backup would."""
    bin_dir = tmp_path / "rbin"; bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\ncase "$*" in\n *" backup "*) for last; do :; done; : > "$last"; exit 0;;\n "sh -c"*) exec "$@";;\nesac\nexit 0\n')
    sudo.chmod(0o755)
    return bin_dir


def switch(tmp_path, release, bin_dir, keep=None, fail_backup=False):
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SOLARBANK_ROOT": "/root", "SOLARBANK_STATE": str(tmp_path / "state")}
    env.pop("SOLARBANK_SKIP_BACKUP", None)
    if keep:
        env["SOLARBANK_KEEP_BACKUPS"] = str(keep)
    return subprocess.run(["sh", str(SCRIPT), release], env=env, capture_output=True, text=True)


def test_after_five_switches_at_most_three_backups_remain_and_the_newest_are_kept(tmp_path):
    import time
    (tmp_path / "state" / "backups").mkdir(parents=True)
    bin_dir = real_shim(tmp_path)
    for i in range(5):
        assert switch(tmp_path, f"0.5.{i}-x", bin_dir).returncode == 0
        os.utime(tmp_path / "state" / "backups" / f"before-0.5.{i}-x.sqlite3", (1000 + i, 1000 + i))      # distinct ages, oldest first
        time.sleep(0.01)
    names = sorted(p.name for p in (tmp_path / "state" / "backups").iterdir())
    assert names == ["before-0.5.2-x.sqlite3", "before-0.5.3-x.sqlite3", "before-0.5.4-x.sqlite3"]


def test_pruning_touches_only_before_backups_and_never_the_new_one(tmp_path):
    backups = tmp_path / "state" / "backups"; backups.mkdir(parents=True)
    (backups / "keep-me-manual.sqlite3").write_text("x"); (backups / "notes.txt").write_text("x")
    for i in range(6):
        p = backups / f"before-old{i}.sqlite3"; p.write_text("x"); os.utime(p, (100 + i, 100 + i))
    assert switch(tmp_path, "9.9.9-new", real_shim(tmp_path), keep=2).returncode == 0
    names = sorted(p.name for p in backups.iterdir())
    assert "before-9.9.9-new.sqlite3" in names and "keep-me-manual.sqlite3" in names and "notes.txt" in names
    assert [n for n in names if n.startswith("before-")] == ["before-9.9.9-new.sqlite3", "before-old5.sqlite3"]


@pytest.mark.parametrize("keep", ["0", "x", "-1", ""])
def test_a_silly_keep_count_falls_back_to_three(tmp_path, keep):
    backups = tmp_path / "state" / "backups"; backups.mkdir(parents=True)
    for i in range(5):
        p = backups / f"before-old{i}.sqlite3"; p.write_text("x"); os.utime(p, (100 + i, 100 + i))
    env_keep = keep or None
    assert switch(tmp_path, "9.9.9-new", real_shim(tmp_path), keep=env_keep).returncode == 0
    assert len([p for p in backups.iterdir() if p.name.startswith("before-")]) == 3


def test_skipping_the_backup_prunes_nothing(shim):
    result, calls = run(shim, skip=True)
    assert not any("prune-backups" in c for c in calls)


def test_a_release_older_than_the_marked_one_is_refused_unless_forced(shim, tmp_path):
    bin_dir, log = shim
    state = tmp_path / "state"; state.mkdir()
    (state / "oldest-compatible-release").write_text("pi-20261110T090000Z-single-store\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SOLARBANK_ROOT": "/root", "SOLARBANK_STATE": str(state), "SOLARBANK_SKIP_BACKUP": "1"}
    env.pop("SOLARBANK_ALLOW_OLDER", None)
    older = subprocess.run(["sh", str(SCRIPT), "pi-20261103T082247Z-fix-combined"], env=env, capture_output=True, text=True)
    assert older.returncode == 3 and "older than pi-20261110T090000Z-single-store" in older.stderr
    assert not log.exists()                                                  # nothing was stopped or switched
    same = subprocess.run(["sh", str(SCRIPT), "pi-20261110T090000Z-single-store"], env=env, capture_output=True, text=True)
    newer = subprocess.run(["sh", str(SCRIPT), "pi-20261201T090000Z-later"], env=env, capture_output=True, text=True)
    forced = subprocess.run(["sh", str(SCRIPT), "pi-20261103T082247Z-fix-combined"], env={**env, "SOLARBANK_ALLOW_OLDER": "1"}, capture_output=True, text=True)
    assert same.returncode == newer.returncode == forced.returncode == 0


@pytest.mark.parametrize("marker,target,code", [
    ("0.5.50-20261110T090000Z", "0.5.36-20261103T082247Z", 3),            # the deploy script's own names: an older release is refused
    ("0.5.50-20261110T090000Z", "0.5.51-20261111T000000Z", 0),
    ("0.5.50-20261110T090000Z", "0.5.50-20261110T090000Z", 0),
    ("0.5.50-20261110T090000Z", "no-stamp-here", 3),                      # an unreadable name is a refusal, not a pass
    ("garbled", "pi-20261201T090000Z-later", 3),                           # so is an unreadable marker
])
def test_the_guard_reads_the_release_names_the_deploy_scripts_make_and_never_fails_open(shim, tmp_path, marker, target, code):
    bin_dir, log = shim
    state = tmp_path / "state"; state.mkdir()
    (state / "oldest-compatible-release").write_text(marker + "\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SOLARBANK_ROOT": "/root", "SOLARBANK_STATE": str(state), "SOLARBANK_SKIP_BACKUP": "1"}
    env.pop("SOLARBANK_ALLOW_OLDER", None)
    assert subprocess.run(["sh", str(SCRIPT), target], env=env, capture_output=True, text=True).returncode == code



# ---- GitHub issue 47: unreadable releases are refused, and a release whose services do not come up is rolled back ---------------------------------------------
def test_a_release_the_service_user_cannot_run_is_refused_before_anything_is_stopped(shim):
    result, calls = run(shim, fail_on="test -x")
    assert result.returncode == 4 and "cannot run" in result.stderr
    assert not any("systemctl stop" in c or "activate" in c or "backup" in c for c in calls)


def test_a_release_whose_services_do_not_come_up_is_rolled_back_and_the_marker_restored(shim):
    result, calls = run(shim, fail_on="is-active")
    assert result.returncode == 3 and "going back" in result.stderr and "Rolled back" in result.stderr
    text = " ".join(calls)
    assert "systemctl restart" in text and "oldest-compatible-release" in text           # restarted, and the marker this activation made is removed


def test_the_deploy_script_refuses_to_switch_on_a_test_report_with_failures(tmp_path):
    import textwrap
    report = tmp_path / "r.xml"
    gate = textwrap.dedent("""
        import sys, xml.etree.ElementTree as ET
        root = ET.parse(sys.argv[1]).getroot()
        suites = [root] if root.tag == "testsuite" else list(root)
        failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
        tests = sum(int(s.get("tests", 0)) for s in suites)
        sys.exit(1 if failed or tests < 100 else 0)
    """)
    text = (Path(__file__).resolve().parents[1] / "scripts" / "deploy-on-pi.sh").read_text()
    assert "pi-test-results.xml" in text and "not switching" in text                       # the gate is in the script, and mirrors the check below
    for body, code in (('<testsuites><testsuite tests="1400" failures="0" errors="0"/></testsuites>', 0),
                       ('<testsuites><testsuite tests="1400" failures="9" errors="0"/></testsuites>', 1),
                       ('<testsuites><testsuite tests="3" failures="0" errors="0"/></testsuites>', 1)):
        report.write_text(body)
        assert subprocess.run(["python3", "-c", gate, str(report)]).returncode == code


def test_a_newer_generation_raises_the_oldest_compatible_marker_and_the_same_one_does_not(tmp_path):
    """The activation snippet, run on its own: the floor moves only when the release carries a newer generation than the recorded one."""
    import subprocess
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "scripts" / "activate-pi-shadow.sh").read_text()
    snippet = text[text.index("# RC1:"):text.index("# Code is read-only")]
    state = tmp_path / "state"; state.mkdir()
    def activate(name, generation):
        release = tmp_path / name
        (release / "deployment").mkdir(parents=True)
        (release / "deployment" / "OLDEST_COMPATIBLE").write_text(f"generation={generation}\nexplanation\n")
        subprocess.run(["bash", "-c", snippet], check=True, env={"PATH": "/usr/bin:/bin", "release_dir": str(release), "state_dir": str(state)})
    marker, generation = state / "oldest-compatible-release", state / "oldest-compatible-generation"
    activate("pi-20261104T000000Z-a", 1)
    assert marker.read_text().strip() == "pi-20261104T000000Z-a" and generation.read_text().strip() == "1"
    activate("pi-20261104T010000Z-b", 1)                   # the same generation: the floor stays where it was
    assert marker.read_text().strip() == "pi-20261104T000000Z-a"
    activate("pi-20261105T000000Z-c", 2)                   # a newer one: the floor moves up
    assert marker.read_text().strip() == "pi-20261105T000000Z-c" and generation.read_text().strip() == "2"


# ---- GitHub 56: the test-report gate, and GitHub 54: an activation that fails part-way puts everything back ----------------------------------------------
def release_with_report(tmp_path, failures=0, errors=0, tests=1500):
    root = tmp_path / "opt"
    (root / "releases" / "r1").mkdir(parents=True)
    (root / "releases" / "r1" / "pi-test-results.xml").write_text(f'<testsuites><testsuite name="pytest" tests="{tests}" failures="{failures}" errors="{errors}"></testsuite></testsuites>')
    return root


def run_in(shim, root, state, extra=None, release="r1"):
    bin_dir, log = shim
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SOLARBANK_ROOT": str(root), "SOLARBANK_STATE": str(state), "SOLARBANK_HEALTH_WAIT": "0",
           "SOLARBANK_HEALTH_STEP": "0", "SOLARBANK_SKIP_BACKUP": "1", **(extra or {})}
    result = subprocess.run(["sh", str(SCRIPT), release], env=env, capture_output=True, text=True)
    return result, (log.read_text().splitlines() if log.exists() else [])


@pytest.mark.parametrize("failures,errors,tests", [(1, 0, 1500), (0, 1, 1500), (0, 0, 20)])
def test_a_release_whose_pi_tests_failed_is_refused_before_anything_is_stopped(shim, tmp_path, monkeypatch, failures, errors, tests):
    monkeypatch.delenv("SOLARBANK_ALLOW_UNTESTED")
    root = release_with_report(tmp_path, failures, errors, tests)
    result, calls = run_in(shim, root, tmp_path / "state")
    assert result.returncode == 5 and "test report" in result.stderr
    assert not any("systemctl stop" in c or "activate" in c for c in calls)


def test_a_passing_report_proceeds_and_a_missing_one_needs_the_explicit_override(shim, tmp_path, monkeypatch):
    monkeypatch.delenv("SOLARBANK_ALLOW_UNTESTED")
    root = release_with_report(tmp_path)
    result, calls = run_in(shim, root, tmp_path / "state")
    assert result.returncode == 0 and any("activate" in c for c in calls)
    (root / "releases" / "r1" / "pi-test-results.xml").unlink()
    refused, calls = run_in(shim, root, tmp_path / "state2")
    assert refused.returncode == 5 and "ALLOW_UNTESTED" in refused.stderr
    allowed, _ = run_in(shim, root, tmp_path / "state3", {"SOLARBANK_ALLOW_UNTESTED": "1"})
    assert allowed.returncode == 0


def test_an_activation_that_fails_part_way_restores_current_and_the_floor_before_the_services_start(tmp_path):
    """A stand-in sudo that really runs the symlink and file commands, and whose activation script 'changes' current and the floor and then fails: both must be
    exactly what they were afterwards, and the services are started last."""
    bin_dir = tmp_path / "rbin"; bin_dir.mkdir()
    log = tmp_path / "calls.log"
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/bin/sh\n"
                    f'echo "$*" >> {log}\n'
                    'case "$*" in\n'
                    ' *activate-pi-shadow*) ln -sfn "$ROOT/releases/r1" "$ROOT/current"; echo r1 > "$STATE/oldest-compatible-release"; echo 2 > "$STATE/oldest-compatible-generation"; exit 9;;\n'
                    ' "ln "*|"mv "*|"rm "*|"tee "*|"chmod "*) exec "$@";;\n'
                    'esac\nexit 0\n')
    sudo.chmod(0o755)
    root, state = tmp_path / "opt", tmp_path / "state"
    for name in ("r0", "r1"):
        (root / "releases" / name).mkdir(parents=True)
    state.mkdir()
    os.symlink(root / "releases" / "r0", root / "current")
    (state / "oldest-compatible-release").write_text("r0\n")
    (state / "oldest-compatible-generation").write_text("1\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SOLARBANK_ROOT": str(root), "SOLARBANK_STATE": str(state), "SOLARBANK_SKIP_BACKUP": "1",
           "ROOT": str(root), "STATE": str(state), "SOLARBANK_ALLOW_OLDER": "1"}
    result = subprocess.run(["sh", str(SCRIPT), "r1"], env=env, capture_output=True, text=True)
    assert result.returncode == 9, result.stderr
    assert os.readlink(root / "current") == str(root / "releases" / "r0")
    assert (state / "oldest-compatible-release").read_text().strip() == "r0" and (state / "oldest-compatible-generation").read_text().strip() == "1"
    calls = log.read_text().splitlines()
    assert calls[-1].startswith("systemctl start")                       # the services come back last, on the release that was running


def test_deploy_refuses_to_start_beside_another_heavy_job_and_runs_the_install_at_low_priority(tmp_path):
    """A fake pgrep that finds a running backup stops the deploy before it downloads anything; SOLARBANK_ALLOW_OVERLAP=1 lets it past the guard."""
    import os, stat, subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy-on-pi.sh"
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    for name, body in (("pgrep", "exit 0"), ("sudo", "exit 0"), ("curl", "echo fake curl >&2; exit 22")):
        f = bin_dir / name; f.write_text("#!/bin/sh\n" + body + "\n"); f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_TOKEN": "x", "HOME": str(tmp_path)}
    blocked = subprocess.run(["sh", str(script), "main"], env=env, capture_output=True, text=True)
    assert blocked.returncode == 1 and "Another heavy job" in blocked.stderr and "State or solver" not in blocked.stderr
    allowed = subprocess.run(["sh", str(script), "main"], env={**env, "SOLARBANK_ALLOW_OVERLAP": "1"}, capture_output=True, text=True)
    assert "Another heavy job" not in allowed.stderr                  # past the guard (what stops it next depends on the machine: the Pi has the solver environment, this sandbox may not)
    assert "nice -n 10" in script.read_text() and "$low sh scripts/install-pi-shadow.sh" in script.read_text()
