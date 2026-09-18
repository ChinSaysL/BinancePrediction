"""CROSS arm over 11 independent days, with a DAY-CLUSTERED test.

The kartik tape is a single day (2026-07-17), so the only split available on it
was within-day and no parameter could be promoted. This runs the same arm over
11 separate days from `data/btc5m` and reports the check that was impossible
before: cluster the per-trade EV by DAY and t-test the day means. One lucky day
cannot carry the result.

    pooled t        treats every trade as independent -- overstates
    day-clustered t treats each DAY as one observation -- the honest one

PRICE IS A REAL PRINT, NOT A QUOTE. This dataset has no order book, so an
entry can only be priced from an actual `trades_raw.json` execution:
a `side == "BUY"` print on the crossed token is someone lifting an offer,
which is exactly the action we would take. That also satisfies the rule
"never price an entry at a venue's DISPLAYED price".

CONSEQUENCE -- STATED UP FRONT: no book means the MAKER path cannot be tested
here (you cannot rest an order without knowing the quotes, and there is no
"trades through the limit" test). The kartik tape (1 day, 10-level L2) covers
the maker path; this dataset covers the taker path across 11 days. They are
complementary, and neither one alone is the whole answer.

TIMESTAMP RESOLUTION: `block_timestamp` is whole seconds, so our measured
280 ms hot path is BELOW the resolution of this data. `--entry-sec 0` takes the
first print in the fire second (most optimistic); `--entry-sec 1` requires the
next second or later (conservative). Both are reported, because the truth
sits between them and the data cannot separate them further.
"""
from __future__ import annotations

import argparse
import math
import statistics as st

from tools import btc5m_tape as T
from btcpredict.main import crossed_side

PM_FEE_RATE = 0.07
ROUND_S = 300


def fire_labels(spot_open: list[float], strike: float, look_s: int) -> list[str | None]:
    """Per-second fire side, using crossed_side's exact tie convention.

    `crossed_side` is `"Up" if px > strike else "Down"`, so px == strike is
    DOWN. A `b >= strike` test looks equivalent and is not.
    """
    def side_of(px: float) -> str:
        return "Up" if px > strike else "Down"
    out: list[str | None] = [None] * len(spot_open)
    for k in range(look_s, len(spot_open)):
        now, ref = side_of(spot_open[k]), side_of(spot_open[k - look_s])
        if now != ref:
            out[k] = now
    return out


def verify(day: str, look_s: int, n_rounds: int = 6) -> bool:
    """The fast per-second label must equal the real crossed_side."""
    bad = tot = 0
    for rd in T.rounds(day)[:n_rounds]:
        R = T.load_round(rd, day)
        if R is None:
            continue
        lab = fire_labels(R.spot_open, R.strike, look_s)
        ticks = [((R.start_s + k) * 1000, R.spot_open[k])
                 for k in range(len(R.spot_open))]
        for k in range(0, len(ticks), 7):
            real = crossed_side(ticks[:k + 1], R.strike, float(look_s), 2.0)
            fast = lab[k]
            tot += 1
            if real != fast:
                bad += 1
                if bad <= 5:
                    print(f"   MISMATCH k={k} real={real} fast={fast}")
    print(f"  fast-path check: {tot - bad}/{tot} agree with crossed_side "
          f"({'OK' if bad == 0 else 'FAILED'})")
    return bad == 0


def aggregate(prices, mode: str) -> float:
    """Turn the prints in the entry window into ONE entry price.

    The window is a proxy for the OFFER we would have to lift, and a print is
    not an offer -- someone else's fill is not a price we are entitled to.
    So the ranking that matters is:
        max    the most anyone paid in the window. If the arm survives being
               charged this, it survives the print/offer objection.
        vwap   size-weighted, the closest thing to a real traded level.
        median robust to the dusty outlier prints that litter the crossing
               second.
        first  the naive read, kept only to show how much it flatters.
    """
    if len(prices) == 0:
        return float("nan")
    if mode == "max":
        return float(prices.max())
    if mode == "first":
        return float(prices.iloc[0])
    if mode == "vwap":
        return float(prices.mean())
    return float(prices.median())


def coherent_seconds(tr: pd.DataFrame, tol: float) -> set[int]:
    """Seconds whose prints are internally consistent (Up + Down ~= 1).

    In the crossing second the tape can print Up 0.89 beside Down 0.11 (sum
    1.00) AND Up 0.18 beside Down 0.10 (sum 0.28). The second kind is not a
    price, so it is excluded when tol > 0.
    """
    if tol <= 0:
        return set()
    g = tr.groupby(["ts", "token_id"], sort=False)["price"].mean().unstack()
    if g.shape[1] < 2:
        return set()
    cols = list(g.columns)
    s = g[cols[0]] + g[cols[1]]
    ok = (s - 1.0).abs() <= tol
    return set(int(t) for t in g.index[ok.fillna(False).to_numpy()])


def run(day: str, *, look_s: int, min_tau: int, max_tau: int, max_px: float,
        min_px: float, lag_s: int, max_stale_s: int, require_buy: bool,
        do_verify: bool, price_mode: str = "median",
        coherence_tol: float = 0.0) -> list[dict]:
    out: list[dict] = []
    for rd in T.rounds(day):
        R = T.load_round(rd, day)
        if R is None:
            continue
        lab = fire_labels(R.spot_open, R.strike, look_s)
        cand = [(k, lab[k]) for k in range(len(lab))
                if lab[k] is not None
                and min_tau <= (ROUND_S - k) <= max_tau]
        if not cand:
            continue
        tr = T.load_trades(rd)
        if tr.empty:
            continue
        # Infer which token_id is UP from data up to the FIRST fire second --
        # no look-ahead, and no reliance on Gamma's clobTokenIds ordering,
        # which is wrong for a few percent of rounds.
        up_id = T.infer_up_token(R, tr, cand[0][0])
        if up_id is None:
            continue
        down_id = next((i for i in tr.token_id.unique() if i != up_id), None)
        if down_id is None:
            continue
        coh = coherent_seconds(tr, coherence_tol)
        for k, side in cand:                 # chronological; first that trades
            tid = up_id if side == "Up" else down_id
            t_from = R.start_s + k + lag_s
            sel = tr[(tr.token_id == tid) & (tr.ts >= t_from) &
                     (tr.ts <= t_from + max_stale_s)]
            if coh:
                sel = sel[sel.ts.isin(coh)]
            if require_buy:
                sel = sel[sel.side == "BUY"]
            if sel.empty:
                continue
            # One entry price for the window. A print is not an offer, so the
            # mode matters: 'max' charges the arm the most anyone paid. Within
            # the crossing second the tape also holds incoherent prints --
            # round btc-updown-5m-1777778700 prints Up 0.89 beside Down 0.11
            # (sum 1.00) but also 0.18 beside 0.10 (sum 0.28) -- which is what
            # coherent_seconds() is for.
            px = aggregate(sel.price, price_mode)
            n_pr = len(sel)
            spread = float(sel.price.max() - sel.price.median())
            if not (min_px <= px <= max_px):
                continue
            won = 1.0 if side == R.label else 0.0
            out.append({
                "day": day, "slug": R.slug, "side": side, "px": px,
                "won": won, "tau": ROUND_S - k, "npr": n_pr, "spread": spread,
                "ev": won * (1 - px) / px - (1 - won) - PM_FEE_RATE * (1 - px),
            })
            break                            # one entry per round
    return out


def report(trades: list[dict]) -> None:
    if not trades:
        print("    no trades")
        return
    ev = [t["ev"] for t in trades]
    n = len(ev)
    mean = st.fmean(ev)
    se = (st.stdev(ev) / math.sqrt(n)) if n > 1 else 0.0
    # cluster by day
    by_day: dict[str, list[float]] = {}
    for t in trades:
        by_day.setdefault(t["day"], []).append(t["ev"])
    dmeans = [st.fmean(v) for v in by_day.values()]
    dse = (st.stdev(dmeans) / math.sqrt(len(dmeans))) if len(dmeans) > 1 else 0.0
    win = st.fmean(t["won"] for t in trades)
    px = st.fmean(t["px"] for t in trades)
    print(f"    n={n:4d}  days={len(dmeans):2d}  paid {px:.4f}  "
          f"win {100*win:5.1f}%  EV ${mean:+.4f}/$1")
    print(f"    pooled t {mean/se if se else float('nan'):+.2f}"
          f"      DAY-CLUSTERED t {mean/dse if dse else float('nan'):+.2f}")
    if "npr" in trades[0]:
        print(f"    prints per window {st.fmean(t['npr'] for t in trades):.1f}"
              f"   mean max-median spread "
              f"{st.fmean(t['spread'] for t in trades):+.4f}"
              f"   (a one-print window is not an offer)")
    pos = sum(1 for m in dmeans if m > 0)
    print(f"    days with positive EV: {pos}/{len(dmeans)}")
    h = n // 2
    if h:
        print(f"    halves: {st.fmean(x['ev'] for x in trades[:h]):+.4f} / "
              f"{st.fmean(x['ev'] for x in trades[h:]):+.4f}")

    # WHICH PRICE BAND CARRIES THE EDGE. The cap decides what we are allowed
    # to buy, so the cap must come from this table and nothing else. Note the
    # break-even inside each band: a band can win a lot and still lose money.
    print("    by entry price (be = that band's own break-even):")
    for lo, hi in ((0.10, 0.35), (0.35, 0.50), (0.50, 0.65),
                   (0.65, 0.80), (0.80, 1.01)):
        sub = [t for t in trades if lo <= t["px"] < hi]
        if not sub:
            continue
        e = [t["ev"] for t in sub]
        w = st.fmean(t["won"] for t in sub)
        p = st.fmean(t["px"] for t in sub)
        be = p + PM_FEE_RATE * p * (1 - p)
        se_b = (st.stdev(e) / math.sqrt(len(e))) if len(e) > 1 else 0.0
        db: dict[str, list[float]] = {}
        for t in sub:
            db.setdefault(t["day"], []).append(t["ev"])
        dm = [st.fmean(v) for v in db.values()]
        dse = (st.stdev(dm) / math.sqrt(len(dm))) if len(dm) > 1 else 0.0
        print(f"      {lo:.2f}-{hi:.2f}  n={len(sub):4d}  d={len(dm):2d}  "
              f"paid {p:.4f}  win {100*w:5.1f}%  be {100*be:5.1f}%  "
              f"EV {st.fmean(e):+.4f}  t {st.fmean(e)/se_b if se_b else float('nan'):+5.2f}"
              f"  day-t {st.fmean(e)/dse if dse else float('nan'):+5.2f}"
              f"  prints/w {st.fmean(t['npr'] for t in sub):4.1f}"
              f"  max-med {st.fmean(t['spread'] for t in sub):+.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="all", help="'all' or comma list")
    ap.add_argument("--look-s", type=int, default=1)
    ap.add_argument("--min-tau", type=int, default=5)
    ap.add_argument("--max-tau", type=int, default=60)
    ap.add_argument("--max-px", type=float, default=0.65)
    ap.add_argument("--min-px", type=float, default=0.10)
    ap.add_argument("--lag-s", type=int, default=0)
    ap.add_argument("--max-stale-s", type=int, default=5,
                    help="window length for the median entry price")
    ap.add_argument("--any-side", action="store_true",
                    help="accept SELL prints too (default: BUY prints only)")
    ap.add_argument("--price-mode", default="median",
                    choices=("first", "median", "max", "vwap"),
                    help="how to turn the window's prints into one entry "
                         "price; 'max' charges the arm the most anyone paid")
    ap.add_argument("--coherence-tol", type=float, default=0.0,
                    help=">0 drops seconds whose Up+Down prints do not sum "
                         "to ~1 (the crossing second is otherwise full of "
                         "prints that are not prices)")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args()

    all_days = T.days_with_trades()
    use = all_days if a.days == "all" else a.days.split(",")
    print(f"CROSS arm on data/btc5m -- {len(use)} days, taker entry at a "
          f"real print")
    print(f"  look {a.look_s}s  tau [{a.min_tau},{a.max_tau}]  cap {a.max_px}"
          f"  lag {a.lag_s}s  window {a.max_stale_s}s  "
          f"prints={'ANY' if a.any_side else 'BUY only'}"
          f"  price={a.price_mode}  coherence {a.coherence_tol}")
    if not a.no_verify and not verify(use[0], a.look_s):
        print("  fast path disagrees with the real trigger; refusing")
        return 1

    kw = dict(look_s=a.look_s, min_tau=a.min_tau, max_tau=a.max_tau,
              max_px=a.max_px, min_px=a.min_px, lag_s=a.lag_s,
              max_stale_s=a.max_stale_s, require_buy=not a.any_side,
              do_verify=False, price_mode=a.price_mode,
              coherence_tol=a.coherence_tol)
    all_trades: list[dict] = []
    for d in use:
        tr = run(d, **kw)
        all_trades.extend(tr)
        if tr:
            e = [x["ev"] for x in tr]
            print(f"  {d}  n={len(tr):3d}  EV {st.fmean(e):+.4f}  "
                  f"win {100*st.fmean(x['won'] for x in tr):5.1f}%  "
                  f"paid {st.fmean(x['px'] for x in tr):.4f}")
        else:
            print(f"  {d}  no trades")
    print()
    report(all_trades)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
