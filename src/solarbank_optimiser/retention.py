"""Retention: keep the long-term records, purge the detail after several days (owner decision, 28 October 2026).

KEPT FOR GOOD: the half-hour records (aligned to Octopus's settlement periods, `half_hours:<date>`), the daily records (`daily_record:<date>`), the saved evening-before solar
forecasts, all settings and their history, car-charging sessions and energy, and imported history. These are small.

PURGED after KEEP_DETAIL_DAYS (7): the archived plan, forecast and slot snapshots (`issuances`), the one-minute observation rows, and the history of OPERATIONAL settings
(caches and health records that change every minute, with their "settings changed" event rows). The history of the settings YOU make (demand, tariff, providers, minimum level,
operating state...) is kept for good. Seven days is deliberate: the planner picks its
Normal reference day from the last seven days of minute rows, so nothing it needs is removed. A day's minute rows are purged only if that day's half-hour record exists, and an archive
snapshot that a current setting still points to is never purged. Sums are never rebuilt from purged detail: the half-hour record is the long-term truth.

Deleting rows frees space inside the database file for reuse; the file itself does not shrink.
"""
from contextlib import closing
from datetime import datetime, timedelta
import re

from .daily_record import HALF_HOUR_PREFIX
from .energy import LONDON, day_bounds
from .models import UNITS

KEEP_DETAIL_DAYS = 7
MIN_KEEP_DAYS = 7                      # the Normal reference day is chosen from the last seven days of minute rows
PURGED_PROVENANCE = ("shared-monitor-observation", "live")      # imported history ("seed") is never touched
HEX64 = re.compile(r"[0-9a-f]{64}")
SETTING = "retention_last"
# Settings the system rewrites constantly. Their old versions are noise; the user's own settings are not in this list and keep their whole history.
OPERATIONAL_SETTINGS = ("forecast_summary", "shadow_attempt", "cache_slots", "provider_slots", "established_slots", "cache_weather", "cache_prices",
                        "provider_prices", "provider_weather", "shadow_review", "collector_health")


def protected_issuance_ids(store):
    """Every archive id that a current setting still refers to (the active plan, the weather behind it, the latest forecast...)."""
    found = set()
    with closing(store.connect()) as db:
        for (payload,) in db.execute("SELECT payload FROM settings WHERE payload LIKE '%issuance%' OR payload LIKE '%reference%'"):
            found.update(HEX64.findall(payload))
    return found


def _delete(db, table, sql, args):
    """One DELETE inside the caller's open transaction. Append-only tables refuse deletion by a trigger; the trigger is dropped and put back EXACTLY as it was within the
    same transaction, so a failure rolls both back and the protection is never left off."""
    triggers = [] if table is None else db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND sql LIKE '%BEFORE DELETE%'", (table,)).fetchall()
    for name, _ in triggers:
        db.execute(f'DROP TRIGGER "{name}"')
    count = db.execute(sql, args).rowcount
    for _, definition in triggers:
        db.execute(definition)
    return count


def _delete_batches(store, sql, args, batch, table=None):
    """Run a DELETE ... LIMIT ? repeatedly, each in its own small transaction."""
    total = 0
    while True:
        with closing(store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            n = _delete(db, table, sql, (*args, batch))
        total += n
        if n < batch:
            return total


def purge_detail(store, now, keep_days=KEEP_DETAIL_DAYS, issuance_hours=None, dry_run=False, batch=400):
    """Returns what was (or, with dry_run, would be) removed. keep_days is held at 7 or more."""
    keep_days = max(int(keep_days), MIN_KEEP_DAYS)
    today = datetime.fromtimestamp(now, LONDON).date()
    cutoff_day = today - timedelta(days=keep_days)
    cutoff = day_bounds(cutoff_day)[0]
    issuance_cutoff = now - issuance_hours * 3600 if issuance_hours is not None else cutoff
    result = {"keep_days": keep_days, "cutoff": cutoff_day.isoformat(), "dry_run": bool(dry_run), "issuances_deleted": 0,
              "setting_versions_deleted": 0, "events_deleted": 0, "observations_deleted": 0, "days_purged": [], "days_kept_no_half_hour_record": []}
    # 1. archived snapshots
    protected = protected_issuance_ids(store)
    with closing(store.connect()) as db:
        old = [r[0] for r in db.execute("SELECT id FROM issuances WHERE acquired_at<?", (issuance_cutoff,)) if r[0] not in protected]
    result["issuances_deleted"] = len(old)
    if not dry_run:
        for i in range(0, len(old), batch):
            chunk = old[i:i + batch]
            with closing(store.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                _delete(db, "issuances", f"DELETE FROM issuances WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    # 1b. old versions of operational settings, and their "settings changed" events
    marks = ",".join("?" * len(OPERATIONAL_SETTINGS))
    with closing(store.connect()) as db:
        versions = db.execute(f"SELECT count(*) FROM setting_versions WHERE known_at<? AND name IN ({marks})", (cutoff, *OPERATIONAL_SETTINGS)).fetchone()[0]
        events = db.execute(f"SELECT count(*) FROM events WHERE kind='settings_changed' AND time<? AND json_extract(payload,'$.name') IN ({marks})", (cutoff, *OPERATIONAL_SETTINGS)).fetchone()[0]
    result["setting_versions_deleted"], result["events_deleted"] = versions, events
    if not dry_run:
        _delete_batches(store, f"DELETE FROM setting_versions WHERE rowid IN (SELECT rowid FROM setting_versions WHERE known_at<? AND name IN ({marks}) LIMIT ?)",
                        (cutoff, *OPERATIONAL_SETTINGS), batch, "setting_versions")
        _delete_batches(store, f"DELETE FROM events WHERE id IN (SELECT id FROM events WHERE kind='settings_changed' AND time<? AND json_extract(payload,'$.name') IN ({marks}) LIMIT ?)",
                        (cutoff, *OPERATIONAL_SETTINGS), batch)
    # 2. one-minute observation rows, day by day, only where the half-hour record is safely on file
    with closing(store.connect()) as db:
        firsts = [db.execute("SELECT min(sample_at) FROM observations WHERE metric=? AND sample_at<?", (m, cutoff)).fetchone()[0] for m in UNITS]
    firsts = [f for f in firsts if f is not None]
    if firsts:
        day = datetime.fromtimestamp(min(firsts), LONDON).date()
        while day < cutoff_day:
            start, end = day_bounds(day)
            record = store.get_setting(HALF_HOUR_PREFIX + day.isoformat())[1]
            if not record or not record.get("slots"):
                result["days_kept_no_half_hour_record"].append(day.isoformat())
            else:
                count = 0
                for metric in UNITS:
                    if dry_run:
                        with closing(store.connect()) as db:
                            count += db.execute("SELECT count(*) FROM observations WHERE metric=? AND sample_at>=? AND sample_at<? AND json_extract(payload,'$.provenance') IN (?,?)",
                                                (metric, start, end, *PURGED_PROVENANCE)).fetchone()[0]
                    else:
                        count += _delete_batches(
                            store, "DELETE FROM observations WHERE rowid IN (SELECT rowid FROM observations WHERE metric=? AND sample_at>=? AND sample_at<? "
                                   "AND json_extract(payload,'$.provenance') IN (?,?) LIMIT ?)", (metric, start, end, *PURGED_PROVENANCE), batch)
                if count:
                    result["days_purged"].append(day.isoformat())
                result["observations_deleted"] += count
            day += timedelta(days=1)
    if not dry_run:
        revision, _ = store.get_setting(SETTING)
        store.set_setting(SETTING, {**result, "at": now}, now, revision)
    return result
