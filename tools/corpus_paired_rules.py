#!/usr/bin/env python3
"""Rules priced at the EXECUTABLE ask, with the opposite side as control.

Joins the 31-day feature table (momentum at nine horizons, spot-vs-strike
distance, vol_surge, CVD) to the 8-day quote table (real buy/sell prices) on the
round id, then asks the only question that matters: does choosing a side beat
choosing the other side at the same instant?

  PAIRED = EV(chosen side) - EV(opposite side at the same instant)

Both legs pay the ask, so the price LEVEL cancels and what survives is
directional skill. A hit-rate table (tools/corpus_signal_power.py) cannot decide
this: it compares against a 50% baseline while the price of a "99.8%-certain"
side is ~0.99 -- the delta-selection mirage this repo already documents.

`--pay ask` is the executable cost. `--pay bid` is included to show the size of
the trap: the same rules read very differently when scored at the bid, which is
what a mid-like or last-trade price column silently does.

One row per round (the last quote inside the tau window), because quotes inside
a round share a single outcome and would otherwise inflate n.

Read-only.
"""
from __future__ import annotations

import argparse
import collections
import statistics as st

import pyarrow.parquet as pq

PRICES = "data/btc5m_prices.parquet"
ML = "data/btc5m_ml.parquet"

# (name, feature, threshold, direction source)
RULES = [
    ("FAV (priced side)", None, 0.0, "price"),
    ("DIST>=2bp", "delta_bps", 2.0, "self"),
    ("DIST>=5bp", "delta_bps", 5.0, "self"),
    ("DIST>=10bp", "delta_bps", 10.0, "self"),
    ("MOM_30>=4bp", "mom_30_bps", 4.0, "self"),
    ("MOM_120>=4bp", "mom_120_bps", 4.0, "self"),
    ("VOLSURGE>=2.5", "vol_surge", 2.5, "mom_5_bps"),
    ("CVD>=0.6", "cvd_ratio", 0.6, "cvd"),
    ("ZTAU>=1.0", "z_tau", 1.0, "z_tau"),
]


def ep(w) -> int:
    return int(str(w).rsplit("/", 1)[-1])


def pnl(p: float, won: bool) -> float:
    return (1 - p) / p if won else -1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau-lo", type=float, default=10.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    ap.add_argument("--pay", choices=("ask", "bid", "mid"), default="ask")
    args = ap.parse_args()

    feats = sorted({r[1] for r in RULES if r[1]}
                   | {r[3] for r in RULES if r[3] not in ("self", "price")})
    m = pq.read_table(ML, columns=["window_id", "y_up"] + feats)
    md = m.to_pydict()
    lab: dict[int, dict] = {}
    for i in range(m.num_rows):
        lab[ep(md["window_id"][i])] = {"y": md["y_up"][i],
                                       **{f: md[f][i] for f in feats}}

    p = pq.read_table(PRICES, columns=[
        "window_id", "sec", "tau_s", "up_buy_px", "dn_buy_px",
        "up_sell_px", "dn_sell_px", "mid", "fees"])
    pd_ = p.to_pydict()

    # ONE row per round: the last quote inside the tau window (smallest tau).
    best: dict[str, dict] = {}
    for i in range(p.num_rows):
        w = pd_["window_id"][i]
        meta = lab.get(ep(w))
        if not meta or meta["y"] is None:
            continue
        tau = pd_["tau_s"][i]
        if tau is None or not (args.tau_lo <= tau <= args.tau_hi):
            continue
        if args.pay == "ask":
            ub, db = pd_["up_sell_px"][i], pd_["dn_sell_px"][i]
        elif args.pay == "bid":
            ub, db = pd_["up_buy_px"][i], pd_["dn_buy_px"][i]
        else:
            mid = pd_["mid"][i]
            ub, db = (mid, 1 - mid) if mid else (None, None)
        if not ub or not db or not (0 < ub < 1) or not (0 < db < 1):
            continue
        cur = best.get(w)
        if cur is not None and tau >= cur["tau"]:
            continue
        best[w] = {"day": str(w).split("/")[0], "tau": tau, "ub": ub, "db": db,
                   "y": meta["y"], "fee": pd_["fees"][i],
                   **{f: meta[f] for f in feats}}
    rounds = list(best.values())
    print(f"rounds with a tradable ask in tau [{args.tau_lo:.0f}, {args.tau_hi:.0f}]s: "
          f"{len(rounds):,} over {len({r['day'] for r in rounds})} days "
          f"(paying the {args.pay.upper()})")

    print(f"\n{'rule':18} {'n':>5} {'side_px':>8} {'hit':>7} {'edge/sh':>8} "
          f"{'ROI':>7} {'se':>7} {'day-t':>7} {'pos/day':>7}")
    for name, feat, thr, dirn in RULES:
        byday: dict[str, list[float]] = collections.defaultdict(list)
        pick_px, pick_won = [], []
        for r in rounds:
            if dirn == "price":
                side = "Up" if r["ub"] >= r["db"] else "Down"
            else:
                v = r[feat] if dirn == "self" else r.get(dirn)
                if v is None or abs(v) < thr:
                    continue
                side = "Up" if v > 0 else "Down"
            p_side = r["ub"] if side == "Up" else r["db"]
            won = (r["y"] == 1) if side == "Up" else (r["y"] == 0)
            # PER-SHARE edge: buy 1 share at p, receive 1 if it wins.
            # Never per-$ staked -- cheap tokens make that levered and the
            # mean-of-ratios estimator unstable (see the repo's sizing notes).
            e = (1.0 if won else 0.0) - p_side - r["fee"]
            byday[r["day"]].append(e)
            pick_px.append(p_side)
            pick_won.append(1.0 if won else 0.0)
        if not byday:
            continue
        dm = [st.mean(v) for v in byday.values()]
        sd = st.stdev(dm) if len(dm) > 1 else 0.0
        t = st.mean(dm) / (sd / len(dm) ** 0.5) if sd else 0.0
        mpx = st.mean(pick_px)
        print(f"{name:18} {len(pick_px):>5} {mpx:8.4f} "
              f"{st.mean(pick_won):7.4f} {st.mean(dm):+8.4f} "
              f"{st.mean(dm)/mpx*100:+6.2f}% {sd/len(dm)**0.5:7.4f} {t:+7.2f} "
              f"{sum(1 for x in dm if x>0):>3}/{len(dm):<3}")
    print("\n  edge/share = P(win) - price paid - fee. Positive with |day-t| > 2 wins.")
    print("  A near-constant small positive across unrelated rules is a pricing")
    print("  OFFSET (the recorded ask is not the ask you get), not N discoveries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
