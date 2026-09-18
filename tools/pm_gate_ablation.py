"""Are the entry gates still earning their keep at the SHIPPED window?

WHY THIS EXISTS. The arm carries six numerical entry gates, and every one of
them was RANKED on a population the arm no longer trades:

    gate                     where its number came from
    cross_min_margin_bps     tau 5-180, look 1.0   (re-ranked today)
    cross_max_tau_s          tau 5-180, look 1.0   (raised 60 -> 180 TODAY)
    cross_min_depth_ticks    per-symbol, live refusals
    cross_max_depth_bps      tau 5-60, real-book days
    cross_max_round_range_bps tau 5-60, look 1.0, ONE day-family grid

The last one is the ask. `cross_max_round_range_bps` -- the VOLATILITY gate --
is the only REGIME filter the arm runs (the sigma gate is off at 0.0, `min_edge`
is off at 0.0), and it is a fixed count of BPS. Range-so-far is a quantity whose
expected size grows with the time already elapsed, so the same 12 bps is most of
a round's diffusion at tau 60 and a small part of it at tau 180 -- which is
exactly what `cross_max_tau_s`'s own comment says ("why the gate binds live but
did not in the measurement"). The window was then TRIPLED from 60 s to 180 s,
which changes the quantity the gate is a threshold ON. Nobody has re-asked the
question at the window the arm actually runs, and the live log is full of

    cross bnb-updown-5m-... Down not taken: round already moved 34.01bps, gate is 7.80

WHAT IT ANSWERS, in four sections:
  S1  every enabled gate, evaluated independently on the shipped cohort: how
      many entries it refuses and WHAT THOSE ENTRIES WERE WORTH. A gate that
      refuses a negative cohort earns its keep; one that refuses a positive
      cohort is paying for a filter that no longer filters.
  S2  the volatility gate's number by TAU BAND -- is the 12-20 bps bin that
      measured -0.324 at tau<60 still negative at tau 60-120 and 120-180?
  S3  sweeps at the shipped window: the absolute bps gate, the TIME-SCALED
      form (`range_z` = travel / sigma of the elapsed time), the tick floor and
      the depth ceiling -- each with the full battery.
  S4  the two gates the replay chain already applies (margin floor, price band),
      priced by re-running the chain with them off.

WHAT IT DOES NOT MODEL, exactly as `pm_cross_replay` states: the tape pays the
recorded ask with no sweep premium and no FAK miss rate, and there is no book,
so the liquidity gates (depth in dollars, sweep price) cannot be applied at all.
`sigma` here is rebuilt from the 1 Hz tape (minute closes, EWMA/simple blend in
the shape of `pricing.vol.sigma_1m`), not the live object -- so `range_z` and
`z` rank, they do not replicate. The trigger, the level, the forecast and the
round-range are imported from the bot, never reimplemented.

Usage (analysis venv, on the VPS -- the bot venv has no numpy):
    /root/analysis-venv/bin/python tools/pm_gate_ablation.py
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_cross_replay as base  # noqa: E402  (also puts the repo on sys.path)

from btcpredict.config import cross_knob, market_prefix, settings  # noqa: E402
from btcpredict.main import depth_in_ticks, depth_sigma  # noqa: E402

DB = "/root/BinancePrediction/data/btcpredict.db"


def sigma_1m(tape: list[tuple[int, float]], upto_ms: int,
             look_min: int = 90) -> float:
    """The scale the TIME-SCALED gate would use: per-minute vol, rebuilt.

    `Bot._sigma` is `pricing.vol.sigma_1m`: a 0.7*EWMA + 0.3*simple blend of
    per-MINUTE log returns, one number per family per poll. The recorded tape is
    1 Hz, so this rebuilds minute CLOSES from it and applies the same blend
    shape. It is not the live object (different sample source, different
    warm-up), which is why everything derived from it here is used to RANK and
    never to ship a number.
    """
    lo = upto_ms - (look_min + 1) * 60_000
    closes: dict[int, float] = {}
    for t, p in tape:
        if lo <= t <= upto_ms and p and p > 0:
            closes[t // 60_000] = float(p)
    ks = sorted(closes)
    rets = [math.log(closes[b] / closes[a]) for a, b in zip(ks, ks[1:])
            if closes[a] > 0 and closes[b] > 0]
    if len(rets) < 5:
        return 0.0
    simple = float(np.std(rets, ddof=1))
    var = rets[0] ** 2
    for x in rets[1:]:
        var = 0.94 * var + 0.06 * x * x
    return 0.7 * math.sqrt(var) + 0.3 * simple


def stamp(pre: dict, e: dict) -> dict:
    """The entry the shipped chain takes, tagged with every gate's own input."""
    r = pre["r"]
    ts = int(e["ts_ms"])
    tape = pre["tape"]
    seen = [p for t, p in tape if t >= int(r.start_ms) and p > 0]
    rows = pre["rows"]
    prior = rows[rows.ts_ms <= ts]
    spot = float(prior.spot.iloc[-1]) if len(prior) else None
    level = float(pre["level"])
    mk = market_prefix(r.slug)
    tau = (int(r.end_ms) - ts) / 1000.0
    elapsed = (ts - int(r.start_ms)) / 1000.0
    sig = sigma_1m(tape, ts)
    rng = (1e4 * (max(seen) - min(seen)) / float(r.strike)
           if len(seen) >= 2 else None)
    # The diffusion the round had to travel its range in, in bps. A fixed bps
    # gate is only comparable across tau through this.
    scale = 1e4 * sig * math.sqrt(max(elapsed, 1.0) / 60.0) if sig else 0.0
    out = dict(e)
    out.update(
        mk=mk, tau_now=tau, elapsed_s=elapsed, spot=spot, level=level,
        depth_bps=(1e4 * abs(spot - level) / level if spot else None),
        ticks=(depth_in_ticks(spot, level, mk) if spot else None),
        z=(depth_sigma(spot, level, sig, tau) if spot and sig else None),
        range_bps=rng,
        range_z=(rng / scale if rng is not None and scale else None),
        sigma_bps=(1e4 * sig if sig else None),
        gate_range=cross_knob(mk, "max_round_range_bps"),
        gate_ticks=(cross_knob(mk, "min_depth_ticks") or 0.0),
        gate_depth=cross_knob(mk, "max_depth_bps"),
    )
    return out


def battery(rows: list[dict], label: str, pad: str = "    ") -> dict:
    """The repo's battery: pooled t, day-clustered t, both halves, worst day."""
    if not rows:
        print(f"{pad}{label:<32} n=0")
        return {}
    d = pd.DataFrame(rows).sort_values("ts_ms")
    v = d.per.to_numpy()
    n = len(v)
    sd = v.std(ddof=1) if n > 1 else 0.0
    t = v.mean() / (sd / math.sqrt(n)) if sd else 0.0
    dm = np.array([v[d.day.values == x].mean() for x in d.day.unique()])
    ds = dm.std(ddof=1) if len(dm) > 1 else 0.0
    day_t = dm.mean() / (ds / math.sqrt(len(dm))) if ds else 0.0
    h = n // 2
    print(f"{pad}{label:<32} n={n:4d} ask={d.ask.mean():.3f} "
          f"win={d.won.mean():5.1%} per={v.mean():+.4f} tot={v.sum():+7.2f} "
          f"t={t:+5.2f} day-t={day_t:+5.2f} "
          f"halves={v[:h].mean():+.4f}/{v[h:].mean():+.4f} "
          f"worst-day={dm.min():+.4f}")
    return dict(n=n, per=float(v.mean()), total=float(v.sum()), t=t,
                day_t=day_t, halves=(float(v[:h].mean()), float(v[h:].mean())))


def by_tau(rows: list[dict], edges=(0, 60, 120, 999)) -> None:
    """Per-share by tau band -- the axis a fixed bps threshold cannot see."""
    for lo, hi in zip(edges, edges[1:]):
        sel = [e for e in rows if lo <= e["tau"] < hi]
        battery(sel, f"tau {lo:.0f}-{hi:.0f}", pad="      ")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=DB)
    ap.add_argument("--no-s4", action="store_true",
                    help="skip the margin/band re-runs (3 chains instead of 1)")
    args = ap.parse_args()

    look = base.shipped_look_s()
    hold = base.shipped_hold_s()
    px_lo = float(settings.cross_min_price)
    px_hi = float(settings.cross_max_fill_px)
    floor = float(settings.cross_min_margin_bps)
    tau_lo = float(settings.cross_min_tau_s)
    tau_hi = float(settings.cross_max_tau_s)

    r, q = base.load(args.db)
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)
    print(f"SHIPPED CHAIN: look {look:.1f}s hold {hold:.1f}s floor {floor} "
          f"band [{px_lo:.2f},{px_hi:.2f}) tau [{tau_lo:.0f},{tau_hi:.0f}] | "
          f"{len(r)} settled PM rounds")

    ents: list[dict] = []
    nofloor: list[dict] = []
    wide: list[dict] = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look, px_lo, px_hi, tau_lo, tau_hi)
        if pre is None:
            continue
        kw = dict(signal="fc", look_s=look, tau_lo=tau_lo, tau_hi=tau_hi,
                  hold_s=hold)
        e = base.pick(pre, floor_bps=floor, px_lo=px_lo, px_hi=px_hi, **kw)
        if e is not None:
            ents.append(stamp(pre, e))
        if not args.no_s4:
            e2 = base.pick(pre, floor_bps=0.0, px_lo=px_lo, px_hi=px_hi, **kw)
            if e2 is not None:
                nofloor.append(stamp(pre, e2))
            e3 = base.pick(pre, floor_bps=floor, px_lo=0.0, px_hi=1.01, **kw)
            if e3 is not None:
                wide.append(stamp(pre, e3))
    ents.sort(key=lambda x: x["ts_ms"])
    bad = [e for e in ents if e["range_bps"] is None or e["ticks"] is None]
    if bad:
        print(f"  note: {len(bad)} entries without a range/tick reading "
              f"(tape gaps or no prior row); excluded from those cuts")
    print(f"entries under the shipped chain: {len(ents)}\n")
    battery(ents, "ALL (gates not in the chain off)")

    # ---------------------------------------------------------------- S1 ----
    print("\nS1. EACH GATE, EVALUATED ALONE ON THAT COHORT "
          "(refused cohort's own P&L)")
    print("  range gate (cross_max_round_range_bps, per family)")
    for fam in sorted({e["mk"] for e in ents}):
        sel = [e for e in ents if e["mk"] == fam]
        g = sel[0]["gate_range"]
        ref = [e for e in sel if e["range_bps"] is not None
               and e["range_bps"] > g]
        keep = [e for e in sel if e["range_bps"] is not None
                and e["range_bps"] <= g]
        print(f"    {fam:<16} gate {g:>5.1f}  keeps {len(keep):3d} "
              f"refuses {len(ref):3d}")
        if ref:
            battery(ref, f"    refused  {fam}", pad="      ")
    ref_all = [e for e in ents if e["range_bps"] is not None
               and e["range_bps"] > e["gate_range"]]
    keep_all = [e for e in ents if e["range_bps"] is not None
                and e["range_bps"] <= e["gate_range"]]
    battery(keep_all, "  RANGE GATE KEEPS")
    battery(ref_all, "  RANGE GATE REFUSES")
    if ref_all:
        print("    those refusals by tau:")
        by_tau(ref_all)

    print("\n  depth ceiling (cross_max_depth_bps)")
    ref = [e for e in ents if e["depth_bps"] is not None
           and e["depth_bps"] > e["gate_depth"]]
    keep = [e for e in ents if e["depth_bps"] is not None
            and e["depth_bps"] <= e["gate_depth"]]
    battery(keep, "  depth ceiling KEEPS")
    battery(ref, "  depth ceiling REFUSES")

    print("\n  tick floor (cross_min_depth_ticks, per market)")
    ref = [e for e in ents if e["ticks"] is not None
           and e["ticks"] < e["gate_ticks"]]
    keep = [e for e in ents if e["ticks"] is not None
            and e["ticks"] >= e["gate_ticks"]]
    battery(keep, "  tick floor KEEPS")
    battery(ref, "  tick floor REFUSES")
    for fam in sorted({e["mk"] for e in ents}):
        sel = [e for e in ents if e["mk"] == fam and e["ticks"] is not None]
        n = sum(1 for e in sel if e["ticks"] < e["gate_ticks"])
        med = float(np.median([e["ticks"] for e in sel])) if sel else 0.0
        print(f"    {fam:<16} floor {sel[0]['gate_ticks']:>3.0f}  "
              f"median depth {med:5.2f} ticks  refuses {n:3d}/{len(sel):3d}")

    # ---------------------------------------------------------------- S2 ----
    print("\nS2. THE VOLATILITY GATE'S NUMBER BY TAU BAND "
          "(range-so-far buckets, one entry per round)")
    edges = [0, 5, 8, 12, 16, 20, 30, 50, 10**6]
    for lo, hi in zip((0, 60, 120), (60, 120, 10**6)):
        sel = [e for e in ents if lo <= e["tau"] < hi
               and e["range_bps"] is not None]
        v0 = np.array([e["per"] for e in sel]) if sel else np.array([0.0])
        print(f"  tau {lo:.0f}-{hi if hi < 10**6 else 'end'}  n={len(sel)} "
              f"band total per-share={v0.mean():+.4f}")
        for a, b in zip(edges, edges[1:]):
            s = [e for e in sel if a <= e["range_bps"] < b]
            if not s:
                continue
            v = np.array([e["per"] for e in s])
            print(f"      range {a:>3}-{b if b < 10**6 else '+':<3} "
                  f"n={len(s):3d} win={np.mean([e['won'] for e in s]):5.1%} "
                  f"per={v.mean():+.4f} tot={v.sum():+6.2f}")

    # ---------------------------------------------------------------- S3 ----
    print("\nS3. SWEEPS AT THE SHIPPED WINDOW")
    print("  absolute bps gate (shipped = per-family 7.8-21.1, global 12.0)")
    for g in (None, 8, 12, 16, 20, 30, 50):
        sel = (ents if g is None else
               [e for e in ents if e["range_bps"] is None
                or e["range_bps"] <= g])
        battery(sel, f"range <= {g}" if g is not None else "range OFF")
    sel = [e for e in ents if e["range_bps"] is None
           or e["range_bps"] <= e["gate_range"]]
    battery(sel, "range SHIPPED (per-family)")
    sel = [e for e in ents if e["range_bps"] is None or e["range_bps"] <= 12.0]
    battery(sel, "range FLAT 12.0")

    print("  time-scaled gate: range / (sigma * sqrt(elapsed))")
    for z in (None, 0.6, 0.9, 1.2, 1.5, 2.0):
        sel = (ents if z is None else
               [e for e in ents if e["range_z"] is None
                or e["range_z"] <= z])
        battery(sel, f"range_z <= {z}" if z is not None else "range_z OFF")

    print("  tick floor")
    for k in (0, 1, 2):
        sel = [e for e in ents if e["ticks"] is None or e["ticks"] >= k]
        battery(sel, f"ticks >= {k}" if k else "tick floor OFF")

    print("  depth ceiling (bps through the reference)")
    for k in (0, 5, 8, 15):
        sel = [e for e in ents if e["depth_bps"] is None
               or e["depth_bps"] <= k] if k else ents
        battery(sel, f"depth <= {k}" if k else "depth ceiling OFF")

    # ---------------------------------------------------------------- S5 ----
    print("\nS5. COMBINATIONS (the shipped knobs, then the candidates)")

    def tick_ok(e, floor):
        return e["ticks"] is None or e["ticks"] >= floor

    def rng_ok(e, val):
        return (True if val is None else
                e["range_bps"] is None or e["range_bps"] <= val)

    battery([e for e in ents if rng_ok(e, e["gate_range"])
             and tick_ok(e, e["gate_ticks"])],
            "SHIPPED (range per-family + ticks)")
    battery([e for e in ents if tick_ok(e, e["gate_ticks"])],
            "range OFF + ticks SHIPPED")
    battery([e for e in ents if tick_ok(e, 2)], "range OFF + ticks >= 2")
    battery([e for e in ents if tick_ok(e, 1)], "range OFF + ticks >= 1")
    battery([e for e in ents if rng_ok(e, e["gate_range"])],
            "range SHIPPED + ticks OFF")
    # The tau-conditional form of the SAME gate: it is right at tau<60 (S2) and
    # inverted after, so applying it only where it was measured is the minimal
    # change that preserves its original finding. NOTE the sense: NO range filter
    # at tau >= 60, the shipped gate below it. The first version of this line had
    # the two halves the wrong way round (it gated tau >= 60 instead), which is
    # the same mistake the shipped config is making -- worth the comment.
    tau_only = [e for e in ents if (e["tau_now"] >= 60.0
                                    or rng_ok(e, e["gate_range"]))]
    battery(tau_only, "range gate only at tau<60")
    battery([e for e in tau_only if tick_ok(e, e["gate_ticks"])],
            "  + ticks SHIPPED")

    # ---------------------------------------------------------------- S4 ----
    if not args.no_s4:
        print("\nS4. THE TWO GATES THE REPLAY CHAIN ALREADY APPLIES")
        battery(ents, "SHIPPED")
        battery(nofloor, f"margin floor OFF (was {floor})")
        extra = {e["slug"] for e in nofloor} - {e["slug"] for e in ents}
        battery([e for e in nofloor if e["slug"] in extra],
                "  the entries it adds")
        battery(wide, f"band OFF (was [{px_lo:.2f},{px_hi:.2f}))")
        extra = {e["slug"] for e in wide} - {e["slug"] for e in ents}
        battery([e for e in wide if e["slug"] in extra],
                "  the entries it adds")
    # ---------------------------------------------------------------- S6 ----
    # THE TWO CANDIDATES THE OWN RECORD POINTS AT, and neither has ever been
    # swept. (b) comes straight from `cross_min_depth_sigma`'s own note: "The
    # leak is overpaying for the shallow band, not a shortage of depth" -- so
    # the instrument is the CONJUNCTION of shallow and expensive, not depth
    # alone (a depth floor alone was refuted as non-monotone). (c) is the
    # regime switch: the arm's loss rate today tracked the family's own
    # realized vol, so the thing to rank is the P&L per fill by sigma, and a
    # stand-down floor above it.
    print("\nS6. (b) SHALLOW *AND* EXPENSIVE -- refuse z below a floor only when "
          "the ask is above break-even for a coin flip")
    for zm in (0.20, 0.30, 0.40):
        for pm in (0.55, 0.60, 0.0):
            sel = [e for e in ents
                   if not (e["z"] is not None and e["z"] < zm
                           and (pm == 0.0 or e["ask"] > pm))]
            battery(sel, f"refuse z<{zm}" + (f" & ask>{pm}" if pm else " (any ask)"))
    print("  (c) THE VOL REGIME: P&L per fill by the family's own sigma "
          "(bps/min) at the trigger")
    for lo, hi in ((0.0, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 1e9)):
        sel = [e for e in ents if e["sigma_bps"] and lo <= e["sigma_bps"] < hi]
        battery(sel, f"sigma {lo:g}-{hi:g} bps/min")
    print("      and as a STAND-DOWN floor (refuse when sigma is above):")
    for sm in (1.5, 2.0, 2.5, 3.0):
        sel = [e for e in ents if e["sigma_bps"] is None or e["sigma_bps"] <= sm]
        battery(sel, f"sigma <= {sm:g}")

    print("\nDONE -- rank, do not ship: sigma here is rebuilt, not the live "
          "object (see the module docstring).")


if __name__ == "__main__":
    main()
