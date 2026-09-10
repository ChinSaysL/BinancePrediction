"""Drift-adjusted digital model (prototype).

The baseline digital model is driftless:

    P(Up) = Phi( ln(S/K) / (sigma * sqrt(tau)) )

This prototype adds an estimated short-term drift mu_1m:

    P(Up) = Phi( (ln(S/K) + drift_scale * mu_1m * tau_min)
                 / (sigma_1m * sqrt(tau_min)) )

mu_1m blends two momentum estimators:
  - realized: the round's own move so far, per minute (continuation)
  - recent:   mean log return over the last `drift_bars` completed 1m bars

`drift_scale` shrinks the drift toward zero -- estimated drift is noisy and
the whole point of the experiment is to see how much of it is tradeable.
The scale IS the haircut, applied to the drift instead of to the edge.

Why this might fix Up: the venue's Up quotes appear to embed momentum that the
driftless model cannot see, so the driftless model's Up disagreement is
anti-informative. If our model sees the same momentum, its Up disagreement
should become informative again (and its Down disagreement should shrink).
"""
from __future__ import annotations

import math

from btcpredict.models.base import (ModelContext, ModelPred, StrategyModel,
                                    StrategySignal, clamp_p)
from btcpredict.pricing.digital import norm_cdf


class DriftDigitalModel(StrategyModel):
    name = "drift_digital"

    DEFAULT_PARAMS = {
        "drift_bars": 6,       # completed 1m bars for the recent estimator
        "drift_scale": 0.5,    # how much of the blended drift to trust
        "blend_realized": 0.5,  # weight on intra-round realized drift
    }

    def _drift_per_min(self, ctx: ModelContext) -> float:
        p = self.params
        # Recent-bar drift, from completed bars only: the context's last
        # element is the live tick, so drop it before differencing.
        bars = ctx.closes[:-1] if len(ctx.closes) >= 2 else ctx.closes
        recent = 0.0
        if len(bars) >= 2:
            rets = [math.log(bars[i] / bars[i - 1])
                    for i in range(1, len(bars))
                    if bars[i] > 0 and bars[i - 1] > 0]
            k = max(1, int(p["drift_bars"]))
            if rets:
                recent = sum(rets[-k:]) / len(rets[-k:])
        # Realized intra-round drift: ln(S/K) over the elapsed part of a 300s
        # round, expressed per minute. Guards against a wild first tick.
        realized = 0.0
        if (ctx.price and ctx.strike and ctx.tau_s is not None
                and ctx.price > 0 and ctx.strike > 0):
            elapsed_s = 300.0 - ctx.tau_s
            if 10.0 <= elapsed_s <= 290.0:
                realized = (math.log(ctx.price / ctx.strike)
                            / (elapsed_s / 60.0))
        return (p["blend_realized"] * realized
                + (1.0 - p["blend_realized"]) * recent)

    def predict(self, ctx: ModelContext) -> ModelPred:
        if (ctx.price is None or ctx.strike is None or ctx.tau_s is None
                or ctx.sigma_1m is None):
            return ModelPred(0.5, False, "drift_digital: missing inputs")
        if ctx.price <= 0 or ctx.strike <= 0 or ctx.sigma_1m <= 0:
            return ModelPred(0.5, False, "drift_digital: bad price/sigma")
        if ctx.tau_s <= 0:
            p = 1.0 if ctx.price > ctx.strike else 0.0
            return ModelPred(clamp_p(p), True, "drift_digital: settled")
        tau_min = ctx.tau_s / 60.0
        mu = self._drift_per_min(ctx)
        m = math.log(ctx.price / ctx.strike)
        z = ((m + self.params["drift_scale"] * mu * tau_min)
             / (ctx.sigma_1m * math.sqrt(tau_min)))
        p = norm_cdf(z)
        return ModelPred(clamp_p(p), True, f"drift_digital mu={mu:+.5f}")

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        pred = self.predict(ctx)
        side = 1 if pred.p_up > 0.5 else (-1 if pred.p_up < 0.5 else 0)
        return StrategySignal(side, min(0.95, abs(pred.p_up - 0.5) * 2.0),
                              pred.reason)
