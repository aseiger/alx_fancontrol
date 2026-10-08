"""Piecewise-linear curve math: eval, bounds, rate limiting, pct<->255.

Duty is percent (0-100) everywhere in the config/daemon; raw 0-255 only at
the sysfs boundary (pct_to_raw255).
"""
from __future__ import annotations


def eval_curve(points, temp: float) -> float:
    """Evaluate a curve at `temp`.

    points: list of [temp_c, duty_pct] pairs with temps strictly increasing
    (validated elsewhere). Clamps flat beyond the end points.
    """
    pts = [(float(t), float(d)) for t, d in points]
    if temp <= pts[0][0]:
        return pts[0][1]
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        if temp <= t1:
            if t1 == t0:
                return d1
            return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
    return pts[-1][1]


def apply_bounds(val: float, floor_duty: float, max_duty: float) -> float:
    """Clamp a duty percentage into [floor_duty, max_duty]."""
    return max(float(floor_duty), min(float(max_duty), float(val)))


def rate_limit(cur, tgt: float, max_rate: float) -> int:
    """Step `cur` toward `tgt` by at most `max_rate` (percent per poll).

    `cur` None → jump straight to tgt (first poll, like gpu-fanctl).
    Returns an int percentage.
    """
    tgt = float(tgt)
    if cur is None:
        return int(round(tgt))
    cur = float(cur)
    delta = tgt - cur
    step = max(-float(max_rate), min(float(max_rate), delta))
    return int(round(cur + step))


def pct_to_raw255(pct: float) -> int:
    """Percent 0-100 → raw sysfs duty 0-255 (round-half-up at .5)."""
    return int(round(float(pct) * 255 / 100))


def is_dead_temp(temp, dead_temp_c: float) -> bool:
    """True when the reading is unusable: absent, or below dead_temp_c.

    Dead thermistors on this board read -55 °C; default dead_temp_c=-10
    filters them out.
    """
    return temp is None or temp < dead_temp_c
