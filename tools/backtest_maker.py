"""Does POSTING at our own fair actually fill at a profit? Simulated on the venue.

The claim in one line: a taker pays the spread to chase a move the whole book has
already seen, while a resting bid is only filled when someone crosses to us -- and
this venue charges makers NO fee at all (the schedule is takerOnly).

This runs on the only data that can settle it -- our venue's own recorded book
(`quotes`: 240k rows over 1,169 btc-updown-5m rounds, 91% carrying a real
top-of-book ask) joined to the `rounds` table's Chainlink winner. The limit price
comes from the DEPLOYED rule (`btcpredict.main.maker_limit_px`), not a copy, so
this backtest cannot drift from what the bot will actually do.

WHAT THIS DOES NOT MODEL, and every one of these flatters the maker:
  * depth. The venue reports qty 1 at every level, which our own 1.5-5.4 share
    fills contradict, so a resting order is assumed to fill IN FULL the first
    time the tape trades through it.
  * queue position. We assume we are first in line at our price.
  * the 1 Hz poll. A touch that appears and vanishes inside one second is
    invisible here and to the bot; a real fill additionally needs the touch to
    still be there when the next poll goes out.
  * adverse selection is the thing being measured, not assumed away: if the only
    time we get filled is when we are wrong, the numbers below will say so.
So the fill RATE and the WR-on-fills are printed next to the EV, never buried.

Every score is an EXECUTABLE price. The venue's displayed price is not a price
you can trade (measured: display 0.15 against a book of 0.60 during a move), so
it appears only as the deliberately-wrong `market` row for contrast.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btcpredict.config import settings                      # noqa: E402
from btcpredict.main import maker_limit_px                  # noqa: E402
from btcpredict.pricing.digital import fair_value           # noqa: E402
from tools.backtest_impulse_orders import sigma_1m_by_round  # noqa: E402


def load_rounds(con: sqlite3.Connection) -> list[tuple]:
    """Rounds with a Chainlink verdict. Our own `rounds` table is ground truth."""
    return con.execute(
        "SELECT slug, start_ms, end_ms, strike, winner FROM rounds "
        "WHERE symbol='BTCUSDT' AND winner IN ('Up','Down') AND strike > 0 "
        "ORDER BY start_ms").fetchall()


def tstat(xs: list[float]) -> float:
    if len(xs) < 3:
        return float("nan")
    m, sd = st.fmean(xs), st.stdev(xs)
    return m / (sd / math.sqrt(len(xs))) if sd > 0 else float("nan")


def day_cluster(returns: list[float], days: list[int]) -> tuple[float, int]:
    """t across per-day MEANS. One lucky day must not carry the whole result."""
    by: dict[int, list[float]] = {}
    for r, d in zip(returns, days):
        by.setdefault(d, []).append(r)
    means = [st.fmean(v) for v in by.values()]
    return tstat(means), len(means)


def battery(name: str, rows: list[dict]) -> None:
    """EV per $1 staked, then the four checks that have killed every other idea.

    A mechanism is only interesting if it survives ALL of them: pooled t, a
    day-clustered t (so one afternoon cannot fake it), both halves of the
    period, and dropping its single best day.
    """
    fills = [e for e in rows if e["filled"]]
    if len(fills) < 5:
        print(f"  {name:<22} only {len(fills)} fill(s) -- unreadable")
        return
    rets = [e["ret"] for e in fills]
    days = [e["day"] for e in fills]
    ev = st.fmean(rets)
    pooled = tstat(rets)
    day_t, ndays = day_cluster(rets, days)
    order = sorted(fills, key=lambda e: e["day"])
    half = len(order) // 2
    h1 = st.fmean([e["ret"] for e in order[:half]]) if half else float("nan")
    h2 = st.fmean([e["ret"] for e in order[half:]])
    best = max(fills, key=lambda e: e["ret"])
    kept = [e for e in fills if e is not best]
    drop1 = st.fmean([e["ret"] for e in kept]) if kept else float("nan")
    wr = 100 * sum(1 for e in fills if e["won"]) / len(fills)
    fill_rate = 100 * len(fills) / len(rows)
    print(f"  {name:<22} n={len(fills):<5} fill {fill_rate:4.0f}%  WR {wr:5.1f}%  "
          f"EV {ev:+.4f}/$1  t {pooled:+5.2f}  day-t {day_t:+5.2f} ({ndays}d)  "
          f"halves {h1:+.3f}/{h2:+.3f}  drop1 {drop1:+.3f}")
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--wait-s", type=float, default=None,
                    help="how long the resting order is left up (live: "
                         "settings.impulse_maker_wait_s)")
    ap.add_argument("--fill-ref", choices=["ask", "display"], default="ask",
                    help="'ask' = the book must trade through our price (default, "
                         "executable); 'display' = the venue's displayed price "
                         "reaches it (the looser, less honest test)")
    ap.add_argument("--window-s", type=float, default=None)
    ap.add_argument("--by-limit", action="store_true",
                    help="break fills down by the price we rested at")
    args = ap.parse_args()

    wait_s = args.wait_s if args.wait_s is not None else settings.impulse_maker_wait_s
    window_s = args.window_s if args.window_s is not None else settings.impulse_window_s
    tick = settings.limit_ask_tick

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rounds = load_rounds(con)
    sig = sigma_1m_by_round(con, rounds)
    fallback = st.fmean(sig.values()) if sig else 0.0
    print(f"rounds with a Chainlink verdict: {len(rounds):,}   "
          f"vol solved for {len(sig):,}   sigma_1m fallback {fallback:.6f}")
    print(f"rule: window {window_s:.1f}s >= {settings.impulse_bps:.1f}bps, "
          f"px band {settings.impulse_min_price:.2f}-{settings.impulse_max_price:.2f}, "
          f"tau {settings.min_tau_s:.0f}-{settings.max_tau_s:.0f}s, "
          f"rest {wait_s:.0f}s, tick {tick:.2f}, fill-ref={args.fill_ref}")
    print()

    entries: list[dict] = []
    for slug, start_ms, end_ms, strike, winner in rounds:
        rows = con.execute(
            "SELECT ts_ms, tau_s, spot, up_price, down_price, up_ask, down_ask "
            "FROM quotes WHERE slug=? AND spot IS NOT NULL ORDER BY ts_ms",
            (slug,)).fetchall()
        if len(rows) < 3:
            continue
        sigma = sig.get(slug, fallback)
        if not sigma or sigma <= 0:
            continue
        for i, (ts_ms, tau_s, spot, up_px, dn_px, up_ask, dn_ask) in enumerate(rows):
            if (tau_s is None
                    or not (settings.min_tau_s <= tau_s <= settings.max_tau_s)):
                continue

            # --- the SAME trigger the live arm uses -------------------------
            j, prev = i - 1, None
            eps_ms = int(window_s * 1000) + 700
            while j >= 0 and ts_ms - rows[j][0] <= eps_ms:
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
            if not (px and settings.impulse_min_price <= px
                    <= settings.impulse_max_price):
                continue

            fair_up = fair_value(spot, strike, tau_s, sigma)
            fair_side = fair_up if side == "Up" else 1.0 - fair_up

            # --- the DEPLOYED maker rule ------------------------------------
            limit = maker_limit_px(fair_side, ask, tick)
            if limit is None:
                entries.append({"slug": slug, "day": start_ms // 86400000,
                                "side": side, "limit": None, "filled": False,
                                "won": side == winner, "ret": 0.0,
                                "ask": ask, "px": px, "tau": tau_s,
                                "move": move, "skipped": True})
                break

            # --- would it have filled? -------------------------------------
            # A resting bid at `limit` fills when the tape trades THROUGH it:
            # the best ask dropping to our price means sellers crossed down past
            # us. Scanning only up to the round's end, and only forward in time.
            filled = False
            for later in rows[i + 1:]:
                l_ts, l_tau, _sp, l_up_px, l_dn_px, l_up_ask, l_dn_ask = later
                if l_ts - ts_ms > wait_s * 1000 or l_ts > end_ms:
                    break
                if l_tau is not None and l_tau <= 0:
                    break
                if args.fill_ref == "ask":
                    ref = l_up_ask if side == "Up" else l_dn_ask
                else:
                    ref = l_up_px if side == "Up" else l_dn_px
                if ref is not None and ref <= limit:
                    filled = True
                    break

            # Buy `stake/limit` shares at our price; each pays 1 or 0. Makers pay
            # no fee (takerOnly schedule), so the limit IS the cost.
            won = side == winner
            entries.append({
                "slug": slug, "day": start_ms // 86400000, "side": side,
                "limit": limit, "filled": filled, "won": won, "skipped": False,
                "ret": ((1.0 if won else 0.0) - limit) / limit if filled else 0.0,
                "ask": ask, "px": px, "tau": tau_s, "move": move})
            break                      # one entry per round, like the live arm

    trig = [e for e in entries if not e["skipped"]]
    skipped = [e for e in entries if e["skipped"]]
    ndays = len({e["day"] for e in entries}) or 1
    print(f"rounds that produced a trigger: {len(entries):,}  "
          f"(fair outside (0,1), no price to rest on: {len(skipped):,})")
    print(f"trigger rate: {100*len(entries)/max(len(rounds),1):.1f}% of rounds, "
          f"{len(entries)/ndays:.1f}/day over {ndays} days")
    print()

    # --- the maker, against the two taker flavours -------------------------
    print("maker path (what is deployed):")
    battery("limit@fair (maker)", trig)
    print()

    # Taker counterfactual on the SAME entries: cross at the ask and pay the
    # fee the venue actually charged us. Fee rate is MEASURED from our own
    # fills (fee_paid / stake), not taken from the published schedule -- the
    # schedule was wrong by 50x once already.
    fee_row = con.execute(
        "SELECT avg(fee_paid / stake) FROM fills WHERE mode='live' "
        "AND stake > 0 AND fee_paid > 0").fetchone()
    fee_frac = fee_row[0] or 0.0
    print(f"taker counterfactual on the same {len(trig):,} entries "
          f"(measured fee {100*fee_frac:.3f}% of stake):")
    for e in trig:
        a = e["ask"]
        cost = (a * (1.0 + fee_frac)) if a else None
        e["taker_filled"] = bool(a)
        e["taker_ret"] = (((1.0 if e["won"] else 0.0) - cost) / cost
                          if cost else 0.0)
    taker_rows = [dict(e, filled=e["taker_filled"], ret=e["taker_ret"])
                  for e in trig]
    battery("limit@ask (taker)", taker_rows)
    mkt_rows = [dict(e, filled=bool(e["ask"] or e["px"]),
                     ret=(((1.0 if e["won"] else 0.0)
                           - max(e["px"], e["ask"] or e["px"]))
                          / max(e["px"], e["ask"] or e["px"])))
                for e in trig]
    battery("market (display)", mkt_rows)
    print()
    print("  the first row posts, the second crosses at the book, the third is")
    print("  the venue's displayed price -- which is not a price you can trade.")
    print()

    # --- is the maker edge uniform, or one lucky price? --------------------
    if args.by_limit:
        print("fills by the price we rested at (is the edge uniform?):")
        buckets: dict[str, list[dict]] = {}
        for e in trig:
            if not e["filled"]:
                continue
            lo = int(e["limit"] * 10) / 10
            buckets.setdefault(f"{lo:.1f}-{lo + 0.1:.1f}", []).append(e)
        for k in sorted(buckets):
            v = buckets[k]
            wr = 100 * sum(1 for x in v if x["won"]) / len(v)
            print(f"    {k:<8} n={len(v):<5} WR {wr:5.1f}%  "
                  f"EV {st.fmean([x['ret'] for x in v]):+.4f}/$1  "
                  f"t {tstat([x['ret'] for x in v]):+5.2f}")
        print()

    # --- attempts vs fills, the maker's one real failure mode --------------
    lat = [e["tau"] for e in trig]
    if lat:
        print(f"tau at trigger: median {st.median(lat):.0f}s "
              f"(wait {wait_s:.0f}s must fit inside it)")
    unfilled = [e for e in trig if not e["filled"]]
    if unfilled:
        print(f"unfilled entries: {len(unfilled):,} "
              f"({100*len(unfilled)/len(trig):.0f}% of triggers)")
        uf_ask = [e["ask"] for e in unfilled if e["ask"]]
        if uf_ask:
            print(f"  they were resting against a median ask of {st.median(uf_ask):.3f}"
                  f" -- the book never came down to us")
    print()
    print("a fill rate near 100% here would be the tell that this model is wrong:")
    print("it means depth and queue position are doing the work, and both are")
    print("routinely worse live than on paper.")


if __name__ == "__main__":
    main()
