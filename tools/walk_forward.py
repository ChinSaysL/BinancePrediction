"""Walk-forward evaluation: choose on the first 60%, score on the unseen 40%.

Every parameter we picked today was chosen on the same window we then quoted
PnL from, which is how a 35-hour recording turns into a +$30 "result". This
tool makes the gap visible: it tunes a grid on the TRAIN slice, then reports
what that exact configuration did on the TEST slice it never saw -- next to the
same numbers for the configuration actually running live.

Usage:
    python -m tools.walk_forward                     # min_quote x max_tau grid
    python -m tools.walk_forward --train-frac 0.6
"""
from __future__ import annotations

import argparse
import itertools
from dataclasses import replace

from btcpredict.config import settings
from btcpredict.models import build_model
from btcpredict.store.db import Store
from tools.backtest_candle_open import (ReplayParams, build_1m_history, replay,
                                        rolling_sigma)


def stats(trades: list[dict]) -> dict:
    cum = peak = mdd = 0.0
    for t in sorted(trades, key=lambda x: x["ts_ms"]):
        cum += t["pnl"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    wins = sum(1 for t in trades if t["won"])
    return {"n": len(trades), "wr": wins / len(trades) if trades else 0.0,
            "pnl": cum, "mdd": mdd,
            "ratio": (cum / abs(mdd)) if mdd else float("inf")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--sweep", choices=["grid", "moneyness"], default="grid",
                    help="grid = min_quote x max_tau; moneyness = signal floor")
    args = ap.parse_args()

    store = Store(path=settings.db_path)
    like = args.market + "%"
    settled = store.query(
        "SELECT slug, strike, end_price, winner, start_ms FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL AND end_price IS NOT NULL "
        "AND slug LIKE ? ORDER BY start_ms", (like,))
    quotes = store.query("""
        SELECT q.* FROM quotes q JOIN rounds r ON r.slug = q.slug
        WHERE r.winner IS NOT NULL AND q.slug LIKE ? ORDER BY q.ts_ms""", (like,))
    sbs = {r["slug"]: r for r in settled}
    # sigma uses only rounds BEFORE each one, so computing it on the full list
    # is still out-of-sample; the split happens at the quote level.
    sigs = {r["slug"]: rolling_sigma(settled, i) for i, r in enumerate(settled)}
    hist = build_1m_history(quotes, store=store, symbol="BTCUSDT")
    digital, drift = build_model("digital"), build_model(settings.up_model)

    times = [q["ts_ms"] for q in quotes]
    split = times[0] + (times[-1] - times[0]) * args.train_frac
    train_q = [q for q in quotes if q["ts_ms"] < split]
    test_q = [q for q in quotes if q["ts_ms"] >= split]
    print(f"{len(settled)} rounds, {len(quotes)} quotes; "
          f"train <= {int(args.train_frac*100)}% ({len(train_q)} quotes), "
          f"test ({len(test_q)} quotes)")

    base = ReplayParams.from_settings()
    live = replace(base)

    if args.sweep == "moneyness":
        # The live cell, with a signal-strength floor swept on top. The floor is
        # meant to be a QUALITY knob: fewer trades, better per-trade economics.
        # Per-trade return is the headline (PnL scales with the stake we set, so
        # n alone does not decide it).
        def fmt_pt(s):
            pt = (s["pnl"] / s["n"]) if s["n"] else 0.0
            return (f"n={s['n']:>3} WR={s['wr']:.3f} pnl={s['pnl']:+7.2f} "
                    f"mdd={s['mdd']:+6.2f} per-trade={pt:+.3f}")

        print(f"\n{'min|z|':>7} | {'TRAIN':<46} | TEST")
        rows = []
        for mz in (0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5):
            params = replace(base, min_moneyness_sd=mz)
            tr = stats(replay(train_q, sbs, sigs, hist, digital, None, False,
                              params=params, model_up=drift)["trades"])
            te = stats(replay(test_q, sbs, sigs, hist, digital, None, False,
                              params=params, model_up=drift)["trades"])
            rows.append((mz, tr, te))
            print(f"{mz:>7.2f} | {fmt_pt(tr):<46} | {fmt_pt(te)}")

        def per_trade(s):
            return s["pnl"] / s["n"] if s["n"] else 0.0

        print("\n-- what the moneyness sweep says --")
        best = max(rows, key=lambda r: per_trade(r[1]))
        print(f"  best per-trade on TRAIN: min|z| {best[0]:.2f}  "
              f"train {per_trade(best[1]):+.3f}/trade (n={best[1]['n']}) -> "
              f"TEST {per_trade(best[2]):+.3f}/trade (n={best[2]['n']})")
        live_row = next(r for r in rows if r[0] == 0.0)
        print(f"  running live (min|z| 0.00): train "
              f"{per_trade(live_row[1]):+.3f}/trade (n={live_row[1]['n']}) -> "
              f"TEST {per_trade(live_row[2]):+.3f}/trade (n={live_row[2]['n']})")
        import statistics as st2
        xs = [per_trade(r[1]) for r in rows]
        ys = [per_trade(r[2]) for r in rows]
        mx, my = st2.mean(xs), st2.mean(ys)
        cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
        print(f"  train/test correlation of per-trade return across "
              f"{len(rows)} floors: {cov/den if den else 0:+.2f}")
        print(f"  total PnL (train -> test): "
              f"{live_row[1]['pnl']:+.2f} -> {live_row[2]['pnl']:+.2f} live  |  "
              f"{best[1]['pnl']:+.2f} -> {best[2]['pnl']:+.2f} at the best floor")
        return

    grid = {
        "min_quote": [0.15, 0.20, 0.25, 0.30],
        "max_tau_s": [150.0, 180.0, 210.0, 240.0],
    }
    keys = list(grid)
    cells = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = replace(base, **dict(zip(keys, combo)))
        tr = stats(replay(train_q, sbs, sigs, hist, digital, None, False,
                          params=params, model_up=drift)["trades"])
        te = stats(replay(test_q, sbs, sigs, hist, digital, None, False,
                          params=params, model_up=drift)["trades"])
        cells.append({**dict(zip(keys, combo)), "train": tr, "test": te})

    def fmt(s):
        return (f"n={s['n']:>3} WR={s['wr']:.3f} pnl={s['pnl']:+7.2f} "
                f"mdd={s['mdd']:+6.2f}")

    print(f"\n{'min_q':>6} {'tau':>5} | {'TRAIN':<42} | TEST")
    for c in cells:
        print(f"{c['min_quote']:>6.2f} {c['max_tau_s']:>5.0f} | "
              f"{fmt(c['train']):<42} | {fmt(c['test'])}")

    best = max(cells, key=lambda c: c["train"]["pnl"])
    best_ratio = max(cells, key=lambda c: c["train"]["ratio"])
    live_cell = next(c for c in cells
                     if c["min_quote"] == live.min_quote
                     and c["max_tau_s"] == live.max_tau_s)
    print("\n-- what the walk-forward actually says --")
    print(f"  best on TRAIN  ({best['min_quote']:.2f}/{best['max_tau_s']:.0f}): "
          f"train {fmt(best['train'])} -> TEST {fmt(best['test'])}")
    print(f"  best risk-adj  ({best_ratio['min_quote']:.2f}/{best_ratio['max_tau_s']:.0f}): "
          f"train {fmt(best_ratio['train'])} -> TEST {fmt(best_ratio['test'])}")
    print(f"  running live   ({live_cell['min_quote']:.2f}/{live_cell['max_tau_s']:.0f}): "
          f"train {fmt(live_cell['train'])} -> TEST {fmt(live_cell['test'])}")
    optimism = best["train"]["pnl"] - best["test"]["pnl"]
    print(f"  in-sample optimism of the train maximum: ${optimism:+.2f} per $1 "
          f"staked ({best['train']['pnl']:+.2f} -> {best['test']['pnl']:+.2f})")

    import statistics as st
    xs = [c["train"]["pnl"] for c in cells]
    ys = [c["test"]["pnl"] for c in cells]
    mx, my = st.mean(xs), st.mean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    denom = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    print(f"  train/test correlation across the {len(cells)} cells: "
          f"{cov/denom if denom else 0:+.2f}  (1.0 = tuning transfers, "
          f"0.0 = it is noise)")

    print(f"\ntop {args.top} cells by train PnL, with their test outcome:")
    for c in sorted(cells, key=lambda c: -c["train"]["pnl"])[:args.top]:
        print(f"  {c['min_quote']:.2f}/{c['max_tau_s']:>3.0f}  "
              f"train pnl={c['train']['pnl']:+7.2f}  ->  "
              f"test pnl={c['test']['pnl']:+7.2f}  (n={c['test']['n']})")


if __name__ == "__main__":
    main()
