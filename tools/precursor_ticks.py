#!/usr/bin/env python3
"""Do microstructure precursors predict the next 0.5-2s move of the SAME
instrument -- at tick resolution, not 1-second bars?

WHY THIS EXISTS. The earlier "1s tape / 1s momentum" falsifications labelled
5-minute settlements and fed the model 1-second klines. The move this arm has
to front-run completes in 250-600ms, so 1s bars are already the aftermath:
that test could not have seen a precursor even if one exists. This tool redoes
it from the beginning on the raw µs feeds we already have on disk:

    data/bookticker/<SYM>-<DAY>.csv.gz   top-of-book, Tardis format:
        exchange,symbol,timestamp(us),local_timestamp(us),
        ask_amount,ask_price,bid_price,bid_amount
    data/aggTrades/<SYM>-aggTrades-<DAY>.zip   trades, µs timestamps:
        id,price,qty,first_id,last_id,timestamp(us),is_buyer_maker,is_best_match

METHOD. Single streaming merge of the two time-sorted files; a fixed 100ms
grid (the same cadence as the @depth@100ms stream Gemini proposes); features
computed from state at t only; label = forward mid return in bps at
+0.5/1/2s. Reports base rates, IC, AUC and conditional hit rates, plus a
first-half/second-half split and a stride-thinned pass so overlapping labels
cannot fake significance. Read-only: parses files, prints numbers.

FEATURES (all causal):
    qi          top-1 queue imbalance (bq-aq)/(bq+aq)   [5-level needs depth data we don't have]
    qi_d        qi - qi(t-300ms)          -- the derivative Gemini's rule 3 asks for
    cvd500/1s   signed taker volume (aggTrades) in rolling windows
    cvd_d       cvd500 - cvd500(t-300ms)  -- flow ACCELERATION
    abs300      aggressive volume hitting a FIXED quote (absorption proxy)
    bid_d       bid size change over 300ms (the bid wall stacking up)
    tn500       trade count in 500ms (activity)

NOTE ON THE TWO GEMINI "BOOK" SIGNALS. At top-of-book the micro-price
deviation is an IDENTITY, not a second signal: micro - mid = -(spread/2)*qi,
so (micro-mid)/spread == -qi/2 exactly. They separate only with 5-level
depth, which this archive does not have (top-1 only). One column, not two.

TARGETS. The sample's features come from state strictly BEFORE the event
that crosses the grid (no lookahead), and each sample gets two labels: the
forward MID return, and the forward LAST-TRADE-PRICE return -- the latter is
what the venue's own feed prints, so it is the tradeable one.

TARGET. The venue odds reprice when SPOT crosses a level (the rounds' depth
gate is 0.5-5bps), so the useful event is "mid moves >= ~0.5bps within the
horizon", not a bare up/down sign.

USAGE
    python3 tools/precursor_ticks.py --symbols BTCUSDT,ETHUSDT,BNBUSDT \
        --day 2026-09-01 --start-hour 0 --end-hour 8
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import heapq
import math
import zipfile

GRID_US = 100_000                      # 10 Hz sample grid (see @depth@100ms)
HORIZONS_S = (0.5, 1.0, 2.0, 5.0)      # 5.0s = does the move persist?
EDGE_BPS = (0.5, 1.0)                  # "cross-worthy" move thresholds
HMAX_US = int(max(HORIZONS_S) * 1e6)
W500_US = 500_000
W1000_US = 1_000_000
W300_US = 300_000
KEEP_US = HMAX_US + 3_000_000          # retention must exceed HMAX
# Lookbacks for the clean event test: the book state this long BEFORE a mid
# tick -- Gemini's "200-800ms ahead". Each mid PRICE CHANGE is one event.
EVENT_LOOKBACKS_US = (100_000, 300_000, 800_000)
DATA = "data"


def read_books(sym: str, day: str, t0: int, t1: int):
    path = f"{DATA}/bookticker/{sym}-{day}.csv.gz"
    with gzip.open(path, "rt") as fh:
        fh.readline()                                  # header row
        for line in fh:
            f = line.split(",")
            if len(f) < 8:
                continue
            ts = int(f[2])
            if ts < t0:
                continue
            if ts > t1:
                return
            # ask_amount, ask_price, bid_price, bid_amount
            yield ts, float(f[4]), float(f[5]), float(f[6]), float(f[7])


def read_trades(sym: str, day: str, t0: int, t1: int):
    path = f"{DATA}/aggTrades/{sym}-aggTrades-{day}.zip"
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as raw:
            for line in raw:
                f = line.decode().split(",")
                if len(f) < 7:
                    continue
                ts = int(f[5])
                if ts < t0:
                    continue
                if ts > t1:
                    return
                qty = float(f[2])
                # is_buyer_maker=True -> the taker was a SELLER
                yield ts, (-qty if f[6] == "True" else qty), float(f[1])


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        dy = y - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def auc(labels, scores):
    """Rank (Mann-Whitney) AUC -- no sklearn needed."""
    pairs = sorted(zip(scores, labels))
    n = len(pairs)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    npos = sum(1 for _, l in pairs if l)
    nneg = n - npos
    if npos == 0 or nneg == 0:
        return float("nan")
    rank_sum = sum(r for r, (_, l) in zip(ranks, pairs) if l)
    return (rank_sum - npos * (npos + 1) / 2.0) / (npos * nneg)


def pct(x):
    return f"{100 * x:5.1f}%"


def run_symbol(sym: str, day: str, t0: int, t1: int) -> list[dict]:
    """One streaming pass -> list of samples (features + forward returns)."""
    books = read_books(sym, day, t0, t1)
    try:
        trades = read_trades(sym, day, t0, t1)
    except FileNotFoundError:
        print(f"   [no aggTrades file for {sym} {day}: flow features inert]")
        trades = iter(())
    merged = heapq.merge(books, trades, key=lambda r: r[0])

    mid_ts: list[int] = []
    mid_px: list[float] = []
    tr_ts: list[int] = []
    tr_sg: list[float] = []
    tr_px: list[float] = []
    ask_ch: list[int] = []
    bid_ch: list[int] = []

    a = b = 0.0
    aq = bq = 0.0
    prev_ask = prev_bid = -1.0
    grid = (t0 // GRID_US + 1) * GRID_US
    hist: list[dict] = []           # recent states, by grid time
    hist_t: list[int] = []          # parallel timestamps for bisect
    pending: list[dict] = []
    out: list[dict] = []
    events: list[dict] = []         # one row per MID PRICE CHANGE
    prev_mid = 0.0

    def state(now: int) -> dict:
        """Features from state at `now` (past-only) + qi/cvd history."""
        if aq + bq > 0:
            qi = (bq - aq) / (bq + aq)
            micro = (b * bq + a * aq) / (bq + aq)
        else:
            qi, micro = 0.0, (a + b) / 2.0
        mid = (a + b) / 2.0
        spread = a - b
        micro_dev = (micro - mid) / spread if spread > 0 else 0.0
        lo500 = bisect.bisect_left(tr_ts, now - W500_US)
        lo1000 = bisect.bisect_left(tr_ts, now - W1000_US)
        cvd500 = math.fsum(tr_sg[lo500:])
        cvd1000 = math.fsum(tr_sg[lo1000:])
        buy300 = math.fsum(v for v in tr_sg[bisect.bisect_left(tr_ts, now - W300_US):] if v > 0)
        sell300 = math.fsum(v for v in tr_sg[bisect.bisect_left(tr_ts, now - W300_US):] if v < 0)
        ask_fixed = bisect.bisect_right(ask_ch, now - W300_US) == len(ask_ch)
        bid_fixed = bisect.bisect_right(bid_ch, now - W300_US) == len(bid_ch)
        # absorption: aggression eating a quote that refuses to move
        absorption = (buy300 if ask_fixed else 0.0) + (sell300 if bid_fixed else 0.0)
        old = None
        for h in reversed(hist):
            if h["t"] <= now - W300_US:
                old = h
                break
        qi_d = qi - old["qi"] if old else 0.0
        cvd_d = cvd500 - old["cvd500"] if old else 0.0
        bid_d = bq - old["bq"] if old else 0.0
        tn500 = len(tr_ts) - lo500
        return dict(t=now, mid=mid, qi=qi, qi_d=qi_d, micro_dev=micro_dev,
                    cvd500=cvd500, cvd1000=cvd1000, cvd_d=cvd_d,
                    absorption=absorption, bid_d=bid_d, tn500=tn500, bq=bq)

    for ev in merged:
        ts = ev[0]
        # sample the grid STRICTLY before this event, from the PRE-event state
        while grid <= ts:
            if b > 0 and a > 0:
                s = state(grid)
                hist.append(s)
                hist_t.append(s["t"])
                pending.append(s)
            grid += GRID_US

        if len(ev) == 5:                       # book update
            _, aq, a, b, bq = ev
            if a != prev_ask:
                ask_ch.append(ts)
                prev_ask = a
            if b != prev_bid:
                bid_ch.append(ts)
                prev_bid = b
            mid_now = (a + b) / 2.0
            if prev_mid and mid_now != prev_mid and hist:
                # THE CLEAN TEST: a mid tick just happened. What did the book
                # look like 100/300/800ms BEFORE it? One event per tick, no
                # overlapping labels, no regime accumulation.
                for lb in EVENT_LOOKBACKS_US:
                    i = bisect.bisect_right(hist_t, ts - lb) - 1
                    if i < 0:
                        continue
                    s0 = hist[i]
                    events.append(dict(
                        lb=lb,
                        up=mid_now > prev_mid,
                        size_bps=abs(mid_now - prev_mid) / prev_mid * 1e4,
                        **{k: s0[k] for k in ("qi", "qi_d", "cvd500", "cvd_d",
                                              "absorption", "bid_d")}))
            prev_mid = mid_now
            mid_ts.append(ts)
            mid_px.append(mid_now)
        else:                                  # trade
            tr_ts.append(ts)
            tr_sg.append(ev[1])
            tr_px.append(ev[2])

        # resolve labels whose full horizon is now available
        if mid_ts:
            while pending and pending[0]["t"] + HMAX_US <= mid_ts[-1]:
                s = pending.pop(0)
                row = dict(s)
                j0 = bisect.bisect_right(tr_ts, s["t"]) - 1
                p0 = tr_px[j0] if j0 >= 0 else None
                for h in HORIZONS_S:
                    i = bisect.bisect_left(mid_ts, s["t"] + int(h * 1e6))
                    row[f"r{h}"] = ((mid_px[i] - s["mid"]) / s["mid"] * 1e4
                                    if i < len(mid_ts) else None)
                    k = bisect.bisect_left(tr_ts, s["t"] + int(h * 1e6))
                    row[f"t{h}"] = ((tr_px[k] - p0) / p0 * 1e4
                                    if p0 and k < len(tr_ts) else None)
                out.append(row)
            # prune state
            cut = mid_ts[-1] - KEEP_US
            i = bisect.bisect_left(mid_ts, cut)
            if i:
                del mid_ts[:i]
                del mid_px[:i]
            j = bisect.bisect_left(tr_ts, cut)
            if j:
                del tr_ts[:j]
                del tr_sg[:j]
                del tr_px[:j]
            k = bisect.bisect_left(ask_ch, cut)
            if k:
                del ask_ch[:k]
            k = bisect.bisect_left(bid_ch, cut)
            if k:
                del bid_ch[:k]
            if len(hist) > 400:                # prune rarely; keep the lookback alive
                hist = [h for h in hist if h["t"] >= cut]
                hist_t = [h["t"] for h in hist]
    return out, events


FEATURES = ("qi", "qi_d", "cvd500", "cvd1000", "cvd_d", "absorption",
            "bid_d", "tn500")


def report(sym: str, day: str, rows: list[dict], events: list[dict]) -> None:
    print(f"\n=== {sym} {day} -- {len(rows):,} samples @100ms, "
          f"{len(events):,} tick events ===")
    if len(rows) < 1000:
        print("   too few samples"); return

    # ---- THE CLEAN TEST: one label per actual mid tick, no overlaps
    if events:
        print("   next mid TICK (non-overlapping): direction vs book at t-lb")
        for lb in EVENT_LOOKBACKS_US:
            sub = [x for x in events if x["lb"] == lb]
            if len(sub) < 200:
                continue
            bu = sum(1 for x in sub if x["up"]) / len(sub)
            line = ("      lb=%4dms n=%6d  P(up)=%s" % (lb // 1000, len(sub), pct(bu)))
            for thr in (0.5, 0.65, 0.8):
                hi = [x for x in sub if x["qi"] > thr]
                lo = [x for x in sub if x["qi"] < -thr]
                if len(hi) >= 50:
                    ph = sum(1 for x in hi if x["up"]) / len(hi)
                    pl = (sum(1 for x in lo if x["up"]) / len(lo)) if len(lo) >= 50 else None
                    line += "\n         qi>%+.2f n=%5d P(up)=%s   qi<%+.2f P(up)=%s" % (
                        thr, len(hi), pct(ph), -thr,
                        pct(pl) if pl is not None else "n/a")
            big = [x for x in sub if x["size_bps"] >= 0.5]
            if len(big) >= 50:
                line += "\n         big ticks (>=0.5bps) n=%d: P(up)=%s" % (
                    len(big), pct(sum(1 for x in big if x["up"]) / len(big)))
            print(line)

    # ---- how big are the moves we are trying to front-run?
    print("   move size: P(|r| >= edge) and median |r|")
    for h in HORIZONS_S:
        rs = [abs(r[f"r{h}"]) for r in rows if r[f"r{h}"] is not None]
        if not rs:
            continue
        rs.sort()
        line = "      %4.1fs  n=%6d  " % (h, len(rs))
        for e in EDGE_BPS:
            line += f"P>= {e}bps {pct(sum(1 for v in rs if v >= e) / len(rs))}  "
        print(line + f"median {rs[len(rs) // 2]:.2f}bps")

    # ---- IC (feature vs forward return), full grid and stride-thinned
    print("   IC(feature, forward mid return bps)   [thinned = non-overlapping]")
    hdr = "      %-11s" % "feature"
    for h in HORIZONS_S:
        hdr += "  %5.1fs" % h
    print(hdr + "   (thinned 1.0s)")
    for f in FEATURES:
        full, thin = [], []
        stride = max(1, int(1.0 / (GRID_US / 1e6)))          # 1.0s stride
        for i, r in enumerate(rows):
            v = r.get(f)
            r1 = r.get("r1.0")
            if v is None or r1 is None:
                continue
            full.append((v, r1))
            if i % stride == 0:
                thin.append((v, r1))
        cells = ""
        for h in HORIZONS_S:
            xy = [(r[f], r[f"r{h}"]) for r in rows
                  if r[f"r{h}"] is not None and r.get(f) is not None]
            cells += "  %+6.3f" % (pearson([p[0] for p in xy], [p[1] for p in xy])
                                   if len(xy) > 10 else 0.0)
        t = pearson([p[0] for p in thin], [p[1] for p in thin]) if len(thin) > 10 else 0.0
        print("      %-11s%s   %+6.3f" % (f, cells, t))

    # ---- AUC for "mid moves >= 0.5bps within the horizon"
    e = EDGE_BPS[0]
    print(f"   AUC(up >= {e}bps) / AUC(down >= {e}bps), 1.0s horizon")
    for f in FEATURES:
        xy = [(r.get(f), r["r1.0"]) for r in rows if r["r1.0"] is not None]
        if len(xy) < 100:
            continue
        xs = [p[0] for p in xy]
        up = [p[1] >= e for p in xy]
        dn = [p[1] <= -e for p in xy]
        print("      %-11s up %5.3f   down %5.3f" %
              (f, auc(up, xs), auc(dn, [-x for x in xs])))

    # ---- conditional hit rates (the Gemini rules, as stated)
    base = {h: sum(1 for r in rows if r[f"r{h}"] is not None and r[f"r{h}"] >= e)
            / max(1, sum(1 for r in rows if r[f"r{h}"] is not None))
            for h in HORIZONS_S}
    print(f"   P(up >= {e}bps) conditional on trigger   (base rate shown)")
    trigs = {
        "qi>+0.65": lambda r: r["qi"] > 0.65,
        "qi<-0.65": lambda r: r["qi"] < -0.65,
        "micro_dev>+0.3": lambda r: r["micro_dev"] > 0.3,
        "cvd500>0 & cvd_d>0": lambda r: r["cvd500"] > 0 and r["cvd_d"] > 0,
        "absorb>+1btc": lambda r: r["absorption"] > 1.0,
        "COMPOSITE": lambda r: (r["qi"] > 0.65 and r["cvd500"] > 0
                                and r["cvd_d"] > 0),
    }
    hdr = "      %-20s" % "trigger"
    for h in HORIZONS_S:
        hdr += "  %5.1fs(n)" % h
    print(hdr)
    line = "      %-20s" % "(base rate)"
    for h in HORIZONS_S:
        line += "  %9s" % pct(base[h])
    print(line)
    for name, fn in trigs.items():
        line = "      %-20s" % name
        for h in HORIZONS_S:
            sub = [r for r in rows if r[f"r{h}"] is not None and fn(r)]
            if len(sub) < 50:
                line += "  %9s" % "n/a"
            else:
                hit = sum(1 for r in sub if r[f"r{h}"] >= e) / len(sub)
                line += "  %9s" % ("%s(%d)" % (pct(hit), len(sub)))
        print(line)

    # ---- the TRADEABLE target: the last trade price (what a venue feed prints)
    bt_rows = [r for r in rows if r.get("t1.0") is not None]
    if len(bt_rows) > 100:
        bt = sum(1 for r in bt_rows if r["t1.0"] >= e) / len(bt_rows)
        print(f"   TRADE-price target (1.0s): base P(up >= {e}bps) = {pct(bt)} "
              f"(n={len(bt_rows):,})")
        for f in ("qi", "cvd500", "cvd_d", "bid_d"):
            xy = [(r[f], r["t1.0"]) for r in bt_rows]
            print("      IC %-8s %+0.3f" %
                  (f, pearson([p[0] for p in xy], [p[1] for p in xy])))
        for name, fn in (("qi>+0.65", lambda r: r["qi"] > 0.65),
                         ("COMPOSITE", lambda r: (r["qi"] > 0.65
                                                  and r["cvd500"] > 0
                                                  and r["cvd_d"] > 0))):
            sub = [r for r in bt_rows if fn(r)]
            if len(sub) >= 50:
                hit = sum(1 for r in sub if r["t1.0"] >= e) / len(sub)
                print("      %-10s %s (n=%d)  lift %+.3f" %
                      (name, pct(hit), len(sub), hit - bt))

    # ---- split-half stability of the composite (1.0s)
    half = len(rows) // 2
    for label, part in (("1st half", rows[:half]), ("2nd half", rows[half:])):
        sub = [r for r in part if r["r1.0"] is not None and trigs["COMPOSITE"](r)]
        allp = [r for r in part if r["r1.0"] is not None]
        if len(sub) >= 50 and allp:
            hit = sum(1 for r in sub if r["r1.0"] >= e) / len(sub)
            b = sum(1 for r in allp if r["r1.0"] >= e) / len(allp)
            print(f"      composite {label}: {pct(hit)} (n={len(sub)}) "
                  f"vs base {pct(b)}  lift {hit - b:+.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT")
    ap.add_argument("--day", default="2026-09-01")
    ap.add_argument("--start-hour", type=int, default=0)
    ap.add_argument("--end-hour", type=int, default=8)
    args = ap.parse_args()

    day0 = None
    # UTC day start from the day string, done by arithmetic on the file clock
    import datetime as dt
    d = dt.datetime.strptime(args.day + " 00:00:00", "%Y-%m-%d %H:%M:%S")
    day0 = int(d.replace(tzinfo=dt.timezone.utc).timestamp() * 1e6)
    t0 = day0 + args.start_hour * 3_600_000_000
    t1 = day0 + args.end_hour * 3_600_000_000

    for sym in args.symbols.split(","):
        sym = sym.strip()
        try:
            rows, events = run_symbol(sym, args.day, t0, t1)
        except FileNotFoundError as e:
            print(f"{sym}: missing file -- {e}")
            continue
        report(sym, args.day, rows, events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
