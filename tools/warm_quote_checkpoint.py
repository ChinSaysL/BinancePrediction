"""The WARM-QUOTE CHECKPOINT, read out exactly (decision -> venue createTime).

FINDINGS.md shipped the warm quote with this checkpoint:

    after ~20 fires, compare the venue order's `createTime` minus the CROSS
    decision log against the 169-220ms cold baseline -- a warm hit should print
    ~125-150ms.

46 warm hits exist, so it is due. The log does not carry createTime (0 hits), so
this pulls the venue's own order records -- read-only GET order/history -- and
joins them to the CROSS decision by slug, taking the first order created at or
after the decision (later ones are the retries, which always fetch fresh and so
pay the fetch deliberately).

    .venv/bin/python tools/warm_quote_checkpoint.py --log data/live.log
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import statistics

import aiohttp

from btcpredict.config import settings
from btcpredict.execution.live import ORDER_HISTORY, LiveBroker

P_CROSS = re.compile(
    r"^(\S+ \S+),(\d+) .*?CROSS (\S+) (\S+) sweep_px=([\d.]+) .*?tau (\d+)s", re.M)
P_WARM = re.compile(
    r"^(\S+ \S+),(\d+) .*?warm quote used for (\S+) (\S+): age", re.M)


def stamp(s: str, ms: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=dt.timezone.utc).timestamp() + int(ms) / 1000.0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="data/live.log")
    ap.add_argument("--pages", type=int, default=4)
    args = ap.parse_args()
    text = open(args.log, errors="ignore").read()

    decisions = {}
    for m in P_CROSS.finditer(text):
        decisions.setdefault(m.group(3), []).append(
            (stamp(m.group(1), m.group(2)), m.group(4), int(m.group(6))))
    warm_at = {}
    for m in P_WARM.finditer(text):
        warm_at.setdefault(m.group(3), []).append(stamp(m.group(1), m.group(2)))

    async with aiohttp.ClientSession() as s:
        b = LiveBroker(s, settings.binance_api_key, settings.binance_api_secret)
        if not await b.discover_wallet():
            raise SystemExit("no wallet")
        orders = []
        for page in range(1, args.pages + 1):
            r = await b._request("GET", ORDER_HISTORY, {
                "walletAddress": b.wallet_address,
                "pageSize": 50, "pageNo": page})
            got = (r or {}).get("orders") or []
            orders.extend(got)
            if len(got) < 50:
                break

    rows = []
    for o in orders:
        slug = o.get("slug")
        ct = o.get("createTime")
        if not slug or not ct or slug not in decisions:
            continue
        ct_s = ct / 1000.0
        first = min((d for d in decisions[slug] if d[0] <= ct_s + 0.5),
                    key=lambda d: abs(d[0] - ct_s), default=None)
        if first is None:
            continue
        lag = (ct_s - first[0]) * 1000.0
        if lag < 0 or lag > 3000:
            continue          # not the first attempt of this fire
        ws = warm_at.get(slug, [])
        warm = any(abs(w - first[0]) < 1.0 for w in ws)
        rows.append((slug, first[2], lag, warm, o.get("status"),
                     (o.get("errorMessage") or "")[:24]))

    rows.sort(key=lambda r: r[1])
    print(f"venue orders read: {len(orders)}   matched to a fire: {len(rows)}\n")
    print(f"  {'tau':>4s} {'decision -> createTime':>22s}  warm  status")
    for slug, tau, lag, warm, st, err in rows:
        print(f"  {tau:>4d} {lag:>19.0f}ms  {'WARM' if warm else 'cold':>5s}"
              f"  {st} {err}")

    for flag in (True, False):
        v = [r[2] for r in rows if r[3] is flag]
        if v:
            print(f"\n  {'WARM' if flag else 'COLD':5s} n={len(v):3d}  "
                  f"p50 {statistics.median(v):6.0f}ms  min {min(v):5.0f}"
                  f"  max {max(v):5.0f}")
    print("\n  The checkpoint expected WARM ~125-150ms against a 169-220ms cold")
    print("  baseline. If warm is not clearly below that, the saving did not")
    print("  land where it was predicted.")


if __name__ == "__main__":
    asyncio.run(main())
