"""Sentiment model -- Binance order-flow adaptation.

The arena's ``bots/bot_sentiment.py`` reads X/social signals, which do not exist
here. The closest Binance-native analogue is *taker order flow*, which every
kline reports directly (field 9: taker buy base volume). The port keeps the
original decision structure -- weighted raw score + score momentum, bullish /
bearish thresholds, noise filter -- and substitutes:

    social score         -> taker buy ratio over the window (0 = all selling)
    influencer score     -> 0.5 (no influencer analogue; drops out of the blend)
    post count filter    -> minimum total volume filter (thin bars are noise)
    sentiment momentum   -> change in the ratio vs the previous window
"""
from __future__ import annotations

from btcpredict.models.base import ModelContext, StrategyModel, StrategySignal


class SentimentModel(StrategyModel):
    name = "sentiment"
    DEFAULT_PARAMS = {
        "sentiment_window": 10,          # bars for the taker-buy ratio
        "momentum_window": 10,           # bars to compare against for momentum
        "min_volume_per_bar": 0.0,       # noise filter: aggregate volume floor
        "bullish_threshold": 0.6,
        "bearish_threshold": 0.4,
        "raw_sentiment_weight": 0.4,
        "sentiment_momentum_weight": 0.6,
        "min_confidence": 0.55,
    }

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        p = self.params
        window = p["sentiment_window"]
        takes = ctx.taker_buys or []
        vols = ctx.volumes or []
        if len(takes) < window or len(vols) < window:
            return StrategySignal(0, 0, "sentiment: no order-flow data")

        def ratio(t: list[float], v: list[float]) -> float | None:
            tot = sum(v)
            if tot <= p["min_volume_per_bar"] * len(v):
                return None                    # too thin to trust (noise filter)
            return sum(t) / tot

        score = ratio(takes[-window:], vols[-window:])
        if score is None:
            return StrategySignal(0, 0, "sentiment: volume below noise floor")

        prev_t = takes[-2 * window:-window]
        prev_v = vols[-2 * window:-window]
        prev = ratio(prev_t, prev_v) if prev_t else None
        momentum = 0.0
        if prev is not None:
            momentum = score - prev

        # Original blend: score + influencer term; influencer analogue is 0.5
        # so it contributes nothing, then raw + momentum are combined by weight.
        momentum_signal = min(1.0, max(0.0, 0.5 + momentum * 5.0))
        combined = (score * p["raw_sentiment_weight"]
                    + momentum_signal * p["sentiment_momentum_weight"])

        if combined > p["bullish_threshold"]:
            confidence = min(0.95, 0.5 + (combined - p["bullish_threshold"]) * 2.0)
            if confidence < p["min_confidence"]:
                return StrategySignal(0, confidence, "sentiment: conf below minimum")
            return StrategySignal(
                1, confidence,
                f"Bullish flow: score={score:.2f}, momentum={momentum:+.3f}, "
                f"combined={combined:.2f}")

        if combined < p["bearish_threshold"]:
            confidence = min(0.95, 0.5 + (p["bearish_threshold"] - combined) * 2.0)
            if confidence < p["min_confidence"]:
                return StrategySignal(0, confidence, "sentiment: conf below minimum")
            return StrategySignal(
                -1, confidence,
                f"Bearish flow: score={score:.2f}, momentum={momentum:+.3f}, "
                f"combined={combined:.2f}")

        return StrategySignal(0, 0, f"Neutral flow: combined={combined:.2f}")
