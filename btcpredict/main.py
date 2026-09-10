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
import signal
import time

import aiohttp

from btcpredict.calibration.report import PnLSummary
from btcpredict.config import MARKETS, settings
from btcpredict.edge.signal import (FeeModel, Signal, evaluate,
                                    evaluate_hybrid, kelly_stake)
from btcpredict.execution.base import Fill
from btcpredict.execution.live import RETRYABLE_FAILURES, LiveBroker
from btcpredict.execution.paper import PaperBroker
from btcpredict.feeds.binance_spot import SpotFeed, fetch_klines
from btcpredict.feeds.market_feed import MarketFeed, Round
from btcpredict.models import PriceHistory, build_model
from btcpredict.pricing.digital import implied_sigma
from btcpredict.pricing.vol import VolEstimator
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

log = logging.getLogger("btcpredict")


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
        # One spot feed and volatility estimator per underlying: the 15m ETH/BNB
        # markets carry 30-450x less volume than BTC 5m, which is where looser
        # pricing is more likely, but they need their own price and sigma.
        self.symbols = sorted({sym for _, sym, _ in MARKETS})
        self.spots: dict[str, SpotFeed] = {
            sym: SpotFeed(session, symbol=sym) for sym in self.symbols}
        self.vols: dict[str, VolEstimator] = {}
        # Rolling 1m-bar history per symbol, feeding the strategy models.
        self.histories: dict[str, PriceHistory] = {}
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
        )
        # --resume deliberately clears a persisted halt (fresh daily budget).
        self.resume_guard = False
        self.broker = None
        # Read-only client for the authenticated order book. Available in every
        # mode, so paper trading prices against the same book as live.
        self.book: LiveBroker | None = None
        self.open_fills: dict[str, tuple[int, Fill]] = {}   # slug -> (fill_id, Fill)
        self.bankroll = settings.bankroll_usdt
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
            bal = await self.broker.balance()
            log.info("LIVE mode. wallet %s balance $%.4f",
                     self.broker.wallet_address, bal)
            if bal < settings.stake_usdt:
                raise SystemExit(f"balance ${bal:.4f} below stake ${settings.stake_usdt}")

        if self.mode != "record":
            self._restore_open_positions()
        if self.mode == "live":
            self._seed_guard_state()
            if self.resume_guard:
                self.guard.resume()
                # Persist the reset: without a marker the next restart
                # re-seeds today's old settlements and re-halts the bot, so
                # the reset would only live until the next crash/restart.
                self.store.set_meta("guard_reset_ms",
                                    str(int(time.time() * 1000)))

    def _seed_guard_state(self) -> None:
        """Carry today's live PnL and loss streak into the fresh guard.

        Without this, a restart after the daily cap is breached lets the bot
        trade again on a day that already lost its budget.
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
        rows = self.store.query(
            "SELECT s.pnl, s.ts_ms FROM settlements s "
            "JOIN fills f ON f.id = s.fill_id WHERE f.mode = ? "
            "AND COALESCE(s.void, 0) = 0 AND s.ts_ms >= ? "
            "ORDER BY s.ts_ms DESC LIMIT 50", (mode, reset_ms))
        pnl = sum(r["pnl"] for r in rows
                  if time.strftime("%Y-%m-%d", time.gmtime(r["ts_ms"] / 1000)) == today)
        streak = 0
        for r in rows:                      # newest first
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
        rows = self.store.query(
            "SELECT f.* FROM fills f "
            "LEFT JOIN settlements s ON s.fill_id = f.id "
            "WHERE s.id IS NULL AND f.mode = ?", (self.broker.mode,))
        for row in rows:
            fill = Fill(
                slug=row["slug"], side=row["side"],
                requested_px=row["requested_px"], filled_px=row["filled_px"],
                shares=row["shares"], stake=row["stake"], fee_paid=row["fee_paid"],
                order_id=row["order_id"] or "", ts_ms=row["ts_ms"],
            )
            self.open_fills[row["slug"]] = (row["id"], fill)
            self.guard.on_open(row["slug"])
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

    async def refresh_vol_if_stale(self) -> None:
        if time.time() - self._last_vol_refresh < 60.0:
            return
        try:
            import math
            for sym, v in self.vols.items():
                klines = await fetch_klines(self.session, "1m", 5, symbol=sym)
                for k in klines[:-1]:      # skip the still-forming bar
                    o, c = float(k[1]), float(k[4])
                    if o > 0 and c > 0:
                        v.add(math.log(c / o))
                self.histories[sym].merge(klines)
            self._last_vol_refresh = time.time()
        except Exception as e:
            log.warning("vol refresh failed: %s: %s", type(e).__name__, e)

    # -- main tick ----------------------------------------------------------
    async def tick(self) -> None:
        await self.refresh_vol_if_stale()
        found = await self.market.rounds_by_prefixes([p for p, _, _ in MARKETS])
        # Record every market; only the primary one may trade, so that a
        # low-liquidity market cannot be traded before it has been evaluated.
        primary = MARKETS[0][0]
        for prefix, sym, _ in MARKETS:
            r = found.get(prefix)
            if r:
                await self._record(r, sym, trade=(prefix == primary))

    async def _record(self, r, symbol: str, *, trade: bool) -> None:
        self.store.upsert_round(round_row(r))
        self.spot = self.spots[symbol]
        self.vol = self.vols[symbol]

        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        up, down = r.up, r.down
        if not up or not down or r.strike is None:
            return

        spot = self.spot.price
        sigma = self.vol.sigma_1m()

        # Buy prices come from the real book (the ask), not the displayed
        # last-trade price, which was observed 2-3 cents away from the mid.
        up_bid = up_ask = down_bid = down_ask = None
        if self.book and r.market_id:
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
        buy_up = up_ask if up_ask is not None else up.price
        buy_down = down_ask if down_ask is not None else down.price
        # No spread guess when the ask is the actual executable price.
        half_spread = 0.0 if up_ask is not None else settings.assumed_half_spread

        self.store.insert("quotes", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau,
            "up_price": up.price, "down_price": down.price,
            "up_chance": up.chance, "down_chance": down.chance,
            "liquidity": r.liquidity, "trade_volume": r.trade_volume,
            "spot": spot, "spot_ts_ms": self.spot.ts_ms,
            "oracle": None, "oracle_ts_ms": None,
            "strike": r.strike,
            "up_bid": up_bid, "up_ask": up_ask,
            "down_bid": down_bid, "down_ask": down_ask,
            "fee_rate_bps": r.fee_rate_bps,
        })

        if self.mode == "record" or not trade or spot is None or tau <= 0:
            return

        hist = self.histories[symbol]
        hist.add_tick(spot)
        pred = self.model.predict(hist.ctx(
            price=spot, strike=r.strike, tau_s=tau, sigma_1m=sigma))
        if not pred.emitted or not (0.0 < pred.p_up < 1.0):
            log.debug("model %s holds on %s: %s", self.model.name, r.slug, pred.reason)
            return
        fair_up = pred.p_up
        sig_kw = dict(
            # fee=None -> per-quote fee from the measured curve
            # (fee_bps = 180 * min(1, (1-px)/px)); a flat fallback would
            # misprice every side except px~0.50.
            fee=None,
            half_spread=half_spread,
            safety_margin=settings.safety_margin,
            fee_model=FeeModel.NOTIONAL,
            max_quote=settings.max_quote,
            min_quote=settings.min_quote,
            min_edge=settings.min_edge,
            edge_haircut=settings.edge_haircut,
        )
        if self.up_model is not None:
            pred_up = self.up_model.predict(hist.ctx(
                price=spot, strike=r.strike, tau_s=tau, sigma_1m=sigma))
            up_fair = (pred_up.p_up if pred_up.emitted
                       and 0.0 < pred_up.p_up < 1.0 else fair_up)
            sig = evaluate_hybrid(fair_up, up_fair, buy_up, buy_down, **sig_kw)
        else:
            sig = evaluate(fair_up, buy_up, buy_down, **sig_kw)

        # Rising-regime gate for Up (see config.up_regime_gate): when the last
        # hour is already up, the venue has priced Up correctly and our Up edge
        # is anti-informative (measured WR 0.50 rising vs 0.82 flat/down).
        if sig.act and sig.side == "Up" and settings.up_regime_gate > 0:
            ret = hist.one_hour_return()
            if ret is not None and ret > settings.up_regime_gate:
                sig = Signal(None, sig.fair, sig.quote, sig.edge, sig.hurdle,
                             sig.ev_per_dollar, False,
                             f"Up skipped: 1h ret {ret:+.4f} > "
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
        if sig.act and streak < settings.signal_persistence:
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
        elif not self.spot.is_fresh(settings.max_spot_age_s):
            gate_reason = f"spot stale ({self.spot.age_s:.1f}s)"
        elif self.broker.mode == "live" and sig.act and self.book:
            # The venue is the ground truth. If it already shows a position in
            # this round (a fill the bot never booked, e.g. after a restart or
            # an unverifiable order), buying again would double the exposure.
            pos_tokens = await self._venue_position_tokens()
            if pos_tokens is None:
                gate_reason = "venue position list unavailable"
            elif up.token_id in pos_tokens or down.token_id in pos_tokens:
                gate_reason = "venue already has a position for this round"
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
            # Kelly is the sizing rule; the venue minimum is the floor. Below that
            # minimum there is no way to express a smaller bet, so fall back to the
            # configured stake rather than silently scaling a tiny Kelly bet up.
            stake = kelly_stake(
                sig.fair, sig.quote, bankroll=self.bankroll,
                fraction=settings.kelly_fraction,
                min_stake=settings.min_order_usdt, max_stake=settings.max_stake_usdt,
            )
            if stake <= 0:
                stake = settings.stake_usdt
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
            return

        token = up.token_id if sig.side == "Up" else down.token_id

        fill = await self._place_order(
            r, sig,
            fair_up=sig.fair_up_model if sig.fair_up_model is not None else fair_up,
            token=token, stake=stake)
        if not fill:
            return
        self.guard.on_open(r.slug)
        self.store.conn.execute("UPDATE decisions SET acted=1 WHERE id=?", (did,))
        self.store.conn.commit()
        fid = self.store.insert("fills", {
            "decision_id": did, "slug": r.slug, "ts_ms": fill.ts_ms,
            "mode": self.broker.mode, "side": fill.side,
            "requested_px": fill.requested_px, "filled_px": fill.filled_px,
            "shares": fill.shares, "stake": fill.stake, "fee_paid": fill.fee_paid,
            "order_id": fill.order_id, "raw": fill.raw,
        })
        self.open_fills[r.slug] = (fid, fill)
        # implied_sigma inverts P(up), so it must always be fed the Up quote --
        # passing the chosen side's quote breaks the inversion when that side is Down.
        iv = implied_sigma(up.price, spot, r.strike, tau)
        log.info("OPEN %s %s stake=$%.2f @%.3f | %s | mkt_iv=%s realised=%.5f",
                 r.slug, fill.side, fill.stake, fill.filled_px, sig,
                 f"{iv:.5f}" if iv else "n/a", sigma)

    async def _place_order(self, r, sig, *, fair_up: float, token: str,
                           stake: float) -> Fill | None:
        """Place one order, retrying a confirmed rejection a bounded number of times.

        Returns the fill, or None when the order could not be placed -- having
        set the order cooldown if the failure was an actual rejection rather
        than a gate refusing to trade.
        """
        # A FOK order fails whenever the book moves between the quote and the
        # execution, and that is a transient miss rather than a reason to give
        # up the round -- so make a bounded number of attempts. Each one redoes
        # the whole pre-trade path: a spent quoteId cannot be re-sent, and both
        # the edge and the candle-open window can evaporate in the second or
        # two a failed attempt costs.
        attempts = max(1, settings.order_max_attempts)
        fill = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(settings.order_retry_delay_s)

            # Re-check against a live quote before committing. The venue's fee is
            # dynamic and its executable price can differ from the book, so the
            # decision above used estimates. get-quote does not execute, so this
            # costs nothing but a round trip -- and the quote is REUSED for the
            # order itself so placing it costs no extra latency (quote TTL is ~5s).
            quote = None
            if self.broker.mode == "live":
                quote = await self.broker.get_quote(token, stake)
                if not quote:
                    log.warning("no pre-trade quote for %s (attempt %d/%d)",
                                r.slug, attempt + 1, attempts)
                    continue
                real_px = float(quote.get("averagePrice") or sig.quote)
                real_fee = float(quote.get("feeRateBps") or settings.fallback_fee_bps) / 10_000.0
                recheck = evaluate(
                    fair_up, real_px if sig.side == "Up" else 1.0 - real_px,
                    1.0 - real_px if sig.side == "Up" else real_px,
                    fee=real_fee, half_spread=0.0,
                    safety_margin=settings.safety_margin, fee_model=FeeModel.NOTIONAL,
                    max_quote=settings.max_quote, min_quote=settings.min_quote,
                    min_edge=settings.min_edge,
                    edge_haircut=settings.edge_haircut)
                if not (recheck.act and recheck.side == sig.side):
                    # The edge is gone at the live price. Retrying would only
                    # be chasing a trade the gate has already refused.
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
            if (self.broker.mode == "live" and settings.trade_on_candle_open):
                sec = sec_into_candle(int(time.time() * 1000))
                if sec >= settings.candle_open_window_s:
                    log.info("SKIP %s %s: slipped past candle open (%ds); order withheld",
                             r.slug, sig.side, sec)
                    return

            kw = {"quote": quote} if self.broker.mode == "live" else {}
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
                done += 1
                log.info("settled %s -> %s (end_price=%s)",
                         fresh.slug, fresh.winner_name, fresh.end_price)
        return done

    async def settle_due(self) -> None:
        now_ms = int(time.time() * 1000)
        for slug, (fid, fill) in list(self.open_fills.items()):
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
                "mode": self.broker.mode, "void": int(s.void),
            })
            if s.void:
                # The venue never filled the order: no stake was deployed, so
                # there is no PnL, no guard update, and nothing to redeem.
                log.warning("SETTLE %s VOID: order never filled at the venue "
                            "(phantom fill dropped)", slug)
                del self.open_fills[slug]
                continue
            self.guard.on_settle(slug, s.pnl)
            self.pnl.add(s.pnl, fill.stake, fill.fee_paid, s.won)
            del self.open_fills[slug]
            # Live winnings sit as a claimable position until redeemed; they do
            # not credit the wallet on their own.
            if s.won and self.broker.mode == "live":
                claimed = await self.broker.redeem()
                log.info("redeem -> %s", str(claimed)[:200])
            log.info("SETTLE %s %s winner=%s pnl=%+.4f | running: %s",
                     slug, "WON" if s.won else "LOST", winner, s.pnl, self.pnl)

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
            except Exception as e:
                log.exception("tick failed: %s: %s", type(e).__name__, e)
            n += 1
            if n % 60 == 0:
                c = self.store.counts()
                log.info("stats: rounds=%d quotes=%d decisions=%d fills=%d | %s",
                         c["rounds"], c["quotes"], c["decisions"], c["fills"], self.pnl)
            elapsed = time.time() - t0
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(0.1, settings.poll_interval_s - elapsed))
        for f in self.spots.values():
            await f.stop()
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
