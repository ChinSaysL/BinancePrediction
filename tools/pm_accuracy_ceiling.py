"""How accurate CAN a Binance-based forecast of the Chainlink settle be?

WHY THIS FILE EXISTS. "Let's push accuracy to 99.9%, one formula per coin" is
the natural next move and it is a TRAP, because accuracy is not a property of
the formula. It is a property of the formula AND the rounds you apply it to.
Predict only on rounds far from the strike and accuracy climbs toward 100%
while the money goes to zero, because a bigger distance does not PREDICT the
win -- it IS the win, and that side is priced near it.

So this measures accuracy TOGETHER WITH the three things that decide whether it
is worth anything:
    coverage  -- how many rounds survive the threshold
    ask       -- what the venue charges for that side at that instant
    break-even-- the accuracy the ask requires; the GAP is the only edge

and it does it per coin, since the Binance-vs-Chainlink basis differs by asset
(measured earlier: uniform -10 bps vs another aggregate, and the residual of the
tail-60 rule concentrates inside ~2 bps of the strike).

Output per coin: a threshold sweep, then the smallest threshold that reaches a
target accuracy and what is left of the sample there.

Usage (on the VPS, analysis venv):
    /root/analysis-venv/bin/python tools/pm_accuracy_ceiling.py --tau 10
    /root/analysis-venv/bin/python tools/pm_accuracy_ceiling.py --tau 3
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

import numpy as np
import pandas as pd

DB = "/root/BinancePrediction/data/btcpredict.db"
LOOKBACK = 60.0
FEE = lambda p: 0.07 * p * (1 - p)                             # noqa: E731
COINS = ("btc", "eth", "bnb", "sol", "xrp", "doge")
GRID = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 40.0, 80.0, 200.0)


def load(db: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = pd.read_sql_query("""
        select q.slug, q.ts_ms, q.tau_s, q.spot, q.up_ask, q.down_ask
        from quotes q
        where q.spot is not null and q.ts_ms is not null
        order by q.slug, q.ts_ms""", con)
    r = pd.read_sql_query("""
        select slug, strike, winner, feed_provider from rounds
        where winner in ('Up','Down') and strike is not null""", con)
    con.close()
    r = r[r.feed_provider.fillna("").str.contains("twap", case=False)]
    df = q.merge(r, on="slug", how="inner")
    df["coin"] = df.slug.str.split("-").str[0]
    return df.sort_values(["slug", "ts_ms"])


def per_round(df: pd.DataFrame, tau_t: float) -> pd.DataFrame:
    """One row per round: the tail-60 forecast at `tau_t`, its distance, prices."""
    out = []
    for slug, g in df.groupby("slug", sort=False):
        g = g.sort_values("ts_ms")
        ts = g.ts_ms.to_numpy(dtype=float)
        sp = g.spot.to_numpy(dtype=float)
        tau = g.tau_s.to_numpy(dtype=float)
        strike = float(g.strike.iloc[0])
        winner = g.winner.iloc[0]
        if not np.isfinite(strike) or strike <= 0:
            continue
        i = int(np.argmin(np.abs(tau - tau_t)))
        if abs(tau[i] - tau_t) > max(1.2, 0.1 * tau_t):
            continue
        lo = ts[i] - LOOKBACK * 1000.0
        m = (ts >= lo) & (ts <= ts[i]) & np.isfinite(sp)
        mk = float(np.mean(sp[m])) if m.sum() else float(sp[i])
        t = float(tau[i])
        fc = (float(sp[i]) if t >= LOOKBACK
              else ((LOOKBACK - t) * mk + t * float(sp[i])) / LOOKBACK)
        out.append(dict(
            slug=slug, coin=g.coin.iloc[0], tau=t, winner=winner, strike=strike,
            d_fc=1e4 * (fc - strike) / strike,
            d_spot=1e4 * (float(sp[i]) - strike) / strike,
            up_ask=float(g.up_ask.iloc[i]), dn_ask=float(g.down_ask.iloc[i]),
            printed=min(1.0, (LOOKBACK - t) / LOOKBACK if t < LOOKBACK else 0.0),
            day=pd.to_datetime(ts[i], unit="ms", utc=True).strftime("%m-%d")))
    return pd.DataFrame(out)


def sweep(d: pd.DataFrame, target: float) -> None:
    """Accuracy on the whole sample, price on the tradable subset.

    These are DIFFERENT populations on purpose. Filtering the accuracy column by
    the ask deletes every round where the market already agrees with us (its ask
    is ~1.00) and leaves only the disagreements -- which is how a 92.6% forecast
    first read as 16%. So: acc is measured on everything, money only where a book
    exists at all (both sides quote above zero).
    """
    if d.empty:
        print("    (no rows)"); return
    print(f"    {'thr':>7} {'n':>5} {'cover':>6} {'acc':>7} {'trad':>5} {'ask':>6} "
          f"{'be%':>7} {'edge':>5} {'per-share':>10}")
    hit = None
    for T in GRID:
        s = d[d.d_fc.abs() >= T]
        if len(s) < 4:
            print(f"    {T:>7.1f} {len(s):>5d} {'--':>6}"); continue
        up = (s.d_fc > 0).to_numpy()
        acc = float((up == (s.winner == "Up").to_numpy()).mean())
        tr = s[(s.up_ask > 0.001) & (s.dn_ask > 0.001)]
        if len(tr) < 4:
            print(f"    {T:>7.1f} {len(s):>5d} {len(s) / len(d):>6.1%} {acc:>7.3%} "
                  f"{len(tr):>5d}   (no book)")
            continue
        u2 = (tr.d_fc > 0).to_numpy()
        ask = np.where(u2, tr.up_ask.to_numpy(), tr.dn_ask.to_numpy())
        won = np.where(u2, (tr.winner == "Up").to_numpy(),
                       (tr.winner == "Down").to_numpy())
        be = ask + np.array([FEE(a) for a in ask])
        per = won - be
        flag = "YES" if acc > be.mean() else "no"
        print(f"    {T:>7.1f} {len(s):>5d} {len(s) / len(d):>6.1%} {acc:>7.3%} "
              f"{len(tr):>5d} {ask.mean():>6.3f} {be.mean():>7.1%} {flag:>5} "
              f"{per.mean():>+10.4f}")
        if hit is None and acc >= target:
            hit = (T, len(s), len(s) / len(d), acc, be.mean())
    if hit:
        T, n, cov, acc, be = hit
        print(f"    -> {target:.1%} accuracy first appears at thr {T:.1f} bps: "
              f"n={n} ({cov:.2%} of rounds), and the book wants {be:.4%} "
              f"=> {'EDGE' if acc > be else 'NO EDGE'} ({acc - be:+.3%})")
    else:
        print(f"    -> {target:.1%} is NOT reached at any threshold up to "
              f"{GRID[-1]:.0f} bps (best seen: {max(
                  float(((x.d_fc > 0) == (x.winner == 'Up')).mean())
                  for x in (d[d.d_fc.abs() >= t] for t in GRID) if len(x) >= 4
              ):.3%})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau", type=float, default=10.0)
    ap.add_argument("--target", type=float, default=0.999)
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()
    df = load(a.db)
    df = df[df.slug.str.contains("-5m-")]      # 15m rounds share the tail-60 rule
    if df.empty:
        print("no Polymarket-era rounds"); return 1
    pr = per_round(df, a.tau)
    if pr.empty:
        print("no usable rows"); return 1
    print(f"=== FORECAST ACCURACY vs WHAT IT COSTS, at tau {a.tau:.0f}s ===")
    print(f"    {len(pr)} rounds, coins {sorted(pr.coin.unique())}, "
          f"days {sorted(pr.day.unique())}")
    print(f"    every row is the SAME forecast; only the distance cut changes.")
    print(f"    read cover and edge, never acc alone.\n")
    print(f"  -- ALL COINS --")
    sweep(pr, a.target)
    for c in COINS:
        s = pr[pr.coin == c]
        if len(s) < 12:
            print(f"\n  -- {c.upper()} -- n={len(s)} (thin)"); continue
        print(f"\n  -- {c.upper()} -- n={len(s)}")
        sweep(s, a.target)
    print(f"\n=== IS A SEPARATE FORMULA PER COIN WORTH IT? ===")
    print("    the tail-60 mean vs the raw spot, per coin, at the same tau")
    print(f"    {'coin':>5} {'n':>5} {'tail60 acc':>11} {'spot acc':>9} "
          f"{'tail60 be':>10} {'spot be':>8}")
    for c in COINS:
        s = pr[pr.coin == c]
        if len(s) < 12:
            continue
        row = [c, len(s)]
        for col in ("d_fc", "d_spot"):
            t = s.dropna(subset=[col])
            t = t[(t.up_ask > 0.001) & (t.up_ask < 0.999)]
            if t.empty:
                row += [float("nan"), float("nan")]; continue
            up = t[col] > 0
            ask = np.where(up, t.up_ask, t.dn_ask)
            won = np.where(up, t.winner == "Up", t.winner == "Down")
            row += [won.mean(), (ask + np.array([FEE(x) for x in ask])).mean()]
        print(f"    {row[0]:>5} {row[1]:>5} {row[2]:>11.3%} {row[4]:>9.3%} "
              f"{row[3]:>10.1%} {row[5]:>8.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
