# Why does this LIMIT order fill ~14 cents worse than the live book?

I run a trading bot on **Binance's W3W prediction market** (binary
outcome tokens, price in (0,1), pays 1 if the outcome happens). I have a
concrete execution discrepancy I cannot explain. Four hypotheses have been
tested and falsified by measurement. I want a fifth, or a reason one of mine
was wrongly rejected.

Please reason from the API mechanics and the numbers below. Do not restate my
hypotheses back to me — they are already ruled out. Be concrete and cite the
specific field or code line your explanation depends on.

## The market

5-minute binary rounds on BTC. Each round has an `Up` token and a `Down`
token. A round has a strike (BTC price at round open); at the bell, the side
that is in-the-money pays 1 and the other pays 0. The two order books mirror
exactly: `down_ask = 1 - up_bid`, verified live including sizes.

## The strategy (only for context)

When BTC spot crosses the strike, buy the side it crossed into, 10–60s before
the bell. Order is a marketable LIMIT with `priceLimit = 0.90` (a ceiling, not
a target). Expectation: fill at the current book ask, which is well below 0.90.

## The observation

Three live fills. `decided` is the best ask read from the venue's **websocket
order book feed** at the moment of the trade decision; `filled` is the price
derived from the venue's own order record afterwards.

| time | side | decided | filled | gap | elapsed |
|------|------|---------|--------|-----|---------|
| 14:44:17 | Down | 0.600 | 0.6977 | +0.098 | 2.8s |
| 16:09:41 | Up   | 0.350 | 0.6562 | +0.306 | 3.6s |
| 18:19:37 | Down | 0.760 | 0.9008 | +0.141 | 4.3s |

The third is the cleanest case: the websocket confirmed the book had **not
moved at all** in the moment before the decision (delta vs the previous poll
was exactly 0.000), the feed was verified on the correct `marketId`, and the
order still filled at 0.9008 — which is **exactly the 0.90 priceLimit**.

## What I have ruled out, and how

1. **Stale price at decision time.** Originally the bot read the book from a
   ~1s REST poll. I added the venue's websocket order book (median 201ms
   between updates, p90 272ms, zero gaps >1s, venue→me latency median 89ms).
   The gap persisted unchanged. The third fill above had a *confirmed-static*
   book at decision time.

2. **Order latency (round trips).** `get_quote` measures **43ms**
   round-trip, measured repeatedly. The order path is at most 3 HTTP calls,
   so ~130ms. That cannot produce a 4-second delay or a 14-cent move.

3. **Sweeping the order book.** I measured the volume-weighted price of a
   $1–$5 order across **19,452 real depth snapshots** (5 levels per side):
   average premium over the touch is **+0.0004**, and **0%** of books could
   not absorb the order. Caveat I acknowledge: this sample is
   *unconditional* — it is not specifically sampled during a cross event,
   when the book may be much thinner. I cannot currently rule this out for
   cross moments specifically.

4. **The venue always fills LIMIT at the priceLimit.** Refuted by fills 1 and
   2: both had `priceLimit = 0.90` and filled at 0.6977 and 0.6562.

## The API mechanics

Placing an order is two calls. First `POST /sapi/v1/w3w/wallet/prediction/trade/quote`:

Request: `walletAddress, tokenId, side=BUY, amountIn (wei), orderType=LIMIT, slippageBps=300, priceLimit=0.90`

Response for the third trade (market price was ~0.74 at the time):

```
quoteId        = 9c6046d3daac448581107ff0ea205991
chance         = 0.735
side           = BUY
amountIn       = 999000000000000000      # $0.999
amountOut      = 1110000000000000000     # 1.11 shares
minAmountOut   = False
feeAmount      = 0
averagePrice   = 0.74
lastPrice      = 0.74
minReceive     = 1110000000000000000     # 1.11 shares
timestamp      = 1789323860160
expireAt       = 1789323863160           # timestamp + 3000ms
priceLimit     = 0.90
slippageBps    = 300
feeRateBps     = 0
```

**Note:** `amountIn / amountOut = 0.999 / 1.11 = 0.90` — the quote sizes the
output at the **priceLimit**, not at `averagePrice` (0.74). `minReceive`
equals `amountOut`, i.e. the guaranteed minimum equals the quoted amount.

Then `POST .../trade/order`:

```
walletAddress, walletId, quoteId, timeInForce=GTC, accountType,
orderType=LIMIT, priceLimit="0.90", slippageBps=300
```

Note the order call does **not** re-send `amountIn` — the size comes from the
`quoteId`.

Then I poll `order_record(orderId)` until `fillPercentage >= 0.95`.

## How the fill price is computed (my code)

```python
usdt   = amount_usdt(rec.get("filledUsdtAmount")) or stake * filled
shares = amount_usdt(rec.get("filledShareQty"))
px     = (usdt / shares) if shares > 0 else limit_px
fee    = amount_usdt(rec.get("marketProviderFee")) * px
```

For the third trade the record gave `$1.09 → 1.2100 shares`, so
`px = 1.09 / 1.21 = 0.9008`, and reported `fee = $0.0000`.

Note `1.09 / 0.90 = 1.2111`, i.e. **shares received = stake / priceLimit
exactly**. Also note the fee is documented in my code as denominated in
**shares**, not USDT, and is multiplied by `px` to convert.

## The order-placement loop

```python
quote = await self.get_quote(token_id, stake, order_type="LIMIT",
                             price_limit=limit_px)
resp = await self._request("POST", PLACE_ORDER, {
    "walletAddress": self.wallet_address, "walletId": self.wallet_id,
    "quoteId": quote["quoteId"], "timeInForce": "GTC",
    "accountType": self.account_type, "orderType": "LIMIT",
    "priceLimit": f"{limit_px:.2f}", "slippageBps": self.slippage_bps,
})
order_id = str(resp.get("orderId") or resp.get("bundleId") or "")

deadline = time.time() + wait_s          # wait_s = min(20, tau-1)
terminal = {"CLOSED","CANCELLED","CANCELED","EXPIRED","FAILED"}
while True:
    rec = await self.order_record(order_id, tries=1)
    filled = float((rec or {}).get("fillPercentage") or 0.0)
    if filled >= 0.95: break
    if filled <= 0.0 and str((rec or {}).get("status") or "") in terminal: break
    if time.time() >= deadline: break
    await asyncio.sleep(0.75)
```

Separately, the calling layer fetches **another** quote before this one
(`get_quote(token, stake)` with default `orderType=MARKET`) for a pre-trade
sanity check. So a single order involves: MARKET quote → LIMIT quote → POST
order → poll. That pre-trade quote's result is not used to place the order.

## Questions

1. What mechanism makes a marketable LIMIT at 0.90 fill at exactly
   `stake / 0.90` shares when the book is at 0.76, while other orders with
   the same `priceLimit` fill at 0.6977 and 0.6562? What distinguishes them?

2. Is `amountOut` being computed at `priceLimit` rather than `averagePrice`
   the actual binding constraint — i.e. does the `quoteId` lock in a share
   count computed at the worst-case price, so the fill can never be better
   than the limit regardless of the book? If so, why did fills 1 and 2 beat
   their limit?

3. Does `slippageBps = 300` interact with `priceLimit` in a way that changes
   the execution price rather than just bounding it?

4. Could `filledUsdtAmount` include fees, inflating my derived
   `px = usdt / shares`? The record reported `fee = $0.0000`, which is itself
   suspicious given the venue advertises 200bps.

5. Is the 3-second `expireAt` relevant — e.g. does an order placed against a
   quote that is near expiry get re-priced at the limit?

6. What single API field or test would most cheaply discriminate between the
   remaining explanations? I can make read-only calls freely and place small
   real orders (~$1) if needed.

## Constraints on your answer

- I cannot see the venue's matching engine; only these API responses.
- Please distinguish clearly between what the fields *prove* and what you are
  inferring.
- If you think one of my four falsifications is wrong, say which and what
  measurement would show it.
