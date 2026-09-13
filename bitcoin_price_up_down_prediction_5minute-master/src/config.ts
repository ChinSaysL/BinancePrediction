/**
 * Configurable thresholds and constants — tune on validation, not test
 */

export interface ThresholdsConfig {
  /** P(up) above this → UP */
  pHigh: number;
  /** Trade only if max(P(up), 1-P(up)) >= this */
  pConfidence: number;
  /** CVD ratio threshold for trending regime */
  cvdTrending: number;
  /** Volatility compression score above this = compression regime */
  volCompress: number;
  /** |OBI| above this to confirm direction */
  obiConfirm: number;
  /** Range position: allow trade in ranging only if outside [rangeLow, rangeHigh] */
  rangeLow: number;
  rangeHigh: number;
  /** Post-liquidation cooldown (seconds) */
  postLiqCooldownSec: number;
  /** Short vol window (seconds) for variance ratio */
  volShortWindowSec: number;
  /** Long vol window (seconds) */
  volLongWindowSec: number;
  /** OBI exponential decay lambda (per tick distance) */
  obiLambda: number;
  /** Levels to use for simple OBI (top N) */
  obiLevels: number;
}

export const DEFAULT_THRESHOLDS: ThresholdsConfig = {
  pHigh: 0.58,
  pConfidence: 0.58,
  cvdTrending: 0.15,
  volCompress: 0.4,
  obiConfirm: 0.05,
  rangeLow: 0.2,
  rangeHigh: 0.8,
  postLiqCooldownSec: 180,
  volShortWindowSec: 60,
  volLongWindowSec: 300,
  obiLambda: 0.2,
  obiLevels: 5,
};

export interface RiskConfig {
  /** Max position size multiplier cap */
  maxSizeMultiplier: number;
  /** Min ATR multiple for stop (or equivalent) */
  stopAtrMultiple: number;
  /** Max trades per hour */
  maxTradesPerHour: number;
  /** Scale size by 1/vol up to this cap */
  volScaleCap: number;
}

export const DEFAULT_RISK: RiskConfig = {
  maxSizeMultiplier: 1,
  stopAtrMultiple: 1.5,
  maxTradesPerHour: 12,
  volScaleCap: 2,
};
