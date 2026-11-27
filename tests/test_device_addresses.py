"""WP20 (#15): device addresses live in one file outside the release folders, not in the code."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def test_no_ip_address_is_written_into_the_code():
    """The one allowed exception is the trusted-network allow-list of the website, a subnet and not a device."""
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for match in IP.findall(line):
                if match.startswith(("127.", "0.0.0.0")) or "100.64.0.0" in line or "192.168.1.0/24" in line or match in ("255.255.255.0",):
                    continue
                if "documentation" in line.lower() or "example" in line.lower():
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {match}")
    assert not offenders, offenders


def test_the_addresses_come_from_the_file_then_the_environment_then_the_command_line(tmp_path):
    from solarbank_optimiser import devices_config as dc
    path = tmp_path / "devices.conf"
    path.write_text("# my devices\nSOLARBANK_HOST=10.0.0.5\nSOLARBANK_METER_HOST = 10.0.0.6\nSOLARBANK_SHELLY_HOST=shelly.local\n")
    assert dc.resolve(env={}, path=path) == {"solarbank": "10.0.0.5", "meter": "10.0.0.6", "shelly": "shelly.local"}
    assert dc.resolve(env={"SOLARBANK_HOST": "10.0.0.9"}, path=path)["solarbank"] == "10.0.0.9"
    assert dc.resolve(env={"SOLARBANK_HOST": "10.0.0.9"}, path=path, overrides={"solarbank": "10.0.0.11"})["solarbank"] == "10.0.0.11"
    assert dc.resolve(env={}, path=tmp_path / "missing.conf") == {"solarbank": None, "meter": None, "shelly": None}


@pytest.mark.parametrize("text", ["SOLARBANK_HOST=not an address", "SOLARBANK_HOST=1.2.3", "SOLARBANK_HOST=;rm -rf /", "SOLARBANK_HOST=10.0.0.5:502", "SOLARBANK_HOST="])
def test_a_malformed_address_is_refused_with_the_line_it_came_from(tmp_path, text):
    from solarbank_optimiser import devices_config as dc
    path = tmp_path / "devices.conf"; path.write_text(text + "\n")
    with pytest.raises(dc.DeviceConfigError, match="SOLARBANK_HOST"):
        dc.resolve(env={}, path=path)


def test_unknown_keys_and_junk_lines_are_ignored_not_trusted(tmp_path):
    from solarbank_optimiser import devices_config as dc
    path = tmp_path / "devices.conf"; path.write_text("SOMETHING_ELSE=1\nthis is not a setting\nSOLARBANK_HOST=10.0.0.5\n")
    assert dc.resolve(env={}, path=path)["solarbank"] == "10.0.0.5"


def test_a_requested_device_with_no_address_is_a_clear_message_naming_the_file():
    from solarbank_optimiser import devices_config as dc
    hosts = {"solarbank": "10.0.0.5", "meter": None, "shelly": None}
    dc.require(hosts, ("solarbank",))
    with pytest.raises(dc.DeviceConfigError, match=r"meter.*SOLARBANK_METER_HOST.*devices\.conf"):
        dc.require(hosts, ("solarbank", "meter"))


def test_the_direct_config_has_no_built_in_address_and_the_controller_refuses_to_start_unconfigured(tmp_path, monkeypatch, capsys):
    from solarbank_optimiser import acquirer, control_loop
    assert acquirer.DirectConfig().solarbank_host is None
    for key in ("SOLARBANK_HOST", "SOLARBANK_METER_HOST", "SOLARBANK_SHELLY_HOST"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SOLARBANK_DEVICES_FILE", str(tmp_path / "none.conf"))
    code = control_loop.main(["--devices", "solarbank,shelly", "--lock", str(tmp_path / "l.lock"), "--out", str(tmp_path / "o.json"), "--plan", str(tmp_path / "p.json"),
                              "--mode-file", str(tmp_path / "m"), "--log", str(tmp_path / "c.jsonl"), "--ownership-file", str(tmp_path / "own"), "--meter-feed", str(tmp_path / "meter.json")])
    assert code == 4 and "SOLARBANK_HOST" in capsys.readouterr().err


def test_the_controller_unit_does_not_restart_on_the_unconfigured_exit_and_may_reach_the_home_network():
    text = (ROOT / "deployment/solarbank-optimiser-control.service").read_text()
    assert "RestartPreventExitStatus=2 3 4" in text and "IPAddressDeny=any" in text
    allow = re.search(r"IPAddressAllow=(.*)", text).group(1)
    assert "192.168.0.0/16" in allow and not IP.search(allow.replace("192.168.0.0", ""))       # a network, not three single addresses


def test_the_collector_unit_may_reach_the_home_network_not_three_addresses():
    allow = re.search(r"IPAddressAllow=(.*)", (ROOT / "deployment/solarbank-optimiser-collector.service").read_text()).group(1)
    assert "192.168.0.0/16" in allow and "localhost" in allow


def test_activation_creates_the_address_file_with_placeholders_only_if_it_is_missing():      # public copy: placeholders, not the original installation's addresses
    text = (ROOT / "scripts/activate-pi-shadow.sh").read_text()
    assert "if [ ! -f /etc/solarbank-cost-optimiser/devices.conf ]" in text           # created once, never overwritten
    assert "SOLARBANK_HOST=CHANGE-ME-solarbank" in text and "SOLARBANK_METER_HOST=CHANGE-ME-meter" in text and "SOLARBANK_SHELLY_HOST=CHANGE-ME-shelly" in text
