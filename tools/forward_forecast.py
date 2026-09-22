"""7-day PnL forecast for the config that is ACTUALLY running.

WHY IT IS BUILT THIS WAY. `tools/live_config_projection.py` already projects a
30-day path, and every one of its inputs is stale: it pins the 2026-09-13 rule
(`stake_fraction` 0.0224 of a COMPOUNDING bankroll, `stake_cap_usdt` off), runs
model + cross, and carries the model arm as a fixed `(170.4/day, win 0.526,
EV +0.0452)` measured before the duplicate-bar sigma fix. The host today runs
(measured 2026-09-20 off `.env`):

    entry_mode=model, model_families=btc-updown-5m, cross_enabled=false
    sizing_mode=edge, min_stake=max_stake=stake_cap=$1.00
        -> every trade is EXACTLY $1.00. No compounding, no tilt, no cap bite.
    min_edge 0.01 / edge_haircut 0.50      (raw edge bar 0.020)
    WEEKEND_OVERRIDES=true  Fri 22:00 -> Sun 21:00 UTC:
        min_edge 0.06 / edge_haircut 1.00  (raw edge bar 0.060, 3x the weekday)
        max_quote 0.85 (not 0.80), persistence 3 (not 2), sigma x1.25,
        tau window 60-240 (not 60-180)
    model_cooldown_after_loss=1, model range gate [5,16] bps, weekend-agnostic

So a parametric simulation is the wrong instrument twice over: the stake is
flat (nothing to compound) and the arm's whole character changes on a weekend
that the old tool does not know exists.

METHOD. Replay the real rule tick by tick -- `model_knob_sweep.replay`, i.e.
the real `edge.signal.evaluate` gate, the real fee curve, the sigma the BOT
recorded (`decisions.sigma_1m`, forward-filled), `limit_take` fills at 1s
latency, ONE entry per round -- with the weekday/weekend knob set chosen per
round from `main.is_weekend_utc`. Then bootstrap whole DAYS, not trades:
samples within a day share a regime, so resampling trades i.i.d. would shrink
the interval to a value the tape does not support.

WHAT THIS IS NOT. It is an extrapolation of the last ~13 days of one config era
onto the next 7. The day-clustered t of the same stream is printed FIRST,
because if the mean is not distinguishable from zero then the median of this
forecast is not a prediction -- only the spread is informative. Depth, queue
position and the venue's reprice between decision and order are not modelled,
and every one of them costs money, so the LEVEL is an optimistic bound.
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# tools/ LAST so it wins -- see the shadowing note in model_knob_sweep.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arm_overlap as ao                                       # noqa: E402

if Path(ao.__file__).resolve().parent != Path(__file__).resolve().parent:
    raise SystemExit(
        f"arm_overlap loaded from {ao.__file__}, not from this tool's own "
        f"directory {Path(__file__).resolve().parent} -- a stray copy is "
        f"shadowing it")

import model_knob_sweep as mks                                 # noqa: E402
from btcpredict.config import settings                         # noqa: E402
from btcpredict.main import is_weekend_utc                     # noqa: E402


def _g(name: str, default=0.0) -> float:
    v = getattr(settings, name, None)
    return default if v is None else float(v)


def cohort_knobs(rnd: dict) -> dict:
    """The live knob set for THIS round, weekend-aware, straight from config.

    `main.Bot._wk` is asked with no timestamp, so the live bot decides the
    cohort from the wall clock at the moment of the decision. Replaying a
    recorded round has only its own clock, so the round's start is used; the
    window is Fri 22:00 -> Sun 21:00 UTC, so the worst this can misassign is
    the one round straddling a boundary.
    """
    wk = bool(getattr(settings, "weekend_overrides", False)) and \
        is_weekend_utc(rnd["start_ms"])

    def k(name: str, fallback: float) -> float:
        return _g(f"weekend_{name}", _g(name, fallback)) if wk else _g(name, fallback)

    return dict(
        weekend=wk,
        persistence=int(k("signal_persistence", 2)),
        max_tau=k("max_tau_s", 180.0),
        min_tau=k("min_tau_s", 60.0),
        min_quote=k("min_quote", 0.55),
        max_quote=k("max_quote", 0.80),
        min_edge=k("min_edge", 0.01),
        haircut=k("edge_haircut", 1.0),
        sigma_mult=(k("sigma_mult", 1.0) if wk else 1.0),
        # NOT weekend-aware in the live path: `_wk` is never called for the
        # range gate, so the [5,16] bps activity filter is identical on a
        # weekend and changing it here would price a system nobody runs.
        min_range=_g("model_min_round_range_bps", 0.0),
        max_range=_g("model_max_round_range_bps", 0.0),
    )


def stake_for() -> tuple[float, str]:
    """The live stake, read from config, with the reason it is a constant.

    `sizing_mode=edge` reaches `stake_for_edge`, which is bounded by
    [min_stake_usdt, max_stake_usdt]; `_stake_for` then applies the low-fair
    multiplier and clamps with `stake_cap_usdt` LAST. When the floor and every
    ceiling coincide the rule degenerates to a constant, and this returns it
    with the pair of numbers that prove it -- so a later config edit that
    breaks the equality fails loudly here instead of being silently ignored.
    """
    lo, hi = _g("min_stake_usdt", 1.0), _g("max_stake_usdt", 1e9)
    cap = _g("stake_cap_usdt", 0.0)
    mult = _g("model_low_fair_stake_mult", 1.0)
    cut = _g("model_low_fair_cut", 0.0)
    bounds = [b for b in (hi, cap) if b > 0]
    if mult != 1.0 and cut > 0:
        return 0.0, (f"REFUSING: model_low_fair_stake_mult={mult:g} at cut "
                     f"{cut:g} makes the stake a function of `fair`, which "
                     f"this flat-stake model does not carry. Set it to 1.0 or "
                     f"extend the tool.")
    if bounds and abs(min(bounds) - lo) < 1e-9:
        return lo, (f"flat ${lo:.2f} (min_stake=max_stake={hi:g}, "
                    f"stake_cap={cap:g})")
    return 0.0, (f"REFUSING: stake is not a constant -- min_stake {lo:g}, "
                 f"max_stake {hi:g}, cap {cap:g}. This tool only models the "
                 f"flat rule; a fraction/edge ramp needs its own tool.")


def build_stream(con, *, families: list[str], exclude_pm: bool) -> list[dict]:
    """Every round-qualifying entry under the live rule, chronological."""
    rs = ao.load(con)
    before = len(rs)
    if families:
        rs = [r for r in rs
              if any(r["slug"].startswith(f) for f in families)]
    print(f"rounds: {len(rs)} of {before} match {families or 'all families'}")
    if exclude_pm:
        pm = {row[0] for row in con.execute(
            "SELECT slug FROM rounds WHERE feed_provider LIKE 'chainlink-twap%'")}
        keep = [r for r in rs if r["slug"] not in pm]
        print(f"  dropped {len(rs) - len(keep)} Polymarket-era rounds "
              f"(different venue: not the book this arm trades)")
        rs = keep

    live_sig = mks.load_live_sigma(con)
    have = sum(1 for r in rs if r["slug"] in live_sig)
    print(f"  live sigma recorded for {have}/{len(rs)} rounds "
          f"({100*have/len(rs):.0f}%); the rest fall back to rolling_sigma")

    entries: list[dict] = []
    n_wk = 0
    for i, r in enumerate(rs):
        k = cohort_knobs(r)
        sg = ao.rolling_sigma(rs, i)
        sig_series = live_sig.get(r["slug"])
        if k["weekend"]:
            n_wk += 1
            sg *= k["sigma_mult"]
            if sig_series:
                sig_series = [(t, s * k["sigma_mult"]) for t, s in sig_series]
        e = mks.replay(r, sg, persistence=k["persistence"], max_tau=k["max_tau"],
                       min_tau=k["min_tau"], min_quote=k["min_quote"],
                       max_quote=k["max_quote"], min_range=k["min_range"],
                       max_range=k["max_range"], haircut=k["haircut"],
                       live_sig=sig_series)
        if e:
            entries.append(dict(e, weekend=k["weekend"],
                                start_ms=r["start_ms"]))
    entries.sort(key=lambda e: e["ts_ms"])
    print(f"  {n_wk} of {len(rs)} rounds fall in the weekend window")
    return entries


def apply_cooldown(entries: list[dict], n: int) -> list[dict]:
    """The arm's own `model_cooldown_after_loss`: skip the next N SIGNALS.

    Counted per round-equivalent signal, not per poll (`main.py` line 2302
    arms it on the settled loss and spends it on the next qualifying
    decision), so a loss removes the FOLLOWING trade from the book, not this
    one. This matters for a forecast: it cuts the trade count, and the trade
    it removes is the one right after a loss.
    """
    if n <= 0:
        return entries
    out, left = [], 0
    for e in entries:
        if left > 0:
            left -= 1
            continue
        out.append(e)
        if not e["won"]:
            left = n
    return out


def simulate(path_days: list[list[dict]], start: float, stake: float,
             cooldown: int, ) -> tuple[float, float, int, int]:
    """Run one resampled path. Returns (end balance, pnl, trades, skipped)."""
    bank = start
    pnl = 0.0
    trades = 0
    skipped = 0
    left = 0
    for day in path_days:
        for e in day:
            if left > 0:
                left -= 1
                continue
            if bank < stake:              # cannot fund the venue minimum
                skipped += 1
                continue
            p = stake * e["ret"]
            bank += p
            pnl += p
            trades += 1
            if not e["won"] and cooldown > 0:
                left = cooldown
    return bank, pnl, trades, skipped


def day_t(entries: list[dict]) -> tuple[float, float, int]:
    by: dict[int, list[float]] = defaultdict(list)
    for e in entries:
        by[e["day"]].append(e["ret"])
    means = [st.fmean(v) for v in by.values()]
    return (ao.tstat([e["ret"] for e in entries]),
            ao.tstat(means) if len(means) > 1 else float("nan"), len(means))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--paths", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=20260920)
    ap.add_argument("--start", type=float, default=None,
                    help="bankroll to start from (default: read the ledger)")
    ap.add_argument("--include-pm", action="store_true",
                    help="keep Polymarket-era rounds (a different venue and a "
                         "different settlement rule -- off by default)")
    a = ap.parse_args()

    stake, why = stake_for()
    print(f"STAKE RULE: {why}")
    if stake <= 0:
        return 2
    if a.start is None:
        a.start = _g("stake_usdt", 7.0)

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    fams = [f.strip() for f in
            str(getattr(settings, "model_families", "") or "").split(",")
            if f.strip()]
    print(f"model_families={fams or 'ALL'} | entry_mode="
          f"{getattr(settings, 'entry_mode', '?')} | "
          f"cross_enabled={getattr(settings, 'cross_enabled', '?')}")
    entries = build_stream(con, families=fams, exclude_pm=not a.include_pm)
    cd = int(_g("model_cooldown_after_loss", 0))
    entries = apply_cooldown(entries, cd)
    if not entries:
        print("no entries -- nothing to forecast")
        return 1

    days: dict[int, list[dict]] = defaultdict(list)
    for e in entries:
        days[e["day"]].append(e)
    day_list = sorted(days)
    t, dt, nd = day_t(entries)

    print(f"\n=== THE OBSERVED STREAM (what the forecast is made of) ===")
    print(f"  n={len(entries)} trades over {nd} days "
          f"({len(entries)/nd:.1f}/day)  "
          f"WR {100*sum(1 for e in entries if e['won'])/len(entries):.1f}%  "
          f"EV {st.fmean([e['ret'] for e in entries]):+.4f}/$1  "
          f"paid {st.fmean([e['cost'] for e in entries]):.3f}")
    print(f"  pooled t {t:+.2f}   DAY-CLUSTERED t {dt:+.2f}"
          f"   sd/trade {st.stdev([e['ret'] for e in entries]):.3f}   "
          f"cooldown {cd}")
    wk = [e for e in entries if e["weekend"]]
    wd = [e for e in entries if not e["weekend"]]
    for lbl, g in (("weekday", wd), ("weekend", wk)):
        if not g:
            print(f"  {lbl:<8} n=0")
            continue
        gt, gdt, gnd = day_t(g)
        print(f"  {lbl:<8} n={len(g):<4} {len(g)/max(gnd,1):5.1f}/d  "
              f"WR {100*sum(1 for e in g if e['won'])/len(g):5.1f}%  "
              f"paid {st.fmean([e['cost'] for e in g]):.3f}  "
              f"EV {st.fmean([e['ret'] for e in g]):+.4f}/$1  "
              f"day-t {gdt:+5.2f}")

    print(f"\n  per-day PnL at ${stake:.2f}/trade:")
    for d in day_list:
        pnl = sum(stake * e["ret"] for e in days[d])
        print(f"    day {d}  n={len(days[d]):<3} "
              f"{pnl:+7.2f}   (cum {sum(stake * e['ret'] for dd in day_list if dd <= d for e in days[dd]):+7.2f})")

    print(f"\n=== DAY-BLOCK BOOTSTRAP ({a.paths} paths, resampling whole days "
          f"from the {nd} above) ===")
    rng = __import__("random").Random(a.seed)
    print(f"  start ${a.start:.2f}, ${stake:.2f}/trade, "
          f"cooldown {cd}, {a.days}-day horizon")
    print(f"  {'days':>4} {'p10':>9} {'p25':>9} {'median':>9} {'p75':>9} "
          f"{'p90':>9} {'mean':>9} {'P(loss)':>8} {'P(ruin)':>8} {'trades':>8}")
    for horizon in sorted({1, a.days, 30}):
        ends, pnls, trs, ruins = [], [], [], 0
        for _ in range(a.paths):
            path = [days[rng.choice(day_list)] for _ in range(horizon)]
            end, pnl, ntr, skipped = simulate(path, a.start, stake, cd)
            ends.append(end)
            pnls.append(pnl)
            trs.append(ntr)
            if end < stake:
                ruins += 1
        ends.sort()
        pnls.sort()

        def pc(x, q):
            return x[min(len(x) - 1, max(0, int(q * len(x))))]
        print(f"  {horizon:4d} "
              f"{pc(ends, .10):9.2f} {pc(ends, .25):9.2f} {pc(ends, .50):9.2f} "
              f"{pc(ends, .75):9.2f} {pc(ends, .90):9.2f} "
              f"{st.fmean(ends):9.2f} "
              f"{100*sum(1 for p in pnls if p < 0)/len(pnls):7.1f}% "
              f"{100*ruins/len(pnls):7.1f}% {st.fmean(trs):8.1f}")

    print(f"\n  the SAME horizon priced with the i.i.d. trade bootstrap, for")
    print(f"  contrast -- it is the narrower (wrong) interval:")
    for horizon in (a.days,):
        ends = []
        for _ in range(a.paths):
            end = a.start
            bank = a.start
            n = 0
            for _ in range(horizon):
                for _ in range(round(len(entries) / nd)):
                    e = entries[rng.randrange(len(entries))]
                    if bank < stake:
                        break
                    bank += stake * e["ret"]
                    n += 1
            ends.append(bank)
        ends.sort()
        print(f"  {horizon:4d} p10 {ends[int(.1*len(ends))]:9.2f}  "
              f"median {ends[int(.5*len(ends))]:9.2f}  "
              f"p90 {ends[int(.9*len(ends))]:9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
