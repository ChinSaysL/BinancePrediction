"""Is the FREE (permissionless, on-chain) Pyth path usable?

Pyth Hermes needs an API key (401 verified; docs say "a free trial is included,
paid plans cover ongoing use"), so the only zero-cost path left is the on-chain
push feed, which the docs call "permissionless". Two things decide whether that
is worth anything, and neither is in the docs cleanly for Polygon:

  1. the upgraded Core contract address on Polygon
  2. how STALE the stored price actually is

(2) matters because a note from 2026-08-26 recorded the keyless on-chain Pyth
feed as **4.66 DAYS stale**, but that predates the Aug 26 upgrade and the whole
contract generation has changed since. Stale-and-free is useless; fresh-and-free
is a real second source, so this re-measures rather than inherits the note.

The push-feed tables give a default heartbeat of 55 s / 0.5% deviation, which is
already coarser than Chainlink's measured 33 s on Polygon -- and the Chainlink
test showed a coarser same-family feed LOSES to Binance. This checks whether the
Pyth side is even alive before arguing about resolution.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

BTC = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
RPC = "https://polygon-bor-rpc.publicnode.com"
UA = {"User-Agent": "Mozilla/5.0"}
# selector candidates for reading a stored price, tried in order
SELECTORS = {
    "getPriceUnsafe(bytes32)": "96834ec1",
    "getPriceNoOlderThan(bytes32,uint256)": "a4ae35e1",
    "getPrice(bytes32)": "31d98b3f",
}
LEGACY = "0xff1a0f4744e8582DF1aE09D5611b887B6a12925C"     # pre-upgrade Polygon
PRO_ON_POLYGON = "0xACeA"                                  # only the prefix is known


def rpc(method: str, params: list, timeout: float = 30.0):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(RPC, data=json.dumps(body).encode(),
                                 headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as h:
        r = json.load(h)
    if "error" in r:
        raise RuntimeError(str(r["error"])[:140])
    return r.get("result")


def eth_call(to: str, data: str) -> str | None:
    try:
        return rpc("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as e:                                       # noqa: BLE001
        print(f"      revert: {str(e)[:100]}")
        return None


def decode(words: list[int], now: int) -> str:
    price = words[0]
    if price >= 2 ** 63:                     # int64 two's complement
        price -= 2 ** 64
    conf, expo = words[1], words[2]
    if expo >= 2 ** 31:
        expo -= 2 ** 32
    pub = words[3]
    px = price * 10 ** expo
    return (f"price {px:,.2f}  conf {conf * 10 ** expo:,.2f}  expo {expo}  "
            f"publishTime {pub}  AGE {now - pub}s ({((now - pub) / 86400):.2f} days)")


def find_polygon_address() -> list[str]:
    url = "https://docs.pyth.network/price-feeds/core/upgrade/contracts"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as h:
            html = h.read().decode(errors="replace")
    except Exception as e:                                       # noqa: BLE001
        print(f"  could not fetch docs: {type(e).__name__}"); return []
    return sorted(set(re.findall(r"0x6E7D[a-fA-F0-9]{36}", html)))


def main() -> int:
    now = int(time.time())
    print("=== addresses from the docs page ===")
    found = find_polygon_address()
    print(f"  0x6E7D* candidates: {found or 'none in HTML'}")

    targets = [("legacy pre-upgrade", LEGACY)] + \
              [(f"docs candidate {a[:10]}", a) for a in found]
    print("\n=== on-chain freshness (free path) ===")
    for label, addr in targets:
        print(f"  {label}  {addr}")
        for name, sel in SELECTORS.items():
            data = "0x" + sel + BTC
            if "NoOlderThan" in name:
                data += f"{3600:064x}"
            out = eth_call(addr, data)
            if not out or len(out) < 2 + 64 * 4:
                continue
            w = out[2:]
            words = [int(w[64 * i:64 * (i + 1)], 16) for i in range(4)]
            if words[0] == 0 and words[3] == 0:
                continue
            print(f"    {name}: {decode(words, now)}")
            break
    print("\n  (a 4.66-day age would confirm the old note; seconds would mean"
          "\n   the Aug 26 upgrade replaced that stale feed and it is usable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
