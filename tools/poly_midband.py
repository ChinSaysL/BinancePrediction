#!/usr/bin/env python3
"""Out-of-venue test of the mid-claim dead band on Polymarket BTC 5m markets.

Replicates the live gate of 2026-09-11 as faithfully as the data allows:

  models  calibrated_down (main; prices Down, anchors on the Down quote)
          calibrated (up_model; prices Up, anchors on the Up quote)
  gate    min_edge 0.01, safety_margin 0.01, quote band [0.50, 0.90],
          tau in [60, 210]s, up_regime_gate 0.005 (Up skipped when 1h ret
          exceeds it), edge_haircut 1.0, half_spread 0 (prints are fills)
  fees    Polymarket taker: 0.07 * (1-q) per $1 staked
  entries taker-BUY prints on a 10s grid; outcome from resolutions; token
          sides labelled by final-minute prices (confidence-gated)

Then the same walk-forward our pool was judged with: fit the middle-third
claim band on one day, apply to the next day, compare with keeping everything.
"""
from __future__ import annotations

import math
import statistics as st

import duckdb
import numpy as np
import pandas as pd

WEEK = "/root/pmweek"
TRADES_GLOB = f"{WEEK}/trades_2026-03-0*.parquet"
RES = f"{WEEK}/resolutions_all.parquet"
K1M = f"{WEEK}/btc_1m.parquet"
K1S = f"{WEEK}/btc_1s.parquet"

# --- live gate, from .env + config defaults (2026-09-11) ---
MIN_EDGE = 0.01
SAFETY = 0.01
MIN_Q, MAX_Q = 0.50, 0.90
MIN_TAU, MAX_TAU = 60.0, 210.0
UP_REGIME_GATE = 0.005
DOWN_P = dict(b_market=1.242673, b_disc=0.056951, b_imb=-0.376878)
UP_P = dict(b_market=1.236789, b_disc=0.103309, b_imb=0.396481)
PM_FEE = 0.07          # taker fee coefficient; fee per $1 staked = 0.07*(1-q)

_EPS = 1e-6


# ---------------- model pieces, mirrored from btcpredict ----------------
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_value(S: float, K: float, tau_s: float, sigma_1m: float) -> float:
    if S <= 0 or K <= 0:
        raise ValueError("prices must be positive")
    if tau_s <= 0:
        return 1.0 if S > K else 0.0
    if sigma_1m <= 0:
        raise ValueError("sigma must be positive")
    z = math.log(S / K) / (sigma_1m * math.sqrt(tau_s / 60.0))
    return min(1.0 - _EPS, max(_EPS, norm_cdf(z)))


def logit(p: float) -> float:
    p = max(_EPS, min(1.0 - _EPS, p))
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def fee_pm(q: float) -> float:
    """Taker fee per $1 staked at fill price q (fee = C*0.07*q*(1-q) on cost
    C*q)."""
    return PM_FEE * (1.0 - q)


def ret_per_dollar(won: bool, q: float) -> float:
    """Win: pay q(1+fee) per share, receive 1 -> 1/(q(1+fee)) - 1. Lose: -1."""
    return (1.0 / (q * (1.0 + fee_pm(q))) - 1.0) if won else -1.0


# ---------------- kline helpers ----------------
def build_sigma_series(dfm: pd.DataFrame):
    """Replicate VolEstimator: EWMA(0.97)+simple blend, 240 1m returns,
    floor 0.00015, seasonality from the seed window (learned once, like the
    live process which seeds at startup)."""
    lam, floor, lookback = 0.97, 0.00015, 240
    rets = np.log(dfm["close"].to_numpy() / dfm["open"].to_numpy())
    opents = dfm["open_time"].to_numpy()
    hours = pd.to_datetime(opents, unit="ms", utc=True).hour.to_numpy()
    n = len(rets)
    sig = np.full(n, np.nan)
    if n <= lookback + 1:
        raise SystemExit("not enough 1m bars")
    # seed
    seed = list(rets[:lookback])
    seasonal = True
    base = st.pstdev(seed)
    buckets: dict[int, list[float]] = {}
    for r, h in zip(seed, hours[:lookback]):
        buckets.setdefault(int(h), []).append(r)
    season = {}
    for h, rs in buckets.items():
        if len(rs) < 10:
            continue
        factor = (st.pstdev(rs) or base) / base
        w = min(1.0, len(rs) / 60.0)
        season[h] = max(0.5, min(2.0, 1.0 + w * (factor - 1.0)))
    ewma_var = None
    for r in seed:
        ewma_var = r * r if ewma_var is None else lam * ewma_var + (1 - lam) * r * r
    win = list(seed)
    for i in range(lookback, n):
        r = rets[i]
        ewma_var = lam * ewma_var + (1 - lam) * r * r
        win.append(r)
        if len(win) > lookback:
            win.pop(0)
        simple = st.pstdev(win) if len(win) > 1 else abs(win[0])
        s = 0.7 * math.sqrt(ewma_var) + 0.3 * simple
        if seasonal and season:
            s *= season.get(int(hours[i]), 1.0)
        sig[i] = max(floor, s)
    return opents, sig


def build_imb_series(dfm: pd.DataFrame):
    """taker_imbalance over the last 5 completed 1m bars, clamped +-0.5."""
    vol = dfm["volume"].to_numpy()
    tb = dfm["taker_buy_base"].to_numpy()
    n = len(vol)
    out = np.zeros(n)
    for i in range(5, n):
        v = vol[i - 5:i].sum()
        out[i] = max(-0.5, min(0.5, (tb[i - 5:i].sum() / v - 0.5))) if v > 0 else 0.0
    return out


def build_1h_ret(dfm: pd.DataFrame):
    close = dfm["close"].to_numpy()
    n = len(close)
    out = np.full(n, np.nan)
    for i in range(60, n):
        out[i] = math.log(close[i] / close[i - 60])
    return out


def last_completed_ms(open_ms: np.ndarray, ts_ms: float, bar_ms: int) -> int:
    """Index of the last bar that had fully closed at ts_ms, or -1."""
    idx = np.searchsorted(open_ms, ts_ms - bar_ms, side="right") - 1
    return idx


# ---------------- data loading ----------------
def load_candidates() -> pd.DataFrame:
    con = duckdb.connect()
    con.sql(f"""
    CREATE TEMP TABLE mine AS
    SELECT
      t.market_id, t.token_id, t.side AS taker_side, t.price, t.size,
      epoch_ms(t.timestamp) AS ts_ms,
      epoch_ms(r.start_time) AS start_ms,
      epoch_ms(r.end_time) AS end_ms,
      r.outcome,
      (epoch_ms(t.timestamp) - epoch_ms(r.start_time)) / 1000.0 AS off_s
    FROM read_parquet('{TRADES_GLOB}') t
    JOIN read_parquet('{RES}') r USING (market_id)
    WHERE t.market_id LIKE 'btc-updown-5m-%'
      AND t.price > 0 AND t.price < 1 AND t.size > 0
      AND (epoch_ms(t.timestamp) - epoch_ms(r.start_time)) BETWEEN -30000
          AND 330000
    """)

    # token sides: last-minute print prices, per market exactly two tokens,
    # winner = higher final price, confident only if they straddle 0.9/0.1
    con.sql(f"""
    CREATE TEMP TABLE labels AS
    WITH fin AS (
      SELECT market_id, token_id,
             arg_max(price, ts_ms) AS last_px,
             count(*) AS n_fin
      FROM mine
      WHERE off_s BETWEEN 240 AND 305
      GROUP BY 1, 2
    ),
    two AS (
      SELECT market_id, count(*) AS ntok,
             max(last_px) AS mx, min(last_px) AS mn
      FROM fin GROUP BY 1 HAVING count(*) = 2
    )
    SELECT f.market_id, f.token_id,
           CASE WHEN f.last_px = t.mx THEN r.outcome
                ELSE CASE WHEN r.outcome = 'Up' THEN 'Down' ELSE 'Up' END
           END AS token_side
    FROM fin f
    JOIN two t USING (market_id)
    JOIN read_parquet('{RES}') r USING (market_id)
    WHERE t.mx >= 0.90 AND t.mn <= 0.10
    """)

    # candidate entries: taker BUYs, tau in [60,210] -> off in [90,240],
    # one print per (market, token, 10s cell)
    df = con.sql("""
    WITH buys AS (
      SELECT m.*, l.token_side,
             floor(off_s / 10.0) AS cell
      FROM mine m
      JOIN labels l USING (market_id, token_id)
      WHERE m.taker_side = 'BUY'
        AND m.off_s BETWEEN 90 AND 240
    ),
    firsts AS (
      SELECT *, row_number() OVER (
        PARTITION BY market_id, token_id, cell ORDER BY ts_ms) AS rn
      FROM buys
    )
    SELECT market_id, token_id, token_side, price, size, ts_ms,
           start_ms, end_ms, outcome,
           (end_ms - ts_ms) / 1000.0 AS tau_s, cell
    FROM firsts WHERE rn = 1
    """).df()

    stats = con.sql("""
    SELECT (SELECT count(*) FROM mine) AS prints,
           (SELECT count(DISTINCT market_id) FROM mine) AS markets,
           (SELECT count(DISTINCT market_id) FROM labels) AS markets_labelled
    """).df().iloc[0]
    print(f"prints kept: {stats['prints']:,}  markets: {stats['markets']:,}"
          f"  labelled: {stats['markets_labelled']:,}")
    return df


# ---------------- gate simulation ----------------
def run_gate(cand: pd.DataFrame):
    dfm = pd.read_parquet(K1M)
    dfs = pd.read_parquet(K1S)
    m_open, sig = build_sigma_series(dfm)
    imb_series = build_imb_series(dfm)
    ret1h = build_1h_ret(dfm)
    s_open = dfs["open_time"].to_numpy()
    s_close = dfs["close"].to_numpy()

    ts = cand["ts_ms"].to_numpy().astype("float64")
    start = cand["start_ms"].to_numpy().astype("float64")
    tau = cand["tau_s"].to_numpy()
    q = cand["price"].to_numpy()
    side = cand["token_side"].to_numpy()
    outcome = cand["outcome"].to_numpy()

    S = np.full(len(cand), np.nan)
    K = np.full(len(cand), np.nan)
    sg = np.full(len(cand), np.nan)
    ib = np.full(len(cand), np.nan)
    r1 = np.full(len(cand), np.nan)
    for i in range(len(cand)):
        si = last_completed_ms(s_open, ts[i], 1000)
        ki = last_completed_ms(s_open, start[i], 1000)
        mi = last_completed_ms(m_open, ts[i], 60_000)
        if si >= 0:
            S[i] = s_close[si]
        if ki >= 0:
            K[i] = s_close[ki]
        if mi >= 0 and not math.isnan(sig[mi]):
            sg[i] = sig[mi]
            ib[i] = imb_series[mi]
            r1[i] = ret1h[mi]

    ok_ctx = (~np.isnan(S)) & (~np.isnan(K)) & (~np.isnan(sg))
    p_dig = np.full(len(cand), np.nan)
    for i in np.where(ok_ctx)[0]:
        p_dig[i] = fair_value(S[i], K[i], tau[i], sg[i])

    p_model = np.full(len(cand), np.nan)
    for i in range(len(cand)):
        if math.isnan(p_dig[i]):
            continue
        if side[i] == "Down":
            x = (DOWN_P["b_market"] * logit(q[i])
                 + DOWN_P["b_disc"] * (logit(1.0 - p_dig[i]) - logit(q[i]))
                 + DOWN_P["b_imb"] * ib[i])
            p_model[i] = sigmoid(x)
        else:
            x = (UP_P["b_market"] * logit(q[i])
                 + UP_P["b_disc"] * (logit(p_dig[i]) - logit(q[i]))
                 + UP_P["b_imb"] * ib[i])
            p_model[i] = sigmoid(x)

    edge = p_model - q
    fee = np.array([fee_pm(v) for v in q])
    ev = p_model / q - 1.0 - fee
    hurdle = q * fee + SAFETY
    up_ok = (side == "Up") & (r1 <= UP_REGIME_GATE)
    act = ((edge > hurdle) & (edge >= MIN_EDGE) & (ev > 0.0)
           & (q >= MIN_Q) & (q <= MAX_Q) & ~np.isnan(p_model)
           & (side == "Down") | (up_ok & (edge > hurdle) & (edge >= MIN_EDGE)
                                 & (ev > 0.0) & (q >= MIN_Q) & (q <= MAX_Q)
                                 & ~np.isnan(p_model)))

    won = (side == outcome)
    ret_net = np.array([ret_per_dollar(w, v) for w, v in zip(won, q)])
    ret_gross = np.where(won, 1.0 / q - 1.0, -1.0)

    out = pd.DataFrame({
        "market_id": cand["market_id"], "token_id": cand["token_id"],
        "side": side, "q": q, "ts_ms": ts, "tau_s": tau, "cell": cand["cell"],
        "S": S, "K": K, "sigma": sg, "imb": ib, "ret1h": r1,
        "p_dig": p_dig, "p_model": p_model, "edge": edge, "fee": fee,
        "ev": ev, "hurdle": hurdle, "act": act, "won": won,
        "ret_net": ret_net, "ret_gross": ret_gross,
        "day": pd.to_datetime(ts, unit="ms", utc=True).date,
    })
    return out


# ---------------- analysis ----------------
def buckets(t: pd.DataFrame, col="edge", ret="ret_net") -> pd.DataFrame:
    rows = []
    for day, g in t.groupby("day"):
        b = g.sort_values(col)
        k = len(b) // 3
        if k < 5:
            continue
        for tag, part in (("small", b.iloc[:k]), ("mid", b.iloc[k:2 * k]),
                          ("big", b.iloc[2 * k:])):
            rows.append(dict(day=str(day), bucket=tag, n=len(part),
                             mean_edge=part[col].mean(),
                             ev=part[ret].mean()))
    return pd.DataFrame(rows)


def walk_forward(t: pd.DataFrame, ret="ret_net") -> None:
    days = sorted(t["day"].unique())
    rules = {"keep everything": lambda x, lo, hi: np.ones(len(x), bool),
             "drop mid third": lambda x, lo, hi: ~((x >= lo) & (x < hi)),
             "edge >= 0.04": lambda x, lo, hi: x >= 0.04,
             "edge >= 0.05": lambda x, lo, hi: x >= 0.05,
             "drop fixed [0.030,0.045)": lambda x, lo, hi: ~(
                 (x >= 0.030) & (x < 0.045))}
    totals = {k: 0.0 for k in rules}
    print(f"\nwalk-forward ({len(days)-1} folds: fit on one day, apply to next)"
          f"  [{ret}]")
    print(f"  {'fit day':<12}{'rule':<26}{'kept':>6}{'EV/$1 out':>11}"
          f"{'net out $':>11}")
    for i in range(len(days) - 1):
        fit = t[t["day"] == days[i]]
        out = t[t["day"] == days[i + 1]]
        e = np.sort(fit["edge"].to_numpy())
        k = len(e) // 3
        lo, hi = e[k], e[2 * k]
        for name, rule in rules.items():
            m = rule(out["edge"].to_numpy(), lo, hi)
            o = out[m]
            if len(o) == 0:
                continue
            totals[name] += o[ret].sum()
            print(f"  {str(days[i]):<12}{name:<26}{len(o):>6}"
                  f"{o[ret].mean():>+11.4f}{o[ret].sum():>+11.2f}")
    print(f"\n  {'rule':<26}{'total net, out-of-sample':>30}")
    for name, tot in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<26}{tot:>+30.2f}")


def main() -> None:
    cand = load_candidates()
    print(f"candidates (10s grid, taker BUY, tau 60-210s): {len(cand):,}"
          f"  markets: {cand['market_id'].nunique():,}")
    res = run_gate(cand)
    res.to_parquet(f"{WEEK}/pm_band_rows.parquet", index=False)

    acc = res[res["act"]].copy()
    print(f"\naccepted trades: {len(acc)}  markets: {acc['market_id'].nunique()}"
          f"  days: {acc['day'].nunique()}")
    if acc.empty:
        print("nothing accepted - gate too strict for this venue")
        return
    wr = acc["won"].mean()
    be = float(np.mean(acc["q"] * (1 + acc["fee"])))
    print(f"WR {wr:.3f} vs breakeven {be:.3f}   "
          f"EV/$1 net {acc['ret_net'].mean():+.4f}  "
          f"t={acc['ret_net'].mean()/acc['ret_net'].std(ddof=1)*math.sqrt(len(acc)):+.2f}")
    print(f"EV/$1 gross (no fee)   {acc['ret_gross'].mean():+.4f}   "
          f"mean edge {acc['edge'].mean():+.4f}  "
          f"[{acc['edge'].min():+.3f}, {acc['edge'].max():+.3f}]")
    print("\nper day:")
    for day, g in acc.groupby("day"):
        print(f"  {day}  n={len(g):>4}  EV/$1 {g['ret_net'].mean():+.4f}"
              f"  WR {g['won'].mean():.3f}  mean edge {g['edge'].mean():+.4f}")

    print("\nbuckets by claimed edge (terciles per day), net of fees:")
    bt = buckets(acc)
    if not bt.empty:
        piv = bt.pivot(index="day", columns="bucket", values=["mean_edge", "ev", "n"])
        with pd.option_context("display.width", 200):
            print(piv.to_string())

    walk_forward(acc, "ret_net")
    walk_forward(acc, "ret_gross")


if __name__ == "__main__":
    main()
