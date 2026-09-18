"""The ASK AT THE TRIGGER, for the candidate families. This is the number that
decides whether a family is tradeable; the sweep only ever ranked the signal.

WHY THE PREVIOUS PROBE WAS NOT ENOUGH. `pm_ask_probe.py` sampled the book at
whatever moment it happened to run, across the whole life of a round. But the
train arm fires on a CONDITION -- `|spot - strike| >= 15bps` with `tau >= 60` --
and that condition is precisely when the favourite is expensive. Measuring the
ask unconditionally answers the wrong question: the first read showed the
favourite running 0.90-0.98 once decided, which is above every candidate's EV
ceiling, but those samples were not at the trigger.

So this waits for the trigger and records the ask THERE:
  * strike = the round's own start price = the OPEN of the Binance 1m kline at
    the round's start second (PM resolves on the price at the beginning of the
    range, and the sweep uses the same definition -- do not re-derive it);
  * d_bps = 1e4 * (spot - strike) / strike, signed;
  * fires when |d_bps| >= --thr and 60 <= tau <= 300, the live train gates.

The side taken is the FAVOURITE (the side the spot has moved toward), which is
what the train arm buys.

Read-only: public Binance + public CLOB. No orders, no credentials, and it runs
as its own process so the live bot is untouched (widening the bot's own book is
what OOM-killed it at 12 assets -- do not repeat that to collect a measurement).

    .venv/bin/python tools/pm_trigger_watch.py --status
    .venv/bin/python tools/pm_trigger_watch.py --minutes 240 --csv data/asks.csv
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

FAMILIES = [("btc", "BTCUSDT"), ("eth", "ETHUSDT"), ("bnb", "BNBUSDT"),
            ("sol", "SOLUSDT"), ("xrp", "XRPUSDT"), ("doge", "DOGEUSDT")]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0.0.0 "
                    "Safari/537.36"}
BAPI = "https://api.binance.com/api/v3"


def get(url: str):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=20).read())


def spot(sym: str) -> float:
    return float(get("%s/ticker/price?symbol=%s" % (BAPI, sym))["price"])


_strikes: dict[tuple[str, int], float] = {}


def strike_of(sym: str, start_s: int) -> float | None:
    """The OPEN of the 1m bar that begins the round. Cached per (sym, start).

    This IS the settlement reference -- "the price at the beginning of that
    range" -- so it is fetched, never approximated from the current spot.
    """
    key = (sym, start_s)
    if key in _strikes:
        return _strikes[key]
    k = get("%s/klines?symbol=%s&interval=1m&startTime=%d&limit=1"
            % (BAPI, sym, start_s * 1000))
    if not k:
        return None
    _strikes[key] = float(k[0][1])
    return _strikes[key]


def touch(levels) -> tuple[float, float]:
    """(best ask, size). Take the touch by MIN, never by index -- the API order
    has bitten this repo before."""
    best = (0.0, 0.0)
    for lv in levels or ():
        try:
            px, sz = float(lv["price"]), float(lv["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if px > 0 and sz > 0 and (best[0] == 0.0 or px < best[0]):
            best = (px, sz)
    return best


def asks(round_obj) -> tuple[tuple[float, float], tuple[float, float]]:
    out = []
    for tok in (round_obj.up_token, round_obj.down_token):
        b = get("%s/book?token_id=%s" % (M.CLOB, tok))
        out.append(touch(b.get("asks")))
    return out[0], out[1]


def sample(fam: str, sym: str, thr: float, lo: float, hi: float,
           *, report_always: bool) -> dict | None:
    now = time.time()
    try:
        r = M.fetch_round(now, fam + "-updown-5m")
    except Exception as exc:                                  # noqa: BLE001
        print("  %-5s round fetch failed: %s" % (fam, type(exc).__name__))
        return None
    if r is None:
        print("  %-5s no current round" % fam)
        return None
    slug = getattr(r, "slug", "")
    try:
        start_s = int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        print("  %-5s unparsable slug %r" % (fam, slug))
        return None
    period = 900 if "15m" in slug else 300
    tau = start_s + period - now
    st = strike_of(sym, start_s)
    if st is None:
        print("  %-5s no strike bar yet" % fam)
        return None
    sp = spot(sym)
    d_bps = 1e4 * (sp - st) / st
    fires = abs(d_bps) >= thr and lo <= tau <= hi
    if not (fires or report_always):
        return None
    (ua, us), (da, ds) = asks(r)
    side = "Up" if d_bps > 0 else "Down"
    fav = ua if side == "Up" else da
    fav_usd = us if side == "Up" else ds
    print("  %-5s tau %5.0f  d %+7.2f bps  %-4s ask %.3f ($%5.0f) %s" % (
        fam, tau, d_bps, side, fav, fav * fav_usd,
        "FIRES" if fires else ""))
    if not fires:
        return None
    return {"ts": int(now), "family": fam, "slug": slug, "tau_s": round(tau, 1),
            "d_bps": round(d_bps, 2), "side": side,
            "up_ask": ua, "dn_ask": da, "fav_ask": fav,
            "fav_touch_usd": round(fav * fav_usd, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thr", type=float, default=15.0, help="train bps gate")
    ap.add_argument("--lo", type=float, default=60.0, help="min tau")
    ap.add_argument("--hi", type=float, default=300.0, help="max tau")
    ap.add_argument("--every", type=float, default=15.0, help="sample seconds")
    ap.add_argument("--minutes", type=float, default=0.0, help="0 = forever")
    ap.add_argument("--csv", default="")
    ap.add_argument("--status", action="store_true",
                    help="one unconditional pass, to prove the wiring")
    a = ap.parse_args()

    deadline = time.time() + a.minutes * 60 if a.minutes else None
    n = rows = 0
    while True:
        n += 1
        print("--- pass %d  %s ---" % (n, time.strftime("%H:%M:%S",
                                                        time.gmtime())))
        for fam, sym in FAMILIES:
            try:
                got = sample(fam, sym, a.thr, a.lo, a.hi,
                             report_always=a.status)
            except Exception as exc:                          # noqa: BLE001
                print("  %-5s failed: %s: %s" % (fam, type(exc).__name__, exc))
                continue
            if got and a.csv:
                os.makedirs(os.path.dirname(a.csv) or ".", exist_ok=True)
                new = not os.path.exists(a.csv)
                with open(a.csv, "a", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=list(got))
                    if new:
                        w.writeheader()
                    w.writerow(got)
                rows += 1
        if a.status:
            break
        if deadline and time.time() >= deadline:
            break
        time.sleep(max(1.0, a.every))
    print("\ndone: %d passes, %d trigger rows" % (n, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
