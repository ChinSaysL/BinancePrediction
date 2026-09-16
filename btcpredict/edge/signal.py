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
# Re-measured 2026-09-12 on the 311 live fills that actually paid a fee,
# bucketed by price (charged bps of stake against this constant):
#   209.9 at px < 0.50     175.1 at 0.50-0.60   116.0 at 0.60-0.70
#    72.7 at 0.70-0.80      38.1 at 0.80-0.90    19.8 at 0.90+
# The SHAPE is confirmed -- the fee falls as (1-px)/px -- but the LEVEL was
# 15-21% low in every single band, and the venue reports its own rate as
# fee_rate_bps=200 on 278,011 of 278,929 recorded ticks. So the coefficient is
# the venue's own number, not a hand-fitted 180. Callers that have the round's
# reported rate (main does) should pass it through as `fee=`.
FEE_MAX_BPS = 200.0

# When a fraction-based stake is floored to the venue minimum, the floor is
# only allowed while it stays under this share of the bankroll. $1 is 10% of
# $10 (trade) and 50% of $2 (do not).
FLOOR_MAX_BANKROLL_FRACTION = 0.25


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
    ev_per_dollar: float      # expected profit per $1 staked; NaN if not priced
    act: bool
    reason: str
    fair_up_model: float | None = None  # raw P(Up) that produced it (rechecks)
    # False when ev_per_dollar is NaN because nothing could be priced. The value
    # is not a return then, and must not be logged, compared or stored as one.
    ev_valid: bool = True

    def __str__(self) -> str:
        if not self.act:
            return f"NO-TRADE ({self.reason})"
        ev = f"{self.ev_per_dollar:+.4f}" if self.ev_valid else "n/a"
        return (f"{self.side} fair={self.fair:.3f} quote={self.quote:.3f} "
                f"edge={self.edge:+.3f} hurdle={self.hurdle:.3f} "
                f"ev/$={ev}")


def expected_value(p: float, q: float, fee: float,
                   model: FeeModel = FeeModel.NOTIONAL) -> float:
    """Expected profit per $1 staked. Negative means the bet loses money.

    An out-of-range quote returns NaN, NOT -1.0. The old -1.0 sentinel was
    indistinguishable from a genuine "-100% per dollar" outcome -- and it was
    read exactly that way, from this repo's own log: 198
    `pre-trade check ok ... ev/$=-1.0000` lines were taken as evidence that an
    EV gate had approved a -1.0 trade. It had not; the value only ever meant
    "no priceable quote". NaN also fails every `>`/`<` comparison an EV gate
    performs, so a degenerate value cannot pass one by accident. Callers that
    need to tell the two apart must consult `Signal.ev_valid`.
    """
    if not 0.0 < q < 1.0:
        return float("nan")
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
        return Signal(None, fair_up, quote_up, 0.0, 0.0, float("nan"), False,
                      "no valid quote", fair_up_model=fair_up, ev_valid=False)

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
        return Signal(None, fair_up, quote_up, 0.0, 0.0, float("nan"), False,
                      f"quote {q:.2f} {side} [{min_quote:.2f},{max_quote:.2f}]",
                      fair_up_model=fair_up, ev_valid=False)

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


def _ev_key(sig: Signal) -> float:
    """Sort key for EV that puts an invalid (NaN) EV below every real one.

    NaN is unordered, so `>=` against it is False in BOTH directions and the
    winner of a tie-break becomes an accident of argument order. This makes the
    ordering total and explicit.
    """
    return sig.ev_per_dollar if sig.ev_valid else float("-inf")


def evaluate_hybrid(fair_up_down_model: float, fair_up_up_model: float,
                    quote_up: float, quote_down: float,
                    *, up_allowed: bool = True, **kwargs) -> Signal:
    """Per-side gate: Down candidates priced by the down model, Up by the up model.

    evaluate() runs once per model probability; the down model's Up call and
    the up model's Down call are discarded, then the higher-EV acting signal
    wins. With identical probabilities this reduces to plain evaluate().

    ``up_allowed=False`` removes the Up candidate BEFORE the pick, so a valid
    Down candidate is still taken. Dropping Up after the pick would discard
    whatever the opposite side had already qualified for. evaluate() treats a
    None quote as "that side is not tradeable", which is how the gate is
    expressed here.
    """
    if not up_allowed:
        quote_up = None
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
        best = (down_sig if _ev_key(down_sig) >= _ev_key(up_sig) else up_sig)
        # `best` may have acted, but on the side its own model does not price
        # here (the down model's Up call, the up model's Down call), so the
        # per-side rule discards it. Say that explicitly: reusing the discarded
        # signal's own "ok" reason made 204 of 205 gate-clearing polls look
        # like trades that some *other* gate had refused.
        reason = (f"discarded {best.side} candidate priced by the other model"
                  if best.act else best.reason)
        return Signal(None, best.fair, best.quote, best.edge, best.hurdle,
                      best.ev_per_dollar, False, reason)
    return max(cands, key=_ev_key)


def take_limit_px(fair_side: float, margin: float, quote: float | None,
                  tick: float = 0.01,
                  take_px: float | None = None) -> float:
    """The taker limit: our valuation minus a margin, but never more than one
    tick above the ask we actually measured.

    The ceiling alone is not enough, because the ceiling is OUR fair and our fair
    is the miscalibrated estimator on this venue (Brier 0.124 against the venue's
    0.106, log-loss 0.57 against 0.35). On a fast move it overshoots. Measured
    live on 2026-09-12 13:02: the trigger saw the ask at 0.7326, our digital said
    P(Down) = 0.94, so the limit went out at 0.90 -- and it FILLED at 0.8919, a
    trade the arm's own `ev_at_fill` scores at -0.195 per $1.

    Clamping to `quote + tick` costs nothing measurable: the backtest's taker leg
    only ever fills AT the ask (it filters to `ask <= fair - margin`, so the ask
    is always under the ceiling), which means this cap is exactly what makes live
    execution match what was measured. `take_px` stays a FLOOR -- an arm with no
    valuation of its own (the ticket) must still cross at the ask it read.
    """
    # A fair outside the open interval is not a valuation, it is a missing
    # input -- or a model disagreeing with the market by more than the contract
    # can express. Pricing a taker limit from one produced max(0.01, 0 - 0.04) =
    # 0.01 against a 0.730 book on 2026-09-12 (a +9.8bps/2s Up impulse fired
    # while spot was still ~14bps below the round's strike, so the digital said
    # P(Up) ~ 0). That order cannot fill: it rested the full wait, was
    # cancelled, and cost the round plus a 30s order pause. A NaN would be
    # worse, poisoning the min/max below. Fall back to the ask we measured.
    if quote and not (0.0 < fair_side < 1.0):
        fair_side = quote
    limit = max(0.01, round(fair_side - margin, 2))
    if take_px is not None:
        limit = max(limit, round(take_px, 2))
    if quote:
        limit = min(limit, round(quote + tick, 2))
    return max(0.01, limit)


def impulse_price_refusal(entry_mode: str, real_px: float | None,
                          cap: float) -> str | None:
    """Why an impulse order must not be placed at THIS price, or None to go.

    The impulse trigger checks `impulse_min_price..impulse_max_price` against the
    last ask the poll saw -- and the code already notes in `_impulse_tick` that
    this is `Band sanity only`: the price that binds is the one that comes back
    with the order's own quote, which `_place_order` fetches and crosses at. In
    impulse mode the model re-valuation that would otherwise re-price the trade
    is deliberately skipped (there is no model fair to re-check), so nothing else
    stands between a 0.70 trigger and a 0.90 fill except this function.

    That gap is not hypothetical: MARKET fills landed a mean +0.16 ABOVE the ask
    on offer at the trigger (6 of 8 fills), and the venue's displayed price lags
    its own book by up to 0.40. It also matters because the top of the band is
    where the money is lost -- `tools/backtest_impulse_orders --by-price` finds
    the only negative cells in the entire decomposition above 0.75 (EV/$1 -0.081
    and -0.104), and the reason is arithmetic rather than fitted: the fee fixes a
    break-even win rate of px/0.98, so at 0.90 a win pays +0.09 and needs 92%.

    Only the CAP is enforced. A quote cheaper than the trigger is a gift; a quote
    above the cap is the trade the band exists to refuse.
    """
    if entry_mode != "impulse" or cap >= 1.0 or real_px is None:
        return None
    if real_px > cap:
        return (f"real quote {real_px:.3f} above the impulse cap {cap:.2f}")
    return None


def stake_for_edge(edge: float, *, min_edge: float, min_stake: float,
                   max_stake: float, edge_at_max: float) -> float:
    """Scale the stake between min_stake and max_stake by signal strength.

    The venue has a hard order minimum, so a smaller bet simply cannot be
    expressed: the weakest signal the gate accepts (edge == min_edge) gets
    min_stake, the strongest (edge >= edge_at_max) gets max_stake, linear in
    between.

    This replaces fractional Kelly as the live rule. On a $40 bankroll
    0.25-Kelly is about $2.65 -- below the venue minimum -- so every order
    previously fell through main's `if stake <= 0` path and was placed at FULL
    size: the exact opposite of what Kelly had just said. A rule that cannot be
    expressed at the current bankroll is worse than no rule, because it is
    silent.
    """
    if max_stake <= min_stake:
        return max(0.0, min_stake)
    span = max(1e-9, edge_at_max - min_edge)
    t = min(1.0, max(0.0, (edge - min_edge) / span))
    return min_stake + (max_stake - min_stake) * t


def tilt_factor(moneyness_sd: float, *, reference: float, lo: float,
                hi: float) -> float:
    """Multiplier that concentrates stake where the recording says the edge is.

    Our edge is the venue's quote lagging spot, and that lag is largest while
    the round is still undecided: measured per-$1 return falls with |z|
    (+1.03, +0.49, +0.06, +0.17 by quartile), the mirror of Polymarket whose
    CLOB prices small moves correctly. So small |z| earns a bigger stake,
    bounded so a single trade cannot dominate the book.

    Returns 1.0 (no tilt) when the inputs carry no information.
    """
    if reference <= 0 or moneyness_sd <= 0 or hi <= 0:
        return 1.0
    return max(lo, min(hi, reference / moneyness_sd))


def stake_for_fraction(bankroll: float, *, fraction: float, min_stake: float,
                       cap: float, floor_to_minimum: bool = False) -> float:
    """Fixed percentage of the bankroll.

    The alternative to a dollar band. A band bet $5-10 regardless of size, so
    on a $68 bankroll it risked 7-15% per trade -- and after losses the SAME
    dollar stake is a larger fraction, so the risk fraction rises exactly when
    the account is shrinking. Measured on the replayed sequence, twelve
    consecutive losses finish a $68 bankroll at fixed dollars, while 5% of
    bankroll leaves $37 and recovers.

    Returns 0.0 (== SKIP) when the fraction cannot reach the venue minimum.
    Rounding up would silently raise the real fraction, which is the one thing
    this mode exists to control; below about a $20 bankroll the account is too
    small to subdivide further and sitting out is the honest answer.

    `floor_to_minimum` changes exactly that case from SKIP to the venue
    minimum: the account keeps trading, at a stake that is a LARGER fraction
    than asked (at $28 a $1 floor is 3.6%). It is off by default because that
    is a deliberate choice about small accounts, not an accident -- but "too
    small to size" and "too small to trade" are different statements, and the
    venue's floor is $1 whatever the account holds.

    The floor stops at FLOOR_MAX_BANKROLL_FRACTION of the bankroll. Above it
    the rule is doing what it says (keep a small account in the market); below
    it the floor is betting a large share of a nearly-empty account -- $1 is
    10% of $10 and 50% of $2 -- and sitting out is the honest answer again.
    """
    if bankroll <= 0 or fraction <= 0:
        return 0.0
    if bankroll < min_stake:
        return 0.0
    stake = bankroll * fraction
    if cap > 0:
        stake = min(stake, cap)
    stake = min(stake, bankroll)
    if stake < min_stake:
        if not floor_to_minimum:
            return 0.0
        if min_stake > bankroll * FLOOR_MAX_BANKROLL_FRACTION:
            return 0.0
        return min_stake
    return stake


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
