"""What actually priced the decisions? Back out the sigma from the logged fair.

`tools/sigma_bias.py` found that the exported decisions stop reproducing
fair = Phi(ln(S/K)/(sigma*sqrt(tau/60))) on 2026-09-10 (86% -> 43% -> 22%), the
same day the ledger turned negative. That could be the money path mispricing or
just a bad export. This runs the identical test against the DB directly and, for
every row that does not reproduce, SOLVES for the sigma that would have:

    Phi^-1(fair) = ln(S/K) / (sigma_used * sqrt(tau/60))
    =>  sigma_used = ln(S/K) / (Phi^-1(fair) * sqrt(tau/60))

then prints sigma_used / sigma_1m. If that ratio is a stable constant the arm
was pricing off a different sigma (a seasonality factor applied twice, or a
tau-scaled sigma). If it drifts with tau or with |z|, the fair came from a
different MODEL (drift_digital adds a drift term, so the gap is not a constant).

Read-only. Usage:
    python3 tools/fair_forensics.py
"""
import math
import sqlite3
import datetime as dt

DB = "/root/BinancePrediction/data/btcpredict.db"
DAY0 = 1788825600000          # 2026-09-08 00:00 UTC  (day 20704)
DAY1 = 1790035200000          # 2026-09-22 00:00 UTC (covers today)
TOL = 0.02


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def phi_inv(p: float) -> float:
    lo, hi = -8.0, 8.0
    for _ in range(90):
        m = (lo + hi) / 2
        if phi(m) < p:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""
SELECT ts_ms, tau_s, fair, sigma_1m, spot, strike, side, acted, reason
FROM decisions
WHERE ts_ms >= ? AND ts_ms < ? AND slug LIKE 'btc-updown-5m%'
  AND sigma_1m > 0 AND spot > 0 AND strike > 0 AND fair > 0 AND fair < 1
  AND tau_s > 0
""", (DAY0, DAY1)).fetchall()
print(f"decisions in 09-08..09-14: {len(rows):,}")

byday = {}
for r in rows:
    d = (r["ts_ms"] - DAY0) // 86_400_000
    z = math.log(r["spot"] / r["strike"]) / (r["sigma_1m"] * math.sqrt(r["tau_s"] / 60.0))
    fm = phi(z)
    if abs(fm - r["fair"]) <= TOL:
        kind = "up"
    elif abs((1.0 - fm) - r["fair"]) <= TOL:
        kind = "down"
    else:
        kind = "neither"
    rec = byday.setdefault(d, {"n": 0, "up": 0, "down": 0, "neither": 0,
                               "ratios": [], "z": [], "tau": [],
                               "a_n": 0, "a_ok": 0, "a_rat": [],
                               "q_n": 0, "q_ok": 0})
    rec["n"] += 1
    rec[kind] += 1
    acted = int(r["acted"] or 0)
    if acted:
        rec["a_n"] += 1
        rec["a_ok"] += int(kind != "neither")
    else:
        rec["q_n"] += 1
        rec["q_ok"] += int(kind != "neither")
    if kind == "neither":
        # `fair` is logged for the side bought. For Up, fair = Phi(ln(S/K)/s');
        # for Down it is 1 - Phi(...), i.e. Phi^{-1}(fair) = -ln(S/K)/s'.
        # The sign MUST follow the side, or every Down row solves to a negative
        # sigma -- which is what an earlier version of this printed.
        sgn = 1.0 if r["side"] == "Up" else -1.0
        f_side = r["fair"] if r["side"] == "Up" else 1.0 - r["fair"]
        zi = sgn * phi_inv(min(0.999999, max(1e-6, f_side)))
        if abs(zi) > 1e-9:
            su = math.log(r["spot"] / r["strike"]) / (zi * math.sqrt(r["tau_s"] / 60.0))
            if r["sigma_1m"] > 0:
                rec["ratios"].append(su / r["sigma_1m"])
                rec["z"].append(abs(z))
                rec["tau"].append(r["tau_s"])
                if acted:
                    rec["a_rat"].append(su / r["sigma_1m"])

print(f"\n  {'day':<12}{'n':>8}{'up':>8}{'down':>8}{'NEITHER':>9}"
      f"{'matched':>9}{'sigma_used/sigma_1m':>22}  note")
for d in sorted(byday):
    r = byday[d]
    day = dt.datetime.fromtimestamp(DAY0 / 1000 + d * 86400,
                                    dt.timezone.utc).date()
    rs = sorted(x for x in r["ratios"] if x > 0)
    med = rs[len(rs) // 2] if rs else float("nan")
    note = "thin (<100 decisions)" if r["n"] < 100 else ""
    print(f"  {str(day):<12}{r['n']:>8}{r['up']:>8}{r['down']:>8}"
          f"{r['neither']:>9}{100 * (r['up'] + r['down']) / r['n']:>8.1f}%"
          f"{med:>22.3f}  {note}")

# is the implied sigma ratio a constant, or does it move with tau / |z|?
print(f"\n  THE SPLIT THAT MATTERS: does the arm ACT on the rows it cannot "
      f"reproduce?")
print(f"  {'day':<12}{'acted n':>9}{'acted ok':>10}{'quiet n':>9}"
      f"{'quiet ok':>10}{'acted sigma_used/sigma_1m':>28}")
for d in sorted(byday):
    r = byday[d]
    if r["a_n"] + r["q_n"] == 0:
        continue
    day = dt.datetime.fromtimestamp(DAY0 / 1000 + d * 86400,
                                    dt.timezone.utc).date()
    ar = sorted(x for x in r["a_rat"] if x > 0)
    amed = ar[len(ar) // 2] if ar else float("nan")
    a_pct = 100 * r["a_ok"] / r["a_n"] if r["a_n"] else float("nan")
    q_pct = 100 * r["q_ok"] / r["q_n"] if r["q_n"] else float("nan")
    print(f"  {str(day):<12}{r['a_n']:>9}{a_pct:>9.1f}%{r['q_n']:>9}"
          f"{q_pct:>9.1f}%{amed:>28.3f}")

allr, allz, allt = [], [], []
for d in byday:
    allr += byday[d]["ratios"]
    allz += byday[d]["z"]
    allt += byday[d]["tau"]
if allr:
    rs = sorted(allr)
    n = len(rs)
    print(f"\n  sigma_used/sigma_1m over {n:,} mismatched rows:")
    for q, lab in ((0.05, "p5"), (0.25, "p25"), (0.50, "median"),
                   (0.75, "p75"), (0.95, "p95")):
        print(f"    {lab:<7}{rs[int(q * (n - 1))]:.3f}")

    def bucket(vals, key, edges):
        print(f"  ratio vs {key}:")
        for lo, hi in edges:
            sel = [v for v, k in zip(allr, vals) if lo <= k < hi]
            if len(sel) < 50:
                continue
            sel.sort()
            print(f"    {key} {lo:>5.0f}-{hi:<5.0f} n={len(sel):>7} "
                  f"median ratio {sel[len(sel) // 2]:.3f}")

    bucket(allt, "tau", [(0, 60), (60, 90), (90, 120), (120, 150), (150, 200)])
    bucket(allz, "|z|", [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.5),
                         (2.5, 99.0)])
    print("\n  a FLAT ratio column = a different sigma (a constant factor).")
    print("  a ratio that MOVES with tau or |z| = a different MODEL "
          "(drift_digital adds a drift term).")
