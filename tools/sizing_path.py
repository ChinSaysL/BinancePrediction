"""Sizing paths: what actually produces dollars, from $7.35 with a measured edge.

`kelly_stake` returns 0.0 -- i.e. SKIP -- whenever f* x fraction x bankroll lands
below min_stake. It does not floor to the minimum. So the question "does Kelly
size us up" has a sharp answer at a small bankroll, and it is arithmetic:

    b       = 1/q - 1
    f*      = (p*(1+b) - 1) / b
    stake   = f* * kelly_fraction * bankroll

At q = 0.707 and a post-haircut edge of 0.025, f* = 0.085. With the shipped
`kelly_fraction` of 0.25 the stake on a $7.35 account is $0.16, which is below
`min_order_usdt`, so Kelly takes NOTHING. It does not trade small -- it stops.

This tool measures every sizing rule on the same fills, from the same 7 days of
the real book, compounding day by day, so the comparison is exposure-for-
exposure and the ruin probability is visible:

    flat $1 (shipped) | fraction of bankroll | Kelly (as shipped) | Kelly floored

Reported per rule: fills taken, $/day at the start, days to a $30/day run-rate,
max drawdown, and P(the account can no longer fund the minimum order).

Usage:
    python3 -m tools.sizing_path
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcpredict.config import settings as S                        # noqa: E402
from btcpredict.edge.signal import (evaluate, fee_rate_for_price,  # noqa: E402
                                    kelly_stake, stake_for_fraction)

from tools.forecast_paired import build_sigma, load                # noqa: E402


def fills(a) -> pd.DataFrame:
    """Every gated fill on the real book, with the numbers sizing needs."""
    q_, rounds, slugs = load(a.family)
    sig = build_sigma(q_, slugs)
    idx = rounds.set_index("slug")
    out = []
    for x in q_.itertuples(index=False):
        if x.slug not in idx.index or not (a.min_tau <= x.tau_s <= a.max_tau):
            continue
        rr = idx.loc[x.slug]
        if isinstance(rr, pd.DataFrame):
            rr = rr.iloc[0]
        if not rr.strike or rr.strike <= 0:
            continue
        minute = (int(x.ts_ms) // 1000) // 60 * 60
        sg_ = sig.get(minute, np.nan)
        if not np.isfinite(sg_) or sg_ <= 0:
            continue
        up_mid = ((x.up_bid + x.up_ask) / 2
                  + (1 - (x.down_bid + x.down_ask) / 2)) / 2
        from btcpredict.pricing.digital import fair_value
        fair = fair_value(x.spot, rr.strike, x.tau_s, sg_)
        s = evaluate(fair, up_mid, 1.0 - up_mid,
                     half_spread=S.assumed_half_spread,
                     safety_margin=S.safety_margin, min_edge=S.min_edge,
                     min_quote=a.min_quote, max_quote=a.max_quote,
                     edge_haircut=S.edge_haircut)
        if not s.act or s.side is None:
            continue
        paid = x.up_ask if s.side == "Up" else x.down_ask
        if not (0.0 < paid < 1.0):
            continue
        won = int(int(rr.winner == "Up") == (1 if s.side == "Up" else 0))
        out.append({"day": int(rr.day), "slug": x.slug, "fair": s.fair,
                    "q": s.quote, "edge": s.edge, "paid": float(paid),
                    "fee": fee_rate_for_price(float(paid)), "won": won})
    f = pd.DataFrame(out)
    if f.empty:
        return f
    f = (f.sort_values(["day", "slug"]).drop_duplicates("slug", keep="first")
           .sort_values(["day", "slug"]).reset_index(drop=True))
    keep, skip = [], 0
    for i, r in f.iterrows():
        if skip:
            skip -= 1
            continue
        keep.append(i)
        if not r.won:
            skip = S.model_cooldown_after_loss
    return f.loc[keep].reset_index(drop=True)


def stake_of(rule: str, r: dict, bank: float, a) -> float:
    """The stake this rule would place. 0 means skip.

    EVERY rule is capped at the venue's resting depth. Uncapped, a fractional
    rule compounds a $7 account into liquidity that is not there -- the same
    trap `live_config_projection.py` documents, where a $46 start reached
    millions on paper. The cap is what makes the paths comparable.
    """
    fair, q = float(r["fair"]), float(r["q"])
    if rule == "flat $1 (shipped)":
        s = 0.0 if bank < S.min_order_usdt else min(1.0, bank)
    elif rule.startswith("fraction"):
        pct = float(rule.split()[-1].rstrip("%")) / 100.0
        s = max(bank * pct, S.min_order_usdt)
    elif rule.startswith("kelly as shipped"):
        s = kelly_stake(fair, q, bank, fraction=S.kelly_fraction,
                        min_stake=S.min_order_usdt, max_stake=a.max_stake)
    elif rule.startswith("kelly floored"):
        pct = float(rule.split()[-1].rstrip("%")) / 100.0
        k = kelly_stake(fair, q, 1.0, fraction=S.kelly_fraction,
                        min_stake=0.0, max_stake=1e9)          # as a fraction
        s = max(k * pct * bank, S.min_order_usdt)
    else:
        return 0.0
    if s <= 0 or bank < S.min_order_usdt:
        return 0.0
    return min(s, a.depth_cap, bank)


def simulate(f: pd.DataFrame, rule: str, a, days: int, rng) -> dict:
    byday = [g.to_dict("records") for _, g in f.groupby("day")]
    out = []
    for _ in range(a.paths):
        bank = a.start
        peak = bank
        mdd = 0.0
        ruleday = None
        n = 0
        for d in range(days):
            start_bank = bank
            for r in byday[rng.integers(0, len(byday))]:
                st = stake_of(rule, r, bank, a)
                if st <= 0 or st > bank:
                    continue
                n += 1
                bank += st * (1.0 / r["paid"] - 1.0 - r["fee"]) if r["won"] else -st
                peak = max(peak, bank)
                mdd = max(mdd, (peak - bank) / peak if peak > 0 else 0.0)
                if bank < S.min_order_usdt:
                    bank = 0.0
                    break
            if bank <= 0:
                break
            if ruleday is None and (bank - start_bank) >= 30.0:
                ruleday = d + 1
        out.append((bank, mdd, ruleday, n))
    arr = np.array([(b, m, (r if r is not None else np.nan), n) for b, m, r, n
                    in out], dtype=float)
    return {"median": float(np.median(arr[:, 0])), "p10": float(np.percentile(arr[:, 0], 10)),
            "mdd": float(np.median(arr[:, 1])), "ruin": float((arr[:, 0] <= 0).mean()),
            "days30": float(np.nanmedian(arr[:, 2])) if np.isfinite(arr[:, 2]).any() else float("nan"),
            "pct30": float(np.isfinite(arr[:, 2]).mean()),
            "fills": float(np.median(arr[:, 3]))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", default="btc-updown-5m")
    ap.add_argument("--min-tau", type=float, default=120.0)
    ap.add_argument("--max-tau", type=float, default=180.0)
    ap.add_argument("--min-quote", type=float, default=0.60)
    ap.add_argument("--max-quote", type=float, default=0.80)
    ap.add_argument("--start", type=float, default=7.35)
    ap.add_argument("--depth-cap", type=float, default=55.0,
                    help="most that can rest at the venue's touch; uncapped "
                         "fractional sizing compounds into liquidity that is "
                         "not there")
    ap.add_argument("--max-stake", type=float, default=1.0)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--paths", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    f = fills(a)
    if f.empty:
        print("no fills")
        return 1
    print(f"fills on the real book at the shipped cell: {len(f)} over "
          f"{f.day.nunique()} days ({len(f) / f.day.nunique():.1f}/day)")
    print(f"start ${a.start:.2f} | Kelly fraction {S.kelly_fraction} | "
          f"min order ${S.min_order_usdt:.2f} | depth cap ${a.depth_cap:.0f}")

    # what Kelly WOULD size, per fill, on today's bankroll
    # units: kelly_stake() is given the LIVE bankroll, so it already returns
    # DOLLARS. An earlier version of this print multiplied by the bankroll again
    # and reported $1.33 where the truth is $0.18 -- which flipped the whole
    # conclusion, because $0.18 is below the minimum order and $1.33 is not.
    ks = np.array([kelly_stake(r.fair, r.q, a.start, fraction=S.kelly_fraction,
                               min_stake=0.0, max_stake=1e9)
                   for r in f.itertuples()])
    per_dollar = float(np.median(ks)) / a.start
    print(f"\n  Kelly stake on a ${a.start:.2f} account, at the shipped "
          f"fraction {S.kelly_fraction}: median ${float(np.median(ks)):.3f}")
    print(f"  trades that clear the ${S.min_order_usdt:.2f} minimum: "
          f"{int((ks >= S.min_order_usdt).sum())} of {len(f)}")
    print(f"  bankroll at which HALF the fills clear it: "
          f"${S.min_order_usdt / per_dollar:.2f}   <-- below this, "
          f"`kelly_stake` returns 0 and the arm places NOTHING")

    rules = ["flat $1 (shipped)", "fraction 2.0%", "fraction 4.0%",
             "fraction 8.0%", "kelly as shipped 100%", "kelly floored 25%",
             "kelly floored 50%"]
    rng = np.random.default_rng(a.seed)
    print(f"\n  === {a.days}-day compounding paths, day-block resampled "
          f"({a.paths} paths), every stake capped at ${a.depth_cap:.0f} "
          f"===")
    print(f"  {'sizing rule':<24}{'fills':>7}{'median $':>12}{'p10 $':>11}"
          f"{'P(ruin)':>9}{'medDD':>8}{'days to $30/day':>17}{'P(hit)':>9}")
    print("  " + "-" * 97)
    for rule in rules:
        st = simulate(f, rule, a, a.days, rng)
        d30 = (f"{st['days30']:.0f}" if np.isfinite(st["days30"]) else "  never")
        print(f"  {rule:<24}{st['fills']:>7.0f}{st['median']:>12.2f}"
              f"{st['p10']:>11.2f}{st['ruin']:>9.1%}{st['mdd']:>8.1%}"
              f"{d30:>17}{st['pct30']:>9.0%}")
    print("\n  'fills' is the median number of orders actually PLACED per "
          "path, so a rule that skips is visible as a smaller number.")
    print("  A rule that cannot reach the minimum order places nothing and "
          "earns nothing, however good its Kelly fraction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
