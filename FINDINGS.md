# Market reconnaissance — measured facts

All figures below were measured against the live market, not assumed.

## Endpoints (public, unauthenticated, undocumented)

Base: `https://www.binance.com/bapi/defi/v1/public/wallet-direct/prediction`

| Endpoint | Body | Notes |
|---|---|---|
| `POST /market/list` | `{}` or `{"eventSlug":"btc-up-or-down-5m"}` | returns only the *live* round of a series |
| `POST /market/detail` | `{"marketTopicId": N}` | works for **any** topic id, settled or not |

`/market/orderbook`, `/market/depth`, `/market/history` → 404. `api.predict.fun/v1` → 401.
There is **no public order book**; `outcomes[].price` is a last-trade/mid, not an executable quote.

## Round structure

- Vendor `PREDICT_FUN`, BSC chain 56, collateral USDT, `feeRateBps: 200` (**2%**).
- `slug` = `btc-updown-5m-<unix_start>`; `startDate`/`endDate` exactly 300 000 ms apart.
- `variantData.startPrice` = strike (oracle at round start).
- `variantData.endPrice` = oracle at round end — **populated after settlement**.
- `outcomes[].winner` = resolved outcome. Rule is `end > strike → Up`.
- Liquidity ~$25–37k mid-round, falling to ~$11.7k in the final ~15 s.

## Settlement oracle — RESOLVED

`priceFeedProvider: CHAINLINK`, `priceFeedId: 0x0003ef42…9d37` (the `0x0003` prefix is a
Chainlink **Data Streams** feed id, not a classic on-chain aggregator).

Reading Data Streams live needs credentials we do not have — but we do not need it:

1. **The oracle chain is exact.** Round N's `end_price` equals round N+1's `strike` to
   0.000000 across every consecutive pair tested. The settled rounds therefore reconstruct
   the true settlement price series at 5-minute granularity, for free.
2. **The oracle tracks Binance spot to ±$0.005.** Across 20 observations, 18 matched the
   corresponding Binance 5m kline open/close to half a cent; 2 differed by $1.145. Against a
   5-minute sigma of ~$52 that basis is negligible, and direction agreement was 10/10.

**Consequence:** Binance klines are a faithful substrate for historical backtesting, and
Binance spot is the right live intra-round input. The basis is still recorded on every poll
so this assumption stays continuously testable rather than trusted.

## Price dynamics (1,199 rounds reconstructed from 1m klines)

Digital-option fair value `P(up) = Φ(ln(S/K) / σ√τ)` is well calibrated:

| Time into round | Brier | vs coinflip (0.25) |
|---|---|---|
| 1 min | 0.226 | −10% |
| 2 min | 0.199 | −20% |
| 3 min | 0.168 | −33% |
| 4 min | **0.134** | **−46%** |

At round open, direction is a coinflip: 483 up / 513 down over 1000 bars, sign
autocorrelation −0.024, return autocorrelation −0.0685 (slight mean reversion).
5-minute sigma ≈ 0.0665% ≈ $52 on a $78k price.

## Arena strategy models, ported and measured

The strategy bots from polymarket-bot-arena were ported into `btcpredict/models/`
as pluggable pricing models and scored with `tools/backtest_models.py` over 10
days of Binance 1m klines (2,879 rounds × 4 decision marks), same methodology
as the digital-model validation:

| Model | Emit rate | Brier (all marks) | Skill vs coinflip |
|---|---|---|---|
| `digital` (baseline) | 100% | 0.169 | +32% |
| `momentum` | 4% | 0.151 | +40% |
| `mean_rev` | 25% | 0.565 | −126% |
| `sentiment` (taker flow) | 78% | 0.370 | −48% |
| `hybrid` | 6% | 0.415 | −66% |
| `phantom` | 2% | 0.154 | +39% |
| `sniper` | 35% | 0.214 | +14% |
| `btc_maker` | 100% | 0.248 | +1% |

1. **None of the direction-prediction strategies beats the digital model.**
   Momentum/phantom edge it on raw Brier but only by trading 2–4% of rounds,
   and their skill is concentrated in the same late-window information the
   digital model already prices. Combined Brier on the full sample is what the
   trade rule feeds on, and digital wins there.
2. **Mean reversion is the worst model by far** (Brier 0.565, 31% accuracy
   against a 49% base). This is consistent with the arena's own data, where
   the mean-rev bot was repeatedly nerfed and eventually made nearly neutral.
3. **The sentiment port is honest but not predictive**: taker order flow on
   1-minute bars predicts nothing about the next 5-minute close.
4. **The maker bots do not predict direction.** `btc_maker`'s score is a
   coinflip by construction — its edge is the spread, which this venue does
   not pay us. `fee_zone_maker` is Polymarket-specific and has no analogue
   here, so it was not ported.
5. **Sniper's selectivity is real but its edge is not independent**: it trades
   35% of rounds at +14% skill, mostly mirroring digital's late-window
   certainty with stricter confirmation.

**Conclusion:** keep `digital` as the default. The ports are retained behind
`pricing_model` so any of them can be run in paper mode against live quotes,
where a model that underperforms on Brier could still matter if its errors
land on cheap quotes — but the measured calibration gap is large enough that
none is a candidate without further work.

# Candle-open replay (`tools/backtest_candle_open.py`)

The recorder's stored quotes (executable asks, spot, tau, ~5h / 74 rounds) let the
exact live decision path be replayed in hindsight: model -> edge gate -> persistence
streak -> candle-open window -> fill at ask -> settle. Fee is assumed 64 bps (the
per-quote fee is not recorded; FINDINGS measured 45-64 bps live).

| Configuration | Trades | Win rate | PnL | ROI |
|---|---|---|---|---|
| No candle gate, `min_edge=0.03` (old behaviour) | 43-47 | 0.62-0.65 | **−$1.21 … −$0.72** | −2.6% … −1.7% |
| Candle-open gate, `min_edge=0.03` | 28 | 0.64 | +$0.54 | +1.9% |
| Candle-open gate, `min_edge=0.05` (**current default**) | 21 | 0.67 | **+$1.32** | +6.3% |
| Candle-open gate, `min_edge=0.06` | 16 | 0.75 | +$2.55 | +15.9% |
| Candle-open gate, `min_edge=0.08` | 8 | 0.88 | +$1.14 | +14.2% |

Two changes follow from this:

1. **Candle-open gating is a real improvement** — it roughly halves the loss and
   turns it positive in every sweep run. Fewer, better trades beat more trades here.
2. **`min_edge` 0.03 -> 0.05.** Raising the edge floor cut the worst trades and
   improved PnL across the sweep. 0.06 scored higher but is a single-sample peak;
   0.05 is the conservative choice until the recorder has more rounds.

### Parameter sweeps (76 settled rounds)

One-dimensional sweeps around the defaults, then combinations of the winners
(`tools/backtest_candle_open.py --sweep`):

| Knob | Range tested | Best | Effect at best |
|---|---|---|---|
| `min_edge` | 0.03-0.08 | 0.05-0.06 | +$1.32 … +$2.55 (peaks at 0.06, degrades at 0.08) |
| `max_quote` | 0.80-0.95 | 0.90 (flat) | no change above 0.85 |
| `min_quote` | 0.10-0.25 | **0.25** | drops one cheap-longshot loser, +$1.32 -> +$2.33 |
| `signal_persistence` | 2-5 | **4** | +$1.32 -> +$4.06 (WR 0.67 -> 0.76) |
| `candle_open_window_s` | 3-10s | **10s** | +$1.32 -> +$4.67; 3s is negative |
| `safety_margin` | 0.005-0.02 | flat | edge > hurdle already dominates |

**Applied defaults** (`persistence=4`, `window=10s`, `min_quote=0.25`,
`min_edge=0.05`): 23 trades, WR 0.783, **+$6.10** (+26.5%), mdd −$1.75, both
sides profitable (Up +$2.71 on 8, Down +$3.39 on 15), and the PnL survives a
2% fee assumption (+$5.79). The no-gate comparison on the same settings trades
42 times for only +$1.95 — gating is doing real work.

Honest caveats: n=76 rounds, one recording, no out-of-sample split. These
defaults are the best *tested* configuration, not a proven one. The recorder
keeps growing the dataset; re-running `--sweep` weekly is the cheap way to
check whether the choices hold.

### Bias checks (`tools/backtest_candle_open.py --bias`, 82 rounds)

A look-ahead bug in the replay was found and fixed: the strategy models were
being handed the whole recording's 1m closes, including future bars. The replay
now only shows a model bars that closed before the decision minute (digital is
unaffected -- it ignores closes -- but every arena port's replay was contaminated
and is now honest).

What the checks say about the tuned defaults:

| Check | Result | Reading |
|---|---|---|
| Chronological split | 1st half −$0.17 (11), 2nd half +$6.27 (12) | profit is regime, not tuning: the untuned configs show the same split |
| Round-level bootstrap | median +$5.96, 95% CI [−$1.75, +$14.49], P>0 = 93% | likely profitable, **not proven** — CI spans zero |
| Profit concentration | top-1 44%, top-3 89% of profit; 5 losers | far less fragile than untuned (top-1 502%) but still few bets |
| Hour of day | profits at 14-17 UTC, −$1.33 at 13 UTC | one session's regime; not generalisable yet |
| Fee-model semantics | notional == profit, same 23 trades | no trade sits in the ambiguity band; result robust to the unconfirmed fee rule |

Tuning was validated on the grown sample: every sweep axis re-confirms the same
winners (persistence 4, window 10s, min_quote 0.25, min_edge 0.05). And across
configurations P(profitable) went 54% -> 62% -> 93% with concentration falling
502% -> 204% -> 44%: the tuning made the strategy less fragile, not more
lucky. The honest bottom line is unchanged though: the profit is real
in-sample, fragile, and needs a longer recording before it is trusted.

Not settled by this sample:

- **Per-candle attribution is unstable.** `tau~180s` lost on one configuration
  and won on another; excluding candles merely reroutes the trades to the next
  open. `trade_candle_opens` is a knob, defaulted to all candles — do not tune
  it until the recording is much larger.
- **n=54 rounds.** PnL numbers here are directionally informative, not
  statistically significant. The model-vs-market Brier at candle opens is a
  dead heat (0.167 vs 0.163, market slightly ahead), same as the fill replay.

## The edge, and what threatens it

**Primary alpha — the quote lags spot.** Observed live: spot jumped $32, model went to 0.911
while the market still quoted 0.600 (+0.31 edge); the market caught up ~7 s later.

**Secondary — longshot bias.** The losing side traded consistently ~9 points rich.

**Threats, in order:**
1. Effective spread is unknown and unobservable (no public book) — the dominant unknown.
2. 2% fee: breakeven needs `p > q × 1.02` if charged on notional. Semantics unconfirmed.
3. Price quantization at 0.01, and liquidity collapsing exactly when the model is sharpest.

## Harvesting history

Topic ids are non-contiguous (~5 ids per round, other markets interleave) and thin out
quickly going back — a backward sweep recovered only ~10 rounds (~1 hour). Rounds appear to
be created shortly before going live. Deep history therefore comes from klines (validated
above); harvested rounds serve as ongoing oracle ground truth.

## Model validation (2,831 rounds, 10 days, out-of-sample rolling sigma)

Sigma is estimated from the 48 rounds *before* each decision, so nothing is fitted in-sample.

| Decision point | Brier | Skill vs coinflip | Accuracy | Reliability (lower=better) |
|---|---|---|---|---|
| t=1 min (240 s left) | 0.2222 | +11.1% | 0.637 | 0.0009 |
| t=2 min (180 s left) | 0.1920 | +23.2% | 0.711 | 0.0011 |
| t=3 min (120 s left) | 0.1517 | +39.3% | 0.782 | 0.0014 |
| t=4 min (60 s left) | **0.1104** | **+55.9%** | 0.852 | 0.0021 |

Reliability is very low at every horizon: the model's stated probabilities are trustworthy,
which is the property the trade rule actually depends on.

### Rejected: sigma calibration multiplier

The reliability tables show an apparent S-shape at t=3/t=4 (predicts 0.35 where reality is
0.24; predicts 0.85 where reality is 0.92), suggesting sigma is biased high. Fitting a scalar
sigma multiplier on 60% of 5,759 rounds and scoring on the held-out 40%:

| Mark | Best multiplier (train) | Train Brier | Test Brier @1.0 | Test Brier @fit | Change |
|---|---|---|---|---|---|
| 1 | 0.90 | 0.2159 | 0.2228 | 0.2231 | −0.17% |
| 2 | 0.90 | 0.1814 | 0.1928 | 0.1929 | −0.08% |
| 3 | 0.75 | 0.1394 | 0.1542 | 0.1544 | −0.12% |
| 4 | 0.70 | 0.0915 | 0.1140 | 0.1146 | −0.55% |

The multiplier improves training Brier substantially and makes **held-out Brier worse at every
horizon**. The S-shape is period-specific noise. **No sigma correction is applied.** Locked in
by a regression test so a future "improvement" cannot silently reintroduce it.

## Longshot bias, quantified

Inverting the model on a live quote: market at 0.60 with spot $50 above strike and 60 s left
implies sigma_1m = 0.0025 against a realised ~0.00037 — the market prices as if volatility
were roughly **7x** reality, which pins quotes toward 0.5 and leaves favourites cheap.
`pricing.digital.implied_sigma` exposes this as a live diagnostic.

---

# LIVE EXECUTION — verified against the real venue

## The API key permission trap

`GET /sapi/v1/account/apiRestrictions` showed `enableReading: true` and **every**
trading flag false. That listing does not cover prediction markets: the same key
placed real orders successfully. Do not use apiRestrictions to decide whether
prediction trading is available — call `wallet/list` instead.

## Endpoints (found via the docs' own `llms.txt`, not the gated HTML)

The rendered docs are a JS app that serves the homepage for unknown routes, but
`https://developers.binance.com/en/docs/llms.txt` is plain text and lists the
whole API surface. Namespace: **`/sapi/v1/w3w/wallet/prediction/`** — the
`/wallet/` segment is what earlier guesses were missing.

| Endpoint | Notes |
|---|---|
| `GET  /wallet/list` | wallet address + walletId; the real access check |
| `GET  /order-book` | **requires `vendor=PREDICT_FUN`** plus tokenId, marketId |
| `GET  /position/list` | requires `walletAddress`; returns balance summary |
| `GET  /position/filter` | `status=CLAIMABLE` surfaces redeemable tokenIds |
| `POST /trade/get-quote` | walletAddress, tokenId, side, amountIn, orderType, slippageBps |
| `POST /trade/place-order-bundle` | walletAddress, walletId, quoteId, timeInForce, accountType, orderType, slippageBps |
| `POST /batch-redeem` | walletAddress, walletId, **tokenIds** (comma-separated string) |

## Hard-won parameter values

- **`amountIn` is 18-decimal wei.** `"1"` is rejected as *"Your order amount is
  too small"* — a units error wearing a misleading message. 1 USDT = `10**18`.
- **`timeInForce` must be `FOK`.** `IOC` and `GTC` both return -1102.
- **`accountType` must be `SPOT`.** `CeDeFi` returns -3026, despite being what
  `balance/payment-options` advertises as the funded account.
- Quotes expire in **~3-5 seconds** (`expireAt`), so the order must follow immediately.
- Winnings do **not** auto-credit. A won position sits as `pendingClaim` until
  `batch-redeem`, which settles on-chain and returns a txHash with status PENDING.

## The two cost assumptions were both wrong — in our favour

| Assumption in the plan | Reality |
|---|---|
| Fee 200 bps (from market metadata) | **45-64 bps**, varying per quote, with `feeDiscountBps: 1000` |
| Half-spread 0.02 (guessed; no public book) | Book is **1 tick wide** (0.67/0.68); half-spread 0.005 |

There *is* an authenticated order book, so the guessed spread is gone entirely —
the bot now prices against the real ask. `priceImpact` was 0.0 at $1 size, with
70-440 units resting at each level. The displayed `outcomes[].price` is a stale
last-trade value (seen at 0.65 while the book mid was 0.675); it must not be used
for pricing.

Net effect: the decision hurdle fell from ~0.042 to ~0.016.

## What live trading exposed that backtesting did not

**Buying deep favourites is a trap.** The first live trade bought Down at 0.962
on a model reading of `fair=1.000`, risking $1 to win $0.04. It won, but the
trade was bad: breakeven at q=0.962 needs ~96.8% accuracy, and measured
top-bucket reliability was **0.964 actual against 0.973 predicted**. The model's
clamped 1.000 is overconfidence, not information. A `max_quote` cap (0.90) and a
`min_edge` floor (0.03) now reject these; both live trades at extreme prices are
refused by the current gate.

**`acted` meant "attempted", not "filled".** A run where every order was rejected
with -1102 wrote ~100 `acted=1` decision rows for a single round, which would
have inflated any trade-count analysis. It is now set only after the broker
confirms a fill.

**Failed orders retried at 1 Hz** for a whole round. There is now a 30 s cooldown
after any rejection.

**The cooldown then went too far the other way.** A FOK order fails whenever the
book moves between the quote and the execution (`status=FAILED` in
`order/history`), which is transient — but a single failure sat the bot out for
30 s, i.e. the rest of the round. Orders now make up to `order_max_attempts` (3)
attempts, each with a **fresh quote** (a spent `quoteId` cannot be re-sent) and
a full re-run of the edge recheck and the candle-open re-gate; the cooldown only
starts once they are all spent.

**A retry is only safe against a failure the venue confirmed.** `LiveBroker`
now records *why* `buy()` returned None (`last_failure`). A terminal
`FAILED`/`CANCELLED`/`REJECTED` status, an expired quote or a 4xx reject mean no
order exists, so re-placing is safe. A timeout or a 5xx on `place-order` does
not: the order may have been accepted and only the reply lost. Those are never
retried — re-placing one would double the exposure.

---

# The analysis tools were mixing markets (found 09-09)

The recorder covers every market in `MARKETS`, but the bot trades only the
primary one. Two tools pooled them, and both of their headline numbers were
wrong as soon as ETH recording started:

- **`analyze.py` oracle chain** paired consecutive rounds by `start_ms` with no
  market grouping, so it compared an ETH close against the next BTC strike and
  called it a settlement fault: **209 of 361 pairs "mismatched"**. Per market
  the chain is exact (btc 0/362, eth 0/294). The settlement series was never
  broken.
- **`analyze.py` go/no-go** priced every market with `median(decisions.sigma_1m)`
  -- and decisions rows only exist for the traded market, so ETH quotes were
  scored with BTC's volatility.
- **`backtest_candle_open.py`** replayed all markets together, measuring a
  strategy nobody runs. ETH alone lost money over the window, so every `--sweep`
  row was a blend of the traded market and an untraded one.

Both are now scoped by `--market`, defaulting to `MARKETS[0]`. Section 2 still
covers every market, one line each, because it is a recorder health check.

What changed once scoped to btc-5m alone:

| | pooled (wrong) | btc-5m only |
|---|---|---|
| replay, live config | n=136, +12.65, ROI +9.3% | n=106, +5.52, ROI +5.2% |
| split halves | +15.84 / **-3.19** | +0.82 / **+4.70** |
| profit concentration | top-3 = 61% | top-3 = **154%** |
| 60-150s Brier gap | +0.0012 | **+0.0018** |
| quote-lag peak corr | +0.2427 | **+0.3171** |

The pooled numbers were wrong in both directions: they invented a decay story
(the "second half is losing" was ETH) and they hid how few trades carry the
profit (without its best 3 trades the rule loses money).

**A sweep grid must bracket the live value on both sides.** `min_edge` stopped
at 0.08, which was the live setting, so it reported a monotone ramp to "the
best" value by construction. Extended past it, 0.08 is an unremarkable cell on
a surface that is flat and mostly inside the noise.

**Average quote accuracy does not imply trade-level profit.** The go/no-go says
the model only beats the venue in the 60-150s band. Cutting `max_tau_s` to 150
to match made things *worse* (-2.01 per $1 staked, P(profitable) 39%, the worst
of every cut-off tried; 180-240s are all positive). The rule does not trade the
average quote, it trades its high-conviction disagreements -- those are a
different population. The window stays at 240s.

---

# The model's error grows with its own confidence (found 09-10)

Live trades kept losing more often than the model said they would. Bucketing
every live fill by the model's claimed probability shows it is optimistic
everywhere:

| model says | actually won | n |
|---|---|---|
| 48.0% | 45.8% | 24 |
| 67.3% | **56.2%** | 16 |
| 80.0% | 76.5% | 17 |
| 91.5% | 85.2% | 27 |

The recorded quotes (40,489 scored btc-5m polls) show *why*, and it is not a
constant bias. Bucketing by **claimed edge** -- how far the model is from the
venue -- the shortfall grows with the claim:

| claimed edge | n (quotes) | model says | actually won | gap |
|---|---|---|---|---|
| 0.00-0.03 | 16647 | 60.0% | 58.2% | -1.7% |
| 0.03-0.06 | 10231 | 57.0% | 52.2% | -4.8% |
| 0.06-0.09 | 5064 | 57.0% | 52.7% | -4.3% |
| 0.09-0.12 | 2575 | 58.9% | 53.4% | -5.5% |
| 0.12-0.20 | 2169 | 53.3% | **39.6%** | -13.6% |
| 0.20+ | 369 | 52.3% | **25.7%** | -26.6% |

**The bigger the disagreement with the venue, the more likely the venue is
right.** A large "edge" is usually our own stale spot or a bad sigma, not a
mispricing. This matters directly, because `min_edge` *selects for* exactly
these trades -- raising it pushes the bot toward its worst-calibrated signals.

Expressed as realised edge over the price paid, inside the trading window:

| claimed edge | rounds | realised edge | 95% CI (clustered by round) |
|---|---|---|---|
| 0.06-0.09 | 339 | +0.030 | [-0.027, +0.081] |
| 0.09-0.12 | 216 | +0.064 | [-0.005, +0.128] |
| 0.12+ | 114 | +0.036 | [-0.070, +0.136] |

Roughly half the claimed edge is real -- but **every CI spans zero**, and the
two halves of the window give k=0.63 and k=0.07 with overlapping intervals. The
*sign* of the bias is solid (every band, both halves, the model claims more than
it delivers); the *magnitude* is not estimable from 31 hours.

## What was built

`evaluate(edge_haircut=...)` shrinks the claim toward the venue price:
`p_used = quote + haircut * (fair - quote)`, feeding the gate, the EV and
therefore Kelly. A proportional haircut is the right shape because the error is
proportional to the claim; `safety_margin` subtracts a constant and cannot
express this.

**It defaults to 1.0 (off).** `min_edge` is compared against the haircut edge,
so setting the haircut alone silently doubles the real threshold -- with
min_edge 0.09 a haircut of 0.5 demands a raw 0.18 and cuts the replayed trade
count from 153 to 13. The pair `haircut 0.5 / min_edge 0.045` reproduces the
current selection exactly (n=153, +23.79, identical).

**Honest limit:** while Kelly is clamped (`stake == min_order == max_stake`),
the haircut is close to a re-parameterisation of `min_edge` -- it only bites
differently through the EV test and the hurdle comparison. Its real value is a
correct EV in the logs, correct sizing if Kelly is ever unclamped, and one knob
that means "how much of the model do I believe" instead of a threshold tuned by
hand.

---

# Backtest on historical fills (`tools/backtest_orders.py`)

The venue has no historical quote feed, but `order/history` returns every order
this account ever filled — 154 orders, 124 of them BTC 5m, spanning 15 days over
52 distinct rounds. Each is a **real executable price at a known instant with a
known outcome**. Strike, settle and spot-at-order-time are reconstructed from
Binance klines (justified by the ±$0.005 basis measured above).

## A methodology bug worth remembering

The first run used **one global sigma** (from the last 1000 minutes) across a
15-day sample and reported *"MARKET IS AT LEAST AS GOOD"*, with model reliability
0.0320 — 15x worse than the 0.0021 measured on klines. That was not the model
failing; it was the input. Per-round sigma over the sample ranges **0.00039 to
0.00117, a 3x spread**, so a single sigma misprices most of the sample.

Recomputing sigma from the 240 minutes *before each round* (strictly out of
sample) flipped the verdict. **The conclusion was an artefact of my own method,
which is the strongest available argument for not over-reading the corrected
number either.**

## Result, with significance

| Metric | Observed | 95% CI (20k bootstrap) | Verdict |
|---|---|---|---|
| Brier gap (market − model) | **+0.00257** | [−0.0083, +0.0137] | **not significant** |
| Strategy PnL, 64 trades @ $1 | **+$29.09** (+45.5%) | [−$7.98, +$71.11] | **not significant** |

`P(model better than market) = 0.674` — barely better than a coin flip.
`P(profit > 0) = 0.932`, but the interval spans a loss.

## Why this does not settle the question

1. **Selection bias.** These are moments a trader chose to act, not a random
   sample of quotes. It cannot show the market is mispriced in general.
2. **High variance.** Win rate is 35.9%; profit comes from cheap longshots paying
   3-5x. Per-trade standard deviation is **$2.55 on a $1 stake**, and max
   drawdown was −$16.80 against +$29 of profit.
3. **n=119 over 52 rounds** is small for a 0.0026 effect.

The recorder remains the right instrument: it samples **every** round rather than
only the ones someone chose to trade, so it produces the unbiased dataset this
test lacks.

---

# GO / NO-GO RESULT (20 recorded rounds, unbiased sample)

## Two bugs that flattered the model

1. **`analyze.py` scored against `up_price`** — the stale displayed last-trade
   value, not the ask. Beating a price you cannot trade at is not evidence.
   Fixed to score against `up_ask`.
2. **Significance was computed per quote.** Quotes inside one round share a
   single outcome, so 1,388 quotes is really ~20 independent observations.
   Treating them as independent inflates confidence by roughly the number of
   quotes per round.

Against the displayed price with per-quote statistics, the 0-60s band showed a
Brier gap of **+0.0134** and looked decisive. Both of those were artefacts.

## The honest numbers

Clustered bootstrap, 4,000 resamples **of rounds**, scored against the ask:

| Band | Rounds | Brier gap | 95% CI | P(model better) | Verdict |
|---|---|---|---|---|---|
| 0-60s | 17 | −0.0020 | [−0.0117, +0.0089] | 0.342 | not significant |
| 60-150s | 20 | **+0.0059** | [−0.0042, +0.0174] | 0.859 | not significant |
| 150-300s | 20 | −0.0007 | [−0.0109, +0.0085] | 0.477 | not significant |
| **ALL** | 20 | **+0.0013** | [−0.0062, +0.0092] | 0.625 | **not significant** |

**There is no demonstrated edge against executable prices.** Every confidence
interval spans zero. The 0-60s band — where the model is sharpest against
*reality* (Brier 0.110 on klines) — is where it does *worst* against the market,
which is consistent with the market also being sharp there.

## Reconciling this with the strong kline result

Both are true and they answer different questions:

- **Model vs reality** (2,831 rounds): Brier 0.110, reliability 0.0021. The model
  is genuinely well calibrated.
- **Model vs market price** (20 rounds): no significant difference. The market's
  quote is *also* well calibrated.

Being good is not the same as being better. The trade only exists in the gap
between the two, and at this sample size that gap is indistinguishable from zero.

## What would change the answer

The 60-150s band is the one worth watching: P(model better) = 0.859, and it is
the band where the quote-lag mechanism should pay. It needs roughly 100+ rounds
(about 8 hours of recording) to separate from noise. Recording costs nothing.

---

# Training on historical data (`tools/train_model.py`)

Logistic regression on 45 days of BTC 1m klines (~12,900 rounds, 3,874 held out),
split by TIME not shuffled, decision point t=3min. Eight causal features,
including the baseline's own moneyness statistic plus six microstructure ones.

| Model | Brier | Accuracy |
|---|---|---|
| Digital closed form | 0.1481 | 0.787 |
| Trained (best L2) | **0.1475** | 0.788 |

Improvement **+0.0006**, 95% CI [−0.0011, +0.0022] — **not significant** on 3,874
held-out rounds.

## What the fitted weights say

    sqrt|z|      +1.0975     <- the baseline's own statistic
    z            +0.9365     <- ditto
    volume       +0.1010
    last_mom     +0.0937
    range/exp    +0.0849
    path_eff     +0.0277
    vol_regime   +0.0200
    prev_ret     -0.0135

**The model relearned the closed form and found nothing else.** Given free rein
over volume, momentum, intra-round range, path efficiency, volatility regime and
prior returns, it put essentially zero weight on all six. Its calibration on the
held-out period is excellent, but it is excellent at being the digital formula.

## Why this closes the loop

The route to an edge required the predictor to improve. It cannot: on a large,
properly held-out sample, `P = Phi(ln(S/K) / sigma*sqrt(tau))` is already the
answer for 5-minute BTC direction, and the market prices it at least as well
(the market beats us near expiry, p=0.008). There is no gap left to trade.

---

# Strategy logic changes (evidence-driven)

| Change | Why | Setting |
|---|---|---|
| Trade window starts at 60s, not 30s | Market beats us in 88% of rounds inside the final minute (p<0.0001). Trading there was a measured loss. | `min_tau_s=60` |
| Floor on the price we will buy | Fee observed at **180 bps on a 0.27 quote** vs 45-64 bps higher up: cheap longshots are the *most* expensive trades. | `min_quote=0.15` |
| Signal must persist 3 polls | A one-tick edge is usually our own stale book/spot sample, not a real dislocation. | `signal_persistence=3` |
| Trades only at candle open | This is a prediction model: predict the round's up/down at each 1m candle open and act there, not mid-candle. The streak resets at every open. | `trade_on_candle_open=true`, `candle_open_window_s=5` |
| Pre-trade quote recheck (live) | The venue's fee is dynamic (45-180 bps) and its executable price can differ from the book, so the decision used estimates. `get-quote` does not execute, so re-deciding on the real numbers is free. | live mode |

Effect on the replay (44 rounds): per-trade **-$0.081 -> -$0.060**, P(profitable) **32% -> 37%**,
trade rate 98% -> 89% of rounds.

**These remove measured losses; they do not create an edge.** Expected value stays
negative and the interval still spans zero. The pre-trade recheck is the one
change that matters regardless of strategy, because it stops a 180 bps fee from
silently turning a marginal trade into a loser.

## Market making is not available on this venue

`get-quote` accepts only `MARKET` -- `LIMIT`, `MAKER`, `GTC` and every limit-price
parameter name return -3026 -- and `place-order-bundle` requires a `quoteId` that
only `get-quote` mints. `order/list` and `batch-cancel` exist but must serve
orders placed through the app UI. **The API is taker-only**, so the
spread-harvesting approach that the published research calls the most profitable
one is structurally closed to us here.

---
## Re-tune on 296 rounds (2026-09-09)

The replay tool had been sweeping the candle-gated variant while live ran
ungated; it now honours `trade_on_candle_open` so sweeps test what live runs.
Re-swept on the grown dataset (296 rounds, 32k quotes):

| Knob | Result |
|---|---|
| `min_edge` | 0.08 is the only profitable floor (+$11.75); 0.03-0.06 all deeply negative |
| `min_quote` | 0.25 confirmed (+$11.75 vs +$9.42 at 0.15) |
| `max_quote` | 0.85 ≈ 0.90 (kept) |
| `signal_persistence` | **4 now wins: +$16.84, WR 0.72, mdd −$3.32** vs 2: +$11.75, WR 0.61, mdd −$11.91 |
| `safety_margin` | flat |

The overnight losses were exactly the marginal trades persistence 2 admits and
4 skips. On persistence 4 both split halves are positive for the first time
(+$12.96 / +$3.88), bootstrap CI [+0.9, +32.5] excludes zero, top-3
concentration 43%. `signal_persistence` moved 2 -> 4 in config and .env.

Caveats unchanged: this is selection on the same sample, and the live forward
test (50 settlements, −$2.26) predates the new settings — persistence 4 has
not been forward-tested yet. The bot is halted until the UTC day rolls and
will resume trading with the new settings.
# The fee is price-dependent, and a flat rate inflated the backtest ~15x

61 live quotes across px 0.01-0.90 fit exactly:

    fee_bps = 180 * min(1, (1 - px) / px)

The fee is **1.8% of potential winnings**, capped at 1.8% of notional below
px 0.50 (0.51->174, 0.73->67, 0.83->37, 0.89->23, 0.90->21 bps).

Backtests had assumed a flat 64 bps. That is ~3x too low for cheap contracts and
~3x too high for favourites. Correcting it:

| | Flat 0.64% | Real curve |
|---|---|---|
| Trades | 41 | 49 |
| Win rate | 63.4% | 69.4% |
| Per trade | +$0.34 | **+$0.023** |
| P(profitable) | 96.1% | **58.5%** |

The corrected gate takes more favourites (where the fee is genuinely small) and
fewer cheap contracts, raising the win rate while collapsing the per-trade edge.
**The apparent edge was ~93% an artefact of the flat-fee assumption.**

### Follow-up: curve applied end-to-end, and the tuning survives it

That collapse was measured on a looser gate. On the committed tuned gate
(`min_edge=0.05`, `min_quote=0.25`, `persistence=4`, `window=10s`, candle-open
only), the correction costs almost nothing, because the gate lives in the price
range where the flat rate was least wrong:

| (94 rounds) | flat 0.64% | measured curve |
|---|---|---|
| Trades | 26 | 26 |
| Win rate | 0.769 | 0.769 |
| PnL | +$9.09 | **+$8.98** |

The curve now applies everywhere it matters: the live gate (was a flat 100 bps
fallback), the paper broker (was flat), and both replay tools (were flat). A
regression test locks the curve. Re-sweeping every knob under the corrected fee
confirms the existing defaults are still the best in each range -- nothing to
re-tune.

The corrected-fee bias checks are unchanged in character, and the split is
sharper than "one regime": the **first 13 trades lose −$0.72** and the **last 13
make +$9.70** — every other period is unprofitable, and 3 trades carry 75% of
the profit. The second half is **not held out**: the gate parameters were swept
on the full recording, so the profitable half participated in choosing them.
The fair label is *hypothesis with encouraging support, not an edge*.
Round-bootstrap: median +$8.82, 95% CI [−$0.55, +$18.74], P(profit) 97%.

Because the split is the fastest discriminator, `--bias` now also prints
rolling halves (oldest N vs newest N) and the live settlements as a forward
test that no sweep has seen. If live trades keep resembling the losing half,
that settles the question before the CI does. Live is currently 4 settlements,
−$1.69 — tracking the losing half; the $3 daily cap bounds the cost of the
experiment.

## Re-tune @ 366 BTC-only settled rounds (2026-09-09, live config)

**First, a data bug was fixed in the tools themselves:** the replay and the
analytics were mixing every market in `MARKETS` (BTC + ETH), but the bot only
ever trades BTC — so every sweep result before this section mixed in an
untraded market's PnL and reported optima nobody would actually have traded.
`tools/backtest_candle_open.py` and `tools/analyze.py` are now scoped to one
market (default: the traded one). All numbers below are BTC-only.

Live config is: ungated, `min_edge=0.08`, `persistence=4`, `min_quote=0.25`,
measured fee curve. On the clean sample:

| Axis | Result |
|---|---|
| `min_edge` | 0.08 → +$3.48 (WR 0.56, mdd −$8.40); 0.06 → +$12.85; 0.09 → +$4.93; 0.10 → −$3.56 |
| `signal_persistence` | 2 → +$19.71 (mdd −$11.62); 4 → +$3.48; 5/6 ≈ 0 |
| `min_quote` | 0.15 → +$8.80 (mdd −$18.76); 0.25 → +$3.48 |
| `max_tau_s` | 210s best (+$6.90); 150s worst (−$4.05) |
| joint cells | 0.09/pers3 → +$12.33, mdd −$6.88; 0.08/pers2 → +$19.71, mdd −$11.62 |

The honest reading changed: the current config is **not** the grid optimum on
clean data (0.09/pers3 and 0.08/pers2 beat it), but every configuration's
round-bootstrap CI spans zero — current: median +$3.10, 95% CI [−$18.73,
+$26.40], P(profitable) 61%, top-3 trades = 244% of profit. In-sample the
strategy is *unproven*, not optimal. The live forward test remains the only
positive signal: **75 settlements, WR 0.653, +$36.52**.

**Adopted (19:43, same day): `min_edge=0.09`, `persistence=2`, `max_tau=210`.**
Joint grid on the clean sample: +$23.67 vs +$4.44 for the old config, 125
trades, WR 0.57. The defensible part of the choice is **persistence=2 alone**:
all top-8 cells in the 54-cell grid use it, i.e. one dial is consistently
better across the whole grid. The rest is noise-level: `0.08/2/180` (+$25.44)
and `0.08/2/210` (+$24.49) sit within each other's error bars of the adopted
cell, and a both-halves-positive filter does not discriminate (25 of 54 cells
pass it, including pers-3 and pers-4 cells). Treat +$23.67 as a ceiling, not
a forecast. A guard simulation shows no downside: both old and adopted halt
once per ~31h on the $35 cap (adopted halts later, 4 signals blocked vs 42).
Do not re-tune on the same 31 hours again — that is fitting the noise twice.
The live forward test remains the judge.

Also in this pass: `LiveBroker` now classifies order failures — only failures
the venue *confirmed* (4xx rejection, terminal FAILED status, missing/expired
quote) are retried (`order_max_attempts=3`), and unknown outcomes (timeouts,
5xx, unreadable status) are never re-placed, closing a potential
double-exposure path. 100 tests pass.

## Drift-adjusted digital prototype (2026-09-10)

Question: can Up be fixed with a momentum-aware model instead of Down-only?

- New model `drift_digital` (btcpredict/models/drift_digital.py): adds an
  estimated short-term drift to the digital formula,
  P(Up) = Phi((ln(S/K) + scale*mu_1m*tau_min) / (sigma_1m*sqrt(tau_min))),
  mu blending recent-bar log returns with the round's realized move.
- Result on the recording (513 rounds, ~43h): driftless Up was 14 trades
  WR 0.429 -$3.98; drift Up is 55 trades WR 0.745 +$8.23. Raising the
  driftless edge floor never fixed Up (losses per trade grew with claimed
  edge) -- the venue prices Up momentum that the driftless model cannot see.
- Per-side hybrid (driftless Down + drift Up, opt-in via `up_model` setting):
  79 trades WR 0.759 +$27.09 mdd -$3.33. All 12 sweep cells positive
  (+15.18..+29.62), split halves +12.06/+17.56, round bootstrap 95% CI
  [+13.09, +46.37] P>0 100%. Defaults (scale 0.5, blend 0.5) kept, not the
  argmax, to avoid cell-picking.
- Live config untouched (digital Down-only, +$19.34, mdd -$1.35). A paper
  instance (`btcpredict-paper` systemd unit) runs the hybrid with
  SIDE_FILTER=both, UP_MODEL=drift_digital on the VPS for an out-of-sample
  trial; guard seeding is mode-scoped so paper never inherits live PnL.

## The drift prototype is over-confident — reverted, and ETH removed (2026-09-10)

**Supersedes the section above: `up_model` is back to `digital` and Up stays on.**

Live Up was claiming ~0.76 and delivering ~0.56. The first measurement of that
used 46 acted Up trades, whose standard error is ~0.12 — big enough to "show" a
bias that small samples cannot see, and to hide one they can. So the measurement
was redone on the whole evaluated population instead of the trades
(`tools/calib_quotes.py`, which scores every tick and clusters errors BY ROUND):

  model                   band 0.5-0.95    all ticks     by band
  driftless (digital)     -0.039 (1.7 s)   -0.007 (0.4s) flat, every band within 1 s
  live drift (scale 0.5)  -0.068 (2.9 s)   -0.009        -0.085 at 0.75-0.95
  drift (scale 1.5)       -0.111 (4.6 s)   -0.022        -0.135 at 0.75-0.95

514 BTC rounds / ~24k ticks, replicated on 444 ETH rounds / ~19k ticks. Re-run
later the same evening on the fuller recording (666 BTC rounds / ~44k ticks) and
the verdict sharpens rather than moves:

  model                   band 0.5-0.95    all ticks    by band
  driftless (digital)     -0.040 (2.0 s)   -0.002 (0.1s) +0.010/-0.006/-0.020
  live drift (scale 0.5)  -0.072 (3.5 s)   -0.007       -0.080 at 0.75-0.95
  drift (scale 1.5)       -0.115 (5.4 s)   -0.018       -0.135 at 0.75-0.95

The shape matters: it is not "P(Up) too high" but OVER-CONFIDENCE — a fan that
under-claims low probabilities (+0.049 at 0.05-0.25) and over-claims high ones —
and it grows with the drift the model is told to trust (-0.072 at scale 0.5,
-0.115 at 1.5). The prototype built to fix Up was the cause, not the cure; the
driftless model is calibrated to within 1 sigma in every band. `.env` had
overridden the config default of `digital`, so the fix was to stop overriding it.

**Cross-checked independently against klines** (`tools/backtest_calibration
--days 10`: 2,879 synthetic rounds, out-of-sample rolling sigma, no venue quotes
and no selection): reliability 0.0011 at every decision mark, skill +11.5% at
t=1m rising to +38.6% at t=3m. It agrees, with one mild exception worth naming:
the top of the range over-claims (0.8-0.9 claimed 0.844 vs 0.739 actual, 0.9-1.0
claimed 0.947 vs 0.843 at t=1m) and the bottom under-claims. That is ordinary
normal-tail optimism, and it is exactly why `max_quote=0.90` exists -- the cap
is validated by this, not contradicted by it.

Do not read this as "Up now makes money": the replay's SELECTED Up trades are
still negative (14 trades, -$4.0) and live Up is 52 trades WR 0.56, -$3.18.
Calibration was necessary, not sufficient. The tradeable-band slice of the
driftless model sits at -0.040 (2.0 sigma) -- borderline, watched, not acted on.

**`up_regime_gate` re-tested** (`tools/gate_sweep.py`), now that Up is priced by
the calibrated model: it moves 0-1 trade out of 14 and 0.01+ is identical to OFF.
Its original justification (Up WR 0.50 rising vs 0.82 flat/down, worth ~+$3) came
from 25 rounds under the old model and does not reproduce. Left at 0.005 as
untuned-but-free, and the config comment now says so.

**ETH removed from `MARKETS`.** The bot never traded it — `main.tick` passes
`trade=(prefix == primary)` and every live fill in the record is `btc` — so all
it produced was recorded data behind a second spot feed, a second volatility
estimator and a second discovery call per tick. The recorded history stays in the
database; re-adding it is one line in `MARKETS` plus the symbol in the tools.
Worth knowing what is lost: the ETH replication was the strongest single piece of
evidence for the calibration finding above, and that is now a manual re-add.

## The Up side: a purpose-built model says there is no Up edge to find (2026-09-10)

The Up side is where this book loses (live: 52 trades, WR 55.8%, -$3.18; replay:
18 trades, WR 44.4%, -$3.43) while Down pays. Every *threshold* tested for it
(per-side edge bar, price cap, price floor, probability shift) helped on one
dataset and hurt on the other, which is the signature of fitting noise. So
instead of a threshold, the probability itself was rebuilt:
`btcpredict/models/calibrated.py`, fitted by `tools.train_up_model.py`.

Method: one tick per round (the first the gate evaluates, so rows are
independent), fitted on the first 60% of rounds and scored on the last 40% it
never saw. The design was checked before the coefficients were believed --
pairwise column correlations are printed, and two earlier specifications were
discarded because they were unidentified:

  * with moneyness in sd as a column, the fit returned b_market = -1.32
    (se 3.54) and b0 se = 11.6: logit(p_digital), z and the disagreement are
    one column three ways, since the digital probability IS a function of z;
  * with a free intercept plus sqrt(tau/60), the pair is only identified
    jointly and put the model 2.5 points BELOW the venue at q = 0.5, inventing
    a permanent Down bias from noise. Dropped both; at q = 0.5 with no
    disagreement the model now returns exactly 0.5.

Final fit (384 rows, 3 parameter), scores on the unseen 250 rows:

| term | coef | se | z | reading |
|---|---|---|---|---|
| `b_market` | +1.2368 | 0.1529 | +8.09 | the venue's level, amplified |
| `b_disc` | +0.1033 | 0.3565 | +0.29 | our disagreement is worth ~0 |
| `b_imb` | +0.3965 | 0.7709 | +0.51 | order flow adds nothing |

**`b_disc` is the whole Up story: when we disagree with the venue, ~10% ± 36%
of that disagreement is real, i.e. statistically nothing.** Fitted b = 1 exactly
is inside the interval. Up stops being mispriced when the model stops claiming
an edge, and this is the measurement of how much was being claimed.

Probability quality on the unseen slice (Brier): venue ask 0.1927, calibrated
0.1921, digital 0.1940. The recalibration beats what we run and ties the market
(paired vs the venue: -0.00064 ± 0.00188, t = -0.34) -- it is not a discovery,
it is a repair.

Consequence, and why this is not simply "turn Up off": used as `up_model`, the
recalibration takes **zero Up trades** on both slices, because it never honestly
finds the 0.12 edge the gate demands. The Up side does not need a filter or a
ban; the venue's own price is already the best available Up probability.

Separate finding, same fit: **the venue's price is too tame.** Fitting
logit(outcome) = b * logit(ask) on BOTH sides over 768 train rows gives
b = 1.2538 ± 0.1055 (z = +2.41 against b = 1), and the amplified price beats the
raw price on Brier on both slices (train 0.1905 vs 0.1916, test 0.1922 vs
0.1928). Buying the amplified favourite (either side) at a 0.01 probability
threshold returns +0.027/$1 on 254 unseen trades, and +0.017-0.044 on the train
slice -- positive everywhere, but t < 1, so the bias is real while the money
from it is not yet demonstrated. Note this is the same force the Down side has
been earning from all along (Down live: mean q 0.555, WR 69.4%).

Open question, unmeasured: the Down side has never been shown the down quote --
the gate prices Down as 1 - fair_up. A symmetric recalibration of the Down side
is the obvious next test, and it is where the money is.

## The same treatment for Down, and the decision not to ship either (2026-09-10)

Both sides now have a fitted, market-anchored recalibration (`calibrated` for
Up, `calibrated_down` for Down), and both were scored on the same split: fitted
on the first 60% of rounds, scored on the last 40%. The Down quote is new --
until now the gate priced Down as (1 - fair_up), so the side that earns the
money had never been shown its own market price.

| fit | b_market | b_disc | test Brier (venue / model / digital) |
|---|---|---|---|
| Up | +1.2368 ± 0.1529 (z 8.09) | +0.1033 ± 0.3565 (z 0.29) | 0.1927 / 0.1921 / 0.1940 |
| Down | +1.2427 ± 0.1523 (z 8.16) | +0.0570 ± 0.3642 (z 0.16) | 0.1925 / 0.1919 / 0.1937 |

Both are better probabilities than the model we run, both are level with the
venue (paired t of -0.34 and -0.31), and **both say our disagreement with the
venue is worth nothing** (b_disc inside one standard error of zero, on two
independent fits). The two sides agree to three decimal places on b_market,
which is the strongest single number produced today: the venue's price is real
and amplified (~1.24x), and our own contribution to it is not measurable.

Then the money test, which is the one that decides:

| config | bar | train | test |
|---|---|---|---|
| live (digital both sides) | 0.12 | 35 trades, +0.388/$1, t 1.89 | 17 trades, +0.012/$1, t 0.04 |
| recalibrated, both sides | 0.12 | 0 trades | 0 trades |
| recalibrated, both sides | 0.05 | 66 trades, +0.052/$1, t 0.80 | 38 trades, -0.032/$1, t -0.33 |
| recalibrated, both sides | 0.03 | 335 trades, +0.008/$1, t 0.25 | 232 trades, +0.002/$1, t 0.06 |
| recalibrated, both sides | 0.02 | 363 trades, +0.008/$1, t 0.23 | 245 trades, +0.013/$1, t 0.32 |

**Decision: neither recalibration is shipped, and `up_model` / `pricing_model`
stay `digital`.**

  * as a swap they are harmful, not neutral: with an honest probability nothing
    clears min_edge = 0.12 -- zero Up trades AND zero Down trades, i.e. the gate
    would stop the book. That is worth knowing in itself: **the 0.12 bar is
    only reachable because the digital model overstates edges.** An honest
    probability refuses to trade this book at all;
  * with the bar lowered to where the honest model does trade, the edge is
    1.3%/$1 with t = 0.32 on 245 unseen trades -- the right sign on both
    slices, and about 10x too small a sample to bank.

The other thing this exposed, which matters more than either fit: **the live
config's profit lives entirely in the slice that chose its thresholds.** On the
unseen 40% it is +$0.21 over 17 trades (t 0.04) against +$13.57 over 35 trades
(t 1.89) on the train slice. Both sides show it (Down: +18.15 train, -0.94
test). This is the walk-forward warning the repo already had a tool for, and it
is the reason not to add any more tuned machinery on top.

## The real fix cannot be shipped blind: it is being settled forward in paper (2026-09-10)

Everything measured today agrees on one thing: our model's disagreement with
the venue is worth nothing (b_disc = 0.10 +- 0.36 on Up, 0.06 +- 0.36 on Down),
and the only mispricing we can actually see is the venue understating
favourites by 1.24 on the log-odds (z ~ 8, on both sides independently). The
obvious fix -- price honestly, i.e. anchor on the venue and stop spending the
disagreement -- has one problem: it cannot be validated on the recording.

  * at the live bar (min_edge 0.12) an honest probability trades NOTHING, on
    either side. Shipping it is not a fix, it is a shutdown, and the fact that
    the bar is only reachable via an overstated model is itself the finding;
  * at a bar where honesty still trades (0.02) the edge is +1.1%/$1 over 610
    rounds, positive on both slices -- and t = 0.45. A grid over the price floor
    and the bar (12 cells) showed the sign flipping between slices, which is
    what a search over noise looks like;
  * the arithmetic of why it cannot be settled here: a binary bet's per-trade
    return has sd ~1.2, so to resolve +1.1%/$1 at 2 sigma takes
    (2 x 1.2 / 0.011)^2 ~ 48,000 trades. The recording has 610.

So it is settled forward, on live quotes, without risking money:

    btcpredict-paper.service   --mode paper
      DB_PATH=data/paper2.db  PRICING_MODEL=calibrated_down
      UP_MODEL=calibrated     MIN_EDGE=0.02   MIN_QUOTE=0.50

A second instance, its own database, so it can never write into the live book.
It prices both sides from the venue's own ask (amplified by the fitted 1.24),
buys only sides quoted at 0.50+, and pays the measured fee curve. The live book
is untouched: `up_model` and `pricing_model` stay `digital`.

Read it with `python -m tools.ab_report`, which reports profit per $1 staked by
side with a t-statistic -- the only column that says anything. Abort condition,
stated before the data arrives: **if the honest book's EV/$1 is <= 0 after 2,000
settled trades, the favourite-bias hypothesis is dead and the honest config is
not the answer either.** At the replay's measured pace that is one to two weeks
of running.

Operational notes, because they cost time once already:
  * the VPS deployment is rsync, NOT git -- `/root/BinancePrediction` has no
    `.git`, so a `git pull && ...` chain fails silently and skips the rest;
  * a stale `btcpredict-paper.service` from an earlier experiment existed on the
    host (mode paper, UP_MODEL=drift_digital, writing into the LIVE db). It has
    been stopped, disabled and kept at `/root/btcpredict-paper.service.old`.
    It had added no rows.

## Shipped: honest pricing is now the live config (2026-09-10)

The best version the historical data supports is now live. It is the honest
pricing from the previous section, with one knob fixed that I had got wrong.

**The haircut was being applied twice.** `edge_haircut` (0.85) shrinks the
model's disagreement toward the quote before the gate believes any of it. But
`calibrated`/`calibrated_down` ARE that shrink, fitted: their b_disc terms are
the measured share of the disagreement that is real (0.10 and 0.06). Running
them under a 0.85 haircut shrank an already-shrunk edge, and the gate then
demanded 0.02 of what was left. With `edge_haircut = 1.0` the same signals come
through at twice the realised value:

| config | train | test |
|---|---|---|
| incumbent (digital both sides, bar 0.12) | 35 trades, +0.388/$1, t 1.89 | 18 trades, **-0.045/$1, t -0.17** |
| honest, haircut 0.85, bar 0.02 (paper run) | 365 trades, +0.009/$1, t 0.26 | 246 trades, +0.011/$1, t 0.26 |
| **honest, haircut 1.00, bar 0.01 (SHIPPED)** | 366 trades, +0.017/$1, t 0.49 | 247 trades, **+0.026/$1, t 0.63** |

The incumbent is the best config on the first 60% of rounds and negative on the
last 40%; the honest config is positive on both and *better* out of sample than
in it. That is the whole reason for the switch, and it is also the honest limit
of the claim: +0.026/$1 at t 0.63 is not a demonstrated edge, it is the best
estimate available.

Live `.env` now:

    pricing_model=calibrated_down     up_model=calibrated
    edge_haircut=1.0                  min_edge=0.01
    min_quote=0.50                    stake_fraction=0.02

Revert is `cp .env.bak-2026-09-10 .env && systemctl restart btcpredict`.
Abort condition: if EV/$1 by side or in total is <= 0 after ~1,000 settled
trades, the honest pricing is wrong too and the revert goes in.

**The risk is not small and the account size is what sets it.** The honest
config takes a position in ~90% of rounds: ~260 trades/day against the previous
~44. Sizing is a fraction of bankroll, and `stake_for_fraction` SKIPS rather
than rounds up when the fraction cannot reach the venue minimum -- so the
venue's $1 order minimum is a hard floor of 1.5% of a $66 account, and the
smallest defensible bet is the smallest bet available. At 2% stakes the
expectation is roughly +0.026 x 260 x 0.02 ~ +13%/day if the edge is real, with
a daily standard deviation of about 0.02 x 1.2 x sqrt(260) ~ 39%. If the edge is
zero that is a 260-trade-a-day coin flip, bounded only by the 20% daily stop and
the 5-loss halt (which will now fire every few days: 260 x 0.28^5 x 0.72 ~ 0.3
expected runs a day). Everything about this is a bet on a t 0.63 estimate.

The paper instance (`btcpredict-paper.service`) has been stopped: with live
running the same pricing there is nothing left to compare it against, and two
instances polling the same book only risks rate limits.

First live order under the new config, for the record: `btc-updown-5m-1789082100`,
Up, requested 0.75, filled 0.7602, stake $1.00 (the 2% fraction came out below
the venue floor and the sizing code clamps up to $1 rather than skipping),
fee $0.0067. It is buying the favourite, which is what the honest pricing is
supposed to do: at q = 0.76 the amplified venue price is 0.806, an edge of
+0.046 against a 0.01 bar.

One fidelity gap between live and the replay, found while watching this:
live updates the persistence STREAK before the tau-window check, so a signal
that has been acting during tau > 210 counts toward the 2 polls and the trade
can fire on the first in-window tick. `backtest_candle_open.replay` filters on
tau first and never counts those ticks. Live therefore trades somewhat MORE than
the replay's numbers predict -- the replay's ~260 trades/day is a floor, not an
estimate. It is a tools-vs-live mismatch, not a live bug, and it pushes the
trade count (and so the variance) up, not down.

## Final backtest of the shipped config (2026-09-10)

Replay of exactly what runs live (`calibrated_down` for Down, `calibrated` for
Up, haircut 1.0, bar 0.01, quote floor 0.50), 678 settled rounds, 614 trades
(249/day), one position per round:

| split | n | mean q | WR | P&L | EV/$1 | se | t |
|---|---|---|---|---|---|---|---|
| TRAIN (first 60%) | 366 | 0.699 | 71.6% | +6.25 | +0.017 | 0.035 | +0.49 |
| TEST (last 40%) | 248 | 0.705 | 72.6% | +5.50 | +0.022 | 0.042 | +0.53 |
| ALL | 614 | 0.701 | 72.0% | +11.74 | +0.019 | 0.027 | +0.71 |

By side, ALL: Up 267 trades +0.002/$1, Down 347 trades +0.032/$1.
By side, TEST: Up 111 trades +0.021/$1, Down 137 trades +0.023/$1.

**The Up/Down asymmetry is gone.** Under the old gate the Up side was -0.072/$1
against Down's +0.199 on live trades; under honest pricing the two sides are
0.021 and 0.023 on the unseen slice, and both are positive. That is the check
that the earlier "Up is broken" reading was a composition effect, not a defect:
change the pricing, and the sides come back together.

Projection from $66.06, 249 trades/day, 800 paths, daily stop 20%, 5-loss halt:

| horizon | median | p10 | p90 | median P/L | return | chance down |
|---|---|---|---|---|---|---|
| 1 day | $70.02 | $55.00 | $83.62 | +$3.96 | +6.0% | 36.0% |
| 2 days | $75.02 | $55.27 | $94.46 | +$8.96 | +13.6% | 28.5% |
| 7 days | $94.68 | $49.72 | $136.49 | +$28.62 | +43.3% | 21.4% |
| 14 days | $120.55 | $49.68 | $199.60 | +$54.49 | +82.5% | 21.2% |
| 30 days | $211.70 | $49.66 | $470.23 | +$145.64 | +220.5% | 21.5% |

Median drawdown 31%. With the edge forced to ZERO the same simulation gives a
1-day median of -$0.66 and a 30-day median of $49.80 (-24.6%), chance down 76%.
That is the honest shape of the bet: the two worlds differ by ~$195 of median
terminal value and by 21% vs 76% chance of loss, and nothing measured yet says
which one we are in.

Two sizing mechanics show up in the numbers and are worth knowing:
  * the $50 floor: 2% of bankroll falls under the venue's $1 minimum below $50
    equity, and `stake_for_fraction` SKIPS rather than rounding up. Every path
    converges on p10 ~ $49.7 -- the account stops trading instead of grinding
    down. It is an accidental, and welcome, circuit breaker;
  * `P(5-loss halt) 91%`: at 249 trades/day and a 28% loss rate a 5-loss run is
    near-certain, so the day-scoped halt will pause trading most days. Part of
    why the realised trade count will sit below 249/day.

**A bug this run exposed, fixed here.** `tools/project_balance.simulate` ended
the whole DAY when one draw of the |z| tilt pushed the stake under the venue
minimum (`break` instead of `continue`), and it never modelled live's clamp-UP
to the floor. Every projection in this repo before now was therefore measured
against a strategy that stops trading after a handful of trades a day, which is
why they reported 30-day median P/L of ~+$0.30. `_stake()` now mirrors
`main._base_stake_for` + `main._stake_for` exactly.

## Daily stop: tested at OFF / 10 / 15 / 20 / 30% -- it does not matter (2026-09-10)

Asked whether the daily loss stop should be disabled, so it was tested properly
on the shipped config (618-trade pool at +0.0219/$1, 249 trades/day, $66.06,
300 paths, 30 days):

| daily stop | median | profit | med DD | p95 DD | P(DD>30%) | P(DD>50%) | ruin | halt |
|---|---|---|---|---|---|---|---|---|
| OFF | $245.88 | +$179.82 | 31.3% | 46.4% | 56.0% | 3.0% | 0% | 92% |
| 10% | $223.15 | +$157.09 | 30.0% | 45.0% | 50.0% | 2.3% | 0% | 92% |
| 15% | $248.52 | +$182.46 | 30.6% | 45.8% | 52.7% | 1.7% | 0% | 93% |
| 20% (live) | $236.27 | +$170.21 | 30.5% | 45.4% | 52.0% | 1.7% | 0% | 95% |
| 30% | $243.07 | +$177.01 | 31.1% | 46.4% | 55.3% | 2.3% | 0% | 92% |

Non-monotonic and within +-6% across every setting: this is noise, not a
signal. The reason is that the stop almost never binds -- the day-scoped
5-loss HALT fires on 92-95% of paths and truncates the day first (median worst
day is 10-17%, short of a 20% stop). Verdict: **leave it at 20%.** Disabling it
buys nothing measurable and removes a control that costs nothing.

## Why an old sizing table promised +$1,818 in 30 days (2026-09-10)

An earlier session's table (`tools/sizing_study`, 3000 paths x 200 TRADES)
reported median profit +$1,347 for f=3% and +$1,818 for f=4%. Re-running the
same tool, same horizon, on the shipped config now reports +$6 (3%) and +$7
(5%) -- a 200x difference from an unchanged calculation. Three reasons:

  * the horizon is 200 TRADES, not 30 days, and it bootstraps trades with no
    daily stop and no 5-loss halt;
  * back then the tool's pool was the in-sample 70-trade replay at +0.43/$1.
    The shipped pool is +0.0219/$1, a 20x smaller edge, and compounding a 20x
    edge over 200 trades is what produced four-figure medians;
  * the tool's own footer says it: "treat the growth columns as an upper bound".

**The honest 30-day answer** (`tools.project_balance`, which models days, the
daily stop, the day-scoped halt, the tilt, the venue floor and the cap): median
$211.70 from $66.06 (+$145.64, 800 paths), p10 $49.66, p90 $470.23. Run with the
edge forced to zero the same simulation gives a median of $49.80. So the range
is +$146 if the edge is real to -$16 if it is not, and a 300-path run of the
same thing gives $236 rather than $212 -- at these path counts +-10% is Monte
Carlo noise, which is itself worth remembering before reading any of these
tables too closely.

## ab_report now cuts at the config change (2026-09-11)

The report the pre-committed decision runs on was pooling eras: it read all
168 live settlements, which mixes the 114 trades of the retired digital/0.12
gate with the 55 under the shipped honest pricing. Pooled it says EV/$1 +0.119
(t 1.81) -- a number that describes no configuration that ever ran, the same
era-pooling error this project was already burned by once.

It now cuts at 2026-09-10T23:14:45Z by default (`--since all` to include older
eras), and the two reads are not close:

| era | n | WR | EV/$1 | t | P&L |
|---|---|---|---|---|---|
| pooled (wrong) | 168 | 69.6% | +0.119 | +1.81 | +$46.07 |
| **shipped config only** | **55** | **83.6%** | **+0.209** | **+2.74** | **+$26.18** |

Equity is $92.38, up from $66.06. Trade 55 of the ~1,000 the scaling rule
waits for.

Note for whoever reads this next: t = 2.74 at 55 trades already clears the
t > 2 bar, but 54 of those 55 trades were the SAME side (Up, 98%) inside one
directional regime, so the effective evidence is far narrower than n = 55
suggests. The bar is not being moved for a good number.

## Two live bugs found and fixed on 2026-09-11

### 1. A failed order could be re-placed and buy the same round twice

`venue_failed` is in RETRYABLE_FAILURES, and it was returned on the strength of
the order-history status alone. Order history is the SAME endpoint that lags a
fill by a second or two -- the code already refuses to trust it in the adjacent
branch -- so a stale FAILED for an order that had actually filled sent the
caller into the retry loop for a round it was already long. The live record
contains two such doubles (Sep 8, $1.00 of extra exposure each); at ~250 orders
/day instead of 44, it would have got much worse.

Fixed in two places:
  * `LiveBroker.buy` -- a non-FILLED status now confirms against the position
    LIST (the effect of a fill) before reporting `venue_failed`. Held -> book it
    and do not retry. Confirmed absent -> retryable. Unreadable -> `unconfirmed`,
    no book, no retry;
  * `Bot._place_order` -- before every retry, ask the venue whether it already
    holds the token, and refuse to re-place if it does or if the list cannot be
    read. Belt and braces, because the cost of the doubt is a doubled position
    and a retry costs one API call.

### 2. Every Down FAVOURITE was being refused by a complement bug (the big one)

`get_quote` quotes the token being bought (`tokenId`), so `averagePrice` is THAT
side's price -- verified against the record: a Down order's `real quote 0.600`
matched the venue's `down_ask = 0.6`. The pre-trade recheck passed `1 - real_px`
as the Down quote anyway, scoring a 0.60 favourite as if it cost 0.40. With
`min_quote = 0.50` that refuses **every Down order above 0.50**, i.e. every Down
favourite -- the exact side the honest pricing exists to buy.

Measured cost: **63 distinct rounds refused since the config switch**, and zero
such refusals on the Up side (Up was unaffected: its recheck passed the price
uncomplemented). The live book therefore ran **54 Up / 1 Down** while the replay
-- which has no pre-trade recheck -- took **267 Up / 347 Down**.

**This corrects the explanation given earlier for the overnight result.** I said
the 98%-Up profile was "by design, the tape was up all night". It was not: it
was this bug. The tape contributed, but the mechanism that produced a
one-sided book was a live-only code path, not the market.

It also invalidates the live sample as evidence about the shipped strategy: 55
trades of an Up-only book are not a sample of what the replay measured, so the
scaling rule's ~1,000-trade clock restarts at the fix (09:41:59Z, recorded in
`tools/ab_report.CONFIG_CHANGE`). First order after the restart was a Down
favourite at 0.73, which the bug would have refused.

## Tests that depend on .env are worse than no tests (2026-09-11)

Three retry tests and one config-shape test failed on the VPS while passing
locally, with byte-identical code and test files. The cause was the `.env`:
`test_gate_window_setting_shape` asserted `0.05 < settings.min_edge <= 0.15` and
`0.20 <= min_quote <= 0.30` -- one era's TUNED VALUES, which the shipped config
deliberately changed to 0.01 / 0.50. The retry tests assumed a Down price that
`min_quote = 0.50` legitimately refuses. A suite whose result depends on which
machine runs it is not a suite. The shape assertions are now invariants and the
retry tests pin the thresholds their stubs assume.

## Flat $10 a trade: asked for, measured, shipped with one change (2026-09-11)

The ask arrived as "set stake size to 10%", then "ok set it to 10$", not 10%.
Both readings were measured before `.env` was touched.

(`tools/project_balance.build_pools` -- 768 settled trades, +0.0331/$1,
sd 0.659; $103.76 start, 249 trades/day, 30 days, 600 paths, with the live
20% daily stop and the 5-loss halt.)

| rule | median | p10 | p5 | P(DD>50%) | P(account gone) | stake today |
|---|---|---|---|---|---|---|
| 2% x tilt (was live) | $886 | $397 | $311 | 0.3% | 0.0% | $1.17 |
| $10 flat, never shrinks | $1,851 | -$3 | -$5 | 68.7% | **20.3%** | $10.00 |
| **$10 while equity >= $83, then 12%** | **$1,846** | **$8** | **$8** | 68.2% | **0.0%** | **$10.00** |

The middle row is the literal ask. A fixed stake does not shrink as the
account does -- at $30 of equity a trade still risks $10 -- so about one path
in five ends with a dead account and the 10th percentile is zero. The bottom
row stakes the same **$10.00 today** (identical median, $1,846 vs $1,851), but
the stake falls below $10 once equity is under $83, and that is what removes
the ruin. Shipped as the bottom row: `sizing_mode=fraction`,
`stake_fraction=0.12`, `stake_cap_usdt=10.0`, `stake_tilt=none` -- i.e. the
tilt is off, because $10 flat and 0.5-2x of $10 flat are not the same request.
The $10 is 9.6% of equity today.

Two things in that table are easy to read past:

  * **At 9.6% a trade the account halves at some point in ~2 of 3 paths.**
    That is not ruin -- p10 is still ~$8 -- but it is the ride being bought,
    and the 20% daily stop now trips after ~2 net losses ($20), so about half
    of all days will end early.
  * The tilt moves the tail more than anything else in the simulator: the
    identical 2% arm reads P(DD>50%) 0.3% WITH the `inv_z` tilt and 18.7%
    WITHOUT it (medians $886 vs $2,695). Return and tilt are drawn
    independently here, so neither arm is "the truth" -- but a 60x swing in a
    headline risk number between two formulations of one rule means any single
    tail figure below deserves a second run before it is quoted as fact.

Revert target: `.env.bak-<date>-pre-10usd` on the VPS. The older
`.env.bak-2026-09-10` predates the honest-pricing era and is NOT a safe revert.

**Amendment, same day.** The brake was removed at the user's request, who
pointed at the live result ("way better than expected") as the reason. Live
sizing is now a literal flat band -- `sizing_mode=edge`,
`min_stake_usdt = max_stake_usdt = 10.0`, `stake_tilt=none` -- so every
accepted signal, at any edge and any equity, stakes exactly $10.00 (checked at
edge 0.011 / 0.02 / 0.05 / 0.20 -> $10.00). `stake_fraction` is now inert.
**The 20.3% ruin / -$3 p10 row above is the row that describes live**; the 12%
row does not.

Worth writing down plainly, because the live result is the argument for this:
the simulator's edge is bootstrapped from the same settled trades that make
live look good, so 20.3% is the ruin probability *conditional on the live edge
holding up* -- it is the optimistic case, not the gloomy one. The one guard
doing real work at this size is the 5-loss halt: five losses is $50, half the
account, which is why it stays on (the 20% daily stop now trips after two).

First order under $10 sizing: `OPEN btc-updown-5m-1789131600 Down
stake=$10.00 @0.837`. Revert other than the flat band: `.env.bak-<date>-pre-flat`.

## Flat $10 with no guards -- and why my own tail numbers cannot be quoted (2026-09-11)

Asked to disable the daily stop and the loss halt. Both are off live, and the
comparison was run first (770-trade pool at +0.0320/$1, flat $10, $103.43, 249
trades/day, 30 days, 600 paths):

| rule | median | p10 | P(ruin) | p95 DD | med worst day | trades/day |
|---|---|---|---|---|---|---|
| **SHIPPED: no guards** | **$2,503** | **$1,762** | 0.0% | 163.7% | 17.4% | 249 |
| 20% stop only | $2,040 | -$4 | 23.3% | 104.2% | 24.4% | 166 |
| 5-loss halt only | $2,193 | $1,466 | 0.0% | 159.6% | 21.7% | 219 |
| previous: stop + halt | $1,838 | -$4 | 24.0% | 104.0% | 24.0% | 148 |

Read the middle of that table and not its ends:

  * removing the guards is worth ~+36% median ($2,503 vs $1,838) and turns the
    10th percentile from -$4 into $1,762 -- because a truncated day cannot
    recover, and the number of trades taken (249/day vs 148) is most of the
    edge. At 2% of the bankroll the stop cost nothing measurable (section
    above, 6% spread); at a FLAT $10 it costs a great deal, for the same
    reason the flat stake is dangerous in the first place: it does not shrink
    with the account, so a stopped day leaves the next stake oversized.
  * the tail columns are NOT trustworthy at a fixed stake. A p95 drawdown of
    104-164% is impossible; the simulator keeps staking $10 after the account
    has fallen to $3, which the venue would refuse. "P(ruin) 0.0%" is that
    same artifact read the other way. What IS true: with both rules off, the
    only thing between a runaway losing day and the account is the wallet
    balance.

`max_consecutive_losses = 0` needed a code change, not just a config line: the
comparison `consecutive_losses >= max_consecutive_losses` is `0 >= 0` on the
very first call, so "disabled" would have read as "halt now" -- and it appeared
in THREE places (`check`, `on_settle`, `seed_daily`), now one `_streak_breached`
helper. Also fixed in passing: `tools/project_balance` still modelled FRACTION
sizing after live moved to a flat edge band, i.e. it would have reported the
braked rule as "live" -- it now reads `sizing_mode` and raises on a mode whose
inputs the pool does not carry.

The era cut moved to **2026-09-11T12:59:44Z** (the $10 sizing switch; guards
off at 13:05:44Z). Third cut in two days, and unavoidable: a stake that is no
longer a fraction of the bankroll is a different strategy, not a tuned version
of the last one. Trades settled before the cut are a different bet.

## Config profiles: one command between "optimal" and "aggressive" (2026-09-11)

The guarded 2% config is saved as `configs/optimal.env`, derived from the real
`.env.bak-20260911-1259-pre-10usd` rather than reconstructed from memory; what
is live now is `configs/aggressive.env` (flat $10, no guards).

    make config-status               # which profile the live .env matches
    make config PROFILE=optimal      # rsync configs+tools, apply, restart
    make config PROFILE=aggressive
    .venv/bin/python -m tools.switch_env optimal --dry-run

Switching optimal -> aggressive differs in exactly seven keys, and the tool
prints them before it writes anything.

Design notes, because this file decides real money:

  * Profiles are OVERLAYS of strategy knobs (sizing, guards, pricing, gate).
    Keys absent from a profile are left byte-for-byte alone, which is what
    keeps credentials and endpoints out of a file that gets rsynced around.
    The `.env.bak-*` files are full copies and DO contain the API keys in
    plaintext -- this is also why the profiles themselves carry none.
  * A profile key that is not a `Settings` field is refused: pydantic ignores
    unknown keys silently, so a typo would otherwise look applied and do
    nothing at all.
  * Every switch writes `.env.bak-<utc>-pre-<profile>` first, then re-imports
    settings in a fresh interpreter. If that import fails the backup is put
    back and the switch reports failure. `--restart` is a separate flag, so a
    rehearsal and a real switch differ by one word.
  * Both profiles pin the knobs that were IMPLICIT in the original files
    (`stake_cap_usdt=25.0`, `max_consecutive_losses=5`): a profile that leaves
    a value to `config.py` is one default edit away from changing silently.
  * "optimal" is the user's name for it and is a preference, not a result --
    that era was 35 settled trades at t = 1.11.

Switching profiles moves the era cut in `tools/ab_report.py`; that stays manual
on purpose, because the tool cannot know whether a change is cosmetic or a new
strategy.

Round trip verified live on 2026-09-11 13:10, in both directions
(optimal -> aggressive), each one a full switch: backup, overlay, pydantic
load-check, restart. No order was placed in either window, so the live record
is unchanged. `make` targets detect whether they are running on the live box
and run in place rather than ssh-ing to themselves.

## Alarm raised and retracted: the "missing" $75 was a withdrawal (2026-09-11)

The wallet read $28.02 at 17:37 against $108.65 at 13:10, while the book for
the same window said only -$7.58, and the bot had adopted three unexplained
drops on the way down ($111.25 -> $98.64, $131.99 -> $91.02, $101.92 ->
$72.67). I called it an unexplained leak of $60-80. It was not:

    $108.65  wallet at 13:10
     -$25    trading 12:59 -> 17:40 (51 trades, ~30 losers at a flat $10)
     -$50    WITHDRAWN BY THE USER, around 16:20
    ------
     ~$34    expected, versus $28.02 actual

The ~$6 residue is the two FILLED-but-unbooked orders (below), which came out
slightly POSITIVE, because the redeem claims all claimable tokens and both
positions won. Nothing is missing.

Two lessons, both mine:
  * Check every external explanation before calling a discrepancy a bug. A
    withdrawal is indistinguishable from a leak in the data available to the
    bot -- but it is not indistinguishable to the human, and one question to
    the user would have settled it before the alarm.
  * The bot's own warning was more careful than my analysis: "venue lower for 5
    reads in a row: withdrawal or unmodelled fees, not lag" named the two
    candidate causes that a silent book cannot distinguish. It adopted the
    level, which is the right response for sizing, and the log says why.

**Also real, and unrelated to the alarm:** the venue's order history holds two
orders that this bot never booked (`26091100001879245817` FILLED at 17:28:26
and `26091100001879260950` FILLED at 17:36:45). The second one is for the SAME
round as a booked fill, 13 seconds later and 1.5 cents higher -- a double buy,
in the path that is deliberately not retried ("unconfirmed: no book, no
retry"). It cost nothing here only because the round won twice. Same defect
class as the 09-11 fix, different door.

At $28.02 of equity the `optimal` profile cannot trade at all: 2% is $0.56, the
venue floor is $1.00, and `stake_for_fraction` returns 0 (skip) rather than
rounding up. The bot is idle, not broken -- the repo has no way to tell the
difference in the log, which is worth fixing next.

## The entry slip is now accounted for, not fixed (2026-09-11)

Two changes, both bookkeeping, neither of which claws back a cent:

  * `fills.ev_at_fill` (migrated) plus a log field: the EV per $1 at the price
    PAID, next to the gate's EV at the quote. First live example, the first $1
    order at 17:46:47: `gate->fill +0.0422 -> +0.0210` -- half the edge, on a
    trade the gate was happy with.
  * `tools.project_balance` subtracts the measured 0.0132/$1 from every trade
    (`--slip 0` restores the old gross pool). A projection built on the gross
    pool is not a slightly optimistic projection, it is a different one:

| pool | edge | median (30d) | p10 | P(DD>50%) |
|---|---|---|---|---|
| gross (what we used before) | +0.0292/$1 | $434 | $186 | 26.8% |
| net of the measured slip | +0.0160/$1 | $149 | $3.80 | 56.2% |

    (from $29.50, 2% floored at $1, no guards, 600 paths)

So the slip removes **45%** of the pool's edge, and the plan built on it was
roughly twice too optimistic about both the middle and the tail. It also
changes what the SIZE rule means: Kelly on the net edge is 3.7% of bankroll
(gross said 6.7%), so the live 2% is now about half-Kelly rather than a third.

**Not fixed.** The cost is still paid on 78% of fills. The levers that would
actually reduce it are execution-side, not filters -- the 1-2s between decision
and order (with `signal_persistence=2` deliberately buying filter quality with
latency), and the venue allowing only FOK. Both are now MEASURABLE per fill,
which is the point: the next real experiment is persistence 1 vs 2, decided on
`ev_at_fill`, not on a hunch.

Also caught while answering this: `tools/project_balance` did not know about
`stake_floor_to_minimum`, so it would have projected an idle bot while a
floored one traded every round -- the third time this tool has described a
configuration nobody was running. It reads the setting now.

## The mid-claim dead band: won the pool walk-forward, rejected out-of-venue

After the "always buying the wrong side" question (side rules flip sign between
halves), a byproduct surfaced: on the recorded pool, the MIDDLE third of the
claimed-edge distribution lost money. On the 833-trade pool, net of the measured
1.32c/$1 slip:

* small claims (~+0.024 mean edge): positive in all 4 quarters (+0.059, +0.029,
  +0.053, +0.030)
* **mid claims (~+0.037): negative in 3 of 4 quarters** (-0.104, -0.150,
  -0.023), ~zero in Q4 (+0.006)
* big claims (~+0.052): positive in all 4 quarters (+0.126, +0.033, +0.064,
  +0.075)

Walk-forward (fit the middle-third bounds on one quarter, apply to the NEXT;
totals over the same three out-of-sample quarters):

| rule | total net, out-of-sample |
|---|---|
| drop mid claims | +18.51 |
| edge >= 0.04 | +9.36 |
| keep everything | +8.25 |
| edge >= 0.05 | +4.59 |
| edge >= 0.03 | -4.00 |

"Drop mid" won all three folds (+5.01, +6.14, +7.36), while a plain edge FLOOR
lost to doing nothing -- the opportunity looked like a band, not a threshold.
Mechanism story: mid claims sit at prices ~0.63-0.68 where fee plus slip
consume the ~3.7c claim.

Then the strongest available test: the same rule on Polymarket BTC 5m trade
prints, 2026-03-01..07 (the week re-downloaded in full; 1,618 of 1,776 rounds
labelled from final-minute prices). The live gate was replicated end to end
(`calibrated_down` + `calibrated`, min_edge 0.01, quote band [0.50, 0.90], tau
[60, 210], up_regime_gate 0.005, Polymarket taker fee 0.07*(1-q)), entries =
taker-BUY prints on a 10s grid: 39,068 candidates -> 10,197 accepted cells
across 1,444 rounds; one-per-round variant: 1,444 trades. Harness:
`tools/poly_midband.py` + `tools/poly_midband_robust.py` (run on the VPS, data
under `/root/pmweek`).

**The band does not transfer.**

* the mid tercile is NOT a loser: all-cells EV/$1 positive on 7/7 days and the
  best bucket on 4 of them; one-per-round it is negative on 3/7 days but its
  per-trade EV beats keep-all in 5 of 6 folds
* on totals -- the test the pool used -- dropping the mid third loses
  out-of-sample in every variant:

| rule | total net, all cells | total net, one per round |
|---|---|---|
| keep everything | +613.63 | +78.03 |
| edge >= 0.04 | +501.85 | +39.41 |
| **drop mid third** | **+388.85** | **+66.70** |

* applying OUR cost structure to the same Polymarket trades (180bps fee curve
  + the 1.32c slip) changes nothing: drop-mid +381.59 against keep-all +597.92.
  If the pool's mid deaths were "fee+slip eating mid-sized claims", identical
  costs on similar claim levels should reproduce them. They do not.

**Verdict: do not ship the dead band.** The pool's mid result stays a
documented local anomaly -- most likely four quarters of luck (it was already
fading in Q4) -- and the out-of-venue pass removed it from the table instead
of confirming it. Same pattern as the buffer sweep and the side/timing rules:
every "win" that came out of one venue's quarters met a test that flipped it.

Incidental, same run: the live gate plus calibrated models prices the
Polymarket week positively even one-entry-per-round (per-fold EV/$1 +0.046,
-0.032, +0.066, +0.061, +0.124, +0.072; total +78 over ~1,400 entries, about
+0.055/trade), same sign as the Sep-10 replication (+0.17/$1 pooled, +0.127
BTC-5m, t=2.64). Caveats unchanged from that study: token sides are labelled
from final-minute prices, prints are treated as the available price, Binance
spot stands in for Chainlink settlement, and entries cluster within rounds.

## The calibrated model is benched: digital wins the tick-level test

Overnight (2026-09-11 22:00Z .. 09-12 04:00Z) the live book lost ~$4.60, and
the suspicion was that the calibrated ("honest pricing") model was the cause.
That is testable without waiting: the DB records every tick (`quotes`) and every
round's outcome (`rounds.winner`), so both models can be recomputed on the SAME
ticks and scored against what actually happened. 96,175 ticks over 973 settled
rounds, tau in the live band 60-210s, fills assumed at the venue ask:

| forecast | Brier | mean p |
|---|---|---|
| the venue's own Up ask | **0.15208** | 49.75% |
| digital (parameter-free) | 0.15729 | 48.29% |
| calibrated (was live) | 0.15311 | 49.45% |

Realised Up rate 50.95%. Gate run over the same rounds, one entry per round,
filled at the ask, fee curve applied:

| model | n | WR | realised EV/$1 | claimed | t |
|---|---|---|---|---|---|
| digital | 825 | 78.1% | **+0.0775** | +0.0544 | **+3.58** |
| calibrated | 938 | 83.3% | **+0.0123** | +0.0460 | +0.79 |

and the calibrated gate flips sign across halves (+0.0338 then -0.0092).

So the P&L complaint was right and the mechanism was not what either of us
said. The calibrated model is the more ACCURATE probability -- it is the only
forecast that tracks the venue's favourite-longshot bias, which is real and
large: realised Up rate against the ask, by band, 21.0% vs 19.5% at 0.15-0.25,
71.1% vs 69.5% at 0.65-0.75, **94.1% vs 89.7% at 0.85-0.95**, 99.2% vs 97.1% at
0.95+. In every band the realised rate is ABOVE the ask, so buying favourites
at the venue's ask is worth +2 to +4 points before fees -- the venue
under-prices favourites, and a naive model captures that as well as a fitted
one. What the calibrated model buys with its accuracy is thinner, worse-selected
claims: it under-delivers its own claimed edge by 3.4 points and takes 113 more
trades for a quarter of the realised edge.

**Decision:** `pricing_model=digital`, `up_model=digital` (`configs/control_digital.env`,
applied 2026-09-12 07:01Z, backup `.env.bak-20260912-0701-pre-control_digital`).
Everything else stays at the last config with a positive live record (market
orders, tau 60-210, 2% x inv_z floored to the venue minimum, guards on).
The calibrated model is not deleted: it belongs in a paper arm that has to beat
digital on the same ticks to earn its place back.

Caveats, stated so they are not rediscovered later: the test fills at the ask
with no slippage, and live market orders pay a measured 1.32/$1 of EV, so the
expected live number is roughly +0.0775 - 0.0130; the digital gate takes ~85%
of rounds (825 of 973), which is a very loose filter and a rate the bankroll
rules will be the binding constraint on; and the 0.020-0.143 edge range seen in
the two models' accepted sets overlaps heavily, so this is a selection
difference, not a different product.

Two process failures from the same night are worth recording with the result:
the server's code had drifted from this workspace (`main.py`, `config.py`,
`execution/live.py`, `models/calibrated.py` all differed, plus an
`order_type="two_sided"` mode and pair engine that never existed here, and a
tau 150-270 window matching no profile). Both were reverted by rsync from the
workspace on 2026-09-12 06:5x, with the test suite green (197) before restart.
The server is not a place to edit code. The unbooked 18:31Z fill of 09-11 is
still missing from the ledger.

## Audit of the digital path: the model is fine, the MEASUREMENT is fragile

Asked to audit digital top to bottom after the calibrated model was benched.
147,350 recorded ticks on settled rounds, 86,991 decisions, 448 live fills.
What came back:

**Inputs -- clean.** Spot age median 2ms (p75 95ms, p99 965ms): the feed is
fresh, so the quote-lag alpha is measurable rather than imaginary. The order
book is present on 78% of ticks and BOTH sides carry bid and ask when it is
(217,672 ticks both, 16 up-only, 4 down-only). Strike is never zero. Fee rate
is recorded on 99.6% of ticks.

**Sigma -- fine.** Median sigma 3.57bps/min against a realised median
|next-minute move| of 2.19bps. For a normal, E|X| = 0.80 sigma, so the implied
true sigma is ~0.94 of the estimate: the estimator is at most ~6-9% high.
corr(sigma, |next move|) = +0.43, so it does carry information.

**Probability scale -- fine, and the opposite of the complaint.** Logistic
regression of the outcome on logit(p): the digital model scores slope 1.063
(se 0.01) -- calibrated, no correction indicated. The CALIBRATED model's fair
scores slope 0.81 (se 0.007), i.e. it overshot (too sharp), and its intercept
flips sign between halves (-0.063 then +0.208). Nothing here says digital is
broken.

**The fee model IS wrong, and it is measurable.** Our assumption
`fee_bps = 180 * min(1, (1-px)/px)` fits the ledger with R2 = **-1.37** --
worse than predicting the mean. Fitting alternative forms on the same 448
fills: `k*(1-px)` gives R2 +0.18, flat 75.6bps gives 0.00, `k*px*(1-px)` gives
+0.11. The natural prediction-market form (fee = rate * shares * px * (1-px))
fits the non-zero-fee fills with rate 0.0331, R2 0.52. So the shape we charge
is not the shape the venue charges.

**But 137 of those 448 fills record a ZERO fee**, spread over both sides
(~$1.18 average stake) -- consistent with maker fills during the limit era.
Until maker and taker fills are separated in the ledger, the fee curve cannot
be identified cleanly, and the gate's cost model stays a guess in the one place
where a 40bps error flips trades from refused to accepted.

**The headline number was fragile, and that is the real finding.** The same
tick data, scored by two equally reasonable entry rules, gives digital's gate:

| entry rule | n | EV/$1 | t |
|---|---|---|---|
| first acting tick per round + up-regime gate | 825 | +0.0775 | +3.58 |
| no up-regime gate, different row order | 841 | +0.0267 | +1.10 |

Every correction tested on top (the fitted fee forms, a fitted probability
sharpening, max_quote 0.95/0.97, min_quote 0.30, min_edge 0.03) lands between
+0.019 and +0.038 per $1 with t < 1.5. So no variant of this gate has a
demonstrated edge yet, and the +0.0775 quoted earlier was the optimistic end of
a fragile measurement rather than the middle of a distribution.

**Actions, in order:** (1) record maker/taker and the venue's actual per-fill
fee, and classify the zero-fee fills, so the cost model can be identified on a
clean sample; (2) freeze ONE entry rule and record which tick was entered, so
entry-tick sensitivity stops being invisible; (3) do NOT re-tune thresholds on
this data -- the spread across variants is noise. Until (1) and (2) are in,
the honest posture is minimum stakes and a forward sample, not a claim.

## Fee coefficient corrected: 180 -> 200, and the threshold sweep SHIPPED NOTHING

Chasing the audit's fee finding produced one retraction and one real fix.

The retraction: an earlier reading of six fills (the largest absolute fees,
$0.18-0.20 on $10 stakes at px 0.27-0.52) suggested the venue charged a FLAT
~200bps of stake. It does not. Those six all sit in the capped region
(px <= 0.50), so they say nothing about the slope. Read properly, on the 311
live fills that actually paid, the charge falls with price exactly as assumed:

| band | n | charged bps | curve at 180 | curve at 200 |
|---|---|---|---|---|
| <0.50 | 40 | 209.9 | 180.0 | 200.0 |
| 0.50-0.60 | 39 | 175.1 | 144.1 | 160.0 |
| 0.60-0.70 | 89 | 116.0 | 96.2 | 106.9 |
| 0.70-0.80 | 89 | 72.7 | 62.2 | 69.1 |
| 0.80-0.90 | 48 | 38.1 | 33.2 | 36.9 |
| 0.90+ | 6 | 19.8 | 17.1 | 19.0 |

So the shape was always right and the LEVEL was 15-21% low in every band. The
fix is the coefficient: `FEE_MAX_BPS` 180 -> 200, which is the venue's own
reported `fee_rate_bps` (stated on 278,011 of 278,929 recorded ticks, and
already asserted in tests/test_store_and_feed.py). Shipped 2026-09-12 07:13Z,
197 tests green. A near-miss worth recording: passing `fee_rate_bps/1e4` as a
flat `fee=` would have charged a flat 2% of stake -- it is the COEFFICIENT, not
the charge. The comment in main.py and the updated test now say so.

Backtest with the corrected fee (96,500 ticks, 976 settled rounds, one entry
per round at the first acting tick, fills at the ask):

| config | n | EV/$1 | t |
|---|---|---|---|
| coefficient 180 (old) | 826 | +0.0233 | +0.95 |
| coefficient 200 (new) | 823 | +0.0226 | +0.92 |
| min_edge 0.03 | 741 | +0.0323 | +1.23 |
| min_edge 0.05 | 553 | +0.0508 | +1.71 |
| max_quote 0.80 / 0.85 / 0.95 | 744 / 784 / 858 | +0.0249 / +0.0207 / +0.0198 | <1.0 |
| up_regime gate OFF | 838 | +0.0261 | +1.08 |

and split in halves the frozen rule gives +0.0361 (t +1.22) then **-0.0053
(t -0.12)**.

**Nothing shipped beyond the fee.** The fee correction is a correctness fix
worth ~0.1% of stake per trade; the sweep's best arm (min_edge 0.05, t 1.71) is
the best of twelve variants on one dataset and still under the project's own
bar, and the recent half is zero. Tuning on that would be fitting noise, which
is the mistake this project has made and documented three times. Config stays
frozen: digital both sides, min_edge 0.01, quotes 0.50-0.90, up-regime gate on,
tau 60-210, 2% x inv_z floored at the venue minimum, guards on. Posture stays
minimum stakes until the forward sample says otherwise; if the recent-half zero
persists, the honest reading is that this venue no longer pays us.

Both "instrumentation" items proposed before the retraction turned out to be
unnecessary: `fills.raw` already carries `orderType`, `marketProviderFee` and
`fillPercentage`, and `decisions` already records which tick acted. The data
was there; the analysis was what was missing.

## entry_mode=impulse: trade the underlying's move, not the model's opinion

The level-based premise of this bot is measurably backwards. Measured on 96,800
recorded ticks, "the venue has not caught up with our fair value" is inversely
related to what pays: staleness buckets 0-0.02 / 0.02-0.05 / 0.05-0.10 / 0.10+
logit return -1.7% / -6.3% / -9.9% / -12.0% per $1 (t -4.9 to -9.1). When the
venue disagrees with our model it is usually right, and the fee is the result.

What does pay is the underlying's own impulse. Reaction events, one entry per
round, filled at the ask, net of the measured 1.32c/$1 slip:

| window | threshold | n | 1st half | 2nd half (out of sample) |
|---|---|---|---|---|
| 1s | >=4 bps | 411 | +0.1145 | **+0.0815** |
| 1s | >=5 bps | 259 | +0.1137 | **+0.2130** |
| 2s | >=5 bps | 295 | +0.1365 | **+0.1499** |
| 5s | >=5 bps | 443 | +0.0961 | **+0.0910** |
| 1s | >=2 bps | 1150 | +0.0253 | **-0.0999** |
| live model gate, same rounds | | 838 | +0.0288 | **-0.0081** |

Positive in both halves at every threshold >= 4 bps, across four independent
window choices, and dominant over the model gate on the same rounds. Sub-2 bps
moves lose: the venue is right about those and the 2% fee is the whole result.

The honest limits, recorded because they decide the outcome: t is 0.4-0.6 out
of sample (per-trade sigma ~2.5, so ~4,000 trades are needed for t=2, about
three weeks at the observed rate); and the whole result assumes the fill lands
near the trigger price. Impulse entries are exactly where adverse selection
lives -- the book is moving away from us as we fire -- so the first thing to
measure live is realised fill price against the impulse-trigger price, on the
first 200 entries, NOT the PnL.

**Shipped** 2026-09-12 07:25Z as `configs/impulse.env` (`entry_mode=impulse`,
1s window, 4 bps, one entry per round, venue-minimum stakes, guards ON, market
orders). The model gate is no longer consulted for entries and keeps being
recorded from `quotes`+`rounds`, so arm (B) is reconstructible offline on
exactly the same rounds. Reverts with one config line.

## 2026-09-12: the cheap-side lottery ticket (the "$1 -> $90" trade)

Asked to add, on top of the running arm, a $1 ticket on the cheap side -- the
trade that occasionally turns $1 into ~$90 at the end of a round. Mechanically
that is buying at ~0.011: for px < 0.5 the fee saturates at 2% of STAKE, so $1
buys ~98 shares and a win pays ~$98. Measured three ways, one ticket per round,
net of the measured per-price fee, entered at the last quote that still has a
book (asks are None inside the last ~6s, so tau >= 6s):

| entry | n | win rate | mean ask | EV/$1 | t |
| --- | --- | --- | --- | --- | --- |
| cheap side, tau >= 15s | 1,055 | 7.01% | 0.079 | **-0.4909** | -5.23 |
| cheap side, tau >= 8s | 1,055 | 6.54% | 0.075 | -0.4737 | -4.75 |
| cheap side, tau >= 6s | 1,055 | 5.59% | 0.072 | -0.5224 | -5.43 |
| favourite side, tau >= 6s | 1,055 | 94.41% | 0.954 | -0.0137 | -1.70 |

and by the ask actually paid (tau >= 6s):

| ask band | n | win rate | breakeven needed | EV/$1 | t | payoff |
| --- | --- | --- | --- | --- | --- | --- |
| 0.02-0.04 | 728 | **0.69%** | **2.31%** | **-0.7532** | **-6.74** | 43x |
| 0.04-0.07 | 111 | 5.41% | 4.92% | +0.1330 | +0.29 | 20x |
| 0.07-0.12 | 55 | 9.09% | 9.37% | -0.0645 | -0.16 | 11x |
| 0.12-0.20 | 43 | 16.28% | 15.50% | +0.1047 | +0.27 | 6.5x |
| 0.20-0.35 | 62 | 22.58% | 26.23% | -0.1595 | -0.79 | 3.8x |
| 0.35-0.50 | 38 | 42.11% | 43.45% | -0.0411 | -0.22 | 2.3x |

Then, in case the cheap side is only rich when the round is DECIDED (all of the
above had the favourite at 0.96+), the same ticket bucketed by how far the price
sat from the strike at entry (tau 6-15s, 541 rounds):

| \|S-K\| bps | n | fav ask | cheap ask | cheap WR | breakeven | EV/$1 | t |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0-1 | 109 | 0.801 | 0.285 | 21.1% | 29.1% | -0.2467 | -1.26 |
| 1-2 | 125 | 0.911 | 0.128 | 13.6% | 13.1% | +0.1377 | +0.39 |
| 2-4 | 204 | 0.972 | 0.056 | 6.86% | 5.72% | +0.0061 | +0.02 |
| 4-8 | 87 | 0.976 | 0.042 | 1.15% | 4.23% | **-0.9625** | **-25.8** |

Restricted to rounds the book itself could not decide (favourite ask < 0.75):
n=51, cheap ask 0.432, cheap WR 39.2%, breakeven 44.1%, EV -0.0343, t -0.20.

**Verdict: no. Not anywhere.** In the decided zone the ticket loses 96c in the
dollar with t -25.8 -- the single most confident measurement in this file, and
it is negative. In the coin-flip zone the reason is visible in the prices: the
two asks SUM to 1.086 when the price is within 1 bp of the strike, so a ticket
starts ~4-5% behind in spread alone and the cheap side -- floored at 0.02 by the
venue -- is the worse half of that spread. The +0.13/+0.006 cells are t 0.4/0.0,
i.e. nothing.

Two facts worth keeping beyond the verdict:
* the cheap side never quoted below **0.020** in 1,055 rounds, so the real
  multiple is ~48x, not 90x, on this venue;
* the mirror of this is the favourite being the *better* bet at -1.4% rather
  than -2%, i.e. the venue understates favourites and overstates longshots --
  the same asymmetry measured earlier, now visible in the cheapest band.

Untested, and now the only gap: the last ~6 seconds, where the asks are missing
from our recording and where the flips are actually watched. The venue publishes
its order book over a signed websocket -- see below -- which is how to see them.

## The prediction-market order-book websocket (found in the docs' own index)

`/en/docs/products/w3w-prediction/websocket-api/{orderbook,integration-guide}`,
listed in the same `llms.txt` index the REST endpoints came from. Not used
anywhere in this codebase, and it removes the reason the book is invisible in
the last seconds.

    wss://api.binance.com/sapi/wss?random={random}&topic=web3_prediction_orderbook_{marketId}
        &recvWindow=30000&timestamp={timestamp}&signature={signature}

* same SAPI HMAC-SHA256 signing as the REST client: sort `random`, `recvWindow`,
  `timestamp`, `topic` alphabetically, HMAC the query string, append
  `&signature=` (self._sign() already does exactly this);
* `topic=web3_prediction_orderbook_data` is an AGGREGATED topic covering every
  market -- one connection, no resubscribe when the 5-minute round rolls;
  `|`-separated topics also work;
* payload: `{"data": "{\"msgType\":\"orderbook\",\"marketId\":...,\"updateTimestampMs\":...,\"asks\":[[\"0.32\",\"500\"]],\"bids\":[[\"0.31\",\"800\"]]}"}`,
  asks ascending, bids descending, empty array means no levels on that side,
  `updateTimestampMs` is the ordering key (no cross-message ordering guarantee);
* upstream is Predict.fun, delivery < 200 ms after the upstream event.

## The ticket, settled on 14 days and on both venues: real on Polymarket, ~7x weaker on ours (2026-09-12)

*Note on ordering: this belongs after the "$1 -> $90" section and before the
order-book websocket spec above, which sits last only because the insert tool was
disabled when it was written.*

The Polymarket sample was extended from one week to all 13 order-book days (14 trade
days, 02-28..03-13) and the BTC 1-second series was completed for the missing days
(`more/btc_1s_ext.parquet`, 778,000 bars, same Binance kline layout as `btc_1s.parquet`).
One ticket per round, first qualifying quote (largest tau), tau 3-30s, cheap side at
0.02-0.06, net of 0.07p(1-p) per share:

| band | n | WR | price | WR/price | EV/$1 | t | day-clustered t | days +/- | drop best day |
|---|---|---|---|---|---|---|---|---|---|
| dist < 1 bp | 110 | 17.3% | 0.0428 | **4.04x** | +3.56 | 3.0 | 2.28 | 10/3 | +3.12 |
| dist 1-2 bp | 175 | 8.6% | 0.0446 | **1.92x** | +1.97 | 2.1 | 1.57 | 7/6 | +1.93 |
| dist 2-3 bp | 241 | 4.1% | 0.0462 | 0.90x | +0.16 | 0.3 | 0.03 | 3/10 | -0.47 |
| dist 3-4 bp | 308 | 5.5% | 0.0441 | 1.25x | +0.56 | 1.2 | 1.12 | 8/5 | +0.15 |
| **dist < 2 bp** | **243** | **11.5%** | **0.0449** | **2.57x** | **+2.31** | **3.2** | **2.76** | **10/3** | **+2.23** |
| dist < 4 bp (deployed) | 592 | 6.4% | 0.0470 | 1.37x | +0.80 | 2.2 | 2.48 | 9/4 | +0.70 |
| **any distance (control)** | **3079** | **1.7%** | **0.0321** | **0.53x** | **-0.61** | **-10.0** | **-7.60** | **1/12** | **-0.60** |

What makes this one different from the earlier claims that died:

* the **control is right**: bought blindly, the cheap side wins 1.7% of the time at a
  3.2c price (0.53x) -- a consistent loser on 12 of 13 days at t -10. A broken pipeline
  cannot produce that and a 2.57x cell at the same time;
* **it survives the exact tests that killed the +10.5% leader cell**: day-clustered
  t 2.76 (not a pooled t), 10 of 13 days positive, and dropping the best day leaves
  +2.23, dropping the best two leaves +1.57;
* the gradient is **monotone in the right direction**: 4.04x -> 1.92x -> 0.90x as the
  price walks away from the strike, i.e. the venue's tail ladder barely responds to how
  close the price is. That coarse-tail-ladder explanation is the mechanism, and it also
  says why the trade must be a longshot bought *only* within ~2 bps;
* the winner has a clean definition: of 4,312 rounds with two tokens, 4,244 (98.4%) have
  exactly one token whose final print is > 0.5.

Window sensitivity (real this time, on a table built to tau<=90): tau<=3s is a loser
(n=128, 0.58x, EV -0.28, 3/10 days), tau<=10s is marginal (1.56x, +1.15, t 1.5),
tau<=30s 2.57x, tau<=60s 3.06x (t 3.8), tau<=90s 3.15x (t 3.9). But "first qualifying
within a window" is not the same trade across windows -- a wider window admits rounds
where the price arrived near the strike earlier, with more time to recover -- so this is
a selection effect, not a clean tau gradient. It is why the deployed band keeps 3-30s:
the tau split was noisy and changed sign between venues, and narrowing it would be
fitting noise.

### The order books are a placeholder, so "is it buyable" is still unverified

All 13 `orderbooks/*.parquet` days were downloaded (3.41M rows). They are **not real
quotes**: 2,138,775 rows are exactly `bid 0.10 / ask 0.90` and the rest are the same
synthetic ladder (0.11/0.89, 0.20/0.80, 0.24/0.76) -- 98% of rows have an ask >= 0.50,
and only **42 rows in 13 days** have an ask below 0.06. So the +2.31 cell rests on
executed prints, not on asks we could have paid. That is now the single biggest caveat
on the whole trade, and this dataset cannot resolve it. Our own recorder
(`tools/record_book.py`) is the only thing that can, and upstream has been quiet.

### On our own venue the same conditioning is 7x weaker and insignificant

Our store already had everything needed (`quotes.spot`, `quotes.strike`, `tau_s`, and
*named* `up_ask`/`down_ask`, so no symbol-mixing trap), 5,239 tickets from 292k quotes
over settled BTC rounds, same rule, fee `0.98/px` per $1 staked (verified against the
live UI: $1 at 0.03 shows +32.66):

| band | n | WR | price | WR/price | EV/$1 | t | day-clustered t |
|---|---|---|---|---|---|---|---|
| dist < 1 bp | 26 | 7.7% | 0.0515 | 1.49x | +0.57 | 0.5 | -0.39 |
| dist 1-2 bp | 94 | 5.3% | 0.0502 | 1.06x | +0.11 | 0.2 | +0.13 |
| dist 2-4 bp | 256 | 4.3% | 0.0453 | 0.95x | +0.01 | 0.0 | +0.28 |
| **dist < 2 bp** | **114** | **6.1%** | **0.0510** | **1.20x** | **+0.28** | **0.6** | **+0.31** |
| any distance | 589 | 3.6% | 0.0425 | 0.84x | -0.13 | -0.6 | +0.19 |

Both venues agree on one thing, and that is what was shipped: **past 2 bps there is no
edge anywhere** (Polymarket 0.90x and 1.25x, ours 0.95x). `ticket_max_dist_bps` went
4.0 -> 2.0, so the paper arm stops spending a $1 ticket on the half of its band that is
measurably worthless (467 of its 592 Polymarket tickets were 2-4 bps). The arm stays
**paper only**: on the venue we can trade it the effect is 1.20x at t 0.6, and reaching
t=2 on an effect that size needs ~1,500 tickets, which at the measured ~23/day is about
two months. That is the honest cost of finding out whether the edge exists where we can
actually reach it.

### Live state after the change (2026-09-12 10:54:54Z)

* band live: `ticket arm ON (paper only): $1.00/ticket, cheap ask 0.02-0.05,
  |S-K| 0.2-2.0 bps, tau 3-30s, max 400/day`; 224 tests green locally and on the VPS;
* the two tickets taken under the old 4 bp band both lost, one of them at dist 2.3 bps
  -- i.e. a ticket the new band refuses;
* balance $18.4787 -> **$55.3187** in 13 minutes (redemptions landing);
* since the feed-clock fast path went in: **1 TRIGGER, 0 acted decisions** -- the
  market has been flat (max 1s move 1.2 bps), so there has been nothing to trade.

### Newly surfaced: our live orders are being rejected by the venue

Not part of this work, but visible in the same log and more important than any of the
above if the intent is real money. The last two live attempts both failed, and the log
does not carry the venue's reason:

```
09:38:05 order 26091200001880692306 status=FAILED with no position held (Up)
09:38:09 order 26091200001880692255 unverifiable; venue holds no position; NOT retrying
09:43:59 order 26091200001880701966 status=FAILED with no position held (Down)
09:44:03 order 26091200001880702147 unverifiable; NOT booking and NOT retrying
```

`FAILED` with no position, twice per attempt, then "unverifiable" is the shape of a
venue-side rejection whose reason the client discards. The next diagnostic step is to
log the raw POST response body on failure, since the reason string is currently thrown
away.

## Why our live orders were rejected: the venue's *display* lags its own book (2026-09-12)

Chased to the ground. The venue does explain itself -- `errorMessage` in the
order-history record, which `_order_status` was reading and throwing away:

```
"status": "FAILED", "orderType": "MARKET",
"errorMessage": "Failed to execute the market order"
```

That message means the bundle's reference price was stale by more than
`slippageBps` (300 bps = 3%) when it executed. Our own 1 Hz record of the
venue's book shows exactly how stale:

```
btc-updown-5m-1789205700, decision instant (+0ms), BTC +4.9 bps in one second
  -983ms  spot=77335.99  dist=-1.81bp   up 0.14/0.15  (px 0.15)  down 0.85/0.86 (px 0.85)
    +0ms  spot=77374.01  dist=+3.10bp   up 0.55/0.60  (px 0.15)  down 0.40/0.45 (px 0.85)
  +6783ms  spot=77374.01  dist=+3.10bp   up 0.80/0.81  (px 0.81)  down 0.19/0.20 (px 0.19)
```

At the instant of the move the venue's **book had already repriced** (Up ask 0.60,
correctly favouring Up) while its **displayed `price` was still 0.15** -- stale for
about seven seconds, and the displayed price is what the quote/bundle path prices
against. So attempt 1 was quoted at ~0.15, executed against a book at 0.60, and FOK
refused it: 300 bps cannot span a 164% gap. Attempt 2, four seconds later, got a fresh
quote and filled at **0.81**.

### The dislocation is real, large, and we hand it back at execution

Priced against the digital fair at the trigger (the same math the decision uses), and
against the ask *on offer* in the venue's own book at that instant:

| trigger | side | ask offered | we paid | slip | EV at the ask | EV at the price paid |
|---|---|---|---|---|---|---|
| 09:38:05 | Up | 0.60 | FAILED | -- | +51% | rejected |
| 09:38:08 | Up | 0.60 | 0.81 | +0.21 | +51% | +12% |
| 09:43:56 | Down | 0.17 | FAILED | -- | +47% | rejected |
| 09:44:00 | Down | 0.17 | 0.84 | +0.67 | +47% | **-70%** |

Across every fill in the order history (15), the mean slip against the ask at the
trigger is **+0.078**. Split by order type it is not noise but a consistent property of
the path: the **8 MARKET fills averaged +0.16 above the ask on offer (6 of 8 above it,
the two impulse entries by +0.21 and +0.67)**, while the **7 LIMIT fills averaged
-0.014 -- every one of them at or better than the ask**. Same book, same venue, two
order types. Both of those failed rounds were then won by the venue's own record (Up at
0.81, Down at 0.84), so the cost was not a loss, it was the edge handed back: +51% EV
became +12%, and on the other round +47% became -70%.

This is the "latency" question answered with a twist: the *displayed* price is the
lagging thing, not the book. The book is tradable at the dislocation price the moment
it happens; only the quote/bundle path is pinned to the stale display -- and a LIMIT
order does not go through that path.

### Shipped for it (2026-09-12 11:01Z)

* `errorMessage` is now read, kept on the broker as `last_order_error`, and logged on
  every terminal failure -- a rejection can never again be an unexplained status;
* the verification patience was too short for the venue: order `...702147` was FILLED
  at +0.75s, yet order/history did not show it within the old 2s (4 x 0.5s) and the
  position list was still empty at 3.5s, so a real fill was declared unverifiable and
  **thrown away** -- `_order_status` now polls 6 x 0.6s and `_confirm_by_position`
  6 x 0.8s, which covers the measured lag;
* the conservative rule itself is unchanged on purpose: the phantom-fill test exists
  because booking on an unreadable status once lost real money, and absence from a
  lagging list is still not proof of absence. Two new tests pin the fixes (one proves a
  history read that answers on the 5th poll now books the fill instead of discarding
  it); 226 green locally and on the VPS.

### The change this points at (not shipped -- it moves real money)

Impulse entries should be a **LIMIT priced from fair value**, not MARKET-FOK. The
evidence says the book is reachable at the dislocation price while the bundle path is
pinned to the stale display, and `buy_limit()` already implements the mechanics
(GTC + wait + cancel + fill price from the venue's own record). The existing
`order_type=limit` is deliberately maker-first (`sig.quote - limit_offset`, "a miss ends
the round"), so it is not this trade. A taker limit at `fair - safety_margin` would have
filled at 0.60 instead of 0.81 -- and cannot fill worse than its own limit by
construction, which a `slippageBps` cap on a market order cannot promise. Open question
worth recording: we have never observed the *depth* behind a dislocation ask, so the
first thing to log is sizes at the level, not just the price.

### While investigating: the ticket arm's lottery ticket actually hit

Found in the same sweep. The paper arm (band then 0.25-4.0 bps, tau 3-30s) fired three
times before the 11:01 restart, and the first one won:

```
fills:        btc-updown-5m-1789209300  Up @0.02  $1.00 -> 50.0000 shares, fee $0.02
settlements:  WON  payout $50.00  pnl +$48.98   (entry 3.3s before the round ended)
```

That is the 50x ($1 -> $50) at the venue's 0.02 floor -- the trade this project has been
chasing since the beginning, on our own venue, taken by the arm that exists to measure
it. The accounting checks out exactly (`shares = 1/px`, `payout = shares`, `pnl =
payout - stake - fee`), and the rows are durable: `settlements WHERE mode='ticket'` is
the ledger. The live stat line's "N today" is in-memory and resets on restart, so the
arm's statistics must be read from the DB, never from the log line.

Two honest caveats, both of which matter:

* the arm is now **1 win / 2 losses, net +$46.94 over 3 tickets**. One binary outcome at
  a 50x payoff swamps three tickets, which is exactly why the plan needs hundreds -- the
  Polymarket estimate of the same condition is 2.57x on 243 tickets, and 243 tickets is
  what it takes to make a statement;
* the winner came in at **tau = 3.3s**, the sub-window the 14-day study measures as the
  *worst* (0.58x, n=128, negative on 3 of 10 days). It is not evidence for the last-3
  seconds; it is one draw. The edge in the data lives at tau >= 10s, where the venue has
  not repriced yet.

### The ticket arm went LIVE on 2026-09-12 11:12:42Z (explicit instruction)

`ticket_live=true`, `ticket_live_max_per_day=10`. It is not switched to real money by
copying the paper code; it goes through the main arm's own order path
(`_place_order` -> `buy` -> `_book_fill` -> `settle_due` -> `redeem`), so the risk
guard, the re-verification, the settlement and the redemption all apply, and there is
one real-money code path rather than two. Two consequences that are deliberate: a live
ticket **takes the round** (guard `max_open_rounds=1`, so the main arm cannot double up
in it), and a live fill is booked as `mode='live'` -- attribute it by joining
`fills.decision_id` to `decisions.reason LIKE 'ticket %'`, or live performance stats
will silently pool two different strategies.

Two gates in the shared order path had to be made skippable for it, and the second one
would have made the whole thing a no-op:

* the **model re-check** has no fair value to use -- the arm's premise is that it does
  not know the fair;
* the **candle-open gate** (`trade_on_candle_open`) is the exact *opposite* of this
  trade: a ticket fires 3-30s before the bell, i.e. 4-5 minutes into a 5m candle, so
  with that gate enforced every live ticket would be withheld while still writing its
  `decisions` row. The arm would have looked like it was trading and never placed a
  single order. Both skips are pinned by tests, and only those two decision gates are
  skipped -- the retry loop's venue-position check and the booking path are untouched.

**What was shipped against, stated plainly so it can be judged later:** this is a bet on
an unproven edge. Our venue: <2bp 1.20x (EV +0.28, t 0.6, n=114); the floor band
(<= 0.025) 3.27x break-even on **2 wins in 30** (day-clustered t 0.43); ~1.0x at every
other distance, i.e. the venue is efficient where the arm is not betting. Polymarket's
2.57x (t 3.2, n=243) is a venue we cannot trade. The rail is the cap: **10/day = $10 of
exposure, 18% of a ~$55 account in the worst case.** The expected value over a week sits
somewhere between +$20 (if 1.20x is real) and -$40 (if the true rate is the 0.95x the
2-4bp band showed), and the difference is one or two coin flips.

## The taker LIMIT: measured, then shipped (`order_type=limit_take`, 2026-09-12 11:23Z)

The market path is priced against the venue's **displayed** price, and the display lags
the venue's own book. Two independent readings of one clock decide it:

* a probe of 335 live samples (display / REST book + size / `get-quote` for $1) found the
  **quote above the book's ask in 5-7% of samples, by up to +0.40 (75% of the price)**,
  i.e. exactly during the moves this arm fires on. The display lagged the book by >2c in
  13-16%. And in the cheap region the market path paid 12-25% above the ask;
* **one real $1 order** on `eth-updown-5m-1789211700` (a market the bot does not trade,
  so nothing could be confused): displayed 0.15, book 0.13/0.15, limit posted at 0.18 --
  **filled at 0.1526**, i.e. at the BOOK'S ask. A crossing limit therefore reaches real
  liquidity, and cannot pay more than the price we chose, which a `slippageBps` cap on a
  market order cannot promise.

Corroborating: our own record has 8 MARKET fills averaging **+0.16 above the ask on
offer** (6 of 8 above it) against 7 LIMIT fills averaging **-0.014**. So `limit_take`
went live, and the impulse arm now computes a real digital fair at the trigger
(`fair_value(spot, strike, tau, sigma)`) to use as the ceiling -- pricing that limit off
the displayed price instead would have refused the very dislocations the arm exists to
take (display 0.15, book 0.60, digital 0.91: a 0.15 limit fills nothing, a 0.90 limit
fills at 0.60).

## Audit of live execution (2026-09-12)

Fixed, with a test each:

1. **[HIGH] the venue's rejection reason was discarded.** `errorMessage` on the
   order-history record is the only place the venue says why; `_order_status` read
   `status` and dropped the rest, so every rejection before today was an unexplained
   bare status.
2. **[HIGH] the verification budget was shorter than the venue's own lag.** Order
   `...702147` was FILLED at +0.75s, yet order/history did not show it within 2s and the
   position list was still empty at 3.5s -- so a real fill was declared unverifiable and
   thrown away. Now 6 x 0.6s and 6 x 0.8s.
3. **[HIGH] the ticket's daily cap lived in memory.** Every restart handed the arm a
   fresh $10 budget, so a restart loop multiplied the one rail keeping it survivable.
   Seeded from `decisions` at startup now.
4. **[HIGH] the ticket passed its own ask as P(Up) regardless of side.** Harmless while
   `fair_up` was unused, fatal under a taker limit: a Down ticket would have gone out
   with a limit near 1 - ask (0.95 on a 0.04 ticket). Caught while wiring the limit.
5. **[MED] `buy_limit` read `marketProviderFee` as dollars when it is SHARES**, so every
   limit fill over-reported its fee by 1/px -- 6.7x on the ETH order above (0.111 shares
   on a $0.83 stake is the advertised 200bps; as dollars it is 13.4%), and 20-50x at
   ticket prices. The market path always converted (`fee_shares * avg_px`); only this one
   did not.
6. **[MED] the limit wait could outlive the round** -- 20s configured, against a ticket
   that fires 3-30s before the bell. Capped by the remaining tau.
7. **[MED] two gates would have silently blocked every live ticket**: the model re-check
   (no fair exists for it) and the candle-open gate, which is the exact opposite of a
   ticket -- it fires 4-5 minutes INTO a 5m candle. The arm would have looked like it was
   trading and never placed an order.
8. **[LOW] the stats line mislabelled ticket activity** in live mode (it printed nothing,
   because the paper broker no longer exists); it now reports `ticket ledger (LIVE):
   n/cap today`.

Checked and found sound -- no change made: the retry loop's venue-position check before
re-placing (and `buy_limit` does not need one: a limit fill comes from the venue's own
order record with a `fillPercentage`, not from a 200-with-FAILED, so the phantom shape is
impossible); `_restore_open_positions` re-opening the round in the guard, so a restart
cannot double-buy; `_seed_guard_state` being day-scoped and including live tickets;
`batch-cancel` using `http.client` deliberately (percent-encoded brackets break the
signature); and the 05:00 burst of 37 `no SELL quote` errors turning out to be an unwind
path that no longer exists in the tree -- dead code, not a live bug. The two wins whose
fills were thrown away on 09:38/09:44 (+$0.42, both rounds won) reconcile themselves:
`_reconcile_bankroll` adopts a higher venue reading immediately, so the ledger catches up
at the next settlement without a hand-written backfill.

Still open, stated rather than hidden: **depth at the ask is unmeasured** -- the venue's
book reports `qty` 1 at every level, which cannot be the real depth when our own fills
have been 1.5-5.4 shares, so a taker limit's fill rate is still a guess. `_place_order`'s
30s cooldown is shared, so a rejected ticket pauses the main arm too. And a live ticket
books as `mode='live'`, so it must be attributed via `fills.decision_id` joined to
`decisions.reason LIKE 'ticket %'` or live statistics will pool two strategies.

## Backtest: the impulse rule replayed on the recorded book (`tools/backtest_impulse_orders`)

`quotes` carries both prices for the same second -- `up_price`/`down_price` (the venue's
DISPLAYED price, what a MARKET order is priced against) and `up_ask`/`down_ask` (the real
top of book) -- so one entry can be priced three ways on the same tick. 96 impulse
entries over 1,112 settled rounds (one per round, first trigger, tau 60-210), $1 stake,
the venue fee, the real winner:

| leg | n | fill | WR | mean px | EV/$1 | t |
|---|---|---|---|---|---|---|
| market | 96 | 100% | 58.3% | 0.5376 | +0.2510 | 1.37 |
| limit@ask | 96 | 100% | 58.3% | 0.5375 | +0.2512 | 1.37 |
| **limit@fair** | 60 | **62%** | 61.7% | 0.5432 | **+0.3448** | 1.55 |
| ~~display~~ | 96 | -- | -- | 0.3903 | *+1.0652* | *3.33* |

Three things to read out of it:

* **the fair ceiling is what the change buys: +0.094/$1, +37% relative**, by refusing 38%
  of entries -- and a MARKET order cannot express that at all, because it has no
  valuation to refuse against;
* **the `display` row looked like +1.0652 and is not implementable.** It was in the first
  version of this tool as a "market" leg and it is a trap of my own making: you cannot buy
  below the best ask, and the venue refused exactly that twice on 2026-09-12 (ask 0.60
  against a 0.15 display; 0.17 against 0.16). The market path's real cost is the ask PLUS
  the measured retry drift (+0.16 over 8 fills), so its true row is worse than
  `market` above -- which is why the honest comparison is limit@ask vs limit@fair;
* **the entry cost is the whole game.** At a mean ask of 0.5375 the break-even win rate is
  54.9%; the rule delivers 58.3%. The 0.16 drift the market path pays is ~2/3 of the
  entire edge, which is why the fill price, not the trigger, decides this strategy.

The earlier replay in this file measured the same rule at +0.1145/+0.0815 (1,641 rounds,
net of a 1.32c slip); this sample is smaller and nets nothing, so the two are not the same
number and neither is significant on its own.

## Second audit of live execution (2026-09-12, after the taker limit went in)

1. **[HIGH] the ceiling had no floor, so a stale-high display could lift the limit above
   our own valuation.** `max(quote, fair - margin)` is wrong in exactly the case the
   ceiling exists for: the venue reprices BEFORE us, so the display reads 0.90 while our
   fair (minus margin) is 0.86 -- the limit would then authorise a fill above fair, i.e. a
   negative-EV entry, taken by the arm the ceiling is there to stop. The limit is now the
   valuation and nothing else; an arm with no valuation of its own passes `take_px`, which
   floors it (the ticket passes its own ask). Two tests, one named for the bug.
2. **[HIGH] limit fills carried no `token_id`, and `fills` had no column for it.** Two
   consequences, one of them money: `bind()` cannot persist what it does not have, and
   `settle()` verifies an *unverified* fill by asking the venue's position list whether it
   holds that token -- with `token_id` None the question is unanswerable, the check
   concludes the venue holds nothing, and after 20 attempts it **voids a real position**:
   stake spent, PnL zero, the round's result missing. The market path always set it; the
   limit path did not, and the ticket arm now trades exclusively through the limit path.
   Fixed at all three points (fill construction, schema migration, both restore sites)
   with two tests.

Checked and found sound: `Store`'s WAL + `synchronous=NORMAL` + 30s busy timeout for one
writer and concurrent readers; `upsert_round`'s `COALESCE(excluded.c, rounds.c)` so a late
poll that lost `strike` cannot erase a good one; the `_migrate` pattern for adding columns
to a database that already exists; and the spot feed's freshness check being on arrival
time, which is the right choice for detecting a dead socket (a stale spot is also
recoverable after the fact because every row stores `spot_ts_ms` next to `spot`).

Noted, not changed: under `limit_take` the pre-trade MARKET quote is fetched and then not
used by `buy_limit`, which fetches its own LIMIT quote -- one wasted round trip (~5-10 ms
of a multi-second window) kept only because its `averagePrice` is what the audit trail
logs. And `decisions.fair` is a probability for the model path but the arm's own price for
impulse and ticket rows, which the schema comment does not say.

## Can the ticket be made to fire only on "very certain" moves? No -- measured (`tools/ticket_filters`)

Asked for directly, so it got a proper search over 5,388 candidate quotes in 596 rounds,
seven axes, with the two tests that killed earlier claims (both halves, day-clustered t,
drop-the-best-day) reported for every row.

First, the reframe that matters: "certain" cannot mean the model likes it. At those
entries the digital fair for the cheap side is 0.0005-0.02 -- it prices the venue as
*generous* -- and the ticket that won was a 3-sigma move. The only lever is finding a
cohort where the venue's price is more wrong.

| cohort | n | WR / price | pooled t | daily t | drop best day |
|---|---|---|---|---|---|
| live now: px 2-5c, dist<=2bp | 102 | 1.35x | +0.89 | +0.80 | **-0.01** |
| px<=2.5c (floor), dist<=2bp | 30 | 3.33x | +1.02 | +0.43 | **-1.00** |
| .. + dist<=1bp | **3** | -- | -- | -- | -- |
| .. + tau>=10s | **0** | -- | -- | -- | -- |
| floor, any distance | 372 | **1.08x** | +0.15 | +0.37 | -0.47 |
| **cheap side already IN THE MONEY** | **1** | -- | -- | -- | -- |
| z 1-2 (behind ~2-4bp) | 253 | **0.42x** | **-2.76** | -0.47 | -0.79 |
| high vol, px<=3c | 251 | **0.44x** | -2.72 | **-3.52** | -0.82 |
| behind 1-2bp, px<=3c | 51 | 1.35x | +0.53 | -0.11 | **-1.00** |

Everything that concentrates the win rate collapses to n<5 or rides one day. The decisive
one: **the cheap side being already in the money -- the single setup that would be a
genuine mispricing, a winner still quoted as a longshot -- happens ONCE in 596 rounds.**
This venue reprices winners properly. There is no certain-move filter to find here.

The best-determined positive cohort is the floor price at any distance: 1.08x on 372
tickets, both halves +0.05. That is **about +$0.05 per ticket, i.e. nothing.** The ticket
is a *paid lottery ticket*, not an edge.

And the search found the valuable thing by accident: the venue **overcharges** near-strike
longshots -- px<=3c with the side 2-5bp behind is 0.81x, and the high-vol version is
0.44x at t -2.72 with a daily t of -3.52. The favourite is the value side of the exact
setup the ticket fishes in, which is what the impulse arm already buys, and it corroborates
the older finding that this venue understates favourites by 1.24x on the log-odds.

### Shipped for the risk, not the edge

Since no filter improves the cohort, the only available improvement is to bound the bleed:
`ticket_max_daily_loss_usdt = 3.0`, read from the DATABASE (a restart must not hand the
arm a fresh budget, same as the count cap) and cached 30s because the check runs on every
websocket tick. Worst case falls from $10/day (18% of the account) to **$3/day (5.5%)**,
and a winless week from -$70 (the account) to **-$21**. A win lifts the day's P&L far
above the stop, so winning days are unaffected. With that in place the count cap barely
binds and stays at 10.

## Who knows better: the venue's price or our spot? The venue, decisively (`tools/venue_vs_spot`)

Written to answer a screenshot: with 14s left the chart sat ON the strike (77349.44) while
the venue priced Up 0.99 / Down 0.03, and that looked like a 30x the venue was handing
over. It is not. This venue settles on **Chainlink**, so its reference can sit several bps
from the Binance price on the chart, and the settlement follows its reference. Measured on
1,116 rounds at tau=10s, scoring both predictors against the real outcome:

| predictor | n | Brier (lower better) | log-loss | accuracy |
|---|---|---|---|---|
| **the venue's price** | 493 | **0.1063** | **0.3529** | 85.2% |
| our spot (digital) | 493 | 0.1240 | 0.5654 | 85.6% |

The venue is not just better on the sharpest metric (log-loss 0.35 against 0.57) -- it is
CALIBRATED, bucket by bucket:

```
venue says 0.00-0.05 -> realised 0.037   (claims 0.030)
venue says 0.20-0.40 -> realised 0.273   (claims 0.265)
venue says 0.80-0.95 -> realised 0.885   (claims 0.890)
ours  says 0.00-0.05 -> realised 0.103   <- 8x out
ours  says 0.60-0.80 -> realised 0.873   <- our digital UNDERESTIMATES favourites
```

And the screenshot's exact setup -- our spot within 1bp of the strike while the venue
prices that side at <=5c -- occurs 6 times in the sample: the cheap side won **0**. At the
venue's implied 3.5%, P(0 wins in 6) = 0.81, so that is what a fairly-priced 30x looks
like, not a missed edge.

**This is why the ticket was never going to work, and it was measured before it was
obvious:** the arm traded AGAINST the best available estimator (the venue's price) using
the worst (our spot's digital), and paid the ask for it. The venue's longshot prices are
honest, so a ticket is a fair bet bought at a premium -- which is exactly the 1.08x on 372
floor tickets, and the 0.42x (t -2.76) in its most common band. **The ticket arm was
switched off on 2026-09-12** (`ticket_enabled=false`; the code, the ledger and every
measurement stay in place, so it re-arms with one line if the venue's calibration ever
changes).

The same table says where the money actually is: our digital calls a side 71% and it wins
**87%** -- our own model is the thing that understates favourites. That is the direction
the impulse arm already trades, and where its +0.25/$1 at the ask comes from.

## The ticket was FIXED, not switched off: the venue is wrongest at 0.08-0.20

The section above concluded "the venue's longshot prices are honest" -- true **at the floor**,
and false as a generalisation. Sweeping the whole curve (`tools/longshot_bands.py`, 1,117
rounds, priced at the executable **ASK**, both sides, every round) shows the mispricing is a
**monotone hump**, and the ticket had been fishing in the *worst* part of it:

| ask band (tau=10s) | n | ratio | EV/$1 |
|---|---|---|---|
| 0.02-0.03 | | 0.19x | -0.813 (pooled t -6.16) |
| 0.03-0.05 | | 0.36x | -0.676 (pooled t -3.60) |
| 0.05-0.08 | | 0.99x | ~0 |
| **0.08-0.12** | | **1.59x** | **+0.673** |
| **0.12-0.20** | | **1.21x** | **+0.155** |
| 0.20-0.35 | | 1.04x | +0.04 |
| 0.35-0.50 | | 0.79x | -0.209 |

So the arm was not "a fair bet bought at a premium" -- it was buying the one cell the venue
prices *wrongly in its own favour*. The correct reading of the calibration table above is
narrower than the earlier conclusion: the venue's price is a better estimator than our spot
**on favourites and on the floor**, and it is *too cheap* in the 0.08-0.20 strip.

### The band survives an out-of-horizon check (the fit was at tau=10s)

Same cells, re-measured at horizons the specification was **not** fitted on:

| band | tau=3s | tau=5s | tau=10s | tau=30s |
|---|---|---|---|---|
| 0.02-0.05 | **0.10x**, EV -0.910, t -13.64, daily-t -7.12 | **0.15x**, EV -0.898, t -15.18, daily-t -11.38 | 0.19-0.36x | -- |
| 0.05-0.08 | 0.27x, EV -0.785, t -3.67 | **0.00x -- zero winners in 69** | 0.99x | -- |
| 0.08-0.12 | 1.22x, EV +0.305, t +0.60 | **1.83x**, EV +0.856, t +1.51 | 1.59x | -- |
| 0.12-0.20 | 1.19x, EV +0.144, daily-t **+2.54** | 1.16x, EV +0.179 | 1.21x | -- |
| 0.08-0.20 (whole) | | | 1.37x | **1.03x** |

The cheap tail is negative at *every* horizon -- and catastrophic at short ones (a 0.24%
win rate against a 2.40% break-even). The 0.08-0.20 strip is positive at *every* horizon,
and only decays to 1.03x by 30s, which is what the 20s tau cap is for. This is the first
ticket specification to survive all five tests: both halves positive (+0.25/+0.62), day-
clustered t **+2.15**, drop-the-best-day **+0.34**, and a sign that holds out of horizon.

### Shipped specification (2026-09-12 11:55Z)

`ticket_min_price=0.08`, `ticket_max_price=0.20`, `ticket_min_tau_s=3`, `ticket_max_tau_s=20`,
distance filter **off** (`0.0-10000.0` -- the edge is a property of the price band, and the
distance filter measured worse), `ticket_max_daily_loss_usdt=3.0`, `ticket_live_max_per_day=10`,
$1/ticket, one round at a time. Expected EV at 1.37x is **+$0.44/ticket** on n=107.

**Stated honestly: still UNPROVEN** (pooled t +1.34; the daily-clustered t of 2.15 carries the
significance, not the pooled one). It is a value bet with a 5-12x payoff shape, not a 50x
lottery ticket, and the $3/day loss stop and `guard.check` remain as the rails.

### The one 50x we did win was the outlier, not the edge

Live on 2026-09-12, the arm (still on the OLD 0.02-0.05 band) fired 3 tickets at 0.02/0.03/0.03
and finished **+$46.94**, from a single 0.02 fill that paid 50x (50 shares on a $1 stake, at
tau=3s). That reads as vindication of the lottery thesis; it is the opposite. In exactly that
cell, at exactly that horizon, 2 of 820 attempts won (0.24%) against a 2.40% break-even -- we
were paid for a **1-in-400 event**, and the mean of the cell is **-$0.91 per ticket**. The two
0.03 tickets we lost alongside it are what that band actually does. The 50x payoff is real; the
*rate* at which it arrives is not enough to pay for it.

# What each live arm has ACTUALLY done (2026-09-12, from the DB not the log)

`tools/live_status.py` and `tools/live_ev_calibration.py` were written because every
"the arm is behaving" check until now was an ad-hoc query that re-derived the schema
each time (there is no `size_usdt`; the stake column is `stake`) and kept missing the
fact that `decisions` logs EVERY poll's reason, so 16,939 "nothing to do" rows had to be
filtered out before the entries were visible.

## The ledger, by arm, settled and non-void

| arm | n | staked | P&L | per fill | return on stake | days |
|---|---|---|---|---|---|---|
| model (`reason='ok'`) | 399 | $1,310.82 | **+$63.15** | +$0.158 | **+4.82%** | 5 |
| impulse | 1 | $1.00 | +$0.42 | — | — | 1 |
| pair (arb, NOT in this repo) | 46 | $54.28 | **−$1.93** | −$0.042 | **−3.56%** | 1 |
| ticket | 3 | $3.00 | +$46.94 | — | — | 1 |
| all | 449 | $1,369.10 | +$108.59 | +$0.242 | +7.93% | 5 |

The `pair ... disc` entries are placed by something outside this repo (no source match
for the reason string anywhere in the tree) writing to the same DB. They stopped at
06:40, so there is nothing here to change -- but the arithmetic is worth stating: a pair
costing 0.99 pays 1.00 and costs 2% in fees on each leg, so it is **−1% by construction**,
and its realised WR is exactly 50.0%.

## The model arm's own EV estimate is ANTI-informative (n=121, 2 days)

`ev_at_fill` is `expected_value(fair, filled_px, fee)` -- already per $1 staked. Bucketed
by it, with the realised side divided by the stake (the units bug: `pnl` is per fill and
stakes ran $0.70-$2.26, $3.29 average, so an unnormalised table turns a sizing rule into a
phantom edge -- the first version of this table claimed the 0.65-0.80 band lost $0.10 a
fill; normalised it returns +1.19%, i.e. zero):

| predicted EV/$1 | n | realised EV/$1 | WR | pooled-t | daily-t | drop1 |
|---|---|---|---|---|---|---|
| +0.032 (q1) | 24 | **+0.254** | 87.5% | +2.39 | **+2.95** | +0.39 |
| +0.047 (q2) | 24 | +0.086 | 70.8% | +0.57 | +1.38 | −0.00 |
| +0.057 (q3) | 24 | −0.176 | 58.3% | −1.20 | −4.69 | −0.11 |
| +0.068 (q4) | 24 | −0.006 | 70.8% | −0.04 | −0.67 | +0.05 |
| +0.107 (q5) | 25 | **−0.244** | 52.0% | −1.67 | −1.67 | −0.04 |

The gate's own ranking is upside down at the extremes. Mechanism: `ev` is computed from
OUR fair, and our digital is the miscalibrated estimator here (it says 0.00-0.05 -> realises
0.103, and 0.60-0.80 -> 0.873). A claimed edge of 11c a dollar is a 40-point disagreement
with a venue that is *better calibrated than we are* -- so it is mostly our error, and it
realises −24%. This is independent corroboration of the decision already made in
`entry_mode=impulse` ("the model is not consulted"), and it is why the impulse arm is right
not to re-price against the model.

## The model arm's profit is in the CHEAP half of its own band

| entry px | n | realised EV/$1 | WR | pooled-t | daily-t | drop1 |
|---|---|---|---|---|---|---|
| 0.00-0.45 | 29 | +0.159 | 37.9% | +0.58 | −0.50 | +0.23 |
| **0.45-0.55** | 34 | **+0.342** | 67.6% | **+2.14** | **+3.03** | **+0.50** |
| 0.55-0.65 | 86 | +0.067 | 65.1% | +0.80 | −1.31 | −0.33 |
| 0.65-0.80 | 176 | +0.012 | 72.7% | +0.25 | −0.57 | −0.04 |
| 0.80-1.01 | 74 | −0.004 | 85.1% | −0.08 | +0.15 | +0.11 |

Everything above 0.65 is indistinguishable from zero: the arm's whole edge sits at
0.45-0.55, where it wins 67.6% of a near-coinflip.

## SHIPPED: `impulse_max_price` 0.95 -> 0.75

`tools/backtest_impulse_orders --by-price` (96 entries / 1,123 rounds, one entry per round
at the first trigger, priced at the ask, restricted to what the live path takes,
`ask <= fair - 0.04` -- live 2026-09-12 12:4xZ):

| ask paid | n | EV/$1 | tot | pooled-t | daily-t | halves | drop1 |
|---|---|---|---|---|---|---|---|
| 0.00-0.50 | 23 | **+1.0568** | +24.31 | +2.03 | **+4.40** | +0.57/+1.50 | +1.30 |
| 0.50-0.65 | 9 | +0.2971 | +2.67 | +1.28 | +2.22 | +0.25/+0.33 | +0.25 |
| 0.65-0.75 | 5 | +0.1062 | +0.53 | +0.43 | −0.21 | −0.33/+0.40 | −0.32 |
| **0.75-0.85** | 4 | **−0.0813** | −0.33 | −0.31 | −0.55 | +0.22/−0.39 | −0.39 |
| **0.85-1.01** | 5 | **−0.1035** | −0.52 | −0.52 | +0.07 | +0.15/−0.27 | −0.07 |

The two cells above 0.75 are the only negative ones anywhere in the decomposition, and the
reason is arithmetic rather than fitted: the fee fixes a break-even win rate of `px/0.98`,
so at 0.85 a win pays +0.15 and needs 87% of them. Those entries are also exactly where our
fair claims its largest edge -- the inflation measured above. Small in dollars (+$0.85 over
46 entries) but it removes the only structurally dead part of the band, and it cuts the
risk per dollar there by 5x.

**Not shipped, and why:** `--by-move` says the 4-5bps impulses (58% of the volume) are the
weakest cell (EV/$1 +0.096, daily-t +0.91, drop1 −0.02) while 5-6bps pays +0.576 (daily-t
+3.24) and 6-8bps +0.394. Tempting to raise `impulse_bps` from 4.0 -- but those 4-5bps
entries are still POSITIVE and worth +$5.40 of the sample's +$24, so raising the threshold
would give up real money to improve an average. The entry count is not the constraint here;
capital is not the constraint either.

## Raising the ticket's WIN RATE: two conditioners, and both are mechanisms

A longshot's win rate is trivially buyable with price -- buy at 0.40 and you win 40% -- so
"raise the win rate" is only a real question at a FIXED price. Inside the shipped cell, the
price split says exactly what you would expect and nothing more:

| ask paid | n | WR | ratio | EV/$1 | daily-t |
|---|---|---|---|---|---|
| 0.08-0.10 | 23 | 13.04% | 1.52x | +0.598 | +0.67 |
| 0.10-0.13 | 43 | 16.28% | 1.49x | +0.499 | +0.63 |
| 0.13-0.16 | 34 | 26.47% | 1.82x | +0.832 | +2.56 |
| 0.16-0.20 | 30 | 20.00% | 1.13x | +0.115 | +0.84 |

`tools/longshot_bands` now takes `--split` (and `--min-z` / `--min-div` to combine), which
re-cuts the fixed population of entries the live rule would take and runs the full battery on
each bucket. Two conditioners survive it, and the pair together is the shipped rule:

| gate | n | WR | ratio | EV/$1 | pooled-t | daily-t | drop1 |
|---|---|---|---|---|---|---|---|
| (none) 145 entries | 145 | 17.93% | 1.30x | +0.392 | +1.51 | +2.08 | +0.30 |
| `z >= 0.8` | 91 | 24.18% | 1.83x | +0.902 | +2.43 | **+4.19** | +0.78 |
| `div >= 0.05` | 39 | 28.21% | 1.90x | +0.966 | +1.80 | +1.97 | +0.81 |
| **`z >= 0.8` AND `div >= 0.05`** | **28** | **32.14%** | **2.32x** | **+1.330** | +1.93 | +1.95 | **+0.83** |

* **`div` = the book's ask minus the venue's own DISPLAYED price for that side.** Positive
  means the display sits below its own book -- the venue is stale, which is the same lag the
  impulse arm is built on (measured up to 0.40; the display lagged the book by >2c in 13-16%
  of 335 samples). `div < -0.02`, a display ABOVE the book, is what a *current* venue looks
  like and measures **0.92x**: when the venue is up to date there is no ticket to buy.
* **`z = |ln(S/K)| / (sigma_1m * sqrt(tau/60))`** -- how many sigma of the move still to come
  separate the spot from the strike (`pricing.digital.moneyness_sd`, the same quantity
  `_impulse_tick` computes inline for its sizing tilt). Under 0.8 the cells are **0.42x /
  0.00x / 0.75x** -- every one negative -- and it holds *inside* a tighter 0.08-0.16 band
  (1.90x), so it is not a proxy for the price paid.

**Why the win rate is worth buying here, beyond EV:** the arm's own `$3/day` stop halts it
after three consecutive losses -- 44% likely at a 24% win rate, 31% at 32% -- so a higher rate
literally means the arm gets to its wins more often. Cost of both gates: ~2.5% of rounds
qualify (~7/day) instead of 8% (~23/day), so the 10/day cap stops being binding.

**Rejected, with the numbers:** a `side_filter` of Up (1.65x, EV +$0.796, daily-t +2.54)
against Down (0.97x, EV +0.004) is a bet on the regime the sample happened to be in, not a
mechanism -- the repo's own `up_regime_gate` shows the same asymmetry in the *opposite*
direction for the model arm. A recent-move filter (`ret1`) has no signal (flat |ret|<5bps
1.41x, after a 5-20bp rise 1.14x, after a 5-20bp fall 0.00% on n=7). A book spread >= 0.10 is
negative in both halves (0.63x, daily-t -2.57): paying a wide spread is how the edge is lost.



## Two more rooms, found and shipped (2026-09-12 12:09Z)

### 1. The ticket's ceiling WAS its entry time (tau <= 20s -> 8s)

`ticket_max_tau_s` is not a filter, it is the entry time. The arm polls ~33/s and buys the
FIRST quote whose cheap ask is in the band, so a 20s ceiling means it enters at a median tau of
18s -- the earliest, and worst, moment in the window. `tools/longshot_bands --window` now
simulates that exact rule (1,119 rounds, priced at the ask, fee paid):

| ceiling | n | ratio | EV/$1 | pooled-t | daily-t | halves | drop-best-day |
|---|---|---|---|---|---|---|---|
| 20s (was live) | 289 | 1.11x | +0.218 | +1.24 | +1.01 | **-0.25**/+0.68 | +0.02 |
| 15s | 251 | 1.12x | +0.139 | +0.77 | +0.78 | -0.17/+0.45 | -0.07 |
| 12s | 211 | 1.29x | +0.332 | +1.58 | +1.93 | +0.04/+0.62 | +0.18 |
| 10s | 186 | 1.27x | +0.341 | +1.50 | +1.48 | -0.11/+0.79 | +0.12 |
| **8s (live)** | 145 | 1.30x | **+0.392** | +1.51 | **+2.08** | +0.03/+0.75 | **+0.30** |
| 6s | 122 | 1.27x | +0.383 | +1.32 | +1.08 | +0.16/+0.61 | +0.12 |

20s was the ONE row that failed the halves test, i.e. the shipped setting was the weakest in
its own sweep -- and every number reported for it so far (1.37x, +$0.436/ticket, daily-t 2.15)
had come from a **tau=10s SNAPSHOT, which is not the rule the arm executes**. Under the rule it
does execute, 8s takes EV/ticket from +$0.218 to **+$0.392** with daily-t 2.08 and
drop-best-day +0.30. The mechanism is the one this file keeps rediscovering: the venue's DISPLAY
lags its own book by ~7s, so the closer to the bell, the staler the price we cross against --
which is why the band decays to 1.03x by tau=30s and why 20s was the worst place to enter.

### 2. `limit_take` was crossing far too readily (margin 0.01 -> 0.04)

The take ceiling (`ask <= fair - margin`) swept 0.00-0.10 -- `tools/backtest_impulse_orders
--sweep-margin`, 96 entries / 1,112 rounds, $1 an entry, the full battery on every row:

| margin | n | fill | WR | EV/$1 | tot$ | pooled-t | daily-t | halves | drop1 |
|---|---|---|---|---|---|---|---|---|---|
| 0.00 | 65 | 68% | 61.5% | +0.319 | +20.8 | 1.53 | 3.44 | +0.25/+0.39 | +0.37 |
| 0.01 (was live) | 60 | 62% | 61.7% | +0.345 | +20.7 | 1.55 | 3.73 | +0.20/+0.49 | +0.37 |
| 0.02 | 57 | 59% | 63.2% | +0.398 | +22.7 | 1.72 | 4.29 | +0.25/+0.54 | +0.41 |
| 0.03 | 54 | 56% | 64.8% | +0.441 | +23.8 | 1.82 | 4.68 | +0.30/+0.59 | +0.44 |
| **0.04 (live)** | 46 | 48% | 65.2% | **+0.580** | **+26.7** | 2.09 | 6.03 | +0.43/+0.73 | +0.56 |
| 0.06 | 37 | 39% | 70.3% | +0.694 | +25.7 | 2.19 | 7.98 | +0.69/+0.69 | +0.61 |
| 0.08 | 35 | 36% | 74.3% | +0.791 | +27.7 | 2.42 | 8.26 | +0.79/+0.79 | +0.70 |
| 0.10 | 28 | 29% | 75.0% | +0.969 | +27.1 | 2.46 | 6.25 | +0.93/+1.01 | +0.76 |

Monotone in win rate, EV, day-clustered t and BOTH halves -- and the win rate rises while the
average price FALLS (0.543 -> 0.453), which is the signature of signal rather than of a filter
fitted to noise. The edge is concentrated in the entries where the ask is far under fair, which
is what a venue whose price lags its own book should look like. `tot` (what the whole sample
pays at $1 an entry) peaks across 0.04-0.08 and those rows are within noise of each other, so
0.04 was chosen for its sample size (48% fill, ~9 entries/day) rather than for the peak.

**It had to become its own setting.** `safety_margin` is ALSO the signal's hurdle
(`edge/signal.py`: `hurdle = breakeven - q + safety_margin`), so raising it to 0.04 would have
quietly raised the entry gate by 3 points of probability -- a large, unmeasured change smuggled
in alongside a measured one. `limit_take_margin` is now the ceiling and `safety_margin` stays
the hurdle at 0.01; `test_the_hurdle_and_the_take_ceiling_are_separate_knobs` pins both
directions (a 9% hurdle must not move a 1% ceiling, and vice versa).

Both changes went live together: ticket window `tau 3-8s`, take ceiling `0.04`, obstacle
hurdle unchanged at `0.01`, 245 tests green locally and on the VPS.

# Which pricing MODEL is worth keeping (53-day corpus, 2026-09-13)

The question was "which models should stay enabled". The corpus in `data/btc5m/data/` answers it
in two halves, and the two halves disagree in an instructive way.

## The instrument

15,228 BTC 5-minute rounds, 2026-04-29 to 2026-06-20 (53 days), each carrying

* `ticks_dense.parquet` -- the 1-second Binance tape (15,240 windows, 300 rows each);
* `events[0].eventMetadata.priceToBeat` / `finalPrice` -- the **Chainlink strike and final**.
  The market resolves Up iff final >= strike, so the label is the SETTLEMENT, not a tape proxy.
  14,976 windows carry both; `outcomePrices` agrees on them.

`tools/corpus_models.py` rebuilds the live `ModelContext` for every (window, tau): closes =
completed 1m bars + the current price as a forming bar (mirroring `PriceHistory.ctx`), and
`sigma_1m` from the real `VolEstimator`, fed strictly past data. Windows are contiguous, so
stitching their tapes (76,130 bars, 6 gaps) reproduces the history the bot's PriceHistory would
have held. This replaces `tools/backtest_models.py`, which labels with `tape_close > tape_open`
-- the ~4% error `corpus_ml_dataset.py` documented -- and has no market price at all.

## Half 1: calibration, all 15,226 windows, 224,640 decisions

Brier skill vs the constant base rate; `day-t` clusters by day (53 clusters).

| model | emit | Brier | skill | day-t | half1 | half2 |
|---|---|---|---|---|---|---|
| **digital** (shipped) | 100% | 0.2460 | +0.020 | 0.38 | +0.144 | **-0.105** |
| **calibrated** (with quotes) | 100% | 0.2383 | +0.051 | 0.97 | +0.191 | -0.089 |
| drift_digital | 100% | 0.2512 | -0.000 | -0.00 | +0.124 | -0.126 |
| btc_maker | 100% | 0.2478 | +0.009 | 14.90 | +0.007 | +0.011 |
| **sniper** | 33% | 0.1215 | +0.510 | 74.86 | +0.510 | +0.517 |
| **phantom** | 2% | 0.0940 | +0.606 | 36.12 | +0.602 | +0.628 |
| **momentum** | 5% | 0.0571 | +0.795 | 45.06 | +0.798 | +0.764 |
| mean_rev | 25% | 0.6366 | **-1.548** | -97.29 | -1.531 | -1.561 |
| sentiment | 82% | 0.3766 | **-0.503** | -31.58 | -0.507 | -0.506 |
| hybrid | 6% | 0.4241 | -0.771 | -11.95 | -1.024 | -0.464 |
| calibrated_down | 100% | 0.5569 | -1.229 | -42.01 | -1.292 | -1.167 |

**The pooled skill is a mirage.** Broken down by tau, the apparent skill of momentum (0.80),
phantom (0.61) and sniper (0.51) is concentrated at SHORT tau, where the round is already
decided and the model is only reading the spot it can see. The market prices that too, so it is
not tradeable. `digital`, meanwhile, is **anti-informative early and only mildly informative
late**:

| tau | 240 | 180 | 120 | 60 | 30 | 10 | 3 |
|---|---|---|---|---|---|---|---|
| digital skill | **-0.192** | **-0.100** | -0.014 | +0.061 | +0.082 | +0.084 | +0.080 |
| sniper skill | -0.062 | +0.212 | +0.413 | +0.580 | +0.658 | +0.693 | +0.713 |
| momentum skill | +0.104 | +0.590 | +0.851 | +0.803 | +0.880 | +0.892 | +0.897 |

`phantom` emits **0%** at tau >= 60 and `momentum` only 4-5%. The live model window is tau
60-210, which straddles digital's sign change: a real, unmeasured defect in the shipped gate.

## Half 2: money, the 2,240 windows that carry a Polymarket price

`tools/corpus_trade.py` pushes each decision through the repo's real `evaluate` gate (same
half_spread 0.005, safety_margin 0.01, same 200bps fee curve), fills at the gate's `q_eff`,
takes ONE position per round (earliest decision, matching `max_open_rounds=1`), applies the
day-scoped loss cap, and reports the required battery. Live band 0.45-0.80, tau 60-210, flat $1:

| model | fills | WR | EV/$ | day-t | half1 | half2 | drop-best | %/day | neg days |
|---|---|---|---|---|---|---|---|---|---|
| **digital** (shipped) | 989 | 62.4% | **+0.0360** | **1.64** | +0.0266 | +0.0578 | +0.0270 | 6.95% | 2/8 |
| calibrated | 590 | 63.4% | +0.0249 | 1.33 | +0.0761 | +0.0104 | +0.0166 | 3.75% | 3/7 |
| drift_digital | 1257 | 63.4% | +0.0220 | 1.31 | +0.0219 | +0.0292 | +0.0092 | 2.09% | 2/8 |
| sniper | 167 | 56.3% | +0.0780 | 0.45 | **-0.0739** | +0.1317 | -0.0087 | 2.94% | 4/8 |
| mean_rev | 314 | 54.8% | -0.0396 | -0.20 | -0.0205 | -0.0063 | -0.0576 | -3.36% | 5/8 |
| sentiment | 1357 | 60.3% | -0.0303 | -0.96 | +0.0039 | -0.0596 | -0.0366 | -8.30% | 4/8 |
| hybrid | 58 | 44.8% | -0.2056 | -1.44 | -0.1597 | -0.1981 | -0.2489 | -3.35% | 5/8 |
| btc_maker / phantom / momentum | 0-16 | -- | inert in the live window | | | | | | |

Two controls matter. **`__market__` (p_up = the mid) fills ZERO trades** -- the gate refuses to
trade a line against itself, so the pipeline is not manufacturing edge. And the causal fix
(quoting only AT OR BEFORE the decision, not the nearest print, which could have been 12s in the
future) moved digital by 0.0003, so these are not look-ahead artifacts.

## Verdict

* **`digital` stays.** Best on money (t 1.64, both halves positive, drop-best-day positive), and
  the only model whose edge survives the fee on every test. Nothing clears the repo's t=2 bar on
  8 priced days -- that is the honest limitation, not a reason to change the model.
* **`calibrated` is better calibrated but makes LESS money** (0.0249 vs 0.0360) because anchoring
  on the market shrinks its own claimed edge and cuts fills from 989 to 590. Calibration is not
  the objective.
* **`mean_rev`, `sentiment`, `hybrid`, `calibrated_down`: stay disabled.** Negative EV with
  negative day-t and BOTH halves negative. These are not noise; they are anti-informative.
* **`btc_maker` is inert under the live band** (0 fills in 0.45-0.80; it trades ~0.31). Keep it
  disabled -- it cannot contribute.
* **`phantom` and `momentum` should stay disabled as PRICING models**: phantom never emits at
  tau >= 60 and momentum emits 4-5%, so neither can drive the model arm. Their high calibration
  skill is a short-tau artifact.
* **The cross and ticket arms are ARMS, not models** -- they are not in this registry and this
  study does not rank them. Note the two are not independent: the cross arm's tau window is
  3-60s, exactly the region where `digital`'s skill turns positive (+0.061 at 60s) and where
  sniper's rises to 0.58, which is the mechanistic reason the late tau window is where the money
  is.

CAVEAT governing every money number: the prices are Polymarket's, not predict.fun's. Only the
MID is taken from Polymarket, as the market's estimate of P(Up); execution is modelled with our
venue's cost. So this measures "does the model beat the market's opinion after our costs", not
"what our book would have quoted". 8 priced days is short -- read the bootstrap as dispersion,
not forecast.

## The btc5m corpus CANNOT price the momentum arms -- and here is the proof

`tools/corpus_models.py` scores a pricing model, because a model's fair value is graded against
the settlement and the settlement is exact. The ARMS are a different test: an arm's mechanism is
"I am faster than the price", so it must be scored against a price that actually existed.

Run that way, the Polymarket corpus returns impulse at **+32% to +69% per $1** and buys sides
that win 55-68% at prices of 0.37-0.57. Three independent checks say that is an artifact, not an
edge:

1. **The "bid" and "ask" columns are not a book.** `up_buy_px - up_sell_px <= 0` on **44.5%** of
   momentum entries (mean -0.0152, median -0.0100). An ask cannot sit below a bid. Both columns
   are per-second AVERAGES of executed buy-side and sell-side trades, not quotes.
2. **The edge grows with staleness**, which a real lag cannot do: same second +0.689, one second
   earlier +0.883, two seconds earlier +0.970, but a *fresher* print (next second) +0.460. A
   trade-print average is simply older than the tape close that triggered us.
3. **It is 2-4x the live measurement.** `edge/impulse.py` records +8% to +21% per $1 measured on
   96,800 recorded live ticks. The corpus says +32-69%.

Also checked and cleared: the tape-vs-strike basis is a near-constant **+6.6 bps** (tape[0] vs
`priceToBeat` +6.61, tape[-1] vs `finalPrice` +6.57), so any "is spot above the strike?" test on
this corpus is biased up by 6.6 bps -- but correcting the strike left the impulse figure
identical (+0.6893), so the basis is not the cause.

**Conclusion: momentum arms must be tested on the bot's own recorded book.** `data/btc5m/*` is
for pricing models only.

## The arms, and the best combination (recorded book, 2026-09-08..13)

`tools/arms_recorded.py`: 1,776 settled rounds / 6 days from `vps.db`, 291,087 book rows. Real
venue book (`up_bid/up_ask/down_bid/down_ask`, **0 crossed of 539,323**), the venue's own strike
and settlement, sigma from the recorded 1m bars through the shipped `VolEstimator`. Every arm
fills at the ASK of the side it chose. One position per round for impulse/model (they share the
`guard.check(slug)` dedup); cross keeps `cross_max_per_round`.

| arm | fills | /day | WR | px | EV/$ | day-t | tot$ | half1 | half2 | drop-best | neg days |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cross | 199 | 33.2 | 74.4% | 0.583 | **+0.3546** | 1.76 | 70.56 | +0.3203 | +0.9331 | +0.2756 | 0/6 |
| impulse | 211 | 35.2 | 45.5% | 0.405 | +0.2537 | 1.35 | 60.51 | **-0.0115** | +0.5279 | +0.1423 | 1/6 |
| model | 874 | 145.7 | 64.3% | 0.614 | +0.0389 | 1.34 | 27.11 | +0.0103 | +0.0479 | +0.0187 | 2/6 |

| combination | fills | EV/$ | day-t | tot$ | halves | drop-best | neg |
|---|---|---|---|---|---|---|---|
| **impulse+cross+model** | 1,144 | +0.1393 | **+3.07** | **$159.37** | +0.0725/+0.2510 | +0.1247 | **0/6** |
| impulse+cross | 410 | +0.3027 | 1.77 | $124.09 | +0.1358/+0.9414 | +0.2463 | 1/6 |
| cross+model | 1,073 | +0.0974 | +2.82 | $104.54 | +0.0725/+0.1863 | +0.0843 | 0/6 |
| impulse+model | 945 | +0.0940 | +1.39 | $88.84 | +0.0144/+0.1389 | +0.0297 | 2/6 |

**Run all three.** It is the best combination on every axis that matters: highest total
($159.37), highest day-clustered t (**+3.07**, the only configuration clearing the repo's t=2
bar), positive in BOTH halves, positive drop-best-day, and **0 negative days out of 6**.

The arms are near-additive (60.51 + 70.56 + 27.11 = $158.18 standalone against $159.37
combined), so they do not cannibalise: impulse/model occupy tau 60-210 and cross occupies tau
10-60, and the position rule lets cross keep its own quota. The model arm's contribution is not
its $27 -- it is that adding it lifts the combination's t from 1.77 (impulse+cross) to 3.07 by
smoothing the daily series.

Two honest caveats. **The per-arm RANKING is not stable**: the first half says cross is best
(+0.2735), the second half says impulse (+0.6571). Only the SIGN of "all three together" holds in
both halves. And **cross was selected after ~40 variants** (see `config.cross_*`), so its point
estimate carries selection risk; its t is 1.76 alone.




---

# Session 2026-09-13 — the limit-price bug, and the model arm's calibration

Everything below is measured. Where a number rests on a small sample, the n is
given; where a previous finding was overturned, the old one is named.

## What the venue actually does with a LIMIT order

**A LIMIT quote is a QUANTITY commitment priced at your limit, never a price.**
Verified read-only against the live venue while the Up ask was 0.960:

| priceLimit | amountIn | amountOut | amountIn/amountOut |
|---|---|---|---|
| 0.30 | 0.999 | 3.330 | **0.3000** |
| 0.50 | 1.000 | 2.000 | **0.5000** |
| 0.70 | 0.994 | 1.420 | **0.7000** |
| 0.90 | 0.999 | 1.110 | **0.9000** |
| 0.99 | 0.9999 | 1.010 | **0.9900** |
| *MARKET* | 1.000 | 1.0407 | 0.9609 ← the only one that tracks the book |

`amountOut = amountIn / priceLimit` exactly, at every level, regardless of the
market. `minReceive == amountOut`. The venue then sometimes **delivers exactly
that commitment**, i.e. you pay your limit.

Measured on the three live cross fills:

| intended | record | shares | fill % | outcome |
|---|---|---|---|---|
| $1.1769 | $0.9000 | 1.29 | 76% | beat the limit (0.6977 vs 0.90) |
| $1.1697 | $0.8400 | 1.28 | 72% | beat the limit (0.6562 vs 0.90) |
| $1.0909 | $1.0900 | 1.21 | **100%** | **paid the limit** (0.9008) |

Hypothesis (n=3, mechanism not proven): **full fill → you pay the limit;
partial fill → you beat it.** `_cross_tick` now logs fill-% on every order so
this confirms or dies on its own.

Consequence, measured across arms on the same day:

| arm | limit policy | n | median slip (gate→fill) |
|---|---|---|---|
| live (main) | quote + 1 tick | 626 | **+0.0086** |
| cross | pinned at `cross_max_price` 0.90 | 3 | **+0.1408** |

`limit_ceiling` (shipped earlier the same day to fix a no-fill) was the cause.
Replaced by `cross_limit_buffer`: limit = ask + min(0.10, 20% of ask), floored
at one tick, capped by `cross_max_price`.

`get_quote` round-trip measures **43 ms**, and a quote's `expireAt` is
`timestamp + 3000 ms`. So: speculative quote pre-fetch is impossible (3s TTL),
and order latency was never the problem.

## Hypotheses killed by measurement (do not re-propose)

| claim | killed by |
|---|---|
| slippage is a stale gate price | websocket made the gate fresh (`run+0.000`); gap persisted |
| slippage is order latency | `get_quote` = 43ms; 3 round trips ≈ 130ms |
| slippage is book sweep | premium for a $1-5 order = **+0.0004** over 19,452 real depth snapshots; 0% of books cannot absorb it |
| venue always fills LIMIT at the limit | fills 1 and 2 beat a 0.90 limit |
| tighter/pinned/aborting limits improve fills | 4 variants, all worse — cheap fills are adversely selected |
| depth caps growth around $150 bankroll | real depth at the touch is median **$55**, p25 $13.50 |

## The websocket order book

`wss://api.binance.com/sapi/wss`, topic `web3_prediction_orderbook_{marketId}`,
signed with the same HMAC query string as SAPI. **Docs are wrong in three
places** (all verified live):

- the aggregated topic `web3_prediction_orderbook_data` registers with SUCCESS
  and then pushes **nothing**; only per-market topics deliver
- frames arrive as `type:"DATA"`, not the documented `"TOPIC"`
- prices/sizes are JSON **numbers**, not the documented strings

Measured: median **201 ms** between updates (p10 111, min 37), **zero gaps over
1 s**, venue→us latency median **89 ms**. Against the 1000 ms REST poll every
earlier slippage figure was measured on.

**The book is the `Up` token.** Verified against REST for the same market and
instant (distance 0.000 vs 0.140 for Down). Complete-set identity holds on
99.5% of 126k recorded quotes: `down_ask = 1 - up_bid`, `down_bid = 1 - up_ask`,
**including sizes**.

Trap: `async for msg in ws` blocks forever when a market goes quiet at round
end, so the feed dies silently after one round and every arm reverts to the 1s
poll with no error. Both `prediction_book.py` and `tools/record_ws_book.py` use
a `wait_for(receive, timeout=1.0)` loop instead.

## THE MODEL ARM — its fair value is worse than the price it bets against

Corpus, tau 60-210, n=4,236:

| | Brier | log-loss |
|---|---|---|
| model | 0.2418 | 2.4016 |
| **venue** | **0.1670** | **0.5002** |

And the model's error is *largest exactly where the arm trades hardest*:

| model − market | n | model says | venue says | ACTUAL |
|---|---|---|---|---|
| +0.02..+0.05 | 158 | 0.953 | 0.916 | 0.924 |
| +0.05..+0.10 | 277 | 0.958 | 0.882 | 0.884 |
| +0.10..+0.20 | 481 | 0.961 | 0.812 | 0.784 |
| +0.20..+1.00 | 1131 | 0.980 | 0.643 | **0.640** |

The venue is within ~1 point of truth at every level. `evaluate()` buys when
`fair - quote > min_edge`, so **the entry rule selects for the model's own
largest errors**. Inverted by construction, not a bad parameter.

Live, the only test that matters (win rate vs price paid):

| window | n | win | paid | edge |
|---|---|---|---|---|
| all (mode=live) | 629 | 0.657 | 0.634 | +2.3 pts (1.2 SE) |
| before Sep 12 | 349 | 0.719 | 0.675 | +4.4 pts (**1.8 SE**) |
| Sep 12 onward | 280 | 0.579 | 0.582 | **−0.3 pts** |

Nothing significant. Corpus replay of the same logic is negative
(weekday −0.0922, weekend −0.0047 per $1, on *optimistic* display prices).
Consistent story: **no established edge, one good week.**

Also: `stale_maker` is flat (n=24, +1.4 pts, 0.1 SE) — the execution-edge
theory does not rescue it. The `ticket` arm's +$45.25 is **one** $0.02 ticket
paying +$48.98; without it, −$3.73 on 20 trades.

## The cross arm — the one signal established on a real sample

Corpus month, current gates (tau 10-60, depth ≤0.5bps, look 2s, ≤3/round):
**792 triggers over 31 days, win 0.6907**, 95% CI 0.6585-0.7228.
No weekend penalty (weekend 0.6979 vs weekday 0.6876).

Break-even at live-observed fills: **book 0.553 → +13.8 pts edge; limit 0.665 →
+2.6 pts.** So P(limit) dominates everything: at 1x, P(limit) 0→1 moves the
30-day median $1453 → $74.

Knobs re-swept under the corrected fill model — **all survived**:
`cross_max_price` 0.90 (best total + day-t), `cross_max_depth_bps` 0.5 (clear
peak), tau 10-60 (best), `cross_limit_buffer` 0.10/20% (curve flat 0.05-0.10,
degrades past 0.15).

`cross_skip_opposite_to_main` — measured on 163 fill-aware triggers against the
main arm's real fills: SAME side n=44 win 0.864 EV +0.3558 day-t 3.93;
**OPPOSITE side n=38 win 0.605 EV −0.1066 day-t −0.66**; no main position n=81
win 0.802 EV +0.2200. Same-vs-opposite win-rate gap z=2.74 (p≈0.006).

## Weekends are NOT worse for either arm

Raw ITM-persistence at tau 10-60 is lower on weekends (0.8654 vs 0.8960,
31 corpus days) — but both arms' *realized* edge is not:
cross 0.6979 vs 0.6876; model 0.6408 vs 0.6281 (+0.0875 EV/$ in the weekend's
favour). Do not disable weekend trading on the persistence number alone.

**Live Sep 12-13 cannot answer this**: those two days are simultaneously the
weekend AND the new sizing era, so the effects are perfectly confounded.

## Risk knobs, swept on the corpus (both arms, 7 days)

`daily_loss_cap_pct` halt frequency: 10% → 36.5% of days, 15% → 19.4%,
**20% → 7.9%**, 30% → 2.1%, 50% → 0.1%. 20% is the knee; 10% is measurably
worse (halts days that would recover). `stake_cap_usdt` made no difference at
7 days and is now **0 (off)** — note `stake_for_fraction` already honoured
`cap > 0`, but `_stake_for`'s tilt clamp and the cross multiplier did **not**,
so a 0 would have clamped every stake to zero. Both fixed.

`max_stake_usdt = 10.0` is **inert** under `sizing_mode = fraction` (that branch
reads `stake_cap_usdt`). It is only reachable in `kelly` or band mode — a
dormant trap that would reinstate the Sep 9-11 $10 stakes if the mode changes.

`cross_max_per_day = 20` no longer binds (live max 17/day, corpus max 18/day
after gates). It bound on 4 of 6 days *before* the limit fix, costing 34% of
fills. **Coupled to the buffer**: widen the buffer and it binds again.

## Method traps hit this session

- **Sizing a sweep on the wrong sample.** 3x cross was chosen against win 0.828
  from 58 replay fills; the month says 0.6907, far outside that CI. Now 1.5x.
- **Assuming a tilt.** Applying tilt=2.0 (4.48%/trade) when real stakes are
  ~2.1% made the model arm look like a loser against its own +$69 record.
  Scale from *observed* stakes, not a re-derived rule.
- **Mixing config eras.** Sep 9-11 used band sizing ($10 stakes, p90-p99 all
  exactly 10.00); Sep 12+ uses fraction. A 6-day replay spans both.
- **Repeated `cat >>` duplicated 248 lines of tests**, so a stale copy shadowed
  the fixed one. Check `grep -oE "def test_[a-z_]+" | sort | uniq -d`.
- **Mislabelled probe output in a prompt.** A `stake=1.0` probe quote was pasted
  next to a `$1.09` trade, producing a phantom "9% overspend". Label probes.

## Live config state at session end

`cross_enabled=true`, `cross_stake_usdt=None` (uses the main rule),
`cross_stake_mult=1.5`, `cross_limit_buffer=0.10`/`_pct=0.20`,
`cross_skip_opposite_to_main=true`, `cross_max_per_day=20`,
`ws_book_enabled=true` (`ws_book_max_age_s=2.0`), `stake_cap_usdt=0` (off),
`daily_loss_cap_pct=0.20`, `entry_mode=both`, `max_open_rounds=1`,
`impulse_max_open=1`. Arms are independent: each has its own pool, and only
exposure the LEDGER cannot explain refuses any of them.

Cross arm lifetime: **3 fills, 2 wins, net −$0.33.** Everything else is replay.

## Open questions

1. **Can the model arm be fixed?** Its pricer competes with a better-calibrated
   market maker. Any fix must beat Brier 0.1670 / log-loss 0.5002, not just
   improve on its own 0.2418 / 2.4016.
2. **P(limit)** — the single most decisive unknown, currently n=3. The
   `cross exec` log line carries fill-% on every order.
3. Why the model arm's corpus replay is negative while its pre-Sep-12 live week
   was +4.4 pts. Unresolved; 1.8 SE is consistent with noise.

## The price tape: what instrument the cross arm actually needs (2026-09-13)

**The bot does not drop quote updates.** `BinanceSpotFeed._run_ws` consumes
every message and appends each to `ticks` (deque, maxlen 4000). The ~0.3/s seen
in the DB is the *persistence* cadence -- `quotes.spot` is written once per
quote poll -- not a websocket delivery rate. Measured on the local DB, BTC 5m:
50,325 distinct `spot_ts_ms` over 1.85d = **0.31/s**, median gap 3.01s.

So the live arm trades the full ~114/s path; only the **backtests** are 1s.

**Binance does not archive spot bookTicker at all** (only aggTrades/klines/
trades), and futures bookTicker stopped 2024-03. Free Tardis
(`datasets.tardis.dev/v1/binance/book_ticker/...`) gives real spot quotes, but
only the **1st of each month**.

### Two-factor test, 7 symbols, synthesized 5m UTC rounds (`tools/instrument_ab.py`)

Rounds are synthesized, so the bot's `rounds` table is not needed and the
disjoint date ranges do not block the question. `crosses_vec` is a vectorised
twin of `cross_multi_market.crosses`, asserted equal to it under `--verify`.

    pooled          2026-09-01 (Tue)        2026-08-01 (Sat)
    kline-1s        n=376 win 0.612         n=617 win 0.609
    quote-1s        n=323 win 0.616         n=298 win 0.772
    quote-full      n=902 win 0.635         n=745 win 0.757

Instrument barely matters on a Tuesday (0.612 vs 0.616) and matters hugely on
a Saturday (0.609 vs 0.772). **The kline-vs-quote bias is day-dependent**, so
klines are not a fixed-offset proxy that can be corrected for.

### The real mechanism: the depth gate is narrower than cross-tape noise

BTC 5m on the bot's own recorded tape vs 1s klines, **real venue rounds and
real venue settlement**, Sep 8-10:

    depth gate   bot tape          kline 1s          gap
    0.25 bps     n= 29 win 0.655   n= 57 win 0.596   +0.059
    0.50 bps     n= 50 win 0.700   n=103 win 0.621   +0.079   <- live gate
    1.00 bps     n= 79 win 0.722   n=155 win 0.697   +0.025
    2.00 bps     n=104 win 0.702   n=196 win 0.694   +0.008
    5.00 bps     n=112 win 0.714   n=204 win 0.711   +0.004

The two tapes agree on *level* -- median **-0.0006 bps** over 50,325 matched
instants -- but disperse at **0.494 bps sd**, and **9.8%** of instants differ by
more than the entire 0.5 bps gate. Above ~2 bps the tapes give the same answer.
So the old "kline 0.627 vs bot-feed 0.802" gap was never an instrument-quality
problem: it is selection on noise inside a gate narrower than the noise.

**Do NOT widen `cross_max_depth_bps` on this table.** It is win-rate-only; a
wider gate enters further ITM at a worse ask. The 0.5 "clear peak" above was a
fill-aware EV sweep on 792 triggers and still stands.

### Consequences

1. Buying Tardis for the exact windows would **not** settle the multi-market
   question. The blocker is not the dates -- it is that at the 0.5 bps gate the
   answer depends on which tape you hold.
2. More free first-days cannot fix it either: the 1st is a **random weekday**
   (2026-08-01 Sat, 2026-09-01 Tue, 12 pts apart), one day per month, so the
   day-clustered t>=2 bar is unreachable.
3. The only instrument that predicts live behaviour is **the bot's own feed**,
   because it is the actual decision input. Persisting it at full rate makes
   every future day self-validating, with the next free Tardis day as an
   independent cross-check.
4. `ticks` (maxlen 4000) is **35 s** of history at BTC's live 114/s. Fine for
   `cross_look_s` 2.0; anything wanting a longer lookback off this buffer is
   silently truncated.

Per-symbol full-rate quotes (one day each, NOT a robustness bar):

    BTC 0.812/0.939   DOGE 0.706/0.882   ETH 0.632/0.703   ZEC 0.608/0.604
    BNB 0.581/0.637   SOL 0.577/0.846    XRP 0.537/0.853        (Sep 1 / Aug 1)

## Multi-market: the blockers, and what the downloaded L2 already answers (2026-09-13)

`data/hf/updown_{5m,15m,4h}.parquet` is the **venue's own L2 book** -- same slug
shape (`<coin>-updown-<tf>-<open epoch>`), same 77-digit token ids, same
CHAINLINK feed as `rounds`. 8 coins, Aug 21-24, ~1 snapshot/s, with full depth
and the settled winner. It is the executable price the recorded-only families
never had.

Two decoding traps, both of which produce a clean-looking coin flip:

* **The slug epoch is the round OPEN**, not the close: `market_end_at` is
  slug + duration. Pricing `[slug-dur, slug]` replays the PREVIOUS round.
* **`winner` is round-level, not per-outcome** -- 2 or 0 per round, never 1.
  `winner == 'yes'` means **Up won**; the per-row flag is `is_winning_outcome`.
  Confirmed against the book: at tau<=15s the winning token bids 0.99 median,
  the loser 0.045.

Decoded correctly, a strike synthesized from Binance 1s spot at the round open
reproduces the venue's Chainlink settlement **91.4%** (5m) / **94.5%** (15m),
rising to 99% at moves >=10bps. The error is concentrated in sub-2bps rounds --
exactly where the cross arm lives -- so any win rate measured this way is
biased DOWN. The ASK is unaffected, which is what the file is for.

### Ask paid for the ITM side, at the arm's own gate (tools/cross_book_pricing.py)

    5m  tau 10-60      n     ask  spread     win    EV/$1
      btc 0-0.5bps     51   0.540   0.011   0.824   +0.283
      btc 0.5-2bps    306   0.442   0.010   0.657   +0.215
      eth 0.5-2bps    172   0.557   0.050   0.453   -0.104
      bnb 0.5-2bps    131   0.652   0.250   0.679   +0.027

    15m tau 30-180
      btc 0.5-2bps     93   0.791   0.016   0.892   +0.102
      eth 0.5-2bps     34   0.566   0.022   0.765   +0.199
      bnb 0.5-2bps    750   0.696   0.033   0.732   +0.036

**ATM spread is what separates the markets**: btc 0.010, eth 0.025-0.050,
bnb **0.250** on 5m. BNB 5m's 0.536 win rate on the bot's own tape is not a
signal problem -- a 25-cent spread cannot be crossed profitably at any win rate
the arm produces.

Snapshots inside one round are autocorrelated, so those n are NOT independent
rounds, and 4 days cannot clear the day-clustered t>=2 bar. This ranks markets;
it does not promote one.

### Timeframes: what the DATASET holds vs what the VENUE lists

`contract_length` across all three parquets is exactly {5m, 15m, 4h}. That is
the dataset's coverage, NOT the venue's: Binance lists **5m, 15m, 1h and 1d**
for btc/eth/bnb (screenshot 2026-09-13), and no 4h. So the parquet prices 5m and
15m for us and says nothing about 1h/1d, which have no L2 tape here at all.

Sample rate still decides how fast each can be judged: 288 rounds/day at 5m,
96 at 15m, 24 at 1h, 1 at 1d.

### Blockers fixed

1. **Books for recorded-only markets.** `tick()` now calls `wsb.follow()` with
   every live market_id (one topic, no SAPI quota), and `_record` reads the ws
   book without the `trade` gate. The REST fetch stays gated -- that one costs
   quota and we have been 429'd. `_record` still returns on `not trade` before
   any order path, so this widens the tape, not the risk.
2. **Per-market knobs.** `cross_overrides` maps a slug prefix to knob values;
   `cross_knob()` / `Bot._ck()` resolve through it with the global as fallback,
   so adding a market cannot move BTC. All 21 knob reads in `_cross_tick` go
   through it.
3. **`bnb-updown-15m`** added to MARKETS (recorded only), completing the
   btc/eth/bnb x 5m/15m grid.

Still single-market on the ORDER path: `_cross_tick` is built on `self._round`
/`self._asks`/`self._primary_spot`, one round at a time. Promoting a second
family to trading needs that made per-market first.

## Multi-market cross: the backtest the experiment asked for (2026-09-13)

The 31-day corpus is BTC 5m only, so the shipped cross row could never speak to
other markets. `tools/cross_market_month.py` replays the arm's own trigger over
a FULL August month of 1s klines for btc/eth/bnb at 5m/15m/1h/1d
(synthesized rounds; see the tape section for why the level, not the ranking,
is the soft part).

    market            rounds/d      n  trig/d    win        95% CI  day-t
    btc-updown-5m          288   2071    66.8  0.655  [0.635-0.676]   9.97
    eth-updown-5m          288   2028    65.4  0.629  [0.608-0.650]   8.90
    bnb-updown-5m          288   2280    73.5  0.619  [0.599-0.639]  11.09
    btc-updown-15m          96    302     9.7  0.695  [0.643-0.747]   6.96
    eth-updown-15m          96    306     9.9  0.644  [0.590-0.697]   3.75
    bnb-updown-15m          96    439    14.2  0.624  [0.579-0.669]   4.11

**Every 5m and 15m family clears day-t 2 on the signal.** The signal is not what
separates them -- the book is.

### 1h and 1d are not worth activating

At the arm's absolute tau gate they fire ~1/day (btc 1h n=25 for the month, 1d
n=2). Scaling the tau gate with round length buys volume and destroys the edge:

    btc-updown-1h  n=333  win 0.574  day-t 2.13     btc-updown-1d  n=16 win 0.688
    eth-updown-1h  n=313  win 0.514  day-t 0.55     eth-updown-1d  n=21 win 0.714
    bnb-updown-1h  n=350  win 0.509  day-t -0.03    bnb-updown-1d  n=10 win 0.600

0.51-0.57 is below break-even at any ask the venue shows. 1d never accumulates a
sample: 1 round/day.

### Ask inside the arm's own gate (|moneyness| <= 0.5bps, tau 10-60)

    market            n    ask  spread    win   EV/$1
    btc-updown-5m    51  0.540   0.011  0.824  +0.283
    eth-updown-5m    28  0.609   0.030  0.679  +0.069
    bnb-updown-5m    25  0.810   0.417  0.720  -0.090

### Projection (tools/multi_market_projection.py)

$50 start, compounding, depth capped at the measured $55 median at the touch.
Sizing is the deployed rule as `main._stake_for` actually implements it:
**fraction mode applies NO tilt**, and the binding floor is `min_order_usdt`
($1), not `min_stake_usdt` ($5, which belongs to the edge/kelly paths) --
below it `stake_for_fraction` SKIPS rather than rounding up. So at a $50
bankroll a cross stake is 4% x 1.5 = $3.00, not $5.

    market set              day     median        p10        p90    $/trade
    BTC 5m only (shipped)    30  $ 7038.78  $ 5339.82  $ 8558.31   +11.9059
    BTC+ETH 5m               30  $ 9643.14  $ 7349.26  $11777.52    +8.2986
    BTC+ETH+BNB 5m           30  $ 1676.47  $   24.59  $ 4875.55    +1.1182

**Adding ETH 5m raises the day-30 median ~37% and lifts p10 from $5,340 to
$7,349. Adding BNB 5m destroys it** -- the median falls to $1,676 and p10 to
$24.59 (below the $50 start), because a 0.810 ask against a 0.72 win is
-0.09/$1 and the arm fires it 28x a day.

These are NOT comparable to the earlier corpus table. That one was uncapped;
this engine uncapped puts BTC-only far above its $3,751 cross row. The only
comparison that means anything is between rows HERE, where the market set is
the single variable.

## Re-run: model arm vs cross arm on the priced corpus (2026-09-13)

`tools/arms_backtest.py` had been failing since a pandas upgrade --
`apply_live_positions` built a frame from a list mixing a Series with
namedtuples, and pandas >=2.2 reads a namedtuple's `.index` METHOD as an axis.
Both branches now append frames and it is concatenated.

The PRICED corpus is 2,240 windows over **8 days** (`data/btc5m_prices.parquet`).
The 8,436 windows in the older table are the FEATURE set
(`data/btc5m_ml.parquet`), which carries no venue quotes -- so 8 days is the
most that can be replayed against real asks, for either arm.

    arm                 fills   /day     WR     px    EV/$  day-t     tot$    half1    half2 dropbest   neg
    impulse               208   26.0  50.5%  0.369  0.5544   2.02   115.31  +1.3857  +0.5102  +0.4898   0/8
    cross                 210   26.2  59.5%  0.506  0.1968   0.65    41.32  +0.2735  -0.0659  +0.0299   1/6
    model                1363  170.4  52.6%  0.500  0.0452   1.31    61.63  +0.0354  +0.0574  +0.0223   3/8
    impulse+cross         418   52.2  55.0%  0.438  0.3747   3.56   156.63  +0.4916  +0.4225  +0.3591   0/8
    cross+model          1573  196.6  53.5%  0.501  0.0654   2.13   102.95  +0.0694  +0.0642  +0.0491   3/8
    impulse+cross+model  1643  205.4  52.5%  0.489  0.0760   3.00   124.82  +0.0880  +0.0725  +0.0643   1/8

**The model arm is not zero.** EV/$ +0.0452, both halves positive, drop-best
still +0.0223. It does not clear day-t 2 alone (1.31) and loses on 3 of 8 days,
but "no INFORMATIONAL edge over the venue price" (the blend result, still true)
and "worthless as a trading arm" are different claims, and the earlier note
conflated them.

`cross+model` is exactly additive: 41.32 + 61.63 = 102.95. The arms do not
interact -- model claims the round, cross holds its own slot -- so the union is
purely "is the model arm's +$61.63 worth running", not a synergy.

**Caveat that cuts the other way: cross is WEAK on this priced slice.** day-t
0.65 and a NEGATIVE second half (-0.0659). Its strong number (792 triggers, win
0.6907) comes from the 31-day SIGNAL replay, which needs no quotes. On real
asks over 8 days it is 210 fills and $41.32.

### Compounding, same engine as the market-set table

    arm            day     median        p10        p90    $/trade
    MODEL alone      1  $   39.92  $   38.73  $  109.14   -0.0833
                    30  $  387.45  $   24.31  $ 9686.31   +0.1091
    CROSS alone      1  $   61.81  $   38.92  $   88.80   +0.5903
                    30  $ 3919.84  $ 1717.50  $ 5835.28   +6.7892
    BOTH             1  $   39.99  $   38.66  $  144.65   -0.0607
                    30  $ 9732.96  $   24.53  $17897.53   +2.1623

**The model arm adds leverage, not edge.** BOTH's day-30 median is 2.5x CROSS
alone ($9,733 vs $3,920) and its p10 is 70x WORSE ($24.53 vs $1,717.50, i.e.
half the starting bankroll). At 170 trades/day and a 4% fraction its median
day-1 outcome is $39.92 -- exactly the 20% daily stop -- so the stop is what is
holding it, in more than half of all paths, on day one.

### The surprise: impulse

Best single arm on the priced corpus (EV/$ +0.5544, day-t 2.02, 0/8 negative
days) and `impulse+cross` is the best combination of all (EV/$ +0.3747, day-t
3.56, both halves positive, drop-best +0.3591, 0/8 negative days). It buys at
px 0.369 -- cheap entries -- and **it has never fired live**, so whether those
fills exist at that price is exactly the question the cross arm's slippage work
answered badly. Untested, not proven.

## Live record cut by weekday (2026-09-13)

Sep 12 is a **Saturday** and Sep 13 a **Sunday**, so the entire "current config
era" is weekend-only. The confound this file warned about is not partial -- the
new sizing era has **never run on a weekday**.

    model arm ("live" mode), settled, non-void
      2026-09-08 Tue  n= 37 win 0.676 staked   37.00 pnl  +3.98  per$1 +0.1076
      2026-09-09 Wed  n= 50 win 0.660 staked  237.00 pnl +57.58  per$1 +0.2430
      2026-09-10 Thu  n= 33 win 0.545 staked  193.37 pnl -42.21  per$1 -0.2183
      2026-09-11 Fri  n=234 win 0.756 staked  797.51 pnl +34.19  per$1 +0.0429
      2026-09-12 Sat  n=150 win 0.553 staked  176.36 pnl  -5.69  per$1 -0.0323
      2026-09-13 Sun  n=139 win 0.597 staked  154.74 pnl  +0.89  per$1 +0.0058

Weekdays +$53.54, weekend -$4.80 -- but the weekend days are the two SMALLEST
magnitudes on the board (-0.032 and +0.006 per $1) against weekday swings of
+0.243 and -0.218. The weekend is **flat, not losing**, which is what "at parity
with the venue, paying the spread" looks like. Any claim that the model arm is
negative in the current era rests entirely on those two days and does not hold.

**The cross arm has ONE day of live history.** Decisions by day: Sun 5, and
zero on every other day -- it only began trading after the 2026-09-12/13 fixes
landed. 4 fills, +$0.02. There is no live-vs-replay trigger-rate gap to measure
yet; a "2.5/day live vs 26/day replay" reading is one weekend day misread as a
week.

**The daily cap halted on 4 of 6 days**: Wed 676 refused decisions, Fri 13,
Sat 526, Sun 139. The corpus sweep put `daily_loss_cap_pct` 20% at 7.9% of days.
67% vs 7.9% is a large discrepancy. Part of Wed predates the halt bug the
operator fixed, but Sat/Sun do not. The halt is account-wide ("no arm is
exempt", guards.check_pool), so whatever trips it also silences cross.
Worth watching; not yet explained.


## RETRACTION: the month-long per-market cross numbers are invalid (2026-09-13)

Retracted: btc-5m 0.655 / eth-5m 0.629 / bnb-5m 0.619 (and the 15m, 1h and 1d
rows), their day-t 3.75-11.09, the per-market ranking drawn from them, and the
BTC+ETH / BTC+ETH+BNB projections built on those rates.

**Why.** Those came from replaying the trigger over a month of 1s klines with
the settlement synthesized from spot. Overall a spot settlement agrees with the
venue's Chainlink one 88.5% of the time at 5m -- but that average does not apply,
because the disagreements concentrate in sub-2bps rounds and **the cross arm
trades only sub-2bps rounds**. Measured CONDITIONAL on the arm triggering
(tools/settlement_bias.py):

    5m   all rounds q=0.8852 (n=418)   TRIGGERED rounds q=0.6410 (n=39)
    15m  all rounds q=0.9571 (n=233)   TRIGGERED rounds q=0.7273 (n=11)

Inverting w_meas = q*w_true + (1-q)*(1-w_true) at q=0.641 sends a measured 0.655
to w_true = 1.05 -- impossible. The measurement is not recoverable, not merely
noisy. A synthesized settlement CANNOT measure this arm at any sample size.

This is structural: the arm fires when spot is within 0.5bps of the strike near
expiry, which is exactly where the spot-vs-Chainlink basis decides the outcome.

### What the real-settlement data actually supports

`tools/market_verdict.py` -- real venue rounds, real L2 ask, real winner, CIs
bootstrapped over ROUNDS (triggers inside a round are not independent; a
per-trigger CI is ~sqrt(k) too narrow and is what made the earlier tables look
decisive):

    5m    trig rounds   ask    win   win 95% CI      EV/$1   EV 95% CI
    btc      7      5  0.366  0.714  [0.374-1.000]  +1.541  [-0.540,+3.920]
    eth      9      5  0.404  0.444  [0.000-0.819]  -0.413  [-1.000,+0.019]
    bnb      6      4  0.808  0.833  [0.700-1.000]  -0.086  [-0.268,+0.201]
    ALL     50     28  0.482  0.460  [0.293-0.648]  +0.322  [-0.484,+1.646]

**Nothing is measurable.** 50 triggers over 28 rounds across 7 coins. The
multi-coin L2 set is Aug 20-24 at ~5% round sampling (471 of a possible ~9,216
5m rounds), and the cross gate is rare, so this is the ceiling.

### The MODEL arm IS answerable on this data -- and the answer is no

It takes one position per round through the real `evaluate` gate at tau 60-210,
so n is the ROUND count, and the winner is the venue's.
`tools/model_arm_markets.py`:

    5m    rounds fills  fill%   ask    win   EV/$1   EV 95% CI (by round)
    btc       62    21  33.9%  0.476  0.524  +0.137  [-0.367,+0.650]
    eth       58    23  39.7%  0.445  0.522  +0.131  [-0.317,+0.609]
    bnb       66    13  19.7%  0.488  0.385  -0.231  [-0.757,+0.372]
    sol       67    26  38.8%  0.429  0.346  -0.245  [-0.640,+0.199]
    xrp       62    17  27.4%  0.429  0.294  -0.381  [-0.850,+0.151]
    doge      51    15  29.4%  0.439  0.467  +0.034  [-0.532,+0.653]
    ALL      418   115  27.5%  0.449  0.426  -0.082  [-0.283,+0.132]

    15m      ALL   233    22   9.4%  0.430  0.545  +0.164  [-0.314,+0.647]

**No market clears zero**, and pooled 5m is negative at the point estimate. This
is consistent with the calibration result (the pricer is at parity with the
venue) and with the 8-day BTC priced corpus (EV/$ +0.0452, day-t 1.31).

### What would settle the cross question -- a download, not a wait

The binding constraint is ROUND COVERAGE, not calendar time: this set samples
~5% of rounds. At the observed ~7 triggers per coin per 4 days, establishing
merely "better than a coin flip" per coin needs ~24 triggers, i.e. ~2 weeks at
current sampling -- or the SAME 5 days at full round coverage, which would be
~20x the rounds and enough on its own.

## Cross arm on complete venue L2 -- and why 0.5bps was never measurable (2026-09-13)

Data: `kartikbathla/polymarket-crypto-up-down-5min-15min-market-orderbook-l2-data`,
2026-07-17, EVERY round of the day (288 per coin at 5m, 96 at 15m), event-level
(~99k book snapshots per 5m round, 10 levels a side) -> `data/hf2/kartik`.
847 of 864 btc/eth/bnb 5m rounds usable. Trend efficiency 0.003-0.019 -- a pure
chop day, i.e. NOT a regime that flatters this arm.

### The strike is the whole problem, and it is now quantified

No dataset publishes the strike; the venue derives it from a Chainlink Data
Stream at `eventStartTime` (Polymarket round metadata has no strike field).
Two independent reconstructions:

  book   recover K by maximising confidence-weighted agreement between
         sign(spot-K) and the book's implied side (tools/cross_l2_backtest.py)
  open   spot at the round open -- touches the book not at all

They differ by a MEDIAN of 0.17 bps (mean -1.15, IQR [-0.95,+0.18]) and both
reproduce the venue's winner (98.6% / 97.5%). That 0.17 bps is small in absolute
terms and fatal at a 0.5 bps gate:

    depth   strike=book   strike=open    win gap
     0.50     +0.231        +0.053        8.3 pp     <- strike error decides
     1.00     +0.185        +0.127        0.7 pp
     2.00     +0.294        +0.229        1.3 pp
     5.00     +0.316        +0.223        1.4 pp
    10.00     +0.316        +0.223        1.3 pp

**At depth <= 0.5bps the backtest measures the strike reconstruction, not the
strategy.** At >= 1bps the two agree and the number is the strategy's. This is
the same failure as the earlier "kline 0.627 vs bot-feed 0.802": a gate narrower
than the uncertainty in its own input.

### Widening the gate is free -- the ask does not move

    depth   ask (clean strike)   win     triggers
     0.50        0.523          0.544      204
     2.00        0.530          0.623      350
     5.00        0.536          0.630      370

The objection that a wider gate buys further-ITM entries at a worse price does
not hold: 10x the gate moves the ask 0.523 -> 0.537 while the win rate goes
0.544 -> 0.631 and triggers nearly double. Retract that objection.

### Per market, at the measurable gate, clean strike

    depth 2.0   trig rounds   ask    win          win CI     EV/$1    EV 95% CI
      btc        121     54  0.535  0.669  [0.577,0.771]   +0.400  [+0.098,+0.792]
      eth        122     49  0.498  0.557  [0.480,0.642]   +0.111  [-0.062,+0.311]
      bnb        107     45  0.560  0.645  [0.555,0.742]   +0.170  [-0.046,+0.433]
      ALL        350    148  0.530  0.623  [0.571,0.677]   +0.229  [+0.084,+0.394]

    depth 5.0    btc +0.405 [+0.121,+0.769] | eth +0.115 [-0.057,+0.301]
                 bnb +0.146 [-0.053,+0.407] | ALL +0.223 [+0.086,+0.383]

**BTC is the only market significant on its own, at both gates and under both
strike reconstructions. ETH and BNB are positive point estimates whose CIs
include zero.** So the multi-market thesis is not supported: cross is not more
profitable elsewhere, it is most profitable on BTC.

### The limit that remains

ONE DAY. The bootstrap is over rounds within 2026-07-17 and captures no
day-to-day variance, so neither day-clustered t nor drop-best-day can be
computed. HF holds no other complete-coverage day (checked both publishers).

## Both arms against the repo's own bar, on every real-book day (2026-09-13)

Five days carrying a venue book AND a venue settlement: 2026-07-17 (complete
L2, data/hf2) and 2026-08-21..24 (sampled L2, data/hf). Strike is spot@open in
both -- the zero-circularity reconstruction. Depth gate 2.0bps.

    CROSS                             MODEL
    2026-07-17  +0.229  n=350         2026-07-17  +0.274  n=241
    2026-08-21  +0.927  n=  7         2026-08-21  +0.367  n= 16
    2026-08-22  -0.069  n= 12         2026-08-22  -0.266  n= 13
    2026-08-23  -0.178  n= 11         2026-08-23  +0.135  n= 19
    2026-08-24  -0.193  n= 10         2026-08-24  -0.234  n=  9
    day-t 0.68   drop-best -0.053     day-t 0.42   drop-best -0.023

**NEITHER ARM CLEARS day-t 2, and both go negative on drop-best-day.** On the
8-day priced BTC corpus the model arm was the better of the two (day-t 1.31 vs
cross 0.65). There is no historical case for cutting the model arm, and the
earlier "cut it" note was built on six live days -- two of them a weekend, all
of them one config era -- which is not evidence.

Caveat that cuts both ways: four of the five days are the SAMPLED Aug set with
n=7-19, so their day-means are noise and the unweighted per-day statistic is
badly behaved. It is the best test available; it says "not established", not
"disproven", for either arm.

### The depth gate, same five days

    gate   pooled EV/$1   per-day mean   day-t   drop-best
    0.5       +0.0765       +0.2066       0.74    -0.0538
    2.0       +0.2102       +0.1433       0.68    -0.0527
    5.0       +0.2226       +0.2442       1.08    +0.0734   <- ships

Pooled favours 2.0 by 2.7x; the per-day mean favours 0.5 only because it gives
2026-08-21 (n=7, mean +0.927) the same weight as 2026-07-17 (n=350). With n=7
that day-mean is noise, so the per-day column cannot arbitrate this knob.

**2.0 ships on the mechanical argument, not the P&L one**: at 0.5bps the
measurement swings 8.3 pts depending on which strike reconstruction is used,
against 1.3 pts at 2.0. A gate narrower than the uncertainty in its own input
cannot be tuned -- whatever the pooled number says, at 0.5 you are fitting the
strike, not the strategy.

### Shipped

  * `cross_max_depth_bps` 0.5 -> 5.0 (the only gate with a POSITIVE
    drop-best-day, and best on pooled, per-day and day-t as well)
  * `entry_mode` gains "off" (main arm only; cross and double tick first, so
    switching one arm off cannot silence another) -- the OPTION only. The
    default stays "model": the model arm is kept, there being no historical
    case against it.
  * websocket book follows every market; `bnb-updown-15m` recorded;
    `cross_overrides` per-market knob table. Nothing new trades.
  * BTC 5m remains the only traded family -- the only market individually
    significant for cross under both strike reconstructions.

## The depth gate, settled on 31 days with the REAL Chainlink strike (2026-09-13)

`data/btc5m_ml.parquet` carries **`strike_cl`** -- the venue's actual Chainlink
strike -- plus `cl_delta_bps` (spot minus that strike) and `y_up` (the venue's
settlement). It needed no reconstruction at all, and finding it retires most of
this session's strike work.

**The spot-vs-Chainlink basis, measured on that file: median 4.01 bps, mean
5.73, 5-95% 0.41 to 15.64.** A strike taken from spot is therefore wrong by
~8x the old 0.5bps gate. That single number explains every failed attempt:
the settlement-bias finding (64% on triggered rounds), the L2 reconstruction
swinging +0.231 -> +0.053 between strike modes, and the original
"kline 0.627 vs bot-feed 0.802". **Retract the claim that spot@open is a usable
strike** -- it is off by 8 gate-widths, and the 0.17bps agreement seen between
two spot-derived reconstructions on 2026-07-17 only showed they were wrong
together.

### Win rate, 31 days, day-clustered (tools/corpus_depth_sweep.py)

    gate      n  rounds    win  per-day  day-t  drop-best  neg days
     0.5    508     452  0.616    0.625   5.16     0.613     5/31
       1    880     714  0.632    0.621   8.25     0.615     0/31
       2   1218     936  0.636    0.625  10.23     0.621     0/31
       5   1413    1064  0.657    0.642  11.14     0.638     1/31
      10   1438    1081  0.659    0.644  11.44     0.640     1/31

Every gate clears day-t 2; wider is monotonically better.

### Priced at the corpus's real asks (7 overlapping days)

    gate      n    ask    win    EV/$1   per-day  day-t  drop-best
     0.5     71  0.568  0.606   +0.130   +0.011   0.04    -0.167
       1    124  0.588  0.661   +0.179   +0.023   0.12    -0.119
       2    185  0.607  0.665   +0.137   +0.004   0.03    -0.114
       5    242  0.623  0.690   +0.314   +0.100   0.59    +0.021
      10    251  0.632  0.693   +0.299   +0.099   0.61    +0.020

    cell        n    ask    win    EV/$1   per-day
    0-0.5      71  0.568  0.606   +0.130   +0.011
    0.5-1      53  0.615  0.736   +0.243   +0.112
    1-2        61  0.647  0.672   +0.054   -0.037
    2-5        57  0.675  0.772   +0.889   +0.502
    5-inf      10  0.888  0.800   -0.094   -0.029

The ask DOES rise with depth (0.568 -> 0.632), so the original objection was
real -- the win rate simply rises faster. **5.0 is the best gate on both
measures and the only one with a positive drop-best-day; past it the ask jumps
to 0.888 and EV turns negative, which is a genuine ceiling rather than a choice.**
EV day-t is 0.59 over 7 priced days, so the ECONOMICS are still not established
at this repo's bar -- only the win rate is.

The old `test_a_deep_cross_is_refused` fixture (~2.6 bps) was asserting the
opposite of the strongest cell on the corpus; it now tests the 5bps boundary.

## Live config projection, model + cross, 30 days compounding (2026-09-13)

`tools/live_config_projection.py`. Sizing read off the LIVE box, which differs
from the repo defaults in two ways that matter: `stake_fraction` is **0.0224**
(not 0.04 -- every earlier projection in this file that used 4% is ~2x too
high in dollars) and `stake_floor_to_minimum` is **True**, so a sub-$1 stake is
raised to the venue floor rather than skipped.

Arm inputs, both from the corpus with the venue's REAL Chainlink strike:
  cross  gate 5.0bps tau 10-60: 1413 crossings / 31 days = 45.6/day (capped 20),
         priced on the 7 quote-carrying days: ask 0.623 win 0.690 EV/$1 +0.314
  model  arms_backtest, 8-day priced corpus: 170.4/day win 0.526 EV/$1 +0.0452

$46.43 start (the live balance), depth capped at the measured $55 at the touch:

    arm          day     median        p10        p90    $/day   $/trade
    MODEL alone    1  $   51.65  $   36.49  $   76.28    +5.22   +0.0320
                   7  $  101.17  $   38.23  $  267.25    +7.82   +0.0541
                  30  $ 1314.36  $  157.51  $ 7172.94   +42.26   +0.2931
    CROSS alone    1  $   57.51  $   47.45  $   65.38   +11.08   +0.5540
                   7  $  194.78  $  116.63  $  305.10   +21.19   +1.0596
                  30  $ 5879.18  $ 4251.22  $ 7438.57  +194.43   +9.7213
    BOTH           1  $   63.64  $   36.75  $   97.12   +17.21   +0.0925
                   7  $  347.75  $  102.92  $ 1143.66   +43.05   +0.2545
                  30  $14652.98  $ 8539.88  $20513.78  +486.89   +2.7190

    BOTH, split by arm (median contribution)
    day     model $     cross $     total $   model tr  cross tr
      1  $     5.95  $    10.52  $    17.21        166        20
      7  $    98.41  $   192.73  $   301.32       1063       123
     30  $  7729.99  $  6837.12  $ 14606.55       4812       559

Cross earns **+$9.72/trade against the model arm's +$0.29** at day 30 on an
eighth of the volume, and carries a far tighter distribution (p10 $4,251 vs
$158). The model arm's value is that it is uncorrelated volume, not edge.

Treat the day-30 levels as directional only: they compound a 7-day-priced edge
for a month and assume $55 of depth is always there.

## Regression found and fixed in the same session: ws book handshake storm

Making the websocket book follow every market (so the recorded families get a
real ask) turned `follow()`'s set-equality check into a live regression. Six
markets roll at different times, so on most polls one id drops out and returns,
and since the topic is a CONNECT-TIME parameter the feed re-handshaked every
10-40s -- **63 reconnects in the first three minutes**, leaving the book stale
for every market including the one that trades.

`follow()` is now ADDITIVE: a market leaving the poll needs no action because
its topic simply stops pushing, so only additions resubscribe, and the set is
trimmed only at MAX_TOPICS=16. Reconnects after the fix: 0.

## The decision loop was coupled to an unrelated market's popularity (2026-09-13)

Found from a live miss the operator spotted: on btc-updown-5m-1789336500 the
spot crossed the strike and the cross arm never fired.

**The crossing was real.** Binance 1s klines, strike 77329.665:

    21:59:03  high 77334.47     <- above the strike
    21:59:04  high 77336.00     <- peak, +0.82 bps through
    21:59:05  back to 77316.01

It was above the strike for ~1.5s. The bot sampled at 21:59:00 (77319.96) and
21:59:05 (77325.51) -- both BELOW -- so `crossed_side`, which compares the
newest tick against one 2-4s older, saw no sign change. The whole excursion fell
between two comparison points.

**Why the gap was 5s and not 1s.** `rounds_by_prefixes` pages the venue market
list 50 at a time and stops only once EVERY requested prefix is found. That list
is volume-sorted, so a quiet family sinks through the day. Measured live:

    pages swept 30, elapsed 3.95s
      btc-updown-5m   offset    0      btc-updown-15m  offset   0
      eth-updown-5m   offset  100      eth-updown-15m  offset  50
      bnb-updown-5m   offset 1450      bnb-updown-15m  offset 200

`bnb-updown-5m` at offset 1450 forced 30 pages EVERY POLL. The loop ran at a
1.00s median earlier in the day and 4.15s by 22:00. Nothing in the config
changed -- BNB simply lost volume. **Trade latency was a function of an
unrelated market's popularity, and the cross arm's detection degrades directly
with it.**

Two wrong diagnoses on the way, both disproved by measurement rather than
argument: the ws-book handshake storm (real, fixed, but reconnects were already
0 while the cadence stayed at 4.15s) and the multi-market ws subscription
(reverted -- cadence did not move).

**Fix**: the traded family is swept every poll; recorded-only families every
`RECORDED_REFRESH_S` (30s). The tape does not need 1s resolution; the arm that
trades does.

**Still open**: even at a 1s poll, `crossed_side` compares two SAMPLED INSTANTS
rather than scanning the interval, so a sub-second excursion can still pass
unseen. The full-rate tick deque holds the data. Scanning the interval for any
strike crossing is the obvious improvement and should be backtested on the
corpus before shipping, not assumed.

## The cross arm's clock was gated on a DIFFERENT arm's switch (2026-09-13)

`_idle()` is the wait between polls. It does not sleep when a tick-resolution
arm is on: it loops on `feed.wait_tick()` and evaluates triggers on every
websocket price update (~114/s). The cross arm has always been built for that
clock -- `crossed_side` reads `feed.ticks`, the full-rate deque.

The gate asked the wrong question:

    if not (settings.entry_mode in ("impulse", "both")
            and settings.impulse_on_tick):
        await asyncio.wait_for(self._stop.wait(), timeout=budget)
        return

So the CROSS arm's latency depended on whether the IMPULSE arm was switched on.
Turning impulse off on 2026-09-13 (entry_mode both -> model, at the operator's
request) silently demoted cross from ~114 evaluations/s to one per poll. Live
confirmation at the time: `want_impulse False | want_cross True -> False`.

Compounded by the market-list sweep above, the poll was 4.15s, so the arm looked
at the price once every four seconds. btc-updown-5m-1789336500 is the cost: spot
crossed the strike to 77336.00 and held above it ~1.5s, and the arm never
sampled it.

**Fix**: the gate now asks which arms want tick resolution --
`want_impulse or want_cross` -- and each tick path is called only if its own arm
is on. An arm's latency no longer depends on an unrelated arm's config.

    stage                     cross evaluations   poll (REST)
    before 2026-09-13         ~114/s              1.00s
    after impulse was off     1 per 4.15s         4.15s
    after both fixes          ~114/s              1.00s

Recorded families now refresh at 28.99s median (RECORDED_REFRESH_S=30), which is
what decoupled the poll from bnb-updown-5m's ranking.

**Still open, unchanged by this**: `crossed_side` compares `ticks[-1]` against a
single reference 2-4s back rather than scanning the interval between them. At
114 looks/s the arm will normally sample DURING an excursion, so this is now
mostly academic -- but it is not the same thing as detecting every crossing.
Backtest the interval-scan variant on the corpus before assuming it helps.

## The seven "missed" crossings were refusals, not misses (2026-09-14)

Replayed all 78 btc-updown-5m rounds from 22:00 UTC against the VENUE's own
strike (`rounds.strike`) and Binance 1s klines (tools/missed_crossings.py).
The arm's close-rule fired on 14 rounds; the bot logged 8. All seven gaps are
now accounted for, and the crossings were real -- verified present in the bot's
OWN recorded spot feed, not just in klines, with closest approaches of
0.00-1.82 bps against a 5.0 bps gate.

**Five of seven: refused on purpose.** `cross_skip_opposite_to_main` -- the main
arm held the opposite side. 164 log lines across 11 distinct rounds. Not a bug:
that cell measures -0.107/$1 against +0.356 same-side.

**Two of seven** (22:10 and 22:30 UTC) have NO cross log line at all: they
predate the 22:48 restart that put the arm back on the websocket clock, so it
never looked while price was on the far side. Same cause as the round the
operator first spotted.

Both causes are now gone: the feed-clock fix shipped 22:48, and with
`entry_mode=off` there is no main arm to hold an opposite side (the gate skips
any fill whose pool is `reserved`, and cross fills are). Last opposite-side
refusal 03:54 UTC; model arm off at 04:16.

**The gate was suppressing roughly half the arm's opportunities** -- 11 rounds
refused against 8 taken -- to avoid trading against an arm since measured at
exactly zero margin (win 0.64971 vs ask 0.64967 over 688 fills).

### The real defect: refusals were invisible

An audit of every exit from `_cross_tick` found 19 return paths, **11 entirely
silent** and NONE writing a decision row. The damaging ones are the refusals
that happen AFTER `crossed_side` returns a side -- depth-exceeded and
ask-out-of-band -- because in the database those are indistinguishable from
"no crossing happened". That is why separating a refusal from a miss required
replaying the venue strike against klines.

`_cross_refuse()` now records every post-detection refusal as a
`cross refused: <reason>` decision row, **deduped per (round, reason)**. The
dedupe matters for a second reason: the one path that did log wrote on every
evaluation, and at ~114 evaluations/s that produced 164 lines for 11 rounds.
One round, one reason, one row.

Three tests asserted `decisions == 0` on refusal -- they encoded the old
silence as correct, and now assert the invariant that actually matters (no
ORDER went out) while allowing the refusal row.

## The cross arm is regime-dependent, and that is a gate (2026-09-14)

Chasing a 4-hour drought -- 3 attempts against ~45/day projected -- turned up
something better than the bug I was looking for.

**Crossings did not stop; they moved.** Over 48 rounds in the last 4h, 79% still
crossed the strike somewhere, but ZERO did so inside tau 5-60:

    window          rounds  crossed anywhere  tau<5  tau5-60  tau60-120  tau120-300
    T-26h..T-14h       144      47 (65%)        11      35        60         115
    last 4h             48      38 (79%)         1       0        22         124

Not a data problem: the recorded tape's median sample spacing is 1.00s in both
windows. Not the depth gate either -- in the last 4h there are zero DETECTIONS,
before any gate, and when detections do occur their depth is a median 0.6-1.1bps
against a 5.0 gate that never binds.

What changed is the regime. Median 5-minute range went 5.8 -> 11.4 bps: price
now crosses once early and trends away, instead of oscillating near the strike
into expiry. The arm's window is built for the latter.

### The edge depends on how far the round has already travelled

Measured with the range taken from the round OPEN to the TRIGGER instant -- the
causal version; an earlier cut used the full-round range, which is the future
and therefore worthless as a gate:

    range-so-far    n    ask     win    EV/$1
      <=5 bps      74   0.762   0.865  +0.661
      5-8          50   0.703   0.760  +0.086
      8-12         45   0.674   0.711  +0.052
      12-20        15   0.669   0.467  -0.324

Monotonic, and mechanically sensible: a crossing in a calm round sticks, one in
a round that has already swung wide reverses again.

### Chosen on COMPOUNDED growth, not on EV/$1

A tighter gate has the better per-trade edge and too few trades. Daily
log-growth at the shipped 5.04% sizing, priced at what we actually fill at:

    gate      n/day   add +0.088 slip   mean-revert 0.47x+0.40
    off        64.4      0.1535            0.2651
    <=8        37.3      0.2436            0.3090
    <=10       46.0      0.2473            0.3463   <- best under BOTH
    <=12       51.9      0.2347            0.3376

**+61% daily growth under additive slippage, +31% under mean-reverting**, and
8/10/12 all beat OFF under both -- so the choice is not knife-edge.

Shipped `cross_max_round_range_bps = 10.0` (0 = OFF). Read from `self._ticks`,
not `feed.ticks`: the feed deque is 4000 ticks, which at BTC's live ~114/s is 35
seconds and does not span a 5-minute round.

CAVEAT: day-t at <=10 is 1.72, under this repo's t=2 bar (<=8 is 2.07). It ships
on the growth result holding across two independent slippage models, not on the
day-t. Revisit at ~30 more fills.

### Incidental: the 4h drought was protective, not costly

In the trending regime the arm's own tau 5-60 cell measures NEGATIVE (-0.074/$1
against +0.459 in calm rounds). The rounds it declined to trade were rounds it
would have lost on.

## Two arms, two regimes (2026-09-14)

The model arm's zero lifetime margin -- win 0.64971 against an ask of 0.64967
over 688 live fills -- is the AVERAGE OF TWO OPPOSITE HALVES, and the split runs
the other way from the cross arm's. Range is measured open -> entry (causal).

    range-so-far     CROSS arm        MODEL arm
      <=5 bps        +0.661/$1        -0.000/$1
      5-8            +0.086           +0.092
      8-12           +0.052           +0.088
      12-20          -0.324           +0.090
      >20              --             -0.083

Day-clustered on the model arm's own live fills, 7 days:

    all fills     n=686 per$1 -0.0070 day-t -0.11 drop-best -0.0406 neg 3/7
    range <= 3.6  n=339 per$1 -0.1274 day-t -0.77 drop-best -0.2364 neg 4/7
    range 5-20    n=223 per$1 +0.1160 day-t +1.80 drop-best +0.0647 neg 1/7
    range > 20    n= 24 per$1 -0.1769 day-t -0.77 drop-best -0.3135 neg 3/5

5-20 carries +$88.73 of the arm's +$59.37 lifetime total. Outside that window it
is a measured loser in BOTH directions. Shipped `model_min/max_round_range_bps`
= 5/20, gated in the `gate_reason` chain so every refusal is recorded like any
other model-arm gate. `_round_range_bps()` is now shared by both arms.

### What cross should do alongside it -- measured, not assumed

    OVERLAP band (5-20bps), where the model now trades
      cross, model not in round   n=118  EV +0.142  day-t +1.52
      cross SAME side as model    n= 73  EV +0.336  day-t +3.01
      cross OPPOSITE to model     n= 78  EV -0.108  day-t -0.29

    CALM band (<5bps), where the model now stands down
      cross SAME side as model    n= 41  EV +0.466  day-t +3.10
      cross OPPOSITE to model     n= 49  EV +0.222  day-t +2.60
      cross, all of calm          n=151  EV +0.534  day-t +2.37

**Keep cross ON and keep skip_opposite_to_main TRUE.** Same-side cross in the
overlap is the strongest cell measured anywhere in this file (+0.336, day-t
3.01); turning cross off would throw it away.

The two changes interlock: opposite-side cross LOSES in the overlap (-0.108) but
MAKES money in calm rounds (+0.222, day-t 2.60), so the skip gate was costing
money below 5bps. Gating the model arm out of the calm band removes the model
position there, so the skip stops applying exactly where it was harmful. Neither
change is correct without the other.

CAVEAT: model day-t 1.80 is under the t=2 bar. It gates an arm that was OFF, so
the question is where it may trade rather than whether to add risk blindly.

## Correcting the model window on a replay, not on live fills (2026-09-14)

The 5-20 window shipped earlier was built on 686 LIVE FILLS. Replaying the arm's
real `evaluate()` gate over the recorded history gives 1,159 entries over 1,650
rounds and moves the top end:

    range    n    ask    win     EV     day-t  drop-best
    <3      513  0.591  0.635  +0.066   +0.31   -0.021
    3-5     253  0.629  0.652  +0.041   +0.40   -0.018
    5-8     198  0.646  0.737  +0.146   +2.18   +0.085
    8-12    105  0.648  0.762  +0.186   +1.67   +0.078
    12-20    70  0.662  0.700  +0.007   -0.62   -0.221   <- was inside the window
    >20      20  0.691  0.600  -0.154   +0.33   -0.046

### The trap in that table

Read raw, it says drop the window entirely: ALL is EV +0.078 at day-t +4.95,
and 0-12 is +0.087 at day-t +8.48 -- both with far more trades. That is an
artifact of pricing at the BOOK ask. Priced at what we actually fill at, the arm
is NEGATIVE everywhere except a narrow band:

    window    n   | add +0.088 EV  growth | mean-revert EV  growth
    ALL     1159  |    -0.058     -1.632  |    -0.039      -1.159
    0-12    1069  |    -0.051     -1.345  |    -0.037      -1.000
    0-20    1139  |    -0.054     -1.519  |    -0.037      -1.073
    5-20     373  |    -0.005     -0.079  |    +0.036      +0.260
    5-12     303  |    +0.018     +0.095  |    +0.053      +0.332   <- ships
    5-10     262  |    +0.019     +0.089  |    +0.056      +0.301

Shipped `model_max_round_range_bps` 20 -> 12. The LOWER bound at 5 is what makes
this arm viable at all; 12 is where it stops paying. Even at 5-12 it is only
marginally positive (+0.018/$1 under the harsher slippage model) -- a supporting
arm, not a driver.

**Same lesson as the cross `look_s` sweep**: any knob swept at the book ask
gives the wrong answer, because slippage is roughly the whole margin. Every
future sweep on this arm has to be repriced before it is believed.

### Stacking, re-tested on the corpus

An earlier note used 20 live stacked rounds and concluded same-side stacking was
a loser (-0.171/$1). On 1,644 replayed rounds it is not:

    model alone                       981 rounds  per$1 +0.115  won 687/981
    cross alone                        42 rounds  per$1 +0.569  won  30/42
    STACKED model + same-side cross    84 rounds  per$1 +0.379  won  73/84
    HEDGED  model + opposite cross    114 rounds  per$1 -0.003  won  64/114

Keep stacking, keep `cross_skip_opposite_to_main`. The real cost of stacking is
in the TAIL, not the mean: worst stacked round -7.75 stake-units against -1.00
for model-alone, because a same-side pair is 1 + 2.25 = 3.25 units on one
outcome. 87% of those rounds win; the 13% is what a -$4.05 round looks like.

## A single-methodology audit of every cross knob (2026-09-14)

Prompted by a fair challenge -- after several knobs were re-derived and
reversed, how is any of them to be trusted? The answer is that they have to be
tested the same way, so here is one audit, one method, every knob:

  1. REAL Chainlink strike (rounds.strike), never spot-derived
  2. REAL venue ask at the trigger instant
  3. PRICED AT WHAT WE FILL AT, under BOTH slippage models. A knob swept at the
     book ask gave the wrong answer three separate times this session
     (cross_look_s, the model window, nearly the stacking decision) -- slippage
     is roughly the entire margin on both arms.
  4. Scored on COMPOUNDED GROWTH, sum of log(1+f*r), not EV/$1. A knob that
     raises EV by cutting volume can still lose money.
  5. day-clustered t and drop-best-day alongside.

A knob counts as SUPPORTED only if the shipped value is at or near the growth
optimum under BOTH models.

### The uncomfortable result

    knob (shipped)          n    growth add  growth rev  day-t  drop-best
    depth 5.0             136      0.167      0.513      0.38   -0.018
    min_tau 5             136      0.167      0.513      0.38   -0.018
    max_tau 60            136      0.167      0.513      0.38   -0.018
    round_range 10        136      0.167      0.513      0.38   -0.018
    min_price 0.10        136      0.167      0.513      0.38   -0.018
    max_price 0.90        136      0.167      0.513      0.38   -0.018
    per_round 3           136      0.167      0.513      0.38   -0.018
    look_s 2.0            136      0.167      0.513      0.38   -0.018

**The configuration in place before this audit had day-t 0.38 and a NEGATIVE
drop-best-day** -- a single day carried it -- and on nearly every knob the two
slippage models preferred different values. Those knobs are NOT established.
They are not obviously wrong on 7 days of data, which is a much weaker claim
than the comments next to them imply.

### What actually survives

    config                    n  growth add  growth rev  day-t  drop-best
    shipped (look2, cap3)   136     0.167      0.513     0.38    -0.018
    look 1.0, cap 3         108     1.223      1.302     3.24    +0.196
    look 2.0, cap 8         257     1.407      2.015     2.08    +0.087
    look 1.0, cap 8         170     1.617      1.823     3.54    +0.162
    look 1.0, cap 5         144     1.550      1.731     5.52    +0.230

`look 1.0 / cap 5` is the only setting in the audit that clears day-t 2 with a
positive drop-best-day under BOTH slippage models, at a trade count close to the
old config. Not outlier-driven -- per-entry EV at real fills is +0.263, +0.157,
+0.292, +0.168, +0.207 for entries 0-4, then decays (-0.054, +0.336, -0.006).

Mechanism for the look: a shorter look-back confirms against a fresher
reference, so the order is out before the venue reprices. The measured ask falls
0.690 -> 0.644 as the look shortens, and that spread is the whole margin.

Shipped `cross_look_s` 2.0 -> 1.0 and `cross_max_per_round` 3 -> 5.

### The standing rule this establishes

Any future sweep on either arm must be repriced at realistic fills and scored on
growth before it is believed, and must agree under both slippage models. A knob
that only looks good at the book ask is not a finding.

## The model arm's own knobs, audited -- and switched off (2026-09-14)

### Correction: the live model values were never the config defaults

    config defaults (quoted in earlier notes):  min_edge 0.12 | band [0.25,0.90] | haircut 0.85
    actual live (.env overrides):               min_edge 0.01 | band [0.45,0.80] | haircut 1.0

Same class of error as the `stake_fraction` 0.04-vs-0.0224 mix-up: reading the
config default instead of what `.env` deploys. Any earlier model-arm number
produced by a tool run LOCALLY (rather than on the box) used the defaults and is
suspect for that reason.

### The sweep

Gate reimplemented inline for speed, then checked against the real `evaluate()`
-- 100% agreement on 6,000 samples. The first attempt scored 93.8% (it selected
by edge, while `evaluate` selects by EV and also requires `edge > hurdle`), and
was discarded rather than reported. The check is the point.

    knob                   n    growth add  growth rev  day-t  drop-best
    SHIPPED config       303      +0.095      +0.332     0.25    -0.019
    min_edge 0.16         55      +0.032      +0.119     1.27    +0.044
    min_edge 0.12        117      -0.114      +0.069     0.54    -0.021
    haircut 0.85         300      -0.087      +0.178    -0.45    -0.053
    persistence 1        350      -0.100      +0.145    -0.90    -0.038
    persistence 3        274      -0.133      +0.122    -1.08    -0.112
    min_quote 0.25       370      -0.900      -1.176    -1.88    -0.175
    max_quote 0.90       352      -0.006      +0.487    -0.02    -0.015
    max_tau 270          184      +0.067      +0.151     0.69    +0.002

**No setting clears the bar.** The live configuration has day-t 0.25 and a
NEGATIVE drop-best-day; only two variants have a positive one and both have
lower growth than what was shipped. There is no robust-and-better setting.

Against the cross arm at its audited settings (day-t 5.52, drop-best +0.230),
the model arm is a marginal contributor carrying two concrete costs: it spends
the account-wide 20% daily stop that halts cross, and it re-arms
`cross_skip_opposite_to_main`, which was suppressing ~half of cross's triggers.

Switched off. `entry_mode=off`.

### Final state

    ARMS   model OFF | cross ON | impulse/double/stale_maker/probe off
    CROSS  look 1.0s | per-round 5 | tau 5-60 | depth 5.0 | round-range <=10
           price [0.10,0.90] | stake 0.0224 x 2.25 = 5.04% | cap/day OFF
    RISK   daily stop 20% | max consecutive losses 12

### What is and is not established

ESTABLISHED (day-t > 2, positive drop-best, both slippage models agree):
  cross_look_s 1.0 and cross_max_per_round 5, together.

NOT ESTABLISHED, merely not-obviously-wrong on 7 days:
  cross depth 5.0, tau bounds 5-60, round-range 10, price band, stake mult 2.25.
  All sat at day-t 0.38 with a negative drop-best-day in the audit, and their
  two slippage models disagreed about the best value.

The binding constraint is 7 days of recorded history. No further re-analysis
fixes that; only more days do.

## The same-round reversal: a hedge that was never a hedge (2026-09-14)

The 12:05 round bought Up at 0.611 and, 22 seconds later, Down at 0.589 --
~4.34 shares each way. The overlap pays 1.000 for 1.199, so the pair was a
locked -$0.87 before the round resolved. First occurrence in 14 cross rounds;
it only became reachable when `cross_max_per_round` went 3 -> 5.

### The decision collapses, and the "locked loss" is a red herring

With the first position sunk, at the instant of the second trigger:

    decline:  S_A if A wins, else 0
    take:     the same, plus S_B if B wins, minus stake_B

    EV(take) - EV(decline) = stake_B * [ (1 - p)/b - 1 ]      p = P(A wins)

positive iff `1 - p > b`. **The first leg's price cancels entirely.** So the
overlap arithmetic decides nothing: the only question is whether a reversal
trigger beats its own ask. That is a win-rate question, which the 31-day
Chainlink corpus can answer even though only 5 days carry quotes.

### Three substrates, one answer

    substrate                    entry     n    win     ask    edge
    31d Chainlink corpus         first   839  0.714      --      --
                              REVERSAL   249  0.574      --      --
    5 priced corpus days         first   167  0.749   0.664  +0.084
                              REVERSAL    34  0.559   0.581  -0.022
    7d recorded venue book       first   150  0.880   0.671  +0.209
                              REVERSAL    14  0.643   0.691  -0.048

Win-rate gap day-t **-3.05 over 27 days** on the corpus (-1.51 over 5 on the
recorded book). Growth of the reversals ALONE is negative under every pricing
tried -- corpus -0.120 additive / -0.353 mean-reverting; recorded -0.043 book /
-0.117 additive / -0.091 reverting -- which satisfies the standing two-model
bar. The venue prices a reversal about right and underprices a FIRST crossing;
the arm's whole edge is in the first one.

The EV/$1 of reversals reads +0.331 on the priced days, which is a Jensen
artefact of comparing mean-win to mean-ask across varying asks. Repriced at
actual fills and compounded, it is negative. Another instance of the rule that
a knob scored at the book ask is not a finding.

### RETRACTED: "consecutive crosses alternate sides by construction"

Both halves of the old comment were wrong.

  * Alternation is an artefact of the corpus's coarse 10s grid. At the recorded
    1s cadence **51 of 65 non-first entries repeat the SAME side**, because
    spot can re-cross and come back inside one look window.
  * Where it does alternate it is not a hedge, per the algebra above.

### What shipped, and what deliberately did not

This is NOT an argument for a smaller cap -- the opposite. Same-side repeats
are the bulk of what the cap buys and are good on their own (n=51, growth
+0.451 book / +0.143 additive / +0.404 reverting, day-t +3.57 / +1.31 / +2.43).
Removing only the reversals raises recorded growth under all three pricings
(additive 1.800 -> 1.917, reverting 1.985 -> 2.076), drop-best-day still
positive.

Shipped `cross_allow_reversal = False`, cap left at 5. The guard is
in-process (`_cross_side`), so a restart mid-round forgets the side: `Fill`
carries no `mode`, so reading it back would mean a per-tick DB query on the
websocket clock, and blocking on any held token is the arms-independence rule
this arm exists outside of.

### Power, stated honestly

The win-rate gap is the well-powered half (n=249 reversals, 27 day-blocks).
The growth improvement is small and rests on 14 recorded reversals over 7 days;
its value is that it is consistently signed across three pricings and two
substrates, not that any single number is significant.

## RETRACTED: "the missed fills were a fixed bug" (2026-09-14)

**This section's query was wrong and its conclusion with it.** `acted` in the
`decisions` table means THE ORDER FILLED, not "an order was placed" (main.py:
"`acted` means an order actually filled, so it is written as 0 here"; it is
promoted to 1 only after the broker confirms). So "acted=1 with no fill" can
never find an unfilled attempt -- it searches for a state that by construction
does not exist. The right predicate is a decision whose `reason` records an
attempt (`cross into ...`) and which has no fill row. See the section below for
what that actually shows: a live fill rate of 0.650, not 1.000.

What follows is still true about the 09-08 cluster, and still wrong about
"there has not been an unfilled attempt since".

### Original text

## The "missed fills" were a fixed bug, not a design choice (2026-09-14)

133 decisions in the whole ledger have `acted=1` and no fill:

    2026-09-08   132
    2026-09-11     1
    2026-09-12+    0

All at the flat $1 stake of the pre-sizing era. The 09-08 cluster is a retry
loop -- same round, same side, one attempt per ~1.5s at quotes 0.87-0.90.
Unfilled quotes skew expensive (median 0.810) against 0.670 for filled orders
in the same window, which is the signature of the display-lag problem: a limit
priced off the venue's stale *displayed* price never matched its real book.

`order_type=limit_take` shipped 2026-09-12 and there has not been an unfilled
acted decision since. The last 60 acted decisions all filled; no voids since
09-09, no orphaned fills, nothing in the log. Not by design, and already fixed.

## The 12:45 "missed fills" were the tau window doing its job (2026-09-14)

Two crossings around 12:45 UTC were not traded. They were real, and they would
have won -- and skipping them was still right.

    btc-updown-5m-1789389900   12:45:00   strike 77918.005   settled Down
       12:45:07  tau 292.7  Down  depth 0.00  ask 0.510
       12:45:19  tau 280.7  Down  depth 0.93  ask 0.500
       12:45:37  tau 262.3  Down  depth 0.00  ask 0.460
       12:45:39  tau 260.3  Up    depth 1.03  ask 0.580
       12:45:57  tau 242.3  Down  depth 0.00  ask 0.490
       12:45:59  tau 240.3  Down  depth 0.26  ask 0.540

Six crossings, all in the round's FIRST minute (tau 240-293). The arm trades
tau 5-60, so it never looked. No decision row is written on that path because
`crossed_side` is never reached -- the tau test precedes it.

### Why the window is not a candidate for widening

Swept on the corpus, reversals already excluded:

    tau band      n(31d)   win     |  n(priced)   ask    edge   growth add / rev
    5-60             839  0.714    |       169  0.663  +0.083   +0.226 / +0.042
    60-120          1005  0.662    |       150  0.657  +0.043   -0.275 / -0.255
    120-180         1101  0.641    |       121  0.635  +0.034   -0.595 / -0.441
    180-270         1505  0.664    |       171  0.635  +0.032   -0.849 / -0.614
    5-270           2486  0.708    |       420  0.652  +0.036   -1.288 / -1.204

**tau 5-60 is the only band that compounds positively, and it does so under
both slippage models.** Every longer band is negative under both. Widening to
5-270 to catch rounds like this one turns +0.226/+0.042 into -1.288/-1.204.

The mechanism is the arm's own premise: the edge is "the nick the venue has not
registered yet", and that requires the round to end before the venue reprices
or the spot crosses back. At tau 280 there are four more minutes for both.

This round is a genuine counter-example -- both early Down entries at ~0.505
would have paid ~+0.96/$1. It is one round against 2,486 corpus triggers
pointing the other way.

Note the model arm used to cover tau 60-210 and is now off, so that stretch is
traded by nothing. That is correct rather than a gap: cross has no usable edge
there (above) and the model arm had none either, which is why it was switched
off.

### Caveat found while doing this: over half the recorded quote rows have no ask

    quotes rows with a spot                     749,458
    quotes rows that also carry up_ask/down_ask 362,981   (48.4%)

The 12:45 round alone is 85 of 263 rows missing an ask. Any replay that filters
on `up_ask is not null` -- which the first pass of this investigation did, and
which `cross_reversal_recorded.py` still does -- is detecting crossings on
fewer than half the ticks and will UNDERCOUNT triggers. It does not flip the
reversal result (the 31-day corpus, which has no such holes, agrees in sign and
carries the power), but recorded-book trigger COUNTS are a floor, not a census.

## The cancelled orders: adverse selection, and why NOT to widen the buffer (2026-09-14)

Two CANCELLED orders on the venue at 12:14:01 and 12:14:37 that the ledger's
`acted` flag could not surface. Both real, both `orderType: LIMIT`, both
`filledShareQty: 0`, both with `makerShareQty` > 0 -- they RESTED below the
book instead of taking it, and terminated ~21s later (`limit_wait_s` is 20).

    12:14:01  Down  decided 0.37  limit 0.44  makerShareQty 5.93  CANCELLED
    12:14:37  Up    decided 0.32  limit 0.38  makerShareQty 6.86  CANCELLED

### The real fill rate is 0.650, not 1.000

Counting attempts properly (`reason like 'cross into%'`, no fill row):

    day          attempts  unfilled  fill rate
    2026-09-13          9         1      0.889
    2026-09-14         15         7      0.533

And it collapses with price:

    decided ask    n   filled   rate
    0.00-0.40      5        0   0.00
    0.40-0.50      4        3   0.75
    0.50-0.60      7        6   0.86
    0.60-1.00      4        4   1.00

**Below 0.40 nothing fills, ever.** That is the arm's most profitable bucket.

### Mechanism: a proportional buffer against an additive move

`buf = max(0.01, min(cross_limit_buffer 0.10, cross_limit_buffer_pct 0.20 * ask))`

so the buffer SHRINKS as the ask falls -- 0.10 at ask 0.50+, 0.064 at 0.32,
0.028 at 0.14 -- while the thing it has to cover does not. When a fill happens
it lands at the LIMIT, not at the decided ask (mean got-limit -0.028, versus
got-decided +0.063), so the limit is effectively the price.

### Why widening it anyway would be a mistake

Tracking what the ask actually did in the 25s after each miss:

    time      side  decided  limit  ask went to   winner  P/L @limit  P/L @real
    00:04:15  Up       0.45   0.54         0.89       Up      +0.835     +0.121
    01:44:00  Up       0.39   0.47         0.62     Down      -1.000     -1.000
    03:14:11  Down     0.14   0.17         0.47     Down      +4.785     +1.105
    08:44:00  Down     0.33   0.40         0.96     Down      +1.470     +0.041
    08:54:47  Down     0.57   0.67          n/a     Down      +0.483        n/a
    12:14:01  Down     0.37   0.44         0.64       Up      -1.000     -1.000
    12:14:37  Up       0.32   0.38          n/a       Up      +1.599        n/a
    ----------------------------------------------------------------------------
    total per $1:                                            +7.171     -0.733

**At the price we bid these are worth +7.17/$1. At the price actually on offer
they are worth -0.73/$1.** The book moved 0.23-0.63 on every miss, not the
~0.09 a wider buffer could plausibly cover. 5 of 7 would have won, and it does
not matter: at the real ask the two losers (-1.000 each) swamp the winners
(+0.121, +1.105, +0.041).

This is textbook adverse selection. We fill when the book stays put and miss
when it runs -- and it runs precisely when we are right. The limit refusing to
chase is the mechanism working, not failing. A corpus EV of +0.257/$1 for
paying "ask + 0.10" on sub-0.40 entries is irrelevant, because +0.10 does not
reach the book; the real price is +0.23 to +0.63.

**Do not raise `cross_limit_buffer_pct` or `cross_limit_buffer` on this
evidence.** Nothing shipped from this investigation.

### Two misses were a dark book, not a runaway one

08:54:47 and 12:14:37 show an ask that never rose above the decided price, yet
a limit 0.10 above it did not fill -- and in both the recorded ask series stops
at the attempt instant. That is the missing-ask problem (48.4% of quote rows
carry no ask), not a pricing one. Those two are unexplained by the mechanism
above and are the honest residual.

### Counter-example against today's reversal guard, recorded for fairness

12:14:01 Down / 12:14:37 Up is another same-round reversal pair, so the guard
shipped earlier today would have refused the second -- which would have won
(+1.599 at our limit). One sample against 249 corpus reversals at day-t -3.05;
the guard stands, but the case is logged rather than buried.

## The round-range gate, re-audited: it IS helpful (2026-09-14)

The knob audit listed `cross_max_round_range_bps` under NOT ESTABLISHED --
day-t 0.38, negative drop-best-day. That verdict was reached at the OLD config
(look 2.0, cap 3) and with reversal entries still included. Re-run at the
shipped config with reversals excluded, on all three substrates:

    31-DAY CHAINLINK CORPUS (win rate -- the powered half)
      gate      n   rounds    win   win day-t vs OFF
       OFF   1164     1164  0.696        --
         5    331      331  0.689      -0.26
         8    676      676  0.709      +1.79
        10    839      839  0.714      +2.40
        12    947      947  0.716      +2.25
        15   1046     1046  0.709      +2.61
        20   1114     1114  0.702      +1.15

    7-DAY RECORDED VENUE BOOK (real asks, compounded growth)
      gate     n    win   additive g / t / drop     reverting g / t / drop
       OFF   293  0.802   +1.451 / +1.28 / +0.566   +1.616 / +1.79 / +0.986
         8   161  0.894   +1.742 / +2.01 / +0.799   +1.795 / +3.14 / +1.141
        10   201  0.881   +1.917 / +2.27 / +0.964   +2.076 / +3.55 / +1.395
        12   230  0.852   +1.816 / +2.45 / +0.966   +1.977 / +4.08 / +1.400
        15   251  0.829   +1.575 / +1.89 / +0.725   +1.761 / +3.02 / +1.184
        20   270  0.815   +1.518 / +1.54 / +0.642   +1.683 / +2.28 / +1.069

**Gate 10-12 beats OFF under BOTH slippage models on the recorded book, with
day-t above 2 and a positive drop-best-day.** That clears the standing bar, so
the knob moves from NOT ESTABLISHED to SUPPORTED -- the audit's earlier reading
was an artefact of scoring it at a config that no longer exists.

### The dissenting substrate, and why it loses the tie

The 5 priced corpus days disagree -- there gate 10 (+0.226 add / +0.042 rev)
is WORSE than OFF (+0.448 / +0.126), with gate 15 best. But every row on that
substrate has day-t below 1.0, and its own resweep docstring says to treat a
sweep over it as a hypothesis rather than a tuning. Two substrates with power
agree; the one without disagrees.

### The value is a plateau, not a point

    substrate            8      10      12      15      20
    corpus win day-t   1.79    2.40    2.25    2.61    1.15
    recorded add t     2.01    2.27    2.45    1.89    1.54
    recorded rev t     3.14    3.55    4.08    3.02    2.28

10-15 is flat, centred near 12. Gate 12 is never worse than 10 on any of the
three substrates and mildly better on two, at +14% volume (230 vs 201 recorded,
947 vs 839 corpus) -- but the gap is well inside noise, and picking the grid
peak is exactly what produced the config this audit dismantled. Left at 10.
Anything in 10-15 is the same bet.

## Can a regime gate rescue the model arm? No -- and the test is the point (2026-09-14)

Raised because the cross arm's round-range gate went NOT ESTABLISHED ->
SUPPORTED hours after the model arm was switched off, and the sweep that
switched it off never tested a range window. The arm deserved the same question.

`tools/model_regime_rescue.py`: 690 settled live fills, real filled prices,
range-so-far measured round-open -> ENTRY instant, growth under both slippage
models, day-clustered t and drop-best-day.

    range at entry      n    win    ask    edge   growth(realised)/t/drop
    0-3 bps           293  0.553  0.568  -0.015   -0.289 / -1.11 / -0.397
    3-5 bps           146  0.678  0.643  +0.035   +0.121 / +0.37 / -0.133
    5-8 bps           115  0.739  0.683  +0.056   +0.152 / +0.83 / +0.038
    8-12 bps           67  0.761  0.710  +0.051   +0.118 / +0.72 / +0.004
    12-20 bps          45  0.778  0.729  +0.049   +0.078 / +0.80 / +0.014
    20+ bps            24  0.708  0.738  -0.030   -0.049 / -1.10 / -0.065

    window 5-12       182  0.747  0.693  +0.054   +0.270 / +2.08 / +0.153
    as shipped (all)  690  0.651  0.633  +0.017   +0.131 / +0.23 / -0.180

The 5-12 window looks like a rescue. It is not:

    model 5-12, day-t +2.08, df 6      nominal p = 0.083   (not even p<0.05)
    after the 12 post-hoc slices I took           p = 0.645
    edge +0.054, 95% CI [-0.009, +0.117]          STRADDLES ZERO

    for contrast, the cross gate that DID pass:
    gate 10 recorded reverting t +3.55            p = 0.012
    gate 12 recorded reverting t +4.08            p = 0.007
    and it replicated on three independent substrates.

The model arm's best regime slice is what picking the maximum of twelve looks
like under the null. The cross gate is not.

### The simplest statement of the problem

    model arm, 7 days of REALISED P/L
      total          +47.27
      best day       +48.58   (09-09)
      DROP BEST DAY   -1.31
      mean/day        +6.75   sd 26.42   day-t +0.68

**The entire week's profit is one day.** That is the same drop-best-day failure
the knob audit found, restated in dollars and immune to any slicing argument.

### CORRECTION to the reason it was switched off

The switch-off note said the arm "re-arms `cross_skip_opposite_to_main`, which
was suppressing ~half of cross's triggers". That 52% figure came from an
earlier config and no longer holds:

    post-detection cross outcomes, lifetime : 29
    refused to the model arm                :  2   (6.9%)

So the interaction cost is ~7%, not ~50%. The case against the model arm rests
on the arm's own record, not on what it does to cross. The other cost -- that
it spends the account-wide 20% daily stop that halts cross -- still stands.

### Why this is not a knob problem

Lifetime the arm won 0.64971 against an average ask of 0.64967 over 688 fills:
zero margin to four decimals. Its own EV estimate was measured ANTI-informative
(n=121). Cross's edge is MECHANICAL -- spot crossed, the venue has not repriced
yet, and that is a latency fact. The model arm's edge would have to be
PREDICTIVE: a better estimate of P(up) at tau 60-210 than the venue's own book.
Nothing measured suggests we have one, and no knob creates one.

Reopening it is a modelling project (a better predictor), not a configuration
change. Left off.

## The model arm, replayed PROPERLY -- and the reason it cannot be tuned (2026-09-14)

### Method correction first

`tools/model_regime_rescue.py` scored only the 690 orders that actually FILLED,
under whatever config happened to be live at the time. Both halves are wrong:
fills are survivorship-conditioned (the cross work proved unfilled attempts are
systematically the cheap, high-payoff ones), and that week pooled several
config eras -- the repo has a commit for exactly that mistake. It also ran at
config.py DEFAULTS rather than the deployed `.env` values, the error FINDINGS
already records twice.

`tools/model_regime_replay.py` replays every trigger the gate WOULD take from
the recorded book, under ONE fixed configuration, at the deployed values
(min_edge 0.01, band [0.45,0.80], haircut 1.0), one position per round.

    regime (causal)      n    win    ask    edge  book g/t/drop     additive g/t/drop  reverting g/t/drop
    no gate           1657  0.655  0.639  +0.016  +0.441/+2.17/+.28  -4.127/-4.79/-3.95  -3.029/-5.31/-2.92
    0-3 bps            615  0.600  0.594  +0.006  -0.099/-0.27/-.23  -1.871/-5.06/-1.81  -1.861/-4.86/-1.83
    3-5 bps            371  0.652  0.644  +0.008  +0.032/+0.09/-.25  -0.975/-2.96/-1.02  -0.713/-2.50/-0.81
    5-8 bps            336  0.696  0.674  +0.023  +0.190/+0.84/+.03  -0.702/-1.99/-0.75  -0.311/-1.09/-0.41
    8-12 bps           199  0.734  0.674  +0.060  +0.356/+2.89/+.24  -0.201/-1.37/-0.24  +0.043/+0.41/-0.02
    12-20 bps          107  0.682  0.685  -0.003  -0.052/-0.36/-.13  -0.317/-2.11/-0.31  -0.173/-1.20/-0.20
    WINDOW 5-12        535  0.710  0.674  +0.037  +0.546/+2.19/+.29  -0.904/-1.91/-0.99  -0.268/-0.79/-0.43

**Not one window clears the two-model bar.** Every single row is negative under
the additive model. The best (8-12 bps) is +0.043 under mean-reversion with a
NEGATIVE drop-best-day.

### The one number that explains the whole arm

    model arm edge at the book:  +0.016 ungated, +0.060 in its best regime
    measured slippage:           +0.088 additive

**The edge is smaller than the slippage.** That is why the arm reads positive at
the book ask (+0.441, day-t 2.17) and hugely negative once filled properly
(-4.127). For contrast the cross arm's first-entry edge on the same book is
+0.209 -- roughly 2.4x its slippage, which is why one survives execution and
the other cannot.

Any replacement model must clear **edge > ~0.09 after gating**, out of sample.
That is the bar, stated quantitatively.

## Is there anything to recalibrate? No -- the signal is not informative (2026-09-14)

`tools/model_information_test.py`, 133,289 evaluations over 1,336 rounds,
thinned to 12/round, every interval bootstrapped over ROUNDS. Discrimination
(AUC), not Brier, because `quote` is an ASK and AUC is invariant to the
monotone spread bias that would otherwise flatter or punish it.

    predictor                     AUC       95% CI (round-clustered)
    venue quote (the ask)      0.7129   [0.6938, 0.7304]
    our fair                   0.6938   [0.6716, 0.7134]
    our edge = fair - quote    0.4906   [0.4656, 0.5127]   <- a coin flip

    tau band     AUC quote  AUC fair   fair beats quote?
    0-60            0.6701    0.6282         no
    60-120          0.7142    0.6923         no
    120-210         0.7188    0.7118         no
    210-270         0.7179    0.6872         no

    incremental beta of (fair-quote) on (outcome-quote):
        -0.1141   95% CI [-0.4486, +0.1865]   INDISTINGUISHABLE FROM ZERO
    Brier: our fair 0.20562   venue quote 0.20074

**The quantity the arm trades on -- its disagreement with the venue -- ranks
outcomes at AUC 0.49.** It is noise. Our fair is worse than the quote at every
tau band, and adds nothing on top of it.

So the arm is not miscalibrated-but-real. There is no transform of `fair` that
rescues it, because a perfectly calibrated version of our fair would still rank
worse than the price it is betting against. Recalibration is the wrong project.

### What this does and does not rule out

It rules out: tuning knobs, recalibrating the digital, reweighting sigma --
anything that is a function of (spot, strike, tau, sigma), because that is the
entire input to `fair` and the venue prices it better.

It does NOT rule out: a model with DIFFERENT INPUTS. The digital knows only
where spot is relative to the strike and how fast it moves. `btc5m_ml.parquet`
carries ~70 engineered features per window -- order-flow (cvd, taker_buy_ratio,
vol_surge, n_trades), path shape (efficiency, path_over_net, crossings),
momentum at 9 horizons -- none of which enter `fair`. Whether any of them
predicts the outcome beyond what the book already knows is an open and testable
question, and it is the only remaining modelling path.

## STAGE 1: can any feature beat position-over-vol? No (2026-09-14)

`tools/model_feature_search.py`. 67,488 snapshots, 8,436 windows, 31 days, tau
60-210. 59 causal features; dropped 4 settlement leakers (final_cl,
close_end_tape, true_delta_bps, y_tape) and 7 absolute price levels (a tree can
use BTC's level to memorise which DAY a row came from). Expanding forward folds
by day, never random.

    fold   baseline  logistic     GBM   GBM-base
     0      0.8319    0.8345    0.8250   -0.0069
     1      0.8693    0.8744    0.8677   -0.0017
     2      0.8248    0.8277    0.8076   -0.0172
     3      0.8552    0.8561    0.8476   -0.0076
     4      0.8635    0.8653    0.8620   -0.0015

    paired by DAY (n=25 test days)
      logistic  0.8510 vs 0.8479  diff +0.0031  t=+1.86  better on 16/25
      GBM       0.8430 vs 0.8479  diff -0.0049  t=-2.34  better on  8/25

**The GBM is significantly WORSE than the baseline and the logistic's +0.0031
is not significant.** Order flow (cvd, taker_buy_ratio, vol_surge), path shape
(efficiency, path_over_net, crossings) and momentum at nine horizons add
nothing to position-over-volatility. Even taken at face value, +0.003 AUC is
orders of magnitude short of the +0.09 edge needed to clear slippage.

The modelling path is closed with the inputs available. Reopening the model arm
needs an input this corpus does not contain -- not a better model over it.

### The near-miss, recorded because it would have shipped

The FIRST run of this scored against `z_tau` and produced logistic 0.8510 vs
0.7799, **t=+5.53, better on 22/25 days** -- a decisive-looking win. It was an
artefact. `z_tau` normalises the CHAINLINK delta, and in this corpus strike_cl
sits a median -4.0 bps from the reconstructed tape strike, so any model wins by
rediscovering `delta_bps` (permutation importance 0.276, against <=0.011 for
every other feature). Against a baseline that already contains it, the entire
gain disappears and reverses.

A day-clustered t of +5.53 on 31 days was not enough to make it true.

## CORRECTION: the "4 bps spot-vs-Chainlink basis" is a CORPUS artefact (2026-09-14)

FINDINGS has stated since 2026-09-13 that "spot-vs-Chainlink basis is median
4.01 bps, mean 5.73 -- ~8x the old 0.5 bps depth gate", and that measurement
was used to argue a sub-bps depth gate measures the strike rather than the
strategy. It is a property of `btc5m_ml.parquet`, not of the venue.

    LIVE recorded book, 362,924 ticks / 2,097 rounds, venue strike vs our spot
      all ticks     median +0.0000   mean +0.1033
      per round     median +0.000    mean +0.173
    CORPUS (strike_cl vs reconstructed strike_tape)
                    median -4.012    mean -5.720

The live number is zero-centred, and it agrees with this document's OWN earlier
direct observation -- "the oracle tracks Binance spot to +-$0.005 ... 18 of 20
matched to half a cent", which is 0.0006 bps, not 4. The corpus figure is a
timestamp misalignment between its tape reconstruction and the Chainlink strike.

The cross backtests are NOT affected: they use `strike_cl`, the real strike.
What IS affected is the argument that a tight depth gate is unmeasurable. Live,
spot tracks the strike to ~0, so sub-bps depth IS measurable, and
`cross_max_depth_bps = 5.0` -- widened from 0.5 partly on this basis figure --
has never been re-audited on the recorded book where the strike is real. That
is the highest-value open item.

## Debugging "too few trades, losses eat the profit" (2026-09-14)

Four knobs tested against the operator's report. Three are already right; the
fourth is a no-op; the real answer is sizing.

### 1. Depth gate: 5.0 is RIGHT, and tightening it hurts

Flagged earlier the same day as the highest-value open item, on the grounds
that the 4 bps basis justifying the 0.5 -> 5.0 widening turned out to be a
corpus artefact. Swept, the conclusion goes the OTHER way:

    depth   n(book)  win    additive g/t/drop     | n(corpus 31d)  win
     0.25        48  0.854  +0.630/+1.73/+0.254   |          208  0.659
     0.5         93  0.871  +1.183/+2.71/+0.676   |          386  0.663
     1.0        146  0.870  +1.526/+3.02/+0.922   |          603  0.683
     2.0        183  0.874  +1.572/+2.87/+0.937   |          760  0.695
     5.0        201  0.881  +1.917/+2.27/+0.964   |          839  0.714
     8.0        202  0.881  +1.918/+2.27/+0.965   |          844  0.716

Corpus win rate RISES monotonically with depth and recorded growth rises with
it. The artefact correction does not imply a tighter gate. Open item closed.

### 2. Range gate: correct, but its cost is volatility-dependent

    day          gate10  gateOFF  blocked   median round range
    2026-09-12       36       36        0       3.2 bps
    2026-09-13       34       39        5       4.5 bps
    2026-09-11       39       77       38      10.8 bps
    2026-09-14        8       12        4      11.8 bps

Nothing blocked on calm days, a third to a half on volatile ones. The blocked
trades win **0.628** against a breakeven of ~0.69 -- they are losers, so the
gate is right. **This is the whole explanation for a quiet day**: 09-14 had a
median round range of 11.8 bps.

### 3. Price band: raising the floor helps on paper, and is a no-op live

    band          RECORDED n/win/add/rev            CORPUS n/win/add/rev
    [0.10,0.90]   186 0.876 +1.655/+3.00  +1.939    120 0.700 -0.086  -0.132
    [0.45,0.90]   174 0.897 +1.189/+4.30  +1.883     86 0.826 +0.081  +0.443
    [0.50,0.90]   169 0.899 +1.093/+3.70  +1.818     80 0.850 +0.127  +0.512

Both substrates prefer a floor near 0.45-0.50, and on the priced corpus it
flips the sign under BOTH slippage models. But **20 of 20 live fills landed at
0.54 or above** -- the sub-0.45 entries are the ones that never fill. Changing
the floor would stop us ATTEMPTING them and change nothing we actually get.
Not shipped, because it buys nothing real.

### 4. Limit buffer: flat, no gain available

`tools/cross_buffer_sweep.py`. The first version was meaningless -- it walked
the ask forward from the trigger instant, where `ask <= ask + buf` always, and
reported a 100% fill rate at every buffer. With the measured 2.7-5.2s order
latency modelled:

    buffer        fills  fill%   win   mean px   growth/t/drop   (latency 3s)
    flat 0.02       104   64.2  0.827    0.656   +1.581/+2.44/+1.012
    flat 0.04       114   70.4  0.842    0.667   +1.770/+2.86/+1.201
    flat 0.06       119   73.5  0.849    0.679   +1.766/+2.98/+1.219
    flat 0.10       132   81.5  0.856    0.699   +1.748/+2.94/+1.181
    flat 0.15       144   88.9  0.868    0.722   +1.720/+2.90/+1.153
    SHIPPED         132   81.5  0.856    0.698   +1.748/+2.94/+1.181

Growth spans +1.581 to +1.770 across a 7x range of buffer, and the ranking
flips with the latency assumption (0.06 wins at 2s, 0.04 at 3s, 0.15 at 4s). A
smaller buffer buys a better price and loses fills, and the two cancel. Nothing
to win here.

### 5. The actual answer: we are at 0.22x Kelly

    full Kelly at win 0.714 / ask 0.629 = 22.9% of bankroll
    shipped stake 0.0224 x 2.25 = 5.04%  =  0.22 x Kelly

Bootstrapping the 16 REAL cross returns, 8 trades/day, 30 days from $52.55:

    mult   stake    median    p10     p90   median worst day
    1.50   3.36%      254     167     386      -7.0%
    2.25   5.04%      535     282   1,006     -10.5%   <- SHIPPED
    3.00   6.72%    1,087     466   1,813     -13.5%
    4.00   8.96%    1,621     866   2,346     -16.0%
    5.00  11.20%    1,921   1,168   2,651     -17.8%

Growth scales nearly linearly with stake over this range. "The trades do not
pay off enough" is therefore accurate and has nothing to do with the gates:
the per-trade edge is being harvested at a fifth of the size the edge supports.

**Not shipped, and the reason matters.** The bootstrap resamples 16 trades whose
realised win rate is 13/16 = 0.8125; the corpus says 0.714 and the corpus is the
number that has been predicting. At 0.714 the P(loss) column would not be 0.000.
Raising size multiplies whatever the true edge is -- including a thinner one.
Sizing is a risk decision for the operator, not a measurement, and the honest
input to it is the table above plus the fact that 16 trades cannot tell 0.71
from 0.81.

## The limit rule was backwards, and is now inverse in the ask (2026-09-14)

Nine live attempts have been cancelled unfilled and EVERY ONE was cheap
(decided ask 0.14-0.57, median 0.36); no attempt at a decided ask of 0.60+ has
ever missed. The rule was

    buf = max(0.01, min(cross_limit_buffer 0.10, cross_limit_buffer_pct 0.20 * a))

which shrinks the buffer as the ask falls -- 0.10 at a>=0.50, 0.064 at 0.32,
0.028 at 0.14. That is backwards with respect to the mechanism. A cheap ask
means the venue has NOT yet repriced a fresh cross, so it is precisely where
the book is about to run furthest. The rule bid least where it had to bid most.

Replaced by `cross_limit_buffer_inv`:  buf = min(cap, 0.25 * (1 - ask))

    ask     OLD limit     NEW limit
    0.14        0.17         0.35
    0.32        0.38         0.49
    0.45        0.54         0.59
    0.55        0.65         0.66
    0.70        0.80         0.77
    0.85        0.90         0.89

More room where the book runs, LESS where it does not.

### Chosen on robustness to limit-pinning, not on the point estimate

`tools/cross_limit_rules.py` sweeps limit RULES (flat / proportional / inverse /
bid-to-target / side-conditional) on the recorded book with the measured 3s
order latency. The decisive axis is P(the venue pins the limit rather than
filling at the book) -- measured ~1/3 across the live fills. Growth vs the old
rule:

    rule              P=0.0    P=0.33   P=0.66    P=1.0
    inverse .25(1-a)  +0.013   +0.070   +0.118   +0.171
    inverse .35(1-a)  +0.086   +0.067   +0.028   +0.014
    inverse .50(1-a)  +0.136   +0.033   -0.090   -0.227

0.25 is the only coefficient positive across the WHOLE range. 0.50 looks best
if the venue never pins and collapses if it always does, and P rests on few
fills. At the measured P~1/3 all three are within 0.004 of each other, so
robustness decides rather than the peak.

### Counterfactual on the real misses

Five of the nine have a known forward ask path:

    time      dec   ask ran to   new limit   fills   result    P/L
    00:04:15  0.45        0.89        0.59    yes      WIN   +0.681
    01:44:00  0.39        0.62        0.54    yes     LOSS   -1.000
    03:14:11  0.14        0.47        0.35    yes      WIN   +1.820
    08:44:00  0.33        0.96        0.50    yes      WIN   +0.980
    12:14:01  0.37        0.64        0.53    yes     LOSS   -1.000
    ------------------------------------------------------------------
    5 of 5 fill, net +1.481/$1 (mean +0.296)   against 0.000 today

For contrast, paying the FULL real ask on these was -0.73/$1: the gain comes
from capping the chase, not from chasing without limit. inverse .35 also fills
all five but at worse prices (+0.114).

### CAVEAT: the recorded book is the wrong population for this question

    decided ask      1s replay    LIVE
    < 0.45               6.8%     28.6%
    median              0.715     0.500

The replay polls at 1s and catches crossings AFTER the venue has repriced; live
runs the websocket at ~114/s and catches them fresh and cheap. So the sweep is
weighted 93% toward triggers that never miss anyway, and it UNDERSTATES this
change -- live is 4.2x richer in exactly the case the rule targets.

This caveat applies to EVERY recorded-book conclusion in this session (the
range-gate re-audit, the depth sweep, the band sweep, the sizing sweep). None
of them are wrong, but all are measured on a more expensive trigger population
than the arm actually trades.

REVERT trigger: if realised fills start landing AT the limit more than ~half
the time, P is heading to 1 and the advantage shrinks toward +0.171 with any
larger coefficient going negative.

## Where the extra trades are: the other five markets (2026-09-14)

The operator's bottleneck is trade count. Taking the levers in order of cost:

### Nothing left on BTC alone

    cap/round    3      5      8     12     99
    triggers   197    201    201    201    201

`cross_max_per_round` DOES NOT BIND -- 5, 8, 12 and unlimited all produce the
same 201 triggers. The reversal guard, the depth gate and the range gate are
what limit entries per round, not the cap. Raising it buys literally nothing.
The tau window cannot widen (5-270 turns +0.226/+0.042 into -1.288/-1.204), and
depth is already at its flat optimum. BTC-5m is fully harvested at ~26
triggers/day.

### The volume is in the families we record and do not trade

`tools/cross_market_scan.py` -- the venue's OWN strike and OWN winner for every
recorded family, which is the substrate the 2026-09-13 retraction said did not
exist. Bootstrap over ROUNDS:

    market            trig  rounds  days  trig/day    win   95% CI        ask rows
    btc-updown-5m      181     132     7      25.9  0.884  [0.821,0.943]   330,763
    bnb-updown-5m       83      52     2      41.5  0.723  [0.591,0.845]         0
    eth-updown-5m       92      76     5      18.4  0.783  [0.681,0.876]    32,857
    btc-updown-15m      21      12     2      10.5  0.810  [0.545,1.000]         2
    eth-updown-15m      16      11     2       8.0  0.688  [0.357,0.941]         2
    bnb-updown-15m       0       -     3         -      -                        2

**eth-5m + bnb-5m would take the arm from 25.9 to 85.8 triggers/day -- 3.3x.**
Every 5m family's CI excludes 0.5.

### Three reasons this is not yet a trade

1. **No prices.** Four of six families have effectively zero recorded ask, so
   EV is unmeasurable. A win rate alone cannot clear a market whose break-even
   at live fills is ~0.665-0.69.

2. **The substrate overstates.** BTC reads 0.884 here against 0.714 on the
   31-day Chainlink corpus -- a 0.17 haircut, because a 1s replay selects late,
   already-repriced triggers. Apply the same haircut and eth 0.783 -> ~0.61 and
   bnb 0.723 -> ~0.55, BOTH BELOW break-even. That is speculative, but it is the
   reason a good win rate here is not a green light.

3. **Subscribing their book in-process costs the BTC arm.** Measured: following
   all six took the poll from a 1.00s median to 4.15s, and a slower poll widens
   the window where a real crossing is invisible (btc-updown-5m 1789336500 --
   spot crossed for ~1.5s and the arm never saw it). The book feed ingests ~6x
   the message volume IN THIS PROCESS.

### The unblock

Point 3 is the only one that is an engineering problem rather than a
measurement, and it has a clean fix: a SEPARATE recorder process subscribing to
the non-traded families' books and writing quotes to the same store. The cost
that was measured is contention inside the trading process; a second process
does not share its event loop, so the trading poll is untouched.

Three to five days of that turns points 1 and 2 into a normal EV question
answerable on real asks -- the same bar btc-5m had to clear. Until then, more
markets is a hypothesis with a good win rate and no price.

## Shipping the cross arm across six families (2026-09-14)

Widened at the operator's explicit instruction, with the risk stated and
accepted. Recorded here is what the audit found, because three of the five bugs
below fail SILENTLY and would not have shown up as errors.

### The exposure cap, chosen from measurement not reasoning

An earlier draft of this added a cap from first principles and was rightly
rejected. `tools/cross_multi_exposure.py` replays the shipped gate across all
six families on the 7 recorded days and walks a clock over the open positions:

    concurrency   peak 9   median 1   p90 4   p99 7
    at 6.72%/trade -> p90 26.9%, p99 47.0%, peak 60.5% of bankroll at risk
    same-side share when several are open: 0.945
    worst single expiry: 7 simultaneous LOSSES = 47.0% of bankroll

**0.945 is the number that decides it.** These are not six diversified markets:
BTC, ETH and BNB cross their strikes on the same move, and btc-updown-5m and
btc-updown-15m are the SAME ASSET on two clocks. It is one directional bet held
up to nine times.

And the 20% daily loss cap cannot cover it -- those positions expire at the same
instant, so the cap is evaluated after the 47% has already happened.
`cross_max_open_stake_pct = 0.30` never binds at p90 and cuts the p99 case from
47% to ~27%.

Two of my own errors in producing that table, both caught before it was used:
the tool first read config.py's `stake_fraction` default (0.04) instead of the
deployed 0.0224, overstating every figure by 1.8x; and a shadowed loop variable
printed "700% of bankroll".

### Bugs the audit found, all introduced by the widening

1. **`check_reserved` was handed `cross_max_per_round`.** `reserved_open` counts
   open positions ACROSS families, so with six markets a per-round knob silently
   became "5 open positions in total" -- and raising it to admit more entries in
   a choppy round would also have raised account-wide concurrency. Split into
   `cross_max_open_positions` (10, just above the measured peak of 9).

2. **`_cross_refused` held ONE slug.** Its dedupe reset whenever the slug
   changed, which is correct for one family and defeats it entirely for six:
   each market's refusal erases the previous market's memory, so on the
   websocket clock (~114/s) every refusal logs and inserts a row on every
   evaluation. Re-keyed by slug, pruned to live rounds.

3. **`_cross_fired` was pruned to the current round's slug.** Same shape of bug:
   each family's tick wiped the others' fired counts, so the per-round cap would
   never have bound anywhere.

4. **`self._asks` is the PRIMARY's book.** The non-websocket fallback path would
   have priced an eth/bnb order off BTC's ask. Off-primary now refuses outright
   rather than falling back -- only the primary fetches the REST book, and only
   its round is refetched every poll, so off-primary the displayed price can be
   a whole round old.

5. **`sigma_now` and `spot_ts_ms` read primary state.** The first sized the
   moneyness tilt for every family off BTC's volatility; the second stamped
   every recorded eth/bnb quote row with BTC's timestamp.

### The cadence problem, and why it is no longer the blocker

Following all six books once took the poll from a 1.00s median to 4.15s, and a
slow poll is what makes short crossings invisible. The other half of that cost
was `rounds_by_prefixes` paging a volume-sorted list every poll to find a quiet
family (bnb-updown-5m at offset 1450 -- 30 pages, 3.95s).

That half is now gone: a round's metadata is fixed once it opens, so a family is
re-swept only when its round has ROLLED. Verified end to end -- the second tick
sweeps only `btc-updown-5m`, and all six again after a roll. What remains is the
websocket message volume, which is reported every 120 ticks as a "poll cadence"
line and can be turned off with `cross_ws_follow_all` without a deploy.

### Per-market knobs: one needed, the rest did not

Only `max_round_range_bps`, and for an arithmetic reason -- the gate is in BPS
and a 15m round has three times as long to travel, so BTC's 10.0 blocks 25% of
bnb-5m's rounds and 95% of bnb-15m's. Each family gets the gate admitting the
same share of its OWN rounds that 10.0 admits of BTC-5m's (59.5%).

Nothing else is overridden, and that is also a measurement: |depth| at the
trigger is 0.48-0.93 bps median on EVERY family, so the 5.0 depth gate binds on
0-3% of triggers everywhere. tau and look are in seconds and the mechanism they
encode is time-based. The price band and stake stay at BTC's because no other
family has a recorded ask to set them from.

## "Only enter on a SIGNIFICANT cross": tested both readings, ship neither (2026-09-14)

Prompted by btc-updown-5m-1789403700: spot dipped 0.51 bps below the strike at
tau 58, the arm bought Down at a filled 0.5789, and the round settled 1.37 bps
ABOVE. A marginal cross that immediately reversed. The arm has a depth CEILING
but no floor, so a 0.01 bps nick counts the same as a decisive move.

### The observation is correct

Win rate DOES rise with depth. Round-clustered bootstrap over every recorded
family (426 triggers, 7 days):

    cut 0.25:  shallow 0.737  deep 0.811  diff +0.074  CI [-0.028,+0.180]  n.s.
    cut 0.50:  shallow 0.753  deep 0.825  diff +0.072  CI [-0.014,+0.162]  n.s.
    cut 1.00:  shallow 0.759  deep 0.874  diff +0.115  CI [+0.022,+0.200]  REAL

Waiting for the cross to HOLD does the same: win 0.883 at no delay, 0.918 after
8 seconds.

### And it is already in the price

    BTC-5m, real asks
    depth band     n    win   mean ask   EDGE (win - ask)
    0.00-0.25     33  0.909      0.617      +0.292
    0.25-0.50     38  0.895      0.646      +0.249
    0.50-1.00     49  0.837      0.691      +0.146
    1.00-2.00     30  0.900      0.790      +0.110
    2.00-5.01     12  0.917      0.787      +0.130

    correlation(depth, ask) = +0.367

**The edge is LARGEST at the shallowest depths and roughly halves by 1 bps.**
The extra win rate is fully paid for, and then some. Same for waiting: the ask
goes 0.691 -> 0.763 between a 0s and an 8s confirmation.

That is the arm's own premise restated. Its edge is the nick the venue has not
repriced yet, so a "significance" filter removes precisely the part that is
mispriced and keeps the part the venue already understands.

### Growth, which is what decides it

    depth FLOOR        n    win    additive g/t/drop     reverting g/t/drop
    none (shipped)   162  0.883   +1.943/+2.40/+1.095   +2.282/+3.20/+1.483
    0.25             129  0.876   +1.050/+3.06/+0.694   +1.577/+4.07/+1.108
    0.60              71  0.887   +0.423/+1.32/+0.128   +0.829/+2.53/+0.499
    1.00              42  0.905   +0.087/+0.55/+0.014   +0.461/+2.19/+0.275

    CONFIRMATION       n    win    additive              reverting
    0s (shipped)     162  0.883   +1.943/+2.40/+1.095   +2.282/+3.20/+1.483
    2s               137  0.876   +0.741/+1.99/+0.480   +1.563/+3.75/+1.069
    8s               122  0.918   +0.615/+1.65/+0.291   +1.591/+3.94/+1.172

    SIZE by depth      n  mean w   additive              reverting
    flat (shipped)   162   1.000  +1.943/+2.40/+1.095   +2.282/+3.20/+1.483
    x0.5 under 0.5   162   0.781  +1.217/+2.45/+0.719   +1.622/+3.17/+1.061
    linear depth/1.0 162   0.595  +0.830/+2.29/+0.507   +1.222/+3.07/+0.811

Every variant loses growth under BOTH slippage models, monotonically in the
strength of the filter. Sizing by depth loses too -- flat is the best rule
tested. NOTHING SHIPPED.

Worth noting what a floor DOES buy: day-t rises (additive 2.40 -> 3.06 at a
0.25 floor, reverting 3.20 -> 4.07). It is a more consistent, smaller edge.
Growth is what compounds, so that is not a trade worth making here -- but it is
the honest reason the idea felt right.

### Footnote on the trade that prompted this

    decided 0.460, filled 0.5789
      OLD buffer limit 0.55 -> would have MISSED
      NEW buffer limit 0.60 -> filled

The inverse-buffer change shipped hours earlier is what bought this trade. That
is the rule working as designed (it exists to fill cheap entries that used to
rest and cancel), and the first one it caught lost. One sample: it is expected
to fill several more, and they will not all win.

## The six-market widening is INERT: the book feed delivers only one topic (2026-09-14)

Deployed, verified, and then found not to work for five of the six families.
Recording it in full because the venue lies about it.

### Symptom

Since the multi-market restart, only the primary carries an executable price:

    market              rows   with ask
    btc-updown-5m        327        303   (93%)
    eth-updown-5m        327          0
    bnb-updown-5m        327          0
    btc-updown-15m       327          0
    eth-updown-15m       327          0
    bnb-updown-15m       327          0

Not a subscription failure in the log -- the feed reports
`prediction book ws connected: [6 ids]` and the venue answers
`{"type":"COMMAND","data":"SUCCESS","subType":"REGISTER","code":"00000000"}`.

### It is not liquidity, and not topic order

* the 15m families have REAL REST books right now (btc-15m 0.630/0.640,
  eth-15m 0.400/0.410, bnb-15m 0.290/0.360), so there is a book to push.
* the delivering id is always a btc-updown-5m one but its POSITION varies --
  11074289 at index [0] and 11075128 at index [6] both delivered -- so it is
  not "the first topic wins".

### Cause, measured

`follow()` joins per-market topics with "|" into ONE connection:

    topic = "|".join(TOPIC_FMT.format(m) for m in mids)

Probed read-only with a SINGLE topic for eth-updown-5m: **94 frames in 30
seconds**. The same market receives NOTHING when it is one of six pipe-joined
topics on one connection.

**The venue accepts a multi-topic subscription, answers SUCCESS, and then
delivers only one of them.** This is the same trap already recorded at the top
of prediction_book.py for the AGGREGATED topic
(`web3_prediction_orderbook_data` "registers with SUCCESS and then pushes
NOTHING") -- it turns out to apply to pipe-joined per-market topics too, which
is why MAX_TOPICS and the additive `follow()` were built on an assumption that
was never true.

### What this means right now

The bot is SAFE but effectively BTC-5m-only. The off-primary guard added with
the widening -- refuse when the websocket book is not fresh, rather than fall
back to a displayed price that may be a round old -- is doing exactly its job:

    cross bnb-updown-5m-1789404600 Up not taken: no fresh websocket book off-primary

So the five new families take no trades rather than bad ones. Everything else
shipped today (the 30% exposure cap, per-market range gates, the round cache,
the inverse limit buffer, the 3.0x stake) is live and working; the poll cadence
is median 133ms / p90 148ms with all six subscribed, which settles the old
"4.15s" fear -- that was the deep page sweep, now cached, not websocket volume.

### The fix, not yet made

One websocket CONNECTION PER MARKET instead of one connection with a joined
topic. Unknown until tried: whether the venue caps concurrent connections per
API key. That is the next piece of work, and until it lands the widening buys
nothing.

Also found while reading the log: `follow()` never drops expired rounds, so the
topic set grows every roll (6 -> 7 -> 12 within five minutes) until MAX_TOPICS
trims oldest-first. Harmless while only one topic delivers, but it must be
fixed alongside -- with per-market connections a dead round should close its
connection rather than hold a slot.

## FIXED: one websocket connection per market (2026-09-14)

The widening was inert because the venue accepts pipe-joined per-market topics
on one socket, answers COMMAND/REGISTER SUCCESS, and delivers exactly one.

Verified before changing anything -- 5 CONCURRENT single-topic connections on
the same API key, 40 seconds:

    btc-updown-5m    201 frames      eth-updown-5m     63
    btc-updown-15m   173             bnb-updown-15m    53
    eth-updown-15m   165
    -> 5 of 5 delivered

So `PredictionBook` now holds one connection per market. `run()` became a
supervisor that reconciles a task per followed id; `_run_one` owns a single
socket with its own backoff.

### `follow()` is authoritative again, and that is the point

It was purely additive, and that was NOT a preference: the topic was a
connect-time parameter of the single shared socket, so any change to the set
re-handshaked EVERY market (63 reconnects in three minutes with six followed).
A rolled round therefore could not release its slot -- the live set grew 6 -> 12
within five minutes and only MAX_TOPICS eviction bounded it.

Per-socket removes that constraint: adding or dropping a market touches only
its own connection. So `follow()` now declares the wanted set, with a 90s grace
period (`CLOSE_AFTER_ABSENT_S`) because a family mid-roll drops out of the poll
for a beat and tearing its socket down each time is the churn the additive
design existed to avoid. The requirement survived; the mechanism changed.

## FIXED: the deep sweep no longer stalls the poll (2026-09-14)

    poll cadence: median 133ms  p90 148ms  max 4673ms

The maxima landed on 5-minute boundaries, where all three 5m families go stale
at once and `rounds_by_prefixes` pages to offset 1450 to find bnb-updown-5m.

That stall is NOT harmless just because the new 5m round is at tau ~300 and out
of the trade window: the 15m families roll on their own schedule, so a 5m roll
can blind a 15m market sitting at tau 5-60 -- which is precisely the window the
whole arm trades in.

Now only the PRIMARY is swept inline (page 1, one request). Rolled families are
refreshed in a background task while the poll keeps serving from the round
cache. A crash in that task is surfaced via `task.result()` on the next pass
rather than swallowed.

## CORRECTION: what the range gate actually blocks (2026-09-14)

Stated twice today that gate-blocked triggers "win 0.628 against a break-even
of ~0.69", i.e. that they lose. **Wrong bar.** 0.69 is the mean ask of the
triggers the gate KEEPS; the blocked ones are cheaper, so their break-even is
lower:

    ALL blocked : n=78   win 0.628  ask 0.610  EDGE +0.018
    ALL kept    : n=162  win 0.883  ask 0.691  EDGE +0.192

Blocked triggers have a slightly POSITIVE edge, not a negative one.

The gate is still correct, for a different reason: +0.018 cannot survive the
measured +0.088 slippage, while +0.192 clears it with room. Same arithmetic
that killed the model arm -- edge smaller than slippage -- not "these trades
lose".

On the volatile half of days, which is the regime that provokes the question:

    blocked, HIGH-volatility days: n=69  win 0.638  ask 0.597  EDGE +0.041

Better than the average blocked trigger, still under half of what execution
costs.

Per day it is very noisy (+0.316 on 09-10, -0.295 on 09-09, on 6-38 triggers),
which is why a run of refused winners -- two on 2026-09-14 17:09, both Down on
rounds that settled Down -- says nothing about the gate.

This is the third time in this file that comparing a win rate to the wrong mean
ask produced a wrong conclusion. The bar is ALWAYS the mean ask OF THE SAME
SUBSET, and the test is whether `win - ask` clears ~0.09, not whether it clears
zero.

## Should the range gate be dynamic? The premise is right, the fix is not (2026-09-14)

Asked because a fixed bps gate has drifting SELECTIVITY -- it was set to admit
59.5% of rounds at the volatility of the week it was measured on, and during a
2x-volatility stretch it was admitting 16%. The arm took one trade in 95
minutes across six families.

The premise is correct and measurable. Share of triggers the shipped static
gate keeps, per day:

    2026-09-12 (median range 2.5 bps)  100% kept
    2026-09-08 (median range 11.8)      45% kept
    spread across 7 days:               55 points

A trailing-median gate (`refuse if range > k * median of the last 40 CLOSED
rounds of this market`, strictly causal) does stabilise that: 36-point spread
at k=1.2, 26 at k=2.0.

### But it does not pay

Paired by day against the static gate, BTC-5m, 240 triggers, 7 days:

    k      n   kept   additive diff / t     reverting diff / t    sel. spread
    1.0  141    59%   -0.449 / t-1.59       -0.593 / t-2.27            42p
    1.1  155    65%   -0.187 / t-0.61       -0.303 / t-1.16            40p
    1.2  171    71%   +0.020 / t+0.05       -0.009 / t-0.02            36p
    1.3  175    73%   -0.018 / t-0.04       -0.018 / t-0.05            36p
    1.4  184    77%   -0.299 / t-0.74       -0.272 / t-0.78            33p
    2.0  213    89%   -0.565 / t-0.81       -0.471 / t-0.72            26p

**No k beats static.** k=1.2/1.3 is a narrow break-even flanked by losses on
both sides -- the signature of a fitted point, not an effect. A sigma-normalised
variant (`range > k * sigma_1m * sqrt(elapsed/60)`) behaves the same way.

Note what this means for the single-rule table that provoked the idea: scored
alone, trailing x1.2 looked clearly better than static (day-t 3.72 vs 2.40
additive, 4.94 vs 3.20 reverting, on more trades). The PAIRED comparison says
the two rules earn the same thing on the same days. A higher t on its own
returns is not evidence that a rule beats another rule.

### Why the static gate is right, which is not obvious

"Calm rounds only" appears to mean calm in ABSOLUTE terms, not calm relative to
what has been normal lately. A gate that widens during a storm trades the
storm, and the storm is where this arm's edge is not. That is why stabilising
selectivity -- which is the mechanically correct thing to do -- does not
improve returns: the thing worth conditioning on was never selectivity.

### The live consequence, stated plainly

In today's regime the dynamic gate would have traded the refusals (trailing
median ~14.4 puts k=1.2 at 17.3 bps against triggers of 10.9). It would have
traded MORE and earned the same or less. So the arm trading very little in a
volatile stretch is the design working, not a fault to be tuned away.

NOTHING SHIPPED. Limits: 7 days, BTC-5m only. A dynamic gate's advantage should
show up across REGIME CHANGES, and seven days may not hold enough of them --
that argues for revisiting with more data, not for shipping on this.

## THE frequency bug: a resting order blinded the whole bot (2026-09-14)

The operator's complaint was trade frequency, and I spent several rounds
testing gates -- the depth floor, the confirmation delay, depth sizing, a
dynamic range gate -- all of which came back "ship nothing". The gates were
never the problem.

`buy_limit` AWAITS the fill for up to `limit_wait_s` (20s). `_cross_all` walks
the six families sequentially inside the poll loop. So the entire decision loop
stopped while any one order rested. Measured against the recorded tape -- every
unfilled attempt produced a quote gap the length of its own wait:

    order waited 21.2s  ->  quote gap 21.4s
    order waited 21.2s  ->  quote gap 25.1s
    order waited 21.5s  ->  quote gap 26.0s
    order waited 12.4s  ->  quote gap 16.3s
    order waited 21.1s  ->  quote gap 21.7s
    order waited  7.6s  ->  quote gap 11.5s

A quote gap means nothing was recorded, nothing was polled, and no crossing on
ANY family could be detected for that long.

Three things make it worse than the raw seconds suggest:

  * it blocks ALL SIX families, not the one ordering. A resting bnb order
    blinds btc.
  * it starts the instant a trigger fires -- when the market is moving and
    further crossings are most likely.
  * a FILLED order blocks ~3.7s, a MISSED one 21s, so the 35% of attempts that
    miss burn six times the blind time of the ones that work.

### The fix

Placement moved to a background task (`_cross_execute`); the poll loop no
longer waits. The slot was already claimed before ordering (`_cross_fired`), so
the await was pure dead time.

What the split must not lose is the cap arithmetic. `reserved_open` and the
ledger's open-stake query both only count positions that have BOOKED, so with
several orders outstanding two triggers could each pass a 30% exposure check
and land at 60% together. Stake and slot are therefore RESERVED at claim
(`_pending_stake`, `_pending_n`) and released in the task's `finally`.

A test caught a real bug in the first version of that: `_open_cross_stake`
returns a 1-second cache, and the cached path returned the bare ledger figure
without adding in-flight -- defeating the reservation in exactly the case it
exists for, two triggers inside the same second.

Also recorded: these stalls put holes in the recorded tape, so every replay in
this file that counts triggers from `quotes` has been UNDERCOUNTING them.

## CORRECTION: `rev_prev == 0` is not the shipped reversal guard (2026-09-14)

Caught in review. `cross_reversal_recorded.replay` marks a reversal against the
IMMEDIATELY PRECEDING entry:

    int(prev_side is not None and side != prev_side)

so a round going Up, Down, Down marks the first Down and NOT the second -- it
matches the entry before it. Filtering `rev_prev == 0` therefore keeps `Up` AND
that second `Down`: a mixed-side round, and an entry the live guard would never
have taken. The shipped guard compares against the last side ACTUALLY TAKEN, so
a skipped entry never becomes the reference.

    trade set               entries  rounds  mixed-side  max/round
    raw (guard off)             215     150          11          5
    rev_prev==0 (what I used)   201     150           5          3
    live guard (correct)        199     150           0          4

The correct set has ZERO mixed-side rounds -- a round really is one bet -- and
contains a 4-ENTRY round that the filter hid.

### What it changes

    trade set                 win   additive g/t        reverting g/t
    rev_prev==0 (mine)      0.881   +2.494/t+2.27       +2.736/t+3.52
    live guard (correct)    0.874   +2.319/t+1.98       +2.567/t+2.99

Re-scored on the correct set, every conclusion SURVIVES but the confidence was
overstated:

    range gate   OFF +1.705/t1.07 | gate 10 +2.319/t1.98 | gate 12 +2.145/t2.03
                 -> still SUPPORTED, but the headline "day-t 2.27-4.08" is
                    wrong; it is 1.98-2.99.
    depth        5.0 +2.319 vs 1.0 +1.837 -> 5.0 still wins on growth (though
                 tighter depth has the higher t, 2.58 vs 1.98)
    price band   [0.10,0.90] +1.988 vs [0.45,0.90] +1.389 -> shipped still wins
                 on growth; the floor still buys t (3.45 vs 2.46) and not growth
    stake        growth still monotone in size; t flat at ~1.98 throughout

Tools that already applied the guard correctly and are UNAFFECTED:
cross_buffer_sweep, cross_limit_rules, cross_market_scan, cross_depth_floor,
cross_dynamic_gate, cross_lock_hedge, cross_multi_exposure.

## The ROUND is the risk unit, not the order (2026-09-14)

`cross_max_open_stake_pct` treats the account as 0.945-correlated positions.
Entries in the SAME round are not that: with the reversal guard on they are the
same side of the same round and settle on ONE oracle print. Correlation 1.000 --
a second entry is an add, not a second trade.

On the corrected set: 105 rounds with 1 entry, 42 with 2, 2 with 3, and ONE
WITH 4 -- 26.9% of bankroll on a single print, which is 1.17x full Kelly (22.9%)
against a per-entry posture of 0.29x. The 3 -> 5 raise of cross_max_per_round
was justified on per-entry EV, correctly; what it also did, silently, was move
worst-round exposure past full Kelly.

    cap    binds   max/print   additive g/t      reverting g/t
    none       0       26.9%   +2.319/t+1.98     +2.567/t+2.99
    3.0x       1       20.2%   +2.301/t+1.99     +2.543/t+3.02
    2.0x       3       13.4%   +2.360/t+2.11     +2.582/t+3.26
    1.0x      45        6.7%   +2.309/t+2.25     +2.205/t+3.51

SHIPPED 2.0x: bounds one print at 13.4% = 0.59x Kelly, the same posture the
per-entry stake takes, binding on 3 of 150 rounds. Its apparent +2% growth gain
is NOT claimed -- 3 rounds is the same n that would reverse it. 3.0x was
rejected for being invisible: it binds once and blesses 0.88x Kelly on a single
print as standing policy, which makes the decision unrecorded rather than made.

## A locked-loss guard for EVERY arm, and why it does not rescue the model arm

Of 720 settled rounds, 25 hold both sides -- 21 of them the model arm colliding
with ITSELF, seconds apart:

    09-12 04:40  pair 1.135 -> -0.49
    09-12 06:11  pair 1.212 -> -0.43
    09-12 06:15  pair 1.192 -> -0.43

Mixed-side rounds returned -4.0% on $61.12 against +5.6% on $1,705 elsewhere.
The cross arm got `cross_allow_reversal` after the 12:05 round; the model arm
never had one, because "arms are independent" meant nothing inspected the
combined book. `max_paired_price = 1.0` now refuses any order that would hold
both sides above break-even, for every arm. 1.0 is arithmetic, not a tuned
number.

**It does not rescue the model arm, and it is not why that arm is off.**

    as-is                total +47.27  drop-best -1.31  day-t +0.68
    collisions removed   total +49.36  drop-best +0.78  day-t +0.71

drop-best flips sign, which looks like a rescue and is not: 98.4% of the fixed
total is still a single day (09-09, +48.58 of +49.36), day-t moves 0.68 -> 0.71,
and the arm's own edge is untouched -- lifetime win 0.64971 against a mean ask
of 0.64967, its `fair - quote` ranking outcomes at AUC 0.4906. The guard removes
a landmine before that arm is ever switched back on. The arm stays OFF.

## REGRESSION: non-blocking placement removed an accidental rate limit (2026-09-14)

Moving order placement off the poll loop (correct, and it fixed a 21s blindness
bug) had a consequence I did not anticipate: it removed a rate limit the arm
had been relying on without anyone knowing.

Live, minutes after the deploy:

    19:34:00,179  CROSS bnb Up $3.54 -> 23 today
    19:34:00,240  CROSS bnb Up $3.54 -> 24 today     <- 61ms later
    19:49:01,614  CROSS eth Up $3.12 -> 27 today
    19:49:01,674  CROSS eth Up $3.12 -> 28 today     <- 60ms later

and one round took FOUR fills for $11.64 on a ~$45 bankroll -- 26% on a single
oracle print.

### Cause

`crossed_side` returns a side for as long as `look_s` of tick history still
straddles the strike, so ONE crossing is detectable for about a second. The arm
runs on the websocket clock at ~114 ticks/s, so it can see that one crossing
~114 times. While placement AWAITED the fill, the first entry blocked the loop
for 2-3s and the crossing had resolved by the time the arm looked again. That
await was the rate limit.

### Why the multi-entry measurement never showed this

`cross_max_per_round = 5` and "entries 0-4 all carry positive EV" were measured
on a substrate sampled once per SECOND. Gaps between consecutive entries in a
round, on that substrate:

    min 0.9s   p10 1.0s   MEDIAN 1.0s   p90 1.0s   max 41.0s

That median is the sampling interval, not a market property: 92% of its "second
entries" are the same crossing re-detected on the next sample. The substrate
could not contain a 0.06s pair, so the measurement never priced one.

### Fix

`cross_reentry_gap_s = 1.0` -- minimum gap between entries in one round,
stamped when the slot is claimed (not when the fill returns, since placement is
now off the poll loop and several duplicates could otherwise pass while the
first order was still in flight). It reproduces the substrate the EV was
measured on, and it is self-consistent with `cross_look_s`: after look_s the
same crossing stops being detectable anyway.

### What DID work, and is worth recording

The `cross_max_round_stake_mult = 2.0` cap shipped 25 minutes earlier behaved
exactly as designed. The 4-fill round PREDATES it; after it, bnb took 2 fills
for $6.30 against a $7.08 budget and eth 2 for $6.04 against $6.24. The cap
caught the symptom while the cause was still unknown -- which is the argument
for a priori bounds over fitted ones.

The in-flight exposure reservation also worked: a unit test isolating the
per-round cap failed because `_pending_stake` correctly blocked a second entry
at 24% + 24% against the 30% account cap.

### Cost

Three rounds traded under the regression: +6.73, -6.30, -6.04 = **-$5.61**.
Bot was stopped on discovery and restarted with the gap in place.

## A depth FLOOR in ticks, because bps is the wrong unit for one (2026-09-14)

Spotted live by the operator: eth entered at spot 2544.48 against a strike of
2544.47, and bnb at 726.05 against 726.04. One-cent "crossings".

`cross_max_depth_bps` is a ceiling with no floor, and bps cannot express a
floor correctly here. All three symbols quote in $0.01, but that cent is:

    BTC @79138   0.0013 bps        ETH @2541   0.0394 bps
    BNB @726     0.1377 bps

so the median trigger depth in TICKS is:

    btc-5m 193    eth-5m 13    bnb-5m 1    eth-15m 4    bnb-15m 2

**BNB's median cross is one tick** -- the last decimal flipping. The flagged
trade was not unusual; it was the median BNB trade.

Win rate by tick depth (7 recorded days, all families, re-entry gap applied):

    ticks    n    win
    0-1     34   0.647 +-0.161
    2-4     36   0.611 +-0.159
    5-15    57   0.825 +-0.099
    16-50   54   0.778 +-0.111
    51+    145   0.876 +-0.054

    bnb-5m alone:  <=1 tick 0.57 (n=14) | 2-4 0.56 (n=18) | 5-15 0.94 (n=18)

SHIPPED `cross_min_depth_ticks = 5`. BTC is untouched at any sane value, so the
unit does the per-symbol scaling that a bps floor could not, and no per-market
table is needed.

### Why the earlier depth-floor test missed this

A depth floor was tested and rejected the same morning. That test was in BPS
and BTC-ONLY, where the median trigger is 193 ticks and a floor only removes
real moves. Asking the same question on a symbol whose median trigger is one
tick is a different question, and the earlier answer did not transfer. The
error was answering a per-market question on the one market where it does not
arise.

## The model arm, on the population it actually trades (2026-09-14)

The shutdown case rested partly on AUC(edge) = 0.49 over all 133k evaluations,
which was challenged on two correct grounds: the statistic is MARGINAL where
the question is conditional, and it was computed on every tau snapshot rather
than the region the arm trades. Re-run on the gate region, bootstrapped over
ROUNDS (`tools/model_gate_region.py`):

    population                          n     AUC(edge)  95% CI          b_fair|quote  95% CI
    ALL evaluations (the old test)  133289    0.5057  [0.479,0.530]    -0.0365  [-0.373,+0.274]
    GATE REGION (edge>hurdle, band)  63331    0.4982  [0.469,0.526]    -0.7290  [-1.402,-0.066]
      ...and tau 60-210 (deployed)   33095    0.4974  [0.460,0.536]    -1.0229  [-2.018,+0.040]

On the right population the answer is not softer, it is harder. AUC(edge) is
0.498 -- a coin flip -- and the incremental slope of `fair` given `quote` is
**-0.729 with a CI that excludes zero**. The arm's disagreement with the venue
is not merely uninformative in the region it trades; it is anti-informative.

The edge quartiles inside the gate region say the same thing monotonically:

    quartile      n   mean edge    win   mean quote   margin
    q1        15833      0.0225  0.639        0.606   +0.0336
    q2        15833      0.0334  0.636        0.639   -0.0027
    q3        15832      0.0452  0.652        0.664   -0.0119
    q4        15833      0.0885  0.632        0.674   -0.0418

**More edge is worse.** Raising `min_edge` -- the obvious next move once the
slippage error was corrected -- would select harder for the losing quartile.

### What this does and does not overturn

Two pillars of the original shutdown case were WRONG and stay corrected: the
arm was scored with the cross arm's slippage (+0.088 against a measured
+0.0037, 23x), and drop-best-day was computed over fills outside its own tau
window (-10.31 vs +4.06 in-window). Both were real errors.

The prediction pillar survives, and this test makes it stronger rather than
weaker. The arm stays OFF -- now on the population it trades, with a
conditional statistic, and with the two bad pillars removed rather than leaned on.

## Structural arbitrage is impossible here: UP and DOWN are one mirrored book

Two strategies were put forward, both premised on UP and DOWN being separate
instruments that can drift apart:

1. **Split-and-sell** — deposit $1.00, mint 1 UP + 1 DOWN at zero fee, rest
   maker sells on both sides (UP at 0.52, DOWN at 0.51), collect 3% risk-free.
2. **Discount pair merging** — when the combined ask dips under $1.00
   (UP 0.62 + DOWN 0.35 = 0.97), take both sides and call MERGE for $1.00.

Neither primitive exists on this venue. `btcpredict/execution/live.py` defines
eleven endpoints — quote, place-order-bundle, order-book, market/list,
wallet/list, position/list, payment-options, batch-redeem, order/history,
order/list, batch-cancel — and none is split, merge or mint.

That is not the real answer, though, because **merge is only a way to collect
the dollar early**. Holding 1 UP + 1 DOWN to settlement pays exactly $1.00 for
free, since exactly one side wins. So strategy 2 reduces to a question the
corpus can answer: *does the combined ask ever fall below $1.00?*

### The measurement (`tools/pair_arb_probe.py`, 366,148 snapshots, 7 days)

It cannot, and the reason is structural rather than empirical:

    down_ask == 1 - up_bid     |diff| < 1e-4 in 98.9% of rows
    down_bid == 1 - up_ask     |diff| < 1e-4 in 98.9% of rows
    up spread == down spread   in 98.8% of rows, median 0.0100 both sides

UP and DOWN are **not two books. They are one book, mirrored.** Therefore

    up_ask + down_ask == 1 + spread      (combined ask, by construction)
    up_bid + down_bid == 1 - spread      (combined bid, by construction)

Over the 362,010 rows where the mirror holds, the combined ask has a **minimum
of exactly 1.0000** and a median of 1.0100. There is no discount pair. Not
rarely — never.

403 sub-dollar snapshots do appear in the raw data, and they are seductive
(median net +0.0138/pair after fee, across 158 rounds). They are **100.0%
rows where the mirror is broken** — one leg stale in the snapshot, not a real
two-sided price. Their shape gives them away: at a combined 0.96–0.99 the
minimum leg is 0.32–0.35 against a 0.50 counterpart. And they do not persist:

    388 episodes; 96% last a SINGLE poll tick; maximum life 1.0s over 7 days

Even taking them at face value, two taker buys are not atomic. Filling one leg
of a pair in a ≤1s window leaves a naked coin flip at ~0.50 with the fee
already paid — strictly negative EV. The strategy converts a theoretical
+1.4% into a 50/50 gamble every time the second leg slips.

### The same identity dissolves strategy 1

Because `down_bid == 1 - up_ask`, **buying DOWN at its bid IS selling UP at its
ask**. So "rest buys on both sides and collect the pair for 0.99" is not a
hedged pair at all: it is a flat book and a captured spread. Plain market
making, in a costume. The per-row figure it produces —

    pair-at-bid net of the 200bp fee: mean +0.0072, positive in 78.8% of rows

— is the 1-cent spread minus the fee, arithmetic already known, and it carries
the maker's whole problem with it: the leg that fills is the leg moving against
you, measured here at **-4.7pts of adverse selection**. Minting would have
sidestepped that by handing you both legs at once for free; without a mint
endpoint there is nothing to sidestep it with.

### Why this matters beyond these two strategies

The venue's book is priced to the fee boundary: bid 0.99, ask 1.01, settlement
1.00, fee ~200bp on the winner's profit ≈ 0.01. The entire spread **is** the
fee. That single line retro-explains a run of independent negative results —
hedging, the maker arm, the dynamic gate, the exit rules — which were all, in
different clothing, attempts to collect a spread the fee had already claimed.

**Nothing to ship. Both strategies are closed.**

## An exit rule does not beat holding, even though SELL works

The SELL path is real (a `side="SELL"` quote is refused for *inventory*, not as
an unsupported operation), which reopened a question the buy-only framing had
closed: a losing cross position need not lose its whole stake.

`tools/cross_exit_probe.py` replays 119 btc-5m cross entries at the shipped
config, captures the forward BID path on the held side, and prices exits at the
raw bid against holding to settlement:

    rule                  exits    EV/$1   growth   day-t
    hold (shipped)            0  +0.4455   +3.248   +3.74
    stop at -0.05            65  +0.2768   +1.940   +2.88
    stop at -0.10            52  +0.3151   +2.236   +3.21
    stop at -0.15            47  +0.3054   +2.149   +3.45
    stop at -0.25            33  +0.3381   +2.401   +3.72
    trail -0.05 from peak     81  +0.2220   +1.647   +3.57
    trail -0.10 from peak     64  +0.2223   +1.666   +4.27
    trail -0.20 from peak     42  +0.2883   +2.102   +4.03

Every rule destroys roughly a third of the growth. Two reasons compound: a sale
crosses to the bid, so a round trip pays the spread twice; and the arm enters
*on* a strike crossing, so a bid dipping ten cents is precisely the noise it is
built to sit through. `trail -0.10` shows a higher day-t (+4.27 vs +3.74) only
because it flattens variance — it makes less money more smoothly.

The sell path is genuine. It is not an edge. **No exit rule shipped.**

## The ledger under-books the venue's fee: `marketProviderFee` is USDT, not shares

`btcpredict/execution/live.py` read the venue's fee field on an order/history
row as SHARES and multiplied it by the fill price. It is USDT. Every limit fill
therefore booked roughly `px` of its real fee — about 58% at the prices this bot
trades — and every PnL figure we have quoted was optimistic by the remainder.

### How it was caught

The settlement path assumes `payout = fill.shares` and `pnl = payout - stake`,
neither read back from the venue, so the ledger had never been checked against
the venue's own accounting. It can be: `position/list` returns a summary with
`todayRealizedPnl`, `todayTotalCost` and `walletBalance`.

    LEDGER  2026-09-14   47 settled   staked $85.81   fees booked $0.763   pnl -$18.55
    VENUE   2026-09-14                cost   $87.11                        pnl -$19.87
    gap                                                                        $ 1.32

The 47 FILLED limit orders in `order/history` match the 47 settled fills
exactly, which makes the decomposition unambiguous:

    todayTotalCost          $87.11308
    sum(filledUsdtAmount)   $85.81000
    difference              $ 1.30308   <- the fee the venue actually charged
    field summed as USDT    $ 1.27158   error +0.03  (2dp rounding of filledUsdtAmount)
    field summed as shares*px $ 0.75248   error +0.55  <- what the code booked

`payout = shares` is **correct** — the venue does pay 1.00 per winning share and
takes nothing at redemption. The whole error is on the entry side.

### The evidence that had supported the old reading

One fill on 2026-09-12 — `marketProviderFee` 0.111 on `filledUsdtAmount` 0.83
and `filledShareQty` 5.44, i.e. px 0.1526 — reads as exactly 2.0% of shares
under the share convention and an implausible 13.4% of stake under the USDT
one. That single observation is what the code, two tests and their comments all
cited. It does not survive a 47-order reconciliation against the venue's own
cost figure, and the fee is in any case not a constant rate: across 85 fills it
ran 168.9 bps of notional in aggregate, 213.4 bps on the 71 that were charged,
and **0 on 14 of them** — every one of those a `fillPercentage == 1.00` fill.

Our own model, `fee_bps_for_price(px) = 180 * (1-px)/px` capped at 180, predicts
123.8 bps against the 168.9 actually charged: it under-states the fee by 45 bps
of stake even before the units bug.

The market path in `buy()` is untouched. It reads the QUOTE's `feeAmount`, a
different field with a different convention (deducted from delivered shares),
and every order this bot places is a LIMIT.

**Shipped.** `fee = amount_usdt(rec.get("marketProviderFee"))`, with the two
tests that asserted the share convention inverted to pin the new one.

### What it does and does not change

It does not change any decision the bot makes — `fee_paid` is recorded, not
gated on. It changes what the record MEANS: past PnL in this ledger is
optimistic by roughly 0.6-1.5% of stake, which is the correct lens for every
arm whose measured edge is thin. The cross arm's edge is not thin enough for
this to reverse it. Nothing else was live.

## Polymarket lists the SAME rounds, on the SAME oracle — and prices them differently

Asked whether to move the cross arm to Polymarket. The answer is no, but the
question turned up something better: Polymarket is not an alternative venue for
these markets, it is a SECOND venue for the identical markets.

    slug       btc-updown-5m-<start epoch>   identical on both
    window     epoch -> epoch+300            identical on both (verified, not assumed)
    title      "Bitcoin Up or Down - September 14, 4:50PM-4:55PM ET"   identical
    oracle     Chainlink BTC/USD data stream (data.chain.link/streams/btc-usd)

A winning share pays 1.00 on either venue, so Up on one plus Down on the other
is a complete pair. Unlike one venue's own Up/Down — which are mirrored into
each other and can never sum below 1.00 (see the structural-arbitrage section) —
these are two independent books, free to disagree.

**A correction on method.** The first sweep concluded Polymarket had no
short-dated markets at all: 800 markets scanned, zero resolving within 48h. That
was wrong. The gamma `/markets` endpoint silently IGNORES both `slug_contains`
and `tag_slug`, so the sweep filtered nothing and returned a default page. The
`/events` endpoint honours `tag_slug`. `tag_slug=5m` returns them immediately.
Polymarket lists eight assets on 5m rounds (btc, eth, bnb, sol, doge, xrp, zec,
hype) against this bot's three.

### They disagree on resolution 6.6% of the time, symmetrically

Same oracle, same window, and yet over 561 common settled rounds:

                    PM=Up   PM=Down
    BN=Up             262        21
    BN=Down            16       262

6.6% disagreement — the near-ties, where the boundary read differs. This is NOT
a wash: `BN Up + PM Down` pays 2.00 on (Up,Down) and **0.00** on (Down,Up). So
the pair is not risk-free, and any writeup calling it that is wrong.

It is, however, unbiased: 21 vs 16 is 0.82 sigma. E[payout] = 1.0089 one way and
0.9911 the other, s.e. 0.0108. Disagreement buys variance, not drift.

### The pricing gap (tools/xvenue_pair_probe.py, 151 obs, 9 rounds, 3 families)

The two directions sum to a near-constant — median `netA + netB = -0.0471`,
which is exactly the two spreads plus Binance's measured 169bp fee — so when one
direction is profitable the other cannot be. The arm evaluates both and takes
the better, or neither:

    positive in 83.4% of ticks;  median +0.0448/pair, +0.0648 when positive

    family            n    %>0   median best   PM depth at best ask
    bnb-updown-5m    40    80%       +0.0431         12 shares
    btc-updown-5m    55    75%       +0.0245        410 shares
    eth-updown-5m    56    95%       +0.1014         55 shares

Which venue is wrong is visible directly. On one eth round spot was 2558.565
against a 2558.625 strike — dead at the money, fair ~0.50. Binance quoted
0.49/0.51. **Polymarket quoted Up 0.39/0.40.** Polymarket's 5m books for eth and
bnb are 11-40 shares deep and lag; Binance's are tight and correct. The edge is
picking off a thin, stale book on one venue and hedging it on the tight one.

The depth is the size limit, and at this bankroll it is not binding: 410 shares
on btc, 55 on eth. A $40 book trades 5-10 shares.

### Why it plausibly persists

Collecting it requires simultaneous capital on BSC and on Polygon, an account
and signing stack on both, and execution inside the same tick. Capital cannot be
rebalanced between the chains inside a 5-minute round. That is a real barrier,
not a free lunch nobody noticed.

### What is NOT yet established

  * Whether Binance's displayed ask is fillable at size. The cross arm's own
    record is ambiguous: measured patient slippage +0.0037, but real cross fills
    show median price improvement +0.0209 and the arm still suffers cancelled
    orders. A 4.5% edge does not survive a missed leg.
  * Account access, KYC and geo for Polymarket, and USDC on Polygon.
  * Leg risk. One leg filling alone is a naked directional position. The SELL
    path on Binance is now confirmed working (MARKET and LIMIT, 50% and 100% of
    a real 4.91-share position, 2026-09-14), so an unwind exists.

**Nothing shipped. No code path trades Polymarket.**

## The cross arm pays its own limit — and the shipped buffer was eating the edge

Asked to establish whether Binance's displayed ask is fillable at size. It is
not the right question: the arm was never being slipped. It was paying exactly
what it asked to pay.

### What the live orders say (38 cross orders, 27 fills, 2026-09-14)

    cross orders PLACED 38   FILLED 27   fill rate 71.1%
    decision.quote IS the book ask       identical in 76% of orders
    requested_px - book ask   +0.1000    the buffer cross_limit_buffer_inv chose
    filled_px    - book ask   +0.0943    what was actually paid
    requested_px - filled_px  +0.0113    price improvement: almost none

Fills land AT the limit 33.3% of the time and within two ticks 55.6%. The venue
quotes, then bundles the order, and fills at about the limit rather than
price-improving to the book. `cross_limit_buffer_inv` is therefore a direct
COST, not a safety margin — at an ask of 0.6 the old 0.25 sets
`0.25 * (1 - 0.6) = 0.10`, which is the +0.10 observed.

The config had already written down this exact revert condition:

> REVERT to the proportional rule if realised fill prices start landing AT the
> limit more than ~half the time.

### The sweep (tools/cross_buffer_vs_fill.py, 119 btc-5m triggers)

Priced at the LIMIT, at the MEASURED 169bp fee, with fills modelled as "the ask
did not run past the limit within the rest window". Calibrated first: the model
predicts 78% fill at the shipped setting against 71.1% live — close enough to
trust the ordering, slightly optimistic as a queue-blind model should be.

      inv   hold    n   growth  day-t  drop-best         95% CI growth
     0.250    20   91   +0.445  +1.31     +0.201   [-0.229, +1.057]   WAS SHIPPED
     0.125    20   83   +0.628  +1.84     +0.363   [-0.065, +1.283]
     0.250     2   78   +0.492  +1.83     +0.282   [-0.093, +1.013]
     0.125     2   67   +0.835  +3.62     +0.607   [+0.280, +1.325]   SHIPPED NOW
     0.100     2   65   +0.838  +3.38     +0.597   [+0.281, +1.303]

**The configuration that was live failed all three house tests** — day-t 1.31,
and a bootstrap CI that spans zero. It was not distinguishable from no edge.
Either fix alone still spans zero. Only both together clear day-t > 2,
drop-best > 0 and a CI excluding zero.

### Why the rest duration matters more than the buffer

A cross limit that fills LATE fills because the ask came back DOWN to it — which
is exactly the move that makes the side just bought less likely to win. Long
rests are adverse selection wearing patience as a disguise. Every 20s row above
has a CI spanning zero; the short-rest rows at inv <= 0.125 do not.

`limit_wait_s` (20s) is shared with the model arm, where it is correct. The new
`cross_limit_wait_s` (2.0, `0 = OFF` falls back to the shared value) is selected
by `limit_ceiling is not None`, which is what marks the cross path inside the
shared placement block.

### The cost, stated plainly

Modelled fills fall from 91 to 67 — about a quarter fewer trades, against a
standing preference for MORE. That is the trade, and it is the right way round:
the old cell was not distinguishable from zero, so its extra fills were not
extra edge. The model also over-states fill rate by ~7 points, so live counts
will come in below 67.

### What this retracts

Every prior score that priced cross entries at `ask + 0.0037` (the model arm's
patient slippage) or at the raw ask is too optimistic. The arm pays about the
buffer. The exit-probe table earlier in this file is affected and its absolute
numbers should not be quoted; its RANKING (holding beats every stop and trail)
is unchanged, since every row there shares the same entry price.

**Shipped**: `cross_limit_buffer_inv` 0.25 -> 0.125, `cross_limit_wait_s` 2.0.
537 tests pass. Deployed and restarted 2026-09-14 21:29 UTC.

## Polymarket as a DATA SOURCE carries no information Binance's own quote lacks

Execution stays on Binance; the question was whether Polymarket's price for the
same round — same slug, same window, same Chainlink stream — predicts Binance's
settlement well enough to trade on Binance alone.

It does not. 1,504 observations over 344 rounds and 3 families, joined from
Polymarket's `prices-history` (60s granularity, 5 points per 5m round) to this
bot's own 1s quote feed and its recorded winners (`tools/pm_signal_collect.py`):

    AUC(bn_mid)  0.8530     the venue we trade
    AUC(pm_up)   0.8113     polymarket alone -- strictly WORSE
    AUC(gap)     0.5111     the gap: a coin flip

    conditional on the binance price:
      bn_mid band      n   AUC(gap)   lo-gap win   hi-gap win
      0.00-0.35      492     0.5070        0.222        0.242
      0.35-0.50      262     0.5143        0.362        0.455
      0.50-0.65      280     0.4894        0.685        0.662
      0.65-1.01      470     0.4847        0.837        0.835

The gaps are not small — |gap| > 0.05 in 60.4% of observations, IQR +-0.07 — but
they are symmetric noise around zero (median +0.0000). Polymarket is simply the
less accurate venue, which is the same fact the cross-venue pricing work found
from the other side: its 5m books are 11-40 shares deep and lag.

Traded on Binance at the REAL cost (`ask + max(0.01, min(0.25, 0.125*(1-ask)))`,
169bp fee), buying whichever side Polymarket calls underpriced:

    rule                          n    EV/$1    growth
    any gap                    1232  -0.2399   -22.140
    |gap| > 0.05                864  -0.1975   -13.142
    |gap| > 0.10                547  -0.1697    -7.352
    |gap| > 0.20                188  -0.1367    -2.174
    |gap|>0.10, bn_mid .35-.65  252  -0.1332    -2.723

Every threshold loses 13-24 cents per dollar. The sample spans a single day, so
day-t is undefined — but a loss of that size is not a sampling artifact, and
AUC(gap) = 0.51 says the same thing independently.

**Nothing to ship. Polymarket is closed as a venue, as a hedge and as a signal.**

## Duplicate cross entries: the re-entry gap was stamped with the WRONG clock

Reported live: multiple trades executing in quick succession. Confirmed, and it
had been running all day.

    round                       fills   gaps between them
    eth-updown-5m-1789413600        4   [0.436, 0.214, 0.612] s
    eth-updown-5m-1789415100        2   [0.036] s
    bnb-updown-5m-1789414200        2   [0.744] s
    btc-updown-5m-1789387500        2   [22.607] s          <- legitimate

10 of 48 real-money fills and **$29.19 of the day's $85.81 stake** went on
entries the 1.0s `cross_reentry_gap_s` was supposed to refuse. They are separate
DECISIONS, 60-70ms apart, at identical quote and stake -- not one decision
placing twice, and not two processes (one startup banner covers the window).

### The mechanism

`_cross_tick` reads `now_ms` once, at the top. The re-entry gap is CHECKED
against it there, and the same value is STAMPED into `_cross_last_ms` at the
claim — which is after an `await` on the venue position read. So the stamp
records when the tick STARTED, not when the round was claimed, and a venue read
slower than the gap expires the stamp before it is even written:

    t=0.00  A starts, now_ms=0, gap passes (nothing stamped), takes _cross_inflight
    t=0.06  B starts, blocked by _cross_inflight                        correct
    t=1.50  A stamps 0, releases _cross_inflight
    t=1.55  C starts, now_ms=1550, reads last=0
            1550 - 0 >= 1000  ->  C PASSES, 50ms after A

`_cross_inflight` is not the backstop here: it is discarded in a `finally`
BEFORE the decision is written and before `create_task(_cross_execute(...))`, so
it covers only the venue read.

Two guards each looked correct in isolation and the combination was not:
`TestReentryGap` drives `_cross_tick` sequentially, where each call stamps
before the next reads, so it could never see this. A concurrency test alone does
not see it either — with a FAST venue read the in-flight set really does hold.
It takes both: concurrent ticks AND a venue read slower than the gap.

### The fix

    lm[r.slug] = now_ms                    ->  lm[r.slug] = int(time.time() * 1000)

Stamp the claim with the time of the claim. One line.

Three new tests: sequential ticks take one entry, concurrent ticks take one
entry, and a slow venue read must not expire the gap (this one fails on the old
code with "2 entries 0.05s apart"). `test_the_stamp_is_taken_with_the_claim`
pinned the literal `lm[r.slug] = now_ms`, i.e. it pinned the bug; it now pins
the ordering, with a companion asserting the start-time stamp cannot return.

**Shipped.** 541 tests pass. Deployed and restarted 2026-09-14 21:36 UTC.

Note the interaction with the same day's `cross_limit_wait_s` change (20s -> 2s):
a shorter rest frees the arm to re-trigger sooner, so this bug would have grown
worse, not better, had it been left.

## The range gate survives the corrected entry price (and the old reason for it did not)

A cross fired live at 22:29:41 and both families refused it:

    cross eth-updown-5m-1789424700 Up not taken: round already moved 18.6bps, gate is 12.9
    cross btc-updown-5m-1789424700 Up not taken: round already moved 14.4bps, gate is 10.0

The gate's recorded justification was arithmetic on a number that turned out to
be wrong: "blocked triggers' +0.018 edge cannot survive the measured +0.088
slippage". That +0.088 was never slippage — it was `cross_limit_buffer_inv`,
the arm's own limit, which this venue fills at — and it was halved the same day.
At a blocked trigger's mean ask the buffer is now ~0.049. **The bar the gate was
calibrated against moved by half**, so the gate had to be re-asked, not assumed.

`tools/cross_range_gate_recheck.py`, btc-5m, entry AT the limit under the new
`inv = 0.125`, 169bp fee, 2s rest, bootstrapped over rounds:

    gate bps      n    win    ask   growth   day-t  drop-best            95% CI
     8           56  0.875  0.726   +0.487   +1.39     +0.242   [-0.041, +0.941]
    10           70  0.886  0.717   +0.781   +2.82     +0.553   [+0.212, +1.289]  shipped
    12           82  0.854  0.707   +0.745   +3.24     +0.489   [+0.066, +1.376]
    15           91  0.813  0.701   +0.488   +1.69     +0.232   [-0.322, +1.201]
    20          101  0.772  0.693   +0.217   +0.35     -0.080   [-0.685, +1.046]
    OFF         114  0.763  0.681   +0.244   +0.34     -0.052   [-0.780, +1.171]

It holds. The gate is the single most valuable knob in the arm: switching it off
buys 44 more fills and costs two thirds of the growth, with the CI falling
through zero.

And on this subset the refused triggers are not merely thin — they lose:

    refused by the shipped gate: n=44  win 0.568  ask 0.626  EDGE -0.058
                                growth -0.536  day-t -1.04

That is a stronger statement than the earlier correction, which measured a
different subset (all markets, a different gate) and found blocked edge +0.018.
Both can be true; the bar remains the mean ask OF THE SAME SUBSET.

### The one frequency lever with evidence behind it

Gate 12 takes 82 fills against 70, at day-t +3.24 vs +2.82 and growth +0.745 vs
+0.781 — 17% more trades for no measurable loss. NOT shipped: its CI lower bound
is +0.066 against 10's +0.212, so it is the less robust of the two, and the
evidence is btc-only (eth has 24 triggers in the corpus, bnb none). Recorded as
the best candidate if frequency has to come from somewhere.

## The gate-12 increment, priced; the 22:25 refusals, audited (2026-09-14, night)

Two follow-ups, because both of the lines above rest on tests that are weaker
than they look.

### Gate 12 is not "more trades, same money" -- it is more trades, less money

The two samples are NESTED, so comparing their 95% CIs answers the wrong
question. What matters is what the triggers gate 12 ADDS are worth -- and
growth is additive in log space, so `growth(g12) - growth(g10)` IS the (10,12]
band's own ledger. No overlapping-sample bootstrap needed
(`tools/cross_gate_increment.py`):

    band        n   win    ask    edge    growth   day-t
    (0,8]      56  0.875  0.726  +0.149   +0.487   +1.39
    (8,10]     14  0.929  0.680  +0.249   +0.294   +2.21
    (10,12]    12  0.667  0.648  +0.018   -0.036   -0.24   <- the "free" fills
    (12,15]     9  0.444  0.652  -0.208   -0.257   -3.50
    (15,20]    10  0.400  0.615  -0.215   -0.271   -0.70
    (20,+)     13  0.692  0.595  +0.098   +0.027   +0.21

The edge dies exactly AT the shipped gate. (10,12] pays +0.018 against ~0.06
of cost (buffer 0.125*(1-0.648) + 169bp fee) and its growth contribution is
NEGATIVE on both substrates (-0.036 here; gate 12 vs 10 on the 7-day recorded
book is +1.816 vs +1.917, i.e. -0.101). So the extra fills are the marginal
trades the gate exists to refuse, just slower losers than (12,20]. Keep 10,
now on the right instrument. The (20,+) row is n=13: no power either way, and
everything above 12 that HAS power points the same direction.

### The two live refusals could not have been taken the way they look

Audited the 22:29:41 refusals (round ...-1789424700, tau 19s) against the
venue's own recorded tape (VPS DB, quotes for the two slugs). Both numbers
reproduce exactly (btc 14.4 bps open->trigger, eth 18.6 -- the gate input is
the quotes tape) and both paths really did oscillate around the strike
(btc +6.9/-7.5 bps, eth +12.2/-6.3) before popping ~+5 bps through it inside
one second. Both settled Up -- refused winners. But the venue repriced within
that same crossing second (btc up-ask 0.07 -> 0.90, eth 0.11 -> 0.95):

  - the ETH trade sat OUTSIDE the arm's own band at the crossing second
    (0.95 > cross_max_price 0.90) -- no range-gate setting makes it tradeable;
  - the BTC trade sat exactly AT the ceiling (0.90): best case ~+0.09, and the
    price band, not the range gate, would bind on the sweep price anyway.

So "the gate refused two winners" is true and says nothing new: the refused
class stays under cost, and on these two the lag the arm harvests was already
repriced by the time the cross was visible.

Also swept for anything salvageable inside the refused set: range bands
(12,15] and (15,20] are strongly negative, (20,+) is 13 fills of noise; a
split by strike-crossing count resolves nothing on the priced book (cells of
8-59 fills, inconsistent across substrates), and the corpus side of that split
is mechanically N-vs-k parity -- won == (N === k mod 2), exact on 1.0000 of 524
recomputed triggers -- i.e. it measures whether the crossing gets revisited,
not where it settles. There is no subclass here worth a gate change.

## The catch: the venue WALKS to its price -- measured before shipping (2026-09-14, night)

The refusals keep producing visible winners, and the standing explanation was
"the venue repriced before we acted" -- assumed, never measured below the 1s
poll. Two facts and one instrument.

### Infrastructure first: geography is not the constraint, it is 1ms from optimal

From the VPS itself: Tokyo (Contabo Asia), `ping api.binance.com` avg **1.086ms**
(min 0.985), TCP connect 2.5ms. The venue (same `api.binance.com` host) and the
spot feed are the same metro; of the measured 89ms venue->us book lag, ~1ms is
the wire. "Move the server / detect earlier" cannot buy anything there.

### The venue's book after a crossing (Sep 13 recording, sub-second)

`data/ws/book2.jsonl` -- the venue's own book feed, 44 consecutive btc-5m
markets, ~4.3 frames/s -- matched to the Sep 13 1-second tape and the live DB's
strikes/settles. The ladder is the Up token (verified: Down-wins collapse it,
Up-wins run it to 0.99, knife-edges sit mid). For 16 up-crossings with a >=0.08
move:

    median ask path   0.475 (crossing second) -> 0.555 (+0.25s) -> 0.590
                      (+0.5s) -> 0.620 (+1s) -> 0.645 (+2s)
    50% of the move done by  +513ms (q25 +380, q75 +905)
    90% done by              +1025ms
    jump-sized movers (>=0.3): the first observable ask sat 0.22-0.39 below the
    +2s ask (median ~0.33)

The venue does not step to the final price at the cross; it walks there over
~1s. On a move like tonight's BTC 0.07 -> 0.90, a quote taken in the first
~250ms sits a quarter to a third of a dollar below one taken a second later.

### And it is the QUOTE that locks the price

34 live `cross exec` lines (decided / limit / fill / elapsed / book-now) show
fills landing at ~their own limit (median limit - 0.02; 12 at >=97% of the
intended size) while the NO-FILLS are the runners (limit 0.57 against a book
that went to 0.92; limit 0.44; limit 0.17). One fill paid decided+0.14 while
the book sat still. Mixed micro-mechanics, but the part that is stable: what we
pay tracks what WE saw and committed when we quoted, not the book three seconds
later. Quoting early is therefore the lever -- and the 2s rest does not cost
the price.

### Shipped (commit 007a14d, deployed 2026-09-14 23:00 UTC)

  * every refusal now stores the WS ask it saw (`quote` was None -- the one
    clean read of "the venue had not repriced yet", thrown away);
  * every trigger meters the book at +0.25/+0.50/+1.00s (`cross meter` log
    lines). Log-only; cannot touch an order.

PRE-REGISTERED, so this is a test and not a story: over the next day the meter
must show the +0.25s ask materially below the +1.0s ask on gated refusals (the
Sep 13 shape), and re-pricing the refused class at the +0.25s ask must clear
the ~0.06 buffer+fee. If instead the +0.25s ask is already ~equal to +1.0s --
our market repricing faster than the recorded one -- there is no catch, the
gate stays at 10, and the "detect/execute earlier" idea closes for good. NO
GATE CHANGE until that read exists.

### Addendum (23:16 UTC): the second half of the question gets its own instrument

The read tool (`tools/cross_catch_report.py`) is live, and its first output --
bucketing the 34 historical orders by how far the book ran past OUR limit --
already says something the catch thesis needs to hear:

    run < 0      9/9 filled
    0 - 0.1      3/3 filled
    > 0.35       0/2 filled   (one round, two orders)

Honouring is clean while the book stays near the limit, and the single case
where it ran +0.35 away did NOT fill. That is the JUMP-SIZED regime the catch
thesis lives on, and natural fills cannot settle it: the range gate refuses
those crossings before the venue can answer, so they never produce a fill to
watch. So the question gets a real-money probe instead:

  * `cross_probe_enabled` (default FALSE): quotes RANGE-REFUSED crossings,
    sized by the arm's NORMAL rule (no special stake -- what it measures must
    be what a gate raise would actually trade), capped
    `cross_probe_max_per_day` (6), labelled "cross probe" in
    decisions.reason. Guard, caps, reserved slot and exposure checks are
    UNCHANGED. Revert = one config line.
  * It spends money to buy mechanics data, not edge, at the normal order
    size; ~-$20/day worst case at the current formula; the cap (6/day) is
    the only probe-specific number.

With the meter, this settles both halves: (a) how fast OUR market's book walks,
and (b) whether the venue fills a quote taken while it walks. Those are the two
numbers the gate decision needs; neither exists yet. GATE STILL 10.

TURNED ON 2026-09-14 23:21 UTC on explicit instruction, with exactly ONE knob
changed: `CROSS_PROBE_ENABLED=true` in the server `.env` (backup
`.env.bak-probe-*` beside it). Probe orders are the arm's NORMAL order -- same
sizing formula, tilt and caps, NO special stake; the first build had a flat-$1
override and it was removed at 23:23, before any probe order fired (verified:
zero `cross probe` lines at the fix). The only probe-specific number is the
cap (6/day). Verified from a fresh interpreter all the same:
`probe True | cap 6 | min_order 1.0 | streak 12`. (Note the live streak halt
turned out to be 12, not the 5 the older notes carry.)

Time-boxed: ~2 days or ~20 probe orders, whichever first, then OFF and read
`tools/cross_catch_report.py`. Revert = one line + restart. The probe's fills
decide the thesis: fills at run >= +0.2 -> catch real -> gate raise is next;
runners consistently no-fill -> catch dead for jumps, gate stays 10.

### First probe fire (2026-09-14 23:29:18 UTC): the runner does not fill

Probe 1 (of 6 that day) fired on `btc-updown-5m-1789428300` Down: decided
0.180, tau 41s, normal size ($3.00; decisions row `cross probe Down @0.180
tau41s run+0.000`, `stake 3.0`, `acted=0`). The book walked exactly as the
thesis says -- and the fill did not come:

    t+0.00s   trigger, book 0.17-0.18        (limit posted: 0.28)
    t+0.25s   meter s25  = 0.17              <- still the stale price
    t+0.50s   meter s50  = 0.46              <- the walk happens inside this 0.25s
    t+1.00s   meter s100 = 0.53
    t+2.90s   exec window closes: NO FILL, committed $0, book-now 0.51 (run +0.23)

The round settled **Down**: the missed order would have paid ~+2.57/$1 at its
limit (~+1.17 at the +0.50s ask), gross of fee. That is the adverse-selection
shape in a single observation, not a reopened case -- the misses are exactly
where the move continues, and the 09-14 seven-sample table above priced it:
+7.17/$1 at our bids, -0.73 at the price actually on offer. Two neighbours on
the same move -- a btc 15m entry (limit 0.28 vs book 0.52, run +0.24) and an
eth entry whose book-now ended BELOW its limit yet never filled (run -0.12,
the documented dark-book residual) -- also NO FILL. Fill-honouring table now:

    run < 0     9/11 filled
    0 - 0.1     4/4  filled
    0.2 - 0.35  0/2
    > 0.35      0/2

Mechanism read: the pre-reprice window is real but ~0.4s wide (trigger price
still on s25; 0.46 by s50), while our order -- out ~0.1s after the trigger --
did not transact inside it; from +0.5s the ask sat 0.18 above our limit and
never came back. One sample cannot split "venue make-live slower than the
window" from "that ask was never tradeable for our size", and neither reading
reopens the case: the first is venue-side (nothing of ours to tune), the
second was already tested flat (`stale_maker`).

#### Correction to the earlier meter read

The first pass of this note said the reprice completed "within 250ms". Wrong
mark: the meter's three values are s25/s50/s100, and this row shows s25 still
AT the trigger price. The correct aggregate over the 7 metered triggers is
s100 - s25 median **+0.17** (q25 +0.02, q75 +0.26, n=7): the venue keeps
repricing for at least a second (Sep 13 shape holds), and our fill path loses
the race at the +0.5s mark, not at +0.25s.

#### Cross-references: this question was answered before (09-12..14)

  * "The cancelled orders: adverse selection, and why NOT to widen the buffer"
    (above): 7 missed fills, +7.17/$1 at our bids vs -0.73 at the real ask;
    "we fill when the book stays put and miss when it runs -- and it runs
    precisely when we are right." Standing rule: do not raise
    `cross_limit_buffer*`; nothing shipped then, and nothing here changes it.
  * Sub-0.40 fill-rate table (same section): "below 0.40 nothing fills, ever"
    -- 0/5 then, 0/6 with this probe. The probe's decided 0.18 is that class;
    informative probe samples would be at 0.40+, which the gate refuses less
    often, so do not read one sub-0.40 no-fill as a new anomaly.
  * `stale_maker` (09-12/13): the literal rest-before-the-move design
    (`stale_maker_px`; "the measured edge is ENTIRELY in the fill price: at
    the ask +0.0175/$1, at the mid +0.3449"). Live: n=24 flat (+1.4 pts,
    0.1 SE). The closest prior to the remaining "pre-cross quoting" item --
    it did not rescue the execution-edge theory.
  * `impulse` (09-12): the other catch-the-move attempt. "The fill price, not
    the trigger, decides this strategy"; market path paid +0.16 mean drift
    over the ask (impulse entries +0.21/+0.67). Converted to limit@fair, then
    switched off.

#### Status after read #1

Probe continues under the pre-registration (~2 days / ~20 orders; counter
resets 00:00 UTC). **No knob changes anywhere** -- gate 10, buffer, sizing all
stand; sizing was verified live ($3.00, normal formula). The read pushes the
posterior to "catch dead for jumps" (the walk is real; transacting inside it
is not ours to do); the remaining fires mostly test whether any 0.40+ refused
crossing behaves differently. Early revert = one `.env` line + restart.

### Does the DEPTH gate need to be dynamic? No -- measured (2026-09-15)

Asked after the 23:39 UTC loss: `btc-updown-5m-1789428900` Up decided 0.561,
filled 0.6165 (`ev_at_fill` **-0.1024** recorded at fill), depth +0.76 bps at
the trigger, sigma ~3 bps/min, the round settled Down (end 78288.655 vs strike
78299.995) -> **-$3.07 + $0.06 fee**. The question: should
`cross_max_depth_bps` (static 5.0) become a function of volatility? Mirror of
`cross_dynamic_gate.py` (same recorded book, same scoring), pointed at depth:
`tools/cross_depth_dynamic.py`.

BTC-5m, 162 triggers / 7 days post the shipped range-band gates, sigma median
3.0 bps/min. Static sweep first -- what a cap tight enough to block that loss
(0.76) costs:

    cap    kept   win    ask    edge   additive g/t    reverting g/t
    0.25    20%  0.909  0.617  +0.292   +0.893/+1.79    +0.705/+1.92
    0.5     44%  0.901  0.633  +0.269   +1.502/+2.12    +1.343/+2.75
    0.75    65%  0.876  0.646  +0.230   +1.661/+2.26    +1.633/+3.11
    1.0     74%  0.875  0.656  +0.219   +1.856/+2.61    +1.821/+3.34
    2.0     93%  0.880  0.683  +0.197   +1.904/+2.35    +2.137/+3.05
    5.0    100%  0.883  0.691  +0.192   +1.943/+2.40    +2.282/+3.20   <- shipped

The 0.75-5.0 band carries +0.65 log-growth over the recorded week (reverting)
against the loss's own -0.07: a cap tight enough to block the loss class gives
up ~9x what the single loss cost.

Dynamic rules, PAIRED BY DAY against shipped static (reverting model):

    sig x0.5   (81% kept)  -0.036  t-1.04      exp x0.5 (97%)   -0.013 t-1.28
    sig x1..x2 (93-100%)   ~0.000              exp x1+  (100%)  no-op
    min(D,sig xk)          no-op for k>=2     trail x0.05..0.3 -0.045..-0.182 (t to -2.65)

**No variant beats static; the ones that bite are negative; trail (tighten in
calm) is the worst family.** Same verdict as the range gate's dynamic test,
same reason: 93%+ of triggers sit under 1x sigma, so a vol-scaled depth cap is
either a no-op or cuts real flow.

Cells: on the live flow no negative cell remains. The storm-x-deep erosion
(storm half, depth 1-2: win 0.565, edge -0.084, n=23) exists only BEFORE the
range gate -- that population is what the range gate already refuses, and its
dynamic variants were already tested and rejected. `--no-range` secondary read
agrees: 242 triggers, every trail variant negative (t to -3.29).

NOTHING SHIPPED; `cross_max_depth_bps` stays 5.0. The loss entry was a
normal-depth member of the profitable band -- a whipsaw loss, not a gate
failure. (For the record: the recovery entry that could have made the round a
winner was the Down reversal at tau 28s, refused by the reversal guard -- a
different knob, and its cost is being recorded, not acted on tonight.)
Limits: 7 days, BTC-5m; a dynamic edge would need regime changes to show, so
revisit with more data -- same caveat as the range-gate test.

## The hedge gate, as stated, measured twice (2026-09-15)

The operator's phrasing: "if a cross trade reverses on us, we execute the
opposite side of it, to minimize losses." Measured as stated --
`tools/cross_hedge_on_reversal.py`, recorded book, BTC-5m, 162 entries, the
reversal = first opposite crossing after entry (best case: hedge leg paid at
the raw opposite ASK):

    20/162 entries reversed before the round ended
    P(entry still wins | reversed) = 0.300     (other samples: 0.357, 0.426)
    pair cost at the reversal: mean 1.302  median 1.295  (<1.0 on 15%)
    return/$1 on the reversed set: hedge -0.431   vs   hold -0.549

    policy                       book g/t      additive g/t    reverting g/t
    hold (shipped)            +3.955/+3.12   +1.943/+2.40    +2.282/+3.20
    hedge at every reversal   +4.134/+3.63   +2.085/+3.33    +2.389/+4.28
    hedge, +0.088 slip leg    +3.949/+3.50   +1.926/+3.06    +2.236/+4.03
    hedge only if pair<1      +4.058/+3.23   +2.022/+2.52    +2.333/+3.33

Read: "losses are almost always 0" is false -- the reversal locks the loss at
~-43% of stake on average (pair 1.302 for a 1.000 payout). The small policy
gain is a wash the moment the hedge leg pays one tick of the arm's own
slippage (+0.088: +1.926 additive vs hold +1.943), n=20 flips on 1-2 rounds,
and **85% of the hedge orders would be refused outright by the shipped
`max_paired_price = 1.0` guard** (pair > 1.0 -- the typical reversal case).

Why it cannot be more than a wash, three ways, all pre-existing:

  * the algebra (same-round reversal section): with the first leg sunk,
    EV(take) - EV(decline) = stake_B * [(1-p)/b - 1] -- the entry price
    CANCELS. Hedging at the reversal is economically the reversal ENTRY
    itself, which the 249-reversal corpus scores day-t -3.05 and
    `cross_allow_reversal=False` refuses;
  * the market: our side wins 0.300 | reversed, and the hedge only pays when
    the opposite ask is under 1-p. It sits ~on that line -- slightly under
    on this n=20 slice, slightly over on the powered corpus. A coin flip
    priced by a spread;
  * the fill: the hedge leg is a runner (the opposite side just spiked) --
    the fill-honouring table's worst class (runs >= +0.2: 0/4). The raw ask
    is a bound, not a fill. And the real-reversal cases (where the hedge
    would have helped, like tonight's $3.07 loss -> locked ~-$1.33) are
    exactly the ones where the leg is running hardest.

The OTHER variant -- lock while WINNING when the pair goes cheap
(`tools/cross_lock_hedge.py`, same rows, fresh run): every threshold destroys
growth. Hold +3.955/+1.943/+2.282 -> lock<1.00 +1.612/+0.646/+0.492; lock<0.90
+2.440/+0.983/+0.933. The lock caps an 88%-win distribution to buy protection
from a 12% tail, and pays the fee twice for it.

And the exit comparison stands: stopping/exiting at the bid destroys ~1/3 of
growth (119 entries, best stop +2.401 vs hold +3.248) -- and by the mirror
identity (`down_ask == 1 - up_bid`) an exit IS this hedge at the same price.
The venue's whole spread is the fee; a hedge round-trips it.

KEEP the guards (`cross_allow_reversal=False`, `max_paired_price=1.0`).
NOTHING SHIPPED. Limits: n=20 reversed (sign flips on 1-2 rounds), BTC-5m,
7 days -- the powered corpus is the substrate that decides, and it already
had. Not "very wrong": it is the refused reversal entry in disguise, and its
net measured effect under realistic fills is zero, not catastrophic.

## PROBE OFF (2026-09-15 ~00:00 UTC) -- final record

Turned off by operator instruction after 2 shots / 0 fills, both in the
decisive runner class:

  * shot 1 (23:29, btc-5m Down, limit 0.28): book 0.17 -> 0.46 -> 0.53
    within a second; no fill, run +0.23.
  * shot 2 (23:54, eth-5m Down, limit 0.36): book 0.27 -> 0.57 -> 0.72;
    rested the designed 2s window and was cancelled; the round then whipsawed
    (Down traded 0.72 mid-round) and settled Up -- a fill would have lost the
    full stake. The operator flagged exactly that, and it is the class's
    adverse-selection shape: the runner class is not fillable, and anything
    that WOULD fill in it is a coin-flip bought at a spread.

The direct question ("does a range-refused crossing's early quote fill while
its book runs") is answered negative on the live record, consistent with
every prior: the fill-honouring table (runs >= +0.2 -> 0/4 ever), the sub-0.40
zero-fill bucket, the buffer sweep, and the adverse-selection table
(+7.17/$1 at our bids vs -0.73 at the real ask -- do not chase). Range gate
stays 10; no knobs changed anywhere. Revert path: `CROSS_PROBE_ENABLED=true`
+ restart. The day's order count is back to the normal arm alone.

## The cross cancel wait: 2s had a reason; the live fills say it was wrong; 6s ships (2026-09-15)

Re-asked after the interim change to 20s, because the 2s had a documented
rationale. It did -- and working it through moved the answer twice.

### The original reason (2026-09-14)

`tools/cross_buffer_vs_fill.py` chose hold 2 with inv 0.125: "a cross limit
that fills LATE fills because the ask came back DOWN to it -- adverse
selection wearing patience as a disguise. Every 20s row has a CI spanning
zero; the short-rest rows at inv <= 0.125 do not." Cost stated: 91 -> 67
modelled fills. Re-run on current data, that tool still prefers short holds
(hold2 +0.84 vs hold20 +0.63) -- but note its fill window starts AT THE
TRIGGER, with no placement latency.

### The corrected sweep

`tools/cross_wait_sweep.py`, aligned to the calibrated tool (re-entry gap,
tick floor, both fill-price conventions, LAT 2/3/4). At the live convention
(LAT=3s, pay-the-limit):

    wait    fills  fill%   win    growth  day-t  drop-best
     2.0       58   48.7  0.810   +0.265  +1.23   +0.098    <- was shipped
     5.0       68   57.1  0.824   +0.389  +1.28   +0.198
     6.0       68   57.1  0.824   +0.389  +1.28   +0.198    <- ships (+margin)
    10.0       72   60.5  0.819   +0.392  +1.24   +0.201
    20.0       79   66.4  0.835   +0.573  +1.72   +0.347

5s (and now 6.0) sits above 2s under every convention (+1.20 vs +0.83 at
touch pricing). The extension past the live-observed ~5.2s is model-dependent
and UNOBSERVED live.

### The live referee: every settled fill by completion latency (n=27)

    <=2.5s     5 fills   2W   (0.40)
    2.5-3.4s  10 fills   7W   (0.70)
    >3.4s     12 fills   9W   (0.75)    <- the class a 2s cancel kills

Slow fills are the BEST class the live record has, not the worst -- the
adverse-selection thesis is refuted by the bot's own fills. (Includes 00:24
UTC 09-15: 92% at 0.5976, reported in 5.20s, won +0.67/$1 -- an order a 2s
cancel provably kills.) The band past ~5.2s has produced zero fills in 45
exec attempts to date, so the extension remains unobserved both ways.

### Shipped

`cross_limit_wait_s` -> 6.0 (from the interim 20.0; 5.0 briefly, then +1 for
margin). Why not 5: every long fill the venue has produced reports at
5.12-5.21s (four, 2W/2L), and that report includes the fill-confirm lag --
a 5.0 cancel raced exactly the tail class the live record says works
(>=3.4s: 9W/12). 6.0 clears every demonstrated fill with ~0.8s to spare, and
its sweep row is IDENTICAL to 5.0's (68 fills, +0.389/1.28/0.198) -- the
extra second adds no modelled fill, so the margin is free. CHECKPOINT: keep
scoring live fills by latency -- if the >3s class turns negative over ~20
more fills, tighten toward 2; if fills start appearing in the 6-20s band and
win, extend toward 10-20. Same-day: the guard audit restored
`daily_loss_cap_pct` 0.0 -> 0.20 in `.env` (left off since the 09-11
no-guards experiment -- the only guard that was off).

## Winnings claims: no 15m bug, but no retry clock either (2026-09-15)

Asked whether 15m winnings get redeemed. The path is family-agnostic --
`redeem()` asks the venue what is CLAIMABLE for the WALLET -- and the claim
call that fired when the first (and only) 15m win settled
(`btc-updown-15m-1789427700`, +$1.93) came 6 seconds after its round ended.
The venue's own summary, read back today: `totalClaimableAmount: 0`,
`pendingClaimCount: 0`, `positions: []` -- nothing unredeemed in any family.

What the audit DID expose is why a payout can look stuck. `redeem()` fired
only when a WIN settled, and its failures were never retried until the NEXT
win. Both failure modes are in the log:

    -9000 SYSTEM_ERROR         23:30 09-14  a token failed in the claim call
                                            that fired 6s after the 15m round
                                            ended -- the venue rejects a
                                            position that is not yet final
    "already have a pending    19:25 09-14  repeated re-POSTs of a claim the
     claim ... please wait"                 venue already had in flight

On a losing stretch, or idle overnight, winnings could sit claimable for
hours. None did -- the next win came first -- but the trap is worst exactly
where the question pointed: a claim fired seconds after a round ends is the
one most likely to be rejected as not-yet-final.

Fix: `sweep_claims()` -- a wallet-wide `redeem()` every `_CLAIM_SWEEP_S`
(180s) from the run loop, independent of settlements. `redeem()` no-ops when
the venue lists nothing claimable (steady-state cost: one read per cooldown),
and the clock starts at 0 on boot, so a restart adopts stranded claims. The
on-win redeem stays as the fast path. Tests: tests/test_claim_sweep.py.

## The 15m families are inert and cannot be calibrated: their book is stale

Asked why bnb-updown-15m bought Down on a round that settled Up while
bnb-updown-5m bought Up on the same move. Not a bug -- two guards firing
asymmetrically -- but the investigation says something worse about 15m.

### What happened, from the tape (2026-09-15 00:59)

    strike 15m 719.255      strike 5m 719.185      (7 cents apart)

    00:59:21.356  15m  Up   REFUSED: only 2 tick(s) through the strike, floor is 5
    00:59:21.643  5m   Up   TAKEN    spot 719.27 vs strike 719.18, tau 38s
    00:59:22      spot 719.175 -> 719.275   crosses UP through both strikes
    00:59:25      spot 719.275 -> 719.165   crosses DOWN through both strikes
    00:59:25.056  5m   Down REFUSED: reversal of the Up entry already taken
    00:59:25.130  15m  Down TAKEN    spot 719.16 vs strike 719.25, tau 35s
    00:59:39      spot 719.345               both rounds settle Up

The 15m arm was offered the CORRECT side four seconds earlier and refused it on
the depth floor: spot was 2 ticks past its strike, against a floor of 5. The 5m
arm passed the same floor on the same move only because its strike sat 7c lower,
putting spot 9 ticks clear. On the reverse leg the roles swapped -- and the 5m
was protected by the reversal guard while the 15m, holding nothing, was not.

Both entries are correct per `crossed_side`. The depth floor is measured from
each round's OWN strike, so inside a 32-cent chop with strikes 7 cents apart,
which family gets to trade is decided by noise.

A cross-family contradiction guard is the obvious idea and is NOT justified:
across all 36 cross fills ever, this has happened ONCE.

### Why it cannot be settled with data

`tools/cross_15m_calibrate.py` replays the 15m families off the live DB
(567 settled rounds, 247,566 quote rows). Almost nothing survives:

    family            in tau 5-60s  crossed  through ALL gates  HAS AN ASK
    btc-updown-15m           7,948       34                 34           6
    eth-updown-15m           7,948       38                 25           0
    bnb-updown-15m           1,961        5                  2           2

The gates are not the constraint -- btc-15m loses none of its 34 crossings to
them. **The ask is.** And this is not a recorder bug: the websocket override in
`_record` deliberately carries no `trade` gate so recorded families get a real
ask. It writes None because `wsb.best(market_id, ws_book_max_age_s)` returns
None -- the 15m book is genuinely STALE about 78% of the time. Which also means
`_cross_one` refuses these families live for the same reason.

### What they have produced

    family              n  won   staked      pnl    ev/$1
    btc-updown-15m      1    1     3.00    +1.93   +0.643
    bnb-updown-15m      1    0     3.48    -3.48   -1.000
    ---- 5m families   34   23    87.56    -1.97   -0.022

**Two fills in five days.** They add no frequency, carry no measurable edge, and
cannot be validated from recorded data because the book they price off is stale
most of the time. Both fills are also n=1 per family, so neither number means
anything.

No config change made: the six-market widening was an explicit operator
decision. Recorded so the choice is informed -- the case for dropping 15m from
`CROSS_TRADED` is that it is unmeasurable, not that it is proven bad.

## The whipsaw hole: `cross_allow_reversal` arms on the ENTRY, not on the CROSSING

Reported: bnb-updown-15m bought Down on a round that settled Up, while
bnb-updown-5m bought Up on the same move.

### The spike is real

The venue's own chart shows no move in that last minute, which looked at first
like our tape carrying a bad tick. It is not. Binance 1s klines, independent of
both the bot and the venue:

    00:59:21   high 719.280  close 719.280   45 trades   crosses UP through 719.255
    00:59:22   719.270 flat                   4 trades   entire bar above strike
    00:59:23   719.270 flat                   0 trades   entire bar above strike
    00:59:24   open 719.270  low 719.160     36 trades   crosses DOWN

BNB traded above the strike for three seconds and fell back. The venue's chart
is downsampled -- a 3s move cannot render at 15-minute width.

Two other theories died on the way, both worth recording:

  * **Not a wrong price series.** The venue's own API answers this:
    `priceFeedProvider: CHAINLINK`, `priceFeedSymbol: BTCUSDT`,
    `priceFeedId: 0x0003ef42...`, and `feeRateBps: 200` (which independently
    confirms the fee measured from fills). We trigger on Binance spot. But
    comparing each round's strike (Chainlink at the open) against our recorded
    spot at the same instant, over ~2,100 rounds: median and p90 difference are
    both **0.0000**. The two series track tick for tick.
  * **The `oracle` column is empty on all 6 families, 0.0% coverage.** It was
    never populated. Worth fixing if the basis ever matters again, but it does
    not here.

### The actual hole

    00:59:21.356  15m  Up   REFUSED  only 2 tick(s) through the strike, floor is 5
    00:59:21.643  5m   Up   TAKEN    spot 9 ticks past ITS strike (7c lower)
    00:59:25.056  5m   Down REFUSED  reversal of the Up entry already taken
    00:59:25.130  15m  Down TAKEN    spot 9 ticks past its strike on the way back

`cross_allow_reversal` refuses the opposite side of a round -- that is what
stopped the 5m arm 70ms earlier. But it arms on the **entry**. The 15m arm
DECLINED its up-cross on the depth floor, so nothing armed, and the return leg
of the same oscillation walked in. Depth is measured from each round's own
strike, so with strikes 7c apart inside a 32c chop it is noise that decides
which family is allowed to trade.

### Flipped crossings have no edge

Scored as a class, which needs no fill model and so uses far more of the tape --
211 crossings that pass every gate, across three families and two independent
data sources. `edge = win - ask`:

                        n    win    ask     EDGE
    btc  FLIPPED        6  0.667  0.702   -0.0350
    btc  clean        160  0.887  0.691   +0.1969
    eth  FLIPPED        7  0.571  0.619   -0.0471
    eth  clean         33  0.727  0.652   +0.0755
    POOLED FLIPPED     14  0.643  0.650   -0.0071
    POOLED clean      197  0.863  0.686   +0.1771

A flipped crossing has no edge at all, never mind enough to clear the limit
buffer. Consistent in sign on both families with data. It is 6.6% of crossings.

Priced through the full fill model the effect is small because it is rare --
btc 83 -> 82 fills (growth +0.629 -> +0.705, day-t +1.84 -> +2.36, CI moves off
zero), eth 23 -> 20 (-0.020 -> +0.087). n=14 blocked is thin, so the case rests
on the sign agreeing across independent families AND on closing a hole the
neighbouring guard was already written to close -- not on those CIs.

**Shipped**: `cross_flip_block_s = 20.0` (0 = OFF), stamped on the CROSSING
before any gate. The window is flat from 2s to 60s in the fill-priced sweep, so
20 is the middle of a flat region rather than a peak.

Four tests, and two notes on making them mean anything: the fixture's default
asks are (0.30, 0.70) against a `cross_max_price` of 0.50, so a Down entry is
refused on the price band and every assertion about a reverse leg passes for the
wrong reason -- they now use (0.30, 0.40). And `cross_allow_reversal` is forced
ON in the whipsaw test so it cannot do the blocking; otherwise the test would
keep passing with the new guard deleted.

### Not shipped

A cross-FAMILY contradiction guard (refuse a cross on an asset opposite to one
another family just took). Across all 36 cross fills ever, that has happened
ONCE. Nothing to fit.

## CORRECTION: the depth floor was refusing the arm's CHEAPEST trades

Reported live: two crossings missed in five minutes on bnb and eth -- "price
stalled at the line then went the good direction". Confirmed, and it is the
depth floor, not the whipsaw guard shipped minutes earlier:

    01:19:17  cross eth-updown-5m Up not taken: only 2 tick(s) through the strike, floor is 5
    01:19:32  cross bnb-updown-5m Up not taken: only 3 tick(s) through the strike, floor is 5

Both went the way the crossing pointed, and the venue agreed within a second --
bnb's Up quote ran 0.720 -> 0.750 on the cross meter.

### The error: a win-rate table with no ask column

`cross_min_depth_ticks = 5` rested on this:

    ticks    n    win
    0-1     34   0.647
    2-4     36   0.611
    5-15    57   0.825
    51+    145   0.876

Shallow crossings really do win less. But the table prices nothing. **This is
the trap this file has now recorded FOUR times**: the bar is the mean ask OF THE
SAME SUBSET, never a win rate compared across subsets that are priced
differently.

A shallow crossing is, by definition, one the venue HAS NOT REPRICED YET -- so
it is cheap. Re-measured over 216 crossings passing every gate except this floor
(btc corpus, eth and bnb from the live DB, whipsaw guard applied), with the ask:

                           n    win    ask     EDGE
    below floor (<5t)     19  0.789  0.558  +0.2311
    at/above floor       197  0.863  0.686  +0.1771

They win less AND cost less, and the second effect is the larger one. The sign
is consistent on all three families -- btc +0.243 vs +0.197, eth +0.185 vs
+0.076, bnb +0.360 vs +0.225.

Priced through the full fill model:

    btc  floor 0  n=86  growth +0.899  day-t +2.23  CI [+0.212, +1.539]
         floor 1  n=83         +0.711        +2.36     [+0.093, +1.264]
         floor 5  n=82         +0.705        +2.36     [+0.078, +1.287]   was shipped
    eth  floor 0  n=29         +0.175        +0.87     [-0.390, +0.717]
         floor 1  n=27         +0.192        +1.50     [-0.308, +0.737]
         floor 5  n=20         +0.087        +1.10     [-0.325, +0.443]   was shipped

**Shipped `cross_min_depth_ticks = 1.0`.** Not 0: at exactly zero ticks spot
sits ON the strike and the side is ambiguous, and btc's extra +0.19 at floor 0
rests on 3 trades. 1 leaves btc where it is and roughly doubles eth's growth.

### Why it is safe to relax now and was not before

The floor was doing two jobs: refusing OSCILLATION (bnb's last decimal flipping
back and forth, the case it was written for) and refusing SHALLOW-BUT-REAL
crossings. `cross_flip_block_s`, shipped hours earlier, does the first job
directly. The floor no longer has to buy it by throwing the second away.

### What it was costing

    family           refused by the old floor
    bnb-updown-5m    3 of   7 crossings = 42.9%
    eth-updown-5m   10 of  43           = 23.3%
    btc-updown-5m    6 of 166           =  3.6%

Which is why every live refusal being reported was eth or bnb: a 5-tick floor is
$0.05 on every symbol, i.e. 0.006 bps on BTC at $78,000 and 0.69 bps on BNB at
$720. The tick unit was chosen to scale across symbols and does the opposite.

Two tests pin the SHIPPED value against the live case (a 2-tick crossing must
trade; spot exactly on the strike must not). `TestTickDepthFloor` monkeypatches
5.0 and still covers the mechanism.

## Per-symbol tuning was blocked by the RECORDER, not by the knobs

Asked whether each symbol has its own optimised values. It does not, and only
one knob ever did:

    knob                  bnb15m  bnb5m  btc15m  btc5m  eth15m  eth5m
    min_tau_s / max_tau_s    5-60   5-60    5-60   5-60    5-60   5-60
    min_depth_ticks           1.0    1.0     1.0    1.0     1.0    1.0
    max_depth_bps             5.0    5.0     5.0    5.0     5.0    5.0
    max_round_range_bps      21.1    7.8    13.6   10.0    21.0   12.9   <- the only one
    max_per_round / stake       5      5       5      5       5      5

Two of those are now answered rather than merely shared:

  * **`max_depth_bps` is INERT.** On btc, 5 / 8 / 15 / OFF all return the same
    83 trades and +0.711 growth -- the ceiling never binds. Nothing to tune.
  * **The tau window agrees across symbols.** Widening is decisively bad on
    both families with data (btc +0.711 -> -0.530 at 5-120s; eth +0.192 ->
    -0.177). The apparent split -- btc likes 10-60s (+0.755), eth likes 5-60s --
    is three trades' worth of difference.
  * And the depth floor, at 1 tick, is no longer symbol-dependent at all. At 5
    it was ($0.05 = 0.006 bps on BTC, 0.69 bps on BNB); at 1 it means "strictly
    past the strike", which is the same condition everywhere.

### The real obstacle

    family              rounds  days  crossings  PRICEABLE
    btc-updown-5m         1656     7        302        302
    eth-updown-5m         1268     7        249         98
    bnb-updown-5m          677     4        159         32
    btc-updown-15m         227     5         34          6
    eth-updown-15m         227     5         38          2
    bnb-updown-15m         110     4          5          4

btc has 302 priceable crossings; bnb-5m has 32, and FOUR survive the shipped
gates. Every per-symbol question is blocked on that one number, and the cause is
not the market -- it is that `ws_book_max_age_s` (2s) gates the RECORDER as well
as trading. Ask coverage was btc-5m ~100% (it is the primary and gets the REST
book every poll) against eth-5m 31% and bnb-5m 17%.

### The fix

`ws_book_record_max_age_s = 30.0` (0 = OFF, falls back to the trading gate). The
recorder fills the four book columns from a wider window ONLY where the fresh
lookup returned nothing, and stamps `book_age_ms` alongside. Strictly more
information: a replay filters to whatever freshness it wants, and one that
ignores the column is no worse off than before.

It cannot reach an order. `_cross_tick` and `_cross_execute` do their own
`wsb.best(..., ws_book_max_age_s)` lookups, and a test asserts the new setting
name appears in neither.

Measured four minutes after deploy:

    family            before   after   mean book age
    bnb-updown-5m       17%     51%          1110ms
    eth-updown-5m       31%     73%           604ms
    eth-updown-15m      22%     96%           225ms
    btc-updown-15m      22%     67%           177ms
    bnb-updown-15m      22%     41%           989ms

**Nothing about trading changed.** This buys the data that per-symbol tuning
needs; the tuning itself waits until bnb and the 15m families have enough
priceable crossings to answer with, rather than being fitted to 4.

## `cross_max_per_round` was 5 on evidence that could not exist

Flagged live: bnb-updown-15m placed two orders one second apart.

    01:29:27.666  CROSS Up sweep_px=0.693  tau 32s   -> 15 today
    01:29:28.735  CROSS Up sweep_px=0.732  tau 31s   -> 16 today
    01:29:29.673  FILLED @0.6993  $3.21
    01:29:30.706  FILLED @0.7356  $3.20

**The guards are working.** This is not the duplicate bug fixed earlier the same
day -- those were 36-70ms apart, this is 1.07s, clearing `cross_reentry_gap_s`
(1.0s) by 69ms. Both entries are the SAME side, so neither the flip guard nor
`cross_allow_reversal` applies. It is the designed same-side repeat.

The design is what does not hold up.

### The replay cannot produce a second entry

`quotes_arm` is sampled at 1s and `cross_reentry_gap_s` is 1.0s, so consecutive
entries are all but impossible in it. Across the whole corpus:

    btc-updown-5m   ONE second entry
    eth-updown-5m   TWO

and so `max_per_round` 1 vs 5 moves btc from 82 to 83 fills and eth from 25 to
27. The +0.022 growth that appears to favour 5 IS those two trades. Likewise
`reentry_gap` 1s / 2s / 5s / 10s are all identical on btc. **Every one of these
knobs is inert on the only substrate they were measured on.**

Live is a different clock -- the websocket runs ~114/s -- so repeats happen
constantly, and that is the only place they can be judged:

    FIRST entry in a round   n=31  won 21/31  staked $79.62  pnl -$3.35  ev/$1 -0.042
    LATER entries            n= 7  won  3/7   staked $20.83  pnl -$6.58  ev/$1 -0.316

n=7 and contaminated -- five came from the sub-second duplicate bug fixed the
same day, one was a reversal `cross_allow_reversal` now blocks -- so it is not
proof. But there is no evidence on the other side either, and the mechanism is
adverse by construction: a repeat one second later buys the SAME bet after the
venue has repriced it. In the flagged case, 3.6 cents worse for an identical
position, and $6.41 of a ~$40 bankroll on one coin flip.

**Shipped `cross_max_per_round = 1`.** It costs no ROUNDS -- the arm still takes
every round it would have -- it only stops doubling inside one. That distinction
is what makes it an acceptable cut while the arm is short of frequency. Revert
to 5 if repeats ever measure positive on live fills.

A test-harness note: `test_a_same_side_repeat_is_untouched` in the whipsaw class
now raises `cross_max_per_round` explicitly. Left alone it would pass because of
the new cap, hiding a flip guard that had started refusing same-side entries.

## The cancelled orders were adverse selection, and the buffer sweep used the wrong substrate

Two instructions: disable the whipsaw guard if it fired, and find out why so many
orders cancel.

### The whipsaw guard fired four times and blocked three winners

    01:29:36  bnb-updown-15m-1789434900  blocked Down  winner Down  WOULD HAVE WON
    01:29:38  bnb-updown-15m-1789434900  blocked Up    winner Down  would have lost
    01:29:45  bnb-updown-15m-1789434900  blocked Down  winner Down  WOULD HAVE WON
    04:34:18  bnb-updown-5m-1789446600   blocked Up    winner Up    WOULD HAVE WON

`cross_flip_block_s = 0.0`. The class statistic it shipped on (flipped crossings
edge -0.0071 on n=14 vs +0.1771 clean) is not refuted, but it was scored on the
1s replay, where a "flip" is a different object from what the websocket clock
produces at ~114/s. Kept in the code behind 0 = OFF, with its tests, because the
hole it closes is real; re-enable only on live fills.

### The cancellations are not a fault. They are the arm losing its best trades

34 of the last 100 orders cancelled, nearly all with zero filled. One in full:

    04:39:12.029  CROSS btc Up sweep_px=0.620 (spot 77590.38 vs strike 77590.01)
    04:39:13.033  cross meter: +0.25s=0.620  +0.50s=0.660  +1.00s=0.730
    04:39:18.889  limit @0.67 unfilled after 6s -> cancelled | book now 0.8900

The venue repriced through the limit inside a second. The arm has been recording
this all along in its `cross meter` lines. Over 34 live triggers with a settled
round:

    reprice  +0.25s  median +0.1200  p90 +0.2800
             +1.00s  median +0.1490  p90 +0.3500

    buffer 0.125*(1-ask) vs that move:
      ask 0.0-0.4  buffer 0.088  median reprice +0.340  covered   0.0%
      ask 0.4-0.6  buffer 0.059  median reprice +0.140  covered  23.5%
      ask 0.6-0.8  buffer 0.044  median reprice +0.040  covered  60.0%

So the limit was covering about a quarter of the move it exists to chase, and
the selection that produces is brutal -- it is only hit when the crossing was
NOISE:

    FILLED  n=12  win 0.583  paid 0.622             EV/$1 -0.0572
    MISSED  n=22  win 0.818  (+1.00s book 0.685)    EV/$1 +0.2466

**The arm was filling the losers and missing the winners.**

### Why the afternoon's sweep said the opposite

`cross_limit_buffer_inv` was halved 0.25 -> 0.125 earlier the same day on a
`quotes_arm` sweep. This file already recorded why that cannot work: *"the 1s
replay is 6.8% cheap triggers while live is 28.6% (live runs the websocket at
~114/s and catches crossings BEFORE the venue reprices; the replay catches them
AFTER)"*. A substrate that only ever sees post-reprice asks cannot measure the
buffer needed to survive the reprice. Same error as `cross_max_per_round`, twice
in one day: **a knob measured on a replay that structurally cannot contain the
phenomenon.**

Simulated over the 34 live triggers, filling at our own limit (realised price
improvement is +0.0113, so that is the honest assumption), against the +1.00s book:

    inv    fills  win   EV/$1    TOTAL
    0.125     10    7  +0.0173   +0.173   was shipped
    0.200     12    9  +0.1332   +1.598
    0.300     19   15  +0.1404   +2.668   <- shipped now
    0.400     23   18  +0.0853   +1.963
    0.500     28   21  -0.0003   -0.009
    1.000     34   25  -0.2771   -9.420

A peak, not a range edge: too small and the good trades escape, too large and we
overpay for the noise ones. **Shipped `cross_limit_buffer_inv = 0.30`,
`cross_limit_buffer = 0.40`** (the old 0.25 cap would have clipped exactly the
cheap-ask cell where the reprice is largest).

### Latency is worth more than the buffer

The same 0.125 buffer judged against the +0.25s book returns **+5.447**, double
what the best buffer achieves at +1.00s:

    vs +0.25s book:  inv 0.125  16 fills  EV +0.3404  TOTAL +5.447

Cutting the decision-to-order round trip beats any value of this knob. That is
the next piece of work, and it is engineering rather than a constant.

n=34 (19 fills at the shipped cell) is thin. It is also the only substrate that
can see the effect at all.

## The cross arm is a MAKER by construction, and its fills are adversely selected

Told that latency and execution are already optimised. Verified, and it is true
on our side:

    decision -> order ON THE VENUE   n=50  median 0.208s  p90 0.233s  max 0.270s
    order created -> FILLED          n=26  median 1.187s  p90 4.004s  max 5.064s

208ms is ours; the remaining ~1.2s is venue-side matching. There is nothing to
win there. But that makes the cancellations incoherent -- until you read the
order path.

### There is no marketable limit on this venue

    live.py:440  "LIMIT must be GTC; FOK belongs to MARKET (schema, verified live)."
    live.py:558  "FOK is the only accepted value -- IOC and GTC are both rejected with -1102"

`buy_limit`'s own docstring: *"Post a resting LIMIT, wait `wait_s`, and cancel
whatever is left."* LIMIT is GTC-only. The arm has been resting, not taking.

### What that costs, measured on live orders

Pairing 100 venue orders against the arm's own `cross meter` lines (the book at
+0.25s after each trigger):

    matched orders with a +0.25s ask: 32
      limit AT/ABOVE the ask (should cross) and FILLED     :  5
      limit AT/ABOVE the ask (should cross) and NOT filled :  8
      limit BELOW the ask (genuinely not crossable)        : 19

Eight of thirteen marketable limits never took. And the split by outcome is the
whole story:

    marketable AND filled (the GTC limit happened to take)
      n=5  won 2/5  mean price 0.638  EV/$1 -0.2330  TOTAL -1.165
    marketable but NOT filled (a FOK would have taken)
      n=8  won 7/8  mean price 0.539  EV/$1 +0.6895  TOTAL +5.516

**When the resting limit gets hit it is picking losers; when it does not get hit
those are the winners.** A maker gets filled by someone who wants to sell to
them, and does not get filled when the price is about to run their way. Same
shape as the earlier FILLED/MISSED cut over all 34 triggers (-0.0572 vs +0.2466).

### Why `order_type = "limit_take"` was chosen, and what that reasoning missed

Recorded in config:

> MARKET prices off the venue's DISPLAYED price -- which lags its own book by up
> to 0.40 and left the quote above the ask in 5-7% of live samples. Market fills
> averaged +0.16 above the ask on offer; limit fills -0.014.

Both numbers are presumably right. The comparison is not: it judges the two
paths on fill PRICE and never asks what each is filling. **+0.16 above the ask
is almost exactly the reprice measured tonight (+0.149 median at +1.00s)** -- it
is the price of actually getting the trade that is running your way. The limit's
cheap -0.014 fills are the adversely-selected ones that win 2 of 5.

This is the same error as the depth-floor win-rate table and the buffer sweep:
**comparing an outcome across subsets without asking what selected them.** Third
instance in two days.

### Not shipped

Switching the cross arm to MARKET FOK is the obvious candidate and it is NOT
made here. n=8 on the decisive cell, it reverses a documented decision made on
real fills, and the operator's standing view is that execution is already tuned.
What is recorded is the defect: the limit path is a maker, its fills are
adversely selected, and the prior comparison that chose it could not have seen
that.

If it is tried, `order_type=market` in the VPS `.env` is the whole change, with
`slippage_bps=300` already bounding the take, and it is one line to revert.

## Sizing read wallet CASH, not equity, and the ledger drifted high for hours

Asked to un-hardcode the balance. First a correction: it was not hardcoded.
`setup` reads the venue and assigns `self.bankroll = bal`, and
`_refresh_bankroll` re-reads after every settlement, so `bankroll_usdt = 40.0`
in config is only a pre-connection placeholder. An earlier claim in this session
that "sizing is running ~5% conservative" was made by reading the config instead
of the runtime value, and was wrong.

The instruction was right anyway, for a different reason.

### What the logs showed

    00:25  bankroll read $45.50 is below the ledger $50.53 (1/5 reads)
    00:35  bankroll read $47.13 is below the ledger $52.78 (1/5 reads)
    00:45  bankroll read $45.86 is below the ledger $49.96 (1/5 reads)
    00:55  bankroll read $46.60 is below the ledger $53.18 (2/5 reads)
    01:00  bankroll read $46.30 is below the ledger $50.24 (3/5 reads)
    03:45  bankroll read $35.12 is below the ledger $44.66 (1/5 reads)

The ledger ran $4-10 ABOVE the venue for hours and never adopted the lower
figure: `_reconcile_bankroll` requires five consecutive low reads, and the
counter kept resetting. **The bot was sizing off inflated equity** -- the
opposite of conservative.

### The cause, and the fix

`LiveBroker.balance()` returned `walletBalance` -- cash only. That is the source
of the very lag `_reconcile_bankroll` was written to survive: a stake leaves the
wallet the moment an order fills, while the payout only returns after redemption
settles on-chain, so mid-flight the wallet under-states the account by the whole
position.

The venue publishes the missing pieces. Verified live 2026-09-15:

    totalValue 42.12746449
      = walletBalance 42.12746449 + positionValue 0 + totalClaimableAmount 0

`totalValue` is exactly that sum. New `LiveBroker.equity()` returns it, falling
back to the recomputed sum and then to `walletBalance` so a partial response can
never yield 0. Both bankroll call sites now use it.

That also retires a guard: `_refresh_bankroll` used to skip entirely while a
position was open, because cash alone would have under-stated the account. With
equity an open position is counted at its VALUE rather than looking like a hole,
so the refresh runs whenever a round settles. `_reconcile_bankroll`'s drift
machinery stays as a safety net -- but with the artefact gone, a persistent gap
is now far more likely to be real, which is the point.

The test that pinned the old guard (`test_refresh_is_skipped_while_a_position_is_open`)
asserted the bug. It is replaced by one asserting the new invariant, with the
fake returning DIFFERENT numbers from `balance()` (20.0) and `equity()` (76.0)
so that reading the wrong one fails loudly instead of passing plausibly.

567 tests pass. Deployed; startup read $42.1275.

## MARKET beats the resting LIMIT, and the ceiling is what makes it safe

Decided with numbers, over the 34 live cross triggers that have both a reprice
curve (`cross meter`) and a settled round. Fills priced at our own limit for the
LIMIT path; the MARKET path drawn from the eight recorded market fills, whose
slip against the ask on offer was `[+0.67, +0.21, and six at +0.067]` (the
FINDINGS mean of +0.16 is those eight, with the two impulse entries dominating).

    path                                fills   won   TOTAL EV   per fill
    LIMIT inv 0.125 (yesterday)            10     7     +0.173    +0.0173
    LIMIT inv 0.30 (this morning)          19    15     +2.668    +0.1404
    LIMIT inv 0.40                         23    18     +1.963    +0.0853
    MARKET FOK (observed slips)            34    25    +10.829    +0.3185

MARKET wins because it fills all 34 while the limit fills 19, and the misses are
disproportionately the winners. Sensitivity over the two things that could be
wrong -- how much MARKET pays, and how often FOK fails:

    market price assumption          FOK fail    TOTAL   vs LIMIT(+2.668)
    ask + observed slip                    0%   10.890   MARKET
    ask + observed slip                   50%    5.521   MARKET
    ask + 0.16 flat                        0%    7.064   MARKET
    ask + 0.16 flat                       50%    3.499   MARKET
    the +1.00s repriced book               0%    4.616   MARKET
    the +1.00s repriced book              50%    2.291   limit
    +1.00s book PLUS 0.067                 0%    0.801   limit
    +1.00s book PLUS 0.16                  0%   -2.206   limit

MARKET wins in 10 of 15 cells, including at a 50% FOK failure rate. It loses
only where the model charges it the fully repriced book AND another premium on
top -- which double-counts, since the +0.16 was measured against the ask at the
trigger, not against a book that had already moved.

### The ceiling is the part that matters

The case against MARKET was never the average, it was the tail. A market order
prices off the venue's QUOTE, and that quote follows the DISPLAYED price, which
lags its own book by up to 0.40 and sits above the ask in 5-7% of samples. The
recorded failure: 2026-09-11, a Down on offer at **0.17 was bought at 0.84** --
+47% EV became -70%. `slippageBps` cannot catch it, because it bounds movement
AFTER the quote, not the quote being wrong to begin with.

So `buy()` now takes `max_px` and refuses before any order exists. The cross arm
passes the same `ask + buffer` ceiling the LIMIT path used. That keeps MARKET's
immediacy and borrows the LIMIT path's one real advantage -- a bounded worst
case. It is a broker-wide contract: `base.buy` documents it and `paper.buy`
honours it, so a backtest cannot show fills the live path would refuse.

**Shipped**: `order_type=market` in the VPS `.env`, with the ceiling wired
through. 570 tests pass, three of them new and one exercising the real
`LiveBroker.buy` rather than a stub.

n=34 triggers, and the market slip distribution is 8 observations. Thin. It is
also the only substrate that can see any of this -- the 1s replay catches
crossings after the venue reprices, which is what made the two earlier buffer
conclusions wrong.

## The first live MARKET order failed, and it falsified the model I shipped on

05:09:13, `btc-updown-5m-1789448700`:

    CROSS Down sweep_px=0.460 (spot 77610.01 vs strike 77610.15, tau 47s)
    cross meter: +0.25s=0.600 +0.50s=0.650 +1.00s=0.650
    order 26091500001887549423  MARKET  status FAILED  price 0.37  filled 0
    NO FILL in 7.83s

The venue quoted **0.370** while the book was at 0.460 and running to 0.650.
Nothing is offered at 0.37 when the book is 0.46+, so the FOK was doomed before
it was sent.

**This is not the failure mode the decision modelled.** The simulation priced
MARKET as always filling at `ask + slip`; it does not. The displayed price the
quote follows lags the book in BOTH directions:

  * too HIGH -- 2026-09-11, a Down on offer at 0.17 bought at 0.84, +47% EV to
    -70%. `max_px` now refuses this before an order exists.
  * too LOW -- this order. The FOK cannot fill and the trigger is spent.

The mitigating fact is that a FAILED order costs **no money**: no fill, no fee.
The cost is the trade, which is the same cost a cancelled LIMIT carries.

### The fix, which dominates either path alone

`market_stale_quote_tol = 0.03` (0 = OFF). When the pre-trade quote comes back
more than 3c BELOW the book ask the arm already has, the order goes out as a
LIMIT instead. A LIMIT does not use the quote/bundle path at all -- it reaches
the real book -- so it is strictly the better instrument for exactly the quotes
that doom a FOK. In the case above, the limit would have gone out at 0.62
against a book at 0.60.

So the arm now picks per ORDER rather than per config:

    quote sound         -> MARKET FOK, capped by max_px
    quote stale-HIGH    -> refused by max_px, no order placed
    quote stale-LOW     -> LIMIT at ask + buffer, which reaches the book

That is MARKET's immediacy where the quote is usable, the LIMIT's book access
where it is not, and a bounded worst case in both.

573 tests pass, three new -- including one asserting a sound quote still takes
with a FOK, so the fallback cannot quietly turn the arm back into a maker.

### Honest status

n=1 on live MARKET orders. The decision to switch rested on a simulation whose
central assumption that first order disproved, and the fallback is a repair of
that model rather than a new measurement. What is solid: a failed FOK costs
nothing but the trade, and the arm can no longer pay a stale-high quote or waste
a trigger on a stale-low one.

## 2026-09-15: a losing day, and the knob that caused the last of it

Account $42.13 -> $36.13. Venue `todayRealizedPnl` **-$11.40, -22.53%** on
$50.61 of cost. The daily cap halted the arm at 06:35:29 and it re-derived the
halt on restart; nothing has traded since.

### What the last two fills were

    05:34  bnb  decided 0.6148  limit 0.73  FILLED 0.7236   ev_at_fill -0.1580
    06:34  btc  decided 0.5617  limit 0.69  FILLED 0.6889   ev_at_fill -0.1937

Both limits are exactly `ask + 0.30*(1-ask)` -- the buffer raised earlier the
same morning. **The arm's own ev_at_fill scored both as badly negative and
booked them anyway.** At the previous 0.125 the limits would have been 0.66 and
0.62, and neither would have filled.

### The number that should have governed this all along

All 43 settled cross fills, bucketed by the price actually PAID:

    fill 0.00-0.55  n= 5  won 0.400  break-even 0.496  edge -0.096
    fill 0.55-0.65  n=19  won 0.632  break-even 0.605  edge +0.026
    fill 0.65-0.75  n=13  won 0.538  break-even 0.691  edge -0.152
    fill 0.75-1.00  n= 6  won 0.833  break-even 0.835  edge -0.002

**The arm is profitable in exactly one band, 0.55-0.65.** Overall it wins
26/43 = 0.605, so a fill above ~0.60 cannot pay for itself. Every knob in this
file bounds the ASK -- `cross_min_price`, `cross_max_price`, the range gate --
and then a buffer adds to it, leaving the price PAID unbounded up to
`cross_max_price + buffer`. That is the hole the 0.30 buffer walked through.

**Shipped**: `cross_max_fill_px = 0.65` (0 = OFF), applied to BOTH paths -- it
caps the LIMIT ceiling and the MARKET `max_px`, so neither instrument can buy
outside the band. `cross_limit_buffer_inv` reverted 0.30 -> 0.125 and
`cross_limit_buffer` 0.40 -> 0.25.

### What went wrong in the reasoning, twice

Both of today's bad calls share a shape: a simulation was trusted past what its
substrate could support.

  * The **buffer raise** came from 34 live triggers, scored by assuming a fill
    at the limit is as good as the outcome distribution of the trades it would
    capture. It never asked what price those fills would land at. The answer was
    0.69-0.72, inside the losing bucket.
  * The **MARKET switch** modelled the path as always filling at `ask + slip`.
    Its first live order failed because the venue quoted BELOW the book, a mode
    the model did not contain at all.

The band table above needs no model. It is 43 real fills and their real
outcomes, and it contradicts both.

### Standing recommendation

The arm is marginal on its whole live record -- 26/43 at 0.605 -- and today cost
22.5% of the account. It is halted until the next UTC day by the loss cap. It
should stay halted until the 0.55-0.65 band is confirmed on fills taken UNDER
the new cap, rather than resumed because the clock rolled over.

## Fixing MARKET rather than abandoning it: the FOK never got its retries

Five of six MARKET orders failed after the 2026-09-15 switch. The reflex is to
revert; the record says the path was never actually given its three attempts.

    05:09:13  MARKET FAILED  btc  quote 0.370   "after 1 attempt(s)"
    05:34:22  MARKET FAILED  btc  quote 0.530
    05:34:25  MARKET FAILED  eth  quote 0.491
    05:34:29  MARKET FAILED  bnb  quote 0.702
    05:34:36  MARKET FILLED  bnb  quote 0.712   <- the RETRY of the line above
    06:19:15  MARKET FAILED  btc  quote 0.370
    06:34:11  LIMIT  FILLED  btc  quote 0.690   <- the stale-quote fallback

The one MARKET fill is a retry. A FOK that fails is retryable
(`venue_failed` is in RETRYABLE_FAILURES) -- but only once the venue SAYS it
failed.

### Why the retry never happened

`buy()` polled `order/history` for a terminal status with `tries=6, delay=0.6`
= 3.6s, gave up, checked the position list, found it empty, and returned
`"unconfirmed"` -- which is deliberately NOT retryable, because an empty
position list lags a fill and re-placing on it could double exposure. That
reasoning is right and stays.

The problem is the clock. Order `...549423`'s own `terminalTime` was **~8.2s
after createTime**, and its verdict was `FAILED`. We stopped asking 4.6 seconds
before the venue answered. The order died after one attempt for want of
patience, while its bnb sibling -- whose status did arrive in time -- was
retried and filled.

**Shipped** `order_status_wait_s = 9.0`. Waiting costs nothing when the order
filled (the poll returns early on any terminal status) and costs only time when
it did not -- and a failed FOK costs no money, just the trade.

### The consequence that had to be handled

Three attempts at up to 9s each spans ~30s, and the cross arm triggers with only
30-50s of round left. Unbounded-in-tau retries were harmless when an attempt was
quick; they are not now. **Shipped** `order_min_tau_s = 5.0` (0 = OFF, matching
`cross_min_tau_s`): no attempt starts into a round that is nearly over.

Four tests, including one asserting a healthy round still gets all three
attempts -- so the tau guard cannot quietly become a one-shot rule. They also
pin that only a CONFIRMED venue failure is retried; an unknown outcome never is.

### A note on the state this was found in

`order_type` was reading `limit_take` when this was checked. A revert command
that the operator rejected had nonetheless executed -- `.env.bak.1789455821`
(07:03:41) holds `order_type=market`, and a restart is logged at 07:03:42.
Restored to `market`. Worth knowing that a rejected command here still ran.

## Retry works everywhere; the price ceiling was paying a 30s tax to use it

Reported: an eth trade did not retry, possibly because retry is wired only to
btc. It is not -- `_place_order` is shared by every family -- and the log shows
the retry firing:

    07:29:06.126  order ...808625 status=FAILED (eth-updown-5m-1789457100)
    07:29:06.126  order attempt 1/3 failed for eth ... Down (venue_failed)
    07:29:06.989  order failed for eth ... Down (quote_above_ceiling); not retrying
    07:29:06.989  order failed ... after 2 attempt(s); pausing orders for 30s

Two attempts, not one. `order_status_wait_s = 9.0` did its job: attempt 1 got
the venue's own FAILED verdict, which classified as `venue_failed` and IS
retryable. The loop then ended because attempt 2's fresh quote came back above
the price ceiling -- the book had run 0.53 -> 0.67 in six seconds -- and the arm
correctly declined to chase it.

### The defect that line exposes

**`pausing orders for 30s`.** The order cooldown is SHARED across every arm and
family, and it exists for a venue that is rejecting orders, where retrying at
1 Hz would only hammer it. `quote_above_ceiling` is not that: the venue is
healthy and this bot is declining a price.

The file already carries the identical argument for `limit_unfilled`:

> A RESTING LIMIT THAT NOBODY CROSSED IS NOT THAT ... the cooldown is SHARED: an
> unfilled model-arm limit silently blocked the impulse and cross arms, which
> fire in windows only seconds wide.

So a refusal by `max_px` -- the guard added hours earlier specifically to stop
the arm overpaying -- was costing 30 seconds of trading across all six families
every time it worked. **The protection was throttling the thing it protects.**

Fixed: `quote_above_ceiling` joins `limit_unfilled` in the no-cooldown set.
Three tests, including one asserting a genuine `venue_failed` STILL pauses, so
the exemption cannot widen into "never cool down".

580 tests pass. Deployed.

## The FOK was unfillable by construction: `slippageBps` was a constant, not a bound

7 of 8 MARKET orders failed after the switch. The reflex reading is "market
orders do not work on this venue". The mechanism says otherwise.

`slippageBps` is the venue's tolerance for the book moving between the QUOTE and
the EXECUTION. It was frozen at the configured 300 (3%). This arm triggers
precisely when the book is about to gap:

    07:39:50  eth-updown-5m-1789457700  sweep_px 0.394
              cross meter: +0.25s=0.720  +0.50s=0.710  +1.00s=0.560
              MARKET FAILED

A quote of 0.3936 with 300bps authorises a fill up to **0.405**, against a book
that was at 0.720 a quarter-second later. The order was dead before it was sent.
Same shape at 07:29 (0.53 decided, book 0.67) and at 05:09 (0.37 quoted, book
0.46).

### The fix is not a bigger constant

`max_px` already states exactly how far this arm will go -- it is the same
`ask + buffer` ceiling the LIMIT path uses, capped by `cross_max_fill_px`. So
the tolerance is now DERIVED from it:

    slip = clamp(slippage_bps, (max_px / avg_px - 1) * 10_000, 9_000)

    quote 0.394, ceiling 0.47  ->  1928 bps   (was 300)
    quote 0.530, ceiling 0.59  ->  1132 bps   (was 300)
    quote 0.600, ceiling 0.65  ->   833 bps   (was 300)

The venue now enforces OUR ceiling: the FOK fills anywhere below it and fails
above. Too tight and a gapping book is unreachable; too loose and we overpay.
Deriving it is neither, and it cannot drift out of step with the ceiling the way
a second constant would. Floored at the configured value so ordinary jitter is
still covered, and clamped at 9000 (raised to the venue-documented maximum of
10_000 on 2026-09-15 -- see the fill-drought section below for why the venue's
own schema, not a probe, settled the number).

### What this does and does not fix

It converts every failure where the gap landed INSIDE the ceiling into a fill.
It does not rescue 07:39:50: that book went 0.394 -> 0.720, far past the 0.47
ceiling, so the FOK still fails -- correctly, because paying 0.72 on a 0.39
decision is the losing band. The arm should decline that, and now it declines it
by not filling rather than by an accident of a 3% constant.

Four tests: the tolerance equals the room to the ceiling, never drops below the
configured floor, is clamped at the top, and falls back to the plain default
when no ceiling is given.

### Operational note, second occurrence

`order_type` was found on `limit_take` twice while checking this, after revert
commands that the operator had REJECTED. Both times the rejection was reported
to the assistant and the command had run anyway. Restored to `market` both
times. Worth knowing when reading this file's timestamps.

587 tests pass. Deployed, `order_type=market`, $1 stake.

## "Retry doesn't work": the venue never saw the retries (2026-09-15)

The operator's report, checked against the venue: on BOTH failed triggers of
the day the venue holds exactly ONE order, and it failed:

    07:39:50  eth-updown-5m-1789457700  order ...29562  FAILED
    07:59:36  btc-updown-5m-1789458900  order ...67082  FAILED

...while the log says "attempt 1/3 failed" and then stops. Three defects; all
three fixed:

1. **A declined price ended the round.** Attempt 2's fresh quote landed above
   the ceiling (btc: 0.750 vs 0.630; the book had run), buy() refused --
   correctly -- and `quote_above_ceiling` hit the "not retrying" branch,
   abandoning the round. A decline is not a failed order: nothing reached the
   venue, and the next attempt re-quotes. The loop now continues (attempt
   N/M declined) instead of breaking -- see TestADeclinedPriceDoesNotEndTheRound.

2. **The retry floor blocked what the retry did not need.** The ~4.3s safety
   confirmation after a venue FAILED (position list -- kept: two real double
   buys, Sep 8) left the eth round at 3.3s to the bell, and the 5s floor
   refused the retry ("not re-attempting: floor is 5.0s"). That floor was
   calibrated for the 9s verdict budget, but an immediate FOK needs the round
   open only long enough to EXECUTE -- and the venue's book processes fills
   into the close (4 live fills booked with under 1s of round left; 5 under
   5s). Market orders now use min(order_min_tau_s, 3.0).

3. **The stale-low LIMIT fallback is removed** (operator directive: market
   only -- resting orders on this arm are a dead end). A FOK that cannot fill
   a correct-priced quote costs one failed attempt and no money; what can save
   the trade is a fresh quote, not a resting order.

Honest limit of the fix: in both observed episodes the book had ALREADY left
the band when the retry ran (eth 0.52-0.56 vs 0.47 ceiling; btc 0.73 vs
0.63), so a running retry still declines -- by design. What changes is that
every retry now RUNS, re-quotes and reports; when the book dips back inside
the band the retry buys it, and the venue shows a second order exactly when
one was possible.

NOT changed: the 4.3s position confirmation (load-bearing via the Sep 8
double buys), the ceiling itself, and the 30s cooldown on a genuine venue
failure. `market_stale_quote_tol` retired from config (env files ignore
unknown keys). 589 tests pass.

## Can we execute faster and catch that trade? (2026-09-15)

Measured, pairing the decision log with the venue's own order records:

    episode         decision -> venue accepted   venue verdict   outcome
    09:09 eth       186ms                         675ms           FOK failed
    09:09 btc       169ms                         755ms           FOK failed
    07:59 btc       179ms                         364ms           FOK failed
    07:39 eth       178ms                         324ms           FOK failed
    08:39 eth fill  ~220ms                        1558ms          FILLED 0.54

169-186ms breaks down as: the quote round-trip (40-65ms) + the order POST and
the venue's own intake (~120-130ms). Two more cold fires at 09:19 (still the
pre-warm build; the restart to it was 09:22:33) read 190ms and 220ms total --
the venue's intake runs 120-175ms, so EXPECT the warm total around
125-150ms, not a fixed number. Detection is already tick-resolution
(`_idle` runs `_cross_all` on every spot tick, ~114/s), and the venue's
intake is theirs. That left ONE shaveable leg: the signed quote fetched
between the decision and the order, while the book is jumping (eth 0.46 ->
0.63, btc 0.30 -> 0.66 by +250ms).

Shipped: the cross arm keeps a quote WARM for the side the NEXT crossing
would buy -- below the strike Up, above it Down -- refreshed at most 1/s while
the family sits in its tradeable window, consumed once by the first attempt
when younger than 2.5s and carrying the fire's exact stake (flat-or-cap
sizing makes that knowable: $1.00 today). Any doubt -- stale, stake mismatch,
fetch failure -- falls back to the synchronous fetch; retries always fetch
fresh, because their question is whether the book CAME BACK.

Honest expectation: 07:59 btc missed by ~20ms and 09:09 eth by ~45ms, inside
this saving -- those flip to toss-ups at best. The violent jumps stay lost:
09:09 btc's book crossed the 0.39 ceiling within ~100ms of the cross, faster
than any ~130ms pipeline can react from Tokyo. The remaining lever is not
latency: it is what the arm will PAY for a fast mover (the band), which is an
edge question with its own evidence bar.

CHECKPOINT: after ~20 fires, compare the venue order's `createTime` minus the
CROSS decision log against the 169-220ms cold baseline -- a warm hit should
print ~125-150ms. The log marks the hits (`warm quote used for <slug> <side>`),
and an unmarked fire that is slow means the fallback ran (window edge, stake
sized down, or no websocket book). 595 tests pass (6 new: warm hit skips the
fetch; stale and stake-mismatch fall back; imminent side chosen; one-shot
consumption; throttled refresh).

## The fill drought classified: venue-side timing, plus a slip cap below the venue's maximum (2026-09-15)

After the warm-quote deploy the client side is fast: 8 fires at 09:34/09:44,
decision -> venue-accepted 116-152ms. The drought continued anyway, and the
09:29-09:44 fires say why -- the binding constraint is now VENUE-side:

    09:44 eth15m  venue quote 0.11, ceiling 0.39. The venue's OWN book was
                  still 0.09-0.11 at +250ms (it had not jumped for this
                  family), our order was at the venue at +137ms -- and the
                  FOK still FAILED: the venue answered at +564ms, AFTER its
                  own book jumped 0.09 -> 0.90 (the +0.25/+0.50s meter).
                  Even the clamped bound (0.11 x 1.9 = 0.209) was satisfied
                  while we were there; the venue's own execution latency is
                  what lost it.
                  >= CORRECTED same day, see "The timing lever, measured":
                  that meter belonged to the REFUSAL ~290ms EARLIER; the
                  fire's own run was 0.30 at the decision and 0.90 by
                  +215ms. The door had already shut.
    09:44 bnb     quote 0.10, book 0.66 by +250ms. Doomed at ANY latency.
    08:39 eth     the counter-example -- the fill. Book stayed put; the
                  venue executed at 0.5405 (~its ceiling) 1.5s later.

So today's crossings reprice inside ~250-500ms while the venue's own FOK
pipeline answers ~300-600ms after intake: the undershoot window is shorter
than the venue's own execution lag. Client-side speed is no longer the
bottleneck -- 0ms would not flip the fast cases.

BUT the audit also caught a REAL machinery limiter: the derived-slip CLAMP
(9_000bps). For extreme undershoots -- the arm's best cases -- the venue's
quote is far below our ceiling, and slip = clamp(room, 9000) bounds the
authorized fill at quote x 1.9, NOT at the ceiling:

    09:44 eth5m  quote 0.03, ceiling 0.64 -> authorized <= 0.057 (not 0.64)
    09:44 btc    quote 0.07, ceiling 0.31 -> authorized <= 0.133 (not 0.31)

RESOLVED the same day, from the venue's own schema: the docs pages serve an
OpenAPI spec (saved as slippagebps/ in the repo), and it types
`slippageBps` on BOTH trade/get-quote and trade/place-order-bundle as an
integer, "Range 1-10000". 10_000 is the most ANY client can ask for -- no
probe could have lifted that, and a static scan of 12 live tokens had
already found no safe window anyway (at rest the venue's quote sits AT its
book). Under the quote x (1 + slip) bound the authorised fill tops out at
2x the quote, which re-reads the table above: eth5m needed ~203_333bps,
eth15m 25_454, btc 34_285 -- all BEYOND the venue's own maximum. Those
fills were unreachable for every participant at any latency; the
self-inflicted part was only ever the 9_000-10_000 slice (authorising less
than the venue permits). Fix shipped: the derived cap moves 9_000 ->
10_000, the documented maximum -- small, correct, and honest about being
small. The venue-side timing conclusion above stands.

Minor, same audit: bnb15m fired at sweep 0.79 on 09:29 (cap
expected 0.82) but the market path caps at cross_max_fill_px=0.65, so all
three attempts were declined client-side. A fire whose ask is already above
the cap can never place; the exec summary's "limit 0.82" hides that.
Consider gating the fire on ask <= cross_max_fill_px (or at least logging).

### The timing lever, measured -- and it is not the lever (2026-09-15 PM)

Pulled the venue's own order records (order/history) and lined them up with
the decisions table (ts_ms = the method's first timestamp, before every
await). Seventeen fires have both sides; the chain is strikingly stable:

    decision -> venue createTime     +171..+246ms   (median ~185)
      * the venue-position read on the fire path is 52-69ms of it
        (20 live samples of position/list)
      * the rest is task + warm-quote + POST + the venue's own stamping
    venue create -> terminal (FOK)   260..1237ms   (fails cluster ~425)
    ------------------------------------------------------------------
    decision -> the venue's verdict   ~610ms

The runs finish far sooner. With the meter anchored correctly (see the
instrumentation note below), the 09:44 eth-15m race reads 0.09 -> 0.30 ->
0.90 within ~215ms of the decision. THE FIRE NEVER HAD A DOOR: even a 0ms
client still needs the venue's own ~425ms match, and every fire that day
had left its bound by then. Classifying all of the day's market orders by
what the venue recorded:

  * quote undershoot beyond ANY slippage: btc-5m 09:44 (ref 0.07 vs a 0.21
    book), eth-5m 09:44 (0.03 vs 0.59) -- ref x 2.0 still short
  * run past the bound mid-match: the 09:29 batch, bnb-5m 09:54 (book 0.85
    vs bound 0.59), btc-15m 09:59 (0.34 vs new-max bound 0.32; the old
    9_000 bound was 0.304 -- the clamp raise helped and still fell short)
  * timing-only: NONE. Every miss closes at <=600ms against a ~610ms
    verdict. The 57ms position read is the only client lever left, and the
    band where 57ms flips a fire (door closes inside the last 57ms before
    the match) is empty in the data -- so it was NOT removed. It guards
    double buys (Sep 8); the evidence does not pay for weakening it.

Two more things the venue records settled, both killing a standing theory:

  * "MARKET orders never fill on this venue" and the schema's ~1.5 USDT
    MARKET minimum do NOT block us: at 10:29:15 order ...162602 FILLED,
    type MARKET, $1.00 -> 2.23 shares (~0.448), on the RETRY -- after
    attempt ...162199 (ref 0.50) failed in 406ms, the whipsaw brought the
    price back under the ceiling and the fresh quote (0.44) did. The
    machinery converted a venue-fail into a cheap fill; the retry fix
    doing exactly its job.
  * the venue anchors the FOK bound to OUR submitted quote: the order
    record's `price` equals the quote's averagePrice to 15 digits (e.g.
    0.4518220214124349 on both the decision row and the order). The fires
    show refs lagging the book during storms (0.03 vs 0.59). A fresher
    quote at fire time might lift the bound -- now instrumented (shadow
    quote) instead of guessed.

Instrumentation shipped (all log-only): `cross fine` -- 50ms book marks
across the +100..600ms window (the `cross meter` line kept verbatim for
tools/cross_catch_report.py); fire/refusal tags on the meter with per-kind
dedup (the bug that mis-anchored the 09:44 read: a fire's meter was deduped
against an earlier refusal's, ~290ms off); the warm-quote marker now carries
age+px; a SHADOW fresh quote fetched concurrently with each cross order
(answers "would a current reference have been accepted?" without touching
the order); and one `venue ack` line per attempt with quote, slip, bound,
ceiling and ms.

## Precursor signals, redone at tick level (2026-09-15)

The earlier "1s tape / 1s momentum" falsifications could not have seen a
precursor to a 250-600ms race: they fed the model 1-second klines and
labelled 5-minute settlements. Redone from scratch on the µs feeds already
on disk (`data/bookticker/*.csv.gz` = Tardis top-of-book; `data/aggTrades/*`
= trades with taker side), tool `tools/precursor_ticks.py`, day 2026-09-01
(the one day with both feeds), 8h per symbol, 100ms grid, features strictly
causal (sampled BEFORE the event that crosses the grid), two targets (quote
mid and LAST TRADE price), and -- the decisive design -- a NON-OVERLAPPING
event test: one label per mid tick, with the book state 100/300/800ms before
it.

WHAT REPLICATES (BTC / ETH / BNB):

  * the next mid TICK is highly predictable from top-of-book imbalance:
    baseline P(up) 50.3 / 53.5 / 49.4%;
        qi > +0.65 ->  88.8 / 92.6 / 90.3%  (at 100ms lookback)
                       85.2 / 88.8 / 88.6%  (300ms)
                       82.3 / 84.3 / 86.0%  (800ms)
        qi < -0.65 ->   9.0 /  8.9 /  9.9%  (mirror, 100ms)
    Three symbols, stable across halves -- this is the real queue-imbalance
    effect, and it is far stronger than anything measured before at 1s.

  * the DRIFT accumulates: P(mid moves >= +0.5bps within 1s) goes from
    3.4 / 5.1 / 3.2% (base) to 8.3 / 13.7 / 8.5% when qi > +0.65, and the
    same lift appears on the TRADE-price target (8.3 / 13.7 / 8.5%).
    Composite (qi>+0.65 AND CVD>0 AND dCVD>0): 10.8 / 19.6 / 11.2%.
    Split-half stable. IC(qi -> forward return) +0.22..+0.30.

WHAT DOES NOT (the part that matters for our fills):

  * BIG single-tick moves -- the >=0.5bps jumps that actually cross strikes
    -- are a COIN FLIP in direction: P(up) 55.1 / 44.4 / 49.5%, n=574/1899/
    1197. Imbalance predicts the mechanical next tick, not the jump.
  * the flow features are weak on their own: cvd500 IC +0.07/+0.05/+0.03,
    absorption +0.014/+0.032/+0.038, and the composite adds little over qi
    alone. tn500 (trade count) is noise.
  * TWO of Gemini's ideas collapse before testing: (a) at top-of-book the
    micro-price deviation is an IDENTITY -- micro-mid = -(spread/2)*qi, so
    (micro-mid)/spread == -qi/2 exactly; it is one signal, not two (5-level
    depth would separate them; this archive is top-1 only). (b) the venue
    accepts FOK for MARKET and GTC for LIMIT ONLY -- IOC is rejected -1102
    (verified live), so "switch to IOC + buffer" is inapplicable; the
    cushion already exists as our ceiling/slippage derivation.

VERDICT: the precursor exists but it is the WRONG CLASS. It front-runs
1-tick queue mechanics (88-93% direction) and a slow drift (2.4-2.7x lift on
>=0.5bps over 1-5s, visible in prints), while the violent jump that kills our
FOKs (0.09 -> 0.90 inside 250-500ms) stays a coin flip -- and its size is
past any slippage the venue accepts anyway (see the fill-drought section).
Gemini's lead-lag idea is untestable here (no free Binance FUTURES archive;
spot bookTicker stopped 2024-03 per the tape section) and is second-order
next to this.

CORRECTION (2026-09-15 PM): the parenthetical above is WRONG. The futures
archive DOES exist and is free -- `data.binance.vision/data/futures/um/daily/`
serves aggTrades, trades, bookTicker, bookDepth, klines, markPriceKlines,
indexPriceKlines, premiumIndexKlines and metrics, and returned HTTP 200 for the
exact days we hold venue quotes. The lead-lag question was testable all along
and is tested in the "four structural catalyst ideas" section below.
`liquidationSnapshot` is the one data type that is NOT there (its bucket listing
is empty), so the liquidation idea has no history to join against.

DATA GAP for the money test. The strong signal needs bookTicker, which the
archive has only for 2026-08-01 and 2026-09-01; the venue's own books (live
DB) span 09-12..09-15; the Polymarket up/down recordings (data/hf/*.parquet,
captured 08-21..08-24) overlap only aggTrades. No join is possible today.
The fix is cheap and local: record Binance bookTicker + aggTrade for BTC/ETH/
BNB on the VPS (the bot already holds the spot ws) and join against the
venue's own recorded quotes -- that is what would settle whether a qi-triggered
EARLY entry beats waiting for the crossing.

## The money question, decided: a pre-cross entry does NOT pay (2026-09-15)

Asked to settle it now rather than wait for the tape archive: "we already have
all data ... i want it decided now, experiment, if positive implement it and
ship it." So the join was done with what exists -- the venue's OWN quotes from
the live DB (09-08..09-10, 964 rounds with a recorded winner) priced against
Binance aggTrades for the flow features. Tool `tools/precursor_entry.py`,
read-only, committed. Entry price = the first venue quote at/after t+lag (so
latency is charged, not assumed away), settle = the recorded winner, EV/$1 =
(1-p)/p if it wins else -1, and the `EV_net` column subtracts the round's
recorded `fee_rate_bps`.

RULES. CROSS = the shipped idea (first quote past the strike). PRE = |spot -
strike| <= gap bps toward the crossing side AND CVD500 > 0 AND CVD500
accelerating. PRE_MOM = PRE + 1s spot momentum agreeing. MOM_ONLY = momentum
alone. PRE_INV = the OPPOSITE side at the same entries (the honest control:
same times, same prices, other direction). PRE_COIN = a random side at the
same times.

LATENCY IS HALF THE "EDGE":

  entry lag      PRE_MOM gross    net     CROSS gross
  0 ms (cheat)        +0.074     +0.054      -0.037
  1000 ms             +0.027     +0.007      -0.056
  2000 ms             +0.026     +0.006      -0.057

The first run -- same-tick pricing -- read +0.054 net and looked shippable.
Charging a realistic 1s (quote cadence is ~1Hz) removed two-thirds of it.
Any precursor rule pays a latency tax by construction; here it eats the edge.

IT ALSO DOES NOT SURVIVE THE KNOBS:

  gap (lag 1s)   PRE_MOM net    n
  2 bps             -0.130      194
  3 bps             +0.007      226
  5 bps             -0.012      216
  3 bps, mom>=1bp   +0.143      112   <- only positive survivor, n=112

A point estimate that flips sign between 2 and 5 bps of the gap is not a
mechanism, it is a fitted window. The one good cell (momentum >= 1bp) is the
smallest sample in the matrix, which is what overfitting looks like.

THE CONTROL KILLS IT. Same entries, opposite side:

  PRE_MOM minus OPPOSITE-SIDE: +0.0734/$1  se 0.1351  t +0.54  (n=226)

t = 0.54. Directional skill is not distinguishable from zero. And day by day
the control BEATS the signal on two of three days:

  day       PRE_MOM              PRE_INV (opposite side)
  09-08     -0.041 (58)          +0.024 (58)
  09-09     +0.100 (135)         -0.112 (135)
  09-10     -0.153 (33)          +0.097 (33)

The entire positive total is one day, 09-09, and on that day the opposite
side is the loser -- i.e. the "signal" picked the winning side about as often
as a coin would, and the sign of the result is set by which way that day
broke. Flow-only PRE is negative (-0.099) and MOM_ONLY is strongly negative
(-0.197): momentum alone is a loser, and the flow+accel+mom combination is
where the fitting happened. PRE_COIN at -0.111 confirms that entering the
cheap side mid-round with no signal loses the fee + spread, as it should.

VERDICT: DO NOT SHIP. The pre-cross logic is not a shippable edge -- not
because it was measured badly, but because when it is priced honestly (1s
latency), given a proper opposite-side control, and asked to hold across a
+/-1bp parameter change, it returns t = 0.54 with a sign that depends on one
day of three. Shipping it would mean adding a second entry path (new trigger,
new reason code, its own cap) on a point estimate inside its own error bar --
for a mechanism whose own control is indistinguishable from it. The
engineering and the risk are certain; the edge is not.

Two realism caveats, both of which would have to be resolved before any
pre-cross rule could be re-tested -- and neither of which rescues this one:
(a) fills are priced off the DB's DISPLAYED price (up_ask/down_ask are mostly
NULL), which is a mid-like number; the real fill pays the spread, so both the
signal and its control are optimistic by roughly the same amount and the
PAIRED test -- the one that decides -- is unaffected. (b) the CROSS column
here is the UNFILTERED crossing rule (-0.076 net); the shipped arm's gates
(range, depth, buffer, tau) refuse those losers and run near break-even, so
the correct yardstick for anything new is the gated arm, not this proxy -- and
+0.007 net at t=0.54 does not clear it.

WHAT WOULD CHANGE THE ANSWER is still the same missing dataset: recorded
top-of-book for BTC/ETH/BNB contemporaneous with the venue's quotes, so the
qi-triggered entry can be tested on the signal that actually replicated
(88-93% next-tick direction, 2.4-2.7x drift lift) instead of on aggTrade flow
alone. That recording is cheap, local, and now the only thing standing between
the tick-level result and a money answer.

### Addendum: I ran it against the WRONG DB, and the real one kills it (2026-09-15)

Asked "would there be any difference if you test on the vps?" -- the answer is
yes, and it is decisive. The run above used `data/btcpredict.db`, a Sep-13
snapshot that is both STALE and FILTERED. Census of the two:

  local snapshot   964 rounds with a winner, days 09-08..09-10, 85,708 quotes
  VPS live DB    5,556 rounds with a winner, days 09-08..09-15, 1,232,941 quotes

So the money test above saw 17% of the rounds that exist, and only 3 of 8 days.
Worse, the local copy is not merely old -- within the SAME days it holds fewer
rounds than the venue recorded (09-08: 236 of 708; 09-10: 161 of 479; 09-09 is
the only day that matches at 567). That subset was itself a favourable slice.

Method note: run it locally anyway, with the live DB pulled down
(`sqlite3 .backup` on the VPS -> rsync -> `--db data/btcpredict_vps.db`). The
aggTrades archive only exists on this box, and it stops at 09-12, so the flow
rules cannot reach 09-13..09-15 (52% of the rounds) wherever they are run.
Added `--db` and `--days` to the tool; the by-day breakdown is no longer
hardcoded to three days.

SAME THREE DAYS, on the venue's full record (1,754 rounds vs 964):

  rule        n    mean_px   win    EV_net
  CROSS     907     0.580   0.552   -0.059
  PRE_MOM   320     0.432   0.431   -0.003   <- was +0.007 on the 964-round slice
  PRE_INV   320     0.581   0.569   -0.031   <- opposite side, same entries

So even inside 09-08..09-10 the extra rounds take the +0.007 to -0.003. The
sign was a slice artefact, not a day artefact.

ALL FIVE DAYS WITH aggTrades (09-08..09-12, 2,606 rounds):

  rule         n    mean_px   win    EV_net
  CROSS     3279     0.571   0.583   +0.125
  PRE_MOM    447     0.415   0.383   -0.026
  PRE_INV    447     0.596   0.617   +0.037
  MOM_ONLY 12347     0.419   0.423   +0.148
  PRE_COIN  1675     0.503   0.473   -0.069

  PRE_MOM minus OPPOSITE-SIDE: -0.0625/$1   se 0.1089   t -0.57   (n=447)

The control now BEATS the signal, and the paired difference is negative. The
t-statistic did not shrink toward zero on 2x the data -- it changed sign.

The by-day table is the honest picture, and it is not a signal:

  PRE_MOM  -0.041(58)  +0.100(135)  -0.046(127)  -0.489(66)  +0.400(61)
  PRE_INV  +0.024(58)  -0.112(135)  +0.080(127)  +0.393(66)  +0.050(61)

On 09-11 the signal loses 0.489/$1 while the opposite side of the same entries
makes 0.393 ($0.88 swing); on 09-12 they reverse (+0.400 / +0.050). The control
wins on 3 of 5 days. Day-level dispersion of +/-0.4-$0.5 per $1 on n~60 swamps
anything the rule produces -- which is exactly the condition under which a
3-day subset can read either sign.

(For the record, the CROSS aggregates here are inflated by one day: 09-12 reads
+0.318 on n=364, and BNB reads +0.819 on n=70. The unfiltered crossing rule is
still not the shipped arm; the gated arm remains the right yardstick.)

VERDICT UNCHANGED AND NOW STRONGER: DO NOT SHIP. The pre-cross entry is a
negative-expectancy rule whose opposite side is positive on more days than it
is, and the earlier positive reading was an artefact of testing on a stale,
filtered 3-day local copy. The lesson is about method, not about the signal:
CHECK THE COVERAGE OF THE DB BEFORE RUNNING A MONEY TEST -- a local snapshot is
a biased sample unless proven otherwise. A 17% slice produced a +0.007 that the
full record turns into -0.026.

## The four "structural catalyst" ideas, classified and (two of them) tested
(2026-09-15)

The proposal: top-of-book queue imbalance only predicts the 1-tick flicker, so
stop modelling micro-ticks and trigger on the things that force SUSTAINED
sweeps -- (1) futures liquidation cascades via `!forceOrder@arr`, (2) L2 "air
pockets" across +/-5bps, (3) trade-arrival velocity / Hawkes clustering, (4)
perp-to-prediction lead-lag. Target a 3-10s drift instead of a 1s impulse,
because our ~1s decision-to-fill cannot front-run a 1s event.

The latency logic is right and it is the same conclusion the timing work
reached. But "testable" is decided by data, so each idea was checked against
what exists:

  idea                    data today?                         verdict
  (4) perp lead-lag       YES - futures aggTrades, free      TESTED below
  (3) arrival velocity    YES - aggTrades already on disk    TESTED below
  (2) L2 air pockets      NO - needs live depth recording    NOT testable
  (1) liquidation cascade NO - needs live recording          NOT testable

Why (2) is not testable: the free `bookDepth` archive is **percentage bands**
(-5,-4,-3,-2,-1,-0.2,+0.2,+1,+2,+3,+4,+5 %) at a ~30s cadence (34,561 rows/day
= 2,880 snapshots = one every 30s). The finest band is 0.2% = **20 bps**, and
the idea needs +/-5 bps resolution -- 4x too coarse, and 30s instead of 100ms.
The archive cannot answer it at any sample size.

Why (1) is not testable: `data/futures/um/daily/liquidationSnapshot/` does not
exist (empty bucket listing), so there is no historical liquidation tape, and
the live stream would have to be recorded from now. Unverified caveat, flagged
rather than assumed: Binance documents the forceOrder stream as pushing at most
one liquidation per symbol per 1000ms, which would make "aggregate liquidation
volume inside a 500ms window" unmeasurable as described. The docs page is
behind a WAF I could not get past, so this stays a to-check, not a finding.

THE TWO THAT COULD BE TESTED. Perp tape pulled for BTC/ETH/BNB 09-08..09-12
(15 files, 146MB, `data/perp/aggTrades/`), joined to the same 2,606 venue
rounds, same honest +1000ms entry pricing, each rule paired against the
OPPOSITE SIDE at its own entries:

  rule       n    mean_px   win     EV/$1   EV_net   day-t
  CROSS   1495     0.583   0.579   +0.052   +0.032   +0.31
  PRE     1675     0.408   0.373   -0.044   -0.064   -1.62
  PRE_MOM  447     0.415   0.383   -0.006   -0.026   -0.10
  PRE_INV  447     0.596   0.617   +0.057   +0.037   +1.04
  PERP     953     0.429   0.407   +0.067   +0.047   +0.54
  PERP_INV 953     0.583   0.593   +0.036   +0.016   +1.80
  VEL     1110     0.417   0.391   -0.007   -0.027   -0.51
  VEL_INV 1110     0.595   0.609   +0.048   +0.028   +6.79
  MOM_ONLY1649     0.418   0.363   -0.111   -0.131   -0.70
  PRE_COIN1675     0.503   0.473   -0.049   -0.069   -1.11

PERP = trailing perp displacement >= 1bps (1s window) in the direction needed;
VEL = trades in the last 500ms >= 3x the trailing-10s rate, with the surge CVD
taking that side.

Paired against their own opposite side:

  PERP minus OPPOSITE-SIDE: +0.0317/$1   se 0.0815   t +0.39   (n=953)
  VEL  minus OPPOSITE-SIDE: -0.0549/$1   se 0.0697   t -0.79   (n=1110)
  PRE_MOM minus OPPOSITE-SIDE: -0.0625/$1  se 0.1089  t -0.57  (n=447)

So neither new idea has directional content. Note the give-away that is worse
than the t-stat: for PERP, BOTH sides of the same entries are positive
(+0.047 net vs +0.016 net). A rule that makes money on its own side and on the
other side is not detecting a direction, it is selecting an entry moment.

SENSITIVITY (perp threshold / window, velocity multiple) -- 7 cells, every one
|t| <= 1.5, and the sign moves with the knob: perp 0.5bps +0.18, 2bps +0.15,
5bps +1.49 (n=27, the smallest cell, EV +2.20 -- the familiar "smallest n
looks best" artefact), 500ms window +0.03, 2000ms window -0.24, vel 5x +0.23,
vel 8x +0.66.

### THE ACTUAL REASON ALL OF THIS KEEPS ALMOST-WORKING: THE RULES ARE THE DAY

Per-day EV for the six rules:

  rule       09-08    09-09    09-10    09-11    09-12    mean
  CROSS     -0.149   -0.033   +0.002   -0.015   +0.318   +0.025
  PRE       -0.022   -0.098   +0.008   -0.153   +0.008   -0.051
  PRE_MOM   -0.041   +0.100   -0.046   -0.489   +0.400   -0.015
  PERP      -0.031   +0.025   -0.034   -0.156   +0.509   +0.063
  VEL       -0.084   -0.053   -0.053   -0.063   +0.146   -0.021
  MOM_ONLY  -0.091   -0.212   -0.129   -0.157   +0.280   -0.062

  09-08: 0/6 positive.  09-11: 0/6 positive.  09-12: 6/6 positive.
  Pairwise correlation of the six DAILY EV series: median r +0.73
  (range +0.36..+0.97, 15 pairs).

A perp lead-lag rule, a trade-velocity rule, a momentum rule, a CVD rule and
the shipped crossing rule are 73%-correlated with each other day to day. They
are not six signals; they are six measurements of one day factor, and on
09-12 -- a trending day where every cheap-side entry won -- all six are
positive. Any rule's 5-day mean is therefore mostly the question "how many
09-12s did my sample contain", and with 5 days the effective sample size for a
directional test is ~5, not 953-1675. That is why the honest statistic is the
day-clustered t printed above, and why every one of them sits inside +/-1.8.

One line in that table deserves an explicit warning rather than an invitation:
VEL_INV (the OPPOSITE side of the velocity signal) reads +0.028 net with
day-t +6.79. It is not a discovery. It is the favourite-side base rate -- buy
the 0.595 side, win 60.9% of the time, collect ~1.4pp -- measured at the DISPLAYED
price, and the displayed price is the one we established is not executable
(`up_ask`/`down_ask` are NULL on these days, so `price_of` falls back to the
mid-like `up_price`). Its paired test, which cancels that pricing error, is
VEL t = -0.79. Before believing any favourite-side level effect, the ask column
has to be populated; until then it is the pricing-assumption trap again.

VERDICT ON THE FOUR IDEAS: (3) and (4) were testable and both fail -- no
directional skill, flat across parameter sweeps, and their apparent EV is the
day factor that all our rules share. (1) and (2) remain genuinely untested,
because the data they need does not exist historically: depth at 100ms and a
liquidation tape. They are also the two that would answer the question the
tick-level work actually raised, so the productive move is not another
backtest on five days -- it is to start recording `depth20@100ms` and
`!forceOrder@arr` on the VPS now, and re-ask in a week with more days than five.
Five days is the binding constraint, not the choice of feature.

## The five "macro / structural" ideas, measured (2026-09-15)

The pitch: since a 1s execution delay destroys sub-second alpha, trade horizons
that make the delay irrelevant -- TWAP-oracle last-mile, fading the
lottery-ticket bias, a volatility regime gate, becoming a maker, macro
releases. Every one of these is speed-independent, so unlike the microstructure
list these can be settled on data we already hold. Tool:
`tools/venue_late_odds.py` (read-only, committed). Four of the five are now
answered.

### 1. "Deterministic TWAP oracle lock" -- DEAD. Settlement is a POINT SNAPSHOT.

The whole idea requires that the venue average a window, so the winner is
locked before the bell. It does not. Joining 3,622 rounds' `end_price` to
Binance 1s bars:

    |end_price - Binance CLOSE at end|   median  0.02 bps   (mean 0.30)
    |end_price - 30s MEAN of Binance|    median  0.44 bps
    |end_price - 60s MEAN of Binance|    median  0.85 bps
    signed (Binance close - end_price):  median +0.00 bps

The instantaneous close fits to 0.02 bps -- two cents on a 77,000 print -- and
the signed lag is exactly zero. A 30s or 60s average fits *worse*. So the
settle is a point value read at the bell, `rounds.feed_provider` is recorded as
CHAINLINK (three feed ids, one per symbol) but the value it stores tracks
Binance's instantaneous price. There is no averaging window, therefore nothing
becomes "mathematically locked" 8-12s early, therefore there is no stale
0.88-0.94 winner to buy. The 15-bps-above-strike example in the pitch describes
a market this venue does not run.

### 2. "Fade the lottery-ticket bias" -- DEAD on the fee, and DEAD on the price.

Two halves, two different answers. First the fee half, which is checkable
against money we actually paid (801 fills):

    realized fee = 95.9 bps MEAN / 88.4 bps MEDIAN of stake
    aggregate: $18.378 fee on $1,850.63 staked = 0.993%

The claim that fees drop near zero at 0.90+ (Polymarket's p(1-p) schedule) is
FALSE here: `fills.fee_paid/stake` is flat across the price range. The 200 bps
in `quotes.fee_rate_bps` is a pre-trade ESTIMATE the arm uses, not what is
charged. Any idea must clear ~96 bps, not 200, and not ~0.

Second half, measured on one row per round (the last quote with tau 5-15s,
n=4,997 -- 88% of them sit in tau 5-10s, so this really is the last-mile
window):

    buy the side priced >= 0.85:  n=3714  mean_px 0.969  realized 0.9620  EV -0.0077
    buy the side priced >= 0.90:  n=3398  mean_px 0.977  realized 0.9788  EV +0.0015
    buy the side priced >= 0.95:  n=3015  mean_px 0.984  realized 0.9847  EV +0.0008

The late favourite is priced to within 0.2pp of its realized frequency. Gross
EV is +0.15% at >=0.90 and, after the real 96 bps fee, it is **-0.81%**. There
is no lottery-ticket bias to fade at this venue -- the bias the idea describes
is real on Polymarket and absent here.

### The trap I walked into, and the check that caught it

Section 3 of the tool bins the SAME late quotes by `|spot - strike|` instead of
by price -- an independent variable, since it does not use the venue's odds.
It reproduces the sane monotone relation (realized P(favourite wins) 0.708 at
|dist|<0.5bps rising to 0.998 past 5 bps) and then reports:

    spot-strike   n     realized  priced   gap     EV_fav
    -99..-5    1203      0.998    0.848   +0.150   +1.194   <- 119% per $1

A +119%/$1 "edge" on 1,203 trades is not a discovery, it is a pricing error, and
it is the one this file already documents at the top: **`up_price` is the stale
displayed value, not an ask -- "beating a price you cannot trade at is not
evidence."** The venue's own recorded book settles it. For |dist| >= 2 bps with
a recorded bid AND ask (680 of 3,463 rows), the favourite side reads:

    mean DISPLAYED price 0.8007   mean bid 0.9143   mean ask 0.9708

The bid is ABOVE the displayed price -- the "price" column is not inside its own
book on average, and only 47.2% of those rows are coherent (bid <= price <= ask).
In that coherent subset the trade the idea describes is:

    n=321   priced 0.9526   ASK 0.9738   realized 0.9564
      EV/$1 at the DISPLAYED price: +0.0030   after fee: -0.0066
      EV/$1 at the ASK            : -0.0221   after fee: -0.0317

Realized 0.956 vs an ask of 0.974. The 8-15pp "gap" in section 3 was the display
column understating the favourite by ~11 cents. Note this is the SAME artefact
that made VEL_INV's mirror read +0.028 at day-t +6.79 and that flipped every
early rule's mirror positive -- it is one bug, not four findings.

### 3. Regime gate -- the mechanism is real (it is the day factor), the edge is not.

The proposal to only trade when the market is trending describes exactly what
was measured earlier today: six unrelated rules with median pairwise daily-EV
correlation +0.73, 6/6 positive on 09-12, 0/6 on 09-08 and 09-11. A gate can
stop trading the chop; it cannot manufacture an edge in a market whose
executable price is calibrated. Worth noting the arm ALREADY has the lever,
pointing the other way: `cross_max_move_bps` (7.8-21 bps) makes it REFUSE when
the round has already moved, and the pitch's gate ("only trade if spot has
displaced >= 4 bps by minute 2") is the opposite instruction. Consistent with
the depth-gate measurement: our money is in the venues' quote lag at the
crossing, not in the size of the move.

### 4. Maker rather than taker -- the one genuinely open item, already partly tested.

The venue does support GTC LIMIT (verified live; MARKET is FOK, IOC is rejected
-1102), and there IS a spread to capture: on the same late rows the
favourite-side spread is median 2.0 cents (p05 1.0c, p95 23c). That is a real
2% gross, which is more than any signal measured today. But as a maker the leg
that fills is the leg moving against you, and the repo has already built and
scored this: `stale_maker` is recorded flat (n=24, +1.4 pts, 0.1 SE) and the
2018-era note "the maker arm ... all flat" stands. What has NOT been done is a
maker sweep on the current 8-day record with maker/taker fills separated. That
is a real, bounded experiment and it is the only item on this list I would
spend time on.

### 5. Macro releases -- not testable, and not a new mechanism.

Eight days of data covers roughly one CPI and no FOMC; there is no calendar on
disk and no pre-event tape. Mechanically it is idea 3 with an exogenous trigger,
so it inherits idea 3's verdict: it can only change WHEN we trade, and the
executable price is what decides whether that pays.

SUMMARY: 1 dead (no TWAP window), 2 dead (fee flat at ~96bps; late favourite
priced to 0.2pp), 3 real-but-edge-free, 5 untestable on this sample. 4 is open
and bounded. And the recurring lesson, restated for the third time today:
`up_price` is a displayed value that is not inside its own book on average --
never score a strategy against it.

## The "you cannot trade 1s impulses" batch: two halves measured, one already built (2026-09-15)

The pitch: (1) extend the horizon to 5-30s so the 1s fill lands mid-move
(ATR/range breakouts, 1m CVD divergence over 10-20s); (2) become a maker with
GTC resting limits and cancel into adverse moves; (3) pay up -- `best_ask + 2-3
ticks` -- to stop FOK failures.

Every window tested earlier today was 0.5-2s, so (1)'s horizon claim was
genuinely untested. It is tested now. (2) and (3) are already in this file.

### 1a. Horizon: extending 1s -> 30s does NOT help. The skill goes NEGATIVE.

`tools/precursor_entry.py --perp-win-ms`, same 2,606 rounds, same honest
+1000ms entry pricing, each paired against the opposite side at its own entries:

  window    n     EV_net   paired vs opposite side
  1s       953    +0.067   +0.0317  se 0.0815  t +0.39
  5s      1360    -0.008   -0.0370  se 0.0614  t -0.60
  10s     1410    -0.007   -0.0415  se 0.0610  t -0.68
  20s     1432    -0.011   -0.0568  se 0.0606  t -0.94
  30s     1429    +0.002   -0.0398  se 0.0622  t -0.64

The trade count triples (953 -> 1,432) and the EV goes to zero, while the paired
difference -- the statistic that isolates DIRECTION -- is negative at every
horizon beyond 1s. Elongating the window does not buy runway for our 1s fill;
it buys more trades with no signal in them.

### 1b. Volatility expansion (squeeze breakout): the only non-flat cell all day, and it is 09-12.

New rule BREAK: 30s perp displacement >= `--break-bps` AND >= `--break-x` times
the mean |30s displacement| of the prior 5 minutes, taking the direction of that
displacement. Four configs:

  x1.5 / 2bps   n= 872   EV_net -0.008   paired -0.0672  t -0.84
  x2.0 / 3bps   n= 513   EV_net +0.093   paired +0.0744  t +0.68
  x3.0 / 3bps   n= 230   EV_net +0.371   paired +0.3259  t +1.41   <- best of the day
  x2.0 / 6bps   n= 199   EV_net +0.139   paired +0.0769  t +0.34

EV rising monotonically with strictness is the signature of a FITTED parameter,
and the best cell is the smallest sample in its own sweep. The decisive read:

  BREAK  n=230  mean_px 0.417  win 0.409  EV_net +0.351  med_tau 127  day-t +0.92
  by day: 09-08 -0.292(29)  09-09 +0.041(58)  09-10 +0.235(57)
          09-11 -0.024(27)  09-12 +1.333(59)

Negative on 2 of 5 days; day-clustered t +0.92; and the entire +0.371 is 09-12
(+1.333 on n=59). Strip that one day and the other four average **+0.039** on
n=171. It is the day factor again, selected by threshold -- the same 09-12 that
makes every other rule positive. And its median tau is 127s: mid-round, not a
late-window rule at all.

### 2. Maker: BUILT AND MEASURED ALREADY -- and the edge was a price artifact

This is not untested work; it is recorded above. `stale_maker` (09-12/13) is the
literal rest-before-the-move design, and its decomposition is the whole story:
**"the measured edge is ENTIRELY in the fill price: at the ask +0.0175/$1, at
the mid +0.3449."** Resting at the mid looks like +34%/$1; at the ask it is
+1.75% gross. Live it ran n=24, flat (+1.4 pts, 0.1 SE). That is the same
displayed-vs-executable collapse measured again in the section above, and it is
why the standing rule is do-not-widen-buffers.

New numbers from the 801 live fills, for whoever re-runs it:

  fee_paid/stake: mean 95.9 bps, median 88.4, p10 0.0, p90 203.8
  **24.8% of fills pay ZERO fee** (the mean is a minority of expensive fills)
  fee_paid is NOT marketProviderFee + networkFee (they disagree on 548/771 rows)
  the venue DOES attribute maker/taker: all 472 fills carrying the field are
  maker-side (makerShareQty > 0, taker 0) at 86.5 bps mean / 63.6 median
  fillPercentage: mean 0.607 -- **566/771 fills are PARTIAL**
  favourite-side spread late in the round: median 2.0 cents

So the prize (2 cents = 200 bps) is real and larger than the fee, but the one
decomposition that exists says it does not survive contact with the ask. The
honest gap is n=24, not the design. A longer live maker run at fixed size is a
legitimate experiment; a fresh backtest is not, because the backtest has been
done and it says the edge is the mid.

### 3. Paying up (best_ask + 2-3 ticks): already tested, and the failures are not 1-2 ticks

Two prior results settle it. (a) "The cancelled orders: adverse selection, and
why NOT to widen the buffer" -- 7 missed fills, **+7.17/$1 at our bids vs -0.73
at the real ask**; the fills we get are the ones where the book stayed put, and
the ones we miss are the ones that ran, i.e. exactly when we were right. (b) The
drought windows needed **25,454-203,333 bps** of slippage -- past the venue's own
documented maximum of 10,000 bps, which we already raised the clamp to (commit
428c0fc). No ceiling can fill those.
Today's fills show we already pay up: executed price vs requested is median
+16 bps (mean +13.3, p95 +373). The complaint that we lose 1-2 ticks to venue
latency describes a different failure than the one we have.

## The 30-day corpus: found it, used it, and it settles the question (2026-09-15)

"we have a historical 30 day corpus i think on the vps or here, so test on
that" -- it is here, and it is bigger than 30 days. Census:

  data/btc5m/                     53 days (2026-04-29 .. 06-20), 15,264 round
                                  dirs, 13 GB. Per round: ticks_dense.parquet
                                  (300 x 1s of the underlying tape),
                                  trades_raw.json, metadata.json
  data/btc5m_prices.parquet       8 days, 2,240 windows, 358,808 rows. Per
                                  second: up_px/dn_px (odds), up_buy_px /
                                  dn_buy_px, up_sell_px / dn_sell_px, mid,
                                  fees, n_up/n_dn, vol_up/vol_dn
  data/btc5m_ml.parquet          31 days, 8,436 windows, 177,156 rows, 71 cols:
                                  mom_1..mom_300_bps, vol_surge, cvd_ratio,
                                  z_tau, delta_bps, sigma_*, efficiency, ...
                                  plus y_up. NOTE `px` here is the BTC PRICE
                                  (72k-82k), NOT the odds.

The prize is `up_sell_px`/`dn_sell_px`: real executable quotes, which the live
venue DB never had in usable form. That is what makes a priced test possible at
all. Tools: `tools/corpus_btc5m_test.py`, `tools/corpus_signal_power.py`,
`tools/corpus_paired_rules.py`, all read-only.

### FIRST: the column names are inverted, and it costs 6.65%/$1 to believe them

`*_buy_px` is the BID and `*_sell_px` is the ASK. Proof, on 166,215 rows that
carry both:

    up_sell - up_buy: median +0.0100, 85.6% positive
    up_buy + dn_buy : median  0.9900, and <1.0 on 93.1% of rows
    up_sell + dn_sell: median 1.0100

Buying BOTH sides at the "*buy*" price would cost 0.99 and pay 1.00 -- free
money, which is the proof those are resting bids. Same corpus, same rule, same
days, only the side of the book changed:

    FAVOURITE, tau 5-20s    n    mean paid  realized  EV_net    day-t  days +
    paying the BID         968    0.8487     0.8936   +0.0665   +4.24   8/8
    paying the ASK         452    0.8672     0.8584   -0.0166   -0.91   2/8

A t of +4.24 positive on eight days of eight becomes -0.91 on six of eight
negative. That is the third appearance of this trap today (venue `up_price`,
the venue's `chance`, now this) and the cleanest demonstration of it yet.

### SECOND: the per-$ "EV" estimator is unsafe on cheap sides

The mean of `(1-p)/p if win else -1` is dominated by leverage, so the same cell
read +0.0665/$1 under it and -0.0043 per SHARE. The repo already carried the
rule ("per-SHARE EV, never per-$ staked: cheap tokens make it levered") and
this is a fresh demonstration: two artifacts (the bid, then the estimator)
stacked to manufacture a +6.65% mirage out of nothing. Everything below is
per-share: `edge = P(win) - price - fee`.

### THIRD: the 31-day table cannot answer a priced question

A hit rate against a 50% baseline over 31 days looks spectacular --
`dist>=10bp` scores 0.9982 with day-t +686, `mom_300>=8bp` 0.9819 with +179 --
and all of it is the delta-selection mirage this file already documents: being
10 bps from the strike with under 60s left does not PREDICT a win, it IS the
win, and the price of that side is ~0.99, not 0.50. Nine such rules came back
"+0.05 to +0.48 edge, day-t 8 to 686". A hit rate is necessary and nowhere near
sufficient; the price has to be below it. Discard this table for P&L.

### THE ANSWER: 8 days, 1,840 rounds, priced at the real ask

Rules joined to real quotes, one row per round (the last quote in tau 10-60s),
paying the executable ask, per-share edge, day-clustered t over 8 days:

    rule                 n   side_px    hit   edge/sh    ROI    day-t  days +
    FAV (priced side) 1840    0.9126  0.9207  -0.0067  -0.74%   -1.29   2/8
    DIST>=2bp          722    0.6272  0.6343  -0.0063  -1.01%   -0.56   2/8
    DIST>=5bp          158    0.6340  0.6456  +0.0009  +0.13%   +0.06   4/8
    DIST>=10bp          23    0.7246  0.7826  +0.0289  +3.99%   +1.15   5/7
    MOM_30>=4bp        247    0.6696  0.6802  -0.0094  -1.40%   -0.64   5/8
    MOM_120>=4bp       680    0.5749  0.5794  -0.0110  -1.91%   -1.25   3/8
    VOLSURGE>=2.5       71    0.6514  0.6761  -0.0237  -3.64%   -0.54   4/8
    CVD>=0.6          1742    0.5166  0.5098  -0.0226  -4.37%   -2.84   1/8
    ZTAU>=1.0          818    0.5609  0.5452  -0.0251  -4.47%   -2.84   0/8

Nothing is positive with significance. The two cells that are positive
(DIST>=5, DIST>=10) have day-t 0.06 and 1.15, and the second is n=23. The two
that ARE significant are significant NEGATIVE, on 1/8 and 0/8 days: taking the
favourite side at the executable ask loses 4.4% per share over 8 days. This is
the same cheap-side/expensive-side asymmetry measured on the live venue, now
reproduced on an independent venue with trustworthy quotes and 1,840 rounds.

VERDICT: the corpus does not rescue the ideas -- it confirms the venue result
with better data. Across two venues, 13 days of priced rounds and ~3,400
entries, buying the side a signal prefers at the executable ask is between
-4.5% and +0.1% per share, and the honest reading is "no edge, and specifically
no favourite-side edge". What the corpus DID produce is worth more than the
answer: three measured traps, of which the bid/ask inversion is the one that
just cost this session a 6.65% false positive.

### CORRECTION to the table above: pooled vs day-mean edge

The first version of that table printed the mean of the DAILY means in the
edge column while printing pooled means for `hit` and `side_px`. With unequal
n per day those are different statistics and they differ in sign on the
near-zero cells. `tools/corpus_paired_rules.py` now prints both:

    rule                 n   side_px    hit  edge_pool  edge_day   day-t  days+
    FAV (priced side) 1840    0.9126 0.9207   -0.0071   -0.0067   -1.29   2/8
    DIST>=2bp          722    0.6272 0.6343   -0.0096   -0.0063   -0.56   2/8
    DIST>=5bp          158    0.6340 0.6456   -0.0016   +0.0009   +0.06   4/8
    DIST>=10bp          23    0.7246 0.7826   +0.0531   +0.0289   +1.15   5/7
    MOM_30>=4bp        247    0.6696 0.6802   -0.0122   -0.0094   -0.64   5/8
    MOM_120>=4bp       680    0.5749 0.5794   -0.0122   -0.0110   -1.25   3/8
    VOLSURGE>=2.5       71    0.6514 0.6761   -0.0060   -0.0237   -0.54   4/8
    CVD>=0.6          1742    0.5166 0.5098   -0.0227   -0.0226   -2.84   1/8
    ZTAU>=1.0          818    0.5609 0.5452   -0.0289   -0.0251   -2.84   0/8

The two columns differ in sign only on DIST>=5 and DIST>=10, both of which were
already inside the noise. FAV is negative both ways, so every verdict above
stands; the bug would only have mattered if a cell had been near-zero and
claimed as an edge.

## The maker: rest at the bid and collect the spread? Measured -- NO. (2026-09-15)

Raised as the natural conclusion: our takers lose about the size of the spread,
so stop paying it and start collecting it -- rest a BUY at `up_buy_px` at
tau 180-120s, cancel defensively if the tape goes against it, and harvest the
1-2 cents the market makers must be taking. `tools/corpus_maker_sim.py`.

The premise that has to be tested rather than assumed: a resting buy at the bid
does not fill because we want it to. It fills when somebody SELLS into us, which
is when the price is coming down. That is adverse selection and it is measurable.

Simulation: at `place_tau`, rest a buy at that side's bid; walk forward second by
second; CANCEL if the underlying tape (ticks_dense, 1s closes) moves
`cancel_bps` against within one second; FILL if that side's ASK trades down to
the limit. The fill rule is OPTIMISTIC -- front of queue, fills on touch -- so
the real result can only be worse.

  place_tau  buffer  fills  limit   realized   edge/sh    ROI    day-t  days+
    240        0.00   847   0.6425   0.6009   -0.0418   -6.50%  -4.28   0/8
    240        0.01   811   0.6416   0.5906   -0.0512   -7.98%  -5.52   0/8
    180        0.00   748   0.6996   0.6430   -0.0570   -8.14%  -5.11   0/8
    180        0.01   707   0.6949   0.6266   -0.0685   -9.86%  -6.03   0/8
    120        0.00   671   0.7390   0.7079   -0.0315   -4.26%  -3.10   1/8
    120        0.01   621   0.7307   0.6860   -0.0452   -6.19%  -4.20   1/8
     60        0.00   499   0.7708   0.7014   -0.0747   -9.70%  -3.77   0/8
     60        0.01   462   0.7644   0.6775   -0.0927  -12.12%  -4.92   0/8

(all with the 2bps defensive cancel). Every configuration loses, on 0-1 of 8
days, at day-t -3.1 to -6.0. The stricter fill rule is worse in all four
comparisons, as it must be: the fills it removes are the benign ones and the
ones it keeps are the most adversely selected.

THE MECHANISM, measured: realized win rate is 3-9 percentage points BELOW the
limit paid, in every single configuration. That is the adverse selection, and it
is 3-7x the ~1.2pp spread the idea is trying to collect. **The spread is not
free money; it is the compensation for being filled exactly when you are wrong,
and at this venue it does not cover the damage.**

The defensive cancel does not rescue it either. At 2bps it cancels ~100 of 1,595
fills and moves the Up side from -7.67% to -8.14% (i.e. nothing, or slightly
worse); 5bps cancels 14 fills, 10bps cancels 1. Cancelling on a 1-second adverse
tick is simply too slow and too local to avoid the fills that matter -- by the
time the tape has ticked 2bps against you, the order is already the best bid in
a falling market.

### And the 6.65% was never the spread

The proposal's arithmetic assumed the +6.65% measured at the bid WAS the
market-making spread, making MMs the harvesters. It was mostly not. That number
came from the per-$ estimator (mean of `(1-p)/p`), which is leverage-dominated;
per share, the same bid-side cell is **-0.43%**, and the bid-ask gap itself is
~1.2pp (median spread 0.0100 on a ~0.85 price). So the spread being fought over
is ~1.2pp -- and the maker sim shows it costs 3-9pp of adverse selection to
collect it. The inference "MMs are harvesting 6.65%" overstates the prize by
roughly 5x, and the direction of the trade is wrong.

FINAL ANSWER ON THE MAKER: no. Not "unproven" -- significantly negative in 8 of
8 configurations with an optimistic fill model, on 8 days, and worse than simply
taking at the ask (-0.71%). The one honest caveat is that this models a single
side with no inventory management and no queue position; an MM that also quotes
the other side and skews with flow is a different object. But the adverse
selection is measured directly at 3-9pp, so any such strategy has to overcome
that first, and nothing measured today comes close.

## The synthetic-sweep idea, tested -- and the recorder that now exists
(2026-09-15)

The follow-up proposal conceded the two data claims and proposed detecting
forced sweeps directly in the tape we already hold, on the grounds that a
liquidation is executed as an aggressive market order and leaves the same
footprint: one-sided, several price ticks, large notional in a burst (no
liquidation tag needed).

VERIFIED FIRST, each against a working control:

  GET fapi/v1/allForceOrders   -> HTTP 404, an HTML error page (route is gone)
  GET fapi/v1/forceOrders      -> HTTP 401 {"code":-2014,"msg":"API-key format invalid."}
  GET fapi/v1/aggTrades        -> HTTP 200   <- control: the API itself is up

so the public liquidation tape is gone and only a private, signed one remains,
and `!forceOrder@arr` is the only public liquidation signal left (throttled to
at most one order per symbol per second -- recorded now rather than assumed).

THE TEST. `tools/precursor_entry.py --sweep-*`: futures tape, burst window,
one-sidedness |net|/total >= --sweep-ratio, >= --sweep-ticks distinct prices
walked, notional >= --sweep-usd. Paired against the OPPOSITE side at the same
entries, same honest +1000ms entry pricing, day-clustered t:

  config                                n     EV_net   day-t   paired t
  $300k / 200ms / 3 ticks / ratio 0.9  249    +0.030   +0.27     +0.39
  $100k notional                       484    -0.074   -0.77     -1.33
  $1M notional                          84    -0.061   -0.11     -0.17
  100ms burst window                   124    +0.130   +0.26     +0.47
  500ms burst window                   540    -0.054   -0.60     -1.50
  2 ticks instead of 3                 255    +0.028   +0.19     +0.38
  spot tape instead of perp             29    +0.221   +0.50     +0.41
  loose ($50k / 500ms / 2 / 0.8)      1030    -0.058   -0.59     -1.81

No cell reaches |t| = 2, and the sign follows the knob: tightening the notional
to $100k turns the default's +0.39 into -1.33; widening the window to 500ms
turns it into -1.50. The LARGEST sample -- n=1030, the loosest definition, i.e.
the closest thing to "all sweeps" -- reads -1.81, so the inverse side (buying
the side the sweep is running AGAINST) is the better trade.

### The pattern that explains every one of these results

Every trigger tested buys the CHEAP side (mean price 0.36-0.43) and every one of
them does worse than its own mirror image, which buys the EXPENSIVE side
(mean 0.60-0.66): SWEEP_INV +0.121 vs SWEEP -0.074 at $100k, and VEL_INV's
day-t +6.79 against VEL's -0.51. That asymmetry is not a discovery about sweeps;
it is a property of the DISPLAYED price, which we established is not executable
(`up_ask`/`down_ask` are NULL on these days, so `price_of` falls back to the
mid-like `up_price`). The paired test cancels it, and the paired test is the one
that keeps coming back flat.

Three independent "structural catalyst" ideas -- perp displacement, burst
velocity, sweep footprint -- are now flat-to-negative, and in each case the
better-looking side is the one the rule does not take. Together with the
day-factor finding above (six rules, median daily-EV correlation +0.73, 6/6
positive on 09-12 and 0/6 on 09-08 and 09-11), the honest read is that on these
five days no microstructure trigger separates from the day.

VERDICT: do not ship any of it, and stop hunting for a trigger inside this
window. Five days, rules that are 73% correlated with each other, and a
non-executable entry price are the binding constraints -- not the choice of
feature.

## `tools/record_micro.py` -- the recorder (built 2026-09-15, NOT deployed)

Both sides of this conversation converged on recording, so it exists now.
It subscribes to futures `depth20@100ms` per symbol plus `!forceOrder@arr` and
writes RAW json prefixed with the local receive timestamp (no schema decision
can lose data later) into hourly gzip files under
`data/micro/depth/<sym>-<YYYYMMDD-HH>.jsonl.gz` and `data/micro/liq/`.
Reconnects with exponential backoff, flushes every 5s, prints a heartbeat with
msg/s and a projected GB/day, and is entirely separate from the trading path --
it cannot touch the venue, the ledger or the bot's state.

Smoke-tested live twice: 4 streams connected, hourly rotation correct, ~7 depth
msgs/s/symbol observed (100ms nominal, so some intervals go unpublished).

FOOTPRINT, measured rather than guessed: ~4.5 KB/s gzipped for ONE symbol at
depth20@100ms => **~1.2 GB/day for BTC/ETH/BNB**, ~8 GB/week. `--depth-levels 5`
or BTC-only cuts that ~4x. The VPS has ~89 GB free, so a week is comfortable and
a month is affordable with pruning.

WHY THIS IS THE ONLY PATH LEFT: the two questions that survived the tick-level
work -- does a depth void, or a forced sweep, predict a drift of 3-10s, long
enough to outlive our ~1s decision-to-fill? -- cannot be asked of any archive.
Depth at that resolution was never published for 2026 (and the free futures
`bookDepth` archive is 20bps bands at 30s), and the liquidation tape is behind
a private endpoint. Recording starts the clock; in about a week there will be
more days than five.

## THE REMAINING GAP, CLOSED: the sub-second defensive cancel (2026-09-15)

This was the last unexcluded version of the maker idea. It was excluded on the
theory that it could only be tested live, because the corpus's venue prices are
1 s granularity and a sub-second cancel is by definition a sub-second question.
That reasoning was wrong twice over, and the way it was wrong is the finding.

### Part 1 -- the physics: the spot is SLOW, so there is nothing sub-second to race

`data/bookticker/BTCUSDT-2026-09-01.csv.gz`, Tardis top-of-book, **9,847,706
events over 24.00 h = 114/s**, microsecond stamps. mid = (bid+ask)/2. From
30,000 random instants, time until the mid falls N bps:

| adverse | reaches in 30 s | p10 | p25 | median | p75 | <250 ms | <500 ms | <1 s |
|---|---|---|---|---|---|---|---|---|
| 0.25 bp | 64.4% | 39 ms | 490 ms | 3,438 ms | 10,954 ms | 20.4% | 25.1% | 31.2% |
| 0.5 bp | 59.9% | 113 ms | 964 ms | 4,465 ms | 12,122 ms | 14.6% | 19.0% | 25.4% |
| 1 bp | 52.4% | 488 ms | 2,047 ms | 6,640 ms | 14,712 ms | 6.6% | 10.1% | 16.1% |
| 2 bp | 37.6% | 1,626 ms | 4,396 ms | 9,816 ms | 17,861 ms | 1.7% | 3.1% | 6.2% |
| 5 bp | 13.2% | 4,477 ms | 8,688 ms | 15,567 ms | 23,309 ms | 0.2% | 0.5% | 1.1% |

The spot needs a **median ~10 s to travel 2 bps**. Only **1.7%** of 2 bp moves
complete inside 250 ms. So the premise of the whole sub-second exercise was
false: there is no millisecond race to win. A defensive cancel that reacts on a
**1-second** grid is not "too slow" -- it has **seconds** of warning (median
3,438 ms even at the hypersensitive 0.25 bp threshold). For the venue's cancel
round-trip to be the binding constraint it would have to exceed ~3 s; measured
venue order round-trips are 260-1,237 ms. **Latency was never the bottleneck**,
so the live cancel-RTT measurement is no longer needed to decide this.

### Part 2 -- my own cancel rule was mis-specified, and the physics is what exposed it

`tools/corpus_maker_sim.py` tested: *"cancel if the tape moves `--cancel-bps`
against the order within one second"* -- a **velocity** rule. The table above
explains its failure exactly: a 2 bp move *inside one second* happens on only
6.2% of 2 bp paths, so the rule fired on 100 of 1,595 orders and was dead on
arrival. The correct defensive rule for a *resting* order is a **level** rule:
*cancel if the spot has moved `--cancel-bps` against the **placement** spot*.
Added as `--cancel-mode {cum,tick}` (default `cum`), and it fires properly:
**24.8%-47.8% of orders cancelled** instead of 6%. The 1 s grid is *adequate* for
a level rule precisely because the trigger travels over ~10 s, not ~100 ms.

### Part 3 -- the corrected cancel, tested 8 ways: still negative everywhere

`--place-tau 180 --side fav`, 8 days, one order per round, edge **per share**
(cross-check: `mean(edge) = mean(won) - mean(limit)`, so fees here are ≈1 bp and
cannot be blamed):

| cancel | mode | side | fills | lim | realised | edge/share | day-t | days + |
|---|---|---|---|---|---|---|---|---|
| off | - | Up | 800 (39.2%) | 0.7035 | 0.6500 | **-0.0539** | -4.93 | 0/8 |
| off | - | Down | 795 (39.0%) | 0.6950 | 0.6465 | **-0.0486** | -2.83 | 1/8 |
| 3 bp | cum | Up | 707 (34.7%) | 0.6961 | 0.6492 | **-0.0473** | -3.40 | 1/8 |
| 3 bp | cum | Down | 701 (34.4%) | 0.6862 | 0.6491 | **-0.0373** | -2.01 | 2/8 |
| 2 bp | cum | Up | 628 (30.8%) | 0.6900 | 0.6433 | **-0.0471** | -3.26 | 1/8 |
| 2 bp | cum | Down | 650 (31.9%) | 0.6816 | 0.6492 | **-0.0325** | -1.53 | 2/8 |
| 1 bp | cum | Up | 510 (25.0%) | 0.6879 | 0.6510 | **-0.0375** | -2.70 | 2/8 |
| 1 bp | cum | Down | 505 (24.8%) | 0.6802 | 0.6515 | **-0.0288** | -1.32 | 3/8 |
| 0.5 bp | cum | Up | 433 (21.2%) | 0.6788 | 0.6351 | **-0.0443** | -2.35 | 2/8 |
| 0.5 bp | cum | Down | 421 (20.6%) | 0.6764 | 0.6532 | **-0.0233** | -0.89 | 3/8 |
| 0.25 bp | cum | Up | 388 (19.0%) | 0.6793 | 0.6443 | **-0.0357** | -1.69 | 3/8 |
| 0.25 bp | cum | Down | 381 (18.7%) | 0.6788 | 0.6614 | **-0.0176** | -0.85 | 3/8 |
| 1 bp | cum | Up (both) | 1,209 (59.2%) | 0.4522 | 0.4194 | **-0.0332** | -3.44 | 1/8 |
| 1 bp | cum, buffer 0.01 (strict) | Up | 448 (22.0%) | 0.6794 | 0.6317 | **-0.0479** | -2.34 | 2/8 |
| 1 bp | cum, buffer 0.01 (strict) | Down | 441 (21.6%) | 0.6727 | 0.6281 | **-0.0447** | -1.94 | 1/8 |

**Best cell of the whole matrix: -0.0176/share, day-t -0.85, 3/8 days.** Still
negative, still not significant. The cancel is monotonically *not* monotone --
0.25 bp beats 1 bp beats 0.5 bp -- which says the threshold is selecting on
noise, not on toxicity.

### Why the gap is closed now, and why the earlier "can only be tested live" was wrong

The defensive cancel **works as designed**. It fires on 19-48% of orders instead
of 6%. It gets seconds of warning, far beyond any plausible cancel round-trip --
so a *zero*-latency cancel is already covered by these results, and no live
venue-latency experiment can rescue the idea. And it still loses, at **every**
threshold, in both directions, under both fill buffers.

The arithmetic that closes it: cancelling recovers **~1.5-2 pp** of a **~5 pp**
hole and stops. The fills you *keep* after the cancel are still adversely
selected by **1.8-4.8 pp per share** -- and the realised-minus-limit gap on the
best cell (0.6614 realised vs 0.6788 limit) is essentially the spread. The
defensive cancel removes the orders, not the *selection*: the trades that still
reach you are the ones where the market came to you because it knew something.
**This is the same wall every other experiment hit this session**, now confirmed
at the one parameterisation that had been left untested.

Method caveat, stated plainly: the corpus's *venue* prices are 1 s, so genuine
sub-second **venue-price** dynamics remain unobservable. That no longer matters,
because the *trigger* (the spot level) travels over ~10 s -- 1 s sampling of a
10 s process is fine -- and because the binding constraint turned out to be
selection, not speed. What remains genuinely unmeasured is the venue's cancel
round-trip; it is no longer decisive, and it is recorded here as unmeasured
rather than as measured.

**VERDICT: the maker idea is now excluded in every testable form -- passive
rest, passive rest with a velocity cancel, and passive rest with a correctly
specified level cancel at five thresholds. There is no remaining variant.**

## The five-idea batch: four falsified, one not applicable (2026-09-15)

Tools: `tools/corpus_roundstart.py` (corpus half), `tools/roundstart_live.py`
(live half -- the one that decides). Both read-only.

### IDEA 1 -- "strike-pegging / initialization lag": premise TRUE, edge FALSE

The premise checks out, and that is worth recording: at sec 0-7 of a round the
corpus quotes a flat **up_buy median exactly 0.5000**, and at sec=0 the spot IS
the strike, so 0.50 is correct there and only becomes stale as the spot walks
away. The venue really does open every contract at ~0.50.

The corpus then appears to show a large edge. `tools/corpus_roundstart.py`,
2,240 windows, strike = tape at sec 0, buy the side the spot moved toward at its
ask, one row per (round, sec):
  sec>=1 pooled  n=1384  ask 0.5164  realized 0.5621  edge +0.0445  **day-t +4.77 (8/8 days)**
  by sec: 1 +0.0193(42) | 2 +0.0697(122) | 3 +0.0959(236) | 4 +0.0705(299) | 5 +0.0072(302) | 6 +0.0166(383)
and by distance, the calibration gap opens exactly where |dist|>0 and is ZERO
where it is not:
  |d| 0.00-0.25  n 669  ask 0.5044  realized 0.5052  **+0.0008**
  |d| 0.25-0.50  n 103  ask 0.5045  realized 0.5922  +0.0878
  |d| 0.50-1.00  n 205  ask 0.5229  realized 0.5707  +0.0478
  |d| 1.00-2.00  n 243  ask 0.5310  realized 0.6214  +0.0904
  |d| 2.00-5.00  n 148  ask 0.5413  realized 0.6824  +0.1412

**First check -- not my strike, not my alignment.** My dist was validated against
the corpus's OWN `delta_bps` at elapsed=31 over 4,000 rows: mean diff **+0.002
bps**, median 0.000, correlation **+0.9618**, and `strike_tape` is byte-identical
to `open_tape` on 100.0% of rows. So the strike and the tape indexing are right.

**Second check -- is the "win rate" momentum? NO.** Pure tape, all 15,240 rounds,
forward move from sec s to the close, split by the sign of the move so far:
  s     n     E[fwd|d>0]  E[fwd|d<0]   diff     win%|d>0  win%|d<0
  1  15240     +0.078      -0.471     +0.549     51.65%    45.78%
  3  15240     +0.381      -0.536     +0.917     54.34%    44.18%
  10 15240     +0.139      -0.326     +0.464     56.83%    42.52%
  31 15240     -0.037      -0.085     +0.048     60.53%    38.37%
  60 15240     -0.108      -0.045     -0.063     64.78%    34.33%
The forward move is essentially INDEPENDENT of the signal (+0.92 bps at s=3
against a remaining sigma of ~9.6 bps, and NEGATIVE by s=60). The rising win rate
is purely mechanical -- being ahead now makes finishing ahead more likely -- and
it matches the closed form: leader win% = Phi(|d| / sigma*sqrt(300-s)) with
sigma~0.298 predicts 55.0/60.9% at |d| 0.5-1/1-2 versus 55.40/58.11% measured.
**This is the delta-selection mirage (NINTH/TENTH face), not a signal.**

**Third check -- is the sample biased? NO.** The venue subsample's leader win
rates track the full-tape ones bin by bin (0.5052/0.5922/0.5707/0.6214/0.6824 vs
50.18/54.39/55.40/58.11/62.28%). So the corpus's own rows are representative.

**Fourth check -- is it round-start-specific? In the corpus, YES.** Using the
corpus's own `delta_bps` and `y_up` at elapsed 31/61/91/121 s the calibration
gap is +-0.03 (well calibrated) -- so the apparent mispricing really is confined
to the first seconds, which is what the claim predicts.

**THE DECIDER -- the live venue contradicts the corpus.** `tools/roundstart_live.py`,
live DB, September 2026, btc-updown-5m, tau 294-300 s, **one row per round**,
868 rounds, fee 96 bps of stake:
  |d| 0.00-0.25  n 487  ask 0.5055  realized 0.4723  **-0.0381**
  |d| 0.25-0.50  n  67  ask 0.5300  realized 0.4776  **-0.0575**
  |d| 0.50-1.00  n  94  ask 0.5572  realized 0.5213  **-0.0413**
  |d| 1.00-2.00  n 110  ask 0.5739  realized 0.5636  **-0.0158**
  |d| 2.00-5.00  n  88  ask 0.6030  realized 0.6250  **+0.0163**
  6 of the 8 live days are negative.
  pooled LEADER -0.0325 (day-t +0.48) | |d|>=0.25 **-0.0254 (day-t -2.22)** |
  |d|>=0.5 -0.0185 (-2.21) | |d|>=1 -0.0088 (-1.89)

**And here is why, in one number: the live ask is not the corpus ask.**
  |d| 1.00-2.00:  corpus ask **0.5310**  vs  live ask **0.5739**
  |d| 2.00-5.00:  corpus ask **0.5413**  vs  live ask **0.6030**
The live market already prices 4-6 cents of the early move, which is exactly the
"edge" the corpus was showing. The April corpus's round-start `*_sell_px`
under-reacts relative to the venue that exists today. Live governs, because live
is what we could have paid.

VERDICT: **falsified on live data.** The structural premise (flat 0.50 open, a
book that takes seconds to differentiate) is real and now documented; the
tradeable edge in it is not.

### IDEA 2 -- "fade the retail overreaction / buy the battered side": NEGATIVE where it applies

For a binary pair edge_A + edge_B = 1 - ask_A - ask_B - fees, so the fade is the
leader's mirror, and the leader (idea 1) is already negative. Live, per share,
both sides at their own ask:
  |d| floor   n    LEADER            FADE (buy the laggard)
  >=0.00     868   -0.0325 (t +0.48)  +0.0109 (day-t -0.83)
  >=0.25     381   -0.0254 (t -2.22)  +0.0036 (day-t +1.32)
  >=0.50     314   -0.0185 (-2.21)    -0.0034 (day-t +0.85)
  >=1.00     220   -0.0088 (-1.89)    -0.0132 (day-t +0.85)
  >=2.00     110   -0.0018 (-0.97)    -0.0199 (day-t +0.78)
The fade is break-even at best, its day-t never exceeds +1.4 in any cut, and it
turns **negative precisely in the regime the claim targets** (odds pushed far
from fair: -0.0132 at >=1 bp, -0.0199 at >=2 bp). Aggregating the two sides'
prices is not the fix either: the ask sum is 1.0119, so the pair is -1.2c before
fees by construction. VERDICT: **no edge.**

### IDEA 3 -- "pin-risk double-fill: rest 0.35 on UP and 0.35 on DOWN": arithmetically impossible here

The claim's arithmetic needs the two BIDS to sum to 0.70 (0.35 + 0.35) so that a
double fill pays 1.00 for 0.70 = +0.30. Measured on 412,502 live two-sided quotes:
  **BID sum: min 0.1200, p01 0.9400, median 0.9900**; <0.95 on 1.2%, <0.80 on 0.18%
  ASK sum: min 0.1500, p01 1.0100, median 1.0100
The two quotes are two prices for the two sides of the SAME 300-second bet. One
cannot be cheap without the other being rich, so they cannot both rest at 0.35 --
the sum is pinned near 1.00. A double fill at the bids is therefore worth about
**+1.0c per pair**, not +30c, and it is paid for by the single fills, which are
the adverse ones. The both-sides maker sim already measured this directly:
**-0.0332/share, day-t -3.44** (previous section). VERDICT: **no edge.**

### IDEA 4 -- "CTF mint/merge + liquidity mining": not available on this venue, and the arb does not exist

The venue (`/sapi/v1/w3w/wallet/prediction/`) is a CLOB with
`trade/place-order-bundle` and `order/history`. There is no mint/merge primitive:
you cannot deposit $1 and receive YES+NO, so the CTF construction has no analogue
here. The derived trade -- buy BOTH sides for under $1.00 -- was checked directly:
  round start: ask sum < 1.0000 on **0 of 868** rounds (min exactly 1.0000)
  all taus:    ask sum < 1.0000 on 407 of 412,502 (0.10%), median 1.0100
and even those 0.10% do not clear the ~96 bps fee (1 - 0.99 - 0.0096 < 0). The
"bid sum 0.99" that looks like free money is the BID; buying happens at the ASK,
where the sum is 1.01. Polymarket's liquidity-reward distributions have no
counterpart on this venue. VERDICT: **not applicable.**

### IDEA 5 -- "oracle heartbeat frontrunning": already measured and dead

Settlement is a POINT SNAPSHOT, not a window: `rounds.end_price` vs Binance 1 s
bars gives |end - close at end| median **0.02 bps**, versus 0.44 bps against the
30 s mean and 0.85 bps against the 60 s mean. There is no 10-30 s heartbeat to
get ahead of, because the settle tracks the instantaneous spot. VERDICT:
**already falsified** (see the settlement section above).

### What the batch did produce

One genuinely new, reusable fact, and it is a data-reliability warning not a
strategy: **the April-May corpus's round-start quotes are not the live venue's
round-start quotes.** Same rule, same distance bin (1-2 bps off the strike), the
corpus says the leader costs 0.5310 while the live venue charges 0.5739 -- and
that 4-6c is the entire difference between a +0.0445 (day-t +4.77) mirage and a
-0.0325 reality. Any future round-start or early-round claim must be re-priced on
`quotes.up_ask/down_ask` from the live DB before it is believed.

## CORRECTION: there is no 1,000 ms venue delay, and the "Last Look" theory is unsupported (2026-09-15)

### The latency number was never ours

"1000 ms" arrived with the third-party text ("the venue takes 800ms to 1,500ms").
It was never measured here, and `grep` finds no hardcoded 1000 ms in the
execution path. What we actually have in `data/live.log`:

**`venue ack`** -- and the code defines it exactly: `t_ack = time.monotonic()`
sits immediately before `self._request("POST", PLACE_ORDER, ...)`, with the log
comment *"the ack ms is the decision->venue share of the race."*
  n=32:  min 185ms | p25 210 | **median 249** | p75 486 | p90 640 | **max 823**
  78.1% <= 500ms, **100% <= 1000ms**. The call that creates the order never took
  a second; the median is a quarter of one.

**`cross exec` duration** (decision -> our terminal state): n=135, min 1.75s,
p25 2.97s, **median 6.87s**, max 21.53s -- but this is OUR OWN machinery: quote ->
POST -> poll `order/history` (budget `order_status_wait_s`, raised 3.6s -> 9.0s)
-> position patience -> up to 3 attempts -> NO_FILL timeout. It says nothing
about venue latency, and repeating the 1 s figure as if it were measured was a
mistake on my part.

The real headline was in the same table all along: of 135 cross decisions,
**87 = NO FILL and only 8 filled -- a 5.9% fill rate.**

### Testing Last Look honestly: it is not needed, and we have no evidence of it

The ack cannot see a dealer hold by construction -- the code says so: *"The venue
returns HTTP 200 + {orderId} even when the order FAILS to fill. Never trust it:
look the order up in order/history."* So a 500-1500 ms internal hold is invisible
to it. The discriminating measurement is whether a *failure* happens when the
price is **inside** the range we authorised: if it does, our own published cap
cannot explain it.

The bound is logged only on the MARKET path, so the sample is tiny: **32
`venue ack` lines across the whole log**, 26 of which pair with a forward path.
Classified against `--slip`-derived bound (quote x (1+slip)) and the 50ms..1s path:

  price LEFT the authorised range (our own cap explains the FOK failure) : 20 / 26
  price stayed INSIDE the range                                          :  6 / 26
     of those 6: 3 FILLED (so not counterexamples at all), 3 unresolved in the log

**Zero clear counterexamples.** The dominant cause of our FOK failures is
arithmetic, not a discretionary veto. And the mechanism is symmetric and
published: `slippageBps` on a MARKET/FOK bounds the fill at quote x (1 + slip),
capped at 10_000 = 2x the quote **for every participant**. Our own comment already
recorded the consequence: the 2026-09-15 drought windows needed 25_454-203_333 bps
(3.5x-21x the quote), beyond the venue's maximum, unfillable at any latency by
anyone.

VERDICT: **Last Look is neither confirmed nor needed.** It is unfalsified only
because our instrument for it (the bound, logged on one path) is too thin --
recorded here as *unmeasured*, not as refuted. Note also that the theory's
conclusion (you cannot snipe 1-2s moves) was already right for a different
reason, so the theory changes no decision.

### The four "exploit the dealer" ideas, finished

1. **Stale volatility parameter** -- FALSIFIED earlier this session. The venue
   is not using a slow rolling vol: implied sigma (inverted out of the contract
   price) median **0.474** bps/sqrt(s) vs realised **0.464**, median ratio
   **0.99** (p10 0.51, p90 1.82). It tracks trailing realised vol to ~1%. There is
   no sigma gap to harvest, and the favourite-at-the-ask edge by vol-ratio bin is
   insignificant in all five bins.

2. **Cross-contract lag (BTC break -> ETH/BNB contract)** -- FALSIFIED, and this
   was the last untested idea in the batch. `tools/cross_asset_lag.py`, live DB,
   one entry per round, 3x3 signal x traded matrix, priced at the ask, fee 96 bps
   of stake, day-clustered t:

     signal traded  lag   n    ask   realized  edge/sh  day-t  days+
     btc    btc       0  859  0.6370  0.6519  +0.0088  +0.25   5/8   (own)
     btc    eth       0  325  0.6210  0.6092  **-0.0177**  -0.22   2/5   (cross)
     btc    bnb       0  155  0.5727  0.5226  **-0.0556**  -2.81   0/2   (cross)
     eth    btc       0  447  0.6150  0.5839  -0.0370  -3.37   1/7   (cross)
     eth    eth       0  472  0.6100  0.5911  -0.0248  -0.37   2/5   (own)
     bnb    btc       0  238  0.6143  0.5714  -0.0488  -1.67   1/4   (cross)
     bnb    eth       0  144  0.5949  0.5486  -0.0520  -4.50   0/2   (cross)
     bnb    bnb       0  149  0.5770  0.5705  -0.0120  -0.53   1/2   (own)

   The claim is specifically that the CROSS signal carries information the
   contract's own spot has not yet imparted, so the test had to be cross vs own
   at identical entries. It fails in 8 of 9 cells:

     own vs cross: btc lag0 own +0.0088 vs cross -0.0411  NOT better
                   eth lag0 own -0.0248 vs cross -0.0283  NOT better
                   bnb lag0 own -0.0120 vs cross -0.0249  NOT better
                   (and at 1s/2s every cell is worse; the single "BETTER" is the
                    bnb/2s cell on 2 days of data, i.e. noise)

   The cross signal is systematically WORSE than the own signal -- it adds no
   information, it only dilutes with a less correlated asset. And paying our real
   latency kills even the diagonal: btc->btc goes +0.0088 (0ms) -> -0.0281 (1s)
   -> -0.0212 (2s). BNB cells rest on 2 days and their day-t values are
   meaningless; the ETH cells on 5 days are solidly negative.

   **Refinement, because a second tool disagrees at lag 0 and the difference is
   the sample definition.** `tools/venue_lag_pullback.py` (IDEA 2b) uses a 1-second
   BTC break with NO tau band and finds the lag-0 cross cell marginally POSITIVE:
       thr  lag     n     ask  realized    edge   day-t
       3bps   0     543  0.6123   0.6335  +0.0212  +1.28
       3bps   1000    543  0.6394   0.6335  **-0.0059**  +0.92
       3bps   2000    543  0.6576   0.6335  **-0.0241**  +0.48
   So "every cross cell is negative" is true for the tau-banded 5s-lookback
   matrix above and NOT true for the 1s-break variant -- I overstated it. Both
   agree on the thing that decides: the edge is gone by +1 s, and +0.0212 at
   day-t +1.28 is not significant anyway.

   **And the lag itself is real -- just smaller than our latency.** The same tool
   regresses the contract's next-1s mid change on the BTC move and the own-spot
   move jointly (67,804 eth / 60,526 bnb observations, overlapping 1s windows):
       eth: BTC coef +0.00670  t +25.83 | own-spot coef +0.00330  t +26.55
       bnb: BTC coef +0.00820  t +27.21 | own-spot coef +0.00451  t +20.01
   The BTC coefficient is significant and POSITIVE, which is exactly the dealer
   lag the claim predicts. But those t-stats are inflated by overlapping windows,
   and the trade test is what decides: a lag that is worth +2.1c at zero latency
   and -0.6c at 1 s is worth nothing to us, because our venue ack alone is a
   median 249 ms and fills take seconds. **The mechanism is real; the edge is
   sub-latency.** That is the honest form of this finding.

3. **Pullback entry (wait for the retrace, then fire)** -- FALSIFIED earlier:
   -0.0150/share, day-t +0.00, 1/8 days positive.

4. **Bundle execution as an atomic multi-leg spread** -- NOT AVAILABLE.
   `trade/place-order-bundle` takes a single `quoteId`; there is no array of legs
   and no cross-round atomicity to exploit. The official schema also confirms
   MARKET must be FOK with no price limit, so `slippageBps` (integer, range
   1-10000) is the only price bound that exists.

New tool: `tools/cross_asset_lag.py` (read-only).
