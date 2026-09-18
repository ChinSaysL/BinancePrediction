"""Does the TICK DEPTH FLOOR remove good trades or bad ones? (2026-09-17)

`cross_min_depth_ticks` / `cross_tick_size` / `cross_overrides[*].min_depth_ticks`
ship as: 1.0 tick globally, 2.0 on sol/xrp/doge-5m (whose strike is
reconstructed from the spot tape). The operator asked whether the implementation
and the values are right, because losing "by cents" is what prompted the floor.

The floor is a CUT, so the question is the INCREMENT (backtest-artifacts.md #6):
not the EV of the kept set against the EV of everything, but the EV of the
cohort the floor REMOVES. Log-growth is additive, so if the removed cohort is
negative the floor is right, and if it is positive the floor is paying to lose
trades it did not have to.

Substrate: the bot's own PM record -- rounds with a real strike, real settlement,
recorded books. Prices are the recorded ASK at trigger+lag, because the price we
actually pay is ~1 s later (see pm-cross-arm-measured.md). The tick depth is
measured from the SAME quote row the trigger fired on, exactly as
`main._cross_tick` measures it from `feed.price`.

Usage:
    PYTHONPATH=. python tools/pm_tick_floor_check.py --db data/btcpredict_vps.db
"""
from __future__ import annotations

import argparse
import math
import re
import sqlite3
import time
from collections import defaultdict

from btcpredict.config import cross_knob, market_prefix
from btcpredict.main import crossed_side

FEE_RATE = 0.07


def fee_per_share(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)


def _t(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m / math.sqrt(var / n) if var > 0 else 0.0


def load(db: str, look_s: float, min_tau: float, max_tau: float,
         max_book_age_ms: int, max_ask: float, entry_lag_ms: int):
    """One trigger per PM round, carrying the SPOT at the trigger and the strike.

    Mirrors `tools/pm_live_band_sweep.py:load` and adds the two fields the floor
    needs. The depth is read from the trigger row itself, so the number here is
    the number the live gate saw.

    STREAMED per slug (`itertools.groupby`) rather than grouped into a dict.
    The live DB is 540 MB / millions of quote rows and the unit has an OOM
    history -- `pm_live_band_sweep.load` holds every row of every round at once,
    which is fine on a dev box and not on the trading host.
    """
    from itertools import groupby

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rounds = {r["slug"]: r for r in con.execute(
        "SELECT slug, strike, winner, start_ms, end_ms FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL AND strike > 0 "
        "AND condition_id IS NOT NULL AND condition_id <> ''")}
    out = []
    q_iter = con.execute(
        "SELECT slug, ts_ms, tau_s, spot, up_ask, down_ask, book_age_ms "
        "FROM quotes WHERE slug IN (SELECT slug FROM rounds WHERE winner "
        "IS NOT NULL AND condition_id IS NOT NULL AND condition_id <> '') "
        "ORDER BY slug, ts_ms")
    for slug, grp in groupby(q_iter, key=lambda q: q["slug"]):
        r = rounds.get(slug)
        if r is None:
            continue
        rows = list(grp)
        ticks: list = []
        hit = None
        for i, q in enumerate(rows):
            if q["spot"] and q["spot"] > 0:
                ticks.append((q["ts_ms"], q["spot"]))
            if hit is not None:
                continue
            if not (min_tau <= (q["tau_s"] or 0) <= max_tau):
                continue
            age = q["book_age_ms"]
            if max_book_age_ms and (age is None or age > max_book_age_ms):
                continue
            side = crossed_side(ticks, r["strike"], look_s)
            if side is not None:
                hit = (i, side)
        if hit is None:
            continue
        i, side = hit
        trig = rows[i]
        spot = trig["spot"]
        if not spot or spot <= 0:
            continue
        fam = market_prefix(slug)
        tick = cross_knob(fam, "tick_size") or 0.01
        floor_ticks = cross_knob(fam, "min_depth_ticks") or 0.0
        depth_ticks = abs(spot - r["strike"]) / tick
        trig_ts = trig["ts_ms"]
        want = trig_ts + entry_lag_ms
        pick = None
        for q in rows[i:]:
            if q["ts_ms"] >= want:
                if q["tau_s"] is not None and q["tau_s"] <= 0:
                    break
                pick = q
                break
        if pick is None:
            continue
        age = pick["book_age_ms"]
        if max_book_age_ms and (age is None or age > max_book_age_ms):
            continue
        px = pick["up_ask"] if side == "Up" else pick["down_ask"]
        if not px or px <= 0 or px > max_ask:
            continue
        out.append({
            "slug": slug, "family": fam, "ts_ms": trig_ts, "side": side,
            "px": float(px), "tau": trig["tau_s"], "winner": r["winner"],
            "spot": float(spot), "strike": float(r["strike"]),
            "tick": float(tick), "floor_ticks": float(floor_ticks),
            "depth_ticks": depth_ticks,
            "depth_bps": 1e4 * abs(spot - r["strike"]) / r["strike"],
            "fill_lag_ms": pick["ts_ms"] - trig_ts,
            "day": time.strftime("%Y-%m-%d", time.gmtime(trig_ts / 1000)),
        })
    return out


def pnl(e: dict) -> float:
    won = 1.0 if e["winner"] == e["side"] else 0.0
    return won - e["px"] - fee_per_share(e["px"])


def row(es: list[dict], label: str) -> None:
    if not es:
        print(f"  {label:<18} n=0")
        return
    ps = [pnl(e) for e in es]
    win = sum(1 for e in es if e["winner"] == e["side"]) / len(es)
    paid = sum(e["px"] for e in es) / len(es)
    by_day = defaultdict(list)
    for e in es:
        by_day[e["day"]].append(pnl(e))
    day_means = [sum(v) / len(v) for v in by_day.values()]
    order = sorted(es, key=lambda e: e["ts_ms"])
    h = len(order) // 2
    print("  %-18s %4d  win %5.1f%%  be %5.1f%%  EV %+7.4f  t %+5.2f  "
          "day-t %+5.2f  days %d  h1 %+7.4f h2 %+7.4f  med%dms"
          % (label, len(es), win * 100, paid * 100, sum(ps) / len(ps), _t(ps),
             _t(day_means), len(by_day),
             sum(pnl(e) for e in order[:h]) / max(1, h),
             sum(pnl(e) for e in order[h:]) / max(1, len(order) - h),
             sorted(e["fill_lag_ms"] for e in es)[len(es) // 2]))


def live(db: str, lag_ms: int, max_book_age_ms: int, max_ask: float) -> None:
    """The cohort the LIVE floor refused, versus the one it admitted.

    This is the exact population -- `main._cross_tick` logs the spot and strike
    it measured on the tick it refused, so the depth here is the depth the gate
    saw at the arm's own ~114 Hz, not the 1 Hz replay's. Prices are still the
    reachable ask (`ts + lag`), because a refusal priced at its own trigger
    instant is the stale-price artefact.

    Refused rows: `cross refused: only N tick(s) through the strike, floor is F`.
    Admitted rows: `cross into <side> @<px> tau..` -- recorded once every OTHER
    gate has passed, so it is the floor-passing AND everything-else-passing set.

    ⛔ TWO CORRECTIONS TO THE FIRST VERSION OF THIS FUNCTION, both found by
    disagreeing with the record (2026-09-17):

    1. ONE ROW PER ROUND. The floor is evaluated on every poll (~114/s), so a
       round that oscillates is refused repeatedly -- 155 rows over 135 rounds
       measured. Row-level pooling double-counts the choppiest rounds AND
       clusters their outcomes, so the t is not a t. `backtest-artifacts.md` #5.
       Keep the FIRST event of each cohort per round.

    2. THE FLOOR IS THE ONLY GATE THAT HAD NOT YET RUN. It sits before
       `max_depth_bps`, the round-range gate and the price band, so a floor
       refusal says NOTHING about whether the trade would have survived them.
       Measured: a large share of these rows are the ask-0.01 books that are 0
       FOR 30 in the band sweep (`--entry-lag-ms 1000`, band 0.00-0.10, win
       0.0%, t -6.16). Pooling them in made the floor's refusals look like a
       `-0.13` cohort when most of that is a band the arm refuses anyway.
       So the rows are split: `MARGINAL` = refused by the floor AND clean under
       every gate that would have run next. THAT is the cohort the floor costs
       us, and the only one that answers "was the floor right?".
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rounds = {r["slug"]: r for r in con.execute(
        "SELECT slug, strike, winner, start_ms FROM rounds "
        "WHERE winner IS NOT NULL AND winner <> '' AND strike > 0")}
    dec = {}
    for d in con.execute(
            "SELECT slug, ts_ms, side, spot, strike, quote, tau_s, reason "
            "FROM decisions "
            "WHERE reason LIKE 'cross refused: only % tick(s) through the strike%' "
            "   OR reason LIKE 'cross into %' ORDER BY ts_ms"):
        if d["slug"] in rounds:
            dec.setdefault(d["slug"], []).append(d)
    out = []
    q_iter = con.execute(
        "SELECT slug, ts_ms, tau_s, spot, up_ask, down_ask, book_age_ms "
        "FROM quotes WHERE slug IN (SELECT slug FROM rounds WHERE winner IS NOT "
        "NULL AND winner <> '' AND strike > 0) ORDER BY slug, ts_ms")
    from itertools import groupby
    for slug, grp in groupby(q_iter, key=lambda q: q["slug"]):
        ds = dec.get(slug)
        if not ds:
            continue
        rows = list(grp)
        r = rounds[slug]
        fam = market_prefix(slug)
        tick = cross_knob(fam, "tick_size") or 0.01
        floor = cross_knob(fam, "min_depth_ticks") or 0.0
        ceil_bps = cross_knob(fam, "max_depth_bps")
        gate_bps = cross_knob(fam, "max_round_range_bps")
        lo_px = cross_knob(fam, "min_price")
        hi_px = cross_knob(fam, "max_price")
        seen = set()
        for d in ds:
            # 1. one row per (round, cohort): the FIRST event only
            key = (slug, d["reason"].startswith("cross refused"))
            if key in seen:
                continue
            seen.add(key)
            if d["spot"] is None or not d["strike"]:
                continue
            dep = abs(d["spot"] - d["strike"])
            if not dep:
                dep_ticks = 0.0
            else:
                dep_ticks = dep / tick
            # the range this round had travelled BEFORE the decision
            hist = [q["spot"] for q in rows
                    if q["ts_ms"] <= d["ts_ms"] and q["spot"] and q["spot"] > 0
                    and q["ts_ms"] >= (r["start_ms"] or 0)]
            rng = (1e4 * (max(hist) - min(hist)) / d["strike"]) if len(hist) > 1 else 0.0
            # price at the trigger and at the reachable lag
            trig_row = None
            pick = None
            for q in rows:
                if trig_row is None and q["ts_ms"] >= d["ts_ms"]:
                    trig_row = q
                if q["ts_ms"] >= d["ts_ms"] + lag_ms:
                    if q["tau_s"] is not None and q["tau_s"] <= 0:
                        break
                    pick = q
                    break
            if pick is None:
                continue
            if not _age_ok(pick["book_age_ms"], max_book_age_ms):
                continue
            side = d["side"]
            ask = pick["up_ask"] if side == "Up" else pick["down_ask"]
            ask_trig = ((trig_row["up_ask"] if side == "Up" else trig_row["down_ask"])
                        if trig_row is not None else None)
            if not ask or ask <= 0 or ask > max_ask:
                continue
            out.append({
                "slug": slug, "family": fam, "ts_ms": d["ts_ms"], "side": side,
                "px": float(ask), "px_trig": float(ask_trig) if ask_trig else None,
                "winner": r["winner"], "depth_ticks": dep_ticks,
                "depth_bps": 1e4 * dep / d["strike"], "range_bps": rng,
                "floor_ticks": float(floor),
                "refused": d["reason"].startswith("cross refused"),
                # the gates that would have run AFTER the floor
                "ok_ceil": (not ceil_bps) or (1e4 * dep / d["strike"] <= ceil_bps),
                "ok_range": (not gate_bps) or (rng <= gate_bps),
                "ok_band": bool(lo_px <= ask <= hi_px),
                "fill_lag_ms": pick["ts_ms"] - d["ts_ms"],
                "day": time.strftime("%Y-%m-%d",
                                     time.gmtime(d["ts_ms"] / 1000)),
            })
    if not out:
        print("nothing found"); return
    nref = sum(1 for e in out if e["refused"])
    print("LIVE record, one row per (round, cohort), price at trigger +%dms,"
          " %d rows (%d refused / %d admitted)"
          % (lag_ms, len(out), nref, len(out) - nref))
    row([e for e in out if e["refused"]], "floor REFUSED")
    row([e for e in out if not e["refused"]], "admitted (all gates)")

    mar = [e for e in out if e["refused"] and e["ok_band"] and e["ok_ceil"]
           and e["ok_range"]]
    junk = [e for e in out if e["refused"] and e not in mar]
    print("\nREFUSED SPLIT ON THE GATES THAT WOULD HAVE RUN NEXT")
    row(mar, "MARGINAL (clean)")
    row(junk, "would fail another")
    print("   of which outside the price band  %d"
          % sum(1 for e in junk if not e["ok_band"]))
    print("   of which over the depth ceiling  %d"
          % sum(1 for e in junk if not e["ok_ceil"]))
    print("   of which over the round range    %d"
          % sum(1 for e in junk if not e["ok_range"]))

    print("\nTHE SAME SPLIT AT THE TRIGGER PRICE (0 ms) -- the stale number")
    for e in out:
        e["px_lag"] = e["px"]
        if e["px_trig"]:
            e["px"] = e["px_trig"]
    row([e for e in out if e["refused"]], "floor REFUSED @0ms")
    row([e for e in out if e["refused"] and e["ok_band"] and e["ok_ceil"]
         and e["ok_range"]], "MARGINAL @0ms")
    row([e for e in out if not e["refused"]], "admitted @0ms")
    for e in out:
        e["px"] = e["px_lag"]

    print("\nBY FAMILY (shipped floor in the label)")
    by_fam = defaultdict(list)
    for e in out:
        by_fam[e["family"]].append(e)
    for fam in sorted(by_fam):
        sub = by_fam[fam]
        ref = [e for e in sub if e["refused"]]
        adm = [e for e in sub if not e["refused"]]
        print(" %s (floor %g ticks, refused %d of %d rounds)"
              % (fam, sub[0]["floor_ticks"], len(ref), len(sub)))
        row(sub, "  all")
        row([e for e in ref if e["ok_band"] and e["ok_ceil"] and e["ok_range"]],
            "  marginal")
        row(adm, "  admitted")
        print()


def _age_ok(age, max_age_ms: int) -> bool:
    """Is a quote row usable by BOOK AGE?

    ⛔ A MISSING age is NOT a stale book. `book_age_ms` was added on 2026-09-16
    and is NULL for every quote before it (269k of 1.23M rows in the local
    09-15 snapshot), so `age is None -> drop` silently deletes the whole
    earlier era -- which is where the 5-tick floor's refusals live, i.e. the
    very cohort `FINDINGS.md` 2026-09-15 scored at +0.2311. The first version
    of this file did exactly that and reported "the 5.0 -> 1.0 rationale does
    not reproduce" off a cohort of n=0. Dropping a row for missing metadata is
    only conservative if you know the metadata would have failed.
    """
    if not max_age_ms:
        return True
    if age is None:
        return True
    return age <= max_age_ms


def floors(db: str, lag_ms: int, max_book_age_ms: int) -> None:
    """Every HISTORIC floor value, scored at the trigger AND at the reachable lag.

    This exists to settle a contradiction, and it is the test that should have
    come first (2026-09-17). `FINDINGS.md` 2026-09-15 measured the cohort below
    the THEN-shipped 5-tick floor and found them BETTER than the ones above it:

        below floor (<5t)   n=19  win 0.789  ask 0.558  edge +0.2311
        at/above floor     n=197  win 0.863  ask 0.686  edge +0.1771

    and that is why the floor was cut 5.0 -> 1.0. The cohort refused TODAY (a
    1-tick floor) is not that cohort: it is only the sub-tick one, 0 and 0.5
    ticks, because 1-4 ticks now passes. Two different populations, so the two
    measurements do not contradict each other -- and the 5-tick cohort can
    still be scored on the live record, because every refusal logs the floor
    that produced it.

    The price column is the whole point. The 2026-09-15 table had an ask column
    but no LAG: it asked the ask AT THE TRIGGER INSTANT, which this file has now
    measured five times as the stale print (it is the same instant at which the
    venue has not yet repriced off the move we just detected). @0ms and @+1s are
    printed side by side so that difference cannot be read as edge.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rounds = {r["slug"]: r for r in con.execute(
        "SELECT slug, strike, winner, start_ms FROM rounds "
        "WHERE winner IS NOT NULL AND winner <> '' AND strike > 0")}
    dec = {}
    for d in con.execute(
            "SELECT slug, ts_ms, side, spot, strike, reason FROM decisions "
            "WHERE reason LIKE 'cross refused: only % tick(s) through the "
            "strike, floor is %' ORDER BY ts_ms"):
        if d["slug"] in rounds:
            dec.setdefault(d["slug"], []).append(d)
    out = []
    q_iter = con.execute(
        "SELECT slug, ts_ms, tau_s, spot, up_ask, down_ask, book_age_ms "
        "FROM quotes WHERE slug IN (SELECT slug FROM rounds WHERE winner IS NOT "
        "NULL AND winner <> '' AND strike > 0) ORDER BY slug, ts_ms")
    from itertools import groupby
    for slug, grp in groupby(q_iter, key=lambda q: q["slug"]):
        ds = dec.get(slug)
        if not ds:
            continue
        rows = list(grp)
        r = rounds[slug]
        fam = market_prefix(slug)
        tick = cross_knob(fam, "tick_size") or 0.01
        seen = set()
        for d in ds:
            m = re.search(r"floor is ([0-9.]+)", d["reason"])
            if not m or d["spot"] is None or not d["strike"]:
                continue
            fl = float(m.group(1))
            # one row per (round, floor): the FIRST time that floor fired
            if (slug, fl) in seen:
                continue
            seen.add((slug, fl))
            trig = pick = None
            for q in rows:
                if trig is None and q["ts_ms"] >= d["ts_ms"]:
                    trig = q
                if q["ts_ms"] >= d["ts_ms"] + lag_ms:
                    if q["tau_s"] is not None and q["tau_s"] <= 0:
                        break
                    pick = q
                    break
            if pick is None or trig is None:
                continue
            if not _age_ok(pick["book_age_ms"], max_book_age_ms):
                continue
            side = d["side"]
            px = pick["up_ask"] if side == "Up" else pick["down_ask"]
            pxt = trig["up_ask"] if side == "Up" else trig["down_ask"]
            if not px or px <= 0 or not pxt or pxt <= 0:
                continue
            out.append({"slug": slug, "family": fam, "ts_ms": d["ts_ms"],
                        "side": side, "winner": r["winner"], "floor": fl,
                        "depth_ticks": abs(d["spot"] - d["strike"]) / tick,
                        "px": float(px), "px_trig": float(pxt),
                        "day": time.strftime("%Y-%m-%d",
                                             time.gmtime(d["ts_ms"] / 1000)),
                        "fill_lag_ms": pick["ts_ms"] - d["ts_ms"]})
    if not out:
        print("no floor refusals found"); return
    print("EVERY HISTORIC FLOOR VALUE, one row per (round, floor).")
    print("  @0ms  = the ask at the TRIGGER (the price the 2026-09-15 table used)")
    print("  @+%dms = the price actually reachable -- venue ack is 260-1237ms"
          % lag_ms)
    print("\n  floor   n   win     be@+lag  EV/sh @0ms   EV/sh @+%dms   "
          "days  med fill" % lag_ms)
    for fl in sorted({e["floor"] for e in out}):
        sub = [e for e in out if e["floor"] == fl]
        if not sub:
            continue
        won = [1.0 if e["winner"] == e["side"] else 0.0 for e in sub]
        win = sum(won) / len(won)
        paid = sum(e["px"] for e in sub) / len(sub)
        paid0 = sum(e["px_trig"] for e in sub) / len(sub)
        ev0 = sum(w - e["px_trig"] - fee_per_share(e["px_trig"])
                  for w, e in zip(won, sub)) / len(sub)
        evn = sum(w - e["px"] - fee_per_share(e["px"])
                  for w, e in zip(won, sub)) / len(sub)
        days = len({e["day"] for e in sub})
        med = sorted(e["fill_lag_ms"] for e in sub)[len(sub) // 2]
        print("  %5g %4d  %5.1f%%   %5.1f%%    %+7.4f    %+7.4f      %2d   %5dms"
              % (fl, len(sub), win * 100, paid * 100, ev0, evn, days, med))
        print("        (asks: @0ms %.4f -> @+lag %.4f; depth ticks mean %.2f)"
              % (paid0, paid, sum(e["depth_ticks"] for e in sub) / len(sub)))
    print("\n  THE 2026-09-15 COHORT SEPARATELY: refused by a floor of 5, i.e."
          " depths 1-4 ticks")
    five = [e for e in out if e["floor"] == 5.0 and e["depth_ticks"] >= 1.0]
    if five:
        won = [1.0 if e["winner"] == e["side"] else 0.0 for e in five]
        win = sum(won) / len(won)
        ev0 = sum(w - e["px_trig"] - fee_per_share(e["px_trig"])
                  for w, e in zip(won, five)) / len(five)
        evn = sum(w - e["px"] - fee_per_share(e["px"])
                  for w, e in zip(won, five)) / len(five)
        print("    n=%d  win %.1f%%  EV/sh @0ms %+0.4f   @+%dms %+0.4f"
              % (len(five), win * 100, ev0, lag_ms, evn))
        print("    (the md says +0.2311 for this cohort on 19 rows, 2026-09-15)")
    print("\n  BY DEPTH, ALL REFUSALS (the two cohorts side by side)")
    edges = [0, 0.5, 1, 2, 3, 5, 1e9]
    for lo, hi in zip(edges, edges[1:]):
        sub = [e for e in out if lo <= e["depth_ticks"] < hi]
        if not sub:
            continue
        lab = ("%g-%g" % (lo, hi)) if hi < 1e8 else ("%g+" % lo)
        row(sub, lab)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--live", action="store_true",
                    help="score the cohort the LIVE floor refused (needs "
                         "`decisions`; the 1 Hz replay under-samples depth)")
    ap.add_argument("--floors", action="store_true",
                    help="score EVERY historic floor value, @0ms vs @+lag")
    ap.add_argument("--look-s", type=float, default=1.0)
    ap.add_argument("--min-tau", type=float, default=5.0)
    ap.add_argument("--max-tau", type=float, default=60.0)
    ap.add_argument("--max-book-age-ms", type=int, default=2000)
    ap.add_argument("--max-ask", type=float, default=0.99)
    ap.add_argument("--lag", type=int, default=1000)
    a = ap.parse_args()

    if a.live:
        live(a.db, a.lag, a.max_book_age_ms, a.max_ask)
        return

    if a.floors:
        floors(a.db, a.lag, a.max_book_age_ms)
        return

    es = load(a.db, a.look_s, a.min_tau, a.max_tau, a.max_book_age_ms,
              a.max_ask, a.lag)
    if not es:
        print("no triggers"); return
    print("PM triggers, one per round, tau [%g,%g], look %.1fs, ask at trigger"
          " +%dms" % (a.min_tau, a.max_tau, a.look_s, a.lag))
    days = defaultdict(int)
    for e in es:
        days[e["day"]] += 1
    print("  n=%d  by day: %s  median fill lag %dms"
          % (len(es), "  ".join("%s=%d" % kv for kv in sorted(days.items())),
             sorted(e["fill_lag_ms"] for e in es)[len(es) // 2]))
    print("\nALL TRIGGERS")
    row(es, "all")

    print("\nBY DEPTH IN TICKS OF THE FAMILY'S OWN SPOT TICK")
    edges = [0, 0.5, 1, 1.5, 2, 3, 5, 10, 30, 100, 1e9]
    for lo, hi in zip(edges, edges[1:]):
        sub = [e for e in es if lo <= e["depth_ticks"] < hi]
        lab = ("%g-%g" % (lo, hi)) if hi < 1e8 else ("%g+" % lo)
        row(sub, lab)

    print("\nTHE INCREMENT: what each candidate floor REMOVES")
    print("  (removed = the cohort with depth < floor; kept = the rest)")
    for floor in (0.0, 1.0, 2.0, 3.0, 5.0):
        removed = [e for e in es if e["depth_ticks"] < floor - 1e-12]
        kept = [e for e in es if e["depth_ticks"] >= floor - 1e-12]
        row(removed, "floor %g removed" % floor)
        row(kept, "floor %g kept" % floor)
        print()

    print("BY FAMILY (shipped floor in the label)")
    by_fam = defaultdict(list)
    for e in es:
        by_fam[e["family"]].append(e)
    for fam in sorted(by_fam):
        sub = by_fam[fam]
        fl = sub[0]["floor_ticks"]
        tk = sub[0]["tick"]
        print(" %s (tick %g, floor %g ticks)" % (fam, tk, fl))
        row(sub, "  all")
        row([e for e in sub if e["depth_ticks"] < fl - 1e-12], "  refused")
        row([e for e in sub if e["depth_ticks"] >= fl - 1e-12], "  admitted")


if __name__ == "__main__":
    main()
