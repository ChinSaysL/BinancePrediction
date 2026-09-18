"""Reader for the Polymarket up/down L2 tape (kartik research sample).

WHAT THIS DATASET IS. `data/hf2/kartik` is a one-day sample (2026-07-17) from a
Polymarket order-book warehouse: 2,303 episodes across btc/eth/bnb/sol/xrp/doge
at 5m and 15m, each an L2 capture with the top 10 levels on BOTH sides plus
yes/no last-traded prices, and a matching trade tape. btc-5m alone is 288
episodes = a full 24 h day, which is the first real PM tape we have.

WHY IT MATTERS. Every earlier PM attempt in this repo died on the same rock:
there was no honest book to price an entry against. The 13-day `orderbooks/*`
parquet is a synthetic placeholder (bid 0.10 / ask 0.90 ladders), and the 9,000
line `polymarket_crypto_5min_l2.jsonl` is 15 rounds of one quarter-hour. This is
the first file where the sizes look organic and the day is complete.

TRAPS THIS MODULE EXISTS TO AVOID -- ALL THREE WERE HIT FOR REAL ON 2026-09-16
- `secs` is the COUNTDOWN: seconds REMAINING until the market closes/resolves.
  It runs 1640 -> 0, so `tau_s = secs`, and rows with `secs > interval_s` are
  PRE-ROLL (the capture starts before the market opens, book all NaN). The first
  version of this module assumed `tau = interval - secs`, i.e. the exact
  inversion its own comment warned about. 99.9% of the rows live in secs 0-300
  for a 5m round; anything above that is not part of the round.
- `ts` is epoch MILLISECONDS. Reading it as seconds lands you in the year 58513.
- The winner is not a column, and it is NOT in `yes_bid`/`yes_ask` at the end
  (the terminal book is one-sided: `yes_ask=0.01`, `no_bid=0.99`, and `yes_bid`
  is NaN). Reading the two columns that look natural resolves 2 of 288 episodes;
  reading `yes_ltp`/`no_ltp` and the one-sided extremes resolves 275 of 288.
- A 5-minute episode holds ~100k rows and the capture is event-driven (median
  dt 1 ms, p90 8 ms), so a mean over ROWS is not a mean over TIME. Resample
  before averaging anything.
- yes/no are two books on the same event. `yes_ask + no_ask` should exceed 1 by
  the spread; a reader that finds the sum BELOW 1 has a stale half, not free
  money.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
KARTIK = ROOT / "data" / "hf2" / "kartik" / "top10_parquet"
INDEX = ROOT / "data" / "hf2" / "kartik" / "metadata" / "episode_index.parquet"

#: Interval -> length in seconds. `secs` counts up from the episode start.
INTERVAL_S = {"5m": 300, "15m": 900}

_EPS = 1e-9


def episodes(asset: str | None = None, interval: str = "5m") -> list[pathlib.Path]:
    """Every episode parquet for an asset, sorted."""
    pat = f"*/{interval}/*.parquet" if asset is None else f"*/{asset}/{interval}/*.parquet"
    return sorted(KARTIK.glob(pat))


def load(path: pathlib.Path, *, drop_preroll: bool = True) -> pd.DataFrame:
    """One episode's snapshots, time-ordered, with `tau_s` added.

    `tau_s` = seconds REMAINING (`secs` itself -- it is already a countdown).
    `drop_preroll` removes the rows captured before the round opens, which is
    where the book is NaN and a naive mean over the file goes wrong.
    """
    df = pd.read_parquet(path)
    if df.empty:
        return df
    iv = df["interval"].iloc[0] if "interval" in df.columns else "5m"
    span = INTERVAL_S.get(iv, 300)
    df = df.sort_values("ts", kind="stable").reset_index(drop=True)
    df["tau_s"] = df["secs"].astype(float)
    if drop_preroll:
        df = df[df["tau_s"] <= span].reset_index(drop=True)
    return df


def episode_winner(df: pd.DataFrame) -> str | None:
    """YES / NO / None, read from the LAST snapshots that are decisive.

    The tape knows the outcome only as the round closes, so the tail is the
    only place allowed to answer -- labelling from a mid-round price is the
    look-ahead that flatters every backtest.

    Note the terminal book is ONE-SIDED (`yes_ask=0.01`, `no_bid=0.99`, with
    `yes_bid` NaN), so both the traded price and the ask are checked.
    """
    if df is None or df.empty:
        return None
    tail = df.tail(200)
    for col_hi, side in (("yes_ltp", "YES"), ("yes_ask", "YES")):
        s = tail[col_hi].dropna()
        if len(s) and (s >= 0.99).any():
            return side
        if len(s) and (s <= 0.01).any():
            return "NO"
    for col_lo, side in (("no_ltp", "NO"), ("no_bid", "NO")):
        s = tail[col_lo].dropna()
        if len(s) and (s >= 0.99).any():
            return side
        if len(s) and (s <= 0.01).any():
            return "YES"
    return None


def spread_at(df: pd.DataFrame, tau_s: float, tol_s: float = 2.0) -> tuple[float, float] | None:
    """(yes_ask, yes_bid) at the snapshot nearest `tau_s` seconds remaining."""
    if df is None or df.empty:
        return None
    sel = df[(df["tau_s"] - tau_s).abs() <= tol_s]
    if sel.empty:
        return None
    r = sel.iloc[0]
    if pd.isna(r["yes_ask"]) or pd.isna(r["yes_bid"]):
        return None
    return float(r["yes_ask"]), float(r["yes_bid"])


def depth_at(df: pd.DataFrame, tau_s: float, side: str = "yes",
             tol_s: float = 2.0) -> float | None:
    """Total size resting on the BID side at the snapshot nearest `tau_s`.

    Depth is what decides whether a small ticket can actually fill; at $13 the
    ticket is tiny, so this is normally a non-issue -- but it is the thing that
    kills a strategy whose edge only exists at the extremes.
    """
    if df is None or df.empty:
        return None
    sel = df[(df["tau_s"] - tau_s).abs() <= tol_s]
    if sel.empty:
        return None
    r = sel.iloc[0]
    tot = 0.0
    for i in range(1, 11):
        v = r.get(f"top_{i}_{side}_bid_size")
        if pd.notna(v):
            tot += float(v)
    return tot


@dataclass
class EpisodeSummary:
    path: str
    condition_id: str
    rows: int
    tau_max: float
    tau_min: float
    winner: str | None
    ask_at_60: float | None
    bid_at_60: float | None
    last_yes_ask: float | None


def summarise(path: pathlib.Path) -> EpisodeSummary:
    df = load(path)
    cid = df["condition_id"].iloc[0] if len(df) else path.stem
    sp = spread_at(df, 60.0)
    return EpisodeSummary(
        path=path.name,
        condition_id=cid,
        rows=len(df),
        tau_max=float(df["tau_s"].max()) if len(df) else float("nan"),
        tau_min=float(df["tau_s"].min()) if len(df) else float("nan"),
        winner=episode_winner(df),
        ask_at_60=sp[0] if sp else None,
        bid_at_60=sp[1] if sp else None,
        last_yes_ask=float(df["yes_ask"].iloc[-1]) if len(df) and pd.notna(df["yes_ask"].iloc[-1]) else None,
    )
