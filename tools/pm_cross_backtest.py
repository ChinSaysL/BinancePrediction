"""Score the CROSS arm on Polymarket tapes.

This is the arm the bot actually trades: it buys the side that the spot just
crossed INTO, inside a tau window, at a capped price.  Until now it had never
been tested on Polymarket data -- the only PM backtest was
`tools/pm_backtest.py`, which bought the favourite at the ask and is not one
of our arms.

Two entry models, both reported:

  taker  pay the ask at the fire instant.  Honest about the price, optimistic
         about the spread (there is no slippage model, and no size check
         against the book).
  limit  the LIVE rule from config.py: post `ask + max(0.01, min(
         cross_limit_buffer, cross_limit_buffer_inv * (1 - ask)))`, capped by
         cross_max_fill_px, and wait cross_limit_wait_s.

The limit model is the one that can lie, so it is held to the rule that killed
the last maker backtest: A RESTING ORDER ONLY FILLS IF THE BOOK TRADES THROUGH
ITS PRICE.  Evidence of that in a book-snapshot tape is `best_ask <= our
limit` -- an ask at or below a resting bid is a crossed book, which is
impossible unless a sell order arrived at that level and matched the bid side.
The best bid merely REACHING our limit is not evidence (that is someone
joining us), so it is not accepted.

Ground truth is the tape's own winner (`episode_winner`), never a model.

Usage:
    python -m tools.pm_cross_backtest --asset btc --interval 5m
    python -m tools.pm_cross_backtest --entry limit --sweep-tau
"""
from __future__ import annotations

import argparse
import math
import statistics as st
import zipfile
import io
import pathlib

import pandas as pd

from tools import pm_tape
from tools.pm_tape import episodes, load, episode_winner

# The trigger itself, imported so the backtest cannot drift from the bot.
from btcpredict.main import crossed_side

SPOT_DIR = pathlib.Path("data/bars_1s/daily")
PM_FEE_RATE = 0.07          # fee = 0.07 * p * (1 - p) per share, taker only


def tape_date(asset: str, interval: str) -> str:
    """The calendar date of the tape, from the dataset's own index."""
    idx = pd.read_parquet(pm_tape.INDEX)
    rows = idx[(idx["asset"] == asset) & (idx["interval"] == interval)]
    if rows.empty:
        raise SystemExit(f"no index rows for {asset}/{interval}")
    return str(rows["date"].iloc[0])


# --------------------------------------------------------------------------
# spot
# --------------------------------------------------------------------------
def load_spot(asset: str, day: str) -> pd.DataFrame:
    """1-second closes for `asset` on `day` (YYYY-MM-DD), indexed by epoch s."""
    z = SPOT_DIR / f"{asset.upper()}USDT-1s-{day}.zip"
    if not z.exists():
        raise SystemExit(f"no spot file: {z}")
    with zipfile.ZipFile(z) as f:
        b = pd.read_csv(io.BytesIO(f.read(f.namelist()[0])), header=None,
                        names=["ot", "o", "h", "l", "c", "v", "ct", "qv",
                               "n", "tb", "tq", "ig"])
    b["ts_s"] = (b["ot"] // 1_000_000).astype("int64")
    return b[["ts_s", "o", "c"]].set_index("ts_s").sort_index()


def round_window(d: pd.DataFrame, interval_s: int) -> tuple[int, int]:
    """(start_s, end_s) of the round, snapped to the interval boundary.

    TRAP (hit on 2026-07-17 btc/5m): the tape's last timestamp is
    1784332199999 -- 1784332199.999 s, i.e. the last millisecond BEFORE the
    boundary. Floormg it gives 1784332199, and `ts - ts % 300` then snaps it
    to 1784331900, a whole 300 s early: the crossing test would run on the
    PREVIOUS round's price series while the strike was this round's. Snap to
    the NEAREST boundary, not the floor.
    """
    last_ms = int(d["ts"].iloc[-1])
    end_s = int(round(last_ms / 1000.0 / interval_s)) * interval_s
    return end_s - interval_s, end_s


# --------------------------------------------------------------------------
# entry rules
# --------------------------------------------------------------------------
def fee_per_share(p: float) -> float:
    return PM_FEE_RATE * p * (1.0 - p)


def breakeven_win_rate(p: float) -> float:
    """Win rate at which EV is exactly zero:  p + fee."""
    return p + fee_per_share(p)


def live_limit(ask: float, buffer: float, inv: float, cap: float) -> float | None:
    """The live posted price, or None when the cap refuses the trade."""
    px = ask + max(0.01, min(buffer, inv * (1.0 - ask)))
    px = min(1.0 - 0.01, round(px, 2))
    return px if px <= cap else None


def book_size(d: pd.DataFrame, i: int, side: str) -> float:
    col = f"top_1_{'yes' if side == 'Up' else 'no'}_ask_size"
    try:
        v = float(d[col].iloc[i])
    except Exception:
        return 0.0
    return v if v == v else 0.0


def book_ask(d: pd.DataFrame, i: int, side: str) -> float:
    col = f"{'yes' if side == 'Up' else 'no'}_ask"
    v = d[col].iloc[i]
    return float(v) if v == v else float("nan")


# --------------------------------------------------------------------------
# one round
# --------------------------------------------------------------------------
def run_round(d: pd.DataFrame, bars: pd.DataFrame, interval_s: int, a) -> dict | None:
    """Return the entry taken in this round, or None."""
    if d.empty:
        return None
    winner = episode_winner(d)
    if winner is None:
        return None

    start_s, end_s = round_window(d, interval_s)
    if start_s not in bars.index or end_s > bars.index.max() + 1:
        return None
    # the tape opens ~1 s before the round (pre-roll); if the window snapped
    # wrongly the first row will be far from `start_s`, so refuse loudly
    first_s = int(d["ts"].iloc[0] // 1000)
    if not (start_s - 1 <= first_s <= start_s + 3):
        return None
    strike = float(bars.loc[start_s, "o"])
    if strike <= 0:
        return None

    # chronological spot ticks (ts_ms, price) over the round.
    #
    # USE THE OPEN, NOT THE CLOSE.  A 1-second bar's close is the price at
    # k.999 but the tick is stamped k.000, so feeding closes to a decision
    # taken at k.000 buys up to 999 ms of look-ahead -- and at 1 ms book
    # cadence that is the whole signal.  The open is the first trade of the
    # second, i.e. genuinely known at the instant it is stamped.
    seg = bars.loc[start_s:end_s - 1]
    tick_ts = (seg.index.to_numpy() * 1000).astype("int64")
    tick_px = seg["o"].to_numpy(dtype="float64")

    ts = d["ts"].to_numpy()
    tau = d["tau_s"].to_numpy(dtype="float64")
    n = len(d)
    ptr = 0  # ticks[ptr:] are still in the future for the current row

    for i in range(n):
        if tau[i] < a.min_tau or tau[i] > a.max_tau:
            continue
        while ptr < len(tick_ts) and tick_ts[ptr] <= ts[i]:
            ptr += 1
        lo = max(0, ptr - a.max_spot_ticks)
        ticks = list(zip(tick_ts[lo:ptr].tolist(), tick_px[lo:ptr].tolist()))
        side = crossed_side(ticks, strike, a.look_s, a.slack_s)
        if side is None:
            continue

        # entry-lag test: price the trade from the book `lag_ms` AFTER the
        # fire, not at it.  A signal that only pays at +0 ms is staleness.
        i0 = i
        if a.entry_lag_ms:
            deadline = ts[i] + a.entry_lag_ms
            j = i
            while j + 1 < n and ts[j + 1] <= deadline:
                j += 1
            i0 = j

        ask = book_ask(d, i0, side)
        if not (ask == ask) or ask <= 0 or ask >= 1:
            continue
        if ask < a.min_price or ask > a.max_price:
            continue
        if book_size(d, i, side) < a.min_size:
            continue

        if a.entry == "taker":
            px = min(ask, a.max_fill_px)
            if px < ask:            # the cap would have refused it
                continue
            filled_at = ts[i0]
        else:
            lim = live_limit(ask, a.buffer, a.buffer_inv, a.max_fill_px)
            if lim is None:
                continue
            px = None
            deadline = ts[i0] + int(a.wait_s * 1000)
            for j in range(i0 + 1, n):
                if ts[j] > deadline:
                    break
                if tau[j] < a.min_tau - a.wait_s:
                    break
                q = book_ask(d, j, side)
                if q == q and q <= lim:     # the book traded through us
                    px = lim
                    filled_at = ts[j]
                    break
            if px is None:
                return None                 # posted, never filled

        # episode_winner() speaks YES/NO; our sides are Up/Down.  Up == YES.
        winner_side = "Up" if winner == "YES" else "Down"
        won = 1.0 if side == winner_side else 0.0
        # stake $1: shares = 1/px, a winner pays $1 each, and the fee is
        # 0.07*p*(1-p) per share -> 0.07*(1-p) per $1 staked.
        return {
            "start_s": start_s, "slug_key": end_s, "side": side, "won": won,
            "px": px, "tau": float(tau[i]), "strike": strike,
            "filled_lag_s": (filled_at - ts[i]) / 1000.0,
            "ev": won * (1 - px) / px - (1 - won) - PM_FEE_RATE * (1 - px),
            "be": breakeven_win_rate(px),
        }
    return None


# --------------------------------------------------------------------------
# battery
# --------------------------------------------------------------------------
def battery(rows: list[dict]) -> None:
    if not rows:
        print("   no trades")
        return
    r = [x["ev"] for x in rows]
    n = len(r)
    mean = st.fmean(r)
    sd = st.stdev(r) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    t = mean / se if se else float("nan")
    win = st.fmean(x["won"] for x in rows)
    paid = st.fmean(x["px"] for x in rows)
    be = st.fmean(x["be"] for x in rows)
    print(f"   n={n:4d}  paid {paid:.4f}  win {100*win:5.1f}%  "
          f"breakeven {100*be:5.1f}%  EV ${mean:+.4f}/$1  pooled t {t:+.2f}")

    half = n // 2
    for name, part in (("1st half", rows[:half]), ("2nd half", rows[half:])):
        if not part:
            continue
        e = [x["ev"] for x in part]
        m = st.fmean(e)
        s = (st.stdev(e) / math.sqrt(len(e))) if len(e) > 1 else 0.0
        print(f"     {name}: n={len(part):4d}  EV ${m:+.4f}  "
              f"t {(m/s if s else float('nan')):+.2f}  "
              f"win {100*st.fmean(x['won'] for x in part):5.1f}%")

    # drop-best-day is meaningless with one day; drop the best 10% instead
    k = max(1, n // 10)
    trimmed = sorted(rows, key=lambda x: -x["ev"])[k:]
    if trimmed:
        print(f"     drop best {k:3d} trades: n={len(trimmed):4d}  "
              f"EV ${st.fmean(x['ev'] for x in trimmed):+.4f}")


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (spot must exist)")
    ap.add_argument("--entry", choices=("taker", "limit"), default="taker")
    ap.add_argument("--look-s", type=float, default=1.0)
    ap.add_argument("--slack-s", type=float, default=2.0)
    ap.add_argument("--min-tau", type=float, default=5.0)
    ap.add_argument("--max-tau", type=float, default=60.0)
    ap.add_argument("--min-price", type=float, default=0.10)
    ap.add_argument("--max-price", type=float, default=0.90)
    ap.add_argument("--max-fill-px", type=float, default=0.65)
    ap.add_argument("--min-size", type=float, default=5.0)
    ap.add_argument("--max-spot-ticks", type=int, default=8)
    ap.add_argument("--buffer", type=float, default=0.25)
    ap.add_argument("--buffer-inv", type=float, default=0.125)
    ap.add_argument("--wait-s", type=float, default=6.0)
    ap.add_argument("--entry-lag-ms", type=int, default=0,
                    help="price the entry from the book this long AFTER the "
                         "fire; a signal that only pays at 0 is staleness")
    ap.add_argument("--sweep-tau", action="store_true")
    a = ap.parse_args()

    paths = episodes(a.asset, a.interval)
    if not paths:
        raise SystemExit(f"no episodes for {a.asset}/{a.interval}")
    day = a.day or tape_date(a.asset, a.interval)
    bars = load_spot(a.asset, day)
    interval_s = 300 if a.interval == "5m" else 900

    print(f"CROSS arm on Polymarket {a.asset}/{a.interval}  {day}")
    print(f"  episodes {len(paths)}   entry={a.entry}   look_s={a.look_s}"
          f"   tau [{a.min_tau},{a.max_tau}]   cap {a.max_fill_px}"
          + (f"   wait {a.wait_s}s" if a.entry == "limit" else ""))
    print(f"  one entry per round; price = the ask of the side spot crossed "
          f"INTO; fee 0.07 p(1-p); won/lost on the tape's own winner")
    print()

    def run(sub) -> list[dict]:
        out = []
        for p in paths:
            d = load(p)
            try:
                got = run_round(d, bars, interval_s, sub)
            except Exception as exc:              # noqa: BLE001
                print("   round failed:", p.name, type(exc).__name__, exc)
                continue
            if got:
                out.append(got)
        out.sort(key=lambda x: x["start_s"])
        return out

    if a.sweep_tau:
        print("tau window sweep (max_tau; min_tau, look_s, cap all fixed)")
        for hi in (15.0, 30.0, 45.0, 60.0, 90.0, 120.0):
            sub = argparse.Namespace(**vars(a))
            sub.max_tau = hi
            print(f"   max_tau {hi:5.0f}:", end="")
            battery(run(sub))
        print()

    rows = run(a)
    print("ALL trades")
    battery(rows)

    if rows:
        print()
        print("by entry price")
        bands = [(0.10, 0.30), (0.30, 0.45), (0.45, 0.55), (0.55, 0.65)]
        for lo, hi in bands:
            part = [r for r in rows if lo <= r["px"] < hi]
            if not part:
                continue
            e = [x["ev"] for x in part]
            m = st.fmean(e)
            s = (st.stdev(e) / math.sqrt(len(e))) if len(e) > 1 else 0.0
            print(f"   {lo:.2f}-{hi:.2f}: n={len(part):3d}  "
                  f"paid {st.fmean(x['px'] for x in part):.4f}  "
                  f"win {100*st.fmean(x['won'] for x in part):5.1f}%  "
                  f"be {100*st.fmean(x['be'] for x in part):5.1f}%  "
                  f"EV ${m:+.4f}  t {(m/s if s else float('nan')):+.2f}")

        print()
        print("by tau at the fire")
        for lo, hi in ((5, 15), (15, 30), (30, 45), (45, 61)):
            part = [r for r in rows if lo <= r["tau"] < hi]
            if not part:
                continue
            print(f"   {lo:2d}-{hi:2d}s: n={len(part):3d}  "
                  f"win {100*st.fmean(x['won'] for x in part):5.1f}%  "
                  f"EV ${st.fmean(x['ev'] for x in part):+.4f}")

        print()
        print("by side")
        for sd in ("Up", "Down"):
            part = [r for r in rows if r["side"] == sd]
            if not part:
                continue
            print(f"   {sd:4s}: n={len(part):3d}  "
                  f"paid {st.fmean(x['px'] for x in part):.4f}  "
                  f"win {100*st.fmean(x['won'] for x in part):5.1f}%  "
                  f"EV ${st.fmean(x['ev'] for x in part):+.4f}")

        if a.entry == "limit":
            lag = [r["filled_lag_s"] for r in rows]
            print()
            print(f"   fill lag: median {st.median(lag):.2f}s  "
                  f"p90 {sorted(lag)[int(len(lag)*0.9)]:.2f}s")


if __name__ == "__main__":
    main()
