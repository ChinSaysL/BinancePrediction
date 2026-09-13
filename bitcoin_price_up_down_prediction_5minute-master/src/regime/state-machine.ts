/**
 * Regime state machine — trending, ranging, vol_compression, post_liquidation
 */

import { FeatureVector, RegimeState, RegimeType } from "../types";
import { ThresholdsConfig, DEFAULT_THRESHOLDS } from "../config";

export interface RegimeInput {
  features: FeatureVector;
  /** Optional: true if we just detected a liquidation-style spike */
  postLiquidationSpike?: boolean;
  /** Optional: variance ratio short/long (e.g. sigma_short^2 / sigma_long^2) for trending */
  varianceRatio?: number;
}

/**
 * Classify current regime from features and optional flags.
 */
export function classifyRegime(
  input: RegimeInput,
  config: Partial<ThresholdsConfig> = {}
): RegimeState {
  const cfg = { ...DEFAULT_THRESHOLDS, ...config };
  const { features, postLiquidationSpike, varianceRatio } = input;
  const now = features.ts;

  if (postLiquidationSpike) {
    return {
      regime: "post_liquidation",
      ts: now,
      enteredAt: now,
    };
  }

  if (features.volCompress >= cfg.volCompress) {
    return {
      regime: "vol_compression",
      ts: now,
      enteredAt: now,
    };
  }

  const absCvd = Math.abs(features.cvdRatio60);
  const vr = varianceRatio ?? 1;
  const isTrending =
    absCvd >= cfg.cvdTrending && vr >= 0.7;

  if (isTrending) {
    return {
      regime: "trending",
      ts: now,
      enteredAt: now,
    };
  }

  const rangeExtreme =
    features.rangePos <= cfg.rangeLow || features.rangePos >= cfg.rangeHigh;
  const lowVol = features.volCompress > 0.2;
  if (!rangeExtreme && lowVol) {
    return {
      regime: "ranging",
      ts: now,
      enteredAt: now,
    };
  }

  return {
    regime: "unknown",
    ts: now,
    enteredAt: now,
  };
}

/**
 * Whether we're still in post-liquidation cooldown (no new trend chase).
 */
export function isInPostLiqCooldown(
  state: RegimeState,
  now: number,
  config: Partial<ThresholdsConfig> = {}
): boolean {
  const cfg = { ...DEFAULT_THRESHOLDS, ...config };
  if (state.regime !== "post_liquidation") return false;
  const elapsedSec = (now - state.enteredAt) / 1000;
  return elapsedSec < cfg.postLiqCooldownSec;
}

/**
 * Whether to allow a trade in this regime (ranging: only at range extremes with flow).
 */
export function regimeAllowsTrade(
  regime: RegimeType,
  features: FeatureVector,
  direction: "UP" | "DOWN",
  config: Partial<ThresholdsConfig> = {}
): { allow: boolean; reason?: string } {
  const cfg = { ...DEFAULT_THRESHOLDS, ...config };

  if (regime === "post_liquidation") {
    return { allow: false, reason: "post_liquidation_cooldown" };
  }

  if (regime === "ranging") {
    const atLow = features.rangePos <= cfg.rangeLow;
    const atHigh = features.rangePos >= cfg.rangeHigh;
    const flowMatch =
      (direction === "UP" && features.cvdRatio60 > cfg.obiConfirm) ||
      (direction === "DOWN" && features.cvdRatio60 < -cfg.obiConfirm);
    if ((atLow && direction === "UP" && flowMatch) || (atHigh && direction === "DOWN" && flowMatch)) {
      return { allow: true };
    }
    return { allow: false, reason: "ranging_no_extreme_flow" };
  }

  if (regime === "vol_compression") {
    return { allow: true }; // allow but caller should reduce size
  }

  return { allow: true };
}
