"""Head-to-head evaluation of every pricing model over Binance kline history.

Same methodology as backtest_calibration.py -- Binance 1m klines stand in for
the oracle, 5-minute rounds are reconstructed, and the decision point is each
minute mark -- but run for ALL models at once, with the original digital model
as the baseline. As there, this tests the *model* half only: historical market
quotes are not retrievable, so the edge gate is scored separately from recorded
data.

A model may decline to emit a view on a round (sniper does this by design, and
every ported strategy holds when its conditions are absent). The report shows
each model's emit rate alongside its score, so a model that wins by never
trading is obvious.

Usage:  python -m tools.backtest_models --days 10
        python -m tools.backtest_models --days 10 --models momentum phantom
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

import aiohttp

from btcpredict.calibration.report import reliability_table, score
from btcpredict.models import MODEL_NAMES, ModelContext, build_model
from tools.backtest_calibration import build_rounds, load_1m, rolling_sigma

ROUND_S = 300


def run_models(rounds: list[list[list]], models: dict) -> dict[str, dict]:
    """Collect (pred, outcome) pairs per model and per minute mark.

    returns {model_name: {"all": [(p, y), ...], 1: [...], ...}} keyed by mark.
    """
    per_model: dict[str, dict] = {name: defaultdict(list) for name in models}
    seen: list[list] = []   # 1m bars of all previous rounds
    for i, r in enumerate(rounds):
        for mark in (1, 2, 3, 4):
            tau = (5 - mark) * 60.0
            K = float(r[0][1])
            S = float(r[mark - 1][4])
            C = float(r[4][4])
            y = 1 if C > K else 0

            closes = [float(b[4]) for b in seen] + [float(b[4]) for b in r[:mark]]
            volumes = [float(b[5]) for b in seen] + [float(b[5]) for b in r[:mark]]
            takers = [float(b[9]) for b in seen] + [float(b[9]) for b in r[:mark]]
            sigma = rolling_sigma(rounds, i)
            ctx = ModelContext(closes, volumes, takers,
                               price=S, strike=K, tau_s=tau, sigma_1m=sigma)
            for name, model in models.items():
                pred = model.predict(ctx)
                if pred.emitted and 0.0 < pred.p_up < 1.0:
                    per_model[name]["all"].append((pred.p_up, y))
                    per_model[name][mark].append((pred.p_up, y))
        seen.extend(r)
    return per_model


def print_mark(name: str, rows: list[tuple[float, int]], total_rounds: int) -> None:
    preds, outs = zip(*rows) if rows else ((), ())
    if not preds:
        print(f"  {name:<10} never emitted")
        return
    sc = score(list(preds), list(outs))
    emit = len(rows) / total_rounds
    print(f"  {name:<10} n={len(rows):4d} emit={emit:5.0%}  {sc}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=10.0)
    ap.add_argument("--models", nargs="*", default=None,
                    help=f"subset of {MODEL_NAMES}")
    args = ap.parse_args()

    names = list(MODEL_NAMES) if not args.models else args.models
    unknown = [m for m in names if m not in MODEL_NAMES]
    if unknown:
        raise SystemExit(f"unknown model(s) {unknown}; choose from {MODEL_NAMES}")

    async with aiohttp.ClientSession() as s:
        bars = await load_1m(s, args.days)
    rounds = build_rounds(bars)
    print(f"loaded {len(bars)} 1m bars -> {len(rounds)} complete 5m rounds "
          f"({len(rounds) * 5 / 60 / 24:.1f} days)\n")

    base = sum(1 for r in rounds if float(r[4][4]) > float(r[0][1])) / len(rounds)
    print(f"base rate (Up): {base:.4f}\n")

    models = {n: build_model(n) for n in names}
    per_model = run_models(rounds, models)

    for mark in (1, 2, 3, 4):
        tau = (5 - mark) * 60.0
        print(f"--- decision at t={mark}min ({int(tau)}s left) ---")
        for name in names:
            print_mark(name, per_model[name][mark], len(rounds))
        print()

    print("--- aggregate across marks ---")
    for name in names:
        print_mark(name, per_model[name]["all"], len(rounds) * 4)

    digital_rows = per_model.get("digital", {}).get("all", [])
    if digital_rows and args.models is None:
        preds, outs = zip(*digital_rows)
        print("\nbaseline (digital) reliability:")
        print(reliability_table(list(preds), list(outs), min_n=20))


if __name__ == "__main__":
    asyncio.run(main())
