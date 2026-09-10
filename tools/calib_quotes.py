"""Decide the P(Up) bias on a sample big enough to actually decide it.

The acted-trade sample is ~70 trades, which cannot resolve a 20-point bias. But
the question "is our P(Up) too high?" does not need trades at all -- it needs the
model's probability on every settled round and that round's outcome. The replay
evaluates thousands of such ticks; this scores every one of them, with errors
clustered by round (ticks inside a round are not independent).

Answers three things:
  * the bias of the driftless model and of the live drift model, with a real
    standard error;
  * whether the bias is a level error (P(Up) shifted) or a slope error
    (over-confident), by reporting it per probability band;
  * the same at the four candle opens the bot actually trades.

Usage:  python -m tools.calib_quotes
"""
from __future__ import annotations

import collections
import statistics as st

from btcpredict.config import settings
from btcpredict.models import build_model
from btcpredict.models.drift_digital import DriftDigitalModel
from btcpredict.store.db import Store
from tools.backtest_candle_open import (ReplayParams, build_1m_history, replay,
                                        rolling_sigma)

BANDS = ((0.00, 0.05), (0.05, 0.25), (0.25, 0.50), (0.50, 0.75),
         (0.75, 0.95), (0.95, 1.01))


def load(market: str):
    store = Store(path=settings.db_path)
    like = market + "%"
    settled = store.query(
        "SELECT slug, strike, end_price, winner, start_ms FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL AND end_price IS NOT NULL "
        "AND slug LIKE ? ORDER BY start_ms", (like,))
    quotes = store.query("""
        SELECT q.* FROM quotes q JOIN rounds r ON r.slug = q.slug
        WHERE r.winner IS NOT NULL AND q.slug LIKE ? ORDER BY q.ts_ms""", (like,))
    sbs = {r["slug"]: r for r in settled}
    sigs = {r["slug"]: rolling_sigma(settled, i) for i, r in enumerate(settled)}
    hist = build_1m_history(quotes, store=store, symbol="BTCUSDT")
    store.close()
    return quotes, sbs, sigs, hist


def clustered(ticks) -> dict:
    """Bias with the error computed ACROSS ROUNDS, not across ticks.

    One row per round (that round's mean claim and its realised 0/1), so a round
    contributing 40 ticks cannot pretend to be 40 independent observations.
    """
    by_round = collections.defaultdict(list)
    for t in ticks:
        by_round[t["slug"]].append(t)
    diffs, claims, reals = [], [], []
    for slug, ts in by_round.items():
        c = st.mean(t["p_up"] for t in ts)
        r = 1.0 if ts[0]["outcome_up"] else 0.0
        claims.append(c)
        reals.append(r)
        diffs.append(r - c)
    m = len(diffs)
    if m < 2:
        return {"m": m, "claim": st.mean(claims) if m else 0.0,
                "real": st.mean(reals) if m else 0.0,
                "gap": 0.0, "se": float("inf")}
    return {"m": m, "ticks": len(ticks), "claim": st.mean(claims),
            "real": st.mean(reals), "gap": st.mean(diffs),
            "se": st.pstdev(diffs) / m ** 0.5}


def show(label, ticks, bands=False):
    if not ticks:
        print(f"  {label:<34} no ticks")
        return
    r = clustered(ticks)
    print(f"  {label:<34} rounds={r['m']:<5} ticks={len(ticks):<6} "
          f"claim={r['claim']:.3f} real={r['real']:.3f} "
          f"gap={r['gap']:+.3f} +-{r['se']:.3f} "
          f"({abs(r['gap']) / r['se']:.1f} sigma)")
    if bands:
        for lo, hi in BANDS:
            sub = [t for t in ticks if lo <= t["p_up"] < hi]
            if not sub:
                continue
            rr = clustered(sub)
            if rr["m"] < 5:
                continue
            print(f"      P(Up) {lo:.2f}-{hi:.2f}  rounds={rr['m']:<5} "
                  f"claim={rr['claim']:.3f} real={rr['real']:.3f} "
                  f"gap={rr['gap']:+.3f} ({abs(rr['gap']) / rr['se']:.1f}s)")


def main() -> None:
    for market, symbol in (("btc-updown-5m", "BTCUSDT"),):
        quotes, sbs, sigs, hist = load(market)
        print(f"\n=== {market}: {len(sbs)} settled rounds ===")
        if not sbs:
            continue
        base_up = sum(1 for r in sbs.values() if r["winner"] == "Up") / len(sbs)
        print(f"  Up base rate in the sample: {base_up:.3f}")

        digital = build_model("digital")
        for label, model_up in (("driftless (digital)", None),
                                ("live drift model (scale 0.5)", DriftDigitalModel()),
                                ("drift model (scale 1.5)", DriftDigitalModel({"drift_scale": 1.5}))):
            ticks: list[dict] = []
            replay(quotes, sbs, sigs, hist, digital, None, False,
                   params=ReplayParams.from_settings(), model_up=model_up,
                   collected=ticks)
            print(f"\n  --- {label} ---")
            show("all evaluated ticks", ticks)
            show("tradeable band (0.5-0.95)",
                 [t for t in ticks if 0.5 <= t["p_up"] < 0.95])
            show("candle opens only (tau 60-240 by 60)",
                 [t for t in ticks if round(t["tau"] / 60.0) in
                  ReplayParams.from_settings().trade_candle_opens])
            show("calibration by band", ticks, bands=True)


if __name__ == "__main__":
    main()
