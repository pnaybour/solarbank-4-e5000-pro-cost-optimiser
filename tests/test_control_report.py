"""The register write journal summary and the logged-mode commands (the soak before go-live)."""
import json
import subprocess
from pathlib import Path

from solarbank_optimiser.control_report import summarise_writes

T0 = 1_793_900_000


def journal(path, kinds):
    lines = [json.dumps({"t": T0 + i, "register": reg, "class": "mode" if reg == 10064 else "setpoint", "value": value, "reason": "plan", "kind": kind,
                         **({"why_not_sent": "logged mode"} if kind == "would_write" else {})}) for i, (kind, reg, value) in enumerate(kinds)]
    path.write_text("\n".join(lines) + "\n")


def test_a_missing_journal_is_explained_not_an_error(tmp_path):
    text, sent = summarise_writes(tmp_path / "none.jsonl")
    assert not sent and "shadow mode" in text and "logged" in text


def test_a_journal_of_would_be_writes_says_nothing_was_sent_and_names_each_register_in_words(tmp_path):
    path = tmp_path / "w.jsonl"
    journal(path, [("would_write", 10064, 3), ("would_write", 10071, 1500), ("would_write", 10071, 0), ("would_write", 10064, 6)])
    text, sent = summarise_writes(path)
    assert not sent and "nothing was sent" in text
    assert "operating mode -> 3 (the mode the controller drives)" in text and "battery power set-point -> 1500 W" in text and "6 (the Anker's own Dynamic Tariff)" in text
    assert "Why not sent: logged mode (4)" in text


def test_verification_records_are_listed_as_records_never_as_writes(tmp_path):
    """GitHub issue 72: the journal also carries 11.4/10.4 records, which have no register and must not read as 'register None -> None W'."""
    path = tmp_path / "w.jsonl"
    journal(path, [("wrote", 10071, -1500)])
    with open(path, "a") as handle:
        handle.write(json.dumps({"t": T0 + 9, "kind": "record", "record": "possible sign inversion", "ac_supplied_w": 800.0}) + "\n")
    text, sent = summarise_writes(path)
    assert "register None" not in text and "Records (what the battery showed, not writes): possible sign inversion (1)" in text
    assert "record: possible sign inversion" in text                         # the newest entries name it too


def test_a_write_that_was_sent_or_failed_is_flagged_loudly(tmp_path):
    for bad in ("wrote", "failed"):
        path = tmp_path / f"{bad}.jsonl"
        journal(path, [("would_write", 10064, 3), (bad, 10071, 500)])
        text, sent = summarise_writes(path)
        assert sent and "SENT or attempted" in text


def test_the_rotated_file_is_read_first_and_unreadable_lines_are_skipped(tmp_path):
    path = tmp_path / "w.jsonl"
    journal(Path(str(path) + ".1"), [("would_write", 10064, 0)])
    path.write_text("not json\n" + json.dumps({"t": T0 + 9, "register": 10064, "value": 3, "kind": "would_write"}) + "\n")
    text, sent = summarise_writes(path)
    assert "2 journal entries" in text and "1 unreadable" in text and not sent


def test_the_command_exits_one_when_a_write_was_sent_and_zero_otherwise(tmp_path):
    from solarbank_optimiser.cli import main
    import pytest
    ok, bad = tmp_path / "ok.jsonl", tmp_path / "bad.jsonl"
    journal(ok, [("would_write", 10064, 3)])
    journal(bad, [("wrote", 10064, 3)])
    main(["--database", str(tmp_path / "d.sqlite3"), "control", "writes", "--journal", str(ok)])
    with pytest.raises(SystemExit) as stopped:
        main(["--database", str(tmp_path / "d.sqlite3"), "control", "writes", "--journal", str(bad)])
    assert stopped.value.code == 1


def test_the_mode_commands_set_only_shadow_or_logged_and_never_live():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "pi-tools.sh").read_text()
    assert "logged-on) set_mode logged" in text and "logged-off) set_mode shadow" in text and "set_mode live" not in text
    assert 'case "$1" in shadow|logged) ;;' in text
    done = subprocess.run(["bash", "-n", str(Path(__file__).resolve().parents[1] / "scripts" / "pi-tools.sh")])
    assert done.returncode == 0


def test_the_summary_says_what_a_logged_night_does_not_prove(tmp_path):
    path = tmp_path / "w.jsonl"
    journal(path, [("would_write", 10064, 3)])
    text, _ = summarise_writes(path)
    assert "NOT show" in text and "identity" in text and "HANDS_OFF" in text


def test_the_docs_no_longer_say_live_cannot_send(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in ("OPERATIONS.md", "POST_SHADOW_PLAN.md", "SOFTWARE_SPECIFICATION.md", "src/solarbank_optimiser/live_transport.py"):
        text = (root / name).read_text()
        assert "cannot send anything yet" not in text and "No transport that sends is installed" not in text, name


def test_entries_carry_wall_clock_time_shown_in_london_time_and_old_ones_still_read(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text(json.dumps({"t": 499019.0, "wall": 1_793_943_605.0, "register": 10064, "value": 0, "kind": "would_write"}) + "\n"
                    + json.dumps({"t": 499020.0, "register": 10071, "value": 0, "kind": "would_write"}) + "\n")
    text, _ = summarise_writes(path)
    assert "06 Nov 05:40:05" in text and "t=499020.0" in text
