"""Solar position and UV interpolation using pvlib.

UV irradiance at the surface is proportional to cos(solar_zenith_angle),
not to clock time. Using cosine-weighted interpolation between hourly
forecast values gives a more physically accurate sub-hourly estimate,
especially near sunrise/sunset where UV changes rapidly.
"""

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pvlib

from uvrad.models import HourlyPoint


def solar_zenith_deg(lat: float, lon: float, dt: datetime) -> float:
    """Return solar zenith angle in degrees for a given location and time.

    dt may be timezone-aware (any tz) or naive (treated as UTC).
    """
    dt_utc = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    times = pd.DatetimeIndex([dt_utc])
    pos = pvlib.solarposition.get_solarposition(times, lat, lon)
    return float(pos["zenith"].iloc[0])


def solar_zenith_series(
    lat: float, lon: float, day: date, tz: str = "Europe/Zurich"
) -> list[float]:
    """Return list of 24 solar zenith angles (hourly, local midnight to 23:00)."""
    times = pd.date_range(
        start=f"{day} 00:00",
        periods=24,
        freq="h",
        tz=tz,
    )
    pos = pvlib.solarposition.get_solarposition(times, lat, lon)
    return list(pos["zenith"].values)


def interpolate_current_uv(
    hourly: list[HourlyPoint],
    lat: float,
    lon: float,
    now: datetime,
) -> float:
    """Interpolate current UV from hourly forecast using solar zenith cosine weighting.

    Returns 0.0 if the sun is below the horizon.
    """
    hour = now.hour
    minute = now.minute

    if not hourly:
        return 0.0

    # Clamp to valid index range
    h0 = min(hour, len(hourly) - 1)
    h1 = min(hour + 1, len(hourly) - 1)

    uv0 = max(0.0, hourly[h0].uv_index)
    uv1 = max(0.0, hourly[h1].uv_index)

    if h0 == h1:
        return uv0

    # Fractional position within the hour
    frac = minute / 60.0

    # Solar zenith at the three times: h0:00, now, h1:00
    dt0 = now.replace(hour=h0, minute=0, second=0, microsecond=0)
    dt1 = now.replace(hour=h1, minute=0, second=0, microsecond=0)

    z0 = solar_zenith_deg(lat, lon, dt0)
    z_now = solar_zenith_deg(lat, lon, now)
    z1 = solar_zenith_deg(lat, lon, dt1)

    # If sun is below horizon right now, return 0
    if z_now >= 90.0:
        return 0.0

    cos0 = max(0.0, float(np.cos(np.radians(z0))))
    cos_now = max(0.0, float(np.cos(np.radians(z_now))))
    cos1 = max(0.0, float(np.cos(np.radians(z1))))

    cos_interp = cos0 + frac * (cos1 - cos0)

    if cos_interp <= 0.0:
        return 0.0

    # If both endpoints have valid cos values, use cosine-ratio interpolation
    if cos0 > 0.0 and cos1 > 0.0:
        # Weight by cosine fraction within [cos0, cos1]
        cos_frac = (cos_now - cos0) / (cos1 - cos0) if (cos1 - cos0) != 0.0 else frac
        cos_frac = max(0.0, min(1.0, cos_frac))
        result = uv0 + cos_frac * (uv1 - uv0)
    else:
        # Fall back to linear interpolation
        result = uv0 + frac * (uv1 - uv0)

    return max(0.0, result)
