"""Turn recorded data into the Phase 0 / Phase 1 answers.

Reports, in order of how much they matter:

  1. MODEL vs MARKET Brier -- the go/no-go. If the market's own quote scores as
     well as our model, there is nothing to trade and no amount of execution
     tuning fixes that.
  2. Quote lag -- how many seconds the market takes to reflect a spot move.
     This is the mechanism behind the primary edge.
  3. Basis -- oracle (strike/end_price) against our recorded spot, so the
     "spot is a fine proxy" assumption keeps being checked rather than trusted.
  4. Cost sensitivity -- realised PnL as a function of assumed half-spread.
     The strategy must survive the spread we actually measured, not a hoped-for one.

Usage:  python -m tools.analyze
"""
from __future__ import annotations

import argparse
import math
import statistics as st

from btcpredict.calibration.report import (PnLSummary, reliability_table, score)
from btcpredict.config import settings
from btcpredict.edge.signal import FeeModel, evaluate
from btcpredict.pricing.digital import fair_value
from btcpredict.store.db import Store

BAR = "=" * 78


def section(t: str) -> None:
    print(f"\n{BAR}\n{t}\n{BAR}")


def basis_report(store: Store) -> None:
    section("1. ORACLE vs SPOT BASIS")
    rounds = store.query(
        "SELECT slug, strike, end_price, start_ms, end_ms FROM rounds "
        "WHERE winner IS NOT NULL AND end_price IS NOT NULL ORDER BY start_ms")
    quotes = store.query(
        "SELECT slug, ts_ms, spot FROM quotes WHERE spot IS NOT NULL ORDER BY ts_ms")
    by_slug: dict[str, list] = {}
    for q in quotes:
        by_slug.setdefault(q["slug"], []).append(q)

    # Match each round to its own last spot sample before settlement. SQLite will
    # not resolve an outer alias inside a subquery ORDER BY, so this is done here.
    diffs = []
    for r in rounds:
        seq = by_slug.get(r["slug"])
        if not seq:
            continue
        nearest = min(seq, key=lambda q: abs(q["ts_ms"] - r["end_ms"]))
        if abs(nearest["ts_ms"] - r["end_ms"]) <= 30_000:
            diffs.append(r["end_price"] - nearest["spot"])
    if not diffs:
        print("  no overlapping quote+settlement data yet (run the recorder longer)")
        return
    print(f"  settled rounds with a nearby spot sample: {len(diffs)}")
    print(f"  mean basis  : ${st.mean(diffs):+.4f}")
    print(f"  median      : ${st.median(diffs):+.4f}")
    print(f"  max |basis| : ${max(map(abs, diffs)):.4f}")
    if len(diffs) > 1:
        print(f"  stdev       : ${st.pstdev(diffs):.4f}")
    print("  (a basis small next to the ~$50 5-min sigma means spot is a sound proxy)")


def chain_report(store: Store) -> None:
    section("2. ORACLE CHAIN INTEGRITY")
    rows = store.query("""SELECT slug, start_ms, strike, end_price FROM rounds
                          WHERE strike IS NOT NULL ORDER BY start_ms""")
    bad = 0
    checked = 0
    for a, b in zip(rows, rows[1:]):
        if b["start_ms"] - a["start_ms"] != 300_000 or a["end_price"] is None:
            continue
        checked += 1
        if abs(a["end_price"] - b["strike"]) > 1e-6:
            bad += 1
    print(f"  consecutive pairs checked: {checked}   mismatches: {bad}")
    if checked and not bad:
        print("  chain is exact: end_price[N] == strike[N+1] (settlement series is reliable)")


def calibration_report(store: Store) -> None:
    section("3. MODEL vs MARKET  (the go/no-go)")
    rows = store.query("""
        SELECT q.*, r.winner, r.strike AS rstrike
          FROM quotes q JOIN rounds r ON r.slug = q.slug
         WHERE r.winner IS NOT NULL AND q.spot IS NOT NULL
               AND q.tau_s > 0 AND r.strike IS NOT NULL AND q.up_ask IS NOT NULL
    """)
    n_rounds = len({r["slug"] for r in rows})
    if n_rounds < 20:
        print(f"  only {n_rounds} settled rounds ({len(rows)} quotes). Need >=20 distinct")
        print("  rounds before any comparison means anything -- quotes within one round")
        print("  share a single outcome, so a big quote count proves nothing. Keep recording.")
        return

    sigma = st.median([r["sigma_1m"] for r in store.query(
        "SELECT sigma_1m FROM decisions WHERE sigma_1m IS NOT NULL")] or [0.00037])

    for lo, hi, label in ((0, 60, "0-60s left"), (60, 150, "60-150s left"),
                          (150, 300, "150-300s left")):
        band = [r for r in rows if lo < r["tau_s"] <= hi]
        if len({r["slug"] for r in band}) < 20:
            continue
        model, market, outs = [], [], []
        for r in band:
            outs.append(1 if r["winner"] == "Up" else 0)
            model.append(fair_value(r["spot"], r["rstrike"], r["tau_s"], sigma))
            # Score against the ask -- the price actually payable. `up_price` is
            # a stale last-trade value (seen at 0.65 while the book mid was
            # 0.675); beating it is not evidence of anything tradeable.
            market.append(r["up_ask"] if r["up_ask"] is not None else r["up_price"])
        ms, qs = score(model, outs), score(market, outs)
        if ms.base_rate in (0.0, 1.0):
            # Every round in the band resolved the same way; Brier is not comparable.
            print(f"\n  --- {label} (n={len(band)}) --- skipped: "
                  f"all outcomes identical (base rate {ms.base_rate:.0f})")
            continue
        verdict = ("MODEL BEATS MARKET" if ms.brier < qs.brier - 1e-6
                   else "market is at least as good -- no edge here")
        print(f"\n  --- {label}  (n={len(band)} quotes over "
              f"{len({r['slug'] for r in band})} rounds) ---")
        print(f"    model : {ms}")
        print(f"    market: {qs}")
        print(f"    Brier gap: {qs.brier - ms.brier:+.4f}   =>  {verdict}")
        print(reliability_table(model, outs, min_n=15))


def lag_report(store: Store) -> None:
    section("4. QUOTE LAG BEHIND SPOT")
    rows = store.query("""SELECT slug, ts_ms, up_price, spot FROM quotes
                          WHERE spot IS NOT NULL ORDER BY slug, ts_ms""")
    by_round: dict[str, list] = {}
    for r in rows:
        by_round.setdefault(r["slug"], []).append(r)

    best = (0, -2.0)
    for lag in range(0, 11):
        num = den_a = den_b = 0.0
        for seq in by_round.values():
            if len(seq) < lag + 6:
                continue
            d_spot = [seq[i + 1]["spot"] - seq[i]["spot"] for i in range(len(seq) - 1)]
            d_q = [seq[i + 1]["up_price"] - seq[i]["up_price"] for i in range(len(seq) - 1)]
            for i in range(len(d_spot) - lag):
                a, b = d_spot[i], d_q[i + lag]
                num += a * b; den_a += a * a; den_b += b * b
        if den_a > 0 and den_b > 0:
            corr = num / math.sqrt(den_a * den_b)
            marker = ""
            if corr > best[1]:
                best = (lag, corr); marker = ""
            print(f"    lag {lag:2d} tick(s): corr(d_spot, d_quote) = {corr:+.4f}{marker}")
    if best[1] > -2:
        print(f"\n  peak correlation at lag {best[0]} tick(s) ({best[1]:+.4f})")
        print("  a peak at lag>0 means the quote follows spot -- that delay is the edge")


def cost_sensitivity(store: Store) -> None:
    section("5. COST SENSITIVITY  (would the rule have made money?)")
    rows = store.query("""
        SELECT q.*, r.winner, r.strike AS rstrike, r.fee_rate_bps
          FROM quotes q JOIN rounds r ON r.slug = q.slug
         WHERE r.winner IS NOT NULL AND q.spot IS NOT NULL
               AND q.tau_s > 0 AND r.strike IS NOT NULL
      ORDER BY q.ts_ms
    """)
    if len(rows) < 100:
        print(f"  only {len(rows)} scored quotes -- need more. Keep recording.")
        return
    sigma = 0.00037
    print(f"  {'half_spread':>12} {'trades':>7} {'win_rate':>9} {'pnl':>9} {'roi':>9}")
    for hs in (0.00, 0.01, 0.02, 0.03, 0.05):
        pnl = PnLSummary()
        taken: set[str] = set()
        for r in rows:
            if r["slug"] in taken:
                continue
            if not (settings.min_tau_s <= r["tau_s"] <= settings.max_tau_s):
                continue
            fair = fair_value(r["spot"], r["rstrike"], r["tau_s"], sigma)
            fee = (r["fee_rate_bps"] or 200) / 10_000.0
            s = evaluate(fair, r["up_price"], r["down_price"], fee=fee,
                         half_spread=hs, safety_margin=settings.safety_margin,
                         fee_model=FeeModel.NOTIONAL)
            if not s.act:
                continue
            taken.add(r["slug"])
            fill_px = min(0.999, s.quote + hs)
            won = (s.side == r["winner"])
            stake, feepaid = 1.0, 1.0 * fee
            payout = stake / fill_px if won else 0.0
            pnl.add(payout - stake - feepaid, stake, feepaid, won)
        print(f"  {hs:>12.2f} {pnl.n_trades:>7d} {pnl.win_rate:>9.3f} "
              f"{pnl.pnl:>+9.2f} {pnl.roi:>+9.2%}")
    print("\n  Read this against the half-spread you actually measured, not the best column.")


def realised_pnl(store: Store) -> None:
    section("6. RECORDED PAPER/LIVE RESULTS")
    rows = store.query("""SELECT s.*, f.stake, f.fee_paid FROM settlements s
                          JOIN fills f ON f.id = s.fill_id""")
    if not rows:
        print("  no settled trades recorded yet")
        return
    for mode in {r["mode"] for r in rows}:
        p = PnLSummary()
        for r in (x for x in rows if x["mode"] == mode):
            p.add(r["pnl"], r["stake"], r["fee_paid"], bool(r["won"]))
        print(f"  {mode:>6}: {p}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(settings.db_path))
    args = ap.parse_args()
    store = Store(args.db)
    c = store.counts()
    print(f"DB: {args.db}")
    print(f"rounds={c['rounds']} quotes={c['quotes']} decisions={c['decisions']} "
          f"fills={c['fills']} settlements={c['settlements']}")
    basis_report(store)
    chain_report(store)
    calibration_report(store)
    lag_report(store)
    cost_sensitivity(store)
    realised_pnl(store)
    store.close()


if __name__ == "__main__":
    main()
