"""The read-only operating measurements and the release tidy-up."""
import os
import time
from pathlib import Path

from solarbank_optimiser import ops_report as ops


def fake_proc(tmp_path, pid, before, after=None, smaps=None):
    folder = tmp_path / "proc" / str(pid)
    folder.mkdir(parents=True)
    (folder / "io").write_text(f"rchar: 1\nwchar: 2\nwrite_bytes: {before}\n")
    if smaps:
        (folder / "smaps_rollup").write_text(smaps)
    return folder


def test_io_rates_are_the_difference_in_write_bytes_over_the_sampling_time(tmp_path):
    folder = fake_proc(tmp_path, 100, 1_000_000)
    ticks = iter([0.0, 10.0])

    def sleep(_seconds):
        (folder / "io").write_text("rchar: 1\nwchar: 2\nwrite_bytes: 1102400\n")      # 102,400 bytes in 10 s = 10 KB/s

    rows = ops.io_rates({"collector": 100}, 10, proc=str(tmp_path / "proc"), sleep=sleep, clock=lambda: next(ticks))
    assert rows[0][0] == "collector" and abs(rows[0][1] - 10.0) < 1e-6 and abs(rows[0][2] - 0.8847) < 0.001
    text = ops.format_io(rows, 10)
    assert "collector" in text and "10.0 KB/s" in text and "total" in text


def test_a_process_that_disappears_or_restarts_is_left_out_not_reported_as_negative(tmp_path):
    folder = fake_proc(tmp_path, 7, 5_000)
    ticks = iter([0.0, 5.0])
    rows = ops.io_rates({"web": 7, "gone": 99999}, 5, proc=str(tmp_path / "proc"), sleep=lambda s: (folder / "io").write_text("write_bytes: 100\n"), clock=lambda: next(ticks))
    assert rows == []


def test_memory_figures_read_rss_pss_and_swap_and_sort_by_pss(tmp_path):
    fake_proc(tmp_path, 1, 0, smaps="Rss:  200000 kB\nPss:  150000 kB\nSwap:  30000 kB\n")
    fake_proc(tmp_path, 2, 0, smaps="Rss:  40000 kB\nPss:  30000 kB\nSwap:  0 kB\n")
    rows = ops.memory_figures({"web": 1, "collector": 2}, proc=str(tmp_path / "proc"))
    assert [r[0] for r in rows] == ["web", "collector"] and abs(rows[0][2] - 146.5) < 0.1
    assert "total" in ops.format_memory(rows)


def test_service_pids_ignores_services_that_are_not_running():
    pids = ops.service_pids(runner=lambda unit: "123" if "web" in unit else "0" if "shadow" in unit else "")
    assert pids == {"web": 123}


def make_releases(tmp_path, names, current, floor=None, age=10 * 86400):
    root, state = tmp_path / "opt", tmp_path / "state"
    (root / "releases").mkdir(parents=True); state.mkdir()
    for name in names:
        folder = root / "releases" / name
        (folder / ".venv").mkdir(parents=True)
        (folder / ".venv" / "big").write_bytes(b"x" * 1000)
        os.utime(folder, (time.time() - age, time.time() - age))
    os.symlink(root / "releases" / current, root / "current")
    if floor:
        (state / "oldest-compatible-release").write_text(floor + "\n")
    return root, state


NAMES = [f"pi-2026110{d}T000000Z-r{d}" for d in range(1, 9)]


def test_the_plan_keeps_current_the_floor_and_the_newest_few_and_lists_the_rest(tmp_path):
    root, state = make_releases(tmp_path, NAMES, current=NAMES[5], floor=NAMES[1])        # r6 is current; r7 and r8 were staged and never activated
    keep, remove = ops.release_plan(root, state)
    kept = {n for n, _b, _w in keep}
    assert NAMES[5] in kept and NAMES[1] in kept                                           # current and the floor
    assert {NAMES[2], NAMES[3], NAMES[4]} <= kept or len(kept) >= 4                        # the newest not newer than current
    gone = {n for n, _b, _w in remove}
    assert NAMES[6] in gone and NAMES[7] in gone and NAMES[0] in gone                      # never activated, and an old one
    assert all(b > 0 for _n, b, _w in remove)


def test_nothing_is_deleted_without_apply_and_apply_never_touches_current_or_a_recent_folder(tmp_path):
    root, state = make_releases(tmp_path, NAMES, current=NAMES[5], floor=NAMES[1])
    os.utime(root / "releases" / NAMES[7], None)                                          # r8 was just staged: a deploy may be running
    _keep, remove = ops.prune_releases(root, state)
    assert (root / "releases" / NAMES[0]).exists() and any(n == NAMES[0] for n, _b, _w in remove)
    assert NAMES[7] not in {n for n, _b, _w in remove}
    ops.prune_releases(root, state, apply=True)
    assert not (root / "releases" / NAMES[0]).exists()
    assert (root / "releases" / NAMES[5]).exists() and (root / "releases" / NAMES[7]).exists() and (root / "releases" / NAMES[1]).exists()
    assert os.path.realpath(root / "current") == str(root / "releases" / NAMES[5])


def test_the_row_cache_keeps_only_this_minute_and_the_last():
    from solarbank_optimiser import minute_source as ms
    assert ms.ROW_CACHE_ENTRIES == 2


def test_profile_status_builds_the_page_a_few_times_and_names_where_the_time_went(tmp_path):
    from solarbank_optimiser.ops_status import profile_status
    from solarbank_optimiser.store import Store
    store = Store(tmp_path / "p.sqlite3"); store.initialise()
    text = profile_status(store, calls=2, top=10)
    assert "builds of /api/status took" in text and "solarbank_optimiser" in text


def test_the_ops_commands_run_from_the_command_line(tmp_path, capsys):
    from solarbank_optimiser.cli import main
    root, state = make_releases(tmp_path, NAMES, current=NAMES[5], floor=NAMES[1])
    main(["--database", str(tmp_path / "d.sqlite3"), "ops", "prune-releases", "--root", str(root), "--state", str(state)])
    out = capsys.readouterr().out
    assert "Would remove" in out and "Nothing was deleted" in out and (root / "releases" / NAMES[0]).exists()


def test_io_and_memory_commands_never_create_the_database_folder(tmp_path, capsys):
    from solarbank_optimiser.cli import main
    missing = tmp_path / "not-there" / "d.sqlite3"
    for action in ("io", "memory"):
        main(["--database", str(missing), "ops", action])
    assert not missing.parent.exists()


def test_services_that_clean_up_on_stop_are_given_longer_than_their_own_cleanup():
    """6 November 20:50: the collector and providers were killed at exactly 30 s. The collector's own stop joins two background threads for up to 15 s each (30 s) and then
    flushes; so the systemd stop timeout must be comfortably longer. The controller keeps its short one: it must release the battery, not wait."""
    import re
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1] / "deployment"
    timeouts = {name: int(re.search(r"^TimeoutStopSec=(\d+)", (folder / f"solarbank-optimiser-{name}.service").read_text(), re.M).group(1))
                for name in ("collector", "providers", "shadow", "web", "control")}
    assert all(timeouts[name] >= 60 for name in ("collector", "providers", "shadow", "web")), timeouts
    assert timeouts["control"] <= 15
