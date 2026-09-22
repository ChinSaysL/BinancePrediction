"""Compare the FAMILIES on the recorded tape itself -- no fitted gate anywhere.

WHY THIS EXISTS. "eth-5m is negative" was produced by replaying the model arm
with BTC's knobs (band [0.50,0.85], travel [5,35], tau [60,210]). That answer
mixes two different claims:

    (a) eth has no edge, and
    (b) BTC's fitted gates do not transfer to eth.

So do not settle it with a P&L either: 7 live fills on eth-5m is not evidence
of anything. Ask the DATA, using no knob that was fitted anywhere:

  A. VENUE CALIBRATION, per family. Bucket every quote by the FAVOURITE's ask
     and compare the realised win rate with that ask. A book where win == ask
     cannot be beaten by any price-taking rule, in any family, at any gate
     setting; where win < ask, crossing is a certain loss of the shortfall. This
     is the bound the arms live under.

  B. MODEL INFORMATION, per family. Bucket by the edge OUR model claims
     (fair - ask, better side) and measure the realised return of buying it. If
     the claim does not order the outcome, that family carries no signal for
     this model -- again independent of how the gates are set.

  C. HEADLINE, per family. One entry per round (the first tick whose claim
     clears the model's own `min_edge`), NO band and NO travel gate, scored with
     the four-check battery. This is the closest thing to "the arm without
     BTC's knobs" and is directly comparable across families.

Rules applied: `ao.load` (the real loader, so the trade set is the one the
book records), the venue's measured fee, one entry per round, and the battery.
The window is FIXED at tau [60,210] because that is when the model has an
opinion at all -- it is the arm's evaluation window, not a fitted filter.

Usage (on the VPS):
    PYTHONPATH=. .venv/bin/python -m tools.family_data_compare --family btc-updown-5m
    PYTHONPATH=. .venv/bin/python -m tools.family_data_compare      # all
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arm_overlap as ao                                     # noqa: E402
from btcpredict.config import settings as S                  # noqa: E402
from btcpredict.edge.signal import fee_rate_for_price        # noqa: E402
from btcpredict.pricing.digital import fair_value            # noqa: E402

MIN_TAU, MAX_TAU = 60.0, 210.0
FAMILIES = ("btc-updown-5m", "eth-updown-5m", "bnb-updown-5m",
            "btc-updown-15m", "eth-updown-15m", "bnb-updown-15m")


def _t(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m / math.sqrt(v / n) if v > 0 else 0.0


def _battery(rows: list[dict]) -> dict:
    if not rows:
        return {}
    order = sorted(rows, key=lambda r: r["ts_ms"])
    by_day: dict[int, list[float]] = {}
    for r in order:
        by_day.setdefault(r["day"], []).append(r["ret"])
    dm = [st.fmean(v) for v in by_day.values()]
    half = len(order) // 2
    best = max(by_day, key=lambda d: st.fmean(by_day[d]))
    rest = [r["ret"] for r in order if r["day"] != best]
    return {"n": len(order),
            "win": st.fmean([1.0 if r["won"] else 0.0 for r in order]),
            "paid": st.fmean([r["px"] for r in order]),
            "ev": st.fmean([r["ret"] for r in order]),
            "t": _t([r["ret"] for r in order]), "day_t": _t(dm),
            "days": len(by_day),
            "h1": st.fmean([r["ret"] for r in order[:half]]),
            "h2": st.fmean([r["ret"] for r in order[half:]]),
            "dday": st.fmean(rest) if rest else float("nan")}


def collect(rs: list[dict], sigmas: list[float]):
    """(calibration rows, claim rows, per-round first-claim rows)."""
    cal, claim, per_round = [], [], []
    for rnd, sg in zip(rs, sigmas):
        strike = rnd["strike"]
        winner = rnd["winner"]
        day = rnd["day"]
        first = None
        for (ts, tau, spot, _up_px, _dn_px, up_ask, dn_ask) in rnd["rows"]:
            if not (MIN_TAU <= tau <= MAX_TAU):
                continue
            if not spot or spot <= 0 or not up_ask or not dn_ask:
                continue
            # A pair summing below $1 is free arbitrage, i.e. a corrupt row,
            # and any price filter selects exactly those -- see arm_overlap.
            if up_ask + dn_ask < 0.999:
                continue
            fv = fair_value(spot, strike, tau, sg)
            if not (0.0 < fv < 1.0):
                continue
            # A. the favourite, chosen with no model at all
            if first is None:
                fav = "Up" if up_ask > dn_ask else "Down"
                fpx = up_ask if fav == "Up" else dn_ask
                cal.append({"ts_ms": ts, "day": day, "px": fpx,
                            "won": winner == fav,
                            "ret": (1.0 if winner == fav else 0.0) - fpx
                                   - fee_rate_for_price(fpx)})
            # B. what OUR model claims
            eu, ed = fv - up_ask, (1.0 - fv) - dn_ask
            side, ask, edge = (("Up", up_ask, eu) if eu >= ed
                               else ("Down", dn_ask, ed))
            claim.append({"ts_ms": ts, "day": day, "px": ask, "edge": edge,
                          "won": winner == side,
                          "ret": (1.0 if winner == side else 0.0) - ask
                                 - fee_rate_for_price(ask)})
            # C. the first tick in the round whose claim clears min_edge
            if first is None and edge >= S.min_edge:
                first = {"ts_ms": ts, "day": day, "px": ask, "won":
                         winner == side, "edge": edge,
                         "ret": (1.0 if winner == side else 0.0) - ask
                                - fee_rate_for_price(ask)}
        if first is not None:
            per_round.append(first)
    return cal, claim, per_round


def show(name: str, cal, claim, per_round) -> None:
    print(f"\n================ {name} ================")
    b = _battery(per_round)
    if b.get("n", 0) >= 5:
        print(f"  C. HEADLINE (first claim >= min_edge {S.min_edge:g}, "
              f"no band, no travel gate, one entry/round):")
        print(f"     n={b['n']:<5} {b['n']/max(b['days'],1):5.1f}/d  "
              f"WR {b['win']:5.1%}  paid {b['paid']:.3f}  EV {b['ev']:+.4f}/$1  "
              f"t {b['t']:+5.2f}  day-t {b['day_t']:+5.2f}  d={b['days']}")
        print(f"     halves {b['h1']:+.4f}/{b['h2']:+.4f}   "
              f"drop-best-day {b['dday']:+.4f}")
    else:
        print(f"  C. HEADLINE: n={b.get('n', 0)} -- too few to read")
    print("  A. VENUE CALIBRATION (favourite, chosen with no model):")
    print("     ask bucket      n    realised win   ask     gap      EV/$1")
    for lo, hi in ((0.50, 0.60), (0.60, 0.70), (0.70, 0.85), (0.85, 1.01)):
        g = [r for r in cal if lo <= r["px"] < hi]
        if len(g) < 30:
            continue
        wr = st.fmean([1.0 if r["won"] else 0.0 for r in g])
        px = st.fmean([r["px"] for r in g])
        print(f"     {lo:.2f}-{hi:.2f}   {len(g):>6}   {wr:>11.1%}   "
              f"{px:.3f}  {wr-px:+.3f}   {st.fmean([r['ret'] for r in g]):+.4f}")
    print("  B. MODEL INFORMATION (does the claimed edge order the outcome?):")
    print("     claim bucket    n    claimed   realised   EV/$1")
    for lo, hi in ((0.04, 0.08), (0.08, 0.12), (0.12, 0.20), (0.20, 1.0)):
        g = [r for r in claim if lo <= r["edge"] < hi]
        if len(g) < 50:
            continue
        print(f"     {lo:.2f}-{hi:.2f}     {len(g):>6}   "
              f"{st.fmean([r['edge'] for r in g]):+.3f}   "
              f"{st.fmean([1.0 if r['won'] else 0.0 for r in g]):>8.1%}   "
              f"{st.fmean([r['ret'] for r in g]):+.4f}")
    if claim:
        ce = [r["edge"] for r in claim]
        cr = [r["ret"] for r in claim]
        n, me, mr = len(ce), sum(ce) / len(ce), sum(cr) / len(cr)
        cov = sum((a - me) * (b2 - mr) for a, b2 in zip(ce, cr))
        de = math.sqrt(sum((a - me) ** 2 for a in ce))
        dr = math.sqrt(sum((b2 - mr) ** 2 for b2 in cr))
        print(f"     corr(claimed edge, realised) = "
              f"{cov/(de*dr) if de and dr else 0:+.3f}   (all {n} ticks)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--family", default=None)
    ap.add_argument("--since-ms", type=int, default=0)
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    pm = {row[0] for row in con.execute(
        "SELECT slug FROM rounds WHERE feed_provider LIKE 'chainlink-twap%'")}
    fams = (a.family,) if a.family else FAMILIES
    print(f"venue=binance only (dropping {len(pm)} Polymarket rounds); "
          f"window tau [{MIN_TAU:g},{MAX_TAU:g}]s; fee = the venue's own curve")
    for f in fams:
        rs = ao.load(con, family=f)
        if a.since_ms:
            rs = [r for r in rs if r["start_ms"] >= a.since_ms]
        rs = [r for r in rs if r["slug"] not in pm]
        if not rs:
            print(f"\n================ {f} ================\n  no rounds")
            continue
        sig = [ao.rolling_sigma(rs, i) for i in range(len(rs))]
        t0 = time.time()
        cal, claim, per_round = collect(rs, sig)
        print(f"\n(family {f}: {len(rs)} rounds, {len(claim)} ticks, "
              f"{time.time()-t0:.0f}s)")
        show(f, cal, claim, per_round)


if __name__ == "__main__":
    main()
