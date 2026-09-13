"""Backtest the live ARMS (impulse, cross, model) and their combinations.

Same corpus and same execution model as `tools/corpus_trade.py`, but instead of
scoring one pricing model it replays the three arms the bot actually runs, using
their own trigger functions:

  impulse  `btcpredict.edge.impulse.impulse_bps/impulse_side`  -- the underlying
           moved >= `impulse_bps` over `impulse_window_s`, buy that side.
  cross    `btcpredict.main.crossed_side`                      -- the spot just
           crossed the strike, buy the side it crossed INTO, but only while the
           crossing is a "nick" (`cross_max_depth_bps`).
  model    the shipped pricing model's fair value vs the venue quote through the
           real `evaluate` gate.

Execution: every arm's entry is priced identically -- our venue's modelled taker
cost (`assumed_half_spread` + the 200bps-capped fee curve) on top of the market
mid -- so the comparison is about the TRIGGERS, not about which arm got a better
fill. `won` is the true settlement.

Positions per round follow live: impulse and model each claim the round (the
`guard.check(slug)` dedup), so at most ONE of them fires; cross may take up to
`cross_max_per_round`. Combinations are therefore unions under that rule.

Usage:
    python -m tools.arms_backtest                      # all arms + every combo
    python -m tools.arms_backtest --cache /tmp/tc2.pkl --pred /tmp/pred.pkl
"""
from __future__ import annotations

import argparse
import itertools
import math
import os
import pickle

import numpy as np
import pandas as pd

from btcpredict.config import settings as S
from btcpredict.edge.impulse import impulse_bps, impulse_side
from btcpredict.edge.signal import evaluate, fee_rate_for_price
from btcpredict.main import crossed_side

PRICES = "data/btc5m_prices.parquet"
WINDOW_S = 300


def load_prices() -> dict:
    p = pd.read_parquet(PRICES, columns=["window_id", "tau_s", "up_px", "dn_px"])
    p["epoch"] = p.window_id.str.split("/").str[1].astype(int)
    out = {}
    for e, g in p.groupby("epoch"):
        g = g.sort_values("tau_s")
        out[int(e)] = (g.tau_s.to_numpy(), g.up_px.to_numpy(), g.dn_px.to_numpy())
    return out


def quote_mid(P: dict, e: int, tau: float):
    """Mid at or before tau (strictly causal). -> (up_px, dn_px) or None."""
    if e not in P:
        return None
    ts, up, dn = P[e]
    k = int(np.searchsorted(ts, tau, side="right")) - 1
    if k < 0 or tau - ts[k] > 30.0:
        return None
    if not (np.isfinite(up[k]) and np.isfinite(dn[k])):
        return None
    return float(up[k]), float(dn[k])


def fill(side: str, up: float, dn: float) -> tuple[float, float]:
    """(price paid, fee rate) for a taker entry on `side`."""
    qeff = min(0.999, (up if side == "Up" else dn) + S.assumed_half_spread)
    return qeff, fee_rate_for_price(qeff)


def ticks_upto(r: dict, i: int) -> list[tuple[int, float]]:
    """Chronological (ts_ms, price) for tape indices 0..i (inclusive)."""
    base = r["start_ms"]
    close = r["close"]
    return [(base + j * 1000, float(close[j])) for j in range(i + 1)]


def arm_impulse(r: dict, P: dict, e: int) -> list[dict]:
    """Fire on every tape second that clears the impulse threshold."""
    out = []
    close = r["close"]
    n = len(close)
    for i in range(2, n):
        tau = WINDOW_S - i - 1
        if not (S.min_tau_s <= tau <= S.max_tau_s):
            continue
        bps = impulse_bps(ticks_upto(r, i), S.impulse_window_s)
        side = impulse_side(bps, S.impulse_bps)
        if side is None:
            continue
        q = quote_mid(P, e, tau)
        if q is None:
            continue
        px, fee = fill(side, *q)
        if not (S.impulse_min_price <= px <= S.impulse_max_price):
            continue
        out.append({"arm": "impulse", "tau": tau, "side": side, "px": px,
                    "fee": fee, "won": int(r["y"] == (1 if side == "Up" else 0))})
    return out


def arm_cross(r: dict, P: dict, e: int) -> list[dict]:
    """Buy the side the spot just crossed into, while it is still a nick."""
    out = []
    close = r["close"]
    strike = r["strike"]
    if not strike or strike <= 0:
        return out
    n = len(close)
    for i in range(2, n):
        tau = WINDOW_S - i - 1
        if not (S.cross_min_tau_s <= tau <= S.cross_max_tau_s):
            continue
        side = crossed_side(ticks_upto(r, i), strike, S.cross_look_s)
        if side is None:
            continue
        depth = 1e4 * abs(float(close[i]) - strike) / strike
        if depth > S.cross_max_depth_bps:
            continue
        q = quote_mid(P, e, tau)
        if q is None:
            continue
        px, fee = fill(side, *q)
        if not (S.cross_min_price <= px <= S.cross_max_price):
            continue
        out.append({"arm": "cross", "tau": tau, "side": side, "px": px,
                    "fee": fee, "won": int(r["y"] == (1 if side == "Up" else 0))})
    return out


def arm_model(r: dict, P: dict, e: int, pred: dict) -> list[dict]:
    """The shipped model's fair value through the live gate."""
    out = []
    p = np.array(pred["p"]); em = np.array(pred["e"])
    ix = np.array(pred["i"]); tt = np.array(pred["t"])
    m = ix == r["_pos"]
    for j in np.nonzero(m)[0]:
        if not em[j]:
            continue
        tau = float(tt[j])
        if not (S.min_tau_s <= tau <= S.max_tau_s):
            continue
        q = quote_mid(P, e, tau)
        if q is None:
            continue
        up, dn = q
        sig = evaluate(float(p[j]), up, dn, half_spread=S.assumed_half_spread,
                       safety_margin=S.safety_margin, min_edge=S.min_edge,
                       min_quote=S.min_quote, max_quote=S.max_quote,
                       edge_haircut=S.edge_haircut)
        if not sig.act or sig.side is None:
            continue
        px, fee = fill(sig.side, up, dn)
        out.append({"arm": "model", "tau": tau, "side": sig.side, "px": px,
                    "fee": fee,
                    "won": int(r["y"] == (1 if sig.side == "Up" else 0))})
    return out


def apply_live_positions(df: pd.DataFrame, arms: tuple) -> pd.DataFrame:
    """Rebuild the live position rule for this combination of arms.

    impulse and model each claim the round (guard.check dedups by slug), so at
    most one of them fires per round -- the earliest decision wins. Cross keeps
    up to `cross_max_per_round` entries of its own, alternating sides.
    """
    keep = []
    for _round, g in df.groupby("epoch"):
        sub = g[g.arm.isin(arms)]
        if sub.empty:
            continue
        main = sub[sub.arm.isin(("impulse", "model"))]
        if not main.empty:
            # .head(1), not .iloc[0]: mixing a Series with the namedtuples the
            # cross branch used to append made pd.DataFrame() read a tuple's
            # .index METHOD as an axis (pandas >=2.2 raises on it). Both
            # branches now append frames, so the concat is unambiguous.
            keep.append(main.sort_values("tau", ascending=False).head(1))
        x = sub[sub.arm == "cross"]
        if not x.empty:
            keep.append(x.sort_values("tau", ascending=False)
                        .head(S.cross_max_per_round))
    if not keep:
        return df.iloc[0:0]
    out = pd.concat(keep, ignore_index=True)
    return out.sort_values(["day", "epoch", "tau"], ascending=[True, True, False])


def run_days(df: pd.DataFrame, flat: float = 1.0) -> tuple:
    """Day-scoped loss cap, $1 flat stake. -> (final bank, per-day pnl list)."""
    bank, per = 50.0, []
    for _day, g in df.groupby("day", sort=True):
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


def stats(label: str, df: pd.DataFrame, days: int) -> dict:
    if df.empty:
        print(f"  {label:<22}{'-- no fills --':>30}")
        return {}
    r = (df.won.to_numpy() / df.px.to_numpy()) - (1 + df.fee.to_numpy())
    byd = pd.Series(r).groupby(df.day.to_numpy()).mean()
    mid = np.median(sorted(df.day.unique()))
    h1 = byd[byd.index <= mid].mean()
    h2 = byd[byd.index > mid].mean()
    drop = byd.drop(byd.idxmax()).mean() if len(byd) > 1 else float("nan")
    dt = (byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd)))
          if len(byd) > 1 and byd.std(ddof=1) else 0.0)
    bank, per = run_days(df)
    v = np.array(per)
    tot = (bank - 50.0)
    print(f"  {label:<22}{len(df):>6}{len(df)/days:>7.1f}{df.won.mean():>7.1%}"
          f"{df.px.mean():>7.3f}{r.mean():>9.4f}{dt:>7.2f}{tot:>9.2f}"
          f"{h1:>9.4f}{h2:>9.4f}{drop:>9.4f}{(v < 0).sum():>5}/{len(v)}")
    return {"n": len(df), "ev": r.mean(), "t": dt, "tot": tot, "day": byd,
            "h1": h1, "h2": h2, "drop": drop}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="/tmp/tc2.pkl")
    ap.add_argument("--pred", default="/tmp/pred.pkl",
                    help="corpus_models eval --out, for the model arm")
    args = ap.parse_args()

    rows = pickle.load(open(args.cache, "rb"))["rows"]
    B = pickle.load(open(args.pred, "rb"))
    usable = B["usable"]
    # position index of each row inside the model-arm arrays
    for pos, i in enumerate(usable):
        rows[i]["_pos"] = pos
    P = load_prices()
    # only windows we can price every arm on
    scored = [(i, r) for i, r in enumerate(rows)
              if r.get("_pos") is not None and r["strike"]
              and int(r["start_ms"] // 1000) in P]
    days = len({r["start_ms"] // 86400000 for _i, r in scored})
    print(f"priced windows: {len(scored):,} over {days} days | "
          f"band model [{S.min_quote},{S.max_quote}] "
          f"impulse [{S.impulse_min_price},{S.impulse_max_price}] "
          f"cross [{S.cross_min_price},{S.cross_max_price}]")
    print(f"tau: impulse/model [{S.min_tau_s:.0f},{S.max_tau_s:.0f}] "
          f"cross [{S.cross_min_tau_s:.0f},{S.cross_max_tau_s:.0f}] | "
          f"impulse {S.impulse_bps}bps/{S.impulse_window_s:.0f}s | "
          f"cross look {S.cross_look_s:.0f}s depth<={S.cross_max_depth_bps}bps\n")

    all_rows = []
    for i, r in scored:
        e = int(r["start_ms"] // 1000)
        day = int(r["start_ms"] // 86400000)
        for fn in (arm_impulse, arm_cross):
            for d in fn(r, P, e):
                d["epoch"], d["day"] = e, day
                all_rows.append(d)
        for d in arm_model(r, P, e, B["results"]["digital"]):
            d["epoch"], d["day"] = e, day
            all_rows.append(d)
    df = pd.DataFrame(all_rows)
    print("=== ARMS ALONE (one position per round for impulse/model; "
          "cross up to 3) ===")
    hdr = (f"  {'arm':<22}{'fills':>6}{'/day':>7}{'WR':>7}{'px':>7}{'EV/$':>9}"
           f"{'day-t':>7}{'tot$':>9}{'half1':>9}{'half2':>9}{'dropbest':>9}"
           f"{'neg':>7}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    solo = {}
    for arm in ("impulse", "cross", "model"):
        solo[arm] = stats(arm, apply_live_positions(df, (arm,)), days)

    print("\n=== EVERY COMBINATION (union under the live position rule) ===")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    combos = {}
    for k in (2, 3):
        for arms in itertools.combinations(("impulse", "cross", "model"), k):
            combos[arms] = stats("+".join(arms), apply_live_positions(df, arms),
                                 days)

    print("\n=== RANKED BY TOTAL $ OVER THE 8 DAYS ===")
    ranked = sorted(combos.items(), key=lambda kv: -kv[1].get("tot", -9e9))
    for arms, st in ranked:
        print(f"  {'+'.join(arms):<24} tot ${st['tot']:7.2f}   "
              f"EV/$ {st['ev']:+.4f}   day-t {st['t']:+.2f}   "
              f"halves {st['h1']:+.4f}/{st['h2']:+.4f}   "
              f"drop-best {st['drop']:+.4f}   fills {st['n']}")

    # split-half parameter stability: which combo wins in each half alone?
    print("\n=== DOES THE WINNER HOLD IN BOTH HALVES? ===")
    mid = np.median(sorted(df.day.unique()))
    for lab, sel in (("first half", lambda d: d.day <= mid),
                     ("second half", lambda d: d.day > mid)):
        best, bs = None, -9e9
        for arms in itertools.chain(((a,) for a in ("impulse", "cross", "model")),
                                    itertools.combinations(
                                        ("impulse", "cross", "model"), 2),
                                    [("impulse", "cross", "model")]):
            d = apply_live_positions(df, arms)
            d = d[sel(d)]
            if d.empty:
                continue
            r = (d.won.to_numpy() / d.px.to_numpy()) - (1 + d.fee.to_numpy())
            if r.mean() > bs:
                best, bs = arms, r.mean()
        print(f"  {lab:<12} best EV/$ {bs:+.4f}  ->  {'+'.join(best) if best else '-'}")


if __name__ == "__main__":
    main()
