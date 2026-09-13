"""Does each pricing model's edge survive the venue's fee and the live gate?

Stage 2 of the corpus study. ``tools/corpus_models.py`` scores CALIBRATION on
all 15,226 windows; this scores MONEY on the subset that carries a market price
(2,240 windows / 8 days, Polymarket's own on-chain prints).

Method
------
For every (window, tau) decision the model's p_up is pushed through the repo's
real ``btcpredict.edge.signal.evaluate`` gate -- same half_spread, same
safety_margin, same fee curve as live -- and the fill is modelled at the gate's
``q_eff``. Live's bookkeeping is then reproduced: ONE position per round (the
earliest decision, matching ``max_open_rounds=1``), the day-scoped loss cap, and
the same P&L arithmetic as the reference run:

    shares = stake / q_eff
    cost   = q_eff * (1 + fee) * shares
    pnl    = shares - cost  if the round settles our side, else -cost

Reported with the repo's required battery: day-clustered t, both halves,
drop-best-day, and a day bootstrap.

The market is also run as if it were a model (p_up = mid), which prices the
house edge: a strategy that cannot beat that line is not adding information.

CAVEAT that governs every number here
-------------------------------------
The prices are Polymarket's, not predict.fun's. Only the MID is taken from
Polymarket (as the market's estimate of P(Up)); execution is modelled with OUR
venue's cost (half_spread + the 200bps-capped fee curve). So this measures
"does the model beat the market's opinion, after our costs", not "what would
our venue's book have quoted". 8 days is also short: the bootstrap is the only
statement about significance worth reading.

Usage:
    python -m tools.corpus_trade --pred /tmp/pred.pkl --cache /tmp/tc2.pkl
"""
from __future__ import annotations

import argparse
import math
import pickle

import numpy as np
import pandas as pd

from btcpredict.config import settings as S
from btcpredict.edge.signal import evaluate, fee_rate_for_price, stake_for_fraction

PRICES = "data/btc5m_prices.parquet"


def load_prices() -> dict:
    p = pd.read_parquet(PRICES, columns=["window_id", "tau_s", "up_px", "dn_px",
                                         "up_buy_px", "dn_buy_px"])
    p["epoch"] = p.window_id.str.split("/").str[1].astype(int)
    out = {}
    for e, g in p.groupby("epoch"):
        g = g.sort_values("tau_s")
        out[int(e)] = (g.tau_s.to_numpy(), g.up_px.to_numpy(), g.dn_px.to_numpy(),
                       g.up_buy_px.to_numpy(), g.dn_buy_px.to_numpy())
    return out


def quote_at(P: dict, e: int, tau: float) -> tuple | None:
    """The most recent quote AT OR BEFORE `tau` -> (up_px, dn_px, up_ask, dn_ask).

    Strictly causal: picking the nearest print by absolute distance could select
    a price from up to 12 seconds AFTER the decision and hand the model
    information it did not have.
    """
    if e not in P:
        return None
    ts, up, dn, ua, da = P[e]
    k = int(np.searchsorted(ts, tau, side="right")) - 1
    if k < 0 or tau - ts[k] > 30.0:      # no print in the last 30s
        return None
    if not (np.isfinite(up[k]) and np.isfinite(dn[k])):
        return None
    return float(up[k]), float(dn[k]), float(ua[k]), float(da[k]), float(ts[k])


def decisions(name: str, a: dict, uy: np.ndarray, uday: np.ndarray,
              epos: list[int], P: dict, args) -> pd.DataFrame:
    """Every gated decision this model would have taken, one row per fill."""
    p = np.array(a["p"]); e = np.array(a["e"]); ix = np.array(a["i"])
    t = np.array(a["t"])
    rows = []
    for j in range(len(p)):
        if not e[j]:
            continue
        tau = t[j]
        if not (args.min_tau <= tau <= args.max_tau):
            continue
        epoch = epos[ix[j]]
        q = quote_at(P, epoch, tau)
        if q is None:
            continue
        up_px, dn_px, up_ask, dn_ask, _ = q
        fair = float(p[j]) if name != "__market__" else up_px
        sig = evaluate(fair, up_px, dn_px,
                       half_spread=S.assumed_half_spread,
                       safety_margin=S.safety_margin,
                       min_edge=S.min_edge,
                       min_quote=args.min_quote,
                       max_quote=args.max_quote,
                       edge_haircut=S.edge_haircut)
        if not sig.act or sig.side is None:
            continue
        qeff = min(0.999, (up_px if sig.side == "Up" else dn_px)
                   + S.assumed_half_spread)
        fee = fee_rate_for_price(qeff)
        rows.append({"day": int(uday[ix[j]]), "epoch": int(epoch), "tau": tau,
                     "side": sig.side, "px": qeff, "fee": fee,
                     "won": int(uy[ix[j]] == (1 if sig.side == "Up" else 0)),
                     "edge": sig.edge})
    return pd.DataFrame(rows)


def run(df: pd.DataFrame, bank: float, flat: float | None, args) -> tuple:
    """One position per round, day-scoped loss cap. -> (bank, per_day list)."""
    if df.empty:
        return bank, []
    # earliest decision in the round wins (live scans tau downward)
    df = (df.sort_values(["day", "epoch", "tau"], ascending=[True, True, False])
            .drop_duplicates(["day", "epoch"], keep="first")
            .sort_values(["day", "epoch"]))
    per = []
    for day, g in df.groupby("day", sort=True):
        net = 0.0
        cap = S.daily_loss_cap_pct * bank
        for px, fee, won in zip(g.px.to_numpy(), g.fee.to_numpy(),
                                g.won.to_numpy()):
            if net <= -cap:
                break
            if flat is not None:
                st = flat
            else:
                base = stake_for_fraction(bank, fraction=S.stake_fraction,
                                          min_stake=S.min_order_usdt,
                                          cap=S.stake_cap_usdt,
                                          floor_to_minimum=S.stake_floor_to_minimum)
                st = base if base > 0 else 0.0
            if st < S.min_order_usdt or st > bank:
                continue
            sh = st / px
            cost = px * (1 + fee) * sh
            p = (sh - cost) if won else -cost
            bank += p
            net += p
        per.append(net)
    return bank, per


def report(label: str, df: pd.DataFrame, args) -> dict:
    if df.empty:
        print(f"  {label:<14}     no filled decisions")
        return {}
    df = (df.sort_values(["day", "epoch", "tau"], ascending=[True, True, False])
            .drop_duplicates(["day", "epoch"], keep="first"))
    r = (df.won.to_numpy() / df.px.to_numpy()) - (1 + df.fee.to_numpy())
    byd = pd.Series(r).groupby(df.day.to_numpy()).mean()
    days = sorted(df.day.unique())
    half = np.median(days)
    h1 = byd[byd.index <= half].mean() if (byd.index <= half).any() else float("nan")
    h2 = byd[byd.index > half].mean() if (byd.index > half).any() else float("nan")
    drop = byd.drop(byd.idxmax()).mean() if len(byd) > 1 else float("nan")
    dt = (byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd)))
          if len(byd) > 1 and byd.std(ddof=1) else 0.0)
    bank, per = run(df, 50.0, args.flat, args)
    v = np.array(per)
    g_ = (bank / 50.0) ** (1.0 / max(1, len(v))) - 1
    print(f"  {label:<14}{len(df):>6}{df.won.mean():>7.1%}{df.px.mean():>7.3f}"
          f"{r.mean():>9.4f}{dt:>8.2f}{h1:>9.4f}{h2:>9.4f}{drop:>9.4f}"
          f"{100*g_:>8.2f}%{(v < 0).sum():>5}/{len(v)}")
    return {"n": len(df), "ev": r.mean(), "t": dt, "days": byd, "bank": bank,
            "h1": h1, "h2": h2, "drop": drop}


def bootstrap(byd: pd.Series, n: int, reps: int, rng) -> np.ndarray:
    """Resample days with replacement, compounding the daily mean EV."""
    if len(byd) < 2:
        return np.array([50.0])
    vals = byd.to_numpy()
    out = np.empty(reps)
    for i in range(reps):
        b = 50.0
        for d in rng.choice(vals, n, replace=True):
            b *= (1.0 + d * S.stake_fraction * 10.0)   # ~$1 stake on ~$45
        out[i] = b
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", default="/tmp/pred.pkl")
    ap.add_argument("--cache", default="/tmp/tc2.pkl")
    ap.add_argument("--min-tau", type=float, default=S.min_tau_s)
    ap.add_argument("--max-tau", type=float, default=S.max_tau_s)
    ap.add_argument("--min-quote", type=float, default=S.min_quote)
    ap.add_argument("--max-quote", type=float, default=S.max_quote)
    ap.add_argument("--flat", type=float, default=1.0)
    args = ap.parse_args()

    B = pickle.load(open(args.pred, "rb"))
    rows = pickle.load(open(args.cache, "rb"))["rows"]
    usable, R = B["usable"], B["results"]
    uy = B["y"][usable]
    uday = B["day"][usable]
    epos = [int(rows[i]["start_ms"] // 1000) for i in usable]
    P = load_prices()
    print(f"priced windows available: {len(P):,} | "
          f"decisions cover {len(usable):,} windows | "
          f"tau window [{args.min_tau:.0f},{args.max_tau:.0f}] | "
          f"band [{args.min_quote},{args.max_quote}] | flat ${args.flat:.2f}\n")
    print(f"  {'model':<14}{'fills':>6}{'WR':>7}{'px':>7}{'EV/$':>9}{'day-t':>8}"
          f"{'half1':>9}{'half2':>9}{'dropbest':>9}{'%/day':>9}{'neg':>8}")
    print("  " + "-" * 90)
    stats = {}
    stats["__market__"] = report("__market__", decisions("__market__", R[
        list(R)[0]], uy, uday, epos, P, args), args)
    for name in R:
        stats[name] = report(name, decisions(name, R[name], uy, uday, epos, P,
                                             args), args)
    rng = np.random.default_rng(11)
    print(f"\n  day bootstrap (compounded, {len(P)} windows ~ 8 days):"
          f"  median [p10 - p90]")
    for name, st in stats.items():
        if not st or "days" not in st:
            continue
        for nd in (8, 30):
            b = bootstrap(st["days"], nd, 2000, rng)
            print(f"    {name:<14} {nd:>2}d  $50 -> ${np.median(b):7.2f}"
                  f"  [{np.percentile(b, 10):6.2f} - {np.percentile(b, 90):7.2f}]"
                  f"   P(<$50) {100*(b < 50).mean():4.0f}%")
        print()


if __name__ == "__main__":
    main()
