"""HorseDev77's full feature set, ported and backtested.

Their repo ships features + a heuristic P(up) + a regime state machine + gates,
but no data, no backtest and no results. This ports the logic verbatim onto real
1-second tape and scores it.

Ported exactly (thresholds from their src/config.ts):
  features : CVD(60s) ratio, CVD acceleration(30s), VWAP distance (+z),
             volatility compression (variance ratio), range position (5m window)
  regime   : post_liquidation / vol_compression(>=0.4) / trending(|cvd|>=0.15 and
             variance ratio >=0.7) / ranging / unknown
  decision : heuristic P(up) weights (cvd .25, obi .20, range +-0.1, vwap +-0.08,
             oi/cvd agreement .05, cvd accel .03), gates pHigh=0.58,
             pConfidence=0.58, OBI confirmation |obi|>=0.05

Two inputs cannot be reproduced and are handled explicitly, not faked:
  * OBI (order-book imbalance) -- the corpus has no book. A CVD-sign substitute
    is used AND the whole backtest is re-run with the gate disabled, so its
    contribution is visible rather than assumed.
  * OI (open interest) -- Binance serves only 30 days of OI history (Aug-Sep) and
    this corpus is Apr-Jun, so there is no overlap. OI terms are omitted.

Scoring note: the corpus has outcomes but no prices. So this reports the HOLD
RATE of every signal it fires -- and the fair price that hold rate implies --
which is then compared against the ask our own venue actually charges in the same
delta band. A signal is only worth money if its hold rate exceeds that price.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os

import numpy as np
import pandas as pd

LAMBDA_OBI = 0.2          # their obiLambda
OBI_LEVELS = 5


def sig(x: float) -> float:
    return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)


def heuristic_p_up(cvd_ratio: float, obi: float, range_pos: float,
                   dist_vwap: float, cvd_accel: float) -> float:
    """Their heuristicPUp(), minus the two OI terms (no history)."""
    s = 0.5
    s += cvd_ratio * 0.25
    s += obi * 0.2
    if range_pos > 0.8:
        s += 0.1
    if range_pos < 0.2:
        s -= 0.1
    if dist_vwap > 0.002:
        s -= 0.08
    if dist_vwap < -0.002:
        s += 0.08
    s += 0.03 * sig(cvd_accel)
    return max(0.05, min(0.95, s))


def classify_regime(cvd_ratio: float, vol_compress: float,
                    range_pos: float, variance_ratio: float) -> str:
    """Their classifyRegime(), same thresholds and same order of checks."""
    if vol_compress >= 0.4:
        return "vol_compression"
    if abs(cvd_ratio) >= 0.15 and variance_ratio >= 0.7:
        return "trending"
    extreme = range_pos <= 0.2 or range_pos >= 0.8
    if not extreme and vol_compress > 0.2:
        return "ranging"
    return "unknown"


def regime_allows(regime: str, direction: str, cvd_ratio: float) -> bool:
    """Their regimeAllowsTrade(): ranging needs flow agreement, unknown is blocked."""
    if regime == "unknown":
        return False
    if regime == "ranging":
        return sig(cvd_ratio) == (1.0 if direction == "UP" else -1.0)
    return True


def decide(p_up: float, cvd_ratio: float, obi: float, regime: str,
           range_pos: float, dist_vwap: float, cvd_accel: float,
           obi_gate: bool = True) -> tuple[str, str]:
    th_high, th_conf, obi_confirm = 0.58, 0.58, 0.05
    conf = max(p_up, 1 - p_up)
    direction = "UP" if p_up >= th_high else ("DOWN" if p_up <= 1 - th_high else "NO_TRADE")
    if direction == "NO_TRADE":
        return direction, "below_probability_threshold"
    if conf < th_conf:
        return "NO_TRADE", "below_confidence_gate"
    if obi_gate:
        ok = (direction == "UP" and obi >= obi_confirm) or \
             (direction == "DOWN" and obi <= -obi_confirm)
        if not ok:
            return "NO_TRADE", "obi_not_confirming"
    if not regime_allows(regime, direction, cvd_ratio):
        return "NO_TRADE", f"regime_block:{regime}"
    return direction, "ok"


def features(t: pd.DataFrame, i: int) -> dict | None:
    vol = t["volume"].to_numpy(float)
    buy = t["taker_buy_volume"].to_numpy(float)
    sell = t["taker_sell_volume"].to_numpy(float)
    close = t["close"].to_numpy(float)
    vw = t["vwap"].to_numpy(float)
    high = t["high"].to_numpy(float)
    low = t["low"].to_numpy(float)
    if i < 130:
        return None
    w60 = slice(max(0, i - 59), i + 1)
    w30a, w30b = slice(max(0, i - 29), i + 1), slice(max(0, i - 59), max(0, i - 29))
    tv60 = vol[w60].sum()
    cvd60 = (buy[w60].sum() - sell[w60].sum()) / tv60 if tv60 > 0 else 0.0
    cvd_a = (buy[w30a].sum() - sell[w30a].sum()) - (buy[w30b].sum() - sell[w30b].sum())
    tv = vol[:i + 1].sum()
    vwap = float((vw[:i + 1] * vol[:i + 1]).sum() / tv) if tv > 0 else close[i]
    dist_vwap = (close[i] - vwap) / vwap if vwap > 0 else 0.0
    r = np.diff(close[i - 60:i + 2]) if i >= 61 else np.array([0.0])
    r_long = np.diff(close[:i + 1]) if i > 1 else np.array([0.0])
    v_short = float(np.var(r)) if len(r) > 1 else 0.0
    v_long = float(np.var(r_long)) if len(r_long) > 1 else 0.0
    vr = (v_short / v_long) if v_long > 0 else 1.0
    vol_compress = max(0.0, min(1.0, 1.0 - vr))
    hi, lo = high[:i + 1].max(), low[:i + 1].min()
    range_pos = (close[i] - lo) / (hi - lo) if hi > lo else 0.5
    return {"cvd": cvd60, "cvd_accel": cvd_a, "dist_vwap": dist_vwap,
            "vol_compress": vol_compress, "vr": vr, "range_pos": range_pos}


def battery(win: np.ndarray, days: np.ndarray) -> dict:
    s = {"n": len(win), "wr": float(win.mean())}
    sd = win.std(ddof=1) if len(win) > 1 else 0.0
    s["t"] = float(win.mean() / (sd / math.sqrt(len(win)))) if sd > 0 else float("nan")
    d = pd.DataFrame({"w": win, "day": days})
    g = d.groupby("day")["w"].mean()
    s["t_day"] = (float(g.mean() / (g.std(ddof=1) / math.sqrt(len(g))))
                  if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = len(win) // 2
    s["h1"], s["h2"] = float(win[:h].mean()), float(win[h:].mean())
    s["drop1"] = float(d[d["day"] != g.idxmax()]["w"].mean())
    return s


def line(tag: str, s: dict) -> str:
    return (f"{tag:30s} n={s['n']:5d}  hold {s['wr']:6.1%}  t {s['t']:+5.2f}  "
            f"day-t {s['t_day']:+5.2f}  halves {s['h1']:5.1%}/{s['h2']:5.1%}  "
            f"drop1 {s['drop1']:5.1%}")


def load(root: str) -> pd.DataFrame:
    rows, reasons = [], {}
    for f in sorted(glob.glob(os.path.join(root, "data", "*", "*", "ticks_dense.parquet"))):
        d = os.path.dirname(f)
        mp = os.path.join(d, "metadata.json")
        if not os.path.exists(mp):
            continue
        try:
            t = pd.read_parquet(f)
            m = json.load(open(mp))
            outs, pr = m.get("outcomes"), m.get("outcomePrices")
            outs = json.loads(outs) if isinstance(outs, str) else outs
            pr = json.loads(pr) if isinstance(pr, str) else pr
            if not outs or not pr or "Up" not in outs:
                continue
            up_won = float(pr[outs.index("Up")]) > 0.5
        except Exception:  # noqa: BLE001
            continue
        if len(t) < 200:
            continue
        i = len(t) - 10                     # features as of tau=10s
        fx = features(t, i)
        if not fx:
            continue
        strike = float(t["open"].iloc[0])
        delta = (float(t["close"].iloc[i]) - strike) / strike * 100.0
        # OBI is unavailable: substitute its sign with normalised CVD, magnitudes
        # scaled to their obiConfirm band so the gate behaves comparably.
        obi = max(-1.0, min(1.0, fx["cvd"] * 2.0))
        rec = {"date": f.split("/")[-3], "delta": delta, "absd": abs(delta),
               "obi": obi, "up_won": up_won, **fx}
        for gate in (True, False):
            dr, why = decide(heuristic_p_up(fx["cvd"], obi, fx["range_pos"],
                                            fx["dist_vwap"], fx["cvd_accel"]),
                             fx["cvd"], obi,
                             classify_regime(fx["cvd"], fx["vol_compress"],
                                             fx["range_pos"], fx["vr"]),
                             fx["range_pos"], fx["dist_vwap"], fx["cvd_accel"],
                             obi_gate=gate)
            rec[f"dir_{gate}"] = dr
            rec[f"why_{gate}"] = why
            rec[f"win_{gate}"] = None if dr == "NO_TRADE" else ((dr == "UP") == up_won)
            reasons[f"{gate}:{why}"] = reasons.get(f"{gate}:{why}", 0) + 1
        rows.append(rec)
    print("decisions:", ", ".join(f"{k}={v}" for k, v in
                                  sorted(reasons.items(), key=lambda kv: -kv[1])[:8]))
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/btc5m")
    args = ap.parse_args()
    df = load(args.root)
    if df.empty:
        print("no windows")
        return
    days = df["date"].nunique()
    print(f"windows: {len(df)} over {days} days  (features as of tau=10s)\n")
    for gate in (True, False):
        sub = df[df[f"win_{gate}"].notna()]
        fire = len(sub) / days
        print(f"=== OBI gate {'ON (CVD substitute)' if gate else 'OFF'} ===")
        if sub.empty:
            print("  no signals\n")
            continue
        print(f"  fires {len(sub)} signals = {fire:.1f}/day "
              f"({len(sub) / len(df):.1%} of windows)")
        for tag, c in (("all signals", sub),
                       ("  |delta| < 0.02%", sub[sub["absd"] < 0.02]),
                       ("  |delta| 0.02-0.05%", sub[(sub["absd"] >= 0.02) & (sub["absd"] < 0.05)]),
                       ("  |delta| >= 0.05%", sub[sub["absd"] >= 0.05])):
            if len(c) < 40:
                continue
            print(line(tag, battery(c[f"win_{gate}"].to_numpy(float),
                                    c["date"].to_numpy())))
        print()


if __name__ == "__main__":
    main()
