"""PRE-CROSS ENTRY: buy the side the spot is walking INTO, before the cross.

THE CLAIM (operator, 2026-09-17): by the time Binance spot crosses the strike,
the venue's quoting engine has already pulled the asks to 0.95, so the cross arm
is always paying the post-reprice price. Front-run it: buy the side the spot is
APPROACHING while it is still the underdog, and hold.

This is the version of "front-running" that does NOT require winning a latency
race. Pre-firing on a sub-second predictor needs a lead longer than the venue's
own ~550 ms order path and the measured hit rate of the best predictor is 21%
(`tools/prefire_physics.py`). Buying at 1 bp from the strike needs no speed at
all -- only that the market misprices a round whose spot is one tick away from
crossing.

DATA (the only place a day-clustered test is possible on the PM side):
    data/btc5m_prices.parquet   8 days, 2,240 windows, per-2s PM prices.
        `*_sell_px` is the ASK you pay (the higher of the two labels -- the
        repo measured that using `*_buy_px` invents a +6.65% mirage).
    data/btc5m/data/<date>/<start>/ticks_dense.parquet   per-1s spot OHLCV.
    metadata.json               priceToBeat (the strike) + finalPrice (winner).

TRAPS HANDLED
- The signal is read from the spot CLOSE of second t; the price is paid from the
  NEXT recorded PM row. Using the same row's price is the classic 1 s look-ahead
  that faked a winner on this corpus before.
- One entry per round, first qualifying second (the live rule), then collapse
  before reading the battery.
- Losers are -100%: the per-$1 estimator is leverage-dominated, so the per-SHARE
  edge is printed beside it and the hit rate is the thing to read.

Usage:
    python -m tools.pm_precross_entry --prox 1.0 --mom 10
    python -m tools.pm_precross_entry --sweep
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "btc5m"
PRICES = ROOT / "data" / "btc5m_prices.parquet"


def pm_rows() -> dict[str, pd.DataFrame]:
    """window_id -> per-row PM prices, sorted by seconds elapsed."""
    p = pd.read_parquet(PRICES)
    p = p[["window_id", "sec", "tau_s", "up_sell_px", "dn_sell_px",
           "up_buy_px", "dn_buy_px", "up_px", "dn_px"]]
    return {k: v.sort_values("sec").reset_index(drop=True)
            for k, v in p.groupby("window_id", sort=False)}


def load_round(date: str, start: str):
    """(spot_close, spot_taker_delta, strike, winner) for one window."""
    base = CORPUS / "data" / date / start
    m = json.loads((base / "metadata.json").read_text())
    ev = m.get("events", [{}])[0].get("eventMetadata", {})
    k, fin = ev.get("priceToBeat"), ev.get("finalPrice")
    if k is None or fin is None:
        return None
    t = pd.read_parquet(base / "ticks_dense.parquet",
                        columns=["timestamp", "open", "close",
                                 "taker_buy_volume", "taker_sell_volume"])
    if t.empty:
        return None
    t = t.sort_values("timestamp").reset_index(drop=True)
    return t, float(k), ("Up" if fin >= k else "Down")


def _battery(vals: list[float], days: list[str], label: str) -> None:
    n = len(vals)
    if n < 10:
        print(f"    {label:<22} n={n} (too thin)"); return
    m = float(np.mean(vals)); sd = float(np.std(vals, ddof=0))
    t = m / (sd / math.sqrt(n)) if sd else 0.0
    byd = collections.defaultdict(list)
    for d, v in zip(days, vals):
        byd[d].append(v)
    dm = [float(np.mean(v)) for v in byd.values()]
    md, sdd = float(np.mean(dm)), float(np.std(dm, ddof=0))
    td = md / (sdd / math.sqrt(len(dm))) if sdd else 0.0
    h1 = float(np.mean(vals[:n // 2])); h2 = float(np.mean(vals[n // 2:]))
    keep = dict(byd); worst = min(keep, key=lambda d: float(np.mean(keep[d])))
    keep.pop(worst)
    drop = float(np.mean([v for vs in keep.values() for v in vs]))
    dpos = sum(1 for x in dm if x > 0)
    print(f"    {label:<22} n={n:4d} mean={m:+.4f} t={t:+.2f} day-t={td:+.2f} "
          f"halves={h1:+.3f}/{h2:+.3f} drop-best={drop:+.3f} days+={dpos}/{len(dm)}")


def run(args) -> None:
    prices = pm_rows()
    dates = sorted({w.split("/")[0] for w in prices})
    cells = ([(p, k) for p in args.prox_list for k in args.mom_list]
             if args.sweep else [(args.prox, args.mom)])
    out: dict[tuple, list] = {c: [] for c in cells}
    meta: dict[tuple, list] = {c: [] for c in cells}
    seen = 0
    for wid, pr in prices.items():
        date, start = wid.split("/")
        got = load_round(date, start)
        if got is None:
            continue
        t, K, winner = got
        seen += 1
        # per-second spot state, indexed by second elapsed
        ts = t["timestamp"].to_numpy()
        t0 = int(start)
        sec = ts - t0
        close = t["close"].to_numpy(dtype=float)
        for (prox, mom_s) in cells:
            if mom_s >= len(close):
                continue
            for i in range(mom_s, len(close)):
                tau = 300 - int(sec[i])
                if tau > args.tau_hi or tau < args.tau_lo:
                    continue
                d_bps = 1e4 * (close[i] - K) / K
                if not np.isfinite(d_bps) or abs(d_bps) > prox:
                    continue
                m_bps = 1e4 * (close[i] - close[i - mom_s]) / K
                # the spot must be moving TOWARD the strike
                if d_bps == 0 or m_bps * (1 if d_bps > 0 else -1) >= 0:
                    continue
                side = "Down" if d_bps > 0 else "Up"
                if args.mirror:
                    side = "Up" if side == "Down" else "Down"
                # pay the NEXT recorded PM ask after the signal second
                nxt = pr[pr["sec"] > sec[i]]
                if nxt.empty:
                    break
                row = nxt.iloc[0]
                ask = float(row["dn_sell_px" if side == "Down" else "up_sell_px"])
                ask = ask + args.ask_pad
                if not np.isfinite(ask) or not (args.min_ask <= ask <= args.max_ask):
                    continue
                won = 1.0 if side == winner else 0.0
                fee_sh = 0.07 * ask * (1 - ask)
                shares = 1.0 / ask
                out[(prox, mom_s)].append(shares * (1.0 - fee_sh) - 1.0 if won else -1.0)
                meta[(prox, mom_s)].append((date, ask, won, tau, side, d_bps, m_bps))
                break                     # ONE entry per round
    print(f"\n=== PRE-CROSS ENTRY: buy the side the spot is walking INTO, before "
          f"it crosses ({seen} windows, {len(dates)} days, tau [{args.tau_lo:.0f},"
          f"{args.tau_hi:.0f}]s, ask in [{args.min_ask:.2f},{args.max_ask:.2f}]"
          + (f", MIRROR (opposite side)" if args.mirror else "")
          + (f", ask pad +{args.ask_pad:.2f}" if args.ask_pad else "") + ") ===")
    print(f"{'prox':>5} {'mom':>4} {'n':>5} {'cov':>6} {'ask':>6} {'win%':>6} "
          f"{'EV/$1':>8} {'per-share':>10} {'pooled t':>9} {'day-t':>7} {'halves':>15} {'days+':>7}")
    for c in cells:
        vals = out[c]; ms = meta[c]
        if not vals:
            continue
        prox, mom_s = c
        asks = [m[1] for m in ms]; wins = [m[2] for m in ms]
        days = [m[0] for m in ms]
        n = len(vals)
        m = float(np.mean(vals)); sd = float(np.std(vals, ddof=0))
        t = m / (sd / math.sqrt(n)) if sd else 0.0
        byd = collections.defaultdict(list)
        for d, v in zip(days, vals):
            byd[d].append(v)
        dm = [float(np.mean(v)) for v in byd.values()]
        md, sdd = float(np.mean(dm)), float(np.std(dm, ddof=0))
        td = md / (sdd / math.sqrt(len(dm))) if sdd else 0.0
        h1 = float(np.mean(vals[:n // 2])); h2 = float(np.mean(vals[n // 2:]))
        edge = float(np.mean(wins)) - float(np.mean(asks)) - float(np.mean(
            [0.07 * a * (1 - a) for a in asks]))
        print(f"{prox:>5.2f} {mom_s:>4d} {n:>5d} {n / max(1, seen):>6.1%} "
              f"{np.mean(asks):>6.3f} {np.mean(wins):>6.1%} {m:>+8.4f} {edge:>+10.4f} "
              f"{t:>+9.2f} {td:>+7.2f} {h1:>+7.3f}/{h2:>+7.3f} "
              f"{sum(1 for x in dm if x > 0):>3d}/{len(dm):<3d}")
    print("\n  cov = share of windows that produced an entry | per-share = P(win) - ask - fee"
          " (the estimator that is NOT leverage-dominated)")
    for c in cells:
        if not out[c]:
            continue
        print(f"\n  --- battery, prox {c[0]} / mom {c[1]} ---")
        _battery(out[c], [m[0] for m in meta[c]], "all entries")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prox", type=float, default=1.0, help="|spot-strike| cap, bps")
    ap.add_argument("--mom", type=int, default=10, help="momentum lookback, seconds")
    ap.add_argument("--prox-list", default="0.25,0.5,1.0,2.0,5.0")
    ap.add_argument("--mom-list", default="3,10,30")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    ap.add_argument("--min-ask", type=float, default=0.02)
    ap.add_argument("--max-ask", type=float, default=0.98)
    ap.add_argument("--ask-pad", type=float, default=0.0,
                    help="cents added to the recorded ask (the live venue quotes richer "
                         "than this corpus: measured +0.043 at |d| 1-2 bps)")
    ap.add_argument("--mirror", action="store_true",
                    help="control: buy the OPPOSITE side (must come out negative)")
    a = ap.parse_args()
    a.prox_list = [float(x) for x in a.prox_list.split(",")]
    a.mom_list = [int(x) for x in a.mom_list.split(",")]
    run(a)


if __name__ == "__main__":
    main()
