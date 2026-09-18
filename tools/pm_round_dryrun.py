#!/usr/bin/env python3
"""What WOULD the bot do in the current round on Polymarket -- buying nothing.

Validates the chain that a venue flip actually exercises, against live data:

    discovery (gamma) -> strike (wrapper feed) -> book (CLOB) -> plan (no order)

Read-only: places no order, needs no key, writes nothing. Run it BEFORE
flipping `venue=polymarket`, and again if a deploy changes the wiring.

Self-contained on purpose -- anything meant to run on the box must not depend
on another tool being deployed alongside it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcpredict.config import settings                        # noqa: E402
from btcpredict.feeds.market_feed import MarketFeed           # noqa: E402
from btcpredict.venues.polymarket.book import PmBook          # noqa: E402
from btcpredict.venues.polymarket.broker import PmBroker      # noqa: E402
from btcpredict.venues.polymarket.feed import PmMarketFeed    # noqa: E402

PREFIXES = ["btc-updown-5m", "eth-updown-5m", "bnb-updown-5m"]


def line(k: str, v) -> None:
    print(f"  {k:<26} {v}")


async def main() -> int:
    import aiohttp

    cap = float(getattr(settings, "cross_max_fill_px", 0.65) or 0.65)
    stake = float(getattr(settings, "min_order_usdt", 1.0) or 1.0)
    print(f"POLYMARKET DRY RUN  (no orders)   cap {cap}  stake ${stake}")

    async with aiohttp.ClientSession() as session:
        # The wrapper feed supplies the STRIKE and nothing else. Polymarket
        # publishes no reference price for this family.
        wrapper = MarketFeed(session)
        feed = PmMarketFeed(session=session, strike_feed=wrapper)

        t0 = time.time()
        found = await feed.rounds_by_prefixes(PREFIXES)
        line("discovery ms", f"{(time.time() - t0) * 1000:.0f}")
        line("prefixes matched", sorted(found) or "*** NONE ***")
        if not found:
            print("\n  FAIL: no Polymarket round discovered.")
            return 1
        for p in PREFIXES:
            line(f"  {p}", found.get(p).slug if found.get(p) else "-")

        r = found.get("btc-updown-5m")
        if r is None:
            print("\n  FAIL: btc-updown-5m did not resolve.")
            return 1
        # The bug this catches: if .up/.down are None the arms evaluate against
        # nothing and trade zero rounds while looking healthy.
        line("round", r.slug)
        line("up token", (r.up.token_id[:16] + "...") if r.up else "*** NONE ***")
        line("down token", (r.down.token_id[:16] + "...") if r.down else "*** NONE ***")
        line("tau_s", f"{r.tau_s(int(time.time() * 1000)):.1f}")
        line("strike (from wrapper)", r.strike)
        line("feed_provider", r.feed_provider)
        if r.up is None or r.down is None:
            print("\n  FAIL: r.up/r.down missing -- the arms would see no market.")
            return 1
        if r.strike is None:
            print("\n  WARN: strike unknown; the cross arm's depth gate refuses.")

        # Book, read-only over REST (no websocket to babysit).
        book = PmBook()
        toks = [r.up.token_id, r.down.token_id]
        try:
            await book.poll_once(session, toks, timeout=10.0)
        except Exception as exc:                             # noqa: BLE001
            print(f"\n  WARN: book poll failed: {type(exc).__name__} {exc}")
        for name, tok in (("Up", r.up.token_id), ("Down", r.down.token_id)):
            t = book.best(tok, 60.0)
            if t is None:
                line(f"{name} touch", "*** NO BOOK ***")
                continue
            line(f"{name} touch", f"bid {t.bid:.2f} x {t.bid_size:.0f} | "
                                  f"ask {t.ask:.2f} x {t.ask_size:.0f}")
            sweep, fillable = t.sweep(stake)
            line(f"{name} sweep ${stake:.0f}", f"{sweep:.4f}  (${fillable:.2f} fillable)")

        # What the broker WOULD send. `plan` is pure -- no venue call.
        #
        # `remember()` takes a markets.PmRound (the VENUE's view: token ids,
        # tick, min order), NOT the market_feed.Round the arms consume. The two
        # classes share the name `Round` and nothing else, and passing the
        # wrong one fails later inside plan() with a cryptic AttributeError.
        br = PmBroker(book=book, client=object(), log=lambda m: None)
        from btcpredict.venues.polymarket import markets as _M
        # `r` is a market_feed.Round, whose clock field is `start_ms`; the
        # venue round keys off unix SECONDS. Same trap as `remember()`.
        pm_cur = _M.fetch_round(r.start_ms / 1000.0)
        if pm_cur is None:
            print("\n  FAIL: could not re-read the venue round for planning.")
            return 1
        br.remember(pm_cur)
        print("\n  plan() -- the order that WOULD go out (none is sent):")
        for side in ("Up", "Down"):
            p = br.plan(slug=r.slug, side=side, stake=stake, max_px=cap)
            if p is None:
                line(f"  {side}", "refused (no fresh book / one-sided book / "
                                  "below venue minimum)")
                continue
            line(f"  {side}", f"limit {p['limit']:.2f}  shares {p['shares']:.0f}  "
                              f"ask {p['ask']:.2f}  sweep {p['sweep_px']:.4f}  "
                              f"book_age {p['book_age_s']:.2f}s")

        # Settlement path: does gamma report a resolved winner for the PREVIOUS
        # round, and does by_slug actually find it? This is the call that books
        # real P&L, so it is worth exercising before it matters.
        prev = f"btc-updown-5m-{int(r.start_ms / 1000.0) - 300}"
        print(f"\n  settlement probe on the PREVIOUS round ({prev}):")
        pr = await feed.by_slug(prev)
        if pr is None:
            line("  by_slug", "None (market not listed yet -- normal early in a round)")
        else:
            line("  by_slug", pr.slug)
            line("  winner_name", pr.winner_name if pr.winner_name
                 else "(not resolved yet -- normal until ~58s after the bell)")

        print("\n  OK: discovery, strike, book and plan all produced usable values.")
        print("  Nothing was ordered.")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
