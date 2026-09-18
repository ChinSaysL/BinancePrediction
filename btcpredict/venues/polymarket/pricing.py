"""The Polymarket terminal distribution: what a `btc-updown-5m` round pays on.

This is the fourth and last thing the venue layout reserves (`markets.py`,
`book.py`, `broker.py`, `pricing.py`). It is the PREREQUISITE for any edge gate
on this venue: an arm earns `P(win) - price - fee`, and on Polymarket `P(win)`
is the probability of a *different* event than the one every model in
`btcpredict/models/` was fitted to.

WHAT IS DIFFERENT (measured, not read off a description)
--------------------------------------------------------
The Binance wrapper pays on a POINT: `endPrice` vs `startPrice`, measured to
0.02 bps of the instantaneous close. Polymarket pays on a Chainlink TWAP
against the price at the START of the round, and the two venues disagree on
**10.4% of settled rounds** (48-round comparison) -- they share a slug and a
slug family but they are not the same bet.

WHICH window, exactly, is SETTLED BY MEASUREMENT -- see `terminal_rule()`.

THE RULE, IN LOG SPACE
----------------------
Let `K` be the round's opening price (the strike) and `x_u = ln(S_u / K)` the
log-return since the round opened. The round averages over a window
`[a, b]`, `b` = the bell, `L = b - a` the window length, and settles

    Up  <=>  (1/L) * integral_a^b S_u du  >=  K

The exponent inside a round is a few bps, so `exp(x) = 1 + x` to O(x^2) (the
neglected term is ~1e-8, far below the venue's 0.01 tick) and dividing by
`L > 0` does not change the sign:

    Up  <=>  W := integral_a^b x_u du  >=  0

`W` is Gaussian, so `P(Up) = Phi(E[W] / sd(W))`. There are exactly two cases,
because the window can be entirely in the future or half-realised:

    tau = b - t (seconds remaining), `L` as above.

    REGIME 1, tau >= L   the window has not opened yet (a >= t)
        E[W]  = L * x_t
        Var[W] = sigma_s^2 * V(tau - L, tau)
      where `V(u1,u2) = u2^3/3 - u1^3/3 - (u2-u1)*u1^2` is
      `Var[integral_{u1}^{u2} W_s ds]` for a standard Brownian motion `W`,
      using `Var[integral_0^s W du] = s^3/3`.
      The path is a martingale, so the mean is just the current level held for
      the window's length -- but the VARIANCE grows as the window recedes,
      which is why a rule looking 4 minutes ahead is weaker than a 60-second
      horizon would suggest.

    REGIME 2, tau < L    the window is partly realised
        E[W]  = J + x_t * tau
        Var[W] = sigma_s^2 * tau^3/3
      with `J = integral_a^t x_u du`, the KNOWN part of the window.

`V` and the `tau^3/3` both ignore the round's length, and so does the
condition `W >= 0` -- **the round's length never enters this model.** (In the
`full` window `L = T`, so it cancels: `pricing/twap.py` carries a `T` only
because it works with window AVERAGES.) That is worth knowing: a 5m and a 15m
round with the same `tau` are priced identically here, and a caller that
cannot say how long the round is can still price it.

`J` IS THE ANCHOR THE POINT RULE DOES NOT HAVE
----------------------------------------------
A round whose spot is above `K` but whose window has averaged below `K` is a
DOWN bet. Both directions occur, and near the bell `J` dominates: with
`tau -> 0` the answer is the sign of `J` alone. Passing `J = None` assumes the
window has traded at the current level all along, which flatters the model
exactly where it matters most; `fair_value` reports that it did so
(`assumed_flat`) and the model adapter puts it in the `reason` string, so a
fill priced off an assumption is visible in the ledger rather than silent.

THE FEE IS NOT THE WRAPPER'S FEE, AND NOT A FLAT RATE
-----------------------------------------------------
Polymarket's taker fee is `0.07 * p * (1-p)` per SHARE, verified to the cent
against a real fill (`0.07 * 0.01 * 0.99 * 500 = 0.3465` exactly, 2026-09-16
17:29:42Z). As a fraction of the money staked that is `0.07 * (1-p)`:
**6.93% at p=0.01**, 1.75% at 0.50, 0.91% at 0.87 -- the OPPOSITE shape to the
wrapper's flat measured 96 bps. Cheap longshots are the most fee-expensive
thing on this venue, and `edge_per_share` below is the only way to compare two
entries on it. Makers pay zero (`takerOnly: true`), which is why the fee needs
the side of the trade, not just the price.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not read the venue, the book or the clock, and it does not decide
anything. It is a pure function of (spot, strike, tau, sigma, realised window
integral), so a backtest prices the same object the bot prices -- which is the
only defence against the pricing-assumption trap (a wrong PRICE assumption
sails through every statistical check; see /memories/backtest-artifacts.md).
"""

from __future__ import annotations

import math
from typing import Callable, Iterable, Literal

from btcpredict.pricing.digital import norm_cdf

#: The venue's own resolution config: `cryptoMarketConfig {id: btc-5m-twap-60,
#: twapLookbackSeconds: 60}` and `resolutionSource data.chain.link/streams/
#: btc-usd-twap-60s-streams`. The 60 is the smoothing lookback, and for a 5m
#: market it cannot be the round length -- so it is carried here as the window.
TWAP_LOOKBACK_S = 60.0

#: The two candidate windows, named rather than booleans so a call site reads
#: as what it means. See `terminal_rule()` for which one is measured.
TAIL = "tail"      # [b - 60, b]: the last 60 seconds of the round
FULL = "full"      # [0, b]: the whole round, i.e. the venue description's
                   # "the time range specified in the title"
Window = Literal["tail", "full"]

#: Clamp, so no single input yields 0.0/1.0 and implies infinite edge.
_EPS = 1e-6

#: Polymarket's crypto taker fee coefficient (`crypto_fees_v2`, rate 0.07,
#: exponent 1, `takerOnly: true`). Per share: `rate * p * (1-p)`.
FEE_RATE = 0.07


def terminal_rule() -> str:
    """Which window the venue has been MEASURED to settle on.

    `tools/pm_rule_ident.py` fits the candidates to Polymarket's OWN published
    winners (`gamma ?slug=X&closed=true`), computing every candidate from the
    same Binance 1s klines. Re-run 2026-09-17 over **2,303 BTC 5m rounds**
    (2026-09-08..09-15):

        C  mean(close[T-60:T]) >= K   90.7%   <-- TAIL
        A  close(T)            >= K   87.3%   (the wrapper's point rule)
        B  mean(close[0:T])    >= K   84.1%   <-- FULL
        D  mean(close[0:60])   >= K   63.5%

    The winner is 6.6 pp clear of the whole-round window on 2,303 rounds (a
    ~13-sigma gap), so this is not a fit on a thin sample: **the settled window
    is the last 60 seconds, not the round.** (The residue is ~9%, it
    concentrates on rounds decided within ~2 bps of the strike, and it is
    Binance-vs-Chainlink source noise rather than a rule error -- so no
    threshold below ~5 bps should be trusted on either window.)

    CONSEQUENCE FOR THE REST OF THE REPO: `btcpredict/pricing/twap.py`
    implements the WHOLE-ROUND window. Its algebra is right, but it is the
    wrong window for this venue, so a model built on it is fitted to a bet
    Polymarket does not pay on -- the same class of error as fitting on the
    wrapper's point rule. That is what `window=FULL` reproduces here, and why
    `TAIL` is the default.

    This is a MEASUREMENT: re-run the tool rather than edit this function.
    """
    return TAIL


# ---------------------------------------------------------------------------
# the distribution
# ---------------------------------------------------------------------------

def _sigma_per_s(sigma_1m: float) -> float:
    """Per-minute sigma -> per-second sigma. The `1/60` matters: every
    variance below is in seconds^3 of Brownian time."""
    return sigma_1m / math.sqrt(60.0)


def integral_variance(u1: float, u2: float) -> float:
    """`Var[integral_{u1}^{u2} W_s ds]` for a standard BM, in seconds^3.

    From `Var[integral_0^s W du] = s^3/3` and
    `Cov(integral_0^a W, integral_0^b W) = a^3/3 + (b-a)*a^2/2` for `b > a`.
    Exposed because it is the one quantity a reader should be able to check.
    """
    if u2 <= u1:
        return 0.0
    return (u2 ** 3 - u1 ** 3) / 3.0 - (u2 - u1) * u1 ** 2


def window_seconds(window: Window, tau_s: float, *,
                   elapsed_s: float | None = None,
                   round_seconds: float | None = None) -> float:
    """Length of the settled window, `L`.

    `tail` is a venue constant and ignores every argument. `full` needs the
    round's length, which the caller must supply (`round_seconds`, or
    `elapsed_s` + `tau_s`): a full-window model with no round length is
    undefined, and inventing a 300 s default would silently price a 15m round
    as a 5m one. Raises instead of guessing.
    """
    w = (window or TAIL).strip().lower()
    if w == TAIL:
        return TWAP_LOOKBACK_S
    if w == FULL:
        if round_seconds is not None and round_seconds > 0:
            return float(round_seconds)
        if elapsed_s is not None and elapsed_s >= 0:
            return float(elapsed_s) + max(float(tau_s), 0.0)
        raise ValueError(
            "window='full' needs the round length (round_seconds, or "
            "elapsed_s together with tau_s) -- there is no safe default")
    raise ValueError(f"unknown window {window!r}; expected {TAIL!r} or {FULL!r}")


def _terminal(S_now: float, K: float, tau_s: float, sigma_1m: float,
              L: float, window_log_int: float | None
              ) -> tuple[float, float, bool]:
    """`(E[W], sd(W), assumed_flat)` for the two regimes in the module docstring.

    `W` is in log-seconds, so `E`/`sd` are only meaningful as a ratio; nothing
    outside this module should consume them separately.
    """
    x = math.log(S_now / K)
    if tau_s <= 0.0:
        # No time left: the window is fully realised and the answer is its
        # sign. `sd = 0` is not a bug, it is the degenerate case.
        J = window_log_int if window_log_int is not None else 0.0
        return J, 0.0, window_log_int is None

    if tau_s >= L:
        # Regime 1: the window is entirely in the future. The mean is the
        # current level held for the window's length (martingale), the
        # variance is the integral variance of the fresh Brownian part.
        return L * x, math.sqrt(integral_variance(tau_s - L, tau_s)), False

    # Regime 2: part of the window is realised. `L - tau_s` seconds of it are
    # known -- as an integral if the caller supplied one, otherwise assumed to
    # have traded at the current level.
    assumed = window_log_int is None
    J = x * (L - tau_s) if assumed else window_log_int
    return J + x * tau_s, math.sqrt(integral_variance(0.0, tau_s)), assumed


def z_score(S_now: float, K: float, tau_s: float, sigma_1m: float, *,
            window_log_int: float | None = None, window: Window = TAIL,
            elapsed_s: float | None = None,
            round_seconds: float | None = None) -> float:
    """`E[W] / sd(W)`, the quantity `fair_value` thresholds.

    `+inf`/`-inf` when the variance is zero (the window is fully realised),
    which is a real answer and not a missing one. Callers that need a finite
    number must handle it: it is not None and it compares as a number.
    """
    if S_now <= 0 or K <= 0:
        raise ValueError(f"prices must be positive (S={S_now}, K={K})")
    if sigma_1m <= 0:
        raise ValueError(f"sigma must be positive (got {sigma_1m})")
    L = window_seconds(window, tau_s, elapsed_s=elapsed_s,
                       round_seconds=round_seconds)
    mean, sd, _ = _terminal(S_now, K, tau_s, sigma_1m, L, window_log_int)
    if sd <= 0.0:
        return math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
    return mean / (sd * _sigma_per_s(sigma_1m))


def fair_value(S_now: float, K: float, tau_s: float, sigma_1m: float, *,
               window_log_int: float | None = None, window: Window = TAIL,
               elapsed_s: float | None = None,
               round_seconds: float | None = None,
               ) -> tuple[float, bool]:
    """P(the round settles Up), and whether the window integral was assumed.

    Returns `(p_up, assumed_flat)`. `assumed_flat` is True when
    `window_log_int` was None AND the window is partly realised, i.e. when the
    number rests on "the window has traded at the current level". A caller that
    records it can tell an observed price from an assumed one later; that is
    the difference between an edge and an artefact.

    Signature shape matches `pricing.digital.fair_value(S, K, tau_s, sigma_1m)`
    so it can be swapped in wherever the point rule was used.
    """
    L = window_seconds(window, tau_s, elapsed_s=elapsed_s,
                       round_seconds=round_seconds)
    mean, sd, assumed = _terminal(S_now, K, tau_s, sigma_1m, L,
                                  window_log_int)
    if sd <= 0.0:
        return (1.0 - _EPS if mean > 0 else _EPS), assumed
    p = norm_cdf(mean / (sd * _sigma_per_s(sigma_1m)))
    return min(1.0 - _EPS, max(_EPS, p)), assumed


def decisiveness_vs_full(tau_s: float, *, round_seconds: float = 300.0) -> float:
    """`|z|` per unit of `ln(S/K)` under the TAIL window, divided by the FULL
    window's, at the same `tau` and with the realised part held at 0 on both
    sides. Defined for `tau >= TWAP_LOOKBACK_S` (the tail window's regime 1).

    THE ANSWER IS NOT WHAT THE "5x LESS DISPERSED" SLOGAN SUGGESTS, and that
    slogan is why this function exists. On a 5-minute round:

        tau =  60 s  (the window opens)   1.000   the two are the same object
        tau = 300 s  (the round opens)    0.620   TAIL is LESS decisive
        tau = 600 s                       0.845
        tau ->  inf                       1.000

    At the round's open the tail window sits 240 s in the FUTURE while the
    full-round window starts now, so for the same displacement the tail rule is
    the more conservative one. The 5x figure (`sigma^2*L/3`) describes the
    TERMINAL distribution of a window that starts now -- it is not the
    conditional z at an arbitrary time, and using it to size a mid-round entry
    over-states the tail rule's confidence. Between `tau = L` and `tau = T`
    the ratio falls from 1 to 0.62 and then climbs back to 1: the two rules
    differ in the MIDDLE of the round, which is where the arms trade.
    """
    L = TWAP_LOOKBACK_S
    if tau_s < L:
        raise ValueError(
            f"tau {tau_s}s is inside the tail window; compare at tau >= {L}s "
            "(at tau == L both windows are the same L-second average)")
    T = float(round_seconds)
    if T <= L:
        raise ValueError(f"round_seconds must exceed the window ({T} <= {L})")
    if tau_s >= T:
        sd_tail = math.sqrt(integral_variance(tau_s - L, tau_s))
        sd_full = math.sqrt(integral_variance(tau_s - T, tau_s))
        z_tail, z_full = L / sd_tail, T / sd_full
    else:
        # The full window is partly realised here; with J = 0 it is a point
        # bet on the remaining `tau` seconds.
        sd_tail = math.sqrt(integral_variance(tau_s - L, tau_s))
        sd_full = math.sqrt(integral_variance(0.0, tau_s))
        z_tail, z_full = L / sd_tail, tau_s / sd_full
    return z_tail / z_full


# ---------------------------------------------------------------------------
# the realised part of the window (`J`)
# ---------------------------------------------------------------------------

def _time_weighted(samples: Iterable[tuple[float, float]],
                   open_ms: float, close_ms: float,
                   fn: Callable[[float], float] | None = None
                   ) -> tuple[float, float] | None:
    """`(integral of fn(value) over [open, close] in value-seconds, span ms)`.

    Step-constant, hold-forward: the price observed at `t` is the price until
    the next observation. That is the honest model for a 1 Hz poll, and it is
    the same convention `pricing/twap.py` uses for the whole-round window.
    Returns None when nothing is known about the interval -- a missing window
    is not a window that averaged zero.
    """
    if close_ms <= open_ms:
        return None
    pts = sorted((float(t), float(v)) for t, v in samples
                 if v is not None and float(v) > 0)
    if not pts:
        return None
    f = fn or (lambda v: v)

    # The value held at the window's open: the newest sample at or before it.
    prior = [(t, v) for t, v in pts if t <= open_ms]
    if prior:
        held = prior[-1][1]
    else:
        inside = [(t, v) for t, v in pts if t < close_ms]
        if not inside:
            return None            # every sample is after the window: no evidence
        held = inside[0][1]

    cursor = open_ms
    total = 0.0
    for t, v in pts:
        if t <= open_ms:
            continue
        stop = min(t, close_ms)
        total += f(held) * (stop - cursor) / 1000.0
        cursor = stop
        held = v
        if cursor >= close_ms:
            break
    if cursor < close_ms:
        total += f(held) * (close_ms - cursor) / 1000.0
    return total, close_ms - open_ms


def window_log_integral(samples: Iterable[tuple[float, float]], strike: float,
                        *, open_ms: float, close_ms: float) -> float | None:
    """`J = integral ln(S_u / K) du` over `[open_ms, close_ms]`, LOG-SECONDS.

    This is the input the point rule has no equivalent of. Units are
    deliberately explicit (`log-seconds`, not an average over an unstated
    window): `E[W] = J + x_t * tau` only balances if `J` is an integral, and
    "the average" is ambiguous exactly where it matters -- over the window, or
    over the round, or over `[0, now]`.

    Compute the average yourself with `window_log_avg` if that is what you
    want; do not guess which one a caller means.
    """
    if not (strike > 0):
        return None
    got = _time_weighted(samples, open_ms, close_ms,
                         fn=lambda p: math.log(p / strike))
    return None if got is None else got[0]


def window_log_avg(samples: Iterable[tuple[float, float]], strike: float,
                   *, open_ms: float, close_ms: float) -> float | None:
    """`J / span`, i.e. the window's mean log-return. Convenience only."""
    got = _time_weighted(samples, open_ms, close_ms,
                         fn=lambda p: math.log(p / strike))
    if got is None or got[1] <= 0:
        return None
    return got[0] / (got[1] / 1000.0)


def settles_up(samples: Iterable[tuple[float, float]], start_price: float, *,
               start_ms: float | None = None, end_ms: float | None = None,
               window: Window = TAIL) -> bool | None:
    """GROUND TRUTH under the venue's own rule: did the round settle Up?

    `>=` not `>`, and the comparison is between the window's time-weighted
    average PRICE and `start_price` -- the log-space algebra above is a
    linearisation for pricing, not the rule itself. Returns None when the
    window cannot be covered.

    Never score a Polymarket model with the wrapper's settlement column: the
    two rules disagree on 10.4% of rounds, so that column is a different bet's
    answer. This function and `gamma`'s own `outcomePrices` are the two labels
    that are about the right bet.

    `start_ms` is required only for the `full` window (the tail window is
    located from `end_ms` alone).

    ONE OPEN CONVENTION, stated rather than hidden: a sample at `t` describes
    `[t, next sample)`, so a sample sitting exactly ON the window's close
    describes nothing inside it (see `window_log_integral`). Working from
    per-second CLOSES that means the window's 60 bars are `T-60 .. T-1`, while
    `tools/pm_rule_ident.py`'s `px[-60:]` counts `T-59 .. T`. That is a one-bar
    (1.7%) difference in the average, and it can only decide a round whose
    displacement is within ~1 s of the strike -- but it is unmeasured, and the
    tool that identified the window is the thing it affects. Measure it with a
    hold-forward candidate before treating either label as exact.
    """
    if start_price <= 0 or end_ms is None:
        return None
    full = (window or TAIL).strip().lower() == FULL
    if full:
        if start_ms is None:
            return None
        open_ms = float(start_ms)
    else:
        open_ms = float(end_ms) - TWAP_LOOKBACK_S * 1000.0
    got = _time_weighted(samples, open_ms, float(end_ms))
    if got is None or got[1] <= 0:
        return None
    return (got[0] / (got[1] / 1000.0)) >= start_price


# ---------------------------------------------------------------------------
# pricing an entry against the venue's own asks
# ---------------------------------------------------------------------------

def fee_per_share(price: float, *, fee_rate: float = FEE_RATE) -> float:
    """Polymarket's TAKER fee on one share bought at `price`.

    `fee_rate * p * (1-p)`, from the venue's `crypto_fees_v2` schedule
    (`rate 0.07, exponent 1, takerOnly true`) and verified to the cent against
    a real fill. Makers pay 0.
    """
    p = min(1.0, max(0.0, float(price)))
    return fee_rate * p * (1.0 - p)


def fee_as_stake_fraction(price: float, *, fee_rate: float = FEE_RATE) -> float:
    """The same fee as a fraction of the money staked, `fee_rate * (1-p)`.

    The shape is what matters: 6.93% at p=0.01, 3.50% at 0.50, 0.91% at 0.87.
    Cheapest contracts are the most expensive to trade. This is the number to
    use when the stake, not the share count, is the risk bound.
    """
    return fee_rate * (1.0 - min(1.0, max(0.0, float(price))))


def edge_per_share(p_up: float, ask: float, *,
                   fee_rate: float = FEE_RATE, maker: bool = False) -> float:
    """`P(win) - price - fee`, per share. The only comparable unit.

    Per SHARE and not per dollar staked: `(1-p)/p if win else -1` is
    leverage-dominated, and on the same cell the two estimators have disagreed
    in sign in this repo (see /memories/backtest-artifacts.md). A taker pays
    the fee; a maker does not, and the venue attributes the side on every
    trade, so `maker=True` is a claim about a real fill and not an assumption.
    """
    return (float(p_up) - float(ask)
            - (0.0 if maker else fee_per_share(ask, fee_rate=fee_rate)))


def breakeven_ask(p_win: float, *, fee_rate: float = FEE_RATE) -> float:
    """The ask at which a taker's `edge_per_share` is exactly zero.

    Solves `p = a + fee_rate*a*(1-a)` for `a`. Use this -- and not
    `p_win / (1 + fee_rate)`, which ignores the `(1-a)` -- when turning a
    measured win rate into a ceiling, and note that a gate built from a POOLED
    win rate is measured on the wrong cohort (the set a low ask ADMITS wins
    less, because a low ask IS the market saying the trigger is weak).
    """
    p = min(1.0, max(0.0, float(p_win)))
    if fee_rate <= 0:
        return p
    b = 1.0 + fee_rate
    disc = b * b - 4.0 * fee_rate * p
    if disc <= 0:
        return 0.0
    return min(1.0, max(0.0, (b - math.sqrt(disc)) / (2.0 * fee_rate)))


# NOTE: there is no model class in this module on purpose. The venue package
# holds PRIMITIVES -- discovery, book, orders, and this distribution -- and must
# not import the model layer. `TwapModel` lives in `btcpredict/models/twap.py`;
# putting it here created a genuine import cycle (importing THIS module first
# pulled in `btcpredict.models`, whose `__init__` imports back into this module
# before the class existed), and the failure only appeared in one of the two
# import orders, so a test starting from `btcpredict.models` hid it.

__all__ = [
    "TWAP_LOOKBACK_S", "TAIL", "FULL", "FEE_RATE",
    "terminal_rule", "integral_variance", "window_seconds", "z_score",
    "fair_value", "decisiveness_vs_full", "window_log_integral",
    "window_log_avg", "settles_up", "fee_per_share", "fee_as_stake_fraction",
    "edge_per_share", "breakeven_ask",
]
