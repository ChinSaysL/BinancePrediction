"""Client for Binance's public prediction-market feed.

These endpoints back the web app and are not in the published API docs; they
were mapped by probing. They need no credentials. Shapes are defensive on
purpose -- treat every field as possibly absent or newly renamed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from btcpredict.config import EVENT_SLUG, MARKET_DETAIL_URL, MARKET_LIST_URL

log = logging.getLogger(__name__)

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) btcpredict/0.1",
}


@dataclass(slots=True)
class Outcome:
    name: str
    price: float
    chance: float
    token_id: str
    market_outcome_id: int
    winner: bool | None


@dataclass(slots=True)
class Round:
    """One 5-minute market round, flattened from the nested API payload."""

    slug: str
    market_topic_id: int
    market_id: int | None
    condition_id: str | None
    symbol: str
    start_ms: int
    end_ms: int
    strike: float | None          # variantData.startPrice
    end_price: float | None       # variantData.endPrice, populated after settle
    price_feed_id: str | None
    feed_provider: str | None
    fee_rate_bps: int
    liquidity: float
    trade_volume: float
    status: str
    trading_status: str | None
    live_status: str | None
    outcomes: list[Outcome]

    def outcome(self, name: str) -> Outcome | None:
        return next((o for o in self.outcomes if o.name.lower() == name.lower()), None)

    @property
    def up(self) -> Outcome | None:
        return self.outcome("Up")

    @property
    def down(self) -> Outcome | None:
        return self.outcome("Down")

    @property
    def winner_name(self) -> str | None:
        for o in self.outcomes:
            if o.winner is True:
                return o.name
        return None

    def tau_s(self, now_ms: int) -> float:
        return (self.end_ms - now_ms) / 1000.0


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_round(topic: dict[str, Any]) -> Round | None:
    """Flatten one marketTopic. Returns None if it is not a usable binary market."""
    markets = topic.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    variant = topic.get("variantData") or {}

    outcomes = []
    for o in m.get("outcomes") or []:
        price = _f(o.get("price"))
        chance = _f(o.get("chance"))
        if price is None:
            continue
        outcomes.append(
            Outcome(
                name=str(o.get("name", "")),
                price=price,
                chance=chance if chance is not None else price,
                token_id=str(o.get("tokenId", "")),
                market_outcome_id=int(o.get("marketOutcomeId") or 0),
                winner=o.get("winner"),
            )
        )
    if len(outcomes) < 2:
        return None

    try:
        start_ms = int(topic["startDate"])
        end_ms = int(topic["endDate"])
    except (KeyError, TypeError, ValueError):
        return None

    return Round(
        slug=str(topic.get("slug", "")),
        market_topic_id=int(topic.get("marketTopicId") or 0),
        market_id=int(m["marketId"]) if m.get("marketId") is not None else None,
        condition_id=m.get("conditionId"),
        symbol=str(topic.get("symbol") or ""),
        start_ms=start_ms,
        end_ms=end_ms,
        strike=_f(variant.get("startPrice")),
        end_price=_f(variant.get("endPrice")),
        price_feed_id=variant.get("priceFeedId"),
        feed_provider=variant.get("priceFeedProvider"),
        fee_rate_bps=int(topic.get("feeRateBps") or 0),
        liquidity=_f(m.get("liquidity")) or 0.0,
        trade_volume=_f(m.get("tradeVolume")) or 0.0,
        status=str(m.get("status") or topic.get("status") or ""),
        trading_status=m.get("tradingStatus"),
        live_status=topic.get("liveStatus"),
        outcomes=outcomes,
    )


class MarketFeed:
    def __init__(self, session: aiohttp.ClientSession, event_slug: str = EVENT_SLUG):
        self.session = session
        self.event_slug = event_slug

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any] | None:
        try:
            async with self.session.post(
                url, json=body, headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    log.warning("feed %s HTTP %s", url, resp.status)
                    return None
                payload = await resp.json(content_type=None)
        except Exception as e:  # network flake -> caller retries next tick
            log.warning("feed %s failed: %s: %s", url, type(e).__name__, e)
            return None
        if not payload.get("success", True) and payload.get("code") not in (None, "000000"):
            log.warning("feed %s error: %s", url, payload.get("message"))
            return None
        return payload.get("data")

    async def live_round(self) -> Round | None:
        """The currently-open round of our event, or None."""
        data = await self._post(MARKET_LIST_URL, {})
        if not data:
            return None
        rounds = [
            r for t in data.get("marketTopics", [])
            if (r := parse_round(t)) and r.slug.startswith(self.event_slug.replace("-up-or-down-", "-updown-"))
        ]
        if not rounds:
            # Fall back to matching on eventSlug, in case the slug convention shifts.
            rounds = [
                r for t in data.get("marketTopics", [])
                if t.get("eventSlug") == self.event_slug and (r := parse_round(t))
            ]
        if not rounds:
            return None
        live = [r for r in rounds if r.live_status == "LIVE"]
        return max(live or rounds, key=lambda r: r.start_ms)

    async def rounds_by_prefixes(self, prefixes: list[str]) -> dict[str, Round]:
        """Live round for each slug prefix, from one paginated sweep.

        The authenticated list omits low-volume markets, so the public feed is
        used here; several pages are needed because up/down markets sort below
        the large sports and politics books.
        """
        found: dict[str, Round] = {}
        for offset in range(0, 600, 50):
            data = await self._post(MARKET_LIST_URL, {"limit": 50, "offset": offset})
            if not data:
                break
            for t in data.get("marketTopics", []):
                slug = t.get("slug", "")
                for p in prefixes:
                    if slug.startswith(p) and p not in found:
                        r = parse_round(t)
                        if r:
                            found[p] = r
            if not data.get("hasMore") or len(found) == len(prefixes):
                break
        return found

    async def by_topic_id(self, market_topic_id: int) -> Round | None:
        """Re-fetch one round by id -- used to collect settlement after it closes."""
        data = await self._post(MARKET_DETAIL_URL, {"marketTopicId": market_topic_id})
        if not data:
            return None
        return parse_round(data)
