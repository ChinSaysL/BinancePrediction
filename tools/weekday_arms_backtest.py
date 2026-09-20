"""Backtest script for Model Arm, Cross Arm, and Combined Arms across Weekdays vs Weekends.

Reads `data/btcpredict.db` (rounds and quotes), applies `is_weekend_utc(ts_ms)` to
partition by FX week regime (Friday 22:00 UTC -> Sunday 21:00 UTC), and evaluates:
1. Model Arm performance (weekday vs weekend)
2. Cross Arm performance (weekday vs weekend)
3. Combined Arms performance on weekdays

Usage:
    python3 -m tools.weekday_arms_backtest
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btcpredict.config import settings as S
from btcpredict.edge.signal import evaluate, fee_rate_for_price
from btcpredict.main import crossed_side, is_weekend_utc
from btcpredict.pricing.digital import fair_value


DB_PATH = "data/btcpredict.db"


def load_data(db_path: str = DB_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found at {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rounds = pd.read_sql_query("""
        SELECT slug, start_ms, end_ms, strike, winner, feed_provider
        FROM rounds
        WHERE strike IS NOT NULL AND winner IS NOT NULL AND winner IN ('Up', 'Down')
    """, con)
    quotes = pd.read_sql_query("""
        SELECT slug, ts_ms, tau_s, spot, up_ask, down_ask
        FROM quotes
        WHERE spot IS NOT NULL AND ts_ms IS NOT NULL
        ORDER BY ts_ms
    """, con)
    con.close()

    rounds = rounds[rounds.feed_provider.fillna("").str.contains("twap", case=False)]
    quotes = quotes[quotes.slug.isin(set(rounds.slug))]
    return rounds.sort_values("start_ms"), quotes.sort_values("ts_ms")


def run_model_arm(rounds: pd.DataFrame, quotes: pd.DataFrame,
                  *, edge_haircut: float = 0.50, min_edge: float = 0.04,
                  min_quote: float = 0.45, max_quote: float = 0.80,
                  min_tau: float = 60.0, max_tau: float = 240.0) -> pd.DataFrame:
    """Replay Model Arm on quotes."""
    idx = rounds.set_index("slug")
    fills = []
    
    # Group quotes by round slug
    for slug, g in quotes.groupby("slug", sort=False):
        if slug not in idx.index:
            continue
        rr = idx.loc[slug]
        if isinstance(rr, pd.DataFrame):
            rr = rr.iloc[0]
        strike = float(rr.strike)
        if strike <= 0:
            continue
        
        # Sort by tau descending
        g = g.sort_values("tau_s", ascending=False)
        
        # Calculate 1m vol rolling estimate from spot series
        spots = g.spot.to_numpy()
        if len(spots) < 10:
            continue
        rets = np.diff(spots) / spots[:-1]
        vol_1m = np.std(rets) * math.sqrt(60) if len(rets) > 0 else 0.0004
        if not np.isfinite(vol_1m) or vol_1m <= 0:
            vol_1m = 0.0004
            
        for row in g.itertuples(index=False):
            tau = float(row.tau_s)
            if not (min_tau <= tau <= max_tau):
                continue
            spot = float(row.spot)
            up_ask = float(row.up_ask)
            down_ask = float(row.down_ask)
            if not (np.isfinite(up_ask) and np.isfinite(down_ask)):
                continue
            up_mid = up_ask - S.assumed_half_spread
            dn_mid = down_ask - S.assumed_half_spread
            if up_mid <= 0 or dn_mid <= 0:
                continue
                
            fv = fair_value(spot, strike, tau, vol_1m)
            if not (0.0 < fv < 1.0):
                continue
                
            sig = evaluate(fv, up_mid, dn_mid,
                           half_spread=S.assumed_half_spread,
                           safety_margin=S.safety_margin,
                           min_edge=min_edge,
                           min_quote=min_quote,
                           max_quote=max_quote,
                           edge_haircut=edge_haircut)
            if not sig.act or sig.side is None:
                continue
                
            paid = up_ask if sig.side == "Up" else down_ask
            if not (min_quote <= paid <= max_quote):
                continue
                
            fee = fee_rate_for_price(paid)
            won = int(rr.winner == sig.side)
            ret = (1.0 if won else 0.0) / paid - 1.0 - fee
            
            ts_ms = int(row.ts_ms)
            is_wknd = is_weekend_utc(ts_ms)
            day = int(ts_ms // 86400000)
            
            fills.append({
                "arm": "model",
                "slug": slug,
                "ts_ms": ts_ms,
                "day": day,
                "is_weekend": is_wknd,
                "side": sig.side,
                "paid": paid,
                "fee": fee,
                "won": won,
                "ret": ret,
                "pnl": (1.0 if won else 0.0) - paid - (fee * paid)
            })
            break  # 1 entry per round for model arm
            
    df = pd.DataFrame(fills)
    return df


def run_cross_arm(rounds: pd.DataFrame, quotes: pd.DataFrame,
                  *, min_price: float = 0.45, max_price: float = 0.65,
                  min_tau: float = 30.0, max_tau: float = 240.0,
                  min_margin_bps: float = 0.5, look_s: float = 1.0) -> pd.DataFrame:
    """Replay Cross Arm on quotes."""
    idx = rounds.set_index("slug")
    fills = []
    
    for slug, g in quotes.groupby("slug", sort=False):
        if slug not in idx.index:
            continue
        rr = idx.loc[slug]
        if isinstance(rr, pd.DataFrame):
            rr = rr.iloc[0]
        strike = float(rr.strike)
        if strike <= 0:
            continue
            
        g = g.sort_values("ts_ms")
        ticks = [(int(t), float(p)) for t, p in zip(g.ts_ms, g.spot) if p and p > 0]
        if len(ticks) < 10:
            continue
            
        ref_level = strike
        
        for i in range(5, len(g)):
            row = g.iloc[i]
            tau = float(row.tau_s)
            if not (min_tau <= tau <= max_tau):
                continue
                
            sub_ticks = ticks[:i+1]
            side = crossed_side(sub_ticks, ref_level, look_s)
            if side is None:
                continue
                
            spot_now = float(row.spot)
            margin_bps = 1e4 * abs(spot_now - ref_level) / ref_level
            if margin_bps < min_margin_bps:
                continue
                
            up_ask = float(row.up_ask)
            down_ask = float(row.down_ask)
            paid = up_ask if side == "Up" else down_ask
            if not (min_price <= paid <= max_price):
                continue
                
            fee = fee_rate_for_price(paid)
            won = int(rr.winner == side)
            ret = (1.0 if won else 0.0) / paid - 1.0 - fee
            
            ts_ms = int(row.ts_ms)
            is_wknd = is_weekend_utc(ts_ms)
            day = int(ts_ms // 86400000)
            
            fills.append({
                "arm": "cross",
                "slug": slug,
                "ts_ms": ts_ms,
                "day": day,
                "is_weekend": is_wknd,
                "side": side,
                "paid": paid,
                "fee": fee,
                "won": won,
                "ret": ret,
                "pnl": (1.0 if won else 0.0) - paid - (fee * paid)
            })
            break  # 1 entry per round for cross arm
            
    return pd.DataFrame(fills)


def summarize(df: pd.DataFrame, label: str) -> None:
    if df.empty:
        print(f"=== {label} ===\n  No fills.")
        return
    n = len(df)
    wr = df.won.mean()
    paid_mean = df.paid.mean()
    ev_per_share = df.ret.mean()
    total_pnl = df.pnl.sum()
    days_count = df.day.nunique()
    per_day = n / max(days_count, 1)
    pnl_per_day = total_pnl / max(days_count, 1)
    
    by_day = df.groupby("day")["pnl"].sum()
    day_t = (by_day.mean() / (by_day.std(ddof=1) / math.sqrt(len(by_day)))) if len(by_day) > 1 and by_day.std(ddof=1) > 0 else 0.0
    
    print(f"=== {label} ===")
    print(f"  Fills: {n:<5} | Days: {days_count:<3} | Fills/day: {per_day:<5.1f}")
    print(f"  Win Rate: {wr:6.1%} | Mean Paid: {paid_mean:6.3f} | EV/$1: {ev_per_share:+6.4f}")
    print(f"  Total PnL: ${total_pnl:+7.2f} | PnL/day: ${pnl_per_day:+6.2f} | Day-t: {day_t:+5.2f}")
    print()


def main() -> None:
    print("Loading database for arms backtest...")
    rounds, quotes = load_data(DB_PATH)
    print(f"Loaded {len(rounds):,} rounds, {len(quotes):,} quotes.")
    
    # 1. Model Arm
    print("\n--- RUNNING MODEL ARM BACKTEST ---")
    df_model = run_model_arm(rounds, quotes, edge_haircut=0.50, min_edge=0.04)
    model_wd = df_model[~df_model.is_weekend]
    model_wk = df_model[df_model.is_weekend]
    summarize(model_wd, "Model Arm (WEEKDAYS)")
    summarize(model_wk, "Model Arm (WEEKENDS)")

    # 2. Cross Arm
    print("\n--- RUNNING CROSS ARM BACKTEST ---")
    df_cross = run_cross_arm(rounds, quotes, min_price=0.45, max_price=0.65, min_tau=30.0, max_tau=240.0, min_margin_bps=0.5)
    cross_wd = df_cross[~df_cross.is_weekend]
    cross_wk = df_cross[df_cross.is_weekend]
    summarize(cross_wd, "Cross Arm (WEEKDAYS)")
    summarize(cross_wk, "Cross Arm (WEEKENDS)")

    # 3. Combined Arms (Weekdays Only)
    print("\n--- RUNNING COMBINED ARMS (WEEKDAYS ONLY) ---")
    combined_wd = pd.concat([model_wd, cross_wd], ignore_index=True)
    summarize(combined_wd, "Combined Model + Cross Arms (WEEKDAYS)")


if __name__ == "__main__":
    main()
