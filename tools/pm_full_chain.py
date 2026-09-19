"""The cross arm replayed under EVERY gate the tape can express, in main.py order.

WHY: the sweeps that chose this config applied 6 gates. The live arm runs ~18.
Against 127 PM-era fills it logged 4,020 refusals, and the largest omissions
were the depth floor (619) and the book-size floor (757).

CONFIG COMES FROM THE LIVE PROCESS, not from importing config.py. Two knobs are
overridden in the VPS .env in lower case (`cross_max_price=0.65`,
`cross_max_depth_bps=8.0`) where config.py's defaults read 0.90 and 5.0, so
importing the defaults replays an arm nobody runs. `.live-config.json` is dumped
from the running interpreter.

    python3 tools/pm_full_chain.py --jobs 10

REPLAYED (main.py line order):
    3437 max_per_round      the slot is stamped at 4221, AFTER every gate, so a
                            refused crossing does NOT burn the round -- the arm
                            walks to the next one
    3552 min/max_tau_s
    3568 feed.is_fresh      spot age vs max_spot_age_s
    3611 min_margin_bps     on the FORECAST
    3628 max_margin_bps     0 = off
    3678 flip_block_s       0 = off
    3750 passes_depth_floor IMPORTED from main, never re-implemented: main.py's
                            own note records tools drifting by inlining a copy
                            that read the global tick size (an empty band on
                            xrp/doge)
    3784 max_depth_bps      on SPOT, a different quantity from the margin floor
    3819 max_round_range_bps 0 = off
    3887 min_price/max_price both 0.65 live, so this and the fill cap coincide
    4129 max_open_stake_pct  against FREE CASH, which shrinks as positions open
    5747 max_fill_px

NOT REPLAYABLE -- named rather than silently skipped:
    4034 book holds < min_order   757 refusals; `quotes` carries no size at the
                                  touch, only top-of-book prices
    4053 sweep price band          35 refusals; needs the ladder
    4189 skip_opposite_to_main     needs the main arm's live positions
    4187 reserved slots in flight
"""
from __future__ import annotations

import argparse, datetime as dt, json, math, os, pickle, sqlite3, sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from btcpredict.main import (crossed_side, market_prefix,  # noqa: E402
                             passes_depth_floor, twap_reference)
import pm_cross_replay as base  # noqa: E402

DB = os.path.join(ROOT, "data", "btcpredict.db")
LIVE = json.load(open(os.path.join(ROOT, ".live-config.json")))
FEE = lambda p: 0.07 * p * (1 - p)
EXEC = 0.0038          # measured: paid minus the ask the tape books, n=119
_Q = None              # inherited by fork, never pickled


def load():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    r = pd.read_sql_query("""select slug,start_ms,end_ms,strike,winner,feed_provider
        from rounds where strike is not null and winner is not null""", con)
    q = pd.read_sql_query("""select slug,ts_ms,tau_s,spot,spot_ts_ms,up_ask,down_ask
        from quotes where spot is not null and ts_ms is not null order by ts_ms""", con)
    con.close()
    r = r[r.feed_provider.fillna("").str.contains("twap", case=False)]
    r = r[r.winner.isin(["Up", "Down"])]
    q = q[q.slug.isin(set(r.slug))].copy()
    q["fam"] = q.slug.map(base.family_of)
    return r.sort_values("start_ms").reset_index(drop=True), q.sort_values("ts_ms")


def _one(args):
    """Candidates for ONE round, with every gate-relevant field precomputed."""
    idx, look, hold = args
    row = _R.iloc[idx]
    pre = base.precompute(_Q, row, look, 0.0, 1.0, 0.0, 1e9)
    if pre is None:
        return None
    lvl, src, src_ts = pre["level"], pre["fc"], pre["fc_ts"]
    if not src or not lvl:
        return None
    rows, mk = pre["rows"], market_prefix(row.slug)
    sts = rows.spot_ts_ms.to_numpy() if "spot_ts_ms" in rows else None
    spots = rows.spot.to_numpy()
    tail = max(10, int((hold + look + 2.0) * 1.6) + 6)
    out, prev = [], None
    lo = hi = None
    for i, (ts, tau, ua, da, sp) in enumerate(zip(rows.ts_ms, rows.tau_s,
                                                  rows.up_ask, rows.down_ask, spots)):
        if sp and np.isfinite(sp):                       # round range so far, 3819
            lo = sp if lo is None else min(lo, sp)
            hi = sp if hi is None else max(hi, sp)
        j = int(np.searchsorted(src_ts, int(ts), side="right"))
        if j < 4:
            continue
        side = crossed_side(src[max(0, j - tail):j], lvl, look, hold_s=hold)
        if side is None:
            continue
        ask = float(ua if side == "Up" else da)
        if not np.isfinite(ask) or not sp or not np.isfinite(sp):
            continue
        age = ((int(ts) - int(sts[i])) / 1000.0) if sts is not None and sts[i] else 0.0
        flip = bool(prev and side != prev[0])
        gap = ((int(ts) - prev[1]) / 1000.0) if prev else 1e9
        prev = (side, int(ts))
        out.append((int(ts), float(tau), side, abs(1e4 * (src[j - 1][1] - lvl) / lvl),
                    ask, bool(row.winner == side), age,
                    bool(passes_depth_floor(float(sp), lvl, mk)),
                    1e4 * abs(float(sp) - lvl) / lvl, flip, gap,
                    1e4 * (hi - lo) / lvl if lo and hi else 0.0))
    return (int(row.end_ms), out) if out else None


def _init(r, q):
    global _R, _Q
    _R, _Q = r, q


def build(look, hold, jobs):
    cache = os.path.join(ROOT, "data", f".chain-look{look:.1f}.pkl")
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    r, q = load()
    print(f"  building candidates: {len(r)} rounds on {jobs} workers ...", flush=True)
    with ProcessPoolExecutor(jobs, initializer=_init, initargs=(r, q)) as ex:
        res = list(ex.map(_one, [(i, look, hold) for i in range(len(r))], chunksize=24))
    out = [x for x in res if x]
    pickle.dump(out, open(cache, "wb"))
    print(f"  -> {len(out)} rounds with >=1 crossing", flush=True)
    return out


def claim(cs, *, floor, ceil, px_lo, px_hi, tau_lo, tau_hi, depth, fresh):
    """The crossing that stamps _cross_fired, or None."""
    for (ts, tau, side, am, ask, won, age, dok, dbps, flip, gap, rng) in cs:
        if not (tau_lo <= tau <= tau_hi):                       continue  # 3552
        if fresh and age > LIVE["max_spot_age_s"]:              continue  # 3568
        if floor > 0 and am < floor:                            continue  # 3611
        if ceil > 0 and am > ceil:                              continue  # 3628
        fb = LIVE["cross_flip_block_s"]
        if fb > 0 and flip and gap < fb:                        continue  # 3678
        if depth and not dok:                                   continue  # 3750
        if depth and dbps > LIVE["cross_max_depth_bps"]:        continue  # 3784
        rg = LIVE["cross_max_round_range_bps"]
        if rg > 0 and rng > rg:                                 continue  # 3819
        if not (px_lo <= ask <= px_hi):                         continue  # 3887
        return (ts, ask, won)                                             # 4221
    return None


def sim(R, *, bankroll, expo, fill_cap, days=None, stake=1.0, **gk):
    ev = []
    for end, cs in R:
        c = claim(cs, **gk)
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
        if days is not None and d not in days:
            continue
        ev.append((c[0], end, c[1], c[2]))
    ev.sort(key=lambda x: (x[0], x[1]))
    slots, tot, n, wins, blk, burned, per, dd = [], 0.0, 0, 0, 0, 0, [], []
    for t0, end, ask, won in ev:
        slots = [e for e in slots if e > t0]
        s = stake * len(slots)
        if s > 0 and s + stake > expo * bankroll:            # 4129
            blk += 1; continue
        if ask > fill_cap:                                    # 5747
            burned += 1; continue
        pay = min(max(ask + EXEC, 0.01), 0.99)
        ps = (1.0 if won else 0.0) - pay - FEE(pay)
        tot += ps * (stake / pay); n += 1; wins += won
        per.append(ps); slots.append(end)
        dd.append(dt.datetime.fromtimestamp(t0 / 1000, dt.UTC).strftime("%m-%d"))
    if n < 15:
        return None
    v, d = np.array(per), np.array(dd)
    sd = v.std(ddof=1)
    u = np.unique(d); dm = np.array([v[d == x].mean() for x in u])
    return dict(n=n, blk=blk, burned=burned, win=100 * wins / n, d=tot,
                t=v.mean() / (sd / math.sqrt(n)) if sd else 0.0,
                dayt=dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
                if len(dm) > 1 and dm.std(ddof=1) else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--bankroll", type=float, default=5.11)
    a = ap.parse_args()
    G = dict(floor=LIVE["cross_min_margin_bps"], ceil=LIVE["cross_max_margin_bps"],
             px_lo=LIVE["cross_min_price"], px_hi=LIVE["cross_max_price"],
             tau_lo=LIVE["cross_min_tau_s"], tau_hi=LIVE["cross_max_tau_s"],
             depth=True, fresh=True)
    B, E, FC = a.bankroll, LIVE["cross_max_open_stake_pct"], LIVE["cross_max_fill_px"]
    for look in (LIVE["cross_look_s"], 2.0):
        R = build(look, LIVE["cross_hold_s"], a.jobs)
        ALL = sorted({dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
                      for _, cs in R for c in cs})
        TE = set(ALL[len(ALL) // 2:])
        print("\n" + "=" * 96)
        print(f"look_s {look}  bankroll ${B:.2f}  expo {E}  fill cap {FC}   "
              f"test days {sorted(TE)}")
        print("=" * 96)
        print("\n  --- each gate added cumulatively ---")
        for lab, ov in (("6 gates (as swept)", dict(depth=False, fresh=False)),
                        ("+ spot freshness",   dict(depth=False, fresh=True)),
                        ("+ depth floor+bps",  dict(depth=True, fresh=True))):
            s = sim(R, bankroll=B, expo=E, fill_cap=FC, **{**G, **ov})
            print(f"   {lab:<20} " + (f"n={s['n']:4d} blk={s['blk']:4d} "
                  f"win{s['win']:5.1f}% ${s['d']:+7.2f} t{s['t']:+5.2f} "
                  f"day-t{s['dayt']:+5.2f}" if s else "n<15"))
        for name, key, vals in (("cross_max_tau_s", "tau_hi", [120., 180., 240., 300.]),
                                ("cross_min_margin_bps", "floor", [0.25, 0.5, 0.75, 1.0]),
                                ("cross_min_price", "px_lo", [0.35, 0.45, 0.50, 0.55])):
            print(f"\n  === {name} (FULL CHAIN) ===")
            for v in vals:
                f = sim(R, bankroll=B, expo=E, fill_cap=FC, **{**G, key: v})
                t = sim(R, bankroll=B, expo=E, fill_cap=FC, days=TE, **{**G, key: v})
                star = "*" if v == G[key] else " "
                print(f"   {star} {v:<6} full: " +
                      (f"n={f['n']:4d} win{f['win']:5.1f}% ${f['d']:+7.2f} t{f['t']:+5.2f}"
                       if f else "n<15   ") + " | TEST: " +
                      (f"n={t['n']:4d} win{t['win']:5.1f}% ${t['d']:+7.2f}" if t else "n<15"))
        print("\n  === cross_max_open_stake_pct (FULL CHAIN) ===")
        for v in (0.35, 0.50, 0.70, 1.00):
            f = sim(R, bankroll=B, expo=v, fill_cap=FC, **G)
            t = sim(R, bankroll=B, expo=v, fill_cap=FC, days=TE, **G)
            print(f"   {'*' if v == E else ' '} {v:<6} full: " +
                  (f"n={f['n']:4d} blk={f['blk']:4d} win{f['win']:5.1f}% ${f['d']:+7.2f}"
                   if f else "n<15") + " | TEST: " +
                  (f"n={t['n']:4d} win{t['win']:5.1f}% ${t['d']:+7.2f}" if t else "n<15"))


if __name__ == "__main__":
    main()
