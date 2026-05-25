"""Top-level UV estimation entry point.

Orchestrates fetching from all sources, fusion, solar interpolation,
and returns a complete UVEstimate for a given location and time.
"""

from datetime import datetime

import pandas as pd

from uvrad.config import DEFAULT, Config
from uvrad.fusion import fuse
from uvrad.models import Location, UVEstimate
from uvrad.solar import interpolate_current_uv, solar_zenith_deg, solar_zenith_series
from uvrad.sources.bfs import fetch_bfs_schauinsland
from uvrad.sources.open_meteo import fetch_cams, fetch_icon


def get_uv_estimate(
    location: Location | None = None,
    config: Config = DEFAULT,
    include_bfs: bool = True,
) -> UVEstimate:
    """Fetch data from all sources and return a fused UV estimate.

    Args:
        location: target location; defaults to Basel if None
        config: configuration (timeouts, URLs, etc.)
        include_bfs: whether to attempt BFS scraping for calibration offset
    """
    loc: Location = location if location is not None else config.default_location

    # Fetch from model sources (these are the fast/reliable ones)
    icon_fetch = fetch_icon(loc, timeout=config.http_timeout)
    cams_fetch = fetch_cams(loc, timeout=config.http_timeout)

    # BFS scraping is slower and may fail — don't block on it
    bfs_fetch = None
    if include_bfs:
        bfs_fetch = fetch_bfs_schauinsland(timeout=config.bfs_timeout)

    # Fuse into a single hourly series
    hourly, sources_used, weights, bfs_offset = fuse(loc, icon_fetch, cams_fetch, bfs_fetch)

    # Current time in local timezone
    now = datetime.now(tz=pd.Timestamp.now(tz="Europe/Zurich").tz)

    # Solar zenith right now
    zenith = solar_zenith_deg(loc.lat, loc.lon, now)
    is_day = zenith < 90.0

    # Annotate hourly points with solar zenith
    day = now.date()
    zeniths = solar_zenith_series(loc.lat, loc.lon, day, tz="Europe/Zurich")
    for point, z in zip(hourly, zeniths, strict=False):
        point.solar_zenith_deg = z

    # Interpolate current UV
    current_uv = interpolate_current_uv(hourly, loc.lat, loc.lon, now)

    # Clear-sky current UV (same interpolation on clear-sky series)
    from uvrad.models import HourlyPoint

    cs_hourly = [
        HourlyPoint(
            hour=p.hour,
            uv_index=p.uv_index_clear_sky,
            uv_index_clear_sky=p.uv_index_clear_sky,
            cloud_cover_pct=p.cloud_cover_pct,
        )
        for p in hourly
    ]
    current_uv_cs = interpolate_current_uv(cs_hourly, loc.lat, loc.lon, now)

    return UVEstimate(
        location=loc,
        current_uv=round(current_uv, 2),
        current_uv_clear_sky=round(current_uv_cs, 2),
        hourly=hourly,
        solar_zenith_deg=round(zenith, 1),
        is_daytime=is_day,
        sources_used=sources_used,
        source_weights=weights,
        bfs_offset=round(bfs_offset, 2) if bfs_offset is not None else None,
        computed_at=now,
    )
