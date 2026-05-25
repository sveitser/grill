"""BFS Schauinsland UV scraper.

The German Federal Office for Radiation Protection (BFS) operates a network
of 43 UV measurement stations. Schauinsland (1284m, Black Forest) is the
closest to Basel at ~60km SSW.

The page embeds a Highcharts chart whose series data is in a JSON-like
inline script block. We extract that rather than parsing the SVG.

These are REAL ground measurements — more trustworthy than model output
for detecting actual cloud/aerosol conditions, but from a different
location and altitude, so they are used only to compute a calibration
offset against the model, not averaged in directly.
"""

import re
import time

import httpx
from bs4 import BeautifulSoup

from uvrad.models import HourlyPoint, SourceFetch

BFS_URL = (
    "https://www.bfs.de/DE/themen/opt/uv/uv-index/aktuelle-tagesverlaeufe"
    "/_documents/schauinsland_node.html"
)
SCHAUINSLAND_ALT_M = 1284.0


def fetch_bfs_schauinsland(timeout: float = 15.0) -> SourceFetch:
    """Scrape current-day UV measurements from BFS Schauinsland station."""
    t0 = time.monotonic()
    try:
        resp = httpx.get(BFS_URL, timeout=timeout, follow_redirects=True)
        latency_ms = (time.monotonic() - t0) * 1000
        resp.raise_for_status()
        points = _parse_bfs_html(resp.text)
        if not points:
            return SourceFetch(
                name="BFS Schauinsland",
                ok=False,
                error="Could not parse UV data from BFS page",
                latency_ms=latency_ms,
            )
        return SourceFetch(
            name="BFS Schauinsland",
            ok=True,
            hourly=points,
            latency_ms=latency_ms,
        )
    except httpx.TimeoutException:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(name="BFS Schauinsland", ok=False, error="Timeout", latency_ms=latency_ms)
    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000
        return SourceFetch(name="BFS Schauinsland", ok=False, error=str(e), latency_ms=latency_ms)


def _parse_bfs_html(html: str) -> list[HourlyPoint]:
    """Extract hourly UV measurements from BFS page HTML.

    The page uses Highcharts with data embedded in inline <script> blocks.
    We look for the series data array pattern.
    """
    soup = BeautifulSoup(html, "lxml")

    # Strategy 1: find Highcharts series data in script tags
    points = _try_parse_highcharts(soup)
    if points:
        return points

    # Strategy 2: look for a data table
    points = _try_parse_table(soup)
    if points:
        return points

    return []


def _try_parse_highcharts(soup: BeautifulSoup) -> list[HourlyPoint]:
    """Look for Highcharts series data embedded in script blocks."""
    scripts = soup.find_all("script")
    for script in scripts:
        text = script.string or ""
        if not text:
            continue

        # Look for patterns like: data: [[timestamp, value], ...]
        # or data: [value, value, ...] with xAxis categories
        matches = re.findall(r"data\s*:\s*\[([^\]]+)\]", text)
        for match in matches:
            # Try to parse as [timestamp_ms, uv_value] pairs
            pairs = re.findall(r"\[(\d{10,13})\s*,\s*([\d.]+)\]", match)
            if len(pairs) >= 3:
                points = []
                for ts_str, uv_str in pairs:
                    ts_ms = int(ts_str)
                    # Convert epoch ms to hour (UTC+1 or UTC+2 for Basel/Schauinsland)
                    import datetime as dt

                    ts = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.UTC)
                    # Use local time (CET/CEST) — approximate as UTC+1 in winter, UTC+2 summer
                    # pvlib will handle exact DST, but for hour extraction ±1h is fine
                    local_offset = 2 if _is_summer() else 1
                    local_hour = (ts.hour + local_offset) % 24
                    uv_val = float(uv_str)
                    points.append(
                        HourlyPoint(
                            hour=local_hour,
                            uv_index=uv_val,
                            uv_index_clear_sky=uv_val,  # ground measurement; no clear-sky separate
                            cloud_cover_pct=0.0,
                        )
                    )
                if points:
                    return sorted(points, key=lambda p: p.hour)

            # Try simple array: [v1, v2, ...] — assumes hourly from midnight
            simple = re.findall(r"([\d.]+)", match)
            if len(simple) >= 6:
                points = []
                for i, v in enumerate(simple):
                    try:
                        uv_val = float(v)
                        if 0.0 <= uv_val <= 20.0:
                            points.append(
                                HourlyPoint(
                                    hour=i,
                                    uv_index=uv_val,
                                    uv_index_clear_sky=uv_val,
                                    cloud_cover_pct=0.0,
                                )
                            )
                    except ValueError:
                        continue
                if len(points) >= 6:
                    return points

    return []


def _try_parse_table(soup: BeautifulSoup) -> list[HourlyPoint]:
    """Fallback: look for an HTML table with time and UV index columns."""
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        points = []
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            time_text = cells[0].get_text(strip=True)
            uv_text = cells[1].get_text(strip=True)
            # Try to parse time like "14:00" or "14"
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
    """Rough check for CEST (UTC+2) vs CET (UTC+1)."""
    import datetime as dt

    now = dt.datetime.now(tz=dt.UTC)
    return 3 < now.month < 11
