"""Fix the model arm: sweep the two knobs that define its claim, on the real book.

Present tense, and the only clean experiment available: `data/recorded/quotes_arm.csv.gz`
holds the venue's spot AND its bid/ask on the same poll, with `rounds.csv.gz`
carrying the settlement. So the model arm's rules can be replayed against the
book it actually trades, with the fill priced at the ASK.

Baseline is the shipped config, read from the repo settings, not hardcoded:
    tau [60,180], quote [0.55,0.80], min_edge 0.01, edge_haircut 0.50,
    safety_margin 0.01, cooldown 1, one entry per round, flat $1.

The two knobs on trial
----------------------
1. `sigma_scale` multiplies the arm's own sigma before fair is computed. Live
   sigma measures 0.94x the realised 1-minute vol, so the shipped value is
   already slightly low, and fair = Phi(ln(S/K)/(sigma*sqrt(tau/60))) moves the
   whole book of decisions when it changes. This is the closest thing to a
   single root cause the arm has.
2. `edge_haircut` is how much of the model's disagreement with the venue is
   believed. 0.50 ships. It is the knob the whole claim rests on, so it is swept
   past both ends rather than around the current value.

Discipline: every cell is scored with the four-check battery, and a change is
only interesting if it ranks the same way in BOTH halves. A cell that wins
overall while flipping between halves is a fitted value, not an improvement.

Usage:
    python3 -m tools.model_arm_sweep
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcpredict.config import settings as S                       # noqa: E402
from btcpredict.edge.signal import evaluate, fee_rate_for_price   # noqa: E402
from btcpredict.pricing.digital import fair_value                 # noqa: E402

from tools.forecast_paired import build_sigma, load               # noqa: E402


def build(args) -> pd.DataFrame:
    """One row per (round, poll) inside the tau window, with sigma attached."""
    q, rounds, slugs = load(args.family)
    sig = build_sigma(q, slugs)
    idx = rounds.set_index("slug")
    rows = []
    for x in q.itertuples(index=False):
        if x.slug not in idx.index:
            continue
        if not (args.min_tau <= x.tau_s <= args.max_tau):
            continue
        rr = idx.loc[x.slug]
        if isinstance(rr, pd.DataFrame):
            rr = rr.iloc[0]
        if not rr.strike or rr.strike <= 0:
            continue
        minute = (int(x.ts_ms) // 1000) // 60 * 60
        sg = sig.get(minute, np.nan)
        if not np.isfinite(sg) or sg <= 0:
            continue
        rows.append({"slug": x.slug, "ts_ms": int(x.ts_ms), "tau": float(x.tau_s),
                     "spot": float(x.spot), "K": float(rr.strike),
                     "up_mid": ((x.up_bid + x.up_ask) / 2
                                + (1 - (x.down_bid + x.down_ask) / 2)) / 2,
                     "up_ask": float(x.up_ask), "down_ask": float(x.down_ask),
                     "sigma": float(sg), "day": int(rr.day),
                     "won_up": int(rr.winner == "Up")})
    d = pd.DataFrame(rows)
    d = d.sort_values(["day", "slug", "tau"], ascending=[True, True, False])
    return d


def run(d: pd.DataFrame, *, sigma_scale: float, haircut: float,
        min_quote: float, max_quote: float, min_edge: float,
        half_spread: float, cooldown: int,
        min_tau: float | None = None) -> pd.DataFrame:
    fills = []
    for x in d.itertuples(index=False):
        if min_tau is not None and x.tau < min_tau:
            continue
        fair = fair_value(x.spot, x.K, x.tau, x.sigma * sigma_scale)
        if not (0.0 < fair < 1.0):
            continue
        sg = evaluate(fair, x.up_mid, 1.0 - x.up_mid,
                      half_spread=half_spread,
                      safety_margin=S.safety_margin, min_edge=min_edge,
                      min_quote=min_quote, max_quote=max_quote,
                      edge_haircut=haircut)
        if not sg.act or sg.side is None:
            continue
        paid = x.up_ask if sg.side == "Up" else x.down_ask
        if not (0.0 < paid < 1.0):
            continue
        fee = fee_rate_for_price(paid)
        won = int(x.won_up == (1 if sg.side == "Up" else 0))
        fills.append({"day": x.day, "slug": x.slug, "paid": paid, "fee": fee,
                      "won": won, "edge": sg.edge, "fair": sg.fair,
                      "ret": won / paid - 1.0 - fee})
    f = pd.DataFrame(fills)
    if f.empty:
        return f
    f = (f.sort_values(["day", "slug"]).drop_duplicates("slug", keep="first")
           .sort_values(["day", "slug"]).reset_index(drop=True))
    if cooldown > 0:
        keep, skip = [], 0
        for i, row in f.iterrows():
            if skip:
                skip -= 1
                continue
            keep.append(i)
            if not row.won:
                skip = cooldown
        f = f.loc[keep].reset_index(drop=True)
    return f


def score(f: pd.DataFrame) -> dict:
    if f.empty or len(f) < 10:
        return {}
    r = f.ret.to_numpy()
    byd = pd.Series(r).groupby(f.day.to_numpy()).mean()
    days = sorted(byd.index)
    half = np.median(days)
    h1 = float(byd[byd.index <= half].mean())
    h2 = float(byd[byd.index > half].mean())
    dt = (float(byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd))))
          if len(byd) > 1 and byd.std(ddof=1) else 0.0)
    return {"n": len(f), "wr": float(f.won.mean()), "paid": float(f.paid.mean()),
            "ev": float(r.mean()), "day_t": dt, "h1": h1, "h2": h2,
            "drop": float(byd.drop(byd.idxmax()).mean()) if len(byd) > 1 else float("nan"),
            "days": len(days), "per_day": float(len(f) / max(len(days), 1))}


def row(label: str, st: dict, base: dict | None = None) -> None:
    if not st:
        print(f"  {label:<26}   no fills")
        return
    d = f"{st['ev'] - base['ev']:+.4f}" if base else "     -"
    # total money at a flat $1 stake is EV x fills; a cell that raises yield by
    # taking half as many trades has to be judged on this column, not on EV.
    print(f"  {label:<26}{st['n']:>6}{st['wr']:>8.1%}{st['paid']:>8.3f}"
          f"{st['ev']:>+10.4f}{d:>9}{st['ev'] * st['n']:>+9.1f}"
          f"{st['day_t']:>+8.2f}{st['h1']:>+9.4f}{st['h2']:>+9.4f}"
          f"{st['drop']:>+9.4f}{st['per_day']:>8.1f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", default="btc-updown-5m")
    ap.add_argument("--min-tau", type=float, default=S.min_tau_s)
    ap.add_argument("--max-tau", type=float, default=S.max_tau_s)
    ap.add_argument("--sigma-scales", type=float, nargs="*",
                    default=[0.90, 0.95, 1.00, 1.05, 1.10, 1.20])
    ap.add_argument("--haircuts", type=float, nargs="*",
                    default=[0.25, 0.50, 0.75, 1.00])
    ap.add_argument("--min-taus", type=float, nargs="*",
                    default=[60.0, 90.0, 120.0])
    ap.add_argument("--min-quotes", type=float, nargs="*",
                    default=[0.55, 0.60, 0.65])
    ap.add_argument("--max-quotes", type=float, nargs="*",
                    default=[0.75, 0.80, 0.85])
    a = ap.parse_args()

    d = build(a)
    if d.empty:
        print("no paired polls")
        return 1
    print(f"{a.family}: {d.slug.nunique():,} rounds | {len(d):,} paired polls | "
          f"{d.day.nunique()} days | tau [{a.min_tau:.0f},{a.max_tau:.0f}]")
    print(f"shipped: min_edge {S.min_edge:.3f} | haircut {S.edge_haircut:.2f} | "
          f"band [{S.min_quote:.2f},{S.max_quote:.2f}] | safety "
          f"{S.safety_margin:.3f} | half_spread {S.assumed_half_spread:.4f} | "
          f"cooldown {S.model_cooldown_after_loss}")

    base_kw = dict(half_spread=S.assumed_half_spread,
                   min_quote=S.min_quote, max_quote=S.max_quote,
                   min_edge=S.min_edge,
                   cooldown=S.model_cooldown_after_loss)
    hdr = (f"  {'cell':<26}{'fills':>6}{'WR':>8}{'paid':>8}{'EV/$1':>10}"
           f"{'vs base':>9}{'tot $':>9}{'day-t':>8}{'half1':>9}{'half2':>9}"
           f"{'drop':>9}{'f/day':>8}")

    base = score(run(d, sigma_scale=1.0, haircut=S.edge_haircut, **base_kw))
    print(f"\n=== BASELINE (shipped) ===")
    print(hdr)
    print("  " + "-" * 108)
    row("shipped", base)

    # Is the sample a normal week? And does the arm behave the same on both
    # halves of it? One week is exactly the smallest sample that can answer this.
    import datetime as dt
    bf = run(d, sigma_scale=1.0, haircut=S.edge_haircut, **base_kw)
    if not bf.empty:
        bf = bf.copy()
        bf["wk"] = [dt.datetime.fromtimestamp(int(x) * 86400, dt.timezone.utc
                                              ).weekday() for x in bf.day]
        bf["kind"] = np.where(bf.wk >= 5, "weekend", "weekday")
        print(f"\n=== WEEKDAY vs WEEKEND (shipped cell) ===")
        for k, g in bf.groupby("kind"):
            st = score(g)
            if st:
                print(f"  {k:<10}{st['n']:>6} fills{st['wr']:>8.1%}"
                      f"{st['paid']:>8.3f}{st['ev']:>+10.4f}"
                      f"{'':>9}{st['day_t']:>+8.2f}   days "
                      f"{g.day.nunique()}")
        print("  a split whose two halves disagree in SIGN is a regime, not a "
              "knob to tune.")

    print(f"\n=== OFAT: sigma scale ===")
    print("  (live sigma measures 0.94x the realised 1m vol; >1 = a less "
          "confident fair)")
    print(hdr)
    print("  " + "-" * 108)
    for s in a.sigma_scales:
        row(f"sigma x{s:.2f}", score(run(d, sigma_scale=s,
                                          haircut=S.edge_haircut, **base_kw)), base)

    print(f"\n=== OFAT: edge_haircut ===")
    print("  (how much of our disagreement with the venue is believed)")
    print(hdr)
    print("  " + "-" * 108)
    for h in a.haircuts:
        row(f"haircut {h:.2f}", score(run(d, sigma_scale=1.0, haircut=h,
                                          **base_kw)), base)

    print(f"\n=== OFAT: max_quote ===")
    print(hdr)
    print("  " + "-" * 108)
    for m in a.max_quotes:
        row(f"max_quote {m:.2f}", score(run(d, sigma_scale=1.0,
                                            haircut=S.edge_haircut,
                                            **{**base_kw, "max_quote": m})), base)

    print(f"\n=== OFAT: min_tau ===")
    print("  (tau 60-90s measured NEGATIVE on the earlier pass; is the short "
          "end a real loss?)")
    print(hdr)
    print("  " + "-" * 116)
    for t in a.min_taus:
        row(f"min_tau {t:.0f}", score(run(d, sigma_scale=1.0,
                                         haircut=S.edge_haircut,
                                         **{**base_kw, "min_tau": t})), base)

    print(f"\n=== OFAT: min_quote ===")
    print("  (the cheap edge of the band measured NEGATIVE; is that real?)")
    print(hdr)
    print("  " + "-" * 116)
    for m in a.min_quotes:
        row(f"min_quote {m:.2f}", score(run(d, sigma_scale=1.0,
                                           haircut=S.edge_haircut,
                                           **{**base_kw, "min_quote": m})), base)

    print(f"\n=== THE JOINT CELL: min_tau x min_quote (the two levers that "
          f"beat baseline in both halves on their own) ===")
    print(hdr)
    print("  " + "-" * 116)
    joint = {}
    for t in a.min_taus:
        for m in a.min_quotes:
            st = score(run(d, sigma_scale=1.0, haircut=S.edge_haircut,
                           **{**base_kw, "min_tau": t, "min_quote": m}))
            if st:
                joint[(t, m)] = st
    for k in sorted(joint, key=lambda k: -joint[k]["ev"] * joint[k]["n"]):
        st = joint[k]
        both = (st["h1"] > base["h1"]) and (st["h2"] > base["h2"])
        flag = "both halves" if both else ("one half" if (
            st["h1"] > base["h1"] or st["h2"] > base["h2"]) else "neither")
        row(f"min_tau {k[0]:.0f} min_quote {k[1]:.2f} [{flag}]", st, base)
    if joint:
        best = max(joint, key=lambda k: joint[k]["ev"] * joint[k]["n"])
        st = joint[best]
        both = (st["h1"] > base["h1"]) and (st["h2"] > base["h2"])
        print(f"\n  best by TOTAL money: min_tau {best[0]:.0f}, "
              f"min_quote {best[1]:.2f} -> EV {st['ev']:+.4f} x {st['n']} fills "
              f"= {st['ev'] * st['n']:+.1f} vs shipped "
              f"{base['ev'] * base['n']:+.1f}")
        print(f"  beats the shipped cell in BOTH halves: "
              f"{'YES' if both else 'NO'}")
        print(f"  fills/day {st['per_day']:.1f} vs {base['per_day']:.1f}")

    print(f"\n=== DOES sigma STACK ON THE BEST STRUCTURAL CELL? "
          f"(min_tau {a.min_taus[-1]:.0f}, min_quote "
          f"{a.min_quotes[1]:.2f}) ===")
    print("  (two structural levers picked by the both-halves test; adding a "
          "third free parameter on 7 days is the edge of what this sample can "
          "carry, so a NO here is a real answer)")
    print(hdr)
    print("  " + "-" * 116)
    stack = {}
    for s in a.sigma_scales:
        st = score(run(d, sigma_scale=s, haircut=S.edge_haircut,
                       **{**base_kw, "min_tau": a.min_taus[-1],
                          "min_quote": a.min_quotes[1]}))
        if st:
            stack[s] = st
            both = (st["h1"] > base["h1"]) and (st["h2"] > base["h2"])
            row(f"x{s:.2f} [{('both halves' if both else 'not both')}]", st, base)

    print(f"\n=== THE JOINT CELL: best sigma x best haircut ===")
    print(hdr)
    print("  " + "-" * 116)
    cells = {}
    for s in a.sigma_scales:
        for h in a.haircuts:
            st = score(run(d, sigma_scale=s, haircut=h, **base_kw))
            if st:
                cells[(s, h)] = st
    if cells:
        best = max(cells, key=lambda k: cells[k]["ev"])
        for k in sorted(cells, key=lambda k: -cells[k]["ev"])[:5]:
            row(f"sigma x{k[0]:.2f} haircut {k[1]:.2f}", cells[k], base)
        st = cells[best]
        same = (st["h1"] > base["h1"]) and (st["h2"] > base["h2"])
        print(f"\n  best cell: sigma x{best[0]:.2f}, haircut {best[1]:.2f} "
              f"-> EV {st['ev']:+.4f} vs shipped {base['ev']:+.4f}")
        print(f"  beats the shipped cell in BOTH halves: {'YES' if same else 'NO'}"
              f"   (half1 {st['h1']:+.4f} vs {base['h1']:+.4f}, "
              f"half2 {st['h2']:+.4f} vs {base['h2']:+.4f})")
        if not same:
            print("  -> a cell that does not beat the baseline in both halves is "
                  "a fitted value. Do not ship it.")
        print(f"  and it is {st['n']} fills against {base['n']}: check that the "
              f"gain is not just fewer, louder trades.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
