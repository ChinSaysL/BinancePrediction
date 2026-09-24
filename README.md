# BTC 5-Minute Prediction Market Bot

Trades Binance's **BTC Up or Down 5m** binary prediction market
(vendor Predict.fun, BSC, USDT collateral, fee `2% x min(p, 1-p)` per share),
staking a percentage of the live bankroll.

This is a **relative-value** strategy, not a price-prediction one. The market
already quotes a probability; we only trade when our fair value differs from
that quote by more than the cost of crossing. At round open, 5-minute BTC
direction is a coinflip — trying to forecast it is the wrong problem.

See [FINDINGS.md](FINDINGS.md) for the measured evidence behind every claim here.

## Current live setup (2026-09-24)

What the live bot runs today, and why. Every number below was measured on the
recorded tape (`tools/model_knob_sweep.py`, which reads its gates from
`settings`, so it replays the rule the bot actually runs), not assumed.

| Piece | Setting | What it does |
|---|---|---|
| Market | `btc-updown-5m` only | The model arm's entry gates (tau 60-210s, quote 0.475-0.70, travel band, crossings, persistence) |
| Edge gate | `MIN_EDGE=0.03` | Was 0.04; ~14 -> ~21 entries/day at equal or better quality once tranche is on |
| **Tranche entry** | `MODEL_TRANCHE_ENABLED=true` | Buy **34%** of the stake at the signal; buy the other **66%** 15s later only if spot is still on our side of the strike (ask <= 0.92) |
| **Decisive-lead gate** | `MODEL_TRANCHE_MIN_Z=0.75` | The add-on also needs `lead / (sigma_1m * sqrt(tau/60)) >= 0.75`: a thin lead with a long way to go does not get the second leg |
| Sizing | `SIZING_MODE=fraction`, `STAKE_FRACTION=0.10`, `STAKE_CAP_USDT=0` | 10% of the live bankroll per round, compounding, no dollar cap |
| Exits | `MODEL_EXIT_BEHIND_BPS=0` (reaction half disarmed) | **No sell rule is armed**: positions ride to settlement |
| Risk | `daily_loss_cap_pct=0.30` | Halts new entries for the rest of the UTC day; does not close open positions |

### Why no exit, and why tranche instead

After a model entry the book keeps **under**pricing our token even once it goes
against us: a bid of 0.35 still wins 40-56% of the time. So every sale hands back
the edge. About 65 exit rules over 11 families (bid stops, trailing stops,
model-value exits, sigma-scaled deficits, trend-confirmed stops, stale-book
exits, a learned continuation value, hedging with the opposite token or a BTC
perp, pausing after losses) were tested on the live-parity entries, a separate
holdout of model entries, and 3,538 market rounds. None beat holding, and stops
make the worst week *deeper* by cutting winners in clusters.

The losers are therefore never fully **bought** instead of being sold:

- A trade that turns in the first 15s loses only its first leg (~34% of the stake).
- The add-on is itself a good buy: high bids are underpriced too.
- With the z-gate, full-stake losses fall from ~1 trade in 5 to ~1 in 33 on the
  replay, and negative days from ~27% to ~20%. Profit per $1 staked falls
  ~40%, which the larger 10% stake buys back: at roughly equal drawdown risk
  the gated setup grows faster than the ungated one.

The drop in full losses is the robust part (it falls steadily as the z
threshold rises, on both populations). The exact growth and drawdown figures
come from resampling 13 days of tape and should be read as direction, not
forecast.

### Measured and rejected (do not re-run without a new mechanism)

- **Other markets** (ETH/BNB 5m, BTC/ETH/BNB 15m): all live, but the model finds
  no edge. A 540-config grid per market, tuned on 09-08..15 and judged on
  09-16..24, picked noise (rank correlation tune-vs-validate +0.00 on ETH-5m).
- **Relaxing the persistence or crossings gates**: the extra entries lose, or
  their gain sits in one half of the tape only.
- **Lower `min_quote` to catch "lagging asks"**: the added entries lose, including
  the high-edge ones. A cheap ask with a big model edge is mostly model error.
- **Edge-weighted stakes, drawdown brakes**: no better than a flat fraction at
  the same average stake.
- **Book depth** at the best ask is ~$41 (p25) to ~$133 (median). Stakes far above
  that fill worse than the replay assumes.

### Execution fixes that the numbers above depend on

- **Booked shares** are the venue's holding when a MARKET order fills worse than
  its quote (it can hold ~1.5% fewer shares than `amountOut`).
- **No order is called dead without the venue's verdict.** A MARKET bundle can
  take 11s to fill and never appears in the working-orders list; the buy path
  polls the order's own history until it is terminal (10s ceiling) and only a
  FAILED verdict authorises a retry.
- **Sales** have no minimum notional, step the size down to 0.01 share when
  refused, and treat an unknown outcome as pending rather than unfilled.
- **One round = one trade**: a two-leg tranche round keeps a settlement row per
  fill but is counted once in the stats, the risk guard's streak, the SETTLE log
  line, the notification and the redeem, even when its legs settle on
  different passes.

## Pricing models

The default fair-value model is the closed-form digital option. The strategy
bots from
[polymarket-bot-arena](https://github.com/ThinkEnigmatic/polymarket-bot-arena)
have been ported into `btcpredict/models/` as drop-in alternatives, adapted to
Binance data (1m klines, taker-buy order flow as the sentiment feed):

| Model | Source bot | Binance adaptation |
|---|---|---|
| `digital` | — | original driftless digital option (baseline) |
| `momentum` | `bot_momentum.py` | candle momentum + volume confirmation |
| `mean_rev` | `bot_mean_rev.py` | z-score + RSI reversion |
| `sentiment` | `bot_sentiment.py` | taker-buy ratio + its momentum replaces X/social data |
| `hybrid` | `bot_hybrid.py` | weighted ensemble of momentum + mean_rev + sentiment |
| `phantom` | `bot_phantom.py` | EMA trend + breakout, ATR-gated |
| `sniper` | `bot_sniper.py` | strict momentum confirmation, trades rarely |
| `btc_maker` | `bot_btc_maker.py` | directional-lean half of the maker bot (anchor = 0.5) |

Select one with `pricing_model` in `.env` (or `PRICING_MODEL`). A model may
decline to emit a view on a tick; the orchestrator treats that as no trade.

`fee_zone_maker` is deliberately absent: it is market-making on Polymarket's
taker-fee curve and contains no directional prediction. `meanrev_sl` /
`meanrev_tp` share `mean_rev`'s analyzer and differ only in exit rules that do
not apply to a hold-to-settlement venue.

To compare every model head-to-head over the same kline history:

```bash
python -m tools.backtest_models --days 10            # all models, all minute marks
python -m tools.backtest_models --models momentum phantom
```

This reports Brier/log-loss/reliability per model per minute mark, with the
emit rate alongside — a model that wins by never trading is easy to spot.

## How it works

```
Binance spot (WS)  ─┐
                    ├─> digital option model ─> fair P(Up) ─┐
round strike (API) ─┘                                       ├─> edge gate ─> broker
                                                            │   (fee+spread
market quote (API) ─────────────────────────────────────────┘    +margin)
```

Entries are evaluated on every tick inside the round's entry window (tau
60-210s). Candle-open gating (`trade_on_candle_open`) exists but is off.

Fair value is the closed-form digital option `P = Φ(ln(S/K) / σ√τ)`. There is no
drift term and no ML: measured drift over 5 minutes is indistinguishable from
zero, and everything predictive comes from moneyness and time-to-expiry.
Validated over 2,831 rounds with out-of-sample rolling sigma — Brier **0.1104**
at 60 s to go versus 0.25 for a coinflip, reliability 0.0021.

## Quick start

```bash
pip install -r requirements.txt
python -m btcpredict.main --mode record     # Phase 0: record only, no orders
python -m tools.analyze                     # what the recording says
python -m pytest -q                         # ~2,200 tests
```

The suite runs without your `.env` (`tests/conftest.py` sets
`BTCPREDICT_ENV_FILE=none`), so shipped defaults are what it tests. To replay
against a live profile, point `BTCPREDICT_ENV_FILE` at that `.env` copy.

## Modes

| Mode | What it does |
|---|---|
| `record` | **Default.** Feeds + storage only. No signals, no orders. This is Phase 0. |
| `paper` | Full signal path against a simulated broker with a pessimistic fill model. |
| `live` | Same path against the real venue. Refuses to start until endpoints are configured. |

Mode is `--mode` or `TRADING_MODE` in `.env`. Live never happens by accident:
it requires an explicit flag *and* configured endpoints, and will not silently
fall back to paper.

## Phases

1. **Phase 0 — record.** Let the recorder run for days. It answers the two
   questions that decide everything: what the *effective spread* is (there is no
   public order book, so the displayed price is not executable), and whether the
   2% fee applies to notional or to winnings.
2. **Phase 1 — analyse.** `tools.analyze` reports model-vs-market Brier (the
   go/no-go), quote lag, oracle basis, and a cost-sensitivity curve. **If the
   rule only profits at a spread tighter than Phase 0 measured, stop.**
3. **Phase 2 — paper.** Needs ≥500 simulated rounds, positive EV, and model
   Brier beating the market quote's own Brier.
4. **Phase 3 — live at $1.** Configure the endpoints in
   `btcpredict/execution/live.py`, then run with guards armed. The first
   session is a fee/fill measurement exercise: reconcile every fill against its
   paper twin.

## Layout

```
btcpredict/
  config.py          settings; reads .env (accepts the original lowercase key names)
  feeds/             binance_spot.py (WS mid), market_feed.py (prediction API)
  pricing/           digital.py (fair value), vol.py (EWMA + seasonality)
  models/            pluggable pricing models (digital + arena bots ported)
  edge/signal.py     fee-aware EV, hurdle, side selection, fractional Kelly
  execution/         base.py (Broker protocol), paper.py, live.py
  risk/guards.py     loss caps, streak halt, one-position-per-round
  calibration/       Brier, log loss, reliability diagram, PnL summary
  store/db.py        SQLite: rounds, quotes, decisions, fills, settlements
  main.py            orchestrator
tools/
  harvest.py             pull settled rounds (oracle ground truth)
  backtest_models.py     head-to-head comparison of all pricing models
  backtest_calibration.py validate the model on kline history
  analyze.py             Phase 0/1 reports
  model_knob_sweep.py    replays the LIVE model rule on the tape (gates read from
                         settings); --mode funnel / sizegrid / --dd-detail
```

## Live trading works

`btcpredict/execution/live.py` is complete and has placed real orders. Endpoints
live under `/sapi/v1/w3w/wallet/prediction/` and were recovered from the docs'
own `llms.txt` index. Three parameter values are non-obvious and were established
by probing the live venue — see FINDINGS.md before changing any of them:

- `amountIn` is **18-decimal wei** (`"1"` is rejected as "amount too small")
- `timeInForce` must be **FOK** (IOC/GTC → -1102)
- `accountType` must be **SPOT** (CeDeFi → -3026)

Winnings do not auto-credit; `batch-redeem` must be called with a comma-separated
`tokenIds`. The bot does this automatically after a win, and a claim sweep runs
every three minutes (`sweep_claims`), so a claim the venue rejects as not-yet-
final is retried on that clock instead of waiting for the next win.

Note that `apiRestrictions` does **not** report prediction permissions — a key
showing every trading flag `false` can still trade prediction markets. Use
`wallet/list` as the access check.

## Redeem API contract changed (2026-09-08 evening)

`batch-redeem` started returning `-1022 signature not valid` for requests that
had worked all day. Two facts were established by probing:

1. **Raw commas in the signed string.** The venue verifies the signature over
   `tokenIds` with *unencoded* commas. `urlencode`-produced `%2C` fails the
   signature check. Fix: replace `%2C` with `,` in the signed query.
2. **One token per call.** A batch containing a not-yet-final token fails the
   whole request with `-9000 SYSTEM_ERROR`; per-token calls redeem the rest.
   `redeem()` now claims tokens one at a time.

Both are locked in `execution/live.py`.

## Guard bypass regression (fixed 2026-09-09)

The venue-position pre-buy check was inserted into the gate chain's `elif`
sequence, before the `else` branch that consulted the risk guard. In live
mode that branch always consumed the chain, so `guard.check()` was never
called: the daily loss cap logged `RISK HALT` and kept trading. Fixes: the
guard is now consulted unconditionally, and `RiskGuard.seed_daily()` persists
the day's PnL across restarts. The live bot now halts for the rest of the UTC
day when the cap trips.

## Known risks

1. **Deep favourites are a trap, and the gate now blocks them.** Buying at 0.98
   wins $0.02 per $1 risked and needs ~98.6% accuracy; measured top-bucket
   reliability is 0.964 against 0.973 predicted. `max_quote` (0.90) and
   `min_edge` (0.03) exist because a live trade walked into exactly this.
2. **Small sample.** The replay covers ~13 days of tape; the tranche + z-gate
   setup has only a handful of live trades. On the 104 rounds where live and
   replay both traded, live earned +0.189 per $1 against the replay's +0.118, so
   the replay looks conservative, but that is one comparison, not proof.
3. **The best signal window is the worst execution window.** The model is
   sharpest in the final 60 s, exactly when liquidity drops (~$34k → ~$12k).
   `min_tau_s` keeps us out of the last 30 s.
4. **Edge decay.** A lag-based edge is exactly the kind other bots compete away.
   Re-run `tools.analyze` regularly; a shrinking lag correlation is the warning.
5. **Fee varies per quote** (45-64 bps observed). It is read from the live quote,
   never assumed — the market metadata's 200 bps is wrong.
