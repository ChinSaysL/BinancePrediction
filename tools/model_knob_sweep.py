"""Sweep the MODEL arm's blocking knobs on the recorded tape.

WHY THIS EXISTS. The model arm last filled 2026-09-14. Live, its entire
positive-edge population sits at tau 240-300s with a round travel of 0.0-2.2bps
-- and the shipped gates are `max_tau_s=210` (skips the first 90s of the round)
and `model_min_round_range_bps=5.0`. Both refuse exactly that region, so nothing
is ever sent. This tool prices the region instead of arguing about it.

HOW IT REPLAYS. Exactly the live rule, not a model of it:
  * `edge.signal.evaluate` -- the real gate, the real quote band, the real fee
  * `pricing.digital.fair_value` + the arm's own out-of-sample sigma
  * the real persistence streak (SAME side on consecutive 1 Hz polls)
  * the round-travel gate mirrored from `main.Bot._round_range_bps`
    (1e4*(max-min)/strike over the round's spot samples so far)
  * fills at the recording's EXECUTABLE ask through the live `limit_take` path,
    at 1s latency -- a decision whose limit does not cross is a MISSED round,
    not a trade (that is the difference between this and the optimistic replay)
  * ONE ENTRY PER ROUND (the first qualifying tick), which is the arm's own
    `cross_max_per_round=1` shape and the only unit a per-round battery is
    valid in

WHAT IT PRINTS PER CELL: n, /day, cover, WR, mean cost paid, EV/$1, pooled t,
day-clustered t, both halves, drop-best-day AND drop-best-entry. A knob is only
evidence when the DIRECTION repeats in both halves -- the absolute value of the
best cell on a pooled sample is a fitted number, not a finding.

NOT MODELLED (so not claimed): depth, queue position, the 1 Hz poll's blind
spot, and the venue's own reprice between decision and order. The level is an
upper bound; the GAIN between two cells is what the sweep is for.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# tools/ LAST so it wins: an earlier stray copy of `arm_overlap.py` at the repo
# root (the host had one, dated 2026-09-12) shadows this one when the root is
# inserted last, and the tool then replays a DIFFERENT rule than the file
# sitting next to it. Measured 2026-09-20: a new field added to every entry came
# back None from all 459 entries while the source on disk was correct.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arm_overlap as ao                                     # noqa: E402

if Path(ao.__file__).resolve().parent != Path(__file__).resolve().parent:
    raise SystemExit(
        f"arm_overlap loaded from {ao.__file__}, not from this tool's own "
        f"directory {Path(__file__).resolve().parent} -- a stray copy is "
        f"shadowing it; delete the stray, do not edit around it")
from btcpredict.config import settings                        # noqa: E402
from btcpredict.edge.signal import (                          # noqa: E402
    FeeModel, evaluate)
from btcpredict.pricing.digital import fair_value, moneyness_sd  # noqa: E402

# The live gate values, read from config rather than typed here. A sweep that
# pins its own copy ranks a system nobody runs: this file carried max_tau 210 /
# min_quote 0.45 / range 5-12 while the host ran 180 / 0.55 / 5-16 (measured
# 2026-09-20), and nothing in the output said so.
LIVE = dict(max_tau=settings.max_tau_s, min_tau=settings.min_tau_s,
            min_quote=settings.min_quote, max_quote=settings.max_quote,
            min_range=settings.model_min_round_range_bps,
            max_range=settings.model_max_round_range_bps)


def replay(rnd: dict, sigma: float, *, persistence: int,
           max_tau: float, min_tau: float | None,
           min_quote: float, max_quote: float,
           min_range: float, max_range: float,
           exec_mode: str = "limit_take", latency_s: float = 1.0,
           margin: float | None = None,
           haircut: float | None = None) -> dict | None:
    """The first round-qualifying entry under these knobs, or None."""
    lo_tau = settings.min_tau_s if min_tau is None else min_tau
    hc = settings.edge_haircut if haircut is None else haircut
    strike = rnd["strike"]
    streak_side, streak = None, 0
    seen: list[float] = []
    for i, (ts_ms, tau, spot, _up_px, _dn_px, up_ask, dn_ask) in enumerate(rnd["rows"]):
        if spot is not None and spot > 0:
            seen.append(spot)
        if (spot is None or not 0 < tau <= 300
                or up_ask is None or dn_ask is None):
            streak_side, streak = None, 0
            continue
        if not (lo_tau <= tau <= max_tau):
            continue
        # main.Bot._round_range_bps: the round's TRAVEL so far, and None (no
        # opinion) until there are two samples -- never a refusal.
        if max_range > 0 and len(seen) >= 2:
            rng = 1e4 * (max(seen) - min(seen)) / strike
            if not (min_range <= rng <= max_range):
                continue
        fair_up = fair_value(spot, strike, tau, sigma)
        sig = evaluate(
            fair_up, up_ask, dn_ask, fee=None, half_spread=0.0,
            safety_margin=settings.safety_margin, fee_model=FeeModel.NOTIONAL,
            max_quote=max_quote, min_quote=min_quote,
            min_edge=settings.min_edge, edge_haircut=hc)
        if not sig.act or sig.side is None:
            streak_side, streak = None, 0
            continue
        if sig.side == streak_side:
            streak += 1
        else:
            streak_side, streak = sig.side, 1
        if streak < persistence:
            continue
        fair_side = fair_up if sig.side == "Up" else 1.0 - fair_up
        return ao._execute(rnd, i, sig.side, fair_side,
                           sig.ev_per_dollar or 0.0,
                           f"model edge {sig.edge:+.3f}", margin=margin,
                           mz=moneyness_sd(spot, strike, tau, sigma),
                           exec_mode=exec_mode, latency_s=latency_s)
    return None


def drop_best_day(entries: list[dict]) -> float:
    by: dict[int, list[float]] = {}
    for e in entries:
        by.setdefault(e["day"], []).append(e["ret"])
    if len(by) < 2:
        return float("nan")
    worst = max(by, key=lambda d: st.fmean(by[d]))
    rest = [r for d, v in by.items() if d != worst for r in v]
    return st.fmean(rest) if rest else float("nan")


def row(label: str, entries: list[dict], rounds: int, days: float) -> None:
    if not entries:
        print(f"  {label:<28} no entries")
        return
    rets = [e["ret"] for e in entries]
    by: dict[int, list[float]] = {}
    for e in entries:
        by.setdefault(e["day"], []).append(e["ret"])
    day_means = [st.fmean(v) for v in by.values()]
    order = sorted(entries, key=lambda e: e["day"])
    half = len(order) // 2
    h1 = st.fmean([e["ret"] for e in order[:half]])
    h2 = st.fmean([e["ret"] for e in order[half:]])
    best = max(entries, key=lambda e: e["ret"])
    drop1 = st.fmean([e["ret"] for e in entries if e is not best])
    wr = 100 * sum(1 for e in entries if e["won"]) / len(entries)
    cost = st.fmean([e["cost"] for e in entries])
    if len(entries) < 5:
        print(f"  {label:<28} n={len(entries):<4} too few to read")
        return
    print(f"  {label:<28} n={len(entries):<5} {len(entries)/days:5.1f}/d  "
          f"cov {100*len(entries)/rounds:4.0f}%  WR {wr:5.1f}%  "
          f"paid {cost:.3f}  EV {st.fmean(rets):+.4f}/$1  "
          f"t {ao.tstat(rets):+5.2f}  day-t {ao.tstat(day_means):+5.2f}  "
          f"H {h1:+.3f}/{h2:+.3f}  dDay {drop_best_day(entries):+.4f}  "
          f"d1 {drop1:+.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--exec", choices=["limit_take", "market"],
                    default="limit_take")
    ap.add_argument("--latency-s", type=float, default=1.0)
    ap.add_argument("--persistence", type=int, default=None,
                    help="default: settings.signal_persistence (live)")
    ap.add_argument("--mode", default="all",
                    choices=["all", "ofat", "joint", "live", "halves",
                             "fair"],
                    help="which grid to run")
    ap.add_argument("--since-ms", type=int, default=0,
                    help="only rounds starting at/after this epoch ms -- for "
                         "reconciling the replay against a live run")
    ap.add_argument("--exclude-pm", action="store_true",
                    help="drop Polymarket-era rounds (a different settlement "
                         "rule, and the arm was switched off then) so a "
                         "halves test spans ONE rule")
    ap.add_argument("--dump", type=int, default=0,
                    help="print the first N entries (slug, tau, side, px)")
    ap.add_argument("--sigma-scale", type=float, default=1.0,
                    help="multiply the out-of-sample sigma by this factor. The "
                         "live estimator applies an hour-of-day seasonality "
                         "multiplier, so its sigma runs above the replay's; "
                         "pricing the edge at 1.0 alone overstates it.")
    args = ap.parse_args()
    persist = (settings.signal_persistence if args.persistence is None
               else args.persistence)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rs = ao.load(con)
    if args.since_ms:
        rs = [r for r in rs if r["start_ms"] >= args.since_ms]
    if args.exclude_pm:
        pm = {row[0] for row in con.execute(
            "SELECT slug FROM rounds WHERE feed_provider LIKE 'chainlink-twap%'")}
        before = len(rs)
        rs = [r for r in rs if r["slug"] not in pm]
        print(f"excluded {before - len(rs)} Polymarket-era rounds")
    print(f"loaded {len(rs)} settled rounds with a quote tape "
          f"({rs[0]['slug']} .. {rs[-1]['slug']})")
    fam: dict[str, int] = {}
    for r in rs:
        k = r["slug"].rsplit("-", 1)[0]
        fam[k] = fam.get(k, 0) + 1
    print("family mix: " + ", ".join(f"{k} {v}" for k, v in
                                     sorted(fam.items(), key=lambda x: -x[1])))
    span_h = (rs[-1]["start_ms"] - rs[0]["start_ms"]) / 3.6e6
    days = max(span_h / 24.0, 1e-9)
    print(f"span {span_h:.1f}h = {days:.2f} days | live: max_tau "
          f"{settings.max_tau_s:g} min_quote {settings.min_quote:g} max_quote "
          f"{settings.max_quote:g} persistence {persist} exec {args.exec} "
          f"latency {args.latency_s:g}s")
    sigmas = [ao.rolling_sigma(rs, i) * args.sigma_scale
              for i in range(len(rs))]
    if args.sigma_scale != 1.0:
        print(f"sigma scaled x{args.sigma_scale:g} "
              f"(mean {sum(sigmas)/len(sigmas)*1e4:.2f} bps/min)")

    base = dict(persistence=persist, exec_mode=args.exec,
                latency_s=args.latency_s)

    def run2(**over) -> list[dict]:
        kw = dict(LIVE)
        kw.update(over)
        out = []
        for r, sg in zip(rs, sigmas):
            e = replay(r, sg, **base, **kw)
            if e:
                out.append(e)
        return out

    if args.mode == "fair":
        # Is a `fair` cut a FILTER worth shipping, or noise? Two questions, and
        # they are not the same one: (a) does the LOW bucket rank above the
        # HIGH one in BOTH halves (a parameter must rank the same twice, or it
        # is a fitted split of one sample), and (b) what does the HIGH bucket
        # earn on its own -- if it is positive at a flat stake, skipping it
        # destroys money however good the per-dollar comparison looks.
        print("\n=== FAIR BUCKETS (the model's fair for the side bought) ===")
        ent = [e for e in run2() if e.get("fair") is not None]
        print("  both buckets are scored on their OWN ret, so `tot $` is what a "
              "flat $1 stake actually earned there.")
        for cut in (0.60, 0.65, 0.70, 0.75, 0.80):
            lo = [e for e in ent if e["fair"] <= cut]
            hi = [e for e in ent if e["fair"] > cut]
            for lab, g in ((f"fair <= {cut:.2f}", lo), (f"fair >  {cut:.2f}", hi)):
                if len(g) < 5:
                    print(f"  {lab:<14} n={len(g)} too few")
                    continue
                order = sorted(g, key=lambda e: e["day"])
                h = len(order) // 2
                print(f"  {lab:<14} n={len(g):<5} WR "
                      f"{100*sum(1 for e in g if e['won'])/len(g):5.1f}%  "
                      f"EV {st.fmean([e['ret'] for e in g]):+.4f}/$1  "
                      f"t {ao.tstat([e['ret'] for e in g]):+5.2f}  "
                      f"H {st.fmean([e['ret'] for e in order[:h]]):+.3f}/"
                      f"{st.fmean([e['ret'] for e in order[h:]]):+.3f}  "
                      f"dDay {drop_best_day(g):+.4f}  "
                      f"tot ${sum(e['ret'] for e in g):+7.2f}")
            print()
        return

    print("\n=== BASELINE (live knobs) ===")
    row("live", run2(), len(rs), days)
    if args.dump:
        kept = run2()[:args.dump]
        print("  first entries: slug / tau / side / px / why")
        for e in kept:
            print(f"    {e['slug']} tau {e['tau']:.0f} {e['side']:<5} "
                  f"px {e['px']:.3f} won {e['won']} | {e['why']}")

    if args.mode in ("all", "ofat"):
        print("\n=== OFAT: max_tau (round window; live 210) ===")
        for v in (210.0, 240.0, 270.0, 300.0):
            row(f"max_tau {v:g}", run2(max_tau=v), len(rs), days)

        print("\n=== OFAT: min_quote (live 0.45) ===")
        for v in (0.45, 0.35, 0.25, 0.15, 0.05):
            row(f"min_quote {v:g}", run2(min_quote=v), len(rs), days)

        print("\n=== OFAT: max_quote (live 0.80) ===")
        for v in (0.80, 0.90, 0.95):
            row(f"max_quote {v:g}", run2(max_quote=v), len(rs), days)

        print("\n=== OFAT: model_min_round_range_bps (live 5.0) ===")
        for v in (5.0, 3.0, 2.0, 0.0):
            row(f"min_range {v:g}", run2(min_range=v), len(rs), days)

        print("\n=== OFAT: model_max_round_range_bps (live 12.0) ===")
        for v in (12.0, 20.0, 0.0):
            row(f"max_range {v:g}", run2(max_range=v), len(rs), days)

        print("\n=== OFAT: min_tau (live 60) ===")
        for v in (60.0, 30.0, 5.0):
            row(f"min_tau {v:g}", run2(min_tau=v), len(rs), days)

    if args.mode in ("all", "joint"):
        print("\n=== JOINT: open the round-open region (tau window x travel) ===")
        for mt in (210.0, 270.0, 300.0):
            for (lo, hi) in ((5.0, 12.0), (5.0, 0.0), (0.0, 0.0)):
                row(f"tau<={mt:g} range[{lo:g},{hi:g}]",
                    run2(max_tau=mt, min_range=lo, max_range=hi), len(rs), days)

        print("\n=== JOINT: round-open region x quote band ===")
        for mq in (0.45, 0.25, 0.05):
            row(f"open, min_quote {mq:g}",
                run2(max_tau=300.0, min_range=0.0, max_range=0.0,
                     min_quote=mq), len(rs), days)

    if args.mode in ("all", "halves"):
        mid = len(rs) // 2
        cells = [
            ("live range[5,12] tau<=210", dict(max_tau=210.0, min_range=5.0, max_range=12.0)),
            ("max_range 20", dict(max_tau=210.0, min_range=5.0, max_range=20.0)),
            ("max_range 0", dict(max_tau=210.0, min_range=5.0, max_range=0.0)),
            ("min_tau 30", dict(max_tau=210.0, min_range=5.0, max_range=12.0, min_tau=30.0)),
            ("min_tau 5", dict(max_tau=210.0, min_range=5.0, max_range=12.0, min_tau=5.0)),
            ("min_range 0", dict(max_tau=210.0, min_range=0.0, max_range=0.0)),
            ("open region tau<=300", dict(max_tau=300.0, min_range=0.0, max_range=0.0)),
        ]
        for label, sub, sg_sub in (("FIRST half", rs[:mid], sigmas[:mid]),
                                   ("SECOND half", rs[mid:], sigmas[mid:])):
            print(f"\n=== {label} (direction check: the ranking must repeat) ===")
            for name, over in cells:
                kw = dict(LIVE)
                kw.update(over)
                out = []
                for r, sg in zip(sub, sg_sub):
                    e = replay(r, sg, **base, **kw)
                    if e:
                        out.append(e)
                row(name, out, len(sub), days / 2)


if __name__ == "__main__":
    main()
