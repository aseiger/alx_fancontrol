"""Unit tests for curve math (eval, bounds, rate limit, pct<->raw, dead temp)."""
import pytest

from alx_fancontrol import curve

PTS = [[40, 20], [55, 30], [65, 70], [75, 100]]


# ------------------------------------------------------------ eval_curve --

def test_exact_hit_on_points():
    for t, d in PTS:
        assert curve.eval_curve(PTS, t) == d


def test_midpoint_interpolation():
    assert curve.eval_curve(PTS, 60) == 50.0      # halfway 55->65: (30+70)/2
    assert curve.eval_curve(PTS, 57.5) == 40.0    # halfway 55->65 on temp
    assert curve.eval_curve(PTS, 42.5) == pytest.approx(21.667, abs=1e-3)


def test_clamp_below_first_point():
    assert curve.eval_curve(PTS, 0) == 20
    assert curve.eval_curve(PTS, 39.9) == 20


def test_clamp_above_last_point():
    assert curve.eval_curve(PTS, 100) == 100
    assert curve.eval_curve(PTS, 75.1) == 100


def test_single_point_curve():
    assert curve.eval_curve([[50, 42]], 10) == 42
    assert curve.eval_curve([[50, 42]], 50) == 42
    assert curve.eval_curve([[50, 42]], 90) == 42


def test_empty_curve_raises():
    with pytest.raises(IndexError):
        curve.eval_curve([], 50)


# ----------------------------------------------------------- apply_bounds --

def test_apply_bounds_floor():
    assert curve.apply_bounds(10, 20, 100) == 20


def test_apply_bounds_max():
    assert curve.apply_bounds(110, 20, 100) == 100


def test_apply_bounds_within():
    assert curve.apply_bounds(55, 20, 100) == 55


def test_apply_bounds_exact_edges():
    assert curve.apply_bounds(20, 20, 80) == 20
    assert curve.apply_bounds(80, 20, 80) == 80


# ------------------------------------------------------------- rate_limit --

def test_rate_limit_first_poll_jumps():
    assert curve.rate_limit(None, 23.3, 6) == 23
    assert curve.rate_limit(None, 0, 6) == 0


def test_rate_limit_steps_up_by_max():
    assert curve.rate_limit(50, 80, 6) == 56


def test_rate_limit_steps_down_by_max():
    assert curve.rate_limit(50, 20, 6) == 44


def test_rate_limit_exact_full_step():
    assert curve.rate_limit(50, 44, 6) == 44
    assert curve.rate_limit(50, 56, 6) == 56


def test_rate_limit_partial_step():
    assert curve.rate_limit(50, 54, 6) == 54


def test_rate_limit_no_move():
    assert curve.rate_limit(50, 50, 6) == 50
    assert curve.rate_limit(50, 50.4, 6) == 50    # rounds back to cur


def test_rate_limit_zero_rate():
    assert curve.rate_limit(50, 80, 0) == 50


# ---------------------------------------------------------- pct_to_raw255 --

def test_pct_to_raw255_endpoints():
    assert curve.pct_to_raw255(0) == 0
    assert curve.pct_to_raw255(100) == 255


def test_pct_to_raw255_plan_case():
    assert curve.pct_to_raw255(41) == 105
    assert curve.pct_to_raw255(50) == 128


def test_pct_to_raw255_monotonic():
    vals = [curve.pct_to_raw255(p / 10) for p in range(0, 1001, 5)]
    assert all(a <= b for a, b in zip(vals, vals[1:]))


# ---------------------------------------------------------- is_dead_temp --

def test_dead_temp_none():
    assert curve.is_dead_temp(None, -10)


def test_dead_temp_below_threshold():
    assert curve.is_dead_temp(-55.0, -10)   # the dead thermistors' reading
    assert curve.is_dead_temp(-10.0, -10) is False  # boundary: not below


def test_live_temp_not_dead():
    assert not curve.is_dead_temp(25.0, -10)
    assert not curve.is_dead_temp(-5.0, -10)
