"""SEVEN exit rules, swept against each other and against holding to the bell.

The shipped exit is ONE hypothesis -- "leave when the entry trigger reads the
other side" -- and it has never been compared against any other rule. It may be
losing because exiting is wrong, or because THAT rule is wrong. Those are
different findings and only a field of candidates separates them.

THE RULES (each sees the same entries, the same slots, the same sale mechanics):

  off          hold to settlement.
  flip         main.py's: arm on the crossing EVENT, confirm on the STATE,
               require the flip to persist `confirm_s`.
  stop         a price stop: leave when the bid falls `x` under what we paid.
  take         a profit target: leave when the bid rises `x` over what we paid.
  bracket      both at once, whichever prints first.
  trail        a trailing stop: track the best bid since entry, leave when the
               bid gives back `x` from it.
  margin       leave when the SIGNAL turns `b` bps against the held side --
               the flip rule without the crossing-event requirement, which is
               the single assumption the shipped rule adds.
  late         leave at tau <= T only if the bid is under what we paid: cut
               losers before the bell, ride winners into it.

THE SALE IS THE BROKER'S, for every rule. `_sell_fak_body` derives a floor of
round_up(bid - slip) and sends a FAK at it; the fill comes back at the venue's
price, and all three real live sales landed AT the bid (floor 0.33/bid 0.36 ->
0.36; 0.31/0.34 -> 0.34; 0.18/0.21 -> 0.33). Both broker refusals are modelled,
because each leaves the position riding to settlement rather than exiting: no
fresh bid, and a floor not strictly inside (0, bid).

    python3 tools/pm_exit_models.py --jobs 10
"""
from __future__ import annotations

import argparse, datetime as dt, hashlib, math, os, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
from pm_full_chain import LIVE  # noqa
from pm_exit_chain import build, claim  # noqa

FEE = lambda p: 0.07 * p * (1 - p)
EXEC = 0.0038
TICK = 0.01


def _sellable(bid, slip):
    """The broker's two refusals, in order. Returns the fill price or None."""
    if bid is None or bid <= 0:
        return None                                   # no fresh bid
    fl = math.ceil((bid - max(0.0, slip)) / TICK - 1e-9) * TICK
    if not (0.0 < fl < bid):
        return None                                   # "nothing to take"
    return bid                                        # measured: fills at bid


def _draws(seed, n):
    """Deterministic per-position coin flips, so a completion rate is a
    property of the run and not of the scheduler."""
    h = hashlib.blake2b(str(seed).encode(), digest_size=8).digest()
    x = int.from_bytes(h, "big") or 1
    out = []
    for _ in range(n):
        x = (x * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        out.append((x >> 11) / float(1 << 53))
    return out


def exit_at(ticks, i0, held, pay, rule, prm, floor_bps, slip, min_tau,
            p_fill=1.0, seed=0):
    """Walk forward from the entry and return (ts, price) or None."""
    armed = None
    best_bid = None
    draws = []
    seen = []
    for k in range(i0 + 1, len(ticks)):
        ts, tau, ev, margin, ub, db = ticks[k]
        if tau <= min_tau:
            return None
        bid = ub if held == "Up" else db
        if best_bid is None or (bid and bid > best_bid):
            best_bid = bid
        side_now = "Up" if margin > 0 else "Down"
        adverse = (side_now != held)
        hit = False
        if rule == "flip":
            confirm_s = prm
            past = (floor_bps <= 0) or (abs(margin) >= floor_bps)
            if side_now == held and past:
                armed = None
            elif armed is None:
                if ev is not None and ev != held:
                    armed = ts
            elif side_now != held and past and ts - armed >= confirm_s * 1000:
                hit = True
        elif rule == "stop":
            hit = bid > 0 and bid <= pay - prm
        elif rule == "take":
            hit = bid > 0 and bid >= pay + prm
        elif rule == "bracket":
            lo, hi = prm
            hit = bid > 0 and (bid <= pay - lo or bid >= pay + hi)
        elif rule == "trail":
            hit = (best_bid is not None and bid > 0
                   and best_bid - bid >= prm and best_bid > pay)
        elif rule == "margin":
            hit = adverse and abs(margin) >= prm
        elif rule == "mute":
            # ⭐ A PRICE STOP THAT ONLY SPEAKS WHEN THE SIGNAL DOES NOT.
            # `either` ORs the two unconditionally and loses $10-13 out of
            # sample, because the price swings far more than the signal inside a
            # round and a stop then cuts positions that recover. But the failure
            # the operator actually saw is narrower than that: the signal was
            # MUTE, not adverse.
            #   LIVE 2026-09-19 bnb-1789802100: entered 0.42, margin oscillated
            #   -1.64/+2.95/-1.51/+2.43/-0.33/-2.43 -- never 4 bps either way,
            #   so the margin rule had no opinion -- while the bid went
            #   0.76 -> 0.24 -> 0.00. The price knew; the signal could not say.
            # So: leave on the margin as usual, OR on the price ONLY while
            # |margin| is under `ms`, i.e. only while the signal is not
            # adjudicating. When the signal IS talking, the price is ignored and
            # recoveries are not cut.
            ms, sx = prm
            hit = ((adverse and abs(margin) >= 4.0)
                   or (abs(margin) < ms and bid > 0 and bid <= pay - sx))
        elif rule == "adapt":
            # ⭐ THE THRESHOLD SCALED BY WHAT THIS ROUND IS ACTUALLY DOING,
            # instead of a flat number that means 41% of a typical btc-5m round
            # and 15% of an eth-15m one. ONE parameter, not nine: a per-family
            # table would be ~23 exits per cell on a four-day tape, which is
            # fitting noise. Scaling per ROUND is also strictly better than per
            # COIN -- it tracks the conditions in front of it rather than a
            # static family average.
            #
            # The scale is the round's own |margin| dispersion since the entry,
            # which needs no new data: the margin series is already the thing
            # the rule reads.
            seen.append(abs(margin))
            if len(seen) >= 8:
                sc = (sum(seen) / len(seen)) or 0.0
                hit = adverse and abs(margin) >= prm * sc
        elif rule == "either":
            # MARGIN **OR** A PRICE STOP. The margin rule invalidates the
            # SIGNAL; it cannot cut a loss the signal never invalidates, and
            # only 29% of positions ever put the signal 4 bps offside. This
            # adds the backstop: leave when the signal turns OR the bid falls
            # `x` under what we paid, whichever prints first.
            #   LIVE 2026-09-19 bnb-1789802100: entered 0.42, signal oscillated
            #   -1.64/+2.95/-1.51/+2.43/-0.33/-2.43 and never reached -4.0, so
            #   the margin rule held while the bid went 0.76 -> 0.24 -> 0.00.
            #   A 0.20 stop fires at 07:17:53 with the bid at 0.24.
            mb, sx = prm
            hit = ((adverse and abs(margin) >= mb)
                   or (bid > 0 and bid <= pay - sx))
        elif rule == "late":
            hit = tau <= prm and bid > 0 and bid < pay
        if hit:
            px = _sellable(bid, slip)
            if px is not None:
                # THE SALE CAN SIMPLY NOT LAND, and the live arm then RETRIES on
                # the next tick ("sell did not fill (%s) -- retrying next tick").
                # So a miss is not an abandoned exit, it is a delayed one -- and
                # modelling it as abandonment would understate the exit as badly
                # as ignoring it overstates it.
                if p_fill >= 1.0:
                    return ts, px
                if not draws:
                    draws.extend(_draws(seed, 64))
                if draws.pop() < p_fill:
                    return ts, px
                # else: fall through and try again at the next qualifying tick
    return None


def sim(R, *, bankroll, expo, rule, prm, min_tau, p_fill=1.0,
        stake=1.0, days=None, **gk):
    fc = gk.pop("fill_cap")
    ev = []
    for end, cands, ticks, *_ in R:
        c = claim(cands, **gk)
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
        if days is not None and d not in days:
            continue
        ev.append((c[0], end, c, ticks))
    ev.sort(key=lambda x: (x[0], x[1]))
    slots, tot, n, wins, blk, exits, per, dd = [], 0.0, 0, 0, 0, 0, [], []
    for t0, end, c, ticks in ev:
        slots = [e for e in slots if e > t0]
        s = stake * len(slots)
        if s > 0 and s + stake > expo * bankroll:
            blk += 1; continue
        (ts, tau, side, am, ask, won, age, dok, dbps, flip, gap, ti, rng) = c
        if ask > fc:
            continue
        pay = min(max(ask + EXEC, 0.01), 0.99)
        sh = stake / pay
        free_at, x = end, None
        if rule != "off":
            x = exit_at(ticks, ti, side, pay, rule, prm, gk["floor"],
                        LIVE["cross_exit_slip"], min_tau,
                        p_fill=p_fill, seed=ts)
        if x:
            xts, xpx = x
            tot += (xpx - pay) * sh - FEE(pay) * sh
            per.append((xpx - pay) - FEE(pay))
            wins += (xpx > pay); free_at = xts; exits += 1
        else:
            ps = (1.0 if won else 0.0) - pay - FEE(pay)
            tot += ps * sh; per.append(ps); wins += won
        n += 1; slots.append(free_at)
        dd.append(dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%m-%d"))
    if n < 15:
        return None
    v, d = np.array(per), np.array(dd)
    sd = v.std(ddof=1)
    return dict(n=n, blk=blk, exits=exits, win=100 * wins / n, d=tot,
                t=v.mean() / (sd / math.sqrt(n)) if sd else 0.0)


MODELS = [("off", [None]),
          ("flip", [1.0, 3.0, 5.0, 10.0]),
          ("stop", [0.05, 0.10, 0.15, 0.20]),
          ("take", [0.05, 0.10, 0.15, 0.20]),
          ("bracket", [(0.10, 0.10), (0.15, 0.10), (0.10, 0.15), (0.20, 0.15)]),
          ("trail", [0.05, 0.08, 0.12]),
          ("margin", [0.5, 1.0, 2.0, 4.0]),
          ("late", [30.0, 60.0, 90.0]),
          ("either", [(4.0, 0.20), (4.0, 0.30)]),
          ("adapt", [2.5]),
          ("mute", [(1.0, 0.15), (1.0, 0.20), (1.0, 0.25),
                    (1.5, 0.15), (1.5, 0.20), (1.5, 0.25),
                    (2.0, 0.20), (2.0, 0.25), (2.0, 0.30)])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument("--bankroll", type=float, default=5.11)
    ap.add_argument("--min-tau", type=float, default=60.0)
    a = ap.parse_args()
    G = dict(floor=LIVE["cross_min_margin_bps"], ceil=LIVE["cross_max_margin_bps"],
             px_lo=LIVE["cross_min_price"], px_hi=LIVE["cross_max_price"],
             tau_lo=LIVE["cross_min_tau_s"], tau_hi=LIVE["cross_max_tau_s"],
             fill_cap=LIVE["cross_max_fill_px"])
    B, E = a.bankroll, LIVE["cross_max_open_stake_pct"]
    res = {}
    for look in (LIVE["cross_look_s"], 2.0):
        R = build(look, LIVE["cross_hold_s"], a.jobs)
        days = sorted({dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).strftime("%m-%d")
                       for _, cs, *_ in R for c in cs})
        TE = set(days[len(days) // 2:])
        print("\n" + "=" * 98)
        print(f"look_s {look}   bankroll ${B:.2f}   expo {E}   min_tau {a.min_tau:.0f}s"
              f"   test {sorted(TE)}")
        print("=" * 98)
        print("  %-9s %-12s %-40s %s" % ("rule", "param", "FULL TAPE", "TEST (unseen)"))
        for rule, prms in MODELS:
            for prm in prms:
                f = sim(R, bankroll=B, expo=E, rule=rule, prm=prm,
                        min_tau=a.min_tau, **G)
                t = sim(R, bankroll=B, expo=E, rule=rule, prm=prm,
                        min_tau=a.min_tau, days=TE, **G)
                if not f or not t:
                    continue
                res.setdefault((rule, str(prm)), {})[look] = (f, t)
                print("  %-9s %-12s n=%4d ex=%4d win%5.1f%% $%+7.2f t%+5.2f   "
                      "| n=%4d win%5.1f%% $%+7.2f"
                      % (rule, prm, f["n"], f["exits"], f["win"], f["d"], f["t"],
                         t["n"], t["win"], t["d"]))
    # the pre-registered rule: rank by MIN(train look1, train look2)
    L1, L2 = LIVE["cross_look_s"], 2.0
    print("\n" + "=" * 98)
    print("RANKED BY MIN(train L1, train L2)   [train = full - test, the rule set"
          " before the run]")
    print("=" * 98)
    rows = []
    for k, v in res.items():
        if L1 not in v or L2 not in v:
            continue
        tr1 = v[L1][0]["d"] - v[L1][1]["d"]
        tr2 = v[L2][0]["d"] - v[L2][1]["d"]
        rows.append((min(tr1, tr2), k, tr1, tr2, v[L1][1]["d"], v[L2][1]["d"],
                     v[L1][0]["win"], v[L2][0]["win"]))
    rows.sort(reverse=True)
    print("  %-9s %-12s %-20s %-20s %s"
          % ("rule", "param", "TRAIN L1 / L2", "TEST L1 / L2", "win% L1 / L2"))
    for m, k, tr1, tr2, te1, te2, w1, w2 in rows[:14]:
        print("  %-9s %-12s $%+7.2f / $%+7.2f   $%+7.2f / $%+7.2f   %5.1f%% / %5.1f%%"
              % (k[0], k[1], tr1, tr2, te1, te2, w1, w2))


if __name__ == "__main__":
    main()
