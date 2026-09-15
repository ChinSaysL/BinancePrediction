"""Read out the WARM-QUOTE checkpoint, and measure what the warm quote costs.

FINDINGS.md shipped the warm quote on 2026-09-15 with an explicit CHECKPOINT:

    after ~20 fires, compare the venue order's `createTime` minus the CROSS
    decision log against the 169-220ms cold baseline -- a warm hit should print
    ~125-150ms. The log marks the hits (`warm quote used for <slug> <side>`).

46 warm hits exist, so this is due. It reads decision -> venue ack from the log
(a lag over createTime, but consistent across fires, so the comparison holds).

The second half is the part nobody checked: the venue anchors the FOK bound to
the QUOTE we submit (`bound = quote x (1 + slip/10000)`), and the warm quote is
older than the fire. The `shadow quote` line is a FRESH quote fetched
concurrently, so warm_px - fresh_px is the price error we hand to the bound.
If the warm quote is stale HIGH the bound is too tight to fill; stale LOW and
the bound is loose, which is the toxic direction.

    .venv/bin/python tools/warm_quote_audit.py --log data/live.log
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import statistics


def ts(s: str, ms: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=dt.timezone.utc).timestamp() + int(ms) / 1000.0


P_CROSS = re.compile(
    r"^(\S+ \S+),(\d+) .*?CROSS (\S+) (\S+) sweep_px=([\d.]+) .*?tau (\d+)s",
    re.M)
P_WARM = re.compile(
    r"^(\S+ \S+),(\d+) .*?warm quote used for (\S+) (\S+): age ([\d.]+)s px ([\d.]+)",
    re.M)
P_SHADOW = re.compile(
    r"^(\S+ \S+),(\d+) .*?shadow quote (\S+) (\S+): fresh px ([\d.]+) after (\d+)ms",
    re.M)
P_ACK = re.compile(
    r"^(\S+ \S+),(\d+) .*?venue ack (\S+) (\S+): (\d+)ms, quote ([\d.]+) slip (\d+)"
    r" -> bound ([\d.]+), ceiling ([\d.]+)", re.M)
P_EXEC = re.compile(
    r"^(\S+ \S+),(\d+) .*?cross exec (\S+) (\S+): decided ([\d.]+) limit ([\d.]+)"
    r" -> (\w+ ?\w*) in ([\d.]+)s \| committed \$([\d.]+) of", re.M)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/live.log")
    args = ap.parse_args()
    text = open(args.log, errors="ignore").read()

    fires = {}
    for m in P_CROSS.finditer(text):
        k = (m.group(3), m.group(4))
        fires.setdefault(k, {"slug": m.group(3), "side": m.group(4),
                             "t": ts(m.group(1), m.group(2)),
                             "sweep": float(m.group(5)), "tau": int(m.group(6))})
    for m in P_WARM.finditer(text):
        k = (m.group(3), m.group(4))
        if k in fires and "warm_age" not in fires[k] and \
                ts(m.group(1), m.group(2)) >= fires[k]["t"] - 1:
            f = fires[k]
            f["warm_age"] = float(m.group(5))
            f["warm_px"] = float(m.group(6))
            f["warm_t"] = ts(m.group(1), m.group(2))
    for m in P_SHADOW.finditer(text):
        k = (m.group(3), m.group(4))
        if k in fires and "fresh_px" not in fires[k]:
            f = fires[k]
            f["fresh_px"] = float(m.group(5))
            f["fresh_ms"] = int(m.group(6))
    for m in P_ACK.finditer(text):
        k = (m.group(3), m.group(4))
        if k in fires and "ack_t" not in fires[k]:
            f = fires[k]
            f["ack_t"] = ts(m.group(1), m.group(2))
            f["ack_ms"] = int(m.group(5))
            f["ack_px"] = float(m.group(6))
            f["bound"] = float(m.group(8))
            f["ceiling"] = float(m.group(9))
    for m in P_EXEC.finditer(text):
        k = (m.group(3), m.group(4))
        if k in fires:
            f = fires[k]
            f["outcome"] = m.group(7).strip()
            f["committed"] = float(m.group(9))

    ok = [f for f in fires.values() if "ack_t" in f]
    warm = [f for f in ok if "warm_px" in f]
    cold = [f for f in ok if "warm_px" not in f]
    print(f"fires with a CROSS decision: {len(fires)}   with an ack: {len(ok)}")
    print(f"warm hit: {len(warm)}   no warm hit: {len(cold)}\n")

    def line(name, rows, key):
        v = [f[key] for f in rows if key in f]
        if not v:
            return
        print(f"  {name:14s} n={len(v):3d}  p50 {statistics.median(v):6.0f}  "
              f"min {min(v):5.0f}  max {max(v):6.0f}")

    for f in ok:
        f["delta"] = (f["ack_t"] - f["t"]) * 1000.0
    print("CHECKPOINT -- decision -> venue ack (ms), warm vs the cold baseline")
    line("WARM", warm, "delta")
    line("COLD", cold, "delta")
    line("WARM ack only", warm, "ack_ms")
    line("COLD ack only", cold, "ack_ms")
    print("  FINDINGS expected a warm hit at ~125-150ms vs 169-220ms cold.\n")

    print("WHAT THE WARM QUOTE COSTS AT THE BOUND")
    pairs = [f for f in ok if "warm_px" in f and "fresh_px" in f]
    if pairs:
        d = [(f["warm_px"] - f["fresh_px"]) * 100 for f in pairs]
        print(f"  warm_px - fresh_px (cents), n={len(d)}: "
              f"median {statistics.median(d):+.2f}  mean {statistics.fmean(d):+.2f}"
              f"  min {min(d):+.2f}  max {max(d):+.2f}")
        print(f"  warm quote is STALE HIGH on {sum(1 for x in d if x > 0)}"
              f", equal on {sum(1 for x in d if x == 0)}"
              f", stale LOW on {sum(1 for x in d if x < 0)}")
        ages = [f["warm_age"] for f in pairs if "warm_age" in f]
        if ages:
            print(f"  warm age (s): median {statistics.median(ages):.2f}"
                  f"  max {max(ages):.2f}")
        print("  A stale-HIGH warm quote tightens the bound; stale LOW loosens it"
              " (the toxic direction).")

    print("\nOUTCOME BY WARM HIT")
    for name, rows in (("warm", warm), ("cold", cold)):
        if not rows:
            continue
        fills = [f for f in rows if f.get("committed", 0) > 0]
        outs = {}
        for f in rows:
            o = f.get("outcome", "?")
            outs[o] = outs.get(o, 0) + 1
        print(f"  {name:5s} n={len(rows):3d}  committed>0: {len(fills):3d}"
              f"   outcomes: {outs}")

    print("\nFIRES WHERE THE WARM QUOTE WAS AT/ABOVE THE CEILING")
    bad = [f for f in ok if "warm_px" in f and f["warm_px"] >= f["ceiling"] - 1e-9]
    for f in bad:
        print(f"  {f['slug']} {f['side']}: warm {f['warm_px']:.4f} age "
              f"{f.get('warm_age', 0):.2f}s vs ceiling {f['ceiling']:.4f}"
              f" -> ack {f.get('ack_ms')}ms  outcome {f.get('outcome', '?')}")
    if not bad:
        print("  none")


if __name__ == "__main__":
    main()
