"""Open-Meteo API fetcher.

Fetches hourly UV index forecasts for today using two model calls:
- best_match (default): primary UV source; Open-Meteo selects ICON-EU for Basel
- gfs_seamless: US GFS + HRRR blend; genuinely different from ICON-EU

ECMWF IFS (ecmwf_ifs025) does not expose uv_index via Open-Meteo.
ICON seamless and CAMS Europe also return null for uv_index.

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
        # Skip hours where UV is null — model doesn't support UV for this hour
        if uv[i] is None and uv_cs[i] is None:
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
    """Fetch UV from Open-Meteo best_match (selects ICON-EU for Basel)."""
    return _fetch(location, model=None, name="Open-Meteo Global", timeout=timeout)


def fetch_gfs(location: Location, timeout: float = 12.0) -> SourceFetch:
    """Fetch UV from Open-Meteo GFS Seamless (NCEP GFS + HRRR blend)."""
    return _fetch(location, model="gfs_seamless", name="Open-Meteo GFS", timeout=timeout)


def _fetch(
    location: Location,
    model: str | None,
    name: str,
    timeout: float,
) -> SourceFetch:
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
