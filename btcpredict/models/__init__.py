"""Pluggable pricing models.

The repository's original model is the closed-form digital option. This package
adds the strategy bots ported from polymarket-bot-arena (adapted to Binance):

    digital    original driftless digital option (baseline)
    momentum   short-term candle momentum + volume confirmation
    mean_rev   z-score + RSI reversion
    sentiment  taker order-flow ratio + its momentum (Binance analogue of X data)
    hybrid     weighted ensemble of momentum + mean_rev + sentiment
    phantom    EMA trend + breakout, ATR-gated
    sniper     strict momentum confirmation, trades rarely
    btc_maker  directional lean half of the arena's BTC maker bot

The arena's fee_zone_maker is deliberately absent: it is a market-making
strategy on Polymarket's taker-fee curve and contains no directional prediction
to port. meanrev_sl / meanrev_tp share mean_rev's analyzer and differ only in
exit rules that do not apply to a hold-to-settlement venue.

Every model exposes ``predict(ctx) -> ModelPred``; the orchestrator treats
``emitted=False`` as "no trade this tick".
"""
from __future__ import annotations

from btcpredict.models.base import (ModelContext, ModelPred, PriceHistory,
                                    StrategyModel, StrategySignal)
from btcpredict.models.digital import DigitalModel
from btcpredict.models.drift_digital import DriftDigitalModel
from btcpredict.models.hybrid import HybridModel
from btcpredict.models.maker import BtcMakerModel
from btcpredict.models.mean_rev import MeanRevModel
from btcpredict.models.momentum import MomentumModel
from btcpredict.models.phantom import PhantomModel
from btcpredict.models.sentiment import SentimentModel
from btcpredict.models.sniper import SniperModel

_REGISTRY: dict[str, type[StrategyModel]] = {
    "digital": DigitalModel,
    "drift_digital": DriftDigitalModel,
    "momentum": MomentumModel,
    "mean_rev": MeanRevModel,
    "sentiment": SentimentModel,
    "hybrid": HybridModel,
    "phantom": PhantomModel,
    "sniper": SniperModel,
    "btc_maker": BtcMakerModel,
}

MODEL_NAMES = tuple(_REGISTRY)


def build_model(name: str, params: dict | None = None) -> StrategyModel:
    """Instantiate a model by name. Raises ValueError for unknown names."""
    key = (name or "digital").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(f"unknown pricing model '{name}'; choose from {MODEL_NAMES}")
    return _REGISTRY[key](params)


__all__ = [
    "ModelContext", "ModelPred", "PriceHistory", "StrategyModel", "StrategySignal",
    "MODEL_NAMES", "build_model",
    "DigitalModel", "MomentumModel", "MeanRevModel", "SentimentModel",
    "DriftDigitalModel",
    "HybridModel", "PhantomModel", "SniperModel", "BtcMakerModel",
]
