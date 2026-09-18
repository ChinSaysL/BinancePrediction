"""Does the Polymarket book pay for itself? Calibration by tau and price.

THE ONLY QUESTION THIS ANSWERS, and it is the right first question. You pay the
ask, you receive $1 if the side you bought wins, so

    EV per $1 = win_rate - (price + fee)

A book that is correctly priced has `win_rate == price`, i.e. EV = -fee, for
EVERY price and EVERY tau. Any cell where the win rate beats price+fee is edge;
every other cell is the fee. So the whole edge question reduces to a table, and
the table needs no model, no spot, no strike -- just the book and who won.

WHAT THIS DATA CANNOT DO: the tape has no BTC spot and no strike, so a
"spot crossed the strike" trigger (the cross arm) is NOT testable here. This
measures the book's own calibration, which is the precondition for any trigger
that reads it.

METHOD NOTES THAT MATTER
- ONE ENTRY PER ROUND. Sampling every snapshot would inflate n by ~100x and
  hand back the row-level significance that collapses when you collapse the
  sample (a documented trap in this repo). One entry per episode.
- The favourite is bought at the ASK and pays the TAKER fee
  (`0.07 * p * (1-p)` per share, the venue's own `crypto_fees_v2` params, which
  reproduce both the 1.0c at 0.82 and 1.75c at 0.50 that were measured).
- The winner comes from the tape's tail only, never from a mid-round price.
- Fee is charged on the side actually taken, at the price actually paid.

Usage:
    python tools/pm_backtest.py btc 5m --taus 30,60,120,180,240
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from collections import defaultdict

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tools.pm_tape import episode_winner, episodes, load  # noqa: E402

#: `crypto_fees_v2`: rate 0.07, exponent 1, takerOnly. Fee per SHARE.
FEE_RATE = 0.07


def taker_fee(price: float) -> float:
    """Fee per share at a fill price. Reproduces the venue's own schedule."""
    return FEE_RATE * price * (1.0 - price)


def snapshot_at(df: pd.DataFrame, tau_s: float, tol_s: float = 1.5):
    """The row nearest `tau_s`. Returns (yes_ask, no_ask) or None."""
    sel = df[(df["tau_s"] - tau_s).abs() <= tol_s]
    if sel.empty:
        return None
    for _, r in sel.iterrows():
        if pd.notna(r["yes_ask"]) and pd.notna(r["no_ask"]):
            return float(r["yes_ask"]), float(r["no_ask"])
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("asset", nargs="?", default="btc")
    ap.add_argument("interval", nargs="?", default="5m")
    ap.add_argument("--taus", default="30,60,120,180,240")
    args = ap.parse_args()
    taus = [float(x) for x in args.taus.split(",")]

    paths = episodes(args.asset, args.interval)
    print("Polymarket tape: %s %s -- %d episodes" % (args.asset, args.interval, len(paths)))

    # tau -> list of (price_paid, won)
    rows: dict[float, list[tuple[float, bool]]] = defaultdict(list)
    skipped = {"no_winner": 0, "no_book": 0}

    for p in paths:
        df = load(p)
        if df.empty:
            continue
        winner = episode_winner(df)
        if winner is None:
            skipped["no_winner"] += 1
            continue
        for tau in taus:
            sp = snapshot_at(df, tau)
            if sp is None:
                skipped["no_book"] += 1
                continue
            yes_ask, no_ask = sp
            # buy the FAVOURITE: the side whose ask is above 0.5
            if yes_ask >= no_ask:
                price, side = yes_ask, "YES"
            else:
                price, side = no_ask, "NO"
            if not (0.0 < price < 1.0):
                continue
            rows[tau].append((price, side == winner))

    print("skipped: %s" % skipped)
    print()
    print("=== buy-the-favourite at the ask, taker fee, ONE entry per round ===")
    print("  tau   n     share   mean_px  win%%    breakeven%%  EV/$1     note")
    for tau in taus:
        r = rows.get(tau, [])
        if not r:
            continue
        n = len(r)
        px = sum(x[0] for x in r) / n
        wr = sum(1 for x in r if x[1]) / n
        fee = sum(taker_fee(x[0]) for x in r) / n
        ev = sum((1.0 - x[0] - taker_fee(x[0])) if x[1] else (-x[0] - taker_fee(x[0]))
                 for x in r) / n
        # share of entries that are effectively a coin flip
        fav = sum(1 for x in r if 0.4 <= x[0] <= 0.6) / n
        print("  %4.0f %4d   %5.2f   %6.3f   %5.1f    %6.1f      %+.4f   %.0f%% near 0.5"
              % (tau, n, wr, px, 100 * wr, 100 * (px + fee), ev, 100 * fav))

    print()
    print("=== the same, split by price band (tau pooled) ===")
    bands = [(0.0, 0.30), (0.30, 0.45), (0.45, 0.55), (0.55, 0.70), (0.70, 0.85), (0.85, 1.0)]
    pooled = [x for tau in taus for x in rows.get(tau, [])]
    print("  band        n     win%%    mean_px  breakeven%%  EV/$1")
    for lo, hi in bands:
        sub = [(p, w) for p, w in pooled if lo <= p < hi]
        if len(sub) < 5:
            continue
        n = len(sub)
        px = sum(x[0] for x in sub) / n
        wr = sum(1 for x in sub if x[1]) / n
        fee = sum(taker_fee(x[0]) for x in sub) / n
        ev = sum((1.0 - x[0] - taker_fee(x[0])) if x[1] else (-x[0] - taker_fee(x[0]))
                 for x in sub) / n
        print("  %.2f-%.2f  %4d   %5.1f   %6.3f   %6.1f      %+.4f"
              % (lo, hi, n, 100 * wr, px, 100 * (px + fee), ev))
    return 0


if __name__ == "__main__":
    sys.exit(main())
