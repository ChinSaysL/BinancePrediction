"""Momentum / trend-following model.

Port of polymarket-bot-arena ``bots/bot_momentum.py`` ("Bot 1"), adapted to
Binance: instead of Polymarket YES/NO side + confidence it emits P(Up) via the
base-class mapping. The strategy itself is unchanged: trade in the direction of
short-term candle momentum, weighted by how one-sided the recent moves were and
by relative volume.
"""
from __future__ import annotations

from btcpredict.models.base import ModelContext, StrategyModel, StrategySignal


class MomentumModel(StrategyModel):
    name = "momentum"
    DEFAULT_PARAMS = {
        "lookback_candles": 5,
        "momentum_threshold": 0.002,      # 0.2% move to trigger
        "trend_strength_weight": 0.7,
        "volume_weight": 0.3,
        "min_confidence": 0.55,
    }

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        p = self.params
        prices = ctx.closes
        if len(prices) < p["lookback_candles"]:
            return StrategySignal(0, 0, "momentum: insufficient data")

        recent = prices[-p["lookback_candles"]:]
        oldest, newest = recent[0], recent[-1]
        if oldest <= 0:
            return StrategySignal(0, 0, "momentum: zero price")

        pct_change = (newest - oldest) / oldest
        threshold = p["momentum_threshold"]

        # Trend strength: fraction of consecutive steps in the signal direction.
        consecutive = 0
        for i in range(1, len(recent)):
            if pct_change > 0 and recent[i] > recent[i - 1]:
                consecutive += 1
            elif pct_change < 0 and recent[i] < recent[i - 1]:
                consecutive += 1
        trend_strength = consecutive / (len(recent) - 1) if len(recent) > 1 else 0.0

        # Volume signal: recent volume vs the window before it.
        volumes = ctx.volumes or []
        vol_signal = 0.5
        if len(volumes) >= p["lookback_candles"]:
            recent_vol = sum(volumes[-p["lookback_candles"]:])
            prev = volumes[-2 * p["lookback_candles"]:-p["lookback_candles"]]
            prev_vol = sum(prev) if prev else recent_vol
            vol_signal = min(1.0, recent_vol / max(prev_vol, 1.0)) * 0.5 + 0.25

        confidence = trend_strength * p["trend_strength_weight"] + vol_signal * p["volume_weight"]

        if abs(pct_change) < threshold:
            return StrategySignal(
                0, confidence,
                f"momentum {pct_change:+.4f} below threshold {threshold}")

        if confidence < p["min_confidence"]:
            return StrategySignal(
                0, confidence,
                f"momentum conf {confidence:.2f} < {p['min_confidence']}")

        side = 1 if pct_change > 0 else -1
        return StrategySignal(
            side, min(confidence, 0.95),
            f"Momentum {pct_change:+.4f} ({p['lookback_candles']} candles), "
            f"trend_str={trend_strength:.2f}, vol={vol_signal:.2f}")
