"""One family the venue cannot resolve must not stop the others being cached.

MEASURED, 2026-09-17. `_refresh_rounds` swept all eight non-primary families in
ONE `rounds_by_prefixes` call, and that method pages until every requested
prefix has been found. So one family whose round is not in the listing (or is
buried past the page cap) consumed the whole page budget and the rest never
landed in `_round_cache`.

A family with no cache entry is never passed to `_record`, so it gets no quote,
no `_mkt` entry, no websocket book and no decision. The measured state:

    family   rounds rows   quotes in the last hour
    btc          32            6,256
    eth          32            6,256
    bnb          32            6,279
    xrp          15                0
    sol          15                0
    doge         15                0

and `pm book ws connected (12 assets)` = 6 markets = btc/eth/bnb only. Three
families were inert, and every symptom was an ABSENCE, so nothing said so.
"""
from __future__ import annotations

import asyncio

from btcpredict import main as M
from btcpredict.config import MARKETS

PREFIXES = [p for p, _, _ in MARKETS]


class FakeMarket:
    """Resolves every prefix except the ones named in `missing`."""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls: list[list[str]] = []

    async def rounds_by_prefixes(self, prefixes):
        self.calls.append(list(prefixes))
        return {p: (None if p in self.missing else "round:" + p)
                for p in prefixes}


def _bot(missing=()):
    bot = M.Bot.__new__(M.Bot)          # skip network setup
    bot._round_cache = {}
    bot.market = FakeMarket(missing)
    return bot


def test_one_unfindable_family_does_not_block_the_rest(monkeypatch):
    monkeypatch.setattr(M, "_stamp_symbol", lambda r, p: r)
    dead = "xrp-updown-5m"
    bot = _bot({dead})
    asyncio.run(bot._refresh_rounds(PREFIXES))
    cached = set(bot._round_cache)
    assert dead not in cached
    assert cached == set(PREFIXES) - {dead}, (
        "the other families must still be cached when one is unresolvable")


def test_every_sweep_is_one_prefix_per_call():
    """The property that makes the families independent. A single call for all
    of them is what coupled their outcomes in the first place."""
    monkeypatch_free = _bot()
    asyncio.run(monkeypatch_free._refresh_rounds(PREFIXES))
    assert all(len(c) == 1 for c in monkeypatch_free.market.calls), \
        monkeypatch_free.market.calls


def test_a_raising_family_does_not_stop_the_rest():
    """An exception must be contained per prefix, not abort the sweep. The old
    code caught once around the whole call and returned."""
    class Flaky(FakeMarket):
        async def rounds_by_prefixes(self, prefixes):
            self.calls.append(list(prefixes))
            if prefixes[0] == "bnb-updown-5m":
                raise RuntimeError("venue said no")
            return {p: "round:" + p for p in prefixes}

    bot = M.Bot.__new__(M.Bot)
    bot._round_cache = {}
    bot.market = Flaky()
    asyncio.run(bot._refresh_rounds(PREFIXES))
    assert "bnb-updown-5m" not in bot._round_cache
    assert len(bot._round_cache) == len(PREFIXES) - 1
