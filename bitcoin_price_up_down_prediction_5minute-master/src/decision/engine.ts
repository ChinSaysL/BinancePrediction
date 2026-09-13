/**
 * Decision engine — P(up), confidence gating, regime filter, size multiplier
 */

import {
  FeatureVector,
  RegimeState,
  Decision,
  Direction,
  RegimeType,
} from "../types";
import { ThresholdsConfig, RiskConfig, DEFAULT_THRESHOLDS, DEFAULT_RISK } from "../config";
import { isInPostLiqCooldown, regimeAllowsTrade } from "../regime";

export type ProbabilitySource = "heuristic" | "model";

/**
 * Heuristic P(up) from features (no ML). Use as baseline or replace with model output.
 * Combines: CVD ratio, OBI, range position, distVWAP (mean reversion), OI delta.
 */
export function heuristicPUp(features: FeatureVector): number {
  let score = 0.5;

  score += features.cvdRatio60 * 0.25;
  score += features.obi * 0.2;
  if (features.rangePos > 0.8) score += 0.1;
  if (features.rangePos < 0.2) score -= 0.1;
  if (features.distVwap > 0.002) score -= 0.08;
  if (features.distVwap < -0.002) score += 0.08;
  if (features.oiDeltaPct1m > 0 && features.cvdRatio60 > 0) score += 0.05;
  if (features.oiDeltaPct1m < 0 && features.cvdRatio60 < 0) score -= 0.05;
  if (features.cvdAccel != null && features.cvdAccel > 0) score += 0.03;
  if (features.cvdAccel != null && features.cvdAccel < 0) score -= 0.03;

  return Math.max(0.05, Math.min(0.95, score));
}

/**
 * Main decision: UP / DOWN / NO_TRADE with confidence and size multiplier.
 */
export function decide(
  features: FeatureVector,
  regimeState: RegimeState,
  pUp: number,
  config: {
    thresholds?: Partial<ThresholdsConfig>;
    risk?: Partial<RiskConfig>;
  } = {}
): Decision {
  const th = { ...DEFAULT_THRESHOLDS, ...config.thresholds };
  const risk = { ...DEFAULT_RISK, ...config.risk };
  const now = features.ts;

  const confidence = Math.max(pUp, 1 - pUp);
  const direction: Direction =
    pUp >= th.pHigh ? "UP" : pUp <= 1 - th.pHigh ? "DOWN" : "NO_TRADE";

  if (direction === "NO_TRADE") {
    return {
      direction: "NO_TRADE",
      pUp,
      ts: now,
      regime: regimeState.regime,
      confidence,
      sizeMultiplier: 0,
      reason: "below_probability_threshold",
    };
  }

  if (confidence < th.pConfidence) {
    return {
      direction: "NO_TRADE",
      pUp,
      ts: now,
      regime: regimeState.regime,
      confidence,
      sizeMultiplier: 0,
      reason: "below_confidence_gate",
    };
  }

  if (isInPostLiqCooldown(regimeState, now, th)) {
    return {
      direction: "NO_TRADE",
      pUp,
      ts: now,
      regime: regimeState.regime,
      confidence,
      sizeMultiplier: 0,
      reason: "post_liquidation_cooldown",
    };
  }

  const flowDir = features.cvdRatio60 >= 0 ? "UP" : "DOWN";
  const obiAlign =
    (direction === "UP" && features.obi >= th.obiConfirm) ||
    (direction === "DOWN" && features.obi <= -th.obiConfirm);
  if (!obiAlign) {
    return {
      direction: "NO_TRADE",
      pUp,
      ts: now,
      regime: regimeState.regime,
      confidence,
      sizeMultiplier: 0,
      reason: "obi_not_confirming",
    };
  }

  const { allow, reason } = regimeAllowsTrade(
    regimeState.regime,
    features,
    direction,
    th
  );
  if (!allow) {
    return {
      direction: "NO_TRADE",
      pUp,
      ts: now,
      regime: regimeState.regime,
      confidence,
      sizeMultiplier: 0,
      reason: reason ?? "regime_filter",
    };
  }

  let sizeMultiplier = confidence;
  if (regimeState.regime === "vol_compression") sizeMultiplier *= 0.5;
  if (regimeState.regime === "ranging") sizeMultiplier *= 0.7;
  sizeMultiplier = Math.min(risk.maxSizeMultiplier, sizeMultiplier);

  return {
    direction,
    pUp,
    ts: now,
    regime: regimeState.regime,
    confidence,
    sizeMultiplier,
  };
}

/**
 * Size multiplier scaled by inverse volatility (caller provides 1/vol or ATR).
 * Cap at risk.volScaleCap.
 */
export function sizeByVolatility(
  baseMultiplier: number,
  invVolOrInvAtr: number,
  risk: Partial<RiskConfig> = {}
): number {
  const r = { ...DEFAULT_RISK, ...risk };
  const scaled = baseMultiplier * Math.min(invVolOrInvAtr, r.volScaleCap);
  return Math.min(r.maxSizeMultiplier, scaled);
}
