"""Impulse entries: trade the underlying's move, not the model's opinion.

Measured 2026-09-12 on 96,800 recorded ticks (FINDINGS.md): a spot impulse of
4-8 bps over 1-5 s is followed by the venue's 5-minute book still showing the
old price, and buying the side of the impulse paid +8% to +21% per $1 in both
halves of the sample. Impulses under ~2 bps are noise -- the venue is right
about those and the fee is the whole result (-6% per $1). The model is not
consulted: its own gate scored +1.3% net with a negative second half on the
same rounds.

These functions are pure so the trigger can be tested without a running bot.
"""
from __future__ import annotations

import math


def impulse_bps(points: list[tuple[int, float]], window_s: float,
                slack_s: float = 1.5) -> float | None:
    """Signed log-return in bps of the newest point against the newest point at
    least `window_s` older.

    `points` is chronological (ts_ms, price). Returns None when there is not
    enough history -- and, importantly, when the only reference point at or
    before the window is *staler* than window + slack: an old move is not an
    impulse, and trading it would be trading a stale signal.
    """
    if len(points) < 2:
        return None
    ts_now, px_now = points[-1]
    if px_now <= 0:
        return None
    target = ts_now - int(window_s * 1000)
    oldest_allowed = ts_now - int((window_s + slack_s) * 1000)
    for ts, px in reversed(points[:-1]):
        if ts > target:
            continue
        if px > 0 and ts >= oldest_allowed:
            return 1e4 * math.log(px_now / px)
        return None
    return None


def impulse_side(bps: float | None, threshold_bps: float) -> str | None:
    """'Up'/'Down' when the impulse clears the threshold, else None.

    Symmetric on purpose: the measurement showed both directions paying.
    """
    if bps is None or abs(bps) < threshold_bps:
        return None
    return "Up" if bps > 0 else "Down"
