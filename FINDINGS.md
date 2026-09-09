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
