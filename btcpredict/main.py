"""Orchestrator.

Modes:
  record  feeds + storage only, no signals, no orders. This is Phase 0 and is
          the default -- it is what answers the open questions about effective
          spread and fee semantics.
  paper   full signal path against the simulated broker.
  live    same path against the real venue. Refuses to start unless the live
          endpoints have actually been configured.

The loop does two independent jobs each tick: record the current round, and
settle any positions whose rounds have closed. Settlement is driven off stored
fills rather than in-memory state so a restart cannot orphan a position.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import dataclasses
import logging
import math
import signal
import time
from types import SimpleNamespace

import aiohttp

from btcpredict.calibration.report import PnLSummary
from btcpredict.edge.impulse import impulse_bps, impulse_side
from btcpredict.config import (CROSS_TRADED, MARKETS, SYMBOL_BY_PREFIX,
                               TRAIN_P_WIN, active_markets, cross_knob,
                               cross_traded, market_prefix, settings)
from btcpredict.edge.signal import (FLOOR_MAX_BANKROLL_FRACTION, FeeModel,
                                    Signal, evaluate,
                                    evaluate_hybrid, expected_value,
                                    fee_rate_for_price, impulse_price_refusal,
                                    kelly_stake, limit_post_px, take_limit_px,
                                    depth_sigma,
                                    stake_for_edge, stake_for_fraction,
                                    tilt_factor)
from btcpredict.execution.base import Fill
from btcpredict.execution.live import RETRYABLE_FAILURES, LiveBroker
from btcpredict.execution.paper import PaperBroker
from btcpredict.venues import registry
from btcpredict.venues.polymarket.broker import entry_ceiling
from btcpredict.venues.polymarket.pricing import (TWAP_LOOKBACK_S,
                                                  window_log_integral)
from btcpredict.feeds.binance_spot import SpotFeed, fetch_klines
from btcpredict.feeds.prediction_book import PredictionBook
from btcpredict.feeds.market_feed import MarketFeed, Round
from btcpredict.models import PriceHistory, build_model
from btcpredict.pricing.digital import fair_value, implied_sigma, moneyness_sd
from btcpredict.pricing.vol import VolEstimator
from btcpredict.risk.guards import RiskGuard
from btcpredict.store.db import Store

log = logging.getLogger("btcpredict")


def _report_task_failure(task) -> None:
    """Log a fire-and-forget task's exception instead of losing it.

    An un-awaited `asyncio.Task` that raises prints NOTHING: the exception just
    sits on the task. The first version of the warm metadata prefetch raised
    `AttributeError` for thirteen minutes of live trading and the only symptom
    was silence -- which reads exactly like "the warm is not needed".
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("background task failed: %s: %s", type(exc).__name__, exc)


def _queue_warm_tokens(owner, toks) -> list[str]:
    """Add a family's tokens to the pending warm set; return the batch to send.

    ⛔ THE QUEUE IS THE FIX, SO IT IS A FUNCTION RATHER THAN THREE LINES INSIDE
    `_record` -- the defect it replaces was invisible and the property that
    matters (a family cannot be starved out of the queue) is directly testable
    here and would not be inline.

    WHY IT EXISTS. `_record` runs for every family in `MARKETS` order and
    `MARKETS[0]` is the PRIMARY. The dispatch used to be
    `if toks and (task is None or task.done())` against ONE shared
    `self._warm_task`: the primary re-dispatched on every sweep, so the task was
    never `done()` when the next family was checked, and the other eight were
    skipped forever. MEASURED live 2026-09-17 -- the `warm` counter moves +2 per
    5-minute rollover (18, 18, 18, 18, 20, 22, 22, 22, 22, 24, 26) against 18
    tokens a round, and two tokens a round is one family's round.

    Returns [] while a warm is in flight; the tokens stay in the set and go out
    with the next batch. Nothing is ever dropped, which is the property the old
    gate lacked.

    `getattr`/`isinstance` guards rather than bare attributes: fixtures build a
    Bot via `__new__`, which skips `__init__` -- the trap this file already hit
    twice (see `_cross_fired`).
    """
    pend = getattr(owner, "_warm_pending", None)
    if not isinstance(pend, set):
        pend = owner._warm_pending = set()
    pend.update(toks or ())
    task = getattr(owner, "_warm_task", None)
    if not pend or (task is not None and not task.done()):
        return []
    batch = sorted(pend)
    pend.clear()
    return batch


# See `_log_warm_done`: the pre-resolve success line is INFO once per process and
# DEBUG after that. Process-wide rather than per-Bot because `_log_warm_done` is a
# done-callback and is handed only the task.
_WARM_LOGGED: bool = False


def _log_warm_done(task) -> None:
    """Say how many tokens the pre-resolve put in the SDK's cache, when > 0.

    `warm()` returns the count it RESOLVED, and 0 means "all already warm" --
    the steady state -- so this is silent then rather than a line per sweep.

    It exists because the failure it replaces was invisible in exactly the way
    `_report_task_failure`'s docstring describes: nothing errored, and the
    `warm` counter climbing by a couple each round read as "working" when it
    was ONE family out of nine (see the `_warm_pending` note in `_record`).
    A number in the log is what turns that from an inference into a fact.

    Never raises: it is a done-callback, and an exception raised here would be
    lost the same way the warm's own was.

    INFO ONCE, THEN DEBUG. The pre-resolve runs for ~18 tokens at every 5-minute
    rollover (9 families x 2 sides), so left at INFO this SUCCESS line was
    MEASURED at **220 of the ~1,600 lines in a 3-hour live.log window** -- the
    largest single source of noise in the file, and every one of the 220 said
    "working". The first is worth keeping at INFO: it is the line that proves the
    queue fix actually ran, which is the one thing that was invisible before. The
    running total stays visible as `warm N` on the once-a-minute liveness line.
    """
    global _WARM_LOGGED
    try:
        if task.cancelled() or task.exception() is not None:
            return                     # _report_task_failure reports that one
        n = task.result() or 0
        if not n:
            return
        if _WARM_LOGGED:
            log.debug("pm order metadata: pre-resolved %d token(s)", n)
        else:
            _WARM_LOGGED = True
            log.info("pm order metadata: pre-resolved %d token(s); orders on "
                     "every family should now pay no lookup", n)
    except Exception:                  # noqa: BLE001
        pass

# How many consecutive venue reads must disagree with the bankroll ledger before
# the venue's figure is believed over our own accounting. One or two is the
# redemption lag (a stake leaves the wallet immediately, the payout lands later);
# a run this long is a deposit, a withdrawal, or fees the ledger does not model.
_BANKROLL_DRIFT_READS = 5

PROBE_MODE = "probe"

# How often the winnings-claim sweep runs (see Bot.sweep_claims): the retry
# clock for claims the venue rejected as not-yet-final. The on-win redeem
# stays immediate; this only has to catch what that one missed.
_CLAIM_SWEEP_S = 180.0

# How often wallet equity is re-read EVEN WHEN NOTHING SETTLES (see
# Bot.refresh_bankroll_if_stale). 60 s, so a deposit is live within a minute.
# Cheap because the PM broker's `equity()` is cached and only SPENDING
# invalidates that cache -- this clock is the one thing that forces a real read
# (~260 ms, in the same phase window as the claim sweep measured at 273 ms).
_BANKROLL_REFRESH_S = 60.0

# ⛔ WHEN ONE TICK IN 120 TAKES TEN SECONDS, NAME THE PHASE, DO NOT THEORISE.
# `poll cadence` has reported `median 34ms p90 50ms max 11964ms` -- a single
# outlier tick per window -- in every window since the log began at 20:15 on
# 09-16. Four explanations were ruled out by MEASUREMENT before this went in:
#   the host          4 vCPU, load 0.41, the bot at 18% of one core
#   a blocked loop    the WS drain EMPTIES its queue (139-421 frames against a
#                     512 cap), so the reader task ran throughout any stall
#   Store.counts()    18 ms warm, timed against the live 502 MB DB
#   the lag itself    the ~120 ms floor is venue DELIVERY latency, measured in
#                     book.py by changing only the backlog cap
# What is left is one of the four phases in `Bot.run`, three of which touch the
# network, and the only honest way to pick one is to have the slow tick say
# which it was. It did, on its very first fire (2026-09-17 09:38:38):
#     slow tick 8.39s: tick 32ms | backfill 8084ms | settle 0ms | claims 273ms
#                      | after 0ms
# `settlement backfill` was the answer, and it is now DISPATCHED rather than
# awaited (see the call site), so this line should not name it again: a
# `backfill` mark that is no longer ~0ms means someone put the await back.
# The same instrument then convicted the other three phases -- `claims` 472.3s,
# `settle` 171.0s, `bankroll` 11.0s across 121 slow ticks -- and they are now
# one dispatched task (`_housekeeping_async`). So `housekeep` is held to the
# same standard: a mark that is no longer ~0ms means someone put THAT await
# back. What remains awaited here is `tick()`, which is the work itself.
# `t0`/`elapsed` deliberately EXCLUDE `_idle`, so a tick that is merely waiting
# on the feed or the clock can never appear here.
# 2 s is far above the 43 ms median and above the 384 ms max of a clean window,
# so this fires on a stall and not on ordinary jitter.
_SLOW_TICK_MS = 2_000.0


def _phase_breakdown(t0: float, marks: list[tuple[str, float]],
                     end: float) -> str:
    """`tick 43ms | settle 9021ms | after 2ms` -- which phase ate the tick.

    Each phase's cost is the gap to the NEXT mark, not to the tick's start, so
    the numbers are exclusive and sum to the tick. An empty `marks` means the
    tick raised before completing a single phase, which is itself the answer
    (a hang inside `tick()`), so it is said rather than reported as a gap from
    `t0` that would read as if some later phase were to blame.
    """
    if not marks:
        return "no phase completed (tick raised or hung) in %.2fs" % (end - t0)
    prev, parts = t0, []
    for name, at in marks:
        parts.append("%s %.0fms" % (name, (at - prev) * 1000.0))
        prev = at
    # Everything after the last completed phase: the `stats` block when it is
    # due, nothing when it is not. `after` is honest about both.
    parts.append("after %.0fms" % ((end - prev) * 1000.0))
    return " | ".join(parts)

# A MARKET attempt needs the round open only long enough to place and execute
# (~1-2s); its order/history verdict may land after the bell. `order_min_tau_s`
# was written for what a RETRY spans with the 9s status budget, which is the
# wrong requirement for an immediate FOK -- and the venue's book demonstrably
# processes fills into the close (live fills are booked with 0.0-0.6s of round
# left). For market orders the retry floor is this instead; see _place_order.
_MARKET_RETRY_TAU_FLOOR_S = 3.0

# How stale the nearest spot tick may be, before the round's OPEN, for
# `_pm_strike_backfill` to accept it as that round's strike. It is a SAFETY
# bound, not an accuracy target: on a live websocket the newest tick is 10-50 ms
# old (BTC bookTicker runs 33-114 updates/s), so this only ever fires when the
# tape has stalled or the process booted after the round opened -- and both of
# those must produce NO strike rather than a price from the wrong minute. The
# error from being `d` seconds early is ~sigma_1m*sqrt(d/60): 0.1-0.2 bps at a
# tick's lag, ~1.8 bps for a whole 2 s at a volatile coin's 10 bps/min.
_PM_STRIKE_TICK_TOL_MS = 2_000

# Quote warm-up for the cross arm (see Bot._prefetch_cross_quote): refresh at
# most this often while a family is in its tradeable window, and consume the
# warm quote only if younger than the max age. The venue's quote TTL is ~5s,
# so 2.5s keeps a wide margin.
_QUOTE_PREFETCH_REFRESH_S = 1.0
_QUOTE_PREFETCH_MAX_AGE_S = 2.5

# Reasons a decision was declined that must NOT be probed. These are real
# limits -- existing exposure, a risk halt, or data we could not read -- and
# probing around them would create exactly the double exposure and the
# post-halt buying that the gate exists to prevent. Everything else the gate
# declines is selectivity, which is what the probe arm is for.
PROBE_NEVER = ("venue", "already positioned", "halt", "loss", "streak",
               "bankroll", "stale", "unavailable", "unreadable",
               "unconfirmed", "no position")


def probeable(reason: str) -> bool:
    """Is this declined decision safe to take as a small measurement trade?"""
    low = (reason or "").lower()
    if not low:
        return False
    return not any(bad in low for bad in PROBE_NEVER)


def stale_maker_px(display: float | None, ask: float | None, fair_side: float,
                   offset: float, tick: float = 0.01) -> float | None:
    """Where to REST when the venue's display is stale, or None if nowhere.

    Three ceilings, and the binding one wins:
      * `display + offset` -- the stale price is the whole point; the offset is
        the fill-rate/price knob (see config.stale_maker_offset);
      * `fair_side` -- never post above what we think it is worth;
      * `ask - tick` -- a maker order must REST. Posting at or above the ask
        crosses, which pays the spread AND the taker fee, and lands in exactly
        the bucket the live record shows losing money (-0.0108/$1).

    Pure, so the rule is testable without a venue or a book.
    """
    if display is None or ask is None:
        return None
    if not (0.0 < display < 1.0) or not (0.0 < ask < 1.0):
        return None
    if not (0.0 < fair_side < 1.0):
        return None
    limit = round(min(display + offset, fair_side, ask - tick), 2)
    if limit < 0.01 or limit >= ask:
        return None
    return limit


def pool_for_mode(mode: str | None) -> tuple[bool, str | None]:
    """Which risk budget a fill's MODE belongs to: (reserved, pool).

    Arms are independent -- one arm's position must never refuse another's --
    so every arm books into a budget of its own and releases that same budget
    on settle. Getting this mapping wrong in either direction is expensive:
    releasing the wrong pool leaves a slot occupied forever (the arm goes
    permanently silent), and claiming the wrong one lets an arm double.

    'maker' is the impulse arm's resting path, not a separate arm, so it shares
    the impulse budget. 'train' is not a separate arm either: the Train arm
    claims the same reserved slot as Cross (`_cross_fired`) and is released by
    the same path, so it MUST book and release the same budget. Listing it here
    is what stops the two arms' fill labels from becoming two pools -- a pool
    split without a matching `_cross_fired` split would leave the reserved slot
    claimed by one arm and released by another, i.e. silent forever.
    Everything else -- 'live', 'stale_maker', 'double', 'paper' -- is the main
    arm under a different execution label.
    """
    if mode in ("cross", "train"):
        return True, None
    if mode in ("impulse", "maker"):
        return False, "impulse"
    return False, None


def arm_of(sig) -> str:
    """The UPPERCASE arm label for a signal, for lines shared by both arms.

    The Train arm is the only one whose `reason` names it ("train into ...",
    "train cooldown ..."), so that string is the discriminator. Both arms send
    their orders through `_place_order`, so without this the retry, decline and
    no-fill lines read identically whichever arm produced them, as did OPEN.
    """
    return ("TRAIN" if str(getattr(sig, "reason", "")).startswith("train")
            else "CROSS")


def pm_take_via_fak(settings, broker, limit_ceiling) -> bool:
    """True when a Polymarket TAKE arm must skip `buy_limit` and go to `buy_fak`.

    `PmBroker.buy_limit` posts with `post_only=True` -- a MAKER order. A take arm
    cannot use it. A post-only order is REJECTED whenever its limit reaches the
    ask ("invalid post-only order: order crosses book", logged live), and when it
    does not reach the ask it RESTS and waits for the market to come back to it.
    Measured on the two orders that got through after the stake-cap fix
    (2026-09-17 03:44, eth-updown-15m and eth-updown-5m): `limit post ... 0.84`
    against a quote of 0.8227 -> POST accepted, the order rested, and it expired
    unfilled after 6053 ms while the SAME round's fine trace read 0.970 five
    milliseconds after the fire and 0.99 by the end. Both arms' cells are priced
    AT THE ASK (`tools/pm_ask_at_trigger.py`: what pays is win - ask - fee), so on
    Polymarket they have to TAKE.

    `buy()` IS the taker path, and with `pm_order_type = "fak"` it routes to
    `buy_fak()`: FAK == IOC, atomic, remainder KILLED, so no leftover order is
    left resting as bait. It costs +153 ms measured (414 vs 261 ms) against a
    horizon that is already binding, so it is opt-in -- and this returns True only
    when all three hold: the knob says "fak", the broker HAS `buy_fak`
    (Polymarket; the Binance wrapper rejects IOC outright with -1102 and has no
    such method), and the caller is a TAKE arm (`limit_ceiling` is set by the
    cross/train path and is None for the model arm, whose limit is a real resting
    bid).

    WHAT IT DOES NOT FIX: a ceiling that is already stale. In the 03:44 orders the
    ceiling (0.84) sat BELOW the venue's ask by the time the POST landed, so a FAK
    would also have come back `fak_unfilled`. This makes the order sendable; it
    does not make it fill.
    """
    if limit_ceiling is None:
        return False
    if not hasattr(broker, "buy_fak"):
        return False
    return str(getattr(settings, "pm_order_type", "limit")).lower() == "fak"


async def venue_top(token: str):
    """The VENUE's own top of book for one token, or None. NEVER raises.

    `GET /book?token_id=` is READ-ONLY and it is the only source in this process
    that can disagree with our own ladder -- which is what makes it the right
    base for a take price. MEASURED 2026-09-18: `sweep()` read 0.42 for a Down
    token while the venue's own book read 0.69 AT THE SAME INSTANT, the limit
    went out at 0.54 off the 0.42, and the FAK answered "no orders found to
    match". Our ws book had a phantom level the venue did not have.

    Same call the shadow probe makes (`Bot._shadow_quote`), same timeouts, and
    deliberately NOT shared with it: a probe that cannot report its own absence
    is not a measurement, so it keeps its own failure line rather than this one
    swallowing the difference between "no book" and "the read failed".

    Returns None on any failure -- the caller falls back to its old base and logs
    it, because an unreadable venue book is not a reason to refuse a trade the
    arm's own gates already approved.

    ⛔ TIGHTER TIMEOUTS THAN THE PROBE'S. The probe sits on a task of its own and
    can afford 3/4 s; this one is INSIDE the order path, where a hung connect
    would push the order towards the bell. Measured: a book fetch is 50-300 ms,
    so 2 s is generous -- and with the entry floor (`order_min_tau_s`, 5 s) it
    keeps the worst case (2 s fetch + ~0.7 s FAK) inside the round with margin.
    """
    try:
        import aiohttp

        from btcpredict.venues.polymarket.book import PmBook
        to = aiohttp.ClientTimeout(total=None, connect=2.0, sock_read=2.0)
        async with aiohttp.ClientSession(timeout=to) as s:
            return await PmBook.fetch_top(s, token)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("venue book fetch failed (%s): %s: %s", token[:14],
                    type(exc).__name__, exc)
        return None


def ws_follow_rounds(round_cache, mkt, primary, follow_all) -> list:
    """The rounds the book socket must subscribe to.

    ⛔ NOT `_mkt`. `_mkt` is filled BY `_record`, and `_record` needs a BOOK;
    the book arrives from this very socket. Subscribing from `_mkt` closes that
    loop on itself -- a family that is not priced can never be subscribed, and a
    family that is never subscribed can never be priced. MEASURED 2026-09-17:
    `pm book ws connected (12 assets)` = 6 families (btc/eth/bnb x 5m/15m), and
    sol/xrp/doge sat at `not live` for hours with every one of their triggers
    refused as `no ask on the book` -- an ABSENCE, so nothing logged it as an
    error and the log looked like "the market is quiet".

    `round_cache` is the family set that exists BEFORE any book does -- it is
    what the once-a-minute liveness line already reports as "9/9 cached" -- so it
    is the source that breaks the loop. `PmBookView.follow` expands a Round into
    its two token ids itself (up first), so every cached family is subsumed by
    one socket.

    Order: every cached round when `follow_all`; else the priced rounds filtered
    to the primary family; else the primary's cached round; else [] (which
    `follow` treats as "leave the subscription alone").
    """
    if follow_all:
        rs = [r for r in (round_cache or {}).values() if r is not None]
        if rs:
            return rs
    rs = [c["round"] for _pfx, c in (mkt or {}).items()
          if c.get("round") is not None and (follow_all or _pfx == primary)]
    if rs:
        return rs
    r0 = (round_cache or {}).get(primary)
    return [r0] if r0 is not None else []


def crossed_side(ticks, strike: float, look_s: float,
                 slack_s: float = 2.0, hold_s: float = 0.0) -> str | None:
    """Which side did the spot just cross INTO, or None if it did not.

    `ticks` is chronological (ts_ms, price). Returns "Up"/"Down" when the newest
    tick sits on the opposite side of `strike` from the newest tick at least
    `look_s` older -- i.e. the in-the-money side just swapped.

    The reference tick must not be STALER than look_s + slack: an old crossing
    is not a fresh one, and the venue has had time to reprice it. Same guard
    `impulse_bps` applies for the same reason.

    `hold_s` IS THE PERSISTENCE REQUIREMENT, and it exists because ONE PRINT is
    not a crossing. MEASURED 2026-09-18 on `btc-updown-5m-1789711500`: the spot
    sat at 77552.85 for 25 s, printed **77560.04 for a single second** (+0.33 bps
    over the level, and past tau=60 `fc` IS the spot, so that print was the whole
    signal), and was back under the level 5 s later. The venue repriced to the
    print -- 0.51/0.50 -> 0.60/0.41 -- so the FAK paid 0.600 for a book that was
    0.41 five seconds later, and the round finished -4.60 bps against the level.
    The old rule's only freshness requirement was look_s = 1.0 s, so a lone wick
    was enough.

    With `hold_s > 0` the side must have been HELD for at least that long --
    the trailing run of the newest tick's side, measured back to the last tick
    on the other side -- and the crossing may then be at most `hold_s + look_s`
    old, so a sustained cross is not refused for being stale by the time it is
    proven. `hold_s = 0` keeps the original path verbatim (a dense oscillating
    tape must not change meaning under a zero knob).
    """
    if not ticks or strike is None or strike <= 0:
        return None
    ts_now, px_now = ticks[-1]
    if not px_now or px_now <= 0:
        return None
    side_now = "Up" if px_now > strike else "Down"
    if hold_s <= 0:
        target = ts_now - int(look_s * 1000)
        oldest = ts_now - int((look_s + slack_s) * 1000)
        for ts, px in reversed(ticks[:-1]):
            if ts > target:
                continue
            if not px or px <= 0 or ts < oldest:
                return None
            return (side_now
                    if ("Up" if px > strike else "Down") != side_now else None)
        return None
    # THE TRAILING RUN of `side_now`, back to the last tick on the other side.
    run_start = ts_now               # the newest tick alone is a run of zero
    other_ts = None
    for ts, px in reversed(ticks[:-1]):
        if not px or px <= 0:
            continue
        if ("Up" if px > strike else "Down") == side_now:
            run_start = ts
            continue
        other_ts = ts
        break
    held_ms = ts_now - int(run_start)
    if held_ms < int(hold_s * 1000.0):
        return None                  # a print, not a crossing: it did not hold
    if held_ms > int((hold_s + look_s) * 1000.0):
        return None                  # held, but the crossing is no longer fresh
    if other_ts is None:
        return None                  # the tape starts inside the run
    if run_start - other_ts > int(slack_s * 1000.0):
        return None                  # a gap, not a measured transition
    return side_now
    return None


#: Tolerance for the tick-depth floor, IN TICKS. See `passes_depth_floor`.
DEPTH_TICKS_EPS = 1e-6

#: The window Polymarket's payout rule averages: `{twapLookbackSeconds: 60}` on
#: the `chainlink-<sym>-usd-twap-60s` stream. See config.cross_ref_twap.
TWAP_REF_LOOKBACK_S = 60.0

#: How far the tape may miss the window's OPEN and its CLOSE and still be
#: averaged. Both are bounds on a MEASUREMENT, not accuracy targets: the
#: reference is a 60 s mean, so a sample missing off one end shifts the mean by
#: (gap/60) x (the price move over that gap) -- ~0.2 bps for a 2 s gap on a
#: 5 bps/minute coin. Wider than this and the window is not the window.
TWAP_REF_EDGE_TOL_MS = 2_000
TWAP_REF_LAST_TOL_MS = 3_000

#: Fewest samples the window must hold. At the live spot rates (33-114
#: updates/s) a full 60 s window holds thousands; at the 1 Hz poll cadence it
#: holds ~60. Below this the tape does not describe a 60 s average at all.
TWAP_REF_MIN_N = 4

#: Attempts at reading the reference window off the 1 s KLINES when the spot
#: tape cannot cover it (a process that booted after the open). Three, one per
#: poll: the read is a single small REST call and the round lasts minutes, so
#: the budget is about surviving a transient failure rather than about cost.
#: After it, `_cross_level` uses `r.strike` and says so at WARNING -- measured
#: 2026-09-18 to be a real risk, not the "no new risk" the old note claimed.
CROSS_LEVEL_KLINE_TRIES = 3

#: How long after a CLAIM a spent-slot tick is still reporting THAT entry rather
#: than a second crossing. `_cross_fired` is stamped with the claim, before the
#: order, so the next tick evaluating the same crossing finds the slot spent.
#: MEASURED 2026-09-18 over the Polymarket era, each cap row against its round's
#: own `cross into` row (n=40): 35 within 1.43 s, one at 5.5 s, and the three
#: genuinely later crossings at 10.4 / 23.8 / 41.6 s. The band 5.5-10.4 s is
#: EMPTY, so this is a separator rather than a fitted value -- anything in it
#: gives the same answer. Only the RECORD is suppressed; the cap itself is
#: unchanged, and the entry being reported still has its own `cross into` row.
CROSS_DUP_TICK_MS = 7_000


def twap_reference(ticks, start_ms: int,
                   look_s: float = TWAP_REF_LOOKBACK_S) -> float | None:
    """The level Polymarket settles against, from the spot tape: or None.

    The payout rule for these markets is the `chainlink-<sym>-usd-twap-60s`
    stream compared at the bell against the SAME stream at the open, so the
    level is the trailing `look_s` mean of the underlying AT THE OPEN -- not the
    spot at the open instant, which is what `r.strike` holds. Measured on 1,509
    live Polymarket rounds this reproduces the venue's own winner 94.1% against
    89.1% for the point (McNemar z +7.31); see config.cross_ref_twap for the
    tables and for why this does not contradict `_pm_strike_backfill`.

    `ticks` is chronological (ts_ms, price), the same tape `crossed_side` reads.
    FAILS CLOSED TO NONE -- on a window the tape does not bracket, on too few
    samples, on a non-positive price, and on a `start_ms` that has not happened
    yet (a pre-open round has no reference; the level is not a forecast). The
    caller falls back to `r.strike`, which is the behaviour that shipped before
    this existed, so an unavailable reference is never a NEW risk.

    A mean of the window's SAMPLE prices, not a time-weighted integral: the
    venue streams a 60 s TWAP that updates ~1/s, so the two agree to the
    sampling, and the tape is irregular enough that a plain mean is the honest
    reading of "what did it say on average" rather than a re-weighting the
    venue does not do.
    """
    if not ticks or start_ms is None:
        return None
    now_ms = int(time.time() * 1000)
    if int(start_ms) > now_ms:
        return None
    lo = int(start_ms) - int(look_s * 1000.0)
    hi = int(start_ms)
    px: list[float] = []
    first = last = None
    # Newest-first is the cheap direction: the window is at the END of the tape.
    for ts, p in reversed(ticks):
        ts = int(ts)
        if ts >= hi:
            continue
        if ts < lo:
            break
        if not p or p <= 0:
            continue
        px.append(float(p))
        # `last` is the NEWEST sample inside the window and this loop walks
        # newest-first, so it is taken ONCE. Assigning it every pass leaves the
        # OLDEST tick in it, which makes `hi - last` the whole window and
        # refuses every round -- the first version of this did exactly that.
        if last is None:
            last = ts
        if first is None or ts < first:
            first = ts
    if len(px) < TWAP_REF_MIN_N or first is None or last is None:
        return None
    if (first - lo) > TWAP_REF_EDGE_TOL_MS:
        return None                     # the window's open is not covered
    if (hi - last) > TWAP_REF_LAST_TOL_MS:
        return None                     # the tape stopped before the open
    return sum(px) / len(px)


def twap_forecast(ticks, tau_now_s: float,
                  look_s: float = TWAP_REF_LOOKBACK_S,
                  now_ms: int | None = None
                  ) -> list[tuple[int, float]] | None:
    """The tape as the VENUE'S OWN STATISTIC: the projected 60 s mean, per tick.

    WHY THE ARM NEEDS THIS. The payout is `TWAP60(at the bell) >= TWAP60(at the
    open)`, so the variable that decides money is a 60 s MEAN. `crossed_side`
    fires when the SPOT crosses the level, and inside the arm's own tau window
    (5-60 s) that spot is largely about to be averaged away:

        forecast(ts) = ((look_s - tau) * mean(spot over [ts-look_s, ts])
                        + tau * spot) / look_s

    At tau = 20 s the settlement window is 67% printed, at tau = 5 s it is 92%.
    Feeding these values to `crossed_side` in place of the spot makes the
    trigger fire on the number the venue pays on -- the fix for the loss family
    "the spot crossed, the mean never did". See config.cross_fc_trigger for the
    measured head-to-head and for what it costs.

    `tau_now_s` is the round's tau NOW; each tick's own tau is that plus the
    tick's age, because the tape is chronological. Before the settlement window
    has started (tau > look_s) there is no printed part to project FROM, so the
    value is the spot itself: zero-variance and unbiased for a martingale's
    future 60 s mean, and the SAME object `honest_fc` substitutes in
    `tools/pm_cross_replay.py`.

    That branch is not decoration as of 2026-09-18: `cross_max_tau_s` went
    60 -> 180, so the arm now ACTS in tau 60-180, where this returns the spot.

    ⛔ THE WINDOW. `TWAP60(bell)` averages `[bell - 60 s, bell]`, so at `ts` the
    PRINTED part of it is `[bell - 60 s, ts]` -- a FIXED left edge and a moving
    right one. This function used to average `[ts - 60 s, ts]` instead: the
    whole trailing minute at every tau, so at tau=30 it averaged 30 seconds the
    paid window will never contain, and at tau=5 it averaged 55 of them.
    MEASURED over 400,079 (round, second) forecasts
    (`tools/pm_fc_formula_check.py`), MAE in bps against the REALIZED
    `TWAP60(bell)` computed from the same tape:

        tau          code    fixed        tau          code    fixed
        [ 0,10)     0.155    0.042        [30,40)     0.990    0.812
        [10,20)     0.431    0.182        [40,50)     1.307    1.235
        [20,30)     0.689    0.437        [50,60)     1.844    1.843

    and the two are IDENTICAL at tau >= 60 (verified: max |code - fixed| = 0.0),
    where the branch below returns the spot. The strategy agrees at the taus the
    arm trades: at tau 5-60 the fixed window wins 58.0% against 54.1% and
    +0.0787 against +0.0517 per share on the same rounds, day-clustered t +4.32
    against +1.77. A 30 s mean and a 60 s mean are the same number only at
    tau=0, and the arm spends its whole life at tau > 0.

    `now_ms` defaults to the wall clock. It exists for the REPLAY tools, which
    project a historical tape: passing the round's newest sample's `ts_ms` and
    its `tau_s` reproduces each tick's own tau exactly -- `tau(now) + age` is
    `(end - ts)/1000` again -- so a backtest cannot compute a different number
    from the one the live arm computes.

    Returns None when the tape cannot describe a 60 s mean -- too few samples,
    or a newest sample staler than `TWAP_REF_LAST_TOL_MS`. The CALLER falls back
    to the spot, which is the behaviour that shipped before this existed, so an
    unavailable forecast is never a new risk.
    """
    if not ticks or tau_now_s is None:
        return None
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    px_all = [(int(ts), float(p)) for ts, p in ticks if p and float(p) > 0]
    if len(px_all) < TWAP_REF_MIN_N:
        return None
    # THE PAID WINDOW'S OPEN, a fixed instant: `bell - look_s`. See the ⛔ above
    # -- this was `ts - look_s`, which is the wrong window at every tau > 0.
    window_open_ms = (int(now_ms + float(tau_now_s) * 1000.0)
                      - int(look_s * 1000.0))
    lo_idx = 0
    roll = 0.0
    out: list[tuple[int, float]] = []
    for i, (ts, p) in enumerate(px_all):
        roll += p
        # TWO POINTERS, so the whole tape costs O(n). The left edge only ever
        # moves right, so each sample enters and leaves the sum once.
        while lo_idx <= i and px_all[lo_idx][0] < window_open_ms:
            roll -= px_all[lo_idx][1]
            lo_idx += 1
        n = i - lo_idx + 1
        tau = max(0.0, float(tau_now_s) + (now_ms - ts) / 1000.0)
        if tau >= look_s:
            # NOTHING IS PRINTED YET: a martingale's future 60 s mean is the
            # spot, and the window above holds samples from BEFORE this tick
            # that the paid window will not contain.
            out.append((ts, p))
            continue
        # COVERAGE, the same guard `twap_reference` applies: the printed part of
        # the window has to BE on the tape. A tape that starts 38 s into it (a
        # fixture, or a stalled feed) would otherwise average 2 samples and call
        # the result a 40 s mean. When it is not covered the spot is the honest
        # answer -- at tau just under the boundary the weight on the mean is
        # (look_s - tau)/look_s < 5%, so this is also the continuous choice.
        if (n < TWAP_REF_MIN_N
                or px_all[lo_idx][0] - window_open_ms > TWAP_REF_EDGE_TOL_MS):
            out.append((ts, p))
            continue
        mean = roll / n
        out.append((ts, ((look_s - tau) * mean + tau * p) / look_s))
    if not out:
        return None
    if (now_ms - out[-1][0]) > TWAP_REF_LAST_TOL_MS:
        return None                     # the tape stopped: no fresh reading
    return out


def depth_in_ticks(spot: float, strike: float, market: str | None) -> float:
    """How far through the strike the spot is, in TICKS of THIS market's spot.

    THE single definition of that conversion, shared by the live arm
    (`Bot._cross_tick`) and by every tool that replays the gate. It has to be
    shared, because `cross_tick_size` is the EXCHANGE tick of the UNDERLYING and
    not the 0.01 probability tick on the CLOB, and it is overridable PER MARKET
    (XRP 0.0001, DOGE 0.00001). A caller that reads the global setting instead
    of `cross_knob` prices those families off a floor that swamps their ceiling
    -- an EMPTY band, in which the arm can never fire and nothing says so. That
    is exactly how xrp and doge sat inert for their whole lives; see
    config.cross_min_depth_ticks and tests/test_depth_band_per_family.py.
    """
    tick = cross_knob(market, "tick_size") or 0.01
    return abs(spot - strike) / tick


def passes_depth_floor(spot: float, strike: float, market: str | None,
                       floor_ticks: float | None = None) -> bool:
    """Is the crossing at least `min_depth_ticks` ticks through the strike?

    STRICTLY shallower is refused, so a depth exactly ON the floor passes --
    the tolerance exists to make the boundary land on the intended side, and it
    is why the tolerance must be in TICKS.

    A fixed absolute epsilon cannot do this job. `abs(spot - strike)` is a
    difference of two doubles, so its error is the ULP of a number of the SPOT's
    size: 1.46e-11 at BTC's ~7.6e4. The `- 1e-12` this replaces was 14x SMALLER
    than that, so it refused crossings sitting exactly on the floor. MEASURED
    LIVE: 3 of 154 floor refusals, each logging

        only 1 tick(s) through the strike, floor is 1

    i.e. refusing a crossing while stating its depth equals the floor. Dividing
    by the tick first makes the error scale-free -- 1e-6 ticks clears BTC's
    1.5e-9, XRP's 2.2e-12 and DOGE's 5e-12 by three orders of magnitude.

    `floor_ticks` overrides the market's configured floor. It exists for sweeps
    that RANK values rather than apply them (`tools/cross_15m_calibrate.py`);
    nothing in the live path passes it.
    """
    if floor_ticks is None:
        floor_ticks = cross_knob(market, "min_depth_ticks") or 0.0
    if floor_ticks <= 0:
        return True
    return depth_in_ticks(spot, strike, market) >= floor_ticks - DEPTH_TICKS_EPS


def maker_limit_px(fair_side: float, quote: float | None,
                   tick: float = 0.01) -> float | None:
    """The price to POST when we want to be the maker, or None if there is none.

    A maker order must REST on the book, so it has to sit strictly below the ask
    -- pricing at or above the ask crosses, which makes it a taker that pays both
    the spread and the fee. It is also capped by our own fair: posting above fair
    is buying the contract for more than we believe it is worth.

    Measured on 1,187 recorded rounds (`tools.backtest_impulse_orders`, the same
    119 impulse entries priced four ways):
      take the ask   +0.0590/$1  pooled-t +0.44  halves -0.04/+0.15   noise
      post at fair   +0.5890/$1  pooled-t +2.23  halves +0.40/+0.77   43% fill
    The gap is selection, not luck: a taker pays the spread to chase a signal the
    whole book can see, while a resting bid only fills when someone crosses to us
    -- i.e. when the venue's price is stale. Makers also pay no fee here (the
    schedule is takerOnly).
    """
    if quote is None or not (0.0 < quote < 1.0):
        return None
    if not (0.0 < fair_side < 1.0):
        return None
    limit = round(min(fair_side, quote - tick), 2)
    if limit < 0.01 or limit >= quote:
        return None
    return limit


def sec_into_candle(now_ms: int) -> int:
    """Seconds elapsed since the current 1-minute candle opened (0-59)."""
    return (now_ms // 1000) % 60


def symbol_for_round(r) -> str:
    """The underlying a round is ABOUT, resolved from its SLUG.

    Never `r.symbol`: the venue builds every family's round through one
    `PmMarketFeed` whose `symbol` defaults to BTCUSDT, so eth/bnb/sol rounds all
    arrive stamped "BTCUSDT" (measured 2026-09-16 -- see SYMBOL_BY_PREFIX in
    config). A per-symbol lookup keyed on `r.symbol` silently reads BTC's data
    for every family, which is what turned a 9.4bps BNB round into "already
    moved 1109.7bps" and refused it.

    Falls back to `r.symbol` only when the slug names a family that is not in
    MARKETS, so fixtures and single-market tests keep working.
    """
    sym = SYMBOL_BY_PREFIX.get(market_prefix(getattr(r, "slug", "") or ""))
    return sym or getattr(r, "symbol", "") or ""


def _stamp_symbol(r, prefix: str):
    """Fix up `r.symbol` as the round enters the cache.

    `symbol_for_round` is the guard the arms read, but a wrong `r.symbol` also
    reaches the DB (`rounds.symbol`) and any future reader, so it is corrected
    at the source too. Both cache-fill sites go through here rather than
    relying on the arms remembering to use the helper.
    """
    want = SYMBOL_BY_PREFIX.get(prefix)
    if want and getattr(r, "symbol", None) != want:
        with contextlib.suppress(Exception):       # frozen/slots fixtures
            r.symbol = want
    return r


def round_row(r: Round) -> dict:
    up, down = r.up, r.down
    return {
        "slug": r.slug, "market_topic_id": r.market_topic_id, "market_id": r.market_id,
        "condition_id": r.condition_id, "symbol": symbol_for_round(r),
        "start_ms": r.start_ms, "end_ms": r.end_ms,
        "strike": r.strike, "end_price": r.end_price,
        "fee_rate_bps": r.fee_rate_bps,
        "up_token_id": up.token_id if up else None,
        "down_token_id": down.token_id if down else None,
        "price_feed_id": r.price_feed_id, "feed_provider": r.feed_provider,
        "status": r.status, "winner": r.winner_name,
    }


class Bot:
    def __init__(self, mode: str, store: Store, session: aiohttp.ClientSession):
        self.mode = mode
        self.store = store
        self.session = session
        self.market = MarketFeed(session)
        # One spot feed and volatility estimator per market in MARKETS. There is
        # only one now (BTC 5m); the dicts stay keyed by symbol because the
        # feeds, the histories and the models are all per-underlying.
        self.symbols = sorted({sym for _, sym, _ in active_markets()})
        self.spots: dict[str, SpotFeed] = {
            sym: SpotFeed(session, symbol=sym) for sym in self.symbols}
        self.vols: dict[str, VolEstimator] = {}
        # Rolling 1m-bar history per symbol, feeding the strategy models.
        self.histories: dict[str, PriceHistory] = {}
        # Recent (ts_ms, spot) per symbol for the impulse trigger (entry_mode
        # "impulse"). Capped well above the longest impulse window.
        self._ticks: dict[str, list[tuple[int, float]]] = {}
        self._cross_refused: dict = {}
        self.model = build_model(settings.pricing_model)
        # Opt-in per-side gate: Up is priced by `up_model` when it differs
        # from the default model (prototype: drift-adjusted digital for Up).
        self.up_model = (build_model(settings.up_model)
                         if settings.up_model.strip().lower()
                         != settings.pricing_model.strip().lower()
                         else None)
        self.spot = self.spots["BTCUSDT"]
        self.vol = VolEstimator(
            lam=settings.vol_ewma_lambda,
            floor=settings.vol_floor_1m,
            lookback=settings.vol_lookback_minutes,
        )
        self.guard = RiskGuard(
            daily_loss_cap=settings.daily_loss_cap_usdt,
            max_consecutive_losses=settings.max_consecutive_losses,
            max_open_rounds=settings.max_open_rounds,
            daily_loss_cap_pct=settings.daily_loss_cap_pct,
            bankroll=lambda: self.bankroll,
        )
        # --resume deliberately clears a persisted halt (fresh daily budget).
        self.resume_guard = False
        self.broker = None
        # Polymarket-only holders; left None on the Binance path so the venue
        # can be checked without a hasattr.
        self._pm_book = None
        self._pm_book_task = None
        self._pm_broker = None
        #: The `PredictionBook`-shaped view over `_pm_book`. This is what the
        #: arms actually read through (`self.ws_book`).
        self._pm_view = None
        #: Slugs already handed to the broker's per-round cache.
        self._pm_seen: set[str] = set()
        # Read-only client for the authenticated order book. Available in every
        # mode, so paper trading prices against the same book as live.
        self.book: LiveBroker | None = None
        self.ws_book: PredictionBook | None = None
        self._ws_task = None
        self._ws_gap_n = 0
        self._ws_gap_sum = 0.0
        # Keyed by FILL ID, not slug. "One position per round" was also a data
        # structure, and the double-down puts two fills in one round: keyed by
        # slug, the second booking overwrote the first, and the restore path
        # kept only one of them. Measured 2026-09-12: an add lost $2.23 and no
        # settlement row was ever written for it, so the ledger silently
        # understated the loss.
        self.open_fills: dict[int, tuple[str, Fill]] = {}   # fill_id -> (slug, Fill)
        # Wall-clock stamp of the last winnings-claim sweep (see
        # sweep_claims). 0.0, so the first tick after a restart picks up any
        # claim a crash or a venue rejection left stranded.
        self._last_claim_sweep = 0.0
        self._last_bankroll_refresh = 0.0
        self.bankroll = settings.bankroll_usdt
        # -- fast impulse path (see _impulse_tick) --------------------------
        # The current round and the last executable asks seen for it, refreshed
        # by every poll. The fast path fires between polls, so it acts on these
        # rather than re-reading the venue: the round's strike and tokens do not
        # change, and the only price that matters is the one that comes back
        # WITH the order's own quote.
        self._round: Round | None = None
        self._asks: tuple[float | None, float | None] = (None, None)
        self._sigma = 0.0
        # Cross arm: one claim per round, and its own day-scoped counter.
        self._cross_fired: dict[str, int] = {}
        # Finished rounds still inside the post-close scan window, by slug.
        self._post_close: dict = {}
        # The side already taken in a round, so a later crossing cannot buy the
        # opposite one. See config.cross_allow_reversal.
        self._cross_side: dict[str, str] = {}
        # The LEVEL each round's crossing is measured against, memoised per
        # slug as (level, source). Computed once per round from the spot tape
        # (see `_cross_level`), because the window it averages is entirely in
        # the past by the time the arm looks -- and because re-deriving it on
        # the websocket clock would rescan a 40,000-tick tape per family per
        # tick. See config.cross_ref_twap.
        self._cross_ref: dict[str, tuple[float, str]] = {}
        # How many 1 s KLINE reads have been started for a slug whose level the
        # TAPE cannot supply (a restart), so a round that cannot be measured is
        # retried a bounded number of times and then priced off `r.strike` with
        # a warning. See `_cross_level` for why that order matters.
        self._cross_kline: dict[str, int] = {}
        # The in-flight reads themselves, slug -> task, so a second tick cannot
        # pile another REST call onto a family that is already waiting.
        self._cross_kline_tasks: dict[str, object] = {}
        # Warm quotes for the cross arm's NEXT crossing (see
        # _prefetch_cross_quote): token -> (monotonic_ts, stake, quote).
        self._quote_pre: dict[str, tuple[float, float, dict]] = {}
        self._quote_pre_busy: set[str] = set()
        self._quote_pre_tasks: set = set()
        # Rounds with a cross decision IN FLIGHT. `_cross_fired` and
        # `_cross_last_ms` are not written until after the venue read in
        # _cross_tick, so on the websocket clock (~114/s) every tick that
        # arrives during that round-trip re-reads them unset, passes the
        # same gates and claims again. The re-entry gap cannot cover it:
        # it is stamped WITH the claim, i.e. after the await, while the
        # measured order latency is 2.7-5.2s. A set, not a counter --
        # entries within a round are serialised, not blocked, so
        # cross_max_per_round still admits the ones it is meant to.
        self._cross_inflight: set[str] = set()
        self._cross_day = ""
        self._cross_count = 0
        # Rounds this process has already triggered in, so one impulse cannot
        # buy the same round twice between polls.
        self._impulse_fired: set[str] = set()
        # Double-down state. In memory only: the daily loss cap is the real
        # limit and is restored from the DB, while this cap is an experiment
        # rate-limit whose exact value across a restart does not matter.
        self._doubled: set[str] = set()
        self._double_count = 0
        # Consecutive venue reads that have disagreed with the ledger. A single
        # disagreement is the redemption lag; this counter is what tells the two
        # apart (see _reconcile_bankroll).
        self._bankroll_drift = 0
        self.pnl = PnLSummary()
        self._order_cooldown_until = 0.0
        # slug -> consecutive polls the same side has shown an edge
        self._streak: dict[str, tuple[str, int]] = {}
        # slug -> 1m candle index the streak was counted in
        self._candle: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._last_vol_refresh = 0.0

    # -- setup --------------------------------------------------------------
    async def setup(self) -> None:
        for sym, feed in self.spots.items():
            await feed.start()
            v = VolEstimator(lam=settings.vol_ewma_lambda, floor=settings.vol_floor_1m,
                             lookback=settings.vol_lookback_minutes)
            klines = await fetch_klines(
                self.session, "1m", settings.vol_lookback_minutes, symbol=sym)
            v.seed_from_klines(klines)
            self.vols[sym] = v
            hist = PriceHistory()
            hist.seed(klines)
            self.histories[sym] = hist
            self._store_bars(sym, klines[:-1])   # seed the bars table too
        self.vol = self.vols["BTCUSDT"]
        self._last_vol_refresh = time.time()
        log.info("pricing model: %s", self.model.name)
        if self.up_model is not None:
            log.info("per-side gate: Down=%s Up=%s",
                     self.model.name, self.up_model.name)

        venue = registry.normalise(getattr(settings, "venue", "binance"))
        if venue == "polymarket":
            # Polymarket is a SECOND VENUE, not a second strategy: the arms,
            # gates and sizing are unchanged. See _setup_polymarket().
            await self._setup_polymarket()
        elif settings.has_credentials:
            book = LiveBroker(self.session, settings.binance_api_key,
                              settings.binance_api_secret,
                              slippage_bps=settings.slippage_bps)
            if await book.discover_wallet():
                self.book = book
                if settings.ws_book_enabled:
                    # Reuses the broker purely as a signer: the WS handshake
                    # takes the same HMAC-signed, sorted query string as SAPI.
                    self.ws_book = PredictionBook(
                        api_key=settings.binance_api_key,
                        api_secret=settings.binance_api_secret,
                        session=self.session, signer=book)
                    self._ws_task = asyncio.create_task(self.ws_book.run())
                    log.info("prediction book websocket enabled "
                             "(max age %.1fs, REST fallback on staleness)",
                             settings.ws_book_max_age_s)
            else:
                log.warning("no prediction wallet; falling back to displayed prices")


        if self.mode == "paper":
            self.broker = PaperBroker(
                starting_balance=100.0,
                # None -> charge the measured per-price fee curve.
                fee_rate=None,
                # With the real book we buy at the ask, so the paper broker must
                # not add a second spread on top of it.
                half_spread=0.0 if self.book else settings.assumed_half_spread,
            )
        elif self.mode == "live":
            if not self.book:
                raise SystemExit(
                    "live mode needs a prediction wallet. Check that the API key has "
                    "prediction access and that a Prediction Account exists."
                )
            self.broker = self.book
            # equity(), not balance(): cash alone under-states the account by
            # any open position and by winnings not yet redeemed.
            bal = await self.broker.equity()
            log.info("LIVE mode. wallet %s balance $%.4f",
                     self.broker.wallet_address, bal)
            if bal > 0:
                # Sizing must track real equity rather than a stale setting.
                self.bankroll = bal
            if bal < settings.min_order_usdt:
                raise SystemExit(f"balance ${bal:.4f} below the venue minimum "
                                 f"${settings.min_order_usdt:.2f}")

        if self.mode != "record":
            self._restore_open_positions()
            self._seed_cross_count()
        if self.mode == "live":
            self._seed_guard_state()
            if self.resume_guard:
                self.guard.resume()
                # Persist the reset: without a marker the next restart
                # re-seeds today's old settlements and re-halts the bot, so
                # the reset would only live until the next crash/restart.
                self.store.set_meta("guard_reset_ms",
                                    str(int(time.time() * 1000)))

    def _broker_modes(self) -> tuple[str, ...]:
        """Every fill mode that settles through THIS broker.

        A mode is a LABEL, not a broker: 'double' is placed and settled by the
        same live broker as 'live', so a `mode = broker.mode` filter silently
        excluded it. Measured 2026-09-12, on the first doubled round:
          * the add orphaned on restart and no settlement row was ever written
            for a $2.23 loss, so the ledger understated it; and
          * the guard's daily P&L would have missed that loss too -- the worse
            half, because it lets a restart trade past the daily cap.
        """
        # 'maker' and 'stale_maker' are the same live broker under a different
        # LABEL, exactly like 'double'. Omitting them would reproduce the bug
        # this docstring describes: the fill orphans on restart, no settlement
        # row is written, and the guard's daily P&L misses the loss -- which
        # lets a restart trade past the daily cap. 'maker' was already reachable
        # via impulse_execution and already exposed to this; it is listed now
        # because stale_maker_enabled makes the maker path routine.
        #
        # 'train' was exactly that omission, and it cost a live deadlock --
        # no trigger is activated by listing it, the LABEL is what is missing.
        # MEASURED 2026-09-16 15:37 -> 2026-09-17 17:2x: the Train arm's fill on
        # btc-updown-5m-1789572900 ($0.98, Up) was orphaned by a restart inside
        # its settlement window. It is the only fill in the ledger with no
        # `settlements` row, and the three queries disagreed about it --
        # `pool_for_mode` books 'train' into the RESERVED slot and
        # `_open_cross_stake` sums `mode IN ('cross','train')`, while this tuple
        # omitted it, so `_restore_open_positions` never replayed it and
        # `settle_due` could never write its settlement. The $0.98 therefore
        # counted against `cross_max_open_stake_pct` FOREVER: on the $5.52
        # collateral 0.98 + 1.00 > 0.30 * 5.52 = $1.66, so every crossing in
        # every family was refused and the arm placed ZERO orders for 2.5 h.
        # The label is BUILT (`arm_mode = "train" if train else "cross"`), not
        # passed as a literal, which is why the source-scanning invariant test
        # that exists to catch this could not see it -- that test now asserts
        # the reserved-pool labels directly.
        return (("live", "double", "maker", "stale_maker", "cross", "train",
                 "impulse")
                if self.broker.mode == "live"
                else ("paper", "impulse", "maker"))

    def _seed_cross_count(self) -> None:
        """Carry today's cross ATTEMPTS into the fresh counter.

        `_cross_count` starts at 0 in __init__, so every restart handed the arm
        a fresh daily budget: on a day with 208 restarts, `cross_max_per_day`
        bounded nothing at all. The deleted ticket arm seeded the same counter
        from the database for exactly this reason.

        ATTEMPTS, not fills: _cross_tick increments the counter before ordering
        and writes its decision row on every attempt, so counting decisions is
        what reproduces the in-memory number. Counting fills instead would let
        a run of unfilled attempts reset the budget, which is the case the cap
        exists to bound.

        The per-ROUND cap needs no equivalent: it goes through check_reserved,
        which reads `reserved_open`, and _restore_open_positions rebuilds that
        from unsettled fills.
        """
        # UTC midnight, arithmetically: time.mktime() would read the string
        # back as LOCAL time while every other day boundary in this repo is
        # gmtime, so on a non-UTC host the window would be hours out.
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        start_ms = int((now - (now % 86400.0)) * 1000)
        try:
            rows = self.store.query(
                "SELECT COUNT(*) AS c FROM decisions "
                "WHERE reason LIKE 'cross into%' AND ts_ms >= ?", (start_ms,))
        except Exception as e:                              # noqa: BLE001
            log.warning("could not seed the cross daily count (%s); starting "
                        "from 0 for today", type(e).__name__)
            return
        n = int(rows[0]["c"]) if rows else 0
        self._cross_day = today
        self._cross_count = n
        if n:
            log.info("cross arm already attempted %d time(s) today; the daily "
                     "cap is counted from the database, not from memory", n)

    def _seed_guard_state(self) -> None:
        """Carry today's live PnL and loss streak into the fresh guard.

        Without this, a restart after the daily cap is breached lets the bot
        trade again on a day that already lost its budget.

        Both quantities are DAY-SCOPED. The streak walk therefore stops at the
        first settlement from an earlier date instead of reading the last N
        settlements regardless of age: otherwise a restart on a fresh day would
        inherit yesterday's losses and halt a day that should start clean.
        """
        today = time.strftime("%Y-%m-%d", time.gmtime())
        mode = getattr(self.broker, "mode", "live")
        reset_ms = 0
        if mode == "live":
            marker = self.store.get_meta("guard_reset_ms")
            if marker:
                try:
                    reset_ms = int(marker)
                except ValueError:
                    reset_ms = 0
        modes = self._broker_modes()
        ph = ", ".join("?" * len(modes))
        # TWO queries, deliberately. They used to share one `LIMIT 50`, which is
        # correct for the streak walk (it only ever needs the newest few) and
        # WRONG for the daily total: on 2026-09-13 the day held 145 settlements
        # worth +$2.49, the newest 50 of them summed to -$7.85, and every
        # restart seeded that phantom loss. Two more real losses then tripped a
        # $9.49 cap on a day that was up $9.98 -- the bot halted itself out of
        # a winning session, and re-halted on each restart because the seed is
        # re-read every time.
        #
        # The daily sum is bounded by the day, not by a row count.
        day_start_ms = int((time.time() - (time.time() % 86400.0)) * 1000)
        since = max(reset_ms, day_start_ms)
        pnl_rows = self.store.query(
            "SELECT COALESCE(SUM(s.pnl), 0.0) AS p FROM settlements s "
            f"JOIN fills f ON f.id = s.fill_id WHERE f.mode IN ({ph}) "
            "AND COALESCE(s.void, 0) = 0 AND s.ts_ms >= ?", (*modes, since))
        pnl = float(pnl_rows[0]["p"]) if pnl_rows else 0.0
        rows = self.store.query(
            "SELECT s.pnl, s.ts_ms FROM settlements s "
            f"JOIN fills f ON f.id = s.fill_id WHERE f.mode IN ({ph}) "
            "AND COALESCE(s.void, 0) = 0 AND s.ts_ms >= ? "
            "ORDER BY s.ts_ms DESC LIMIT 50", (*modes, reset_ms))
        streak = 0
        for r in rows:                      # newest first
            if time.strftime("%Y-%m-%d", time.gmtime(r["ts_ms"] / 1000)) != today:
                break                       # the streak is day-scoped
            if r["pnl"] < 0:
                streak += 1
            else:
                break
        self.guard.seed_daily(pnl, streak)

    def _restore_open_positions(self) -> None:
        """Reload fills that never settled.

        Without this a restart orphans any open position: the round resolves,
        nothing records the settlement, and the risk guard's view of open rounds
        and PnL silently diverges from reality.
        """
        modes = self._broker_modes()
        ph = ", ".join("?" * len(modes))
        rows = self.store.query(
            "SELECT f.* FROM fills f "
            "LEFT JOIN settlements s ON s.fill_id = f.id "
            f"WHERE s.id IS NULL AND f.mode IN ({ph})", modes)
        for row in rows:
            fill = Fill(
                slug=row["slug"], side=row["side"],
                requested_px=row["requested_px"], filled_px=row["filled_px"],
                shares=row["shares"], stake=row["stake"], fee_paid=row["fee_paid"],
                order_id=row["order_id"] or "", ts_ms=row["ts_ms"],
                token_id=row["token_id"],
            )
            self.open_fills[row["id"]] = (row["slug"], fill)
            reserved, pool = pool_for_mode(row["mode"])
            self.guard.on_open(row["slug"], reserved=reserved, pool=pool)
            # EXIT-ON-FLIP COVERAGE ACROSS A RESTART. `_exit_watch` is memory,
            # so without this every position that is live when the process
            # restarts goes to the bell unwatched -- observed 2026-09-18, when
            # two fills booked 33 s before the flag was enabled rode to the
            # bell unprotected. `entry_ms` is the ORIGINAL fill time, so the
            # watcher's own clock is not reset by a restart.
            if row["mode"] == "cross":
                self._arm_exit_watch(
                    fid=row["id"], slug=row["slug"], side=row["side"],
                    token=row["token_id"], shares=row["shares"],
                    stake=row["stake"], fee=row["fee_paid"],
                    entry_ms=row["ts_ms"], did=row["decision_id"],
                    px=row["filled_px"], req=row["requested_px"],
                    oid=row["order_id"])
        if rows:
            log.info("restored %d unsettled position(s): %s",
                     len(rows), ", ".join(r["slug"] for r in rows))
    async def _venue_position_tokens(self) -> set[str] | None:
        """Tokens the venue currently holds a position in (ground truth)."""
        try:
            pos = await self.broker.positions()
        except Exception:
            return None
        return {p.get("tokenId") for p in pos or []}

    async def _unbooked_round_tokens(self, r, up, down) -> set[str] | None:
        """Tokens in THIS round the venue holds that our ledger cannot explain.

        Returns None when the venue cannot be read at all, which is its own
        reason to sit out: we cannot reason about exposure we cannot see.

        ARMS ARE INDEPENDENT -- one arm's position must never refuse another's.
        The venue cannot say WHICH arm bought a token, so asking it "is anything
        held here?" conflates two different situations:

          * a position our ledger already knows about. That is either another
            arm (which is allowed, and is what the per-arm pools bound) or our
            own, which the pool guard has already counted. It must not block.
          * a position our ledger has NO record of -- an order that was never
            confirmed but actually filled. _restore_open_positions cannot replay it
            because it never reached the database, so the venue is the only
            place it exists. THAT is what this check is for, and it still
            blocks.

        Measured cost of conflating them: 52% of the cross arm's fill-aware
        triggers over 6 days were refused because the MAIN arm held the round,
        at an EV indistinguishable from the ones that got through.
        """
        held = await self._venue_position_tokens()
        if held is None:
            return None
        booked = {getattr(f, "token_id", None)
                  for slug, f in self.open_fills.values() if slug == r.slug}
        return {t for t in (up.token_id, down.token_id)
                if t in held} - {b for b in booked if b}

    async def refresh_vol_if_stale(self) -> None:
        if time.time() - self._last_vol_refresh < 60.0:
            return
        try:
            for sym, v in self.vols.items():
                klines = await fetch_klines(self.session, "1m", 5, symbol=sym)
                for k in klines[:-1]:      # skip the still-forming bar
                    o, c = float(k[1]), float(k[4])
                    if o > 0 and c > 0:
                        # add_bar, NOT add: this poll runs every 60 s and
                        # re-fetches the same completed bars, so a plain add()
                        # re-feeds them and shortens the EWMA's time constant by
                        # the number of duplicates (see VolEstimator.add_bar).
                        v.add_bar(int(k[0]), math.log(c / o))
                self.histories[sym].merge(klines)
                # Persist the bars incl. taker-buy volume (kline field 9): the
                # quotes table cannot reconstruct order flow, so without this
                # the order-flow models can never be replayed or tuned.
                self._store_bars(sym, klines[:-1])
            self._last_vol_refresh = time.time()
        except Exception as e:
            log.warning("vol refresh failed: %s: %s", type(e).__name__, e)

    def _store_bars(self, symbol: str, klines: list[list]) -> None:
        """Store (open_ms, close, volume, taker_buy) for completed bars."""
        rows = []
        for k in klines:
            try:
                rows.append((int(k[0]), float(k[4]), float(k[5]), float(k[9])))
            except (TypeError, ValueError, IndexError):
                continue
        if rows:
            self.store.upsert_bars(symbol, rows)

    # -- main tick ----------------------------------------------------------
    async def tick(self) -> None:
        await self.refresh_vol_if_stale()
        primary = active_markets()[0][0]
        now_s = time.time()
        now_ms = int(now_s * 1000)
        cache = getattr(self, "_round_cache", None)
        if cache is None:
            cache = self._round_cache = {}
        # A round's METADATA -- strike, token ids, end time -- is fixed once the
        # round opens. Only spot and the ask move, and both arrive on their own
        # feeds. So a family needs the paginated sweep only when its round has
        # actually ROLLED, which is what makes six traded families affordable:
        # `rounds_by_prefixes` stops early only when EVERY requested prefix is
        # found, and a quiet family sinks through the day (bnb-updown-5m hit
        # offset 1450 -- 30 pages, 3.95s). Re-sweeping all six every poll is
        # precisely the coupling that made short crossings invisible.
        # ⭐ STASH THE ROUND THAT JUST ENDED, before the cache drops it. Its
        # outcome is now DETERMINISTIC and the book has not necessarily caught
        # up -- see `_post_close_scan`. Kept keyed by slug so a re-fetch of the
        # same prefix cannot overwrite the finished one with its successor.
        if float(settings.post_close_scan_s) > 0:
            pc = getattr(self, "_post_close", None)
            if not isinstance(pc, dict):
                pc = self._post_close = {}
            for _p, _sym, _ in active_markets():
                _r = cache.get(_p)
                if _r is None or getattr(_r, "strike", None) is None:
                    continue
                _e = int(getattr(_r, "end_ms", 0) or 0)
                if _e and _e <= now_ms and _r.slug not in pc:
                    pc[_r.slug] = (_r, _sym)
            if len(pc) > 32:                 # 9 families; anything more is dead
                for _k in sorted(pc, key=lambda k: int(pc[k][0].end_ms or 0))[:16]:
                    pc.pop(_k, None)
        stale = [p for p, _, _ in active_markets()
                 if cache.get(p) is None
                 or getattr(cache[p], "end_ms", 0) <= now_ms
                 or getattr(cache[p], "strike", None) is None]
        # The primary is refetched every poll INLINE: it is page 1 (highest
        # volume) so it costs one request, and the model arm reads its live
        # displayed price, not just its strike.
        found = await self.market.rounds_by_prefixes([primary])
        for pfx, rr in found.items():
            if rr:
                cache[pfx] = _stamp_symbol(rr, pfx)
        # Everything else is refreshed OFF the poll. `rounds_by_prefixes` stops
        # paging only once every requested prefix is found, and a quiet family
        # sinks through the day (bnb-updown-5m at offset 1450 -- 30 pages,
        # ~4s). At a 5m roll all three 5m families go stale at once, so doing
        # that inline stalled the poll for 4.4-4.7s every five minutes
        # (measured: median 133ms, max 4673ms).
        #
        # That stall is not harmless just because the NEW 5m round is at tau
        # ~300 and out of the trade window: the 15m families roll on a
        # different schedule, so a 5m roll can blind a 15m market sitting at
        # tau 5-60. The refresh therefore runs as a background task and the
        # poll keeps using the cache until it lands.
        others = [p for p in stale if p != primary]
        task = getattr(self, "_round_refresh", None)
        if others and (task is None or task.done()):
            if task is not None and task.done():
                with contextlib.suppress(Exception):
                    task.result()               # surface a crash, never swallow
            self._round_refresh = asyncio.create_task(
                self._refresh_rounds(others))
        # Spot history for EVERY symbol, appended HERE rather than in _record.
        # The range gate reads this buffer, and a family whose round is only
        # refetched on a roll would otherwise measure its range on a handful of
        # samples -- the gate would return None ("no opinion") and silently not
        # apply. Costs no network: the spot feeds are websockets.
        buf = getattr(self, "_ticks", None)
        if buf is None:
            buf = self._ticks = {}
        # Guarded: fixtures build a Bot via __new__ and have no `spots`.
        spots = getattr(self, "spots", None) or {}
        for sym in {s for _, s, _ in active_markets()}:
            f = spots.get(sym)
            px = getattr(f, "price", None) if f is not None else None
            if px:
                tbuf = buf.setdefault(sym, [])
                tbuf.append((now_ms, px))
                if len(tbuf) > 2400:
                    del tbuf[: len(tbuf) - 2400]
        # `trade` is membership of CROSS_TRADED, NOT "is primary". The two are
        # deliberately separate: MARKETS widens the tape, CROSS_TRADED widens
        # the risk, and collapsing them is how the daily loss cap was once
        # bypassed.
        self._mkt = {}
        for prefix, sym, _ in active_markets():
            r = cache.get(prefix)
            if r:
                await self._record(r, sym,
                                   trade=cross_traded(prefix),
                                   primary=(prefix == primary))
        # The websocket book follows EVERY market, not just the traded one. The
        # recorded-only families had no book at all (the REST fetch in _record
        # is gated on `primary` because it costs SAPI quota and we have been
        # rate limited), so their tape carried win rates but no price --
        # and win rate alone cannot clear a market, whose break-even is ~0.665
        # at live fills. The websocket costs no quota, and `follow` puts all of
        # them in ONE topic, so this is the cheap half of that fix.
        #
        # The topic is a connect-time parameter, so the set changing forces a
        # handshake. The set changes at a round roll, which for the 5m families
        # is the same wall-clock boundary for all of them; a 15m market can be
        # mid-round (inside the cross window) when that happens, and will fall
        # back to the REST/displayed price for the reconnect. That fallback is
        # the same one a stale book already takes.
        # The settled-but-still-trading window. Runs here because `_record`
        # above has just refreshed every book, so the freshness check in the
        # scan is reading what the tick actually saw.
        with contextlib.suppress(Exception):
            await self._post_close_scan()
        # The locked-in tail. Runs on the same refreshed books.
        with contextlib.suppress(Exception):
            await self._locked_tail_tick()
        wsb = getattr(self, "ws_book", None)
        if wsb is not None:
            # EVERY family the cross arm may order in. Off-primary there is no
            # REST book and no displayed-price fallback (see _cross_tick), so a
            # market without a websocket book simply cannot trade.
            #
            # THIS IS THE MEASURED RISK OF THE WIDENING. Following all six once
            # took the poll from a 1.00s median to 4.15s (3.04s after the
            # handshake fix) because the book feed ingests ~6x the message
            # volume in this process -- and that is not a background cost: the
            # cross arm compares the newest tick with one 1-3s old, so a slow
            # poll makes short crossings invisible. btc-updown-5m 1789336500 is
            # the worked case, a ~1.5s crossing the arm never sampled.
            #
            # Two things are different now. The paginated sweep no longer runs
            # every poll (rounds are cached until they roll), which was the
            # other half of that 4.15s; and `poll_ms` below reports the cadence
            # continuously, so this is monitored rather than assumed. If it
            # degrades, `cross_ws_follow_all` turns it off without a deploy.
            # ROUNDS, not market ids. The wrapper addresses a market by an
            # integer id; Polymarket addresses `btc-updown-5m-<ts>` by its own
            # slug, and its two outcomes have SEPARATE books. Both book classes
            # accept a Round, which is the one argument that means the same
            # thing on either venue -- and on Polymarket `market_id` is None,
            # so an id-based list was always EMPTY and `follow` was never
            # called: the socket connected, ingested ~680 messages/s and was
            # read by nothing.
            rs = ws_follow_rounds(cache, getattr(self, "_mkt", None), primary,
                                  settings.cross_ws_follow_all)
            if rs:
                wsb.follow(rs)
                # The subscription IS the difference between a family that can
                # be priced and one that cannot, and its failure mode is SILENCE:
                # `_tokens` returns [] for a round with no token ids and `follow`
                # then ignores it, while every arm on that family refuses with
                # `no ask on the book` and the log reads as "the market is
                # quiet". Print the counts so "18 assets" (9 families x 2) is
                # verifiable rather than assumed.
                tok_of = getattr(wsb, "_tokens", None)
                toks = sum(len(tok_of(r)) for r in rs) if tok_of else 2 * len(rs)
                if tok_of is not None and toks != 2 * len(rs):
                    log.warning("ws follow: %d round(s) -> %d token(s); a round "
                                "with no token ids cannot be priced",
                                len(rs), toks)
        # LIVENESS, once a minute. A family that is not in `_mkt` gets no
        # quote, no book and no decision, and every one of those is an ABSENCE
        # -- so a whole family can be dead for hours with nothing in the log.
        # Measured 2026-09-17: xrp/sol/doge each had 0 quotes in an hour while
        # the log said only "no trades". This line makes the per-family state a
        # number rather than an inference.
        self._liveness_next = getattr(self, "_liveness_next", 0.0)
        if now_s >= self._liveness_next:
            self._liveness_next = now_s + 60.0
            live = sorted((getattr(self, "_mkt", None) or {}).keys())
            cached = sorted(cache.keys())
            # `warm` counts order-metadata cache misses actually fetched (see
            # `PmBroker.warm`). SUCCESS IS SILENT there by design, so without
            # this number the warm's presence is an inference from a log line
            # that only appears on failure -- the same blindness that hid the
            # dead first version.
            warm = getattr(getattr(self, "_pm_broker", None), "warm_calls", None)
            miss = [p for p, _, _ in active_markets() if p not in cached]
            dead = [p for p, _, _ in active_markets() if p not in live]
            # WARNING when a family is missing, DEBUG when all nine are fine.
            # The line exists for the FAILURE case: a family that is not in
            # `_mkt` gets no quote, no book and no decision, and every one of
            # those is an ABSENCE, so a whole family can be dead for hours with
            # nothing in the log. Healthy it only repeats itself -- measured 135
            # of the ~1,600 lines in a 3-hour window, all of them "no change".
            pat = ("families: %d/%d cached, %d live | missing cache: %s | "
                   "not live: %s | warm %s")
            fargs = (len(cached), len(active_markets()), len(live), miss or "-",
                     dead or "-", "n/a" if warm is None else warm)
            if miss or dead:
                log.warning(pat, *fargs)
            else:
                log.debug(pat, *fargs)
        # AFTER the subscription, not before. The arm refuses off-primary
        # without a fresh websocket book, so evaluating first would spend the
        # poll in which a newly-appeared family could have traded -- and on a
        # round roll that is every family at once.
        await self._cross_all()

    async def _refresh_rounds(self, prefixes: list[str]) -> None:
        """Re-sweep rolled families off the poll's critical path.

        ONE PREFIX PER CALL, DELIBERATELY. `rounds_by_prefixes` pages until
        every requested prefix has been found, so a single call for all eight
        non-primary families makes their outcomes COUPLED: one family whose
        round is not in the listing (or is buried past the page cap) consumes
        the whole page budget and the rest never land in the cache.

        MEASURED, 2026-09-17: sol-updown-5m, xrp-updown-5m and doge-updown-5m
        had rounds rows in the database for every round and **ZERO quotes over
        the preceding hour**, while btc/eth/bnb had ~6,250 each. A family with
        no cache entry is never passed to `_record`, so it gets no quote, no
        `_mkt` entry, no websocket book, and no decision -- and because a
        refusal is not an error, nothing said so. `pm book ws connected (12
        assets)` = 6 markets = btc/eth/bnb only, which is the same fact seen
        from the other end. Per-prefix calls make each family's success
        independent of the others'.

        The misses are then REPORTED. This class of failure was invisible for
        hours because the only symptom was an absence.
        """
        cache = getattr(self, "_round_cache", None)
        if cache is None:
            cache = self._round_cache = {}
        missing: list[str] = []
        for pfx in prefixes:
            try:
                found = await self.market.rounds_by_prefixes([pfx])
            except Exception as e:                          # noqa: BLE001
                log.warning("round refresh failed for %s (%s: %s)",
                            pfx, type(e).__name__, e)
                missing.append(pfx)
                continue
            r = found.get(pfx)
            if r:
                cache[pfx] = _stamp_symbol(r, pfx)
            else:
                missing.append(pfx)
        if missing:
            # Not necessarily fatal on its own -- a family can legitimately have
            # no round listed between rounds -- but if a prefix is here on every
            # sweep it is DEAD, and that is what this line is for.
            log.warning("round refresh: no round for %s (%d/%d cached)",
                        ", ".join(missing), len(cache), len(active_markets()))

    async def _record(self, r, symbol: str, *, trade: bool,
                      primary: bool = False) -> None:
        self.store.upsert_round(round_row(r))
        # Keep the venue broker's per-round cache on the LIVE round. Cheap and
        # idempotent -- a set membership test once a slug is known -- and it is
        # what makes a roll survivable, because booting only remembers the
        # rounds that happened to be live at that moment. Guarded by the broker
        # rather than inside, so the Binance path pays nothing.
        if getattr(self, "_pm_broker", None) is not None:
            self._pm_remember(r)
            # WARM THE ORDER METADATA, OFF THE CRITICAL PATH.
            #
            # MEASURED 2026-09-17: a live FAK took 918-947 ms, and the httpx log
            # shows ~490 ms of that is two GETs the SDK does before the POST --
            # `GET /markets-by-token/<tok>` (253 ms) then `GET /clob-markets/
            # <cond>` (238 ms). Those are its order-metadata cache resolving the
            # market for a token. It caches (`_conditions` forever, `_markets`
            # 600 s) but keyed by TOKEN/CONDITION, and a token IS a round side,
            # so every order is a cold lookup. Meanwhile the book moves 1-4c in
            # that window: five of six live FAKs came back `no orders found to
            # match`, one of them missing by a single cent.
            #
            # In a THREAD (the client is blocking) so the decision loop never
            # stalls on it. A round is registered within seconds of opening and
            # the arms fire at tau <= 300 s, so the cache is warm long before
            # the first order of that round.
            try:
                # `up.token_id` / `down.token_id` is EXACTLY how the order gets
                # its token (see `_order`: `token = up.token_id if sig.side ==
                # "Up" else down.token_id`). The first version of this called
                # `r.token("Up")`, a method a PM round does not have; the
                # AttributeError was swallowed below, so the warm never ran ONCE
                # in 13 minutes of live trading. Warm those ids, not a guess.
                up_r, down_r = r.up, r.down
                toks = [t for t in (up_r.token_id if up_r else None,
                                    down_r.token_id if down_r else None) if t]
            except Exception as exc:                          # noqa: BLE001
                # Loud, but ONCE per process: this is on the poll path and must
                # never break the loop, yet a silent swallow is what hid the
                # defect above.
                if not getattr(self, "_warm_gripe", False):
                    self._warm_gripe = True
                    log.warning("pm warm: cannot read round tokens (%s %s); "
                                "orders will pay their own lookups",
                                type(exc).__name__, exc)
                toks = []
            # ⛔ A SINGLE SHARED TASK STARVED EIGHT OF THE NINE FAMILIES, and
            # that was the whole of the order path's latency.
            #
            # This used to be `if toks and (task is None or task.done())`
            # against ONE `self._warm_task`. `_record` runs for every family in
            # `MARKETS` order, and `MARKETS[0]` is the PRIMARY -- so the primary
            # re-dispatched its own task on every sweep, the task was therefore
            # never `done()` by the time the next family was checked, and every
            # other family was skipped for the life of the process.
            #
            # MEASURED live, 2026-09-17: the `warm` counter moves +2 per
            # 5-minute rollover (18, 18, 18, 18, 20, 22, 22, 22, 22, 24, 26)
            # against 18 tokens a round. Two tokens a round is not a rate, it is
            # ONE family's round -- the primary's.
            #
            # The cost was the entire gap this arm has been chasing. Counting
            # the httpx lines between `take via FAK` and the FAK ack over 16
            # live attempts:
            #     13 paid BOTH metadata GETs -> decision->ack median  963 ms
            #      3 paid NEITHER            -> decision->ack median  442 ms
            # At 963 ms the venue's price is gone -- the shadow probe read the
            # real venue book 0.06-0.28 ABOVE our own at every fire -- so the
            # FAK comes back `no orders found to match`, and the fills it does
            # get are the ones where the book did not move, which is the
            # adverse selection in the fill record.
            #
            # ACCUMULATE, THEN DRAIN -- see `_queue_warm_tokens`, which holds
            # the measurement and the starvation it removes. Every family adds
            # its tokens on every sweep and one task drains the set; it is
            # still ONE task and ONE thread at a time, so the SDK client is
            # driven exactly as before.
            batch = _queue_warm_tokens(self, toks)
            if batch:
                br = self._pm_broker
                t = asyncio.create_task(asyncio.to_thread(br.warm, batch))
                # NOTHING AWAITS THIS TASK, so an exception inside the warm
                # would be swallowed for the life of the process -- which is
                # exactly how the first version's AttributeError stayed
                # invisible. Report it once, from the task itself.
                t.add_done_callback(_report_task_failure)
                # ...and say how many it resolved, so coverage is a NUMBER in
                # the log rather than something inferred from the counter.
                t.add_done_callback(_log_warm_done)
                self._warm_task = t
        feed = self.spots[symbol]
        vol = self.vols[symbol]
        # self.spot / self.vol are the PRIMARY's and are set once. They used to
        # be reassigned on every market in the loop, so after the loop they held
        # the LAST family's feed while _round still held BTC -- which on
        # 2026-09-12 compared ETH's price to BTC's strike (-9672 bps) and bought
        # BTC Down because ETH had ticked. Everything below reads the locals.
        if primary:
            self.spot = feed
            self.vol = vol

        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        up, down = r.up, r.down
        if not up or not down:
            return

        spot = feed.price
        sigma = vol.sigma_1m()

        # Buy prices come from the real book (the ask), not the displayed
        # last-trade price, which was observed 2-3 cents away from the mid.
        up_bid = up_ask = down_bid = down_ask = None
        book_age_ms = None
        # Sizes ride with `book_age_ms`: both are properties of the WEBSOCKET
        # frame, and both stay None when the REST poll is what answered.
        up_ask_size = up_bid_size = venue_lag_ms = None
        if self.book and r.market_id and primary:
            # Only the PRIMARY, not every traded family: the REST book costs
            # SAPI quota and we have been rate limited, so six families would
            # be twelve calls a second. The other families price off the
            # websocket book instead, and _cross_one REFUSES them outright when
            # that book is stale rather than falling back to a displayed price
            # that may be seconds old. The untraded market's
            # books were fetched every second purely for the recorder, tripling
            # SAPI calls for data no replay uses (the tool is BTC-only). If the
            # venue rate-limits, best_prices() returns None and the bot silently
            # falls back to the displayed price plus an assumed spread.
            # Concurrently: sequential fetches let the price move between them,
            # which manufactured phantom complete-set arbitrages in recorded data
            # (one book drifted 0.21 between two calls).
            bp_up, bp_dn = await asyncio.gather(
                self.book.best_prices(up.token_id, r.market_id),
                self.book.best_prices(down.token_id, r.market_id))
            if bp_up:
                up_bid, up_ask = bp_up
            if bp_dn:
                down_bid, down_ask = bp_dn
        # The WEBSOCKET book, if it is fresh, overrides the REST poll we just
        # did. It is ~4x the resolution (median 250ms against 1000ms) and the
        # venue reprices 66% of a cross inside one second, so the poll was
        # systematically quoting the fast arms a price that had already gone.
        # Staleness falls back rather than waiting: a frozen feed pricing every
        # order off a dead book is worse than the poll it replaces.
        wsb = getattr(self, "ws_book", None)
        if wsb is not None and r.market_id:
            # No `trade` gate: tick() subscribes every market, so the recorded
            # families get a real ask here instead of None. Nothing downstream
            # of this is reached for them -- _record returns on `not trade`
            # before any order path -- so this widens the TAPE, not the risk.
            top = wsb.best(r, settings.ws_book_max_age_s)
            if top is not None:
                # Measured while it trades, so the value of this feed is a
                # number in the log rather than an argument.
                if up_ask is not None:
                    self._ws_gap_n = getattr(self, "_ws_gap_n", 0) + 1
                    self._ws_gap_sum = getattr(self, "_ws_gap_sum", 0.0) + abs(top.up_ask - up_ask)
                    if self._ws_gap_n % 500 == 0:
                        log.info("ws book vs REST poll: mean |ask gap| %.4f "
                                 "over %d samples (%d msgs)",
                                 self._ws_gap_sum / self._ws_gap_n,
                                 self._ws_gap_n, wsb.messages)
                up_bid, up_ask = top.up_bid, top.up_ask
                down_bid, down_ask = top.down_bid, top.down_ask
                book_age_ms = int(top.age_s() * 1000)
                # Taken from the SAME BookTop as the prices, never re-read: a
                # second lookup could land on a different frame and pair a
                # price with a size that was never quoted beside it.
                up_ask_size, up_bid_size = top.up_ask_size, top.up_bid_size
                venue_lag_ms = top.lag_ms
        # RECORD-ONLY fallback. The gate above is right for trading, and it also
        # decides what the tape contains -- off-primary that is most of it. Ask
        # coverage was btc-5m ~100% (the primary, which gets the REST book every
        # poll) against eth-5m 31% and bnb-5m 17%, which is why bnb had FOUR
        # priceable crossings to tune on and btc had 302. Every per-symbol
        # question was blocked on that.
        #
        # This fills the recorded prices in ONLY where the fresh lookup returned
        # nothing, and stamps `book_age_ms` so a replay can filter to whatever
        # freshness it wants. It cannot reach an order: the trading paths do
        # their own `wsb.best(..., ws_book_max_age_s)` lookups.
        # `r.market_id` used to gate this, which silently disabled the stale
        # fill on Polymarket -- where a round carries no market id at all.
        # Whether a book exists is `wsb.best`'s answer, not the caller's.
        if (wsb is not None and up_ask is None
                and settings.ws_book_record_max_age_s > 0):
            stale = wsb.best(r, settings.ws_book_record_max_age_s)
            if stale is not None:
                up_bid, up_ask = stale.up_bid, stale.up_ask
                down_bid, down_ask = stale.down_bid, stale.down_ask
                book_age_ms = int(stale.age_s() * 1000)
                up_ask_size, up_bid_size = stale.up_ask_size, stale.up_bid_size
                venue_lag_ms = stale.lag_ms
        buy_up = up_ask if up_ask is not None else up.price
        buy_down = down_ask if down_ask is not None else down.price
        # No spread guess when the ask is the actual executable price.
        half_spread = 0.0 if up_ask is not None else settings.assumed_half_spread

        self.store.insert("quotes", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau,
            "up_price": up.price, "down_price": down.price,
            "up_chance": up.chance, "down_chance": down.chance,
            "liquidity": r.liquidity, "trade_volume": r.trade_volume,
            "spot": spot, "spot_ts_ms": feed.ts_ms,
            "oracle": None, "oracle_ts_ms": None,
            "strike": r.strike,
            "book_age_ms": book_age_ms,
            "up_ask_size": up_ask_size, "up_bid_size": up_bid_size,
            "venue_lag_ms": venue_lag_ms,
            "up_bid": up_bid, "up_ask": up_ask,
            "down_bid": down_bid, "down_ask": down_ask,
            "fee_rate_bps": r.fee_rate_bps,
        })

        # A round with no strike is TELEMETRY ONLY, and stops here.
        #
        # It has no reference price, so every gate below -- the cross arm (via
        # `_mkt`), the model, the impulse path and the double arm -- would be
        # reasoning about a level this market was never settled against. A
        # wrong reference is STRICTLY worse than none, because the arms gate on
        # `strike is None` in the first place.
        #
        # So the quote and the spot are written and nothing else runs. That is
        # what makes the round-notice lag and the open spot measurable for sol,
        # xrp and doge: previously they returned BEFORE the insert, so the tape
        # held no row for them at all and the starvation could not be diagnosed
        # from recorded data (measured 2026-09-17: 1,233,073 quote rows, all of
        # them btc/eth/bnb, zero for the other three families).
        if r.strike is None:
            return

        if self.mode == "record" or not trade or spot is None or tau <= 0:
            return

        # Per-market context for the cross arm, which now runs across every
        # family in CROSS_TRADED. tick() clears and rebuilds this each poll, so
        # a family that stops reporting drops out rather than going stale.
        mkt = getattr(self, "_mkt", None)
        if mkt is None:
            mkt = self._mkt = {}
        mkt[market_prefix(r.slug) or r.slug] = {
            "round": r, "asks": (buy_up, buy_down), "feed": feed,
            "symbol": symbol, "sigma": sigma, "primary": primary,
        }
        # THE IMPULSE AND DOUBLE ARMS STAY PRIMARY-ONLY. Their state below is
        # SINGLE-VALUED and `_impulse_tick` reads it from OUTSIDE this call, so
        # widening it would leave it holding whichever family ran last -- which
        # is exactly the bug the comment inside this block records.
        #
        # THE MODEL ARM IS WIDENED to every family in `active_markets()`
        # (2026-09-19, operator's instruction: one trade per round for each
        # coin). That is safe where the block below is not, because the model
        # path carries no cross-call state: the persistence and candle gates are
        # keyed `[r.slug]`, the price history is keyed `[symbol]`, sigma and the
        # quotes are arguments, and the entry uses the local `r`. The cap the
        # operator asked for is `guard.check(r.slug)` -- a slug IS one family's
        # round, so one position per coin per round falls out of it.
        #
        # `max_open_rounds` must be >= the number of traded families or it
        # becomes the real cap; it is 6 for the six families here.
        #
        # 15m IS SAFE ONLY BECAUSE `pricing_model` IS `digital`, which reads
        # tau_s and sigma_1m and nothing else. `models/sniper.py` hardcodes
        # ROUND_SECONDS = 300 (used as `window_age = ROUND_SECONDS - tau`, which
        # goes NEGATIVE at tau > 300) -- so selecting sniper while a 15m family
        # is traded is the failure config.py:44 warns about. Re-check that line
        # before changing `pricing_model`.
        if primary:
            # What the fast impulse path needs to act between polls (see
            # _impulse_tick). Deliberately not cached in record mode: `_round`
            # is the fast path's only precondition, so leaving it None is what
            # guarantees --mode record can never place an order.
            self._round = r
            self._asks = (buy_up, buy_down)
            self._sigma = sigma
            # The feed THIS round settles against, captured here because here is
            # the only place it is unambiguous: _record runs once per market in
            # MARKETS and each call reassigns self.spot, so after the loop
            # self.spot holds the LAST symbol's feed (eth-updown-15m ->
            # ETHUSDT) while _round holds the traded BTC market. _impulse_tick
            # runs on the websocket clock, i.e. outside this call, so reading
            # self.spot there compared ETH's price to BTC's strike (measured
            # 2026-09-12: spot=2530.835 vs strike=77344.005, -9672 bps) and
            # bought BTC Down because ETH had ticked. The same nonsense fair
            # priced an Up take-limit at max(0.01, 0 - 0.04) = 0.01, which is
            # the unfillable order that stalled the arm for an hour.
            self._primary_spot = feed
            await self._double_tick()

        # Guarded the same way `_ticks` is below, and for the same two reasons:
        # fixtures build a Bot without running __init__, and since the model arm
        # widened past the primary this runs for EVERY family -- so a symbol
        # with no seeded history must be skipped rather than raise. In
        # production every active family's symbol is seeded at startup
        # (`self.symbols` comes from `active_markets()`), and a family with no
        # history has nothing to price against anyway.
        hist = (getattr(self, "histories", None) or {}).get(symbol)
        if hist is None:
            return
        hist.add_tick(spot)
        # tick() now appends `_ticks` for EVERY symbol, so this only reads it.
        # Guarded lookup: fixtures build a Bot without running __init__, and the
        # tick buffer must not be the thing that breaks them.
        ticks = (getattr(self, "_ticks", None) or {}).get(symbol) or []

        # entry_mode "off" stops the MAIN arm here: the cross and double ticks
        # above have already run, the tape is already recorded, and the history
        # buffer above is still maintained so turning the arm back on does not
        # start from a cold estimator. Placed after those deliberately -- the
        # arms are independent, and switching one off must not silence another.
        if settings.entry_mode == "off":
            return

        sig = None
        # `and primary`: the impulse arm was measured on BTC 5m and its fast
        # path is primary-only (above), so widening the poll branch alone would
        # trade families the trigger was never measured on.
        if settings.entry_mode in ("impulse", "both") and primary:
            if not settings.impulse_on_tick:
                # Trade the underlying's own move. The model is not consulted:
                # its gate measured +1.3%/$1 net with a negative second half on
                # the same rounds, while impulses of 4-8 bps paid +8% to +21%
                # per $1 in BOTH halves. Sub-2 bps impulses are noise and lose
                # the fee, which is what impulse_bps refuses.
                # Only reached with impulse_on_tick OFF: the default owner of
                # the trigger is _impulse_tick, which runs on the feed's clock
                # instead of on this once-a-second poll (the venue answers in
                # 5 ms, so the poll interval was the entire latency budget).
                move = impulse_bps(ticks, settings.impulse_window_s)
                side = impulse_side(move, settings.impulse_bps)
                if side is not None:
                    px = buy_up if side == "Up" else buy_down
                    if px is not None and (settings.impulse_min_price <= px
                                           <= settings.impulse_max_price):
                        sig = Signal(side, px, px, 0.0, 0.0, 0.0, True,
                                     f"impulse {move:+.1f}bps/"
                                     f"{settings.impulse_window_s:.0f}s")
            # This return is UNCONDITIONAL within PURE impulse mode, and it
            # must stay that way: impulse mode exists because the model's gate
            # lost on the same rounds, so falling through to the model block
            # below would trade the model once a second and never the impulse.
            # With impulse_on_tick on, the entry is placed by _impulse_tick and
            # this poll's only job is recording.
            #
            # In "both" mode the fall-through is the POINT, and it does not
            # starve the impulse for two reasons: _impulse_tick runs on the
            # websocket clock and has already had its chance before this poll,
            # and the two gates want the same round only 52 times in 830
            # (measured 2026-09-12). `sig` stays None here, so the model block
            # below picks the round up exactly as it would in "model" mode.
            #
            # A duplicate of the trigger used to sit here, recomputing `move` --
            # which is UNDEFINED when impulse_on_tick is on. It was unreachable
            # only because the return above fired first, so it had to go before
            # this could fall through at all.
            if sig is None:
                if settings.entry_mode == "impulse":
                    return
            else:
                fair_up = sig.quote

        # Which gate produced this signal. In "both" mode the global
        # entry_mode can no longer answer that, and the persistence rule below
        # must apply to model entries while never applying to impulse ones.
        from_impulse = sig is not None

        pred = None
        if sig is None:
            pred = self.model.predict(hist.ctx(
                price=spot, strike=r.strike, tau_s=tau, sigma_1m=sigma,
                quote_up=buy_up, quote_down=buy_down,
                window_log_int=self._pm_window_log_int(r, symbol, tau)))
            if not pred.emitted or not (0.0 < pred.p_up < 1.0):
                log.debug("model %s holds on %s: %s", self.model.name, r.slug,
                          pred.reason)
                return
            fair_up = pred.p_up
        if sig is None:
            # Per-side pricing: Up may be priced by a different model (the drift
            # adjustment) than Down (the driftless digital).
            up_fair = fair_up
            if self.up_model is not None:
                pred_up = self.up_model.predict(hist.ctx(
                    price=spot, strike=r.strike, tau_s=tau, sigma_1m=sigma,
                    quote_up=buy_up, quote_down=buy_down,
                    window_log_int=self._pm_window_log_int(r, symbol, tau)))
                if pred_up.emitted and 0.0 < pred_up.p_up < 1.0:
                    up_fair = pred_up.p_up
            sig_kw = dict(
                # fee=None -> per-quote fee from the measured curve
                # (fee_bps = 200 * min(1, (1-px)/px)). The 200 is the venue's own
                # reported fee_rate_bps, which it states on 99.7% of recorded ticks,
                # so a per-round override would change nothing today. Do NOT pass
                # fee_rate_bps/1e4 directly: that is the coefficient, and charging it
                # flat would over-price favourites by ~6x and under-price cheap
                # contracts by ~2x.
                fee=None,
                half_spread=half_spread,
                safety_margin=settings.safety_margin,
                fee_model=FeeModel.NOTIONAL,
                max_quote=settings.max_quote,
                min_quote=settings.min_quote,
                min_edge=settings.min_edge,
                edge_haircut=settings.edge_haircut,
            )
            # Rising-regime gate (see config.up_regime_gate): when the last hour is
            # already up, the venue has priced Up correctly and our Up edge is
            # anti-informative (measured WR 0.50 rising vs 0.82 flat/down). Decided
            # BEFORE the side pick, so a blocked Up still falls back to a Down
            # candidate that qualified on the same quote.
            up_allowed, blocked_ret = True, None
            if settings.up_regime_gate > 0:
                ret = hist.one_hour_return()
                if ret is not None and ret > settings.up_regime_gate:
                    up_allowed, blocked_ret = False, ret
            sig = evaluate_hybrid(fair_up, up_fair, buy_up, buy_down,
                                  up_allowed=up_allowed, **sig_kw)
            if blocked_ret is not None and not sig.act:
                sig = Signal(None, sig.fair, sig.quote, sig.edge, sig.hurdle,
                             sig.ev_per_dollar, False,
                             f"Up skipped: 1h ret {blocked_ret:+.4f} > "
                             f"{settings.up_regime_gate:+.3f}")

        # A signal must hold for several consecutive polls. A one-tick edge is
        # usually our own stale book or spot sample rather than a real
        # dislocation, and acting on it was trading noise.
        # Each 1m candle is a fresh prediction problem, so the streak restarts
        # at every candle open.
        candle_idx = now_ms // 60_000
        if self._candle.get(r.slug) != candle_idx:
            self._candle[r.slug] = candle_idx
            self._streak.pop(r.slug, None)
        prev_side, prev_n = self._streak.get(r.slug, (None, 0))
        if sig.act and sig.side == prev_side:
            self._streak[r.slug] = (sig.side, prev_n + 1)
        elif sig.act:
            self._streak[r.slug] = (sig.side, 1)
        else:
            self._streak.pop(r.slug, None)
        streak = self._streak.get(r.slug, (None, 0))[1]

        gate_reason = ""
        # With candle-open gating the final candle opens at tau ~= 59.9s, just
        # under min_tau_s=60. The allowance lets that open stand for its own
        # time; mid-candle ticks are blocked by the candle gate itself.
        min_tau_eff = (settings.min_tau_s - settings.candle_open_window_s
                       if settings.trade_on_candle_open else settings.min_tau_s)
        # Impulse entries are deliberately NOT subject to the persistence rule:
        # the dislocation is the event, and waiting two polls for it to repeat
        # is exactly how the venue's book catches up. The rule was validated with
        # one entry per round on the first qualifying tick.
        if (sig.act and not from_impulse
                and streak < settings.signal_persistence):
            gate_reason = f"signal held {streak}/{settings.signal_persistence} polls"
        elif (settings.side_filter != "both"
              and sig.act and sig.side != settings.side_filter):
            gate_reason = (f"side {sig.side} filtered "
                           f"(trading {settings.side_filter} only)")
        elif (settings.trade_on_candle_open
              and sec_into_candle(now_ms) >= settings.candle_open_window_s):
            gate_reason = (f"mid-candle ({sec_into_candle(now_ms)}s past open; "
                           f"trades only within {settings.candle_open_window_s:.0f}s)")
        elif (settings.trade_on_candle_open
              and round(tau / 60.0) not in settings.trade_candle_opens):
            gate_reason = (f"candle {round(tau / 60.0)} not in "
                           f"{settings.trade_candle_opens}")
        elif not (min_tau_eff <= tau <= settings.max_tau_s):
            gate_reason = f"tau {tau:.0f}s outside window"
        elif (settings.model_max_round_range_bps > 0
              and (rng := self._round_range_bps(r)) is not None
              and not (settings.model_min_round_range_bps
                       <= rng <= settings.model_max_round_range_bps)):
            # This arm only pays in rounds that have MOVED. Outside 5-20bps it
            # is a measured loser in both directions -- -0.127/$1 in calm rounds
            # and -0.177/$1 past 20bps. See config.model_min_round_range_bps.
            gate_reason = (f"round range {rng:.1f}bps outside "
                           f"[{settings.model_min_round_range_bps:.0f},"
                           f"{settings.model_max_round_range_bps:.0f}]")
        elif not self.spot.is_fresh(settings.max_spot_age_s):
            gate_reason = f"spot stale ({self.spot.age_s:.1f}s)"
        elif self.broker.mode == "live" and sig.act and self.book:
            # The venue is the ground truth. If it already shows a position in
            # this round (a fill the bot never booked, e.g. after a restart or
            # an unverifiable order), buying again would double the exposure.
            unbooked = await self._unbooked_round_tokens(r, up, down)
            if unbooked is None:
                gate_reason = "venue position list unavailable"
            elif unbooked:
                gate_reason = "venue has an UNBOOKED position for this round"
        elif time.time() < self._order_cooldown_until:
            gate_reason = "order cooldown after a failure"

        # The risk guard is always consulted when a signal would otherwise act.
        # It must never sit in an else branch: any earlier elif that consumes
        # the chain silently disables it (that is exactly how the daily loss
        # cap was bypassed and today's loss ran to -$5 instead of -$3).
        if not gate_reason:
            allowed, why = self.guard.check(r.slug)
            if not allowed:
                gate_reason = why

        acted = False
        stake = 0.0
        if sig.act and not gate_reason:
            # Edge scaling: the weakest signal the gate accepts is staked at the
            # venue floor, the strongest at the ceiling (see config.sizing_mode).
            # Kelly is available but off by default -- at this bankroll a
            # fractional-Kelly value sits below the venue minimum, and rounding
            # that up to full size inverts the rule it came from.
            # |z| -- how decided the round already is, in units of the remaining
            # move. Used by the sizing tilt; 0.0 means "no information".
            mz = (abs(math.log(spot / r.strike))
                  / (sigma * math.sqrt(tau / 60.0))
                  if spot and r.strike and spot > 0 and r.strike > 0
                  and tau > 0 and sigma > 0 else 0.0)
            stake = self._stake_for(sig, mz)
            acted = stake >= settings.min_order_usdt

        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau,
            "side": sig.side or "NONE", "fair": sig.fair, "quote": sig.quote,
            "edge": sig.edge, "hurdle": sig.hurdle, "sigma_1m": sigma,
            "spot": spot, "oracle": None, "strike": r.strike,
            # `acted` means an order actually filled, so it is written as 0 here
            # and only promoted after the broker confirms. Recording intent
            # instead made a run of rejected orders look like 100 trades.
            "stake": stake, "acted": 0,
            "reason": gate_reason or sig.reason,
        })

        if not acted:
            if gate_reason:
                await self._probe(r, sig, gate_reason=gate_reason, up=up,
                                  down=down, spot=spot, sigma=sigma, tau=tau)
            return

        token = up.token_id if sig.side == "Up" else down.token_id

        # Stale-display maker path (see config.stale_maker_*). The display lags
        # spot by ~2s while the book does not, and the measured edge is ENTIRELY
        # in the fill price: at the book ask this same rule scores +0.0175/$1
        # (t 0.47), at the mid +0.3449 (t 5.88). So when the display is stale
        # this rests a bid near it instead of crossing to the ask.
        #
        # It REPLACES the taker order for this entry rather than adding one --
        # resting and crossing the same signal would double the exposure the
        # one-position-per-round rule exists to prevent. The cost is fill rate,
        # which is the unknown this is built to measure: an unfilled attempt
        # keeps its decision row (acted=0) and is logged, so the rate is
        # recoverable from the database.
        if settings.stale_maker_enabled and hasattr(self.broker, "buy_limit"):
            disp = up.price if sig.side == "Up" else down.price
            gap = (sig.quote - disp) if disp else 0.0
            edge = (sig.fair - disp) if disp else 0.0
            post = stale_maker_px(disp, sig.quote, sig.fair,
                                  settings.stale_maker_offset,
                                  settings.limit_ask_tick)
            if (post is not None
                    and gap >= settings.stale_maker_min_gap
                    and edge >= settings.stale_maker_min_edge):
                log.info("STALE-MAKER post %s %s @%.2f (display %.3f ask %.3f "
                         "fair %.3f gap %+.3f edge %+.3f) stake=$%.2f wait=%.0fs",
                         r.slug, sig.side, post, disp, sig.quote, sig.fair,
                         gap, edge, stake, settings.stale_maker_wait_s)
                fill = await self.broker.buy_limit(
                    slug=r.slug, side=sig.side, token_id=token, limit_px=post,
                    stake=stake, wait_s=settings.stale_maker_wait_s,
                    fill_break=settings.limit_fill_break)
                if not fill:
                    # The measurement this arm exists for: the decision row
                    # stays at acted=0, so fill rate is (fills / attempts).
                    log.info("STALE-MAKER unfilled %s %s @%.2f after %.0fs",
                             r.slug, sig.side, post, settings.stale_maker_wait_s)
                    return
                await self._book_fill(r, sig, fill, did=did, up=up, spot=spot,
                                      sigma=sigma, tau=tau, token=token,
                                      mode="stale_maker")
                return

        fill = await self._place_order(
            r, sig,
            fair_up=sig.fair_up_model if sig.fair_up_model is not None else fair_up,
            token=token, stake=stake)
        if not fill:
            return
        await self._book_fill(r, sig, fill, did=did, up=up,
                              spot=spot, sigma=sigma, tau=tau, token=token)

    async def _probe(self, r, sig, *, gate_reason: str, up, down,
                     spot: float, sigma: float, tau: float) -> None:
        """Trade a DECLINED decision at minimum stake, booked as mode='probe'.

        Purpose is measurement, not profit: the gate declines thousands of
        decisions a day and we cannot tell from the recorded quote which of
        those cells pay (our real fills beat that quote by ~1% of stake). Take
        a small real position in them so the marginal cell can be priced from
        fills instead of from a biased quote, then promote it only if it clears
        day-clustered t > 2 with a positive drop-best-day.

        Deliberately never probes a decision declined for exposure or risk
        reasons: one-position-per-round and the daily halt are real limits, and
        probing around them would create the double-exposure the gate exists to
        prevent.
        """
        if not settings.probe_enabled or sig.side not in ("Up", "Down"):
            return
        if not (0.0 < sig.quote < 1.0):
            return
        # Risk, exposure and data blocks are NOT selectivity. Leave them alone.
        if not probeable(gate_reason):
            return
        fired = getattr(self, "_probe_fired", None)
        if fired is None:
            fired = self._probe_fired = set()
        if r.slug in fired:
            return
        day0 = int(time.time() * 1000) // 86_400_000 * 86_400_000
        try:
            got = self.store.query(
                "SELECT COUNT(*) AS c FROM fills WHERE mode='probe' AND ts_ms>=?",
                (day0,))
        except Exception:
            got = []
        if got and got[0]["c"] >= settings.probe_max_per_day:
            return
        stake = max(settings.probe_stake, settings.min_order_usdt)
        if self.bankroll < stake:
            return
        allowed, why = self.guard.check(r.slug)
        if not allowed:
            return
        fired.add(r.slug)
        token = up.token_id if sig.side == "Up" else down.token_id
        psig = Signal(sig.side, sig.quote, sig.quote, 0.0, 0.0, 0.0, True,
                      f"probe[{gate_reason}]")
        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": int(time.time() * 1000), "tau_s": tau,
            "side": sig.side, "fair": sig.quote, "quote": sig.quote,
            "edge": 0.0, "hurdle": 0.0, "sigma_1m": sigma, "spot": spot,
            "oracle": None, "strike": r.strike, "stake": stake, "acted": 0,
            "reason": f"probe: {gate_reason}",
        })
        fill = await self._place_order(
            r, psig,
            fair_up=(1.0 - sig.quote) if sig.side == "Down" else sig.quote,
            token=token, stake=stake, skip_pretrade_gates=True,
            take_px=sig.quote)
        if not fill:
            log.info("probe %s not filled at %.3f (%s)", r.slug, sig.quote,
                     gate_reason)
            return
        await self._book_fill(r, psig, fill, did=did, up=up, spot=spot,
                             sigma=sigma, tau=tau, token=token, mode=PROBE_MODE)
        log.info("PROBE %s %s $%.2f @%.4f tau %.0fs | declined: %s",
                 r.slug, sig.side, fill.stake, fill.filled_px, tau, gate_reason)

    async def _book_fill(self, r, sig, fill, *, did: int, up,
                         spot: float, sigma: float, tau: float,
                         token: str | None = None,
                         mode: str | None = None) -> None:
        """Record a fill: guard, decision promotion, fill row, P&L log.

        `token` is passed explicitly rather than read off the broker's Fill:
        450 of 455 live fills were persisted with token_id NULL although both
        call sites and the broker do carry it, which left the venue-position
        verification unanswerable and P&L unattributable to Up vs Down. The
        caller always knows the token, so take it from the caller.

        `mode` overrides the broker's mode so a measurement arm (probe) can book
        its fills separately without touching the live record.

        Shared by the poll path and the fast impulse path (`_impulse_tick`) so
        the two cannot drift. A fill booked on one path and not the other is
        exactly how the risk guard loses track of the exposure it is guarding.
        """
        # The cross arm holds the RESERVED slot, not the main one. The model
        # arm enters at tau 60-210 and holds to settlement, covering the cross
        # arm's whole window, so sharing one slot let the model crowd it out of
        # 82% of the rounds it wanted -- 61% of them the OPPOSITE side.
        reserved, pool = pool_for_mode(mode or self.broker.mode)
        self.guard.on_open(r.slug, reserved=reserved, pool=pool)
        # The gate's ev/$ is quoted at the price it saw; this is the same
        # quantity at the price paid. `None` when the fill price is unusable
        # (never a guessed number -- the log would then be quietly wrong).
        ev_at_fill = None
        if fill.filled_px and 0.0 < fill.filled_px < 1.0 and 0.0 < sig.fair < 1.0:
            ev_at_fill = expected_value(sig.fair, fill.filled_px,
                                        fee_rate_for_price(fill.filled_px))
        self.store.conn.execute("UPDATE decisions SET acted=1 WHERE id=?", (did,))
        self.store.conn.commit()
        fid = self.store.insert("fills", {
            "decision_id": did, "slug": r.slug, "ts_ms": fill.ts_ms,
            "mode": mode or self.broker.mode, "side": fill.side,
            "requested_px": fill.requested_px, "filled_px": fill.filled_px,
            "shares": fill.shares, "stake": fill.stake, "fee_paid": fill.fee_paid,
            "order_id": fill.order_id, "raw": fill.raw,
            # Persisted so a restored fill can still be verified at settlement:
            # an unverified one is checked against the venue's position list by
            # token, and NULL would make that check answer "not held" every time.
            "token_id": token or getattr(fill, "token_id", None),
            # What the trade was worth at the price actually paid. The gate
            # prices it at the quote it saw; the fill lands a second or two
            # later. Measured on 51 live fills this gap costs 0.0132 per $1 --
            # about a third of the claimed edge, worse on 78% of fills -- so it
            # is recorded per fill instead of being assumed away.
            "ev_at_fill": ev_at_fill,
        })
        self.open_fills[fid] = (r.slug, fill)
        # EXIT-ON-FLIP: remember what this position IS, so the watcher can sell
        # it if the trigger that opened it turns (config.cross_exit_enabled).
        # Keyed by SLUG because that is what a tick has; the fid rides along so
        # the sale can close exactly this fill.
        #
        # Cross arm only, live only, and only when the broker can actually
        # sell: `sell_fak` is new, so a paper broker or an older one must
        # simply not get a watcher rather than fail at the first flip.
        if (mode or self.broker.mode) == "cross":
            # All the guards live in `_arm_exit_watch`, so the booking path and
            # the restart-restore path cannot drift apart (the audit found they
            # had: a restart left every live position unwatched).
            self._arm_exit_watch(
                fid=fid, slug=r.slug, side=fill.side,
                token=token or getattr(fill, "token_id", None),
                shares=fill.shares, stake=fill.stake, fee=fill.fee_paid,
                entry_ms=fill.ts_ms, did=did, px=fill.filled_px,
                req=fill.requested_px, oid=fill.order_id)
        # implied_sigma inverts P(up), so it must always be fed the Up quote --
        # passing the chosen side's quote breaks the inversion when that side is
        # Down. A missing Up quote must not take down the tick after the fill
        # has already been booked and stored.
        try:
            iv = implied_sigma(up.price, spot, r.strike, tau)
        except (TypeError, ValueError):
            iv = None
        # Tag the arm and stop inventing an EV for it. The Train arm has no model
        # valuation -- its trigger is the underlying's move off the strike -- so
        # its synthetic Signal carries fair == quote == the ask and this line
        # printed `edge=+0.000 hurdle=0.000 ev/$=+0.0000` on every Train fill: a
        # fabricated zero, the same class of lie as the -1.0 sentinel. All 658
        # OPEN lines were indistinguishable between the two arms. No tool parses
        # this line (checked), so the prefix is safe to extend.
        arm = arm_of(sig)
        # The gate EV is only meaningful when the signal HAS a model valuation.
        # BOTH arms build a synthetic Signal whose `fair` is the ask they are
        # about to pay, so edge == 0 and ev/$ is a structural zero -- while
        # `ev_valid` defaults True, so OPEN printed a fabricated "+0.0000" for
        # Cross as well. Labelling only Train (earlier today) was an incomplete
        # fix resting on the wrong assumption that Train alone lacked a
        # valuation. The 06:04:14 Cross fill proves otherwise: OPEN printed
        # ev/$=+0.0000 while the pre-trade recheck on that same order reported a
        # real ev/$ of +0.0577 at px 0.5500.
        log.info("OPEN %s %s %s stake=$%.2f @%.3f | %s | mkt_iv=%s realised=%.5f "
                 "| ev/$ gate->fill %s -> %s",
                 arm, r.slug, fill.side, fill.stake, fill.filled_px, sig,
                 f"{iv:.5f}" if iv else "n/a", sigma,
                 f"{sig.ev_per_dollar:+.4f}"
                 if (sig.ev_valid and sig.edge) else "n/a",
                 f"{ev_at_fill:+.4f}" if ev_at_fill is not None else "n/a")

    async def _impulse_tick(self) -> None:
        """Impulse entry, evaluated on every price update instead of on the poll.

        Measured 2026-09-12 from this host: the venue answers in 5.3 ms (three
        round trips, 16 ms) while the poll loop looked at the price once every
        1002 ms. So the poll interval WAS the trigger-to-order latency: a move
        the websocket showed at t was acted on at the next poll, ~500 ms later
        on average and 1 s at worst, and the side being bought keeps drifting
        for seconds after the impulse. This evaluates the SAME statistic (a
        |window| bps move in the underlying) the moment the window closes and
        goes straight to the order.

        Free when it does not fire: no venue calls, no writes, no logging.
        """
        # This arm had NO enable flag until 2026-09-13, which is how it went
        # live by accident: it is called unconditionally from the poll and the
        # idle loop, and only `guard.check()` -- the MAIN arm's slot, almost
        # always occupied -- had been keeping it silent. Per-arm risk pools
        # removed that accident and it placed its first ever order within
        # hours. See config.impulse_enabled for why it measures negative at a
        # realistic fill.
        if not settings.impulse_enabled:
            return
        r = getattr(self, "_round", None)
        # NEVER `self.spot` here: that attribute is rewritten once per market as
        # the poll walks MARKETS, so by the time this websocket-clock path runs
        # it holds the LAST symbol's feed (ETHUSDT), not the feed of the round in
        # _round (BTCUSDT). Using it made the trigger fire on ETH's move and then
        # trade the BTC market, and made the take-limit fair meaningless.
        feed = getattr(self, "_primary_spot", None)
        if feed is None:
            feed = self.spots.get(MARKETS[0][1])
        ticks = getattr(feed, "ticks", None)
        if r is None or not ticks or r.slug in self._impulse_fired:
            return
        # Only the live round can be re-entered, so nothing older is worth
        # keeping -- the set would otherwise grow all day.
        self._impulse_fired.intersection_update({r.slug})
        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        if not (settings.min_tau_s <= tau <= settings.max_tau_s):
            return
        up, down = r.up, r.down
        if not up or not down or r.strike is None:
            return
        if not feed.is_fresh(settings.max_spot_age_s):
            return
        # Honour the order cooldown. _place_order sets it after a rejection that
        # survived every attempt, so that a persistent venue-side condition is
        # not hammered for the rest of the round -- at one attempt per second
        # that was rude; at one per websocket tick it would be an attack.
        if time.time() < self._order_cooldown_until:
            return
        move = impulse_bps(list(ticks), settings.impulse_window_s)
        side = impulse_side(move, settings.impulse_bps)
        if side is None:
            return
        # Band sanity only, from the last asks the poll saw (<= 1 s old). The
        # price that binds is the one that comes back with the order's own
        # quote, which _place_order fetches and reuses.
        px = self._asks[0] if side == "Up" else self._asks[1]
        if px is None or not (settings.impulse_min_price <= px
                              <= settings.impulse_max_price):
            return
        # The venue is ground truth on exposure: a fill this process never
        # booked (restart, unverified order) must not be doubled. Measured at
        # 5 ms, so unlike the poll's other work this is not worth caching.
        if self.broker.mode == "live" and self.book:
            # Only exposure the ledger cannot explain refuses this arm; another
            # arm holding the round does not. See _unbooked_round_tokens.
            unbooked = await self._unbooked_round_tokens(r, up, down)
            if unbooked is None or unbooked:
                return
        # Its OWN budget, not the main arm's. Sharing one meant whichever arm
        # fired first disabled the other for the round.
        allowed, why = self.guard.check_pool(
            r.slug, pool="impulse", max_open=settings.impulse_max_open)
        if not allowed:
            log.info("impulse %+.1fbps %s not taken: %s", move, side, why)
            return
        sig = Signal(side, px, px, 0.0, 0.0, 0.0, True,
                     f"impulse {move:+.1f}bps/{settings.impulse_window_s:.0f}s")
        spot = feed.price
        sigma = getattr(self, "_sigma", 0.0)
        mz = (abs(math.log(spot / r.strike)) / (sigma * math.sqrt(tau / 60.0))
              if spot and spot > 0 and sigma > 0 and tau > 0 else 0.0)
        stake = self._stake_for(sig, mz)
        if stake < settings.min_order_usdt:
            return
        # Claim the round BEFORE ordering. A rejected order then leaves the
        # round skipped rather than possibly bought twice, and _place_order
        # already retries a confirmed rejection `order_max_attempts` times.
        self._impulse_fired.add(r.slug)
        log.info("TRIGGER %+.1fbps/%ds %s %s tau=%.0fs px=%.3f stake=$%.2f",
                 move, int(settings.impulse_window_s), side, r.slug, tau, px, stake)
        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau,
            "side": side, "fair": sig.fair, "quote": sig.quote,
            "edge": sig.edge, "hurdle": sig.hurdle, "sigma_1m": sigma,
            "spot": spot, "oracle": None, "strike": r.strike,
            # `acted` is only promoted after the broker confirms the fill, so a
            # run of rejected orders cannot look like a run of trades.
            "stake": stake, "acted": 0,
            "reason": sig.reason,
        })
        # Which side of the CONTRACT we are buying and what we think it is worth.
        # Both executions below need these, and `px` is the ASK we read, not the
        # model's view -- they are different numbers and conflating them is how
        # the take-limit collapsed to 0.01 on 2026-09-12.
        fair_model = fair_value(spot, r.strike, tau, sigma) if sigma > 0 else px
        fair_side = fair_model if side == "Up" else 1.0 - fair_model
        token = up.token_id if side == "Up" else down.token_id

        if settings.impulse_execution == "maker" and not hasattr(
                self.broker, "buy_limit"):
            log.warning("maker execution requested but %s has no buy_limit; "
                        "taking this entry instead",
                        type(self.broker).__name__)
        if settings.impulse_execution == "maker" and hasattr(
                self.broker, "buy_limit"):
            maker_px = maker_limit_px(fair_side, px, settings.limit_ask_tick)
            if maker_px is None:
                log.info("maker %s skipped: fair %.3f vs ask %.3f leaves no "
                         "price to rest on", r.slug, fair_side, px)
                return
            log.info("MAKER post %s %s @%.2f (fair %.3f, ask %.3f, tau %.0fs) "
                     "stake=$%.2f", r.slug, side, maker_px, fair_side, px, tau,
                     stake)
            fill = await self.broker.buy_limit(
                slug=r.slug, side=side, token_id=token, limit_px=maker_px,
                stake=stake, wait_s=settings.impulse_maker_wait_s,
                fill_break=settings.limit_fill_break)
            if not fill:
                log.info("maker %s unfilled at %.2f after %.0fs", r.slug,
                         maker_px, settings.impulse_maker_wait_s)
                return
            await self._book_fill(r, sig, fill, did=did, up=up, spot=spot,
                                  sigma=sigma, tau=tau, token=token,
                                  mode="maker")
            return

        fill = await self._place_order(
            r, sig,
            # Under a taker limit this fair IS the price ceiling, so it has to be
            # a real probability rather than the venue's own quote. `px` is the
            # displayed price -- the number measured lagging its own book by up
            # to 0.40 during a move -- so pricing the limit off it would refuse
            # exactly the dislocations this arm exists to take (display 0.15,
            # book 0.60, digital 0.91: a 0.15 limit fills nothing, a 0.90 limit
            # fills at 0.60). The digital needs no new inputs: spot, strike, tau
            # and sigma are all already here.
            #
            # `take_px` is the FLOOR, and the impulse arm needs it more than any
            # other caller. `take_limit_px` computes max(fair - margin, take_px),
            # so with no floor the limit collapses whenever the digital fair is
            # far from the market -- exactly the case the impulse arm creates on
            # purpose, because it ignores the model. Measured live 2026-09-12:
            # a +9.8bps/2s Up trigger fired while spot was still ~14bps BELOW the
            # round's strike, so P(Up) ~ 0, the limit went out at max(0.01,
            # 0 - 0.04) = 0.01 against a 0.730 book, rested 20s, never filled,
            # and cost the round plus a 30s order pause. The cross arm already
            # passes its ask for this reason; the impulse arm is in the same
            # position -- it has no valuation to price the ceiling from, and its
            # own band (impulse_min/max_price) plus impulse_price_refusal are
            # what sanity-check the price. The `quote + tick` cap still applies,
            # so this can never bid above the ask it read.
            fair_up=fair_model,
            token=token, stake=stake,
            take_px=px,
            # This order came from the impulse gate, not the model's. In "both"
            # mode that is no longer implied by settings.entry_mode.
            impulse_order=True)
        if not fill:
            return
        await self._book_fill(r, sig, fill, did=did, up=up,
                              spot=spot, sigma=sigma, tau=tau, token=token,
                              # Its own LABEL, so settle releases the impulse
                              # pool rather than the main arm's slot.
                              # Tagged in paper too: the label says which ARM,
                              # not which broker, and check_pool/on_open would
                              # otherwise disagree about which budget it spends.
                              mode="impulse")

    def _ck(self, name: str, r=None):
        """A cross knob for ONE round.

        `r` is explicit because the arm now runs across every family in
        CROSS_TRADED and the knobs differ between them -- reading `self._round`
        here would price eth-updown-15m with BTC's range gate. It falls back to
        `self._round` only for the single-market call path.

        Unknown markets fall through to the global `settings.cross_<name>`, so
        widening MARKETS cannot silently change how BTC trades. See
        config.cross_overrides for the measured per-market spreads.
        """
        if r is None:
            r = getattr(self, "_round", None)
        return cross_knob(market_prefix(getattr(r, "slug", None)), name)

    def _round_range_bps(self, r) -> float | None:
        """How far this round has travelled, open -> now, in bps of the strike.

        Read from `self._ticks`, NOT `feed.ticks`. The feed deque's SPAN is a
        burst rate, not a duration: 40,000 updates is ~5 minutes at BTC's live
        ~114/s and ~35 seconds if a burst triples that, so how far back it
        reaches is not something this gate can rely on. `self._ticks` is
        appended once per poll and holds 2400 samples, so its span is fixed by
        the poll cadence.

        Returns None when there is not enough history to judge -- callers must
        treat that as "no opinion" and NOT as a refusal, or a cold start would
        silently disable the arm.
        """
        if not r or not getattr(r, "strike", None):
            return None
        buf = (getattr(self, "_ticks", None) or {}).get(symbol_for_round(r)) or []
        seen = [px for t_ms, px in buf if t_ms >= r.start_ms and px > 0]
        if len(seen) < 2:
            return None
        return 1e4 * (max(seen) - min(seen)) / r.strike

    def _cross_level(self, r) -> tuple[float | None, str]:
        """(level, source) the cross arm measures its crossing AGAINST.

        NOT `r.strike`, and that is the fix. `r.strike` is the spot at the open
        INSTANT; the venue pays on `TWAP60(at the bell) >= TWAP60(at the open)`,
        so the level to compare a crossing to is the 60 s mean at the boundary.
        Measured on 1,509 live Polymarket rounds that mean reproduces the
        venue's own winner 94.1% against 89.1% for the point (McNemar z +7.31
        on 92-vs-16 discordant rounds) and it differs from `r.strike` by more
        than 2 bps on 40% of rounds -- a band the arm's whole tick floor sits
        inside. See config.cross_ref_twap for the tables.

        Read from `self._ticks`, NOT `feed.ticks`, for the reason
        `_round_range_bps` states: the feed deque's span is a BURST RATE rather
        than a duration, while `self._ticks` is appended once per poll and holds
        2400 samples -- 40 minutes, so it brackets the 60 s reference window of
        every round the process has been alive for.

        Memoised per slug. The window is entirely in the past by the time the arm
        looks, so the answer is final and re-deriving it on the websocket clock
        would rescan the buffer per family per tick. Two cases are NOT cached:
        a round that has not opened yet (its reference does not exist, and
        caching `r.strike` for it would freeze today's behaviour onto the round
        the fix is for), and a fallback that may yet be superseded by the tape.

        WHEN THE TAPE CANNOT COVER THE WINDOW -- a process that booted after
        the open, a stalled tape, an unknown family -- the level is read from
        Binance's 1 s klines instead, which are retrospective (see
        `_cross_level_kline`). `r.strike` is used only when no kline answer can
        arrive at all, and that case is a WARNING naming the strike that was
        used. The old note here said the fallback "is exactly the behaviour that
        shipped before this existed, so it is never a new risk", and on
        2026-09-18 that claim cost a real entry $1.05 -- the measurement is on
        the code below, and it is not a new risk but a swapped estimator.
        """
        strike = getattr(r, "strike", None)
        # The level is a 60 s MEAN only where the venue pays on one; on a
        # point venue `r.strike` IS the thing settled against.
        if not (self._ck("ref_twap", r) and self._settles_on_twap()):
            return strike, "strike"
        slug = getattr(r, "slug", None)
        cache = getattr(self, "_cross_ref", None)
        if not isinstance(cache, dict):
            cache = self._cross_ref = {}
        hit = cache.get(slug)
        if hit is not None:
            return hit
        start_ms = int(getattr(r, "start_ms", 0) or 0)
        if not start_ms or start_ms > int(time.time() * 1000):
            # PRE-OPEN. A round that has not started has no reference window,
            # and this is the one case that must not be cached.
            return strike, "strike"
        buf = (getattr(self, "_ticks", None) or {}).get(symbol_for_round(r)) or []
        ref = twap_reference(buf, start_ms)
        if ref is not None:
            cache[slug] = (ref, "twap60")
            return cache[slug]
        # THE TAPE CANNOT COVER THE WINDOW -- a process that booted after the
        # open (the round's reference minute is partly pre-boot), a stalled
        # tape, an unknown family.
        #
        # ⛔ THIS USED TO GO STRAIGHT TO `r.strike`, AND THAT IS WHAT LOST
        # `bnb-updown-5m-1789710600` ON 2026-09-18. A restart at 05:49:35 (a
        # deploy) put the round that opened at 05:50:00 on the strike -- 752.095,
        # the SPOT AT THE OPEN INSTANT -- while the LEVEL was 752.5447, +5.98
        # bps away: BNB had dropped ~6 bps INTO the open, so the point sat under
        # the mean of the minute before it. The arm fired on `fc 752.13 vs
        # strike 752.10` = +0.47 bps, which clears the 0.25 bps floor; read
        # against the level it is -5.51 bps, i.e. THE FLOOR REFUSES IT. The
        # venue settled Down and the market had said so from the first tick
        # (0.26/0.76 at the open, 0.22/0.80 at the entry, 0.01/1.00 at tau 43).
        # One entry, -$1.05, on a rule every gate in this arm exists to block.
        #
        # The note that used to sit here -- "the fallback is exactly what
        # shipped before this existed, so it is never a new risk" -- is FALSE,
        # and false in the way that does not raise: it swaps a 60 s MEAN for a
        # POINT, and the two differ by more than the 0.25 bps floor on ~40% of
        # rounds.
        #
        # Binance's 1 s klines are RETROSPECTIVE, so unlike the tape they can
        # still answer for a round whose reference minute predates this process
        # -- the same REST source `_pm_strike_backfill_kline` already recovers a
        # strike from. While that read is out this returns None, and
        # `_cross_tick` returns on None: THE ROUND IS NOT TRADED for the poll the
        # answer takes, rather than being priced off a level that cannot be
        # measured. `r.strike` stays the answer, as today, only when no kline
        # answer can ever arrive (no session, no running loop, an unknown
        # symbol) or after `CROSS_LEVEL_KLINE_TRIES` attempts have failed -- and
        # that case names the strike it used, at WARNING, once per round.
        kline = getattr(self, "_cross_kline", None)
        if not isinstance(kline, dict):
            kline = self._cross_kline = {}
        tries = kline.get(slug, 0)
        answer = ("impossible" if tries >= CROSS_LEVEL_KLINE_TRIES
                  else self._ensure_cross_level_kline(r, start_ms))
        if answer != "impossible":
            if answer == "started":
                kline[slug] = tries + 1
                if tries == 0:
                    log.info(
                        "cross %s: no 60s reference on the tape -- reading the "
                        "level off the 1s klines (the round opened before this "
                        "process started); no entry until it lands", slug)
            return None, "kline-pending"
        cache[slug] = (strike, "strike")
        seen = getattr(self, "_cross_nolevel", None)
        if not isinstance(seen, set):
            seen = self._cross_nolevel = set()
        if slug not in seen:
            if len(seen) > 400:
                seen.clear()
            seen.add(slug)
            log.warning(
                "cross %s: NO LEVEL -- the tape cannot cover the reference "
                "window and the 1s kline read did not land (see "
                "_cross_level_kline), so this round is priced off the strike "
                "%.6f. That is a POINT, not the 60 s mean the venue pays: on "
                "bnb-updown-5m-1789710600 the two were 5.98 bps apart and the "
                "difference was a -$1.05 entry on 2026-09-18", slug,
                strike or 0.0)
        return cache[slug]

    def _ensure_cross_level_kline(self, r, start_ms: int) -> str:
        """Start (or find) the 1 s kline read of this round's LEVEL.

        'started'    -- a task is now out; the answer lands within a poll.
        'inflight'   -- one is already out; do not pile another REST call on it.
        'impossible' -- no session (fixtures build Bot via __new__), no running
                        loop (a sync caller), no slug, or no symbol: nothing is
                        coming, so the caller falls back to `r.strike`.

        A TASK rather than an inline await because `_cross_level` is
        SYNCHRONOUS: it is called once per family per tick and `_cross_tick`
        needs its answer before it can evaluate anything. That is what lets the
        tick fail closed for one poll instead of blocking the poll loop on a
        REST call -- and it is why every fixture-built Bot (no session) keeps
        the pre-change behaviour rather than timeouting a test.
        """
        if getattr(self, "session", None) is None:
            return "impossible"
        slug = getattr(r, "slug", None)
        if not slug:
            return "impossible"
        if SYMBOL_BY_PREFIX.get(market_prefix(slug) or "") is None:
            return "impossible"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:                    # a sync caller: no task exists
            return "impossible"
        tasks = getattr(self, "_cross_kline_tasks", None)
        if not isinstance(tasks, dict):
            tasks = self._cross_kline_tasks = {}
        live = tasks.get(slug)
        if live is not None and not live.done():
            return "inflight"
        task = loop.create_task(self._cross_level_kline(r, start_ms))
        tasks[slug] = task
        task.add_done_callback(lambda _t, s=slug: tasks.pop(s, None))
        return "started"

    async def _cross_level_kline(self, r,
                                 start_ms: int | None = None,
                                 ) -> float | None:
        """This round's LEVEL off Binance's 1 s klines -- after a RESTART.

        ⛔ THE HOLE THIS CLOSES. `twap_reference` averages `self._ticks`, a
        deque appended once per poll, so a process that started after the round
        opened has no sample from the round's reference minute and the arm fell
        back to `r.strike`. MEASURED 2026-09-18 on `bnb-updown-5m-1789710600`:
        strike 752.095 against a LEVEL of 752.5447, +5.98 bps, inside a floor of
        0.25 bps -- a -$1.05 entry on a rule the arm's own gates refuse (the
        full measurement is in `_cross_level`). A REST kline does not care when
        this process started.

        The window is `[start - 60 s, start)`, one bar per second, and the
        answer is the mean of their CLOSES. That is the same statistic
        `twap_reference` computes from the tape -- a plain mean of the samples
        in the window, one sample per second either way, rather than a
        re-weighting the venue does not do -- and it is the one the venue
        settles on (`chainlink-<sym>-usd-twap-60s`).

        FAILS CLOSED, and harder than the strike's version, because here the
        average IS the quantity the arm needs rather than a way to estimate a
        point: the bar at the open (`openTime == start_ms`, which covers the
        second AFTER the open and belongs to the next comparison) is EXCLUDED,
        and the window must be COMPLETE -- 60 of 60 bars. A missing second is
        not repaired by scaling the mean up, and this number is compared
        against a 0.25 bps floor.

        Note the asymmetry with `_pm_strike_backfill_kline`, which reads the 1 s
        close at `start_ms - 1000`: a 60 s window average is a BAD STRIKE
        (1.4-2.1 bps mean error) and a GOOD LEVEL. Same source, different
        quantity -- do not unify them.

        One caveat no source removes: the venue reads Chainlink, this reads
        Binance, and that gap is the +-3-4 bps already documented under
        `config.cross_ref_twap`. This closes a 6 bps BIAS, not that noise.
        """
        slug = getattr(r, "slug", None)
        if start_ms is None:
            start_ms = int(getattr(r, "start_ms", 0) or 0)
        level = None
        try:
            level = await self._fetch_cross_level_kline(slug, start_ms)
        except Exception as exc:                                   # noqa: BLE001
            log.warning("cross level kline read failed for %s: %s: %s",
                        slug, type(exc).__name__, exc)
        if level is None:
            return None
        cache = getattr(self, "_cross_ref", None)
        if not isinstance(cache, dict):
            cache = self._cross_ref = {}
        # Written BEFORE anything the tick reads: `_cross_level` treats a cache
        # hit as the final answer, so the flag and the value cannot disagree.
        cache[slug] = (level, "kline60")
        log.info("cross %s: level recovered from the 1s klines: %.6f (the 60 s "
                 "mean at the open; the spot tape could not reach the window)",
                 slug, level)
        return level

    async def _fetch_cross_level_kline(self, slug, start_ms: int,
                                       ) -> float | None:
        """The 60 s mean at the open from 1 s bars, or None if not printable.

        Split out from `_cross_level_kline` so a test can answer it without a
        session; the window rule and the fail-closed reason are on that method.
        """
        symbol = SYMBOL_BY_PREFIX.get(market_prefix(slug or "") or "")
        if not symbol or not start_ms:
            return None
        if start_ms > int(time.time() * 1000):
            return None                     # no window exists yet, not a forecast
        look_ms = int(TWAP_REF_LOOKBACK_S * 1000.0)
        lo, hi = start_ms - look_ms, start_ms
        # +2: the 60 bars in the window, plus the bar AT the open (excluded by
        # the filter below) in case `endTime` is exclusive at the far end.
        bars = await fetch_klines(self.session, "1s",
                                  int(TWAP_REF_LOOKBACK_S) + 2,
                                  symbol=symbol, end_ms=hi)
        px: dict[int, float] = {}
        for b in bars or ():
            try:
                ot = int(b[0])
                close = float(b[4])
            except (TypeError, ValueError, IndexError):
                continue
            if lo <= ot < hi and close > 0:
                px[ot] = close      # keyed: a duplicated bar counts ONCE
        want = int(TWAP_REF_LOOKBACK_S)
        if len(px) < want:
            log.info("cross level kline for %s: %d of %d bars in the window "
                     "-- no level from this source (a 60 s mean that is missing "
                     "a minute is not a 60 s mean)", slug, len(px), want)
            return None
        return sum(px.values()) / len(px)

    def _cross_signal(self, r, ticks, tau_now_s: float):
        """(tape, name) the cross arm measures its CROSSING on.

        `spot` -- the feed's own point tape -- is what shipped, and it is the
        variable the venue averages away: the payout is
        `TWAP60(bell) >= TWAP60(open)`, so a spot crossing 20 s from the bell is
        two thirds of a done deal that has not happened yet. With `fc` the tape
        fed to `crossed_side` is the projected 60 s mean instead, read from
        `self._ticks` -- the 1 Hz poll tape whose span is fixed by the poll
        cadence, for the reason `_round_range_bps` gives about the feed deque.

        MEASURED (tools/pm_twap_trigger_check.py, PM era, tau 5-60, one entry
        per round): per-share +0.0382 on 154 entries for the spot against
        +0.0453 on 105 for the forecast -- better per share, and 32% fewer
        entries, so the TOTAL at a fixed stake is lower. It is a RISK change:
        what it removes by construction is the family where the spot crosses
        and the mean it is about to be averaged into never does. See
        config.cross_fc_trigger.

        FALLS BACK TO THE SPOT, logged once per round, when the poll tape cannot
        describe the window (a boot inside the last 60 s). That fallback is
        what shipped before this existed and it is the SAME judgement
        `_cross_level` already ships for the LEVEL, so a partial tape leaves the
        arm in one coherent legacy mode -- old level, old signal -- rather than
        half of each. It is NOT silent: the trigger carries `sig=spot`, so the
        fallback cohort is separable from `sig=fc` in `decisions` and the fire
        line.

        ⛔ A first version FAILED CLOSED here (no forecast -> no trade) and was
        backed out: 10 fixtures seed exactly this partial tape, and refusing is
        an unmeasured change in money terms whose only live effect is the
        post-restart window. If it is ever wanted, it needs its own measurement
        and its own fixtures, not a ride on this one.
        """
        # Same gate as the level: forecasting a 60 s mean is only the right
        # signal on a venue that pays on one.
        if not (self._ck("fc_trigger", r) and self._settles_on_twap()):
            return ticks, "spot"
        buf = (getattr(self, "_ticks", None) or {}).get(symbol_for_round(r)) or []
        fc = twap_forecast(buf, tau_now_s)
        if fc is None:
            seen = getattr(self, "_cross_fc_dbg", None)
            if not isinstance(seen, set):
                seen = self._cross_fc_dbg = set()
            if len(seen) > 400:
                seen.clear()
            if getattr(r, "slug", None) not in seen:
                seen.add(getattr(r, "slug", None))
                log.info("cross %s: no 60s forecast on the tape, firing on the "
                         "spot (sig=spot -- the older behaviour)",
                         getattr(r, "slug", "?"))
            return ticks, "spot"
        return fc, "fc"

    def _train_is_fresh(self, r, thr_bps: float) -> bool:
        """Did the spot cross `thr_bps` from the strike JUST NOW?

        THE BUG THIS FIXES, live 2026-09-15 20:49:54. The train condition is a
        LEVEL -- |spot - strike| >= 10bps -- and a level has no anchor. On the
        first tick after a restart it fired on all three families at once, on a
        15m round whose strike was TEN MINUTES old with the spot already
        16-25bps out (btc +18.17, eth +16.33, bnb +25.18). It bought a stale,
        fully-repriced level at asks of 0.77-0.89, not a breakout. The
        measurement it was built from anchored at the FIRST quote to cross the
        threshold; the shipped code anchored at nothing.

        True only when a sample INSIDE the last `train_fresh_s` seconds was
        still BELOW the threshold -- the crossing is then provably recent.
        Fails CLOSED when there is not enough history to prove it, which is
        exactly the restart case that misfired.
        """
        if not r.strike or thr_bps <= 0:
            return False
        win_ms = int(self._ck("train_fresh_s", r) * 1000)
        if win_ms <= 0:
            return False
        buf = (getattr(self, "_ticks", None) or {}).get(symbol_for_round(r)) or []
        now = int(time.time() * 1000)
        recent = [(t, px) for t, px in buf if t >= now - win_ms and px > 0]
        if not recent:
            return False
        oldest = min(recent, key=lambda s: s[0])[1]
        return abs(1e4 * (oldest - r.strike) / r.strike) < thr_bps

    def _train_age_ms(self, r, thr_bps: float) -> int | None:
        """Milliseconds since we last SAW the spot inside `thr_bps` of strike.

        This is the D the arm's edge turns on: the sweep measures the X>=15 cell
        at 94.9% for D=0 decaying to 83.7% by D=5s, and `fresh_s` is nothing but
        the D gate -- so report the exact quantity the gate constrains instead
        of reconstructing it from `quotes` afterwards (that reconstruction is
        provably wrong: walking the live fills under a 5.0s bound gave p90 8.3s
        and max 25.8s, which the gate cannot have admitted, because the gate
        reads THIS buffer and `quotes` is a different, coarser series).

        ANCHORED ON THE LAST IN-BAND SAMPLE, not on a run of above-threshold
        samples. `self._ticks` is appended ONCE PER POLL (~1 Hz, 2400 samples),
        so at a fire the newest sample is usually still INSIDE the threshold --
        that sample is exactly what makes `_train_is_fresh` pass -- while
        `feed.price` has already moved out. Anchoring on the current excursion
        therefore finds no run at all and returns None on EVERY fire, which is
        what it did in production: every TRAIN reason was written without D.

        So D here is `now - (time of the most recent sample with |gap| < thr)`:
        an UPPER BOUND on the excursion age, quantised to the poll cadence. That
        is the same resolution the sweep's D cells carry (its "D=0" is the first
        quote at/after the breakout, ~1s later at this cadence), so the two are
        directly comparable. Passing `_train_is_fresh` implies a sample inside
        the window was in-band, so D <= fresh_s always -- a logged D can never
        contradict its own gate.

        None only when the buffer holds no in-band sample at all, which is the
        same case `_train_is_fresh` fails closed on.
        """
        if not r.strike or thr_bps <= 0:
            return None
        buf = (getattr(self, "_ticks", None) or {}).get(symbol_for_round(r)) or []
        if not buf:
            return None
        now = int(time.time() * 1000)
        for t, px in sorted(buf, key=lambda s: s[0], reverse=True):
            if px <= 0 or t > now:
                continue
            if abs(1e4 * (px - r.strike) / r.strike) < thr_bps:
                return now - t
        return None

    def _train_round_ok(self, r) -> bool:
        """May the TRAIN trigger trade this round? (cross_train_5m_only)

        The trigger is time-boxed to "the first 4 minutes" and priced in the
        money, and BOTH are properties of a 300-SECOND round. On a 15m round
        `tau >= 60` is 14 of the 15 minutes, not the first four; and 10bps at
        tau 820 is ~1 sigma, not in the money -- the three live 15m legs filled
        at 0.66/0.73/0.74 against a stated 0.75-0.88 band, while every 5m leg
        filled in 0.79-0.88. Restricting to 5m makes the two properties the
        same property.

        An unreadable slug FAILS CLOSED: a trigger that overrides three gates
        should not act on a round whose length it cannot establish. This gates
        the TRAIN trigger only -- the cross arm still trades every family.
        """
        if not self._ck("train_5m_only", r):
            return True
        try:
            return int(str(r.slug).split("-")[2].rstrip("m")) * 60 == 300
        except (AttributeError, IndexError, ValueError):
            return False

    def _probe_fires(self, r) -> bool:
        """Quote a range-REFUSED crossing anyway, at a fixed tiny stake.

        The catch thesis says the venue walks to its post-cross price over ~1s
        and an early quote pays less -- but its decisive case (a jump-sized
        move, book running far past the quote) can never produce a natural
        fill to test that: the range gate refuses those crossings before the
        venue can answer. So this buys the answer with real orders, once per
        round, a few times a day, at the arm's NORMAL size -- no special
        stake, because what it measures must be what a gate raise would
        actually trade. Default OFF: it spends money to measure mechanics,
        not for edge. Rows are labelled "cross probe" in decisions.reason;
        everything else -- the guard, the caps, the reserved slot, the
        exposure checks -- is UNCHANGED.
        """
        if not self._ck("probe_enabled", r):
            return False
        day = time.strftime("%Y-%m-%d", time.gmtime())
        st = getattr(self, "_probe_state", None)
        if not isinstance(st, dict):
            st = self._probe_state = {"day": "", "n": 0, "slugs": set()}
        if st["day"] != day:
            st["day"], st["n"], st["slugs"] = day, 0, set()
        cap = int(self._ck("probe_max_per_day", r) or 0)
        if (cap > 0 and st["n"] >= cap) or r.slug in st["slugs"]:
            return False
        st["slugs"].add(r.slug)
        st["n"] += 1
        log.info("cross probe %s: quoting a range-refused crossing at the "
                 "arm's normal size (probe %d/%d today)", r.slug, st["n"], cap)
        return True

    def _cross_refuse(self, r, side, reason: str, *, tau: float = 0.0,
                      spot=None, stake: float = 0.0, sigma=None,
                      arm: str | None = None,
                      detail: str = "") -> None:
        """Record a cross trigger that was DETECTED and then refused.

        Two problems this fixes, both found on 2026-09-14 while reconstructing
        seven rounds that looked like missed crossings:

        * SILENCE. Every refusal after `crossed_side` returns a side used to
          exit with no decision row -- and most with no log line either. In the
          database that is indistinguishable from "no crossing happened", so
          the only way to tell a refusal from a miss was to replay the venue's
          strike against 1s klines. A detected-then-refused trigger is exactly
          the thing worth seeing, because it is rare and it is a choice.
        * SPAM. The one path that did log wrote on every evaluation. Now that
          the arm runs on the websocket clock (~114/s) that produced 164 lines
          for 11 rounds. Deduped per (round, reason), so a refusal that persists
          for a whole round is one line and one row.
        """
        # Keyed BY SLUG, one entry per live round. It used to hold a single
        # {"slug", "seen"} pair and reset whenever the slug changed, which was
        # right for one family and defeats the dedupe entirely with six: each
        # market's refusal resets the previous market's memory, so on the
        # websocket clock (~114/s) every refusal logs and inserts a row on every
        # evaluation -- the exact spam this method was written to stop.
        #
        # getattr, not attribute access: the fixtures build a Bot with __new__
        # and never run __init__, the same reason `_ticks` is looked up that way.
        seen = getattr(self, "_cross_refused", None)
        if not isinstance(seen, dict) or "seen" in seen:
            seen = self._cross_refused = {}         # drop the old single-slug shape
        live = {c["round"].slug
                for c in (getattr(self, "_mkt", None) or {}).values()
                if c.get("round") is not None} | {r.slug}
        for k in [k for k in seen if k not in live]:
            del seen[k]
        bucket = seen.setdefault(r.slug, set())
        if reason in bucket:
            return
        bucket.add(reason)
        # WHICH ARM REFUSED. This method is shared by both arms, and `train` is a
        # local in the caller that cannot be read from here. The Train arm's own
        # refusal messages name it ("train cooldown ...", "train ask ... above
        # the gate"); every other refusal reaching this path is the Cross arm's.
        # The word "cross" used to be stamped on ALL of them -- 846 log lines
        # and every row in decisions.reason -- so a refused Train trigger was
        # indistinguishable from a refused Cross one in either record. Callers
        # that can name the arm outright should pass it, not rely on the text.
        if arm is None:
            arm = "train" if "train" in (reason or "") else "cross"
        log.info("%s %s %s not taken: %s", arm, r.slug, side,
                 f"{reason} ({detail})" if detail else reason)
        # Record what the book showed at the refusal. A refusal is the only
        # clean read of "the venue had not repriced yet" that arrives without
        # an order, and the row used to carry quote=None -- which threw that
        # read away. Meter the book's path after it too (see
        # _schedule_cross_meter): the catch a faster trigger would buy is
        # exactly how fast the book leaves this price.
        quote = None
        with contextlib.suppress(Exception):
            wsb = getattr(self, "ws_book", None)
            if wsb is not None:
                top = wsb.best(r, settings.ws_book_max_age_s)
                quote = top.ask(side) if top is not None else None
        self._schedule_cross_meter(r, side, "refuse", label=arm)
        with contextlib.suppress(Exception):
            self.store.insert("decisions", {
                "slug": r.slug, "ts_ms": int(time.time() * 1000), "tau_s": tau,
                "side": side, "fair": None, "quote": quote, "edge": 0.0,
                "hurdle": 0.0,
                # the REFUSED market's sigma; `self._sigma` is the primary's,
                # which would mislabel every eth/bnb row.
                "sigma_1m": (sigma if sigma is not None
                             else getattr(self, "_sigma", 0.0)),
                "spot": spot, "oracle": None, "strike": r.strike,
                "stake": stake, "acted": 0, "reason": f"{arm} refused: {reason}",
            })

    def _schedule_cross_meter(self, r, side, kind: str = "trigger",
                              label: str = "cross") -> None:
        """Sample the book after a trigger, once per (round, side, kind).

        `kind` separates a FIRED trigger from a refused one. Without it a fire
        whose round had been refused a few hundred ms earlier was deduped away
        -- the only meter on the log belonged to the refusal, which
        mis-anchored the 09:44 timing reads by ~290ms until the venue's own
        order records exposed it (2026-09-15). A fire always gets its own.

        The trigger path records what the book showed at the decision; this
        records what it did NEXT. Log-only: it cannot place, price or change
        an order, and a failure here must never touch the arm.
        """
        if getattr(self, "ws_book", None) is None:
            return
        seen = getattr(self, "_meter_seen", None)
        if not isinstance(seen, set):
            seen = self._meter_seen = set()
        if (r.slug, side, kind, label) in seen:
            return
        if len(seen) > 400:                 # ~a day of triggers; keep it bounded
            seen.clear()
        seen.add((r.slug, side, kind, label))
        try:
            task = asyncio.create_task(self._cross_meter(r, side, kind, label))
        except RuntimeError:                # no running loop (sync/fixture use)
            return
        tasks = getattr(self, "_meter_tasks", None)
        if tasks is None:
            tasks = self._meter_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _cross_meter(self, r, side, kind: str = "trigger",
                           label: str = "cross") -> None:
        """The book after a trigger, at 50ms resolution where the race lives.

        250/500/1000ms marks cannot tell a reprice at +330 from one at +540,
        and that band is where a client-side saving would or would not flip a
        fire: measured 2026-09-15 from the venue's own records, the order is
        created ~185ms after the decision and the FOK answers ~425ms later,
        against runs that finish in <=600ms. The `fine` line samples the
        window; the legacy line is kept verbatim for tools/cross_catch_report.
        """
        wsb = getattr(self, "ws_book", None)
        if wsb is None:
            return
        coarse, fine, prev = {}, [], 0.0
        try:
            for mark in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40,
                         0.45, 0.50, 0.60, 0.75, 1.00):
                await asyncio.sleep(mark - prev)
                prev = mark
                top = wsb.best(r, settings.ws_book_max_age_s)
                q = top.ask(side) if top is not None else None
                txt = "n/a" if q is None else f"{q:.3f}"
                fine.append(f"{int(round(mark * 100)):02d}={txt}")
                if mark in (0.25, 0.50, 1.00):
                    coarse[mark] = txt
            log.info("%s meter %s %s: +0.25s=%s +0.50s=%s +1.00s=%s [%s]",
                     label, r.slug, side, coarse[0.25], coarse[0.50],
                     coarse[1.00], kind)
            log.info("%s fine %s %s %s: %s", label, r.slug, side, kind,
                     " ".join(fine))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _open_cross_stake(self) -> float:
        """Total stake in cross + train positions that have not settled yet.

        Read from the LEDGER, not tracked in memory: a restart must not reset it
        to zero, which is precisely how an exposure cap silently stops capping.
        Cached for a second because the arm asks once per family per poll.

        On a query failure this returns the LAST KNOWN value rather than 0.0 --
        unknown exposure must never read as no exposure.
        """
        now = time.time()
        if now - getattr(self, "_open_stake_at", 0.0) < 1.0:
            # + in-flight on the CACHED path too. Returning the bare cached
            # figure here defeated the whole reservation: two triggers inside
            # the same second is precisely the case it exists for.
            return getattr(self, "_open_stake", 0.0) + self._inflight_stake()
        try:
            rows = self.store.query(
                "SELECT COALESCE(SUM(f.stake), 0.0) AS s FROM fills f "
                "LEFT JOIN settlements st ON st.fill_id = f.id "
                # BOTH arms, because both hold the same reserved slot. Train
                # was added to this list the same commit that started labelling
                # its fills 'train' -- a label change without this line would
                # have made the cap blind to every train position, which is the
                # "silently stops capping" failure this docstring warns about.
                "WHERE st.id IS NULL AND f.mode IN ('cross', 'train')")
            val = float(rows[0]["s"] or 0.0) if rows else 0.0
        except Exception:                                   # pragma: no cover
            return getattr(self, "_open_stake", 0.0) + self._inflight_stake()
        self._open_stake, self._open_stake_at = val, now
        return val + self._inflight_stake()

    def _inflight_stake(self) -> float:
        """Stake committed to orders that have not booked a fill yet.

        Orders are placed off the poll loop, so without this two triggers could
        each pass a 30% exposure check while neither had reached the ledger,
        and land at 60% together. NOT cached -- the cached ledger figure is a
        second old by construction and this is the part that changes between
        two triggers in the same poll.
        """
        return float(getattr(self, "_pending_stake", 0.0) or 0.0)

    async def _prefetch_cross_quote(self, r, up, down, feed, tau, ctx):
        """Keep a quote WARM for the side the next crossing would buy.

        The fire path pays a quote round-trip (measured 40-65ms) between the
        decision and the order, inside a race the venue settles in ~250ms:
        measured 2026-09-15 against the venue's own order records, decision ->
        venue-accepted is 169-186ms, and the book is already past the ceiling
        at +250ms (eth 0.46 -> 0.63, btc 0.30 -> 0.66). Two of that morning's
        four failures missed by less than this round-trip (07:59 btc by
        ~20ms, 09:09 eth by ~45ms).

        The side is knowable BEFORE the crossing -- below the strike the next
        cross is Up, above it Down -- so the quote is fetched here, while the
        family sits in its tradeable window, and consumed ONCE by the first
        attempt (`_take_prefetched_quote`, used in _place_order). Any doubt
        (stale, stake mismatch, fetch failure) falls back to the synchronous
        fetch: this can only ever be faster, never a different trade.

        The stake mirrors the fire's sizing so the quote's amountIn matches
        the order's; if the two ever drift apart the mismatch is caught at
        use time rather than trading a size the arm did not choose.
        """
        broker = getattr(self, "broker", None)
        if broker is None or getattr(broker, "mode", None) != "live":
            return None
        get_quote = getattr(broker, "get_quote", None)
        spot, strike = getattr(feed, "price", None), getattr(r, "strike", None)
        if get_quote is None or not spot or not strike or spot == strike:
            return None
        side = "Up" if spot < strike else "Down"
        token = up.token_id if side == "Up" else down.token_id
        if not token:
            return None
        wsb = getattr(self, "ws_book", None)
        top = (wsb.best(r, settings.ws_book_max_age_s)
               if wsb is not None else None)
        ask = top.ask(side) if top is not None else None
        if ask is None or ask <= 0:
            return None
        stake = self._ck("stake_usdt", r)
        if stake is None:
            sigma = (ctx.get("sigma") if ctx else None) \
                or getattr(self, "_sigma", 0.0) or 0.0
            mz = (abs(math.log(spot / strike))
                  / (sigma * math.sqrt(tau / 60.0))
                  if tau > 0 and sigma > 0 else 0.0)
            stake = self._stake_for(
                Signal(side, ask, ask, 0.0, 0.0, 0.0, True, "cross"), mz)
            stake = round(stake * self._ck("stake_mult", r), 2)
            if settings.stake_cap_usdt > 0:
                stake = min(stake, settings.stake_cap_usdt)
        stake = float(stake)
        if stake < settings.min_order_usdt:
            return None
        cache = getattr(self, "_quote_pre", None)
        if not isinstance(cache, dict):
            cache = self._quote_pre = {}
        busy = getattr(self, "_quote_pre_busy", None)
        if not isinstance(busy, set):
            busy = self._quote_pre_busy = set()
        now = time.monotonic()
        for old, entry in list(cache.items()):
            if now - entry[0] > 60.0:
                cache.pop(old, None)
        entry = cache.get(token)
        if entry is not None and now - entry[0] < _QUOTE_PREFETCH_REFRESH_S:
            return None
        if token in busy:
            return None
        busy.add(token)

        async def _warm() -> None:
            try:
                q = await get_quote(token, stake)
                if q:
                    cache[token] = (time.monotonic(), stake, q)
            except Exception as e:                      # noqa: BLE001
                log.debug("quote warm-up failed for %s: %s", r.slug, e)
            finally:
                busy.discard(token)

        task = asyncio.create_task(_warm())
        tasks = getattr(self, "_quote_pre_tasks", None)
        if not isinstance(tasks, set):
            tasks = self._quote_pre_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def _prefetch_venue_top(self, r, up, down, feed, margin_bps=None):
        """Keep the VENUE's own ask WARM for the side the next crossing buys.

        ⛔ THE LARGEST REMAINING COST ON THE ORDER PATH, and it is the only one
        left that is not the order itself. Measured 2026-09-19 over the live
        log, decision -> verdict is 1001ms median (p90 1999ms), and it
        decomposes as:

            get_quote        LOCAL `book.best()` read          ~0ms
            venue_top        REST                            ~292ms
            FAK ack          REST, and this IS the order      452ms median
            shadow book      spawned, off the path              --

        `venue_top` has to finish before the FAK can be priced, so the two
        cannot overlap -- but nothing says the fetch has to START at the
        decision. The side is knowable BEFORE the crossing (below the strike
        the next cross is Up, above it Down), which is the same fact
        `_prefetch_cross_quote` already exploits, from the same call site.

        ⭐ WHY A STALE BASE IS SAFE HERE, and it is worth being exact because
        the opposite would be a money bug. The probe does NOT set the price we
        pay. A FAK fills at the BOOK, bounded by the limit -- verified on the
        live fills, which land BELOW both the ceiling and our own estimate:

            pm FAK FILL 2.08333 Down @ 0.48 ... ceiling 0.59 ask 0.53
            pm FAK FILL 1.78571 Up   @ 0.56 ... ceiling 0.65 ask 0.62

        So the base feeds `_take_cap_for`, which sets the CEILING, and
        `cross_max_fill_px` hard-bounds that ceiling regardless. A base that is
        slightly stale moves the ceiling slightly; it cannot move the fill
        price, and it cannot lift the cap.

        The residual risk is the other direction: a base that is too LOW builds
        a ceiling under the ask that exists, and the FAK finds nothing. That is
        exactly today's failure mode, which is why the freshness bound is a
        knob and not a constant, and why any doubt -- stale, missing, fetch
        failed -- falls through to the synchronous fetch. This can only ever be
        faster, never a different price.
        """
        max_age = float(settings.cross_venue_prefetch_max_age_s)
        if max_age <= 0:                       # ships OFF
            return None
        # ⛔ IMMINENCE. The first version of this had no such gate and fired on
        # EVERY no-crossing tick, which is what made it 19 probes a second. The
        # tau window and the `_cross_fired` slot are already spent by the time
        # we get here, so the only thing left to ask is whether a crossing is
        # actually near -- and `|margin|` is exactly that question.
        # MEASURED over 1,060,056 replay ticks, share of ticks kept against
        # share of the ticks that IMMEDIATELY PRECEDE a crossing:
        #     <=1 bps   12.75% of ticks   63.3% of pre-crossings
        #     <=2 bps   24.06% of ticks   84.9% of pre-crossings
        #     <=5 bps   50.39% of ticks   96.9% of pre-crossings
        # 2 bps keeps 85% of the value for a quarter of the traffic.
        band = float(settings.cross_venue_prefetch_margin_bps)
        if band > 0:
            if margin_bps is None:
                return None            # cannot tell: do not spend a round trip
            if abs(float(margin_bps)) > band:
                return None
        broker = getattr(self, "broker", None)
        if broker is None or getattr(broker, "mode", None) != "live":
            return None
        if not settings.cross_venue_take:      # nothing consumes it otherwise
            return None
        spot, strike = getattr(feed, "price", None), getattr(r, "strike", None)
        if not spot or not strike or spot == strike:
            return None
        side = "Up" if spot < strike else "Down"
        token = up.token_id if side == "Up" else down.token_id
        if not token:
            return None
        cache = getattr(self, "_vtop_pre", None)
        if not isinstance(cache, dict):
            cache = self._vtop_pre = {}
        # ⛔ ITS OWN KNOB, NOT `max_age * 0.5`. Tying the refresh to the
        # freshness bound meant a 0.35s bound refetched every 0.175s -- twice a
        # second per family, for a REST call, forever. The two are unrelated
        # questions: how old may a warm ask BE, and how often may we ASK.
        refresh = float(settings.cross_venue_prefetch_refresh_s)
        e = cache.get(token)
        if e is not None and time.monotonic() - e[0] < refresh:
            return None

        async def _go():
            try:
                vt = await self._venue_top(token, r)
            except Exception:                            # noqa: BLE001
                return
            if vt is not None and getattr(vt, "ask", 0) > 0:
                cache[token] = (time.monotonic(), float(vt.ask))
                # Unbounded growth is the failure mode a dict cache invites;
                # the arm trades 9 families x 2 sides, so anything past that is
                # dead rounds.
                if len(cache) > 64:
                    # TRIM TO A TARGET, not by a fixed count: dropping 32 from
                    # 201 leaves 169, which is still a leak wearing a bound.
                    # Oldest first -- they are the ones a `max_age` check would
                    # refuse anyway.
                    for k in sorted(cache, key=lambda x: cache[x][0])[:-32]:
                        cache.pop(k, None)

        # ⭐ COUNTED, because the whole reason the first version shipped and
        # came straight back off is that I ESTIMATED this rate instead of
        # measuring it. The log now says what it actually is.
        n = self._vtop_pre_n = getattr(self, "_vtop_pre_n", 0) + 1
        t0 = getattr(self, "_vtop_pre_t0", None)
        if t0 is None:
            t0 = self._vtop_pre_t0 = time.monotonic()
        if n % 200 == 0:
            el = max(1e-6, time.monotonic() - t0)
            log.info("venue prefetch: %d probes in %.0fs = %.2f/s (%.0f/hr) | "
                     "consumed %d, expired-or-unused %d",
                     n, el, n / el, 3600.0 * n / el,
                     getattr(self, "_vtop_hit_n", 0),
                     n - getattr(self, "_vtop_hit_n", 0))
        task = asyncio.create_task(_go())
        tasks = getattr(self, "_vtop_pre_tasks", None)
        if not isinstance(tasks, set):
            tasks = self._vtop_pre_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    def _take_prefetched_venue_top(self, token: str) -> float | None:
        """Pop the warm venue ask when it is fresh enough to price a ceiling.

        ONE SHOT, like the quote: the attempt that consumes it has spent it, so
        a retry can never build its ceiling from a base the first attempt
        already used and that is now a full round older.
        """
        max_age = float(settings.cross_venue_prefetch_max_age_s)
        if max_age <= 0:
            return None
        cache = getattr(self, "_vtop_pre", None)
        if not isinstance(cache, dict):
            return None
        entry = cache.pop(token, None)
        if entry is None:
            return None
        ts, ask = entry
        age = time.monotonic() - ts
        if age > max_age:
            return None
        self._vtop_warm_age_s = age
        self._vtop_hit_n = getattr(self, "_vtop_hit_n", 0) + 1
        return float(ask)

    def _take_prefetched_quote(self, token: str, stake: float) -> dict | None:
        """Pop the warm quote for this token when it is fresh and exact.

        One shot: the attempt that consumes it has spent it, so a later
        attempt can never trade on a price the first one already used.
        """
        cache = getattr(self, "_quote_pre", None)
        if not isinstance(cache, dict):
            return None
        entry = cache.pop(token, None)
        if entry is None:
            return None
        ts, q_stake, q = entry
        age = time.monotonic() - ts
        if age > _QUOTE_PREFETCH_MAX_AGE_S:
            return None
        if round(float(q_stake), 2) != round(float(stake), 2):
            return None
        # The caller's marker logs this: the venue anchors the FOK bound to
        # THIS quote's averagePrice (verified 2026-09-15 -- the order record's
        # `price` equals the submitted quote's avg to 15 digits), so the age
        # is part of the bound's meaning.
        self._warm_age_s = age
        return q

    async def _cross_all(self) -> None:
        """Run the cross arm over every family in CROSS_TRADED.

        One family raising must not stop the others -- a six-market arm whose
        whole loop dies because eth-updown-15m returned a malformed round is
        strictly worse than the single-market one it replaced.

        With no per-market context (fixtures, or a poll that recorded nothing)
        this falls back to the single-market path so the arm behaves exactly as
        it did before it was widened.
        """
        mkt = getattr(self, "_mkt", None)
        if not mkt:
            await self._cross_tick()
            return
        for prefix, ctx in list(mkt.items()):
            try:
                await self._cross_tick(ctx)
            except Exception as e:                      # pragma: no cover
                log.exception("cross tick failed for %s: %s: %s",
                              prefix, type(e).__name__, e)

    async def _cross_tick(self, ctx=None) -> None:
        """Buy the side the spot JUST crossed into, while the venue undershoots.

        See config.cross_* for the measurement and for why it is thin. It is
        live to buy a sample, not because it is proven: as of 2026-09-13 it has
        no settled live fill of its own, and every number below it comes from
        replay. Sizing follows the main arm (see cross_stake_usdt), so it is no
        longer the flat $1 the earlier note described.

        Shaped like _impulse_tick in the parts that matter -- one claim per
        round, claim before ordering, and NOTHING (no venue call at all) when
        it does not fire -- but deliberately NOT in its gating:

          * the guard call is check_reserved, not check. This arm has a slot of
            its own so the main arm cannot crowd it out.
          * the venue test blocks only on exposure the LEDGER cannot explain
            (see _unbooked_round_tokens), not on any held token. Blocking on
            any token refused 52% of its triggers, all of them to the main arm.
        """
        # getattr: tick() now calls this directly (via _cross_all) rather than
        # only from _record, and fixtures build Bot via __new__ with no `mode`.
        if getattr(self, "mode", None) not in ("live", "paper"):
            return
        # `ctx` is one family's state, built by _record and dispatched by
        # _cross_all. None means the single-market path (the primary), which is
        # kept so the arm stays directly callable and testable.
        if ctx is None:
            r = getattr(self, "_round", None)
            # NOT self.spot: the poll used to rewrite it once per market as it
            # walked MARKETS, so on the websocket clock it held the LAST
            # symbol's feed.
            feed = getattr(self, "_primary_spot", None)
            if feed is None:
                feed = self.spots.get(MARKETS[0][1])
            primary = True
        else:
            r, feed = ctx["round"], ctx["feed"]
            primary = ctx.get("primary", False)
        asks = (getattr(self, "_asks", (None, None)) if ctx is None
                else ctx.get("asks", (None, None)))
        if not self._ck("enabled", r):
            return
        ticks = getattr(feed, "ticks", None)
        if r is None or not ticks:
            return
        if r.strike is None:
            # ⛔ A ROUND WITH NO STRIKE IS UNFIREABLE, AND IT USED TO BE SILENT.
            # Every gate below reads `r.strike`, so this is not "no crossing"
            # -- it is "this family cannot be evaluated at all" -- and it was
            # the only thing in this file that could disable a whole round with
            # no line to grep. MEASURED 2026-09-17: `sol-updown-5m-1789642800`
            # (open 11:00:00, across the 10:59:43 -> 11:00:01 restart) sat with
            # `strike IS NULL` for its entire life, and the log showed nothing
            # for that round at all -- indistinguishable from a quiet market.
            #
            # Once per slug, not per tick: this runs on the poll loop.
            seen = getattr(self, "_no_strike_seen", None)
            if not isinstance(seen, set):
                seen = self._no_strike_seen = set()
            if r.slug not in seen:
                if len(seen) > 400:
                    seen.clear()
                seen.add(r.slug)
                log.warning("cross %s cannot be evaluated for the whole "
                            "round: no strike (see _pm_strike_backfill_kline "
                            "-- the round opened before this process started)",
                            r.slug)
            return
        # THE LEVEL THIS ROUND'S CROSSING IS MEASURED AGAINST, resolved ONCE
        # here and used by every depth gate below. It is the 60 s mean at the
        # open -- what the venue pays on -- and NOT `r.strike`, which is the
        # spot at the open instant. See config.cross_ref_twap and
        # `_cross_level`. `train` deliberately keeps `r.strike`: its threshold
        # is >= 15 bps, four times the +-4 bps this level moves by, so the
        # change would be noise there while the range gate's "how far has this
        # round travelled" is about the round's own prices, not the level.
        level, level_src = self._cross_level(r)
        if level is None:
            return
        # EXIT-ON-FLIP RUNS HERE, BEFORE THE PER-ROUND CAP. While we hold a
        # position `_cross_fired[slug]` already equals max_per_round, so the cap
        # returns on the next line -- a watcher placed after it would never see
        # the tick that has to decide the exit. It is a no-op unless the flag is
        # on AND this round's fill was registered (see `_book_fill`).
        if await self._cross_exit_check(r, level, ticks):
            return
        # Entries already taken in THIS round. `max_per_round` is the cap.
        #
        # The note that used to sit here claimed consecutive crosses "alternate
        # sides, so a round oscillating around the strike is naturally hedged
        # rather than doubled, and taking every one measured +$31.37 against
        # +$14.16 for the first alone". BOTH halves of that are wrong, and it
        # was quoted as a reason to raise this cap:
        #
        #  * Binary contracts do not hedge. Measured on the real fills, a round
        #    that took Up then Down paid 0.6106 + 0.5885 = 1.1991 per share for
        #    a $1.00 payout -- a guaranteed -0.1991/share before fees. That is a
        #    synthetic NEGATIVE BOX SPREAD, not a hedge. Realized on
        #    btc-updown-5m-1789387500: -$0.87.
        #  * Same-side repeats are concentration, not diversification. The one
        #    multi-entry round that won took FOUR fills on one side in 2s
        #    (eth-updown-5m-1789413600, +$6.73) -- a 4x bet on a single oracle
        #    print, equally capable of being -$11.64. Across the five multi-entry
        #    rounds the realized total is -$12.89 on $35.60 = -36%.
        #
        # The +$31.37 figure cannot be reconciled with the fills; it comes from a
        # replay that priced neither the box nor the concentration.
        #
        # The cap also costs almost nothing, which is the measurement that
        # settles it. `cross_max_tau_s` confines this arm to a slice of the
        # round, and over 2,119 settled btc-5m rounds (1s klines, true strikes):
        #
        #   first crossing in tau 5-60   n=354   win 69.8%   med z 0.113
        #   first crossing in tau 60-300 n=2119  win 51.8%   med z 0.000
        #
        #   83% of rounds hold ZERO crossings in the 5-60s window and only 6%
        #   hold more than one (median 0). So max_per_round=1 is not spending a
        #   ticket on noise -- it spends it in the window whose first crossing
        #   wins 69.8%, and there is usually only one to spend it on.
        #
        # THE 51.8% ROW IS NOT THE LATE COHORT THIS ARM NOW TRADES. Since
        # 2026-09-18 `cross_max_tau_s` is 180, so the one slot can be spent in
        # tau 60-180 -- and that row is UNGATED: every crossing, with no margin
        # floor and no price band, wins 51.8% there. The entries the arm would
        # actually take in tau 60-180 (floor 0.25 bps, ask <= 0.65, one per
        # round) measure +0.0719 per share over 551 on the Polymarket tape
        # (`tools/pm_tau_window.py`; table in config.cross_max_tau_s). The gate
        # chain separates those two populations, not the tau -- so do not quote
        # this row as a veto on the wide window. What DOES rise with the wider
        # window is the cost of this cap: crossings in tau 60-180 that arrive
        # after the slot is spent are dropped, and `_cross_refuse` records each
        # one as a decision row so that population can be priced directly.
        #
        # Do not raise this cap on the strength of the old note. Re-measure if it
        # is ever revisited: `_cross_refuse` now records each cap-blocked
        # crossing as a decision row (spot/strike/tau/sigma_1m) so the blocked
        # population can be priced directly rather than replayed.
        if not isinstance(self._cross_fired, dict):
            self._cross_fired = {}
        # getattr, not a bare attribute: fixtures build Bot via __new__, which
        # skips __init__ (the same trap `_cross_refused` hit on 2026-09-14).
        if not isinstance(getattr(self, "_cross_side", None), dict):
            self._cross_side = {}
        if not isinstance(getattr(self, "_cross_ref", None), dict):
            self._cross_ref = {}
        if self._cross_fired.get(r.slug, 0) >= self._ck("max_per_round", r):
            # SILENT BY DEFAULT, and that is a BIAS, not merely a gap. A
            # slot-blocked tick used to return before anything was logged OR
            # written to `decisions`, so the cap's cost was unmeasurable from
            # the record -- and the entries it does admit are the FIRST crossing
            # of every round, by construction.
            #
            # Measured over 2,119 settled btc-5m rounds (1s klines, true
            # strikes): the first crossing of a round has median z = 0.000 and
            # sits below z 0.15 in 91.5% of rounds -- median tau 299s, i.e. the
            # one where spot has barely nicked the strike -- and taking it wins
            # 51.8%. A round holds a median of 5 crossings and 90.2% of rounds
            # hold more than one, so with max_per_round = 1 the arm spends its
            # only slot on a coin flip in 9 rounds out of 10.
            #
            # Recorded as a DECISION ROW via `_cross_refuse`, not just a log
            # line, so the existing settlement joins can price the
            # counterfactual: which crossings were dropped, at what z, and
            # whether replacing the first entry with a later one would have paid.
            # `spot`/`strike`/`tau_s`/`sigma_1m` all land in the row. That
            # helper already dedupes per (round, reason), so a round that keeps
            # re-crossing produces ONE row rather than the hundreds a per-tick
            # write would -- which is why this does not keep its own set.
            #
            # Only recorded when `crossed_side` still resolves a side: a tick
            # with no crossing would have returned at the trigger anyway, and
            # recording it would invent rows for rounds that never crossed.
            blocked = crossed_side(list(ticks), level, self._ck("look_s", r),
                                   hold_s=self._ck("hold_s", r))
            # ⛔ NOT A SECOND CROSSING: THE TICK THAT ARRIVES WHILE THE ENTRY IT
            # MATCHES IS GOING OUT. `_cross_fired` is stamped WITH the claim,
            # before the order, so the very next tick evaluating the SAME
            # crossing finds the slot spent and used to log "crossing DROPPED,
            # priced for the counterfactual only" and write a decision row for a
            # crossing that WAS entered. MEASURED 2026-09-18 against each round's
            # own `cross into` row: of the 40 rows this branch had written, 35
            # sat within 1.43 s of the entry, 1 at 5.5 s, and only 3 were
            # genuinely later crossings (10.4 / 23.8 / 41.6 s), plus 1 round that
            # never entered at all -- so 87.5% of the population the
            # counterfactual is priced from described entries that were TAKEN,
            # and any "should the cap be raised" reading of it is biased the
            # wrong way. That is the entire reason the row exists.
            #
            # The live shape: `btc-updown-5m-1789710900`, where this line
            # printed 6 ms after the fire and the order lines that followed
            # belonged to that same fire.
            #
            # The duplicate is not made SILENT: the `cross into` row for the
            # entry it is reporting IS the record of it, and it is written once.
            lm = getattr(self, "_cross_last_ms", None)
            claimed = lm.get(r.slug) if isinstance(lm, dict) else None
            duplicate = (claimed is not None
                         and int(time.time() * 1000) - claimed
                         <= CROSS_DUP_TICK_MS)
            if blocked is not None and not duplicate:
                self._cross_refuse(
                    r, blocked,
                    f"slot already used this round "
                    f"({self._cross_fired.get(r.slug, 0)}"
                    f"/{self._ck('max_per_round', r):.0f}) -- crossing DROPPED, "
                    f"priced for the counterfactual only",
                    tau=r.tau_s(int(time.time() * 1000)),
                    spot=getattr(feed, "price", None))
            return
        # Keep every LIVE round, not just this one. Pruning to `r.slug` was
        # correct with a single family and is a bug with six: each family's call
        # would wipe the others' per-round state, so every market would forget
        # it had already fired and the per-round cap would never bind.
        live = {c["round"].slug for c in (getattr(self, "_mkt", None) or {}).values()
                if c.get("round") is not None} | {r.slug}
        # `getattr`, not attribute access. These two live only in `__init__`,
        # and this method is reached from the trigger loop -- which is entered
        # by a Bot that has already been constructed -- but NOT only that way:
        # the impulse trigger can run against a partially-built Bot and the
        # result is `AttributeError: 'Bot' object has no attribute
        # '_cross_fired'`, raised inside the trigger loop's `except Exception`
        # and therefore logged as a generic "impulse trigger failed" once per
        # tick while the cross arm silently does nothing. The rest of this file
        # already guards these two attributes by hand (`isinstance(..., dict)`
        # above, `getattr` below); this is the one unguarded read.
        self._cross_fired = {k: v for k, v in
                             getattr(self, "_cross_fired", {}).items()
                             if k in live}
        self._cross_side = {k: v for k, v in
                            getattr(self, "_cross_side", {}).items()
                            if k in live}
        self._cross_ref = {k: v for k, v in
                           getattr(self, "_cross_ref", {}).items()
                           if k in live}
        self._cross_kline = {k: v for k, v in
                             getattr(self, "_cross_kline", {}).items()
                             if k in live}
        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        # ---- TRAIN TRIGGER (config.cross_train_*) --------------------------
        # Fires on DISTANCE from the strike instead of a crossing, so it lives
        # OUTSIDE this arm's tau window (which is 5-60s) rather than inside it.
        # It is an explicit override of three gates -- max_depth_bps,
        # max_round_range_bps and cross_max_fill_px -- because a >=10bps entry
        # is deep, implies a >=10bps round travel, and prices near 0.80. Open
        # by operator decision 2026-09-15; see config for the measurement.
        # Everything that bounds RISK is untouched and shared with the cross
        # arm: the guard, the reserved slot, the exposure caps, the daily cap,
        # the reentry gap, the cooldown and the reversal rule.
        train = False
        side = None
        # The train arm's D, measured at the trigger and carried to the fire
        # log AND the decisions row. See _train_age_ms.
        train_age_ms = None
        tmax = self._ck("train_max_tau_s", r)
        if self._ck("train_enabled", r) and self._train_round_ok(r) \
                and r.strike \
                and tau >= self._ck("train_min_tau_s", r) \
                and (tmax <= 0 or tau <= tmax) \
                and feed.is_fresh(settings.max_spot_age_s):
            dist_bps = 1e4 * (feed.price - r.strike) / r.strike
            thr = self._ck("train_bps", r)
            if abs(dist_bps) >= thr and self._train_is_fresh(r, thr):
                # Measured here, BEFORE the burst guard, so the number is the
                # one the freshness gate actually decided on.
                train_age_ms = self._train_age_ms(r, thr)
                # BURST GUARD, 2026-09-15 21:01. See cross_train_cooldown_s.
                #
                # The trigger is a LEVEL (`|spot-strike| >= thr`), so the
                # moment btc/eth/bnb move together it goes true for EVERY
                # family -- and because a 5m round and its 15m sibling share a
                # strike, those two are the SAME bet. Measured: eight entries
                # in 241s, all Down, as eth-5m+eth-15m (one tick), then
                # bnb-5m+bnb-15m (+20s), then btc-5m+btc-15m (+34s) -- one view
                # expressed six ways. A CROSSING cannot cluster like this; a
                # distance condition always will. The exposure caps do not
                # catch it: they count positions and stake, not correlation.
                cd = self._ck("train_cooldown_s", r)
                last_ms = getattr(self, "_train_last_ms", 0)
                if cd > 0 and last_ms and (now_ms - last_ms) < cd * 1000.0:
                    # NO `return` here. This guard suppresses the TRAIN entry,
                    # not the family's whole tick: returning would also swallow
                    # a genuine CROSS in the same family inside the 5-60s
                    # window, i.e. a second signal killed by the first one's
                    # burst guard. Falling through lets the ordinary window gate
                    # and the crossing path decide.
                    #
                    # SELF-DIAGNOSING. A refusal was logged at 2026-09-15
                    # 21:13:16 that sat 124.8s after the only visible entry --
                    # PAST this window -- and it could not be reconstructed from
                    # the rows. Two processes and `_ck` returning 0 were both
                    # ruled out (one stats line per minute; cross_knob falls
                    # through correctly), so log the age itself, once per round,
                    # and let the next occurrence name its own number.
                    dbg = getattr(self, "_train_cd_dbg", None)
                    if not isinstance(dbg, set):
                        dbg = self._train_cd_dbg = set()
                    if len(dbg) > 400:
                        dbg.clear()
                    if r.slug not in dbg:
                        dbg.add(r.slug)
                        log.info("train cooldown HIT %s: last entry %.1fs ago "
                                 "(cooldown %.0fs), tau %.0fs", r.slug,
                                 (now_ms - last_ms) / 1000.0, cd, tau)
                    self._cross_refuse(
                        r, "Up" if dist_bps > 0 else "Down",
                        f"train cooldown {cd:.0f}s: one entry per burst",
                        tau=tau, spot=feed.price, arm="train")
                else:
                    # Stamped on the SIGNAL, before any later gate, so a refused
                    # order cannot let the same move be re-attempted family by
                    # family -- the next one is what would rebuild the burst.
                    self._train_last_ms = now_ms
                    side = "Up" if dist_bps > 0 else "Down"
                    train = True
        if not train and not (self._ck("min_tau_s", r) <= tau
                              <= self._ck("max_tau_s", r)):
            return
        # RE-ENTRY GAP. Without it the websocket clock (~114/s) takes the same
        # crossing dozens of times; the substrate this arm's multi-entry EV was
        # measured on was sampled at 1s and could never contain such a pair.
        # See config.cross_reentry_gap_s. Checked before any venue call so a
        # duplicate costs nothing.
        gap = self._ck("reentry_gap_s", r)
        if gap > 0:
            last = (getattr(self, "_cross_last_ms", None) or {}).get(r.slug)
            if last is not None and (now_ms - last) < gap * 1000.0:
                return
        up, down = r.up, r.down
        if not up or not down:
            return
        if not feed.is_fresh(settings.max_spot_age_s):
            return
        if time.time() < self._order_cooldown_until:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
        if day != self._cross_day:
            self._cross_day, self._cross_count = day, 0
        # 0 = OFF, the repo's convention for every other limit (daily_loss_cap,
        # max_consecutive_losses, stake_cap_usdt). Written as `cap > 0 and ...`
        # rather than a bare `>=`: at cap 0 a bare compare reads 0 >= 0 as
        # "budget exhausted" and silently disables the arm outright, which is
        # the exact opposite of what setting it to 0 is meant to express.
        cap_per_day = self._ck("max_per_day", r)
        if cap_per_day > 0 and self._cross_count >= cap_per_day:
            return
        # THE VARIABLE THE CROSSING IS READ ON. `spot` is the point price;
        # `fc` is the venue's own statistic, the projected 60 s mean it pays on.
        # `sig_name`/`sig_px` ride into the fire line and the trigger's reason,
        # so the two cohorts are separable in the DB rather than assumed -- the
        # same idiom `ref=twap60` uses for the level.
        sig_name, sig_px = "spot", getattr(feed, "price", None)
        if not train:
            sig_ticks, sig_name = self._cross_signal(r, ticks, tau)
            sig_px = sig_ticks[-1][1] if sig_ticks else sig_px
            side = crossed_side(list(sig_ticks), level, self._ck("look_s", r),
                                hold_s=self._ck("hold_s", r))
            # ⭐ THE TRIGGER'S OWN MARGIN, not the spot's. `crossed_side` fires on
            # the FIRST tick past the level, so every entry is BY CONSTRUCTION a
            # zero-margin entry -- and that is the weak cohort. Measured on the
            # live record (tools/pm_twap_trigger_check.py, fc entries, tau 5-60,
            # n=149, one per round): |fc-level| 0-1 bps wins 56.6% for +0.0054/sh
            # with halves +0.0766/-0.0646, while 1-2 bps wins 71.4% for +0.1715
            # (halves +0.2247/+0.1231) and 2-3 bps 77.8% for +0.1886. 76% of all
            # entries sit in the 0-1 bucket, and a floor there raises the TOTAL
            # as well as the per-share. The live fires of 09-17/18 are the same
            # shape: three of four sat at 0.00 bps and lost, the winner at 2.0.
            # The DEPTH floor below still reads the spot, so this is the only
            # gate that measures the variable the trigger actually uses.
            # See config.cross_min_margin_bps.
            floor = self._ck("min_margin_bps", r)
            if side is not None and floor > 0 and level:
                margin = 1e4 * (sig_px - level) / level
                if abs(margin) < floor:
                    self._cross_refuse(
                        r, side,
                        f"only {margin:+.2f} bps of its own margin past "
                        f"{level_src} at tau {tau:.0f}s, floor is {floor:.2f} "
                        f"(sig={sig_name})",
                        tau=tau, spot=getattr(feed, "price", None))
                    return
            # ⭐ AND THE CEILING. Past a point a bigger margin is a WARNING: the
            # price band already caps us at 0.65, so a large margin with the
            # price still inside it means our forecast and the venue's price
            # DISAGREE -- and the venue is usually right. Both samples put the
            # break at 1.5 bps ([1.50,2.00) wins 51.4% on the tape and 50.0%
            # live, negative in both). See config.cross_max_margin_bps.
            cap = self._ck("max_margin_bps", r)
            if side is not None and cap > 0 and level:
                margin = 1e4 * (sig_px - level) / level
                if abs(margin) > cap:
                    self._cross_refuse(
                        r, side,
                        f"{margin:+.2f} bps of margin past {level_src} is over "
                        f"the {cap:.2f} ceiling at tau {tau:.0f}s "
                        f"(sig={sig_name})",
                        tau=tau, spot=getattr(feed, "price", None))
                    return
        if side is None:
            # No crossing this tick: keep a quote WARM for the side the next
            # crossing would buy (see _prefetch_cross_quote). This is the
            # only point that is in the tradeable window, live, fresh and not
            # already spent -- a fire from here is plausible.
            await self._prefetch_cross_quote(r, up, down, feed, tau, ctx)
            # AND THE VENUE'S OWN ASK, for the same reason and from the same
            # fact: the side is knowable before the crossing. This is the
            # ~292ms that `venue_top` costs inside the order path.
            await self._prefetch_venue_top(
                r, up, down, feed,
                margin_bps=(1e4 * (sig_px - level) / level)
                if (level and sig_px) else None)
            return
        # WHIPSAW. Stamped on the CROSSING, before any gate, because that is the
        # hole `cross_allow_reversal` leaves: it arms on the ENTRY, so a crossing
        # the arm DECLINED leaves it unarmed and the opposite leg of the same
        # oscillation walks straight in.
        #
        # LIVE 2026-09-15 00:59 on bnb-updown-15m -- Binance 1s klines confirm
        # both legs are real trades, not a bad tick:
        #
        #   00:59:21  Up   DECLINED  only 2 tick(s) through the strike, floor 5
        #   00:59:24  Down TAKEN     9 ticks through the strike, passes
        #   00:59:38  spot back above the strike; the round settles Up
        #
        # bnb-updown-5m saw the identical move 70ms earlier, took the Up (its
        # strike sat 7c lower, so the same move cleared the floor) and was then
        # refused the Down BY the reversal guard. Depth is measured from each
        # round's own strike, so inside a chop it is noise that decides which
        # family is allowed to trade.
        #
        # Measured over 211 crossings that pass every gate (btc corpus, eth and
        # bnb from the live DB), edge = win - ask:
        #
        #     FLIPPED within 20s    n= 14  win 0.643  ask 0.650  edge -0.0071
        #     clean                 n=197  win 0.863  ask 0.686  edge +0.1771
        #
        # Consistent in sign on both families with data (btc -0.035 vs +0.197,
        # eth -0.047 vs +0.076). A flipped crossing has no edge at all, never
        # mind enough to clear the limit buffer. It is 6.6% of crossings.
        fb = self._ck("flip_block_s", r)
        if not train and fb > 0:
            fl = getattr(self, "_cross_flip", None)
            if not isinstance(fl, dict):
                fl = self._cross_flip = {}
            prev = fl.get(r.slug)
            seen_ms = int(time.time() * 1000)
            if prev and prev[0] != side and (seen_ms - prev[1]) <= fb * 1000.0:
                fl[r.slug] = (side, seen_ms)
                self._cross_refuse(
                    r, side,
                    f"crossing flipped from {prev[0]} "
                    f"{(seen_ms - prev[1]) / 1000.0:.1f}s ago, window is {fb:.0f}s",
                    tau=r.tau_s(now_ms), spot=None, sigma=None)
                return
            fl[r.slug] = (side, seen_ms)
            self._cross_flip = {k: v for k, v in fl.items() if k in live}
        # A crossing BACK through the strike is not a hedge on what we already
        # hold. With the first position sunk the second leg is worth taking iff
        # it is +EV on its own, and measured it is not: it wins 0.574 against
        # 0.714 for a first entry (31 days of real strikes, day-t -3.05), and
        # its growth is negative under every pricing tried. Same-side repeats
        # are a different animal and stay allowed -- they are most of what the
        # per-round cap buys and they are profitable. See
        # config.cross_allow_reversal.
        #
        # In-process state, so a restart mid-round forgets the side. Left that
        # way deliberately: `open_fills` holds a Fill, which carries no `mode`,
        # so reading the side back would mean re-querying the DB per tick on
        # the websocket clock, and blocking on any held token is exactly the
        # arms-independence rule this arm exists outside of. The window is one
        # round and the observed failure (two entries 22s apart) is in-process.
        if not self._ck("allow_reversal", r):
            held = self._cross_side.get(r.slug)
            if held is not None and held != side:
                self._cross_refuse(
                    r, side,
                    f"reversal of the {held} entry already taken this round",
                    tau=tau, spot=feed.price)
                return
        # How far through the strike we already are. A cross that is 1-2 bps
        # deep has been seen and repriced (that cell measures -0.19/$1); the
        # edge lives in the nick the venue has not registered yet.
        spot_now = feed.price
        # THIS family's sigma. `self._sigma` is the primary's, so before the
        # widening it was the same object; with six families it would size
        # eth/bnb's moneyness tilt off BTC's volatility and label every
        # recorded row with it.
        sigma_now = (ctx.get("sigma") if ctx else getattr(self, "_sigma", 0.0))
        if not sigma_now:
            sigma_now = getattr(self, "_sigma", 0.0) or 0.0
        # DEPTH IS MEASURED FROM `level`, NOT FROM `r.strike`. The number this
        # gate exists to bound is "how far has the spot moved past the level the
        # round is paid on", and the strike is not that level -- see
        # config.cross_ref_twap. `level` is `r.strike` on any round whose 60 s
        # reference the tape could not bracket, so this is the old behaviour
        # exactly where the new number is unavailable.
        depth_bps = 1e4 * abs(spot_now - level) / level if level else 1e9
        # MINIMUM depth, in TICKS. bps is the wrong unit for a floor: the same
        # $0.01 is 0.0013 bps on BTC and 0.1377 on BNB, so BNB's median trigger
        # was ONE tick -- the last decimal flipping. See config.
        min_ticks = self._ck("min_depth_ticks", r)
        if not train and min_ticks > 0:
            # THE COMPARISON AND ITS TOLERANCE BOTH LIVE IN `passes_depth_floor`,
            # IN TICKS. Do not re-inline it: the tools replay this gate and a
            # copy here is how they drifted apart (they read the GLOBAL
            # tick_size, which is an empty band on xrp/doge).
            #
            # The old form was `abs(spot_now - r.strike) < min_ticks * tick
            # - 1e-12`. `abs(...)` is a difference of two doubles, so its error
            # is the ULP of a number of the spot's size -- 1.46e-11 at BTC's
            # ~7.6e4, which is 14x the epsilon that was supposed to absorb it.
            # So it refused a crossing sitting exactly ON the floor: 3 of 154
            # recorded refusals, each logging "only 1 tick(s) through the
            # strike, floor is 1" (2026-09-16 06:19 / 20:34 / 23:39, btc-5m).
            # Spot 75996.005 against strike 75996.015 reproduces it exactly.
            #
            # The point is NOT to admit sub-tick crossings -- it is that the
            # same economic state must not be admitted or refused according to
            # which way two floats happened to round.
            mk = market_prefix(getattr(r, "slug", None))
            if not passes_depth_floor(spot_now, level, mk):
                self._cross_refuse(
                    r, side,
                    # 2 decimals, because the spot feed quotes a MID on a
                    # half-tick grid, so the real depths here are 0 / 0.5 / 1.0
                    # and "%0f" printed the first two alike as `0 tick(s)` --
                    # which reads exactly like the gate refusing a crossing it
                    # should have taken.
                    f"only {depth_in_ticks(spot_now, level, mk):.2f} tick(s) "
                    f"through the {level_src} reference, floor is {min_ticks:g}",
                    tau=tau, spot=spot_now, sigma=sigma_now)
                return
        # MINIMUM depth in SIGMA OF THE TIME REMAINING. 0 = OFF.
        #
        # The tick floor above is a per-symbol correction (BNB's median trigger
        # is ONE tick) and is a no-op on BTC. This is the signal-to-noise test
        # it cannot make: a crossing of z ~ 0 is a coin flip at ANY horizon,
        # because the diffusion still to come decides the round -- and the arm
        # pays ~0.6 for it, so it loses by construction.
        #
        # `sigma_now` is sigma_1m, so the diffusion left is sigma_1m*sqrt(tau/60)
        # and z is unitless. See config.cross_min_depth_sigma for the two
        # independent measurements (51 real fills; 1,645 rounds of true strikes)
        # that put the coin-flip/signal boundary at z ~ 0.4.
        min_sig = self._ck("min_depth_sigma", r)
        if not train and min_sig > 0 and sigma_now > 0 and tau > 0:
            z = depth_sigma(spot_now, level, sigma_now, tau)
            if z < min_sig:
                self._cross_refuse(
                    r, side,
                    f"crossing is {z:.2f} sigma deep ({depth_bps:.2f} bps, "
                    f"tau {tau:.0f}s), floor is {min_sig:.2f}",
                    tau=tau, spot=spot_now, sigma=sigma_now)
                return
        # ⛔ 0 IS *NOT* OFF FOR THIS KNOB, unlike every other cap in this arm.
        # `depth_bps > 0` is true for essentially every crossing, so 0 means
        # REFUSE ALL -- and `cross_overrides` relies on that to disable a single
        # family (test_an_override_on_this_market_blocks_the_entry). A `> 0`
        # guard was written here to restore the repo's usual convention and then
        # REVERTED: it would have silently inverted a documented, tested meaning
        # on the live order path. To disable the gate, use a large number
        # (config ships 99.0), not 0.
        if not train and depth_bps > self._ck("max_depth_bps", r):
            self._cross_refuse(r, side,
                               f"{depth_bps:.2f}bps through the {level_src} "
                               f"reference, gate is "
                               f"{self._ck('max_depth_bps'):.2f}",
                               tau=tau, spot=spot_now, sigma=sigma_now)
            return
        # How far this round has ALREADY travelled, open -> now. A round that
        # has swung wide is one where a late crossing reverses again; a calm
        # one is where it sticks (see config.cross_max_round_range_bps).
        #
        # Read from `self._ticks`, NOT `feed.ticks`. The feed deque's span is a
        # BURST RATE rather than a duration (40,000 updates is ~5 minutes at
        # BTC's ~114/s, but far less in a burst), while `self._ticks` is
        # appended once per poll and holds 2400 samples, so its span is fixed
        # by the poll cadence and does span a 5-minute round.
        rng_gate = self._ck("max_round_range_bps", r)
        probe = False
        if not train and rng_gate > 0:
            rng_bps = self._round_range_bps(r)
            if rng_bps is not None:
                if rng_bps > rng_gate:
                    if self._probe_fires(r):
                        probe = True        # flag-off experiment; see config
                    else:
                        # ⛔ TWO DECIMALS AND THE MARGIN, BECAUSE ONE IS NOT A
                        # DIAGNOSIS. The comparison is exact; the LINE was not.
                        # MEASURED 2026-09-17: this gate logged
                        #     round already moved 13.6bps, gate is 13.6
                        # for a true 13.6086 against a 13.6000 gate. The two
                        # numbers printed IDENTICAL, so a correct refusal read
                        # as an impossible one and was reported as a bug. Naming
                        # the excess makes the line answer its own question at
                        # any magnitude -- no precision choice can, when the
                        # margin is smaller than the last printed digit.
                        self._cross_refuse(
                            r, side,
                            f"round already moved {rng_bps:.2f}bps, gate is "
                            f"{rng_gate:.2f} (+{rng_bps - rng_gate:.3f} over)",
                            tau=tau, spot=spot_now, sigma=sigma_now)
                        return
        # The price that binds is the order's own quote; this is band sanity on
        # the last asks the poll saw. The ceiling is what makes the arm pay --
        # above it the favourite is overpriced and the cell measures negative.
        # Read the WEBSOCKET book directly, not self._asks. This arm runs on
        # the tick clock while _record refreshes self._asks only once per poll,
        # so going through it would throw away exactly the resolution the feed
        # was added for -- and the venue completes 66% of its post-cross
        # repricing inside that one second.
        wsb = getattr(self, "ws_book", None)
        top = (wsb.best(r, settings.ws_book_max_age_s)
               if wsb is not None else None)
        if top is not None:
            ask = top.ask(side)
            depth_usd = top.ask_dollars(side)
            # How far the book has ALREADY moved since the last poll saw it.
            # MEASURED 2026-09-13 on 163 fill-aware triggers, split by this:
            #     run <= 0.00   n=29  win 0.931  EV/$ +0.5436  day-t 4.63
            #     run 0.00-0.05 n=44  win 0.750  EV/$ +0.0596  day-t 1.60
            #     run 0.05-0.15 n=40  win 0.725  EV/$ +0.2028  day-t 2.29
            #     run 0.15+     n=50  win 0.740  EV/$ +0.0584  day-t 1.86
            # Catching the book BEFORE it reprices is worth 3x the arm's
            # average -- which is the arm's stated premise ("the nick the
            # venue has not registered yet") finally measurable.
            #
            # Recorded, NOT gated on. That split is n=29 out of a post-hoc
            # 4-way cut measured at 1s resolution, and the state it selects is
            # sub-second; gating on it from that evidence would be fitting.
            # The websocket samples at ~250ms, so a day of live triggers
            # measures it properly and then the gate is a decision, not a bet.
            stale_ref = asks[0] if side == "Up" else asks[1]
            book_run = (ask - stale_ref) if stale_ref is not None else None
        elif primary:
            # `asks`, not `self._asks`: with six families in flight the
            # attribute holds the PRIMARY's book, so a non-primary market would
            # have priced its order off BTC's ask.
            ask = asks[0] if side == "Up" else asks[1]
            depth_usd = None                    # unknown: do not cap on it
            book_run = None
        else:
            # OFF-PRIMARY THERE IS NO FALLBACK. Only the primary fetches the
            # REST book (it is the one that costs SAPI quota), and its round is
            # the only one refetched every poll -- so for the other families the
            # displayed price behind `asks` can be a whole round old. Pricing a
            # live order off that is how a stale book becomes a filled trade, so
            # a stale websocket means no trade rather than a guessed one.
            self._cross_refuse(r, side, "no fresh websocket book off-primary",
                               tau=tau, spot=spot_now, sigma=sigma_now)
            return
        # A NON-POSITIVE ask is a MISSING INPUT, not a price outside the band.
        # MEASURED, 2026-09-17: the refusal census over the Polymarket era
        # contained `ask 0.000 outside [0.10,0.90]` -- twice. 0.000 is what an
        # absent book/ask is spelled as downstream, so the gate above (`ask is
        # None`) never fired and a missing price was reported as a price that
        # was too low. That is the same class of error as the stale-display
        # price: it makes a missing measurement look like a measurement. The
        # two cases need different names because they need different fixes --
        # "outside the band" says the market disagrees with us, "no ask" says we
        # cannot see the market at all.
        if ask is None or not ask > 0:
            self._cross_refuse(r, side, "no ask on the book",
                               tau=tau, spot=spot_now, sigma=sigma_now)
            return
        # ⭐ THE BAND IS CHECKED ON THE BOOK WE PAY, NOT THE ONE WE SEE.
        # `ask` here is OUR websocket ladder, and it runs a median 0.030 BELOW
        # the venue's ask, 0.130 below it at the tail.
        # ⛔ THIS USED TO SAY "a median 593 ms behind the venue's (p90 1015 ms)"
        # AND THAT WAS WRONG. 593/599 ms is the median of `venue lag` over the
        # 968 occasions the lag ALARM FIRED -- a distribution conditioned on
        # the alarm, quoted as if it were the feed's typical latency. It never
        # was. `book_age_ms` (median 24 ms, p90 271 ms) does not settle it
        # either: that is `now - recv_ms`, time since OUR last frame, which is
        # near zero on a busy book however far away the venue is.
        # The PRICE GAP above is unaffected -- it comes from 305 shadow-book
        # samples that compare our ladder to the venue's own ask at order time,
        # and never depended on the latency figure. Only the explanation did.
        # `venue_lag_ms` is now recorded per quote so the real one is knowable. Gating the real band on it
        # selects a different set of trades from the one we then buy.
        # MEASURED, `tools/pm_two_book.py`, which models both books:
        #     band on the ladder  n=743 miss=129 win 57.1% $+160.81 | TEST $+85.61
        #     band on the venue   n=947 miss=  0 win 58.5% $+272.80 | TEST $+151.15
        # So this becomes a cheap SANITY filter, padded outwards, and the real
        # band is applied to the VENUE ask in `_place_order` -- which already
        # fetches it, so the only cost is probes on candidates that then fail
        # the real band: 9.0/hr -> 11.7/hr at a 0.10 pad. `pad` 0 = old
        # behaviour exactly.
        _pad = max(0.0, self._ck("ladder_band_pad", r))
        if not (self._ck("min_price", r) - _pad <= ask
                <= self._ck("max_price", r) + _pad):
            self._cross_refuse(
                r, side,
                f"ask {ask:.3f} outside "
                f"[{self._ck('min_price') - _pad:.2f},"
                f"{self._ck('max_price') + _pad:.2f}] (ladder, pad {_pad:.2f})",
                tau=tau, spot=spot_now)
            return
        # MODEL-EDGE GATE. This is the arm's economic constraint; the band
        # above is a sanity bound.
        #
        # The arm previously had only an ask band, which cannot say "cheap" --
        # it can only say "not too expensive". MEASURED 2026-09-17 over 1,246
        # triggers (8 days, one entry per round): the venue's ask at the
        # trigger instant is calibrated to within about a cent (pooled ask
        # 0.8909 against a 0.9090 win rate, z 1.35, not significant; ask 0.88
        # -> 93.3%, 0.94 -> 95.8%, 0.99 -> 93.8%). On a book like that a band
        # cannot be tuned into an edge, because there is no mispricing in the
        # band to find. The edge has to come from OUR fair disagreeing with the
        # quote, which is what `fair_value` prices -- the same closed form the
        # `digital` model uses, priced at THIS family's sigma (`sigma_now`, not
        # the primary's) and the time left.
        #
        # An unpriceable edge SKIPS this gate rather than becoming a refusal.
        # The arm has always been able to run without sigma -- the depth floors
        # switch themselves off when it is missing (`min_depth_sigma` requires
        # `sigma_now > 0` to fire) -- so turning a missing input into a refusal
        # here would quietly re-close the arm through a door it has never used.
        # The band above still applies in that case: this gate can only ever
        # remove entries, never add them.
        if not train and sigma_now > 0 and tau > 0:
            fair_p = fair_value(spot_now, r.strike, tau, sigma_now)
            if side == "Down":
                fair_p = 1.0 - fair_p
            edge = fair_p - ask
            emin = self._ck("min_edge", r)
            # 0 = OFF, this repo's convention for every gate knob (see
            # `cross_max_fill_px`, `max_depth_bps`). It has to mean OFF rather
            # than "off except for the sign": a NEGATIVE edge is the whole point
            # of being able to switch it off, and `edge < 0.0` would keep
            # refusing quotes while the knob reads off.
            if emin > 0 and edge < emin:
                # CONSTANT reason: `_cross_refuse` dedupes per (round, reason),
                # and putting the edge in the text makes a new row every tick --
                # the per-tick-refusal trap this file already records.
                self._cross_refuse(r, side, f"edge under the {emin:.3f} gate",
                                   tau=tau, spot=spot_now, sigma=sigma_now)
                return
        # TRAIN price gate. Placed HERE, after `ask` is FINAL, not on the
        # websocket branch: inside `if top is not None` it was skipped entirely
        # whenever the ws book was stale and the primary fell back to `asks`,
        # which is exactly when a gate is most needed. The reason is CONSTANT so
        # `_cross_refuse`'s per-(round, reason) dedupe collapses it to one row --
        # with the ask in the text every distinct cent made a new row (7 rows for
        # one round on 2026-09-15, the per-tick-refusal trap this file records).
        if train:
            # The static knob TIGHTENED by the dynamic EV ceiling. The ceiling
            # uses THIS FAMILY's measured win rate -- one shared number is
            # BTC's, and at margin 0.03 that is 0.876 against DOGE's 0.829, so
            # a global value would overpay the cheaper families by ~4.7c.
            # `min()` lives inside `entry_ceiling`, so this can only ever
            # refuse more, and the refusal line prints whichever value bound.
            sym = (market_prefix(getattr(r, "slug", "") or "") or ""
                   ).split("-")[0]
            tmax = entry_ceiling(self._ck("train_max_ask", r),
                                 TRAIN_P_WIN.get(sym) or settings.train_p_win,
                                 settings.ev_ceiling_margin)
            if tmax > 0 and ask > tmax:
                self._cross_refuse(
                    r, side, f"train ask above the gate {tmax:.3f}",
                    tau=tau, spot=spot_now, sigma=sigma_now)
                return
        # Size like the main arm unless a flat stake is configured: a flat $1
        # cannot compound, so the arm's contribution shrinks as a share of the
        # book while the bankroll grows.
        #
        # The tilt needs |z| -- how decided the round already is in units of the
        # vol still to come. A cross sits AT the strike by construction (the
        # depth gate allows <=0.5bps), so z is small and the tilt usually pins
        # to tilt_max. Measured at sigma_1m=2bps/min, tilt_reference_z=0.3:
        #     depth 0.01-0.10bps -> t=2.00 at every tau in the window
        #     depth 0.25bps      -> t=0.98 (tau 10s) .. 2.00 (tau 60s)
        #     depth 0.50bps      -> t=0.50 (tau 10s) .. 1.20 (tau 60s)
        # So a shallow cross -- the one the arm is built for -- stakes 2x base,
        # and a cross already half a bp through the strike stakes down. Note
        # tilt_factor returns 1.0 (NOT tilt_max) when z is exactly 0, which is
        # its "no information" path, so a zero sigma or zero tau sizes neutral
        # rather than doubling on a reading we do not have.
        if self._ck("stake_usdt", r) is not None:
            stake = self._ck("stake_usdt", r)
        else:
            mz = (abs(math.log(spot_now / r.strike))
                  / (sigma_now * math.sqrt(tau / 60.0))
                  if spot_now > 0 and r.strike > 0 and tau > 0 and sigma_now > 0
                  else 0.0)
            stake = self._stake_for(
                Signal(side, ask, ask, 0.0, 0.0, 0.0, True, "cross"), mz)
            # This arm only: see config.cross_stake_mult for the sweep. The
            # cap still binds -- the multiplier scales the arm, it does not
            # exempt it from stake_cap_usdt.
            stake = round(stake * self._ck("stake_mult", r), 2)
            if settings.stake_cap_usdt > 0:      # 0 = no cap, see config
                stake = min(stake, settings.stake_cap_usdt)
        # ---- A STAKE THAT CANNOT REACH THE VENUE MINIMUM IS A SKIP, AND THE
        # SIZING REFUSED IT -- NOT THE BOOK. -------------------------------
        # This has to come BEFORE the book-depth check below, because a zero
        # stake makes `sweep()` return (0.0, 0.0) whatever the book holds, and
        # the old order then logged
        #
        #     cross eth-updown-5m-... Up not taken: book holds only $0.00
        #     (wanted $0.00)
        #
        # MEASURED 2026-09-18: **731 of those in one afternoon**, ~20/second
        # while a trigger was live, and every one named the book when the cause
        # was the OTHER number on the same line. Two diagnoses were written off
        # that line (a thin book, then the gates) before anyone read the stake.
        # `stake_for_fraction` returns 0 below `min_stake /
        # FLOOR_MAX_BANKROLL_FRACTION` -- $4.00 for a $1 floor -- so on the live
        # $2.19 bankroll EVERY trigger lands here, and the arm cannot trade at
        # all until the account is back above it. `stake_usdt` per market is the
        # flat-stake escape hatch; see config.cross_stake_usdt.
        if stake < settings.min_order_usdt:
            self._cross_refuse(
                r, side, "stake under the venue minimum",
                tau=tau, spot=spot_now, stake=stake, sigma=sigma_now,
                detail=f"stake ${stake:.2f} < ${settings.min_order_usdt:.2f}; "
                       f"bankroll ${self.bankroll:.2f} at "
                       f"{settings.sizing_mode} sizing "
                       f"({settings.stake_fraction:.4f} x bankroll; the floor "
                       f"stops under "
                       f"${settings.min_order_usdt / FLOOR_MAX_BANKROLL_FRACTION:.2f})")
            return
        # The probe gets NO special sizing: it is the arm's normal order
        # (formula, tilt, caps), so what it measures is what a gate raise
        # would actually trade. Only the daily count is probe-specific.
        # Size to what is actually resting at the touch. On 2026-09-13 the arm
        # asked for $1.18 and the venue filled $0.90 of it, because nothing
        # checked whether the size existed -- the REST poll carries no depth at
        # all. A partial fill is not free: it books the full intended exposure
        # in the decision row and the guard, then holds less of it.
        # Price the WHOLE order against the book, not the touch. The touch is
        # routinely a few cents wide -- a live Down book showed $0.10 at the
        # best price and $2.61 one level behind it -- so an order gated on the
        # touch is gated on almost nothing. That is how a cross gated at 0.760
        # filled at 0.9008 on 2026-09-13: $1.09 swept through to the limit.
        if top is not None:
            sweep_px, fillable = top.sweep(side, stake)
            if fillable < settings.min_order_usdt or sweep_px <= 0:
                # CONSTANT reason, numbers in `detail`: the book moves every
                # tick and the REASON is the dedupe key, so numbers in the
                # reason make a new line per tick -- the trap this file records
                # twice. `_cross_refuse` prints the detail on the round's first
                # occurrence and collapses the rest.
                self._cross_refuse(
                    r, side, "book holds under the venue minimum",
                    tau=tau, spot=spot_now, stake=stake, sigma=sigma_now,
                    detail=f"book holds ${fillable:.2f}, wanted ${stake:.2f}")
                return
            if fillable < stake:
                log.info("cross %s %s: sizing down $%.2f -> $%.2f (book depth)",
                         r.slug, side, stake, fillable)
                stake = round(fillable, 2)
                sweep_px, _ = top.sweep(side, stake)
            # The gate applies to what we will PAY, not to the touch we happen
            # to see. cross_max_price is the load-bearing gate of this arm; a
            # sweep price outside it is a trade the measurement never blessed.
            if not (self._ck("min_price", r) <= sweep_px
                    <= self._ck("max_price", r)):
                self._cross_refuse(
                    r, side, "sweep price outside the band",
                    tau=tau, spot=spot_now, stake=stake, sigma=sigma_now,
                    detail=f"sweep {sweep_px:.3f} for ${stake:.2f}, band "
                           f"{self._ck('min_price', r):.2f}-"
                           f"{self._ck('max_price', r):.2f}, touch {ask:.3f}")
                return
            if sweep_px > ask + 1e-9:
                log.info("cross %s %s: $%.2f sweeps to %.3f (touch %.3f, "
                         "+%.3f)", r.slug, side, stake, sweep_px, ask,
                         sweep_px - ask)
            ask = sweep_px
        if self.bankroll < stake:
            # Silent before, and the one path that could reach here without the
            # book block above is a missing websocket book -- so an unfunded
            # arm and a blind one both vanished without a row. Constant reason.
            self._cross_refuse(
                r, side, "bankroll under the stake",
                tau=tau, spot=spot_now, stake=stake, sigma=sigma_now,
                detail=f"bankroll ${self.bankroll:.2f} < stake ${stake:.2f}")
            return
        # THE ROUND STAKE CAP IS PER FAMILY, NOT PER WINDOW. Its name and the
        # original note said "the round is one bet ... the round's CUMULATIVE
        # stake is the risk unit" -- true inside one market, FALSE across the
        # nine the arm trades: this accumulator is `[r.slug]`, and
        # `btc-updown-5m-...` and `sol-updown-5m-...` in the same 5-minute
        # window are different keys, so a slot can hold nine independent budgets.
        # Live 2026-09-18 11:20 spent $4 in one window across four families
        # against a $2 intent and settled -$2.30.
        #
        # Keying it on `rounds.end_ms` to make a true window cap was replayed on
        # the whole PM era and is NEGATIVE -- see config.cross_max_round_stake_mult
        # for the table (+$62.37 -> +$12.92 at cap 1/window, t 4.57 -> 1.52), and
        # do not "fix" the scope without re-reading it: the stacking is where the
        # edge is. It is also inert today, because `cross_max_per_round = 1`
        # refuses a family's second entry before this is ever read.
        rmult = settings.cross_max_round_stake_mult
        if rmult > 0:
            spent = (getattr(self, "_round_stake", None) or {}).get(r.slug, 0.0)
            room = rmult * stake - spent
            if room < settings.min_order_usdt:
                self._cross_refuse(
                    r, side,
                    f"round already holds ${spent:.2f}, cap is "
                    f"{rmult:.1f}x ${stake:.2f}",
                    tau=tau, spot=spot_now, stake=stake, sigma=sigma_now)
                return
            if room < stake:
                log.info("cross %s %s: sizing down $%.2f -> $%.2f (round cap)",
                         r.slug, side, stake, room)
                stake = round(room, 2)
        # ACCOUNT-WIDE exposure, across every family. See
        # config.cross_max_open_stake_pct: with six markets 0.945 of
        # simultaneously-open positions sit on the SAME side, so this is one bet
        # held many times, and the daily loss cap cannot help because they
        # expire together. Checked here -- after depth sizing, before ordering
        # -- so the number compared is the stake actually about to be committed.
        cap_pct = settings.cross_max_open_stake_pct
        if cap_pct > 0 and self.bankroll > 0:
            open_stake = self._open_cross_stake()
            # ⛔ NEVER REFUSE THE FIRST POSITION. This cap bounds CONCURRENCY --
            # it exists to stop one macro move opening a position in each of
            # nine families. With nothing open there is no concurrency to bound,
            # so refusing here only deadlocks the arm:
            #   LIVE 2026-09-19 01:52-02:02, bankroll $1.83 and cap 0.35 ->
            #   $0.64 against a $1.00 venue minimum, so EVERY trigger read
            #   "open cross exposure $0.00 + $1.00 exceeds 35% of $1.83".
            #   58 refusals, zero fills, and no trade can settle to rebuild the
            #   bankroll that would lift the cap -- the deadlock this knob's own
            #   note warns about, reached by drawdown rather than a stale read.
            # A LARGER FRACTION IS NOT THE FIX: it only moves the deadlock to
            # the next drawdown. The exemption is exact -- below
            # `min_order_usdt / cap_pct` in bankroll the cap can express nothing
            # BUT a block, and above it this branch never triggers anyway.
            if open_stake > 0.0 and open_stake + stake > cap_pct * self.bankroll:
                self._cross_refuse(
                    r, side,
                    f"open cross exposure ${open_stake:.2f} + ${stake:.2f} "
                    f"exceeds {100 * cap_pct:.0f}% of ${self.bankroll:.2f}",
                    tau=tau, spot=spot_now, stake=stake, sigma=sigma_now)
                return
        # The venue list is consulted to confirm we can still READ it: an
        # unreadable position list means we cannot reason about exposure at
        # all, so the arm sits the round out.
        #
        # What this deliberately does NOT do is refuse the round because a
        # token is held. That test was copied from _impulse_tick, where it is
        # correct because the impulse arm takes the MAIN slot. This arm has a
        # reserved slot of its own, and the main arm enters at tau 60-210 and
        # holds to the bell -- so the test refused 52% of this arm's triggers,
        # at an EV indistinguishable from the ones it let through (measured on
        # 162 fill-aware triggers over 6 days: +0.2416/$1 blocked against
        # +0.2411 allowed, day-t 4.44 against 1.69). It was also SILENT,
        # returning before the check_reserved call that logs a refusal.
        #
        # Doubling is still bounded, including across a restart: the reserved
        # slot counts unsettled cross fills because _restore_open_positions replays
        # them through guard.on_open(reserved=True), so check_reserved below
        # already sees a position this process never booked.
        # Claim the round across the ONLY await in this method (the venue
        # position read below). Everything else here is synchronous, so
        # this is the whole window in which two ticks can both pass the
        # per-round gates above and both claim. try/finally rather than a
        # release at each return: there are five refusal paths inside and
        # an exception must not leave the round latched shut.
        if not isinstance(getattr(self, "_cross_inflight", None), set):
            self._cross_inflight = set()
        if r.slug in self._cross_inflight:
            return
        self._cross_inflight.add(r.slug)
        try:
            if self.broker.mode == "live" and self.book:
                unbooked = await self._unbooked_round_tokens(r, up, down)
                if unbooked is None:
                    log.info("cross %s %s not taken: venue position list "
                             "unavailable", r.slug, side)
                    return
                if unbooked:
                    log.info("cross %s %s not taken: venue holds %d token(s) here "
                             "the ledger cannot explain", r.slug, side,
                             len(unbooked))
                    return
            # The one losing cell of this arm: the side OPPOSITE a position the
            # MAIN arm already holds. See config.cross_skip_opposite_to_main for
            # the measurement. Only the main arm counts -- a cross opposite an
            # earlier CROSS is the alternating-hedge case cross_max_per_round
            # exists to allow.
            if self._ck("skip_opposite_to_main", r):
                for fid, (fslug, held) in list(self.open_fills.items()):
                    if fslug != r.slug or getattr(held, "side", None) == side:
                        continue
                    reserved, _pool = pool_for_mode(self._fill_mode(fid))
                    if reserved:
                        continue                      # an earlier cross, not the main arm
                    self._cross_refuse(
                        r, side,
                        "the main arm holds the opposite side here (measured "
                        "EV/$1 -0.107 against +0.356 same-side)",
                        tau=tau, spot=spot_now, sigma=sigma_now)
                    return
            # The RESERVED slot, not the main one. The main arm enters at tau
            # 60-210 and holds to settlement, which covers this arm's whole window,
            # so `check` refused 82% of cross triggers as "already positioned" --
            # and 61% of those wanted the OPPOSITE side, i.e. hedges were being
            # blocked rather than double exposure prevented. Both sides are taken;
            # see check_reserved for what that gives up.
            # `max_open` is ACCOUNT-WIDE (guards.state.reserved_open counts every
            # open reserved position), so it must NOT be the per-round knob. The
            # per-round limit is enforced separately at the top of this method via
            # `_cross_fired[r.slug]`. Passing max_per_round here was harmless with
            # one family and would have capped all six at 5 concurrent positions.
            # Minus the orders already in flight: `reserved_open` only counts
            # positions that have BOOKED, and since placement moved off the poll
            # loop several can be outstanding at once.
            cap_open = settings.cross_max_open_positions or 10 ** 6
            cap_open -= getattr(self, "_pending_n", 0)
            if cap_open <= 0:
                self._cross_refuse(r, side, "reserved slots full (orders in flight)",
                                   tau=tau, spot=spot_now, sigma=sigma_now)
                return
            allowed, why = self.guard.check_reserved(r.slug, max_open=cap_open)
            if not allowed:
                self._cross_refuse(r, side, why, tau=tau, spot=spot_now, sigma=sigma_now)
                return
            # Claim before ordering, so a rejection skips the round rather than
            # risking a second attempt at it.
            self._cross_fired[r.slug] = self._cross_fired.get(r.slug, 0) + 1
            # Cumulative stake for the ROUND, claimed with the slot. Pruned to live
            # rounds so the map cannot grow all day.
            rs = getattr(self, "_round_stake", None)
            if not isinstance(rs, dict):
                rs = self._round_stake = {}
            rs[r.slug] = rs.get(r.slug, 0.0) + stake
            # Stamp the entry time with the claim, for the re-entry gap above.
            #
            # NOT `now_ms`. That is read at the TOP of this method, before the
            # venue round-trip below, so stamping it records when the tick
            # STARTED rather than when the round was claimed. A venue read
            # slower than the gap then expires the stamp before it is even
            # written, and the next tick walks straight through:
            #
            #   t=0.00  A starts, now_ms=0, gap passes, claims _cross_inflight
            #   t=0.06  B starts, blocked by _cross_inflight          (correct)
            #   t=1.50  A stamps 0, releases _cross_inflight
            #   t=1.55  C starts, now_ms=1550, reads last=0
            #           1550 - 0 >= 1000  ->  C PASSES, 50ms after A
            #
            # LIVE 2026-09-14, three rounds took paired entries 36-744ms apart
            # this way -- eth-updown-5m-1789415100 twice 65ms apart at the same
            # quote and stake, eth-updown-5m-1789413600 four times inside 300ms
            # -- 10 of 48 fills and $29.19 of the day's stake.
            lm = getattr(self, "_cross_last_ms", None)
            if not isinstance(lm, dict):
                lm = self._cross_last_ms = {}
            lm[r.slug] = int(time.time() * 1000)
            self._cross_last_ms = {k: v for k, v in lm.items() if k in live}
            self._round_stake = {k: v for k, v in rs.items() if k in live}
            # Remember the side, not just the count: the reversal guard reads it.
            self._cross_side[r.slug] = side
            self._cross_count += 1
        finally:
            self._cross_inflight.discard(r.slug)
        spot = spot_now
        sigma = sigma_now
        # `ask` is the last POLLED book price, and the poll runs ~1/s while the
        # venue completes 66% of its post-cross move inside that same second
        # (measured 2026-09-13, n=189: pre-cross poll 0.505 -> cross tick 0.618
        # -> +4s 0.645). So this number is NOT what we will pay, and saying
        # "@0.350" when the fill lands at 0.656 reads as a 30-cent execution
        # failure when it is really the price never having existed. Label it.
        # `ask` is now the SWEEP price for this stake when the websocket book
        # is live, so it is what we expect to pay rather than a touch we cannot
        # get. The old label said "stale_ask" and quoted the touch, which made
        # a normal sweep read as a 14-cent execution failure.
        src = "sweep" if top is not None else "poll"
        # The label has to carry the TRIGGER. A train fire logged as "CROSS"
        # reads as the cross arm violating its own 5-60s tau gate -- it did at
        # tau 820s on 2026-09-15 21:01, and that is exactly how it was misread.
        # THE SIGNAL'S NAME AND VALUE, not a hardcoded "spot": with `fc_trigger`
        # on, this line reads `fc 734.560 vs twap60 734.550` -- the number the
        # crossing was actually measured on, which is what a cohort split needs.
        # With the flag off the text is byte-identical to what it always was.
        log.info("%s %s %s %s_px=%.3f (%s %.2f vs %s %.2f, tau %.0fs) "
                 "stake=$%.2f -> %d today%s",
                 "TRAIN" if train else "CROSS", r.slug, side, src, ask,
                 sig_name, sig_px,
                 "strike" if train else level_src, r.strike if train else level,
                 tau, stake, self._cross_count,
                 (" D=%dms" % train_age_ms) if train_age_ms is not None else "")
        # `run` goes into the reason so every trigger carries the number the
        # gate decision will be made on, recoverable from the decisions table.
        run_txt = "" if book_run is None else f" run{book_run:+.3f}"
        # THE LEVEL'S SOURCE rides with it, for the same reason: a trigger priced
        # off `r.strike` (the tape could not bracket the 60 s reference) is the
        # OLD behaviour, and a cohort split by ref= is how that is audited
        # rather than assumed. See config.cross_ref_twap.
        ref_txt = (f" ref={'strike' if train else level_src}"
                   f" sig={sig_name}")
        # Same idiom, same reason: the number the gate decided on rides in the
        # reason so it is recoverable from `decisions` with no reconstruction.
        age_txt = "" if train_age_ms is None else f" D={train_age_ms}ms"
        kind = ("train into" if train
                else ("cross probe" if probe else "cross into"))
        sig = Signal(side, ask, ask, 0.0, 0.0, 0.0, True,
                     f"{kind} {side} @{ask:.3f} tau{tau:.0f}s{run_txt}{ref_txt}"
                     f"{age_txt}")
        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau, "side": side,
            "fair": ask, "quote": ask, "edge": 0.0, "hurdle": 0.0,
            "sigma_1m": sigma, "spot": spot, "oracle": None,
            "strike": r.strike, "stake": stake, "acted": 0,
            "reason": sig.reason,
        })
        token = up.token_id if side == "Up" else down.token_id
        # Neither pre-trade gate fits: there is no model fair to re-check, and
        # the candle-open gate is the opposite of this trade, which fires deep
        # inside a candle. Same reasoning as the double-down.
        #
        # The limit is priced at the CEILING, not at the trigger ask. Passing
        # the ask made `take_limit_px` return max(ask - margin, ask) == ask,
        # i.e. no slack at all: one tick of book movement between the trigger
        # and the order arriving meant no fill. The book moves hard right after
        # a cross -- 0.18 -> 0.50 inside one second on
        # btc-updown-5m-1789283400 -- so that pinned limit filled an estimated
        # 10% of the time, and the fills it did get were adversely selected
        # (we fill when the book moves AGAINST us, not for us).
        #
        # Pricing at cross_max_price gives the limit room to chase the book up
        self._schedule_cross_meter(
            r, side, "fire",                         # the +0.25/0.5/1.0s path
            label=("train" if train else "cross"))
        # to the ceiling and no further, which is the same shape that makes the
        # model arm fill 95%. `take_px` stays the trigger ask as the floor.
        # The limit sits just above the ask we actually observed, NOT at
        # cross_max_price. A LIMIT quote commits amountIn/priceLimit shares
        # regardless of the market (verified read-only across limits 0.30-0.99
        # while the ask was 0.960), so a limit pinned at 0.90 commits at 0.90
        # and the venue sometimes fills exactly that commitment. See
        # config.cross_limit_buffer for the measurement that forced this.
        # INVERSE in the ask, not proportional to it. The old rule was
        # `min(0.10, 0.20 * ask)`, which shrinks the buffer as the ask falls --
        # 0.10 at 0.50+, 0.064 at 0.32, 0.028 at 0.14. That is backwards with
        # respect to the mechanism: a cheap ask means the venue has NOT yet
        # repriced a fresh cross, so it is exactly the case where the book is
        # about to run furthest. It bid least where it had to bid most, and all
        # nine live cancellations were cheap (decided ask 0.14-0.57, median
        # 0.36) while no attempt at 0.60+ has ever missed.
        #
        # `cross_limit_buffer` stays the absolute cap; see config for the
        # limit-pinning sweep that chose 0.25 over the more aggressive 0.35/0.50.
        if train:
            # TRAIN: a flat bound. The proportional buffer below is tuned for a
            # crossing's ~0.50-0.60 ask and collapses as the ask rises; this
            # entry prices near 0.80, where the measured bound is a flat +1.5c.
            # cross_max_fill_px (0.65) cannot be allowed to clamp it -- it
            # would put the entry out of reach entirely -- hence fill_cap.
            ceil_side = min(self._ck("train_max_fill_px", r),
                            round(ask + self._ck("train_bound", r), 2))
        else:
            buf = max(0.01, min(self._ck("limit_buffer", r),
                                self._ck("limit_buffer_inv", r) * (1.0 - ask)))
            ceil_side = min(self._ck("max_price", r), round(ask + buf, 2))
            # ...and the same bound expressed RELATIVE to the ask we saw.
            # `ask + buf` is an ABSOLUTE allowance, so it is regressive in the
            # ask: 0.10 + 0.18 = 2.8x at an ask of 0.10, 0.60 + 0.20 = 1.3x at
            # 0.60. Live 2026-09-17 `sol-updown-5m-1789649400` saw an ask of
            # 0.100, carried a ceiling of 0.28, and FILLED AT 0.200 -- 2.0x the
            # only price we ever saw -- on a round that resolved against us.
            # Swept on the 8-day corpus (see config.cross_max_fill_multiple):
            # capping at 1.5x costs 8 of 122 triggers and 0.0077 EV/sh, so it is
            # a risk bound rather than an edge. 0 restores the old ceiling.
            mult = self._ck("max_fill_multiple", r)
            if mult and mult > 0:
                ceil_side = min(ceil_side, math.floor(ask * mult * 100) / 100)
        # INSTRUMENTATION. Three explanations for the gate->fill gap have now
        # been wrong: a stale gate (the websocket made it fresh and the gap
        # stayed), order latency (get_quote round-trips measure 43ms), and
        # book sweep (the measured premium for a $1-5 order is +0.0004 across
        # 19,452 real depth snapshots). On 2026-09-13 a cross decided at 0.760
        # with run+0.000 filled at 0.9008 four seconds later and none of those
        # account for it. So record what the book was doing WHILE the order was
        # in flight instead of inferring it again.
        # NON-BLOCKING FROM HERE. `buy_limit` awaits the fill for up to
        # limit_wait_s (20s) and `_cross_all` walks the six families
        # sequentially inside the poll loop, so awaiting the order here stopped
        # EVERYTHING. Measured 2026-09-14: every unfilled attempt produced a
        # quote gap the length of its own wait --
        #     waited 21.2s -> gap 21.4s     waited 21.5s -> gap 26.0s
        #     waited 12.4s -> gap 16.3s     waited  7.6s -> gap 11.5s
        # -- no market polled, no crossing on ANY family detectable, for that
        # long. The stall begins the instant a trigger fires, which is when the
        # market is moving and further crossings are most likely. A filled
        # order cost ~3.7s, a missed one 21s, so the 35% of attempts that miss
        # burned six times the blind time of the ones that worked.
        #
        # The slot is already claimed above (`_cross_fired`), so the await was
        # pure dead time. What the task must not lose is the cap arithmetic:
        # two orders in flight could each pass a 30% exposure check and land at
        # 60% together, so stake and slot are RESERVED here and released in the
        # task's `finally`.
        self._pending_n = getattr(self, "_pending_n", 0) + 1
        self._pending_stake = getattr(self, "_pending_stake", 0.0) + stake
        task = asyncio.create_task(self._cross_execute(
            r=r, sig=sig, side=side, token=token, stake=stake, ask=ask,
            ceil_side=ceil_side, did=did, up=up, spot=spot, sigma=sigma,
            tau=tau, wsb=wsb, train=train,
            fill_cap=(self._ck("train_max_fill_px", r) if train else None)))
        tasks = getattr(self, "_order_tasks", None)
        if tasks is None:
            tasks = self._order_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _arm_exit_watch(self, *, fid, slug, side, token, shares, stake, fee,
                        entry_ms, did=None, px=None, req=None,
                        oid=None) -> bool:
        """Register a held cross position with the exit watcher.

        ONE construction site, called from `_book_fill` AND from
        `_restore_open_positions`. The audit that produced this helper found the
        restart path had no registration at all, so a restart mid-round silently
        left every live position unprotected to the bell (observed 2026-09-18).

        The guards live here rather than at the call sites: `cross_exit_enabled`
        is read at call time so flipping it needs no rebuild, and a broker that
        cannot sell must simply get no watcher instead of failing at the first
        flip.
        """
        if not (settings.cross_exit_enabled
                and getattr(self.broker, "mode", None) == "live"
                and hasattr(self.broker, "sell_fak")):
            return False
        if not token or not shares or float(shares) <= 0.0:
            # No token means the position could never be sold, and zero shares
            # means there is nothing to sell: either would only produce one
            # refusal line per tick.
            return False
        watch = getattr(self, "_exit_watch", None)
        if not isinstance(watch, dict):
            watch = self._exit_watch = {}
        watch[slug] = {
            "fid": fid, "side": side, "token": token,
            "shares": float(shares), "stake": float(stake or 0.0),
            "fee": float(fee or 0.0), "entry_ms": int(entry_ms or 0),
            # Carried so a PARTIAL sale can split the fill without a store
            # lookup the DB layer does not offer.
            "did": did, "px": px, "req": req, "oid": oid,
            "flip_ms": None, "done": 0, "halted": False,
        }
        return True

    async def _cross_exit_check(self, r, level, ticks) -> bool:
        """Sell the position when the SAME trigger that opened it reads the
        other side. Returns True when this tick was spent on the exit.

        ⛔ SHIPS OFF (`cross_exit_enabled=False`), AND THE NUMBERS THAT ONCE
        JUSTIFIED IT ARE RETRACTED. This docstring used to claim the exit took
        the cohort "from +0.0773 to +0.1372 per share (t +4.32 -> +13.69)" on
        766-768 entries, with the flip arriving at "median 1.0 s after the
        entry". Every one of those came from `tools/pm_exit_flip.py:walk()`,
        which latched its exit candidate on the FIRST TICK AFTER THE ENTRY and
        never advanced it -- so it priced every exit at roughly the ENTRY
        price, and the median 1.0 s was the 1 Hz tape's own spacing, never a
        property of the flip. Do not restore them.

        WHAT REPLACED THEM, `tools/pm_exit_chain.py`: the exit re-swept under
        the full live gate chain, with the freed SLOT credited (an exited
        position gives its slot back at the exit instant, which at $5.11 and
        two concurrent positions is worth ~100 extra entries) and the sale
        priced at the BID, which is where all three real live sales landed:
            EXIT OFF       L1 n=689 win 63.1% $+64.12 t+2.70 | TEST $+19.17
                           L2 n=756 win 63.0% $+63.25 t+2.48 | TEST $+18.29
            best exit      L1 n=781 win 43.3% $+39.20 t+1.86 | TEST $+13.10
            (3 s / 60 s)   L2 n=869 win 44.5% $+49.44 t+2.25 | TEST $+13.56
        The failure is one column: WIN RATE 63% -> 43-47%. It fires on ~55% of
        positions and is a losing trade ~57% of the time -- it sells winners,
        and ~100 extra entries cannot cover that.

        THE LIVE RECORD SAYS THE SAME, HARDER. Ten completed sales against ~40
        attempts (23 size-downs, 8 UNVERIFIED, 7 confirm-pending, 7 confirmed
        late, 5 fak_unfilled, 5 sell_no_shares, 5 refused). Three of the ten
        were DUST (4.3e-05, 9.7e-05, 0.039 shares). Seven of ten sold at
        `tau 0 s` through the late-confirm path, dumping at 0.28-0.45 into the
        bell on a position about to settle at 0 or 1. ALL TEN LOST, -$3.04
        total, not one winner.

        The flip must PERSIST `cross_exit_confirm_s` before we leave, and the
        sweep still prefers 3 s with `cross_exit_min_tau_s` at 60 -- longer
        min_tau is monotonically better at both looks. That gradient is real;
        it simply points downhill from OFF.
        """
        if not settings.cross_exit_enabled:
            return False
        watch = getattr(self, "_exit_watch", None)
        if not watch:
            return False
        w = watch.get(r.slug)
        if w is None or w.get("halted"):
            return False
        # ---- A SALE WE PLACED BUT COULD NOT SEE YET IS CONFIRMED HERE --------
        # This MUST come before the `done` check below: a pending sale has
        # already spent the position's one order (`cross_exit_max_per_position`),
        # so that check would pop the watch and the confirmation could never
        # run -- which is exactly how four live exits ended up unbooked while
        # `settle_due` later booked the round result in their place.
        if w.get("pending"):
            got = self._confirm_pending_sale(r, w)
            if got is not None:
                # ⛔ THE REAL TAU, NOT 0.0. This passed a literal zero, so
                # every confirmed-late sale logged "at tau 0s" and read as a
                # dump into the bell. The real taus were 51-160 s. Two people
                # (one of them an assistant, 2026-09-19) read the artifact as
                # evidence the exit was selling at the close and argued from it.
                _tau = (int(r.end_ms or 0) - int(time.time() * 1000)) / 1000.0
                await self._cross_exit_execute(r, w, tau=_tau,
                                               sig_name="confirm",
                                               confirmed=got)
            return True
        if w.get("done", 0) >= settings.cross_exit_max_per_position:
            watch.pop(r.slug, None)
            return False
        if w["fid"] not in getattr(self, "open_fills", {}):
            # Closed by the normal settlement path (or voided): nothing to sell.
            watch.pop(r.slug, None)
            return False
        now_ms = int(time.time() * 1000)
        tau = (int(r.end_ms or 0) - now_ms) / 1000.0
        if tau <= settings.cross_exit_min_tau_s:
            return False          # the book goes one-sided at the bell
        # ⭐ A MINIMUM HOLD, MEASURED FROM THE FILL. `entry_ms` is the FILL's
        # timestamp, not the decision's, and that distinction is the whole
        # point: the decision and the fill are separated by a median 1.03 s and
        # a p90 of 1.79 s (live, n=30), because a missed FAK re-prices and
        # retries. A position is therefore BORN on a signal already a second or
        # more old, and an exit judging it immediately is judging the latency.
        #
        # MEASURED, `tools/pm_exit_models.py`, exit versus HOLDING the same
        # positions, bucketed by how long after entry the exit fired:
        #     0-5s     n=  7  exit -2.16  hold +0.22  delta -2.38   <- the only
        #     5-10s    n=  9  exit -4.48  hold -5.58  delta +1.10      losing
        #     10-30s   n= 57  exit -28.35 hold -39.17 delta +10.82     bucket
        #     30-60s   n= 91  exit -52.63 hold -68.51 delta +15.88
        # A minimum hold gains +2.38/+1.24 at 5 s and +1.23/+1.23 at 2 s across
        # the two looks, and TURNS HARMFUL at 12 s (-1.70/-1.92) and 20 s
        # (-8.07/-11.39). So this is a narrow guard on a known-bad bucket, not a
        # fitted parameter -- it is bounded on both sides by measurement.
        #
        # ⛔ SHIPPED ON n=7, WHICH IS BELOW THIS REPO'S USUAL BAR, and the
        # justification is asymmetry rather than significance: the guard can
        # only DELAY an exit, it touches ~2.4% of them, and both looks agree on
        # the direction. It was declined once on the sample size and then seen
        # live -- xrp-updown-5m-1789804800, filled 08:02:23.334 at 0.49 after a
        # 3.57 s decision-to-fill gap (the day's worst), sold 08:02:25.353 at
        # 0.31, a 2.02 s hold for -$0.4336.
        # KILL CRITERION: exits in [5s,10s) turning negative against holding
        # over ~40 of them. Revert: CROSS_EXIT_MIN_HOLD_S=0 + restart.
        hold_ms = int(settings.cross_exit_min_hold_s * 1000)
        if hold_ms > 0:
            ems = int(w.get("entry_ms") or 0)
            if ems and now_ms - ems < hold_ms:
                return False
        sig_ticks, sig_name = self._cross_signal(r, ticks, tau)
        if not sig_ticks:
            return False
        held = w["side"]
        # ⭐ THE EXIT RUNS THE ENTRY'S CHAIN, NOT A WEAKER ONE. Until
        # 2026-09-18 this called `crossed_side` with no `hold_s` and applied no
        # margin floor, while the entry applies BOTH -- so the arm refused to
        # OPEN on evidence it was happy to CLOSE on. There was no hysteresis
        # band at all: the buy threshold sat at `level + floor` and the sell
        # threshold at `level + 0.00`, i.e. every position was opened `floor`
        # bps above a trapdoor of zero width -- and `crossed_side` fires on the
        # FIRST tick past the level, so by construction every entry starts life
        # sitting on it.
        #   LIVE PROOF, sol-updown-5m-1789743000, consecutive log lines:
        #     14:52:35 not taken: only -0.20 bps of its own margin, floor 0.25
        #     14:52:36 / 14:52:42 / 14:52:43  the same refusal, four in all
        #     14:53:20.626 OPEN CROSS sol ... Down @0.540
        #     14:53:25.064 pm sell sol ... Down          <- 4.44 s later
        #   The round refused to OPEN four times on a move it then CLOSED on.
        #   eth-updown-5m-1789742700 did the same in 4.30 s. Both are the
        #   4 s floor of the old gate, hit exactly.
        # ⛔ ARM ON THE EVENT, CONFIRM ON THE STATE. `crossed_side` reports a
        # CROSSING, and a SUSTAINED flip produces exactly ONE of them -- so a
        # fire rule that lives inside the "this tick is a crossing into the
        # other side" branch can only ever fire on an OSCILLATING signal, which
        # is the opposite of what the rule is for.
        #   LIVE 2026-09-18, btc-updown-5m-1789750800 Up entered 17:02:09 @0.640:
        #     17:02:12 tau 167  FLIP to Down (m -0.60)  ARMED
        #     ...one opposite reading, ever; the spot sat Down for the rest of
        #     the round (-1.45, -6.77, -8.70, -2.70, -5.22 bps) and the Up token
        #     went 0.64 -> 0.07 while the watcher waited for a second crossing.
        # This is the trap this file already records ("a crossing is an event,
        # so a second one 3 s later may simply never come"). The confirmation is
        # therefore a STATE question -- is the signal STILL past the level on
        # the other side -- and only the ARMING is an event.
        now_side = crossed_side(list(sig_ticks), level, self._ck("look_s", r),
                                hold_s=self._ck("hold_s", r))
        sig_px = sig_ticks[-1][1]
        # THE STATE, independent of any crossing: which side of the level the
        # signal sits on now, and whether it is past the floor the ENTRY had to
        # clear. Same gate, same number -- see the hysteresis note above.
        floor = self._ck("min_margin_bps", r)
        margin = (1e4 * (sig_px - level) / level) if level else 0.0
        side_now = "Up" if sig_px > level else "Down"
        past_floor = (floor <= 0) or (abs(margin) >= floor)
        if side_now == held and past_floor:
            # BACK ON OUR SIDE, CONVINCINGLY: the flip died, so the latch goes.
            # A wobble that does not clear the floor leaves it alone -- a flip
            # still building is not a flip that died.
            w["flip_ms"] = None
            return False
        # ⭐ THE MARGIN RULE, 2026-09-19. The block below arms on a crossing
        # EVENT and confirms on the state. This leaves on the STATE ALONE, with
        # no event required -- and that single difference is what separates a
        # rule that loses from one that wins. `tools/pm_exit_models.py` put
        # seven rules on the same entries, slots and sale mechanics:
        #     off           L1 $+64.12 t+2.70 | TEST $+19.17   win 63.1%
        #     flip   3.0    L1 $+60.61 t+3.15 | TEST $+27.05   win 43.9%
        #     margin 4.0    L1 $+85.04 t+3.97 | TEST $+39.46   win 57.1%
        # take/bracket/trail are all worse; `take` reaches 87% win rate and
        # makes nothing, because it caps winners. The crossing-event arming was
        # crippling the old rule: a SUSTAINED flip produces exactly ONE
        # crossing, so a rule waiting for a second one waits forever.
        #
        # WHY IT SURVIVES THE SALE NOT LANDING, which is the risk that matters
        # here. A miss is a RETRY next tick, not an abandoned exit, and this
        # fires only ~165 times against margin 0.5's ~322 -- so it beats holding
        # at EVERY completion rate measured, down to the worst case:
        #     completion  1.00   0.88   0.70   0.50   0.39   (hold: +27.00)
        #     TEST L1    +45.39 +43.91 +43.33 +42.75 +40.85
        # 0.88 is the live rate once the UNVERIFIED bug is excluded (7 of 8
        # post-fix attempts completed); 0.39 is the all-time rate including it.
        # margin 0.5 stops beating holding below 0.70, which is why the larger
        # threshold ships even though the search ranked 0.5 first on train.
        # 0 = OFF, which restores the event-armed rule below.
        xb = float(settings.cross_exit_margin_bps)
        if xb > 0:
            if side_now == held or abs(margin) < xb:
                return False
            await self._cross_exit_execute(r, w, tau=tau, sig_name=sig_name)
            return True
        if w.get("flip_ms") is None:
            # NOT ARMED YET, and only a genuine CROSSING into the other side
            # arms it (with the entry's own `hold_s` applied, so a lone print is
            # not a flip). Arming is the event; everything after is state.
            #
            # ⛔ NO FLOOR ON THE ARMING, AND THAT IS THE WHOLE POINT.
            # `crossed_side` fires on the FIRST tick past the level, so the
            # margin AT a crossing is ~0 BY CONSTRUCTION -- the entry's own note
            # says so ("every entry is BY CONSTRUCTION a zero-margin entry").
            # Requiring `past_floor` here therefore asks for something that
            # essentially never happens, and the watcher never arms:
            #   LIVE 2026-09-18 21:07:47 eth-updown-5m-1789765500 Up @0.600.
            #   21:08:04 crossed_side reads Down (logged as "slot already used")
            #   -- a real flip 17 s in, margin ~-0.30 bps against a 0.50 floor.
            #   NEVER ARMED, no `cross exit` line at all, and the position rode
            #   0.600 -> 0.00.
            # The floor belongs on the STATE below, where the signal has had
            # time to move clear of the level, not on the instant it crosses.
            if now_side is not None and now_side != held:
                w["flip_ms"] = now_ms
            return False
        # ---- ARMED. Fire when the flip has PERSISTED, not when it re-crosses.
        if side_now == held or not past_floor:
            return False          # not flipped right now: wait, do not disarm
        confirm_ms = int(settings.cross_exit_confirm_s * 1000)
        if now_ms - int(w["flip_ms"]) < confirm_ms:
            return False
        await self._cross_exit_execute(r, w, tau=tau, sig_name=sig_name)
        return True

    def _confirm_pending_sale(self, r, w) -> tuple | None:
        """Did a sale we PLACED land? `(sold, px, fee)` or None (ask again).

        The venue's position list lags a fill in both directions and by more
        than one confirm budget covers (measured 2026-09-18: 0 shares 4 s after
        a confirmed buy; `still 1.8867 of 1.8867` 0.7 s after a matched sell).
        So a sale that could not be confirmed in the moment is confirmed later
        rather than the position being halted into limbo.
        """
        fn = getattr(self.broker, "confirm_sale", None)
        if fn is None:
            w.pop("pending", None)     # a broker that cannot answer: stop asking
            return None
        pend = w["pending"]
        since, floor = pend[0], pend[1]
        # The SENT size bounds the confirmation; `w["shares"]` is only what the
        # ledger believes. Older watch entries carry a 2-tuple, so fall back.
        sent = pend[2] if len(pend) > 2 else float(w["shares"])
        try:
            got = fn(token_id=w["token"], shares=float(sent),
                     since_ts=float(since), fallback_px=float(floor or 0.0))
        except Exception as exc:                              # noqa: BLE001
            log.warning("cross exit %s: sale confirmation failed (%s)",
                        r.slug, exc)
            return None
        if not got or float(got[0]) <= 0.0:
            return None
        return got

    async def _cross_exit_execute(self, r, w, *, tau, sig_name,
                                  confirmed: tuple | None = None) -> None:
        """Sell `w`'s position and close it in the ledger.

        THE SALE IS BOOKED AS A SETTLEMENT ROW for the entry's fill, and that
        one write is what makes every consumer agree without a new concept:
          * `_open_cross_stake` drops it (it counts fills with NO settlements
            row), so exposure stops counting a position that is gone;
          * `settle_due` can never pay it out against the round's winner --
            it iterates `open_fills`, and this fill leaves that dict here;
          * `pnl`/`bankroll`/the guard move exactly as a normal settlement
            moves them, because this is the same sequence `settle_due` runs.
        A PARTIAL sale splits the fill: the sold fraction settles against the
        original row and the remainder becomes a new open fill, so no fill row
        ever represents shares that no longer exist.
        """
        slug, fid, held = r.slug, w["fid"], w["side"]
        # ⛔ THE BROKER DERIVES THE FLOOR FROM ITS OWN BID -- the arm must NOT.
        # It used to read `self._wsb` (the arm's websocket book) and refuse the
        # exit when that book was unreadable:
        #   LIVE 13:08:51 `cross btc-updown-5m-1789736700 Down not taken: exit:
        #   no fresh bid to sell into` -- the watcher FIRED and the position
        #   still rode to the bell.
        # `PmBroker.book` IS the live PM feed, and `Touch.bid` is the authoritative
        # side of the touch. One book, one source of truth: the only refusal left
        # is the venue's own "nothing to take".
        fill = None
        if confirmed is None:
            fill = await self.broker.sell_fak(
                slug=slug, side=held, token_id=w["token"],
                shares=w["shares"], slip=settings.cross_exit_slip)
        if confirmed is not None:
            # A sale placed on an earlier tick and confirmed LATE by
            # `confirm_sale`: book it here with the venue's own numbers. NO
            # second order is sent -- the position spent its one order when it
            # went pending (`cross_exit_max_per_position`).
            sold, px, fee = (float(confirmed[0]), float(confirmed[1]),
                             float(confirmed[2] or 0.0))
            if px <= 0.0 and w.get("pending"):
                px = float(w["pending"][1] or 0.0)   # the floor: a lower bound
            # KEEP THE FLOOR FOR THE LOG. On this path `fill` is None -- it is
            # only assigned when `confirmed is None` -- so the log line's
            # `getattr(fill, "requested_px", ...) or 0.0` printed 0.0000 for
            # EVERY confirmed-late sale, which reads as "sold with no floor".
            # The floor was real; only the log could not see it.
            w["_floor_seen"] = float(w["pending"][1] or 0.0) if w.get("pending") else 0.0
            w.pop("pending", None)
            log.info("cross exit %s: sale confirmed late -- booking %g sh @ "
                     "%.4f", slug, sold, px)
        elif fill is None:
            # A FAK kill is a no-fill, not an error: try again next tick.
            log.info("cross exit %s: sell did not fill (%s) -- retrying next "
                     "tick", slug, getattr(self.broker, "last_failure", "?"))
            return
        elif getattr(fill, "pending", False):
            # ⛔ PLACED BUT NOT YET VISIBLE -- AND THIS IS NOT A HALT. The
            # order is spent (so it counts against the one-per-position rail),
            # the position STAYS WATCHED, and `confirm_sale` closes it on a
            # later tick when the venue catches up. Halting here is what left
            # four live sales unbooked today, with `settle_due` then booking the
            # round's result in their place.
            # THE SIZE THE BROKER ACTUALLY SENT, not the one we believe we
            # hold -- they differ whenever the entry was booked at its ceiling,
            # and confirming against the ledger's number reads a full close
            # where a remainder is still on-chain (live 18:32, eth-1789756200:
            # ledger "2", venue 1.8181, booked "sold 2 sh", no split row).
            sent = getattr(self.broker, "last_sell_size", None)
            w["pending"] = (time.time(), float(fill.requested_px or 0.0),
                            float(sent) if sent else float(w["shares"]))
            w["done"] = w.get("done", 0) + 1
            log.info("cross exit %s: sale PLACED, confirmation pending (%s) -- "
                     "position stays watched, no second order", slug,
                     getattr(self.broker, "last_failure", "?"))
            return
        elif getattr(fill, "unverified", False) or not fill.shares:
            # UNKNOWN STATE IS THE ONE THING TO STOP ON: the venue may have
            # sold the position, so a second sell from here would be a naked
            # short. Halt the watcher and leave the fill where it is.
            w["halted"] = True
            log.warning("cross exit %s: sell UNVERIFIED (%s) -- watcher halted "
                        "for this position", slug,
                        getattr(self.broker, "last_failure", "?"))
            return
        if confirmed is None:
            sold = float(fill.shares)
            px = float(fill.filled_px)
            fee = float(fill.fee_paid or 0.0)
        frac = min(1.0, sold / w["shares"]) if w["shares"] else 1.0
        # Captured BEFORE any split mutates the watch dict: these are the legs
        # of THIS sale, and `pnl.add`/the ledger must see them, not the stake
        # that is left on the book.
        stake_leg = w["stake"] * frac
        fee_leg = w["fee"] * frac
        cost = stake_leg + fee_leg
        proceeds = sold * px - fee
        pnl = proceeds - cost
        won = pnl > 0
        now_ms = int(self.broker._now() * 1000) if hasattr(
            self.broker, "_now") else int(time.time() * 1000)
        mode = self._fill_mode(fid)
        if frac < 0.999:
            # Split: the remainder stays a real, open position on its own row.
            left = w["shares"] - sold
            rs = w["stake"] * (left / w["shares"]) if w["shares"] else 0.0
            rf = w["fee"] * (left / w["shares"]) if w["shares"] else 0.0
            new_fid = self.store.insert("fills", {
                "decision_id": w.get("did"), "slug": slug,
                "ts_ms": w.get("entry_ms") or now_ms, "mode": mode,
                "side": held, "requested_px": w.get("req"),
                "filled_px": w.get("px"), "shares": left,
                "stake": rs, "fee_paid": rf,
                "order_id": w.get("oid"),
                "raw": f"exit split: {sold:g} of {w['shares']:g} sold",
                "token_id": w["token"], "ev_at_fill": None,
            })
            # ⛔ THE REMAINDER NEEDS ITS OWN Fill, NOT THE ORIGINAL OBJECT.
            # `settle_due` settles from the in-memory Fill, not from the DB row
            # -- so reusing the original here made the DUST settle as if it were
            # the whole position:
            #   LIVE 2026-09-18 21:33, btc-updown-5m-1789767000 Up.
            #   fid 984 sold 1.69 of 1.69491 -> pnl -0.4777 (correct).
            #   fid 985 held 0.004914 shares costing $0.0030 and settled
            #   **-1.0287** -- the FULL original cost -- so the round booked
            #   -1.5064 against a true -0.4807. Every exited round was
            #   over-booked by roughly a whole stake.
            # The DB row was always right; only the object handed to `settle`
            # was wrong, which is why the ledger and the venue disagreed.
            # THE FALLBACK MUST STILL PRODUCE A DISTINCT OBJECT. Handing back
            # `_orig` on any failure would reinstate the exact defect, so the
            # copy path sets the three sizes by hand rather than giving up.
            _orig = self.open_fills[fid][1]
            try:
                _rem = dataclasses.replace(_orig, shares=left, stake=rs,
                                           fee_paid=rf)
            except Exception:                                     # noqa: BLE001
                _rem = copy.copy(_orig)
                for _k, _v in (("shares", left), ("stake", rs),
                               ("fee_paid", rf)):
                    with contextlib.suppress(Exception):
                        setattr(_rem, _k, _v)
            self.open_fills[new_fid] = (slug, _rem)
            w["fid"], w["shares"], w["stake"], w["fee"] = (
                new_fid, left, rs, rf)
        self.store.insert("settlements", {
            "fill_id": fid, "slug": slug, "ts_ms": now_ms,
            "won": int(won), "payout": proceeds, "pnl": pnl,
            "mode": mode, "void": 0,
        })
        reserved, pool = pool_for_mode(mode)
        self.guard.on_settle(slug, pnl, reserved=reserved, pool=pool)
        fully_closed = frac >= 0.999 or w["shares"] <= 1e-9
        if not fully_closed:
            # ⛔ `on_settle` RELEASES the slot, AND A PARTIAL SALE MUST NOT.
            # The remainder is still open, so releasing here lets the arm open
            # another position against a concurrency/exposure bound it believes
            # it has freed. The P&L half of `on_settle` is still wanted (the
            # daily-loss and streak logic must see the realised loss), which is
            # why this is a re-claim rather than a skipped call.
            self.guard.on_open(slug, reserved=reserved, pool=pool)
        self.pnl.add(pnl, stake_leg, fee_leg + fee, won)
        self.bankroll += pnl
        self.open_fills.pop(fid, None)
        w["done"] = w.get("done", 0) + 1
        if fully_closed:
            self._exit_watch.pop(slug, None)
        log.info("cross EXIT %s %s: sold %g sh @ %.4f (floor %.4f) "
                 "proceeds %+.4f pnl %+.4f at tau %.0fs (sig=%s) | running: %s",
                 slug, held, sold, px,
                 float(getattr(fill, "requested_px", None)
                       or w.get("_floor_seen") or 0.0),
                 proceeds, pnl, tau, sig_name, self.pnl)

    async def _cross_execute(self, *, r, sig, side, token, stake, ask,
                             ceil_side, did, up, spot, sigma, tau, wsb,
                             train: bool = False,
                             fill_cap: float | None = None) -> None:
        """Place one cross order and book it, OFF the poll loop.

        Same code that used to run inline in `_cross_tick`; what changed is
        that the decision loop no longer waits for it.

        `train` selects the arm LABEL only. It is passed from `_cross_tick`,
        where it already decides the ceiling and the fill cap, and it names the
        arm that actually fired rather than re-deriving it here.
        """
        # ONE label for the log line AND the booked fill mode. These were
        # derived separately: the log from `sig.reason`, the ledger hardcoded to
        # 'cross'. So for the Train arm's entire life the log said TRAIN and the
        # database said cross, which buried 46 train fills -- win 78.3%, paid
        # 0.8406, EV/sh -0.0612 -- inside the Cross cohort (win 62.6%, EV/sh
        # +0.0015). Two arms with opposite signs under one label, and nothing in
        # the log to reconcile against, because the log was already right.
        arm_mode = "train" if train else "cross"
        try:
            t_send = time.monotonic()
            book_at_send = ask
            # OUR two numbers at send time, both from our own book: `ask` is
            # `Touch.sweep()` (the decision, the size, the gate, the ledger and
            # the CEILING all come from it) and `touch_at_send` is `Touch.ask`.
            # They are handed to the shadow probe so the log can say which of
            # them the VENUE agrees with.
            top_send = (wsb.best(r, settings.ws_book_max_age_s)
                        if wsb is not None else None)
            touch_at_send = top_send.ask(side) if top_send is not None else None
            # BOTH SIDES OF THE SAME ROUND, because one number cannot tell LAG
            # from A SWAPPED SIDE. Measured 2026-09-17 the venue read 0.82/0.83
            # while our Up read 0.57/0.58, and 12 s later 0.75/0.78 against
            # 0.56/0.60 -- a CONSTANT ~0.7 ratio. A book that is merely old
            # drifts; a book that is the WRONG SIDE keeps a fixed relationship.
            # With the other side logged, the two are one subtraction apart:
            # if ours(Down) is the venue's Up, the sides are swapped; if ours
            # and the venue agree on DOWN but not on UP, one token is stale.
            other = "Down" if side == "Up" else "Up"
            other_at_send = (top_send.ask(other) if top_send is not None
                             else None)
            # TWO SHAPES REACH HERE. On this venue `wsb.best` hands back a
            # `BookTop` whose bid/ask are METHODS (`bid(side)`, `ask(side)`); the
            # Binance wrapper's `BookTop` is the same. `Touch` is the only thing
            # here whose bid/ask are ATTRIBUTES. Reading `top_send.bid` blind
            # raised AttributeError inside the fire path (caught by
            # `tests/test_cross_arm.py::TestTheWebsocketBook`), so both shapes
            # are handled explicitly and a missing field degrades to None.
            ours_bid = ours_size = ours_age = None
            ours_token_lag = None
            if top_send is not None:
                b = getattr(top_send, "bid", None)
                ours_bid = b(side) if callable(b) else b
                size = getattr(top_send, "ask_size", None)
                ours_size = size if not callable(size) else None
                age = getattr(top_send, "age_s", None)
                ours_age = age() if callable(age) else None
            # PER TOKEN, not global. Measured 2026-09-17 the global `lag_ms()`
            # read 121 ms while the round being priced was four minutes old, so
            # the global number cannot answer "is THIS order priced off a live
            # book?" -- some busy neighbour keeps the maximum high.
            _book = getattr(wsb, "book", None)
            if _book is not None and hasattr(_book, "lag_ms"):
                try:
                    ours_token_lag = _book.lag_ms(asset_id=token)
                except Exception:                             # noqa: BLE001
                    ours_token_lag = None
            self._spawn_shadow_quote(
                r.slug, side, token, stake, ceil_side,
                ours_touch=touch_at_send, ours_sweep=book_at_send,
                # LAG vs LEVEL SHIFT. `ours bid` separates the two readings of
                # the 2026-09-17 08:49 line (`venue ask 0.2000 | ours touch
                # 0.1100`): if OUR bid and ask are BOTH ~9c low the book is at
                # an older price (the feed fell behind), and if only the ask is
                # off then the ladder is wrong instead. `age` dates the sample:
                # `Touch.recv_ms` is stamped per ASSET, so it is the only number
                # that says how far behind the socket actually was.
                ours_bid=ours_bid, ours_age=ours_age, ours_size=ours_size,
                ours_other=other_at_send, ours_other_side=other,
                ours_token_lag=ours_token_lag)
            self._take_last = None
            fill = await self._place_order(
                r, sig,
                fair_up=(ceil_side if side == "Up" else 1.0 - ceil_side),
                token=token, stake=stake, skip_pretrade_gates=True, take_px=ask,
                # Bid up to the ceiling, not to the stale quote plus a tick. See
                # limit_ceiling in _place_order for the round this cost us.
                limit_ceiling=ceil_side, fill_cap=fill_cap,
                # THE VENUE'S OWN BOOK IS THE PRICE BASE (see config
                # .cross_venue_take). None for the train arm, which keeps its
                # own ceiling arithmetic, and None when the knob is off -- so
                # both revert to the old base with no code change.
                venue_token=(token if (not train
                                       and self._ck("venue_take", r)) else None))
            elapsed = time.monotonic() - t_send
            # WHICH PRICE THE LIMIT WAS BUILT FROM, on the same tick as the
            # verdict: `base` is the VENUE's ask when it was readable, `src`
            # says whether the venue or our own ladder priced it, and `attempt`
            # says whether the FAK had to be re-priced and re-sent. Without this
            # the exec line cannot be reconciled against the 20-miss cohort the
            # 2026-09-18 measurement came from.
            _tl = getattr(self, "_take_last", None) or {}
            if _tl:
                # "sent ceiling", not "cap": the exec line above already prints
                # `limit` = OUR ladder's ceiling (0.58 in the 2026-09-18 eth-15m
                # fill), and this is the number the ORDER carried (0.65, built
                # from the venue's 0.60). Two different objects, two names.
                log.info("%s take %s %s: base %.4f (%s) sent ceiling %.2f, "
                         "attempt %s/%s", arm_mode, r.slug, side,
                         _tl.get("base", 0.0), _tl.get("src", "?"),
                         _tl.get("cap", 0.0), _tl.get("attempt", 0),
                         _tl.get("attempts", 0))
            top_now = (wsb.best(r, settings.ws_book_max_age_s)
                       if wsb is not None else None)
            book_now = top_now.ask(side) if top_now is not None else None
            got = getattr(fill, "filled_px", None) if fill else None
            # The four numbers that let the buffer be tuned from data instead of
            # intuition: what we decided on, what limit we actually sent, what we
            # got, and how much of the intended stake filled. The last one is the
            # discriminator -- a 100% fill paid its limit, partial fills beat it.
            cash = getattr(fill, "stake", None) if fill else None
            shares = getattr(fill, "shares", None) if fill else None
            # ⛔ `decided` AND `book now` ARE A REGRESSION DETECTOR, SO KEEP
            # BOTH ON ONE LINE. `decided` is `Touch.sweep()`'s price -- the
            # number the entry, the size, the gate, the LEDGER and the LIMIT
            # CEILING are all derived from. `book now` is `Touch.ask`, the
            # venue's own best ask, restated on every one of ~1,250 messages/s.
            #
            # They must agree. On 2026-09-17 they read 0.4000 against 0.6000 and
            # the FAK came back `no orders found to match`, because the ladder
            # behind `sweep()` was frozen at the last `book` snapshot while
            # `ask` was live: the arm decided on a price that no longer existed
            # and sent `round(0.40 + buf, 2) = 0.42` as its limit. A gap between
            # these two numbers on a NO FILL is that bug returning, and it is
            # the whole gate->fill story -- not latency, not adverse selection.
            log.info("%s exec %s %s: decided %.4f limit %.2f -> %s in %.2fs | "
                     "committed $%s of $%.2f (%s) | shares %s | cash/shares %s | "
                     "book now %s",
                     arm_mode,
                     r.slug, side, book_at_send, ceil_side,
                     ("%.4f" % got) if got else "NO FILL", elapsed,
                     ("%.4f" % cash) if cash else "0", stake,
                     ("%.0f%%" % (100 * cash / stake)) if cash and stake else "0%",
                     ("%.4f" % shares) if shares else "0",
                     ("%.4f" % (cash / shares)) if (cash and shares) else "n/a",
                     ("%.4f" % book_now) if book_now is not None else "n/a")
            if not fill:
                self._release_round_slot(r)
                return
            await self._book_fill(r, sig, fill, did=did, up=up, spot=spot,
                                  sigma=sigma, tau=tau, token=token,
                                  mode=arm_mode)
        except Exception as e:
            # ⛔ AN EXCEPTION IS NOT A NO-FILL, SO THE SLOT IS NOT GIVEN BACK.
            # This used to `_release_round_slot(r)` and log, i.e. treat an
            # unreadable venue state as "nothing filled" -- and the arm would
            # then take a second entry in the same round while the first order
            # may be live and unbooked. That is exactly the exposure the caps
            # cannot see, because the caps read the ledger and the ledger has
            # no row.
            #
            # LIVE 2026-09-17 19:09:12, btc-updown-5m-1789671900 Down: the
            # exception was `UnexpectedResponseError: account trades response
            # items malformed` raised while READING BACK an order the venue had
            # already matched (3.1 shares @ 0.335, settled our way, paid
            # $3.13). The line below said `cross order failed ... not retrying`
            # and nothing was ever booked. The read-back side of that is fixed
            # in the broker (`_trades_since` cannot raise, and `settle()`
            # confirms from the position list); this is the arm-level rule that
            # must hold even when some OTHER call in the order path throws:
            # hold the round, and say the exposure is UNKNOWN rather than zero.
            #
            # The honest way to clear it is the ledger -- the position list, or
            # a `fills` row from a later reconcile -- not a guess here.
            log.exception("cross order path raised for %s %s: %s: %s -- the "
                          "round's slot is HELD (not released): whether an "
                          "order reached the venue is UNKNOWN, so this round "
                          "will not be re-entered and the exposure must be "
                          "reconciled from the venue's position list",
                          r.slug, side, type(e).__name__, e)
        finally:
            self._pending_n = max(0, getattr(self, "_pending_n", 1) - 1)
            self._pending_stake = max(
                0.0, getattr(self, "_pending_stake", stake) - stake)

    def _take_cap_for(self, r, base_px: float, fill_cap: float | None) -> float:
        """The most a TAKE may pay when the best price that EXISTS is `base_px`.

        The SAME arithmetic `_cross_tick` applies to the price it decided on --
        `cross_max_price`, the inverse buffer, the `cross_max_fill_multiple`
        bound -- re-run against the VENUE's ask instead of our own ladder sweep,
        then clamped by the fill cap. The base is the ONLY thing that changes:
        the band, the margin floor, the ceiling and the cap are the same objects
        they were.

        ⛔ "CAN NEVER ENLARGE ONE" WAS WRONG AND IS REMOVED. The caller assigns
        `cap = self._take_cap_for(r, venue_ask, fill_cap)`, REPLACING the
        ceiling the decision authorised -- so when the venue sits above our
        ladder (68% of fills, median +0.030) this RAISES the limit. That is the
        feature working as designed (it exists to clear a venue ask our stale
        ladder could not reach, see the call site) but the sentence describing
        it as one-directional was false.
          LIVE 2026-09-19 10:47 eth-updown-5m-1789814700: ours 0.51 / venue
          0.64, decision ceiling 0.57, re-priced to 0.65, FILLED 0.63 at an
          ev_at_fill of -0.2022, exited at 0.05 for -0.9465.
        WHETHER IT PAYS IS UNRESOLVED. Raw, it looks bad -- re-based fills
        n=79 win 51% -0.1048/fill against n=101 win 51% -0.0568/fill. But the
        re-base fires precisely when the market has moved, so it is confounded
        with fast tape, and CONTROLLING FOR THE PAID PRICE the buckets
        disagree: at 0.5 re-based is +0.1307/fill against -0.2266, at 0.6 it is
        -0.1175 against -0.0007. n=76 vs 93. Not evidence either way.
        DO NOT change this to a ratchet on the raw comparison alone; measure it
        with the price controlled and a sample that can carry the split.
        """
        buf = max(0.01, min(self._ck("limit_buffer", r),
                            self._ck("limit_buffer_inv", r) * (1.0 - base_px)))
        cap = min(self._ck("max_price", r), round(base_px + buf, 2))
        mult = self._ck("max_fill_multiple", r)
        if mult and mult > 0:
            cap = min(cap, math.floor(base_px * mult * 100) / 100)
        fcap = self._ck("max_fill_px", r) if fill_cap is None else fill_cap
        if fcap > 0:
            cap = min(cap, fcap)
        return min(cap, 0.99)

    def _release_round_slot(self, r) -> None:
        """Give the round's entry slot back when an order fills NOTHING.

        `_cross_fired[slug]` is claimed at DECISION time -- that is what stops
        the websocket clock submitting one trigger twice -- but on a NO FILL we
        hold no position, and leaving the slot claimed locks the CROSS arm out
        of this round's late entry (tau 5-60: net +0.115/sh, day-t +5.43, the
        best-measured cell in this whole arm).

        Measured live 2026-09-15: four consecutive train NO FILLs at 21:36,
        21:37 and 21:51 twice, and the cross window produced NOTHING on every
        one of those rounds. A slot-blocked tick returns before it can log a
        refusal, so the cost is SILENT -- there is no line to grep.

        Only the COUNT is returned. `_cross_side` keeps the side it saw, so the
        reversal guard still refuses a subsequent flip.
        """
        fired = getattr(self, "_cross_fired", None)
        if isinstance(fired, dict) and fired.get(r.slug, 0) > 0:
            fired[r.slug] -= 1

    def _spawn_shadow_quote(self, slug, side, token, stake, ceiling,
                            **ours) -> None:
        """Launch the log-only venue probe; never raise into the fire path."""
        if getattr(self.broker, "mode", "") != "live":
            return
        try:
            task = asyncio.create_task(
                self._shadow_quote(slug, side, token, stake, ceiling, **ours))
        except RuntimeError:                # no running loop (fixture use)
            return
        tasks = getattr(self, "_shadow_tasks", None)
        if tasks is None:
            tasks = self._shadow_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _shadow_quote(self, slug, side, token, stake, ceiling,
                            *, ours_touch=None, ours_sweep=None,
                            ours_bid=None, ours_age=None, ours_size=None,
                            ours_other=None, ours_other_side=None,
                            ours_token_lag=None) -> None:
        """The VENUE's own book at fire time, logged; the ORDER never sees it.

        ⛔ THIS USED TO BE A TAUTOLOGY, AND THAT IS WHAT LET THE STALE LADDER
        SURVIVE. It called `broker.get_quote()`, whose own docstring reads
        "the wrapper's quote shape (NO VENUE CALL)" -- it re-reads OUR book,
        same `Touch.sweep()`, and so echoed the decision price straight back:
        `shadow quote ...: fresh px 0.4000 after 0ms`. A 0 ms "fresh" quote is
        the tell. It could never disagree with the number it existed to audit,
        so it certified a book that was 45 cents out.

        `GET /book?token_id=` is read-only and answers the only question that
        matters: on 2026-09-17 the arm logged `$1.00 sweeps to 0.610 (touch
        0.160, +0.450)` -- its OWN two fields, 45 cents apart, with nothing 
        available to say which was the dead one. This line says which.
        """
        t0 = time.monotonic()
        try:
            import aiohttp

            from btcpredict.venues.polymarket.book import PmBook
            to = aiohttp.ClientTimeout(total=None, connect=3.0, sock_read=4.0)
            async with aiohttp.ClientSession(timeout=to) as s:
                t = await PmBook.fetch_top(s, token)
        except Exception as exc:                          # noqa: BLE001
            # ⛔ THIS USED TO RETURN IN SILENCE. The probe is the ONLY instrument
            # that can disagree with our own book (see the docstring), so a
            # probe that fails without saying so reads exactly like a fire that
            # was never probed -- which is what 24 of the first 46 live FAK
            # attempts looked like: no `shadow book` line, no error, nothing in
            # the log to reconcile against. A measurement that cannot report
            # its own absence is not a measurement.
            log.warning("shadow book %s %s: PROBE FAILED after %.0fms: %s: %s",
                        slug, side, (time.monotonic() - t0) * 1000.0,
                        type(exc).__name__, exc)
            return
        ms = (time.monotonic() - t0) * 1000.0
        if t is None or t.ask <= 0:
            log.info("shadow book %s %s: venue book unavailable after %.0fms",
                     slug, side, ms)
            return
        # The verdict, spelled out rather than left as two numbers to subtract:
        # `lag` is how far OUR touch sits from the venue's, and it is what the
        # order's limit was derived from.
        lag = (ours_touch - t.ask) if ours_touch is not None else None
        verdict = "-"
        if lag is not None:
            if abs(lag) <= 0.02:
                verdict = "ok"
            elif lag > 0:
                verdict = "OURS HIGH"
            else:
                verdict = "OURS LOW"
        # LAG vs LEVEL SHIFT: our own book is shifted as a WHOLE only if the bid
        # moved with the ask. `age` then says whether the socket was behind.
        spread = (ours_touch - ours_bid) if (ours_touch is not None
                                             and ours_bid is not None) else None
        log.info("shadow book %s %s: venue %.4f/%.4f | ours %.4f/%.4f (%s) | "
                 "ours sweep %s size %s age %sms tok-lag %sms | spread %s vs "
                 "venue %s | ours %s ask %s | after %.0fms",
                 slug, side, t.bid, t.ask,
                 ours_bid if ours_bid is not None else float("nan"),
                 ours_touch if ours_touch is not None else float("nan"),
                 ("%+.4f" % lag) if lag is not None else "n/a",
                 ("%.4f" % ours_sweep) if ours_sweep is not None else "n/a",
                 ("%.0f" % ours_size) if ours_size is not None else "n/a",
                 ("%.0f" % (1000.0 * ours_age)) if ours_age is not None else "n/a",
                 ("%d" % ours_token_lag) if ours_token_lag is not None else "n/a",
                 ("%.4f" % spread) if spread is not None else "n/a",
                 "%.4f" % (t.ask - t.bid),
                 ours_other_side or "other",
                 ("%.4f" % ours_other) if ours_other is not None else "n/a",
                 ms)

    @staticmethod
    def double_trigger(*, entry_px: float | None, ask: float | None, tau: float,
                       min_tau: float, max_tau: float, min_quote: float,
                       max_quote: float, need: float, count: int, cap: int,
                       already: bool) -> str | None:
        """Should this winning position be added to? None = no, else why.

        Pure, so the rule is testable without a venue. Every bound here is one
        that has bitten elsewhere in this repo: the tau window is load-bearing
        (widening it turns the book negative), the quote band is where the edge
        actually lives, and `cap` stops a losing experiment from quietly
        becoming a position size. One add per round, ever.
        """
        if already or (cap > 0 and count >= cap):
            return None                      # 0 = no cap, the repo's convention
        if not (min_tau <= tau <= max_tau):
            return None
        if not ask or not 0 < ask < 1 or not entry_px:
            return None
        if not (min_quote <= ask <= max_quote):
            return None
        if ask < entry_px + need:
            return None
        return f"up {ask - entry_px:+.3f} from entry {entry_px:.3f}"

    async def _double_tick(self) -> None:
        """Add a second unit to a position that is already winning.

        MEASURED BEFORE SHIPPING (tools/arm_overlap --double-at, 1,177 rounds):
        the add scores +0.0686/$1 at t 2.05, but it is bought at a WORSE price
        than the first unit (~0.69 against ~0.61), so its payoff is 1.44:1 where
        the first unit's is 1.64:1, and it FAILS the half-sample check (second
        half +0.003 against the base arm's +0.112). Total profit rises ~36% on
        ~49% more capital, so return per dollar FALLS ~8%. It is also the first
        thing in this repo to relax the one-position-per-round invariant.

        So it is capped, booked as its own mode, and settled forward instead of
        trusted. Kill it if mode='double' EV/$1 <= 0 after ~200 adds.
        """
        if not settings.double_enabled or self.mode != "live":
            return
        r = getattr(self, "_round", None)
        if r is None:
            return
        # The round's ENTRY fill: the earliest one, since an add is always
        # later. `open_fills` is keyed by fill id and can hold both units of a
        # doubled round.
        mine = [f for (sl, f) in self.open_fills.values() if sl == r.slug]
        up, down = r.up, r.down
        feed = getattr(self, "_primary_spot", None)
        if (not mine or feed is None or not feed.price or not up or not down
                or not r.strike):
            return
        fill = min(mine, key=lambda f: f.ts_ms)
        up_ask, down_ask = getattr(self, "_asks", (None, None))
        ask = up_ask if fill.side == "Up" else down_ask
        now_ms = int(time.time() * 1000)
        tau = r.tau_s(now_ms)
        why = self.double_trigger(
            entry_px=getattr(fill, "filled_px", None), ask=ask, tau=tau,
            min_tau=settings.min_tau_s, max_tau=settings.max_tau_s,
            min_quote=settings.min_quote, max_quote=settings.max_quote,
            need=settings.double_at, count=self._double_count,
            cap=settings.double_max_per_day, already=r.slug in self._doubled)
        if why is None:
            return
        allowed, blocked = self.guard.check_add(r.slug)
        if not allowed:
            log.info("double %s not taken: %s", r.slug, blocked)
            return
        stake = round(fill.stake * settings.double_stake_mult, 2)
        if stake < settings.min_order_usdt or self.bankroll < stake:
            return
        token = up.token_id if fill.side == "Up" else down.token_id
        self._doubled.add(r.slug)
        self._double_count += 1
        log.info("DOUBLE %s %s +$%.2f at %.3f (%s) tau=%.0fs -> %d today",
                 r.slug, fill.side, stake, ask, why, tau, self._double_count)
        sig = Signal(fill.side, ask, ask, 0.0, 0.0, 0.0, True, f"double {why}")
        did = self.store.insert("decisions", {
            "slug": r.slug, "ts_ms": now_ms, "tau_s": tau, "side": fill.side,
            "fair": ask, "quote": ask, "edge": 0.0, "hurdle": 0.0,
            "sigma_1m": getattr(self, "_sigma", 0.0), "spot": feed.price,
            "oracle": None, "strike": r.strike, "stake": stake, "acted": 0,
            "reason": f"double: {why}",
        })
        # Neither pre-trade gate fits an add
        # (there is no fresh model claim to recheck, and the add happens
        # mid-round rather than at a candle open). `fair_up` is P(Up), so a Down
        # add must pass the OTHER side's price or the taker limit inverts.
        got = await self._place_order(
            r, sig, fair_up=(1.0 - ask) if fill.side == "Down" else ask,
            token=token, stake=stake, skip_pretrade_gates=True, take_px=ask)
        if not got:
            log.warning("DOUBLE %s not filled at %.3f", r.slug, ask)
            return
        await self._book_fill(r, sig, got, did=did, up=up, spot=feed.price,
                              sigma=getattr(self, "_sigma", 0.0), tau=tau,
                              token=token, mode="double")



    async def _idle(self, budget: float) -> None:
        """Wait out the poll interval -- on the feed's clock in impulse mode.

        Sleeping the whole interval is what put a median 500 ms (1 s worst case)
        between "the websocket showed the move" and "we looked", on a host
        5.3 ms from the venue. In impulse mode the wait ends on the next price
        update instead, and the trigger is evaluated there.
        """
        # WHICH ARMS WANT TICK RESOLUTION. This used to ask only about the
        # impulse arm, which silently made the CROSS arm's latency depend on
        # whether a completely different arm was switched on. Turning impulse
        # off on 2026-09-13 demoted cross from ~114 price updates/s to the 1s
        # poll (4.15s in practice, see RECORDED_REFRESH_S) and it immediately
        # missed a real crossing: btc-updown-5m-1789336500, spot through the
        # strike to 77336.00 for ~1.5s, never sampled.
        #
        # Both arms trade off `feed.ticks`, so both belong on the feed's clock.
        want_impulse = (settings.entry_mode in ("impulse", "both")
                        and settings.impulse_on_tick)
        want_cross = settings.cross_enabled
        if not (want_impulse or want_cross):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=budget)
            return
        feed = getattr(self, "spot", None)
        if feed is None:
            return
        deadline = time.time() + budget
        while not self._stop.is_set():
            try:
                if want_impulse:
                    await self._impulse_tick()
                if want_cross:
                    await self._cross_all()
            except Exception as e:                      # noqa: BLE001
                # Never let a trigger bug take down the loop: recording,
                # settlement and the risk guard all live in the poll.
                log.exception("impulse trigger failed: %s: %s",
                              type(e).__name__, e)
            left = deadline - time.time()
            if left <= 0:
                return
            if not await feed.wait_tick(timeout=left):
                return

    def _stake_for(self, sig: Signal, moneyness_sd: float = 0.0) -> float:
        """Stake for an accepted signal (see config.sizing_mode/_metric).

        Applies the optional sizing tilt on top of whichever base rule is
        active: the tilt redistributes the stake, it does not change the total.

        THE CAP IS APPLIED HERE, ONCE, FOR EVERY RULE. `stake_for_fraction`
        takes a `cap=` argument and `stake_for_edge` DOES NOT, and the clamp
        used to live INSIDE the tilt branch above -- so it ran only when
        `stake_tilt == "inv_z"`. With any sizing_mode that reaches the edge
        branch (`edge`, or a name the dispatcher does not recognise, which
        falls through to the same line) the configured cap was silently
        ignored and the stake came straight off [min_stake_usdt,
        max_stake_usdt].

        MEASURED, 2026-09-16 17:29:39 UTC, `btc-updown-5m-1789579500`:
        `stake_cap_usdt` was $1.00 and the bot spent **$5.00** -- 500 shares at
        the $0.01 entry floor, plus a $0.3465 fee, i.e. **-$5.3465 realised**
        when the round settled Up against the Down that was bought. There is no
        `decisions` row for that slug, so nothing in the ledger shows it.

        A risk bound has to bind LAST and bind on EVERY rule: a cap that one
        sizing mode ignores is not a cap. Applying it at the bottom means a
        future rule cannot opt out by forgetting to take `cap=`.
        """
        base = self._base_stake_for(sig)
        if base > 0 and settings.stake_tilt == "inv_z":
            t = tilt_factor(moneyness_sd, reference=settings.tilt_reference_z,
                            lo=settings.tilt_min, hi=settings.tilt_max)
            base = max(base * t, settings.min_order_usdt)
        if base > 0 and settings.stake_cap_usdt > 0:      # 0 = no cap, see config
            # Below `min_order_usdt` this makes the caller SKIP rather than
            # place a small order -- `_place_order` and the arms check
            # `stake < min_order_usdt` -- which is the honest outcome when the
            # cap and the venue minimum leave no legal size.
            base = min(base, settings.stake_cap_usdt)
        return base

    def _base_stake_for(self, sig: Signal) -> float:
        """The active sizing rule, before any tilt."""
        if settings.sizing_mode == "kelly":
            # Kelly returns 0 when the bet sits below the venue minimum; that
            # means SKIP, never "bet the maximum instead".
            return kelly_stake(sig.fair, sig.quote, bankroll=self.bankroll,
                               fraction=settings.kelly_fraction,
                               min_stake=settings.min_order_usdt,
                               max_stake=settings.max_stake_usdt)
        if settings.sizing_mode == "fraction":
            # Percentage of the live bankroll. Returns 0 (skip) when the
            # fraction cannot reach the venue minimum -- see
            # stake_for_fraction for why it must not round up on its own; the
            # floor is opt-in via stake_floor_to_minimum.
            return stake_for_fraction(self.bankroll,
                                      fraction=settings.stake_fraction,
                                      min_stake=settings.min_order_usdt,
                                      cap=settings.stake_cap_usdt,
                                      floor_to_minimum=(
                                          settings.stake_floor_to_minimum))
        # What counts as "strongest": the claimed edge, or expected profit per
        # dollar. The recording favours ev/$ -- large claims are the most
        # inflated, so sizing up on them rewards the noisiest signals.
        if settings.sizing_metric == "ev":
            value = sig.ev_per_dollar
            lo, hi = settings.stake_ev_lo, settings.stake_ev_hi
        else:
            value = sig.edge
            lo, hi = settings.min_edge, settings.stake_edge_hi
        return stake_for_edge(value, min_edge=lo,
                              min_stake=settings.min_stake_usdt,
                              max_stake=settings.max_stake_usdt,
                              edge_at_max=hi)

    async def _place_order(self, r, sig, *, fair_up: float, token: str,
                           stake: float,
                           skip_pretrade_gates: bool = False,
                           take_px: float | None = None,
                           impulse_order: bool = False,
                           limit_ceiling: float | None = None,
                           fill_cap: float | None = None,
                           venue_token: str | None = None) -> Fill | None:
        """Place one order, retrying a confirmed rejection a bounded number of times.

        Returns the fill, or None when the order could not be placed -- having
        set the order cooldown if the failure was an actual rejection rather
        than a gate refusing to trade.

        `skip_pretrade_gates` is for arms with no model fair to re-check that
        fire late in a round rather than at a candle open (cross, double).
        It skips only those two decision gates; every safety check below (the
        venue-position check before a retry, the booking, the verification) is
        untouched.

        `take_px` is the arm's own price for the trade, and it is a FLOOR on a
        taker limit -- for an arm with no valuation of its own (the cross),
        whose limit would otherwise sit below the ask it just read.
        """
        # A FOK order fails whenever the book moves between the quote and the
        # execution, and that is a transient miss rather than a reason to give
        # up the round -- so make a bounded number of attempts. Each one redoes
        # the whole pre-trade path: a spent quoteId cannot be re-sent, and both
        # the edge and the candle-open window can evaporate in the second or
        # two a failed attempt costs.
        # LOCKED-LOSS GUARD, ahead of every attempt and NOT behind
        # `skip_pretrade_gates` -- that flag skips signal re-checks, and this is
        # a risk check. Holding both sides of a round pays exactly 1.000, so a
        # pair costing more than `max_paired_price` is a loss by arithmetic and
        # cannot be undone on a venue with no sell. See config.
        cap_pair = settings.max_paired_price
        if cap_pair > 0 and sig.side in ("Up", "Down"):
            mine = float(getattr(sig, "quote", 0.0) or 0.0)
            # getattr: fixtures build Bot via __new__ and have no open_fills.
            _of = getattr(self, "open_fills", None) or {}
            for _fid, (fslug, held) in list(_of.items()):
                if fslug != r.slug or getattr(held, "side", None) == sig.side:
                    continue
                other = float(getattr(held, "filled_px", 0.0) or 0.0)
                if other <= 0 or mine <= 0:
                    continue
                if other + mine > cap_pair:
                    log.info("%s %s not taken: would hold both sides of this "
                             "round at %.3f + %.3f = %.3f (> %.2f); a matched "
                             "pair pays 1.000, so that is a locked loss",
                             r.slug, sig.side, other, mine, other + mine,
                             cap_pair)
                    return None
        attempts = max(1, settings.order_max_attempts)
        fill = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(settings.order_retry_delay_s)
                # Never start an attempt into a round that is nearly over.
                # Retries used to be cheap and quick; `order_status_wait_s` now
                # spends up to 9s hearing the venue's verdict, so three attempts
                # can span ~30s against a trigger with 30-50s of round left.
                # See config.order_min_tau_s.
                _tau_fn = getattr(r, "tau_s", None)
                _tau_left = (_tau_fn(int(time.time() * 1000))
                             if _tau_fn else float("inf"))
                # A MARKET attempt needs the round open only long enough to
                # place and execute; its verdict may land after the bell, and
                # the venue executes near the close (4 live fills booked with
                # under 1s of round left). The 5s floor was calibrated against
                # the verdict budget, which an immediate FOK does not spend --
                # and on 07:39:50 it refused the eth retry at 3.3s left, so the
                # venue never saw a second order. For market orders the floor
                # is what EXECUTION needs instead.
                _floor = settings.order_min_tau_s
                if settings.order_type == "market":
                    _floor = min(_floor, _MARKET_RETRY_TAU_FLOOR_S)
                if _floor > 0 and _tau_left < _floor:
                    log.info("not re-attempting %s %s: %.1fs of round left, "
                             "floor is %.1fs", r.slug, sig.side, _tau_left,
                             _floor)
                    break
                # Last line of defence against double exposure. Whatever the
                # previous attempt reported -- including a status the venue
                # called FAILED -- ask the venue what it actually holds before
                # re-placing: an earlier attempt that filled shows up here, and
                # buying again would put on a second position in a round we are
                # already long. The broker confirms this for its own failures
                # (see LiveBroker.buy), so this only ever catches the residual
                # cases -- and it is cheap next to a doubled position.
                if self.broker.mode == "live":
                    held = await self._venue_position_tokens()
                    if held is None:
                        log.error("cannot read the venue's positions; not "
                                  "re-placing %s %s after a failure",
                                  r.slug, sig.side)
                        break
                    if token in held:
                        log.error("venue already holds %s for %s; an earlier "
                                  "attempt DID fill -- not re-placing",
                                  token, r.slug)
                        break

            # Re-check against a live quote before committing. The venue's fee is
            # dynamic and its executable price can differ from the book, so the
            # decision above used estimates. get-quote does not execute, so this
            # costs nothing but a round trip -- and the quote is REUSED for the
            # order itself so placing it costs no extra latency (quote TTL is ~5s).
            quote = None
            if self.broker.mode == "live":
                # A WARM quote on the FIRST attempt only: the cross fetches it
                # before the crossing, and consuming it removes the 40-65ms
                # quote round-trip from the decision -> venue race (measured
                # 169-186ms against the venue's own records; the venue's
                # reprice settles inside ~250ms). Retries fetch fresh -- their
                # question is whether the book CAME BACK, which only a fresh
                # quote can answer. Any doubt (stale, stake mismatch) falls
                # through to the synchronous fetch, so this can only ever be
                # faster, never a different trade.
                if attempt == 0:
                    quote = self._take_prefetched_quote(token, stake)
                    if quote is not None:
                        # Marks the warm hits so the decision -> venue delta
                        # (order createTime minus the CROSS line) can be
                        # scored against the 169-186ms cold baseline; see
                        # FINDINGS "Can we execute faster". The age and px
                        # are part of the bound: the venue anchors the FOK to
                        # this quote's price, and the 2026-09-15 fires show
                        # refs lagging the book by up to 20x (0.03 vs 0.59).
                        try:
                            _wpx = float(quote.get("averagePrice") or 0.0)
                        except (TypeError, ValueError):
                            _wpx = 0.0
                        log.info("warm quote used for %s %s: age %.2fs px %.4f",
                                 r.slug, sig.side,
                                 getattr(self, "_warm_age_s", -1.0), _wpx)
                if quote is None:
                    quote = await self.broker.get_quote(token, stake)
                if not quote:
                    log.warning("no pre-trade quote for %s (attempt %d/%d)",
                                r.slug, attempt + 1, attempts)
                    continue
                real_px = float(quote.get("averagePrice") or sig.quote)
                real_fee = float(quote.get("feeRateBps") or settings.fallback_fee_bps) / 10_000.0
                # The band was checked at the trigger against the last ask the
                # POLL saw; this quote is the price that actually binds. In
                # impulse mode the model re-check below is skipped (there is no
                # model fair to re-check), so without this the book could drift
                # from 0.70 to 0.90 between the trigger and the order and the
                # price cap would be decoration.
                refused = impulse_price_refusal(
                    "impulse" if impulse_order else settings.entry_mode,
                    real_px, settings.impulse_max_price)
                if refused:
                    log.info("SKIP %s %s: %s (trigger saw %.3f)",
                             r.slug, sig.side, refused, sig.quote)
                    return
                # Re-check ONLY the chosen side, against its own hurdle. The old
                # code re-picked "the best side" at the live price and refused
                # the order when the OTHER side looked better -- discarding
                # trades whose own edge was still intact. A None quote makes the
                # other side untradeable, so evaluate() can only answer for this
                # one.
                # That quote is for the TOKEN being bought (get_quote sends its
                # tokenId), so its averagePrice IS that side's price. This used
                # to pass `1.0 - real_px` for a Down order -- complementing a
                # price that needed no complementing, so the Down candidate was
                # scored against 0.40 when the market asked 0.60. Measured on the
                # live record: down_ask 0.600 came back as "quote 0.40", and with
                # min_quote at 0.50 that refused every Down FAVOURITE -- the side
                # the honest pricing exists to buy. The live book ran 54 Up to 1
                # Down, against the replay's 267 Up / 347 Down, and the replay has
                # no recheck: it is the live path only.
                recheck = evaluate(
                    fair_up,
                    real_px if sig.side == "Up" else None,
                    real_px if sig.side == "Down" else None,
                    fee=real_fee, half_spread=0.0,
                    safety_margin=settings.safety_margin, fee_model=FeeModel.NOTIONAL,
                    max_quote=settings.max_quote, min_quote=settings.min_quote,
                    min_edge=settings.min_edge,
                    edge_haircut=settings.edge_haircut)
                if (not skip_pretrade_gates and not recheck.act
                        and settings.entry_mode != "impulse"
                        and not impulse_order):
                    # The edge is gone at the live price. Retrying would only
                    # be chasing a trade the gate has already refused.
                    # (In impulse mode there is no model edge to re-check: the
                    # trigger is the underlying's move, and the price sanity was
                    # applied at the decision. The quote is still fetched and
                    # reused, so the fill still lands on the live executable
                    # price rather than the stale displayed one.)
                    log.info("SKIP %s %s: real quote %.3f @ %d bps -> %s",
                             r.slug, sig.side, real_px, int(real_fee * 10_000),
                             recheck.reason)
                    return
                # Two different things happen here and the old line reported
                # only one of them. This statement is a SIBLING of the refusal
                # above, not a child of it, so it printed "pre-trade check ok"
                # even when `skip_pretrade_gates` short-circuited that refusal
                # -- which is every live order path (main.py:1260, :2694,
                # :2896). The recheck was still computed eagerly and its
                # degenerate EV was still formatted, so the log asserted a
                # passed check that had never run: 198 `pre-trade check ok ...
                # ev/$=-1.0000` lines, which were read as "an EV gate approved
                # this trade at -1.0". Nothing had approved anything.
                log.info("%s pre-trade check %s: px=%.3f fee=%dbps ev/$=%s",
                         arm_of(sig),
                         "BYPASSED(skip_pretrade_gates)" if skip_pretrade_gates
                         else "ok",
                         real_px, int(real_fee * 10_000),
                         f"{recheck.ev_per_dollar:+.4f}" if recheck.ev_valid
                         else "n/a (quote outside the priced band)")

            # Re-gate at execution time. The decision fired inside the open window,
            # but the live quote recheck above costs a second or two of round trip
            # -- and so does every retry; never let a fill land after the window
            # has closed. Trades happen at the candle open or not at all.
            if (self.broker.mode == "live" and settings.trade_on_candle_open
                    and not skip_pretrade_gates):
                sec = sec_into_candle(int(time.time() * 1000))
                if sec >= settings.candle_open_window_s:
                    log.info("SKIP %s %s: slipped past candle open (%ds); order withheld",
                             r.slug, sig.side, sec)
                    return

            # MARKET ONLY for the cross arm (operator directive 2026-09-15:
            # resting orders here are a dead end). The stale-low fallback to a
            # LIMIT is removed: a quote the FOK cannot fill costs one failed
            # attempt and no money, and the retry re-quotes -- resting an order
            # instead was never the answer for an arm whose edge is seconds
            # wide.
            use_limit = settings.order_type in ("limit", "limit_take")
            if pm_take_via_fak(settings, self.broker, limit_ceiling):
                # Polymarket's LIMIT path is `post_only=True`, i.e. a maker order
                # that these take arms cannot use. Trade them as takers instead:
                # `buy()` routes to `buy_fak()` (FAK == IOC) under this knob.
                # See `pm_take_via_fak` for the two live orders that put this
                # here, and for what it does NOT fix (a stale ceiling).
                log.info("%s take via FAK on Polymarket (pm_order_type=fak): "
                         "the post-only LIMIT path cannot cross",
                         arm_of(sig))
                use_limit = False
            if self.broker.mode == "live" and use_limit:
                if settings.order_type == "limit":
                    # Maker-first: post one tick below the quote and let it rest.
                    # A miss ENDS the round -- no retry and no crossing, because
                    # crossing would pay exactly the spread the limit exists to
                    # avoid, and the A/B has to measure posting on its own.
                    limit_px = max(0.01,
                                   round(sig.quote - settings.limit_offset, 2))
                else:
                    # Taker limit: cross the book, but never above our own
                    # valuation, and never more than a tick above the ask we
                    # actually measured. Two venues, same call: a CROSSING limit
                    # fills at the BOOK'S ASK (limit 0.18 against an ask of 0.15
                    # -> filled 0.1526 on eth-updown), so this reaches the real
                    # liquidity while the market path prices off the venue's
                    # DISPLAYED price, which lags its own book by up to 0.40 (75%
                    # of the price) and left the quote above the book in 5-7% of
                    # 335 live samples. Market fills averaged +0.16 above the ask
                    # on offer; limit fills, -0.014.
                    # The ceiling is the valuation, and NOTHING else. It used to
                    # be max(quote, fair - margin), which is wrong in the one
                    # case the ceiling exists for: a display that has gone stale
                    # HIGH (the venue repriced before us, i.e. we are late). Then
                    # the quote (say 0.90) beats the fair minus margin (0.86) and
                    # the limit would authorise a fill ABOVE our own valuation --
                    # a negative-EV entry, taken by the very arm the ceiling is
                    # there to stop. An arm with no valuation of its own passes
                    # `take_px`, which floors the limit instead.
                    # `limit_take_margin`, NOT `safety_margin`: the latter is the
                    # signal's hurdle (edge/signal.py), so raising it would
                    # quietly move the entry gate too. This knob sets only the
                    # ceiling, and it is swept in the config comment.
                    # The ceiling ALONE proved insufficient in live trading: on
                    # 2026-09-12 13:02 the trigger read an ask of 0.7326, our
                    # digital claimed P(Down)=0.94, the limit went out at 0.90 and
                    # filled at 0.8919 -- a trade the arm's own ev_at_fill scores
                    # at -0.195. `take_limit_px` clamps to ask + one tick, which
                    # cannot cost anything measured (the taker leg only ever fills
                    # AT the ask) and removes paying above the price we sized on.
                    limit_px = take_limit_px(
                        fair_up if sig.side == "Up" else 1.0 - fair_up,
                        settings.limit_take_margin, sig.quote,
                        settings.limit_ask_tick, take_px)
                    # Hard cap on the price PAID. Every other knob bounds the
                    # ask; this bounds the fill. See config.cross_max_fill_px:
                    # the arm is only profitable on fills of 0.55-0.65.
                    if limit_ceiling is not None and \
                            (settings.cross_max_fill_px if fill_cap is None
                             else fill_cap) > 0:
                        limit_ceiling = min(
                            limit_ceiling,
                            settings.cross_max_fill_px if fill_cap is None
                            else fill_cap)
                    if limit_ceiling is not None:
                        # `take_limit_px` clamps to quote + tick LAST, which
                        # overrides everything else. That clamp is right for the
                        # model arm -- its fair overshoots on fast moves and a
                        # 0.90 limit filled at 0.8919 for -0.195/$1 on
                        # 2026-09-12. It is WRONG for the cross arm, whose whole
                        # premise is that the side just went in the money and
                        # the quote it read is about to be stale by 60 cents.
                        #
                        # Measured live 2026-09-13 07:49 on
                        # btc-updown-5m-1789285500: spot crossed above the
                        # strike, Up was still quoted 0.200, the limit was
                        # clamped to 0.21, it never filled, and the round
                        # settled Up. A ~5x win the arm identified correctly and
                        # structurally could not take.
                        #
                        # `limit_ceiling` replaces that clamp with an explicit
                        # one. It is a RISK bound, not a valuation: above it the
                        # cell measures negative, so the order simply refuses.
                        # That makes it a bound in BOTH directions -- see
                        # `limit_post_px`, which is where the upper side is
                        # enforced (a bare max() posts through the cap).
                        limit_px = limit_post_px(limit_px, limit_ceiling)
                # Never rest past the bell: the cross fires 3-60s before it, and
                # the configured 20s would outlive the round.
                #
                # The cross path's wait is `cross_limit_wait_s` (6s since the
                # 2026-09-15 reconciliation: 2s cancels inside the venue's
                # fill process -- fills complete to ~5.2s -- and 5s left no
                # margin on those reports; see the config block).
                # `limit_ceiling` is what marks this as the cross path (see
                # above); the model arm keeps limit_wait_s, and 0 falls back.
                wait_cap = settings.limit_wait_s
                if limit_ceiling is not None and settings.cross_limit_wait_s > 0:
                    wait_cap = settings.cross_limit_wait_s
                # POST AT THE CEILING, not below the ask.
                #
                # Measured live 2026-09-16, immediately after the market->limit
                # swap: three consecutive orders went out at quote-1 tick
                # (0.13 / 0.55 / 0.16) against ceilings of 0.16 / 0.58 / 0.19,
                # and not one of them filled. That is the PASSIVE maker
                # configuration -- priced by tools/cross_limit_sim.py at 8.5%
                # fill against 86% for a limit posted at the arriving ask -- and
                # it is the same failure the 2026-09-13 note above records
                # costing a ~5x win.
                #
                # The refinement further up does the same thing, but it sits
                # behind an upstream branch that did not run for those orders,
                # so the price that reached the venue was the passive one from
                # the earlier assignment. This raises it where nothing can gate
                # it: immediately before the POST. It is a no-op for the model
                # arm, which passes limit_ceiling=None.
                #
                # `limit_post_px`, not a bare max(): the ceiling is
                # `min(ask + buffer, cross_max_fill_px)`, so when the ask is
                # above the cap the value from `take_limit_px` is already
                # higher than the ceiling and max() posts it through the cap.
                # The market branch clamps with min() and the venue enforces
                # it; this makes the limit path agree.
                if limit_ceiling is not None:
                    limit_px = limit_post_px(limit_px, limit_ceiling)
                    log.info("limit post %s %s %s: %.2f (ceiling %.2f, quote "
                             "%s, take_px %s, wait cap %.1fs)", arm_of(sig),
                             r.slug, sig.side, limit_px, limit_ceiling,
                             sig.quote, take_px, wait_cap)
                tau_fn = getattr(r, "tau_s", None)
                tau_left = tau_fn(int(time.time() * 1000)) if tau_fn else 1e9
                fill = await self.broker.buy_limit(
                    slug=r.slug, side=sig.side, token_id=token,
                    limit_px=limit_px, stake=stake,
                    wait_s=min(wait_cap, max(1.0, tau_left - 1.0)),
                    fill_break=settings.limit_fill_break)
                if fill:
                    break
                break
            kw = {"quote": quote} if self.broker.mode == "live" else {}
            # The price this order is sent AT. Every arm but the take arms ends
            # up sending `sig.quote` exactly as before; the take path may
            # re-base it on the venue's ask below. Hoisted so the `buy(...)`
            # call has one definition on every path.
            base = sig.quote
            base_src = "ours"
            # The cross arm's `limit_ceiling` is `ask + buffer` -- the highest
            # price its own measurement says is worth paying. Under MARKET it
            # becomes the quote ceiling, so the venue's lagging DISPLAYED price
            # cannot sell us a 0.17 ask at 0.84 (2026-09-11, +47% EV -> -70%).
            if limit_ceiling is not None:
                cap = min(round(limit_ceiling, 2), 0.99)
                _fcap = (settings.cross_max_fill_px if fill_cap is None
                         else fill_cap)
                if _fcap > 0:
                    cap = min(cap, _fcap)
                # ⭐ THE FLAT CEILING: DON'T COMPUTE A NUMBER THAT NEVER BINDS.
                # The venue probe below costs ~292ms on the order path to derive
                # a ceiling. MEASURED over 195 real FAK fills, that ceiling has
                # NEVER ONCE BOUND:
                #     filled exactly AT the ceiling : 0.0%
                #     headroom (ceiling - filled)   : median +0.0700, p10 +0.0200
                #     filled at or below the ask    : 73.8%, median sweep +0.0000
                # The size-at-touch tape says why: at a $1 stake the touch alone
                # fills the order 98.84% of the time, so the order never reaches
                # a level the ceiling could refuse.
                # What the probe actually buys is HEIGHT, not precision -- a
                # ladder-derived ceiling is too LOW 82.7% of the time and the FAK
                # then finds nothing. `cross_max_fill_px` is also always high
                # enough, and it is free.
                # THE LATENCY IS WORTH REAL MONEY. Slippage (filled - decided)
                # is monotone in time to fill, n=204:
                #     0.6-0.9s  median +0.0000   2.0s+  median +0.0600
                #     $3.79 total, $0.0186 per fill
                # ⛔ WHAT IS GIVEN UP, and it is why this ships OFF. Without the
                # probe there is no venue price, so `cross_ladder_band_pad`'s
                # venue-side FLOOR check cannot run -- the one that caught
                # `venue 0.3900 while our ladder read 0.5100` twice on 09-19.
                # The fill-cap refusal survives: it re-runs below against `base`,
                # which is our ladder, and the ladder sits BELOW the venue 82.7%
                # of the time, so `ladder > cap` still implies `venue > cap`.
                _flat = (bool(settings.cross_flat_take_ceiling)
                         and arm_of(sig) == "CROSS" and _fcap > 0)
                if _flat:
                    cap = _fcap
                # ── THE BASE PRICE IS THE VENUE'S ASK, NOT OUR LADDER ────────
                # MEASURED 2026-09-18 on the live NO-FILL cohort: of the 20
                # misses whose probing line caught the venue's own book, 17 had
                # a venue ask ABOVE our 0.65 cap while we had DECIDED at ~0.60,
                # and the mean gap between the venue's ask and the limit we sent
                # was +0.155. The clearest one:
                #     `shadow book xrp-updown-5m-1789704600 Down: venue
                #      0.3200/0.6900 | ours 0.3200/0.4200` -> decided 0.42,
                #      ceiling 0.54, "no orders found to match"
                # OUR sweep() read 0.42 AT THE SAME INSTANT the venue's book read
                # 0.69. The limit was then built off that 0.42, so it could not
                # match a 0.69 ask: the FAK miss was arithmetic, not latency.
                # Re-basing on the venue's ask changes the BASE only -- the cap,
                # the band, the floor and the margin gate are the same objects,
                # and `_take_cap_for` re-runs exactly the ceiling arithmetic
                # `_cross_tick` used on the price it decided.
                # A fetch failure is NOT a reason to refuse the trade: it falls
                # back to the old base, logged.
                if venue_token and not _flat:
                    # WARM FIRST. `_prefetch_venue_top` fires this same read at
                    # the tick before the crossing, so the ~292ms it costs is
                    # already spent by the time we get here. One shot, and any
                    # doubt falls through to the synchronous fetch below --
                    # which is the old path, byte for byte, when the knob is 0.
                    _warm = (self._take_prefetched_venue_top(venue_token)
                             if attempt == 0 else None)
                    vt = None
                    if _warm is not None and _warm > 0:
                        base, base_src = _warm, "venue"
                        cap = self._take_cap_for(r, base, fill_cap)
                        log.info("warm venue ask used for %s %s: age %.2fs "
                                 "ask %.4f against our ladder %.4f (cap %.2f)",
                                 r.slug, sig.side,
                                 getattr(self, "_vtop_warm_age_s", -1.0),
                                 base, sig.quote, cap)
                    else:
                        vt = await self._venue_top(venue_token, r)
                    if vt is not None and vt.ask > 0:
                        base, base_src = vt.ask, "venue"
                        cap = self._take_cap_for(r, base, fill_cap)
                        if abs(base - sig.quote) >= 0.02:
                            log.info("take base %s %s: VENUE ask %.3f against our"
                                     " ladder %.3f (%+.3f) -- limit priced off "
                                     "the venue (cap %.2f)", r.slug, sig.side,
                                     base, sig.quote, base - sig.quote, cap)
                    elif (_warm is None
                          and getattr(self, "_venue_take_dbg", None) != r.slug):
                        # ⛔ `_warm is None` IS LOAD-BEARING. On the warm path
                        # `vt` is never assigned, so without this guard a
                        # SUCCESSFUL prefetch logs "venue book unreadable" --
                        # a line whose whole job is to report the opposite, and
                        # the one the fail-closed pad check is read against.
                        self._venue_take_dbg = r.slug
                        log.info("take base %s %s: venue book unreadable; "
                                 "pricing off our ladder %.3f (old "
                                 "behaviour)", r.slug, sig.side, sig.quote)
                self._take_last = {"base": base, "src": base_src, "cap": cap,
                                   "attempt": attempt + 1, "attempts": attempts}
                # A TAKE CAP BELOW THE PRICE THAT EXISTS IS A DEAD ORDER.
                # A taker can only match an ask at or below the cap, so if the
                # book is already above it, nothing this order can reach is
                # worth having. MEASURED 2026-09-17 04:54:20:
                #     decided 0.7900 -> ceiling 0.8100 -> cap 0.65 (the fill cap)
                #     FAK sent at <= 0.65 against a 0.79 book
                #     venue: "no orders found to match with FAK order" (947 ms)
                # Nothing was bought, but the round slot was burned and the log
                # read like a trade attempt. The band allows entries to 0.90
                # (`cross_max_price`) while the fill cap refuses above 0.65, so
                # EVERY decision in 0.65-0.90 was this same dead order.
                # Judged against `base` -- the best price we know of -- so with
                # the venue readable this refuses exactly the orders the venue
                # would refuse, BEFORE the venue is called.
                # ⭐ THE REAL PRICE BAND, ON THE PRICE WE WILL PAY. The
                # decision gated a PADDED band on our ladder (see
                # `cross_ladder_band_pad`); this is where the true one binds,
                # and it is free because `base` is already the venue's ask.
                # Only the MIN is new: the max has always been enforced by the
                # dead-order check immediately below.
                # ⛔ A PAD WITH NO VENUE READ IS A HOLE: if the probe failed,
                # `base` is still our ladder and the padded band was the only
                # one applied. Refuse rather than buy outside the band on a
                # book we could not check.
                _pad2 = max(0.0, self._ck("ladder_band_pad", r))
                # ⛔ `not _flat`: with no probe there is no venue price to gate
                # on, and `base_src` is "ours" -- so this would refuse EVERY
                # order via its own fail-closed branch. Turning the flat ceiling
                # on therefore turns the venue-side floor OFF, which is stated
                # at startup rather than left to be discovered.
                if _pad2 > 0 and arm_of(sig) == "CROSS" and not _flat:
                    _lo = self._ck("min_price", r)
                    if base_src != "venue":
                        log.info("cross skip %s %s: venue book unreadable and "
                                 "the ladder band was padded -- refusing rather "
                                 "than trusting a stale price",
                                 r.slug, sig.side)
                        return None
                    if base + 1e-9 < _lo:
                        log.info("cross skip %s %s: venue %.4f is below the "
                                 "%.2f floor (our ladder read %.4f)",
                                 r.slug, sig.side, base, _lo, sig.quote)
                        with contextlib.suppress(Exception):
                            self._cross_refuse(
                                r, sig.side,
                                f"venue book {base:.3f} below the "
                                f"{_lo:.2f} floor", stake=stake, arm="cross")
                        return None
                if cap + 1e-9 < base:
                    with contextlib.suppress(Exception):
                        self.broker.last_failure = "above_fill_cap"
                    log.info("cross skip %s %s: %s %.4f is above the fill cap"
                             " %.2f -- a take at the cap cannot match",
                             r.slug, sig.side, base_src, base, cap)
                    with contextlib.suppress(Exception):
                        self._cross_refuse(
                            r, sig.side,
                            f"{base_src} book {base:.3f} is above the fill cap "
                            f"{cap:.2f}: a take at the cap cannot match",
                            stake=stake, arm="cross")
                    return None
                kw["max_px"] = cap
            fill = await self.broker.buy(slug=r.slug, side=sig.side, token_id=token,
                                         price=base, stake=stake, **kw)
            if fill:
                break

            # Only retry a failure the venue confirmed. Anything else -- a
            # timeout or a 5xx on place-order, a status we could not read --
            # may have left a live order behind, and placing a second one
            # would double the exposure. Brokers that report no reason (paper,
            # whose rejections are deterministic anyway) stop here too.
            reason = getattr(self.broker, "last_failure", None)
            # Declining a price is not a failed ORDER: nothing reached the
            # venue, and the next attempt re-quotes into a book that may have
            # come back inside the ceiling. It used to END the round right
            # here -- which is why the venue saw one order per failed trigger
            # while the log said "attempt 1/3; not retrying": the operator's
            # "retry doesn't work" report, 2026-09-15.
            if reason == "quote_above_ceiling":
                log.info("%s attempt %d/%d declined: quote above the ceiling "
                         "(price left the band)", arm_of(sig), attempt + 1,
                         attempts)
                continue
            # A FAK THAT FOUND NOTHING IS NOT A FAILED ORDER. It is the venue
            # saying "nothing at or below your cap", and on this arm the cap is
            # now derived from the venue's own ask -- so a miss means the book
            # moved between our fetch and the venue's match, which a bounded
            # re-price can answer. The machinery is the loop that already
            # exists: `order_retry_delay_s` between attempts, the
            # `order_min_tau_s` floor so an attempt never starts into a nearly
            # over round, a FRESH quote per attempt, and -- the one that
            # matters for exposure -- `_venue_position_tokens()` re-read before
            # every re-place, so an attempt that DID fill cannot be doubled.
            # ONLY on the take path (`limit_ceiling`): the model and impulse
            # arms pass no ceiling and keep the single-attempt behaviour.
            if (reason == "fak_unfilled" and limit_ceiling is not None
                    and attempt + 1 < attempts):
                log.info("%s FAK found no match at <= %.2f (attempt %d/%d): "
                         "re-pricing off the venue and re-trying", arm_of(sig),
                         kw.get("max_px"), attempt + 1, attempts)
                continue
            if reason not in RETRYABLE_FAILURES:
                log.error("%s order failed for %s %s (%s); not retrying",
                          arm_of(sig), r.slug, sig.side,
                          reason or "outcome unknown")
                break
            log.warning("%s order attempt %d/%d failed for %s %s (%s)",
                        arm_of(sig), attempt + 1, attempts, r.slug, sig.side,
                        reason)

        if not fill:
            # Back off rather than re-firing every tick: a rejection that
            # survives every attempt is almost always a persistent condition,
            # and retrying at 1 Hz just hammers the venue for the rest of the
            # round.
            #
            # A RESTING LIMIT THAT NOBODY CROSSED IS NOT THAT. It is the normal,
            # expected outcome of posting inside the spread -- the venue is
            # healthy and the next round may fill immediately. Pausing every arm
            # for order_cooldown_s on it cost ~150s on 2026-09-13 (3 unfills,
            # each 20s of wait plus 30s of pause) and, worse, the cooldown is
            # SHARED: an unfilled model-arm limit silently blocked the impulse
            # and cross arms, which fire in windows only seconds wide.
            #
            # `quote_above_ceiling` is the SAME class and was added 2026-09-15.
            # It is not a venue failure at all -- it is this bot refusing a
            # price. Measured live at 07:29:06 on eth-updown-5m-1789457100: the
            # book ran 0.53 -> 0.67 in six seconds, attempt 2's quote came back
            # over the ceiling, we correctly declined, and that refusal then
            # paused every family for 30s. Declining a bad price has to be free,
            # or the ceiling that protects the arm silently throttles it.
            no_cooldown = {"limit_unfilled", "quote_above_ceiling",
                           "quote_unfillable", "quote_stale",
                           "quote_price_missing",
                           # The entry-price floor refusing a degenerate price
                           # (broker.DEFAULT_MIN_ENTRY_PX, shipped after a
                           # 500-share fill at 1c). That refusal IS the fix
                           # working -- without this name a correctly-declined
                           # order would pause every family for 30 s.
                           "px_below_floor",
                           # The venue's 5-SHARE minimum would spend more than
                           # the stake cap, so no size both trades and fits
                           # (_size_above_cap). A BOUND refusing, not an order
                           # failing -- same reason as px_below_floor above.
                           "size_above_cap",
                           # A FAK/IOC order that finds nothing at or below its
                           # ceiling comes back as a REJECTION, not as an
                           # accepted order with zero fill. That is the venue
                           # saying "nothing there", which is a no-fill -- and
                           # without this name every miss would pause orders
                           # for 30 s, the exact coupling that made a tighter
                           # ceiling throttle the arm.
                           "fak_unfilled"}
            if getattr(self.broker, "last_failure", None) in no_cooldown:
                log.info("%s no fill for %s %s: %s (not a venue failure; "
                         "no cooldown)", arm_of(sig), r.slug, sig.side,
                         self.broker.last_failure)
                return fill
            self._order_cooldown_until = time.time() + settings.order_cooldown_s
            log.warning("%s order failed for %s %s after %d attempt(s); "
                        "pausing orders for %.0fs", arm_of(sig), r.slug, sig.side,
                        attempt + 1, settings.order_cooldown_s)
        return fill

    # -- settlement ---------------------------------------------------------
    async def _backfill_async(self) -> None:
        """`backfill_settlements` off the tick; see the call site in `run`.

        ⛔ IT MUST NOT RAISE INTO THE LOOP, and it must not die quietly either.
        A task that fails here stops settlement backfill for the rest of the
        process's life -- and the failure would be silent by construction:
        `live_round()` only ever returns the round that is CURRENTLY open, so by
        the time a round settles the feed has moved on, and this call is the only
        thing that ever attaches an outcome. Unlabelled rounds collect data that
        cannot be scored, which is the entire point of Phase 0. So the exception
        is caught and logged rather than propagated.

        The cost is reported with the tick's own threshold, so the two numbers
        are directly comparable in the log -- and the message says OFF the tick,
        because that is the difference that matters: it is no longer paid for
        with the cross arm's sampling window.
        """
        t0 = time.time()
        try:
            await self.backfill_settlements()
        except asyncio.CancelledError:
            raise
        except Exception as e:                          # noqa: BLE001
            log.exception("settlement backfill failed: %s: %s",
                          type(e).__name__, e)
        spent = time.time() - t0
        if spent * 1000.0 >= _SLOW_TICK_MS:
            log.warning("slow settlement backfill: %.2fs OFF the tick (the "
                        "cross arm kept sampling through it)", spent)

    def _settles_on_twap(self) -> bool:
        """Does the venue we are trading pay on a 60 s TWAP, or on a POINT?

        ⛔ THE TWO VENUES PAY ON DIFFERENT STATISTICS, and the cross arm's whole
        signal is built on the answer. `_cross_level` uses `twap_reference`
        (the 60 s mean at the OPEN) and `crossed_side` reads `fc` (the projected
        60 s mean) -- both correct for Polymarket, both WRONG on a point venue.
        Neither branched on venue before this, and both knobs default True, so
        moving the arm to Binance would have compared the wrong statistic
        against the wrong level. That is a signal error, not plumbing: no amount
        of venue-agnostic order routing fixes it.

        MEASURED against each venue's OWN recorded winner, on Binance 1-second
        bars (`tools/pm_venue_settlement.py`):

            era                        rounds  TWAP60vTWAP60  point vs strike
            BINANCE (CHAINLINK)          5924          90.4%           98.7%
            POLYMARKET (twap label)      4054          98.6%              --

        8.3 points apart on nearly six thousand rounds. The venue label in the
        `rounds` table says the same thing: `CHAINLINK` before 2026-09-16,
        `chainlink-twap-60s-vs-range-start` after.

        The KNOBS still win when they are off -- this can only ever turn the
        TWAP treatment OFF, never on -- so an operator who sets
        `CROSS_REF_TWAP=false` on Polymarket still gets the point behaviour.
        """
        return registry.normalise(
            getattr(settings, "venue", "binance")) == "polymarket"

    async def _venue_top(self, token: str, r):
        """The VENUE's own top of book, on whichever venue we are trading.

        ⛔ THE ARM MUST BEHAVE IDENTICALLY ON BOTH VENUES, and before this it
        could not. `venue_top()` is the Polymarket CLOB `/book` read, hardcoded.
        On Binance it simply fails, so `base_src` never became "venue" -- and
        `cross_ladder_band_pad`'s fail-closed branch refuses any order whose
        price it could not check against the real book. With the live
        `CROSS_LADDER_BAND_PAD=0.10`, switching `VENUE=binance` would therefore
        have refused EVERY cross order, silently, with the arm looking healthy.

        The two venues offer the same thing under different names:
            Polymarket   GET /book?token_id=            (venue_top)
            Binance      SAPI /w3w/wallet/prediction/order-book
                         via `LiveBroker.best_prices(token_id, market_id)`
        Both are a REST read of the book we PAY into, independent of our own
        websocket ladder -- which is the entire point of the re-base: it is the
        only source in the process that can disagree with us.

        Returns an object with `.bid`/`.ask` or None. NEVER raises: an
        unreadable venue book is a fallback, not an exception.
        """
        if registry.normalise(getattr(settings, "venue", "binance")) == "polymarket":
            return await venue_top(token)
        bp = getattr(self.broker, "best_prices", None)
        mid = getattr(r, "market_id", None)
        if bp is None or not mid or not token:
            return None
        try:
            got = await bp(str(token), int(mid))
        except Exception:                                # noqa: BLE001
            return None
        if not got:
            return None
        try:
            bid, ask = float(got[0]), float(got[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not (ask > 0):
            return None
        return SimpleNamespace(bid=bid, ask=ask)

    def _locked_tail_p(self, r, sym: str, tau: float):
        """P(Up) for a round whose TWAP window is partly PRINTED, or None.

        The venue pays `TWAP60(bell) >= TWAP60(open)`, so at `ts` with `tau`
        left the paid window is `[ts+tau-60, ts+tau]`:

            tau <  60   printed `[ts-(60-tau), ts]` + future `[ts, ts+tau]`
                        forecast = ((60-tau)*M_printed + tau*spot)/60
                        Var      = (tau/60)^2 * sigma^2 * tau/3
            tau >= 60   nothing printed; forecast = spot, and there is no edge
                        here (measured +$0.004/bet), so this returns None.

        ⛔ M_PRINTED IS THE RECENT (60-tau) SECONDS, NOT THE TRAILING MINUTE.
        `twap_forecast` averages `[ts-60, ts]` at every tau, so at tau=30 it
        averages 30 seconds the paid window will never contain. Measured MAE
        against the realised TWAP60(bell): 0.349 bps for this window against
        0.629 for the trailing minute at tau 5-30.

        `sigma` is fitted from THIS round's own recent tape rather than assumed,
        because the probability -- not the point forecast -- is what decides the
        trade, and the probability is all variance.
        """
        # ⛔ TWAP VENUES ONLY, AND DELIBERATELY SO. The whole premise is that
        # part of the paid 60 s window is ALREADY PRINTED, which is what makes
        # the outcome near-determined while the book still discounts it. A
        # point venue has no printed component: the payout is the bell price,
        # so the right model is P(S_bell >= K) = Phi((S-K)/(sigma*sqrt(tau))),
        # a DIFFERENT variance with a different shape.
        # The edge was measured on Polymarket only (+2.3%/bet, t+1.04 at round
        # level, 3 of 4 days negative -- see config.locked_tail_enabled). It is
        # unproven there and entirely unmeasured on a point venue, so this
        # refuses rather than extrapolating a thin edge onto a market whose
        # mechanism it was never tested against.
        if not self._settles_on_twap():
            return None
        lo = float(settings.locked_tail_tau_lo)
        hi = float(settings.locked_tail_tau_hi)
        if not (lo <= tau < hi) or tau >= TWAP_REF_LOOKBACK_S:
            return None
        feed = self.spots.get(sym)
        ticks = list(getattr(feed, "ticks", None) or []) if feed is not None else []
        if len(ticks) < 40:
            return None
        start_ms = int(getattr(r, "start_ms", 0) or 0)
        if not start_ms:
            return None
        level = twap_reference(ticks, start_ms)
        if not level or level <= 0:
            return None
        now_ms = int(ticks[-1][0])
        spot = float(ticks[-1][1])
        if spot <= 0:
            return None
        W = TWAP_REF_LOOKBACK_S
        lo_ms = now_ms - int((W - tau) * 1000.0)
        seg = [p for t, p in ticks if lo_ms <= t <= now_ms and p and p > 0]
        if len(seg) < 4:
            return None
        m_printed = sum(seg) / len(seg)
        fc = ((W - tau) * m_printed + tau * spot) / W
        # per-sample move of the recent tape, as the diffusion scale
        tail = [p for _t, p in ticks[-300:] if p and p > 0]
        if len(tail) < 30:
            return None
        d = [tail[i + 1] - tail[i] for i in range(len(tail) - 1)]
        sig = (sum(x * x for x in d) / len(d)) ** 0.5
        if sig <= 0:
            return None
        var = (tau / W) ** 2 * (sig ** 2) * (tau / 3.0)
        sd = max(var, 1e-18) ** 0.5
        z = (fc - level) / sd
        p_up = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        return p_up, level, fc

    async def _locked_tail_tick(self) -> None:
        """Buy the near-certain favourite while the book still discounts it.

        Ships OFF, and SHADOW by default -- it logs what it would have done so
        the live opportunity rate can be measured before anything is risked.
        See config.locked_tail_enabled for the measurement and for why the
        round-level t (+1.04) is the number that matters, not the tick-level
        one (+6.31) that the same data produces if rounds are double-counted.
        """
        if not settings.locked_tail_enabled:
            return
        wsb = getattr(self, "ws_book", None)
        if wsb is None:
            return
        now_ms = int(time.time() * 1000)
        for prefix, sym, _ in active_markets():
            r = (self._mkt or {}).get(prefix)
            if r is None or getattr(r, "strike", None) is None:
                continue
            tau = (int(getattr(r, "end_ms", 0) or 0) - now_ms) / 1000.0
            got = self._locked_tail_p(r, sym, tau)
            if got is None:
                continue
            p_up, level, fc = got
            side = "Up" if p_up >= float(settings.locked_tail_min_p) else (
                "Down" if (1.0 - p_up) >= float(settings.locked_tail_min_p) else None)
            if side is None:
                continue
            p_side = p_up if side == "Up" else (1.0 - p_up)
            top = wsb.best(r, settings.ws_book_max_age_s)
            if top is None:
                continue
            ask = top.ask(side) if callable(getattr(top, "ask", None)) else None
            if ask is None or not (0.01 < ask <= float(settings.locked_tail_max_px)):
                continue
            fee = 0.07 * ask * (1.0 - ask)
            if (p_side - ask - fee) < float(settings.locked_tail_min_edge):
                continue
            fired = getattr(self, "_tail_fired", None)
            if not isinstance(fired, set):
                fired = self._tail_fired = set()
            if r.slug in fired:
                continue
            fired.add(r.slug)
            if len(fired) > 256:
                fired.clear()
                fired.add(r.slug)
            log.info("LOCKED TAIL%s %s %s: P=%.4f ask=%.4f edge=%+.4f "
                     "tau=%.0fs fc=%.4f level=%.4f",
                     " (shadow)" if settings.locked_tail_shadow else "",
                     r.slug, side, p_side, ask, p_side - ask - fee, tau, fc, level)
            if settings.locked_tail_shadow:
                continue
            up = next((o for o in r.outcomes if o.name == "Up"), None)
            dn = next((o for o in r.outcomes if o.name == "Down"), None)
            token = (up.token_id if side == "Up" else dn.token_id) if (up and dn) else None
            stake = self._ck("stake_usdt", r) or settings.min_order_usdt
            if not token or self.bankroll < stake or self.broker.mode != "live":
                continue
            with contextlib.suppress(Exception):
                await self.broker.buy(slug=r.slug, side=side, token_id=token,
                                      price=float(ask), stake=float(stake),
                                      max_px=float(settings.locked_tail_max_px))

    def _settled_winner(self, r, sym: str):
        """The winner of a FINISHED round, from our own spot tape, or None.

        Returns `(side, margin_bps)`. None when the tape cannot cover either
        60 s window -- which is the honest answer, not a guess: the whole trade
        rests on this being right, and it reproduces the venue's settlement on
        98.6% of rounds ONLY when both windows are actually printed.
        """
        feed = self.spots.get(sym)
        ticks = list(getattr(feed, "ticks", None) or []) if feed is not None else []
        if len(ticks) < 8:
            return None
        end_ms, start_ms = int(r.end_ms or 0), int(r.start_ms or 0)
        if not end_ms or not start_ms:
            return None
        if self._settles_on_twap():
            lvl = twap_reference(ticks, start_ms)
            fin = twap_reference(ticks, end_ms)
        else:
            # ⛔ POINT VENUE. Binance settles the BELL PRICE against the open,
            # not a 60 s mean against a 60 s mean. Measured against each
            # venue's own recorded winner on Binance 1-second bars
            # (tools/pm_venue_settlement.py):
            #     BINANCE     n=5924  TWAP60 90.4%   point-vs-strike 98.7%
            #     POLYMARKET  n=4054  TWAP60 98.6%
            # Reconstructing this trade's whole justification with the wrong
            # rule would be wrong on ~1 round in 10 -- and the entire edge is
            # that we KNOW the outcome the book has not priced yet.
            lvl = float(getattr(r, "strike", 0) or 0) or None
            fin = None
            for ts, px in reversed(ticks):
                if ts <= end_ms and px and px > 0:
                    fin = float(px)
                    break
        if not lvl or not fin or lvl <= 0:
            return None
        margin = 1e4 * (fin - lvl) / lvl
        return ("Up" if fin >= lvl else "Down"), margin

    async def _post_close_scan(self) -> None:
        """Buy a winner the book has not finished repricing. See config.

        ⛔ EVERY GUARD HERE IS LOAD-BEARING and each one killed a phantom
        version of this trade in testing:
          * the round must be FINISHED -- before the bell this is just a
            directional bet at a bad price;
          * our reconstruction must clear `post_close_min_margin_bps`, because
            the 1.4% of rounds we call wrong are concentrated in near-ties;
          * the book must be FRESHER than `post_close_max_book_age_ms`. This is
            the one that matters most: the complete-set arb looked real on the
            same tape and went to exactly ZERO instances once a fresh book was
            required. A stale frame is how a free lunch gets invented;
          * one buy per round, and never above `post_close_max_px`.
        """
        window = float(settings.post_close_scan_s)
        if window <= 0 or self.broker is None or self.broker.mode != "live":
            return
        pend = getattr(self, "_post_close", None)
        if not isinstance(pend, dict) or not pend:
            return
        now_ms = int(time.time() * 1000)
        wsb = getattr(self, "ws_book", None)
        for slug, (r, sym) in list(pend.items()):
            age_s = (now_ms - int(r.end_ms or 0)) / 1000.0
            if age_s < 0 or age_s > window:
                pend.pop(slug, None)
                continue
            got = self._settled_winner(r, sym)
            if got is None:
                continue
            side, margin = got
            if abs(margin) < float(settings.post_close_min_margin_bps):
                continue
            if wsb is None:
                continue
            top = wsb.best(r, float(settings.post_close_max_book_age_ms) / 1000.0)
            if top is None:
                continue
            ask = top.ask(side) if callable(getattr(top, "ask", None)) else None
            if ask is None or not (0.01 < ask <= float(settings.post_close_max_px)):
                continue
            up = next((o for o in r.outcomes if o.name == "Up"), None)
            dn = next((o for o in r.outcomes if o.name == "Down"), None)
            token = (up.token_id if side == "Up" else dn.token_id) if (up and dn) else None
            if not token:
                continue
            stake = self._ck("stake_usdt", r) or settings.min_order_usdt
            if self.bankroll < stake:
                continue
            pend.pop(slug, None)                       # one attempt per round
            log.info("POST-CLOSE %s %s: settled %+.2f bps past the level "
                     "%.1fs ago, book still asks %.4f -- taking it",
                     slug, side, margin, age_s, ask)
            try:
                fill = await self.broker.buy(
                    slug=slug, side=side, token_id=token,
                    price=float(ask), stake=float(stake),
                    max_px=float(settings.post_close_max_px))
            except Exception as e:                       # noqa: BLE001
                log.warning("post-close buy failed %s: %s: %s", slug,
                            type(e).__name__, e)
                continue
            if fill:
                log.info("POST-CLOSE FILL %s %s @ %.4f", slug, side,
                         getattr(fill, "price", ask))

    async def _housekeeping_async(self) -> None:
        """`settle_due` + `sweep_claims` + the bankroll re-read, OFF the tick.

        ⛔ THE LARGEST REMAINING STALL, and like the backfill before it the
        instrument named it rather than a theory. Over 121 slow ticks:

            claims    n=121  median 271ms  p90 8939ms  max 31822ms  total 472.3s
            settle    n=121  median   0ms  p90 4250ms  max 22859ms  total 171.0s
            bankroll  n=115  median   0ms  p90  262ms  max   291ms  total  11.0s

        643 seconds of the loop's life inside bookkeeping that DECIDES NOTHING.
        None of it places an order, and every second of it is a second `_idle`
        does not sample the cross arm: the budget at the bottom of `run` is
        `max(0.05, 1.0 - elapsed)`, so the 31.8 s claim sweep cost the arm its
        whole ~960 ms window -- and left any open position UNMANAGED, because
        `_cross_exit_check` runs from the tick too. A stall is therefore also a
        window in which a losing position cannot be cut. Against
        `cross_max_tau_s=240` on a 300 s round, the worst tick is 13% of the
        tradeable window.

        ONE TASK, NOT THREE, because the ORDER is load-bearing. `settle_due`
        redeems on the fast path and `sweep_claims` is its retry clock, so the
        sweep must run after the settle it retries or it fires a duplicate
        against a claim already in flight ("already have a pending claim", 3x
        on 09-14). `refresh_bankroll_if_stale` stays last for the reason it is
        last today: it reads equity after the settlement has moved it.

        ⭐ CANCEL-SAFE AT EVERY AWAIT, which is what makes dispatching a MONEY
        path acceptable. `settle_due` books a settlement in a contiguous
        SYNCHRONOUS run -- DB insert, `guard.on_settle`, `pnl.add`, `bankroll
        +=`, `del open_fills[fid]` -- with no await inside it. So a cancel can
        only land in one of three places:
          * before `broker.settle` returns -- nothing booked, the fill is still
            open, and the next tick retries it;
          * after that block, at `broker.redeem()` -- fully booked, only the
            claim is lost, and `sweep_claims` is precisely the retry clock for
            a dropped claim;
          * at the trailing `_refresh_bankroll` -- booked, and the 60 s clock
            in `refresh_bankroll_if_stale` re-reads it.
        No interleaving double-counts a settlement or leaks a slot. The
        single-flight guard at the call site rules out the other half of that
        risk: two overlapping `settle_due`s walking the same `open_fills` WOULD
        double-book, and at a 1 s poll against a 22.9 s settle there would have
        been twenty-two of them.

        It must not raise into the loop and must not die quietly -- same
        contract as `_backfill_async`, and worse consequences: a task that
        died here would stop settling positions entirely.
        """
        t0 = time.time()
        try:
            await self.settle_due()
            # Claims the venue rejected at settlement are retried on this
            # clock, not on the next win (see sweep_claims).
            await self.sweep_claims()
            # AND THE SAME FOR THE BANKROLL. `settle_due` re-reads equity too,
            # but only when a position settles -- so a bot that has STOPPED
            # trading never re-reads, and an arm locked out by the exposure cap
            # (a fraction of that stale number) cannot trade its way out of it.
            # Measured 2026-09-17: a $5.00 deposit sat invisible for four hours
            # behind exactly that loop. Self-guarded on a 60 s clock.
            await self.refresh_bankroll_if_stale()
        except asyncio.CancelledError:
            raise
        except Exception as e:                          # noqa: BLE001
            log.exception("housekeeping failed: %s: %s", type(e).__name__, e)
        spent = time.time() - t0
        if spent * 1000.0 >= _SLOW_TICK_MS:
            log.warning("slow housekeeping: %.2fs OFF the tick (the cross arm "
                        "kept sampling and the exit gate kept running)", spent)

    async def backfill_settlements(self, limit: int = 5) -> int:
        """Fetch outcomes for rounds that have ended but have no recorded winner.

        `live_round()` only ever returns the round that is currently open, so by
        the time round N settles the feed has already moved to N+1. Without this
        backfill, recorded quotes never get an outcome attached and Phase 0
        collects data that cannot be scored -- which is the entire point of it.
        Runs in every mode, including `record`.
        """
        now_ms = int(time.time() * 1000)
        pending = self.store.unsettled_rounds(before_ms=now_ms - 10_000)
        by_slug = getattr(self.market, "by_slug", None)
        done = 0
        # The budget counts ATTEMPTS, not rows, and the skip tests below are
        # free. Counting a skipped row against `limit` lets a few
        # unaddressable rounds occupy the whole window forever, because
        # `unsettled_rounds` is ordered OLDEST FIRST and they never leave it.
        # Measured 2026-09-16, the day this bot moved to Polymarket: five
        # Binance-era rounds (16:25) sat at the head of an 18-row list, so
        # `pending[:5]` was the same five rows on every call and not ONE of the
        # 13 Polymarket rounds was ever labelled -- `grep -c 'settled '` was 0,
        # with the resolution call itself working perfectly.
        attempted = 0
        for row in pending:
            if attempted >= limit:
                break
            tid = row["market_topic_id"]
            # A round carries the addressing key of the venue that WROTE it:
            # the wrapper's integer topic id, or nothing at all for a round
            # discovered by slug. Handing the wrong key to the wrong feed does
            # not fail loudly, it fetches an UNRELATED market (see
            # PmMarketFeed.by_topic_id) -- so skip it rather than resolve it
            # wrongly. Settlement is the one place a silently wrong answer
            # costs money.
            if by_slug is not None and tid:
                continue
            if not tid and by_slug is None:
                continue
            attempted += 1
            fresh = await self._resolve_round(row)
            if fresh and fresh.winner_name:
                self.store.upsert_round(round_row(fresh))
                # Stamp WHEN the outcome was learned. The column has always
                # existed and was never written, so every settled round in the
                # record read NULL -- an audit trail missing the one timestamp
                # that says how fresh the settlement is.
                self.store.conn.execute(
                    "UPDATE rounds SET settled_at_ms=? WHERE slug=?",
                    (int(time.time() * 1000), fresh.slug))
                self.store.conn.commit()
                done += 1
                log.info("settled %s -> %s (end_price=%s)",
                         fresh.slug, fresh.winner_name, fresh.end_price)
        return done

    async def _resolve_round(self, row):
        """Re-fetch a round to collect its outcome, by whichever key its VENUE
        uses.

        The wrapper addresses a round by `marketTopicId`; Polymarket addresses
        it by slug (gamma `?slug=&closed=true`). The two are NOT
        interchangeable, and a Polymarket round carries no Binance topic id --
        so asking the wrapper path for topic 0 would fetch an unrelated market
        and then book ITS winner against a real position. Settlement is the one
        place where a silently wrong answer costs money, so the key is chosen
        by capability rather than by configuration.
        """
        by_slug = getattr(self.market, "by_slug", None)
        slug = row["slug"] if "slug" in row.keys() else ""
        if by_slug is not None and slug:
            return await by_slug(slug)
        return await self.market.by_topic_id(row["market_topic_id"])

    def _pm_remember(self, r) -> None:
        """Keep the Polymarket broker's per-round cache current.

        `PmBroker` caches a round's token ids, tick and min order so the ORDER
        path makes NO discovery call (each clob.polymarket.com call costs
        ~260 ms, against a whole edge worth a few cents). That cache is keyed
        by slug, and it has to follow the ROLL: booting remembers only the
        rounds that were live then, so without this the next round could not be
        priced at all -- `buy_limit` would find no tokens for the slug the arm
        is trading, at exactly the moment the arm wants to fire.

        A no-op on the Binance path, where `self.market` has no
        `pm_round_for`.
        """
        br = getattr(self, "_pm_broker", None)
        getter = getattr(self.market, "pm_round_for", None)
        slug = getattr(r, "slug", "") if r is not None else ""
        if br is None or getter is None or not slug or slug in self._pm_seen:
            return
        pmp = getter(slug)
        if pmp is None:
            return
        self._pm_seen.add(slug)
        try:
            br.remember(pmp)
        except Exception as e:                       # noqa: BLE001
            log.warning("could not remember %s for the PM broker (%s: %s)",
                        slug, type(e).__name__, e)

    def _pm_window_log_int(self, r, symbol: str,
                           tau: float) -> float | None:
        """`J`, the realised part of the Polymarket settlement window.

        Polymarket settles on a TWAP, so the round's average SO FAR is a state
        variable, and near the bell it is the only one that matters -- the
        answer is the sign of `J`. The point rule has no equivalent and the
        models fitted on it cannot see this, which is why `TwapModel` needs it
        and why `None` (an assumption) is a different thing from a number.

        Two cheap guards, both deliberate:
          * `tau >= TWAP_LOOKBACK_S` means the window has not even opened, so
            there is nothing realised to report. Regime 1 of the distribution
            needs no `J` -- a return of 0 here would be a WRONG anchor, not a
            neutral one.
          * `self._ticks` is the ~1 Hz poll buffer (2400 samples). It is keyed
            by Binance symbol and appended once per poll for every symbol in
            MARKETS, so its span is fixed by the poll cadence and covers the
            whole round; `feed.ticks` is a high-rate buffer whose span depends
            on the symbol's burst rate, which is not a property a window of a
            stated length can be built on.

        Returns None on the Binance path, where the window has no meaning.
        """
        if settings.venue != "polymarket" or r is None:
            return None
        if tau >= TWAP_LOOKBACK_S or not getattr(r, "strike", None):
            return None
        end_ms = getattr(r, "end_ms", None)
        if end_ms is None:
            return None
        ticks = (getattr(self, "_ticks", None) or {}).get(symbol)
        if not ticks:
            return None
        return window_log_integral(
            ticks, r.strike,
            open_ms=float(end_ms) - TWAP_LOOKBACK_S * 1000.0,
            close_ms=float(end_ms) - max(tau, 0.0) * 1000.0)

    def _pm_strike_backfill(self, slug: str) -> float | None:
        """The round's START price, taken from the live spot tape.

        WHY THIS EXISTS. Polymarket lists sol, xrp and doge up/down markets and
        this bot can order in them, but the STRIKE had no source: Binance's
        prediction feed (which supplies `variantData.startPrice`) carries
        btc/eth/bnb up/down only, and gamma carries no reference price at all
        (its resolution source is a Chainlink stream URL). A round with no
        strike never reaches `_mkt`, so those families were discovered, cached
        and then never evaluated -- `9/9 cached, 6 live`, with the three missing
        ones showing up nowhere else, because a refusal is not an error.

        WHAT THE NUMBER MUST BE. The SPOT PRICE AT THE START INSTANT, not a
        window average. Measured against 2,120 BTC / 1,553 ETH / 962 BNB rounds
        with known strikes: the 1 s close at T-1 reproduces it to **0.042/0.091/
        0.114 bps** (p90 <= 0.07, 99% under 1 bp), while a 60 s TWAP is off by
        1.42-2.13 bps mean and **8-10 bps p99** -- the size of the whole 10 bps
        cross gate, so a TWAP strike would manufacture phantom crossings. A
        websocket tick is FINER than the 1 s bar the 0.04 bps figure was
        measured on, so this is the more accurate of the two sources.

        `SpotFeed.ticks` is the tape: 40,000 updates in arrival order with the
        clock reading at which each was seen -- ~5 minutes at BTC's ~114/s, so
        it still brackets a round's open even when the round is only picked up
        a minute or two late. (It was 4,000 = ~35 s until 2026-09-17, which is
        why `sol/xrp/doge` could not be priced whenever discovery lagged: the
        ticks from before the open had already been evicted.) It is maintained
        for every symbol in MARKETS, so this costs no network and no new buffer.

        FAILS CLOSED, and both conditions are load-bearing:
          * the round must have OPENED (`start_ms <= now`). Before that the
            newest tick is a pre-roll price, and the strike is the level the
            round is settled against -- a price from before the round is not
            that level. The caller retries on `recheck_s`, so this self-heals
            within seconds rather than being cached as a miss.
          * the newest tick at or before the open must be within
            `_PM_STRIKE_TICK_TOL_MS`. Otherwise the tape stalled, or this
            process booted after the open, and NO strike is the honest answer.
            (The wrapper-first order means btc/eth/bnb keep their exact oracle
            value; this only ever supplies the families it cannot.)
        """
        prefix = market_prefix(slug or "")
        symbol = SYMBOL_BY_PREFIX.get(prefix or "")
        if not symbol:
            return None
        try:
            start_ms = int(slug.rsplit("-", 1)[1]) * 1000
        except (IndexError, ValueError, AttributeError):
            return None
        if start_ms > int(time.time() * 1000):
            return None                     # the round has not opened yet
        feed = (getattr(self, "spots", None) or {}).get(symbol)
        ticks = getattr(feed, "ticks", None) if feed is not None else None
        if not ticks:
            return None
        # Append-ordered, so the newest tick at or before the open is the first
        # one found scanning backwards. Bounded by the deque's own maxlen.
        for ts_ms, px in reversed(ticks):
            if ts_ms > start_ms:
                continue
            if start_ms - ts_ms > _PM_STRIKE_TICK_TOL_MS or not px:
                return None
            return float(px)
        return None                         # nothing predates the open

    async def _pm_strike_backfill_kline(self, slug: str) -> float | None:
        """The round's START price from Binance's 1 s kline -- after a RESTART.

        ⛔ THE HOLE THIS CLOSES. `_pm_strike_backfill` reads the newest tick at
        or before the open off the LIVE spot tape, and fails closed when there
        is none -- which is right, because a price from before the round is not
        the level the round is settled against. But a process that BOOTS after
        the open has an EMPTY tape for that round, so the round is unfireable
        for its whole life, and `_cross_tick` returned on `r.strike is None`
        without a line -- so it read as "no crossing".

        MEASURED 2026-09-17: the 10:59:43 -> 11:00:01 restart (systemd takes
        ~18 s, so the process was DOWN across the open) opened exactly one
        round across the gap, `sol-updown-5m-1789642800` at 11:00:00. It was
        the ONLY round of the 63 since 10:30 with `strike IS NULL`, and it
        settled Up -- a round this arm could not act on and did not mention.

        THE 1 s CLOSE AT T-1 IS THE SAME NUMBER, and that is measured, not
        assumed: against 2,120 BTC / 1,553 ETH / 962 BNB rounds with known
        strikes it reproduces the strike to **0.042 / 0.091 / 0.114 bps**
        (p90 <= 0.07, 99% under 1 bp). That is the same figure the tape-based
        backfill is justified by -- and unlike the tape, a REST kline is
        RETROSPECTIVE: it does not care when this process started.

        FAILS CLOSED on everything else. The bar must be EXACTLY the one ENDING
        at the open (openTime == start_ms - 1000), so a gap, a wrong interval or
        a shifted bar yields NO strike rather than a plausible one. A wrong
        strike does not raise; it trades.
        """
        prefix = market_prefix(slug or "")
        symbol = SYMBOL_BY_PREFIX.get(prefix or "")
        if not symbol:
            return None
        try:
            start_ms = int(slug.rsplit("-", 1)[1]) * 1000
        except (IndexError, ValueError, AttributeError):
            return None
        if start_ms > int(time.time() * 1000):
            return None                     # a pre-open ask cannot be answered
        try:
            bars = await fetch_klines(self.session, "1s", 3,
                                      symbol=symbol, end_ms=start_ms)
        except Exception as exc:                          # noqa: BLE001
            log.warning("strike kline backfill failed for %s: %s: %s",
                        slug, type(exc).__name__, exc)
            return None
        want = start_ms - 1000
        for b in bars or ():
            try:
                if int(b[0]) != want:
                    continue
                px = float(b[4])
            except (TypeError, ValueError, IndexError):
                continue
            if px > 0:
                log.info("strike recovered for %s from the 1s kline: %.6f "
                         "(the round opened across a restart, so the spot "
                         "tape cannot reach it)", slug, px)
                return px
        return None

    async def _setup_polymarket(self) -> None:
        """Build the Polymarket book + broker for the current and next round.

        DISCOVERY IS OUT OF THE ORDER PATH ON PURPOSE. Every
        clob.polymarket.com call costs ~260 ms whatever our box, our IP family
        or how warm the connection is, and the order itself needs only two of
        them (sign + post). So gamma discovery, token ids and the venue's own
        tick/min-size are resolved HERE, once per roll, and cached -- see
        `PmBroker`'s four rules.

        A missing private key is not an error: it means we cannot sign, and
        the rest of the bot already knows how to fall back to displayed
        prices.
        """
        from btcpredict.feeds.market_feed import MarketFeed
        from btcpredict.venues.polymarket import markets as pm
        from btcpredict.venues.polymarket.book import PmBook, PmBookView
        from btcpredict.venues.polymarket.feed import PmMarketFeed

        if not registry.pm_credentials_present(settings):
            log.warning("venue=polymarket but POLYMARKET_PRIVATE_KEY is missing "
                        "(L2 creds alone cannot sign an order); falling back "
                        "to displayed prices")
            return

        # The round source, swapped for this venue. `MarketFeed` is kept as the
        # STRIKE provider only: Polymarket publishes no reference price for this
        # family, and both rules compare against the price at the START of the
        # range, so `variantData.startPrice` is the right number for both.
        # `strike_backfill` covers the families that provider does NOT carry
        # (sol/xrp/doge) -- see `_pm_strike_backfill` for why it is not a
        # second-best source for them but the only one.
        self.market = PmMarketFeed(session=self.session,
                                   strike_feed=MarketFeed(self.session),
                                   strike_backfill=self._pm_strike_backfill,
                                   strike_backfill_async=(
                                       self._pm_strike_backfill_kline))

        # EVERY traded family, not just the default series. The venue lists
        # eth/bnb 5m and 15m alongside BTC's (verified live: gamma returns a
        # market for both `eth-updown-5m` and `bnb-updown-5m`), and the arms
        # trade all six. Discovering only the default series is what left five
        # families with no round and no book -- `no fresh websocket book
        # off-primary`, 34 of them in one evening.
        prefixes = [p for p, _, _ in MARKETS]
        found = await self.market.rounds_by_prefixes(prefixes)
        rounds = [r for r in found.values() if r is not None]
        if not rounds:
            log.warning("polymarket: no open round discovered for any of %s",
                        ", ".join(prefixes))
            return

        # THE BOOK IS THE ARMS' PRICE SOURCE, not decoration. It is wired into
        # `ws_book` -- the slot every arm reads through -- via an adapter that
        # resolves each side to its OWN token, because Polymarket has a
        # separate book per outcome where the wrapper had one covering both.
        # Left unwired, this socket connects, ingests ~680 messages/s and is
        # read by nothing: every arm silently falls back to displayed prices.
        book = PmBook()
        view = PmBookView(book)
        view.follow(rounds)
        self._pm_book = book
        self._pm_view = view
        self.ws_book = view
        self._pm_book_task = asyncio.create_task(book.run())

        # `make_pm_bundle` returns a VenueBundle (name + broker + book + note),
        # not the broker itself.
        bundle = registry.make_pm_bundle(settings=settings, book=book,
                                         log=log.info)
        br = bundle.broker
        self._pm_broker = br
        for r in rounds:
            self._pm_remember(r)
        # The signer's first call costs ~500 ms. Pay it at boot or pay it on
        # the first order, where it is worth more than the whole edge.
        cold_ms = br.prime()
        bal = await br.equity()
        now_ms = int(time.time() * 1000)
        log.info("POLYMARKET venue live: %d famil(ies) %s | collateral $%.4f "
                 "| signer prime %.0f ms", len(rounds),
                 ",".join(sorted(found)), bal, cold_ms)
        for pfx in sorted(found):
            rr = found[pfx]
            log.info("POLYMARKET round %-15s %s  tau %.0fs",
                     pfx, rr.slug, rr.tau_s(now_ms))
        self.book = br

    async def _refresh_bankroll(self) -> None:
        """Re-read wallet equity so percentage sizing compounds intraday.

        `setup` reads the balance once, and without this the stake stays frozen
        at whatever the account held when the process started: after a winning
        run the bot would keep betting 4% of the ORIGINAL account, and after
        losses it would keep betting the same dollars against a smaller one --
        at a $76 start, a slide to $40 would have made the "4%" stake 7.6% of
        what was left, which is the exact failure mode fraction sizing exists
        to prevent.

        Used to be gated on `not self.open_fills`, because `walletBalance`
        excludes cash locked in an open position and reading it mid-round would
        UNDERSTATE equity and shrink the next stake. That gate is gone with the
        move to `equity()`, which adds `positionValue` and
        `totalClaimableAmount` back -- an open position is now counted at its
        value rather than looking like a hole in the account.

        The read is still a RECONCILIATION, not the answer -- see
        _reconcile_bankroll -- but the artefact it defends against is now much
        rarer, so a persistent gap is far more likely to be real.
        """
        if self.mode != "live" or getattr(self, "book", None) is None:
            return
        try:
            # `refresh=True`: the Polymarket broker serves `equity()` from a
            # cache that only SPENDING invalidates, so without this a deposit is
            # invisible while the bot is idle (measured 2026-09-17: $5.00 landed
            # and the bot sized off the old number for four hours). `LiveBroker`
            # accepts the kwarg and ignores it -- its read is live per call.
            bal = await self.book.equity(refresh=True)
        except Exception as e:          # never let a read failure stop trading
            log.warning("bankroll refresh failed: %s: %s", type(e).__name__, e)
            return
        if bal <= 0:
            return                      # unreadable
        self._reconcile_bankroll(bal)

    def _reconcile_bankroll(self, read: float) -> None:
        """Adopt the venue's balance only when it is not a lag artefact.

        The venue's walletBalance does NOT track equity in real time: a stake
        leaves the wallet the instant the order fills, while the payout only
        lands after the redemption settles on-chain. Measured on a win of
        $0.4576 on a $1.53 stake, the balance read went $76.5027 -> $74.9727,
        i.e. down by EXACTLY the stake, with the payout simply absent.

        Sizing off that number shrinks the stake after every win -- the opposite
        of compounding, and it would have quietly capped the account's growth.
        So the bot keeps its own ledger (updated by settle_due) and only accepts
        the venue's figure when it disagrees across several reads in a row, which
        is what a deposit, a withdrawal or unmodelled fees look like. A lag
        resolves within a read or two and never gets that far.
        """
        tol = max(0.50, 0.01 * abs(self.bankroll))
        if abs(read - self.bankroll) <= tol:
            self._bankroll_drift = 0
            return
        if read > self.bankroll:
            # Money we did not account for: a deposit, or winnings the ledger
            # never saw. Nothing is lost by taking it immediately.
            log.info("bankroll $%.4f -> $%.4f (venue reports more than the "
                     "ledger; adopting)", self.bankroll, read)
            self.bankroll = read
            self._bankroll_drift = 0
            return
        self._bankroll_drift += 1
        if self._bankroll_drift < _BANKROLL_DRIFT_READS:
            log.info("bankroll read $%.4f is below the ledger $%.4f "
                     "(%d/%d reads; a redemption has not landed yet, keeping "
                     "the ledger for sizing)", read, self.bankroll,
                     self._bankroll_drift, _BANKROLL_DRIFT_READS)
            return
        log.warning("bankroll ledger $%.4f -> $%.4f (venue lower for %d reads "
                    "in a row: withdrawal or unmodelled fees, not lag)",
                    self.bankroll, read, self._bankroll_drift)
        self.bankroll = read
        self._bankroll_drift = 0

    def _fill_mode(self, fid: int) -> str:
        """The mode a FILL was booked under -- not the broker's mode.

        These used to be the same thing and no longer are: the double-down goes
        through the LIVE broker (real money) but is booked as mode='double', so
        settling it under the broker's mode would pool its P&L into the live
        book. That would make the experiment impossible to judge on its own
        ledger, which is the only reason it has its own cap.
        """
        try:
            row = self.store.conn.execute(
                "select mode from fills where id=?", (fid,)).fetchone()
        except Exception as e:                       # noqa: BLE001
            log.warning("could not read the mode of fill %s (%s); settling "
                        "under the broker's mode", fid, type(e).__name__)
            return self.broker.mode
        return row[0] if row and row[0] else self.broker.mode

    async def settle_due(self) -> None:
        now_ms = int(time.time() * 1000)
        settled = False
        for fid, (slug, fill) in list(self.open_fills.items()):
            row = self.store.get_round(slug)
            if not row or row["end_ms"] > now_ms - 5_000:
                continue
            winner = row["winner"]
            if not winner:
                fresh = await self._resolve_round(row)
                if fresh:
                    self.store.upsert_round(round_row(fresh))
                    winner = fresh.winner_name
            if not winner:
                continue      # not resolved yet; try again next tick
            s = await self.broker.settle(fill, winner)
            if s is None:
                continue      # unverified and still unreadable; retry next tick
            self.store.insert("settlements", {
                "fill_id": fid, "slug": slug, "ts_ms": s.ts_ms,
                "won": int(s.won), "payout": s.payout, "pnl": s.pnl,
                "mode": self._fill_mode(fid), "void": int(s.void),
            })
            settled = True
            if s.void:
                # The venue never filled the order: no stake was deployed, so
                # there is no PnL, no guard update, and nothing to redeem.
                log.warning("SETTLE %s VOID: order never filled at the venue "
                            "(phantom fill dropped)", slug)
                # Give the slot back. _book_fill claimed it before the venue
                # disowned the order, and skipping this leaked it for the life
                # of the process -- one void was enough to silence the main
                # arm, whose pool is a set of size max_open_rounds. Not
                # on_settle: there is no P&L and no loss to record.
                reserved, pool = pool_for_mode(self._fill_mode(fid))
                self.guard.release(slug, reserved=reserved, pool=pool)
                del self.open_fills[fid]
                continue
            # Release the slot this fill is actually holding. Passing the main
            # the main one for a cross fill would leave `reserved_rounds` occupied
            # forever and the reserved slot permanently unavailable.
            reserved, pool = pool_for_mode(self._fill_mode(fid))
            self.guard.on_settle(slug, s.pnl, reserved=reserved, pool=pool)
            self.pnl.add(s.pnl, fill.stake, fill.fee_paid, s.won)
            # The LEDGER, moved immediately and exactly: the stake has already
            # left the wallet and the payout is on its way, so equity changes by
            # exactly the settlement P&L. Waiting for the venue to agree is what
            # made a win look like a loss (see _reconcile_bankroll).
            self.bankroll += s.pnl
            del self.open_fills[fid]
            # Live winnings sit as a claimable position until redeemed; they do
            # not credit the wallet on their own. This is the FAST path -- the
            # venue sometimes rejects a not-yet-final position here, and
            # sweep_claims() is the retry clock for whatever it drops.
            if s.won and self.broker.mode == "live":
                claimed = await self.broker.redeem()
                log.info("redeem -> %s", str(claimed)[:200])
            log.info("SETTLE %s %s winner=%s pnl=%+.4f | running: %s",
                     slug, "WON" if s.won else "LOST", winner, s.pnl, self.pnl)
        if settled:
            # A position closed, so the wallet balance is readable as equity
            # and the next stake should track it (see _refresh_bankroll).
            await self._refresh_bankroll()

    async def sweep_claims(self) -> None:
        """Retry winnings claims on a clock, not only on a win.

        `settle_due` redeems the moment a WON fill settles -- but that is the
        only trigger, so a claim that FAILS there is not retried until the
        next win. Both failure modes are on the record (2026-09-14): the
        venue rejects a position that is not yet final (`-9000 SYSTEM_ERROR`
        -- the claim call that fired 6 seconds after the first 15m round
        ended returned `failed_tokens: 1`, exactly this trap), and it rejects
        a claim already in flight ("already have a pending claim", 3x at
        19:25). Whatever the cause, the winnings sat claimable until
        something WON again: hours on a losing stretch, overnight when idle.

        The sweep is wallet-wide by construction: `redeem()` asks the venue
        what is CLAIMABLE (every family, every cause -- it also picks up
        claims a restart stranded) and returns without POSTing when the
        answer is empty, so the steady-state cost is one read per cooldown.
        The on-win redeem stays: it is the fast path when the venue accepts.
        """
        broker = getattr(self, "broker", None)
        if broker is None or broker.mode != "live":
            return
        now = time.time()
        if now - getattr(self, "_last_claim_sweep", 0.0) < _CLAIM_SWEEP_S:
            return
        self._last_claim_sweep = now
        claimed = await broker.redeem()
        # `redeem()` returns a DICT and a dict carrying no redemptions is TRUTHY,
        # so `if claimed:` printed `{'redeemed': [], 'reason':
        # 'nothing_redeemable'}` once a minute forever -- measured 64 of the
        # ~1,600 lines in a 3-hour window, every one saying nothing happened.
        got = claimed.get("redeemed") if isinstance(claimed, dict) else claimed
        if got:
            log.info("claim sweep -> %s", str(claimed)[:160])
        elif claimed:
            log.debug("claim sweep -> %s", str(claimed)[:160])

    async def refresh_bankroll_if_stale(self) -> None:
        """Re-read wallet equity on a CLOCK, not only when a position settles.

        `_refresh_bankroll` used to have exactly one caller -- `settle_due`,
        when a fill settles -- and that is a loop with no exit:

            no trade -> nothing settles -> the bankroll is never re-read
                     -> the exposure cap keeps using a stale number
                     -> no trade

        MEASURED 2026-09-17: $5.00 was deposited (pUSD 5.5243 -> 10.5243,
        confirmed on-chain) and the bot kept sizing off **$5.5243** for four
        hours, until a human restarted it. With `cross_max_open_stake_pct=0.30`
        and a $0.98 orphaned position open,

            0.98 + 1.00 > 0.30 * 5.52 = $1.66

        refused EVERY crossing -- so the account could not trade its way to a
        settlement that would have refreshed the number. The cap is a FRACTION
        of this figure, which is what makes a stale read a hard trading halt
        rather than a cosmetic detail.

        Rate-limited rather than event-driven, for two reasons: the read is a
        260 ms venue call, and the number only moves when money moves.
        `_reconcile_bankroll` is unchanged, so the redemption lag still cannot
        shrink a stake -- it adopts a HIGHER read at once and needs
        `_BANKROLL_DRIFT_READS` consecutive lower ones to believe a withdrawal.
        """
        if self.mode != "live" or getattr(self, "book", None) is None:
            return
        now = time.time()
        if now - getattr(self, "_last_bankroll_refresh", 0.0) < _BANKROLL_REFRESH_S:
            return
        self._last_bankroll_refresh = now
        await self._refresh_bankroll()

    # -- run ----------------------------------------------------------------
    async def run(self) -> None:
        await self.setup()
        log.info("running in %s mode (poll %.1fs)", self.mode, settings.poll_interval_s)
        n = 0
        # At most one settlement backfill in flight; see the call site below.
        bf: asyncio.Task | None = None
        # And at most one housekeeping pass; see `_housekeeping_async`.
        hk: asyncio.Task | None = None
        while not self._stop.is_set():
            t0 = time.time()
            # Timed phase marks for `_SLOW_TICK_MS`; see that constant for why.
            ph: list[tuple[str, float]] = []
            try:
                await self.tick()
                ph.append(("tick", time.time()))
                # Backfill outcomes at a low duty cycle -- rounds close every 5
                # minutes, so checking every ~15s is ample and keeps the poll light.
                #
                # ⛔ DISPATCHED, NEVER AWAITED. This was the whole of the long
                # tick, and the instrument put in for exactly this question said
                # so on its first fire (2026-09-17 09:38:38, the first tick
                # after a restart, where `n % 15 == 0` holds at n=0):
                #     slow tick 8.39s: tick 32ms | backfill 8084ms
                #                      | settle 0ms | claims 273ms | after 0ms
                # `tick 32ms` is the healthy median; the other 8.36 s is this
                # call. `backfill_settlements` walks up to `limit` rounds (5)
                # and each one is a SEQUENTIAL REST round trip to the venue, so
                # its cost is that latency times however many rounds are pending.
                # The per-call figure is NOT constant, and the number that
                # matters is the worst one: the same call has since returned
                # inside this instrument's 2 s threshold (0 warnings in the 20
                # minutes after the fix, against 8.08 s at boot). A mean would
                # have hidden it -- five calls can fit in 50 ms, because gamma
                # is edge-cached at 8.7 ms, yet the whole call took 8084 ms.
                # Waiting on it here means the loop runs NO tick for
                # eight seconds, and `_idle` is what evaluates the cross arm:
                # the budget passed at the bottom of this loop is
                # `max(0.05, 1.0 - 8.39)` = **50 ms of cross-arm sampling
                # instead of ~960 ms**. The feed keeps ticking throughout, so
                # nothing is lost on the socket -- what is lost is the chance to
                # ACT, which is the same failure `_idle`'s own docstring records
                # for leaving the cross arm on the 1 s clock.
                # The deadline is unchanged (still every 15th tick) and still
                # one at a time: `_resolve_round` calls the same venue the fire
                # path does, so a pile of overlapping backfills would be a worse
                # neighbour than the stall it replaces. A slow one cannot
                # overlap itself into a stampede, and the next dispatch simply
                # waits for `done()`.
                if n % 15 == 0 and (bf is None or bf.done()):
                    bf = asyncio.create_task(self._backfill_async())
                ph.append(("backfill", time.time()))
                # ⛔ DISPATCHED, NEVER AWAITED -- the same fix as the backfill
                # above, for the same reason, found by the same instrument.
                # Settlement, the claim sweep and the bankroll re-read cost the
                # loop 643 s across 121 slow ticks (worst single tick 31.8 s)
                # and NONE of them decides a trade. See `_housekeeping_async`
                # for the measurement, for why the three stay in one task and
                # in this order, and for why cancelling one is safe.
                # SINGLE-FLIGHT, and that is not decoration: `settle_due`
                # mutates `open_fills`, the guard pool and the ledger, so two
                # in flight would double-book a settlement. `done()` is the
                # whole interlock -- at a 1 s poll against a 22.9 s worst case
                # there would otherwise be twenty-two concurrent settles.
                if self.mode != "record" and (hk is None or hk.done()):
                    hk = asyncio.create_task(self._housekeeping_async())
                ph.append(("housekeep", time.time()))
            except Exception as e:
                log.exception("tick failed: %s: %s", type(e).__name__, e)
            n += 1
            if n % 60 == 0:
                c = self.store.counts()
                log.info("stats: rounds=%d quotes=%d decisions=%d fills=%d | %s",
                         c["rounds"], c["quotes"], c["decisions"], c["fills"], self.pnl)
            elapsed = time.time() - t0
            if elapsed * 1000.0 >= _SLOW_TICK_MS:
                log.warning("slow tick %.2fs: %s", elapsed,
                            _phase_breakdown(t0, ph, time.time()))
            # The cadence this arm's edge depends on. Reported rather than
            # assumed, because widening the websocket book to six families is
            # the one change in this repo measured to slow it (1.00s -> 4.15s).
            pm = getattr(self, "_poll_ms", None)
            if pm is None:
                pm = self._poll_ms = []
            pm.append(elapsed * 1000.0)
            if len(pm) >= 120:
                q = sorted(pm)
                # WARNING when the worst tick in the window is actually bad,
                # DEBUG otherwise. The number matters when it MOVES (widening the
                # websocket book is the one change here measured to take the
                # cadence 1.00s -> 4.15s) but a steady "median 40ms" every two
                # minutes was 80 of the ~1,600 lines in a 3-hour window.
                cad = ("poll cadence over %d ticks: median %.0fms p90 %.0fms "
                       "max %.0fms", len(q), q[len(q) // 2],
                       q[int(len(q) * 0.9)], q[-1])
                if q[-1] >= _SLOW_TICK_MS:
                    log.warning(*cad)
                else:
                    log.debug(*cad)
                self._poll_ms = []
            # In impulse mode this is spent waiting on the price feed rather
            # than on the clock, so the trigger fires ~20 ms after the move
            # instead of up to a poll interval later (see _idle).
            await self._idle(max(0.05, settings.poll_interval_s - elapsed))
        for f in self.spots.values():
            await f.stop()
        # Stop the book feed BEFORE the session goes: otherwise its reconnect
        # loop sees "Session is closed", logs a warning and backs off on every
        # shutdown, which reads like a feed fault in the log when it is just
        # the process exiting.
        wsb = getattr(self, "ws_book", None)
        if wsb is not None:
            wsb.stop()
        # The backfill and housekeeping tasks are deliberately not awaited on
        # the way in, so they must be cancelled on the way out: left pending
        # they would keep a REST call and a SQLite write running past `stop()`,
        # and "Task was destroyed but it is pending" is a confusing last word
        # for a clean shutdown. Cancelling housekeeping mid-settle is safe by
        # construction -- `_housekeeping_async` enumerates the three places a
        # cancel can land and why none of them loses money.
        for _t in (bf, hk):
            if _t is not None and not _t.done():
                _t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await _t
        task = getattr(self, "_ws_task", None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log.info("stopped. final: %s", self.pnl)

    def stop(self) -> None:
        self._stop.set()


def _quiet_libraries() -> None:
    """Silence third-party INFO chatter that is not a trading event.

    httpx logs ONE INFO LINE PER REQUEST (`HTTP Request: GET <url> "HTTP/2 200
    OK"`). That is right for a library and wrong for an operations log: the bot
    fetches order metadata for every family at every round roll, reads positions
    once a minute for the claim sweep, and reads balance-allowance for the
    bankroll -- and EVERY one of those already logs its own line where it
    matters (`warm quote used`, `claim sweep`, `pm FAK FILL`). Measured over the
    last 3 hours of live.log: ~300 of ~1,600 lines were transport noise for
    actions that each had a better line elsewhere. It also hides the lines worth
    reading -- `POST /order "HTTP/2 400 Bad Request"` and the startup probe's 404
    are both httpx INFO, so "scan the log for trouble" ends up reading the
    transport instead of the decisions.

    `log_level=DEBUG` restores everything: at DEBUG nothing is suppressed, on the
    principle that diagnostic mode should be complete rather than merely tidier.
    """
    if settings.log_level.upper() == "DEBUG":
        return
    for name in ("httpx", "httpcore", "urllib3", "aiohttp", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("record", "paper", "live"),
                    default=settings.trading_mode)
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = run forever)")
    ap.add_argument("--resume", action="store_true",
                    help="live mode: clear a persisted risk halt and trade again "
                         "with a fresh daily loss budget (logged loudly)")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    _quiet_libraries()
    store = Store(settings.db_path)
    async with aiohttp.ClientSession() as session:
        bot = Bot(args.mode, store, session)
        bot.resume_guard = args.resume
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(s, bot.stop)
        task = asyncio.create_task(bot.run())
        if args.duration:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=args.duration)
            bot.stop()
        await task
    store.close()


if __name__ == "__main__":
    asyncio.run(amain())
