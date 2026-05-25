"""Top-level UV estimation entry point.

Orchestrates fetching from all sources, fusion, solar interpolation,
and returns a complete UVEstimate for a given location and time.
"""

from datetime import datetime, timedelta

import pandas as pd

from uvrad.altitude import altitude_factor
from uvrad.config import DEFAULT, Config
from uvrad.fusion import fuse
from uvrad.models import (
    AltitudeCorrectionTrace,
    BFSTrace,
    ComputationTrace,
    HourlyPoint,
    Location,
    SourceContribution,
    SourceFetch,
    UVEstimate,
    UVDataUnavailableError,
)
from uvrad.solar import (
    interpolate_current_uv,
    interpolate_current_uv_traced,
    solar_zenith_deg,
    solar_zenith_series,
)
from uvrad.sources.bfs import fetch_bfs_best
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
        include_bfs: whether to attempt BFS ground-station scraping
    """
    loc: Location = location if location is not None else config.default_location

    icon_fetch = fetch_icon(loc, timeout=config.http_timeout)
    gfs_fetch = fetch_gfs(loc, timeout=config.http_timeout)

    bfs_fetch = None
    if include_bfs:
        bfs_fetch = fetch_bfs_best(timeout=config.bfs_timeout)

    # Fuse; raises UVDataUnavailableError if all model sources fail
    fused = fuse(loc, icon_fetch, gfs_fetch, bfs_fetch=bfs_fetch)

    all_model_fetches = [icon_fetch, gfs_fetch]
    source_errors = {f.name: f.error for f in all_model_fetches if not f.ok and f.error}
    if bfs_fetch and not bfs_fetch.ok and bfs_fetch.error:
        source_errors[bfs_fetch.name] = bfs_fetch.error

    # Current time in local timezone
    now = datetime.now(tz=pd.Timestamp.now(tz="Europe/Zurich").tz)

    zenith = solar_zenith_deg(loc.lat, loc.lon, now)
    is_day = zenith < 90.0

    # Annotate hourly points with solar zenith
    day = now.date()
    zeniths = solar_zenith_series(loc.lat, loc.lon, day, tz="Europe/Zurich")
    for point, z in zip(fused.hourly, zeniths, strict=False):
        point.solar_zenith_deg = z

    # Tomorrow's forecast (model-only, no BFS — future data)
    tomorrow_hourly: list[HourlyPoint] = []
    icon_tmr = SourceFetch(
        name=icon_fetch.name,
        ok=icon_fetch.ok and bool(icon_fetch.tomorrow_hourly),
        hourly=icon_fetch.tomorrow_hourly,
    )
    gfs_tmr = SourceFetch(
        name=gfs_fetch.name,
        ok=gfs_fetch.ok and bool(gfs_fetch.tomorrow_hourly),
        hourly=gfs_fetch.tomorrow_hourly,
    )
    if icon_tmr.ok or gfs_tmr.ok:
        try:
            tomorrow_fused = fuse(loc, icon_tmr, gfs_tmr, bfs_fetch=None)
            tomorrow_date = day + timedelta(days=1)
            tomorrow_zeniths = solar_zenith_series(loc.lat, loc.lon, tomorrow_date, tz="Europe/Zurich")
            for point, z in zip(tomorrow_fused.hourly, tomorrow_zeniths, strict=False):
                point.solar_zenith_deg = z
            tomorrow_hourly = tomorrow_fused.hourly
        except UVDataUnavailableError:
            pass

    current_uv, interp_trace = interpolate_current_uv_traced(fused.hourly, loc.lat, loc.lon, now)

    cs_hourly = [
        HourlyPoint(
            hour=p.hour,
            uv_index=p.uv_index_clear_sky,
            uv_index_clear_sky=p.uv_index_clear_sky,
            cloud_cover_pct=p.cloud_cover_pct,
        )
        for p in fused.hourly
    ]
    current_uv_cs = interpolate_current_uv(cs_hourly, loc.lat, loc.lon, now)

    # Effective weights at the current hour (for computation trace and source_weights)
    bracket_hour = now.hour
    bfs_w = fused.bfs_hourly_weights[bracket_hour] if fused.bfs_hourly_weights else 0.0
    model_total = sum(fused.weights.values())  # = 1.0 by construction
    total_w = model_total + bfs_w

    def _eff_weight(name: str) -> float:
        if name == fused.bfs_source_name:
            return bfs_w / total_w if total_w > 0 else 0.0
        return fused.weights[name] / total_w if total_w > 0 else 0.0

    source_contribs = [
        SourceContribution(
            name=name,
            weight=round(_eff_weight(name), 4),
            uv_index=round(fused.per_source_hourly[name][bracket_hour].uv_index, 3),
            uv_index_clear_sky=round(
                fused.per_source_hourly[name][bracket_hour].uv_index_clear_sky, 3
            ),
        )
        for name in fused.sources_used
    ]

    alt_factor = altitude_factor(loc.alt_m) / altitude_factor(0.0)
    alt_trace = AltitudeCorrectionTrace(
        from_alt_m=0.0,
        to_alt_m=loc.alt_m,
        factor=round(alt_factor, 4),
    )

    bfs_trace: BFSTrace | None = None
    if fused.bfs_offset is not None and bfs_fetch is not None:
        bfs_trace = BFSTrace(
            station=fused.bfs_source_name or bfs_fetch.name,
            station_alt_m=bfs_fetch.station_alt_m or 0.0,
            hours_matched=fused.bfs_hours_matched,
            offset=round(fused.bfs_offset, 3),
        )

    computation = ComputationTrace(
        altitude_correction=alt_trace,
        source_contributions=source_contribs,
        interpolation=interp_trace,
        bfs=bfs_trace,
    )

    # source_weights: effective fractions at current hour (BFS varies by cloud cover)
    source_weights = {name: round(_eff_weight(name), 4) for name in fused.sources_used}

    return UVEstimate(
        location=loc,
        current_uv=round(current_uv, 2),
        current_uv_clear_sky=round(current_uv_cs, 2),
        hourly=fused.hourly,
        tomorrow_hourly=tomorrow_hourly,
        solar_zenith_deg=round(zenith, 1),
        is_daytime=is_day,
        sources_used=fused.sources_used,
        source_weights=source_weights,
        bfs_offset=round(fused.bfs_offset, 2) if fused.bfs_offset is not None else None,
        computed_at=now,
        source_errors=source_errors,
        computation=computation,
    )
