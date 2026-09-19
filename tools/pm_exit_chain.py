"""Sweep the EXIT the same way the entry was swept -- and count the freed slot.

WHY THIS IS NOT A REPEAT. The exit was retired on 2026-09-19 after every live
regime that ran it came in 14-16 points under break-even. That measurement
scored the exit on ONE thing: the P&L of the position it closed. It never
counted the slot.

At $5.11 the exposure cap allows two concurrent $1 positions and blocks 238-331
qualifying signals. A position held to the bell occupies its slot for the whole
round; one exited at tau 150 gives the slot back with 150 s of round left. So
an exit that LOSES a little on the position can still pay, by letting a trade in
that the cap would otherwise have refused. Nothing in the earlier work could see
that, because nothing before the capital sim modelled slots at all.

THE RULE IS MAIN.PY'S, not a paraphrase -- arm on the crossing EVENT, confirm on
the STATE:
    tau <= cross_exit_min_tau_s              -> never (the book goes one-sided)
    side_now == held AND past the floor      -> disarm
    not armed: a crossed_side() reading the OTHER side arms it, no floor test
               (a crossing is zero-margin by construction, which is the bug
               that made nothing ever arm live: btc-1789750800 went 0.64 -> 0.00)
    armed, and side_now != held, and past the floor, and the flip has persisted
               cross_exit_confirm_s          -> SELL into the bid, minus slip

WHAT THE EXIT SIDE DOES NOT MODEL, and it is more than the entry side omits.
The entry chain here is pm_full_chain's, gate for gate. The SELL is not:

  * it assumes the sale fills INSTANTLY AND WHOLLY at (bid - cross_exit_slip).
    A live sale is an order that can partially fill, miss, or be refused;
  * `_MIN_SALE_FRACTION` (0.5 in the broker) decides dust from a real sale, and
    a partial under it leaves the position open. Not modelled;
  * the pending/confirm round trip in `_confirm_pending_sale` takes real time,
    during which the bid moves. Not modelled;
  * size at the BID is absent from `quotes` for the same reason it is absent at
    the ask, so a sale into a book too thin to take it is scored as filled.

EVERY ONE OF THOSE IS OPTIMISTIC FOR THE EXIT. The exit still loses at all 24
cells on both looks, so the conclusion is safe in the direction it matters --
a more honest sell model can only make the exit look worse, never better.

    python3 tools/pm_exit_chain.py --jobs 10
"""
from __future__ import annotations

import argparse, datetime as dt, math, os, pickle, sqlite3, sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
from btcpredict.main import crossed_side, market_prefix, passes_depth_floor  # noqa
import pm_cross_replay as base  # noqa
from pm_full_chain import LIVE  # noqa

FEE = lambda p: 0.07 * p * (1 - p)


def load():
    """Like pm_full_chain.load, but carrying the BIDS: the exit sells into
    them, so a loader without up_bid/down_bid cannot price the sale at all."""
    con = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'btcpredict.db')}"
                          "?mode=ro", uri=True)
    r = pd.read_sql_query("""select slug,start_ms,end_ms,strike,winner,feed_provider
        from rounds where strike is not null and winner is not null""", con)
    q = pd.read_sql_query("""select slug,ts_ms,tau_s,spot,spot_ts_ms,
        up_ask,down_ask,up_bid,down_bid from quotes
        where spot is not null and ts_ms is not null order by ts_ms""", con)
    con.close()
    r = r[r.feed_provider.fillna("").str.contains("twap", case=False)]
    r = r[r.winner.isin(["Up", "Down"])]
    q = q[q.slug.isin(set(r.slug))].copy()
    q["fam"] = q.slug.map(base.family_of)
    return r.sort_values("start_ms").reset_index(drop=True), q.sort_values("ts_ms")
EXEC = 0.0038
_R = _Q = None


def _init(r, q):
    global _R, _Q
    _R, _Q = r, q


def _one(args):
    """Entry candidates AND the full post-entry tick stream for the exit."""
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
    tail = max(10, int((hold + look + 2.0) * 1.6) + 6)
    ticks, cands, prev = [], [], None
    lo = hi = None
    for i, (ts, tau, ua, da, ub, db, sp) in enumerate(zip(
            rows.ts_ms, rows.tau_s, rows.up_ask, rows.down_ask,
            rows.up_bid, rows.down_bid, rows.spot)):
        if sp and np.isfinite(sp):            # round range so far, main.py:3819
            lo = sp if lo is None else min(lo, sp)
            hi = sp if hi is None else max(hi, sp)
        j = int(np.searchsorted(src_ts, int(ts), side="right"))
        if j < 4:
            continue
        ev = crossed_side(src[max(0, j - tail):j], lvl, look, hold_s=hold)
        sig = src[j - 1][1]
        margin = 1e4 * (sig - lvl) / lvl
        # every tick, for the exit walk
        ticks.append((int(ts), float(tau), ev, float(margin),
                      float(ub) if ub and np.isfinite(ub) else 0.0,
                      float(db) if db and np.isfinite(db) else 0.0))
        if ev is None or not sp or not np.isfinite(sp):
            prev = prev
            continue
        ask = float(ua if ev == "Up" else da)
        if not np.isfinite(ask):
            continue
        age = ((int(ts) - int(sts[i])) / 1000.0) if sts is not None and sts[i] else 0.0
        flip = bool(prev and ev != prev[0])
        gap = ((int(ts) - prev[1]) / 1000.0) if prev else 1e9
        prev = (ev, int(ts))
        cands.append((int(ts), float(tau), ev, abs(margin), ask,
                      bool(row.winner == ev), age,
                      bool(passes_depth_floor(float(sp), lvl, mk)),
                      1e4 * abs(float(sp) - lvl) / lvl, flip, gap, len(ticks) - 1,
                      1e4 * (hi - lo) / lvl if lo and hi else 0.0))
    if not cands:
        return None
    return (int(row.end_ms), cands, ticks, str(row.slug))


def build(look, hold, jobs):
    cache = os.path.join(ROOT, "data", f".exit2-look{look:.1f}.pkl")
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    r, q = load()
    print(f"  building: {len(r)} rounds on {jobs} workers ...", flush=True)
    with ProcessPoolExecutor(jobs, initializer=_init, initargs=(r, q)) as ex:
        res = list(ex.map(_one, [(i, look, hold) for i in range(len(r))], chunksize=24))
    out = [x for x in res if x]
    pickle.dump(out, open(cache, "wb"))
    print(f"  -> {len(out)} rounds", flush=True)
    return out


def claim(cands, *, floor, ceil, px_lo, px_hi, tau_lo, tau_hi):
    """main.py's entry chain, gate for gate, in its order.

    This MUST stay identical to pm_full_chain.claim. It briefly was not:
    cross_flip_block_s (3678) and cross_max_round_range_bps (3819) were absent.
    Both are 0 live so they were no-ops and the published numbers were
    unaffected -- the exit-off baseline matched pm_full_chain exactly, n=689
    $+64.12 at look 1.0 and n=756 $+63.25 at look 2.0 -- but either knob being
    switched on would have made this tool silently replay a different arm.
    """
    for c in cands:
        (ts, tau, side, am, ask, won, age, dok, dbps, flip, gap, ti, rng) = c
        if not (tau_lo <= tau <= tau_hi):                    continue  # 3552
        if age > LIVE["max_spot_age_s"]:                     continue  # 3568
        if floor > 0 and am < floor:                         continue  # 3611
        if ceil > 0 and am > ceil:                           continue  # 3628
        fb = LIVE["cross_flip_block_s"]
        if fb > 0 and flip and gap < fb:                     continue  # 3678
        if not dok:                                          continue  # 3750
        if dbps > LIVE["cross_max_depth_bps"]:               continue  # 3784
        rg = LIVE["cross_max_round_range_bps"]
        if rg > 0 and rng > rg:                              continue  # 3819
        if not (px_lo <= ask <= px_hi):                      continue  # 3887
        return c
    return None


def walk_exit(ticks, i0, held, floor, confirm_s, min_tau, slip,
              sale_px="bid", tick=0.01):
    """main.py's exit rule, then THE BROKER'S OWN SELL PATH.

    The sale is not "the bid minus slip". `_sell_fak_body` derives a FLOOR of
    round_up(bid - slip) and sends a FAK with min_price=floor; the floor is a
    LOWER BOUND, and the fill comes back at the venue's own price. Every real
    sale on the live record landed at or above the BID, never at the floor:
        eth-1789750200  floor 0.33  bid 0.36  sold 0.36
        btc-1789767000  floor 0.31  bid 0.34  sold 0.34
        xrp-1789767300  floor 0.18  bid 0.21  sold 0.33
    Pricing exits at bid-slip therefore charged ~0.03 a share that the venue
    never took -- against ~400 exits at ~1.7 shares that is ~$20, which is most
    of the gap the exit was being judged on. `sale_px="floor"` keeps the old,
    pessimistic convention for comparison.

    The broker's two refusals are modelled, because both leave the position
    riding to settlement rather than exiting:
      * no fresh bid  -- "the book goes one-sided near the bell";
      * floor not strictly inside (0, bid) -- "nothing to take", which is what
        happens once the bid is at or under the slip.
    """
    armed = None
    for k in range(i0 + 1, len(ticks)):
        ts, tau, ev, margin, ub, db = ticks[k]
        if tau <= min_tau:
            return None
        side_now = "Up" if margin > 0 else "Down"
        past = (floor <= 0) or (abs(margin) >= floor)
        if side_now == held and past:
            armed = None
            continue
        if armed is None:
            if ev is not None and ev != held:   # NO floor test on the arming
                armed = ts
            continue
        if side_now == held or not past:
            continue
        if ts - armed < confirm_s * 1000:
            continue
        bid = ub if held == "Up" else db
        if bid <= 0:                            # no fresh bid -> refused
            continue
        fl = math.ceil((bid - max(0.0, slip)) / tick - 1e-9) * tick
        if not (0.0 < fl < bid):                # "nothing to take" -> refused
            continue
        return ts, (bid if sale_px == "bid" else fl)
    return None


def sim(R, *, bankroll, expo, exit_on, confirm_s, min_tau, sale_px="bid",
        stake=1.0, days=None, **gk):
    fc = gk.pop("fill_cap")
    ev = []
    for end, cands, ticks, *_ in R:
        c = claim(cands, **gk)
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
        if days is not None and d not in days:
            continue
        ev.append((c[0], end, c, ticks))
    ev.sort(key=lambda x: (x[0], x[1]))
    slots, tot, n, wins, blk, exits, per, dd = [], 0.0, 0, 0, 0, 0, [], []
    for t0, end, c, ticks in ev:
        slots = [e for e in slots if e > t0]
        s = stake * len(slots)
        if s > 0 and s + stake > expo * bankroll:
            blk += 1; continue
        (ts, tau, side, am, ask, won, age, dok, dbps, flip, gap, ti, rng) = c
        if ask > fc:
            continue
        pay = min(max(ask + EXEC, 0.01), 0.99)
        sh = stake / pay
        free_at = end
        if exit_on:
            x = walk_exit(ticks, ti, side, gk["floor"], confirm_s, min_tau,
                          LIVE["cross_exit_slip"], sale_px=sale_px)
            if x:
                xts, xpx = x
                tot += (xpx - pay) * sh - FEE(pay) * sh
                free_at = xts                      # THE SLOT COMES BACK EARLY
                exits += 1
                n += 1; wins += (xpx > pay); per.append((xpx - pay) - FEE(pay))
                slots.append(free_at)
                dd.append(dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%m-%d"))
                continue
        ps = (1.0 if won else 0.0) - pay - FEE(pay)
        tot += ps * sh; n += 1; wins += won; per.append(ps)
        slots.append(free_at)
        dd.append(dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%m-%d"))
    if n < 15:
        return None
    v, d = np.array(per), np.array(dd)
    sd = v.std(ddof=1)
    return dict(n=n, blk=blk, exits=exits, win=100 * wins / n, d=tot,
                t=v.mean() / (sd / math.sqrt(n)) if sd else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--bankroll", type=float, default=5.11)
    a = ap.parse_args()
    G = dict(floor=LIVE["cross_min_margin_bps"], ceil=LIVE["cross_max_margin_bps"],
             px_lo=LIVE["cross_min_price"], px_hi=LIVE["cross_max_price"],
             tau_lo=LIVE["cross_min_tau_s"], tau_hi=LIVE["cross_max_tau_s"],
             fill_cap=LIVE["cross_max_fill_px"])
    B, E = a.bankroll, LIVE["cross_max_open_stake_pct"]
    for look in (LIVE["cross_look_s"], 2.0):
        R = build(look, LIVE["cross_hold_s"], a.jobs)
        days = sorted({dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
                       for _, cs, *_ in R for c in cs})
        k = len(days) // 2
        TR, TE = set(days[:k]), set(days[k:])
        print("\n" + "=" * 100)
        print(f"look_s {look}   bankroll ${B:.2f}   expo {E}   test {sorted(TE)}")
        print("=" * 100)
        off_f = sim(R, bankroll=B, expo=E, exit_on=False, confirm_s=0, min_tau=0, **G)
        off_t = sim(R, bankroll=B, expo=E, exit_on=False, confirm_s=0, min_tau=0,
                    days=TE, **G)
        print(f"\n  EXIT OFF (ships today)   full: n={off_f['n']:4d} blk={off_f['blk']:4d} "
              f"win{off_f['win']:5.1f}% ${off_f['d']:+7.2f} t{off_f['t']:+5.2f}"
              f" | TEST ${off_t['d']:+7.2f} win{off_t['win']:5.1f}%")
        for spx, lab in (("bid", "sale at the BID (measured: 3/3 live sales)"),
                         ("floor", "sale at bid-slip (the old, pessimistic run)")):
            print(f"\n  EXIT ON -- {lab}")
            print("  %-9s %-9s %s" % ("confirm", "min_tau", "full tape / TEST"))
            for cs_ in (1.0, 3.0, 5.0, 10.0):
                for mt in (8.0, 30.0, 60.0):
                    f = sim(R, bankroll=B, expo=E, exit_on=True, confirm_s=cs_,
                            min_tau=mt, sale_px=spx, **G)
                    t = sim(R, bankroll=B, expo=E, exit_on=True, confirm_s=cs_,
                            min_tau=mt, sale_px=spx, days=TE, **G)
                    if not f or not t:
                        continue
                    mark = "*" if (cs_ == LIVE["cross_exit_confirm_s"]
                                   and mt == LIVE["cross_exit_min_tau_s"]) else " "
                    beat = "  <-- BEATS OFF" if t["d"] > off_t["d"] else ""
                    print(f"  {mark}{cs_:<8.0f} {mt:<9.0f} n={f['n']:4d} "
                          f"exits={f['exits']:4d} blk={f['blk']:4d} "
                          f"win{f['win']:5.1f}% ${f['d']:+7.2f} t{f['t']:+5.2f}"
                          f" | TEST ${t['d']:+7.2f}{beat}")


if __name__ == "__main__":
    main()
