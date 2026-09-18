"""A/B the OLD ladder against the FIXED one on the SAME live websocket stream.

WHY THIS EXISTS. The fix to `PmBook._apply_delta` is provable from the code and
pinned by tests, but its SIZE on live data is the number that matters: how often
did `Touch.sweep()` quote a price the book no longer held, and by how much? The
port's own log says it reached 45 cents:

    cross bnb-updown-15m-... Down: $1.00 sweeps to 0.610 (touch 0.160, +0.450)

Waiting for the cross arm to fire only samples the moments the arm already
liked. This subscribes ONE socket, feeds the identical frames to two `PmBook`
instances -- one with the OLD `_apply_delta`, one with the new -- and reports
the divergence once a second, including the quiet stretches.

ONE SOCKET ON PURPOSE. An earlier shape gave each book its own socket, which
would have doubled the ~1,250 msg/s that OOM-killed this process on 2026-09-17
(see `book.py`'s own note). A mirror tap costs one extra parse per frame and
nothing on the queue, and it makes the A/B exact: both books see the same bytes
in the same order.

Run on the VPS (needs the live venue):
    PYTHONPATH=/root/BinancePrediction /root/analysis-venv/bin/python \
        tools/pm_ladder_divergence.py --seconds 240 --symbol btc --probe-every 20

The venue's OWN book is sampled alongside, because without it "they disagree"
is only half an answer -- the point is which instance the venue agrees with.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from btcpredict.venues.polymarket.book import PmBook, Touch  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / "btcpredict.db"


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


class LegacyBook(PmBook):
    """`PmBook` carrying the PRE-FIX `_apply_delta`.

    It updated the touch scalars and then carried the ladder forward verbatim,
    so the ladder was rebuilt only by a `book` snapshot. That is the whole bug,
    reproduced here against a live stream rather than a fixture.
    """

    def _apply_delta(self, ch: dict, now_ms: int, msg_ts: int = 0) -> None:
        aid = str(ch.get("asset_id") or "")
        if not aid:
            return
        prev = self.tops.get(aid)
        bb, ba = _f(ch.get("best_bid")), _f(ch.get("best_ask"))
        if bb <= 0 or ba <= 0:
            return
        px, sz, side = _f(ch.get("price")), _f(ch.get("size")), ch.get("side")
        bid_sz = prev.bid_size if prev else 0.0
        ask_sz = prev.ask_size if prev else 0.0
        if side == "BUY" and abs(px - bb) < 1e-9:
            bid_sz = sz
        elif side == "SELL" and abs(px - ba) < 1e-9:
            ask_sz = sz
        self.tops[aid] = Touch(
            asset_id=aid, bid=bb, bid_size=bid_sz, ask=ba, ask_size=ask_sz,
            ts_ms=int(_f(ch.get("timestamp"))), recv_ms=now_ms,
            bids=(prev.bids if prev else ()), asks=(prev.asks if prev else ()),
        )


class Mirrored(PmBook):
    """One socket, two books: every frame is applied to both."""

    def __init__(self, *others: PmBook) -> None:
        super().__init__()
        self._others = others

    def _ingest(self, raw: str) -> None:
        super()._ingest(raw)
        for o in self._others:
            o._ingest(raw)


def current_tokens(symbol: str) -> list[tuple[str, str, str]]:
    """(slug, token, side) for the rounds of `symbol` still open."""
    if not DB.exists():
        return []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT slug, up_token_id, down_token_id FROM rounds "
            "WHERE symbol = ? AND up_token_id IS NOT NULL "
            "AND down_token_id IS NOT NULL AND end_ms > ? "
            "ORDER BY start_ms DESC LIMIT 4",
            (symbol.upper(), int(time.time() * 1000))).fetchall()
    finally:
        con.close()
    out: list[tuple[str, str, str]] = []
    for slug, up, dn in rows:
        out.append((slug, str(up), "Up"))
        out.append((slug, str(dn), "Down"))
    return out


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(q * (len(xs) - 1))))
    return xs[i]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=240.0)
    ap.add_argument("--symbol", default="btc")
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--probe-every", type=int, default=0,
                    help="probe the VENUE's REST book every N samples (0=off)")
    args = ap.parse_args()

    fixed, legacy = PmBook(), LegacyBook()
    tap = Mirrored(fixed, legacy)
    stop = asyncio.Event()

    async def refresh() -> None:
        while not stop.is_set():
            toks = [t for _, t, _ in current_tokens(args.symbol)]
            if toks:
                tap.follow(toks)
            try:
                await asyncio.wait_for(stop.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass

    async def sample() -> None:
        import aiohttp

        gaps: list[float] = []
        vs_touch: list[float] = []
        per_slug: dict[str, list[float]] = {}
        rows = probes = fixed_right = legacy_right = 0
        t0 = time.monotonic()
        to = aiohttp.ClientTimeout(total=None, connect=3.0, sock_read=4.0)
        async with aiohttp.ClientSession(timeout=to) as s:
            while time.monotonic() - t0 < args.seconds and not stop.is_set():
                for slug, tok, side in current_tokens(args.symbol):
                    tn = fixed.best(tok, 60.0)
                    to_ = legacy.best(tok, 60.0)
                    if tn is None or to_ is None or tn.ask <= 0 or to_.ask <= 0:
                        continue
                    fx, _ = tn.sweep(args.stake)
                    lx, _ = to_.sweep(args.stake)
                    if fx <= 0 or lx <= 0:
                        continue
                    rows += 1
                    gaps.append(lx - fx)
                    vs_touch.append(abs(lx - tn.ask))
                    key = f"{slug.split('-')[0]}-{side}"
                    per_slug.setdefault(key, []).append(abs(lx - fx))
                    if args.probe_every and rows % args.probe_every == 0 \
                            and probes < 60:
                        vt = await PmBook.fetch_top(s, tok)
                        if vt is not None and vt.ask > 0:
                            probes += 1
                            dfx = abs(vt.ask - tn.ask)
                            dlx = abs(vt.ask - to_.ask)
                            if dfx < dlx:
                                fixed_right += 1
                            elif dlx < dfx:
                                legacy_right += 1
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        stop.set()
        n = max(rows, 1)
        big = [g for g in gaps if abs(g) > 0.01]
        print(f"\nsamples                            {rows}")
        print(f"legacy sweep != fixed sweep        {len(big)} "
              f"({100.0 * len(big) / n:.1f}%)")
        print(f"  |legacy-fixed| p50               "
              f"{_pct([abs(g) for g in gaps], 0.50):.4f}")
        print(f"  |legacy-fixed| p90               "
              f"{_pct([abs(g) for g in gaps], 0.90):.4f}")
        print(f"  |legacy-fixed| max               "
              f"{max((abs(g) for g in gaps), default=0.0):.4f}")
        print(f"  legacy sweep vs LIVE TOUCH p50    {_pct(vs_touch, 0.50):.4f}")
        print(f"  legacy sweep vs LIVE TOUCH p90    {_pct(vs_touch, 0.90):.4f}")
        if probes:
            print(f"venue REST probes: fixed closer   {fixed_right}/{probes}, "
                  f"legacy closer {legacy_right}/{probes}")
        print("\nReading: a legacy sweep ABOVE the live touch quotes a level that "
              "has\nalready been taken -- that is the order the venue kills.")
        worst = sorted(per_slug.items(), key=lambda kv: -_pct(kv[1], 0.90))[:4]
        for k, v in worst:
            print(f"  worst p90 {k:22s} {_pct(v, 0.90):.4f}  (n={len(v)})")
        return 0

    tasks = [asyncio.create_task(tap.run()),
             asyncio.create_task(refresh())]
    try:
        return await sample()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
