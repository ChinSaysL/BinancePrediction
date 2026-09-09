"""Measure real complete-set arbitrage frequency and size.

Books are fetched CONCURRENTLY (sequential fetches manufacture phantom arbs),
and every candidate is confirmed against live venue quotes with share-parity
sizing before being counted.
"""
from __future__ import annotations
import asyncio, statistics as st, time
import aiohttp
from btcpredict.config import settings
from btcpredict.execution.live import LiveBroker, from_wei

async def main(minutes: float = 10.0) -> None:
    async with aiohttp.ClientSession() as s:
        b = LiveBroker(s, settings.binance_api_key, settings.binance_api_secret)
        await b.discover_wallet()
        end = time.time() + minutes * 60
        polls = cand = real = 0
        edges, sums = [], []
        while time.time() < end:
            r = await b._request("GET", "/sapi/v1/w3w/wallet/prediction/market/list", {})
            t = next((x for x in (r or {}).get("marketTopics", [])
                      if x.get("slug", "").startswith("btc-updown-5m")), None)
            if not t:
                await asyncio.sleep(2); continue
            m = t["markets"][0]
            up = next(o for o in m["outcomes"] if o["name"] == "Up")
            dn = next(o for o in m["outcomes"] if o["name"] == "Down")
            bu, bd = await asyncio.gather(
                b.best_prices(up["tokenId"], m["marketId"]),
                b.best_prices(dn["tokenId"], m["marketId"]))
            polls += 1
            if not (bu and bd):
                await asyncio.sleep(1.5); continue
            asum = bu[1] + bd[1]
            sums.append(asum)
            if asum < 1.0:
                cand += 1
                qu, qd = await asyncio.gather(b.get_quote(up["tokenId"], 1.0),
                                              b.get_quote(dn["tokenId"], 1.0))
                if qu and qd:
                    nu = (from_wei(qu["amountOut"]) - from_wei(qu["feeAmount"]))
                    nd = (from_wei(qd["amountOut"]) - from_wei(qd["feeAmount"]))
                    if nu > 0 and nd > 0:
                        cost = 1.0 / nu + 1.0 / nd
                        if cost < 1.0:
                            real += 1
                            edges.append(1.0 - cost)
                            print(f"  REAL arb: book_sum={asum:.3f} cost={cost:.4f} "
                                  f"edge={(1-cost)*100:.2f}%")
            await asyncio.sleep(1.5)

        mins = minutes
        print(f"\npolls={polls} over {mins:.0f} min | candidates={cand} | confirmed={real}")
        sums.sort()
        print(f"book ask-sum: p5={sums[len(sums)//20]:.3f} median={sums[len(sums)//2]:.3f}")
        if real:
            print(f"real arb rate : {real/mins*60:.1f}/hour  ({real/polls:.2%} of polls)")
            print(f"edge per set  : mean {st.mean(edges)*100:.3f}%  "
                  f"max {max(edges)*100:.3f}%")
        else:
            print("no confirmed arbitrage in this window")

if __name__ == "__main__":
    asyncio.run(main())
