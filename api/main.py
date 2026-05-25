"""FastAPI application for UV radiation index.

Single endpoint: GET /uv
Returns current UV + full-day hourly forecast for Basel (or any location).
In-memory TTL cache avoids hammering Open-Meteo on every request.
"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from uvrad.config import DEFAULT
from uvrad.estimate import get_uv_estimate
from uvrad.models import Location, UVDataUnavailableError, uv_category

_cache: dict[str, tuple[float, dict]] = {}  # key → (expires_at, payload)
CACHE_TTL = 300  # 5 minutes


def _cache_key(lat: float, lon: float, alt: float) -> str:
    return f"{lat:.4f}:{lon:.4f}:{alt:.0f}"


def _cached(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry and time.monotonic() < entry[0]:
        return entry[1]
    return None


def _store(key: str, payload: dict) -> None:
    _cache[key] = (time.monotonic() + CACHE_TTL, payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _cache.clear()


app = FastAPI(
    title="UV Radiation Index",
    description="Multi-source UV index for Basel, Switzerland (and any location).",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/uv")
def get_uv(
    lat: float = Query(default=None, description="Latitude"),
    lon: float = Query(default=None, description="Longitude"),
    alt: float = Query(default=None, description="Altitude in metres"),
    bfs: bool = Query(default=True, description="Include BFS Schauinsland calibration"),
) -> JSONResponse:
    """Current UV index and same-day hourly forecast.

    Defaults to Basel, Switzerland (lat=47.5596, lon=7.5886, alt=260m).
    Source weights renormalize automatically if a model source is unavailable.
    """
    loc = DEFAULT.default_location
    if lat is not None and lon is not None:
        loc = Location(lat=lat, lon=lon, alt_m=alt or 0.0)
    elif alt is not None:
        loc = Location(lat=loc.lat, lon=loc.lon, alt_m=alt, name=loc.name)

    key = _cache_key(loc.lat, loc.lon, loc.alt_m)
    cached = _cached(key)
    if cached:
        return JSONResponse(content=cached, headers={"X-Cache": "HIT"})

    try:
        est = get_uv_estimate(location=loc, include_bfs=bfs)
    except UVDataUnavailableError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": str(exc),
                "source_errors": {s.name: s.error for s in exc.failed_sources},
            },
        )
    cat, advice = uv_category(est.current_uv)

    payload = {
        "location": {
            "lat": est.location.lat,
            "lon": est.location.lon,
            "alt_m": est.location.alt_m,
            "name": est.location.name,
        },
        "current": {
            "uv_index": est.current_uv,
            "uv_index_clear_sky": est.current_uv_clear_sky,
            "category": cat,
            "advice": advice,
            "solar_zenith_deg": est.solar_zenith_deg,
            "is_daytime": est.is_daytime,
        },
        "hourly": [
            {
                "hour": p.hour,
                "uv_index": p.uv_index,
                "uv_index_clear_sky": p.uv_index_clear_sky,
                "cloud_cover_pct": p.cloud_cover_pct,
                "solar_zenith_deg": p.solar_zenith_deg,
            }
            for p in est.hourly
        ],
        "sources_used": est.sources_used,
        "source_weights": est.source_weights,
        "source_errors": est.source_errors,
        "bfs_offset": est.bfs_offset,
        "computed_at": est.computed_at.isoformat(),
    }

    _store(key, payload)
    return JSONResponse(content=payload, headers={"X-Cache": "MISS"})
