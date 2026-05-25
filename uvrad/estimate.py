"""Top-level UV estimation entry point.

Orchestrates fetching from all sources, fusion, solar interpolation,
and returns a complete UVEstimate for a given location and time.
"""

from datetime import datetime

import pandas as pd

from uvrad.altitude import altitude_factor
from uvrad.config import DEFAULT, Config
from uvrad.fusion import fuse
from uvrad.models import (
    AltitudeCorrectionTrace,
    BFSTrace,
    ComputationTrace,
    Location,
    SourceContribution,
    UVEstimate,
)
from uvrad.solar import (
    interpolate_current_uv,
    interpolate_current_uv_traced,
    solar_zenith_deg,
    solar_zenith_series,
)
from uvrad.sources.bfs import SCHAUINSLAND_ALT_M, fetch_bfs_schauinsland
from uvrad.sources.open_meteo import fetch_gfs, fetch_icon


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

    # Fetch from model sources in parallel would be nicer, but sequential is simple and correct
    icon_fetch = fetch_icon(loc, timeout=config.http_timeout)
    gfs_fetch = fetch_gfs(loc, timeout=config.http_timeout)

    # BFS scraping is slower and may fail — don't block on it
    bfs_fetch = None
    if include_bfs:
        bfs_fetch = fetch_bfs_schauinsland(timeout=config.bfs_timeout)

    # Fuse into a single hourly series; raises UVDataUnavailableError if all sources fail
    fused = fuse(loc, icon_fetch, gfs_fetch, bfs_fetch=bfs_fetch)
    hourly = fused.hourly
    sources_used = fused.sources_used
    weights = fused.weights
    bfs_offset = fused.bfs_offset
    per_source_hourly = fused.per_source_hourly
    bfs_hours_matched = fused.bfs_hours_matched

    all_model_fetches = [icon_fetch, gfs_fetch]
    source_errors = {f.name: f.error for f in all_model_fetches if not f.ok and f.error}

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

    # Interpolate current UV (with trace for computation transparency)
    current_uv, interp_trace = interpolate_current_uv_traced(hourly, loc.lat, loc.lon, now)

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

    # Build altitude correction trace
    alt_factor = altitude_factor(loc.alt_m) / altitude_factor(0.0)
    alt_trace = AltitudeCorrectionTrace(
        from_alt_m=0.0,
        to_alt_m=loc.alt_m,
        factor=round(alt_factor, 4),
    )

    # Per-source contributions at the interpolation hour bracket
    bracket_hour = now.hour
    source_contribs = [
        SourceContribution(
            name=name,
            weight=round(weights[name], 4),
            uv_index=round(per_source_hourly[name][bracket_hour].uv_index, 3),
            uv_index_clear_sky=round(per_source_hourly[name][bracket_hour].uv_index_clear_sky, 3),
        )
        for name in sources_used
    ]

    bfs_trace: BFSTrace | None = None
    if bfs_offset is not None:
        bfs_trace = BFSTrace(
            station="Schauinsland",
            station_alt_m=SCHAUINSLAND_ALT_M,
            hours_matched=bfs_hours_matched,
            offset=round(bfs_offset, 3),
        )

    computation = ComputationTrace(
        altitude_correction=alt_trace,
        source_contributions=source_contribs,
        interpolation=interp_trace,
        bfs=bfs_trace,
    )

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
        source_errors=source_errors,
        computation=computation,
    )
