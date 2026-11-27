"""GitHub issue 89: the independent heartbeat. Read-only, outside the control loop, never logs a URL, and withholds the optimiser ping unless the controller and the collector
are demonstrably working."""
import json
from pathlib import Path

import pytest

from solarbank_optimiser import external_heartbeat as hb

NOW = 1_800_000_000.0
PI = "https://hc-ping.com/aaaaaaaa-0000-0000-0000-000000000001"
APP = "https://hc-ping.com/bbbbbbbb-0000-0000-0000-000000000002"
BACKUP = "https://hc-ping.com/cccccccc-0000-0000-0000-000000000003"
DEPLOY = Path(__file__).resolve().parents[1] / "deployment"


class Opener:
    """A fake urlopen that records the addresses it was given."""
    def __init__(self, fail=False, status=200):
        self.urls, self.fail, self.status = [], fail, status

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        if self.fail:
            raise OSError("network is down")
        status = self.status

        class Response:
            def __init__(self):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return Response()


@pytest.fixture
def system(tmp_path):
    """A healthy installation: fresh status files, the mode file saying live, and a heartbeat config."""
    run, state, etc = tmp_path / "run", tmp_path / "state", tmp_path / "etc"
    for folder in (run, state, etc):
        folder.mkdir()
    (etc / "control-mode").write_text("live\n")
    (state / "healthchecks.json").write_text(json.dumps({"pi": PI, "optimiser": APP, "backup": BACKUP}))

    def write(controller=None, live=None):
        c = {"at": NOW - 1, "cycle": 5000, "execution": {"mode": "live", "sends_commands": True}}
        l = {"written_at": NOW - 2, "observations": [{"metric": "soc", "value": 0.5}]}
        c.update(controller or {}); l.update(live or {})
        (run / "control-loop.json").write_text(json.dumps(c)); (run / "live.json").write_text(json.dumps(l))
    write()
    return type("S", (), {"run": run, "state": state, "mode": etc / "control-mode", "write": staticmethod(write)})


def healthy(s, now=NOW):
    return hb.optimiser_healthy(now, s.run, s.mode)


def test_a_working_installation_is_healthy(system):
    assert healthy(system) == (True, "ok")


@pytest.mark.parametrize("controller,why", [({"at": NOW - 120}, "status is"), ({"cycle": 0}, "completed a cycle"), ({"execution": {"mode": "logged", "sends_commands": False}}, "control-mode says"),
                                            ({"execution": {"mode": "live", "sends_commands": False}}, "not sending"), ({"at": NOW + 3000}, "status is")])
def test_a_stalled_or_misbehaving_controller_is_unhealthy(system, controller, why):
    system.write(controller=controller)
    ok, reason = healthy(system)
    assert not ok and why in reason


@pytest.mark.parametrize("live", [{"written_at": NOW - 600}, {"observations": []}, {"written_at": NOW + 3000}])
def test_a_stalled_collector_is_unhealthy(system, live):
    system.write(live=live)
    assert not healthy(system)[0]


def test_missing_or_unreadable_files_are_unhealthy(system):
    (system.run / "live.json").unlink()
    assert not healthy(system)[0]
    system.write()
    (system.run / "control-loop.json").write_text("not json")
    assert not healthy(system)[0]
    system.write()
    system.mode.unlink()
    assert not healthy(system)[0]


def test_shadow_and_logged_modes_are_healthy_when_they_match_the_mode_file(system):
    system.mode.write_text("logged")
    system.write(controller={"execution": {"mode": "logged", "sends_commands": False}})
    assert healthy(system)[0]                                                    # a controller that is not live is not a fault


def test_a_healthy_system_sends_both_pings(system):
    opener = Opener()
    assert hb.main(system.state, system.run, system.mode, NOW, opener) == 0
    assert opener.urls == [PI, APP]


def test_an_unhealthy_optimiser_withholds_only_its_own_ping(system, capsys):
    system.write(controller={"at": NOW - 900})
    opener = Opener()
    assert hb.main(system.state, system.run, system.mode, NOW, opener) == 0      # withholding is the signal, not a failure of this job
    assert opener.urls == [PI]
    assert "withheld" in capsys.readouterr().err


def test_a_ping_that_cannot_be_delivered_fails_the_run_and_never_prints_the_address(system, capsys):
    assert hb.main(system.state, system.run, system.mode, NOW, Opener(fail=True)) == 1
    err = capsys.readouterr().err
    assert "OSError" in err and "hc-ping.com" not in err and "aaaaaaaa" not in err


def test_a_missing_config_sends_nothing(system, capsys):
    (system.state / "healthchecks.json").unlink()
    opener = Opener()
    assert hb.main(system.state, system.run, system.mode, NOW, opener) == 1 and opener.urls == []
    assert "configuration" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://hc-ping.com/abc", "https://evil.example/abc", "https://hc-ping.com/", "https://user:pw@hc-ping.com/abc", "https://hc-ping.com/abc?x=1", "ftp://hc-ping.com/abc", "", None, 5])
def test_only_a_plain_https_hc_ping_address_is_accepted(url, capsys):
    opener = Opener()
    assert hb.ping(url, opener=opener) is False and opener.urls == []
    assert "evil.example" not in capsys.readouterr().err


def test_a_non_200_answer_is_not_a_delivered_ping():
    assert hb.ping(PI, opener=Opener(status=500)) is False


def test_the_backup_ping_goes_only_to_the_backup_check_and_only_when_configured(system):
    opener = Opener()
    assert hb.ping_backup(system.state, opener) is True and opener.urls == [BACKUP]
    (system.state / "healthchecks.json").write_text(json.dumps({"pi": PI, "optimiser": APP}))
    assert hb.ping_backup(system.state, opener) is False and opener.urls == [BACKUP]


def test_the_backup_ping_can_never_break_the_backup(system):
    assert hb.ping_backup(system.state, Opener(fail=True)) is False
    (system.state / "healthchecks.json").write_text("garbage")
    assert hb.ping_backup(system.state, Opener()) is False
    assert hb.ping_backup(system.state / "missing", Opener()) is False


def test_the_unit_is_read_only_confined_and_the_timer_runs_every_five_minutes():
    service = (DEPLOY / "solarbank-optimiser-heartbeat.service").read_text()
    assert "Type=oneshot" in service and "User=solarbank-optimiser" in service and "ProtectSystem=strict" in service and "NoNewPrivileges=true" in service
    assert "ReadOnlyPaths=/var/lib/solarbank-cost-optimiser /run/solarbank-cost-optimiser /etc/solarbank-cost-optimiser" in service
    assert "ReadWritePaths" not in service                                       # it writes nothing at all
    assert "AF_INET6" in service and " heartbeat" in service.split("ExecStart=")[1].splitlines()[0]
    assert "OnFailure" not in service                                            # an unreachable network cannot send an email about itself
    timer = (DEPLOY / "solarbank-optimiser-heartbeat.timer").read_text()
    assert "OnUnitActiveSec=5min" in timer and "OnBootSec=2min" in timer


def test_no_ping_address_is_in_the_repository():
    root = Path(__file__).resolve().parents[1]
    offenders = [str(p) for p in list((root / "src").rglob("*.py")) + list(DEPLOY.glob("*")) + [root / "OPERATIONS.md"]
                 if "hc-ping.com/" in p.read_text() and p.name not in ("external_heartbeat.py", "OPERATIONS.md")]
    assert offenders == []
    for p in (root / "src" / "solarbank_optimiser" / "external_heartbeat.py", root / "OPERATIONS.md"):
        import re
        assert not re.search(r"hc-ping\.com/[0-9a-f]{8}-", p.read_text())          # only the placeholder, never a real UUID


def test_the_activation_installs_the_units_and_turns_the_timer_on_only_when_the_private_config_exists():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "activate-pi-shadow.sh").read_text()
    assert "solarbank-optimiser-heartbeat.service" in text and "solarbank-optimiser-heartbeat.timer" in text
    enable = text.split("healthchecks.json")[1].split("fi")[0]
    assert "systemctl enable --now solarbank-optimiser-heartbeat.timer" in enable
    assert "[ -s /var/lib/solarbank-cost-optimiser/healthchecks.json ]" in text      # no config, no timer: a deploy without one changes nothing
    assert text.count("heartbeat.timer") >= 3                                       # installed, verified and enabled
