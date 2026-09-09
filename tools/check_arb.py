"""Are the apparent complete-set arbitrages real, or a recording artefact?

2.8% of recorded quotes showed up_ask + down_ask < 1.00, which would be
risk-free profit. That is suspicious: the recorder fetches the two books in
*separate sequential calls*, so any price move in between manufactures a phantom
arb that never existed at one instant.

This settles it by asking the venue itself. When an apparent arb appears, it
immediately requests a real quote for BOTH sides. `get-quote` is non-executing,
so nothing is risked -- but it returns the price the venue would actually fill
at, including size and fees. If the two quoted prices still sum to < 1.00, the
arbitrage is real money.
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

from btcpredict.config import settings
from btcpredict.execution.live import LiveBroker, from_wei

STAKE = 1.0


async def main(minutes: float = 6.0) -> None:
    async with aiohttp.ClientSession() as s:
        b = LiveBroker(s, settings.binance_api_key, settings.binance_api_secret)
        if not await b.discover_wallet():
            print("no wallet"); return

        deadline = time.time() + minutes * 60
        checked = confirmed = phantom = 0
        print(f"watching for apparent arbitrage for {minutes:.0f} min...\n")
        while time.time() < deadline:
            r = await b._request("GET", "/sapi/v1/w3w/wallet/prediction/market/list", {})
            topic = next((t for t in (r or {}).get("marketTopics", [])
                          if t.get("slug", "").startswith("btc-updown-5m")), None)
            if not topic:
                await asyncio.sleep(2); continue
            m = topic["markets"][0]
            up = next(o for o in m["outcomes"] if o["name"] == "Up")
            dn = next(o for o in m["outcomes"] if o["name"] == "Down")

            # Fetch both books as close together as possible, then re-fetch the
            # first to bound how much drift the sequential calls introduce.
            bu1 = await b.best_prices(up["tokenId"], m["marketId"])
            bd = await b.best_prices(dn["tokenId"], m["marketId"])
            bu2 = await b.best_prices(up["tokenId"], m["marketId"])
            if not (bu1 and bd and bu2):
                await asyncio.sleep(2); continue
            drift = abs(bu2[1] - bu1[1])
            ask_sum = bu1[1] + bd[1]

            if ask_sum < 1.0:
                checked += 1
                print(f"  APPARENT ARB: up_ask={bu1[1]:.2f} down_ask={bd[1]:.2f} "
                      f"sum={ask_sum:.3f}  (up-book drift over the two calls: {drift:.3f})")
                qu = await b.get_quote(up["tokenId"], STAKE)
                qd = await b.get_quote(dn["tokenId"], STAKE)
                if not (qu and qd):
                    print("     could not quote both sides\n"); continue
                pu, pd = float(qu["averagePrice"]), float(qd["averagePrice"])
                su, sd = from_wei(qu["amountOut"]), from_wei(qd["amountOut"])
                fu, fd = from_wei(qu["feeAmount"]), from_wei(qd["feeAmount"])
                # A complete set pays $1 only if we hold EQUAL shares of both
                # sides. Sizing $1 per side buys wildly unequal share counts when
                # the prices differ, which understates the arbitrage.
                net_u = (su - fu) / STAKE      # net shares per $1 spent
                net_d = (sd - fd) / STAKE
                # cost to acquire one share of each, net of fees
                cost = 1.0 / net_u + 1.0 / net_d
                payout = 1.0
                print(f"     venue quotes: up={pu:.3f} down={pd:.3f}  sum={pu+pd:.3f}")
                print(f"     net shares per $1: up {net_u:.4f}, down {net_d:.4f}")
                print(f"     cost of 1 complete set ${cost:.4f} -> pays $1.0000 "
                      f"= {payout-cost:+.4f} ({(payout-cost)/cost:+.2%})")
                if payout > cost:
                    confirmed += 1
                    print("     *** REAL ARBITRAGE ***\n")
                else:
                    phantom += 1
                    print("     phantom: venue does not honour the book sum\n")
            await asyncio.sleep(2)

        print(f"\napparent arbs seen: {checked} | confirmed: {confirmed} | phantom: {phantom}")
        if checked and not confirmed:
            print("=> the recorded 'arbitrage' is an artefact of sequential book fetches")


if __name__ == "__main__":
    asyncio.run(main())
