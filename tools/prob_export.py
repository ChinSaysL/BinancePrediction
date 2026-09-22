"""Export the modelling rows as CSV, using ONLY the standard library.

WHY IT IS SPLIT OUT. The bot's venv has no numpy, scipy or sklearn -- it has what
the bot needs and nothing else -- so the fit cannot run on the host. The host
does the part it is good at (SQL over a 1 GB live database) and the analysis
machine does the part it is good at (numpy). The transfer is a few MB of CSV
instead of a 1 GB database copy, and nothing new is installed into a live
trading environment.

`fair_value` is NOT imported here. It is four lines of arithmetic
(`norm_cdf(ln(S/K)/(sigma*sqrt(tau/60)))`), so the consumer recomputes it and
this script stays stdlib-only and independent of the bot's config -- which also
means it cannot fail on a settings change.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys

COLS = ("slug", "ts_ms", "tau_s", "spot", "strike", "sigma_1m",
        "up_ask", "up_bid", "down_ask", "down_bid",
        "up_ask_size", "up_bid_size", "book_age_ms", "venue_lag_ms",
        "winner", "start_ms", "fee_rate_bps")

SQL = """
SELECT d.slug, d.ts_ms, d.tau_s, d.spot, d.strike, d.sigma_1m,
       q.up_ask, q.up_bid, q.down_ask, q.down_bid,
       q.up_ask_size, q.up_bid_size, q.book_age_ms, q.venue_lag_ms,
       r.winner, r.start_ms, q.fee_rate_bps
FROM decisions d
JOIN quotes q ON q.slug = d.slug AND q.ts_ms = d.ts_ms
JOIN rounds r ON r.slug = d.slug
WHERE d.slug LIKE ? AND d.ts_ms > ? AND d.tau_s BETWEEN ? AND ?
  AND d.spot > 0 AND d.strike > 0 AND d.sigma_1m > 0
  AND q.up_bid IS NOT NULL AND q.up_ask IS NOT NULL
  AND q.up_bid > 0 AND q.up_ask < 1
  AND r.winner IN ('Up','Down')
ORDER BY d.slug, d.ts_ms
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    ap.add_argument("--family", default="btc-updown-5m")
    ap.add_argument("--tau-lo", type=float, default=60.0)
    ap.add_argument("--tau-hi", type=float, default=210.0)
    ap.add_argument("--since-ms", type=int, default=1789880000000)
    ap.add_argument("--out", default="/tmp/prob_rows.csv")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = con.execute(SQL, (f"{args.family}-%", args.since_ms,
                            args.tau_lo, args.tau_hi))
    n = 0
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for row in cur:
            w.writerow(row)
            n += 1
    print(f"{n} rows -> {args.out}  ({args.family}, tau "
          f"[{args.tau_lo:g},{args.tau_hi:g}], since {args.since_ms})")
    if not n:
        sys.exit("nothing exported -- check --family / --since-ms")


if __name__ == "__main__":
    main()
