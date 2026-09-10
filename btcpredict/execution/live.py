"""Live Binance prediction-market broker.

Endpoints were recovered from the developer docs' `llms.txt` index and verified
against the live API. Namespace: /sapi/v1/w3w/wallet/prediction/...
Auth is standard Binance SAPI: HMAC-SHA256 over the alphabetically sorted query
string, with an `X-MBX-APIKEY` header. Prediction access is a separate grant from
the spot trading flags -- a key showing `enableSpotAndMarginTrading: false` in
apiRestrictions can still trade prediction markets.

Ordering is two-step and time-critical:

    1. POST trade/get-quote          -> quoteId, amountOut, averagePrice, feeAmount
    2. POST trade/place-order-bundle -> executes that quote

The quote carries an `expireAt` roughly 5 seconds out, so step 2 must follow
immediately. `amountIn` is an integer string in **18-decimal wei** (1 USDT =
10**18); sending plain "1" is rejected as "order amount too small", which is a
misleading message for a units error.

Order parameters were established empirically against the live venue:
`timeInForce` must be "FOK" (IOC and GTC return -1102) and `accountType` must be
"SPOT" (CeDeFi returns -3026, despite being what balance/payment-options lists).

The fee is read from the live quote rather than assumed: the market metadata
advertises `feeRateBps: 200`, but observed quotes returned `feeRateBps: 64`
with a `feeDiscountBps: 1000`. Trusting the metadata would misprice every trade.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from decimal import Decimal
from urllib.parse import urlencode

import aiohttp

from btcpredict.execution.base import Fill, Settlement

log = logging.getLogger(__name__)

BASE = "https://api.binance.com"
P = "/sapi/v1/w3w/wallet/prediction"

WEI = Decimal(10) ** 18

GET_QUOTE = f"{P}/trade/get-quote"
PLACE_ORDER = f"{P}/trade/place-order-bundle"
ORDER_BOOK = f"{P}/order-book"
MARKET_LIST = f"{P}/market/list"
WALLET_LIST = f"{P}/wallet/list"
POSITION_LIST = f"{P}/position/list"
PAYMENT_OPTIONS = f"{P}/balance/payment-options"
BATCH_REDEEM = f"{P}/batch-redeem"
ORDER_HISTORY = f"{P}/order/history"


def to_wei(usdt: float) -> str:
    return str(int(Decimal(str(usdt)) * WEI))


def from_wei(wei: str | int | None) -> float:
    if wei in (None, ""):
        return 0.0
    return float(Decimal(str(wei)) / WEI)


class LiveBrokerError(RuntimeError):
    pass


# `buy()` failure reasons for which the venue is KNOWN to hold no order, so
# placing again cannot double the exposure. Anything else -- a timeout, a 5xx,
# a status we could not read -- may have left a live order behind and must
# never be re-placed. See `LiveBroker.last_failure`.
RETRYABLE_FAILURES = frozenset({"no_quote", "quote_expired", "rejected",
                                "venue_failed"})


class LiveBroker:
    mode = "live"

    def __init__(self, session: aiohttp.ClientSession, api_key: str, api_secret: str,
                 *, wallet_address: str = "", wallet_id: str = "",
                 account_type: str = "SPOT", slippage_bps: int = 100,
                 recv_window: int = 5000):
        self.session = session
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.wallet_address = wallet_address
        self.wallet_id = wallet_id
        self.account_type = account_type
        self.slippage_bps = slippage_bps
        self.recv_window = recv_window
        # Why the most recent `buy()` returned None, so the caller can tell a
        # safe-to-retry rejection from an unknown outcome (see
        # RETRYABLE_FAILURES). Only one order is ever in flight at a time
        # (max_open_rounds == 1), so a single field per broker is enough.
        self.last_failure: str | None = None
        self._transport_error = False
        self._last_status = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret
                    and self.wallet_address and self.wallet_id)

    # -- transport ----------------------------------------------------------
    def _sign(self, params: dict) -> str:
        p = {**params, "timestamp": int(time.time() * 1000),
             "recvWindow": self.recv_window}
        qs = urlencode(sorted(p.items()))
        # The venue verifies the signature over RAW commas in comma-separated
        # list parameters (observed on batch-redeem after a contract change:
        # %2C-encoded queries started returning -1022). Replacing %2C with
        # ',' keeps the signed string identical to what is sent. No other
        # parameter uses reserved characters.
        qs = qs.replace("%2C", ",")
        sig = hmac.new(self.api_secret, qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    async def _request(self, method: str, path: str, params: dict) -> dict | None:
        url = f"{BASE}{path}?{self._sign(params)}"
        self._transport_error = False
        self._last_status = 0
        try:
            async with self.session.request(
                method, url, headers={"X-MBX-APIKEY": self.api_key},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                body = await r.json(content_type=None)
                self._last_status = r.status
                if r.status != 200:
                    log.error("live %s %s -> HTTP %s: %s", method, path, r.status, body)
                    return None
                return body
        except Exception as e:
            # The request may still have reached the venue. For an order that
            # means the outcome is UNKNOWN, never "it did not happen".
            self._transport_error = True
            log.error("live %s %s failed: %s: %s", method, path, type(e).__name__, e)
            return None

    # -- discovery ----------------------------------------------------------
    async def discover_wallet(self) -> bool:
        """Populate wallet address/id from the account. Safe, read-only."""
        resp = await self._request("GET", WALLET_LIST, {})
        wallets = (resp or {}).get("wallets") or []
        if not wallets:
            log.error("no prediction wallet found -- create one in the Binance app")
            return False
        self.wallet_address = wallets[0]["walletAddress"]
        self.wallet_id = wallets[0]["walletId"]
        log.info("prediction wallet %s", self.wallet_address)
        return True

    async def order_book(self, token_id: str, market_id: int) -> dict | None:
        """Live book. `vendor` is required or the request 400s."""
        return await self._request("GET", ORDER_BOOK, {
            "tokenId": token_id, "vendor": "PREDICT_FUN", "marketId": market_id})

    async def best_prices(self, token_id: str, market_id: int
                          ) -> tuple[float, float] | None:
        ob = await self.order_book(token_id, market_id)
        if not ob:
            return None
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        if not bids or not asks:
            return None
        return (max(float(b["price"]) for b in bids),
                min(float(a["price"]) for a in asks))

    async def balance(self) -> float:
        resp = await self._request("GET", POSITION_LIST,
                                   {"walletAddress": self.wallet_address})
        if not resp:
            return 0.0
        return float((resp.get("summary") or {}).get("walletBalance") or 0.0)

    async def positions(self) -> list[dict]:
        resp = await self._request("GET", POSITION_LIST,
                                   {"walletAddress": self.wallet_address})
        return (resp or {}).get("positions") or []

    async def _order_status(self, order_id: str,
                            tries: int = 8, delay: float = 1.5) -> str | None:
        """Authoritative order status from order/history.

        The venue's lifecycle is SUBMITTED -> FILLED | FAILED, and history can
        lag the place-order call by several seconds. Only terminal states are
        conclusive: FILLED is success, everything clearly terminal
        (FAILED / CANCELLED / EXPIRED / REJECTED) is failure. SUBMITTED means
        "still in flight" and is retried. Returns None when the endpoint cannot
        be reached at all (unverifiable), which the caller treats separately.
        """
        in_flight = {"SUBMITTED", "PENDING", "NEW", "FILLING", "PARTIAL_FILL"}
        terminal_fail = {"FAILED", "CANCELLED", "CANCELED", "EXPIRED",
                         "REJECTED"}
        for i in range(tries):
            resp = await self._request("GET", ORDER_HISTORY, {
                "walletAddress": self.wallet_address,
                "pageSize": 50, "pageNo": 1,
            })
            if resp:
                for o in (resp.get("orders") or []):
                    if o.get("orderId") == order_id:
                        status = o.get("status") or ""
                        if status == "FILLED" or status in terminal_fail:
                            return status
                        # SUBMITTED etc.: still in flight, retry below.
                        break
            if i < tries - 1:
                await asyncio.sleep(delay)
        return None

    # -- trading ------------------------------------------------------------
    async def get_quote(self, token_id: str, stake: float, side: str = "BUY") -> dict | None:
        return await self._request("POST", GET_QUOTE, {
            "walletAddress": self.wallet_address,
            "tokenId": token_id,
            "side": side,
            "amountIn": to_wei(stake),
            "orderType": "MARKET",
            "slippageBps": self.slippage_bps,
        })

    async def buy(self, *, slug: str, side: str, token_id: str,
                  price: float, stake: float,
                  quote: dict | None = None) -> Fill | None:
        if not self.enabled:
            raise LiveBrokerError("wallet not configured; call discover_wallet() first")
        self.last_failure = None

        # A quote may be supplied by the caller (the pre-trade recheck already
        # fetched one). Using it avoids a second round trip that eats into the
        # quote's ~5s expireAt window -- the dominant cause of FOK failures.
        if quote is None:
            quote = await self.get_quote(token_id, stake)
        if not quote or not quote.get("quoteId"):
            self.last_failure = "no_quote"
            log.error("no quote for %s %s stake=%.2f", slug, side, stake)
            return None

        shares_gross = from_wei(quote.get("amountOut"))
        fee_shares = from_wei(quote.get("feeAmount"))
        avg_px = float(quote.get("averagePrice") or price)
        expire_at = int(quote.get("expireAt") or 0)
        if expire_at and expire_at <= time.time() * 1000:
            self.last_failure = "quote_expired"
            log.warning("quote %s already expired; skipping", quote["quoteId"])
            return None

        resp = await self._request("POST", PLACE_ORDER, {
            "walletAddress": self.wallet_address,
            "walletId": self.wallet_id,
            "quoteId": quote["quoteId"],
            # Verified against the live venue: FOK is the only accepted value --
            # IOC and GTC are both rejected with -1102. accountType must be SPOT;
            # CeDeFi returns -3026 even though payment-options advertises it.
            "timeInForce": "FOK",
            "accountType": self.account_type,
            "orderType": "MARKET",
            "slippageBps": self.slippage_bps,
        })
        if not resp:
            # A 4xx carries a venue error code (-1102, -3026, ...): the order
            # was refused outright and nothing is live. A transport failure or
            # a 5xx is different -- the order may have been accepted and we
            # simply never saw the reply, so its outcome is unknown.
            refused = (not self._transport_error
                       and 400 <= self._last_status < 500)
            self.last_failure = "rejected" if refused else "unknown"
            return None

        # The venue returns HTTP 200 + {orderId} even when the order FAILS to
        # fill ("Failed to execute the market order"). Never trust it: look the
        # order up in order/history, which reports the authoritative status.
        # History can lag a second or two, so retry briefly.
        order_id = resp.get("orderId") or resp.get("bundleId")
        status = await self._order_status(order_id)
        if status is None:
            log.warning("order %s placed but history unavailable; "
                        "proceeding unverified", order_id)
        elif status != "FILLED":
            # A terminal non-FILLED status is authoritative: the venue took no
            # position, so the caller may safely place a fresh order. FOK
            # market orders land here whenever the book moves between the
            # quote and the execution, which is transient.
            self.last_failure = "venue_failed"
            log.error("order %s status=%s on venue; treating as failed (%s)",
                      order_id, status, slug)
            return None

        shares = shares_gross - fee_shares
        # The fee is quoted in outcome-token units; express it in USDT so PnL
        # accounting stays in one currency.
        fee_usdt = fee_shares * avg_px
        return Fill(
            slug=slug, side=side, requested_px=price,
            filled_px=(stake / shares) if shares else avg_px,
            shares=shares, stake=stake, fee_paid=fee_usdt,
            order_id=str(resp.get("orderId") or resp.get("bundleId")
                         or quote["quoteId"]),
            ts_ms=int(time.time() * 1000), raw=str(resp)[:2000],
            token_id=token_id,
            # Status unreadable: the venue may or may not hold an order. The
            # fill is provisional and settle() will re-verify and void it if
            # the venue never actually filled anything.
            unverified=(status is None),
        )

    async def settle(self, fill: Fill, winner: str) -> Settlement | None:
        if getattr(fill, "unverified", False):
            # The venue returns 200 + orderId even when a FOK order fails, so
            # a fill booked without a confirmed status may be phantom. By
            # settlement time the order-history has caught up: FILLED proves
            # the position, any terminal-failure proves there never was one.
            status = await self._order_status(fill.order_id)
            if status is None:
                fill.unverified_checks += 1
                if fill.unverified_checks < 20:
                    return None    # still unreadable; caller retries next tick
                # Give up on history and ask the venue's position list: a
                # claimable token for this position proves a real fill.
                claimable = await self.claimable_token_ids()
                if fill.token_id in claimable:
                    status = "FILLED"
            if status != "FILLED":
                log.warning("unverified order %s status=%s; voiding fill for %s",
                            fill.order_id, status, fill.slug)
                return Settlement(slug=fill.slug, won=False, payout=0.0,
                                  pnl=0.0, ts_ms=int(time.time() * 1000),
                                  void=True)
        won = fill.side.lower() == (winner or "").lower()
        payout = fill.shares if won else 0.0
        return Settlement(slug=fill.slug, won=won, payout=payout,
                          pnl=payout - fill.stake, ts_ms=int(time.time() * 1000))

    async def claimable_token_ids(self) -> list[str]:
        resp = await self._request("GET", f"{P}/position/filter", {
            "walletAddress": self.wallet_address, "status": "CLAIMABLE"})
        return [p["tokenId"] for p in (resp or {}).get("positions") or []
                if p.get("tokenId")]

    async def redeem(self, token_ids: list[str] | None = None) -> dict | None:
        """Claim settled winnings.

        Winnings do NOT auto-credit: a won position sits as `pendingClaim` until
        redeemed. `tokenIds` is mandatory (omitting it returns -1102).
        Redeem one token per call: the venue system-errors (-9000) on a batch
        that contains a not-yet-final token, and per-token calls keep the rest
        of the winnings flowing.
        """
        ids = token_ids if token_ids is not None else await self.claimable_token_ids()
        if not ids:
            return None
        ok, failed = [], []
        for tok in ids:
            resp = await self._request("POST", BATCH_REDEEM, {
                "walletAddress": self.wallet_address,
                "walletId": self.wallet_id,
                "tokenIds": tok})
            if resp:
                ok.append(resp)
            else:
                failed.append(tok)
        if not ok:
            return None
        return {"redeemed": len(ok), "failed_tokens": len(failed), "responses": ok}
