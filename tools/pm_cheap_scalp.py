"""BUY THE 1-5c LONG SHOT AND SCALP IT: does the venue let you, and does it pay?

THE CLAIM (operator, 2026-09-17): a token can print 0.01 and later print 0.10
inside the same 5-minute round -- "x10 the money" -- so rest a bid at 0.02, plant
a resting sell at 0.05, and never wait for the candle. The stated constraints:
entry only in the first 90 s (tau >= 210), maker entry only, target 0.05-0.06,
30 s time stop.

WHY THIS TOOL EXISTS. Every version of this idea that "works" in a spreadsheet
does so through one bug: it fills the resting order because the DISPLAYED price
reached the limit. A resting bid does not fill because you want it to; it fills
when someone SELLS INTO IT. This tape carries yes/no LAST-TRADED prices at ~1 ms,
so a fill can be required to be an actual PRINT at or through the price:

    BUY  at L  fills only if a trade prints at <= L      (someone sold to us)
    SELL at T  fills only if a trade prints at >= T      (someone bought from us)

and the mirrored "displayed" test (bid came down to L / ask went up to T) is
reported alongside as the ARTIFACT column, so the size of that lie is visible
instead of assumed.

DATA. data/hf2/kartik (2026-07-17, ONE day) via tools/pm_tape: 6 assets x 288
5m episodes, top-10 L2 both sides + LTP, ~1 ms event cadence. ONE DAY means no
day-clustered test is possible; the cluster column is the UTC HOUR and the
verdict must stay weak. Never ship a parameter off this file.

Usage:
    python -m tools.pm_cheap_scalp premise  [--asset btc] [--tau-min 210]
    python -m tools.pm_cheap_scalp scalp    --entry 0.02 --target 0.05
    python -m tools.pm_cheap_scalp artifact --entry 0.02 --target 0.05
"""
from __future__ import annotations

import argparse
import collections
import math
import pathlib
import sys
import time
import zipfile

import numpy as np
import pandas as pd

from tools.pm_tape import episodes, load, episode_winner

ROOT = pathlib.Path(__file__).resolve().parents[1]
BARS = ROOT / "data" / "bars_1s" / "daily"
SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "bnb": "BNBUSDT",
       "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT"}


def load_bars(asset: str, day: str = "2026-07-17") -> tuple[np.ndarray, np.ndarray]:
    """(open_time_ms, open) for the tape day. Columns are taken BY POSITION."""
    z = zipfile.ZipFile(BARS / f"{SYM[asset]}-1s-{day}.zip")
    with z.open(z.namelist()[0]) as fh:
        df = pd.read_csv(fh, header=None, usecols=[0, 1], names=["us", "o"])
    return (df["us"].to_numpy() // 1000), df["o"].to_numpy(dtype=float)


def spot_mom(bar_ms: np.ndarray, bar_o: np.ndarray, ts_ms: np.ndarray,
             look_s: int) -> np.ndarray:
    """Spot return in bps over `look_s` seconds, known at each tape row's instant.

    Uses the bar's OPEN price, never the close: a close at second k carries up to
    999 ms of look-ahead (the trap that faked a winner on this corpus before).
    """
    i = np.searchsorted(bar_ms, ts_ms, side="right") - 1
    j = i - look_s
    ok = (i >= 0) & (j >= 0) & (j < len(bar_o))
    out = np.full(len(ts_ms), np.nan)
    ii, jj = i[ok], j[ok]
    prev = bar_o[jj]
    out[ok] = 1e4 * (bar_o[ii] - prev) / prev
    return out

#: Polymarket taker fee per SHARE: 0.07 * p * (1-p). Makers pay zero
#: (feeType crypto_fees_v2, takerOnly: true). Measured on a real fill:
#: 500 shares @ 0.01 -> $0.3465 = 0.07*0.01*0.99*500, to the cent.
def fee_share(p: float) -> float:
    return 0.07 * p * (1.0 - p)


def _side_cols(df: pd.DataFrame, side: str) -> pd.DataFrame:
    """LTP / bid / ask for one side, time-ordered, with tau_s."""
    out = pd.DataFrame({
        "ts": df["ts"].to_numpy(),
        "tau": df["tau_s"].to_numpy(),
        "ltp": df[f"{side}_ltp"].to_numpy(dtype=float),
        "bid": df[f"{side}_bid"].to_numpy(dtype=float),
        "ask": df[f"{side}_ask"].to_numpy(dtype=float),
    })
    out["tick"] = out["ltp"].ffill().bfill()
    return out


#: masks are built once per side-round: a trade is a CHANGE in the last-traded
#: price (the tape is a state series, not a print list).
def _trade_masks(tick: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(changed, finite) for a last-traded-price series."""
    finite = np.isfinite(tick)
    changed = np.zeros(len(tick), dtype=bool)
    changed[0] = finite[0]
    changed[1:] = finite[1:] & (~finite[:-1] | (tick[1:] != tick[:-1]))
    return changed, finite


def _first_cross(changed: np.ndarray, tick: np.ndarray, level: float,
                 *, le: bool, tau: np.ndarray, after_s: float,
                 start: int = 0) -> int | None:
    """First index > start where a NEW print lands at/through `level`.

    `tick` is the last-traded price. A trade is a CHANGE in it, so a level that
    was already there when we arrived must not count -- we were not resting yet.
    """
    hit = changed & (tau < after_s) & ((tick <= level) if le else (tick >= level))
    idx = np.where(hit & (np.arange(len(tick)) > start))[0]
    return int(idx[0]) if idx.size else None


# --------------------------------------------------------------------------- #
# PART A -- the premise, price-free: how cheap does it get, when, and does it
# come back? This is the part the operator's eyeballs were reading.
# --------------------------------------------------------------------------- #
def premise(args) -> None:
    assets = [args.asset] if args.asset != "all" else ["btc", "eth", "bnb", "sol", "xrp", "doge"]
    recs = []
    for asset in assets:
        eps = episodes(asset, args.interval)
        for p in eps:
            d = load(p)
            if d is None or d.empty:
                continue
            w = episode_winner(d)
            if w is None:
                continue
            for side in ("yes", "no"):
                s = _side_cols(d, side)
                tick = s["tick"].to_numpy()
                tau = s["tau"].to_numpy()
                ok = np.isfinite(tick)
                if ok.sum() < 10:
                    continue
                won = 1 if (side == "yes") == (w == "YES") else 0
                early = np.where(tau >= args.tau_min)[0]
                for band in (0.01, 0.02, 0.03, 0.05):
                    idx = early[ok[early] & (tick[early] <= band)]
                    if idx.size == 0:
                        continue
                    i = int(idx[0])
                    fwd = tick[i:]
                    recs.append(dict(asset=asset, slug=p.stem, side=side, band=band,
                                     tau=float(tau[i]), won=won,
                                     mx=float(np.nanmax(fwd)),
                                     mx60=float(np.nanmax(tick[i:][tau[i:] - tau[i] <= 60]))
                                     if (tau[i:] - tau[i] <= 60).any() else np.nan))
    df = pd.DataFrame(recs)
    if df.empty:
        print("no cheap prints found"); return
    side_rounds = len(df.drop_duplicates(["slug", "side"]))
    print(f"\n=== PREMISE: {side_rounds} side-rounds, {df.slug.nunique()} episodes, "
          f"{len(assets)} asset(s), {args.interval}, entry restricted to tau >= {args.tau_min:.0f}s "
          f"(the first {300 - args.tau_min:.0f}s of a 5m round) ===")
    print(f"{'band':>6} {'n':>6} {'fill%':>6} {'tau':>6} {'win%':>6} "
          f"{'medMX':>7} {'P(MX>=2x)':>10} {'P(MX>=0.10)':>12} {'med MX+60s':>11}")
    for band in (0.01, 0.02, 0.03, 0.05):
        s = df[df.band == band]
        if s.empty:
            continue
        n = len(s)
        print(f"{band:>6.2f} {n:>6d} {n / side_rounds:>6.1%} {s.tau.median():>6.0f} "
              f"{s.won.mean():>6.1%} {s.mx.median():>7.3f} "
              f"{(s.mx >= 2 * band).mean():>10.1%} {(s.mx >= 0.10).mean():>12.1%} "
              f"{s.mx60.median():>11.3f}")
    print("\n(mx = best print of that token anywhere AFTER the cheap print, to the end of the round.")


# --------------------------------------------------------------------------- #
# PART B -- the trade, with fills that require the tape to trade through.
# --------------------------------------------------------------------------- #
def _simulate_side(s: pd.DataFrame, tau: np.ndarray, tick: np.ndarray,
                   changed: np.ndarray, won: int, args, *, naive: bool,
                   sign: int = 1) -> dict | None:
    """One side-round. Returns a record, or None if the entry never filled."""
    entry = args.entry
    bid = s["bid"].to_numpy(dtype=float)
    ask = s["ask"].to_numpy(dtype=float)
    # ---- entry -------------------------------------------------------------
    if naive:
        # THE ARTIFACT: fill because the *displayed bid* reached our limit.
        idx = np.where((tau < args.tau_place) & np.isfinite(bid) & (bid <= entry))[0]
        if idx.size == 0:
            return None
        i_fill = int(idx[0])
        px_fill = entry
    else:
        i_fill = _first_cross(changed, tick, entry, le=True, tau=tau,
                              after_s=args.tau_place)
        if i_fill is None:
            return None
        px_fill = min(float(tick[i_fill]), entry)
    tau_fill = float(tau[i_fill])
    if getattr(args, "entry_tau_min", 0) and tau_fill < args.entry_tau_min:
        return None                              # fill came too late to work
    if getattr(args, "entry_tau_max", 0) and tau_fill > args.entry_tau_max:
        return None                              # too early / stale round
    # ---- the operator's trigger 3: only buy while the spot REVERSES ------ #
    mom = np.nan
    if getattr(args, "spot_look", 0) and "spot_mom" in s:
        mom = float(s["spot_mom"].to_numpy()[i_fill])
        if not np.isfinite(mom) or mom * sign <= 0:
            return None                      # fail closed: no reversal, no trade
    shares = 1.0 / px_fill                      # $1 staked
    # maker entry -> no fee (takerOnly schedule)
    # ---- exit --------------------------------------------------------------
    target = args.target
    if naive:
        # THE ARTIFACT: the *displayed ask* reached our target.
        jdx = np.where((tau < tau_fill) & np.isfinite(ask) & (ask >= target))[0]
        i_exit = int(jdx[0]) if jdx.size else None
    else:
        i_exit = _first_cross(changed, tick, target, le=False, tau=tau,
                              after_s=tau_fill, start=i_fill)
    # time stop / round end
    deadline = min(tau_fill - args.hold_s, args.tau_stop)
    i_stop = None
    stop = np.where(tau <= deadline)[0]
    if stop.size:
        i_stop = int(stop[0])
    if i_exit is not None and (i_stop is None or i_exit <= i_stop):
        mode, px_out, fee_out = "target", target, 0.0
        i_end = i_exit
    else:
        i_end = i_stop if i_stop is not None else len(tick) - 1
        b = bid[i_end]
        if np.isfinite(b) and b > 0:
            mode, px_out = "bid_dump", float(b)
            fee_out = fee_share(px_out)          # taker leg pays
        else:
            mode, px_out, fee_out = "settle", (1.0 if won else 0.0), 0.0
    pnl = shares * (px_out - fee_out) - 1.0
    return dict(tau_fill=tau_fill, px_fill=px_fill, shares=shares, exit=mode,
                tau_exit=float(tau[i_end]), px_out=px_out, won=won, pnl=pnl, mom=mom)


def _battery(vals, hours, label) -> None:
    n = len(vals)
    if n == 0:
        print(f"  {label:<26} none"); return
    m = float(np.mean(vals))
    sd = float(np.std(vals, ddof=0))
    t = m / (sd / math.sqrt(n)) if sd else 0.0
    byh = collections.defaultdict(list)
    for h, v in zip(hours, vals):
        byh[h].append(v)
    hm = [float(np.mean(v)) for v in byh.values()]
    mh, sh = float(np.mean(hm)), float(np.std(hm, ddof=0))
    th = mh / (sh / math.sqrt(len(hm))) if sh else 0.0
    h1 = float(np.mean(vals[:n // 2])); h2 = float(np.mean(vals[n // 2:]))
    drop = float(np.mean(sorted(vals)[:-1])) if n > 1 else m
    print(f"  {label:<26} n={n:4d} mean={m:+.4f} med={np.median(vals):+.4f} "
          f"t={t:+.2f} hour-t={th:+.2f} halves={h1:+.3f}/{h2:+.3f} drop1={drop:+.4f}")


def scalp(args) -> None:
    assets = [args.asset] if args.asset != "all" else ["btc", "eth", "bnb", "sol", "xrp", "doge"]
    rows, seen = [], 0
    for asset in assets:
        eps = episodes(asset, args.interval)
        for p in eps:
            d = load(p)
            if d is None or d.empty:
                continue
            w = episode_winner(d)
            if w is None:
                continue
            seen += 1
            hour = int(d["ts"].iloc[0] // 3_600_000)
            for side in ("yes", "no"):
                s = _side_cols(d, side)
                tau = s["tau"].to_numpy(); tick = s["tick"].to_numpy()
                changed, _ = _trade_masks(tick)
                won = 1 if (side == "yes") == (w == "YES") else 0
                r = _simulate_side(s, tau, tick, changed, won, args, naive=args.naive)
                if r is None:
                    continue
                r.update(asset=asset, slug=p.stem, side=side, hour=hour)
                rows.append(r)
    df = pd.DataFrame(rows)
    head = ("ARTIFACT (displayed price fills us)" if args.naive
            else "STRICT (the tape must print through our price)")
    print(f"\n=== SCALP: {head} ===")
    print(f"    entry resting bid {args.entry:.2f} placed at tau {args.tau_place:.0f}s | "
          f"target {args.target:.2f} | hold {args.hold_s:.0f}s | stop at tau {args.tau_stop:.0f}s | "
          f"{args.interval}, {len(assets)} asset(s)")
    if df.empty:
        print("  no entries at all"); return
    fills = len(df)
    fills_round = df.slug.nunique()
    print(f"  entries: {fills} on {fills_round} of {seen} rounds ({fills_round / max(1, seen):.1%} of rounds) "
          f"| {fills / max(1, seen):.2f} per round")
    mix = df.exit.value_counts()
    print("  exit mix: " + ", ".join(f"{k} {v} ({v / fills:.0%})" for k, v in mix.items()))
    print(f"  win rate of the token we BOUGHT: {df.won.mean():.1%} | "
          f"mean price paid {df.px_fill.mean():.4f} | shares/$1 {df.shares.mean():.1f}")
    print(f"  P&L per $1 staked: mean {df.pnl.mean():+.4f} | median {df.pnl.median():+.4f} | "
          f"skew {df.pnl.skew():+.1f} | best {df.pnl.max():+.2f} | worst {df.pnl.min():+.2f}")
    lg = np.log1p(df.pnl.clip(lower=-0.999))
    print(f"  log-growth per trade {lg.mean():+.4f} (a $1 ticket, 1 position/round)")
    for m in ("target", "bid_dump", "settle"):
        c = df[df.exit == m]
        if len(c):
            print(f"    {m:<9} n={len(c):4d} mean {c.pnl.mean():+.3f} | "
                  f"won {c.won.mean():.1%} | exit px {c.px_out.mean():.3f}")
    print("\n  --- battery (values = P&L per $1) ---")
    _battery(list(df.pnl), list(df.hour), "all entries")
    if args.assets_together:
        for a in sorted(df.asset.unique()):
            c = df[df.asset == a]
            _battery(list(c.pnl), list(c.hour), f"asset {a}")


def grid(args) -> None:
    """One pass of the tape, many (entry, target, tau_place) cells.

    Loading is the bottleneck (~20 ms of parquet per episode), so the whole grid
    is evaluated inside a single episode loop. EVERY cell is printed, including
    the losing ones -- a sweep that reports only its best cell is a fit.
    """
    cells = [(e, t, p, f, lo, hi) for (e, t) in args.grid_pairs
             for p in args.grid_tau for f in args.grid_filter
             for (lo, hi) in args.grid_entry_tau]
    assets = [args.asset] if args.asset != "all" else ["btc", "eth", "bnb", "sol", "xrp", "doge"]
    rows: dict[tuple, list] = {c: [] for c in cells}
    seen = 0
    for asset in assets:
        bar_ms, bar_o = (load_bars(asset) if any(args.grid_filter) else (None, None))
        for p in episodes(asset, args.interval):
            d = load(p)
            if d is None or d.empty:
                continue
            w = episode_winner(d)
            if w is None:
                continue
            seen += 1
            hour = int(d["ts"].iloc[0] // 3_600_000)
            ts = d["ts"].to_numpy()
            smom = {L: spot_mom(bar_ms, bar_o, ts, L) for L in set(args.grid_filter) if L}
            for side in ("yes", "no"):
                s = _side_cols(d, side)
                tau = s["tau"].to_numpy(); tick = s["tick"].to_numpy()
                changed, _ = _trade_masks(tick)
                won = 1 if (side == "yes") == (w == "YES") else 0
                sign = 1 if side == "yes" else -1    # Up wins if spot rises
                for c in cells:
                    entry, target, place, flt, lo, hi = c
                    if flt:
                        s["spot_mom"] = smom[flt]
                    elif "spot_mom" in s:
                        del s["spot_mom"]
                    a = argparse.Namespace(**vars(args))
                    a.entry, a.target, a.tau_place, a.spot_look = entry, target, place, flt
                    a.entry_tau_min, a.entry_tau_max = lo, hi
                    r = _simulate_side(s, tau, tick, changed, won, a, naive=False,
                                       sign=sign)
                    if r is not None:
                        r.update(asset=asset, hour=hour, slug=p.stem)
                        rows[c].append(r)
    print(f"\n=== GRID (strict fills): {seen} rounds, {len(assets)} asset(s), "
          f"hold {args.hold_s:.0f}s, stop at tau {args.tau_stop:.0f}s ===")
    print(f"{'entry':>5} {'tgt':>5} {'fill tau':>10} {'filter':>10} {'entries':>7} {'%rd':>6} "
          f"{'tgt%':>6} {'win%':>6} {'mean':>8} {'t':>6} {'h-t':>6} {'halves':>14}")
    for c in cells:
        rs = rows[c]
        if not rs:
            continue
        e, t, place, flt, lo, hi = c
        vals = [r["pnl"] for r in rs]
        hours = [r["hour"] for r in rs]
        n = len(vals)
        m = float(np.mean(vals)); sd = float(np.std(vals, ddof=0))
        tstat = m / (sd / math.sqrt(n)) if sd else 0.0
        byh = collections.defaultdict(list)
        for h, v in zip(hours, vals):
            byh[h].append(v)
        hm = [float(np.mean(v)) for v in byh.values()]
        sh = float(np.std(hm, ddof=0))
        th = float(np.mean(hm)) / (sh / math.sqrt(len(hm))) if sh else 0.0
        h1 = float(np.mean(vals[:n // 2])); h2 = float(np.mean(vals[n // 2:]))
        tgt = sum(1 for r in rs if r["exit"] == "target") / n
        won = sum(r["won"] for r in rs) / n
        tag = "-none-" if not flt else f"spot {flt}s"
        band = f"{lo:.0f}-{hi:.0f}" if hi else f">={lo:.0f}"
        print(f"{e:>5.2f} {t:>5.2f} {band:>10} {tag:>10} {n:>7d} {n / (2 * seen):>6.1%} {tgt:>6.1%} "
              f"{won:>6.1%} {m:>+8.4f} {tstat:>+6.2f} {th:>+6.2f} "
              f"{h1:>+6.3f}/{h2:>+6.3f}")
    print("  %rd = entries as a share of side-rounds | tgt% = share of entries that sold at the target")
    print("  filter 'spot Ns' = take the fill only if the spot moved TOWard the cheap side over N seconds")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["premise", "scalp", "artifact", "grid"])
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--assets-together", action="store_true",
                    help="also print per-asset battery rows")
    ap.add_argument("--interval", default="5m", choices=["5m", "15m"])
    ap.add_argument("--entry", type=float, default=0.02)
    ap.add_argument("--target", type=float, default=0.05)
    ap.add_argument("--hold-s", type=float, default=30.0)
    ap.add_argument("--tau-place", type=float, default=210.0,
                    help="entry allowed only while tau is above this")
    ap.add_argument("--tau-stop", type=float, default=15.0,
                    help="never hold past this tau without dumping")
    ap.add_argument("--tau-min", type=float, default=210.0, help="premise mode only")
    ap.add_argument("--grid-pairs", default="0.01:0.03,0.01:0.05,0.01:0.06,0.02:0.05,0.02:0.06,0.03:0.06,0.05:0.10",
                    help="grid mode: entry:target pairs")
    ap.add_argument("--grid-tau", default="300,210",
                    help="grid mode: tau_place values (210 = the operator's first-90s rule)")
    ap.add_argument("--grid-filter", default="0",
                    help="grid mode: spot-reversal lookbacks in seconds, 0 = off")
    ap.add_argument("--grid-entry-tau", default="0:0",
                    help="grid mode: tau-at-fill bands, e.g. '0:0,60:0,90:0'")
    ap.add_argument("--spot-look", type=float, default=0.0,
                    help="scalp mode: require the spot to reverse over N seconds")
    a = ap.parse_args()
    a.naive = (a.mode == "artifact")
    a.grid_pairs = [tuple(float(x) for x in q.split(":")) for q in a.grid_pairs.split(",")]
    a.grid_tau = [float(x) for x in a.grid_tau.split(",")]
    a.grid_filter = [int(x) for x in a.grid_filter.split(",")]
    a.grid_entry_tau = [tuple(float(y) for y in x.split(":"))
                        for x in a.grid_entry_tau.split(",")]
    t0 = time.time()
    if a.mode == "premise":
        premise(a)
    elif a.mode == "grid":
        grid(a)
    else:
        scalp(a)
    print(f"\n[{time.time() - t0:.0f}s]")


if __name__ == "__main__":
    main()
