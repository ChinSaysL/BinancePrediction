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
    # Added 2026-09-16 on the per-symbol train sweep -- one symbol per run,
    # because `--symbol A,B,C` POOLS them and a pooled run cannot rank. All
    # three clear the four-check battery on 2-3x BTC's sample:
    #   SOL  n=1366 win 89.3%  halves 89.1/89.6  drop-best 89.2  be-ask 0.876
    #   XRP  n=1504 win 87.8%  halves 87.7/88.0  drop-best 87.7  be-ask 0.862
    #   DOGE n=1635 win 86.9%  halves 88.4/85.3  drop-best 86.6  be-ask 0.852
    # The SIGNAL generalises. Their ASK is what was NOT measured -- the sweep
    # never sees the venue's price, and the arm earns (win - ask - fee). The EV
    # ceiling is what bounds that, and it now uses each family's OWN win rate
    # (see TRAIN_P_WIN) rather than BTC's, which would overpay DOGE by ~4.7c.
    ("sol-updown-5m", "SOLUSDT", 300),
    ("xrp-updown-5m", "XRPUSDT", 300),
    ("doge-updown-5m", "DOGEUSDT", 300),
]

#: Measured train win rate PER SYMBOL, from `tools/pm_train_sweep.py` run one
#: symbol at a time (PM tail-60 rule, thr 15bps, tau >= 60), 2026-08-31..09-14.
#: This is a MEASUREMENT, not a knob. A missing entry falls back to
#: `settings.train_p_win`, which is BTC's number -- correct for BTC and LOOSE
#: for anything with a lower win rate, so add an entry before activating a
#: family. Why it matters: the ceiling at margin 0.03 is 0.876 at p=0.914 but
#: only 0.829 at p=0.869, so one shared number would overpay DOGE by 4.7c.
#: HYPE is absent deliberately: HYPEUSDT 404s on Binance spot, so there is no
#: reference price for the strike or the model -- not tradeable here at all.
TRAIN_P_WIN = {"btc": 0.914, "sol": 0.893, "xrp": 0.878, "doge": 0.869}

# Families the CROSS arm may order in. Everything not listed here is recorded
# only. Kept separate from MARKETS so widening the tape and widening the risk
# stay two different edits -- removing that separation is how the daily loss cap
# was once bypassed.
CROSS_TRADED = frozenset(p for p, _, _ in MARKETS)

#: prefix -> Binance symbol, for keying any per-symbol buffer off a round.
#:
#: THE SLUG IS THE GROUND TRUTH, NOT `r.symbol`. The venue's round objects are
#: built by one `PmMarketFeed` whose `symbol` argument defaults to BTCUSDT, and
#: nothing constructs it per family -- so EVERY family's round comes back
#: stamped "BTCUSDT". Measured 2026-09-16: `to_round(pm, symbol=PmRoundSource().
#: symbol)` on the btc, eth AND bnb slugs all returned 'BTCUSDT'.
#:
#: Anyone who looked up a per-symbol buffer by `r.symbol` therefore read BTC's
#: ticks for every family. Live at 21:59:34 this printed
#:   "cross bnb-updown-5m-... Down not taken: round already moved 1109.7bps"
#: on a round that had moved 9.4bps: `_round_range_bps` had taken BTC's $80.00
#: range and divided it by BNB's 720.925 strike (1e4*80.00/720.925 = 1109.7,
#: and 1e4*80.00/2405.835 = 332.5 = the eth message in the same second). Both
#: arms refused, so the added families could not trade at all.
SYMBOL_BY_PREFIX = {p: s for p, s, _ in MARKETS}


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

    # --- venue -------------------------------------------------------------
    # Which venue the arms place orders on. BOTH list the SAME
    # `btc-updown-5m-<unix>` market -- same 5-minute boundary, same Up/Down
    # outcomes, same Chainlink BTC/USD family -- but they are NOT the same bet:
    #
    #   binance     resolves on a POINT snapshot (endPrice vs startPrice,
    #               measured 0.02 bps from the instantaneous close)
    #   polymarket  resolves on a 60 s TWAP vs the range's opening price
    #               (btc-5m-twap-60), variance sigma^2*T/3 -- 1.73x less
    #               dispersed than the point rule
    #
    # Measured disagreement between the two over 48 settled rounds: 10.4%.
    # Every calibration cell fitted on the point rule is INVALID under the
    # TWAP rule, so switching this without re-pricing the model is not a
    # venue change, it is a strategy change. Defaults to the incumbent.
    venue: Literal["binance", "polymarket"] = "binance"

    # Polymarket: the family we trade. The slug builder can produce nothing
    # else, which is what makes "BTC only" structural rather than a policy.
    pm_series: str = "btc-updown-5m"
    # Polymarket: refuse to price off a book older than this. Fails CLOSED --
    # a frozen feed that prices every order off a dead book is worse than no
    # price at all.
    pm_max_book_age_s: float = 2.0
    # Polymarket: how far past the ask we reach. The tick is 0.01, so this is
    # really "one or two ticks"; the fill ceiling is what bounds us.
    pm_slippage: float = 0.02
    # Polymarket: the lowest price an ENTRY may be quoted at, on the ask or on
    # the limit. 0.05 is not tuned, it is a REFUSAL bound: a contract under 5c
    # says the other side is worth 95c+, which none of the measured cells
    # covers -- and because the share count is `ceil(stake / limit)`, a price
    # at the venue's 0.01 floor multiplies the position 100x. Both happened on
    # 2026-09-16 (500 shares at 1c, $5.00 stake, lost); see
    # `broker.DEFAULT_MIN_ENTRY_PX`.
    pm_min_entry_px: float = 0.05
    # Polymarket: how long to wait for the venue to show the position before a
    # fill is marked UNVERIFIED. The POST returns 200 + an order id BEFORE the
    # outcome exists, so the ack alone is never proof of a fill.
    pm_confirm_budget_s: float = 2.5
    # How the Polymarket MARKET path takes liquidity.
    #   "limit" - sign locally, post, let any remainder REST (261 ms median)
    #   "fak"   - Fill-And-Kill == IOC: atomic take, remainder KILLED (414 ms)
    # A resting remainder is filled only when the market comes to us, which is
    # the adverse selection measured on this arm's predecessor (maker fills won
    # 41% at limits 0.6-0.7, EV -0.30/$1). FAK removes that, and costs +153 ms
    # against a horizon that is ALREADY the binding constraint (250 ms
    # profitable at every cap, 500 ms negative at every cap). Not a free
    # upgrade: it ships OFF and is A/B'd on its own fills.
    pm_order_type: Literal["limit", "fak"] = "limit"
    # Polymarket: collateral is pUSD (6 decimals), NOT USDC.e/USDC. A zero
    # USDC.e balance says nothing about a Polymarket balance.
    pm_collateral_decimals: int = 6

    # --- Polymarket credentials -------------------------------------------
    # THESE MUST BE SETTINGS, NOT `os.environ` READS.
    #
    # pydantic-settings loads `.env` into this object; it does NOT export those
    # names into the process environment. The service is started by systemd
    # with only `PYTHONUNBUFFERED=1`, so any code doing
    # `os.environ["POLYMARKET_PRIVATE_KEY"]` sees NOTHING while the value sits
    # in `.env` the whole time. That mistake cost a crash-restart loop: the
    # venue gate said "private key missing", live mode refused to start, and
    # systemd restarted it every 10 s.
    #
    # `POLYMARKET_API_KEY/_SECRET/_PASSPHRASE` alone are L2 credentials and
    # CANNOT sign an order -- see `registry.pm_credentials_present`.
    polymarket_private_key: str = ""
    polymarket_funder_address: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_passphrase: str = ""
    polymarket_relayer_api: str = ""
    polymarket_relayer_address: str = ""

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
    # The venue's floor, in DOLLARS. Binance's wrapper takes $1; Polymarket's
    # CLOB carries `minimum_order_size = 5` -- SHARES, confirmed on both gamma
    # (`orderMinSize`) and the CLOB market record (`minimum_order_size`), so the
    # dollar floor it implies is 5 x price: $1.00 at 0.20, $2.90 at 0.58,
    # $4.50 at 0.90. A $1 SIZING floor is still right -- the broker rounds the
    # share count UP to the venue minimum, so the order is always placeable --
    # but the REALISED stake is the venue's, not this number. Do not read this
    # as "the bot risks $1".
    min_stake_usdt: float = 1.0
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
    # How long buy() waits for order/history to return a TERMINAL status before
    # falling back to the position list. Seconds; 0 keeps the old 3.6s budget.
    #
    # A FOK that fails is retryable ("venue_failed"), but only if we hear the
    # venue SAY so. Measured 2026-09-15 05:09:13: the order was placed, the
    # status poll ran 6 x 0.6s = 3.6s and gave up, the position list was empty,
    # and buy() returned "unconfirmed" -- which is deliberately NOT retryable,
    # because an empty position list lags a fill and re-placing on it could
    # double exposure. The venue's own terminalTime for that order was ~8.2s
    # after createTime, and its verdict was FAILED.
    #
    # So the order died after ONE attempt for want of ~5 more seconds of
    # patience, while the bnb order at 05:34:29 -- whose status did arrive in
    # time -- was retried at 05:34:36 and FILLED. Five of six MARKET orders
    # since the switch failed; this is the difference between one shot and the
    # three `order_max_attempts` already allows.
    #
    # Waiting costs nothing when the order filled (the poll returns early on any
    # terminal status) and costs only time when it did not -- and a failed FOK
    # costs no money, just the trade.
    # Do not START another attempt with less than this much of the round left.
    # 0 = OFF.
    #
    # Retries were unbounded in tau. That was harmless while a failed attempt
    # was cheap and quick, but `order_status_wait_s` now spends up to 9s hearing
    # the venue's verdict, so three attempts can span ~30s -- and the cross arm
    # triggers with only 30-50s of round left. Without this, attempt 3 could be
    # placed into a round that has already settled.
    #
    # 5.0 matches cross_min_tau_s: the arm's own judgement of when a round is
    # too far gone to enter.
    #
    # MARKET carve-out (2026-09-15): for `order_type == "market"` the floor is
    # min(this, 3.0). An immediate FOK needs the round open only long enough to
    # place and execute (~1-2s) and its verdict may land after the bell, while
    # this value was calibrated for the 9s verdict budget; and the venue's book
    # demonstrably processes fills into the close (live fills booked with
    # 0.0-0.6s of round left). The 07:39:50 eth retry was refused at 3.3s left
    # by the old floor, and the venue never saw a second order.
    order_min_tau_s: float = 5.0
    order_status_wait_s: float = 9.0
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
    #
    # The CROSS arm is MARKET-ONLY (operator directive 2026-09-15). The
    # per-order fallback that briefly routed stale-low quotes to the LIMIT
    # path is removed; a FOK that cannot fill is re-quoted by the retry
    # instead.
    order_type: Literal["market", "limit", "limit_take"] = "market"
    # REMOVED 2026-09-15: `market_stale_quote_tol` (0.03) used to route a
    # stale-LOW quote to the LIMIT path. Operator directive the same day:
    # market orders only for the cross arm -- resting orders there are a dead
    # end. A quote the FOK cannot fill now costs one failed attempt and its
    # retry, not a resting order; see Bot._place_order.
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
    # ⛔ THE DEFAULT IS 1.0 -- WHICH DISABLES THE HAIRCUT (p = q + 1.0*(p-q)).
    # The paragraph below says "DEFAULT IS 0.5" and has been wrong since the
    # value was raised; it is kept because the 31 h measurement it records is
    # still the reason the knob exists. Re-measured 2026-09-19 on the 9-day
    # Binance tape with every other knob pinned to the live values
    # (min_quote 0.55, max_quote 0.80, min_edge 0.01, tau 60-210, per-symbol
    # sigma, measured taker fee), the 0.5 finding did NOT reproduce:
    #
    #     haircut  fills   EV/$     day-t   halves            neg
    #     1.00     2,170  +0.0202   +1.30   +0.0211/+0.0183   4/9   <- shipped
    #     0.95     2,158  +0.0238   +1.64   +0.0267/+0.0218   3/9
    #     0.90     2,133  +0.0240   +1.69   +0.0268/+0.0229   3/9
    #     0.85     2,117  +0.0266   +1.94   +0.0269/+0.0276   2/9
    #     0.80     2,088  +0.0186   +1.22   +0.0164/+0.0198   3/9
    #     0.50     1,823  +0.0155   +0.59   -0.0025/+0.0251   5/9
    #
    # 0.85 is an isolated peak with a cliff at 0.80, and the calibrated null
    # for this tape returns a best-cell day-t of +4.08 on average, so none of
    # these rows is distinguishable from the others. NOT changed on that
    # evidence. If it is ever changed, change `min_edge` with it -- min_edge is
    # compared against the HAIRCUT edge.
    #
    # (historical, and the reason the knob exists:)
    # THE OLD NOTE SAID: DEFAULT IS 0.5 (a deliberate safety haircut).
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
    # ⛔ 12.0 -> 16.0 on 2026-09-19. The 12 above was fitted with a replay that
    # was wrong five ways: it priced every Up fill at the BID, used BTCUSDT's
    # sigma for every family, modelled the taker fee at ~60% of the charge,
    # read the analysis machine's .env instead of the live host's, and did not
    # apply THIS gate at all. Re-swept with all of that corrected, on
    # btc-updown-5m over 2,267 rounds with every decision knob pinned to live,
    # the best cell moved to min_quote 0.55 / max_quote 0.85 / range [5,16] /
    # tau [60,180] at EV/$ +0.0531, day-t +5.54.
    #
    # It is a PLATEAU, not a spike -- nine of its ten one-axis neighbours sit
    # at day-t +2.2 to +4.3, the exception being range_lo 7 (+1.23). Against a
    # calibrated null (600 draws, outcome ~ Bernoulli(entry-implied p)) the
    # shipped cell tests at p = 0.0017 PRE-SPECIFIED, and best-of-grid at
    # p = 0.0017 multiplicity-honest.
    model_max_round_range_bps: float = 16.0
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
    # HOW LONG THE CROSSED SIDE MUST HOLD before it counts as a crossing.
    #
    # ⛔ WHAT THIS STOPS. `look_s = 1.0` requires only that the newest tick is on
    # the other side of the level from the tick 1 s earlier, and past tau=60 the
    # fc signal IS the spot, so ONE PRINT is the whole trigger. MEASURED
    # 2026-09-18 on `btc-updown-5m-1789711500`: the spot sat at 77552.85 for
    # 25 s, printed 77560.04 for a single second (+0.33 bps over the level), and
    # was 77554.01 five seconds later. The venue repriced TO the print
    # (0.51/0.50 -> 0.60/0.41), so the FAK filled at 0.600 for a book that was
    # 0.41 by the time the tape reverted; the round finished -4.60 bps against
    # the level and cost -$1.05.
    #
    # The knob is the trailing run of the newest tick's side, measured back to
    # the last tick on the other side: the crossing must be at least `hold_s`
    # old AND still standing, and at most `hold_s + look_s` old so a sustained
    # cross is not refused for being stale by the time it is proven.
    # 0 restores the old rule exactly (its own code path -- a dense oscillating
    # tape must not change meaning under a zero knob).
    #
    # ⛔ MEASURED, AND REFUTED: THE HOLD IS NOT SHIPPED. `tools/pm_hold_sweep.py`
    # over the whole Polymarket record (2,441 rounds, the shipped rule chain,
    # one entry/round, floor 0.25, band 0.10-0.65, tau 5-180):
    #
    #   hold   n    win     ask    per-share   total    t     day-t   halves
    #    0s   594  62.6%  0.523   +0.0870    +51.67  +4.31  +3.09  +0.069/+0.105
    #    1s   443  59.1%  0.560   +0.0150     +6.64  +0.64  +1.38  +0.005/+0.025
    #    2s   429  55.2%  0.570   -0.0342    -14.65  -1.42  +0.21  -0.019/-0.049
    #    3s   433  57.3%  0.570   -0.0138     -5.96  -0.58  +0.94  -0.023/-0.005
    #    5s   409  54.5%  0.571   -0.0426    -17.40  -1.72  +0.48  -0.030/-0.055
    #    8s   371  53.6%  0.567   -0.0476    -17.64  -1.85  +0.09  -0.026/-0.069
    #
    # Monotone, and the entries a hold REMOVES score about **+0.40/share**: the
    # subset is (594*0.0870 - 429*(-0.0342))/165. Waiting does not filter noise,
    # it buys the venue's repricing -- the ask rises 0.523 -> 0.570 while the hit
    # rate FALLS 62.6% -> 55.2%. The undershoot after the FIRST print is what
    # this arm is paid for (see the ceiling note above), so the 2026-09-18
    # `btc-updown-5m-1789711500` loss was the cost of the mechanism and not a
    # defect in it. The knob stays as the measured artifact, at 0.
    # Revert/keep: CROSS_HOLD_S=0 (the default).
    cross_hold_s: float = 0.0
    # MEASURE THE CROSSING AGAINST THE LEVEL THE VENUE PAYS ON. ON.
    #
    # The trigger above finds WHICH SIDE the spot crossed into; this decides
    # WHICH LEVEL it crossed. They are different objects and the arm had them
    # the same:
    #
    #   we compare      spot                     vs  `r.strike`  (a POINT)
    #   the venue pays  TWAP60(secs to the bell) vs  TWAP60(60s to the open)
    #
    # Polymarket's resolution source for these markets is
    # `data.chain.link/streams/<sym>-usd-twap-60s-streams` and its config is
    # `{id: btc-5m-twap-60, twapLookbackSeconds: 60}`, so BOTH ends of the
    # comparison are one 60 s average of the underlying -- the stream's value at
    # the bell against the same stream's value at the open. `r.strike` is the
    # SPOT AT THE OPEN INSTANT (correctly so; see `_pm_strike_backfill`), which
    # is a single print of the level the venue AVERAGES.
    #
    # MEASURED on the live DB, 1,509 settled Polymarket-era rounds with a
    # recorded strike, family keyed on the SLUG PREFIX (never `rounds.symbol`:
    # pre-fix rows stamp BTCUSDT on every family). Both candidate levels, same
    # rounds, same feed, one comparison each:
    #
    #     fin_TWAP >= TWAP60(open)   1420/1509 = 94.1%   <- the venue's own label
    #     fin_TWAP >= `r.strike`     1344/1509 = 89.1%   <- what the arm trades
    #     paired: 92 rounds where only the TWAP rule is right, 16 the other way
    #             McNemar z = +7.31 on 108 discordant pairs
    #
    # and the offset it corrects, `TWAP60(open) - r.strike`, is UNBIASED but
    # +-3-4 bps WIDE: median +0.00, mean -0.17, p10 -3.85, p90 +3.18, more than
    # 2 bps on 40% of rounds (doge 52%, xrp 52%, sol 47%, eth 44%, btc 32%,
    # bnb 30%). The arm's own floor is ONE TICK -- 0.0013 bps on BTC, 1.2 on
    # DOGE -- so on 4 rounds in 10 the level it measures against is wrong by
    # more than the whole depth band, and roughly 1 round in 10 is decided by a
    # level it never measured.
    #
    # THE THREE LIVE LOSSES OF 2026-09-17 ARE ALL IN THAT COHORT (PM-era fills
    # 873/874/875, btc + 2x xrp):
    #   874 btc Up 19:04  crossed `strike` by +0.53 bps while TWAP60(open) sat
    #       +1.55 bps ABOVE it, i.e. the entry was still 1.02 bps BELOW the real
    #       level; the round's own TWAP finished 1.2 bps under it and went Down.
    #       This fix does not take that trade: spot was under the reference
    #       before and after the move, so there is no crossing to take.
    #   875 xrp Up 19:14  did clear the reference (+0.62 bps) and lost by
    #       0.44 bps = 0.0057 cents. Nothing selects that one away.
    #   873 xrp Down 13:19  -1.61 bps under the reference; a coin flip.
    #
    # ⛔ THIS DOES NOT RE-OPEN "the strike should be a TWAP". `_pm_strike_backfill`
    # rejected a 60 s average AS THE STRIKE, correctly: measured against the
    # wrapper's own oracle `variantData.startPrice` it is off 1.42-2.13 bps mean
    # / 8-10 bps p99, against 0.042-0.114 bps for the spot at T-1. That question
    # is "what is the STRIKE"; this one is "what LEVEL does the payout compare
    # to". A wrong answer to the first corrupts the pricing of every round; this
    # changes only the level the crossing and its depth band are read against.
    # `r.strike` is left untouched everywhere it means the round's start price
    # (the stored row, the pricing model, the train arm's distance, the
    # round-range gate).
    #
    # WHEN THE TAPE CANNOT COVER THE WINDOW -- a process that booted after the
    # open, a tape that stalled, a round first seen before it opened -- the
    # level now comes from Binance's 1 s KLINES, which are retrospective, and
    # `r.strike` only when no kline answer can arrive at all.
    #
    # ⛔ THE OLD FALLBACK TO `r.strike` COST A REAL TRADE, MEASURED 2026-09-18.
    # A restart at 05:49:35 (a deploy) put the round opening at 05:50:00 on the
    # strike for its whole 5 minutes, because its reference minute
    # [05:49:00, 05:50:00) is partly pre-boot:
    #     strike (spot at the open instant)  752.095
    #     LEVEL  (60 s mean at the open)     752.5447   +5.98 bps away
    #     `bnb-updown-5m-1789710600` Up fired on `fc 752.13 vs strike 752.10`
    #         = +0.47 bps  -> clears the 0.25 bps floor
    #         against the LEVEL: -5.51 bps -> THE FLOOR REFUSES IT
    #     market: 0.26/0.76 at the open, 0.22/0.80 at the entry, 0.01/1.00 at
    #         tau 43. BNB had dropped ~6 bps INTO the open, so the point sat
    #         under the mean of the minute before it. Settled Down. -$1.05.
    #
    # The note that used to sit here claimed the fallback "is exactly the
    # behaviour that shipped before this existed, so it is never a new risk".
    # It IS a new risk: it swaps a 60 s MEAN for a POINT, and the two differ by
    # more than the 0.25 bps floor on ~40% of rounds. Blast radius of a restart
    # is one round per family (the rounds already open at boot) -- the only
    # window in which the tape cannot bracket an open.
    #
    # The source actually used rides in the trigger's `reason` and the fire line
    # (`ref=twap60` / `ref=kline60` / `ref=strike`), and the case where neither
    # source could answer is a WARNING naming the strike, so the split is
    # measurable from the DB rather than assumed.
    #
    # ⛔ AND THE KLINE READ IS THE SAME STATISTIC, NOT A SUBSTITUTE FOR IT.
    # Measured over 34 recent btc/eth/bnb 5m rounds where BOTH windows are
    # complete, the 1 s kline mean against the tape mean differs by median
    # +0.007 bps, range -0.21..+0.16, 34/34 inside 0.5 bps -- one sample per
    # second either way, on the same underlying. On `bnb-updown-5m-1789710600`
    # itself the kline window (60 of 60 bars) gives 752.4772 against a strike
    # of 752.095 ARRIVED AT +5.10 bps the other way, so the arm's 752.13
    # forecast is -4.61 bps against the level and the entry is refused.
    # Revert = CROSS_REF_TWAP=false + restart (back to `r.strike` everywhere).
    cross_ref_twap: bool = True

    # WHICH VARIABLE THE CROSSING IS READ ON: the SPOT (shipped) or the venue's
    # own statistic, the projected 60 s mean (`fc`).
    #
    # WHY. The payout is `TWAP60(bell) >= TWAP60(open)`, so money is decided by
    # a 60 s MEAN -- and `cross_ref_twap` already fixed WHICH LEVEL that mean is
    # compared against. The remaining hole is the SIGNAL: `crossed_side` fires
    # on the point spot, and inside this arm's own window (tau 5-60 s) that spot
    # is between 67% and 92% averaged away by the time the round ends. Measured
    # live: `bnb-updown-5m-1789680600`, spot crossed the reference by +1.63 bps
    # at tau 19 s, we paid 0.26, the MEAN never crossed, and the venue's book
    # had said 0.01-0.04 for the 44 s before we bought.
    #
    # MEASURED (tools/pm_twap_trigger_check.py, live DB, PM era only, first
    # crossing, one entry per round, tau 5-60):
    #     SPOT cross (what ships)  n=154  ask=0.468  win 51.3%  per-share +0.0382
    #                              t +1.37  day-t +12.06  halves +0.0587/+0.0178
    #     FC   cross (this knob)   n=105  ask=0.548  win 60.0%  per-share +0.0453
    #                              t +1.17  day-t  +5.08  halves +0.0839/+0.0074
    # Per-share BETTER on both halves, days 2/2 -- and 49 of 154 entries drop, so
    # at a fixed $1 stake the TOTAL is lower (5.89 -> 4.76 on that sample). Read
    # that as the price: this is a RISK change that removes a family by
    # construction, not an edge claim. Do NOT sell it as more money.
    #
    # ⛔ SPLIT BY FAMILY (`--by-family`, same run, same rounds) THE POOLED GAIN
    # DOES NOT REPEAT. Six families, n=13-31 each, and only BTC clears |t|=2:
    #     btc-5m   SPOT +0.1220 (t+2.06)   FC +0.2612 (t+2.99, halves +0.26/+0.26)
    #     bnb-5m   SPOT +0.0336 (t+0.43)   FC -0.0201 (t-0.29, days 0/2)  <-- the
    #     eth-5m   SPOT -0.0340 (t-0.58)   FC -0.0598 (t-0.70)            <-- two
    #     doge-5m  SPOT -0.0351            FC +0.0280                     <-- that
    #     sol-5m   SPOT +0.0206            FC +0.0317                     <-- lost
    #     xrp-5m   SPOT +0.1038            FC +0.0562                     <-- first
    # The pooled +0.0453 is ONE coin's, and on the two families that matter most
    # here the forecast cohort is NEGATIVE inside the noise. Carving those two
    # out on n=21/20 at t=-0.3 would be fitting the noise, not reading it -- the
    # repo's own 5th check. What the split settles: NOT a demonstrated general
    # improvement. What it does not touch: the MECHANISM (the venue pays on the
    # 60 s mean) and the spot-cross/mean-did-not family it stops by construction.
    # The per-coin question needs a longer sample than 2 days. Re-run the split
    # before quoting the pooled number.
    #
    # NOT the filter form. Keeping the spot trigger and dropping the ticks where
    # the forecast disagrees measured WORSE (5.89 -> 4.31), and both sides of the
    # disagreement cohort are negative when priced (tau 1: fcst -0.0032 t -3.16,
    # spot -0.0075 t -6.00) because the MARKET already prices the mean.
    #
    # THE SIGNAL ACTUALLY USED rides in the trigger's `reason` and the fire line
    # (`sig=fc` / `sig=spot`), so the two cohorts are separable in the DB rather
    # than assumed. Revert = CROSS_FC_TRIGGER=false + restart.
    #
    # ⭐ DEFAULT ON since 2026-09-17 22:02 UTC, and NOT only in the VPS `.env`.
    # The fix has to survive a `make config PROFILE=` switch and an `.env`
    # restore, because the profiles under configs/ do not all set it -- as an
    # env-only default this protection was ONE profile away from silently
    # disappearing, and the arm it protects loses real money when it does.
    # ⛔ STILL FALLS BACK TO THE SPOT on a partial poll tape (a boot inside the
    # last 60 s), logged once per round with `sig=spot` on the trigger -- the
    # same judgement `_cross_level` already ships for the level, so the arm
    # stays in ONE legacy mode rather than half of each. A build that failed
    # closed here was backed out: it is an unmeasured change in money terms and
    # its only live effect is the post-restart window.
    #
    # ⛔ AND THE PROJECTION ITSELF WAS AVERAGING THE WRONG WINDOW (fixed
    # 2026-09-18). `TWAP60(bell)` averages `[bell - 60 s, bell]`, so at `ts` the
    # PRINTED part of it is `[bell - 60 s, ts]` -- a FIXED left edge and a moving
    # right one. `twap_forecast` averaged `[ts - 60 s, ts]` instead: the whole
    # trailing minute at every tau, so at tau=30 it averaged 30 seconds the paid
    # window will never contain, and at tau=5 it averaged 55 of them. The two
    # windows are the same number only at tau=0 and at tau>=60 (where both are
    # the spot), and the arm spends its whole life in between.
    #
    # MEASURED over 400,079 (round, second) forecasts of the REALIZED
    # `TWAP60(bell)` off the same tape (`tools/pm_fc_formula_check.py`), MAE in
    # bps:
    #     tau          old     fixed        tau          old     fixed
    #     [ 0,10)     0.155    0.042        [30,40)     0.990    0.812
    #     [10,20)     0.431    0.182        [40,50)     1.307    1.235
    #     [20,30)     0.689    0.437        [50,60)     1.844    1.843
    # and IDENTICAL at tau >= 60 (max |old - fixed| = 0.0). Better at every tau
    # in BOTH halves, and the strategy agrees where it differs -- tau 5-60, same
    # rounds, `tools/pm_cross_replay.py --tau-lo 5 --tau-hi 60`:
    #     fc, no floor   n=122  win 54.1%  per-share +0.0517  day-t +1.77  halves +0.076/+0.028
    #     FIXED,         n=112  win 58.0%  per-share +0.0787  day-t +4.32  halves +0.076/+0.081
    #     fc + 0.5 bps   n= 48  win 58.3%  per-share +0.0788  day-t +1.89
    #     FIXED, + 0.5   n= 45  win 62.2%  per-share +0.0902  day-t +2.15  halves +0.074/+0.106
    # (the per-share t is 1.2-1.7 there, so the small-sample claim is "same
    # direction, no significance of its own"; the 400k-forecast MAE table is
    # what carries the change). Revert = restore the `ts - look_s` window in
    # `twap_forecast`; there is no env knob for it, deliberately.
    cross_fc_trigger: bool = True

    # ⭐ HOW FAR PAST THE LEVEL THE TRIGGER'S OWN VARIABLE MUST BE, IN BPS.
    # `crossed_side` fires on the FIRST tick past the level, so every entry is by
    # construction a ZERO-MARGIN entry -- the 0-1 bps bucket is not a tail, it is
    # 76% of all entries, and it is the weak one.
    # MEASURED on the live record (`tools/pm_twap_trigger_check.py`, fc entries,
    # tau 5-60, one entry per round, n=149 -- section "THE FC ENTRY'S OWN MARGIN"):
    #     |fc-level|  0-1 bps  n=113  ask=0.552  win 56.6%  +0.0054/sh
    #                              halves +0.0766/-0.0646   <- the weak bucket
    #                1-2 bps  n= 21  ask=0.535  win 71.4%  +0.1715/sh
    #                              halves +0.2247/+0.1231
    #                2-3 bps  n=  9  ask=0.584  win 77.8%  +0.1886/sh
    #                              halves +0.2259/+0.1588
    # That measurement shipped 1.0 as "the bucket boundary". A FULL SWEEP over
    # the whole PM-era record (`tools/pm_param_sweep.py`, 2,246 rounds, one
    # entry/round, taker at the recorded ask) then showed 1.0 was NOT robust:
    #     floor   n   ask   win    per-share  total   day-t  halves
    #     0.00  113  .472  47.8%   -0.0097   -1.10   -0.03  +0.030/-0.048
    #     0.25   68  .489  55.9%   +0.0538   +3.66   +1.43  +0.027/+0.068  <- ships
    #     0.50   43  .485  55.8%   +0.0574   +2.47   +1.42  +0.037/+0.077
    #     0.75   25  .484  52.0%   +0.0203   +0.51   +1.03  +0.003/+0.036
    #     1.00   19  .489  52.6%   +0.0206   +0.39   +0.27  -0.046/+0.081
    #     1.50    9  .464  66.7%   +0.1858   +1.67   +3.17  +0.064/+0.283
    #     2.00    5  .506  60.0%   +0.0767   +0.38   +0.90  -0.012/+0.136
    # THE WIN RATE IS NOT THE TARGET: it rises with the floor by construction
    # (1.5 bps wins 66.7% on n=9) because the venue's ask rises with it -- the
    # repo already measured "100.000% accuracy, ask 1.000". Re-running the SAME
    # sweep on each half separately is what decided it:
    #     half 1  0.00 +0.0363 | 0.25 +0.0270 | 0.50 -0.0245 | 1.00 -0.3451 | 1.50 -0.1673
    #     half 2  0.00 -0.0402 | 0.25 +0.0675 | 0.50 +0.0929 | 1.00 +0.1512 | 1.50 +0.3624
    # 0.25 IS THE ONLY FLOOR POSITIVE IN BOTH HALVES; the whole 0.5+ range is a
    # second-half effect, and 1.0 -- which shipped on n=36 from a different level
    # definition -- is the WORST row in the first half. 0.25 also beats 1.0 on
    # total money (+$3.66 vs +$0.39) and on entries (68 vs 19 over three days),
    # and it still refuses what the live losses had: the two losing fires were
    # priced at a 0.00 bps margin, the two winners at 2.0 and 3.1 bps.
    # THE LEVEL DEFINITION MATTERS, so re-measure rather than re-quoting: the
    # n=149 table above used K, the sweep uses the trailing-60 s mean.
    # ⛔ RE-RUN AT THE SHIPPED LOOK (1.0, not the tools' stale 2.0 default:
    # `pm_param_sweep.py` ranked on `--look-s 2.0` while the arm runs
    # `cross_look_s = 1.0`), AND THE "ONLY 0.25 SURVIVES THE HALVES" CLAIM DOES
    # NOT REPLICATE. The floor's ranking at the arm's own look (2,306 rounds):
    #     fc floor    n   per-share   total    t     day-t   halves
    #     0.00      111   +0.0504    +5.60  +1.10  +1.16   +0.088/+0.014
    #     0.25       69   +0.0525    +3.62  +0.85  +1.40   -0.001/+0.104  <- SHIPPED
    #     0.50       42   +0.0894    +3.75  +1.12  +1.69   +0.076/+0.103
    #     1.00       16   +0.0671    +1.07  +0.47  +3.06   +0.004/+0.130
    # and the per-half re-run puts 0.50 above 0.25 in BOTH halves (+0.1304 vs
    # +0.0550, then +0.0665 vs +0.0510). 0.25 STAYS ANYWAY, for the repo's own
    # reason: the ranking FLIPS between the two looks (at 2.0 the same 0.50 row is
    # -0.0245 in the first half and 0.25 is the only positive one), and a nuisance
    # parameter does not get to decide a knob -- see user memory
    # `backtest-artifacts.md` #7. n is 42-69 either way, so neither value is
    # quotable as fitted. What this DOES establish is the opposite of what the 2.0
    # table suggested: the FLOOR is not the lever, the WINDOW is (see
    # `cross_max_tau_s`), and at the shipped look even 0.00 is positive in both
    # halves.
    # ⭐ 0.25 -> 0.50 ON 2026-09-18, AND THIS IS THE RE-OPENING THE NOTE ABOVE
    # ASKED FOR. That note's verdict was right FOR ITS SAMPLE: every ranking
    # above was measured at tau **[5,60]**, where the whole cohort is 68-111
    # entries and a 1.0 floor leaves 16 -- "n is 42-69 either way, so neither
    # value is quotable as fitted... it needs a sample that can separate 42
    # entries from 69". `cross_max_tau_s` went 60 -> 180 the same day, so that
    # sample now exists: 3,176 settled rounds, 854 entries at the shipped floor.
    #
    # THE SWEEP ON THE SHIPPED WINDOW (`tools/pm_floor_sweep.py`, tau [5,180],
    # band [0.10,0.65), one entry per round, the repo's PASS bar -- pooled t>=2
    # AND day-t>=2 AND both halves positive AND drop-best-day positive):
    #     floor  n     per-share  total    t     day-t  halves          verdict
    #     0.00  1002   +0.0407   +40.79  +2.58  +1.68  +.0393/+.0422   fail
    #     0.25   854   +0.0594   +50.76  +3.48  +2.13  +.0620/+.0569   PASS <- was
    #     0.50   741   +0.0863   +63.95  +4.75  +2.08  +.1023/+.0703   PASS <- IS
    #     0.75   600   +0.0893   +53.56  +4.40  +2.28  +.0935/+.0850   PASS
    #     1.00   473   +0.1002   +47.38  +4.36  +2.21  +.1242/+.0762   PASS
    #     2.00   193   +0.1627   +31.41  +4.38  +4.67  +.2259/+.1003   PASS
    # 0.50 carries the MOST MONEY of any floor at either look, and it is the
    # smallest change from shipped.
    #
    # THE NUISANCE TEST IS WHAT ACTUALLY DECIDED IT, because it is what refused
    # 0.50 last time ("the ranking FLIPS between the two looks"). Re-run at
    # look_s 2.0 on this window, the flip is GONE -- 0.50's halves are
    # +0.1022/+0.0710 there, both positive, where at [5,60] they were -0.0245:
    #     floor 0.50 vs 0.25:  look1.0 +0.0269/sh   look2.0 +0.0217/sh   AGREE
    #     floor 0.75 vs 0.25:  look1.0 +0.0298      look2.0 +0.0100      AGREE
    #     floor 1.00 vs 0.25:  look1.0 +0.0407      look2.0 -0.0092      FLIPS
    # 1.00 is the unstable one now. 0.50 PASSES at both looks and AGREEs.
    #
    # INDEPENDENT CONFIRMATION, not an argmax over the grid (the trap this file
    # records: "the argmax over ~290 estimates manufactures a winner").
    # `tools/pm_margin_unit.py` buckets the SAME entries by tau x |margin|, and
    # the sign flips at 0.5 bps in EVERY tau band separately:
    #     tau [ 30, 60)  [0.25,0.5) -0.1327  ->  [0.5,1) +0.1546
    #     tau [ 60,120)  [0.25,0.5) -0.0435  ->  [0.5,1) +0.0763
    #     tau [120,180)  [0.25,0.5) -0.0368  ->  [0.5,1) +0.0475
    # That tool also settles the UNIT question before the value was swept: a
    # flat bps floor beats the scale-free `|m|/(sigma*sqrt(resid))` at every
    # matched entry count (keep-50% +0.0946 vs +0.0759, keep-10% +0.2341 vs
    # +0.1236), so the gate is the right object and only its value was wrong.
    #
    # ⭐ AND THE LIVE FILLS AGREE ON 0.50 WHILE REFUTING EVERYTHING ABOVE IT
    # -- this repo's rule is that live fills outrank the tape, and here they
    # disagree with it at the top end. 89 settled fills with a reconstructable
    # margin:
    #     |m| [0.00,0.25) n= 5  total  -3.502  per-share -0.1466
    #     |m| [0.25,0.50) n=20  total  -6.028  per-share -0.1346   <- REFUSED NOW
    #     |m| [0.50,1.00) n=20  total  +6.721  per-share +0.1761
    #     |m| [1.00,2.00) n=22  total  +2.160  per-share +0.0486
    #     |m| [2.00,99.0) n=22  total -10.367  per-share -0.1788   <- tape says +0.16
    #     floor 0.25 keeps n=84 total -7.514 | floor 0.50 keeps n=64 total -1.486
    #     floor 0.75 keeps n=57 total -5.639 | floor 1.00 keeps n=44 total -8.207
    # WHY THE TOP END DIVERGES, measured rather than guessed: entry slippage
    # against the ask the replay books RISES WITH THE MARGIN, because a big
    # margin IS a big fast move and the venue reprices before the FAK lands:
    #     |m| [0.50,1.00)  slip mean +0.0045  p90 +0.0440  (live .544 tape .540)
    #     |m| [1.00,2.00)  slip mean +0.0214  p90 +0.1090  (live .540 tape .518)
    #     |m| [2.00,99.0)  slip mean +0.0545  p90 +0.3010  (live .542 tape .487)
    # 5.5c/share at |m|>=2 is more than the edge the tape measures there. So the
    # sweep's monotonicity is an ARTIFACT OF UNMODELLED SLIPPAGE above ~1 bps,
    # and 0.50 -- the lowest-slippage bucket -- is where the two sources meet.
    # DO NOT raise this further on the tape alone; it needs live fills.
    #
    # HONEST BOUNDS: only 89 of 200 settled fills have a reconstructable margin
    # (the rest are rounds the quote tape does not cover), the live buckets are
    # n=20-22, and the live record is NEGATIVE at every floor -- 0.50 is the
    # least-bad (-1.486) and the only one near breakeven, not a proven winner.
    # KILL CRITERION: the [0.50,1.00) cohort at or below 0 per share over ~60
    # live fills means the venue repriced what the tape says it had not.
    #
    # ⛔ THE `.env` PIN IS LOAD-BEARING. `CROSS_MIN_MARGIN_BPS=0.25` sits in
    # the server `.env` and SHADOWS this default -- the same trap that kept the
    # refuted range gate live (see `cross_max_round_range_bps`). Changing this
    # line alone does NOTHING. Set `CROSS_MIN_MARGIN_BPS=0.50` there + restart.
    # Revert = 0.25 in both places.
    #
    # MEASURED ON THE FC COHORT. Applied to whatever signal is in use, so with
    # `CROSS_FC_TRIGGER=false` set this to 0 as well to get the spot arm's old
    # behaviour exactly.
    cross_min_margin_bps: float = 0.50

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
    # ⭐ 5.0 -> 30.0 ON 2026-09-18, AND IT WAS NEVER A STRATEGY NUMBER BEFORE.
    # 5.0 was an EXECUTION bound -- "the arm's own judgement of when a round is
    # too close to act on", sized off the median decision->fill of 3.7 s. It was
    # never measured as a GATE, and the sweeps COULD NOT measure it: the replay
    # takes one entry per round and picks the FIRST qualifying crossing, so only
    # 38 of 806 of its entries land at tau<60 and a tau floor never binds. The
    # LIVE arm is different -- 28 of 152 fills (18%) are at tau<60, because
    # restarts, the per-round cap and the gates delay the fire.
    #
    # THE LIVE RECORD (152 settled PM-era fills, `tools/pm_feasibility_gate.py`):
    #     tau [  0, 30)  n=  7  total -4.679  per-share -0.1130  win 14.3%  t -1.94
    #     tau [ 30, 60)  n= 21  total -1.591  per-share -0.0307  win 47.6%
    #     tau [ 60,120)  n= 12  total +1.182  per-share +0.0461  win 66.7%
    #     tau [120,181)  n= 84  total -7.707  per-share -0.0541
    # ONE WIN IN SEVEN below 30 s, at ~2x the average loss per share. The check
    # that stops this being an artifact: the whole live sample is negative, so
    # ANY gate improves the total by trading less -- but tau[30,60) BEATS the
    # ~-0.061 average, so the damage is specifically under 30.
    #
    # WHY, and it is arithmetic rather than a fitted edge. The payout is
    # `TWAP60(bell)`, so at `tau` seconds out `(60 - tau)` seconds of the
    # settling window are ALREADY PRINTED and unchangeable. Below tau=30 MORE
    # THAN HALF is locked, and if it printed against us the remaining seconds
    # must average enormously the other way to drag the mean over:
    #     required_remaining = (60*level - printed_s*printed_mean) / tau
    # Measured on the live fills at tau<60: median +0.40 bps, p90 +5.52, max
    # +14.99. The cohort needing >=2 bps is n=5, -0.1277/share, win 20%, t -2.26.
    # `sol-updown-5m-1789756200` is the worked example: entered at tau 31 with 28
    # of 59 samples locked at -5.35 bps, so the remaining 31 s had to average
    # +4.83 bps. It managed +4.86 and STILL lost, on feed noise.
    #
    # ⛔ THE MARGIN FLOOR CANNOT SEE THIS. `cross_min_margin_bps` asks how far
    # the signal is past the level NOW; this asks what the CLOCK still allows.
    # That sol entry read an ordinary +0.5 bps margin and sailed through.
    # WHY 30 AND NOT 60: 30 is where the locked fraction passes one half -- a
    # mechanical line, not a fitted one -- and [30,60) outperforms the average,
    # so a 60 floor would cut profitable entries too. A 60 floor "saves" more on
    # this sample (+6.27 vs +4.68) precisely by trading less, which is the
    # artifact above.
    # BOUNDS: n=7 refused. Thin. It is a TIGHTENING with a mechanical argument,
    # and it costs ~nothing on the tape (tau>=30 is +56.18 against +56.30 for
    # tau>=5 -- 2 entries and $0.12). KILL CRITERION: entry rate down more than
    # ~20%, or tau[30,60) turning negative over ~40 fills.
    # Revert: CROSS_MIN_TAU_S=5.0 + restart.
    cross_min_tau_s: float = 30.0
    # ⭐ANSWERED 2026-09-18: 60 IS NOT THE BEST WINDOW, AND THE SHIPPED ONE
    # FAILS THIS REPO'S OWN BATTERY. The question was opened by
    # `pm_param_sweep.py` measuring tau [60,120] at +$11.18 against +$0.39 for
    # the shipped [5,60] on the same 2,246 rounds -- a 28x difference on one
    # knob, which is not a number to leave lying around. It was NOT shipped
    # because (a) its day-clustered t was negative and (b) past tau=60 the
    # projection `((60-tau)*mean + tau*spot)/60` has a NEGATIVE weight -- at
    # tau=120 it is `2*spot - mean`, an extrapolation of a quantity the venue
    # computes as a plain average. So the +$11.18 looked like a momentum signal
    # wearing the venue's rule's clothes.
    # THAT SUSPICION IS NOW REFUTED BY DIRECT MEASUREMENT. `tools/pm_tau_window.py`
    # (2,282 rounds, one entry/round, floor 0.25 bps, taker at the recorded ask)
    # runs the SAME sweep with both signals -- `fc` (the shipped projection) and
    # `fc60` (`fc` while tau<=60, the SPOT past that, which is the unbiased
    # minimum-variance forecast of a martingale's future 60 s mean) -- so the two
    # differ ONLY in the tau>60 value:
    #     [60,120] extrapolating +0.0789/sh vs honest +0.0801/sh -> the
    #     extrapolation is worth -0.0012/sh, i.e. NOTHING (every window 0.0000
    #     to 0.0012). The wide window's edge is real, not an artifact of the
    #     formula. The formula should still be `fc60` (it is the correct object,
    #     and it costs nothing), but it is not what decides the window.
    # THE WINDOW SWEEP, honest signal, [PASS] = pooled t>=2 AND day-clustered
    # t>=2 AND both halves positive AND drop-best-day positive:
    #     window     n   win   ask   per-share  total   t    day-t  halves      drop  verdict
    #     [ 5, 60]  69 56.5% .488  +0.0612   +4.22 +1.01 +1.51  -0.017/+0.137 +0.056 FAIL <- SHIPPED
    #     [ 5, 90] 172 55.2% .505  +0.0309   +5.31 +0.80 -0.44  -0.006/+0.068 +0.028 FAIL
    #     [ 5,120] 330 62.1% .522  +0.0831  +27.42 +3.06 +0.61  +0.063/+0.103 +0.055 FAIL
    #     [ 5,180] 574 61.3% .526  +0.0710  +40.76 +3.42 +2.82  +0.047/+0.095 +0.065 PASS
    #     [ 5,300] 1194 57.6% .522 +0.0376  +44.92 +2.62 +3.10  +0.024/+0.051 +0.035 PASS
    #     [30,120] 325 61.8% .523  +0.0786  +25.56 +2.88 +0.59  +0.058/+0.099 +0.051 FAIL
    #     [60,120] 297 62.3% .526  +0.0801  +23.80 +2.81 +0.28  +0.060/+0.100 +0.054 FAIL
    #     [60,180] 551 61.5% .527  +0.0719  +39.63 +3.39 +2.99  +0.043/+0.101 +0.067 PASS
    #     [60,300] 1182 57.7% .521 +0.0387  +45.73 +2.67 +3.11  +0.026/+0.051 +0.036 PASS
    #     [120,300] 1094 57.8% .523 +0.0380  +41.59 +2.52 +2.60 +0.041/+0.035 +0.034 PASS
    # READ IT AS A DIRECTION, NOT AN OPTIMUM (the repo's own rule): the edge
    # plateaus over EVERY upper bound >= 180 and [5,60]'s first half is
    # NEGATIVE (-0.0173), so the last-minute window the arm ships is the worst
    # of the tested family, not a tuned one. [5,120] and [60,120] show the
    # highest per-share but die on the day-clustered check -- the same half-2
    # effect that made the first answer wrong. [5,180] is the smallest change
    # that passes (ONE knob) and [60,180] measures the same edge with 23 fewer
    # entries, so `cross_min_tau_s` does not need to move.
    # WHAT IT COSTS AND WHAT IS NOT MODELLED: entries go from 69 to 574 over the
    # same three days (~8x), each still $1 and still one per round, so the change
    # is one of TURNOVER, not of per-trade risk. The replay pays the recorded ask
    # with no sweep premium and no FAK miss rate, and the live `max_round_range_bps`
    # gate is NOT applied -- it refuses a round that has already moved far, which
    # binds on LATE entries, so its omission flatters the shipped window rather
    # than the wide ones. The 15m families would enter up to 3 minutes before the
    # bell instead of 1. Three days of data.
    # RE-RUN AT THE SHIPPED LOOK. That table was produced at `--look-s 2.0`, which
    # was the LITERAL default of all four PM tools while the arm runs
    # `cross_look_s = 1.0` -- i.e. it ranked a crossing rule the bot does not run
    # (`crossed_side` compares a different pair of samples at 1 s than at 2 s).
    # The default is now `pm_cross_replay.shipped_look_s()`, and the whole sweep
    # was re-run at 1.0 (2,288 rounds, honest signal):
    #     [ 5, 60]   68 57.4% .495 +0.0619   +4.21 +1.00 +1.51 -0.001/+0.124 FAIL
    #     [ 5,120]  316 61.4% .519 +0.0783  +24.75 +2.83 -0.01 +0.040/+0.117 FAIL
    #     [ 5,180]  556 61.3% .522 +0.0744  +41.38 +3.54 +3.00 +0.058/+0.091 PASS
    #     [ 5,300] 1164 58.1% .519 +0.0452  +52.56 +3.10 +3.75 +0.038/+0.052 PASS
    #     [60,180]  530 61.5% .524 +0.0749  +39.69 +3.48 +3.18 +0.060/+0.090 PASS
    # THE VERDICTS REPLICATE EXACTLY: every upper bound >= 180 passes, the shipped
    # 60 fails, [5,120] still dies on the day-clustered check, and the
    # extrapolation is still worth <= 0.0013/share. The window is therefore not an
    # artifact of the look either -- which is the one thing the 2.0 table could not
    # answer, because the arm does not run a 2 s look.
    # SHIPPED 2026-09-18 ON THE OPERATOR'S APPROVAL -- the risk decision the
    # paragraph above left open. 60 -> 180: the arm may enter up to three minutes
    # before the bell instead of one, on a population 8x larger PER THE REPLAY
    # (69 -> 574 entries over the same three days, each still $1 and still one
    # entry per round, so this is a change of TURNOVER and not of per-trade
    # risk). The signal does NOT change: `twap_forecast` already returns the SPOT
    # past tau=60 (see its docstring), which is `fc60` -- the honest object the
    # window was measured on, and the same one `honest_fc` substitutes in
    # `tools/pm_cross_replay.py`.
    # WHAT TO WATCH, stated before the fact: the extra entries are LATE ones, and
    # the live `cross_max_round_range_bps` gate -- the one gate the replay cannot
    # apply, because the recorded tape has no book -- is exactly the gate that
    # refuses rounds that have already moved far. So the realized entry count
    # should come in BELOW 574, and that shortfall measures the range gate, not
    # this change. Kill criteria: the tau 60-180 cohort at or below 0 per share
    # over ~150 entries puts this back to 60.
    # Revert = `CROSS_MAX_TAU_S=60.0` (the shipped value until today) + restart.
    #
    # RE-RUN 2026-09-18 ON THE CORRECTED FORECAST. `twap_forecast` had been
    # averaging the wrong window (see `cross_fc_trigger`), and this window was
    # ranked on that signal, so the sweep was repeated on the fixed one: same
    # verdicts, BETTER numbers -- [5,180] n=603 win 63.2% +0.0914/sh total
    # +55.13 t +4.59 day-t +3.27 halves +0.080/+0.103 drop-best +0.0872 PASS,
    # [60,180] n=574 +0.0890 t +4.35 day-t +3.24 PASS, while [5,60] still fails
    # on day-clustered t (+1.79) exactly as before. So the choice of 180 is not
    # an artifact of the formula bug -- it survives its correction, and
    # `pm_tau_window.py`'s "honest" and "shipped" sections are now identical
    # line for line.
    # RAISED 180 -> 240 on 2026-09-19, on the first sweep of this window that
    # was both config-independent and scored out of sample. Inside [0.45,0.65),
    # floor 0.50, ceiling off, charged the live-measured ladder:
    #                    FULL TAPE (4 days)          TEST DAYS (unseen)
    #     tau<=180  L1  n= 540 +19.00 t+1.68         n=330 +10.00 t+1.12
    #               L2  n= 917 +25.28 t+1.71         n=539  +7.93 t+0.69
    #     tau<=240  L1  n= 885 +24.41 t+1.68         n=519 +14.34 t+1.28
    #               L2  n=1395 +42.68 t+2.35         n=788 +19.16 t+1.39
    # Better at both looks, on the full tape and on days the choice never saw.
    # 300 is worse than 240 at both looks, so this is an interior optimum, not
    # the edge of the grid.
    # ⛔ BOUND: the tape is FOUR DAYS (09-16..09-19). The day-clustered t that
    # this repo's PASS bar asks for is computed on four points, so no cell here
    # PASSES and none is claimed to. Revert: CROSS_MAX_TAU_S=180 + restart.
    cross_max_tau_s: float = 240.0
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
    # 0.90 STANDS. Raised to 0.99 and reverted the same day (2026-09-17) after
    # reading back the recorded evidence, which is unanimous and points the
    # OTHER way -- cheaper caps are better:
    #   tools/pm_cross_sweep.py, 360 cells, btc/5m 274 rounds, lag 300ms:
    #     max_px  0.45 +0.3882 | 0.55 +0.1817 | 0.65 +0.1166 | 0.75 +0.1171
    #             0.90 +0.0944   <- MONOTONE, and 0 cells reach |t| >= 2
    #   tools/btc5m_cross.py, 11 days, 1s coherent window, n=292:
    #     0.65-0.80 win 77.4% be 73.8% EV +0.0478 day-t +0.48
    #     0.80-1.01 win 90.0% be 87.6% EV +0.0297 day-t +0.72   <- nothing
    #   the LIVE Binance ledger (110 decided fills):
    #     0.65-1.01 n=68 win 75.0% paid 0.8088 be 81.3%  <- where the money went
    # So above ~0.80 the market prices the crossing almost exactly right and the
    # arm pays the difference. The 2026-09-17 calibration agrees (0.98 -0.0290,
    # 0.99 -0.0523) and the pooled +0.0108 that looked favourable is z 1.35, i.e.
    # inside its own error bar. Do not raise this to "make it trade":
    # cross_max_fill_px (0.65) caps the payment anyway, so a looser band here
    # only burns venue calls and the daily attempt budget on orders that cannot
    # fill.
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
    # ⭐ 0.10 -> 0.45 ON 2026-09-18. The largest single loss cohort in the live
    # record, and the tape says the EXACT OPPOSITE for a reason now measured.
    #
    # LIVE (155 settled PM-era fills), by the price actually PAID:
    #     px [0.00,0.35)  n= 11  total -8.018  per-share -0.1191  win  9.1%  t -2.24
    #     px [0.35,0.45)  n=  7  total -2.364  per-share -0.1381  win 28.6%
    #     px [0.45,0.50)  n=  6  total +0.212  per-share +0.0164  win 50.0%
    #     px [0.50,0.55)  n= 25  total +0.904                     win 52.0%
    #     px [0.55,0.60)  n= 38  total -4.420                     win 50.0%
    #     px [0.60,0.65)  n= 31  total +1.703                     win 64.5%
    #     floor 0.45: kept n=137 total -6.063 | REFUSED n=18 total -10.382
    # EIGHTEEN fills below 0.45 cost -$10.38 of a -$16.44 total, and the sub-0.35
    # bucket wins NINE PERCENT. The band [0.45,0.65] is n=101, total -0.907.
    #
    # ⛔ THE TAPE SAYS THE OPPOSITE AND IS WRONG, MEASURABLY. On the replay
    # `ask [0.00,0.35)` is the BEST cell in the whole sweep -- n=91, win 65.9%,
    # +0.4693/share, t +9.11, PASS. That is an artifact of WHICH BOOK the replay
    # prices at. `quotes.up_ask` is the WEBSOCKET ladder (main.py ~1697: the ws
    # book overrides the REST poll), which is the same book the gate reads --
    # and it is accurate at mid prices and wildly optimistic at cheap ones:
    #     ours [0.00,0.35)  ours 0.200  venue 0.368 (+0.168)  filled 0.333
    #     ours [0.35,0.45)  ours 0.402  venue 0.605 (+0.203)  filled 0.595
    #     ours [0.45,0.55)  ours 0.508  venue 0.541 (+0.033)  filled 0.522
    #     ours [0.55,0.65)  ours 0.583  venue 0.581 (-0.002)  filled 0.579
    # So the replay "buys at 0.20" (a 5x payoff) where we actually pay 0.333
    # (3x). Its cheap-price edge is arithmetic on a price that does not exist.
    # THIS IS THE UNIFIED EXPLANATION for every tape-vs-live inversion recorded
    # today (the |m|>=2 cohort, the stacking, `confirm=0`, this): the replay
    # systematically overvalues whatever cohort our ladder misprices most.
    #
    # WHY 0.45, and it is two independent lines meeting: the live P&L sign flips
    # there, AND it is where our own book becomes trustworthy (+0.193 error
    # below it, +0.015 above). A gate that refuses the region where our price is
    # unreliable is principled, not fitted.
    # MECHANISM, stated plainly: a cheap fill means the market prices our side
    # far out of the money while our trigger says otherwise -- i.e. the paid
    # TWAP is nowhere near crossing -- and it is exactly there that our ladder
    # is least able to tell us what we are paying. Wrong twice over.
    # ⭐ RE-JUSTIFIED 2026-09-19. The note above shipped this on the live record
    # alone, and that record turned out to be unusable for the purpose: the 231
    # settled fills span seven configs and none of the 139 in-band ones ran
    # under these gates. The floor is still right, for a reason that needs no
    # live outcomes at all -- THE CHEAP BOOK IS NOT FILLABLE. Quote age at the
    # replay's own entry instant, and where that side's ask sits 2 s later:
    #     book [0.02,0.20)  n= 60  age 2.4s   ask +0.5407   win 65.0%
    #     book [0.20,0.30)  n= 48  age 2.3s   ask +0.3515   win 60.4%
    #     book [0.30,0.45)  n=123  age 1.0s   ask +0.2174   win 48.0%
    #     book [0.45,0.55)  n=513  age 0.8s   ask +0.0762   win 58.5%
    #     book [0.55,0.65)  n=679  age 0.1s   ask +0.0180   win 62.9%
    # A crossing is precisely the instant a stale cheap quote is about to
    # reprice, so the replay "buys" 0.06 and two seconds later that side is at
    # 0.60. Priced at the quoted ask the sub-0.45 cohort is +$58.42; priced at
    # the ask 2 s later it is -$17.13. Every joint-search cell that wanted the
    # floor opened to 0.10 was scoring fills that cannot happen.
    # AND IT IS NOT A CASE FOR GOING HIGHER: on unseen days, px_lo 0.50 makes
    # +2.29/+10.86 and px [0.55,0.75) makes -3.33/-3.06, against 0.45's
    # +14.34/+19.16. The band is fillable at 0.45 and the edge is inside it.
    # KILL CRITERION: the [0.45,0.55) cohort turning negative over ~40 fills.
    # Revert: CROSS_MIN_PRICE=0.10 + restart.
    # ⭐ RE-CONFIRMED 2026-09-19 against a SECOND tool that wanted it lowered.
    # `tools/pm_two_book.py` models both books and prefers a 0.35 floor
    # monotonically ($+272 against $+176). It is wrong for the reason above: it
    # draws one gap distribution at every price and cannot see that the cheap
    # book is stale. A 0.35 floor adds 171 entries of which 27% PAY under 0.45,
    # where the two-second reprice is +0.2174 to +0.5407. Two independent
    # sweeps have now asked for this floor to come down; both were artifacts of
    # pricing fills that cannot be had.
    # ⭐ HOW FAR THE DECISION-TIME BAND IS PADDED OUTWARDS, so the REAL band
    # can be applied to the venue's ask at send instead of our ladder's.
    # 0 = off, which is exactly the old behaviour.
    #
    # WHY. Our websocket book runs a median 593 ms behind the venue (p90
    # 1015 ms), which puts its ask a median 0.030 under the venue's and 0.130
    # under at the tail. Gating [0.45,0.65] on it therefore selects trades by a
    # price we do not pay. `tools/pm_two_book.py` models both books:
    #     band on the ladder  n=743 miss=129 win 57.1% $+160.81 | TEST $+85.61
    #     band on the venue   n=947 miss=  0 win 58.5% $+272.80 | TEST $+151.15
    # worth +$66 (look 1.0) and +$69 (look 2.0) on held-out days.
    #
    # THE COST IS PROBES, AND IT IS SMALL: candidates that clear the padded
    # ladder band and then fail the real one still cost a venue read. Measured
    # on the tape, 9.0/hr at pad 0 against 11.7/hr at pad 0.10, which also
    # captures essentially every available fill (839 of the 838-947 range).
    # A wider pad only buys wasted probes: [0.30,0.80] gives 832 fills for 12.1
    # probes/hr, [0.00,1.00] gives 838 for 13.6.
    #
    # ⛔ AND IT IS NOT A LICENCE TO WIDEN THE REAL BAND. cross_min_price stays
    # 0.45 because the cheap book is unfillable; this changes WHICH BOOK the
    # 0.45 is read on, not the number. A venue read that FAILS while the pad is
    # on means the padded band was the only one applied, so `_place_order`
    # refuses outright rather than buying on an unchecked price.
    # Revert: CROSS_LADDER_BAND_PAD=0 + restart.
    cross_ladder_band_pad: float = 0.0
    cross_min_price: float = 0.45
    # ⛔ RETIRED 2026-09-19, THE SAME DAY IT SHIPPED. It was chosen on live
    # fills bucketed by their realised margin. Those buckets are not a
    # measurement: the 231 settled PM-era fills span SEVEN distinct live
    # configs, and ZERO of the 139 in the [0.45,0.65] band were taken under the
    # gates they were used to justify. A fill is only IN a bucket because the
    # config at that instant admitted it, so bucketing them confounds the knob
    # under test with every other knob that moved. The note this replaces said
    # the live cohort "is essentially ONE DAY" and shipped anyway.
    #
    # ON THE TAPE, which is config-independent because `pm_cross_replay.pick`
    # re-derives entries from the raw quote history under the chain it is
    # handed, the ceiling is simply a cost. Charged the live-measured ladder,
    # inside [0.45,0.65), floor 0.50, everything else equal:
    #                        FULL TAPE (4 days)        TEST DAYS (unseen)
    #     ceiling 1.5   L1  n=407 +12.53 t+1.27        n=252  +5.92 t+0.76
    #                   L2  n=769 +18.28 t+1.35        n=449  +8.70 t+0.84
    #     ceiling off   L1  n=540 +19.00 t+1.68        n=330 +10.00 t+1.12
    #                   L2  n=917 +25.28 t+1.71        n=539  +7.93 t+0.69
    # Every finite ceiling in a 768-cell joint search was beaten by no ceiling,
    # at BOTH look_s 1.0 and the 2.0 the arm does not trade. It costs money and
    # buys nothing.
    # 0 = off, the repo's convention. Re-enable: CROSS_MAX_MARGIN_BPS=1.5.
    cross_max_margin_bps: float = 0.0
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
    # RECORDING ONLY. The gate above is right for trading -- a stale book must
    # never price an order -- but it also silently decides what the tape
    # contains, and off-primary that is most of it. Ask coverage in the recorded
    # quotes: btc-5m ~100% (it is the primary and gets the REST book every
    # poll), eth-5m 31%, bnb-5m 17%, the 15m families ~22%.
    #
    # The cost is not theoretical. Asked on 2026-09-15 whether each symbol
    # should have its own tuned values -- the right instinct -- the answer was
    # that it cannot be settled, because at the shipped gates bnb-5m has FOUR
    # priceable crossings and every 15m family has fewer than seven. btc has 302.
    # Every per-symbol question is blocked on this one number.
    #
    # So the recorder takes a wider window and STAMPS THE AGE (`book_age_ms`),
    # which is strictly more information: a replay can filter to whatever
    # freshness it wants, and one that ignores the column is no worse off than
    # today. It never feeds an order -- the trading paths keep
    # ws_book_max_age_s, and the recorded value only fills in where the fresh
    # lookup returned nothing. 0 = fall back to the trading gate.
    ws_book_record_max_age_s: float = 30.0
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
    # RAISED 0.25 -> 0.40 with cross_limit_buffer_inv: at inv 0.30 a cheap ask
    # wants 0.30*(1-0.2) = 0.24, and the old cap would have clipped exactly the
    # cheap-ask cell where the reprice is largest (median +0.340).
    #
    # PROVENANCE WARNING (2026-09-17). The `cross_limit_buffer=0.02` the LIVE
    # box runs comes from a canary dated 2026-09-15 -- the BINANCE venue, since
    # the Polymarket flip is 2026-09-16 (see the first PM-era note in
    # execution/live.py). That n=386 canary therefore does NOT measure this
    # venue, and neither does the "live Binance ledger (110 decided fills)"
    # quoted under cross_max_price. Do not cite either as Polymarket evidence.
    #
    # What DOES support a tight buffer here, measured on Polymarket:
    #   * the EDGE is in the fire quote only: buying a completed crossing at
    #     the displayed ask is +0.026..+0.042 a share, at every depth through
    #     the strike, both halves positive (4,246 settled rounds, quotes joined
    #     to winners, one entry per round).
    #   * the REPRICE at our own ~0.44s send is a median +0.115, p90 +0.260
    #     (24 live paired triggers, `cross fine` at 50ms resolution; p90 +0.590
    #     for fire asks under 0.30). The reprice is 3-4x the whole edge, so a
    #     buffer big enough to fill THROUGH it converts +0.03 into about -0.08.
    # So on this venue the tight end is the right end, for a reason independent
    # of the Binance number: chase the reprice and you pay more than the trade
    # is worth. `pm_cross_sweep.py` (Polymarket tape, 274 rounds, lag 300ms)
    # agrees on the direction -- max_px 0.45 +0.3882 | 0.55 +0.1817 |
    # 0.65 +0.1166 | 0.90 +0.0944, monotone, though no cell reaches |t| >= 2.
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
    # ---- REVERSED 0.125 -> 0.30 on 2026-09-15, on the LIVE substrate ------
    # The halving to 0.125 earlier the same day was measured on `quotes_arm`,
    # and this file already records why that replay cannot answer this question:
    # "the 1s replay is 6.8% cheap triggers while live is 28.6% (live runs the
    # websocket at ~114/s and catches crossings BEFORE the venue reprices; the
    # replay catches them AFTER)". A substrate that only sees post-reprice asks
    # structurally cannot measure the buffer needed to survive the reprice.
    #
    # The arm logs its own answer. `cross meter` records the book at +0.25s,
    # +0.50s and +1.00s after every trigger. Over 34 live triggers with a
    # settled round, the reprice after a cross is:
    #
    #     +0.25s  median +0.1200   p90 +0.2800
    #     +1.00s  median +0.1490   p90 +0.3500
    #
    # against a buffer of 0.125*(1-ask), which is 0.059 at an ask of 0.53. So
    # the limit was covering roughly a quarter of the move it exists to chase:
    #
    #     ask 0.0-0.4  buffer 0.088  median reprice +0.340  covered   0.0%
    #     ask 0.4-0.6  buffer 0.059  median reprice +0.140  covered  23.5%
    #     ask 0.6-0.8  buffer 0.044  median reprice +0.040  covered  60.0%
    #
    # The consequence is adverse selection, and it is severe. The limit is only
    # hit when the crossing was NOISE; when it is real the book runs past us:
    #
    #     FILLED  n=12  win 0.583  paid 0.622            EV/$1 -0.0572
    #     MISSED  n=22  win 0.818  (+1.00s book 0.685)   EV/$1 +0.2466
    #
    # We were filling the losers and missing the winners. Simulated over those
    # 34 triggers, filling at our own limit (which is what this venue does --
    # realised price improvement is +0.0113) against the +1.00s book:
    #
    #     inv    fills  win   EV/$1    TOTAL
    #     0.125     10    7  +0.0173   +0.173   was shipped
    #     0.200     12    9  +0.1332   +1.598
    #     0.300     19   15  +0.1404   +2.668   <- here
    #     0.400     23   18  +0.0853   +1.963
    #     0.500     28   21  -0.0003   -0.009
    #     1.000     34   25  -0.2771   -9.420
    #
    # It is a peak, not an edge of a range: too small and the good trades escape,
    # too large and we overpay for the noise ones. n=34 (19 fills at this cell)
    # is thin, but it is the only substrate that can see the effect at all.
    #
    # LATENCY IS THE BIGGER LEVER. The same 0.125 buffer against the +0.25s book
    # returns +5.447 -- double the best buffer can do at +1.00s. Cutting the
    # decision-to-order round trip is worth more than any value of this knob.
    # REVERTED 0.30 -> 0.125 on 2026-09-15 06:40. The raise was made on a
    # 34-trigger simulation and its first two live fills both lost, at prices
    # the arm's OWN ev_at_fill scored -0.158 and -0.194:
    #
    #   05:34 bnb  decided 0.6148  limit 0.73  FILLED 0.7236
    #   06:34 btc  decided 0.5617  limit 0.69  FILLED 0.6889
    #
    # Both limits are exactly ask + 0.30*(1-ask). At 0.125 they would have been
    # 0.66 and 0.62 and neither would have filled. The raise did not buy better
    # trades; it converted "no fill" into "fill at a price the arm cannot win
    # at". See cross_max_fill_px for the bound that now enforces that directly.
    # ⛔ THIS KNOB IS INERT WHILE `cross_venue_take` IS ON, measured 2026-09-19
    # with `tools/pm_two_book.py`. The send path re-bases the cap on the VENUE's
    # ask, and `venue + buffer` always covers `venue`, so the fill test
    # collapses to `venue <= cross_max_fill_px`. Sweeping inv from 0.05 to 0.25
    # returns IDENTICAL results -- same n, same misses, same P&L, both looks.
    #     venue 0.50 -> cap 0.53 @inv.05, 0.62 @inv.25 -- fills either way
    #     venue 0.66 -> cap 0.65 either way             -- misses either way
    # It binds only when the re-base is OFF or the venue is unreadable, where it
    # matters a great deal (inv 0.05: 411 misses $+122; inv 0.25: 148 misses
    # $+156).
    # ⛔ SO THE 0.200 -> 0.125 CHANGE MADE THAT DAY DID NOTHING, and the fill
    # improvement credited to it (59% of fills over the decided price, then 1 of
    # 6) belonged to the `cross_max_fill_px` 0.70 -> 0.65 revert shipped in the
    # same breath. Recorded because the wrong lever was thanked for it.
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
    # ---- DISABLED 20.0 -> 0.0 on 2026-09-15, on its own live record -------
    # It fired four times and blocked THREE WINNERS:
    #
    #   01:29:36  bnb-updown-15m-1789434900  blocked Down  winner Down  WOULD HAVE WON
    #   01:29:38  bnb-updown-15m-1789434900  blocked Up    winner Down  would have lost
    #   01:29:45  bnb-updown-15m-1789434900  blocked Down  winner Down  WOULD HAVE WON
    #   04:34:18  bnb-updown-5m-1789446600   blocked Up    winner Up    WOULD HAVE WON
    #
    # The class statistic it shipped on (flipped crossings edge -0.0071 on n=14
    # against +0.1771 for clean ones) has not been refuted -- but it was n=14,
    # and it was scored on a 1s replay where a "flip" is a different object from
    # the one the live websocket clock produces at ~114/s. Four live blocks, 3
    # of them winners, is the first evidence from the substrate that matters and
    # it points the other way.
    #
    # Kept in the code with 0 = OFF rather than deleted: the hole it closes is
    # real (`cross_allow_reversal` arms on the ENTRY, so a DECLINED crossing
    # leaves it unarmed) and the tests still pin the behaviour. Re-enable only
    # with live fills, not another replay.
    cross_flip_block_s: float = 0.0
    # HARD CEILING ON WHAT WE PAY, not on the ask. 0 = OFF.
    #
    # Every knob before this bounded the ASK and then let a buffer add to it, so
    # the price actually PAID was unbounded up to cross_max_price + buffer. All
    # 43 settled cross fills, bucketed by the price paid:
    #
    #     fill 0.00-0.55  n= 5  won 0.400  break-even 0.496  edge -0.096
    #     fill 0.55-0.65  n=19  won 0.632  break-even 0.605  edge +0.026
    #     fill 0.65-0.75  n=13  won 0.538  break-even 0.691  edge -0.152
    #     fill 0.75-1.00  n= 6  won 0.833  break-even 0.835  edge -0.002
    #
    # The arm makes money in ONE band, 0.55-0.65. The overall win rate is
    # 26/43 = 0.605, so a fill above ~0.60 cannot pay for itself -- and the
    # 0.65-0.75 bucket, where the 0.30 buffer put two fills on 2026-09-15, loses
    # 15 cents per dollar.
    #
    # A win rate is an arm-wide property; the price paid is per order. Bounding
    # the second is the only one of the two that can be enforced at the moment
    # it matters. Applied to BOTH paths: it caps the LIMIT ceiling and the
    # MARKET `max_px`, so neither instrument can buy outside the band.
    # ⭐ 0.65 -> 0.70 on 2026-09-19, the first change chosen by a JOINT search
    # under the FULL live gate chain rather than a one-at-a-time sweep of six
    # gates. `tools/pm_joint_chain.py` scores 1,728 cells; the selection rule
    # was fixed BEFORE the run, because 1,728 cells on a four-day tape will
    # hand you a winner whether or not one exists: rank by MIN(train look 1.0,
    # train look 2.0) -- which puts look-stability inside the selection instead
    # of checking it afterwards -- then require the winner to beat what ships
    # on the HELD-OUT days at BOTH looks, or ship nothing.
    #
    # With every other knob at its shipped value:
    #     px_hi   TRAIN L1/L2       TEST L1/L2        FULL L1 win/$/t
    #     0.60    +32.17 / +31.41   +26.64 / +29.94   61.3%  +58.81  +2.55
    #     0.65    +44.95 / +44.96   +19.17 / +18.29   63.1%  +64.12  +2.70
    #     0.70    +50.23 / +55.52   +27.00 / +22.19   65.1%  +77.23  +3.28
    #     0.75    +44.24 / +49.29   +25.68 / +19.83   65.1%  +69.92  +2.91
    #     0.90    +41.05 / +49.40   +24.35 / +18.34   65.6%  +65.39  +2.63
    # Best on train, on test and on the full tape at BOTH looks, and INTERIOR:
    # 0.75 through 0.90 are all worse, so this is not a grid edge. Nine of the
    # joint search's top ten cells carry 0.70, so it is not one lucky cell.
    #
    # ⛔ THE MARGIN IS THIN AND MUST BE WATCHED. A fill AT 0.70 needs 71.5% to
    # break even (fee 0.0147) against 66.6% at 0.65. The band pays for itself
    # only because, inside the arm's own margin window, an ask in [0.65,1.00)
    # wins 72.5% (n=12840) -- about one point of cushion. If the realised win
    # rate on fills over 0.65 drops under ~71% across ~40 of them, this is the
    # first knob to revert.
    # ⛔ AND IT DOES NOT LICENSE A LOWER FLOOR. The same search prefers px_lo
    # 0.30, and that is refused: the +0.0038 execution cost these numbers use
    # was measured on 119 live fills averaging 0.554, i.e. inside [0.45,0.65].
    # Below 0.45 the measured 2 s repricing is +0.2174, so extending the floor
    # would price fills with a constant known to be wrong exactly there.
    # Revert: CROSS_MAX_FILL_PX=0.65 and cross_max_price=0.65 + restart.
    # ⛔ 0.70 REVERTED TO 0.65 THE SAME DAY, 2026-09-19. The table above is not
    # withdrawn -- on the tape 0.70 really is the interior optimum -- but the
    # tape PRICES ENTRIES AT THE QUOTED ASK, and the live FAK does not pay the
    # ask. It sweeps up to whatever ceiling it is handed. So a wider ceiling did
    # not admit better trades, it bought room to overpay:
    #     59% of the day's 63 fills landed ABOVE the price the gate decided on
    #     25% overpaid by more than 5 cents; mean +0.0176, worst +0.13
    #     09:52 eth decided 0.4700 limit 0.58 FILLED 0.5800, ev_at_fill -0.2041
    # That last line is the same failure `cross_limit_buffer_inv` already
    # records from 2026-09-15 ("it converted no fill into fill at a price the
    # arm cannot win at"), and the root cause was there too: the live .env
    # carried inv=0.200 against this file's 0.125, so the ceiling at a 0.48 ask
    # was 0.58 rather than 0.54. Both are now 0.65 / 0.125.
    #
    # THE LESSON IS ABOUT THE REPLAY, NOT THE NUMBER. Any knob that widens what
    # the arm MAY pay cannot be chosen on a tape that assumes what it DOES pay.
    # Re-raising this needs a fill model, not a better sweep.
    cross_max_fill_px: float = 0.65
    # ---- the SAME bound expressed RELATIVE to the ask we actually saw ----
    # `cross_max_fill_px` and the `ask + buffer` ceiling are both ABSOLUTE, so
    # the allowance they give is REGRESSIVE in the ask: 1.5x at 0.50, 3.0x at
    # 0.10. The cheap end is exactly where a stale trigger ask is most likely --
    # and where the relative overpay is largest.
    #
    # MEASURED LIVE 2026-09-17, `sol-updown-5m-1789649400`: trigger ask 0.100,
    # ceiling 0.28, FILL 0.200 -- 2.0x the only ask we ever saw -- on a round
    # that resolved against us. Two candles later `doge-updown-5m-1789649700`
    # paid 0.46 against a 0.61 ask (0.75x).
    #
    # SWEPT on the 8-day spring corpus (tools/pm_range_gate_corpus.py --sweep
    # cap; 122 triggers, band [0.10,0.65], entry lag 500 ms). Cap = k x the ask
    # seen AT THE TRIGGER; a capped trigger is DROPPED, not repriced (repricing
    # is the maker-path artefact in backtest-artifacts.md):
    #
    #     k     n    dropped   win     EV/sh     halves
    #    1.00   77     45     55.8%   +0.1850   flip
    #    1.50  114      8     53.5%   +0.1506   +0.180/+0.122
    #    2.00  118      4     52.5%   +0.1430   +0.160/+0.126
    #    off   121      1     53.7%   +0.1583   +0.150/+0.166
    #
    # SO THIS IS NOT AN EV LEVER -- from 1.25 to off the EV is flat within 0.01
    # and the halves disagree. k=1.0 has the best EV/trade and the best win rate
    # but drops 37% of triggers to buy it, and those triggers were net POSITIVE,
    # so its total PnL is WORSE (+14.3 vs +19.2 on 121).
    # 1.5 is the near-free cell: it bounds the 2.0x class, costs 8 of 122
    # triggers and 0.0077 EV/sh, and still allows the 0.75x fill above. It is a
    # RISK bound, not an edge -- do not quote it as an improvement.
    #
    # ⚠️ SHIPPED **OFF** (0), and the reason is the total, not the average.
    # At 1.5 it drops 8 of 122 triggers and moves total PnL +19.15 -> +17.17,
    # i.e. it pays ~10% of the arm's money to buy a bound. Worse, it does not
    # prevent the loss it was built for: `sol-updown-5m-1789649400` lost its
    # whole $1 stake either way -- a cheaper fill only shrinks the PAYOUT on a
    # win, and the round resolved against the arm. So the trade is real and
    # unfavourable; the evidence for taking it does not exist yet.
    # It also contradicts a MEASURED rule the buffer was inverted to satisfy:
    # a cheap ask means the venue has not repriced a fresh cross yet, so the
    # book is about to run furthest there (all nine live cancellations had a
    # decided ask of 0.14-0.57). Turning this on bids LEAST where the arm has
    # measured that it needs the most, and pays for the privilege.
    # Switch to 1.5 only if a bounded worst-case overpay is worth ~10% of PnL.
    # 0 disables it and keeps the pure `ask + buf` ceiling.
    cross_max_fill_multiple: float = 0.0
    # ⭐ THE TAKE PRICE IS BUILT FROM THE VENUE'S OWN BOOK, NOT OUR LADDER
    # (shipped 2026-09-18). The arm decides on `Touch.sweep()` of its ws book and
    # then builds the FAK limit from that same number -- so when the ws ladder
    # carries a level the venue does not have, the limit is below the ask that
    # actually exists and the FAK cannot match. MEASURED on the 75 live order
    # attempts of 09-16..09-18: of the 20 NO-FILLs whose shadow probe caught the
    # venue's book, **17 had a venue ask above our 0.65 cap** while we had
    # decided at ~0.60, and the mean (venue ask - our limit) was **+0.155**. The
    # clearest:
    #     `shadow book xrp-updown-5m-1789704600 Down: venue 0.3200/0.6900 |
    #      ours 0.3200/0.4200` -> decided 0.42, limit 0.54, "no orders found"
    # i.e. our `sweep()` read 0.42 at the same instant the venue's book read
    # 0.69. The miss was ARITHMETIC, not latency.
    # ON: the take path re-reads `GET /book?token_id=` (read-only, the same call
    # the shadow probe makes) and re-runs the ceiling arithmetic on THAT ask --
    # `cross_max_price`, the inverse buffer, `cross_max_fill_multiple`, then the
    # fill cap. The bands, the margin floor and the caps are unchanged, so this
    # can only refuse a trade or price it correctly; it cannot enlarge one. When
    # the venue book is above the cap the order is now REFUSED WITH A DECISION
    # ROW instead of burning a FAK round-trip, and an unreadable venue book
    # falls back to the old base, logged. It applies to the TAKER branch, which
    # is the one the cross arm uses (`pm_order_type=fak`); the post-only LIMIT
    # branch is unchanged.
    # WHAT IT DOES NOT CHANGE: the retry. A `fak_unfilled` now re-prices and
    # re-sends inside the EXISTING attempt loop, bounded by `order_max_attempts`
    # (3 sends per decision), `order_retry_delay_s`, the `order_min_tau_s` floor
    # and the venue-position re-read that makes doubling impossible.
    # Revert = `CROSS_VENUE_TAKE=false` + restart (old base, one less REST call).
    # Send the FAK with `cross_max_fill_px` as its ceiling and SKIP the venue
    # probe entirely. 0/False = off, which is the shipped default.
    #
    # ⭐ WHY THE PROBE IS REMOVABLE. It costs ~292ms inside the order path to
    # derive a ceiling. Over 195 real FAK fills that ceiling NEVER ONCE BOUND:
    #     filled exactly AT the ceiling : 0.0%
    #     headroom (ceiling - filled)   : median +0.0700, p10 +0.0200
    #     filled at or below the ask    : 73.8%, median sweep +0.0000
    # The size-at-touch tape says why: at a $1 stake the touch alone fills the
    # order 98.84% of the time, so it never reaches a level a ceiling could
    # refuse. What the probe buys is HEIGHT, not precision -- a ladder-derived
    # ceiling is too LOW 82.7% of the time and the FAK finds nothing. The fill
    # cap is also always high enough, and it is free.
    #
    # WHAT THE LATENCY IS WORTH. Slippage (filled - decided) is monotone in
    # time to fill, n=204 live fills:
    #     0.6-0.9s  n= 16  median +0.0000  mean -0.0044
    #     0.9-1.2s  n=135  median +0.0100  mean +0.0228
    #     1.2-2.0s  n= 28  median +0.0200  mean +0.0242
    #     2.0s+     n= 25  median +0.0600  mean +0.0352
    #     $3.79 total over 204 fills, $0.0186 per fill
    # This is the measurement that overturned an earlier `cross meter` reading
    # of "median +0.0000 over 250ms" -- that one tracked OUR LADDER's quoted
    # price, not the price we pay, and it is why two versions of a prefetch got
    # built and withdrawn before the probe itself was questioned.
    #
    # ⛔ WHAT IT GIVES UP. No probe means no venue price, so
    # `cross_ladder_band_pad`'s venue-side FLOOR check cannot run -- the one
    # that caught `venue 0.3900 while our ladder read 0.5100` twice on
    # 2026-09-19. Turning this on turns that off, and `setup()` says so in the
    # log rather than leaving it to be discovered. The fill-cap refusal
    # SURVIVES: it re-runs against `base`, which is then our ladder, and the
    # ladder sits BELOW the venue 82.7% of the time, so `ladder > cap` still
    # implies `venue > cap`. What is lost there is 93 pre-flight refusals
    # becoming FAKs that cannot match -- ~450ms each and no money.
    # ---- LOCKED-IN TAIL: buy the near-certain favourite near the bell ----
    # THE ONE EDGE MEASURED TODAY THAT IS LATENCY-IMMUNE. Priced at the tick and
    # then at realistic delays, on 15,755 qualifying ticks:
    #     0ms +$0.0806/bet | 1000ms +$0.0768 | 2000ms +$0.0786 | 5000ms +$0.0929
    # The crossing arm, on the same tape, fell from +6.2 points to +0.9 over
    # that first second. This does not decay because "the outcome is locked" is
    # not a print the venue races to reprice -- it is a slow drift to 1.00.
    #
    # THE MECHANISM. Near the bell the paid 60s TWAP window is partly PRINTED,
    # so the outcome is close to determined while the book still walks the
    # price up. The favourite-longshot bias is monotone and large-sample:
    #     model P 0.90-0.95  n=35679  realises 91.5%  market implies 88.3%
    #     model P 0.95-0.98  n=30223  realises 93.5%  market implies 91.2%
    #     model P 0.00-0.02  n=61052  realises  3.1%  market implies  5.6%
    # The venue's fee curve helps: 0.07*p*(1-p) is 33 bps at 0.95 against 364
    # bps at 0.50, so the extreme is exactly where trading is cheapest.
    #
    # ⛔ NOT PROVEN, AND THE HONEST NUMBER IS THE ROUND-LEVEL ONE. Ticks inside
    # a round share one outcome, so per-tick statistics are inflated by about
    # sqrt(9.7). Corrected to one bet per round:
    #     n=1618 rounds  +2.3%/bet  t+1.04  day-clustered t+0.32
    #     by day: 09-16 -0.0275 | 09-17 -0.0106 | 09-18 +0.0696 | 09-19 -0.0044
    # Three of four days negative and one day carries it. That is why this
    # ships OFF and why `locked_tail_shadow` exists: it needs weeks, not days.
    locked_tail_enabled: bool = False
    # LOG ONLY, never order. The arm writes what it WOULD have done so the
    # opportunity rate and the price it would have paid can be measured live
    # before a dollar is risked. Leave True until the day-clustered t clears 2.
    locked_tail_shadow: bool = True
    # How certain the model must be before the favourite is worth buying.
    locked_tail_min_p: float = 0.95
    # How far below our probability the ask must sit, after fees, to be worth
    # taking. The measured bias is 1-3 points; asking for 2 keeps the thin half.
    locked_tail_min_edge: float = 0.02
    # The window where the TWAP is partly locked. The edge is here and nowhere
    # else: at tau 60-300 it measured +$0.004/bet, which is nothing.
    locked_tail_tau_lo: float = 5.0
    locked_tail_tau_hi: float = 60.0
    # Never pay more than this for a favourite -- above it the remaining edge
    # cannot cover a single adverse round.
    locked_tail_max_px: float = 0.97
    # ---- POST-CLOSE SCAN: buy a settled winner the book has not repriced ----
    # How many seconds past the bell to keep scanning a finished round. 0 = OFF.
    #
    # THE EDGE. After the bell the outcome is DETERMINISTIC -- the 60s TWAP
    # window is fully printed -- but the venue's resolution is not instant and
    # the book keeps trading. MEASURED on the recorded tape, post-bell rows
    # whose book was genuinely fresh:
    #     book_age < 2000ms  27271 rows   88 chances to buy the winner < 0.99
    #     book_age < 1000ms  17801 rows   52 chances   mean ask 0.8223
    #     book_age <  500ms   9195 rows   30 chances   mean ask 0.8050
    # Buying a decided winner at 0.82 is +0.18/share with no directional risk.
    #
    # ⭐ THIS IS THE ONLY CONCEPT TESTED TODAY THAT SURVIVED EVERY FILTER. The
    # complete-set arb (`up_ask+down_ask < 1`) hit 408 of 1,938,243 ticks and
    # ZERO once a fresh book was required -- every instance was a stale frame.
    # This one does not vanish when the same filter is applied.
    #
    # ⛔ THE RISK IS OUR OWN ARITHMETIC, not the market. We compute the winner
    # from our spot tape; that reproduces the venue's settlement on 98.6% of
    # rounds. The residual 1.4% is a round we call wrong and buy at ~0.86,
    # losing it. At those odds the trade is still strongly positive
    # (0.986 - 0.86 = +0.126/share), but `post_close_min_margin_bps` exists so
    # a near-tie -- where our reconstruction is least reliable -- never fires.
    post_close_scan_s: float = 0.0
    # Highest ask we will pay for an already-settled winner. Above this the
    # book has repriced and there is nothing left to take.
    post_close_max_px: float = 0.97
    # How far TWAP60(bell) must sit from TWAP60(open), in bps, before we
    # believe our own reconstruction enough to bet on it. A near-tie is exactly
    # where a 1.4% error rate concentrates.
    post_close_min_margin_bps: float = 1.0
    # Freshest-book requirement for the post-close read, in ms. The whole edge
    # is an un-repriced book; a stale frame is how a phantom one is invented.
    post_close_max_book_age_ms: float = 1000.0
    cross_flat_take_ceiling: bool = False
    cross_venue_take: bool = True
    # How old a PREFETCHED venue ask may be and still price a ceiling, in
    # seconds. 0 = OFF, which is the shipped default and the old path byte for
    # byte (see Bot._prefetch_venue_top).
    #
    # WHAT IT BUYS. Measured 2026-09-19 over the live log, decision -> verdict
    # is 1001ms median / 1999ms p90, and only two of its steps are network:
    #     get_quote   LOCAL `book.best()` on this venue          ~0ms
    #     venue_top   REST                                     ~292ms
    #     FAK ack     REST, and this IS the order               452ms median
    # `venue_top` must finish before the FAK can be priced, so they cannot
    # overlap -- but the SIDE is knowable before the crossing (below the strike
    # the next cross is Up), so the fetch can start a tick early. That is the
    # whole of it: ~292ms off the order path, or ~29% of the median.
    #
    # ⭐ WHY A STALE BASE CANNOT COST MONEY HERE. The probe does not set the
    # price we pay. A FAK fills at the BOOK bounded by the limit, which the
    # live fills show plainly -- they land below BOTH the ceiling and our own
    # estimate:
    #     pm FAK FILL 2.08333 Down @ 0.48 ... ceiling 0.59 ask 0.53
    #     pm FAK FILL 1.78571 Up   @ 0.56 ... ceiling 0.65 ask 0.62
    # The base feeds `_take_cap_for`, which sets the CEILING, and
    # `cross_max_fill_px` hard-bounds that ceiling regardless. A stale base
    # moves the ceiling slightly and cannot move the fill or lift the cap.
    #
    # ⛔ THE RISK IS THE OTHER DIRECTION, and it is why this is a knob. A base
    # that is too LOW builds a ceiling under the ask that exists and the FAK
    # finds nothing -- which is today's failure mode, the one `cross_venue_take`
    # was added to fix. Too tight a bound wastes the prefetch (falls through to
    # the synchronous fetch, no harm); too loose reintroduces the miss. The
    # venue ask moves a median 0.070 per ~320ms of our own book staleness, so
    # anything past ~0.5s is giving back what the re-base bought.
    # ⛔⛔ MEASURED, ENABLED FOR FIVE MINUTES, AND TURNED BACK OFF. DO NOT
    # ENABLE THIS WITHOUT RE-MEASURING BOTH NUMBERS BELOW. The reasoning above
    # is sound and the implementation is correct and tested; the premise -- that
    # the 292ms is worth paying for -- is what failed.
    #
    # WHAT IT WOULD SAVE, from 2,143 live `cross meter` samples, which record
    # the price at +0.25s / +0.50s / +1.00s after each decision:
    #     +0.25 -> +0.50 (250ms)  median +0.0000   mean +0.0118
    #     +0.25 -> +1.00 (750ms)  median +0.0000   mean +0.0181
    #     the price moved AT ALL within 250ms: 43.8% of the time
    # So in the ~292ms this buys back, the price moves a MEDIAN OF ZERO. More
    # than half the time it has not moved at all. The mean is a cent, and it is
    # a tail.
    #
    # WHAT IT WOULD COST. The prefetch fires per family per tick with no
    # crossing, deduped at max_age*0.5. At 9 families that is ~19.3 probes per
    # SECOND against the ~9-12 per HOUR the order path pays today -- roughly a
    # 1,500x load increase on `GET /book`, the same public endpoint the FAK
    # depends on. Gating on imminence helps but does not rescue it; measured
    # over 1,060,056 replay ticks:
    #     |margin| <= 1 bps   12.75% of ticks   63.3% of pre-crossings   2.5/s
    #     |margin| <= 2 bps   24.06% of ticks   84.9% of pre-crossings   4.6/s
    #     |margin| <= 5 bps   50.39% of ticks   96.9% of pre-crossings   9.7/s
    # The best of those is still ~400x today's rate.
    #
    # THE TRADE IS THEREFORE: risk throttling the endpoint the order path runs
    # on, to save a median of zero. No live rate-limiting was observed in the
    # five minutes it ran (the "429"s in the log are `win_rate=0.429`), but
    # five minutes is not evidence and the downside lands on the money path.
    #
    # Kept rather than deleted because it is correct, tested (19 tests) and
    # inert at 0.0, and because the measurement is the valuable part: if the
    # venue ever gets slower or the drift ever gets steeper, this is ready and
    # the two numbers to re-check are named above.
    cross_venue_prefetch_max_age_s: float = 0.0
    # How near a crossing must be before the prefetch spends a round trip, in
    # bps of the signal's margin past its level. 0 = no gate, which is what the
    # first version effectively had and why it was 19 probes a second.
    # MEASURED over 1,060,056 replay ticks -- share of ticks kept against share
    # of the ticks that IMMEDIATELY PRECEDE a crossing:
    #     <=1 bps   12.75% of ticks   63.3% of pre-crossings
    #     <=2 bps   24.06% of ticks   84.9% of pre-crossings
    #     <=5 bps   50.39% of ticks   96.9% of pre-crossings
    # 2.0 keeps 85% of the value for a quarter of the traffic. The tau window
    # and the `_cross_fired` slot are ALREADY spent before the prefetch runs,
    # so this is the only remaining question worth asking.
    cross_venue_prefetch_margin_bps: float = 2.0
    # Minimum seconds between two probes for the same token. ITS OWN KNOB: the
    # first version reused `max_age * 0.5`, so a 0.35s freshness bound refetched
    # every 0.175s, forever, per family. "How old may a warm ask be" and "how
    # often may we ask" are unrelated questions and conflating them is what
    # produced the traffic.
    cross_venue_prefetch_refresh_s: float = 1.0
    # ---- TRAIN entry trigger: DISTANCE from the strike, not a crossing ----
    # Explicitly an OVERRIDE of three of the arm's gates, taken as a risk
    # decision by the operator on 2026-09-15. The strike IS the round's open, so
    # spot >=10bps from it means the round has travelled >=10bps and the entry
    # is deep -- which is exactly what max_round_range_bps (10.0) and
    # max_depth_bps (5.0) refuse, and what cross_max_fill_px (0.65) puts out of
    # reach. Those three are therefore replaced for this trigger only.
    #
    # Measured on the live book (tools/momentum_continuation.py, coherent asks,
    # 5,776 rounds, fee 0.0117, one entry per round, day-clustered t), REPORTED
    # AT ask+1.5c because 69% of live fills land exactly on the limit:
    #     X>=10 D=0  n= 75  px 0.799  hit 89.3%  +0.085/sh  day-t +2.94
    #     X>=15 D=0  n= 39  px 0.817  hit 94.9%  +0.123/sh  day-t +4.04
    #     opposite side, same entries  n=68  hit 11.8%  -0.128/sh  t -3.83
    # DELAY: D=0 only. D=2/5/10 collapse the day-t to -0.33/+0.52/+0.32, because
    # the continuation premium is spent in the first seconds.
    # NOT reconciled with the n=321 coherent-favourite result (-0.0221 at the
    # ask), and 76% of these fires sit on rounds the range gate refuses.
    # ⛔ PROVENANCE CORRECTED 2026-09-18 BY THE OPERATOR: that table is
    # BINANCE-ERA (pre-2026-09-16 flip). It does NOT transfer to Polymarket and
    # must not be cited as Polymarket evidence for this trigger -- the same
    # mistake the `cross_limit_buffer` canary made (see the `.env`). The PM-era
    # record for the train trigger is **n=2 fills on $2.00 = -$0.72**
    # (xrp-5m 09-17, one win one loss), i.e. nothing at all. RE-MEASURE on the PM
    # tape before this is enabled on the strength of the numbers above.
    # ⭐ THE RE-MEASURE THE NOTE ABOVE DEMANDED, DONE 2026-09-19 --
    # `tools/pm_train_chain.py`, PM tape, 4057 settled rounds. VERDICT: the
    # shipped configuration has no edge worth the name, and the variant that
    # looks better cannot be validated. STAYS OFF.
    #
    # THE BINANCE TABLE DOES NOT TRANSFER, exactly as the correction said. Its
    # 89-95% hit rates become 80.5% here, and at a mean price of 0.795 the
    # break-even is 79.5%: a margin of +1.1 points on n=442, t +1.62. It is a
    # coin flip that pays its own fee. Above 20 bps it goes NEGATIVE:
    #     X>=10bps  n=1246 win 77.4% be 77.8%  margin -0.4  $+53.30  t+1.98
    #     X>=15bps  n= 442 win 80.5% be 79.5%  margin +1.1  $+22.61  t+1.62
    #     X>=20bps  n= 156 win 78.8% be 80.1%  margin -1.3  $ -3.82  t-0.56
    #     X>=25bps  n=  74 win 75.7% be 80.5%  margin -4.8  $ -6.12  t-1.29
    #
    # ONE BINANCE FINDING DOES REPLICATE: the edge lives in the first seconds.
    # D=0 makes +$22.61, a 2 s delay makes -$2.06, a 5 s delay -$0.52. That is
    # the same shape the old table reported -- and it means the trigger races
    # latency, against a measured token lag of 400-700 ms.
    #
    # ⛔ AND THE CHEAP CEILING, WHICH LOOKS TEMPTING, CANNOT BE HELD OUT. The
    # edge appears at cheap entries, not the 0.89 this ships:
    #     max_ask 0.89  n=442 margin  +1.1  t+1.62
    #     max_ask 0.70  n= 98 margin  +8.0  t+2.17
    #     max_ask 0.65  n= 76 margin +13.8  t+2.65
    # Split train/test, those cells have ELEVEN entries on the train half
    # against 87 on the test half. A distance trigger fires as a function of how
    # far the day travelled, so its rate swings with the day -- 11 vs 87 is the
    # trigger being day-dependent, not a sampling accident. Where it can be
    # measured it degrades: X>=10 at max_ask 0.65 wins 76.5% on train and 62.8%
    # on test. No selection rule can run on ten observations, so the better
    # ceiling is UNPROVEN rather than good, and `cross_train_max_ask` is left at
    # 0.89 rather than tuned on a cell that cannot be checked.
    #
    # TO REVISIT: re-run `tools/pm_train_chain.py` once the tape has ~2 weeks,
    # which is the first point at which the cheap cells could have a train half.
    # Enabling it also costs SLOTS the cross arm is using, and its edge sits in
    # the same price band the cross arm already trades.
    cross_train_enabled: bool = False
    # ---- GATE PARITY WITH THE CROSS ARM (audited 2026-09-16) -------------
    # The TRAIN trigger is NOT a separate arm: it sets `train = True` and falls
    # through into the cross arm's ordering path, skipping only the gates
    # wrapped in `if not train`. Read this before changing any `if not train`
    # line, and see tests/test_arm_parity.py, which freezes the lists below.
    #
    # SHARED (both arms, verified by reading the path): max_per_round,
    # max_per_day, the reentry gap, min/max_price, stake_usdt/stake_mult,
    # cross_max_round_stake_mult, cross_max_open_positions,
    # cross_max_open_stake_pct, the reserved slot, cross_allow_reversal and
    # cross_skip_opposite_to_main. The risk bounds are genuinely shared.
    #
    # BYPASSED ON PURPOSE, each with a measurement behind it: the tau window
    # (the trigger deliberately lives OUTSIDE 5-60s), max_depth_bps,
    # max_round_range_bps, and the fill cap (cross_max_fill_px 0.65 would
    # forbid a 0.75-0.88 entry outright, so the train arm uses
    # cross_train_max_fill_px instead).
    #
    # BYPASSED WITHOUT THIS COMMENT SAYING SO -- previously undocumented, found
    # by the 2026-09-16 audit. They do not fire today, but turning any of them
    # on does NOT reach the train arm:
    #   cross_flip_block_s   (0.0, off)  -> the whipsaw guard is cross-only
    #   cross_min_depth_ticks (1.0, ON)  -> train may quote a level with no
    #                                       depth at the touch
    #   cross_min_depth_sigma (0.0, off)
    # If the whipsaw guard or the depth floor is ever turned on, decide
    # explicitly whether train should honour it; the arm inherits neither.
    cross_train_min_tau_s: float = 60.0
    # TAU CEILING. Added 2026-09-15 by the sweep, and TURNED OFF THE SAME HOUR.
    # I read the joint table's `t>=180` column as a CEILING when it is MIN_TAU,
    # shipped 180 as a ceiling, and the combination X=15 + tau<=180 collapses the
    # population to n=16 (net +0.018, day-t +1.36) against n=39 (net +0.123,
    # day-t +4.04) for X=15 alone. The ceiling is real but modest at X=10
    # (day-t 1.99 -> 2.31) and non-monotone (240 is negative, t -0.08), and at
    # X=15 it is actively harmful. min_tau is flat 60->180 at X=15, so it was
    # never the lever the joint table's label made it look like. 0 = OFF.
    cross_train_max_tau_s: float = 0.0
    cross_train_bps: float = 15.0
    cross_train_max_ask: float = 0.890
    # THE DYNAMIC EV CEILING (2026-09-16). `cross_train_max_ask` is a
    # hand-picked price; this states the same bound in the unit the arm earns
    # in -- the most we may pay for a MEASURED win rate once the fee is charged
    # AT that price. Solved, not approximated (see `broker.ev_ceiling`).
    #
    # train_p_win is a MEASUREMENT from `tools/pm_train_sweep.py`, not a knob:
    # PM tail-60 rule, thr 15bps, tau >= 60, all families, n=2397 -> win 91.4%,
    # halves 92.1/90.8, drop-best 91.2, be-ask 0.898. Re-run the sweep before
    # moving it; never quote an optimum you have not re-measured.
    # At these values the gate goes 0.890 -> 0.876, i.e. 1.4c TIGHTER, and the
    # static value stays as a backstop (`entry_ceiling` takes the min).
    train_p_win: float = 0.914
    #: How much edge to leave on the table at that win rate.
    ev_ceiling_margin: float = 0.03
    cross_train_bound: float = 0.015
    cross_train_max_fill_px: float = 0.90
    # EVENT ANCHOR. The train condition is a LEVEL (|spot-strike| >= 10bps);
    # without this it fires on any tick where the spot is merely far away, which
    # on 2026-09-15 20:49:54 meant three fills on ONE 15m round whose strike was
    # ten minutes old and whose spot was already 16-25bps out -- bought on the
    # first tick after a restart, at asks of 0.77-0.89. Requires a sample inside
    # the last `train_fresh_s` seconds to have been BELOW the threshold, i.e. the
    # crossing is provably recent. Fails CLOSED without enough history (the
    # restart case). Do not set this to 0 -- 0 means "cannot prove freshness"
    # and the trigger goes inert.
    #
    # 5.0 -> 1.0 on 2026-09-16. This knob IS the D parameter the sweep above
    # calls "the parameter that kills it", and 5.0 shipped the losing cells:
    # _train_is_fresh takes the OLDEST tick inside the window, so 5.0 admits
    # any crossing up to five seconds old. Re-measured on our own tape (4,362
    # rounds with quotes, real strikes, real settlements, one entry per round,
    # X>=15, tau>=60, ask<=0.890, fee included):
    #     D<=1s  n=164  hit 86.6%  @ask+1.5c  +0.0459/sh  day-t +1.39  4/7 days+
    #     D<=5s  n=300  hit 85.0%  @ask+1.5c  +0.0220/sh  day-t +0.82  4/8 days+
    # So 5.0 was trading the +0.022 cell while the +0.046 cell sat inside its
    # own window. Nearly 2x the EV and a t that more than doubles, at roughly
    # half the fire rate -- the right trade when the edge is the scarce thing.
    #
    # The 94.9% this arm was built on is an n=39 artefact: at n=172 the same
    # cell measures 86.6%. Live's 78.0% (32 of 41) against 86.6% is z -1.24,
    # p~0.11 -- NOISE, not a refutation. An earlier verdict in this session
    # compared live against the inflated 94.9% figure and got p=1.8e-04; that
    # comparison was the error, not the arm.
    cross_train_fresh_s: float = 1.0
    # BURST GUARD (2026-09-15 21:01). `|spot-strike| >= 10bps` is a LEVEL, so
    # when btc/eth/bnb move together it goes true for EVERY family at once --
    # and a 5m and its 15m sibling SHARE a strike, so those two are literally
    # the same bet. Measured live: EIGHT entries in 241 seconds, every one of
    # them Down, arriving as
    #     eth-5m + eth-15m  (same tick)   bnb-5m + bnb-15m  (+20s)
    #     btc-5m + btc-15m  (+34s)
    # -- one view expressed six ways, at $1 a leg, and the 15m legs fired 46-80
    # seconds into a 900-second round, a regime the measurement never sampled.
    # The cross arm cannot do this (a CROSSING happens at unrelated times per
    # family); a DISTANCE condition always can. Nothing that bounds RISK
    # catches it either: the caps count positions and stake, not correlation.
    # So: ONE train entry per burst, globally, not per family. 0 disables the
    # guard, which is the behaviour that took the eight.
    cross_train_cooldown_s: float = 0.0
    # TRAIN TRADES THE 5-MINUTE ROUNDS ONLY (operator decision 2026-09-15).
    #
    # The design puts Train in "the first 4 minutes", priced 0.75-0.88, with
    # saturated delta. BOTH of those are properties of a 300-SECOND round:
    #   * `tau >= 60s` only means "the first 4 minutes" when the round is 300s
    #     long. On a 900s round it means the arm is live for 14 of the 15
    #     minutes -- the opposite of a time-boxed complement.
    #   * 10bps at tau 820 is ~1 sigma, NOT in the money. The live record is
    #     unambiguous: every 5m leg filled in 0.79-0.88, every 15m leg filled
    #     at 0.66 / 0.73 / 0.74 -- all three outside the stated band, and the
    #     15m legs are where tau 854 (46s into the round) came from.
    # With the 5m restriction those two properties become the same property.
    # Applies to the TRAIN trigger only; the cross arm still trades every
    # family in CROSS_TRADED.
    cross_train_5m_only: bool = True
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
    # ⭐ SHIPPED OFF 2026-09-19. The table above measured 0.5 / 2.0 / 5.0 on
    # BINANCE-ERA L2 and never tested anything wider. On the PM tape, under the
    # full live chain with the exit on, the gate is monotonically HARMFUL and
    # `off` wins train AND test at BOTH looks:
    #     gate   TRAIN L1/L2         TEST L1/L2
    #     5      +50.50 / +49.58     +51.03 / +65.09
    #     8      +53.78 / +53.81     +62.35 / +72.19   (what ran live)
    #     12     +59.38 / +59.87     +64.82 / +75.26
    #     off    +63.32 / +63.80     +70.82 / +81.25
    #
    # ⛔ AND IT IS NOT OUTLIER-DRIVEN, which is the check that changed the
    # verdict: the 22 entries an 8 bps gate refuses total +19.11 with a MEDIAN
    # of +1.0103 per dollar, and dropping the three largest still leaves +15.66.
    # The typical refused trade is excellent, not one lucky tail. Mechanically
    # they are deep crossings -- >8 bps through the reference -- that the book
    # still prices inside [0.45,0.65], i.e. exactly the disagreement between
    # move and price that this arm exists to take.
    #
    # THE BINANCE FINDING IS NOT CONTRADICTED, only extended: its own note said
    # "widening is also free ... the win rate goes 0.544 -> 0.631 and triggers
    # nearly double". It simply stopped measuring at 5.0.
    # ⛔ 99.0 RATHER THAN 0, AND THAT IS NOT COSMETIC. For this knob 0 means
    # REFUSE ALL, not off -- `depth_bps > 0` is true for every crossing, and
    # `cross_overrides` uses exactly that to disable one family (see
    # test_an_override_on_this_market_blocks_the_entry). It is the one cap in
    # this arm that breaks the repo's 0=off convention. A `> 0` guard was
    # written to fix that and reverted: inverting a documented, tested meaning
    # on the live order path is not worth the tidiness. 99 bps is past the p90
    # round range of every family, so it is off in practice.
    # Revert: CROSS_MAX_DEPTH_BPS=8 + restart.
    cross_max_depth_bps: float = 99.0
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
    # RAISED 10.0 -> 12.0 on 2026-09-17. The gate was refusing EVERY live
    # trigger (`round already moved 10.2bps, gate is 10.0`) while sitting in the
    # middle of its own POSITIVE bin: the table above bins 8-12 at +0.052 and
    # 12-20 at -0.324, so a cut at 10.0 discards the upper half of a bin
    # measured positive while buying no extra distance from the negative one.
    # The growth table agrees (<=12 is 0.2347/0.3376 against <=10 0.2473/0.3463,
    # which that block already calls "not knife-edge"). 8, 12 and 20 are the
    # measured bin edges; 10.0 was an interpolation between two of them.
    #
    # Why the gate binds live but did not in the measurement: the sample above
    # is tau 5-60s, and the live trigger window runs to tau ~207s. Range-so-far
    # is a fixed bps count of a quantity whose expected size grows with the time
    # already elapsed, so the same 10 bps is a large fraction of the diffusion
    # at tau 60 and a small one at tau 207. This is why the arm now also carries
    # an edge gate (`cross_min_edge`): an edge is scale-free where a bps is not.
    #
    # ---- TURNED OFF (12.0 -> 0.0) ON 2026-09-18, OWN REPLAY, SHIPPED WINDOW --
    # REFUTED by `tools/pm_gate_ablation.py`: 3,101 settled PM-era rounds on the
    # SHIPPED chain (fc trigger, look 1.0, tau 5-180, floor 0.25, band
    # [0.10,0.65), one entry per round, n=826). The gate does not merely fail to
    # filter -- it keeps the WORSE half:
    #     range gate KEEPS    n=416 win 56.2% +0.0180/sh t +0.73 day-t +1.11
    #                           halves +0.0285/+0.0074  worst-day -0.0115
    #     range gate REFUSES  n=408 win 65.4% +0.1191/sh t +4.93 day-t +4.93
    #                           halves +0.0991/+0.1392  worst-day +0.0958
    # Every statistic favours the refusals, including the two this repo requires
    # (both halves agree -- and it is the KEPT set that has a negative worst
    # day). Turning it off is +$48.6 on that cohort.
    # WHY -- and it is this block's own paragraph above, coming true. By tau:
    #   tau 0-60   8-12 +0.3723 | 12-16 -0.0439 | 16-20 -0.2266 | 20-30 -0.0927
    #   tau 60-120 12-16 +0.0443 | 16-20 +0.0368 | 20-30 +0.1667 | 30-50 +0.1787
    #   tau 120+   12-16 -0.0308 | 16-20 +0.0886 | 20-30 +0.1587 | 50+ +0.2110
    # The tau<60 row REPRODUCES the original measurement (wide travel = the round
    # reverses); from 60 s on the sign flips and RISES with travel. So the gate
    # is right in a band that now holds 5% of the entries and backwards in the
    # 64% at tau>120 -- because `cross_max_tau_s` was tripled 60 -> 180 the same
    # day, while range-so-far grows with the time elapsed. The sweep is monotone
    # toward off and the SHIPPED values were its worst row:
    #     range OFF n=826 +0.0677 t +3.90 day-t +2.39 worst +0.0580
    #     <=50      n=812 +0.0649 t +3.71 | <=30 n=753 +0.0583 t +3.20
    #     <=20      n=567 +0.0276 t +1.30 | <=12 n=247 +0.0337 t +1.04
    #     SHIPPED   n=416 +0.0180 t +0.73 day-t +1.11 worst -0.0115
    # The scale-free replacement does NOT rescue the idea either:
    # range/(sigma*sqrt(elapsed)) is flat from 0.6 to 2.0 (+0.0653..+0.0674 on
    # n=733..808), so the ratio carries no information. The fix is not a better
    # unit -- there is no regime signal in travel at this window.
    # THE COMBINATION THAT ACTUALLY SHIPS (the tick floor is untouched):
    #     SHIPPED (range + ticks)    n=216 +0.0665 t +1.94 day-t +2.21
    #     range OFF + ticks SHIPPED  n=505 +0.0995 t +4.46 day-t +3.59
    #                                  halves +0.1004/+0.0985 worst +0.0817
    # 2.3x the entries, +50% per share -- and the shipped row is the one that
    # fails this repo's t>=2 bar (1.94) while its replacement clears it.
    # ONE UNMODELLED CAVEAT: `_round_range_bps` reads `self._ticks`, appended
    # once per TICK, and the poll now runs at a median 58 ms (p90 102 ms), so the
    # live measure is DENSER than the 1 Hz `quotes` series these values were
    # fitted on (`pm_range_gate_sweep.py` says so itself). A denser sampler
    # reports a wider max-min, so live refusals were MORE frequent than the
    # replay's -- the bias runs with this change, not against it.
    # WHAT TO WATCH, stated before the fact: the newly admitted class is
    # late-tau / wide-range (n~390 of the 408 refusals, all at tau>=60) and the
    # entry rate should go ~72/day -> ~168/day on that cohort. KILL CRITERION:
    # that class at or below 0 per share over ~100 live fills means the venue had
    # already repriced what the tape says it had not -- put the values back.
    # REVERT = 12.0 here plus the five per-family entries this block used to
    # carry in `cross_overrides` (12.9 eth-5m, 7.8 bnb-5m, 13.6 btc-15m, 21.0
    # eth-15m, 21.1 bnb-15m), then restart.
    cross_max_round_range_bps: float = 0.0
    # MINIMUM MODEL EDGE at the trigger instant: `fair_value(spot, strike, tau,
    # sigma) - ask`, its own side. 0 = OFF (the repo's convention for a gate).
    #
    # SHIPPED AT 0.0 (OFF) on 2026-09-17, deliberately, because the fair it
    # would compare against is the WRONG DISTRIBUTION for this venue.
    #
    # `fair_value` is the POINT-snapshot digital. Polymarket does not settle on
    # a point: it settles on the 60s TAIL TWAP `mean(close[T-60:T]) >= K`
    # (89.9% fit to gamma's own winners, 286 rounds, vs 83.9% for the point
    # rule -- see the repo note on the resolution rule). Terminal variance is
    # therefore sigma^2*60/3, which is 5x less dispersed than a whole-range
    # TWAP and ~2.7x less than the point rule at tau 150s. The two rules agree
    # to +0.4pp at >=15bps, but they diverge most NEAR the strike -- and the
    # cross arm is the arm that sits AT the strike (`cross_max_depth_bps` 5.0,
    # typically under 0.5bps). So a point-rule fair understates how decided the
    # round is, exactly in the arm's operating region, and a gate built on it
    # would refuse the crossings it exists to take.
    #
    # The fix is not a different number here, it is a PM terminal distribution
    # in `btcpredict/venues/polymarket/pricing.py` -- the file the venue layout
    # has always reserved for it and which still does not exist. Turn this on
    # once that fair exists, not before.
    #
    # The TRAIN arm does not apply it. That arm's economic gate is
    # `entry_ceiling(cross_train_max_ask, TRAIN_P_WIN[sym], ev_ceiling_margin)`
    # -- already a model-derived ceiling -- so applying this one too would price
    # the same decision twice and the tighter would silently become the rule.
    # See tests/test_arm_parity.py's BYPASSED_ON_PURPOSE.
    cross_min_edge: float = 0.0
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
    #
    # ---- LOWERED 5.0 -> 1.0 on 2026-09-15 -------------------------------
    # The table above has no ASK column, and that is the whole error. It is the
    # trap this file has now recorded FOUR times: the bar is the mean ask OF THE
    # SAME SUBSET, never a win rate compared across subsets priced differently.
    #
    # A shallow crossing is one the venue has NOT REPRICED YET, so it is cheap.
    # Re-measured over 216 crossings passing every gate except this floor (btc
    # corpus, eth and bnb from the live DB, whipsaw guard applied), with the ask:
    #
    #                        n    win    ask     EDGE
    #     below floor (<5t) 19  0.789  0.558  +0.2311
    #     at/above floor   197  0.863  0.686  +0.1771
    #
    # Shallow crossings win less AND cost less, and the second effect is larger.
    # Consistent in sign on all three families: btc +0.243 vs +0.197, eth +0.185
    # vs +0.076, bnb +0.360 vs +0.225.
    #
    # Priced through the full fill model:
    #
    #     btc  floor 0  n=86  growth +0.899  day-t +2.23  CI [+0.212, +1.539]
    #          floor 1  n=83         +0.711        +2.36     [+0.093, +1.264]
    #          floor 5  n=82         +0.705        +2.36     [+0.078, +1.287]
    #     eth  floor 0  n=29         +0.175        +0.87     [-0.390, +0.717]
    #          floor 1  n=27         +0.192        +1.50     [-0.308, +0.737]
    #          floor 5  n=20         +0.087        +1.10     [-0.325, +0.443]
    #
    # 1, not 0: at exactly zero ticks spot sits ON the strike and the side is
    # ambiguous, and btc's extra +0.19 at floor 0 rests on 3 trades. 1 keeps
    # btc where it is and roughly doubles eth's growth.
    #
    # What made this safe to relax is `cross_flip_block_s`, shipped the same
    # day. The floor was doing two jobs -- refusing OSCILLATION (the last
    # decimal flipping back and forth) and refusing SHALLOW-BUT-REAL crossings.
    # The flip guard now does the first job directly, so the floor no longer has
    # to buy it by throwing away the second.
    #
    # Cost of the old value, in the frequency this arm is short of: it refused
    # 42.9% of bnb crossings and 23.3% of eth's, against 3.6% of btc's.
    #
    # ⛔ THIS KNOB IS IN TICKS, SO THE FLOOR IT PRODUCES IS IN BPS OF A
    # SYMBOL-DEPENDENT PRICE. That has now been mis-derived three times: it
    # left xrp/doge inert for their whole life (tick_size table below), and on
    # 2026-09-17 DOGE's 0.00001 tick was read as "0.12 bps" when it is 1.23 --
    # a 10x understatement, because the division is tick/spot and not spot/
    # tick. The floors this setting actually produces:
    #
    #     family  spot      tick        1 tick     2 ticks
    #     btc     76300     0.01        0.001 bps  0.003 bps
    #     eth      2450     0.01        0.04  bps  0.08  bps
    #     bnb       726     0.01        0.14  bps  0.28  bps
    #     sol       100.5   0.01        0.99  bps  1.99  bps
    #     xrp         1.30  0.0001      0.77  bps  1.54  bps
    #     doge        0.0813 0.00001    1.23  bps  2.46  bps
    #
    # THE SUB-BPS FAMILIES ARE NOT ONE CASE, AND THE DIFFERENCE IS THE STRIKE.
    # btc/eth/bnb take the wrapper's exact `variantData.startPrice`, so a
    # 0.001 bps floor is correct there -- they can resolve a crossing to the
    # cent. sol/xrp/doge have no such source (Binance's prediction feed lists
    # btc/eth/bnb only), so their strike is RECONSTRUCTED from the spot tape,
    # and the only accuracy bound that exists for that reconstruction is the
    # 0.042/0.091/0.114 bps mean and **p99 < 1 bp** measured over 5,635
    # btc/eth/bnb rounds (main.py `_pm_strike_backfill`).
    #
    # ONE tick on those three is 0.77-1.23 bps, i.e. the ENTIRE crossing sits
    # inside one error bar of the level it is measured against -- the sign of
    # the crossing, which is what the arm trades, is then not resolvable. So
    # `cross_overrides` sets `min_depth_ticks: 2.0` on exactly those three, and
    # the test pins that. It is a BOUND (the p99 of a measurement), not a value
    # fitted from a sweep, which is why it is the same 2 on all three.
    #
    # Counter-evidence on record, deliberately not buried: the 5.0-tick floor
    # was swept and REJECTED on 2026-09-15 because shallow crossings are
    # CHEAPER than deep ones (edge +0.2311 below the floor vs +0.1771 above).
    # That sweep ran on btc/eth/bnb -- all three with an EXACT strike -- so it
    # priced "shallow", which is a different question from "shallower than the
    # level can be measured". If this is re-measured on sol/xrp/doge, the
    # cheap-ask effect is the thing to beat.
    cross_min_depth_ticks: float = 1.0
    # Venue price increment per symbol, used only by the floor above.
    cross_tick_size: float = 0.01
    # MINIMUM crossing depth in SIGMA of the time remaining. 0 = OFF.
    #
    # The tick floor above is a per-symbol correction and is a no-op on BTC
    # (median trigger 193 ticks). This one is the actual signal-to-noise test:
    #
    #     z = |spot - strike| / strike / (sigma_1m * sqrt(tau_min))
    #
    # A crossing of z ~ 0 is a coin flip at ANY horizon, because what decides
    # the round is the diffusion still to come -- and the arm pays ~0.55-0.70
    # for it, so it is a losing bet by construction.
    #
    # Measured on the 51 recorded cross fills against the venue's TRUE strikes
    # (bars_1s, 09-13..09-15), which is the last measurement this floor needs
    # because it is the first one that signs the gap instead of using abs():
    #
    #     |gap| at entry   n    win     P&L      staked
    #     0 - 0.5 bps     29   51.7%  -10.70      69.86   <- 57% of the stake
    #     0.5 - 1         13   69.2%   +2.14      31.74
    #     1 - 2            8   50.0%   -3.39      18.45
    #     ALL             51   56.9%  -11.37     123.43
    #
    # Bucketed in sigma instead of bps, with the sign aligned to the side taken
    # (an earlier pass bucketed the SIGNED gap, which silently dropped all 20
    # Down trades -- 51 fills became 31, and every number below was Up-only):
    #
    #   z band        n    win     mean_ask   fee      edge/sh    P&L
    #   0 - 0.15     27   51.9%    0.6032   0.0098   -0.0945   -10.96   <- 54% of stake
    #   0.15 - 0.30  13   69.2%    0.6228   0.0099   +0.0596    +3.52
    #   0.30 - 0.50   4   25.0%    0.4978   0.0044   -0.2522    -2.70
    #   >= 0.50       6   83.3%    0.8151   0.0033   +0.0149    -0.67
    #   ALL          51   56.9%    0.6238   0.0088   -0.0640   -11.37
    #
    # THE ASK DOES NOT RESCUE THE SHALLOW BAND, which is the reconciliation this
    # knob was held back for. At z<0.15 the win rate is 50.0% and the mean ask is
    # 0.6013 -- break-even needs 0.50, so the shortfall is -0.1013/sh BEFORE fee.
    # The cheapest fill in the band was 0.2812 and the dearest 0.7562; the mean
    # carries no discount. And there is no sweet spot higher up either: at
    # z>=0.50 the ask is 0.8151 and the edge +0.0149, i.e. deep crossings are
    # priced CORRECTLY. The leak is overpaying for the shallow band, not a
    # shortage of depth.
    #
    # ...AND FOR THAT REASON THIS KNOB DOES NOT FIX IT. The counterfactual is
    # non-monotone, the same failure mode that killed the bps floor:
    #
    #   floor z    kept  /day   P&L      P&L/fill
    #   0 (LIVE)    50  16.67  -10.80   -0.2161
    #   0.15        23   7.67   +0.16   +0.0068     <- the only positive cell
    #   0.20        14   4.67   -2.11   -0.1509
    #   0.25        11   3.67   -5.32   -0.4839
    #   0.30        10   3.33   -3.37   -0.3366
    #   0.40         8   2.67   -1.13   -0.1412
    #
    # 0.40 was recommended from the 1,645-round anchor and is REFUTED here. The
    # two are not measuring the same object: the anchor priced a FIXED tau=19s,
    # while the arm enters across tau=20-60s. Every band above the first holds
    # n=4-13 over 3 days, so no threshold in this range is identifiable yet.
    #
    # Do not enable this on the strength of the mechanism alone. The mechanism is
    # settled (a ~0-gap crossing is a coin flip bought at 0.6); the THRESHOLD is
    # not. If it is enabled, 0.15 is the only value this data supports, and its
    # measured value is +0.0068/fill -- indistinguishable from zero.
    cross_min_depth_sigma: float = 0.0
    cross_reentry_gap_s: float = 1.0
    # ROUND STAKE, KEYED PER FAMILY -- and a WINDOW-level version of this was
    # MEASURED AND REFUTED (2026-09-18). Read the scope before trusting the
    # name: the accumulator is `_round_stake[r.slug]`, so with nine families a
    # 5-minute window holds nine independent budgets. Live 2026-09-18 11:20 took
    # FOUR entries across four families ($4 against a $2 intent) and settled
    # -$2.30; 11:15 took three same-side Downs and settled -$3.12. That is what
    # the knob's own comment describes preventing. It does not.
    #
    # It is also INERT as shipped: `cross_max_per_round = 1` refuses the second
    # entry of a family-round first, so `spent` is always 0 when this is read.
    # Keep it as the backstop for the day max_per_round is raised.
    #
    # THE FIX ONE WOULD REACH FOR -- key it on `rounds.end_ms` so every 5m
    # family in a slot shares one budget -- was replayed over the whole PM era
    # (`tools/pm_window_cap.py`, 763 entries, 299 windows, shipped chain) and it
    # is NEGATIVE:
    #   base (no cap)          n=763 per-share +0.0817 tot +62.37 t +4.57 day-t +3.51
    #   cap 1 per window       n=299             +0.0432     +12.92 t +1.52 day-t +1.53
    #   cap 2 per window       n=504             +0.0593     +29.87 t +2.70 day-t +2.36
    #   cap 3 per window       n=640             +0.0734     +46.98 t +3.77 day-t +3.13
    #   same side <=1/window   n=396             +0.0417     +16.53 t +1.67 day-t +2.02
    # Every dropped cohort is POSITIVE (+0.1066 / +0.1255 / +0.1251 / +0.1249
    # per share) and every cap also LOWERS the day-clustered t. The stacking is
    # not the leak, it is where the edge is: per-entry by entries-in-window k=1
    # +0.1199, k=5 +0.1815 (a third of the total comes from k=5 windows alone),
    # and the same-side stack #2/#4/#5 read +0.1154/+0.2564/+0.2194 against
    # +0.0418 for the first entry of a window. A crossing simultaneous across
    # the complex is ONE real macro move, not the same coin flipped k times.
    #
    # WHAT THE LIVE SAMPLE THAT RAISED THIS SHOWED (for the record, NOT refuted):
    # 64 PM-era fills, per-entry EV by window-k monotone 1:+0.0295 2:+0.0056
    # 3:-0.1112 4:-0.5750, same-side #2 -0.0435 / #3 -0.5347 -- the OPPOSITE
    # sign to the replay. n=64 over three days against n=763; the two worst
    # windows of the sample were 11:15 and 11:20 and both were killed by the
    # move, not by the stacking. Two days of variance is not a signal.
    # RE-CHECK: `/root/analysis-venv/bin/python tools/pm_window_cap.py`.
    cross_max_round_stake_mult: float = 2.0
    # ACCOUNT-WIDE, across every family -- the limit the cross-family case actually
    # needs, and the LIVE PROFILE OVERRIDES IT TO 0 (OFF):
    # `CROSS_MAX_OPEN_STAKE_PCT=0` sits in `/root/BinancePrediction/.env` while
    # this default is 0.30. With the $12.47 collateral in the account, 0.30 caps
    # open cross stake at ~$3.74, i.e. ~3.7 one-dollar entries -- it would have
    # refused the FOURTH entry of the 11:20 window above.
    # Do not confuse it with a concurrency cap: the entry-COUNT caps are refuted
    # (see `cross_max_round_stake_mult`), this one bounds STAKE as a fraction of
    # bankroll.
    #
    # ⭐ 0.30 -> 0.50 on 2026-09-19, and the claim above that it "cannot be
    # priced from the replay (the tape carries no bankroll)" is WITHDRAWN. The
    # tape carries no bankroll, but it carries every round's OPEN AND END, so a
    # bankroll can be imposed on it: replay wall-clock ordered, let a position
    # hold its slot until its own round ends, and drop a signal that arrives
    # with no free slot exactly as the live arm drops it.
    #
    # That constraint is not academic. In 70 minutes live at $6 the cap BLOCKED
    # 16 signals against 9 fills, and the arm never held more than the 2
    # concurrent $1 positions 0.35 allows. Simulated at $6 with $1 stakes,
    # chosen on 09-16..09-17 and scored on 09-18..09-19:
    #     pct   slots  blocked(test)  win%    TEST $  look1.0 / look2.0
    #     0.20    1        506        56.9     +1.80 / -2.46
    #     0.35    2        304        60.4    +32.16 / +19.59   <- was shipped
    #     0.50    3        150        60.1    +36.10 / +37.14
    #     0.70    4         53        60.6    +48.96 / +42.21
    #     1.00    6          0        60.5    +50.00 / +48.29
    # MONOTONE at both looks, on train AND test, and THE WIN RATE IS FLAT at
    # 60-62% throughout: extra slots do not admit worse trades, they stop
    # discarding good ones. This is the only knob swept on 09-19 whose optimum
    # did not flip between look_s 1.0 and 2.0 -- `cross_max_fill_px` picks 0.55
    # at one look and 0.70 at the other, and `cross_min_margin_bps` picks 0.25
    # (test -1.13) at one and 0.75 at the other. Those were left alone.
    #
    # ⛔ WHY 0.50 AND NOT 1.00, WHICH THE TABLE PREFERS. The simulation prices
    # EXPECTED DOLLARS. It does not model RUIN, and it cannot: at 1.00 the arm
    # may hold six concurrent $1 positions on a six-dollar account, so one bad
    # cluster takes most of it, and there is no bankroll left to trade the edge
    # the table is measuring. 0.50 keeps half the account uncommitted and still
    # halves the blocking. The operator chose this level; going further is a
    # risk decision, not a measurement one.
    # Revert: CROSS_MAX_OPEN_STAKE_PCT=0.35 + restart.
    cross_max_open_stake_pct: float = 0.50
    # EXIT-ON-FLIP: SELL the position when the SAME trigger that justified it
    # reads the other side. Operator's idea, 2026-09-18; measured, SHIPPED, and
    # then REFUTED ON THE LIVE RECORD the same day.
    #
    # ⛔ EVERY NUMBER THIS BLOCK USED TO CARRY WAS A MEASUREMENT ARTIFACT.
    # It claimed the flip arrives "at a median of 1.0 s after the entry
    # (p10 = p90 = 1.0 s)" and that exiting took the cohort from +0.0773 to
    # +0.1372 per share. Both came from `tools/pm_exit_flip.py:walk()`, which
    # latched its exit candidate on the FIRST TICK AFTER THE ENTRY -- the
    # `if side != other: if cand is None` branch -- and never advanced it. So
    # the exit was booked at the book ~1 s after the entry, i.e. at roughly the
    # ENTRY PRICE, however long the rule actually took to fire. The p10 = p90 =
    # 1.0 s is the 1 Hz tape's own spacing; it was never a property of the flip.
    # Corrected (`walk_legacy` is kept beside `walk` for the A/B):
    #     legacy   (booked at entry+1s, ungated)  +0.1343/sh t+13.44 [505 exits]
    #        entry -> exit lag: median 1.0s  p10 1.0  p90 1.0    <- the artifact
    #     corrected (priced where it fires, gated) +0.0599/sh t +3.52 [6 exits]
    #        entry -> exit lag: median 99.8s p10 46.6 p90 125.7
    #     hold to settlement (base)                +0.0594/sh t +3.48
    # The honest flip statistics: 52.6% of entries flip (not 62.9%), at a median
    # 17.3 s (p10 3.0, p90 57.4), P(win|flip) 37.0% vs 85.2%.
    #
    # ⛔ AND THE RULE HAD NO HYSTERESIS BAND, which is what cost the money.
    # `_cross_exit_check` ran `crossed_side` with NO `hold_s` and applied NO
    # margin floor, while the ENTRY applies both -- so the buy threshold sat at
    # `level + min_margin_bps` and the sell threshold at `level + 0.00`. Since
    # `crossed_side` fires on the FIRST tick past the level, every entry starts
    # life sitting on it, and any tick back across closed the position.
    #     LIVE, sol-updown-5m-1789743000, consecutive log lines:
    #       14:52:35/36/42/43  not taken: only -0.20 bps of margin, floor 0.25
    #       14:53:20.626       OPEN CROSS sol ... Down @0.540
    #       14:53:25.064       pm sell sol ... Down        <- 4.44 s later
    #     The round refused to OPEN four times on a move it then CLOSED on.
    # `confirm_s` did not help because `flip_ms` was stamped and never read: the
    # gate compared against `entry_ms + 1000`, so "confirm 3 s" meant "wait 4 s
    # after the BUY, then leave on the first opposite tick". Every live exit
    # fired at 4.30-4.44 s, the floor exactly.
    #
    # ⭐ SCORED ON THE 200 REAL FILLS (`tools/pm_exit_live_recheck.py`), which
    # is the measurement that decides this -- the rule applied to the trades the
    # bot ACTUALLY took, not to a cohort the rule re-chooses:
    #     HELD to the bell (counterfactual)   total -11.071  per-fill -0.1244
    #     SHIPPED exit (entry clock, ungated) total -17.743  per-fill -0.1994
    #     FIXED   exit (flip clock, gated)    total -11.138  per-fill -0.1251
    # The shipped rule cost **-$6.67 against doing nothing**, and the mechanism
    # is the operator's own report ("every trade went the wrong way"):
    #     43 exits -- 23 on LOSING positions, saving        +17.380
    #                 20 on WINNING positions, giving up    -24.051
    # It cut winners nearly as often as losers, and a winner cut at the level
    # gives up the whole payout. On 09-18 alone it turns -3.371 into -7.881.
    #
    # WHAT SHIPPED: the hysteresis fix (the exit now runs the entry's `hold_s`
    # and `min_margin_bps`, and `confirm_s` is clocked from the FLIP). That
    # gives back the -6.67. It does NOT make the exit pay: the fixed rule is
    # -11.138 against -11.071 for holding, and every (confirm, floor) cell
    # swept on the live fills is neutral at best, with the positive ones at
    # n=2-5 exits. So this stays FALSE until a cell clears on real fills.
    # Re-check: `/root/analysis-venv/bin/python tools/pm_exit_live_recheck.py`
    cross_exit_enabled: bool = False
    # The flip must PERSIST this long before we leave. 0 exits on the first
    # print. Measured 3/5/10 s are all as good or better than 0 (the effect is
    # not "sell at the bottom of the wobble at t+1"), so this is a whipsaw
    # guard, not a fitted parameter.
    cross_exit_confirm_s: float = 3.0
    # NEVER exit inside this many seconds of the bell: near the end the book
    # goes one-sided ("Up 0 bids / 99 asks") and there is no bid to hit.
    #
    # ⛔ 8.0 -> 60.0 WAS SHIPPED AND THEN RETRACTED THE SAME DAY (2026-09-18).
    # The sweep that chose 60 ran on a BROKEN `walk()`: the fire check sat
    # inside the "this tick is a crossing into the other side" branch, and
    # `crossed_side` is an EVENT detector, so a SUSTAINED flip -- the case the
    # exit exists for -- produced exactly ONE reading and could never fire.
    # Only oscillating signals were being scored, which is why the sweep saw 28
    # exits out of 748 entries and why a tau gate looked like the discriminator.
    # With the fix (arm on the event, confirm on the STATE) the same grid fires
    # 267 times and the tau gate is worth x1.05 on the loser/winner ratio -- a
    # coin flip -- while the live fills put it slightly NEGATIVE (+3.904 with
    # the gate against +4.361 without, over 31 exits). Neither is significant.
    # RESTORED to 8.0 because the value's justification was retracted, not
    # because 60 was disproved: 8.0 carries its own independent reason, above.
    # See config.cross_exit_enabled for the corrected numbers.
    # ⭐ THE EXIT RULE ITSELF, NEW 2026-09-19. >0 leaves on the SIGNAL STATE:
    # sell when the signal sits this many bps on the WRONG side of the level,
    # with no crossing event required. 0 restores the event-armed flip rule.
    #
    # Seven rules on identical entries, slots and sale mechanics
    # (`tools/pm_exit_models.py`), full tape / held-out days:
    #     off           L1 $+64.12 t+2.70 | TEST $+19.17   win 63.1%
    #     flip   3.0    L1 $+60.61 t+3.15 | TEST $+27.05   win 43.9%
    #     margin 0.5    L1 $+82.98 t+4.70 | TEST $+38.05   win 44.7%
    #     margin 4.0    L1 $+85.04 t+3.97 | TEST $+39.46   win 57.1%
    # take/bracket/trail are worse; `take` hits 87% win rate and makes nothing.
    #
    # ⛔ 4.0 SHIPS THOUGH THE SEARCH RANKED 0.5 FIRST ON TRAIN, and the reason
    # is execution, not score. 4.0 fires ~165 times to 0.5's ~322, so it is half
    # as exposed to a sale that does not land -- and a miss is a RETRY, not an
    # abandoned exit. Beating holding by completion rate:
    #     rate       1.00   0.88   0.70   0.50   0.39
    #     4.0 TEST  +45.39 +43.91 +43.33 +42.75 +40.85   all beat hold
    #     0.5 TEST  +34.79 +34.08 +33.95 +29.99 +25.36   fails under 0.70
    # (hold is +27.00.) 0.88 is the live completion rate excluding the
    # UNVERIFIED bug -- 7 of 8 post-fix attempts -- and 0.39 is the all-time
    # rate including it. It also keeps 55.6% win rate against 0.5's 41.6%.
    # Revert: CROSS_EXIT_MARGIN_BPS=0 (back to the flip rule), or
    # CROSS_EXIT_ENABLED=false (back to holding) + restart.
    cross_exit_margin_bps: float = 4.0
    # ⭐ A MINIMUM HOLD FROM THE FILL, NEW 2026-09-19. 0 = off.
    #
    # The exit versus HOLDING the same positions, by how long after the fill it
    # fired (`tools/pm_exit_models.py`):
    #     0-5s     n=  7  exit  -2.16  hold  +0.22  delta  -2.38  <- only loser
    #     5-10s    n=  9  exit  -4.48  hold  -5.58  delta  +1.10
    #     10-30s   n= 57  exit -28.35  hold -39.17  delta +10.82
    #     30-60s   n= 91  exit -52.63  hold -68.51  delta +15.88
    # A guard gains +2.38/+1.24 at 5 s and +1.23/+1.23 at 2 s across the two
    # looks, and TURNS HARMFUL at 12 s (-1.70/-1.92) and 20 s (-8.07/-11.39).
    #
    # WHY IT IS MEASURED FROM THE FILL AND NOT THE DECISION. Live, the two are
    # a median 1.03 s apart (p90 1.79 s, n=30) because a missed FAK re-prices
    # and retries, so a position is born on a signal already a second old. The
    # replay measures lag from the SIGNAL and therefore understates how early a
    # live exit can look.
    #
    # ⛔ SHIPPED ON n=7, BELOW THIS REPO'S USUAL BAR. The case is asymmetry, not
    # significance: it can only DELAY an exit, it touches ~2.4% of them, and
    # both looks agree. Declined once on sample size, then seen live --
    # xrp-updown-5m-1789804800 filled 08:02:23.334 at 0.49 after the day's worst
    # 3.57 s decision-to-fill gap, sold 08:02:25.353 at 0.31: a 2.02 s hold for
    # -$0.4336.
    # Revert: CROSS_EXIT_MIN_HOLD_S=0 + restart.
    cross_exit_min_hold_s: float = 5.0
    # ⭐ 8.0 -> 60.0 on 2026-09-19 -- THE SAME NUMBER RETRACTED EARLIER THE SAME
    # DAY, restored on different evidence. The first 60.0 rested on a sweep run
    # over `pm_exit_flip.py:walk()`, which latched the exit candidate on the
    # first tick after the entry and so priced every exit at roughly the ENTRY
    # price; that evidence is dead. This one comes from the joint entry-x-exit
    # search under the full gate chain (`tools/pm_joint_exit.py`): the winning
    # cell is margin 4.0 at xmin 60, TEST $+45.39/$+42.14 against holding's
    # $+27.00/$+22.19, and it still beats holding at a sale completion rate of
    # 0.39. 8.0's own reason -- the book goes one-sided at the bell -- is not
    # contradicted, only exceeded. Revert: CROSS_EXIT_MIN_TAU_S=8 + restart.
    cross_exit_min_tau_s: float = 60.0
    # The sell's price floor: `min_price` on the FAK, set this far BELOW the bid
    # we can see. It is the only risk bound on the order -- the venue will not
    # take less -- so it is also what the ledger books when the venue's own fill
    # price cannot be isolated (the mirror of the entry convention, where
    # `filled_px` is the CEILING and the stake is an upper bound: here the floor
    # makes the proceeds a LOWER bound and the P&L conservative both ways).
    cross_exit_slip: float = 0.03
    # One exit per position, and only in the live book. Both are sanity rails
    # against a watcher that fires twice on the same fill.
    cross_exit_max_per_position: int = 1
    cross_ws_follow_all: bool = True
    cross_overrides: dict[str, dict[str, float]] = {
        # THE FIVE `max_round_range_bps` VALUES ARE GONE -- this table used to
        # carry 12.9 / 7.8 / 13.6 / 21.0 / 21.1 for eth-5m / bnb-5m / btc-15m /
        # eth-15m / bnb-15m. They are read through `cross_knob`, so leaving them
        # here would have kept the refuted gate binding on exactly the five
        # families that fire most while the global read as OFF. See
        # `cross_max_round_range_bps` for the replay that removed them and for
        # the revert line (the numbers are named there).
        # `tick_size` here is the SPOT price tick of the UNDERLYING, NOT the
        # 0.01 probability tick on the CLOB. The depth floor is
        # `min_depth_ticks * tick_size` and it is compared against a SPOT
        # difference, while the ceiling (`max_depth_bps`) is in bps of spot.
        #
        # MEASURED, 2026-09-16: a single global 0.01 made that floor a
        # per-symbol error of the order of the symbol's price. In bps:
        #     btc  76000 ->    0.0013   (floor effectively off, as intended)
        #     eth   2500 ->    0.04
        #     bnb    726 ->    0.14     (this is the case the comment above
        #                                cross_min_depth_ticks was written for)
        #     sol    200 ->    0.50
        #     xrp   1.28 ->   78.0     <- ABOVE the 5.0 ceiling
        #     doge  0.08 -> 1250.0     <- ABOVE the 5.0 ceiling
        # A floor above the ceiling is an EMPTY band, so the arm refused every
        # crossing those two families ever produced -- inert, and silently so,
        # because a refusal is not an error. Neither family had ever traded.
        # ⛔ TWO TICKS, NOT ONE, ON THE THREE FAMILIES WHOSE STRIKE IS
        # RECONSTRUCTED. See `cross_min_depth_ticks` for the derivation: one
        # tick here is 0.99 / 0.77 / 1.23 bps of spot against a strike that is
        # only known to p99 < 1 bp, so a one-tick crossing is inside its own
        # error bar. Two ticks is the smallest floor that clears it.
        #
        # This is a BOUND and is deliberately identical on all three rather
        # than swept per family -- three identical cells fitted separately
        # would read as a choice, the same trap `cross_limit_buffer` fell into
        # (0.25/0.35/0.50 all measuring one cell because the min() always took
        # the flat branch).
        #
        # Risk: this refuses the shallowest crossings on these families, and
        # the 2026-09-15 sweep found shallow crossings have the better EDGE
        # because they are cheaper. That sweep did not cover these three. If
        # the arm's fires get too rare, revert to 1.0 here -- nothing else
        # depends on the value.
        "sol-updown-5m": {"min_depth_ticks": 2.0},
        "xrp-updown-5m": {"tick_size": 0.0001, "min_depth_ticks": 2.0},
        "doge-updown-5m": {"tick_size": 0.00001, "min_depth_ticks": 2.0},
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
    # ---- CUT 5 -> 1 on 2026-09-15 ---------------------------------------
    # This knob has never been measured on a substrate that can produce a
    # second entry. The replay is sampled at 1s and `cross_reentry_gap_s` is
    # 1.0s, so consecutive entries are all but impossible in it: across the
    # whole corpus btc-5m yields ONE second entry and eth-5m TWO. Setting this
    # to 1 or to 5 moves btc by 82 -> 83 fills and eth by 25 -> 27. The +0.022
    # growth that appears to favour 5 is those two trades.
    #
    # Live is a different clock -- the websocket runs ~114/s -- so repeats
    # happen constantly there, and that is the only place they can be judged:
    #
    #     FIRST entry in a round   n=31  won 21/31  staked $79.62  ev/$1 -0.042
    #     LATER entries            n= 7  won  3/7   staked $20.83  ev/$1 -0.316
    #
    # n=7 and contaminated (five came from the sub-second duplicate bug fixed
    # the same day, one was a reversal `cross_allow_reversal` now blocks), so it
    # is not proof. But there is no evidence on the other side either, and the
    # mechanism is adverse by construction: a repeat one second later buys the
    # SAME bet after the venue has repriced it. 2026-09-15 01:29 on
    # bnb-updown-15m, the two entries the operator flagged:
    #
    #     01:29:27.666  Up  decided 0.6934  filled 0.6993  $3.21
    #     01:29:28.735  Up  decided 0.7320  filled 0.7356  $3.20
    #
    # 3.6 cents worse for an identical position, and $6.41 of a ~$40 bankroll on
    # one coin flip.
    #
    # This costs no ROUNDS -- the arm still takes every round it would have. It
    # only stops doubling inside one. Revert to 5 if repeats ever measure
    # positive on live fills.
    cross_max_per_round: int = 1
    # FAMILIES THE CROSS ARM MAY NOT ORDER IN -- comma-separated prefixes, the
    # same keys `cross_overrides` uses. A disabled family keeps its ROUNDS, its
    # QUOTE TAPE and its websocket book: it is exactly the state the three
    # never-traded families have always been in (MARKETS widens the tape,
    # this narrows the risk). Nothing is lost, so re-enabling is one edit and
    # the counterfactual keeps accruing while it is off.
    #
    # eth-updown-5m WAS HERE FOR PART OF 2026-09-18 and was taken off the same
    # day. The block below is kept because the REASON it was put there is the
    # reason it came back, and both halves of that reason were a measurement.
    #
    # WHY eth-updown-5m WENT ON (2026-09-18, morning). It was the ONE family
    # whose trigger has no edge against the price it is charged. Measured over
    # every settled PM-era round, first crossing in tau [5,60], NO band and NO
    # floor (`tools/pm_family_signal_check.py`, so n is per-family triggers, not
    # the 10-13 entries the band lets through):
    #     family   n   trigger hit   be-ask   edge
    #     bnb-5m  28      82.1%       0.602   +21.9pp
    #     btc-5m  37      78.4%       0.643   +14.1pp
    #     sol-5m  25      76.0%       0.631   +12.9pp
    #     xrp-5m  32      71.9%       0.607   +11.2pp
    #     doge-5m 27      66.7%       0.609    +5.8pp
    #     eth-5m  38      63.2%       0.645    -1.4pp   <- this one
    # Every other family's trigger is worth 6-22 pp more than the book charges;
    # eth's is worth LESS than its price, i.e. every entry pays the taker fee
    # (1.1-2.5c per $1 at 0.45-0.65) for a coin flip. Its leak is visible inside
    # that: the 1-2 bps crossings are bought at a MEAN ASK of 0.788 for a 57%
    # shot (n=7, -$1.52, more than eth's whole -$0.94 over 38 triggers).
    # eth-updown-15m was never disabled -- it is +9.7pp (n=5, tau 45-60 only).
    #
    # WHAT THAT DELIBERATELY OVERRODE: by the repo's own rule a family is only
    # cut when its edge is negative with the SAME sign in BOTH halves. eth's
    # halves were -6.6/+3.9, i.e. it FAILS that test, and its money sample is
    # 13-16 entries. So that was the OPERATOR's risk decision, taken because a
    # zero-edge family still pays a fee on every entry, and recorded as such
    # rather than dressed up as a measurement.
    #
    # WHY IT CAME BACK OFF (2026-09-18, later the same day). BOTH inputs of the
    # table above changed underneath it:
    #
    #   1. THE FORECAST WIRE WAS WRONG WHEN THE TABLE WAS TAKEN. twap_forecast
    #      averaged [ts-60, ts] instead of the paid window's printed part
    #      [bell-60, ts] (fixed: see `cross_fc_trigger`). Re-run with the
    #      corrected wire at the SAME tau [5,60], eth reads n=40 hit 60.0%
    #      be-ask 0.647 edge -4.7pp -- WORSE, so the fix did not rescue eth.
    #      What moved is the window below.
    #   2. THE ARM STOPPED TRADING tau [5,60]. The window was extended to
    #      [5,180] and re-validated the same day (`cross_max_tau_s`), so the
    #      table above now measures a slice the arm only partly trades. In the
    #      window it DOES trade, eth is positive:
    #          eth-5m n=170 hit 64.7% be-ask 0.546 edge +10.1pp
    #                 per-share +0.0941  t +2.46  halves +8.9/+11.3
    #          btc-5m +9.2pp | xrp-5m +8.8pp | bnb-5m +8.2pp | sol-5m +4.1pp
    #      and it REPEATS: by day +21.8/+7.0/+12.3pp, by side Up +12.2pp /
    #      Down +8.5pp. Arithmetic rather than a bucket: 40 triggers at -0.0525
    #      = -$2.10, 170 at +0.0941 ~ +$16.0, so the 130 EARLIER
    #      first-crossings pay ~ +0.139/share.
    #
    # The money replay agrees and makes eth the LARGEST 5m family at the 0.5 bps
    # floor (`pm_cross_replay.py --by-family --tau-lo 5 --tau-hi 180`):
    #     eth n=113 ask=0.520 win=70.8% total=+$19.33 t=+3.89 day-t=+2.88
    #         halves=+0.2070/+0.1358
    #     then btc +13.49, sol +11.12, bnb +10.60, xrp +10.28, doge +6.50
    # Eth is also the family the floor helps MOST, which is what defuses the
    # original "fee on a coin flip" objection: |m| 0-1bps is +5.3pp (n=118,
    # t +1.05) while 1-2bps is +23.1pp (n=27) and 3+bps is +23.7pp. The coin
    # flip lives in the LATE, LOW-MARGIN crossings -- exactly the ones a 1.0 bps
    # floor already removes -- so what is left is not the thing that was cut.
    # And the override rationale is gone: in the traded window BOTH halves are
    # positive, so the repo's own rule says keep.
    #
    # THE VENUE-ASK RISK, MEASURED RATHER THAN UNKNOWN. The replay above pays
    # the RECORDED ask, so the question is what eth's fills actually pay. eth-5m
    # has ZERO fills in the PM era (and zero under this rule chain), but it has
    # 43 real fills from before it (09-14 19:24 -> 09-16 15:06), at 0.42-0.89
    # under the old cross arm, and their `filled_px - requested_px` is:
    #     mean -0.0054  median +0.0005  max +0.0265  n=43
    #     worse than +0.02: 1 fill | better than -0.02: 5 fills
    # So a fill lands AT or BELOW the price asked for, on average. The xrp case
    # that showed a venue ask 0.1600 ABOVE our ladder is the tail, not the
    # shape: one such print in 43 here. Caveat that remains: those fills are a
    # different era, band (0.42-0.89 vs our 0.10-0.65) and execution path, so
    # they bound the risk rather than remove it.
    # KILL CRITERION: eth-5m live fills at <= 0 per share over ~50 entries ->
    # put `eth-updown-5m` back into this string + restart.
    # REVERT: `cross_disabled_families=""` (or drop the prefix) + restart.
    # Re-check with:
    #   /root/analysis-venv/bin/python tools/pm_family_signal_check.py \
    #       --tau-lo 5 --tau-hi 180
    #   /root/analysis-venv/bin/python tools/pm_cross_replay.py --by-family \
    #       --tau-lo 5 --tau-hi 180
    # ⭐ NARROWED 2026-09-19 TO THE THREE DEEPEST SAMPLES, and this is a RISK
    # decision taken by the operator on a small account, not an edge one.
    #
    # Per family on the tape under the shipped chain, held to settlement:
    #     eth-updown-5m  n=240 win 70% +49.65  +0.2069/fill  t +3.84
    #     bnb-updown-5m  n=220 win 62%  +9.87  +0.0449/fill  t +0.78
    #     btc-updown-5m  n=175 win 67% +25.49  +0.1456/fill  t +2.31
    #     ---- kept above, dropped below ----
    #     xrp-updown-5m  n=141 win 61%  +2.08  bnb-15m n=33 +10.23
    #     sol-updown-5m  n= 89 win 63%  +2.73  btc-15m n=34  +6.89
    #     doge-updown-5m n= 67 win 60%  -0.46  eth-15m n=25  -3.58
    # The three kept are the three with the MOST evidence (240/220/175); every
    # family dropped has 141 entries or fewer and eth-15m has 25. This is
    # declining to bet where the sample cannot see, NOT selecting on return --
    # bnb-5m is kept at t +0.78 while bnb-15m (+10.23, t +2.46) is dropped,
    # because n=33 is not a measurement.
    #
    # WHY IT IS SAFE EVEN IF THE RANKING IS WRONG, which is what separates it
    # from the four knobs refused the same day: tuning a threshold only helps if
    # the tuning is right, whereas trading fewer and better-sampled markets cuts
    # variance either way. Simulated from $6.36 at the $1 venue minimum:
    #     everything   n=1022 win 60% end +140.03  maxDD 12.13  LOW 4.11  streak 8
    #     these three  n= 635 win 61% end  +99.82  maxDD  9.50  LOW 6.36  streak 6
    # It gives up ~29% of return to never dip below the starting balance, and it
    # stops the exposure cap binding at all -- which removes the spiral seen at
    # cap 0.35 on the full set, where 63 trades took equity to $0.96 and the
    # falling equity then tightened the cap further.
    #
    # ⛔ WHAT THIS DOES NOT FIX. `min_order_usdt` is $1.00, so stake size is not
    # a lever, and max drawdown is a property of the STRATEGY -- identical at
    # every starting balance ($9.50 here, $12.13 on the full set). A 6-loss
    # streak is -$6 against $6.36: survivable in the replay only because equity
    # grew first. The dominant risk is the bankroll, not the family list.
    # ⛔ REVERTED TO EMPTY 2026-09-19, BY OPERATOR CHOICE, ON THROUGHPUT. The
    # narrowing above held for roughly an hour and was reverted because three
    # families is too slow to trade -- 635 entries against 1022 over the same
    # four days. THE MEASUREMENT IS NOT WITHDRAWN, only the decision it fed:
    #     everything    n=1022 win 60% end +140.03  maxDD 12.13  LOW 4.11  streak 8
    #     three deepest n= 635 win 61% end  +99.82  maxDD  9.50  LOW 6.36  streak 6
    # So the full set is ~61% more trades and ~40% more return, bought with a
    # drawdown that takes the account BELOW its starting balance (4.11 from
    # 6.36) and a worst losing streak of 8 rather than 6. At the $1 venue
    # minimum an 8-loss streak is -$8.
    # ⛔ AND THE CAP MATTERS MORE ON THE FULL SET. With nine families the
    # exposure cap binds hard: at 0.35 the replay takes 63 trades and falls to
    # $0.96, the falling equity tightening the cap that caused it. At 0.50 it is
    # fine. Do not lower `cross_max_open_stake_pct` while this string is empty.
    # RE-NARROW: cross_disabled_families="xrp-updown-5m,sol-updown-5m,
    # doge-updown-5m,eth-updown-15m,btc-updown-15m,bnb-updown-15m" + restart.
    cross_disabled_families: str = ""
    #: Families the MODEL arm may trade. Empty = every family in
    #: `active_markets()`, which is the behaviour from 2026-09-19 when the arm
    #: was widened past `MARKETS[0]` at the operator's request.
    #:
    #: WHY IT EXISTS. Re-measured the same day with every decision knob pinned
    #: to the live host (min_quote 0.55, max_quote 0.80, min_edge 0.01,
    #: edge_haircut 1.0, tau 60-210, per-symbol sigma, measured taker fee), the
    #: widening was trading two families that lose:
    #:
    #:     family          fills     WR     EV/$   day-t     tot$
    #:     btc-updown-5m   1,467  72.5%  +0.0300   +1.55  +$44.01
    #:     eth-updown-5m     528  69.5%  -0.0038   +0.05   -$2.01
    #:     bnb-updown-5m     142  73.2%  -0.0093   +0.36   -$1.32
    #:
    #: ETH also fails its own calibration independently of P&L: scaling
    #: realised moves by the predicted sd gives std(z) = 1.525 over 354,939
    #: observations, against BTC 0.999 and BNB 1.017. It is not a feed mismatch
    #: (median |end_price - last spot| is 0.00 bps for both) -- ETH trends
    #: within a round faster than sqrt-time implies, so the model is
    #: structurally overconfident there. Two independent lines of evidence.
    #:
    #: BNB is n=142 over 3 days and is NOT established either way; it is off
    #: because it is unmeasured, not because it is proven bad.
    #:
    #: This narrows TRADING only. Every family in `active_markets()` is still
    #: recorded and still has its book followed, so re-enabling one costs
    #: nothing but the edit -- the same separation `CROSS_TRADED` keeps from
    #: `MARKETS`, and for the same reason.
    model_families: str = ""
    #: Skip this many of the MODEL arm's next signals after one of its trades
    #: settles a LOSS. 0 = off, which is the shipped default.
    #:
    #: Measured 2026-09-19 on btc-updown-5m, 886 signals over 9 days, every
    #: other knob pinned to the live host, simulated SEQUENTIALLY (a skipped
    #: signal never happens, so it has no outcome and cannot advance the
    #: chain -- the earlier filter test got that wrong and used information a
    #: live bot would not have):
    #:
    #:     cooldown  traded     WR     EV/$   day-t     tot$
    #:        0 (today)  886  78.1%  +0.0531   +5.54   +47.05
    #:        1          736  79.6%  +0.0747   +3.88   +54.94   <- ships as 1
    #:        3          553  79.9%  +0.0840   +3.49   +46.46
    #:        5          466  82.0%  +0.1099   +4.19   +51.23
    #:
    #: Against a calibrated null (800 draws, outcome ~ Bernoulli(entry-implied
    #: p), cooldown and baseline BOTH simulated), the EV/$ advantage of
    #: cooldown 1 is +0.0215 against a null mean of -0.0000, p = 0.019.
    #:
    #: ⛔ DO NOT read a p-value off the TOTAL-$ column. In a null every trade
    #: has EV = -fee, so trading less mechanically improves the total there and
    #: the null's own total advantage is +3.79. That confound does not apply to
    #: real positive-EV data, which makes the total-$ p meaningless in either
    #: direction. The EV/$ null is the clean one.
    #:
    #: Temporal holdout, fit on days 1-5 and scored on 6-9:
    #:     cooldown 0: train +0.0497 tot +23.44 | test +0.0570 tot +23.62
    #:     cooldown 1: train +0.0755 tot +29.61 | test +0.0736 tot +25.33
    #:     cooldown 5: train +0.1214 tot +31.08 | test +0.0960 tot +20.15
    #:
    #: 1 is the only setting that beats no-rule on BOTH EV/$ and total dollars
    #: out of sample. Longer cooldowns look better per dollar and OVER-FILTER:
    #: at 5 the holdout total falls to $20.15 against no-rule's $23.62, which
    #: is the same trap every other filter tried on this arm fell into (higher
    #: yield, less money -- see the exits, the momentum split and max_fair,
    #: all rejected the same day).
    #:
    #: Mechanism: volatility regimes cluster, so a round the model got wrong is
    #: followed by conditions that make it wrong again. Not proven -- this is a
    #: rule found by search, carried mainly by the holdout.
    #:
    #: Counted per ROUND, not per poll: a round is evaluated many times a
    #: second and consuming the cooldown on each would burn it inside one
    #: round. Seeded from the ledger at startup so a restart cannot launder it.
    model_cooldown_after_loss: int = 0
    #: Stake MULTIPLIER for model-arm entries whose `fair` for the side bought
    #: sits at or below `model_low_fair_cut`. 1.0 = off (the shipped default).
    #:
    #: NOT a filter -- every trade is still taken. It reallocates a FIXED
    #: capital budget toward the bucket that earns more per dollar. That
    #: distinction is why this survives where `max_fair` did not: capping at a
    #: flat stake means fewer trades and less total money, while reweighting at
    #: constant deployment means the same money spent better.
    #:
    #: Measured 2026-09-19, btc-updown-5m, 886 entries, every other knob pinned
    #: to the live host:
    #:     fair <= 0.684 (bottom third)  n=295  EV/$ +0.1006
    #:     fair >  0.684                 n=591  EV/$ +0.0294
    #: The cut ships as a round 0.70 (n=332, EV/$ +0.0962) rather than the
    #: fitted 0.6844, because a quantile to four decimals is a fitted constant
    #: and 0.70 measures the same thing.
    #:
    #: Weights normalised to the same total capital, fit on days 1-5, scored on
    #: days 6-9:
    #:     flat      train +23.44   test +23.62
    #:     1.5x      train +25.82   test +26.84
    #:     2.0x      train +27.61   test +29.29   <- ships as 2.0
    #:     3.0x      train +30.12   test +32.78
    #:
    #: ⛔ IT NEVER TURNS. "low-fair only" is the in-sample optimum (test
    #: +47.49), so this is a concentration rule, not an optimum -- the ceiling
    #: is set by risk, not by the data. It is also NOT significant: against a
    #: calibrated null the 2x advantage is +5.67 with a null mean of -0.50 and
    #: sd 4.83, p = 0.10. Shipped at the operator's explicit instruction with
    #: that stated; 2.0 rather than 3.0 because at an $8 balance with the risk
    #: guards off, 3x is $3/trade and four losses ends the account.
    #:
    #: `stake_cap_usdt` still binds LAST and will clamp this, so the cap must
    #: be raised with the multiplier or the reweight silently does nothing.
    model_low_fair_cut: float = 0.0
    model_low_fair_stake_mult: float = 1.0
    #: BANDS of `fair` that get `model_stake_boost_mult` times the base stake,
    #: as "lo-hi,lo-hi" (e.g. "0.50-0.60,0.70-0.80"). Empty = off. When set it
    #: SUPERSEDES `model_low_fair_cut`, which is a single cut and cannot
    #: express a gap.
    #:
    #: ⛔ THE GAP IS THE POINT. A single cut at 0.70 -- shipped 2026-09-19 and
    #: reverted the same night -- spends the multiplier on a DEAD ZONE. The
    #: model arm's `fair` bands on btc-updown-5m, 649 entries over 11.65 days,
    #: PM-era excluded, live recorded sigma, replayed through the live gate
    #: chain and the limit_take fill path (a limit that does not cross is a
    #: MISSED round, not a trade):
    #:
    #:     band         n     WR    EV/$1   day-t   H1      H2      tot$
    #:     0.50-0.60   27   77.8%  +0.3468  +4.30  +0.358  +0.337   +9.36
    #:     0.60-0.65   90   57.8%  -0.0312  -0.01  -0.150  +0.048   -2.81
    #:     0.65-0.70   72   66.7%  +0.0419  +0.00  +0.072  +0.008   +3.02
    #:     0.70-0.75   97   76.3%  +0.1100  +0.69  +0.099  +0.118  +10.67
    #:     0.75-0.80  129   78.3%  +0.0754  +2.34  +0.055  +0.092   +9.73
    #:     0.80-0.85  143   74.1%  -0.0371  -1.08  +0.035  -0.108   -5.30
    #:     0.85-0.90   72   81.9%  +0.0449  -0.35  +0.009  +0.081   +3.23
    #:     0.90+       18   72.2%  -0.0750  -0.57  -0.182  +0.010   -1.35
    #:
    #: 0.60-0.70 contributes +$0.21 across 162 trades. A cut at 0.70 boosts it.
    #:
    #: At CONSTANT total capital (weights normalised to mean 1, so this is
    #: allocation and not leverage), fit on the first half, scored on the
    #: second:
    #:
    #:     candidate                  train tot$   test tot$   test vs flat
    #:     flat                          +10.46      +16.59          +0.00
    #:     2x <=0.70 (the reverted one)   +9.64      +19.06          +2.47
    #:     2x 0.50-0.60                  +14.48      +20.48          +3.89
    #:     2x 0.70-0.80                  +13.29      +21.82          +5.23
    #:     2x 0.50-0.60 + 0.70-0.80      +16.26      +24.54          +7.95  <- ships
    #:     2x <=0.80                     +12.13      +22.81          +6.22
    #:
    #: Against a calibrated null at constant capital (outcome ~ Bernoulli(price
    #: paid), 3000 draws) the split band is the ONLY candidate under p<0.05:
    #:     0.50-0.60             real +3.89  null -0.10 sd 2.94  p = 0.091
    #:     0.70-0.80             real +5.23  null -0.02 sd 4.37  p = 0.118
    #:     0.50-0.60 + 0.70-0.80 real +7.95  null +0.04 sd 4.38  p = 0.036
    #:
    #: ⛔ STATED PLAINLY: p=0.036 is ONE candidate of six examined, so a crude
    #: multiplicity adjustment puts it past 0.2. What carries it is the
    #: holdout -- train and test both improve -- and that the bands were
    #: identified before this test rather than fitted by it. 0.50-0.60 rests on
    #: 14 test trades. Shipped at the operator's explicit instruction after
    #: that was said; the recommendation was to fund the account first.
    #:
    #: `stake_cap_usdt` still binds LAST and will clamp the boost, so raise it
    #: with the multiplier or the reweight silently does nothing.
    model_stake_boost_bands: str = ""
    model_stake_boost_mult: float = 1.0
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


def cross_disabled() -> frozenset[str]:
    """The families named in `cross_disabled_families`, parsed once.

    Parsed rather than stored as a set so the knob stays a plain .env string:
    pydantic would want JSON for a set and a typo would then fail at import
    instead of at the arm.
    """
    raw = settings.cross_disabled_families or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def stake_boost_bands() -> tuple[tuple[float, float], ...]:
    """Parsed `model_stake_boost_bands`. Malformed entries are DROPPED, never
    guessed: a typo must cost the boost, not silently widen it onto a band the
    measurement never covered."""
    out = []
    for part in str(getattr(settings, "model_stake_boost_bands", "") or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        lo, _, hi = part.partition("-")
        try:
            a2, b2 = float(lo), float(hi)
        except ValueError:
            continue
        if 0.0 < a2 < b2 <= 1.0:
            out.append((a2, b2))
    return tuple(out)


def stake_boosted(fair: float | None) -> bool:
    """Is this `fair` inside a boost band? Half-open [lo, hi)."""
    if fair is None:
        return False
    try:
        f = float(fair)
    except (TypeError, ValueError):
        return False
    return any(lo <= f < hi for lo, hi in stake_boost_bands())


def model_families() -> frozenset[str]:
    """Families the MODEL arm may order in; empty means no restriction."""
    raw = settings.model_families or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def model_traded(prefix: str | None) -> bool:
    """May the model arm ORDER in this family?

    An empty `model_families` means every active family, so the widened
    behaviour is what you get by default and narrowing is the explicit act.
    """
    allowed = model_families()
    if not allowed:
        return True
    return bool(prefix) and prefix in allowed


def cross_traded(prefix: str | None) -> bool:
    """May the cross arm ORDER in this family, or is it recorded only?

    This is the single place `MARKETS` (the tape) is narrowed to the families
    that may risk money. `_record` passes it as `trade=`; a family that fails
    it still records its rounds and quotes and still has its book followed, it
    just never builds the per-market context the arm fires from.
    """
    if not prefix or prefix not in CROSS_TRADED:
        return False
    return prefix not in cross_disabled()


def market_prefix(slug: str | None) -> str | None:
    """"btc-updown-5m-1788872400" -> "btc-updown-5m"."""
    if not slug:
        return None
    head, _, tail = slug.rpartition("-")
    return head if head and tail.isdigit() else slug


# ⛔ sol/xrp/doge up-down markets are POLYMARKET-ONLY. They were added to
# MARKETS on 2026-09-16 off `pm_train_sweep` (the PM tail-60 rule) -- the same
# day the venue flipped to Polymarket -- and Binance does not list them at all.
#
# Leaving them in the Binance list is not cosmetic. `rounds_by_prefixes` pages
# the venue's ~1,600-topic market list and stops only once EVERY requested
# prefix is found, so three prefixes that can never be found force the full
# sweep every time and log
#     round refresh: no round for sol-updown-5m, xrp-updown-5m, doge-updown-5m
# once per poll. That is the same coupling the RECORDED_REFRESH_S block above
# was written to kill: an unfindable family stalls the decision loop.
#
# Filtered here rather than at line 62 because the venue is only known once
# `settings` has merged `.env`. `main.py` imports these names after this module
# has finished executing, so it sees the narrowed list.
PM_ONLY_FAMILIES = ("sol-updown-5m", "xrp-updown-5m", "doge-updown-5m")


def active_markets() -> list[tuple[str, str, int]]:
    """`MARKETS` narrowed to the families that EXIST on the live venue.

    A FUNCTION, not a filtered constant: the venue is a setting, and the test
    profile flips it after this module has imported. Baking the narrowing in at
    import time makes `MARKETS` disagree with `settings.venue` for the rest of
    the process, which is exactly the stale-default trap `registry.normalise`
    exists to prevent.
    """
    if (settings.venue or "binance").strip().lower() == "binance":
        return [m for m in MARKETS if m[0] not in PM_ONLY_FAMILIES]
    return list(MARKETS)


def active_symbol_by_prefix() -> dict[str, str]:
    return {p: s for p, s, _ in active_markets()}
