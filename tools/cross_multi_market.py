"""Does the cross arm's mechanic transfer to other markets?

The arm is pure mechanics -- spot crosses the strike, buy the side it crossed
into -- with no model to be wrong, so there is nothing BTC-specific in it by
construction. Whether it WORKS elsewhere is an empirical question, and this
answers it.

WHY IT NEEDS THREE INPUTS. The venue serves no historical order book
(`/market/orderbook` -> 404), so the three things a cross needs come from three
different places:

  strike + outcome : the venue, via market/detail -> the `rounds` table.
                     Harvest with `tools.harvest_wide --prefix all`.
  spot             : Binance, 1s klines or aggTrades. The ONLY real download.
                     `cross_look_s` is 2s, so 1m bars are useless here.
  executable price : an L2 book capture (data/hf/updown_5m.parquet covers eight
                     coins). Optional -- omit it and the tool reports the raw
                     WIN RATE, which is the go/no-go on its own: if the signal
                     is not there, no price makes it tradeable.

WHAT ALREADY MEASURED. On live recorded data, same gates and same days:
    btc-updown-5m  n=84  win 0.762  EV/$ +0.2144
    eth-updown-5m  n=42  win 0.571  EV/$ -0.0703
and ETH stayed negative across depth 0.25-2.0 and tau 10-30 / 30-90 / 10-120.
The working theory is that the edge is the venue UNDER-REACTING to a cross, so
it should scale with book depth -- BTC 5m holds ~$748k against ETH 5m's ~$44k,
and a thin book reprices instantly because one maker moves it. That predicts
BTC's own 15m/1h are better candidates than other coins. This tool is how that
gets checked rather than assumed.

Usage:
    python -m tools.cross_multi_market --spot data/spot1s
    python -m tools.cross_multi_market --spot data/spot1s --books data/hf/updown_5m.parquet
    python -m tools.cross_multi_market --spot data/spot1s --sweep
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import sys
import zipfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings          # noqa: E402
from btcpredict.edge.signal import fee_rate_for_price  # noqa: E402
from btcpredict.store.db import Store           # noqa: E402

# slug prefix -> the Binance spot symbol whose price the strike is set from
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "bnb": "BNBUSDT",
          "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT",
          "hype": "HYPEUSDT", "zec": "ZECUSDT"}


def load_spot(path: str, symbol: str) -> np.ndarray | None:
    """(ts_ms, price) sorted. Accepts Binance 1s-kline or aggTrades dumps.

    Both arrive as headerless CSV, optionally still zipped. Klines put close at
    column 4 and open_time at 0; aggTrades put price at 1 and time at 5. The
    shapes are distinct enough to tell apart by column count.
    """
    # recursive: the Binance dumps land as bars_1s/monthly/*.zip and
    # bars_1s/daily/*.zip, and a per-symbol subdirectory layout is also common.
    files = sorted({f for ext in ("csv", "zip")
                    for f in glob.glob(os.path.join(path, "**", f"{symbol}*.{ext}"),
                                       recursive=True)})
    if not files:
        return None
    out = []
    for f in files:
        try:
            if f.endswith(".zip"):
                with zipfile.ZipFile(f) as z:
                    name = z.namelist()[0]
                    with z.open(name) as fh:
                        d = pd.read_csv(fh, header=None)
            else:
                d = pd.read_csv(f, header=None)
        except Exception as e:                       # noqa: BLE001
            print(f"  ! {os.path.basename(f)}: {type(e).__name__}: {e}")
            continue
        if d.shape[1] >= 11:                         # kline
            ts, px = d[0].values, d[4].values
        elif d.shape[1] >= 6:                        # aggTrades
            ts, px = d[5].values, d[1].values
        else:
            continue
        ts = np.asarray(ts, dtype="float64")
        # Binance 1s dumps carry MICROSECOND open_time (16 digits). Verified on
        # BTCUSDT-1s-2026-08: 1785542400000000 // 1000 -> 2026-08-01 00:00:00.
        # Older dumps use ms, and some tooling emits seconds, so normalise on
        # magnitude rather than trusting one layout.
        if ts.max() > 1e15:                          # microseconds -> ms
            ts /= 1000.0
        elif ts.max() < 1e12:                        # seconds -> ms
            ts *= 1000.0
        out.append(np.column_stack([ts, np.asarray(px, dtype="float64")]))
    if not out:
        return None
    a = np.concatenate(out)
    a = a[np.argsort(a[:, 0])]
    return a[np.isfinite(a).all(axis=1)]


def load_books(path: str | None) -> dict:
    """slug -> (seconds_to_close, up_ask, down_ask), ascending by tau."""
    if not path or not os.path.exists(path):
        return {}
    h = pd.read_parquet(path, columns=["market_slug", "outcome", "seconds_to_close",
                                       "best_ask"])
    h = h.dropna(subset=["best_ask"])
    piv = h.pivot_table(index=["market_slug", "seconds_to_close"], columns="outcome",
                        values="best_ask", aggfunc="first").dropna()
    out = {}
    for slug, g in piv.reset_index().groupby("market_slug"):
        g = g.sort_values("seconds_to_close")
        out[slug] = (g.seconds_to_close.values, g.get("Up").values, g.get("Down").values)
    return out


def crosses(ts, px, strike, start_ms, end_ms, lo, hi, look_s, slack_s, maxdep, cap):
    """Every qualifying cross in one round: (tau, side)."""
    m = (ts >= start_ms - 1000) & (ts <= end_ms)
    t, p = ts[m], px[m]
    if len(t) < 10:
        return []
    tau = (end_ms - t) / 1000.0
    itm = np.where(p > strike, "Up", "Down")
    hits, n = [], 0
    j = 0
    for i in range(1, len(t)):
        if not (lo <= tau[i] <= hi):
            continue
        # newest sample at least look_s old, and no older than look+slack
        while j < i and (t[i] - t[j]) / 1000.0 > look_s + slack_s:
            j += 1
        ref = None
        for q in range(i - 1, j - 1, -1):
            if (t[i] - t[q]) / 1000.0 >= look_s:
                ref = q
                break
        if ref is None or itm[ref] == itm[i]:
            continue
        if 1e4 * abs(p[i] - strike) / strike > maxdep:
            continue
        n += 1
        hits.append((float(tau[i]), str(itm[i])))
        if n >= cap:
            break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot", required=True, help="dir of Binance 1s-kline/aggTrades dumps")
    ap.add_argument("--books", default=None, help="L2 parquet for executable pricing")
    ap.add_argument("--sweep", action="store_true", help="per-market knob sweep")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()

    store = Store(a.db or settings.db_path)
    rounds = store.query(
        "SELECT slug, start_ms, end_ms, strike, winner FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL ORDER BY start_ms")
    if not rounds:
        print("no settled rounds in the store -- run tools.harvest_wide --prefix all")
        return 1
    # store.query yields sqlite3.Row; pandas does not unpack those into columns
    df = pd.DataFrame([dict(r) for r in rounds])
    df["fam"] = df.slug.str.extract(r"^([a-z]+-updown-\d+[mhd])")[0]
    df["coin"] = df.slug.str.extract(r"^([a-z]+)-")[0]
    books = load_books(a.books)
    print(f"rounds {len(df)} | families {df.fam.nunique()} | books for {len(books)} slugs\n")

    spots: dict[str, np.ndarray] = {}
    for coin in sorted(df.coin.dropna().unique()):
        sym = SYMBOL.get(coin)
        s = load_spot(a.spot, sym) if sym else None
        if s is not None:
            spots[coin] = s
            span = (s[-1, 0] - s[0, 0]) / 86400e3
            print(f"  spot {sym:9s} {len(s):>9,} ticks over {span:.1f} days")
        else:
            print(f"  spot {str(sym):9s} MISSING -- {coin} cannot be tested")
    print()

    base = dict(lo=settings.cross_min_tau_s, hi=settings.cross_max_tau_s,
                look_s=settings.cross_look_s, slack_s=2.0,
                maxdep=settings.cross_max_depth_bps, cap=settings.cross_max_per_round)

    def run(sub, coin, **kw):
        p = {**base, **kw}
        ts, px = spots[coin][:, 0], spots[coin][:, 1]
        rows = []
        for r in sub.itertuples():
            for tau, side in crosses(ts, px, r.strike, r.start_ms, r.end_ms, **p):
                won = float(side == r.winner)
                fill = np.nan
                bk = books.get(r.slug)
                if bk is not None:
                    taus, ua, da = bk
                    k = int(np.argmin(np.abs(taus - tau)))
                    if abs(taus[k] - tau) <= 15:
                        fill = float(ua[k] if side == "Up" else da[k])
                rows.append((pd.Timestamp(r.start_ms, unit="ms").strftime("%Y-%m-%d"),
                             tau, won, fill))
        return pd.DataFrame(rows, columns=["day", "tau", "won", "fill"])

    def report(lab, t):
        if len(t) < 15:
            print(f"  {lab:<28s} n={len(t)} too few")
            return
        w = t.won.mean()
        se = math.sqrt(w * (1 - w) / len(t))
        line = (f"  {lab:<28s} n={len(t):<5d} {len(t)/max(t.day.nunique(),1):5.1f}/day "
                f" win {w:.3f} [{w-1.96*se:.3f}-{w+1.96*se:.3f}]")
        f = t.dropna(subset=["fill"])
        f = f[(f.fill > 0) & (f.fill < 1)]
        if len(f) >= 15:
            fee = np.array([fee_rate_for_price(x) for x in f.fill])
            r = (f.won.values / f.fill.values) - (1 + fee)
            byd = f.assign(r=r).groupby("day").r.mean()
            tt = (byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd)))
                  if len(byd) > 1 and byd.std(ddof=1) > 0 else float("nan"))
            line += f"  paid {f.fill.mean():.3f}  EV/$ {r.mean():+.4f}  day-t {tt:5.2f}"
        else:
            line += "   (no book -> win rate only)"
        print(line)

    print("CROSS SIGNAL BY MARKET  (gates scaled to round length)")
    for fam, sub in df.groupby("fam"):
        coin = sub.coin.iloc[0]
        if coin not in spots or len(sub) < 20:
            continue
        rl = float(np.median(sub.end_ms - sub.start_ms)) / 1000.0
        sc = rl / 300.0
        report(f"{fam} (tau {base['lo']:.0f}-{base['hi']:.0f})", run(sub, coin))
        if sc > 1.5:
            report(f"{fam} (tau {base['lo']*sc:.0f}-{base['hi']*sc:.0f})",
                   run(sub, coin, lo=base["lo"] * sc, hi=base["hi"] * sc))

    if a.sweep:
        print("\nPER-MARKET KNOB SWEEP")
        for fam, sub in df.groupby("fam"):
            coin = sub.coin.iloc[0]
            if coin not in spots or len(sub) < 20:
                continue
            print(f"\n  --- {fam} ---")
            for dep in (0.25, 0.5, 1.0, 2.0):
                report(f"depth<={dep}bps", run(sub, coin, maxdep=dep))
            for lo, hi in ((10, 30), (10, 60), (30, 90), (10, 120)):
                report(f"tau {lo}-{hi}s", run(sub, coin, lo=lo, hi=hi))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
