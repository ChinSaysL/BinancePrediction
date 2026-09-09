"""Realized-volatility estimation for the 5-minute horizon.

sigma is the single most important model input: fair value depends on
ln(S/K)/(sigma*sqrt(tau)), so a sigma that is 2x too high halves the apparent
edge, and one that is 2x too low invents edge that is not there.

Estimator: EWMA over 1-minute log returns, blended with a slower simple mean for
stability, floored to survive dead-quiet periods. Intraday seasonality is applied
as a multiplicative factor learned from the same history rather than hardcoded.
"""
from __future__ import annotations

import logging
import math
import statistics as st
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_MIN_SAMPLES = 30


@dataclass
class VolEstimator:
    """Maintains sigma_1m from a rolling window of 1-minute log returns."""

    lam: float = 0.97
    floor: float = 0.00015
    lookback: int = 240
    seasonality: bool = True

    _returns: list[float] = field(default_factory=list)
    _hours: list[int] = field(default_factory=list)
    _ewma_var: float | None = None
    _season: dict[int, float] = field(default_factory=dict)

    # -- ingestion ----------------------------------------------------------
    def seed_from_klines(self, klines: list[list]) -> None:
        """Seed from Binance 1m klines: [open_time, o, h, l, c, ...]."""
        for k in klines:
            try:
                o, c = float(k[1]), float(k[4])
                ts = int(k[0]) // 1000
            except (TypeError, ValueError, IndexError):
                continue
            if o > 0 and c > 0:
                self.add(math.log(c / o), hour=time.gmtime(ts).tm_hour)
        self._recompute_seasonality()
        log.info("vol seeded: n=%d sigma_1m=%.6f", len(self._returns), self.sigma_1m())

    def add(self, log_return: float, hour: int | None = None) -> None:
        self._returns.append(log_return)
        self._hours.append(time.gmtime().tm_hour if hour is None else hour)
        if len(self._returns) > self.lookback:
            self._returns.pop(0)
            self._hours.pop(0)
        r2 = log_return * log_return
        self._ewma_var = r2 if self._ewma_var is None else (
            self.lam * self._ewma_var + (1.0 - self.lam) * r2
        )

    # -- estimation ---------------------------------------------------------
    def _recompute_seasonality(self) -> None:
        """Multiplicative per-UTC-hour vol factor, shrunk toward 1.0."""
        if not self.seasonality or len(self._returns) < _MIN_SAMPLES:
            return
        base = st.pstdev(self._returns) or self.floor
        buckets: dict[int, list[float]] = {}
        for r, h in zip(self._returns, self._hours):
            buckets.setdefault(h, []).append(r)
        season = {}
        for h, rs in buckets.items():
            if len(rs) < 10:
                continue
            factor = (st.pstdev(rs) or base) / base
            # Shrink toward 1 by sample weight, and clamp: an hour with 12 samples
            # should barely move the estimate.
            w = min(1.0, len(rs) / 60.0)
            season[h] = max(0.5, min(2.0, 1.0 + w * (factor - 1.0)))
        self._season = season

    def sigma_1m(self, hour: int | None = None) -> float:
        """Per-minute log-return standard deviation."""
        if not self._returns:
            return self.floor
        simple = st.pstdev(self._returns) if len(self._returns) > 1 else abs(self._returns[0])
        ewma = math.sqrt(self._ewma_var) if self._ewma_var else simple
        # Blend: EWMA reacts, simple stabilises.
        sigma = 0.7 * ewma + 0.3 * simple
        if self.seasonality and self._season:
            h = time.gmtime().tm_hour if hour is None else hour
            sigma *= self._season.get(h, 1.0)
        return max(self.floor, sigma)

    def sigma_for_tau(self, tau_s: float, hour: int | None = None) -> float:
        """Total sigma over tau seconds (square-root-of-time scaling)."""
        return self.sigma_1m(hour) * math.sqrt(max(0.0, tau_s) / 60.0)

    @property
    def n(self) -> int:
        return len(self._returns)
