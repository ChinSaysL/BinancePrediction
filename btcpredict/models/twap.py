"""The Polymarket TWAP model, in the `StrategyModel` interface.

WHY THIS IS HERE AND NOT IN `venues/polymarket/pricing.py`
----------------------------------------------------------
The venue package holds the venue's PRIMITIVES (discovery, book, orders, and
the terminal distribution) and must not depend on the model layer. Putting the
adapter in `pricing.py` created a real import cycle: importing
`venues.polymarket.pricing` first pulled in `btcpredict.models`, whose package
`__init__` imports back into `pricing` before `TwapModel` exists --
`ImportError: cannot import name 'TwapModel' from partially initialized module`.
It only failed when the VENUE module was imported first, so a test that started
from `btcpredict.models` passed and hid it.

Layering: `pricing.py` = the distribution (math, no models), this = the
adapter (models, no venue state). `models/__init__.py` imports this in the
normal direction, so both import orders work.
"""
from __future__ import annotations

from btcpredict.models.base import (ModelContext, ModelPred, StrategyModel,
                                    StrategySignal, clamp_p)
from btcpredict.venues.polymarket.pricing import FULL, TAIL, fair_value


class TwapModel(StrategyModel):
    """`P(Up)` from the Polymarket terminal distribution.

    Registered as `"twap"`, so `pricing_model=twap` selects it. A fair-value
    model: `analyze()` reports a side and how far the fair value sits from a
    coin flip, `predict()` returns the probability itself -- the same split
    `DigitalModel` uses, so the two are interchangeable under `evaluate()`.

    The point rule (`digital`) and this are NOT interchangeable on Polymarket:
    they price different random variables, and the two venues disagree on 10.4%
    of settled rounds. `settings.venue` picks the bet; this picks the model for
    it.

    `params["window"]` selects `TAIL` (the measured rule) or `FULL`.
    `FULL` additionally needs `params["round_seconds"]` and fails CLOSED
    without it (`emitted=False`) rather than assuming a 5-minute round -- a
    silent 300 s default would price a 15m round as a 5m one.
    """

    name = "twap"
    DEFAULT_PARAMS = {"window": TAIL, "round_seconds": None}

    def _p_up(self, ctx: ModelContext) -> tuple[float, bool] | None:
        if (ctx.price is None or ctx.strike is None or ctx.tau_s is None
                or ctx.sigma_1m is None):
            return None
        return fair_value(ctx.price, ctx.strike, ctx.tau_s, ctx.sigma_1m,
                          window_log_int=ctx.window_log_int,
                          window=self.params.get("window", TAIL),
                          round_seconds=self.params.get("round_seconds"))

    def predict(self, ctx: ModelContext) -> ModelPred:
        w = self.params.get("window", TAIL)
        if (w or TAIL).strip().lower() == FULL \
                and not self.params.get("round_seconds"):
            return ModelPred(0.5, False, "twap:full needs round_seconds")
        got = self._p_up(ctx)
        if got is None:
            return ModelPred(0.5, False, "twap: missing price/strike/tau/sigma")
        p, assumed = got
        # "assumed" is not cosmetic: a price off the flat-window assumption is
        # a price off a guess, and the ledger has to be able to tell the two
        # apart. It is False whenever the window has not opened yet, because
        # then there is no realised part to assume.
        return ModelPred(clamp_p(p), True,
                         f"twap:{w}" + (" flat" if assumed else " J"))

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        got = self._p_up(ctx)
        if got is None:
            return StrategySignal(0, 0.0, "twap: missing price/strike/tau/sigma")
        p, _ = got
        side = 1 if p > 0.5 else (-1 if p < 0.5 else 0)
        return StrategySignal(side, min(0.95, abs(p - 0.5) * 2.0),
                              self.predict(ctx).reason)


__all__ = ["TwapModel"]
