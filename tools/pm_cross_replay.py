"""Replay the CROSS arm over the POLYMARKET era, under the rules that ship.

WHAT THIS ANSWERS: "if the current gates had been in place, what would every
Polymarket trade have done?" -- and it answers it on the record, not on a model.

THREE THINGS IT DOES THAT THE EARLIER TOOLS DID NOT:

 1. BOTH INTERVALS. `pm_twap_trigger_check.py` filters `-5m-` out of its query;
    the arm trades 6 x 5m AND 3 x 15m, and the 15m legs are half its volume.
 2. THE LEVEL THE ARM ACTUALLY USES. `rounds.strike` is the spot at the open
    instant; the venue pays on `TWAP60(bell) >= TWAP60(open)` and the arm reads
    the trailing 60 s mean at the boundary (`cross_ref_twap`, shipped ON). The
    quote tape is contiguous across a family's rounds, so the window before a
    round's open is the PREVIOUS round's last 60 s -- that is computable here,
    and it is what the bot reads from its own 40-minute poll buffer.
 3. THE TRIGGER IS IMPORTED, NOT REWRITTEN. `crossed_side`, `twap_forecast` and
    `twap_reference` come from `btcpredict.main`, so a backtest cannot drift
    from the arm it claims to describe (the rule `pm_cross_backtest.py` states).

WHAT IT DOES NOT MODEL, because the tape does not carry it: the book BEYOND the
best ask (a $1 order sweeps, and the sweep price is what the live arm pays), the
FAK miss rate (10 fills against 54 misses is live evidence, not tape evidence),
and the exposure cap when it is on. Entries here pay the recorded ask on that
side, so they are TAKER entries with no slippage model -- optimistic by the
sweep premium, honest about the spread.

Usage (on the VPS, analysis venv -- the bot venv has no numpy):
    /root/analysis-venv/bin/python tools/pm_cross_replay.py
    /root/analysis-venv/bin/python tools/pm_cross_replay.py --by-family
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/BinancePrediction")
from btcpredict.main import (TWAP_REF_MIN_N, crossed_side,  # noqa: E402
                             twap_forecast, twap_reference)

DB = "/root/BinancePrediction/data/btcpredict.db"
PM_PROVIDER_LIKE = "chainlink-twap%"
FEE = lambda p: 0.07 * p * (1 - p)          # per share, taker


def shipped_look_s() -> float:
    """The look the ARM runs -- the default of every `--look-s` in these tools.

    It was the literal 2.0 here and in the three siblings while the arm shipped
    `cross_look_s = 1.0`, so the margin floor AND the tau window were both
    ranked on a CROSSING RULE THE BOT DOES NOT RUN. `crossed_side` fires on a
    different pair of samples at 1 s than at 2 s, so that is not a detail.
    `pm_tick_floor_check.py` already read 1.0 by hand; this makes the shipped
    value structural, the same rule that makes the TRIGGER imported here rather
    than rewritten.
    """
    from btcpredict.config import settings
    return float(settings.cross_look_s)


def shipped_hold_s() -> float:
    """The PERSISTENCE the arm runs, for the reason `shipped_look_s` gives: a
    sweep that ranks a trigger the bot does not run answers nothing."""
    from btcpredict.config import settings
    return float(settings.cross_hold_s)


def family_of(slug: str) -> str:
    return str(slug).rsplit("-", 1)[0]


def load(db: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    r = pd.read_sql_query("""
      select slug, start_ms, end_ms, strike, winner, feed_provider
      from rounds
      where strike is not null and winner is not null
    """, con)
    q = pd.read_sql_query("""
      select slug, ts_ms, tau_s, spot, up_ask, down_ask
      from quotes
      where spot is not null and ts_ms is not null
      order by ts_ms
    """, con)
    con.close()
    r = r[r.feed_provider.fillna("").str.contains("twap", case=False)]
    r = r[r.winner.isin(["Up", "Down"])]
    q = q[q.slug.isin(set(r.slug))]
    return r.sort_values("start_ms"), q.sort_values("ts_ms")


def tape_for(q: pd.DataFrame, slug: str) -> list[tuple[int, float]]:
    g = q[q.slug == slug]
    return [(int(t), float(p)) for t, p in zip(g.ts_ms, g.spot) if p and p > 0]


def rows_for(q: pd.DataFrame, slug: str) -> pd.DataFrame:
    g = q[q.slug == slug].sort_values("ts_ms")
    return g


def precompute(Q: pd.DataFrame, r, look_s: float, px_lo: float, px_hi: float,
               tau_lo: float, tau_hi: float) -> dict | None:
    """Everything every variant needs, computed ONCE per round.

    The level and the forecast do not depend on the floor or the signal choice,
    so computing them inside the variant loop made the first version re-walk the
    whole family tape five times per round. The tape is also CLIPPED to the
    round: the 60 s window of every tick the arm can act on lies inside
    `[start - 60 s, last row]`, and `twap_reference` needs the 60 s BEFORE the
    open -- which is the previous round's tail, hence the family-keyed frame.
    """
    fam = family_of(r.slug)
    g = Q[Q.fam == fam]
    rows = g[(g.ts_ms >= int(r.start_ms)) & (g.ts_ms <= int(r.end_ms))
             & g.tau_s.between(tau_lo - 1, tau_hi + 1)]
    if rows.empty:
        return None
    last_ts = int(rows.ts_ms.iloc[-1])
    win = g[(g.ts_ms >= int(r.start_ms) - 60_000) & (g.ts_ms <= last_ts)]
    tape = [(int(t), float(p)) for t, p in zip(win.ts_ms, win.spot)
            if p and p > 0]
    if len(tape) < 8:
        return None
    level = twap_reference(tape, int(r.start_ms))
    level_src = "twap60"
    if level is None:                       # the live fallback, same as the bot
        level = float(r.strike)
        level_src = "strike"
    if not level or level <= 0:
        return None
    # THE PROJECTION AS OF THE ROUND'S OWN CLOCK, not today's: `tau_now + age`
    # reproduces `(end - ts)/1000` for every tick. With the wall clock every
    # tick's tau is millions of seconds, every value collapses to the spot, and
    # the fc variant silently becomes the spot variant.
    fc = twap_forecast(tape, float(rows.tau_s.iloc[-1]), now_ms=last_ts)
    return dict(r=r, level=level, level_src=level_src, rows=rows,
                tape=tape, tape_ts=[t for t, _ in tape],
                fc=fc or [], fc_ts=[t for t, _ in (fc or [])])


def honest_fc(pre: dict, boundary_ms: int = 60_000) -> list[tuple[int, float]]:
    """The forecast of the PAID QUANTITY, valid at every tau.

    `twap_forecast` projects `((60-tau)*mean + tau*spot)/60`, which is the right
    thing only while tau <= 60: that is when the paid window (`mean[bell-60,]
    bell]`) is already partly printed. Past tau=60 the weight on the mean goes
    NEGATIVE and the projection becomes `2*spot - mean` at tau=120 -- an
    extrapolation of a quantity the venue computes as an average.

    Before that window opens the best forecast of a martingale's future 60 s
    mean is the SPOT ITSELF (zero-variance, unbiased), so that is what this
    substitutes. Same ticks, same trigger, only the tau>60 value changed -- so
    comparing this against `fc` isolates the extrapolation exactly.
    """
    end = int(pre["r"].end_ms)
    fv = dict(pre["fc"])
    return [(t, (fv.get(t, s) if end - t <= boundary_ms else s))
            for t, s in pre["tape"]]


def fixed_fc(pre: dict, look_s: float = 60.0) -> list[tuple[int, float]]:
    """`twap_forecast` with the RIGHT printed window, for the A/B.

    The paid window at `ts` is `[bell-60, bell]` and its printed part is
    `[ts+tau-60, ts]` -- the RECENT `(60-tau)` seconds. `twap_forecast` averages
    the whole trailing minute at every tau, so at tau=30 it averages 30 seconds
    the paid window will never contain. `pm_fc_formula_check.py` measures the
    cost: MAE against the realized `TWAP60(bell)` falls at every tau < 60
    (0.155 -> 0.042 bps at tau<10, 0.689 -> 0.437 at 20-30), and the two agree
    exactly at tau >= 60 (`tools/pm_tau_window.py`'s `fc60` handles that end).
    """
    end = int(pre["r"].end_ms)
    tape = pre["tape"]
    out: list[tuple[int, float]] = []
    for i, (ts, px) in enumerate(tape):
        tau = (end - ts) / 1000.0
        if tau >= look_s:
            out.append((ts, px))
            continue
        need = look_s - tau                 # seconds the window has printed
        lo = ts - int(need * 1000)
        win = [p for t, p in tape[:i + 1] if t >= lo]
        if len(win) < TWAP_REF_MIN_N:
            out.append((ts, px))
            continue
        m = sum(win) / len(win)
        out.append((ts, (need * m + tau * px) / look_s))
    return out


def pick(pre: dict, *, signal: str, floor_bps: float, look_s: float,
         px_lo: float, px_hi: float, tau_lo: float | None = None,
         tau_hi: float | None = None, hold_s: float = 0.0) -> dict | None:
    """The arm's own rule chain for one (signal, floor) pair: one entry per
    round, or None. `crossed_side` reads only the newest tick and the first one
    at least `look_s` old, so it is handed the tape's TAIL -- slicing the whole
    tape per row is what made the first version take minutes."""
    r, level = pre["r"], pre["level"]
    if signal in ("fc60", "fcx"):
        src = pre.get(signal) or (honest_fc(pre) if signal == "fc60"
                                  else fixed_fc(pre))
        src_ts = [t for t, _ in src]
    else:
        src, src_ts = ((pre["fc"], pre["fc_ts"]) if signal == "fc"
                       else (pre["tape"], pre["tape_ts"]))
    if signal == "fc" and not src:
        return None
    # Enough tail for the hold's trailing run (which reaches back `hold_s` plus
    # the OTHER-side tick that proves the transition) -- 10 samples was sized
    # for a pure 1 s look-back and would truncate the run.
    tail = max(10, int((hold_s + look_s + 2.0) * 1.6) + 6)
    for ts, tau, up_ask, dn_ask in zip(pre["rows"].ts_ms, pre["rows"].tau_s,
                                       pre["rows"].up_ask, pre["rows"].down_ask):
        if tau_lo is not None and tau < tau_lo:
            continue
        if tau_hi is not None and tau > tau_hi:
            continue
        j = int(np.searchsorted(src_ts, int(ts), side="right"))
        if j < 4:
            continue
        side = crossed_side(src[max(0, j - tail):j], level, look_s,
                            hold_s=hold_s)
        if side is None:
            continue
        sig_px = src[j - 1][1]
        margin = 1e4 * (sig_px - level) / level
        if floor_bps > 0 and abs(margin) < floor_bps:
            continue
        ask = float(up_ask if side == "Up" else dn_ask)
        if not (px_lo < ask < px_hi):
            continue
        won = (r.winner == "Up") if side == "Up" else (r.winner == "Down")
        return dict(slug=r.slug, family=family_of(r.slug), side=side, ask=ask,
                    won=won, per=(1.0 if won else 0.0) - ask - FEE(ask),
                    tau=float(tau), margin=margin, level_src=pre["level_src"],
                    day=dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
                    .strftime("%m-%d"), ts_ms=int(ts))
    return None


def batter(rows: list[dict], label: str) -> dict:
    if not rows:
        print(f"    {label:<34} n=0")
        return {}
    d = pd.DataFrame(rows)
    v = d.per.to_numpy()
    n = len(v)
    t = v.mean() / (v.std() / math.sqrt(n)) if v.std() else 0.0
    dm = [v[d.day.values == x].mean() for x in d.day.unique()]
    day_t = (np.mean(dm) / (np.std(dm) / math.sqrt(len(dm)))) if np.std(dm) else 0.0
    h = n // 2
    print(f"    {label:<34} n={n:4d} ask={d.ask.mean():.3f} win={d.won.mean():5.1%} "
          f"per-share={v.mean():+.4f} total={v.sum():+7.2f} "
          f"t={t:+5.2f} day-t={day_t:+5.2f} "
          f"halves={v[:h].mean():+.4f}/{v[h:].mean():+.4f}")
    return dict(n=n, per=v.mean(), total=v.sum(), label=label)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=DB)
    ap.add_argument("--by-family", action="store_true")
    ap.add_argument("--look-s", type=float, default=None,
                    help="default: the SHIPPED cross_look_s, so a sweep cannot "
                         "rank a trigger the arm does not run")
    ap.add_argument("--hold-s", type=float, default=None,
                    help="default: the SHIPPED cross_hold_s (0 = off), same "
                         "reason")
    ap.add_argument("--tau-lo", type=float, default=5.0)
    ap.add_argument("--tau-hi", type=float, default=60.0)
    ap.add_argument("--px-lo", type=float, default=0.10)
    ap.add_argument("--px-hi", type=float, default=0.65)
    args = ap.parse_args()
    if args.look_s is None:
        args.look_s = shipped_look_s()
    if args.hold_s is None:
        args.hold_s = shipped_hold_s()

    r, q = load(args.db)
    if r.empty:
        print("no Polymarket-era settled rounds with a strike yet")
        return
    q = q.copy()
    q["fam"] = q.slug.map(family_of)
    print(f"POLYMARKET ERA ONLY: {len(r)} settled rounds, "
          f"{r.slug.nunique()} slugs, quotes {len(q)}")
    days = sorted({dt.datetime.fromtimestamp(t / 1000, dt.UTC).strftime("%m-%d")
                   for t in r.start_ms})
    print(f"  days {days}")
    print(f"  window tau [{args.tau_lo:.0f},{args.tau_hi:.0f}]  band "
          f"[{args.px_lo:.2f},{args.px_hi:.2f}]  look_s {args.look_s:.1f}  "
          f"taker, pays the recorded ask")

    variants = [
        ("A  old: spot, no floor", "spot", 0.0),
        ("B  fc, no floor", "fc", 0.0),
        ("C  fc + 0.5 bps floor", "fc", 0.5),
        ("D  fc + 1.0 bps floor  <- ships", "fc", 1.0),
        ("E  fc + 2.0 bps floor", "fc", 2.0),
        # THE FORMULA A/B: identical to C/D except the forecast averages the
        # trailing minute only where the paid window actually IS printed --
        # `(60 - tau)` seconds, not 60. See `fixed_fc` and
        # `tools/pm_fc_formula_check.py` for the MAE table that motivates it.
        ("F  FIXED fc, no floor", "fcx", 0.0),
        ("G  FIXED fc + 0.5 bps floor", "fcx", 0.5),
        ("H  FIXED fc + 1.0 bps floor", "fcx", 1.0),
    ]
    print(f"\n=== THE WHOLE POLYMARKET RECORD UNDER EACH RULE SET ===")
    per_variant: dict[str, list[dict]] = {lab: [] for lab, _s, _f in variants}
    for n, (_, rr) in enumerate(r.iterrows(), 1):
        pre = precompute(q, rr, args.look_s, args.px_lo, args.px_hi,
                         args.tau_lo, args.tau_hi)
        if pre is None:
            continue
        for lab, signal, floor in variants:
            got = pick(pre, signal=signal, floor_bps=floor, look_s=args.look_s,
                       px_lo=args.px_lo, px_hi=args.px_hi,
                       hold_s=args.hold_s)
            if got:
                per_variant[lab].append(got)
        if n % 500 == 0:
            print(f"    ... {n}/{len(r)} rounds", flush=True)
    for lab, _s, _f in variants:
        batter(per_variant[lab], lab)

    if args.by_family:
        for label, _s, _f in variants[::2]:
            print(f"\n  -- {label} --")
            d = pd.DataFrame(per_variant[label])
            if d.empty:
                continue
            for fam in sorted(d.family.unique()):
                batter(d[d.family == fam].to_dict("records"), f"    {fam:<20}")

    # GROUND TRUTH: what actually happened, same era, same arm.
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    live = pd.read_sql_query("""
      select f.slug, f.side, f.stake, f.filled_px, f.fee_paid, s.won, s.pnl
      from fills f join settlements s on s.fill_id = f.id
      where f.mode = 'cross' and f.ts_ms >= 1789588200000
      order by f.ts_ms
    """, con)
    con.close()
    print(f"\n=== GROUND TRUTH: the cross fills that ACTUALLY happened (PM era) ===")
    if live.empty:
        print("    none")
    else:
        v = live.pnl.to_numpy()
        st = live.stake.sum()
        print(f"    n={len(live)} win={live.won.mean():5.1%} staked=${st:.2f} "
              f"pnl=${v.sum():+.2f} roi={100 * v.sum() / st:+.1f}% "
              f"ask={live.filled_px.mean():.3f} "
              f"halves={live.pnl[:len(live) // 2].mean():+.4f}/"
              f"{live.pnl[len(live) // 2:].mean():+.4f}")
    print("\n  NOTE the two rows are NOT the same experiment: the replay pays the")
    print("  recorded ask with no sweep premium and no FAK miss rate, and it can")
    print("  only enter where the tape has a quote -- so read the DIRECTION of the")
    print("  variants, not the level of the total.")


if __name__ == "__main__":
    main()
