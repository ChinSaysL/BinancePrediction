"""Validate the digital model against realised 5-minute outcomes.

This tests the *model* half of the strategy only. It cannot test the trade rule,
because historical market quotes are not retrievable -- those come from Phase 0
recording. What it does establish is whether our probabilities are trustworthy
enough to price against, using Binance klines as the oracle substitute
(justified by the +-$0.005 basis measured in FINDINGS.md).

Usage:  python -m tools.backtest_calibration --days 10
"""
from __future__ import annotations

import argparse
import asyncio
import math
import statistics as st
from collections import defaultdict

import aiohttp

from btcpredict.calibration.report import reliability_table, score
from btcpredict.feeds.binance_spot import fetch_klines
from btcpredict.pricing.digital import fair_value

ROUND_S = 300


async def load_1m(session: aiohttp.ClientSession, days: float) -> list[list]:
    want = int(days * 24 * 60)
    out: list[list] = []
    end = None
    while len(out) < want:
        batch = await fetch_klines(session, "1m", 1000, end_ms=end)
        if not batch:
            break
        out = batch + out
        end = int(batch[0][0]) - 1
        if len(batch) < 1000:
            break
    return out[-want:]


def build_rounds(bars: list[list]) -> list[list[list]]:
    groups: dict[int, list[list]] = defaultdict(list)
    for k in bars:
        groups[(int(k[0]) // 1000 // ROUND_S) * ROUND_S].append(k)
    return [groups[k] for k in sorted(groups) if len(groups[k]) == 5]


def rolling_sigma(rounds: list[list[list]], idx: int, window: int = 48) -> float:
    """sigma_1m from the `window` rounds *before* idx -- strictly out of sample."""
    lo = max(0, idx - window)
    rets = []
    for r in rounds[lo:idx]:
        for bar in r:
            o, c = float(bar[1]), float(bar[4])
            if o > 0 and c > 0:
                rets.append(math.log(c / o))
    if len(rets) < 20:
        return 0.0004
    return max(0.00015, st.pstdev(rets))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=10.0)
    args = ap.parse_args()

    async with aiohttp.ClientSession() as s:
        bars = await load_1m(s, args.days)
    rounds = build_rounds(bars)
    print(f"loaded {len(bars)} 1m bars -> {len(rounds)} complete 5m rounds "
          f"({len(rounds) * 5 / 60 / 24:.1f} days)\n")

    base = sum(1 for r in rounds if float(r[4][4]) > float(r[0][1])) / len(rounds)
    print(f"base rate (Up): {base:.4f}\n")

    for mark in (1, 2, 3, 4):
        tau_s = (5 - mark) * 60.0
        preds, outs = [], []
        for i, r in enumerate(rounds):
            if i < 48:
                continue
            K = float(r[0][1])
            S = float(r[mark - 1][4])
            C = float(r[4][4])
            sigma = rolling_sigma(rounds, i)
            preds.append(fair_value(S, K, tau_s, sigma))
            outs.append(1 if C > K else 0)
        sc = score(preds, outs)
        print(f"--- decision at t={mark}min ({int(tau_s)}s left) ---")
        print(f"    {sc}")
        print(f"    reliability={sc.reliability:.4f} (lower better)  "
              f"resolution={sc.resolution:.4f} (higher better)")
        print(reliability_table(preds, outs, min_n=20))
        print()


if __name__ == "__main__":
    asyncio.run(main())
