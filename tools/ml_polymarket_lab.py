"""ML lab on 471 resolved 5-minute crypto up/down markets (Polymarket L2).

The first experiment where a model can be judged against a venue that is
CALIBRATED, on far more than our own 145-round cell: 471 markets, full bid/ask
ladders at 1 s, and the winning outcome on every row.

The question is deliberately narrow, because it is the only one that matters:

    does the ORDER BOOK LADDER plus MOMENTUM predict the winner better than the
    market's own price does?

The market's price is the baseline, so `mid` is a feature the model is GIVEN.
A model that cannot beat the mid on held-out markets has found nothing, however
good its accuracy looks -- the mid is what we would have to pay to trade.

Falsifiable acceptance, fixed before the run:
  1. lower Brier than the mid on markets the model never saw (split by TIME, so
     a market's neighbours cannot leak), and
  2. a trading simulation that is positive AFTER the price paid, and
  3. big enough to survive our own venue's ~2% fee. Polymarket charges no
     trading fee, so an edge measured here is an UPPER BOUND for us: anything
     under ~2% per trade is eaten whole on the venue we actually trade.

Usage:  python -m tools.ml_polymarket_lab [--parquet data/hf/updown_5m.parquet]
"""
from __future__ import annotations

import argparse
import collections
import math
from pathlib import Path

import numpy as np
import pandas as pd


def build(parquet: str) -> pd.DataFrame:
    df = pd.read_parquet(parquet)
    up = df[df["outcome"] == "Up"].copy()
    up = up.sort_values(["market_slug", "captured_at"])
    up["mid"] = (up["best_bid"] + up["best_ask"]) / 2.0
    up["spread"] = up["best_ask"] - up["best_bid"]
    up["q"] = up["best_bid"]                        # the executable-sale price
    # the ladder, which is the thing we have never had
    up["bid_sz"] = up["bid_sizes"].apply(lambda a: float(a[0]) if len(a) else 0.0)
    up["ask_sz"] = up["ask_sizes"].apply(lambda a: float(a[0]) if len(a) else 0.0)
    up["nb"] = up["bid_sizes"].apply(lambda a: float(np.sum(a)))
    up["na"] = up["ask_sizes"].apply(lambda a: float(np.sum(a)))
    up["nb5"] = up.apply(lambda r: float(np.sum(
        np.array(r["bid_sizes"])[np.array(r["bid_prices"]) >= r["best_bid"] - 0.05])),
        axis=1)
    up["na5"] = up.apply(lambda r: float(np.sum(
        np.array(r["ask_sizes"])[np.array(r["ask_prices"]) <= r["best_ask"] + 0.05])),
        axis=1)
    up["imb"] = up["nb"] / (up["nb"] + up["na"]).replace(0, np.nan)
    up["imb5"] = up["nb5"] / (up["nb5"] + up["na5"]).replace(0, np.nan)
    up["nlv"] = up["bid_sizes"].apply(len) + up["ask_sizes"].apply(len)
    # momentum on both sides of the book, straight from the tape
    for k in (5, 15, 30, 60):
        for c, name in (("best_bid", "mb"), ("best_ask", "ma"), ("spread", "ms")):
            up[name + str(k)] = up.groupby("market_slug")[c].diff(k)
    up["logtau"] = np.log(up["seconds_to_close"].clip(lower=1))
    # Drop on EVERY feature, not a hand-picked subset: a 60-tick momentum column
    # is null for any market with fewer snapshots, which poisons the standardiser
    # and turns every weight into nan. The inf handling is done on the numeric
    # feature frame alone -- the ladder columns hold numpy ARRAYS, and a blanket
    # `replace` tries to compare them elementwise and raises.
    feat = up[FEATURES].astype(float).replace([np.inf, -np.inf], np.nan)
    up = up[feat.notna().all(axis=1)].copy()
    up[FEATURES] = feat[feat.notna().all(axis=1)]
    return up


FEATURES = ["mid", "spread", "q", "imb", "imb5", "bid_sz", "ask_sz", "nb", "na",
            "nb5", "na5", "nlv", "logtau", "mb5", "mb15", "mb30", "mb60",
            "ma5", "ma15", "ma30", "ma60", "ms5", "ms15", "ms30", "ms60"]


def fit_logit(X: np.ndarray, y: np.ndarray, iters: int = 600, lr: float = 0.5,
              l2: float = 1e-3) -> np.ndarray:
    """Plain logistic regression: no sklearn on this machine, and for 25
    standardised features on a few thousand rows it is not needed."""
    w = np.zeros(X.shape[1])
    n = len(X)
    for _ in range(iters):
        z = X @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        g = X.T @ (p - y) / n + l2 * w
        w -= lr * g
    return w


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


SEQ_FEATS = ["best_bid", "best_ask", "spread", "mid", "lbidsz", "lasksz",
             "lnb", "lna", "imb", "lnb5", "lna5", "dmid", "dspr",
             "tau_norm"]
SEQ_K = 60


def build_sequences(df: pd.DataFrame, k: int = SEQ_K):
    """The tape as a sequence: the last `k` seconds of the book before a cutoff.

    A logistic or a tree sees a SNAPSHOT. A sequence model sees the path -- the
    book getting thinner, the spread widening, the mid drifting -- and that is the
    only formulation where a GPU is doing something a logistic regression cannot:
    on ~200 training markets nothing else about a neural net can help.

    Returns X (markets, k, features), y (the Up token won) and the frame the rows
    came from, so the same prices and labels are used for every model compared.
    """
    d = df.copy()
    d["lbidsz"] = np.log1p(d["bid_sz"])
    d["lasksz"] = np.log1p(d["ask_sz"])
    d["lnb"] = np.log1p(d["nb"])
    d["lna"] = np.log1p(d["na"])
    d["lnb5"] = np.log1p(d["nb5"])
    d["lna5"] = np.log1p(d["na5"])
    d["dmid"] = d.groupby("market_slug")["mid"].diff().fillna(0.0)
    d["dspr"] = d.groupby("market_slug")["spread"].diff().fillna(0.0)
    d["tau_norm"] = d["seconds_to_close"] / 300.0
    d = d.replace([np.inf, -np.inf], np.nan)
    d = d.dropna(subset=SEQ_FEATS)
    X, y, idx = [], [], []
    for slug, g in d.groupby("market_slug"):
        g = g.sort_values("captured_at")
        if len(g) < k:
            continue
        tail = g.tail(k)
        X.append(tail[SEQ_FEATS].to_numpy(dtype=np.float32))
        y.append(float(tail["is_winning_outcome"].iloc[-1]))
        idx.append(tail.index[-1])
    return np.stack(X), np.array(y), d.loc[idx]


def train_seq(Xtr, ytr, Xte, epochs: int = 60):
    """A small GRU over the tape. Lazy torch import with an explicit device log,
    so it is always visible whether the GPU was actually used."""
    import torch
    import torch.nn as nn

    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"  torch {torch.__version__} | device={dev} | "
          f"{torch.cuda.get_device_name(0) if dev.type == 'cuda' else 'CPU only'}")

    class Net(nn.Module):
        def __init__(self, f):
            super().__init__()
            self.gru = nn.GRU(f, 64, batch_first=True)
            self.head = nn.Linear(64, 1)

        def forward(self, x):
            o, _ = self.gru(x)
            return self.head(o[:, -1]).squeeze(-1)

    mu, sd = Xtr.reshape(-1, Xtr.shape[-1]).mean(0), \
        Xtr.reshape(-1, Xtr.shape[-1]).std(0) + 1e-6

    def z(a):
        return torch.tensor((a - mu) / sd, dtype=torch.float32, device=dev)

    n = len(Xtr)
    cut = int(n * 0.85)
    xt, yt = z(Xtr[:cut]), torch.tensor(ytr[:cut], dtype=torch.float32, device=dev)
    xv, yv = z(Xtr[cut:]), torch.tensor(ytr[cut:], dtype=torch.float32, device=dev)
    net = Net(Xtr.shape[-1]).to(dev)
    opt = torch.optim.Adam(net.parameters(), lr=2e-3, weight_decay=1e-4)
    lossf = nn.BCEWithLogitsLoss()
    best, best_state, bad = 9.9, None, 0
    for ep in range(epochs):
        net.train()
        perm = torch.randperm(len(xt), device=dev)
        for i in range(0, len(xt), 64):
            b = perm[i:i + 64]
            opt.zero_grad()
            loss = lossf(net(xt[b]), yt[b])
            loss.backward()
            opt.step()
        net.eval()
        with torch.no_grad():
            pv = torch.sigmoid(net(xv)) if len(xv) else None
            v = float(((pv - yv) ** 2).mean()) if pv is not None else 0.0
        if v < best:
            best, best_state, bad = v, {k2: t.clone() for k2, t in
                                        net.state_dict().items()}, 0
        else:
            bad += 1
            if bad >= 12:
                break
    if best_state is not None:
        net.load_state_dict(best_state)
    net.eval()
    with torch.no_grad():
        ptr = torch.sigmoid(net(z(Xtr))).cpu().numpy()
        pte = torch.sigmoid(net(z(Xte))).cpu().numpy() if len(Xte) else np.array([])
    return ptr, pte, f"seq K={Xtr.shape[1]} h=64"


def train_xgb(Xtr: np.ndarray, ytr: np.ndarray, Xte: np.ndarray):
    """Gradient boosting, the natural stronger model on tabular features.
    It TRIES the GPU and says so plainly if the pip wheel cannot do it -- the
    CPU-only wheel ignores `device` and silently runs on the CPU otherwise."""
    from xgboost import XGBClassifier

    dev = "cpu"
    try:
        import xgboost as xgb
        if "cuda" in xgb.build_info().get("OPTIONS", ""):
            dev = "cuda"
    except Exception:
        pass
    print(f"  xgboost {xgb.__version__} | device={dev}" +
          ("" if dev == "cuda" else "  (pip wheel is CPU-only; GPU needs a "
           "CUDA build)"))
    m = XGBClassifier(n_estimators=400, max_depth=3, learning_rate=0.05,
                      subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
                      min_child_weight=5, device=dev, tree_method="hist",
                      eval_metric="logloss")
    m.fit(Xtr, ytr)
    return (m.predict_proba(Xtr)[:, 1], m.predict_proba(Xte)[:, 1], "xgb d=3")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", default="data/hf/updown_5m.parquet")
    ap.add_argument("--taus", type=float, nargs="+", default=[8, 30, 60, 120])
    ap.add_argument("--tol", type=float, default=1.5)
    ap.add_argument("--model", default="logit",
                    choices=["logit", "xgb", "seq"],
                    help="logit = the numpy baseline; xgb = gradient boosting; "
                         "seq = GRU over the tape (GPU if torch+cuda is present)")
    args = ap.parse_args()
    df = build(args.parquet)
    print(f"Up-token snapshots: {len(df):,} across "
          f"{df['market_slug'].nunique()} markets "
          f"({df['coin'].nunique()} coins, {df['coin'].unique()[:6]})\n")

    print(f"{'tau<=':>5} {'n':>5} {'real tau':>9} | "
          f"{'Brier mid':>10} {'Brier model':>12} {'Brier x-mid':>12} | "
          f"{'traded':>7} {'WR':>6} {'EV/$1':>8} {'t':>6}")
    for tau in args.taus:
        # The last snapshot at or before `tau`, one per market -- exactly what a
        # live arm does (act at the latest moment it can, not at a nominal tau).
        # Near the close the tape is sparse, so the realised tau is reported
        # rather than assumed.
        cand = df[df["seconds_to_close"] <= tau]
        idx = cand.groupby("market_slug")["captured_at"].idxmax()
        sub = cand.loc[idx].copy()
        if len(sub) < 80:
            print(f"{tau:>5} {len(sub):>5}  (too few markets)")
            continue
        y = sub["is_winning_outcome"].astype(float).to_numpy()
        # walk-forward: split by market CLOSE TIME, so no market is in both
        cut = sub["market_end_at"].quantile(0.7)
        tr, te = sub["market_end_at"] <= cut, sub["market_end_at"] > cut
        if tr.sum() < 50 or te.sum() < 20:
            print(f"{tau:>5} {len(sub):>5}  (split too small)")
            continue
        X = sub[FEATURES].to_numpy(dtype=float)
        assert np.isfinite(X).all(), "non-finite feature survived the dropna"
        if args.model == "seq":
            Xs3, ys3, sub3 = build_sequences(df[df["seconds_to_close"] <= tau])
            if len(sub3) < 80:
                print(f"{tau:>5} {len(sub3):>5}  (too few markets with {SEQ_K} "
                      f"prior snapshots)")
                continue
            sub, y = sub3, ys3
            cut3 = sub["market_end_at"].quantile(0.7)
            tr3 = (sub["market_end_at"] <= cut3).to_numpy()
            te3 = ~tr3
            p_tr, p_te, label = train_seq(Xs3[tr3], y[tr3], Xs3[te3])
            p = np.full(len(sub), np.nan)
            p[tr3], p[te3] = p_tr, p_te
            b_nomid = float("nan")
            X = np.zeros((len(sub), 1))
        else:
            y = sub["is_winning_outcome"].astype(float).to_numpy()
            cut = sub["market_end_at"].quantile(0.7)
            tr, te = sub["market_end_at"] <= cut, sub["market_end_at"] > cut
            if tr.sum() < 50 or te.sum() < 20:
                print(f"{tau:>5} {len(sub):>5}  (split too small)")
                continue
            X = sub[FEATURES].to_numpy(dtype=float)
            mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
            Xs = np.column_stack([np.ones(len(X)), (X - mu) / sd])
            if args.model == "xgb":
                p_tr, p_te, label = train_xgb(X[tr.to_numpy()], y[tr.to_numpy()],
                                              X[te.to_numpy()])
                p = np.full(len(sub), np.nan)
                p[tr.to_numpy()], p[te.to_numpy()] = p_tr, p_te
                b_nomid = float("nan")
            else:
                label = "logit"
                w = fit_logit(Xs[tr.to_numpy()], y[tr.to_numpy()])
                p = 1.0 / (1.0 + np.exp(-np.clip(Xs @ w, -30, 30)))
                i_nomid = [i + 1 for i, f in enumerate(FEATURES) if f != "mid"]
                w2 = fit_logit(Xs[tr.to_numpy()][:, [0] + i_nomid], y[tr.to_numpy()])
                p2 = 1.0 / (1.0 + np.exp(-np.clip(
                    Xs[:, [0] + i_nomid] @ w2, -30, 30)))
                b_nomid = brier(p2[te.to_numpy()], y[te.to_numpy()])
        te_a = (sub["market_end_at"] > sub["market_end_at"].quantile(0.7)).to_numpy()
        b_mid, b_mod = (brier(sub["mid"].to_numpy()[te_a], y[te_a]),
                        brier(p[te_a], y[te_a]))
        # trade the model's disagreement with the price, paying the ask
        ask = sub["best_ask"].to_numpy()
        bid = sub["best_bid"].to_numpy()
        stake = np.where(p > ask + 0.02, 1.0, 0.0)      # buy Up above the ask
        stake = np.where(p < bid - 0.02, -1.0, stake)   # buy Down below the bid
        vals = []
        for i in np.where((te_a) & (stake != 0))[0]:
            if stake[i] > 0:
                vals.append((1.0 / ask[i] - 1.0) if y[i] == 1 else -1.0)
            else:
                vals.append((1.0 / (1.0 - bid[i]) - 1.0) if y[i] == 0 else -1.0)
        ev = float(np.mean(vals)) if vals else float("nan")
        t = (ev / (np.std(vals) / math.sqrt(len(vals)))
             if len(vals) > 2 and np.std(vals) > 0 else 0.0)
        wr = (float(np.mean([v > 0 for v in vals])) if vals else float("nan"))
        print(f"{tau:>5} {len(sub):>5} {sub['seconds_to_close'].median():>8.0f}s | "
              f"{b_mid:>10.4f} {b_mod:>12.4f} {b_nomid:>12.4f} | "
              f"{len(vals):>7} {100 * wr:>5.1f}% {ev:>+8.4f} {t:>+6.2f}")
    print("\n  Brier x-mid = the model WITHOUT the market price (does the ladder")
    print("  alone predict?). The model only wins if Brier model < Brier mid on")
    print("  held-out markets AND the traded EV survives our venue's ~2% fee.")


if __name__ == "__main__":
    main()
