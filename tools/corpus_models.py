"""Evaluate every pricing model on the 53-day Polymarket 5m corpus.

The corpus is ``data/btc5m/data/<day>/<start_epoch>/``: 15,228 BTC 5-minute
rounds from 2026-04-29 to 2026-06-20, each carrying

  * ``metadata.json`` -> ``events[0].eventMetadata.priceToBeat`` (the Chainlink
    price at window start -- the STRIKE) and ``finalPrice`` (Chainlink at window
    end). The market resolves Up iff finalPrice >= priceToBeat, so the label is
    the settlement itself, not a tape proxy. ``outcomePrices`` agrees on every
    window that carries both, and is used as a cross-check.
  * ``ticks_dense.parquet`` -> the 1-second Binance tape for the window (close,
    volume, taker_buy_volume). This is what feeds the models' candles.

Why this is a better instrument than ``tools/backtest_models.py``
----------------------------------------------------------------
That tool fetches live klines and labels rounds with ``tape_close > tape_open``.
``tools/corpus_ml_dataset.py`` documents why that label is wrong: settlement is
Chainlink, the tape is Binance, and the two disagree on ~4% of windows. It also
has no market price, so it can only rank models on the model half. Here the
label is the settlement, the strike is the one the market used, and 3,164
windows additionally carry Polymarket's own on-chain fills (see
``corpus_market_prices.py``) for the execution half.

Faithfulness to live
--------------------
Contexts are built to mirror ``PriceHistory.ctx`` exactly: ``closes`` is the
completed 1m bars plus the current price appended as a forming bar, while
``volumes``/``taker_buys`` are completed bars only. ``sigma_1m`` comes from
``btcpredict.pricing.vol.VolEstimator`` (EWMA 0.97 + simple stdev blend, floored,
hour-seasonal), fed strictly past data -- NOT the ``rolling_sigma`` used by
``arm_overlap``, which config.py documents as not matching live.

Usage:
    python -m tools.corpus_models cache            # build the cache (~1 min)
    python -m tools.corpus_models eval             # all models, all taus
    python -m tools.corpus_models eval --models digital momentum sniper
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import pickle
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from btcpredict.models import MODEL_NAMES, ModelContext, build_model
from btcpredict.pricing.vol import VolEstimator

ROOT = os.path.join("data", "btc5m", "data")
CACHE = os.path.join("data", "btc5m", "corpus_cache.pkl")
WINDOW_S = 300
BAR_S = 60
TAU_GRID = (3, 5, 8, 10, 15, 20, 30, 45, 60, 90, 120, 150, 180, 210, 240)

TAPE_COLS = ("timestamp", "close", "volume", "taker_buy_volume")


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def _read_window(d: str) -> dict | None:
    """-> {start_ms, strike, final, y, close[], vol[], taker[]} or None."""
    try:
        meta = json.load(open(os.path.join(d, "metadata.json")))
    except Exception:
        return None
    ev = (meta.get("events") or [{}])[0].get("eventMetadata") or {}
    beat = ev.get("priceToBeat")
    fin = ev.get("finalPrice")
    slug = meta.get("slug") or ""
    try:
        start_s = int(slug.rsplit("-", 1)[1])
    except (ValueError, IndexError):
        return None
    # Prefer the settlement rule; fall back to the market's own resolution.
    if beat is not None and fin is not None:
        y = int(float(fin) >= float(beat))
    else:
        op = meta.get("outcomePrices")
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except Exception:
                op = None
        if not op or len(op) != 2:
            return None
        y = int(float(op[0]) > float(op[1]))
        beat, fin = None, None
    tp = os.path.join(d, "ticks_dense.parquet")
    if not os.path.exists(tp):
        return None
    try:
        t = pd.read_parquet(tp, columns=list(TAPE_COLS))
    except Exception:
        return None
    if len(t) < WINDOW_S // 2:
        return None
    ts = t["timestamp"].to_numpy()
    if np.issubdtype(ts.dtype, np.datetime64):
        ts = ts.astype("datetime64[s]").astype(np.int64)
    else:
        ts = ts.astype(np.int64)
        if ts.max() > 1e12:          # milliseconds
            ts = ts // 1000
    order = np.argsort(ts)
    close = t["close"].to_numpy(dtype=np.float64)[order]
    vol = t["volume"].to_numpy(dtype=np.float64)[order]
    taker = t["taker_buy_volume"].to_numpy(dtype=np.float64)[order]
    if not np.all(np.isfinite(close)) or (close <= 0).any():
        return None
    return {"start_ms": start_s * 1000, "strike": beat, "final": fin, "y": y,
            "close": close.astype(np.float32), "vol": vol.astype(np.float32),
            "taker": taker.astype(np.float32)}


def build_cache(limit: int | None, workers: int) -> dict:
    dirs = sorted(glob.glob(os.path.join(ROOT, "*", "*", "")))
    if limit:
        dirs = dirs[:limit]
    t0 = time.time()
    out: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(_read_window, dirs, chunksize=64)):
            if r is not None:
                out.append(r)
            if (i + 1) % 4000 == 0:
                print(f"  {i + 1:,}/{len(dirs):,} dirs  "
                      f"{len(out):,} usable  {time.time() - t0:.0f}s",
                      flush=True)
    out.sort(key=lambda r: r["start_ms"])
    print(f"cache: {len(out):,} windows of {len(dirs):,} dirs in "
          f"{time.time() - t0:.0f}s")
    ys = np.array([r["y"] for r in out])
    if len(ys):
        first = min(r["start_ms"] for r in out)
        last = max(r["start_ms"] for r in out)
        print(f"  {time.strftime('%Y-%m-%d', time.gmtime(first / 1000))} .. "
              f"{time.strftime('%Y-%m-%d', time.gmtime(last / 1000))} | "
              f"Up rate {ys.mean():.4f} | with Chainlink "
              f"{sum(1 for r in out if r['strike'] is not None):,}")
    return {"rows": out}


# ---------------------------------------------------------------------------
# bars + causal contexts
# ---------------------------------------------------------------------------

def to_bars(close: np.ndarray, vol: np.ndarray, taker: np.ndarray,
            n_bars: int = WINDOW_S // BAR_S) -> tuple:
    """Aggregate the 1s tape into n_bars 1m bars: (closes, vols, takers)."""
    n = len(close)
    step = n / n_bars
    c, v, k = [], [], []
    for b in range(n_bars):
        lo, hi = int(round(b * step)), int(round((b + 1) * step))
        hi = min(max(hi, lo + 1), n)
        if lo >= n:
            break
        c.append(float(close[hi - 1]))
        v.append(float(vol[lo:hi].sum()))
        k.append(float(taker[lo:hi].sum()))
    return c, v, k


MAX_BARS = 2000          # PriceHistory.MAX_BARS -- the live cap


def build_series(rows: list[dict]) -> tuple:
    """Stitch every window's tape into one continuous 1m bar series.

    Live holds up to 2000 completed 1m bars and hands the models all of them.
    Windows are contiguous 5-minute slices, so concatenating their bars
    reproduces exactly what the bot's PriceHistory would have held. Returns
    (closes, vols, takers, bar0, sigma) where ``bar0[w]`` is the number of
    completed bars before window ``w`` starts and ``sigma[w]`` is the
    VolEstimator's sigma_1m as of that window, fed strictly past data.
    """
    vol = VolEstimator()
    gclose: list[float] = []
    gvol: list[float] = []
    gtaker: list[float] = []
    bar0 = np.zeros(len(rows), dtype=np.int64)
    sigma = np.zeros(len(rows))
    prev_close = None
    day_seen = None
    gaps = 0
    for i, r in enumerate(rows):
        c, v, k = to_bars(r["close"], r["vol"], r["taker"])
        if i and r["start_ms"] - rows[i - 1]["start_ms"] != WINDOW_S * 1000:
            gaps += 1
        ts = r["start_ms"] / 1000.0
        day = int(ts // 86400)
        if day != day_seen:
            vol._recompute_seasonality()
            day_seen = day
        bar0[i] = len(gclose)
        for j, cl in enumerate(c):
            if prev_close is not None and prev_close > 0:
                vol.add(math.log(cl / prev_close),
                        hour=int(((ts + j * BAR_S) // 3600) % 24))
            prev_close = cl
        gclose.extend(c)
        gvol.extend(v)
        gtaker.extend(k)
        sigma[i] = vol.sigma_1m(hour=int((ts // 3600) % 24))
    print(f"  series: {len(gclose):,} 1m bars | {gaps} inter-window gaps")
    return (np.array(gclose), np.array(gvol), np.array(gtaker), bar0, sigma)


def make_ctx(r: dict, series: tuple, b0: int, sigma: float,
             tau: float, quote_up: float | None = None,
             quote_down: float | None = None) -> ModelContext:
    """Rebuild the live ModelContext for `tau` seconds left in this window.

    ``quote_up``/``quote_down`` are only supplied for the priced windows, so
    the market-anchored model (``calibrated``) falls back to the closed-form
    digital everywhere else -- which is exactly what live does.
    """
    gclose, gvol, gtaker, _b0, _s = series
    close = r["close"]
    elapsed = max(1, int(WINDOW_S - tau))
    idx = min(elapsed, len(close)) - 1
    price = float(close[idx])
    done = max(0, elapsed // BAR_S)
    total = b0 + done
    lo = max(0, total - MAX_BARS)
    return ModelContext(closes=gclose[lo:total].tolist() + [price],
                        volumes=gvol[lo:total].tolist(),
                        taker_buys=gtaker[lo:total].tolist(),
                        price=price,
                        strike=float(r["strike"]) if r["strike"] else None,
                        tau_s=tau, sigma_1m=sigma,
                        quote_up=quote_up, quote_down=quote_down)


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def logloss(p: np.ndarray, y: np.ndarray) -> float:
    q = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


def day_stats(p: np.ndarray, y: np.ndarray, day: np.ndarray,
              base_brier: float) -> tuple[float, float]:
    """(mean per-day Brier skill vs base, day-clustered t)."""
    df = pd.DataFrame({"p": p, "y": y, "d": day})
    g = df.groupby("d").apply(
        lambda x: 1.0 - brier(x.p.to_numpy(), x.y.to_numpy()) / base_brier,
        include_groups=False)
    v = g.to_numpy()
    if len(v) < 2 or v.std(ddof=1) == 0:
        return float(v.mean()), 0.0
    return float(v.mean()), float(v.mean() / (v.std(ddof=1) / math.sqrt(len(v))))


# Module-level shard state. The parent fills this BEFORE forking, so the 57 MB
# corpus is inherited copy-on-write instead of being pickled to every worker.
_G: dict = {}


def _work(chunk: list[int]) -> dict:
    """Evaluate every model at every tau for one shard of usable positions."""
    rows, series = _G["rows"], _G["series"]
    bar0, sig, taus = _G["bar0"], _G["sig"], _G["taus"]
    usable, models = _G["usable"], _G["models"]
    quotes, epos = _G.get("quotes"), _G.get("epos")
    out: dict[str, dict] = {n: {"p": [], "e": [], "i": [], "t": []}
                            for n in models}
    for pos in chunk:
        i = usable[pos]
        r = rows[i]
        for tau in taus:
            qu = qd = None
            if quotes is not None:
                q = quotes.get((epos[pos], tau))
                if q is not None:
                    qu, qd = q
            ctx = make_ctx(r, series, int(bar0[i]), float(sig[i]), tau, qu, qd)
            for name, mdl in models.items():
                pr = mdl.predict(ctx)
                acc = out[name]
                acc["p"].append(pr.p_up)
                acc["e"].append(bool(pr.emitted))
                acc["i"].append(pos)
                acc["t"].append(tau)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("cache", "eval"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                    help="eval shards; contexts are independent per window")
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--taus", nargs="*", type=float, default=list(TAU_GRID))
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--out", default=None,
                    help="eval: save predictions to this pickle")
    ap.add_argument("--by-tau", action="store_true",
                    help="eval: one row per (model, tau) instead of pooled")
    ap.add_argument("--prices", default=None,
                    help="eval: inject market quotes from this parquet so the "
                         "market-anchored model can be tested")
    args = ap.parse_args()

    if args.cmd == "cache":
        blob = build_cache(args.limit, args.workers)
        with open(args.cache, "wb") as fh:
            pickle.dump(blob, fh, protocol=4)
        print(f"wrote {args.cache} ({os.path.getsize(args.cache) / 1e6:.0f} MB)")
        return

    with open(args.cache, "rb") as fh:
        rows = pickle.load(fh)["rows"]
    print(f"corpus: {len(rows):,} windows, "
          f"{len({r['start_ms'] // 86400000 for r in rows})} days\n")

    names = list(MODEL_NAMES) if not args.models else args.models
    for n in names:
        if n not in MODEL_NAMES:
            raise SystemExit(f"unknown model {n}; choose from {MODEL_NAMES}")
    models = {n: build_model(n) for n in names}
    print("scoring (contexts rebuilt per window x tau; this takes a while)...")
    t0 = time.time()
    series = build_series(rows)
    bar0, sig = series[3], series[4]
    day = np.array([r["start_ms"] // 86400000 for r in rows])
    y = np.array([r["y"] for r in rows])
    print(f"  bars+sigma in {time.time() - t0:.0f}s\n")

    usable = [i for i, r in enumerate(rows) if r["strike"] is not None]
    epos = [int(rows[i]["start_ms"] // 1000) for i in usable]
    quotes = None
    if args.prices:
        px = pd.read_parquet(args.prices,
                             columns=["window_id", "tau_s", "up_px", "dn_px"])
        px["epoch"] = px.window_id.str.split("/").str[1].astype(int)
        quotes = {}
        for e, g in px.groupby("epoch"):
            for tau, u, d in zip(g.tau_s.to_numpy(), g.up_px.to_numpy(),
                                 g.dn_px.to_numpy()):
                if np.isfinite(u) and np.isfinite(d):
                    quotes[(int(e), float(tau))] = (float(u), float(d))
        print(f"  injected {len(quotes):,} market quotes over "
              f"{px.epoch.nunique():,} windows")
    _G.update(rows=rows, series=series, bar0=bar0, sig=sig, taus=list(args.taus),
              models=models, usable=usable, quotes=quotes, epos=epos)
    step = max(1, len(usable) // (args.jobs * 4) or 1)
    chunks = [list(range(i, min(i + step, len(usable))))
              for i in range(0, len(usable), step)]
    results: dict[str, dict] = {n: {"p": [], "e": [], "i": [], "t": []}
                                for n in names}
    done = 0
    if args.jobs > 1:
        from multiprocessing import get_context
        with get_context("fork").Pool(args.jobs) as pool:
            for out in pool.imap_unordered(_work, chunks):
                for n in names:
                    for k in ("p", "e", "i", "t"):
                        results[n][k].extend(out[n][k])
                done += 1
                if done % max(1, len(chunks) // 8) == 0:
                    print(f"  {done}/{len(chunks)} shards  "
                          f"{time.time() - t0:.0f}s", flush=True)
    else:
        out = _work(list(range(len(usable))))
        for n in names:
            for k in ("p", "e", "i", "t"):
                results[n][k].extend(out[n][k])
    print(f"  scored in {time.time() - t0:.0f}s\n")

    if args.out:
        with open(args.out, "wb") as fh:
            pickle.dump({"results": results, "usable": usable,
                         "day": day, "y": y, "taus": list(args.taus)}, fh,
                        protocol=4)
        print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.0f} MB)\n")

    base = float(y.mean())
    base_b = base * (1 - base)      # Brier of the constant base-rate forecast
    uy = y[usable]
    uday = day[usable]
    half = uday < np.median(uday)
    print(f"\nbase rate Up {base:.4f} | baseline Brier {base_b:.4f} | "
          f"{len(usable):,} windows x {len(args.taus)} taus\n")

    def row(name: str, mask: np.ndarray, keep: np.ndarray) -> str:
        pv, yv = p[keep & mask], uy[ix[keep & mask]]
        if not len(pv):
            return f"  {name:<14}{'-':>12}"
        dv, hv = uday[ix[keep & mask]], half[ix[keep & mask]]
        b = brier(pv, yv)
        skill, dt = day_stats(pv, yv, dv, base_b)
        s1 = (1 - brier(pv[hv], yv[hv]) / base_b) if hv.any() else float("nan")
        s2 = (1 - brier(pv[~hv], yv[~hv]) / base_b) if (~hv).any() else float("nan")
        return (f"  {name:<14}{len(pv):>12,}{keep[mask].mean():>7.0%}{b:>9.4f}"
                f"{skill:>8.3f}{dt:>8.2f}{s1:>8.3f}{s2:>8.3f}")

    hdr = (f"  {'model':<14}{'n':>12}{'emit':>7}{'Brier':>9}{'skill':>8}"
           f"{'day-t':>8}{'h1':>8}{'h2':>8}")
    for name in names:
        a = results[name]
        p = np.array(a["p"]); e = np.array(a["e"]); ix = np.array(a["i"])
        t = np.array(a["t"])
        if not len(p):
            print(f"  {name:<14}   (no decisions)")
            continue
        # Models that hold return p=0.5 with emitted=False; score their views
        # only, and always report the emit rate so "wins by never trading" is
        # visible.
        keep = e if e.any() else np.ones(len(p), bool)
        if not args.by_tau:
            if name == names[0]:
                print(hdr)
                print("  " + "-" * (len(hdr) - 2))
            print(row(name, np.ones(len(p), bool), keep))
            continue
        print(f"--- {name} (emit {e.mean():.0%} overall) ---")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for tau in args.taus:
            print(row(name, t == tau, keep))
        print()


if __name__ == "__main__":
    main()
