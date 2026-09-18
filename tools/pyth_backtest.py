"""Does the PAID Pyth feed beat Binance for predicting the venue's settle?

This is the last untested accuracy lever, and it is worth testing properly
because it is the first source that is simultaneously (a) the right KIND of
thing -- a multi-venue oracle aggregate, like the Chainlink series the venue
settles on -- and (b) high resolution, unlike Chainlink's free on-chain feed at
33 s which measured 3.3 pp WORSE than Binance on contested rounds.

The venue settles on Chainlink `btc-usd-twap-60s-streams`. We forecast from
Binance's single-venue tape. Pyth's aggregate may sit closer to Chainlink's
aggregate than Binance does. Pyth Core REST measured: publish gap median 1 s,
staleness median 0.8 s, and a working historical endpoint at second granularity.

WHAT IS SCORED, on the venue's own settled winner, per coin and pooled:
    binance   Binance tail-60 @tau vs the stored strike   (the incumbent)
    pyth      Pyth    tail-60 @tau vs the stored strike   (the challenger)
A source only earns its place if `pyth` beats `binance` on the CONTESTED rounds
-- the ones with a two-sided book, which are the only ones you can buy.

History is cached to a json so re-runs and extra taus cost nothing.

Usage:
    python3 tools/pyth_backtest.py rounds.json --limit 60      # pilot
    python3 tools/pyth_backtest.py rounds.json                 # full
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://hermes.pyth.network"
LOOKBACK = 60.0
CACHE = "/tmp/pyth_hist.json"
UA = {"User-Agent": "Mozilla/5.0"}


def load_key() -> str | None:
    k = os.environ.get("PYTH")
    if k and k.strip():
        return k.strip()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(here, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            m = re.match(r'\s*PYTH\s*=\s*["\']?([^"\'\s#]+)', line)
            if m:
                return m.group(1)
    return None


KEY = load_key()


def get(url: str, timeout: float = 25.0, tries: int = 5):
    for a in range(tries):
        req = urllib.request.Request(url, headers={**UA,
                                                   "Authorization": f"Bearer {KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as h:
                return json.load(h)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and a + 1 < tries:
                time.sleep(0.5 * (a + 1))
                continue
            return None
        except Exception:                                        # noqa: BLE001
            if a + 1 < tries:
                time.sleep(0.4 * (a + 1))
                continue
            return None
    return None


def feed_map() -> dict[str, str]:
    """coin -> Pyth feed id, from the catalogue (open endpoint, no key)."""
    want = {"btc", "eth", "bnb", "sol", "xrp", "doge"}
    out: dict[str, str] = {}
    for asset in ("crypto",):
        d = get(f"{BASE}/v2/price_feeds?asset_type={asset}", tries=3)
        if not isinstance(d, list):
            continue
        for f in d:
            # symbol looks like "Crypto.BTC/USD" -> strip both the class prefix
            # and the quote leg, or nothing ever matches
            sym = (f.get("attributes") or {}).get("symbol", "")
            if "/" not in sym or not f.get("id"):
                continue
            base = sym.split("/")[0].split(".")[-1].lower()
            if base in want:
                out.setdefault(base, "0x" + f["id"].lower())
    return out


def hist(feed: str, ts: int) -> tuple[int, float] | None:
    d = get(f"{BASE}/v2/updates/price/{ts}?ids[]={feed}")
    if not isinstance(d, dict) or not d.get("parsed"):
        return None
    p = d["parsed"][0]["price"]
    return int(p["publish_time"]), int(p["price"]) * 10 ** p["expo"]


def twap(series: list[tuple[int, float]], a: float, b: float) -> float:
    if b <= a or not series:
        return float("nan")
    tot = 0.0
    for i, (t, p) in enumerate(series):
        lo = max(a, t)
        hi = min(b, series[i + 1][0] if i + 1 < len(series) else b)
        if hi > lo:
            tot += p * (hi - lo)
    if series[0][0] > a:
        tot += series[0][1] * (min(series[0][0], b) - a)
    return tot / (b - a)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rounds")
    ap.add_argument("--tau", type=float, default=10.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    if not KEY:
        print("no PYTH key"); return 1
    rounds = json.load(open(a.rounds))
    fm = feed_map()
    print(f"feed ids: { {k: v[:12] + '...' for k, v in fm.items()} }")
    missing = {r["coin"] for r in rounds} - set(fm)
    if missing:
        print(f"  WARNING no feed id for {missing}; those rounds skipped")
    rounds = [r for r in rounds if r["coin"] in fm]
    if a.limit:
        rounds = rounds[:a.limit]
    print(f"{len(rounds)} rounds, tau {a.tau:.0f}s, step {a.step}s")

    cache: dict = {}
    if os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE))
        except Exception:                                        # noqa: BLE001
            cache = {}

    # build the work list: (feed, ts) for the tail-60 window of every round
    jobs = []
    for r in rounds:
        end = r["end_ms"] / 1000.0
        feed = fm[r["coin"]]
        t0 = int(end - LOOKBACK - 2)
        t1 = int(end - a.tau)
        for ts in range(t0, t1 + 1, a.step):
            if f"{feed}:{ts}" not in cache:
                jobs.append((feed, ts))
    print(f"history points to fetch: {len(jobs)} ({len(cache)} cached)")

    t_start = time.time()
    done = 0

    def work(job):
        feed, ts = job
        res = hist(feed, ts)
        return f"{feed}:{ts}", (list(res) if res else None)

    if jobs:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for k, v in ex.map(work, jobs):
                cache[k] = v
                done += 1
                if done % 2000 == 0:
                    el = time.time() - t_start
                    print(f"  {done}/{len(jobs)}  {done / el:.1f}/s  "
                          f"eta {(len(jobs) - done) / max(done / el, 1e-9) / 60:.1f} min",
                          flush=True)
        json.dump(cache, open(CACHE, "w"))
    print(f"fetch done in {time.time() - t_start:.0f}s\n")

    rows = []
    for r in rounds:
        end = r["end_ms"] / 1000.0
        feed = fm[r["coin"]]
        t0, t1 = int(end - LOOKBACK - 2), int(end - a.tau)
        ser = [(cache[f"{feed}:{ts}"][0], cache[f"{feed}:{ts}"][1])
               for ts in range(t0, t1 + 1, a.step)
               if cache.get(f"{feed}:{ts}")]
        ser.sort()
        if len(ser) < 5:
            continue
        py = twap(ser, end - LOOKBACK, end - a.tau)
        bs = sorted((t / 1000.0, s) for t, _tau, s, _u, _d in r["q"])
        bn = twap(bs, end - LOOKBACK, end - a.tau)
        if not (py == py and bn == bn):
            continue
        near = min(r["q"], key=lambda x: abs(x[1] - a.tau))
        ua, da = near[3], near[4]
        cont = (ua is not None and da is not None
                and 0.001 < ua < 0.999 and 0.001 < da < 0.999)
        s = r["strike"]
        rows.append({
            "coin": r["coin"], "win": r["winner"], "cont": cont,
            "py": "Up" if py >= s else "Down",
            "bn": "Up" if bn >= s else "Down",
            "py_px": py, "bn_px": bn, "strike": s,
            "basis_bps": (py - bn) / bn * 1e4 if bn else 0.0,
        })

    if not rows:
        print("nothing scored"); return 1

    def acc(rs, k):
        return sum(1 for r in rs if r[k] == r["win"]) / len(rs)

    cn = [r for r in rows if r["cont"]]
    print(f"=== accuracy vs the venue's settled winner (tau {a.tau:.0f}s) ===")
    print(f"  scored {len(rows)} rounds, {len(cn)} contested\n")
    print(f"  {'scope':<14} {'n':>5} {'binance':>9} {'pyth':>9} {'delta':>9}")
    print("  " + "-" * 48)
    for label, rs in (("ALL rounds", rows), ("CONTESTED", cn)):
        if not rs:
            continue
        b, p = acc(rs, "bn"), acc(rs, "py")
        print(f"  {label:<14} {len(rs):>5} {b:>9.1%} {p:>9.1%} {p - b:>+9.1%}")
    for coin in sorted({r["coin"] for r in cn}):
        cc = [r for r in cn if r["coin"] == coin]
        if len(cc) < 12:
            continue
        b, p = acc(cc, "bn"), acc(cc, "py")
        print(f"  {coin:<14} {len(cc):>5} {b:>9.1%} {p:>9.1%} {p - b:>+9.1%}")

    bs = sorted(abs(r["basis_bps"]) for r in rows)
    print(f"\n=== how different are they at all? ===")
    print(f"  |Pyth - Binance| bps: median {bs[len(bs) // 2]:.2f}  "
          f"p90 {bs[int(len(bs) * 0.9)]:.2f}  max {bs[-1]:.2f}")
    print("  (if this were ~0 the two sources carry identical information and"
          "\n   no accuracy gain is possible; a large value means they diverge"
          "\n   and one of them must be closer to the Chainlink settle)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
