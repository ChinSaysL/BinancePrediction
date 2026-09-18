"""THE SAME FORECAST TEST ON THE 13 GB CORPUS (2,240 rounds, not 247).

WHY. The live verdict (model Brier 0.246 vs the venue's 0.111) rests on 247
Polymarket rounds / 1.5 days, because that is all the live DB holds. That is thin
enough that "the model is under-informed" and "my sample is too small" are not
separable. `data/btc5m` holds 53 days of BTC 5m rounds and, for 8 of them,
`btc5m_prices.parquet` = per-round PM prices with BOTH sides (bid and ask) --
**2,240 rounds with the spot tape, the book, and the settlement.** This runs the
identical measurement there: same deployed `fair_value`, same TAIL window, same
`window_log_integral` for the printed part, same Brier + edge-decile + money test.

WHAT THIS CAN AND CANNOT SETTLE
  CAN: whether the model's failure is a property of the MODEL or of the sample.
       A model with no edge anywhere shows no edge here either, and 9x the sample
       makes that a verdict rather than a hint.
  CANNOT: it is a DIFFERENT ERA (2026-04-29..06-20 vs the live 2026-09-16..17) and
       a THINNER book -- this repo has already measured that the corpus's asks
       under-react vs the live venue (0.5310 vs 0.5739 at |d| 1-2 bps). So a
       POSITIVE result here would not be shippable; it would say "revisit live
       when the DB is bigger". A NEGATIVE one closes the question for good.

Sigma is the bot's convention (stdev of 1-minute log returns over a trailing 240
minutes) computed from the STITCHED 1-second tape, which the corpus makes possible
and the live DB does not -- the live quotes hold ~300 s per round, so the live run
had to substitute a within-round estimate.

Usage: python3 tools/pm_corpus_forecast_check.py
"""
from __future__ import annotations

import json
import math
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from btcpredict.venues.polymarket.pricing import (   # noqa: E402
    TAIL, TWAP_LOOKBACK_S, fair_value, fee_per_share, window_log_integral)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "btc5m"
PRICES = ROOT / "data" / "btc5m_prices.parquet"
TAU_LO, TAU_HI = 5.0, 60.0
SIGMA_MIN = 240          # one-minute returns used for sigma


def day_series(date: str) -> tuple[np.ndarray, np.ndarray]:
    """(ts_ms, open) stitched across the whole day's rounds, 1 s grid.

    Stitching matters for two reasons: the bot's sigma is a 240-MINUTE estimator
    and a single round only holds 300 s; and `window_log_integral` wants the
    samples that sit before the round opened.
    """
    base = CORPUS / "data" / date
    frames = []
    for d in sorted(base.iterdir()):
        p = d / "ticks_dense.parquet"
        if not p.exists():
            continue
        try:
            t = pd.read_parquet(p, columns=["timestamp", "open"])
        except Exception:
            continue
        if t.empty:
            continue
        frames.append(t)
    if not frames:
        return np.array([]), np.array([])
    allt = pd.concat(frames, ignore_index=True).dropna()
    allt = allt.sort_values("timestamp").drop_duplicates("timestamp")
    return (allt["timestamp"].to_numpy(dtype=np.int64) * 1000,
            allt["open"].to_numpy(dtype=float))


def sigma_bot(ts: np.ndarray, px: np.ndarray, at_ms: float,
              cache: dict) -> float | None:
    """Bot convention: stdev of the last 240 ONE-MINUTE log returns."""
    key = int(at_ms // 60_000)
    if key in cache:
        return cache[key]
    m = ts <= at_ms
    if m.sum() < 60:
        cache[key] = None
        return None
    t, p = ts[m], px[m]
    # one sample per minute: the first 1 s observation of each minute
    mins = t // 60_000
    _, idx = np.unique(mins, return_index=True)
    idx = np.sort(idx)
    if len(idx) < 30:
        cache[key] = None
        return None
    pp = p[idx][-SIGMA_MIN:]
    if len(pp) < 30 or np.any(pp <= 0):
        cache[key] = None
        return None
    r = np.diff(np.log(pp))
    sd = float(np.std(r))
    out = sd if sd > 0 else None
    cache[key] = out
    return out


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def main() -> None:
    px_all = pd.read_parquet(PRICES)
    dates = sorted({w.split("/")[0] for w in px_all.window_id.unique()})
    print(f"corpus priced windows: {px_all.window_id.nunique()} over {len(dates)} days "
          f"({dates[0]} .. {dates[-1]})")
    rows = []
    for date in dates:
        ts, sp = day_series(date)
        if not len(ts):
            continue
        cache: dict = {}
        day = px_all[px_all.window_id.str.startswith(date + "/")]
        for wid, g in day.groupby("window_id", sort=False):
            start = wid.split("/")[1]
            md = CORPUS / "data" / date / start / "metadata.json"
            if not md.exists():
                continue
            try:
                meta = json.loads(md.read_text())
            except Exception:
                continue
            ev = meta.get("events", [{}])[0].get("eventMetadata", {})
            K, fin = ev.get("priceToBeat"), ev.get("finalPrice")
            if K is None or fin is None:
                continue
            K_chain = float(K)
            won_up = 1.0 if float(fin) >= K_chain else 0.0
            end_ms = (int(start) + 300) * 1000
            # THE STRIKE MUST COME FROM THE SAME SOURCE AS THE SPOT SERIES.
            # `priceToBeat` is CHAINLINK (it equals the previous round's
            # `finalPrice`, i.e. the boundary value on that feed); the spot below
            # is BINANCE. Mixing them injects the source gap -- measured here at
            # 2.2 bps on the first round of the corpus -- as if it were
            # displacement, and the model then prices a stale strike. The live
            # bot avoids this: `_pm_strike_backfill` takes the round's START price
            # from the live spot tape. This does the same.
            start_ms = int(start) * 1000
            m0 = ts <= start_ms
            if not m0.sum():
                continue
            K = float(sp[m0][-1])            # Binance-consistent strike
            if K <= 0:
                continue
            chain_gap_bps = 1e4 * (K - K_chain) / K_chain
            g = g.sort_values("tau_s", ascending=False)
            for r in g.itertuples():
                tau = float(r.tau_s)
                if not (TAU_LO <= tau <= TAU_HI):
                    continue
                now = end_ms - tau * 1000.0
                # only the samples the WINDOW can use: [now-60, now] plus a lead-in
                # for the hold-forward anchor. Passing the whole day makes
                # `window_log_integral` O(100k) per row instead of O(130).
                m = (ts <= now) & (ts >= now - 130_000)
                if m.sum() < 5:
                    continue
                spot = float(sp[m][-1])
                sig = sigma_bot(ts, sp, now, cache)
                if sig is None or spot <= 0:
                    continue
                J = (None if tau >= TWAP_LOOKBACK_S else
                     window_log_integral(
                         list(zip(ts[m].tolist(), sp[m].tolist())), K,
                         open_ms=end_ms - TWAP_LOOKBACK_S * 1000.0,
                         close_ms=now))
                p_up, _a = fair_value(spot, K, tau, sig, window_log_int=J,
                                      window=TAIL)
                au, ad = float(r.up_sell_px), float(r.dn_sell_px)   # asks
                bu, bd = float(r.up_buy_px), float(r.dn_buy_px)     # bids
                if not (0.01 < au < 0.99 and 0.01 < ad < 0.99):
                    continue
                mid = (bu + au) / 2.0 if 0 < bu < 1 else np.nan
                rows.append(dict(slug=wid, tau=tau, p_model=p_up, mid_up=mid,
                                 up_ask=au, dn_ask=ad, won_up=won_up,
                                 spot=spot, strike=K, sigma=sig,
                                 J=(np.nan if J is None else J),
                                 chain_gap=chain_gap_bps, day=date))
    d = pd.DataFrame(rows)
    if d.empty:
        print("no rows"); return
    y = d.won_up.to_numpy()
    pr = d
    print(f"=== {pr.slug.nunique()} rounds, {len(pr)} snapshots, tau "
          f"[{TAU_LO:.0f},{TAU_HI:.0f}]s, sigma = 240 one-minute returns, "
          f"strike = BINANCE spot at the round start ===")
    print(f"    realised Up {y.mean():.3f} | model mean {pr.p_model.mean():.3f} | "
          f"venue mid mean {pr.mid_up.mean():.3f} | sigma median {pr.sigma.median():.2e}")
    g = pr.chain_gap.dropna()
    if len(g):
        print(f"    Chainlink-vs-Binance boundary gap: median {g.median():+.2f} bps  "
              f"p10 {g.quantile(.1):+.2f}  p90 {g.quantile(.9):+.2f}  "
              f"|gap| > 2 bps on {(g.abs() > 2).mean():.1%} of rounds")

    ok = pr.mid_up.notna().to_numpy()
    bm = brier(pr.p_model.to_numpy(), y)
    bv = brier(pr.mid_up.to_numpy()[ok], y[ok])
    print(f"\n--- A. CALIBRATION (Brier, lower better) ---")
    print(f"    model      {bm:.4f}")
    print(f"    venue mid  {bv:.4f}   (n={ok.sum()})")
    print(f"    coin flip  0.2500")
    print(f"    by day:")
    for day, g in pr.groupby("day"):
        gg = g.mid_up.notna().to_numpy()
        if len(g) < 50:
            continue
        print(f"      {day}  n={len(g):5d}  model {brier(g.p_model.to_numpy(), g.won_up.to_numpy()):.4f}"
              f"  venue {brier(g.mid_up.to_numpy()[gg], g.won_up.to_numpy()[gg]):.4f}")

    print(f"\n--- B. WHERE THE ERROR IS (model vs venue, by |disp|) ---")
    disp = 1e4 * np.log(pr.spot.to_numpy() / pr.strike.to_numpy())
    print(f"    {'|disp|':>10} {'n':>6} {'ask':>6} {'model':>7} {'venue':>7} "
          f"{'realised':>9} {'err(m)':>8} {'err(v)':>8}")
    for lo, hi in [(0.0, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 10.0), (10.0, 1e9)]:
        m = (np.abs(disp) >= lo) & (np.abs(disp) < hi)
        if m.sum() < 30:
            continue
        up = pr.p_model.to_numpy()[m] >= 0.5
        ps = np.where(up, pr.p_model.to_numpy()[m], 1 - pr.p_model.to_numpy()[m])
        ask = np.where(up, pr.up_ask.to_numpy()[m], pr.dn_ask.to_numpy()[m])
        won = np.where(up, y[m] == 1.0, y[m] == 0.0)
        mid = np.where(up, pr.mid_up.to_numpy()[m], 1 - pr.mid_up.to_numpy()[m])
        v = np.isfinite(mid)
        print(f"    {lo:>4.1f}-{hi if hi < 1e8 else float('inf'):<5} {int(m.sum()):>6d} "
              f"{ask.mean():>6.3f} {ps.mean():>7.3f} "
              f"{np.nanmean(mid) if v.any() else float('nan'):>7.3f} {won.mean():>9.1%} "
              f"{(ps - won).mean():>+8.3f} "
              f"{(np.nanmean(mid) - won.mean()) if v.any() else float('nan'):>+8.3f}")

    print(f"\n--- C. THE MONEY TEST: best-edge pick per round, band 0.10-0.90 ---")
    print("    the VENUE row is the control: if the venue's OWN mid also shows a")
    print("    positive edge here, the corpus's prices are cheap, not the model good")
    print("    (this repo has already measured the corpus ask under-reacting vs the")
    print("    live venue: 0.5310 vs 0.5739 at |d| 1-2 bps).")
    for col, name in (("p_model", "MODEL"), ("mid_up", "VENUE (control)")):
        rows2 = []
        for slug, g in pr.groupby("slug", sort=False):
            gg = g[np.isfinite(g[col].to_numpy())]
            if gg.empty:
                continue
            pu = gg[col].to_numpy()
            au, ad = gg.up_ask.to_numpy(), gg.dn_ask.to_numpy()
            e_up = pu - au - np.array([fee_per_share(a) for a in au])
            e_dn = (1 - pu) - ad - np.array([fee_per_share(a) for a in ad])
            band_u, band_d = (au >= 0.10) & (au <= 0.90), (ad >= 0.10) & (ad <= 0.90)
            e_up = np.where(band_u, e_up, -9.0)
            e_dn = np.where(band_d, e_dn, -9.0)
            i = int(np.argmax(np.maximum(e_up, e_dn)))
            up = e_up[i] >= e_dn[i]
            if max(e_up[i], e_dn[i]) < -8.0:
                continue
            dp = 1e4 * np.log(gg.spot.to_numpy()[i] / gg.strike.to_numpy()[i])
            rows2.append(dict(edge=float(max(e_up[i], e_dn[i])),
                              ask=float(au[i] if up else ad[i]),
                              absdisp=abs(float(dp)), tau=float(gg.tau.to_numpy()[i]),
                              won=bool((gg.won_up.iloc[i] == 1.0) if up
                                       else (gg.won_up.iloc[i] == 0.0)),
                              day=str(gg.day.iloc[0])))
        dd = pd.DataFrame(rows2)
        if dd.empty:
            continue
        dd["per"] = (dd.won.astype(float) - dd.ask
                     - np.array([fee_per_share(a) for a in dd.ask]))
        print(f"\n    {name}: rounds {len(dd)} | claimed edge median {dd.edge.median():+.4f} "
              f"| REALISED per-share {dd.per.mean():+.4f}")
        dm = [dd.per[dd.day.values == x].mean() for x in dd.day.unique()]
        print(f"      ALL n={len(dd)} ask {dd.ask.mean():.3f} win {dd.won.mean():.1%} "
              f"|disp| med {dd.absdisp.median():.2f} bps tau med {dd.tau.median():.0f}s "
              f"day-t {np.mean(dm) / (np.std(dm) / math.sqrt(len(dm))) if np.std(dm) else 0.0:+.2f}")
        for thr in (0.02, 0.10):
            s = dd[dd.edge >= thr]
            if len(s) < 10:
                print(f"      edge >= {thr:+.2f}  n={len(s)} (thin)"); continue
            dm = [s.per[s.day.values == x].mean() for x in s.day.unique()]
            print(f"      edge >= {thr:+.2f}  n={len(s):4d} ask {s.ask.mean():.3f} "
                  f"win {s.won.mean():5.1%} per-share {s.per.mean():+.4f} "
                  f"|disp| med {s.absdisp.median():.2f} tau med {s.tau.median():.0f}s "
                  f"day-t {np.mean(dm) / (np.std(dm) / math.sqrt(len(dm))) if np.std(dm) else 0.0:+.2f}")
        # the per-dollar estimator is leverage-dominated; per-share is the honest one
        print(f"      per-$1 mean {np.mean(np.where(dd.won, (1 - np.array([fee_per_share(a) for a in dd.ask])) / dd.ask - 1, -1)):+.3f} "
              f"(leverage-dominated, shown only for scale)")

    print(f"\n--- D. IS THE CLAIMED EDGE INFORMATIVE? (deciles) ---")
    rr = []
    for slug, g in pr.groupby("slug", sort=False):
        pu = g.p_model.to_numpy()
        au, ad = g.up_ask.to_numpy(), g.dn_ask.to_numpy()
        e_up = pu - au - np.array([fee_per_share(a) for a in au])
        e_dn = (1 - pu) - ad - np.array([fee_per_share(a) for a in ad])
        for e_arr, ask_arr, is_up in ((e_up, au, True), (e_dn, ad, False)):
            for k in range(len(g)):
                if not (0.10 <= ask_arr[k] <= 0.90):
                    continue
                won = ((g.won_up.iloc[k] == 1.0) if is_up
                       else (g.won_up.iloc[k] == 0.0))
                rr.append(dict(claimed=float(e_arr[k]), ask=float(ask_arr[k]),
                               per=float((1.0 if won else 0.0) - ask_arr[k]
                                         - fee_per_share(ask_arr[k]))))
    rd = pd.DataFrame(rr)
    rd["dec"] = pd.qcut(rd.claimed, 10, labels=False, duplicates="drop")
    print(rd.groupby("dec").agg(n=("claimed", "size"), claimed=("claimed", "mean"),
                                realised=("per", "mean"),
                                ask=("ask", "mean")).round(4).to_string())
    print(f"    corr(claimed edge, realised per-share) = "
          f"{np.corrcoef(rd.claimed, rd.per)[0, 1]:+.3f}   "
          f"(a gate NEEDS this positive and large)")


if __name__ == "__main__":
    main()
