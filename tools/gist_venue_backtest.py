"""The gist strategy, backtested on OUR venue, with REAL asks.

tools/gist_snipe.py implemented the gist's indicators but priced the trades with
an invented curve. This runs the same strategy against our own recorded tape:

  * window delta (the gist's dominant term) from quotes.spot vs rounds.strike
  * micro momentum / acceleration from the venue's own spot path
  * EMA9/EMA21/RSI14 on a 1m spot series built from that same tape (causal only)
  * score = sign(delta)*weight + 2*sign(mom) + 1.5*sign(accel) + 1*(ema) +- rsi
  * confidence = min(|score| / 7, 1)          -- the gist's own formula
  * entry price = the REAL ask of the signalled side at the chosen tau
  * settlement  = rounds.winner, fee = the venue's own fee_rate_bps

Side by side it also reports what the same trades would pay under
  (a) fixed 0.50   -- the screenshot's assumption
  (b) the invented `0.5 + 0.5*min(1, |delta|/0.10)` curve from gist_snipe.py
so the share of the result that is pricing fantasy is explicit.

Battery on every cell: pooled-t, day-clustered t, both halves, drop-best-day.
"""
from __future__ import annotations

import argparse
import math
import sqlite3

import numpy as np
import pandas as pd

WEIGHTS = [(0.10, 7.0), (0.02, 5.0), (0.005, 3.0), (0.001, 1.0)]


def load(db: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rounds = pd.read_sql_query(
        "select slug, symbol, strike, end_ms, winner, fee_rate_bps from rounds "
        "where strike is not null and winner is not null", con)
    q = pd.read_sql_query(
        "select slug, ts_ms, tau_s, spot, up_ask, down_ask from quotes "
        "where spot is not null and up_ask is not null and down_ask is not null",
        con)
    con.close()
    q = q.merge(rounds[["slug", "strike", "winner", "fee_rate_bps", "end_ms",
                        "symbol"]], on="slug", how="inner")
    return q.sort_values(["symbol", "ts_ms"]), rounds


def indicators(q: pd.DataFrame) -> pd.DataFrame:
    """EMA9/21 and RSI14 on a 1m series built from the tape. Causal."""
    q = q.copy()
    q["minute"] = (q["ts_ms"] // 60_000) * 60_000
    ser = q.groupby(["symbol", "minute"], as_index=False)["spot"].last()
    out = []
    for sym, g in ser.groupby("symbol"):
        g = g.sort_values("minute")
        c = g["spot"].to_numpy(dtype=float)
        e9 = pd.Series(c).ewm(span=9, adjust=False).mean().to_numpy()
        e21 = pd.Series(c).ewm(span=21, adjust=False).mean().to_numpy()
        d = np.diff(c, prepend=c[0])
        up = pd.Series(np.where(d > 0, d, 0.0)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
        dn = pd.Series(np.where(d < 0, -d, 0.0)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
        rs = up / np.maximum(dn, 1e-12)
        out.append(pd.DataFrame({"symbol": sym, "minute": g["minute"].to_numpy(),
                                 "ema9": e9, "ema21": e21, "rsi": 100 - 100 / (1 + rs)}))
    return pd.concat(out, ignore_index=True)


def score(delta_pct: float, mom: float, accel: float, ema9: float, ema21: float,
          r: float) -> float:
    a = abs(delta_pct)
    w = 0.0
    for thr, ww in WEIGHTS:
        if a > thr:
            w = ww
            break
    s = math.copysign(w, delta_pct) if w else 0.0
    s += 2.0 * math.copysign(1.0, mom) if mom else 0.0
    s += 1.5 * math.copysign(1.0, accel) if accel else 0.0
    s += 1.0 if ema9 > ema21 else -1.0
    if r > 75:
        s -= 2.0
    elif r < 25:
        s += 2.0
    return s


def fee(px: np.ndarray, rate_bps: np.ndarray) -> np.ndarray:
    r = rate_bps / 1e4
    return r * np.minimum(1.0, (1.0 - px) / np.maximum(px, 1e-9))


def build(q: pd.DataFrame, ind: pd.DataFrame, tau: float, min_conf: float,
          always: bool) -> pd.DataFrame:
    q = q.copy()
    q["minute"] = (q["ts_ms"] // 60_000) * 60_000
    q = q.merge(ind, on=["symbol", "minute"], how="left")
    rows = []
    for slug, g in q.groupby("slug"):
        g = g.sort_values("ts_ms")
        at = g[g["tau_s"] >= tau]
        if at.empty:
            continue
        e = at.iloc[-1]                      # latest quote at or past tau = closest to T-tau
        hist = g[g["ts_ms"] <= e["ts_ms"]]
        if len(hist) < 5:
            continue
        strike = float(e["strike"])
        delta = (float(e["spot"]) - strike) / strike * 100.0
        sp = hist["spot"].to_numpy(dtype=float)
        t = hist["ts_ms"].to_numpy(dtype=float)
        def chg(sec: float) -> float:
            m = t >= t[-1] - sec * 1000.0
            return sp[m][-1] / sp[m][0] - 1.0 if m.sum() >= 2 else 0.0
        mom = chg(120.0)
        accel = chg(60.0) - chg(120.0)
        s = score(delta, mom, accel, float(e["ema9"]), float(e["ema21"]),
                  float(e["rsi"]))
        conf = min(abs(s) / 7.0, 1.0)
        if not always and conf < min_conf:
            continue
        side_up = s > 0
        px = float(e["up_ask"] if side_up else e["down_ask"])
        if not (0 < px < 1):
            continue
        w = str(e["winner"]).strip().lower()
        win = (side_up and w.startswith("up")) or ((not side_up) and w.startswith("down"))
        rows.append({"slug": slug, "tau": float(e["tau_s"]), "delta": delta,
                     "score": s, "conf": conf, "px": px, "win": win,
                     "rate": float(e["fee_rate_bps"]), "day": int(e["end_ms"] // 86_400_000),
                     "px_fixed": 0.50,
                     "px_curve": min(0.99, 0.50 + 0.50 * min(1.0, abs(delta) / 0.10))})
    return pd.DataFrame(rows)


def ev_of(r: pd.DataFrame, pxcol: str) -> np.ndarray:
    p = r[pxcol].to_numpy(dtype=float)
    return np.where(r["win"].to_numpy(), (1.0 - p) / p, -1.0) - fee(p, r["rate"].to_numpy())


def battery(ev: np.ndarray, days: np.ndarray) -> dict:
    s = {"n": len(ev), "ev": float(ev.mean())}
    sd = ev.std(ddof=1) if len(ev) > 1 else 0.0
    s["t"] = float(ev.mean() / (sd / math.sqrt(len(ev)))) if sd > 0 else float("nan")
    d = pd.DataFrame({"ev": ev, "day": days})
    g = d.groupby("day")["ev"].mean()
    s["ndays"] = len(g)
    s["t_day"] = (float(g.mean() / (g.std(ddof=1) / math.sqrt(len(g))))
                  if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = len(ev) // 2
    s["h1"], s["h2"] = float(ev[:h].mean()), float(ev[h:].mean())
    s["drop1"] = float(d[d["day"] != g.idxmax()]["ev"].mean())
    s["survives"] = (s["ev"] > 0.02 and s["t"] > 1.5 and s["t_day"] > 1.5
                     and s["h1"] > 0 and s["h2"] > 0 and s["drop1"] > 0)
    return s


def line(tag: str, s: dict) -> str:
    return (f"{tag:30s} n={s['n']:5d}  EV/$1 {s['ev']:+.4f}  t {s['t']:+.2f}  "
            f"day-t {s['t_day']:+.2f}  halves {s['h1']:+.3f}/{s['h2']:+.3f}  "
            f"drop1 {s['drop1']:+.3f}" + ("   <-- SURVIVES" if s["survives"] else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--taus", type=float, nargs="+", default=[5, 10, 30, 60])
    ap.add_argument("--conf", type=float, default=0.30, help="gist safe mode")
    ap.add_argument("--always", action="store_true", help="gist: never skip a trade")
    args = ap.parse_args()

    q, rounds = load(args.db)
    ind = indicators(q)
    print(f"tape: {len(q):,} quotes with real asks | {q['slug'].nunique()} rounds | "
          f"{q['symbol'].nunique()} symbols | "
          f"{pd.to_datetime(q['end_ms'], unit='ms', utc=True).dt.date.nunique()} days | "
          f"conf>={args.conf}{' (ALWAYS TRADE)' if args.always else ''}\n")
    for tau in args.taus:
        r = build(q, ind, tau, args.conf, args.always)
        if r.empty:
            print(f"tau {tau:.0f}s: no trades\n")
            continue
        days = r["day"].to_numpy()
        print(f"=== gist strategy @ tau {tau:.0f}s "
              f"(real tau {r['tau'].mean():.1f}s, avg ask {r['px'].mean():.3f}, "
              f"WR {r['win'].mean():.1%}) ===")
        print(line("REAL asks (our venue)", battery(ev_of(r, "px"), days)))
        print(line("  same trades @ fixed 0.50", battery(ev_of(r, "px_fixed"), days)))
        print(line("  same trades @ invented curve", battery(ev_of(r, "px_curve"), days)))
        for lo, hi in [(0.0, 0.005), (0.005, 0.02), (0.02, 0.05), (0.05, 0.10),
                       (0.10, 99.0)]:
            c = r[(r["delta"].abs() >= lo) & (r["delta"].abs() < hi)]
            if len(c) < 30:
                continue
            s = battery(ev_of(c, "px"), c["day"].to_numpy())
            print(f"   |delta| {lo:>5.3f}-{hi:<5.2f}%  WR {c['win'].mean():6.1%} "
                  f"ask {c['px'].mean():.3f}  " + line("", s).strip())
        print()


if __name__ == "__main__":
    main()
