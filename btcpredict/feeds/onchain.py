"""Read prediction-market trades directly from BNB Smart Chain.

The venue publishes no historical quote feed, but every fill settles on-chain as
an `OrderFilled` event on the Predict.fun CTF exchange. Those events are the only
unbiased source of historical market prices: they include every trader, not just
this account (which `order/history` is limited to, and which is selection-biased
toward moments someone chose to act).

Event layout (topic0 0xd0a08e8c...), verified by decoding live logs:
    topics: [sig, orderHash, maker, taker]
    data  : [makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, fee]
`assetId == 0` is USDT collateral; the other is the outcome token. Price is
usdt_amount / share_amount, and Up + Down fills in a round sum to ~1.0.

Access notes:
  * Public BSC endpoints refuse eth_getLogs outright ("limit exceeded").
  * 1rpc.io with a key allows it but caps each call at **50 blocks**, so the
    call budget, not the range, is the scarce resource.
  * 1rpc load-balances across nodes with differing heads (one was ~8h behind),
    so never trust its head for recent data -- pin explicit ranges.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

EXCHANGE = "0x8BC070BEdAB741406F4B1Eb65A72bee27894B689"
ORDER_FILLED = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
MAX_SPAN = 50            # hard cap imposed by the provider
BSC_BLOCK_S = 0.45       # measured 0.4501 s/block; calibrated at runtime
PUBLIC_HEAD = "https://bsc-dataseed.binance.org"


@dataclass(slots=True)
class Fill:
    block: int
    token_id: str
    price: float          # USDT per share, i.e. implied probability
    shares: float
    usdt: float


class RpcError(RuntimeError):
    pass


class BscClient:
    # The daily quota is a scarce, shared resource, so usage is persisted across
    # runs rather than reset every process.
    BUDGET_FILE = Path("data/rpc_calls.json")

    def __init__(self, api_key: str, daily_budget: int = 1000,
                 min_interval_s: float = 0.35):
        if not api_key:
            raise RpcError("no 1rpc.io API key configured (rpcio_api_key in .env)")
        self.url = f"https://1rpc.io/{api_key}/bnb"
        self.daily_budget = daily_budget
        self.min_interval_s = min_interval_s
        self._last_call = 0.0
        self.calls = 0
        self._load_budget()

    def _load_budget(self) -> None:
        today = time.strftime("%Y-%m-%d")
        try:
            d = json.loads(self.BUDGET_FILE.read_text())
            self.used_today = d.get("count", 0) if d.get("day") == today else 0
        except Exception:
            self.used_today = 0
        self._day = today

    def _save_budget(self) -> None:
        try:
            self.BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.BUDGET_FILE.write_text(
                json.dumps({"day": self._day, "count": self.used_today}))
        except Exception:
            pass

    @property
    def remaining(self) -> int:
        return max(0, self.daily_budget - self.used_today)

    def _post(self, url: str, method: str, params: list, timeout: int = 20):
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps({"jsonrpc": "2.0", "id": 1,
                             "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def call(self, method: str, params: list):
        if self.remaining <= 0:
            raise RpcError(f"daily RPC budget of {self.daily_budget} exhausted")
        # Space out requests: the provider enforces a burst limit (HTTP 429)
        # independently of the daily quota.
        gap = time.time() - self._last_call
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last_call = time.time()

        self.calls += 1
        self.used_today += 1
        self._save_budget()
        out = self._post(self.url, method, params)
        if "error" in out:
            raise RpcError(out["error"].get("message", "rpc error"))
        return out["result"]

    # -- chain metadata -----------------------------------------------------
    def public_head(self) -> int:
        """Head from a public node -- does not consume the metered budget."""
        return int(self._post(PUBLIC_HEAD, "eth_blockNumber", [])["result"], 16)

    def block_time(self, block: int) -> int:
        """Timestamp for a block, read from public nodes.

        Block metadata is free everywhere, so it must not consume the metered
        getLogs budget. Public nodes are also load-balanced and some lag, hence
        the fallback list -- a stale node returns null rather than an error.
        """
        for url in (PUBLIC_HEAD, "https://bsc-dataseed1.defibit.io",
                    "https://bsc-dataseed2.binance.org"):
            try:
                b = self._post(url, "eth_getBlockByNumber", [hex(block), False])["result"]
                if b and b.get("timestamp"):
                    return int(b["timestamp"], 16)
            except Exception:
                continue
        raise RpcError(f"no public node served block {block}")

    def calibrate(self, head: int, back: int = 40_000) -> tuple[int, int, float]:
        """Return (head_block, head_ts, seconds_per_block) from two samples."""
        t_head = self.block_time(head)
        t_old = self.block_time(head - back)
        spb = (t_head - t_old) / back
        log.info("chain calibrated: %.4f s/block over %d blocks", spb, back)
        return head, t_head, spb

    @staticmethod
    def block_for(ts: int, head: int, head_ts: int, spb: float) -> int:
        return int(head - (head_ts - ts) / spb)

    # -- trades -------------------------------------------------------------
    def fills(self, from_block: int, to_block: int) -> list[Fill]:
        if to_block - from_block > MAX_SPAN:
            raise RpcError(f"span {to_block - from_block} exceeds {MAX_SPAN}")
        params = [{"address": EXCHANGE, "topics": [ORDER_FILLED],
                   "fromBlock": hex(from_block), "toBlock": hex(to_block)}]
        for attempt in range(4):
            try:
                logs = self.call("eth_getLogs", params)
                break
            except urllib.error.HTTPError as e:
                # 429 is the burst limiter, not the daily quota. Back off and
                # retry; a failed request still consumed a call.
                if e.code != 429 or attempt == 3:
                    raise
                time.sleep(2.0 * (attempt + 1))
            except RpcError:
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
            except (TimeoutError, OSError) as e:
                # Socket timeouts and connection resets are common on this
                # provider and must not abort a long harvest.
                if attempt == 3:
                    raise
                log.debug("transient network error: %s", e)
                time.sleep(1.5 * (attempt + 1))
        else:
            return []
        out = []
        for l in logs:
            d = l["data"][2:]
            if len(d) < 256:
                continue
            mk_id = int(d[0:64], 16)
            tk_id = int(d[64:128], 16)
            mk_amt = int(d[128:192], 16)
            tk_amt = int(d[192:256], 16)
            # exactly one side is collateral (assetId 0)
            if mk_id == 0 and tk_id != 0:
                token, usdt, shares = tk_id, mk_amt, tk_amt
            elif tk_id == 0 and mk_id != 0:
                token, usdt, shares = mk_id, tk_amt, mk_amt
            else:
                continue
            if shares <= 0:
                continue
            price = usdt / shares
            if not 0.0 < price < 1.0:
                continue
            out.append(Fill(block=int(l["blockNumber"], 16), token_id=str(token),
                            price=price, shares=shares / 1e18, usdt=usdt / 1e18))
        return out
