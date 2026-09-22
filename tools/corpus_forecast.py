"""7-day PnL forecast for the running config, built from the CORPUS ONLY.

Why not the live database
-------------------------
The live ledger is contaminated as a sample. It is one continuous path with the
config changing under it (the sigma bug, the venue switch, four different gate
settings), its `fair` values are the output of the very sigma that was broken,
and its entries are selected by the arm being scored. Any distribution taken
from it inherits all of that. The corpus is the only part of this repo where the
tape, the strike, the settlement and the quote are recorded INDEPENDENTLY of the
arm under test:

    data/btc5m/data/<day>/<epoch>/   ticks_dense.parquet (1s Binance tape)
                                     metadata.json     (Chainlink strike+final)
                                     trades_raw.json   (venue fills -> quotes)
    data/btc5m/corpus_cache.pkl      built by tools/corpus_models.py

What this tool does
-------------------
1. Rebuilds the live model's decision on every (corpus window, tau) exactly as
   live does: the `digital` model (Phi(ln(S/K)/(sigma*sqrt(tau/60)))) on the
   corpus's own sigma, gated by the repo's real `evaluate()` with the LIVE
   knobs -- edge_haircut, min_edge, safety margin, quote band, tau window.
2. Applies the two live filters that the money path applies and that a naive
   replay drops: the round-range gate (realised travel from the round's open, in
   bps of the strike) and the post-loss cooldown.
3. Prices the fill at the venue's real ASK, not at the mid and not at the
   model's own fair, and charges the repo's fee curve for the price paid.
4. Collapses to ONE position per round (the earliest decision, as live does),
   then reports the battery and bootstraps whole DAYS into a 7-day distribution.

The number this tool exists to produce is EV/$1 and the day rate; the 7-day
percentiles follow from those two and are only as good as the priced days.

Limits, stated once and not repeated
------------------------------------
* The quotes are Polymarket's on-chain prints. The arm now trades Binance's
  prediction venue. The gate is OUR gate and the fee is OUR fee, but the price
  level is a proxy. A venue whose book sits one cent richer turns +0.02/$1 into
  a loss.
* Only windows that carry on-chain fills have quotes, so the priced span is
  shorter than the 53-day tape. `--prices` decides which span this is; the tool
  prints the coverage it actually got.
* `signal_persistence` is approximated by requiring the same side to also act at
  the next-coarser tau in the grid. The grid is not a poll stream, so this is a
  proxy, and it is reported separately so its effect is visible.

Usage:
    python3 -m tools.corpus_forecast --pred /tmp/pred_digital.pkl
"""
from __future__ import annotations

import argparse
import math
import os
import pickle
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btcpredict.config import settings as S          # noqa: E402
from btcpredict.edge.signal import evaluate, fee_rate_for_price  # noqa: E402

CACHE = "data/btc5m/corpus_cache.pkl"
TAU_GRID = (60, 90, 120, 150, 180, 210, 240)
BACK = 30.0          # a quote older than this is not a quote


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------

def load_prices(path: str) -> dict:
    """epoch -> (tau[], mid_up[], mid_dn[], ask_up[], ask_dn[]), tau sorted."""
    p = pd.read_parquet(path, columns=["window_id", "tau_s", "up_px", "dn_px",
                                       "up_buy_px", "dn_buy_px"])
    p["epoch"] = p.window_id.str.split("/").str[1].astype(int)
    out = {}
    for e, g in p.groupby("epoch"):
        g = g.sort_values("tau_s")
        out[int(e)] = (g.tau_s.to_numpy(), g.up_px.to_numpy(), g.dn_px.to_numpy(),
                       g.up_buy_px.to_numpy(), g.dn_buy_px.to_numpy())
    return out


def quote_at(P: dict, e: int, tau: float) -> tuple | None:
    """The most recent quote AT OR BEFORE `tau`.

    Strictly causal. Nearest-print selection can pick a quote from up to 12s in
    the future and hand the decision information it did not have.
    """
    if e not in P:
        return None
    ts, up, dn, ua, da = P[e]
    k = int(np.searchsorted(ts, tau, side="right")) - 1
    if k < 0 or tau - ts[k] > BACK:
        return None
    if not (np.isfinite(up[k]) and np.isfinite(dn[k])):
        return None
    return (float(up[k]), float(dn[k]), float(ua[k]), float(da[k]))


def range_bps_of(r: dict, tau: float) -> float | None:
    """Realised travel from the round's open to now, in bps of the strike.

    This is `Main._round_range_bps` on the corpus: max-min of the 1s closes seen
    since the round opened, over the strike. Live samples it once per poll, so
    the 1s tape is the closest thing to the same quantity.
    """
    strike = r.get("strike")
    if not strike:
        return None
    close = r["close"]
    elapsed = max(1, int(300 - tau))
    seen = close[:min(elapsed, len(close))]
    seen = seen[np.isfinite(seen)]
    if len(seen) < 2:
        return None
    return 1e4 * (float(seen.max()) - float(seen.min())) / float(strike)


# ---------------------------------------------------------------------------
# the decision stream
# ---------------------------------------------------------------------------

def decisions(B: dict, rows: list, P: dict, args) -> pd.DataFrame:
    res = B["results"][args.model]
    usable = B["usable"]
    y = B["y"]
    p = np.array(res["p"]); e = np.array(res["e"])
    ix = np.array(res["i"]); t = np.array(res["t"])
    uy = y[usable]

    keep = e.astype(bool) if e.any() else np.ones(len(p), bool)
    out = []
    for j in range(len(p)):
        if not keep[j]:
            continue
        tau = float(t[j])
        if not (args.min_tau <= tau <= args.max_tau):
            continue
        i = int(ix[j])
        r = rows[usable[i]]
        epoch = int(r["start_ms"] // 1000)
        q = quote_at(P, epoch, tau)
        if q is None:
            continue
        up_mid, dn_mid, up_ask, dn_ask = q
        rng = range_bps_of(r, tau)
        if (args.max_range > 0 and rng is not None
                and not (args.min_range <= rng <= args.max_range)):
            continue
        fair_up = float(p[j])
        sig = evaluate(fair_up, up_mid, dn_mid,
                       half_spread=args.half_spread,
                       safety_margin=S.safety_margin,
                       min_edge=S.min_edge,
                       min_quote=args.min_quote, max_quote=args.max_quote,
                       edge_haircut=S.edge_haircut)
        if not sig.act or sig.side is None:
            continue
        ask = up_ask if sig.side == "Up" else dn_ask
        if not (np.isfinite(ask) and 0.0 < ask < 1.0):
            ask = min(0.999, (up_mid if sig.side == "Up" else dn_mid)
                      + args.half_spread)
        fee = fee_rate_for_price(ask)
        out.append({"day": int(r["start_ms"] // 86_400_000), "epoch": epoch,
                    "tau": tau, "side": sig.side, "mid": (up_mid if sig.side == "Up"
                                                          else dn_mid),
                    "ask": ask, "fee": fee, "rng": rng,
                    "won": int(uy[i] == (1 if sig.side == "Up" else 0)),
                    "fair": sig.fair, "edge": sig.edge,
                    "ev": sig.ev_per_dollar})
    return pd.DataFrame(out)


def persist_filter(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Approximate `signal_persistence=n` on a coarse tau grid.

    Live demands n consecutive agreeing polls before it acts. The corpus gives
    tau snapshots, not polls, so the closest available statement is "the same
    side also acted at the next-coarser tau". Reported as its own row because it
    is a proxy, not the live filter.
    """
    if n <= 1 or df.empty:
        return df
    keys = set(zip(df.epoch, df.tau, df.side))
    grid = sorted(df.tau.unique())
    ok = []
    for e, tau, side in zip(df.epoch, df.tau, df.side):
        need = [t for t in grid if t > tau][:n - 1]
        ok.append(all((e, t, side) in keys for t in need))
    return df[np.array(ok)]


def cooldown(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Drop the next n signals after a settled loss, in time order.

    Live counts the cooldown per ROUND and re-arms it from the ledger, so a
    restart cannot launder it. Same rule here, over the corpus's own order.
    """
    if n <= 0 or df.empty:
        return df
    df = df.sort_values(["epoch", "tau"], ascending=[True, False])
    skip = 0
    keep = []
    for _, row in df.iterrows():
        if skip > 0:
            skip -= 1
            continue
        keep.append(row)
        if not row["won"]:
            skip = n
    return pd.DataFrame(keep) if keep else df.iloc[0:0]


def one_per_round(df: pd.DataFrame) -> pd.DataFrame:
    """Live takes the EARLIEST decision in a round (max_open_rounds=1)."""
    if df.empty:
        return df
    return (df.sort_values(["epoch", "tau"], ascending=[True, False])
              .drop_duplicates("epoch", keep="first")
              .sort_values("epoch"))


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------

def ret_of(row: dict, stake: float) -> float:
    """Per-$1-staked return of one fill, fee included.

    shares = stake/ask; cost = ask*(1+fee)*shares; pnl = shares-cost on a win,
    -cost on a loss. So the break-even win rate IS the price paid, grossed up by
    the fee -- which is why `paid` is reported beside every EV.
    """
    sh = stake / row["ask"]
    cost = row["ask"] * (1.0 + row["fee"]) * sh
    pnl = (sh - cost) if row["won"] else -cost
    return pnl / cost


def battery(df: pd.DataFrame, stake: float) -> dict:
    if df.empty:
        return {}
    r = np.array([ret_of(x, stake) for _, x in df.iterrows()])
    byd = pd.Series(r).groupby(df.day.to_numpy()).mean()
    days = sorted(byd.index)
    half = np.median(days)
    h1 = float(byd[byd.index <= half].mean())
    h2 = float(byd[byd.index > half].mean())
    drop = float(byd.drop(byd.idxmax()).mean()) if len(byd) > 1 else float("nan")
    dt = (float(byd.mean() / (byd.std(ddof=1) / math.sqrt(len(byd))))
          if len(byd) > 1 and byd.std(ddof=1) else 0.0)
    pooled = float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 1 else 0.0
    return {"n": len(df), "wr": float(df.won.mean()), "paid": float(df.ask.mean()),
            "ev": float(r.mean()), "sd": float(r.std(ddof=1)) if len(r) > 1 else 0.0,
            "t": pooled, "day_t": dt, "h1": h1, "h2": h2, "drop": drop,
            "days": len(days), "byd": byd, "rets": r}


def show(label: str, df: pd.DataFrame, stake: float) -> dict:
    st = battery(df, stake)
    if not st:
        print(f"  {label:<26}  no fills")
        return {}
    print(f"  {label:<26} {st['n']:>5} {st['wr']:>7.1%} {st['paid']:>7.3f} "
          f"{st['ev']:>+9.4f} {st['t']:>+7.2f} {st['day_t']:>+8.2f} "
          f"{st['h1']:>+8.4f} {st['h2']:>+8.4f} {st['drop']:>+8.4f} "
          f"{st['n'] / max(st['days'], 1):>7.1f}")
    return st


def bootstrap_7d(df: pd.DataFrame, stake: float, start: float, reps: int,
                 rng, horizon: int = 7) -> dict:
    """Resample whole DAYS from the priced span, compounding the balance.

    Days, not trades. The trades inside a day share a regime, a set of strikes
    and one book, so trade-level resampling reports an interval far narrower
    than the truth. The day is the unit that can be treated as exchangeable.
    """
    pnl_by_day = df.groupby("day").apply(
        lambda g: float(sum(ret_of(x, stake) for _, x in g.iterrows()) * stake),
        include_groups=False)
    cnt = df.groupby("day").size()
    vals = pnl_by_day.to_numpy()
    counts = cnt.to_numpy()
    k = len(vals)
    pick = rng.integers(0, k, size=(reps, horizon))
    out_bank = start + vals[pick].sum(axis=1)
    out_tr = counts[pick].sum(axis=1)
    return {"bank": out_bank, "trades": out_tr, "days": k}


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", default="/tmp/pred_digital.pkl")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--model", default="digital")
    ap.add_argument("--prices", default="data/btc5m_prices_full.parquet")
    ap.add_argument("--min-tau", type=float, default=S.min_tau_s)
    ap.add_argument("--max-tau", type=float, default=S.max_tau_s)
    ap.add_argument("--min-quote", type=float, default=S.min_quote)
    ap.add_argument("--max-quote", type=float, default=S.max_quote)
    ap.add_argument("--min-range", type=float, default=5.0)
    ap.add_argument("--max-range", type=float, default=16.0)
    ap.add_argument("--half-spread", type=float, default=S.assumed_half_spread)
    ap.add_argument("--cooldown", type=int, default=S.model_cooldown_after_loss)
    ap.add_argument("--persistence", type=int, default=1)
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--start", type=float, default=7.35)
    ap.add_argument("--reps", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    B = pickle.load(open(a.pred, "rb"))
    rows = pickle.load(open(a.cache, "rb"))["rows"]
    P = load_prices(a.prices)
    pdays = len({int(e) // 86400 for e in P})
    print(f"corpus: {len(rows):,} windows | quotes: {len(P):,} windows over "
          f"{pdays} days")
    print(f"LIVE CONFIG  model={a.model} arm=model only | tau "
          f"[{a.min_tau:.0f},{a.max_tau:.0f}] | quote [{a.min_quote:.2f},"
          f"{a.max_quote:.2f}] | range [{a.min_range:.0f},{a.max_range:.0f}]bps | "
          f"min_edge {S.min_edge:.2f} | haircut {S.edge_haircut:.2f} | "
          f"safety {S.safety_margin:.3f} | half_spread {a.half_spread:.4f} | "
          f"cooldown {a.cooldown} | flat ${a.stake:.2f}")

    raw = decisions(B, rows, P, a)
    if raw.empty:
        print("no gated decisions on the corpus -- nothing to forecast")
        return 1
    print(f"\nraw gated decisions: {len(raw):,} over {raw.epoch.nunique():,} "
          f"rounds / {raw.day.nunique()} days   ask paid {raw.ask.mean():.3f} "
          f"(mid {raw.mid.mean():.3f}, slippage "
          f"{(raw.ask - raw.mid).mean():+.4f})")

    # A nested ladder, so each filter's contribution is visible and no two rows
    # are the same run printed twice.
    no_rng = decisions(B, rows, P, argparse.Namespace(
        **{**vars(a), "min_range": 0.0, "max_range": 0.0}))
    ladder = (("1 no range gate", no_rng),
              ("2 + range gate", raw),
              ("3 + range gate + cooldown", cooldown(raw, a.cooldown)),
              ("4 + persistence(proxy)",
               cooldown(persist_filter(raw, a.persistence), a.cooldown)))

    print(f"\n  {'variant':<28}{'fills':>6}{'WR':>8}{'paid':>8}{'EV/$1':>10}"
          f"{'t':>7}{'day-t':>8}{'half1':>9}{'half2':>9}{'dropbest':>9}"
          f"{'tr/day':>8}")
    print("  " + "-" * 110)
    variants = {}
    for label, f in ladder:
        rounds = one_per_round(f)
        st = show(label, rounds, a.stake)
        if st:
            st["df"] = rounds
            variants[label] = st

    if not variants:
        print("\nno variant produced a fill")
        return 1
    live_label = ("4 + persistence(proxy)" if a.persistence > 1
                  else "3 + range gate + cooldown")
    st = variants.get(live_label) or max(variants.values(), key=lambda s: s["n"])

    print(f"\n=== PER-DAY PnL at ${a.stake:.2f}/trade -- variant: {live_label} "
          f"===")
    df = st["df"]
    cum = 0.0
    for d, g in df.groupby("day"):
        pnl = sum(ret_of(x, a.stake) * a.stake for _, x in g.iterrows())
        cum += pnl
        print(f"  day {d}  n={len(g):>3}  WR {g.won.mean():>5.1%}  "
              f"paid {g.ask.mean():.3f}  {pnl:+7.2f}   (cum {cum:+7.2f})")

    rng = np.random.default_rng(a.seed)
    print(f"\n=== 7-DAY FORECAST (day-block bootstrap over the {st['days']} "
          f"priced days, {a.reps:,} paths, start ${a.start:.2f}) ===")
    b = bootstrap_7d(df, a.stake, a.start, a.reps, rng)
    bank, tr = b["bank"], b["trades"]
    q = np.percentile(bank, [5, 10, 25, 50, 75, 90, 95])
    print(f"  trades/day      {df.groupby('day').size().mean():.1f}")
    print(f"  median 7d pnl   ${np.median(bank) - a.start:+.2f}")
    print(f"  p5/p10/p25      ${q[0]:.2f} ${q[1]:.2f} ${q[2]:.2f}")
    print(f"  median/p75/p90  ${q[3]:.2f} ${q[4]:.2f} ${q[5]:.2f}  p95 ${q[6]:.2f}")
    print(f"  P(7d loss)      {100 * (bank < a.start).mean():.1f}%")
    print(f"  P(below $1)     {100 * (bank < 1.0).mean():.2f}%")
    print(f"  expected trades {tr.mean():.0f} over 7 days")

    print("\n  same horizon, TRADE bootstrap (the wrong resampling unit):")
    n_trades = int(tr.mean())
    r = rng.choice(st["rets"], size=(a.reps, n_trades), replace=True)
    tb = a.start + (r * a.stake).sum(axis=1)
    w_tr = np.percentile(tb, 90) - np.percentile(tb, 10)
    w_day = q[5] - q[1]
    print(f"    trade   p10 ${np.percentile(tb, 10):7.2f}  median "
          f"${np.median(tb):7.2f}  p90 ${np.percentile(tb, 90):7.2f}")
    print(f"    day     p10 ${q[1]:7.2f}  median ${np.median(bank):7.2f}  "
          f"p90 ${q[5]:7.2f}")
    # Which one is wider is a property of THIS span, not a law. With few trades
    # a day the day-block can come out NARROWER, because it cannot express a day
    # that trades more or less often than the corpus's own days did. Reported
    # rather than asserted.
    print(f"    width ratio day/trade {w_day / max(w_tr, 1e-9):.2f}x "
          f"({'day wider' if w_day > w_tr else 'trade wider -- the day-block '
             'cannot vary the trade count'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
