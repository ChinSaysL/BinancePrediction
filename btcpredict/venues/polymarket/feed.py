"""A `MarketFeed`-shaped source backed by Polymarket.

WHY IT IS A DROP-IN. `main.py` touches its round source in exactly two ways:

    found = await self.market.rounds_by_prefixes([...])     # discovery
    fresh = await self.market.by_topic_id(row["market_topic_id"])  # settlement

so swapping the venue is one assignment, not an edit at every call site. This
class provides that same shape over `markets.py`.

TWO THINGS IT DELIBERATELY DOES DIFFERENTLY.

1. **The strike still comes from the wrapper.** Polymarket publishes NO
   reference price for this family -- its resolution source is the Chainlink
   stream -- while both venues compare against the price at the START of the
   range. So `variantData.startPrice` is the right number for both, and a
   Binance feed is injected for that ONE field and nothing else. If it is
   absent the strike is None, which the arms treat as unknown rather than as
   zero.

2. **It never answers `by_topic_id`.** A Binance `marketTopicId` is meaningless
   here, and a Polymarket round carries none. Returning a round for topic 0
   would fetch an UNRELATED market and settle a real position against it. The
   caller routes settlement by slug instead (see `by_slug`).

BLOCKING I/O. `markets.py` is synchronous urllib by design (it is also used by
throwaway tools). Every call here goes through `asyncio.to_thread` so a slow or
hung gamma request cannot stall the trading loop -- a 20 s blocking call inside
the event loop would freeze order placement, which is the one thing this bot
cannot afford.
"""

from __future__ import annotations

import asyncio
import logging
import time

from btcpredict.feeds.market_feed import Round
from btcpredict.venues.polymarket import markets as M
from btcpredict.venues.polymarket.rounds import DEFAULT_SYMBOL, to_round

log = logging.getLogger(__name__)

#: Short on purpose: this runs inside the trading loop. gamma is edge-cached
#: (measured 8.7 ms), so a request this slow is already a fault.
FETCH_TIMEOUT_S = 5.0


class PmMarketFeed:
    """Round discovery + settlement read-back for `btc-updown-5m` on Polymarket."""

    #: How many times to ask the wrapper for a round's strike before accepting
    #: that it does not have one. Bounded because one ask is a PAGINATED sweep
    #: of the wrapper's live list (30 pages, ~4s, when the prefix is not found).
    #: At `recheck_s` = 5 s this is a minute of trying, which covers the roll.
    _STRIKE_MAX_TRIES = 12

    def __init__(self, session=None, strike_feed=None,
                 symbol: str = DEFAULT_SYMBOL,
                 timeout: float = FETCH_TIMEOUT_S,
                 recheck_s: float = 5.0,
                 strike_backfill=None,
                 strike_backfill_async=None) -> None:
        # `session` is accepted and unused: the wrapper's feed needs an
        # aiohttp session, this one does not. Keeping it in the signature means
        # main.py can construct either source the same way.
        self.session = session
        self.strike_feed = strike_feed
        #: FALLBACK strike source, `Callable[[slug], float | None]`. It exists
        #: for the families the wrapper's feed does not LIST at all: Binance's
        #: prediction feed carries btc/eth/bnb up/down only, and Polymarket's
        #: gamma carries no reference price (its resolution source is a
        #: Chainlink stream URL). So for sol/xrp/doge the strike had NO source,
        #: and a round without a strike never reaches `_mkt` -- the families
        #: were recorded and could not trade (`not live: sol, xrp, doge`).
        #:
        #: The hook lives in main.py because it needs the live spot tape, which
        #: is per-symbol and lives there. It must FAIL CLOSED: a wrong strike is
        #: strictly worse than none, because the arms gate on `strike is None`
        #: and `crossed_side` would look for a crossing of a level this market
        #: was never settled against.
        self.strike_backfill = strike_backfill
        #: SECOND-CHANCE strike source, `Callable[[slug], Awaitable[float|None]]`.
        #:
        #: ⛔ WHY A SYNC BACKFILL IS NOT ENOUGH. The tape-based backfill above
        #: reads the newest tick at or before the open off the LIVE spot tape
        #: and fails closed when there is none -- correct, but a process that
        #: BOOTS after the open has no tick before it, so that round is
        #: unfireable for its whole life and nothing says so.
        #:
        #: MEASURED 2026-09-17: the 10:59:43 -> 11:00:01 restart opened exactly
        #: one round across the gap (`sol-updown-5m-1789642800`, open 11:00:00),
        #: and it was the ONLY one of the 63 rounds since 10:30 with no strike.
        #: A REST kline answers retrospectively and does not care when this
        #: process started, which is the property that is missing here.
        #:
        #: Async on purpose: it costs a network call, and `_strike_for` is
        #: already awaited, so the poll loop is never blocked by it.
        self.strike_backfill_async = strike_backfill_async
        self.symbol = symbol
        self.timeout = timeout
        #: How often to re-ask gamma about an unresolved round.
        self.recheck_s = recheck_s
        self._rounds: dict[str, Round] = {}
        self._strikes: dict[str, float | None] = {}
        self._checked_at: dict[str, float] = {}
        #: slug -> the venue's own round object. `PmBroker.remember` needs the
        #: PmRound (it carries tick and min_order, which the arm-shaped Round
        #: does not), and the broker's cache has to follow the ROLL or the
        #: second round after boot cannot be priced at all.
        self._pm_by_slug: dict[str, M.PmRound] = {}
        #: When the strike was last ASKED for (a miss is not cached, so this is
        #: the only thing standing between a retry and a sweep per poll).
        self._strike_checked: dict[str, float] = {}
        self._strike_tries: dict[str, int] = {}
        #: slug -> the round actually opened, so the attempt budget can be
        #: restarted ONCE at the open. See `_strike_for`.
        self._strike_opened: dict[str, bool] = {}

    def pm_round_for(self, slug: str) -> "M.PmRound | None":
        """The venue's own round for `slug`, or None if never discovered."""
        return self._pm_by_slug.get(slug)

    # -- strike (the one field this venue cannot supply) -------------------
    async def _strike_for(self, slug: str, start_ts: int) -> float | None:
        if self._strikes.get(slug) is not None:
            return self._strikes[slug]
        # A miss is retried -- but only on a clock, and only so many times.
        # `main.py` already marks a strike-less round STALE and re-fetches it,
        # and the primary family is re-fetched inline EVERY POLL, so an
        # unthrottled retry would run a paginated wrapper sweep up to once a
        # second (measured cost of a sweep that does not find its prefix: 30
        # pages, ~4s). Once the round is over the answer cannot improve, so
        # stop asking.
        now = time.time()
        if now - self._strike_checked.get(slug, 0.0) < self.recheck_s:
            return None
        # ⛔ THE BUDGET RESTARTS AT THE OPEN, and this is not a nicety.
        # A round is often DISCOVERED BEFORE IT OPENS, and every attempt made
        # before the open is doomed by construction -- the backfill refuses
        # (`start_ms > now`: the newest tick is a PRE-ROLL price) and the
        # wrapper may not have listed the round yet. The refresh loop runs for
        # any strike-less family, and with `recheck_s = 5.0` twelve such
        # attempts are spent inside a minute, so `_STRIKE_MAX_TRIES` would
        # abandon the round BEFORE it ever became priceable, for its whole
        # life. Measured live 2026-09-17: sol/xrp/doge sat at `not live` for
        # round after round while the strike was obtainable at the open.
        # One full budget per round, starting when the round actually opens.
        # Pre-open attempts stay capped at the same number, so the sweep cost
        # is unchanged -- it just cannot spend the post-open budget.
        if start_ts * 1000 <= now * 1000 and not self._strike_opened.get(slug):
            self._strike_opened[slug] = True
            self._strike_tries[slug] = 0
        if self._strike_tries.get(slug, 0) >= self._STRIKE_MAX_TRIES:
            return None
        self._strike_checked[slug] = now
        self._strike_tries[slug] = self._strike_tries.get(slug, 0) + 1
        strike = None
        if self.strike_feed is not None:
            prefix = slug.rsplit("-", 1)[0]          # btc-updown-5m
            try:
                found = await self.strike_feed.rounds_by_prefixes([prefix])
            except Exception as exc:                 # noqa: BLE001
                log.warning("strike lookup failed for %s: %s: %s",
                            slug, type(exc).__name__, exc)
                found = None
            r = (found or {}).get(prefix)
            # IDENTITY IS REQUIRED, not assumed. The wrapper feed has no slug
            # lookup -- its only entry point sweeps the venue's LIVE list -- so
            # it answers with the CURRENT round for whatever prefix we ask.
            # Accepting that answer for a different slug attaches one round's
            # reference price to another round's market: a silently WRONG
            # number, and the arms gate on `strike is None`, so a wrong strike
            # is strictly worse than no strike -- `crossed_side` would look for
            # a crossing of a level this market was never settled against.
            if r is not None and getattr(r, "slug", None) == slug:
                strike = getattr(r, "strike", None)
        if strike is None and self.strike_backfill is not None:
            # THE FAMILIES THE WRAPPER DOES NOT LIST. `found` is empty for
            # every prefix the wrapper's feed carries no market for, and for
            # those the backfill is not a second-best source -- it is the only
            # one. One pass is enough: a hit is cached below, so a miss-sweep
            # costs ONE wrapper sweep per slug, not one per retry.
            try:
                strike = self.strike_backfill(slug)
            except Exception as exc:                     # noqa: BLE001
                log.warning("strike backfill failed for %s: %s: %s",
                            slug, type(exc).__name__, exc)
                strike = None
        if strike is None and self.strike_backfill_async is not None:
            # THE ROUND THAT OPENED ACROSS A RESTART. The tape cannot reach a
            # price from before this process existed, so the sync backfill
            # correctly refuses; this one asks the venue's own history instead.
            # See `strike_backfill_async` in `__init__` for the measurement.
            try:
                strike = await self.strike_backfill_async(slug)
            except Exception as exc:                     # noqa: BLE001
                log.warning("strike kline backfill failed for %s: %s: %s",
                            slug, type(exc).__name__, exc)
                strike = None
        if strike is not None:
            self._strikes[slug] = strike
        return strike

    async def _wrap(self, pm: M.PmRound) -> Round:
        self._pm_by_slug[pm.slug] = pm
        r = self._rounds.get(pm.slug)
        if r is None:
            strike = await self._strike_for(pm.slug, pm.start_ts)
            r = to_round(pm, symbol=self.symbol, strike=strike)
            self._rounds[pm.slug] = r
        elif r.strike is None:
            # REFILL A MISSING STRIKE ON THE CACHED ROUND. The round object is
            # cached on first sight, so a strike that was unavailable then is
            # unavailable forever -- and main.py's staleness rule explicitly
            # re-fetches a round whose `strike is None`, expecting the answer to
            # appear. Returning the same object unchanged defeated that rule
            # silently: measured 2026-09-16, 4 of 8 rounds carried a strike and
            # 4 never did, for the whole life of each round.
            r.strike = await self._strike_for(pm.slug, pm.start_ts)
        return r

    async def _fetch(self, ts: float) -> M.PmRound | None:
        """gamma, off the event loop."""
        return await asyncio.to_thread(M.fetch_round, ts)

    # -- discovery ---------------------------------------------------------
    async def live_round(self) -> Round | None:
        cur, _nxt = await asyncio.to_thread(M.current_and_next)
        return None if cur is None else await self._wrap(cur)

    async def rounds_by_prefixes(self, prefixes) -> dict[str, Round]:
        """The CURRENT round of each requested family.

        Per-FAMILY on purpose. This used to call `current_and_next()` once --
        with the BTC series baked into `markets.SERIES` -- and then test every
        requested prefix against that one slug, so only `btc-updown-5m` could
        ever match. The venue lists all six families the arms trade (verified
        live 2026-09-16: `eth-updown-5m` and `bnb-updown-5m` both resolve to a
        market), so returning nothing for five of them is what left those
        families unpriceable and refused by the cross arm.
        """
        out: dict[str, Round] = {}
        now = time.time()
        for p in prefixes:
            cur, _nxt = await asyncio.to_thread(M.current_and_next, now, p)
            if cur is not None and cur.slug.startswith(p):
                out[p] = await self._wrap(cur)
        return out

    async def by_slug(self, slug: str) -> Round | None:
        """Re-fetch one round, resolving its outcome if it has closed.

        Settlement must use THIS, not `by_topic_id`. Polymarket pays by its own
        60 s TWAP rule and the wrapper's point rule disagrees on 10.4% of
        rounds, so borrowing the wrapper's winner books P&L against a
        different bet.
        """
        if not slug:
            return None
        try:
            ts = float(slug.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return None
        # The family is IN the slug, so settlement needs no configuration: the
        # 15m families align to a 900 s boundary and asking for the 5m one
        # would fetch a market that does not exist.
        series = slug.rsplit("-", 1)[0]

        r = self._rounds.get(slug)
        if r is None:
            # `fetch_round_any`, not `fetch_round`: a round settles AFTER the
            # bell, when the venue no longer lists it as open, so the
            # open-only lookup returns None and the round becomes permanently
            # unresolvable -- the state of every round that was not already
            # current when this process started. It is not a rare path: the
            # feed's cache is empty on every restart.
            pm = await asyncio.to_thread(M.fetch_round_any, ts, series)
            if pm is None:
                return None
            r = await self._wrap(pm)

        # A settled round is final; never re-read it.
        if r.winner_name:
            return r

        # `settle_due` runs every tick (~100 ms). Resolution takes ~58 s after
        # the bell, so re-asking every tick would be ~600 pointless gamma calls
        # per round. Re-check on an interval instead; the caller retries.
        last = self._checked_at.get(slug, 0.0)
        if time.time() - last < self.recheck_s:
            return r
        self._checked_at[slug] = time.time()

        winner, status = await asyncio.to_thread(M.settled_winner, ts, series)
        if winner:
            for o in r.outcomes:
                o.winner = (o.name.lower() == str(winner).lower())
        else:
            log.debug("pm %s not resolved yet (%s)", slug, status)
        return r

    async def by_topic_id(self, market_topic_id) -> Round | None:
        """NOT SUPPORTED, and deliberately loud about it.

        A wrapper `marketTopicId` has no meaning on this venue and a Polymarket
        round carries none. Answering numerically (topic 0, say) would return an
        unrelated market whose `winner_name` would then be booked against a real
        position -- a silent, expensive wrong answer. Returning None makes the
        caller retry, which is the correct failure mode for "not resolved yet".
        """
        log.warning("by_topic_id(%r) called on the Polymarket feed; settlement "
                    "must go by slug (see by_slug)", market_topic_id)
        return None

    def prune(self, keep=None) -> None:
        keep = set(keep or ())
        for d in (self._rounds, self._strikes, self._checked_at,
                  self._strike_checked, self._strike_tries,
                  self._strike_opened, self._pm_by_slug):
            for k in [k for k in d if k not in keep]:
                del d[k]


__all__ = ["PmMarketFeed", "FETCH_TIMEOUT_S"]
