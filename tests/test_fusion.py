"""Tests for multi-source UV fusion."""

import pytest

from uvrad.fusion import BFS_MAX_WEIGHT, FuseResult, fuse
from uvrad.models import HourlyPoint, Location, SourceFetch, UVDataUnavailableError

BASEL = Location(lat=47.5596, lon=7.5886, alt_m=260.0, name="Basel")


def _make_fetch(
    name: str, uv_values: list[float], ok: bool = True, station_alt_m: float | None = None
) -> SourceFetch:
    points = [
        HourlyPoint(hour=h, uv_index=v, uv_index_clear_sky=v * 1.2, cloud_cover_pct=20.0)
        for h, v in enumerate(uv_values)
    ]
    return SourceFetch(name=name, ok=ok, hourly=points if ok else [], station_alt_m=station_alt_m)


def test_fuse_returns_fuse_result():
    icon = _make_fetch("Open-Meteo Global", [0.0] * 24)
    cams = _make_fetch("Open-Meteo ECMWF", [0.0] * 24)
    result = fuse(BASEL, icon, cams)
    assert isinstance(result, FuseResult)


def test_fuse_two_sources_equal_weights():
    icon = _make_fetch("Open-Meteo Global", [0.0] * 6 + [2.0, 4.0, 6.0, 8.0, 7.0, 5.0] + [0.0] * 12)
    cams = _make_fetch("Open-Meteo ECMWF", [0.0] * 6 + [2.0, 4.0, 7.0, 9.0, 8.0, 6.0] + [0.0] * 12)

    r = fuse(BASEL, icon, cams)

    assert len(r.hourly) == 24
    assert "Open-Meteo Global" in r.sources_used
    assert "Open-Meteo ECMWF" in r.sources_used
    assert r.bfs_offset is None
    assert r.bfs_hours_matched == 0
    assert r.bfs_source_name is None

    # Hour 9: Global=8, ECMWF=9, equal weights → avg=8.5 before altitude correction
    expected_uv = 8.5 * (1.0 + 260.0 / 1000.0 * 0.10)
    assert r.hourly[9].uv_index == pytest.approx(expected_uv, rel=0.01)

    # per_source_hourly should contain both sources with 24-element lists
    assert "Open-Meteo Global" in r.per_source_hourly
    assert "Open-Meteo ECMWF" in r.per_source_hourly
    assert len(r.per_source_hourly["Open-Meteo Global"]) == 24


def test_fuse_fallback_to_single_source():
    icon = _make_fetch("Open-Meteo Global", [0.0] * 12 + [5.0] * 12)
    cams = _make_fetch("Open-Meteo ECMWF", [], ok=False)

    r = fuse(BASEL, icon, cams)

    assert r.sources_used == ["Open-Meteo Global"]
    assert r.weights["Open-Meteo Global"] == pytest.approx(1.0)
    expected = 5.0 * (1.0 + 260.0 / 1000.0 * 0.10)
    assert r.hourly[12].uv_index == pytest.approx(expected, rel=0.01)


def test_fuse_no_sources_raises():
    icon = _make_fetch("Open-Meteo Global", [], ok=False)
    cams = _make_fetch("Open-Meteo ECMWF", [], ok=False)

    with pytest.raises(UVDataUnavailableError):
        fuse(BASEL, icon, cams)


def test_fuse_no_sources_error_includes_source_info():
    icon = _make_fetch("Open-Meteo Global", [], ok=False)
    icon.error = "Timeout"
    cams = _make_fetch("Open-Meteo ECMWF", [], ok=False)
    cams.error = "No UV data in response"

    with pytest.raises(UVDataUnavailableError) as exc_info:
        fuse(BASEL, icon, cams)

    assert "Open-Meteo Global" in str(exc_info.value)
    assert "Open-Meteo ECMWF" in str(exc_info.value)
    assert len(exc_info.value.failed_sources) == 2


def test_fuse_altitude_correction_applied():
    sea_level = Location(lat=47.5596, lon=7.5886, alt_m=0.0, name="Sea Level")
    icon = _make_fetch("Open-Meteo Global", [5.0] * 24)
    cams = _make_fetch("Open-Meteo ECMWF", [5.0] * 24)

    r_sl = fuse(sea_level, icon, cams)
    r_bl = fuse(BASEL, icon, cams)

    assert r_bl.hourly[12].uv_index > r_sl.hourly[12].uv_index


def test_fuse_bfs_included_in_fusion():
    """BFS ground measurement should be included in the fused result when available."""
    icon = _make_fetch("Open-Meteo Global", [0.0] * 6 + [4.0] * 12 + [0.0] * 6)
    cams = _make_fetch("Open-Meteo GFS", [0.0] * 6 + [4.0] * 12 + [0.0] * 6)
    # BFS measured at 0m so altitude correction is trivial; cloud=0 so max weight applies
    bfs_points = [
        HourlyPoint(
            hour=h,
            uv_index=0.0 if h < 6 or h >= 18 else 6.0,
            uv_index_clear_sky=0.0 if h < 6 or h >= 18 else 6.0,
            cloud_cover_pct=0.0,
        )
        for h in range(24)
    ]
    bfs = SourceFetch(name="BFS Schauinsland", ok=True, hourly=bfs_points, station_alt_m=0.0)

    r_no_bfs = fuse(BASEL, icon, cams)
    r_with_bfs = fuse(BASEL, icon, cams, bfs_fetch=bfs)

    # With BFS reporting higher UV and max weight applied, fused UV should be higher
    assert r_with_bfs.hourly[12].uv_index > r_no_bfs.hourly[12].uv_index
    assert r_with_bfs.bfs_source_name == "BFS Schauinsland"
    assert "BFS Schauinsland" in r_with_bfs.sources_used
    assert len(r_with_bfs.bfs_hourly_weights) == 24
    # At 20% model cloud cover, BFS weight should be BFS_MAX_WEIGHT * (1 - 0.2)
    assert r_with_bfs.bfs_hourly_weights[12] == pytest.approx(BFS_MAX_WEIGHT * 0.8, rel=0.01)


def test_fuse_bfs_excluded_when_fully_overcast():
    """BFS weight drops to 0 at 100% cloud cover."""
    icon_points = [
        HourlyPoint(hour=h, uv_index=4.0, uv_index_clear_sky=5.0, cloud_cover_pct=100.0)
        for h in range(24)
    ]
    gfs_points = [
        HourlyPoint(hour=h, uv_index=4.0, uv_index_clear_sky=5.0, cloud_cover_pct=100.0)
        for h in range(24)
    ]
    bfs_points = [
        HourlyPoint(hour=h, uv_index=6.0, uv_index_clear_sky=6.0, cloud_cover_pct=0.0)
        for h in range(24)
    ]
    icon2 = SourceFetch(name="Open-Meteo Global", ok=True, hourly=icon_points)
    gfs2 = SourceFetch(name="Open-Meteo GFS", ok=True, hourly=gfs_points)
    bfs = SourceFetch(name="BFS Schauinsland", ok=True, hourly=bfs_points, station_alt_m=0.0)

    r = fuse(BASEL, icon2, gfs2, bfs_fetch=bfs)

    # At 100% cloud, BFS weight should be 0 at all hours
    assert all(w == pytest.approx(0.0) for w in r.bfs_hourly_weights)


def test_fuse_bfs_offset_is_model_vs_ground():
    """BFS offset should reflect model-only estimate vs BFS ground measurement."""
    icon = _make_fetch("Open-Meteo Global", [0.0] * 6 + [4.0] * 12 + [0.0] * 6)
    cams = _make_fetch("Open-Meteo GFS", [0.0] * 6 + [4.0] * 12 + [0.0] * 6)
    bfs = _make_fetch("BFS Schauinsland", [0.0] * 6 + [6.0] * 12 + [0.0] * 6, station_alt_m=0.0)

    r = fuse(BASEL, icon, cams, bfs_fetch=bfs)

    assert r.bfs_offset is not None
    assert r.bfs_hours_matched == 12
    # BFS (6.0 at 0m → adjusted to Basel 260m) vs model (4.0 at 0m → adjusted) should be positive
    assert r.bfs_offset > 0.0


def test_fuse_uv_never_negative():
    icon = _make_fetch("Open-Meteo Global", [-1.0, 0.0, 5.0] + [0.0] * 21)
    cams = _make_fetch("Open-Meteo ECMWF", [0.0] * 24)

    r = fuse(BASEL, icon, cams)
    assert all(p.uv_index >= 0.0 for p in r.hourly)


def test_fuse_three_sources():
    icon = _make_fetch("Open-Meteo Global", [0.0] * 8 + [5.0] * 8 + [0.0] * 8)
    cams = _make_fetch("Open-Meteo ECMWF", [0.0] * 8 + [7.0] * 8 + [0.0] * 8)
    metro = _make_fetch("MeteoSwiss ICON-CH2", [0.0] * 8 + [6.0] * 8 + [0.0] * 8)

    r = fuse(BASEL, icon, cams, metro)

    assert len(r.sources_used) == 3
    assert "MeteoSwiss ICON-CH2" in r.sources_used
    for w in r.weights.values():
        assert w == pytest.approx(1 / 3, rel=0.01)
    expected = 6.0 * (1.0 + 260.0 / 1000.0 * 0.10)
    assert r.hourly[8].uv_index == pytest.approx(expected, rel=0.01)
