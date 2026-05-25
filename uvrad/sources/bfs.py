"""BFS UV ground-station scrapers.

The German Federal Office for Radiation Protection (BFS) operates UV measurement
stations. We scrape two stations near Basel:

  Schauinsland: 1284m, ~60km SSW of Basel — primary
  Freiburg:      278m, ~60km ENE of Basel — backup

The pages embed Highcharts charts with series data in inline <script> blocks.
These are REAL ground measurements, but from different locations and altitudes,
so they are used only to compute a calibration offset against the model.
"""

import datetime as dt
import re
import time

import httpx
from bs4 import BeautifulSoup

from uvrad.models import HourlyPoint, SourceFetch

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

# Keep legacy name for import compatibility
BFS_URL = BFS_SCHAUINSLAND_URL


def fetch_bfs_schauinsland(timeout: float = 15.0) -> SourceFetch:
    return _fetch_bfs_station(BFS_SCHAUINSLAND_URL, "BFS Schauinsland", timeout)


def fetch_bfs_freiburg(timeout: float = 15.0) -> SourceFetch:
    return _fetch_bfs_station(BFS_FREIBURG_URL, "BFS Freiburg", timeout)


def _fetch_bfs_station(url: str, name: str, timeout: float) -> SourceFetch:
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
            )
        return SourceFetch(name=name, ok=True, hourly=points, latency_ms=latency_ms)
    except httpx.TimeoutException:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(name=name, ok=False, error="Timeout", latency_ms=latency_ms)
    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(name=name, ok=False, error=str(e), latency_ms=latency_ms)


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

        # Search for [epoch_ms, float] pairs anywhere in the script.
        # This is more robust than finding data:[...] wrappers, which break
        # on nested bracket patterns with simple character-class regex.
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
