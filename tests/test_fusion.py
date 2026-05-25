"""Tests for multi-source UV fusion."""

import pytest

from uvrad.fusion import fuse
from uvrad.models import HourlyPoint, Location, SourceFetch

BASEL = Location(lat=47.5596, lon=7.5886, alt_m=260.0, name="Basel")


def _make_fetch(name: str, uv_values: list[float], ok: bool = True) -> SourceFetch:
    points = [
        HourlyPoint(hour=h, uv_index=v, uv_index_clear_sky=v * 1.2, cloud_cover_pct=20.0)
        for h, v in enumerate(uv_values)
    ]
    return SourceFetch(name=name, ok=ok, hourly=points if ok else [])


def test_fuse_two_sources_equal_weights():
    icon = _make_fetch("Open-Meteo ICON", [0.0] * 6 + [2.0, 4.0, 6.0, 8.0, 7.0, 5.0] + [0.0] * 12)
    cams = _make_fetch("Open-Meteo CAMS", [0.0] * 6 + [2.0, 4.0, 7.0, 9.0, 8.0, 6.0] + [0.0] * 12)

    hourly, sources, weights, bfs_off = fuse(BASEL, icon, cams)

    assert len(hourly) == 24
    assert "Open-Meteo ICON" in sources
    assert "Open-Meteo CAMS" in sources
    assert bfs_off is None

    # Hour 9: ICON=8, CAMS=9, equal weights → avg=8.5 before altitude correction
    h9 = hourly[9]
    expected_uv = 8.5 * (1.0 + 260.0 / 1000.0 * 0.10)  # altitude corrected
    assert h9.uv_index == pytest.approx(expected_uv, rel=0.01)


def test_fuse_fallback_to_single_source():
    icon = _make_fetch("Open-Meteo ICON", [0.0] * 12 + [5.0] * 12)
    cams = _make_fetch("Open-Meteo CAMS", [], ok=False)

    hourly, sources, weights, _ = fuse(BASEL, icon, cams)

    assert sources == ["Open-Meteo ICON"]
    assert weights["Open-Meteo ICON"] == pytest.approx(1.0)
    # Altitude-corrected value
    expected = 5.0 * (1.0 + 260.0 / 1000.0 * 0.10)
    assert hourly[12].uv_index == pytest.approx(expected, rel=0.01)


def test_fuse_no_sources_returns_zeros():
    icon = _make_fetch("Open-Meteo ICON", [], ok=False)
    cams = _make_fetch("Open-Meteo CAMS", [], ok=False)

    hourly, sources, weights, _ = fuse(BASEL, icon, cams)

    assert sources == []
    assert all(p.uv_index == 0.0 for p in hourly)


def test_fuse_altitude_correction_applied():
    # Sea-level location vs Basel — Basel should have higher UV
    sea_level = Location(lat=47.5596, lon=7.5886, alt_m=0.0, name="Sea Level")
    icon = _make_fetch("Open-Meteo ICON", [5.0] * 24)
    cams = _make_fetch("Open-Meteo CAMS", [5.0] * 24)

    hourly_sl, _, _, _ = fuse(sea_level, icon, cams)
    hourly_bl, _, _, _ = fuse(BASEL, icon, cams)

    # Basel should have slightly higher UV due to altitude
    assert hourly_bl[12].uv_index > hourly_sl[12].uv_index


def test_fuse_bfs_offset_computed():
    icon = _make_fetch("Open-Meteo ICON", [0.0] * 6 + [4.0] * 12 + [0.0] * 6)
    cams = _make_fetch("Open-Meteo CAMS", [0.0] * 6 + [4.0] * 12 + [0.0] * 6)
    # BFS (at Schauinsland, 1284m) measures higher than model predicts
    bfs = _make_fetch("BFS Schauinsland", [0.0] * 6 + [6.0] * 12 + [0.0] * 6)

    _, _, _, bfs_offset = fuse(BASEL, icon, cams, bfs_fetch=bfs)

    # BFS offset should be positive (BFS > model after altitude correction)
    assert bfs_offset is not None
    assert bfs_offset != 0.0


def test_fuse_uv_never_negative():
    icon = _make_fetch("Open-Meteo ICON", [-1.0, 0.0, 5.0] + [0.0] * 21)
    cams = _make_fetch("Open-Meteo CAMS", [0.0] * 24)

    hourly, _, _, _ = fuse(BASEL, icon, cams)
    assert all(p.uv_index >= 0.0 for p in hourly)
