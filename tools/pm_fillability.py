"""Is the price the replay pays a price that still exists?

WHY THIS EXISTS: every tape sweep prices an entry at `quotes.up_ask`/`down_ask`
-- the WEBSOCKET book, which is what the GATE reads. So every sweep prefers the
prices that are least fillable, and on 2026-09-19 a 768-cell joint search wanted
the price floor opened to 0.10 on exactly that basis.

It is a mirage. A crossing IS the instant a stale cheap quote is about to
reprice: in book [0.02,0.20) the quote at the entry instant is 2.4 s old and the
same side's ask is +0.54 two seconds later, against 0.1 s old and +0.018 in
[0.55,0.65). Priced at the quoted ask the sub-0.45 cohort is +$58.42; priced at
the ask 2 s later, -$17.13.

The right question is therefore NOT "how much do we pay over the book" but
"how old is this quote, and where does it go next". This answers the second.

Usage (VPS, analysis venv):
    /root/analysis-venv/bin/python tools/pm_fillability.py
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/BinancePrediction")
sys.path.insert(0, "/root/BinancePrediction/tools")

import pm_cross_replay as base  # noqa: E402
from btcpredict.main import crossed_side  # noqa: E402

BANDS = ((0.02, 0.20), (0.20, 0.30), (0.30, 0.45), (0.45, 0.55), (0.55, 0.65))


def main() -> None:
    look, hold = base.shipped_look_s(), base.shipped_hold_s()
    r, q = base.load(base.DB)
    q = q.copy()
    q["fam"] = q.slug.map(base.family_of)
    rows = []
    for _, row in r.iterrows():
        pre = base.precompute(q, row, look, 0.0, 1.0, 0.0, 1e9)
        if pre is None:
            continue
        lvl, src, src_ts = pre["level"], pre["fc"], pre["fc_ts"]
        if not src or not lvl:
            continue
        g = pre["rows"]
        ts_a, ua, da = g.ts_ms.to_numpy(), g.up_ask.to_numpy(), g.down_ask.to_numpy()
        tail = max(10, int((hold + look + 2.0) * 1.6) + 6)
        for i, (ts, tau) in enumerate(zip(ts_a, g.tau_s.to_numpy())):
            j = int(np.searchsorted(src_ts, int(ts), side="right"))
            if j < 4:
                continue
            side = crossed_side(src[max(0, j - tail):j], lvl, look, hold_s=hold)
            if side is None:
                continue
            if abs(1e4 * (src[j - 1][1] - lvl) / lvl) < 0.50 or not 5.0 <= tau <= 240.0:
                continue
            a = ua if side == "Up" else da
            book = float(a[i])
            if not np.isfinite(book) or not 0.02 < book < 0.65:
                continue
            k = i
            while k > 0 and a[k - 1] == book:      # how long this quote has stood
                k -= 1
            nxt = lambda s: float(a[min(int(np.searchsorted(ts_a, ts + s * 1000,
                                                            side="left")), len(a) - 1)])
            rows.append(dict(book=book, age=(ts - ts_a[k]) / 1000.0, p2=nxt(2),
                             p5=nxt(5), won=(row.winner == side)))
            break
    D = pd.DataFrame(rows)
    print(f"replay entries: {len(D)}\n")
    print("  %-14s %6s %9s %9s %10s %8s" % ("book at entry", "n", "quote age",
                                            "ask +2s", "ask +5s", "win%"))
    for lo, hi in BANDS:
        d = D[(D.book >= lo) & (D.book < hi)]
        if len(d) < 10:
            continue
        print("  [%.2f,%.2f)    %6d %8.1fs %+9.4f %+10.4f %7.1f%%"
              % (lo, hi, len(d), d.age.mean(), (d.p2 - d.book).mean(),
                 (d.p5 - d.book).mean(), 100 * d.won.mean()))
    print("\n=== the same entries, priced three ways ===")
    for lo, hi, lab in ((0.02, 0.45, "below 0.45 (refused by cross_min_price)"),
                        (0.45, 0.65, "0.45-0.65 (the shipped band)")):
        d = D[(D.book >= lo) & (D.book < hi)]
        if len(d) < 10:
            continue
        print(f"  {lab}  n={len(d)}")
        for col, cl in (("book", "at the quoted ask (what every sweep assumes)"),
                        ("p2", "at the ask 2 s later"), ("p5", "at the ask 5 s later")):
            p = d[col].clip(0.01, 0.99)
            per = np.where(d.won, 1.0, 0.0) - p - 0.07 * p * (1 - p)
            print("      %-44s per=%+.4f tot=%+8.2f" % (cl, per.mean(), per.sum()))


if __name__ == "__main__":
    sys.exit(main())
