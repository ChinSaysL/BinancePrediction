"""Mean-reversion model.

Port of polymarket-bot-arena ``bots/bot_mean_rev.py`` ("Bot 2"): bet against
overextended moves, using a z-score over a lookback window plus RSI to confirm
the extension. Overextended UP (z > threshold, RSI > 70) -> lean Down;
overextended DOWN (z < -threshold, RSI < 30) -> lean Up.
"""
from __future__ import annotations

import math

from btcpredict.models.base import ModelContext, StrategyModel, StrategySignal


class MeanRevModel(StrategyModel):
    name = "mean_rev"
    DEFAULT_PARAMS = {
        "lookback_candles": 20,
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "reversion_threshold": 0.6,
        "min_confidence": 0.55,
    }

    @staticmethod
    def _calc_rsi(prices: list[float], period: int) -> float:
        if len(prices) < period + 1:
            return 50.0  # neutral
        gains, losses = [], []
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))
        gains, losses = gains[-period:], losses[-period:]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _calc_zscore(prices: list[float], lookback: int) -> float:
        if len(prices) < lookback:
            return 0.0
        window = prices[-lookback:]
        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        std = math.sqrt(variance) if variance > 0 else 1.0
        return (prices[-1] - mean) / std

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        p = self.params
        prices = ctx.closes
        if len(prices) < p["lookback_candles"]:
            return StrategySignal(0, 0, "mean_rev: insufficient data")

        zscore = self._calc_zscore(prices, p["lookback_candles"])
        rsi = self._calc_rsi(prices, p["rsi_period"])
        threshold = p["reversion_threshold"]

        if zscore > threshold and rsi > p["rsi_overbought"]:
            confidence = min(0.95, 0.5 + abs(zscore) * 0.15 + (rsi - 70.0) * 0.005)
            if confidence < p["min_confidence"]:
                return StrategySignal(0, confidence, "mean_rev: conf below minimum")
            return StrategySignal(
                -1, confidence,
                f"Mean reversion SHORT: z={zscore:.2f}, RSI={rsi:.1f} (overbought)")

        if zscore < -threshold and rsi < p["rsi_oversold"]:
            confidence = min(0.95, 0.5 + abs(zscore) * 0.15 + (30.0 - rsi) * 0.005)
            if confidence < p["min_confidence"]:
                return StrategySignal(0, confidence, "mean_rev: conf below minimum")
            return StrategySignal(
                1, confidence,
                f"Mean reversion LONG: z={zscore:.2f}, RSI={rsi:.1f} (oversold)")

        return StrategySignal(
            0, 0, f"No reversion signal: z={zscore:.2f}, RSI={rsi:.1f}")
