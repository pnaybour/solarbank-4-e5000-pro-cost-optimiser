"""GitHub issue 94: a learned correction of the solar forecast, run in the solver environment as a separate process (JSON job on stdin, JSON result on stdout).

The method is the one EMHASS uses for its "adjusted PV forecast" (`emhass.forecast.Forecast.adjust_pv_forecast_*`, read at 0.18.7), built from the same two
libraries directly, because the pinned EMHASS 0.18.4 could not be checked: scikit-learn regression (Lasso after scaling, its strength chosen by a grid search over
time-ordered cross-validation, so it never trains on the future) on the forecast, the hour of day (sine and cosine) and the sun's elevation (pvlib). As in EMHASS,
the result is zero when the sun is down, faded below a low elevation, and never negative. EMHASS's calendar features (month, day of year) are left out: with a few
weeks of hourly data they would only fit noise. Curtailed hours are removed by the caller before the job is built."""
import json
import os
import sys

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):      # one thread: the planner's unit allows few tasks and half a CPU
    os.environ.setdefault(_name, "1")


def fit_predict(job):
    import numpy as np
    import pandas as pd
    import pvlib
    import sklearn
    from sklearn.linear_model import Lasso
    from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    latitude, longitude = float(job["latitude"]), float(job["longitude"])

    def features(times, forecast):
        index = pd.to_datetime(times, unit="s", utc=True)
        sun = pvlib.solarposition.get_solarposition(index, latitude, longitude)
        hour = index.hour + index.minute / 60.0
        return pd.DataFrame({"forecast": np.asarray(forecast, dtype=float), "hour_sin": np.sin(2 * np.pi * hour / 24.0),
                             "hour_cos": np.cos(2 * np.pi * hour / 24.0), "elevation": sun["apparent_elevation"].to_numpy()}, index=index)

    train = job["train"]
    x = features([r["t"] for r in train], [r["forecast_kw"] for r in train])
    y = np.asarray([r["actual_kw"] for r in train], dtype=float)
    search = GridSearchCV(make_pipeline(StandardScaler(), Lasso(max_iter=20000)), {"lasso__alpha": [0.001, 0.01, 0.1, 1.0]},
                          cv=TimeSeriesSplit(n_splits=min(5, max(2, len(train) // 24))), scoring="neg_mean_squared_error")
    search.fit(x, y)

    rows = job["predict"]
    px = features([(r["start"] + r["end"]) / 2 for r in rows], [r["pv_kw"] for r in rows])
    predicted = search.best_estimator_.predict(px)
    threshold, ceiling = float(job["elevation_threshold"]), float(job["max_kw"])
    hourly = []
    for row, value, elevation in zip(rows, predicted, px["elevation"]):
        if elevation <= 0:
            value = 0.0
        elif elevation < threshold:
            value = value * elevation / threshold
        hourly.append({"start": row["start"], "end": row["end"], "pv_kw": round(min(max(float(value), 0.0), ceiling), 4)})
    return {"status": "ok", "hourly": hourly, "cv_rmse_kw": round(float(np.sqrt(-search.best_score_)), 4),
            "alpha": float(search.best_params_["lasso__alpha"]), "training_hours": len(train),
            "engine": f"scikit-learn {sklearn.__version__} Lasso, pvlib {pvlib.__version__}"}


def main():
    try:
        result = fit_predict(json.load(sys.stdin))
        sys.stdout.write(json.dumps(result, allow_nan=False))
        return 0
    except Exception:
        sys.stdout.write('{"status":"failed"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
