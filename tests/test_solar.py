"""Tests for solar position and UV interpolation."""

from datetime import UTC, datetime

import pytest

from uvrad.models import HourlyPoint
from uvrad.solar import interpolate_current_uv, solar_zenith_deg

BASEL_LAT = 47.5596
BASEL_LON = 7.5886


def test_solar_zenith_noon_basel():
    # Solar noon in Basel in summer should give zenith well below 90°
    # Around June 21, solar noon is ~12:20 CEST (10:20 UTC)
    dt = datetime(2024, 6, 21, 10, 20, tzinfo=UTC)
    zenith = solar_zenith_deg(BASEL_LAT, BASEL_LON, dt)
    assert zenith < 45.0, f"Expected zenith < 45° at summer noon, got {zenith:.1f}°"


def test_solar_zenith_midnight_basel():
    # Midnight UTC is local 02:00 CEST — sun definitely below horizon
    dt = datetime(2024, 6, 21, 0, 0, tzinfo=UTC)
    zenith = solar_zenith_deg(BASEL_LAT, BASEL_LON, dt)
    assert zenith > 90.0, f"Expected zenith > 90° at midnight, got {zenith:.1f}°"


def test_interpolate_returns_zero_at_night():
    # All hourly points show zero UV (night)
    hourly = [HourlyPoint(hour=h, uv_index=0.0, uv_index_clear_sky=0.0, cloud_cover_pct=0.0)
              for h in range(24)]
    # 2 AM local time (UTC midnight)
    now = datetime(2024, 6, 21, 0, 0, tzinfo=UTC)
    result = interpolate_current_uv(hourly, BASEL_LAT, BASEL_LON, now)
    assert result == pytest.approx(0.0)


def test_interpolate_midday():
    # Simulate a bell-curve UV day peaking at hour 12
    hourly = []
    for h in range(24):
        # Simple bell curve
        uv = max(0.0, 8.0 - abs(h - 12) * 1.2)
        hourly.append(HourlyPoint(hour=h, uv_index=uv, uv_index_clear_sky=uv, cloud_cover_pct=0.0))

    # Request UV at 12:30 UTC (roughly 14:30 CEST — early afternoon)
    now = datetime(2024, 6, 21, 10, 30, tzinfo=UTC)  # 12:30 CEST
    result = interpolate_current_uv(hourly, BASEL_LAT, BASEL_LON, now)
    # Should be positive and reasonable
    assert result > 0.0
    assert result <= 10.0


def test_interpolate_empty_hourly():
    now = datetime(2024, 6, 21, 10, 0, tzinfo=UTC)
    result = interpolate_current_uv([], BASEL_LAT, BASEL_LON, now)
    assert result == pytest.approx(0.0)


def test_interpolate_never_negative():
    """Interpolation should never return negative UV values."""
    hourly = [HourlyPoint(hour=h, uv_index=float(h % 3) * 2, uv_index_clear_sky=0.0,
                          cloud_cover_pct=0.0)
              for h in range(24)]
    now = datetime(2024, 6, 21, 11, 30, tzinfo=UTC)
    result = interpolate_current_uv(hourly, BASEL_LAT, BASEL_LON, now)
    assert result >= 0.0
