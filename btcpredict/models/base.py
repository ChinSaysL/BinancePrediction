"""Base types for pluggable pricing models.

The bot's original pricing model is a closed-form digital option. The strategy
bots ported from polymarket-bot-arena predict *direction* instead: each one
looks at recent candles and returns a side ("Up"/"Down") plus a confidence.
This module defines the smallest common interface that lets both kinds plug
into the same fair-value slot.

    ModelContext   everything a model may look at (candles, strike, tau, sigma)
    StrategySignal side/confidence/reasoning, the arena bots' native output
    ModelPred      p_up in (0,1) plus whether the model actually emitted a view
    StrategyModel  abstract base: analyze() -> StrategySignal, predict() -> ModelPred
    PriceHistory   rolling 1m-bar history (klines + live ticks) used by live mode

Mapping a directional signal to a probability is deliberately plain:
``p_up = 0.5 + 0.5 * side * confidence``, clamped to [0.05, 0.95]. That is the
same belief the arena bots trade on -- a side bet at confidence c is worth
exactly that probability. "Hold" maps to p=0.5 with ``emitted=False``, which
the orchestrator reads as "no trade", preserving selective strategies (sniper).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

_PMIN, _PMAX = 0.05, 0.95
# A decision is only relevant while it stays away from the certainty boundary:
# the digital model proved 0.0/1.0 implies infinite edge and sizes a bet on a
# single bad tick.
_P_EPS = 1e-6


@dataclass(slots=True)
class ModelContext:
    """Everything a model may inspect. Candles are 1m bars, oldest -> newest."""

    closes: list[float]
    volumes: list[float] | None = None
    taker_buys: list[float] | None = None   # Binance kline field 9: taker buy base
    price: float | None = None              # current spot (defaults to last close)
    strike: float | None = None
    tau_s: float | None = None              # seconds to settlement
    sigma_1m: float | None = None           # per-minute log-return stdev (digital only)

    def __post_init__(self) -> None:
        if self.volumes is None:
            self.volumes = []
        if self.taker_buys is None:
            self.taker_buys = []
        if self.price is None and self.closes:
            self.price = self.closes[-1]


@dataclass(slots=True)
class StrategySignal:
    """Arena-bot style output: side + confidence + reasoning."""

    side: int          # +1 Up, -1 Down, 0 hold
    confidence: float  # 0..1
    reason: str


@dataclass(slots=True)
class ModelPred:
    """Probability the round settles Up, plus whether it is a real view."""

    p_up: float
    emitted: bool
    reason: str


def clamp_p(p: float) -> float:
    return max(_PMIN + _P_EPS, min(_PMAX - _P_EPS, p))


def prob_from_signal(sig: StrategySignal) -> ModelPred:
    """Linear map from a side bet at confidence c to P(Up)."""
    if sig.side == 0:
        return ModelPred(0.5, False, sig.reason)
    p = 0.5 + 0.5 * sig.side * min(1.0, max(0.0, sig.confidence))
    return ModelPred(clamp_p(p), True, sig.reason)


class StrategyModel(ABC):
    """Base class for all pricing models (digital and arena ports)."""

    name: str = "base"
    DEFAULT_PARAMS: dict = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.DEFAULT_PARAMS, **(params or {})}

    @abstractmethod
    def analyze(self, ctx: ModelContext) -> StrategySignal:
        """Strategy logic on candles. Implemented per strategy."""

    def predict(self, ctx: ModelContext) -> ModelPred:
        try:
            return prob_from_signal(self.analyze(ctx))
        except Exception as e:  # a model error must never take the bot down
            return ModelPred(0.5, False, f"{self.name}: error {type(e).__name__}")


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

class PriceHistory:
    """Rolling 1m bars plus a live tick, feeding ModelContext in live mode.

    Seeded and refreshed from Binance REST klines (same fetch already done for
    the volatility estimator), extended every poll by the WS spot mid as the
    "current" candle. Merges deduplicate on open time so double fetches are
    harmless.
    """

    MAX_BARS = 2000

    def __init__(self) -> None:
        self._bars: list[tuple[int, float, float, float]] = []  # (open_ms, close, vol, taker)
        self._current: float | None = None

    def __len__(self) -> int:
        return len(self._bars) + (1 if self._current is not None else 0)

    @staticmethod
    def _parse(klines: list[list]) -> list[tuple[int, float, float, float]]:
        out = []
        for k in klines:
            try:
                out.append((int(k[0]), float(k[4]), float(k[5]), float(k[9])))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def seed(self, klines: list[list], drop_forming: bool = True) -> None:
        """Initial fill. The last REST kline is still forming, so drop it.

        ``_current`` stays None: it only ever holds a live WS tick, so seeded
        bars are never double-counted in ctx().
        """
        bars = self._parse(klines)
        if drop_forming and bars:
            bars = bars[:-1]
        self._bars = bars[-self.MAX_BARS:]
        self._current = None

    def merge(self, klines: list[list], drop_forming: bool = True) -> int:
        """Append complete bars newer than the newest one already held."""
        bars = self._parse(klines)
        if drop_forming and bars:
            bars = bars[:-1]
        newest = self._bars[-1][0] if self._bars else 0
        fresh = [b for b in bars if b[0] > newest]
        if not fresh:
            return 0
        self._bars = (self._bars + sorted(fresh))[-self.MAX_BARS:]
        return len(fresh)

    def add_tick(self, price: float | None) -> None:
        if price and price > 0:
            self._current = price

    def ctx(self, *, price: float | None = None, strike: float | None = None,
            tau_s: float | None = None, sigma_1m: float | None = None) -> ModelContext:
        cur = price if price and price > 0 else self._current
        closes = [b[1] for b in self._bars]
        if cur is not None:
            closes = closes + [cur]
        return ModelContext(
            closes=closes,
            volumes=[b[2] for b in self._bars],
            taker_buys=[b[3] for b in self._bars],
            price=cur,
            strike=strike,
            tau_s=tau_s,
            sigma_1m=sigma_1m,
        )
