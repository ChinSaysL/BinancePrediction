"""BTC maker directional-lean model.

The arena's ``bots/bot_btc_maker.py`` quotes both sides of the Polymarket book
and computes a fair value by blending the *market price* with a BTC momentum
lean: ``fair = market_price + directional_weight * momentum``. A Binance port
has no Polymarket price to blend against, so the neutral anchor becomes 0.5 --
this is the pure directional half of the original strategy, isolated from its
market-making half. With the default 0.4 weight the model barely leaves 0.5,
which is the honest reading: a maker's edge is spread, not direction.
"""
from __future__ import annotations

from btcpredict.models.base import (ModelContext, ModelPred, StrategyModel,
                                    StrategySignal, clamp_p)


class BtcMakerModel(StrategyModel):
    name = "btc_maker"
    DEFAULT_PARAMS = {
        "lookback_candles": 3,     # candles used for the directional lean
        "directional_weight": 0.4, # 0 = pure neutral, 1 = full directional
        "momentum_cap": 0.01,      # clamp momentum to +/-1%, as the original
        "momentum_scale": 25.0,    # p = 0.5 + dw * clamp(mom) * scale
    }

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        # BtcMaker is a fair-value model, not a selective signal: predict()
        # below builds the probability directly from the momentum lean.
        p = self.params
        prices = ctx.closes
        lb = p["lookback_candles"]
        if len(prices) < lb or prices[-lb] <= 0:
            return StrategySignal(0, 0, "btc_maker: insufficient data")
        momentum = (prices[-1] - prices[-lb]) / prices[-lb]
        momentum = max(-p["momentum_cap"], min(p["momentum_cap"], momentum))
        fair = 0.5 + p["directional_weight"] * momentum * p["momentum_scale"]
        side = 1 if fair > 0.5 else (-1 if fair < 0.5 else 0)
        return StrategySignal(side, min(0.95, abs(fair - 0.5) * 2.0),
                              f"maker lean mom={momentum:+.4f}")

    def predict(self, ctx: ModelContext) -> ModelPred:
        p = self.params
        prices = ctx.closes
        lb = p["lookback_candles"]
        if len(prices) < lb or prices[-lb] <= 0:
            return ModelPred(0.5, False, "btc_maker: insufficient data")
        momentum = (prices[-1] - prices[-lb]) / prices[-lb]
        momentum = max(-p["momentum_cap"], min(p["momentum_cap"], momentum))
        fair = 0.5 + p["directional_weight"] * momentum * p["momentum_scale"]
        return ModelPred(
            clamp_p(fair), True,
            f"maker: mom={momentum:+.4f} dw={p['directional_weight']} "
            f"fair={fair:.3f}")
