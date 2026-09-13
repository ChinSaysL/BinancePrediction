"""Turn the HF 1-second tape into a supervised dataset, honestly labelled.

Why this is not the earlier attempt
-----------------------------------
The label here is the SETTLEMENT event, not the tape's own close. metadata.json
carries `eventMetadata.priceToBeat` (Chainlink's price at window start) and
`eventMetadata.finalPrice` (Chainlink's price at window end). The market resolves
on those two numbers -- "Up if the price at the end is >= the price at the start",
resolutionSource chain.link. The tape is Binance, which is why a naive
tape_close >= tape_open reader disagrees with the real outcome on ~4% of windows.

So we get three things the earlier kline work never had:
  * a CONTINUOUS target -- true_delta_bps = (finalPrice - priceToBeat)/priceToBeat,
    which carries information the binary throw away;
  * the true strike, so `cl_delta_bps` (tape price now vs the CHAINLINK strike)
    is available, which is the quantity the market actually settles on;
  * a causal, 1-second-resolution view of the tape that produced it.

Causality
---------
For a decision with tau seconds left, only tape rows 0..(300-tau) are read.
Volatility references are trailing-window estimates that reach back into the
previous window's tape when it is contiguous -- never forward. `day` is emitted
so cross-validation can be grouped by day; shuffling windows leaks regime.

Output: one row per (window, tau).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

WINDOW_S = 300
TAU_GRID = [3, 5, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 75, 90, 105, 120,
            150, 180, 210, 240, 270]


def trailing_std(x: np.ndarray, w: int) -> np.ndarray:
    """Std of the last `w` observations ending at (and including) each index."""
    n = len(x)
    if n == 0:
        return np.empty(0)
    c1 = np.concatenate([[0.0], np.cumsum(x)])
    c2 = np.concatenate([[0.0], np.cumsum(x * x)])
    j = np.arange(n)
    lo = np.maximum(0, j - w + 1)
    cnt = (j - lo + 1).astype(float)
    s1 = c1[j + 1] - c1[lo]
    s2 = c2[j + 1] - c2[lo]
    var = np.maximum(s2 / cnt - (s1 / cnt) ** 2, 0.0)
    out = np.sqrt(var)
    out[cnt < 5] = np.nan
    return out


def _crossings(sign: np.ndarray) -> int:
    s = sign[sign != 0]
    return int((s[1:] != s[:-1]).sum()) if len(s) > 1 else 0


def window_rows(path: str, taus, prev: tuple | None) -> list[dict]:
    d = os.path.dirname(path)
    mp = os.path.join(d, "metadata.json")
    if not os.path.exists(mp):
        return []
    try:
        meta = json.load(open(mp))
        ev = meta["events"][0]["eventMetadata"]
        strike_c = float(ev["priceToBeat"])
        final_c = float(ev["finalPrice"])
    except Exception:
        return []
    if not np.isfinite(strike_c) or strike_c <= 0 or not np.isfinite(final_c):
        return []
    try:
        t = pd.read_parquet(path)
    except Exception:
        return []
    if len(t) < WINDOW_S:
        return []

    c = t["close"].to_numpy(float)
    o = t["open"].to_numpy(float)
    hi = t["high"].to_numpy(float)
    lo = t["low"].to_numpy(float)
    v = t["volume"].to_numpy(float)
    tb = t["taker_buy_volume"].to_numpy(float)
    ts_ = t["taker_sell_volume"].to_numpy(float)
    ntr = t["n_trades"].to_numpy(float)
    vwap = t["vwap"].to_numpy(float)
    notrade = t["no_trade"].to_numpy(float)
    ssl = t["seconds_since_last_trade"].to_numpy(float)
    start = int(t["timestamp"].iloc[0])
    if not np.isfinite(c).all() or (c <= 0).any():
        return []

    strike_t = float(o[0])
    logc = np.log(c)
    ret1 = np.diff(logc, prepend=logc[0])

    # contiguous previous window gives real trailing history
    prev_p, prev_r, prev_v, prev_tb, prev_ts, prev_nt, prev_ntr = (
        prev if prev is not None else (None,) * 7)
    if prev_p is not None and len(prev_p) == WINDOW_S and prev_r is not None:
        ap = np.concatenate([prev_p, c])
        ar = np.concatenate([prev_r, ret1])
        av = np.concatenate([prev_v, v])
        atb = np.concatenate([prev_tb, tb])
        ats = np.concatenate([prev_ts, ts_])
        ant = np.concatenate([prev_nt, notrade])
        antr = np.concatenate([prev_ntr, ntr])
        plen = WINDOW_S
    else:
        ap, ar, av, atb, ats, ant, antr = c, ret1, v, tb, ts_, notrade, ntr
        plen = 0

    sd300 = trailing_std(ar, 300)
    sd60 = trailing_std(ar, 60)
    sd10 = trailing_std(ar, 10)
    logap = np.log(ap)
    cum_v = np.cumsum(av)
    cum_tb = np.cumsum(atb)
    cum_ts = np.cumsum(ats)
    cum_ntr = np.cumsum(antr)
    cum_nt = np.cumsum(ant)
    above_t = (c > strike_t).astype(float)
    above_c = (c > strike_c).astype(float)

    true_delta_bps = (final_c - strike_c) / strike_c * 1e4
    basis_bps = (strike_t - strike_c) / strike_c * 1e4
    sec_of_day = (start % 86400)
    dow = (start // 86400 + 4) % 7
    sid = f"{os.path.basename(os.path.dirname(d))}/{os.path.basename(d)}"
    out = []
    for tau in taus:
        i = WINDOW_S - tau
        if i < 2 or i >= WINDOW_S:
            continue
        g = plen + i
        n_el = i + 1
        px = c[i]
        delta_bps = (px - strike_t) / strike_t * 1e4
        cl_delta_bps = (px - strike_c) / strike_c * 1e4
        sref = sd300[g] if np.isfinite(sd300[g]) else np.nan
        smid = sd60[g] if np.isfinite(sd60[g]) else np.nan
        sshort = sd10[g] if np.isfinite(sd10[g]) else np.nan
        if not np.isfinite(sref) or sref <= 0:
            sref = 1e-5
        z_tau = cl_delta_bps * 1e-4 / (sref * np.sqrt(max(tau, 1)))
        z_el = cl_delta_bps * 1e-4 / (sref * np.sqrt(n_el))

        def mom(L):
            if g - L < 0:
                return np.nan
            return float((logap[g] - logap[g - L]) * 1e4)

        def mom_z(L):
            if g - L < 0:
                return np.nan
            return float((logap[g] - logap[g - L]) / (sref * np.sqrt(L)))

        rng = float(hi[:n_el].max() - lo[:n_el].min()) / strike_t * 1e4
        rngpos = ((px - lo[:n_el].min()) / (hi[:n_el].max() - lo[:n_el].min())
                  if hi[:n_el].max() > lo[:n_el].min() else 0.5)
        max_exc = float((hi[:n_el].max() - strike_c) / strike_c * 1e4)
        min_exc = float((lo[:n_el].min() - strike_c) / strike_c * 1e4)
        path = float(np.abs(ret1[:n_el]).sum() * 1e4)
        net = abs(delta_bps)
        sign = np.sign(c[:n_el] - strike_c)
        streak = 0
        sgn = np.sign(ret1[max(0, i - 59):i + 1])
        if len(sgn):
            last = sgn[-1]
            for x in sgn[::-1]:
                if x == last and x != 0:
                    streak += 1
                else:
                    break
        vol_ref_pre = (cum_v[g - 300] if g >= 300 else 0.0)
        rows = {
            "window_id": sid, "start_ms": start, "day": start // 86400,
            "tau_s": tau, "dow": dow, "sec_of_day": sec_of_day,
            "strike_tape": strike_t, "strike_cl": strike_c, "final_cl": final_c,
            "px": px, "close_end_tape": float(c[-1]), "open_tape": strike_t,
            "true_delta_bps": true_delta_bps,
            "basis_bps": basis_bps,
            "y_up": 1 if final_c >= strike_c else 0,
            "y_tape": 1 if c[-1] >= strike_t else 0,
            "delta_bps": delta_bps,
            "cl_delta_bps": cl_delta_bps,
            "z_tau": z_tau,
            "z_tau_sqrt": float(np.copysign(np.sqrt(abs(z_tau)), z_tau)),
            "z_elapsed": z_el,
            "sigma_ref_bps": sref * 1e4,
            "sigma_mid_bps": (smid if np.isfinite(smid) else sref) * 1e4,
            "sigma_short_bps": (sshort if np.isfinite(sshort) else sref) * 1e4,
            "vol_ratio_short": (sshort / sref if np.isfinite(sshort) and sref else np.nan),
            "vol_ratio_mid": (smid / sref if np.isfinite(smid) and sref else np.nan),
            "mom_1_bps": mom(1), "mom_2_bps": mom(2), "mom_5_bps": mom(5),
            "mom_10_bps": mom(10), "mom_20_bps": mom(20), "mom_30_bps": mom(30),
            "mom_60_bps": mom(60), "mom_120_bps": mom(120), "mom_300_bps": mom(300),
            "mom_z_10": mom_z(10), "mom_z_30": mom_z(30), "mom_z_60": mom_z(60),
            "mom_z_120": mom_z(120),
            "accel_5_20": (mom(5) or 0.0) - (mom(20) or 0.0) / 4.0,
            "range_bps": rng, "range_pos": float(rngpos),
            "max_exc_bps": max_exc, "min_exc_bps": min_exc,
            "dist_to_max_bps": float((hi[:n_el].max() - px) / px * 1e4),
            "dist_to_min_bps": float((px - lo[:n_el].min()) / px * 1e4),
            "frac_above_tape": float(above_t[:n_el].mean()),
            "frac_above_cl": float(above_c[:n_el].mean()),
            "crossings_tape": _crossings(sign),
            "crossings_per_min": _crossings(sign) / (n_el / 60.0),
            "path_bps": path,
            "efficiency": (net / path if path > 0 else 0.0),
            "path_over_net": (path / net if net > 1e-9 else 10.0),
            "vol_sum": float(cum_v[g]),
            "vol_per_s": float(cum_v[g] / n_el),
            "vol_surge": (float(cum_v[g] / n_el / (vol_ref_pre / 300.0))
                          if vol_ref_pre > 0 else np.nan),
            "cvd": float(cum_tb[g] - cum_ts[g]),
            "cvd_ratio": (float((cum_tb[g] - cum_ts[g]) / cum_v[g])
                          if cum_v[g] > 0 else 0.0),
            "cvd_10_ratio": (
                float((atb[g - 9:g + 1].sum() - ats[g - 9:g + 1].sum())
                      / av[g - 9:g + 1].sum())
                if g >= 9 and av[g - 9:g + 1].sum() > 0 else 0.0),
            "taker_buy_ratio": (float(cum_tb[g] / cum_v[g]) if cum_v[g] > 0 else 0.5),
            "n_trades": float(cum_ntr[g]),
            "trades_per_s": float(cum_ntr[g] / n_el),
            "no_trade_frac": float(cum_nt[g] / n_el),
            "ssl": float(ssl[i]),
            "vwap_dev_bps": float((px - vwap[i]) / px * 1e4),
            "wick_up_bps": float((hi[i] - px) / px * 1e4),
            "wick_dn_bps": float((px - lo[i]) / px * 1e4),
            "last_ret_bps": float(ret1[i] * 1e4),
            "ret_streak": streak,
            "dist_round100_bps": float(abs(px - round(px / 100.0) * 100.0) / px * 1e4),
            "elapsed_s": n_el,
        }
        out.append(rows)
    return out


def _day_worker(args):
    day, taus = args
    files = sorted(glob.glob(os.path.join("data/btc5m/data", day, "*", "ticks_dense.parquet")))
    rows = []
    prev_start = None
    prev = None
    for f in files:
        try:
            t = pd.read_parquet(f, columns=["timestamp", "open", "close", "volume",
                                            "taker_buy_volume", "taker_sell_volume",
                                            "n_trades", "no_trade"])
        except Exception:
            prev = None
            continue
        if len(t) < WINDOW_S:
            prev = None
            continue
        start = int(t["timestamp"].iloc[0])
        c = t["close"].to_numpy(float)
        logc = np.log(np.maximum(c, 1e-12))
        ret1 = np.diff(logc, prepend=logc[0])
        cur = (c, ret1, t["volume"].to_numpy(float),
               t["taker_buy_volume"].to_numpy(float),
               t["taker_sell_volume"].to_numpy(float),
               t["no_trade"].to_numpy(float), t["n_trades"].to_numpy(float))
        use_prev = prev if (prev_start is not None and start - prev_start == WINDOW_S) else None
        rows.extend(window_rows(f, taus, use_prev))
        prev_start, prev = start, cur
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/btc5m_ml.parquet")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--taus", type=int, nargs="+", default=TAU_GRID)
    ap.add_argument("--limit-days", type=int, default=0)
    args = ap.parse_args()

    days = sorted(os.path.basename(d) for d in
                  glob.glob("data/btc5m/data/*") if os.path.isdir(d))
    if args.limit_days:
        days = days[:args.limit_days]
    print(f"days: {len(days)} ({days[0]} .. {days[-1]})  taus: {len(args.taus)}")
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        chunks = list(ex.map(_day_worker, [(d, args.taus) for d in days]))
    rows = [r for ch in chunks for r in ch]
    df = pd.DataFrame(rows)
    if df.empty:
        print("no rows"); return
    df = df.sort_values(["start_ms", "tau_s"]).reset_index(drop=True)
    df.to_parquet(args.out, index=False)
    print(f"windows: {df['window_id'].nunique():,}  rows: {len(df):,}  "
          f"cols: {df.shape[1]}")
    print(f"days: {df['day'].nunique()}  -> {args.out} "
          f"({os.path.getsize(args.out) / 1e6:.1f} MB)")
    print(f"up rate (settlement): {df['y_up'].mean():.4f}   "
          f"tape-reader agrees: {(df['y_tape'] == df['y_up']).mean():.4f}")


if __name__ == "__main__":
    main()
