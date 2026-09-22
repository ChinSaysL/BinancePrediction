"""Sweep the MODEL arm's blocking knobs on the recorded tape.

WHY THIS EXISTS. The model arm last filled 2026-09-14. Live, its entire
positive-edge population sits at tau 240-300s with a round travel of 0.0-2.2bps
-- and the shipped gates are `max_tau_s=210` (skips the first 90s of the round)
and `model_min_round_range_bps=5.0`. Both refuse exactly that region, so nothing
is ever sent. This tool prices the region instead of arguing about it.

HOW IT REPLAYS. Exactly the live rule, not a model of it:
  * `edge.signal.evaluate` -- the real gate, the real quote band, the real fee
  * `pricing.digital.fair_value` + the arm's own out-of-sample sigma
  * the real persistence streak (SAME side on consecutive 1 Hz polls)
  * the round-travel gate mirrored from `main.Bot._round_range_bps`
    (1e4*(max-min)/strike over the round's spot samples so far)
  * fills at the recording's EXECUTABLE ask through the live `limit_take` path,
    at 1s latency -- a decision whose limit does not cross is a MISSED round,
    not a trade (that is the difference between this and the optimistic replay)
  * ONE ENTRY PER ROUND (the first qualifying tick), which is the arm's own
    `cross_max_per_round=1` shape and the only unit a per-round battery is
    valid in

WHAT IT PRINTS PER CELL: n, /day, cover, WR, mean cost paid, EV/$1, pooled t,
day-clustered t, both halves, drop-best-day AND drop-best-entry. A knob is only
evidence when the DIRECTION repeats in both halves -- the absolute value of the
best cell on a pooled sample is a fitted number, not a finding.

NOT MODELLED (so not claimed): depth, queue position, the 1 Hz poll's blind
spot, and the venue's own reprice between decision and order. The level is an
upper bound; the GAIN between two cells is what the sweep is for.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# tools/ LAST so it wins: an earlier stray copy of `arm_overlap.py` at the repo
# root (the host had one, dated 2026-09-12) shadows this one when the root is
# inserted last, and the tool then replays a DIFFERENT rule than the file
# sitting next to it. Measured 2026-09-20: a new field added to every entry came
# back None from all 459 entries while the source on disk was correct.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arm_overlap as ao                                     # noqa: E402

# The live flat stake, used by the $-columns and by BOTH reporting helpers
# (`row` and `erow`). Module scope, because `row` is defined at module scope.
STAKE = 2.0

# WHERE IN THE ROUND THE EXITS ACTUALLY HAPPEN. A `min_tau` sweep that returns
# the same numbers at 0, 15 and 30 is EITHER a floor that never binds OR a flag
# that is not wired -- and this repo has been bitten by the second one (see the
# inert `--price-mode` note in the cross-arm memory). Logging the tau of every
# exit separates the two: if the minimum logged tau is 30+, the floor below that
# genuinely never binds. `erow` clears this before each rule.
EXIT_TAU: list[float] = []

# THE RESTING-LIMIT FALLBACK'S OWN COUNTERS (`--limit-fallback`). `sold`/`miss`
# are what stop the fallback from being read as free money: a resting order that
# is posted and never reached is a MISSED EXIT, and the tail of the table is
# decided by how many of those there are. `erow` clears these per rule.
#   miss    = ATTEMPTS that never came back (one per triggering tick)
#   posted  = DISTINCT POSITIONS that ever rested an order
# The two differ because the loop re-prices at every triggering tick, so one
# position that never fills contributes many `miss`es and exactly one `posted`.
# Quote the POSITION rate (`sold / posted`); `miss` alone overstates the number
# of positions exposed to a miss.
LIMIT: dict = {"sold": 0, "miss": 0, "px": [], "posted": set(),
               "fixed": {}, "cur": ""}


def _maxdd(seq: list[float]) -> float:
    """Worst peak-to-trough of the equity curve, in dollars at STAKE.

    ⛔ A DRAWDOWN CLAIM NEEDS ITS OWN DIRECTION CHECK. The both-halves test this
    repo uses for an EV claim does not transfer: maxDD is a statistic of the
    WHOLE curve, so a rule can have a flat EV in both halves and still own all
    of its drawdown in one of them. Callers therefore report `maxDD` for the
    first and second half separately alongside the pooled figure, and a depth
    that exists in ONE half only is a single bad run, not protection.
    """
    eq = peak = wor = 0.0
    for r in seq:
        eq += STAKE * r
        peak = max(peak, eq)
        wor = min(wor, eq - peak)
    return wor

if Path(ao.__file__).resolve().parent != Path(__file__).resolve().parent:
    raise SystemExit(
        f"arm_overlap loaded from {ao.__file__}, not from this tool's own "
        f"directory {Path(__file__).resolve().parent} -- a stray copy is "
        f"shadowing it; delete the stray, do not edit around it")
from btcpredict.config import settings                        # noqa: E402
from btcpredict.edge.signal import (                          # noqa: E402
    FeeModel, evaluate, fee_rate_for_price, stake_for_edge)
from btcpredict.pricing.digital import fair_value, moneyness_sd  # noqa: E402

# The live gate values, read from config rather than typed here. A sweep that
# pins its own copy ranks a system nobody runs: this file carried max_tau 210 /
# min_quote 0.45 / range 5-12 while the host ran 180 / 0.55 / 5-16 (measured
# 2026-09-20), and nothing in the output said so.
LIVE = dict(max_tau=settings.max_tau_s, min_tau=settings.min_tau_s,
            min_quote=settings.min_quote, max_quote=settings.max_quote,
            min_range=settings.model_min_round_range_bps,
            max_range=settings.model_max_round_range_bps)


def load_live_sigma(con) -> dict[str, list[tuple[int, float]]]:
    """The sigma the BOT used, per round: (ts_ms, sigma_1m) from `decisions`.

    The replay's own `rolling_sigma` is a different estimator AND a different
    level: measured on 1,790 Binance rounds the live/rolling ratio runs 0.37 to
    6.0 (median 0.84 pooled, 0.71 after the duplicate-bar fix). Any claim whose
    sign depends on the sigma level -- the `fair` split, and the 2x sizing rule
    that is monotone in it, crossing zero at ~1.24 -- has to be scored with the
    sigma the arm actually had, or it is ranking a system nobody runs.

    Forward-filled because `refresh_vol_if_stale` only updates the estimate once
    a minute, so the last recorded value is the value in force at every tick
    between decisions.
    """
    out: dict[str, list[tuple[int, float]]] = {}
    for slug, ts, s in con.execute(
            "SELECT slug, ts_ms, sigma_1m FROM decisions "
            "WHERE sigma_1m IS NOT NULL ORDER BY slug, ts_ms"):
        out.setdefault(slug, []).append((ts, s))
    return out


def _sig_at(series: list[tuple[int, float]] | None, ts: int,
            default: float) -> float:
    """The live sigma in force at `ts` (last value at or before it)."""
    if not series:
        return default
    if ts < series[0][0]:
        return series[0][1]
    lo, hi = 0, len(series) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if series[mid][0] <= ts:
            lo = mid
        else:
            hi = mid - 1
    return series[lo][1]


def crossings(seen: list[float], strike: float, band: float = 0.0) -> int:
    """How many times the spot has crossed the STRIKE this round.

    A count, not a distance: the strike is the level the payout is written
    against, so a round that has already been through it N times is a round
    where the price is BOUNCING off the strike rather than leaving it. That is
    the reaction-zone claim, stated as something the tape can answer.

    ⛔ A RAW SIGN COUNT IS NOT A PROPERTY OF THE ROUND, IT IS A PROPERTY OF THE
    SAMPLER. This replay reads the 1 Hz `quotes` tape; the live bot appends
    `self._ticks` once per poll at a median 58 ms -- ~17x denser. Counting
    `spot > strike` changes on a 58 ms grid around a level the price is sitting
    on counts jitter, not bounces, so the same round would score 4 crossings in
    the replay and 60 live and the gate would refuse everything. `band` is
    HYSTERESIS in price terms: the state only flips once the spot is clear of
    the strike by `band`, which makes the count a property of the path instead
    of the sampling rate. Callers pass `k * sigma_1m * strike`, i.e. a band of
    `k` minutes of typical movement.
    """
    cr, state = 0, 0
    hi, lo = strike + band, strike - band
    for s in seen:
        new = 3 if s >= hi else (1 if s <= lo else state)
        if new != state:
            if state:
                cr += 1
            state = new
    return cr


def replay(rnd: dict, sigma: float, *, persistence: int,
           max_tau: float, min_tau: float | None,
           min_quote: float, max_quote: float,
           min_range: float, max_range: float,
           exec_mode: str = "limit_take", latency_s: float = 1.0,
           margin: float | None = None,
           haircut: float | None = None,
           zone_min_margin: float = 0.0, zone_max_cross: int = 0,
           zone_band_k: float = 0.5,
           entry_rev_bps: float = 0.0, entry_look_s: float = 20.0,
           live_sig: list[tuple[int, float]] | None = None) -> dict | None:
    """The first round-qualifying entry under these knobs, or None.

    With `live_sig` the fair value at each tick uses the sigma the BOT recorded
    at that moment, so the replay reproduces the live rule rather than a
    parallel one; without it, the round's scalar `sigma`.
    """
    lo_tau = settings.min_tau_s if min_tau is None else min_tau
    hc = settings.edge_haircut if haircut is None else haircut
    strike = rnd["strike"]
    streak_side, streak = None, 0
    seen: list[float] = []
    seen_ts: list[tuple[int, float]] = []
    for i, (ts_ms, tau, spot, _up_px, _dn_px, up_ask, dn_ask) in enumerate(rnd["rows"]):
        if spot is not None and spot > 0:
            seen.append(spot)
            seen_ts.append((ts_ms, spot))
        if (spot is None or not 0 < tau <= 300
                or up_ask is None or dn_ask is None):
            streak_side, streak = None, 0
            continue
        if not (lo_tau <= tau <= max_tau):
            continue
        sg_i = _sig_at(live_sig, ts_ms, sigma)
        # main.Bot._round_range_bps: the round's TRAVEL so far, and None (no
        # opinion) until there are two samples -- never a refusal.
        if max_range > 0 and len(seen) >= 2:
            rng = 1e4 * (max(seen) - min(seen)) / strike
            if not (min_range <= rng <= max_range):
                continue
        fair_up = fair_value(spot, strike, tau, sg_i)
        sig = evaluate(
            fair_up, up_ask, dn_ask, fee=None, half_spread=0.0,
            safety_margin=settings.safety_margin, fee_model=FeeModel.NOTIONAL,
            max_quote=max_quote, min_quote=min_quote,
            min_edge=settings.min_edge, edge_haircut=hc)
        if not sig.act or sig.side is None:
            streak_side, streak = None, 0
            continue
        if sig.side == streak_side:
            streak += 1
        else:
            streak_side, streak = sig.side, 1
        # ===================== REACTION ZONE =====================
        # THE STRIKE IS THE REACTION LEVEL. The payout is written against it,
        # so when the spot is only barely on our side of it the round is still
        # undecided and the price can bounce back through -- which is the loss
        # this gate exists to avoid. `z_fav` is how far OUR side is ahead of
        # the strike, in sigma of the move still left:
        #     z_fav = +ln(S/K)/(sigma*sqrt(tau/60))  for Up
        #     z_fav = -ln(S/K)/(sigma*sqrt(tau/60))  for Down
        # POSITION MATTERS. This sits AFTER the streak update, exactly where
        # the live gate chain sits, because in the bot a refused tick still
        # extends the persistence streak. Filtering the entries post hoc
        # instead would let the gate break the streak and would score a
        # STRICTER rule than the one that would ship.
        if zone_min_margin > 0 or zone_max_cross > 0:
            z_fav = moneyness_sd(spot, strike, tau, sg_i)
            if sig.side == "Down":
                z_fav = -z_fav
            if (zone_min_margin > 0
                    and (not math.isfinite(z_fav)
                         or z_fav < zone_min_margin)):
                continue
            if zone_max_cross > 0 and len(seen) >= 3:
                # see `crossings`: the band is what makes this a property of the
                # path rather than of the sampler.
                if crossings(seen, strike,
                             zone_band_k * sg_i * strike) >= zone_max_cross:
                    continue
        # ============ THE ENTRY-SIDE MIRROR OF THE EXIT RULE ============
        # ⭐ THIS COMES FROM THE EXIT'S OWN DATA, not from a hunch. The tau
        # diagnostic in `--mode exit` shows 21 of 46 reaction exits fire at tau
        # 150-210 -- i.e. within ~60s of entry, against a `max_tau` ceiling of
        # 210 and a median exit tau of 149. So the exit is mostly catching "I
        # just entered and it immediately went against me". If that cohort is
        # bad, the cheaper place to fix it is BEFORE the fill: refuse a tick
        # where the spot has ALREADY moved against the proposed side. Same
        # shape as `model_exit_rev_bps`, pointed at the entry, and it sits in
        # the gate chain so a refused tick still extends the streak (the live
        # chain's behaviour -- see the note above).
        if entry_rev_bps > 0:
            sgn_e = 1.0 if sig.side == "Up" else -1.0
            past = [s for t, s in seen_ts if t <= ts_ms - entry_look_s * 1000]
            if past:
                mv = math.log(spot / past[-1]) * 1e4 * sgn_e
                if mv <= -entry_rev_bps:
                    continue
        # ================================================================
        if streak < persistence:
            continue
        fair_side = fair_up if sig.side == "Up" else 1.0 - fair_up
        return ao._execute(rnd, i, sig.side, fair_side,
                           sig.ev_per_dollar or 0.0,
                           f"model edge {sig.edge:+.3f}", margin=margin,
                           mz=moneyness_sd(spot, strike, tau, sg_i),
                           exec_mode=exec_mode, latency_s=latency_s)
    return None


def drop_best_day(entries: list[dict]) -> float:
    by: dict[int, list[float]] = {}
    for e in entries:
        by.setdefault(e["day"], []).append(e["ret"])
    if len(by) < 2:
        return float("nan")
    worst = max(by, key=lambda d: st.fmean(by[d]))
    rest = [r for d, v in by.items() if d != worst for r in v]
    return st.fmean(rest) if rest else float("nan")


def row(label: str, entries: list[dict], rounds: int, days: float) -> None:
    if not entries:
        print(f"  {label:<28} no entries")
        return
    # ⛔ THE GUARD MUST COME BEFORE THE HALVES. It used to sit below them, so
    # `half = len(order) // 2` was 0 for a single entry and `order[:0]` made
    # `statistics.fmean` raise StatisticsError -- the tool CRASHED instead of
    # reporting "1 entry", which is exactly the case a dead family produces
    # (btc-updown-15m: 1 entry in 8.64 days, measured 2026-09-21).
    if len(entries) < 5:
        print(f"  {label:<28} n={len(entries):<4} too few to read")
        return
    rets = [e["ret"] for e in entries]
    by: dict[int, list[float]] = {}
    for e in entries:
        by.setdefault(e["day"], []).append(e["ret"])
    day_means = [st.fmean(v) for v in by.values()]
    order = sorted(entries, key=lambda e: e["day"])
    half = len(order) // 2
    h1 = st.fmean([e["ret"] for e in order[:half]])
    h2 = st.fmean([e["ret"] for e in order[half:]])
    best = max(entries, key=lambda e: e["ret"])
    drop1 = st.fmean([e["ret"] for e in entries if e is not best])
    wr = 100 * sum(1 for e in entries if e["won"]) / len(entries)
    cost = st.fmean([e["cost"] for e in entries])
    print(f"  {label:<28} n={len(entries):<5} {len(entries)/days:5.1f}/d  "
          f"cov {100*len(entries)/rounds:4.0f}%  WR {wr:5.1f}%  "
          f"paid {cost:.3f}  EV {st.fmean(rets):+.4f}/$1  "
          f"t {ao.tstat(rets):+5.2f}  day-t {ao.tstat(day_means):+5.2f}  "
          f"H {h1:+.3f}/{h2:+.3f}  dDay {drop_best_day(entries):+.4f}  "
          f"d1 {drop1:+.4f}")
    # ⭐ DRAWDOWN, the same measure `erow` prints. A gate that raises EV but
    # makes the drawdown deeper is a worse trade even when the total is bigger.
    # `entries` is in round-start order (the replay walks `rs` by start_ms), so
    # this is the curve an account would actually have lived through. NB a gate
    # changes the CURVE, not just the sum: dropping entries moves every later
    # point, which is exactly why the total alone is blind to it.
    out = [e["ret"] for e in entries]
    mdd = _maxdd(out)
    mid = len(out) // 2
    mdd1, mdd2 = _maxdd(out[:mid]), _maxdd(out[mid:])
    eq, peak, streak, worst_streak = 0.0, 0.0, 0, 0
    for r_ in out:
        eq += STAKE * r_
        peak = max(peak, eq)
        streak = streak + 1 if r_ < 0 else 0
        worst_streak = max(worst_streak, streak)
    tail5 = st.fmean(sorted(out)[:5]) if len(out) >= 5 else float("nan")
    ratio = (STAKE * sum(out)) / abs(mdd) if mdd < 0 else float("inf")
    print(f"  {label:<28} maxDD ${mdd:+6.2f}  tot/|maxDD| {ratio:5.2f}  "
          f"worst streak {worst_streak:<3} mean-worst-5 {tail5:+.4f}  "
          f"({STAKE*5*tail5:+.2f} on the 5 worst)  | halfDD {mdd1:+.2f}/{mdd2:+.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--exec", choices=["limit_take", "market"],
                    default="limit_take")
    ap.add_argument("--latency-s", type=float, default=1.0)
    ap.add_argument("--persistence", type=int, default=None,
                    help="default: settings.signal_persistence (live)")
    ap.add_argument("--mode", default="all",
                    choices=["all", "ofat", "joint", "live", "halves",
                             "fair", "size", "sure", "price", "zone",
                             "exit", "struct", "predict", "react",
                             "stakeband"],
                    help="which grid to run")
    ap.add_argument("--weight-low-fair", type=float, default=2.0,
                    help="stake MULTIPLIER for entries with fair <= the cut "
                         "(the shipped `model_low_fair_stake_mult`). Scored at "
                         "EQUAL CAPITAL, because the claim is that the same "
                         "money spent on the cheap bucket earns more -- not "
                         "that staking more money earns more.")
    ap.add_argument("--fair-cut", type=float, default=None,
                    help="default: settings.model_low_fair_cut (the live cut)")
    ap.add_argument("--since-ms", type=int, default=0,
                    help="only rounds starting at/after this epoch ms -- for "
                         "reconciling the replay against a live run")
    ap.add_argument("--family", default=None,
                    help="score ONLY this slug prefix, e.g. btc-updown-5m. "
                         "The live bot trades `model_families`; without this "
                         "the tool scores every family in the DB, so a table "
                         "can be dominated by rounds the bot never trades.")
    ap.add_argument("--exclude-pm", action="store_true",
                    help="drop Polymarket-era rounds (a different settlement "
                         "rule, and the arm was switched off then) so a "
                         "halves test spans ONE rule")
    ap.add_argument("--only-pm", action="store_true",
                    help="KEEP ONLY the `chainlink-twap%%` rounds -- the "
                         "04-16..04-19 block that `--exclude-pm` throws away. "
                         "This is the third independent sample: same venue, same "
                         "bot, DIFFERENT SETTLEMENT RULE (a 60s TWAP against the "
                         "range start rather than a point comparison). A knob "
                         "whose direction repeats here as well as on the two "
                         "CHAINLINK blocks is not a property of one tape.")
    ap.add_argument("--dump", type=int, default=0,
                    help="print the first N entries (slug, tau, side, px)")
    ap.add_argument("--use-live-sigma", action="store_true",
                    help="price EVERY tick with the sigma the bot RECORDED "
                         "(decisions.sigma_1m, forward-filled) instead of the "
                         "replay's own rolling_sigma. The two disagree by a "
                         "factor that runs 0.37-6.0, and a claim whose sign "
                         "depends on the sigma level must be scored on the "
                         "sigma the arm actually had.")
    ap.add_argument("--sigma-scale", type=float, default=1.0,
                    help="multiply the out-of-sample sigma by this factor. The "
                         "live estimator applies an hour-of-day seasonality "
                         "multiplier, so its sigma runs above the replay's; "
                         "pricing the edge at 1.0 alone overstates it.")
    ap.add_argument("--look-s", type=float, default=20.0,
                    help="window for the REACTION move (--mode react)")
    ap.add_argument("--rev-bps", type=float, default=3.0,
                    help="how far AGAINST us the spot must have moved over "
                         "--look-s to count as a reaction (--mode react)")
    ap.add_argument("--zone-band-k", type=float, default=0.5,
                    help="crossing DEADBAND, in units of one minute of sigma "
                         "(see `crossings`). 0 counts raw sign flips, which is "
                         "a property of the sampler -- 1 Hz here against ~58 ms "
                         "live -- and must not be shipped.")
    ap.add_argument("--whole-floor", action="store_true",
                    help="reproduce the FIRST (wrong) sell-floor check, which "
                         "compared the whole POSITION's notional to the venue "
                         "minimum instead of the SLICE actually sent. It makes "
                         "a partial exit look viable at frac 0.25, which live "
                         "it never is -- see the comment at `_slice_ok`.")
    ap.add_argument("--limit-fallback", action="store_true",
                    help="model `LiveBroker.sell_limit` beside the MARKET exit: "
                         "when the venue's ~$1.50 MARKET minimum refuses the "
                         "slice, REST a LIMIT sell at the venue's tick and fill "
                         "it only when a LATER recorded bid prints one tick "
                         "THROUGH the price. Off by default so the existing "
                         "tables reproduce byte-identically.")
    ap.add_argument("--limit-single", action="store_true",
                    help="with --limit-fallback, rest ONE order per position at "
                         "the FIRST trigger's price instead of re-pricing at "
                         "every triggering tick. The re-pricing version is the "
                         "more permissive of the two, so this is the bound:"
                         "if the ranking does not survive it, it is the "
                         "re-pricing that produced it.")
    args = ap.parse_args()
    persist = (settings.signal_persistence if args.persistence is None
               else args.persistence)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    # the family goes to the LOADER: `ao.load` used to filter on
    # `symbol='BTCUSDT'`, which silently returned zero rounds for every other
    # family (see its docstring), so a per-family sweep could not exist.
    rs = ao.load(con, family=args.family)
    if args.since_ms:
        rs = [r for r in rs if r["start_ms"] >= args.since_ms]
    if args.exclude_pm:
        pm = {row[0] for row in con.execute(
            "SELECT slug FROM rounds WHERE feed_provider LIKE 'chainlink-twap%'")}
        before = len(rs)
        rs = [r for r in rs if r["slug"] not in pm]
        print(f"excluded {before - len(rs)} Polymarket-era rounds")
    if args.only_pm:
        keep = {row[0] for row in con.execute(
            "SELECT slug FROM rounds WHERE feed_provider LIKE 'chainlink-twap%'")}
        before = len(rs)
        rs = [r for r in rs if r["slug"] in keep]
        print(f"KEPT ONLY the twap block: {len(rs)} of {before} rounds")
    if args.family:
        before = len(rs)
        rs = [r for r in rs if r["slug"].rsplit("-", 1)[0] == args.family]
        print(f"family filter {args.family}: kept {len(rs)} of {before} rounds")
    print(f"loaded {len(rs)} settled rounds with a quote tape "
          f"({rs[0]['slug']} .. {rs[-1]['slug']})")
    fam: dict[str, int] = {}
    for r in rs:
        k = r["slug"].rsplit("-", 1)[0]
        fam[k] = fam.get(k, 0) + 1
    print("family mix: " + ", ".join(f"{k} {v}" for k, v in
                                     sorted(fam.items(), key=lambda x: -x[1])))
    span_h = (rs[-1]["start_ms"] - rs[0]["start_ms"]) / 3.6e6
    days = max(span_h / 24.0, 1e-9)
    # ⛔ THE SAMPLE CENSUS, PRINTED BEFORE ANY TABLE. `--exclude-pm` drops only
    # `feed_provider LIKE 'chainlink-twap%'`; the remainder can still span more
    # than one venue era, and on 2026-09-22 a whole set of exit tables was read
    # as Binance evidence when the strict Binance window held 13 entries out of
    # 181. Any per-entry table must be read against this line.
    fed: dict[str, int] = {}
    for r in rs:
        # ⚠️ named `frow`, NOT `row`: `row` is the reporting helper defined at
        # module scope, and reusing the name here shadowed it for every mode
        # below -- `--mode zone` died with "'tuple' object is not callable".
        frow = con.execute("SELECT feed_provider FROM rounds WHERE slug=?",
                           (r["slug"],)).fetchone()
        fed[(frow[0] if frow else None) or "?"] = fed.get(
            (frow[0] if frow else None) or "?", 0) + 1
    print("  feed_provider mix: " + ", ".join(
        f"{k} {v}" for k, v in sorted(fed.items(), key=lambda x: -x[1])))
    if args.since_ms:
        print(f"  era filter --since-ms {args.since_ms} "
              f"({rs[0]['slug']} .. {rs[-1]['slug']})")
    print(f"span {span_h:.1f}h = {days:.2f} days | live: max_tau "
          f"{settings.max_tau_s:g} min_quote {settings.min_quote:g} max_quote "
          f"{settings.max_quote:g} persistence {persist} exec {args.exec} "
          f"latency {args.latency_s:g}s")
    sigmas = [ao.rolling_sigma(rs, i) * args.sigma_scale
              for i in range(len(rs))]
    live_sig = load_live_sigma(con) if args.use_live_sigma else {}
    if args.use_live_sigma:
        have = sum(1 for r in rs if r["slug"] in live_sig)
        print(f"  sigma source: LIVE per tick -- {have}/{len(rs)} rounds have "
              f"a recorded series ({100*have/len(rs):.0f}%); the rest fall "
              f"back to rolling_sigma x{args.sigma_scale:g}")
    if args.sigma_scale != 1.0 and not args.use_live_sigma:
        print(f"sigma scaled x{args.sigma_scale:g} "
              f"(mean {sum(sigmas)/len(sigmas)*1e4:.2f} bps/min)")

    base = dict(persistence=persist, exec_mode=args.exec,
                latency_s=args.latency_s)

    def run2(**over) -> list[dict]:
        kw = dict(LIVE)
        kw.update(over)
        out = []
        for r, sg in zip(rs, sigmas):
            e = replay(r, sg, **base, **kw,
                       live_sig=(live_sig.get(r["slug"])
                                 if args.use_live_sigma else None))
            if e:
                out.append(e)
        return out

    if args.mode == "sure":
        # FEWER TRADES, SURER TRADES. Every knob here trades COUNT for QUALITY,
        # so a cell must be read on BOTH axes: `/d` and EV/$1. Halving the count
        # for +50% per trade is an improvement in dollars; keeping 90% of the
        # trades at 105% of the EV is not. The direction check at the bottom is
        # what decides -- a knob whose ranking FLIPS between halves is a fitted
        # split of one sample, not a finding.
        print("\n=== SURE: knobs that trade COUNT for QUALITY ===")
        print("  -- the quote floor: paying up for a surer side --")
        for v in (0.50, 0.55, 0.60, 0.65, 0.70):
            row(f"min_quote {v:g}", run2(min_quote=v), len(rs), days)
        print("  -- the travel band (live ceiling is 35) --")
        for v in (5.0, 8.0, 12.0):
            row(f"min_range {v:g}", run2(min_range=v), len(rs), days)
        for v in (16.0, 20.0, 25.0, 35.0):
            row(f"max_range {v:g}", run2(max_range=v), len(rs), days)
        print("  -- the round window --")
        for v in (180.0, 150.0):
            row(f"max_tau {v:g}", run2(max_tau=v), len(rs), days)
        print("\n  -- JOINT candidates (fewer AND surer at once) --")
        for name, over in (
                ("mq.55 rng[5,16] tau180",
                 dict(min_quote=0.55, min_range=5.0, max_range=16.0,
                      max_tau=180.0)),
                ("mq.60 rng[5,16] tau180",
                 dict(min_quote=0.60, min_range=5.0, max_range=16.0,
                      max_tau=180.0)),
                ("mq.55 rng[5,20] tau210",
                 dict(min_quote=0.55, min_range=5.0, max_range=20.0,
                      max_tau=210.0)),
                ("mq.65 rng[5,16] tau180",
                 dict(min_quote=0.65, min_range=5.0, max_range=16.0,
                      max_tau=180.0))):
            row(name, run2(**over), len(rs), days)

        print("\n=== SURE: DIRECTION CHECK (the ranking must repeat in both "
              "halves) ===")
        mid = len(rs) // 2
        for lbl, a_over, b_over in (
                ("min_range 0 vs 5 (ceil 35)", dict(min_range=0.0,
                                                    max_range=35.0),
                 dict(min_range=5.0, max_range=35.0)),
                ("max_range 35 vs 16", dict(min_range=5.0, max_range=35.0),
                 dict(min_range=5.0, max_range=16.0)),
                ("max_range 16 vs 20", dict(min_range=5.0, max_range=16.0),
                 dict(min_range=5.0, max_range=20.0)),
                ("min_quote 0.50 vs 0.55", dict(min_quote=0.50),
                 dict(min_quote=0.55)),
                ("max_tau 210 vs 180", dict(max_tau=210.0),
                 dict(max_tau=180.0))):
            got = {}
            for nm, over in (("A", a_over), ("B", b_over)):
                kw = dict(LIVE)
                kw.update(over)
                for lab, sub, sg_sub in (("h1", rs[:mid], sigmas[:mid]),
                                         ("h2", rs[mid:], sigmas[mid:])):
                    es = []
                    for r, sg in zip(sub, sg_sub):
                        e = replay(r, sg, **base, **kw)
                        if e:
                            es.append(e)
                    got[(nm, lab)] = (len(es),
                                      st.fmean([e["ret"] for e in es])
                                      if es else float("nan"))
            a1, a2 = got[("A", "h1")], got[("A", "h2")]
            b1, b2 = got[("B", "h1")], got[("B", "h2")]
            same = (a1[1] > b1[1]) == (a2[1] > b2[1])
            print(f"  {lbl:<22} h1 A {a1[1]:+.4f} (n={a1[0]:<4}) B "
                  f"{b1[1]:+.4f} (n={b1[0]:<4}) | h2 A {a2[1]:+.4f} "
                  f"(n={a2[0]:<4}) B {b2[1]:+.4f} (n={b2[0]:<4})   "
                  f"{'REPEATS' if same else 'FLIPS -> fitting, do not ship'}")
        return

    if args.mode == "price":
        # CAN A PRICE CAP AVOID THE BAD ONES? The arm's break-even win rate for a
        # fill costing `cost` IS `cost` (win pays 1, lose pays 0, so EV = win - cost),
        # which makes `win` and `be` directly comparable per band. The cumulative
        # table is the actionable form: `max_quote` refuses on the QUOTE and the
        # fill lands near it, so a cap is the only knob that can refuse a price.
        ent = run2(max_quote=1.0)
        ent.sort(key=lambda e: e["cost"])
        print("\n=== PRICE BANDS of the arm's OWN entries (cost = fill + fee) ===")
        print("  band          n    win    be=cost   gap     EV/$cost   tot $      t")
        for lo, hi in ((0.45, 0.55), (0.55, 0.65), (0.65, 0.70), (0.70, 0.75),
                       (0.75, 0.80), (0.80, 0.85), (0.85, 1.01)):
            g = [e for e in ent if lo <= e["cost"] < hi]
            if len(g) < 8:
                print(f"  {lo:.2f}-{hi:.2f}  {len(g):>5}   (n too small to read)")
                continue
            wr = st.fmean([1.0 if e["won"] else 0.0 for e in g])
            cost = st.fmean([e["cost"] for e in g])
            print(f"  {lo:.2f}-{hi:.2f}  {len(g):>5}  {wr:6.1%}   {cost:.3f}   "
                  f"{wr-cost:+.3f}   {st.fmean([e['ret'] for e in g]):+.4f}  "
                  f"{sum(e['ret'] for e in g):+7.2f}  "
                  f"{ao.tstat([e['ret'] for e in g]):+6.2f}")
        print("\n=== CUMULATIVE: cap the entry price at X (max_quote) ===")
        for cap in (0.95, 0.85, 0.80, 0.75, 0.70, 0.65):
            g = run2(max_quote=cap)
            if len(g) < 5:
                print(f"  cap {cap:.2f}   n={len(g)} too few to read")
                continue
            order = sorted(g, key=lambda e: e["day"])
            half = len(order) // 2
            by: dict[int, list[float]] = {}
            for e in g:
                by.setdefault(e["day"], []).append(e["ret"])
            dm = [st.fmean(v) for v in by.values()]
            best = max(by, key=lambda d: st.fmean(by[d]))
            rest = [e["ret"] for e in g if e["day"] != best]
            print(f"  cap {cap:.2f}   n={len(g):<5} {len(g)/days:5.1f}/d  WR "
                  f"{100*st.fmean([1.0 if e['won'] else 0.0 for e in g]):5.1f}%  "
                  f"cost {st.fmean([e['cost'] for e in g]):.3f}  "
                  f"EV {st.fmean([e['ret'] for e in g]):+.4f}  "
                  f"t {ao.tstat([e['ret'] for e in g]):+5.2f}  "
                  f"day-t {ao.tstat(dm):+5.2f}  "
                  f"H {st.fmean([e['ret'] for e in order[:half]]):+.3f}/"
                  f"{st.fmean([e['ret'] for e in order[half:]]):+.3f}  "
                  f"dDay {st.fmean(rest) if rest else float('nan'):+.4f}  "
                  f"tot ${sum(e['ret'] for e in g):+.1f}")
        return

    if args.mode == "zone":
        # ========================= REACTION ZONES =========================
        # THE CLAIM: the strike is a reaction level. The payout is written
        # against it, so a favourite that is only a hair past it is a round the
        # price can bounce back through, and those are the avoidable losses.
        # Two DIFFERENT forms, tested side by side so the second cannot be
        # credited with the first's work:
        #   (1) MARGIN  `z_fav` = how far OUR side leads the strike, in sigma of
        #               the move still left. Refuse z_fav < X.
        #   (2) BOUNCE  how many times the spot has ALREADY crossed the strike
        #               this round. Refuse crossings >= N. This is not a
        #               distance filter: a round can end far from the strike on
        #               the final print and still have been through it 4 times.
        # Everything below is applied INSIDE the entry loop (see `replay`), so a
        # refused tick still extends the persistence streak the way the live
        # gate chain does. A post-hoc filter over the entries would score a
        # STRICTER rule than the one that ships.
        ent = run2()
        by_slug = {r["slug"]: r for r in rs}
        idx_of = {r["slug"]: i for i, r in enumerate(rs)}
        for e in ent:
            # signed distance to the strike at the entry tick, oriented so that
            # POSITIVE always means "our side is ahead".
            e["z_fav"] = e["mz"] if e["side"] == "Up" else -e["mz"]
            r = by_slug.get(e["slug"])
            seen = ([s for ts, _t, s, *_ in r["rows"] if ts <= e["ts_ms"] and s]
                    if r else [])
            # the band uses the REPLAY's rolling sigma here (this is a
            # descriptive table over a fixed entry set); the gate sweep below
            # goes through `replay` and therefore uses the live sigma when
            # --use-live-sigma is set.
            i = idx_of.get(e["slug"])
            band = args.zone_band_k * (sigmas[i] if i is not None else 0.0) \
                * (r["strike"] if r else 0.0)
            e["xr"] = crossings(seen, r["strike"], band) if r else 0
        print(f"  (crossings counted with a deadband of {args.zone_band_k:g} x "
              f"1-min sigma)")
        print(f"\n=== REACTION ZONE: the arm's own entries under the LIVE band "
              f"(n={len(ent)}) ===")
        print("  margin z_fav   n    win    be=cost   gap     EV/$cost   tot $"
              "      t   |  xr")
        for lo, hi in ((0.0, 0.30), (0.30, 0.45), (0.45, 0.60), (0.60, 0.80),
                       (0.80, 1.10), (1.10, 9e9)):
            g = [e for e in ent if lo <= e["z_fav"] < hi]
            if len(g) < 8:
                print(f"  {lo:.2f}-{hi:.2f}     {len(g):>5}   (n too small)")
                continue
            wr = st.fmean([1.0 if e["won"] else 0.0 for e in g])
            cost = st.fmean([e["cost"] for e in g])
            xr = st.fmean([e["xr"] for e in g])
            print(f"  {lo:.2f}-{hi:.2f}     {len(g):>5}  {wr:6.1%}   {cost:.3f}"
                  f"   {wr-cost:+.3f}   {st.fmean([e['ret'] for e in g]):+.4f}  "
                  f"{sum(e['ret'] for e in g):+7.2f}  "
                  f"{ao.tstat([e['ret'] for e in g]):+6.2f}   | {xr:.1f}")
        print("  crossings xr   n    win    be=cost   gap     EV/$cost   tot $"
              "      t   | z_fav")
        for lo, hi in ((0, 3), (3, 5), (5, 8), (8, 99)):
            g = [e for e in ent if lo <= e["xr"] < hi]
            if len(g) < 8:
                print(f"  xr {lo}-{hi}       {len(g):>5}   (n too small)")
                continue
            wr = st.fmean([1.0 if e["won"] else 0.0 for e in g])
            cost = st.fmean([e["cost"] for e in g])
            zf = st.fmean([e["z_fav"] for e in g])
            print(f"  xr {lo}-{hi}       {len(g):>5}  {wr:6.1%}   {cost:.3f}"
                  f"   {wr-cost:+.3f}   {st.fmean([e['ret'] for e in g]):+.4f}  "
                  f"{sum(e['ret'] for e in g):+7.2f}  "
                  f"{ao.tstat([e['ret'] for e in g]):+6.2f}   | {zf:.2f}")

        print("\n  -- as a GATE, applied inside the loop (live band) --")
        row("baseline (no zone gate)", run2(), len(rs), days)
        for v in (0.30, 0.45, 0.60, 0.80, 1.00):
            row(f"margin>={v:g}", run2(zone_min_margin=v), len(rs), days)
        print("  -- the crossing gate, at three deadbands "
              "(band = k x 1-min sigma) --")
        for k in (0.0, 0.5, 1.0):
            for n in (4, 3, 2):
                row(f"k={k:g} xr<{n}",
                    run2(zone_band_k=k, zone_max_cross=n), len(rs), days)

        print("\n  -- ⭐ THE ENTRY-SIDE MIRROR OF THE EXIT. 46% of the reaction "
              "EXITS fire at tau 150-210, i.e. within ~60s of entry (see the "
              "tau diagnostic in --mode exit), so the exit is mostly catching "
              "'entered and it immediately went against me'. If that cohort is "
              "bad, refuse it BEFORE the fill instead of paying a spread to "
              "leave it --")
        for b in (0.0, 2.0, 3.0, 4.0, 5.0, 8.0, 12.0):
            row(f"no-rev {b:g}bps/20s{'  (gate off)' if b == 0.0 else ''}",
                run2(entry_rev_bps=b), len(rs), days)
        print("  -- and the same gate on a longer window --")
        for b in (3.0, 5.0, 8.0):
            row(f"no-rev {b:g}bps/60s",
                run2(entry_rev_bps=b, entry_look_s=60.0), len(rs), days)

        print("\n=== ZONE: DIRECTION CHECK (the ranking must repeat in both "
              "halves) ===")
        mid = len(rs) // 2

        def half(sub_r, sub_s, over):
            kw = dict(LIVE)
            kw.update(over)
            out = []
            for r, sg in zip(sub_r, sub_s):
                # ⛔ THE HALVES MUST USE THE SAME SIGMA AS THE TABLE ABOVE.
                # Dropping `live_sig` here silently ran the halves on the
                # replay's own rolling_sigma while the band/gate tables ran on
                # the recorded live sigma -- 268 entries against 181 on the same
                # tape, i.e. the direction check was scoring a different system
                # from the one it was checking. Same rule, same sigma, or the
                # comparison is between two rules.
                e = replay(r, sg, **base, **kw,
                           live_sig=(live_sig.get(r["slug"])
                                     if args.use_live_sigma else None))
                if e:
                    out.append(e)
            return (len(out), st.fmean([e["ret"] for e in out]) if out
                    else float("nan"))

        for lbl, a_over, b_over in (
                ("margin 0.0 vs 0.60", dict(zone_min_margin=0.0),
                 dict(zone_min_margin=0.60)),
                ("margin 0.60 vs 1.00", dict(zone_min_margin=0.60),
                 dict(zone_min_margin=1.00)),
                ("xr<4 @k0.5 off vs on", dict(zone_band_k=0.5),
                 dict(zone_band_k=0.5, zone_max_cross=4)),
                ("xr<4 @k0 off vs on", dict(zone_band_k=0.0),
                 dict(zone_band_k=0.0, zone_max_cross=4)),
                ("xr<3 @k0 off vs on", dict(zone_band_k=0.0),
                 dict(zone_band_k=0.0, zone_max_cross=3)),
                ("xr<3 @k0 4 vs 3", dict(zone_band_k=0.0, zone_max_cross=4),
                 dict(zone_band_k=0.0, zone_max_cross=3)),
                ("xr<2 @k0 3 vs 2", dict(zone_band_k=0.0, zone_max_cross=3),
                 dict(zone_band_k=0.0, zone_max_cross=2)),
                ("xr<3 @k1.0 off vs on", dict(zone_band_k=1.0),
                 dict(zone_band_k=1.0, zone_max_cross=3))):
            a1 = half(rs[:mid], sigmas[:mid], a_over)
            a2 = half(rs[mid:], sigmas[mid:], a_over)
            b1 = half(rs[:mid], sigmas[:mid], b_over)
            b2 = half(rs[mid:], sigmas[mid:], b_over)
            same = (a1[1] > b1[1]) == (a2[1] > b2[1])
            print(f"  {lbl:<22} h1 A {a1[1]:+.4f} (n={a1[0]:<4}) B "
                  f"{b1[1]:+.4f} (n={b1[0]:<4}) | h2 A {a2[1]:+.4f} "
                  f"(n={a2[0]:<4}) B {b2[1]:+.4f} (n={b2[0]:<4})   "
                  f"{'REPEATS' if same else 'FLIPS -> fitting, do not ship'}")
        return

    if args.mode == "exit":
        # ================== EARLY EXIT ("price reacting") ==================
        # THE CLAIM: do not refuse the entry -- leave it early, when the price
        # starts reacting back off the strike, before it settles against us.
        # This is NOT the refuted `cross_exit` rule: that one keyed on the ARM'S
        # OWN TRIGGER turning (see `main.Bot._cross_exit_check`, which records
        # the retraction and the live record: ten completed sales, ALL TEN LOST,
        # -$3.04). The rules here key on the MARKET, not on our signal.
        #
        # THE COST THIS HAS TO CLEAR IS THE SPREAD, NOT THE MOVE. The arm buys
        # at the ASK and an exit sells into the BID, so the round trip pays the
        # whole spread before anything else. Measured on this tape the spread
        # runs ~0.02 on btc-5m (2026-09-22 06:02 btc-updown-15m: ask 0.55 /
        # bid 0.53) and up to 0.15 on the thinner families -- against a
        # per-trade EV of about 0.10. That, and not the timing, is the hurdle.
        #
        # WHAT IS MODELLED HONESTLY:
        #  * the sale is priced at the recorded BID minus `slip`, then charged
        #    the venue fee -- never at the mark, never at the ask;
        #  * the venue's SELL MINIMUM is enforced (`SELL_MIN_NOTIONAL_USDT`).
        #    It binds EXACTLY when the stop is doing its job: at a $2 stake a
        #    position whose bid has fallen below ~0.45 is too small to sell, so
        #    the stop is refused at the moment it matters. The count is printed;
        #  * the trigger must PERSIST `confirm_s` (the repo already learned this:
        #    a one-tick flip is our own stale book), and a `hold_s` minimum
        #    after the fill, because a position is born on a signal already a
        #    second old;
        #  * no exit past `min_tau` -- the book goes one-sided at the bell.
        ent = run2()
        by_slug = {r["slug"]: r for r in rs}
        idx_of = {r["slug"]: i for i, r in enumerate(rs)}
        bids: dict[str, dict[int, tuple]] = {}
        for e in ent:
            if e["slug"] in bids:
                continue
            bids[e["slug"]] = {
                t: (ub, db, ba) for t, ub, db, ba in con.execute(
                    "SELECT ts_ms, up_bid, down_bid, book_age_ms FROM quotes "
                    "WHERE slug=? AND up_bid IS NOT NULL", (e["slug"],))}
        SLIP = 0.01          # one tick of concession to cross the spread
        # ⚠️ `STAKE` is a MODULE constant (defined at the top of the file), not
        # a local here: `row()` -- used by the ofat/joint/zone grids -- prints a
        # drawdown line too, and a function defined at module scope cannot see a
        # name bound inside `main`. It was a local until that line was added.

        # ⛔ THE VENUE'S FLOOR IS ON THE SLICE THAT IS SENT, NOT ON THE POSITION.
        #
        # MEASURED LIVE, 2026-09-22 13:42:01: the reaction exit fired on a
        # 2.6831-share position and `sell_fak` refused it --
        #     proceeds $0.3556 under the venue's ~$1.50 MARKET minimum
        #     (0.6708 shares @ 0.5300) -- not sent
        # 0.6708 shares is `frac 0.25` of 2.6831, so the check is on the SALE's
        # notional: 0.25 x shares x price >= 1.50. That needs a position worth
        # **>= $6.00**, and this arm trades $1-3, so a 25% exit is refused at
        # EVERY price. The live record agrees: `react` exits booked = 0.
        #
        # The first version of this file compared the WHOLE position (`shares *
        # px_out < 1.5`), a different and much laxer rule: it binds only once
        # the bid falls below ~0.75 x entry, which is why the exit looked
        # viable at frac 0.25 in every sweep. `--whole-floor` reproduces it.
        from btcpredict.execution.live import (
            SELL_MIN_NOTIONAL_USDT as _SELL_MIN)
        FLOOR_BASE = 0.25      # model_exit_frac, the configured partial
        FLOOR_ON_SLICE = not args.whole_floor

        def _eff_frac(f, sh: float, pxo: float, base: float):
            """The fraction of the position that would actually be SENT.

            `f == "dyn"` is the DYNAMIC rule: sell the SMALLEST slice that
            clears the venue's floor, never below the configured `base`. It is
            derived from the venue's constraint rather than tuned, so it keeps
            the configured partial whenever the venue allows it and degrades to
            a full exit exactly when a partial one is impossible.

            None when even the WHOLE position cannot clear the floor -- at that
            price the venue will not buy it at all, which is the case the log
            above shows.
            """
            if f != "dyn":
                return float(f)
            if sh * pxo <= 0.0:
                return None
            need = _SELL_MIN / (sh * pxo)
            if need > 1.0:
                return None
            return min(1.0, max(base, need))

        def _slice_ok(f, sh: float, pxo: float) -> bool:
            """Would the venue accept this sale? `--whole-floor` reproduces
            the wrong version, which compared the whole position."""
            n = sh * pxo if not FLOOR_ON_SLICE else sh * f * pxo
            return n >= _SELL_MIN

        # ⛔⛔ THE FILL RULE FOR THE RESTING LIMIT -- READ THIS BEFORE TRUSTING
        # ANY ROW OF THE `--limit-fallback` TABLE.
        #
        # A resting order MUST NOT be credited with a fill merely because the
        # displayed price REACHED it. That is the single most expensive artifact
        # in this repo's backtest history: modelled on the same tape with the
        # same strategy and only the fill rule changed, a touch-fill "maker"
        # path read **+0.736/$1, t +12.4** while requiring the tape to print
        # THROUGH the resting price read **-0.182/$1, t -2.5**. The lie is worth
        # about +0.92 per $1.
        #
        # It is not a modelling nicety, it is the whole economics: a resting
        # order is only reached when the market comes to it, and the market
        # comes to a resting SELL when the bid RISES to us. That is a bounce,
        # and conditional on needing to exit, bounces are the exception.
        #
        # So: the order rests at `lim` and fills only at the first tick where a
        # LATER recorded bid is >= lim + one tick. The entry tick itself cannot
        # fill it (its own bid is < lim + 0.01 by construction), so an instant
        # cross at the trigger is NOT credited either -- deliberately, since
        # that would be a taker fill at ~the same price as `sell_fak` and would
        # hide the real question, which is whether the order that the venue's
        # MARKET minimum refuses can be SOLD AT ALL.
        THROUGH = 0.01       # one venue tick, in price
        LIMIT_FB = bool(args.limit_fallback)
        LIMIT_SINGLE = bool(args.limit_single)

        def _limit_fill(bmap: dict, side_: str, ts0: int, lim: float,
                        stop_ts: int) -> int | None:
            """The tick at which a RESTING sell at `lim` is traded through.

            None means the tape never came through the price before the book
            goes one-sided at `min_tau` -- a MISSED exit, and the position is
            still held at the bell.
            """
            for t in sorted(bmap):
                if t <= ts0 or (stop_ts and t > stop_ts):
                    continue
                row_ = bmap[t]
                bb = row_[0] if side_ == "Up" else row_[1]
                if bb is not None and bb >= lim + THROUGH:
                    return t
            return None

        def _sell_slice(ts: int, side_: str, bb, px_out: float, eff,
                        cost: float, shares: float, bmap: dict,
                        stop_ts: int):
            """Price ONE exit and say which path took it.

            MARKET exactly as live -- if the venue's minimum accepts the slice,
            that is the sale and the limit never exists. Only when it refuses
            does `--limit-fallback` rest an order instead.

            Returns `(kind, eff, ret_ex, px_used)` with kind one of
            "market" / "limit" / "blocked" (refused, no fallback) / "missed"
            (posted, never reached). ONE SLICE, ONE CALL: the fee is charged on
            the price actually got, and the limit is charged the fee the fill
            actually carries -- no maker rebate is assumed anywhere.

            ⚠️ THE ORDER IS RE-PRICED AT EVERY TRIGGERING TICK, which is the
            permissive reading of "keep trying to sell": each attempt rests at
            the CURRENT bid's tick, so a falling book lowers the price the
            order needs to be reached at. `--limit-single` freezes the first
            attempt's price instead (one order, resting to the bell) and is the
            bound to check any row against.
            """
            if eff is not None and _slice_ok(eff, shares, px_out):
                got = px_out * (1.0 - fee_rate_for_price(px_out))
                return "market", float(eff), (got - cost) / cost, px_out
            if not LIMIT_FB or bb is None:
                return "blocked", 0.0, 0.0, 0.0
            if LIMIT_SINGLE and LIMIT["cur"] in LIMIT["fixed"]:
                # one order per position, and it has already missed
                return "missed", 0.0, 0.0, 0.0
            # `eff is None` is the `dyn` case where NO slice clears the floor:
            # the venue constraint that forced a full exit is gone here, so the
            # fallback reverts to the CONFIGURED partial rather than to 100%.
            eff2 = FLOOR_BASE if eff is None else float(eff)
            lim, ts0 = max(0.01, int(bb * 100) / 100.0), ts
            if LIMIT_SINGLE:
                LIMIT["fixed"][LIMIT["cur"]] = (lim, ts0)
            LIMIT["posted"].add(LIMIT["cur"])
            if _limit_fill(bmap, side_, ts0, lim, stop_ts) is None:
                LIMIT["miss"] += 1
                return "missed", 0.0, 0.0, 0.0
            got = lim * (1.0 - fee_rate_for_price(lim))
            LIMIT["sold"] += 1
            LIMIT["px"].append(lim)
            return "limit", eff2, (got - cost) / cost, lim

        def sell_ret(e, rule, stop_c=0.0, stop_pct=0.0, confirm_s=0.0,
                     hold_s=0.0, min_tau=60.0, slip=SLIP,
                     fair_m=0.0, fair_drop=0.0, stale_ms=1000,
                     look_s=20.0, rev_bps=5.0, frac=1.0,
                     force_tau=0.0, trail_c=0.0):
            """Per-$1 return if the rule exits, else the HOLD return.

            Returns (ret, exited, blocked) -- `blocked` counts triggers whose
            sale the venue would refuse for being under its minimum."""
            if rule == "none":          # the baseline: hold to settlement
                return e["ret"], False, 0, None
            r = by_slug[e["slug"]]
            side, strike = e["side"], r["strike"]
            bmap = bids.get(e["slug"]) or {}
            cost = e["cost"]
            shares = STAKE / cost
            # the (ts, spot) series once, for the reaction move
            spots = [(x[0], x[2]) for x in r["rows"] if x[2]]
            sgn = 1.0 if side == "Up" else -1.0
            # the sigma the arm actually had, for the per-tick fair
            _i = idx_of.get(e["slug"])
            sg0 = sigmas[_i] if _i is not None else 0.0
            sseries = live_sig.get(e["slug"]) if args.use_live_sigma else None
            held, blocked = 0.0, 0
            peak_bb = 0.0
            # WHERE A RESTING ORDER MAY STILL BE REACHED: up to the last tick
            # above `min_tau`. Past that the book goes one-sided, so a GTC
            # order left resting would simply be cancelled at the bell.
            stop_ts = 0
            for _ts, _tau, *_tail in r["rows"]:
                if _tau is None or _tau > min_tau:
                    stop_ts = _ts
            for ts, tau, spot, *_rest in r["rows"]:
                if ts < e["ts_ms"] + hold_s * 1000:
                    continue
                # ⭐ THE FORCED (CLOCK) EXIT IS CHECKED BEFORE THE `min_tau`
                # BREAK. Every other rule here is a rule about PRICE, and
                # `min_tau` exists so those cannot fire on the entry latency;
                # a forced exit at tau <= T is a rule about THE CLOCK and it is
                # by construction the last tick at or above T. Letting the break
                # run first would make the rule unobservable (it broke out at
                # the first tick under min_tau, so a force_tau < min_tau could
                # never fire). This is the ONLY family that removes the full
                # -100% loss BY CONSTRUCTION, so it prices the insurance.
                if force_tau > 0 and tau is not None and tau <= force_tau:
                    b = bmap.get(ts)
                    bb = None if not b else (b[0] if side == "Up" else b[1])
                    if bb is None:
                        continue
                    px_out = bb - slip
                    if px_out <= 0.0:
                        continue
                    eff = _eff_frac(frac, shares, px_out, FLOOR_BASE)
                    kind, eff_x, ret_ex, px_used = _sell_slice(
                        ts, side, bb, px_out, eff, cost, shares, bmap, stop_ts)
                    if kind == "blocked":
                        blocked += 1
                        continue
                    if kind == "missed":
                        continue
                    EXIT_TAU.append(float(tau or 0.0))
                    if eff_x >= 1.0:
                        return ret_ex, True, blocked, px_used
                    return (eff_x * ret_ex + (1.0 - eff_x) * e["ret"], True,
                            blocked, px_used)
                # ⛔ `force` MUST NOT FALL THROUGH TO THE PRICE BRANCHES. It is
                # not a rule name the branches know, so without this it landed
                # on the final `else` (the -stop_pct stop) and fired on every
                # tick where the bid sat at or below cost -- a completely
                # different rule that would have been reported as the clock
                # exit. Its ONLY trigger is the block above.
                if rule == "force":
                    continue
                if tau is not None and tau <= min_tau:
                    break
                b = bmap.get(ts)
                bb = None if not b else (b[0] if side == "Up" else b[1])
                if rule == "react":
                    hit = bool(spot and strike and (
                        spot < strike if side == "Up" else spot > strike))
                elif rule == "reactmove":
                    # ⭐ THE SHIPPED RULE, mirrored exactly from
                    # `main.Bot._reaction_against`: the spot is still AHEAD of
                    # the strike (there is a lead to protect) and has moved
                    # AGAINST us by >= rev_bps over the last look_s.
                    if not (spot and strike and tau and tau > 0):
                        continue
                    if math.log(spot / strike) * sgn <= 0:
                        continue
                    past = [s for t, s in spots if t <= ts - look_s * 1000]
                    if not past:
                        continue
                    mv = math.log(spot / past[-1]) * 1e4 * sgn
                    hit = mv <= -rev_bps
                elif rule in ("fair", "fairdrop", "stale"):
                    # ⭐ THE ONLY FORM OF "EXIT EARLY" THAT CAN PAY, and the
                    # exact criterion: a position worth `p` is worth selling iff
                    # the bid exceeds `p`. So exit when the VENUE IS PAYING MORE
                    # THAN OUR OWN FAIR. Every price-stop below is a rule about
                    # a move that has ALREADY happened, and it carries no
                    # information about which way the round settles -- which is
                    # why they take the win rate from 68% to 24-37%.
                    # `stale` is the refinement the venue offers: only act when
                    # the bid is a STALE quote (`book_age_ms`), i.e. the venue
                    # has not repriced to the spot in front of it. That is a
                    # known mispricing on this venue; a bid that merely differs
                    # from our fair is not.
                    sg_i = _sig_at(sseries, ts, sg0)
                    if not (spot and strike and tau and tau > 0 and sg_i > 0):
                        continue
                    fu = fair_value(spot, strike, tau, sg_i)
                    fs = fu if side == "Up" else 1.0 - fu
                    if rule == "fair":
                        hit = bb is not None and (bb - fs) >= fair_m
                    elif rule == "stale":
                        age = None if not b else b[2]
                        hit = (bb is not None and (bb - fs) >= fair_m
                               and age is not None and age >= stale_ms)
                    else:
                        hit = e["fair"] is not None and \
                            fs <= e["fair"] - fair_drop
                else:
                    if bb is None:
                        continue
                    # ⭐ A STOP CANNOT PRODUCE A POSITIVE EXIT: it sells at a
                    # price BELOW what we paid, so its exit P&L is negative by
                    # construction and it can only be judged on loss AVOIDANCE.
                    # `tp` / `tp_pct` are the opposite family -- they sell only
                    # while the position is IN PROFIT -- and they are the ones
                    # that can be judged on the criterion "pnl > 0 on exit".
                    if rule == "tp":
                        hit = bb >= e["px"] + stop_c
                    elif rule == "tp_pct":
                        hit = bb >= e["px"] * (1.0 + stop_pct)
                    elif rule == "stop":
                        hit = bb <= e["px"] - stop_c
                    elif rule == "trail":
                        # ⭐ A TRAILING EXIT: sell after the bid gives back
                        # `trail_c` from its OWN peak since entry. It is the
                        # only price rule that needs no constant -- the level is
                        # the trade's own high-water mark -- so it is the fair
                        # test of the whole "exit when it reacts" family on the
                        # drawdown axis. It is still a rule about a move that has
                        # ALREADY happened, so expect it to pay like a stop.
                        peak_bb = max(peak_bb, bb)
                        hit = peak_bb > 0.0 and bb <= peak_bb - trail_c
                    else:
                        hit = bb <= e["px"] * (1.0 - stop_pct)
                if not hit:
                    held = 0.0
                    continue
                held += 1.0
                if held < confirm_s:
                    continue
                if bb is None:
                    continue
                px_out = bb - slip
                if px_out <= 0.0:
                    continue
                eff = _eff_frac(frac, shares, px_out, FLOOR_BASE)
                kind, eff_x, ret_ex, px_used = _sell_slice(
                    ts, side, bb, px_out, eff, cost, shares, bmap, stop_ts)
                if kind == "blocked":
                    blocked += 1
                    continue
                if kind == "missed":
                    continue
                EXIT_TAU.append(float(tau or 0.0))
                # ⭐ A PARTIAL EXIT, and it is the only lever that CAPS the loss
                # without giving up the whole edge. Sell `frac` of the position
                # at the reaction and keep the rest to the bell: the realised
                # return is the stake-weighted blend, so the downside on a round
                # that eventually loses is `frac * (a small loss) +
                # (1-frac) * (-100%)` instead of a full -100%. The code path
                # already supports it -- `_cross_exit_execute` splits a partial
                # sale into a settled part and a new open fill.
                if eff_x >= 1.0:
                    return ret_ex, True, blocked, px_used
                return (eff_x * ret_ex + (1.0 - eff_x) * e["ret"], True, blocked,
                        px_used)
            return e["ret"], False, blocked, None

        def erow(label, rule, **kw):
            EXIT_TAU.clear()
            LIMIT["sold"] = 0
            LIMIT["miss"] = 0
            LIMIT["px"].clear()
            LIMIT["posted"].clear()
            LIMIT["fixed"].clear()
            out, nex, blocked, blocked_entries, ex_rets = [], 0, 0, 0, []
            ex_px, costs = [], []
            for e in ent:
                LIMIT["cur"] = e["slug"]
                ret, ex, bl, pxo = sell_ret(e, rule, **kw)
                out.append(ret)
                nex += ex
                if ex:
                    ex_rets.append(ret)
                    ex_px.append(pxo)
                    costs.append(e["cost"])
                blocked += bl
                blocked_entries += 1 if (bl and not ex) else 0
            wr = 100 * sum(1 for r_ in out if r_ > 0) / len(out)
            by: dict[int, list[float]] = {}
            for e, r_ in zip(ent, out):
                by.setdefault(e["day"], []).append(r_)
            dm = [st.fmean(v) for v in by.values()]
            order = sorted(zip(ent, out), key=lambda p: p[0]["day"])
            half = len(order) // 2
            # THE USER'S CRITERION, reported directly: of the positions this
            # rule actually SOLD, how many realised a positive P&L, and what did
            # the average sale realise? A stop scores 0% here by construction.
            pos = 100 * sum(1 for r_ in ex_rets if r_ > 0) / nex if nex else 0.0
            avgex = st.fmean(ex_rets) if ex_rets else 0.0
            # PnL at the live flat $2 stake, which is the number the account
            # actually moves by; `tot@$2` uses the live stake rather than $1 so a
            # strategy comparison is in dollars, not in per-share units.
            # A HOLD row and an EXIT row with the SAME `n` is the whole point:
            # an exit rule never changes how many trades are taken.
            # ⭐ PAID vs SOLD is the column that answers "how can an exit lose
            # money?". The trigger is an ADVERSE move, so an exit usually
            # happens at a price BELOW what we paid -- being ahead of the STRIKE
            # (which the rule requires) says nothing about being above our ENTRY.
            prices = (f"paid {st.fmean(costs):.3f} sold {st.fmean(ex_px):.3f}"
                      if ex_px else "paid   -   sold   -  ")
            # ⛔ THE LOSS TAIL, which is a DIFFERENT objective from EV and the
            # one that actually matters to an account. `whole` is the share of
            # trades that lose the ENTIRE stake (a binary settling against a
            # position held to the bell); `worst` is the single worst trade.
            whole = 100 * sum(1 for r_ in out if r_ <= -0.99) / len(out)
            worst = min(out)
            # The fallback's own two numbers, printed only when it was used: of
            # the triggers its MARKET path refused, how many the resting order
            # actually reached. A high `missed` is the whole story on a rule
            # that only pays when the tape bounces.
            lim_tag = ""
            if LIMIT["sold"] or LIMIT["miss"]:
                # POSITIONS, not attempts: the loop re-prices per triggering
                # tick, so `miss` counts order-attempts and would overstate how
                # many positions were exposed to a miss.
                npos = len(LIMIT["posted"])
                mx = st.fmean(LIMIT["px"]) if LIMIT["px"] else 0.0
                lim_tag = (f"  | limit {LIMIT['sold']}/{npos} positions filled "
                           f"@{mx:.3f}  missed {LIMIT['miss']} attempts")
            print(f"  {label:<24} n={len(out):<4} exited {nex:>4} "
                  f"({100*nex/len(out):3.0f}%)  WR {wr:5.1f}%  "
                  f"whole-stake -100% {whole:5.1f}%  worst {worst:+.2f}  "
                  f"exitPnL>0 {pos:5.1f}%  {prices}  "
                  f"EV {st.fmean(out):+.4f}/$1  t {ao.tstat(out):+5.2f}  "
                  f"day-t {ao.tstat(dm):+5.2f}  "
                  f"H {st.fmean([r_ for _e, r_ in order[:half]]):+.3f}/"
                  f"{st.fmean([r_ for _e, r_ in order[half:]]):+.3f}  "
                  f"tot@$2 ${STAKE*sum(out):+7.2f}  "
                  f"| unsellable {blocked_entries:>3}{lim_tag}")
            # ⭐ DRAWDOWN, NOT JUST PnL. These rules exist to change the SHAPE of
            # the loss distribution, and the total is nearly blind to that: an
            # exit that turns full losses into small ones barely moves the sum
            # while it moves the drawdown a lot. `ent` is already in round-start
            # order (`run2` walks `rs` in start_ms order), so the equity curve
            # below is the one an account would actually have lived through.
            # `ret/dd` is the return per unit of pain; `worst streak` is how many
            # losers in a row; `worst-5` is the mean of the five worst trades --
            # a tail measure that a full-loss rule cannot hide from.
            eq, peak, mdd, streak, worst_streak = 0.0, 0.0, 0.0, 0, 0
            for r_ in out:
                eq += STAKE * r_
                peak = max(peak, eq)
                mdd = min(mdd, eq - peak)
                streak = streak + 1 if r_ < 0 else 0
                worst_streak = max(worst_streak, streak)
            tail5 = st.fmean(sorted(out)[:5]) if len(out) >= 5 else float("nan")
            ratio = (STAKE * sum(out)) / abs(mdd) if mdd < 0 else float("inf")
            mid_ = len(out) // 2
            mdd1, mdd2 = _maxdd(out[:mid_]), _maxdd(out[mid_:])
            print(f"  {label:<24} maxDD ${mdd:+6.2f}  "
                  f"tot/|maxDD| {ratio:5.2f}  worst streak {worst_streak:<3} "
                  f"mean-worst-5 {tail5:+.4f}  "
                  f"({STAKE*5*tail5:+.2f} on the 5 worst)  "
                  f"| halfDD {mdd1:+.2f}/{mdd2:+.2f}")

        print(f"\n=== EARLY EXIT vs HOLD ({len(ent)} entries, live band, "
              f"exit into the BID - {SLIP:g}) ===")
        erow("HOLD (exit off)", "none")
        # ⭐ THE FORM THAT CAN PAY: sell when the VENUE PAYS MORE THAN OUR FAIR.
        # A position worth `p` is worth selling iff bid > p, so this is the
        # criterion, not a heuristic. `fair_m` <= 0 makes it more eager.
        for m in (0.0, -0.02, -0.05, -0.08):
            erow(f"bid >= fair{m:+.2f}", "fair", fair_m=m, hold_s=5.0)
        # the other reading of "thesis death": our fair itself decays by X
        for d in (0.05, 0.10, 0.20):
            erow(f"fair drops -{d:.2f}", "fairdrop", fair_drop=d, hold_s=5.0)
        print("  -- ⭐ THE LEVER: only sell into a STALE venue quote --")
        for a in (500, 1000, 2000):
            erow(f"bid>=fair & age>={a}ms", "stale", fair_m=0.0, stale_ms=a,
                 hold_s=5.0)
        print("  -- ⭐ TAKE PROFIT: sell only while IN PROFIT (the only family "
              "whose exits can satisfy pnl>0) --")
        for c in (0.04, 0.08, 0.12, 0.20):
            erow(f"tp +{c:.2f} on the bid", "tp", stop_c=c, hold_s=5.0)
        for p in (0.05, 0.10, 0.20, 0.35):
            erow(f"tp +{p:.0%} on the bid", "tp_pct", stop_pct=p, hold_s=5.0)
        print("  -- ⭐ THE SHIPPED MODEL RULE (reaction), as a strategy --")
        for look, rev in ((20.0, 5.0), (20.0, 3.0), (10.0, 5.0)):
            erow(f"reactmove {look:g}s/{rev:g}bps", "reactmove",
                 look_s=look, rev_bps=rev, hold_s=5.0, min_tau=60.0)
        print("  -- ⭐ PARTIAL reaction exits: cap the loss, keep the edge --")
        for frac in (0.25, 0.50, 0.75):
            erow(f"react 20s/5bps x{frac:.0%}", "reactmove",
                 look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=60.0, frac=frac)
        if args.limit_fallback:
            # ⭐⭐ THE FRAC DECISION, RE-OPENED. The rows above are priced with
            # `sell_fak` alone, so at a $1-3 stake EVERY partial is refused by
            # the venue's ~$1.50 MARKET minimum and the only sellable slice is
            # 100% -- which is why `model_exit_frac` was set to 1.0. With
            # `LiveBroker.sell_limit` the slice is placeable at any size, so the
            # configured partial is a real option again and the frac question
            # has to be answered on the tape rather than by the floor.
            # READ THE FILL RATE, NOT THE EV: `limit N/M filled` says how many
            # of the refused slices the resting order actually reached. The
            # 100% rows are the control that needs no fallback at all.
            print("  -- ⭐⭐ SAME RULE WITH THE RESTING-LIMIT FALLBACK "
                  "(--limit-fallback): the frac decision, re-opened --")
            for frac in (0.25, 0.50, 0.75, 1.0, "dyn"):
                lbl = f"x{frac}" if isinstance(frac, str) else f"x{frac:.0%}"
                erow(f"react 20s/5bps {lbl} +lim", "reactmove",
                     look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=60.0,
                     frac=frac)
        print("  -- the price-stops that were refuted, kept as the control --")
        erow("stop -0.02 (bid)", "stop", stop_c=0.02, confirm_s=1.0, hold_s=5.0)
        erow("stop -10% (bid)", "stop_pct", stop_pct=0.10, confirm_s=1.0,
             hold_s=5.0)
        print("  -- ⭐ TRAILING exits: the level is the trade's own high-water "
              "mark, so no constant is fitted --")
        for c in (0.04, 0.08, 0.12, 0.20, 0.30):
            erow(f"trail -{c:.2f} from peak", "trail", trail_c=c, hold_s=5.0)
        print("  -- ⭐ THE CLOCK EXIT: sell everything at tau <= T, whatever the "
              "price. This is the ONLY family that removes the -100% loss by "
              "construction, so it PRICES THE INSURANCE -- read `whole-stake "
              "-100%` and `unsellable`, not the total --")
        for t in (10.0, 30.0, 60.0, 90.0, 120.0, 180.0, 210.0):
            erow(f"sell all at tau<={t:g}s", "force", force_tau=t, min_tau=0.0,
                 hold_s=5.0)
        erow("react@strike confirm3s", "react", confirm_s=3.0, hold_s=5.0)
        # ⭐ TUNING THE SHIPPED RULE AGAINST ITS OWN DEFAULTS. `frac` had only
        # ever been tried at 0.25/0.50/0.75, and `min_tau` and `min_hold_s` had
        # NEVER been swept at all -- yet `min_tau=60` is a hard floor on when
        # the rule may fire, which is the single biggest thing it does. Every
        # row here is the SHIPPED rule with exactly one knob moved, so the
        # reference row is `react 20s/5bps x25%` above; read the H column (the
        # ranking must repeat in both halves) and maxDD beside the total.
        print("  -- ⭐ THE SHIPPED RULE, ONE KNOB AT A TIME (reference row = "
              "react 20s/5bps x25%) --")
        # ⛔ THE "shipped" MARKERS AND THE FIXED ARGUMENTS ARE READ FROM
        # SETTINGS, NEVER HARDCODED. They were literals (0.25 / 5.0 / 20.0 /
        # 60.0 / 5.0); the moment the live profile moved one -- `model_exit_frac`
        # went 0.25 -> 1.0 on 2026-09-22 -- this sweep was ranking a system
        # nobody runs, with the label pointing at a row that is NOT shipped.
        _S_FRAC = float(settings.model_exit_frac)
        _S_REV = float(settings.model_exit_rev_bps)
        _S_LOOK = float(settings.model_exit_look_s)
        _S_MTAU = float(settings.model_exit_min_tau_s)
        _S_HOLD = float(settings.model_exit_min_hold_s)

        def _is(v: float, ref: float) -> str:
            return "  <- shipped" if abs(v - ref) < 1e-9 else ""

        print(f"  ---- the fraction sold (model_exit_frac, shipped "
              f"{_S_FRAC:.2f}) ----")
        for fr in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00):
            erow(f"frac {fr:.0%}{_is(fr, _S_FRAC)}",
                 "reactmove", look_s=_S_LOOK, rev_bps=_S_REV, hold_s=_S_HOLD,
                 min_tau=_S_MTAU, frac=fr)
        # ⭐ THE DYNAMIC RULE, beside the fixed fractions it would replace: the
        # smallest slice that clears the venue floor, never below 25%. Where a
        # fixed 25% is refused at EVERY price, this sells what the venue will
        # actually take.
        erow("frac dyn (clears $1.50)", "reactmove", look_s=20.0,
             rev_bps=5.0, hold_s=5.0, min_tau=60.0, frac="dyn")
        print(f"  ---- the reaction size (model_exit_rev_bps, shipped "
              f"{_S_REV:g}) ----")
        for rv in (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0):
            erow(f"rev {rv:g}bps{_is(rv, _S_REV)}",
                 "reactmove", look_s=_S_LOOK, rev_bps=rv, hold_s=_S_HOLD,
                 min_tau=_S_MTAU, frac=_S_FRAC)
        print(f"  ---- the lookback (model_exit_look_s, shipped "
              f"{_S_LOOK:g}) ----")
        for lk in (10.0, 20.0, 30.0, 45.0, 60.0, 90.0):
            erow(f"look {lk:g}s{_is(lk, _S_LOOK)}",
                 "reactmove", look_s=lk, rev_bps=_S_REV, hold_s=_S_HOLD,
                 min_tau=_S_MTAU, frac=_S_FRAC)
        print("  ---- the late-exit floor (model_exit_min_tau_s, shipped 60.0) "
              "----")
        print("  ---- the late-exit floor (model_exit_min_tau_s, shipped "
              f"{_S_MTAU:g}) ----")
        for mt in (0.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 45.0, 50.0, 60.0,
                   75.0, 90.0):
            erow(f"min_tau {mt:g}s{_is(mt, _S_MTAU)}",
                 "reactmove", look_s=_S_LOOK, rev_bps=_S_REV, hold_s=_S_HOLD,
                 min_tau=mt, frac=_S_FRAC)
        # ⭐ AND WHERE THE EXITS LIVE, which is what a min_tau sweep cannot show:
        # the number of exits is flat across 0/15/30 for a reason, and this says
        # which one. Printed for the SHIPPED rule (last erow above).
        erow("react @ shipped knobs (tau diag)", "reactmove", look_s=_S_LOOK,
             rev_bps=_S_REV, hold_s=_S_HOLD, min_tau=0.0, frac=_S_FRAC)
        buckets = ((0, 15), (15, 30), (30, 45), (45, 60), (60, 75), (75, 90),
                   (90, 120), (120, 150), (150, 210), (210, 9999))
        print(f"  ---- TAU OF THE {len(EXIT_TAU)} EXITS (shipped trigger, "
              f"min_tau=0 so nothing is masked) ----")
        for lo, hi in buckets:
            k = sum(1 for t in EXIT_TAU if lo <= t < hi)
            if k or lo <= 60:
                print(f"       tau {lo:>3}-{hi:<4} {k:>4}")
        if EXIT_TAU:
            print(f"       earliest exit tau {min(EXIT_TAU):.0f}s, "
                  f"median {st.median(EXIT_TAU):.0f}s")
        print("  ---- the minimum hold (model_exit_min_hold_s, shipped 5.0) ----")
        for hd in (0.0, 2.0, 5.0, 10.0, 20.0, 30.0):
            erow(f"hold {hd:g}s{_is(hd, _S_HOLD)}",
                 "reactmove", look_s=_S_LOOK, rev_bps=_S_REV, hold_s=hd,
                 min_tau=_S_MTAU, frac=_S_FRAC)
        print("  (HOLD is the same-entry baseline the exit is scored against; "
              "`t` is pooled over per-entry returns, and a rule that only "
              "changes the LOSERS cannot raise it by much.)")
        return

    if args.mode == "struct":
        # ============ DOES SPOT STRUCTURE PREDICT THE SETTLEMENT? ============
        # The ask was to use spot logic -- support/resistance, liquidity zones,
        # order blocks -- to find reaction zones an exit could key on.
        #
        # THE ORDER OF ENQUIRY MATTERS. In THIS market the payout is
        # `sign(spot_at_the_bell - strike)` and nothing else, so a level that is
        # not the strike cannot change the settlement directly: it can only
        # matter by predicting where the spot ENDS UP. That makes this a single,
        # cheap, decisive question -- does the feature separate the rounds that
        # settle our way from the ones that do not? If it does not, no exit
        # built from it can work, however sound the chart reading is; that is
        # exactly the test our own `fair` already failed (corr(claimed edge,
        # realised) ~ -0.01).
        #
        # Features are computed from the round's own recorded spot tape at the
        # entry tick, so they are exactly what the bot could have known then.
        # `won` is the settlement. `corr` is point-biserial; the HALVES columns
        # are the same correlation recomputed on each half of the period, and a
        # feature only counts if the SIGN repeats.
        ent = run2()
        by_slug = {r["slug"]: r for r in rs}
        for e in ent:
            r = by_slug[e["slug"]]
            rows = [q for q in r["rows"] if q[0] <= e["ts_ms"] and q[2]]
            if len(rows) < 5:
                continue
            spots = [q[2] for q in rows]
            spot, strike = spots[-1], r["strike"]
            hi, lo = max(spots), min(spots)
            rng = hi - lo
            e["f_pos"] = ((spot - lo) / rng) if rng > 0 else 0.5
            e["f_dhi"] = 1e4 * (hi - spot) / strike      # bps under the high
            e["f_dlo"] = 1e4 * (spot - lo) / strike      # bps over the low
            e["f_rng"] = 1e4 * rng / strike
            rec = [q[2] for q in rows if q[0] >= e["ts_ms"] - 60_000]
            e["f_imp"] = (1e4 * (max(rec) - min(rec)) / strike
                          if len(rec) > 3 else 0.0)       # the "impulse"
            mag = 10 ** max(1, int(math.floor(math.log10(spot))) - 1)
            for tag, st_ in (("f_rn1k", mag), ("f_rn100", mag / 10.0)):
                e[tag] = 1e4 * abs(spot - round(spot / st_) * st_) / spot
            e["f_mz"] = abs(e["mz"])
            e["y"] = 1.0 if e["won"] else 0.0
        feats = [f for f in ("f_pos", "f_dhi", "f_dlo", "f_rng", "f_imp",
                             "f_rn1k", "f_rn100", "f_mz") if f in ent[0]]

        def corr(xs, ys):
            if len(xs) < 5:
                return float("nan")
            mx, my = st.fmean(xs), st.fmean(ys)
            sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
            sy = math.sqrt(sum((y - my) ** 2 for y in ys))
            if sx <= 0 or sy <= 0:
                return float("nan")
            return sum((x - mx) * (y - my)
                       for x, y in zip(xs, ys)) / (sx * sy)

        mid = len(ent) // 2
        print(f"\n=== STRUCTURE vs SETTLEMENT ({len(ent)} entries) ===")
        print("  feature      meaning                        corr(won)  "
              "h1        h2       sign repeats?")
        MEAN = {
            "f_pos": "position in the round's own range",
            "f_dhi": "bps below the round's high",
            "f_dlo": "bps above the round's low",
            "f_rng": "round travel so far (bps)",
            "f_imp": "impulse: 60s range before entry",
            "f_rn1k": "distance to the nearest 1000 level",
            "f_rn100": "distance to the nearest 100 level",
            "f_mz": "margin over the strike (sigma left)",
        }
        for f in feats:
            xs = [e[f] for e in ent]
            ys = [e["y"] for e in ent]
            c = corr(xs, ys)
            c1 = corr(xs[:mid], ys[:mid])
            c2 = corr(xs[mid:], ys[mid:])
            same = (c1 > 0) == (c2 > 0) if (c1 == c1 and c2 == c2) else False
            print(f"  {f:<12} {MEAN[f]:<30} {c:+7.3f}   {c1:+.3f}   "
                  f"{c2:+.3f}   "
                  f"{'YES' if same else 'no'}")
        print("\n  -- the same features as TERCILE buckets (WR / EV per $1) --")
        for f in feats:
            xs = sorted(e[f] for e in ent)
            q1, q2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
            print(f"  {f:<12}", end="")
            for lo_, hi_ in ((-9e9, q1), (q1, q2), (q2, 9e9)):
                g = [e for e in ent if lo_ <= e[f] < hi_]
                if len(g) < 5:
                    print(f"   n={len(g)} -", end="")
                    continue
                wr = st.fmean([e["y"] for e in g])
                print(f"   n={len(g):<4} WR {wr:5.1f}% EV "
                      f"{st.fmean([e['ret'] for e in g]):+.4f}", end="")
            print()
        print("  (A feature with corr ~0 in BOTH halves cannot drive an exit: "
              "it does not say which way the round settles.)")
        return

    if args.mode == "predict":
        # ============ THE FORMULA, AND WHAT IT ACTUALLY REQUIRES ============
        # A binary position pays 1 with probability p and 0 otherwise, so it is
        # worth EXACTLY p. Selling realises the bid b. The whole problem is
        # therefore one comparison and there is no other optimal rule:
        #         SELL iff b > p            HOLD iff p > b
        # Every "exit strategy" anyone writes is an attempt to evaluate that
        # inequality. So an exit beats holding iff OUR p PREDICTS THE SETTLEMENT
        # BETTER THAN THE MARKET'S OWN BID DOES. If it does not, then `b > p` is
        # noise around `b > b`, which is never true, and every exit does is add
        # a round trip through the spread. That is a measurable claim, and this
        # mode measures the two halves of it:
        #   (1) WHO PREDICTS BETTER -- Brier / log-loss of the market bid against
        #       the model fair, on the SAME ticks, against the same settlements.
        #   (2) WHERE THE INEQUALITY HOLDS -- the time profile of E[b - outcome]
        #       after the entry. Where it is positive, selling is +EV, and the
        #       size of it is the most an exit could ever earn per share.
        ent = run2()
        # ---- (1) calibration on every recorded decision tick ----------------
        q_by_slug: dict[str, dict[int, tuple]] = {}
        # ⛔ SCOPED TO THE FAMILY AND THE ERA THE REST OF THE RUN USES. This
        # query was unscoped, so it pulled quotes from EVERY venue era the tape
        # contains while the entry replay honoured --since-ms: the bid-vs-fair
        # comparison above was then partly about a different venue's book than
        # the one the exits are priced in.
        for slug, ts, ub, db, w in con.execute(
                "SELECT q.slug, q.ts_ms, q.up_bid, q.down_bid, r.winner "
                "FROM quotes q JOIN rounds r ON r.slug=q.slug "
                "WHERE q.up_bid IS NOT NULL AND r.winner IN ('Up','Down') "
                "AND q.slug LIKE ? AND r.start_ms >= ?",
                (f"{args.family}-%", args.since_ms)):
            q_by_slug.setdefault(slug, {})[ts] = (ub, db, 1.0 if w == "Up" else 0.0)
        wins: dict[str, float] = {}
        for slug, y in con.execute(
                "SELECT r.slug, r.winner FROM rounds r "
                "WHERE r.winner IN ('Up','Down') AND r.slug LIKE ? "
                "AND r.start_ms >= ?", (f"{args.family}-%", args.since_ms)):
            wins[slug] = 1.0 if y == "Up" else 0.0
        rows = con.execute(
            "SELECT d.slug, d.ts_ms, d.side, d.fair FROM decisions d "
            "JOIN rounds r ON r.slug=d.slug "
            "WHERE d.side IN ('Up','Down') AND d.fair IS NOT NULL "
            "AND d.slug LIKE ? AND r.start_ms >= ?",
            (f"{args.family}-%", args.since_ms)).fetchall()
        brier_bid, brier_fair, brier_flat, n1 = 0.0, 0.0, 0.0, 0
        ll_bid, ll_fair = 0.0, 0.0
        for slug, ts, side, fair in rows:
            t = q_by_slug.get(slug)
            if not t or slug not in wins:
                continue
            hit = [k for k in t if abs(k - ts) <= 1000]
            if not hit:
                continue
            q = t[min(hit, key=lambda k: abs(k - ts))]
            y = wins[slug]
            p_up_mkt = q[0]
            p_up_md = float(fair) if side == "Up" else 1.0 - float(fair)
            for p_up, key in ((p_up_mkt, "bid"), (p_up_md, "fair")):
                p = p_up if y == 1.0 else 1.0 - p_up
                p = min(1 - 1e-9, max(1e-9, p))
                if key == "bid":
                    brier_bid += (p_up - y) ** 2
                    ll_bid += -math.log(p)
                else:
                    brier_fair += (p_up - y) ** 2
                    ll_fair += -math.log(p)
            brier_flat += (0.5 - y) ** 2
            n1 += 1
        if n1:
            print(f"\n=== (1) WHO PREDICTS THE SETTLEMENT BETTER ({n1} ticks, "
                  f"{args.family or 'all families'}, "
                  f"tau [{settings.min_tau_s:g},{settings.max_tau_s:g}]) ===")
            print(f"  {'predictor':<26} {'Brier':>8} {'log-loss':>9}")
            print(f"  {'market bid (up_bid)':<26} {brier_bid/n1:8.4f} "
                  f"{ll_bid/n1:9.4f}")
            print(f"  {'model fair':<26} {brier_fair/n1:8.4f} "
                  f"{ll_fair/n1:9.4f}")
            print(f"  {'constant 0.5 (no info)':<26} {brier_flat/n1:8.4f} "
                  f"{'--':>9}")
            print("  LOWER IS BETTER. If the model's Brier is not below the "
                  "bid's, then `b > p` cannot beat `b > b`.")

        # ---- (2) the sell edge E[b - p] by time left ------------------------
        by_slug = {r["slug"]: r for r in rs}
        bids: dict[str, dict[int, tuple]] = {}
        for e in ent:
            if e["slug"] not in bids:
                bids[e["slug"]] = {
                    t: (ub, db) for t, ub, db in con.execute(
                        "SELECT ts_ms, up_bid, down_bid FROM quotes "
                        "WHERE slug=? AND up_bid IS NOT NULL", (e["slug"],))}
        bins: dict[int, list[float]] = {}
        entry_edges: list[float] = []
        for e in ent:
            side = e["side"]
            y = 1.0 if e["won"] else 0.0
            bmap = bids.get(e["slug"]) or {}
            for ts, tau, _sp, *_r in by_slug[e["slug"]]["rows"]:
                if tau is None or tau <= 60 or ts <= e["ts_ms"]:
                    continue
                b = bmap.get(ts)
                if not b:
                    continue
                bb = b[0] if side == "Up" else b[1]
                if bb is None:
                    continue
                bins.setdefault(int(tau) // 20 * 20, []).append(bb - y)
            b0 = bmap.get(e["ts_ms"])
            if b0:
                entry_edges.append((b0[0] if side == "Up" else b0[1]) - y)
        print(f"\n=== (2) THE SELL EDGE E[bid - outcome] by seconds LEFT "
              f"({len(ent)} entries) ===")
        print("  tau (s)      n      E[bid - p]   +slip&fee  verdict")
        # `SLIP` lives in the exit branch; this one carries its own cost line.
        TCOST = 0.01 + 0.005      # a tick of slippage + ~50bp of fee
        for k in sorted(bins, reverse=True):
            v = bins[k]
            if len(v) < 200:
                continue
            m = st.fmean(v)
            print(f"  {k:3d}-{k+19:3d}  {len(v):>6}     {m:+.4f}     "
                  f"{m - TCOST:+.4f}    "
                  f"{'SELL is +EV' if m > TCOST else 'hold'}")
        if entry_edges:
            print(f"  AT ENTRY     {len(entry_edges):>6}     "
                  f"{st.fmean(entry_edges):+.4f}  <- the entry edge, run "
                  f"backwards: negative because we buy at the ask")
        print("  Read it as: E[bid - p] > tick+fee is the ONLY condition under "              "which selling earns anything, and the number IS the most an "
              "exit can make per share. If every row is below the cost line, "
              "no formula can beat holding on this tape.")
        return

    if args.mode == "react":
        # ========== THE RULE, TESTED AT SCALE ON THE BINANCE VENUE ==========
        # "The price reacts and goes on to reverse against our side -> exit."
        #
        # ⛔ THIS DOES NOT NEED THE ARM'S ENTRIES, WHICH IS THE WHOLE POINT. On
        # the strict Binance window the arm fires ~13 times in 2 days, so no exit
        # rule can be RANKED there. But the question the rule asks is not about
        # the arm: for any tick, holding the side that is ahead of the strike
        # pays `outcome` (1 if that side settles in the money), and selling pays
        # the BID. So
        #         d = bid - outcome        E[d] = the sell edge per share
        # and selling beats holding iff E[d] exceeds the round-trip cost. That is
        # estimable on EVERY Binance-era tick in the window -- thousands of
        # samples over hundreds of rounds instead of 13 entries -- and if E[d] is
        # below the cost line in the state the rule fires in, the rule cannot
        # save a loss no matter how the arm's entries happen to fall.
        #
        # THE STATE IS THE USER'S TRIGGER. `lead` is how far the side is ahead of
        # the strike in bps; `mv` is the spot's move over the last `look_s`,
        # signed so that positive means "moving further into the money for us".
        # A REACTION is `lead > 0` (we are ahead) with `mv` negative (it has
        # turned against us). `E[d | reaction]` is therefore exactly the number
        # that decides the rule, and the mirror bucket (`mv > 0`) is the control.
        TCOST = settings.cross_exit_slip + 0.005 if hasattr(
            settings, "cross_exit_slip") else 0.015
        # ONE PASS, then every threshold is free. Each tick becomes
        # (round, ts, tau, side, lead_bps, mv_bps, bid, outcome).
        ticks: list[tuple] = []
        for r in rs:
            y_up = 1.0 if r["winner"] == "Up" else 0.0
            strike = r["strike"]
            base = [(t, s) for t, _tau, s, *_x in r["rows"] if s]
            bm = {t: (ub, db) for t, ub, db in con.execute(
                "SELECT ts_ms, up_bid, down_bid FROM quotes "
                "WHERE slug=? AND up_bid IS NOT NULL", (r["slug"],))}
            for ts, tau, spot, _up, _dn, _ua, _da in r["rows"]:
                if spot is None or tau is None or not (60 <= tau <= 210):
                    continue
                raw = math.log(spot / strike) * 1e4
                if abs(raw) < 1e-9:
                    continue
                side = "Up" if raw > 0 else "Down"
                y = y_up if side == "Up" else 1.0 - y_up
                b = bm.get(ts)
                if not b:
                    continue
                bid = b[0] if side == "Up" else b[1]
                if bid is None:
                    continue
                # mv for several lookbacks at once, so --look-s can be swept
                mvs = []
                for look in (10.0, 20.0, 40.0):
                    past = [s for t, s in base
                            if ts - look * 1000 <= t <= ts]
                    if len(past) < 3:
                        mvs.append(None)
                        continue
                    v = math.log(spot / past[0]) * 1e4
                    mvs.append(v if side == "Up" else -v)
                ticks.append((r["slug"], ts, tau, side, abs(raw), bid, y, mvs))
        if not ticks:
            print("  no usable ticks")
            return
        print(f"\n=== SELL EDGE BY STATE — {args.family or 'all'}, Binance era, "
              f"tau [60,210] ===")
        print(f"  {len(ticks)} ticks over {len(rs)} rounds. `d = bid - "
              f"outcome`, so E[d] IS the sell edge per share.")

        def show(label, key_fn, tick_sel=None):
            v = [t for t in ticks if tick_sel is None or tick_sel(t)]
            sel = [t for t in v if key_fn(t)]
            if len(sel) < 30:
                return
            dd = [t[5] - t[6] for t in sel]              # bid - outcome
            # ROUND-CLUSTERED t: ticks of one round are not independent, and on a
            # 2-day sample clustering by DAY would leave one observation.
            by_round: dict[str, list[float]] = {}
            for t in sel:
                by_round.setdefault(t[0], []).append(t[5] - t[6])
            per_round = [st.fmean(x) for x in by_round.values()]
            tt = ao.tstat(per_round)
            print(f"  {label:<34} n={len(sel):<6} rounds={len(per_round):<4} "
                  f"E[d] {st.fmean(dd):+.4f}  round-t {tt:+5.2f}  "
                  f"bid {st.fmean([t[5] for t in sel]):.4f}  "
                  f"win {st.fmean([t[6] for t in sel]):.4f}  "
                  f"edge-slip+ .005 {st.fmean(dd)-0.010:+.4f} "
                  f"| .010 {st.fmean(dd)-0.015:+.4f} "
                  f"| .020 {st.fmean(dd)-0.025:+.4f}")

        show("all in-window ticks", lambda t: True)
        for li, look in enumerate((10.0, 20.0, 40.0)):
            for thr in (2.0, 3.0, 5.0, 8.0):
                show(f"REACTION look{look:g}s >= {thr:g}bps",
                     lambda t, li=li, thr=thr: (
                         t[7][li] is not None and t[7][li] <= -thr),
                     tick_sel=None)
            show(f"mirror  look{look:g}s <= -3bps",
                 lambda t, li=li: (t[7][li] is not None and t[7][li] >= 3.0))
        print(f"  cost line: the columns subtract slip 5/10/20 mills plus a "
              f"~5bp fee on ~0.74, i.e. 0.010 / 0.015 / 0.025.")
        print("  READ THE `win` COLUMN AGAINST `bid`: where bid > win the venue "
              "is UNDERPAYING our side and selling is the profit. Where bid < "
              "win the reaction is already priced and exiting only pays the "
              "spread. This is the whole comparison, and it needs no entries.")
        return

    if args.mode == "stakeband":
        # ⭐ WHAT THE SIZING BAND ACTUALLY DOES. `_stake_for` calls
        # `stake_for_edge(ev_per_dollar, min_edge=stake_ev_lo,
        # min_stake=min_stake_usdt, max_stake=max_stake_usdt,
        # edge_at_max=stake_ev_hi)` -- so the stake is a FUNCTION of the
        # decision-time ev/$, which `enter()` stores per entry. Sizing changes
        # no entry, only its weight, so the entry set is held fixed and the
        # comparison is purely about allocation.
        # ⛔ TWO COLUMNS, AND THE SECOND IS THE ONE THAT DECIDES. A bigger
        # average stake makes more money on a profitable arm BY CONSTRUCTION --
        # that is leverage, not allocation. The equal-capital column rescales
        # every variant to the same total staked as flat $2, so a difference
        # there is the sizing rule RANKING the trades, which is the only claim
        # worth making. (Same discipline as `--mode size`.)
        ent = [e for e in run2() if e.get("ev") is not None]
        ent.sort(key=lambda e: e["day"])
        n = len(ent)
        if n < 8:
            print(f"\n=== SIZING BAND: {n} entries, too few to read ===")
            return
        FLAT = 2.0
        print(f"\n=== THE SIZING BAND ({n} entries over {days:.2f} days, "
              f"decision-time ev/$) ===")
        evs = sorted(e["ev"] for e in ent)
        print("  ev/$ percentiles: " + "  ".join(
            f"p{q}={evs[min(n - 1, q * n // 100)]:.4f}"
            for q in (0, 10, 25, 50, 75, 90, 100)))
        # THE PREMISE, TESTED DIRECTLY: the band puts more money on higher ev/$,
        # so it only helps if ev/$ RANKS the per-$1 outcome. This is the table
        # that can refute it, printed before the money columns.
        print("  -- does ev/$ rank the outcome? (per-$1 return by ev bucket) --")
        for lo, hi in ((0.0, 0.05), (0.05, 0.06), (0.06, 0.07), (0.07, 9.0)):
            g = [e["ret"] for e in ent if lo <= e["ev"] < hi]
            if len(g) < 5:
                print(f"     ev {lo:.2f}-{hi:.2f}  n={len(g):<4} (too few)")
                continue
            print(f"     ev {lo:.2f}-{hi:.2f}  n={len(g):<4} "
                  f"mean {st.fmean(g):+.4f}/$1  "
                  f"win {100*sum(1 for r in g if r > 0)/len(g):5.1f}%")
        variants = (
            ("flat $2  (was live)", (0.04, 0.04, 2.0, 2.0)),
            # ⛔ THE SHIPPED CONFIG, READ FROM SETTINGS INSIDE THE TOOL. Ranking
            # a hardcoded default here would rank a system nobody runs -- and
            # this very table was read once against hi=0.07 while the live value
            # was 0.45 (see config.stake_ev_hi). Same failure as the
            # `--look-s 2.0` trap: read the live value, never the default.
            (f"SHIPPED ${settings.min_stake_usdt:g}-"
             f"{settings.max_stake_usdt:g} @ {settings.stake_ev_lo:.2f}-"
             f"{settings.stake_ev_hi:.2f}",
             (settings.stake_ev_lo, settings.stake_ev_hi,
              settings.min_stake_usdt, settings.max_stake_usdt)),
            ("$1-3 @ .15-.45", (0.15, 0.45, 1.0, 3.0)),
            ("$1-3 @ .04-.05", (0.04, 0.05, 1.0, 3.0)),
            ("$1-3 @ .04-.07", (0.04, 0.07, 1.0, 3.0)),
            ("$1-3 @ .04-.10", (0.04, 0.10, 1.0, 3.0)),
            ("$1-3 @ .04-.15", (0.04, 0.15, 1.0, 3.0)),
            ("$1-3 @ .04-.20", (0.04, 0.20, 1.0, 3.0)),
        )
        print(f"\n  {'variant':<26} {'avg$':>5} {'P&L':>8} {'$/day':>7} "
              f"{'maxDD':>8} {'streak':>6} | {'EQ-cap P&L':>10} {'EQ $/day':>8} "
              f"{'EQ maxDD':>8} {'h1':>7} {'h2':>7}")
        for label, band in variants:
            lo, hi, smin, smax = band
            stk = [stake_for_edge(e["ev"], min_edge=lo, min_stake=smin,
                                  max_stake=smax, edge_at_max=hi)
                   for e in ent]
            avg = st.fmean(stk)
            pnl = sum(s * e["ret"] for s, e in zip(stk, ent))
            eq = peak = dd = 0.0
            streak = worst_streak = 0
            for s, e in zip(stk, ent):
                eq += s * e["ret"]
                peak = max(peak, eq)
                dd = min(dd, eq - peak)
                streak = streak + 1 if e["ret"] < 0 else 0
                worst_streak = max(worst_streak, streak)
            # EQUAL CAPITAL: rescale so the total staked equals flat $2 x n.
            k = FLAT / avg
            pnl_e = pnl * k
            eq2 = peak2 = dd2 = 0.0
            for s, e in zip(stk, ent):
                eq2 += k * s * e["ret"]
                peak2 = max(peak2, eq2)
                dd2 = min(dd2, eq2 - peak2)
            mid = n // 2
            h1 = k * sum(s * e["ret"] for s, e in zip(stk[:mid], ent[:mid]))
            h2 = k * sum(s * e["ret"] for s, e in zip(stk[mid:], ent[mid:]))
            print(f"  {label:<26} {avg:5.2f} {pnl:+8.2f} {pnl/days:+7.2f} "
                  f"{dd:+8.2f} {worst_streak:>6} | {pnl_e:+10.2f} "
                  f"{pnl_e/days:+8.2f} {dd2:+8.2f} {h1:+7.2f} {h2:+7.2f}")
        print("  (EQ = every variant rescaled to flat $2's total capital. If a "
              "band does not beat flat THERE, its extra money is leverage, not "
              "a better allocation.)")
        return

    if args.mode == "size":
        cut = (settings.model_low_fair_cut if args.fair_cut is None
               else args.fair_cut)
        mult = args.weight_low_fair
        ent = [e for e in run2() if e.get("fair") is not None]
        ent.sort(key=lambda e: e["day"])
        n, mid = len(ent), len(ent) // 2
        wt = [mult if e["fair"] <= cut else 1.0 for e in ent]
        n_lo = sum(1 for e in ent if e["fair"] <= cut)
        print(f"\n=== SIZE-BY-FAIR: {mult:g}x on fair <= {cut:g} "
              f"({n_lo} of {n} entries) ===")
        print(f"  capital: flat ${n}  ->  weighted ${sum(wt):.0f} "
              f"({sum(wt)/n:.2f}x). Both columns below are scaled to the SAME "
              f"capital, so a difference is allocation, not leverage.")
        print(f"  {'':22} {'train':>9} {'test':>9} {'TOTAL':>9}")
        for lbl, ws in (("flat 1x", [1.0] * n),
                        (f"{mult:g}x <= {cut:g}", wt)):
            sc = n / sum(ws)
            tr = sc * sum(w * e["ret"] for w, e in zip(ws[:mid], ent[:mid]))
            te = sc * sum(w * e["ret"] for w, e in zip(ws[mid:], ent[mid:]))
            print(f"  {lbl:22} {tr:+9.2f} {te:+9.2f} {tr + te:+9.2f}")
        for lbl, ws in (("flat 1x", [1.0] * n),
                        (f"{mult:g}x <= {cut:g}", wt)):
            cap = n / sum(ws)                      # equal-capital scale factor
            peak = run = dd = 0.0
            for w, e in zip(ws, ent):
                run += cap * w * e["ret"]
                peak = max(peak, run)
                dd = min(dd, run - peak)
            print(f"  {lbl:22} max drawdown ${dd:+7.2f} on that capital, "
                  f"largest single entry ${cap * max(ws):.2f}")
        return

    if args.mode == "fair":
        # Is a `fair` cut a FILTER worth shipping, or noise? Two questions, and
        # they are not the same one: (a) does the LOW bucket rank above the
        # HIGH one in BOTH halves (a parameter must rank the same twice, or it
        # is a fitted split of one sample), and (b) what does the HIGH bucket
        # earn on its own -- if it is positive at a flat stake, skipping it
        # destroys money however good the per-dollar comparison looks.
        print("\n=== FAIR BUCKETS (the model's fair for the side bought) ===")
        ent = [e for e in run2() if e.get("fair") is not None]
        print("  both buckets are scored on their OWN ret, so `tot $` is what a "
              "flat $1 stake actually earned there.")
        for cut in (0.60, 0.65, 0.70, 0.75, 0.80):
            lo = [e for e in ent if e["fair"] <= cut]
            hi = [e for e in ent if e["fair"] > cut]
            for lab, g in ((f"fair <= {cut:.2f}", lo), (f"fair >  {cut:.2f}", hi)):
                if len(g) < 5:
                    print(f"  {lab:<14} n={len(g)} too few")
                    continue
                order = sorted(g, key=lambda e: e["day"])
                h = len(order) // 2
                print(f"  {lab:<14} n={len(g):<5} WR "
                      f"{100*sum(1 for e in g if e['won'])/len(g):5.1f}%  "
                      f"EV {st.fmean([e['ret'] for e in g]):+.4f}/$1  "
                      f"t {ao.tstat([e['ret'] for e in g]):+5.2f}  "
                      f"H {st.fmean([e['ret'] for e in order[:h]]):+.3f}/"
                      f"{st.fmean([e['ret'] for e in order[h:]]):+.3f}  "
                      f"dDay {drop_best_day(g):+.4f}  "
                      f"tot ${sum(e['ret'] for e in g):+7.2f}")
            print()
        return

    print("\n=== BASELINE (live knobs) ===")
    row("live", run2(), len(rs), days)
    if args.dump:
        kept = run2()[:args.dump]
        print("  first entries: slug / tau / side / px / why")
        for e in kept:
            print(f"    {e['slug']} tau {e['tau']:.0f} {e['side']:<5} "
                  f"px {e['px']:.3f} won {e['won']} | {e['why']}")

    if args.mode in ("all", "ofat"):
        print("\n=== OFAT: max_tau (round window; live 210) ===")
        for v in (210.0, 240.0, 270.0, 300.0):
            row(f"max_tau {v:g}", run2(max_tau=v), len(rs), days)

        print("\n=== OFAT: min_quote (live 0.45) ===")
        for v in (0.45, 0.35, 0.25, 0.15, 0.05):
            row(f"min_quote {v:g}", run2(min_quote=v), len(rs), days)

        print("\n=== OFAT: max_quote (live 0.80) ===")
        for v in (0.80, 0.90, 0.95):
            row(f"max_quote {v:g}", run2(max_quote=v), len(rs), days)

        print("\n=== OFAT: model_min_round_range_bps (live 5.0) ===")
        for v in (5.0, 3.0, 2.0, 0.0):
            row(f"min_range {v:g}", run2(min_range=v), len(rs), days)

        print("\n=== OFAT: model_max_round_range_bps (live 12.0) ===")
        for v in (12.0, 20.0, 0.0):
            row(f"max_range {v:g}", run2(max_range=v), len(rs), days)

        print("\n=== OFAT: min_tau (live 60) ===")
        for v in (60.0, 30.0, 5.0):
            row(f"min_tau {v:g}", run2(min_tau=v), len(rs), days)

    if args.mode in ("all", "joint"):
        print("\n=== JOINT: open the round-open region (tau window x travel) ===")
        for mt in (210.0, 270.0, 300.0):
            for (lo, hi) in ((5.0, 12.0), (5.0, 0.0), (0.0, 0.0)):
                row(f"tau<={mt:g} range[{lo:g},{hi:g}]",
                    run2(max_tau=mt, min_range=lo, max_range=hi), len(rs), days)

        print("\n=== JOINT: round-open region x quote band ===")
        for mq in (0.45, 0.25, 0.05):
            row(f"open, min_quote {mq:g}",
                run2(max_tau=300.0, min_range=0.0, max_range=0.0,
                     min_quote=mq), len(rs), days)

    if args.mode in ("all", "halves"):
        mid = len(rs) // 2
        cells = [
            ("live range[5,12] tau<=210", dict(max_tau=210.0, min_range=5.0, max_range=12.0)),
            ("max_range 20", dict(max_tau=210.0, min_range=5.0, max_range=20.0)),
            ("max_range 0", dict(max_tau=210.0, min_range=5.0, max_range=0.0)),
            ("min_tau 30", dict(max_tau=210.0, min_range=5.0, max_range=12.0, min_tau=30.0)),
            ("min_tau 5", dict(max_tau=210.0, min_range=5.0, max_range=12.0, min_tau=5.0)),
            ("min_range 0", dict(max_tau=210.0, min_range=0.0, max_range=0.0)),
            ("open region tau<=300", dict(max_tau=300.0, min_range=0.0, max_range=0.0)),
        ]
        for label, sub, sg_sub in (("FIRST half", rs[:mid], sigmas[:mid]),
                                   ("SECOND half", rs[mid:], sigmas[mid:])):
            print(f"\n=== {label} (direction check: the ranking must repeat) ===")
            for name, over in cells:
                kw = dict(LIVE)
                kw.update(over)
                out = []
                for r, sg in zip(sub, sg_sub):
                    e = replay(r, sg, **base, **kw)
                    if e:
                        out.append(e)
                row(name, out, len(sub), days / 2)


if __name__ == "__main__":
    main()
