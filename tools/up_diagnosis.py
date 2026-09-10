"""Why the Up side loses money, and whether a filter can fix it.

The Down side of this book pays and the Up side does not:

    live      114 settled   Down 62 trades 69.4% WR +$50.16
                            Up   52 trades 55.8% WR  -$3.18
    replay     52 trades    Down 34 trades 73.5% WR +$17.20
                            Up   18 trades 44.4% WR  -$3.43

"There is a bias" is not an answer, because the three possible shapes of the
bias have different fixes:

  1. the model's Up probability is unbiased, but buying the highest claimed
     edges selects on its own error -> a threshold fixes it;
  2. the Up probability is too high by a constant inside the traded band ->
     no threshold on edge or price fixes it; the probability needs shifting;
  3. the bias lives in a price BAND (e.g. only mid-priced Up) -> refusing
     that band fixes it.

So this measures the model's claimed probability against what actually
happened, per side, in price buckets, on the SAME ticks the live gate looked
at. Ticks within a round are correlated, so every row is one FIRST-qualifying
tick per round -- the sample the gate would have traded, and independent.

Columns: `n` rounds, `raw p` mean model P(side), `realised` win rate, `bias`
= raw p - realised (positive = model too confident), `EV` realised profit per
$1 staked (=$1 flat stake, real per-price fee), with its standard error and
t-statistic. `EV` is the only number that decides whether a bucket is worth
trading.

Usage:
    python -m tools.up_diagnosis
    python -m tools.up_diagnosis --market btc-updown-5m --min-n 10
"""
from __future__ import annotations

import argparse
import collections
import math
import statistics as st
from dataclasses import dataclass, replace

from btcpredict.config import settings
from btcpredict.edge.signal import (FeeModel, breakeven_prob, evaluate,
                                    fee_rate_for_price)
from btcpredict.models import build_model
from tools.backtest_candle_open import ReplayParams, replay
from tools.sizing_study import load_inputs

BUCKETS = (0.30, 0.45, 0.55, 0.65, 0.75, 0.85, 0.96)


def bucket(q: float) -> str:
    lo = BUCKETS[0]
    for hi in BUCKETS[1:]:
        if q < hi:
            return f"[{lo:.2f},{hi:.2f})"
        lo = hi
    return f"[{lo:.2f},"


def _eval(params, fair_up, up_ask, down_ask, max_quote=None, min_edge=None):
    return evaluate(
        fair_up, up_ask, down_ask, fee=None, half_spread=0.0,
        safety_margin=params.safety_margin,
        fee_model=(FeeModel.NOTIONAL if params.fee_model == "notional"
                   else FeeModel.PROFIT),
        max_quote=params.max_quote if max_quote is None else max_quote,
        min_quote=params.min_quote,
        min_edge=params.min_edge if min_edge is None else min_edge,
        edge_haircut=params.edge_haircut)


@dataclass
class Rule:
    """A per-side Up rule layered on top of the live gate."""

    up_min_edge: float | None = None    # None = the live global min_edge
    up_max_quote: float | None = None   # None = the live global max_quote
    min_quote: float | None = None      # floor on the price paid, both sides
    up_min_quote: float | None = None   # floor on the price paid, Up only
    up_shift: float = 0.0               # subtract from the Up probability
    side: str = "both"                  # both / Up / Down


def decide(t: dict, params: ReplayParams, rule: Rule):
    """The signal the gate would fire on one tick under `rule`."""
    fair_up = t["p_up"] - rule.up_shift
    if rule.side == "Down":
        sig = _eval(params, fair_up, None, t["down_ask"])
    else:
        sig = _eval(params, fair_up, t["up_ask"], t["down_ask"])
        if sig.act and sig.side == "Up" and not t.get("up_allowed", True):
            # The live rising-regime gate: it blocks Up BEFORE the side pick,
            # so a blocked Up still leaves the Down candidate alive.
            sig = _eval(params, fair_up, None, t["down_ask"])
        if sig.act and sig.side == "Up":
            too_dear = (rule.up_max_quote is not None
                        and sig.quote > rule.up_max_quote)
            too_thin = (rule.up_min_edge is not None
                        and sig.edge < rule.up_min_edge)
            if too_dear or too_thin:
                # Mirrors the live up_regime_gate: a refused Up does not kill
                # the quote, it lets Down qualify on its own terms.
                sig = _eval(params, fair_up, None, t["down_ask"])
    if rule.side == "Up" and sig.side != "Up":
        sig.act, sig.side = False, None
    if sig.act:
        floor = rule.min_quote
        if sig.side == "Up" and rule.up_min_quote is not None:
            floor = max(floor or 0.0, rule.up_min_quote)
        if floor is not None and sig.quote < floor:
            # A floor on the price paid refuses the trade outright: the other
            # side is a different bet, not a substitute for this one.
            sig.act, sig.side = False, None
    return sig


def simulate(ticks: list[dict], params: ReplayParams, rule: Rule) -> list[dict]:
    """Re-apply the decision path to the recorded ticks under `rule`.

    One position per round, `signal_persistence` consecutive agreeing polls of
    the same candle -- the same shape as tools.backtest_candle_open.replay, so
    the default rule must reproduce its trade list exactly (checked in main).
    """
    rounds: dict[str, list[dict]] = collections.OrderedDict()
    for t in ticks:
        rounds.setdefault(t["slug"], []).append(t)
    trades = []
    for slug, ts in rounds.items():
        candle, prev, n = None, None, 0
        for t in ts:
            ci = t["ts_ms"] // 60_000
            if candle != ci:
                candle, prev, n = ci, None, 0
            sig = decide(t, params, rule)
            if sig.act and sig.side == prev:
                n += 1
            elif sig.act:
                prev, n = sig.side, 1
            else:
                prev, n = None, 0
            if not (sig.act and n >= params.signal_persistence):
                continue
            fill = t["up_ask"] if sig.side == "Up" else t["down_ask"]
            if fill is None or not 0.0 < fill < 1.0:
                break
            won = (sig.side == "Up") == t["outcome_up"]
            f = fee_rate_for_price(fill)
            trades.append({
                "slug": slug, "side": sig.side, "q": fill, "won": won,
                "fair": sig.fair, "edge": sig.edge,
                "pnl": (1.0 / fill if won else 0.0) - 1.0 - f, "fee": f,
            })
            break                       # one position per round
    return trades


def line(label: str, trades: list[dict]) -> None:
    n = len(trades)
    if not n:
        print(f"  {label:<26}{'--':>7}")
        return
    rs = [t["pnl"] for t in trades]
    wr = 100.0 * sum(1 for t in trades if t["won"]) / n
    ev = st.mean(rs)
    se = st.pstdev(rs) / math.sqrt(n) if n > 1 else 0.0
    q = st.mean([t["q"] for t in trades])
    print(f"  {label:<26}{n:>6}{q:>8.3f}{wr:>9.1f}{sum(rs):>+10.2f}"
          f"{ev:>+9.3f}{se:>8.3f}{ev / se if se else 0:>+7.2f}")


def head(title: str) -> None:
    print(f"\n{title}")
    print(f"  {'':<26}{'n':>6}{'mean q':>8}{'WR%':>9}{'P&L':>10}"
          f"{'EV/$1':>9}{'se':>8}{'t':>7}")


def calibration(ticks: list[dict], params: ReplayParams, side: str,
                require_edge: float | None = None) -> None:
    """Claimed vs realised probability, bucketed by the price we would pay.

    One first-qualifying tick per round. With `require_edge` the row is
    restricted to rounds the live gate would actually trade (post-haircut edge
    at least the live min_edge), which is the population the trades come from;
    without it the row also contains rounds the edge bar filters out -- useful
    for seeing the model's claim everywhere, but NOT the traded sample.
    `bias` is the model's raw probability minus what happened; positive = too
    confident. `realEV` is what a $1 flat stake actually returned there, net of
    the real per-price fee -- the only column that decides if it is tradeable.
    """
    seen: set[str] = set()
    rows: dict[str, list[tuple[float, float, float, bool, float]]] = {}
    for t in ticks:
        if t["slug"] in seen:
            continue
        q = t["up_ask"] if side == "Up" else t["down_ask"]
        if q is None or not 0.0 < q < 1.0:
            continue
        raw = t["p_up"] if side == "Up" else 1.0 - t["p_up_down_model"]
        p_used = q + params.edge_haircut * (raw - q)
        f = fee_rate_for_price(q)
        if p_used <= breakeven_prob(q, f, FeeModel.NOTIONAL):
            continue                    # the gate would not look at it
        if (require_edge is not None
                and p_used - q < require_edge):
            continue                    # the edge bar would refuse it anyway
        seen.add(t["slug"])
        won = t["outcome_up"] if side == "Up" else not t["outcome_up"]
        rows.setdefault(bucket(q), []).append((q, raw, p_used, won, f))

    qualifier = ("all rounds the model likes" if require_edge is None
                 else f"rounds clearing the live edge bar {require_edge:.2f}")
    print(f"\n{side} side, {qualifier}: one tick per round")
    print(f"  {'band':<12}{'n':>5}{'mean q':>8}{'model p':>9}{'realised':>10}"
          f"{'bias':>8}{'claimEV':>9}{'realEV':>9}{'se':>8}{'t':>7}")
    for b in sorted(rows, key=lambda s: float(s[1:5])):
        rs = rows[b]
        n = len(rs)
        if n < 5:
            continue
        q = st.mean([r[0] for r in rs])
        raw = st.mean([r[1] for r in rs])
        claimed = st.mean([r[2] for r in rs])
        wr = sum(1 for r in rs if r[3]) / n
        claim_ev = st.mean([r[2] / r[0] - 1.0 - r[4] for r in rs])
        rets = [((1.0 / r[0]) if r[3] else 0.0) - 1.0 - r[4] for r in rs]
        ev = st.mean(rets)
        se = st.pstdev(rets) / math.sqrt(n) if n > 1 else 0.0
        print(f"  {b:<12}{n:>5}{q:>8.3f}{raw:>9.3f}{100 * wr:>9.1f}%"
              f"{raw - wr:>+8.3f}{claim_ev:>+9.3f}{ev:>+9.3f}{se:>8.3f}"
              f"{ev / se if se else 0:>+7.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--min-n", type=int, default=5)
    args = ap.parse_args()

    params = ReplayParams.from_settings()
    quotes, sbs, sigs, hist = load_inputs(args.market)
    model = build_model("digital")
    ticks: list[dict] = []
    res = replay(quotes, sbs, sigs, hist, model, None, False, params=params,
                 model_up=build_model(settings.up_model),
                 collected=ticks)

    print(f"{args.market}: {len(sbs)} settled rounds, {len(ticks)} evaluated ticks")
    print(f"live gate: min_edge {params.min_edge:.2f}  haircut "
          f"{params.edge_haircut:.2f}  quotes [{params.min_quote:.2f},"
          f"{params.max_quote:.2f}]  persistence {params.signal_persistence}  "
          f"up_model {settings.up_model}")

    print(f"\nmodel under test: {settings.up_model} for Up, digital for Down")
    print(f"replay by side  ({len(res['trades'])} trades)")
    head("baseline")
    for s in ("Up", "Down", "both"):
        line(s, [t for t in res["trades"] if s == "both" or t["side"] == s])

    mine = simulate(ticks, params, Rule())
    theirs = {(t["slug"], t["side"]) for t in res["trades"]}
    got = {(t["slug"], t["side"]) for t in mine}
    ok = theirs == got
    print(f"\nreproduction check: {len(got)} trades vs replay's "
          f"{len(theirs)} -> {'IDENTICAL' if ok else 'MISMATCH'}")
    if not ok:
        print(f"  only in mine  : {sorted(got - theirs)[:5]}")
        print(f"  only in replay: {sorted(theirs - got)[:5]}")

    for side in ("Up", "Down"):
        calibration(ticks, params, side)
        calibration(ticks, params, side, require_edge=params.min_edge)

    print("\nUp-only rules (no Down fallback), realised on the recording")
    print(f"  {'':<26}{'n':>6}{'mean q':>8}{'WR%':>9}{'P&L':>10}"
          f"{'EV/$1':>9}{'se':>8}{'t':>7}")
    rules: list[tuple[str, Rule]] = [("live gate, Up only", Rule(side="Up"))]
    for e in (0.12, 0.15, 0.18, 0.20, 0.25, 0.30):
        rules.append((f"Up edge >= {e:.2f}", Rule(side="Up", up_min_edge=e)))
    for c in (0.90, 0.80, 0.70, 0.60, 0.50):
        rules.append((f"Up quote <= {c:.2f}", Rule(side="Up", up_max_quote=c)))
    for r in (0.60, 0.70, 0.80):
        for e in (0.20, 0.30):
            rules.append((f"Up <= {r:.2f} & edge >= {e:.2f}",
                          Rule(side="Up", up_max_quote=r, up_min_edge=e)))
    for label, rule in rules:
        line(label, simulate(ticks, params, rule))

    print("\nshifting the Up probability down (a recalibration, not a filter)")
    print(f"  {'':<26}{'n':>6}{'mean q':>8}{'WR%':>9}{'P&L':>10}"
          f"{'EV/$1':>9}{'se':>8}{'t':>7}")
    for d in (0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.08):
        line(f"p_up - {d:.2f}", simulate(ticks, params, Rule(side="Up",
                                                            up_shift=d)))

    print("\nboth sides, with the Up bar raised (Down still trades)")
    print(f"  {'':<26}{'n':>6}{'mean q':>8}{'WR%':>9}{'P&L':>10}"
          f"{'EV/$1':>9}{'se':>8}{'t':>7}")
    line("live gate", simulate(ticks, params, Rule()))
    for e in (0.15, 0.20, 0.25, 0.30):
        line(f"Up edge >= {e:.2f}", simulate(ticks, params,
                                             Rule(up_min_edge=e)))
    for d in (0.02, 0.04, 0.05):
        line(f"p_up - {d:.2f}", simulate(ticks, params, Rule(up_shift=d)))

    print("\nprice floor: never pay less than this for either side")
    print(f"  {'':<26}{'n':>6}{'mean q':>8}{'WR%':>9}{'P&L':>10}"
          f"{'EV/$1':>9}{'se':>8}{'t':>7}")
    for floor in (None, 0.30, 0.40, 0.45, 0.50, 0.55, 0.60):
        label = ("live (no floor)" if floor is None else f"floor {floor:.2f}")
        line(label, simulate(ticks, params, Rule(min_quote=floor)))

    print("\nprice floor on Up only (Down unchanged -- keeps upside alive)")
    print(f"  {'':<26}{'n':>6}{'mean q':>8}{'WR%':>9}{'P&L':>10}"
          f"{'EV/$1':>9}{'se':>8}{'t':>7}")
    for floor in (0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        tr = simulate(ticks, params, Rule(up_min_quote=floor))
        line(f"Up floor {floor:.2f}", tr)
        line(f"   of which Up", [t for t in tr if t["side"] == "Up"])


if __name__ == "__main__":
    main()
