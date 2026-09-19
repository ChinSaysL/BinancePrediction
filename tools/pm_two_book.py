"""A replay that models BOTH books -- the gate's and the venue's.

WHY THIS HAD TO EXIST. Every other replay here prices on `quotes.*_ask`, and
that column tracks the VENUE (verified: quotes-minus-venue median 0.0000 with 43
exact matches, quotes-minus-ours median +0.0050 with 23). So the tape knows what
we PAY and not what the gate SEES -- and the whole venue-re-base question is
about the difference between them. A one-book replay cannot arbitrate it, which
is why it sat unresolved.

THE BRIDGE is the `shadow book` log line, which prints both books at the same
instant. 305 snapshots give the gap directly:
    venue - ours : median +0.0400, mean +0.0883, p10 -0.0200, p90 +0.2700
    ours is BELOW the venue 67% of the time, equal 15%, above 18%
So the model is: the tape's ask IS the venue; our ladder is that minus a gap
drawn from this empirical distribution. The gate then decides on the ladder, and
the order pays the venue -- which is what actually happens.

WHAT IT ARBITRATES. `_place_order` re-bases the cap on the venue's ask:
    cap = self._take_cap_for(r, venue_ask, fill_cap)      # REPLACES
When the venue sits above our ladder this ENLARGES the ceiling the decision
authorised. The alternative is a ratchet -- `min(decision_cap, venue_cap)` --
which turns those into misses instead of dearer fills. Live evidence could not
separate them: raw, re-based fills look worse (n=79 -0.1048/fill against n=101
-0.0568), but controlling for the paid price the buckets disagree (+0.1307 vs
-0.2266 at 0.5; -0.1175 vs -0.0007 at 0.6).

⛔ WHAT THIS MODEL CANNOT SEE, AND IT WILL LIE TO YOU ABOUT ONE THING. The gap
is drawn from a SINGLE distribution regardless of price, so the model has no
notion of the cheap book being stale. It therefore recommends lowering
`cross_min_price`, monotonically, all the way down -- $+176 at a 0.45 ladder
floor against $+272 at 0.35. That is the artifact `config.cross_min_price`
already documents: below 0.45 the quote reprices +0.2174 to +0.5407 within two
seconds, so those fills cannot be had at the price shown. Measured directly: a
0.35 ladder floor adds 171 entries of which 27% PAY a venue price under 0.45.
DO NOT lower the floor on this tool's advice.
`band_on="venue"` does not have the same flaw -- it gates the PAID price into
the band, so it never reaches that zone.

    python3 tools/pm_two_book.py
"""
from __future__ import annotations

import datetime as dt, json, math, os, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
from pm_full_chain import LIVE  # noqa
from pm_exit_chain import build  # noqa
from pm_exit_models import exit_at  # noqa

FEE = lambda p: 0.07 * p * (1 - p)
_SAMP = json.load(open(os.path.join(ROOT, "data", ".ladder-gaps.json")))
GAPS = np.array([g for g, _age in _SAMP])

# ⛔ THE GAP IS THE STALENESS, SO IT MUST BE DRAWN CONDITIONED ON IT.
# The unconditional draw was this model's known blind spot: it handed a 17 ms
# book the same handicap as a 1.8 s one, which systematically over-penalised
# the fresh quotes and under-penalised the stale ones. That is the mechanism
# behind the artifact -- it is what made two independent sweeps ask to lower
# `cross_min_price`, because cheap quotes are disproportionately STALE quotes
# and the model was pricing them as if they were fresh.
#
# Buckets rather than a fitted curve: 305 samples is not enough to fit a shape
# and quantile bins make no assumption about one. Edges are the sample
# quantiles, so every bucket is populated by construction.
_AGES = np.array([a for _g, a in _SAMP], dtype=float)
_EDGES = np.quantile(_AGES, [0.25, 0.50, 0.75])
_BUCKETS = []
for _lo, _hi in zip([-np.inf, *_EDGES], [*_EDGES, np.inf]):
    _sel = GAPS[(_AGES >= _lo) & (_AGES < _hi)]
    _BUCKETS.append(_sel if len(_sel) else GAPS)


# ⛔ THE SUPPORT OF THE SAMPLE SET, AND THE REASON IT IS ENFORCED.
# Conditioning on age was supposed to remove the cheap-price artifact. IT DID
# NOT -- at px_lo 0.30 the conditioned model liked the cheap end MORE
# ($+363.71 against the pooled $+288.52 at look 1.0). The measurement that
# explains why is not in this file, it is in the tape:
#
#   ask bucket        n        med age    p90 age    max age
#   [0.02,0.20)   369366           63ms     3458ms    30000ms
#   [0.20,0.35)   251202           40ms      478ms    29978ms
#   [0.45,0.55)   224179           53ms      705ms    29948ms
#   [0.65,0.98)   594902           38ms      464ms    30000ms
#
#   the gap samples:  n=305  median 322ms  p90 515ms  MAX 1811ms
#
# The cheapest bucket's p90 staleness is 3.5 SECONDS -- 6.7x every other
# bucket -- and the oldest gap we have ever sampled is 1.8 s. So the model has
# NO OBSERVATIONS AT ALL in the regime where the cheap-book pathology lives,
# and drawing from the pooled or the bucketed set alike hands those quotes a
# ~0.04 gap when `tools/pm_fillability.py` measured the real ones repricing
# +0.2174 to +0.5407 within two seconds. No amount of conditioning on an axis
# introduces data the axis never covered.
#
# So the model REFUSES rather than extrapolates. A candidate whose book is
# staler than anything we have sampled is not priced at all and is counted, so
# the answer reads "I cannot price N of these" instead of quietly inventing a
# number for exactly the trades that are unfillable in life.
_AGE_MAX = float(_AGES.max())

# ⛔⛔ AND IT STILL DOES NOT EXPLAIN THE CHEAP-PRICE ARTIFACT. Read this before
# believing anything this model says below `cross_min_price`.
#
# Three explanations were tested and all three are WRONG:
#   1. age-pooling      conditioning the draw did not remove it -- at px_lo
#                       0.30 the conditioned model liked cheap MORE ($+362
#                       against $+289 pooled, look 1.0)
#   2. out-of-support   only 75 of 14,341 candidates (0.5%) are staler than
#                       the oldest gap sample, so the refusal above barely bites
#   3. thin books       the new size-at-touch tape says cheap books hold
#                       $6.02-$9.48 median at the touch and 0.0% below $1
#
# The measured cause is TEMPORAL, and this model cannot see it because of an
# assumption one line up from where the gap is applied: `venue = float(ask)`,
# "the tape IS the venue". For a stale cheap book the RECORDED ASK IS ITSELF
# GONE. From test_cross_min_margin.py, on the same tape:
#
#     priced at the quoted ask       the sub-0.45 cohort is  +$58.42
#     priced at the ask 2 s later    the same cohort is      -$17.13
#
# A crossing IS the moment a stale cheap quote reprices, so the replay buys
# 0.06 and the side is at 0.60 before the order could land. The candidates
# confirm the shape: below 0.45 they win 48.7% against 58.2% at or above it,
# and are 4-12x staler at p90 (835-1002ms against 79-261ms). The model still
# likes them because a win at 0.30 pays enormously -- at a price nobody gets.
#
# The ladder gap is a SPATIAL correction (our book against the venue's at one
# instant). No spatial correction fixes a venue price that is stale in TIME.
# Fixing it means pricing the fill at the ask some seconds after the decision,
# which this tool cannot do yet: `ticks` carries the BIDS (the exit sells into
# them) and not the later asks. That is the next change, not this one.
#
# Until then `cross_min_price` stays at 0.45 and this model's sub-0.45 numbers
# are not evidence for anything.


def gap_for(age_ms, rng):
    """A ladder gap drawn from the samples whose staleness resembles this one.

    Returns None when the book is staler than the whole sample set -- OUT OF
    SUPPORT, see above. The caller must skip such a candidate.

    `age_ms` None -- the REST poll supplied the ask, so no websocket staleness
    applies -- falls back to the pooled draw. That is the old behaviour and the
    honest answer for a row that has no age, but it is a DIFFERENT unknown from
    an age we have measured and cannot price, so `sim` counts the two apart.
    """
    if age_ms is None or not np.isfinite(age_ms):
        return float(rng.choice(GAPS))
    if age_ms > _AGE_MAX:
        return None
    return float(rng.choice(_BUCKETS[int(np.searchsorted(_EDGES, age_ms))]))


def take_cap(base, *, buf_inv, buf_abs, max_price, fill_cap):
    """main.py's `_take_cap_for` / `_cross_tick` ceiling arithmetic."""
    buf = max(0.01, min(buf_abs, buf_inv * (1.0 - base)))
    cap = min(max_price, round(base + buf, 2))
    return min(cap, fill_cap) if fill_cap > 0 else cap


def sim(R, *, rebase, days=None, bankroll=22.6, expo=0.50, stake=1.0, seed=7,
        px_lo=None, px_hi=None, fill_cap=None, buf_inv=None, gap_scale=1.0,
        band_on="ladder"):
    px_lo = LIVE["cross_min_price"] if px_lo is None else px_lo
    px_hi = LIVE["cross_max_price"] if px_hi is None else px_hi
    fill_cap = LIVE["cross_max_fill_px"] if fill_cap is None else fill_cap
    bi = LIVE["cross_limit_buffer_inv"] if buf_inv is None else buf_inv
    ba = LIVE["cross_limit_buffer"]
    rng = np.random.default_rng(seed)
    ev = []
    unsupported = 0
    for end, cands, ticks, slug in R:
        for c in cands:
            (ts, tau, side, am, ask, won, age, dok, dbps, flip, gap, ti, rng_,
             bage) = c
            if not (LIVE["cross_min_tau_s"] <= tau <= LIVE["cross_max_tau_s"]):
                continue
            if age > LIVE["max_spot_age_s"] or am < LIVE["cross_min_margin_bps"]:
                continue
            if not dok:
                continue
            md = LIVE["cross_max_depth_bps"]
            if md > 0 and dbps > md:
                continue
            venue = float(ask)                       # the tape IS the venue
            # `gap_scale` models LATENCY: 0.5 = half the current staleness,
            # 0.0 = our book is the venue's (a perfect feed).
            _g = gap_for(bage, rng)
            if _g is None:               # book staler than any sample we hold
                unsupported += 1
                continue
            ladder = round(venue - _g * gap_scale, 2)
            gated = ladder if band_on == "ladder" else venue
            if not (px_lo <= gated <= px_hi):
                continue
            ev.append((ts, end, side, won, venue, ladder, ticks, ti))
            break
    ev.sort(key=lambda x: (x[0], x[1]))
    slots, tot, n, wins, miss, per, dd = [], 0.0, 0, 0, 0, [], []
    for ts, end, side, won, venue, ladder, ticks, ti in ev:
        d = dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%m-%d")
        if days is not None and d not in days:
            continue
        slots = [e for e in slots if e > ts]
        s = stake * len(slots)
        if s > 0 and s + stake > expo * bankroll:
            continue
        dec_cap = take_cap(ladder, buf_inv=bi, buf_abs=ba,
                           max_price=px_hi, fill_cap=fill_cap)
        ven_cap = take_cap(venue, buf_inv=bi, buf_abs=ba,
                           max_price=px_hi, fill_cap=fill_cap)
        cap = ven_cap if rebase else min(dec_cap, ven_cap)
        if venue > cap:                              # FAK finds nothing
            miss += 1
            continue
        pay = min(max(venue, 0.01), 0.99)
        sh = stake / pay
        x = exit_at(ticks, ti, side, pay, "margin", LIVE["cross_exit_margin_bps"],
                    LIVE["cross_min_margin_bps"], LIVE["cross_exit_slip"],
                    LIVE["cross_exit_min_tau_s"])
        r = ((x[1] - pay) - FEE(pay)) / pay if x else \
            ((1.0 if won else 0.0) - pay - FEE(pay)) / pay
        tot += r * sh; per.append(r); wins += r > 0; n += 1
        slots.append(x[0] if x else end); dd.append(d)
    if n < 20:
        return None
    v, dv = np.array(per), np.array(dd)
    sd = v.std(ddof=1)
    u = np.unique(dv); dm = np.array([v[dv == x].mean() for x in u])
    return dict(n=n, miss=miss, unsupported=unsupported,
                win=100 * wins / n, tot=tot, per=v.mean(),
                t=v.mean() / (sd / math.sqrt(n)) if sd else 0.0,
                dayt=dm.mean() / (dm.std(ddof=1) / math.sqrt(len(dm)))
                if len(dm) > 1 and dm.std(ddof=1) else 0.0)


def main():
    print("  gap model: n=%d  median %+.4f  mean %+.4f\n"
          % (len(GAPS), float(np.median(GAPS)), float(GAPS.mean())))
    for look in (LIVE["cross_look_s"], 2.0):
        R = build(look, LIVE["cross_hold_s"], 10)
        days = sorted({dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
                       for _, cs, _, _ in R for c in cs})
        TE = set(days[len(days) // 2:])
        print("  === look %.1f ===" % look)
        print("  %-26s %-34s %s" % ("", "FULL TAPE", "TEST (unseen)"))
        for rebase, lab in ((True, "re-base ON (ships)"), (False, "ratchet (min)")):
            accs = []
            for sd_ in (7, 11, 13, 17, 23):          # average over gap draws
                f = sim(R, rebase=rebase, seed=sd_)
                t = sim(R, rebase=rebase, seed=sd_, days=TE)
                if f and t:
                    accs.append((f, t))
            if not accs:
                print("  %-26s n<20" % lab); continue
            m = lambda k, i: float(np.mean([a[i][k] for a in accs]))
            print("  %-26s n=%4.0f miss=%4.0f win%5.1f%% $%+7.2f t%+5.2f   "
                  "n=%4.0f win%5.1f%% $%+7.2f"
                  % (lab, m("n", 0), m("miss", 0), m("win", 0), m("tot", 0),
                     m("t", 0), m("n", 1), m("win", 1), m("tot", 1)))
        print()


if __name__ == "__main__":
    main()
