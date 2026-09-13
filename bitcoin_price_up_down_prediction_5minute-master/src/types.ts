/**
 * Shared types for 5m BTC direction model — trades, book, OI, OHLCV
 */

export type Side = "buy" | "sell";

/** Single trade (aggressor side + size) */
export interface Trade {
  ts: number;
  price: number;
  size: number;
  side: Side;
}

/** Order book level */
export interface BookLevel {
  price: number;
  size: number;
}

/** Snapshot or update: bids/asks arrays, best first */
export interface OrderBookSnapshot {
  ts: number;
  bids: BookLevel[];
  asks: BookLevel[];
}

/** Open interest at a timestamp */
export interface OIDataPoint {
  ts: number;
  oi: number;
}

/** 1m OHLCV candle */
export interface OHLCV1m {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** Inputs required to compute features */
export interface FeatureInputs {
  trades: Trade[];
  orderBook: OrderBookSnapshot;
  oiSeries: OIDataPoint[];
  candles1m: OHLCV1m[];
  currentPrice: number;
  now: number;
  volShortSec?: number;
  volLongSec?: number;
  obiLambda?: number;
  obiLevels?: number;
}

/** Aggregated feature vector (output of feature pipeline) */
export interface FeatureVector {
  ts: number;
  cvdRatio60: number;
  obi: number;
  oiDeltaPct1m: number;
  volCompress: number;
  rangePos: number;
  distVwap: number;
  distVwapZ?: number;
  /** Optional: flow acceleration = CVD(last 30s) - CVD(30s before) */
  cvdAccel?: number;
}

/** Regime state */
export type RegimeType = "trending" | "ranging" | "vol_compression" | "post_liquidation" | "unknown";

export interface RegimeState {
  regime: RegimeType;
  ts: number;
  /** Seconds since regime entered (for cooldowns) */
  enteredAt: number;
}

/** Decision output */
export type Direction = "UP" | "DOWN" | "NO_TRADE";

export interface Decision {
  direction: Direction;
  pUp: number;
  ts: number;
  regime: RegimeType;
  confidence: number;
  /** Suggested size multiplier 0..1 */
  sizeMultiplier: number;
  reason?: string;
}
