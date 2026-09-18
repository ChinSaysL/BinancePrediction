"""WHY THE TWO VENUES DISAGREE — decomposed on 53 days, not 48 rounds.

THE OPEN QUESTION. The same slug exists on the Binance wrapper and on Polymarket,
and they do not always pay the same: a 48-round comparison measured **10.4%
disagreement**, and the mechanism test on the five differing rounds explained 3 of
5 (near-strike settlements) and left 2 unexplained ("do not claim it is solved").
Two candidate causes, and they have never been separated:

    RULE    the wrapper pays on a POINT (`endPrice` vs `startPrice`); Polymarket
            pays on a Chainlink 60 s TWAP vs the price at the START.
    SOURCE  the wrapper's tape is BINANCE spot; Polymarket's is CHAINLINK. The
            boundary values of the two feeds differ -- measured on this corpus at
            **median +4.14 bps, p90 +15.30, |gap| > 2 bps on 70.4% of rounds**.

`data/btc5m` is the only place both halves exist: 53 days / 15,264 rounds, each
with `priceToBeat` + `finalPrice` (CHAINLINK, and the venue's own on-chain label)
and `ticks_dense.parquet` (BINANCE, 1 s). So the decomposition is computable:

    pm_label        finalPrice >= priceToBeat            (Chainlink, the real bet)
    bin_point       close[-1]  >= open[0]               (wrapper's rule AND wrapper's source)
    bin_twap60      mean(close[-60:]) >= open[0]        (PM's rule, BINANCE source)

    TOTAL   = bin_point != pm_label     (what "the two venues disagree" means)
    RULE    = bin_point != bin_twap60   (same source: isolates the rule)
    SOURCE  = bin_twap60 != pm_label    (same rule, different source)
A TOTAL far above RULE+SOURCE means the two effects interact -- i.e. the honest
answer is "the sources differ, so the rules are not interchangeable".

Also measures the repo's ONE UNMEASURED CONVENTION: whether the 60 s window is
`T-60..T-1` (per-second closes, what `settles_up` documents) or `T-59..T`
(what `pm_rule_ident.py`'s `px[-60:]` counts). A 1-bar (1.7%) difference in the
average, flagged in `pricing.py` as never measured. Measured here.

Usage: python3 tools/pm_rule_source_decomp.py
"""
from __future__ import annotations

import collections
import json
import pathlib
import sys

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "btc5m"


def rounds():
    for day in sorted((CORPUS / "data").iterdir()):
        if not day.is_dir():
            continue
        for r in sorted(day.iterdir()):
            if r.is_dir() and (r / "metadata.json").exists():
                yield day.name, r


def main() -> None:
    rows = []
    bad_meta = 0
    for day, r in rounds():
        try:
            meta = json.loads((r / "metadata.json").read_text())
        except Exception:
            bad_meta += 1
            continue
        ev = meta.get("events", [{}])[0].get("eventMetadata", {})
        K, F = ev.get("priceToBeat"), ev.get("finalPrice")
        t = r / "ticks_dense.parquet"
        if K is None or F is None or not t.exists():
            continue
        try:
            d = pd.read_parquet(t, columns=["timestamp", "open", "close"])
        except Exception:
            continue
        if len(d) < 200:
            continue
        d = d.sort_values("timestamp")
        o, c = d["open"].to_numpy(dtype=float), d["close"].to_numpy(dtype=float)
        if len(c) < 61 or not np.isfinite(o[0]) or o[0] <= 0:
            continue
        s0 = float(o[0])
        Kc = float(K)
        pm = 1 if float(F) >= Kc else 0
        bin_point = 1 if float(c[-1]) >= s0 else 0
        twap_a = 1 if float(np.mean(c[-60:])) >= s0 else 0        # T-60..T-1
        twap_b = 1 if float(np.mean(c[-59:])) >= s0 else 0        # T-59..T  (px[-60:])
        # the SAME two windows, but against the CHAINLINK reference -- this is
        # what `terminal_rule()`/`pm_rule_ident.py` fit, so the 2x2 separates the
        # REFERENCE from the WINDOW.
        ch_point = 1 if float(c[-1]) >= Kc else 0
        ch_twap = 1 if float(np.mean(c[-60:])) >= Kc else 0
        # the venue's own secondary label, where present
        op = meta.get("outcomePrices")
        try:
            op = json.loads(op) if isinstance(op, str) else op
            lab = 1 if (op and float(op[0]) == 1.0) else None
        except Exception:
            lab = None
        rows.append(dict(day=day, pm=pm, bin_point=bin_point, twap_a=twap_a,
                         twap_b=twap_b, label=lab, ch_point=ch_point,
                         ch_twap=ch_twap,
                         gap_bps=1e4 * (s0 - float(K)) / float(K),
                         move_bps=1e4 * (float(c[-1]) - s0) / s0))
    d = pd.DataFrame(rows)
    if d.empty:
        print("no rounds"); return
    n = len(d)
    print(f"=== {n} corpus rounds over {d.day.nunique()} days "
          f"(metadata unusable: {bad_meta}) ===")
    chk = d[d.label.notna()]
    if len(chk):
        print(f"    label cross-check vs outcomePrices: "
              f"{(chk.label == chk.pm).mean():.2%} agree on {len(chk)} rounds")

    tot = float((d.bin_point != d.pm).mean())
    rule = float((d.bin_point != d.twap_a).mean())
    src = float((d.twap_a != d.pm).mean())
    print(f"\n--- THE DECOMPOSITION ---")
    print(f"    TOTAL   wrapper point (Binance) vs the venue's label   {tot:6.2%}")
    print(f"    RULE    point vs 60s TWAP, both on BINANCE             {rule:6.2%}")
    print(f"    SOURCE  60s TWAP on Binance vs the venue's label       {src:6.2%}")
    print(f"    (the old 48-round comparison gave 10.4% total)")
    print(f"    interaction = TOTAL - (RULE + SOURCE as independent) = "
          f"{tot - (rule + src - 2 * rule * src):+.2%}")

    print(f"\n--- the 1-bar window convention (pricing.py flags this as never measured) ---")
    print(f"    T-60..T-1 vs T-59..T disagree on {(d.twap_a != d.twap_b).mean():.2%} "
          f"of rounds ({(d.twap_a != d.twap_b).sum()} rounds)")

    print(f"\n--- THE 2x2 THAT SEPARATES REFERENCE FROM WINDOW ---")
    print("    each cell: agreement with the venue's own label (finalPrice vs priceToBeat)")
    print(f"    {'':>22} {'END = point close[T-1]':>24} {'END = 60s mean':>18}")
    for ref, pcol, tcol in (("REF = Binance start", "bin_point", "twap_a"),
                            ("REF = Chainlink priceToBeat", "ch_point", "ch_twap")):
        print(f"    {ref:<22} {(d[pcol] == d.pm).mean():>24.2%} "
              f"{(d[tcol] == d.pm).mean():>18.2%}")
    print("    -> if the REFERENCE rows differ far more than the WINDOW columns,")
    print("       then `terminal_rule()`'s TAIL-vs-point gap is a REFERENCE effect")
    print("       (Chainlink vs Binance at the boundary), not a window effect.")

    print(f"\n--- WHERE IT HAPPENS (|move| of the round, in bps) ---")
    print(f"    {'|move| bps':>12} {'n':>6} {'TOTAL':>8} {'RULE':>8} {'SOURCE':>8}")
    mv = d.move_bps.abs().to_numpy()
    for lo, hi in [(0, 1), (1, 2), (2, 5), (5, 10), (10, 1e9)]:
        m = (mv >= lo) & (mv < hi)
        if m.sum() < 30:
            continue
        print(f"    {lo:>4}-{hi if hi < 1e8 else float('inf'):<7} {int(m.sum()):>6d} "
              f"{(d.bin_point[m] != d.pm[m]).mean():>8.2%} "
              f"{(d.bin_point[m] != d.twap_a[m]).mean():>8.2%} "
              f"{(d.twap_a[m] != d.pm[m]).mean():>8.2%}")

    print(f"\n--- AND BY THE SOURCE GAP ITSELF (Binance start vs Chainlink strike) ---")
    print(f"    {'|gap| bps':>12} {'n':>6} {'TOTAL':>8} {'SOURCE':>8}")
    gp = d.gap_bps.abs().to_numpy()
    for lo, hi in [(0, 1), (1, 2), (2, 5), (5, 10), (10, 1e9)]:
        m = (gp >= lo) & (gp < hi)
        if m.sum() < 30:
            continue
        print(f"    {lo:>4}-{hi if hi < 1e8 else float('inf'):<7} {int(m.sum()):>6d} "
              f"{(d.bin_point[m] != d.pm[m]).mean():>8.2%} "
              f"{(d.twap_a[m] != d.pm[m]).mean():>8.2%}")

    print(f"\n--- IMPLICATION FOR A BINANCE-FED MODEL ---")
    print(f"    A model that prices Binance spot against a Binance strike is betting")
    print(f"    on `bin_twap60`, and the venue pays `pm_label`: they differ on")
    print(f"    **{src:.2%}** of rounds. That is the irreducible error floor for this")
    print(f"    approach -- no amount of calibration removes it, because it is the")
    print(f"    settle-rule/source mismatch itself, and it is measured, not assumed.")


if __name__ == "__main__":
    main()
