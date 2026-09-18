"""Probe the live liquidation feed: is there enough of it to trigger on?

The proposal: `wss://fstream.binance.com/ws/!forceOrder@arr` prints forced
liquidation orders in real time, and multi-basis-point 5-minute breakouts are
"frequently triggered by forced liquidation cascades", so fire when a cluster
prints.

Two things decide whether that is usable, and neither can be read off the docs:
  1. THE RATE. A trigger needs events to exist. Measured once before: 0 events in
     75 s across ALL symbols.
  2. THE SIZE AND THE FOLLOW-THROUGH. A cascade that is already over is not a
     signal; the ones that matter are the ones big enough to move spot.

This script only counts and prints. It is read-only and safe to run any time.

Usage:  python3 tools/liquidations_probe.py 180
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter

import websockets

URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
SYMS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"}


async def main(seconds: float) -> None:
    t0 = time.time()
    n = 0
    per_sym: Counter = Counter()
    rows = []
    async with websockets.connect(URL, ping_interval=20) as ws:
        while time.time() - t0 < seconds:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=seconds - (time.time() - t0))
            except asyncio.TimeoutError:
                break
            try:
                d = json.loads(msg)
            except Exception:
                continue
            o = d.get("o") or {}
            sym = o.get("s")
            if sym not in SYMS:
                continue
            try:
                qty = float(o.get("q", 0))
                px = float(o.get("ap", 0) or o.get("p", 0))
            except Exception:
                continue
            notional = qty * px
            n += 1
            per_sym[sym] += 1
            rows.append((time.strftime("%H:%M:%S", time.gmtime(o.get("T", 0) / 1000)),
                         sym, o.get("S"), qty, px, notional))
            print(f"  {rows[-1][0]} {sym:8s} {o.get('S'):4s} qty {qty:>12.3f} "
                  f"@ {px:>10.2f} = ${notional:>12,.0f}")
    el = time.time() - t0
    print(f"\n=== {el:.0f}s on !forceOrder@arr, symbols {sorted(SYMS)} ===")
    print(f"  events: {n}  ({n / el * 60:.2f} per minute, all 6 symbols)")
    if rows:
        not_ = [r[5] for r in rows]
        not_.sort()
        print(f"  notional: median ${not_[len(not_) // 2]:,.0f}  "
              f"p90 ${not_[int(len(not_) * 0.9)]:,.0f}  max ${not_[-1]:,.0f}")
        print(f"  >= $100k: {sum(1 for x in not_ if x >= 100_000)}  "
              f">= $500k: {sum(1 for x in not_ if x >= 500_000)}  "
              f">= $1M: {sum(1 for x in not_ if x >= 1_000_000)}")
    print("  per symbol: " + ", ".join(f"{k} {v}" for k, v in per_sym.most_common()))
    print("\n  REMEMBER: the arm needs a trigger every 5-minute round to be worth "
          "carrying. Compare the per-minute rate above with the round cadence.")


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    asyncio.run(main(secs))
