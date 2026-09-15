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
DATA = "data"


def load_trades(sym: str, day: str):
    """(ts_us list, prefix-sum list of signed taker volume)."""
    path = f"{DATA}/aggTrades/{sym}-aggTrades-{day}.zip"
    ts: list[int] = []
    pre: list[float] = [0.0]
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as raw:
            for line in raw:
                f = line.decode().split(",")
                if len(f) < 7:
                    continue
                ts.append(int(f[5]))
                q = float(f[2])
                pre.append(pre[-1] + (-q if f[6] == "True" else q))
    return ts, pre


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
    res: dict[str, list] = {}
    pair: list[float] = []

    def q_of(r) -> tuple[int, int, float, float]:
        return (0, 0, 0.0, 0.0)                      # placeholder (unused)

    for r in rounds:
        qs = quotes_for(conn, r["slug"])
        if len(qs) < 20:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(r["start_ms"] / 1000))
        key = (r["symbol"], day)
        if key not in cache:
            try:
                cache[key] = load_trades(*key)
            except FileNotFoundError:
                cache[key] = ([], [0.0])
        ts, pre = cache[key]
        strike = r["strike"] or qs[0]["strike"]
        if not strike:
            continue
        s0 = None
        cross_done = False
        pre_done = False
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
                                ev_l = ((1 - p) / p if r["winner"] == side
                                        else -1.0)
                                ev_s = ((1 - pi) / pi if r["winner"] == oside
                                        else -1.0)
                                pair.append(ev_l - ev_s)
                        rnd = random.Random(r["slug"]).random() < 0.5
                        cside = "Up" if rnd else "Down"
                        pc = entry_price(qs, i, cside, args.entry_lag_ms)
                        if pc:
                            res.setdefault("PRE_COIN", []).append(
                                (r["slug"], cside, pc, r["winner"],
                                 r["fee_rate_bps"], tau))

    print(f"\n{'rule':9} {'n':>5} {'mean_px':>8} {'win':>6} {'EV/$1':>8} "
          f"{'EV_net':>8} {'med_tau':>8}")
    for k in ("CROSS", "PRE", "PRE_MOM", "PRE_INV", "MOM_ONLY", "PRE_COIN"):
        v = res.get(k) or []
        if not v:
            print(f"{k:9} {0:5}")
            continue
        ev = sum(((1 - p) / p if w == s else -1.0) for _, s, p, w, _, _ in v) / len(v)
        net = sum(((1 - p) / p if w == s else -1.0) - fee / 1e4
                  for _, s, p, w, fee, _ in v) / len(v)
        wins = sum(1 for _, s, p, w, _, _ in v if w == s) / len(v)
        px = sum(p for _, _, p, _, _, _ in v) / len(v)
        taus = sorted(t for *_, t in v)
        print(f"{k:9} {len(v):5} {px:8.3f} {wins:6.3f} {ev:+8.3f} {net:+8.3f} "
              f"{taus[len(taus)//2]:8.0f}")

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
    if pair:
        m = sum(pair) / len(pair)
        if len(pair) > 1:
            var = sum((x - m) ** 2 for x in pair) / (len(pair) - 1)
            se = (var / len(pair)) ** 0.5
            print(f"\nPRE_MOM minus OPPOSITE-SIDE at the same entries: {m:+.4f}/$1"
                  f"  se {se:.4f}  t {m/se if se else 0:+.2f}  (n={len(pair)})")
            print(f"   -> directional skill per side net of the pair's costs: "
                  f"{m/2:+.4f}/$1")

    print("\nby symbol (EV/$1, n):")
    for k in ("CROSS", "PRE", "PRE_MOM", "MOM_ONLY"):
        v = res.get(k) or []
        parts = []
        for sym in sorted(syms):
            sub = [x for x in v if x[0].startswith(
                {"BTCUSDT": "btc", "ETHUSDT": "eth", "BNBUSDT": "bnb"}[sym])]
            if sub:
                ev = sum(((1 - p) / p if w == s else -1.0)
                         for _, s, p, w, _, _ in sub) / len(sub)
                parts.append(f"{sym[:3]} {ev:+.3f}({len(sub)})")
        print(f"   {k:9} " + "  ".join(parts))
    print("\nby day (EV/$1, n) -- stability:")
    days_seen = sorted({slug_day(x[0]) for x in (res.get("CROSS") or [])})
    for k in ("CROSS", "PRE", "PRE_MOM", "PRE_INV", "MOM_ONLY"):
        v = res.get(k) or []
        parts = []
        for d in days_seen:
            sub = [x for x in v if slug_day(x[0]) == d]
            if sub:
                ev = sum(((1 - p) / p if w == s else -1.0)
                         for _, s, p, w, _, _ in sub) / len(sub)
                parts.append(f"{d} {ev:+.3f}({len(sub)})")
        print(f"   {k:9} " + "  ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
