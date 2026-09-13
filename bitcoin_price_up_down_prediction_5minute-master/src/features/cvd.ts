/**
 * Cumulative Volume Delta — rolling 60s (and optional 30s for acceleration)
 */

import { Trade } from "../types";

const MS_60 = 60_000;
const MS_30 = 30_000;

/**
 * Compute CVD over [now - windowMs, now] and optionally total volume for ratio.
 */
export function cvdAndVolume(
  trades: Trade[],
  now: number,
  windowMs: number = MS_60
): { cvd: number; totalVolume: number } {
  const cutoff = now - windowMs;
  let cvd = 0;
  let totalVolume = 0;
  for (const t of trades) {
    if (t.ts < cutoff) continue;
    const signed = t.side === "buy" ? 1 : -1;
    cvd += signed * t.size;
    totalVolume += t.size;
  }
  return { cvd, totalVolume };
}

/**
 * CVD ratio in [-1, 1]: CVD / totalVolume (0 if no volume).
 */
export function cvdRatio60(trades: Trade[], now: number): number {
  const { cvd, totalVolume } = cvdAndVolume(trades, now, MS_60);
  if (totalVolume <= 0) return 0;
  return Math.max(-1, Math.min(1, cvd / totalVolume));
}

/**
 * Flow acceleration: CVD(30s) - CVD(previous 30s).
 */
export function cvdAcceleration(trades: Trade[], now: number): number {
  const { cvd: cvdRecent } = cvdAndVolume(trades, now, MS_30);
  const { cvd: cvdPrior } = cvdAndVolume(trades, now - MS_30, MS_30);
  return cvdRecent - cvdPrior;
}
