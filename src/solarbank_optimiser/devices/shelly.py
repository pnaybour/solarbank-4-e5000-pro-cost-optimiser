"""Shelly Pro EM-50 over HTTP GET only (em1:0 Solarbank, em1:1 EV charger). The sample keys are role names: see shelly_channels."""
import http.client
import json
import math

from ..shelly_channels import CHANNELS
from .errors import DeviceError

MAX_BYTES = 65_536


def parse_status(data):
    """GetStatus JSON -> the sample fields, named by role (without a time stamp)."""
    sample = {}
    try:
        for i, name in enumerate(CHANNELS):
            channel = data[f"em1:{i}"]
            if channel.get("errors"):
                raise DeviceError("invalid", f"{name} (em1:{i}): {channel['errors']}")
            for field, suffix in (("act_power", ""), ("voltage", "_voltage"), ("current", "_current")):
                value = channel[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise DeviceError("invalid", f"{name} (em1:{i}): unavailable {field}")
                sample[f"{name}{suffix}"] = value
    except (KeyError, TypeError, AttributeError):
        raise DeviceError("invalid", "Unexpected Shelly status layout") from None
    return sample


def parse_device_info(data):
    """Identity fields from GetDeviceInfo; never includes credentials."""
    try:
        return {"id": data["id"], "mac": data["mac"], "model": data["model"], "gen": data.get("gen"),
                "firmware": data.get("ver"), "auth_enabled": bool(data.get("auth_en"))}
    except (KeyError, TypeError):
        raise DeviceError("invalid", "Unexpected Shelly device information") from None


def _get(host, path, timeout, port=80, connection_factory=http.client.HTTPConnection):
    connection = connection_factory(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:           # redirects and errors are never followed
            raise DeviceError("protocol", f"Shelly HTTP status {response.status}")
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise DeviceError("protocol", "Shelly response too large")
        try:
            return json.loads(raw)
        except ValueError:
            raise DeviceError("protocol", "Shelly response is not JSON") from None
    except TimeoutError:
        raise DeviceError("timeout", "Shelly did not answer in time") from None
    except (OSError, http.client.HTTPException) as error:
        raise DeviceError("closed", f"Shelly request failed: {error.__class__.__name__}") from None
    finally:
        connection.close()


def read(host, timeout=2.0, **options):
    return parse_status(_get(host, "/rpc/Shelly.GetStatus", timeout, **options))


def device_info(host, timeout=2.0, **options):
    return parse_device_info(_get(host, "/rpc/Shelly.GetDeviceInfo", timeout, **options))


def read_channels(host, timeout=2.0, **options):
    """Per-channel status (EM1.GetStatus for ids 0 and 1): about 30 ms each against 135 ms for the full status."""
    data = {f"em1:{i}": _get(host, f"/rpc/EM1.GetStatus?id={i}", timeout, **options) for i in range(len(CHANNELS))}
    return parse_status(data)
