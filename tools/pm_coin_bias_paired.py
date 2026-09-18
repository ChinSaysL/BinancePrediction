"""Is there a per-coin BIAS in our forecast of the Chainlink settle?

SUPERSEDES tools/pm_coin_bias.py, which had three defects that flattered the
result:

 1. MIXED POPULATIONS. It measured accuracy over ALL rounds but money over only
    the rounds where BOTH sides quote above zero. Those are different rounds --
    the second set is the CONTESTED ones, where the forecast is materially
    worse, and conflating them implied an edge that is not there. Both numbers
    are now reported separately and labelled, and the paired test runs on the
    tradable set, because that is the set you can actually buy.
 2. NO TEST. A 2.6 pp accuracy "gain" on 195 rounds is 2-3 round flips. Because
    two offsets are scored on the SAME rounds, the correct test is PAIRED, not
    two independent proportions: McNemar on the discordant pairs, reported as z.
 3. BOUNDARY. The venue rule is `mean >= K -> Up`, so the forecast's own
    comparison must be `>= 0`, not `> 0`.

THE CLAIM UNDER TEST. "Use a different formula per coin." That only helps if
there is a per-coin systematic error -- e.g. Binance's BNB book sits above
Chainlink's BNB print by a few bps, so a constant offset before comparing to
the strike would flip borderline rounds the right way.

The SHAPE is the whole answer: a real bias shows a PEAK at a non-zero offset,
in both halves; no bias shows a flat line whose argmax wanders.

Usage (on the VPS, analysis venv):
    /root/analysis-venv/bin/python tools/pm_coin_bias_paired.py --tau 10
"""
from __future__ import annotations

import argparse
import importlib.util
import math
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CEIL = os.path.join(HERE, "pm_accuracy_ceiling.py")
FEE = lambda p: 0.07 * p * (1 - p)                             # noqa: E731
COINS = ("btc", "eth", "bnb", "sol", "xrp", "doge")
DELTAS = tuple(round(x * 0.5, 1) for x in range(-20, 21))       # -10 .. +10 bps
LO, HI = 0.001, 0.999


def _mod(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def tradable(d: pd.DataFrame) -> pd.DataFrame:
    return d[(d.up_ask > LO) & (d.dn_ask > HI * 0 + LO) & (d.up_ask < HI)
             & (d.dn_ask < HI)]


def stats(d: pd.DataFrame, delta: float) -> dict:
    """Everything at one offset, with the two populations kept separate."""
    if d.empty:
        return {}
    all_up = (d.d_fc + delta).to_numpy() >= 0.0
    right_all = all_up == (d.winner == "Up").to_numpy()
    tr = d[(d.up_ask > LO) & (d.dn_ask > LO)]
    out = dict(n=len(d), acc_all=float(right_all.mean()), n_tr=len(tr),
               acc_tr=float("nan"), ask=float("nan"), be=float("nan"),
               per=float("nan"))
    if len(tr) < 4:
        return out
    u = (tr.d_fc + delta).to_numpy() >= 0.0
    ask = np.where(u, tr.up_ask.to_numpy(), tr.dn_ask.to_numpy())
    won = np.where(u, (tr.winner == "Up").to_numpy(),
                   (tr.winner == "Down").to_numpy())
    be = ask + np.array([FEE(x) for x in ask])
    out.update(acc_tr=float(won.mean()), ask=float(ask.mean()),
               be=float(be.mean()), per=float((won - be).mean()))
    return out


def mcnemar(d: pd.DataFrame, off: float) -> tuple[int, int, float]:
    """Discordant pairs vs offset 0, on the tradable set. The paired test."""
    tr = d[(d.up_ask > LO) & (d.dn_ask > LO)].reset_index(drop=True)
    if len(tr) < 8:
        return 0, 0, 0.0
    win = (tr.winner == "Up").to_numpy()
    ra = ((tr.d_fc + off).to_numpy() >= 0.0) == win
    rb = (tr.d_fc.to_numpy() >= 0.0) == win
    b = int((ra & ~rb).sum())        # the offset is right where 0 was wrong
    c = int((~ra & rb).sum())        # 0 is right where the offset is wrong
    z = (b - c) / math.sqrt(b + c) if (b + c) else 0.0
    return b, c, z


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau", type=float, default=10.0)
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    ceil = _mod("ceil", CEIL)
    df = ceil.load(a.db or ceil.DB)
    df = df[df.slug.str.contains("-5m-")]
    pr = ceil.per_round(df, a.tau)
    if pr.empty:
        print("no usable rows"); return 1

    print(f"=== PER-COIN BIAS SWEEP, tau {a.tau:.0f}s, {len(pr)} rounds ===")
    print("    acc_all  = all rounds.")
    print("    acc_tr   = rounds with a two-sided book (contested, weaker, and the")
    print("               only ones you can buy). argmax is taken on acc_tr.\n")

    grid = (-4.0, -3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 3.0)
    print(f"  {'coin':>5} {'n':>4} {'trad':>5} " + " ".join(f"{x:>7.1f}" for x in grid))
    for c in ("ALL",) + COINS:
        d = pr if c == "ALL" else pr[pr.coin == c]
        if len(d) < 40:
            print(f"  {c:>5} {len(d):>4}  (thin)"); continue
        ntr = len(d[(d.up_ask > LO) & (d.dn_ask > LO)])
        cells = " ".join(
            f"{(stats(d, x)['acc_tr']):>7.3%}"
            if np.isfinite(stats(d, x)["acc_tr"]) else f"{'--':>7}" for x in grid)
        print(f"  {c:>5} {len(d):>4} {ntr:>5} {cells}")
    print("  (that is acc_tr by offset in bps; the PEAK is what matters)\n")

    print("=== VERDICT: is the best offset real? paired McNemar, same rounds ===")
    print(f"  {'coin':>5} {'argmax':>7} {'acc_tr@1':>9} {'acc_tr@0':>9} {'gain':>7} "
          f"{'b':>4} {'c':>4} {'z':>6}  call")
    keep = []
    for c in ("ALL",) + COINS:
        d = pr if c == "ALL" else pr[pr.coin == c]
        if len(d) < 40:
            continue
        if len(d[(d.up_ask > LO) & (d.dn_ask > LO)]) < 20:
            print(f"  {c:>5}   too few tradable rounds"); continue
        cand = [x for x in DELTAS if abs(x) <= 6]
        base = stats(d, 0.0)
        best = max(cand, key=lambda x: (stats(d, x)["acc_tr"]
                                        if np.isfinite(stats(d, x)["acc_tr"]) else -1))
        bs = stats(d, best)
        gain = bs["acc_tr"] - base["acc_tr"]
        b, cc, z = mcnemar(d, best)
        call = "REAL" if abs(z) >= 2.0 else "weak" if abs(z) >= 1.0 else "noise"
        print(f"  {c:>5} {best:>+7.1f} {bs['acc_tr']:>9.3%} {base['acc_tr']:>9.3%} "
              f"{gain:>+7.2%} {b:>4d} {cc:>4d} {z:>+6.2f}  {call}")
        if c != "ALL" and abs(z) >= 2.0 and gain > 0:
            keep.append((c, best, z, bs["per"] - base["per"]))

    print(f"\n=== offsets surviving |z| >= 2: {len(keep)} ===")
    for c, off, z, dper in keep:
        print(f"  {c} at {off:+.1f} bps  z={z:+.2f}  per-share gain {dper:+.4f}")
    if not keep:
        print("  none. Every coin's best offset sits inside its own sample noise.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
