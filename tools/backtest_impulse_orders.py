"""Impulse entries replayed under each order type, on the book we recorded.

`quotes` carries BOTH prices for the same second:
  up_price / down_price  the venue's DISPLAYED price -- what a market order's
                         quote/bundle path is priced against
  up_ask  / down_ask     the real top-of-book ask, from the REST book read
That is what makes this backtest possible at all: one entry can be priced three
ways on the same tick.

  market      fills at the displayed price (the venue's reference for a MARKET
              order). Measured slip against the book on our own fills: +0.16.
  limit@ask   a taker LIMIT crosses and fills at the BOOK's ask -- measured with
              one real order on 2026-09-12 (limit 0.18 vs ask 0.15 -> 0.1526).
  limit@fair  same, but only when the ask is under the digital fair minus a
              margin, i.e. the entry has to be worth taking, not merely visible.

Everything else is the live rule: a >=4bps/1s move in the underlying, tau 60-210,
the price band, ONE entry per round (the first trigger), $1 stake, the venue's
measured fee (0.98/px on a win), and the round's real winner.

Limits of the sample, stated up front rather than buried:
  * 1 Hz: the live trigger reads the websocket (33/s), so this under-counts
    triggers that open and close inside a second;
  * no depth data, so a limit fill is assumed whenever the ask exists. The venue
    reports qty 1 at every level, which our own 1.5-5.4 share fills contradict;
  * the winner comes from our own rounds table (Chainlink), not a re-simulation.

Usage:  python -m tools.backtest_impulse_orders [--db data/btcpredict.db]
"""
from __future__ import annotations

import argparse
import collections
import math
import sqlite3
import statistics as st
import time
from pathlib import Path

from btcpredict.config import settings
from btcpredict.pricing.digital import fair_value


def sigma_1m_by_round(con: sqlite3.Connection, rounds: list[tuple]) -> dict[str, float]:
    """60-bar realised 1m vol before each round, from the stored klines."""
    bars = con.execute(
        "SELECT open_ms, close FROM bars WHERE symbol='BTCUSDT' "
        "ORDER BY open_ms").fetchall()
    if len(bars) < 70:
        return {}
    out = {}
    for slug, start_ms, *_ in rounds:
        past = [b for b in bars if b[0] <= start_ms][-61:]
        if len(past) < 20:
            continue
        rets = [math.log(past[i][1] / past[i - 1][1])
                for i in range(1, len(past)) if past[i - 1][1] > 0]
        if len(rets) >= 10:
            out[slug] = st.pstdev(rets)
    return out


def build_entries(con: sqlite3.Connection, rounds: list[tuple],
                  sig: dict[str, float], fallback: float,
                  window_s: float) -> list[dict]:
    """Every round's first impulse trigger, for a given move window.

    The live arm measures the underlying's move over `impulse_window_s` (1.0 by
    default) and has NEVER been swept: the trigger's window decides which moves
    qualify at all, and a 1s window on a 1 Hz tape is a single tick -- so a
    longer window asks whether the venue lags a *slow* drift too, or only a
    jump. `rows` is the round's 1 Hz tape, so the spot `window_s` earlier is a
    row lookup rather than a re-query.
    """
    eps = int(window_s * 1000) + 700          # tolerate the 1 Hz sampling
    entries: list[dict] = []
    for slug, start_ms, end_ms, strike, winner in rounds:
        rows = con.execute(
            "SELECT ts_ms, tau_s, spot, up_price, down_price, up_ask, down_ask "
            "FROM quotes WHERE slug=? AND spot IS NOT NULL ORDER BY ts_ms",
            (slug,)).fetchall()
        if len(rows) < 3:
            continue
        sigma = sig.get(slug, fallback)
        for i, (ts_ms, tau_s, spot, up_px, dn_px, up_ask, dn_ask) in enumerate(rows):
            if (tau_s is None
                    or not (settings.min_tau_s <= tau_s <= settings.max_tau_s)):
                continue
            # the row closest to `window_s` back, walking only that far
            j, prev = i - 1, None
            while j >= 0 and ts_ms - rows[j][0] <= eps:
                prev = rows[j]
                j -= 1
            if prev is None or not prev[2] or not spot:
                continue
            move = 1e4 * (spot - prev[2]) / prev[2]
            side = "Up" if move > 0 else "Down" if move < 0 else None
            if side is None or abs(move) < settings.impulse_bps:
                continue
            px = up_px if side == "Up" else dn_px
            ask = up_ask if side == "Up" else dn_ask
            if px and settings.impulse_min_price <= px <= settings.impulse_max_price:
                fair_up = (fair_value(spot, strike, tau_s, sigma)
                           if sigma > 0 else None)
                fair_side = (fair_up if side == "Up"
                             else (1.0 - fair_up)
                             if fair_up is not None else None)
                entries.append({
                    "slug": slug, "tau": tau_s, "side": side,
                    "px": px, "ask": ask, "fair": fair_side,
                    "won": side == winner, "move": move,
                    "day": start_ms // 86400000})
            break            # one entry per round: the first trigger
    return entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--sweep-margin", action="store_true",
                    help="sweep the limit@fair safety margin (live: "
                         "settings.safety_margin)")
    ap.add_argument("--by-price", action="store_true",
                    help="break the entries down by the PRICE paid, i.e. "
                         "whether impulse_max_price is set too high")
    ap.add_argument("--by-move", action="store_true",
                    help="break the entries down by the size of the impulse we "
                         "reacted to, i.e. whether impulse_bps is set too low")
    ap.add_argument("--by-div", action="store_true",
                    help="break the entries down by the venue's DISPLAY LAG "
                         "(ask - displayed price), the mechanism the ticket "
                         "arm's div gate was built on, on this arm's own "
                         "population")
    ap.add_argument("--window-s", type=float, default=None,
                    help="measure the trigger's move over this many seconds "
                         "(live: settings.impulse_window_s)")
    ap.add_argument("--sweep-window", action="store_true",
                    help="sweep the trigger's move window: 0.5/1/2/3/5s")
    ap.add_argument("--by-tau", action="store_true",
                    help="break the entries down by tau at entry -- the venue's "
                         "display lag grows closer to the bell, so later "
                         "entries may be worth more")
    args = ap.parse_args()

    con = sqlite3.connect(Path(args.db))
    rounds = con.execute(
        "SELECT slug, start_ms, end_ms, strike, winner FROM rounds "
        "WHERE symbol='BTCUSDT' AND winner IN ('Up','Down') AND strike > 0 "
        "ORDER BY start_ms").fetchall()
    print(f"settled BTC rounds: {len(rounds):,}")
    sig = sigma_1m_by_round(con, rounds)
    fallback = (st.median(sig.values()) if sig else 0.0)
    missing = sum(1 for r in rounds if r[0] not in sig)
    print(f"rounds with a 60-bar vol estimate: {len(sig):,} "
          f"(median sigma_1m {fallback:.2e}); {missing:,} fall back to that "
          f"median, so their `fair` is indicative rather than measured")

    entries = build_entries(con, rounds, sig, fallback,
                            args.window_s or settings.impulse_window_s)

    if not entries:
        print("no impulse entries found")
        return
    n = len(entries)
    print(f"\nimpulse entries (one per round, first trigger): {n:,} "
          f"over {len(rounds):,} rounds")
    have_ask = [e for e in entries if e["ask"]]
    print(f"  with a book ask recorded: {len(have_ask):,} "
          f"({100*len(have_ask)/n:.0f}%)")
    print(f"  win rate: {100*sum(1 for e in entries if e['won'])/n:.1f}%")

    def score(name, price_of, keep, ents=None):
        sub = [(e, price_of(e)) for e in (entries if ents is None else ents)]
        sub = [(e, p) for e, p in sub if keep(e, p)]
        if not sub:
            print(f"  {name:<12} no fills"); return
        vals = [((0.98 / p) - 1.0) if e["won"] else -1.0 for e, p in sub]
        m = sum(vals) / len(vals)
        sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
        t = m / (sd / len(vals) ** 0.5) if sd else 0.0
        px = sum(p for _, p in sub) / len(sub)
        wr = sum(1 for e, _ in sub if e["won"]) / len(sub)
        # The repo's own bar for any claim: both halves positive, a DAY-clustered
        # t (a pooled t over-counts when a few rounds carry the result) and the
        # result with the best day removed. A margin that only looks good pooled
        # is a margin fitted to one afternoon.
        byday = collections.defaultdict(list)
        for (e, _), v in zip(sub, vals):
            byday[e["day"]].append(v)
        dm = [sum(v) / len(v) for v in byday.values()]
        dm_m = sum(dm) / len(dm)
        dm_sd = (sum((x - dm_m) ** 2 for x in dm) / len(dm)) ** 0.5
        td = dm_m / (dm_sd / len(dm) ** 0.5) if dm_sd else 0.0
        half = len(vals) // 2
        h1 = sum(vals[:half]) / max(1, half)
        h2 = sum(vals[half:]) / max(1, len(vals) - half)
        d1 = sorted(dm)[:-1]
        drop1 = sum(d1) / len(d1) if d1 else 0.0
        print(f"  {name:<12} n={len(sub):<5} fill {100*len(sub)/n:4.0f}%  "
              f"WR {100*wr:5.1f}%  px {px:.4f}  EV/$1 {m:+7.4f}  tot "
              f"{m*len(sub)*args.stake:+7.2f}  pooled-t {t:+5.2f}  daily-t "
              f"{td:+5.2f}  halves {h1:+.2f}/{h2:+.2f}  drop1 {drop1:+.2f}")
        return m

    print("\n=== the same entries, priced three ways ===")
    # You CANNOT buy below the best ask, and the venue refuses to try: a MARKET
    # order is quoted off the displayed price, so when the display sits under the
    # book (the normal state -- the display is barely half the spread here) the
    # bundle executes against the ask and FOK rejects it, as it did twice on
    # 2026-09-12 (ask 0.60 against a 0.15 display; ask 0.17 against 0.16). The
    # retry then pays the repriced ask. So `market` is modelled as the ASK, and
    # the extra drift the retry costs is measured separately below -- NOT folded
    # in here, because this sample does not record the retry's price.
    score("market", lambda e: max(e["px"], e["ask"] or e["px"]),
          lambda e, p: True)
    score("limit@ask", lambda e: e["ask"] or e["px"],
          lambda e, p: bool(e["ask"]))
    margin = settings.limit_take_margin
    score("limit@fair", lambda e: e["ask"] or e["px"],
          lambda e, p: bool(e["ask"]) and e["fair"] is not None
          and e["ask"] <= e["fair"] - margin)
    # Kept purely to expose why the display is a trap: this is the price the
    # venue's QUOTE advertises, and it is not reachable.
    score("display(n/a)", lambda e: e["px"], lambda e, p: True)
    if args.sweep_margin:
        print("\n=== limit@fair: how much edge must the entry show before we "
              "cross? ===")
        print("    (the ask must sit under fair - margin; the live value is "
              f"{settings.limit_take_margin:.3f})")
        for mg in (0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08,
                   0.10):
            score(f"m>={mg:.3f}", lambda e: e["ask"] or e["px"],
                  lambda e, p, mg=mg: bool(e["ask"]) and e["fair"] is not None
                  and e["ask"] <= e["fair"] - mg)
        print("\n    EV/$1 rises with the margin but the trade count falls, so read")
        print("    `tot`: what the whole sample would have paid at $1 an entry.")
    print("\n  the last row is what the venue's DISPLAYED price implies, and it is")
    print("  not implementable -- listed only to show the size of the trap: the")
    print("  market path cannot buy there, so its real cost is the ask plus the")
    print("  measured retry drift (+0.16 above the ask on our 8 market fills,")
    print("  against -0.014 on our 7 limit fills).")

    if args.by_price:
        # The fee fixes a break-even win rate of px/0.98, so the top of the band
        # is arithmetically dead: at 0.90 you need 92% and win 8.9% when right.
        print("\n=== every entry, priced at the ask, by the PRICE WE PAY ===")
        for a, z in ((0.0, 0.50), (0.50, 0.65), (0.65, 0.75), (0.75, 0.85),
                     (0.85, 1.01)):
            score(f"ask {a:.2f}-{z:.2f}", lambda e: e["ask"] or e["px"],
                  lambda e, p, a=a, z=z: bool(e["ask"]) and a <= p < z)
        print(f"\n=== and over what the LIVE path takes (ask <= fair - "
              f"{margin:.2f}) ===")
        for a, z in ((0.0, 0.50), (0.50, 0.65), (0.65, 0.75), (0.75, 0.85),
                     (0.85, 1.01)):
            score(f"take {a:.2f}-{z:.2f}", lambda e: e["ask"] or e["px"],
                  lambda e, p, a=a, z=z: bool(e["ask"]) and a <= p < z
                  and e["fair"] is not None and e["ask"] <= e["fair"] - margin)
        cheap = [e for e in entries
                 if e["ask"] and e["ask"] < 0.75 and e["fair"] is not None
                 and e["ask"] <= e["fair"] - margin]
        dear = [e for e in entries
                if e["ask"] and e["ask"] >= 0.75 and e["fair"] is not None
                and e["ask"] <= e["fair"] - margin]
        print(f"\n  the live path's entries split at 0.75: {len(cheap)} under, "
              f"{len(dear)} over")

    if args.by_move:
        # impulse_bps is 4.0. Entries are the FIRST trigger per round at or above
        # that, so bucketing by the move that triggered them is the marginal
        # value of raising the threshold.
        print("\n=== by the size of the impulse we reacted to ===")
        for a, z in ((4.0, 5.0), (5.0, 6.0), (6.0, 8.0), (8.0, 12.0),
                     (12.0, 1e9)):
            score(f"{a:.0f}-{z:.0f}bps", lambda e: e["ask"] or e["px"],
                  lambda e, p, a=a, z=z: bool(e["ask"]) and a <= abs(e["move"]) < z)

    if args.by_div:
        # The ticket arm's two conditioners were found inside ITS cell, so they
        # are fitted on 145 entries of one population. This arm is a different
        # population (a genuine 4bps/1s move of the underlying) and its `px` IS
        # the venue's displayed price, so `ask - px` is the same display lag.
        # If the mechanism is real it must show up here too; if it only works
        # inside the ticket's cell, it was fitted.
        print("\n=== by the venue's DISPLAY LAG (ask - display), limit@fair ===")
        for a, z in ((-1.0, 0.0), (0.0, 0.02), (0.02, 0.05), (0.05, 0.10),
                     (0.10, 1.0)):
            score(f"div {a:.2f} to {z:.2f}", lambda e: e["ask"] or e["px"],
                  lambda e, p, a=a, z=z: bool(e["ask"]) and e["fair"] is not None
                  and e["ask"] <= e["fair"] - margin
                  and a <= (e["ask"] - e["px"]) < z)
        print("\n    (and the same buckets over EVERY entry, priced at the ask)")
        for a, z in ((-1.0, 0.0), (0.0, 0.02), (0.02, 0.05), (0.05, 0.10),
                     (0.10, 1.0)):
            score(f"all {a:.2f} to {z:.2f}", lambda e: e["ask"] or e["px"],
                  lambda e, p, a=a, z=z: bool(e["ask"])
                  and a <= (e["ask"] - e["px"]) < z)

    if args.sweep_window:
        print("\n=== the trigger's move window: how far back does an impulse "
              "reach? (live: "
              f"{settings.impulse_window_s:.1f}s) ===")
        for w in (0.5, 1.0, 2.0, 3.0, 5.0):
            sub = build_entries(con, rounds, sig, fallback, w)
            print(f"  -- {len(sub)} triggers at a {w:.1f}s window")
            score(f"w={w:.1f}s", lambda e: e["ask"] or e["px"],
                  lambda e, p: bool(e["ask"]) and e["fair"] is not None
                  and e["ask"] <= e["fair"] - margin, ents=sub)

    if args.by_tau:
        print("\n=== by tau at entry (seconds left in the round) -- the venue's "
              "display lag grows towards the bell, so a later entry should be "
              "worth more if that is the mechanism ===")
        for a, z in ((settings.min_tau_s, 90), (90, 120), (120, 160),
                     (160, 211)):
            score(f"tau {a:.0f}-{z:.0f}", lambda e: e["ask"] or e["px"],
                  lambda e, p, a=a, z=z: bool(e["ask"]) and e["fair"] is not None
                  and e["ask"] <= e["fair"] - margin
                  and a <= e["tau"] < z)

    print("\n=== the dislocations, i.e. where the display and the book disagree ===")
    wide = [e for e in entries if e["ask"] and abs(e["px"] - e["ask"]) >= 0.05]
    print(f"  entries with |display - ask| >= 0.05: {len(wide)}")
    for e in wide[:12]:
        print(f"    {e['slug']:<28} {e['side']:<5} tau {e['tau']:5.1f}  display "
              f"{e['px']:.2f}  book ask {e['ask']:.2f}  fair "
              f"{e['fair']:.3f}  {'WON' if e['won'] else 'lost'}")
    if wide:
        paid_disp = [-1.0 if not e["won"] else (0.98 / e["px"] - 1) for e in wide]
        paid_ask = [(-1.0 if not e["won"] else (0.98 / e["ask"] - 1))
                    for e in wide]
        print(f"  EV/$1 on those: market {sum(paid_disp)/len(paid_disp):+.4f}  "
              f"limit@ask {sum(paid_ask)/len(paid_ask):+.4f}")


if __name__ == "__main__":
    main()
