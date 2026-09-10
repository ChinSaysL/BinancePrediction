"""Trade-level backtest of the candle-open gate against recorded real quotes.

The venue exposes no historical quote feed, but the recorder stores every poll:
executable asks, spot, tau and fee metadata for the settled rounds it saw. This
replays the *exact* live decision path (model -> edge gate -> persistence streak
-> candle-open window -> fill at ask -> settle) over those recordings, so it
measures the gate itself, not a model idealisation.

It also reruns the same path with the candle-open gate switched off, to show
what the gating changed, and compares the measured fee curve against the old
flat-fee assumption (the real fee is per-quote: fee_bps = 180 * min(1, (1-px)/px)).

Usage:  python -m tools.backtest_candle_open [--model digital|all] [--fee 0.0064]
        (no --fee = measured fee curve)
"""
from __future__ import annotations

import argparse
import math
import random
import statistics as st
import time
from collections import defaultdict
from dataclasses import dataclass, replace

from btcpredict.calibration.report import PnLSummary, score
from btcpredict.config import MARKETS, settings
from btcpredict.edge.signal import (FeeModel, Signal, evaluate,
                                    evaluate_hybrid, fee_rate_for_price)
from btcpredict.models import MODEL_NAMES, ModelContext, build_model
from btcpredict.models.calibrated import taker_imbalance
from btcpredict.store.db import Store

BAR = "=" * 78


@dataclass
class ReplayParams:
    """Every gate knob the replay honours, seeded from live settings."""

    min_edge: float = 0.05
    # Must track live: a replay that believes the raw model edge measures a
    # different strategy from the one that trades.
    edge_haircut: float = 1.0
    max_quote: float = 0.90
    min_quote: float = 0.15
    # Signal-strength floor: |ln(spot/strike)| / (sigma*sqrt(tau/60)). Measured
    # on an independent venue (Polymarket 5m crypto, 1,292 trades) the per-trade
    # return rises monotonically with this quantity, and weak-signal trades
    # LOSE money even though the edge gate accepts them. On OUR recording it is
    # the reverse at every floor -- see settings.min_moneyness_sd. 0.0 = off.
    min_moneyness_sd: float = 0.0
    signal_persistence: int = 3
    candle_open_window_s: float = 5.0
    safety_margin: float = 0.01
    min_tau_s: float = 60.0
    max_tau_s: float = 240.0
    trade_candle_opens: tuple = (1, 2, 3, 4)
    side_filter: str = "both"   # both / Up / Down -- mirrors settings
    up_regime_gate: float = 0.0  # skip Up when 1h log return exceeds this
    # Fee semantics are unconfirmed; NOTIONAL (on the stake) is the stricter
    # of the two, so it is the default. PROFIT is the looser guess.
    fee_model: str = "notional"

    @classmethod
    def from_settings(cls) -> "ReplayParams":
        return cls(
            min_edge=settings.min_edge,
            edge_haircut=settings.edge_haircut,
            max_quote=settings.max_quote,
            min_quote=settings.min_quote,
            min_moneyness_sd=settings.min_moneyness_sd,
            signal_persistence=settings.signal_persistence,
            candle_open_window_s=settings.candle_open_window_s,
            safety_margin=settings.safety_margin,
            min_tau_s=settings.min_tau_s,
            max_tau_s=settings.max_tau_s,
            trade_candle_opens=tuple(settings.trade_candle_opens),
            side_filter=settings.side_filter,
            up_regime_gate=settings.up_regime_gate,
        )


def rolling_sigma(settled: list[dict], idx: int, window: int = 48) -> float:
    """sigma_1m from the `window` settled rounds BEFORE idx (out of sample)."""
    lo = max(0, idx - window)
    rets = []
    for r in settled[lo:idx]:
        s, e = r["strike"], r["end_price"]
        if s and e and s > 0 and e > 0:
            rets.append(math.log(e / s) / math.sqrt(5.0))  # 5m return -> 1m
    if len(rets) < 20:
        return 0.0004
    return max(0.00015, st.pstdev(rets))


def build_1m_history(quotes: list[dict], store=None, symbol: str | None = None
                     ) -> list[tuple[int, float, float, float]]:
    """1m (minute, close, volume, taker_buy) bars.

    close = last spot polled in that minute (the venue's own series, and the
    only price the strategy actually saw). volume/taker_buy come from the stored
    Binance bars when available, because the quotes table cannot reconstruct
    order flow at all -- without it the order-flow models (sentiment, hybrid)
    can never be replayed. Missing taker data is written as 0.0, which makes
    those models hold rather than invent a signal. Sorted by minute.
    Quotes must be sorted by ts_ms.
    """
    minutes: dict[int, list[float]] = defaultdict(list)
    for q in quotes:
        if q["spot"] and q["spot"] > 0:
            minutes[q["ts_ms"] // 60_000].append(q["spot"])

    stored: dict[int, tuple[float, float]] = {}
    if store is not None and symbol and minutes:
        first = min(minutes)
        for row in store.bars_since(symbol, first * 60_000):
            stored[row["open_ms"] // 60_000] = (row["volume"] or 0.0,
                                                row["taker_buy"] or 0.0)

    out = []
    for m, spots in sorted(minutes.items()):
        vol, taker = stored.get(m, (float(len(spots)), 0.0))
        out.append((m, spots[-1], vol, taker))
    return out


def replay(quotes, settled_by_slug, sigma_by_slug, hist, model,
           fee: float | None, candle_gate: bool, params: ReplayParams,
           model_up=None, collected: list | None = None) -> dict:
    """Replay the decision path over recorded quotes (sorted by time).

    fee=None -> the measured per-price fee curve is used both in the gate and
    in PnL accounting; a float overrides with a flat rate for comparisons.
    model_up (optional) mirrors the live per-side gate: Down candidates are
    priced by `model`, Up candidates by `model_up`.

    collected (optional): if a list is passed, EVERY evaluated tick is appended
    to it as {slug, tau, p_up, outcome_up, ...}. Calibration questions need the
    full population, not the handful of ticks the gate happened to trade -- the
    acted sample is ~70 trades, while the evaluated population is thousands of
    ticks across every settled round. Ticks inside a round are correlated, so
    consumers must cluster by slug before computing errors.
    """
    pnl = PnLSummary()
    trades: list[dict] = []
    open_rounds: set[str] = set()
    streak: dict[str, tuple[str, int]] = {}
    candle: dict[str, int] = {}
    # Only bars that CLOSED before the current minute are visible -- extending
    # the model's history with future bars would be look-ahead bias. The cursor
    # advances monotonically because quotes are sorted by ts_ms.
    bars = hist
    cursor = 0
    closes: list[float] = []
    vols: list[float] = []
    takers: list[float] = []
    # With candle gating the final candle opens at tau ~= 59.9s, just under
    # min_tau_s=60; the allowance lets the decision stand for its open time.
    min_tau = (params.min_tau_s - params.candle_open_window_s
               if candle_gate else params.min_tau_s)

    for q in quotes:
        slug = q["slug"]
        if slug in open_rounds:
            continue                            # one position per round
        r = settled_by_slug.get(slug)
        if not r:
            continue
        tau, spot = q["tau_s"], q["spot"]
        strike = r["strike"]
        if (spot is None or strike is None or not 0 < tau <= 300
                or q["up_ask"] is None or q["down_ask"] is None):
            continue
        if not (min_tau <= tau <= params.max_tau_s):
            continue

        sec = (q["ts_ms"] // 1000) % 60
        if candle_gate and sec >= params.candle_open_window_s:
            continue
        if candle_gate and round(tau / 60.0) not in params.trade_candle_opens:
            continue

        ci = q["ts_ms"] // 60_000
        if candle.get(slug) != ci:
            candle[slug] = ci
            streak.pop(slug, None)              # fresh prediction each candle

        # Candle series up to now (completed bars only), extended with the
        # live spot tick.
        while cursor < len(bars) and bars[cursor][0] < ci:
            closes.append(bars[cursor][1])
            vols.append(bars[cursor][2])
            takers.append(bars[cursor][3])
            cursor += 1
        ctx = ModelContext(
            closes + [spot], vols, takers, price=spot,
            strike=strike, tau_s=tau, sigma_1m=sigma_by_slug.get(slug, 0.0004),
            # Live passes the executable Up ask; a market-anchored model must
            # see the same number here or the replay prices a different book.
            quote_up=q["up_ask"])
        pred = model.predict(ctx)
        if not pred.emitted:
            continue
        # Rising-regime gate for Up, mirrored from live (config.up_regime_gate):
        # decided BEFORE the side pick, so a blocked Up still falls back to a
        # Down candidate that qualified on the same quote.
        up_allowed, blocked_ret = True, None
        if params.up_regime_gate > 0:
            c0 = bars[cursor - 1][1] if cursor > 0 else None
            past = [b for b in bars[:cursor] if b[0] <= ci - 60]
            if c0 and past and c0 > 0 and past[-1][1] > 0:
                ret = math.log(c0 / past[-1][1])
                if ret > params.up_regime_gate:
                    up_allowed, blocked_ret = False, ret
        up_fair = pred.p_up
        if model_up is not None:
            pred_up = model_up.predict(ctx)
            if pred_up.emitted and 0.0 < pred_up.p_up < 1.0:
                up_fair = pred_up.p_up
        if collected is not None:
            # Before the gate: this is the model's opinion, not the gate's.
            # The quotes travel with it so a consumer can re-run the gate under
            # different knobs (per-side bars, price caps) on the SAME ticks the
            # live bot actually saw, instead of guessing what it was shown.
            collected.append({
                "slug": slug, "ts_ms": q["ts_ms"], "tau": tau,
                "p_up_down_model": pred.p_up, "p_up": up_fair,
                "outcome_up": r["winner"] == "Up",
                "up_ask": q["up_ask"], "down_ask": q["down_ask"],
                "spot": spot, "strike": strike,
                "sigma": sigma_by_slug.get(slug, 0.0004),
                "up_allowed": up_allowed, "blocked_ret": blocked_ret,
                "imb": taker_imbalance(takers, vols),
            })
        sig = evaluate_hybrid(
            pred.p_up, up_fair, q["up_ask"], q["down_ask"],
            up_allowed=up_allowed,
            fee=fee, half_spread=0.0, safety_margin=params.safety_margin,
            fee_model=FeeModel.NOTIONAL if params.fee_model == "notional"
            else FeeModel.PROFIT,
            max_quote=params.max_quote, min_quote=params.min_quote,
            min_edge=params.min_edge, edge_haircut=params.edge_haircut)
        if blocked_ret is not None and not sig.act:
            sig = Signal(None, sig.fair, sig.quote, sig.edge, sig.hurdle,
                         sig.ev_per_dollar, False,
                         f"Up skipped: 1h ret {blocked_ret:+.4f} > "
                         f"{params.up_regime_gate:+.3f}")

        # Signal-strength floor. |z| is how decided the round already is, in
        # units of the remaining move -- it is independent of where the venue
        # prices the side, so it catches the trades the edge gate wrongly
        # accepts (cheap because unlikely, not because mispriced). Recorded on
        # every trade so sizing studies can use it.
        sigma_used = sigma_by_slug.get(slug, 0.0004)
        mz = (abs(math.log(spot / strike))
              / (sigma_used * math.sqrt(tau / 60.0))
              if spot > 0 and strike > 0 and tau > 0 else 0.0)
        if sig.act and params.min_moneyness_sd > 0:
            if mz < params.min_moneyness_sd:
                sig = Signal(None, sig.fair, sig.quote, sig.edge, sig.hurdle,
                             sig.ev_per_dollar, False,
                             f"weak signal: |z| {mz:.2f} < "
                             f"{params.min_moneyness_sd:.2f}")

        prev_side, prev_n = streak.get(slug, (None, 0))
        if sig.act and sig.side == prev_side:
            streak[slug] = (sig.side, prev_n + 1)
        elif sig.act:
            streak[slug] = (sig.side, 1)
        else:
            streak.pop(slug, None)
        n = streak.get(slug, (None, 0))[1]

        if sig.act and n >= params.signal_persistence:
            if (params.side_filter != "both"
                    and sig.side != params.side_filter):
                streak.pop(slug, None)      # skip this signal; round stays open
                continue
            fill = q["up_ask"] if sig.side == "Up" else q["down_ask"]
            won = sig.side == r["winner"]
            f = fee if fee is not None else fee_rate_for_price(fill)
            trade_pnl = ((1.0 / fill if won else 0.0) - 1.0 - f)
            pnl.add(trade_pnl, 1.0, f, won)
            trades.append({
                "slug": slug, "side": sig.side, "tau": tau,
                "q": fill, "p": sig.fair, "edge": sig.edge,
                "won": won, "pnl": trade_pnl, "mz": mz,
                "ts_ms": q["ts_ms"], "round_start": r["start_ms"],
            })
            open_rounds.add(slug)
            streak.pop(slug, None)
    return {"pnl": pnl, "trades": trades}


def print_trades(label: str, res: dict) -> None:
    t = res["trades"]
    print(f"\n{label}: {len(t)} trades")
    if not t:
        print("  no trades")
        return
    print(f"  {res['pnl']}")
    by_tau = defaultdict(PnLSummary)
    for x in t:
        # Candle opens poll at tau ~= k*60 - jitter, so round() groups them:
        # 1 = final minute open, 2 = tau~120s, 3 = tau~180s, 4 = tau~240s.
        by_tau[round(x["tau"] / 60.0)].add(x["pnl"], 1.0, 0.0, x["won"])
    for k in sorted(by_tau):
        print(f"    candle {k} (tau~{k * 60}s open): {by_tau[k]}")
    by_side = defaultdict(PnLSummary)
    for x in t:
        by_side[x["side"]].add(x["pnl"], 1.0, 0.0, x["won"])
    print(f"    Up  : {by_side['Up']}")
    print(f"    Down: {by_side['Down']}")
    edges = [x["edge"] for x in t]
    if edges:
        print(f"    acted edge: min {min(edges):+.3f}  median "
              f"{st.median(edges):+.3f}  max {max(edges):+.3f}")


def market_vs_model(quotes, settled_by_slug, sigma_by_slug, hist, model,
                    params: ReplayParams) -> None:
    """Brier of model vs market ask on every candle-open poll (the go/no-go)."""
    preds_m, preds_q, outs = [], [], []
    cursor = 0
    closes, vols, takers = [], [], []
    for q in quotes:
        slug = q["slug"]
        r = settled_by_slug.get(slug)
        if not r or q["up_ask"] is None or q["spot"] is None:
            continue
        if not (0 < q["tau_s"] <= params.max_tau_s):
            continue
        if (q["ts_ms"] // 1000) % 60 >= params.candle_open_window_s:
            continue
        m = q["ts_ms"] // 60_000
        while cursor < len(hist) and hist[cursor][0] < m:
            closes.append(hist[cursor][1])
            vols.append(hist[cursor][2])
            takers.append(hist[cursor][3])
            cursor += 1
        pred = model.predict(ModelContext(
            closes + [q["spot"]], vols, takers,
            price=q["spot"], strike=r["strike"], tau_s=q["tau_s"],
            sigma_1m=sigma_by_slug.get(slug, 0.0004)))
        if not pred.emitted:
            continue
        up = 1 if r["winner"] == "Up" else 0
        preds_m.append(pred.p_up)
        preds_q.append(q["up_ask"])
        outs.append(up)
    if len(outs) < 20:
        print("  too few candle-open quotes for a Brier comparison")
        return
    print(f"\n  model-vs-market at candle opens (n={len(outs)}):")
    print(f"    model : {score(preds_m, outs)}")
    print(f"    market: {score(preds_q, outs)}")


def sweep(quotes, settled_by_slug, sigma_by_slug, hist, model, fee, base) -> None:
    """One-dimensional parameter sweeps around `base` (candle-open gate)."""
    axes = {
        # The grid must bracket the live value on BOTH sides: a sweep that
        # stops at the current setting reports it as the optimum by
        # construction, which is how 0.08 came to look monotone-best.
        "min_edge": [0.03, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.12],
        "max_quote": [0.80, 0.85, 0.90, 0.95],
        # Cheap contracts pay the fee cap (180 bps) regardless of price, so
        # the interesting cut-offs sit higher than they did under flat fees.
        "min_quote": [0.15, 0.25, 0.35, 0.50],
        "signal_persistence": [2, 3, 4, 5, 6],
        "edge_haircut": [0.3, 0.4, 0.5, 0.7, 1.0],
        "max_tau_s": [120.0, 150.0, 180.0, 210.0, 240.0],
        "candle_open_window_s": [3.0, 5.0, 8.0, 10.0],
        "safety_margin": [0.005, 0.010, 0.015, 0.020],
    }
    for axis, values in axes.items():
        print(f"\n  sweep {axis}:")
        for v in values:
            params = replace(base, **{axis: v})
            res = replay(quotes, settled_by_slug, sigma_by_slug, hist, model,
                         fee, candle_gate=settings.trade_on_candle_open,
                         params=params)
            p = res["pnl"]
            mark = "  <-- current" if getattr(base, axis) == v else ""
            print(f"    {axis}={v:<6}: n={p.n_trades:3d} wr={p.win_rate:.2f} "
                  f"pnl={p.pnl:+7.2f} roi={p.roi:+6.1%} "
                  f"mdd={p.max_drawdown:6.2f}{mark}")


def bias_report(quotes, settled_by_slug, sigma_by_slug, hist, model, fee, base) -> None:
    """Checks for the biases that would make the replay number meaningless.

    1. Chronological split -- do the settings win in both halves, or is the
       profit one lucky stretch?
    2. Round-level bootstrap -- trades within a round share an outcome, so
       resample rounds, not trades, for the confidence interval.
    3. Profit concentration -- a few longshot wins can carry everything.
    4. Hour-of-day -- one regime's PnL.
    """
    res = replay(quotes, settled_by_slug, sigma_by_slug, hist, model, fee,
                 candle_gate=settings.trade_on_candle_open, params=base)
    trades = res["trades"]
    if not trades:
        print("  no trades -- nothing to bias-check")
        return
    gate_label = "gated" if settings.trade_on_candle_open else "ungated"
    print(f"\n{BAR}\nBIAS CHECKS on {len(trades)} {gate_label} trades "
          f"(live configuration)\n{BAR}")

    starts = sorted({t["round_start"] for t in trades})
    mid = starts[len(starts) // 2]
    for label, half in (("first half ", [t for t in trades if t["round_start"] < mid]),
                        ("second half", [t for t in trades if t["round_start"] >= mid])):
        s = PnLSummary()
        for t in half:
            s.add(t["pnl"], 1.0, 0.0, t["won"])
        print(f"  {label} (n={len(half)}): {s}")

    def cohort(items) -> PnLSummary:
        s = PnLSummary()
        for t in items:
            s.add(t["pnl"], 1.0, 0.0, t["won"])
        return s

    # Rolling halves: the split is the fastest discriminator between "edge" and
    # "one good window". Watch whether the newest cohort looks like the losing
    # half rather than the winning one.
    by_time = sorted(trades, key=lambda t: t["ts_ms"])
    n_half = max(1, len(by_time) // 2)
    print(f"  rolling halves: oldest {n_half} -> {cohort(by_time[:n_half])}")
    print(f"                  newest {n_half} -> {cohort(by_time[-n_half:])}")

    # The forward test: live settlements, which no sweep has seen.
    live = Store(path=settings.db_path).query("""
        SELECT s.pnl, s.won FROM settlements s
        JOIN fills f ON f.id = s.fill_id
        WHERE f.mode = 'live' AND COALESCE(s.void, 0) = 0
        ORDER BY s.ts_ms""")
    if live:
        l = cohort([{"pnl": r["pnl"], "won": r["won"]} for r in live])
        print(f"  LIVE settlements (forward test): {l}")
        per_t = sum(r["pnl"] for r in live)
        if len(live) >= 4:
            which = "losing" if per_t <= 0 else "winning"
            print(f"    -> tracking the {which} half so far "
                  f"(watch the daily loss cap)")
    else:
        print("  LIVE settlements: none yet")

    per_round = defaultdict(float)
    for t in trades:
        per_round[t["slug"]] += t["pnl"]
    rng = random.Random(42)
    keys = list(per_round)
    boot = sorted(sum(per_round[rng.choice(keys)] for _ in keys)
                  for _ in range(10_000))
    print(f"  bootstrap over rounds: median {st.median(boot):+.2f} "
          f"95% CI [{boot[250]:+.2f}, {boot[9749]:+.2f}] "
          f"P(profitable)={sum(v > 0 for v in boot) / len(boot):.0%}")

    pnls = sorted((t["pnl"] for t in trades), reverse=True)
    total = sum(pnls)
    if total > 0:
        print(f"  concentration: top-1 {pnls[0] / total:.0%}, top-2 "
              f"{sum(pnls[:2]) / total:.0%}, top-3 {sum(pnls[:3]) / total:.0%} "
              f"of profit; {sum(1 for p in pnls if p < 0)} losers")

    hours = defaultdict(PnLSummary)
    for t in trades:
        hours[time.gmtime(t["ts_ms"] / 1000).tm_hour].add(
            t["pnl"], 1.0, 0.0, t["won"])
    print("  by UTC hour: " + "  ".join(
        f"{h:02d}:{s.pnl:+.2f}({s.n_trades})" for h, s in sorted(hours.items())))

    cum, peak, mdd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda t: t["ts_ms"]):
        cum += t["pnl"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    print(f"  cumulative curve: end {cum:+.2f}, peak-to-trough {mdd:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default=MARKETS[0][0],
                    help="market prefix to replay (default: the traded one)")
    ap.add_argument("--symbol", default=None,
                    help="spot symbol whose stored bars supply volume/taker "
                         "data (default: the market's own symbol)")
    ap.add_argument("--model", default="digital",
                    help="model name or 'all'")
    ap.add_argument("--fee", type=float, default=None,
                    help="flat fee override; omit to use the measured fee curve")
    ap.add_argument("--sweep", action="store_true",
                    help="one-dimensional parameter sweeps around the defaults")
    ap.add_argument("--bias", action="store_true",
                    help="bias checks: split-half, round bootstrap, concentration")
    ap.add_argument("--fee-model", default="notional",
                    choices=("notional", "profit"),
                    help="fee semantics assumed by the gate (notional is stricter)")
    args = ap.parse_args()
    if args.symbol is None:
        args.symbol = next((sym for prefix, sym, _ in MARKETS
                            if prefix == args.market), "BTCUSDT")

    store = Store(path=settings.db_path)
    # One market only. The recorder covers every market in MARKETS, but the bot
    # trades just the primary one, so replaying them together measures a
    # strategy nobody runs -- and the untraded market's PnL moves every sweep
    # result it is mixed into.
    like = args.market + "%"
    settled = store.query(
        "SELECT slug, strike, end_price, winner, start_ms FROM rounds "
        "WHERE winner IS NOT NULL AND strike IS NOT NULL AND end_price IS NOT NULL "
        "AND slug LIKE ? ORDER BY start_ms", (like,))
    quotes = store.query("""
        SELECT q.* FROM quotes q
        JOIN rounds r ON r.slug = q.slug
        WHERE r.winner IS NOT NULL AND q.slug LIKE ?
        ORDER BY q.ts_ms""", (like,))

    settled_by_slug = {r["slug"]: r for r in settled}
    sigma_by_slug = {r["slug"]: rolling_sigma(settled, i)
                     for i, r in enumerate(settled)}
    hist = build_1m_history(quotes, store=store, symbol=args.symbol)
    base = ReplayParams.from_settings()
    if args.fee_model != "notional":
        base = replace(base, fee_model=args.fee_model)
    cost = (f"flat fee {args.fee:.2%}" if args.fee is not None
            else "measured fee curve")

    if args.sweep:
        print(f"{args.market}: {len(settled)} settled rounds, "
              f"{len(quotes)} quotes ({cost})\n")
        sweep(quotes, settled_by_slug, sigma_by_slug, hist,
              build_model("digital"), args.fee, base)
        return

    if args.bias:
        print(f"{args.market}: {len(settled)} settled rounds, "
              f"{len(quotes)} quotes ({cost})\n")
        bias_report(quotes, settled_by_slug, sigma_by_slug, hist,
                    build_model("digital"), args.fee, base)
        return

    print(f"{args.market}: {len(settled)} settled rounds, {len(quotes)} quotes, "
          f"{len(hist)} reconstructed 1m bars ({cost})\n")

    names = list(MODEL_NAMES) if args.model == "all" else [args.model]
    live_gate = settings.trade_on_candle_open
    up_name = settings.up_model.strip().lower()
    for name in names:
        model = build_model(name)
        model_up = (build_model(up_name)
                    if up_name and up_name != name.strip().lower() else None)
        print(f"{BAR}\nmodel: {name}"
              + (f" + up-model: {up_name}" if model_up else ""))
        current = replay(quotes, settled_by_slug, sigma_by_slug, hist, model,
                         args.fee, candle_gate=live_gate, params=base,
                         model_up=model_up)
        other = replay(quotes, settled_by_slug, sigma_by_slug, hist, model,
                       args.fee, candle_gate=not live_gate, params=base,
                       model_up=model_up)
        market_vs_model(quotes, settled_by_slug, sigma_by_slug, hist, model, base)
        print_trades("CURRENT LIVE CONFIG (" +
                     ("candle-open gate)" if live_gate else "ungated)"), current)
        print_trades("comparison (" +
                     ("ungated)" if live_gate else "candle-open gate)"), other)

    print(f"\n{BAR}\ncost-model comparison (current live config, digital):")
    model = build_model("digital")
    model_up = (build_model(up_name)
                if up_name and up_name != "digital" else None)
    for label, f in (("flat 0.64% (old assumption)", 0.0064),
                     ("measured fee curve", None)):
        r = replay(quotes, settled_by_slug, sigma_by_slug, hist, model,
                   f, candle_gate=live_gate, params=base, model_up=model_up)
        print(f"  {label}: {r['pnl']}")


if __name__ == "__main__":
    main()
