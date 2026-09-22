"""7-day PnL forecast on a NORMAL MONTH, from Bars only.

Corpus: data/bars_1m/<SYM>_1m_30d.csv -- 30 consecutive days, 2026-08-13 15:00
.. 2026-09-12 15:00, BTC/ETH/SOL/XRP/DOGE, 1-minute, zero gaps. Four of every
weekday and four of every weekend day, so the day mix a 7-day bootstrap draws
from is a normal month rather than whichever scattered dates happened to carry
quotes somewhere else.

Sub-minute path comes from data/bars_1s (monthly 2026-08 + daily 2026-09-01..12
over the same days), because the round-range gate is measured from the tick path
and a 1-minute grid cannot resolve it.

What is REPLAYED, not assumed
-----------------------------
* sigma: the repo's own `VolEstimator`, warm-started on 240 completed bars and
  then fed one bar at a time -- the same object, the same EWMA+seasonality blend
  the live arm runs. Never the replay's `rolling_sigma` and never a live number.
* fair: the repo's `fair_value` (the `digital` model), with S the close of the
  last COMPLETED 1-minute bar at the decision instant and K the price at the
  round's open. Strictly causal: a bar closing at t is used at t, never before.
* outcome: the venue's own rule, end >= strike -> Up, on Binance 1m as the
  Chainlink proxy (measured 0.4% direction disagreement, median basis 0.00 bps).
* the gate: the repo's real `evaluate()`, with this run's knobs. No reimplementation.

What CANNOT be replayed here, and is therefore swept
----------------------------------------------------
There is no venue order book in these files, so the ASK is unknown. Rather than
invent one, the whole quote band is priced: for each p in --grid the tool builds
the synthetic book (up=p, down=1-p), runs the real gate against it, and reports
the realised win rate and EV/$1 of the rounds that would have been taken AT THAT
PRICE. Break-even is p*(1+fee), so a row is profitable iff its win rate sits
above its own paid price. Read the row for the price the venue actually charges.

This is the honest shape: everything except the price is data, and the price is
a one-dimensional sweep you can point at.

Usage:
    python3 -m tools.forecast_30d --sym BTCUSDT --range 5 16
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import zipfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcpredict.config import settings as S                       # noqa: E402
from btcpredict.edge.signal import evaluate, fee_rate_for_price   # noqa: E402
from btcpredict.pricing.digital import fair_value                 # noqa: E402
from btcpredict.pricing.vol import VolEstimator                   # noqa: E402

SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
ROUND_S = 300
WARMUP = 240                     # live seeds 240 bars then never re-seeds
TAUS = (60.0, 120.0, 180.0)      # live scans tau downward inside [60, 180]


def read_1m(sym: str) -> pd.DataFrame:
    p = f"data/bars_1m/{sym}_1m_30d.csv"
    d = pd.read_csv(p, usecols=["open_time", "open", "high", "low", "close",
                                "ts"])
    d["ts"] = d["ts"].astype("int64")
    d = (d.drop_duplicates("ts").sort_values("ts")
           .reset_index(drop=True))
    return d


def read_1s(sym: str, lo_s: int, hi_s: int) -> tuple[np.ndarray, np.ndarray]:
    """(second, close) over [lo_s, hi_s] from the monthly + daily archives."""
    out = []
    paths = [f"data/bars_1s/monthly/{sym}-1s-2026-08.zip"]
    paths += [f"data/bars_1s/daily/{sym}-1s-2026-09-{d:02d}.zip"
              for d in range(1, 13)]
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with zipfile.ZipFile(p) as z:
                name = z.namelist()[0]
                with z.open(name) as fh:
                    df = pd.read_csv(fh, header=None, usecols=[0, 3, 4],
                                     names=["t", "low", "close"])
        except Exception:
            continue
        t = df["t"].to_numpy(dtype=np.int64)
        if t[0] > 10 ** 14:                 # microseconds
            t = t // 1_000_000
        elif t[0] > 10 ** 11:               # milliseconds
            t = t // 1_000
        keep = (t >= lo_s) & (t <= hi_s)
        if keep.any():
            out.append((t[keep], df["close"].to_numpy(dtype=np.float64)[keep],
                        df["low"].to_numpy(dtype=np.float64)[keep]))
    if not out:
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0)
    t = np.concatenate([o[0] for o in out])
    c = np.concatenate([o[1] for o in out])
    l = np.concatenate([o[2] for o in out])
    o = np.argsort(t)
    return t[o], c[o], l[o]


def replay(sym: str, *, min_range: float, max_range: float,
           half_spread: float) -> pd.DataFrame:
    """Every (round, tau) decision the live model would have faced."""
    d = read_1m(sym)
    ts = d["ts"].to_numpy()
    op = d["open"].to_numpy()
    cl = d["close"].to_numpy()
    hi = d["high"].to_numpy()
    lo = d["low"].to_numpy()

    vol = VolEstimator()
    known: dict[int, int] = {}          # ts -> row index of that 1m bar
    for i, t in enumerate(ts[:WARMUP]):
        vol.add_bar(int(t) * 1000, math.log(cl[i] / op[i]),
                    hour=int((t // 3600) % 24))
    vol._recompute_seasonality()        # live does this once, at seed time
    for i, t in enumerate(ts):
        known[int(t)] = i

    t1, c1, l1 = read_1s(sym, int(ts[WARMUP]), int(ts[-1]))
    print(f"  {sym}: 1m bars {len(ts):,} | 1s samples {len(t1):,} "
          f"({len(t1) / 86400:.1f} days worth)")

    first = int(ts[WARMUP] // ROUND_S) * ROUND_S
    last = int(ts[-1] // ROUND_S) * ROUND_S
    rows = []
    # ingest bar i, then act on the rounds whose decision instants fall at or
    # after that bar's close.
    idx = WARMUP
    for start in range(first, last, ROUND_S):
        j = known.get(start)
        if j is None or start + 240 not in known:
            continue
        strike = float(op[j])
        end_px = float(cl[known[start + 240]])
        y_up = int(end_px >= strike)
        # walk the bars of this round up to each decision instant
        for tau in TAUS:
            dec = start + (ROUND_S - int(tau))
            # ingest every 1m bar that has CLOSED by `dec`
            while idx < len(ts) and ts[idx] + 60 <= dec:
                vol.add_bar(int(ts[idx]) * 1000, math.log(cl[idx] / op[idx]),
                            hour=int((ts[idx] // 3600) % 24))
                idx += 1
            k = idx - 1
            if k < WARMUP or ts[k] + 60 > dec:
                continue
            S_now = float(cl[k])
            sigma = vol.sigma_1m(hour=int((dec // 3600) % 24))
            if S_now <= 0 or strike <= 0 or sigma <= 0:
                continue
            fair = fair_value(S_now, strike, tau, sigma)
            # round-range gate: max-min of the 1s path since the round opened
            m = (t1 >= start) & (t1 < dec)
            if not m.any():
                continue
            rng = 1e4 * (float(c1[m].max()) - float(l1[m].min())) / strike
            if max_range > 0 and not (min_range <= rng <= max_range):
                continue
            rows.append({"day": start // 86_400, "start": start, "tau": tau,
                         "dec_s": dec, "fair": fair, "sigma": sigma,
                         "S": S_now, "strike": strike, "rng": rng,
                         "y_up": y_up})
    df = pd.DataFrame(rows)
    return df, t1, c1


def _stale(t1: np.ndarray, c1: np.ndarray, dec_s: int,
           lag: float) -> float | None:
    """The spot the venue would have been quoting from, `lag` seconds ago."""
    if lag <= 0 or len(t1) == 0:
        return None
    k = int(np.searchsorted(t1, dec_s - lag, side="right")) - 1
    if k < 0 or (dec_s - lag) - t1[k] > 30:
        return None
    return float(c1[k])


def gate_lagged(df: pd.DataFrame, t1: np.ndarray, c1: np.ndarray, *,
                lag: float, half_spread: float, offset: float = 0.0) -> pd.DataFrame:
    """Run the real gate against a book that prices off a STALE spot.

    THE BUG THIS REPLACES. An earlier version of this tool held the ask at a
    constant `p` and swept p. That is not a book, it is a price freeze: with the
    model's fair free to move to 0.90 while the venue allegedly still sold at
    0.68, the gate reports a huge edge on every round where the price has already
    travelled -- and it reported +0.36/$1, t +41, WR 93.9% at p=0.68. Nothing
    was wrong with the arithmetic; the construction was. A venue that quotes a
    side whose fair is 0.90 at 0.68 does not exist.

    What replaces it is the mechanism the arm actually trades: the venue's book
    is OUR OWN fair computed from the spot as it was `lag` seconds ago. At
    lag=0 the book is exactly our fair, the gate sees zero edge, and EV = -fee
    -- which is the check that this is now a real book and not a freeze.
    """
    if df.empty:
        return df
    out = []
    for r in df.itertuples(index=False):
        if lag <= 0:
            q_mid = float(r.fair)
        else:
            s_lag = _stale(t1, c1, int(r.dec_s), lag)
            if s_lag is None or s_lag <= 0:
                continue
            q_mid = fair_value(s_lag, float(r.strike), r.tau + lag, r.sigma)
        q_mid = float(min(0.999, max(0.001, q_mid + offset)))
        sig = evaluate(float(r.fair), q_mid, 1.0 - q_mid,
                       half_spread=half_spread,
                       safety_margin=S.safety_margin,
                       min_edge=S.min_edge,
                       min_quote=S.min_quote, max_quote=S.max_quote,
                       edge_haircut=S.edge_haircut)
        if not sig.act or sig.side is None:
            continue
        paid = min(0.999, sig.quote + half_spread)
        fee = fee_rate_for_price(paid)
        won = int(r.y_up == (1 if sig.side == "Up" else 0))
        out.append({"day": r.day, "start": r.start, "tau": r.tau,
                    "fair": r.fair, "q_mid": q_mid, "side": sig.side,
                    "edge": sig.edge, "paid": paid, "fee": fee,
                    "won": won, "y_up": r.y_up, "rng": r.rng,
                    "ret": (won / paid - 1.0 - fee)})
    g = pd.DataFrame(out)
    if g.empty:
        return g
    return (g.sort_values(["start", "tau"], ascending=[True, False])
             .drop_duplicates("start", keep="first")
             .sort_values("start")
             .reset_index(drop=True))


def stats(g: pd.DataFrame) -> dict:
    if g.empty:
        return {}
    r = g.ret.to_numpy()
    byd = pd.Series(r).groupby(g.day.to_numpy()).mean()
    days = sorted(byd.index)
    half = np.median(days)
    h1 = float(byd[byd.index <= half].mean())
    h2 = float(byd[byd.index > half].mean())
    drop = float(byd.drop(byd.idxmax()).mean()) if len(byd) > 1 else float("nan")
    dt = (float(byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd))))
          if len(byd) > 1 and byd.std(ddof=1) else 0.0)
    t = float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 1 else 0.0
    return {"n": len(g), "wr": float(g.won.mean()), "paid": float(g.paid.mean()),
            "ev": float(r.mean()), "t": t, "day_t": dt, "h1": h1, "h2": h2,
            "drop": drop, "days": len(days), "byd": byd, "df": g}


def dow_table(df: pd.DataFrame) -> None:
    """The question this corpus was chosen to answer: is the month normal?"""
    import datetime as dt
    d = df.drop_duplicates("start").copy()
    d["dow"] = [dt.datetime.fromtimestamp(x, dt.timezone.utc).weekday()
                for x in d.start]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print("  rounds per weekday (UTC) -- a normal month, not a weekend pile:")
    tot = len(d)
    for i, nm in enumerate(names):
        n = int((d.dow == i).sum())
        days = int(d[d.dow == i].day.nunique())
        print(f"    {nm}  {n:>5} rounds over {days:>2} days  "
              f"({100 * n / max(tot, 1):4.1f}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="BTCUSDT")
    ap.add_argument("--lags", type=float, nargs="*",
                    default=[0.0, 5.0, 10.0, 20.0, 30.0, 60.0],
                    help="how many seconds the venue's book lags the spot")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="shift the venue's mid by this much (a richer/poorer "
                         "book than our own fair would imply)")
    ap.add_argument("--min-range", type=float, default=5.0)
    ap.add_argument("--max-range", type=float, default=16.0)
    ap.add_argument("--half-spread", type=float, default=S.assumed_half_spread)
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--start", type=float, default=7.35)
    ap.add_argument("--reps", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    print(f"30-DAY MONTH CORPUS  {a.sym}  2026-08-13 15:00 .. 2026-09-12 15:00")
    print(f"gate: tau {TAUS} | range [{a.min_range:.0f},{a.max_range:.0f}]bps | "
          f"min_edge {S.min_edge:.2f} | haircut {S.edge_haircut:.2f} | "
          f"safety {S.safety_margin:.3f} | half_spread {a.half_spread:.4f}")

    df, t1, c1 = replay(a.sym, min_range=a.min_range, max_range=a.max_range,
                        half_spread=a.half_spread)
    if df.empty:
        print("no decisions survived the range gate")
        return 1
    df.to_pickle(f"/tmp/f30_{a.sym}.pkl")
    print(f"\n  decisions after the range gate: {len(df):,} over "
          f"{df.start.nunique():,} rounds / {df.day.nunique()} days")
    print(f"  sigma_1m mean {df.sigma.mean() * 1e4:.2f} bps/min "
          f"(p10 {df.sigma.quantile(.1) * 1e4:.2f} p90 "
          f"{df.sigma.quantile(.9) * 1e4:.2f})")
    print(f"  fair mean {df.fair.mean():.3f} | round range bps "
          f"median {df.rng.median():.2f}")
    dow_table(df)

    print(f"\n=== THE MODEL'S OWN CALIBRATION on this month "
          f"(before any gate) ===")
    print(f"  {'fair bucket':<14}{'n':>7}{'actual WR':>11}{'gap':>9}")
    one = df.sort_values(["start", "tau"], ascending=[True, False]).drop_duplicates(
        "start", keep="first")
    for lo_, hi_ in ((0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
                     (0.70, 0.75), (0.75, 0.80), (0.80, 0.90), (0.90, 1.01)):
        b = one[(one.fair >= lo_) & (one.fair < hi_)]
        if len(b) < 20:
            continue
        wr = float(b.y_up.mean())
        print(f"  {f'{lo_:.2f}-{hi_:.2f}':<14}{len(b):>7}{wr:>11.1%}"
              f"{wr - ((lo_ + hi_) / 2):>+9.3f}")

    print(f"\n=== EV/$1 BY HOW STALE THE VENUE'S BOOK IS "
          f"(offset {a.offset:+.3f}, one entry per round) ===")
    print(f"  {'lag s':>6}{'fills':>7}{'WR':>8}{'paid':>7}{'edge':>8}"
          f"{'break-even':>12}{'EV/$1':>10}{'t':>7}{'day-t':>8}{'half1':>9}"
          f"{'half2':>9}{'dropbest':>10}{'tr/day':>8}")
    print("  " + "-" * 108)
    keep: dict[float, dict] = {}
    for lag in a.lags:
        g = gate_lagged(df, t1, c1, lag=lag, half_spread=a.half_spread,
                        offset=a.offset)
        st = stats(g)
        if not st or st["n"] < 10:
            print(f"  {lag:6.0f}   no fills -- the gate refused every round")
            continue
        keep[lag] = st
        be = st["paid"] * (1 + float(g.fee.mean()))
        print(f"  {lag:6.0f}{st['n']:>7}{st['wr']:>8.1%}{st['paid']:>7.3f}"
              f"{g.edge.mean():>+8.4f}{be:>12.3f}{st['ev']:>+10.4f}"
              f"{st['t']:>+7.2f}{st['day_t']:>+8.2f}{st['h1']:>+9.4f}"
              f"{st['h2']:>+9.4f}{st['drop']:>+10.4f}"
              f"{st['n'] / max(st['days'], 1):>8.1f}")

    if not keep:
        print("\n  every lag refused -- the book never shows edge")
        return 1
    if 0.0 in keep:
        print(f"\n  lag 0 is the CONTROL: the book IS our fair, edge is zero by "
              f"construction, and EV must be about -fee. It reads "
              f"{keep[0.0]['ev']:+.4f}/$1. If that row ever turns positive, the "
              f"rest of the table is an artifact again.")
    pos = [l for l, s in keep.items() if s["ev"] > 0]
    print("\n  READ THIS BEFORE USING THE TABLE: EV is monotone in the lag BY "
          "CONSTRUCTION.")
    print("  The gate selects on (fair_now - fair_stale) and the outcome is "
          "driven by how far")
    print("  the price has already travelled, so choosing a longer lag does not "
          "test the edge --")
    print("  it ASSERTS it. The venue's lag is not identifiable from bars "
          "alone; it needs a")
    print("  file holding the quote and the spot at the same timestamp. Until "
          "then, every row")
    print("  above is the assumption restated, and the 7-day block below is that "
          "assumption in")
    print("  dollars, not a forecast.")
    if pos:
        bl = max(pos)
        st = keep[bl]
        print(f"\n=== 7-DAY WHAT-IF, NOT A FORECAST: IF the venue lags {bl:.0f}s "
              f"({st['days']} days, {a.reps:,} day-block paths, "
              f"start ${a.start:.2f}, ${a.stake:.2f}/trade) ===")
        g = st["df"]
        pnl = g.groupby("day").apply(
            lambda x: float(x.ret.sum()) * a.stake, include_groups=False)
        cnt = g.groupby("day").size()
        rng = np.random.default_rng(a.seed)
        vals, counts = pnl.to_numpy(), cnt.to_numpy()
        pickd = rng.integers(0, len(vals), size=(a.reps, 7))
        bank = a.start + vals[pickd].sum(axis=1)
        print(f"  trades/day      {cnt.mean():.1f}")
        q = np.percentile(bank, [5, 10, 25, 50, 75, 90, 95])
        print(f"  median 7d pnl   ${np.median(bank) - a.start:+.2f}")
        print(f"  p5/p10/p25      ${q[0]:.2f} ${q[1]:.2f} ${q[2]:.2f}")
        print(f"  median/p75/p90  ${q[3]:.2f} ${q[4]:.2f} ${q[5]:.2f}  "
              f"p95 ${q[6]:.2f}")
        print(f"  P(7d loss)      {100 * (bank < a.start).mean():.1f}%")
        print(f"  P(below $1)     {100 * (bank < 1.0).mean():.2f}%")
        print(f"  expected trades {counts[pickd].sum(axis=1).mean():.0f}")
    else:
        print("\n  no lag in the sweep pays after the fee; the 7-day forecast at "
              "every tested staleness is a loss.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
