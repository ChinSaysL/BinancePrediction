"""What the candidate families COST at the moment the train arm would fire.

WHY THIS EXISTS. `pm_train_sweep.py` ranks the SIGNAL: it answers "when spot is
>= 15bps through the strike with >=60s left, how often does it hold?" It never
sees the venue's ask, and what the arm earns is `win - ask - fee`. A family can
win 87% and still lose money if the market charges 0.90 for it.

So this measures the OTHER half: for every family, the live round's tau and the
ask on both sides, plus the notional resting at the touch. Point it at the six
5m families and let it run; what matters is the ask on the FAVOURITE side at
tau 60-300, which is the price the train arm would pay.

Read-only: no orders, no credentials, public CLOB book only.

    .venv/bin/python tools/pm_ask_probe.py --n 10 --every 30 [--csv /tmp/asks.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from btcpredict.venues.polymarket import markets as M  # noqa: E402

FAMILIES = ["btc-updown-5m", "eth-updown-5m", "bnb-updown-5m",
            "sol-updown-5m", "xrp-updown-5m", "doge-updown-5m"]

#: The 403 on urllib's default UA is real and silent-ish; always send a browser
#: UA on these endpoints.
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0.0.0 "
                    "Safari/537.36"}


def top(levels: list) -> tuple[float, float]:
    """(best price, size) for a BUY, from levels in whatever order they came.

    Takes the touch by min/max, NEVER by index: the API returns bids descending
    and asks ascending, but relying on that has bitten this repo before.
    """
    best: tuple[float, float] = (0.0, 0.0)
    for lv in levels or ():
        try:
            px, sz = float(lv["price"]), float(lv["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if px <= 0 or sz <= 0:
            continue
        if best[0] == 0.0 or px < best[0]:
            best = (px, sz)
    return best


def book(token_id: str) -> dict:
    url = "%s/book?token_id=%s" % (M.CLOB, token_id)
    req = urllib.request.Request(url, headers=UA)
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="samples")
    ap.add_argument("--every", type=float, default=30.0, help="seconds between")
    ap.add_argument("--csv", default="", help="append rows here")
    a = ap.parse_args()

    rows: list[dict] = []
    for i in range(max(1, a.n)):
        now = time.time()
        print("\n--- sample %d  %s ---" % (i + 1,
              time.strftime("%H:%M:%S", time.gmtime(now))))
        print("%-16s %6s %9s %9s %10s %10s" % (
            "family", "tau", "Up ask", "Dn ask", "Up$@touch", "Dn$@touch"))
        for fam in FAMILIES:
            try:
                r = M.fetch_round(now, fam)
            except Exception as exc:                          # noqa: BLE001
                print("%-16s  fetch failed %s" % (fam, type(exc).__name__))
                continue
            if r is None:
                print("%-16s  (no round)" % fam)
                continue
            # `PmRound` carries `seconds_left`/`end_ms`, NOT tau_s/end_ts: the
            # earlier versions of this line guessed attribute names and printed
            # nan for every row, which read as "no data" rather than as a bug.
            tau = r.seconds_left
            try:
                ub, db = book(r.up_token), book(r.down_token)
                ua, us = top(ub.get("asks"))
                da, ds = top(db.get("asks"))
            except Exception as exc:                          # noqa: BLE001
                print("%-16s  book failed %s" % (fam, type(exc).__name__))
                continue
            print("%-16s %6.0f %9.3f %9.3f %10.0f %10.0f" % (
                fam, tau, ua, da, ua * us, da * ds))
            rows.append({"ts": int(now), "family": fam, "tau_s": round(tau, 1),
                         "up_ask": ua, "dn_ask": da,
                         "up_touch_usd": round(ua * us, 2),
                         "dn_touch_usd": round(da * ds, 2)})
        if i + 1 < a.n:
            time.sleep(max(0.0, a.every))

    if a.csv and rows:
        new = not os.path.exists(a.csv)
        with open(a.csv, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            if new:
                w.writeheader()
            w.writerows(rows)
        print("\nappended %d rows to %s" % (len(rows), a.csv))

    # The decision this exists to inform: is the FAVOURITE ask inside the EV
    # ceiling for this family's measured win rate?
    if rows:
        print("\n--- favourite ask vs the EV ceiling (p_win from the sweep) ---")
        from btcpredict.venues.polymarket.broker import entry_ceiling
        for fam, p_win in (("btc", 0.915), ("eth", None), ("bnb", None),
                           ("sol", 0.893), ("xrp", 0.878), ("doge", 0.869)):
            if p_win is None:
                continue
            ceil = entry_ceiling(0.890, p_win, 0.03)
            fav = [max(r["up_ask"], r["dn_ask"]) for r in rows
                   if r["family"].startswith(fam + "-")]
            if fav:
                print("   %-5s p_win %.3f  ceiling %.3f  favourite ask "
                      "min %.3f med %.3f max %.3f" % (
                          fam, p_win, ceil, min(fav),
                          sorted(fav)[len(fav) // 2], max(fav)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
