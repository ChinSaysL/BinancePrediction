"""The one-day read on the catch thesis: what the meter, refusals and orders say.

Run on the VPS: `cd /root/BinancePrediction && .venv/bin/python
tools/cross_catch_report.py`

Sections
  1) METERED TRIGGERS -- the book at +0.25/+0.50/+1.00s after every trigger.
     The number the catch thesis stands on: s100 - s25 (how much the venue
     reprices in the 0.75s after its first-quarter-second), and s25 vs the
     price we would quote at.
  2) REFUSED CROSSINGS -- quote (ask we saw) + the meter path, so the refused
     class can be priced at the EARLY ask instead of the late one.
  3) ORDERS -- decided / limit / fill / book-now, and the fill-honouring
     question: did the book run past the limit, and did the venue still fill?
     Bucketed by run-beyond-limit.
  4) PROBES -- rows whose decision says "cross probe" (flag-off experiment).
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from collections import defaultdict

METER = re.compile(r'cross meter (\S+) (\S+): \+0\.25s=(\S+) \+0\.50s=(\S+) \+1\.00s=(\S+)')
EXEC = re.compile(r'cross exec (\S+) (\S+): decided ([\d.]+) limit ([\d.]+) -> '
                  r'([\d.]+|NO FILL) in ([\d.]+)s.*book now (\S+)')


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> int:
    meters = []
    execs = []
    for line in open('data/live.log', errors='replace'):
        m = METER.search(line)
        if m:
            ts = line[:23]
            meters.append(dict(ts=ts, slug=m.group(1), side=m.group(2),
                               s25=fnum(m.group(3)), s50=fnum(m.group(4)),
                               s100=fnum(m.group(5))))
            continue
        e = EXEC.search(line)
        if e:
            execs.append(dict(ts=line[:23], slug=e.group(1), side=e.group(2),
                              dec=float(e.group(3)), lim=float(e.group(4)),
                              fill=None if e.group(5) == 'NO FILL' else float(e.group(5)),
                              el=float(e.group(6)), book=fnum(e.group(7))))

    con = sqlite3.connect('file:data/btcpredict.db?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    refs = list(con.execute(
        "SELECT ts_ms, slug, side, quote, reason FROM decisions "
        "WHERE reason LIKE 'cross refused:%' AND quote IS NOT NULL "
        "ORDER BY ts_ms DESC LIMIT 200"))
    probes = [e for e in execs]  # probe orders are labelled in decisions, below

    print(f"=== CATCH REPORT {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} ===")
    print(f"metered triggers: {len(meters)}   exec lines: {len(execs)}   "
          f"refusals with quote: {len(refs)}")

    print("\n1) METERED TRIGGERS (book path after the trigger)")
    if not meters:
        print("   none yet")
    else:
        bym = defaultdict(list)
        for m in meters:
            bym[(m['slug'], m['side'])].append(m)
        d = [m['s100'] - m['s25'] for m in meters
             if m['s25'] is not None and m['s100'] is not None]
        if d:
            d.sort()
            print(f"   s100 - s25: median {d[len(d)//2]:+.3f}  "
                  f"q25 {d[len(d)//4]:+.3f}  q75 {d[3*len(d)//4]:+.3f}  "
                  f"(n={len(d)})")
        for m in meters[-25:]:
            print(f"   {m['ts']} {m['slug'][-14:]:<14} {m['side']:<4} "
                  f"{m['s25']} -> {m['s50']} -> {m['s100']}")

    print("\n2) REFUSED CROSSINGS with a recorded quote (new rows only)")
    if not refs:
        print("   none yet (refusals before 2026-09-14 23:00 stored quote=None)")
    else:
        met = {(m['slug'], m['side']): m for m in meters}
        for r in refs[:20]:
            m = met.get((r['slug'], r['side']))
            path = (f"  meter {m['s25']} -> {m['s50']} -> {m['s100']}"
                    if m else "")
            print(f"   {time.strftime('%m-%d %H:%M', time.gmtime(r['ts_ms']/1000))} "
                  f"{r['slug'][-14:]:<14} {r['side']:<4} quote {r['quote']:.3f}"
                  f" | {r['reason'][14:46]}{path}")

    print("\n3) ORDERS and the fill-honouring question")
    if not execs:
        print("   none yet")
    else:
        buckets = defaultdict(lambda: [0, 0])       # run-beyond bucket -> [fills, n]
        for e in execs:
            ref = e['book'] if e['book'] is not None else None
            if ref is None:
                mm = [m for m in meters if m['slug'] == e['slug']
                      and m['side'] == e['side']]
                ref = mm[-1]['s100'] if mm else None
            run = None if ref is None else ref - e['lim']
            if run is not None:
                b = ('run < 0' if run < 0 else '0-0.1' if run < 0.1
                     else '0.1-0.2' if run < 0.2 else '0.2-0.35' if run < 0.35
                     else '> 0.35')
                buckets[b][1] += 1
                buckets[b][0] += 1 if e['fill'] is not None else 0
            fl = f"{e['fill']:.4f}" if e['fill'] else 'NO FILL'
            print(f"   {e['ts']} {e['slug'][-14:]:<14} decided {e['dec']:.3f} "
                  f"limit {e['lim']:.2f} -> {fl:<8} book-now "
                  f"{'-' if ref is None else f'{ref:.3f}':<6} "
                  f"run {'-' if run is None else f'{run:+.3f}'}")
        if buckets:
            print("   fill rate by how far the book ran past OUR limit:")
            for b in ('run < 0', '0-0.1', '0.1-0.2', '0.2-0.35', '> 0.35'):
                if b in buckets:
                    f_, n = buckets[b]
                    print(f"     {b:<9} {f_}/{n} filled")

    if probes:
        print("\n4) PROBES: see decisions WHERE reason LIKE 'cross probe%'")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
