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
