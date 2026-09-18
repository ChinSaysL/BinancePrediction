"""Is Pyth's V2 webservice actually free, and is it fresh enough to matter?

A note recorded earlier (2026-08-26) says Pyth is paid and that the keyless
ON-CHAIN feed was 4.66 DAYS stale. That observation was about the on-chain
contract, not about Hermes -- Pyth's off-chain price service. These are
different things and only one of them was measured, so the claim is retested
here end to end.

Four questions, in the order that decides whether it is worth anything:
  1. does hermes / benchmarks answer WITHOUT an API key?
  2. latest BTC/USD -- and how old is it?
  3. can it serve HISTORY over our settled rounds (that is what a backtest
     against the venue's own winner needs)?
  4. what is the real update cadence -- Chainlink's on-chain feed was 33 s,
     which is what sank it. Anything near 1 s changes the picture.

Run: python3 tools/pyth_probe.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BTC = "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
HERMES = "https://hermes.pyth.network"
BENCH = "https://benchmarks.pyth.network"
# a timestamp inside our settled-round window (09-17 13:10 UTC)
ROUND_TS = 1789650600


def get(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as h:
        return h.status, json.load(h)


def try_get(url: str) -> tuple[int, object]:
    try:
        return get(url)
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode(errors="replace")
        return e.code, body
    except Exception as e:                                       # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def main() -> int:
    print("=== 1. keyless access ===")
    for name, url in (
        ("hermes latest", f"{HERMES}/v2/updates/price/latest?ids[]={BTC}"),
        ("hermes feeds ", f"{HERMES}/v2/price_feeds?query=btc&asset_type=crypto"),
        ("benchmarks   ", f"{BENCH}/v1/updates/price/latest?ids={BTC}"),
    ):
        st, body = try_get(url)
        note = ""
        if st == 200 and isinstance(body, dict):
            note = f"keys={sorted(body)[:4]}"
        elif isinstance(body, str):
            note = body[:90]
        print(f"  {name:<14} HTTP {st:<4} {note}")

    print("\n=== 2. latest BTC/USD, and its age ===")
    st, body = try_get(f"{HERMES}/v2/updates/price/latest?ids[]={BTC}")
    if st == 200 and isinstance(body, dict):
        p = body["parsed"][0]["price"]
        ex, mant = p["expo"], int(p["price"])
        px = mant * 10 ** ex
        pub = int(p["publish_time"])
        now = int(time.time())
        print(f"  price {px:,.2f}   publish_time {pub} "
              f"({dt.datetime.fromtimestamp(pub, dt.timezone.utc)})   "
              f"age {now - pub}s")
        print(f"  expo {ex}  conf {int(p['conf']) * 10 ** ex:.2f}")
    else:
        print(f"  FAILED {st} {body}")

    print("\n=== 3. history over the settled rounds ===")
    for name, url in (
        ("hermes @ts ",
         f"{HERMES}/v2/updates/price/{ROUND_TS}?ids[]={BTC}"),
        ("bench @ts  ",
         f"{BENCH}/v1/updates/price/{ROUND_TS}?ids={BTC}"),
        ("tv 1s bars ",
         f"{BENCH}/v1/shims/tradingview/history?symbol="
         + urllib.parse.quote("Crypto.BTC/USD")
         + f"&resolution=1&from={ROUND_TS - 300}&to={ROUND_TS}"),
    ):
        st, body = try_get(url)
        if st == 200 and isinstance(body, dict):
            if "parsed" in body:
                p = body["parsed"][0]["price"]
                pub = int(p["publish_time"])
                print(f"  {name} HTTP 200  price "
                      f"{int(p['price']) * 10 ** p['expo']:,.2f}  "
                      f"publish_time {pub} ({ROUND_TS - pub:+}s vs asked)")
            else:
                keys = sorted(body)
                n = len(body.get("t", []) or body.get("c", []))
                print(f"  {name} HTTP 200  keys={keys} n={n}")
                if body.get("t"):
                    print(f"        t {body['t'][:5]} ... c {body['c'][:5]}")
        else:
            print(f"  {name} HTTP {st}  {str(body)[:110]}")

    print("\n=== 4. update cadence ===")
    print("  asking hermes for the latest publish_time repeatedly...")
    seen, t0 = [], time.time()
    while time.time() - t0 < 25:
        st, body = try_get(f"{HERMES}/v2/updates/price/latest?ids[]={BTC}")
        if st == 200 and isinstance(body, dict):
            seen.append((time.time(), int(body["parsed"][0]["price"]["publish_time"])))
        time.sleep(0.35)
    if len(seen) >= 4:
        pubs = [p for _, p in seen]
        uniq = sorted(set(pubs))
        gaps = [b - a for a, b in zip(uniq, uniq[1:])]
        lag = [t - p for t, p in seen]
        print(f"  {len(seen)} polls in {seen[-1][0] - seen[0][0]:.1f}s -> "
              f"{len(uniq)} distinct publish_time values")
        if gaps:
            g = sorted(gaps)
            print(f"  gap between successive publishes: median {g[len(g) // 2]}s "
                  f"min {g[0]}s max {g[-1]}s")
        l = sorted(lag)
        print(f"  staleness (poll time - publish_time): median "
              f"{l[len(l) // 2]:.1f}s  min {l[0]:.1f}s  max {l[-1]:.1f}s")
    else:
        print(f"  only {len(seen)} successful polls; nothing to say")

    print("\n  websocket check (wss://hermes.pyth.network/ws):")
    try:
        import asyncio
        import websockets                                      # noqa: PLC0415
    except ImportError as e:
        print(f"    skipped, no client available ({e})")
        return 0

    async def ws_probe():
        url = "wss://hermes.pyth.network/ws"
        async with websockets.connect(url, open_timeout=15) as ws:
            await ws.send(json.dumps({
                "type": "subscribe",
                "ids": [BTC if BTC.startswith("0x") else "0x" + BTC],
            }))
            t_start = time.time()
            pubs = []
            while time.time() - t_start < 25:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=25)
                except asyncio.TimeoutError:
                    break
                try:
                    d = json.loads(msg)
                except Exception:                              # noqa: BLE001
                    continue
                arr = d.get("parsed") or []
                for item in arr:
                    p = item.get("price") or {}
                    if p.get("publish_time"):
                        pubs.append((time.time(), int(p["publish_time"])))
            return pubs

    try:
        pubs = asyncio.run(ws_probe())
        if pubs:
            uniq = sorted({p for _, p in pubs})
            gaps = [b - a for a, b in zip(uniq, uniq[1:])]
            g = sorted(gaps) if gaps else [0]
            lag = sorted(t - p for t, p in pubs)
            print(f"    messages {len(pubs)}, distinct publishes {len(uniq)}, "
                  f"gap median {g[len(g) // 2]}s, "
                  f"wall-lag median {lag[len(lag) // 2]:.1f}s")
        else:
            print("    connected but received no price messages")
    except Exception as e:                                       # noqa: BLE001
        print(f"    FAILED {type(e).__name__}: {str(e)[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
