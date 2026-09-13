"""Two-factor test: does the price tape change the cross arm's answer?

Factor 1 INSTRUMENT: 1s-kline close (a trade print) vs quote mid (bid+ask)/2,
                     both at 1s resolution, so only the instrument differs.
Factor 2 RESOLUTION: quote mid at 1s vs quote mid at full rate (~114/s),
                     so only the sampling differs.

Why it can be run at all: the free Tardis quote days (the 1st of each month)
and the 1s-kline dumps overlap, even though neither overlaps the bot's own
recorded rounds. Rounds are synthesized on 5m/15m UTC boundaries, so the
bot's `rounds` table is not needed.

Two truth modes:
  self   -- each tape defines its own strike and its own settlement. This is
            what a naive backtest does, and what produced kline 0.627 vs
            bot-feed 0.802.
  common -- strike and settlement come from the full-rate quote tape for every
            arm; only the tape the SIGNAL reads varies. Isolates signal
            fidelity from truth definition.
"""
from __future__ import annotations

import argparse
import glob
import io
import os
import sys
import zipfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.config import settings                      # noqa: E402
from tools.cross_multi_market import crosses                # noqa: E402


def crosses_vec(ts, px, strike, start_ms, end_ms, lo, hi, look_s, slack_s, maxdep, cap):
    """Vectorised twin of tools.cross_multi_market.crosses.

    The reference sample the loop version finds -- walking back from i until
    the first sample at least look_s old, bounded by look_s+slack_s -- is the
    largest q with t[q] <= t[i] - look_s, which searchsorted gives directly.
    `--verify` asserts the two agree on the 1s arms, where the loop is cheap.
    """
    m = (ts >= start_ms - 1000) & (ts <= end_ms)
    t, p = ts[m], px[m]
    if len(t) < 10:
        return []
    tau = (end_ms - t) / 1000.0
    up = p > strike
    ref = np.searchsorted(t, t - look_s * 1000.0, side="right") - 1
    ok = (
        (tau >= lo) & (tau <= hi)
        & (ref >= 0)
        & (t - t[np.clip(ref, 0, None)] <= (look_s + slack_s) * 1000.0)
        & (up != up[np.clip(ref, 0, None)])
        & (1e4 * np.abs(p - strike) / strike <= maxdep)
    )
    ok[0] = False
    idx = np.flatnonzero(ok)[:cap]
    return [(float(tau[i]), "Up" if up[i] else "Down") for i in idx]


def _to_ms(ts: np.ndarray) -> np.ndarray:
    """Binance/Tardis dumps carry us, ms or s. Normalise to ms."""
    mx = ts.max()
    if mx > 1e15:
        return ts / 1000.0
    if mx < 1e12:
        return ts * 1000.0
    return ts.astype(float)


def load_quotes(path: str) -> tuple[np.ndarray, np.ndarray]:
    df = pd.read_csv(path, usecols=["timestamp", "bid_price", "ask_price"])
    ts = _to_ms(df["timestamp"].to_numpy(dtype="float64"))
    mid = (df["bid_price"].to_numpy("float64") + df["ask_price"].to_numpy("float64")) / 2.0
    ok = np.isfinite(mid) & (mid > 0)
    ts, mid = ts[ok], mid[ok]
    o = np.argsort(ts, kind="stable")
    return ts[o], mid[o]


def load_kline(path: str) -> tuple[np.ndarray, np.ndarray]:
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, usecols=[0, 4],
                     names=["open_ms", "close"])
    ts = _to_ms(df["open_ms"].to_numpy(dtype="float64"))
    px = df["close"].to_numpy("float64")
    ok = np.isfinite(px) & (px > 0)
    return ts[ok], px[ok]


def to_1s(ts: np.ndarray, px: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Last observation in each wall-clock second, stamped at the second."""
    sec = np.floor(ts / 1000.0).astype("int64")
    last = np.empty(len(sec), dtype=bool)
    last[:-1] = sec[1:] != sec[:-1]
    last[-1] = True
    return (sec[last] * 1000).astype(float), px[last]


def px_at(ts: np.ndarray, px: np.ndarray, when_ms: float) -> float | None:
    """Last price at or before `when_ms`."""
    i = np.searchsorted(ts, when_ms, side="right") - 1
    return None if i < 0 else float(px[i])


def build_rounds(ts: np.ndarray, px: np.ndarray, minutes: int) -> list[tuple]:
    """(start_ms, end_ms, strike) on UTC boundaries covered by the tape."""
    step = minutes * 60_000
    first = int(np.ceil(ts[0] / step) * step)
    last = int(np.floor(ts[-1] / step) * step)
    out = []
    for start in range(first, last, step):
        k = px_at(ts, px, start)
        if k is not None:
            out.append((float(start), float(start + step), k))
    return out


def run_arm(sig_ts, sig_px, truth_ts, truth_px, rounds, scale: float, verify=False):
    """Signal read off (sig_ts, sig_px); settlement off (truth_ts, truth_px)."""
    lo = settings.cross_min_tau_s * scale
    hi = settings.cross_max_tau_s * scale
    wins, n = 0, 0
    for start, end, strike in rounds:
        hits = crosses_vec(sig_ts, sig_px, strike, start, end,
                       lo=lo, hi=hi, look_s=settings.cross_look_s, slack_s=2.0,
                       maxdep=settings.cross_max_depth_bps,
                       cap=settings.cross_max_per_round)
        if verify:
            ref_hits = crosses(sig_ts, sig_px, strike, start, end,
                               lo=lo, hi=hi, look_s=settings.cross_look_s, slack_s=2.0,
                               maxdep=settings.cross_max_depth_bps,
                               cap=settings.cross_max_per_round)
            assert hits == ref_hits, f"vec != loop at {start}: {hits} vs {ref_hits}"
        if not hits:
            continue
        final = px_at(truth_ts, truth_px, end)
        if final is None:
            continue
        settled = "Up" if final > strike else "Down"
        for _tau, side in hits:
            n += 1
            wins += int(side == settled)
    return n, wins


def ci(n: int, w: int) -> tuple[float, float, float]:
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    p = w / n
    se = (p * (1 - p) / n) ** 0.5
    return p, p - 1.96 * se, p + 1.96 * se


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes", default="data/bookticker")
    ap.add_argument("--klines", default="data/bars_1s/daily")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ZECUSDT")
    ap.add_argument("--minutes", type=int, default=5)
    ap.add_argument("--days", default="")
    ap.add_argument("--verify", action="store_true", help="assert vec==loop on the 1s arms")
    a = ap.parse_args()

    scale = a.minutes / 5.0
    print(f"gates: tau {settings.cross_min_tau_s*scale:.0f}-{settings.cross_max_tau_s*scale:.0f}s  "
          f"look {settings.cross_look_s}s  depth<={settings.cross_max_depth_bps}bps  "
          f"cap {settings.cross_max_per_round}/round   rounds: {a.minutes}m UTC")

    want_days = [d for d in a.days.split(",") if d]
    tot: dict[tuple[str, str], list[int]] = {}

    for sym in a.symbols.split(","):
        for qpath in sorted(glob.glob(os.path.join(a.quotes, f"{sym}-*.csv.gz"))):
            day = os.path.basename(qpath).split("-", 1)[1].replace(".csv.gz", "")
            if want_days and day not in want_days:
                continue
            kpath = os.path.join(a.klines, f"{sym}-1s-{day}.zip")
            if not os.path.exists(kpath):
                print(f"  {sym} {day}: no 1s kline, skipped")
                continue

            qts, qpx = load_quotes(qpath)
            kts, kpx = load_kline(kpath)
            q1ts, q1px = to_1s(qts, qpx)

            arms = {
                "A kline-1s   ": (kts, kpx),
                "B quote-1s   ": (q1ts, q1px),
                "C quote-full ": (qts, qpx),
            }
            print(f"\n{sym} {day}   quotes {len(qts):,} ({len(qts)/86400:.0f}/s) "
                  f"-> 1s {len(q1ts):,} | kline {len(kts):,}")

            for truth_mode in ("self", "common"):
                for lab, (sts, spx) in arms.items():
                    tts, tpx = (sts, spx) if truth_mode == "self" else (qts, qpx)
                    # strike is part of the truth definition
                    rds = build_rounds(*( (sts, spx) if truth_mode == "self" else (qts, qpx) ),
                                       minutes=a.minutes)
                    n, w = run_arm(sts, spx, tts, tpx, rds, scale,
                                   verify=a.verify and lab.strip() != 'C quote-full')
                    p, lo_, hi_ = ci(n, w)
                    print(f"   truth={truth_mode:6s} {lab} n={n:5d}  win {p:.3f} [{lo_:.3f}-{hi_:.3f}]")
                    k = (truth_mode, lab)
                    acc = tot.setdefault(k, [0, 0])
                    acc[0] += n
                    acc[1] += w

    print("\n" + "=" * 66)
    print("POOLED across every symbol/day")
    for (truth_mode, lab), (n, w) in sorted(tot.items()):
        p, lo_, hi_ = ci(n, w)
        print(f"   truth={truth_mode:6s} {lab} n={n:6d}  win {p:.3f} [{lo_:.3f}-{hi_:.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
