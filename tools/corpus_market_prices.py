"""Market prices from Polymarket's own on-chain fills, one row per (window, second).

trades_raw.json is a list of CTF-exchange OrderFilled events:
  block_timestamp, token_id, side, maker_amount, taker_amount, fee
The two tokens are the Up and Down shares (metadata.clobTokenIds[0] is Up, matching
outcomes ["Up","Down"]). A trade of the Down token at p implies the Up price ~1-p.

Price recovery: one of maker/taker or taker/maker is the USDC-per-share price and
the other is its reciprocal (e.g. 2.0 / 4.0 -> 0.50 for a 4-share fill costing $2).
We take whichever lies strictly inside (0,1) and record the rejection rate, then
verify the series against settlement: at the end of a window the winning token must
be priced near 1. That check is printed so the mapping is evidenced, not assumed.

Run with --diag N first to print that evidence, then without it to write parquet.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd


def _meta(d: str) -> tuple[str | None, int | None, int | None]:
    """-> (up_token_id, won_up, window_start_epoch)."""
    mp = os.path.join(d, "metadata.json")
    if not os.path.exists(mp):
        return None, None, None
    try:
        m = json.load(open(mp))
        ids = m.get("clobTokenIds")
        ids = json.loads(ids) if isinstance(ids, str) else ids
        ev = m["events"][0]["eventMetadata"]
        beat, final = float(ev["priceToBeat"]), float(ev["finalPrice"])
        return str(ids[0]), (1 if final >= beat else 0), int(os.path.basename(d))
    except Exception:
        return None, None, None


def one_window(f: str) -> tuple[list[dict], dict | None]:
    d = os.path.dirname(f)
    up_id, won_up, start = _meta(d)
    if up_id is None or not os.path.exists(f):
        return [], None
    try:
        ev = json.load(open(f))
    except Exception:
        return [], None
    if not isinstance(ev, list) or not ev:
        return [], None
    rec: dict[int, dict] = {}
    bad = 0
    for e in ev:
        try:
            ma, ta = float(e["maker_amount"]), float(e["taker_amount"])
        except Exception:
            bad += 1
            continue
        if ma <= 0 or ta <= 0:
            bad += 1
            continue
        p = ma / ta
        if not (0.0 < p < 1.0):
            p = ta / ma
            if not (0.0 < p < 1.0):
                bad += 1
                continue
        is_up = (str(e.get("token_id")) == str(up_id))
        buy = str(e.get("side", "")).upper() == "BUY"
        ts = int(e.get("block_timestamp") or 0)
        if ts <= 0:
            bad += 1
            continue
        sec = ts - start
        r = rec.get(sec)
        notional = min(ma, ta)  # USDC leg
        if r is None:
            r = rec[sec] = {"n_up": 0, "s_up": 0.0, "v_up": 0.0,
                            "n_dn": 0, "s_dn": 0.0, "v_dn": 0.0, "fees": 0.0,
                            "nb_up": 0, "sb_up": 0.0, "ns_up": 0, "ss_up": 0.0,
                            "nb_dn": 0, "sb_dn": 0.0, "ns_dn": 0, "ss_dn": 0.0}
        r["fees"] += float(e.get("fee") or 0.0)
        if is_up:
            r["n_up"] += 1
            r["s_up"] += p
            r["v_up"] += notional
            if buy:
                r["nb_up"] += 1
                r["sb_up"] += p
            else:
                r["ns_up"] += 1
                r["ss_up"] += p
        else:
            r["n_dn"] += 1
            r["s_dn"] += p
            r["v_dn"] += notional
            if buy:
                r["nb_dn"] += 1
                r["sb_dn"] += p
            else:
                r["ns_dn"] += 1
                r["ss_dn"] += p
    wid = f"{os.path.basename(os.path.dirname(d))}/{os.path.basename(d)}"
    rows = []
    for sec, r in rec.items():
        rows.append({
            "window_id": wid, "start_ms": start, "sec": sec,
            "tau_s": 300 - sec,
            "n_up": r["n_up"], "n_dn": r["n_dn"],
            "up_px": (r["s_up"] / r["n_up"]) if r["n_up"] else np.nan,
            "dn_px": (r["s_dn"] / r["n_dn"]) if r["n_dn"] else np.nan,
            "vol_up": r["v_up"], "vol_dn": r["v_dn"],
            "fees": r["fees"],
            "up_buy_px": (r["sb_up"] / r["nb_up"]) if r["nb_up"] else np.nan,
            "up_sell_px": (r["ss_up"] / r["ns_up"]) if r["ns_up"] else np.nan,
            "dn_buy_px": (r["sb_dn"] / r["nb_dn"]) if r["nb_dn"] else np.nan,
            "dn_sell_px": (r["ss_dn"] / r["ns_dn"]) if r["ns_dn"] else np.nan,
        })
    rows.sort(key=lambda x: x["sec"])
    return rows, {"window_id": wid, "won_up": won_up, "n_events": len(ev), "bad": bad}


def _w(f):
    return one_window(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/btc5m_prices.parquet")
    ap.add_argument("--diag", type=int, default=0,
                    help="sample N windows and prove the price mapping against settlement")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(glob.glob("data/btc5m/data/*/*/trades_raw.json"))
    if args.limit:
        files = files[:args.limit]
    print(f"trades_raw files: {len(files):,}")

    if args.diag:
        sub = files[::max(1, len(files) // args.diag)][:args.diag]
        print(f"{'window':26s} {'wonup':>5s} {'evs':>6s} {'secmin':>7s} {'secmax':>7s} "
              f"{'nsec':>5s} {'lastpx':>7s} {'late_vw':>8s} {'ok':>3s}")
        ok = tot = 0
        for f in sub:
            rows, info = one_window(f)
            if not info or not rows:
                continue
            secs = [r["sec"] for r in rows]

            def implied(r):
                if np.isfinite(r["up_px"]):
                    return r["up_px"]
                if np.isfinite(r["dn_px"]):
                    return 1.0 - r["dn_px"]
                return np.nan

            tail = [(implied(r), r["vol_up"] + r["vol_dn"]) for r in rows
                    if 270 <= r["sec"] <= 300 and np.isfinite(implied(r))]
            vw = (sum(p * w for p, w in tail) / sum(w for _, w in tail)
                  if tail and sum(w for _, w in tail) > 0 else np.nan)
            last = next((implied(r) for r in reversed(rows)
                         if np.isfinite(implied(r))), np.nan)
            good = bool(np.isfinite(vw) and ((vw > 0.5) == (info["won_up"] == 1)))
            ok += int(good)
            tot += 1
            print(f"{info['window_id']:26s} {info['won_up']:5d} {info['n_events']:6d} "
                  f"{min(secs):7d} {max(secs):7d} {len(secs):5d} {last:7.3f} "
                  f"{vw:8.3f} {'Y' if good else 'n':>3s}")
        print(f"\nsettlement agrees with the late-window price: {ok}/{tot} = "
              f"{ok / max(tot, 1):.1%}")
        return

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        res = list(ex.map(_w, files, chunksize=8))
    rows = [r for rr, _ in res for r in rr]
    infos = [i for _, i in res if i]
    if not rows:
        print("no price rows"); return
    df = pd.DataFrame(rows).sort_values(["start_ms", "sec"]).reset_index(drop=True)
    # implied up price from the down token
    df["up_from_dn"] = 1.0 - df["dn_px"]
    df["mid"] = df[["up_px", "up_from_dn"]].mean(axis=1, skipna=True)
    df.to_parquet(args.out, index=False)
    nf = len(df)
    print(f"windows: {df['window_id'].nunique():,}  rows: {nf:,}  "
          f"-> {args.out} ({os.path.getsize(args.out) / 1e6:.1f} MB)")
    print(f"median sec span/window: {df.groupby('window_id')['sec'].max().median():.0f}")
    print(f"rows with a usable Up print: {df['up_px'].notna().mean():.1%}   "
          f"from Down only: {(df['up_px'].isna() & df['up_from_dn'].notna()).mean():.1%}")
    if infos:
        bad = sum(i["bad"] for i in infos)
        evs = sum(i["n_events"] for i in infos)
        print(f"events parsed: {evs:,}   rejected: {bad:,} ({bad / max(evs, 1):.3%})")


if __name__ == "__main__":
    main()
