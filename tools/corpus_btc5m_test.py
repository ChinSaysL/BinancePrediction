#!/usr/bin/env python3
"""Test the "near-certain winner" and momentum ideas on the BTC-5m corpus.

Why this corpus: `data/btc5m_prices.parquet` carries, per second and per round,
the EXECUTABLE buy price for each side (`up_buy_px` / `dn_buy_px`, 93%
populated) plus `fees`. The live venue DB has no trustworthy ask -- `up_price`
sits outside its own book on average -- so every "buy the favourite" result
measured there had to be thrown away. Here the ask is the ask.

Two datasets, deliberately kept apart:
  prices: 8 days, 2,240 windows, executable buy price  -> the honest test
  ml    : 31 days, 8,436 windows, only `px` (the odds) -> the POWER test, but
          scored against a price we cannot trade at, so it is labelled as such

Rules (one entry per window, priced at the EXECUTABLE buy price):
  FAV   buy the side with the higher buy price, if >= --fair-min
  MOM   buy the side of the trailing --mom-s second tape move
  OPP   the opposite side at FAV's entries -- the control that isolates skill
P&L per $1: win -> (1-p)/p, lose -> -1, less the round's fee.

Read-only.
"""
from __future__ import annotations

import argparse
import collections
import statistics as st

import pyarrow.parquet as pq

PRICES = "data/btc5m_prices.parquet"
ML = "data/btc5m_ml.parquet"


def epoch(wid) -> int:
    return int(str(wid).rsplit("/", 1)[-1])


def load_prices() -> list[dict]:
    t = pq.read_table(PRICES, columns=[
        "window_id", "sec", "tau_s", "up_px", "dn_px", "up_buy_px", "dn_buy_px",
        "up_sell_px", "dn_sell_px", "mid", "fees"])
    d = t.to_pydict()
    rows = []
    for i in range(t.num_rows):
        rows.append({k: d[k][i] for k in d})
    return rows


def labels() -> dict[int, dict]:
    """outcome + day per window, from the ML table (y_up = did Up win)."""
    t = pq.read_table(ML, columns=["window_id", "day", "y_up"])
    d = t.to_pydict()
    out: dict[int, dict] = {}
    for i in range(t.num_rows):
        w = epoch(d["window_id"][i])
        y = d["y_up"][i]
        if y is None:
            continue
        out[w] = {"y": int(y), "day": d["day"][i], "win": "Up" if int(y) else "Down"}
    return out


def day_of(wid) -> str:
    return str(wid).split("/")[0]


def pnl(p: float, won: bool) -> float:
    return (1 - p) / p if won else -1.0


def day_t(records, value) -> tuple[float, str]:
    g: dict[str, list[float]] = {}
    for r in records:
        g.setdefault(r["day"], []).append(value(r))
    if len(g) < 2:
        return 0.0, ""
    m = [st.mean(v) for v in g.values()]
    sd = st.stdev(m)
    t = st.mean(m) / (sd / len(m) ** 0.5) if sd else 0.0
    return t, "".join("+" if x > 0 else "-" for x in
                      [st.mean(g[k]) for k in sorted(g)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=20.0)
    ap.add_argument("--fair-min", type=float, default=0.0)
    ap.add_argument("--mom-s", type=float, default=10.0)
    ap.add_argument("--pay", choices=("ask", "bid", "mid"), default="ask",
                    help="ask = *_sell_px (the executable cost to buy; the "
                         "column names read as maker-side orders), bid = "
                         "*_buy_px (what you would get SELLING -- scoring "
                         "a buy here is the spread artefact), mid = mid")
    args = ap.parse_args()

    lab = labels()
    rows = load_prices()
    by_w: dict[int, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_w[epoch(r["window_id"])].append(r)
    print(f"prices: {len(rows):,} rows / {len(by_w):,} windows :: "
          f"labels: {len(lab):,} windows over {len({v['day'] for v in lab.values()})} days")

    picks, mid_picks, mom_picks = [], [], []
    fee_bps = []
    for w, rs in by_w.items():
        meta = lab.get(w)
        if not meta:
            continue
        rs.sort(key=lambda r: r["sec"])
        win = rs[0].get("fees")
        # one observation: the LAST row inside the tau window
        sel = [r for r in rs if r["tau_s"] is not None
               and args.tau_lo <= r["tau_s"] <= args.tau_hi]
        if not sel:
            continue
        q = sel[-1]
        if args.pay == "ask":
            ub, db = q["up_sell_px"], q["dn_sell_px"]
        elif args.pay == "bid":
            ub, db = q["up_buy_px"], q["dn_buy_px"]
        else:
            ub, db = q["mid"], (1 - q["mid"]) if q["mid"] else None
        if not ub or not db or not (0 < ub < 1) or not (0 < db < 1):
            continue
        fee = float(win or 0.0)
        fee_bps.append(fee * 1e4)
        fav = "Up" if ub >= db else "Down"
        px = ub if fav == "Up" else db
        rec = {"day": day_of(q["window_id"]), "fav": fav, "px": px,
               "won": meta["win"] == fav, "fee": fee, "w": w}
        if px >= args.fair_min:
            picks.append(rec)
            # control: the OTHER side at the same instant
            oside = "Down" if fav == "Up" else "Up"
            opx = db if fav == "Up" else ub
            mid_picks.append({"day": rec["day"], "fav": oside, "px": opx,
                              "won": meta["win"] == oside, "fee": fee, "w": w})
        # momentum on the tape, from the ml features if available
    print(f"\nTAU WINDOW tau in [{args.tau_lo:.0f}, {args.tau_hi:.0f}]s, "
          f"fair_min {args.fair_min:.2f}, paying the {args.pay.upper()}")
    print(f"  fee column: mean {st.mean(fee_bps):.1f} bps  median {st.median(fee_bps):.1f} bps")

    for name, recs in (("FAVOURITE @ ask", picks), ("OPPOSITE @ ask (control)", mid_picks)):
        if not recs:
            continue
        ev = [pnl(r["px"], r["won"]) for r in recs]
        wr = sum(1 for r in recs if r["won"]) / len(recs)
        net = st.mean([e - r["fee"] for e, r in zip(ev, recs)])
        t, signs = day_t(recs, lambda r: pnl(r["px"], r["won"]) - r["fee"])
        print(f"\n  {name}: n={len(recs)}  mean paid {st.mean([r['px'] for r in recs]):.4f}"
              f"  realized {wr:.4f}  EV_net {net:+.4f}  day-t {t:+.2f}  (per-day signs {signs})")

    print("\n  by day (EV_net/$1, n):")
    g: dict[str, list] = collections.defaultdict(list)
    for r in picks:
        g[r["day"]].append(pnl(r["px"], r["won"]) - r["fee"])
    for d in sorted(g):
        v = g[d]
        print(f"    {d}  {st.mean(v):+7.3f}  n={len(v):>4}")
    # how much of a positive total is one day?
    if len(g) > 1:
        tot = {d: st.mean(v) * len(v) for d, v in g.items()}
        best = max(tot, key=lambda d: tot[d])
        s, n = sum(tot.values()), sum(len(v) for v in g.values())
        print(f"    drop best day ({best}): {(s - tot[best]) / (n - len(g[best])):+.4f}/$1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
