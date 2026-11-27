"""The planner keeps an hourly log of what it expected the battery level to do (36 hours), which the day view reads for the dotted 'plan' line.
(The status no longer carries the battery history or the earlier forecast: the home page reads the whole day from /api/day.)"""
import json

NOW = 1_793_000_000.0


def test_the_planner_logs_a_forecast_hourly_and_keeps_36_hours(tmp_path):
    import sqlite3
    from solarbank_optimiser.shadow import log_forecast
    db = sqlite3.connect(tmp_path / "l.sqlite3")
    db.execute("CREATE TABLE settings(name TEXT PRIMARY KEY, revision INTEGER, payload TEXT)")
    chart = lambda t: [{"time": t + 300 * k, "soc": 0.5} for k in range(200)]
    stored = lambda: json.loads(db.execute("SELECT payload FROM settings").fetchone()[0])["entries"]
    log_forecast(db, NOW, chart(NOW)); log_forecast(db, NOW + 600, chart(NOW + 600))
    assert len(stored()) == 1 and len(stored()[0]["chart"]) < 40                                    # thinned to half-hour points; no second entry within the hour
    for hour in range(1, 40):
        log_forecast(db, NOW + hour * 3700, chart(NOW + hour * 3700))
    entries = stored()
    assert NOW + 39 * 3700 - entries[0]["issued_at"] <= 36 * 3600 and len(entries) <= 40
