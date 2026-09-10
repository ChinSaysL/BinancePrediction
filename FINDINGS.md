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
