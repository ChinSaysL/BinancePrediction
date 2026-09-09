"""Digital-option model, wrapped in the StrategyModel interface.

This is the repository's original pricing model (see pricing/digital.py), so it
can compete head-to-head with the ported strategy models on the same interface.
"""
from __future__ import annotations

from btcpredict.models.base import (ModelContext, ModelPred, StrategyModel,
                                    StrategySignal, clamp_p)
from btcpredict.pricing.digital import fair_value


class DigitalModel(StrategyModel):
    name = "digital"

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        # Digital is a fair-value model: predict() below builds the probability
        # directly from price/strike/tau/sigma.
        if (ctx.price is None or ctx.strike is None or ctx.tau_s is None
                or ctx.sigma_1m is None):
            return StrategySignal(0, 0, "digital: missing price/strike/tau/sigma")
        p = self.predict(ctx)
        side = 1 if p.p_up > 0.5 else (-1 if p.p_up < 0.5 else 0)
        return StrategySignal(side, min(0.95, abs(p.p_up - 0.5) * 2.0), p.reason)

    def predict(self, ctx: ModelContext) -> ModelPred:
        if (ctx.price is None or ctx.strike is None or ctx.tau_s is None
                or ctx.sigma_1m is None):
            return ModelPred(0.5, False, "digital: missing price/strike/tau/sigma")
        if ctx.tau_s <= 0:
            p = 1.0 if ctx.price > ctx.strike else 0.0
        else:
            p = fair_value(ctx.price, ctx.strike, ctx.tau_s, ctx.sigma_1m)
        return ModelPred(clamp_p(p), True, "digital")
