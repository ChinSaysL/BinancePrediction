#!/usr/bin/env python3
"""Two structural ideas from the "Last Look" batch, tested on the live DB.

IDEA 2 -- CROSS-CONTRACT LAG. Claim: the BTC dealer engine reprices first, and
the ETH/BNB prediction contracts lag by 200-500ms, so a BTC break can be traded
in the ETH/BNB contract. Test: regress the ETH/BNB contract's own next-second
mid change on (recent BTC spot move, recent OWN spot move). A dealer lag shows up
as a POSITIVE, significant BTC coefficient. The own-spot coefficient is the
control: it says how much of the contract's move its OWN underlying explains.

IDEA 3 -- DELAYED PULLBACK ENTRY. Claim: do not buy the expansion (the venue's
2x slippage cap blocks it anyway); wait for a 1s pullback against a multi-second
trend and fire during the pullback. Test: require |10s trend| >= 3 bps and a 1s
move of >= 0.5 bps against it, then buy the trend side at its ASK, per share,
day-clustered.

Read-only. Uses ONE merged (ts, spot) series per symbol, so it is O(n log n).
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import math
import sqlite3

import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict_vps.db")
    ap.add_argument("--horizon-ms", type=int, default=1000)
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    c.row_factory = sqlite3.Row
    lab: dict[str, str] = {}
    day: dict[str, str] = {}
    for r in c.execute("select slug, start_ms, winner from rounds where winner is not null"):
        lab[r["slug"]] = r["winner"]
        day[r["slug"]] = dt.datetime.fromtimestamp(int(r["start_ms"]) / 1000, dt.UTC).strftime("%m-%d")

    series: dict[str, list[tuple[int, float, float, float]]] = collections.defaultdict(list)
    spotmerge: dict[str, dict[int, float]] = collections.defaultdict(dict)
    for r in c.execute("""select q.slug, q.ts_ms, q.spot, q.up_ask, q.down_ask
                          from quotes q
                          where q.spot is not null and q.up_ask is not null
                            and q.down_ask is not null"""):
        slug = r["slug"]
        series[slug].append((int(r["ts_ms"]), r["spot"], r["up_ask"], r["down_ask"]))
        sym = slug.split("-")[0]
        spotmerge[sym][int(r["ts_ms"])] = r["spot"]
    for k in series:
        series[k].sort()
    mts: dict[str, list[int]] = {}
    mpx: dict[str, list[float]] = {}
    for sym, d in spotmerge.items():
        items = sorted(d.items())
        mts[sym] = [t for t, _ in items]
        mpx[sym] = [p for _, p in items]

    def spot_at(sym: str, ts: int, tol: int = 600) -> float | None:
        t, p = mts.get(sym), mpx.get(sym)
        if not t:
            return None
        i = bisect.bisect_right(t, ts) - 1
        if i < 0 or ts - t[i] > tol:
            return None
        return p[i]

    def row_at(rows, ts):
        i = bisect.bisect_right([x[0] for x in rows], ts) - 1
        return rows[i] if i >= 0 else None

    H = args.horizon_ms
    print(f"{args.db}   horizon {H} ms")

    # ---------------- IDEA 2 ----------------
    print("\nIDEA 2: does a BTC break LEAD the ETH/BNB contract?  "
          "(dep: contract's own next-1s mid change)")
    print(f"  {'asset':>5} {'n':>7} {'BT C coef':>10} {'t':>7} | {'own-spot coef':>14} "
          f"{'t':>7} | {'corr(BTC, next)':>16}")
    for pre in ("eth", "bnb"):
        X, Y = [], []
        for slug, rows in series.items():
            if not slug.startswith(pre) or "updown-5m" not in slug:
                continue
            for i in range(1, len(rows) - 1):
                t, sp, ua, da = rows[i]
                b0 = spot_at("btc", t)
                b1 = spot_at("btc", t - H)
                o1 = row_at(rows, t - H)
                if not b0 or not b1 or not o1:
                    continue
                nxt = None
                for k2 in range(i + 1, len(rows)):
                    if rows[k2][0] - t >= H * 0.9:
                        nxt = rows[k2]
                        break
                if not nxt:
                    continue
                up_now = ua
                up_fut = nxt[2]
                X.append([(b0 - b1) / b1 * 1e4, (sp - o1[1]) / o1[1] * 1e4, 1.0])
                Y.append(up_fut - up_now)
        if len(Y) < 200:
            print(f"  {pre:>5} too few ({len(Y)})")
            continue
        A = np.asarray(X, float)
        Yv = np.asarray(Y, float)
        inv = np.linalg.pinv(A.T @ A)
        beta = inv @ (A.T @ Yv)
        res = Yv - A @ beta
        s2 = float(res @ res) / max(len(Yv) - 3, 1)
        se = np.sqrt(np.diag(s2 * inv))
        corr = float(np.corrcoef(A[:, 0], Yv)[0, 1])
        print(f"  {pre:>5} {len(Yv):>7} {beta[0]:>10.5f} {beta[0]/se[0]:>+7.2f} | "
              f"{beta[1]:>14.5f} {beta[1]/se[1]:>+7.2f} | {corr:>16.4f}")
    print("  (a dealer lag = POSITIVE significant BTC coefficient.)")

    # ---- IDEA 2b: the TRADE, not the regression: BTC break -> buy ETH/BNB ----
    # The claim is a 200-500ms dealer lag. Our own venue ack is 185-332ms and a
    # fill takes 2-15s, so the only version that matters is: signal at t, enter
    # at the ask at t+LAG. If the edge is gone by LAG, we could not have had it.
    print("\nIDEA 2b: the actual TRADE -- BTC break in the last 1s, buy the ETH/BNB")
    print("          contract on the implied side, at its ASK, after a realistic lag")
    print(f"  {'thr bps':>8} {'lag ms':>7} {'n':>7} {'ask':>7} {'realized':>9} "
          f"{'edge':>8} {'day-t':>7}")
    for thr in (3.0, 6.0, 12.0):
        for lag in (0, 1000, 2000):
            out2 = []
            for slug, rows in series.items():
                if not (slug.startswith("eth") or slug.startswith("bnb")):
                    continue
                if "updown-5m" not in slug:
                    continue
                y = lab.get(slug)
                if y is None:
                    continue
                for i in range(1, len(rows) - 1):
                    t, sp, ua, da = rows[i]
                    b0 = spot_at("btc", t)
                    b1 = spot_at("btc", t - 1000)
                    if not b0 or not b1:
                        continue
                    br = (b0 - b1) / b1 * 1e4
                    if abs(br) < thr:
                        continue
                    fav_up = br > 0
                    ent = row_at(rows, t + lag) if lag else rows[i]
                    if not ent:
                        continue
                    ask = ent[2] if fav_up else ent[3]
                    won = (y == "Up") if fav_up else (y == "Down")
                    out2.append(dict(day=day[slug], edge=(1.0 if won else 0.0) - ask,
                                     ask=ask, won=1.0 if won else 0.0))
            if len(out2) < 100:
                print(f"  {thr:>8.1f} {lag:>7} {len(out2):>7}   (too few)")
                continue
            bd2 = collections.defaultdict(list)
            for r in out2:
                bd2[r["day"]].append(r["edge"])
            dm2 = [float(np.mean(v)) for v in bd2.values()]
            a = float(np.mean([r["ask"] for r in out2]))
            e = float(np.mean([r["edge"] for r in out2]))
            tstat = (float(np.mean(dm2)) / (float(np.std(dm2, ddof=1)) / math.sqrt(len(dm2)))
                     if len(dm2) > 1 and np.std(dm2, ddof=1) > 0 else 0.0)
            print(f"  {thr:>8.1f} {lag:>7} {len(out2):>7} {a:>7.4f} "
                  f"{np.mean([r['won'] for r in out2]):>9.4f} {e:>+8.4f} {tstat:>+7.2f}")

    # ---------------- IDEA 3 ----------------
    print("\nIDEA 3: buy the TREND side, but only DURING a 1s pullback against it")
    out = []
    for slug, rows in series.items():
        y = lab.get(slug)
        if y is None or len(rows) < 10:
            continue
        for i in range(4, len(rows) - 1):
            t, sp, ua, da = rows[i]
            q10 = row_at(rows, t - 10_000)
            q1 = row_at(rows, t - 1_000)
            if not q10 or not q1:
                continue
            trend = (sp - q10[1]) / q10[1] * 1e4
            last1 = (sp - q1[1]) / q1[1] * 1e4
            if abs(trend) < 3:
                continue
            tdir = 1 if trend > 0 else -1
            if tdir * last1 > -0.5:
                continue
            fav_up = tdir > 0
            ask = ua if fav_up else da
            won = (y == "Up") if fav_up else (y == "Down")
            out.append(dict(day=day[slug], edge=(1.0 if won else 0.0) - ask,
                            ask=ask, trend=trend))
    print(f"  signals: {len(out)}")
    if out:
        bd = collections.defaultdict(list)
        for r in out:
            bd[r["day"]].append(r["edge"])
        dm = [float(np.mean(v)) for v in bd.values()]
        a = float(np.mean([r["ask"] for r in out]))
        e = float(np.mean([r["edge"] for r in out]))
        t = (float(np.mean(dm)) / (float(np.std(dm, ddof=1)) / math.sqrt(len(dm)))
             if len(dm) > 1 and np.std(dm, ddof=1) > 0 else 0.0)
        print(f"  ask {a:.4f}  edge/share {e:+.4f}  day-t {t:+.2f}  "
              f"({sum(1 for x in dm if x > 0)}/{len(dm)} days +)")
        for lo, hi in [(3, 6), (6, 10), (10, 25), (25, 1e9)]:
            g = [r for r in out if lo <= abs(r["trend"]) < hi]
            if len(g) < 40:
                continue
            print(f"    |10s trend| {lo:>3}-{hi:<5} n={len(g):5d} "
                  f"ask {np.mean([r['ask'] for r in g]):.4f} "
                  f"edge {np.mean([r['edge'] for r in g]):+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
