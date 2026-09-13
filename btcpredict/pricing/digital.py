"""Digital (binary) option fair value for a 5-minute up/down round.

P(settle > strike) under a driftless lognormal walk:

    P = Phi( ln(S/K) / (sigma * sqrt(tau)) )

No drift term by design. Measured 5-minute drift is indistinguishable from zero
(483 up / 513 down over 1000 bars), so a fitted drift would encode noise and
would be the easiest way to turn a calibrated model into an overfitted one.

Settlement is `end_price > strike -> Up`, strictly. A tie settles Down, which is
why `tau <= 0` compares with `>` rather than `>=`.
"""
from __future__ import annotations

import math

# Clamp so a single wild input cannot produce a 0.0/1.0 that implies infinite edge.
_EPS = 1e-6


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def moneyness_sd(S: float, K: float, tau_s: float, sigma_1m: float) -> float | None:
    """How many sigma of the REMAINING move separate the spot from the strike?

        z = |ln(S/K)| / (sigma_1m * sqrt(tau/60))

    The ticket arm's `ticket_min_z` gate is built on this, and `_impulse_tick`
    computes the same quantity inline for its sizing tilt. Returns None when it
    cannot be computed -- callers must treat that as "no evidence", never as
    zero, or a missing sigma silently becomes a passing gate.
    """
    if not S or not K or S <= 0 or K <= 0 or tau_s <= 0 or sigma_1m <= 0:
        return None
    return abs(math.log(S / K)) / (sigma_1m * math.sqrt(tau_s / 60.0))


def fair_value(S: float, K: float, tau_s: float, sigma_1m: float) -> float:
    """Probability the round settles Up.

    S        current oracle/spot price
    K        strike (variantData.startPrice)
    tau_s    seconds remaining until settlement
    sigma_1m per-minute log-return standard deviation
    """
    if S <= 0 or K <= 0:
        raise ValueError(f"prices must be positive (S={S}, K={K})")
    if tau_s <= 0:
        return 1.0 if S > K else 0.0
    if sigma_1m <= 0:
        raise ValueError(f"sigma must be positive (got {sigma_1m})")

    sigma_tau = sigma_1m * math.sqrt(tau_s / 60.0)
    z = math.log(S / K) / sigma_tau
    return min(1.0 - _EPS, max(_EPS, norm_cdf(z)))


def moneyness_sd(S: float, K: float, tau_s: float, sigma_1m: float) -> float:
    """Distance from the strike in standard deviations -- useful for diagnostics."""
    if tau_s <= 0 or sigma_1m <= 0:
        return math.inf if S > K else -math.inf
    return math.log(S / K) / (sigma_1m * math.sqrt(tau_s / 60.0))


def implied_sigma(p_up: float, S: float, K: float, tau_s: float) -> float | None:
    """Invert fair_value to get the sigma the market's quote implies.

    Returns None when the quote carries no volatility information (at S == K
    every sigma yields 0.5) or when the inversion has no solution.
    """
    if tau_s <= 0 or S <= 0 or K <= 0:
        return None
    p = min(1.0 - 1e-9, max(1e-9, p_up))
    m = math.log(S / K)
    if abs(m) < 1e-12:
        return None
    # Invert Phi by bisection; robust and fast enough at this call rate.
    lo, hi = 1e-6, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        val = norm_cdf(m / (mid * math.sqrt(tau_s / 60.0)))
        if (val > p) == (m > 0):
            lo = mid
        else:
            hi = mid
    out = (lo + hi) / 2.0
    return out if 1e-5 < out < 0.999 else None
