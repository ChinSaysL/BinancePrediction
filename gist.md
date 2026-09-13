# Polymarket BTC 5-Minute Up/Down Trading Bot — Build Guide

## What This Bot Does

This bot trades Polymarket's "BTC Up or Down" 5-minute binary markets. Every 5 minutes, Polymarket opens a market asking: "Will BTC be higher or lower than the opening price when this 5-minute window closes?" You buy "Up" or "Down" tokens at some price (e.g. $0.50–$0.95), and if you're right, each token pays out $1.00. If you're wrong, you lose your bet.

The bot uses technical analysis on real-time Binance BTC price data to predict the outcome, then places the trade on Polymarket right before the window closes — when we have the most information but (ideally) before the token price has fully priced in the outcome.

---

## Architecture Overview

The bot has 6 files:

| File | Purpose |
|------|---------|
| `bot.py` | Main trading engine — timing, order placement, modes, bankroll management |
| `strategy.py` | Technical analysis — composite signal from 7 weighted indicators |
| `compare_runs.py` | Backtesting tool — tests multiple configs, outputs Excel comparison |
| `backtest.py` | Historical candle fetcher (used by compare_runs.py) |
| `setup_creds.py` | One-time setup — derives Polymarket API credentials from private key |
| `auto_claim.py` | Background auto-claimer for winning positions (uses Playwright) |

### Dependencies

```
py-clob-client==0.34.5   # Polymarket's official CLOB trading client
python-dotenv>=1.0.0      # .env file loading
requests>=2.31.0          # HTTP calls to Binance + Polymarket APIs
playwright>=1.40.0        # Browser automation for auto-claiming wins
openpyxl>=3.1.0           # Excel output for comparison tool
```

---

## Core Concept: Clock-Based Snipe Timing

Polymarket's BTC 5-min markets follow fixed timestamps divisible by 300 (Unix epoch). The bot doesn't search for markets — it **calculates** which market is active based on the clock.

```
window_ts = now - (now % 300)        # Current window start
close_time = window_ts + 300          # Window closes exactly 5 min later
slug = f"btc-updown-5m-{window_ts}"  # Polymarket slug is deterministic
```

The bot sleeps until **T-10 seconds** before the window closes, then runs TA and fires. At T-10s, the BTC price direction is largely locked in — there isn't enough time for a major reversal. The tradeoff: tokens may be pricier (the market has partially priced in the outcome), but accuracy is much higher.

---

## The Strategy: Composite Weighted Signal

The strategy (`strategy.py`) produces a single score from 7 indicators. Positive score = Up, negative = Down. Each indicator has a weight reflecting its predictive power for 5-minute binary outcomes.

### Indicator Breakdown

**1. Window Delta (weight 5–7) — THE dominant signal**

This is the most important indicator by far. It answers the exact question the market is asking: "Is BTC up or down vs the window open price?"

```
window_pct = (current_price - window_open_price) / window_open_price * 100

> 0.10%  → weight 7 (decisive — nearly certain)
> 0.02%  → weight 5 (strong)
> 0.005% → weight 3 (moderate)
> 0.001% → weight 1 (slight)
```

At T-10s, if BTC is already up 0.10%+ from window open, it almost never reverses in 10 seconds. This indicator must dominate everything else — we increased its weight from 3 to 5-7 after observing the bot bet the wrong direction when noisy short-term indicators overruled a clear window delta.

**2. Micro Momentum (weight 2)** — Last 2 candles direction (1-min candles). Quick read on recent price movement.

**3. Acceleration (weight 1.5)** — Is momentum building or fading? Compares the latest candle's move to 2 candles ago. "Accelerating upward" vs "Decelerating upward (fading)."

**4. EMA Crossover 9/21 (weight 1)** — Standard short-term trend indicator. EMA9 > EMA21 = bullish.

**5. RSI 14-period (weight 1–2)** — Overbought (>75, weight 2) and oversold (<25, weight 2) extremes. Neutral range gets 0 weight.

**6. Volume Surge (weight 1)** — If recent 3-bar average volume is 1.5x the prior 3-bar average, it confirms the current direction.

**7. Real-Time Tick Trend (weight 2)** — This uses the bot's own 2-second price polling (not candles) to detect micro-trends between 1-minute candle updates. Requires 60%+ directional consistency across accumulated ticks and >0.005% move to trigger.

### Confidence Calculation

```
confidence = min(abs(score) / 7.0, 1.0)
```

We divide by 7 instead of 10 because in a 5-minute market, the long-term indicators (EMA, RSI) are less relevant — the window delta is king. This makes it easier to reach meaningful confidence levels.

---

## Trading Modes

### Safe Mode (default)
- **Bet size:** 25% of bankroll per trade
- **Min confidence:** 30%
- **Philosophy:** Survive losing streaks. Even 4 consecutive losses only costs ~68% of bankroll. Slow compounding.

### Aggressive Mode
- **Bet size:** All proceeds (profits above original investment). First trade risks the original bankroll, then the original is protected.
- **Min confidence:** 20%
- **Philosophy:** Compound profits fast, protect the original. One bad streak wipes gains but not the principal.

### Degen Mode
- **Bet size:** ALL-IN every trade. Entire bankroll, every time.
- **Min confidence:** 0% (takes every trade regardless)
- **Philosophy:** Double or nothing. At T-10s, tokens near $0.50 = 2x payout. Our TA gives a slight edge over a pure coin flip. You'll bust often, but when you streak, you streak hard.

---

## The TA Loop (Snipe Window Behavior)

The bot doesn't just check once and trade. Starting at T-10s, it enters a polling loop:

1. **Run `analyze()`** every 2 seconds with the latest Binance data + accumulated tick prices
2. **Track the best signal** seen across all checks (highest |score|)
3. **Spike detection:** If the score jumps ≥1.5 between consecutive checks, that's the "teetering moment" — fire immediately
4. **Confidence threshold:** If confidence meets the mode's minimum, fire
5. **T-5s hard deadline:** If we haven't fired by T-5s, use the best signal we saw (never skip a trade)

This loop catches the moment the market tips one direction — especially useful when it's been flat and suddenly moves.

---

## Order Execution

### Primary: FOK Market Buy
Fill-or-Kill market buy for the exact dollar amount on the correct token (Up or Down based on signal direction). Retries every 3 seconds until the window closes.

### Fallback: GTC Limit Buy at $0.95
When the winning token has no asks (no sell-side liquidity), the bot posts a GTC limit buy at $0.95 — becoming the liquidity itself. If filled, profit is $0.05/share when the token resolves to $1.00.

**Polymarket minimum:** 5 shares per order. At $0.95/share, minimum spend is $4.75.

---

## Dry Run Mode

`--dry-run` runs against real live data without placing actual trades:

1. Full TA loop with real Binance price data at T-10s
2. **Delta-based token pricing** simulates what you'd actually pay on Polymarket (see pricing model below)
3. Waits for the window to actually close
4. Checks what BTC **really did** via Binance API
5. Scores win/loss against reality with realistic profit margins
6. If bankroll drops below minimum bet, resets and keeps collecting data

### Token Pricing Model (for dry run + backtesting)

Market makers see the same BTC delta we do. The larger the move from window open, the more the winning token costs:

```
delta < 0.005% → $0.50   (coin flip, nobody knows)
delta ~ 0.02%  → $0.55   (slight lean)
delta ~ 0.05%  → $0.65   (moderate edge)
delta ~ 0.10%  → $0.80   (strong, market pricing it in)
delta ~ 0.15%+ → $0.92–0.97 (nearly certain)
```

This is a piecewise linear model based on observed live Polymarket trading. It prevents backtests from being unrealistically optimistic (fixed $0.50 tokens = fake 2x every win).

---

## Comparison/Backtesting Tool

`compare_runs.py` backtests the current strategy across multiple configurations:

- **9 confidence thresholds** × **3 modes** (flat, safe, aggressive) = 27 configs
- Runs the actual `strategy.py analyze()` function on historical candles
- Simulates bankroll evolution with realistic token pricing
- Outputs an Excel workbook with 3 sheets: Summary, Best Config Trades, Bankroll Curves

```bash
python compare_runs.py --hours 72 --output results.xlsx
```

---

## Market Discovery

Markets are found by constructing the slug directly:

```
slug = f"btc-updown-5m-{window_ts}"
```

One API call to `gamma-api.polymarket.com/events?slug=...` returns the market with Up/Down token IDs and current prices. No scanning or searching required.

---

## Resolution Checking

### Primary: Binance (instant, reliable)
After the window closes, fetch the 1-min candle at window start (open price) and window end (close price) from Binance. Compare: close >= open → Up wins.

### Fallback: Polymarket API
If Binance is unreachable, poll Polymarket's API for the market's outcome prices. The winning outcome goes to ~$1.00.

---

## Setup Requirements

1. **Polymarket account** with USDC on Polygon network
2. **Private key** exported from your Polymarket wallet
3. **API credentials** — derived from the private key using `setup_creds.py`
4. **`.env` file** with your credentials and settings:

```env
POLY_PRIVATE_KEY=0x...your_private_key...
POLY_API_KEY=...derived...
POLY_API_SECRET=...derived...
POLY_API_PASSPHRASE=...derived...
POLY_FUNDER_ADDRESS=0x...your_proxy_wallet...
POLY_SIGNATURE_TYPE=1
STARTING_BANKROLL=1.0
MIN_BET=1.0
BOT_MODE=safe
```

5. **Python 3.10+** with a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Running

```bash
# Live trading (safe mode)
python bot.py --mode safe

# Dry run — real data, no real trades
python bot.py --dry-run --mode safe

# Degen dry run — watch it double or bust repeatedly
python bot.py --dry-run --mode degen

# Single trade cycle
python bot.py --dry-run --once

# Limit number of trades
python bot.py --dry-run --max-trades 20
```

---

## Key Lessons Learned

1. **Window delta is king.** Short-term TA (EMA, RSI) is noisy at the 5-minute scale. The window delta — "is BTC up or down vs window open?" — is the only indicator that directly answers the market's question. Weight it 5-7x.

2. **Entry timing is everything.** Too early (T-150s) = cheaper tokens but price can reverse. Too late (T-5s) = tokens already at $0.95+, no profit margin. T-10s is the sweet spot.

3. **Confidence should never skip trades.** The original bot checked once and gave up if confidence was low. Now it loops and always trades by T-5s. Better to trade at low confidence than miss a window.

4. **Token pricing makes or breaks backtests.** Fixed $0.50 token price shows 80%+ win rate with 2x returns = astronomical fake profits. Delta-based pricing reflects reality: when we're confident, so is the market, and tokens cost more.

5. **Polymarket has minimums.** 5 shares minimum per order. With $0.95 limit buys, that's $4.75 minimum. Low bankrolls can't use the limit order fallback.

6. **Binance rate limits matter.** The bot hits Binance for candles every 2 seconds during the TA loop. If you get rate-limited, the bot catches the exception and retries instead of crashing.