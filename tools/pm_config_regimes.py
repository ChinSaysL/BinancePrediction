"""Which CONFIG was live when each settled fill was taken?

WHY THIS EXISTS: on 2026-09-19 two knobs were shipped on cohorts built by
filtering past fills on their realised price and margin. That is not a
measurement. The 231 settled PM-era fills span SEVEN distinct live configs, and
ZERO of the 139 in the [0.45,0.65] band were taken under the gates those
cohorts were used to justify -- a fill is only IN a bucket because the config of
that moment admitted it. Run this BEFORE trusting any statement of the form
"live fills with property X did Y".

The regime boundaries come from the `.env.bak-*` snapshots the deploy path
writes, keyed by mtime. A knob absent from a snapshot fell back to config.py at
that commit, so a regime string is a lower bound on what differed.

Usage (VPS, analysis venv -- the bot venv has no pandas):
    /root/analysis-venv/bin/python tools/pm_config_regimes.py
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

DB = "/root/BinancePrediction/data/btcpredict.db"
ROOT = "/root/BinancePrediction"
KNOBS = ("MIN_MARGIN_BPS", "MAX_MARGIN_BPS", "MIN_PRICE", "MAX_FILL_PX",
         "MIN_TAU_S", "MAX_TAU_S", "MAX_ROUND_RANGE_BPS", "EXIT_ENABLED")


def regimes() -> list[tuple[float, str]]:
    out = []
    for f in glob.glob(os.path.join(ROOT, ".env.bak-*")) + [os.path.join(ROOT, ".env")]:
        try:
            txt = open(f, errors="ignore").read()
        except OSError:
            continue
        vals = sorted(ln.strip() for ln in txt.splitlines()
                      if ln.startswith("CROSS_") and ln.split("=")[0][6:] in KNOBS)
        out.append((os.path.getmtime(f), " ".join(vals)))
    return sorted(out)


def main() -> None:
    reg = regimes()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    L = pd.read_sql_query("""
      select f.ts_ms, f.filled_px, f.shares, s.pnl
      from fills f join settlements s on s.fill_id = f.id
      join rounds rr on rr.slug = f.slug
      where f.mode in ('cross','train') and s.void = 0 and s.pnl is not null
        and rr.winner in ('Up','Down') order by f.ts_ms
    """, con)
    con.close()

    def which(ts_ms: int) -> str:
        cur = ""
        for m, v in reg:
            if m <= ts_ms / 1000.0:
                cur = v
            else:
                break
        return cur

    L["cfg"] = [which(t) for t in L.ts_ms]
    L["won"] = L.pnl > 0
    L["per"] = L.pnl / L.shares.replace(0, np.nan)
    print(f"settled PM-era fills: {len(L)}   distinct live configs: {L.cfg.nunique()}\n")
    print("  %-4s %-6s %-9s %-10s %s" % ("n", "win%", "pnl", "per-share", "config"))
    for c, d in L.groupby("cfg", sort=False):
        p = d.filled_px.mean()
        be = p + 0.07 * p * (1 - p)
        lab = c or "<no CROSS_* overrides: config.py defaults of the day>"
        print("  %-4d %-6.1f %+-9.2f %+-10.4f %s" % (len(d), 100 * d.won.mean(),
                                                     d.pnl.sum(), d.per.mean(), lab[:70]))
        print("       break-even %.1f%% at mean ask %.3f -> gap %+.1f pts"
              % (100 * be, p, 100 * (d.won.mean() - be)))
    for lo, hi in ((0.45, 0.65),):
        B = L[(L.filled_px >= lo) & (L.filled_px <= hi)]
        print(f"\n  the [{lo},{hi}] cohort: n={len(B)} spanning {B.cfg.nunique()} configs")
        print("  ⛔ any per-knob conclusion drawn from it is confounded with the other "
              f"{B.cfg.nunique() - 1} changes.")


if __name__ == "__main__":
    sys.exit(main())
