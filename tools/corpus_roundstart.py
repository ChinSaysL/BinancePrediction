#!/usr/bin/env python3
"""Round-start ("initialization lag") test on the BTC-5m corpus.

THE CLAIM BEING TESTED (third-party idea #1):
  A new 5-minute contract initializes every 300s. The strike is pegged to the
  spot at the start second, but the makers' books take 1-4 s to spin up, so
  early in the round the quote is still ~0.50 while the spot has ALREADY moved
  away from the strike. Exploit: buy the side the spot has moved toward.

Why it is worth a test: it is the one structural claim in the batch that does
not require speed. We are not racing anyone -- we are reading a stale quote in
the first few seconds of a round that we know starts on a fixed clock.

What makes it falsifiable: at sec=0 the spot IS the strike, so the fair price is
0.50 by construction and no edge can exist there. The edge, if any, has to
appear at sec>=1, and it has to be BIGGER than the ask we pay.

Method, per round, one row per (round, sec):
  strike   = tape at sec 0
  dist_bps = (tape[sec] - strike) / strike * 1e4
  side     = Up if dist > 0 else Down        (the side the spot moved toward)
  price    = that side's ASK at that sec     (up_sell_px / dn_sell_px)
  edge     = (1 if that side won else 0) - price - fee    PER SHARE

Controls, because a positive level here is exactly how this project has been
fooled before:
  - opposite side at the SAME sec (the paired difference cancels any pricing error)
  - buy Up unconditionally at the same sec (the base rate)
  - a minimum |dist| sweep: a real signal is roughly flat in the threshold once
    past the noise floor; a fitted one rises monotonically.

Read-only.
"""
from __future__ import annotations

import argparse
import collections
import os
import statistics as st

import pyarrow.parquet as pq

PRICES = "data/btc5m_prices.parquet"
ML = "data/btc5m_ml.parquet"
TICKS = "data/btc5m/data"


def ep(w) -> int:
    return int(str(w).rsplit("/", 1)[-1])


def load_tape(wid) -> dict[int, float]:
    """The underlying spot tape for one round: {relative_sec: close}.

    Keys are made RELATIVE here (sec 0 = round open) so the strike is tape[0]
    regardless of whether the stored timestamps are absolute epoch seconds or
    already relative.
    """
    day, e = str(wid).split("/")
    path = f"{TICKS}/{day}/{e}/ticks_dense.parquet"
    if not os.path.exists(path):
        return {}
    t = pq.read_table(path, columns=["timestamp", "close"])
    d = t.to_pydict()
    ts, cl = d["timestamp"], d["close"]
    rows = []
    for i in range(len(ts)):
        if ts[i] is None or cl[i] is None:
            continue
        v = int(ts[i])
        if v > 10 ** 14:
            v //= 10 ** 6
        elif v > 10 ** 11:
            v //= 10 ** 3
        rows.append((v, cl[i]))
    if not rows:
        return {}
    rows.sort()
    t0 = rows[0][0]
    # if the stamps are absolute the offset is huge; if relative t0 is ~0.
    # Either way subtracting t0 gives sec-since-tape-start.
    out = {}
    for v, c in rows:
        out[v - t0] = c
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sec", type=int, default=6,
                    help="test every second in 0..max-sec inclusive")
    ap.add_argument("--min-dist-bps", type=float, default=0.0)
    ap.add_argument("--min-price", type=float, default=0.05)
    ap.add_argument("--max-price", type=float, default=0.95)
    args = ap.parse_args()

    m = pq.read_table(ML, columns=["window_id", "y_up"])
    md = m.to_pydict()
    lab = {ep(w): md["y_up"][i] for i, w in enumerate(md["window_id"])}

    p = pq.read_table(PRICES, columns=[
        "window_id", "sec", "up_px", "up_buy_px", "up_sell_px",
        "dn_buy_px", "dn_sell_px", "fees"])
    pd_ = p.to_pydict()
    byw: dict[str, dict[int, dict]] = collections.defaultdict(dict)
    for i in range(p.num_rows):
        w = pd_["window_id"][i]
        byw[w][pd_["sec"][i]] = {c: pd_[c][i] for c in pd_}

    recs = []
    n_tape_missing = 0
    for w, secs in byw.items():
        y = lab.get(ep(w))
        if y is None:
            continue
        tape = load_tape(w)
        if not tape:
            n_tape_missing += 1
            continue
        strike = tape.get(0)
        if not strike:
            continue
        for s in range(0, args.max_sec + 1):
            r = secs.get(s)
            if r is None:
                continue
            px_now = tape.get(s)
            if not px_now:
                continue
            dist = (px_now - strike) / strike * 1e4
            # the side the spot has moved toward
            side = "Up" if dist > 0 else "Down"
            opp = "Down" if side == "Up" else "Up"
            px = r["up_sell_px"] if side == "Up" else r["dn_sell_px"]
            pxo = r["up_sell_px"] if opp == "Up" else r["dn_sell_px"]
            if not px or not pxo:
                continue
            if not (args.min_price <= px <= args.max_price):
                continue
            if abs(dist) < args.min_dist_bps:
                continue
            won = (y == 1) if side == "Up" else (y == 0)
            wono = (y == 1) if opp == "Up" else (y == 0)
            fee = (r["fees"] or 0.0) * px
            recs.append({
                "day": str(w).split("/")[0], "sec": s, "side": side,
                "dist": dist, "px": px, "won": 1.0 if won else 0.0,
                "edge": (1.0 if won else 0.0) - px - fee,
                "pxo": pxo, "wono": 1.0 if wono else 0.0,
                "edgeo": (1.0 if wono else 0.0) - pxo - fee,
                "up": 1.0 if y == 1 else 0.0,
            })
    print(f"rounds with a tape: {len(byw)-n_tape_missing} "
          f"(missing {n_tape_missing})   rows: {len(recs)}"
          f"   min|dist| {args.min_dist_bps}")
    if not recs:
        print("nothing")
        return 0

    def rep(rows, label):
        if not rows:
            return
        byday = collections.defaultdict(list)
        for r in rows:
            byday[r["day"]].append(r["edge"])
        days = [st.mean(v) for v in byday.values()]
        sd = st.stdev(days) if len(days) > 1 else 0.0
        t = st.mean(days) / (sd / len(days) ** 0.5) if sd else 0.0
        e = st.mean([r["edge"] for r in rows])
        po = st.mean([r["pxo"] for r in rows])
        eo = st.mean([r["edgeo"] for r in rows])
        diff = st.mean([r["edge"] - r["edgeo"] for r in rows])
        print(f"    {label:12s} n={len(rows):5d} price {st.mean([r['px'] for r in rows]):.4f}"
              f"  won {st.mean([r['won'] for r in rows]):.4f}"
              f"  edge/sh {e:+.4f}  day-t {t:+5.2f} ({sum(1 for x in days if x>0)}/{len(days)})"
              f"   | opp px {po:.4f} edge {eo:+.4f}  PAIR {diff:+.4f}")

    print("\n  === by second (side the spot moved toward, bought at the ASK) ===")
    for s in range(0, args.max_sec + 1):
        rep([r for r in recs if r["sec"] == s], f"sec={s}")
    print("  === all early seconds pooled ===")
    rep([r for r in recs if 1 <= r["sec"] <= args.max_sec], "sec>=1")

    print("\n  === buy Up unconditionally at the same seconds (base rate) ===")
    allrows = [r for r in recs if 1 <= r["sec"] <= args.max_sec]
    if allrows:
        u = st.mean([r["up"] for r in allrows])
        print(f"    P(Up) {u:.4f} over {len(allrows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
