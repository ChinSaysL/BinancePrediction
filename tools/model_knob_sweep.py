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
import bisect
import math
import sqlite3
import statistics as st
import sys
import time
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

#: CLI overrides for the exit's GRIND half (`model_exit_behind_bps` /
#: `model_exit_behind_hold_s`), read by `_ret_exited`. `None` = use the live
#: config. They exist because the grind is the ONE half whose exit COUNT moves
#: with the stake: it sells into a bid of ~0.36, so at $1-2 the venue's $1.00
#: floor refuses the sale and its cost stays latent, while from ~$2.5 it becomes
#: placeable and the money is really spent. Without an override a size grid
#: cannot separate "the stake is expensive" from "the grind is expensive once
#: the stake lets it fire".
BEHIND_OVR: dict = {"bps": None, "hold": None}

#: CLI overrides for the exit's TAU FLOOR, one per half
#: (`--min-tau` / `--min-tau-behind`). `None` = the live config. The floor is
#: what DISARMS the exit below it, and the two halves do not deserve the same
#: one: the live 60s was measured on the LEAD half (a still-ahead position is
#: underpriced by the venue late, so selling it there destroys value), while the
#: LOSS half's alternative is zero. `0` = armed to the bell.
TAU_OVR: dict = {"react": None, "behind": None}

#: CLI override for the reaction THRESHOLD (`--rev-bps`, i.e.
#: `model_exit_rev_bps`), read by `_ret_exited`. `None` = live config. This is
#: the knob that decides how big a move against us counts as "the lead is being
#: snapped away", and it is the ONLY lever that fires while we are still AHEAD
#: (the grind fires after the lead is already gone).
REV_OVR: dict = {"rev": None}

#: `--exit-max-bid`: REFUSE to sell while the recorded bid is ABOVE this.
#: `None`/0 = off (sell at any bid, the shipped behaviour).
#: WHY IT EXISTS. Live 2026-09-23 11:29:43, `btc-updown-5m-1790162700`: the sell
#: fired off a +19-point BUMP inside a red candle -- spot 85497 -> 85516, still 57
#: points BELOW an 85573.6 strike, 6.73bps in the money with tau 16s, i.e. ~4.2
#: sigma of the remaining move (sigma*sqrt(16/60) ~= 1.6bps) and P(win) ~ 0.9999.
#: The bid there was 0.93-0.95, so the rule was giving away 5c of a near-certain
#: winner because a 5.3bps/20s threshold does not scale with how far AHEAD it is.
#: The bid IS the market's read of how done the round is, so this gate is a
#: statement about the price we would be surrendering, not about the spot.
MAX_BID_OVR: dict = {"bid": None}

#: `--exit-max-z`: REFUSE to sell while the position is more than this many
#: sigma of the REMAINING move in the money (`|ln(S/K)| / (sigma*sqrt(tau/60))`).
#: `None`/0 = off. WHY IT EXISTS, and why it is NOT the same gate as
#: `--exit-max-bid`: the reaction rule fires on an ABSOLUTE 20s move (5bps),
#: which says nothing about how far ahead we are. Live 2026-09-23 11:29:43 the
#: spot sat 6.73bps ahead with tau 16s -- 2.4 sigma by the venue's own implied
#: vol, ~4.2 sigma by realised -- and a +19-point bump inside a red candle
#: fired the exit on a position that was (by its own sigma) ~99% won. A gate on
#: the bid is a statement about the price; a gate on z is a statement about
#: whether the move is a THREAT, which is what the rule claims to measure.
MAX_Z_OVR: dict = {"z": None}

#: `--exit-min-prem`: refuse a sale unless the book is still paying MORE than the
#: position is worth (`bid - slip >= fair + prem`). 0 = off (sell at any price).
#: ⭐ THE POINT, and it is the repo's own edge run in reverse. The arm's profit
#: comes from the venue's quote LAGGING the spot; the same lag decides when an
#: EXIT is paid. Selling at/below the fair value is handing over the spread to
#: convert a coin flip into a coin flip -- which is why every grind threshold
#: measured 2026-09-23 lost money (behind 1bp: -$15 to -$44 for $1-9 of
#: drawdown). Selling while the book is still ABOVE fair captures the lag instead
#: of paying for it, and by construction it cannot fire on a round the market has
#: already priced out.
PREM_OVR: dict = {"prem": None}

#: `--behind-max-tau T`: fire the GRIND half only when `tau <= T`. 0/None = no
#: ceiling (`None` = read `settings.model_exit_behind_max_tau_s`, the SHIPPED
#: value -- a literal default here would rank a system nobody runs).
#: ⭐ WHY A CEILING AND NOT JUST A THRESHOLD. The operator's constraint
#: (2026-09-23) is "no full loss", and the rule that satisfies it must not touch
#: winners. Near the bell the two states are separable: a position that will WIN
#: is AHEAD of the strike, one that will LOSE is BEHIND it -- and a deficit is
#: only decisive late (σ_1m ~4bps, so 2bps behind is 1.25σ with 10s left but
#: 0.5σ with 60s). A late grind therefore sells the about-to-lose cohort at
#: 0.2-0.4 (capping the loss near -60..-80% instead of -100%) while leaving
#: every winner alone. The early grind has no such separation: at tau 200 a
#: 2bps deficit is noise, so it fires on rounds that recover and pays the spread
#: for nothing -- which is exactly what the measured costs showed.
BMAX_OVR: dict = {"tau": None}

#: `--dump-rows`: print the per-trade sequence (one `#ROW` line each) instead of
#: holding it inside one report. It exists for the COMPOUNDING audit: the rows
#: carry PER-$1 RETURNS, not dollars, so a sizing scheme (5% of the bankroll,
#: say) can be applied to the same trades afterwards without a re-replay -- the
#: replay is linear in the stake, so only the money needs re-deriving. Both
#: paths are dumped (exit ON and holding to the bell) and they are the SAME
#: trades, which is what makes their difference the exit rather than a
#: different sample.
DUMP_ROWS: dict = {"on": False}

#: ⛔ TRIGGER vs TAKE, per slug: `"<rule>:<outcome>"` for the LAST time the exit
#: rule fired, where outcome is `taken` / `floor` (the venue's SELL minimum) /
#: `no_bid` / `max_bid`. `_ret_exited` returns ONE boolean, and that boolean
#: conflates two very different failures: a rule that never fires (nothing to
#: protect) and a rule that fires but cannot sell. "Does the exit cut our
#: losses?" is a question about the FIRST, and the boolean cannot answer it.
EXIT_TRIG: dict = {}

# ============================================================================
# LIVE PARITY. Added 2026-09-23, after measuring the replay against the live
# fills. Three things the tool did NOT mirror, each of which makes the replay a
# DIFFERENT RULE from the one on the box:
#
#  1. THE WEEKEND OVERRIDES. Live reads every model knob through `Bot._wk`,
#     which swaps in `settings.weekend_<name>` on Saturday/Sunday UTC. The live
#     values are not small: max_quote 0.70 -> 0.85, min_edge 0.04 -> 0.06,
#     edge_haircut 0.5 -> 1.0, persistence 2 -> 3, max_tau 210 -> 240. So every
#     weekend round in the tape was being replayed under a rule the bot does not
#     run, on ~2 days of 10.
#  2. THE CANDLE-MINUTE STREAK RESET. `main.Bot._record` pops the persistence
#     streak whenever `now_ms // 60_000` changes -- unconditionally, NOT only
#     when `trade_on_candle_open` is set (it is off). A signal straddling a
#     minute boundary therefore restarts from 1 live; without the reset the
#     replay granted every persistence value above 1 a little extra.
#  3. THE UP-REGIME GATE (`up_regime_gate`, live 0.01). When the last hour's
#     return exceeds it, live removes the Up candidate BEFORE the side pick, so
#     a Down candidate on the same quote still stands.
#
# `--no-weekend`, `--no-candle-reset` and `--no-up-gate` restore the old
# behaviour, so any figure quoted before this date can be reproduced.
#
# ⛔ AND WHAT THE LIVE RECORD IS NOT. The fills in `fills` (mode='live') span
# DOZENS of configurations -- min_quote, the crossings gate, the exit rule and
# stakes from $0.98 to $10 all changed inside the same window. They are a
# MIXTURE of rules, so they CANNOT be used to validate this replay, and a
# disagreement with them is not a defect in the model. The replay's reference is
# the CURRENT config applied to the raw tape (quotes, bids, asks, spot, strikes,
# winners -- all config-independent). The two live-derived knobs (`--fee-model
# live`, `--entry-slip`) are VENUE-EXECUTION measurements: what the book does to
# an order, not what the arm's entries were. They are opt-in and off by default.
# ============================================================================


def _is_weekend_utc(ts_ms: int) -> bool:
    """Saturday/Sunday UTC -- the same cohort `Bot._wk` uses.

    UTC because the round slugs and every measurement behind the weekend knobs
    are UTC; a local-time weekend would shift the cohort by hours and score a
    different set of rounds than the one that was measured.
    """
    return time.gmtime(ts_ms / 1000.0).tm_wday >= 5


def _wk_live(name: str, ts_ms: int, fallback):
    """`Bot._wk` for ONE tick: the weekend value on a weekend, else `fallback`.

    Mirror of the live lookup, including the `getattr(..., None)` guard: only a
    knob that HAS a `weekend_` twin switches, so a knob with no override keeps
    its weekday value rather than becoming None.
    """
    if getattr(settings, "weekend_overrides", False) and _is_weekend_utc(ts_ms):
        v = getattr(settings, f"weekend_{name}", None)
        if v is not None:
            return v
    return fallback


#: THE FEE RATE THE CASH LEGS ARE CHARGED (hook, not a constant).
#:
#: `fee_rate_for_price` is the rate the arm's own EV and hurdle are built from,
#: and it is what the entry GATE must keep using -- change it here and the
#: replay's entry SET stops matching the bot's. But it is NOT what the venue
#: charges. Measured 2026-09-23 on 778 live `mode='live'` fills, charged bps of
#: notional against that model:
#:
#:     px band    n     zero-fee   measured bps   model bps   ratio
#:     0.00-0.50  153     22.9%        160.7        206.5     0.78
#:     0.50-0.60  143     28.0%        132.6        128.3     1.03
#:     0.60-0.70  194     24.7%         91.2         75.0     1.22
#:     0.70-0.80  195     16.9%         60.2         42.9     1.40
#:     0.80-0.90   85     27.1%         29.4         21.0     1.40
#:     0.90-1.01    8     25.0%         14.8          9.6     1.54
#:
#: So inside the traded band the model UNDERSTATES the realised charge by 20-54%.
#: `--fee-model live` replaces this hook with the measured band rate (computed
#: from the DB at run time), which is what the DOLLARS should be priced at. The
#: default keeps the tool's historical numbers reproducible.
_FEE_LIVE = False


def FEE_RATE(px: float) -> float:
    """The fee rate the CASH legs are charged (late-bound, see above)."""
    if _FEE_LIVE:
        return _measured_fee_rate(px)
    return fee_rate_for_price(px)


def _measured_fee_table(con, days: int = 30):
    """Fit the realised fee rate as a function of price, from the live fills.

    Bands rather than a fit: the realised rate is a step function on this venue
    (it charges a partial fill and not a complete one), and the mean per band is
    the honest expectation -- it INCLUDES the fills that paid nothing.
    """
    now = con.execute("select max(ts_ms) from decisions").fetchone()[0] or 0
    rows = con.execute(
        "select filled_px px, shares sh, stake, fee_paid fee from fills "
        "where mode='live' and filled_px>0 and shares>0 and stake>0 "
        "and ts_ms > ?", (now - days * 86400000,)).fetchall()
    bands = [(0.0, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
             (0.80, 0.90), (0.90, 1.01)]
    out: list[tuple[float, float, float, int]] = []
    for lo, hi in bands:
        g = [r for r in rows if lo <= r[0] < hi]
        if len(g) < 5:
            out.append((lo, hi, fee_rate_for_price((lo + hi) / 2.0), len(g)))
            continue
        out.append((lo, hi,
                    st.fmean([(r[3] / (r[1] * r[0])) for r in g]), len(g)))
    return out, len(rows)


def _measured_fee_rate(px: float) -> float:
    """The realised band rate for this price (set by `--fee-model live`)."""
    for lo, hi, rate, _n in _FEE_BANDS:
        if lo <= px < hi:
            return rate
    return fee_rate_for_price(px)


_FEE_BANDS: list[tuple[float, float, float, int]] = []


def up_gate_by_minute(closes: dict[int, float], gate: float) -> dict[int, bool]:
    """{minute index -> may we take the Up side?}, PRECOMPUTED for the tape.

    Mirrors `Bot._record`'s up-regime gate on top of `ao.one_hour_return`: the
    last hour's return is read from COMPLETED 1m bars (the current minute has
    not closed, and using it is a look-ahead of up to 59s on the exact quantity
    the gate keys on).

    Precomputed because the live gate is evaluated once per poll and the naive
    per-tick call is O(number of minutes) -- 1M ticks x 14k minutes is why the
    first version of this did not finish. The answer only changes when a minute
    closes, so the whole tape is swept once with bisect lookups.
    """
    if not closes or gate <= 0:
        return {}
    mins = sorted(closes)
    out: dict[int, bool] = {}
    for m in mins:
        newest = m - 1
        k = bisect.bisect_right(mins, newest) - 1
        if k < 0:
            continue
        r = bisect.bisect_right(mins, mins[k] - 60) - 1
        if r < 0:
            continue
        c0, c1 = closes[mins[k]], closes[mins[r]]
        if c0 > 0 and c1 > 0:
            out[m] = not (math.log(c0 / c1) > gate)
    return out

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


def _dd_detail(ent: list, rmap: dict, bmap: dict, stake: float, lo: float,
               hi: float, bankroll: float, ret_fn, n_boot: int = 2000,
               seed: int = 7) -> None:
    """HOW one cell's maxDD is built -- and how much of it is PATH LUCK.

    ⛔ A DRAWDOWN IS ONE PATH THROUGH ONE ORDERING OF THE SAME TRADES, and the
    grid prints a single peak-to-trough figure that reads like a property of the
    strategy. It is a property of the SEQUENCE. Three things decide whether it
    means anything, and the grid prints none of them:

    1. WHERE THE PEAK SAT. The grid divides the drop by the STARTING bankroll,
       but equity is `bankroll + cumulative`, so a drop that begins from a high
       peak leaves the wallet far above zero. **Survival is decided by the
       trough's LEVEL, not by the size of the drop.** A -$75 fall from a +$50
       peak ends at $55 on an $80 wallet -- uncomfortable, not fatal. The same
       -$75 from a +$5 peak ends at $10.
    2. THE ORDER. Same trades, different sequence, different drawdown: the
       reshuffle below keeps the multiset AND the total identical and moves the
       drawdown. If the measured figure sits in the middle of that spread it is
       not a measurement of the strategy at all.
    3. THE SAMPLE. This is ONE walk over one 15-day tape. The bootstrap of the
       RETURNS (with replacement) is a different question -- how far the figure
       moves when the trades themselves are resampled -- and is printed too.

    Read-only, but the numbers come from the same `_ret_exited` path as the
    grid (`ret_fn`, passed in because it is a closure of `main`), at a FLAT
    stake, so the walk is the grid's own.

    ⛔ AND THE WALK IS ALREADY EXIT-ADJUSTED -- `_ret_exited` is the exit, per
    entry, at that entry's own stake (both halves, the z gate, the tau floors,
    the venue floor checked against the SLICE ACTUALLY SENT, shares floored to
    0.01, a refused sale retried on the next tick, and the sale priced at
    `bid - 0.01`). A drawdown that ignored the exit would be a different
    strategy's drawdown, so the same path is walked a second time with the
    exit OFF (`e["ret"]`, hold to the bell) and both are printed.
    """
    import random

    # (ts, slug, stake, $ with exit, r with exit, exited, $ holding to the bell)
    rows: list[tuple[int, str, float, float, float, bool, float]] = []
    for e in ent:
        sk = stake_for_edge(float(e["ev"]), min_edge=lo, min_stake=stake,
                            max_stake=stake, edge_at_max=hi)
        if sk < settings.min_order_usdt:
            continue
        # ⛔ ASK THE INSTRUMENT WHETHER THE RULE EVER FIRED. `_ret_exited`'s
        # boolean is True only when a SALE RESULTED, so 57 "unexited"
        # catastrophes could be either "no rule fired" or "the rule fired and
        # the venue refused" -- and those call for opposite fixes.
        EXIT_TRIG.pop(e["slug"], None)
        r, did, _rule = ret_fn(e, rmap[e["slug"]], bmap.get(e["slug"], {}),
                               sk)
        rows.append((int(e["ts_ms"]), e["slug"], sk, sk * r, r, did,
                     sk * float(e.get("ret") or 0.0),
                     EXIT_TRIG.get(e["slug"], "")))
    rows.sort(key=lambda x: x[0])
    n = len(rows)
    if n < 8:
        print("  too few trades to read")
        return

    def walk(vals: list[float]) -> tuple[float, float, float]:
        """-> (maxDD, cumulative AT the peak it fell from, cumulative at low).

        ⛔ `peak_at` IS CAPTURED WHEN THE TROUGH IS RECORDED, not while walking.
        Tracking "the index/value of the highest cumulative so far" gives the
        ALL-TIME high, which on a profitable tape happens LATER than the trough
        -- the first version printed peak +41.75 with a trough of -18.88 and a
        maxDD of -26.32, three numbers that cannot all be true (peak - low must
        equal the drawdown). The wallet-at-trough and the maxDD were right; only
        the peak label was the final high instead of the one the fall started
        from.
        """
        cum = peak = dd = 0.0
        peak_at = low_at = 0.0
        for v in vals:
            cum += v
            if cum > peak:
                peak = cum
            if cum - peak < dd:
                dd, peak_at, low_at = cum - peak, peak, cum
        return dd, peak_at, low_at

    dollars = [x[3] for x in rows]
    dd, peak_at, low_at = walk(dollars)
    tot = sum(dollars)
    print(f"\n=== HOW THE maxDD IS BUILT -- flat ${stake:g}, {n} trades, "
          f"walked in fill order ===")
    print(f"  equity = ${bankroll:g} + cumulative dollars:")
    print(f"    total P&L      ${tot:+9.2f}")
    print(f"    peak           ${peak_at:+9.2f}   <- the peak the trough "
          f"fell FROM")
    print(f"    trough         ${low_at:+9.2f}")
    print(f"    maxDD          ${dd:+9.2f}   = trough - peak (this is the "
          f"grid's column)")
    print(f"  ⭐ WALLET AT THE TROUGH: ${bankroll + low_at:.2f} of ${bankroll:g} "
          f"({100 * (bankroll + low_at) / bankroll:.0f}%) -- survival is "
          f"decided HERE, not by ${abs(dd):.2f} of drop.")
    print(f"  the grid's '94% of the bankroll' style row is "
          f"{100 * abs(dd) / bankroll:.0f}% -- the DROP, which starts from "
          f"${peak_at:+0.2f}, not from $0.")

    # --- 1b. THE SAME PATH WITH THE EXIT OFF (is the exit in this number?) ---
    hold = [x[6] for x in rows]
    hdd, hpeak, hlow = walk(hold)
    nex = sum(1 for x in rows if x[5])
    print("\n  -- IS THE EXIT IN THIS NUMBER? same entries, same path, exit "
          "switch only --")
    print(f"     exit ON   (live)  total ${tot:+8.2f}  maxDD ${dd:+8.2f}  "
          f"wallet at trough ${bankroll + low_at:7.2f}  "
          f"exits {nex}/{n} ({100 * nex / n:.0f}%)")
    print(f"     exit OFF (hold)   total ${sum(hold):+8.2f}  maxDD ${hdd:+8.2f}  "
          f"wallet at trough ${bankroll + hlow:7.2f}")
    cut = abs(hdd) - abs(dd)
    print(f"     => the exit is worth ${tot - sum(hold):+.2f} of P&L and makes "
          f"the drawdown ${-cut:+.2f} "
          f"({'SHALLOWER' if cut > 0 else 'DEEPER'} by ${abs(cut):.2f}) on "
          f"this path")

    # --- 1c. WHY THE EXIT BARELY MOVES THE TAIL ----------------------------
    # ⛔ A DRAWDOWN IS NOT AN AVERAGE. The exit can be worth -$20 of P&L overall
    # and still shave the trough, or cost nothing there -- the only way to tell
    # is to price BOTH paths over the peak->trough window itself, and to split
    # the exits by what HOLDING would have done (a lead protected while it is
    # still ahead is a different trade from a loss cut after the strike broke).
    ex = [x for x in rows if x[5]]
    won_r = [x for x in ex if x[6] > 0]
    lost_r = [x for x in ex if x[6] <= 0]
    print(f"\n  -- WHAT THE {len(ex)} EXITS ACTUALLY SOLD "
          f"({100 * len(ex) / n:.0f}% of trades; the other "
          f"{n - len(ex)} are identical in both paths) --")
    print(f"     sold a WINNER (holding won)  {len(won_r):3d}  "
          f"the exit is ${sum(x[3] - x[6] for x in won_r):+8.2f} against "
          f"holding")
    print(f"     sold a LOSER  (holding lost) {len(lost_r):3d}  "
          f"the exit is ${sum(x[3] - x[6] for x in lost_r):+8.2f} for "
          f"holding")
    # ⛔ THE QUESTION THE TOTALS HIDE: the exit's job is to CUT A CATASTROPHE,
    # and a catastrophe is a -100% trade. Counting them separately answers
    # "does it cap the bad ones" (yes, per event) and "how many does it MISS"
    # (which is what leaves the drawdown where it is). Both are true at once,
    # and the totals above cannot distinguish them.
    fl = [x for x in rows if x[6] <= -0.99]
    fl_ex = [x for x in fl if x[5]]
    wn = [x for x in rows if x[6] > 0]
    wn_ex = [x for x in wn if x[5]]
    print(f"\n  -- THE CATASTROPHES (-100% trades), and what the exit did --")
    print(f"     holding loses the whole stake on {len(fl)}/{n} trades "
          f"({100 * len(fl) / n:.0f}%)")
    fl_tr = [x for x in fl if x[7]]
    from collections import Counter
    for lbl, grp in (("catastrophes", fl), ("winners", wn)):
        tr = [x for x in grp if x[7]]
        tk = [x for x in grp if x[5]]
        why = Counter(x[7].split(":")[-1] for x in tr)
        print(f"     {lbl:<12} n={len(grp):<4} rule fired {len(tr):3d} "
              f"({100 * len(tr) / max(1, len(grp)):3.0f}%), sale taken "
              f"{len(tk):3d} ({100 * len(tk) / max(1, len(grp)):3.0f}%)"
              f"   {dict(why)}")
    if fl_ex:
        rec = [x[3] - x[6] for x in fl_ex]
        print(f"     the exit CUT {len(fl_ex)} of them "
              f"({100 * len(fl_ex) / len(fl):.0f}%), recovering "
              f"${sum(rec):+.2f} (${st.fmean(rec):+.2f} each, i.e. a "
              f"-100% trade becomes "
              f"${st.fmean([x[3] / x[2] for x in fl_ex]) * 100:+.0f}% per $1)")
    print(f"     it MISSED {len(fl) - len(fl_ex)} "
          f"({100 * (len(fl) - len(fl_ex)) / len(fl):.0f}%) -- "
          f"{len(fl) - len(fl_tr)} of them never triggered at all, "
          f"{len(fl_tr) - len(fl_ex)} fired and could not sell. THAT is what "
          f"the drawdown is made of.")
    if wn_ex:
        print(f"     for balance: it also cut {len(wn_ex)} of the {len(wn)} "
              f"WINNERS ({100 * len(wn_ex) / len(wn):.0f}%), "
              f"${sum(x[3] - x[6] for x in wn_ex):+.2f}")

    # --- 1d. THE SAME CENSUS PER DAY ---------------------------------------
    # ⛔ A COVERAGE RATE POOLED OVER 15 DAYS CAN HIDE THE REGIME THAT MATTERS.
    # The entry set is chosen by the CURRENT config on every day, but the market
    # it is applied to is not the same, so a rule that covers every loss this
    # week and half of them three weeks ago reports 30% and reads as a property
    # of the rule. Split it before quoting it.
    byday: dict[int, list] = {}
    for x in rows:
        byday.setdefault(x[0] // 86400000, []).append(x)
    print(f"\n  -- BY DAY: the entry set, the losses, and the exit's coverage --")
    print(f"     {'day':<6} {'n':>4} {'-100%':>6} {'fired':>6} {'taken':>6} "
          f"{'cut%':>5} {'$ exit-on':>10} {'$ exit-off':>11}")
    for d in sorted(byday):
        g = byday[d]
        cl = [x for x in g if x[6] <= -0.99]
        tr = [x for x in cl if x[7]]
        tk = [x for x in cl if x[5]]
        print(f"     {time.strftime('%m-%d', time.gmtime(d * 86400)):<6} "
              f"{len(g):4d} {len(cl):6d} {len(tr):6d} {len(tk):6d} "
              f"{(100 * len(tk) / len(cl) if cl else float('nan')):5.0f} "
              f"{sum(x[3] for x in g):+10.2f} {sum(x[6] for x in g):+11.2f}")

    def _window(ser: list[float]) -> tuple[float, int, int]:
        """-> (maxDD, index of the peak it fell FROM, index of the trough).

        ⛔ THE PEAK INDEX MUST BE CAPTURED AT THE TROUGH, not tracked as a
        running max. `peak` keeps rising to the END of the tape, so "the index
        of the highest cumulative" is a LATER trade than the peak the trough
        fell from -- the first version of this printed a NEGATIVE-length window
        (09-23 -> 09-11, -210 trades, 0 exits), which is what it looks like
        when the worst stretch is asked for backwards.
        """
        cum = peak = dd = 0.0
        i_pk = i_at_trough = i_lw = 0
        for i, v in enumerate(ser):
            cum += v
            if cum > peak:
                peak, i_pk = cum, i
            if cum - peak < dd:
                dd, i_at_trough, i_lw = cum - peak, i_pk, i
        return dd, i_at_trough, i_lw

    _d, i_pk, i_lw = _window(dollars)
    sl = slice(i_pk, i_lw + 1)
    f = "%m-%d %H:%M"
    n_ex_w = sum(1 for x in rows[sl] if x[5])
    print(f"\n  -- THE WORST STRETCH ITSELF: "
          f"{time.strftime(f, time.gmtime(rows[i_pk][0] / 1000))} -> "
          f"{time.strftime(f, time.gmtime(rows[i_lw][0] / 1000))} "
          f"({i_lw - i_pk + 1} trades, {n_ex_w} exits) --")
    print(f"     exit ON   ${sum(dollars[sl]):+9.2f}")
    print(f"     exit OFF  ${sum(hold[sl]):+9.2f}")
    print(f"     => the exit {'barely touches' if abs(sum(dollars[sl]) - sum(hold[sl])) < 0.2 * abs(dd) else 'moves'} "
          f"the trough: "
          f"${sum(dollars[sl]) - sum(hold[sl]):+.2f} over "
          f"{i_lw - i_pk + 1} trades, against a ${abs(dd):.2f} drawdown.")


    # --- 2/3. the same trades in a different order, and resampled -----------
    # ⛔ BOTH PATHS, because the exit's whole claim is TAIL protection and a
    # single measured drawdown is one draw from that tail. A rule that shaves
    # the average and clips the tail can show a WORSE measured maxDD and still
    # be the reason the account survives, so the two distributions are printed
    # side by side rather than argued about.
    rnd = random.Random(seed)
    print(f"\n  -- THE SAME TRADES UNDER {n_boot} RESHUFFLES / RESAMPLES "
          f"(dollars) --")
    print(f"     {'path':<10} {'total':>9} {'maxDD':>8} {'wallet@low':>11} "
          f"{'shuffle p05/50/95':>22} {'pct':>5} {'resample p05/50/95':>22}")
    for lbl, ser in (("exit ON", dollars), ("exit OFF", hold)):
        _dd, _pk, _lw = walk(ser)
        sh: list[float] = []
        bs: list[float] = []
        for _ in range(n_boot):
            s = ser[:]
            rnd.shuffle(s)
            sh.append(-walk(s)[0])
            bs.append(-walk([rnd.choice(ser) for _ in range(n)])[0])
        sh.sort()
        bs.sort()
        pct = 100 * sum(1 for d in sh if d < -_dd) / n_boot
        print(f"     {lbl:<10} ${sum(ser):+8.2f} ${_dd:+7.2f} "
              f"${bankroll + _lw:10.2f} "
              f"${sh[int(.05 * n_boot)]:7.2f}/{sh[n_boot // 2]:7.2f}/"
              f"{sh[int(.95 * n_boot)]:7.2f} {pct:4.0f}% "
              f"${bs[int(.05 * n_boot)]:7.2f}/{bs[n_boot // 2]:7.2f}/"
              f"{bs[int(.95 * n_boot)]:7.2f}")
    print(f"     ('pct' = where the measured maxDD sits among its own "
          f"reshuffles: 50% = a typical drawdown for these trades, 95% = a "
          f"lucky one.")
    print(f"      'wallet@low' = ${bankroll:g} + the lowest cumulative -- the "
          f"survival number. 'total' is identical across every reshuffle.)")

    # --- the shape of the bad part -----------------------------------------
    worst_run, run = 0.0, 0.0
    streak = worst_streak = 0
    for v in dollars:
        run = min(run + v, v) if v < 0 else 0.0
        worst_run = min(worst_run, run)
        streak = streak + 1 if v < 0 else 0
        worst_streak = max(worst_streak, streak)
    print(f"  worst consecutive LOSS RUN ${worst_run:+.2f} "
          f"(longest losing streak {worst_streak} trades)")
    print("  the 5 worst trades:")
    # ⛔ EIGHT FIELDS, NOT SEVEN. The row gained the trigger string
    # (`EXIT_TRIG`) after this loop was written, and the 7-name unpack then
    # raised `ValueError: too many values to unpack` AT THE END of every
    # `--dd-detail` run -- silently, because the report's own sections print
    # first and a piped grep returns 0 whether the tail crashed or not. Do not
    # shorten this to `_tr` removal: the tuple is (ts, slug, stake, $on, r_on,
    # did, $off, trig).
    for ts, slug, _sk, dl, r, did, _dh, _tr in sorted(rows,
                                                      key=lambda x: x[3])[:5]:
        day = time.strftime("%m-%d %H:%M", time.gmtime(ts / 1000))
        print(f"    {day} {slug:26s} ${dl:+7.2f} ({r:+.3f}/$1)"
              f"{'  exited' if did else ''}")

    # --- the per-trade sequence, for the sizing / compounding audits -------
    if DUMP_ROWS["on"]:
        print("#ROWS day ts_ms r_on r_off exited rule")
        for ts, slug, sk, _dl, r, did, dh, _tr in rows:
            print(f"#ROW {ts // 86400000} {ts} {r:.6f} "
                  f"{(dh / sk if sk else 0.0):.6f} {int(bool(did))} "
                  f"{EXIT_TRIG.get(slug, '') or '-'}")

if Path(ao.__file__).resolve().parent != Path(__file__).resolve().parent:
    raise SystemExit(
        f"arm_overlap loaded from {ao.__file__}, not from this tool's own "
        f"directory {Path(__file__).resolve().parent} -- a stray copy is "
        f"shadowing it; delete the stray, do not edit around it")
from btcpredict.config import settings                        # noqa: E402
from btcpredict.edge.signal import (                          # noqa: E402
    FeeModel, evaluate, evaluate_hybrid, fee_rate_for_price, stake_for_edge,
    tilt_factor)
from btcpredict.pricing.digital import fair_value, moneyness_sd  # noqa: E402

# The live gate values, read from config rather than typed here. A sweep that
# pins its own copy ranks a system nobody runs: this file carried max_tau 210 /
# min_quote 0.45 / range 5-12 while the host ran 180 / 0.55 / 5-16 (measured
# 2026-09-20), and nothing in the output said so.
LIVE = dict(max_tau=settings.max_tau_s, min_tau=settings.min_tau_s,
            min_quote=settings.min_quote, max_quote=settings.max_quote,
            min_range=settings.model_min_round_range_bps,
            max_range=settings.model_max_round_range_bps,
            # ⛔ THE CHOP GATE WAS MISSING HERE, AND IT COST MORE ENTRIES THAN
            # ANY OTHER KNOB IN THIS FILE IS WORTH. `run2()` defaults
            # `max_strike_crossings=0`, so every grid in this tool -- the sizing
            # front tier, the ship tables, `--dd-detail` -- entered rounds the
            # BOT REFUSES. Measured 2026-09-23 15:00-16:25: the live refusal log
            # is DOMINATED by this gate (1155 + 713 + 520 + 263 + 197 + 124 =
            # ~3,000 tick-refusals in three hours: "spot crossed the strike >=3
            # times"), live took 10 fills in the day, and the replay took 23 on
            # the SAME tape. A parallel copy of a live gate that is switched off
            # is not a conservative default -- it is a different strategy.
            # (Repo artifact #7: a nuisance parameter at a literal default that
            # is NOT the shipped value ranks a system you do not run.)
            max_strike_crossings=getattr(settings, "model_max_strike_crossings",
                                         0) or 0)


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


def strike_crossings(
        sec_last: list[tuple[float, float]], strike: float,
        min_secs: int = 3) -> int | None:
    """`Bot._strike_crossings`, reproduced EXACTLY.

    ⛔ THIS IS NOT `crossings()` ABOVE, AND THE TWO MUST NOT BE CONFUSED. That
    one counts sign changes of (spot - strike) on every row with a DEAD BAND in
    sigma units; this one is the LIVE gate:
      * RESAMPLE to the last spot in each whole second since the round opened
        (the live poll grid is a median 58-125 ms, so a raw count flips on
        jitter many times per real crossing and a threshold of 3 would refuse
        nearly every round);
      * NO DEAD BAND; a flip is a flip;
      * fewer than `min_secs` whole seconds -> None, which is "no opinion" and
        must NOT refuse, or a cold start silently disables the arm.

    `sec_last` is the per-second series built so far; the LAST entry is the
    second in progress, whose value is the current tick's spot -- exactly what
    the live buffer holds when the arm evaluates at that tick.
    """
    if len(sec_last) < min_secs:
        return None
    cr, up = 0, None
    for _sec, px in sec_last:
        now_up = px > strike
        if up is not None and now_up != up:
            cr += 1
        up = now_up
    return cr


def replay(rnd: dict, sigma: float, *, persistence: int,
           max_tau: float, min_tau: float | None,
           min_quote: float, max_quote: float,
           min_range: float, max_range: float,
           exec_mode: str = "limit_take", latency_s: float = 1.0,
           margin: float | None = None,
           haircut: float | None = None,
           min_edge: float | None = None,
           zone_min_margin: float = 0.0, zone_max_cross: int = 0,
           zone_band_k: float = 0.5, max_strike_crossings: int = 0,
           entry_rev_bps: float = 0.0, entry_look_s: float = 20.0,
           live_sig: list[tuple[int, float]] | None = None,
           funnel: dict | None = None,
           weekend: bool = True, candle_reset: bool = True,
           up_gate: bool = True, up_ok: dict | None = None,
           entry_slip: float = 0.0) -> dict | None:
    """The first round-qualifying entry under these knobs, or None.

    With `live_sig` the fair value at each tick uses the sigma the BOT recorded
    at that moment, so the replay reproduces the live rule rather than a
    parallel one; without it, the round's scalar `sigma`.

    LIVE PARITY (2026-09-23): with `weekend`, `candle_reset` and `up_gate` on --
    the defaults -- this mirrors `Bot._record` including the parts it used to
    omit: the weekend knob swap (`Bot._wk`), the wall-clock minute streak reset,
    and the up-regime gate. Turn them off to reproduce numbers quoted before
    that date. See the LIVE PARITY note at the top of the file.

    With `funnel` (a dict) the furthest GATE STAGE each round reached is written
    into it: the index is the last door the round got through, so a round with no
    entry died at `funnel[slug] + 1`. Stages:
        1 tick usable   2 tau window   3 travel band   4 signal agrees
        5 zone          6 crossings    7 entry-side reversal
        8 persistence   9 ENTERED
    The chain is this function's own, so the funnel cannot drift from the rule
    it is measuring (see the note on `strike_crossings`: a parallel copy of a
    gate that only samples post-gate ticks is inert and prints a fake null).
    """
    lo_tau = settings.min_tau_s if min_tau is None else min_tau
    hc = settings.edge_haircut if haircut is None else haircut
    up_gate_setting = getattr(settings, "up_regime_gate", 0.0) or 0.0
    w_sigma = float(getattr(settings, "weekend_sigma_mult", 1.0) or 1.0)
    # The candle index the streak belongs to. `None` until the first tick, so a
    # round whose first tick is mid-minute still starts a clean streak.
    cur_candle: int | None = None
    strike = rnd["strike"]
    start_ms = rnd.get("start_ms") or (rnd["rows"][0][0] if rnd.get("rows") else 0)
    sec_last: list[tuple[float, float]] = []      # (second, last spot in it)
    streak_side, streak = None, 0
    seen: list[float] = []
    seen_ts: list[tuple[int, float]] = []
    _deep: dict = funnel if funnel is not None else {}
    _slug = rnd.get("slug", "?")

    def _mark(st: int) -> None:
        if funnel is not None and st > _deep.get(_slug, 0):
            _deep[_slug] = st

    for i, (ts_ms, tau, spot, _up_px, _dn_px, up_ask, dn_ask) in enumerate(rnd["rows"]):
        # CANDLE-MINUTE STREAK RESET, exactly as live: `now_ms // 60_000`
        # changing pops the streak for this round unconditionally, on EVERY
        # tick, before the streak is read or extended. It sits at the top of
        # the loop for that reason -- a tick the tau window refuses still
        # resets it in the bot.
        if candle_reset:
            _ci = ts_ms // 60_000
            if _ci != cur_candle:
                cur_candle = _ci
                streak_side, streak = None, 0
        if spot is not None and spot > 0:
            seen.append(spot)
            seen_ts.append((ts_ms, spot))
            # `Bot._strike_crossings` resamples the round's OWN tape to 1 Hz
            # (the last spot in each whole second since the round opened) and
            # does so on EVERY tick -- it has nothing to do with whether the
            # signal likes the tick. Feeding it only on ticks that already
            # passed the gates below starves it: the arm's first agreeing tick
            # can be 200s into the round, and the helper returns None until it
            # has 3 whole seconds, so the gate could never refuse anything.
            sec = (ts_ms - start_ms) // 1000
            if not sec_last or sec_last[-1][0] != sec:
                sec_last.append((sec, spot))
            else:
                sec_last[-1] = (sec, spot)
        if (spot is None or not 0 < tau <= 300
                or up_ask is None or dn_ask is None):
            streak_side, streak = None, 0
            _mark(1)
            continue
        # THE WEEKEND KNOB SWAP (`Bot._wk`), per TICK, because the tape spans
        # weekends and the live rule changes inside it.
        if weekend and getattr(settings, "weekend_overrides", False) \
                and _is_weekend_utc(ts_ms):
            _max_tau = _wk_live("max_tau_s", ts_ms, max_tau)
            _min_quote = _wk_live("min_quote", ts_ms, min_quote)
            _max_quote = _wk_live("max_quote", ts_ms, max_quote)
            _min_edge = _wk_live("min_edge", ts_ms, min_edge)
            _hc = _wk_live("edge_haircut", ts_ms, hc)
            _persist = _wk_live("signal_persistence", ts_ms, persistence)
        else:
            _max_tau, _min_quote, _max_quote = max_tau, min_quote, max_quote
            _min_edge, _hc, _persist = min_edge, hc, persistence
        if not (lo_tau <= tau <= _max_tau):
            _mark(2)
            continue
        sg_i = _sig_at(live_sig, ts_ms, sigma)
        # `weekend_sigma_mult` is applied by the LIVE estimator, so the recorded
        # series already carries it; it is applied here only on the fallback
        # path, where the replay computes its own sigma and would otherwise
        # price a weekend tick at a weekday volatility.
        if live_sig is None and weekend and w_sigma != 1.0 \
                and _is_weekend_utc(ts_ms):
            sg_i = sg_i * w_sigma
        # main.Bot._round_range_bps: the round's TRAVEL so far, and None (no
        # opinion) until there are two samples -- never a refusal.
        if max_range > 0 and len(seen) >= 2:
            rng = 1e4 * (max(seen) - min(seen)) / strike
            if not (min_range <= rng <= max_range):
                _mark(3)
                continue
        fair_up = fair_value(spot, strike, tau, sg_i)
        # THE UP-REGIME GATE: when the last COMPLETED hour is up more than the
        # gate, the venue has priced Up correctly and our Up edge is
        # anti-informative, so live removes the Up candidate BEFORE the side
        # pick -- a Down candidate on the same quote still stands. Read from
        # completed 1m bars (`ao.one_hour_return`) for the same reason live is:
        # the current minute has not closed and using it is a look-ahead.
        _up_ok = True
        if up_gate and up_gate_setting > 0 and up_ok:
            _up_ok = up_ok.get(ts_ms // 60_000, True)
        sig = evaluate_hybrid(
            fair_up, fair_up, up_ask, dn_ask, up_allowed=_up_ok,
            fee=None, half_spread=0.0,
            safety_margin=settings.safety_margin, fee_model=FeeModel.NOTIONAL,
            max_quote=_max_quote, min_quote=_min_quote,
            min_edge=(settings.min_edge if _min_edge is None else _min_edge),
            edge_haircut=_hc)
        if not sig.act or sig.side is None:
            streak_side, streak = None, 0
            _mark(4)
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
                _mark(5)
                continue
            if zone_max_cross > 0 and len(seen) >= 3:
                # see `crossings`: the band is what makes this a property of the
                # path rather than of the sampler.
                if crossings(seen, strike,
                             zone_band_k * sg_i * strike) >= zone_max_cross:
                    _mark(5)
                    continue
        # ================= THE LIVE CROSSINGS GATE =================
        # ⛔ THIS IS `Bot._strike_crossings`, THE RULE THAT WENT LIVE ON
        # 2026-09-22 06:03Z -- NOT the `zone_max_cross` block above, which is
        # banded. The live gate has NO band and resamples to 1 Hz. It is
        # evaluated here, in the gate chain after the streak update, because in
        # the bot a refused tick still extends the persistence streak.
        if max_strike_crossings > 0:
            xc = strike_crossings(sec_last, strike)
            if xc is not None and xc >= max_strike_crossings:
                _mark(6)
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
                    _mark(7)
                    continue
        # ================================================================
        if streak < _persist:
            _mark(8)
            continue
        _mark(9)
        fair_side = fair_up if sig.side == "Up" else 1.0 - fair_up
        e = ao._execute(rnd, i, sig.side, fair_side,
                        sig.ev_per_dollar or 0.0,
                        f"model edge {sig.edge:+.3f}", margin=margin,
                        mz=moneyness_sd(spot, strike, tau, sg_i),
                        exec_mode=exec_mode, latency_s=latency_s)
        # THE MEASURED SLIPPAGE OF THE LIVE MARKET ORDER. The decision -> fill
        # lag is a median 2.59s (p90 3.57) on the live record while `latency_s`
        # is 1.0, and the fill pays ABOVE the ask we recorded at the decision:
        # measured on 454 live fills, `filled_px - venue_ask` is mean +0.0060,
        # median +0.0088, p90 +0.0173. Leaving it at 0 prices an entry the bot
        # never got; `--entry-slip 0.0088` charges the measured median.
        if e is not None and entry_slip:
            e["px"] = min(0.999, e["px"] + entry_slip)
            e["cost"] = ao._cost_per_share(e["px"])
            e["ret"] = ((1.0 if e["won"] else 0.0) - e["cost"]) / e["cost"]
        return e
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
                             "crossings", "funnel", "count", "ship",
                             "stakeband", "sizegrid", "tight", "floor"],
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
    ap.add_argument("--entry-slip", type=float, default=0.0,
                    help="VENUE EXECUTION, opt-in: charge the entry the price it "
                         "paid above the recorded ask (median +0.0073 against "
                         "the nearest quote on the live fills). NOT a property "
                         "of the arm's entries -- those span many configs -- it "
                         "is what an order pays against the book. 0.0 = clean.")
    ap.add_argument("--fee-model", choices=["curve", "live"], default="curve",
                    help="`curve` charges the fee `fee_rate_for_price` models "
                         "(what the arm's own gate uses); `live` charges the "
                         "band rate MEASURED from this DB's own fills, which "
                         "is 20-54%% higher inside the traded band. The gate "
                         "always uses the curve either way -- changing that "
                         "would change the entry SET, not the money.")
    ap.add_argument("--bankroll", type=float, default=76.0,
                    help="report each sizing variant against this bankroll "
                         "(the deposit the operator is sizing for)")
    ap.add_argument("--no-weekend", action="store_true",
                    help="do NOT swap in the weekend knobs (pre-2026-09-23 "
                         "behaviour: one band/tau/persistence for the whole "
                         "tape, including weekends)")
    ap.add_argument("--no-candle-reset", action="store_true",
                    help="do NOT reset the persistence streak at each "
                         "wall-clock minute (pre-2026-09-23 behaviour)")
    ap.add_argument("--no-up-gate", action="store_true",
                    help="do NOT apply the up-regime gate")
    ap.add_argument("--sms", default=None,
                    help="comma list of MAX stakes to sweep (--mode sizegrid)")
    ap.add_argument("--dump-rows", action="store_true",
                    help="print the per-trade sequence as `#ROW` lines "
                         "(day, ts_ms, per-$1 return with the exit, per-$1 "
                         "return holding, exited, rule). Per-$1 and not "
                         "dollars on purpose: the replay is linear in the "
                         "stake, so a sizing scheme (5%% of the bankroll) can "
                         "be applied afterwards without a re-replay")
    ap.add_argument("--dd-detail", type=float, default=None,
                    help="with --mode sizegrid: for ONE flat stake, print how "
                         "the maxDD is built (the peak it fell from, the "
                         "wallet's level at the trough) and what the SAME "
                         "trades score under reordering/resampling. A "
                         "peak-to-trough figure is one path, not a property "
                         "of the rule. e.g. --dd-detail 10")
    ap.add_argument("--smin", default=None,
                    help="comma list of MIN stakes to sweep (--mode sizegrid); "
                         "default 1,1.5,2,3")
    ap.add_argument("--ev-lo", type=float, default=None,
                    help="override `stake_ev_lo`: the ev/$ at which the ramp "
                         "pays the MIN stake (--mode sizegrid). The live value "
                         "is config's; 83%% of entries sit ABOVE the other end "
                         "of the ramp, so the ramp as shipped is inert.")
    ap.add_argument("--ev-hi", type=float, default=None,
                    help="override `stake_ev_hi`: the ev/$ at which the ramp "
                         "pays the MAX stake (--mode sizegrid)")
    ap.add_argument("--behind-bps", type=float, default=None,
                    help="override the exit's GRIND threshold "
                         "(`model_exit_behind_bps`; 0 = grind OFF = the "
                         "reaction rule alone). The grind is the half whose "
                         "exit count RISES with the stake, so a size grid "
                         "without this reads its cost as the stake's.")
    ap.add_argument("--behind-hold", type=float, default=None,
                    help="override `model_exit_behind_hold_s` (seconds the "
                         "spot must stay behind the strike)")
    ap.add_argument("--behind-max-tau", type=float, default=None,
                    help="`--behind-max-tau T`: fire the GRIND half only when "
                         "tau <= T (0/None = no ceiling = the live behaviour). "
                         "THE LATE-ONLY GRIND: near the bell a winner is AHEAD "
                         "of the strike and a loser is BEHIND it, and a deficit "
                         "is only decisive late (2bps behind is 1.25 sigma at "
                         "tau 10, 0.5 sigma at tau 60), so this caps the loss on "
                         "the about-to-lose cohort without cutting winners.")
    ap.add_argument("--min-tau", type=float, default=None,
                    help="override the exit's TAU FLOOR for the LEAD half "
                         "(`model_exit_min_tau_s`; 0 = armed to the bell). This "
                         "is the window that lost 2026-09-22 23:17 (tau 56) and "
                         "2026-09-23 10:34 (tau 54).")
    ap.add_argument("--min-tau-behind", type=float, default=None,
                    help="override the tau floor for the LOSS half alone "
                         "(`--behind-bps` must be > 0 to have one)")
    # ⛔ NOT `--rev-bps`: that name is already taken by the `--mode react`
    # helper (a different code path). `--exit-rev-bps` is the EXIT's threshold.
    ap.add_argument("--exit-rev-bps", type=float, default=None,
                    help="override `model_exit_rev_bps`: how big a move against "
                         "us counts as a reaction (5.0 live). Lower = more "
                         "exits, fewer full losses, less P&L.")
    ap.add_argument("--exit-max-bid", type=float, default=None,
                    help="REFUSE to sell while the recorded bid is above this "
                         "(0 = off). Use it to stop the exit giving away a "
                         "round the market has already priced as won -- the "
                         "2026-09-23 11:29 trigger sold at 0.93 while the "
                         "position was ~4.2 sigma in the money with 16s left.")
    ap.add_argument("--exit-max-z", type=float, default=None,
                    help="REFUSE to sell while the position is more than this "
                         "many sigma of the REMAINING move in the money "
                         "(0 = off). The sigma-scaled form of the same idea: "
                         "a 5bps/20s move is not a threat when the lead is 4 "
                         "sigma wide.")
    ap.add_argument("--exit-min-prem", type=float, default=None,
                    help="`--exit-min-prem`: refuse a sale unless the book still "
                         "pays MORE than the model's fair value for the "
                         "position (`bid - slip >= fair + prem`). 0/None = OFF "
                         "(sell at any price). THE EDGE RUN IN REVERSE: the "
                         "arm's profit comes from the venue's quote lagging the "
                         "spot, and the same lag says when an EXIT is paid -- "
                         "only while the book is still above fair. Below it we "
                         "hand over the spread to convert a coin flip into a "
                         "coin flip, which is why every grind threshold "
                         "measured 2026-09-23 lost money. Partner it with "
                         "`--behind-bps`: sell a decided loss at a price the "
                         "market has not caught up to, not at any price.")
    args = ap.parse_args()
    BEHIND_OVR["bps"] = args.behind_bps
    BEHIND_OVR["hold"] = args.behind_hold
    TAU_OVR["react"] = args.min_tau
    TAU_OVR["behind"] = args.min_tau_behind
    REV_OVR["rev"] = args.exit_rev_bps
    MAX_BID_OVR["bid"] = args.exit_max_bid
    MAX_Z_OVR["z"] = args.exit_max_z
    PREM_OVR["prem"] = args.exit_min_prem
    BMAX_OVR["tau"] = args.behind_max_tau
    DUMP_ROWS["on"] = bool(getattr(args, "dump_rows", False))
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
                latency_s=args.latency_s,
                weekend=not args.no_weekend,
                candle_reset=not args.no_candle_reset,
                up_gate=not args.no_up_gate,
                up_ok=(up_gate_by_minute(ao.minute_closes(rs),
                                         getattr(settings, "up_regime_gate", 0.0)
                                         or 0.0)
                       if not args.no_up_gate else {}),
                entry_slip=args.entry_slip)
    if base["up_ok"]:
        _blocked = sum(1 for v in base["up_ok"].values() if not v)
        print(f"  up-regime gate: {_blocked} of {len(base['up_ok'])} minutes "
              f"block the Up side (gate "
              f"{getattr(settings, 'up_regime_gate', 0.0):+.3f})")
    if args.fee_model == "live":
        global _FEE_LIVE, _FEE_BANDS
        _FEE_BANDS, _n = _measured_fee_table(con)
        _FEE_LIVE = True
        print(f"  fee model: LIVE bands measured from {_n} fills in this DB -- "
              + ", ".join(f"{lo:.2f}-{hi:.2f}:{1e4*rate:.0f}bps(n={n})"
                          for lo, hi, rate, n in _FEE_BANDS))
    else:
        print("  fee model: curve (fee_rate_for_price) -- the rate the arm's own "
              "gate uses")

    def run2(**over) -> list[dict]:
        # ⛔ FOLD `base` IN, DO NOT PASS IT SEPARATELY. `replay(r, sg, **base,
        # **kw)` raised "got multiple values for keyword argument 'persistence'"
        # the moment a mode swept a knob that both dicts carry. Overrides win.
        kw = {**base, **LIVE, **over}
        out = []
        for r, sg in zip(rs, sigmas):
            e = replay(r, sg, **kw,
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
        # ⭐ THE FLOOR, DOWNWARD (added 2026-09-23). The UPWARD rows above answer
        # "should we pay up for a surer side". They do not answer the question
        # the live record asks: `min_quote 0.50` refuses ~60% of the IN-WINDOW
        # ticks (6,060 of 8h on 2026-09-23), and the model's side prices just
        # under it in a calm regime -- so the arm sits out for hours. Lowering
        # the floor admits the cheap side, which also pays the fee at its 180bps
        # CAP (fee_bps = 180*min(1,(1-px)/px), pinned below px 0.50), so this is
        # a real trade and not free trades.
        print("  -- ⭐ the quote floor DOWNWARD: the gate refusing 60% of the "
              "priced ticks --")
        for v in (0.30, 0.35, 0.40, 0.45, 0.50):
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
                ("min_quote 0.50 -> 0.45", dict(min_quote=0.50),
                 dict(min_quote=0.45)),
                ("min_quote 0.45 -> 0.40", dict(min_quote=0.45),
                 dict(min_quote=0.40)),
                ("min_quote 0.40 -> 0.35", dict(min_quote=0.40),
                 dict(min_quote=0.35)),
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

    if args.mode == "crossings":
        # ============ THE LIVE STRIKE-CROSSINGS GATE ============
        # ⛔ WHAT THIS IS FOR. `model_max_strike_crossings=3` went live on
        # 2026-09-22 06:03Z, and the arm's attempt rate on btc-5m fell from
        # 32-57 rounds/day to 6 in the 12 hours after. The gate's own note
        # claimed 13.2 -> 10.2 entries/day (-23%); live it cost ~7x the
        # attempts. That note also states the hazard in its own words -- "our
        # edge comes from the venue's quote lagging spot, which is LARGEST when
        # the move is small; refusing small moves removes the edge with the
        # risk" -- and a round bouncing off the strike IS the small-move round,
        # so the gate does not remove a random slice of the population, it
        # removes candidates.
        # (And this was never replayable before: the `zone_max_cross` block is
        # a DIFFERENT rule, banded and unresampled. See `strike_crossings`.)
        #
        # READ THE INCREMENT, NOT THE HEADLINE CELL. The thresholds nest, so
        # the question is what each STEP bans: the gate is worth its count only
        # if the cohort it removes has a NEGATIVE EV. A 46% cut in rounds is a
        # high price for banning a cohort that is already flat.
        print("\n=== THE LIVE STRIKE-CROSSINGS GATE (`Bot._strike_crossings`) ===")
        print("  (1 Hz-resampled sign flips vs the strike, no dead band; 0 = off)")
        lvl = (0, 2, 3, 4, 5, 6, 8, 10, 15)
        ent = {}
        for t in lvl:
            e = run2(max_strike_crossings=t)
            ent[t] = e
            row(f"max_crossings {t}" if t else "crossings OFF", e, len(rs), days)
        print("\n  -- THE COHORT EACH VALUE REMOVES, MEASURED AGAINST OFF --")
        print("     (a threshold is worth its cut only if the removed cohort is"
              " flat or negative)")
        # ⛔ ANCHOR ON OFF, NOT ON THE NEIGHBOUR. The thresholds NEST and a
        # HIGHER value is LOOSER -- it refuses fewer rounds -- so consecutive
        # pairs read "bans 0 of N" and hide the cost entirely. Measured
        # 2026-09-22: the pairs printed 0,0,0,0,0,0,0 while OFF -> 3 was
        # removing 66 rounds. What the decision needs is each value against no
        # gate at all.
        # ⛔ ALSO DO NOT NAME THIS `base`. `run2` is a closure over main's
        # `base` (the replay kwargs), so rebinding it here made the NEXT
        # `run2()` unpack a slug->return dict as kwargs: "replay() got an
        # unexpected keyword argument 'btc-updown-5m-...'". Measured
        # 2026-09-22.
        off_ret = {e["slug"]: e["ret"] for e in ent[lvl[0]]}
        for t in lvl[1:]:
            keep = {e["slug"]: e["ret"] for e in ent[t]}
            gone = [v for k, v in off_ret.items() if k not in keep]
            won = list(keep.values())
            ev = (sum(gone) / len(gone)) if gone else float("nan")
            wr = (100 * sum(1 for v in gone if v > 0) / len(gone)
                  if gone else float("nan"))
            evw = (sum(won) / len(won)) if won else float("nan")
            print(f"     OFF -> {t:<2}  removes {len(gone):>4} of {len(off_ret):>4} "
                  f"rounds ({len(gone)/max(days,1):4.1f}/day, "
                  f"{100*len(gone)/max(len(off_ret),1):3.0f}%)  "
                  f"their EV {ev:+.4f}/$1  WR {wr:5.1f}%  "
                  f"t {ao.tstat(gone) if gone else 0:+5.2f}  "
                  f"tot@$2 ${STAKE*sum(gone):+7.2f}   | kept EV "
                  f"{evw:+.4f}/$1 over {len(won)}")
        print("  -- and the whole thing vs OFF --")
        print(f"     OFF gives {len(off_ret)} rounds; the shipped rule (3) keeps "
              f"{len(ent[3])} ({100*len(ent[3])/max(len(off_ret),1):.0f}%)")

    if args.mode == "funnel":
        # =============== WHERE THE ROUNDS ACTUALLY DIE ===============
        # 288 rounds open on btc-5m every day and the arm enters ~15, so the
        # question "can we trade more without losing quality" is a question
        # about which DOOR is shut. The distinction that decides everything:
        # stages 2/3/5/6/7 are KNOBS (loosening one admits rounds that the
        # signal already liked), stage 4 is OUR OWN SIGNAL -- if rounds die
        # there, no gate can be opened, because the fair value never disagreed
        # with the quote by `min_edge`, and the only lever is a smaller edge
        # requirement, i.e. a different (worse) trade.
        # ⭐ The stage index is the LAST door the round got through, so a round
        # with no entry died at stage+1.
        print("\n=== FUNNEL: how far each round gets (live knobs) ===")
        names = {1: "tick usable (spot + 2 quotes)",
                 2: "tau window (min_tau..max_tau)",
                 3: "travel band (min..max range)",
                 4: "signal agrees (min_edge)",
                 5: "zone",
                 6: "crossings",
                 7: "entry-side reversal",
                 8: "persistence",
                 9: "ENTERED"}
        fun: dict = {}
        ent = run2(funnel=fun)
        n = len(rs)
        reach = {s: 0 for s in names}
        died = {s: 0 for s in names}
        for stage in fun.values():
            for s in names:
                if stage >= s:
                    reach[s] += 1
            died[stage] = died.get(stage, 0) + 1
        print(f"  rounds on the tape: {n} over {days:.1f} days "
              f"({n/max(days,1):.0f}/day)")
        print(f"  {'stage':<38}{'reached':>8}{'%':>6}{'died here':>11}")
        for s in sorted(names):
            print(f"  {s}. {names[s]:<35}{reach[s]:>8}"
                  f"{100*reach[s]/max(n,1):>5.0f}%{died[s]:>11}")
        print(f"  entries: {len(ent)} ({len(ent)/max(days,1):.1f}/day)  "
              f"-- must equal the stage-9 count")
        if n - len(fun):
            print(f"  (!) {n - len(fun)} rounds never had one usable tick")
        # The conversion that matters: of the rounds the SIGNAL liked, how many
        # the gates then threw away.
        if reach[5]:
            print(f"  signal-to-entry: {reach[9]}/{reach[5]} of the rounds where "
                  f"the signal ever agreed "
                  f"({100*reach[9]/max(reach[5],1):.0f}%)")

    if args.mode == "count":
        # ================ MORE TRADES, SAME QUALITY? ================
        # 288 rounds open on btc-5m a day and the arm enters ~15. `--mode
        # funnel` says where the rest die; this mode PRICES the doors. Run the
        # shipped rule, then open ONE door and read the rounds that walk in.
        # ⭐ READ THE DOOR'S OWN NUMBERS, NOT THE HEADLINE. A loosening that adds
        # 100 trades at +0.04/$1 to a book running +0.13 is a 35% decline in
        # quality whatever the total does, and the halves columns are what say
        # whether the direction repeats rather than the exact value winning on
        # one sample.
        L = dict(min_edge=settings.min_edge, min_quote=settings.min_quote,
                 max_quote=settings.max_quote,
                 min_range=settings.model_min_round_range_bps,
                 max_range=settings.model_max_round_range_bps,
                 max_tau=settings.max_tau_s)
        print("\n=== MORE TRADES: open one door, price what walks in ===")
        base_e = run2()
        row("live (shipped)", base_e, len(rs), days)
        bmap = {e["slug"]: e["ret"] for e in base_e}
        cands = [
            (f"min_edge {L['min_edge']:g} -> 0.05", dict(min_edge=0.05)),
            (f"min_edge {L['min_edge']:g} -> 0.03", dict(min_edge=0.03)),
            (f"min_edge {L['min_edge']:g} -> 0.02", dict(min_edge=0.02)),
            (f"min_edge {L['min_edge']:g} -> 0.00", dict(min_edge=0.00)),
            (f"persistence {persist} -> 1", dict(persistence=1)),
            (f"min_range {L['min_range']:g} -> 3.0", dict(min_range=3.0)),
            (f"max_range {L['max_range']:g} -> 20.0", dict(max_range=20.0)),
            (f"min_quote {L['min_quote']:g} -> 0.45", dict(min_quote=0.45)),
            (f"max_quote {L['max_quote']:g} -> 0.80", dict(max_quote=0.80)),
            (f"max_quote {L['max_quote']:g} -> 0.90", dict(max_quote=0.90)),
            (f"max_tau {L['max_tau']:g} -> 300", dict(max_tau=300.0)),
        ]
        for label, over in cands:
            e = run2(**over)
            row(label, e, len(rs), days)
            nmap = {x["slug"]: x["ret"] for x in e}
            added = [v for k, v in nmap.items() if k not in bmap]
            gone = [v for k, v in bmap.items() if k not in nmap]
            ev_a = (sum(added) / len(added)) if added else float("nan")
            ev_g = (sum(gone) / len(gone)) if gone else float("nan")
            wr_a = (100 * sum(1 for v in added if v > 0) / len(added)
                    if added else float("nan"))
            print(f"      door: +{len(added)} rounds "
                  f"({len(added)/max(days,1):4.1f}/day) at EV {ev_a:+.4f}/$1  "
                  f"WR {wr_a:5.1f}%  t {ao.tstat(added) if added else 0:+5.2f}"
                  f"   | lost {len(gone)} at EV {ev_g:+.4f}/$1")

    if args.mode == "tight":
        # ============ THE TIGHTENING SIDE OF THE ENTRY BAND ============
        # ⭐ THE ONE DIRECTION THE OFAT LEFT UNPROBED (2026-09-23). Every other
        # entry knob came back with the LIVE value at or near the best cell, and
        # the two that did not are monotone in the direction of TIGHTER:
        #   `max_range 20 -> 12` raised EV 17%, day-t 4.5x (+0.57 -> +2.59) and
        #   made the drawdown SHALLOWER (-16.51 -> -14.63), and 12 was the
        #   tightest value the OFAT tested;
        #   `min_quote` was only ever probed DOWNWARD (0.45 -> 0.05, all worse),
        #   so the upward side (0.50-0.60) is unmeasured.
        # Both are the same claim: the arm's edge lives in the rounds that have
        # MOVED (a range of 8-12bps, a price away from 0.50), not in the flat
        # ones where the model's `fair` is barely distinguishable from the book.
        # ⛔ JUDGE ON THE HALVES RANKING, never on the pooled number: a knob
        # whose ranking FLIPS between halves is a fitted split of one sample --
        # the `min_tau 60 vs 5` trap (day-t +0.46 -> +1.75, halves reversed).
        print("\n=== TIGHT: the band the live config has not yet probed "
              "(H = per-$1 by half) ===")
        print("  -- the round-range CEILING, tighter than live 12 --")
        for mr in (12.0, 10.0, 8.0, 6.0):
            row(f"max_range {mr:g}", run2(max_range=mr), len(rs), days)
        print("  -- the round-range FLOOR, tighter than live 5 --")
        for mn in (5.0, 8.0, 10.0):
            row(f"min_range {mn:g}", run2(min_range=mn), len(rs), days)
        print("  -- the quote FLOOR, with the MARGINAL COHORT priced --")
        # ⛔ THE INCREMENT, NOT TWO CONFIDENCE INTERVALS. Raising a floor does
        # not "improve the cell" -- it REMOVES a cohort, and the honest question
        # is what THAT cohort earns. `growth(A+B) - growth(A)` is B's own
        # growth, so the removed rounds' mean return is the whole argument: if
        # the removed cohort is negative, removing it is a gain of its size; if
        # it is positive, the floor is buying a nicer ratio with real money.
        base_e = run2()
        bmap0 = {e["slug"]: e for e in base_e}
        print(f"     live floor {settings.min_quote:g}: n={len(base_e)} "
              f"EV {st.fmean([e['ret'] for e in base_e]):+.4f}/$1")
        for mq in (0.45, 0.475, 0.50, 0.525, 0.55, 0.575, 0.60):
            cell = run2(min_quote=mq)
            got = {e["slug"]: e for e in cell}
            gone = [e for s, e in bmap0.items() if s not in got]
            added = [e for s, e in got.items() if s not in bmap0]
            if gone:
                print(f"     -> removing {len(gone):3d} rounds at "
                      f"EV {st.fmean([e['ret'] for e in gone]):+.4f}/$1  "
                      f"(adds {len(added)})")
            else:
                print(f"     -> the baseline itself (removes 0)")
            row(f"min_quote {mq:g}", cell, len(rs), days)
        print("  -- the quote CEILING, tighter than live 0.80 --")
        for xq in (0.80, 0.75, 0.70):
            row(f"max_quote {xq:g}", run2(max_quote=xq), len(rs), days)
        print("  -- the two best guessed TOGETHER (the joint cell) --")
        for mr, mq in ((10.0, 0.50), (8.0, 0.50), (10.0, 0.55)):
            row(f"mr {mr:g} + mq {mq:g}",
                run2(max_range=mr, min_quote=mq), len(rs), days)
        return

    if args.mode == "floor":
        # ====== THE QUOTE FLOOR AT 0.01 RESOLUTION (added 2026-09-23) ======
        # WHY THIS EXISTS. The 0.025 grid showed an interior hump peaking at
        # 0.475 with a DIP exactly at the previously-live 0.50:
        #   0.525 +0.1309 | **0.50 +0.1201** | 0.475 +0.1345 | 0.45 +0.1315
        # A local minimum sitting on the value we happened to be running is this
        # repo's noise signature (the train sweep's X=12.5 hole), so the value
        # cannot be read off that grid -- the neighbours are what could be
        # lucky. This resolves it a tick at a time, printing the halves (the
        # direction check) and the cohort each step adds or removes.
        # ⛔ `bmap` NOT `base`: see the note in `--mode tight` -- rebinding
        # `base` breaks `run2`'s closure.
        print("\n=== THE QUOTE FLOOR AT 0.01 RESOLUTION (H = per-$1 by half) ===")
        base_e = run2()
        bmap = {e["slug"]: e for e in base_e}
        print(f"     live floor {settings.min_quote:g}: n={len(base_e)} "
              f"EV {st.fmean([e['ret'] for e in base_e]):+.4f}/$1")
        for mq in (0.44, 0.45, 0.46, 0.47, 0.475, 0.48, 0.49, 0.50, 0.51,
                   0.52):
            cell = run2(min_quote=mq)
            got = {e["slug"]: e for e in cell}
            gone = [e for s, e in bmap.items() if s not in got]
            if gone:
                print(f"     -> removes {len(gone):3d} rounds at "
                      f"EV {st.fmean([e['ret'] for e in gone]):+.4f}/$1")
            else:
                print(f"     -> adds {len(got) - len(bmap):3d} rounds")
            row(f"min_quote {mq:g}", cell, len(rs), days)
        return

    if args.mode in ("ship", "sizegrid"):
        # ======= WHAT ACTUALLY SHIPS: ENTRY + SIZING + THE LIVE EXIT =======
        # An entry table is in EV/$1 at a constant stake, so it CANNOT rank a
        # sizing change: `stake_for_edge` pays between min_stake and max_stake as
        # a function of ev/$1, so a knob that adds high-EV trades adds the
        # LARGEST positions, and one that lowers EV/trade lowers the stake on
        # every trade it keeps. Everything below is DOLLARS, and the exit is
        # applied on top of the entry.
        #
        # THE SIZING SPEC, verbatim from `Bot._stake_for` / `_base_stake_for`:
        #   value = ev_per_dollar                    (sizing_metric == "ev")
        #   stake = stake_for_edge(value, min_edge=stake_ev_lo,
        #           min_stake=min_stake_usdt, max_stake=max_stake_usdt,
        #           edge_at_max=stake_ev_hi)         -> LINEAR ramp, clipped
        #   * model_stake_boost_mult if stake_boosted(fair)   (MODEL arm only)
        #   * tilt_factor(mz) if stake_tilt == "inv_z"
        #   min(stake, stake_cap_usdt) if cap > 0
        # and a stake under min_order_usdt is SKIPPED, never rounded up.
        #
        # THE EXIT, verbatim from `_arm_exit_watch` (`react`): sell
        # model_exit_frac of the position at the RECORDED BID (minus one tick of
        # slip and the fee) the first time the spot has moved
        # model_exit_rev_bps against us over model_exit_look_s, subject to
        # tau > model_exit_min_tau_s and a model_exit_min_hold_s hold, and ONE
        # sale per position.
        S = settings
        SLIP, XFEE = 0.01, 0.005
        # ⛔ NOT `st.fmean` HERE, AND IT IS NOT PARANOIA. `main()` binds `st` as
        # a for-loop target (the funnel block's `for stage in ...` used to be
        # `for st in ...`), and ANY assignment to a name anywhere in a function
        # makes it local to that function for the whole body -- so every nested
        # helper that says `st.fmean` resolves to main's local and dies with
        # "cannot access free variable 'st' where it is not associated with a
        # value". That rename broke `--mode exit` (it uses st.fmean in `erow`)
        # and `--mode count` long before the ship mode existed.
        from statistics import fmean as _fmean
        # The venue refuses a SELL whose notional is under this, so a trigger
        # whose slice is too small is NOT an exit -- it is a position held to
        # the bell. Modelled because it binds exactly when the stop is working.
        from btcpredict.execution.live import (                     # noqa: E402
            SELL_MIN_NOTIONAL_USDT as _SELL_MIN)
        SZ = dict(lo=S.stake_ev_lo, hi=S.stake_ev_hi,
                  min_stake=S.min_stake_usdt, max_stake=S.max_stake_usdt,
                  tilt=S.stake_tilt, cap=S.stake_cap_usdt,
                  boost=S.model_stake_boost_mult)
        _sz = [SZ]                       # the sizing spec in force

        def _stake_of(e: dict) -> float:
            z = _sz[0]
            s = stake_for_edge(float(e.get("ev") or 0.0), min_edge=z["lo"],
                               min_stake=z["min_stake"],
                               max_stake=z["max_stake"],
                               edge_at_max=z["hi"])
            if abs(z["boost"] - 1.0) > 1e-9:
                try:
                    from btcpredict.config import stake_boosted
                    if stake_boosted(e.get("fair")):
                        s *= z["boost"]
                except Exception:               # stub configs in tests
                    pass
            if z["tilt"] == "inv_z":
                s = max(s * tilt_factor(float(e.get("mz") or 0.0),
                                        reference=S.tilt_reference_z,
                                        lo=S.tilt_min, hi=S.tilt_max),
                        S.min_order_usdt)
            if z["cap"] > 0:
                s = min(s, z["cap"])
            return 0.0 if s < S.min_order_usdt else s

        def _bids_for(ent: list[dict]) -> dict:
            out: dict[str, dict] = {}
            for s in {e["slug"] for e in ent}:
                out[s] = {t: (u, d) for t, u, d in con.execute(
                    "SELECT ts_ms, up_bid, down_bid FROM quotes "
                    "WHERE slug=? AND up_bid IS NOT NULL ORDER BY ts_ms",
                    (s,))}
            return out

        def _ret_exited(e: dict, rnd: dict, bidmap: dict,
                        stake: float) -> tuple[float, bool, str]:
            """(per-$1 return with the live exit applied, did it exit, rule).

            MIRRORS `main.Bot._model_exit_check` in FULL -- both halves, the
            venue floor, and the retry. Versions of this overlay did NOT:

              * `_reaction_against` only: the spot must still be AHEAD of the
                strike (`log(S/K)*sgn > 0`), because the rule protects a LEAD,
                so once the price is behind the strike there is nothing to
                protect. What that misses is the BLEED -- a slow drift across
                the strike produces no reaction at all and the lead guard turns
                the exit off exactly when the thesis is dead. Live 2026-09-22
                23:17 (btc-updown-5m-1790118900, Up, $2 at 0.647) settled
                -2.0000 with the exit armed and silent. `_grind_against` is the
                other half: BEHIND the strike by >= model_exit_behind_bps for
                >= model_exit_behind_hold_s, as a STATE with its own clock.
              * the floor is a NOTIONAL ON THE SLICE SENT, checked against the
                ACTUAL POSITION (`stake / cost` shares) -- not the slice per $1
                of stake. That unit error blocked 81 of 85 triggers once and
                reported the exit as inert (artifact list, entry 8).
              * the SHARES ARE FLOORED TO 0.01 before the sale, as
                `LiveBroker.sell_fak` floors them ("selling FEWER is always
                accepted"). At $1-2 stakes this is the difference between a sale
                that clears $1.00 of notional and one that does not.
              * a refused sale is NOT final: live leaves the position watched and
                retries EVERY tick, so a refusal here `continue`s the scan.
            """
            rows = rnd["rows"]
            strike = rnd.get("strike")
            j = next((k for k, rr in enumerate(rows) if rr[0] >= e["ts_ms"]),
                     None)
            if j is None:
                return e["ret"], False, ""
            sgn = 1.0 if e["side"] == "Up" else -1.0
            _t_react = (float(S.model_exit_min_tau_s) if TAU_OVR["react"] is None
                        else float(TAU_OVR["react"]))
            _t_behind = (_t_react if TAU_OVR["behind"] is None
                         else float(TAU_OVR["behind"]))
            _b_bps = (float(S.model_exit_behind_bps)
                      if BEHIND_OVR["bps"] is None
                      else float(BEHIND_OVR["bps"]))
            _b_hold = (float(S.model_exit_behind_hold_s)
                       if BEHIND_OVR["hold"] is None
                       else float(BEHIND_OVR["hold"]))
            # `None` = no CLI override -> the SHIPPED value, so the replay here
            # ranks the rule the bot actually runs. A literal default would
            # rank a system nobody runs (artifact list, entry 7).
            _bmax = (float(getattr(S, "model_exit_behind_max_tau_s", 0.0) or 0.0)
                     if BMAX_OVR["tau"] is None
                     else float(BMAX_OVR["tau"] or 0.0))
            _rev = (float(S.model_exit_rev_bps) if REV_OVR["rev"] is None
                    else float(REV_OVR["rev"]))
            _max_bid = (float(MAX_BID_OVR["bid"] or 0.0))
            _max_z = (float(MAX_Z_OVR["z"] or 0.0))
            _prem = float(PREM_OVR["prem"] or 0.0)
            # ⛔ NOT `idx_of`/`sigmas` FROM THE EXIT BLOCK: they are bound LATER
            # in `main`, so referencing them here raises NameError the moment
            # the grid's diagnostic calls this helper (the same footgun the `st`
            # note above records). Computed into `_SG0` in this block instead.
            _sg0 = _SG0.get(e["slug"], 0.0)
            _sseries = live_sig.get(e["slug"]) if args.use_live_sigma else None
            seen = [(rr[0], rr[2]) for rr in rows[:j + 1]
                    if rr[2] and rr[2] > 0]
            look_ms = S.model_exit_look_s * 1000.0
            sh = stake / ao._cost_per_share(e["px"])        # ACTUAL shares
            # The venue's own step: `int(shares * 100) / 100.0`.
            sh = int(sh * 100) / 100.0
            frac = float(S.model_exit_frac)
            behind_since: int | None = None
            for rr in rows[j + 1:]:
                ts, tau, spot = rr[0], rr[1], rr[2]
                if ts - e["ts_ms"] < S.model_exit_min_hold_s * 1000:
                    continue
                # The watch ends only when NEITHER half can still act; tau falls
                # monotonically so a break here is final.
                if tau is None or tau <= min(_t_react, _t_behind):
                    break            # the book goes one-sided at the bell
                if not spot or spot <= 0:
                    continue
                seen.append((ts, spot))
                if not (strike and tau and tau > 0):
                    continue
                # ⛔ IS THIS MOVE A THREAT, OR JUST A BUMP? The reaction rule
                # asks only "did the spot move 5bps against us over 20s", which
                # is satisfied identically whether we are 0.5 sigma or 4 sigma
                # ahead. This refuses the tick while the lead is still larger
                # than `_max_z` sigma of the move LEFT, and KEEPS WATCHING: a
                # lead that shrinks below the gate can still be sold later, at
                # which point the sale is real protection rather than
                # surrendering a won round.
                # ⚠️ IT APPLIES TO THE LEAD ONLY (`log(S/K)*sgn > 0`), which is
                # what shipped in `_reaction_against`: a position 4 sigma BEHIND
                # the strike is exactly the case the grind half exists for, so a
                # magnitude gate there would suppress the sale that matters.
                if _max_z and math.log(spot / strike) * sgn > 0:
                    _sg = _sig_at(_sseries, ts, _sg0)
                    if _sg and _sg > 0:
                        if moneyness_sd(spot, strike, tau, _sg) * sgn > _max_z:
                            continue
                rule = ""
                if math.log(spot / strike) * sgn > 0:
                    # THE REACTION HALF: a lead that is being snapped away.
                    behind_since = None
                    if tau <= _t_react:
                        continue          # this half's floor, separately
                    past = [s for t, s in seen if t <= ts - look_ms]
                    if past and (
                            math.log(spot / past[-1]) * 1e4 * sgn
                            <= -_rev):
                        rule = "react"
                elif _b_bps > 0 and tau > _t_behind:
                    # THE GRIND HALF: the lead is not snapped away, it is GONE.
                    # `behind_since` is stamped on the first tick at or beyond
                    # the threshold and CLEARED the moment the spot is back on
                    # our side, so a position wobbling across the strike does not
                    # accumulate time it never spent behind it.
                    # ⛔ `_bmax` KEEPS THIS TO THE END OF THE ROUND (see
                    # BMAX_OVR): the same deficit is decisive at tau 10 and
                    # noise at tau 200, so a ceiling separates "about to lose"
                    # from "wobbled", which is what lets the sale cap a loss
                    # without cutting a winner.
                    if _bmax and tau > _bmax:
                        continue
                    behind = -math.log(spot / strike) * 1e4 * sgn
                    if behind < _b_bps:
                        behind_since = None
                    else:
                        if behind_since is None:
                            behind_since = ts
                        if ts - behind_since >= _b_hold * 1000.0:
                            rule = "grind"
                if not rule:
                    continue
                px = None                          # the recorded bid, that tick
                for t in sorted(bidmap, reverse=True):
                    if t <= ts:
                        u, d = bidmap[t]
                        px = u if e["side"] == "Up" else d
                        break
                if not px:
                    EXIT_TRIG[e["slug"]] = f"{rule}:no_bid"
                    continue
                # ⛔ DO NOT GIVE AWAY A DONE ROUND. A bid above `max_bid` means
                # the market already prices this position as settled in our
                # favour; selling it is surrendering (1 - bid) of a near-certain
                # $1. KEEP WATCHING rather than stopping -- the rule is a
                # transient and the price may fall to a level that is worth
                # selling at, which is when the exit is actually paid.
                if _max_bid and px > _max_bid:
                    EXIT_TRIG[e["slug"]] = f"{rule}:max_bid"
                    continue
                out_px = max(0.0, px - SLIP)
                # ⭐ SELL THE LAG, NOT THE LOSS (see PREM_OVR). `_prem > 0`
                # refuses the sale unless the book still pays more than the
                # model's fair value for this position, i.e. unless the venue's
                # quote has not yet caught up with the spot -- the one state in
                # which an exit is PAID rather than charged the spread.
                if _prem > 0:
                    _sgf = _sig_at(_sseries, ts, _sg0) or _sg0
                    if strike and tau and _sgf:
                        _fu = fair_value(spot, strike, tau, _sgf)
                        _fair_ours = _fu if e["side"] == "Up" else 1.0 - _fu
                        if out_px < _fair_ours + _prem:
                            EXIT_TRIG[e["slug"]] = f"{rule}:below_fair"
                            continue
                if sh * frac * out_px < _SELL_MIN:
                    EXIT_TRIG[e["slug"]] = f"{rule}:floor"
                    continue         # the venue refuses the SELL; retried next tick
                got = sh * frac * out_px * (1.0 - FEE_RATE(out_px))
                rest = sh * (1.0 - frac) * (1.0 if e["won"] else 0.0)
                EXIT_TRIG[e["slug"]] = f"{rule}:taken"
                return (got + rest) / stake - 1.0, True, rule
            return e["ret"], False, ""

        def _battery(ent: list[dict], rmap: dict, *, use_exit: bool,
                     tag: str) -> tuple[float, float]:
            bmap = _bids_for(ent) if use_exit else {}
            cash, n_exit = [], 0
            for e in ent:
                stake = _stake_of(e)
                if stake <= 0:
                    continue
                if use_exit:
                    r, did, _rule = _ret_exited(e, rmap[e["slug"]], bmap.get(
                        e["slug"], {}), stake)
                    n_exit += 1 if did else 0
                else:
                    r = e["ret"]
                cash.append((e["ts_ms"], e["day"], stake, stake * r, r))
            if not cash:
                print(f"      {tag}: no trades")
                return 0.0, 0.0
            cash.sort()
            tot = sum(c[3] for c in cash)
            curve = peak = dd = 0.0
            for c in cash:
                curve += c[3]
                peak = max(peak, curve)
                dd = min(dd, curve - peak)
            half = len(cash) // 2
            h1 = sum(c[3] for c in cash[:half]) / max(half, 1)
            h2 = sum(c[3] for c in cash[half:]) / max(len(cash) - half, 1)
            print(f"      {tag:<12} ${tot:+8.2f}  ${tot/max(days,1):+7.2f}/day  "
                  f"n={len(cash):<4} ${tot/len(cash):+.3f}/trade  "
                  f"stake ${_fmean([c[2] for c in cash]):.2f}  "
                  f"maxDD ${dd:+7.2f}  tot/|DD| {tot/max(abs(dd),1e-9):5.2f}  "
                  f"halves ${h1:+.3f}/${h2:+.3f}"
                  + (f"  exits {n_exit}/{len(cash)}" if use_exit else ""))
            return tot, dd

        print("\n=== SHIP: DOLLARS on the live sizing spec, with the live exit ===")
        print(f"  sizing {S.sizing_mode}/{S.sizing_metric}: "
              f"${S.min_stake_usdt:g}->${S.max_stake_usdt:g} at ev "
              f"{S.stake_ev_lo:g}->{S.stake_ev_hi:g}, tilt {S.stake_tilt}, "
              f"cap {S.stake_cap_usdt:g}, boost x{S.model_stake_boost_mult:g}")
        print(f"  exit LIVE: react {-S.model_exit_rev_bps:g}bps/"
              f"{S.model_exit_look_s:g}s OR behind "
              f"{S.model_exit_behind_bps:g}bps for "
              f"{S.model_exit_behind_hold_s:g}s, frac {S.model_exit_frac:g}, "
              f"tau>{S.model_exit_min_tau_s:g}s, hold>{S.model_exit_min_hold_s:g}s"
              f"  (sold at bid - {SLIP:g}, venue floor ${_SELL_MIN:.2f} on the "
              f"SLICE, shares floored to 0.01)")
        rmap = {r["slug"]: r for r in rs}
        L = dict(min_edge=S.min_edge, min_quote=S.min_quote,
                 max_quote=S.max_quote,
                 min_range=S.model_min_round_range_bps,
                 max_range=S.model_max_round_range_bps, max_tau=S.max_tau_s)
        ship_cands = [
            ("live entry", {}, None, True),
            (f"max_quote {L['max_quote']:g}->0.80", dict(max_quote=0.80), None,
             True),
            (f"max_quote {L['max_quote']:g}->0.90", dict(max_quote=0.90), None,
             True),
            ("ev_hi 0.12->0.07", {}, dict(hi=0.07), True),
            ("ev_hi 0.12->0.20", {}, dict(hi=0.20), True),
            ("max_stake 3->2", {}, dict(max_stake=2.0), True),
            ("max_stake 3->5", {}, dict(max_stake=5.0), True),
            ("flat $1", {}, dict(min_stake=1.0, max_stake=1.0), True),
            ("flat $1.5", {}, dict(min_stake=1.5, max_stake=1.5), True),
            ("flat $2", {}, dict(min_stake=2.0, max_stake=2.0), True),
            ("min_stake 1->1.5", {}, dict(min_stake=1.5), True),
            ("boost x2 (bands)", {}, dict(boost=2.0), True),
            ("tilt inv_z", {}, dict(tilt="inv_z"), True),
            ("0.90 + flat $2", dict(max_quote=0.90),
             dict(min_stake=2.0, max_stake=2.0), True),
            ("0.90 + ev_hi 0.07", dict(max_quote=0.90), dict(hi=0.07), True),
            ("flat $2 + ev_hi 0.07", {},
             dict(min_stake=2.0, max_stake=2.0, hi=0.07), True),
            ("0.90 + flat $2 + hi0.07", dict(max_quote=0.90),
             dict(min_stake=2.0, max_stake=2.0, hi=0.07), True),
        ]
        for label, over, sz, use_exit in ship_cands:
            if args.mode != "ship":
                break                  # sizegrid only wants the machinery
            _sz[0] = {**SZ, **(sz or {})}
            ent = run2(**over)
            print(f"  {label:<24} n={len(ent)}"
                  + (f"   sizing={sz}" if sz else ""))
            _battery(ent, rmap, use_exit=use_exit, tag="with exit")
            if label.startswith("live entry") and use_exit:
                _battery(ent, rmap, use_exit=False, tag="exit OFF")
        _sz[0] = SZ

        if args.mode == "sizegrid":
            # ============ HOW MUCH PER TRADE: THE (min -> max) GRID ============
            #
            # THE QUESTION THIS ANSWERS. `stake_for_edge` puts `min_stake` on
            # the weakest accepted signal and `max_stake` on the strongest, ramping
            # linearly in decision-time ev/$. Which PAIR to ship is not answered
            # by an EV/$1 table -- that is at a constant stake by construction.
            #
            # WHAT IS HELD FIXED. The entry SET: sizing re-weights entries, it
            # does not select them, so one `run2()` (the expensive part) serves
            # every cell. What is NOT held fixed is the EXIT's viability, which
            # depends on the stake -- so every cell re-runs the exit per entry
            # at ITS OWN stake rather than scaling one flat-$2 result.
            #
            # ⛔ TWO WAYS TO GET THIS WRONG, both guarded here:
            #  * bigger stakes make more money on a profitable arm BY
            #    CONSTRUCTION. That is leverage. So `$/$1 staked` is printed
            #    beside the totals: it is the allocation-independent quality, and
            #    a cell that does not beat flat THERE is just more money at more
            #    risk.
            #  * a stake change moves the EXIT, and the exit is where the risk
            #    reduction lives (a $1 position's sale is refused by the venue's
            #    $1 notional floor the moment the bid drops; a $2 one's is not).
            #    The `exits` column is printed for exactly that reason.
            ent = [e for e in run2() if e.get("ev") is not None]
            ent.sort(key=lambda e: e["ts_ms"])
            bmap = _bids_for(ent)
            #: scalar fallback sigma per round, for `--exit-max-z` on the ~23% of
            #: rounds with no recorded live series.
            _SG0 = {r["slug"]: ao.rolling_sigma(rs, i)
                    for i, r in enumerate(rs)}

            def _grid_row(smin: float, smax: float, lo: float, hi: float,
                          tag: str = "") -> tuple[float, float, float]:
                """One (min, max) cell: dollars, drawdown, exits."""
                stks = [stake_for_edge(e["ev"], min_edge=lo, min_stake=smin,
                                       max_stake=smax, edge_at_max=hi)
                        for e in ent]
                cash, n_exit, n_skip = [], 0, 0
                rules: dict[str, int] = {}
                for e, sk in zip(ent, stks):
                    if sk < S.min_order_usdt:
                        n_skip += 1        # live SKIPS, never rounds up
                        continue
                    r, did, rule = _ret_exited(
                        e, rmap[e["slug"]], bmap.get(e["slug"], {}), sk)
                    n_exit += 1 if did else 0
                    rules[rule or "-"] = rules.get(rule or "-", 0) + 1
                    cash.append((e["day"], sk, sk * r, r))
                if len(cash) < 8:
                    print(f"  {tag or f'{smin:g}->{smax:g}':<16} n={len(cash)} "
                          f"too few to read")
                    return 0.0, 0.0, 0.0
                tot = sum(c[2] for c in cash)
                staked = sum(c[1] for c in cash)
                curve = peak = dd = 0.0
                # ⭐ AVERAGE DRAWDOWN BESIDE maxDD (operator's ask, 2026-09-23).
                # maxDD is ONE point of ONE ordering, so a single unlucky trade
                # can set it (measured: the same trades reshuffled move it
                # -18..-65 at $3.50). The time-average of the running distance
                # below the peak is a property of the whole path, so it is the
                # stable number to compare cells on; maxDD stays as the tail.
                dds: list[float] = []
                for c in cash:
                    curve += c[2]
                    peak = max(peak, curve)
                    dd = min(dd, curve - peak)
                    dds.append(peak - curve)
                avg_dd = (sum(dds) / len(dds)) if dds else 0.0
                half = len(cash) // 2
                # ⛔ THE HALVES ARE PER-$1 RETURNS, and the first version of
                # this column was DOLLARS PER TRADE -- wrong, and wrong in the
                # one place it could not be seen: it sat beside `dDay`, which is
                # per-$1, and beside `$/$1`, so one row carried two units and
                # neither was labelled. On a RAMP it is worse than a mislabel:
                # a cell whose stakes are larger in the first half looks better
                # in the first half even when its per-$1 edge is identical, so
                # the column cannot be compared ACROSS cells -- which is the
                # only reason a halves column exists here.
                # Both units are now printed: `h/x` per-$1 (allocation-free,
                # the number to compare between cells) and `$h` the money
                # actually made in each half (the number that reaches the wallet).
                h1 = _fmean([c[3] for c in cash[:half]]) if half else 0.0
                h2 = (_fmean([c[3] for c in cash[half:]])
                      if len(cash) > half else 0.0)
                m1 = sum(c[2] for c in cash[:half])
                m2 = sum(c[2] for c in cash[half:])
                by: dict[int, list[float]] = {}
                for c in cash:
                    by.setdefault(c[0], []).append(c[3])
                worst = max(by, key=lambda d: _fmean(by[d])) if by else None
                rest = [r for d, v in by.items() if d != worst for r in v]
                dday = _fmean(rest) if rest else float("nan")
                lbl = tag or f"{smin:g}->{smax:g}"
                print(f"  {lbl:<16} ${_fmean([c[1] for c in cash]):5.2f} "
                      f"n={len(cash):<4} exits {n_exit:>3}/{len(cash):<4} "
                      f"${tot:+8.2f} ${tot/max(days,1):+7.2f}/d "
                      f"avgDD ${avg_dd:+7.2f} "
                      f"P/|aDD| {tot/max(avg_dd,1e-9):6.2f} "
                      f"-100% {100*sum(1 for c in cash if c[3] <= -0.99)/max(len(cash),1):3.0f}% "
                      f"maxDD ${dd:+7.2f} tot/|DD| {tot/max(abs(dd),1e-9):5.2f} "
                      f"$/$1 {tot/max(staked,1e-9):+.4f} "
                      f"H/$1 {h1:+.3f}/{h2:+.3f} "
                      f"H$ {m1:+.2f}/{m2:+.2f} dDay {dday:+.4f}"
                      + (f"  skip {n_skip}" if n_skip else "")
                      + ("  " + ",".join(f"{k}:{v}" for k, v in rules.items()
                                          if k != "-") if n_exit else ""))
                return tot, dd, staked

            print(f"\n=== SIZING GRID: min -> max, DOLLARS with the LIVE exit "
                  f"({len(ent)} entries over {days:.2f} days) ===")
            _evlo = (S.stake_ev_lo if args.ev_lo is None else args.ev_lo)
            _evhi = (S.stake_ev_hi if args.ev_hi is None else args.ev_hi)
            print(f"  bankroll ${args.bankroll:g} | ramp is linear in ev/$"
                  f" from ${_evlo:g} to ${_evhi:g} | "
                  f"min_order ${S.min_order_usdt:g} | cap ${S.stake_cap_usdt:g}")
            _gb = (S.model_exit_behind_bps if BEHIND_OVR["bps"] is None
                   else BEHIND_OVR["bps"])
            _gh = (S.model_exit_behind_hold_s if BEHIND_OVR["hold"] is None
                   else BEHIND_OVR["hold"])
            _gtag = ("  (CLI OVERRIDE)" if (args.behind_bps is not None
                                            or args.behind_hold is not None)
                     else "")
            _gc = (float(getattr(S, "model_exit_behind_max_tau_s", 0.0) or 0.0)
                   if BMAX_OVR["tau"] is None
                   else float(BMAX_OVR["tau"] or 0.0))
            print(f"  exit: react -{S.model_exit_rev_bps:g}bps/"
                  f"{S.model_exit_look_s:g}s OR grind behind {_gb:g}bps for "
                  f"{_gh:g}s{'  (GRIND OFF = reaction alone)' if not _gb else ''}"
                  f"{'  LATE-ONLY tau<=' + format(_gc, 'g') + 's' if (_gb and _gc) else ''}"
                  f"{'  (CEILING OFF = grind from entry to bell)' if (_gb and not _gc) else ''}"
                  f"{_gtag}")
            _tr = (S.model_exit_min_tau_s if TAU_OVR["react"] is None
                   else TAU_OVR["react"])
            _tb = (_tr if TAU_OVR["behind"] is None else TAU_OVR["behind"])
            print(f"  tau floor: lead t>{_tr:g}s"
                  f"{'  (ARMED TO THE BELL)' if not _tr else ''}"
                  f" | loss t>{_tb:g}s"
                  f"{'  <-- CLI OVERRIDE' if (args.min_tau is not None or args.min_tau_behind is not None) else ''}")
            print(f"  {'min->max':<16} {'avg$':>5} {'n':>8} {'exits':>13} "
                  f"{'P&L':>9} {'$/day':>8} {'avgDD':>8} {'P/|aDD|':>7} "
                  f"{'maxDD':>8} {'t/|DD|':>6} "
                  f"{'$/$1':>7} {'H/$1':>14} {'H$':>14} {'drop-best-day':>13}")
            print("  (H/$1 = mean per-$1 return of each half -- the comparable "
                  "number; H$ = dollars made in each half)")
            mins = ([float(v) for v in args.smin.split(",")] if args.smin
                    else [1.0, 1.5, 2.0])
            maxs = ([float(v) for v in args.sms.split(",")] if args.sms
                    else [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0])
            # ⭐ WHERE DOES THE PER-$1 RETURN LIVE? The ramp's whole premise is
            # that `ev/$` RANKS the entries, so staking more on a bigger ev/$ is
            # concentration in quality. The grid cannot answer that: a cell's
            # `$/$1` is a weighted average, so a ramp that is backwards and a
            # ramp that is right can print the SAME number. This buckets the
            # entries by their OWN ev/$ and shows each bucket's per-$1 return at
            # ONE flat stake, which is the allocation-free question -- does the
            # arm make more per dollar on the trades it likes most?
            print("\n  -- ⭐ SIZING DIAGNOSTIC: is `ev/$` a ranking? (per-$1 "
                  "return by the entry's OWN ev/$, live exit at flat $1.5) --")
            for _lo, _hi in ((0.0, 0.05), (0.05, 0.055), (0.055, 0.06),
                             (0.06, 0.065), (0.065, 0.075), (0.075, 0.09),
                             (0.09, 0.11), (0.11, 0.14), (0.14, 1.0)):
                _sel = [e for e in ent
                        if _lo <= float(e.get("ev") or 0.0) < _hi]
                if not _sel:
                    continue
                _rr = [_ret_exited(e, rmap[e["slug"]],
                                   bmap.get(e["slug"], {}), 1.5)[0]
                       for e in _sel]
                print(f"    ev {_lo:.3f}-{_hi:.3f}  n={len(_sel):<4} "
                      f"mean ev {_fmean([float(e.get('ev') or 0) for e in _sel]):.4f}"
                      f"  per-$1 {_fmean(_rr):+.4f}  "
                      f"(no exit {_fmean([e['ret'] for e in _sel]):+.4f})")
            best: list[tuple[float, str, float, float]] = []
            for smin in mins:
                for smax in maxs:
                    if smax < smin:
                        continue
                    tag = (f"flat ${smin:g}" if smin == smax
                           else f"{smin:g}->{smax:g}")
                    tot, dd, staked = _grid_row(
                        smin, smax, _evlo, _evhi, tag)
                    if staked:
                        best.append((tot / max(abs(dd), 1e-9), tag, tot, dd))
            print(f"  live (({S.min_stake_usdt:g}->{S.max_stake_usdt:g}) is the "
                  f"row labelled flat ${S.min_stake_usdt:g} when min==max)")
            if best:
                best.sort(reverse=True)
                print("\n  best by tot/|maxDD|: " + ", ".join(
                    f"{t} ({r:.2f}, ${tot:+.0f})" for r, t, tot, _ in best[:4]))
                bd = max(best, key=lambda x: x[2])
                print(f"  best by P&L:        {bd[1]} ${bd[2]:+.2f} "
                      f"(maxDD ${bd[3]:+.2f} = "
                      f"{100*abs(bd[3])/args.bankroll:.0f}% of the bankroll)")
            # ⛔ `--dd-detail 10` SHOWS THE WORK behind one cell's maxDD: where
            # the peak sat, where the wallet got to, and what the same trades
            # score in a different ORDER. A single peak-to-trough figure is one
            # path, and the grid prints it as if it were a property of the rule.
            if args.dd_detail:
                _dd_detail(ent, rmap, bmap, float(args.dd_detail), _evlo,
                           _evhi, args.bankroll, _ret_exited)
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
                got = px_out * (1.0 - FEE_RATE(px_out))
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
            got = lim * (1.0 - FEE_RATE(lim))
            LIMIT["sold"] += 1
            LIMIT["px"].append(lim)
            return "limit", eff2, (got - cost) / cost, lim

        def sell_ret(e, rule, stop_c=0.0, stop_pct=0.0, confirm_s=0.0,
                     hold_s=0.0, min_tau=60.0, slip=SLIP,
                     fair_m=0.0, fair_drop=0.0, stale_ms=1000,
                     look_s=20.0, rev_bps=5.0, frac=1.0,
                     force_tau=0.0, trail_c=0.0, behind_bps=0.0,
                     behind_confirm_s=1.0, behind_min_tau: float | None = None,
                     stake: float | None = None):
            """Per-$1 return if the rule exits, else the HOLD return.

            Returns (ret, exited, blocked, px_used) -- `blocked` counts triggers
            whose sale the venue would refuse for being under its minimum.

            `stake` IS NOT COSMETIC. The venue's sell floor is a NOTIONAL, so
            whether an exit can be placed at all depends on the stake: at $1 a
            position's sale is refused the moment the bid drops, at $2 it goes
            through. The money columns must therefore be produced with the
            variant's OWN stake, not with the module constant -- which is
            exactly why this is a parameter and not a global.
            """
            if rule == "none":          # the baseline: hold to settlement
                return e["ret"], False, 0, None
            # ⛔⭐ THE ASYMMETRIC GATE. `min_tau` is a fact about the BOOK (it
            # goes one-sided at the bell) and the two halves of the exit do NOT
            # suffer from it equally:
            #   `reactmove` SELLS A LEAD. Measured on 2,811 settled live btc-5m
            #     rounds, one trigger per (round, side): below tau 30 the venue's
            #     bid on a STILL-AHEAD position is 0.791 against a 0.900
            #     realised win rate -- the venue UNDERPRICES us there, so selling
            #     that late DESTROYS value (-0.109/share). Same at 30-60s: +0.012,
            #     a wash. That is the population the disarm was measured on.
            #   `behind` SELLS A LOSER. The alternative is ZERO, and the same
            #     measurement has the bid ABOVE the realised win rate at EVERY
            #     tau (+0.002 at 0-30s ... +0.019 above 180s).
            # So the two deserve different floors: `behind_min_tau` is the floor
            # the LOSS half watches to. `None` = the shipped behaviour (one floor
            # for both). Only the two rules that can be a LOSS trigger may watch
            # past `min_tau`; every other rule keeps the shipped floor.
            bt = (min_tau if behind_min_tau is None
                  else float(behind_min_tau))
            if rule not in ("behind", "either"):
                bt = min_tau
            r = by_slug[e["slug"]]
            side, strike = e["side"], r["strike"]
            bmap = bids.get(e["slug"]) or {}
            cost = e["cost"]
            shares = (STAKE if stake is None else float(stake)) / cost
            # the (ts, spot) series once, for the reaction move
            spots = [(x[0], x[2]) for x in r["rows"] if x[2]]
            sgn = 1.0 if side == "Up" else -1.0
            # the sigma the arm actually had, for the per-tick fair
            _i = idx_of.get(e["slug"])
            sg0 = sigmas[_i] if _i is not None else 0.0
            sseries = live_sig.get(e["slug"]) if args.use_live_sigma else None
            held, blocked = 0.0, 0
            peak_bb = 0.0
            behind_run = 0
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
                # `min(min_tau, bt)`: the watch stops only when NEITHER half can
                # still act. tau falls monotonically, so a break is final.
                if tau is not None and tau <= min(min_tau, bt):
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
                elif rule == "behind":
                    # ⭐ SELL ONCE THE LEAD IS GONE -- the GRIND stop.
                    # `reactmove` protects a lead that is being SNAPPED away: it
                    # needs `log(S/K)*sgn > 0` (there is a lead to protect) AND a
                    # `rev_bps` move inside `look_s`. A slow bleed produces
                    # neither, and the moment the spot crosses the strike the
                    # lead guard turns the exit off altogether.
                    #   MEASURED LIVE 2026-09-22 23:17 (btc-updown-5m-1790118900
                    #   Up, $2 at 0.647): the worst 20s move against it over the
                    #   WHOLE round was -3.47bps, the exit never fired, and the
                    #   position settled `won=0 payout=0.0000 pnl=-2.0000`.
                    # This is a STATE, like `reactmove`, not an event.
                    if not (spot and strike and tau and tau > 0):
                        continue
                    hit = math.log(spot / strike) * 1e4 * sgn <= -behind_bps
                elif rule == "either":
                    # WHICHEVER FIRES FIRST: the reaction (protect a lead being
                    # snapped away) OR the grind stop (the lead is already gone).
                    # One rule, one sale -- the one-sale rail is unchanged.
                    #
                    # ⛔ THE TWO HALVES MUST NOT SHARE A CONFIRMATION. The
                    # reaction is a SNAP: it is worth ~1.5bps/share of the arm's
                    # edge (measured: react exits win 29.5% of the time, i.e. the
                    # trigger is an adverse move) and delaying it by seconds
                    # changes which trades it catches. The grind half is a STATE
                    # that can be true on one noisy print, so IT gets the delay.
                    # That is why `behind_confirm_s` is separate from `confirm_s`.
                    if not (spot and strike and tau and tau > 0):
                        continue
                    lead_bps = math.log(spot / strike) * 1e4 * sgn
                    if lead_bps > 0:
                        behind_run = 0        # back ahead: the grind clock resets
                        # THE ASYMMETRY, APPLIED: while we are still AHEAD this
                        # half is `reactmove`, and it obeys ITS OWN floor. Below
                        # it the lead half is disarmed and only the loss half
                        # below can fire.
                        if tau <= min_tau:
                            hit = False
                            continue
                        past = [s for t, s in spots if t <= ts - look_s * 1000]
                        hit = bool(past) and (
                            math.log(spot / past[-1]) * 1e4 * sgn <= -rev_bps)
                    else:
                        behind_run = (behind_run + 1
                                      if lead_bps <= -behind_bps else 0)
                        hit = behind_run >= behind_confirm_s
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

        def erow(label, rule, stake_amt: float | None = None, **kw):
            # `stake_amt` is NOT cosmetic: the venue's sell floor is a NOTIONAL,
            # so whether an exit can be placed at all depends on the stake (at
            # $1 a position's sale is refused the moment the bid drops, at $2 it
            # goes through). A row's money columns must therefore be printed at
            # the SAME stake its exit was priced at, not at the module constant.
            _st = STAKE if stake_amt is None else float(stake_amt)
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
                ret, ex, bl, pxo = sell_ret(e, rule, stake=stake_amt, **kw)
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
                  f"tot@${_st:g} ${_st*sum(out):+7.2f}  "
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
                eq += _st * r_
                peak = max(peak, eq)
                mdd = min(mdd, eq - peak)
                streak = streak + 1 if r_ < 0 else 0
                worst_streak = max(worst_streak, streak)
            tail5 = st.fmean(sorted(out)[:5]) if len(out) >= 5 else float("nan")
            ratio = (_st * sum(out)) / abs(mdd) if mdd < 0 else float("inf")
            mid_ = len(out) // 2
            mdd1, mdd2 = _maxdd(out[:mid_]), _maxdd(out[mid_:])
            print(f"  {label:<24} maxDD ${mdd:+6.2f}  "
                  f"tot/|maxDD| {ratio:5.2f}  worst streak {worst_streak:<3} "
                  f"mean-worst-5 {tail5:+.4f}  "
                  f"({_st*5*tail5:+.2f} on the 5 worst)  "
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
        # ⭐⭐ THE EXIT WINDOW IS `min_tau`, AND IT IS WHAT LOST THE 23:17 TRADE.
        # On `btc-updown-5m-1790118900` (23:17:51 entry, $2 Up) the spot stayed
        # AHEAD of the strike until t=243 (tau 57) and crossed only at t=244
        # (tau 56, -1.61bps). `min_tau 60` had already disarmed every rule, so
        # that trade was UN-EXITABLE BY CONSTRUCTION -- not by choice of trigger.
        # Any rule that only fires while the side is AHEAD is dead in exactly
        # the rounds that turn against us last. This sweeps the window itself.
        print("  -- ⭐⭐ THE EXIT WINDOW (min_tau): the 23:17 loss crossed the "
              "strike at tau 56, where min_tau 60 had already disarmed --")
        for mt in (60.0, 45.0, 30.0, 20.0, 10.0):
            erow(f"react 20s/5bps tau>{mt:g}s", "reactmove",
                 look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=mt)
        print("  -- ⭐ ...and the COMBINED rule at the same windows --")
        for mt in (45.0, 30.0, 20.0):
            erow(f"react OR behind2bps/5tk t>{mt:g}s", "either",
                 behind_bps=2.0, behind_confirm_s=5.0,
                 look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=mt)
        # ⭐⭐ THE ASYMMETRIC WINDOW, WHICH IS THE SHAPE THE TWO LIVE LOSSES
        # ACTUALLY HAD. `min_tau` is one floor for two rules that are NOT the
        # same bet: `reactmove` sells a LEAD (and the venue underprices a
        # still-ahead position below tau 30 -- 0.791 bid vs 0.900 realised -- so
        # selling there destroys value), while `behind` sells a LOSER whose only
        # alternative is ZERO (and the bid beats the realised win rate at every
        # tau). So: keep the LEAD half at tau>60 and let the LOSS half watch to
        # the bell. 2026-09-22 23:17 crossed at tau 56 and 2026-09-23 10:34:05
        # reacted at tau 54 -- both inside the window this opens.
        print("  -- ⭐⭐ THE ASYMMETRIC GATE: lead half at tau>60, LOSS half "
              "watching to the bell --")
        for bt in (60.0, 45.0, 30.0, 15.0, 0.0):
            erow(f"lead t>60 / loss t>{bt:g}s", "either",
                 behind_bps=2.0, behind_confirm_s=5.0,
                 look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=60.0,
                 behind_min_tau=bt)
        print("  -- ⭐ ...and the same gate at the LIVE flat $1 stake (the "
              "venue's $1 floor is what decides whether a late sale is "
              "placeable at all) --")
        for bt in (60.0, 30.0, 0.0):
            erow(f"lead t>60 / loss t>{bt:g}s @$1", "either",
                 behind_bps=2.0, behind_confirm_s=5.0,
                 look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=60.0,
                 behind_min_tau=bt, stake_amt=1.0)
        print("  -- ⭐ ...and the LOSS half alone (no reaction rule at all), "
              "whose granularity is the confirmation count, not seconds --")
        for bt in (30.0, 0.0):
            for c in (1.0, 2.0, 5.0):
                erow(f"behind2bps/{c:g}tk t>{bt:g}s", "behind",
                     behind_bps=2.0, behind_confirm_s=c, hold_s=5.0,
                     min_tau=60.0, behind_min_tau=bt)
        print("  -- ⭐⭐ PROTECTION IN EVERY WINDOW: the WINDOW is not the knob, "
              "the RULE is. `bid >= fair` only sells when the venue pays MORE "
              "than our own fair value, so it is a profitable sale BY "
              "CONSTRUCTION and can be armed to the bell (tau 0) without the "
              "'selling a winner cheap' problem the reaction rule has --")
        for mt in (60.0, 30.0, 0.0):
            erow(f"react 20s/5bps t>{mt:g}s", "reactmove",
                 look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=mt)
        for mt in (60.0, 30.0, 0.0):
            for m in (0.0, -0.02):
                erow(f"bid>=fair{m:+.2f} t>{mt:g}s", "fair", fair_m=m,
                     hold_s=5.0, min_tau=mt)
        if args.limit_fallback:
            # ⭐⭐ WHAT THE LIVE FAILURE ON 2026-09-23 11:29 ACTUALLY NEEDS.
            # `btc-updown-5m-1790162700`: the exit fired at tau 16.3 on a
            # -5.3bps/20s wobble while the position was 6.7bps IN THE MONEY, the
            # venue quoted the sale at 0.9300, and the FOK came back
            # `fak_unfilled` -- no fill at all, despite a bid of 0.93 on the tape
            # (the likeliest cause is DEPTH: FOK is all-or-nothing and the order
            # was 5.40 shares). One tick later the 20s window had gone flat, so
            # the rule stopped holding and there was no second attempt. The
            # position rode to the bell and won +$1.91 by luck.
            # The fix to price is therefore NOT a better price, it is a SECOND
            # CHANCE at the same trigger: a resting LIMIT at the touch, which is
            # the one form that can still fill after the market order is killed.
            # ⚠️ At a $1-2 stake this alternative does not exist: the venue's
            # LIMIT minimum is 5 SHARES and $1 buys 1.4-3, so a resting sell is
            # only placeable at the bigger stake -- which is another reason the
            # stake and the exit cannot be decided separately.
            print("  -- ⭐⭐ A RESTING LIMIT AT THE TOUCH WHEN THE MARKET KILL "
                  "LEAVES US WITH NOTHING (the 11:29 failure) --")
            for _st in (2.0, 3.5, 5.0):
                erow(f"react t>0 +lim @${_st:g}", "reactmove",
                     look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=0.0,
                     stake_amt=_st)
        print("  -- ⭐ THE GRIND STOP: sell once the lead is GONE (a state, so a"
              " slow bleed cannot hide from it) --")
        for b in (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0):
            erow(f"behind strike {b:g}bps", "behind", behind_bps=b,
                 hold_s=5.0, min_tau=60.0)
        print("  -- ⭐ GRIND STOP + reaction: whichever fires first --")
        for b in (1.0, 3.0):
            erow(f"react OR behind {b:g}bps", "either", behind_bps=b,
                 look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=60.0)
        print("  -- ⭐ ...with the GRIND half gated by a confirmation (the "
              "reaction half stays immediate) --")
        for b in (0.5, 1.0, 2.0):
            for c in (3.0, 5.0, 10.0):
                erow(f"react OR behind {b:g}bps/{c:g}tk", "either",
                     behind_bps=b, behind_confirm_s=c,
                     look_s=20.0, rev_bps=5.0, hold_s=5.0, min_tau=60.0)
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
