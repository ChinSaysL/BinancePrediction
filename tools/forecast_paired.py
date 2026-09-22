"""The forecast, priced on the venue's REAL book.

Why this file and not the others
--------------------------------
A PnL number needs three things at one instant: the spot, the book, and a
settlement. `data/recorded/quotes_arm.csv.gz` is the only local file with the
first two (`spot`, `up_bid/up_ask/down_bid/down_ask`, `tau_s`, one row per poll)
and `rounds.csv.gz` carries the third (`winner`, read off the venue).

This is NOT the arm's own ledger. Nothing here is selected by the arm: the model's
fair is recomputed from scratch off the recorded spot with the repo's own
VolEstimator, the gate is the repo's real `evaluate()`, and the settlement is the
venue's. The arm's decisions and fills are never read. That is the difference
between replaying the rules on recorded data and believing the ledger.

What it answers
---------------
1. Is the venue's implied sigma the same as ours? The venue's mid IS a
   probability, so z = Phi^-1(mid) and sigma_v = ln(S/K) / (z*sqrt(tau/60)).
   If the venue runs a different sigma, every fair we compute is off by a level
   factor and that -- not staleness -- is where the edge or the trap lives.
2. What does the real book charge, and what does the arm make at that price?
   Priced at the ASK, one entry per round, with the post-loss cooldown.

`tools/venue_lag.py` already measured the venue's staleness at **0 seconds**
(corr +0.749 at lag 0, decaying to 0 by lag 10), so the book cannot be scored as
pricing from a stale spot. This tool prices against it as it is.

Limits: 7 days (2026-09-08..09-14), one regime, and the same period the live arm
ran in. Those 7 days are the whole sample for the money statement.

Usage:
    python3 -m tools.forecast_paired
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcpredict.config import settings as S                       # noqa: E402
from btcpredict.edge.signal import evaluate, fee_rate_for_price   # noqa: E402
from btcpredict.pricing.digital import fair_value, norm_cdf       # noqa: E402
from btcpredict.pricing.vol import VolEstimator                   # noqa: E402

QF = "data/recorded/quotes_arm.csv.gz"
RF = "data/recorded/rounds.csv.gz"


def phi_inv_vec(p: np.ndarray) -> np.ndarray:
    """Inverse standard normal, vectorised -- the venue's mid in probit units."""
    try:
        from scipy.stats import norm
        return norm.ppf(p)
    except Exception:
        lo = np.full_like(p, -8.0)
        hi = np.full_like(p, 8.0)
        for _ in range(60):
            mid = (lo + hi) / 2
            lo = np.where(norm_cdf(mid) < p, mid, lo)
            hi = np.where(norm_cdf(mid) < p, hi, mid)
        return (lo + hi) / 2


def phi_inv(p: float) -> float:
    return float(phi_inv_vec(np.array([p]))[0])


def load(family: str):
    q = pd.read_csv(QF)
    q = q[q.slug.str.startswith(family)]
    q = q.dropna(subset=["up_bid", "up_ask", "down_bid", "down_ask", "spot",
                         "tau_s"])
    q = q[(q.up_ask > 0) & (q.up_bid > 0) & (q.spot > 0) & (q.tau_s > 0)]
    r = pd.read_csv(RF)
    r = r[r.slug.str.startswith(family)]
    r = r.dropna(subset=["strike", "winner"])
    r = r[r.strike > 0]
    r = r.assign(day=r.start_ms // 86_400_000)
    keep = r.slug.isin(set(q.slug)) & r.winner.isin(["Up", "Down"])
    return q, r[keep], set(r.loc[keep, "slug"])


def build_sigma(q: pd.DataFrame, slugs: set[str]) -> pd.Series:
    """Causal sigma_1m from the recorded spot, fed as 1-minute bars.

    The recorded spot is a ~1/s poll, so 1-minute bars can be built from it the
    same way live builds them from the kline feed: open = first poll of the
    minute, close = last. The VolEstimator is then the live object, one bar at a
    time, never re-seeded except for the initial warmup.
    """
    s = q[q.slug.isin(slugs)][["ts_ms", "spot"]].sort_values("ts_ms")
    sec = (s.ts_ms.to_numpy() // 1000).astype(np.int64)
    px = s.spot.to_numpy(dtype=np.float64)
    new = np.ones(len(sec), bool)
    new[1:] = sec[1:] != sec[:-1]
    sec, px = sec[new], px[new]
    minute = sec // 60
    df = pd.DataFrame({"minute": minute, "sec": sec, "px": px})
    g = df.groupby("minute").agg(first=("px", "first"), last=("px", "last"),
                                 n=("px", "size"))
    g = g[g.n >= 5]
    vol = VolEstimator()
    out = []
    for m, row in g.iterrows():
        if row["first"] > 0 and row["last"] > 0:
            vol.add_bar(int(m) * 60_000, math.log(row["last"] / row["first"]),
                        hour=int((m * 60 // 3600) % 24))
        out.append((int(m) * 60, vol.sigma_1m(
            hour=int((m * 60 // 3600) % 24))))
    return pd.Series({m: sg for m, sg in out})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", default="btc-updown-5m")
    ap.add_argument("--min-tau", type=float, default=S.min_tau_s)
    ap.add_argument("--max-tau", type=float, default=S.max_tau_s)
    ap.add_argument("--cooldown", type=int, default=S.model_cooldown_after_loss)
    ap.add_argument("--persistence", type=int, default=1)
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--start", type=float, default=7.35)
    ap.add_argument("--reps", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    q, rounds, slugs = load(a.family)
    print(f"{a.family}: {len(slugs):,} rounds with quotes+settlement | "
          f"{len(q):,} paired polls | "
          f"{rounds.day.nunique()} days "
          f"({pd.to_datetime(rounds.start_ms.min(), unit='ms').date()} .. "
          f"{pd.to_datetime(rounds.start_ms.max(), unit='ms').date()})")

    sig = build_sigma(q, slugs)
    print(f"our sigma_1m from the recorded spot: median "
          f"{sig.median() * 1e4:.2f} bps/min "
          f"(p10 {sig.quantile(.1) * 1e4:.2f} p90 {sig.quantile(.9) * 1e4:.2f})")

    # ---- 1. the venue's implied sigma vs ours ---------------------------
    prev = rounds.set_index("slug")
    rows = []
    for x in q.itertuples(index=False):
        if x.slug not in prev.index:
            continue
        rr = prev.loc[x.slug]
        if isinstance(rr, pd.DataFrame):
            rr = rr.iloc[0]
        K = float(rr.strike)
        tau = float(x.tau_s)
        if tau < a.min_tau or tau > a.max_tau:
            continue
        qm = ((x.up_bid + x.up_ask) / 2 + (1 - (x.down_bid + x.down_ask) / 2)) / 2
        qm = min(0.999, max(0.001, qm))
        minute = (int(x.ts_ms) // 1000) // 60 * 60
        sg = sig.get(minute, np.nan)
        if not np.isfinite(sg):
            continue
        lr = math.log(x.spot / K)
        fair = fair_value(x.spot, K, tau, sg)
        rows.append({"slug": x.slug, "tau": tau, "S": x.spot, "K": K,
                     "lr": lr, "mid": qm, "fair": fair, "sigma": sg,
                     "up_ask": x.up_ask, "down_ask": x.down_ask,
                     "up_bid": x.up_bid, "down_bid": x.down_bid,
                     "day": rr.day, "won_up": int(rr.winner == "Up")})
    d = pd.DataFrame(rows)
    if d.empty:
        print("no paired polls inside the tau window")
        return 1
    # the slope of ln(S/K) on z*sqrt(tau/60) IS 1/sigma_venue; fit it, rather
    # than averaging per-row inversions, which explode as z -> 0 and would let a
    # handful of near-0.50 quotes set the answer.
    zz = phi_inv_vec(d.mid.to_numpy())
    xv = zz * np.sqrt(d.tau.to_numpy() / 60.0)
    yv = d.lr.to_numpy()
    ok = np.abs(xv) > 1e-9
    slope = float(np.sum(xv[ok] * yv[ok]) / np.sum(xv[ok] ** 2))
    print(f"\n=== THE VENUE'S IMPLIED SIGMA vs OURS ({len(d):,} polls) ===")
    # z = ln(S/K)/(sigma_v*sqrt(tau/60)), so regressing ln(S/K) on
    # z*sqrt(tau/60) returns sigma_v itself, in log units per sqrt-minute.
    sv = slope
    print(f"  fitted sigma_venue = {sv * 1e4:.2f} bps/min")
    print(f"  ours               = {d.sigma.median() * 1e4:.2f} bps/min")
    print(f"  ratio venue/ours   = {sv / d.sigma.median():.3f}   "
          f"(1.00 = the venue prices at our sigma)")
    print(f"  corr(ln(S/K), venue z*sqrt(tau/60)) = "
          f"{np.corrcoef(xv[ok], yv[ok])[0, 1]:+.3f}")
    print(f"  a ratio above 1 means the venue's book sits CLOSER TO 0.50 than "
          f"our fair does,")
    print(f"  so the side we favour is offered below what we think it is worth. "
          f"Whether that")
    print(f"  is edge or error is decided by which sigma settles better -- see "
          f"the WR below.")

    # ---- 2. money at the real book -------------------------------------
    print(f"\n=== REPLAY AT THE REAL ASK (one entry/round, tau "
          f"[{a.min_tau:.0f},{a.max_tau:.0f}], cooldown {a.cooldown}) ===")
    fills = []
    for x in d.sort_values(["day", "slug", "tau"], ascending=[True, True, False]
                           ).itertuples(index=False):
        sg_ = evaluate(x.fair, x.mid, 1.0 - x.mid,
                       half_spread=S.assumed_half_spread,
                       safety_margin=S.safety_margin, min_edge=S.min_edge,
                       min_quote=S.min_quote, max_quote=S.max_quote,
                       edge_haircut=S.edge_haircut)
        if not sg_.act or sg_.side is None:
            continue
        paid = x.up_ask if sg_.side == "Up" else x.down_ask
        if not (0.0 < paid < 1.0):
            continue
        fee = fee_rate_for_price(paid)
        won = int(x.won_up == (1 if sg_.side == "Up" else 0))
        # the mid OF THE SIDE BOUGHT -- comparing the chosen side's ask to the
        # Up mid is a side-mixed number and means nothing
        own_mid = x.mid if sg_.side == "Up" else 1.0 - x.mid
        fills.append({"day": x.day, "slug": x.slug, "tau": x.tau,
                      "side": sg_.side, "fair": x.fair, "mid": x.mid,
                      "own_mid": own_mid, "paid": paid, "fee": fee, "won": won,
                      "edge": sg_.edge, "slip": paid - own_mid})
    f = pd.DataFrame(fills)
    if f.empty:
        print("  the gate took nothing on the real book")
        return 1
    f = (f.sort_values(["day", "slug", "tau"], ascending=[True, True, False])
           .drop_duplicates("slug", keep="first").sort_values(["day", "slug"])
           .reset_index(drop=True))
    if a.cooldown > 0:
        skip, keep = 0, []
        for i, row in f.iterrows():
            if skip:
                skip -= 1
                continue
            keep.append(i)
            if not row.won:
                skip = a.cooldown
        f = f.loc[keep].reset_index(drop=True)
    r = (f.won / f.paid - 1.0 - f.fee).to_numpy()
    byd = pd.Series(r).groupby(f.day.to_numpy()).mean()
    days = sorted(byd.index)
    half = np.median(days)
    h1 = float(byd[byd.index <= half].mean())
    h2 = float(byd[byd.index > half].mean())
    drop = float(byd.drop(byd.idxmax()).mean())
    dt = float(byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd))))
    t = float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r))))
    print(f"  fills {len(f):,} over {f.day.nunique()} days "
          f"({len(f) / f.day.nunique():.1f}/day)")
    print(f"  WR {f.won.mean():.1%} | paid {f.paid.mean():.3f} "
          f"(own mid {f.own_mid.mean():.3f}, ask-minus-own-mid "
          f"{f.slip.mean():+.4f})")
    print(f"  EV/$1 {r.mean():+.4f}   pooled t {t:+.2f}   day-t {dt:+.2f}")
    print(f"  halves {h1:+.4f} / {h2:+.4f}   drop-best-day {drop:+.4f}")
    print(f"  break-even WR at the mean price: "
          f"{f.paid.mean() * (1 + f.fee.mean()):.3f}")
    print(f"  side mix: " + ", ".join(f"{k} {v}" for k, v in
                                     f.side.value_counts().items()))

    print(f"\n  by tau band:")
    for lo_, hi_ in ((60, 90), (90, 120), (120, 150), (150, 181)):
        g = f[(f.tau >= lo_) & (f.tau < hi_)]
        if len(g) < 5:
            continue
        print(f"    {lo_}-{hi_}s  n={len(g):<5} WR {g.won.mean():>5.1%} "
              f"paid {g.paid.mean():.3f} "
              f"EV {float((g.won / g.paid - 1 - g.fee).mean()):+.4f}")
    print("  by quote band:")
    band = pd.cut(f.own_mid, [0.5, 0.6, 0.65, 0.7, 0.75, 0.8])
    for b, g in f.groupby(band, observed=True):
        print(f"    {b}  n={len(g):<5} WR {g.won.mean():>5.1%} "
              f"paid {g.paid.mean():.3f} "
              f"EV {float((g.won / g.paid - 1 - g.fee).mean()):+.4f}")

    print(f"\n=== PER-DAY at ${a.stake:.2f}/trade ===")
    cum = 0.0
    for dd, g in f.groupby("day"):
        pnl = float((g.won / g.paid - 1 - g.fee).sum()) * a.stake
        cum += pnl
        print(f"  {pd.to_datetime(dd * 86_400_000, unit='ms').date()}  "
              f"n={len(g):>3}  WR {g.won.mean():>5.1%}  paid {g.paid.mean():.3f}"
              f"  {pnl:+7.2f}  (cum {cum:+7.2f})")

    rng = np.random.default_rng(a.seed)
    pnl_day = f.groupby("day").apply(
        lambda x: float((x.won / x.paid - 1 - x.fee).sum()) * a.stake,
        include_groups=False).to_numpy()
    cnt = f.groupby("day").size().to_numpy()
    pickd = rng.integers(0, len(pnl_day), size=(a.reps, 7))
    bank = a.start + pnl_day[pickd].sum(axis=1)
    q_ = np.percentile(bank, [5, 10, 25, 50, 75, 90, 95])
    print(f"\n=== 7-DAY FORECAST ({len(pnl_day)} days, {a.reps:,} day-block "
          f"paths, start ${a.start:.2f}) ===")
    print(f"  median 7d pnl   ${np.median(bank) - a.start:+.2f}")
    print(f"  p5/p10/p25      ${q_[0]:.2f} ${q_[1]:.2f} ${q_[2]:.2f}")
    print(f"  median/p75/p90  ${q_[3]:.2f} ${q_[4]:.2f} ${q_[5]:.2f}  "
          f"p95 ${q_[6]:.2f}")
    print(f"  P(7d loss)      {100 * (bank < a.start).mean():.1f}%")
    print(f"  P(below $1)     {100 * (bank < 1.0).mean():.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
