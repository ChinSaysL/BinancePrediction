"""Does trading only large dislocations beat trading every round?

Grid-searching thresholds on ~25 rounds and shipping the best cell is exactly
the overfitting that already produced a false result today (the sigma
multiplier). So this checks the *hypothesis* rather than hunting for a number:

  * a real effect shows per-trade P&L rising monotonically with the edge
    threshold, because bigger mispricings should pay better;
  * noise shows cells jumping around with no trend.

Each threshold is bootstrapped over ROUNDS, and the Spearman correlation between
threshold and per-trade P&L is reported as the trend test.
"""
from __future__ import annotations

import collections
import random
import statistics as st

from btcpredict.config import settings
from btcpredict.edge.signal import FeeModel, evaluate
from btcpredict.pricing.digital import fair_value
from btcpredict.store.db import Store

FEE = 0.0064


def load():
    s = Store(settings.db_path)
    rows = s.query("""
        SELECT q.slug,q.tau_s,q.spot,q.up_ask,q.down_ask,r.strike,r.winner
          FROM quotes q JOIN rounds r ON r.slug=q.slug
         WHERE r.winner IS NOT NULL AND q.up_ask IS NOT NULL
               AND q.down_ask IS NOT NULL AND q.spot IS NOT NULL
               AND q.tau_s>0 AND r.strike IS NOT NULL
      ORDER BY q.ts_ms""")
    sigma = st.median([d["sigma_1m"] for d in s.query(
        "SELECT sigma_1m FROM decisions WHERE sigma_1m IS NOT NULL")] or [0.0005])
    data = collections.defaultdict(list)
    for r in rows:
        if not (settings.min_tau_s <= r["tau_s"] <= settings.max_tau_s):
            continue
        data[r["slug"]].append({
            "fair_up": fair_value(r["spot"], r["strike"], r["tau_s"], sigma),
            "up_ask": r["up_ask"], "down_ask": r["down_ask"], "winner": r["winner"]})
    return data, sigma


def run(data, slugs, min_edge):
    pnls = []
    for sl in slugs:
        for r in data[sl]:
            sig = evaluate(r["fair_up"], r["up_ask"], r["down_ask"], fee=FEE,
                           half_spread=0.0, safety_margin=settings.safety_margin,
                           fee_model=FeeModel.NOTIONAL,
                           max_quote=settings.max_quote, min_edge=min_edge)
            if not sig.act:
                continue
            won = (sig.side == "Up") == (r["winner"] == "Up")
            pnls.append((1 / sig.quote - 1 - FEE) if won else -(1 + FEE))
            break
    return pnls


def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def main() -> None:
    data, sigma = load()
    slugs = list(data)
    print(f"{len(slugs)} rounds, sigma={sigma:.6f}, fee={FEE*100:.2f}%\n")

    # how big are the available edges at all?
    all_edges = []
    for sl in slugs:
        best = 0.0
        for r in data[sl]:
            for side, p, q in (("Up", r["fair_up"], r["up_ask"]),
                               ("Down", 1 - r["fair_up"], r["down_ask"])):
                best = max(best, p - q)
        all_edges.append(best)
    all_edges.sort()
    print("best available edge per round (distribution):")
    for pct in (10, 25, 50, 75, 90, 100):
        i = min(len(all_edges) - 1, int(len(all_edges) * pct / 100) - 0 if pct < 100 else -1)
        print(f"   p{pct:<3}: {all_edges[i]:+.3f}")
    print()

    random.seed(17)
    grid = [0.02, 0.04, 0.06, 0.08, 0.10, 0.14, 0.20]
    means, kept = [], []
    print(f"{'min_edge':>9} {'trades':>7} {'win%':>6} {'per trade':>11} "
          f"{'95% CI':>22} {'P(>0)':>7}")
    for me in grid:
        pnls = run(data, slugs, me)
        if len(pnls) < 3:
            print(f"{me:>9.2f} {len(pnls):>7} {'-':>6} {'too few trades':>11}")
            means.append(None); kept.append(me); continue
        m = st.mean(pnls)
        boots = []
        for _ in range(4000):
            smp = [slugs[random.randrange(len(slugs))] for _ in range(len(slugs))]
            b = run(data, smp, me)
            boots.append(st.mean(b) if b else 0.0)
        boots.sort()
        lo, hi = boots[100], boots[3899]
        p = sum(1 for x in boots if x > 0) / len(boots)
        wr = sum(1 for x in pnls if x > 0) / len(pnls)
        print(f"{me:>9.2f} {len(pnls):>7} {wr*100:>5.0f}% {m:>+11.4f} "
              f"[{lo:>+8.4f},{hi:>+8.4f}] {p:>7.3f}")
        means.append(m); kept.append(me)

    pairs = [(k, m) for k, m in zip(kept, means) if m is not None]
    if len(pairs) >= 4:
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        print(f"\nTREND TEST (does P&L rise with edge threshold?)")
        print(f"  Spearman rho = {rho:+.3f}")
        if rho > 0.6:
            print("  -> consistent monotone improvement: supports the hypothesis")
        elif rho > 0.2:
            print("  -> weak positive trend; not convincing at this sample size")
        else:
            print("  -> NO trend. Cells vary randomly, which is what noise looks like.")


if __name__ == "__main__":
    main()
