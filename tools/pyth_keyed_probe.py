"""Pyth with a real API key: does the paid path actually give us anything?

Hermes is keyed since 2026-08-26 16:00 UTC (docs verbatim: "a free trial is
included, paid plans cover ongoing use"), and the keyless probe returned 401 on
every price route plus the WebSocket handshake. Now that a key exists, three
things have to be true before this is worth building on:

  1. the key is ACCEPTED (REST and WS, old host and the new dourolabs host)
  2. the data is FRESH and HIGH-RESOLUTION -- Pyth Core advertises 400 ms, and
     400 ms is the whole point: Chainlink's free on-chain feed ran 33 s and loss
     to Binance by 3.3 pp because of it
  3. HISTORY is reachable, because the only honest test is scoring it on the
     932 already-settled rounds against the venue's own winner, not eyeballing
     the live feed

The key is read from .env and never printed -- only its length and prefix.

Usage: python3 tools/pyth_keyed_probe.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request

BTC = "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
ROUND_TS = 1789650600          # inside the settled-round window, 09-17 13:10Z
UA = {"User-Agent": "Mozilla/5.0"}


def load_key() -> str | None:
    key = os.environ.get("PYTH")
    if key and key.strip():
        return key.strip()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, ".env")
    if not os.path.exists(path):
        return None
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r'\s*PYTH\s*=\s*["\']?([^"\'\s#]+)', line)
        if m:
            return m.group(1)
    return None


def call(url: str, key: str, timeout: float = 25.0):
    req = urllib.request.Request(url, headers={**UA, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as h:
            return h.status, h.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode(errors="replace")
    except Exception as e:                                       # noqa: BLE001
        return -1, f"{type(e).__name__}: {str(e)[:120]}"


def summarize(body: str) -> str:
    try:
        d = json.loads(body)
    except Exception:                                            # noqa: BLE001
        return body[:120]
    if isinstance(d, dict) and "parsed" in d:
        arr = d["parsed"]
        if not arr:
            return "parsed=[] (empty)"
        p = arr[0]["price"]
        px = int(p["price"]) * 10 ** p["expo"]
        return (f"n_parsed={len(arr)} first {px:,.2f} "
                f"publish_time {p['publish_time']} (age {int(time.time()) - int(p['publish_time'])}s)")
    if isinstance(d, list):
        return f"list of {len(d)}"
    return f"keys={sorted(d)[:6]}"


def main() -> int:
    key = load_key()
    if not key:
        print("no PYTH key found in env or .env"); return 1
    print(f"key loaded: {len(key)} chars, starts '{key[:4]}...' (not printed in full)\n")

    hosts = [
        ("hermes.pyth.network   ", "https://hermes.pyth.network"),
        ("pyth.dourolabs.app    ", "https://pyth.dourolabs.app/hermes"),
    ]
    print("=== 1. REST accepted? ===")
    agreed = []
    for name, base in hosts:
        for label, path in (("latest", f"/v2/updates/price/latest?ids[]={BTC}"),
                            ("hist  ", f"/v2/updates/price/{ROUND_TS}?ids[]={BTC}"),
                            ("feed  ", f"/v2/updates/price/latest?ids[]={BTC}&parsed=true")):
            st, body = call(base + path, key)
            print(f"  {name} {label}  {st}  {summarize(body)}")
            if st == 200 and label == "latest":
                agreed.append((name.strip(), base))
    if not agreed:
        print("\n  no host accepted the key -- stopping"); return 1

    base = agreed[0][1]
    print(f"\n  using {base}")

    print("\n=== 2. freshness / resolution of the REST feed ===")
    seen = []
    t0 = time.time()
    while time.time() - t0 < 20:
        st, body = call(f"{base}/v2/updates/price/latest?ids[]={BTC}", key)
        if st == 200:
            try:
                p = json.loads(body)["parsed"][0]["price"]
                seen.append((time.time(), int(p["publish_time"]), int(p["price"]) * 10 ** p["expo"]))
            except Exception:                                    # noqa: BLE001
                pass
        time.sleep(0.2)
    if len(seen) >= 3:
        pubs = sorted({p for _, p, _ in seen})
        gaps = [b - a for a, b in zip(pubs, pubs[1:])]
        lag = sorted(t - p for t, p, _ in seen)
        print(f"  {len(seen)} polls -> {len(pubs)} distinct publish_time")
        if gaps:
            g = sorted(gaps)
            print(f"  gap median {g[len(g) // 2]}s  min {g[0]}s  max {g[-1]}s")
        print(f"  staleness median {lag[len(lag) // 2]:.1f}s  min {lag[0]:.1f}s")
    else:
        print(f"  only {len(seen)} usable polls")

    print("\n=== 3. can it serve HISTORY (needed for the 932-round backtest)? ===")
    for label, path in (("hist single ts", f"/v2/updates/price/{ROUND_TS}?ids[]={BTC}&parsed=true"),
                        ("hist ts-1      ", f"/v2/updates/price/{ROUND_TS - 1}?ids[]={BTC}&parsed=true")):
        st, body = call(base + path, key)
        print(f"  {label}  {st}  {summarize(body)}")
    st, body = call(f"https://benchmarks.pyth.network/v1/updates/price/{ROUND_TS}?ids[]={BTC}&parsed=true", key)
    print(f"  benchmarks hist   {st}  {summarize(body)}")

    print("\n=== 4. WebSocket ===")
    try:
        import websockets                                        # noqa: PLC0415
    except ImportError:
        print("  websockets not installed"); return 0

    async def ws_probe(url: str, label: str):
        hdr = {"Authorization": f"Bearer {key}"}
        try:
            async with websockets.connect(url, additional_headers=hdr,
                                          open_timeout=15) as ws:
                print(f"  {label} CONNECTED")
                await ws.send(json.dumps({"type": "subscribe", "ids": [BTC]}))
                t_s = time.time()
                pubs, n, raws = [], 0, []
                while time.time() - t_s < 20:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=20)
                    except asyncio.TimeoutError:
                        break
                    n += 1
                    if len(raws) < 4:
                        raws.append(str(msg)[:300])
                    try:
                        d = json.loads(msg)
                    except Exception:                            # noqa: BLE001
                        continue
                    if isinstance(d, dict) and d.get("type") not in (None, "price_update"):
                        if len(raws) < 8:
                            raws.append(f"typed: {str(d)[:300]}")
                    for it in (d.get("parsed") or []):
                        pr = it.get("price") or {}
                        if pr.get("publish_time"):
                            pubs.append((time.time(), int(pr["publish_time"])))
                print(f"    {n} frames received; first frames:")
                for r in raws[:6]:
                    print(f"      {r}")
                if pubs:
                    uniq = sorted({p for _, p in pubs})
                    gaps = [b - a for a, b in zip(uniq, uniq[1:])]
                    g = sorted(gaps) if gaps else [0]
                    lag = sorted(t - p for t, p in pubs)
                    print(f"    {len(uniq)} distinct publishes, gap median "
                          f"{g[len(g) // 2]}s, wall-lag median "
                          f"{lag[len(lag) // 2]:.1f}s")
                else:
                    print("    no price payloads -- subscribe format needs fixing")
        except Exception as e:                                   # noqa: BLE001
            print(f"  {label} FAILED {type(e).__name__}: {str(e)[:130]}")

    asyncio.run(ws_probe("wss://hermes.pyth.network/ws", "hermes.pyth.network/ws"))
    asyncio.run(ws_probe("wss://pyth.dourolabs.app/hermes/ws", "dourolabs/hermes/ws"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
