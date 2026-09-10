"""Which Up logic works best? A head-to-head over the recorded rounds.

Tuning a threshold for the Up side is hopeless: it clears the edge bar in 55
rounds of 670 and is worth ~0 +- 0.24 per $1, so every filter we tested was
fitted to noise. The question that can be answered is a different one -- not
"which threshold" but "which Up LOGIC".

The repository already registers nine pricing models, and the gate can price
the Up side with any of them while the Down side keeps the calibrated digital
model (that is what `settings.up_model` does; tools.backtest_candle_open.replay
routes it through evaluate_hybrid). So the candidates are already written, and
this tool scores them.

Method, and why it is not a repeat of the threshold mistake:
  * every candidate runs through the REAL replay, with `model` = digital for
    Down and `model_up` = the candidate for Up. No part of the gate is
    re-implemented here, so these numbers cannot drift from what live does.
    The default candidate must therefore reproduce the live trade list;
  * a candidate is scored twice, because the two can disagree: as a
    PROBABILITY (Brier over every evaluated tick, clustered by round, against
    the market's own ask as the benchmark) and as MONEY (realised EV per $1 on
    the trades it causes). A model can win the Brier and still lose money;
  * nine candidates judged on one slice is a nine-way selection, so every
    column is shown on a TRAIN slice (first 60% of rounds) and a TEST slice
    (the rest). Only a candidate that holds up on TEST has earned anything.

Usage:
    python -m tools.up_logic_sweep
    python -m tools.up_logic_sweep --models digital momentum sentiment
    python -m tools.up_logic_sweep --train-frac 0.6 --haircut
"""
from __future__ import annotations

import argparse
import collections
import math
import statistics as st
from dataclasses import replace

from btcpredict.config import settings
from btcpredict.models import MODEL_NAMES, build_model
from tools.backtest_candle_open import ReplayParams, replay
from tools.sizing_study import load_inputs


def brier_clustered(ticks: list[dict], key: str) -> tuple[float, int]:
    """Brier over ticks, clustered by round: rounds are the unit of evidence.

    Ticks inside one round are the same question asked repeatedly, so a plain
    average over ticks would report a sample size that is 15x too large.
    """
    per: dict[str, list[float]] = collections.defaultdict(list)
    for t in ticks:
        p = t[key]
        if p is None:
            continue
        per[t["slug"]].append((p - (1.0 if t["outcome_up"] else 0.0)) ** 2)
    if not per:
        return float("nan"), 0
    vals = [st.mean(v) for v in per.values()]
    return st.mean(vals), len(vals)


def trade_stats(trades: list[dict]) -> str:
    n = len(trades)
    if not n:
        return f"{0:>6}{'':>8}{'':>8}{'':>10}{'':>8}{'':>8}"
    rs = [t["pnl"] for t in trades]
    q = st.mean([t["q"] for t in trades])
    wr = 100.0 * sum(1 for t in trades if t["won"]) / n
    ev = st.mean(rs)
    se = st.pstdev(rs) / math.sqrt(n) if n > 1 else 0.0
    return (f"{n:>6}{q:>8.3f}{wr:>7.1f}%{sum(rs):>+10.2f}{ev:>+8.3f}"
            f"{ev / se if se else 0:>+7.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--models", nargs="*", default=list(MODEL_NAMES))
    ap.add_argument("--train-frac", type=float, default=0.60)
    ap.add_argument("--haircut", action="store_true",
                    help="also sweep how much of the model to believe at all")
    args = ap.parse_args()

    params = ReplayParams.from_settings()
    quotes, sbs, sigs, hist = load_inputs(args.market)

    starts = sorted({r["start_ms"] for r in sbs.values()})
    cut = starts[int(len(starts) * args.train_frac)]
    n_train = sum(1 for s in starts if s < cut)
    print(f"{args.market}: {len(sbs)} settled rounds, {len(quotes)} quotes")
    print(f"live gate: min_edge {params.min_edge:.2f}  haircut "
          f"{params.edge_haircut:.2f}  quotes [{params.min_quote:.2f},"
          f"{params.max_quote:.2f}]  persistence {params.signal_persistence}")
    print(f"split: TRAIN = first {n_train} rounds, TEST = last "
          f"{len(starts) - n_train} (cut {cut})")
    print(f"Down side is digital for every candidate; Up side varies\n")

    digital = build_model("digital")
    rows = []
    for name in args.models:
        ticks: list[dict] = []
        res = replay(quotes, sbs, sigs, hist, digital, None, False,
                     params=params, model_up=build_model(name),
                     collected=ticks)
        train = [t for t in ticks if sbs[t["slug"]]["start_ms"] < cut]
        test = [t for t in ticks if sbs[t["slug"]]["start_ms"] >= cut]
        trades = res["trades"]
        up = [t for t in trades if t["side"] == "Up"]
        rows.append({
            "name": name,
            "brier_train": brier_clustered(train, "p_up")[0],
            "brier_test": brier_clustered(test, "p_up")[0],
            "mkt_train": brier_clustered(train, "up_ask")[0],
            "mkt_test": brier_clustered(test, "up_ask")[0],
            "rounds": brier_clustered(ticks, "p_up")[1],
            "trades": trades,
            "test_trades": [t for t in trades
                            if sbs[t["slug"]]["start_ms"] >= cut],
            "up_train": [t for t in up if sbs[t["slug"]]["start_ms"] < cut],
            "up_test": [t for t in up if sbs[t["slug"]]["start_ms"] >= cut],
        })
        print(f"  {name:<12} done: {len(trades)} trades ({len(up)} Up)")

    print("\n=== Up side, as a PROBABILITY (Brier, clustered by round) ===")
    print(f"  {'model':<14}{'train':>9}{'test':>9}{'mkt train':>11}"
          f"{'mkt test':>10}{'rounds':>8}")
    for r in rows:
        print(f"  {r['name']:<14}{r['brier_train']:>9.4f}{r['brier_test']:>9.4f}"
              f"{r['mkt_train']:>11.4f}{r['mkt_test']:>10.4f}"
              f"{r['rounds']:>8}")

    print(f"\n=== Up side, as MONEY (the trades that Up logic causes) ===")
    print(f"  {'model':<14}{'n':>6}{'mean q':>8}{'WR%':>8}{'P&L':>10}"
          f"{'EV/$1':>8}{'t':>7}   (TRAIN)")
    for r in rows:
        print(f"  {r['name']:<14}{trade_stats(r['up_train'])}")
    print(f"  {'model':<14}{'n':>6}{'mean q':>8}{'WR%':>8}{'P&L':>10}"
          f"{'EV/$1':>8}{'t':>7}   (TEST)")
    for r in rows:
        print(f"  {r['name']:<14}{trade_stats(r['up_test'])}")

    print(f"\n=== whole book (Up + Down), the number that is actually paid ===")
    print(f"  {'model':<14}{'n':>6}{'mean q':>8}{'WR%':>8}{'P&L':>10}"
          f"{'EV/$1':>8}{'t':>7}   (all rounds)")
    for r in rows:
        print(f"  {r['name']:<14}{trade_stats(r['trades'])}")
    print(f"  {'model':<14}{'n':>6}{'mean q':>8}{'WR%':>8}{'P&L':>10}"
          f"{'EV/$1':>8}{'t':>7}   (TEST only)")
    for r in rows:
        print(f"  {r['name']:<14}{trade_stats(r['test_trades'])}")

    if not args.haircut:
        return
    print(f"\n=== how much of the model to believe at all (both sides) ===")
    print(f"  {'haircut':<14}{'n':>6}{'mean q':>8}{'WR%':>8}{'P&L':>10}"
          f"{'EV/$1':>8}{'t':>7}")
    for h in (0.0, 0.25, 0.50, 0.70, 0.85, 1.00):
        res = replay(quotes, sbs, sigs, hist, digital, None, False,
                     params=replace(params, edge_haircut=h),
                     model_up=build_model(settings.up_model))
        print(f"  {h:<14.2f}{trade_stats(res['trades'])}")


if __name__ == "__main__":
    main()
