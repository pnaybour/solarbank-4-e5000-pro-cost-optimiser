"""Review items 1 and 8: the history export must work with current records and must not hold a year in memory."""
import csv
import dataclasses
import io
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from solarbank_optimiser.models import FakeClock, Observation
from solarbank_optimiser.store import Store
from solarbank_optimiser.web.app import WebConfig, create_app
from solarbank_optimiser.energy import day_bounds
from datetime import date

PEER = ("192.168.1.50", 1234)


def site(tmp_path):
    store = Store(tmp_path / "h.sqlite3"); store.initialise()
    app = create_app(WebConfig(tmp_path / "h.sqlite3", "a-long-test-password", trusted_network=True, status_cache_seconds=0),
                     FakeClock(1_800_000_000), store=store)
    return store, TestClient(app, base_url="http://solarbank-pi.local", client=PEER)


def minute_rows(first, count, metric="soc", with_summary=True, offset=0):
    unit = {"soc": "fraction", "household_power": "kW", "pv_power": "kW"}[metric]
    extra = dict(window_samples=60, window_ok=60, window_mean=.5, window_max=.9) if with_summary else {}
    return [Observation("monitor:test", metric, first + 60 * i + 60 + offset, .5, unit, source_at=first + 60 * i + 59 + offset,
                        provenance="shared-monitor-observation", **extra) for i in range(count)]


def put_rows_directly(store, rows):
    """Rows as an earlier release's collector wrote them: into the database only (the suite's mirror would also put their minutes in the telemetry files)."""
    from contextlib import closing
    from dataclasses import asdict
    from solarbank_optimiser.models import encoded
    with closing(store.connect()) as db, db:
        db.executemany("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)", [(o.id, o.source, o.metric, o.sample_at, encoded(asdict(o))) for o in rows])


def test_export_with_current_minute_summary_records_works_and_has_every_field(tmp_path):
    store, client = site(tmp_path)
    first = day_bounds(date(2026, 10, 20))[0]
    put_rows_directly(store, minute_rows(first, 5) + minute_rows(first, 5, "household_power"))
    response = client.get("/history.csv?start=2026-10-20&end=2026-10-20")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 10
    assert list(rows[0]) == [f.name for f in dataclasses.fields(Observation)]          # nothing stored is left out
    assert {"window_samples", "window_ok", "window_mean", "window_max"} <= set(rows[0])
    assert rows[0]["window_samples"] == "60" and rows[0]["window_max"] == "0.9"


def test_old_records_without_the_summary_fields_still_export(tmp_path):
    store, client = site(tmp_path)
    first = day_bounds(date(2026, 10, 20))[0]
    store.add_observations(minute_rows(first, 3, with_summary=False))
    rows = list(csv.DictReader(io.StringIO(client.get("/history.csv?start=2026-10-20&end=2026-10-20").text)))
    assert len(rows) == 3 and rows[0]["window_samples"] == ""


def test_json_history_pages_return_every_row_exactly_once_in_order(tmp_path):
    store, client = site(tmp_path)
    first = day_bounds(date(2026, 10, 20))[0]
    store.add_observations(minute_rows(first, 7) + minute_rows(first, 5, "household_power") + minute_rows(first, 4, "pv_power"))
    seen, cursor, pages = [], None, 0
    while True:
        url = "/api/history?start=2026-10-20&end=2026-10-20&limit=3" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(url).json()
        assert len(body["observations"]) <= 3
        seen += [(o["metric"], o["retrieved_at"]) for o in body["observations"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 16 == len(set(seen)) and pages >= 6
    assert seen == sorted(seen, key=lambda x: (["household_power", "pv_power", "soc"].index(x[0]), x[1]))


@pytest.mark.parametrize("query", ["limit=0", "limit=10001", "limit=abc", "cursor=notbase64!!", "cursor=e30="])
def test_bad_page_requests_are_refused_not_crashed(tmp_path, query):
    _, client = site(tmp_path)
    assert client.get(f"/api/history?start=2026-10-20&end=2026-10-20&{query}").status_code == 422


def test_a_large_export_streams_without_holding_it_all_in_memory(tmp_path):
    store, client = site(tmp_path)
    first = day_bounds(date(2026, 10, 20))[0]
    for part in range(21):                                          # 30,240 records, all inside one London day
        store.add_observations(minute_rows(first, 1440, with_summary=False, offset=part * 0.001))
    end = first + 86400
    tracemalloc.start()
    old_way = store.observations(first, end)                          # what the endpoint used to do: everything at once
    _, old_peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    del old_way
    tracemalloc.start()
    total = 0
    with client.stream("GET", "/history.csv?start=2026-10-20&end=2026-10-20") as response:
        assert response.status_code == 200
        for chunk in response.iter_bytes():
            total += len(chunk)
    _, new_peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    assert total > 3_000_000
    # The test client keeps the whole body it receives, which a real browser connection does not, yet the peak is still far lower.
    assert new_peak < old_peak / 3, (new_peak, old_peak)


def test_invalid_ranges_are_still_rejected_before_any_streaming(tmp_path):
    _, client = site(tmp_path)
    assert client.get("/history.csv?start=2026-10-17&end=2026-10-10").status_code == 422
    assert client.get("/history.csv?start=2025-01-01&end=2026-10-10").status_code == 422


def test_a_minute_the_files_hold_is_exported_once_from_the_files_and_an_older_summary_of_it_is_not_repeated(tmp_path):
    """The telemetry files are the store of readings: the export reads them, and the collector's old summary row of the same minute is not given a second time."""
    store, client = site(tmp_path)
    first = day_bounds(date(2026, 10, 20))[0]
    store.add_observations(minute_rows(first, 4))                      # the suite's mirror puts these four minutes in the telemetry files as well
    rows = list(csv.DictReader(io.StringIO(client.get("/history.csv?start=2026-10-20&end=2026-10-20").text)))
    assert [r["source"] for r in rows] == ["telemetry-files"] * 4
    assert [float(r["value"]) for r in rows] == [0.5] * 4 and {r["unit"] for r in rows} == {"fraction"}
