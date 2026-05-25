# grill

UV radiation index for Basel, Switzerland — and anywhere else.

Fetches from multiple data sources, fuses them, corrects for your altitude and current cloud cover, and gives you the best available estimate of current UV index plus a full same-day hourly forecast.

## Usage

```bash
uv sync

# Current UV for Basel (default)
uv run python scripts/current_uv.py

# With full day table
uv run python scripts/current_uv.py --hourly

# Any location
uv run python scripts/current_uv.py --lat 47.37 --lon 8.54 --alt 408 --name Zurich

# Skip BFS Schauinsland scraping (faster, no calibration offset)
uv run python scripts/current_uv.py --no-bfs
```

Example output:

```
============================================================
  UV Index — Basel, Switzerland
  Altitude: 260m
  2026-05-25 13:45 CEST
============================================================

  Current UV index:  7.3  [High]
  Clear-sky (max):   9.1
  Solar zenith:      27.4°
    [████████████████████████░░░░░░░░░░░░░░░░] 7.3/12

  Advice: Sunscreen + hat required

  Sources: Open-Meteo ICON, Open-Meteo CAMS
    Open-Meteo ICON: 50%
    Open-Meteo CAMS: 50%

  BFS Schauinsland calibration offset: +0.4 UV units

  Hour     UV  Clear  Cloud   Zenith
  ------------------------------------
     7   0.5    0.8    45%    79.0°
     8   2.1    3.2    40%    69.2°
    ...
    13   7.3    9.1    20%    27.1°
    ...
    19   0.8    1.2    30%    70.3°
```

## How it works

**Data sources**

| Source | Role |
|--------|------|
| [Open-Meteo](https://open-meteo.com) ICON seamless (MeteoSwiss model) | Primary — 50% weight |
| [Open-Meteo](https://open-meteo.com) CAMS Europe (Copernicus) | Secondary — 50% weight |
| [BFS Schauinsland](https://www.bfs.de/DE/themen/opt/uv/uv-index/aktuelle-tagesverlaeufe/_documents/schauinsland_node.html) ground station | Calibration offset only |

Both Open-Meteo sources are free and require no API key. They return `uv_index` (already corrected for cloud cover by the atmospheric model) and `uv_index_clear_sky`.

**Altitude correction**

UV intensity increases roughly 10% per 1000m elevation (WHO/ICNIRP approximation). Basel at 260m gets a ×1.026 factor applied. You can specify any altitude with `--alt`.

**Sub-hourly interpolation**

Open-Meteo provides hourly values. To estimate the current UV, we interpolate between the two bracketing hours using the cosine of the solar zenith angle (via [pvlib](https://pvlib-python.readthedocs.io/)) rather than simple linear interpolation — UV follows `cos(zenith)`, not clock time, which matters near sunrise and sunset.

**BFS calibration offset**

The [BFS Schauinsland](https://www.bfs.de) station in the Black Forest (~60km SSW of Basel, 1284m elevation) provides real ground measurements. Because it's a different location and altitude, its readings aren't averaged into the estimate directly. Instead, the signed difference between altitude-corrected BFS measurements and the model output is reported as a calibration offset. A large positive offset indicates the model is underestimating UV — potentially due to a Saharan dust event, unusual aerosol load, or model bias on that day.

## Dev

```bash
uv run ruff check .        # lint
uv run ruff format .       # format
uv run ty check            # type check
uv run pytest tests/ -v    # tests
```

CI runs all four as separate parallel jobs on every push.

## Requirements

Python 3.11+, [uv](https://docs.astral.sh/uv/). All dependencies declared in `pyproject.toml` and pinned in `uv.lock`.
