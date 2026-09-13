"""Does the window-delta signal hold across 15,264 windows, or was 5 days luck?

Our own tape gave 251 BTC windows in the 0.05-0.10% delta cell with a 95.9% hold
rate -- good enough to look at, too small to believe. This corpus (yamalalaxman/
btc-updown-5m-trades) has one dense tick file PER SECOND for 53 days of 5-minute
BTC up/down markets, plus the final outcome, so the same question can be asked
with ~10x the sample and a day-clustered test that actually means something.

Schema (verified): ticks_dense.parquet = 300 rows/second of BTC spot OHLC +
taker volume; metadata.json = outcomes + final outcomePrices.

Strike = the spot at the window's first second (the market resolves Up if the
price at the end >= the price at the start). Outcome is cross-checked against the
metadata, and a mismatch rate is printed -- if that is not ~0 the reader is wrong.

No market prices exist in this corpus, so this measures the SIGNAL (hold rate per
delta band) and prints the implied fair price. Comparing that fair price with the
asks our own venue actually charges is what decides whether there is an edge.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os

import pandas as pd

BANDS = [(0.0, 0.005), (0.005, 0.02), (0.02, 0.05), (0.05, 0.10),
         (0.10, 0.20), (0.20, 0.50), (0.50, 99.0)]
TAUS = [10, 30, 60, 120, 180, 240]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - s) / d, (c + s) / d)


def _as_list(v):
    """metadata.json stores outcomes/outcomePrices as JSON STRINGS, not lists.
    Indexing them as strings silently matches substrings and kills every row."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            return []
    return v if isinstance(v, list) else []


def load(root: str) -> pd.DataFrame:
    rows = []
    files = sorted(glob.glob(os.path.join(root, "data", "*", "*", "ticks_dense.parquet")))
    for f in files:
        d = f.rsplit("/", 1)[0]
        mp = os.path.join(d, "metadata.json")
        if not os.path.exists(mp):
            continue
        try:
            t = pd.read_parquet(f, columns=["timestamp", "open", "close"])
            m = json.load(open(mp))
        except Exception:  # noqa: BLE001
            continue
        if len(t) < 100:
            continue
        outcomes = _as_list(m.get("outcomes"))
        prices = _as_list(m.get("outcomePrices"))
        try:
            final = [float(x) for x in prices]
        except (TypeError, ValueError):
            continue
        if len(outcomes) < 2 or len(final) < 2 or "Up" not in outcomes:
            continue
        up_won = final[outcomes.index("Up")] > 0.5
        strike = float(t["open"].iloc[0])
        if strike <= 0:
            continue
        last = float(t["close"].iloc[-1])
        rec = {"slug": d.split("/")[-1], "date": f.split("/")[-3],
               "strike": strike, "up_won": bool(up_won),
               "sanity_ok": (last >= strike) == bool(up_won)}
        n = len(t)
        for tau in TAUS:
            i = max(0, min(n - 1, n - tau))
            spot = float(t["close"].iloc[i])
            rec[f"d{tau}"] = (spot - strike) / strike * 100.0
        rows.append(rec)
    return pd.DataFrame(rows)


def stats(sub: pd.DataFrame, tau: int) -> dict:
    d = sub[f"d{tau}"].to_numpy()
    direction_up = d > 0
    win = direction_up == sub["up_won"].to_numpy()
    n = len(win)
    k = int(win.sum())
    lo, hi = wilson(k, n)
    days = sub["date"].to_numpy()
    df = pd.DataFrame({"win": win.astype(float), "date": days})
    g = df.groupby("date")["win"].mean()
    t_day = (g.mean() / (g.std(ddof=1) / math.sqrt(len(g)))
             if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = n // 2
    best = g.idxmax()
    return {"n": n, "wr": win.mean(), "lo": lo, "hi": hi,
            "h1": win[:h].mean(), "h2": win[h:].mean(),
            "drop1": float(df[df["date"] != best]["win"].mean()),
            "ndays": len(g), "t_day": t_day}


def line(tag: str, s: dict) -> str:
    return (f"{tag:26s} n={s['n']:5d}  hold {s['wr']:6.1%}  "
            f"CI[{s['lo']:5.1%},{s['hi']:5.1%}]  halves {s['h1']:5.1%}/{s['h2']:5.1%}  "
            f"drop1 {s['drop1']:5.1%}  days {s['ndays']:3d}  day-t {s['t_day']:+.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/root/btc5m")
    ap.add_argument("--tau", type=int, nargs="+", default=TAUS)
    ap.add_argument("--min-n", type=int, default=40)
    args = ap.parse_args()

    df = load(args.root)
    if df.empty:
        print("no windows loaded")
        return
    agree = df["sanity_ok"].mean()
    print(f"windows loaded: {len(df)} over {df['date'].nunique()} days "
          f"({df['date'].min()} -> {df['date'].max()})")
    print(f"outcome cross-check (our Up vs metadata): {agree:.2%} agreement"
          + ("  OK" if agree > 0.99 else "  <-- READER IS WRONG, fix before trusting anything"))
    print(f"median |delta| at tau=60s: {df['d60'].abs().median():.4f}%\n")

    for tau in args.tau:
        print(f"=== tau={tau}s ===")
        print(line("ALL", stats(df, tau)))
        for lo, hi in BANDS:
            a = df[f"d{tau}"].abs()
            sub = df[(a >= lo) & (a < hi)]
            if len(sub) < args.min_n:
                continue
            print(line(f"  |delta| {lo:.3f}-{hi:.2f}%", stats(sub, tau)))
        print()

    # the cell we have been chasing, at its most recent reading
    print("=== the cell in question, |delta| 0.05-0.10% at tau=60s ===")
    a = df["d60"].abs()
    cell = df[(a >= 0.05) & (a < 0.10)]
    if len(cell) >= args.min_n:
        s = stats(cell, 60)
        print(line("  BTC 5m 0.05-0.10%", s))
        print(f"  implied fair price: {s['wr']:.4f}   "
              f"(our venue charged an average ask of 0.938 in this cell)")


if __name__ == "__main__":
    main()
