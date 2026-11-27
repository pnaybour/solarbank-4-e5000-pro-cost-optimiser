"""WP5 (#9): the one alert path, and the never-give-up restart policy of every long-running unit."""
import json
from pathlib import Path
import urllib.error

import pytest

from solarbank_optimiser import alerts

DEPLOY = Path(__file__).resolve().parents[1] / "deployment"
LONG_RUNNING = ("control", "web", "collector", "providers", "shadow")


class Sender:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send(self, subject, body):
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append((subject, body))


# ---- the alert files and the sender ------------------------------------------------------------------------------------------------------------
def test_an_alert_is_a_small_file_and_raising_it_again_keeps_its_first_time(tmp_path):
    assert alerts.raise_alert(tmp_path / "a", "service-web", "web is not running", "details", now=100.0)
    assert alerts.raise_alert(tmp_path / "a", "service-web", "web is not running", "newer details", now=900.0)
    (item,) = alerts.pending(tmp_path / "a")
    assert item["key"] == "service-web" and item["raised_at"] == 100.0 and item["body"] == "newer details"


def test_raising_an_alert_never_raises_even_when_the_folder_is_unusable(tmp_path):
    blocker = tmp_path / "file"; blocker.write_text("x")
    assert alerts.raise_alert(blocker / "alerts", "k", "s", "b") is False


def test_a_pending_alert_is_sent_once_per_cooldown_not_every_run(tmp_path):
    alerts.raise_alert(tmp_path / "a", "service-web", "web is not running", "details", now=0.0)
    sender = Sender()
    assert alerts.process(tmp_path / "a", tmp_path / "sent.json", sender, now=1000.0) == (["service-web"], {})
    assert alerts.process(tmp_path / "a", tmp_path / "sent.json", sender, now=1120.0) == ([], {})            # two minutes later: not again
    assert alerts.process(tmp_path / "a", tmp_path / "sent.json", sender, now=1000.0 + alerts.COOLDOWN_S + 1)[0] == ["service-web"]      # still broken after 6 hours: again
    assert len(sender.sent) == 2 and sender.sent[0][0] == "web is not running"


def test_a_failed_send_keeps_the_alert_and_is_retried_on_the_next_run(tmp_path):
    alerts.raise_alert(tmp_path / "a", "k", "s", "b", now=0.0)
    sent, failed = alerts.process(tmp_path / "a", tmp_path / "sent.json", Sender(fail="the mail service answered 503"), now=10.0)
    assert sent == [] and "503" in failed["k"] and len(alerts.pending(tmp_path / "a")) == 1
    good = Sender()
    assert alerts.process(tmp_path / "a", tmp_path / "sent.json", good, now=130.0)[0] == ["k"]


def test_a_cleared_incident_is_forgotten_so_a_repeat_is_mailed_again(tmp_path):
    sender = Sender()
    alerts.raise_alert(tmp_path / "a", "k", "s", "b", now=0.0); alerts.process(tmp_path / "a", tmp_path / "sent.json", sender, now=10.0)
    alerts.clear_alert(tmp_path / "a", "k"); alerts.process(tmp_path / "a", tmp_path / "sent.json", sender, now=20.0)
    alerts.raise_alert(tmp_path / "a", "k", "s", "b", now=30.0)
    assert alerts.process(tmp_path / "a", tmp_path / "sent.json", sender, now=40.0)[0] == ["k"]


def make_sender(tmp_path, transport, private=True, config=True):
    key, cfg = tmp_path / "resend-key", tmp_path / "alert.json"
    key.write_text("re_secret_key_value"); key.chmod(0o600 if private else 0o644)
    if config:
        cfg.write_text(json.dumps({"to": "owner@example.com", "from": "alerts@example.com"}))
    return alerts.ResendSender(key, cfg, transport)


def test_the_sender_posts_to_resend_with_the_key_in_a_header_and_never_in_the_body(tmp_path):
    seen = {}
    sender = make_sender(tmp_path, lambda request: seen.update(url=request.full_url, auth=request.get_header("Authorization"), body=json.loads(request.data)) or 200)
    sender.send("web is not running", "details")
    assert seen["url"] == "https://api.resend.com/emails" and seen["auth"] == "Bearer re_secret_key_value"
    assert seen["body"]["to"] == ["owner@example.com"] and seen["body"]["subject"] == "[Solarbank] web is not running" and "re_secret" not in json.dumps(seen["body"])


@pytest.mark.parametrize("private,config,why", [(False, True, "private"), (True, False, "alert.json")])
def test_an_unprivate_key_or_missing_addresses_is_refused_with_a_reason(tmp_path, private, config, why):
    sender = make_sender(tmp_path, lambda r: 200, private=private, config=config)
    with pytest.raises(RuntimeError, match=why):
        sender.send("s", "b")


def test_a_network_error_or_a_refusal_becomes_a_clear_failure_not_a_crash(tmp_path):
    def down(request):
        raise urllib.error.URLError("no route")
    with pytest.raises(RuntimeError, match="send failed"):
        make_sender(tmp_path, down).send("s", "b")
    with pytest.raises(RuntimeError, match="401"):
        make_sender(tmp_path, lambda r: 401).send("s", "b")


# ---- the check ---------------------------------------------------------------------------------------------------------------------------------
def run_check(tmp_path, active, now, heartbeat_age=10.0, free=500 * 1024 ** 3, live_age=10.0, boot_time=0.0):
    """`heartbeat_age` / `live_age`: seconds old (negative: dated in the future); None: the file does not exist."""
    runtime = tmp_path / "run"; runtime.mkdir(exist_ok=True)
    import os
    for name, age in (("control-loop.json", heartbeat_age), ("live.json", live_age)):
        if age is None:
            (runtime / name).unlink(missing_ok=True)
            continue
        (runtime / name).write_text("{}")
        os.utime(runtime / name, (now - age, now - age))
    alerts.check(runtime / "alerts", tmp_path / "seen.json", runtime, tmp_path, now=now, is_active=lambda unit: active.get(unit.split("-")[-1].split(".")[0], True), disk_free=free,
                 boot_time=boot_time)
    return {a["key"] for a in alerts.pending(runtime / "alerts")}


def test_a_service_that_is_not_active_is_an_alert_only_after_two_checks_in_a_row(tmp_path):
    assert run_check(tmp_path, {"collector": False}, 1000.0) == set()                       # one miss: a deploy stops them for a minute
    assert run_check(tmp_path, {"collector": False}, 1120.0) == {"service-collector"}
    assert run_check(tmp_path, {}, 1240.0) == set()                                          # it came back: the alert clears


def test_a_stale_heartbeat_from_an_active_service_is_an_alert(tmp_path):
    assert run_check(tmp_path, {}, 5000.0, heartbeat_age=400.0) == {"heartbeat-control"}
    assert run_check(tmp_path, {}, 5120.0, heartbeat_age=5.0) == set()


# ---- GitHub issue 88: a MISSING heartbeat file must not look healthy ---------------------------------------------------------------------------------
def test_an_active_service_whose_heartbeat_file_is_missing_is_an_alert_once_it_has_had_time(tmp_path):
    assert run_check(tmp_path, {}, 1000.0, heartbeat_age=None) == set()                      # just seen active: the first write may not have happened yet
    assert run_check(tmp_path, {}, 1120.0, heartbeat_age=None) == set()                      # two minutes: still within the grace
    assert run_check(tmp_path, {}, 1400.0, heartbeat_age=None) == {"heartbeat-control"}      # more than five minutes active and still no file


def test_the_collectors_missing_live_file_is_an_alert_too(tmp_path):
    run_check(tmp_path, {}, 1000.0, live_age=None)
    assert run_check(tmp_path, {}, 1400.0, live_age=None) == {"heartbeat-collector"}


def test_a_heartbeat_file_that_reappears_clears_the_alert(tmp_path):
    run_check(tmp_path, {}, 1000.0, heartbeat_age=None)
    assert run_check(tmp_path, {}, 1400.0, heartbeat_age=None) == {"heartbeat-control"}
    assert run_check(tmp_path, {}, 1520.0, heartbeat_age=3.0) == set()


def test_a_fresh_boot_gets_the_grace_even_if_the_services_were_active_before(tmp_path):
    """/run is empty after a boot; the services write within seconds. The alert state is in the data folder, so it remembers an old 'active since'."""
    run_check(tmp_path, {}, 1000.0)
    assert run_check(tmp_path, {}, 90000.0, heartbeat_age=None, boot_time=89900.0) == set()  # booted 100 s ago
    assert run_check(tmp_path, {}, 90400.0, heartbeat_age=None, boot_time=89900.0) == {"heartbeat-control"}


def test_a_heartbeat_file_dated_far_in_the_future_is_an_alert(tmp_path):
    assert run_check(tmp_path, {}, 5000.0, heartbeat_age=-600.0) == {"heartbeat-control"}
    assert run_check(tmp_path, {}, 5120.0, heartbeat_age=-30.0) == set()                     # a few seconds ahead is not a fault


def test_an_inactive_service_has_its_own_alert_not_a_heartbeat_one(tmp_path):
    run_check(tmp_path, {"control": False}, 1000.0, heartbeat_age=None)
    assert run_check(tmp_path, {"control": False}, 1400.0, heartbeat_age=None) == {"service-control"}
    assert run_check(tmp_path, {}, 1520.0, heartbeat_age=None) == set()                      # back to active: the grace starts again


def test_low_disk_space_is_an_alert(tmp_path):
    assert run_check(tmp_path, {}, 9000.0, free=2 * 1024 ** 3) == {"disk-low"}


# ---- the units ---------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", LONG_RUNNING)
def test_every_long_running_unit_never_gives_up_and_alerts_when_it_fails(name):
    text = (DEPLOY / f"solarbank-optimiser-{name}.service").read_text()
    assert "StartLimitIntervalSec=0" in text and "Restart=on-failure" in text
    assert "OnFailure=solarbank-optimiser-alert@%n.service" in text
    import re
    assert 5 <= int(re.search(r"RestartSec=(\d+)", text).group(1)) <= 30


def test_the_controller_keeps_its_two_never_restart_exits_and_still_has_no_internet():
    text = (DEPLOY / "solarbank-optimiser-control.service").read_text()
    assert "RestartPreventExitStatus=2 3" in text and "IPAddressDeny=any" in text           # exit 2: the monitor owns the meter; exit 3: another controller holds the lock


def test_only_the_alert_units_may_use_the_internet_among_the_new_units_and_they_are_installed():
    for name in ("solarbank-optimiser-alert.service", "solarbank-optimiser-alert@.service"):
        text = (DEPLOY / name).read_text()
        assert "AF_INET6" in text and "IPAddressDeny" not in text and "Type=oneshot" in text
    assert "OnUnitActiveSec=2min" in (DEPLOY / "solarbank-optimiser-alert.timer").read_text()
    activate = (Path(__file__).resolve().parents[1] / "scripts/activate-pi-shadow.sh").read_text()
    assert "solarbank-optimiser-alert@.service" in activate and "solarbank-optimiser-alert.timer" in activate.split("systemctl enable --now")[1]


def test_a_unit_failed_alert_clears_when_the_unit_is_running_again_and_stays_while_it_is_not(tmp_path):
    """7 Nov 08:50: the 20:50 'collector failed' mail was repeated every six hours although the collector had been running since 20:50:06. Nothing cleared the file."""
    runtime = tmp_path / "run"
    alerts.raise_alert(runtime / "alerts", "unit-failed-solarbank-optimiser-collector.service", "collector failed", "details", now=100.0)
    assert run_check(tmp_path, {"collector": False}, 1000.0) == {"unit-failed-solarbank-optimiser-collector.service"}     # still down: the alert stays
    assert run_check(tmp_path, {}, 1120.0) == set()                                                                    # running again: it is cleared


def test_a_unit_failed_alert_for_a_timer_run_job_clears_once_it_is_no_longer_failed(tmp_path):
    """8 November: the off-site backup is inactive, not active, after a good run, so 'is it active?' could never clear its alert. Clear on 'no longer failed'."""
    runtime = tmp_path / "run"
    key = "unit-failed-solarbank-optimiser-offsite.service"
    alerts.raise_alert(runtime / "alerts", key, "offsite failed", "details", now=100.0)
    run = lambda failed: alerts.check(runtime / "alerts", tmp_path / "seen.json", runtime, tmp_path, now=1000.0, is_active=lambda unit: True, is_failed=lambda unit: failed, disk_free=500 * 1024 ** 3,
                                      boot_time=0.0)
    (runtime).mkdir(exist_ok=True)
    import os
    for name in ("control-loop.json", "live.json"):
        (runtime / name).write_text("{}")
        os.utime(runtime / name, (990.0, 990.0))                  # written 10 s before the check, on the check's own clock
    run(True)
    assert key in {a["key"] for a in alerts.pending(runtime / "alerts")}                      # still failed: the alert stays
    run(False)
    assert key not in {a["key"] for a in alerts.pending(runtime / "alerts")}                  # inactive after a later good run: cleared
