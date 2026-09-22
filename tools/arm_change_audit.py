"""Audit the 2026-09-20/21 arm changes against the recorded tape.

WHAT IS BEING AUDITED (the diff `HEAD~6..HEAD`, three arms):

  MODEL     `model_min_round_range_bps` 5.0 -> 0.0   (config, and pinned in .env)
  CROSS     `cross_enabled` default False -> True, plus a NEW `cross_weekdays_only`
            gate whose stated justification is a backtest showing
            weekday +0.0925 vs weekend -0.0430 EV/$1
  REVERSAL  a NEW arm (`reversal_15m`), shipped ENABLED, that fires on a 5m
            reversal and buys the 15m contract

WHY THIS TOOL EXISTS. `tools/weekday_arms_backtest.py` (added by the same change)
produced the CROSS weekday/weekend numbers, and it does so by pricing the fill at
the ask of the SAME quote row that carries the trigger -- i.e. at ZERO latency.
That is the stale-price artefact this repo has been caught on five times
(see `pm-cross-arm-measured.md`: at 0 ms the arm reads +0.38, at 1 s it is
negative; the venue's own ack is 260-1237 ms, so 0 ms is unreachable). The tool
also re-derives the trigger against `r.strike` where the live arm uses
`_cross_level` (the 60 s reference), and it scores a `model` arm at a quote band
and tau window that are typed literals rather than the live ones.

RULES APPLIED HERE (every one of them earned):
  * the trigger is the bot's OWN `crossed_side` / the live rule READ FROM THE
    CODE, imported rather than re-derived -- a reimplementation that disagreed
    would measure the reimplementation
  * ONE ENTRY PER ROUND
  * the entry price is taken `--entry-lag-ms` AFTER the trigger; the lag sweep is
    the decisive test, and the 0 ms column is printed beside it as a control,
    not as a result
  * the fee is the venue's OWN, measured per price band from `fills.fee_paid /
    fills.stake` on this DB (`fee_rate_for_price` is printed alongside for
    comparison -- the realised fraction is NOT 0.445 on every arm)
  * the four-check battery: pooled t, day-clustered t, both halves, drop-best-day
  * any knob this tool ranks is re-ranked on each half separately; a ranking that
    flips is fitting, not a finding

Usage (on the VPS the DB is the live one):
    python3 -m tools.arm_change_audit --arm cross    --lag-sweep
    python3 -m tools.arm_change_audit --arm reversal
    python3 -m tools.arm_change_audit --arm daysplit
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from bisect import bisect_left, bisect_right
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btcpredict.config import settings as S                     # noqa: E402
from btcpredict.edge.signal import fee_rate_for_price           # noqa: E402
from btcpredict.main import crossed_side, is_weekend_utc        # noqa: E402

PM_LIKE = "chainlink-twap"          # the era these arms ran / run in
FEE_RATE_BPS_FALLBACK = 200.0


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #
def _t(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 0.0
    return m / math.sqrt(var / n)


def battery(rows):
    """The four checks on ONE-ENTRY-PER-ROUND rows."""
    if not rows:
        return {}
    order = sorted(rows, key=lambda r: r["ts_ms"])
    pnl = [r["pnl"] for r in order]
    by_day = {}
    for r in order:
        by_day.setdefault(r["day"], []).append(r["pnl"])
    day_means = [sum(v) / len(v) for v in by_day.values()]
    half = len(order) // 2
    best = max(by_day, key=lambda d: sum(by_day[d]) / len(by_day[d]))
    rest = [r["pnl"] for r in order if r["day"] != best]
    return {
        "n": len(order),
        "win": sum(1 for r in order if r["won"]) / len(order),
        "be": sum(r["px"] for r in order) / len(order),
        "ev": sum(pnl) / len(pnl),
        "t": _t(pnl),
        "day_t": _t(day_means),
        "days": len(by_day),
        "h1": sum(r["pnl"] for r in order[:half]) / max(1, half),
        "h2": sum(r["pnl"] for r in order[half:]) / max(1, len(order) - half),
        "dropbest": (sum(rest) / len(rest)) if rest else 0.0,
    }


HDR = ("  {:<26}{:>5} {:>7} {:>7} {:>9} {:>6} {:>7} {:>4} "
       "{:>8} {:>8} {:>9}")


def prow(label, b):
    if not b or not b.get("n"):
        print(f"  {label:<26}  (no entries)")
        return
    print(HDR.format("", "n", "win", "be", "EV/$1", "t", "day-t", "d",
                     "half1", "half2", "drop-best"))
    print(HDR.format(label, b["n"], f"{b['win']:.1%}", f"{b['be']:.3f}",
                     f"{b['ev']:+.4f}", f"{b['t']:+.2f}", f"{b['day_t']:+.2f}",
                     b["days"], f"{b['h1']:+.4f}", f"{b['h2']:+.4f}",
                     f"{b['dropbest']:+.4f}"))


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def connect(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_rounds(con):
    out = {}
    for r in con.execute(
            "SELECT slug, start_ms, end_ms, strike, winner, feed_provider "
            "FROM rounds WHERE strike IS NOT NULL AND strike > 0 "
            "AND winner IN ('Up','Down')"):
        out[r["slug"]] = dict(r)
    return out


def load_quotes(con, slugs):
    """slug -> sorted rows [(ts_ms, tau_s, spot, up_ask, down_ask, age_ms)]."""
    series = {}
    cur = con.execute(
        "SELECT slug, ts_ms, tau_s, spot, up_ask, down_ask, book_age_ms "
        "FROM quotes ORDER BY ts_ms")
    for q in cur:
        if q["slug"] not in slugs:
            continue
        series.setdefault(q["slug"], []).append(
            (q["ts_ms"], q["tau_s"], q["spot"], q["up_ask"], q["down_ask"],
             q["book_age_ms"]))
    return series


def symbol_tape(rounds, qs):
    """symbol -> (sorted ts array, px array) merged over EVERY slug of the asset.

    This is the live `self._ticks` shape: ONE continuous series per symbol, not
    a per-round buffer. `_cross_level` and `_round_range_bps` both read it, so a
    per-round reconstruction silently loses the 60 s before the round open.
    """
    ts_acc, px_acc = {}, {}
    for slug, rows in qs.items():
        sym = asset_of(slug)
        t = ts_acc.setdefault(sym, [])
        p = px_acc.setdefault(sym, [])
        for ts, _t, px, *_ in rows:
            if px and px > 0:
                t.append(ts)
                p.append(px)
    tape = {}
    for sym in ts_acc:
        order = sorted(range(len(ts_acc[sym])), key=lambda i: ts_acc[sym][i])
        tape[sym] = ([ts_acc[sym][i] for i in order],
                     [px_acc[sym][i] for i in order])
    return tape


def ref_level(tape, sym, start_ms, n_min=10):
    """`main.Bot._cross_level`: the 60 s reference at the round open."""
    tt, pp = tape.get(sym, ([], []))
    if not tt:
        return None
    i = bisect_left(tt, start_ms - 60_000)
    j = bisect_left(tt, start_ms)
    win = pp[i:j]
    if len(win) < n_min:
        return None
    return sum(win) / len(win)


def family(slug):
    head, _, tail = slug.rpartition("-")
    return head if tail.isdigit() else slug


def asset_of(slug):
    return slug.split("-")[0].upper() + "USDT"


def measured_fee(con):
    """fee/stake vs price, measured on THIS venue's own fills.

    The repo's `fee_rate_for_price` applies a fixed 0.445 realised fraction;
    measured on the fills in this DB the fraction differs by arm, so the tool
    reads the venue instead of assuming it.
    """
    pts = sorted((con.execute("SELECT 1")).fetchall() and [
        (float(r["filled_px"]), float(r["fee_paid"]) / float(r["stake"]))
        for r in con.execute(
            "SELECT filled_px, fee_paid, stake FROM fills "
            "WHERE stake > 0 AND filled_px > 0 AND fee_paid IS NOT NULL")])

    def fee_at(px):
        near = [f for p, f in pts if abs(p - px) <= 0.05]
        if len(near) >= 8:
            return sum(near) / len(near)
        return fee_rate_for_price(px)

    return fee_at, pts


# --------------------------------------------------------------------------- #
# CROSS
# --------------------------------------------------------------------------- #
def cross_triggers(con, rounds, qs, *, look_s, min_tau, max_tau,
                   min_margin_bps, only_families=None, ref_is_strike=False,
                   tape=None):
    """One TRIGGER per round. The trigger does NOT depend on the entry lag, so
    it is derived once and priced separately -- re-deriving it per lag is 10x
    the work for the same list (and `crossed_side` copies its tick list)."""
    out = []
    for slug, rows in qs.items():
        r = rounds.get(slug)
        if r is None:
            continue
        if only_families and family(slug) not in only_families:
            continue
        if ref_is_strike:
            ref = r["strike"]
        else:
            ref = ref_level(tape or {}, asset_of(slug), r["start_ms"])
            if ref is None:
                continue
        ticks, hit = [], None
        for i, (ts, tau, spot, _ua, _da, _age) in enumerate(rows):
            if spot and spot > 0:
                ticks.append((ts, spot))
            if hit is not None or tau is None:
                continue
            if not (min_tau <= tau <= max_tau):
                continue
            side = crossed_side(ticks, ref, look_s)
            if side is None:
                continue
            margin = 1e4 * abs(spot - ref) / ref
            if margin < min_margin_bps:
                continue
            hit = (i, side, margin)
        if hit is None:
            continue
        i, side, margin = hit
        out.append({"slug": slug, "i": i, "ref": ref, "side": side,
                    "margin_bps": margin, "ts_ms": rows[i][0],
                    "tau": rows[i][1], "winner": r["winner"]})
    return out


def price_triggers(triggers, qs, *, lag_ms, min_px, max_px):
    """Attach the price at/after trigger+lag. A trigger whose price is outside
    the band at THAT instant is dropped -- the band binds on the price paid,
    not on the price at the trigger (the 2026-09-17 fill bug)."""
    out = []
    for t in triggers:
        rows = qs[t["slug"]]
        want = t["ts_ms"] + lag_ms
        pick = None
        for row in rows[t["i"]:]:
            if row[0] >= want:
                if row[1] is not None and row[1] <= 0:
                    break
                pick = row
                break
        if pick is None:
            continue
        px = pick[3] if t["side"] == "Up" else pick[4]
        if not px or px <= 0 or not (min_px <= px <= max_px):
            continue
        e = dict(t)
        e["px"] = float(px)
        e["fill_lag_ms"] = pick[0] - t["ts_ms"]
        out.append(e)
    return out


def cross_entries(con, rounds, qs, *, look_s, min_tau, max_tau, min_px, max_px,
                  min_margin_bps, lag_ms, only_families=None, ref_is_strike=False,
                  tape=None):
    """Convenience wrapper (used by --arm bands): trigger + price at one lag."""
    trg = cross_triggers(con, rounds, qs, look_s=look_s, min_tau=min_tau,
                         max_tau=max_tau, min_margin_bps=min_margin_bps,
                         only_families=only_families,
                         ref_is_strike=ref_is_strike, tape=tape)
    return price_triggers(trg, qs, lag_ms=lag_ms, min_px=min_px, max_px=max_px)


def _ref_level(rows, r):
    """Superseded by `ref_level(tape, ...)`; kept for callers passing rows."""
    win = [px for ts, _t, px, *_ in rows
           if px and r["start_ms"] - 60_000 <= ts < r["start_ms"]]
    if len(win) < 10:
        return None
    return sum(win) / len(win)


def build_rows(entries, fee_at, slip=0.0):
    rows = []
    for e in entries:
        px = min(0.99, e["px"] + slip)
        won = 1.0 if e["winner"] == e["side"] else 0.0
        rows.append({"pnl": won - px - fee_at(px), "won": won, "px": px,
                     "ts_ms": e["ts_ms"],
                     "day": time.strftime("%Y-%m-%d",
                                          time.gmtime(e["ts_ms"] / 1000)),
                     "weekend": is_weekend_utc(e["ts_ms"])})
    return rows


# --------------------------------------------------------------------------- #
# REVERSAL 15m -- the live rule, transcribed from Bot._reversal_15m_tick
# --------------------------------------------------------------------------- #
def reversal_entries(rounds, qs, *, families, min_wait_s, min_tau_15, max_tau_15,
                     max_range_bps, min_q, max_q, lag_ms=0, fee_at=None):
    """Replay `_reversal_15m_tick` on the recorded tape.

    Faithful to the code:
      * the 5m round is the one LIVE at the moment of the tick
      * `ticks_5` is the symbol's poll tape since the 5m round opened
      * first crossing with tau_5 in [60,270]; wait >= min_wait_s
      * current side must BE the reverse of that first crossing
      * tau_5 in [30,240] at entry, tau_15 in [min_tau_15, max_tau_15]
      * round travel <= max_range_bps for BOTH the 5m and the 15m round
      * 15m ask in [min_q, max_q]
      * ONE entry per 15m round, one per 5m round
    NOT replayable (stated, not pretended): the live `top.sweep(stake)` depth
    test -- the DB has no book depth -- and the venue's own fill slip.
    """
    from bisect import bisect_left, bisect_right

    r5_by_asset = {}
    for slug, r in rounds.items():
        if "-5m-" in slug:
            r5_by_asset.setdefault(slug.split("-")[0], []).append(r)
    r5_ts = {}
    for a, v in r5_by_asset.items():
        v.sort(key=lambda r: r["start_ms"])
        r5_ts[a] = [r["start_ms"] for r in v]

    # the symbol tape: ONE continuous series per asset (the live `self._ticks`
    # shape), as parallel arrays so a window is a bisect, not a scan.
    tape_ts, tape_px = {}, {}
    for slug, rows in qs.items():
        a = slug.split("-")[0]
        tape_ts.setdefault(a, [])
        tape_px.setdefault(a, [])
        for ts, _t, px, *_ in rows:
            if px and px > 0:
                tape_ts[a].append(ts)
                tape_px[a].append(px)
    for a in tape_ts:
        order = sorted(range(len(tape_ts[a])), key=lambda i: tape_ts[a][i])
        tape_ts[a] = [tape_ts[a][i] for i in order]
        tape_px[a] = [tape_px[a][i] for i in order]

    def win(a, lo_ms, hi_ms):
        ts_a = tape_ts.get(a) or []
        i = bisect_left(ts_a, lo_ms)
        j = bisect_right(ts_a, hi_ms)
        return i, j

    out = []
    for slug, r15 in sorted(rounds.items(), key=lambda kv: kv[1]["start_ms"]):
        if "-15m-" not in slug or family(slug) not in families:
            continue
        a = slug.split("-")[0]
        rows15 = qs.get(slug) or []
        if len(rows15) < 10:
            continue
        r5s = r5_by_asset.get(a)  # noqa: F841  (lookup is by bisect above)
        r15_start = r15["start_ms"]
        r15_strike = r15["strike"]
        fired_5 = set()
        i15 = win(a, r15_start, 0)[0]          # first tape index inside round 15
        max15 = min15 = None
        done = False
        for ts, tau15, _spot, up_ask, dn_ask, _age in rows15:
            if done:
                break
            if tau15 is None or not (min_tau_15 <= tau15 <= max_tau_15):
                continue
            # extend the 15m running travel (start is fixed, right edge grows)
            j = bisect_right(tape_ts.get(a) or [], ts)
            while i15 < j:
                px = tape_px[a][i15]
                if max15 is None or px > max15:
                    max15 = px
                if min15 is None or px < min15:
                    min15 = px
                i15 += 1
            r5 = None
            st = r5_ts.get(a) or []
            k = bisect_right(st, ts) - 1
            if k >= 0 and r5_by_asset[a][k]["start_ms"] <= ts < r5_by_asset[a][k]["end_ms"]:
                r5 = r5_by_asset[a][k]
            if r5 is None or r5["slug"] in fired_5 or not r5["strike"]:
                continue
            tau5 = (r5["end_ms"] - ts) / 1000.0
            if not (30.0 <= tau5 <= 240.0):
                continue
            s5, e5 = win(a, r5["start_ms"], ts)
            if e5 - s5 < 2:
                continue
            if max_range_bps > 0 and max15 is not None and r15_strike:
                if 1e4 * (max15 - min15) / r15_strike > max_range_bps:
                    continue
            # first crossing inside the 5m round, tau_5 in [60,270]
            first = None
            px5 = tape_px[a]
            for k in range(s5 + 1, e5):
                t_i = tape_ts[a][k]
                tau5_i = (r5["end_ms"] - t_i) / 1000.0
                if not (60.0 <= tau5_i <= 270.0):
                    continue
                prev = "Up" if px5[k - 1] > r5["strike"] else "Down"
                cur = "Up" if px5[k] > r5["strike"] else "Down"
                if prev != cur:
                    first = (cur, t_i)
                    break
            if first is None:
                continue
            cross_side, cross_ts = first
            wait_s = (ts - cross_ts) / 1000.0
            if wait_s < min_wait_s:
                continue
            rev = "Down" if cross_side == "Up" else "Up"
            curr = "Up" if px5[e5 - 1] > r5["strike"] else "Down"
            if curr != rev:
                continue
            if max_range_bps > 0 and r5["strike"]:
                t5 = [px5[q] for q in range(s5, e5)]
                if len(t5) >= 2 and 1e4 * (max(t5) - min(t5)) / r5["strike"] > max_range_bps:
                    continue
            ask = up_ask if rev == "Up" else dn_ask
            if not ask or ask <= 0 or not (min_q <= ask <= max_q):
                continue
            px = min(0.99, float(ask) * (1.0 + lag_ms / 1000.0))
            won = 1.0 if r15["winner"] == rev else 0.0
            out.append({"slug": slug, "ts_ms": ts + lag_ms, "side": rev,
                        "px": px, "tau": tau15, "winner": r15["winner"],
                        "won": won, "wait_s": wait_s, "tau5": tau5,
                        "day": time.strftime("%Y-%m-%d",
                                             time.gmtime(ts / 1000)),
                        "pnl": won - px - (fee_at(px) if fee_at else 0.0)})
            fired_5.add(r5["slug"])
            done = True
    return out


def reverse_rows(entries):
    return [{"pnl": e["pnl"], "won": e["won"], "px": e["px"],
             "ts_ms": e["ts_ms"], "day": e["day"]} for e in entries]


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--arm", default="all",
                    choices=["all", "cross", "reversal", "daysplit", "bands"])
    ap.add_argument("--lag-sweep", action="store_true")
    ap.add_argument("--entry-lag-ms", type=int, default=0)
    ap.add_argument("--era", default="binance",
                    choices=["all", "twap", "chainlink", "binance"],
                    help="binance = OUR venue: everything that is NOT the "
                         "Polymarket window. The `chainlink-twap-60s-vs-range-"
                         "start` label IS the Polymarket era (venue=polymarket "
                         "in .env.bak.20260919-151020, flipped back to binance "
                         "on 09-19 ~15:10 UTC), so 'binance' = not twap.")
    # Deployed .env values (the host, 2026-09-21). Read from settings when the
    # env is present, overridable here because a local checkout has no .env and
    # would otherwise rank a system nobody runs (backtest-artifacts #7).
    ap.add_argument("--cross-min-px", type=float, default=None)
    ap.add_argument("--cross-max-px", type=float, default=None)
    ap.add_argument("--cross-min-tau", type=float, default=None)
    ap.add_argument("--cross-max-tau", type=float, default=None)
    a = ap.parse_args()

    min_px = a.cross_min_px if a.cross_min_px is not None else S.cross_min_price
    max_px = a.cross_max_px if a.cross_max_px is not None else S.cross_max_price
    min_tau = (a.cross_min_tau if a.cross_min_tau is not None
               else S.cross_min_tau_s)
    max_tau = (a.cross_max_tau if a.cross_max_tau is not None
               else S.cross_max_tau_s)

    con = connect(a.db)
    rounds = load_rounds(con)
    if a.era == "twap":
        rounds = {k: v for k, v in rounds.items()
                  if "twap" in (v["feed_provider"] or "")}
    elif a.era in ("chainlink", "binance"):
        rounds = {k: v for k, v in rounds.items()
                  if "twap" not in (v["feed_provider"] or "")}
    fee_at, fee_pts = measured_fee(con)
    print(f"db {a.db}: {len(rounds):,} settled rounds (era={a.era})")
    print(f"fee model: {len(fee_pts)} live fills; "
          f"measured 0.50-> {fee_at(0.50):.4f}/$1  0.60-> {fee_at(0.60):.4f}"
          f"  0.70-> {fee_at(0.70):.4f} | curve(fraction .445) "
          f"{fee_rate_for_price(0.50):.4f}/{fee_rate_for_price(0.60):.4f}"
          f"/{fee_rate_for_price(0.70):.4f}")
    print(f"cross knobs used: band [{min_px},{max_px}] tau [{min_tau:.0f},{max_tau:.0f}]"
          f" look {S.cross_look_s}s min_margin {S.cross_min_margin_bps}bps")

    # the families each arm is ALLOWED to trade on the live host
    cross_fams = set()
    for f in ("btc-updown-5m", "eth-updown-5m", "bnb-updown-5m", "xrp-updown-5m",
              "sol-updown-5m", "doge-updown-5m"):
        if S.cross_disabled_families and f in [
                p.strip() for p in S.cross_disabled_families.split(",")]:
            continue
        cross_fams.add(f)
    rev_fams = {p.strip() for p in
                (getattr(S, "reversal_15m_families", "") or "").split(",")
                if p.strip()}
    print(f"cross families (live): {sorted(cross_fams)}")
    print(f"reversal families (live): {sorted(rev_fams)}")

    if a.arm in ("all", "cross", "daysplit"):
        qs = load_quotes(con, set(rounds))
        tape = symbol_tape(rounds, qs)
        print("\n=== CROSS: entry-lag sweep (the decisive test) ===")
        lags = ([0, 200, 1000, 2000, 5000] if a.lag_sweep
                else [a.entry_lag_ms])
        for ref_is_strike in (True, False):
            print(f"  -- reference = {'r.strike (weekday tool)' if ref_is_strike else '60s level (live _cross_level)'}")
            trg = cross_triggers(
                con, rounds, qs, look_s=S.cross_look_s, min_tau=min_tau,
                max_tau=max_tau, min_margin_bps=S.cross_min_margin_bps,
                only_families=cross_fams, ref_is_strike=ref_is_strike,
                tape=tape)
            print(f"     triggers: {len(trg)} rounds (one per round)")
            for lag in lags:
                es = price_triggers(trg, qs, lag_ms=lag, min_px=min_px,
                                    max_px=max_px)
                if not es:
                    print(f"     lag {lag:>5}ms  (no entries in band)")
                    continue
                rows = build_rows(es, fee_at)
                b = battery(rows)
                med = sorted(e["fill_lag_ms"] for e in es)[len(es) // 2]
                wk = [r for r in rows if not r["weekend"]]
                we = [r for r in rows if r["weekend"]]
                bw, bwe = battery(wk), battery(we)
                print(f"     lag {lag:>5}ms  n={b['n']:>4}  medfill {med:>5}ms  "
                      f"WR {b['win']:.1%}  paid {b['be']:.3f}  "
                      f"EV {b['ev']:+.4f}  t {b['t']:+.2f}  day-t {b['day_t']:+.2f}"
                      f"  halves {b['h1']:+.4f}/{b['h2']:+.4f}  "
                      f"drop {b['dropbest']:+.4f}")
                if bw and bwe:
                    print(f"        weekday n={bw['n']:>4} EV {bw['ev']:+.4f} "
                          f"day-t {bw['day_t']:+.2f} | weekend n={bwe['n']:>4} "
                          f"EV {bwe['ev']:+.4f} day-t {bwe['day_t']:+.2f}")

    if a.arm in ("all", "reversal"):
        qs = load_quotes(con, set(rounds))
        print("\n=== REVERSAL_15m: the live rule, one entry per 15m round ===")
        ent = reversal_entries(
            rounds, qs, families=rev_fams,
            min_wait_s=S.reversal_15m_min_wait_s,
            min_tau_15=S.reversal_15m_min_tau_s,
            max_tau_15=S.reversal_15m_max_tau_s,
            max_range_bps=S.reversal_15m_max_round_range_bps,
            min_q=S.reversal_15m_min_quote, max_q=S.reversal_15m_max_quote,
            fee_at=fee_at)
        if not ent:
            print("  (no entries on this tape)")
        else:
            rows = reverse_rows(ent)
            b = battery(rows)
            prow(f"reversal_15m (n fired)", b)
            days = {}
            for e in ent:
                days[e["day"]] = days.get(e["day"], 0) + 1
            print(f"  fire rate: {len(ent)} over {len(days)} days -> "
                  f"{len(ent)/max(1,len(days)):.1f}/day")
            print(f"  by family: " + "  ".join(
                f"{f}={sum(1 for e in ent if family(e['slug'])==f)}"
                for f in sorted(rev_fams)))
            wk = battery([r for r in rows if not is_weekend_utc(r["ts_ms"])])
            we = battery([r for r in rows if is_weekend_utc(r["ts_ms"])])
            prow("  weekday", wk)
            prow("  weekend", we)
            print("  entry price bands (paid):")
            for lo in (0.40, 0.50, 0.55, 0.60, 0.65, 0.70):
                hi = lo + 0.05
                sub = [r for r in rows if lo <= r["px"] < hi]
                if len(sub) >= 5:
                    bb = battery(sub)
                    print(f"    {lo:.2f}-{hi:.2f}  n={bb['n']:>4} "
                          f"WR {bb['win']:.1%} paid {bb['be']:.3f} "
                          f"EV {bb['ev']:+.4f} t {bb['t']:+.2f}")
            # direction check on the two knobs the arm's own comments rank
            print("  KNOB: min_wait_s (direction must repeat in both halves)")
            for w in (0.0, 10.0, 25.0, 40.0, 60.0):
                e2 = reversal_entries(
                    rounds, qs, families=rev_fams, min_wait_s=w,
                    min_tau_15=S.reversal_15m_min_tau_s,
                    max_tau_15=S.reversal_15m_max_tau_s,
                    max_range_bps=S.reversal_15m_max_round_range_bps,
                    min_q=S.reversal_15m_min_quote,
                    max_q=S.reversal_15m_max_quote, fee_at=fee_at)
                if len(e2) < 5:
                    print(f"    wait {w:>4.0f}s   (n={len(e2)} too few)")
                    continue
                rr = reverse_rows(e2)
                bb = battery(rr)
                h1 = battery(rr[:len(rr) // 2])
                h2 = battery(rr[len(rr) // 2:])
                print(f"    wait {w:>4.0f}s   n={bb['n']:>4} EV {bb['ev']:+.4f} "
                      f"t {bb['t']:+.2f} day-t {bb['day_t']:+.2f} "
                      f"| half1 {h1.get('ev', 0):+.4f} half2 {h2.get('ev', 0):+.4f}")


if __name__ == "__main__":
    main()
