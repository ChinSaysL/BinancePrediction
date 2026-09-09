"""Hybrid / ensemble model.

Port of polymarket-bot-arena ``bots/bot_hybrid.py``: weighted combination of the
momentum, mean-reversion and sentiment sub-strategies, with an agreement bonus
when at least two of them point the same way.
"""
from __future__ import annotations

from btcpredict.models.base import ModelContext, StrategyModel, StrategySignal
from btcpredict.models.mean_rev import MeanRevModel
from btcpredict.models.momentum import MomentumModel
from btcpredict.models.sentiment import SentimentModel


class HybridModel(StrategyModel):
    name = "hybrid"
    DEFAULT_PARAMS = {
        "momentum_weight": 0.35,
        "mean_rev_weight": 0.35,
        "sentiment_weight": 0.30,
        "confidence_threshold": 0.55,
        "agreement_bonus": 0.15,
        "min_confidence": 0.5,
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        # Sub-analyzers, exactly like the arena's internal bots.
        self._momentum = MomentumModel()
        self._mean_rev = MeanRevModel()
        self._sentiment = SentimentModel()

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        p = self.params
        subs = [
            (self._momentum.analyze(ctx), p["momentum_weight"]),
            (self._mean_rev.analyze(ctx), p["mean_rev_weight"]),
            (self._sentiment.analyze(ctx), p["sentiment_weight"]),
        ]

        weighted_score = 0.0
        reasons: list[str] = []
        yes_votes = no_votes = 0
        for sig, weight in subs:
            if sig.side == 0:
                continue
            weighted_score += sig.side * sig.confidence * weight
            yes_votes += sig.side > 0
            no_votes += sig.side < 0
            reasons.append(sig.reason[:60])

        if yes_votes == 0 and no_votes == 0:
            return StrategySignal(0, 0, "hybrid: all sub-strategies hold")

        agreement = max(yes_votes, no_votes) >= 2
        confidence = min(0.95, abs(weighted_score) + (p["agreement_bonus"] if agreement else 0.0))

        if confidence < p["confidence_threshold"]:
            return StrategySignal(
                0, confidence,
                f"hybrid: ensemble confidence {confidence:.2f} below "
                f"{p['confidence_threshold']}")

        side = 1 if weighted_score > 0 else -1
        return StrategySignal(
            side, confidence,
            f"hybrid ({yes_votes}Y/{no_votes}N, agree={agreement}): "
            + " | ".join(reasons))
