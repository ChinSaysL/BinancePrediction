"""Record the prediction-market orderbook from the WEBSOCKET, sub-second.

WHY THIS EXISTS. Every slippage number in this repo comes from the ~1s REST
poll, and the whole event we are arguing about finishes inside one second:

    pre-cross poll  0.505          <- what self._asks holds at the trigger
    cross tick      0.618  (+66%)  <- my first sample already shows most of it
    +1s             0.638
    +4s             0.645  (fill)

So the 1s tape cannot say whether a sub-second window exists in which the book
has NOT yet repriced. Four execution fixes were tested against that tape and
all measured worse -- which is what you would expect if the tape is too coarse
to contain the thing being tested. The WS pushes 1-3/s average, ~10/s peak, at
<200ms latency, which is 3-10x finer.

READ ONLY. Separate process, no orders, no contact with the bot's trading
path. It writes JSONL and nothing else.

    python -m tools.record_ws_book --minutes 120 --out data/wsbook.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sqlite3
import sys
import time

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings          # noqa: E402
from btcpredict.execution.live import LiveBroker  # noqa: E402

WS = "wss://api.binance.com/sapi/wss"

# VERIFIED LIVE 2026-09-13, and the docs are wrong in three places:
#   * the AGGREGATED topic (web3_prediction_orderbook_data) registers with
#     SUCCESS and then pushes nothing. Only per-market topics deliver.
#   * frames arrive as type "DATA", not the documented "TOPIC".
#   * prices and sizes are JSON NUMBERS, not the documented strings.
# Observed cadence on a live BTC 5m market: 178-238ms between snapshots
# (~5/s), against the documented "1-3/s average, 10/s peak" -- and against the
# ~1s REST poll that every slippage number in this repo was measured on.
TOPIC_FMT = "web3_prediction_orderbook_{}"


def live_market_ids(db_path: str, ahead: int = 3) -> list[int]:
    """The BTC 5m markets worth listening to: the live one plus the next few.

    The topic is a CONNECT-TIME query parameter, so following a 5-minute
    rolling market would otherwise mean a handshake every five minutes -- and
    a reconnect is exactly when a snapshot feed loses the moment we care
    about. Subscribing to several at once (the docs allow "|" concatenation)
    buys ~15 minutes per connection instead.
    """
    con = sqlite3.connect(db_path)
    con.execute("pragma busy_timeout=20000")
    now = int(time.time() * 1000)
    rows = con.execute(
        "SELECT market_id FROM rounds WHERE slug LIKE 'btc-updown-5m-%' "
        "AND market_id IS NOT NULL AND end_ms > ? ORDER BY end_ms LIMIT ?",
        (now, ahead)).fetchall()
    con.close()
    return [int(r[0]) for r in rows]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--out", default="data/wsbook.jsonl")
    ap.add_argument("--db", default="data/btcpredict.db")
    a = ap.parse_args()

    if not (settings.binance_api_key and settings.binance_api_secret):
        print("no API credentials in the environment", file=sys.stderr)
        return 2

    async with aiohttp.ClientSession() as session:
        signer = LiveBroker(session, settings.binance_api_key,
                            settings.binance_api_secret)
        # `random` is required by the docs; a nonce so repeat connections are
        # not deduplicated by the gateway.
        deadline = time.time() + a.minutes * 60.0
        n = 0
        seen_markets: set[int] = set()
        backoff = 1.0
        with open(a.out, "a", buffering=1) as fh:
            while time.time() < deadline:
                mids = live_market_ids(a.db)
                if not mids:
                    print("no live BTC 5m market yet; waiting", flush=True)
                    await asyncio.sleep(5.0)
                    continue
                topic = "|".join(TOPIC_FMT.format(m) for m in mids)
                # Resubscribe once the live market is no longer the one we
                # joined on, so the feed follows the rolling round.
                joined_on = mids[0]
                qs = signer._sign({"random": secrets.token_hex(8), "topic": topic})
                url = f"{WS}?{qs}"
                try:
                    async with session.ws_connect(
                        url, headers={"X-MBX-APIKEY": settings.binance_api_key},
                        heartbeat=30.0, timeout=20,
                    ) as ws:
                        print(f"connected: {len(mids)} market(s) {mids}", flush=True)
                        backoff = 1.0
                        last_check = time.time()
                        # NOT `async for msg in ws`: it blocks until a frame
                        # arrives, and a market goes SILENT the moment its
                        # round ends -- so the resubscribe check below never
                        # runs and the recorder sits on a dead topic forever.
                        # That is exactly what happened on the first run: it
                        # stopped writing at 16:35 and nothing said so.
                        while True:
                            try:
                                msg = await asyncio.wait_for(ws.receive(),
                                                             timeout=1.0)
                            except asyncio.TimeoutError:
                                msg = None
                            if time.time() >= deadline:
                                break
                            if time.time() - last_check > 30.0:
                                last_check = time.time()
                                cur = live_market_ids(a.db)
                                if cur and cur[0] != joined_on and joined_on not in cur:
                                    print("markets rolled; resubscribing", flush=True)
                                    break
                            if msg is None:
                                continue                  # quiet: re-check
                            if msg.type in (aiohttp.WSMsgType.CLOSED,
                                            aiohttp.WSMsgType.CLOSING,
                                            aiohttp.WSMsgType.ERROR):
                                break
                            if msg.type is not aiohttp.WSMsgType.TEXT:
                                continue
                            recv_ms = int(time.time() * 1000)
                            try:
                                env = json.loads(msg.data)
                                body = env.get("data")
                                d = json.loads(body) if isinstance(body, str) else body
                            except (ValueError, TypeError):
                                continue
                            if not isinstance(d, dict) or d.get("msgType") != "orderbook":
                                continue
                            mid = d.get("marketId")
                            seen_markets.add(mid)
                            # Keep only the top of book plus enough depth to
                            # answer the sizing question; the full 50 levels
                            # would be 10x the disk for no extra question.
                            fh.write(json.dumps({
                                "recv_ms": recv_ms,
                                "upd_ms": d.get("updateTimestampMs"),
                                "market_id": mid,
                                "asks": (d.get("asks") or [])[:5],
                                "bids": (d.get("bids") or [])[:5],
                            }) + "\n")
                            n += 1
                            if n % 500 == 0:
                                print(f"  {n} snapshots, {len(seen_markets)} markets, "
                                      f"{(deadline - time.time())/60:.0f} min left",
                                      flush=True)
                except Exception as e:                       # noqa: BLE001
                    print(f"ws error ({type(e).__name__}: {e}); retry in {backoff:.0f}s",
                          flush=True)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        print(f"done: {n} snapshots across {len(seen_markets)} markets -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
