"""Polymarket rounds, in the shape the arms already consume.

WHY THIS EXISTS. `main.py` does not read a venue-neutral round. It reads
`btcpredict.feeds.market_feed.Round` and its `.up` / `.down` outcomes:

    up, down = r.up, r.down
    token = up.token_id if sig.side == "Up" else down.token_id
    ...
    fill = await self.broker.buy(slug=r.slug, side=sig.side, token_id=token, ...)

So a broker swap alone cannot move the bot to Polymarket: the ROUND SOURCE has
to produce that type. Without this module `r.up` is None and the arm evaluates
against nothing -- no error, just silence.

WHAT THE TWO VENUES SHARE, AND WHAT THEY DO NOT
-----------------------------------------------
Shared, and verified byte for byte: the slug family (`btc-updown-5m-<unix>`),
the 5-minute boundary, the `Up`/`Down` outcome names, and the Chainlink BTC/USD
oracle family.

NOT shared -- the settlement rule (see `markets.py`), and this is the one that
must never be papered over:
    Binance wrapper   the POINT price at round end vs the round's startPrice
    Polymarket        a 60 s Chainlink TWAP vs the range's opening price
Measured over 48 settled rounds, the two disagree on **10.4%**.

That has two consequences, and they pull in opposite directions:

1. **The STRIKE is safe to borrow.** Both rules compare against the price at
   the START of the range, so the wrapper's `variantData.startPrice` is the
   right number for this venue. Polymarket carries no reference price in gamma
   at all, because its resolution source is the Chainlink stream itself.

2. **The WINNER must NOT be borrowed.** A fill placed on Polymarket is paid by
   Polymarket's rule. Settling it against the wrapper's point winner books P&L
   against a different bet ~1 round in 10. Use `settle_winner()` below, which
   reads gamma's own `umaResolutionStatus == "resolved"`.

So: strike from Binance, orders and settlement from Polymarket.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from btcpredict.feeds.market_feed import Outcome, Round
from btcpredict.venues.polymarket import markets as M

#: What the arms filter on. Polymarket lists a BTC/USD 5-minute up-down market
#: and nothing else this bot trades, so every round carries this symbol and the
#: "BTC only" restriction is structural rather than a policy that can drift.
DEFAULT_SYMBOL = "BTCUSDT"

#: Fee metadata. The venue's real schedule is `0.07*p*(1-p)`, taker-only --
#: NOT a flat bps. This field is carried for report symmetry only; the broker
#: prices the fee from the schedule, and `get_quote` reports it against notional.
FEE_RATE_BPS_NOTE = 0


def to_round(pm: M.PmRound, *, symbol: str = DEFAULT_SYMBOL,
             strike: float | None = None,
             end_ms: int | None = None) -> Round:
    """A `market_feed.Round` backed by a Polymarket market.

    `strike` is a PARAMETER and not derived here on purpose. Polymarket does
    not publish a reference price for this family -- the resolution source is
    the Chainlink stream -- so the honest source is the wrapper's
    `variantData.startPrice`, which both venues compare against. Passing None
    is valid and means "unknown"; the arms that need it will refuse rather than
    invent one.
    """
    end_ms = int(end_ms if end_ms is not None
                 else (pm.end_ms or (pm.start_ts + pm.period_s) * 1000))
    outcomes = [
        Outcome(name="Up", price=0.0, chance=0.0, token_id=pm.up_token,
                market_outcome_id=0, winner=None),
        Outcome(name="Down", price=0.0, chance=0.0, token_id=pm.down_token,
                market_outcome_id=1, winner=None),
    ]
    return Round(
        slug=pm.slug,
        # `market_topic_id` is an integer in the wrapper's payload and is only
        # ever logged, never sent to a venue. 0 is a deliberate "not
        # applicable", not a guess at a real id.
        market_topic_id=0,
        # `market_id` is the WRAPPER's id space. It is threaded into
        # `best_prices(token_id, market_id)` and ignored by the Polymarket
        # broker, but filling it with a Binance id would be a lie that reads
        # like a lookup failure later.
        market_id=None,
        condition_id=pm.condition_id or None,
        symbol=symbol,
        start_ms=pm.start_ts * 1000,
        end_ms=end_ms,
        strike=strike,
        end_price=None,          # populated by settle_winner(), from gamma
        price_feed_id=None,      # the Chainlink stream id lives in gamma's
                                 # cryptoMarketConfig; not needed to trade
        feed_provider=M.RESOLUTION,
        fee_rate_bps=FEE_RATE_BPS_NOTE,
        liquidity=pm.liquidity,
        trade_volume=0.0,
        status="open",
        trading_status="open",
        live_status="open",
        outcomes=outcomes,
    )


def settle_winner(pm: M.PmRound) -> str | None:
    """`"Up"`/`"Down"` from POLYMARKET's own resolution, or None.

    Strict on purpose: `umaResolutionStatus == "resolved"` AND outcome prices
    that are a clean 0/1. Anything else means the market has closed but the
    outcome is not written, and booking a fill against it would invent P&L.

    Never substitute the wrapper's winner here. The two rules disagree on 10.4%
    of rounds, and this one is the rule that pays.
    """
    return M.settled_winner_strict(pm.start_ts)


@dataclass(slots=True)
class PmRoundSource:
    """Discovers the current (and next) Polymarket round.

    Both exist ahead of time -- gamma creates markets roughly a day early -- so
    there is no roll race and no need to re-query per tick.
    """

    symbol: str = DEFAULT_SYMBOL
    #: How to obtain the strike. Left injectable because it is the ONE field
    #: this venue cannot supply, and a test must not need the wrapper.
    strike_of: Callable[[M.PmRound], float | None] | None = None
    _cache: dict[str, Round] = None            # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._cache is None:
            self._cache = {}

    def _wrap(self, pm: M.PmRound) -> Round:
        r = self._cache.get(pm.slug)
        if r is None:
            strike = self.strike_of(pm) if self.strike_of else None
            r = to_round(pm, symbol=self.symbol, strike=strike)
            self._cache[pm.slug] = r
        return r

    def current_and_next(self, now: float | None = None) -> list[Round]:
        cur, nxt = M.current_and_next(now)
        return [self._wrap(r) for r in (cur, nxt) if r is not None]

    def for_slug(self, slug: str) -> Round | None:
        try:
            ts = float(slug.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            return None
        r = self._cache.get(slug)
        if r is not None:
            return r
        pm = M.fetch_round(ts)
        return None if pm is None else self._wrap(pm)

    def prune(self, keep: Iterable[str] | None = None) -> None:
        """Drop cached rounds we will never trade again.

        Unbounded growth here is one small entry per 5 minutes, so this is
        hygiene rather than a leak fix -- but a long-running process should not
        accumulate a market dict per round forever.
        """
        keep = set(keep or ())
        if not keep:
            self._cache.clear()
            return
        for k in [k for k in self._cache if k not in keep]:
            del self._cache[k]


__all__ = ["DEFAULT_SYMBOL", "PmRoundSource", "to_round", "settle_winner"]
