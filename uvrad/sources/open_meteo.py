"""Open-Meteo API fetcher.

Fetches hourly UV index forecasts for today using two model calls:
- GFS global (default/best_match): primary UV source, always includes uv_index
- ECMWF IFS 0.25°: secondary UV source for cross-checking

ICON seamless and CAMS Europe do not expose uv_index via Open-Meteo
(they return null). UV index is available from their global blend or
explicitly from ECMWF IFS.

Open-Meteo outputs are treated as sea-level equivalent;
altitude correction is applied separately in fusion.py.
"""

import time
from datetime import date

import httpx

from uvrad.models import HourlyPoint, Location, SourceFetch

BASE_URL = "https://api.open-meteo.com/v1/forecast"


def _build_url(location: Location, model: str | None) -> str:
    today = date.today().isoformat()
    model_param = f"&models={model}" if model else ""
    params = (
        f"latitude={location.lat}"
        f"&longitude={location.lon}"
        f"&hourly=uv_index,uv_index_clear_sky,cloud_cover"
        f"&start_date={today}&end_date={today}"
        f"{model_param}"
        f"&timezone=Europe%2FZurich"
    )
    return f"{BASE_URL}?{params}"


def _parse_response(data: dict) -> list[HourlyPoint]:
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    uv = hourly.get("uv_index", [])
    uv_cs = hourly.get("uv_index_clear_sky", [])
    cloud = hourly.get("cloud_cover", [])

    points = []
    for i, t in enumerate(times):
        hour = int(t[11:13])
        uv_val = float(uv[i]) if uv[i] is not None else 0.0
        uv_cs_val = float(uv_cs[i]) if uv_cs[i] is not None else 0.0
        # Treat null UV as missing rather than zero so we can detect it
        if uv_val == 0.0 and uv_cs_val == 0.0 and (uv[i] is None or uv_cs[i] is None):
            continue
        points.append(
            HourlyPoint(
                hour=hour,
                uv_index=max(0.0, uv_val),
                uv_index_clear_sky=max(0.0, uv_cs_val),
                cloud_cover_pct=float(cloud[i]) if cloud[i] is not None else 0.0,
            )
        )
    return points


def fetch_icon(location: Location, timeout: float = 12.0) -> SourceFetch:
    """Fetch UV from Open-Meteo default global model (best_match includes ECMWF UV)."""
    return _fetch(location, model=None, name="Open-Meteo Global", timeout=timeout)


def fetch_cams(location: Location, timeout: float = 12.0) -> SourceFetch:
    """Fetch UV from Open-Meteo ECMWF IFS 0.25° model."""
    return _fetch(location, model="ecmwf_ifs025", name="Open-Meteo ECMWF", timeout=timeout)


def _fetch(location: Location, model: str | None, name: str, timeout: float) -> SourceFetch:
    url = _build_url(location, model)
    t0 = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout)
        latency_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()
        data = resp.json()
        points = _parse_response(data)
        if not points:
            return SourceFetch(
                name=name, ok=False, error="No UV data in response", latency_ms=latency_ms
            )
        return SourceFetch(name=name, ok=True, hourly=points, latency_ms=latency_ms)
    except httpx.TimeoutException:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(name=name, ok=False, error="Timeout", latency_ms=latency_ms)
    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(name=name, ok=False, error=str(e), latency_ms=latency_ms)
