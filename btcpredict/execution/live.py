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
import http.client
import json
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
ACTIVE_ORDERS = f"{P}/order/list"
BATCH_CANCEL = f"{P}/trade/batch-cancel"


#: The venue refuses MARKET orders under roughly this notional. ITS OWN DOC says
#: "For `MARKET` orders, minimum is approximately 1.5 USDT (varies by market
#: liquidity)" -- and "approximately" is doing real work in that sentence.
#:
#: ⛔ CORRECTED 2026-09-23, BY THE OPERATOR, who places these orders in the
#: venue's own UI: the minimum is **$1.00**. The documented 1.5 is what THIS
#: GUARD was built on -- and the guard is a PRE-EMPTIVE REFUSAL, the order is
#: never sent, so nothing downstream could ever correct it. Every sale whose
#: proceeds fell between $1.00 and $1.50 was therefore refused by US and
#: reported as the venue's doing (`sell_below_venue_minimum`).
#:
#: THE ASYMMETRY IS WHY ERRING LOW IS THE SAFE SIDE. A sale the venue really
#: does refuse costs one rejected call and comes back as `-9000 Your order
#: amount is too small`, which this module already classifies and the exit
#: retries on the next tick -- the position stays watched and nothing is
#: stranded. A guard set TOO HIGH silently kills exits that would have
#: succeeded, with no order, no error and no log line from the venue: exactly
#: the failure mode this bot spent 2026-09-22 chasing.
#:
#: ⛔ CORRECTED AGAIN 2026-09-23 23:10, BY THE OPERATOR: there is NO minimum on
#: a SELL. The $1.00 guard refused the 22:04 grind exit (5.71 sh @ 0.14 =
#: $0.78, never sent) and the position settled -$3.90. So the guard is 0: every
#: sale is sent and the VENUE decides. Kept as a name because the replay tool
#: (`tools/model_knob_sweep.py`) imports it, so the sim follows the same rule.
SELL_MIN_NOTIONAL_USDT = 0.0

#: The SELL size ladder `_sell_quote` walks when the venue refuses a quote, as
#: fractions of the ask; the last rung is floored at 0.01 share (the venue's
#: step). Quotes are read-only, so a longer ladder costs time, never an order.
SELL_STEP_FRACS = (1.0, 0.99, 0.95, 0.90, 0.80, 0.65, 0.50, 0.35, 0.25,
                   0.15, 0.10, 0.05, 0.0)
SELL_STEP_PAUSE_S = 0.2

#: Order statuses the venue considers FINAL. A MARKET order-bundle on this venue
#: takes 1.2-2.2 s from `createTime` to `terminalTime` (measured 2026-09-23:
#: 13:52:31.627 -> 13:52:32.915, 13:36:52.522 -> 13:36:54.714,
#: 11:29:43.878 -> 11:29:45.283), so a single snapshot read right after the ack
#: shows a WORKING order -- which is how four FILLED sells were booked as
#: `fak_unfilled` (see `sell_fak`).
ORDER_TERMINAL_STATUSES = frozenset({
    "FILLED", "PARTIALLY_FILLED", "FAILED", "CANCELED", "CANCELLED",
    "REJECTED", "EXPIRED", "CLOSED",
})
#: Terminal AND without a fill: the venue genuinely matched nothing.
ORDER_DEAD_STATUSES = frozenset({
    "FAILED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "CLOSED",
})
#: How long `sell_fak` waits for a terminal status before handing the decision
#: to the caller's late-confirmation path. Covers the measured 1.2-2.2 s fills
#: with room to spare, and is short enough not to stall the tick loop.
SELL_CONFIRM_WAIT_S = 3.0
#: How long `buy` waits for a MARKET bundle whose status is still non-terminal
#: before it may call the order anything. Measured live 2026-09-24 04:52:
#: order 26092400001911886956 went FILLED **11.1 s** after createTime, while
#: the bot had already declared it dead at 6.8 s and tried to buy again (only a
#: min_quote refusal stopped a double position). FAILED stamps have been seen
#: at 5-14 s. So a verdict is waited for, not inferred.
BUY_TERMINAL_WAIT_S = 10.0
BUY_TERMINAL_POLL_S = 0.75
SELL_CONFIRM_POLL_S = 0.25


def derived_slip_bps(default_bps: int, max_px: float | None,
                     avg_px: float) -> int:
    """The `slippageBps` to send, given the caller's own price ceiling.

    The venue anchors the FOK bound to the QUOTE's price -- bound = quote *
    (1 + slip/10000) -- and caps `slip` at 10000 (2x the quote) for every
    client. So when the caller names a ceiling, the ceiling is only actually
    enforced if `slip` is derived from it.

    `default_bps` is used ONLY when there is no ceiling, and it must NOT be a
    floor on the derived value. With a ceiling, flooring can only ever push the
    bound ABOVE that ceiling, because `room` falls below `default_bps` exactly
    when the quote has drifted within default_bps/10000 of it. Measured live
    2026-09-15: quote 0.5800 against ceiling 0.58 authorised 0.5974 -- 3.74c
    above a trigger ask of 0.56 when the ceiling said 2c. Clamp to the ceiling.

    The result is always within the venue's documented range (1 to 10000).
    """
    if max_px is None or avg_px <= 0:
        return default_bps
    room = int((max_px / avg_px - 1.0) * 10_000)
    # FLOOR OF 1, NOT 0. The venue's own schema types `slippageBps` as
    # "Slippage tolerance in basis points. Range 1–10000"
    # (slippagebps/schema(1).yaml:1405 for place-order-bundle, :1675 for
    # get-quote). A quote at or above the ceiling yields room <= 0, and sending
    # 0 is OUT OF RANGE -- measured live 2026-09-15 19:14, two attempts with
    # slip 0 came back in 19ms and 18ms as last_failure "rejected", while every
    # in-range attempt on the same arm took 200-250ms and failed as
    # "venue_failed". The 1bp costs 0.006c on a 0.60 quote and is the smallest
    # value the schema permits here.
    return max(1, min(room, 10_000))


# The venue anchors the FOK bound to the QUOTE and caps `slippageBps` at 10000,
# so `bound <= quote * 2` FOR EVERY CLIENT. A quote whose 2x cannot reach our
# ceiling therefore cannot fill -- not slowly, not at any latency. It is dead
# when it is signed, and it is signable from our side, which makes it a
# protocol error rather than a strategy outcome.
_VENUE_MAX_SLIP_BPS = 10_000
# REMOVED 2026-09-16, ~25 minutes after shipping. There was a
# `_STALE_QUOTE_MAX_DROP` branch that discarded any quote more than 5c BELOW the
# ceiling. It was wrong, and it cost a fill on eth-updown-15m-1789537500 within
# the hour:
#
#   CROSS eth-updown-15m-1789537500 Down sweep_px=0.490 (spot 2406.64 ...)
#   warm quote used ... age 0.98s px 0.4400
#   shadow quote ... fresh px 0.4400 after 52ms -> bound 0.5100 (ceiling 0.510)
#   ... discarding quote (quote_stale: 0.4400 is 0.0700 below ceiling 0.5100)
#   CROSS order failed ... (quote_stale); not retrying -> NO FILL in 0.05s
#
# The quote was FRESH -- 52ms -- and the venue really was at 0.44. The premise
# was wrong: `max_px` is OUR OWN limit (the decided price plus our bound), so a
# quote BELOW it is the market offering a BETTER price than we authorised. That
# is the case we want, not a defect, and refusing it is exactly backwards. The
# harmful displacement is the other direction and `quote_above_ceiling` already
# refuses it. The 42-of-112 "displaced" cohort that motivated the branch was
# this same benign situation miscounted as harm.
#
# `quote_unfillable` below is KEPT: that one is a genuine protocol error.


def quote_is_unusable(avg_px: float, max_px: float | None) -> str | None:
    """Why this quote can never produce a fill, or None if it can fill.

    ONE test only, and it is structural: the venue anchors the FOK bound to the
    QUOTE and caps `slippageBps` at 10000, so `bound <= quote * 2` for every
    client. When `quote * 2 < ceiling` the ceiling is unreachable and the order
    is dead when it is signed -- not slow, not at any latency. 7 of 112 live
    attempts were in exactly that state and every one carried `slip 10000`, the
    clamp showing its work:
        quote 0.0696 (x2 = 0.1392) against a ceiling of 0.650
        quote 0.0400 (x2 = 0.0800) against a ceiling of 0.190

    Nothing else belongs here. In particular a quote BELOW the ceiling is not a
    defect -- see the note above.
    """
    if avg_px <= 0:
        return "quote_price_missing"
    if max_px is None:
        return None
    if avg_px * (1.0 + _VENUE_MAX_SLIP_BPS / 10_000.0) < max_px:
        return (f"quote_unfillable: quote {avg_px:.4f} x2 = {avg_px * 2:.4f} "
                f"cannot reach the ceiling {max_px:.4f}")
    return None


def amount_usdt(x: str | int | float | None) -> float:
    """An order-record amount as USDT.

    Order/quotes disagree on units in the venue's own docs: quotes carry wei
    ("1000000000000000000"), order records carry decimals ("1.00"). Reading
    one as the other is a 1e18 error, so treat anything implausibly large as
    wei rather than trusting either convention.
    """
    try:
        v = float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return float(from_wei(int(v))) if v >= 1e6 else v


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
# a status we could not read, or an absence from a position list that has not
# yet caught up (`unconfirmed`) -- may have left a live order behind and must
# never be re-placed. See `LiveBroker.last_failure`.
RETRYABLE_FAILURES = frozenset({"no_quote", "quote_expired", "rejected",
                                "venue_failed"})
TERMINAL_FAIL_STATUSES = frozenset({"FAILED", "CANCELLED", "CANCELED",
                                   "EXPIRED", "REJECTED"})


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
        # The venue's own explanation for the most recent failure, verbatim.
        # It lives ONLY in the order-history record (`errorMessage`), which
        # `_order_status` reads and used to throw away -- so every rejection
        # before 2026-09-12 was logged as a bare status with no cause. The one
        # value seen so far is "Failed to execute the market order", which
        # means the bundle's reference price was stale by more than
        # `slippageBps` when it executed (measured: a quote taken while the
        # displayed price was still 0.15 against a book that had already
        # repriced to 0.60).
        self.last_order_error: str | None = None
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
        # ',' keeps the signed string identical to what is sent. The same
        # applies to batch-cancel's indexed brackets (documented as a known
        # issue: %5B/%5D break the signature), and for the same reason.
        qs = qs.replace("%2C", ",").replace("%5B", "[").replace("%5D", "]")
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
        # DEBUG: the wallet address is already printed, once, by the
        # `LIVE mode. wallet <addr> balance $X` line in Bot.setup, which also
        # carries the number that matters. This was 1,322 lines of duplicate.
        log.debug("prediction wallet %s", self.wallet_address)
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
        """Cash sitting in the wallet. NOT equity -- see `equity()`."""
        resp = await self._request("GET", POSITION_LIST,
                                   {"walletAddress": self.wallet_address})
        if not resp:
            return 0.0
        return float((resp.get("summary") or {}).get("walletBalance") or 0.0)

    async def equity(self, *, refresh: bool = False) -> float:
        """Total account value: cash + open positions + unredeemed winnings.

        `refresh` is accepted and IGNORED: this read is live on every call, so
        there is nothing to force. It exists for SIGNATURE PARITY with
        `PmBroker.equity` -- which DOES cache and needs the flag -- because a
        broker that mirrors the mode but not the signature passes every guard
        and then raises TypeError on the first call (measured on `buy()`'s
        `quote=` parameter, 2026-09-17; see `PmBroker.buy`).

        `walletBalance` alone is what sizing used to read, and it is the source
        of the lag `Bot._reconcile_bankroll` was written to defend against: a
        stake leaves the wallet the instant an order fills, while the payout
        only reappears after redemption settles on-chain. In between, the
        wallet under-states the account by the whole position.

        The venue publishes the missing pieces. Verified live 2026-09-15:

            totalValue 42.12746449
              = walletBalance 42.12746449
              + positionValue 0
              + totalClaimableAmount 0

        so `totalValue` is exactly the sum, and reading it removes the lag
        rather than waiting it out. The sum is recomputed as a fallback in case
        `totalValue` is ever absent, and `walletBalance` remains the last
        resort so this can never return 0 on a partial response.
        """
        resp = await self._request("GET", POSITION_LIST,
                                   {"walletAddress": self.wallet_address})
        if not resp:
            return 0.0
        s = resp.get("summary") or {}

        def num(key: str) -> float:
            try:
                return float(s.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        total = num("totalValue")
        parts = num("walletBalance") + num("positionValue") + num("totalClaimableAmount")
        return total or parts or num("walletBalance")

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
        for i in range(tries):
            resp = await self._request("GET", ORDER_HISTORY, {
                "walletAddress": self.wallet_address,
                "pageSize": 50, "pageNo": 1,
            })
            if resp:
                for o in (resp.get("orders") or []):
                    if str(o.get("orderId")) == str(order_id):
                        status = o.get("status") or ""
                        # The venue explains a refusal here and nowhere else.
                        # Read it before the status check: a FAILED record with
                        # an empty `errorMessage` is a different problem from
                        # one the venue explained.
                        err = o.get("errorMessage")
                        if err:
                            self.last_order_error = str(err)
                        if status == "FILLED" or status in TERMINAL_FAIL_STATUSES:
                            if status in TERMINAL_FAIL_STATUSES:
                                log.error("order %s %s: %s", order_id, status,
                                          err or "(venue gave no reason)")
                            return status
                        # SUBMITTED etc.: still in flight on-chain, retry below.
                        if i >= 5 and (i % 5 == 0 or i == tries - 1):
                            log.info("order %s still %s (try %d/%d)",
                                     order_id, status, i + 1, tries)
                        break
            if i < tries - 1:
                await asyncio.sleep(delay)
        return None

    # -- trading ------------------------------------------------------------
    async def get_quote(self, token_id: str, stake: float, side: str = "BUY",
                        *, order_type: str = "MARKET",
                        price_limit: float | None = None) -> dict | None:
        params = {
            "walletAddress": self.wallet_address,
            "tokenId": token_id,
            "side": side,
            "amountIn": to_wei(stake),
            "orderType": order_type,
            "slippageBps": self.slippage_bps,
        }
        # LIMIT quotes REQUIRE priceLimit (verified: omitting it is refused
        # with a generic -3026 that names no field).
        if order_type == "LIMIT" and price_limit is not None:
            params["priceLimit"] = f"{price_limit:.2f}"
        return await self._request("POST", GET_QUOTE, params)

    async def order_record(self, order_id: str, tries: int = 3,
                           delay: float = 0.4) -> dict | None:
        """The full order-history record for one order, or None if unknown.

        `_order_status` answers "did it work?"; this answers "how much of it
        filled, at what price" -- which is what a resting order needs.
        """
        for i in range(tries):
            resp = await self._request("GET", ORDER_HISTORY, {
                "walletAddress": self.wallet_address,
                "pageSize": 50, "pageNo": 1,
            })
            for o in ((resp or {}).get("orders") or []):
                if str(o.get("orderId")) == str(order_id):
                    return o
            if i < tries - 1:
                await asyncio.sleep(delay)
        return None

    async def _await_terminal(self, order_id: str,
                              max_wait_s: float) -> dict | None:
        """The order's history record once it is TERMINAL, bounded by time.

        Returns the last record seen: terminal (FILLED or a failure) when the
        venue concluded in time, otherwise whatever non-terminal record was last
        read, or None if the order never appeared in the history at all. The
        caller must treat a non-terminal or missing record as UNKNOWN -- never
        as "did not fill" (see BUY_TERMINAL_WAIT_S).
        """
        deadline = time.monotonic() + max_wait_s
        rec = None
        while True:
            got = await self.order_record(order_id, tries=1)
            if got is not None:
                rec = got
                st = str(got.get("status") or "").upper()
                if st == "FILLED" or st in TERMINAL_FAIL_STATUSES:
                    return got
            if time.monotonic() >= deadline:
                return rec
            await asyncio.sleep(BUY_TERMINAL_POLL_S)

    async def active_orders(self) -> list[dict]:
        """Orders still working (OPENING, partly filled) for this wallet."""
        resp = await self._request("GET", ACTIVE_ORDERS, {
            "walletAddress": self.wallet_address, "limit": 50,
        })
        return (resp or {}).get("orders") or []

    async def _working_order_ids(self) -> list[str] | None:
        """Ids the venue still holds as WORKING, or None if unreadable.

        ⛔ None AND [] ARE DIFFERENT ANSWERS. [] means "we read the list and
        nothing of ours is working"; None means "we could not read it". Only
        the first can authorise placing another order -- `active_orders()`
        collapses both into [], which is exactly the distinction an exposure
        check cannot afford to lose.

        ⚠️ `cancel_orders` ALREADY READS THIS SAME LIST on its refusal path
        (`_cancel_blocking` -> the -9000 branch), and it logs the same
        distinction. This asks again anyway because that function's RETURN
        cannot answer the question: it returns the ids the venue confirmed
        cancelled, so an order that was already terminal and one whose working
        state could not be read both come back as []. Measured 2026-09-22: the
        duplicate read costs 23ms, which is worth paying for an unambiguous
        answer about exposure rather than refactoring a live path to supply it.
        """
        resp = await self._request("GET", ACTIVE_ORDERS, {
            "walletAddress": self.wallet_address, "limit": 50,
        })
        if resp is None:
            return None
        return [str(o.get("orderId")) for o in (resp.get("orders") or [])]

    async def cancel_orders(self, order_ids: list[str]) -> list[str]:
        """Cancel working orders; returns the ids the venue confirms cancelled.

        Built by hand over http.client rather than the normal client. The
        parameters are indexed brackets (cancelInfoList[0].orderId) and the
        venue signs the RAW string, so any HTTP layer that percent-encodes
        [ ] -> %5B/%5D yields -1022 -- measured live on 2026-09-11 with
        aiohttp, which does exactly that. http.client sends the bytes as
        given, and `_sign` already restores the brackets it encodes.
        """
        if not order_ids:
            return []
        done = await asyncio.to_thread(self._cancel_blocking, list(order_ids))
        settled = {str(d) for d in done}
        left = [o for o in order_ids if str(o) not in settled]
        if not left:
            return done
        # The venue answers -9000 SYSTEM_ERROR to cancels it will not take:
        # measured 15 of 15 on 2026-09-12 while the orders had already filled
        # 98-99%, and 104 times in the log since 09-11 (batch-redeem too).
        #
        # A refused cancel is NOT evidence the order is still working. The
        # remainder is usually a 1-2% remnant that already filled or expired
        # with its round. Assuming "still working" would cry wolf on every
        # single order; assuming "cancelled" would leave a genuine orphan
        # unmanaged on the ~5% of rounds where one exists. So ask the venue's
        # own working-order list, which is the only authority, and treat an
        # unanswerable question as unanswered rather than as an answer.
        resp = await self._request("GET", ACTIVE_ORDERS, {
            "walletAddress": self.wallet_address, "limit": 50,
        })
        if resp is None:
            log.warning("cancel of %s was refused and the working-order list "
                        "could not be read, so their state is UNKNOWN -- not "
                        "retrying blind", left)
            return done
        working = {str(o.get("orderId")) for o in (resp.get("orders") or [])}
        stuck = [o for o in left if str(o) in working]
        if not stuck:
            # The expected resolution of the -9000 above, and equally
            # routine: DEBUG so the pair does not dominate the log. The two
            # branches that DO matter -- an unreadable working list, and an
            # order the venue still lists -- stay at WARNING below.
            log.debug("cancel of %s was refused but the venue shows nothing "
                      "working; treating as done", left)
            return done
        log.warning("cancel refused and the venue still lists %s as working; "
                    "retrying once", stuck)
        done += await asyncio.to_thread(self._cancel_blocking, stuck)
        settled = {str(d) for d in done}
        after = await self._request("GET", ACTIVE_ORDERS, {
            "walletAddress": self.wallet_address, "limit": 50,
        })
        if after is None:
            log.warning("retried cancel of %s; state now unknown", stuck)
            return done
        still = {str(o.get("orderId")) for o in (after.get("orders") or [])}
        for o in stuck:
            if str(o) in still:
                log.error("order %s is STILL working after two cancels -- "
                          "unmanaged exposure, check the venue", o)
        return done

    def _cancel_blocking(self, order_ids: list[str]) -> list[str]:
        params: dict[str, object] = {"walletAddress": self.wallet_address,
                                     "walletId": self.wallet_id}
        for i, oid in enumerate(order_ids):
            params[f"cancelInfoList[{i}].orderId"] = oid
        host = BASE.split("//", 1)[1]
        conn = http.client.HTTPSConnection(host, timeout=10)
        try:
            conn.request("POST", f"{BATCH_CANCEL}?{self._sign(params)}",
                         headers={"X-MBX-APIKEY": self.api_key})
            r = conn.getresponse()
            raw = r.read()
            body = json.loads(raw or b"{}")
            if r.status != 200:
                # -9000 is the venue's answer to cancelling an order that is
                # already terminal, and it is the NORMAL case here: a limit
                # that books at fill_break leaves a 1-2% tail which never
                # fills and expires with the round, so there is nothing left
                # to cancel by the time we ask.
                #
                # Measured 2026-09-13 over the full log: 199 of these, and
                # `cancel_orders` -- which reconciles against the venue's own
                # working-order list -- found a genuinely stuck order ZERO
                # times and failed to read that list ZERO times. Booked stakes
                # and shares also match the venue's record exactly, and an
                # order that read 98% at fill time still reads 98% hours
                # later, so the tail is dead rather than unbooked.
                #
                # It was a WARNING on the reasoning that cancel_orders would
                # escalate a real orphan. That reasoning still holds and those
                # escalations are untouched -- but 199 warnings for a condition
                # that has never once mattered trains you to ignore the log,
                # which costs more than it saves. Any OTHER code stays loud.
                code = body.get("code") if isinstance(body, dict) else None
                (log.debug if code == -9000 else log.warning)(
                    "live POST %s -> HTTP %s: %s", BATCH_CANCEL, r.status, body)
                return []
            return [str(x) for x in (body.get("canceled") or [])]
        except Exception as e:                      # noqa: BLE001 - never raise
            log.error("cancel failed: %s: %s", type(e).__name__, e)
            return []
        finally:
            conn.close()

    async def buy_limit(self, *, slug: str, side: str, token_id: str,
                        limit_px: float, stake: float, wait_s: float,
                        poll_s: float = 0.75,
                        fill_break: float = 0.999) -> Fill | None:
        """Post a resting LIMIT, wait `wait_s`, and cancel whatever is left.

        A limit that does not fill is a MISSED ROUND, not a quiet success, so
        the caller gets None and the round is over. Crossing afterwards would
        pay exactly the spread the limit exists to avoid, so it is deliberately
        not done here -- the A/B has to measure posting, not posting-then-
        paying-anyway.

        The fill price comes from the venue's own record, never from what we
        asked: a resting order can fill BETTER than its limit, and booking the
        requested price would invent a P&L.
        """
        if not self.enabled:
            raise LiveBrokerError("wallet not configured; call discover_wallet() first")
        self.last_failure = None
        self.last_order_error = None
        quote = await self.get_quote(token_id, stake, order_type="LIMIT",
                                     price_limit=limit_px)
        if not quote or not quote.get("quoteId"):
            self.last_failure = "no_quote"
            log.error("no LIMIT quote for %s %s at %.2f", slug, side, limit_px)
            return None
        resp = await self._request("POST", PLACE_ORDER, {
            "walletAddress": self.wallet_address,
            "walletId": self.wallet_id,
            "quoteId": quote["quoteId"],
            # LIMIT must be GTC; FOK belongs to MARKET (schema, verified live).
            "timeInForce": "GTC",
            "accountType": self.account_type,
            "orderType": "LIMIT",
            "priceLimit": f"{limit_px:.2f}",
            "slippageBps": self.slippage_bps,
        })
        if not resp:
            refused = (not self._transport_error
                       and 400 <= self._last_status < 500)
            self.last_failure = "rejected" if refused else "unknown"
            return None
        order_id = str(resp.get("orderId") or resp.get("bundleId") or "")
        if not order_id:
            self.last_failure = "no_order_id"
            return None

        deadline = time.time() + max(0.0, wait_s)
        rec: dict | None = None
        terminal = {"CLOSED", "CANCELLED", "CANCELED", "EXPIRED", "FAILED"}
        while True:
            rec = await self.order_record(order_id, tries=1)
            filled = float((rec or {}).get("fillPercentage") or 0.0)
            # Done at `fill_break`, not at 1.0. Waiting out the clock for the
            # last 1-2% of an already-filled order cost a median 16s on half of
            # 238 measured orders, and at tau < wait_s that delay is the
            # difference between a booked fill and a cancellation at the bell.
            # The remainder is cancelled below, and the fill is booked from the
            # venue's own filled amounts.
            if filled >= fill_break:
                break
            if filled <= 0.0 and str((rec or {}).get("status") or "") in terminal:
                break
            if time.time() >= deadline:
                break
            await asyncio.sleep(poll_s)

        filled = float((rec or {}).get("fillPercentage") or 0.0)
        if filled <= 0.0:
            cancelled = await self.cancel_orders([order_id])
            # ⛔ RE-READ AFTER CANCELLING, ALWAYS. `filled` came from the last
            # POLL; the venue can fill the order in the gap between that poll
            # and the cancel landing. Returning None there books no fill for a
            # position the venue has actually opened -- an untracked holding
            # that never settles, never redeems, and never appears in the
            # ledger or the daily P&L.
            #
            # Trusting `cancelled` is not enough on its own: it only tells us
            # what the CANCEL call reported, and a venue that fills first and
            # then accepts the cancel of a zero remainder looks identical to a
            # clean cancel. The order record is the only authority on whether
            # shares were bought, so ask it.
            #
            # Cheap: this runs only on the no-fill path, which is 10 orders in
            # the tape so far. Under `order_type=limit_take` the race window is
            # small because a crossing order resolves in ~840 ms rather than
            # resting 20 s, but small is not zero.
            post = await self.order_record(order_id, tries=2)
            post_filled = float((post or {}).get("fillPercentage") or 0.0)
            if post_filled > 0.0:
                log.warning(
                    "limit %s %s @%.2f RACED THE CANCEL: polled 0%% filled, "
                    "cancel %s, but the venue now reports %.0f%% filled (%s). "
                    "Booking the fill -- returning None here would leave an "
                    "untracked position.",
                    slug, side, limit_px,
                    "confirmed" if cancelled else "NOT confirmed",
                    post_filled * 100, order_id)
                rec, filled = post, post_filled
            else:
                self.last_failure = "limit_unfilled"
                log.info("limit %s %s @%.2f unfilled after %.0fs -> %s (%s)",
                         slug, side, limit_px, wait_s,
                         "cancelled" if cancelled else "CANCEL NOT CONFIRMED",
                         order_id)
                return None

        usdt = amount_usdt(rec.get("filledUsdtAmount")) or stake * filled
        shares = amount_usdt(rec.get("filledShareQty"))
        px = (usdt / shares) if shares > 0 else limit_px
        # `marketProviderFee` on an order/history row is USDT. It is NOT the
        # same convention as the quote's `feeAmount`, which buy() correctly
        # treats as shares.
        #
        # This line used to multiply by `px`, on a 2026-09-12 reading of one
        # fill (0.111 on a $0.83 fill at px 0.1526) where the share convention
        # happens to land on exactly 2.0%. The venue's own accounting refutes
        # it. Over all 47 of today's FILLED limit orders (2026-09-14):
        #
        #     todayTotalCost        $87.11308
        #     sum(filledUsdtAmount) $85.81000
        #     difference            $ 1.30308   <- the fee the venue charged
        #     summed as USDT        $ 1.27158   error +0.03 (2dp rounding)
        #     summed as shares*px   $ 0.75248   error +0.55
        #
        # Under-booking the fee makes every PnL figure optimistic -- the
        # ledger read -$18.55 against the venue's -$19.87 on the same day.
        fee = amount_usdt(rec.get("marketProviderFee"))
        if filled < 0.999:                    # partial: cancel the remainder
            await self.cancel_orders([order_id])
        log.info("limit %s %s FILLED %.0f%% @%.4f ($%.2f -> %.4f shares, fee $%.4f)",
                 slug, side, filled * 100, px, usdt, shares, fee)
        return Fill(ts_ms=int(time.time() * 1000), side=side, slug=slug,
                    requested_px=limit_px, filled_px=px, shares=shares,
                    stake=usdt, fee_paid=fee, order_id=order_id,
                    # Which token was bought. bind() needs it to persist the fill
                    # and settle() needs it to VERIFY one: an unverified fill is
                    # checked with "is this token in the venue's positions?", and
                    # with token_id None that question is unanswerable -- the
                    # check concludes the venue holds nothing and voids a real
                    # position. The market path always set it; this one did not.
                    token_id=token_id,
                    # JSON, not the dict itself: `fills.raw` is TEXT, and a dict
                    # fails to bind AFTER the order has already filled -- which
                    # is how a real fill went unbooked on 2026-09-11.
                    raw=json.dumps(rec, default=str))

    async def buy(self, *, slug: str, side: str, token_id: str,
                  price: float, stake: float,
                  quote: dict | None = None,
                  max_px: float | None = None) -> Fill | None:
        if not self.enabled:
            raise LiveBrokerError("wallet not configured; call discover_wallet() first")
        self.last_failure = None
        self.last_order_error = None

        # A quote may be supplied by the caller (the pre-trade recheck already
        # fetched one). Using it avoids a second round trip that eats into the
        # quote's ~5s expireAt window -- the dominant cause of FOK failures.
        # ---- QUOTE VIABILITY, BEFORE ANY ORDER EXISTS -----------------------
        # The caller may hand us a pre-fetched (WARM) quote to save a round
        # trip. That saves real time, but a stale one is NOT harmless: the
        # venue anchors the FOK bound to this quote, so a quote that has
        # drifted is an order that cannot fill. Discard it and re-fetch rather
        # than sign a dead order -- see `quote_is_unusable` for the two classes
        # measured live on 2026-09-15/16, and note that 7 of 112 attempts were
        # PROVABLY unfillable, every one carrying slip 10000.
        for _attempt in (0, 1):
            if quote is None:
                quote = await self.get_quote(token_id, stake)
            if not quote or not quote.get("quoteId"):
                self.last_failure = "no_quote"
                log.error("no quote for %s %s stake=%.2f", slug, side, stake)
                return None
            why = quote_is_unusable(
                float(quote.get("averagePrice") or price), max_px)
            if why is None:
                break
            log.warning("%s %s: discarding quote (%s); re-quoting",
                        slug, side, why)
            self.last_failure = why.split(":")[0]
            quote = None
        else:
            return None

        shares_gross = from_wei(quote.get("amountOut"))
        fee_shares = from_wei(quote.get("feeAmount"))
        avg_px = float(quote.get("averagePrice") or price)
        expire_at = int(quote.get("expireAt") or 0)
        if expire_at and expire_at <= time.time() * 1000:
            self.last_failure = "quote_expired"
            log.warning("quote %s already expired; skipping", quote["quoteId"])
            return None
        # PRICE CEILING. A MARKET order prices off the venue's quote, and that
        # quote is pinned to the DISPLAYED price, which lags its own book -- by
        # up to 0.40, and above the ask in 5-7% of samples. Our own record has
        # the failure: on 2026-09-11 a Down offered at 0.17 was bought at 0.84,
        # turning a +47% EV trade into -70%. `slippageBps` cannot catch that; it
        # bounds movement AFTER the quote, not the quote being wrong to begin
        # with.
        #
        # So the caller passes the highest price it is willing to pay -- for the
        # cross arm, the same `ask + buffer` ceiling the LIMIT path used -- and
        # a quote above it is refused before any order exists. This is what
        # makes MARKET safe to use here: it keeps the immediacy and borrows the
        # limit path's one real advantage, a bounded worst case.
        if max_px is not None and avg_px > max_px + 1e-9:
            self.last_failure = "quote_above_ceiling"
            log.warning("quote %.4f for %s %s is above the ceiling %.4f "
                        "(displayed price lags the book); refusing",
                        avg_px, slug, side, max_px)
            return None

        # SLIPPAGE DERIVED FROM THE CEILING, not frozen at a constant.
        #
        # `slippageBps` is the venue's tolerance for the book moving between the
        # quote and the execution. At the default 300 (3%) a quote of 0.3936
        # authorises a fill up to 0.405 -- and this arm triggers precisely when
        # the book is about to gap. Measured live 2026-09-15 07:39:50 on
        # eth-updown-5m-1789457700: sweep_px 0.394, the book at +0.25s was
        # 0.720. The FOK was dead on arrival, and 7 of 8 MARKET orders failed
        # the same way.
        #
        # The fix is not a bigger constant. `max_px` already says exactly how
        # far we are willing to go, so the tolerance is computed from it: the
        # venue then enforces our own ceiling, and the order fills anywhere in
        # between. Too tight and a gapping book is unreachable; too loose and we
        # overpay -- deriving it is neither.
        #
        # Without a ceiling the caller gets the configured default unchanged.
        #
        # The range's top is the VENUE's maximum, not ours: the official
        # OpenAPI schema (slippagebps/ in the repo) types `slippageBps` on
        # BOTH trade/get-quote and trade/place-order-bundle as an integer,
        # "Range 1-10000". 10_000 is therefore the most any client can ask
        # for; under the quote x (1 + slip) bound that tops out an authorised
        # fill at 2x the quote, for everyone. The 2026-09-15 drought windows
        # needed 25_454-203_333bps (3.5x-21x the quote) -- beyond the venue's
        # own maximum, unfillable at any latency by any participant. Within
        # the range the venue enforces OUR ceiling exactly, which is the
        # property this arm wants.
        slip = derived_slip_bps(self.slippage_bps, max_px, avg_px)
        # Log-only, one line per attempt, so the 2026-09-15 loss classes are
        # legible from the log alone: `quote` is the venue's FOK reference
        # (echoed verbatim in the order record's `price`), `bound` is what
        # the FOK will accept (quote x (1 + slip)), and the ack ms is the
        # decision->venue share of the race.
        t_ack = time.monotonic()
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
            "slippageBps": slip,
        })
        log.info("venue ack %s %s: %.0fms, quote %.4f slip %d -> bound %.4f%s",
                 slug, side, (time.monotonic() - t_ack) * 1000.0, avg_px, slip,
                 avg_px * (1 + slip / 10_000.0),
                 "" if max_px is None else f", ceiling {max_px:.3f}")
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
        # This sits inside the order path, where every second spent waiting is a
        # second of the quote's ~5s expireAt window and of the trade's tau
        # window -- so the budget is bounded, but it has to be long enough to
        # mean something. Measured on 2026-09-12: order ...702147 was FILLED by
        # the venue at +0.75s yet order/history did not show it within 2s (4 x
        # 0.5s), so the old budget reported "unreadable" for an order that had
        # already executed, and the position list was still empty 3.5s later.
        # ~3.6s here plus the position patience below covers that latency.
        #
        # RAISED 2026-09-15 via settings.order_status_wait_s. 3.6s was short of
        # the venue's own verdict: order ...549423 on 05:09:13 had a terminalTime
        # ~8.2s after createTime, so the poll gave up, the position list was
        # (correctly) empty, and buy() returned the NON-retryable "unconfirmed".
        # The order died after one attempt for want of a few more seconds, while
        # a sibling whose status did arrive was retried and FILLED. Hearing
        # "FAILED" is what makes a FOK retryable, and a failed FOK costs no
        # money -- only the trade.
        from btcpredict.config import settings as _s
        _budget = getattr(_s, "order_status_wait_s", 0.0) or 3.6
        _delay = 0.6
        # The budget is honoured AS WRITTEN, at `_delay` resolution. This used
        # to be `max(6, ...)`, so the shortest wait anyone could configure was
        # 3.6s no matter what they set.
        status = await self._order_status(
            order_id, tries=max(2, int(_budget / _delay) + 1), delay=_delay)
        if status is None:
            # The history endpoint did not answer in time. It lags the
            # place-order call by a second or two, so this is the COMMON case
            # rather than the rare one -- and it used to mean the fill was
            # booked on assumption. Never do that: a phantom fill corrupts the
            # P&L, the risk guard's exposure and the trade record, and nothing
            # downstream can tell it apart from a real trade.
            # ⛔ AND THE CUT-OFF IS SHORT BY DESIGN, because the venue's verdict
            # is no longer what authorises a retry. Measured on the venue's OWN
            # records (20 orders, 2026-09-22, its createTime -> terminalTime):
            #     FILLED  n=17  min 0.52s  p50 0.97s  p90 1.13s  max 1.65s
            #     FAILED  n=3       5.25s      12.88s      14.01s
            # and order/history said SUBMITTED for the whole of those 12.9s.
            # NOTHING lands between 1.65s and 5.25s. So a 15s budget is spent
            # waiting for a FAILED stamp that was already true, and it blocks
            # the tick while it waits (measured: an 18.64s tick). Instead, pull
            # the order and ask the venue's two STATE lists -- "is anything of
            # ours still working?" and "do we hold a position?" -- and retry on
            # the answer rather than on the stamp.
            await self.cancel_orders([order_id])
            # The position list is the EFFECT of a fill, so ask it instead: a
            # position in this token can only exist if the order went through.
            held = await self._confirm_by_position(token_id)
            if held is False:
                # Absent from a readable position list. That is evidence the
                # order did not land -- but NOT proof, since the list lags a
                # fill. So this is deliberately NOT retryable: re-placing on
                # weak evidence would double the exposure if the first order
                # did fill. Nothing is booked and the round is abandoned; if
                # the position does appear, the round gate sees it and blocks
                # re-entry anyway, and redeem() claims ALL claimable tokens
                # rather than only the ones this bot booked, so winnings are
                # not stranded.
                #
                # SAFETY NET: _confirm_by_position just spent ~4s checking the
                # position list. In case the on-chain execution concluded while
                # that poll was running, check order_record one final time: if
                # the order has now reached a terminal failure state (FAILED,
                # CANCELLED, etc.), classify it as venue_failed so the caller
                # can cleanly retry or skip without a 30s penalty lock.
                # ⛔ WAIT FOR THE VENUE'S VERDICT (BUY_TERMINAL_WAIT_S). The
                # old single re-read here could see SUBMITTED for an order that
                # then FILLED 4 s later (2026-09-24 04:52, filled at 11.1 s).
                rec = await self._await_terminal(order_id, BUY_TERMINAL_WAIT_S)
                if rec:
                    final_st = rec.get("status") or ""
                    if final_st in TERMINAL_FAIL_STATUSES:
                        self.last_failure = "venue_failed"
                        self.last_order_error = str(rec.get("errorMessage") or "")
                        log.error("order %s status=%s confirmed by late history check; "
                                  "treating as failed (%s): %s",
                                  order_id, final_st, slug,
                                  self.last_order_error or "no reason given")
                        return None
                    elif final_st == "FILLED":
                        log.info("order %s confirmed FILLED by late history check (%s)",
                                 order_id, slug)
                        status = "FILLED"

                if status != "FILLED":
                    # ⛔ NO VERDICT = UNKNOWN, NEVER "RETRY". This used to ask
                    # order/list whether the order was still WORKING and retry
                    # when it was absent -- but a MARKET bundle NEVER appears in
                    # order/list, so "absent" was always true. Live 2026-09-24
                    # 04:52: declared dead at 6.8 s, FILLED at 11.1 s, retry
                    # attempted. Only a terminal FAILED (above) may authorise a
                    # retry; anything else abandons the round unbooked, and
                    # redeem() still claims every claimable token.
                    self.last_failure = "unconfirmed"
                    log.error("order %s unverifiable and the venue holds no "
                              "position in %s; NOT booking and NOT retrying%s",
                              order_id, slug,
                              f" (venue said: {self.last_order_error})"
                              if self.last_order_error else "")
                    return None
            if held is True:
                log.info("order %s confirmed by the venue's position list (%s)",
                         order_id, slug)
                status = "FILLED"
            else:
                log.warning("order %s UNCONFIRMED: order history and position "
                            "list both unreadable; booking provisionally and "
                            "letting settle() decide (%s)", order_id, slug)
        elif status != "FILLED":
            # A non-FILLED status is NOT evidence that the venue holds no
            # order, and treating it as such bought the same round twice: this
            # is the SAME order-history endpoint that lags the fill by a second
            # or two in the branch above, so a stale FAILED for an order that
            # had actually filled sent the caller into the retry loop, which
            # placed a second order for a round it was already long. Double
            # exposure, real money.
            # So confirm against the position list -- the EFFECT of a fill --
            # before believing the status. `venue_failed` (and therefore the
            # retry) is only returned when a readable list omits the token.
            held = await self._confirm_by_position(token_id)
            if held is True:
                log.error("order %s reported %s but the venue HOLDS a position "
                          "in %s; booking it and NOT retrying",
                          order_id, status, slug)
                status = "FILLED"
            elif held is False:
                # Confirmed empty: no position, so the caller may safely place
                # a fresh order. FOK market orders land here whenever the book
                # moves between the quote and the execution, which is
                # transient.
                self.last_failure = "venue_failed"
                log.error("order %s status=%s with no position held; treating "
                          "as failed (%s): %s", order_id, status, slug,
                          self.last_order_error or "no reason given")
                return None
            else:
                # Unreadable position list: the status says no fill and the
                # venue may disagree, and re-placing on that doubt is exactly
                # the double buy. Abandon the round instead. redeem() claims
                # ALL claimable tokens rather than only the ones this bot
                # booked, so a position we never booked is not stranded.
                self.last_failure = "unconfirmed"
                log.error("order %s status=%s but the position list is "
                          "unreadable; NOT booking and NOT retrying (%s)",
                          order_id, status, slug)
                return None

        # ⛔ THE POSITION IS `amountOut` -- UNLESS THE VENUE HOLDS FEWER.
        # `amountOut` is already net of the fee (wallet-measured 2026-09-23,
        # order 26092300001909464075: amountOut 1.41632653 == the redeemed
        # position to the 8th dp, `filledShareQty` "1.42" a round-UP of it),
        # so `amountOut - feeAmount` under-credits and must never be booked.
        # BUT `amountOut` is priced AT THE QUOTE, and the FOK may fill up to
        # `slip` worse. Measured from order/history 2026-09-23:
        #   1790200800  quote 0.64 -> filled 0.65   amountOut 6.0215  venue 5.93
        #   1790165400  quote 0.67 -> filled 0.68   amountOut 5.1722  venue 5.10
        #   1790197500  quote 0.67 -> filled 0.67   amountOut 6.0632  venue 6.06
        # One tick of slippage = ~1.5% fewer shares than the quote promised,
        # and the 22:04 exit then asked to sell 6.02 of a 5.93 holding (-9000).
        # The 2026-09-22 22:43 dead exit (3.2442 asked, 3.24 held) is the same.
        # So: a venue figure within its own 2dp rounding (0.005) confirms
        # amountOut; one BELOW that cannot be rounding, and the position is the
        # venue's. The display rounds to nearest, so book its LOWEST consistent
        # value (`_ex - 0.005`): a sale floored from it never asks for more
        # than is held. The fee needs no separate PnL deduction either way.
        _rec = await self.order_record(order_id, tries=2, delay=0.4) \
            if order_id else None
        shares = shares_gross
        _ex = amount_usdt((_rec or {}).get("filledShareQty"))
        if _ex and _ex > 0.0 and _ex < shares - 0.005:
            log.warning("buy %s %s: venue holds %.2f shares, the quote's "
                        "amountOut is %.4f (filled worse than the quote) -- "
                        "booking %.4f", slug, side, _ex, shares, _ex - 0.005)
            shares = _ex - 0.005
        if not _ex:
            log.warning("buy %s %s: no executed quantity readable for order %s "
                        "-- booking the quote's amountOut %.4f", slug, side,
                        order_id, shares)
        # The same for the cash: a fill that SPENT less than the stake
        # (1790194200: $3.99 asked, `filledUsdtAmount` 3.93) books what left
        # the wallet. Only below the 2dp display's own rounding.
        _spent = amount_usdt((_rec or {}).get("filledUsdtAmount"))
        if _spent and 0.0 < _spent < stake - 0.005:
            log.warning("buy %s %s: venue spent $%.2f of the $%.4f stake -- "
                        "booking $%.2f", slug, side, _spent, stake, _spent)
            stake = _spent
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

    async def _sell_quote(self, slug: str, side: str, token_id: str,
                          shares: float,
                          quote: dict | None = None) -> tuple[dict | None, float]:
        """A SELL quote, STEPPING THE SIZE DOWN when the venue refuses.

        ⛔ A REFUSED SELL QUOTE IS USUALLY A SIZE MISMATCH, NOT A DEAD BOOK.
        Positions are whole HUNDREDTHS (`filledShareQty` reads "3.24") while a
        quote's `amountOut` is a float, so an ask a hair above the holding is
        refused with -9000 "You have exceeded your available shares". Measured
        live 2026-09-22 22:43: that refusal killed the reaction exit outright --
        no sale, position to the bell.
        QUOTES ARE READ-ONLY, so a retry places no order and can only cost time;
        selling FEWER shares is always accepted. A short pause also absorbs a
        credit that has not landed yet.
        """
        if quote:
            return quote, shares
        # ⛔ STEP DOWN ALL THE WAY, NOT TO 90%. Operator, 2026-09-23: the sale
        # is critical, so keep asking smaller until the venue quotes one. The
        # old ladder stopped at 0.90 and 21:09 died on it ("lacks enough
        # liquidity" at 6.06 / 5.99 / 5.75, nothing smaller ever asked). A
        # thin book takes a small sale that it refuses whole; the remainder is
        # retried on the next tick by the caller.
        asks: list[float] = []
        for step in SELL_STEP_FRACS:
            ask = shares if step == 1.0 else int(shares * step * 100) / 100.0
            ask = max(ask, 0.01)
            if ask > shares or (asks and ask >= asks[-1]):
                continue
            asks.append(ask)
        for i, ask in enumerate(asks):
            q = await self.get_quote(token_id, ask, side="SELL",
                                     order_type="MARKET")
            if q and q.get("quoteId"):
                if ask != shares:
                    log.warning("sell %s %s: venue refused %.2f shares (status "
                                "%s) -- quoting %.2f instead", slug, side,
                                shares, self._last_status, ask)
                return q, ask
            if i < len(asks) - 1:
                log.warning("sell %s %s: no quote for %.2f shares (status %s) "
                            "-- retrying smaller", slug, side, ask,
                            self._last_status)
                await asyncio.sleep(SELL_STEP_PAUSE_S)
        return None, shares

    async def sell_fak(self, *, slug: str, side: str, token_id: str,
                       shares: float, min_px: float | None = None,
                       slip: float = 0.03,
                       quote: dict | None = None) -> Fill | None:
        """Sell `shares` of an outcome token. Mirrors `PmBroker.sell_fak`.

        THE FIRST SELL PATH ON THIS VENUE. `main.py` has called
        `self.broker.sell_fak` since the cross exit shipped, but this class
        never defined it -- so `_arm_exit_watch`'s `hasattr(self.broker,
        "sell_fak")` was False on Binance and the watcher silently returned,
        with no error and no log line. Every exit rule was inert here for that
        reason alone, whatever its config said.

        UNITS. For a SELL `amountIn` is SHARES in wei, not USDT: the venue's
        own schema states it for the blocktrade endpoint --
            | `SELL` | shares (wei)  | USDT (wei)    |
        -- and `side` is the same BUY/SELL enum on trade/get-quote. Sending
        USDT here would ask to sell ~`price` times too few shares.

        `stake` ON THE RETURNED FILL IS PROCEEDS, NOT AN OUTLAY -- cash coming
        IN. Do NOT hand it to `settle()`: that pays a position out against the
        round's winner and this position no longer exists. The caller books the
        sale itself (see `Bot._cross_exit_execute`).

        ⛔ THE VENUE'S MARKET MINIMUM IS $1 OF NOTIONAL, and this bot
        trades $1-$2 positions. A sale whose proceeds fall under that is
        refused outright, so the common case for THIS account is that a sell
        cannot be placed at all. Checked below and reported as
        `sell_below_venue_minimum` rather than sent and rejected.

        NOT WIRED TO ANY AUTOMATIC RULE. The cross exit that would call it
        ships off with a retracted justification and a 0-for-10 live record,
        and the three exit rules measured on 2026-09-19 (stop, take, late spot)
        all lost to holding. This is execution capability, deliberately inert.
        """
        if not self.enabled:
            raise LiveBrokerError("wallet not configured; call discover_wallet() first")
        self.last_failure = None
        self.last_order_error = None
        self.last_sell_size = 0.0
        shares = float(shares or 0.0)
        # ⛔ FLOOR TO THE VENUE'S OWN STEP -- HUNDREDTHS OF A SHARE. An order
        # record reports `filledShareQty` as "3.24"; asking for the quote's
        # 3.2442222 is refused (-9000, exceeded your available shares) and the
        # exit dies. Selling FEWER is always accepted, so floor and never round
        # up. This is also what makes a percentage exit behave like the venue's
        # own UI: 100% of 3.24 is 3.24, 50% is 1.62.
        _asked = shares
        shares = int(shares * 100) / 100.0
        if shares <= 0.0 or not token_id:
            # Nothing to sell, or a position with no token: either would only
            # produce a venue error naming the wrong cause.
            self.last_failure = "sell_no_shares"
            return None
        if shares != _asked:
            log.info("sell %s %s: flooring %.6f -> %.2f shares (the venue "
                     "steps in 0.01)", slug, side, _asked, shares)

        q, shares = await self._sell_quote(slug, side, token_id, shares, quote)
        if not q or not q.get("quoteId"):
            self.last_failure = "no_sell_quote"
            log.warning("no SELL quote for %s %s (asked %.4f shares, tried down "
                        "to 90%%)", slug, side, shares)
            return None

        # What the venue says this sale is worth. `amountOut` is USDT for a
        # SELL; `averagePrice` is per share. Either alone is enough to decide
        # whether the venue minimum is reachable.
        out_usdt = amount_usdt(q.get("amountOut"))
        avg_px = float(q.get("averagePrice") or 0.0)
        if out_usdt <= 0.0 and avg_px > 0.0:
            out_usdt = avg_px * shares
        if 0.0 < out_usdt < SELL_MIN_NOTIONAL_USDT:
            # The refusal that matters for this account. Reported by name so it
            # is never mistaken for a book or latency problem.
            self.last_failure = "sell_below_venue_minimum"
            log.info("sell %s %s: proceeds $%.4f under the venue's $%.2f "
                     "MARKET minimum (%.4f shares @ %.4f) -- not sent",
                     slug, side, out_usdt, SELL_MIN_NOTIONAL_USDT, shares, avg_px)
            return None

        # A FLOOR ON WHAT WE RECEIVE. `min_px` is the caller's worst acceptable
        # price per share; the venue enforces it through slippageBps against
        # the quote, the same mechanism the buy path uses for its ceiling.
        # Without a floor the caller gets the configured default unchanged.
        slip_bps = int(round(max(1.0, min(10_000.0, slip * 10_000.0))))
        if min_px is not None and avg_px > 0.0:
            give = max(0.0, (avg_px - float(min_px)) / avg_px)
            slip_bps = int(round(max(1.0, min(10_000.0, give * 10_000.0))))

        t_ack = time.monotonic()
        resp = await self._request("POST", PLACE_ORDER, {
            "walletAddress": self.wallet_address,
            "walletId": self.wallet_id,
            "quoteId": q["quoteId"],
            "timeInForce": "FOK",
            "accountType": self.account_type,
            "orderType": "MARKET",
            "slippageBps": slip_bps,
        })
        log.info("venue ack SELL %s %s: %.0fms, quote %.4f slip %d%s",
                 slug, side, (time.monotonic() - t_ack) * 1000.0, avg_px,
                 slip_bps, "" if min_px is None else f", floor {min_px:.3f}")
        if not resp:
            refused = (not self._transport_error
                       and 400 <= self._last_status < 500)
            if refused:
                self.last_failure = "rejected"
                return None
            # A transport error or a 5xx: the order MAY be live. The model exit
            # retries every tick, so a None here would send a second sale over
            # an unbooked first one. PENDING hands it to `confirm_sale`, which
            # finds it in the history by side/size/time (no order id needed).
            self.last_failure = "sell_pending"
            self.last_sell_size = float(shares)
            log.warning("sell %s %s: place-order outcome unknown (status %s) "
                        "-- treated as PENDING, confirmed late", slug, side,
                        self._last_status)
            return Fill(
                slug=slug, side=side,
                requested_px=float(min_px) if min_px is not None else avg_px,
                filled_px=avg_px, shares=0.0, stake=0.0, fee_paid=0.0,
                order_id="", ts_ms=int(time.time() * 1000), raw="",
                token_id=token_id, pending=True,
            )

        # Same discipline as `buy`: HTTP 200 + {orderId} does NOT mean filled.
        # A phantom SALE is worse than a phantom buy -- it books proceeds that
        # never arrived AND releases a position the venue still holds, so the
        # round then settles against a position the ledger thinks is gone.
        order_id = str(resp.get("orderId") or resp.get("bundleId") or "")
        # ⛔⛔ NEVER JUDGE A MARKET ORDER FROM ONE SNAPSHOT TAKEN RIGHT AFTER THE
        # ACK. `order_record` retries only until the order APPEARS, so reading
        # the fill there reads a still-OPENING order -- and this venue's MARKET
        # bundles reach their terminal state 1.2-2.2 s AFTER `createTime`
        # (measured 2026-09-23, see ORDER_TERMINAL_STATUSES).
        # WHAT THAT COST: `order/history` for this wallet shows 5 SELLs ever,
        # **4 FILLED / 1 FAILED**, and the bot booked NONE of the fills -- it
        # called every one of them `fak_unfilled`, left the position in the
        # ledger, and `settle_due` then booked the ROUND's own result in place
        # of the sale. Live 2026-09-23 13:52: the venue sold 5.09 shares at 0.52
        # for $2.60 (its own `realizedPnl` -$0.90) while the ledger recorded a
        # full -$3.50 loss; 13:36 sold $2.66 where the ledger recorded +$1.46;
        # 11:29 sold $5.01 where the ledger recorded +$1.91.
        # So: wait for a terminal status (bounded), book what the venue says
        # filled, and when it has still not said, hand back a PENDING fill --
        # the caller keeps the position watched and closes it through
        # `confirm_sale` rather than inventing a no-fill.
        deadline = time.monotonic() + SELL_CONFIRM_WAIT_S
        rec: dict | None = None
        while order_id:
            rec = await self.order_record(order_id, tries=1)
            if rec is not None:
                st = str(rec.get("status") or "").upper()
                fp = float(rec.get("fillPercentage") or 0.0)
                if st in ORDER_TERMINAL_STATUSES or fp >= 1.0:
                    break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(SELL_CONFIRM_POLL_S)
        status = str((rec or {}).get("status") or "").upper()
        filled = float((rec or {}).get("fillPercentage") or 0.0)
        if rec is None:
            # Unreadable. NOT a no-fill: the model exit now RETRIES a failed
            # sale every tick, so calling this None would send a second sale
            # while the first may have filled unbooked. Say PENDING instead --
            # `confirm_sale` books it if the venue shows it, and only a sale
            # still unseen after `PENDING_SALE_GIVEUP_S` is retried.
            self.last_failure = "sell_pending"
            self.last_sell_size = float(shares)
            log.warning("sell %s %s: order %s status unreadable -- treated as "
                        "PENDING, confirmed late", slug, side, order_id)
            return Fill(
                slug=slug, side=side,
                requested_px=float(min_px) if min_px is not None else avg_px,
                filled_px=avg_px, shares=0.0, stake=0.0, fee_paid=0.0,
                order_id=order_id, ts_ms=int(time.time() * 1000),
                raw=str(resp)[:2000], token_id=token_id, pending=True,
            )
        sold = amount_usdt(rec.get("filledShareQty")) or (float(shares) * filled)
        if sold <= 0.0 and filled <= 0.0:
            if status not in ORDER_DEAD_STATUSES:
                # NOT DEAD, SO NOT A NO-FILL -- and this is the branch the whole
                # fix turns on. Either the order is still working at the
                # deadline, or it is a status we do not model (or no status at
                # all). 141 ms separated the 13:52 order from this branch and
                # its fill. Say PENDING: the caller keeps the position watched
                # and closes it through `confirm_sale` on a later tick. Only a
                # status that MEANS dead is allowed to retire a position.
                self.last_failure = "sell_pending"
                self.last_sell_size = float(shares)
                log.info("sell %s %s: order %s is still '%s' after %.1fs -- NOT "
                         "a no-fill; the position stays watched and the sale "
                         "is confirmed late", slug, side, order_id,
                         status or "unknown", SELL_CONFIRM_WAIT_S)
                return Fill(
                    slug=slug, side=side,
                    requested_px=float(min_px) if min_px is not None else avg_px,
                    filled_px=avg_px, shares=0.0, stake=0.0, fee_paid=0.0,
                    order_id=order_id, ts_ms=int(time.time() * 1000),
                    raw=str(resp)[:2000], token_id=token_id, pending=True,
                )
            self.last_failure = "fak_unfilled"
            log.info("sell %s %s: order %s terminal '%s' with NO fill -- the "
                     "venue matched nothing (quote %.4f)", slug, side, order_id,
                     status, avg_px)
            return None

        fee = amount_usdt(rec.get("marketProviderFee"))
        net = amount_usdt(rec.get("filledUsdtAmount"))
        if net <= 0.0 and sold > 0:
            # Position gone, cash unreadable: booking $0 proceeds would leave a
            # hole in the P&L that nothing ever revisits. Price it at the fill
            # and say so.
            net = sold * avg_px
            log.warning("sell %s %s: no readable filledUsdtAmount for %s -- "
                        "booked %.4f shares at the %.4f fill ($%.4f)",
                        slug, side, order_id, sold, avg_px, net)
        # ⛔ GROSS, NOT THE VENUE'S NET. `filledUsdtAmount` is what LANDED
        # (gross - fee: 5.09 sh @0.52 = $2.6468 gross -> $2.60 in the record),
        # and `_cross_exit_execute` books `proceeds = sold*px - fee_paid` --
        # handing back the net as the price charges the fee TWICE. This is why
        # `PmBroker.sell_fak` books `stake = sold * px`, gross, and it is the
        # convention every settled row in this ledger already follows.
        gross = net + fee
        px = (gross / sold) if sold > 0 else avg_px
        self.last_sell_size = float(sold)
        log.info("SELL %s %s %s (%.4f shares, $%.4f net + $%.4f fee -> "
                 "%.4f/share)", slug, side, status or "FILLED", sold, net, fee,
                 px)
        return Fill(
            slug=slug, side=side,
            requested_px=float(min_px) if min_px is not None else px,
            filled_px=px, shares=sold,
            # PROCEEDS (GROSS of the fee), not an outlay. See the docstring.
            stake=gross, fee_paid=fee,
            order_id=order_id or str(q["quoteId"]),
            ts_ms=int(time.time() * 1000), raw=str(resp)[:2000],
            token_id=token_id,
        )

    async def confirm_sale(self, *, token_id: str, shares: float,
                           since_ts: float,
                           fallback_px: float = 0.0) -> tuple | None:
        """Did a SELL we placed land? `(sold, px, fee)` or None (ask again).

        THE OTHER HALF OF THE PENDING PATH, AND THE VENUE NEEDS BOTH. On a
        MARKET bundle the fill is not visible at the moment `sell_fak` returns
        (1.2-2.2 s to the terminal state), so it returns a `pending` Fill and
        `_cross_exit_execute` keeps the position watched. `_confirm_pending_sale`
        then calls THIS method on later ticks -- and without it the caller does
        `fn is None -> w.pop("pending") -> stop asking`, which retires the watch
        with the position still on the ledger and hands the round's own result
        to `settle_due`. That is precisely how a placed-and-FILLED sale ended up
        booked as a full loss (2026-09-23 13:52).

        MATCHING, and why it is strict. `since_ts` is when we placed the sale;
        the history also carries the OPERATOR'S OWN manual trades, so a loose
        match can book someone else's order. Requirements: side SELL, a positive
        fill, terminal time not before we placed it (5 s of clock slack), and a
        size no LARGER than what we sent (a stepped-down fill is fine, a bigger
        one cannot be ours). The most recent match wins.
        """
        resp = await self._request("GET", ORDER_HISTORY, {
            "walletAddress": self.wallet_address, "pageSize": 50, "pageNo": 1})
        best: tuple | None = None
        for o in ((resp or {}).get("orders") or []):
            if str(o.get("side") or "").upper() != "SELL":
                continue
            sold = amount_usdt(o.get("filledShareQty")) or 0.0
            if sold <= 0.0:
                continue
            ts = float(o.get("terminalTime") or o.get("createTime") or 0) / 1000.0
            if since_ts and ts + 5.0 < float(since_ts):
                continue
            if shares and sold > float(shares) + 0.02:
                continue                      # bigger than we sent: not ours
            if best is None or ts > best[0]:
                proceeds = amount_usdt(o.get("filledUsdtAmount"))
                best = (ts, sold, proceeds, amount_usdt(o.get("marketProviderFee")))
        if best is None:
            return None
        _, sold, proceeds, fee = best
        px = (proceeds / sold) if (sold > 0 and proceeds > 0) else \
            float(fallback_px or 0.0)
        log.info("confirm_sale: the venue shows %.4f shares sold @%.4f "
                 "(proceeds $%.4f, fee $%.4f)", sold, px, proceeds, fee)
        return (sold, px, fee)

    async def sell_limit(self, *, slug: str, side: str, token_id: str,
                         shares: float, limit_px: float | None = None,
                         wait_s: float = 20.0, poll_s: float = 0.75,
                         fill_break: float = 0.999) -> Fill | None:
        """Post a resting LIMIT SELL, wait `wait_s`, cancel the remainder.

        WHY THIS EXISTS. `sell_fak` is a MARKET order and the venue's MARKET
        minimum is $1 of notional (corrected 2026-09-23; it was believed to be
        ~1.5), so a position worth less than that
        cannot be sold through it AT ALL. That is a property of the ORDER TYPE,
        not a wall: LIMIT carries no such minimum (verified against the venue's
        schema and live -- see the `order_type` note in config.py). MEASURED
        2026-09-22 13:42:01: the reaction exit was refused on a 2.6831-share
        position because its 25% slice came to $0.3556, and with `sell_fak` as
        the ONLY sell method there was no way to place it. `react` exits booked:
        0, all-time.

        ⛔ UNITS: `amountIn` FOR A SELL IS SHARES IN WEI, NOT USDT. `get_quote`
        is the same enum as `place-order-bundle`, so sending USDT here would ask
        to sell ~`price` times too few shares. `sell_fak` carries the same
        warning; this method passes `shares` and `side="SELL"` is what makes the
        venue read it that way.

        ⛔ `stake` ON THE RETURNED FILL IS PROCEEDS -- CASH COMING IN -- exactly
        as in `sell_fak`. The caller books the sale itself; do NOT hand it to
        `settle()`, which would pay the position out against the round's winner.

        IT MAY NOT FILL, AND THAT IS THE TRADE-OFF BEING TESTED: a resting order
        needs the market to come to it. None means a MISSED EXIT **OR AN
        UNKNOWN ONE** -- the position is still held in the first case, while the
        second (`last_failure == "sell_unconfirmed"`) means the venue never said
        and the caller must NOT treat it as safe to sell again. The caller must
        read `last_failure` before retrying; a retry after a real fill is a
        naked short.

        THE PRICE IS NOT THE CALLER'S TO CHOOSE. `_cross_exit_execute` is
        explicit that the arm must not read its own book to price an exit, so
        when `limit_px` is None the price is taken from the VENUE's own SELL
        quote (`averagePrice`) and floored to the venue's 2-decimal tick: at or
        just inside the quoted price, never resting above the market, giving up
        at most one tick to be filled. That costs one extra quote round trip
        (a MARKET quote to learn the price, then the LIMIT quote, because the
        venue requires `priceLimit` on a LIMIT quote) -- paid only on this
        fallback path.
        """
        if not self.enabled:
            raise LiveBrokerError("wallet not configured; call discover_wallet() first")
        self.last_failure = None
        self.last_order_error = None
        self.last_sell_size = 0.0
        shares = float(shares or 0.0)
        # FLOORED FOR THE SAME REASON AS `sell_fak` -- see the note there. An
        # order history record reports the executed quantity as "3.24", so a
        # position booked by an older fill (3.2442222) and asked for verbatim is
        # refused -9000. Applies to the reference MARKET quote below and to the
        # LIMIT quote alike, hence once, here.
        _asked = shares
        shares = int(shares * 100) / 100.0
        if shares <= 0.0 or not token_id:
            # Nothing to sell, or a position with no token: either would only
            # produce a venue error naming the wrong cause.
            self.last_failure = "sell_no_shares"
            return None
        if shares != _asked:
            log.info("sell limit %s %s: flooring %.6f -> %.2f shares (the "
                     "venue steps in 0.01)", slug, side, _asked, shares)

        if limit_px is None:
            mk = await self.get_quote(token_id, shares, side="SELL",
                                      order_type="MARKET")
            if not mk or not mk.get("quoteId"):
                self.last_failure = "no_sell_quote"
                log.warning("no SELL quote for %s %s (%.4f shares) -- cannot "
                            "price the limit", slug, side, shares)
                return None
            avg = float(mk.get("averagePrice") or 0.0)
            if not (0.0 < avg < 1.0):
                self.last_failure = "sell_price_missing"
                log.warning("sell limit %s %s: venue quote %.4f is unusable",
                            slug, side, avg)
                return None
            limit_px = max(0.01, int(avg * 100) / 100.0)
        limit_px = float(limit_px)
        if not (0.0 < limit_px < 1.0):
            self.last_failure = "sell_price_missing"
            return None

        # NO MIN-NOTIONAL CHECK HERE, DELIBERATELY: that rule is MARKET-only and
        # this path exists precisely to be placeable under it.
        #
        # ⛔ `last_sell_size` IS NOT SET HERE, and that is deliberate: it means
        # "what we actually SOLD", the same as in `sell_fak`. It used to report
        # the size INTENDED before any order existed, so every refusal (a
        # rejected placement, an unreadable status) left a nonzero "we sold
        # this much" behind -- and main.py reads that field to size a pending
        # sale's confirmation (`sent = getattr(self.broker, "last_sell_size")`).
        quote = await self.get_quote(token_id, shares, side="SELL",
                                     order_type="LIMIT",
                                     price_limit=limit_px)
        if not quote or not quote.get("quoteId"):
            self.last_failure = "no_sell_quote"
            log.warning("no SELL LIMIT quote for %s %s (%.4f shares @ %.4f)",
                        slug, side, shares, limit_px)
            return None
        resp = await self._request("POST", PLACE_ORDER, {
            "walletAddress": self.wallet_address,
            "walletId": self.wallet_id,
            "quoteId": quote["quoteId"],
            # LIMIT must be GTC; FOK belongs to MARKET (schema, verified live).
            "timeInForce": "GTC",
            "accountType": self.account_type,
            "orderType": "LIMIT",
            "priceLimit": f"{limit_px:.2f}",
            "slippageBps": self.slippage_bps,
        })
        if not resp:
            refused = (not self._transport_error
                       and 400 <= self._last_status < 500)
            self.last_failure = "rejected" if refused else "unknown"
            return None
        order_id = str(resp.get("orderId") or resp.get("bundleId") or "")
        if not order_id:
            self.last_failure = "no_order_id"
            return None

        deadline = time.time() + max(0.0, wait_s)
        rec: dict | None = None
        terminal = {"CLOSED", "CANCELLED", "CANCELED", "EXPIRED", "FAILED"}
        while True:
            rec = await self.order_record(order_id, tries=1)
            filled = float((rec or {}).get("fillPercentage") or 0.0)
            if filled >= fill_break:
                break
            if filled <= 0.0 and str((rec or {}).get("status") or "") in terminal:
                break
            if time.time() >= deadline:
                break
            await asyncio.sleep(poll_s)

        filled = float((rec or {}).get("fillPercentage") or 0.0)
        if rec is None:
            # ⛔ THE VENUE DID NOT SAY, and that is NOT the same as "it did not
            # fill". Mirrors `sell_fak`, which reports `sell_unconfirmed` here
            # in the same words: a phantom SALE books proceeds that never
            # arrived AND releases a position the venue still holds, so the
            # round then settles against shares the ledger thinks are gone.
            # THIS USED TO REPORT `limit_unfilled`, which asserts the opposite of
            # what we know -- and the caller's miss branch RETRIES next tick
            # (main.py: "sell did not fill ... retrying next tick"), so a retry
            # after a real fill is a naked short. The cancel is best-effort:
            # leaving an untracked GTC order resting until the bell is worse.
            await self.cancel_orders([order_id])
            self.last_failure = "sell_unconfirmed"
            log.warning("sell limit %s %s @%.4f: order %s status UNREADABLE -- "
                        "cancelled; UNKNOWN, not a confirmed miss",
                        slug, side, limit_px, order_id)
            return None
        if filled <= 0.0:
            cancelled = await self.cancel_orders([order_id])
            # ⛔ RE-READ AFTER CANCELLING, ALWAYS -- the same race `buy_limit`
            # documents at length. The venue can fill between the last poll and
            # the cancel landing, and it is WORSE on a sell than on a buy:
            # returning None there tells the caller it still owns a position the
            # venue has already paid for, so the round then settles against
            # shares that no longer exist.
            post = await self.order_record(order_id, tries=2)
            post_filled = float((post or {}).get("fillPercentage") or 0.0)
            if post_filled <= 0.0:
                self.last_failure = "limit_unfilled"
                log.info("sell limit %s %s @%.4f unfilled after %.0fs -> %s (%s)",
                         slug, side, limit_px, wait_s,
                         "cancelled" if cancelled else "CANCEL NOT CONFIRMED",
                         order_id)
                return None
            log.warning("sell limit %s %s @%.4f RACED THE CANCEL: polled 0%% "
                        "filled, but the venue now reports %.0f%% (%s) -- "
                        "booking it", slug, side, limit_px, post_filled * 100,
                        order_id)
            rec, filled = post, post_filled

        # FOR A SELL THESE ROLES ARE INVERTED from the buy path:
        # `filledShareQty` is what we GAVE UP and `filledUsdtAmount` is the cash
        # we RECEIVED. Parsed separately for that reason, not reused from
        # `buy_limit`'s arithmetic.
        if filled < 0.999:                  # partial: cancel the remainder
            cancelled = await self.cancel_orders([order_id])
            # ⛔ RE-READ AFTER CANCELLING A PARTIAL TOO, not only a zero. The
            # same race the no-fill path documents runs here: the remainder can
            # fill between the last poll and the cancel landing. Booking only
            # the polled part under-states the proceeds AND tells the caller it
            # still holds shares the venue has already paid for.
            # (buy_limit carries the same gap on its own partial path --
            # reported, not changed: that is the live ENTRY path and it needs
            # its own verification.)
            post = await self.order_record(order_id, tries=2)
            post_filled = float((post or {}).get("fillPercentage") or 0.0)
            if post_filled > filled:
                log.warning(
                    "sell limit %s %s @%.4f: remainder FILLED WITH THE CANCEL "
                    "(%.0f%% -> %.0f%%, cancel %s) -- booking the venue's "
                    "number, not the polled one", slug, side, limit_px,
                    filled * 100, post_filled * 100,
                    "confirmed" if cancelled else "NOT confirmed")
                rec, filled = post, post_filled
        sold = amount_usdt(rec.get("filledShareQty"))
        proceeds = amount_usdt(rec.get("filledUsdtAmount"))
        px = (proceeds / sold) if sold > 0 else limit_px
        fee = amount_usdt(rec.get("marketProviderFee"))
        self.last_sell_size = float(sold)
        log.info("sell limit %s %s FILLED %.0f%% @%.4f (%.4f shares -> $%.4f, "
                 "fee $%.4f)", slug, side, filled * 100, px, sold, proceeds, fee)
        return Fill(
            slug=slug, side=side, requested_px=limit_px, filled_px=px,
            shares=sold,
            # PROCEEDS, not an outlay. See the docstring.
            stake=proceeds, fee_paid=fee, order_id=order_id,
            ts_ms=int(time.time() * 1000), raw=json.dumps(rec, default=str),
            token_id=token_id,
        )

    async def _confirm_by_position(self, token_id: str, tries: int = 6,
                                   delay: float = 0.8) -> bool | None:
        """Does the venue hold `token_id`? True / False / None (unreadable).

        The tie-breaker for an order whose status could not be read. Polled
        briefly, because a position takes a moment to appear after a FOK fill --
        and on 2026-09-12 it took longer than the 1.5s this used to allow, so a
        real fill was declared absent and thrown away. Absence is only believed
        after ~4s of asking, which still fits inside an impulse round's tau.

        False is only returned when the list was actually read and did not
        contain the token -- a list we could never read proves nothing, so it
        returns None rather than an absence (the None/empty distinction in
        _position_token_ids is load-bearing).
        """
        held: set[str] | None = None
        for i in range(tries):
            held = await self._position_token_ids()
            if held is not None and token_id in held:
                return True
            if i < tries - 1:
                await asyncio.sleep(delay)
        return False if held is not None else None

    async def settle(self, fill: Fill, winner: str) -> Settlement | None:
        if getattr(fill, "unverified", False):
            # The venue returns 200 + orderId even when a FOK order fails, so a
            # fill booked without a confirmed status may be phantom. By
            # settlement time the order-history has caught up: FILLED proves the
            # position, a terminal failure proves there never was one.
            status = await self._order_status(fill.order_id)
            if status is None:
                fill.unverified_checks += 1
                # "Cannot read" is NOT the same as "empty". Only positive
                # evidence may void a fill: a failed read that was treated as
                # absence would silently drop a real position -- and its
                # winnings would never be redeemed either. So ask the venue what
                # it actually holds, and keep asking while the answer is unknown.
                held = await self._position_token_ids()
                if held is not None:
                    if fill.token_id in held:
                        status = "FILLED"
                    elif fill.unverified_checks >= 20:
                        status = "FAILED"
                        log.error("unverified order %s absent from the venue's "
                                  "positions after %d checks; voiding fill for %s",
                                  fill.order_id, fill.unverified_checks, fill.slug)
                if status is None:
                    # Unknown outcome: retry forever rather than guess. The
                    # round slot stays closed (fail closed, never fail open).
                    if fill.unverified_checks % 10 == 0:
                        log.error("unverified order %s still unverifiable after "
                                  "%d checks; holding %s un-settled (no PnL "
                                  "booked, no new rounds) until the venue answers",
                                  fill.order_id, fill.unverified_checks, fill.slug)
                    return None
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

    async def _position_token_ids(self) -> set[str] | None:
        """Token ids the wallet currently holds, or None when unreadable.

        The None/empty distinction is load-bearing: callers must never read a
        failed request as "the venue holds nothing".
        """
        resp = await self._request("GET", POSITION_LIST,
                                   {"walletAddress": self.wallet_address})
        if resp is None:
            return None
        return {p.get("tokenId") for p in (resp.get("positions") or [])
                if p.get("tokenId")}

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
