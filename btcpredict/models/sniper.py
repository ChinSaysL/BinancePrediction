"""Sniper model -- high-selectivity momentum.

The arena's ``bots/bot_sniper.py`` gates trades on Polymarket price zones that
encode that venue's own market price as a signal. Those zones do not exist on
Binance (the round quote is only available live, and the backtest has no
historical quotes), so the port keeps the sniper's *essence*: trade rarely,
only when several independent conditions all confirm, and never without
momentum confirmation. The Polymarket YES/NO price zones are replaced by a
stricter momentum confirmation (higher trend-strength bar, volatility sanity
check) and the early/late-window boosts are kept, driven by ``tau_s``.
"""
from __future__ import annotations

from btcpredict.models.base import ModelContext, StrategyModel, StrategySignal

# The venue's round length is fixed at 5 minutes.
ROUND_SECONDS = 300


class SniperModel(StrategyModel):
    name = "sniper"
    DEFAULT_PARAMS = {
        "lookback_candles": 6,
        "momentum_threshold": 0.0003,   # tight, as in the original
        "trend_confirm": 0.6,           # fraction of one-sided steps required
        "atr_period": 14,
        "min_atr_pct": 0.0002,
        "max_atr_pct": 0.01,
        "min_confidence": 0.10,
        "early_window_s": 90.0,         # boosts mirror the original window rules
        "late_window_s": 60.0,
    }

    @staticmethod
    def _atr(prices: list[float], period: int) -> float:
        if len(prices) < period + 1:
            return 0.0
        diffs = [abs(prices[i] - prices[i - 1])
                 for i in range(len(prices) - period, len(prices))]
        return sum(diffs) / period

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        p = self.params
        prices = ctx.closes
        lb = p["lookback_candles"]
        if len(prices) < lb + 1:
            return StrategySignal(0, 0, "sniper: insufficient data")

        recent = prices[-lb - 1:]
        momentum = (recent[-1] - recent[0]) / recent[0] if recent[0] > 0 else 0.0
        if abs(momentum) < p["momentum_threshold"]:
            return StrategySignal(
                0, 0, f"sniper: momentum {momentum:+.4f} below {p['momentum_threshold']}")

        # Consecutive one-sided moves over the lookback.
        consecutive = sum(
            1 for i in range(1, lb + 1)
            if (momentum > 0 and recent[i] > recent[i - 1])
            or (momentum < 0 and recent[i] < recent[i - 1]))
        trend_frac = consecutive / lb
        if trend_frac < p["trend_confirm"]:
            return StrategySignal(
                0, 0, f"sniper: trend strength {trend_frac:.2f} < {p['trend_confirm']}")

        atr = self._atr(prices, p["atr_period"])
        price = ctx.price if ctx.price is not None else prices[-1]
        atr_pct = atr / price if price > 0 else 0.0
        if not (p["min_atr_pct"] <= atr_pct <= p["max_atr_pct"]):
            return StrategySignal(
                0, 0, f"sniper: vol out of bounds ({atr_pct:.4%})")

        side = 1 if momentum > 0 else -1
        # Confidence scales with how far momentum is past the trigger.
        confidence = 0.20 + min(0.40, (abs(momentum) / p["momentum_threshold"]) * 0.05)

        # Early/late-window boosts, ported from the original timing rules.
        tau = ctx.tau_s
        if tau is not None:
            window_age = ROUND_SECONDS - tau
            if 0.0 <= window_age < p["early_window_s"]:
                confidence *= 1.25
            if 0.0 < tau < p["late_window_s"]:
                confidence *= 1.30
        confidence = min(0.95, confidence)

        if confidence < p["min_confidence"]:
            return StrategySignal(0, confidence, "sniper: conf below minimum")

        return StrategySignal(
            side, confidence,
            f"sniper: mom={momentum:+.4f} trend={trend_frac:.2f} "
            f"atr={atr_pct:.3%} conf={confidence:.2f}")
