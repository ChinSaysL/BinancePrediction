"""Configuration. Values come from .env, overridable by real environment vars."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# Public prediction-market feed (unauthenticated, reverse-engineered from the web app).
BAPI_BASE = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/prediction"
MARKET_LIST_URL = f"{BAPI_BASE}/market/list"
MARKET_DETAIL_URL = f"{BAPI_BASE}/market/detail"

SPOT_REST = "https://api.binance.com"
SPOT_WS = "wss://stream.binance.com:9443/ws"

# The 5-minute BTC up/down series.
EVENT_SLUG = "btc-up-or-down-5m"
ROUND_SECONDS = 300

# Markets to record, as (slug prefix, Binance symbol, round seconds).
# MARKETS[0] is the PRIMARY: the model, impulse and double arms only ever look
# at it, and it is the only family whose round is re-swept every poll.
#
# The CROSS arm trades every family in CROSS_TRADED (below). Widened from
# BTC-5m-only on 2026-09-14 at the operator's explicit instruction, with the
# risk stated and accepted. What the data does and does not say:
#
#   * the WIN RATE is now measurable on every family, because the bot records
#     the venue's own strike and winner (tools/cross_market_scan.py):
#         btc-5m 0.884 [0.821,0.943] 25.9 trig/day   eth-5m 0.783 [0.681,0.876] 18.4
#         bnb-5m 0.723 [0.591,0.845] 41.5            btc-15m 0.810 [0.545,1.000] 10.5
#         eth-15m 0.688 [0.357,0.941] 8.0            bnb-15m: no triggers yet
#   * the EV is NOT measurable for anything but BTC-5m, because the websocket
#     book followed only the traded market, so the others have ~zero recorded
#     ask. Break-even at live fills is ~0.665-0.69.
#   * this substrate reads BTC at 0.884 against the 31-day Chainlink corpus's
#     0.714. If that same 0.17 haircut applies, eth -> ~0.61 and bnb -> ~0.55,
#     i.e. BELOW break-even. That is the live risk being taken.
#
# Note: models/sniper.py still hardcodes ROUND_SECONDS=300 for the (disabled)
# model arm, so a 900s market must never become primary without fixing that.
#
# How often the RECORDED-only families are refreshed, in seconds. The traded
# family is fetched every poll; everything else is a tape for later analysis and
# does not need 1s resolution.
#
# WHY THIS EXISTS (measured 2026-09-13). `rounds_by_prefixes` pages the venue's
# market list 50 at a time and stops only once EVERY requested prefix is found.
# That list is volume-sorted, so a quiet family sinks through the day: at 22:00
# UTC on a Sunday `bnb-updown-5m` sat at **offset 1450**, forcing 30 pages and
# 3.95s per sweep. The decision loop went from a 1.00s median to 4.15s -- and
# because the cross arm compares the newest tick with one 2-4s old, a 4s poll
# makes short crossings invisible. btc-updown-5m-1789336500 is the worked case:
# spot crossed the strike to 77336.00 for ~1.5s and the arm never sampled it.
# Coupling trade latency to an unrelated market's popularity is the bug.
RECORDED_REFRESH_S = 30.0

MARKETS = [
    ("btc-updown-5m", "BTCUSDT", 300),      # primary (model/impulse/double too)
    ("eth-updown-5m", "ETHUSDT", 300),
    ("bnb-updown-5m", "BNBUSDT", 300),
    ("btc-updown-15m", "BTCUSDT", 900),
    ("eth-updown-15m", "ETHUSDT", 900),
    ("bnb-updown-15m", "BNBUSDT", 900),
]

# Families the CROSS arm may order in. Everything not listed here is recorded
# only. Kept separate from MARKETS so widening the tape and widening the risk
# stay two different edits -- removing that separation is how the daily loss cap
# was once bypassed.
CROSS_TRADED = frozenset(p for p, _, _ in MARKETS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- credentials -------------------------------------------------------
    # The repo's original .env used lowercase `binance_api_key` / `secret_key`.
    # Both those and the canonical names are accepted; canonical wins.
    binance_api_key: str = ""
    binance_api_secret: str = ""
    secret_key: str = ""

    # --- mode --------------------------------------------------------------
    trading_mode: Literal["record", "paper", "live"] = "record"

    # --- sizing ------------------------------------------------------------
    # "edge": the weakest accepted signal (edge == min_edge) is staked at
    #   min_stake_usdt, the strongest (edge >= stake_edge_hi) at max_stake_usdt,
    #   linear in between. "kelly": fractional Kelly, and a Kelly value below
    #   the venue minimum SKIPS the trade instead of rounding up to full size.
    #   "fraction": a fixed PERCENTAGE of the live bankroll (stake_fraction),
    #   floored at the venue minimum and capped by stake_cap_usdt. Unlike the
    #   band, the stake shrinks as the bankroll shrinks, which is the whole
    #   point: with fixed dollars the risk FRACTION rises exactly when losing.
    #   A fraction that cannot reach the venue minimum SKIPS the trade rather
    #   than rounding up, because rounding up silently raises the real fraction.
    sizing_mode: Literal["edge", "kelly", "fraction"] = "edge"
    # Which measure of "signal strength" drives the stake inside the band:
    #   edge -> the gated, post-haircut edge   (min_edge .. stake_edge_hi)
    #   ev   -> expected profit per $1 staked  (stake_ev_lo .. stake_ev_hi)
    # Measured on the 61 recorded accepted trades, the edge metric is the
    # weakest candidate: its top quartile realised WR 0.667 against 0.933 for
    # the third quartile, i.e. the largest claims are the most inflated --
    # exactly what edge_haircut documents. Sizing on ev/$, which prices the
    # payoff asymmetry in, roughly doubles recorded PnL for ~35% more
    # drawdown. Kept on "edge" until the live sample says otherwise.
    sizing_metric: Literal["edge", "ev"] = "edge"
    min_stake_usdt: float = 5.0
    max_stake_usdt: float = 20.0
    # sizing_mode="fraction" knobs. 4% of bankroll, chosen on DRAWDOWN rather
    # than return: the old $5-10 band risked 9.0% of a $75 account per trade,
    # and that fraction does not fall as the account shrinks. Simulated over the
    # recorded trades (3000 paths, 19 trades/day), at 30 days:
    #   band $5-10   median DD 29.7%   p95 DD 72.4%   p95 DD 100.2% at +0.15/$1
    #   f=4%         median DD 23.5%   p95 DD 35.6%   p95 DD  49.1% at +0.15/$1
    # and 4% also earns more in the central case ($1,818 vs $910), so it
    # dominates the band on both axes there. The drawdown gain holds at BOTH
    # edge assumptions because variance is far better estimated from 70 trades
    # than the mean is; the profit gain does not.
    stake_fraction: float = 0.04
    # Ceiling on the fractional stake, so compounding cannot run the stake up
    # to a size the venue's book cannot absorb in one FOK order. Also the
    # ceiling once a sizing tilt is on.
    # 0 = NO CAP, the repo's convention for every other limit (daily_loss_cap,
    # max_consecutive_losses). Disabled deliberately on 2026-09-13: at a $25
    # ceiling the cap binds from a $558 bankroll at the main arm's 4.48% and
    # from ~$186 at the cross arm's 3x, which turns percentage sizing back into
    # fixed dollars exactly as the account grows -- the failure
    # stake_for_fraction exists to avoid (a fixed stake is a RISING fraction
    # after losses).
    #
    # NOTE both bare `min(x, stake_cap_usdt)` sites were fixed alongside this.
    # stake_for_fraction already guarded `if cap > 0`, but _stake_for's tilt
    # clamp and the cross multiplier did not, so a 0 here would have clamped
    # every stake to zero and silently stopped the bot.
    stake_cap_usdt: float = 0.0
    # When the fraction cannot reach the venue minimum ($1), False skips the
    # trade and True stakes the minimum instead. Off by default because
    # rounding up raises the real fraction -- at $28 a $1 floor is 3.6%, not
    # 2% -- but an account too small to size is not the same as an account too
    # small to trade, and the venue's floor is $1 at any size.
    stake_floor_to_minimum: bool = False
    # Sizing tilt: multiply the stake by a factor that depends on how undecided
    # the round still is, then re-clamp. Available in every sizing mode, so it
    # redistributes the stake without having to change the base rule.
    #   none  - no tilt
    #   inv_z - stake UP when |z| is small. Justified on the recording: per-$1
    #           return FALLS with |z| (+1.03, +0.49, +0.06, +0.17 by quartile)
    #           -- the mirror of Polymarket, whose CLOB prices small moves
    #           correctly. Out of sample, at matched average exposure, every
    #           tilt exponent from 0.5 to 1.5 beat flat on BOTH slices (test
    #           $134 -> $157..$314) with the gain monotone in the exponent, so
    #           this is a plateau and not a fitted peak. It costs drawdown:
    #           -9.8% flat vs -10.8% to -22% tilted.
    # Turning it on widens the effective stake range beyond [min_stake_usdt,
    # max_stake_usdt] up to [min_order_usdt, stake_cap_usdt].
    # RE-MEASURED 2026-09-12 (tools/arm_overlap --tilt, 1,229 rounds, live
    # execution, band 0.45-0.80, $50 bankroll). The tilt's own rationale is that
    # per-$1 return FALLS with |z|, so small |z| earns a bigger stake. On this
    # recording its contribution to per-$1 return FLIPS SIGN between halves --
    # +0.0941 vs +0.0889 in the first, +0.0842 vs +0.0988 in the second, pooled
    # +0.0894 vs +0.0934 -- so the allocation claim is NOT supported and must not
    # be quoted as one.
    #
    # What it certainly does is raise the average stake from $1.00 to $1.12, and
    # that is where its +6.8% of daily dollars comes from (+$17.58 vs +$16.46 at
    # $50). Same expectation, more variance: for independent positive-EV bets a
    # FLAT stake at the same mean has the same expectation with strictly lower
    # variance, so the tilt is dominated unless its allocation is real -- and it
    # is not measurable here. Hence "none" with stake_fraction raised to 0.0224
    # to keep the average stake, which is a transparent leverage choice rather
    # than a knob that claims to allocate. `stake_tilt` stays switchable: the
    # decisive test is per-|z|-quartile return, not the pooled aggregate.
    stake_tilt: Literal["none", "inv_z"] = "none"
    # The multiplier is clamp(reference/|z|, tilt_min, tilt_max). NOTE the clip
    # makes its MEAN exceed reference/median|z| -- with the recording's |z|
    # distribution, reference 0.65 leaves the mean multiplier at 1.30, i.e. a
    # silent 30% stake increase. 0.30 is the value that makes the mean
    # multiplier 1.0 on the 70 replayed trades, so turning the tilt on changes
    # only the SHAPE of the bets, which is the part that was tested at matched
    # exposure: same mean stake ($6.76 vs $6.83), PnL +$385.57 vs +$252.39
    # (+53%), drawdown -$22.81 vs -$20.14, PnL/DD 16.9 vs 12.5.
    # Caveat: |z| = |ln(S/K)|/(sigma*sqrt(tau/60)) shrinks when volatility
    # rises, so a fixed reference drifts exposure with the regime. Re-derive it
    # if the realised |z| distribution moves.
    tilt_reference_z: float = 0.30
    tilt_min: float = 0.5            # bounds, so one trade cannot dominate
    tilt_max: float = 2.0
    # Edge at which the maximum stake is reached. The gate accepts from
    # min_edge up; recorded accepted edges ran 0.12 -> 0.22.
    stake_edge_hi: float = 0.22
    # The same anchors for the ev/$ metric: 5 cents of expected profit per
    # dollar at the floor, 40 cents at the ceiling.
    stake_ev_lo: float = 0.05
    stake_ev_hi: float = 0.40
    # Flat fallback, used only when sizing_mode is not "edge" and no stake can
    # be computed.
    stake_usdt: float = 10.0
    # Venue minimum order size. Measured, not assumed: 54 live orders of exactly
    # $1.00 filled with real order ids (1.04-3.86 shares), so the floor is $1.
    # The old $5 was self-inflicted and leaked into the balance gate and the
    # Kelly minimum, silently blocking stakes the venue would have accepted.
    min_order_usdt: float = 1.0
    kelly_fraction: float = 0.25
    # Venue tolerance on price movement between quote and fill. 100 bps was
    # producing FOK failures ("Failed to execute the market order") when the
    # book moved mid-flight; 300 bps cuts those while the 0.08 edge floor
    # still leaves room for a 3% slip on a winning trade.
    slippage_bps: int = 300
    # A FOK order that fails is usually just the book moving between the quote
    # and the execution -- transient, and worth one more shot with a FRESH
    # quote rather than sitting out the round. Total attempts per decision,
    # including the first; 1 disables retrying. Each attempt costs a quote plus
    # an order round trip (~2s), so 3 is what fits inside a 10s candle-open
    # window. Retries only ever follow a failure the venue confirmed, never an
    # order whose outcome is unknown (see execution/live.RETRYABLE_FAILURES).
    order_max_attempts: int = 3
    # Small pause between attempts: re-quoting into the same instant that just
    # failed tends to fail the same way.
    order_retry_delay_s: float = 0.75
    # Backoff after all attempts are spent, so a persistent rejection cannot
    # re-fire on every 1 Hz tick for the rest of the round.
    order_cooldown_s: float = 30.0

    # --- execution -----------------------------------------------------------
    # "market" takes the ask with a FOK; "limit" posts at `limit_offset` below
    # the signal's quote and rests for `limit_wait_s`, cancelling whatever has
    # not filled; "limit_take" crosses the book at max(quote, fair -
    # safety_margin) and fills at the book's ask.
    # The venue accepts LIMIT only with GTC, and -- verified against
    # the documented schema and the live venue -- a LIMIT needs `priceLimit`,
    # has no ~1.5 USDT minimum (MARKET does), and fills at OUR price instead of
    # the spread. Measured with one real crossing order (2026-09-12): a limit of
    # 0.18 against an ask of 0.15 filled at 0.1526, i.e. at the BOOK's ask, so a
    # taker limit reaches real liquidity while MARKET prices off the venue's
    # DISPLAYED price -- which lags its own book by up to 0.40 and left the quote
    # above the ask in 5-7% of live samples. Market fills averaged +0.16 above
    # the ask on offer; limit fills -0.014. "limit_take" also cannot pay more
    # than the price we chose, which a slippageBps cap on a market order cannot
    # promise.
    order_type: Literal["market", "limit", "limit_take"] = "market"
    limit_offset: float = 0.01
    limit_wait_s: float = 20.0
    # Kelly sizes against this. Kept explicit rather than derived from the stake,
    # so raising the bankroll is one edit and sizing scales as intended.
    bankroll_usdt: float = 40.0

    # --- edge gate ---------------------------------------------------------
    # Fair-value model producing P(Up). "digital" is the original closed-form
    # model; the rest are the arena bots ported from polymarket-bot-arena
    # (see btcpredict/models/__init__.py for the full list).
    pricing_model: str = "digital"
    # Opt-in per-side hybrid gate: Down candidates are priced by
    # `pricing_model`, Up candidates by `up_model` (prototype: drift_digital).
    # Equal names disable the hybrid and keep the single-model gate.
    up_model: str = "digital"
    # Rising-regime gate for Up: skip Up when the 1h log return is above this
    # (0 disables). Re-tested on the full recording once Up went back to the
    # calibrated driftless model (tools/gate_sweep.py, 514 BTC + 444 ETH rounds):
    # the lever is nearly inert. On BTC it removes 1 of 14 Up trades at 0.005
    # (+$16.4 vs +$15.4 total per-$1, test slice identical), thresholds of 0.01
    # and above are indistinguishable from OFF, and ETH has 7 trades total so it
    # cannot speak. The original justification -- "Up WR 0.50 rising vs 0.82
    # flat/down, recovers ~+$3" -- was measured on 25 rounds under the old Up
    # model and does not reproduce.
    # Kept at 0.005 because it is mildly positive on both assets and costs
    # nothing measurable; treat it as untuned, not as a validated filter.
    up_regime_gate: float = 0.0
    # Fallback only: the fee comes from the measured curve
    # fee_bps = 180 * min(1, (1-px)/px) (see edge/signal.py). 180 bps is the
    # curve's cap, so it is the conservative fallback when no fee is known.
    fallback_fee_bps: int = 180
    # With the authenticated order book we trade against the real ask, so no
    # spread guess is needed. This only applies when the book is unavailable.
    assumed_half_spread: float = 0.005
    # The signal's HURDLE: this much extra edge is demanded before an entry is
    # taken (edge/signal.py). Raising it makes the arm take fewer, stronger
    # trades -- a different lever from the price ceiling below, and it has never
    # been swept. Do not reuse it as a take-price margin.
    safety_margin: float = 0.01
    # The TAKE ceiling: `limit_take` crosses the book only when the ask sits at
    # least this far under our own digital fair. Its own setting precisely so it
    # cannot be confused with the hurdle above.
    #
    # `tools/backtest_impulse_orders --sweep-margin`: 96 entries / 1,112 rounds,
    # $1 an entry, the full battery on every row:
    #   margin  n   fill   WR      EV/$1   tot$   pooled-t  daily-t  halves      drop1
    #   0.00   65   68%  61.5%   +0.319  +20.8    1.53      3.44   +0.25/+0.39  +0.37
    #   0.01   60   62%  61.7%   +0.345  +20.7    1.55      3.73   +0.20/+0.49  +0.37 <- was live
    #   0.02   57   59%  63.2%   +0.398  +22.7    1.72      4.29   +0.25/+0.54  +0.41
    #   0.03   54   56%  64.8%   +0.441  +23.8    1.82      4.68   +0.30/+0.59  +0.44
    #   0.04   46   48%  65.2%   +0.580  +26.7    2.09      6.03   +0.43/+0.73  +0.56 <- live
    #   0.06   37   39%  70.3%   +0.694  +25.7    2.19      7.98   +0.69/+0.69  +0.61
    #   0.08   35   36%  74.3%   +0.791  +27.7    2.42      8.26   +0.79/+0.79  +0.70
    #   0.10   28   29%  75.0%   +0.969  +27.1    2.46      6.25   +0.93/+1.01  +0.76
    # Monotone in win rate, EV, day-clustered t AND both halves as the margin
    # rises: the edge lives in the entries where the ask is FAR under fair, which
    # is exactly what a venue whose price lags its own book should look like. The
    # win rate rising while the average price FALLS (0.543 -> 0.453) is the
    # signature of signal rather than of a fitting artefact.
    # `tot` (what the whole sample pays at $1 an entry) peaks across 0.04-0.08 and
    # those rows are within noise of each other, so 0.04 is chosen for its sample
    # (48% fill, ~9 entries/day), not for the peak.
    limit_take_margin: float = 0.04
    # How far above the ask we measured a taker limit may ever reach. The ceiling
    # itself is our own fair, which overshoots on fast moves; a real fill on
    # 2026-09-12 13:02 crossed a 0.90 limit against an ask of 0.7326 and landed at
    # 0.8919 (ev_at_fill -0.195). One tick of tolerance keeps the order crossable
    # while making it impossible to pay the price that invalidates the thesis.
    limit_ask_tick: float = 0.01
    # Never buy a favourite richer than this: the payoff asymmetry out there
    # exceeds what the model's calibration can support.
    max_quote: float = 0.90
    # Fee is per-price: fee_bps = 180 * min(1, (1-px)/px), capped at 180 bps
    # below px 0.50. Cheap longshots pay the cap; favourites are cheap. This
    # floor and the re-sweep under the true curve agree on 0.25 (+$8.98 vs
    # +$7.66 at 0.15, +$5.90 at 0.35 on the 94-round recording).
    # Signal-strength floor, in units of the remaining move:
    # |ln(spot/strike)| / (sigma*sqrt(tau/60)). 0.0 = off.
    # Motivated by an independent venue (Polymarket 5m crypto, 1,292 trades,
    # 2026-03-06..12) where per-trade return rises monotonically with |z| and
    # the weak-signal trades actually LOSE money. It does NOT reproduce here:
    # in tools/walk_forward --sweep moneyness every floor from 0.25 to 1.50
    # scores worse than 0.0 on both train and test (train +0.303/trade at 0.0
    # vs +0.027 at 0.5; total PnL +15.47 -> +0.96). Our edge comes from the
    # venue's quote lagging spot, which is largest exactly when the move is
    # SMALL; Polymarket's CLOB prices those rounds correctly (realised WR 0.374
    # against its own price of 0.359). Kept at 0.0, do not re-tune without a
    # bigger recording.
    min_moneyness_sd: float = 0.0
    min_quote: float = 0.25
    # Walk-forward check (tools/walk_forward.py, 60/40 time split): widening
    # this to 0.20 looks better on the pooled window (+35.8 vs +30.3 per $1)
    # but NOT out of sample -- test-slice per-trade is +0.47 at 0.20 against
    # +0.69 at 0.25, with a deeper drawdown (-2.04 vs -1.02). So 0.25 stays.
    # The code comment about sub-0.25 "losing money" predates edge_haircut.
    # Replay evidence under the measured fee curve (backtest_candle_open,
    # BTC-only): the winning variant is the ungated price-catcher -- no
    # candle-open requirement, a BIG edge floor instead. Small edges are
    # noise; big edges are the quote lag, and they can appear at any second.
    # BTC-only joint re-tune (471 rounds, 2026-09-10): live runs min_edge 0.12
    # with edge_haircut 0.85 and side_filter Down (set in .env): +$19.34,
    # mdd -$1.35, WR 0.80, halves +8.42/+10.91, bootstrap CI [+7.35,+31.50].
    # --- probe arm: measure the trades the selectivity gate declines --------
    # The gate's selectivity IS the frequency ceiling (245 rounds/day, positions
    # in ~105, ~99 fills). Whether that ceiling costs money cannot be answered
    # from the recorded `quote`: our real fills beat the quoted ask by ~1% of
    # stake (138/455 were maker fills paying no fee), so a quote-priced backtest
    # rejects cells that may actually pay. Sweeping the model's own edge on
    # 88,102 recorded decisions, EVERY threshold loses at the quote (-0.5% to
    # -4.2% of stake) while the same selections at real fills make +0.054/share.
    #
    # So the declined cells get traded at a size small enough to lose on purpose,
    # recorded as mode='probe'. Promotion to the live arm requires day-clustered
    # t > 2 AND drop-best-day > 0 on real fills. Never probes a decision declined
    # for exposure or risk reasons -- those are real limits, not selectivity.
    probe_enabled: bool = False
    probe_stake: float = 1.0
    probe_max_per_day: int = 150
    # --- double down: add a unit once the position is already winning -------
    # MEASURED 2026-09-12 before shipping (tools/arm_overlap --double-at, 1,177
    # rounds, live execution model, band 0.45-0.80): the added unit scores
    # +0.0686/$1 at t 2.05, and total profit rises ~36% -- but it is bought at a
    # WORSE price than the first unit (~0.69 against ~0.61), so its payoff is
    # 1.44:1 where the first unit's is 1.64:1, on ~49% more capital, so return
    # per dollar FALLS ~8%. It also FAILS the half-sample check (second half
    # +0.003 against the base arm's +0.112), which is the tell that made this
    # repo reject edge_haircut=0.9.
    #
    # It is also the first thing here to relax the one-position-per-round
    # invariant, so it ships CAPPED and booked as mode='double' to be settled
    # FORWARD rather than trusted. Kill it if mode='double' EV/$1 <= 0 after
    # ~200 booked adds; its `_double_tick` docstring carries the same note.
    double_enabled: bool = False
    # How far our side must have RISEN in price since entry to trigger the add.
    # There is deliberately NO minimum age: measured 2026-09-12
    # (`tools/arm_overlap --double-after-s`), requiring the position to be 5/15/
    # 30/60s old before the add may fire LOWERS the add's EV monotonically --
    # +0.0703 / +0.0700 / +0.0667 / +0.0637 / +0.0287 per $1, and its profit
    # contribution from +$6.08/day to +$1.81/day. The price gap IS the signal: by
    # the time a position is old enough to look "confirmed", the book has
    # repriced further and less edge is left. So an add fires as soon as the
    # price is up `double_at`, which can be ~1s after the entry (measured live:
    # entry 0.519 -> add 0.640, 0.9s). That is not "pressing a winner", it is
    # buying when the entry filled cheap relative to the next tick -- and that is
    # the only version of this idea that pays. Do NOT add an age floor.
    double_at: float = 0.10
    # The add's size as a multiple of the first unit.
    double_stake_mult: float = 1.0
    # Its own daily cap: 0 = UNCAPPED, the same convention as the guards' limits.
    # The 10 originally here was arbitrary -- an experiment guard, never
    # measured. Measured 2026-09-12: the cap is
    # what stands between the two numbers -- uncapped the add fires on 49% of
    # entries (~86/day) and lifts profit ~35% (+$6.07/day at $50); at 10/day it
    # fires on 7% and lifts ~8% (+$1.32/day). Uncapping therefore multiplies the
    # add's contribution by 4.6x, and is a LEVERAGE decision as much as a signal
    # one: the per-round stake averages 1.49x the base. The daily loss cap still
    # bounds a bad day, which is what makes uncapping defensible at all.
    double_max_per_day: int = 0
    min_edge: float = 0.09
    # Fraction of the model's disagreement with the venue that is treated as
    # real: p_used = quote + edge_haircut * (fair - quote).
    #
    # NOT a fitted constant, on purpose. Measured on 31h of btc-5m recordings
    # the realised edge is roughly half the claimed edge (claimed 0.096 ->
    # realised 0.061 in the first half of the window, +0.007 in the second),
    # but every band's 95% CI spans zero once errors are clustered by round,
    # and the two halves are not statistically distinguishable. So 0.5 is a
    # deliberate safety haircut, not an estimate: the sign of the bias is
    # solid (every band, both halves, the model claims more than it delivers),
    # the magnitude is not. min_edge is set against the HAIRCUT edge, so
    # changing this knob alone changes which trades qualify.
    #
    # DEFAULT IS 0.5 (a deliberate safety haircut, not a fitted constant).
    # Measured on 31h of live btc-5m recordings the realised edge is roughly
    # half the claimed edge, so believing half of the disagreement is the
    # conservative choice. min_edge is compared against the HAIRCUT edge: with
    # min_edge 0.09, a haircut of 0.5 demands a raw edge of 0.18 and cuts the
    # replayed trade count from ~155 to ~13 (WR 0.77, mdd -$1.07, both halves
    # positive). 0.7 is the more-active compromise (56 trades, mdd -$5.09).
    # Always change the pair together: 0.5 with min_edge 0.045 reproduces the
    # haircut-off selection exactly.
    edge_haircut: float = 1.0
    # Which sides to trade. "both" is the neutral default; the live choice is
    # "Down" (set in .env). Evidence (BTC-only, measured fee curve): across
    # every configuration and BOTH chronological halves, Up trades are where
    # the money is lost -- old 0.08/4/240: Up -11.07 vs Down +13.54; 0.09/2/210:
    # first half Up +0.73 vs Down +25.52, second half Up -6.62 vs Down +2.13.
    # The venue's Up side is persistently rich relative to the model (the crowd
    # is long-biased), so the model's Up edge is an illusion more often than
    # not. Down-only keeps the first half's PnL and turns the losing second
    # half positive: n=95, WR 0.60, +$26.46, mdd -$9.05, halves +23.38 / +3.08,
    # bootstrap 95% CI [+2.41, +50.97] -- the first CI here to exclude zero.
    side_filter: Literal["both", "Up", "Down"] = "both"
    # The market beats us in 88% of rounds inside the final minute (p<0.0001).
    # Trading there is a measured loss, so the window starts at 60s.
    min_tau_s: float = 60.0
    # The upper end of this window is at `max_tau_s` below, where the 2026-09-12
    # re-measurement lives -- the candle-era note that used to sit here is
    # superseded by it.
    # --- entry logic -------------------------------------------------------
    # "model": price the round, trade when the model's fair value shows an edge
    #          against the venue's quote (everything below applies).
    # "impulse": ignore the model. Trade the underlying's own move: when the
    #          spot prints |impulse_bps| over impulse_window_s, buy that side at
    #          the venue's ask and hold. Measured 2026-09-12 on 96,800 recorded
    #          ticks: 4-8 bps impulses paid +8% to +21% per $1 in BOTH halves of
    #          the sample while the model gate paid +1.3% with a negative second
    #          half; sub-2 bps impulses lose to the fee and are refused here.
    #          One entry per round; the model's gate is still evaluated for the
    #          record so the A/B can be reconstructed offline from `quotes`.
    # "both": run BOTH gates. The impulse owns a round when it fires (it is on
    #          the websocket clock, so it fires before this poll); otherwise the
    #          model's gate decides, exactly as in "model". The guard's
    #          one-position-per-round rule is what keeps them from doubling.
    #          MEASURED 2026-09-12 on 1,255 rounds / 4.4 days, rebuilt with the
    #          LIVE sigma estimator (sigma_1m_by_round -- NOT arm_overlap's
    #          rolling_sigma, which does not match VolEstimator.sigma_1m):
    #            model alone   n=806  ev/$1 +0.0273  t 0.93  halves +0.056/-0.001
    #            impulse alone n= 76  ev/$1 +0.3773  t 1.02  halves +0.051/+0.614
    #            both          n=830  ev/$1 +0.0635  t 1.49  halves +0.049/+0.078
    #          "both" is the only one whose SECOND half improves, and the arms
    #          collide on just 52 of 830 rounds (model earlier 38, impulse 14),
    #          so falling through does not starve the impulse. The gain rests on
    #          ~38 impulse entries, so it is thin: t is 1.49, under this repo's
    #          t=2 bar. Shipped on an explicit instruction to ship the most
    #          profitable backtest combination, not because it is proven.
    # "off" disables the MAIN (model) arm while leaving cross running. The
    # option exists; the DEFAULT is not being changed on live evidence. Six live
    # days -- two of them a weekend, all of them one config era -- cannot decide
    # this, and a drop-best-day run over six days is not this repo's bar, it is
    # a coin flip with extra steps. The model arm is judged on historical data
    # like everything else; see the L2 measurement below cross_max_depth_bps.
    entry_mode: Literal["model", "impulse", "both", "off"] = "model"
    # The MODEL arm's regime window, in bps of how far the round has travelled
    # from its open to the entry instant. 0/0 = OFF.
    #
    # Its lifetime margin is famously zero -- win 0.64971 against an ask of
    # 0.64967 over 688 live fills -- but that is the AVERAGE of two opposite
    # halves, and the split is the opposite of the cross arm's:
    #     range-so-far    n     ask     win   margin   per$1   total $
    #       <=5 bps     439   0.593   0.595   +0.002  -0.000    -1.24
    #       5-8         112   0.683   0.750   +0.067  +0.092   +60.77
    #       8-12         66   0.710   0.758   +0.048  +0.088   -15.48
    #       12-20        45   0.729   0.778   +0.049  +0.090   +43.44
    #       >20          24   0.738   0.708   -0.030  -0.083   -28.12
    #
    # Day-clustered on its own live fills, 7 days:
    #     all fills        n=686 per$1 -0.0070 day-t -0.11 drop-best -0.0406
    #     range <= 3.6     n=339 per$1 -0.1274 day-t -0.77 drop-best -0.2364
    #     range 5-20       n=223 per$1 +0.1160 day-t +1.80 drop-best +0.0647
    #     range > 20       n= 24 per$1 -0.1769 day-t -0.77 drop-best -0.3135
    # 5-20 carries +$88.73 of the arm's +$59.37 lifetime total and is positive
    # on 6 of 7 days. Outside it the arm is a measured loser in BOTH directions.
    #
    # This is the symmetric counterpart to cross_max_round_range_bps: the cross
    # arm owns calm rounds (+0.661/$1 under 5bps, -0.074 when trending), this
    # one owns moving rounds. Each arm's dead zone is the other's edge.
    #
    # UPPER BOUND CORRECTED 20 -> 12 on 2026-09-14. The window above was built
    # on 686 LIVE FILLS; replaying the arm's real evaluate() gate over the
    # recorded history gives 1,159 entries and a different top end:
    #     range    n    ask    win     EV     day-t  drop-best
    #     <3      513  0.591  0.635  +0.066   +0.31   -0.021
    #     3-5     253  0.629  0.652  +0.041   +0.40   -0.018
    #     5-8     198  0.646  0.737  +0.146   +2.18   +0.085
    #     8-12    105  0.648  0.762  +0.186   +1.67   +0.078
    #     12-20    70  0.662  0.700  +0.007   -0.62   -0.221   <- was inside
    #     >20      20  0.691  0.600  -0.154   +0.33   -0.046
    #
    # The raw replay appears to say "drop the window entirely" (ALL: EV +0.078,
    # day-t +4.95). That is an artifact of pricing at the BOOK ask. Priced at
    # what we actually fill at, the arm is NEGATIVE everywhere except here:
    #     window    n   | add +0.088 EV  growth | mean-revert EV  growth
    #     ALL     1159  |    -0.058     -1.632  |    -0.039      -1.159
    #     0-12    1069  |    -0.051     -1.345  |    -0.037      -1.000
    #     5-20     373  |    -0.005     -0.079  |    +0.036      +0.260
    #     5-12     303  |    +0.018     +0.095  |    +0.053      +0.332  <- ships
    #
    # So the lower bound at 5 is what makes this arm viable at all, and 12 is
    # where it stops paying. Note the arm is only MARGINALLY positive even here
    # (+0.018/$1 under the harsher slippage model) -- it is a supporting arm,
    # not a driver.
    model_min_round_range_bps: float = 5.0
    model_max_round_range_bps: float = 12.0
    # --- stale-display maker ------------------------------------------------
    # MEASURED 2026-09-12, lead-lag cross-correlation over 1,372 rounds of the
    # 1 Hz quote tape (corr of the venue's implied-probability change against
    # the model-fair change from L seconds earlier):
    #     BOOK    (up_ask/down_ask)     peak at L = +0s, corr 0.661
    #     DISPLAY (up_price/down_price) peak at L = +2s, corr 0.277
    # The executable book does NOT lag spot. The venue's DISPLAYED price -- the
    # number its UI shows -- lags by ~2s, decaying over ~5s. So during that
    # window the display is a stale quote on a side whose value has moved.
    #
    # It is an EXECUTION edge, not a signal. Keyed on the same rule and priced
    # three ways (tau 60-210s, one entry per round, venue fee paid):
    #     fair-display >= 0.20, ask-display >= 0.20   n=309
    #        filled at the BOOK ASK   EV/$1 +0.0175   t 0.47   <- no edge
    #        filled at the MID        EV/$1 +0.3449   t 5.88   <- the whole edge
    # Buying at the ask earns nothing; the money is entirely in getting a price
    # below the book. That matches the live record (fills at or under the
    # display returned +0.2318/$1 against -0.0108/$1 for the rest) and the
    # maker_limit_px docstring's own 43%-fill measurement.
    #
    # UNKNOWN, and the reason this ships OFF: the FILL RATE. The recorded tape
    # has no depth or queue position, so it cannot say how often a resting bid
    # is actually hit. That number decides whether this pays, and it can only
    # be measured live. Every unfilled attempt is logged and keeps its decision
    # row (acted=0) so the rate is recoverable from the database.
    stale_maker_enabled: bool = False
    # How stale the display must be before resting: ask - display.
    # RE-MEASURED 2026-09-12 against the MODEL ARM's own 862 entries, which is
    # what the hook actually sees. The first values here (0.05/0.20) came from a
    # scan of every quote and fired on just 7 of 862 entries -- 1%, i.e. inert.
    # Gated to real entries, coverage and P&L (200 random fill draws, $50 start,
    # baseline taker $77.11 / maxDD $18.70):
    #     gap>=0.05 edge>=0.20    7 entries ( 1%)   no material change
    #     gap>=0.02 edge>=0.05  141 entries (16%)
    #     gap>=0.01 edge>=0.02  331 entries (38%)   fill 43% -> mean $86.51
    #                                               fill 57% -> mean $93.50
    # At 43% fill (the maker_limit_px measurement) it beats the taker in 65% of
    # draws; at 57% (limit@fair) 79%. p10 stays BELOW baseline until ~70% fill,
    # so the downside is real.
    #
    # NOT a filter. Skipping the non-stale entries instead of resting on the
    # stale ones raises EV per trade (+0.0451 vs +0.0314) but cuts volume 61%,
    # and compounding turns that into $66.50 -- worse than doing nothing. It
    # halves drawdown, which is the only thing it buys.
    stale_maker_min_gap: float = 0.01
    # How wrong the display must be against our own fair: fair - display.
    stale_maker_min_edge: float = 0.02
    # Posted at display + this. Larger = better fill rate, worse price; the
    # measured edge is quoted at the display and at the mid, so this is the
    # knob that trades one against the other.
    stale_maker_offset: float = 0.01
    # How long the bid may rest before it is cancelled.
    stale_maker_wait_s: float = 20.0
    # Window the impulse is measured over, in seconds.
    # MEASURED 2026-09-12 (`tools/backtest_impulse_orders --sweep-window`, 1,128
    # rounds, 85-229 triggers depending on the window, priced at the ask and
    # restricted to what the live path takes -- ask <= fair - limit_take_margin):
    #
    #   window  triggers  fills  WR      px      EV/$1   tot     pooled-t  drop1
    #   0.5s      85       43    65.1%  0.4753  +0.6329  +27.22   +2.15   +0.61
    #   1.0s      85       43    65.1%  0.4733  +0.6379  +27.43   +2.17   +0.64
    #   2.0s     115       48    66.7%  0.5027  +0.6384  +30.64   +2.30   +0.43
    #   3.0s     173       55    69.1%  0.5275  +0.5728  +31.51   +2.34   +0.45
    #   5.0s     229       61    60.7%  0.5403  +0.3748  +22.86   +1.63   +0.15
    #
    # 1.0 was never swept. On a 1 Hz tape it is a SINGLE TICK, so it only ever
    # caught jumps; 2.0 keeps the EV per dollar identical (+0.638) and fills 12%
    # more entries, i.e. the extra volume is free. The price we pay rises
    # 0.473 -> 0.540 across the sweep, which is the mechanism: past ~3s the venue
    # has repriced and the move is old news (EV decays to +0.375 at 5s).
    # `impulse_bps` takes the newest tick at least `window_s` old, refusing a
    # reference staler than window + 1.5s -- so this is a measured span, not the
    # previous tick (pinned by tests/test_impulse_window.py).
    impulse_window_s: float = 2.0
    # Minimum |move| in bps to trade. 4 is the measured floor; 2 is noise.
    impulse_bps: float = 4.0
    # Price sanity for impulse entries: refuse absurd quotes, but do NOT reuse
    # min_quote/max_quote -- those are the model's band (0.50-0.90) and would
    # refuse both tails of exactly the impulses that pay.
    impulse_min_price: float = 0.05
    # MEASURED 2026-09-12 (`tools/backtest_impulse_orders --by-price`, 96
    # entries / 1,123 rounds, priced at the ask the arm actually crosses, and
    # restricted to what the live path takes, ask <= fair - 0.04):
    #     ask 0.00-0.50  n=23  EV/$1 +1.0568  daily-t +4.40  drop1 +1.30
    #     ask 0.50-0.65  n= 9  EV/$1 +0.2971  daily-t +2.22  drop1 +0.25
    #     ask 0.65-0.75  n= 5  EV/$1 +0.1062  daily-t -0.21  drop1 -0.32
    #     ask 0.75-0.85  n= 4  EV/$1 -0.0813  daily-t -0.55  drop1 -0.39
    #     ask 0.85-1.01  n= 5  EV/$1 -0.1035  daily-t +0.07  drop1 -0.07
    # The two cells above 0.75 are the only NEGATIVE ones, and the reason is
    # arithmetic, not fitting: the fee fixes a break-even win rate of px/0.98,
    # so at 0.85 a win pays +0.15 and it needs 87% of them. Those entries are
    # also where our own fair claims the LARGEST edge -- the same inflation the
    # live record shows (fills bucketed by predicted EV: +0.032 -> +0.254
    # realised, but +0.107 -> -0.244, n=121). So the band stops at 0.75.
    # `--by-move` says do NOT raise impulse_bps instead: the 4-5bps cell is the
    # weakest (EV +0.096) but it is still positive and it is 58% of the volume,
    # so cutting it would lose +$5.40 of the sample's +$24.
    impulse_max_price: float = 0.75
    # Evaluate the trigger on the price feed's own clock instead of on the poll.
    # Measured 2026-09-12 from the VPS: the venue answers in 5.3 ms (3 round
    # trips = 16 ms), while the poll loop looked at the price once every 1002 ms
    # -- so the poll WAS the entire trigger-to-order latency, and the side being
    # bought keeps drifting for seconds after the impulse. With this on, the
    # trigger is checked on every websocket update (~33/s) and the order goes
    # out ~20 ms after the window closes rather than ~500 ms after it.
    impulse_on_tick: bool = True
    # How an impulse entry is EXECUTED.
    #   taker - cross the book at the ask (`take_px` = the ask we read).
    #   maker - post at our own fair, below the ask, and wait.
    # Same 119 impulse entries on 1,187 recorded rounds, priced four ways
    # (`tools.backtest_impulse_orders`):
    #   take the ask   +0.0590/$1  pooled-t +0.44  daily-t +0.41  halves -0.04/+0.15
    #   post at fair   +0.5890/$1  pooled-t +2.23  daily-t +4.24  halves +0.40/+0.77
    #                  (43% fill, and 66.7% of the ones that fill WIN)
    # The taker row is noise; the maker row is the only arrangement in this repo
    # that clears the battery on recorded prices. The reason is selection: a taker
    # pays the spread to chase a move the whole book can see, while a resting bid
    # fills only when someone crosses to us -- exactly when the venue is stale.
    # Makers also pay no fee (the schedule is takerOnly).
    # Note the row NOT to trust: pricing entry at the venue's DISPLAYED price
    # shows +0.8462/$1 and +100.70 total. It is not implementable, and the 15x
    # gap between it and the ask rows is the trap this repo keeps re-learning.
    impulse_execution: Literal["taker", "maker"] = "taker"
    # A limit is DONE at this fill fraction; the remainder is cancelled.
    # MEASURED 2026-09-13 over 238 real live limit orders. The wait loop only
    # broke at >= 0.999, so a 98-99% fill -- a filled order by any practical
    # measure -- sat out the rest of limit_wait_s waiting for the last 1-2%:
    #     100%% fills (loop breaks early)  n=119  median  5.0s  76% within 8s
    #     partial fills (wait out clock)   n=119  median 21.0s   3% within 8s
    # Exactly half of all orders paid a ~16s penalty for nothing. That matters
    # three ways: the cross arm fires as little as 3s before the bell and its
    # wait is capped by the remaining tau, so the penalty turned fills into
    # cancellations; buy_limit blocks the poll loop while it waits, which is
    # what produced the 80-131s stalls between 60s stats lines; and the shared
    # order cooldown then fired on the "failure".
    # The remainder is cancelled on any partial (it already was), and the fill
    # is booked from the venue's own filledUsdtAmount/filledShareQty, so a 95%
    # fill books 95% of the stake rather than a guess.
    limit_fill_break: float = 0.95
    # How long a resting impulse bid may wait for a fill before it is cancelled.
    # The trigger looks several seconds back, and the round has ~60-210s left, so
    # waiting is cheap; a bid that outlives the round is not, hence the caller
    # also clamps this to the remaining time.
    # Concurrent rounds the IMPULSE arm may hold, in its OWN budget. It does
    # not share `max_open_rounds` with the main arm: the main arm enters at tau
    # 60-210 and holds to the bell, which spans this arm's whole window, so one
    # shared slot meant whichever fired first silenced the other for the round.
    # 1 keeps this arm's own exposure exactly where it was; what changed is
    # that it is no longer spent by a different arm.
    # OFF as of 2026-09-13. The arm is NEGATIVE at a realistic fill.
    #
    # Replayed on the bot's own recorded book, 145 triggers over 6 days, with
    # the main arm's real fills for context:
    #     fill AT THE TRIGGER   fill 0.412  win 0.434  EV/$1 +0.1729  day-t  0.33
    #     fill +1s (as it lands) fill 0.475 win 0.434  EV/$1 -0.0479  day-t -1.27
    # One second of latency moves the fill 0.412 -> 0.475 and flips the sign.
    # 43.4% wins against a 47.5% break-even is -$6.95 over those 6 days.
    #
    # The note above claiming "+8% to +21% per $1 in BOTH halves" is not wrong,
    # it is measured AT THE TRIGGER PRICE -- the same no-latency artifact that
    # made the cross arm look like +0.24/$1 when it is +0.15 at a real fill.
    # Raising the bar to impulse_bps=6.0 does recover +0.1124/$1, but on 51
    # triggers at day-t -0.79, which is "not established" rather than "good".
    #
    # It also has no live record to appeal to: in the bot's entire history it
    # placed ONE order, on 2026-09-13, and only because per-arm risk pools let
    # it past `guard.check()` for the first time. Before that the main arm held
    # the only slot (max_open_rounds=1) and this arm was blocked every time it
    # fired -- so its measured history is a backtest, not a track record.
    #
    # NOT gated on the opposite-side rule that the cross arm uses: tested, and
    # it goes the OTHER way here (-0.1153/$1 gated against -0.0479 ungated;
    # opposite-side is this arm's best cell, not its worst). The cross-arm
    # finding does not generalise.
    impulse_enabled: bool = False
    impulse_max_open: int = 1
    impulse_maker_wait_s: float = 20.0
    max_tau_s: float = 210.0
    # RE-MEASURED 2026-09-12 on the live execution model (tools/arm_overlap,
    # 1,177 rounds, limit_take + 1s latency, band 0.45-0.80, min_tau 60).
    # A 300s round with max_tau=210 SKIPS ITS FIRST 90 SECONDS, and that skip
    # is load-bearing:
    #   max_tau 300 -> EV -0.0254/$1  WR 54.4%  t -0.87  second half -0.0619
    #   max_tau 270 ->     +0.0214     WR 59.5%
    #   max_tau 240 ->     +0.0115     WR 61.0%
    #   max_tau 210 ->     +0.0924     WR 66.5%  t +3.10  day-t +4.53
    #   max_tau 180 ->     +0.0585     WR 64.9%
    # 210 wins in BOTH halves (+0.086/+0.100) and beats both neighbours in
    # each, so this is not a fitted peak. Two things worth keeping:
    #   * widening does NOT add rounds -- it enters EARLIER in the same rounds
    #     (first half: n=483 at both 300 and 270). It buys worse information,
    #     not more trades, which is why the win rate collapses as it widens.
    #   * the previous note here justified 210 from a 366-round CANDLE-ERA
    #     re-tune ("the newest candle's first 30s is the only losing slice").
    #     That mechanism is off now (trade_on_candle_open=False) and this
    #     measurement supersedes it: the bound earns its place on its own.

    # --- cross arm: buy the side the spot JUST moved into the money ---------
    # The trigger IS the swap: spot crossed the strike
    # within `cross_look_s`, so the newly in-the-money side is the one that wins
    # if it holds, and the venue has not finished repricing it.
    #
    # MEASURED 2026-09-13, live sigma estimator, 1,384 rounds / 4.7 days, one
    # entry per round, priced at the ask, TODAY'S PARTIAL DAY EXCLUDED:
    #   any price   n=85  win 75.3%  edge +8.1 pts  EV/$1 +0.1210  day-t 1.96
    #   ask <=0.50  n=20  win 60.0%  edge +23.0pts  EV/$1 +0.6227  day-t 2.08
    #                     drop-best-day +0.4396, daily Sharpe 1.13
    # The venue UNDERSHOOTS a cross rather than ignoring it: median ask after a
    # cross is 0.72 on a side that wins 74.7%. The edge is the undershoot.
    #
    # Win rate and profit move in OPPOSITE directions here. The 0.85-0.99 bucket
    # wins 86.8% and is the only NEGATIVE one (-0.0364/$1): chasing win rate
    # walks into the overpriced favourites. Hence the 0.50 ceiling.
    #
    # HONEST WEAKNESS, stated because the numbers above look better than the
    # evidence is: n=20. The 0.50 ceiling was chosen AFTER seeing the buckets,
    # out of ~40 configurations tried across bands, tau windows, look windows
    # and price ceilings -- at that many attempts one variant at t~2 is what
    # noise alone produces, and the unrestricted rule falls to 1.96 (under the
    # bar) once today is dropped. Shipped live because $1 x 10/day is a bounded
    # way to buy the sample, NOT because it is proven. Kill it if EV/$1 <= 0
    # after ~150 booked entries.
    cross_enabled: bool = False
    # How far back to look for the flip. 2s and 3s measure the same (+0.1421 /
    # +0.1441); 1s finds almost nothing on a 1 Hz tape.
    # RE-SWEPT 2026-09-13 with FILL-AWARE scoring (a trigger only counts if the
    # ceiling-priced limit would actually be hit). Shorter is monotonically
    # better -- a fresher cross gives the venue less time to reprice:
    #     look 1s +$9.11   2s +$8.17   3s +$7.11   5s +$6.23   8s +$0.03
    # 2s rather than 1s because the replay tape is 1 Hz, so look=1 compares
    # adjacent samples and cannot be validated below that. The live arm reads
    # the websocket at ~33/s, so 2s is measured honestly at both resolutions.
    # 2.0 -> 1.0 on 2026-09-14, from a single-methodology audit of EVERY cross
    # knob (real Chainlink strike, real venue ask, priced at what we FILL at
    # under both slippage models, scored on compounded growth, day-clustered).
    # A shorter look-back confirms the crossing against a fresher reference, so
    # the order goes out before the venue reprices -- the measured ask falls
    # 0.690 -> 0.644 as the look shortens, and that is the whole margin.
    cross_look_s: float = 1.0

    # THE AUDIT THESE TWO COME FROM (2026-09-14). Every cross knob, one method:
    #     config                    n  growth add  growth rev  day-t  drop-best
    #     shipped (look2, cap3)   136     0.167      0.513     0.38    -0.018
    #     look 1.0, cap 3         108     1.223      1.302     3.24    +0.196
    #     look 2.0, cap 8         257     1.407      2.015     2.08    +0.087
    #     look 1.0, cap 8         170     1.617      1.823     3.54    +0.162
    #     look 1.0, cap 5         144     1.550      1.731     5.52    +0.230
    #
    # Read the first row before trusting anything else in this file: the config
    # shipped before this audit had day-t 0.38 and a NEGATIVE drop-best-day --
    # one day carried it. Every other knob (depth, tau bounds, round-range,
    # price band) sat at that same 0.38/-0.018 point, and its two slippage
    # models disagreed about which value was best. Those knobs are NOT
    # established; they are simply not obviously wrong on 7 days of data.
    # look 1.0 / cap 5 is the only setting that clears day-t 2 with a positive
    # drop-best-day under BOTH slippage models.
    # The window. RE-MEASURED 2026-09-13 (today excluded), px<=0.50, look 3s:
    #     tau 3-30s   n=20  4.0/day  win 60.0%  edge +23.0 pts  EV/$1 +0.6227
    #     tau 3-45s   n=26  5.2/day  win 65.4%  edge +26.2 pts  EV/$1 +0.6667
    #     tau 3-60s   n=34  6.8/day  win 64.7%  edge +23.6 pts  EV/$1 +0.5755
    #     tau 3-90s   n=59 11.8/day  win 59.3%  edge +16.3 pts  EV/$1 +0.3786
    # 3-60 is chosen for SAMPLE RATE, not for its point estimate: it books 70%
    # more entries at the same edge, and the edge is flat across 30/45/60
    # rather than spiking at one setting, which is the part worth trusting.
    # It decays by 90s. At 3-8s alone n drops to 26 and the edge with it.
    #
    # Raising the PRICE ceiling is the wrong lever for volume -- it buys trades
    # by giving up the edge: <=0.70 is 9.2/day at +10.3 pts, <=0.85 is 15/day
    # at +8.6, against +23.0 at <=0.50.
    # LOWERED 10 -> 5 on 2026-09-14. The edge is strongest closest to the bell,
    # measured on the largest sample this repo has for the question: 1,718
    # recorded rounds carrying the venue's REAL Chainlink strike, with the
    # trigger priced at the REAL venue ask (4,592 triggers):
    #     tau       n     ask     win   margin     EV/$1
    #     0-3      73   0.807   0.890   +0.083   +0.502   <- unfillable
    #     5-10     36   0.701   0.833   +0.132   +0.330
    #     10-15    38   0.693   0.737   +0.044   +0.262
    #     45-60   183   0.668   0.760   +0.092   +0.138
    #     60-90   344   0.621   0.625   +0.004   +0.029   <- edge is gone
    #    150-300 2863   0.581   0.576   -0.005   -0.008
    #   grouped: tau 1-10 EV +0.444/$1 (+2.1 SE) | 10-60 +0.243 (+3.2 SE)
    #
    # 5, not 3 or 1, is an EXECUTION floor and nothing else. Live decision->fill
    # is a median 3.7s and a max 4.4s over 10 fills, so a 5s budget is tight but
    # real while 3s is not: the 0-3 bucket is 73 triggers that can be seen and
    # never touched.
    #
    # CONTRADICTS a recorded fill-aware result, deliberately and with the reason
    # stated: "tau 10-60 books +$10.63 against +$7.11 for the old 3-60 window on
    # the same days" (median fill was then 5.0s). That was measured at the 0.5bps
    # depth gate and BEFORE the arm was put back on the websocket clock, which
    # is what cut the latency. If the fill rate at tau 5-10 comes in materially
    # below the 69% seen elsewhere, this goes back to 10.
    cross_min_tau_s: float = 5.0
    cross_max_tau_s: float = 60.0
    # RAISED 0.50 -> 0.70 on 2026-09-13. Re-measured by price bucket on live
    # data, one entry per round, priced at the trigger ask:
    #     ask 0.02-0.30  n= 8  win 75.0%  edge +61.1  tot $+64.37
    #     ask 0.30-0.50  n=15  win 66.7%  edge +23.6  tot  $+9.24
    #     ask 0.50-0.70  n=97  win 68.0%  edge  +7.0  tot $+11.84  <- was excluded
    #     ask 0.70-0.95  n=81  win 84.0%  edge  +3.4  tot  $+3.47
    # The 0.50-0.70 band is thinner per trade (+7.0 pts against +23.6) but it is
    # 4x the volume and positive, and it is the single largest block of edge the
    # arm was refusing. 0.70-0.95 is left out: +3.4 points is inside the noise
    # of its own break-even, and at those prices one wrong call costs four wins.
    # RAISED 0.70 -> 0.90 on 2026-09-13. The ceiling, not depth or tau, was the
    # binding constraint: on one day's tape 347 crosses became 44 inside the tau
    # window and then only 5 inside a 0.10-0.70 price band -- 89% refused for
    # being EXPENSIVE, which is what a correct call looks like after a cross.
    # Re-measured with look=2, tau 10-60, depth<=0.5, up to 3 per round:
    #     <=0.70  113 fills  win 71.7%  +$30.50  day-t 1.96  drop1 +15.50  2nd half +6.45
    #     <=0.80  135 fills  win 73.3%  +$32.72  day-t 2.07  drop1 +16.93  2nd half +5.72
    #     <=0.90  162 fills  win 76.5%  +$39.10  day-t 2.94  drop1 +23.17  2nd half +12.63
    #     <=0.95  171 fills  win 77.8%  +$39.88  day-t 2.88  drop1 +23.95  2nd half +13.32
    # 0.90 is the first setting that clears the repo's day-clustered t=2 bar,
    # and the fill rate goes 85% -> 99% because almost nothing lands above the
    # limit. 0.95 is inside the noise of it on totals and buys a worse trade:
    # at 0.95 one wrong call costs nineteen wins.
    cross_max_price: float = 0.90
    # 0.10, not 0.02: the 0.02-0.10 slice adds fills worth almost nothing and
    # drags the average. Raising the floor was +$1.44 on its own with no cost
    # to the fill rate.
    # NOTE (2026-09-13): this band is checked against `self._asks`, the last
    # POLLED book price, which is ~1s stale -- and the venue completes 66% of
    # its post-cross repricing within that second (n=189: pre-cross poll 0.505,
    # cross tick 0.618, +4s 0.645). So the FLOOR is close to advisory: a cross
    # admitted here at 0.35 is routinely bought at 0.65.
    #
    # The CEILING still binds on what we actually pay, but through a different
    # mechanism -- _place_order's `limit_ceiling`, which caps the limit itself
    # at cross_max_price. That is the gate doing the work, not this check.
    #
    # Reading the book fresh at trigger time would make the band honest, but it
    # costs a round trip in the one path where latency is priced, and gating on
    # the fresh price measured NO better (EV/$1 +0.1863 against +0.1890).
    cross_min_price: float = 0.10
    # How far past the strike the cross may already be, in bps. MEASURED
    # 2026-09-13, fill-aware, by depth at the moment of the cross:
    #     0.0-0.5 bps  n=59  tot  +$9.70  $/fill +0.1644  drop1 +4.43
    #     0.5-1.0 bps  n=18  tot  +$1.88  $/fill +0.1044  drop1 -0.06
    #     1.0-2.0 bps  n=13  tot  -$2.44  $/fill -0.1873  drop1 -2.88
    # A cross already 1-2 bps through the strike has been SEEN -- the venue has
    # repriced it and there is nothing left to take. The edge is the nick that
    # has not registered yet. Confirms the same finding from the other side: a
    # cross still advancing on the next tick scores worse (+$1.95 on n=21) than
    # one that has stalled (+$8.67 on n=71), because advancing means going
    # deeper.
    # Refuse a cross that takes the side OPPOSITE a position the MAIN arm
    # already holds in this round. MEASURED 2026-09-13 on 163 fill-aware
    # triggers replayed against the main arm's real live fills, 6 days:
    #     SAME side         n=44  win 0.864  EV/$ +0.3558  day-t  3.93
    #     OPPOSITE side     n=38  win 0.605  EV/$ -0.1066  day-t -0.66
    #     no main position  n=81  win 0.802  EV/$ +0.2200  day-t  2.23
    # Break-even at the average fill (0.642) is 64.2%, so the opposite cell
    # sits BELOW its own break-even. On its own it is only day-t -0.66 -- not
    # a proven loser -- but the SAME-vs-OPPOSITE win-rate gap is z=2.74
    # (p~0.006), and that contrast is what makes this a rule rather than a
    # noisy bucket. Dropping it: 125 trades instead of 163, +$33.48 instead of
    # +$29.42, EV/$1 +0.2678 instead of +0.1805.
    #
    # This REVERSES the earlier "both sides" setting, whose supporting note
    # rested on 16 and 9 display-priced fills. Why opposite loses is not
    # mysterious: the main arm entered on a model view at tau 60-210 and is
    # right 66% of the time, so a late tick that contradicts it is usually the
    # noise that reverts, not new information.
    #
    # Cross-vs-cross opposites are NOT refused: consecutive crosses alternate
    # sides by construction, and taking every one measured +$31.37 against
    # +$14.16 for the first alone (see cross_max_per_round).
    # Take the order book from the venue's WEBSOCKET instead of the ~1s REST
    # poll. Measured live 2026-09-13 on a BTC 5m market: median 250ms between
    # updates (p10 111ms, min 37ms), ZERO gaps over 1s, venue->us latency
    # median 89ms -- against the 1000ms poll every slippage figure in this repo
    # was measured on. That gap matters because the venue completes 66% of its
    # post-cross repricing inside one second, so the poll was quoting the arm a
    # price that no longer existed.
    #
    # It does NOT make fills cheaper by itself: placement still costs two REST
    # round trips (get_quote -> quoteId -> POST). What it buys is an honest
    # gate, depth-aware sizing, and visibility inside the second that decides
    # this arm.
    ws_book_enabled: bool = True
    # Refuse a websocket book older than this and fall back to the REST poll.
    # A silently frozen feed would price every order off a dead book, which is
    # strictly worse than the poll it replaces. 2s is ~8x the median update
    # gap, so a healthy feed never trips it.
    ws_book_max_age_s: float = 2.0
    # How far ABOVE the observed ask the cross arm's limit may sit.
    #
    # This replaces pinning the limit at cross_max_price (0.90), which was
    # shipped on 2026-09-13 to fix a no-fill and turned out to cost far more
    # than it saved. MEASURED on the venue, read-only, at every limit from
    # 0.30 to 0.99 while the ask was 0.960:
    #     priceLimit 0.30 -> amountIn/amountOut = 0.3000
    #     priceLimit 0.70 -> 0.7000        priceLimit 0.99 -> 0.9900
    #     a MARKET quote  -> 0.9609, i.e. the actual ask
    # A LIMIT quote commits `amountIn / priceLimit` SHARES regardless of the
    # market. It is a worst-case quantity, not a ceiling you pay only if the
    # book demands it -- and the venue sometimes delivers exactly that
    # commitment. Pinning the limit at 0.90 therefore means paying 0.90.
    #
    # Confirmed on the live record, same venue, same day:
    #     live arm (limit = quote + 1 tick)  n=626  median slip +0.0086
    #     cross arm (limit pinned at 0.90)   n=3    median slip +0.1408
    # and the third cross received stake/0.90 = 1.2111 shares against 1.2100
    # delivered -- exactly its worst case.
    #
    # 0.10 leaves slack for the book moving between trigger and arrival (the
    # venue reprices ~66% of a cross within one second) while bounding the
    # worst case at ask+0.10 instead of 0.90. Tighten once there are enough
    # fills to measure the fill-rate/price trade-off properly; the earlier
    # replay that rejected tight limits assumed "fill at the book price if
    # <= limit", which is NOT how this venue prices a LIMIT order, so that
    # rejection is void.
    # Multiplier on the MAIN arm's sizing rule for this arm only.
    #
    # WHY IT IS NOT 1. The corrected limit policy cut this arm from ~25 to
    # ~9.8 trades/day, but each surviving trade is worth far more (+0.27 to
    # +0.61 per $1 against the main arm's +0.11). At 1x the arm was sized for
    # the old trade count and now under-contributes.
    #
    # SWEPT 2026-09-13, 600 bootstrap paths over 6 days, PESSIMISTIC bound
    # (pay the limit), median 6-day bank [p10] and P(end below the $50 start):
    #             win 0.828 (measured)   win 0.73 (CI low)   win 0.665 (b/e)
    #     1x      $192 [150]  0%         $156 [118]  0%      $128 [94]   1%
    #     2x      $265 [189]  1%         $184 [ 81]  4%      $128 [43]  13%
    #     3x      $408 [209]  1%         $207 [ 60]  7%      $109 [33]  20%
    #     5x      $657 [217]  1%         $185 [ 36] 16%      $ 75 [15]  37%
    #     10x     $754 [144]  3%         $175 [ 15] 26%      $ 47 [ 9]  54%
    #
    # The MEASURED column says "bigger is better" to 7x and is the wrong one
    # to read: the win rate is 0.828 on n=58 with a 5-point standard error.
    # At the CI low the median PEAKS at 3x and then falls (207 -> 185 -> 173)
    # while p10 collapses -- past ~3x the daily loss cap halts the day before
    # the edge plays out, so the extra size buys variance and no return.
    #
    # 3x is ~6.5% of bankroll, about half of quarter-Kelly (12.1% on the
    # pessimistic bound). Chosen deliberately over the safer 2x: the arm has
    # 3 live fills and NONE under the current logic, so this is sized on a
    # replay. Cut it to 1x if live fills come in below ~70% win.
    # RESIZED 2026-09-13 from 3.0 after the historical month replaced the
    # week-long sample it was chosen on. The 3x sweep used win 0.828 from 58
    # replay fills; the month (792 triggers, 31 days, same gates) says 0.6907
    # with a 95% CI of 0.6585-0.7228 -- 0.828 is far outside it, so 3x was
    # sized against noise.
    #
    # RE-SWEPT on the monthly rate, 30 days from $50, 500 paths, fill prices
    # from the live week and the venue pinning the limit on 1 of 3 real fills
    # (P(limit)=1/3). Median [p10], P(end below start):
    #             win 0.6907 (month)   win 0.6585 (CI low)
    #     1.0x    $  505 [246]  0%     $  370 [180]  0%
    #     1.5x    $ 1424 [486]  0%     $  831 [269]  0%
    #     3.0x    $19645[2619]  0%     $ 5818 [861]  0%
    #
    # Bigger looks strictly better there -- and that is exactly why it is not
    # the number to size on. The result is dominated by P(limit), not by this
    # multiplier: at 1x, P(limit) 0 -> 1 moves the median $1453 -> $74. That
    # input rests on THREE live fills, and in the corner where P(limit)=1 and
    # the win rate is at its CI low, the monthly rate (0.6585) sits BELOW the
    # limit-price break-even (0.665) and 3x takes p10 from $39 to $19.
    #
    # 1.5x is up from 1x because the signal is now established on a month and
    # holds on weekends, and short of 3x because the decisive input is about
    # to be measured for free: the cross-exec telemetry logs fill-% on every
    # order, and full-fill-vs-partial IS P(limit). Revisit at ~20 live fills;
    # below P(limit) ~0.3, 3x becomes justified on evidence.
    # RAISED 1.5 -> 2.25 on 2026-09-14, with the model arm switched off so this
    # is the account's only risk. 0.0224 x 2.25 = 5.04% of bankroll per trade.
    #
    # Swept on the EMPIRICAL per-trade returns (tools/cross_sizing_sweep.py),
    # resampled in whole-day blocks so the skew and the within-day correlation
    # survive -- a mean-EV sweep would be wrong here, because 17% of trades
    # (ask < 0.30) carry 74% of the profit and win only 28.6% of the time.
    # 30 days from $50, with the 20% daily stop and the $55 depth cap:
    #
    #   frac    all asks (EV +.314)        fillable only (EV +.098)
    #           median    p10   ruin       median    p10   P(<start)
    #   2.24%  $ 9,232  $4,474   0%       $   293  $ 149     0.0%
    #   3.36%  $12,426  $7,618   0%       $   655  $ 236     0.0%
    #   5.00%  $14,353  $9,506   0%       $ 1,806  $ 423     0.1%   <- ships
    #  10.00%  $15,299  $9,949   0%       $   329  $  65     6.8%
    #  15.00%  $13,906  $5,469 1.5%       $    78  $  10    40.9%
    #  20.00%  $ 8,417  $    0 18.9%
    #  30.00%  $     0  $    0  100%
    #
    # 5% is the only setting at or near the peak under BOTH regimes, and which
    # regime is true is exactly what is unknown: the headline +0.314 has day-t
    # 0.59, the fillable +0.098 has day-t 1.91. 10% is the all-asks peak but 5x
    # WORSE than 5% if the cheap tail is unfillable, which live suggests it may
    # be (4 of 4 unfilled attempts had decided asks 0.14-0.45).
    #
    # Full-Kelly on this distribution is 17-19%, so this is ~1.1x quarter-Kelly.
    # Past 15% is a cliff rather than a slope -- these are fat-tailed empirical
    # returns, and 30% wipes out every path. Do not size on the median alone.
    #
    # Only 7 priced corpus days exist, so the block bootstrap is lumpy; read the
    # shape of the table, not single cells.
    #
    # RAISED 2.25 -> 3.0 on 2026-09-14 (0.0224 x 3.0 = 6.72% of bankroll).
    # The table above warned that 10% is "5x WORSE than 5% if the cheap tail is
    # unfillable". That condition is now CONFIRMED -- 0 of 5 live fills below a
    # decided ask of 0.40, all 8 unfilled attempts cheap -- so the fillable
    # column is the live regime and the warning had to be re-checked before
    # touching this. Re-swept on the CURRENT config's fillable triggers
    # (tools/cross_buffer_sweep.simulate at the shipped buffer and the measured
    # 3s order latency: 132 fills / 30 misses), resampled in whole-day blocks,
    # 30 days from $50, under three edge estimates:
    #
    #   frac        EV +0.279 (rec book)   EV +0.098 (corpus)   EV +0.040 (thin)
    #               median     p10         median     p10       median   p10
    #   5.04%       $6,185   $4,601        $  482   $  136      $  92   $  27
    #   6.72%       $6,984   $5,373        $  922   $  164      $  92   $  19
    #   8.96%       $7,548   $5,915        $1,477   $  167      $  79   $   8
    #
    # 6.72% is better on BOTH median and p10 under the two central estimates and
    # costs p10 $27 -> $19 under the pessimistic one. That asymmetry is why this
    # is 3.0 and not 4.0: past ~9% the thin-edge column falls off a cliff
    # (p10 $8, P(end below start) 0.39), and which edge is true is still the
    # open question -- the recorded book says +0.279, the priced corpus +0.098,
    # and 16 live trades say +0.202 at a win rate (0.8125) that 16 trades cannot
    # distinguish from the corpus's 0.714.
    #
    # This supersedes the old table only for the 5-9% region. Its warning about
    # 10%+ stands, and is now reinforced by the thin-edge column.
    # REVERT to 2.25 if the live win rate settles below ~0.70, or if realised
    # EV/$1 over the next ~30 fills comes in under +0.10.
    cross_stake_mult: float = 3.0
    # The absolute CAP on the buffer. RAISED 0.10 -> 0.25 on 2026-09-14 so it
    # stops binding at the cheap end, where cross_limit_buffer_inv now wants
    # more room (at ask 0.14 the inverse rule asks for 0.215).
    #
    # The concern the old 0.10 encoded is still real and still governs the cap:
    # the limit IS the price whenever the venue fills the whole order (measured
    # 2026-09-13 -- the one cross that filled 100% of its intended stake paid
    # its limit to the tick, while the two that filled 72-76% beat it). That is
    # the P(pin) risk swept in cross_limit_buffer_inv below, and it is why the
    # inverse coefficient is 0.25 rather than 0.50.
    cross_limit_buffer: float = 0.25
    # SUPERSEDED 2026-09-14 by cross_limit_buffer_inv. Kept only so an
    # existing cross_overrides entry naming it does not KeyError; nothing
    # reads it.
    cross_limit_buffer_pct: float = 0.20
    # The limit is `ask + max(0.01, min(cross_limit_buffer, inv * (1 - ask)))`.
    # INVERSE in the ask: bid MOST where the book is about to run furthest.
    #
    # Nine live cancellations, every one cheap (decided 0.14-0.57, median 0.36);
    # no attempt at 0.60+ has ever missed. The old proportional rule gave the
    # SMALLEST buffer exactly there (0.064 at ask 0.32 -> limit 0.38, missed).
    #
    # Swept over limit RULES, not one buffer (tools/cross_limit_rules.py), on
    # the recorded book with the measured 3s order latency. Growth vs the old
    # rule, by P(the venue pins the limit rather than filling at the book) --
    # measured at ~1/3 over the live fills:
    #
    #   rule              P=0.0    P=0.33   P=0.66    P=1.0
    #   inverse .25(1-a)  +0.013   +0.070   +0.118   +0.171
    #   inverse .35(1-a)  +0.086   +0.067   +0.028   +0.014
    #   inverse .50(1-a)  +0.136   +0.033   -0.090   -0.227
    #
    # 0.25 is chosen because it is the only one positive across the WHOLE range
    # -- 0.50 looks best if the venue never pins and collapses if it always
    # does, and P is known from few fills. At the measured P~1/3 the three are
    # within 0.004 of each other, so robustness decides, not the point estimate.
    #
    # Counterfactual on the 5 real misses with a known forward ask path: all 5
    # fill, net +1.481/$1 (mean +0.296) against 0.000 today. 0.35 also fills
    # all 5 but at worse prices (+0.114).
    #
    # CAVEAT on the substrate: the 1s replay is 6.8% cheap triggers while live
    # is 28.6% (live runs the websocket at ~114/s and catches crossings before
    # the venue reprices; the replay catches them after). So the sweep is
    # weighted toward triggers that never miss anyway, and it UNDERSTATES this
    # rule -- the live population is 4.2x richer in the case it targets.
    #
    # REVERT to the proportional rule if realised fill prices start landing AT
    # the limit more than ~half the time (that is P -> 1, where the advantage
    # shrinks to +0.171 -> and any larger inv goes negative).
    #
    # HALVED 0.25 -> 0.125 on 2026-09-14, and the revert condition above is the
    # reason. Realised cross fills land AT the limit 33.3% of the time and
    # within two ticks 55.6% (27 live fills), i.e. roughly the "more than half"
    # the note set as the trigger. The arm is not being slipped -- it is paying
    # the buffer it asked for:
    #
    #     decision.quote IS the book ask   (identical in 76% of 38 orders)
    #     requested_px - ask   +0.1000     the buffer this knob chose
    #     filled_px    - ask   +0.0943     what was actually paid
    #
    # Swept on 119 btc-5m triggers at the shipped gates, priced at the limit and
    # at the MEASURED 169bp fee, with the fill modelled as "the ask did not run
    # past the limit within `cross_limit_wait_s`":
    #
    #     inv   hold    n   growth  day-t  drop-best         95% CI growth
    #    0.250    20   91   +0.445  +1.31     +0.201   [-0.229, +1.057]  was shipped
    #    0.125    20   83   +0.628  +1.84     +0.363   [-0.065, +1.283]
    #    0.250     2   78   +0.492  +1.83     +0.282   [-0.093, +1.013]
    #    0.125     2   67   +0.835  +3.62     +0.607   [+0.280, +1.325]
    #
    # Both changes are needed. Either one alone leaves a CI that spans zero;
    # together they clear day-t > 2, drop-best > 0 and a CI excluding zero --
    # the three tests the old configuration failed on all counts.
    #
    # It costs frequency: 91 modelled fills become 67. That is the trade, and it
    # is the right way round -- the old cell was not distinguishable from no edge.
    cross_limit_buffer_inv: float = 0.125
    # How long a cross limit rests before it is cancelled. 0 keeps the shared
    # `limit_wait_s` (20s).
    #
    # HISTORY, because this knob was wrong in BOTH directions in one day.
    # 2.0 shipped 2026-09-14 on "a late fill is adverse selection" -- decided
    # in tools/cross_buffer_vs_fill.py, whose fill window starts AT THE
    # TRIGGER (no placement latency) and whose hold-2 row rode the
    # CI-clearing combo with cross_limit_buffer_inv = 0.125. Re-run on current
    # data, that tool still prefers short holds (hold2 +0.84 vs hold20 +0.63),
    # BUT the live record refutes its thesis directly -- every settled fill
    # bucketed by completion latency (n=27):
    #     <=2.5s    5 fills   2W  (0.40)
    #     2.5-3.4s 10 fills   7W  (0.70)
    #     >3.4s    12 fills   9W  (0.75)   <- the class a 2s cancel kills
    # Slow fills are the BEST class measured live, not the worst. (The 5.5-20s
    # band has also produced zero fills in ~39 orders -- unobserved either way.)
    #
    # But 2.0 is provably inside the venue's own fill process: fills complete
    # up to ~5.2s after the decision (four observed at 5.12-5.21s reported,
    # incl. 00:24 UTC 09-15: 92% at 0.5976, +0.67/$1), and a 2s cancel kills
    # them by construction. The corrected sweep (tools/cross_wait_sweep.py --
    # re-entry gap and tick floor aligned to the calibrated tool, both
    # fill-price conventions, LAT 2/3/4) puts longer waits above 2s under
    # EVERY convention (+0.27 -> +0.39 at pay-the-limit; +0.83 -> +1.20 at
    # touch). Anything past the demonstrated ~5.2s is UNOBSERVED live (zero
    # fills there in 45 exec attempts to date) and the two models disagree on
    # the extension, so the rest stops just past what the venue has shown.
    #
    # 6.0 = every fill the venue has produced, with ~0.8s of margin. 5.0 was
    # shipped briefly, then moved +1 the same evening: the long fills report
    # at 5.12-5.21s (four, 2W/2L) and that report includes the fill-confirm
    # lag, so a 5.0 cancel raced exactly the tail the record says works
    # (>=3.4s: 9W/12). The 6.0 sweep row is IDENTICAL to 5.0's (68 fills,
    # +0.389/1.28/0.198 -- the extra second adds no modelled fill), so the
    # margin is free. CHECKPOINT: keep scoring live fills by latency -- if
    # the >3s class turns negative over ~20 more fills, tighten toward 2; if
    # fills start appearing in the 6-20s band and win, extend toward 10-20.
    cross_limit_wait_s: float = 6.0
    # Refuse a crossing whose side FLIPPED within this many seconds of the
    # previous crossing in the same round. 0 = OFF.
    #
    # `cross_allow_reversal` already refuses the opposite side of a round, but it
    # arms on the ENTRY. A crossing the arm DECLINES leaves it unarmed, so the
    # opposite leg of the same oscillation is free to walk in -- which is what
    # bnb-updown-15m did on 2026-09-15 00:59, declining the correct Up on the
    # depth floor (2 ticks) and taking the Down 3s later on the return leg.
    #
    # Measured over 211 crossings that pass every gate, edge = win - ask:
    #
    #     FLIPPED within 20s    n= 14  win 0.643  ask 0.650  edge -0.0071
    #     clean                 n=197  win 0.863  ask 0.686  edge +0.1771
    #
    # Consistent in sign on both families with enough data (btc -0.035 vs
    # +0.197, eth -0.047 vs +0.076); bnb has one usable trigger and is not
    # evidence either way. n=14 is thin, so the case rests on the sign agreeing
    # across independent families AND on closing a hole the neighbouring guard
    # was already written to close. It costs 6.6% of crossings.
    #
    # The window is flat from 2s to 60s in the fill-priced sweep -- the same 1-3
    # trades are removed throughout -- so 20.0 is chosen as the middle of a flat
    # region, not a peak.
    cross_flip_block_s: float = 20.0
    cross_skip_opposite_to_main: bool = True
    # RAISED 0.5 -> 2.0 on 2026-09-13. The old 0.5 was picked by a fill-aware
    # sweep that called it a "clear peak", but that sweep could not have seen
    # what was wrong with it: at 0.5bps the backtest measures the STRIKE
    # RECONSTRUCTION, not the strategy. The venue publishes no strike (it comes
    # from a Chainlink stream at the round open), and two independent
    # reconstructions of it differ by a median 0.17bps -- about a third of the
    # old gate. Measured on every round of 2026-07-17 (btc/eth/bnb, complete
    # venue L2, tools/cross_l2_backtest.py), EV/$1 by gate and by which strike
    # is used:
    #     gate   K from book   K from spot@open   win gap
    #     0.50     +0.231          +0.053          8.3 pp   <- strike decides
    #     1.00     +0.185          +0.127          0.7 pp
    #     2.00     +0.294          +0.229          1.3 pp
    #     5.00     +0.316          +0.223          1.4 pp
    # At >=1bps the two agree, so the number is the strategy's. Widening is also
    # free: 10x the gate moves the ask only 0.523 -> 0.537 while the win rate
    # goes 0.544 -> 0.631 and triggers nearly double (204 -> 370). The old
    # objection -- that a wider gate buys further-ITM entries at a worse price --
    # is not what the book does.
    # 5.0 rather than 2.0. Both sit inside the agreeing region (strike-mode gap
    # 1.4 vs 1.3 pts), so both are measurable -- but across all five real-book
    # days (2026-07-17 complete + 2026-08-21..24 sampled) 5.0 wins on every
    # statistic, and is the ONLY gate that survives drop-best-day:
    #     gate   pooled EV/$1   per-day mean   day-t   drop-best-day
    #      0.5      +0.0765        +0.2066      0.74      -0.0538
    #      2.0      +0.2102        +0.1433      0.68      -0.0527
    #      5.0      +0.2226        +0.2442      1.08      +0.0734
    # day-t 1.08 is still under this repo's t=2 bar, so this is the best of the
    # available settings rather than an established edge. Four of the five days
    # are the SAMPLED Aug set (n=7-19), so the per-day column is noisy; the
    # mechanical argument above -- that 0.5 measures the strike, not the
    # strategy -- is what rules 0.5 out regardless of which column you read.
    cross_max_depth_bps: float = 5.0
    # Skip a round that has ALREADY moved this far. 0 = OFF.
    #
    # The arm's edge is regime-dependent, and this is the causal way to say so:
    # a round that has been calm is one where a late crossing STICKS; a round
    # that has already swung wide is one where it reverses again. Measured on
    # the recorded history (real Chainlink strikes, real venue asks, tau 5-60),
    # with the range taken from the round open to the TRIGGER instant -- never
    # the full-round range, which is not known when the order is placed:
    #     range-so-far    n    ask     win    EV/$1
    #       <=5 bps      74   0.762   0.865  +0.661
    #       5-8          50   0.703   0.760  +0.086
    #       8-12         45   0.674   0.711  +0.052
    #       12-20        15   0.669   0.467  -0.324
    #
    # Chosen on COMPOUNDED growth, not on EV/$1 -- a tighter gate has a better
    # per-trade edge but too few trades. Daily log-growth at the shipped 5.04%
    # sizing, priced at what we actually fill at rather than the book ask:
    #     gate      n/day  add-slippage   mean-revert
    #     off        64.4     0.1535        0.2651
    #     <=8        37.3     0.2436        0.3090
    #     <=10       46.0     0.2473        0.3463   <- best under BOTH models
    #     <=12       51.9     0.2347        0.3376
    # 8/10/12 all beat OFF under both models, so this is not knife-edge.
    #
    # CAVEAT: day-t at <=10 is 1.72, under this repo's t=2 bar (<=8 is 2.07).
    # It ships on the growth result being consistent across two independent
    # slippage models, not on the day-t. Revisit at ~30 more fills.
    cross_max_round_range_bps: float = 10.0
    # PER-MARKET overrides, {slug prefix: {knob-without-the-cross_-prefix: value}}.
    # Read through cross_knob() so an unset market falls back to the global
    # above and the live BTC config cannot drift by accident. Set from .env as
    # JSON, e.g.
    #     CROSS_OVERRIDES={"eth-updown-15m":{"min_tau_s":30,"max_tau_s":180}}
    # MEASURED 2026-09-13 on the venue's own L2 (data/hf/updown_*.parquet,
    # Aug 21-24) -- ask paid for the ITM side at the arm's own gate, which is
    # the half the recorded-only markets were missing:
    #     5m  tau 10-60, |mny|<=0.5bps : btc ask 0.540 win 0.824 EV +0.283
    #     5m  tau 10-60, 0.5-2bps      : btc +0.215 | eth -0.104 | bnb +0.027
    #     15m tau 30-180, 0.5-2bps     : btc +0.102 | eth +0.199 | bnb +0.091
    # ATM spread is what separates them: btc 0.010, eth 0.025-0.050, bnb 0.250.
    # Nothing is overridden yet: snapshots inside a round are autocorrelated,
    # so those n are not independent rounds and none has cleared the day-t bar.
    # PER-MARKET knobs. Only `max_round_range_bps` needs one, and the reason is
    # arithmetic rather than taste: the gate is in BPS, and a 15m round has
    # three times as long to travel as a 5m one, so BTC's 10.0 is a completely
    # different filter on each family. Measured share of rounds a 10 bps gate
    # would BLOCK (2026-09-14):
    #     bnb-5m 25%   btc-5m 40%   eth-5m 57%   btc-15m 56%   eth-15m 77%
    #     bnb-15m 95%  <- BTC's number would switch that family off entirely
    #
    # So each family gets the gate that admits the SAME SHARE of its own rounds
    # that 10.0 admits of BTC-5m's (59.5%) -- matched selectivity, not a copied
    # constant. Medians for context: bnb-5m 6.7 bps, btc-5m 8.4, btc-15m 11.3,
    # eth-5m 11.3, eth-15m 17.0, bnb-15m 18.0.
    #
    # NOTHING ELSE is overridden, and that is a measurement too: |depth| at the
    # trigger is 0.48-0.93 bps median and 1.47-2.23 at p90 on EVERY family, so
    # the 5.0 depth gate binds on 0-3% of triggers everywhere and needs no
    # per-market value. tau and look are in seconds and the mechanism they
    # encode (the venue has not repriced yet) is time-based, not size-based.
    # The price band and stake are left at BTC's because no other family has a
    # recorded ask to set them from -- see MARKETS for that gap.
    #
    # bnb-15m's 21.1 rests on 62 rounds and produced ZERO triggers in the
    # recording window; it is the least supported number here.
    # Whether the websocket book follows every CROSS_TRADED family or only the
    # primary. ON is required for the widening to trade at all -- off-primary
    # there is no REST book and no displayed-price fallback. It exists as a
    # knob because following all six once took the poll from a 1.00s median to
    # 4.15s, and a slow poll is what makes short crossings invisible. Watch the
    # "poll cadence" line; if the median climbs past ~1.5s, set this False and
    # the arm falls back to BTC-5m-only without a deploy.
    # Ceiling on TOTAL unsettled cross stake, as a fraction of bankroll.
    # 0 = OFF, the repo's convention for every other limit.
    #
    # MEASURED, after an earlier draft of this knob was (rightly) rejected for
    # being reasoned rather than measured. tools/cross_multi_exposure.py replays
    # the shipped gate across all six families on the 7 recorded days and walks
    # a clock over the open positions:
    #
    #     concurrency   peak 9   median 1   p90 4   p99 7
    #     at 6.72%/trade -> p90 26.9%, p99 47.0%, peak 60.5% of bankroll at risk
    #     same-side share when several are open: 0.945
    #     worst single expiry: 7 simultaneous LOSSES = 47.0% of bankroll
    #
    # 0.945 is the number that matters. These are not six diversified markets;
    # BTC, ETH and BNB cross their strikes on the same move and
    # btc-updown-5m / btc-updown-15m are the SAME ASSET on two clocks, so this
    # is one directional bet held up to nine times.
    #
    # And the daily loss cap CANNOT cover it: those positions expire at the same
    # instant, so a 20% cap is evaluated after the 47% has already happened. It
    # stops tomorrow's trading, not this loss.
    #
    # 0.30 is chosen off that table: it never binds at p90 (26.9%) and cuts the
    # p99 case from 47% to ~27%, at the cost of the top ~10% of overlapping
    # moments. It also sits just above single-market full Kelly (22.9%), so it
    # cannot bind on BTC-5m alone and leaves the sizing decision untouched.
    # Account-wide RESERVED SLOTS: how many cross positions may be open at once
    # across every family. 0 = OFF.
    #
    # This exists because `check_reserved` was being passed `max_per_round`,
    # and `reserved_open` is an ACCOUNT-WIDE counter. With one family those two
    # meant the same thing (only one round was ever live). With six they do
    # not: `cross_max_per_round` would silently have become "5 open positions
    # in total", so raising the per-round knob to admit more entries in a choppy
    # round would also have raised account-wide concurrency. Two different
    # limits must not share one number.
    #
    # 10 sits just above the measured peak concurrency of 9
    # (tools/cross_multi_exposure.py, 7 recorded days) so it bounds the
    # pathological case without being the everyday limiter -- at 6.72% a trade
    # the 30% dollar cap binds first, at 4 positions. This is the backstop; the
    # dollar cap is the control.
    cross_max_open_positions: int = 10
    # Cap on the CUMULATIVE stake of one ROUND, in units of the per-entry
    # stake. 0 = OFF.
    #
    # `cross_max_open_stake_pct` treats the account as a portfolio of
    # 0.945-correlated positions. Entries in the SAME round are not that: with
    # cross_allow_reversal=False they are the same side of the same round, so
    # they settle on ONE oracle print. Correlation is 1.000 -- a second entry is
    # an add to an existing position, not a second trade.
    #
    # That was unguarded. Measured on the recorded week (199 entries / 150
    # rounds, reversal guard applied correctly):
    #     1 entry  105 rounds     3 entries   2 rounds
    #     2 entries 42 rounds     4 entries   1 round   <- 26.9% of bankroll
    # and 26.9% on a single print is 1.17x FULL Kelly (22.9%), while the
    # per-entry stake is deliberately 0.29x Kelly because 18 live fills cannot
    # tell a 0.714 win rate from 0.881. The 3 -> 5 raise of cross_max_per_round
    # was justified on per-entry EV, which is correct and unchanged; what it
    # silently also did was move worst-round exposure past full Kelly.
    #
    #     cap   binds   max/print   additive g/t      reverting g/t
    #     none      0       26.9%   +2.319/t+1.98     +2.567/t+2.99
    #     3.0x      1       20.2%   +2.301/t+1.99     +2.543/t+3.02
    #     2.0x      3       13.4%   +2.360/t+2.11     +2.582/t+3.26
    #     1.0x     45        6.7%   +2.309/t+2.25     +2.205/t+3.51
    #
    # 2.0 bounds one print at 13.4% = 0.59x Kelly, which is the same posture the
    # per-entry stake takes, and it binds on 3 of 150 rounds. Its apparent +2%
    # growth gain is NOT claimed -- that is 3 rounds, the same n it would take
    # to reverse it. What is real is the a priori bound.
    #
    # 3.0 was the alternative and is rejected for being invisible: it binds on
    # one round and blesses 0.88x Kelly on a single print as standing policy.
    # A cap that never binds makes the decision unrecorded rather than made.
    # Refuse an order that would hold BOTH sides of one round at a combined
    # price above this. 0 = OFF. Applies to EVERY arm, not just cross.
    #
    # Holding matched shares of both sides pays exactly 1.000 whatever the
    # oracle prints, so the pair's cost decides the outcome outright:
    #     entry_px + other_px < 1.0  -> locked profit
    #     entry_px + other_px > 1.0  -> locked loss, and it cannot be undone
    #                                   (this venue has no sell, only buy)
    #
    # The cross arm got `cross_allow_reversal` after btc-updown-5m-1789387500
    # on 2026-09-14 (Up 0.611 + Down 0.589 = 1.199, a locked -$0.87 that could
    # not win). The MODEL arm never had one, and "arms are independent" means
    # nothing was inspecting the combined book. Measured over 720 settled
    # rounds: 25 hold both sides, 21 of them the model arm colliding with
    # ITSELF, seconds apart --
    #     09-12 04:40  pair 1.135 -> -0.49     09-12 06:11  pair 1.212 -> -0.43
    #     09-12 06:15  pair 1.192 -> -0.43
    # Mixed-side rounds returned -4.0% on $61.12 against +5.6% on $1,705 for
    # every other round.
    #
    # 1.0 is the break-even, not a tuned number: above it the pair is a loss by
    # arithmetic. It does NOT block a hedge that locks a profit (pair < 1.0),
    # which is measured separately and also rejected -- for a different reason,
    # that it forfeits an 88% win rate for certainty.
    #
    # This does not rescue the model arm and is not why it is off. Removing
    # every collision moves its day-t from 0.68 to 0.71 and leaves 98.4% of its
    # profit in a single day. It is a landmine, removed before that arm is ever
    # switched back on.
    max_paired_price: float = 1.0
    # Minimum gap between two entries in the SAME round. 0 = OFF.
    #
    # `cross_max_per_round` (5) and the "entries 0-4 all carry positive EV"
    # measurement behind it were computed on a substrate sampled once per
    # SECOND. Measured on that substrate, the gap between consecutive entries in
    # a round is: min 0.9s, p10 1.0s, MEDIAN 1.0s, p90 1.0s. That is the
    # sampling interval, not a property of the market -- 92% of its "second
    # entries" are the SAME crossing re-detected on the next sample.
    #
    # Live, the arm runs on the websocket clock at ~114 ticks/s, so it detects
    # that one crossing ~114 times. That was masked for as long as placement
    # AWAITED the fill: the first entry blocked for 2-3s and the crossing had
    # resolved by the time the arm looked again. Moving placement off the poll
    # loop on 2026-09-14 removed that accidental rate limit, and the arm
    # immediately took paired entries 0.06s apart at the same price --
    #     19:34:00,179  CROSS bnb Up $3.54 -> 23 today
    #     19:34:00,240  CROSS bnb Up $3.54 -> 24 today
    # which is not five looks at a changing book, it is one bet at double size,
    # and it spends the round's budget before any later crossing can be taken.
    #
    # 1.0 reproduces the substrate the EV was measured on. It is also
    # self-consistent with cross_look_s: once look_s has passed, `crossed_side`
    # stops returning a side for the same crossing anyway, because its reference
    # tick has moved to the near side.
    # Minimum depth through the strike, in TICKS. 0 = OFF.
    #
    # `cross_max_depth_bps` is a ceiling with no floor, and bps is the wrong
    # unit for a floor because a tick means a different thing on each symbol.
    # All three quote in $0.01, but that cent is:
    #     BTC @79138  0.0013 bps      ETH @2541  0.0394 bps
    #     BNB @726    0.1377 bps
    # so the median trigger depth, in ticks, is:
    #     btc-5m 193    eth-5m 13    bnb-5m 1    eth-15m 4    bnb-15m 2
    #
    # BNB's median cross is ONE CENT -- the last decimal flipping. Spotted live
    # on 2026-09-14: eth at spot 2544.48 vs strike 2544.47 and bnb at 726.05 vs
    # 726.04, both one-tick "crossings" that the arm treated exactly like BTC's
    # 193-tick ones.
    #
    # Win rate by tick depth (7 recorded days, all families, re-entry gap
    # applied):
    #     ticks    n    win
    #     0-1     34   0.647 +-0.161
    #     2-4     36   0.611 +-0.159
    #     5-15    57   0.825 +-0.099
    #     16-50   54   0.778 +-0.111
    #     51+    145   0.876 +-0.054
    # and on BNB specifically: 0.57 at <=1 tick, 0.56 at 2-4, 0.94 at 5-15.
    #
    # 5 is where the break is. BTC is untouched at any sane value (median 193),
    # so this is a per-symbol correction that needs no per-market table -- the
    # unit does the scaling a bps floor could not.
    #
    # NOTE this is NOT the depth floor tested and rejected earlier the same day.
    # That test was in BPS and BTC-ONLY, where the median trigger is 193 ticks
    # and a floor only removes real moves. The question is different on a symbol
    # whose median trigger is one tick.
    cross_min_depth_ticks: float = 5.0
    # Venue price increment per symbol, used only by the floor above.
    cross_tick_size: float = 0.01
    cross_reentry_gap_s: float = 1.0
    cross_max_round_stake_mult: float = 2.0
    cross_max_open_stake_pct: float = 0.30
    cross_ws_follow_all: bool = True
    cross_overrides: dict[str, dict[str, float]] = {
        "eth-updown-5m": {"max_round_range_bps": 12.9},
        "bnb-updown-5m": {"max_round_range_bps": 7.8},
        "btc-updown-15m": {"max_round_range_bps": 13.6},
        "eth-updown-15m": {"max_round_range_bps": 21.0},
        "bnb-updown-15m": {"max_round_range_bps": 21.1},
    }
    # Entries per ROUND. Consecutive crosses alternate sides by construction, so
    # a choppy round is naturally hedged rather than doubled. MEASURED, taking
    # every qualifying cross instead of only the first:
    #     1st cross only, depth<=0.5  n= 66  tot +$14.16  drop1  +8.02
    #     EVERY cross,    depth<=0.5  n=123  tot +$31.37  drop1 +16.37
    # 3 bounds the per-round exposure at $3 against an ~$11 daily cap.
    # 3 -> 5. Entries 0-4 in a round all carry positive EV at real fill prices
    # (+0.263, +0.157, +0.292, +0.168, +0.207 per $1); it decays from 5 on
    # (-0.054, +0.336, -0.006 on 7-12 samples each).
    #
    # "Consecutive crossings alternate sides by construction, so extra entries
    # in a choppy round hedge rather than double" -- RETRACTED 2026-09-14, on
    # both halves. It is not true at live tick resolution (51 of 65 non-first
    # entries on the recorded book repeat the SAME side; alternation only looks
    # universal on the corpus's coarse 10s grid), and where it does happen it
    # is not a hedge. See cross_allow_reversal.
    cross_max_per_round: int = 5
    # May a cross entry take the side OPPOSITE a position already held in this
    # round? No. Buying both sides is not a hedge: with the first position sunk,
    #     EV(take) - EV(decline) = stake_B * [ (1 - p)/b - 1 ]
    # so the second leg is worth taking iff `1 - p > b`, i.e. iff it is +EV on
    # its own. The first leg's price cancels; "locked overlap loss" is a red
    # herring. The only question is whether a reversal beats its own ask, and it
    # does not:
    #     substrate                  entry    n    win     ask    edge
    #     31d Chainlink corpus       first  839  0.714      --      --
    #                             REVERSAL  249  0.574      --      --   day-t -3.05 / 27d
    #     5 priced corpus days       first  167  0.749   0.664  +0.084
    #                             REVERSAL   34  0.559   0.581  -0.022
    #     7d recorded venue book     first  150  0.880   0.671  +0.209
    #                             REVERSAL   14  0.643   0.691  -0.048
    # Compounded growth of the reversals ALONE is negative under every pricing
    # tried -- book -0.043, additive -0.117, mean-reverting -0.091 (recorded),
    # and -0.120 / -0.353 (corpus) -- which is the standing two-model bar.
    # Removing them raises growth on the recorded book under all three pricings
    # (additive 1.800 -> 1.917, reverting 1.985 -> 2.076) with drop-best-day
    # still positive.
    #
    # This is NOT an argument for a smaller cap. Same-side repeats are the bulk
    # of what the cap buys and they are clearly good on their own (n=51, growth
    # +0.451 / +0.143 / +0.404, day-t +3.57 / +1.31 / +2.43). Keep cap 5, refuse
    # only the reversal. tools/cross_reversal_entries.py, cross_reversal_recorded.py
    cross_allow_reversal: bool = False
    # Flat dollars, or None to use the MAIN arm's sizing rule (sizing_mode +
    # stake_fraction + tilt + caps). None is the default: a flat $1 does not
    # compound, so the arm's contribution stays constant while the bankroll
    # grows, and it ignores the per-trade sizing logic the rest of the bot uses.
    cross_stake_usdt: float | None = None
    # Its own daily cap. **0 = OFF**, the same convention as daily_loss_cap,
    # max_consecutive_losses and stake_cap_usdt.
    #
    # The old note here -- "NOT the binding constraint and never has been:
    # at tau 3-30 the busiest observed day produced 9 entries against a cap of
    # 10, and at tau 3-60 it is 16 against 20" -- was measured at tau 3-30/3-60
    # with the 0.5bps depth gate. Widening that gate to 5.0 on 2026-09-13 made
    # the cap BIND: the corpus produces 45.6 qualifying crossings/day (126/day
    # on the complete-L2 day), so a cap of 20 was throwing away more than half
    # the arm's triggers. Turned off 2026-09-13.
    #
    # What still bounds a bad day, so this is not unbounded risk:
    #   daily_loss_cap_pct   20% of bankroll, account-wide, halts every arm
    #   cross_max_per_round  3
    #   the reserved slot    one open cross position at a time
    #   max_consecutive_losses
    cross_max_per_day: int = 0
    # PROBE (default OFF): quote RANGE-REFUSED crossings, sized by the arm's
    # NORMAL sizing rule -- deliberately no special stake, so what it measures
    # is what a gate raise would actually trade -- so the venue can answer the
    # one question the catch thesis (FINDINGS 2026-09-14 night) rests on: does
    # it honour an early quote while its book then runs? Nothing else can
    # answer it: natural fills cannot, because the range gate refuses exactly
    # the jump-sized crossings the question is about, so those never reach
    # the venue. Spends real money at the normal order size; the only
    # probe-specific number is the cap. Guard, caps, slot and exposure
    # accounting are unchanged. Rows are labelled "cross probe" in
    # decisions.reason; revert is one line (cross_probe_enabled False).
    cross_probe_enabled: bool = False
    cross_probe_max_per_day: int = 6
    # An edge must survive this many consecutive polls before we act: a
    # single-tick edge is usually our own stale data, not a real dislocation.
    # BTC-only re-tune (366 rounds, 2026-09-09): 2 beats 4 on clean data
    # (the old persistence-4 tune was run with ETH mixed into the replay,
    # inflating it). 0.09/2/210: +$25.31, mdd -$5.58, both halves positive
    # (+$17.32 / +$7.99); 0.09/3/210's newest half is already negative.
    signal_persistence: int = 2

    # --- execution timing ---------------------------------------------------
    # OFF: the candle-open gate lost to the price-catcher on every robustness
    # metric (see min_edge above). Instead of restricting WHEN to act, the bot
    # now restricts WHAT to act on: any second of the round, if the edge is at
    # least min_edge and holds for signal_persistence polls, buy immediately.
    # Set true to go back to open-only execution; window and candle selection
    # below still apply when it is on.
    trade_on_candle_open: bool = False
    candle_open_window_s: float = 10.0
    # Which candle opens are tradeable, indexed by minutes remaining at the
    # open: 1 = final minute (tau~60s), 2 = tau~120s, 3 = tau~180s, 4 = tau~240s.
    # The replay showed tau~180 losing on its own, but excluding it just reroutes
    # those trades to later candles and got worse overall (n=54 rounds), so the
    # default keeps all candles and the knob stays for a bigger recording.
    trade_candle_opens: list[int] = [1, 2, 3, 4]

    # --- freshness ---------------------------------------------------------
    max_spot_age_s: float = 2.0
    max_quote_age_s: float = 5.0
    max_basis_usd: float = 60.0

    # --- risk --------------------------------------------------------------
    # Daily stop as a FRACTION of the bankroll: the day halts once cumulative
    # PnL reaches -pct * bankroll, measured against the CURRENT bankroll, so it
    # tightens as the account shrinks and -- with the intraday equity refresh
    # in main.Bot._refresh_bankroll -- also grows with the account.
    #
    # The percentage is the primary rule because a fixed dollar stop does not
    # mean the same thing at two account sizes. The old -$35 was ~46% of a $76
    # account but only 3.5% of a $1,000 one, where it would trip on the second
    # loser and sit the day out.
    #
    # 0.20 -- five stakes at 4% -- is the knee of the swept curve
    # (tools/project_balance.py --sweep-cap; 30 days, 44 trades/day, at four
    # edge levels from +0.25/$1 down to zero):
    #   * a daily stop CANNOT shrink a MONTHLY drawdown, at any level. medDD and
    #     p95DD are flat across the whole 5%-to-disabled range at every edge
    #     (central: 18.5-18.7% and 30.6-31.5%), because the cap resets each UTC
    #     day while a monthly drawdown accumulates ACROSS days. The lever for
    #     that is stake_fraction, not this.
    #   * what it does buy is a bound on the worst DAY. p95 worst day at 45% was
    #     16.1% / 23.4% / 33.4% (at +0.25 / +0.17 / +0.06 per $1); at 20% it is
    #     19.6% / 20.7% / 22.6% -- bounded at ~20-23% whatever the edge turns
    #     out to be.
    #   * the premium rises as the edge falls: 45% -> 20% gives up 0.2% of profit
    #     at +0.25/$1 and 0.8% at +0.17/$1, but 12% at +0.06/$1. Below ~15% the
    #     cap fires 5-15 days a month and eats real EV for no monthly benefit;
    #     above ~30% it stops buying any tail protection at all.
    # The previous 0.45 was continuity with the old -$35 stop at $76.49, not a
    # tuned number -- it let a single day take 45% of the account.
    daily_loss_cap_pct: float = 0.20
    # Optional dollar CEILING on top of the percentage, for an account large
    # enough that 20% is an unacceptable absolute loss. It takes part in the
    # same min(), and 0.0 = off, so at $76 the percentage alone binds.
    daily_loss_cap_usdt: float = 0.0
    # Deliberately the same width as max_consecutive_losses: five losses at 4%
    # compounding is ~18.5% of the bankroll, so the two rules now agree on what
    # a bad day is. This cap catches the choppy days that never string five
    # losses together; the streak rule catches the rest.
    #
    # 0 = off, following the same convention as the caps above (and enforced in
    # RiskGuard.check, not here: the comparison there would otherwise halt on
    # the first call). At a flat $10 stake the streak rule is five losses, i.e.
    # half a $100 account -- it is the guard that actually binds, not the daily
    # stop, which now trips after two.
    max_consecutive_losses: int = 5
    max_open_rounds: int = 1

    # --- volatility --------------------------------------------------------
    vol_lookback_minutes: int = 240
    vol_ewma_lambda: float = 0.97
    vol_floor_1m: float = 0.00015

    # --- infrastructure ----------------------------------------------------
    poll_interval_s: float = 1.0
    db_path: Path = ROOT / "data" / "btcpredict.db"
    bsc_rpc_url: str = "https://bsc-dataseed.binance.org"
    # 1rpc.io key for BSC log access. Public BSC endpoints refuse eth_getLogs
    # ("limit exceeded"), which is what the on-chain trade reconstruction needs.
    rpcio_api_key: str = ""
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _reconcile_credentials(self) -> "Settings":
        # Fall back to the legacy `secret_key` name if the canonical one is unset.
        if not self.binance_api_secret and self.secret_key:
            self.binance_api_secret = self.secret_key
        return self

    @model_validator(mode="after")
    def _validate_pricing_model(self) -> "Settings":
        from btcpredict.models import MODEL_NAMES
        for name in (self.pricing_model, self.up_model):
            if name.strip().lower() not in MODEL_NAMES:
                raise ValueError(
                    f"unknown model '{name}'; choose from {MODEL_NAMES}")
        return self

    @property
    def has_credentials(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def fee_rate(self) -> float:
        return self.fallback_fee_bps / 10_000.0


settings = Settings()


def cross_knob(prefix: str | None, name: str):
    """A cross knob for one market, falling back to the global setting.

    `name` is the knob WITHOUT the `cross_` prefix ("max_depth_bps"). Unknown
    markets and unset knobs fall through to `settings.cross_<name>`, so adding
    a market to MARKETS never silently changes how an existing one trades.
    """
    if prefix:
        ov = settings.cross_overrides.get(prefix)
        if ov is not None and name in ov:
            return ov[name]
    return getattr(settings, f"cross_{name}")


def market_prefix(slug: str | None) -> str | None:
    """"btc-updown-5m-1788872400" -> "btc-updown-5m"."""
    if not slug:
        return None
    head, _, tail = slug.rpartition("-")
    return head if head and tail.isdigit() else slug
