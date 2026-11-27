"""Read-only provider transports and strict pure decoders. No equipment access.

Endpoints are fixed; secrets only travel in HTTPS POST bodies/headers. Raw
account/auth responses are deliberately never archived. Public prices/weather
are archived exactly. GraphQL data is a redacted typed dispatch revision.
"""
from datetime import datetime, timezone
import base64
import http.client
import json
import math
import re
import ssl
from urllib.parse import urlencode

from .collector import strict_json
from .models import number


class ProviderError(ValueError):
    """Only fixed, secret-free reason codes may cross the worker boundary."""


def timestamp(value):
    if not isinstance(value, str):
        raise ProviderError("invalid_timestamp")
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError()
        return number(at.timestamp(), "timestamp", 0)
    except (ValueError, OverflowError):
        raise ProviderError("invalid_timestamp") from None


def request(host, path, body=None, token=None):
    """A disposable process enforces a 12-second total deadline, even on trickle I/O.

    Secrets use an anonymous stdin pipe, never process arguments or files. A
    stalled provider cannot hold the worker indefinitely or hold a database lock.
    """
    import subprocess
    import sys
    if host not in {"api.octopus.energy", "api.open-meteo.com"}:
        raise ProviderError("unsupported_provider")
    try:
        result = subprocess.run([sys.executable, "-m", "solarbank_optimiser.provider_http"],
            input=json.dumps({"host": host, "path": path, "body": body, "token": token}).encode(),
            capture_output=True, timeout=12, check=False)
    except (subprocess.TimeoutExpired, OSError):
        raise ProviderError("connection_failed") from None
    if result.returncode:
        reason = result.stdout.decode("ascii", errors="ignore")
        if reason not in {"rate_limited", "response_too_large", "http_failure", "connection_failed"}:
            reason = "connection_failed"
        raise ProviderError(reason)
    return result.stdout


def _request(host, path, body=None, token=None):
    """One attempt, no redirects, bounded bytes and socket timeout; worker only."""
    if host not in {"api.octopus.energy", "api.open-meteo.com"}:
        raise ProviderError("unsupported_provider")
    headers = {"Accept": "application/json", "User-Agent": "Solarbank-observe/0.3"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = token
    connection = http.client.HTTPSConnection(host, timeout=5, context=ssl.create_default_context())
    try:
        connection.request("POST" if body is not None else "GET", path,
                           json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ProviderError("rate_limited" if response.status == 429 else "http_failure")
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ProviderError("response_too_large")
        return raw
    except ProviderError:
        raise
    except (OSError, http.client.HTTPException):
        raise ProviderError("connection_failed") from None
    finally:
        connection.close()


def public_prices(raw):
    data = strict_json(raw)
    if not isinstance(data.get("results"), list) or data.get("next"):
        # A truncated page is not a successful coverage fetch.
        raise ProviderError("incomplete_price_response")
    result = []
    for row in data["results"]:
        start, end = timestamp(row["valid_from"]), timestamp(row["valid_to"])
        if end <= start:
            raise ProviderError("invalid_price_interval")
        # Octopus standard-unit-rates are GBP pence/kWh including VAT.
        result.append({"start": start, "end": end,
                       "price": number(row["value_inc_vat"]) / 100})
    result.sort(key=lambda r: r["start"])
    if any(a["end"] > b["start"] for a, b in zip(result, result[1:])):
        raise ProviderError("overlapping_prices")
    return result


def prices_path(product, tariff, now, kind='standard-unit-rates'):
    if kind not in {'standard-unit-rates','day-unit-rates','night-unit-rates'}:
        raise ProviderError('invalid_tariff_identity')
    for value in (product, tariff):
        if not re.fullmatch(r"[A-Z0-9-]{3,100}", value):
            raise ProviderError("invalid_tariff_identity")
    if not tariff.startswith("E-1R-" + product + "-"):
        raise ProviderError("mismatched_product_tariff")
    stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    query = urlencode({"period_from": stamp(now), "period_to": stamp(now + 72 * 3600), "page_size": 1500})
    return f"/v1/products/{product}/electricity-tariffs/{tariff}/{kind}/?{query}"


def register_prices(day_raw,night_raw,start,end):
    """Published day/night register prices, without inventing clock eligibility.

    Null valid_to means the supplier currently publishes an ongoing rate. Clip
    that assumption to this bounded horizon and retain cache acquisition times.
    The configured London night window is applied separately by tariff_horizon.
    """
    groups=[]
    for raw in (day_raw,night_raw):
        data=strict_json(raw)
        if not isinstance(data.get('results'),list) or not data['results'] or data.get('next'):
            raise ProviderError('incomplete_price_response')
        rows=[]
        for r in data['results']:
            left=timestamp(r['valid_from'])
            right=timestamp(r['valid_to']) if r['valid_to'] is not None else end
            if right<=left:raise ProviderError('invalid_price_interval')
            left=max(left,start);right=min(right,end)
            if left<right:rows.append({'start':left,'end':right,'price':number(r['value_inc_vat'])/100})
        rows.sort(key=lambda r:r['start'])
        if any(a['end']>b['start'] for a,b in zip(rows,rows[1:])):raise ProviderError('overlapping_prices')
        groups.append(rows)
    boundaries=sorted({start,end}|{r[k] for rows in groups for r in rows for k in ('start','end')})
    result=[]
    for left,right in zip(boundaries,boundaries[1:]):
        pair=[next((r for r in rows if r['start']<=left and right<=r['end']),None) for rows in groups]
        if all(pair):result.append({'start':left,'end':right,'day_price':pair[0]['price'],'night_price':pair[1]['price']})
    if not result:raise ProviderError('incomplete_price_response')
    return result


TOKEN_QUERY = "mutation($input: ObtainJSONWebTokenInput!) { obtainKrakenToken(input: $input) { token } }"
DEVICES_QUERY = "query($accountNumber: String!) { devices(accountNumber: $accountNumber) { id deviceType } }"
DISPATCH_QUERY = "query($deviceId: String!) { flexPlannedDispatches(deviceId: $deviceId) { start end type energyAddedKwh } }"


def graphql(raw, key):
    data = strict_json(raw)
    if data.get("errors") or not isinstance(data.get("data"), dict) or key not in data["data"]:
        raise ProviderError("graphql_error")
    return data["data"][key]


class OctopusAccount:
    def __init__(self, transport=request):
        self.transport = transport
        self.token = None
        self.expiry = 0
        self.key_identity = None

    def call(self, query, variables, token=None):
        return self.transport("api.octopus.energy", "/v1/graphql/",
                              {"query": query, "variables": variables}, token)

    def authenticate(self, api_key, now):
        import hashlib
        identity = hashlib.sha256(api_key.encode()).digest()
        if self.token is None or self.key_identity != identity or now >= self.expiry - 60:
            self.token = None
            token = graphql(self.call(TOKEN_QUERY, {"input": {"APIKey": api_key}}), "obtainKrakenToken")["token"]
            try:
                # Expiry is scheduling metadata, not a substitute for server verification.
                claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "===").decode())
                expiry = number(claims["exp"], "token expiry", now + 60)
            except (ValueError, KeyError, IndexError, TypeError):
                raise ProviderError("invalid_token") from None
            self.token, self.expiry, self.key_identity = token, expiry, identity
        return self.token

    def linked_devices(self, api_key, account, now):
        self.authenticate(api_key,now)
        try:
            devices=graphql(self.call(DEVICES_QUERY, {'accountNumber':account},self.token),'devices')
            if not isinstance(devices,list) or len(devices)>1000:
                raise ProviderError('linked_device_not_verified')
            result=[]
            for d in devices:
                if isinstance(d,dict) and d.get('deviceType') in {'ELECTRIC_VEHICLES','CHARGE_POINTS'}:
                    if not isinstance(d.get('id'),str) or not 1<=len(d['id'])<=100:
                        raise ProviderError('linked_device_not_verified')
                    result.append({'id':d['id'],'deviceType':d['deviceType']})
            return result
        except Exception:
            self.token=None
            raise

    def dispatches(self, api_key, account, device, now):
        self.authenticate(api_key,now)
        try:
            devices = graphql(self.call(DEVICES_QUERY, {"accountNumber": account}, self.token), "devices")
            if not isinstance(devices, list) or not any(d.get("id") == device and d.get("deviceType") in
                                                        {"ELECTRIC_VEHICLES", "CHARGE_POINTS"} for d in devices):
                raise ProviderError("linked_device_not_verified")
            raw = self.call(DISPATCH_QUERY, {"deviceId": device}, self.token)
            rows = graphql(raw, "flexPlannedDispatches")
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ProviderError("invalid_dispatch_list")
            result = []
            for row in rows:
                # Never turn manual/boost/test into smart slots. A row that is not a smart row is not used, so its other fields are not judged: one odd bump-charge
                # row (8 November: the feed failed as soon as a bump charge began) must not discard the genuine smart slots listed beside it.
                # Any other type (including one Octopus adds later, such as a bump charge's) is skipped too: it is never turned into a smart slot, which is the safe direction,
                # and it does not take the genuine smart slots down with it.
                if row["type"] != "SMART":
                    continue
                start, end = timestamp(row["start"]), timestamp(row["end"])
                if end <= start or end - start > 86400 or end > now + 7 * 86400:
                    raise ProviderError("invalid_dispatch_interval")
                energy = row["energyAddedKwh"]
                if isinstance(energy, bool) or not isinstance(energy, (int, float, str)):
                    raise ProviderError("invalid_dispatch_energy")
                # Octopus reports the energy added as a NEGATIVE number ("-0.6154", "-22.155": 9 November, the first real slots); only its size is sanity-checked, never its sign.
                try:
                    number(float(energy), "dispatch energy", -1000, 1000)
                except ValueError:
                    raise ProviderError("invalid_dispatch_energy") from None
                if end > now:
                    result.append({"start": start, "end": end})
            return sorted(result, key=lambda r: r["start"])
        except Exception:
            self.token = None  # A failed account call must reauthenticate next time.
            raise


def weather_path():
    return "/v1/forecast?" + urlencode({"latitude": 51.5074, "longitude": -0.1278,
        "hourly": "shortwave_radiation,diffuse_radiation,direct_normal_irradiance,temperature_2m,wind_speed_10m",
        "timezone": "UTC", "timeformat": "unixtime", "forecast_days": 4, "wind_speed_unit": "ms"})


def _present(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def first_missing(values):
    """Index of the first missing or non-numeric value, or the length if there is none.

    Issue #32: one null near the end of Open-Meteo's 4-day forecast used to reject the whole forecast, and with it all planning. The forecast is now
    cut at the first missing hour instead. Nothing is invented (no synthetic solar): if what remains does not cover the 48-hour horizon, the forecast
    is still refused."""
    return next((i for i, v in enumerate(values) if not _present(v)), len(values))


def solar_forecast(raw, acquired_at):
    """Open-Meteo preceding-hour means; pvlib midpoint transposition, two 1kWp planes.

    Available DC potential, not battery-terminal output. No fitted curtailment or
    training from full SOC. Fixed 10% system derate remains provisional.
    """
    import pandas as pd
    import pvlib
    data = strict_json(raw)
    if (abs(number(data["latitude"]) - 51.5074) > .1 or
            abs(number(data["longitude"]) + 0.1278) > .1 or data.get("utc_offset_seconds") != 0):
        raise ProviderError("wrong_weather_location_or_timezone")
    h, units = data["hourly"], data["hourly_units"]
    names = {"shortwave_radiation": "W/m²", "diffuse_radiation": "W/m²",
             "direct_normal_irradiance": "W/m²", "temperature_2m": "°C", "wind_speed_10m": "m/s"}
    times = h["time"]
    if not isinstance(times, list) or not 2 <= len(times) <= 400:
        raise ProviderError("invalid_weather_length")
    for name, unit in names.items():
        if units.get(name) != unit or len(h[name]) != len(times):
            raise ProviderError("wrong_weather_units_or_length")
    usable = min(first_missing(h[name]) for name in names)          # the forecast ends at the first missing hour
    times = times[:usable]
    h = {name: h[name][:usable] for name in names}
    if len(times) < 2:
        raise ProviderError("invalid_weather_length")
    for i, t in enumerate(times):
        number(t, "weather time", 0)
        if i and t != times[i-1] + 3600:
            raise ProviderError("weather_gap")
        for name in names:
            number(h[name][i], name, -100 if name == "temperature_2m" else 0,
                   100 if name == "temperature_2m" else 2000)
    index = pd.to_datetime([t - 1800 for t in times], unit="s", utc=True)
    sun = pvlib.solarposition.get_solarposition(index, 51.5074, -0.1278)
    series = lambda name: pd.Series(h[name], index=index)
    power = pd.Series(0., index=index)
    for tilt in (80, 20):
        poa = pvlib.irradiance.get_total_irradiance(tilt, 180, sun.apparent_zenith, sun.azimuth,
                series("direct_normal_irradiance"), series("shortwave_radiation"), series("diffuse_radiation"))["poa_global"]
        temperature = pvlib.temperature.faiman(poa, series("temperature_2m"), series("wind_speed_10m"))
        power += pvlib.pvsystem.pvwatts_dc(poa, temperature, 1000, -.004) / 1000 * .9
    intervals = [{"start": t-3600, "end": t, "pv_kw": number(max(0, float(p)), "PV kW", 0)}
                 for t, p in zip(times, power) if t > acquired_at]
    if not intervals or intervals[-1]["end"] < acquired_at + 48 * 3600:
        raise ProviderError("weather_horizon_incomplete")
    return {"model": "two-plane-pvlib-v1", "acquired_at": acquired_at, "intervals": intervals,
            "provider": "Open-Meteo forecast", "averaging": "preceding hour mean; midpoint solar position",
            "provider_model_issue_at": None, "pvlib": pvlib.__version__, "derate": .9,
            "planes": [{"kwp": 1, "tilt": t, "azimuth": 180} for t in (80, 20)],
            "calibration": "Provisional; no curtailment training"}
