"""Pyth vs Binance on every settled round -- in ~400 requests, not 57,784.

WHY THIS IS SUDDENLY FEASIBLE. Pyth's historical route 429s with
`Retry-After: 22`, so a naive 60-samples-per-round backtest needs ~57,784
requests and days. Two facts collapse that to nothing:

  1. THE SIX COINS SHARE ROUND BOUNDARIES. 932 settled rounds occupy only
     **204 distinct end timestamps** (measured: 104 timestamps carry all 6
     coins, 79 carry 3). A round's open IS the previous round's close, so
     210 distinct timestamps cover every round's close AND its open, for
     every coin.
  2. ONE REQUEST RETURNS MANY FEEDS. `?ids[]=A&ids[]=B&...` returns a batch,
     so a single request at one timestamp yields all six coins.

That is 210-410 requests total for the entire dataset. Even honouring
`Retry-After: 22` on every call that is under 3 hours worst case, and the
observed post-burst throughput is far better.

WHAT IS SCORED. Head-to-head on the venue's own settled winner, both sources
measured against the SAME stored strike so neither gets a free denominator:

    binance @tau / @close   -- the incumbent, from the recorded tape
    pyth    @tau / @close   -- the challenger, instantaneous Pyth aggregate

and the decisive cut is CONTESTED rounds (a two-sided book), because those are
the only ones you can actually buy.

Usage:
    python3 tools/pyth_fast_backtest.py /tmp/rounds_local.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request

BASE = "https://benchmarks.pyth.network/v1/updates/price"
CACHE = "/tmp/pyth_pts.json"
COINS = ("btc", "eth", "bnb", "sol", "xrp", "doge")
UA = {"User-Agent": "Mozilla/5.0"}

# Verified against a live /v1/price_feeds catalogue fetch. Hardcoded because
# that route is rate-limited too, and a 429 there killed an earlier run before
# it fetched anything: the ids never change, so there is no reason to depend on
# a request we might not be allowed to make.
KNOWN_IDS = {
    "btc": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "eth": "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "bnb": "0x2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f",
    "sol": "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "xrp": "0xec5d399846a9209f3fe5881d70aae9268c94339ffb82b3ff00286fca9c1c4b8d",
    "doge": "0xdcef50dd0a4cd2dcc17e45df1676dcb336a11a61c69df7a0299b0150c672d25c",
}


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


def feed_ids() -> dict[str, str]:
    """KNOWN_IDS is authoritative. The benchmarks catalogue serves a DIFFERENT
    id set for btc/eth/sol (measured: it returned 0x6666661e.. for btc, which
    403s on the price route), so the catalogue may only fill gaps, never
    overwrite a verified id."""
    out = dict(KNOWN_IDS)
    try:
        u = "https://benchmarks.pyth.network/v1/price_feeds"
        req = urllib.request.Request(u, headers={**UA,
                                                 "Authorization": f"Bearer {KEY}"})
        with urllib.request.urlopen(req, timeout=40) as h:
            cat = json.load(h)
        want = set(COINS)
        for f in cat:
            sym = (f.get("attributes") or {}).get("symbol", "")
            if "/" not in sym or not f.get("id"):
                continue
            base = sym.split("/")[0].split(".")[-1].lower()
            if base in want and base not in out:      # gap-fill only
                out[base] = "0x" + f["id"].lower()
    except Exception:                                            # noqa: BLE001
        pass                       # the hardcoded set is already correct
    return out


def fetch(ts: int, ids: list[str], tries: int = 10):
    """One batched request. Honours Retry-After instead of hammering.
    A burst trips a ~22 s cooldown for EVERY subsequent call, so on 429 we
    wait the advertised time out rather than retrying fast."""
    q = "&".join(f"ids[]={i}" for i in ids)
    url = f"{BASE}/{ts}?{q}"
    last = "failed"
    for _ in range(tries):
        req = urllib.request.Request(
            url, headers={**UA, "Authorization": f"Bearer {KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as h:
                d = json.load(h)
            out = {}
            for item in d.get("parsed", []):
                fid = item.get("id", "")
                fid = fid if fid.startswith("0x") else "0x" + fid
                p = item["price"]
                out[fid.lower()] = [int(p["publish_time"]),
                                    int(p["price"]) * 10 ** p["expo"]]
            return out, None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("retry-after") or 5)
                time.sleep(min(wait, 30) + 1.0)
                last = "429"
                continue
            return None, f"HTTP {e.code}"
        except Exception as e:                                   # noqa: BLE001
            time.sleep(1.0 * (_ + 1))
            last = type(e).__name__
    return None, last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rounds")
    ap.add_argument("--tau", type=float, default=10.0)
    ap.add_argument("--budget", type=int, default=1500,
                    help="seconds to spend fetching before scoring what we have")
    ap.add_argument("--with-opens", action="store_true",
                    help="also fetch end-tau; doubles the requests. Off by "
                         "default because the stored strike is the shared "
                         "denominator, so the close alone is enough")
    ap.add_argument("--sleep", type=float, default=1.5,
                    help="seconds between requests. The route 429s on BURSTS, "
                         "not on volume; a steady ~1.5 s pace measured clean")
    a = ap.parse_args()

    if not KEY:
        print("no PYTH key"); return 1
    fm = feed_ids()
    print(f"feed ids: { {k: v[:10] + '..' for k, v in fm.items()} }")
    if len(fm) < 6:
        print("missing feeds, aborting"); return 1
    ids = [fm[c] for c in COINS]
    id2coin = {v: k for k, v in fm.items()}

    rounds = json.load(open(a.rounds))
    want: set[int] = set()
    for r in rounds:
        e = int(r["end_ms"] // 1000)
        want.add(e)                       # the close -- the decisive timestamp
        if a.with_opens:
            want.add(int(e - a.tau))
    print(f"{len(rounds)} rounds -> {len(want)} distinct timestamps to fetch "
          f"(x{len(ids)} feeds per request)")

    cache: dict[str, dict] = {}
    if os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE))
        except Exception:                                        # noqa: BLE001
            cache = {}
    todo = sorted(t for t in want if str(t) not in cache)
    print(f"already cached {len(want) - len(todo)}, to fetch {len(todo)}\n")

    t0 = time.time()
    ok = fail = 0
    for n, ts in enumerate(todo, 1):
        if time.time() - t0 > a.budget:
            print(f"  budget reached after {n - 1} fetches"); break
        res, err = fetch(ts, ids)
        if res is None:
            fail += 1
            if fail <= 5:
                print(f"  ts {ts} failed: {err}", flush=True)
            if fail == 1:
                print("  (403 = feed id not served here; 429 = pacing. "
                      "Stopping early if the first few all fail.)", flush=True)
            if fail >= 12 and ok == 0:
                print("  every request failed -- wrong ids or blocked; aborting")
                break
            continue
        cache[str(ts)] = res
        ok += 1
        if n % 25 == 0:
            el = time.time() - t0
            print(f"  {n}/{len(todo)}  {ok} ok  {el:.0f}s  "
                  f"{ok / el:.2f} req/s  eta {(len(todo) - n) / max(ok / el, 1e-9) / 60:.1f} min",
                  flush=True)
        if ok % 20 == 0:
            json.dump(cache, open(CACHE, "w"))
        time.sleep(a.sleep)
    json.dump(cache, open(CACHE, "w"))
    el = time.time() - t0
    print(f"\nfetched {ok} ok / {fail} failed in {el:.0f}s ({ok / max(el, 1e-9):.2f} req/s)")

    # ---- score ----
    rows = []
    for r in rounds:
        e = int(r["end_ms"] // 1000)
        coin = r["coin"]
        fid = fm.get(coin)
        if not fid:
            continue
        at = cache.get(str(int(e - a.tau)), {})
        cl = cache.get(str(e), {})
        pt = at.get(fid)
        pc = cl.get(fid)
        bs = sorted((t / 1000.0, s) for t, _tau, s, _u, _d in r["q"])
        # binance instantaneous nearest to tau and to close
        def bn(ts_off):
            if not bs:
                return None
            tgt = e - ts_off
            return min(bs, key=lambda x: abs(x[0] - tgt))[1]
        b_at, b_cl = bn(a.tau), bn(0)
        near = min(r["q"], key=lambda x: abs(x[1] - a.tau))
        ua, da = near[3], near[4]
        cont = (ua is not None and da is not None
                and 0.001 < ua < 0.999 and 0.001 < da < 0.999)
        s = r["strike"]
        rows.append({
            "coin": coin, "win": r["winner"], "cont": cont,
            "pyth_at": ("Up" if pt[1] >= s else "Down") if pt else None,
            "pyth_cl": ("Up" if pc[1] >= s else "Down") if pc else None,
            "bin_at": ("Up" if b_at >= s else "Down") if b_at else None,
            "bin_cl": ("Up" if b_cl >= s else "Down") if b_cl else None,
        })

    def acc(rs, k):
        v = [r for r in rs if r[k]]
        return (sum(1 for r in v if r[k] == r["win"]) / len(v), len(v)) if v else (0.0, 0)

    print(f"\n=== accuracy vs the venue's settled winner "
          f"(tau {a.tau:.0f}s, same strike for both) ===")
    print(f"  {'scope':<12} {'method':<18} {'n':>5} {'acc':>8}")
    for label, rs in (("ALL", rows), ("CONTESTED", [x for x in rows if x["cont"]])):
        for k in ("bin_at", "bin_cl", "pyth_at", "pyth_cl"):
            ac, n = acc(rs, k)
            if n:
                print(f"  {label:<12} {k:<18} {n:>5} {ac:>8.1%}")
        print()
    cn = [x for x in rows if x["cont"]]
    print(f"=== contested, per coin (n={len(cn)}) ===")
    print(f"  {'coin':>5} {'n':>4} {'bin@close':>10} {'pyth@close':>11} {'delta':>8}")
    for coin in COINS:
        cc = [x for x in cn if x["coin"] == coin]
        if len(cc) < 12:
            continue
        ab, nb = acc(cc, "bin_cl")
        ap, np_ = acc(cc, "pyth_cl")
        if nb and np_:
            print(f"  {coin:>5} {len(cc):>4} {ab:>10.1%} {ap:>11.1%} {ap - ab:>+8.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
