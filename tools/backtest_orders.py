"""Model vs market, scored on real historical fill prices.

The venue exposes no historical quote feed, but `order/history` returns every
order this account ever filled: slug, outcome, timestamp and the price actually
paid. Each is a real executable market price at a known instant with a known
result -- exactly the pairing the go/no-go test needs.

Everything else is reconstructed from Binance klines, which the FINDINGS basis
check showed track the settlement oracle to +-$0.005:
    strike  = 5m kline open at the round start encoded in the slug
    settle  = that kline's close  (close > open -> Up)
    S at t  = 1s kline at the order timestamp

IMPORTANT -- this is a biased sample. These are moments a trader chose to act,
not a random sample of quotes, so it cannot show the market is mispriced in
general. It shows whether the model would have disagreed usefully at the moments
trading actually happened.

Work here is network-bound (~180 kline requests), so fetches run concurrently.

Usage:  python -m tools.backtest_orders
"""
from __future__ import annotations

import asyncio
import json
import math
import statistics as st
from pathlib import Path

import aiohttp

from btcpredict.calibration.report import PnLSummary, reliability_table, score
from btcpredict.config import settings
from btcpredict.edge.signal import FeeModel, evaluate, fee_rate_for_price
from btcpredict.feeds.binance_spot import fetch_klines
from btcpredict.pricing.digital import fair_value

ROUND_S = 300
CONCURRENCY = 10
ORDERS = Path("data/order_history.json")


async def _retry(fn, *a, tries: int = 3, **kw):
    for i in range(tries):
        try:
            return await fn(*a, **kw)
        except Exception:
            if i == tries - 1:
                return None
            await asyncio.sleep(0.5 * (i + 1))
    return None


async def price_at(session, sem, ts_ms: int) -> tuple[int, float | None]:
    async with sem:
        ks = await _retry(fetch_klines, session, "1s", 5, end_ms=ts_ms + 2000)
    if not ks:
        return ts_ms, None
    best = min(ks, key=lambda k: abs(int(k[0]) - ts_ms))
    return ts_ms, float(best[4])


async def round_bar(session, sem, start_s: int) -> tuple[int, tuple[float, float] | None]:
    async with sem:
        ks = await _retry(fetch_klines, session, "5m", 2,
                          end_ms=start_s * 1000 + ROUND_S * 1000 - 1)
    for k in (ks or []):
        if int(k[0]) // 1000 == start_s:
            return start_s, (float(k[1]), float(k[4]))
    return start_s, None


async def local_sigma(session, sem, start_s: int) -> tuple[int, float]:
    """sigma_1m from the 240 minutes immediately BEFORE the round.

    A single global sigma is wrong across a multi-week sample: volatility on one
    day is not volatility on another, and feeding a stale sigma into
    ln(S/K)/(sigma*sqrt(tau)) makes the model look badly calibrated when the
    real fault is the input. This is also strictly out of sample -- it uses only
    bars that closed before the round opened.
    """
    async with sem:
        ks = await _retry(fetch_klines, session, "1m", 240,
                          end_ms=start_s * 1000 - 1)
    rets = [math.log(float(k[4]) / float(k[1]))
            for k in (ks or []) if float(k[1]) > 0 and float(k[4]) > 0]
    if len(rets) < 30:
        return start_s, 4.0e-4
    return start_s, max(1.5e-4, st.pstdev(rets))


async def main() -> None:
    orders = json.loads(ORDERS.read_text())
    print(f"{len(orders)} filled BTC 5m orders")

    async with aiohttp.ClientSession() as s:
        sem = asyncio.Semaphore(CONCURRENCY)
        starts, stamps = set(), set()
        for o in orders:
            try:
                starts.add(int(o["slug"].rsplit("-", 1)[1]))
                stamps.add(int(o["createTime"]))
            except (KeyError, ValueError, IndexError):
                continue
        print(f"fetching {len(starts)} round bars + {len(stamps)} tick prices "
              f"+ {len(starts)} local sigmas ({CONCURRENCY} concurrent)...")

        bars = dict(await asyncio.gather(*(round_bar(s, sem, x) for x in starts)))
        ticks = dict(await asyncio.gather(*(price_at(s, sem, t) for t in stamps)))
        sigmas = dict(await asyncio.gather(*(local_sigma(s, sem, x) for x in starts)))
    sv = sorted(sigmas.values())
    print(f"per-round sigma_1m: min {sv[0]:.6f}  median {sv[len(sv)//2]:.6f}  "
          f"max {sv[-1]:.6f}   (a single global sigma would misprice most of these)")

    rows = []
    for o in orders:
        try:
            start_s = int(o["slug"].rsplit("-", 1)[1])
        except (KeyError, ValueError, IndexError):
            continue
        bar, S = bars.get(start_s), ticks.get(int(o["createTime"]))
        if not bar or not S:
            continue
        K, C = bar
        tau = (start_s + ROUND_S) - int(o["createTime"]) / 1000
        if not (0 < tau <= ROUND_S):
            continue
        winner = "Up" if C > K else "Down"
        side = o["outcome"]
        fair_up = fair_value(S, K, tau, sigmas.get(start_s, 4.0e-4))
        rows.append({
            "slug": o["slug"], "side": side, "tau": tau, "q": float(o["price"]),
            "p": fair_up if side == "Up" else 1.0 - fair_up, "fair_up": fair_up,
            "won": 1 if side == winner else 0,
            "up_won": 1 if winner == "Up" else 0,
        })

    if len(rows) < 20:
        print(f"only {len(rows)} usable rows"); return

    print(f"\nreconstructed {len(rows)} orders over {len({r['slug'] for r in rows})} rounds\n")
    print("=" * 74)
    print("MODEL vs MARKET  (probability the side actually bought would win)")
    print("=" * 74)
    model = [r["p"] for r in rows]
    market = [r["q"] for r in rows]
    outs = [r["won"] for r in rows]
    ms, qs = score(model, outs), score(market, outs)
    print(f"  model : {ms}")
    print(f"  market: {qs}")
    gap = qs.brier - ms.brier
    print(f"\n  Brier gap: {gap:+.4f}  =>  "
          f"{'MODEL BEATS MARKET' if gap > 0 else 'MARKET IS AT LEAST AS GOOD'}")
    print(f"  reliability: model {ms.reliability:.4f} vs market {qs.reliability:.4f}"
          "  (lower = better)")
    print(f"\n  win rate of the trades actually taken: {sum(outs)/len(outs):.3f}")
    print("\nMODEL calibration on these fills:")
    print(reliability_table(model, outs, min_n=8))

    print("\n" + "=" * 74)
    print("WOULD THE CURRENT GATE HAVE TRADED THESE?")
    print("=" * 74)
    for hs in (0.0, 0.005, 0.01):
        pnl = PnLSummary()
        skipped = 0
        for r in rows:
            q_up = r["q"] if r["side"] == "Up" else 1.0 - r["q"]
            # fee=None -> the measured per-price fee curve (180 * min(1,(1-px)/px)).
            sig = evaluate(r["fair_up"], q_up, 1.0 - q_up, fee=None, half_spread=hs,
                           safety_margin=settings.safety_margin,
                           fee_model=FeeModel.NOTIONAL,
                           max_quote=settings.max_quote, min_edge=settings.min_edge)
            if not sig.act:
                skipped += 1
                continue
            fill = min(0.999, sig.quote + hs)
            won = (sig.side == "Up") == bool(r["up_won"])
            fee = fee_rate_for_price(fill)
            pnl.add((1.0 / fill if won else 0.0) - 1.0 - fee, 1.0, fee, won)
        print(f"  half_spread={hs:.3f}: {pnl}")
        print(f"      (took {len(rows)-skipped} of {len(rows)})")


if __name__ == "__main__":
    asyncio.run(main())
