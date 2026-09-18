"""IS `twap_forecast` STILL RIGHT NOW THAT THE ARM ACTS PAST tau=60?

THE QUESTION. The formula was written when `cross_max_tau_s = 60`, i.e. the arm
only ever acted INSIDE the paid window. It projects the paid quantity

    TWAP60(bell) = mean(spot over [bell - 60, bell])

from `ts` with `tau = bell - ts` seconds left, and the printed part of that
window is `[bell - 60, ts] = [ts + tau - 60, ts]`:

    tau >= 60   the whole window is in the FUTURE        -> forecast = spot
    tau <  60   the window is printed over (60 - tau) s  -> ((60-tau)*m + tau*p)/60

where `m` is the mean over `[ts + tau - 60, ts]` -- the RECENT `(60 - tau)`
seconds, which is the whole point: as tau grows, the older part of the minute
falls OUT of the window and must stop being averaged in.

WHAT THE CODE DOES. `twap_forecast` computes `m` over `[ts - 60, ts]` -- the
whole trailing minute, at every tau. At tau=0 the two agree (the window is the
whole minute). At tau=5 the code averages 55 seconds of data the paid window
does not contain; at tau=30 it averages 30 seconds it does not contain.

WHAT THIS MEASURES. No strategy involved: the MAE of each candidate forecast of
the REALIZED `TWAP60(bell)` (computed from the same tape), by tau bucket. That
is the question the formula exists to answer, and it is answerable directly.

    code  `twap_forecast` exactly as shipped
    fix   the printed part shrinks with tau (the derivation above)
    spot  the point spot, the lower bound on tau >= 60

`--px` switches to the traded version: which side each forecast says at the tick
the arm would fire, and what the entry paid. That one carries the usual replay
caveats; the MAE table does not carry any beyond the tape being Binance.

Usage (VPS, analysis venv):
    /root/analysis-venv/bin/python tools/pm_fc_formula_check.py
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/BinancePrediction")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_cross_replay import family_of, load      # noqa: E402
from btcpredict.main import twap_forecast        # noqa: E402

BUCKETS = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, 60), (60, 90),
           (90, 120), (120, 180), (180, 300)]


def fixed_fc(ts_list, end_ms, look_s=60.0):
    """The derivation above, on a 1 Hz (ts, px) tape."""
    out = []
    for i, (ts, px) in enumerate(ts_list):
        tau = (end_ms - ts) / 1000.0
        if tau >= look_s:
            out.append((ts, px))
            continue
        need = look_s - tau                      # seconds the window has printed
        lo = ts - int(need * 1000)
        win = [p for t, p in ts_list[:i + 1] if t >= lo]
        if len(win) < 4:
            out.append((ts, px))
            continue
        m = sum(win) / len(win)
        out.append((ts, (need * m + tau * px) / look_s))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?",
                    default="/root/BinancePrediction/data/btcpredict.db")
    ap.add_argument("--look-s", type=float, default=60.0)
    args = ap.parse_args()

    r, q = load(args.db)
    if r.empty:
        print("no PM-era settled rounds")
        return
    q = q.copy()
    q["fam"] = q.slug.map(family_of)
    rows = []
    for _, rr in r.iterrows():
        g = q[(q.fam == family_of(rr.slug))
              & (q.ts_ms >= int(rr.start_ms) - 60_000)
              & (q.ts_ms <= int(rr.end_ms))]
        tape = [(int(t), float(p)) for t, p in zip(g.ts_ms, g.spot) if p and p > 0]
        if len(tape) < 90:
            continue
        end = int(rr.end_ms)
        # the venue's paid quantity, off the same tape: mean over [bell-60, bell)
        paid_win = [p for t, p in tape if end - 60_000 <= t < end]
        if len(paid_win) < 30:
            continue
        realized = sum(paid_win) / len(paid_win)
        code = twap_forecast(tape, (end - tape[-1][0]) / 1000.0,
                             look_s=args.look_s, now_ms=tape[-1][0]) or []
        fix = fixed_fc(tape, end, args.look_s)
        cmap, fmap = dict(code), dict(fix)
        for ts, px in tape:
            if ts < int(rr.start_ms):
                continue
            tau = (end - ts) / 1000.0
            c, f = cmap.get(ts), fmap.get(ts)
            if c is None or f is None:
                continue
            rows.append(dict(tau=tau, spot=px, code=c, fix=f, realized=realized,
                             r=realized))
        if len(rows) > 400_000:
            break
    d = pd.DataFrame(rows)
    if d.empty:
        print("no usable rounds")
        return
    for k in ("code", "fix", "spot"):
        d[k + "_e"] = 1e4 * (d[k] - d.realized) / d.realized
    d["code_e"] = d["code_e"].abs()
    d["fix_e"] = d["fix_e"].abs()
    d["spot_e"] = d["spot_e"].abs()
    print(f"POLYMARKET ERA: {len(d):,} (round, second) forecasts over "
          f"{d.tau.nunique()}+ distinct taus | error in bps of the spot")
    print("MAE against the REALIZED TWAP60(bell), by tau:")
    print("  tau       n     MAE code   MAE fix   MAE spot   "
          "code-fix   bias code  bias fix")
    for lo, hi in BUCKETS:
        s = d[(d.tau >= lo) & (d.tau < hi)]
        if len(s) < 50:
            continue
        b1 = 1e4 * (s.code - s.realized).mean() / s.realized.mean()
        b2 = 1e4 * (s.fix - s.realized).mean() / s.realized.mean()
        print(f"  [{lo:3.0f},{hi:3.0f}) {len(s):7d}   {s.code_e.mean():8.3f}   "
              f"{s.fix_e.mean():8.3f}   {s.spot_e.mean():9.3f}   "
              f"{s.code_e.mean() - s.fix_e.mean():+8.3f}   {b1:+8.3f}  {b2:+8.3f}")
    s = d[d.tau >= 60]
    if len(s):
        print(f"\n  tau >= 60: the paid window is entirely in the future. "
              f"MAE code {s.code_e.mean():.3f} vs spot {s.spot_e.mean():.3f} bps"
              f" -- {'AGREES' if abs(s.cmd if False else 0) == 0 else ''}"
              f" (they must be the same number)")
        same = (s.code - s.spot).abs().max()
        print(f"             max |code - spot| = {same:.2e} "
              f"({'identical' if same < 1e-9 else 'DIVERGENT'})")
    h = len(d) // 2
    print(f"\n  halves (MAE, all taus): code {d.code_e[:h].mean():.3f}/"
          f"{d.code_e[h:].mean():.3f}  fix {d.fix_e[:h].mean():.3f}/"
          f"{d.fix_e[h:].mean():.3f}")
    print("\n  HOW TO READ: the fix is only worth shipping if it beats the code")
    print("  in BOTH halves and at the taus the arm actually trades (5-180).")


if __name__ == "__main__":
    main()
