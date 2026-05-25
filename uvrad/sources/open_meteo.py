"""Open-Meteo API fetcher.

Fetches hourly UV index forecasts for today using two models:
- ICON seamless: MeteoSwiss-backed, high resolution for Alps/Switzerland
- CAMS Europe: Copernicus aerosol-corrected European model

Both return uv_index (cloud-corrected) and uv_index_clear_sky.
These are already at sea level equivalent in the model output;
altitude correction is applied separately in fusion.py.
"""

import time
from datetime import date

import httpx

from uvrad.models import HourlyPoint, Location, SourceFetch

BASE_URL = "https://api.open-meteo.com/v1/forecast"


def _build_url(location: Location, model: str) -> str:
    today = date.today().isoformat()
    params = (
        f"latitude={location.lat}"
        f"&longitude={location.lon}"
        f"&hourly=uv_index,uv_index_clear_sky,cloud_cover"
        f"&start_date={today}&end_date={today}"
        f"&models={model}"
        f"&timezone=Europe%2FZurich"
    )
    return f"{BASE_URL}?{params}"


def _parse_response(data: dict, model_name: str) -> list[HourlyPoint]:
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    uv = hourly.get("uv_index", [])
    uv_cs = hourly.get("uv_index_clear_sky", [])
    cloud = hourly.get("cloud_cover", [])

    points = []
    for i, t in enumerate(times):
        # t is like "2024-05-25T14:00" — extract hour
        hour = int(t[11:13])
        points.append(
            HourlyPoint(
                hour=hour,
                uv_index=max(0.0, float(uv[i]) if uv[i] is not None else 0.0),
                uv_index_clear_sky=max(0.0, float(uv_cs[i]) if uv_cs[i] is not None else 0.0),
                cloud_cover_pct=float(cloud[i]) if cloud[i] is not None else 0.0,
            )
        )
    return points


def fetch_icon(location: Location, timeout: float = 12.0) -> SourceFetch:
    """Fetch hourly UV from Open-Meteo using ICON seamless (MeteoSwiss) model."""
    return _fetch(location, model="icon_seamless", name="Open-Meteo ICON", timeout=timeout)


def fetch_cams(location: Location, timeout: float = 12.0) -> SourceFetch:
    """Fetch hourly UV from Open-Meteo using CAMS Europe (Copernicus) model."""
    return _fetch(location, model="cams_europe", name="Open-Meteo CAMS", timeout=timeout)


def _fetch(location: Location, model: str, name: str, timeout: float) -> SourceFetch:
    url = _build_url(location, model)
    t0 = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout)
        latency_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()
        data = resp.json()
        points = _parse_response(data, model)
        if not points:
            return SourceFetch(name=name, ok=False, error="Empty response", latency_ms=latency_ms)
        return SourceFetch(name=name, ok=True, hourly=points, latency_ms=latency_ms)
    except httpx.TimeoutException:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(name=name, ok=False, error="Timeout", latency_ms=latency_ms)
    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(name=name, ok=False, error=str(e), latency_ms=latency_ms)
