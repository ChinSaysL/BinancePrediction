/**
 * 5-minute BTCUSDT direction model
 *
 * Microstructure-driven: CVD, order book imbalance, OI delta, volatility
 * compression, range position, VWAP distance. Regime filter + confidence gating.
 */

export * from "./types";
export * from "./config";
export * from "./features";
export * from "./regime";
export * from "./decision";

import { FeatureInputs, FeatureVector, RegimeState, Decision } from "./types";
import { computeFeatures } from "./features";
import { classifyRegime, RegimeInput } from "./regime";
import { decide, heuristicPUp } from "./decision";
import { ThresholdsConfig, RiskConfig } from "./config";

export interface PipelineInput extends FeatureInputs {
  /** Optional: set if a liquidation spike was detected */
  postLiquidationSpike?: boolean;
  varianceRatio?: number;
}

export interface PipelineConfig {
  thresholds?: Partial<ThresholdsConfig>;
  risk?: Partial<RiskConfig>;
  /** Use heuristic P(up); set to false when you plug in a model */
  useHeuristicPUp?: boolean;
  /** If using a model, pass P(up) from it; otherwise ignored */
  modelPUp?: number;
}

/**
 * Full pipeline: features -> regime -> P(up) -> decision.
 */
export function runPipeline(
  input: PipelineInput,
  config: PipelineConfig = {}
): { features: FeatureVector; regime: RegimeState; decision: Decision } {
  const features = computeFeatures(input);
  const regimeInput: RegimeInput = {
    features,
    postLiquidationSpike: input.postLiquidationSpike,
    varianceRatio: input.varianceRatio,
  };
  const regime = classifyRegime(regimeInput);
  const pUp =
    config.useHeuristicPUp !== false
      ? heuristicPUp(features)
      : config.modelPUp ?? 0.5;
  const decision = decide(features, regime, pUp, {
    thresholds: config.thresholds,
    risk: config.risk,
  });
  return { features, regime, decision };
}
