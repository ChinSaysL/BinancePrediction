"""The Archetapp gist strategy, rebuilt and tested -- because the gist ships no code.

The spec (from the guide itself):
  * 5-min windows on a 300s grid
  * enter near the close, when direction is nearly locked
  * composite score from window delta (DOMINANT, weight up to 7) plus momentum,
    acceleration, EMA9/21 and RSI14; confidence = min(|score| / 7, 1)
  * spike detection: |score| jumping >= 1.5 between polls = fire immediately
  * "never skip a trade" -- if nothing fired, take the best signal by T-5s

Two pricing models, because that is the whole argument:
  A) FIXED 0.50  -- what the shared simulator screenshot assumes ("SHARE $0.5").
     With this, the result is arithmetic: WR * 2 - 1.
  B) DELTA-PRICED -- the gist's own stated model ("the larger the move from
     window open, the more the winning token costs"). At the gist's own
     "0.10% = nearly certain" threshold the token costs ~0.99, not 0.50.

Also charges OUR venue's fee: 200bps * min(1, (1-p)/p) of stake.

Entry timing: with 1m bars we can only use the last COMPLETED bar (T-60), which
is strictly less information than the gist's T-10. So this handicaps the
strategy -- favourably for the strategy's own claims.
"""
from __future__ import annotations

import argparse
import os
import time
import urllib.request

import numpy as np
import pandas as pd

API = "https://api.binance.com/api/v3/klines"
UA = {"User-Agent": "research"}


def fetch(symbol: str, interval: str, days: int, cache: str) -> pd.DataFrame:
    os.makedirs(cache, exist_ok=True)
    path = f"{cache}/{symbol}_{interval}_{days}d.csv"
    if os.path.exists(path):
        return pd.read_csv(path)
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    rows, t = [], start
    while t < end:
        url = f"{API}?symbol={symbol}&interval={interval}&startTime={t}&limit=1000"
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            import json
            batch = json.loads(r.read().decode())
        if not batch:
            break
        rows += batch
        t = batch[-1][0] + 1
        time.sleep(0.12)
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "tbb", "tbq", "ignore"])
    df = df[["open_time", "close_time", "open", "high", "low", "close",
             "volume", "trades"]].astype(float)
    df["ts"] = (df["open_time"] / 1000).astype(int)
    df.to_csv(path, index=False)
    print(f"  fetched {symbol} {interval}: {len(df):,} bars")
    return df


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(close, prepend=close[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = pd.Series(up).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    ad = pd.Series(dn).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    rs = au / np.maximum(ad, 1e-12)
    return 100 - 100 / (1 + rs)


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def score_row(delta_pct: float, r1: float, r2: float, ema9: float, ema21: float,
              r: float) -> float:
    """The gist's composite score. Window delta dominates by design."""
    a = abs(delta_pct)
    wdelta = 7.0 if a > 0.10 else 5.0 if a > 0.02 else 3.0 if a > 0.005 else \
        1.0 if a > 0.001 else 0.0
    s = np.sign(delta_pct) * wdelta
    s += 2.0 * np.sign(r1)                       # micro momentum, last bar
    s += 1.5 * np.sign(r1 - r2)                  # acceleration
    s += 1.0 * (1 if ema9 > ema21 else -1)       # EMA 9/21
    if r > 75:
        s -= 2.0                                  # overbought
    elif r < 25:
        s += 2.0                                  # oversold
    return float(s)


def price_fixed(delta_pct: float) -> float:
    """Model A: the screenshot's assumption."""
    return 0.50


def price_delta(delta_pct: float) -> float:
    """Model B: gist's 'bigger move -> pricier token', 0.10% => ~0.99."""
    return float(min(0.99, 0.50 + 0.50 * min(1.0, abs(delta_pct) / 0.10)))


def fee(px: float) -> float:
    """Our venue: 200bps * min(1, (1-p)/p) of stake. Vectorised."""
    p = np.asarray(px, dtype=float)
    return 0.02 * np.minimum(1.0, (1.0 - p) / np.maximum(p, 1e-9))


def run(symbol: str, days: int, min_conf: float, grid: int, cache: str):
    df = fetch(symbol, "1m", days, cache)
    o, c = df["open"].to_numpy(), df["close"].to_numpy()
    ts = df["ts"].to_numpy()
    e9, e21, rr = ema(c, 9), ema(c, 21), rsi(c)
    # bin each bar into a 300s window on the epoch grid
    wid = ts // grid
    out = []
    for w, idx in pd.Series(range(len(ts))).groupby(wid):
        idx = idx.to_numpy()
        if len(idx) < grid // 60:            # incomplete window at the edges
            continue
        entry_i = idx[-2]                     # last COMPLETED bar (T-60)
        settle_i = idx[-1]                    # window close
        wopen = o[idx[0]]
        px_now = c[entry_i]
        delta_pct = (px_now - wopen) / wopen * 100.0
        r1 = c[entry_i] / c[entry_i - 1] - 1 if entry_i > 0 else 0.0
        r2 = c[entry_i - 1] / c[entry_i - 2] - 1 if entry_i > 1 else 0.0
        s = score_row(delta_pct, r1, r2, e9[entry_i], e21[entry_i], rr[entry_i])
        conf = min(abs(s) / 7.0, 1.0)
        if conf < min_conf:
            continue
        up = c[settle_i] > wopen
        win = (s > 0) == up
        p_fix, p_del = price_fixed(delta_pct), price_delta(delta_pct)
        out.append({
            "window": int(w), "delta_pct": delta_pct, "score": s, "conf": conf,
            "win": win, "p_fix": p_fix, "p_del": p_del,
        })
    return pd.DataFrame(out)


def report(name: str, r: pd.DataFrame) -> None:
    if r.empty:
        print(f"{name}: no trades")
        return
    for tag, pxcol in (("A fixed 0.50 (screenshot)", "p_fix"),
                       ("B delta-priced (gist)", "p_del")):
        px = r[pxcol].to_numpy()
        gross = np.where(r["win"].to_numpy(), 1.0 / px - 1.0, -1.0)
        net = gross - fee(px)
        print(f"  {tag:26s} n={len(r):6,d}  WR {r['win'].mean():6.1%}  "
              f"avg px {px.mean():5.3f}  EV/$1 {net.mean():+8.4f}  "
              f"total {net.sum():+10.1f}")
    print("  by |window delta| bucket (WR / EV at delta price):")
    a = r["delta_pct"].abs()
    for lo, hi in [(0, .005), (.005, .02), (.02, .05), (.05, .10), (.10, 9)]:
        m = (a >= lo) & (a < hi)
        if not m.any():
            continue
        sub = r[m]
        px = sub["p_del"].to_numpy()
        net = np.where(sub["win"].to_numpy(), 1.0 / px - 1.0, -1.0) - \
            fee(px)
        print(f"    {lo:>5.3f}-{hi:<5.3f}%  n={len(sub):6,d}  WR {sub['win'].mean():6.1%}"
              f"  avg px {px.mean():5.3f}  EV/$1 {net.mean():+8.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--conf", type=float, default=0.30,
                    help="gist 'safe mode' minimum confidence")
    ap.add_argument("--always-trade", action="store_true",
                    help="gist behaviour: if nothing fired, take the best by T-5s")
    ap.add_argument("--cache", default="data/bars_1m")
    args = ap.parse_args()
    conf = 0.0 if args.always_trade else args.conf
    allr = []
    for sym in args.symbols:
        print(f"{sym}:")
        r = run(sym, args.days, conf, 300, args.cache)
        report(sym, r)
        allr.append(r)
        print()
    tot = pd.concat(allr, ignore_index=True)
    print(f"=== POOLED {len(tot):,} windows, {args.days}d ===")
    report("pooled", tot)
