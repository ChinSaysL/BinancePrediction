"""Market-anchored recalibration of the Up probability ("calibrated").

Why this exists
---------------
The Up side of this book does not pay, and the reason is the probability, not
the threshold. Measured on the recorded BTC rounds (670 settled rounds, one
first-qualifying tick per round, clustered):

  * the venue's own Up ask is a BETTER Up probability than our closed form --
    Brier 0.1550 / 0.1503 (train / test) against the digital model's
    0.1572 / 0.1521. We are adding error to a price that was already right;
  * our error has a shape: on the 50 rounds where the gate would buy Up
    (q ~ 0.23) the model claimed 0.383 and reality was 0.220 -- 16 points too
    confident (t = 2.8) -- while on expensive Up (q ~ 0.80) it claimed 0.846
    and reality was 0.902, i.e. too TIMID;
  * every threshold that tried to exploit that (per-side edge bar, price cap,
    price floor, probability shift) helped in one dataset and hurt in the
    other, which is the signature of fitting noise.

So this model does not add a new alpha signal and it does not filter anything.
It repairs the probability: it keeps the venue's price as the ANCHOR, so it can
never claim an edge the market does not already show, and adds only the part of
our disagreement with the venue that the recording says is real.

    logit(p_up) = b_market * logit(q) + b_disc * d + b_imb * imb

    q      the venue's Up ask (the anchor)
    d      logit(p_digital) - logit(q), the disagreement we are trying to
           spend -- b_disc is the fraction of it that survives measurement
    imb    taker-buy share of volume over the last 5 completed minutes, minus
           0.5: order-flow pressure, the only live microstructure feature in
           the context

There is deliberately NO intercept and no separate moneyness (z) or tau term.
Two fits taught that:
  * moneyness cannot be a separate column: the digital probability IS a
    deterministic function of z, so logit(p_digital), z and the disagreement
    are one column three ways. Fitted together they are unidentified -- the
    first attempt returned b_market = -1.32 with an se of 3.54, a fit that
    describes nothing;
  * a free intercept + tau are jointly unidentified (both only shift the
    level) and the fitted pair put the model 2.5 points BELOW the venue at
    q = 0.5, i.e. it invented a permanent Down bias out of pure noise. Anchored
    with no intercept the model is symmetric by construction: at q = 0.5 with
    no disagreement and balanced flow it returns exactly 0.5.

So the model claims exactly one thing: the venue's level is the level, and the
only question is how much of our disagreement and of order flow to add.

Coefficients come from tools.train_up_model, fitted on the first 60% of
recorded rounds and scored on the last 40%. They are pasted in below rather
than loaded from a file so that what runs live is what was reviewed, and so a
missing file can never silently change the pricing. If `quote_up` is absent
(no book, paper mode, a recorded tick without an ask) the model falls back to
the closed-form digital value instead of guessing.
"""
from __future__ import annotations

import math

from btcpredict.models.base import (ModelContext, ModelPred, StrategyModel,
                                    StrategySignal, clamp_p)
from btcpredict.pricing.digital import fair_value

_P_EPS = 1e-6
_IMB_BARS = 5


def taker_imbalance(taker_buys, volumes, k: int = _IMB_BARS) -> float:
    """Taker-buy share of traded volume over the last `k` completed bars, -0.5.

    0 means buyers and sellers were balanced, +0.5 means every trade was a
    taker buy. Uses only completed bars, so live and replay agree.
    """
    if not taker_buys or not volumes:
        return 0.0
    tb = list(taker_buys)[-k:]
    vol = list(volumes)[-k:]
    tot = sum(v for v in vol if v and v > 0)
    if tot <= 0:
        return 0.0
    buy = sum(v for v in tb if v and v > 0)
    return max(-0.5, min(0.5, buy / tot - 0.5))


def _logit(p: float) -> float:
    p = max(_P_EPS, min(1.0 - _P_EPS, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def features(ctx: ModelContext) -> dict[str, float] | None:
    """The Up-side feature vector, or None when the context cannot support one.

    Exported so tools.train_up_model fits exactly the features predict() uses;
    a trainer that re-derives them would eventually fit something else.
    """
    return features_from_scalars(
        price=ctx.price, strike=ctx.strike, tau_s=ctx.tau_s,
        sigma_1m=ctx.sigma_1m, quote_up=ctx.quote_up,
        imb=taker_imbalance(ctx.taker_buys, ctx.volumes))


def features_from_scalars(*, price, strike, tau_s, sigma_1m, quote_up,
                          imb: float) -> dict[str, float] | None:
    """The same features, from recorded scalars instead of a live context."""
    if (price is None or strike is None or tau_s is None or sigma_1m is None
            or quote_up is None or price <= 0 or strike <= 0 or sigma_1m <= 0
            or tau_s <= 0):
        return None
    return _feat(quote_up, clamp_p(fair_value(price, strike, tau_s,
                                              sigma_1m)), imb)


def features_down_from_scalars(*, price, strike, tau_s, sigma_1m, quote_down,
                               imb: float) -> dict[str, float] | None:
    """The Down-side features: same shape, the Down quote and Down closed form.

    The gate derives the Down candidate from (1 - fair_up), so a model that
    prices Down must use the down side's own disagreement, not the up side's.
    """
    if (price is None or strike is None or tau_s is None or sigma_1m is None
            or quote_down is None or price <= 0 or strike <= 0 or sigma_1m <= 0
            or tau_s <= 0):
        return None
    p_up_dig = clamp_p(fair_value(price, strike, tau_s, sigma_1m))
    return _feat(quote_down, 1.0 - p_up_dig, imb)


def _feat(quote, p_side_digital: float, imb: float) -> dict[str, float] | None:
    """market/disc/imb for one side, given that side's closed-form value."""
    q = quote
    if q is None or not 0.0 < q < 1.0 or p_side_digital is None:
        return None
    lq = _logit(q)
    return {
        "market": lq,
        "disc": _logit(p_side_digital) - lq,
        "imb": imb,
    }


# Fitted by tools.train_up_model on the first 60% of the recorded BTC rounds
# (384 of 673 rows, cut 2026-09-10T11:40Z), scored on the unseen 250 rows.
#   b_market 1.2368 +- 0.1529 (z 8.09)  the venue's level, amplified: it is
#                                       systematically too tame -- at q = 0.5 it
#                                       is right, and the further q gets from
#                                       0.5 the more it understates the
#                                       favourite (the classic favourite-
#                                       longshot bias, z 2.41 against b = 1
#                                       when fitted on both sides)
#   b_disc   0.1033 +- 0.3565 (z 0.29)  our disagreement with the venue is worth
#                                       ~10% of what we claim, i.e. nothing.
#                                       This is the whole Up story.
#   b_imb    0.3965 +- 0.7709 (z 0.51)  taker order flow adds nothing measurable
# See FINDINGS.md. Test-slice Brier 0.1921 against the venue's own 0.1927 and
# the digital model's 0.1940 -- better than what we run, level with the market.
DEFAULT_PARAMS: dict = {
    "b_market": 1.236789,
    "b_disc": 0.103309,
    "b_imb": 0.396481,
}

# Fitted the same way on the DOWN side of the same 674 rounds (384 train / 250
# test):
#   b_market 1.2427 +- 0.1523 (z 8.16)
#   b_disc   0.0570 +- 0.3642 (z 0.16)   the Down side's disagreement is also
#                                        worth nothing
#   b_imb   -0.3769 +- 0.7715 (z -0.49)
# As a probability it is the best of the three (test Brier 0.1919 against the
# venue's 0.1925 and digital's 0.1937). It is deliberately left UNFITTED here
# because it failed the money test, which is the test that decides:
#   * at the live min_edge of 0.12 it takes ZERO Down trades -- an honest
#     probability never claims a 12-point edge on a 5-minute binary, so
#     shipping it would simply stop the book (+$50.16 live, +$18.15 on the
#     train slice);
#   * with the bar lowered to 0.02 it trades 245 unseen rounds for +0.013/$1
#     (t 0.32) and 363 train rounds for +0.008 (t 0.23) -- the right sign on
#     both slices, but an edge of 1.3% per $1 staked that would need ~10x the
#     trades to demonstrate (about a month of running).
# See FINDINGS.md. Live does not select this model; it exists so the honest
# pricing can be run forward in paper (see tools/ab_report.py for the readout)
# against the live book, instead of being argued about on 674 rounds.
DOWN_PARAMS: dict = {
    "b_market": 1.242673,
    "b_disc": 0.056951,
    "b_imb": -0.376878,
}


class CalibratedUpModel(StrategyModel):
    """Recalibrated P(Up): market anchor plus the measured part of our edge."""

    name = "calibrated"
    DEFAULT_PARAMS = DEFAULT_PARAMS

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        p = self.predict(ctx)
        if not p.emitted:
            return StrategySignal(0, 0, p.reason)
        side = 1 if p.p_up > 0.5 else (-1 if p.p_up < 0.5 else 0)
        return StrategySignal(side, min(0.95, abs(p.p_up - 0.5) * 2.0),
                              p.reason)

    def predict(self, ctx: ModelContext) -> ModelPred:
        f = features(ctx)
        if f is None:
            # No anchor, no model: fall back to the closed form rather than
            # inventing a probability the venue cannot be compared against.
            if (ctx.price is None or ctx.strike is None or ctx.tau_s is None
                    or ctx.sigma_1m is None):
                return ModelPred(0.5, False, "calibrated: missing inputs")
            p = (1.0 if ctx.price > ctx.strike else 0.0) if ctx.tau_s <= 0 \
                else fair_value(ctx.price, ctx.strike, ctx.tau_s, ctx.sigma_1m)
            return ModelPred(clamp_p(p), True,
                             "calibrated: no quote, closed form")

        par = self.params
        x = (par.get("b_market", 0.0) * f["market"]
             + par.get("b_disc", 0.0) * f["disc"]
             + par.get("b_imb", 0.0) * f["imb"])
        return ModelPred(clamp_p(_sigmoid(x)), True, "calibrated")


class CalibratedDownModel(StrategyModel):
    """The symmetric recalibration, for the Down side.

    The gate prices the Down candidate as (1 - fair_up), so this returns
    `1 - p_down_calibrated`: the number the gate will use for Down is then
    exactly the calibrated Down probability. Its own Up call is discarded by
    evaluate_hybrid (the Down model's Up side has never been used), which is
    the same contract DigitalModel already lives under.

    This exists because the Down side is where the money is and it has never
    seen its own market price: every live Down trade so far was priced by an
    implied complement, not by the venue's ask for Down.
    """

    name = "calibrated_down"
    DEFAULT_PARAMS = DOWN_PARAMS

    def analyze(self, ctx: ModelContext) -> StrategySignal:
        p = self.predict(ctx)
        if not p.emitted:
            return StrategySignal(0, 0, p.reason)
        side = 1 if p.p_up > 0.5 else (-1 if p.p_up < 0.5 else 0)
        return StrategySignal(side, min(0.95, abs(p.p_up - 0.5) * 2.0),
                              p.reason)

    def predict(self, ctx: ModelContext) -> ModelPred:
        p_up_dig = None
        if (ctx.price is not None and ctx.strike is not None
                and ctx.tau_s is not None and ctx.sigma_1m is not None):
            p_up_dig = (1.0 if ctx.price > ctx.strike else 0.0) \
                if ctx.tau_s <= 0 else fair_value(ctx.price, ctx.strike,
                                                  ctx.tau_s, ctx.sigma_1m)
            p_up_dig = clamp_p(p_up_dig)
        if p_up_dig is None:
            return ModelPred(0.5, False, "calibrated_down: missing inputs")
        f = features_down_from_scalars(
            price=ctx.price, strike=ctx.strike, tau_s=ctx.tau_s,
            sigma_1m=ctx.sigma_1m, quote_down=ctx.quote_down,
            imb=taker_imbalance(ctx.taker_buys, ctx.volumes))
        if f is None:
            # No Down quote, no anchor: fall back to the closed form, which is
            # exactly what p_up = 1 - p_up_digital implies for the Down side.
            return ModelPred(clamp_p(1.0 - p_up_dig), True,
                             "calibrated_down: no quote, closed form")
        par = self.params
        if not par:
            # Not fitted: behave exactly like the digital model. Returning a
            # constant here would price Down at 0.5 and kill every Down trade.
            return ModelPred(clamp_p(1.0 - p_up_dig), True,
                             "calibrated_down: unfitted, closed form")
        x = (par.get("b_market", 0.0) * f["market"]
             + par.get("b_disc", 0.0) * f["disc"]
             + par.get("b_imb", 0.0) * f["imb"])
        return ModelPred(clamp_p(1.0 - _sigmoid(x)), True, "calibrated_down")
