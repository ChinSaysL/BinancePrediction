/**
 * Open Interest delta — 1m % change (and optional acceleration)
 */

import { OIDataPoint } from "../types";

const MS_1M = 60_000;

/**
 * Find OI at time and at (time - 1min). Returns % change.
 */
export function oiDeltaPct1m(oiSeries: OIDataPoint[], now: number): number | null {
  const current = findNearest(oiSeries, now);
  const past = findNearest(oiSeries, now - MS_1M);
  if (current == null || past == null || past.oi <= 0) return null;
  return ((current.oi - past.oi) / past.oi) * 100;
}

/**
 * Second derivative: (OI% now) - (OI% 1m ago).
 */
export function oiAcceleration(
  oiSeries: OIDataPoint[],
  now: number
): number | null {
  const nowPct = oiDeltaPct1m(oiSeries, now);
  const pastPct = oiDeltaPct1m(oiSeries, now - MS_1M);
  if (nowPct == null || pastPct == null) return null;
  return nowPct - pastPct;
}

function findNearest(series: OIDataPoint[], t: number): OIDataPoint | null {
  if (series.length === 0) return null;
  let best = series[0];
  let bestDiff = Math.abs(series[0].ts - t);
  for (let i = 1; i < series.length; i++) {
    const d = Math.abs(series[i].ts - t);
    if (d < bestDiff) {
      bestDiff = d;
      best = series[i];
    }
  }
  return bestDiff <= MS_1M * 1.5 ? best : null;
}
