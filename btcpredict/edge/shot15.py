"""The 15-minute ITM cell -- pre-registered, and OBSERVED before it is trusted.

This is the rule that came out of the perfect_shot / Impulse Lag work. It is
written down here so that when enough dates exist it is tested as a hypothesis
stated beforehand, rather than rediscovered from a grid. Nothing in this module
places an order; see `config.shot15_mode`.

WHAT IT DOES

At 300-600s to expiry it buys the side that is currently WINNING (spot past the
strike), when the venue's ask for that side sits at least `min_edge` below a
driftless digital fair value:

    m      = |spot - strike| / strike
    P*     = Phi( m / (sigma_1m * sqrt(tau/60)) )        driftless, no drift term
    side   = "Up" if spot > strike else "Down"
    fire   = P* - ask >= min_edge, with ask inside [min_quote, max_quote]

WHAT IT IS NOT

It is NOT the Impulse Lag arm, and it must never be described as one. That spec
gated on a Z-score of the displacement (Z >= 2.2) and capped the entry at 0.75.
Measured 2026-09-20 on 391 Binance CHAINLINK 15m rounds, those gates produce
ZERO trades: of the 907 ticks that cleared Z >= 2.2, all 907 already showed an
ask above 0.75, and all 907 were already within 12 cents of P*. The harder the
impulse, the more certainly the book has already repriced it -- so the gate
selects exactly the moments it is least likely to pay. This module is what is
left after removing that gate, which makes it a DIFFERENT hypothesis.

It is also not a reversal arm. It buys favourites (mean ask 0.714). Whether that
is distinct from what the model arm already harvests is unmeasured, and is the
first thing to check before this is ever funded.

THE EVIDENCE, AND THE BAR IT HAS NOT CLEARED

On the only venue we can trade (feed_provider='CHAINLINK'; the TWAP rows are
Polymarket and unreachable -- they carry no market_id):

    n=121 trades over 4 dates, 4/4 positive
    hit 0.752   mean ask 0.714   EV +0.0555/$1   day-clustered t +1.64

+1.64 is BELOW the t >= 2 bar. One 12-trade date (+0.2516) carries much of it:
dropping it takes the mean of day means from +0.0913 to +0.0379. A 288-cell
sweep of these knobs was run and produced a best cell at EV +0.2673, day-t
+2.85, 4/4 dates, surviving every leave-one-date-out fold -- and a best-of-grid
null (round-level resampling from the venue's own implied probability) put that
at p = 0.225, i.e. an ordinary draw from noise. So the defaults below are the
PRE-SPECIFIED values, deliberately not the swept ones.

This exists to accumulate dates honestly. It should stay in shadow until the
pre-specified cell clears t >= 2 on dates it was not chosen on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Shot15Signal:
    """One evaluation. `act` never implies an order -- see `config.shot15_mode`."""

    act: bool
    side: str | None          # "Up" / "Down" / None
    pstar: float              # driftless digital fair value for that side
    ask: float                # the venue's ask for that side
    edge: float               # pstar - ask
    reason: str

    def __str__(self) -> str:
        if not self.act:
            return f"NO-SHOT ({self.reason})"
        return (f"{self.side} P*={self.pstar:.3f} ask={self.ask:.3f} "
                f"edge={self.edge:+.3f}")


#: The sigma window the evidence was measured on, in seconds, and the minimum
#: number of 1-second returns it needs before it will speak.
SIGMA_WINDOW_S = 900.0
SIGMA_MIN_SAMPLES = 150


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sigma_from_ticks(ticks, now_ms: int,
                     window_s: float = SIGMA_WINDOW_S,
                     min_samples: int = SIGMA_MIN_SAMPLES) -> float | None:
    """sigma_1m from the trailing `window_s` of ~1Hz spot. None if too thin.

    ⛔ THIS ARM MUST NOT USE `VolEstimator.sigma_1m()`, which is what `_record`
    hands every other arm. That is an EWMA (lam=0.97) over 1-MINUTE bars with a
    seasonality adjustment; the evidence for this cell was measured on a 900s
    rolling std of 1-SECOND returns. They are not interchangeable, and the
    difference is not cosmetic -- replayed over the same recorded rounds, the
    EWMA estimator took the cell from n=121 / EV +0.0555 / day-t +1.64 to
    n=25 / EV -0.1025 / day-t +0.18. Feeding it the wrong sigma would shadow a
    DIFFERENT rule than the one the config documents, and would look fine.

    Returns None rather than falling back to another estimator, deliberately: a
    fallback would silently mix two cells in one decision series, and nothing in
    the recorded rows would say which was which.
    """
    if not ticks:
        return None
    lo = now_ms - window_s * 1000.0
    # One price per second, last print wins -- the recorded series the evidence
    # was built on was collapsed the same way.
    per_sec: dict[int, float] = {}
    for ts_ms, px in ticks:
        if px and px > 0 and lo <= ts_ms <= now_ms:
            per_sec[int(ts_ms // 1000)] = float(px)
    if len(per_sec) < min_samples + 1:
        return None
    secs = sorted(per_sec)
    rets = [per_sec[b] / per_sec[a] - 1.0
            for a, b in zip(secs, secs[1:]) if per_sec[a] > 0]
    if len(rets) < min_samples:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)   # ddof=1
    if var <= 0:
        return None
    return math.sqrt(var) * math.sqrt(60.0)


def fair_itm(spot: float, strike: float, tau_s: float, sigma_1m: float) -> float:
    """Driftless P(the side currently ahead stays ahead).

    Deliberately has no drift term: a drift estimated from the same window that
    sets sigma is fitted noise at this horizon, and adding one was rejected.
    Returns 0.5 when spot sits exactly on the strike, which is the honest value
    and keeps the function continuous there.
    """
    if tau_s <= 0 or sigma_1m <= 0 or strike <= 0 or spot <= 0:
        return 0.5
    m = abs(spot - strike) / strike
    return _phi(m / (sigma_1m * math.sqrt(tau_s / 60.0)))


def evaluate(
    spot: float | None,
    strike: float | None,
    tau_s: float | None,
    sigma_1m: float | None,
    up_ask: float | None,
    down_ask: float | None,
    *,
    min_tau_s: float,
    max_tau_s: float,
    min_edge: float,
    min_quote: float,
    max_quote: float,
) -> Shot15Signal:
    """Evaluate the cell. Every refusal carries a distinct reason.

    The reasons matter as much as the fires: a decision row with `acted=0` and a
    reason is what makes the funnel recoverable from the database later, and the
    funnel is what localised the Impulse Lag failure to a single gate.
    """
    none = Shot15Signal(False, None, 0.0, 0.0, 0.0, "")
    # ⛔ TAU FIRST, before anything else can refuse. It is the most selective
    # gate -- a 15m round spends two thirds of its life outside the window --
    # and the caller drops `tau ...` refusals unlogged. Checking a cold sigma
    # buffer ahead of it would relabel every out-of-window tick as "missing
    # input" and write ~600 rows per round during warmup instead of none.
    if tau_s is None:
        return Shot15Signal(**{**none.__dict__, "reason": "missing tau"})
    if not (min_tau_s <= tau_s <= max_tau_s):
        return Shot15Signal(**{**none.__dict__,
                               "reason": f"tau {tau_s:.0f}s outside window"})
    if sigma_1m is None:
        # Distinct from "missing input": this is the 900s buffer not yet warm,
        # which is a startup state that ends, not a broken input.
        return Shot15Signal(**{**none.__dict__, "reason": "sigma buffer thin"})
    if spot is None or strike is None:
        return Shot15Signal(**{**none.__dict__, "reason": "missing input"})
    if not (spot > 0 and strike > 0 and sigma_1m > 0):
        return Shot15Signal(**{**none.__dict__, "reason": "non-positive input"})
    if up_ask is None or down_ask is None:
        return Shot15Signal(**{**none.__dict__, "reason": "no book"})
    # ⛔ THE COHERENCE INVARIANT. Two complementary asks must sum ABOVE $1.00
    # (~1.02 live). A pair summing below it is free arbitrage, i.e. corrupt
    # data, and a price filter selects exactly those rows -- that is how a
    # +0.58/$1 "edge" that passed 23-fold LODO was once manufactured here.
    if up_ask + down_ask <= 1.0:
        return Shot15Signal(**{**none.__dict__,
                               "reason": f"incoherent book {up_ask + down_ask:.3f}"})

    side = "Up" if spot > strike else "Down"
    ask = up_ask if side == "Up" else down_ask
    pstar = fair_itm(spot, strike, tau_s, sigma_1m)
    edge = pstar - ask

    if not (min_quote < ask <= max_quote):
        return Shot15Signal(False, side, pstar, ask, edge,
                            f"ask {ask:.3f} outside [{min_quote:.2f},{max_quote:.2f}]")
    if edge < min_edge:
        return Shot15Signal(False, side, pstar, ask, edge,
                            f"edge {edge:+.3f} < {min_edge:.2f}")
    return Shot15Signal(True, side, pstar, ask, edge,
                        f"shot15 edge {edge:+.3f}")
