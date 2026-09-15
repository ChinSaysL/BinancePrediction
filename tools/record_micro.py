#!/usr/bin/env python3
"""Record the two Binance feeds that have NO historical archive.

Why this exists (2026-09-15): every precursor idea we can backtest today is
capped by data that stopped being published or never was:

  * top-of-book / L2 depth at sub-second resolution -- spot never archived it,
    futures `bookTicker` stops 2023-05-18, and the free `bookDepth` archive is
    percentage bands (+/-0.2% = 20bps) at a 30s cadence, which cannot resolve
    the +/-5bps "air pocket" question;
  * the liquidation tape -- `allForceOrders` is gone (404), `forceOrders` is
    private (401), and `!forceOrder@arr` is throttled to at most one order per
    symbol per second.

So the only way to ever settle "do forced sweeps / depth voids predict the 3-10s
drift that our ~1s execution can still catch" is to start recording now and
re-ask when we have more days than the five the venue DB holds.

Reconnaissance-scale, not a market data platform:
  * subscribes to futures `depth20@100ms` per symbol + the `!forceOrder@arr`
    stream (the throttled one is still worth having: it is the only public
    liquidation signal left);
  * writes RAW json plus the local receive timestamp -- a parser written later
    can extract anything, and no schema decision made today can lose data;
  * hourly gzip rotation, one file per symbol per hour, so a crash costs at
    most the current hour and files stay small enough to grep;
  * reconnects with exponential backoff; flushes every --flush-s (default 5s);
  * prints a heartbeat so a silent stall is visible in the log.

Read-only. It never touches the trading path, the venue, or the bot's state.

Usage:
    python3 tools/record_micro.py                    # BTC/ETH/BNB, 100ms depth
    python3 tools/record_micro.py --symbols btcusdt --depth-levels 20
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import signal
import sys
import time
from typing import IO

import aiohttp

FSTREAM = "wss://fstream.binance.com/stream?streams="
ROOT = "data/micro"


class Rotator:
    """Append-only, hourly-rotated gzip writer.

    Concatenated gzip members are legal and every reader handles them, so
    rotation is just 'close and open the next name' -- no merging step.
    """

    def __init__(self, kind: str, flush_s: float = 5.0) -> None:
        self.kind = kind
        self.flush_s = flush_s
        self._fh: IO[str] | None = None
        self._key: str | None = None
        self._last_flush = 0.0
        self.rows = 0
        self.bytes_raw = 0

    def _path(self, stamp: str, sym: str | None) -> str:
        d = os.path.join(ROOT, self.kind)
        os.makedirs(d, exist_ok=True)
        name = f"{sym}-{stamp}.jsonl.gz" if sym else f"{self.kind}-{stamp}.jsonl.gz"
        return os.path.join(d, name)

    def write(self, sym: str | None, payload: str, now: float) -> None:
        stamp = time.strftime("%Y%m%d-%H", time.gmtime(now))
        key = f"{sym}|{stamp}"
        if key != self._key:
            self.close()
            self._fh = gzip.open(self._path(stamp, sym), "at", encoding="utf-8")
            self._key = key
            self._last_flush = now
        line = f"{int(now * 1000)},{payload}\n"
        assert self._fh is not None
        self._fh.write(line)
        self.rows += 1
        self.bytes_raw += len(line)
        if now - self._last_flush >= self.flush_s:
            self._fh.flush()
            self._last_flush = now

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            self._key = None


def streams(symbols: list[str], levels: int) -> str:
    parts = [f"{s}@depth{levels}@100ms" for s in symbols]
    parts.append("!forceOrder@arr")          # throttled, but the only public one
    return "/".join(parts)


async def run(args: argparse.Namespace, stop: asyncio.Event) -> int:
    url = FSTREAM + streams(args.symbols, args.depth_levels)
    kinds = {f"{s}@depth{args.depth_levels}@100ms": "depth" for s in args.symbols}
    kinds["!forceOrder@arr"] = "liq"
    rot = {k: Rotator(k, args.flush_s) for k in ("depth", "liq")}

    backoff = 1.0
    total = 0
    t_start = time.monotonic()
    t_last = t_start
    while not stop.is_set():
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.ws_connect(url, heartbeat=args.heartbeat_s) as ws:
                    print(f"connected: {len(kinds)} streams, "
                          f"{len(args.symbols)} symbols, "
                          f"depth{args.depth_levels}@100ms", flush=True)
                    backoff = 1.0
                    async for msg in ws:
                        if stop.is_set():
                            break
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        now = time.time()
                        mono = time.monotonic()
                        raw = msg.data
                        try:
                            name = json.loads(raw).get("stream", "")
                        except (ValueError, AttributeError):
                            continue
                        sym = name.split("@")[0] if "@" in name else None
                        kind = kinds.get(name)
                        if kind is None:                 # unknown stream: keep it
                            kind = "depth" if name.endswith("@100ms") else "liq"
                        rot[kind].write(sym, raw, now)
                        total += 1
                        if mono - t_last >= args.status_s:
                            el = mono - t_start
                            print(f"status: {total} msgs, "
                                  f"{total / el:.1f} msg/s, "
                                  f"{rot['depth'].bytes_raw / 1e6:.1f} MB raw "
                                  f"depth, {rot['liq'].rows} liquidations, "
                                  f"est {rot['depth'].bytes_raw / el * 3600 * 24 / 1e9:.2f} GB/day raw",
                                  flush=True)
                            t_last = mono
        except asyncio.CancelledError:
            raise
        except Exception as exc:                     # noqa: BLE001 - log & retry
            print(f"stream error ({type(exc).__name__}: {exc}); "
                  f"reconnecting in {backoff:.0f}s", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)
    for r in rot.values():
        r.close()
    print(f"stopped after {total} messages", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="btcusdt,ethusdt,bnbusdt")
    ap.add_argument("--depth-levels", type=int, default=20,
                    help="futures partial depth levels (5/10/20)")
    ap.add_argument("--flush-s", type=float, default=5.0)
    ap.add_argument("--status-s", type=float, default=60.0)
    ap.add_argument("--heartbeat-s", type=float, default=20.0)
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="exit after N seconds (0 = run until stopped)")
    args = ap.parse_args()
    args.symbols = [s.strip().lower() for s in args.symbols.split(",") if s.strip()]

    async def _main() -> int:
        stop = asyncio.Event()

        def _sig(*_: object) -> None:
            stop.set()

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(s, _sig)
            except NotImplementedError:              # pragma: no cover
                signal.signal(s, _sig)
        task = asyncio.create_task(run(args, stop))
        if args.seconds > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task),
                                       timeout=args.seconds)
            except asyncio.TimeoutError:
                stop.set()
                await task
        else:
            await task
        return 0

    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:                        # pragma: no cover
        return 0


if __name__ == "__main__":
    sys.exit(main())
