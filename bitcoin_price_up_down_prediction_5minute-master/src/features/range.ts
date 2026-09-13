/**
 * Distance to recent high/low — local range position in [0, 1]
 */

import { OHLCV1m } from "../types";

const MS_5M = 5 * 60 * 1000;

/**
 * Range position: (price - low) / (high - low) over last 5 minutes.
 * Uses candles to get high/low; current price can be passed for real-time.
 */
export function rangePosition(
  candles: OHLCV1m[],
  now: number,
  currentPrice: number
): number {
  const cutoff = now - MS_5M;
  const inRange = candles.filter((c) => c.ts >= cutoff);
  if (inRange.length === 0) return 0.5;
  const high = Math.max(...inRange.map((c) => c.high));
  const low = Math.min(...inRange.map((c) => c.low));
  const range = high - low;
  if (range <= 0) return 0.5;
  return Math.max(0, Math.min(1, (currentPrice - low) / range));
}
