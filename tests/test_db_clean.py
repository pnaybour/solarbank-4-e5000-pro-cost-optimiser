"""GitHub 46: the database report and the clean-out."""
from contextlib import closing

import pytest

from solarbank_optimiser import db_clean
from solarbank_optimiser.store import Store
from solarbank_optimiser.telemetry_backfill import backfill

from test_daily_record import DAY, add_day, put
from test_minute_source import END, OLD, START


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "d.sqlite3"); s.initialise()
    put(s, "fallback_tariff", {"day_pence": 30.0}, 1.0)
    return s


def rows(store, sql="SELECT count(*) FROM observations"):
    with closing(store.connect()) as db:
        return db.execute(sql).fetchone()[0]


def test_the_report_lists_every_table_and_marks_the_protected_ones(store):
    add_day(store, DAY)
    result = db_clean.report(store)
    names = {t["name"]: t for t in result["tables"]}
    assert names["observations"]["rows"] == 6 * 1440 and names["settings"]["protected"] and not names["observations"]["protected"]          # five daily-record metrics and the car (add_day, record version 4)


def test_a_dry_run_changes_nothing_and_apply_refuses_until_the_files_agree(store, tmp_path):
    add_day(store, DAY)
    before = rows(store)
    result = db_clean.clean(store, tmp_path / "t", OLD, tmp_path / "b")
    assert not result["applied"] and rows(store) == before and not result["plan"]["agreement_ok"]
    with pytest.raises(RuntimeError, match="do not agree"):
        db_clean.clean(store, tmp_path / "t", OLD, tmp_path / "b", apply=True)
    assert rows(store) == before and not (tmp_path / "b").exists()


def test_apply_backs_up_first_removes_the_verified_minute_rows_and_keeps_the_settings(store, tmp_path):
    add_day(store, DAY)
    root = tmp_path / "t"
    backfill(store, root, OLD)
    result = db_clean.clean(store, root, OLD, tmp_path / "b", apply=True)
    assert result["applied"] and (tmp_path / "b").exists() and any((tmp_path / "b").iterdir())
    assert rows(store, "SELECT count(*) FROM observations WHERE json_extract(payload,'$.window_mean') IS NOT NULL") == 0
    assert store.get_setting("fallback_tariff")[1] == {"day_pence": 30.0}


def test_the_clean_out_keeps_two_days_of_archived_snapshots_not_seven(store, tmp_path):
    from solarbank_optimiser.retention import purge_detail
    now = OLD
    with closing(store.connect()) as db, db:
        for hours in (1, 30, 60, 100):                                 # snapshots this many hours old
            db.execute("INSERT INTO issuances VALUES(?,?,?,?)", (f"i{hours}", now - hours * 3600, '{"kind":"other"}', b"x"))
    plan = db_clean.plan(store, tmp_path / "t", now)
    assert plan["detail"]["issuances_deleted"] == 2                    # the 60 and 100 hour old ones
    assert purge_detail(store, now, dry_run=True)["issuances_deleted"] == 0     # the ordinary retention (seven days) would remove none of them


def test_a_gap_at_the_live_edge_does_not_block_cleaning_older_rows(store, tmp_path):
    import whisper
    add_day(store, DAY)
    root = tmp_path / "t"
    backfill(store, root, OLD)
    # a minute inside the last two days that the files cannot vouch for (a restart cut it)
    recent = int(OLD) // 60 * 60 - 3600
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO observations VALUES('r1','fixture','household_power',?,?)",
                   (recent + 60.0, '{"metric":"household_power","value":1.0,"unit":"kW","quality":"ok","window_mean":1.0,"window_ok":60,"window_max":1.0,"sample_at":%d}' % (recent + 60)))
    result = db_clean.clean(store, root, OLD, tmp_path / "b", apply=True)
    assert result["applied"]


# ---- GitHub 53: every metric the clean-out can delete is compared first ------------------------------------------------------------------------------
def _uncounted_setup(store, tmp_path):
    from test_minute_source import window_row
    minute = START + 3600
    store.add_observations([window_row("ac_power", minute, 1.0, 60), window_row("charge_energy", minute, 100.0, 60, peak=100.4), window_row("household_power", minute, 0.5, 60)])
    root = tmp_path / "t"
    backfill(store, root, OLD)
    return minute, root


def test_a_metric_without_a_count_file_is_compared_and_a_wrong_file_stops_the_clean_out(store, tmp_path):
    import whisper
    from solarbank_optimiser.telemetry_backfill import agreement
    minute, root = _uncounted_setup(store, tmp_path)
    assert agreement(store, root, OLD)["ok"]
    whisper.update_many(str(root / "ac_power.wsp"), [(minute, 1.5)], now=int(OLD))                      # half a kW off in the file
    result = agreement(store, root, OLD)
    assert not result["ok"] and result["metrics"]["ac_power"]["mean_differences"] == 1
    with pytest.raises(RuntimeError, match="do not agree"):
        db_clean.clean(store, root, OLD, tmp_path / "b", apply=True)
    assert rows(store, "SELECT count(*) FROM observations WHERE metric='ac_power'") == 1               # nothing was deleted


def test_a_counter_kept_as_the_largest_value_of_the_minute_is_judged_between_the_mean_and_the_maximum(store, tmp_path):
    import whisper
    from solarbank_optimiser.telemetry_backfill import agreement
    minute, root = _uncounted_setup(store, tmp_path)
    whisper.update_many(str(root / "charge_energy.wsp"), [(minute, 100.3)], now=int(OLD))               # a live minute's maximum: between 100.0 and 100.4
    assert agreement(store, root, OLD)["ok"]
    whisper.update_many(str(root / "charge_energy.wsp"), [(minute, 101.5)], now=int(OLD))
    assert not agreement(store, root, OLD)["ok"]


def test_the_clean_out_deletes_only_metrics_that_were_compared_and_a_backup_can_be_verified_afterwards(store, tmp_path):
    minute, root = _uncounted_setup(store, tmp_path)
    plan = db_clean.plan(store, root, OLD)
    assert plan["agreement_ok"] and {"ac_power", "charge_energy", "household_power"} <= set(plan["metrics_checked"])
    backup = tmp_path / "copy.sqlite3"
    store.backup(backup)
    result = db_clean.clean(store, root, OLD, tmp_path / "b", apply=True)
    assert result["applied"] and rows(store, "SELECT count(*) FROM observations WHERE json_extract(payload,'$.window_mean') IS NOT NULL") == 0
    verdict = db_clean.verify_backup(backup, root, OLD, hours_back=0)                                    # the copy still has the rows; the files hold the same
    assert verdict["ok"] and verdict["metrics"]["ac_power"]["compared"] == 1
