"""Run every arm at once? The OVERLAP decides it, so measure that first.

The ask was "run the profitable strategies at the same time". The strategies are
not independent bets on different rounds -- they are three different reasons to
enter THE SAME 5-minute round, and the bot can only hold one position in a round
(`max_open_rounds=1`, plus the venue position check). So the number of trades is
capped by the number of ROUNDS, and adding an arm only adds trades on the rounds
the other arms leave alone.

That makes the whole question a set-intersection question, which is measurable:

  * per arm: which rounds it wants, and what entering them was worth
  * overlap: how many rounds two or more arms want (those are NOT extra trades)
  * union: replicating all arms at once, one position per round, awarded to
    whichever arm would have fired FIRST in time -- which is what a single
    account actually gets
  * ceiling: 5-minute rounds cap this venue at 12 trades/hour no matter how many
    arms are running

Every arm is reconstructed from recorded quotes with the live gate: the model
arm calls the real `edge.signal.evaluate`, the impulse arm uses the live
window/threshold, the ticket arm uses the live band/conditioners. All three pay
the venue's own fee curve (`200bps * (1-px)/px` of stake, the corrected
coefficient), and all fills are at the recording's executable ask.

Not modelled, so not claimed: depth, queue position, the 1 Hz poll's blind spot,
and slippage. The model arm's numbers are therefore an upper bound; the OVERLAP
is not affected by any of them, and the overlap is the point.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btcpredict.config import settings                       # noqa: E402
from btcpredict.edge.signal import (                         # noqa: E402
    FeeModel, evaluate, fee_rate_for_price, take_limit_px)
from btcpredict.pricing.digital import fair_value, moneyness_sd  # noqa: E402

ARMS = ("model", "impulse", "ticket")


def load(con: sqlite3.Connection) -> list[dict]:
    """Settled rounds with a Chainlink verdict and their recorded quote tape."""
    rounds = con.execute(
        "SELECT slug, start_ms, end_ms, strike, winner, end_price FROM rounds "
        "WHERE symbol='BTCUSDT' AND winner IN ('Up','Down') AND strike > 0 "
        "ORDER BY start_ms").fetchall()
    out = []
    for slug, start_ms, end_ms, strike, winner, end_price in rounds:
        rows = con.execute(
            "SELECT ts_ms, tau_s, spot, up_price, down_price, up_ask, down_ask "
            "FROM quotes WHERE slug=? ORDER BY ts_ms", (slug,)).fetchall()
        if len(rows) < 3:
            continue
        out.append({"slug": slug, "start_ms": start_ms, "end_ms": end_ms,
                    "strike": strike, "winner": winner, "rows": rows,
                    "day": start_ms // 86400000})
    return out


def rolling_sigma(rs: list[dict], idx: int, window: int = 48) -> float:
    """Out-of-sample sigma_1m from the `window` rounds settled BEFORE idx."""
    lo = max(0, idx - window)
    rets = [math.log(r["end_price"] / r["strike"]) / math.sqrt(5.0)
            for r in rs[lo:idx]
            if r.get("end_price") and r["strike"] > 0 and r["end_price"] > 0]
    if len(rets) < 20:
        return 0.0004
    return max(0.00015, st.pstdev(rets))


def _cost_per_share(px: float) -> float:
    """Ask plus the venue's own fee curve -- identical cost for every arm."""
    return px * (1.0 + fee_rate_for_price(px))


def enter(slug: str, rnd: dict, ts_ms: int, tau: float, side: str, px: float,
          ev: float, why: str, mz: float = 0.0,
          fair: float | None = None) -> dict:
    cost = _cost_per_share(px)
    won = side == rnd["winner"]
    return {"slug": slug, "ts_ms": ts_ms, "tau": tau, "side": side, "px": px,
            "cost": cost, "won": won, "ev": ev, "why": why,
            "day": rnd["day"], "mz": mz, "fair": fair,
            "ret": ((1.0 if won else 0.0) - cost) / cost}


def stake_for(mz: float, bankroll: float,
              tilt: str | None = None) -> float:
    """The LIVE sizing rule, per trade: 2% of bankroll, tilted by inv_z.

    `main.Bot._stake_for` = stake_for_fraction(bankroll, 0.02, floored to the
    venue minimum) then clamped after `tilt_factor`, which is 0.30/|z| bounded
    to [0.5, 2.0]. The tilt's own justification says the per-$1 return FALLS
    with |z|, so it deliberately puts more money on the higher-return trades.

    `tilt` overrides `settings.stake_tilt` so the rule can be measured, which
    matters because on this recording the tilt appears to do the OPPOSITE of
    its rationale: the stake-weighted return comes out BELOW the unweighted
    one, i.e. it concentrates money on the worse trades.
    """
    mode = settings.stake_tilt if tilt is None else tilt
    base = bankroll * settings.stake_fraction
    if settings.stake_floor_to_minimum:
        base = max(base, settings.min_order_usdt)
    if mode == "inv_z" and mz and mz > 0:
        t = max(settings.tilt_min,
                min(settings.tilt_max, settings.tilt_reference_z / mz))
    else:
        t = 1.0
    return min(max(base * t, settings.min_order_usdt), settings.stake_cap_usdt)


def _execute(rnd: dict, i: int, side: str, fair_side: float, ev: float,
             why: str, take_px: float | None = None,
             margin: float | None = None, mz: float = 0.0, *,
             exec_mode: str = "limit_take", latency_s: float = 1.0) -> dict | None:
    """How the order actually fills -- which is NOT "at the ask we decided on".

    The live path is `order_type=limit_take`:
      * the order leaves on a LATER poll than the decision and is priced against
        a fresh quote, modelled here as the first recorded tick at or after
        `latency_s` (the 1 Hz poll cadence);
      * the limit is `take_limit_px(fair - limit_take_margin, ...)`, never more
        than a tick above the ask, so an edge >= the margin crosses and fills AT
        the ask, while a SMALLER edge leaves a limit below the ask;
      * a limit that does not cross rests for `limit_wait_s` and then becomes a
        MISSED ROUND. The old replay filled every decision at the ask, so it
        could not see that cost at all -- and `min_edge` (0.01) sits below
        `limit_take_margin` (0.04), so this is the common case, not the edge
        case.

    Fill prices: measured on 15 live fills, the 7 LIMIT fills came in at -0.014
    against the ask on offer, so pricing a fill at the ask is slightly
    conservative in our favour. The 8 MARKET fills averaged +0.16, which is why
    `market` is kept for contrast rather than as the default.
    """
    rows = rnd["rows"]
    ts0 = rows[i][0]
    k = i
    while k < len(rows) and rows[k][0] < ts0 + latency_s * 1000:
        k += 1
    if k >= len(rows):
        return None
    t1, tau1, _sp, _lup, _ldn, up_ask, dn_ask = rows[k]
    ask = up_ask if side == "Up" else dn_ask
    if not ask or not 0 < ask < 1:
        return None
    if exec_mode == "market":
        return enter(rnd["slug"], rnd, t1, tau1, side, ask, ev,
                     f"{why} [market]", mz, fair=fair_side)
    limit = take_limit_px(fair_side,
                          settings.limit_take_margin if margin is None else margin,
                          ask, settings.limit_ask_tick, take_px)
    if limit >= ask - 1e-9:
        return enter(rnd["slug"], rnd, t1, tau1, side, ask, ev,
                     f"{why} [limit {limit:.2f} crosses]", mz, fair=fair_side)
    for later in rows[k + 1:]:
        t2, tau2, _s2, _u2, _d2, lua, lda = later
        if t2 - t1 > settings.limit_wait_s * 1000:
            break
        if tau2 is not None and tau2 <= 0:
            break
        ref = lua if side == "Up" else lda
        if ref and ref <= limit:
            return enter(rnd["slug"], rnd, t2, tau2, side, limit, ev,
                         f"{why} [limit {limit:.2f} resting]", mz,
                         fair=fair_side)
    return None              # rested the whole window: MISSED, not traded


def arm_model(rnd: dict, sigma: float, persistence: int,
              exec_mode: str = "limit_take",
              latency_s: float = 1.0,
              margin: float | None = None,
              haircut: float | None = None,
              max_quote: float | None = None,
              min_quote: float | None = None,
              min_tau: float | None = None,
              max_tau: float | None = None,
              apply_range_gate: bool = True) -> dict | None:
    """The live model gate: real `evaluate`, real quote band, real persistence.

    ⛔ THE ROUND-RANGE GATE IS LIVE AND WAS MISSING HERE, exactly as it was
    missing from `tools/arms_recorded.py` before that tool was fixed.
    `Bot._record` refuses unless the round has travelled
    `model_min_round_range_bps`..`model_max_round_range_bps` since its open
    (5-16 bps as shipped), and it is the DOMINANT refusal reason in the live
    log -- so every EV, win rate and trade count this function produced was
    scored on a population the bot will not trade. Measured 2026-09-20 on the
    weekend cohort, the gate MATTERS and helps: at sigma x0.90 the weekend arm
    goes -0.0027/$1 (day-t +0.39) ungated to +0.0296 (day-t +0.98) gated, and
    at x1.00 from -0.0311 to +0.0452.

    Semantics copied from `Bot._round_range_bps`: the RUNNING range of the spot
    from the round's open to THIS tick, in bps of the strike, so it grows
    through the round. Fewer than two samples means "no opinion" and must NOT
    refuse, or a cold start silently disables the arm.

    `apply_range_gate=False` restores the old ungated behaviour for callers
    that deliberately want the unfiltered population.
    """
    streak_side, streak = None, 0
    lo = hi = None
    rlo = getattr(settings, "model_min_round_range_bps", 0.0) or 0.0
    rhi = getattr(settings, "model_max_round_range_bps", 0.0) or 0.0
    strike = rnd.get("strike") or 0.0
    for i, (ts_ms, tau, spot, up_px, dn_px, up_ask, dn_ask) in enumerate(rnd["rows"]):
        # The range accumulates over EVERY tick since the open, including ones
        # the tau window rejects -- the live buffer is appended once per poll
        # and is not filtered by the decision gates.
        if spot is not None and spot > 0:
            lo = spot if lo is None else min(lo, spot)
            hi = spot if hi is None else max(hi, spot)
        if (spot is None or not 0 < tau <= 300
                or up_ask is None or dn_ask is None):
            streak_side, streak = None, 0
            continue
        if not ((settings.min_tau_s if min_tau is None else min_tau) <= tau
                <= (settings.max_tau_s if max_tau is None else max_tau)):
            continue
        if apply_range_gate and rhi > 0 and strike > 0 and lo is not None and hi > lo:
            if not (rlo <= 1e4 * (hi - lo) / strike <= rhi):
                streak_side, streak = None, 0
                continue
        fair_up = fair_value(spot, rnd["strike"], tau, sigma)
        sig = evaluate(
            fair_up, up_ask, dn_ask, fee=None, half_spread=0.0,
            safety_margin=settings.safety_margin, fee_model=FeeModel.NOTIONAL,
            max_quote=(settings.max_quote if max_quote is None else max_quote),
            min_quote=(settings.min_quote if min_quote is None else min_quote),
            min_edge=settings.min_edge,
            edge_haircut=(settings.edge_haircut if haircut is None else haircut))
        if not sig.act or sig.side is None:
            streak_side, streak = None, 0
            continue
        # Mirror the live streak: the SAME side must act on consecutive polls.
        if sig.side == streak_side:
            streak += 1
        else:
            streak_side, streak = sig.side, 1
        if streak < persistence:
            continue
        fair_side = fair_up if sig.side == "Up" else 1.0 - fair_up
        return _execute(rnd, i, sig.side, fair_side, sig.ev_per_dollar or 0.0,
                        f"model edge {sig.edge:+.3f}", margin=margin,
                        mz=moneyness_sd(spot, rnd["strike"], tau, sigma),
                        exec_mode=exec_mode, latency_s=latency_s)
    return None


def arm_impulse(rnd: dict, sigma: float, exec_mode: str = "limit_take",
                latency_s: float = 0.0) -> dict | None:
    """The live impulse gate: an underlying move over `impulse_window_s`.

    Latency 0: this arm is armed on the price feed's own clock (~33 updates/s),
    so unlike the poll path it does not wait a poll. Its order still passes
    `take_px` = the price it read as a FLOOR, mirroring `_place_order`.
    """
    window_s = settings.impulse_window_s
    eps = int(window_s * 1000) + 700
    rows = rnd["rows"]
    for i, (ts_ms, tau, spot, up_px, dn_px, up_ask, dn_ask) in enumerate(rows):
        if spot is None or tau is None:
            continue
        if not (settings.min_tau_s <= tau <= settings.max_tau_s):
            continue
        j, prev = i - 1, None
        while j >= 0 and ts_ms - rows[j][0] <= eps:
            prev = rows[j]
            j -= 1
        if prev is None or not prev[2]:
            continue
        move = 1e4 * (spot - prev[2]) / prev[2]
        if abs(move) < settings.impulse_bps:
            continue
        side = "Up" if move > 0 else "Down"
        px = up_px if side == "Up" else dn_px
        ask = up_ask if side == "Up" else dn_ask
        if not (px and ask and settings.impulse_min_price <= px
                <= settings.impulse_max_price):
            continue
        fair_model = (fair_value(spot, rnd["strike"], tau, sigma)
                      if sigma > 0 else px)
        return _execute(rnd, i, side, fair_model, 0.0,
                        f"impulse {move:+.1f}bps/{window_s:.0f}s", take_px=px,
                        exec_mode=exec_mode, latency_s=latency_s)
    return None


def arm_ticket(rnd: dict, sigma: float) -> dict | None:
    """The live cheap-side ticket: band, staleness and moneyness conditioners."""
    for ts_ms, tau, spot, up_px, dn_px, up_ask, dn_ask in rnd["rows"]:
        if (spot is None or spot <= 0
                or not (settings.ticket_min_tau_s <= tau
                        <= settings.ticket_max_tau_s)):
            continue
        if not (up_ask and dn_ask and 0 < up_ask < 1 and 0 < dn_ask < 1):
            continue
        cheap_up = up_ask < dn_ask
        ask = min(up_ask, dn_ask)
        if not (settings.ticket_min_price <= ask <= settings.ticket_max_price):
            continue
        dist = 1e4 * abs(spot - rnd["strike"]) / rnd["strike"]
        if not (settings.ticket_min_dist_bps <= dist
                <= settings.ticket_max_dist_bps):
            continue
        disp = up_px if cheap_up else dn_px
        div = (ask - disp) if disp and 0 < disp < 1 else None
        if div is None or div < settings.ticket_min_div:
            continue
        z = moneyness_sd(spot, rnd["strike"], tau, sigma)
        if z is None or z < settings.ticket_min_z:
            continue
        return enter(rnd["slug"], rnd, ts_ms, tau,
                     "Up" if cheap_up else "Down", ask, 0.0,
                     f"ticket div {div:.3f} z {z:.2f}")
    return None


def find_add(rnd: dict, entry: dict, rise: float, min_quote: float | None,
             max_quote: float | None, after_s: float = 0.0,
             sigma: float = 0.0) -> dict | None:
    """The double-down: a SECOND unit, bought once our side is already up `rise`.

    Scoring the added unit ALONE is the whole point, because the intuition
    ("it is working, buy more") ignores three things that all move the wrong
    way at the moment of the add:

      * the add is bought at a WORSE price -- that is what `rise` means;
      * so its payoff ratio is worse: at 0.50 a win pays +100%, at 0.70 +43%;
      * and the reason the price moved is that the market AGREED with us, so
        the edge still on the table is smaller, not larger.

    The realised outcome can therefore be positive while the add is negative,
    which is why it gets its own battery rather than being buried in an average.
    """
    side = entry["side"]
    want = entry["px"] + rise
    # `after_s` is the MINIMUM AGE of the position before an add may fire. With
    # 0 the trigger is purely a price gap, so an entry filled at a momentarily
    # cheap price re-quotes on the very next tick and the add fires ~1s after
    # the entry -- measured live 2026-09-12 (entry 0.519, add 0.640, 0.9s).
    # That is a re-quote, not confirmation, and it is the mechanism behind the
    # add's negative ev_at_fill.
    earliest = entry["ts_ms"] + int(after_s * 1000)
    for row in rnd["rows"]:
        ts, tau, _sp, _up, _dn, up_ask, dn_ask = row
        if ts <= earliest:
            continue
        if tau is None or tau <= 0:
            break
        if not (settings.min_tau_s <= tau <= settings.max_tau_s):
            continue
        ask = up_ask if side == "Up" else dn_ask
        if ask is None or not 0 < ask < 1:
            continue
        if not ((settings.min_quote if min_quote is None else min_quote) <= ask
                <= (settings.max_quote if max_quote is None else max_quote)):
            continue
        if ask >= want:
            cost = _cost_per_share(ask)
            won = side == rnd["winner"]
            return {"slug": rnd["slug"], "ts_ms": ts, "tau": tau, "side": side,
                    "px": ask, "cost": cost, "won": won, "ev": 0.0,
                    "day": rnd["day"],
                    # The add gets the tilt too, so it must carry its OWN
                    # moneyness -- passing 0 sized every add at the $1 floor
                    # and understated the doubling's dollars.
                    "mz": (moneyness_sd(_sp, rnd["strike"], tau, sigma)
                           if sigma > 0 and _sp else 0.0),
                    "ret": ((1.0 if won else 0.0) - cost) / cost,
                    "why": f"add after +{rise:.2f}"}
    return None


def tstat(xs: list[float]) -> float:
    if len(xs) < 3:
        return float("nan")
    m, sd = st.fmean(xs), st.stdev(xs)
    return m / (sd / math.sqrt(len(xs))) if sd > 0 else float("nan")


def battery(name: str, entries: list[dict], rounds: int, days: int) -> None:
    if len(entries) < 5:
        print(f"  {name:<10} n={len(entries):<4} too few to read")
        return
    rets = [e["ret"] for e in entries]
    # The user's question, in one line: how many wins does one loss cost, and
    # what win rate does the price we paid actually require? Break-even WR is
    # the cost per share, because a winner pays exactly 1.00.
    wins = [e["ret"] for e in entries if e["won"]]
    losses = [e["ret"] for e in entries if not e["won"]]
    cost = st.fmean([e["cost"] for e in entries])
    if wins and losses and st.fmean(wins) > 0:
        ratio = abs(st.fmean(losses)) / st.fmean(wins)
        payoff = (f"win {st.fmean(wins):+.3f} / lose {st.fmean(losses):+.3f} "
                  f"= 1 loss costs {ratio:.1f} wins, needs WR "
                  f"{100*cost:.1f}%")
    else:
        payoff = "no wins or no losses"
    by: dict[int, list[float]] = {}
    for e in entries:
        by.setdefault(e["day"], []).append(e["ret"])
    day_means = [st.fmean(v) for v in by.values()]
    day_t = tstat(day_means)
    order = sorted(entries, key=lambda e: e["day"])
    half = len(order) // 2
    h1 = st.fmean([e["ret"] for e in order[:half]])
    h2 = st.fmean([e["ret"] for e in order[half:]])
    best = max(entries, key=lambda e: e["ret"])
    drop1 = st.fmean([e["ret"] for e in entries if e is not best])
    wr = 100 * sum(1 for e in entries if e["won"]) / len(entries)
    print(f"  {name:<10} n={len(entries):<4} {len(entries)/max(days,1):5.1f}/day  "
          f"cover {100*len(entries)/rounds:4.0f}%  WR {wr:5.1f}%  "
          f"EV {st.fmean(rets):+.4f}/$1  t {tstat(rets):+5.2f}  "
          f"day-t {day_t:+5.2f}  halves {h1:+.3f}/{h2:+.3f}  drop1 {drop1:+.3f}")
    print(f"  {'':<10} {payoff}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--no-persistence", action="store_true",
                    help="model arm enters on the FIRST acting tick instead of "
                         "after settings.signal_persistence polls")
    ap.add_argument("--exec", choices=["limit_take", "market"],
                    default="limit_take",
                    help="limit_take = the live order type (may not fill); "
                         "market = fill every decision at the ask (the old, "
                         "optimistic replay)")
    ap.add_argument("--latency-s", type=float, default=1.0,
                    help="how long after the decision the order is priced, "
                         "i.e. the poll cadence")
    ap.add_argument("--margin", type=float, default=None,
                    help="limit_take_margin to price the crossing threshold "
                         "with. A margin at or below min_edge makes every "
                         "acting decision cross at the ask (live: "
                         "settings.limit_take_margin)")
    ap.add_argument("--haircut", type=float, default=None,
                    help="edge_haircut: how much of the model's disagreement "
                         "with the venue to believe. p_used = q + h*(p-q). "
                         "Live is 1.0 -- believe all of it")
    ap.add_argument("--max-quote", type=float, default=None,
                    help="highest price the model arm may pay (live: "
                         "settings.max_quote)")
    ap.add_argument("--min-quote", type=float, default=None,
                    help="lowest price the model arm may pay (live: "
                         "settings.min_quote). Lower sells a better payoff "
                         "ratio: EV/$1 = edge/px, so the same edge is worth "
                         "1/px times more")
    ap.add_argument("--half", choices=["all", "first", "second"], default="all",
                    help="score only one half of the recording. A band choice "
                         "that only wins on the pooled sample is a fitted "
                         "choice; one that wins in BOTH halves is evidence")
    ap.add_argument("--max-tau", type=float, default=None,
                    help="latest (largest tau) we may enter. 300 = the whole "
                         "round; a 300s round with max_tau 210 SKIPS its first "
                         "90 seconds (live: settings.max_tau_s)")
    ap.add_argument("--min-tau", type=float, default=None,
                    help="earliest (smallest tau) we may enter (live: "
                         "settings.min_tau_s)")
    ap.add_argument("--double-at", type=float, default=None,
                    help="DOUBLE DOWN: buy a second unit once our side is up "
                         "this much on price (e.g. 0.10). Reported separately, "
                         "because an add is bought at a worse price with less "
                         "edge left and can lose while the round wins")
    ap.add_argument("--double-stake", type=float, default=1.0,
                    help="size of the added unit relative to the first")
    ap.add_argument("--double-after-s", type=float, default=0.0,
                    help="minimum AGE of the position before an add may fire. "
                         "0 = a pure price gap, which fires ~1s after the entry "
                         "when the entry happened to fill cheap")
    ap.add_argument("--double-max-per-day", type=int, default=None,
                    help="the LIVE daily cap on adds. Without it the backtest "
                         "counts every qualifying add (~86/day) and so reports a "
                         "lift the shipped config cannot reach: live fires at "
                         "most settings.double_max_per_day. Pass 0 to disable.")
    ap.add_argument("--bankroll", type=float, default=50.0,
                    help="bankroll the LIVE sizing rule is applied to; dollars "
                         "are computed per trade from it, not from an average "
                         "stake, because the tilt weights trades unequally")
    ap.add_argument("--tilt", choices=["inv_z", "none"], default=None,
                    help="override settings.stake_tilt. Measured: the stake-"
                         "weighted return comes out BELOW the unweighted one, "
                         "so the tilt concentrates money on the worse trades -- "
                         "the opposite of what it was built for. Sweep it before "
                         "believing either.")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rs = load(con)
    if args.half != "all" and len(rs) > 20:
        mid = len(rs) // 2
        rs = rs[:mid] if args.half == "first" else rs[mid:]
    span_h = (rs[-1]["start_ms"] - rs[0]["start_ms"]) / 3.6e6 if len(rs) > 1 else 1
    # Rates come from the SPAN, not from the number of calendar days touched:
    # a recording that covers 100h touches 5 day buckets but is 4.2 days long,
    # and using the bucket count understates every per-day figure by ~20%.
    days = span_h / 24.0 if span_h > 0 else 1.0
    rph = len(rs) / span_h if span_h > 0 else float("nan")
    print(f"settled rounds with a tape: {len(rs):,}   span {span_h:.1f}h "
          f"({days:.1f} days)   {len(rs)/days:.0f} rounds/day")
    print(f"CEILING: {rph:.1f} rounds/hour, and one position per round -> at")
    print(f"most {rph:.1f} trades/hour for ALL arms combined. Frequency is")
    print("capped by the round cadence, not by how many strategies run.")
    print()
    print(f"live gate: entry_mode={settings.entry_mode} pricing={settings.pricing_model}"
          f" min_edge={settings.min_edge} quotes {settings.min_quote}-"
          f"{settings.max_quote} tau {settings.min_tau_s:.0f}-{settings.max_tau_s:.0f}"
          f" impulse {settings.impulse_window_s:.0f}s>={settings.impulse_bps:.0f}bps"
          f" ticket {settings.ticket_min_price}-{settings.ticket_max_price}"
          f" tau {settings.ticket_min_tau_s:.0f}-{settings.ticket_max_tau_s:.0f}s"
          f" div>={settings.ticket_min_div} z>={settings.ticket_min_z}")
    print(f"execution: order_type={settings.order_type} mode={args.exec} "
          f"latency={args.latency_s:.1f}s margin={settings.limit_take_margin} "
          f"tick={settings.limit_ask_tick} wait={settings.limit_wait_s:.0f}s")
    print()

    persistence = 1 if args.no_persistence else settings.signal_persistence
    picks: dict[str, list[dict]] = {a: [] for a in ARMS}
    sigmas = {r["slug"]: rolling_sigma(rs, i2) for i2, r in enumerate(rs)}
    for i, rnd in enumerate(rs):
        sigma = sigmas[rnd["slug"]]
        hits = {}
        got = arm_model(rnd, sigma, persistence, args.exec, args.latency_s,
                        args.margin, args.haircut, args.max_quote,
                        args.min_quote, args.min_tau, args.max_tau)
        if got:
            hits["model"] = got
        got = arm_impulse(rnd, sigma, args.exec, 0.0)
        if got:
            hits["impulse"] = got
        got = arm_ticket(rnd, sigma)
        if got:
            hits["ticket"] = got
        for a, e in hits.items():
            picks[a].append(e)
        # The union: ONE position per round, awarded to whichever arm fires
        # first in time -- what a single account actually ends up holding.
        if hits:
            first = min(hits.items(), key=lambda kv: kv[1]["ts_ms"])
            picks.setdefault("union", []).append(
                dict(first[1], why=f"[{first[0]}] {first[1]['why']}"))
        # A second union that spends the round on the best-EV arm instead of
        # the first: an allocation choice, not an execution one.
        if hits:
            best = max(hits.items(), key=lambda kv: kv[1]["ev"])
            picks.setdefault("union_best", []).append(
                dict(best[1], why=f"[{best[0]}] {best[1]['why']}"))

    print("each arm ALONE (one entry per round, filled at the ask, own fee curve):")
    for a in ARMS:
        battery(a, picks[a], len(rs), days)
    if args.double_at:
        print()
        print(f"DOUBLE DOWN: add {args.double_stake:.2f}x once our side is up "
              f"{args.double_at:.2f} in price")
        bysl = {r["slug"]: r for r in rs}
        adds: list[dict] = []
        combined: list[dict] = []
        cap = (settings.double_max_per_day
               if args.double_max_per_day is None else args.double_max_per_day)
        per_day: dict[int, int] = {}
        for e in picks["model"]:
            if cap and per_day.get(e["day"], 0) >= cap:
                combined.append(dict(e))          # the day's add budget is spent
                continue
            a = find_add(bysl[e["slug"]], e, args.double_at, args.min_quote,
                         args.max_quote, args.double_after_s,
                         sigmas.get(e["slug"], 0.0))
            if a is None:
                combined.append(dict(e))
                continue
            per_day[e["day"]] = per_day.get(e["day"], 0) + 1
            adds.append(a)
            w1, w2 = 1.0, args.double_stake
            combined.append(dict(e, ret=(e["ret"] * w1 + a["ret"] * w2)
                                      / (w1 + w2)))
        battery("add-only", adds, len(rs), days)
        battery("combined", combined, len(rs), days)
        n = max(len(picks["model"]), 1)
        print(f"  {len(adds)} of {len(picks['model'])} entries got an add "
              f"({100*len(adds)/n:.0f}%)")
        # The question "is it MORE PROFITABLE" is a TOTAL question, and EV/$1
        # answers a different one: it divides by the extra capital and so
        # penalises size. Both are reported, because they disagree here --
        # total profit rises while return per unit of capital falls -- and
        # which one matters depends on whether the capital has another use.
        # On this arm it largely does not: it already takes 62% of the rounds
        # it wants, so spare capital has nowhere else to go. The one real cost
        # is that bigger positions reach the daily loss cap sooner.
        base_tot = sum(e["ret"] for e in picks["model"])
        add_tot = sum(a["ret"] * args.double_stake for a in adds)
        cap_units = len(picks["model"]) + len(adds) * args.double_stake
        print(f"  PROFIT base    {base_tot:+8.1f} units of stake over "
              f"{len(picks['model'])} rounds  ({base_tot/days:+.2f}/day)")
        print(f"  PROFIT doubled {base_tot + add_tot:+8.1f} units over "
              f"{cap_units:.0f} units of capital  "
              f"({100*add_tot/abs(base_tot):+.0f}% more profit than base)")
        print(f"         the adds contributed {add_tot:+.1f} "
              f"({add_tot/max(len(adds),1):+.4f} each, {add_tot/days:+.2f}/day)")
        print(f"  RETURN PER UNIT OF CAPITAL: base {base_tot/len(picks['model']):+.4f}"
              f" -> doubled {(base_tot + add_tot)/cap_units:+.4f}")

    # --- DOLLARS, at the live sizing rule (per trade, not an average) -------
    if picks["model"]:
        n = len(picks["model"])
        dollar = sum(e["ret"] * stake_for(e.get("mz", 0.0), args.bankroll,
                                          args.tilt)
                     for e in picks["model"])
        staked = sum(stake_for(e.get("mz", 0.0), args.bankroll, args.tilt)
                     for e in picks["model"])
        print()
        print(f"  DOLLARS at a ${args.bankroll:.0f} bankroll, live sizing "
              f"({settings.sizing_mode} {settings.stake_fraction:.0%} + "
              f"{settings.stake_tilt}):")
        print(f"    avg stake ${staked/n:.2f}   mean per-$1 ret "
              f"{dollar/staked:+.4f} (unweighted "
              f"{st.fmean([e['ret'] for e in picks['model']]):+.4f})")
        print(f"    {dollar/days:+.2f}/day over {days:.1f} days")
        if args.double_at:
            add_d = sum(a["ret"] * stake_for(a.get("mz", 0.0), args.bankroll,
                                              args.tilt)
                        * args.double_stake for a in adds)
            print(f"    with doubling: {(dollar + add_d)/days:+.2f}/day "
                  f"({100*add_d/abs(dollar):+.0f}%, adds contribute "
                  f"{add_d/days:+.2f}/day)")
    print()
    print("all arms at once (this is the thing that was asked for):")
    battery("union", picks["union"], len(rs), days)
    battery("union_best", picks["union_best"], len(rs), days)
    print()

    # --- overlap: are these independent bets, or the same bet three times? ---
    sets = {a: {e["slug"] for e in picks[a]} for a in ARMS}
    print("overlap (rounds wanted by BOTH -- these add a position, not a trade):")
    for i, a in enumerate(ARMS):
        for b in ARMS[i + 1:]:
            both = sets[a] & sets[b]
            print(f"  {a:<8} & {b:<8} {len(both):5,}  "
                  f"({100*len(both)/max(len(sets[a] | sets[b]),1):4.0f}% of the "
                  f"{len(sets[a] | sets[b]):,} they collectively want)")
    any_arm = set().union(*sets.values()) if sets else set()
    print(f"  rounds any arm wants: {len(any_arm):,} of {len(rs):,} "
          f"({100*len(any_arm)/len(rs):.0f}%)")
    print()

    # --- the marginal arm: what does each arm ADD to the union? ---
    print("marginal contribution -- rounds ONLY this arm wants:")
    for a in ARMS:
        others = set().union(*[sets[b] for b in ARMS if b != a]) if len(ARMS) > 1 else set()
        only = sets[a] - others
        only_e = [e for e in picks[a] if e["slug"] in only]
        if not only_e:
            print(f"  {a:<8} none -- it never adds a round the others miss")
            continue
        wr = 100 * sum(1 for e in only_e if e["won"]) / len(only_e)
        print(f"  {a:<8} {len(only_e):4,} rounds ({len(only_e)/days:.1f}/day)  "
              f"WR {wr:5.1f}%  EV {st.fmean([e['ret'] for e in only_e]):+.4f}/$1  "
              f"t {tstat([e['ret'] for e in only_e]):+5.2f}")
    print()
    print("If the marginal rows are small or negative, running every arm at once")
    print("buys almost no extra trades and adds only overlapping risk -- because")
    print("the binding constraint is the 5-minute ROUND CADENCE, not the number")
    print("of strategies.")
    print()

    # --- paired: on a round two arms both want, is either entry better? ---
    # The union awards a shared round to whichever arm fires first, so this is
    # the question that decides whether combining arms adds QUALITY even when it
    # adds no trades. Paired on the same round, so the round's own difficulty
    # cancels out.
    by_slug = {a: {e["slug"]: e for e in picks[a]} for a in ARMS}
    print("paired on the same round (positive = the second arm's entry is better):")
    printed = False
    for i, a in enumerate(ARMS):
        for b in ARMS[i + 1:]:
            shared = sorted(sets[a] & sets[b], key=lambda s: by_slug[a][s]["ts_ms"])
            diffs = [by_slug[b][s]["ret"] - by_slug[a][s]["ret"] for s in shared]
            if len(diffs) < 5:
                continue
            printed = True
            earlier = sum(1 for s in shared
                          if by_slug[b][s]["ts_ms"] < by_slug[a][s]["ts_ms"])
            print(f"  {b:<8} - {a:<8} n={len(diffs):<4} mean {st.fmean(diffs):+.4f}  "
                  f"t {tstat(diffs):+5.2f}  ({b} fires first on {earlier})")
    if not printed:
        print("  (no pair shares enough rounds to test)")


if __name__ == "__main__":
    main()
