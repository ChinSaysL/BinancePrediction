"""Orchestrator.

Modes:
  record  feeds + storage only, no signals, no orders. This is Phase 0 and is
          the default -- it is what answers the open questions about effective
          spread and fee semantics.
  paper   full signal path against the simulated broker.
  live    same path against the real venue. Refuses to start unless the live
          endpoints have actually been configured.

The loop does two independent jobs each tick: record the current round, and
settle any positions whose rounds have closed. Settlement is driven off stored
fills rather than in-memory state so a restart cannot orphan a position.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import signal
import time

import aiohttp

from btcpredict.calibration.report import PnLSummary
from btcpredict.edge.impulse import impulse_bps, impulse_side
from btcpredict.config import (CROSS_TRADED, MARKETS, cross_knob,
                               market_prefix, settings)
from btcpredict.edge.signal import (FeeModel, Signal, evaluate,
                                    evaluate_hybrid, expected_value,
                                    fee_rate_for_price, impulse_price_refusal,
                                    kelly_stake, take_limit_px,
                                    stake_for_edge, stake_for_fraction,
                                    tilt_factor)
from btcpredict.execution.base import Fill
from btcpredict.execution.live import RETRYABLE_FAILURES, LiveBroker
from btcpredict.execution.paper import PaperBroker
from btcpredict.feeds.binance_spot import SpotFeed, fetch_klines
from btcpredict.feeds.prediction_book import PredictionBook
from btcpredict.feeds.market_feed import MarketFeed, Round
from btcpredict.models import PriceHistory, build_model
from btcpredict.pricing.digital import fair_value, implied_sigma, moneyness_sd
from btcpredict.pricing.vol import VolEstimator
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

log = logging.getLogger("btcpredict")

# How many consecutive venue reads must disagree with the bankroll ledger before
# the venue's figure is believed over our own accounting. One or two is the
# redemption lag (a stake leaves the wallet immediately, the payout lands later);
# a run this long is a deposit, a withdrawal, or fees the ledger does not model.
_BANKROLL_DRIFT_READS = 5

PROBE_MODE = "probe"

# How often the winnings-claim sweep runs (see Bot.sweep_claims): the retry
# clock for claims the venue rejected as not-yet-final. The on-win redeem
# stays immediate; this only has to catch what that one missed.
_CLAIM_SWEEP_S = 180.0

# A MARKET attempt needs the round open only long enough to place and execute
# (~1-2s); its order/history verdict may land after the bell. `order_min_tau_s`
# was written for what a RETRY spans with the 9s status budget, which is the
# wrong requirement for an immediate FOK -- and the venue's book demonstrably
# processes fills into the close (live fills are booked with 0.0-0.6s of round
# left). For market orders the retry floor is this instead; see _place_order.
_MARKET_RETRY_TAU_FLOOR_S = 3.0

# Quote warm-up for the cross arm (see Bot._prefetch_cross_quote): refresh at
# most this often while a family is in its tradeable window, and consume the
# warm quote only if younger than the max age. The venue's quote TTL is ~5s,
# so 2.5s keeps a wide margin.
_QUOTE_PREFETCH_REFRESH_S = 1.0
_QUOTE_PREFETCH_MAX_AGE_S = 2.5

# Reasons a decision was declined that must NOT be probed. These are real
# limits -- existing exposure, a risk halt, or data we could not read -- and
# probing around them would create exactly the double exposure and the
# post-halt buying that the gate exists to prevent. Everything else the gate
# declines is selectivity, which is what the probe arm is for.
PROBE_NEVER = ("venue", "already positioned", "halt", "loss", "streak",
               "bankroll", "stale", "unavailable", "unreadable",
               "unconfirmed", "no position")


def probeable(reason: str) -> bool:
    """Is this declined decision safe to take as a small measurement trade?"""
    low = (reason or "").lower()
    if not low:
        return False
    return not any(bad in low for bad in PROBE_NEVER)


def stale_maker_px(display: float | None, ask: float | None, fair_side: float,
                   offset: float, tick: float = 0.01) -> float | None:
    """Where to REST when the venue's display is stale, or None if nowhere.

    Three ceilings, and the binding one wins:
      * `display + offset` -- the stale price is the whole point; the offset is
        the fill-rate/price knob (see config.stale_maker_offset);
      * `fair_side` -- never post above what we think it is worth;
      * `ask - tick` -- a maker order must REST. Posting at or above the ask
        crosses, which pays the spread AND the taker fee, and lands in exactly
        the bucket the live record shows losing money (-0.0108/$1).

    Pure, so the rule is testable without a venue or a book.
    """
    if display is None or ask is None:
        return None
    if not (0.0 < display < 1.0) or not (0.0 < ask < 1.0):
        return None
    if not (0.0 < fair_side < 1.0):
        return None
    limit = round(min(display + offset, fair_side, ask - tick), 2)
    if limit < 0.01 or limit >= ask:
        return None
    return limit


def pool_for_mode(mode: str | None) -> tuple[bool, str | None]:
    """Which risk budget a fill's MODE belongs to: (reserved, pool).

    Arms are independent -- one arm's position must never refuse another's --
    so every arm books into a budget of its own and releases that same budget
    on settle. Getting this mapping wrong in either direction is expensive:
    releasing the wrong pool leaves a slot occupied forever (the arm goes
    permanently silent), and claiming the wrong one lets an arm double.

    'maker' is the impulse arm's resting path, not a separate arm, so it shares
    the impulse budget. Everything else -- 'live', 'stale_maker', 'double',
    'paper' -- is the main arm under a different execution label.
    """
    if mode == "cross":
        return True, None
    if mode in ("impulse", "maker"):
        return False, "impulse"
    return False, None


def crossed_side(ticks, strike: float, look_s: float,
                 slack_s: float = 2.0) -> str | None:
    """Which side did the spot just cross INTO, or None if it did not.

    `ticks` is chronological (ts_ms, price). Returns "Up"/"Down" when the newest
    tick sits on the opposite side of `strike` from the newest tick at least
    `look_s` older -- i.e. the in-the-money side just swapped.

    The reference tick must not be STALER than look_s + slack: an old crossing
    is not a fresh one, and the venue has had time to reprice it. Same guard
    `impulse_bps` applies for the same reason.
    """
    if not ticks or strike is None or strike <= 0:
        return None
    ts_now, px_now = ticks[-1]
    if not px_now or px_now <= 0:
        return None
    side_now = "Up" if px_now > strike else "Down"
    target = ts_now - int(look_s * 1000)
    oldest = ts_now - int((look_s + slack_s) * 1000)
    for ts, px in reversed(ticks[:-1]):
        if ts > target:
            continue
        if not px or px <= 0 or ts < oldest:
            return None
        return side_now if ("Up" if px > strike else "Down") != side_now else None
    return None


def maker_limit_px(fair_side: float, quote: float | None,
                   tick: float = 0.01) -> float | None:
    """The price to POST when we want to be the maker, or None if there is none.

    A maker order must REST on the book, so it has to sit strictly below the ask
    -- pricing at or above the ask crosses, which makes it a taker that pays both
    the spread and the fee. It is also capped by our own fair: posting above fair
    is buying the contract for more than we believe it is worth.

    Measured on 1,187 recorded rounds (`tools.backtest_impulse_orders`, the same
    119 impulse entries priced four ways):
      take the ask   +0.0590/$1  pooled-t +0.44  halves -0.04/+0.15   noise
      post at fair   +0.5890/$1  pooled-t +2.23  halves +0.40/+0.77   43% fill
    The gap is selection, not luck: a taker pays the spread to chase a signal the
    whole book can see, while a resting bid only fills when someone crosses to us
    -- i.e. when the venue's price is stale. Makers also pay no fee here (the
    schedule is takerOnly).
    """
    if quote is None or not (0.0 < quote < 1.0):
        return None
    if not (0.0 < fair_side < 1.0):
        return None
    limit = round(min(fair_side, quote - tick), 2)
    if limit < 0.01 or limit >= quote:
        return None
    return limit


def sec_into_candle(now_ms: int) -> int:
    """Seconds elapsed since the current 1-minute candle opened (0-59)."""
    return (now_ms // 1000) % 60


def round_row(r: Round) -> dict:
    up, down = r.up, r.down
    return {
        "slug": r.slug, "market_topic_id": r.market_topic_id, "market_id": r.market_id,
        "condition_id": r.condition_id, "symbol": r.symbol,
        "start_ms": r.start_ms, "end_ms": r.end_ms,
        "strike": r.strike, "end_price": r.end_price,
        "fee_rate_bps": r.fee_rate_bps,
        "up_token_id": up.token_id if up else None,
        "down_token_id": down.token_id if down else None,
        "price_feed_id": r.price_feed_id, "feed_provider": r.feed_provider,
        "status": r.status, "winner": r.winner_name,
    }


class Bot:
    def __init__(self, mode: str, store: Store, session: aiohttp.ClientSession):
        self.mode = mode
        self.store = store
        self.session = session
        self.market = MarketFeed(session)
        # One spot feed and volatility estimator per market in MARKETS. There is
        # only one now (BTC 5m); the dicts stay keyed by symbol because the
        # feeds, the histories and the models are all per-underlying.
        self.symbols = sorted({sym for _, sym, _ in MARKETS})
        self.spots: dict[str, SpotFeed] = {
            sym: SpotFeed(session, symbol=sym) for sym in self.symbols}
        self.vols: dict[str, VolEstimator] = {}
        # Rolling 1m-bar history per symbol, feeding the strategy models.
        self.histories: dict[str, PriceHistory] = {}
        # Recent (ts_ms, spot) per symbol for the impulse trigger (entry_mode
        # "impulse"). Capped well above the longest impulse window.
        self._ticks: dict[str, list[tuple[int, float]]] = {}
        self._cross_refused: dict = {}
        self.model = build_model(settings.pricing_model)
        # Opt-in per-side gate: Up is priced by `up_model` when it differs
        # from the default model (prototype: drift-adjusted digital for Up).
        self.up_model = (build_model(settings.up_model)
                         if settings.up_model.strip().lower()
                         != settings.pricing_model.strip().lower()
                         else None)
        self.spot = self.spots["BTCUSDT"]
        self.vol = VolEstimator(
            lam=settings.vol_ewma_lambda,
            floor=settings.vol_floor_1m,
            lookback=settings.vol_lookback_minutes,
        )
        self.guard = RiskGuard(
            daily_loss_cap=settings.daily_loss_cap_usdt,
            max_consecutive_losses=settings.max_consecutive_losses,
            max_open_rounds=settings.max_open_rounds,
            daily_loss_cap_pct=settings.daily_loss_cap_pct,
            bankroll=lambda: self.bankroll,
        )
        # --resume deliberately clears a persisted halt (fresh daily budget).
        self.resume_guard = False
        self.broker = None
        # Read-only client for the authenticated order book. Available in every
        # mode, so paper trading prices against the same book as live.
        self.book: LiveBroker | None = None
        self.ws_book: PredictionBook | None = None
        self._ws_task = None
        self._ws_gap_n = 0
        self._ws_gap_sum = 0.0
        # Keyed by FILL ID, not slug. "One position per round" was also a data
        # structure, and the double-down puts two fills in one round: keyed by
        # slug, the second booking overwrote the first, and the restore path
        # kept only one of them. Measured 2026-09-12: an add lost $2.23 and no
        # settlement row was ever written for it, so the ledger silently
        # understated the loss.
        self.open_fills: dict[int, tuple[str, Fill]] = {}   # fill_id -> (slug, Fill)
        # Wall-clock stamp of the last winnings-claim sweep (see
        # sweep_claims). 0.0, so the first tick after a restart picks up any
        # claim a crash or a venue rejection left stranded.
        self._last_claim_sweep = 0.0
        self.bankroll = settings.bankroll_usdt
        # -- fast impulse path (see _impulse_tick) --------------------------
        # The current round and the last executable asks seen for it, refreshed
        # by every poll. The fast path fires between polls, so it acts on these
        # rather than re-reading the venue: the round's strike and tokens do not
        # change, and the only price that matters is the one that comes back
        # WITH the order's own quote.
        self._round: Round | None = None
        self._asks: tuple[float | None, float | None] = (None, None)
        self._sigma = 0.0
        # Cross arm: one claim per round, and its own day-scoped counter.
        self._cross_fired: dict[str, int] = {}
        # The side already taken in a round, so a later crossing cannot buy the
        # opposite one. See config.cross_allow_reversal.
        self._cross_side: dict[str, str] = {}
        # Warm quotes for the cross arm's NEXT crossing (see
        # _prefetch_cross_quote): token -> (monotonic_ts, stake, quote).
        self._quote_pre: dict[str, tuple[float, float, dict]] = {}
        self._quote_pre_busy: set[str] = set()
        self._quote_pre_tasks: set = set()
        # Rounds with a cross decision IN FLIGHT. `_cross_fired` and
        # `_cross_last_ms` are not written until after the venue read in
        # _cross_tick, so on the websocket clock (~114/s) every tick that
        # arrives during that round-trip re-reads them unset, passes the
        # same gates and claims again. The re-entry gap cannot cover it:
        # it is stamped WITH the claim, i.e. after the await, while the
        # measured order latency is 2.7-5.2s. A set, not a counter --
        # entries within a round are serialised, not blocked, so
        # cross_max_per_round still admits the ones it is meant to.
        self._cross_inflight: set[str] = set()
        self._cross_day = ""
        self._cross_count = 0
        # Rounds this process has already triggered in, so one impulse cannot
        # buy the same round twice between polls.
        self._impulse_fired: set[str] = set()
        # Double-down state. In memory only: the daily loss cap is the real
        # limit and is restored from the DB, while this cap is an experiment
        # rate-limit whose exact value across a restart does not matter.
        self._doubled: set[str] = set()
        self._double_count = 0
        # Consecutive venue reads that have disagreed with the ledger. A single
        # disagreement is the redemption lag; this counter is what tells the two
        # apart (see _reconcile_bankroll).
        self._bankroll_drift = 0
        self.pnl = PnLSummary()
        self._order_cooldown_until = 0.0
        # slug -> consecutive polls the same side has shown an edge
        self._streak: dict[str, tuple[str, int]] = {}
        # slug -> 1m candle index the streak was counted in
        self._candle: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._last_vol_refresh = 0.0

    # -- setup --------------------------------------------------------------
    async def setup(self) -> None:
        for sym, feed in self.spots.items():
            await feed.start()
            v = VolEstimator(lam=settings.vol_ewma_lambda, floor=settings.vol_floor_1m,
                             lookback=settings.vol_lookback_minutes)
            klines = await fetch_klines(
                self.session, "1m", settings.vol_lookback_minutes, symbol=sym)
            v.seed_from_klines(klines)
            self.vols[sym] = v
            hist = PriceHistory()
            hist.seed(klines)
            self.histories[sym] = hist
            self._store_bars(sym, klines[:-1])   # seed the bars table too
        self.vol = self.vols["BTCUSDT"]
        self._last_vol_refresh = time.time()
        log.info("pricing model: %s", self.model.name)
        if self.up_model is not None:
            log.info("per-side gate: Down=%s Up=%s",
                     self.model.name, self.up_model.name)

        if settings.has_credentials:
            book = LiveBroker(self.session, settings.binance_api_key,
                              settings.binance_api_secret,
                              slippage_bps=settings.slippage_bps)
            if await book.discover_wallet():
                self.book = book
                if settings.ws_book_enabled:
                    # Reuses the broker purely as a signer: the WS handshake
                    # takes the same HMAC-signed, sorted query string as SAPI.
                    self.ws_book = PredictionBook(
                        api_key=settings.binance_api_key,
                        api_secret=settings.binance_api_secret,
                        session=self.session, signer=book)
                    self._ws_task = asyncio.create_task(self.ws_book.run())
                    log.info("prediction book websocket enabled "
                             "(max age %.1fs, REST fallback on staleness)",
                             settings.ws_book_max_age_s)
            else:
                log.warning("no prediction wallet; falling back to displayed prices")


        if self.mode == "paper":
            self.broker = PaperBroker(
                starting_balance=100.0,
                # None -> charge the measured per-price fee curve.
                fee_rate=None,
                # With the real book we buy at the ask, so the paper broker must
                # not add a second spread on top of it.
                half_spread=0.0 if self.book else settings.assumed_half_spread,
            )
        elif self.mode == "live":
            if not self.book:
                raise SystemExit(
                    "live mode needs a prediction wallet. Check that the API key has "
                    "prediction access and that a Prediction Account exists."
                )
            self.broker = self.book
            # equity(), not balance(): cash alone under-states the account by
            # any open position and by winnings not yet redeemed.
            bal = await self.broker.equity()
            log.info("LIVE mode. wallet %s balance $%.4f",
                     self.broker.wallet_address, bal)
            if bal > 0:
                # Sizing must track real equity rather than a stale setting.
                self.bankroll = bal
            if bal < settings.min_order_usdt:
                raise SystemExit(f"balance ${bal:.4f} below the venue minimum "
                                 f"${settings.min_order_usdt:.2f}")

        if self.mode != "record":
            self._restore_open_positions()
            self._seed_cross_count()
        if self.mode == "live":
            self._seed_guard_state()
            if self.resume_guard:
                self.guard.resume()
                # Persist the reset: without a marker the next restart
                # re-seeds today's old settlements and re-halts the bot, so
                # the reset would only live until the next crash/restart.
                self.store.set_meta("guard_reset_ms",
                                    str(int(time.time() * 1000)))

    def _broker_modes(self) -> tuple[str, ...]:
        """Every fill mode that settles through THIS broker.

        A mode is a LABEL, not a broker: 'double' is placed and settled by the
        same live broker as 'live', so a `mode = broker.mode` filter silently
        excluded it. Measured 2026-09-12, on the first doubled round:
          * the add orphaned on restart and no settlement row was ever written
            for a $2.23 loss, so the ledger understated it; and
          * the guard's daily P&L would have missed that loss too -- the worse
            half, because it lets a restart trade past the daily cap.
        """
        # 'maker' and 'stale_maker' are the same live broker under a different
        # LABEL, exactly like 'double'. Omitting them would reproduce the bug
        # this docstring describes: the fill orphans on restart, no settlement
        # row is written, and the guard's daily P&L misses the loss -- which
        # lets a restart trade past the daily cap. 'maker' was already reachable
        # via impulse_execution and already exposed to this; it is listed now
        # because stale_maker_enabled makes the maker path routine.
        return (("live", "double", "maker", "stale_maker", "cross", "impulse")
                if self.broker.mode == "live"
                else ("paper", "impulse", "maker"))

    def _seed_cross_count(self) -> None:
        """Carry today's cross ATTEMPTS into the fresh counter.

        `_cross_count` starts at 0 in __init__, so every restart handed the arm
        a fresh daily budget: on a day with 208 restarts, `cross_max_per_day`
        bounded nothing at all. The deleted ticket arm seeded the same counter
        from the database for exactly this reason.

        ATTEMPTS, not fills: _cross_tick increments the counter before ordering
        and writes its decision row on every attempt, so counting decisions is
        what reproduces the in-memory number. Counting fills instead would let
        a run of unfilled attempts reset the budget, which is the case the cap
        exists to bound.

        The per-ROUND cap needs no equivalent: it goes through check_reserved,
        which reads `reserved_open`, and _restore_open_positions rebuilds that
        from unsettled fills.
        """
        # UTC midnight, arithmetically: time.mktime() would read the string
        # back as LOCAL time while every other day boundary in this repo is
        # gmtime, so on a non-UTC host the window would be hours out.
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        start_ms = int((now - (now % 86400.0)) * 1000)
        try:
            rows = self.store.query(
                "SELECT COUNT(*) AS c FROM decisions "
                "WHERE reason LIKE 'cross into%' AND ts_ms >= ?", (start_ms,))
        except Exception as e:                              # noqa: BLE001
            log.warning("could not seed the cross daily count (%s); starting "
                        "from 0 for today", type(e).__name__)
            return
        n = int(rows[0]["c"]) if rows else 0
        self._cross_day = today
        self._cross_count = n
        if n:
            log.info("cross arm already attempted %d time(s) today; the daily "
                     "cap is counted from the database, not from memory", n)

    def _seed_guard_state(self) -> None:
        """Carry today's live PnL and loss streak into the fresh guard.

        Without this, a restart after the daily cap is breached lets the bot
        trade again on a day that already lost its budget.

        Both quantities are DAY-SCOPED. The streak walk therefore stops at the
        first settlement from an earlier date instead of reading the last N
        settlements regardless of age: otherwise a restart on a fresh day would
        inherit yesterday's losses and halt a day that should start clean.
        """
        today = time.strftime("%Y-%m-%d", time.gmtime())
        mode = getattr(self.broker, "mode", "live")
        reset_ms = 0
        if mode == "live":
            marker = self.store.get_meta("guard_reset_ms")
            if marker:
                try:
                    reset_ms = int(marker)
                except ValueError:
                    reset_ms = 0
        modes = self._broker_modes()
        ph = ", ".join("?" * len(modes))
        # TWO queries, deliberately. They used to share one `LIMIT 50`, which is
        # correct for the streak walk (it only ever needs the newest few) and
        # WRONG for the daily total: on 2026-09-13 the day held 145 settlements
        # worth +$2.49, the newest 50 of them summed to -$7.85, and every
        # restart seeded that phantom loss. Two more real losses then tripped a
        # $9.49 cap on a day that was up $9.98 -- the bot halted itself out of
        # a winning session, and re-halted on each restart because the seed is
        # re-read every time.
        #
        # The daily sum is bounded by the day, not by a row count.
        day_start_ms = int((time.time() - (time.time() % 86400.0)) * 1000)
        since = max(reset_ms, day_start_ms)
        pnl_rows = self.store.query(
            "SELECT COALESCE(SUM(s.pnl), 0.0) AS p FROM settlements s "
            f"JOIN fills f ON f.id = s.fill_id WHERE f.mode IN ({ph}) "
            "AND COALESCE(s.void, 0) = 0 AND s.ts_ms >= ?", (*modes, since))
        pnl = float(pnl_rows[0]["p"]) if pnl_rows else 0.0
        rows = self.store.query(
            "SELECT s.pnl, s.ts_ms FROM settlements s "
            f"JOIN fills f ON f.id = s.fill_id WHERE f.mode IN ({ph}) "
            "AND COALESCE(s.void, 0) = 0 AND s.ts_ms >= ? "
            "ORDER BY s.ts_ms DESC LIMIT 50", (*modes, reset_ms))
        streak = 0
        for r in rows:                      # newest first
            if time.strftime("%Y-%m-%d", time.gmtime(r["ts_ms"] / 1000)) != today:
                break                       # the streak is day-scoped
            if r["pnl"] < 0:
                streak += 1
            else:
                break
        self.guard.seed_daily(pnl, streak)

    def _restore_open_positions(self) -> None:
        """Reload fills that never settled.

        Without this a restart orphans any open position: the round resolves,
        nothing records the settlement, and the risk guard's view of open rounds
        and PnL silently diverges from reality.
        """
        modes = self._broker_modes()
        ph = ", ".join("?" * len(modes))
        rows = self.store.query(
            "SELECT f.* FROM fills f "
            "LEFT JOIN settlements s ON s.fill_id = f.id "
            f"WHERE s.id IS NULL AND f.mode IN ({ph})", modes)
        for row in rows:
            fill = Fill(
                slug=row["slug"], side=row["side"],
                requested_px=row["requested_px"], filled_px=row["filled_px"],
                shares=row["shares"], stake=row["stake"], fee_paid=row["fee_paid"],
                order_id=row["order_id"] or "", ts_ms=row["ts_ms"],
                token_id=row["token_id"],
            )
            self.open_fills[row["id"]] = (row["slug"], fill)
            reserved, pool = pool_for_mode(row["mode"])
            self.guard.on_open(row["slug"], reserved=reserved, pool=pool)
        if rows:
            log.info("restored %d unsettled position(s): %s",
                     len(rows), ", ".join(r["slug"] for r in rows))
    async def _venue_position_tokens(self) -> set[str] | None:
        """Tokens the venue currently holds a position in (ground truth)."""
        try:
            pos = await self.broker.positions()
        except Exception:
            return None
        return {p.get("tokenId") for p in pos or []}

    async def _unbooked_round_tokens(self, r, up, down) -> set[str] | None:
        """Tokens in THIS round the venue holds that our ledger cannot explain.

        Returns None when the venue cannot be read at all, which is its own
        reason to sit out: we cannot reason about exposure we cannot see.

        ARMS ARE INDEPENDENT -- one arm's position must never refuse another's.
        The venue cannot say WHICH arm bought a token, so asking it "is anything
        held here?" conflates two different situations:

          * a position our ledger already knows about. That is either another
            arm (which is allowed, and is what the per-arm pools bound) or our
            own, which the pool guard has already counted. It must not block.
          * a position our ledger has NO record of -- an order that was never
            confirmed but actually filled. _restore_open_positions cannot replay it
            because it never reached the database, so the venue is the only
            place it exists. THAT is what this check is for, and it still
            blocks.

        Measured cost of conflating them: 52% of the cross arm's fill-aware
        triggers over 6 days were refused because the MAIN arm held the round,
        at an EV indistinguishable from the ones that got through.
        """
        held = await self._venue_position_tokens()
        if held is None:
            return None
        booked = {getattr(f, "token_id", None)
                  for slug, f in self.open_fills.values() if slug == r.slug}
        return {t for t in (up.token_id, down.token_id)
                if t in held} - {b for b in booked if b}

    async def refresh_vol_if_stale(self) -> None:
        if time.time() - self._last_vol_refresh < 60.0:
            return
        try:
            for sym, v in self.vols.items():
                klines = await fetch_klines(self.session, "1m", 5, symbol=sym)
                for k in klines[:-1]:      # skip the still-forming bar
                    o, c = float(k[1]), float(k[4])
                    if o > 0 and c > 0:
                        v.add(math.log(c / o))
                self.histories[sym].merge(klines)
                # Persist the bars incl. taker-buy volume (kline field 9): the
                # quotes table cannot reconstruct order flow, so without this
                # the order-flow models can never be replayed or tuned.
                self._store_bars(sym, klines[:-1])
            self._last_vol_refresh = time.time()
        except Exception as e:
            log.warning("vol refresh failed: %s: %s", type(e).__name__, e)

    def _store_bars(self, symbol: str, klines: list[list]) -> None:
        """Store (open_ms, close, volume, taker_buy) for completed bars."""
        rows = []
        for k in klines:
            try:
                rows.append((int(k[0]), float(k[4]), float(k[5]), float(k[9])))
            except (TypeError, ValueError, IndexError):
                continue
        if rows:
            self.store.upsert_bars(symbol, rows)

    # -- main tick ----------------------------------------------------------
    async def tick(self) -> None:
        await self.refresh_vol_if_stale()
        primary = MARKETS[0][0]
        now_s = time.time()
        now_ms = int(now_s * 1000)
        cache = getattr(self, "_round_cache", None)
        if cache is None:
            cache = self._round_cache = {}
        # A round's METADATA -- strike, token ids, end time -- is fixed once the
        # round opens. Only spot and the ask move, and both arrive on their own
        # feeds. So a family needs the paginated sweep only when its round has
        # actually ROLLED, which is what makes six traded families affordable:
        # `rounds_by_prefixes` stops early only when EVERY requested prefix is
        # found, and a quiet family sinks through the day (bnb-updown-5m hit
        # offset 1450 -- 30 pages, 3.95s). Re-sweeping all six every poll is
        # precisely the coupling that made short crossings invisible.
        stale = [p for p, _, _ in MARKETS
                 if cache.get(p) is None
                 or getattr(cache[p], "end_ms", 0) <= now_ms
                 or getattr(cache[p], "strike", None) is None]
        # The primary is refetched every poll INLINE: it is page 1 (highest
        # volume) so it costs one request, and the model arm reads its live
        # displayed price, not just its strike.
        found = await self.market.rounds_by_prefixes([primary])
        for pfx, rr in found.items():
            if rr:
                cache[pfx] = rr
        # Everything else is refreshed OFF the poll. `rounds_by_prefixes` stops
        # paging only once every requested prefix is found, and a quiet family
        # sinks through the day (bnb-updown-5m at offset 1450 -- 30 pages,
        # ~4s). At a 5m roll all three 5m families go stale at once, so doing
        # that inline stalled the poll for 4.4-4.7s every five minutes
        # (measured: median 133ms, max 4673ms).
        #
        # That stall is not harmless just because the NEW 5m round is at tau
        # ~300 and out of the trade window: the 15m families roll on a
        # different schedule, so a 5m roll can blind a 15m market sitting at
        # tau 5-60. The refresh therefore runs as a background task and the
        # poll keeps using the cache until it lands.
        others = [p for p in stale if p != primary]
        task = getattr(self, "_round_refresh", None)
        if others and (task is None or task.done()):
            if task is not None and task.done():
                with contextlib.suppress(Exception):
                    task.result()               # surface a crash, never swallow
            self._round_refresh = asyncio.create_task(
                self._refresh_rounds(others))
        # Spot history for EVERY symbol, appended HERE rather than in _record.
        # The range gate reads this buffer, and a family whose round is only
        # refetched on a roll would otherwise measure its range on a handful of
        # samples -- the gate would return None ("no opinion") and silently not
        # apply. Costs no network: the spot feeds are websockets.
        buf = getattr(self, "_ticks", None)
        if buf is None:
            buf = self._ticks = {}
        # Guarded: fixtures build a Bot via __new__ and have no `spots`.
        spots = getattr(self, "spots", None) or {}
        for sym in {s for _, s, _ in MARKETS}:
            f = spots.get(sym)
            px = getattr(f, "price", None) if f is not None else None
            if px:
                tbuf = buf.setdefault(sym, [])
                tbuf.append((now_ms, px))
                if len(tbuf) > 2400:
                    del tbuf[: len(tbuf) - 2400]
        # `trade` is membership of CROSS_TRADED, NOT "is primary". The two are
        # deliberately separate: MARKETS widens the tape, CROSS_TRADED widens
        # the risk, and collapsing them is how the daily loss cap was once
        # bypassed.
        self._mkt = {}
        for prefix, sym, _ in MARKETS:
            r = cache.get(prefix)
            if r:
                await self._record(r, sym,
                                   trade=(prefix in CROSS_TRADED),
                                   primary=(prefix == primary))
        # The websocket book follows EVERY market, not just the traded one. The
        # recorded-only families had no book at all (the REST fetch in _record
        # is gated on `primary` because it costs SAPI quota and we have been
        # rate limited), so their tape carried win rates but no price --
        # and win rate alone cannot clear a market, whose break-even is ~0.665
        # at live fills. The websocket costs no quota, and `follow` puts all of
        # them in ONE topic, so this is the cheap half of that fix.
        #
        # The topic is a connect-time parameter, so the set changing forces a
        # handshake. The set changes at a round roll, which for the 5m families
        # is the same wall-clock boundary for all of them; a 15m market can be
        # mid-round (inside the cross window) when that happens, and will fall
        # back to the REST/displayed price for the reconnect. That fallback is
        # the same one a stale book already takes.
        wsb = getattr(self, "ws_book", None)
        if wsb is not None:
            # EVERY family the cross arm may order in. Off-primary there is no
            # REST book and no displayed-price fallback (see _cross_tick), so a
            # market without a websocket book simply cannot trade.
            #
            # THIS IS THE MEASURED RISK OF THE WIDENING. Following all six once
            # took the poll from a 1.00s median to 4.15s (3.04s after the
            # handshake fix) because the book feed ingests ~6x the message
            # volume in this process -- and that is not a background cost: the
            # cross arm compares the newest tick with one 1-3s old, so a slow
            # poll makes short crossings invisible. btc-updown-5m 1789336500 is
            # the worked case, a ~1.5s crossing the arm never sampled.
            #
            # Two things are different now. The paginated sweep no longer runs
            # every poll (rounds are cached until they roll), which was the
            # other half of that 4.15s; and `poll_ms` below reports the cadence
            # continuously, so this is monitored rather than assumed. If it
            # degrades, `cross_ws_follow_all` turns it off without a deploy.
            ids = [c["round"].market_id
                   for pfx, c in (getattr(self, "_mkt", None) or {}).items()
                   if c.get("round") is not None and c["round"].market_id
                   and (settings.cross_ws_follow_all or pfx == primary)]
            if not ids:
                r0 = cache.get(primary)
                if r0 is not None and r0.market_id:
                    ids = [r0.market_id]
            if ids:
                wsb.follow(ids)
        # AFTER the subscription, not before. The arm refuses off-primary
        # without a fresh websocket book, so evaluating first would spend the
        # poll in which a newly-appeared family could have traded -- and on a
        # round roll that is every family at once.
        await self._cross_all()

    async def _refresh_rounds(self, prefixes: list[str]) -> None:
        """Re-sweep rolled families off the poll's critical path."""
        try:
            found = await self.market.rounds_by_prefixes(prefixes)
        except Exception as e:                              # pragma: no cover
            log.warning("round refresh failed (%s: %s)", type(e).__name__, e)
            return
        cache = getattr(self, "_round_cache", None)
        if cache is None:
            cache = self._round_cache = {}
        for pfx, r in found.items():
            if r:
                cache[pfx] = r

    async def _record(self, r, symbol: str, *, trade: bool,
                      primary: bool = False) -> None:
        self.store.upsert_round(round_row(r))
        feed = self.spots[symbol]
        vol = self.vols[symbol]
        # self.spot / self.vol are the PRIMARY's and are set once. They used to
        # be reassigned on every market in the loop, so after the loop they held
        # the LAST family's feed while _round still held BTC -- which on
        # 2026-09-12 compared ETH's price to BTC's strike (-9672 bps) and bought
        # BTC Down because ETH had ticked. Everything below reads the locals.
        if primary:
            self.spot = feed
            self.vol = vol

        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        up, down = r.up, r.down
        if not up or not down or r.strike is None:
            return

        spot = feed.price
        sigma = vol.sigma_1m()

        # Buy prices come from the real book (the ask), not the displayed
        # last-trade price, which was observed 2-3 cents away from the mid.
        up_bid = up_ask = down_bid = down_ask = None
        book_age_ms = None
        if self.book and r.market_id and primary:
            # Only the PRIMARY, not every traded family: the REST book costs
            # SAPI quota and we have been rate limited, so six families would
            # be twelve calls a second. The other families price off the
            # websocket book instead, and _cross_one REFUSES them outright when
            # that book is stale rather than falling back to a displayed price
            # that may be seconds old. The untraded market's
            # books were fetched every second purely for the recorder, tripling
            # SAPI calls for data no replay uses (the tool is BTC-only). If the
            # venue rate-limits, best_prices() returns None and the bot silently
            # falls back to the displayed price plus an assumed spread.
            # Concurrently: sequential fetches let the price move between them,
            # which manufactured phantom complete-set arbitrages in recorded data
            # (one book drifted 0.21 between two calls).
            bp_up, bp_dn = await asyncio.gather(
                self.book.best_prices(up.token_id, r.market_id),
                self.book.best_prices(down.token_id, r.market_id))
            if bp_up:
                up_bid, up_ask = bp_up
            if bp_dn:
                down_bid, down_ask = bp_dn
        # The WEBSOCKET book, if it is fresh, overrides the REST poll we just
        # did. It is ~4x the resolution (median 250ms against 1000ms) and the
        # venue reprices 66% of a cross inside one second, so the poll was
        # systematically quoting the fast arms a price that had already gone.
        # Staleness falls back rather than waiting: a frozen feed pricing every
        # order off a dead book is worse than the poll it replaces.
        wsb = getattr(self, "ws_book", None)
        if wsb is not None and r.market_id:
            # No `trade` gate: tick() subscribes every market, so the recorded
            # families get a real ask here instead of None. Nothing downstream
            # of this is reached for them -- _record returns on `not trade`
            # before any order path -- so this widens the TAPE, not the risk.
            top = wsb.best(r.market_id, settings.ws_book_max_age_s)
            if top is not None:
                # Measured while it trades, so the value of this feed is a
                # number in the log rather than an argument.
                if up_ask is not None:
                    self._ws_gap_n = getattr(self, "_ws_gap_n", 0) + 1
                    self._ws_gap_sum = getattr(self, "_ws_gap_sum", 0.0) + abs(top.up_ask - up_ask)
                    if self._ws_gap_n % 500 == 0:
                        log.info("ws book vs REST poll: mean |ask gap| %.4f "
                                 "over %d samples (%d msgs)",
                                 self._ws_gap_sum / self._ws_gap_n,
                                 self._ws_gap_n, wsb.messages)
                up_bid, up_ask = top.up_bid, top.up_ask
                down_bid, down_ask = top.down_bid, top.down_ask
                book_age_ms = int(top.age_s() * 1000)
        # RECORD-ONLY fallback. The gate above is right for trading, and it also
        # decides what the tape contains -- off-primary that is most of it. Ask
        # coverage was btc-5m ~100% (the primary, which gets the REST book every
        # poll) against eth-5m 31% and bnb-5m 17%, which is why bnb had FOUR
        # priceable crossings to tune on and btc had 302. Every per-symbol
        # question was blocked on that.
        #
        # This fills the recorded prices in ONLY where the fresh lookup returned
        # nothing, and stamps `book_age_ms` so a replay can filter to whatever
        # freshness it wants. It cannot reach an order: the trading paths do
        # their own `wsb.best(..., ws_book_max_age_s)` lookups.
        if (wsb is not None and r.market_id and up_ask is None
                and settings.ws_book_record_max_age_s > 0):
            stale = wsb.best(r.market_id, settings.ws_book_record_max_age_s)
            if stale is not None:
                up_bid, up_ask = stale.up_bid, stale.up_ask
                down_bid, down_ask = stale.down_bid, stale.down_ask
                book_age_ms = int(stale.age_s() * 1000)
        buy_up = up_ask if up_ask is not None else up.price
        buy_down = down_ask if down_ask is not None else down.price
        # No spread guess when the ask is the actual executable price.
        half_spread = 0.0 if up_ask is not None else settings.assumed_half_spread

        self.store.insert("quotes", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau,
            "up_price": up.price, "down_price": down.price,
            "up_chance": up.chance, "down_chance": down.chance,
            "liquidity": r.liquidity, "trade_volume": r.trade_volume,
            "spot": spot, "spot_ts_ms": feed.ts_ms,
            "oracle": None, "oracle_ts_ms": None,
            "strike": r.strike,
            "book_age_ms": book_age_ms,
            "up_bid": up_bid, "up_ask": up_ask,
            "down_bid": down_bid, "down_ask": down_ask,
            "fee_rate_bps": r.fee_rate_bps,
        })

        if self.mode == "record" or not trade or spot is None or tau <= 0:
            return

        # Per-market context for the cross arm, which now runs across every
        # family in CROSS_TRADED. tick() clears and rebuilds this each poll, so
        # a family that stops reporting drops out rather than going stale.
        mkt = getattr(self, "_mkt", None)
        if mkt is None:
            mkt = self._mkt = {}
        mkt[market_prefix(r.slug) or r.slug] = {
            "round": r, "asks": (buy_up, buy_down), "feed": feed,
            "symbol": symbol, "sigma": sigma, "primary": primary,
        }
        # Everything below is the PRIMARY-only path: the model, impulse and
        # double arms were measured on BTC 5m and are not widened here.
        if not primary:
            return

        # What the fast impulse path needs to act between polls (see
        # _impulse_tick). Deliberately not cached in record mode: `_round` is
        # the fast path's only precondition, so leaving it None is what
        # guarantees --mode record can never place an order.
        self._round = r
        self._asks = (buy_up, buy_down)
        self._sigma = sigma
        # The feed THIS round settles against, captured here because here is the
        # only place it is unambiguous: _record runs once per market in MARKETS
        # and each call reassigns self.spot, so after the loop self.spot holds
        # the LAST symbol's feed (eth-updown-15m -> ETHUSDT) while _round holds
        # the traded BTC market. _impulse_tick runs on the websocket clock, i.e.
        # outside this call, so reading self.spot there compared ETH's price to
        # BTC's strike (measured 2026-09-12: spot=2530.835 vs strike=77344.005,
        # -9672 bps) and bought BTC Down because ETH had ticked. The same
        # nonsense fair priced an Up take-limit at max(0.01, 0 - 0.04) = 0.01,
        # which is the unfillable order that stalled the arm for an hour.
        self._primary_spot = feed
        await self._double_tick()

        hist = self.histories[symbol]
        hist.add_tick(spot)
        # tick() now appends `_ticks` for EVERY symbol, so this only reads it.
        # Guarded lookup: fixtures build a Bot without running __init__, and the
        # tick buffer must not be the thing that breaks them.
        ticks = (getattr(self, "_ticks", None) or {}).get(symbol) or []

        # entry_mode "off" stops the MAIN arm here: the cross and double ticks
        # above have already run, the tape is already recorded, and the history
        # buffer above is still maintained so turning the arm back on does not
        # start from a cold estimator. Placed after those deliberately -- the
        # arms are independent, and switching one off must not silence another.
        if settings.entry_mode == "off":
            return

        sig = None
        if settings.entry_mode in ("impulse", "both"):
            if not settings.impulse_on_tick:
                # Trade the underlying's own move. The model is not consulted:
                # its gate measured +1.3%/$1 net with a negative second half on
                # the same rounds, while impulses of 4-8 bps paid +8% to +21%
                # per $1 in BOTH halves. Sub-2 bps impulses are noise and lose
                # the fee, which is what impulse_bps refuses.
                # Only reached with impulse_on_tick OFF: the default owner of
                # the trigger is _impulse_tick, which runs on the feed's clock
                # instead of on this once-a-second poll (the venue answers in
                # 5 ms, so the poll interval was the entire latency budget).
                move = impulse_bps(ticks, settings.impulse_window_s)
                side = impulse_side(move, settings.impulse_bps)
                if side is not None:
                    px = buy_up if side == "Up" else buy_down
                    if px is not None and (settings.impulse_min_price <= px
                                           <= settings.impulse_max_price):
                        sig = Signal(side, px, px, 0.0, 0.0, 0.0, True,
                                     f"impulse {move:+.1f}bps/"
                                     f"{settings.impulse_window_s:.0f}s")
            # This return is UNCONDITIONAL within PURE impulse mode, and it
            # must stay that way: impulse mode exists because the model's gate
            # lost on the same rounds, so falling through to the model block
            # below would trade the model once a second and never the impulse.
            # With impulse_on_tick on, the entry is placed by _impulse_tick and
            # this poll's only job is recording.
            #
            # In "both" mode the fall-through is the POINT, and it does not
            # starve the impulse for two reasons: _impulse_tick runs on the
            # websocket clock and has already had its chance before this poll,
            # and the two gates want the same round only 52 times in 830
            # (measured 2026-09-12). `sig` stays None here, so the model block
            # below picks the round up exactly as it would in "model" mode.
            #
            # A duplicate of the trigger used to sit here, recomputing `move` --
            # which is UNDEFINED when impulse_on_tick is on. It was unreachable
            # only because the return above fired first, so it had to go before
            # this could fall through at all.
            if sig is None:
                if settings.entry_mode == "impulse":
                    return
            else:
                fair_up = sig.quote

        # Which gate produced this signal. In "both" mode the global
        # entry_mode can no longer answer that, and the persistence rule below
        # must apply to model entries while never applying to impulse ones.
        from_impulse = sig is not None

        pred = None
        if sig is None:
            pred = self.model.predict(hist.ctx(
                price=spot, strike=r.strike, tau_s=tau, sigma_1m=sigma,
                quote_up=buy_up, quote_down=buy_down))
            if not pred.emitted or not (0.0 < pred.p_up < 1.0):
                log.debug("model %s holds on %s: %s", self.model.name, r.slug,
                          pred.reason)
                return
            fair_up = pred.p_up
        if sig is None:
            # Per-side pricing: Up may be priced by a different model (the drift
            # adjustment) than Down (the driftless digital).
            up_fair = fair_up
            if self.up_model is not None:
                pred_up = self.up_model.predict(hist.ctx(
                    price=spot, strike=r.strike, tau_s=tau, sigma_1m=sigma,
                    quote_up=buy_up, quote_down=buy_down))
                if pred_up.emitted and 0.0 < pred_up.p_up < 1.0:
                    up_fair = pred_up.p_up
            sig_kw = dict(
                # fee=None -> per-quote fee from the measured curve
                # (fee_bps = 200 * min(1, (1-px)/px)). The 200 is the venue's own
                # reported fee_rate_bps, which it states on 99.7% of recorded ticks,
                # so a per-round override would change nothing today. Do NOT pass
                # fee_rate_bps/1e4 directly: that is the coefficient, and charging it
                # flat would over-price favourites by ~6x and under-price cheap
                # contracts by ~2x.
                fee=None,
                half_spread=half_spread,
                safety_margin=settings.safety_margin,
                fee_model=FeeModel.NOTIONAL,
                max_quote=settings.max_quote,
                min_quote=settings.min_quote,
                min_edge=settings.min_edge,
                edge_haircut=settings.edge_haircut,
            )
            # Rising-regime gate (see config.up_regime_gate): when the last hour is
            # already up, the venue has priced Up correctly and our Up edge is
            # anti-informative (measured WR 0.50 rising vs 0.82 flat/down). Decided
            # BEFORE the side pick, so a blocked Up still falls back to a Down
            # candidate that qualified on the same quote.
            up_allowed, blocked_ret = True, None
            if settings.up_regime_gate > 0:
                ret = hist.one_hour_return()
                if ret is not None and ret > settings.up_regime_gate:
                    up_allowed, blocked_ret = False, ret
            sig = evaluate_hybrid(fair_up, up_fair, buy_up, buy_down,
                                  up_allowed=up_allowed, **sig_kw)
            if blocked_ret is not None and not sig.act:
                sig = Signal(None, sig.fair, sig.quote, sig.edge, sig.hurdle,
                             sig.ev_per_dollar, False,
                             f"Up skipped: 1h ret {blocked_ret:+.4f} > "
                             f"{settings.up_regime_gate:+.3f}")

        # A signal must hold for several consecutive polls. A one-tick edge is
        # usually our own stale book or spot sample rather than a real
        # dislocation, and acting on it was trading noise.
        # Each 1m candle is a fresh prediction problem, so the streak restarts
        # at every candle open.
        candle_idx = now_ms // 60_000
        if self._candle.get(r.slug) != candle_idx:
            self._candle[r.slug] = candle_idx
            self._streak.pop(r.slug, None)
        prev_side, prev_n = self._streak.get(r.slug, (None, 0))
        if sig.act and sig.side == prev_side:
            self._streak[r.slug] = (sig.side, prev_n + 1)
        elif sig.act:
            self._streak[r.slug] = (sig.side, 1)
        else:
            self._streak.pop(r.slug, None)
        streak = self._streak.get(r.slug, (None, 0))[1]

        gate_reason = ""
        # With candle-open gating the final candle opens at tau ~= 59.9s, just
        # under min_tau_s=60. The allowance lets that open stand for its own
        # time; mid-candle ticks are blocked by the candle gate itself.
        min_tau_eff = (settings.min_tau_s - settings.candle_open_window_s
                       if settings.trade_on_candle_open else settings.min_tau_s)
        # Impulse entries are deliberately NOT subject to the persistence rule:
        # the dislocation is the event, and waiting two polls for it to repeat
        # is exactly how the venue's book catches up. The rule was validated with
        # one entry per round on the first qualifying tick.
        if (sig.act and not from_impulse
                and streak < settings.signal_persistence):
            gate_reason = f"signal held {streak}/{settings.signal_persistence} polls"
        elif (settings.side_filter != "both"
              and sig.act and sig.side != settings.side_filter):
            gate_reason = (f"side {sig.side} filtered "
                           f"(trading {settings.side_filter} only)")
        elif (settings.trade_on_candle_open
              and sec_into_candle(now_ms) >= settings.candle_open_window_s):
            gate_reason = (f"mid-candle ({sec_into_candle(now_ms)}s past open; "
                           f"trades only within {settings.candle_open_window_s:.0f}s)")
        elif (settings.trade_on_candle_open
              and round(tau / 60.0) not in settings.trade_candle_opens):
            gate_reason = (f"candle {round(tau / 60.0)} not in "
                           f"{settings.trade_candle_opens}")
        elif not (min_tau_eff <= tau <= settings.max_tau_s):
            gate_reason = f"tau {tau:.0f}s outside window"
        elif (settings.model_max_round_range_bps > 0
              and (rng := self._round_range_bps(r)) is not None
              and not (settings.model_min_round_range_bps
                       <= rng <= settings.model_max_round_range_bps)):
            # This arm only pays in rounds that have MOVED. Outside 5-20bps it
            # is a measured loser in both directions -- -0.127/$1 in calm rounds
            # and -0.177/$1 past 20bps. See config.model_min_round_range_bps.
            gate_reason = (f"round range {rng:.1f}bps outside "
                           f"[{settings.model_min_round_range_bps:.0f},"
                           f"{settings.model_max_round_range_bps:.0f}]")
        elif not self.spot.is_fresh(settings.max_spot_age_s):
            gate_reason = f"spot stale ({self.spot.age_s:.1f}s)"
        elif self.broker.mode == "live" and sig.act and self.book:
            # The venue is the ground truth. If it already shows a position in
            # this round (a fill the bot never booked, e.g. after a restart or
            # an unverifiable order), buying again would double the exposure.
            unbooked = await self._unbooked_round_tokens(r, up, down)
            if unbooked is None:
                gate_reason = "venue position list unavailable"
            elif unbooked:
                gate_reason = "venue has an UNBOOKED position for this round"
        elif time.time() < self._order_cooldown_until:
            gate_reason = "order cooldown after a failure"

        # The risk guard is always consulted when a signal would otherwise act.
        # It must never sit in an else branch: any earlier elif that consumes
        # the chain silently disables it (that is exactly how the daily loss
        # cap was bypassed and today's loss ran to -$5 instead of -$3).
        if not gate_reason:
            allowed, why = self.guard.check(r.slug)
            if not allowed:
                gate_reason = why

        acted = False
        stake = 0.0
        if sig.act and not gate_reason:
            # Edge scaling: the weakest signal the gate accepts is staked at the
            # venue floor, the strongest at the ceiling (see config.sizing_mode).
            # Kelly is available but off by default -- at this bankroll a
            # fractional-Kelly value sits below the venue minimum, and rounding
            # that up to full size inverts the rule it came from.
            # |z| -- how decided the round already is, in units of the remaining
            # move. Used by the sizing tilt; 0.0 means "no information".
            mz = (abs(math.log(spot / r.strike))
                  / (sigma * math.sqrt(tau / 60.0))
                  if spot and r.strike and spot > 0 and r.strike > 0
                  and tau > 0 and sigma > 0 else 0.0)
            stake = self._stake_for(sig, mz)
            acted = stake >= settings.min_order_usdt

        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau,
            "side": sig.side or "NONE", "fair": sig.fair, "quote": sig.quote,
            "edge": sig.edge, "hurdle": sig.hurdle, "sigma_1m": sigma,
            "spot": spot, "oracle": None, "strike": r.strike,
            # `acted` means an order actually filled, so it is written as 0 here
            # and only promoted after the broker confirms. Recording intent
            # instead made a run of rejected orders look like 100 trades.
            "stake": stake, "acted": 0,
            "reason": gate_reason or sig.reason,
        })

        if not acted:
            if gate_reason:
                await self._probe(r, sig, gate_reason=gate_reason, up=up,
                                  down=down, spot=spot, sigma=sigma, tau=tau)
            return

        token = up.token_id if sig.side == "Up" else down.token_id

        # Stale-display maker path (see config.stale_maker_*). The display lags
        # spot by ~2s while the book does not, and the measured edge is ENTIRELY
        # in the fill price: at the book ask this same rule scores +0.0175/$1
        # (t 0.47), at the mid +0.3449 (t 5.88). So when the display is stale
        # this rests a bid near it instead of crossing to the ask.
        #
        # It REPLACES the taker order for this entry rather than adding one --
        # resting and crossing the same signal would double the exposure the
        # one-position-per-round rule exists to prevent. The cost is fill rate,
        # which is the unknown this is built to measure: an unfilled attempt
        # keeps its decision row (acted=0) and is logged, so the rate is
        # recoverable from the database.
        if settings.stale_maker_enabled and hasattr(self.broker, "buy_limit"):
            disp = up.price if sig.side == "Up" else down.price
            gap = (sig.quote - disp) if disp else 0.0
            edge = (sig.fair - disp) if disp else 0.0
            post = stale_maker_px(disp, sig.quote, sig.fair,
                                  settings.stale_maker_offset,
                                  settings.limit_ask_tick)
            if (post is not None
                    and gap >= settings.stale_maker_min_gap
                    and edge >= settings.stale_maker_min_edge):
                log.info("STALE-MAKER post %s %s @%.2f (display %.3f ask %.3f "
                         "fair %.3f gap %+.3f edge %+.3f) stake=$%.2f wait=%.0fs",
                         r.slug, sig.side, post, disp, sig.quote, sig.fair,
                         gap, edge, stake, settings.stale_maker_wait_s)
                fill = await self.broker.buy_limit(
                    slug=r.slug, side=sig.side, token_id=token, limit_px=post,
                    stake=stake, wait_s=settings.stale_maker_wait_s,
                    fill_break=settings.limit_fill_break)
                if not fill:
                    # The measurement this arm exists for: the decision row
                    # stays at acted=0, so fill rate is (fills / attempts).
                    log.info("STALE-MAKER unfilled %s %s @%.2f after %.0fs",
                             r.slug, sig.side, post, settings.stale_maker_wait_s)
                    return
                await self._book_fill(r, sig, fill, did=did, up=up, spot=spot,
                                      sigma=sigma, tau=tau, token=token,
                                      mode="stale_maker")
                return

        fill = await self._place_order(
            r, sig,
            fair_up=sig.fair_up_model if sig.fair_up_model is not None else fair_up,
            token=token, stake=stake)
        if not fill:
            return
        await self._book_fill(r, sig, fill, did=did, up=up,
                              spot=spot, sigma=sigma, tau=tau, token=token)

    async def _probe(self, r, sig, *, gate_reason: str, up, down,
                     spot: float, sigma: float, tau: float) -> None:
        """Trade a DECLINED decision at minimum stake, booked as mode='probe'.

        Purpose is measurement, not profit: the gate declines thousands of
        decisions a day and we cannot tell from the recorded quote which of
        those cells pay (our real fills beat that quote by ~1% of stake). Take
        a small real position in them so the marginal cell can be priced from
        fills instead of from a biased quote, then promote it only if it clears
        day-clustered t > 2 with a positive drop-best-day.

        Deliberately never probes a decision declined for exposure or risk
        reasons: one-position-per-round and the daily halt are real limits, and
        probing around them would create the double-exposure the gate exists to
        prevent.
        """
        if not settings.probe_enabled or sig.side not in ("Up", "Down"):
            return
        if not (0.0 < sig.quote < 1.0):
            return
        # Risk, exposure and data blocks are NOT selectivity. Leave them alone.
        if not probeable(gate_reason):
            return
        fired = getattr(self, "_probe_fired", None)
        if fired is None:
            fired = self._probe_fired = set()
        if r.slug in fired:
            return
        day0 = int(time.time() * 1000) // 86_400_000 * 86_400_000
        try:
            got = self.store.query(
                "SELECT COUNT(*) AS c FROM fills WHERE mode='probe' AND ts_ms>=?",
                (day0,))
        except Exception:
            got = []
        if got and got[0]["c"] >= settings.probe_max_per_day:
            return
        stake = max(settings.probe_stake, settings.min_order_usdt)
        if self.bankroll < stake:
            return
        allowed, why = self.guard.check(r.slug)
        if not allowed:
            return
        fired.add(r.slug)
        token = up.token_id if sig.side == "Up" else down.token_id
        psig = Signal(sig.side, sig.quote, sig.quote, 0.0, 0.0, 0.0, True,
                      f"probe[{gate_reason}]")
        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": int(time.time() * 1000), "tau_s": tau,
            "side": sig.side, "fair": sig.quote, "quote": sig.quote,
            "edge": 0.0, "hurdle": 0.0, "sigma_1m": sigma, "spot": spot,
            "oracle": None, "strike": r.strike, "stake": stake, "acted": 0,
            "reason": f"probe: {gate_reason}",
        })
        fill = await self._place_order(
            r, psig,
            fair_up=(1.0 - sig.quote) if sig.side == "Down" else sig.quote,
            token=token, stake=stake, skip_pretrade_gates=True,
            take_px=sig.quote)
        if not fill:
            log.info("probe %s not filled at %.3f (%s)", r.slug, sig.quote,
                     gate_reason)
            return
        await self._book_fill(r, psig, fill, did=did, up=up, spot=spot,
                             sigma=sigma, tau=tau, token=token, mode=PROBE_MODE)
        log.info("PROBE %s %s $%.2f @%.4f tau %.0fs | declined: %s",
                 r.slug, sig.side, fill.stake, fill.filled_px, tau, gate_reason)

    async def _book_fill(self, r, sig, fill, *, did: int, up,
                         spot: float, sigma: float, tau: float,
                         token: str | None = None,
                         mode: str | None = None) -> None:
        """Record a fill: guard, decision promotion, fill row, P&L log.

        `token` is passed explicitly rather than read off the broker's Fill:
        450 of 455 live fills were persisted with token_id NULL although both
        call sites and the broker do carry it, which left the venue-position
        verification unanswerable and P&L unattributable to Up vs Down. The
        caller always knows the token, so take it from the caller.

        `mode` overrides the broker's mode so a measurement arm (probe) can book
        its fills separately without touching the live record.

        Shared by the poll path and the fast impulse path (`_impulse_tick`) so
        the two cannot drift. A fill booked on one path and not the other is
        exactly how the risk guard loses track of the exposure it is guarding.
        """
        # The cross arm holds the RESERVED slot, not the main one. The model
        # arm enters at tau 60-210 and holds to settlement, covering the cross
        # arm's whole window, so sharing one slot let the model crowd it out of
        # 82% of the rounds it wanted -- 61% of them the OPPOSITE side.
        reserved, pool = pool_for_mode(mode or self.broker.mode)
        self.guard.on_open(r.slug, reserved=reserved, pool=pool)
        # The gate's ev/$ is quoted at the price it saw; this is the same
        # quantity at the price paid. `None` when the fill price is unusable
        # (never a guessed number -- the log would then be quietly wrong).
        ev_at_fill = None
        if fill.filled_px and 0.0 < fill.filled_px < 1.0 and 0.0 < sig.fair < 1.0:
            ev_at_fill = expected_value(sig.fair, fill.filled_px,
                                        fee_rate_for_price(fill.filled_px))
        self.store.conn.execute("UPDATE decisions SET acted=1 WHERE id=?", (did,))
        self.store.conn.commit()
        fid = self.store.insert("fills", {
            "decision_id": did, "slug": r.slug, "ts_ms": fill.ts_ms,
            "mode": mode or self.broker.mode, "side": fill.side,
            "requested_px": fill.requested_px, "filled_px": fill.filled_px,
            "shares": fill.shares, "stake": fill.stake, "fee_paid": fill.fee_paid,
            "order_id": fill.order_id, "raw": fill.raw,
            # Persisted so a restored fill can still be verified at settlement:
            # an unverified one is checked against the venue's position list by
            # token, and NULL would make that check answer "not held" every time.
            "token_id": token or getattr(fill, "token_id", None),
            # What the trade was worth at the price actually paid. The gate
            # prices it at the quote it saw; the fill lands a second or two
            # later. Measured on 51 live fills this gap costs 0.0132 per $1 --
            # about a third of the claimed edge, worse on 78% of fills -- so it
            # is recorded per fill instead of being assumed away.
            "ev_at_fill": ev_at_fill,
        })
        self.open_fills[fid] = (r.slug, fill)
        # implied_sigma inverts P(up), so it must always be fed the Up quote --
        # passing the chosen side's quote breaks the inversion when that side is
        # Down. A missing Up quote must not take down the tick after the fill
        # has already been booked and stored.
        try:
            iv = implied_sigma(up.price, spot, r.strike, tau)
        except (TypeError, ValueError):
            iv = None
        log.info("OPEN %s %s stake=$%.2f @%.3f | %s | mkt_iv=%s realised=%.5f "
                 "| ev/$ gate->fill %+.4f -> %s",
                 r.slug, fill.side, fill.stake, fill.filled_px, sig,
                 f"{iv:.5f}" if iv else "n/a", sigma, sig.ev_per_dollar,
                 f"{ev_at_fill:+.4f}" if ev_at_fill is not None else "n/a")

    async def _impulse_tick(self) -> None:
        """Impulse entry, evaluated on every price update instead of on the poll.

        Measured 2026-09-12 from this host: the venue answers in 5.3 ms (three
        round trips, 16 ms) while the poll loop looked at the price once every
        1002 ms. So the poll interval WAS the trigger-to-order latency: a move
        the websocket showed at t was acted on at the next poll, ~500 ms later
        on average and 1 s at worst, and the side being bought keeps drifting
        for seconds after the impulse. This evaluates the SAME statistic (a
        |window| bps move in the underlying) the moment the window closes and
        goes straight to the order.

        Free when it does not fire: no venue calls, no writes, no logging.
        """
        # This arm had NO enable flag until 2026-09-13, which is how it went
        # live by accident: it is called unconditionally from the poll and the
        # idle loop, and only `guard.check()` -- the MAIN arm's slot, almost
        # always occupied -- had been keeping it silent. Per-arm risk pools
        # removed that accident and it placed its first ever order within
        # hours. See config.impulse_enabled for why it measures negative at a
        # realistic fill.
        if not settings.impulse_enabled:
            return
        r = getattr(self, "_round", None)
        # NEVER `self.spot` here: that attribute is rewritten once per market as
        # the poll walks MARKETS, so by the time this websocket-clock path runs
        # it holds the LAST symbol's feed (ETHUSDT), not the feed of the round in
        # _round (BTCUSDT). Using it made the trigger fire on ETH's move and then
        # trade the BTC market, and made the take-limit fair meaningless.
        feed = getattr(self, "_primary_spot", None)
        if feed is None:
            feed = self.spots.get(MARKETS[0][1])
        ticks = getattr(feed, "ticks", None)
        if r is None or not ticks or r.slug in self._impulse_fired:
            return
        # Only the live round can be re-entered, so nothing older is worth
        # keeping -- the set would otherwise grow all day.
        self._impulse_fired.intersection_update({r.slug})
        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        if not (settings.min_tau_s <= tau <= settings.max_tau_s):
            return
        up, down = r.up, r.down
        if not up or not down or r.strike is None:
            return
        if not feed.is_fresh(settings.max_spot_age_s):
            return
        # Honour the order cooldown. _place_order sets it after a rejection that
        # survived every attempt, so that a persistent venue-side condition is
        # not hammered for the rest of the round -- at one attempt per second
        # that was rude; at one per websocket tick it would be an attack.
        if time.time() < self._order_cooldown_until:
            return
        move = impulse_bps(list(ticks), settings.impulse_window_s)
        side = impulse_side(move, settings.impulse_bps)
        if side is None:
            return
        # Band sanity only, from the last asks the poll saw (<= 1 s old). The
        # price that binds is the one that comes back with the order's own
        # quote, which _place_order fetches and reuses.
        px = self._asks[0] if side == "Up" else self._asks[1]
        if px is None or not (settings.impulse_min_price <= px
                              <= settings.impulse_max_price):
            return
        # The venue is ground truth on exposure: a fill this process never
        # booked (restart, unverified order) must not be doubled. Measured at
        # 5 ms, so unlike the poll's other work this is not worth caching.
        if self.broker.mode == "live" and self.book:
            # Only exposure the ledger cannot explain refuses this arm; another
            # arm holding the round does not. See _unbooked_round_tokens.
            unbooked = await self._unbooked_round_tokens(r, up, down)
            if unbooked is None or unbooked:
                return
        # Its OWN budget, not the main arm's. Sharing one meant whichever arm
        # fired first disabled the other for the round.
        allowed, why = self.guard.check_pool(
            r.slug, pool="impulse", max_open=settings.impulse_max_open)
        if not allowed:
            log.info("impulse %+.1fbps %s not taken: %s", move, side, why)
            return
        sig = Signal(side, px, px, 0.0, 0.0, 0.0, True,
                     f"impulse {move:+.1f}bps/{settings.impulse_window_s:.0f}s")
        spot = feed.price
        sigma = getattr(self, "_sigma", 0.0)
        mz = (abs(math.log(spot / r.strike)) / (sigma * math.sqrt(tau / 60.0))
              if spot and spot > 0 and sigma > 0 and tau > 0 else 0.0)
        stake = self._stake_for(sig, mz)
        if stake < settings.min_order_usdt:
            return
        # Claim the round BEFORE ordering. A rejected order then leaves the
        # round skipped rather than possibly bought twice, and _place_order
        # already retries a confirmed rejection `order_max_attempts` times.
        self._impulse_fired.add(r.slug)
        log.info("TRIGGER %+.1fbps/%ds %s %s tau=%.0fs px=%.3f stake=$%.2f",
                 move, int(settings.impulse_window_s), side, r.slug, tau, px, stake)
        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau,
            "side": side, "fair": sig.fair, "quote": sig.quote,
            "edge": sig.edge, "hurdle": sig.hurdle, "sigma_1m": sigma,
            "spot": spot, "oracle": None, "strike": r.strike,
            # `acted` is only promoted after the broker confirms the fill, so a
            # run of rejected orders cannot look like a run of trades.
            "stake": stake, "acted": 0,
            "reason": sig.reason,
        })
        # Which side of the CONTRACT we are buying and what we think it is worth.
        # Both executions below need these, and `px` is the ASK we read, not the
        # model's view -- they are different numbers and conflating them is how
        # the take-limit collapsed to 0.01 on 2026-09-12.
        fair_model = fair_value(spot, r.strike, tau, sigma) if sigma > 0 else px
        fair_side = fair_model if side == "Up" else 1.0 - fair_model
        token = up.token_id if side == "Up" else down.token_id

        if settings.impulse_execution == "maker" and not hasattr(
                self.broker, "buy_limit"):
            log.warning("maker execution requested but %s has no buy_limit; "
                        "taking this entry instead",
                        type(self.broker).__name__)
        if settings.impulse_execution == "maker" and hasattr(
                self.broker, "buy_limit"):
            maker_px = maker_limit_px(fair_side, px, settings.limit_ask_tick)
            if maker_px is None:
                log.info("maker %s skipped: fair %.3f vs ask %.3f leaves no "
                         "price to rest on", r.slug, fair_side, px)
                return
            log.info("MAKER post %s %s @%.2f (fair %.3f, ask %.3f, tau %.0fs) "
                     "stake=$%.2f", r.slug, side, maker_px, fair_side, px, tau,
                     stake)
            fill = await self.broker.buy_limit(
                slug=r.slug, side=side, token_id=token, limit_px=maker_px,
                stake=stake, wait_s=settings.impulse_maker_wait_s,
                fill_break=settings.limit_fill_break)
            if not fill:
                log.info("maker %s unfilled at %.2f after %.0fs", r.slug,
                         maker_px, settings.impulse_maker_wait_s)
                return
            await self._book_fill(r, sig, fill, did=did, up=up, spot=spot,
                                  sigma=sigma, tau=tau, token=token,
                                  mode="maker")
            return

        fill = await self._place_order(
            r, sig,
            # Under a taker limit this fair IS the price ceiling, so it has to be
            # a real probability rather than the venue's own quote. `px` is the
            # displayed price -- the number measured lagging its own book by up
            # to 0.40 during a move -- so pricing the limit off it would refuse
            # exactly the dislocations this arm exists to take (display 0.15,
            # book 0.60, digital 0.91: a 0.15 limit fills nothing, a 0.90 limit
            # fills at 0.60). The digital needs no new inputs: spot, strike, tau
            # and sigma are all already here.
            #
            # `take_px` is the FLOOR, and the impulse arm needs it more than any
            # other caller. `take_limit_px` computes max(fair - margin, take_px),
            # so with no floor the limit collapses whenever the digital fair is
            # far from the market -- exactly the case the impulse arm creates on
            # purpose, because it ignores the model. Measured live 2026-09-12:
            # a +9.8bps/2s Up trigger fired while spot was still ~14bps BELOW the
            # round's strike, so P(Up) ~ 0, the limit went out at max(0.01,
            # 0 - 0.04) = 0.01 against a 0.730 book, rested 20s, never filled,
            # and cost the round plus a 30s order pause. The cross arm already
            # passes its ask for this reason; the impulse arm is in the same
            # position -- it has no valuation to price the ceiling from, and its
            # own band (impulse_min/max_price) plus impulse_price_refusal are
            # what sanity-check the price. The `quote + tick` cap still applies,
            # so this can never bid above the ask it read.
            fair_up=fair_model,
            token=token, stake=stake,
            take_px=px,
            # This order came from the impulse gate, not the model's. In "both"
            # mode that is no longer implied by settings.entry_mode.
            impulse_order=True)
        if not fill:
            return
        await self._book_fill(r, sig, fill, did=did, up=up,
                              spot=spot, sigma=sigma, tau=tau, token=token,
                              # Its own LABEL, so settle releases the impulse
                              # pool rather than the main arm's slot.
                              # Tagged in paper too: the label says which ARM,
                              # not which broker, and check_pool/on_open would
                              # otherwise disagree about which budget it spends.
                              mode="impulse")

    def _ck(self, name: str, r=None):
        """A cross knob for ONE round.

        `r` is explicit because the arm now runs across every family in
        CROSS_TRADED and the knobs differ between them -- reading `self._round`
        here would price eth-updown-15m with BTC's range gate. It falls back to
        `self._round` only for the single-market call path.

        Unknown markets fall through to the global `settings.cross_<name>`, so
        widening MARKETS cannot silently change how BTC trades. See
        config.cross_overrides for the measured per-market spreads.
        """
        if r is None:
            r = getattr(self, "_round", None)
        return cross_knob(market_prefix(getattr(r, "slug", None)), name)

    def _round_range_bps(self, r) -> float | None:
        """How far this round has travelled, open -> now, in bps of the strike.

        Read from `self._ticks`, NOT `feed.ticks`: the feed deque is 4000 ticks,
        which at BTC's live ~114/s is 35 seconds and does not span a 5-minute
        round. `self._ticks` is appended once per poll and holds 2400 samples.

        Returns None when there is not enough history to judge -- callers must
        treat that as "no opinion" and NOT as a refusal, or a cold start would
        silently disable the arm.
        """
        if not r or not getattr(r, "strike", None):
            return None
        buf = (getattr(self, "_ticks", None) or {}).get(getattr(r, "symbol", "")) or []
        seen = [px for t_ms, px in buf if t_ms >= r.start_ms and px > 0]
        if len(seen) < 2:
            return None
        return 1e4 * (max(seen) - min(seen)) / r.strike

    def _train_is_fresh(self, r, thr_bps: float) -> bool:
        """Did the spot cross `thr_bps` from the strike JUST NOW?

        THE BUG THIS FIXES, live 2026-09-15 20:49:54. The train condition is a
        LEVEL -- |spot - strike| >= 10bps -- and a level has no anchor. On the
        first tick after a restart it fired on all three families at once, on a
        15m round whose strike was TEN MINUTES old with the spot already
        16-25bps out (btc +18.17, eth +16.33, bnb +25.18). It bought a stale,
        fully-repriced level at asks of 0.77-0.89, not a breakout. The
        measurement it was built from anchored at the FIRST quote to cross the
        threshold; the shipped code anchored at nothing.

        True only when a sample INSIDE the last `train_fresh_s` seconds was
        still BELOW the threshold -- the crossing is then provably recent.
        Fails CLOSED when there is not enough history to prove it, which is
        exactly the restart case that misfired.
        """
        if not r.strike or thr_bps <= 0:
            return False
        win_ms = int(self._ck("train_fresh_s", r) * 1000)
        if win_ms <= 0:
            return False
        buf = (getattr(self, "_ticks", None) or {}).get(
            getattr(r, "symbol", "")) or []
        now = int(time.time() * 1000)
        recent = [(t, px) for t, px in buf if t >= now - win_ms and px > 0]
        if not recent:
            return False
        oldest = min(recent, key=lambda s: s[0])[1]
        return abs(1e4 * (oldest - r.strike) / r.strike) < thr_bps

    def _train_round_ok(self, r) -> bool:
        """May the TRAIN trigger trade this round? (cross_train_5m_only)

        The trigger is time-boxed to "the first 4 minutes" and priced in the
        money, and BOTH are properties of a 300-SECOND round. On a 15m round
        `tau >= 60` is 14 of the 15 minutes, not the first four; and 10bps at
        tau 820 is ~1 sigma, not in the money -- the three live 15m legs filled
        at 0.66/0.73/0.74 against a stated 0.75-0.88 band, while every 5m leg
        filled in 0.79-0.88. Restricting to 5m makes the two properties the
        same property.

        An unreadable slug FAILS CLOSED: a trigger that overrides three gates
        should not act on a round whose length it cannot establish. This gates
        the TRAIN trigger only -- the cross arm still trades every family.
        """
        if not self._ck("train_5m_only", r):
            return True
        try:
            return int(str(r.slug).split("-")[2].rstrip("m")) * 60 == 300
        except (AttributeError, IndexError, ValueError):
            return False

    def _probe_fires(self, r) -> bool:
        """Quote a range-REFUSED crossing anyway, at a fixed tiny stake.

        The catch thesis says the venue walks to its post-cross price over ~1s
        and an early quote pays less -- but its decisive case (a jump-sized
        move, book running far past the quote) can never produce a natural
        fill to test that: the range gate refuses those crossings before the
        venue can answer. So this buys the answer with real orders, once per
        round, a few times a day, at the arm's NORMAL size -- no special
        stake, because what it measures must be what a gate raise would
        actually trade. Default OFF: it spends money to measure mechanics,
        not for edge. Rows are labelled "cross probe" in decisions.reason;
        everything else -- the guard, the caps, the reserved slot, the
        exposure checks -- is UNCHANGED.
        """
        if not self._ck("probe_enabled", r):
            return False
        day = time.strftime("%Y-%m-%d", time.gmtime())
        st = getattr(self, "_probe_state", None)
        if not isinstance(st, dict):
            st = self._probe_state = {"day": "", "n": 0, "slugs": set()}
        if st["day"] != day:
            st["day"], st["n"], st["slugs"] = day, 0, set()
        cap = int(self._ck("probe_max_per_day", r) or 0)
        if (cap > 0 and st["n"] >= cap) or r.slug in st["slugs"]:
            return False
        st["slugs"].add(r.slug)
        st["n"] += 1
        log.info("cross probe %s: quoting a range-refused crossing at the "
                 "arm's normal size (probe %d/%d today)", r.slug, st["n"], cap)
        return True

    def _cross_refuse(self, r, side, reason: str, *, tau: float = 0.0,
                      spot=None, stake: float = 0.0, sigma=None) -> None:
        """Record a cross trigger that was DETECTED and then refused.

        Two problems this fixes, both found on 2026-09-14 while reconstructing
        seven rounds that looked like missed crossings:

        * SILENCE. Every refusal after `crossed_side` returns a side used to
          exit with no decision row -- and most with no log line either. In the
          database that is indistinguishable from "no crossing happened", so
          the only way to tell a refusal from a miss was to replay the venue's
          strike against 1s klines. A detected-then-refused trigger is exactly
          the thing worth seeing, because it is rare and it is a choice.
        * SPAM. The one path that did log wrote on every evaluation. Now that
          the arm runs on the websocket clock (~114/s) that produced 164 lines
          for 11 rounds. Deduped per (round, reason), so a refusal that persists
          for a whole round is one line and one row.
        """
        # Keyed BY SLUG, one entry per live round. It used to hold a single
        # {"slug", "seen"} pair and reset whenever the slug changed, which was
        # right for one family and defeats the dedupe entirely with six: each
        # market's refusal resets the previous market's memory, so on the
        # websocket clock (~114/s) every refusal logs and inserts a row on every
        # evaluation -- the exact spam this method was written to stop.
        #
        # getattr, not attribute access: the fixtures build a Bot with __new__
        # and never run __init__, the same reason `_ticks` is looked up that way.
        seen = getattr(self, "_cross_refused", None)
        if not isinstance(seen, dict) or "seen" in seen:
            seen = self._cross_refused = {}         # drop the old single-slug shape
        live = {c["round"].slug
                for c in (getattr(self, "_mkt", None) or {}).values()
                if c.get("round") is not None} | {r.slug}
        for k in [k for k in seen if k not in live]:
            del seen[k]
        bucket = seen.setdefault(r.slug, set())
        if reason in bucket:
            return
        bucket.add(reason)
        log.info("cross %s %s not taken: %s", r.slug, side, reason)
        # Record what the book showed at the refusal. A refusal is the only
        # clean read of "the venue had not repriced yet" that arrives without
        # an order, and the row used to carry quote=None -- which threw that
        # read away. Meter the book's path after it too (see
        # _schedule_cross_meter): the catch a faster trigger would buy is
        # exactly how fast the book leaves this price.
        quote = None
        with contextlib.suppress(Exception):
            wsb = getattr(self, "ws_book", None)
            if wsb is not None:
                top = wsb.best(r.market_id, settings.ws_book_max_age_s)
                quote = top.ask(side) if top is not None else None
        self._schedule_cross_meter(r, side, "refuse",
                                   label=("train" if "train" in (reason or "")
                                          else "cross"))
        with contextlib.suppress(Exception):
            self.store.insert("decisions", {
                "slug": r.slug, "ts_ms": int(time.time() * 1000), "tau_s": tau,
                "side": side, "fair": None, "quote": quote, "edge": 0.0,
                "hurdle": 0.0,
                # the REFUSED market's sigma; `self._sigma` is the primary's,
                # which would mislabel every eth/bnb row.
                "sigma_1m": (sigma if sigma is not None
                             else getattr(self, "_sigma", 0.0)),
                "spot": spot, "oracle": None, "strike": r.strike,
                "stake": stake, "acted": 0, "reason": f"cross refused: {reason}",
            })

    def _schedule_cross_meter(self, r, side, kind: str = "trigger",
                              label: str = "cross") -> None:
        """Sample the book after a trigger, once per (round, side, kind).

        `kind` separates a FIRED trigger from a refused one. Without it a fire
        whose round had been refused a few hundred ms earlier was deduped away
        -- the only meter on the log belonged to the refusal, which
        mis-anchored the 09:44 timing reads by ~290ms until the venue's own
        order records exposed it (2026-09-15). A fire always gets its own.

        The trigger path records what the book showed at the decision; this
        records what it did NEXT. Log-only: it cannot place, price or change
        an order, and a failure here must never touch the arm.
        """
        if getattr(self, "ws_book", None) is None:
            return
        seen = getattr(self, "_meter_seen", None)
        if not isinstance(seen, set):
            seen = self._meter_seen = set()
        if (r.slug, side, kind, label) in seen:
            return
        if len(seen) > 400:                 # ~a day of triggers; keep it bounded
            seen.clear()
        seen.add((r.slug, side, kind, label))
        try:
            task = asyncio.create_task(self._cross_meter(r, side, kind, label))
        except RuntimeError:                # no running loop (sync/fixture use)
            return
        tasks = getattr(self, "_meter_tasks", None)
        if tasks is None:
            tasks = self._meter_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _cross_meter(self, r, side, kind: str = "trigger",
                           label: str = "cross") -> None:
        """The book after a trigger, at 50ms resolution where the race lives.

        250/500/1000ms marks cannot tell a reprice at +330 from one at +540,
        and that band is where a client-side saving would or would not flip a
        fire: measured 2026-09-15 from the venue's own records, the order is
        created ~185ms after the decision and the FOK answers ~425ms later,
        against runs that finish in <=600ms. The `fine` line samples the
        window; the legacy line is kept verbatim for tools/cross_catch_report.
        """
        wsb = getattr(self, "ws_book", None)
        if wsb is None:
            return
        coarse, fine, prev = {}, [], 0.0
        try:
            for mark in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40,
                         0.45, 0.50, 0.60, 0.75, 1.00):
                await asyncio.sleep(mark - prev)
                prev = mark
                top = wsb.best(r.market_id, settings.ws_book_max_age_s)
                q = top.ask(side) if top is not None else None
                txt = "n/a" if q is None else f"{q:.3f}"
                fine.append(f"{int(round(mark * 100)):02d}={txt}")
                if mark in (0.25, 0.50, 1.00):
                    coarse[mark] = txt
            log.info("%s meter %s %s: +0.25s=%s +0.50s=%s +1.00s=%s [%s]",
                     label, r.slug, side, coarse[0.25], coarse[0.50],
                     coarse[1.00], kind)
            log.info("%s fine %s %s %s: %s", label, r.slug, side, kind,
                     " ".join(fine))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _open_cross_stake(self) -> float:
        """Total stake in cross positions that have not settled yet.

        Read from the LEDGER, not tracked in memory: a restart must not reset it
        to zero, which is precisely how an exposure cap silently stops capping.
        Cached for a second because the arm asks once per family per poll.

        On a query failure this returns the LAST KNOWN value rather than 0.0 --
        unknown exposure must never read as no exposure.
        """
        now = time.time()
        if now - getattr(self, "_open_stake_at", 0.0) < 1.0:
            # + in-flight on the CACHED path too. Returning the bare cached
            # figure here defeated the whole reservation: two triggers inside
            # the same second is precisely the case it exists for.
            return getattr(self, "_open_stake", 0.0) + self._inflight_stake()
        try:
            rows = self.store.query(
                "SELECT COALESCE(SUM(f.stake), 0.0) AS s FROM fills f "
                "LEFT JOIN settlements st ON st.fill_id = f.id "
                "WHERE st.id IS NULL AND f.mode = 'cross'")
            val = float(rows[0]["s"] or 0.0) if rows else 0.0
        except Exception:                                   # pragma: no cover
            return getattr(self, "_open_stake", 0.0) + self._inflight_stake()
        self._open_stake, self._open_stake_at = val, now
        return val + self._inflight_stake()

    def _inflight_stake(self) -> float:
        """Stake committed to orders that have not booked a fill yet.

        Orders are placed off the poll loop, so without this two triggers could
        each pass a 30% exposure check while neither had reached the ledger,
        and land at 60% together. NOT cached -- the cached ledger figure is a
        second old by construction and this is the part that changes between
        two triggers in the same poll.
        """
        return float(getattr(self, "_pending_stake", 0.0) or 0.0)

    async def _prefetch_cross_quote(self, r, up, down, feed, tau, ctx):
        """Keep a quote WARM for the side the next crossing would buy.

        The fire path pays a quote round-trip (measured 40-65ms) between the
        decision and the order, inside a race the venue settles in ~250ms:
        measured 2026-09-15 against the venue's own order records, decision ->
        venue-accepted is 169-186ms, and the book is already past the ceiling
        at +250ms (eth 0.46 -> 0.63, btc 0.30 -> 0.66). Two of that morning's
        four failures missed by less than this round-trip (07:59 btc by
        ~20ms, 09:09 eth by ~45ms).

        The side is knowable BEFORE the crossing -- below the strike the next
        cross is Up, above it Down -- so the quote is fetched here, while the
        family sits in its tradeable window, and consumed ONCE by the first
        attempt (`_take_prefetched_quote`, used in _place_order). Any doubt
        (stale, stake mismatch, fetch failure) falls back to the synchronous
        fetch: this can only ever be faster, never a different trade.

        The stake mirrors the fire's sizing so the quote's amountIn matches
        the order's; if the two ever drift apart the mismatch is caught at
        use time rather than trading a size the arm did not choose.
        """
        broker = getattr(self, "broker", None)
        if broker is None or getattr(broker, "mode", None) != "live":
            return None
        get_quote = getattr(broker, "get_quote", None)
        spot, strike = getattr(feed, "price", None), getattr(r, "strike", None)
        if get_quote is None or not spot or not strike or spot == strike:
            return None
        side = "Up" if spot < strike else "Down"
        token = up.token_id if side == "Up" else down.token_id
        if not token:
            return None
        wsb = getattr(self, "ws_book", None)
        top = (wsb.best(r.market_id, settings.ws_book_max_age_s)
               if wsb is not None else None)
        ask = top.ask(side) if top is not None else None
        if ask is None or ask <= 0:
            return None
        stake = self._ck("stake_usdt", r)
        if stake is None:
            sigma = (ctx.get("sigma") if ctx else None) \
                or getattr(self, "_sigma", 0.0) or 0.0
            mz = (abs(math.log(spot / strike))
                  / (sigma * math.sqrt(tau / 60.0))
                  if tau > 0 and sigma > 0 else 0.0)
            stake = self._stake_for(
                Signal(side, ask, ask, 0.0, 0.0, 0.0, True, "cross"), mz)
            stake = round(stake * self._ck("stake_mult", r), 2)
            if settings.stake_cap_usdt > 0:
                stake = min(stake, settings.stake_cap_usdt)
        stake = float(stake)
        if stake < settings.min_order_usdt:
            return None
        cache = getattr(self, "_quote_pre", None)
        if not isinstance(cache, dict):
            cache = self._quote_pre = {}
        busy = getattr(self, "_quote_pre_busy", None)
        if not isinstance(busy, set):
            busy = self._quote_pre_busy = set()
        now = time.monotonic()
        for old, entry in list(cache.items()):
            if now - entry[0] > 60.0:
                cache.pop(old, None)
        entry = cache.get(token)
        if entry is not None and now - entry[0] < _QUOTE_PREFETCH_REFRESH_S:
            return None
        if token in busy:
            return None
        busy.add(token)

        async def _warm() -> None:
            try:
                q = await get_quote(token, stake)
                if q:
                    cache[token] = (time.monotonic(), stake, q)
            except Exception as e:                      # noqa: BLE001
                log.debug("quote warm-up failed for %s: %s", r.slug, e)
            finally:
                busy.discard(token)

        task = asyncio.create_task(_warm())
        tasks = getattr(self, "_quote_pre_tasks", None)
        if not isinstance(tasks, set):
            tasks = self._quote_pre_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    def _take_prefetched_quote(self, token: str, stake: float) -> dict | None:
        """Pop the warm quote for this token when it is fresh and exact.

        One shot: the attempt that consumes it has spent it, so a later
        attempt can never trade on a price the first one already used.
        """
        cache = getattr(self, "_quote_pre", None)
        if not isinstance(cache, dict):
            return None
        entry = cache.pop(token, None)
        if entry is None:
            return None
        ts, q_stake, q = entry
        age = time.monotonic() - ts
        if age > _QUOTE_PREFETCH_MAX_AGE_S:
            return None
        if round(float(q_stake), 2) != round(float(stake), 2):
            return None
        # The caller's marker logs this: the venue anchors the FOK bound to
        # THIS quote's averagePrice (verified 2026-09-15 -- the order record's
        # `price` equals the submitted quote's avg to 15 digits), so the age
        # is part of the bound's meaning.
        self._warm_age_s = age
        return q

    async def _cross_all(self) -> None:
        """Run the cross arm over every family in CROSS_TRADED.

        One family raising must not stop the others -- a six-market arm whose
        whole loop dies because eth-updown-15m returned a malformed round is
        strictly worse than the single-market one it replaced.

        With no per-market context (fixtures, or a poll that recorded nothing)
        this falls back to the single-market path so the arm behaves exactly as
        it did before it was widened.
        """
        mkt = getattr(self, "_mkt", None)
        if not mkt:
            await self._cross_tick()
            return
        for prefix, ctx in list(mkt.items()):
            try:
                await self._cross_tick(ctx)
            except Exception as e:                      # pragma: no cover
                log.exception("cross tick failed for %s: %s: %s",
                              prefix, type(e).__name__, e)

    async def _cross_tick(self, ctx=None) -> None:
        """Buy the side the spot JUST crossed into, while the venue undershoots.

        See config.cross_* for the measurement and for why it is thin. It is
        live to buy a sample, not because it is proven: as of 2026-09-13 it has
        no settled live fill of its own, and every number below it comes from
        replay. Sizing follows the main arm (see cross_stake_usdt), so it is no
        longer the flat $1 the earlier note described.

        Shaped like _impulse_tick in the parts that matter -- one claim per
        round, claim before ordering, and NOTHING (no venue call at all) when
        it does not fire -- but deliberately NOT in its gating:

          * the guard call is check_reserved, not check. This arm has a slot of
            its own so the main arm cannot crowd it out.
          * the venue test blocks only on exposure the LEDGER cannot explain
            (see _unbooked_round_tokens), not on any held token. Blocking on
            any token refused 52% of its triggers, all of them to the main arm.
        """
        # getattr: tick() now calls this directly (via _cross_all) rather than
        # only from _record, and fixtures build Bot via __new__ with no `mode`.
        if getattr(self, "mode", None) not in ("live", "paper"):
            return
        # `ctx` is one family's state, built by _record and dispatched by
        # _cross_all. None means the single-market path (the primary), which is
        # kept so the arm stays directly callable and testable.
        if ctx is None:
            r = getattr(self, "_round", None)
            # NOT self.spot: the poll used to rewrite it once per market as it
            # walked MARKETS, so on the websocket clock it held the LAST
            # symbol's feed.
            feed = getattr(self, "_primary_spot", None)
            if feed is None:
                feed = self.spots.get(MARKETS[0][1])
            primary = True
        else:
            r, feed = ctx["round"], ctx["feed"]
            primary = ctx.get("primary", False)
        asks = (getattr(self, "_asks", (None, None)) if ctx is None
                else ctx.get("asks", (None, None)))
        if not self._ck("enabled", r):
            return
        ticks = getattr(feed, "ticks", None)
        if r is None or not ticks or r.strike is None:
            return
        # Entries already taken in THIS round. Several are allowed: consecutive
        # crosses alternate sides, so a round oscillating around the strike is
        # naturally hedged rather than doubled, and taking every one measured
        # +$31.37 against +$14.16 for the first alone (see config).
        if not isinstance(self._cross_fired, dict):
            self._cross_fired = {}
        # getattr, not a bare attribute: fixtures build Bot via __new__, which
        # skips __init__ (the same trap `_cross_refused` hit on 2026-09-14).
        if not isinstance(getattr(self, "_cross_side", None), dict):
            self._cross_side = {}
        if self._cross_fired.get(r.slug, 0) >= self._ck("max_per_round", r):
            return
        # Keep every LIVE round, not just this one. Pruning to `r.slug` was
        # correct with a single family and is a bug with six: each family's call
        # would wipe the others' per-round state, so every market would forget
        # it had already fired and the per-round cap would never bind.
        live = {c["round"].slug for c in (getattr(self, "_mkt", None) or {}).values()
                if c.get("round") is not None} | {r.slug}
        self._cross_fired = {k: v for k, v in self._cross_fired.items() if k in live}
        self._cross_side = {k: v for k, v in self._cross_side.items() if k in live}
        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        # ---- TRAIN TRIGGER (config.cross_train_*) --------------------------
        # Fires on DISTANCE from the strike instead of a crossing, so it lives
        # OUTSIDE this arm's tau window (which is 5-60s) rather than inside it.
        # It is an explicit override of three gates -- max_depth_bps,
        # max_round_range_bps and cross_max_fill_px -- because a >=10bps entry
        # is deep, implies a >=10bps round travel, and prices near 0.80. Open
        # by operator decision 2026-09-15; see config for the measurement.
        # Everything that bounds RISK is untouched and shared with the cross
        # arm: the guard, the reserved slot, the exposure caps, the daily cap,
        # the reentry gap, the cooldown and the reversal rule.
        train = False
        side = None
        tmax = self._ck("train_max_tau_s", r)
        if self._ck("train_enabled", r) and self._train_round_ok(r) \
                and r.strike \
                and tau >= self._ck("train_min_tau_s", r) \
                and (tmax <= 0 or tau <= tmax) \
                and feed.is_fresh(settings.max_spot_age_s):
            dist_bps = 1e4 * (feed.price - r.strike) / r.strike
            thr = self._ck("train_bps", r)
            if abs(dist_bps) >= thr and self._train_is_fresh(r, thr):
                # BURST GUARD, 2026-09-15 21:01. See cross_train_cooldown_s.
                #
                # The trigger is a LEVEL (`|spot-strike| >= thr`), so the
                # moment btc/eth/bnb move together it goes true for EVERY
                # family -- and because a 5m round and its 15m sibling share a
                # strike, those two are the SAME bet. Measured: eight entries
                # in 241s, all Down, as eth-5m+eth-15m (one tick), then
                # bnb-5m+bnb-15m (+20s), then btc-5m+btc-15m (+34s) -- one view
                # expressed six ways. A CROSSING cannot cluster like this; a
                # distance condition always will. The exposure caps do not
                # catch it: they count positions and stake, not correlation.
                cd = self._ck("train_cooldown_s", r)
                last_ms = getattr(self, "_train_last_ms", 0)
                if cd > 0 and last_ms and (now_ms - last_ms) < cd * 1000.0:
                    # NO `return` here. This guard suppresses the TRAIN entry,
                    # not the family's whole tick: returning would also swallow
                    # a genuine CROSS in the same family inside the 5-60s
                    # window, i.e. a second signal killed by the first one's
                    # burst guard. Falling through lets the ordinary window gate
                    # and the crossing path decide.
                    #
                    # SELF-DIAGNOSING. A refusal was logged at 2026-09-15
                    # 21:13:16 that sat 124.8s after the only visible entry --
                    # PAST this window -- and it could not be reconstructed from
                    # the rows. Two processes and `_ck` returning 0 were both
                    # ruled out (one stats line per minute; cross_knob falls
                    # through correctly), so log the age itself, once per round,
                    # and let the next occurrence name its own number.
                    dbg = getattr(self, "_train_cd_dbg", None)
                    if not isinstance(dbg, set):
                        dbg = self._train_cd_dbg = set()
                    if len(dbg) > 400:
                        dbg.clear()
                    if r.slug not in dbg:
                        dbg.add(r.slug)
                        log.info("train cooldown HIT %s: last entry %.1fs ago "
                                 "(cooldown %.0fs), tau %.0fs", r.slug,
                                 (now_ms - last_ms) / 1000.0, cd, tau)
                    self._cross_refuse(
                        r, "Up" if dist_bps > 0 else "Down",
                        f"train cooldown {cd:.0f}s: one entry per burst",
                        tau=tau, spot=feed.price)
                else:
                    # Stamped on the SIGNAL, before any later gate, so a refused
                    # order cannot let the same move be re-attempted family by
                    # family -- the next one is what would rebuild the burst.
                    self._train_last_ms = now_ms
                    side = "Up" if dist_bps > 0 else "Down"
                    train = True
        if not train and not (self._ck("min_tau_s", r) <= tau
                              <= self._ck("max_tau_s", r)):
            return
        # RE-ENTRY GAP. Without it the websocket clock (~114/s) takes the same
        # crossing dozens of times; the substrate this arm's multi-entry EV was
        # measured on was sampled at 1s and could never contain such a pair.
        # See config.cross_reentry_gap_s. Checked before any venue call so a
        # duplicate costs nothing.
        gap = self._ck("reentry_gap_s", r)
        if gap > 0:
            last = (getattr(self, "_cross_last_ms", None) or {}).get(r.slug)
            if last is not None and (now_ms - last) < gap * 1000.0:
                return
        up, down = r.up, r.down
        if not up or not down:
            return
        if not feed.is_fresh(settings.max_spot_age_s):
            return
        if time.time() < self._order_cooldown_until:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
        if day != self._cross_day:
            self._cross_day, self._cross_count = day, 0
        # 0 = OFF, the repo's convention for every other limit (daily_loss_cap,
        # max_consecutive_losses, stake_cap_usdt). Written as `cap > 0 and ...`
        # rather than a bare `>=`: at cap 0 a bare compare reads 0 >= 0 as
        # "budget exhausted" and silently disables the arm outright, which is
        # the exact opposite of what setting it to 0 is meant to express.
        cap_per_day = self._ck("max_per_day", r)
        if cap_per_day > 0 and self._cross_count >= cap_per_day:
            return
        if not train:
            side = crossed_side(list(ticks), r.strike, self._ck("look_s", r))
        if side is None:
            # No crossing this tick: keep a quote WARM for the side the next
            # crossing would buy (see _prefetch_cross_quote). This is the
            # only point that is in the tradeable window, live, fresh and not
            # already spent -- a fire from here is plausible.
            await self._prefetch_cross_quote(r, up, down, feed, tau, ctx)
            return
        # WHIPSAW. Stamped on the CROSSING, before any gate, because that is the
        # hole `cross_allow_reversal` leaves: it arms on the ENTRY, so a crossing
        # the arm DECLINED leaves it unarmed and the opposite leg of the same
        # oscillation walks straight in.
        #
        # LIVE 2026-09-15 00:59 on bnb-updown-15m -- Binance 1s klines confirm
        # both legs are real trades, not a bad tick:
        #
        #   00:59:21  Up   DECLINED  only 2 tick(s) through the strike, floor 5
        #   00:59:24  Down TAKEN     9 ticks through the strike, passes
        #   00:59:38  spot back above the strike; the round settles Up
        #
        # bnb-updown-5m saw the identical move 70ms earlier, took the Up (its
        # strike sat 7c lower, so the same move cleared the floor) and was then
        # refused the Down BY the reversal guard. Depth is measured from each
        # round's own strike, so inside a chop it is noise that decides which
        # family is allowed to trade.
        #
        # Measured over 211 crossings that pass every gate (btc corpus, eth and
        # bnb from the live DB), edge = win - ask:
        #
        #     FLIPPED within 20s    n= 14  win 0.643  ask 0.650  edge -0.0071
        #     clean                 n=197  win 0.863  ask 0.686  edge +0.1771
        #
        # Consistent in sign on both families with data (btc -0.035 vs +0.197,
        # eth -0.047 vs +0.076). A flipped crossing has no edge at all, never
        # mind enough to clear the limit buffer. It is 6.6% of crossings.
        fb = self._ck("flip_block_s", r)
        if not train and fb > 0:
            fl = getattr(self, "_cross_flip", None)
            if not isinstance(fl, dict):
                fl = self._cross_flip = {}
            prev = fl.get(r.slug)
            seen_ms = int(time.time() * 1000)
            if prev and prev[0] != side and (seen_ms - prev[1]) <= fb * 1000.0:
                fl[r.slug] = (side, seen_ms)
                self._cross_refuse(
                    r, side,
                    f"crossing flipped from {prev[0]} "
                    f"{(seen_ms - prev[1]) / 1000.0:.1f}s ago, window is {fb:.0f}s",
                    tau=r.tau_s(now_ms), spot=None, sigma=None)
                return
            fl[r.slug] = (side, seen_ms)
            self._cross_flip = {k: v for k, v in fl.items() if k in live}
        # A crossing BACK through the strike is not a hedge on what we already
        # hold. With the first position sunk the second leg is worth taking iff
        # it is +EV on its own, and measured it is not: it wins 0.574 against
        # 0.714 for a first entry (31 days of real strikes, day-t -3.05), and
        # its growth is negative under every pricing tried. Same-side repeats
        # are a different animal and stay allowed -- they are most of what the
        # per-round cap buys and they are profitable. See
        # config.cross_allow_reversal.
        #
        # In-process state, so a restart mid-round forgets the side. Left that
        # way deliberately: `open_fills` holds a Fill, which carries no `mode`,
        # so reading the side back would mean re-querying the DB per tick on
        # the websocket clock, and blocking on any held token is exactly the
        # arms-independence rule this arm exists outside of. The window is one
        # round and the observed failure (two entries 22s apart) is in-process.
        if not self._ck("allow_reversal", r):
            held = self._cross_side.get(r.slug)
            if held is not None and held != side:
                self._cross_refuse(
                    r, side,
                    f"reversal of the {held} entry already taken this round",
                    tau=tau, spot=feed.price)
                return
        # How far through the strike we already are. A cross that is 1-2 bps
        # deep has been seen and repriced (that cell measures -0.19/$1); the
        # edge lives in the nick the venue has not registered yet.
        spot_now = feed.price
        # THIS family's sigma. `self._sigma` is the primary's, so before the
        # widening it was the same object; with six families it would size
        # eth/bnb's moneyness tilt off BTC's volatility and label every
        # recorded row with it.
        sigma_now = (ctx.get("sigma") if ctx else getattr(self, "_sigma", 0.0))
        if not sigma_now:
            sigma_now = getattr(self, "_sigma", 0.0) or 0.0
        depth_bps = 1e4 * abs(spot_now - r.strike) / r.strike if r.strike else 1e9
        # MINIMUM depth, in TICKS. bps is the wrong unit for a floor: the same
        # $0.01 is 0.0013 bps on BTC and 0.1377 on BNB, so BNB's median trigger
        # was ONE tick -- the last decimal flipping. See config.
        min_ticks = self._ck("min_depth_ticks", r)
        if not train and min_ticks > 0:
            tick = self._ck("tick_size", r) or 0.01
            if abs(spot_now - r.strike) < min_ticks * tick - 1e-12:
                self._cross_refuse(
                    r, side,
                    f"only {abs(spot_now - r.strike) / tick:.0f} tick(s) through "
                    f"the strike, floor is {min_ticks:.0f}",
                    tau=tau, spot=spot_now, sigma=sigma_now)
                return
        if not train and depth_bps > self._ck("max_depth_bps", r):
            self._cross_refuse(r, side,
                               f"{depth_bps:.2f}bps through the strike, gate is "
                               f"{self._ck('max_depth_bps'):.2f}",
                               tau=tau, spot=spot_now, sigma=sigma_now)
            return
        # How far this round has ALREADY travelled, open -> now. A round that
        # has swung wide is one where a late crossing reverses again; a calm
        # one is where it sticks (see config.cross_max_round_range_bps).
        #
        # Read from `self._ticks`, NOT `feed.ticks`: the feed's deque is 4000
        # ticks, which at BTC's live ~114/s is 35 seconds -- it does not span a
        # 5-minute round. `self._ticks` is appended once per poll and holds
        # 2400 samples, so it does.
        rng_gate = self._ck("max_round_range_bps", r)
        probe = False
        if not train and rng_gate > 0:
            rng_bps = self._round_range_bps(r)
            if rng_bps is not None:
                if rng_bps > rng_gate:
                    if self._probe_fires(r):
                        probe = True        # flag-off experiment; see config
                    else:
                        self._cross_refuse(
                            r, side,
                            f"round already moved {rng_bps:.1f}bps, gate is "
                            f"{rng_gate:.1f}",
                            tau=tau, spot=spot_now, sigma=sigma_now)
                        return
        # The price that binds is the order's own quote; this is band sanity on
        # the last asks the poll saw. The ceiling is what makes the arm pay --
        # above it the favourite is overpriced and the cell measures negative.
        # Read the WEBSOCKET book directly, not self._asks. This arm runs on
        # the tick clock while _record refreshes self._asks only once per poll,
        # so going through it would throw away exactly the resolution the feed
        # was added for -- and the venue completes 66% of its post-cross
        # repricing inside that one second.
        wsb = getattr(self, "ws_book", None)
        top = (wsb.best(r.market_id, settings.ws_book_max_age_s)
               if wsb is not None else None)
        if top is not None:
            ask = top.ask(side)
            depth_usd = top.ask_dollars(side)
            # How far the book has ALREADY moved since the last poll saw it.
            # MEASURED 2026-09-13 on 163 fill-aware triggers, split by this:
            #     run <= 0.00   n=29  win 0.931  EV/$ +0.5436  day-t 4.63
            #     run 0.00-0.05 n=44  win 0.750  EV/$ +0.0596  day-t 1.60
            #     run 0.05-0.15 n=40  win 0.725  EV/$ +0.2028  day-t 2.29
            #     run 0.15+     n=50  win 0.740  EV/$ +0.0584  day-t 1.86
            # Catching the book BEFORE it reprices is worth 3x the arm's
            # average -- which is the arm's stated premise ("the nick the
            # venue has not registered yet") finally measurable.
            #
            # Recorded, NOT gated on. That split is n=29 out of a post-hoc
            # 4-way cut measured at 1s resolution, and the state it selects is
            # sub-second; gating on it from that evidence would be fitting.
            # The websocket samples at ~250ms, so a day of live triggers
            # measures it properly and then the gate is a decision, not a bet.
            stale_ref = asks[0] if side == "Up" else asks[1]
            book_run = (ask - stale_ref) if stale_ref is not None else None
        elif primary:
            # `asks`, not `self._asks`: with six families in flight the
            # attribute holds the PRIMARY's book, so a non-primary market would
            # have priced its order off BTC's ask.
            ask = asks[0] if side == "Up" else asks[1]
            depth_usd = None                    # unknown: do not cap on it
            book_run = None
        else:
            # OFF-PRIMARY THERE IS NO FALLBACK. Only the primary fetches the
            # REST book (it is the one that costs SAPI quota), and its round is
            # the only one refetched every poll -- so for the other families the
            # displayed price behind `asks` can be a whole round old. Pricing a
            # live order off that is how a stale book becomes a filled trade, so
            # a stale websocket means no trade rather than a guessed one.
            self._cross_refuse(r, side, "no fresh websocket book off-primary",
                               tau=tau, spot=spot_now, sigma=sigma_now)
            return
        if ask is None or not (self._ck("min_price", r) <= ask
                               <= self._ck("max_price", r)):
            self._cross_refuse(
                r, side,
                ("no ask on the book" if ask is None else
                 f"ask {ask:.3f} outside [{self._ck('min_price'):.2f},"
                 f"{self._ck('max_price'):.2f}]"),
                tau=tau, spot=spot_now)
            return
        # TRAIN price gate. Placed HERE, after `ask` is FINAL, not on the
        # websocket branch: inside `if top is not None` it was skipped entirely
        # whenever the ws book was stale and the primary fell back to `asks`,
        # which is exactly when a gate is most needed. The reason is CONSTANT so
        # `_cross_refuse`'s per-(round, reason) dedupe collapses it to one row --
        # with the ask in the text every distinct cent made a new row (7 rows for
        # one round on 2026-09-15, the per-tick-refusal trap this file records).
        if train:
            tmax = self._ck("train_max_ask", r)
            if tmax > 0 and ask > tmax:
                self._cross_refuse(
                    r, side, f"train ask above the gate {tmax:.3f}",
                    tau=tau, spot=spot_now, sigma=sigma_now)
                return
        # Size like the main arm unless a flat stake is configured: a flat $1
        # cannot compound, so the arm's contribution shrinks as a share of the
        # book while the bankroll grows.
        #
        # The tilt needs |z| -- how decided the round already is in units of the
        # vol still to come. A cross sits AT the strike by construction (the
        # depth gate allows <=0.5bps), so z is small and the tilt usually pins
        # to tilt_max. Measured at sigma_1m=2bps/min, tilt_reference_z=0.3:
        #     depth 0.01-0.10bps -> t=2.00 at every tau in the window
        #     depth 0.25bps      -> t=0.98 (tau 10s) .. 2.00 (tau 60s)
        #     depth 0.50bps      -> t=0.50 (tau 10s) .. 1.20 (tau 60s)
        # So a shallow cross -- the one the arm is built for -- stakes 2x base,
        # and a cross already half a bp through the strike stakes down. Note
        # tilt_factor returns 1.0 (NOT tilt_max) when z is exactly 0, which is
        # its "no information" path, so a zero sigma or zero tau sizes neutral
        # rather than doubling on a reading we do not have.
        if self._ck("stake_usdt", r) is not None:
            stake = self._ck("stake_usdt", r)
        else:
            mz = (abs(math.log(spot_now / r.strike))
                  / (sigma_now * math.sqrt(tau / 60.0))
                  if spot_now > 0 and r.strike > 0 and tau > 0 and sigma_now > 0
                  else 0.0)
            stake = self._stake_for(
                Signal(side, ask, ask, 0.0, 0.0, 0.0, True, "cross"), mz)
            # This arm only: see config.cross_stake_mult for the sweep. The
            # cap still binds -- the multiplier scales the arm, it does not
            # exempt it from stake_cap_usdt.
            stake = round(stake * self._ck("stake_mult", r), 2)
            if settings.stake_cap_usdt > 0:      # 0 = no cap, see config
                stake = min(stake, settings.stake_cap_usdt)
        # The probe gets NO special sizing: it is the arm's normal order
        # (formula, tilt, caps), so what it measures is what a gate raise
        # would actually trade. Only the daily count is probe-specific.
        # Size to what is actually resting at the touch. On 2026-09-13 the arm
        # asked for $1.18 and the venue filled $0.90 of it, because nothing
        # checked whether the size existed -- the REST poll carries no depth at
        # all. A partial fill is not free: it books the full intended exposure
        # in the decision row and the guard, then holds less of it.
        # Price the WHOLE order against the book, not the touch. The touch is
        # routinely a few cents wide -- a live Down book showed $0.10 at the
        # best price and $2.61 one level behind it -- so an order gated on the
        # touch is gated on almost nothing. That is how a cross gated at 0.760
        # filled at 0.9008 on 2026-09-13: $1.09 swept through to the limit.
        if top is not None:
            sweep_px, fillable = top.sweep(side, stake)
            if fillable < settings.min_order_usdt or sweep_px <= 0:
                log.info("cross %s %s not taken: book holds only $%.2f "
                         "(wanted $%.2f)", r.slug, side, fillable, stake)
                return
            if fillable < stake:
                log.info("cross %s %s: sizing down $%.2f -> $%.2f (book depth)",
                         r.slug, side, stake, fillable)
                stake = round(fillable, 2)
                sweep_px, _ = top.sweep(side, stake)
            # The gate applies to what we will PAY, not to the touch we happen
            # to see. cross_max_price is the load-bearing gate of this arm; a
            # sweep price outside it is a trade the measurement never blessed.
            if not (self._ck("min_price", r) <= sweep_px
                    <= self._ck("max_price", r)):
                log.info("cross %s %s not taken: sweep price %.3f for $%.2f "
                         "is outside %.2f-%.2f (touch was %.3f)", r.slug, side,
                         sweep_px, stake, self._ck("min_price", r),
                         self._ck("max_price", r), ask)
                return
            if sweep_px > ask + 1e-9:
                log.info("cross %s %s: $%.2f sweeps to %.3f (touch %.3f, "
                         "+%.3f)", r.slug, side, stake, sweep_px, ask,
                         sweep_px - ask)
            ask = sweep_px
        if stake < settings.min_order_usdt or self.bankroll < stake:
            return
        # THE ROUND is one bet: with the reversal guard on, every entry in a
        # round is the same side of the same round and settles on ONE oracle
        # print. So the round's CUMULATIVE stake is the risk unit, not the
        # order's. See config.cross_max_round_stake_mult -- without this a
        # 4-entry round put 26.9% of bankroll on a single print, 1.17x full
        # Kelly, against a per-entry posture of 0.29x.
        rmult = settings.cross_max_round_stake_mult
        if rmult > 0:
            spent = (getattr(self, "_round_stake", None) or {}).get(r.slug, 0.0)
            room = rmult * stake - spent
            if room < settings.min_order_usdt:
                self._cross_refuse(
                    r, side,
                    f"round already holds ${spent:.2f}, cap is "
                    f"{rmult:.1f}x ${stake:.2f}",
                    tau=tau, spot=spot_now, stake=stake, sigma=sigma_now)
                return
            if room < stake:
                log.info("cross %s %s: sizing down $%.2f -> $%.2f (round cap)",
                         r.slug, side, stake, room)
                stake = round(room, 2)
        # ACCOUNT-WIDE exposure, across every family. See
        # config.cross_max_open_stake_pct: with six markets 0.945 of
        # simultaneously-open positions sit on the SAME side, so this is one bet
        # held many times, and the daily loss cap cannot help because they
        # expire together. Checked here -- after depth sizing, before ordering
        # -- so the number compared is the stake actually about to be committed.
        cap_pct = settings.cross_max_open_stake_pct
        if cap_pct > 0 and self.bankroll > 0:
            open_stake = self._open_cross_stake()
            if open_stake + stake > cap_pct * self.bankroll:
                self._cross_refuse(
                    r, side,
                    f"open cross exposure ${open_stake:.2f} + ${stake:.2f} "
                    f"exceeds {100 * cap_pct:.0f}% of ${self.bankroll:.2f}",
                    tau=tau, spot=spot_now, stake=stake, sigma=sigma_now)
                return
        # The venue list is consulted to confirm we can still READ it: an
        # unreadable position list means we cannot reason about exposure at
        # all, so the arm sits the round out.
        #
        # What this deliberately does NOT do is refuse the round because a
        # token is held. That test was copied from _impulse_tick, where it is
        # correct because the impulse arm takes the MAIN slot. This arm has a
        # reserved slot of its own, and the main arm enters at tau 60-210 and
        # holds to the bell -- so the test refused 52% of this arm's triggers,
        # at an EV indistinguishable from the ones it let through (measured on
        # 162 fill-aware triggers over 6 days: +0.2416/$1 blocked against
        # +0.2411 allowed, day-t 4.44 against 1.69). It was also SILENT,
        # returning before the check_reserved call that logs a refusal.
        #
        # Doubling is still bounded, including across a restart: the reserved
        # slot counts unsettled cross fills because _restore_open_positions replays
        # them through guard.on_open(reserved=True), so check_reserved below
        # already sees a position this process never booked.
        # Claim the round across the ONLY await in this method (the venue
        # position read below). Everything else here is synchronous, so
        # this is the whole window in which two ticks can both pass the
        # per-round gates above and both claim. try/finally rather than a
        # release at each return: there are five refusal paths inside and
        # an exception must not leave the round latched shut.
        if not isinstance(getattr(self, "_cross_inflight", None), set):
            self._cross_inflight = set()
        if r.slug in self._cross_inflight:
            return
        self._cross_inflight.add(r.slug)
        try:
            if self.broker.mode == "live" and self.book:
                unbooked = await self._unbooked_round_tokens(r, up, down)
                if unbooked is None:
                    log.info("cross %s %s not taken: venue position list "
                             "unavailable", r.slug, side)
                    return
                if unbooked:
                    log.info("cross %s %s not taken: venue holds %d token(s) here "
                             "the ledger cannot explain", r.slug, side,
                             len(unbooked))
                    return
            # The one losing cell of this arm: the side OPPOSITE a position the
            # MAIN arm already holds. See config.cross_skip_opposite_to_main for
            # the measurement. Only the main arm counts -- a cross opposite an
            # earlier CROSS is the alternating-hedge case cross_max_per_round
            # exists to allow.
            if self._ck("skip_opposite_to_main", r):
                for fid, (fslug, held) in list(self.open_fills.items()):
                    if fslug != r.slug or getattr(held, "side", None) == side:
                        continue
                    reserved, _pool = pool_for_mode(self._fill_mode(fid))
                    if reserved:
                        continue                      # an earlier cross, not the main arm
                    self._cross_refuse(
                        r, side,
                        "the main arm holds the opposite side here (measured "
                        "EV/$1 -0.107 against +0.356 same-side)",
                        tau=tau, spot=spot_now, sigma=sigma_now)
                    return
            # The RESERVED slot, not the main one. The main arm enters at tau
            # 60-210 and holds to settlement, which covers this arm's whole window,
            # so `check` refused 82% of cross triggers as "already positioned" --
            # and 61% of those wanted the OPPOSITE side, i.e. hedges were being
            # blocked rather than double exposure prevented. Both sides are taken;
            # see check_reserved for what that gives up.
            # `max_open` is ACCOUNT-WIDE (guards.state.reserved_open counts every
            # open reserved position), so it must NOT be the per-round knob. The
            # per-round limit is enforced separately at the top of this method via
            # `_cross_fired[r.slug]`. Passing max_per_round here was harmless with
            # one family and would have capped all six at 5 concurrent positions.
            # Minus the orders already in flight: `reserved_open` only counts
            # positions that have BOOKED, and since placement moved off the poll
            # loop several can be outstanding at once.
            cap_open = settings.cross_max_open_positions or 10 ** 6
            cap_open -= getattr(self, "_pending_n", 0)
            if cap_open <= 0:
                self._cross_refuse(r, side, "reserved slots full (orders in flight)",
                                   tau=tau, spot=spot_now, sigma=sigma_now)
                return
            allowed, why = self.guard.check_reserved(r.slug, max_open=cap_open)
            if not allowed:
                self._cross_refuse(r, side, why, tau=tau, spot=spot_now, sigma=sigma_now)
                return
            # Claim before ordering, so a rejection skips the round rather than
            # risking a second attempt at it.
            self._cross_fired[r.slug] = self._cross_fired.get(r.slug, 0) + 1
            # Cumulative stake for the ROUND, claimed with the slot. Pruned to live
            # rounds so the map cannot grow all day.
            rs = getattr(self, "_round_stake", None)
            if not isinstance(rs, dict):
                rs = self._round_stake = {}
            rs[r.slug] = rs.get(r.slug, 0.0) + stake
            # Stamp the entry time with the claim, for the re-entry gap above.
            #
            # NOT `now_ms`. That is read at the TOP of this method, before the
            # venue round-trip below, so stamping it records when the tick
            # STARTED rather than when the round was claimed. A venue read
            # slower than the gap then expires the stamp before it is even
            # written, and the next tick walks straight through:
            #
            #   t=0.00  A starts, now_ms=0, gap passes, claims _cross_inflight
            #   t=0.06  B starts, blocked by _cross_inflight          (correct)
            #   t=1.50  A stamps 0, releases _cross_inflight
            #   t=1.55  C starts, now_ms=1550, reads last=0
            #           1550 - 0 >= 1000  ->  C PASSES, 50ms after A
            #
            # LIVE 2026-09-14, three rounds took paired entries 36-744ms apart
            # this way -- eth-updown-5m-1789415100 twice 65ms apart at the same
            # quote and stake, eth-updown-5m-1789413600 four times inside 300ms
            # -- 10 of 48 fills and $29.19 of the day's stake.
            lm = getattr(self, "_cross_last_ms", None)
            if not isinstance(lm, dict):
                lm = self._cross_last_ms = {}
            lm[r.slug] = int(time.time() * 1000)
            self._cross_last_ms = {k: v for k, v in lm.items() if k in live}
            self._round_stake = {k: v for k, v in rs.items() if k in live}
            # Remember the side, not just the count: the reversal guard reads it.
            self._cross_side[r.slug] = side
            self._cross_count += 1
        finally:
            self._cross_inflight.discard(r.slug)
        spot = spot_now
        sigma = sigma_now
        # `ask` is the last POLLED book price, and the poll runs ~1/s while the
        # venue completes 66% of its post-cross move inside that same second
        # (measured 2026-09-13, n=189: pre-cross poll 0.505 -> cross tick 0.618
        # -> +4s 0.645). So this number is NOT what we will pay, and saying
        # "@0.350" when the fill lands at 0.656 reads as a 30-cent execution
        # failure when it is really the price never having existed. Label it.
        # `ask` is now the SWEEP price for this stake when the websocket book
        # is live, so it is what we expect to pay rather than a touch we cannot
        # get. The old label said "stale_ask" and quoted the touch, which made
        # a normal sweep read as a 14-cent execution failure.
        src = "sweep" if top is not None else "poll"
        # The label has to carry the TRIGGER. A train fire logged as "CROSS"
        # reads as the cross arm violating its own 5-60s tau gate -- it did at
        # tau 820s on 2026-09-15 21:01, and that is exactly how it was misread.
        log.info("%s %s %s %s_px=%.3f (spot %.2f vs strike %.2f, tau %.0fs) "
                 "stake=$%.2f -> %d today",
                 "TRAIN" if train else "CROSS", r.slug, side, src, ask, spot,
                 r.strike, tau, stake, self._cross_count)
        # `run` goes into the reason so every trigger carries the number the
        # gate decision will be made on, recoverable from the decisions table.
        run_txt = "" if book_run is None else f" run{book_run:+.3f}"
        kind = ("train into" if train
                else ("cross probe" if probe else "cross into"))
        sig = Signal(side, ask, ask, 0.0, 0.0, 0.0, True,
                     f"{kind} {side} @{ask:.3f} tau{tau:.0f}s{run_txt}")
        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau, "side": side,
            "fair": ask, "quote": ask, "edge": 0.0, "hurdle": 0.0,
            "sigma_1m": sigma, "spot": spot, "oracle": None,
            "strike": r.strike, "stake": stake, "acted": 0,
            "reason": sig.reason,
        })
        token = up.token_id if side == "Up" else down.token_id
        # Neither pre-trade gate fits: there is no model fair to re-check, and
        # the candle-open gate is the opposite of this trade, which fires deep
        # inside a candle. Same reasoning as the double-down.
        #
        # The limit is priced at the CEILING, not at the trigger ask. Passing
        # the ask made `take_limit_px` return max(ask - margin, ask) == ask,
        # i.e. no slack at all: one tick of book movement between the trigger
        # and the order arriving meant no fill. The book moves hard right after
        # a cross -- 0.18 -> 0.50 inside one second on
        # btc-updown-5m-1789283400 -- so that pinned limit filled an estimated
        # 10% of the time, and the fills it did get were adversely selected
        # (we fill when the book moves AGAINST us, not for us).
        #
        # Pricing at cross_max_price gives the limit room to chase the book up
        self._schedule_cross_meter(
            r, side, "fire",                         # the +0.25/0.5/1.0s path
            label=("train" if train else "cross"))
        # to the ceiling and no further, which is the same shape that makes the
        # model arm fill 95%. `take_px` stays the trigger ask as the floor.
        # The limit sits just above the ask we actually observed, NOT at
        # cross_max_price. A LIMIT quote commits amountIn/priceLimit shares
        # regardless of the market (verified read-only across limits 0.30-0.99
        # while the ask was 0.960), so a limit pinned at 0.90 commits at 0.90
        # and the venue sometimes fills exactly that commitment. See
        # config.cross_limit_buffer for the measurement that forced this.
        # INVERSE in the ask, not proportional to it. The old rule was
        # `min(0.10, 0.20 * ask)`, which shrinks the buffer as the ask falls --
        # 0.10 at 0.50+, 0.064 at 0.32, 0.028 at 0.14. That is backwards with
        # respect to the mechanism: a cheap ask means the venue has NOT yet
        # repriced a fresh cross, so it is exactly the case where the book is
        # about to run furthest. It bid least where it had to bid most, and all
        # nine live cancellations were cheap (decided ask 0.14-0.57, median
        # 0.36) while no attempt at 0.60+ has ever missed.
        #
        # `cross_limit_buffer` stays the absolute cap; see config for the
        # limit-pinning sweep that chose 0.25 over the more aggressive 0.35/0.50.
        if train:
            # TRAIN: a flat bound. The proportional buffer below is tuned for a
            # crossing's ~0.50-0.60 ask and collapses as the ask rises; this
            # entry prices near 0.80, where the measured bound is a flat +1.5c.
            # cross_max_fill_px (0.65) cannot be allowed to clamp it -- it
            # would put the entry out of reach entirely -- hence fill_cap.
            ceil_side = min(self._ck("train_max_fill_px", r),
                            round(ask + self._ck("train_bound", r), 2))
        else:
            buf = max(0.01, min(self._ck("limit_buffer", r),
                                self._ck("limit_buffer_inv", r) * (1.0 - ask)))
            ceil_side = min(self._ck("max_price", r), round(ask + buf, 2))
        # INSTRUMENTATION. Three explanations for the gate->fill gap have now
        # been wrong: a stale gate (the websocket made it fresh and the gap
        # stayed), order latency (get_quote round-trips measure 43ms), and
        # book sweep (the measured premium for a $1-5 order is +0.0004 across
        # 19,452 real depth snapshots). On 2026-09-13 a cross decided at 0.760
        # with run+0.000 filled at 0.9008 four seconds later and none of those
        # account for it. So record what the book was doing WHILE the order was
        # in flight instead of inferring it again.
        # NON-BLOCKING FROM HERE. `buy_limit` awaits the fill for up to
        # limit_wait_s (20s) and `_cross_all` walks the six families
        # sequentially inside the poll loop, so awaiting the order here stopped
        # EVERYTHING. Measured 2026-09-14: every unfilled attempt produced a
        # quote gap the length of its own wait --
        #     waited 21.2s -> gap 21.4s     waited 21.5s -> gap 26.0s
        #     waited 12.4s -> gap 16.3s     waited  7.6s -> gap 11.5s
        # -- no market polled, no crossing on ANY family detectable, for that
        # long. The stall begins the instant a trigger fires, which is when the
        # market is moving and further crossings are most likely. A filled
        # order cost ~3.7s, a missed one 21s, so the 35% of attempts that miss
        # burned six times the blind time of the ones that worked.
        #
        # The slot is already claimed above (`_cross_fired`), so the await was
        # pure dead time. What the task must not lose is the cap arithmetic:
        # two orders in flight could each pass a 30% exposure check and land at
        # 60% together, so stake and slot are RESERVED here and released in the
        # task's `finally`.
        self._pending_n = getattr(self, "_pending_n", 0) + 1
        self._pending_stake = getattr(self, "_pending_stake", 0.0) + stake
        task = asyncio.create_task(self._cross_execute(
            r=r, sig=sig, side=side, token=token, stake=stake, ask=ask,
            ceil_side=ceil_side, did=did, up=up, spot=spot, sigma=sigma,
            tau=tau, wsb=wsb,
            fill_cap=(self._ck("train_max_fill_px", r) if train else None)))
        tasks = getattr(self, "_order_tasks", None)
        if tasks is None:
            tasks = self._order_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _cross_execute(self, *, r, sig, side, token, stake, ask,
                             ceil_side, did, up, spot, sigma, tau, wsb,
                             fill_cap: float | None = None) -> None:
        """Place one cross order and book it, OFF the poll loop.

        Same code that used to run inline in `_cross_tick`; what changed is
        that the decision loop no longer waits for it.
        """
        try:
            t_send = time.monotonic()
            book_at_send = ask
            # SHADOW QUOTE, log-only: the order below carries the WARM quote,
            # and the venue anchors the FOK bound to that quote's price. A
            # fresh quote fetched NOW answers the counterfactual the catch
            # question needs -- "would a current reference have been
            # accepted?" -- concurrently with the POST, read-only, and it can
            # never touch the order itself.
            self._spawn_shadow_quote(r.slug, side, token, stake, ceil_side)
            fill = await self._place_order(
                r, sig,
                fair_up=(ceil_side if side == "Up" else 1.0 - ceil_side),
                token=token, stake=stake, skip_pretrade_gates=True, take_px=ask,
                # Bid up to the ceiling, not to the stale quote plus a tick. See
                # limit_ceiling in _place_order for the round this cost us.
                limit_ceiling=ceil_side, fill_cap=fill_cap)
            elapsed = time.monotonic() - t_send
            top_now = (wsb.best(r.market_id, settings.ws_book_max_age_s)
                       if wsb is not None else None)
            book_now = top_now.ask(side) if top_now is not None else None
            got = getattr(fill, "filled_px", None) if fill else None
            # The four numbers that let the buffer be tuned from data instead of
            # intuition: what we decided on, what limit we actually sent, what we
            # got, and how much of the intended stake filled. The last one is the
            # discriminator -- a 100% fill paid its limit, partial fills beat it.
            cash = getattr(fill, "stake", None) if fill else None
            shares = getattr(fill, "shares", None) if fill else None
            log.info("%s exec %s %s: decided %.4f limit %.2f -> %s in %.2fs | "
                     "committed $%s of $%.2f (%s) | shares %s | cash/shares %s | "
                     "book now %s",
                     ("train" if str(getattr(sig, "reason", "")).startswith("train")
                      else "cross"),
                     r.slug, side, book_at_send, ceil_side,
                     ("%.4f" % got) if got else "NO FILL", elapsed,
                     ("%.4f" % cash) if cash else "0", stake,
                     ("%.0f%%" % (100 * cash / stake)) if cash and stake else "0%",
                     ("%.4f" % shares) if shares else "0",
                     ("%.4f" % (cash / shares)) if (cash and shares) else "n/a",
                     ("%.4f" % book_now) if book_now is not None else "n/a")
            if not fill:
                return
            await self._book_fill(r, sig, fill, did=did, up=up, spot=spot,
                                  sigma=sigma, tau=tau, token=token, mode="cross")
        except Exception as e:                              # pragma: no cover
            log.exception("cross order failed for %s %s: %s: %s",
                          r.slug, side, type(e).__name__, e)
        finally:
            self._pending_n = max(0, getattr(self, "_pending_n", 1) - 1)
            self._pending_stake = max(
                0.0, getattr(self, "_pending_stake", stake) - stake)

    def _spawn_shadow_quote(self, slug, side, token, stake, ceiling) -> None:
        """Launch the log-only shadow quote; never raise into the fire path."""
        if getattr(self.broker, "mode", "") != "live":
            return
        try:
            task = asyncio.create_task(
                self._shadow_quote(slug, side, token, stake, ceiling))
        except RuntimeError:                # no running loop (fixture use)
            return
        tasks = getattr(self, "_shadow_tasks", None)
        if tasks is None:
            tasks = self._shadow_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _shadow_quote(self, slug, side, token, stake, ceiling) -> None:
        """A fresh quote at fire time, logged; the ORDER never sees it.

        get-quote does not execute, so this is read-only. What it measures:
        our place-order carries the WARM quote, the venue anchors its FOK
        bound to that quote's averagePrice (the order record's `price` equals
        it), and the 2026-09-15 fires show refs lagging the book by up to 20x
        (0.03 against a 0.59 book). If fresh refs are systematically higher
        at fire time, moving the quote fetch back onto the critical path --
        or refreshing the warm entry -- becomes a measured argument instead
        of a guess.
        """
        t0 = time.monotonic()
        try:
            q = await self.broker.get_quote(token, stake)
        except Exception:
            return
        if not q:
            log.info("shadow quote %s %s: none", slug, side)
            return
        try:
            px = float(q.get("averagePrice") or 0.0)
        except (TypeError, ValueError):
            return
        if px <= 0 or not ceiling:
            return
        room = int((ceiling / px - 1.0) * 10_000)
        slip = max(settings.slippage_bps, min(room, 10_000))
        log.info("shadow quote %s %s: fresh px %.4f after %.0fms -> "
                 "bound %.4f (ceiling %.3f)",
                 slug, side, px, (time.monotonic() - t0) * 1000.0,
                 px * (1 + slip / 10_000.0), ceiling)

    @staticmethod
    def double_trigger(*, entry_px: float | None, ask: float | None, tau: float,
                       min_tau: float, max_tau: float, min_quote: float,
                       max_quote: float, need: float, count: int, cap: int,
                       already: bool) -> str | None:
        """Should this winning position be added to? None = no, else why.

        Pure, so the rule is testable without a venue. Every bound here is one
        that has bitten elsewhere in this repo: the tau window is load-bearing
        (widening it turns the book negative), the quote band is where the edge
        actually lives, and `cap` stops a losing experiment from quietly
        becoming a position size. One add per round, ever.
        """
        if already or (cap > 0 and count >= cap):
            return None                      # 0 = no cap, the repo's convention
        if not (min_tau <= tau <= max_tau):
            return None
        if not ask or not 0 < ask < 1 or not entry_px:
            return None
        if not (min_quote <= ask <= max_quote):
            return None
        if ask < entry_px + need:
            return None
        return f"up {ask - entry_px:+.3f} from entry {entry_px:.3f}"

    async def _double_tick(self) -> None:
        """Add a second unit to a position that is already winning.

        MEASURED BEFORE SHIPPING (tools/arm_overlap --double-at, 1,177 rounds):
        the add scores +0.0686/$1 at t 2.05, but it is bought at a WORSE price
        than the first unit (~0.69 against ~0.61), so its payoff is 1.44:1 where
        the first unit's is 1.64:1, and it FAILS the half-sample check (second
        half +0.003 against the base arm's +0.112). Total profit rises ~36% on
        ~49% more capital, so return per dollar FALLS ~8%. It is also the first
        thing in this repo to relax the one-position-per-round invariant.

        So it is capped, booked as its own mode, and settled forward instead of
        trusted. Kill it if mode='double' EV/$1 <= 0 after ~200 adds.
        """
        if not settings.double_enabled or self.mode != "live":
            return
        r = getattr(self, "_round", None)
        if r is None:
            return
        # The round's ENTRY fill: the earliest one, since an add is always
        # later. `open_fills` is keyed by fill id and can hold both units of a
        # doubled round.
        mine = [f for (sl, f) in self.open_fills.values() if sl == r.slug]
        up, down = r.up, r.down
        feed = getattr(self, "_primary_spot", None)
        if (not mine or feed is None or not feed.price or not up or not down
                or not r.strike):
            return
        fill = min(mine, key=lambda f: f.ts_ms)
        up_ask, down_ask = getattr(self, "_asks", (None, None))
        ask = up_ask if fill.side == "Up" else down_ask
        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        why = self.double_trigger(
            entry_px=getattr(fill, "filled_px", None), ask=ask, tau=tau,
            min_tau=settings.min_tau_s, max_tau=settings.max_tau_s,
            min_quote=settings.min_quote, max_quote=settings.max_quote,
            need=settings.double_at, count=self._double_count,
            cap=settings.double_max_per_day, already=r.slug in self._doubled)
        if why is None:
            return
        allowed, blocked = self.guard.check_add(r.slug)
        if not allowed:
            log.info("double %s not taken: %s", r.slug, blocked)
            return
        stake = round(fill.stake * settings.double_stake_mult, 2)
        if stake < settings.min_order_usdt or self.bankroll < stake:
            return
        token = up.token_id if fill.side == "Up" else down.token_id
        self._doubled.add(r.slug)
        self._double_count += 1
        log.info("DOUBLE %s %s +$%.2f at %.3f (%s) tau=%.0fs -> %d today",
                 r.slug, fill.side, stake, ask, why, tau, self._double_count)
        sig = Signal(fill.side, ask, ask, 0.0, 0.0, 0.0, True, f"double {why}")
        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau, "side": fill.side,
            "fair": ask, "quote": ask, "edge": 0.0, "hurdle": 0.0,
            "sigma_1m": getattr(self, "_sigma", 0.0), "spot": feed.price,
            "oracle": None, "strike": r.strike, "stake": stake, "acted": 0,
            "reason": f"double: {why}",
        })
        # Neither pre-trade gate fits an add
        # (there is no fresh model claim to recheck, and the add happens
        # mid-round rather than at a candle open). `fair_up` is P(Up), so a Down
        # add must pass the OTHER side's price or the taker limit inverts.
        got = await self._place_order(
            r, sig, fair_up=(1.0 - ask) if fill.side == "Down" else ask,
            token=token, stake=stake, skip_pretrade_gates=True, take_px=ask)
        if not got:
            log.warning("DOUBLE %s not filled at %.3f", r.slug, ask)
            return
        await self._book_fill(r, sig, got, did=did, up=up, spot=feed.price,
                              sigma=getattr(self, "_sigma", 0.0), tau=tau,
                              token=token, mode="double")



    async def _idle(self, budget: float) -> None:
        """Wait out the poll interval -- on the feed's clock in impulse mode.

        Sleeping the whole interval is what put a median 500 ms (1 s worst case)
        between "the websocket showed the move" and "we looked", on a host
        5.3 ms from the venue. In impulse mode the wait ends on the next price
        update instead, and the trigger is evaluated there.
        """
        # WHICH ARMS WANT TICK RESOLUTION. This used to ask only about the
        # impulse arm, which silently made the CROSS arm's latency depend on
        # whether a completely different arm was switched on. Turning impulse
        # off on 2026-09-13 demoted cross from ~114 price updates/s to the 1s
        # poll (4.15s in practice, see RECORDED_REFRESH_S) and it immediately
        # missed a real crossing: btc-updown-5m-1789336500, spot through the
        # strike to 77336.00 for ~1.5s, never sampled.
        #
        # Both arms trade off `feed.ticks`, so both belong on the feed's clock.
        want_impulse = (settings.entry_mode in ("impulse", "both")
                        and settings.impulse_on_tick)
        want_cross = settings.cross_enabled
        if not (want_impulse or want_cross):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=budget)
            return
        feed = getattr(self, "spot", None)
        if feed is None:
            return
        deadline = time.time() + budget
        while not self._stop.is_set():
            try:
                if want_impulse:
                    await self._impulse_tick()
                if want_cross:
                    await self._cross_all()
            except Exception as e:                      # noqa: BLE001
                # Never let a trigger bug take down the loop: recording,
                # settlement and the risk guard all live in the poll.
                log.exception("impulse trigger failed: %s: %s",
                              type(e).__name__, e)
            left = deadline - time.time()
            if left <= 0:
                return
            if not await feed.wait_tick(timeout=left):
                return

    def _stake_for(self, sig: Signal, moneyness_sd: float = 0.0) -> float:
        """Stake for an accepted signal (see config.sizing_mode/_metric).

        Applies the optional sizing tilt on top of whichever base rule is
        active: the tilt redistributes the stake, it does not change the total.
        """
        base = self._base_stake_for(sig)
        if base > 0 and settings.stake_tilt == "inv_z":
            t = tilt_factor(moneyness_sd, reference=settings.tilt_reference_z,
                            lo=settings.tilt_min, hi=settings.tilt_max)
            base = max(base * t, settings.min_order_usdt)
            if settings.stake_cap_usdt > 0:      # 0 = no cap, see config
                base = min(base, settings.stake_cap_usdt)
        return base

    def _base_stake_for(self, sig: Signal) -> float:
        """The active sizing rule, before any tilt."""
        if settings.sizing_mode == "kelly":
            # Kelly returns 0 when the bet sits below the venue minimum; that
            # means SKIP, never "bet the maximum instead".
            return kelly_stake(sig.fair, sig.quote, bankroll=self.bankroll,
                               fraction=settings.kelly_fraction,
                               min_stake=settings.min_order_usdt,
                               max_stake=settings.max_stake_usdt)
        if settings.sizing_mode == "fraction":
            # Percentage of the live bankroll. Returns 0 (skip) when the
            # fraction cannot reach the venue minimum -- see
            # stake_for_fraction for why it must not round up on its own; the
            # floor is opt-in via stake_floor_to_minimum.
            return stake_for_fraction(self.bankroll,
                                      fraction=settings.stake_fraction,
                                      min_stake=settings.min_order_usdt,
                                      cap=settings.stake_cap_usdt,
                                      floor_to_minimum=(
                                          settings.stake_floor_to_minimum))
        # What counts as "strongest": the claimed edge, or expected profit per
        # dollar. The recording favours ev/$ -- large claims are the most
        # inflated, so sizing up on them rewards the noisiest signals.
        if settings.sizing_metric == "ev":
            value = sig.ev_per_dollar
            lo, hi = settings.stake_ev_lo, settings.stake_ev_hi
        else:
            value = sig.edge
            lo, hi = settings.min_edge, settings.stake_edge_hi
        return stake_for_edge(value, min_edge=lo,
                              min_stake=settings.min_stake_usdt,
                              max_stake=settings.max_stake_usdt,
                              edge_at_max=hi)

    async def _place_order(self, r, sig, *, fair_up: float, token: str,
                           stake: float,
                           skip_pretrade_gates: bool = False,
                           take_px: float | None = None,
                           impulse_order: bool = False,
                           limit_ceiling: float | None = None,
                           fill_cap: float | None = None) -> Fill | None:
        """Place one order, retrying a confirmed rejection a bounded number of times.

        Returns the fill, or None when the order could not be placed -- having
        set the order cooldown if the failure was an actual rejection rather
        than a gate refusing to trade.

        `skip_pretrade_gates` is for arms with no model fair to re-check that
        fire late in a round rather than at a candle open (cross, double).
        It skips only those two decision gates; every safety check below (the
        venue-position check before a retry, the booking, the verification) is
        untouched.

        `take_px` is the arm's own price for the trade, and it is a FLOOR on a
        taker limit -- for an arm with no valuation of its own (the cross),
        whose limit would otherwise sit below the ask it just read.
        """
        # A FOK order fails whenever the book moves between the quote and the
        # execution, and that is a transient miss rather than a reason to give
        # up the round -- so make a bounded number of attempts. Each one redoes
        # the whole pre-trade path: a spent quoteId cannot be re-sent, and both
        # the edge and the candle-open window can evaporate in the second or
        # two a failed attempt costs.
        # LOCKED-LOSS GUARD, ahead of every attempt and NOT behind
        # `skip_pretrade_gates` -- that flag skips signal re-checks, and this is
        # a risk check. Holding both sides of a round pays exactly 1.000, so a
        # pair costing more than `max_paired_price` is a loss by arithmetic and
        # cannot be undone on a venue with no sell. See config.
        cap_pair = settings.max_paired_price
        if cap_pair > 0 and sig.side in ("Up", "Down"):
            mine = float(getattr(sig, "quote", 0.0) or 0.0)
            # getattr: fixtures build Bot via __new__ and have no open_fills.
            _of = getattr(self, "open_fills", None) or {}
            for _fid, (fslug, held) in list(_of.items()):
                if fslug != r.slug or getattr(held, "side", None) == sig.side:
                    continue
                other = float(getattr(held, "filled_px", 0.0) or 0.0)
                if other <= 0 or mine <= 0:
                    continue
                if other + mine > cap_pair:
                    log.info("%s %s not taken: would hold both sides of this "
                             "round at %.3f + %.3f = %.3f (> %.2f); a matched "
                             "pair pays 1.000, so that is a locked loss",
                             r.slug, sig.side, other, mine, other + mine,
                             cap_pair)
                    return None
        attempts = max(1, settings.order_max_attempts)
        fill = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(settings.order_retry_delay_s)
                # Never start an attempt into a round that is nearly over.
                # Retries used to be cheap and quick; `order_status_wait_s` now
                # spends up to 9s hearing the venue's verdict, so three attempts
                # can span ~30s against a trigger with 30-50s of round left.
                # See config.order_min_tau_s.
                _tau_fn = getattr(r, "tau_s", None)
                _tau_left = (_tau_fn(int(time.time() * 1000))
                             if _tau_fn else float("inf"))
                # A MARKET attempt needs the round open only long enough to
                # place and execute; its verdict may land after the bell, and
                # the venue executes near the close (4 live fills booked with
                # under 1s of round left). The 5s floor was calibrated against
                # the verdict budget, which an immediate FOK does not spend --
                # and on 07:39:50 it refused the eth retry at 3.3s left, so the
                # venue never saw a second order. For market orders the floor
                # is what EXECUTION needs instead.
                _floor = settings.order_min_tau_s
                if settings.order_type == "market":
                    _floor = min(_floor, _MARKET_RETRY_TAU_FLOOR_S)
                if _floor > 0 and _tau_left < _floor:
                    log.info("not re-attempting %s %s: %.1fs of round left, "
                             "floor is %.1fs", r.slug, sig.side, _tau_left,
                             _floor)
                    break
                # Last line of defence against double exposure. Whatever the
                # previous attempt reported -- including a status the venue
                # called FAILED -- ask the venue what it actually holds before
                # re-placing: an earlier attempt that filled shows up here, and
                # buying again would put on a second position in a round we are
                # already long. The broker confirms this for its own failures
                # (see LiveBroker.buy), so this only ever catches the residual
                # cases -- and it is cheap next to a doubled position.
                if self.broker.mode == "live":
                    held = await self._venue_position_tokens()
                    if held is None:
                        log.error("cannot read the venue's positions; not "
                                  "re-placing %s %s after a failure",
                                  r.slug, sig.side)
                        break
                    if token in held:
                        log.error("venue already holds %s for %s; an earlier "
                                  "attempt DID fill -- not re-placing",
                                  token, r.slug)
                        break

            # Re-check against a live quote before committing. The venue's fee is
            # dynamic and its executable price can differ from the book, so the
            # decision above used estimates. get-quote does not execute, so this
            # costs nothing but a round trip -- and the quote is REUSED for the
            # order itself so placing it costs no extra latency (quote TTL is ~5s).
            quote = None
            if self.broker.mode == "live":
                # A WARM quote on the FIRST attempt only: the cross fetches it
                # before the crossing, and consuming it removes the 40-65ms
                # quote round-trip from the decision -> venue race (measured
                # 169-186ms against the venue's own records; the venue's
                # reprice settles inside ~250ms). Retries fetch fresh -- their
                # question is whether the book CAME BACK, which only a fresh
                # quote can answer. Any doubt (stale, stake mismatch) falls
                # through to the synchronous fetch, so this can only ever be
                # faster, never a different trade.
                if attempt == 0:
                    quote = self._take_prefetched_quote(token, stake)
                    if quote is not None:
                        # Marks the warm hits so the decision -> venue delta
                        # (order createTime minus the CROSS line) can be
                        # scored against the 169-186ms cold baseline; see
                        # FINDINGS "Can we execute faster". The age and px
                        # are part of the bound: the venue anchors the FOK to
                        # this quote's price, and the 2026-09-15 fires show
                        # refs lagging the book by up to 20x (0.03 vs 0.59).
                        try:
                            _wpx = float(quote.get("averagePrice") or 0.0)
                        except (TypeError, ValueError):
                            _wpx = 0.0
                        log.info("warm quote used for %s %s: age %.2fs px %.4f",
                                 r.slug, sig.side,
                                 getattr(self, "_warm_age_s", -1.0), _wpx)
                if quote is None:
                    quote = await self.broker.get_quote(token, stake)
                if not quote:
                    log.warning("no pre-trade quote for %s (attempt %d/%d)",
                                r.slug, attempt + 1, attempts)
                    continue
                real_px = float(quote.get("averagePrice") or sig.quote)
                real_fee = float(quote.get("feeRateBps") or settings.fallback_fee_bps) / 10_000.0
                # The band was checked at the trigger against the last ask the
                # POLL saw; this quote is the price that actually binds. In
                # impulse mode the model re-check below is skipped (there is no
                # model fair to re-check), so without this the book could drift
                # from 0.70 to 0.90 between the trigger and the order and the
                # price cap would be decoration.
                refused = impulse_price_refusal(
                    "impulse" if impulse_order else settings.entry_mode,
                    real_px, settings.impulse_max_price)
                if refused:
                    log.info("SKIP %s %s: %s (trigger saw %.3f)",
                             r.slug, sig.side, refused, sig.quote)
                    return
                # Re-check ONLY the chosen side, against its own hurdle. The old
                # code re-picked "the best side" at the live price and refused
                # the order when the OTHER side looked better -- discarding
                # trades whose own edge was still intact. A None quote makes the
                # other side untradeable, so evaluate() can only answer for this
                # one.
                # That quote is for the TOKEN being bought (get_quote sends its
                # tokenId), so its averagePrice IS that side's price. This used
                # to pass `1.0 - real_px` for a Down order -- complementing a
                # price that needed no complementing, so the Down candidate was
                # scored against 0.40 when the market asked 0.60. Measured on the
                # live record: down_ask 0.600 came back as "quote 0.40", and with
                # min_quote at 0.50 that refused every Down FAVOURITE -- the side
                # the honest pricing exists to buy. The live book ran 54 Up to 1
                # Down, against the replay's 267 Up / 347 Down, and the replay has
                # no recheck: it is the live path only.
                recheck = evaluate(
                    fair_up,
                    real_px if sig.side == "Up" else None,
                    real_px if sig.side == "Down" else None,
                    fee=real_fee, half_spread=0.0,
                    safety_margin=settings.safety_margin, fee_model=FeeModel.NOTIONAL,
                    max_quote=settings.max_quote, min_quote=settings.min_quote,
                    min_edge=settings.min_edge,
                    edge_haircut=settings.edge_haircut)
                if (not skip_pretrade_gates and not recheck.act
                        and settings.entry_mode != "impulse"
                        and not impulse_order):
                    # The edge is gone at the live price. Retrying would only
                    # be chasing a trade the gate has already refused.
                    # (In impulse mode there is no model edge to re-check: the
                    # trigger is the underlying's move, and the price sanity was
                    # applied at the decision. The quote is still fetched and
                    # reused, so the fill still lands on the live executable
                    # price rather than the stale displayed one.)
                    log.info("SKIP %s %s: real quote %.3f @ %d bps -> %s",
                             r.slug, sig.side, real_px, int(real_fee * 10_000),
                             recheck.reason)
                    return
                log.info("pre-trade check ok: px=%.3f fee=%dbps ev/$=%+.4f",
                         real_px, int(real_fee * 10_000), recheck.ev_per_dollar)

            # Re-gate at execution time. The decision fired inside the open window,
            # but the live quote recheck above costs a second or two of round trip
            # -- and so does every retry; never let a fill land after the window
            # has closed. Trades happen at the candle open or not at all.
            if (self.broker.mode == "live" and settings.trade_on_candle_open
                    and not skip_pretrade_gates):
                sec = sec_into_candle(int(time.time() * 1000))
                if sec >= settings.candle_open_window_s:
                    log.info("SKIP %s %s: slipped past candle open (%ds); order withheld",
                             r.slug, sig.side, sec)
                    return

            # MARKET ONLY for the cross arm (operator directive 2026-09-15:
            # resting orders here are a dead end). The stale-low fallback to a
            # LIMIT is removed: a quote the FOK cannot fill costs one failed
            # attempt and no money, and the retry re-quotes -- resting an order
            # instead was never the answer for an arm whose edge is seconds
            # wide.
            use_limit = settings.order_type in ("limit", "limit_take")
            if self.broker.mode == "live" and use_limit:
                if settings.order_type == "limit":
                    # Maker-first: post one tick below the quote and let it rest.
                    # A miss ENDS the round -- no retry and no crossing, because
                    # crossing would pay exactly the spread the limit exists to
                    # avoid, and the A/B has to measure posting on its own.
                    limit_px = max(0.01,
                                   round(sig.quote - settings.limit_offset, 2))
                else:
                    # Taker limit: cross the book, but never above our own
                    # valuation, and never more than a tick above the ask we
                    # actually measured. Two venues, same call: a CROSSING limit
                    # fills at the BOOK'S ASK (limit 0.18 against an ask of 0.15
                    # -> filled 0.1526 on eth-updown), so this reaches the real
                    # liquidity while the market path prices off the venue's
                    # DISPLAYED price, which lags its own book by up to 0.40 (75%
                    # of the price) and left the quote above the book in 5-7% of
                    # 335 live samples. Market fills averaged +0.16 above the ask
                    # on offer; limit fills, -0.014.
                    # The ceiling is the valuation, and NOTHING else. It used to
                    # be max(quote, fair - margin), which is wrong in the one
                    # case the ceiling exists for: a display that has gone stale
                    # HIGH (the venue repriced before us, i.e. we are late). Then
                    # the quote (say 0.90) beats the fair minus margin (0.86) and
                    # the limit would authorise a fill ABOVE our own valuation --
                    # a negative-EV entry, taken by the very arm the ceiling is
                    # there to stop. An arm with no valuation of its own passes
                    # `take_px`, which floors the limit instead.
                    # `limit_take_margin`, NOT `safety_margin`: the latter is the
                    # signal's hurdle (edge/signal.py), so raising it would
                    # quietly move the entry gate too. This knob sets only the
                    # ceiling, and it is swept in the config comment.
                    # The ceiling ALONE proved insufficient in live trading: on
                    # 2026-09-12 13:02 the trigger read an ask of 0.7326, our
                    # digital claimed P(Down)=0.94, the limit went out at 0.90 and
                    # filled at 0.8919 -- a trade the arm's own ev_at_fill scores
                    # at -0.195. `take_limit_px` clamps to ask + one tick, which
                    # cannot cost anything measured (the taker leg only ever fills
                    # AT the ask) and removes paying above the price we sized on.
                    limit_px = take_limit_px(
                        fair_up if sig.side == "Up" else 1.0 - fair_up,
                        settings.limit_take_margin, sig.quote,
                        settings.limit_ask_tick, take_px)
                    # Hard cap on the price PAID. Every other knob bounds the
                    # ask; this bounds the fill. See config.cross_max_fill_px:
                    # the arm is only profitable on fills of 0.55-0.65.
                    if limit_ceiling is not None and \
                            (settings.cross_max_fill_px if fill_cap is None
                             else fill_cap) > 0:
                        limit_ceiling = min(
                            limit_ceiling,
                            settings.cross_max_fill_px if fill_cap is None
                            else fill_cap)
                    if limit_ceiling is not None:
                        # `take_limit_px` clamps to quote + tick LAST, which
                        # overrides everything else. That clamp is right for the
                        # model arm -- its fair overshoots on fast moves and a
                        # 0.90 limit filled at 0.8919 for -0.195/$1 on
                        # 2026-09-12. It is WRONG for the cross arm, whose whole
                        # premise is that the side just went in the money and
                        # the quote it read is about to be stale by 60 cents.
                        #
                        # Measured live 2026-09-13 07:49 on
                        # btc-updown-5m-1789285500: spot crossed above the
                        # strike, Up was still quoted 0.200, the limit was
                        # clamped to 0.21, it never filled, and the round
                        # settled Up. A ~5x win the arm identified correctly and
                        # structurally could not take.
                        #
                        # `limit_ceiling` replaces that clamp with an explicit
                        # one. It is a RISK bound, not a valuation: above it the
                        # cell measures negative, so the order simply refuses.
                        limit_px = max(limit_px,
                                       min(round(limit_ceiling, 2), 0.99))
                # Never rest past the bell: the cross fires 3-60s before it, and
                # the configured 20s would outlive the round.
                #
                # The cross path's wait is `cross_limit_wait_s` (6s since the
                # 2026-09-15 reconciliation: 2s cancels inside the venue's
                # fill process -- fills complete to ~5.2s -- and 5s left no
                # margin on those reports; see the config block).
                # `limit_ceiling` is what marks this as the cross path (see
                # above); the model arm keeps limit_wait_s, and 0 falls back.
                wait_cap = settings.limit_wait_s
                if limit_ceiling is not None and settings.cross_limit_wait_s > 0:
                    wait_cap = settings.cross_limit_wait_s
                tau_fn = getattr(r, "tau_s", None)
                tau_left = tau_fn(int(time.time() * 1000)) if tau_fn else 1e9
                fill = await self.broker.buy_limit(
                    slug=r.slug, side=sig.side, token_id=token,
                    limit_px=limit_px, stake=stake,
                    wait_s=min(wait_cap, max(1.0, tau_left - 1.0)),
                    fill_break=settings.limit_fill_break)
                if fill:
                    break
                break
            kw = {"quote": quote} if self.broker.mode == "live" else {}
            # The cross arm's `limit_ceiling` is `ask + buffer` -- the highest
            # price its own measurement says is worth paying. Under MARKET it
            # becomes the quote ceiling, so the venue's lagging DISPLAYED price
            # cannot sell us a 0.17 ask at 0.84 (2026-09-11, +47% EV -> -70%).
            if limit_ceiling is not None:
                cap = min(round(limit_ceiling, 2), 0.99)
                _fcap = (settings.cross_max_fill_px if fill_cap is None
                         else fill_cap)
                if _fcap > 0:
                    cap = min(cap, _fcap)
                kw["max_px"] = cap
            fill = await self.broker.buy(slug=r.slug, side=sig.side, token_id=token,
                                         price=sig.quote, stake=stake, **kw)
            if fill:
                break

            # Only retry a failure the venue confirmed. Anything else -- a
            # timeout or a 5xx on place-order, a status we could not read --
            # may have left a live order behind, and placing a second one
            # would double the exposure. Brokers that report no reason (paper,
            # whose rejections are deterministic anyway) stop here too.
            reason = getattr(self.broker, "last_failure", None)
            # Declining a price is not a failed ORDER: nothing reached the
            # venue, and the next attempt re-quotes into a book that may have
            # come back inside the ceiling. It used to END the round right
            # here -- which is why the venue saw one order per failed trigger
            # while the log said "attempt 1/3; not retrying": the operator's
            # "retry doesn't work" report, 2026-09-15.
            if reason == "quote_above_ceiling":
                log.info("attempt %d/%d declined: quote above the ceiling "
                         "(price left the band)", attempt + 1, attempts)
                continue
            if reason not in RETRYABLE_FAILURES:
                log.error("order failed for %s %s (%s); not retrying",
                          r.slug, sig.side, reason or "outcome unknown")
                break
            log.warning("order attempt %d/%d failed for %s %s (%s)",
                        attempt + 1, attempts, r.slug, sig.side, reason)

        if not fill:
            # Back off rather than re-firing every tick: a rejection that
            # survives every attempt is almost always a persistent condition,
            # and retrying at 1 Hz just hammers the venue for the rest of the
            # round.
            #
            # A RESTING LIMIT THAT NOBODY CROSSED IS NOT THAT. It is the normal,
            # expected outcome of posting inside the spread -- the venue is
            # healthy and the next round may fill immediately. Pausing every arm
            # for order_cooldown_s on it cost ~150s on 2026-09-13 (3 unfills,
            # each 20s of wait plus 30s of pause) and, worse, the cooldown is
            # SHARED: an unfilled model-arm limit silently blocked the impulse
            # and cross arms, which fire in windows only seconds wide.
            #
            # `quote_above_ceiling` is the SAME class and was added 2026-09-15.
            # It is not a venue failure at all -- it is this bot refusing a
            # price. Measured live at 07:29:06 on eth-updown-5m-1789457100: the
            # book ran 0.53 -> 0.67 in six seconds, attempt 2's quote came back
            # over the ceiling, we correctly declined, and that refusal then
            # paused every family for 30s. Declining a bad price has to be free,
            # or the ceiling that protects the arm silently throttles it.
            no_cooldown = {"limit_unfilled", "quote_above_ceiling"}
            if getattr(self.broker, "last_failure", None) in no_cooldown:
                log.info("no fill for %s %s: %s (not a venue failure; "
                         "no cooldown)", r.slug, sig.side,
                         self.broker.last_failure)
                return fill
            self._order_cooldown_until = time.time() + settings.order_cooldown_s
            log.warning("order failed for %s %s after %d attempt(s); "
                        "pausing orders for %.0fs",
                        r.slug, sig.side, attempt + 1, settings.order_cooldown_s)
        return fill

    # -- settlement ---------------------------------------------------------
    async def backfill_settlements(self, limit: int = 5) -> int:
        """Fetch outcomes for rounds that have ended but have no recorded winner.

        `live_round()` only ever returns the round that is currently open, so by
        the time round N settles the feed has already moved to N+1. Without this
        backfill, recorded quotes never get an outcome attached and Phase 0
        collects data that cannot be scored -- which is the entire point of it.
        Runs in every mode, including `record`.
        """
        now_ms = int(time.time() * 1000)
        pending = self.store.unsettled_rounds(before_ms=now_ms - 10_000)
        done = 0
        for row in pending[:limit]:
            tid = row["market_topic_id"]
            if not tid:
                continue
            fresh = await self.market.by_topic_id(tid)
            if fresh and fresh.winner_name:
                self.store.upsert_round(round_row(fresh))
                # Stamp WHEN the outcome was learned. The column has always
                # existed and was never written, so every settled round in the
                # record read NULL -- an audit trail missing the one timestamp
                # that says how fresh the settlement is.
                self.store.conn.execute(
                    "UPDATE rounds SET settled_at_ms=? WHERE slug=?",
                    (int(time.time() * 1000), fresh.slug))
                self.store.conn.commit()
                done += 1
                log.info("settled %s -> %s (end_price=%s)",
                         fresh.slug, fresh.winner_name, fresh.end_price)
        return done

    async def _refresh_bankroll(self) -> None:
        """Re-read wallet equity so percentage sizing compounds intraday.

        `setup` reads the balance once, and without this the stake stays frozen
        at whatever the account held when the process started: after a winning
        run the bot would keep betting 4% of the ORIGINAL account, and after
        losses it would keep betting the same dollars against a smaller one --
        at a $76 start, a slide to $40 would have made the "4%" stake 7.6% of
        what was left, which is the exact failure mode fraction sizing exists
        to prevent.

        Used to be gated on `not self.open_fills`, because `walletBalance`
        excludes cash locked in an open position and reading it mid-round would
        UNDERSTATE equity and shrink the next stake. That gate is gone with the
        move to `equity()`, which adds `positionValue` and
        `totalClaimableAmount` back -- an open position is now counted at its
        value rather than looking like a hole in the account.

        The read is still a RECONCILIATION, not the answer -- see
        _reconcile_bankroll -- but the artefact it defends against is now much
        rarer, so a persistent gap is far more likely to be real.
        """
        if self.mode != "live" or getattr(self, "book", None) is None:
            return
        try:
            bal = await self.book.equity()
        except Exception as e:          # never let a read failure stop trading
            log.warning("bankroll refresh failed: %s: %s", type(e).__name__, e)
            return
        if bal <= 0:
            return                      # unreadable
        self._reconcile_bankroll(bal)

    def _reconcile_bankroll(self, read: float) -> None:
        """Adopt the venue's balance only when it is not a lag artefact.

        The venue's walletBalance does NOT track equity in real time: a stake
        leaves the wallet the instant the order fills, while the payout only
        lands after the redemption settles on-chain. Measured on a win of
        $0.4576 on a $1.53 stake, the balance read went $76.5027 -> $74.9727,
        i.e. down by EXACTLY the stake, with the payout simply absent.

        Sizing off that number shrinks the stake after every win -- the opposite
        of compounding, and it would have quietly capped the account's growth.
        So the bot keeps its own ledger (updated by settle_due) and only accepts
        the venue's figure when it disagrees across several reads in a row, which
        is what a deposit, a withdrawal or unmodelled fees look like. A lag
        resolves within a read or two and never gets that far.
        """
        tol = max(0.50, 0.01 * abs(self.bankroll))
        if abs(read - self.bankroll) <= tol:
            self._bankroll_drift = 0
            return
        if read > self.bankroll:
            # Money we did not account for: a deposit, or winnings the ledger
            # never saw. Nothing is lost by taking it immediately.
            log.info("bankroll $%.4f -> $%.4f (venue reports more than the "
                     "ledger; adopting)", self.bankroll, read)
            self.bankroll = read
            self._bankroll_drift = 0
            return
        self._bankroll_drift += 1
        if self._bankroll_drift < _BANKROLL_DRIFT_READS:
            log.info("bankroll read $%.4f is below the ledger $%.4f "
                     "(%d/%d reads; a redemption has not landed yet, keeping "
                     "the ledger for sizing)", read, self.bankroll,
                     self._bankroll_drift, _BANKROLL_DRIFT_READS)
            return
        log.warning("bankroll ledger $%.4f -> $%.4f (venue lower for %d reads "
                    "in a row: withdrawal or unmodelled fees, not lag)",
                    self.bankroll, read, self._bankroll_drift)
        self.bankroll = read
        self._bankroll_drift = 0

    def _fill_mode(self, fid: int) -> str:
        """The mode a FILL was booked under -- not the broker's mode.

        These used to be the same thing and no longer are: the double-down goes
        through the LIVE broker (real money) but is booked as mode='double', so
        settling it under the broker's mode would pool its P&L into the live
        book. That would make the experiment impossible to judge on its own
        ledger, which is the only reason it has its own cap.
        """
        try:
            row = self.store.conn.execute(
                "select mode from fills where id=?", (fid,)).fetchone()
        except Exception as e:                       # noqa: BLE001
            log.warning("could not read the mode of fill %s (%s); settling "
                        "under the broker's mode", fid, type(e).__name__)
            return self.broker.mode
        return row[0] if row and row[0] else self.broker.mode

    async def settle_due(self) -> None:
        now_ms = int(time.time() * 1000)
        settled = False
        for fid, (slug, fill) in list(self.open_fills.items()):
            row = self.store.get_round(slug)
            if not row or row["end_ms"] > now_ms - 5_000:
                continue
            winner = row["winner"]
            if not winner:
                fresh = await self.market.by_topic_id(row["market_topic_id"])
                if fresh:
                    self.store.upsert_round(round_row(fresh))
                    winner = fresh.winner_name
            if not winner:
                continue      # not resolved yet; try again next tick
            s = await self.broker.settle(fill, winner)
            if s is None:
                continue      # unverified and still unreadable; retry next tick
            self.store.insert("settlements", {
                "fill_id": fid, "slug": slug, "ts_ms": s.ts_ms,
                "won": int(s.won), "payout": s.payout, "pnl": s.pnl,
                "mode": self._fill_mode(fid), "void": int(s.void),
            })
            settled = True
            if s.void:
                # The venue never filled the order: no stake was deployed, so
                # there is no PnL, no guard update, and nothing to redeem.
                log.warning("SETTLE %s VOID: order never filled at the venue "
                            "(phantom fill dropped)", slug)
                # Give the slot back. _book_fill claimed it before the venue
                # disowned the order, and skipping this leaked it for the life
                # of the process -- one void was enough to silence the main
                # arm, whose pool is a set of size max_open_rounds. Not
                # on_settle: there is no P&L and no loss to record.
                reserved, pool = pool_for_mode(self._fill_mode(fid))
                self.guard.release(slug, reserved=reserved, pool=pool)
                del self.open_fills[fid]
                continue
            # Release the slot this fill is actually holding. Passing the main
            # the main one for a cross fill would leave `reserved_rounds` occupied
            # forever and the reserved slot permanently unavailable.
            reserved, pool = pool_for_mode(self._fill_mode(fid))
            self.guard.on_settle(slug, s.pnl, reserved=reserved, pool=pool)
            self.pnl.add(s.pnl, fill.stake, fill.fee_paid, s.won)
            # The LEDGER, moved immediately and exactly: the stake has already
            # left the wallet and the payout is on its way, so equity changes by
            # exactly the settlement P&L. Waiting for the venue to agree is what
            # made a win look like a loss (see _reconcile_bankroll).
            self.bankroll += s.pnl
            del self.open_fills[fid]
            # Live winnings sit as a claimable position until redeemed; they do
            # not credit the wallet on their own. This is the FAST path -- the
            # venue sometimes rejects a not-yet-final position here, and
            # sweep_claims() is the retry clock for whatever it drops.
            if s.won and self.broker.mode == "live":
                claimed = await self.broker.redeem()
                log.info("redeem -> %s", str(claimed)[:200])
            log.info("SETTLE %s %s winner=%s pnl=%+.4f | running: %s",
                     slug, "WON" if s.won else "LOST", winner, s.pnl, self.pnl)
        if settled:
            # A position closed, so the wallet balance is readable as equity
            # and the next stake should track it (see _refresh_bankroll).
            await self._refresh_bankroll()

    async def sweep_claims(self) -> None:
        """Retry winnings claims on a clock, not only on a win.

        `settle_due` redeems the moment a WON fill settles -- but that is the
        only trigger, so a claim that FAILS there is not retried until the
        next win. Both failure modes are on the record (2026-09-14): the
        venue rejects a position that is not yet final (`-9000 SYSTEM_ERROR`
        -- the claim call that fired 6 seconds after the first 15m round
        ended returned `failed_tokens: 1`, exactly this trap), and it rejects
        a claim already in flight ("already have a pending claim", 3x at
        19:25). Whatever the cause, the winnings sat claimable until
        something WON again: hours on a losing stretch, overnight when idle.

        The sweep is wallet-wide by construction: `redeem()` asks the venue
        what is CLAIMABLE (every family, every cause -- it also picks up
        claims a restart stranded) and returns without POSTing when the
        answer is empty, so the steady-state cost is one read per cooldown.
        The on-win redeem stays: it is the fast path when the venue accepts.
        """
        broker = getattr(self, "broker", None)
        if broker is None or broker.mode != "live":
            return
        now = time.time()
        if now - getattr(self, "_last_claim_sweep", 0.0) < _CLAIM_SWEEP_S:
            return
        self._last_claim_sweep = now
        claimed = await broker.redeem()
        if claimed:
            log.info("claim sweep -> %s", str(claimed)[:160])

    # -- run ----------------------------------------------------------------
    async def run(self) -> None:
        await self.setup()
        log.info("running in %s mode (poll %.1fs)", self.mode, settings.poll_interval_s)
        n = 0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                await self.tick()
                # Backfill outcomes at a low duty cycle -- rounds close every 5
                # minutes, so checking every ~15s is ample and keeps the poll light.
                if n % 15 == 0:
                    await self.backfill_settlements()
                if self.mode != "record":
                    await self.settle_due()
                    # Claims the venue rejected at settlement are retried on
                    # this clock, not on the next win (see sweep_claims).
                    await self.sweep_claims()
            except Exception as e:
                log.exception("tick failed: %s: %s", type(e).__name__, e)
            n += 1
            if n % 60 == 0:
                c = self.store.counts()
                log.info("stats: rounds=%d quotes=%d decisions=%d fills=%d | %s",
                         c["rounds"], c["quotes"], c["decisions"], c["fills"], self.pnl)
            elapsed = time.time() - t0
            # The cadence this arm's edge depends on. Reported rather than
            # assumed, because widening the websocket book to six families is
            # the one change in this repo measured to slow it (1.00s -> 4.15s).
            pm = getattr(self, "_poll_ms", None)
            if pm is None:
                pm = self._poll_ms = []
            pm.append(elapsed * 1000.0)
            if len(pm) >= 120:
                q = sorted(pm)
                log.info("poll cadence over %d ticks: median %.0fms p90 %.0fms "
                         "max %.0fms", len(q), q[len(q) // 2],
                         q[int(len(q) * 0.9)], q[-1])
                self._poll_ms = []
            # In impulse mode this is spent waiting on the price feed rather
            # than on the clock, so the trigger fires ~20 ms after the move
            # instead of up to a poll interval later (see _idle).
            await self._idle(max(0.05, settings.poll_interval_s - elapsed))
        for f in self.spots.values():
            await f.stop()
        # Stop the book feed BEFORE the session goes: otherwise its reconnect
        # loop sees "Session is closed", logs a warning and backs off on every
        # shutdown, which reads like a feed fault in the log when it is just
        # the process exiting.
        wsb = getattr(self, "ws_book", None)
        if wsb is not None:
            wsb.stop()
        task = getattr(self, "_ws_task", None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log.info("stopped. final: %s", self.pnl)

    def stop(self) -> None:
        self._stop.set()


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("record", "paper", "live"),
                    default=settings.trading_mode)
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = run forever)")
    ap.add_argument("--resume", action="store_true",
                    help="live mode: clear a persisted risk halt and trade again "
                         "with a fresh daily loss budget (logged loudly)")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    store = Store(settings.db_path)
    async with aiohttp.ClientSession() as session:
        bot = Bot(args.mode, store, session)
        bot.resume_guard = args.resume
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(s, bot.stop)
        task = asyncio.create_task(bot.run())
        if args.duration:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=args.duration)
            bot.stop()
        await task
    store.close()


if __name__ == "__main__":
    asyncio.run(amain())
