"""What does the one real signal actually pay, after the real costs?

The forward-move test found exactly one predictable thing in the tape: 1-second
continuation momentum, at horizons <=15s, worth 0.17-0.33 bps of the underlying.
A GBM could not beat `sign(last 1s return)`, so that one-liner is the strategy.

This prices it honestly. Entry is the ASK -- the vwap of BUY prints on the token we
are buying, i.e. what other takers actually paid to cross -- not the mixed vwap, and
not the same second we took the snapshot. Then the taker fee from the metadata's
feeSchedule (rate 0.07, exponent 1, takerOnly) is applied as

    fee_per_share = 0.07 * min(p, 1-p)

and the observed fee is also reported straight from the data, so the formula is
checked rather than trusted.

Controls are included so a positive number can be read as signal and not as
"tokens happen to be cheap": the same arithmetic buying Up every time, and the
reversed signal.
"""
from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd

FEE_RATE = 0.07  # metadata feeSchedule.rate, exponent 1, takerOnly


def battery(v, days):
    v = np.asarray(v, float)
    if len(v) < 20:
        return "n<20"
    sd = v.std(ddof=1)
    t = v.mean() / (sd / math.sqrt(len(v))) if sd > 0 else float("nan")
    d = pd.DataFrame({"v": v, "day": np.asarray(days)})
    g = d.groupby("day")["v"].mean()
    td = (g.mean() / (g.std(ddof=1) / math.sqrt(len(g)))
          if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    h = len(v) // 2
    return (f"t {t:+5.2f} day-t {td:+5.2f} h1 {v[:h].mean():+.4f} "
            f"h2 {v[h:].mean():+.4f} drop1 {d[d['day'] != g.idxmax()]['v'].mean():+.4f}")


def ev_block(tag, entry, win, day, fee_rate, stake=1.0):
    """Per-share EV is the honest unit; per-$ staked is reported but is levered."""
    fee = fee_rate * np.minimum(entry, 1.0 - entry)
    cost = entry + fee
    ps = win.astype(float) - cost                 # profit per share bought
    pct = ps / cost                               # per $1 staked (leverage-prone)
    nd = len(set(np.asarray(day)))
    print(f"  {tag:22s} n={len(ps):6,d} ({len(ps) / max(nd, 1):6.1f}/day)  "
          f"win {win.mean():6.3f}  ask {entry.mean():6.3f}  fee {fee.mean():.4f}  "
          f"EV/share {ps.mean():+.4f}")
    print(f"        per-$ staked {pct.mean():+.4f} (median {np.median(pct):+.4f}, "
          f"levered by cheap tokens)  winrate edge {win.mean() - cost.mean():+.4f}")
    return ps


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/btc5m_ml.parquet")
    ap.add_argument("--prices", default="data/btc5m_prices.parquet")
    ap.add_argument("--tau-max", type=int, default=15)
    args = ap.parse_args()

    df = pd.read_parquet(args.data)
    px = pd.read_parquet(args.prices)

    notional = float((px["vol_up"].fillna(0) + px["vol_dn"].fillna(0)).sum())
    fees = float(px["fees"].fillna(0).sum())
    fee_obs = fees / notional if notional else 0.0
    print(f"=== fees actually charged, from the on-chain data ===")
    print(f"  ${fees:,.2f} of fee on ${notional:,.2f} of notional = {fee_obs:.6%} "
          f"effective -- the advertised schedule would be ~7%")

    bb = px[px["up_buy_px"].notna() & px["up_sell_px"].notna()]
    if len(bb) > 100:
        sp = (bb["up_buy_px"] - bb["up_sell_px"]).to_numpy()
        print(f"\n=== which print is the ask? ===")
        print(f"  buy-labelled vwap minus sell-labelled vwap: mean {sp.mean():+.4f} "
              f"median {np.median(sp):+.4f}")
        print(f"  negative => the side label is inverted (or maker-side); the ask "
              f"is therefore taken as the HIGHER of the two, which is "
              f"side-agnostic and cannot be wrong by construction.")
        print(f"  implied spread (high - low): median "
              f"{np.median(np.abs(sp)):.4f}")

    cols = ["window_id", "tau_s", "up_px", "dn_px", "up_buy_px", "dn_buy_px",
            "up_sell_px", "dn_sell_px"]
    m = df[df["tau_s"] <= args.tau_max].merge(px[cols], on=["window_id", "tau_s"],
                                              how="inner")
    m = m[m["last_ret_bps"].notna()].copy()
    for t in ("up", "dn"):
        hi = np.fmax(m[f"{t}_buy_px"].to_numpy(), m[f"{t}_sell_px"].to_numpy())
        m[f"ask_{t}"] = np.where(np.isfinite(hi), hi, m[f"{t}_px"].to_numpy())
    m = m[np.isfinite(m["ask_up"]) & np.isfinite(m["ask_dn"])]
    m = m[(m["ask_up"] > 0) & (m["ask_up"] < 1) & (m["ask_dn"] > 0) & (m["ask_dn"] < 1)]
    print(f"\n=== tradeable rows at tau<={args.tau_max}s: {len(m):,} over "
          f"{m['window_id'].nunique():,} windows / {m['day'].nunique()} days ===")
    if len(m) < 200:
        print("  too few rows"); return

    mom = np.sign(m["last_ret_bps"].to_numpy())
    up_win = (m["y_up"].to_numpy() == 1)
    entry = np.where(mom > 0, m["ask_up"].to_numpy(), m["ask_dn"].to_numpy())
    win = np.where(mom > 0, up_win, ~up_win)
    day = m["day"].to_numpy()
    keep = mom != 0

    for fr, label in ((fee_obs, "fees as OBSERVED"), (FEE_RATE, "fees as SCHEDULED")):
        print(f"\n=== the momentum signal, entry at the ask, {label} ===")
        ev_block("sign(last 1s) -> side", entry[keep], win[keep], day[keep], fr)
        rev_entry = np.where(mom > 0, m["ask_dn"].to_numpy(), m["ask_up"].to_numpy())
        ev_block("REVERSED (control)", rev_entry[keep], (~win)[keep], day[keep], fr)
        ev_block("always buy Up (control)", m["ask_up"].to_numpy(), up_win, day, fr)
        sub = keep & (entry >= 0.35) & (entry <= 0.65)
        if sub.sum() > 100:
            print(f"  -- no-leverage subset, ask in [0.35, 0.65] --")
            ev_block("  signal", entry[sub], win[sub], day[sub], fr)

    print(f"\n=== by strength of the signal (|1s move| in bps), observed fees ===")
    a = np.abs(m["last_ret_bps"].to_numpy())
    for lo, hi in ((0.0, 0.1), (0.1, 0.3), (0.3, 1.0), (1.0, 1e9)):
        k = keep & (a >= lo) & (a < hi)
        if k.sum() < 100:
            continue
        print(f"  |ret| {lo:.1f}-{hi:.1f} bps  ({k.mean():.1%} of rows)")
        ev_block("  signal", entry[k], win[k], day[k], fee_obs)

    print(f"\n=== by tau, observed fees ===")
    for lo, hi in ((0, 5), (5, 10), (10, 15)):
        k = keep & (m["tau_s"] > lo) & (m["tau_s"] <= hi)
        if k.sum() < 100:
            continue
        print(f"  tau {lo}-{hi}s")
        ev_block("  signal", entry[k], win[k], day[k], fee_obs)

    # ---- round level: what a live arm would actually do -------------------
    mm = m.reset_index(drop=True)
    pick = mm.groupby("window_id")["last_ret_bps"].apply(lambda s: s.abs().idxmax())
    r = mm.loc[pick.to_numpy()]
    nd = r["day"].nunique()
    print(f"\n=== ROUND LEVEL: one trade per window, taken at its strongest "
          f"tau<={args.tau_max}s signal ===")
    print(f"  {len(r):,} windows over {nd} days "
          f"({len(r) / nd:.0f} rounds/day available)")
    print(f"  {'thr':>5s} {'n':>5s} {'/day':>6s} {'win':>6s} {'ask':>6s} "
          f"{'EV/share':>9s} {'EV@sch':>8s}   checks")
    for thr in (0.0, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0):
        k = (r["last_ret_bps"].abs() >= thr).to_numpy()
        if k.sum() < 30:
            continue
        sg = np.sign(r["last_ret_bps"].to_numpy()[k])
        ent = np.where(sg > 0, r["ask_up"].to_numpy()[k], r["ask_dn"].to_numpy()[k])
        wn = np.where(sg > 0, (r["y_up"].to_numpy() == 1)[k],
                      (r["y_up"].to_numpy() == 0)[k])
        dy = r["day"].to_numpy()[k]
        e_obs = (wn.astype(float) - ent
                 - fee_obs * np.minimum(ent, 1 - ent))
        e_sch = (wn.astype(float) - ent
                 - FEE_RATE * np.minimum(ent, 1 - ent))
        d = pd.DataFrame({"v": e_obs, "day": dy})
        g = d.groupby("day")["v"].mean()
        td = (g.mean() / (g.std(ddof=1) / math.sqrt(len(g)))
              if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
        tt = (e_obs.mean() / (e_obs.std(ddof=1) / math.sqrt(len(e_obs)))
              if e_obs.std(ddof=1) > 0 else float("nan"))
        h = len(e_obs) // 2
        ok = (td > 2) and (e_obs[:h].mean() > 0) and (e_obs[h:].mean() > 0)
        print(f"  {thr:5.2f} {k.sum():5d} {k.sum() / nd:6.1f} {wn.mean():6.3f} "
              f"{ent.mean():6.3f} {e_obs.mean():+9.4f} {e_sch.mean():+8.4f}   "
              f"t {tt:+5.2f} day-t {td:+5.2f} h {e_obs[:h].mean():+.3f}/"
              f"{e_obs[h:].mean():+.3f} {'PASS' if ok else 'fail'}")

    print(f"\nNOTE: EV is per SHARE on a ~0.50 token, so x2 for % of stake. "
          f"'EV@sch' applies the advertised 7% taker fee instead of the observed "
          f"{fee_obs:.5%}.")

    # ---- the timing test: kill the same-second stale-price advantage --------
    lut = px[["window_id", "tau_s", "up_px", "dn_px", "up_buy_px", "dn_buy_px",
              "up_sell_px", "dn_sell_px"]].copy()
    for t in ("up", "dn"):
        hi = np.fmax(lut[f"{t}_buy_px"].to_numpy(), lut[f"{t}_sell_px"].to_numpy())
        lut[f"ask_{t}"] = np.where(np.isfinite(hi), hi, lut[f"{t}_px"].to_numpy())
    base = r[["window_id", "tau_s", "last_ret_bps", "y_up", "day"]].copy()
    print(f"\n=== TIMING TEST: entry priced k seconds AFTER the signal second ===")
    print(f"  last_ret_bps ends AT the decision second; the ask is that second's "
          f"prints. Selecting big moves therefore selects stale prices.")
    for k in (0, 1, 2, 3, 5):
        sh = lut.assign(tau_s=lut["tau_s"] + k)
        q = base.merge(sh[["window_id", "tau_s", "ask_up", "ask_dn"]],
                       on=["window_id", "tau_s"], how="inner")
        q = q[np.isfinite(q["ask_up"]) & np.isfinite(q["ask_dn"])]
        if len(q) < 50:
            print(f"  +{k}s: too few rows ({len(q)})")
            continue
        parts = []
        for thr in (0.1, 0.3, 0.5, 1.0):
            kk = (q["last_ret_bps"].abs() >= thr).to_numpy()
            if kk.sum() < 20:
                continue
            sg = np.sign(q["last_ret_bps"].to_numpy()[kk])
            ent = np.where(sg > 0, q["ask_up"].to_numpy()[kk],
                           q["ask_dn"].to_numpy()[kk])
            wn = np.where(sg > 0, (q["y_up"].to_numpy() == 1)[kk],
                          (q["y_up"].to_numpy() == 0)[kk])
            ev = wn.astype(float) - ent - fee_obs * np.minimum(ent, 1 - ent)
            parts.append(f"thr{thr:.1f} n{kk.sum():4d} EV{ev.mean():+.4f}")
        print(f"  +{k}s: " + "   ".join(parts))


if __name__ == "__main__":
    main()
