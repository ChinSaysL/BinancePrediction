# BTC 5m Direction Model

Microstructure-based framework to predict **UP** / **DOWN** / **NO_TRADE** for BTCUSDT perpetual over a 5-minute horizon. No RSI/MACD; uses order flow, order book, OI, volatility compression, range position, and VWAP.

## Structure

- **`src/features`** — Feature engineering: CVD (60s), OBI (depth-weighted), OI delta 1m %, volatility compression, range position (5m), distance to VWAP (+ optional z-score), CVD acceleration.
- **`src/regime`** — Regime state machine: trending, ranging, vol_compression, post_liquidation; cooldowns and trade-allowed logic.
- **`src/decision`** — Decision engine: P(up) (heuristic or plug-in model), confidence gating, OBI confirmation, regime filter, size multiplier.
- **`src/config`** — Thresholds and risk (tune on validation).

## Usage

```ts
import { runPipeline } from "./index";

const result = runPipeline(
  {
    trades: [...],       // { ts, price, size, side }
    orderBook: { ts, bids, asks },
    oiSeries: [...],     // { ts, oi }
    candles1m: [...],   // { ts, open, high, low, close, volume }
    currentPrice: 97_000,
    now: Date.now(),
  },
  {
    thresholds: { pHigh: 0.58, pConfidence: 0.58 },
    useHeuristicPUp: true,
    // modelPUp: 0.62,  // when using external model
  }
);

console.log(result.decision);
// { direction: "UP" | "DOWN" | "NO_TRADE", pUp, confidence, sizeMultiplier, reason? }
```

## Plugging in a model

1. Compute features with `computeFeatures(input)`.
2. Get regime with `classifyRegime({ features, ... })`.
3. Call your model to get `P(up)` from the feature vector.
4. Call `decide(features, regime, modelPUp, config)`.

## Runner (live predictions + accuracy)

- **`npm start`** or **`npm run dev`** — run the bot: every 1 minute it fetches Binance BTCUSDT data, runs the model, logs a prediction; 5 minutes later it resolves each prediction (actual = price up or down), appends one JSON line per result to `logs/results.jsonl`, and updates `logs/accuracy.json` with **accuracy rate** (correct / total directional predictions).
- **Logs**
  - `logs/results.jsonl` — one JSON object per resolved prediction: `tsPredict`, `tsResolve`, `prediction`, `actual`, `correct`, `priceAtPredict`, `priceAtResolve`, `pUp`, `regime`.
  - `logs/accuracy.json` — `totalDirectionalPredictions`, `correct`, `accuracyRate` (%), last 50 results.

## Scripts

- `npm run build` — compile to `dist/`
- `npm start` — run bot (node dist/runner.js)
- `npm run dev` — run bot with ts-node
- `npm run watch` — tsc --watch
