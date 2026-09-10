"""Turn a model probability and a market quote into a trade decision.

Buying `shares = stake / q` of an outcome quoted at q pays $1 per share if it
wins. With fee rate f the expected value of a `stake` bet is:

    fee on notional : EV = p*(stake/q) - stake - f*stake
                      breakeven  p > q * (1 + f)
    fee on profit   : EV = p*(stake/q - stake)*(1-f) - (1-p)*stake
                      breakeven  p > q / (1 - f + f*q)

The notional model is the stricter of the two at every q, so it is the default
while the real fee semantics are unconfirmed. Guessing the looser one would
manufacture edge that does not exist.

On top of the fee the quote must also cover the effective half-spread (the
displayed price is not executable -- there is no public book) and a safety
margin. Together these form the `hurdle` the raw edge has to clear.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# Measured fee curve (61 live quotes, px 0.01-0.90):
#     fee_bps = 180 * min(1, (1 - px) / px)
# i.e. 1.8% of potential winnings, capped at 1.8% of notional below px 0.50.
# Fits every sample: 0.51->174, 0.73->67, 0.83->37, 0.89->23.
# A flat rate is wrong in both directions -- far too low for cheap contracts
# (180 bps, not 64) and too high for favourites (~21 bps at 0.90).
FEE_MAX_BPS = 180.0


def fee_bps_for_price(px: float) -> float:
    if px <= 0.0:
        return FEE_MAX_BPS
    return min(FEE_MAX_BPS, FEE_MAX_BPS * (1.0 - px) / px)


def fee_rate_for_price(px: float) -> float:
    return fee_bps_for_price(px) / 10_000.0


class FeeModel(str, Enum):
    NOTIONAL = "notional"
    PROFIT = "profit"


@dataclass(slots=True)
class Signal:
    side: str | None          # "Up" / "Down" / None
    fair: float               # model probability for that side
    quote: float              # market price for that side
    edge: float               # fair - quote
    hurdle: float             # what edge had to beat
    ev_per_dollar: float      # expected profit per $1 staked
    act: bool
    reason: str
    fair_up_model: float | None = None  # raw P(Up) that produced it (rechecks)

    def __str__(self) -> str:
        if not self.act:
            return f"NO-TRADE ({self.reason})"
        return (f"{self.side} fair={self.fair:.3f} quote={self.quote:.3f} "
                f"edge={self.edge:+.3f} hurdle={self.hurdle:.3f} "
                f"ev/$={self.ev_per_dollar:+.4f}")


def expected_value(p: float, q: float, fee: float,
                   model: FeeModel = FeeModel.NOTIONAL) -> float:
    """Expected profit per $1 staked. Negative means the bet loses money."""
    if not 0.0 < q < 1.0:
        return -1.0
    if model is FeeModel.NOTIONAL:
        return p / q - 1.0 - fee
    return p * (1.0 / q - 1.0) * (1.0 - fee) - (1.0 - p)


def breakeven_prob(q: float, fee: float,
                   model: FeeModel = FeeModel.NOTIONAL) -> float:
    if model is FeeModel.NOTIONAL:
        return q * (1.0 + fee)
    return q / (1.0 - fee + fee * q)


def evaluate(
    fair_up: float,
    quote_up: float,
    quote_down: float,
    *,
    fee: float | None = None,
    half_spread: float,
    safety_margin: float,
    fee_model: FeeModel = FeeModel.NOTIONAL,
    min_ev: float = 0.0,
    max_quote: float = 0.90,
    min_quote: float = 0.0,
    min_edge: float = 0.0,
    edge_haircut: float = 1.0,
) -> Signal:
    """Pick the better side, if either clears its hurdle.

    Both sides are evaluated independently rather than assuming they are
    complementary: quotes are quantized to 0.01 and need not sum to exactly 1.

    `edge_haircut` scales the model's disagreement with the venue before any
    of it is believed: p_used = q + haircut * (p_model - q). Measured on 31h of
    recorded btc-5m quotes, only part of the claimed edge is ever realised, and
    the shortfall GROWS with the claim -- at a claimed edge of 0.03-0.06 the
    realised edge was +0.005, at 0.09-0.12 it was +0.064 against a claim of
    ~0.10. A flat `safety_margin` cannot express that, because the error is
    proportional to the claim rather than constant. The haircut feeds the EV
    and therefore Kelly as well as the gate: sizing off an inflated probability
    overbets, which is the expensive half of the mistake.

    1.0 disables it. See config.edge_haircut for why the default is not fitted.
    """
    fair_down = 1.0 - fair_up
    candidates = []
    for side, p_model, q in (("Up", fair_up, quote_up),
                             ("Down", fair_down, quote_down)):
        # A missing quote (one side of the book empty) is not tradeable, and must
        # not raise -- callers pass straight through from feed data.
        if q is None or not 0.0 < q < 1.0:
            continue
        # Believe only `edge_haircut` of the disagreement with the venue.
        p = q + edge_haircut * (p_model - q)
        # Crossing the spread means paying above the displayed price.
        q_eff = min(0.999, q + half_spread)
        # The venue's fee depends on the price paid, so it is derived per
        # candidate rather than taken as one flat number for both sides.
        f = fee if fee is not None else fee_rate_for_price(q_eff)
        ev = expected_value(p, q_eff, f, fee_model)
        hurdle = breakeven_prob(q_eff, f, fee_model) - q + safety_margin
        candidates.append((ev, side, p, q, hurdle))

    if not candidates:
        return Signal(None, fair_up, quote_up, 0.0, 0.0, -1.0, False,
                      "no valid quote", fair_up_model=fair_up)

    # Refuse deep favourites. At q=0.98 a $1 bet wins $0.02 and needs ~98.6%
    # accuracy to break even, but measured reliability in the top bucket was
    # 0.964 actual against 0.973 predicted -- the model's confidence out there
    # is worth less than the payoff asymmetry demands, so these are -EV even
    # when the arithmetic edge looks positive.
    priced_out = [c for c in candidates if not (min_quote <= c[3] <= max_quote)]
    candidates = [c for c in candidates if min_quote <= c[3] <= max_quote]
    if not candidates:
        q = min(c[3] for c in priced_out)
        side = "above max_quote" if q > max_quote else "below min_quote"
        return Signal(None, fair_up, quote_up, 0.0, 0.0, -1.0, False,
                      f"quote {q:.2f} {side} [{min_quote:.2f},{max_quote:.2f}]",
                      fair_up_model=fair_up)

    ev, side, p, q, hurdle = max(candidates, key=lambda c: c[0])
    edge = p - q
    act = ev > min_ev and edge > hurdle and edge >= min_edge
    reason = "ok" if act else (
        f"ev {ev:+.4f} <= {min_ev:.4f}" if ev <= min_ev
        else f"edge {edge:+.3f} < min_edge {min_edge:.3f}" if edge < min_edge
        else f"edge {edge:+.3f} <= hurdle {hurdle:.3f}"
    )
    return Signal(side, p, q, edge, hurdle, ev, act, reason,
                  fair_up_model=fair_up)


def evaluate_hybrid(fair_up_down_model: float, fair_up_up_model: float,
                    quote_up: float, quote_down: float, **kwargs) -> Signal:
    """Per-side gate: Down candidates priced by the down model, Up by the up model.

    evaluate() runs once per model probability; the down model's Up call and
    the up model's Down call are discarded, then the higher-EV acting signal
    wins. With identical probabilities this reduces to plain evaluate().
    """
    if fair_up_down_model == fair_up_up_model:
        return evaluate(fair_up_down_model, quote_up, quote_down, **kwargs)
    down_sig = evaluate(fair_up_down_model, quote_up, quote_down, **kwargs)
    up_sig = evaluate(fair_up_up_model, quote_up, quote_down, **kwargs)
    cands = []
    if down_sig.act and down_sig.side == "Down":
        cands.append(down_sig)
    if up_sig.act and up_sig.side == "Up":
        cands.append(up_sig)
    if not cands:
        best = (down_sig if down_sig.ev_per_dollar >= up_sig.ev_per_dollar
                else up_sig)
        return Signal(None, best.fair, best.quote, best.edge, best.hurdle,
                      best.ev_per_dollar, False, best.reason)
    return max(cands, key=lambda s: s.ev_per_dollar)


def kelly_stake(p: float, q: float, bankroll: float, fraction: float,
                min_stake: float, max_stake: float) -> float:
    """Fractional Kelly for a binary payout, clamped to the allowed range.

    Full Kelly on odds b = (1/q - 1) is f* = (p*(1+b) - 1) / b.
    """
    if not 0.0 < q < 1.0 or p <= q:
        return 0.0
    b = (1.0 / q) - 1.0
    if b <= 0:
        return 0.0
    f_star = (p * (1.0 + b) - 1.0) / b
    stake = max(0.0, f_star) * fraction * bankroll
    if stake < min_stake:
        return 0.0
    return min(stake, max_stake)
