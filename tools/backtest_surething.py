"""Experiment: a third arm that buys SURE trades -- high price, high win rate.

The idea: the longshot ticket wins rarely and pays a lot; an arm that buys the
EXPENSIVE side would win often and pay a little, and a high win rate feels like
the safer way to compound. This measures it on the same rounds the ticket uses,
at the same prices (the executable ask), with the same fee, and runs the same
battery that has killed every claim that did not hold.

Three things are worth knowing before reading the output:

  * The favourite IS the mirror of the ticket. The venue's asks sum to ~1.00, so
    buying the expensive side in a round where the cheap side is mispriced is
    buying the other half of the same mistake.
  * The venue is CALIBRATED on favourites: it says 0.890 and 0.885 happens. So at
    an ask of 0.89 the break-even win rate is 91% and the true probability is
    ~89% -- the trade is short by construction, before any signal.
  * That is not theory, it is our own live record: the retired model arm bought
    the 0.80-1.01 band 74 times, WON 85.1% of them, and returned -0.4% of stake.
    A win rate says nothing without the price it was paid at.

Usage:  python -m tools.backtest_surething [--db data/btcpredict.db]
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

from btcpredict.config import settings
from tools.backtest_impulse_orders import sigma_1m_by_round
from tools.longshot_bands import report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/btcpredict.db")
    args = ap.parse_args()
    con = sqlite3.connect(Path(args.db))
    rounds = con.execute(
        "SELECT slug, start_ms, winner FROM rounds "
        "WHERE symbol='BTCUSDT' AND winner IN ('Up','Down')").fetchall()
    sig = sigma_1m_by_round(con, rounds)
    win = (settings.ticket_min_tau_s, settings.ticket_max_tau_s)
    print(f"rounds {len(rounds):,}; the SAME cell as the ticket "
          f"(tau {win[0]:.0f}-{win[1]:.0f}s, first quote), but buying the "
          f"EXPENSIVE side\n")

    bands: dict[str, list] = {k: [] for k in
                              ("all", ">=0.75", ">=0.80", ">=0.85", ">=0.90",
                               "z<0.5", "z>=0.5", "z>=1.0", "z>=2.0",
                               "stale>=0.05", "stale<0.05", "cheap side")}
    for slug, start_ms, winner in rounds:
        rows = con.execute(
            "SELECT ts_ms, tau_s, up_ask, down_ask, up_price, down_price, "
            "spot, strike FROM quotes WHERE slug=? AND up_ask IS NOT NULL "
            "AND down_ask IS NOT NULL AND tau_s BETWEEN ? AND ? "
            "ORDER BY ts_ms LIMIT 1", (slug, win[0], win[1])).fetchall()
        if not rows:
            continue
        _ts, tau, ua, da, up_px, dn_px, spot, strike = rows[0]
        if not (0 < ua < 1 and 0 < da < 1 and spot and strike):
            continue
        day = start_ms // 86400000
        fav_up = ua > da
        fav, cheap = (ua, da) if fav_up else (da, ua)
        fav_disp = up_px if fav_up else dn_px
        fav_side = "Up" if fav_up else "Down"
        cheap_side = "Down" if fav_up else "Up"
        sig_r = sig.get(slug)
        z = (abs(math.log(spot / strike)) / (sig_r * math.sqrt(tau / 60.0))
             if sig_r and sig_r > 0 and tau > 0 else None)
        stale = fav - fav_disp if fav_disp else None

        bands["all"].append((day, fav, winner == fav_side))
        bands["cheap side"].append((day, cheap, winner == cheap_side))
        for lo in (0.75, 0.80, 0.85, 0.90):
            if fav >= lo:
                bands[f">={lo:.2f}"].append((day, fav, winner == fav_side))
        if z is not None:
            for lo in (0.5, 1.0, 2.0):
                if z >= lo:
                    bands[f"z>={lo:.1f}"].append((day, fav, winner == fav_side))
            if z < 0.5:
                bands["z<0.5"].append((day, fav, winner == fav_side))
        if stale is not None:
            key = "stale>=0.05" if stale >= 0.05 else "stale<0.05"
            bands[key].append((day, fav, winner == fav_side))

    print("=== buying the EXPENSIVE side (the 'sure' trade) ===")
    print("  a row is only real if the ratio beats 1.00x AND the battery passes\n")
    for k in ("all", ">=0.75", ">=0.80", ">=0.85", ">=0.90"):
        report(bands[k], f"fav {k}")
    print("\n=== 'very sure': the spot this many sigma past the strike ===")
    for k in ("z<0.5", "z>=0.5", "z>=1.0", "z>=2.0"):
        report(bands[k], f"fav {k}")
    print("\n=== and only when the venue's display is stale (the mechanism "
          "that works elsewhere) ===")
    for k in ("stale>=0.05", "stale<0.05"):
        report(bands[k], f"fav {k}")
    print("\n=== control: the CHEAP side of the same rounds, same prices ===")
    report(bands["cheap side"], "cheap side")
    print("\n  Read the ratio column: for a favourite the break-even win rate is")
    print("  px/0.98, and the venue's price already IS the probability, so any")
    print("  ratio near 1.00x is the venue being right and us paying the spread.")


if __name__ == "__main__":
    main()
