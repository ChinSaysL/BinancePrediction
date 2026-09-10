"""Tune the drift prototype so P(Up) stops over-promising.

The finding: on the signals the gate acts on, Up claims ~0.76 and delivers
~0.56, while Down claims ~0.82 and delivers ~0.92. Both symptoms are one
monotone bias -- our P(Up) is too high -- and every recorded day shows it.

`drift_digital` was built for exactly this ("drift_scale ... The scale IS the
haircut, applied to the drift instead of to the edge"), it is live, and it is
under-correcting at 0.5. This sweeps its parameters and scores the thing that
matters: Up-side CALIBRATION, on a train/test split, not P&L.

Usage:  python -m tools.drift_sweep
"""
from __future__ import annotations

import statistics as st

from btcpredict.config import settings
from btcpredict.models import build_model
from btcpredict.models.drift_digital import DriftDigitalModel
from btcpredict.store.db import Store
from tools.backtest_candle_open import (ReplayParams, build_1m_history, replay,
                                        rolling_sigma)

MARKET = "btc-updown-5m"


def load_market():
    store = Store(path=settings.db_path)
    like = MARKET + "%"
    settled = store.query(
        "SELECT slug, strike, end_price, winner, start_ms FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL AND end_price IS NOT NULL "
        "AND slug LIKE ? ORDER BY start_ms", (like,))
    quotes = store.query("""
        SELECT q.* FROM quotes q JOIN rounds r ON r.slug = q.slug
        WHERE r.winner IS NOT NULL AND q.slug LIKE ? ORDER BY q.ts_ms""", (like,))
    sbs = {r["slug"]: r for r in settled}
    sigs = {r["slug"]: rolling_sigma(settled, i) for i, r in enumerate(settled)}
    hist = build_1m_history(quotes, store=store, symbol="BTCUSDT")
    store.close()
    return quotes, sbs, sigs, hist


def stats(trades, side, lo=0.5, hi=0.95):
    s = [t for t in trades if t["side"] == side and lo <= t["p"] < hi]
    if not s:
        return None
    claimed = st.mean(t["p"] for t in s)
    real = sum(t["won"] for t in s) / len(s)
    se = (real * (1 - real) / len(s)) ** 0.5
    return {"n": len(s), "claimed": claimed, "real": real, "gap": real - claimed,
            "se": se}


def run(quotes, sbs, sigs, hist, model_up, digital):
    res = replay(quotes, sbs, sigs, hist, digital, None, False,
                 params=ReplayParams.from_settings(), model_up=model_up)
    return res["trades"]


def drift(params: dict | None) -> DriftDigitalModel:
    """None -> the model's own defaults, which is what live is running."""
    return DriftDigitalModel(params) if params else DriftDigitalModel()


def line(label, trades, split_ms=None):
    u, d = stats(trades, "Up"), stats(trades, "Down")
    pnl = sum(t["pnl"] for t in trades)
    out = f"  {label:<34}"
    if u:
        out += (f"Up n={u['n']:<4} claim={u['claimed']:.3f} real={u['real']:.3f} "
                f"gap={u['gap']:+.3f}")
    else:
        out += f"{'Up n=0':<40}"
    if d:
        out += f" | Down gap={d['gap']:+.3f} (n={d['n']})"
    out += f" | trades={len(trades):<4} pnl={pnl:+.1f}/$1"
    print(out)
    if split_ms:
        for name, sub in (("train", [t for t in trades if t["ts_ms"] < split_ms]),
                          ("test", [t for t in trades if t["ts_ms"] >= split_ms])):
            us = stats(sub, "Up")
            if us:
                sig = (f"({abs(us['gap'])/us['se']:.1f} sigma)" if us["se"] > 0
                       else "(perfect)")
                print(f"      {name:<5} Up n={us['n']:<4} claimed={us['claimed']:.3f} "
                      f"real={us['real']:.3f} gap={us['gap']:+.3f} {sig}")


def main() -> None:
    quotes, sbs, sigs, hist = load_market()
    digital = build_model("digital")
    span = [q["ts_ms"] for q in quotes]
    split = span[0] + (span[-1] - span[0]) // 2
    print(f"recording: {len(quotes)} quotes, {quotes[0]['ts_ms']}..{quotes[-1]['ts_ms']}")
    print(f"live config: pricing_model={settings.pricing_model} "
          f"up_model={settings.up_model}\n")

    print("=== LIVE CONFIG as deployed (up_model=drift_digital, scale 0.5) ===")
    base = run(quotes, sbs, sigs, hist, drift(None), digital)
    line("as deployed (DEFAULT_PARAMS)", base, split)

    print("\n=== drift_scale sweep (bars=6, blend=0.5) ===")
    for scale in (0.0, 1.0, 1.5, 2.0, 3.0):
        t = run(quotes, sbs, sigs, hist, drift({"drift_scale": scale}), digital)
        line(f"drift_scale={scale}", t, split)

    print("\n=== driftless control (up_model=digital, i.e. one model both sides) ===")
    line("digital", run(quotes, sbs, sigs, hist, digital, digital), split)


if __name__ == "__main__":
    main()
