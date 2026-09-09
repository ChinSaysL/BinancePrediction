"""Scoring and calibration diagnostics.

Accuracy is the wrong metric here. What matters is whether a stated 0.7 wins
about 70% of the time, because the trade rule compares our probability to a
price. A model can be 55% accurate and still lose money if it is overconfident,
and can be 51% accurate and profitable if it is well calibrated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

_EPS = 1e-12


@dataclass
class Scores:
    n: int
    brier: float
    log_loss: float
    accuracy: float
    base_rate: float
    skill_vs_coinflip: float   # 1 - brier/0.25
    resolution: float
    reliability: float

    def __str__(self) -> str:
        return (f"n={self.n}  Brier={self.brier:.4f}  logloss={self.log_loss:.4f}  "
                f"acc={self.accuracy:.4f}  base={self.base_rate:.4f}  "
                f"skill={self.skill_vs_coinflip:+.1%}")


@dataclass
class Bucket:
    lo: float
    hi: float
    n: int = 0
    wins: int = 0
    p_sum: float = 0.0

    @property
    def predicted(self) -> float:
        return self.p_sum / self.n if self.n else 0.0

    @property
    def actual(self) -> float:
        return self.wins / self.n if self.n else 0.0


def score(preds: list[float], outcomes: list[int]) -> Scores:
    """preds: P(event). outcomes: 1 if the event happened."""
    n = len(preds)
    if n == 0 or n != len(outcomes):
        raise ValueError(f"length mismatch: {len(preds)} preds, {len(outcomes)} outcomes")

    brier = sum((p - y) ** 2 for p, y in zip(preds, outcomes)) / n
    log_loss = -sum(
        y * math.log(max(_EPS, p)) + (1 - y) * math.log(max(_EPS, 1 - p))
        for p, y in zip(preds, outcomes)
    ) / n
    acc = sum(1 for p, y in zip(preds, outcomes) if (p >= 0.5) == bool(y)) / n
    base = sum(outcomes) / n

    # Murphy decomposition: Brier = reliability - resolution + uncertainty
    bs = reliability_buckets(preds, outcomes, n_buckets=10)
    rel = sum(b.n * (b.predicted - b.actual) ** 2 for b in bs) / n
    res = sum(b.n * (b.actual - base) ** 2 for b in bs) / n

    return Scores(n=n, brier=brier, log_loss=log_loss, accuracy=acc, base_rate=base,
                  skill_vs_coinflip=1.0 - brier / 0.25, resolution=res, reliability=rel)


def reliability_buckets(preds: list[float], outcomes: list[int],
                        n_buckets: int = 10) -> list[Bucket]:
    buckets = [Bucket(i / n_buckets, (i + 1) / n_buckets) for i in range(n_buckets)]
    for p, y in zip(preds, outcomes):
        idx = min(n_buckets - 1, max(0, int(p * n_buckets)))
        b = buckets[idx]
        b.n += 1
        b.wins += int(y)
        b.p_sum += p
    return buckets


def reliability_table(preds: list[float], outcomes: list[int],
                      n_buckets: int = 10, min_n: int = 10) -> str:
    """Text reliability diagram -- a bar chart would need matplotlib."""
    rows = ["  bucket   n     predicted  actual   gap    |0                  1|"]
    for b in reliability_buckets(preds, outcomes, n_buckets):
        if b.n < min_n:
            continue
        gap = b.actual - b.predicted
        pos = int(round(b.actual * 20))
        bar = "".join("#" if i == pos else ("." if i == int(round(b.predicted * 20)) else " ")
                      for i in range(21))
        rows.append(f"  {b.lo:.1f}-{b.hi:.1f} {b.n:5d}   {b.predicted:8.3f} {b.actual:7.3f} "
                    f"{gap:+6.3f}   |{bar}|")
    rows.append("  legend: '.' = predicted, '#' = actual; they coincide when calibrated")
    return "\n".join(rows)


@dataclass
class PnLSummary:
    n_trades: int = 0
    wins: int = 0
    staked: float = 0.0
    fees: float = 0.0
    pnl: float = 0.0
    pnls: list[float] = field(default_factory=list)

    def add(self, pnl: float, stake: float, fee: float, won: bool) -> None:
        self.n_trades += 1
        self.wins += int(won)
        self.staked += stake
        self.fees += fee
        self.pnl += pnl
        self.pnls.append(pnl)

    @property
    def roi(self) -> float:
        return self.pnl / self.staked if self.staked else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_trades if self.n_trades else 0.0

    @property
    def max_drawdown(self) -> float:
        peak = cum = 0.0
        mdd = 0.0
        for p in self.pnls:
            cum += p
            peak = max(peak, cum)
            mdd = min(mdd, cum - peak)
        return mdd

    def __str__(self) -> str:
        if not self.n_trades:
            return "no trades"
        return (f"trades={self.n_trades} win_rate={self.win_rate:.3f} "
                f"staked=${self.staked:.2f} fees=${self.fees:.2f} "
                f"pnl=${self.pnl:+.2f} roi={self.roi:+.2%} mdd=${self.max_drawdown:.2f}")
