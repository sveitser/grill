"""Core data models."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SourceContribution:
    name: str
    weight: float
    uv_index: float  # altitude-corrected, at the interpolated hour bracket
    uv_index_clear_sky: float


@dataclass
class AltitudeCorrectionTrace:
    from_alt_m: float  # model outputs are sea-level equivalent (0 m)
    to_alt_m: float
    factor: float  # altitude_factor(to) / altitude_factor(from)


@dataclass
class InterpolationTrace:
    method: str  # "cosine_weighted" or "linear_fallback"
    prev_hour: int
    next_hour: int
    prev_uv: float
    next_uv: float
    cos_fraction: float  # fraction (0–1) applied toward next_hour


@dataclass
class BFSTrace:
    station: str
    station_alt_m: float
    hours_matched: int
    offset: float  # mean(bfs_at_target_alt − fused) over matched hours


@dataclass
class ComputationTrace:
    altitude_correction: AltitudeCorrectionTrace
    source_contributions: list[SourceContribution]
    interpolation: InterpolationTrace | None  # None when sun is below horizon
    bfs: BFSTrace | None


@dataclass
class Location:
    lat: float
    lon: float
    alt_m: float = 0.0
    name: str = ""


@dataclass
class HourlyPoint:
    hour: int  # 0-23 local time
    uv_index: float  # cloud-corrected, altitude-corrected
    uv_index_clear_sky: float  # altitude-corrected, no clouds
    cloud_cover_pct: float  # 0-100
    solar_zenith_deg: float = 90.0


@dataclass
class SourceFetch:
    name: str
    ok: bool
    hourly: list[HourlyPoint] = field(default_factory=list)
    tomorrow_hourly: list[HourlyPoint] = field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0
    station_alt_m: float | None = (
        None  # None = model output (sea-level equivalent); float = measured at this altitude
    )


@dataclass
class UVEstimate:
    location: Location
    current_uv: float  # interpolated, cloud+altitude corrected
    current_uv_clear_sky: float
    hourly: list[HourlyPoint]  # full day, altitude-corrected
    tomorrow_hourly: list[HourlyPoint]  # next day forecast, altitude-corrected
    solar_zenith_deg: float
    is_daytime: bool
    sources_used: list[str]
    source_weights: dict[str, float]
    bfs_offset: float | None  # positive = model underestimates vs BFS ground truth
    computed_at: datetime
    source_errors: dict[str, str] = field(default_factory=dict)
    computation: "ComputationTrace | None" = None


class UVDataUnavailableError(Exception):
    def __init__(self, failed_sources: "list[SourceFetch]"):
        parts = [f"{s.name}: {s.error or 'no data'}" for s in failed_sources]
        super().__init__("No UV data from any source. " + "; ".join(parts))
        self.failed_sources = failed_sources


UV_CATEGORIES = [
    (0, 2, "Low", "No protection needed"),
    (3, 5, "Moderate", "Sunscreen recommended (SPF 30+)"),
    (6, 7, "High", "Sunscreen + hat required"),
    (8, 10, "Very High", "Minimize midday exposure"),
    (11, 999, "Extreme", "Avoid outdoor exposure"),
]


def uv_category(uv_index: float) -> tuple[str, str]:
    """Return (category_name, advice) for a UV index value."""
    for lo, hi, name, advice in UV_CATEGORIES:
        if lo <= uv_index <= hi:
            return name, advice
    return "Extreme", "Avoid outdoor exposure"
