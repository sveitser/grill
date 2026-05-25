"""Multi-source UV data fusion.

Fuses ICON and CAMS model forecasts with a weighted average, applies
altitude correction, then computes a BFS calibration offset.

Why BFS is calibration-only (not averaged in):
  Schauinsland is 60km away at 1284m. Its absolute UV values reflect a
  different location and microclimate. However, systematic deviations
  between BFS measurements and model predictions reveal model bias:
  aerosol events, Saharan dust, unusual cloud patterns. The offset is
  useful metadata but is not automatically applied (that would require
  assuming Basel and Schauinsland have the same conditions).
"""

from uvrad.altitude import correct_uv
from uvrad.models import HourlyPoint, Location, SourceFetch, UVDataUnavailableError
from uvrad.sources.bfs import SCHAUINSLAND_ALT_M

# Relative weights for model sources (renormalized if a source fails)
SOURCE_WEIGHTS: dict[str, float] = {
    "Open-Meteo Global": 0.50,
    "Open-Meteo ECMWF": 0.50,
    "MeteoSwiss ICON-CH2": 0.50,
}


def fuse(
    location: Location,
    *model_fetches: SourceFetch,
    bfs_fetch: SourceFetch | None = None,
) -> tuple[list[HourlyPoint], list[str], dict[str, float], float | None]:
    """Fuse source data into a single altitude-corrected hourly series.

    Returns:
        hourly: 24-element list of fused HourlyPoints (altitude-corrected)
        sources_used: names of sources that contributed
        weights: effective weight per source
        bfs_offset: mean(bfs_corrected - fused) over available hours, or None

    Raises:
        UVDataUnavailableError: if no model source returned usable UV data
    """
    available: list[tuple[SourceFetch, float]] = []
    for fetch in model_fetches:
        if fetch.ok and fetch.hourly:
            w = SOURCE_WEIGHTS.get(fetch.name, 0.5)
            available.append((fetch, w))

    if not available:
        raise UVDataUnavailableError(list(model_fetches))

    # Normalize weights
    total_w = sum(w for _, w in available)
    normalized: list[tuple[SourceFetch, float]] = [(f, w / total_w) for f, w in available]

    # Build hour → point lookup for each source
    def to_lookup(fetch: SourceFetch) -> dict[int, HourlyPoint]:
        return {p.hour: p for p in fetch.hourly}

    lookups = [(to_lookup(f), w) for f, w in normalized]

    fused_hourly: list[HourlyPoint] = []
    for h in range(24):
        uv_sum = 0.0
        uv_cs_sum = 0.0
        cloud_sum = 0.0
        w_total = 0.0

        for lookup, w in lookups:
            if h in lookup:
                p = lookup[h]
                uv_sum += p.uv_index * w
                uv_cs_sum += p.uv_index_clear_sky * w
                cloud_sum += p.cloud_cover_pct * w
                w_total += w

        if w_total > 0:
            uv = uv_sum / w_total
            uv_cs = uv_cs_sum / w_total
            cloud = cloud_sum / w_total
        else:
            uv = uv_cs = cloud = 0.0

        # Apply altitude correction: Open-Meteo outputs are ~sea-level equivalent;
        # correct to the target location's altitude.
        uv_corrected = correct_uv(uv, from_alt_m=0.0, to_alt_m=location.alt_m)
        uv_cs_corrected = correct_uv(uv_cs, from_alt_m=0.0, to_alt_m=location.alt_m)

        fused_hourly.append(
            HourlyPoint(
                hour=h,
                uv_index=max(0.0, uv_corrected),
                uv_index_clear_sky=max(0.0, uv_cs_corrected),
                cloud_cover_pct=cloud,
            )
        )

    sources_used = [f.name for f, _ in normalized]
    weights = {f.name: w for f, w in normalized}

    # BFS calibration offset
    bfs_offset: float | None = None
    if bfs_fetch and bfs_fetch.ok and bfs_fetch.hourly:
        bfs_offset = _compute_bfs_offset(fused_hourly, bfs_fetch, location.alt_m)

    return fused_hourly, sources_used, weights, bfs_offset


def _compute_bfs_offset(
    fused: list[HourlyPoint],
    bfs_fetch: SourceFetch,
    target_alt_m: float,
) -> float | None:
    """Compute mean(bfs_at_target_alt - fused) over hours with valid BFS measurements.

    A positive offset means the model is underestimating UV relative to
    what a BFS-like station would measure at the target altitude.
    """
    fused_lookup = {p.hour: p for p in fused}
    bfs_lookup = {p.hour: p for p in bfs_fetch.hourly}

    deltas: list[float] = []
    for hour, bfs_point in bfs_lookup.items():
        if bfs_point.uv_index <= 0.0:
            continue
        if hour not in fused_lookup:
            continue
        fused_uv = fused_lookup[hour].uv_index
        if fused_uv <= 0.0:
            continue
        # Convert BFS Schauinsland measurement to target altitude
        bfs_at_target = correct_uv(
            bfs_point.uv_index,
            from_alt_m=SCHAUINSLAND_ALT_M,
            to_alt_m=target_alt_m,
        )
        deltas.append(bfs_at_target - fused_uv)

    if not deltas:
        return None
    return sum(deltas) / len(deltas)
