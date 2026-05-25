"""Multi-source UV data fusion.

Fuses ICON-EU (best_match) and GFS seamless forecasts with a weighted average,
applies altitude correction, then blends in any available BFS ground measurement.

BFS ground stations (Schauinsland / Freiburg) provide real-time UV readings.
On clear days these are more reliable than model forecasts, so BFS gets a
cloud-scaled weight: BFS_MAX_WEIGHT × (1 − cloud_cover/100).  At 0% cloud
BFS contributes ~50% of the final value; it is excluded when fully overcast.

Altitude correction is applied per-source using each source's station altitude
(model outputs are sea-level equivalent; BFS stations are at their actual altitude).
"""

from dataclasses import dataclass, field

from uvrad.altitude import correct_uv
from uvrad.models import HourlyPoint, Location, SourceFetch, UVDataUnavailableError

# Relative weights for model sources (renormalized if a source fails)
SOURCE_WEIGHTS: dict[str, float] = {
    "Open-Meteo Global": 0.50,
    "Open-Meteo GFS": 0.50,
}

# BFS max weight at 0% cloud cover. Equals combined model weight so BFS
# contributes 50% of the final value on a perfectly clear day.
BFS_MAX_WEIGHT = 1.0


@dataclass
class FuseResult:
    hourly: list[HourlyPoint]
    sources_used: list[str]
    weights: dict[str, float]  # model source normalized weights (sum to 1.0)
    bfs_offset: float | None  # mean(bfs_corrected − model_only) over matched hours
    per_source_hourly: dict[str, list[HourlyPoint]] = field(default_factory=dict)
    bfs_hours_matched: int = 0
    bfs_hourly_weights: list[float] = field(default_factory=list)  # effective BFS weight per hour
    bfs_source_name: str | None = None  # name of BFS source if included in fusion


def fuse(
    location: Location,
    *model_fetches: SourceFetch,
    bfs_fetch: SourceFetch | None = None,
) -> FuseResult:
    """Fuse source data into a single altitude-corrected hourly series.

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

    # Normalize model weights to sum to 1.0
    total_w = sum(w for _, w in available)
    normalized: list[tuple[SourceFetch, float]] = [(f, w / total_w) for f, w in available]

    def _alt_correct_series(fetch: SourceFetch) -> dict[int, HourlyPoint]:
        """Build an hour-keyed lookup with altitude correction applied."""
        from_alt = fetch.station_alt_m if fetch.station_alt_m is not None else 0.0
        lookup: dict[int, HourlyPoint] = {}
        for p in fetch.hourly:
            uv_c = max(0.0, correct_uv(p.uv_index, from_alt_m=from_alt, to_alt_m=location.alt_m))
            uv_cs_c = max(
                0.0, correct_uv(p.uv_index_clear_sky, from_alt_m=from_alt, to_alt_m=location.alt_m)
            )
            lookup[p.hour] = HourlyPoint(
                hour=p.hour,
                uv_index=uv_c,
                uv_index_clear_sky=uv_cs_c,
                cloud_cover_pct=p.cloud_cover_pct,
            )
        return lookup

    _empty = HourlyPoint(hour=0, uv_index=0.0, uv_index_clear_sky=0.0, cloud_cover_pct=0.0)

    def _to_series(lookup: dict[int, HourlyPoint]) -> list[HourlyPoint]:
        return [
            lookup.get(
                h, HourlyPoint(hour=h, uv_index=0.0, uv_index_clear_sky=0.0, cloud_cover_pct=0.0)
            )
            for h in range(24)
        ]

    # Altitude-corrected lookups for model sources
    model_lookups = [(fetch.name, _alt_correct_series(fetch), w) for fetch, w in normalized]

    # Per-source altitude-corrected series (for computation trace)
    per_source_hourly: dict[str, list[HourlyPoint]] = {
        name: _to_series(lookup) for name, lookup, _ in model_lookups
    }

    # Altitude-corrected BFS lookup (None if unavailable)
    bfs_lookup: dict[int, HourlyPoint] | None = None
    if bfs_fetch and bfs_fetch.ok and bfs_fetch.hourly:
        bfs_lookup = _alt_correct_series(bfs_fetch)
        per_source_hourly[bfs_fetch.name] = _to_series(bfs_lookup)

    fused_hourly: list[HourlyPoint] = []
    model_only_hourly: list[HourlyPoint] = []  # without BFS, for calibration offset
    bfs_hourly_weights: list[float] = []

    for h in range(24):
        # Model weighted average
        uv_sum = 0.0
        uv_cs_sum = 0.0
        cloud_sum = 0.0
        w_total = 0.0

        for _name, lookup, w in model_lookups:
            if h in lookup:
                p = lookup[h]
                uv_sum += p.uv_index * w
                uv_cs_sum += p.uv_index_clear_sky * w
                cloud_sum += p.cloud_cover_pct * w
                w_total += w

        if w_total > 0:
            cloud_avg = cloud_sum / w_total
            model_uv = uv_sum / w_total
            model_uv_cs = uv_cs_sum / w_total
        else:
            cloud_avg = model_uv = model_uv_cs = 0.0

        model_only_hourly.append(
            HourlyPoint(
                hour=h,
                uv_index=max(0.0, model_uv),
                uv_index_clear_sky=max(0.0, model_uv_cs),
                cloud_cover_pct=cloud_avg,
            )
        )

        # Add BFS with cloud-scaled weight
        bfs_w = 0.0
        if bfs_lookup and h in bfs_lookup:
            bfs_p = bfs_lookup[h]
            bfs_w = BFS_MAX_WEIGHT * max(0.0, 1.0 - cloud_avg / 100.0)
            uv_sum += bfs_p.uv_index * bfs_w
            uv_cs_sum += bfs_p.uv_index_clear_sky * bfs_w
            w_total += bfs_w

        bfs_hourly_weights.append(bfs_w)

        if w_total > 0:
            fused_hourly.append(
                HourlyPoint(
                    hour=h,
                    uv_index=max(0.0, uv_sum / w_total),
                    uv_index_clear_sky=max(0.0, uv_cs_sum / w_total),
                    cloud_cover_pct=cloud_avg,
                )
            )
        else:
            fused_hourly.append(
                HourlyPoint(hour=h, uv_index=0.0, uv_index_clear_sky=0.0, cloud_cover_pct=0.0)
            )

    sources_used = [f.name for f, _ in normalized]
    weights = {f.name: w for f, w in normalized}

    bfs_source_name: str | None = None
    if bfs_lookup and bfs_fetch is not None:
        bfs_source_name = bfs_fetch.name
        sources_used.append(bfs_source_name)

    # Calibration offset: model-only vs BFS ground measurement
    bfs_offset: float | None = None
    bfs_hours_matched = 0
    if bfs_lookup and bfs_fetch:
        bfs_offset, bfs_hours_matched = _compute_bfs_offset(model_only_hourly, bfs_lookup)

    return FuseResult(
        hourly=fused_hourly,
        sources_used=sources_used,
        weights=weights,
        bfs_offset=bfs_offset,
        per_source_hourly=per_source_hourly,
        bfs_hours_matched=bfs_hours_matched,
        bfs_hourly_weights=bfs_hourly_weights,
        bfs_source_name=bfs_source_name,
    )


def _compute_bfs_offset(
    model_only: list[HourlyPoint],
    bfs_corrected: dict[int, HourlyPoint],
) -> tuple[float | None, int]:
    """Compute mean(bfs_at_target_alt − model_only) over hours with valid data.

    Positive offset means models underestimate relative to the BFS station.
    Uses already-altitude-corrected BFS values.
    """
    model_lookup = {p.hour: p for p in model_only}
    deltas: list[float] = []
    for hour, bfs_p in bfs_corrected.items():
        if bfs_p.uv_index <= 0.0:
            continue
        model_p = model_lookup.get(hour)
        if model_p is None or model_p.uv_index <= 0.0:
            continue
        deltas.append(bfs_p.uv_index - model_p.uv_index)
    if not deltas:
        return None, 0
    return sum(deltas) / len(deltas), len(deltas)
