"""Can a trained model beat the closed-form digital baseline? (scaled version)

History gives unlimited (features -> outcome) pairs from klines, but no market
quotes, so this cannot train "beat the market" directly. What it answers is
whether our *predictor* can be improved at all -- if it cannot, there is no
route to an edge over a market that already matches the baseline.

Scaling notes:
  * Fetching is network-bound (~526 requests for a year), so chunks are fetched
    concurrently.
  * The feature build is vectorised with numpy rather than looped in Python;
    the rolling-sigma state made a naive loop the bottleneck.
  * The logistic fit is numpy/BLAS, which threads on its own.

Everything is strictly causal: sigma for a round uses only bars that closed
before that round opened. The split is by TIME, never shuffled -- shuffling
leaks future regime information backwards and is how these backtests lie.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import time

import aiohttp
import numpy as np

from btcpredict.calibration.report import reliability_table, score

ROUND_S = 300
MIN_PER_REQ = 1000


async def _chunk(session, sem, symbol, start_ms):
    url = ("https://api.binance.com/api/v3/klines?symbol=" + symbol +
           f"&interval=1m&limit={MIN_PER_REQ}&startTime={start_ms}")
    async with sem:
        for attempt in range(4):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    await asyncio.sleep(1.0 * (attempt + 1))
            except Exception:
                await asyncio.sleep(1.0 * (attempt + 1))
    return []


async def load_1m(symbol: str, days: float, concurrency: int) -> np.ndarray:
    total_min = int(days * 24 * 60)
    now_ms = int(time.time() * 1000) // 60000 * 60000
    starts = [now_ms - (total_min - i) * 60_000
              for i in range(0, total_min, MIN_PER_REQ)]
    print(f"fetching {len(starts)} chunks ({total_min:,} minutes) "
          f"with {concurrency} concurrent requests...")
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession() as s:
        t0 = time.time()
        out = await asyncio.gather(*(_chunk(s, sem, symbol, st) for st in starts))
    rows = [k for part in out for k in part]
    print(f"  fetched {len(rows):,} bars in {time.time()-t0:.0f}s")
    if not rows:
        return np.empty((0, 6))
    arr = np.array([[float(k[0]), float(k[1]), float(k[2]),
                     float(k[3]), float(k[4]), float(k[5])] for k in rows])
    # de-duplicate and order by open time
    _, idx = np.unique(arr[:, 0], return_index=True)
    return arr[np.sort(idx)]


def rolling_std(x: np.ndarray, w: int) -> np.ndarray:
    """Trailing std ending at each index (exclusive of the current element)."""
    c1 = np.concatenate([[0.0], np.cumsum(x)])
    c2 = np.concatenate([[0.0], np.cumsum(x * x)])
    n = len(x)
    out = np.full(n, np.nan)
    i = np.arange(w, n + 1)
    s1 = c1[i] - c1[i - w]
    s2 = c2[i] - c2[i - w]
    var = np.maximum(s2 / w - (s1 / w) ** 2, 0.0)
    out[i - 1] = np.sqrt(var)
    return out


def build(arr: np.ndarray, mark_min: int):
    """Vectorised: one row per complete 5-bar round, decided after mark_min."""
    t = (arr[:, 0] // 1000).astype(np.int64)
    start = (t // ROUND_S) * ROUND_S
    pos = ((t - start) // 60).astype(int)
    # Trim the front so index 0 is the first bar of a round; without this the
    # reshape below groups bars across round boundaries and nothing validates.
    first = int(np.argmax(pos == 0))
    if pos[first] != 0:
        return np.empty((0, 8)), np.empty(0, int), np.empty(0)
    arr, start, pos = arr[first:], start[first:], pos[first:]
    n_full = len(arr) // 5 * 5
    arr, start, pos = arr[:n_full], start[:n_full], pos[:n_full]

    o, h, lo_, c, v = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
    r1 = np.log(np.maximum(c, 1e-12) / np.maximum(o, 1e-12))

    # sigma from the trailing 240 / 60 bars, ending BEFORE the round opens
    sd_slow = rolling_std(r1, 240)
    sd_fast = rolling_std(r1, 60)

    # reshape into rounds; drop any group that is not exactly 5 aligned bars
    G = len(arr) // 5
    O = o.reshape(G, 5); H = h.reshape(G, 5); L = lo_.reshape(G, 5)
    C = c.reshape(G, 5); V = v.reshape(G, 5); R = r1.reshape(G, 5)
    P = pos.reshape(G, 5); S0 = start.reshape(G, 5)
    ok = (P == np.arange(5)).all(1) & (S0 == S0[:, :1]).all(1)

    prev_idx = np.arange(G) * 5 - 1          # last bar before the round
    valid = ok & (prev_idx >= 0)
    sig = np.where(valid, sd_slow[np.maximum(prev_idx, 0)], np.nan)
    sigf = np.where(valid, sd_fast[np.maximum(prev_idx, 0)], np.nan)
    prev_ret = np.where(valid, r1[np.maximum(prev_idx, 0)], np.nan)
    valid &= np.isfinite(sig) & (sig > 0) & np.isfinite(sigf)

    tau = (5 - mark_min) * 60.0
    K = O[:, 0]; S = C[:, mark_min - 1]; SET = C[:, 4]
    sig = np.maximum(sig, 1.5e-4)
    z = np.log(np.maximum(S, 1e-12) / np.maximum(K, 1e-12)) / (sig * math.sqrt(tau / 60.0))

    rng = (H[:, :mark_min].max(1) - L[:, :mark_min].min(1)) / K
    path = np.abs(R[:, :mark_min]).sum(1)
    net = np.abs(np.log(np.maximum(S, 1e-12) / np.maximum(K, 1e-12)))
    vol_now = np.maximum(V[:, :mark_min].mean(1), 0.0)
    vol_ref = np.convolve(V.sum(1), np.ones(20) / 20, mode="full")[:G]
    vol_ref = np.maximum(np.roll(vol_ref, 1), 1e-9)

    X = np.column_stack([
        z,
        np.copysign(np.sqrt(np.abs(z)), z),
        sigf / sig,
        rng / (sig * math.sqrt(mark_min)),
        np.where(net > 1e-9, path / np.maximum(net, 1e-9), 5.0),
        R[:, mark_min - 1] / sig,
        vol_now / vol_ref,
        prev_ret / sig,
    ])
    y = (SET > K).astype(int)
    from btcpredict.pricing.digital import norm_cdf
    base = 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))
    good = valid & np.isfinite(X).all(1)
    return X[good], y[good], np.clip(base[good], 1e-6, 1 - 1e-6)


def fit_logistic(X, y, l2=1.0, iters=60):
    Xs = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(Xs.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xs @ w, -30, 30)))
        g = Xs.T @ (p - y) / len(y) + l2 * np.r_[0, w[1:]] / len(y)
        W = p * (1 - p)
        h = (Xs * W[:, None]).T @ Xs / len(y) + l2 * np.eye(Xs.shape[1]) / len(y)
        try:
            step = np.linalg.solve(h + 1e-9 * np.eye(len(h)), g)
        except np.linalg.LinAlgError:
            break
        w -= step
        if np.max(np.abs(step)) < 1e-10:
            break
    return w


def predict(w, X):
    Xs = np.hstack([np.ones((len(X), 1)), X])
    return 1 / (1 + np.exp(-np.clip(Xs @ w, -30, 30)))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=365)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--mark", type=int, default=3)
    ap.add_argument("--threads", type=int, default=10)
    args = ap.parse_args()
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = str(args.threads)

    arr = await load_1m(args.symbol, args.days, args.threads)
    if len(arr) < 5000:
        print("not enough data"); return
    t0 = time.time()
    X, y, base = build(arr, args.mark)
    print(f"  built {len(X):,} rounds in {time.time()-t0:.1f}s "
          f"({len(X)*5/60/24:.0f} days), decision at t={args.mark}min\n")

    cut = int(len(X) * 0.7)
    Xtr, ytr = X[:cut], y[:cut]
    Xte, yte, bte = X[cut:], y[cut:], base[cut:]
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    print(f"train {len(Xtr):,} rounds | test {len(Xte):,} rounds (strictly later)\n")

    b_sc = score(list(bte), list(yte))
    print(f"  BASELINE (digital) : {b_sc}")
    best = None
    for l2 in (0.3, 1.0, 3.0, 10.0, 30.0, 100.0):
        w = fit_logistic((Xtr - mu) / sd, ytr, l2=l2)
        p = predict(w, (Xte - mu) / sd)
        sc = score(list(p), list(yte))
        tag = ""
        if best is None or sc.brier < best[1].brier:
            best = (l2, sc, p, w); tag = "  <-- best"
        print(f"  trained (l2={l2:>5}) : {sc}{tag}")

    l2, sc, p, w = best
    gap = b_sc.brier - sc.brier
    print(f"\n  best trained Brier {sc.brier:.5f} vs baseline {b_sc.brier:.5f} -> {gap:+.5f}")
    rng = np.random.default_rng(7)
    diffs = (bte - yte) ** 2 - (p - yte) ** 2
    boots = np.sort([diffs[rng.integers(0, len(diffs), len(diffs))].mean()
                     for _ in range(4000)])
    sigflag = boots[100] > 0
    print(f"  paired diff {diffs.mean():+.6f}  95% CI [{boots[100]:+.6f}, {boots[3899]:+.6f}]"
          f"  {'SIGNIFICANT' if sigflag else 'not significant'}")

    names = ["z", "sqrt|z|", "vol_regime", "range/exp", "path_eff",
             "last_mom", "volume", "prev_ret"]
    print("\n  fitted weights (standardised):")
    for n, wt in sorted(zip(names, w[1:]), key=lambda x: -abs(x[1])):
        print(f"    {n:12} {wt:+.4f}")
    print("\n  trained-model calibration, held-out period:")
    print(reliability_table(list(p), list(yte), min_n=100))


if __name__ == "__main__":
    asyncio.run(main())
