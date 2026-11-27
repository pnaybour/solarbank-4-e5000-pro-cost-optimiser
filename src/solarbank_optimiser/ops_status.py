"""Where the dashboard's status page spends its time (`solarbank ops profile-status`): a few real builds of /api/status under cProfile, against the live database. Read-only in effect:
the page only reads, and nothing is cached between the runs."""
from __future__ import annotations

import cProfile
import io
import pstats
import time


def profile_status(store, calls=3, top=20):
    from .web.app import WebConfig, create_app
    app = create_app(WebConfig(store.path, "profiling-only-password-12345", trusted_network=True, status_cache_seconds=0), store=store)
    build = app.state.status_builder
    build()                                                        # the first build also warms the page cache: not measured
    profile, times = cProfile.Profile(), []
    profile.enable()
    for _ in range(calls):
        started = time.perf_counter()
        build()
        times.append(time.perf_counter() - started)
    profile.disable()
    out = io.StringIO()
    pstats.Stats(profile, stream=out).sort_stats("cumulative").print_stats("solarbank_optimiser", top)
    head = f"{calls} builds of /api/status took {', '.join(f'{t:.2f}' for t in times)} s (under the profiler, so each is slower than the real thing). Where the time went (cumulative, the application's own functions):\n"
    return head + "\n".join(line for line in out.getvalue().splitlines() if line.strip() and ("ncalls" in line or "solarbank_optimiser" in line))
