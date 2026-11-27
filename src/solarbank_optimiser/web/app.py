import base64
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import csv
import hashlib
import hmac
import io
from pathlib import Path
import secrets
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..car_history import car_history, car_power_minutes
from ..household_policy import household_policy
from ..collector import PROVENANCE
from .. import freshness as freshness_limits
from ..battery_limits import DEFAULT_MIN_SOC, MIN_SOC_HIGHEST, MIN_SOC_LOWEST, SETTING as MIN_SOC_SETTING, valid_min_soc
from ..calibration import MAX_DAYS as CALIBRATION_MAX_DAYS, MIN_DAYS as CALIBRATION_MIN_DAYS, SETTING as CALIBRATION_SETTING, state as calibration_state, stored_days as calibration_days
from ..validation import KINDS, all_differences, report as build_validation_report
from ..tariff_window import effective_tariff, unused_reason, describe as describe_window
from ..automation import MAX_OVERRIDE_S, OVERRIDE_SETTING, PAUSE_SETTING, describe as describe_automation
from ..anker_view import describe as describe_anker
from ..daily_record import HALF_HOUR_PREFIX, summary as savings_summary
from ..services import FORECAST_FRESH_SECONDS
from ..realtime_status import assess as assess_realtime
from ..control import request_handover
from ..energy import LONDON, day_bounds
from ..models import Clock, Observation
from ..settings import DemandSelection, FallbackTariff, london_input
from ..services import ProviderConfig, SecretFile, provider_view, tariff_horizon, window_cache
from ..store import Store
from ..shadow import plan_view
from contextlib import asynccontextmanager, closing
import json
import ipaddress
import threading
import time

STALE_SERVE_S = 15.0          # a previous status picture up to this old is served at once while the next is built (it is marked stale beyond status_max_age_seconds)


def lan_prefixes(path):
    """Network prefixes (one per line, # comments) from the one configuration file; an unreadable file or a line that is not a network gives none of it."""
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                try:
                    ipaddress.ip_network(line)
                except ValueError:
                    continue
                out.append(line)
    except OSError:
        pass
    return out


@dataclass(frozen=True)
class WebConfig:
    database: Path
    password: str
    username: str = "solarbank"
    display_notice: str | None = None
    trusted_network: bool = False
    lan_prefixes_path: str = "/etc/solarbank-cost-optimiser/lan-prefixes"      # the home network's prefixes beyond the fixed private ranges (GitHub 45)
    # Request worker threads. Bounded so a slow disk makes requests queue instead of exhausting the
    # service's task limit (a burst of polling once caused "can't start new thread" and HTTP 500s).
    max_request_threads: int = 16
    # The status page costs about a second of Pi CPU to build. Share one result between every browser
    # and tab for this long (cleared by any change), so polling cannot saturate the service.
    status_cache_seconds: float = 3.0
    status_max_age_seconds: float = 30.0       # a status picture older than this is marked stale by the server and refused by the browser (#22)
    # Login-free (trusted network) mode answers only to these names, plus plain IP addresses and *.ts.net names. A page on any other
    # name (DNS rebinding) is refused, so a browser cannot be tricked into handing a stranger the request token.
    allowed_hosts: tuple = ("solarbank-pi.local", "solarbank-pi", "localhost")
    # Where the controller writes its status (RAM). None means this site does not report on a controller at all.
    control_state_path: Path | None = None

    def __post_init__(self):
        if type(self.max_request_threads) is not int or not 2 <= self.max_request_threads <= 64:
            raise ValueError("Request threads must be 2 to 64")
        if not 0 <= self.status_cache_seconds <= 30:
            raise ValueError("Status cache must be 0 to 30 seconds")
        if not isinstance(self.allowed_hosts, tuple) or not all(isinstance(h, str) and h and h == h.lower() and "/" not in h and "@" not in h
                                                                 for h in self.allowed_hosts):
            raise ValueError("Allowed hosts must be a tuple of lower-case host names")
        if len(self.password) < 10:
            raise ValueError("Use a dashboard password of at least 10 characters")


class SaveDemand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    selection: DemandSelection


class SaveTariff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    tariff: FallbackTariff


class SaveSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    mode: str
    start_local: str | None = None
    end_local: str | None = None
    start_fold: int | None = Field(default=None, ge=0, le=1)
    end_fold: int | None = Field(default=None, ge=0, le=1)
    away_watts: float | None = None
    high_extra_kwh: float | None = None




class SaveDemandLearning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class SaveDeviceAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str
    host: str = Field(min_length=1, max_length=253)


class SaveAutomation(BaseModel):
    """Pause the automation, force a charge level for a while, or go back to automatic. Times are London clock times."""
    model_config = ConfigDict(extra="forbid")
    action: str
    until_local: str | None = None
    until_fold: int | None = Field(default=None, ge=0, le=1)
    target_percent: float | None = Field(default=None, ge=5, le=100)


class SaveCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    days: int = Field(ge=CALIBRATION_MIN_DAYS, le=CALIBRATION_MAX_DAYS, description="a full charge at least this often")


class SaveFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    charge_s: float = Field(ge=freshness_limits.CHARGE_RANGE[0], le=freshness_limits.CHARGE_RANGE[1], description="oldest garage current reading acted on while a charge is under way")
    idle_s: float = Field(ge=freshness_limits.IDLE_RANGE[0], le=freshness_limits.IDLE_RANGE[1], description="oldest reading acted on otherwise, and every other reading always (owner specification v1.3 STALE_S: 10)")


class SaveMinSoc(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    value: float = Field(ge=MIN_SOC_LOWEST, le=MIN_SOC_HIGHEST, description="fraction of capacity, 0.05 to 0.20")


class SaveProviders(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    config: ProviderConfig


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    mode: str


def read_control_state(path):
    """The controller's status file, or None if it is missing, unreadable, not an object or oversized."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(262144)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def host_allowed(header, names):
    """Is this Host header one this dashboard should answer to? (names, IP addresses, or a Tailscale *.ts.net name)"""
    host = (header or "").strip().lower()
    if not host or any(c in host for c in "/@\\ ?#"):
        return False
    if host.startswith("["):                                  # [IPv6]:port
        end = host.find("]")
        host = host[1:end] if end > 0 and (host[end + 1:] == "" or host[end + 1:].startswith(":")) else ""
    elif host.count(":") == 1:
        host, port = host.split(":")
        if not port.isdigit():
            return False
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True                                           # a bare address cannot be a rebinding name; clients are limited separately
    except ValueError:
        pass
    return host in names or (host.endswith(".ts.net") and len(host) > 7)


def create_app(config, clock=None, store=None):
    """`store` lets the service share the command line's tuned store, which knows the live-readings file."""
    clock = clock or Clock()
    store = store or Store(config.database)
    store.initialise()
    @asynccontextmanager
    async def lifespan(app):
        import anyio.to_thread
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = config.max_request_threads
        app.state.request_threads = limiter.total_tokens
        yield

    app = FastAPI(title="Solarbank Cost Optimiser", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    basic = (lambda: None) if config.trusted_network else HTTPBasic(auto_error=False)
    csrf = hmac.new(config.password.encode(), b"solarbank-csrf-v1", hashlib.sha256).hexdigest()
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.exception_handler(sqlite3.OperationalError)
    async def database_busy(request, exc):
        """A write that waited out the busy timeout while another process held the database: say so plainly (and ask for a retry) instead of a bare server error."""
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return JSONResponse(status_code=503, content={"detail": "The database is busy for a moment. Please try again."}, headers={"Retry-After": "2"})
        raise exc

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Never echo submitted values, even if a key is pasted into a wrong field.
        return JSONResponse(status_code=422, content={"detail": "Check the settings, dates and required fields."})

    def authenticated(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)):
        if config.trusted_network:
            try:
                peer=ipaddress.ip_address(request.client.host)
                allowed=any(peer in ipaddress.ip_network(cidr) for cidr in
                            ('127.0.0.0/8','::1/128','192.168.1.0/24','100.64.0.0/10',
                             'fe80::/10','fc00::/7','fd7a:115c:a1e0::/48', *lan_prefixes(config.lan_prefixes_path)))   # IPv6: same ranges as the nginx allow-list
            except ValueError:
                allowed=False
            if not allowed:
                raise HTTPException(403,'This dashboard is limited to the home LAN and private Tailscale network')
            return
        if credentials is None:
            raise HTTPException(401,'Dashboard login required',headers={'WWW-Authenticate':'Basic'})
        valid_user = secrets.compare_digest(credentials.username.encode(), config.username.encode())
        valid_password = secrets.compare_digest(credentials.password.encode(), config.password.encode())
        if not valid_user or not valid_password:
            raise HTTPException(401, "Dashboard login required", headers={"WWW-Authenticate": "Basic"})

    def write_access(request: Request, _=Depends(authenticated)):
        if request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "A same-origin dashboard request is required")
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
            raise HTTPException(403, "Missing or invalid request token")

    def key_entry_allowed(request: Request):
        return request.url.scheme == "https" or request.client.host in {"127.0.0.1", "::1"}

    status_cache = {"at": 0.0, "value": None, "computing": False, "gen": 0}          # `gen` counts attempted changes: a picture built before one is never stored after it
    status_cond = threading.Condition()

    @app.middleware("http")
    async def only_known_hosts(request, call_next):
        if config.trusted_network and not host_allowed(request.headers.get("host"), config.allowed_hosts):
            return JSONResponse(status_code=421, content={"detail": "This dashboard does not answer to that address."},
                                headers={"Cache-Control": "no-store"})
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            status_cache["value"] = None          # a change was attempted: never serve the old picture
            status_cache["gen"] += 1                # ...and a picture still being built from before the change must not be stored when it finishes
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    def served(value):
        """What goes out: the picture with its age. The server marks it stale beyond the limit and the page applies the same limit from the same numbers."""
        now = clock.utc()
        age = max(0.0, now - value["computed_at"])
        out = {**value, "served_at": now, "stale": age > config.status_max_age_seconds, "status_max_age_seconds": config.status_max_age_seconds}
        if out["stale"]:
            out["stale_seconds"] = round(age)
        return out

    def status():
        with store.pinned_live():          # one consistent snapshot of the live readings for the whole page
            return build_status()

    app.state.status_builder = status      # `solarbank ops profile-status` times this directly, in its own thread (a profiler only sees the thread it runs in)

    def calibration_view(now):
        """Days since the last full charge and whether one is due (the lookup itself is cached for ten minutes in calibration.py)."""
        try:
            return calibration_state(store, now, calibration_days(store.get_setting(CALIBRATION_SETTING, {})[1]))
        except Exception:
            return None

    def device_floor(min_soc):
        """D13: the minimum level the Solarbank itself holds (its discharge limit register, set in the Anker app) beside the Settings value, with a flag when they differ."""
        try:
            from ..live_transport import device_limits_from_log
            percent = device_limits_from_log(store.path.parent / "device-settings.jsonl").get("discharge_limit_soc")
        except Exception:
            percent = None
        if percent is None:
            return None
        return {"percent": percent, "settings_percent": round(min_soc * 100), "mismatch": abs(percent - min_soc * 100) >= 1.0}

    def learning_summary():
        try:
            from ..demand_learning import describe
            return describe(store)
        except Exception:
            return None

    def build_status():
        now = clock.utc()
        revision, selection = store.get_setting("demand")
        demand_dates = None
        if selection:
            local = lambda t: datetime.fromtimestamp(t, LONDON)
            demand_dates = {"start_local": local(selection["start"]).strftime("%Y-%m-%dT%H:%M"),
                            "start_fold": local(selection["start"]).fold,
                            "end_local": local(selection["end"]).strftime("%Y-%m-%dT%H:%M") if selection.get("end") else None,
                            "end_fold": local(selection["end"]).fold if selection.get("end") else None}
        tariff_revision, tariff = store.get_setting("fallback_tariff")
        min_soc_revision, min_soc_payload = store.get_setting(MIN_SOC_SETTING, {})
        min_soc = valid_min_soc((min_soc_payload or {}).get("value")) or DEFAULT_MIN_SOC
        saved_tariff, product = tariff, store.get_setting("providers", {})[1].get("product")
        usable = window_cache(store, now)
        tariff, window_source = effective_tariff(tariff, usable, product)
        tariff = FallbackTariff(**tariff).model_dump() if tariff else None
        night_window = describe_window(tariff, window_source, saved_tariff, unused_reason(store.get_setting("cache_prices", {})[1], product, usable))
        mode_revision, mode = store.get_setting("operating_state", {"mode": "observe"})
        latest = store.latest_observations(PROVENANCE)
        last_good = store.latest_observations(PROVENANCE, "ok")
        # Historical seed is explicitly excluded from live readiness.
        for row in store.latest_observations("live").values():
            if row["metric"] not in latest or latest[row["metric"]]["retrieved_at"] < row["retrieved_at"]:
                latest[row["metric"]] = row
        for row in store.latest_observations("live", "ok").values():
            if row["metric"] not in last_good or last_good[row["metric"]]["retrieved_at"] < row["retrieved_at"]:
                last_good[row["metric"]] = row
        readings = {}
        for metric in ("soc", "pv_power", "household_power", "grid_power", "battery_power", "garage_power", "garage_current", "ev_power"):
            row = latest.get(metric)
            good = last_good.get(metric)
            fresh = bool(row and Observation(**row).fresh(now, 5))
            readings[metric] = {"value": row["value"] if fresh else None,
                                "unit": row["unit"] if row else None,
                                "status": "fresh retrieval; device update unverified" if fresh else "unavailable",
                                "last_reading": row["retrieved_at"] if row else None,
                                "source_at": row.get("source_at") if row else None,
                                "last_good_value": good["value"] if good else None,
                                "last_good_at": Observation(**good).sample_at if good else None}
        _, health = store.get_setting("collector_health")
        controller_arg = {} if config.control_state_path is None else {"controller": read_control_state(config.control_state_path)}
        live_at = store.live_written_at()
        # Building this page takes a second or two on the Pi, so the files read above can be newer than `now`. Judge their age against a
        # time taken after reading them; otherwise a perfectly fresh file looks as if it came from the future.
        realtime = assess_realtime(health, max(now, clock.utc()), live_at, **controller_arg)    # judged on the raw record, before the display wording below
        if health and not 0 <= now - health["acquired_at"] <= 5:
            health = {**health, "last_status": health["status"], "status": "stale / collector not updating"}
        _, tracker = store.get_setting("charger_tracker", {})
        ev = latest.get("ev_power")
        aligned = ev and tracker.get("last_sample_t", tracker.get("last_t")) == Observation(**ev).sample_at
        charger_state = tracker.get("label", "unknown") if aligned and readings["ev_power"]["value"] is not None else "unknown"
        today = datetime.fromtimestamp(now, LONDON).date()
        today_car = car_history(store, today, today)
        current_session = next((s for s in today_car["sessions"] if s["id"] == tracker.get("session_id")), None)
        current_tariff = tariff_horizon(store, now, now, now+1)
        current_rate = current_tariff["intervals"][0]
        price = current_rate["price"]
        tariff_status = current_rate["source"]
        if tariff and not tariff.get("confirmed", True) and ("Fallback" in tariff_status or "estimate" in tariff_status):
            tariff_status += " · Prices awaiting confirmation"
        providers_revision, providers, diagnostics = provider_view(store, now)
        _, forecast = store.get_setting("forecast_summary")
        if forecast:
            forecast = {**{k: v for k, v in forecast.items() if k != "series"}, "fresh": 0 <= now - forecast["issued_at"] <= FORECAST_FRESH_SECONDS,
                        "settings_current": forecast["demand_revision"] == revision and forecast.get("provider_revision") == providers_revision and forecast.get("fallback_revision") == tariff_revision}
        try:
            key_configured = SecretFile(config.database.parent / ".env").read() is not None
        except ValueError:
            key_configured = False
        return {"version": __version__, "mode": mode["mode"], "mode_revision": mode_revision,
                "mode_reason": mode.get("reason"), "equipment_writes": False,
                "control_ready": False, "commissioning": "See the controller panel (realtime.controller) for the controller's mode",
                "access_mode": 'private_network' if config.trusted_network else 'password',
                "readings": readings, "demand_revision": revision, "demand": selection, "demand_dates": demand_dates,
                "collector": health, "realtime": realtime, "charger_state": charger_state, "today_car": today_car,
                "charger_last_known": {"state": tracker.get("last_confirmed_label"), "time": tracker.get("last_confirmed_at")},
                "current_session": current_session,
                "active_demand": DemandSelection(**selection).active_mode(now) if selection else "Normal consumption",
                "household_policy": household_policy(selection, now, tariff),
                "tariff_revision": tariff_revision, "tariff": tariff, "night_window": night_window, "tariff_status": tariff_status,
                "savings": savings_summary(store, today),
                "calibration": calibration_view(now), "calibration_revision": store.get_setting(CALIBRATION_SETTING, {})[0],
                "demand_learning": learning_summary(), "automation": describe_automation(store.get_setting(PAUSE_SETTING, {})[1], store.get_setting(OVERRIDE_SETTING, {})[1], now, min_soc),
                "anker_now": describe_anker(*(readings[m]["value"] for m in ("battery_power", "grid_power", "household_power"))),
                "freshness": freshness_limits.stored(store.get_setting(freshness_limits.SETTING, {})[1]), "freshness_revision": store.get_setting(freshness_limits.SETTING, {})[0],
                "freshness_ranges": {"charge_s": list(freshness_limits.CHARGE_RANGE), "idle_s": list(freshness_limits.IDLE_RANGE)},
                "min_soc_device": device_floor(min_soc), "min_soc": min_soc, "min_soc_revision": min_soc_revision, "min_soc_range": [MIN_SOC_LOWEST, MIN_SOC_HIGHEST],
                "price_gbp_per_kwh": price,
                "extra_slots": current_tariff["extra_slots"] or "Unavailable or no verified household cheap slots",
                "providers_revision": providers_revision, "providers": providers.model_dump(),
                "provider_diagnostics": diagnostics, "api_key_configured": key_configured,
                "octopus_connection": store.get_setting('octopus_connection',{})[1],
                "forecast": forecast, "shadow_plan": plan_view(store, now),
                "csrf_token": csrf}

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__, "equipment_writes": False, "control_ready": False}

    def page_response(request, page, title, subtitle, script):
        return templates.TemplateResponse(request=request, name=page+".html", context={
            "version": __version__, "today": datetime.fromtimestamp(clock.utc(), LONDON).date().isoformat(),
            "display_notice": config.display_notice, "page": page, "title": title, "eyebrow": "YOUR HOME ENERGY", "subtitle": subtitle, "script": script,
            "key_entry_allowed": key_entry_allowed(request)})

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(authenticated)])
    def home(request: Request):
        return page_response(request, "dashboard", "Home energy", "Battery, electricity and your household at a glance.", "dashboard.js")

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(authenticated)])
    def settings_page(request: Request):
        return page_response(request, "settings", "Settings", "Household plans, electricity prices and optional connections.", "settings.js")

    @app.get("/history", response_class=HTMLResponse, dependencies=[Depends(authenticated)])
    def history_page(request: Request):
        return page_response(request, "history", "Energy history", "Charging records and observation downloads.", "history.js")

    def recompute_status(generation):
        """Build the picture and store it; whoever asked has already marked the computation as running. If a change was attempted while it was being built (`gen` moved), the
        picture is returned to the one who asked but NOT stored: the next request builds a new one."""
        try:
            fresh = status()
            fresh["computed_at"] = clock.utc()
        except BaseException:
            with status_cond:
                status_cache["computing"] = False
                status_cond.notify_all()
            raise
        with status_cond:
            if status_cache["gen"] == generation:
                status_cache.update(at=time.monotonic(), value=fresh, computing=False)
            else:
                status_cache["computing"] = False
            status_cond.notify_all()
        return fresh

    def recompute_in_background(generation):
        try:
            recompute_status(generation)
        except Exception:
            pass                       # the next request tries again; the picture being served is judged stale by its own age if this keeps failing

    @app.get("/api/status", dependencies=[Depends(authenticated)])
    def api_status():
        # One computation at a time. A request that finds a recent-enough previous picture is answered with it AT ONCE and the new one is built in the background
        # (stale-while-revalidate, GitHub 61): building it takes 0.3 s on a quiet Pi and several seconds when the disk is busy, and a page that waits for that gives up. Only the
        # very first request, one after a change was attempted, or one whose previous picture is older than STALE_SERVE_S has to wait. The picture always carries its own age.
        background, generation = False, 0
        with status_cond:
            while True:
                value, age = status_cache["value"], time.monotonic() - status_cache["at"]
                if value is not None and age < config.status_cache_seconds:
                    return served(value)
                if not status_cache["computing"]:
                    status_cache["computing"] = True
                    generation = status_cache["gen"]
                    background = value is not None and config.status_cache_seconds > 0 and age < STALE_SERVE_S
                    break
                if value is not None:
                    return served(value)
                status_cond.wait(timeout=60)
        if background:
            threading.Thread(target=recompute_in_background, args=(generation,), name="status-refresh", daemon=True).start()
            return served(value)
        return served(recompute_status(generation))

    @app.get("/api/shadow-plan", dependencies=[Depends(authenticated)])
    def shadow_plan():
        return plan_view(store, clock.utc())

    @app.get("/plan-series.csv", dependencies=[Depends(authenticated)])
    def plan_series_csv():
        """The current plan hour by hour in the Anker app's four quantities (solar, battery, home, grid), to lay beside its Smart Mode chart."""
        from ..plan_series import to_csv
        view = plan_view(store, clock.utc())
        series = view.get("series") or []
        if not series:
            raise HTTPException(404, "There is no current plan to show hour by hour")
        return Response(to_csv(series), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="plan-series.csv"'})

    @app.get("/api/shadow-plan/{issuance_id}", dependencies=[Depends(authenticated)])
    def shadow_evidence(issuance_id: str):
        with closing(store.connect()) as db:
            row = db.execute("SELECT payload FROM issuances WHERE id=?", (issuance_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Shadow evidence not found")
        evidence = json.loads(row[0])
        if evidence.get("kind") != "shadow_plan":
            raise HTTPException(404, "Shadow evidence not found")
        return evidence

    @app.put("/api/shadow-refresh", dependencies=[Depends(write_access)])
    def refresh_shadow():
        revision, _ = store.get_setting("planner_refresh")
        try:
            store.set_setting("planner_refresh", {"requested_at": clock.utc()}, clock.utc(), revision)
        except ValueError:
            raise HTTPException(409, "Another refresh was already requested") from None
        return {"queued": True, "equipment_writes": False, "note": "Separate shadow worker must be running"}

    @app.put("/api/demand", dependencies=[Depends(write_access)])
    def save_demand(body: SaveDemand):
        try:
            revision = store.set_setting("demand", body.selection.model_dump(), clock.utc(), body.revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"revision": revision, "active": body.selection.active_mode(clock.utc()), "replan_required": True}

    @app.put("/api/demand-schedule", dependencies=[Depends(write_access)])
    def save_schedule(body: SaveSchedule):
        try:
            selection = DemandSelection(mode=body.mode,
                start=london_input(body.start_local, body.start_fold) if body.start_local else clock.utc(),
                end=london_input(body.end_local, body.end_fold) if body.end_local else None,
                away_watts=body.away_watts, high_extra_kwh=body.high_extra_kwh)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "Check London dates and allowances. Repeated clock times need first/second occurrence; skipped clock times cannot be used.") from None
        return save_demand(SaveDemand(revision=body.revision, selection=selection))

    @app.put("/api/providers", dependencies=[Depends(write_access)])
    def save_providers(body: SaveProviders):
        try:
            revision = store.set_setting("providers", body.config.model_dump(), clock.utc(), body.revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"revision": revision, "equipment_writes": False}

    @app.put("/api/provider-refresh", dependencies=[Depends(write_access)])
    def refresh_providers():
        revision, _ = store.get_setting("provider_refresh")
        try:
            store.set_setting("provider_refresh", {"requested_at": clock.utc()}, clock.utc(), revision)
        except ValueError:
            raise HTTPException(409, "Refresh already requested") from None
        return {"queued": True, "note": "Separate worker required; provider request limits still apply"}

    @app.put("/api/octopus-key", dependencies=[Depends(write_access)])
    async def save_octopus_key(request: Request):
        if not key_entry_allowed(request):
            raise HTTPException(403, "Enter keys on this computer or through HTTPS")
        # Manual parsing avoids validation errors echoing a secret input field.
        try:
            raw = await request.body()
            if len(raw) > 2048:
                raise ValueError()
            data = __import__("json").loads(raw)
            if not isinstance(data, dict) or set(data) != {"api_key"}:
                raise ValueError()
            SecretFile(config.database.parent / ".env").write(data["api_key"])
        except (ValueError, TypeError):
            raise HTTPException(422, "API key was not saved. Check the entered key.") from None
        return {"configured": True}

    @app.put('/api/octopus-connect',dependencies=[Depends(write_access)])
    def connect_octopus():
        revision,_=store.get_setting('octopus_connect_request')
        try:
            store.set_setting('octopus_connect_request',{'requested_at':clock.utc()},clock.utc(),revision)
        except ValueError:
            raise HTTPException(409,'Connection request already changed') from None
        return {'queued':True,'equipment_writes':False}

    @app.get("/api/forecast", dependencies=[Depends(authenticated)])
    def api_forecast():
        return status()["forecast"] or {"demand_status": "unavailable", "solar_status": "unavailable", "target": None, "reason": "Run the separate provider worker"}

    @app.put("/api/calibration", dependencies=[Depends(write_access)])
    def save_calibration(body: SaveCalibration):
        """How often the battery must be charged to 100 % (BMS calibration). Used by the planner; see calibration.py."""
        try:
            revision = store.set_setting(CALIBRATION_SETTING, {"days": body.days}, clock.utc(), body.revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"revision": revision, "replan_required": True}

    @app.put("/api/freshness", dependencies=[Depends(write_access)])
    def save_freshness(body: SaveFreshness):
        """How old a reading may be before the controller treats it as unknown: tighter for the garage current while charging, the specification's 10 s otherwise. Reaches the controller in the plan file."""
        if freshness_limits.valid({"charge_s": body.charge_s, "idle_s": body.idle_s}) is None:
            raise HTTPException(422, "The limit while idle cannot be tighter than the limit while charging.")
        try:
            revision = store.set_setting(freshness_limits.SETTING, {"charge_s": body.charge_s, "idle_s": body.idle_s}, clock.utc(), body.revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"revision": revision, "replan_required": True}

    @app.put("/api/min-soc", dependencies=[Depends(write_access)])
    def save_min_soc(body: SaveMinSoc):
        """The minimum battery level (fraction). Used by the planner and, through the plan file, by the controller."""
        try:
            revision = store.set_setting(MIN_SOC_SETTING, {"value": body.value}, clock.utc(), body.revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"revision": revision, "replan_required": True}

    def devices_view():
        from ..device_book import DeviceBook
        from ..devices_config import resolve
        book = DeviceBook(store.path.parent / "device-book.json")
        data = book.read()
        try:
            configured = resolve()
        except Exception:
            configured = {}
        out = {}
        for name in ("solarbank", "meter", "shelly"):
            conf = configured.get(name)
            found = data["addresses"].get(name)
            out[name] = {"configured": conf, "in_use": book.effective_host(name, conf) if conf else None,
                         "moved": bool(found and found.get("superseded") == conf and found.get("host") != conf),
                         "identity_known": name in data["identities"],
                         "identity": data["identities"].get(name),
                         "identity_confirmed": book.is_confirmed(name),
                         "request": data["requests"].get(name)}
        return out

    @app.get("/api/devices", dependencies=[Depends(authenticated)])
    def devices_get():
        """Where each device is, whether it has been recognised, and any address change asked for (WP21, GitHub issues 14 and 15)."""
        return devices_view()

    @app.put("/api/devices", dependencies=[Depends(write_access)])
    def devices_put(body: SaveDeviceAddress):
        """Ask for a device address. The controller probes it and adopts it only if the device's identity matches the one commissioned; the answer appears in GET /api/devices."""
        from ..device_book import DeviceBook
        try:
            DeviceBook(store.path.parent / "device-book.json").request_address(body.device, body.host.strip(), clock.utc())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {**devices_view(), "equipment_writes": False}

    @app.get("/api/demand-learning", dependencies=[Depends(authenticated)])
    def demand_learning_view():
        """What the planner assumes for household demand: the typed figure beside the learned one, and whether learning is on."""
        from ..demand_learning import describe
        return describe(store)

    @app.put("/api/demand-learning", dependencies=[Depends(write_access)])
    def demand_learning_switch(body: SaveDemandLearning):
        """The one switch: off returns the planner to the typed Away figure and the last good Normal day at once."""
        from ..demand_learning import describe, set_enabled
        set_enabled(store, body.enabled, clock.utc())
        return {**describe(store), "replan_required": True, "equipment_writes": False}

    @app.put("/api/automation", dependencies=[Depends(write_access)])
    def change_automation(body: SaveAutomation):
        """Pause automation (until a time, or until resumed), override the charge level until a time, or return to automatic (clears both)."""
        now = clock.utc()
        until = None
        if body.until_local:
            try:
                until = london_input(body.until_local, body.until_fold)
            except (ValueError, OverflowError, OSError):
                raise HTTPException(422, "Check the London date and time. Repeated clock times need first/second occurrence; skipped clock times cannot be used.") from None
            if not now < until <= now + MAX_OVERRIDE_S:
                raise HTTPException(422, "Choose an end time in the future and within 14 days.")
        floor = valid_min_soc((store.get_setting(MIN_SOC_SETTING, {})[1] or {}).get("value")) or DEFAULT_MIN_SOC
        updates = {}
        if body.action == "pause":
            updates[PAUSE_SETTING] = {"paused": True, "until": until, "set_at": now}
        elif body.action == "override":
            if until is None or body.target_percent is None:
                raise HTTPException(422, "An override needs a charge level and an end time.")
            if body.target_percent / 100 < floor - 1e-9:
                raise HTTPException(422, f"The level cannot be below the minimum battery level ({floor * 100:.0f}%).")
            updates[OVERRIDE_SETTING] = {"target": round(body.target_percent / 100, 2), "valid_until": until, "set_at": now}
        elif body.action == "resume":
            updates[PAUSE_SETTING] = {"paused": False, "set_at": now}
            updates[OVERRIDE_SETTING] = {}
        else:
            raise HTTPException(422, "Unknown action.")
        try:
            store.set_settings({name: (value, store.get_setting(name)[0]) for name, value in updates.items()}, now)       # both settings or neither (#20)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"automation": describe_automation(store.get_setting(PAUSE_SETTING, {})[1], store.get_setting(OVERRIDE_SETTING, {})[1], now, floor),
                "replan_required": True, "equipment_writes": False}

    @app.put("/api/tariff", dependencies=[Depends(write_access)])
    def save_tariff(body: SaveTariff):
        try:
            revision = store.set_setting("fallback_tariff", body.tariff.model_dump(), clock.utc(), body.revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"revision": revision, "replan_required": True}

    @app.put("/api/mode", dependencies=[Depends(write_access)])
    def change_mode(body: ModeRequest):
        if body.mode not in ("observe", "shadow", "handover_pending"):
            raise HTTPException(409, "Automatic control and verified Anker handover require equipment commissioning")
        try:
            if body.mode == "handover_pending":
                revision = request_handover(store, clock.utc(), body.revision)
            else:
                revision = store.set_setting("operating_state", {"mode": body.mode}, clock.utc(), body.revision)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"revision": revision, "equipment_writes": False}

    def historical_bounds(start, end):
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if not 0 <= (last - first).days <= 366:
                raise ValueError("Choose an ordered range of at most 367 days")
            return day_bounds(first)[0], day_bounds(last + timedelta(days=1))[0]
        except (ValueError, OverflowError, OSError) as exc:
            raise HTTPException(422, "Invalid London calendar date range") from exc

    def selected_car_history(start, end):
        try:
            return car_history(store, date.fromisoformat(start), date.fromisoformat(end))
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "Invalid London calendar date range") from None

    @app.get("/api/car-history", dependencies=[Depends(authenticated)])
    def api_car_history(start: str, end: str):
        return selected_car_history(start, end)

    @app.get("/api/car-history/minutes", dependencies=[Depends(authenticated)])
    def api_car_minutes(start: str, end: str, limit: int = 1440, after: float | None = None):
        """Per-minute charger power, one bounded page at a time. Follow `next_after` until it is null."""
        try:
            rows, following = car_power_minutes(store, date.fromisoformat(start), date.fromisoformat(end), limit, after)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "Invalid London date range, page size or cursor") from None
        return {"power_minutes": rows, "next_after": following}

    @app.get("/api/day", dependencies=[Depends(authenticated)])
    def api_day(day: str | None = None):
        """The home page's day chart: one London day in five-minute steps (battery, house, solar, grid, battery level), its totals and its cheap periods."""
        from ..day_view import day_series
        now = clock.utc()
        try:
            chosen = date.fromisoformat(day) if day else datetime.fromtimestamp(now, LONDON).date()
            return day_series(store, chosen, now)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "Choose a London date within the last 90 days") from None

    @app.get("/half-hours.csv", dependencies=[Depends(authenticated)])
    def half_hours_csv(start: str, end: str):
        """The long-term half-hour record (Octopus settlement periods): used, imported, exported, solar, battery, price and the two costs. Estimates."""
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if not 0 <= (last - first).days <= 366:
                raise ValueError("range")
        except ValueError:
            raise HTTPException(422, "Choose an ordered range of at most 367 days") from None
        out = io.StringIO()
        fields = ["period_start_utc", "period_start_london", "minutes_covered", "consumption_kwh", "import_kwh", "export_kwh", "solar_kwh", "battery_charged_kwh",
                  "battery_discharged_kwh", "price_gbp_per_kwh", "cheap_rate", "baseline_cost_gbp_no_solar_no_battery", "actual_cost_gbp",
                  "car_kwh", "car_minutes_covered", "house_without_car_kwh"]          # the car apart (record version 4), to set beside Octopus's half-hours; empty for older records
        writer = csv.writer(out)
        writer.writerow(fields)
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            record = store.get_setting(HALF_HOUR_PREFIX + day.isoformat())[1] or {}
            for slot in record.get("slots", []):
                writer.writerow([datetime.fromtimestamp(slot["t"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), f"{day.isoformat()} {slot['london']}", slot["minutes_covered"],
                                 slot["consumption_kwh"], slot["import_kwh"], slot["export_kwh"], slot["solar_kwh"], slot["battery_charged_kwh"], slot["battery_discharged_kwh"],
                                 slot["price_gbp_per_kwh"], slot["cheap_rate"], slot["baseline_cost_gbp"], slot["actual_cost_gbp"],
                                 slot.get("car_kwh"), slot.get("car_minutes_covered"), slot.get("house_without_car_kwh")])
        return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="half-hours.csv"'})

    @app.get("/api/validation", dependencies=[Depends(authenticated)])
    def api_validation(days: int = 7):
        """B3: the weekly agreement report (default the last 7 finished days): per-day agreement and every difference between the Anker and the controller."""
        if not 1 <= days <= 60:
            raise HTTPException(422, "Choose 1 to 60 days")
        return build_validation_report(store, datetime.fromtimestamp(clock.utc(), LONDON).date(), days)

    @app.get("/validation.csv", dependencies=[Depends(authenticated)])
    def validation_csv():
        """Every stored difference between the Anker's behaviour and the controller's request, oldest first (kept long term; minute detail is purged after a week)."""
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["date", "start_london", "end_london", "minutes", "kind", "controller_asked", "anker_did", "mean_battery_kw_positive_is_discharge", "soc_start", "soc_end"])
        for d in all_differences(store, datetime.fromtimestamp(clock.utc(), LONDON).date()):
            writer.writerow([d["date"], datetime.fromtimestamp(d["start"], LONDON).strftime("%H:%M"), datetime.fromtimestamp(d["end"], LONDON).strftime("%H:%M"), d["minutes"],
                             KINDS[d["kind"]], d["intent"], d["actual"], d["mean_battery_kw"], d["soc_start"], d["soc_end"]])
        return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="validation.csv"'})

    @app.get("/car-charging.csv", dependencies=[Depends(authenticated)])
    def car_csv(start: str, end: str):
        data = selected_car_history(start, end)
        out = io.StringIO()
        fields = ["date", "charging_kwh", "active_seconds", "covered_seconds", "expected_seconds", "partial"]
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data["daily"])
        return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="car-charging.csv"'})

    @app.get("/api/history", dependencies=[Depends(authenticated)])
    def history(start: str, end: str, limit: int = 2000, cursor: str | None = None):
        """One bounded page of observations (metric by metric, then time). Follow `next_cursor` until it is null."""
        bounds = historical_bounds(start, end)
        try:
            position = json.loads(base64.urlsafe_b64decode(cursor.encode())) if cursor else None
            rows, following = store.observations_page(*bounds, limit=limit, cursor=position)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "Invalid page size or cursor") from exc
        next_cursor = base64.urlsafe_b64encode(json.dumps(following).encode()).decode() if following else None
        return {"observations": rows, "next_cursor": next_cursor,
                "coverage": "Coverage is recorded per observation; unknown is not complete",
                "car_sessions": selected_car_history(start, end)["sessions"] if not cursor else [],
                "car_history_status": "Partial/unknown coverage is retained"}

    # Every stored field is exported, so a field added to the record can never again break the download.
    history_fields = [f.name for f in dataclasses.fields(Observation)]

    @app.get("/history.csv", dependencies=[Depends(authenticated)])
    def history_csv(start: str, end: str):
        bounds = historical_bounds(start, end)             # validated before streaming begins

        def chunks():
            out = io.StringIO()
            writer = csv.DictWriter(out, fieldnames=history_fields, extrasaction="ignore")
            writer.writeheader()
            yield out.getvalue()
            cursor = None
            while True:
                rows, cursor = store.observations_page(*bounds, limit=1000, cursor=cursor)
                out.seek(0); out.truncate()
                writer.writerows(rows)
                if rows:
                    yield out.getvalue()
                if cursor is None:
                    return
        return StreamingResponse(chunks(), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="solarbank-observations.csv"'})

    return app

