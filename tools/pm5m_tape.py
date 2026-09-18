"""Loader for the 53-day btc5m round dataset (data/btc5m).

LAYOUT, one directory per round:
    data/btc5m/data/<date>/<round_ts>/
        metadata.json        Gamma market: slug, clobTokenIds, outcomePrices
                             (the RESOLUTION), tick, min size, feeSchedule
        ticks_dense.parquet  300 x 1-second Binance spot bars for the round
        trades_raw.json      real on-chain Polymarket trades

WHY THIS EXISTS: the kartik tape is ONE day (2026-07-17). This covers
2026-04-29 .. 2026-06-20, 288 rounds a day, 15,240 rounds -- enough for the
day-clustered test that no single day can support.

WHAT IT IS NOT, STATED UP FRONT:
  * There is NO ORDER BOOK. Only trades. So the entry price is the LAST TRADE
    price at or before the fire instant, which is OPTIMISTIC for a taker (we
    would pay the ask, at or above the last trade). The `spread` in the
    metadata is typically 0.01, so apply a spread penalty and test its
    sensitivity rather than pretending it away.
  * `fee` is 0.0 on every trade record: these are the MAKER legs, and PM
    charges takers only. The taker fee therefore CANNOT be validated here and
    stays modelled as 0.07*p*(1-p).

PRICE CONVENTION (validated on 5,811 trades of one round, 100% land in (0,1)):
    price = min(maker_amount, taker_amount) / max(maker_amount, taker_amount)
    shares = max(maker_amount, taker_amount)
This is side-independent and needs no per-side branch: the two legs are
{USDC, shares}, so the ratio is always the price and the larger leg is always
the share count. Up+Down prices then sum to ~1.00, which is the check that it
is right.
"""
from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd

ROOT = pathlib.Path("data/btc5m/data")
ROUND_S = 300


# ---------------------------------------------------------------- discovery
def dates() -> list[str]:
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir())


def rounds(day: str) -> list[pathlib.Path]:
    d = ROOT / day
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_dir())


# ---------------------------------------------------------------------- meta
def meta(rd: pathlib.Path) -> dict | None:
    f = rd / "metadata.json"
    if not f.exists():
        return None
    m = json.loads(f.read_text())

    def jlist(k):
        v = m.get(k)
        if isinstance(v, str):
            try:
                return json.loads(v)
            except ValueError:
                return []
        return v or []

    toks = jlist("clobTokenIds")
    outs = jlist("outcomes")
    prices = [float(x) for x in jlist("outcomePrices")] or []
    if len(toks) < 2 or len(outs) < 2 or len(prices) < 2:
        return None
    # outcomes is ["Up","Down"]; outcomePrices settles to 0/1 -> the winner is
    # the outcome whose price is 1.
    win_i = int(np.argmax(prices))
    fs = m.get("feeSchedule") or {}
    return {
        "slug": m.get("slug") or rd.name,
        "start_s": int(rd.name),
        "up_token": str(toks[0]), "down_token": str(toks[1]),
        "winner": outs[win_i],
        "winner_price": prices[win_i],
        "tick": float(m.get("orderPriceMinTickSize") or 0.01),
        "min_size": float(m.get("orderMinSize") or 5),
        "fee_rate": float(fs.get("rate") or 0.0),
        "fee_taker_only": bool(fs.get("takerOnly", False)),
        "spread": float(m.get("spread") or 0.0),
    }


# ---------------------------------------------------------------------- spot
def spot(rd: pathlib.Path) -> pd.DataFrame | None:
    f = rd / "ticks_dense.parquet"
    if not f.exists():
        return None
    d = pd.read_parquet(f)
    if d.empty or "timestamp" not in d.columns:
        return None
    d = d.sort_values("timestamp").reset_index(drop=True)
    start = int(rd.name)
    d["sec"] = (d["timestamp"].astype("int64") - start).astype("int64")
    return d[d["sec"].between(0, ROUND_S - 1)]


# -------------------------------------------------------------------- trades
def trades(rd: pathlib.Path) -> pd.DataFrame | None:
    f = rd / "trades_raw.json"
    if not f.exists():
        return None
    rows = json.loads(f.read_text())
    if not rows:
        return None
    d = pd.DataFrame(rows)
    if d.empty or "maker_amount" not in d.columns:
        return None
    ma = d["maker_amount"].astype(float)
    ta = d["taker_amount"].astype(float)
    lo = np.minimum(ma, ta)
    hi = np.maximum(ma, ta)
    d["price"] = np.where(hi > 0, lo / hi, np.nan)
    d["shares"] = hi
    d["ts"] = d["block_timestamp"].astype("int64")
    return d[["ts", "token_id", "side", "price", "shares", "fee"]].dropna(
        subset=["price"])


def price_series(tr: pd.DataFrame, token: str) -> pd.DataFrame:
    """Chronological last-trade price for one token, deduped to 1 s."""
    t = tr[tr["token_id"] == token].sort_values("ts")
    if t.empty:
        return t
    t = t.groupby("ts", as_index=False).agg(price=("price", "last"),
                                            n=("price", "size"))
    return t


def price_at(series: pd.DataFrame, ts: np.ndarray) -> np.ndarray:
    """Forward-fill the last trade price at or before each ts (NaN before)."""
    if series.empty:
        return np.full(len(ts), np.nan)
    return np.interp(ts, series["ts"].to_numpy(),
                     series["price"].to_numpy(),
                     left=np.nan, right=np.nan)


def fee_per_share(p: float, rate: float = 0.07) -> float:
    return rate * p * (1.0 - p)
