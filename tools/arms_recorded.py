"""Backtest the live arms (impulse, cross, model) and every combination.

ON THE BOT'S OWN RECORDED BOOK -- which is the only instrument that can test the
momentum arms. `data/btc5m/*` (Polymarket) cannot: its "prices" are per-second
averages of EXECUTED trades, and in that data the buy-side average sits at or
below the sell-side average on 44.5% of momentum entries, which no book can do.
An arm whose whole mechanism is "we are faster than the price" cannot be scored
against a stale print average -- it scores +32% to +69% per $1 there, against
+8-21% measured live on 96,800 ticks.

Here the quotes are the venue's real book (up_bid/up_ask/down_bid/down_ask, 0
crossed of 539,323), with the venue's own strike and settlement, so every arm is
priced at an ask that actually existed.

Execution, identical for all arms: fill at the ASK of the side chosen, fee from
the shipped curve, $1 flat stake. One position per round for impulse/model (they
share the `guard.check(slug)` dedup); cross may take `cross_max_per_round`.

Usage:
    python -m tools.arms_recorded
    python -m tools.arms_recorded --db data/btcpredict.db
"""
from __future__ import annotations

import argparse
import itertools
import math
import os
import sqlite3

import numpy as np
import pandas as pd

from btcpredict.config import settings as S
from btcpredict.edge.impulse import impulse_bps, impulse_side
from btcpredict.edge.signal import evaluate, fee_rate_for_price
from btcpredict.main import crossed_side
from btcpredict.pricing.digital import fair_value
from btcpredict.pricing.vol import VolEstimator

DEFAULT_DB = ("/tmp/claude-1000/-var-home-abdou-Documents-BinancePrediction/"
              "2631457b-390c-4726-81f1-8af14c73be89/scratchpad/vps.db")


def load_rounds(c) -> list[dict]:
    q = """select slug, start_ms, end_ms, strike, winner from rounds
           where winner is not null and strike is not null
           order by start_ms"""
    return [{"slug": s, "start_ms": a, "end_ms": b, "strike": k, "winner": w}
            for s, a, b, k, w in c.execute(q)]


def load_quotes(c) -> dict:
    q = """select slug, ts_ms, tau_s, spot, up_price, down_price,
                  up_bid, up_ask, down_bid, down_ask
           from quotes where spot is not null and up_ask is not null
             and down_ask is not null and strike is not null
           order by slug, ts_ms"""
    out: dict[str, list] = {}
    for row in c.execute(q):
        out.setdefault(row[0], []).append(row[1:])
    return out


def sigma_by_bar(c) -> tuple:
    """Replay the shipped VolEstimator over the recorded 1m bars.

    Mirrors live: close-to-close log returns, hour-aware, fed in order, with
    seasonality recomputed as the day rolls. Returns (bar_open_ms[], sigma[])
    where sigma[i] is the estimate AFTER bar i closed -- i.e. what a decision
    taken during the next bar would have seen.
    """
    bars = list(c.execute("select open_ms, close from bars where symbol='BTCUSDT'"
                          " order by open_ms"))
    vol = VolEstimator()
    opens, sig = [], []
    prev, prev_day = None, None
    for ts, cl in bars:
        day = ts // 86400000
        if day != prev_day:
            vol._recompute_seasonality()
            prev_day = day
        if prev and prev > 0 and cl > 0:
            vol.add(math.log(cl / prev), hour=int((ts // 3600000) % 24))
        prev = cl
        opens.append(ts)
        sig.append(vol.sigma_1m(hour=int((ts // 3600000) % 24)))
    return np.array(opens, dtype=np.int64), np.array(sig)


def sigma_at(opens: np.ndarray, sig: np.ndarray, ts_ms: int) -> float:
    k = int(np.searchsorted(opens, ts_ms, side="right")) - 1
    if k < 0:
        return float(S.vol_floor_1m)
    return float(sig[k])


def arm_impulse(rows: list, book: np.ndarray) -> list[dict]:
    out = []
    for i in range(len(book)):
        ts, tau, spot = int(book[i][0]), float(book[i][1]), float(book[i][2])
        if not (S.min_tau_s <= tau <= S.max_tau_s):
            continue
        ticks = [(int(book[j][0]), float(book[j][2])) for j in range(i + 1)]
        bps = impulse_bps(ticks, S.impulse_window_s)
        side = impulse_side(bps, S.impulse_bps)
        if side is None:
            continue
        ask = float(book[i][5]) if side == "Up" else float(book[i][8])
        if not (S.impulse_min_price <= ask <= S.impulse_max_price):
            continue
        out.append({"arm": "impulse", "tau": tau, "side": side, "px": ask,
                    "fee": fee_rate_for_price(ask)})
    return out


def arm_cross(rows: list, book: np.ndarray, strike: float) -> list[dict]:
    out = []
    for i in range(len(book)):
        tau, spot = float(book[i][1]), float(book[i][2])
        if not (S.cross_min_tau_s <= tau <= S.cross_max_tau_s):
            continue
        ticks = [(int(book[j][0]), float(book[j][2])) for j in range(i + 1)]
        side = crossed_side(ticks, strike, S.cross_look_s)
        if side is None:
            continue
        if 1e4 * abs(spot - strike) / strike > S.cross_max_depth_bps:
            continue
        ask = float(book[i][5]) if side == "Up" else float(book[i][8])
        if not (S.cross_min_price <= ask <= S.cross_max_price):
            continue
        out.append({"arm": "cross", "tau": tau, "side": side, "px": ask,
                    "fee": fee_rate_for_price(ask)})
    return out


def arm_model(book: np.ndarray, strike: float, opens, sig) -> list[dict]:
    out = []
    for i in range(len(book)):
        ts, tau, spot = int(book[i][0]), float(book[i][1]), float(book[i][2])
        if not (S.min_tau_s <= tau <= S.max_tau_s) or tau <= 0:
            continue
        up_disp, dn_disp = float(book[i][3]), float(book[i][4])
        up_ask, dn_ask = float(book[i][5]), float(book[i][8])
        if not (0 < up_disp < 1 and 0 < dn_disp < 1):
            continue
        sg = sigma_at(opens, sig, ts)
        fair = far_guard(spot, strike, tau, sg)
        if fair is None:
            continue
        s = evaluate(fair, up_disp, dn_disp, half_spread=S.assumed_half_spread,
                     safety_margin=S.safety_margin, min_edge=S.min_edge,
                     min_quote=S.min_quote, max_quote=S.max_quote,
                     edge_haircut=S.edge_haircut)
        if not s.act or s.side is None:
            continue
        ask = up_ask if s.side == "Up" else dn_ask
        if not (S.min_quote <= ask <= S.max_quote):
            continue
        out.append({"arm": "model", "tau": tau, "side": s.side, "px": ask,
                    "fee": fee_rate_for_price(ask)})
    return out


def far_guard(spot: float, strike: float, tau: float, sg: float):
    try:
        return float(fair_value(spot, strike, tau, sg))
    except Exception:
        return None


def apply_positions(df: pd.DataFrame) -> pd.DataFrame:
    """impulse and model share the round dedup; cross keeps its own quota."""
    parts = []
    for _slug, g in df.groupby("slug"):
        main = g[g.arm.isin(("impulse", "model"))]
        if not main.empty:
            parts.append(main.sort_values("tau", ascending=False).head(1))
        x = g[g.arm == "cross"]
        if not x.empty:
            parts.append(x.sort_values("tau", ascending=False)
                         .head(S.cross_max_per_round))
    if not parts:
        return df.iloc[0:0]
    return pd.concat(parts).sort_values(["day", "slug", "tau"],
                                        ascending=[True, True, False])


def run_days(df: pd.DataFrame, flat: float = 1.0) -> tuple:
    bank, per = 50.0, []
    for _d, g in df.groupby("day", sort=True):
        net, cap = 0.0, S.daily_loss_cap_pct * bank
        for px, fee, won in zip(g.px.to_numpy(), g.fee.to_numpy(),
                                g.won.to_numpy()):
            if net <= -cap:
                break
            sh = flat / px
            cost = px * (1 + fee) * sh
            p = (sh - cost) if won else -cost
            bank += p
            net += p
        per.append(net)
    return bank, per


def report(label: str, df: pd.DataFrame, days: int) -> dict:
    if df.empty:
        print(f"  {label:<22}{'-- no fills --':>26}")
        return {}
    r = (df.won.to_numpy() / df.px.to_numpy()) - (1 + df.fee.to_numpy())
    byd = pd.Series(r).groupby(df.day.to_numpy()).mean()
    mid = float(np.median(sorted(df.day.unique())))
    h1 = byd[byd.index <= mid].mean()
    h2 = byd[byd.index > mid].mean()
    drop = byd.drop(byd.idxmax()).mean() if len(byd) > 1 else float("nan")
    dt = (byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd)))
          if len(byd) > 1 and byd.std(ddof=1) else 0.0)
    bank, per = run_days(df)
    v = np.array(per)
    print(f"  {label:<22}{len(df):>6}{len(df)/days:>7.1f}{df.won.mean():>7.1%}"
          f"{df.px.mean():>7.3f}{r.mean():>9.4f}{dt:>7.2f}{bank-50.0:>9.2f}"
          f"{h1:>9.4f}{h2:>9.4f}{drop:>9.4f}{(v < 0).sum():>5}/{len(v)}")
    return {"n": len(df), "ev": r.mean(), "t": dt, "tot": bank - 50.0,
            "h1": h1, "h2": h2, "drop": drop, "day": byd}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB if os.path.exists(DEFAULT_DB)
                    else "data/btcpredict.db")
    ap.add_argument("--flat", type=float, default=1.0)
    args = ap.parse_args()
    c = sqlite3.connect(args.db)
    rounds = load_rounds(c)
    quotes = load_quotes(c)
    opens, sig = sigma_by_bar(c)
    rounds = [r for r in rounds if r["slug"] in quotes]
    print(f"{args.db}\n  {len(rounds):,} settled rounds with quotes | "
          f"{sum(len(quotes[r['slug']]) for r in rounds):,} book rows | "
          f"sigma from {len(opens):,} 1m bars")
    print(f"  bands: impulse [{S.impulse_min_price},{S.impulse_max_price}] "
          f"cross [{S.cross_min_price},{S.cross_max_price}] "
          f"model [{S.min_quote},{S.max_quote}] | tau: impulse/model "
          f"[{S.min_tau_s:.0f},{S.max_tau_s:.0f}] cross "
          f"[{S.cross_min_tau_s:.0f},{S.cross_max_tau_s:.0f}]\n")

    rows = []
    for r in rounds:
        book = np.array(quotes[r["slug"]], dtype=object)
        book = np.array(sorted(book, key=lambda x: x[0]), dtype=object)
        day = int(r["start_ms"] // 86400000)
        for d in (arm_impulse(r, book) + arm_cross(r, book, r["strike"])
                  + arm_model(book, r["strike"], opens, sig)):
            d["slug"] = r["slug"]
            d["day"] = day
            d["won"] = int(r["winner"] == d["side"])
            rows.append(d)
    if not rows:
        raise SystemExit("no entries generated")
    df = pd.DataFrame(rows)
    days = df.day.nunique()

    hdr = (f"  {'arm':<22}{'fills':>6}{'/day':>7}{'WR':>7}{'px':>7}{'EV/$':>9}"
           f"{'day-t':>7}{'tot$':>9}{'half1':>9}{'half2':>9}{'dropbest':>9}"
           f"{'neg':>7}")
    print("=== EACH ARM ALONE (live position rule) ===")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    stats = {}
    for arm in ("impulse", "cross", "model"):
        stats[(arm,)] = report(arm, apply_positions(df[df.arm == arm]), days)

    print("\n=== EVERY COMBINATION ===")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    combos = {}
    for k in (2, 3):
        for arms in itertools.combinations(("impulse", "cross", "model"), k):
            sub = df[df.arm.isin(arms)]
            combos[arms] = report("+".join(arms), apply_positions(sub), days)

    print(f"\n=== RANKED BY TOTAL $ OVER {days} DAYS ===")
    allc = {**stats, **combos}
    for arms, st in sorted(allc.items(), key=lambda kv: -kv[1].get("tot", -9e9)):
        print(f"  {'+'.join(arms):<24} tot ${st['tot']:7.2f}  EV/$ {st['ev']:+.4f}"
              f"  day-t {st['t']:+.2f}  halves {st['h1']:+.4f}/{st['h2']:+.4f}"
              f"  drop-best {st['drop']:+.4f}  fills {st['n']}")

    print("\n=== PARAMETER STABILITY: best combo IN EACH HALF ALONE ===")
    mid = float(np.median(sorted(df.day.unique())))
    for lab, sel in (("first half", lambda d: d.day <= mid),
                     ("second half", lambda d: d.day > mid)):
        best, bs = None, -9e9
        for arms, _st in allc.items():
            d = apply_positions(df[df.arm.isin(arms)])
            d = d[sel(d)]
            if len(d) < 5:
                continue
            r = (d.won.to_numpy() / d.px.to_numpy()) - (1 + d.fee.to_numpy())
            if r.mean() > bs:
                best, bs = arms, r.mean()
        print(f"  {lab:<12} best EV/$ {bs:+.4f} -> "
              f"{'+'.join(best) if best else '-'}")


if __name__ == "__main__":
    main()
