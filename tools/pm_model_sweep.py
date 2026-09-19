"""SWEEP THE MODEL ARM ON BINANCE -- WITH A SHUFFLE CONTROL.

The model arm is the only thing in this repo that made money: +$47.27 over 693
fills, all on Binance. But that record does not survive inspection -- the top 5
trades are +$64.16, so without them it is -$16.89, and the day-clustered t is
-0.11. So this is not "retune a winner", it is "is there anything here at all".

THE ARM: fair = Phi(ln(S/K) / (sigma_1m * sqrt(tau/60))), buy the side when
fair - ask > min_edge, inside a quote band and a tau window. Replayed on the
Binance era only (settlement is the POINT vs strike there, 98.7% reproduced --
see tools/pm_venue_settlement.py), priced at the ask that existed `delay` ms
after the decision.

⛔ EVERY SWEEP IN THIS REPO HAS BEEN RUN WITHOUT A CONTROL, AND THAT IS HOW THE
CROSS ARM'S GATES WERE CHOSEN. So this runs the identical grid three more times
against SHUFFLED outcomes, where no edge exists by construction. If the shuffles
produce cells as good as the real data, a winning cell here means nothing. On
the locked-tail data this afternoon all three shuffles BEAT the real best.

    python3 tools/pm_model_sweep.py
"""
from __future__ import annotations

import collections, datetime as dt, math, os, sqlite3, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEE = lambda p: 0.07 * p * (1 - p)
CUT = dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc).timestamp() * 1000


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def load(delay_ms=1000):
    db = os.path.join(ROOT, "data", "btcpredict.db")
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rd = {r["slug"]: r for r in c.execute(
        """select slug,strike,winner,start_ms from rounds
           where winner in ('Up','Down') and strike is not null and start_ms < ?""",
        (CUT,))}
    q = collections.defaultdict(list)
    for r in c.execute("""select slug,ts_ms,tau_s,spot,up_ask,down_ask from quotes
          where spot is not null and spot>0 and up_ask is not null
            and down_ask is not null and ts_ms < ? and tau_s between 5 and 300
          order by ts_ms""", (CUT,)):
        if r["slug"] in rd:
            q[r["slug"]].append(r)
    c.close()
    cand = []                       # (slug, day, tau, fair_up, ask_up, ask_dn, won_up)
    for slug, rows in q.items():
        K = float(rd[slug]["strike"]); win = rd[slug]["winner"]
        day = dt.datetime.fromtimestamp(rd[slug]["start_ms"]/1000, dt.UTC).strftime("%m-%d")
        sp = np.array([float(x["spot"]) for x in rows])
        if len(sp) < 40:
            continue
        # sigma per MINUTE from this round's own opening tape, as the arm does
        d = np.diff(np.log(sp[:max(30, len(sp)//3)]))
        if len(d) < 10:
            continue
        sig1m = float(d.std() * math.sqrt(60.0))       # ~1 sample/s -> per minute
        if not np.isfinite(sig1m) or sig1m <= 0:
            continue
        ts = np.array([x["ts_ms"] for x in rows])
        ua = np.array([x["up_ask"] for x in rows], float)
        da = np.array([x["down_ask"] for x in rows], float)
        for i, x in enumerate(rows):
            tau = float(x["tau_s"] or 0)
            if tau <= 0:
                continue
            z = math.log(sp[i] / K) / (sig1m * math.sqrt(tau / 60.0))
            fair = norm_cdf(z)
            j = int(np.searchsorted(ts, ts[i] + delay_ms, "left"))
            if j >= len(ua):
                continue
            au, ad = float(ua[j]), float(da[j])
            if not (0.01 < au < 0.99 and 0.01 < ad < 0.99):
                continue
            cand.append((slug, day, tau, fair, au, ad, 1.0 if win == "Up" else 0.0))
    return cand


def run(cand, up_outcome, min_edge, min_q, max_q, tlo, thi):
    """One bet per round: the first tick clearing every gate."""
    seen = set(); pnl = []; days = []; wins = []; asks = []
    for (slug, day, tau, fair, au, ad, _w), won_up in zip(cand, up_outcome):
        if slug in seen or not (tlo <= tau <= thi):
            continue
        for side, ask, p in (("Up", au, fair), ("Down", ad, 1.0 - fair)):
            if not (min_q <= ask <= max_q):
                continue
            if (p - ask - FEE(ask)) < min_edge:
                continue
            w = won_up if side == "Up" else 1.0 - won_up
            pnl.append((1.0 / ask) * (w - ask - FEE(ask)))
            wins.append(w); asks.append(ask); days.append(day); seen.add(slug)
            break
    if len(pnl) < 60:
        return None
    p = np.array(pnl); dv = np.array(days)
    sd = p.std(ddof=1)
    dm = np.array([p[dv == d].mean() for d in np.unique(dv)])
    return dict(n=len(p), per=p.mean(), tot=p.sum(), win=100*np.mean(wins),
                ask=float(np.mean(asks)),
                t=p.mean()/(sd/math.sqrt(len(p))) if sd else 0.0,
                dayt=(dm.mean()/(dm.std(ddof=1)/math.sqrt(len(dm)))
                      if len(dm) > 1 and dm.std(ddof=1) else 0.0),
                nd=len(dm))


GRID = [(e, lo, hi, tl, th)
        for e in (0.01, 0.03, 0.05, 0.09, 0.15)
        for lo, hi in ((0.25, 0.90), (0.35, 0.80), (0.45, 0.80), (0.45, 0.65))
        for tl, th in ((5, 300), (30, 240), (60, 210), (60, 120), (120, 300))]


def sweep(cand, outcome, label):
    rows = []
    for e, lo, hi, tl, th in GRID:
        r = run(cand, outcome, e, lo, hi, tl, th)
        if r is None:
            continue
        r.update(e=e, lo=lo, hi=hi, tl=tl, th=th)
        rows.append(r)
    rows.sort(key=lambda x: -x["per"])
    print("  %s: %d live cells of %d" % (label, len(rows), len(GRID)))
    print("    %-38s %5s %7s %8s %9s %7s %7s"
          % ("cell", "n", "win%", "ask", "$/bet", "t", "day-t"))
    for r in rows[:5]:
        print("    edge>=%.2f q[%.2f,%.2f] tau%3d-%3d %5d %6.1f%% %8.4f %+9.4f %+7.2f %+7.2f"
              % (r["e"], r["lo"], r["hi"], r["tl"], r["th"], r["n"], r["win"],
                 r["ask"], r["per"], r["t"], r["dayt"]))
    sig = [r for r in rows if r["t"] >= 2.0 and r["dayt"] >= 2.0]
    print("    cells with t>=2 AND day-t>=2 : %d" % len(sig))
    return rows


def main():
    cand = load()
    print("candidate ticks: %s over %d rounds\n"
          % (f"{len(cand):,}", len({c[0] for c in cand})))
    y = np.array([c[6] for c in cand])
    print("=" * 78); print("1. THE REAL SWEEP"); print("=" * 78)
    real = sweep(cand, y, "real outcomes")
    print()
    print("=" * 78)
    print("2. SHUFFLED OUTCOMES -- no edge exists here by construction")
    print("=" * 78)
    # ⛔ THE NULL MUST KEEP THE MARKET CALIBRATED. Permuting outcomes across
    # rounds breaks the price-outcome relationship: a 5c longshot then "wins"
    # 50% of the time instead of ~10%, and a win at 0.05 pays 19x, so the
    # permuted data looks wildly profitable (+0.12/bet) for a reason that has
    # nothing to do with the sweep. That is a broken control, not a finding.
    #
    # The right null draws each round's outcome from Bernoulli(market-implied
    # p) taken from the two asks. The market is then EXACTLY fair by
    # construction and the model has no edge -- which is precisely the
    # hypothesis a sweep has to beat.
    rng = np.random.default_rng(11)
    pm = {}
    for slug, day, tau, fair, au, ad, w in cand:
        if slug not in pm:
            pm[slug] = au / (au + ad)          # de-vigged market P(Up)
    slugs = list(pm)
    fakes = []
    for k in range(3):
        draw = {s: float(rng.random() < pm[s]) for s in slugs}
        fakes.append(sweep(cand, np.array([draw[c[0]] for c in cand]),
                           "calibrated null #%d" % (k + 1)))
        print()
    print("=" * 78); print("3. VERDICT"); print("=" * 78)
    rb = real[0]["per"] if real else 0
    fb = [f[0]["per"] for f in fakes if f]
    print("  best $/bet REAL     : %+.4f" % rb)
    print("  best $/bet SHUFFLED : %s" % ", ".join("%+.4f" % x for x in fb))
    if fb and rb <= max(fb):
        print("\n  the sweep finds an equally good config in data with NO edge.")
        print("  a profitable cell here is evidence of nothing.")
    else:
        print("\n  the real best exceeds every shuffle. weak evidence, not proof --")
        print("  check the day-t and whether one day carries it.")


if __name__ == "__main__":
    main()
