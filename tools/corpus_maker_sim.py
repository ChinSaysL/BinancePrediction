#!/usr/bin/env python3
"""Passive maker simulation on the BTC-5m corpus (resting bids + cancels).

The premise being tested: our taker tests all lose about the size of the spread,
so rest AT the bid and collect it instead. The catch the simulation has to model
is that a resting buy at the bid does not fill because you want it to -- it fills
when somebody SELLS into you, which is when the market is coming down. That is
adverse selection, and it is measurable here.

Model
  - at `--place-tau` seconds left, rest a BUY on the chosen side at that side's
    BID (`up_buy_px` / `dn_buy_px` -- verified in FINDINGS to be the bid).
  - walk forward second by second to expiry:
      CANCEL if the underlying tape moves `--cancel-bps` against the order
             within one second (the defensive cancel);
      FILL   if that side's ASK trades down to `<= limit - buffer`, i.e. the
             market came to us. `--fill-buffer 0` is the optimistic reading
             (front of the queue), 0.01 is the strict one.
  - a fill settles at 1 or 0 per share; edge = win - limit - fee.

Reports the thing that decides the idea: the FILL RATE, the realized win rate on
the fills only, and edge per share with its day-clustered t. A maker strategy
lives or dies on whether the fills it gets are the good ones.

Both sides are reported: quoting one side, and quoting BOTH (where up_bid +
dn_bid = 0.99 medians -- which is only money if BOTH legs fill).

Read-only.
"""
from __future__ import annotations

import argparse
import collections
import glob
import os
import statistics as st

import pyarrow.parquet as pq

PRICES = "data/btc5m_prices.parquet"
ML = "data/btc5m_ml.parquet"
TICKS = "data/btc5m/data"


def ep(w) -> int:
    return int(str(w).rsplit("/", 1)[-1])


def load_tape(wid) -> dict[int, float]:
    """The underlying spot tape for one round: {epoch_sec: close}."""
    day, e = str(wid).split("/")
    path = f"{TICKS}/{day}/{e}/ticks_dense.parquet"
    if not os.path.exists(path):
        return {}
    t = pq.read_table(path, columns=["timestamp", "close"])
    ts, cl = t.to_pydict()["timestamp"], t.to_pydict()["close"]
    out = {}
    for i in range(len(ts)):
        ts_i = ts[i]
        if ts_i is None or cl[i] is None:
            continue
        # timestamps may be s, ms or us -- normalise on magnitude
        v = int(ts_i)
        if v > 10 ** 14:
            v //= 10 ** 6
        elif v > 10 ** 11:
            v //= 10 ** 3
        out[v] = cl[i]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--place-tau", type=float, default=180.0)
    ap.add_argument("--cancel-bps", type=float, default=2.0,
                    help="0 disables the defensive cancel")
    ap.add_argument("--fill-buffer", type=float, default=0.0,
                    help="fill needs ask <= limit - buffer")
    ap.add_argument("--side", choices=("fav", "up", "dn"), default="fav")
    ap.add_argument("--min-ask-obs", type=int, default=5,
                    help="require this many ask observations after placement")
    args = ap.parse_args()

    # outcome labels
    m = pq.read_table(ML, columns=["window_id", "y_up"])
    md = m.to_pydict()
    lab = {ep(md["window_id"][i]): md["y_up"][i] for i in range(m.num_rows)}

    p = pq.read_table(PRICES, columns=[
        "window_id", "sec", "tau_s", "up_px", "up_buy_px", "up_sell_px",
        "dn_buy_px", "dn_sell_px", "fees"])
    pd_ = p.to_pydict()
    pcols = list(pd_.keys())
    byw: dict[str, list[dict]] = collections.defaultdict(list)
    for i in range(p.num_rows):
        byw[pd_["window_id"][i]].append({c: pd_[c][i] for c in pcols})

    res = []
    for w, rs in byw.items():
        y = lab.get(ep(w))
        if y is None:
            continue
        rs.sort(key=lambda r: r["sec"])
        place = None
        for r in rs:
            if r["tau_s"] is not None and r["tau_s"] <= args.place_tau:
                place = r
                break
        if place is None:
            continue
        ub, db = place["up_buy_px"], place["dn_buy_px"]
        if not ub or not db:
            continue
        if args.side == "up":
            sides = ["Up"]
        elif args.side == "dn":
            sides = ["Down"]
        else:
            sides = ["Up", "Down"]
        # the "fav" mode quotes only the richer side (>= 0.5)
        if args.side == "fav":
            sides = ["Up"] if ub >= db else ["Down"]

        after = [r for r in rs if r["sec"] > place["sec"]]
        asks = [(r["up_sell_px"] if s == "Up" else r["dn_sell_px"]) for r in after
                for s in sides]
        if sum(1 for a in asks if a) < args.min_ask_obs:
            continue
        tape = load_tape(w) if args.cancel_bps else {}
        base = ep(w)

        for side in sides:
            L = ub if side == "Up" else db
            filled = False
            cancelled = False
            for r in after:
                b = r["tau_s"]
                if b is None or b < 0:
                    break
                if args.cancel_bps and tape:
                    # tape keys are ABSOLUTE epoch seconds = round start + sec
                    idx = base + r["sec"]
                    p_now = tape.get(idx)
                    p_prev = tape.get(idx - 1)
                    if p_now and p_prev:
                        move = (p_now - p_prev) / p_prev * 1e4
                        against = -move if side == "Up" else move
                        if against >= args.cancel_bps:
                            cancelled = True
                            break
                a = r["up_sell_px"] if side == "Up" else r["dn_sell_px"]
                if a and a <= L - args.fill_buffer - 1e-9:
                    filled = True
                    break
            if not filled:
                continue
            won = (y == 1) if side == "Up" else (y == 0)
            res.append({"day": str(w).split("/")[0], "side": side, "L": L,
                        "won": 1.0 if won else 0.0,
                        "edge": (1.0 if won else 0.0) - L - (place["fees"] or 0)})
    n_windows = len(byw)
    nplaced = sum(1 for r in res if True)
    print(f"windows: {n_windows:,}   place at tau<={args.place_tau:.0f}s  "
          f"cancel {args.cancel_bps}bps  fill buffer {args.fill_buffer}  "
          f"side={args.side}  fills={len(res)}")
    for side in ("Up", "Down"):
        g = [r for r in res if r["side"] == side]
        if not g:
            continue
        dm = collections.defaultdict(list)
        for r in g:
            dm[r["day"]].append(r["edge"])
        days = [st.mean(v) for v in dm.values()]
        sd = st.stdev(days) if len(days) > 1 else 0.0
        t = st.mean(days) / (sd / len(days) ** 0.5) if sd else 0.0
        print(f"\n  {side}: fills {len(g)}  ({100*len(g)/n_windows:.1f}% of windows)"
              f"  mean limit {st.mean([r['L'] for r in g]):.4f}"
              f"  realized {st.mean([r['won'] for r in g]):.4f}"
              f"  edge/share {st.mean([r['edge'] for r in g]):+.4f}"
              f"  ROI {st.mean([r['edge'] for r in g])/st.mean([r['L'] for r in g])*100:+.2f}%"
              f"  day-t {t:+.2f}  ({sum(1 for d in days if d>0)}/{len(days)} days +)")
    if not res:
        print("\n  no fills at all under this rule")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
