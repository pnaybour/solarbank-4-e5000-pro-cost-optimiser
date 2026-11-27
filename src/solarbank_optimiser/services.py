"""Separate provider/forecast worker and cache selection. Never run in HTTP handlers."""
import sqlite3
import json
import re
import math
import os
from pathlib import Path
import tempfile

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .minute_source import demand_rows
from .control import SingleOwner
from .forecasts import demand_horizon
from .household_policy import household_policy
from .models import Clock, encoded, fingerprint, number
from .providers import (OctopusAccount, ProviderError, prices_path, public_prices,
                        request, solar_forecast, weather_path, register_prices)
from .settings import FallbackTariff
from .tariff_window import effective_tariff, parse_night_window
from .sense_check import compare_daily


def forecast_series(demand, weather_intervals, start, end, step=1800):
    """Half-hour means of the forecast household demand and solar output (kW) over the horizon, for the dashboard graph. Either may be None."""
    def means(intervals, key):
        total, seconds = {}, {}
        for r in intervals or ():
            lo, hi = max(start, r["start"]), min(end, r["end"])
            t = lo
            while t < hi:
                bin_start = int(t // step) * step
                piece = min(hi, bin_start + step) - t
                total[bin_start] = total.get(bin_start, 0.0) + r[key] * piece
                seconds[bin_start] = seconds.get(bin_start, 0.0) + piece
                t += piece
        return {b: total[b] / seconds[b] for b in total if seconds[b] > 0}
    demand_kw = means(demand["intervals"] if demand else None, "demand_kw")
    pv_kw = means(weather_intervals, "pv_kw")
    return [{"time": b, "demand_kw": round(demand_kw[b], 3) if b in demand_kw else None, "pv_kw": round(pv_kw[b], 3) if b in pv_kw else None}
            for b in sorted(set(demand_kw) | set(pv_kw))]


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    weather_enabled: bool = False
    prices_enabled: bool = False
    account_enabled: bool = False
    product: str = Field(default="", max_length=100)
    tariff: str = Field(default="", max_length=110)
    account: str = Field(default="", max_length=80)
    device: str = Field(default="", max_length=100)
    household_rate_verified: bool = False
    eligibility_until: float | None = Field(default=None, ge=0)
    eligibility_source: str = Field(default="", max_length=300)
    billing_rule: str = "exact_dispatch"

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.prices_enabled:
            prices_path(self.product, self.tariff, 0)
        if self.account_enabled and (not self.account.strip() or not self.device.strip()):
            raise ValueError("Account and linked device are required")
        if self.billing_rule not in {"exact_dispatch", "whole_half_hours"}:
            raise ValueError("Unsupported billing rule")
        if self.household_rate_verified and (not self.account_enabled or self.eligibility_until is None or not self.eligibility_source.strip()):
            raise ValueError("Record account-specific household eligibility evidence and expiry")
        return self


class SecretFile:
    """Dedicated private file: key never enters SQLite, logs or browser responses."""
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if self.path.name == '.env' and not self.path.exists():
            return SecretFile(self.path.with_name('octopus-api-key')).read()
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise ProviderError("credential_file_not_private")
        value = self.path.read_text().strip()
        if self.path.name == '.env':
            matches = [line.split('=',1)[1].strip() for line in value.splitlines()
                       if line.strip().startswith('OCTOPUS_API_KEY=')]
            if len(matches) > 1:
                raise ProviderError('invalid_credential_file')
            if not matches or not matches[0]:
                return None
            value = matches[0]
            if len(value)>1 and value[0] in {'"', "'"} and value[-1]==value[0]:
                value=value[1:-1]
        if not 8 <= len(value) <= 512 or not value.isascii() or any(c.isspace() for c in value):
            raise ProviderError("invalid_credential_file")
        return value

    def write(self, value):
        if not isinstance(value, str) or not 8 <= len(value) <= 512 or not value.isascii() or any(c.isspace() for c in value):
            raise ValueError("Enter a valid API key")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('Credential file must not be a symlink')
        contents=value+'\n'
        if self.path.name == '.env':
            previous=self.path.read_text().splitlines() if self.path.exists() else []
            previous=[line for line in previous if not line.strip().startswith('OCTOPUS_API_KEY=')]
            contents='\n'.join(previous+['OCTOPUS_API_KEY='+value])+'\n'
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".octopus-key-")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def cache_identity(config, name):
    return fingerprint({"product": config.product, "tariff": config.tariff} if name == "prices"
                       else config.model_dump() if name == "slots" else {"weather_model": "two-plane-pvlib-v1"})


# Issue #32: how old a weather forecast may be and still be used. Weather is refreshed hourly; an older forecast is far better than no plan.
WEATHER_MAX_AGE_S = 20 * 3600          # 4 days are fetched, so even a 20-hour-old forecast covers the 48-hour horizon with margin
WEATHER_FRESH_S = 6 * 3600          # older than this is used but reported as stale
WEATHER_RETRY_S = 600              # first retry after a weather failure; doubles up to the normal hourly refresh
WEATHER_REASONS = {"wrong_weather_location_or_timezone", "invalid_weather_length", "wrong_weather_units_or_length", "weather_gap",
                   "weather_horizon_incomplete", "unsupported_provider"}


def provider_view(store, now):
    revision, raw = store.get_setting("providers", {})
    config = ProviderConfig(**raw)
    view = {}
    for name, enabled, max_age in (("weather", config.weather_enabled, WEATHER_MAX_AGE_S),
                                    ("prices", config.prices_enabled, 86400),
                                    ("slots", config.account_enabled, 120)):
        _, health = store.get_setting("provider_" + name, {})
        _, cache = store.get_setting("cache_" + name)
        identity = cache_identity(config, name)
        valid = bool(enabled and cache and cache["identity"] == identity and 0 <= now-cache["acquired_at"] <= max_age)
        view[name] = {**health, "enabled": enabled, "cache_valid": valid,
                      "last_success": cache["acquired_at"] if cache and cache["identity"] == identity else None}
    return revision, config, view


def window_cache(store, now):
    """The price cache to read the cheap WINDOW from: the cache itself when it passes the same test as the cached prices (provider enabled, built for this
    product and tariff, no older than a day), otherwise {} so the saved fallback window applies (#19). Every reader of the window uses this."""
    _, _, view = provider_view(store, now)
    _, cache = store.get_setting("cache_prices", {})
    return cache if view["prices"]["cache_valid"] else {}


def slot_intervals(store, config, now):
    if not (config.account_enabled and config.household_rate_verified and config.eligibility_until > now):
        return []
    identity = cache_identity(config, "slots")
    _, cache = store.get_setting("cache_slots")
    slots = []
    if cache and cache["identity"] == identity and 0 <= now - cache["acquired_at"] <= 120:
        slots.extend(cache["intervals"])
    _, established = store.get_setting("established_slots", {})
    if established.get("identity") == identity:
        slots.extend(r for r in established["intervals"] if r["start"] <= now < r["end"])
    slots = [{"start": left, "end": right} for left, right in sorted({(r["start"], r["end"]) for r in slots})]
    slots = [{"start": r["start"], "end": min(r["end"], config.eligibility_until)} for r in slots if r["start"] < min(r["end"], config.eligibility_until)]
    return apply_slot_rules(store, slots, now)


HALF_HOUR = 1800
SLOT_START_GRACE_S = 300.0                                   # a slot that has only just begun is given this long for the car to start before it stops counting as cheap


def _car_charging_now(store, now):
    from .collector import PROVENANCE
    from .ev_policy import EV_ON_KW
    row = store.live_observations(PROVENANCE).get("ev_power")
    return bool(row and row.get("quality") == "ok" and isinstance(row.get("value"), (int, float)) and row["value"] >= EV_ON_KW and 0 <= now - row["retrieved_at"] <= 30)


def _car_charged_in_half_hour(store, half, now):
    """True when the car was measured charging in any part of half-hour number `half` so far (from the telemetry files). Octopus bills the whole half-hour as a
    smart-charging half-hour once its charger has supplied power in it (owner, 15 November 2026)."""
    from .ev_policy import EV_ON_KW
    try:
        minutes = store.minute_source().minutes("ev_power", half * HALF_HOUR, now + 1)
    except (ValueError, OSError):
        return False
    return any(item.mean is not None and item.mean >= EV_ON_KW for item in minutes.values())


def apply_slot_rules(store, slots, now):
    """The listed SMART dispatch intervals the planner prices as cheap. A slot is cheap only when Octopus's charger supplies power in it, and the car's measured
    power is the same evidence (owner, 15 November 2026: the bills of 13 and 14 November, REQUIREMENTS_AMENDMENTS.md). So the half-hour in progress counts only
    if the car is charging now or has charged in it (a slot that has only just begun has SLOT_START_GRACE_S for the car to start); a FUTURE listed slot is
    assumed to be used, because nothing can say otherwise yet and the plan needs the window for the controller to charge in, which it does only while the car draws
    (signals.SmartSlot). No allowance is counted (owner, 12 November 2026, GitHub issue 79). Slots are cut into half-hours, the rule applied, and adjacent half-hours
    joined again."""
    if not slots:
        return []
    charging_now = _car_charging_now(store, now)
    current = int(now // HALF_HOUR)
    charged_this_half_hour = charging_now or _car_charged_in_half_hour(store, current, now)
    kept = []
    for h in sorted({h for r in slots for h in range(int(r["start"] // HALF_HOUR), int(-(-r["end"] // HALF_HOUR)))}):
        left, right = max(h * HALF_HOUR, min(r["start"] for r in slots)), (h + 1) * HALF_HOUR
        pieces = [(max(left, r["start"]), min(right, r["end"])) for r in slots if r["start"] < right and r["end"] > h * HALF_HOUR]
        pieces = [(a, b) for a, b in pieces if b > a]
        if not pieces:
            continue
        if h * HALF_HOUR + HALF_HOUR <= now:
            kept.extend(pieces)                              # the past is not priced, but its boundaries are kept as listed
            continue
        began = min((a for a, _b in pieces), default=now)
        if h == current and not (charged_this_half_hour or now - began < SLOT_START_GRACE_S):
            continue                                         # in progress, the car had time to start and is not charging: nothing is being billed as a slot
        kept.extend(pieces)
    merged = []
    for a, b in sorted(kept):
        if merged and a <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], b)
        else:
            merged.append({"start": a, "end": b})
    return merged


def tariff_horizon(store, now, start, end):
    """Select every interval independently: current API / valid cache / defaults.

    Missing prices are null and inhibit economic planning. Smart slots need
    separately recorded household eligibility; load alone is never evidence.
    """
    number(start, "start", now)
    number(end, "end", start + 1, start + 72*3600)
    revision, config, view = provider_view(store, now)
    _, cache = store.get_setting("cache_prices", {})
    rates = cache.get("intervals", []) if view["prices"]["cache_valid"] else []
    fallback_revision, fallback = store.get_setting("fallback_tariff")
    fallback, window_source = effective_tariff(fallback, cache if view["prices"]["cache_valid"] else {}, config.product)           # the cheap window from the Octopus product when it states one
    fallback = FallbackTariff(**fallback) if fallback else None
    slots = slot_intervals(store, config, now)
    boundaries = {start, end}
    boundaries.update(range((int(start)//60+1)*60, math.ceil(end), 60))
    for r in rates + slots:
        boundaries.update(t for t in (r["start"], r["end"]) if start < t < end)
    if fallback:
        boundaries.update(t for t in (fallback.valid_from, fallback.valid_until) if t is not None and start < t < end)
    bounds = sorted(boundaries)
    result = []
    for left, right in zip(bounds, bounds[1:]):
        middle = (left + right)/2
        rate = next((r for r in rates if r["start"] <= middle < r["end"]), None)
        price, cheap, source = None, False, "unavailable"
        if rate:
            if 'price' in rate:
                price = rate['price']
            elif fallback and fallback.confirmed:
                try:
                    _,cheap=fallback.at(middle)
                    price=rate['night_price'] if cheap else rate['day_price']
                except ValueError:
                    pass
            health = view["prices"]
            if price is not None:
                source = "API" if health.get("status") == "ok" and 0 <= now-cache["acquired_at"] <= 300 else "cache"
                if 'day_price' in rate:source+=' · Published day/night prices; night window '+('from the Octopus product' if window_source=='octopus' else 'as configured')
        if price is None and fallback:
            try:
                price, cheap = fallback.at(middle)
                source = "Fallback defaults"
            except ValueError:
                pass
        # The default night window also identifies cheap periods when the public
        # product exposes a flat standard rate. Its prices remain explicit estimates.
        if fallback:
            try:
                _, night = fallback.at(middle)
                if night:
                    cheap = True
                    if rate is None:
                        price, source = fallback.cheap_pence/100, "Fallback defaults: night"
            except ValueError:
                pass
        if any(s["start"] <= middle < s["end"] for s in slots):
            # A verified smart slot is billed at the cheap rate. Precedence (specification 7.2): the applicable API / valid cached cheap
            # price first, the configured fallback cheap price only where that is unavailable.
            api_cheap = rate.get("night_price") if rate else None
            if isinstance(api_cheap, (int, float)) and not isinstance(api_cheap, bool) and math.isfinite(api_cheap) and api_cheap > 0:
                origin = "API" if view["prices"].get("status") == "ok" and 0 <= now-cache["acquired_at"] <= 300 else "cache"
                price, cheap, source = api_cheap, True, f"{origin}: verified smart slot at the published cheap rate"
            elif fallback:
                try:
                    fallback.at(middle)
                    price, cheap, source = fallback.cheap_pence/100, True, "Verified smart slot; default cheap price estimate"
                except ValueError:
                    pass
        item = {"start": left, "end": right, "price": price, "cheap": cheap, "source": source}
        if result and all(result[-1][k] == item[k] for k in ("price", "cheap", "source")):
            result[-1]["end"] = right
        else:
            result.append(item)
    return {"intervals": result, "complete": all(r["price"] is not None for r in result),
            "provider_revision": revision, "fallback_revision": fallback_revision,
            "estimated_costs": True, "extra_slots": slots}


SLOT_ARCHIVE_SECONDS = 1800         # an unchanged slot list is archived at most this often
FORECAST_REFRESH_SECONDS = 1800     # was 15 minutes (specification section 7); half an hour matches the hourly weather and the one-write-per-half-hour budget
FORECAST_FRESH_SECONDS = 2400       # shown as current for the refresh interval plus a margin (it was 300 s while refreshing every minute)


def publish_dispatch_feed(path, intervals, fetched_at):
    """The small file the controller reads for SMARTSLOT (specification 6.2, GitHub issue 67): the Octopus SMART dispatches, in the shape
    `signals.SmartSlot` expects -- (start, end, "SMART") -- and when they were fetched, so the controller's own 300 s/1,800 s freshness rules
    run from the real fetch time, not from how often the controller happens to look. Written like the plan file: atomically, and never raising
    (its absence just leaves SMARTSLOT false, the safe direction)."""
    if path is None:
        return
    path = Path(path)
    try:
        document = {"dispatches": [[r["start"], r["end"], "SMART"] for r in intervals], "fetched_at": fetched_at}
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(document, separators=(",", ":")))
        os.replace(temporary, path)
    except OSError:
        pass


class ProviderWorker:
    def __init__(self, store, secret_file=None, clock=None, transport=request, solar=solar_forecast, dispatch_feed_path=None):
        self.store, self.clock, self.transport, self.solar = store, clock or Clock(), transport, solar
        self.secrets = SecretFile(secret_file or store.path.parent / ".env")
        self.account = OctopusAccount(transport)
        self.dispatch_feed_path = dispatch_feed_path

    def put(self, name, value, now):
        revision, _ = self.store.get_setting(name)
        return self.store.set_setting(name, value, now, revision)

    def night_window(self, config):
        """The cheap-rate window stated by the Octopus product description (public, no key), or None. A failed lookup only means the configured window is used."""
        try:
            raw = self.transport("api.octopus.energy", f"/v1/products/{config.product}/")
            product = json.loads(raw)
            window = parse_night_window(" ".join(str(product.get(k, "")) for k in ("description", "full_name", "display_name")))
        except (ProviderError, ValueError, TypeError, AttributeError, KeyError, OSError):
            return None
        if window is None:
            return None
        return {"start_minute": window[0], "end_minute": window[1], "product": config.product, "source": "Octopus product description"}

    def refresh(self, name, config, now):
        identity = cache_identity(config, name)
        if name == "weather":
            raw = self.transport("api.open-meteo.com", weather_path())
            result = self.solar(raw, self.clock.utc())
        elif name == "prices":
            raw = self.transport("api.octopus.energy", prices_path(config.product, config.tariff, now))
            intervals=public_prices(raw)
            extra={}
            window=self.night_window(config)
            if window:extra['night_window']=window
            if not intervals:
                day=self.transport('api.octopus.energy',prices_path(config.product,config.tariff,now,'day-unit-rates'))
                night=self.transport('api.octopus.energy',prices_path(config.product,config.tariff,now,'night-unit-rates'))
                intervals=register_prices(day,night,now,now+72*3600)
                raw=encoded({'day':json.loads(day),'night':json.loads(night)}).encode()
                extra={**extra,'rate_kind':'day_night_registers','night_window_source':'Octopus product description' if window else 'Owner-confirmed London night window','ongoing_rates':'Null supplier expiry clipped to the 72-hour price horizon; refresh required'}
            result = {"intervals": intervals, "currency": "GBP", "unit": "GBP/kWh", "vat": "included",**extra}
        else:
            key = self.secrets.read()
            if not key:
                raise ProviderError("no_api_key")
            intervals = self.account.dispatches(key, config.account, config.device, now)
            if config.household_rate_verified and config.billing_rule == "whole_half_hours":
                intervals = [{"start": math.floor(r["start"]/1800)*1800,
                              "end": math.ceil(r["end"]/1800)*1800} for r in intervals]
            result = {"intervals": intervals, "household_eligibility_verified": config.household_rate_verified}
            raw = encoded(result).encode()  # Explicitly typed/redacted; no auth or account response archive.
        acquired_at = self.clock.utc()
        issuance = None
        if name == "slots":
            # The slot list is polled every minute and almost always says the same thing: keep one archived copy until it changes (or half an hour passes)
            # instead of writing an identical evidence row every minute.
            _, previous = self.store.get_setting("cache_slots", {})
            if (previous.get("identity") == identity and previous.get("intervals") == result["intervals"] and previous.get("issuance_id")
                    and 0 <= acquired_at - previous.get("archived_at", previous.get("acquired_at", 0)) < SLOT_ARCHIVE_SECONDS):
                issuance, archived_at = previous["issuance_id"], previous.get("archived_at", previous.get("acquired_at", acquired_at))
        if issuance is None:
            issuance = self.store.archive(acquired_at, raw, {"kind": name, "identity": identity, **result})
            archived_at = acquired_at
        self.put("cache_" + name, {**result, "identity": identity, "acquired_at": acquired_at, "issuance_id": issuance, "archived_at": archived_at}, acquired_at)
        if name == "slots":
            # GitHub issue 67 (specification 6.2, owner decision A): the controller never sees this database, so the dispatches it needs for
            # SMARTSLOT go out as their own small file, like the plan file, stamped with when THIS fetch actually happened (not archived_at,
            # which a repeated identical answer keeps frozen: SMARTSLOT's 300 s/1,800 s freshness rules are about Octopus being asked, not about
            # the evidence record changing).
            publish_dispatch_feed(self.dispatch_feed_path, result["intervals"], acquired_at)
            _, old = self.store.get_setting("established_slots", {})
            retained = old.get("intervals", []) if old.get("identity") == identity else []
            retained = [r for r in retained if r["start"] <= acquired_at < r["end"]]
            if config.household_rate_verified and config.eligibility_until > acquired_at:
                retained += [r for r in result["intervals"] if r["start"] <= acquired_at < r["end"]]
            retained = [{"start": left, "end": right} for left, right in sorted({(r["start"], r["end"]) for r in retained})]
            self.put("established_slots", {"identity": identity, "intervals": retained}, acquired_at)

    def once(self):
        now = self.clock.utc()
        self.connect_octopus(now)
        _, raw = self.store.get_setting("providers", {})
        config = ProviderConfig(**raw)
        for name, enabled, period in (("weather", config.weather_enabled, 3600),
                                      ("prices", config.prices_enabled, 1800), ("slots", config.account_enabled, 60)):
            _, health = self.store.get_setting("provider_" + name, {})
            identity = cache_identity(config, name)
            _, refresh = self.store.get_setting("provider_refresh", {})
            # Issue #32: after a weather failure, try again within minutes rather than waiting the full hour.
            minimum = (WEATHER_RETRY_S if health.get("status") == "failed" else 3600) if name == "weather" else 60
            requested = refresh.get("requested_at", 0) > health.get("last_attempt", -1)
            changed = health.get("identity") != identity
            due = now >= health.get("next_retry", 0) or requested or changed
            if not enabled or not due or now < health.get("last_attempt", -1e20) + minimum:
                continue
            try:
                self.refresh(name, config, now)
                health = {"status": "ok", "reason": None, "failures": 0, "last_attempt": now, "next_retry": now+period}
            except Exception as exc:
                # Never persist arbitrary exception strings (they may contain keys/account data).
                reason = str(exc) if isinstance(exc, ProviderError) else "invalid_response"
                allowed = {"rate_limited", "http_failure", "response_too_large", "connection_failed", "no_api_key",
                           "graphql_error", "linked_device_not_verified", "credential_file_not_private", "invalid_credential_file",
                           # fixed codes from the dispatch decoder: they hold no account data, and the generic "invalid_response" hid which rule rejected the feed (8 November)
                           "invalid_dispatch_list", "invalid_dispatch_interval", "unknown_dispatch_type", "invalid_dispatch_energy"}
                if name != "weather" and not isinstance(exc, ProviderError):
                    # The class of the exception is not account data and says where decoding broke (8 November: the slot feed failed with only "invalid_response"); the message may hold account data and is never kept.
                    reason = "invalid_response:" + exc.__class__.__name__
                    allowed = allowed | {reason}
                if name == "weather":
                    # Issue #32: weather responses hold no secrets, so keep the specific reason (a fixed code, or the exception class).
                    allowed = allowed | WEATHER_REASONS
                    if not isinstance(exc, ProviderError):
                        detail = re.sub(r"[^A-Za-z0-9_ .:-]", "", str(exc))[:60].strip()
                        reason = "invalid_response:" + exc.__class__.__name__ + (": " + detail if detail else "")
                        allowed = allowed | {reason}
                reason = reason if reason in allowed else "invalid_response"
                failures = health.get("failures", 0) + 1
                retry = min(period, WEATHER_RETRY_S * 2**min(failures-1, 3)) if name == "weather" else max(60, min(3600, 60*2**min(failures-1, 6)))
                health = {"status": "failed", "reason": reason, "failures": failures, "last_attempt": now, "next_retry": now+retry}
            health["identity"] = identity
            self.put("provider_" + name, health, self.clock.utc())
        self.forecast()

    def connect_octopus(self,now):
        request_revision,_=self.store.get_setting('octopus_connect_request')
        _,old=self.store.get_setting('octopus_connection',{})
        if not request_revision or old.get('request_revision')==request_revision or now<old.get('attempted_at',-1e20)+60:
            return
        revision,raw=self.store.get_setting('providers',{})
        config=ProviderConfig(**raw)
        result={'request_revision':request_revision,'attempted_at':now,'equipment_writes':False,'devices':[]}
        try:
            key=self.secrets.read()
            if not key:raise ProviderError('no_api_key')
            if not config.account.strip():raise ProviderError('account_number_required')
            devices=self.account.linked_devices(key,config.account,now)
            chosen=next((d for d in devices if d['id']==config.device),devices[0] if len(devices)==1 else None)
            result['devices']=devices
            if chosen:
                # Specification 7.2: a linked device alone is not a connection. A valid dispatch response (an empty list is valid and
                # means "no extra slots") must come back before anything is called connected or enabled.
                planned=self.account.dispatches(key,config.account,chosen['id'],now)
                updated=config.model_dump()|{'device':chosen['id'],'account_enabled':True}
                self.store.set_setting('providers',updated,self.clock.utc(),revision)
                result.update(status='connected',dispatch_verified=True,planned_slots=len(planned),
                              reason=('Linked device and a valid dispatch response found ('+(f'{len(planned)} current planned slot(s)' if planned else 'no extra slots planned')+
                                      '). Household billing eligibility is separate and not yet verified.'))
            else:
                result.update(status='choose_device' if devices else 'no_devices',reason='Choose one of the linked EV/charger IDs and save account settings.' if devices else 'Account authenticated; no linked EV or charger found.')
        except Exception as exc:
            allowed={'invalid_dispatch_list','invalid_dispatch_interval','unknown_dispatch_type','invalid_dispatch_energy','no_api_key','account_number_required','graphql_error','rate_limited','http_failure','connection_failed','linked_device_not_verified','credential_file_not_private','invalid_credential_file'}
            reason=str(exc) if isinstance(exc,ProviderError) and str(exc) in allowed else 'connection_failed'
            result.update(status='failed',reason=reason)
        self.put('octopus_connection',result,self.clock.utc())

    def forecast(self):
        now = self.clock.utc()
        # Continuous services poll providers frequently, but archive forecasts
        # at most every four minutes unless an input revision changes.
        # The slot list is refreshed every minute and its revision changes each time even when it still says "no extra slots": judge it by its
        # CONTENT, otherwise a 340 KB forecast archive is written every minute for nothing.
        slots = self.store.get_setting('cache_slots')[1] or {}
        established = self.store.get_setting('established_slots')[1] or {}
        token = tuple(self.store.get_setting(k)[0] for k in ('demand','providers','fallback_tariff','cache_weather','cache_prices','normal_reference')) \
            + (encoded(slots.get('intervals', [])), encoded(established.get('intervals', [])))
        if getattr(self,'forecast_token',None)==token and now<getattr(self,'next_forecast',0):
            return
        revision, selection = self.store.setting_at("demand", now)
        start = (int(now)//60+1)*60
        end = start + 48*3600
        try:
            from .demand_learning import planner_inputs, planner_level
            learned_selection, average_days, _note = planner_inputs(self.store, selection)
            demand = demand_horizon(demand_rows(self.store, self.store.minute_source(), now-8*86400, now),
                                    learned_selection, revision, now, start, end, average_days, level_kwh=planner_level(self.store), default_kwh=planner_level(self.store))
            demand_status, demand_reason = "ok", None
        except ValueError as exc:
            demand, demand_status, demand_reason = None, "unavailable", str(exc)
        _, _, view = provider_view(self.store, now)
        _, weather = self.store.get_setting("cache_weather", {})
        solar_ok = view["weather"]["cache_valid"] and weather.get("intervals") and weather["intervals"][0]["start"] <= start and weather["intervals"][-1]["end"] >= end
        prices = tariff_horizon(self.store, now, start, end)
        _, tariff = self.store.get_setting("fallback_tariff")
        tariff, _ = effective_tariff(tariff, window_cache(self.store, now), self.store.get_setting("providers", {})[1].get("product"))
        payload = {"issued_at": now, "valid_until": now+FORECAST_FRESH_SECONDS, "start": start, "end": end,
            "demand_status": demand_status, "demand_reason": demand_reason,
            "demand_kwh": demand["demand_kwh"] if demand else None,
            "reference_day": demand["reference_day"] if demand else None,
            "demand_revision": revision, "provider_revision": prices["provider_revision"],
            "fallback_revision": prices["fallback_revision"], "solar_status": "ok" if solar_ok else "unavailable",
            "pv_kwh": sum(r["pv_kw"]*(min(end,r["end"])-max(start,r["start"]))/3600
                           for r in weather["intervals"] if r["start"] < end and r["end"] > start) if solar_ok else None,
            "price_complete": prices["complete"], "target": None,
            "household_policy": household_policy(selection, now, tariff),
            "target_reason": "Shadow planner not yet connected; equipment control uncommissioned",
            "equipment_writes": False}
        payload['series']=forecast_series(demand, weather["intervals"] if solar_ok else None, start, end)
        payload['normal_sense_check']=compare_daily(self.store.get_setting('normal_reference')[1],demand)
        # Archive reproducible full inputs, with no account/key/token metadata.
        model = {"kind": "household_forecast", "summary": payload, "demand": demand,
                 "weather_issuance_id": weather.get("issuance_id") if solar_ok else None, "prices": prices}
        payload["issuance_id"] = self.store.archive(now, encoded(model).encode(), model)
        self.put("forecast_summary", payload, now)
        self.forecast_token=token
        self.next_forecast=now+FORECAST_REFRESH_SECONDS

    def run(self, stop, count=1):
        with SingleOwner(str(self.store.path.resolve()) + ".providers.lock"):
            iteration = 0
            try:
                while count is None or iteration < count:
                    if stop.is_set():
                        break
                    try:
                        self.once()
                    except sqlite3.OperationalError:
                        pass          # database busy (for example while the other services start): try again next cycle
                    iteration += 1
                    if (count is None or iteration < count) and stop.wait(5):
                        break
            finally:
                try:self.store.flush_ram_settings()      # hand the RAM-held working records to the database before stopping
                except (sqlite3.Error,OSError,ValueError):pass
