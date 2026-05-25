"""BFS UV ground-station data.

Primary: parse the live chart PNG at uvi.bfs.de — gives ~30-minute UV readings
for the current day.  Fallback: query the WFS uv_index_timeseries layer for the
daily peak (one integer per day).

Chart PNG URL pattern:
  https://uvi.bfs.de/Tagesgrafiken/EEr_{StationName}_today.png
  e.g. EEr_Schauinsland_today.png  (updated every 6 minutes)

PNG pixel layout (640×480, fixed Highcharts template):
  X_AXIS_START=105 (hour 6:00), X_AXIS_END=578 (hour 21:00)
  Y_BOTTOM=427 (UV=0 baseline), Y_TOP=68 (UV=9 top gridline)
  Bars are adjacent with no gaps; data ends where bars stop.
  Timestamp indicator box (dark blue) sits at y<72, top-right corner.

WFS layers at https://www.imis.bfs.de/ogc/opendata/ows:
  opendata:uv_index_tagesverlauf  — station metadata (lat/lon/alt, 35 stations)
  opendata:uv_index_timeseries    — daily peak, one record/day per station;
                                    requires viewparams=station_id:<id>
  (Sub-daily data is NOT in the WFS — the PNG chart is the only source.)
"""

import datetime as dt
import io
import logging
import math
import time

import httpx

from uvrad.models import HourlyPoint, SourceFetch

logger = logging.getLogger(__name__)

WFS_BASE_URL = "https://www.imis.bfs.de/ogc/opendata/ows"
WFS_STATIONS_LAYER = "opendata:uv_index_tagesverlauf"
WFS_TIMESERIES_LAYER = "opendata:uv_index_timeseries"
PNG_BASE_URL = "https://uvi.bfs.de/Tagesgrafiken"

_BASEL_LAT = 47.5596
_BASEL_LON = 7.5886

_DEFAULT_STATION_ID = "SL"
_DEFAULT_STATION_NAME = "Schauinsland"
_DEFAULT_STATION_ALT_M = 1206.0

# PNG chart pixel calibration (derived from live 640×480 chart)
_PNG_X_START = 105    # x-pixel at hour 6:00
_PNG_X_END = 578      # x-pixel at hour 21:00 (right axis)
_PNG_Y_BOTTOM = 427   # y-pixel = UV 0 (baseline)
_PNG_Y_TOP = 68       # y-pixel = UV 9 (top gridline)
_PNG_UV_MAX = 9.0
_PNG_PX_PER_HOUR = (_PNG_X_END - _PNG_X_START) / 15.0  # 15 hours (6–21)

# Legacy constants kept for import compatibility
SCHAUINSLAND_ALT_M = _DEFAULT_STATION_ALT_M
FREIBURG_ALT_M = 278.0
SCHAUINSLAND_STATION_ID = _DEFAULT_STATION_ID
FREIBURG_STATION_ID = "FREI"
BFS_URL = "https://www.bfs.de/DE/themen/opt/uv/uv-index/aktuelle-tagesverlaeufe/_documents/schauinsland_node.html"
BFS_SCHAUINSLAND_URL = BFS_URL


def fetch_bfs_schauinsland(timeout: float = 15.0) -> SourceFetch:
    meta = _discover_station(name_hint="Schauinsland", timeout=timeout)
    return _fetch_station(meta, fallback_name="BFS Schauinsland", timeout=timeout)


def fetch_bfs_freiburg(timeout: float = 15.0) -> SourceFetch:
    meta = _discover_nearest_station(timeout=timeout)
    return _fetch_station(meta, fallback_name="BFS nearest", timeout=timeout)


def fetch_bfs_best(timeout: float = 15.0) -> SourceFetch:
    """Fetch today's UV from the nearest BFS station to Basel (Schauinsland)."""
    meta = _discover_station(name_hint="Schauinsland", timeout=timeout)
    if meta is None:
        meta = _discover_nearest_station(timeout=timeout)
    return _fetch_station(meta, fallback_name="BFS Schauinsland", timeout=timeout)


def _fetch_station(meta: dict | None, fallback_name: str, timeout: float) -> SourceFetch:
    if meta:
        station_id = meta["station_id"]
        station_name = meta.get("station_name", fallback_name)
        alt_m = float(meta.get("hoehe") or _DEFAULT_STATION_ALT_M)
        display_name = f"BFS {station_name}"
    else:
        station_id = _DEFAULT_STATION_ID
        station_name = _DEFAULT_STATION_NAME
        alt_m = _DEFAULT_STATION_ALT_M
        display_name = fallback_name

    # Try PNG chart first (gives ~30-min resolution for today)
    result = _fetch_png(station_name, display_name, alt_m, timeout)
    if result.ok:
        return result

    # Fallback: WFS daily peak (single integer for today)
    fallback = _fetch_daily_peak_wfs(station_id, display_name, alt_m, timeout)
    if fallback.ok:
        return fallback

    return SourceFetch(
        name=display_name,
        ok=False,
        error=f"PNG: {result.error}; WFS: {fallback.error}",
        latency_ms=result.latency_ms + fallback.latency_ms,
        station_alt_m=alt_m,
    )


# ── PNG chart parser ────────────────────────────────────────────────────────


def _fetch_png(station_name: str, display_name: str, alt_m: float, timeout: float) -> SourceFetch:
    """Fetch and parse the daily chart PNG for a BFS station."""
    url = f"{PNG_BASE_URL}/EEr_{station_name}_today.png"
    t0 = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        latency_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()

        points = _parse_png(resp.content)
        if not points:
            return SourceFetch(
                name=display_name,
                ok=False,
                error="PNG: no UV readings extracted",
                latency_ms=latency_ms,
                station_alt_m=alt_m,
            )

        logger.debug("BFS PNG %s: %d hourly points, peak=%.1f", station_name, len(points), max(p.uv_index for p in points))
        return SourceFetch(
            name=display_name,
            ok=True,
            hourly=points,
            latency_ms=latency_ms,
            station_alt_m=alt_m,
        )
    except httpx.TimeoutException:
        return SourceFetch(
            name=display_name,
            ok=False,
            error="PNG timeout",
            latency_ms=(time.monotonic() - t0) * 1000,
            station_alt_m=alt_m,
        )
    except Exception as e:
        logger.warning("BFS PNG fetch failed for %s: %s", station_name, e)
        return SourceFetch(
            name=display_name,
            ok=False,
            error=f"PNG: {e}",
            latency_ms=(time.monotonic() - t0) * 1000,
            station_alt_m=alt_m,
        )


def _parse_png(png_bytes: bytes) -> list[HourlyPoint]:
    """Extract hourly UV readings from a BFS chart PNG.

    Scans each half-hour column bottom-up to find the bar top, converts the
    y-pixel to a UV value, then averages the two half-hour slots per hour.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed; PNG parsing unavailable")
        return []

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    px = img.load()
    img_w, img_h = img.size

    # Bail out if the chart template has changed significantly
    if img_w != 640 or img_h != 480:
        logger.warning("BFS PNG size changed: %dx%d (expected 640x480)", img_w, img_h)
        return []

    def _is_bar(r: int, g: int, b: int) -> bool:
        if r > 245 and g > 245 and b > 245:
            return False  # white background
        if abs(r - g) < 12 and abs(g - b) < 12:
            return False  # gray (gridlines, clear-sky curve)
        if r < 20 and g < 20 and b < 20:
            return False  # black (axis lines)
        if b > r + 20 and b > g + 20:
            return False  # blue (timestamp indicator top-right)
        return True

    # Find the rightmost x where bar data exists (where today's data ends)
    x_data_end = _PNG_X_START
    for x in range(_PNG_X_START, _PNG_X_END):
        c = px[x, _PNG_Y_BOTTOM - 2]
        if _is_bar(c[0], c[1], c[2]):
            x_data_end = x

    # Half-hour readings
    half_hour_values: dict[tuple[int, int], float] = {}
    for hour in range(6, 21):
        for half in range(2):
            t_frac = (hour - 6) + half * 0.5
            x = int(_PNG_X_START + (t_frac + 0.25) * _PNG_PX_PER_HOUR)
            if x > x_data_end or x >= img_w:
                continue

            bar_top_y: int | None = None
            for y in range(_PNG_Y_BOTTOM - 1, _PNG_Y_TOP - 1, -1):
                c = px[x, y]
                if _is_bar(c[0], c[1], c[2]):
                    bar_top_y = y
                elif bar_top_y is not None:
                    break

            if bar_top_y is not None:
                uv = max(0.0, (_PNG_Y_BOTTOM - bar_top_y) / (_PNG_Y_BOTTOM - _PNG_Y_TOP) * _PNG_UV_MAX)
                half_hour_values[(hour, half)] = round(uv, 2)

    if not half_hour_values:
        return []

    # Average two half-hour slots per hour
    hourly: dict[int, list[float]] = {}
    for (hour, _half), uv in half_hour_values.items():
        hourly.setdefault(hour, []).append(uv)

    return sorted(
        [
            HourlyPoint(
                hour=h,
                uv_index=sum(vals) / len(vals),
                uv_index_clear_sky=sum(vals) / len(vals),
                cloud_cover_pct=0.0,
            )
            for h, vals in hourly.items()
            if sum(vals) / len(vals) > 0
        ],
        key=lambda p: p.hour,
    )


# ── WFS daily-peak fallback ─────────────────────────────────────────────────


def _fetch_daily_peak_wfs(
    station_id: str, display_name: str, alt_m: float, timeout: float
) -> SourceFetch:
    """Fetch today's peak UV (integer) from the WFS uv_index_timeseries layer."""
    t0 = time.monotonic()
    try:
        resp = httpx.get(
            WFS_BASE_URL,
            params={
                "service": "WFS",
                "version": "1.1.0",
                "request": "GetFeature",
                "typeName": WFS_TIMESERIES_LAYER,
                "outputFormat": "application/json",
                "viewparams": f"station_id:{station_id}",
                "sortBy": "date D",
                "maxFeatures": "3",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()

        today_utc = dt.datetime.now(tz=dt.UTC).date()
        for feat in resp.json().get("features", []):
            props = feat.get("properties", {})
            try:
                record_date = dt.datetime.fromisoformat(
                    props.get("date", "").replace("Z", "+00:00")
                ).date()
            except (ValueError, AttributeError):
                continue
            raw_uv = props.get("uv_index")
            if raw_uv is None:
                continue
            uv_val = float(raw_uv)
            if not (0.0 <= uv_val <= 25.0):
                continue
            if record_date in (today_utc, today_utc - dt.timedelta(days=1)):
                logger.debug("BFS WFS %s: peak_uv=%.0f date=%s", station_id, uv_val, record_date)
                return SourceFetch(
                    name=display_name,
                    ok=True,
                    hourly=[HourlyPoint(hour=13, uv_index=uv_val, uv_index_clear_sky=uv_val, cloud_cover_pct=0.0)],
                    latency_ms=latency_ms,
                    station_alt_m=alt_m,
                )

        return SourceFetch(
            name=display_name,
            ok=False,
            error="no valid UV reading in WFS response",
            latency_ms=latency_ms,
            station_alt_m=alt_m,
        )
    except httpx.TimeoutException:
        return SourceFetch(
            name=display_name,
            ok=False,
            error="WFS timeout",
            latency_ms=(time.monotonic() - t0) * 1000,
            station_alt_m=alt_m,
        )
    except Exception as e:
        logger.exception("BFS WFS fetch failed for %s", station_id)
        return SourceFetch(
            name=display_name,
            ok=False,
            error=str(e),
            latency_ms=(time.monotonic() - t0) * 1000,
            station_alt_m=alt_m,
        )


# ── Station discovery ────────────────────────────────────────────────────────


def _discover_station(name_hint: str, timeout: float) -> dict | None:
    """Return WFS metadata for the station matching name_hint."""
    try:
        resp = httpx.get(
            WFS_BASE_URL,
            params={
                "service": "WFS",
                "version": "1.1.0",
                "request": "GetFeature",
                "typeName": WFS_STATIONS_LAYER,
                "outputFormat": "application/json",
                "maxFeatures": "100",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        hint_lower = name_hint.lower()
        for feat in resp.json().get("features", []):
            props = feat.get("properties", {})
            if hint_lower in props.get("station_name", "").lower():
                logger.debug(
                    "Discovered BFS station: %s id=%s alt=%sm",
                    props.get("station_name"),
                    props.get("station_id"),
                    props.get("hoehe"),
                )
                return props
    except Exception as e:
        logger.warning("BFS station discovery failed: %s", e)
    return None


def _discover_nearest_station(timeout: float) -> dict | None:
    """Return WFS metadata for the BFS station nearest to Basel."""
    try:
        resp = httpx.get(
            WFS_BASE_URL,
            params={
                "service": "WFS",
                "version": "1.1.0",
                "request": "GetFeature",
                "typeName": WFS_STATIONS_LAYER,
                "outputFormat": "application/json",
                "maxFeatures": "100",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            return None
        nearest = min(
            features,
            key=lambda f: math.hypot(
                f["properties"].get("latitude", 0) - _BASEL_LAT,
                f["properties"].get("longitude", 0) - _BASEL_LON,
            ),
        )
        return nearest["properties"]
    except Exception as e:
        logger.warning("BFS nearest-station discovery failed: %s", e)
    return None
