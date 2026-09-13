/**
 * VWAP and distance from VWAP (raw and z-score)
 */

import { OHLCV1m } from "../types";

const MS_1H = 60 * 60 * 1000;

/**
 * 1h rolling VWAP from 1m candles: sum(typical * vol) / sum(vol).
 */
export function vwap1h(candles: OHLCV1m[], now: number): number | null {
  const cutoff = now - MS_1H;
  const inWindow = candles.filter((c) => c.ts >= cutoff && c.ts <= now);
  if (inWindow.length === 0) return null;
  let sumPv = 0;
  let sumV = 0;
  for (const c of inWindow) {
    const typical = (c.high + c.low + c.close) / 3;
    sumPv += typical * c.volume;
    sumV += c.volume;
  }
  if (sumV <= 0) return null;
  return sumPv / sumV;
}

/**
 * Distance from VWAP as fraction: (price - vwap) / vwap.
 */
export function distVwap(price: number, vwap: number | null): number {
  if (vwap == null || vwap <= 0) return 0;
  return (price - vwap) / vwap;
}

/**
 * Distance in volatility units (z): (price - vwap) / sigma_1h.
 * Sigma from close returns of 1m candles.
 */
export function distVwapZ(
  price: number,
  vwap: number | null,
  candles: OHLCV1m[],
  now: number
): number | null {
  if (vwap == null || vwap <= 0) return null;
  const cutoff = now - MS_1H;
  const inWindow = candles.filter((c) => c.ts >= cutoff).sort((a, b) => a.ts - b.ts);
  if (inWindow.length < 2) return null;
  const returns: number[] = [];
  for (let i = 1; i < inWindow.length; i++) {
    const prev = inWindow[i - 1].close;
    if (prev <= 0) continue;
    returns.push((inWindow[i].close - prev) / prev);
  }
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
  const variance =
    returns.reduce((s, r) => s + (r - mean) ** 2, 0) / (returns.length - 1) || 0;
  const sigma = Math.sqrt(variance) * vwap;
  if (sigma <= 0) return null;
  return (price - vwap) / sigma;
}
