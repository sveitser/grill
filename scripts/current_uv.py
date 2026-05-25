"""CLI: print current UV index estimate for a location.

Usage:
    uv run python scripts/current_uv.py
    uv run python scripts/current_uv.py --lat 47.5596 --lon 7.5886 --alt 260
    uv run python scripts/current_uv.py --lat 47.37 --lon 8.54 --alt 408 --name Zurich
    uv run python scripts/current_uv.py --no-bfs
"""

import argparse
import sys

from uvrad.estimate import get_uv_estimate
from uvrad.models import Location, uv_category


def main() -> None:
    parser = argparse.ArgumentParser(description="UV radiation index estimator")
    parser.add_argument("--lat", type=float, help="Latitude (default: Basel)")
    parser.add_argument("--lon", type=float, help="Longitude (default: Basel)")
    parser.add_argument("--alt", type=float, default=None, help="Altitude in meters")
    parser.add_argument("--name", type=str, default="", help="Location name")
    parser.add_argument("--no-bfs", action="store_true", help="Skip BFS Schauinsland scraping")
    parser.add_argument("--hourly", action="store_true", help="Print full day hourly table")
    args = parser.parse_args()

    location = None
    if args.lat is not None and args.lon is not None:
        location = Location(
            lat=args.lat,
            lon=args.lon,
            alt_m=args.alt or 0.0,
            name=args.name,
        )
    elif args.alt is not None:
        # Override just the altitude for the default location
        from uvrad.config import DEFAULT

        loc = DEFAULT.default_location
        location = Location(lat=loc.lat, lon=loc.lon, alt_m=args.alt, name=loc.name)

    print("Fetching UV data...", file=sys.stderr)
    est = get_uv_estimate(location=location, include_bfs=not args.no_bfs)

    loc_name = est.location.name or f"{est.location.lat:.4f}°N {est.location.lon:.4f}°E"
    print(f"\n{'=' * 60}")
    print(f"  UV Index — {loc_name}")
    print(f"  Altitude: {est.location.alt_m:.0f}m")
    print(f"  {est.computed_at.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"{'=' * 60}")

    if not est.is_daytime:
        print("\n  Current UV:  0.0  (sun below horizon)")
        print(f"  Solar zenith: {est.solar_zenith_deg:.1f}°")
    else:
        cat, advice = uv_category(est.current_uv)
        bar = _uv_bar(est.current_uv)
        print(f"\n  Current UV index:  {est.current_uv:.1f}  [{cat}]")
        print(f"  Clear-sky (max):   {est.current_uv_clear_sky:.1f}")
        print(f"  Solar zenith:      {est.solar_zenith_deg:.1f}°")
        print(f"  {bar}")
        print(f"\n  Advice: {advice}")

    print(f"\n  Sources: {', '.join(est.sources_used) or 'none'}")
    if est.source_weights:
        for src, w in est.source_weights.items():
            print(f"    {src}: {w:.0%}")
    if est.bfs_offset is not None:
        sign = "+" if est.bfs_offset >= 0 else ""
        print(f"\n  BFS Schauinsland calibration offset: {sign}{est.bfs_offset:.2f} UV units")
        if abs(est.bfs_offset) > 1.0:
            print(f"  (model may be {'under' if est.bfs_offset > 0 else 'over'}estimating)")
    else:
        print("\n  BFS Schauinsland: unavailable")

    if args.hourly:
        print(f"\n  {'Hour':>4}  {'UV':>5}  {'Clear':>5}  {'Cloud':>5}  {'Zenith':>7}")
        print(f"  {'-' * 36}")
        for p in est.hourly:
            if p.solar_zenith_deg < 90.0 or p.uv_index > 0:
                print(
                    f"  {p.hour:>4}  {p.uv_index:>5.1f}  "
                    f"{p.uv_index_clear_sky:>5.1f}  "
                    f"{p.cloud_cover_pct:>4.0f}%  "
                    f"{p.solar_zenith_deg:>6.1f}°"
                )

    print()


def _uv_bar(uv: float, width: int = 40) -> str:
    max_uv = 12.0
    filled = min(int((uv / max_uv) * width), width)
    bar = "█" * filled + "░" * (width - filled)
    return f"  [{bar}] {uv:.1f}/12"


if __name__ == "__main__":
    main()
