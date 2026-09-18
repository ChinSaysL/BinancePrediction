"""What the CORRECTED exit rule would have done to the trades we actually took.

The replay in `pm_exit_flip.py` scores the rule against the entries the rule
CHOOSES. This one scores it against the entries the bot ACTUALLY TOOK -- the
`fills` table -- so the answer is about real money at real prices, not about a
reconstructed cohort.

THE THREE RULES COMPARED, per live fill:

  HELD      what the ledger did (hold to the bell, or the live exit if one fired)
  SHIPPED   the rule that was live on 2026-09-18: `crossed_side` with no
            `hold_s` and NO MARGIN FLOOR, fired `confirm_s` after the ENTRY
            (`flip_ms` was stamped and never read), i.e. "wait 4 s after the
            buy, then leave on the first opposite tick"
  FIXED     the entry's own chain -- `hold_s` and `min_margin_bps` applied to
            the flip exactly as they are applied to the entry -- with the
            confirmation clocked from the FLIP, and a reading back on the held
            side killing the latch

The exit price is the complementary bid `1 - other_ask` at the tick the rule
fires, less the same taker fee the entry pays. That is ~1c optimistic against
the venue's real bid (measured on a live shadow-book line), so the FIXED
column's edge over HELD should be read as an upper bound.

    /root/analysis-venv/bin/python tools/pm_exit_live_recheck.py
    /root/analysis-venv/bin/python tools/pm_exit_live_recheck.py --day 09-18
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pm_cross_replay as base  # noqa: E402

FEE = base.FEE


def live_fills(db: str) -> pd.DataFrame:
    """Every settled cross/train fill, with the round it belongs to.

    `exited` marks the ones the live exit already closed: a settlement written
    BEFORE the bell. Those are excluded from the "what would have happened"
    arithmetic for HELD, because for them the ledger is not a hold.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    d = pd.read_sql_query("""
      select f.id fid, f.slug, f.ts_ms, f.side, f.filled_px, f.shares,
             f.stake, f.fee_paid, s.ts_ms s_ts, s.pnl, r.end_ms, r.winner
      from fills f
      join settlements s on s.fill_id = f.id
      join rounds r on r.slug = f.slug
      where f.mode in ('cross','train') and s.void = 0 and s.pnl is not null
        and r.winner in ('Up','Down') and f.filled_px is not null
      order by f.ts_ms
    """, con)
    con.close()
    d["exited"] = d.s_ts < d.end_ms
    return d


def walk_rule(pre: dict, *, entry_ms: int, held: str, confirm_s: float,
              look_s: float, tau_min: float, hold_s: float, floor_bps: float,
              clock: str, gated: bool) -> dict | None:
    """One pass of the exit over a round's tape, after `entry_ms`.

    `clock='entry'` + `gated=False` reproduces what shipped; `clock='flip'` +
    `gated=True` is the fix. Both share this body so the comparison cannot
    drift into comparing two different implementations.
    """
    other = "Down" if held == "Up" else "Up"
    src, src_ts = pre["fc"], pre["fc_ts"]
    if not src:
        return None
    rows, level = pre["rows"], pre["level"]
    end_ms = int(pre["r"].end_ms)
    tail = max(10, int((hold_s + look_s + 2.0) * 1.6) + 6)
    armed = None
    for ts, tau, up_ask, dn_ask in zip(rows.ts_ms, rows.tau_s,
                                       rows.up_ask, rows.down_ask):
        ts = int(ts)
        if ts <= entry_ms or tau < tau_min:
            continue
        j = int(np.searchsorted(src_ts, ts, side="right"))
        if j < 4:
            continue
        side = base.crossed_side(src[max(0, j - tail):j], level, look_s,
                                 hold_s=(hold_s if gated else 0.0))
        ok = True
        if gated and floor_bps > 0 and level and side is not None:
            ok = abs(1e4 * (src[j - 1][1] - level) / level) >= floor_bps
        if side == held:
            if not gated or ok:
                armed = None
            continue
        if side != other:
            continue
        if gated and not ok:
            continue
        if armed is None:
            armed = ts
        ref = armed if clock == "flip" else (entry_ms + 1000)
        if confirm_s <= 0 or (ts - ref) / 1000.0 >= confirm_s:
            opp = dn_ask if held == "Up" else up_ask
            if not opp or not (0.0 < float(opp) < 1.0):
                continue
            px = 1.0 - float(opp)
            if not (0.0 < px < 1.0):
                continue
            return dict(px=px, ts_ms=ts, tau=float(tau),
                        lag_s=(ts - entry_ms) / 1000.0)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=base.DB)
    ap.add_argument("--day", default=None, help="UTC MM-DD, e.g. 09-18")
    ap.add_argument("--tau-min", type=float, default=8.0)
    ap.add_argument("--confirm-s", type=float, default=None)
    ap.add_argument("--floor-bps", type=float, default=None)
    ap.add_argument("--detail", action="store_true",
                    help="one line per fill")
    args = ap.parse_args()
    from btcpredict.config import settings
    confirm_s = (args.confirm_s if args.confirm_s is not None
                 else float(settings.cross_exit_confirm_s))
    floor_bps = (args.floor_bps if args.floor_bps is not None
                 else float(settings.cross_min_margin_bps))
    look_s, hold_s = base.shipped_look_s(), base.shipped_hold_s()

    r, q = base.load(args.db)
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)
    rounds = {row.slug: row for _, row in r.iterrows()}
    d = live_fills(args.db)
    if args.day:
        d = d[d.ts_ms.map(lambda t: dt.datetime.fromtimestamp(
            t / 1000, dt.UTC).strftime("%m-%d")) == args.day]
    print(f"live fills: {len(d)} | look_s {look_s} hold_s {hold_s} "
          f"floor {floor_bps} confirm {confirm_s}s tau_min {args.tau_min}")

    out = []
    for _, f in d.iterrows():
        row = rounds.get(f.slug)
        if row is None:
            continue
        pre = base.precompute(q, row, look_s, 0.0, 1.0, 0.0, 1e9)
        if pre is None:
            continue
        held, sh = f.side, float(f.shares or 0.0)
        cost = float(f.stake or 0.0) + float(f.fee_paid or 0.0)
        hold_pnl = (sh if f.winner == held else 0.0) - cost
        rec = dict(fid=int(f.fid), slug=f.slug, side=held, ts_ms=int(f.ts_ms),
                   shares=sh, px=float(f.filled_px), cost=cost,
                   won=bool(f.winner == held), ledger=float(f.pnl),
                   exited=bool(f.exited), hold=hold_pnl,
                   day=dt.datetime.fromtimestamp(f.ts_ms / 1000, dt.UTC)
                   .strftime("%m-%d"))
        for name, kw in (("shipped", dict(clock="entry", gated=False)),
                         ("fixed", dict(clock="flip", gated=True))):
            g = walk_rule(pre, entry_ms=int(f.ts_ms), held=held,
                          confirm_s=confirm_s, look_s=look_s,
                          tau_min=args.tau_min, hold_s=hold_s,
                          floor_bps=floor_bps, **kw)
            if g is None:
                rec[name] = hold_pnl
                rec[name + "_x"] = None
            else:
                proceeds = sh * (g["px"] - FEE(g["px"]))
                rec[name] = proceeds - cost
                rec[name + "_x"] = g
        out.append(rec)

    if not out:
        print("nothing to score")
        return
    D = pd.DataFrame(out)

    def blk(label: str, col: str) -> None:
        v = D[col].to_numpy()
        n = len(v)
        t = v.mean() / (v.std(ddof=1) / math.sqrt(n)) if n > 1 and v.std() else 0.0
        dm = np.array([v[D.day.values == x].mean() for x in D.day.unique()])
        dt_ = (dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
               if len(dm) > 1 and dm.std(ddof=1) else 0.0)
        nx = (D[col + "_x"].notna().sum() if col + "_x" in D else 0)
        print(f"  {label:<34} total {v.sum():+8.3f}  per-fill {v.mean():+.4f}  "
              f"t {t:+5.2f}  day-t {dt_:+5.2f}  exits {nx:3d}")

    print("\n=== what each rule would have banked on the fills we took ===")
    blk("HELD to the bell (counterfactual)", "hold")
    blk("SHIPPED exit (entry clock, ungated)", "shipped")
    blk("FIXED exit (flip clock, gated)", "fixed")
    print(f"  {'LEDGER (what actually happened)':<34} total "
          f"{D.ledger.sum():+8.3f}  per-fill {D.ledger.mean():+.4f}")

    print("\n=== the exits each rule takes, and whether they were right ===")
    for name in ("shipped", "fixed"):
        e = D[D[name + "_x"].notna()]
        if e.empty:
            print(f"  {name:<8} no exits")
            continue
        saved = (e[name] - e.hold)
        lags = [g["lag_s"] for g in e[name + "_x"]]
        print(f"  {name:<8} {len(e):3d} exits | saved {saved.sum():+7.3f} "
              f"({(saved > 0).sum()} helped / {(saved < 0).sum()} hurt) | "
              f"lag median {np.median(lags):6.1f}s p10 {np.percentile(lags,10):5.1f} "
              f"p90 {np.percentile(lags,90):6.1f}")
        would = e[~e.won]
        print(f"           of those, {len(would):3d} were LOSING positions "
              f"(cut early, saved {(would[name]-would.hold).sum():+7.3f}) and "
              f"{len(e)-len(would):3d} were WINNING "
              f"(cut early, gave up {(e[e.won][name]-e[e.won].hold).sum():+7.3f})")

    if args.detail:
        print("\n=== per fill ===")
        for _, x in D.iterrows():
            tag = "EXITED-LIVE" if x.exited else ""
            sx = "-" if x.shipped_x is None else f"{x.shipped_x['lag_s']:.0f}s"
            fx = "-" if x.fixed_x is None else f"{x.fixed_x['lag_s']:.0f}s"
            print(f"  {dt.datetime.fromtimestamp(x.ts_ms/1000, dt.UTC):%m-%d %H:%M:%S} "
                  f"{x.slug[:24]:24s} {x.side:4s} @{x.px:.2f} "
                  f"{'WIN ' if x.won else 'LOSE'} | hold {x.hold:+.3f} "
                  f"shipped {x.shipped:+.3f} ({sx:>6s}) "
                  f"fixed {x.fixed:+.3f} ({fx:>6s}) {tag}")


main()
