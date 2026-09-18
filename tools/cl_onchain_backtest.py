"""Can the FREE on-chain Chainlink feed replace Binance for these rounds?

WHY. Polymarket's own market metadata names the resolution source:

    resolutionSource   = https://data.chain.link/streams/btc-usd-twap-60s-streams
    cryptoMarketConfig = {id: btc-5m-twap-60, twapLookbackSeconds: 60}

Chainlink's *Data Streams* product (the one the venue pays on) is a paid
subscription -- no free tier, $150/month per feed. But Chainlink ALSO runs the
older push-based *Data Feeds*, and those publish on a public chain, where
reading costs nothing. Measured from Chainlink's own feed directory
(`reference-data-directory.vercel.app/feeds-matic-mainnet.json`):

    BTC/ETH/BNB  heartbeat 27 s, deviation 0.1%
    SOL/XRP/DOGE heartbeat 27 s, deviation 0.25%

and off Polygon, BTC round gaps run median 33 s (min 22 s). The directory lists
ZERO twap-named feeds, so what is on-chain is Chainlink's aggregated BTC/USD
SPOT, not the `btc-usd-twap-60s` series. The bet this file tests is:

    a 60 s mean of Chainlink's own spot prints is a better estimate of the
    venue's settle value than a 60 s mean of Binance's tape -- same source
    family removes the Binance-vs-Chainlink basis, at the cost of sampling
    only ~2 prints per window.

It scores the on-chain statistic and the Binance statistic against the venue's
own settled winner on the SAME rounds.

Usage:
    python3 tools/cl_onchain_backtest.py /tmp/pm_rounds.json
where pm_rounds.json is [[slug, start_ms, end_ms, strike, winner], ...].
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROXY = "0xc907E116054Ad103354f2D350FD2514433D57F6f"   # Chainlink BTC/USD, Polygon
SEL_ROUND = "0x9a6fc8f5"          # getRoundData(uint80)
SEL_LATEST = "0xfeaf968c"         # latestRoundData()

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon.blockpi.network/v1/rpc/public",
]
_rpc = {"url": RPCS[0]}
NBATCH = 12
LOOKBACK = 60.0


def _post(payload, timeout: float = 90.0, tries: int = 6):
    """POST a JSON-RPC payload, rotating endpoints and backing off on 429."""
    last: Exception | None = None
    for a in range(tries):
        try:
            req = urllib.request.Request(
                _rpc["url"], data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as h:
                return json.load(h)
        except Exception as e:                                   # noqa: BLE001
            last = e
            i = RPCS.index(_rpc["url"]) if _rpc["url"] in RPCS else 0
            if i + 1 < len(RPCS):
                _rpc["url"] = RPCS[i + 1]
                print(f"    rpc -> {_rpc['url']} ({type(e).__name__})", flush=True)
            time.sleep(0.8 * (a + 1))
    raise last if last else RuntimeError("rpc failed")


def _get(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as h:
        return json.load(h)


def _words(hx: str):
    if not hx or len(hx) < 2 + 320:
        return None
    w = hx[2:]
    return [int(w[64 * i:64 * (i + 1)], 16) for i in range(5)]


def _rpc_call(to: str, data: str, tries: int = 8):
    """One eth_call, rotating endpoints and backing off. Public RPCs throttle
    batch JSON-RPC hard but tolerate sequential single calls."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
               "params": [{"to": to, "data": data}, "latest"]}
    last: Exception | None = None
    for a in range(tries):
        try:
            req = urllib.request.Request(
                _rpc["url"], data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as h:
                return json.load(h).get("result")
        except Exception as e:                                   # noqa: BLE001
            last = e
            i = RPCS.index(_rpc["url"]) if _rpc["url"] in RPCS else 0
            _rpc["url"] = RPCS[(i + 1) % len(RPCS)]
            time.sleep(0.4 * (a + 1))
    raise last if last else RuntimeError("rpc failed")


def _one(i: int):
    try:
        return _words(_rpc_call(PROXY, SEL_ROUND + f"{i:064x}", tries=4))
    except Exception:                                            # noqa: BLE001
        return None


def chain_series(t_min: float, cap: int = 12000, block: int = 300,
                 workers: int = 6) -> list[tuple[float, float]]:
    """[(unix_second, Chainlink BTC/USD)] back to t_min, free, off Polygon.

    One eth_call per round, but ~6 in flight at once: public RPCs throttle
    batch JSON-RPC and are happy with a few parallel singles.
    """
    rid = _words(_rpc_call(PROXY, SEL_LATEST))[0]
    out: list[tuple[float, float]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while len(out) < cap and rid > 1:
            ids = [r for r in range(rid - block + 1, rid + 1) if r > 0]
            got = list(ex.map(_one, ids))
            if sum(1 for w in got if w) < 0.5 * len(ids):
                time.sleep(3.0)
                got = list(ex.map(_one, ids))
            for w in got:
                if w:
                    out.append((float(w[3]), w[1] / 1e8))
            rid -= block
            oldest = min(t for t, _ in out)
            print(f"    {len(out)} prints, back to "
                  f"{dt.datetime.fromtimestamp(oldest, dt.UTC):%m-%d %H:%M}Z", flush=True)
            if oldest <= t_min:
                break
            time.sleep(0.3)
    out.sort()
    return [p for p in out if p[0] >= t_min - LOOKBACK]


def binance_series(start_s: float, end_s: float) -> list[tuple[float, float]]:
    """[(unix_second, Binance BTCUSDT 1s close)] over [start, end]."""
    out: list[tuple[float, float]] = []
    t = int(start_s * 1000)
    while t < end_s * 1000:
        u = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s"
             f"&startTime={t}&limit=1000")
        try:
            rows = _get(u)
        except Exception:
            break
        if not rows:
            break
        for k in rows:
            out.append((float(k[0]) / 1000.0, float(k[4])))
        nt = int(rows[-1][0]) + 1000
        if nt <= t:
            break
        t = nt
        time.sleep(0.15)
    return out


def mean_over(series, end: float, window: float):
    v = [p for t, p in series if end - window <= t <= end]
    return (statistics.mean(v), len(v)) if v else (None, 0)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pm_rounds.json"
    rows = json.load(open(path))
    t_min = min(r[1] for r in rows) / 1000.0 - 120
    t_max = max(r[2] for r in rows) / 1000.0 + 10
    print(f"{len(rows)} settled 5m rounds, "
          f"{dt.datetime.fromtimestamp(t_min, dt.UTC):%m-%d %H:%M} -> "
          f"{dt.datetime.fromtimestamp(t_max, dt.UTC):%m-%d %H:%M}Z")

    print("walking the chain backwards (free, no key)...")
    cl = chain_series(t_min)
    if not cl:
        print("no on-chain prints returned"); return 1
    gaps = [cl[i + 1][0] - cl[i][0] for i in range(len(cl) - 1)]
    off = [g for g in gaps if g > 0]
    print(f"  {len(cl)} prints | gap median {statistics.median(off):.0f}s "
          f"mean {statistics.mean(off):.1f}s max {max(off):.0f}s "
          f"| {LOOKBACK / statistics.mean(off):.1f} prints per {LOOKBACK:.0f}s window")

    print("fetching Binance 1s tape for the same window...")
    bin_ = binance_series(t_min, t_max)
    print(f"  {len(bin_)} 1s bars")

    keys = ["binance last print", "chainlink last print",
            "binance tail-60 mean", "chainlink tail-60 mean"]
    agree = {k: 0 for k in keys}
    n = 0
    dis = []
    for slug, start_ms, end_ms, strike, winner in rows:
        end = end_ms / 1000.0
        bl = max((p for t, p in bin_ if t <= end), default=None)
        cll = max((p for t, p in cl if t <= end), default=None)
        bm, kb = mean_over(bin_, end, LOOKBACK)
        cm, kc = mean_over(cl, end, LOOKBACK)
        if None in (bl, cll, bm, cm) or min(kb, kc) < 1:
            continue
        up = winner == "Up"
        n += 1
        for k, v in zip(keys, (bl, cll, bm, cm)):
            agree[k] += ((v >= strike) == up)
        if (cm >= strike) != (bm >= strike):
            dis.append((slug, end, strike, winner, cm, bm, cll, bl))
    if not n:
        print("no rounds scoreable"); return 1

    print(f"\n=== agreement with the venue's own winner, same {n} rounds ===")
    for k in keys:
        print(f"  {k:<24} {agree[k] / n:6.1%}")
    print(f"  prints per round, chainlink tail-60: "
          f"{statistics.mean([mean_over(cl, r[2] / 1000.0, LOOKBACK)[1] for r in rows]):.1f}"
          f"  | binance: "
          f"{statistics.mean([mean_over(bin_, r[2] / 1000.0, LOOKBACK)[1] for r in rows]):.0f}")

    print(f"\n=== where the two statistics pick OPPOSITE sides (n={len(dis)}) ===")
    if len(dis) >= 6:
        c_ok = sum(1 for _, _, K, w, cm, bm, _, _ in dis if ((cm >= K) == (w == "Up")))
        b_ok = len(dis) - c_ok
        print(f"  chainlink right {c_ok}/{len(dis)} = {c_ok / len(dis):.1%}")
        print(f"  binance   right {b_ok}/{len(dis)} = {b_ok / len(dis):.1%}")
        for slug, end, K, w, cm, bm, cll, bl in dis[:12]:
            print(f"    {dt.datetime.fromtimestamp(end, dt.UTC):%m-%d %H:%M} {slug:<34} "
                  f"K={K:<12.2f} winner={w:<4} cl={cm:>10,.2f} bin={bm:>10,.2f}")
    else:
        print("  too few -- the two statistics almost always agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
