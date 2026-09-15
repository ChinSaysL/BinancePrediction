#!/usr/bin/env python3
"""DECISION TEST: does a PRECURSOR entry (before the crossing) pay, vs the
current rule (enter AT the crossing, on the lagging odds)?

The precursor literature (queue imbalance, CVD) predicts the next tick; the
tick-level test (tools/precursor_ticks.py) showed it predicts 1-tick motion
and a slow drift, NOT the >=0.5bps jump class. But the only question that
decides shipping is economic: if we enter the venue EARLY -- while spot is
still short of the strike but Binance aggressive flow says it is coming --
do we win often enough to beat the price we pay?

Data (all local, no downloads):
  data/btcpredict.db         venue quotes (1 Hz) + rounds with the winner,
                             Sep-08..Sep-10 (the local snapshot)
  data/aggTrades/*.zip       Binance trades with taker side, same days

Rules simulated per round (first qualifying quote wins, one entry a round):
  CROSS   the current arm, idealised: first quote where the spot has crossed
          the strike; buy the crossed side at its ask (fallback: displayed).
  PRE     precursor: spot within --gap-bps of the strike from below and
          CVD500 > 0 and CVD500 accelerating; buy Up. Mirror for Down.
  PRE_MOM PRE + 1s spot momentum agreeing.
  PRE_COIN control: same entry times as PRE, random side (pseudo, by slug).
P&L per $1: win -> (1-p)/p, lose -> -1 (p = entry price). Net variant
subtracts the round's fee_rate_bps as a stake haircut.

Read-only. Prints tables; decides nothing by itself.
"""
from __future__ import annotations

import argparse
import bisect
import random
import sqlite3
import time
import zipfile

W500_US = 500_000
W300_US = 300_000
W1000_US = 1_000_000
VEL_WIN_US = 500_000
VEL_BASE_US = 10_000_000
DATA = "data"


def _ts_us(v: int) -> int:
    """Normalise a Binance trade timestamp to microseconds.

    The spot daily files carry microseconds; the futures daily files do not
    always, so the unit is taken from the magnitude rather than assumed.
    """
    if v > 10 ** 14:
        return v                    # already microseconds
    if v > 10 ** 11:
        return v * 1000             # milliseconds
    return v * 1_000_000            # seconds


def load_tape(sym: str, day: str, base: str = f"{DATA}/aggTrades"):
    """(ts_us, signed-volume prefix, trade prices, absolute-volume prefix).

    The absolute prefix is what lets one-sidedness be measured: a forced sweep
    is |net|/total near 1. The sign convention is taker: isBuyerMaker=True is an
    aggressive SELL (negative), False is an aggressive BUY (positive).
    """
    path = f"{base}/{sym}-aggTrades-{day}.zip"
    ts: list[int] = []
    px: list[float] = []
    pre: list[float] = [0.0]
    absq: list[float] = [0.0]
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as raw:
            for line in raw:
                f = line.decode().split(",")
                if len(f) < 7:
                    continue
                try:
                    t = _ts_us(int(f[5]))
                    q = float(f[2])
                    pr = float(f[1])
                except ValueError:
                    continue                          # header row
                ts.append(t)
                px.append(pr)
                pre.append(pre[-1] + (-q if f[6] == "True" else q))
                absq.append(absq[-1] + q)
    return ts, pre, px, absq


def load_perp(sym: str, day: str):
    """The futures (perp) tape for the same symbol/day."""
    try:
        return load_tape(sym, day, f"{DATA}/perp/aggTrades")
    except FileNotFoundError:
        return [], [0.0], [], [0.0]


def sweep_at(ts, spre, apre, px, t_us: int, win_us: int, min_usd: float,
             min_ticks: int, ratio: float):
    """The mechanical footprint of a forced sweep, in the last win_us.

    Liquidations are not tagged in the public tape any more (allForceOrders was
    deprecated and the forceOrder stream is throttled), but a liquidation is
    executed as an aggressive market order, so the footprint is the same: one
    side only, several distinct price ticks, and a large notional in a burst.

    Returns (direction, notional_usd, ticks) or None. direction +1 = buy sweep.
    """
    if not ts:
        return None
    j = bisect.bisect_right(ts, t_us)
    i0 = bisect.bisect_left(ts, t_us - win_us)
    if j - i0 < 3:                                # a burst, not a single print
        return None
    net = spre[j] - spre[i0]
    tot = apre[j] - apre[i0]
    if tot <= 0 or abs(net) / tot < ratio:        # must be one-sided
        return None
    last = px[j - 1]
    notional = tot * last
    if notional < min_usd:
        return None
    ticks = len(set(px[i0:j]))
    if ticks < min_ticks:                         # must walk the book
        return None
    return (1 if net > 0 else -1, notional, ticks)


def ev(side: str, p: float, winner: str) -> float:
    """P&L per $1: win pays (1-p)/p, lose pays -1."""
    return (1 - p) / p if winner == side else -1.0


def perp_move_bps(ts, px, t_us: int, win_ms: float) -> float | None:
    """Signed perp displacement (bps) over the trailing win_ms."""
    if not ts:
        return None
    j = bisect.bisect_right(ts, t_us)
    if j == 0:
        return None
    i0 = bisect.bisect_right(ts, t_us - int(win_ms * 1000))
    if i0 == 0:
        return None
    then, now = px[i0 - 1], px[j - 1]
    return (now - then) / then * 1e4 if then else None


def move_ratio(ts, px, t_us: int, win_ms: float, base_n: int = 10) -> float | None:
    """Volatility expansion: recent |move| / mean of the prior |moves|.

    The 'squeeze breakout' claim: when the last window's displacement is a
    multiple of its own trailing baseline, the drift continues for 15-45s.
    """
    recent = perp_move_bps(ts, px, t_us, win_ms)
    if recent is None:
        return None
    step = int(win_ms * 1000)
    base = [perp_move_bps(ts, px, t_us - k * step, win_ms)
            for k in range(1, base_n + 1)]
    base = [abs(b) for b in base if b is not None]
    if len(base) < 5:
        return None
    m = sum(base) / len(base)
    return abs(recent) / m if m > 0 else None


def win_sum(ts, pre, lo: int, hi: int) -> float:
    i = bisect.bisect_left(ts, lo)
    j = bisect.bisect_left(ts, hi)
    return pre[j] - pre[i]


def slug_day(slug: str) -> str:
    """The day of a round, from the start seconds embedded in its slug."""
    try:
        return time.strftime("%m-%d", time.gmtime(int(slug.rsplit("-", 1)[1])))
    except (ValueError, IndexError):
        return "?"


def load_rounds(conn, sym_filter: set[str], day_filter: set[str] = set()):
    out = []
    q = ("SELECT slug, symbol, start_ms, end_ms, strike, winner, fee_rate_bps "
         "FROM rounds WHERE winner IN ('Up','Down')")
    for r in conn.execute(q):
        if r["symbol"] not in sym_filter:
            continue
        if day_filter and slug_day(r["slug"]) not in day_filter:
            continue
        out.append(dict(r))
    out.sort(key=lambda r: r["start_ms"])
    return out


def quotes_for(conn, slug: str):
    q = ("SELECT ts_ms, tau_s, up_price, up_ask, down_price, down_ask, spot, "
         "strike FROM quotes WHERE slug=? ORDER BY ts_ms")
    return [dict(r) for r in conn.execute(q, (slug,))]


def price_of(row: dict, side: str) -> float | None:
    ask = row.get("up_ask") if side == "Up" else row.get("down_ask")
    if ask and 0 < ask < 1:
        return float(ask)
    px = row.get("up_price") if side == "Up" else row.get("down_price")
    if px and 0 < px < 1:
        return float(px)
    return None


def entry_price(qs: list[dict], i: int, side: str, lag_ms: int) -> float | None:
    """The price we would actually get: the first quote at/after t+lag.

    A signal observed on a 1 Hz row cannot be acted on in that same row --
    our order needs a venue round trip. With lag=0 this is the optimistic
    same-tick fill; with lag=1000 it is the honest one.
    """
    t0 = qs[i]["ts_ms"]
    for j in range(i, len(qs)):
        if qs[j]["ts_ms"] - t0 >= lag_ms:
            return price_of(qs[j], side)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT")
    ap.add_argument("--gap-bps", type=float, default=3.0,
                    help="max distance to the strike for a precursor entry")
    ap.add_argument("--min-tau", type=float, default=20.0)
    ap.add_argument("--max-tau", type=float, default=240.0)
    ap.add_argument("--require-mom", action="store_true")
    ap.add_argument("--entry-lag-ms", type=int, default=1000,
                    help="quote age before we could actually fill (0 = same tick)")
    ap.add_argument("--mom-bps", type=float, default=0.0,
                    help="minimum 1s momentum for MOM_ONLY / PRE_MOM")
    ap.add_argument("--perp-bps", type=float, default=1.0,
                    help="minimum trailing PERP displacement (bps) to fire "
                         "the lead-lag rule")
    ap.add_argument("--perp-win-ms", type=float, default=1000.0,
                    help="window for the perp displacement")
    ap.add_argument("--vel-x", type=float, default=3.0,
                    help="trade-arrival multiple over the 10s baseline")
    ap.add_argument("--break-x", type=float, default=2.0,
                    help="BREAK: recent 30s perp move vs its 5min baseline")
    ap.add_argument("--break-bps", type=float, default=3.0,
                    help="BREAK: minimum recent 30s perp displacement (bps)")
    ap.add_argument("--sweep-ms", type=float, default=200.0,
                    help="burst window for the forced-sweep footprint")
    ap.add_argument("--sweep-usd", type=float, default=300_000.0,
                    help="min notional in the burst; 0 disables the rule")
    ap.add_argument("--sweep-ticks", type=int, default=3,
                    help="distinct price ticks the burst must walk")
    ap.add_argument("--sweep-ratio", type=float, default=0.9,
                    help="min one-sidedness |net|/total")
    ap.add_argument("--sweep-tape", default="perp", choices=["perp", "spot"],
                    help="which tape the sweep is detected on")
    ap.add_argument("--days", default="",
                    help="comma-separated MM-DD filter (empty = every day in "
                         "the DB that has quotes)")
    ap.add_argument("--db", default=f"{DATA}/btcpredict.db",
                    help="venue DB; use data/btcpredict_vps.db for the live "
                         "snapshot (8 days, ~5.5k rounds) instead of the "
                         "stale 3-day local copy")
    args = ap.parse_args()

    syms = {s.strip() for s in args.symbols.split(",")}
    days = {d.strip() for d in args.days.split(",") if d.strip()}
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rounds = load_rounds(conn, syms, days)
    print(f"rounds with a winner: {len(rounds)}")

    cache: dict[tuple[str, str], tuple] = {}
    pcache: dict[tuple[str, str], tuple] = {}
    res: dict[str, list] = {}
    pairs: dict[str, list[float]] = {}

    for r in rounds:
        qs = quotes_for(conn, r["slug"])
        if len(qs) < 20:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(r["start_ms"] / 1000))
        key = (r["symbol"], day)
        if key not in cache:
            try:
                cache[key] = load_tape(*key)
            except FileNotFoundError:
                cache[key] = ([], [0.0], [], [0.0])
        if key not in pcache:
            pcache[key] = load_perp(*key)
        ts, pre, _px, absq = cache[key]
        perp_ts, _perp_pre, perp_px, perp_absq = pcache[key]
        strike = r["strike"] or qs[0]["strike"]
        if not strike:
            continue
        s0 = None
        cross_done = False
        pre_done = False
        perp_done = False
        vel_done = False
        sweep_done = False
        break_done = False
        rows_pre = {}
        for i, q in enumerate(qs):
            spot = q.get("spot")
            if not spot or not strike:
                continue
            tau = q["tau_s"] or 0.0
            dist_bps = (spot - strike) / strike * 1e4
            if s0 is None:
                s0 = 1 if dist_bps >= 0 else -1
            # ---- CROSS: the first quote on the far side of the strike
            if (not cross_done and tau >= args.min_tau
                    and ((s0 < 0 and dist_bps > 0) or (s0 > 0 and dist_bps < 0))):
                cross_done = True
                side = "Up" if dist_bps > 0 else "Down"
                p = entry_price(qs, i, side, args.entry_lag_ms)
                if p:
                    res.setdefault("CROSS", []).append(
                        (r["slug"], side, p, r["winner"], r["fee_rate_bps"], tau))
            # ---- PRE: approaching the strike, flow says it arrives
            if (not pre_done and args.min_tau <= tau <= args.max_tau
                    and abs(dist_bps) <= args.gap_bps and dist_bps != 0):
                side = "Up" if dist_bps < 0 else "Down"
                t_us = q["ts_ms"] * 1000
                c500 = win_sum(ts, pre, t_us - W500_US, t_us)
                c_prev = win_sum(ts, pre, t_us - W500_US - W300_US,
                                 t_us - W300_US)
                accel = c500 - c_prev
                flow = c500 if side == "Up" else -c500
                acc = accel if side == "Up" else -accel
                mom = 0.0
                for j in range(i - 1, max(-1, i - 4), -1):
                    if q["ts_ms"] - qs[j]["ts_ms"] >= 1000 and qs[j].get("spot"):
                        mom = (spot - qs[j]["spot"]) / qs[j]["spot"] * 1e4
                        break
                mom_ok = (mom > args.mom_bps) if side == "Up" else (mom < -args.mom_bps)
                if mom_ok:
                    p = entry_price(qs, i, side, args.entry_lag_ms)
                    if p:
                        res.setdefault("MOM_ONLY", []).append(
                            (r["slug"], side, p, r["winner"], r["fee_rate_bps"], tau))
                if flow > 0 and acc > 0 and (mom_ok or not args.require_mom):
                    pre_done = True
                    p = entry_price(qs, i, side, args.entry_lag_ms)
                    if p:
                        rows_pre = (r["slug"], side, p, r["winner"],
                                    r["fee_rate_bps"], tau)
                        res.setdefault("PRE", []).append(rows_pre)
                        if mom_ok:
                            res.setdefault("PRE_MOM", []).append(rows_pre)
                            oside = "Down" if side == "Up" else "Up"
                            pi = entry_price(qs, i, oside, args.entry_lag_ms)
                            if pi:
                                res.setdefault("PRE_INV", []).append(
                                    (r["slug"], oside, pi, r["winner"],
                                     r["fee_rate_bps"], tau))
                                pairs.setdefault("PRE_MOM", []).append(
                                    ev(side, p, r["winner"])
                                    - ev(oside, pi, r["winner"]))
                        rnd = random.Random(r["slug"]).random() < 0.5
                        cside = "Up" if rnd else "Down"
                        pc = entry_price(qs, i, cside, args.entry_lag_ms)
                        if pc:
                            res.setdefault("PRE_COIN", []).append(
                                (r["slug"], cside, pc, r["winner"],
                                 r["fee_rate_bps"], tau))
            # ---- PERP: does the FUTURES tape lead the venue's odds?
            if (not perp_done and perp_ts
                    and args.min_tau <= tau <= args.max_tau
                    and 0 < abs(dist_bps) <= args.gap_bps):
                side = "Up" if dist_bps < 0 else "Down"
                rp = perp_move_bps(perp_ts, perp_px, q["ts_ms"] * 1000,
                                   args.perp_win_ms)
                sgn = 1.0 if side == "Up" else -1.0
                if rp is not None and rp * sgn >= args.perp_bps:
                    perp_done = True
                    p = entry_price(qs, i, side, args.entry_lag_ms)
                    oside = "Down" if side == "Up" else "Up"
                    pi = entry_price(qs, i, oside, args.entry_lag_ms)
                    if p:
                        res.setdefault("PERP", []).append(
                            (r["slug"], side, p, r["winner"],
                             r["fee_rate_bps"], tau))
                        if pi:
                            res.setdefault("PERP_INV", []).append(
                                (r["slug"], oside, pi, r["winner"],
                                 r["fee_rate_bps"], tau))
                            pairs.setdefault("PERP", []).append(
                                ev(side, p, r["winner"])
                                - ev(oside, pi, r["winner"]))
            # ---- VEL: trade-arrival velocity (algo parent orders cluster)
            if (not vel_done and ts
                    and args.min_tau <= tau <= args.max_tau
                    and 0 < abs(dist_bps) <= args.gap_bps):
                side = "Up" if dist_bps < 0 else "Down"
                t_us = q["ts_ms"] * 1000
                j = bisect.bisect_right(ts, t_us)
                n_now = j - bisect.bisect_left(ts, t_us - VEL_WIN_US)
                n_base = (j - bisect.bisect_left(ts, t_us - VEL_BASE_US)) \
                    / (VEL_BASE_US / VEL_WIN_US)
                if n_base >= 1.0 and n_now / n_base >= args.vel_x:
                    c = win_sum(ts, pre, t_us - VEL_WIN_US, t_us)
                    if (c > 0) if side == "Up" else (c < 0):
                        vel_done = True
                        p = entry_price(qs, i, side, args.entry_lag_ms)
                        oside = "Down" if side == "Up" else "Up"
                        pi = entry_price(qs, i, oside, args.entry_lag_ms)
                        if p:
                            res.setdefault("VEL", []).append(
                                (r["slug"], side, p, r["winner"],
                                 r["fee_rate_bps"], tau))
                            if pi:
                                res.setdefault("VEL_INV", []).append(
                                    (r["slug"], oside, pi, r["winner"],
                                     r["fee_rate_bps"], tau))
                                pairs.setdefault("VEL", []).append(
                                    ev(side, p, r["winner"])
                                    - ev(oside, pi, r["winner"]))
            # ---- SWEEP: forced-sweep footprint (liquidations are not tagged
            #      any more, but they print as aggressive market orders)
            if (not sweep_done and args.sweep_usd > 0
                    and args.min_tau <= tau <= args.max_tau
                    and 0 < abs(dist_bps) <= args.gap_bps):
                side = "Up" if dist_bps < 0 else "Down"
                if args.sweep_tape == "spot":
                    sts, spre, spx, sap = ts, pre, _px, absq
                else:
                    sts, spre, spx, sap = (perp_ts, _perp_pre, perp_px,
                                           perp_absq)
                sw = sweep_at(sts, spre, sap, spx, q["ts_ms"] * 1000,
                              int(args.sweep_ms * 1000), args.sweep_usd,
                              args.sweep_ticks, args.sweep_ratio)
                if sw is not None and (sw[0] > 0) == (side == "Up"):
                    sweep_done = True
                    p = entry_price(qs, i, side, args.entry_lag_ms)
                    oside = "Down" if side == "Up" else "Up"
                    pi = entry_price(qs, i, oside, args.entry_lag_ms)
                    if p:
                        res.setdefault("SWEEP", []).append(
                            (r["slug"], side, p, r["winner"],
                             r["fee_rate_bps"], tau))
                        if pi:
                            res.setdefault("SWEEP_INV", []).append(
                                (r["slug"], oside, pi, r["winner"],
                                 r["fee_rate_bps"], tau))
                            pairs.setdefault("SWEEP", []).append(
                                ev(side, p, r["winner"])
                                - ev(oside, pi, r["winner"]))

            # ---- BREAK: volatility expansion (squeeze breakout) in the tape
            if (not break_done and perp_ts
                    and args.min_tau <= tau <= args.max_tau
                    and 0 < abs(dist_bps) <= args.gap_bps):
                side = "Up" if dist_bps < 0 else "Down"
                t_us = q["ts_ms"] * 1000
                mv = perp_move_bps(perp_ts, perp_px, t_us, 30_000)
                ratio = move_ratio(perp_ts, perp_px, t_us, 30_000)
                sgn = 1.0 if side == "Up" else -1.0
                if (mv is not None and ratio is not None and sgn * mv >= args.break_bps
                        and ratio >= args.break_x):
                    break_done = True
                    p = entry_price(qs, i, side, args.entry_lag_ms)
                    oside = "Down" if side == "Up" else "Up"
                    pi = entry_price(qs, i, oside, args.entry_lag_ms)
                    if p:
                        res.setdefault("BREAK", []).append(
                            (r["slug"], side, p, r["winner"],
                             r["fee_rate_bps"], tau))
                        if pi:
                            res.setdefault("BREAK_INV", []).append(
                                (r["slug"], oside, pi, r["winner"],
                                 r["fee_rate_bps"], tau))
                            pairs.setdefault("BREAK", []).append(
                                ev(side, p, r["winner"])
                                - ev(oside, pi, r["winner"]))

    print(f"\n{'rule':9} {'n':>5} {'mean_px':>8} {'win':>6} {'EV/$1':>8} "
          f"{'EV_net':>8} {'med_tau':>8} {'day-t':>7}")
    for k in ("CROSS", "PRE", "PRE_MOM", "PRE_INV", "PERP", "PERP_INV",
              "VEL", "VEL_INV", "SWEEP", "SWEEP_INV", "BREAK", "BREAK_INV",
              "MOM_ONLY", "PRE_COIN"):
        v = res.get(k) or []
        if not v:
            print(f"{k:9} {0:5}")
            continue
        gv = sum(((1 - p) / p if w == s else -1.0) for _, s, p, w, _, _ in v) / len(v)
        net = sum(((1 - p) / p if w == s else -1.0) - fee / 1e4
                  for _, s, p, w, fee, _ in v) / len(v)
        wins = sum(1 for _, s, p, w, _, _ in v if w == s) / len(v)
        px = sum(p for _, _, p, _, _, _ in v) / len(v)
        taus = sorted(t for *_, t in v)
        # day-clustered t: the honest statistic once rules share a day factor
        dgs: dict[str, list[float]] = {}
        for x in v:
            dgs.setdefault(slug_day(x[0]), []).append(
                (1 - x[2]) / x[2] if x[3] == x[1] else -1.0)
        dmeans = [sum(y) / len(y) for y in dgs.values()]
        dt = 0.0
        if len(dmeans) > 1:
            m = sum(dmeans) / len(dmeans)
            sd = (sum((x - m) ** 2 for x in dmeans)
                  / (len(dmeans) - 1)) ** 0.5
            dt = m / (sd / len(dmeans) ** 0.5) if sd else 0.0
        print(f"{k:9} {len(v):5} {px:8.3f} {wins:6.3f} {gv:+8.3f} {net:+8.3f} "
              f"{taus[len(taus)//2]:8.0f} {dt:+7.2f}")

    # paired view: on rounds where PRE fired, what did CROSS do?
    pre_slugs = {x[0] for x in (res.get("PRE") or [])}
    cross = {x[0]: x for x in (res.get("CROSS") or [])}
    both = [(x, cross[x[0]]) for x in (res.get("PRE") or []) if x[0] in cross]
    if both:
        evp = sum(((1 - p) / p if w == s else -1.0) for _, s, p, w, _, _ in
                  [b[0] for b in both]) / len(both)
        evc = sum(((1 - p) / p if w == s else -1.0) for _, s, p, w, _, _ in
                  [b[1] for b in both]) / len(both)
        print(f"\npaired rounds (both fired): {len(both)}  "
              f"PRE EV {evp:+.3f}  CROSS EV {evc:+.3f}")
        wp = sum(1 for b in both if b[0][3] == b[0][1]) / len(both)
        wc = sum(1 for b in both if b[1][3] == b[1][1]) / len(both)
        print(f"   win: PRE {wp:.3f} @ px {sum(b[0][2] for b in both)/len(both):.3f}"
              f"   CROSS {wc:.3f} @ px {sum(b[1][2] for b in both)/len(both):.3f}")

    # paired signal-vs-opposite-side test: is the direction skilful, or noise?
    for rule, diffs in pairs.items():
        if not diffs:
            continue
        m = sum(diffs) / len(diffs)
        if len(diffs) > 1:
            var = sum((x - m) ** 2 for x in diffs) / (len(diffs) - 1)
            se = (var / len(diffs)) ** 0.5
            print(f"\n{rule} minus OPPOSITE-SIDE at the same entries: {m:+.4f}/$1"
                  f"  se {se:.4f}  t {m/se if se else 0:+.2f}  (n={len(diffs)})")
            print(f"   -> directional skill per side net of the pair's costs: "
                  f"{m/2:+.4f}/$1")

    print("\nby symbol (EV/$1, n):")
    for k in ("CROSS", "PRE", "PRE_MOM", "PERP", "VEL", "SWEEP", "BREAK", "MOM_ONLY"):
        v = res.get(k) or []
        parts = []
        for sym in sorted(syms):
            sub = [x for x in v if x[0].startswith(
                {"BTCUSDT": "btc", "ETHUSDT": "eth", "BNBUSDT": "bnb"}[sym])]
            if sub:
                gv = sum(((1 - p) / p if w == s else -1.0)
                         for _, s, p, w, _, _ in sub) / len(sub)
                parts.append(f"{sym[:3]} {gv:+.3f}({len(sub)})")
        print(f"   {k:9} " + "  ".join(parts))
    print("\nby day (EV/$1, n) -- stability:")
    days_seen = sorted({slug_day(x[0]) for x in (res.get("CROSS") or [])})
    for k in ("CROSS", "PRE", "PRE_MOM", "PERP", "VEL", "SWEEP", "BREAK", "MOM_ONLY"):
        v = res.get(k) or []
        parts = []
        for d in days_seen:
            sub = [x for x in v if slug_day(x[0]) == d]
            if sub:
                gv = sum(((1 - p) / p if w == s else -1.0)
                         for _, s, p, w, _, _ in sub) / len(sub)
                parts.append(f"{d} {gv:+.3f}({len(sub)})")
        print(f"   {k:9} " + "  ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
