"""The only test that matters: does the TRAINED model beat the market's price?

Training showed a small but significant gain over the closed form (BTC +0.00128,
ETH +0.00218 Brier). That is a better predictor of reality -- it says nothing
about beating a market that is itself well calibrated.

Here the model is fit on historical klines (strictly before the recorded
rounds), then scored against the real asks captured by the recorder, paired
within round so round-level variance does not swamp a small effect.
"""
from __future__ import annotations

import asyncio, collections, math, statistics as st
import numpy as np

from btcpredict.config import settings
from btcpredict.calibration.report import score
from btcpredict.pricing.digital import fair_value
from btcpredict.store.db import Store
from tools.train_model import build, fit_logistic, load_1m, predict


def binom_p(k, n):
    tail = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1))
    return min(1.0, 2 * tail / 2 ** n)


async def main(symbol="BTCUSDT", prefix="btc-updown-5m", days=365, mark=3):
    arr = await load_1m(symbol, days, 10)
    X, y, base = build(arr, mark)
    cut = int(len(X) * 0.7)
    mu, sd = X[:cut].mean(0), X[:cut].std(0) + 1e-9
    w = fit_logistic((X[:cut] - mu) / sd, y[:cut], l2=1.0)
    print(f"trained on {cut:,} rounds of {symbol} history\n")

    s = Store(settings.db_path)
    rows = s.query("""SELECT q.slug,q.tau_s,q.spot,q.up_ask,r.strike,r.winner
      FROM quotes q JOIN rounds r ON r.slug=q.slug
      WHERE r.winner IS NOT NULL AND q.up_ask IS NOT NULL AND q.spot IS NOT NULL
        AND q.tau_s>0 AND r.strike IS NOT NULL AND q.slug LIKE ?""", (prefix + "%",))
    sigma = st.median([d["sigma_1m"] for d in s.query(
        "SELECT sigma_1m FROM decisions WHERE sigma_1m IS NOT NULL")] or [0.0005])
    if not rows:
        print("no recorded rounds for", prefix); return

    # Features for a live quote need the same shape the trainer used. Only the
    # two moneyness terms are available per-quote; the rest are set to their
    # training means, so the trained blend of z / sqrt|z| is what is tested.
    for lo, hi, label in ((0, 300, "all"), (60, 150, "60-150s"), (0, 60, "0-60s")):
        by = collections.defaultdict(lambda: [[], [], []])
        for r in rows:
            if not (lo < r["tau_s"] <= hi):
                continue
            tau = r["tau_s"]
            z = math.log(r["spot"] / r["strike"]) / (sigma * math.sqrt(tau / 60.0))
            feat = mu.copy()
            feat[0] = z
            feat[1] = math.copysign(math.sqrt(abs(z)), z)
            p_tr = float(predict(w, ((feat - mu) / sd)[None, :])[0])
            p_bs = fair_value(r["spot"], r["strike"], tau, sigma)
            yv = 1 if r["winner"] == "Up" else 0
            d = by[r["slug"]]
            d[0].append((p_tr - yv) ** 2)
            d[1].append((r["up_ask"] - yv) ** 2)
            d[2].append((p_bs - yv) ** 2)
        diffs = [(st.mean(q) - st.mean(m), st.mean(q) - st.mean(b))
                 for m, q, b in by.values() if m]
        n = len(diffs)
        if n < 8:
            continue
        wins = sum(1 for d, _ in diffs if d > 0)
        wins_b = sum(1 for _, d in diffs if d > 0)
        print(f"--- {label} (n={n} rounds) ---")
        print(f"  TRAINED beat market in {wins}/{n} ({wins/n:.0%})  p={binom_p(wins,n):.4f}"
              f"   mean diff {st.mean([d for d,_ in diffs]):+.5f}")
        print(f"  baseline beat market in {wins_b}/{n} ({wins_b/n:.0%})"
              f"   mean diff {st.mean([d for _,d in diffs]):+.5f}")


if __name__ == "__main__":
    asyncio.run(main())
