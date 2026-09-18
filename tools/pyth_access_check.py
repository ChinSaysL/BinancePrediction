"""Are Pyth's 401s a PAYWALL, or just an API KEY requirement (possibly free)?

`tools/pyth_probe.py` showed hermes answering 401 on every price route while
`/v2/price_feeds` (the catalogue) still returns 200. A dead product 404s; a
gated one 401s and keeps the catalogue open. Those lead to opposite decisions,
so this pins down which it is, and also fixes a bug in the first probe: the
benchmarks routes take `ids[]=`, not `ids=`, so one of its 401s may have been
a 400 misread.

It prints the response headers and body for every rejection -- vendors usually
put the reason ("missing api key", "subscribe at ...") in the body -- and tries
the alternate public hosts that predate the current gating.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

BTC = "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
TS = 1789650600

TRIES = [
    ("hermes  latest  ids[]", f"https://hermes.pyth.network/v2/updates/price/latest?ids[]={BTC}"),
    ("hermes  latest  ids=",  f"https://hermes.pyth.network/v2/updates/price/latest?ids={BTC}"),
    ("hermes  hist    ids[]", f"https://hermes.pyth.network/v2/updates/price/{TS}?ids[]={BTC}"),
    ("hermes  catalogue",     "https://hermes.pyth.network/v2/price_feeds?query=btc&asset_type=crypto"),
    ("bench   latest  ids[]", f"https://benchmarks.pyth.network/v1/updates/price/latest?ids[]={BTC}"),
    ("bench   hist    ids[]", f"https://benchmarks.pyth.network/v1/updates/price/{TS}?ids[]={BTC}"),
    ("bench   tv 1s",         "https://benchmarks.pyth.network/v1/shims/tradingview/history"
                              "?symbol=Crypto.BTC%2FUSD&resolution=1&from=1789650300&to=1789650600"),
    ("xc-mainnet hist",       f"https://xc-mainnet.pyth.network/v1/updates/price/{TS}?ids[]={BTC}"),
    ("xc-mainnet latest",     f"https://xc-mainnet.pyth.network/v1/updates/price/latest?ids[]={BTC}"),
]


def probe(name: str, url: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as h:
            body = h.read()[:220].decode(errors="replace")
            print(f"  {name:<22} 200  {body[:150]}")
            return
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in e.headers.items()}
        interesting = {k: v for k, v in hdrs.items()
                       if k in ("www-authenticate", "x-ratelimit-limit",
                                "x-request-id", "server", "content-type")}
        body = e.read()[:220].decode(errors="replace")
        print(f"  {name:<22} {e.code}  hdrs={interesting}")
        print(f"      body: {body[:200]}")
    except Exception as e:                                       # noqa: BLE001
        print(f"  {name:<22} ERR  {type(e).__name__}: {str(e)[:100]}")


def main() -> int:
    print("=== REST routes ===")
    for name, url in TRIES:
        probe(name, url)

    print("\n=== does the WebSocket 401 carry a reason? ===")
    import asyncio
    try:
        import websockets
    except ImportError:
        print("  websockets not installed"); return 0

    async def ws(url: str, sub: dict | None):
        try:
            async with websockets.connect(url, open_timeout=15) as ws:
                print(f"  {url} -> CONNECTED")
                if sub:
                    await ws.send(json.dumps(sub))
                    m = await asyncio.wait_for(ws.recv(), timeout=10)
                    print(f"      first message: {str(m)[:200]}")
        except Exception as e:                                   # noqa: BLE001
            print(f"  {url} -> {type(e).__name__}: {str(e)[:160]}")

    asyncio.run(ws("wss://hermes.pyth.network/ws",
                   {"type": "subscribe", "ids": [BTC]}))
    asyncio.run(ws("wss://benchmarks.pyth.network/v1/ws",
                   {"type": "subscribe", "ids": [BTC]}))
    asyncio.run(ws("wss://xc-mainnet.pyth.network/v1/ws",
                   {"type": "subscribe", "ids": [BTC]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
