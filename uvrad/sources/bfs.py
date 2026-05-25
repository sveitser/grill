"""BFS UV ground-station data via IMIS WFS API.

The German Federal Office for Radiation Protection (BFS) operates UV measurement
stations. We query the nearest station to Basel via the IMIS WFS API:

  Schauinsland: station_id="SL", ~30km SSE of Basel, 1206m altitude

Station discovery:
  1. Query opendata:uv_index_tagesverlauf to get all 35 stations with metadata
  2. Find station named "Schauinsland" (or fall back to nearest to Basel)
  3. Use its short station_id to query opendata:abi_30min for UV readings

opendata:abi_30min contains current 30-minute UV readings during daylight hours.
At night it returns 0 features — this is correct (no UV when sun is down).

Falls back to HTML scraping on the old bfs.de Schauinsland page if WFS fails.
"""

import datetime as dt
import logging
import math
import re
import time

import httpx
from bs4 import BeautifulSoup

from uvrad.models import HourlyPoint, SourceFetch

logger = logging.getLogger(__name__)

WFS_BASE_URL = "https://www.imis.bfs.de/ogc/opendata/ows"
WFS_UV_LAYER = "opendata:abi_30min"
WFS_STATION_LAYER = "opendata:uv_index_tagesverlauf"

# Basel coordinates for nearest-station fallback
_BASEL_LAT = 47.5596
_BASEL_LON = 7.5886

# Known defaults (used when WFS station discovery fails)
_DEFAULT_STATION_ID = "SL"
_DEFAULT_STATION_NAME = "Schauinsland"
_DEFAULT_STATION_ALT_M = 1206.0

BFS_SCHAUINSLAND_URL = (
    "https://www.bfs.de/DE/themen/opt/uv/uv-index/aktuelle-tagesverlaeufe"
    "/_documents/schauinsland_node.html"
)

# Keep legacy constants for import compatibility
SCHAUINSLAND_ALT_M = _DEFAULT_STATION_ALT_M
FREIBURG_ALT_M = 278.0
SCHAUINSLAND_STATION_ID = _DEFAULT_STATION_ID
FREIBURG_STATION_ID = "FREI"
BFS_URL = BFS_SCHAUINSLAND_URL

# Candidate property names in abi_30min features (tried in order)
_UV_PROP_CANDIDATES = ["uv_index", "uvi", "value", "messwert", "uv", "UV_Index", "UV"]
_TIME_PROP_CANDIDATES = [
    "end_measure",
    "time",
    "zeitstempel",
    "datetime",
    "timestamp",
    "datum",
    "date",
    "start_measure",
]
_STATION_PROP_CANDIDATES = ["station_id", "station", "kenn", "standort_id", "id"]


def fetch_bfs_schauinsland(timeout: float = 15.0) -> SourceFetch:
    meta = _discover_station(name_hint="Schauinsland", timeout=timeout)
    return _fetch_station(meta, fallback_name="BFS Schauinsland", timeout=timeout)


def fetch_bfs_freiburg(timeout: float = 15.0) -> SourceFetch:
    # No Freiburg station in the BFS network — use nearest station to Basel
    meta = _discover_nearest_station(timeout=timeout)
    return _fetch_station(meta, fallback_name="BFS Schauinsland", timeout=timeout)


def fetch_bfs_best(timeout: float = 15.0) -> SourceFetch:
    """Fetch UV from the nearest BFS station to Basel (Schauinsland)."""
    meta = _discover_station(name_hint="Schauinsland", timeout=timeout)
    if meta is None:
        meta = _discover_nearest_station(timeout=timeout)
    return _fetch_station(meta, fallback_name="BFS Schauinsland", timeout=timeout)


def _fetch_station(meta: dict | None, fallback_name: str, timeout: float) -> SourceFetch:
    """Fetch UV for a station described by WFS metadata dict."""
    if meta:
        station_id = meta["station_id"]
        station_name = meta.get("station_name", fallback_name)
        alt_m = float(meta.get("hoehe") or _DEFAULT_STATION_ALT_M)
        display_name = f"BFS {station_name}"
    else:
        station_id = _DEFAULT_STATION_ID
        alt_m = _DEFAULT_STATION_ALT_M
        display_name = fallback_name

    result = _fetch_bfs_wfs(station_id, display_name, alt_m, timeout)
    if result.ok:
        return result

    # HTML fallback (Schauinsland only)
    html = _fetch_bfs_html_station(BFS_SCHAUINSLAND_URL, display_name, alt_m, timeout)
    if html.ok:
        return html

    return SourceFetch(
        name=display_name,
        ok=False,
        error=f"WFS: {result.error}; HTML: {html.error}",
        latency_ms=result.latency_ms + html.latency_ms,
        station_alt_m=alt_m,
    )


def _discover_station(name_hint: str, timeout: float) -> dict | None:
    """Query uv_index_tagesverlauf and return properties for station matching name_hint."""
    try:
        resp = httpx.get(
            WFS_BASE_URL,
            params={
                "service": "WFS",
                "version": "1.1.0",
                "request": "GetFeature",
                "typeName": WFS_STATION_LAYER,
                "outputFormat": "application/json",
                "maxFeatures": "100",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        hint_lower = name_hint.lower()
        for feat in features:
            props = feat.get("properties", {})
            if hint_lower in props.get("station_name", "").lower():
                logger.debug(
                    "Discovered station: %s id=%s alt=%sm",
                    props.get("station_name"),
                    props.get("station_id"),
                    props.get("hoehe"),
                )
                return props
    except Exception as e:
        logger.warning("Station discovery failed: %s", e)
    return None


def _discover_nearest_station(timeout: float) -> dict | None:
    """Return the WFS station metadata for the station nearest to Basel."""
    try:
        resp = httpx.get(
            WFS_BASE_URL,
            params={
                "service": "WFS",
                "version": "1.1.0",
                "request": "GetFeature",
                "typeName": WFS_STATION_LAYER,
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
        logger.warning("Nearest-station discovery failed: %s", e)
    return None


def _fetch_bfs_wfs(station_id: str, name: str, station_alt_m: float, timeout: float) -> SourceFetch:
    """Fetch UV readings from opendata:abi_30min for the given station_id.

    Tries two variants:
      1. CQL_FILTER=station_id='{station_id}' (server-side)
      2. No filter, maxFeatures=200, filter client-side
         (fallback if the server rejects the CQL property name)
    """
    t0 = time.monotonic()
    attempts = [
        {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": WFS_UV_LAYER,
            "outputFormat": "application/json",
            "CQL_FILTER": f"station_id='{station_id}'",
            "maxFeatures": "60",
        },
        {
            "service": "WFS",
            "version": "1.1.0",
            "request": "GetFeature",
            "typeName": WFS_UV_LAYER,
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
                last_error = f"non-JSON: {body[:120]!r}"
                continue

            features = data.get("features", [])

            if not features:
                # No data — likely nighttime (correct behavior), not an error worth retrying
                last_error = "no UV readings (nighttime or station offline)"
                logger.debug("WFS %s attempt %d: 0 features (nighttime?)", station_id, attempt_idx)
                break  # Don't bother with second attempt — it'll also be empty

            # On no-filter attempt, narrow client-side
            if attempt_idx == 1:
                all_features = features
                features = _filter_features_by_station(features, station_id)
                if not features:
                    all_ids = _collect_station_ids(all_features)
                    logger.warning(
                        "WFS no features for %s; station IDs present: %s", station_id, all_ids[:20]
                    )
                    last_error = f"no match for {station_id!r}; seen: {all_ids[:5]}"
                    continue

            sample_props = features[0].get("properties", {})
            logger.debug(
                "WFS %s properties: %s | sample: %s",
                station_id,
                list(sample_props.keys()),
                {k: v for k, v in list(sample_props.items())[:6]},
            )

            points = _parse_wfs_features(features)
            if not points:
                last_error = f"could not parse UV from props {list(sample_props.keys())}"
                logger.warning("WFS %s: %s", station_id, last_error)
                continue

            return SourceFetch(
                name=name,
                ok=True,
                hourly=points,
                latency_ms=latency_ms,
                station_alt_m=station_alt_m,
            )

        except httpx.TimeoutException:
            return SourceFetch(
                name=name,
                ok=False,
                error="WFS timeout",
                latency_ms=(time.monotonic() - t0) * 1000,
                station_alt_m=station_alt_m,
            )
        except Exception as e:
            logger.exception("WFS fetch failed for %s (attempt %d)", station_id, attempt_idx)
            last_error = f"exception: {e}"
            continue

    return SourceFetch(
        name=name,
        ok=False,
        error=last_error,
        latency_ms=(time.monotonic() - t0) * 1000,
        station_alt_m=station_alt_m,
    )


def _parse_wfs_features(features: list[dict]) -> list[HourlyPoint]:
    """Parse abi_30min GeoJSON features into hourly UV points (mean per hour)."""
    local_offset = 2 if _is_summer() else 1

    if not features:
        return []

    sample = features[0].get("properties", {})
    uv_prop = _detect_prop(sample, _UV_PROP_CANDIDATES)
    time_prop = _detect_prop(sample, _TIME_PROP_CANDIDATES)

    if uv_prop is None:
        logger.warning("Cannot find UV property in WFS response. Keys: %s", list(sample.keys()))
        return []

    logger.debug("WFS parsing: uv_prop=%s time_prop=%s", uv_prop, time_prop)

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
            local_hour = _parse_hour(props.get(time_prop), local_offset)
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

    return sorted(
        [
            HourlyPoint(
                hour=h,
                uv_index=sum(vals) / len(vals),
                uv_index_clear_sky=sum(vals) / len(vals),
                cloud_cover_pct=0.0,
            )
            for h, vals in hourly_readings.items()
        ],
        key=lambda p: p.hour,
    )


def _detect_prop(props: dict, candidates: list[str]) -> str | None:
    keys_lower = {k.lower(): k for k in props}
    for c in candidates:
        if c in props:
            return c
        if c.lower() in keys_lower:
            return keys_lower[c.lower()]
    return None


def _parse_hour(raw: object, local_offset: int) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            ts = dt.datetime.fromtimestamp(float(raw) / 1000, tz=dt.UTC)
            return (ts.hour + local_offset) % 24
        except Exception:
            return None
    if isinstance(raw, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                ts = dt.datetime.strptime(raw[:19], fmt[: len(fmt)])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.UTC)
                return (ts.hour + local_offset) % 24
            except ValueError:
                continue
        if raw.isdigit() and len(raw) >= 10:
            try:
                ts = dt.datetime.fromtimestamp(int(raw) / 1000, tz=dt.UTC)
                return (ts.hour + local_offset) % 24
            except Exception:
                pass
    return None


def _filter_features_by_station(features: list[dict], station_id: str) -> list[dict]:
    sid_lower = station_id.lower()
    for prop in _STATION_PROP_CANDIDATES:
        matched = [
            f for f in features if str(f.get("properties", {}).get(prop, "")).lower() == sid_lower
        ]
        if matched:
            return matched
    for prop in _STATION_PROP_CANDIDATES:
        matched = [
            f for f in features if sid_lower in str(f.get("properties", {}).get(prop, "")).lower()
        ]
        if matched:
            return matched
    return []


def _collect_station_ids(features: list[dict]) -> list[str]:
    seen: set[str] = set()
    for feat in features:
        props = feat.get("properties", {})
        for prop in _STATION_PROP_CANDIDATES:
            val = props.get(prop)
            if val is not None:
                seen.add(str(val))
    return sorted(seen)


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
    return _try_parse_table(soup)


def _try_parse_highcharts(soup: BeautifulSoup) -> list[HourlyPoint]:
    local_offset = 2 if _is_summer() else 1
    for script in soup.find_all("script"):
        text = script.string or ""
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
    for table in soup.find_all("table"):
        points: list[HourlyPoint] = []
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            hour_match = re.match(r"^(\d{1,2})(?::\d{2})?$", cells[0].get_text(strip=True))
            if hour_match:
                try:
                    hour = int(hour_match.group(1))
                    uv_val = float(cells[1].get_text(strip=True).replace(",", "."))
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
    return 3 < dt.datetime.now(tz=dt.UTC).month < 11
