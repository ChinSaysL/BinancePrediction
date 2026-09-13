/**
 * Volatility compression score — variance ratio (short/long) or ATR-based
 */

import { OHLCV1m } from "../types";

function variance(returns: number[]): number {
  if (returns.length < 2) return 0;
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
  return returns.reduce((s, r) => s + (r - mean) ** 2, 0) / (returns.length - 1);
}

/**
 * Returns from 1m closes: (close[i] - close[i-1]) / close[i-1]
 */
function returnsFromCandles(candles: OHLCV1m[]): number[] {
  const out: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const prev = candles[i - 1].close;
    if (prev <= 0) continue;
    out.push((candles[i].close - prev) / prev);
  }
  return out;
}

/**
 * Volatility compression: 1 - (var_short / var_long). High = compressed.
 * Uses 1m candles: short = last shortWindowMins, long = last longWindowMins.
 */
export function volCompressScore(
  candles: OHLCV1m[],
  now: number,
  shortWindowSec: number = 60,
  longWindowSec: number = 300
): number {
  const cutoffLong = now - longWindowSec * 1000;
  const cutoffShort = now - shortWindowSec * 1000;
  const inLong = candles.filter((c) => c.ts >= cutoffLong);
  const inShort = inLong.filter((c) => c.ts >= cutoffShort);
  const retLong = returnsFromCandles(inLong);
  const retShort = returnsFromCandles(inShort);
  const varLong = variance(retLong);
  const varShort = variance(retShort);
  if (varLong <= 0) return 0;
  const ratio = varShort / varLong;
  return Math.max(0, Math.min(1, 1 - ratio));
}
