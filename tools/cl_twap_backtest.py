"""Rebuild the venue's settle statistic from Chainlink itself, and score it.

THE HYPOTHESIS. The venue's own metadata says it settles on
`btc-usd-twap-60s-streams` (`twapLookbackSeconds: 60`). Every forecast this
project has built so far used BINANCE tape to approximate a CHAINLINK
statistic, and the residual showed up as a hard floor: Binance instantaneous
agrees with the venue 89.7% of the time, and the venue's own rule applied to
Binance tape only reaches 93.1% (84.8% on the rounds that actually have a
two-sided book). No amount of Binance modelling closed that gap, because it is
a SOURCE gap, not a sampling gap.

What was never tested is whether Chainlink's own free on-chain feed can be
substituted for Binance. The on-chain BTC/USD feed is NOT the paid Data Streams
TWAP-60s -- it is Chainlink's aggregated spot, pushed on ~43 s cadence -- but it
comes from the same oracle that settles the market, so the basis should be
smaller even though the sampling is coarser.

WHAT IS SCORED, on the venue's own settled winner, same rounds:

  oracle-perfect   CL tail-60 at close  vs  CL tail-60 at open    (the ceiling)
  chainlink        CL tail-60 AT TAU    vs  CL tail-60 at open    (tradeable)
  binance          Binance tail-60 @tau vs  stored strike         (the incumbent)

The middle row is the one that matters: it is what we could know `tau` seconds
before the close if we had the Chainlink feed instead of Binance.

Usage:
    python3 tools/cl_twap_backtest.py rounds.json [--tau 10]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
import urllib.request

PROXY = "0xc907E116054Ad103354f2D350FD2514433D57F6f"     # Chainlink BTC/USD, Polygon
SEL_AGG = "0x245a7bfc"                                    # aggregator()
TOPIC = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"
LOOKBACK = 60.0
MAXRANGE = 10_000

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon.blockpi.network/v1/rpc/public",
]
_rpc = {"url": RPCS[0]}


def _call(method: str, params: list, timeout: float = 60.0, tries: int = 7):
    for a in range(tries):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            req = urllib.request.Request(
                _rpc["url"], data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as h:
                r = json.load(h)
            if "error" in r:
                raise RuntimeError(r["error"].get("message", str(r["error"])))
            return r.get("result")
        except Exception as e:                                   # noqa: BLE001
            i = RPCS.index(_rpc["url"]) if _rpc["url"] in RPCS else 0
            if i + 1 < len(RPCS):
                _rpc["url"] = RPCS[i + 1]
                print(f"    rpc -> {_rpc['url']} ({str(e)[:50]})")
            time.sleep(0.5 * (a + 1))
    raise RuntimeError(f"{method} failed on every endpoint")


def block_at(ts: int, head: int) -> int:
    """Binary-search the block whose timestamp brackets `ts`."""
    lo, hi = 1, head
    while lo < hi:
        mid = (lo + hi) // 2
        b = _call("eth_getBlockByNumber", [hex(mid), False])
        if int(b["timestamp"], 16) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def chain_series(t_from: int, t_to: int) -> list[tuple[int, float]]:
    """Every Chainlink print in [t_from, t_to], as (unix_s, price)."""
    head = int(_call("eth_blockNumber", []), 16)
    agg = "0x" + _call("eth_call", [{"to": PROXY, "data": SEL_AGG}, "latest"])[-40:]
    b_from, b_to = block_at(t_from, head), block_at(t_to, head)
    print(f"  aggregator {agg}")
    print(f"  blocks {b_from}..{b_to} ({b_to - b_from} blocks, "
          f"{(b_to - b_from) * 2 / 3600:.1f} h of chain)")
    out: list[tuple[int, float]] = []
    b = b_from
    n = 0
    while b <= b_to:
        top = min(b + MAXRANGE - 1, b_to)
        logs = _call("eth_getLogs", [{"address": agg, "topics": [TOPIC],
                                      "fromBlock": hex(b), "toBlock": hex(top)}])
        for lg in logs:
            tp = lg["topics"]
            cur = int(tp[1], 16)
            if cur >= 2 ** 255:                    # int256 two's complement
                cur -= 2 ** 256
            out.append((int(lg["data"], 16), cur / 1e8))
        n += 1
        b = top + 1
    out.sort()
    print(f"  {len(out)} prints in {n} getLogs call(s)")
    if out:
        ps = [p for _, p in out]
        print(f"  price range {min(ps):.2f} .. {max(ps):.2f}   "
              f"first {out[0][0]}:{ps[0]:.2f}  last {out[-1][0]}:{ps[-1]:.2f}")
    if len(out) > 1:
        gaps = sorted(b - a for a, b in zip([x[0] for x in out],
                                            [x[0] for x in out][1:]))
        print(f"  print cadence: median {gaps[len(gaps) // 2]}s "
              f"-> {60 / (sum(gaps) / len(gaps)):.2f} prints per 60 s window")
    return out


def twap(series: list[tuple[int, float]], a: float, b: float) -> float:
    """Time-weighted mean of a step-wise-constant series over [a, b]."""
    if b <= a or not series:
        return float("nan")
    tot = 0.0
    for i, (t, p) in enumerate(series):
        seg_a = max(a, t)
        seg_b = min(b, series[i + 1][0] if i + 1 < len(series) else b)
        if seg_b > seg_a:
            tot += p * (seg_b - seg_a)
    # leading stub: hold the last print from before `a`
    if series[0][0] > a:
        tot += series[0][1] * (min(series[0][0], b) - a)
    span = b - a
    return tot / span


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rounds")
    ap.add_argument("--tau", type=float, default=10.0)
    ap.add_argument("--coin", default="btc",
                    help="the Chainlink series fetched is BTC/USD only")
    a = ap.parse_args()

    rounds = [r for r in json.load(open(a.rounds)) if r["coin"] == a.coin]
    if not rounds:
        print(f"no {a.coin} rounds"); return 1
    print(f"COIN {a.coin.upper()} ONLY -- the fetched feed is Chainlink {a.coin.upper()}/USD. "
          f"Scoring one coin's feed against another coin's strike is not a"
          f" price comparison, so every coin must be run separately.")
    t_from = min(r["start_ms"] for r in rounds) // 1000 - 180
    t_to = max(r["end_ms"] for r in rounds) // 1000 + 60
    print(f"{len(rounds)} rounds, "
          f"{dt.datetime.fromtimestamp(t_from, dt.timezone.utc)} .. "
          f"{dt.datetime.fromtimestamp(t_to, dt.timezone.utc)}")
    coins = sorted({r["coin"] for r in rounds})
    print("  strikes by coin: " + ", ".join(
        f"{c} {min(r['strike'] for r in rounds if r['coin'] == c):.2f}"
        f"..{max(r['strike'] for r in rounds if r['coin'] == c):.2f}"
        for c in coins))
    print(f"  winners: {sum(1 for r in rounds if r['winner'] == 'Up')} Up / "
          f"{sum(1 for r in rounds if r['winner'] == 'Down')} Down")
    cl = chain_series(t_from, t_to)
    if len(cl) < 10:
        print("not enough Chainlink prints"); return 1

    taus = [5, 10, 15, 20, 30, 45, 60]
    base = []
    for r in rounds:
        end = r["end_ms"] / 1000.0
        start = r["start_ms"] / 1000.0
        bs = sorted((t / 1000.0, s) for t, _tau, s, _u, _d in r["q"])
        cl_open = twap(cl, start, start + LOOKBACK)
        cl_close = twap(cl, end - LOOKBACK, end)
        if not (cl_open and cl_close and math.isfinite(cl_open)
                and math.isfinite(cl_close)):
            continue
        base.append({"coin": r["coin"], "win": r["winner"], "q": r["q"],
                     "end": end, "bs": bs, "cl_open": cl_open,
                     "cl_close": cl_close, "strike": r["strike"]})

    print(f"\n=== accuracy vs the venue's settled winner ({len(base)} rounds) ===")
    print("  perfect = CL tail-60 at close vs CL tail-60 at open  (the ceiling)")
    print("  chain   = CL tail-60 AT TAU   vs CL tail-60 at open  (what we could know)")
    print("  binance = Binance tail-60 @tau vs the stored strike  (the incumbent)")
    print()
    print(f"  {'tau':>4} {'n':>5} {'perf':>7} | {'ALL rounds':^29} | "
          f"{'CONTESTED rounds':^29}")
    print(f"  {'':>4} {'':>5} {'':>7} | {'chain':>9} {'bin':>8} {'delta':>9} | "
          f"{'chain':>9} {'bin':>8} {'delta':>9}")
    print("  " + "-" * 78)

    def acc(rs, key):
        return sum(1 for x in rs if x[key] == x["win"]) / len(rs)

    kept = []
    for tau in taus:
        rows = []
        for r in base:
            cl_at = twap(cl, r["end"] - LOOKBACK, r["end"] - tau)
            bn_at = twap(r["bs"], r["end"] - LOOKBACK, r["end"] - tau)
            if not (cl_at and bn_at and math.isfinite(cl_at)
                    and math.isfinite(bn_at)):
                continue
            near = min(r["q"], key=lambda x: abs(x[1] - tau))
            ua, da = near[3], near[4]
            cont = (ua is not None and da is not None
                    and 0.001 < ua < 0.999 and 0.001 < da < 0.999)
            rows.append({"coin": r["coin"], "win": r["win"], "cont": cont,
                         "perfect": "Up" if r["cl_close"] >= r["cl_open"] else "Down",
                         "chain": "Up" if cl_at >= r["cl_open"] else "Down",
                         "binance": "Up" if bn_at >= r["strike"] else "Down"})
        cn = [x for x in rows if x["cont"]]
        if not cn:
            continue
        kept.append((tau, rows, cn))
        print(f"  {tau:>4} {len(rows):>5} {acc(rows, 'perfect'):>7.1%} | "
              f"{acc(rows, 'chain'):>9.1%} {acc(rows, 'binance'):>8.1%} "
              f"{acc(rows, 'chain') - acc(rows, 'binance'):>+9.1%} | "
              f"{acc(cn, 'chain'):>9.1%} {acc(cn, 'binance'):>8.1%} "
              f"{acc(cn, 'chain') - acc(cn, 'binance'):>+9.1%}")

    if not kept:
        print("nothing scored"); return 1

    tau_d, _rows_d, _cn_d = kept[len(kept) // 2]
    print(f"\n=== close side only, against the stored strike (tau {tau_d:.0f}s) ===")
    print("  A forecast replaces the CLOSE only. If the venue truly settles on a")
    print("  Chainlink 60 s mean, the CL-close row is that mean itself -- not a")
    print("  forecast -- so it should be near 100%.\n")
    var: dict[str, list[bool]] = {}
    for r in base:
        near = min(r["q"], key=lambda x: abs(x[1] - tau_d))
        ua, da = near[3], near[4]
        if not (ua is not None and da is not None and 0.001 < ua < 0.999
                and 0.001 < da < 0.999):
            continue
        s = r["strike"]
        cl_last = None
        for t, p in cl:
            if t <= r["end"] - tau_d:
                cl_last = p
            else:
                break
        cand = {
            "binance tail-60 @tau": twap(r["bs"], r["end"] - LOOKBACK,
                                         r["end"] - tau_d),
            "binance tail-60 @close": twap(r["bs"], r["end"] - LOOKBACK, r["end"]),
            "chainlink LAST print @tau": cl_last,
            "chainlink tail-60 @close": twap(cl, r["end"] - LOOKBACK, r["end"]),
        }
        for k, v in cand.items():
            if v and math.isfinite(v):
                var.setdefault(k, []).append(
                    ("Up" if v >= s else "Down") == r["win"])
    for k, v in var.items():
        print(f"  {k:<28} {sum(v):>5}/{len(v):<5} {sum(v) / len(v):>6.1%}")

    tau, rows, cn = kept[len(kept) // 2]
    print(f"\n=== per coin at tau {tau:.0f}s, contested rounds (n={len(cn)}) ===")
    print(f"  {'coin':>5} {'n':>4} {'chainlink':>11} {'binance':>9} {'delta':>8}")
    for coin in sorted({x["coin"] for x in cn}):
        cc = [x for x in cn if x["coin"] == coin]
        if len(cc) < 15:
            continue
        a1 = sum(1 for x in cc if x["chain"] == x["win"]) / len(cc)
        a2 = sum(1 for x in cc if x["binance"] == x["win"]) / len(cc)
        print(f"  {coin:>5} {len(cc):>4} {a1:>11.1%} {a2:>9.1%} {a1 - a2:>+8.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
