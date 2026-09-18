"""WHICH RULE DOES POLYMARKET ACTUALLY SETTLE ON? Fit it to its own winners.

WHY THIS COMES FIRST. Every calibration cell in FINDINGS was fitted against the
Binance wrapper's rule -- the POINT price at the round's end versus `startPrice`.
Polymarket pays on something else (its description: a Chainlink TWAP versus the
price at the BEGINNING of the range), and the two disagree on 10.4% of rounds.
A train-arm threshold swept against the wrong terminal variable is fitted to a
different bet than the one the venue settles.

The API says `cryptoMarketConfig.twapLookbackSeconds = 60`, which is NOT the
same as "average the whole 5-minute window": it could be a 60 s TWAP taken at
the END of the range. Those two rules have variances of `sigma^2*T/3` and
`sigma^2*60/3` -- a factor of T/60 = 5 apart -- so a threshold calibrated under
the wrong one is out by more than a factor of two in sigma units. This tool
settles it by fitting the candidates to Polymarket's OWN winners:

    for each (family, round) with a published winner, and a 1 s price path:
        A  close(T)            >= K   <- the Binance rule, as a control
        B  mean(close[0:T])    >= K   <- TWAP over the whole range
        C  mean(close[T-60:T]) >= K   <- 60 s TWAP at the end
        D  mean(close[0:60])   >= K   <- 60 s TWAP at the start
and report which candidate reproduces the published winner most often.

Read-only: gamma GETs and local 1 s klines. No venue credentials, no orders.

Usage:
    python3 tools/pm_rule_ident.py --days 2026-09-13,2026-09-14 --symbol BTCUSDT
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import time
import urllib.request
import zipfile
from collections import defaultdict

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
      "Accept": "application/json"}
BARS = "data/bars_1s/daily"
PERIODS = {"5m": 300, "15m": 900}


def get(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as fh:
            return json.loads(fh.read().decode())
    except Exception:                                    # noqa: BLE001
        return None


def jparse(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except (ValueError, TypeError):
            return None
    return x


def load_day(symbol: str, day: str) -> dict[int, float] | None:
    """{unix_second: close} for one symbol-day of 1 s klines."""
    path = os.path.join(BARS, f"%s-1s-%s.zip" % (symbol, day))
    if not os.path.exists(path):
        return None
    out: dict[int, float] = {}
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            for row in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                if not row or not row[0].isdigit():
                    continue          # header, if any
                t = int(row[0])
                # open_time arrives in ms in these files (and us elsewhere).
                if t > 1e15:
                    t //= 1_000_000
                elif t > 1e11:
                    t //= 1_000
                try:
                    out[t] = float(row[4])       # close
                except (ValueError, IndexError):
                    continue
    return out


def pm_winner(series: str, start: int) -> str | None:
    """Polymarket's own settled winner for a round, or None."""
    d = get("%s/markets?slug=%s-%d&closed=true" % (GAMMA, series, start))
    if not d:
        return None
    m = d[0]
    st = str(m.get("umaResolutionStatus") or "")
    oc, op = jparse(m.get("outcomes")), jparse(m.get("outcomePrices"))
    if not oc or not op or len(oc) != len(op):
        return None
    try:
        prices = [float(x) for x in op]
    except (TypeError, ValueError):
        return None
    if max(prices) < 0.99:
        return None                       # not decided
    return oc[max(range(len(op)), key=lambda i: prices[i])], st


def rules(px: list[float]) -> dict[str, bool]:
    """Each candidate rule's verdict: True == Up wins."""
    n = len(px)
    k = px[0]                             # the price at the BEGINNING
    return {
        "A_close": px[-1] >= k,
        # Averaging is over the range; the tail window is the last 60 s.
        "B_twap_all": (sum(px) / n) >= k,
        "C_twap_tail60": (sum(px[-60:]) / len(px[-60:])) >= k,
        "D_twap_head60": (sum(px[:60]) / len(px[:60])) >= k,
        # Chainlink rounds the index to 8 dp; use closes rounded the same way.
        "E_twap_all_r8": (round(sum(px) / n, 8)) >= k,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", required=True,
                    help="comma-separated YYYY-MM-DD")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the rounds fetched (0 = all)")
    a = ap.parse_args()

    period = PERIODS[a.tf]
    series = "%s-updown-%s" % (a.symbol[:-4].lower(), a.tf)
    days = [d.strip() for d in a.days.split(",") if d.strip()]

    bars: dict[int, float] = {}
    for day in days:
        got = load_day(a.symbol, day)
        if got is None:
            print("  (no local 1s bars for %s %s)" % (a.symbol, day))
            continue
        bars.update(got)
    if not bars:
        print("no bars loaded")
        return 1
    lo, hi = min(bars), max(bars)
    print("%s %s: %d seconds of %s, %s .. %s"
          % (a.symbol, a.tf, len(bars), a.symbol,
             time.strftime("%m-%d %H:%M", time.gmtime(lo)),
             time.strftime("%m-%d %H:%M", time.gmtime(hi))))

    starts = [t for t in range(lo, hi - period, period) if t % period == 0]
    if a.limit:
        starts = starts[-a.limit:]

    tally = defaultdict(lambda: [0, 0])       # rule -> [agree, total]
    both = []                                  # both venues, for the disagreement
    n_pm = 0
    for start in starts:
        px = [bars.get(start + i) for i in range(period)]
        if any(p is None for p in px):
            continue
        res = pm_winner(series, start)
        if res is None:
            continue
        winner, status = res
        n_pm += 1
        want_up = (winner == "Up")
        verdicts = rules(px)
        for name, up in verdicts.items():
            tally[name][1] += 1
            tally[name][0] += int(up == want_up)
        # how often does the Binance rule disagree with what PM paid?
        if verdicts["A_close"] != want_up:
            both.append((start, winner, px[0], px[-1],
                         sum(px) / period, sum(px[-60:]) / 60))

    print("\nrounds with a PM winner AND a full price path: %d" % n_pm)
    if not n_pm:
        return 0
    print("\n%-16s %8s %8s" % ("candidate rule", "agree", "rate"))
    for name in ("A_close", "E_twap_all_r8", "B_twap_all",
                 "C_twap_tail60", "D_twap_head60"):
        ok, tot = tally[name]
        if not tot:
            continue
        print("%-16s %8d %7.1f%%" % (name, ok, 100.0 * ok / tot))

    print("\nthe Binance POINT rule disagrees with PM on %d of %d rounds (%.1f%%)"
          % (len(both), n_pm, 100.0 * len(both) / n_pm))
    print("  start        PM paid   K(open)      close(T)   TWAP_all  TWAP_t60")
    for start, winner, k, c, tw, t60 in both[:12]:
        print("  %s  %-6s  %10.2f  %10.2f  %9.2f  %8.2f"
              % (time.strftime("%m-%d %H:%M", time.gmtime(start)), winner,
                 k, c, tw, t60))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
