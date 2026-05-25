"""Tests for altitude UV correction."""

import pytest

from uvrad.altitude import altitude_factor, correct_uv


def test_sea_level_factor():
    assert altitude_factor(0.0) == pytest.approx(1.0)


def test_1000m_factor():
    assert altitude_factor(1000.0) == pytest.approx(1.10)


def test_basel_factor():
    # Basel at 260m: factor should be 1.026
    assert altitude_factor(260.0) == pytest.approx(1.026)


def test_schauinsland_factor():
    # Schauinsland at 1284m
    assert altitude_factor(1284.0) == pytest.approx(1.1284)


def test_correct_uv_same_altitude():
    assert correct_uv(5.0, 260.0, 260.0) == pytest.approx(5.0)


def test_correct_uv_sea_level_to_basel():
    # UV at sea level → Basel: should increase slightly
    uv_sl = 5.0
    uv_basel = correct_uv(uv_sl, 0.0, 260.0)
    assert uv_basel > uv_sl
    assert uv_basel == pytest.approx(5.0 * 1.026)


def test_correct_uv_schauinsland_to_basel():
    # Schauinsland reading of 8.0 → Basel equivalent
    uv_schauinsland = 8.0
    uv_basel = correct_uv(uv_schauinsland, 1284.0, 260.0)
    # Should be less than Schauinsland (lower altitude)
    assert uv_basel < uv_schauinsland
    assert uv_basel == pytest.approx(8.0 * 1.026 / 1.1284)


def test_correct_uv_invertible():
    uv = 6.5
    uv_corrected = correct_uv(uv, 0.0, 500.0)
    uv_back = correct_uv(uv_corrected, 500.0, 0.0)
    assert uv_back == pytest.approx(uv, rel=1e-9)
