"""Build a BETTER probability estimate than the market, and test it honestly.

WHY THIS IS THE ONLY THING LEFT. A binary position is worth exactly p, so the
only optimal exit rule is `sell iff bid > p` (see `model_knob_sweep --mode
predict`). Measured on 272,214 ticks the market's own bid predicts the
settlement BETTER than our model does -- Brier 0.2091 against 0.2128 -- and
E[bid - p] is negative in every bucket after entry, so no exit rule can pay.
The gap to close is 0.0037 of Brier. Nothing else substitutes for it: not a
rule, not a fee change, not a support/resistance feature (eight were tested,
all ~0 against the settlement).

WHERE IT RUNS. The bot's venv has no numpy, so the fit cannot run there. The
host exports rows with `tools/prob_export.py` (stdlib only, SQL over the live
1 GB database) and this script fits them on the analysis machine. Nothing new
is installed into a live trading environment.

WHAT IT FITS. `logit(p_up)` as a linear function of the features, INCLUDING
`logit(market_mid)` as one of them. That specification makes the model strictly
more expressive than "just trust the market": it can shrink the market's logit,
expand it, or leave it alone, and the fit decides. Every feature is a quantity
the bot had at that tick, so there is no look-ahead.

HOW IT IS SCORED. A TIME split, not a random one: TRAIN on the earliest 60% of
the days, TEST on the last 40%. A random split over ticks is the classic way to
fake this -- consecutive ticks of one round share almost all their information,
so a random split leaks the answer. The headline numbers are the TEST ones, and
the gain is judged against DAY-level noise, because ticks within a day are not
independent observations.

WHAT IT IS NOT. It is not a claim that the market is beatable. The measured
question is exactly one: does the fitted p beat the market's Brier on data the
fit never saw, by more than day-to-day noise? If it does not, then the answer
to "is there a formula better than holding" is measured rather than argued.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics as st
from pathlib import Path

import numpy as np

SQ2 = math.sqrt(2.0)
NAMES = ["const", "logit(mkt)", "logit(fair)", "z", "|z|", "tau", "sigma_bps",
         "z^2", "lg-lf", "z-lg", "spread", "book_age", "venue_lag", "imb",
         "book_disp", "travel_bps"]


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQ2))


def fair_value(spot: float, strike: float, tau: float, sigma_1m: float) -> float:
    """A local copy of `pricing.digital.fair_value` -- four lines of arithmetic,
    so this tool does not depend on the bot's config loading at all."""
    if tau <= 0:
        return 1.0 if spot > strike else 0.0
    z = math.log(spot / strike) / (sigma_1m * math.sqrt(tau / 60.0))
    return min(1.0 - 1e-9, max(1e-9, _ncdf(z)))


def _f(r: dict, key: str, default: float = 0.0) -> float:
    v = r.get(key)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        return default


def load_rows(path: Path):
    rows = []
    with path.open() as fh:
        for r in csv.DictReader(fh):
            try:
                ts = int(r["ts_ms"])
                start = int(r["start_ms"])
                tau = float(r["tau_s"])
                spot, strike = float(r["spot"]), float(r["strike"])
                sig = float(r["sigma_1m"])
                ua, ub = float(r["up_ask"]), float(r["up_bid"])
                da, db = float(r["down_ask"]), float(r["down_bid"])
            except (TypeError, ValueError):
                continue
            if not (0.0 < ua < 1.0 and 0.0 < ub < 1.0 and sig > 0.0
                    and spot > 0.0 and strike > 0.0 and tau > 0.0):
                continue
            y = 1.0 if r["winner"] == "Up" else 0.0
            fair_up = fair_value(spot, strike, tau, sig)
            # TWO reads of the same probability: the Up book, and the Down book
            # implied back through the complement. Averaging removes part of the
            # one-sided quote noise.
            mkt_up = 0.5 * (ub + ua)
            mkt_dn = (1.0 - 0.5 * (db + da)) if (0 < da < 1 and 0 < db < 1) \
                else mkt_up
            mkt = min(0.999, max(0.001, 0.5 * (mkt_up + mkt_dn)))
            age, lag = _f(r, "book_age_ms"), _f(r, "venue_lag_ms")
            uasz, ubsz = _f(r, "up_ask_size"), _f(r, "up_bid_size")
            z = math.log(spot / strike) / (sig * math.sqrt(tau / 60.0))
            lg = math.log(mkt / (1.0 - mkt))
            fc = min(0.999, max(0.001, fair_up))
            lf = math.log(fc / (1.0 - fc))
            spread = ua - ub
            sz = uasz + ubsz
            rows.append((
                [1.0, lg, lf, z, abs(z), tau, sig * 1e4, z * z, lg - lf,
                 z - lg, spread, min(1.0, age / 1000.0),
                 min(1.0, lag / 500.0), (ubsz - uasz) / sz if sz > 0 else 0.0,
                 mkt_up - mkt_dn, abs(math.log(spot / strike)) * 1e4],
                y, (r["slug"], ts, tau, start, mkt, fair_up, ub)))
    X = np.asarray([r[0] for r in rows], float)
    y = np.asarray([r[1] for r in rows], float)
    return X, y, [r[2] for r in rows]


def fit_logit(X, y, l2=1.0, iters=80):
    """Newton-Raphson with an L2 ridge. Small and dependency-free."""
    n, k = X.shape
    w = np.zeros(k)
    reg = l2 * np.eye(k)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(X @ w, -30, 30)))
        g = X.T @ (p - y) / n + l2 * w / n
        H = (X.T * (p * (1.0 - p))) @ X / n + reg / n
        try:
            step = np.linalg.solve(H + 1e-9 * np.eye(k), g)
        except np.linalg.LinAlgError:
            break
        w = w - step
        if np.max(np.abs(step)) < 1e-9:
            break
    return w


def brier(p, y) -> float:
    return float(np.mean((p - y) ** 2))


def logloss(p, y) -> float:
    p = np.clip(p, 1e-9, 1.0 - 1e-9)
    return float(np.mean(-(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="/tmp/prob_btc5m.csv")
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--scale-cut", type=float, default=3.0)
    args = ap.parse_args()

    X, y, meta = load_rows(Path(args.csv))
    days = np.array([m[3] // 86_400_000 for m in meta])
    print(f"{len(y)} ticks over {len(set(days.tolist()))} days | up-rate "
          f"{y.mean():.3f} | constant-0.5 Brier "
          f"{brier(np.full_like(y, 0.5), y):.4f}")

    cuts = sorted(set(days.tolist()))
    ntr = max(1, int(len(cuts) * args.train_frac))
    train_days = set(cuts[:ntr])
    tr = np.array([d in train_days for d in days])
    te = ~tr
    print(f"TRAIN {tr.sum()} ticks / {len(train_days)} days | TEST {te.sum()} "
          f"ticks / {len(cuts) - ntr} days")

    mu, sd = X[tr].mean(axis=0), X[tr].std(axis=0)
    sd[sd == 0] = 1.0
    Z = np.clip((X - mu) / sd, -args.scale_cut, args.scale_cut)
    w = fit_logit(Z[tr], y[tr], l2=args.l2)
    p = 1.0 / (1.0 + np.exp(-np.clip(Z @ w, -30, 30)))

    mkt = np.array([m[4] for m in meta])
    fair = np.array([m[5] for m in meta])

    print("\n=== BRIER / LOG-LOSS (TEST is the only number that counts) ===")
    print("  predictor             TRAIN brier   TEST brier   TEST logloss")
    for name, v in (("market mid", mkt), ("model fair", fair),
                    ("fitted logit", p)):
        print(f"  {name:<20}    {brier(v[tr], y[tr]):.4f}       "
              f"{brier(v[te], y[te]):.4f}      {logloss(v[te], y[te]):.4f}")

    d_te = days[te]
    diff = (p[te] - y[te]) ** 2 - (mkt[te] - y[te]) ** 2
    per_day = [float(np.mean(diff[d_te == d]))
               for d in sorted(set(d_te.tolist()))]
    per_day = [v for v in per_day if v == v]
    print("\n=== IS THE GAIN BIGGER THAN DAILY NOISE?  (+ = ours is better) ===")
    if len(per_day) >= 3:
        m_, s_ = st.fmean(per_day), st.stdev(per_day)
        t_ = m_ / (s_ / math.sqrt(len(per_day))) if s_ > 0 else float("nan")
        print(f"  Brier gain vs the market per day: mean {m_:+.5f}  sd "
              f"{s_:.5f}  n {len(per_day)}  t {t_:+.2f}")
        print(f"  days better than the market: "
              f"{sum(1 for v in per_day if v > 0)}/{len(per_day)}")
        print(f"  total TEST Brier improvement: "
              f"{brier(p[te], y[te]) - brier(mkt[te], y[te]):+.5f}")
    else:
        print("  too few test days")

    print("\n=== CALIBRATION on TEST (decile of our p) ===")
    pv, yv = p[te], y[te]
    o = np.argsort(pv)
    k = max(1, len(pv) // 10)
    print("   decile    n    predicted   realised     gap")
    for i in range(10):
        idx = o[i * k:(i + 1) * k] if i < 9 else o[9 * k:]
        if len(idx) < 10:
            continue
        pr, re = float(pv[idx].mean()), float(yv[idx].mean())
        print(f"   {i + 1:>5}  {len(idx):>5}     {pr:.4f}     {re:.4f}   "
              f"{re - pr:+.4f}")

    print("\n=== DOES A BETTER p OPEN THE EXIT?  E[bid - p] on TEST ===")
    ub = np.array([m[6] for m in meta])
    tb = np.array([m[2] for m in meta])
    print("     tau        n     E[bid-p_fit]   E[bid-p_mkt]    gain")
    for lo_, hi_ in ((180, 210), (160, 180), (140, 160), (120, 140),
                     (100, 120), (80, 100), (60, 80)):
        sel = te & (tb >= lo_) & (tb < hi_)
        if sel.sum() < 50:
            continue
        a = float(np.mean(ub[sel] - mkt[sel]))
        b = float(np.mean(ub[sel] - p[sel]))
        print(f"   {lo_:>3}-{hi_:<3} {sel.sum():>6}      {b:+.4f}        "
              f"{a:+.4f}      {b - a:+.4f}")

    print("\n=== FITTED WEIGHTS (standardised, so they are comparable) ===")
    for nm, wi in zip(NAMES, w):
        print(f"  {nm:<12} {wi:+.4f}")

    # ==== IS ITOVERFITTING, OR IS THE MARKET SIMPLY BETTER? ================
    # A model that wins on TRAIN and loses on TEST is overfitting, and the two
    # cures are fewer features and more shrinkage. Both are swept here, because
    # the honest question is not "can I fit this" but "does ANY setting beat the
    # market on data it never saw". The FAILING row is the one that matters.
    print("\n=== SWEEP: TEST Brier by feature set and ridge strength ===")
    mkt_te = brier(mkt[te], y[te])
    print(f"  market mid, TEST Brier = {mkt_te:.4f}  (the bar to clear)")
    sets = {
        "mkt only": [1, 1],
        "mkt+fair": [1, 1, 1],
        "mkt+fair+z": [1, 1, 1, 1],
        "no book cols": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1],
        "all": [1] * X.shape[1],
    }
    print(f"  {'features':<14} " + "".join(f"l2={v:<8g}" for v in
                                           (0.1, 1, 10, 100, 1000)))
    for nm, mask in sets.items():
        sub = [i for i, m_ in enumerate(mask) if m_]
        Zs = Z[:, sub]
        line = f"  {nm:<14} "
        for l2 in (0.1, 1, 10, 100, 1000):
            ws = fit_logit(Zs[tr], y[tr], l2=l2)
            ps = 1.0 / (1.0 + np.exp(-np.clip(Zs @ ws, -30, 30)))
            bt = brier(ps[te], y[te])
            line += f"{bt:.4f}{'*' if bt < mkt_te else ' '}   "
        print(line)
    print("  `*` = beats the market on TEST. A model that beats it on TRAIN but "
          "not here is fitting noise.")
    print(f"\n  MEAN CHECK (a biased p fakes a sell edge): mean(p_fit) "
          f"{float(p[te].mean()):.4f} vs realised {float(y[te].mean()):.4f} "
          f"and mean(market) {float(mkt[te].mean()):.4f}")
    print("  If mean(p_fit) sits well below both, then `E[bid - p] > 0` below is "
          "MISCALIBRATION, not an edge: the model is simply too timid, and "
          "selling whenever bid > p_fit would fire on positions that are "
          "genuinely worth more than the bid.")


if __name__ == "__main__":
    main()
