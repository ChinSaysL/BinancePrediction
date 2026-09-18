"""IS THE VENUE'S PRICE MISCALIBRATED vs THE PM TERMINAL DISTRIBUTION?

The claim (operator, 2026-09-17): stop racing the wire, forecast the outcome.
On Polymarket the terminal variable is a 60 s TAIL TWAP vs the range start,
whose printed part is FIXED near the bell, so a naive quoting engine can post
0.20 on a side the realised window has already decided at 95%.

The repo already owns the model promised for this. `btcpredict/venues/
polymarket/pricing.py` implements it and `terminal_rule()` picks the TAIL
window by measurement (90.7% fit to gamma's own winners, 2,303 rounds, vs 87.3%
for the point rule and 84.1% for the whole-round window). `cross_min_edge` is
the knob that would consume it, and it is DEAD: no call site in main.py, and
its own comment says to enable it only once this distribution exists.

This tool measures three things, in this order, because each one gates the next:

  A. CALIBRATION. Brier(model) vs Brier(venue mid) on the same rows. If the
     venue's implied probability already beats the model, the whole thesis is
     finished before any threshold is chosen.
  B. THE DECISIVE-WINDOW CLAIM. With most of the settlement window printed
     (tau small), when the model is nearly certain, where does the venue's ask
     sit? `ask - p_model` is the claimed mispricing, in cents per share.
  C. THE GATE ON THE CROSS ARM, AS AN INCREMENT. On the arm's own trigger
     population, score the cohort a `cross_min_edge` gate would REFUSE. Never
     compare the CIs of nested subsets: growth is additive, so the refused
     cohort's own EV is the number that decides.

Imports the DEPLOYED functions -- `fair_value`, `window_log_integral`,
`edge_per_share`, `crossed_side` -- so the measurement cannot drift from the
code the gate would run.

Usage (on the VPS; needs numpy/pandas + the repo deps):
    PYTHONPATH=/root/BinancePrediction /root/analysis-venv/bin/python \
        tools/pm_forecast_edge_check.py
"""
from __future__ import annotations

import math
import pathlib
import sqlite3
import sys

import numpy as np
import pandas as pd

from btcpredict.config import SYMBOL_BY_PREFIX
from btcpredict.main import crossed_side
from btcpredict.pricing.vol import VolEstimator
from btcpredict.venues.polymarket.pricing import (
    TAIL, TWAP_LOOKBACK_S, edge_per_share, fair_value, fee_per_share,
    window_log_integral)

DB = sys.argv[1] if len(sys.argv) > 1 else "/root/BinancePrediction/data/btcpredict.db"
SIGMA_MODE = sys.argv[2] if len(sys.argv) > 2 else "bot"
BARS = pathlib.Path("/root/BinancePrediction/data/bars_1m")
SIGMA_N = 240          # the bot seeds sigma from 240 ONE-MINUTE returns
TAU_LO, TAU_HI = 5.0, 60.0

_KL: dict[str, tuple] = {}


def klines(sym: str):
    """(ts_array, rows) of 1m bars, cached. Rows in Binance kline layout."""
    if sym not in _KL:
        p = BARS / f"{sym}_1m_pm.csv"
        if not p.exists():
            _KL[sym] = (np.array([]), [])
        else:
            d = pd.read_csv(p)
            rows = [[int(t) * 1000, o, h, l, c]
                    for t, o, h, l, c in zip(d.ts, d.open, d.high, d.low, d.close)]
            _KL[sym] = (d.ts.to_numpy(dtype=np.int64), rows)
    return _KL[sym]


def bot_sigma(slug: str, start_ms: float) -> float | None:
    """The sigma the DEPLOYED model would run on: `vol.VolEstimator` seeded with
    the 1m bars up to the round's open, exactly as `_setup` seeds it at boot.

    This is the fair input. A within-round estimate from the quotes table is a
    DIFFERENT estimator (240 seconds of 1 Hz returns vs 240 minutes of 1m
    returns), and scoring the model with it measures the substitute.
    """
    pre = slug.split("-")[0].upper()
    sym = SYMBOL_BY_PREFIX.get(pre.lower()) or f"{pre}USDT"
    ts, rows = klines(sym)
    if not len(ts):
        return None
    upto = int((ts * 1000 <= start_ms).sum())
    if upto < SIGMA_N + 1:
        return None
    est = VolEstimator()
    est.seed_from_klines(rows[max(0, upto - SIGMA_N - 1):upto])
    sig = est.sigma_1m()
    return float(sig) if sig and sig > 0 else None


def load(db: str | None = None) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db or DB}?mode=ro", uri=True)
    q = pd.read_sql_query("""
      select q.slug, q.ts_ms, q.tau_s, q.spot, q.up_ask, q.down_ask,
             q.up_bid, q.down_bid
      from quotes q
      where q.spot is not null and q.up_ask is not null and q.down_ask is not null
        and q.tau_s between 0 and 300
      order by q.slug, q.ts_ms
    """, con)
    r = pd.read_sql_query("""
      select slug, end_ms, start_ms, strike, winner, feed_provider
      from rounds where strike is not null and winner is not null
    """, con)
    con.close()
    r = r[r.feed_provider.fillna("").str.contains("twap", case=False)]
    r = r[r.slug.str.contains("-5m-")]
    return q.merge(r, on="slug", how="inner").sort_values(["slug", "ts_ms"])


def sigma_1m(spots: np.ndarray, ts: np.ndarray, now_ms: float) -> float | None:
    """Trailing per-minute sigma from the poll series, no look-ahead.

    The bot's own estimator (`pricing/vol.py`) is an EWMA over **240 ONE-MINUTE
    log returns** (4 h of klines) and the DB only carries ~300 s of spot per
    round, so this cannot be the same estimator. It is scaled by the MEASURED
    spacing instead of assuming 1 Hz: `Var` scales with dt, so using a nominal
    1 s on a 3 s series understates sigma by sqrt(3) and makes the model look
    decisive for a reason that is arithmetic.
    """
    m = ts <= now_ms
    t = ts[m][-SIGMA_N:]
    s = spots[m][-SIGMA_N:]
    if len(s) < 30 or np.any(s <= 0):
        return None
    dt = float(np.median(np.diff(t))) / 1000.0
    if not (0.2 <= dt <= 30.0):
        return None
    r = np.diff(np.log(s))
    if len(r) < 10 or not np.isfinite(r).all():
        return None
    sd = float(np.std(r))
    return sd * math.sqrt(60.0 / dt) if sd > 0 else None


def spacing_s(ts: np.ndarray) -> float:
    d = np.diff(np.sort(ts))
    d = d[d > 0]
    return float(np.median(d)) / 1000.0 if len(d) else float("nan")


def implied_sigma_from_venue(mid_up: float, S: float, K: float, tau: float,
                             J: float | None, sig: float) -> float | None:
    """Bisect the DEPLOYED fair_value for the sigma the venue's mid implies.

    This is the diagnostic that separates "the model is wrong" from "the input
    sigma is wrong": if the venue behaves as if sigma were 3x the realized one,
    the model is not miscalibrated, it is being fed a number 3x too small.
    """
    if not (0.02 < mid_up < 0.98):
        return None
    lo, hi = sig / 20.0, sig * 20.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        p, _a = fair_value(S, K, tau, mid, window_log_int=J, window=TAIL)
        if (p > mid_up) == (mid_up < 0.5) or p > mid_up:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def build(df: pd.DataFrame, sigma_mode: str | None = None) -> pd.DataFrame:
    mode = sigma_mode or SIGMA_MODE
    rows = []
    for slug, g in df.groupby("slug", sort=False):
        g = g.sort_values("ts_ms")
        ts = g.ts_ms.to_numpy(dtype=float)
        sp = g.spot.to_numpy(dtype=float)
        tau = g.tau_s.to_numpy(dtype=float)
        strike = float(g.strike.iloc[0])
        end_ms = float(g.end_ms.iloc[0])
        winner = g.winner.iloc[0]
        if not np.isfinite(strike) or strike <= 0 or not np.isfinite(end_ms):
            continue
        sig_bot = bot_sigma(slug, float(g.start_ms.iloc[0])) if mode == "bot" else None
        for i in range(len(g)):
            if not (TAU_LO <= tau[i] <= TAU_HI):
                continue
            sig = sigma_1m(sp, ts, ts[i]) if sig_bot is None else sig_bot
            if sig is None:
                continue
            t = float(tau[i])
            # the PRINTED part of the settled window: [b-60, b-tau]
            open_ms = end_ms - TWAP_LOOKBACK_S * 1000.0
            close_ms = end_ms - max(t, 0.0) * 1000.0
            samples = [(float(a), float(b)) for a, b in zip(ts, sp) if a <= ts[i]]
            J = (None if t >= TWAP_LOOKBACK_S else
                 window_log_integral(samples, strike, open_ms=open_ms,
                                     close_ms=close_ms))
            p_up, assumed = fair_value(float(sp[i]), strike, t, sig,
                                       window_log_int=J, window=TAIL)
            up_ask, dn_ask = float(g.up_ask.iloc[i]), float(g.down_ask.iloc[i])
            up_bid = float(g.up_bid.iloc[i])
            if not (0.01 < up_ask < 0.99 and 0.01 < dn_ask < 0.99):
                continue
            # the market's own implied P(Up): the mid of the Up book
            mid_up = ((up_bid + up_ask) / 2.0) if (0 < up_bid < 1) else np.nan
            imp = (implied_sigma_from_venue(float(mid_up), float(sp[i]),
                                            strike, t, J, sig)
                   if np.isfinite(mid_up) else np.nan)
            rows.append(dict(
                slug=slug, ts_ms=float(ts[i]), tau=t, p_model=p_up,
                spot=float(sp[i]), strike=strike,
                J=(np.nan if J is None else float(J)),
                mid_up=mid_up, up_ask=up_ask, dn_ask=dn_ask,
                printed=min(1.0, max(0.0, (TWAP_LOOKBACK_S - t) / TWAP_LOOKBACK_S)),
                sigma=sig, assumed=assumed, won_up=1.0 if winner == "Up" else 0.0,
                sigma_imp=imp, spacing=spacing_s(ts),
                day=pd.to_datetime(ts[i], unit="ms", utc=True).strftime("%m-%d")))
    return pd.DataFrame(rows)


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def brier_with_scale(pr: pd.DataFrame, k: float) -> float:
    """Brier of the model when its sigma is multiplied by `k`.

    Recomputed through the DEPLOYED `fair_value` on the stored inputs, so this
    is the same model under a different input, not a different model.
    """
    p = np.array([
        fair_value(r.spot, r.strike, r.tau, r.sigma * k,
                   window_log_int=(None if not np.isfinite(r.J) else r.J),
                   window=TAIL)[0]
        for r in pr.itertuples()])
    return brier(p, pr.won_up.to_numpy())


def main() -> None:
    df = load()
    if df.empty:
        print("no Polymarket-era rounds with spot + strike + winner"); return
    pr = build(df)
    if pr.empty:
        print("no usable rows"); return
    print(f"=== {pr.slug.nunique()} PM-era 5m rounds, {len(pr)} priced snapshots, "
          f"tau [{TAU_LO:.0f},{TAU_HI:.0f}]s, days {sorted(pr.day.unique())} | "
          f"SIGMA={SIGMA_MODE} ===")
    y = pr.won_up.to_numpy()
    print(f"realised Up rate {y.mean():.3f} | model mean p_up {pr.p_model.mean():.3f}"
          f" | venue mid mean {pr.mid_up.mean():.3f}")

    print("\n--- A. CALIBRATION (Brier, lower is better; 0.25 = a coin flip) ---")
    ok = pr.mid_up.notna().to_numpy()
    print(f"    model          {brier(pr.p_model.to_numpy(), y):.4f}")
    print(f"    venue mid      {brier(pr.mid_up.to_numpy()[ok], y[ok]):.4f}   (n={ok.sum()})")
    print(f"    coin flip      {brier(np.full(len(y), 0.5), y):.4f}")
    print(f"    poll spacing median {pr.spacing.median():.2f}s | realised sigma_1m "
          f"median {pr.sigma.median():.2e} | venue-IMPLIED sigma_1m "
          f"median {pr.sigma_imp.median():.2e}")
    ratio = (pr.sigma_imp / pr.sigma).dropna()
    if len(ratio):
        print(f"    implied/realised sigma: median {ratio.median():.2f}  "
              f"p25 {ratio.quantile(.25):.2f}  p75 {ratio.quantile(.75):.2f}   "
              f"<- >1 means the venue is WIDER than the model's sigma")
    print("    Brier vs a sigma MULTIPLIER applied to the realised sigma:")
    best = None
    for k in (1.0, 1.5, 2.0, 3.0, 4.0, 6.0):
        b = brier_with_scale(pr, k)
        print(f"      sigma x {k:<4.1f}   {b:.4f}")
        if best is None or b < best[1]:
            best = (k, b)
    venue_b = brier(pr.mid_up.to_numpy()[ok], y[ok])
    print(f"      -> best multiplier {best[0]:.1f} (Brier {best[1]:.4f}); "
          f"the venue scores {venue_b:.4f}")
    print(f"      -> 'x1.0' is the estimator as written; anything >> 1 means the "
          f"model was overconfident, not wrong")
    print("    by printed share of the window:")
    for lo, hi in [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)]:
        m = (pr.printed >= lo) & (pr.printed < hi)
        if m.sum() < 20:
            continue
        mm = m & pr.mid_up.notna()
        print(f"      printed {lo:.0%}-{hi:.0%}  n={int(m.sum()):4d}  "
              f"model {brier(pr.p_model.to_numpy()[m], y[m]):.4f}  "
              f"venue {brier(pr.mid_up.to_numpy()[mm], y[mm]):.4f}  "
              f"(tau med {pr.tau[m].median():.0f}s)")

    print("\n--- B. THE CLAIM: when the window is mostly PRINTED, is a decided "
          "side still cheap? ---")
    print(f"{'printed':>8} {'n':>5} {'p_model':>8} {'venue ask':>10} {'ask-p':>7} "
          f"{'realised':>9} {'EV/sh@ask':>10}")
    for lo, hi in [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 0.9), (0.9, 1.01)]:
        s = pr[(pr.printed >= lo) & (pr.printed < hi)
               & (pr.p_model >= 0.85)]           # the model says "decided"
        if len(s) < 10:
            continue
        ask = s.up_ask.to_numpy()
        ev = (1.0 - fee_per_share(a) for a in ask)
        evs = s.won_up.to_numpy() * np.array([1.0 - fee_per_share(a) for a in ask]) - ask
        print(f"{lo:>7.0%}+ {len(s):>5d} {s.p_model.mean():>8.3f} {ask.mean():>10.4f} "
              f"{(ask - s.p_model).mean():>+7.4f} {s.won_up.mean():>9.1%} "
              f"{(s.won_up.to_numpy() - ask - np.array([fee_per_share(a) for a in ask])).mean():>+10.4f}")
    print("    (ask-p < 0 means the venue is CHEAPER than the model -- the claim)")

    print("\n--- B2. WHERE THE MODEL'S 'EDGE' LIVES (the noise-floor test) ---")
    print("    `disp` = |log(S/K)| in bps. pricing.py's own docstring warns that no")
    print("    threshold below ~5 bps should be trusted: the Binance-vs-Chainlink")
    print("    source difference decides ~9% of rounds and concentrates within 2 bps")
    print("    of the strike. If the model's claimed edge lives ONLY there, it is")
    print("    inside the noise floor of its own input and cannot be traded.")
    print(f"    {'|disp| bps':>12} {'n':>5} {'ask':>6} {'p_model':>8} {'claim':>8} "
          f"{'realised':>9} {'venue mid':>10} {'err(model)':>11} {'err(venue)':>11}")
    disp = (1e4 * np.log(pr.spot.to_numpy() / pr.strike.to_numpy()))
    for lo, hi in [(0.0, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 10.0), (10.0, 1e9)]:
        m = (np.abs(disp) >= lo) & (np.abs(disp) < hi)
        if m.sum() < 20:
            continue
        side_up = pr.p_model.to_numpy()[m] >= 0.5
        p_side = np.where(side_up, pr.p_model.to_numpy()[m], 1 - pr.p_model.to_numpy()[m])
        ask = np.where(side_up, pr.up_ask.to_numpy()[m], pr.dn_ask.to_numpy()[m])
        won = np.where(side_up, y[m] == 1.0, y[m] == 0.0)
        mid = np.where(side_up, pr.mid_up.to_numpy()[m], 1 - pr.mid_up.to_numpy()[m])
        vmid = np.isfinite(mid)
        print(f"    {lo:>5.1f}-{hi if hi < 1e8 else float('inf'):<6} {int(m.sum()):>5d} "
              f"{ask.mean():>6.3f} {p_side.mean():>8.3f} {(ask - p_side).mean():>+8.4f} "
              f"{won.mean():>9.1%} {np.nanmean(mid) if vmid.any() else float('nan'):>10.3f} "
              f"{(p_side - won).mean():>+11.3f} "
              f"{(np.nanmean(mid) - won.mean()) if vmid.any() else float('nan'):>+11.3f}")
    print("    `claim` = model p minus the ask (positive = the model thinks it is cheap)")
    print("    err = the estimate MINUS what happened; the honest column is the last two")

    print("\n--- C. THE GATE ON THE CROSS ARM, AS AN INCREMENT ---")
    trigs = []
    for slug, g in df.groupby("slug", sort=False):
        g = g.sort_values("ts_ms")
        ticks = list(zip(g.ts_ms.to_numpy(dtype=float), g.spot.to_numpy(dtype=float)))
        strike = float(g.strike.iloc[0])
        p = pr[pr.slug == slug]
        if p.empty:
            continue
        done = False
        for i in range(len(ticks)):
            if done:
                break
            side = crossed_side(ticks[:i + 1], strike, 1.0)
            if side is None:
                continue
            near = p[(p.ts_ms >= ticks[i][0] - 1500) & (p.ts_ms <= ticks[i][0] + 1500)]
            if near.empty:
                continue
            row = near.iloc[0]
            ask = float(row.up_ask if side == "Up" else row.dn_ask)
            p_side = float(row.p_model if side == "Up" else 1.0 - row.p_model)
            if not (0.02 < ask < 0.98):
                continue
            won = (row.won_up == 1.0) if side == "Up" else (row.won_up == 0.0)
            trigs.append(dict(slug=slug, ask=ask, p_side=p_side,
                              edge=edge_per_share(p_side, ask),
                              pnl=(1.0 - fee_per_share(ask)) / ask - 1.0 if won else -1.0,
                              day=row.day, tau=row.tau, printed=row.printed))
            done = True
    t = pd.DataFrame(trigs)
    if t.empty:
        print("    no reconstructed triggers"); return
    print(f"    reconstructed triggers n={len(t)} (1 Hz tape; the live arm fires on a "
          f"websocket clock so this UNDERCOUNTS crossings)")
    print(f"    {'cohort':<26} {'n':>4} {'ask':>6} {'p_model':>8} {'win%':>6} "
          f"{'EV/$1':>8} {'per-share':>10}")
    def line(sub, label):
        if len(sub) < 5:
            print(f"    {label:<26} n={len(sub)} (thin)"); return
        v = sub.pnl.to_numpy()
        per = (sub.p_side - sub.ask - np.array([fee_per_share(a) for a in sub.ask]))
        print(f"    {label:<26} {len(sub):>4d} {sub.ask.mean():>6.3f} "
              f"{sub.p_side.mean():>8.3f} {(sub.pnl > 0).mean():>6.1%} "
              f"{v.mean():>+8.3f} {per.mean():>+10.4f}")
    line(t, "ALL triggers")
    for thr in (0.0, 0.02, 0.05, 0.08, 0.10):
        keep = t[t.edge >= thr]
        line(keep, f"edge >= {thr:.2f} (kept)")
        line(t[t.edge < thr], f"  refused by that gate")
    print("\n    THE REFUSED ROW IS THE DECISION: if it is better than the kept row, "
          "the gate is harmful.\n    per-share = p_model - ask - fee (the model's own "
          "estimate of the edge, scored against the price paid).")


if __name__ == "__main__":
    main()
