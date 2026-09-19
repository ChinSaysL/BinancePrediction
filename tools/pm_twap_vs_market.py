"""DOES THE MODEL'S PROBABILITY BEAT THE MARKET'S PRICE?

Stage 2 of `pm_twap_formulas.py`, which validated the ground truth (98.6% of
the venue's settlements reproduced from the tape) and saved the scored frame.

⛔ BEATING A NAIVE FORECAST IS WORTHLESS. The shipped formula is 1.8x worse
than the corrected one at tau 5-30 -- and that means nothing for P&L if the
market already prices the difference. The only question is whether
`P(model)` carries information the ASK does not.

Scored three ways, weakest to strongest:
  1. Brier / log-loss of P(model) vs the ask, against the realised outcome.
     A model that cannot beat the price on calibration cannot beat it on money.
  2. Calibration table: when the model says 60%, does it win 60%?
  3. The money: bet only when P - ask clears a threshold, pay the ask, charge
     the venue's fee curve.
"""
from __future__ import annotations

import os, sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEE = lambda p: 0.07 * p * (1 - p)


def main():
    d = pd.read_pickle(os.path.join(ROOT, "data", ".twapfx.pkl"))
    # The tradeable side: model says Up -> buy Up at ua, else buy Down at da.
    d = d[np.isfinite(d.ua) & np.isfinite(d.da)]
    d = d[(d.ua > 0.01) & (d.ua < 0.99) & (d.da > 0.01) & (d.da < 0.99)]
    print("usable ticks: %d over %d rounds\n" % (len(d), d.slug.nunique()))

    # market-implied P(Up) from the two asks, de-vigged
    tot = d.ua + d.da
    d = d.assign(p_mkt=(d.ua / tot).clip(0.001, 0.999))
    y = d.up.to_numpy()
    pm = d.p_mkt.to_numpy()
    pmod = d.p_up.to_numpy().clip(0.001, 0.999)

    # ⛔ TAKE THE OUTCOME AS AN ARGUMENT. The first version closed over the
    # global `y`, so every masked slice was scored against the FULL outcome
    # vector and the by-tau table raised instead of quietly lying. It could
    # just as easily have broadcast.
    def brier(p, yy=None):
        yy = y if yy is None else yy
        return float(np.mean((p - yy) ** 2))
    def ll(p, yy=None):
        yy = y if yy is None else yy
        return float(-np.mean(yy * np.log(p) + (1 - yy) * np.log(1 - p)))

    print("=" * 74)
    print("1. CAN THE MODEL OUT-PREDICT THE PRICE?")
    print("=" * 74)
    print("  %-22s %10s %10s" % ("", "Brier", "log-loss"))
    print("  %-22s %10.5f %10.5f" % ("market (de-vigged ask)", brier(pm), ll(pm)))
    print("  %-22s %10.5f %10.5f" % ("model P(Up)", brier(pmod), ll(pmod)))
    print("  %-22s %10.5f %10.5f" % ("50/50 baseline", brier(np.full_like(y,.5)), ll(np.full_like(y,.5))))
    better = brier(pmod) < brier(pm)
    print("\n  -> the model %s the market on calibration"
          % ("BEATS" if better else "does NOT beat"))
    print()
    print("  by tau:")
    print("  %-12s %8s %10s %10s %8s" % ("tau","n","mkt Brier","mdl Brier","winner"))
    for lo, hi in ((5,30),(30,60),(60,120),(120,180),(180,260)):
        m = d.tau.between(lo, hi).to_numpy()
        if m.sum() < 500: continue
        bm, bd = brier(pm[m], y[m]), brier(pmod[m], y[m])
        print("  %3d-%-8d %8d %10.5f %10.5f %8s"
              % (lo,hi,m.sum(),bm,bd,"MODEL" if bd<bm else "market"))

    print()
    print("=" * 74)
    print("2. IS THE MODEL CALIBRATED? (when it says X%, does it win X%?)")
    print("=" * 74)
    print("  %-14s %9s %10s %10s" % ("model P(Up)","n","realised","mkt says"))
    for lo in np.arange(0.0, 1.0, 0.1):
        m = (pmod >= lo) & (pmod < lo + 0.1)
        if m.sum() < 300: continue
        print("  %.1f - %.1f %11d %9.1f%% %9.1f%%"
              % (lo, lo+0.1, m.sum(), 100*y[m].mean(), 100*pm[m].mean()))

    print()
    print("=" * 74)
    print("3. THE MONEY: bet only when the model's edge over the ask clears E")
    print("=" * 74)
    print("  %-8s %9s %9s %10s %10s %9s" % ("edge E","n","win%","ask","$ net","$/bet"))
    for E in (0.02, 0.05, 0.10, 0.15, 0.20):
        # buy Up when p_up - ua > E ; buy Down when (1-p_up) - da > E
        bu = (pmod - d.ua.to_numpy()) > E
        bd = ((1 - pmod) - d.da.to_numpy()) > E
        ask = np.where(bu, d.ua.to_numpy(), np.where(bd, d.da.to_numpy(), np.nan))
        wins = np.where(bu, y, np.where(bd, 1 - y, np.nan))
        m = np.isfinite(ask)
        if m.sum() < 30:
            print("  %-8.2f %9d   (too few)" % (E, m.sum())); continue
        a, wv = ask[m], wins[m]
        pnl = (1.0 / a) * (wv - a - FEE(a))
        print("  %-8.2f %9d %8.1f%% %10.3f %+10.2f %+9.4f"
              % (E, m.sum(), 100*wv.mean(), a.mean(), pnl.sum(), pnl.mean()))
    print()
    print("  ⛔ these price at the TICK's ask. Live pays ~1s later and the")
    print("     crossing arm's edge fell +6.2 -> +0.9 over exactly that second.")
    print("     Treat any positive here as an UPPER BOUND, not a result.")


if __name__ == "__main__":
    main()
