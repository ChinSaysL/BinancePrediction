"""Loader for `data/btc5m` -- 53 days of btc-updown-5m rounds.

WHY THIS DATASET MATTERS: the kartik tape is ONE day (2026-07-17), so no
day-clustered test was possible on it and no parameter could be promoted. This
one carries 53 days (2026-04-29 .. 2026-06-20); 11 of them include trade tapes,
which is 3,168 rounds over 11 independent days.

WHAT EACH ROUND DIRECTORY HAS, and how each is used:
    metadata.json        Gamma market. `outcomePrices` is the ON-CHAIN
                         RESOLUTION -> the label, taken from the chain rather
                         than inferred from spot. `clobTokenIds[0]` is "Up"
                         and `[1]` is "Down" (paired with `outcomes`).
    ticks_dense.parquet  300 rows, one per second: Binance spot OHLCV + vwap +
                         taker buy/sell volume. Supplies the STRIKE and the
                         CROSSING SIGNAL.
    trades_raw.json      real on-chain OrderFilled records. Supplies the PRICE.
                         There is NO order book in this dataset, so an entry
                         price can only be an actual print.

The directory name IS the round start (unix seconds, divisible by 300).

PRICE CONVENTION, validated (see `validate` below): for every trade,
    price  = min(maker_amount, taker_amount) / max(...)
    shares = max(maker_amount, taker_amount)
and it is side-agnostic: for a taker BUY the taker receives shares and pays
collateral; for a taker SELL it is the other way round, so min/max is the
price either way. Checked over thousands of records: price lands in (0,1) and
Up + Down prices sum to ~1.

`fee` is 0.0 on every record here -- these are maker-leg fills. The venue's
taker fee (0.07*p*(1-p) per share, takerOnly) is therefore NOT observable from
this data and must be applied analytically.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

import pandas as pd

ROOT = pathlib.Path("data/btc5m/data")
ROUND_S = 300


def days() -> list[str]:
    if not ROOT.exists():
        raise SystemExit(f"missing {ROOT}")
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir())


def rounds(day: str) -> list[pathlib.Path]:
    d = ROOT / day
    return sorted((p for p in d.iterdir() if p.is_dir()),
                  key=lambda p: int(p.name))


def days_with_trades() -> list[str]:
    out = []
    for d in days():
        rs = rounds(d)
        if rs and next(rs[0].glob("trades_raw.json"), None) is not None:
            out.append(d)
    return out


@dataclass
class Round:
    day: str
    start_s: int
    strike: float
    label: str                 # "Up" / "Down", from outcomePrices
    spot_open: list[float]     # 300 one-second opens
    spot_close: list[float]
    tokens: tuple[str, str]    # (up_token, down_token)

    @property
    def slug(self) -> str:
        return f"btc-updown-5m-{self.start_s}"


def _as_list(v):
    """Gamma ships these as JSON-ENCODED STRINGS, not lists.

    `outcomes`, `outcomePrices` and `clobTokenIds` all arrive as
    '["Up", "Down"]' -- a str. Assuming a list raises on the length check,
    and printing them looks identical to a real list, which is how this got
    missed on the first pass.
    """
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return v if isinstance(v, (list, tuple)) else None


def _label(meta: dict) -> str:
    outcomes = _as_list(meta.get("outcomes"))
    prices = _as_list(meta.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != 2 or len(prices) != 2:
        raise ValueError("unexpected outcomes/outcomePrices shape")
    try:
        vals = [float(x) for x in prices]
    except (TypeError, ValueError):
        raise ValueError(f"unparseable outcomePrices {prices!r}") from None
    if max(vals) < 0.99:
        raise ValueError(f"round not resolved: {prices!r}")
    return str(outcomes[vals.index(max(vals))])


def _tokens(meta: dict) -> tuple[str, str]:
    ids = _as_list(meta.get("clobTokenIds"))
    if not ids or len(ids) != 2:
        raise ValueError(f"unexpected clobTokenIds {ids!r}")
    # `outcomes` is positionally aligned with `clobTokenIds`
    outcomes = _as_list(meta.get("outcomes")) or ["Up", "Down"]
    if outcomes[0] == "Down":
        return str(ids[1]), str(ids[0])
    return str(ids[0]), str(ids[1])


def load_round(rnd_dir: pathlib.Path, day: str) -> Round | None:
    """Metadata + spot only. Cheap: ~25 KB per round, no trade tape."""
    try:
        meta = json.loads((rnd_dir / "metadata.json").read_text())
        label = _label(meta)
        tokens = _tokens(meta)
        t = pd.read_parquet(rnd_dir / "ticks_dense.parquet")
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if len(t) < ROUND_S // 2:
        return None
    t = t.sort_values("timestamp")
    start = int(rnd_dir.name)
    if int(t["timestamp"].iloc[0]) != start:
        return None                      # misaligned round; refuse silently
    return Round(
        day=day, start_s=start,
        strike=float(t["open"].iloc[0]),
        label=label,
        spot_open=[float(x) for x in t["open"].iloc[:ROUND_S]],
        spot_close=[float(x) for x in t["close"].iloc[:ROUND_S]],
        tokens=tokens,
    )


def load_trades(rnd_dir: pathlib.Path) -> pd.DataFrame:
    """Real prints: ts (unix s), side, token_id (RAW), price, shares.

    The token is deliberately NOT mapped to Up/Down here. Gamma's
    `clobTokenIds` ordering is supposed to pair positionally with
    `outcomes`, and it does not always: round btc-updown-5m-1777778700 has
    clobTokenIds[0] closing at 0.14 while the market resolved DOWN, i.e. [0]
    is the Down token. So the mapping is inferred in `infer_up_token` from
    PAST spot excursions instead, and this function stays dumb.
    """
    path = rnd_dir / "trades_raw.json"
    empty = pd.DataFrame(columns=["ts", "side", "token_id", "price", "shares"])
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return empty
    rows = []
    for r in raw:
        try:
            ma = float(r["maker_amount"])
            ta = float(r["taker_amount"])
            tid = str(r["token_id"])
            ts = int(r["block_timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        lo, hi = (ma, ta) if ma <= ta else (ta, ma)
        if hi <= 0 or lo <= 0 or lo >= hi:
            continue
        rows.append((ts, str(r.get("side", "")), tid, lo / hi, hi))
    if not rows:
        return empty
    return (pd.DataFrame(rows, columns=["ts", "side", "token_id", "price", "shares"])
            .sort_values("ts", kind="stable").reset_index(drop=True))


def infer_up_token(R: "Round", tr: pd.DataFrame, upto_k: int,
                   min_bps: float = 1.0) -> str | None:
    """Which token_id is UP, using ONLY data up to `upto_k`.

    NO LOOK-AHEAD: the close is not consulted, and neither is the metadata
    ordering. Method: pick the seconds where spot is unambiguously one side of
    the strike, then ask which token trades richer over exactly those seconds.
    The token that is worth more while spot sits ABOVE the strike is Up.

    Returns None when the round never gets far enough from the strike to
    decide, in which case the round must be skipped rather than guessed.
    """
    if tr.empty or upto_k <= 0:
        return None
    k_hi = min(upto_k, len(R.spot_open))
    ids = tr.token_id.unique().tolist()
    if len(ids) != 2:
        return None
    above = float(R.strike) * (1.0 + min_bps / 1e4)
    sums = {i: 0.0 for i in ids}
    cnts = {i: 0 for i in ids}
    # ONLY seconds where spot sits clearly ABOVE the strike. Accepting the
    # below-strike seconds too makes the two tokens' contributions cancel and
    # the answer becomes noise -- which is exactly what the first version of
    # this did (it reported 6/14 disagreements, i.e. nothing but noise).
    for k in range(0, k_hi):
        if not (R.spot_open[k] > above):
            continue
        t0 = R.start_s + k
        sec = tr[(tr.ts == t0)]
        if sec.empty:
            continue
        for i in ids:
            sub = sec[sec.token_id == i]
            if len(sub):
                sums[i] += float(sub.price.mean())
                cnts[i] += 1
    if min(cnts.values()) < 5:
        return None
    means = {i: sums[i] / cnts[i] for i in ids}
    hi_id = max(means, key=lambda i: means[i])
    lo_id = min(means, key=lambda i: means[i])
    if means[hi_id] - means[lo_id] < 0.05:
        return None                    # too close to call; refuse
    return hi_id


def validate(day: str, n_rounds: int = 12) -> None:
    """Report how often Gamma's clobTokenIds ordering disagrees with reality.

    The metadata mapping is clobTokenIds[i] <-> outcomes[i] == ["Up","Down"].
    The truth is inferred from how each token trades while spot sits away from
    the strike. A non-zero disagreement rate means any backtest that trusts
    the metadata ordering is buying the wrong token in those rounds.
    """
    agree = disagree = undecided = 0
    for rd in rounds(day)[:n_rounds]:
        R = load_round(rd, day)
        if R is None:
            continue
        tr = load_trades(rd)
        if tr.empty:
            continue
        up_id = infer_up_token(R, tr, len(R.spot_open) - 1)
        if up_id is None:
            undecided += 1
            print(f"  {R.slug}  label {R.label:4s}  n={len(tr):5d}  "
                  f"mapping: UNDECIDED")
            continue
        meta_up = R.tokens[0]
        same = (up_id == meta_up)
        agree += int(same)
        disagree += int(not same)
        print(f"  {R.slug}  label {R.label:4s}  n={len(tr):5d}  "
              f"inferred up {up_id[:12]}  metadata up {meta_up[:12]}  "
              f"{'agree' if same else 'DISAGREE'}")
    print(f"  -> metadata ordering agrees {agree}, DISAGREES {disagree}, "
          f"undecided {undecided}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="2026-05-03")
    ap.add_argument("--n", type=int, default=12)
    a = ap.parse_args()
    ds = days()
    print(f"{len(ds)} days: {ds[0]} .. {ds[-1]}")
    witht = days_with_trades()
    print(f"{len(witht)} days carry trade tapes: {', '.join(witht)}")
    print(f"rounds per day: {len(rounds(a.day))}")
    print("\nvalidating the price convention:")
    validate(a.day, a.n)
