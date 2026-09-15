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
        """Cash sitting in the wallet. NOT equity -- see `equity()`."""
        resp = await self._request("GET", POSITION_LIST,
                                   {"walletAddress": self.wallet_address})
        if not resp:
            return 0.0
        return float((resp.get("summary") or {}).get("walletBalance") or 0.0)

    async def equity(self) -> float:
        """Total account value: cash + open positions + unredeemed winnings.

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
        terminal_fail = {"FAILED", "CANCELLED", "CANCELED", "EXPIRED",
                         "REJECTED"}
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
                        if status == "FILLED" or status in terminal_fail:
                            if status in terminal_fail:
                                log.error("order %s %s: %s", order_id, status,
                                          err or "(venue gave no reason)")
                            return status
                        # SUBMITTED etc.: still in flight, retry below.
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

    async def active_orders(self) -> list[dict]:
        """Orders still working (OPENING, partly filled) for this wallet."""
        resp = await self._request("GET", ACTIVE_ORDERS, {
            "walletAddress": self.wallet_address, "limit": 50,
        })
        return (resp or {}).get("orders") or []

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
        status = await self._order_status(order_id,
                                          tries=max(6, int(_budget / 0.6)),
                                          delay=0.6)
        if status is None:
            # The history endpoint did not answer in time. It lags the
            # place-order call by a second or two, so this is the COMMON case
            # rather than the rare one -- and it used to mean the fill was
            # booked on assumption. Never do that: a phantom fill corrupts the
            # P&L, the risk guard's exposure and the trade record, and nothing
            # downstream can tell it apart from a real trade.
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
