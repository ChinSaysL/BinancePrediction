"""What to expect from running live right now.

Replays the current gate over every recorded quote that has a real ask price and
a known outcome, then bootstraps by ROUND (quotes inside a round share an
outcome, so resampling quotes would overstate confidence).

This is an optimistic estimate in two ways worth stating plainly:
  * the same 25 rounds shaped the max_quote / min_edge thresholds, so it is not
    fully out of sample;
  * fills are assumed at the displayed ask, ignoring queue position and any
    movement between decision and execution.
"""
from __future__ import annotations

import collections
import random
import statistics as st

from btcpredict.config import settings
from btcpredict.edge.signal import FeeModel, evaluate, fee_rate_for_price
from btcpredict.pricing.digital import fair_value
from btcpredict.store.db import Store

# Fee is price-dependent: 180 bps * min(1, (1-px)/px), measured over 61 live
# quotes. A flat rate understated the cost of exactly the cheap contracts the
# gate takes most.
STAKE = 1.0


def simulate(rounds_data, slugs):
    """One trade per round, first qualifying signal -- matches max_open_rounds=1.

    Mirrors the live gate, including the persistence requirement: a signal must
    hold for `signal_persistence` consecutive polls before it is acted on.
    """
    pnls, wins, prices = [], 0, []
    for sl in slugs:
        streak_side, streak = None, 0
        for r in rounds_data[sl]:
            sig = evaluate(r["fair_up"], r["up_ask"], r["down_ask"], fee=None,
                           half_spread=0.0, safety_margin=settings.safety_margin,
                           fee_model=FeeModel.NOTIONAL,
                           max_quote=settings.max_quote,
                           min_quote=settings.min_quote, min_edge=settings.min_edge)
            if sig.act and sig.side == streak_side:
                streak += 1
            elif sig.act:
                streak_side, streak = sig.side, 1
            else:
                streak_side, streak = None, 0
            if not sig.act or streak < settings.signal_persistence:
                continue
            won = (sig.side == "Up") == (r["winner"] == "Up")
            f = fee_rate_for_price(sig.quote)
            pnl = (STAKE / sig.quote - STAKE - STAKE * f) if won else -(STAKE + STAKE * f)
            pnls.append(pnl); wins += won; prices.append(sig.quote)
            break
    return pnls, wins, prices


def main() -> None:
    s = Store(settings.db_path)
    rows = s.query("""
        SELECT q.slug, q.tau_s, q.spot, q.up_ask, q.down_ask, r.strike, r.winner
          FROM quotes q JOIN rounds r ON r.slug = q.slug
         WHERE r.winner IS NOT NULL AND q.up_ask IS NOT NULL AND q.spot IS NOT NULL
               AND q.tau_s > 0 AND r.strike IS NOT NULL
      ORDER BY q.ts_ms
    """)
    sigma = st.median([d["sigma_1m"] for d in s.query(
        "SELECT sigma_1m FROM decisions WHERE sigma_1m IS NOT NULL")] or [0.0005])

    data = collections.defaultdict(list)
    for r in rows:
        if not (settings.min_tau_s <= r["tau_s"] <= settings.max_tau_s):
            continue
        data[r["slug"]].append({
            "fair_up": fair_value(r["spot"], r["strike"], r["tau_s"], sigma),
            "up_ask": r["up_ask"], "down_ask": r["down_ask"], "winner": r["winner"]})
    slugs = [k for k in data if data[k]]
    print(f"replaying {len(slugs)} rounds, sigma={sigma:.6f}, price-dependent fee\n")

    pnls, wins, prices = simulate(data, slugs)
    n = len(pnls)
    if not n:
        print("gate fired on no rounds"); return

    rate = n / len(slugs)
    print("=" * 62)
    print("EXPECTED BEHAVIOUR IF YOU RUN LIVE NOW")
    print("=" * 62)
    print(f"  trades        : {n} on {len(slugs)} rounds  ({rate:.0%} of rounds)")
    print(f"                  ~{rate*12:.1f} trades/hour  (~{rate*12*24:.0f}/day)")
    print(f"  win rate      : {wins/n:.1%}  ({wins}W / {n-wins}L)")
    print(f"  avg buy price : {st.mean(prices):.3f}")
    print(f"  total P&L     : ${sum(pnls):+.2f} on ${n*STAKE:.0f} staked")
    print(f"  per trade     : ${st.mean(pnls):+.4f}  (sd ${st.pstdev(pnls):.3f})")
    print(f"  ROI           : {sum(pnls)/(n*STAKE):+.1%}")
    print(f"  per hour      : ${st.mean(pnls)*rate*12:+.3f}")

    peak = cum = mdd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); mdd = min(mdd, cum - peak)
    print(f"  max drawdown  : ${mdd:.2f}")

    random.seed(5)
    boots = []
    for _ in range(8000):
        smp = [slugs[random.randrange(len(slugs))] for _ in range(len(slugs))]
        bp, _, _ = simulate(data, smp)
        boots.append(sum(bp) / len(bp) if bp else 0.0)
    boots.sort()
    lo, hi = boots[200], boots[7799]
    print("\n  --- bootstrap over rounds (the honest interval) ---")
    print(f"  per-trade P&L : ${st.mean(pnls):+.4f}   95% CI [${lo:+.4f}, ${hi:+.4f}]")
    print(f"  P(profitable) : {sum(1 for b in boots if b>0)/len(boots):.1%}")
    print(f"  verdict       : {'edge is significant' if lo>0 else 'NOT significant - could be losing'}")

    print("\n  --- what a $10 bankroll looks like over 24h ---")
    tp = n / len(slugs) * 12 * 24
    print(f"  ~{tp:.0f} trades/day at $1")
    for label, v in (("median outcome", st.mean(pnls)), ("bad case (2.5%)", lo),
                     ("good case (97.5%)", hi)):
        print(f"    {label:18}: ${v*tp:+.2f}/day  -> balance ${10+v*tp:.2f}")


if __name__ == "__main__":
    main()
