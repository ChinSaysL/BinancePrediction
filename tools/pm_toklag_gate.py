"""A PER-TOKEN BOOK-LAG GATE: proposed, measured, REFUTED. Do not ship it.

THE PROPOSAL (2026-09-18). `PmBook.lag_ms(asset_id=...)` measures how far the
newest VENUE timestamp for one token sits behind our clock. It exists because
`Touch.age_s()` cannot see this failure -- `recv_ms` dates when the frame
reached US, so a consumer 0.9 s behind still reports a few milliseconds -- and
book.py says so outright: "Both are needed; only this one can fail." It is
wired into the shadow log and it GATES NOTHING. The order path's only freshness
bound is `max_book_age_s = 2.0`, which is the measure that cannot fail.

The shadow probe showed |ours - venue| ask gaps rising cleanly with it
(0.0200 under 300 ms, 0.0650 at 300-600, 0.1300 at 1-5 s), so refusing a fire
whose token book is stale looked like a cheap tail fix.

⛔ IT DOES NOT SURVIVE THE FILLS. Matching every shadow line to the fill it
belongs to (same slug+side within 8 s) gives 95 settled fills:

    ALL matched fills      n=95  total -12.824  per-share -0.0585
    tok-lag < 300ms   kept n= 7  total  -3.752  per-share -0.2461
    tok-lag < 500ms   kept n=70  total  -8.516  per-share -0.0496
    tok-lag < 600ms   kept n=78  total  -6.303  per-share -0.0337
    tok-lag < 800ms   kept n=85  total  -9.139  per-share -0.0458
    tok-lag <2000ms   kept n=91  total -11.972  per-share -0.0567

600 ms is a SPIKE, not a plateau: 500 and 800 are both worse than the 600 cell
between them. The 8 fills in [500,600) earned +2.213 and the 7 in [600,800)
earned -2.836 -- that is the whole "effect", and it is two handfuls of trades.

AND THE DIRECTION IS WRONG AT THE FRESH END, which is what actually kills it:

    tok-lag [  0, 300)  n= 7  per-share -0.2461  win 29%  |gap| 0.0100
    tok-lag [300, 500)  n=63  per-share -0.0304  win 49%  |gap| 0.0300
    tok-lag [500, 800)  n=15  per-share -0.0223  win 53%  |gap| 0.0400
    tok-lag [800, inf)  n=10  per-share -0.1867  win 40%  |gap| 0.0700

The FRESHEST books are the WORST trades. |gap| rises monotonically with lag
exactly as the shadow log said -- so lag genuinely predicts how wrong our book
is -- but it does not predict the P&L, and at the fresh end it predicts it
BACKWARDS.

THE LIKELY WHY, stated as a hypothesis and not as a result: a fresh token book
means a QUIET token, and a crossing on a quiet token is a weak signal; a laggy
book means a fast-moving one, which is where the real moves are. The worse fill
and the better signal offset. If that is right, no gate on lag alone can work --
it is a proxy for two things that pull opposite ways.

WHAT THIS DOES NOT REFUTE: the gap itself is real and directional (70% of fires
have our ask CHEAPER than the venue's, median -0.055). It is just not
actionable through this variable.

BOUNDS: 95 of 211 fills carry a shadow line, so the matched set is a selected
subset, and the buckets are n=7-63.

    /root/analysis-venv/bin/python tools/pm_toklag_gate.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import statistics as st

LOG = "/root/BinancePrediction/data/live.log"
DB = "/root/BinancePrediction/data/btcpredict.db"
PAT = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+).*shadow book (\S+) "
                 r"(\w+): venue ([\d.]+)/([\d.]+) \| ours ([\d.]+)/([\d.]+) "
                 r"\(([-+\d.]+)\).*?age (\d+)ms tok-lag (-?\d+)ms")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    sh = []
    for ln in open(args.log, errors="ignore"):
        m = PAT.search(ln)
        if not m:
            continue
        ts, ms, slug, side, _vb, _va, _ob, _oa, d, age, tok = m.groups()
        t = dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc)
        sh.append(dict(ms=int(t.timestamp() * 1000) + int(ms), slug=slug,
                       side=side, tok=int(tok), age=int(age), gap=float(d)))
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fills = list(con.execute("""
      select f.id,f.slug,f.side,f.ts_ms,f.shares,s.pnl
      from fills f left join settlements s on s.fill_id=f.id
      where f.mode in ('cross','train') order by f.ts_ms"""))
    matched = []
    for s in sh:
        best = None
        for f in fills:
            if f["slug"] != s["slug"] or f["side"] != s["side"]:
                continue
            d = abs(f["ts_ms"] - s["ms"])
            if d <= 8000 and (best is None or d < best[0]):
                best = (d, f)
        if best and best[1]["pnl"] is not None:
            matched.append((s, best[1]))
    print(f"shadow lines {len(sh)} | settled fills matched {len(matched)}")
    if not matched:
        print("not enough data")
        return
    tot = sum(f["pnl"] for _, f in matched)
    shs = sum(f["shares"] or 0 for _, f in matched)
    print(f"  ALL matched            n={len(matched):3d}  total {tot:+8.3f}  "
          f"per-share {tot/shs if shs else float('nan'):+.4f}")
    for thr in (300, 400, 500, 600, 800, 1000, 2000):
        keep = [(s, f) for s, f in matched if s["tok"] < thr]
        drop = [(s, f) for s, f in matched if s["tok"] >= thr]
        if not drop or not keep:
            continue
        kt = sum(f["pnl"] for _, f in keep)
        ks = sum(f["shares"] or 0 for _, f in keep)
        print(f"  tok-lag < {thr:5d}ms  kept n={len(keep):3d} total {kt:+8.3f} "
              f"per-share {kt/ks if ks else float('nan'):+.4f} | refused "
              f"n={len(drop):3d} total {sum(f['pnl'] for _, f in drop):+8.3f}")
    print("\n=== does it predict the OUTCOME, or only the book gap? ===")
    for lo, hi in ((0, 300), (300, 500), (500, 800), (800, 10**9)):
        sel = [(s, f) for s, f in matched if lo <= s["tok"] < hi]
        if len(sel) < 3:
            continue
        p = [f["pnl"] for _, f in sel]
        ss = sum(f["shares"] or 0 for _, f in sel)
        print(f"  tok-lag [{lo:4d},{hi if hi < 10**8 else 'inf':>5}) n={len(sel):3d} "
              f"total {sum(p):+7.3f} per-share {sum(p)/ss if ss else float('nan'):+.4f} "
              f"win {100*sum(1 for x in p if x > 0)/len(p):3.0f}% "
              f"|gap| {st.median([abs(s['gap']) for s, _ in sel]):.4f}")
    print("\n  REFUTED: |gap| rises with lag, the P&L does not -- and the")
    print("  freshest bucket is the worst. See this file's docstring.")


main()
