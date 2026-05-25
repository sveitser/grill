# grill — UV Radiation Index

Accurate UV index and same-day forecast for Basel, Switzerland (and any location).
Fuses multiple data sources, corrects for altitude and cloud cover.

## Quick start

```bash
uv sync
uv run python scripts/current_uv.py
# Optional: specify location
uv run python scripts/current_uv.py --lat 47.5596 --lon 7.5886 --alt 260 --name Basel
```

## Dev commands

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run ty check              # type check
uv run pytest tests/ -v      # tests
```

## Package layout

```
uv/                  # core UV pipeline package (name matches theme)
  sources/
    open_meteo.py    # Open-Meteo REST API (ICON seamless + CAMS Europe)
    bfs.py           # BFS Schauinsland HTML scraper (calibration only)
  altitude.py        # WHO 10%/1000m altitude correction
  solar.py           # pvlib solar zenith + cos-weighted interpolation
  fusion.py          # multi-source weighted fusion
  estimate.py        # top-level entry point
  models.py          # dataclasses
  config.py          # defaults and constants
scripts/
  current_uv.py      # CLI script
tests/               # pytest unit tests
```

## Data sources

| Source | Role | Auth |
|--------|------|------|
| Open-Meteo ICON seamless (MeteoSwiss) | Primary — 50% weight | None |
| Open-Meteo CAMS Europe | Secondary — 50% weight | None |
| BFS Schauinsland (~60km SSW, 1284m) | Calibration offset only | None (scrape) |

Open-Meteo returns `uv_index` (cloud-corrected by their model) and `uv_index_clear_sky`.
Both are stored and displayed.

## Altitude correction

UV increases ~10% per 1000m elevation (WHO/ICNIRP standard approximation).
Basel at 260m → factor 1.026 relative to sea level.
BFS Schauinsland at 1284m → factor 1.128. Readings are altitude-normalized before
computing calibration offset against model output.

## Primary location

Basel, Switzerland: lat=47.5596, lon=7.5886, alt=260m (city centre)
