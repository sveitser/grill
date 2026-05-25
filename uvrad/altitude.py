"""Altitude correction for UV index.

WHO/ICNIRP standard: UV increases ~10% per 1000m elevation gain.
This is a linear approximation; the true effect depends on aerosol optical
depth and ozone column, but at Basel's elevation (260m) the correction is
only ~2.6% — well within the uncertainty of any forecast source.
"""


def altitude_factor(alt_m: float) -> float:
    """Multiplier relative to sea level for given altitude."""
    return 1.0 + (alt_m / 1000.0) * 0.10


def correct_uv(uv: float, from_alt_m: float, to_alt_m: float) -> float:
    """Convert UV index measured/forecast at from_alt_m to equivalent at to_alt_m."""
    if from_alt_m == to_alt_m:
        return uv
    return uv * altitude_factor(to_alt_m) / altitude_factor(from_alt_m)
