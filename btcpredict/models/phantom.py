"""Phantom swing model.

Port of polymarket-bot-arena ``bots/bot_phantom.py``: EMA trend filter plus a
breakout trigger, gated by an ATR volatility sanity check.

One deliberate deviation, noted for honesty: the original computes the breakout
window *including* the current price, so ``current > recent_high`` can never
fire. Here the breakout window excludes the current bar (the previous
``breakout_lookback`` bars), which is the only reading under which the strategy
can trade at all.
"""
from __future__ import annotations

from btcpredict.models.base import ModelContext, StrategyModel, StrategySignal


class PhantomModel(StrategyModel):
    name = "phantom"
    DEFAULT_PARAMS = {
        "ema_fast": 20,
        "ema_slow": 50,
        "atr_period": 14,
        "breakout_lookback": 20,
        "min_atr_pct": 0.0005,   # 0.05%
        "max_atr_pct": 0.01,     # 1.0%
        "min_confidence": 0.20,
    }

    @staticmethod
    def _calc_ema(prices: list[float], period: int) -> float:
        if len(prices) < period:
            return sum(prices) / len(prices) if prices else 0.0
        alpha = 2.0 / (period + 1.0)
        ema = prices[0]
        for px in prices[1:]:
            ema = px * alpha + ema * (1.0 - alpha)
        return ema

    @staticmethod
    def _calc_atr(prices: list[float], period: int) -> float:
        """ATR approximation on closes (no H/L in our 1m series)."""
        if len(prices) < period + 1:
            return 0.0
        diffs = [abs(prices[i] - prices[i - 1])
                 for i in range(len(prices) - period, len(prices))]
        return sum(diffs) / period

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        p = self.params
        prices = ctx.closes
        if len(prices) < p["ema_slow"] + p["breakout_lookback"] + 1:
            return StrategySignal(0, 0, "phantom: insufficient data")

        current_price = ctx.price if ctx.price is not None else prices[-1]
        ema_fast = self._calc_ema(prices, p["ema_fast"])
        ema_slow = self._calc_ema(prices, p["ema_slow"])

        # Breakout window excludes the current bar (see module docstring).
        window = prices[-p["breakout_lookback"] - 1:-1]
        recent_high, recent_low = max(window), min(window)

        atr = self._calc_atr(prices, p["atr_period"])
        atr_pct = atr / current_price if current_price > 0 else 0.0
        if not (p["min_atr_pct"] <= atr_pct <= p["max_atr_pct"]):
            return StrategySignal(
                0, 0, f"phantom: vol out of bounds ({atr_pct:.4%})")

        if ema_fast > ema_slow and current_price > ema_fast and current_price > recent_high:
            trend_strength = (ema_fast - ema_slow) / current_price
            confidence = 0.3 + min(0.4, trend_strength * 100.0)
            if confidence < p["min_confidence"]:
                return StrategySignal(0, confidence, "phantom: conf below minimum")
            return StrategySignal(
                1, confidence,
                f"phantom LONG: trend={trend_strength:.4%}, breakout above {recent_high:.0f}")

        if ema_fast < ema_slow and current_price < ema_fast and current_price < recent_low:
            trend_strength = (ema_slow - ema_fast) / current_price
            confidence = 0.3 + min(0.4, trend_strength * 100.0)
            if confidence < p["min_confidence"]:
                return StrategySignal(0, confidence, "phantom: conf below minimum")
            return StrategySignal(
                -1, confidence,
                f"phantom SHORT: trend={trend_strength:.4%}, breakdown below {recent_low:.0f}")

        return StrategySignal(
            0, 0,
            f"phantom: no signal (ema_f={ema_fast:.0f}, ema_s={ema_slow:.0f}, "
            f"high={recent_high:.0f}, low={recent_low:.0f})")
