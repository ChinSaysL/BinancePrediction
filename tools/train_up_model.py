"""Fit and honestly score the Up-side recalibration (models/calibrated.py).

What is being fitted
--------------------
A logistic recalibration of P(Up) that is anchored on the venue's own ask:

    logit(p) = b0 + b_m*logit(q) + b_d*d + b_z*z + b_imb*imb + b_tau*sqrt(tau/60)

`d` is the disagreement (logit(p_digital) - logit(q)) -- the thing the gate has
been spending as edge. b_d is therefore the direct answer to "how much of our
Up edge is real": b_d ~ 1 means all of it, b_d ~ 0 means the venue was right and
we add nothing.

Method
------
  * the sample is ONE tick per round -- the first tick the gate evaluates -- so
    the rows are independent and are the population the decision is made on.
    Ticks inside a round are the same question asked repeatedly and would
    otherwise inflate every standard error by ~15x;
  * the split is by round and by TIME: the model is fitted on the first 60% of
    rounds and scored on the last 40%, which it has never seen. Nine candidate
    logics judged on one slice is a nine-way selection; this is the slice that
    decides;
  * it is scored as a PROBABILITY (Brier, against the venue's own ask as the
    benchmark) and as MONEY (the trades the gate takes when Up is priced by it,
    on the unseen slice only). A model can win the Brier and still lose money;
  * IRLS is written out in plain Python (6 parameters, ~650 rows) so the tool
    has no dependency beyond the standard library.

Usage:
    python -m tools.train_up_model              # fit, score, and report
    python -m tools.train_up_model --l2 1.0     # stronger shrinkage
    python -m tools.train_up_model --no-replay  # fit and score only
"""
from __future__ import annotations

import argparse
import math
import statistics as st

from btcpredict.config import settings
from btcpredict.models import build_model
from btcpredict.models.base import clamp_p
from btcpredict.models.calibrated import features_from_scalars
from btcpredict.pricing.digital import fair_value
from tools.backtest_candle_open import ReplayParams, replay
from tools.sizing_study import load_inputs

COLS = ("market", "disc", "imb")


def _logit(p: float) -> float:
    p = max(1e-6, min(1.0 - 1e-6, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small, dense, symmetric)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            continue
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r == c:
                continue
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit_irls(rows: list[tuple[list[float], int]], l2: float = 0.0,
             iters: int = 40) -> tuple[list[float], list[float]]:
    """Newton-Raphson fit. Returns (coefficients, standard errors)."""
    k = len(rows[0][0])
    beta = [0.0] * k
    for _ in range(iters):
        a = [[0.0] * k for _ in range(k)]
        g = [0.0] * k
        for x, y in rows:
            p = _sigmoid(sum(b * v for b, v in zip(beta, x)))
            w = max(1e-9, p * (1.0 - p))
            r = y - p
            for i in range(k):
                g[i] += r * x[i]
                for j in range(k):
                    a[i][j] += w * x[i] * x[j]
        for i in range(k):
            a[i][i] += l2
        step = solve(a, g)
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    # Standard errors from the inverse information matrix, via the same solver:
    #  Var = (X'WX)^-1, obtained column by column.
    a = [[0.0] * k for _ in range(k)]
    for x, _y in rows:
        p = _sigmoid(sum(b * v for b, v in zip(beta, x)))
        w = max(1e-9, p * (1.0 - p))
        for i in range(k):
            for j in range(k):
                a[i][j] += w * x[i] * x[j]
    for i in range(k):
        a[i][i] += l2
    se = []
    for i in range(k):
        e = [1.0 if j == i else 0.0 for j in range(k)]
        se.append(math.sqrt(max(0.0, solve(a, e)[i])))
    return beta, se


def brier(pairs: list[tuple[float, int]]) -> float:
    return st.mean([(p - y) ** 2 for p, y in pairs]) if pairs else float("nan")


def logloss(pairs: list[tuple[float, int]]) -> float:
    return st.mean([-(y * math.log(max(1e-9, p))
                      + (1 - y) * math.log(max(1e-9, 1 - p)))
                    for p, y in pairs]) if pairs else float("nan")


def build_sample(ticks: list[dict], sbs: dict, cut: int):
    """One row per round: the first tick the gate evaluates, with its outcome."""
    seen: set[str] = set()
    train, test, skipped = [], [], 0
    for t in ticks:
        slug = t["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        f = features_from_scalars(
            price=t["spot"], strike=t["strike"], tau_s=t["tau"],
            sigma_1m=t["sigma"], quote_up=t["up_ask"], imb=t["imb"])
        if f is None:
            skipped += 1
            continue
        # The feature builder must reproduce the digital probability the gate
        # actually saw, or the fit is not of the live model.
        dig = clamp_p(fair_value(t["spot"], t["strike"], t["tau"],
                                 t["sigma"]))
        if abs(_logit(dig) - (_logit(t["up_ask"]) + f["disc"])) > 1e-6:
            raise SystemExit("feature/digital mismatch: refusing to fit")
        x = [f[c] for c in COLS]
        y = 1 if t["outcome_up"] else 0
        (train if sbs[slug]["start_ms"] < cut else test).append((x, y, f, t))
    return train, test, skipped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="btc-updown-5m")
    ap.add_argument("--train-frac", type=float, default=0.60)
    ap.add_argument("--l2", type=float, default=0.0,
                    help="ridge on the information matrix (0 = plain MLE)")
    ap.add_argument("--no-replay", action="store_true")
    args = ap.parse_args()

    params = ReplayParams.from_settings()
    quotes, sbs, sigs, hist = load_inputs(args.market)
    ticks: list[dict] = []
    base = replay(quotes, sbs, sigs, hist, build_model("digital"), None, False,
                  params=params, model_up=build_model(settings.up_model),
                  collected=ticks)

    starts = sorted({r["start_ms"] for r in sbs.values()})
    cut = starts[int(len(starts) * args.train_frac)]
    train, test, skipped = build_sample(ticks, sbs, cut)
    print(f"{args.market}: {len(sbs)} settled rounds, {len(ticks)} evaluated "
          f"ticks, {len(train)} train rows / {len(test)} test rows "
          f"({skipped} rounds unusable)")
    print(f"split at round {int(len(starts) * args.train_frac)} of {len(starts)} "
          f"(ms {cut})\n")

    beta, se = fit_irls([(x, y) for x, y, _f, _t in train], l2=args.l2)

    # Design check first: collinear columns produce wild coefficients with
    # enormous standard errors, which is how a fit that describes nothing gets
    # pasted into production. The first run of this tool shipped b_market
    # se=3.54 because logit(p_digital), z and the disagreement were the same
    # column three ways.
    print("=== design check (pairwise correlation of the fitted columns) ===")
    vecs = [[f[c] for _x, _y, f, _t in train] for c in COLS]
    worst = 0.0
    for i, ci in enumerate(COLS):
        for j, cj in enumerate(COLS):
            if j <= i:
                continue
            r = st.correlation(vecs[i], vecs[j])
            worst = max(worst, abs(r))
            flag = "  <-- COLLINEAR" if abs(r) > 0.9 else ""
            print(f"  {ci:<8} vs {cj:<8} r = {r:+.3f}{flag}")
    if worst > 0.95:
        print("  REFUSING to report coefficients: the design is not identified.")
        return
    print()
    print("=== fitted coefficients (logit space) ===")
    print(f"  {'term':<10}{'coef':>10}{'se':>9}{'z':>8}")
    names = tuple(f"b_{c}" for c in COLS)
    for nm, b, s in zip(names, beta, se):
        print(f"  {nm:<10}{b:>+10.4f}{s:>9.4f}{b / s if s else 0:>+8.2f}")
    print(f"\n  b_disc is the share of our disagreement with the venue that "
          f"survives measurement.")
    print(f"  b_market ~1.0 means the venue's level is right and we only correct "
          f"it.")

    print("\n=== probability quality, TRAIN / TEST (one tick per round) ===")
    print(f"  {'model':<14}{'brier tr':>10}{'brier te':>10}"
          f"{'logloss te':>12}{'mean|p-q| te':>14}")
    for label, fn in (("digital", lambda t: clamp_p(fair_value(
                          t["spot"], t["strike"], t["tau"], t["sigma"]))),
                      ("venue ask", lambda t: t["up_ask"]),
                      ("calibrated", None)):
        tr_p, te_p = [], []
        for x, y, _f, t in train:
            p = (_sigmoid(sum(b * v for b, v in zip(beta, x)))
                 if fn is None else fn(t))
            tr_p.append((p, y))
        for x, y, _f, t in test:
            p = (_sigmoid(sum(b * v for b, v in zip(beta, x)))
                 if fn is None else fn(t))
            te_p.append((p, y))
        gap = st.mean([abs(p - t["up_ask"]) for (_x, _y, _f, t), (p, _y2)
                       in zip(test, te_p)]) if te_p else float("nan")
        print(f"  {label:<14}{brier(tr_p):>10.4f}{brier(te_p):>10.4f}"
              f"{logloss(te_p):>12.4f}{gap:>14.4f}")

    # Skill against the venue, paired within round so round-level variance does
    # not swamp a small difference.
    diffs = []
    for x, y, _f, t in test:
        p = _sigmoid(sum(b * v for b, v in zip(beta, x)))
        diffs.append((p - y) ** 2 - (t["up_ask"] - y) ** 2)
    if len(diffs) > 2:
        mu = st.mean(diffs)
        sd = st.pstdev(diffs)
        se_d = sd / math.sqrt(len(diffs))
        print(f"\n  paired Brier difference (calibrated - venue) on TEST: "
              f"{mu:+.5f} +- {se_d:.5f}  t={mu / se_d if se_d else 0:+.2f}"
              f"  n={len(diffs)}")
        print("  negative = the recalibration beats the venue's own price")

    fitted = {f"b_{c}": b for c, b in zip(COLS, beta)}
    print("\n=== paste into btcpredict/models/calibrated.py ===")
    print("DEFAULT_PARAMS: dict = {")
    for k in tuple(f"b_{c}" for c in COLS):
        print(f'    "{k}": {fitted[k]:.6f},')
    print("}")
    if args.no_replay:
        return

    print("\n=== money: the gate with Up priced by the recalibration ===")
    print(f"  {'':<26}{'n':>6}{'mean q':>8}{'WR%':>9}{'P&L':>10}"
          f"{'EV/$1':>9}{'se':>8}{'t':>7}")

    def line(label, trades):
        if not trades:
            print(f"  {label:<26}{0:>6}")
            return
        rs = [t["pnl"] for t in trades]
        ev = st.mean(rs)
        sem = st.pstdev(rs) / math.sqrt(len(rs))
        print(f"  {label:<26}{len(trades):>6}"
              f"{st.mean([t['q'] for t in trades]):>8.3f}"
              f"{100.0 * sum(1 for t in trades if t['won']) / len(trades):>8.1f}%"
              f"{sum(rs):>+10.2f}{ev:>+9.3f}{sem:>8.3f}"
              f"{ev / sem if sem else 0:>+7.2f}")

    for label, name, par in (("live (digital for Up)", settings.up_model, None),
                             ("recalibrated for Up", "calibrated", fitted)):
        res = replay(quotes, sbs, sigs, hist, build_model("digital"), None,
                     False, params=params, model_up=build_model(name, par))
        tr = [t for t in res["trades"] if sbs[t["slug"]]["start_ms"] < cut]
        te = [t for t in res["trades"] if sbs[t["slug"]]["start_ms"] >= cut]
        for who, sel in (("Up  train", [t for t in tr if t["side"] == "Up"]),
                         ("Up  test", [t for t in te if t["side"] == "Up"]),
                         ("all train", tr), ("all test", te)):
            line(f"{label[:14]:<14} {who}", sel)
    print(f"\n  (baseline gate without the recalibration: "
          f"{len(base['trades'])} trades)")


if __name__ == "__main__":
    main()
