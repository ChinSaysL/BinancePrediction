"""Reconstruct historical market prices from on-chain fills.

Rounds older than ~3h are not served by the API, so tokenIds are unknown and
Up/Down must be inferred. The inference is guarded rather than trusted:

  * Three windows are sampled per round (early / mid / late) inside the round's
    own 5-minute boundary.
  * The round's token pair must be two tokens whose prices sum to ~1.0. Anything
    else means we latched onto a different market (15m, daily, sports) that
    happens to trade in the same blocks.
  * The pair must be present in more than one window, so a token that merely
    trades once in the block range cannot be mistaken for this round's.
  * Near expiry one token must be at an extreme (>=0.85). The winner is known
    independently from Binance klines, so that token is labelled with it.

Rounds failing any check are dropped, and the pass rate is reported. A low pass
rate means the reconstruction is unreliable and must not be used.

Only blocks older than MIN_DEPTH are queried: the provider load-balances across
nodes and recent blocks intermittently 404 as "invalid block range params".
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import math
import statistics as st
import time
import urllib.error
import urllib.request
from pathlib import Path

from btcpredict.config import settings
from btcpredict.feeds.onchain import BscClient, RpcError

log = logging.getLogger("onchain")

ROUND_S = 300
MIN_DEPTH = 55_000        # ~6.9h; newer blocks are unreliable on this provider
PAIR_SUM_TOL = 0.06
OUT = Path("data/onchain_rounds.json")


def klines(interval: str, start_ms: int, end_ms: int) -> list[list]:
    url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT"
           f"&interval={interval}&startTime={start_ms}&endTime={end_ms}&limit=1000")
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def pick_pair(windows: list[list]) -> tuple[str, str] | None:
    """Find two tokens that look like one binary market's Up/Down pair."""
    seen = collections.defaultdict(list)          # token -> [(win_idx, price)]
    for i, fills in enumerate(windows):
        agg = collections.defaultdict(list)
        for f in fills:
            agg[f.token_id].append(f.price)
        for t, ps in agg.items():
            seen[t].append((i, st.mean(ps)))

    # candidates must appear in >1 window
    cands = {t: v for t, v in seen.items() if len({i for i, _ in v}) > 1}
    best, best_err = None, 9.9
    toks = list(cands)
    for a in range(len(toks)):
        for b in range(a + 1, len(toks)):
            ta, tb = toks[a], toks[b]
            pa, pb = dict(cands[ta]), dict(cands[tb])
            shared = set(pa) & set(pb)
            if not shared:
                continue
            err = st.mean(abs(pa[i] + pb[i] - 1.0) for i in shared)
            if err < best_err:
                best, best_err = (ta, tb), err
    return best if best and best_err <= PAIR_SUM_TOL else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=100)
    ap.add_argument("--budget", type=int, default=700, help="max RPC calls to spend")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    c = BscClient(settings.rpcio_api_key, min_interval_s=0.6)
    log.info("RPC budget remaining today: %d", c.remaining)
    head = c.public_head()
    _, head_ts, spb = c.calibrate(head - 200, back=40_000)

    newest_ts = head_ts - int(MIN_DEPTH * spb)
    newest_round = (newest_ts // ROUND_S) * ROUND_S - ROUND_S
    starts = [newest_round - i * ROUND_S for i in range(args.rounds)]

    kl = klines("5m", (min(starts) - 600) * 1000, (max(starts) + 900) * 1000)
    bars = {int(k[0]) // 1000: (float(k[1]), float(k[4])) for k in kl}
    log.info("targeting %d rounds from %s back to %s", len(starts),
             time.strftime("%H:%M", time.gmtime(max(starts))),
             time.strftime("%H:%M", time.gmtime(min(starts))))

    out, stats = [], collections.Counter()
    start_calls = c.calls
    for s0 in starts:
        if c.calls - start_calls >= args.budget or c.remaining < 10:
            log.warning("budget reached; stopping"); break
        bar = bars.get(s0)
        if not bar:
            stats["no_kline"] += 1; continue
        K, C = bar
        winner = "Up" if C > K else "Down"

        wins = []
        for off in (60, 180, 285):          # tau ~240s, ~120s, ~15s
            blk = c.block_for(s0 + off, head - 200, head_ts, spb)
            try:
                wins.append(c.fills(blk - 45, blk))
            except Exception as e:
                log.debug("window fetch failed: %s: %s", type(e).__name__, e)
                wins.append([])
        if sum(len(w) for w in wins) == 0:
            stats["no_fills"] += 1; continue

        pair = pick_pair(wins)
        if not pair:
            stats["no_clean_pair"] += 1; continue

        late = collections.defaultdict(list)
        for f in wins[2]:
            if f.token_id in pair:
                late[f.token_id].append(f.price)
        if len(late) < 1:
            stats["no_late_fills"] += 1; continue
        late_avg = {t: st.mean(v) for t, v in late.items()}
        hi = max(late_avg, key=late_avg.get)
        if late_avg[hi] < 0.85:
            stats["late_not_extreme"] += 1; continue

        # hi is the winner's token; label the pair from the kline-derived winner
        up_tok = hi if winner == "Up" else (pair[0] if pair[1] == hi else pair[1])

        mid = [f for f in wins[1] if f.token_id in pair]
        if not mid:
            stats["no_mid_fills"] += 1; continue
        up_px = [f.price if f.token_id == up_tok else 1.0 - f.price for f in mid]

        out.append({"start_s": s0, "strike": K, "settle": C, "winner": winner,
                    "up_price_mid": st.mean(up_px), "n_mid_fills": len(mid),
                    "tau_mid": ROUND_S - 180})
        stats["ok"] += 1
        if stats["ok"] % 10 == 0:
            log.info("%d rounds reconstructed | %d calls used | %d left",
                     stats["ok"], c.calls - start_calls, c.remaining)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out))
    total = sum(stats.values())
    log.info("DONE: %d/%d rounds usable (%.0f%%)", stats["ok"], total,
             100 * stats["ok"] / total if total else 0)
    for k, v in stats.most_common():
        log.info("   %-18s %d", k, v)
    log.info("RPC calls this run: %d | remaining today: %d",
             c.calls - start_calls, c.remaining)
    log.info("saved -> %s", OUT)


if __name__ == "__main__":
    main()
