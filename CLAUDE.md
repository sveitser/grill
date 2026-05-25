# grill — UV Radiation Index

Accurate UV index and same-day hourly forecast for Basel, Switzerland (and any location).
Fuses multiple data sources, corrects for altitude and cloud cover using solar geometry.

## Quick start

```bash
uv sync
uv run python scripts/current_uv.py                                        # Basel
uv run python scripts/current_uv.py --hourly                              # + full day table
uv run python scripts/current_uv.py --lat 47.37 --lon 8.54 --alt 408 --name Zurich
uv run python scripts/current_uv.py --no-bfs                              # skip BFS scraping
```

## Dev commands

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run ruff format --check . # format check (CI)
uv run ty check              # type check
uv run pytest tests/ -v      # tests
```

## CI

Four parallel GitHub Actions jobs in `.github/workflows/ci.yml`: `lint`, `format`, `typecheck`, `test`.

## Package layout

```
uvrad/                  # core UV pipeline package
  sources/
    open_meteo.py       # Open-Meteo REST API (ICON seamless + CAMS Europe)
    bfs.py              # BFS Schauinsland HTML scraper (calibration only)
  altitude.py           # WHO 10%/1000m altitude correction
  solar.py              # pvlib solar zenith + cosine-weighted interpolation
  fusion.py             # multi-source weighted fusion
  estimate.py           # top-level entry point
  models.py             # dataclasses: Location, HourlyPoint, UVEstimate, SourceFetch
  config.py             # defaults and constants (BASEL location, timeouts)
scripts/
  current_uv.py         # CLI script
tests/
  test_altitude.py
  test_fusion.py
  test_solar.py
```

## Data sources

| Source | Role | Auth |
|--------|------|------|
| Open-Meteo ICON seamless (MeteoSwiss) | Primary — 50% weight | None |
| Open-Meteo CAMS Europe (Copernicus) | Secondary — 50% weight | None |
| BFS Schauinsland (~60km SSW of Basel, 1284m) | Calibration offset only | None (scrape) |

Open-Meteo returns `uv_index` (cloud-corrected by the model) and `uv_index_clear_sky`.
Both are stored and displayed. Source weights renormalize automatically if a source fails.

BFS Schauinsland is used only for a calibration offset (signed difference between
altitude-corrected ground measurement and model output). It is never averaged into
the estimate directly — different location and microclimate.

## Key algorithms

**Altitude correction** (`uvrad/altitude.py`): WHO/ICNIRP standard 10%/1000m linear
approximation. Basel at 260m → factor 1.026. BFS Schauinsland at 1284m → factor 1.128.
Open-Meteo outputs are treated as sea-level equivalent; correction applied in fusion.

**Solar zenith interpolation** (`uvrad/solar.py`): uses `pvlib.solarposition` for exact
solar geometry. Interpolates between hourly forecast values using cosine(zenith) fraction
rather than clock fraction — physically correct because UV ∝ 1/air_mass ≈ cos(zenith).
Returns 0 when sun is below horizon (zenith ≥ 90°).

**Fusion** (`uvrad/fusion.py`): weighted average of ICON and CAMS for each hourly slot,
then altitude correction applied to the fused series. BFS offset computed separately.

## Primary location

Basel, Switzerland: lat=47.5596, lon=7.5886, alt=260m (city centre)
