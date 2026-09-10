"""Do smarter sizing / smarter loss limits beat a flat percentage of bankroll?

The bar is deliberately high. With ~70 replayed trades, almost any rule can be
made to look good in-sample, so each candidate must beat the simple baseline on
a slice it was NOT chosen on, and across the bootstrap distribution rather than
on the single historical path.

Candidates
  flat 3/5/10%     the baseline family
  kelly k          f* = p - (1-p)*q/(1-q) scaled by k, using the model's own
                   (post-haircut) fair -- "smart" because it prices both the
                   probability and the payoff
  edge-scaled      fraction proportional to the claimed edge
  z-scaled         fraction proportional to |z| (how decided the round is)
  dd-throttled     fraction cut as the drawdown from peak deepens
Loss limits
  daily % cap      stop for the day at -X% of the day's opening bankroll
  equity stop      hard halt for good at -X% from peak (shows the cost of a
                   permanent stop)
  dd throttle      soft version: keep trading but shrink the stake

Usage: python -m tools.sizing_experiments [--bankroll 68] [--paths 2000]
"""
from __future__ import annotations

import argparse
import math
import random
import statistics as st

from btcpredict.config import settings
from tools.sizing_study import load_trades

MIN_ORDER = settings.min_order_usdt
CAP = 0.0                 # absolute $ cap; 0 = none. Set from --cap.
MAX_FRAC = 0.20           # no rule may ever stake more than 20% of bankroll
BUNDLE = dict(j=1.0, median_edge=1.0)   # helper globals


def kelly_fraction(p: float, q: float) -> float:
    """Binary Kelly: f* = p - (1-p) * q / (1-q)."""
    if q <= 0 or q >= 1:
        return 0.0
    return max(0.0, p - (1.0 - p) * q / (1.0 - q))


def make_rules(med_edge: float, med_mz: float):
    def clip(frac):
        return max(0.0, min(MAX_FRAC, frac))

    def ev_per_dollar(t):
        # what the live band sizes on, expressed as a fraction of bankroll
        return t["edge"] / t["q"]

    def raw_kelly(t, hair: float):
        # `hair` = how much of the model's disagreement to believe. 1.0 uses the
        # un-haircut fair, which is what overbetting looks like.
        p = t["q"] + (t["p"] - t["q"]) / hair if hair else t["p"]
        return kelly_fraction(p, t["q"])

    return {
        # --- baselines ---
        "flat 3%":        lambda b, t, dd: 0.03,
        "flat 5%":        lambda b, t, dd: 0.05,
        "flat 10%":       lambda b, t, dd: 0.10,
        # --- smarter: size from the model's own edge ---
        "kelly 0.10":     lambda b, t, dd: clip(0.10 * kelly_fraction(t["p"], t["q"])),
        "kelly 0.25":     lambda b, t, dd: clip(0.25 * kelly_fraction(t["p"], t["q"])),
        "kelly 0.50":     lambda b, t, dd: clip(0.50 * kelly_fraction(t["p"], t["q"])),
        "kelly .25 nohair": lambda b, t, dd: clip(0.25 * raw_kelly(t, None)),
        "edge-scaled 5%": lambda b, t, dd: clip(0.05 * t["edge"] / med_edge),
        "z-scaled 5%":    lambda b, t, dd: clip(0.05 * t["mz"] / med_mz),
        "ev-band as %b":  lambda b, t, dd: clip(0.30 * ev_per_dollar(t)),
        # --- our venue says low |z| pays best (the mirror of Polymarket) ---
        "inv-z 5%":       lambda b, t, dd: clip(0.05 * med_mz / max(t["mz"], 0.05)),
        "low-q 5%":       lambda b, t, dd: clip(0.05 * 0.5 / max(t["q"], 0.05)),
        # --- smarter: react to the equity curve ---
        "5% + dd-throttle": lambda b, t, dd: 0.05 * max(0.25, 1.0 - dd / 0.25),
        "kelly .25 + dd":  lambda b, t, dd: clip(0.25 * kelly_fraction(t["p"], t["q"])
                                                * max(0.25, 1.0 - dd / 0.25)),
    }


def simulate(recs, frac_fn, bankroll, daily_cap=None, equity_stop=None):
    """recs = list of (ret, trade_dict, day). frac_fn(bankroll, trade, dd)."""
    b, peak = bankroll, bankroll
    dd_max, low = 0.0, bankroll
    taken = skipped = 0
    cur_day, day_start = None, bankroll
    stopped = False
    for ret, t, day in recs:
        if b < MIN_ORDER or stopped:
            break
        if equity_stop and (b - peak) <= -equity_stop * peak:
            stopped = True
            break
        if daily_cap:
            if day != cur_day:
                cur_day, day_start = day, b
            if (b - day_start) <= -daily_cap * day_start:
                skipped += 1
                continue
        dd = (peak - b) / peak if peak > 0 else 0.0
        stake = frac_fn(b, t, dd) * b
        if CAP > 0:
            stake = min(stake, CAP)
        if stake < MIN_ORDER:
            skipped += 1
            continue
        stake = min(stake, b)
        b += ret * stake
        taken += 1
        peak = max(peak, b)
        low = min(low, b)
        if peak > 0:
            dd_max = min(dd_max, (b - peak) / peak * 100.0)
    return {"final": b, "dd": dd_max, "taken": taken, "skipped": skipped,
            "stopped": stopped}




def boot(recs, frac_fn, bankroll, paths, length, daily_cap=None, equity_stop=None):
    rng = random.Random(20260910)
    finals, dds = [], []
    for _ in range(paths):
        seq = [rng.choice(recs) for _ in range(length)]
        o = simulate(seq, frac_fn, bankroll, daily_cap, equity_stop)
        finals.append(o["final"])
        dds.append(o["dd"])
    finals.sort()
    return {"med": st.median(finals), "p05": finals[int(0.05 * len(finals))],
            "p95": finals[int(0.95 * len(finals))],
            "ruin": sum(1 for f in finals if f < 10) / paths,
            "dd": st.median(dds)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=68.0)
    ap.add_argument("--paths", type=int, default=2000)
    ap.add_argument("--length", type=int, default=200)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--cap", type=float, default=0.0,
                    help="absolute $ stake cap; 0 = none (only MAX_FRAC binds)")
    args = ap.parse_args()
    global CAP
    CAP = args.cap

    trades, days = load_trades()
    recs = [(t["pnl"], t, d) for t, d in zip(trades, days)]
    med_edge = st.median(t["edge"] for t in trades)
    med_mz = st.median(t["mz"] for t in trades)
    b0 = args.bankroll
    print(f"{len(recs)} trades   bankroll ${b0:.2f}   floor ${MIN_ORDER:.2f}   "
          f"cap ${CAP:.0f}   max fraction {MAX_FRAC:.0%}")
    print(f"median edge {med_edge:.3f}   median |z| {med_mz:.3f}\n")

    rules = make_rules(med_edge, med_mz)

    # Kelly on this sample is f* = mean/var ~= 45%, far above every rule here, so
    # in a mean-preserving bootstrap ANY rule with a bigger average stake grows
    # more -- a raw comparison just ranks by size. To ask whether a rule is
    # SMARTER (better shape) rather than merely BIGGER, scale every rule so its
    # mean fraction equals the baseline's 5%, then compare.
    def mean_frac(fn):
        return st.mean(fn(b0, t, 0.0) for t in trades)

    def clip(f):
        return max(0.0, min(MAX_FRAC, f))

    matched = {}
    for name, fn in rules.items():
        mf = mean_frac(fn)
        scale = (0.05 / mf) if mf > 0 else 0.0
        matched[name] = (lambda b, t, dd, fn=fn, s=scale: clip(fn(b, t, dd) * s))
    print("=== mean fraction per rule (raw -> matched) ===")
    for name, fn in rules.items():
        print(f"  {name:<20}{mean_frac(fn) * 100:>7.2f}%  ->  "
              f"{mean_frac(matched[name]) * 100:>6.2f}%")

    print(f"\n=== full sample, bootstrap {args.paths} paths x {args.length} trades ===")
    print(f"  {'rule':<20}{'median':>12}{'p05':>10}{'p95':>14}{'P(<$10)':>9}{'medDD%':>9}")
    base = {}
    for name, fn in rules.items():
        o = boot(recs, fn, b0, args.paths, args.length)
        base[name] = o
        print(f"  {name:<20}${o['med']:>11.2f}${o['p05']:>9.2f}"
              f"${o['p95']:>13.2f}{o['ruin']:>9.1%}{o['dd']:>9.1f}")

    print(f"\n=== SAME AVERAGE EXPOSURE (5% of bankroll), {args.paths} paths ===")
    print("  a rule is smarter only if it beats flat 5% here")
    print(f"  {'rule':<20}{'median':>12}{'p05':>10}{'medDD%':>9}{'growth/DD':>11}")
    for name, fn in matched.items():
        o = boot(recs, fn, b0, args.paths, args.length)
        ratio = (o["med"] / abs(o["dd"])) if o["dd"] else float("inf")
        print(f"  {name:<20}${o['med']:>11.2f}${o['p05']:>9.2f}"
              f"{o['dd']:>9.1f}{ratio:>11.0f}")

    print("\n=== does signal strength predict per-trade return at all? ===")
    print("  (a smart rule can only pay off if it can)")
    for label, key in (("edge", "edge"), ("|z|", "mz")):
        vals = sorted(t[key] for t in trades)
        q1, q2, q3 = vals[len(vals) // 4], vals[len(vals) // 2], vals[3 * len(vals) // 4]
        for lo, hi, tag in ((-9, q1, "Q1"), (q1, q2, "Q2"), (q2, q3, "Q3"),
                            (q3, 9, "Q4")):
            sel = [t["pnl"] for t in trades if lo <= t[key] < hi]
            if sel:
                print(f"  {label:<5}{tag}  n={len(sel):>3}  mean {st.mean(sel):+.3f}  "
                      f"WR {sum(1 for t in trades if lo <= t[key] < hi and t['won']) / len(sel):.3f}")

    print("\n=== loss limits on top of flat 5% and kelly 0.25 ===")
    for label, fn in (("flat 5%", rules["flat 5%"]),
                      ("kelly 0.25", rules["kelly 0.25"])):
        print(f"  {label}")
        for cap in (None, 0.20, 0.10):
            for es in (None, 0.50, 0.30):
                o = boot(recs, fn, b0, args.paths, args.length,
                         daily_cap=cap, equity_stop=es)
                cl = f"daily {cap * 100:.0f}%" if cap else "no daily"
                el = f"equity stop {es * 100:.0f}%" if es else "no stop"
                print(f"    {cl:<14}{el:<22}median ${o['med']:>10.2f} "
                      f"p05 ${o['p05']:>8.2f}  DD {o['dd']:>7.1f}%")

    print(f"\n=== out-of-sample: choose on the first {args.train_frac:.0%}, "
          f"score on the rest (EXPOSURE-MATCHED) ===")
    cut = int(len(recs) * args.train_frac)
    train, test = recs[:cut], recs[cut:]
    print(f"  train {len(train)} trades, test {len(test)} trades")
    picks = []
    for name, fn in matched.items():
        tr = boot(train, fn, b0, args.paths, len(train))
        te = boot(test, fn, b0, args.paths, len(test))
        picks.append((name, tr, te))
        print(f"  {name:<20} train ${tr['med']:>10.2f}  ->  test ${te['med']:>10.2f}"
              f"   (test p05 ${te['p05']:>8.2f}, DD {te['dd']:>6.1f}%)")
    best = max(picks, key=lambda p: p[1]["med"])
    baseline = next(p for p in picks if p[0] == "flat 5%")
    print(f"\n  best on TRAIN : {best[0]}  train ${best[1]['med']:.2f} -> "
          f"TEST ${best[2]['med']:.2f}")
    print(f"  baseline      : flat 5%     train ${baseline[1]['med']:.2f} -> "
          f"TEST ${baseline[2]['med']:.2f}")
    gain = best[2]["med"] / baseline[2]["med"] - 1
    print(f"  out-of-sample gain from the smart rule: {gain:+.1%}")
    if gain <= 0.05:
        print("  VERDICT: does not beat flat 5% by a margin worth the extra")
        print("           moving parts. Keep flat 5% of bankroll, no daily cap")
        print("           (the 5-loss halt already bounds a day).")
    else:
        print("  VERDICT: candidate beat the baseline out of sample; check the")
        print("           parameter sensitivity before trusting it.")

    print("\n=== sensitivity: is the winner's optimum a peak or a plateau? ===")
    print("  tilt = (median|z| / |z|)^p, exposure-matched, train vs TEST")
    for p in (0.0, 0.5, 1.0, 1.5):
        for lo, hi in ((0.0, 1e9), (0.25, 4.0), (0.5, 2.0)):
            def tilt(b, t, dd, p=p, lo=lo, hi=hi):
                m = (med_mz / max(t["mz"], 0.05)) ** p
                return max(lo, min(hi, m))
            mf = st.mean(clip(0.05 * tilt(b0, t, 0.0)) for t in trades)
            sc = (0.05 / mf) if mf > 0 else 0.0
            fn = lambda b, t, dd, s=sc: clip(0.05 * tilt(b, t, dd) * s)
            tr = boot(train, fn, b0, args.paths, len(train))
            te = boot(test, fn, b0, args.paths, len(test))
            print(f"  p={p:<4} clip [{lo:.2f},{hi:>6.1f}]  train "
                  f"${tr['med']:>9.2f}  test ${te['med']:>9.2f}  "
                  f"testDD {te['dd']:>6.1f}%")
    print("  for reference, kelly k and flat pct (unmatched)")
    for k in (0.05, 0.10, 0.15, 0.20, 0.25, 0.35, 0.50):
        fn = lambda b, t, dd, k=k: min(MAX_FRAC, k * kelly_fraction(t["p"], t["q"]))
        tr = boot(train, fn, b0, args.paths, len(train))
        te = boot(test, fn, b0, args.paths, len(test))
        print(f"  kelly k={k:<5} train ${tr['med']:>10.2f}  test ${te['med']:>10.2f}")
    for pct in (0.02, 0.03, 0.05, 0.075, 0.10, 0.15):
        fn = lambda b, t, dd, p=pct: p
        tr = boot(train, fn, b0, args.paths, len(train))
        te = boot(test, fn, b0, args.paths, len(test))
        print(f"  flat {pct:<5.1%}     train ${tr['med']:>10.2f}  test ${te['med']:>10.2f}")


if __name__ == "__main__":
    main()
