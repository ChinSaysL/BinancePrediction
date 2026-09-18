"""Dump the twap-resolved 5m rounds + their Binance spot tape, for the
Chainlink comparison. Runs ON THE VPS, writes JSON to stdout.

    cd /root/BinancePrediction && PYTHONPATH=. \
        /root/analysis-venv/bin/python tools/pm_rounds_dump.py > /tmp/rounds.json

Why only the last ~100 s of each round: the only statistic under test is the
tail-60 mean at tau, so anything older is dead weight.
"""
from __future__ import annotations

import json
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "data/btcpredict.db"
TAU_MAX = 95.0

c = sqlite3.connect(DB)
rows = c.execute(
    """
    select slug, start_ms, end_ms, strike, winner
      from rounds
     where feed_provider like '%twap%'
       and slug like '%-5m-%'
       and winner is not null
       and strike is not null
     order by end_ms
    """
).fetchall()
print(f"rounds: {len(rows)}", file=sys.stderr)

out = []
for slug, start_ms, end_ms, strike, winner in rows:
    q = c.execute(
        """
        select ts_ms, tau_s, spot, up_ask, down_ask
          from quotes
         where slug = ? and tau_s >= 0 and tau_s <= ?
           and spot is not null
         order by ts_ms
        """,
        (slug, TAU_MAX),
    ).fetchall()
    if len(q) < 20:
        continue
    coin = slug.split("-", 1)[0]
    out.append({
        "slug": slug, "coin": coin, "start_ms": start_ms, "end_ms": end_ms,
        "strike": strike, "winner": winner,
        "q": [[int(t), t, s, u, d] for t, tau, s, u, d in q],
    })

json.dump(out, sys.stdout)
print(f"\nkept: {len(out)} rounds, "
      f"{sum(len(r['q']) for r in out)} quotes", file=sys.stderr)
