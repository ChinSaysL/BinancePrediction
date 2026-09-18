"""The fast path for Chainlink's on-chain history.

`tools/cl_onchain_backtest.py` walks `getRoundData(i)` one round per RPC call,
which is why it stalled at ~50 prints/min. Chainlink's aggregator EMITS an
`AnswerUpdated(int256 current, uint256 roundId, uint256 updatedAt)` event on
every print, and `eth_getLogs` returns all of them for a block range in ONE
call.

This probe answers four things before any of that is worth building:
  1. does the proxy expose `aggregator()`?
  2. does eth_getLogs work on a public RPC at all? (batch is throttled, logs
     may not be)
  3. what block range will an endpoint serve in one go?
  4. what is the real print cadence -- the memory note says BTC gaps run
     median 33 s, i.e. ~1.9 prints per 60 s window.

Usage: python3 tools/cl_logs_probe.py
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request

PROXY = "0xc907E116054Ad103354f2D350FD2514433D57F6f"   # Chainlink BTC/USD, Polygon
SEL_AGG = "0x245a7bfc"                                  # aggregator()
TOPIC = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon.blockpi.network/v1/rpc/public",
]


def rpc(url: str, method: str, params: list, timeout: float = 30.0):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as h:
        r = json.load(h)
    if "error" in r:
        raise RuntimeError(r["error"].get("message", r["error"]))
    return r.get("result")


def main() -> int:
    live = None
    for url in RPCS:
        try:
            bn = int(rpc(url, "eth_blockNumber", []), 16)
            print(f"  OK   {url:<48} head {bn}")
            live = url
            break
        except Exception as e:                                   # noqa: BLE001
            print(f"  FAIL {url:<48} {type(e).__name__}: {str(e)[:60]}")
    if not live:
        print("\nnothing reachable; stop here"); return 1

    print(f"\n1) aggregator() on the proxy ({live})")
    try:
        agg = "0x" + rpc(live, "eth_call", [{"to": PROXY, "data": SEL_AGG}, "latest"])[-40:]
        print(f"   aggregator = {agg}")
    except Exception as e:                                       # noqa: BLE001
        print(f"   FAILED: {e}"); return 1

    print("\n2) eth_getLogs, growing block ranges (Polygon ~2 s/block)")
    t = dt.datetime.now(dt.timezone.utc)
    for span in (200, 2_000, 20_000):
        frm, to = hex(bn - span), "latest"
        t0 = time.time()
        try:
            logs = rpc(live, "eth_getLogs",
                       [{"address": agg, "topics": [TOPIC], "fromBlock": frm, "toBlock": to}],
                       timeout=60.0)
            el = time.time() - t0
            print(f"   span {span:>6} blocks -> {len(logs):>4} prints in {el:6.2f}s"
                  f"   ({span * 2 / 86400:.2f} days of chain)")
            if len(logs) >= 2:
                ts = [int(l["data"][2:][64:128], 16) for l in logs]   # updatedAt
                gaps = [b - a for a, b in zip(ts, ts[1:])]
                gaps.sort()
                print(f"      gaps: n={len(gaps)} median {gaps[len(gaps) // 2]}s "
                      f"min {gaps[0]}s max {gaps[-1]}s")
                print(f"      window {dt.datetime.fromtimestamp(ts[0], dt.timezone.utc)} "
                      f"-> {dt.datetime.fromtimestamp(ts[-1], dt.timezone.utc)}")
                print(f"      last answer {int(logs[-1]['data'][2:][:64], 16) / 1e8:.2f}")
                per60 = 60 / (sum(gaps) / len(gaps))
                print(f"      cadence -> {per60:.2f} prints per 60 s window")
                print(f"      to cover 8 days of chain: "
                      f"{8 * 86400 / (span * 2) * el / 60:.1f} min at this span")
        except Exception as e:                                   # noqa: BLE001
            print(f"   span {span:>6} blocks -> FAILED {type(e).__name__}: {str(e)[:80]}")
    print(f"\n   ({dt.datetime.now(dt.timezone.utc) - t} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
