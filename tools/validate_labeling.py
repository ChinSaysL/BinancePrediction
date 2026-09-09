"""Can Up/Down be inferred from on-chain fills alone?

On-chain we see tokenIds, not outcome names. For rounds older than the API
serves, labels must be inferred. The method: near expiry the winning outcome
trades toward 1.0, and the winner is known independently from klines, so the
token trading above 0.5 in a late window is the winner's token.

This validates that inference against rounds whose true labels came from the
API. If it is not near-perfect, the historical reconstruction is not trustworthy
and must not be used.
"""
from __future__ import annotations

import collections
import logging

from btcpredict.config import settings
from btcpredict.feeds.onchain import BscClient
from btcpredict.store.db import Store

log = logging.getLogger("validate")


def main(n_rounds: int = 10) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    store = Store(settings.db_path)
    c = BscClient(settings.rpcio_api_key)
    print(f"RPC budget remaining: {c.remaining}")

    head = c.public_head()
    _, head_ts, spb = c.calibrate(head - 200, back=40_000)

    rounds = store.query(
        "SELECT slug, start_ms, end_ms, winner, up_token_id, down_token_id "
        "FROM rounds WHERE winner IS NOT NULL AND up_token_id IS NOT NULL "
        "ORDER BY start_ms DESC")
    print(f"{len(rounds)} rounds with known labels; testing {n_rounds}\n")

    ok = bad = nodata = 0
    for r in rounds[:n_rounds]:
        end_s = r["end_ms"] // 1000
        # window ending ~10s before settlement
        blk = c.block_for(end_s - 10, head - 200, head_ts, spb)
        try:
            fills = c.fills(blk - 45, blk)
        except Exception as e:
            print(f"  {r['slug'][-10:]}: rpc {type(e).__name__} {e}")
            nodata += 1
            continue

        want = {r["up_token_id"]: "Up", r["down_token_id"]: "Down"}
        mine = [f for f in fills if f.token_id in want]
        if not mine:
            print(f"  {r['slug'][-10:]}: no fills in window ({len(fills)} other-market)")
            nodata += 1
            continue

        px = collections.defaultdict(list)
        for f in mine:
            px[f.token_id].append(f.price)
        avg = {t: sum(v) / len(v) for t, v in px.items()}
        top = max(avg, key=avg.get)
        inferred = "Up" if want[top] == "Up" else "Down"
        # the token trading highest late should be the winner
        good = (inferred == r["winner"])
        ok, bad = ok + good, bad + (not good)
        marks = "  ".join(f"{want[t]}={avg[t]:.3f}" for t in sorted(avg, key=want.get))
        print(f"  {r['slug'][-10:]}: {marks}  -> inferred {inferred}, "
              f"actual {r['winner']}  {'OK' if good else 'WRONG'}")

    total = ok + bad
    print(f"\nlabeling accuracy: {ok}/{total}" + (f" ({ok/total:.0%})" if total else ""))
    print(f"no-data rounds: {nodata}")
    print(f"RPC calls used: {c.calls}; remaining today: {c.remaining}")
    if total and ok == total:
        print("=> inference is reliable; historical reconstruction can proceed")
    else:
        print("=> NOT reliable enough; do not trust reconstructed historical labels")


if __name__ == "__main__":
    main()
