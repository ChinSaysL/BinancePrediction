"""Does the venue PUSH frames for the round we are pricing? Count, per token.

WHY. The live log shows a book that cannot come from `_apply_delta` at all:

    shadow book eth-updown-5m Down: venue 0.6500/0.6600
                                    ours 0.5000/0.7100
                                    spread 0.2100 vs venue 0.0100

`_apply_delta` writes `best_bid` AND `best_ask` from ONE message, so a 21-cent
spread in a 1-cent market can only mean nothing has been applied since the
`book` SNAPSHOT -- and both fires were on round `...636500`, which opened
09:15:00, four minutes earlier. The BTC fire at the same moment was the same
shape: ours 0.42/0.43 against a venue 0.67/0.70, a coherent book that is 0.27
stale, with `age 114ms` claiming freshness.

So the question this answers, and it is binary: is the venue sending frames for
these two asset ids, and are we simply not applying them -- or is it not sending
them at all? `Touch.recv_ms` cannot tell the difference; a frame count can.

Reads the round's tokens from the DB (the bot's own book has no external
reader), subscribes exactly as `PmBook._run_once` does, and reports per asset.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = Path(__file__).resolve().parents[1] / "data" / "btcpredict.db"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BOOK_URL = "https://clob.polymarket.com/book?token_id=%s"
UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
      "Accept": "application/json"}


def open_round(symbol: str):
    n = int(time.time() * 1000)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT slug, start_ms, end_ms, up_token_id, down_token_id "
            "FROM rounds WHERE symbol=? AND end_ms>? AND up_token_id IS NOT NULL "
            "ORDER BY start_ms DESC LIMIT 1", (symbol.upper(), n)).fetchone()
    finally:
        con.close()


async def main() -> int:
    import aiohttp

    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    row = open_round(symbol)
    if row is None:
        print("no open round"); return 1
    slug, start_ms, end_ms, up, dn = row
    age_s = (time.time() * 1000 - start_ms) / 1000.0
    print(f"round {slug}  opened {age_s:.0f}s ago  ({end_ms - time.time()*1000:.0f}ms left)")

    toks = {"up": str(up), "down": str(dn)}
    counts = {t: 0 for t in toks.values()}
    last = {t: None for t in toks.values()}

    async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=5.0,
                                          sock_read=8.0)) as s:
        # The VENUE's own view first, so the comparison has a reference.
        for name, tok in toks.items():
            try:
                async with s.get(BOOK_URL % tok, headers=UA, timeout=6.0) as r:
                    d = await r.json()
                bids = [float(x["price"]) for x in (d.get("bids") or []) if float(x["size"]) > 0]
                asks = [float(x["price"]) for x in (d.get("asks") or []) if float(x["size"]) > 0]
                bb = max(bids) if bids else 0.0
                ba = min(asks) if asks else 0.0
                print(f"  REST {name:5s} {tok[:16]}... bid {bb:.2f} ask {ba:.2f} "
                      f"levels {len(bids)}/{len(asks)}")
            except Exception as e:                            # noqa: BLE001
                print(f"  REST {name:5s} ERR {type(e).__name__}: {e}")

        async with s.ws_connect(WS_URL, heartbeat=20.0) as ws:
            await ws.send_json({"assets_ids": list(toks.values()), "type": "market"})
            print("  ws subscribed; counting frames for 25s ...")
            t0 = time.monotonic()
            while time.monotonic() - t0 < 25.0:
                try:
                    m = await asyncio.wait_for(ws.receive(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if m.type.name != "TEXT":
                    continue
                try:
                    d = json.loads(m.data)
                except ValueError:
                    continue
                for msg in (d if isinstance(d, list) else [d]):
                    if not isinstance(msg, dict):
                        continue
                    # DELTAS ONLY. A `book` snapshot proves nothing -- the whole
                    # hypothesis is that we get the snapshot and then silence.
                    for ch in (msg.get("price_changes") or []):
                        if not isinstance(ch, dict):
                            continue
                        a = str(ch.get("asset_id") or "")
                        if a in counts:
                            counts[a] += 1
                            last[a] = (ch.get("best_bid"), ch.get("best_ask"))
            await ws.close()

    print("\n  frames received per asset in 25s:")
    for name, tok in toks.items():
        print(f"    {name:5s} {counts[tok]:5d}   last best_bid/best_ask {last[tok]}")
    tot = sum(counts.values())
    print(f"\n  TOTAL {tot}")
    print("  ZERO for an asset means the venue is not pushing it -- our book for "
          "it\n  can only ever be its opening snapshot, and every order priced "
          "off it is\n  priced off the round's first second.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
