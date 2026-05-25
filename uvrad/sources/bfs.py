"""BFS UV ground-station data via IMIS WFS API.

The German Federal Office for Radiation Protection (BFS) operates UV measurement
stations. We query two stations near Basel via the IMIS WFS API:

  Schauinsland: 1284m, ~60km SSW of Basel — primary
  Freiburg:      278m, ~60km ENE of Basel — backup

WFS endpoint: https://www.imis.bfs.de/ogc/opendata/ows
Layer: opendata:abi_30min (30-minute UV data)
Station IDs: EMF_STATION_UV_Schauinsland, EMF_STATION_UV_Freiburg

Falls back to HTML scraping if WFS is unavailable.
"""

import datetime as dt
import logging
import re
import time

import httpx
from bs4 import BeautifulSoup

from uvrad.models import HourlyPoint, SourceFetch

logger = logging.getLogger(__name__)

WFS_BASE_URL = "https://www.imis.bfs.de/ogc/opendata/ows"
WFS_TYPENAME = "opendata:abi_30min"

BFS_SCHAUINSLAND_URL = (
    "https://www.bfs.de/DE/themen/opt/uv/uv-index/aktuelle-tagesverlaeufe"
    "/_documents/schauinsland_node.html"
)
BFS_FREIBURG_URL = (
    "https://www.bfs.de/DE/themen/opt/uv/uv-index/aktuelle-tagesverlaeufe"
    "/_documents/freiburg_node.html"
)

SCHAUINSLAND_ALT_M = 1284.0
FREIBURG_ALT_M = 278.0
SCHAUINSLAND_STATION_ID = "EMF_STATION_UV_Schauinsland"
FREIBURG_STATION_ID = "EMF_STATION_UV_Freiburg"

# Keep legacy name for import compatibility
BFS_URL = BFS_SCHAUINSLAND_URL

# Candidate property names for WFS feature properties (try in order)
_UV_PROP_CANDIDATES = ["uv_index", "uvi", "value", "messwert", "uv", "UV_Index", "UV"]
_TIME_PROP_CANDIDATES = [
    "end_measure",
    "time",
    "zeitstempel",
    "datetime",
    "timestamp",
    "datum",
    "date",
]
_STATION_PROP_CANDIDATES = ["station_id", "station", "kenn", "standort_id", "id"]


def fetch_bfs_schauinsland(timeout: float = 15.0) -> SourceFetch:
    result = _fetch_bfs_wfs(
        SCHAUINSLAND_STATION_ID, "BFS Schauinsland", SCHAUINSLAND_ALT_M, timeout
    )
    if result.ok:
        return result
    html_result = _fetch_bfs_html_station(
        BFS_SCHAUINSLAND_URL, "BFS Schauinsland", SCHAUINSLAND_ALT_M, timeout
    )
    if html_result.ok:
        return html_result
    return SourceFetch(
        name="BFS Schauinsland",
        ok=False,
        error=f"WFS: {result.error}; HTML: {html_result.error}",
        latency_ms=result.latency_ms + html_result.latency_ms,
        station_alt_m=SCHAUINSLAND_ALT_M,
    )


def fetch_bfs_freiburg(timeout: float = 15.0) -> SourceFetch:
    result = _fetch_bfs_wfs(FREIBURG_STATION_ID, "BFS Freiburg", FREIBURG_ALT_M, timeout)
    if result.ok:
        return result
    html_result = _fetch_bfs_html_station(BFS_FREIBURG_URL, "BFS Freiburg", FREIBURG_ALT_M, timeout)
    if html_result.ok:
        return html_result
    return SourceFetch(
        name="BFS Freiburg",
        ok=False,
        error=f"WFS: {result.error}; HTML: {html_result.error}",
        latency_ms=result.latency_ms + html_result.latency_ms,
        station_alt_m=FREIBURG_ALT_M,
    )


def fetch_bfs_best(timeout: float = 15.0) -> SourceFetch:
    """Try Schauinsland first; fall back to Freiburg if unavailable."""
    result = fetch_bfs_schauinsland(timeout)
    if result.ok:
        return result
    freiburg = fetch_bfs_freiburg(timeout)
    if freiburg.ok:
        return freiburg
    return SourceFetch(
        name=result.name,
        ok=False,
        error=f"Schauinsland: {result.error}; Freiburg: {freiburg.error}",
        latency_ms=result.latency_ms + freiburg.latency_ms,
        station_alt_m=SCHAUINSLAND_ALT_M,
    )


def _fetch_bfs_wfs(station_id: str, name: str, station_alt_m: float, timeout: float) -> SourceFetch:
    """Fetch UV data from IMIS WFS API for a given station.

    Tries two request variants:
      1. With CQL_FILTER by station_id (server-side filter)
      2. Without filter — fetch all stations, filter client-side
         (fallback in case the server rejects the CQL filter)
    """
    t0 = time.monotonic()
    attempts = [
        {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": WFS_TYPENAME,
            "outputFormat": "application/json",
            "CQL_FILTER": f"station_id='{station_id}'",
            "maxFeatures": "60",
        },
        # No CQL filter — server may not support it; filter client-side
        {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": WFS_TYPENAME,
            "outputFormat": "application/json",
            "maxFeatures": "200",
        },
    ]

    last_error = "no attempts made"
    for attempt_idx, params in enumerate(attempts):
        try:
            resp = httpx.get(WFS_BASE_URL, params=params, timeout=timeout, follow_redirects=True)
            latency_ms = (time.monotonic() - t0) * 1000

            if not resp.is_success:
                logger.warning(
                    "WFS HTTP %d for %s (attempt %d): %r",
                    resp.status_code,
                    station_id,
                    attempt_idx,
                    resp.text[:300],
                )
                last_error = f"HTTP {resp.status_code}"
                continue

            body = resp.text.strip()
            if not body:
                logger.warning("WFS empty response for %s (attempt %d)", station_id, attempt_idx)
                last_error = "empty response"
                continue

            try:
                data = resp.json()
            except Exception as json_err:
                logger.warning(
                    "WFS non-JSON for %s (attempt %d): %s | body[:400]=%r",
                    station_id,
                    attempt_idx,
                    json_err,
                    body[:400],
                )
                last_error = f"non-JSON response: {body[:120]!r}"
                continue

            features = data.get("features", [])
            logger.debug(
                "WFS %s attempt %d: got %d features", station_id, attempt_idx, len(features)
            )

            if not features:
                logger.warning(
                    "WFS 0 features for %s (attempt %d). Top-level keys: %s",
                    station_id,
                    attempt_idx,
                    list(data.keys()),
                )
                last_error = "0 features returned"
                continue

            # On the no-filter attempt, narrow to this station client-side
            if attempt_idx == 1:
                features = _filter_features_by_station(features, station_id)
                logger.debug(
                    "WFS %s after client-side filter: %d features", station_id, len(features)
                )
                if not features:
                    # Log all distinct station IDs we saw so we can fix the ID
                    all_ids = _collect_station_ids(data.get("features", []))
                    logger.warning(
                        "WFS no features matching %s. Station IDs in response: %s",
                        station_id,
                        all_ids[:20],
                    )
                    last_error = (
                        f"no features matching station_id {station_id!r}; seen: {all_ids[:5]}"
                    )
                    continue

            sample_props = features[0].get("properties", {})
            logger.debug(
                "WFS %s sample properties: %s | values: %s",
                station_id,
                list(sample_props.keys()),
                {k: v for k, v in list(sample_props.items())[:6]},
            )

            points = _parse_wfs_features(features, station_id)
            if not points:
                logger.warning(
                    "WFS %s: could not parse UV from properties %s",
                    station_id,
                    list(sample_props.keys()),
                )
                last_error = f"could not parse UV from props {list(sample_props.keys())}"
                continue

            return SourceFetch(
                name=name,
                ok=True,
                hourly=points,
                latency_ms=latency_ms,
                station_alt_m=station_alt_m,
            )

        except httpx.TimeoutException:
            latency_ms = (time.monotonic() - t0) * 1000
            return SourceFetch(
                name=name,
                ok=False,
                error="WFS timeout",
                latency_ms=latency_ms,
                station_alt_m=station_alt_m,
            )
        except Exception as e:
            logger.exception("WFS fetch failed for %s (attempt %d)", station_id, attempt_idx)
            last_error = f"exception: {e}"
            continue

    return SourceFetch(
        name=name,
        ok=False,
        error=f"WFS: {last_error}",
        latency_ms=(time.monotonic() - t0) * 1000,
        station_alt_m=station_alt_m,
    )


def _filter_features_by_station(features: list[dict], station_id: str) -> list[dict]:
    """Filter GeoJSON features to those matching station_id (tries multiple property names)."""
    station_id_lower = station_id.lower()
    for prop in _STATION_PROP_CANDIDATES:
        matched = [
            f
            for f in features
            if str(f.get("properties", {}).get(prop, "")).lower() == station_id_lower
        ]
        if matched:
            return matched
    # Substring match as last resort
    for prop in _STATION_PROP_CANDIDATES:
        matched = [
            f
            for f in features
            if station_id_lower in str(f.get("properties", {}).get(prop, "")).lower()
        ]
        if matched:
            return matched
    return []


def _collect_station_ids(features: list[dict]) -> list[str]:
    """Extract unique station ID values from features for diagnostic logging."""
    seen: set[str] = set()
    for feat in features:
        props = feat.get("properties", {})
        for prop in _STATION_PROP_CANDIDATES:
            val = props.get(prop)
            if val is not None:
                seen.add(str(val))
    return sorted(seen)


def _parse_wfs_features(features: list[dict], station_id: str) -> list[HourlyPoint]:
    """Parse GeoJSON features into hourly UV points.

    Tries multiple candidate property names since the exact schema is
    not confirmed from local testing (network to imis.bfs.de is blocked
    from dev environment).
    """
    local_offset = 2 if _is_summer() else 1

    # Detect which property holds UV value and timestamp
    if not features:
        return []

    sample = features[0].get("properties", {})

    uv_prop = _detect_prop(sample, _UV_PROP_CANDIDATES)
    time_prop = _detect_prop(sample, _TIME_PROP_CANDIDATES)

    if uv_prop is None:
        logger.warning(
            "Cannot detect UV property from WFS response. Available: %s", list(sample.keys())
        )
        return []

    logger.debug("WFS using uv_prop=%s, time_prop=%s", uv_prop, time_prop)

    # Aggregate 30-min readings into hourly slots (mean)
    hourly_readings: dict[int, list[float]] = {}

    for feat in features:
        props = feat.get("properties", {})
        raw_uv = props.get(uv_prop)
        if raw_uv is None:
            continue
        try:
            uv_val = float(raw_uv)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= uv_val <= 25.0):
            continue

        local_hour: int | None = None
        if time_prop:
            raw_time = props.get(time_prop)
            local_hour = _parse_hour(raw_time, local_offset)

        # Fallback: try to detect hour from any timestamp-like property
        if local_hour is None:
            for _k, v in props.items():
                if isinstance(v, str) and ("T" in v or ":" in v):
                    local_hour = _parse_hour(v, local_offset)
                    if local_hour is not None:
                        break

        if local_hour is None:
            continue

        hourly_readings.setdefault(local_hour, []).append(uv_val)

    if not hourly_readings:
        return []

    points = [
        HourlyPoint(
            hour=hour,
            uv_index=sum(vals) / len(vals),
            uv_index_clear_sky=sum(vals) / len(vals),
            cloud_cover_pct=0.0,
        )
        for hour, vals in hourly_readings.items()
    ]
    return sorted(points, key=lambda p: p.hour)


def _detect_prop(props: dict, candidates: list[str]) -> str | None:
    """Return the first candidate key that exists in props (case-insensitive)."""
    keys_lower = {k.lower(): k for k in props}
    for candidate in candidates:
        if candidate in props:
            return candidate
        if candidate.lower() in keys_lower:
            return keys_lower[candidate.lower()]
    return None


def _parse_hour(raw: object, local_offset: int) -> int | None:
    """Parse a timestamp string/int into a local hour."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Epoch ms
        try:
            ts = dt.datetime.fromtimestamp(float(raw) / 1000, tz=dt.UTC)
            return (ts.hour + local_offset) % 24
        except Exception:
            return None
    if isinstance(raw, str):
        # ISO 8601 or similar
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                ts = dt.datetime.strptime(raw[:19], fmt[: len(fmt)])
                # If no timezone info, assume UTC
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.UTC)
                return (ts.hour + local_offset) % 24
            except ValueError:
                continue
        # Try epoch ms as string
        if raw.isdigit() and len(raw) >= 10:
            try:
                ts = dt.datetime.fromtimestamp(int(raw) / 1000, tz=dt.UTC)
                return (ts.hour + local_offset) % 24
            except Exception:
                pass
    return None


# ── HTML scraping fallback ──────────────────────────────────────────────────


def _fetch_bfs_html_station(
    url: str, name: str, station_alt_m: float, timeout: float
) -> SourceFetch:
    t0 = time.monotonic()
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        latency_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()
        points = _parse_bfs_html(resp.text)
        if not points:
            return SourceFetch(
                name=name,
                ok=False,
                error="Could not parse UV data from BFS page",
                latency_ms=latency_ms,
                station_alt_m=station_alt_m,
            )
        return SourceFetch(
            name=name, ok=True, hourly=points, latency_ms=latency_ms, station_alt_m=station_alt_m
        )
    except httpx.TimeoutException:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(
            name=name,
            ok=False,
            error="HTML timeout",
            latency_ms=latency_ms,
            station_alt_m=station_alt_m,
        )
    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(
            name=name,
            ok=False,
            error=f"HTML error: {e}",
            latency_ms=latency_ms,
            station_alt_m=station_alt_m,
        )


def _parse_bfs_html(html: str) -> list[HourlyPoint]:
    soup = BeautifulSoup(html, "lxml")

    points = _try_parse_highcharts(soup)
    if points:
        return points

    points = _try_parse_table(soup)
    return points


def _try_parse_highcharts(soup: BeautifulSoup) -> list[HourlyPoint]:
    """Search script blocks for Highcharts [timestamp_ms, uv_value] pairs."""
    local_offset = 2 if _is_summer() else 1
    scripts = soup.find_all("script")
    for script in scripts:
        text = script.string or ""
        if not text:
            continue

        pairs = re.findall(r"\[(\d{10,13})\s*,\s*([\d.]+)\]", text)
        if len(pairs) < 3:
            continue

        points: list[HourlyPoint] = []
        for ts_str, uv_str in pairs:
            ts = dt.datetime.fromtimestamp(int(ts_str) / 1000, tz=dt.UTC)
            local_hour = (ts.hour + local_offset) % 24
            uv_val = float(uv_str)
            if 0.0 <= uv_val <= 20.0:
                points.append(
                    HourlyPoint(
                        hour=local_hour,
                        uv_index=uv_val,
                        uv_index_clear_sky=uv_val,
                        cloud_cover_pct=0.0,
                    )
                )
        if points:
            return sorted(points, key=lambda p: p.hour)

    return []


def _try_parse_table(soup: BeautifulSoup) -> list[HourlyPoint]:
    """Fallback: look for an HTML table with time and UV index columns."""
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        points: list[HourlyPoint] = []
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            time_text = cells[0].get_text(strip=True)
            uv_text = cells[1].get_text(strip=True)
            hour_match = re.match(r"^(\d{1,2})(?::\d{2})?$", time_text)
            if hour_match:
                try:
                    hour = int(hour_match.group(1))
                    uv_val = float(uv_text.replace(",", "."))
                    if 0 <= hour <= 23 and 0.0 <= uv_val <= 20.0:
                        points.append(
                            HourlyPoint(
                                hour=hour,
                                uv_index=uv_val,
                                uv_index_clear_sky=uv_val,
                                cloud_cover_pct=0.0,
                            )
                        )
                except ValueError:
                    continue
        if len(points) >= 4:
            return sorted(points, key=lambda p: p.hour)
    return []


def _is_summer() -> bool:
    now = dt.datetime.now(tz=dt.UTC)
    return 3 < now.month < 11
