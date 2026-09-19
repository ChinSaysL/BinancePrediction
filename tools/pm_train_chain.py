"""The TRAIN trigger, re-measured on the POLYMARKET tape. The note demands it.

`config.cross_train_enabled` carries a table showing 89-95% hit rates, and then
this correction, made by the operator on 2026-09-18:

    ⛔ PROVENANCE CORRECTED: that table is BINANCE-ERA (pre-2026-09-16 flip).
    It does NOT transfer to Polymarket and must not be cited as Polymarket
    evidence for this trigger. The PM-era record is n=2 fills on $2.00 = -$0.72.
    RE-MEASURE on the PM tape before this is enabled.

This is that re-measure.

THE TRIGGER IS DISTANCE, NOT A CROSSING, which is why it cannot reuse
pm_cross_replay: there is no `crossed_side` here. The round's spot travels
`cross_train_bps` from the STRIKE (the round's open) and the arm buys the side
it travelled toward -- a momentum-continuation bet, explicitly overriding three
gates the cross arm relies on (max_round_range_bps, max_depth_bps and
cross_max_fill_px), because a >=15bps entry is deep by construction.

    python3 tools/pm_train_chain.py
"""
from __future__ import annotations

import datetime as dt, math, os, sqlite3, sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FEE = lambda p: 0.07 * p * (1 - p)
EXEC = 0.0038


def load():
    con = sqlite3.connect(f"file:{os.path.join(ROOT,'data','btcpredict.db')}?mode=ro",
                          uri=True)
    r = pd.read_sql_query("""select slug,start_ms,end_ms,strike,winner,feed_provider
        from rounds where strike is not null and winner is not null""", con)
    q = pd.read_sql_query("""select slug,ts_ms,tau_s,spot,up_ask,down_ask
        from quotes where spot is not null order by ts_ms""", con)
    con.close()
    r = r[r.feed_provider.fillna("").str.contains("twap", case=False)]
    r = r[r.winner.isin(["Up", "Down"])]
    return r, q[q.slug.isin(set(r.slug))]


def run(r, q, *, bps, min_tau, max_ask, five_only=True, delay_s=0.0):
    out = []
    byslug = {s: g for s, g in q.groupby("slug", sort=False)}
    for _, row in r.iterrows():
        if five_only and "-5m-" not in row.slug:
            continue
        g = byslug.get(row.slug)
        if g is None or g.empty or not row.strike:
            continue
        k = float(row.strike)
        fired_at = None
        for ts, tau, sp, ua, da in zip(g.ts_ms, g.tau_s, g.spot, g.up_ask, g.down_ask):
            if tau < min_tau or not sp:
                continue
            d = 1e4 * (float(sp) - k) / k
            if abs(d) < bps:
                continue
            if fired_at is None:
                fired_at = ts
                if delay_s > 0:
                    continue
            elif delay_s > 0 and ts - fired_at < delay_s * 1000:
                continue
            side = "Up" if d > 0 else "Down"
            ask = float(ua if side == "Up" else da)
            if not np.isfinite(ask) or not (0 < ask <= max_ask):
                continue
            pay = min(max(ask + EXEC, 0.01), 0.99)
            won = (row.winner == side)
            out.append(dict(
                day=dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%m-%d"),
                ask=pay, won=won,
                per=((1.0 if won else 0.0) - pay - FEE(pay)) / pay))
            break
    return out


def stat(rows):
    if len(rows) < 10:
        return None
    v = np.array([x["per"] for x in rows]); n = len(v); sd = v.std(ddof=1)
    p = float(np.mean([x["ask"] for x in rows]))
    return dict(n=n, win=100*np.mean([x["won"] for x in rows]), ask=p,
                be=100*(p + FEE(p)), tot=v.sum(), per=v.mean(),
                t=v.mean()/(sd/math.sqrt(n)) if sd else 0.0)


def main():
    r, q = load()
    days = sorted({dt.datetime.fromtimestamp(t/1000, dt.UTC).strftime("%m-%d")
                   for t in q.ts_ms})
    TE = set(days[len(days)//2:])
    print(f"  PM tape: {len(r)} settled rounds, days {days}")
    print(f"  test half: {sorted(TE)}\n")
    print("  %-22s %5s %7s %9s %7s %9s %8s"
          % ("config", "n", "win%", "break-even", "margin", "total $", "t"))
    for bps in (10.0, 15.0, 20.0, 25.0):
        rows = run(r, q, bps=bps, min_tau=60.0, max_ask=0.89)
        s = stat(rows)
        if not s:
            print("  %-22s n<10" % f"X>={bps:.0f} (shipped tau)"); continue
        print("  %-22s %5d %6.1f%% %9.1f%% %+7.1f %+9.2f %+8.2f"
              % (f"X>={bps:.0f}bps", s["n"], s["win"], s["be"],
                 s["win"]-s["be"], s["tot"], s["t"]))
    print("\n  === the SHIPPED cell (X>=15, tau>=60, ask<=0.89), held out ===")
    rows = run(r, q, bps=15.0, min_tau=60.0, max_ask=0.89)
    for lab, sub in (("full tape", rows),
                     ("TRAIN days", [x for x in rows if x["day"] not in TE]),
                     ("TEST days", [x for x in rows if x["day"] in TE])):
        s = stat(sub)
        print("  %-22s %s" % (lab, (
            "n=%4d win %5.1f%%  break-even %5.1f%%  margin %+5.1f  $%+7.2f  t%+5.2f"
            % (s["n"], s["win"], s["be"], s["win"]-s["be"], s["tot"], s["t"]))
            if s else "n<10"))
    print("\n  === the delay the Binance table said kills it (D=0 only) ===")
    for d in (0.0, 2.0, 5.0):
        s = stat(run(r, q, bps=15.0, min_tau=60.0, max_ask=0.89, delay_s=d))
        print("  %-22s %s" % (f"delay {d:.0f}s", (
            "n=%4d win %5.1f%%  margin %+5.1f  $%+7.2f"
            % (s["n"], s["win"], s["win"]-s["be"], s["tot"])) if s else "n<10"))
    print("\n  === and a cheaper ceiling, since 0.89 needs 89.7%% ===")
    for ma in (0.89, 0.80, 0.70, 0.65):
        s = stat(run(r, q, bps=15.0, min_tau=60.0, max_ask=ma))
        print("  %-22s %s" % (f"max_ask {ma:.2f}", (
            "n=%4d win %5.1f%%  break-even %5.1f%%  margin %+5.1f  $%+7.2f  t%+5.2f"
            % (s["n"], s["win"], s["be"], s["win"]-s["be"], s["tot"], s["t"]))
            if s else "n<10"))


if __name__ == "__main__":
    main()
