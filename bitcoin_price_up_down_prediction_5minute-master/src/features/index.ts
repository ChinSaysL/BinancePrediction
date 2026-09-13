/**
 * Feature pipeline — aggregates all inputs into a single FeatureVector
 */

import { FeatureInputs, FeatureVector } from "../types";
import { cvdRatio60, cvdAcceleration } from "./cvd";
import { orderBookImbalanceWeighted, orderBookImbalanceSimple } from "./orderbook";
import { oiDeltaPct1m } from "./oi";
import { volCompressScore } from "./volatility";
import { rangePosition } from "./range";
import { vwap1h, distVwap, distVwapZ } from "./vwap";
import { DEFAULT_THRESHOLDS } from "../config";

/**
 * Build full feature vector. Uses config defaults for windows/lambda if not provided.
 */
export function computeFeatures(input: FeatureInputs): FeatureVector {
  const now = input.now;
  const cfg = DEFAULT_THRESHOLDS;
  const volShort = input.volShortSec ?? cfg.volShortWindowSec;
  const volLong = input.volLongSec ?? cfg.volLongWindowSec;
  const obiLambda = input.obiLambda ?? cfg.obiLambda;
  const obiLevels = input.obiLevels ?? cfg.obiLevels;

  const mid =
    (input.orderBook.bids[0]?.price + input.orderBook.asks[0]?.price) / 2 ||
    input.currentPrice;

  const cvdRatio = cvdRatio60(input.trades, now);
  const obiW = orderBookImbalanceWeighted(input.orderBook, mid, obiLambda);
  const obiS = orderBookImbalanceSimple(input.orderBook, obiLevels);
  const obi = obiW !== 0 ? obiW : obiS;

  const oiDelta = oiDeltaPct1m(input.oiSeries, now) ?? 0;
  const volCompress = volCompressScore(
    input.candles1m,
    now,
    volShort,
    volLong
  );
  const rangePos = rangePosition(
    input.candles1m,
    now,
    input.currentPrice
  );
  const vwap = vwap1h(input.candles1m, now);
  const distV = distVwap(input.currentPrice, vwap);
  const distVZ = distVwapZ(input.currentPrice, vwap, input.candles1m, now) ?? undefined;
  const cvdAccel = cvdAcceleration(input.trades, now);

  return {
    ts: now,
    cvdRatio60: cvdRatio,
    obi,
    oiDeltaPct1m: oiDelta,
    volCompress,
    rangePos,
    distVwap: distV,
    distVwapZ: distVZ,
    cvdAccel,
  };
}

export { cvdRatio60, cvdAcceleration } from "./cvd";
export { orderBookImbalanceWeighted, orderBookImbalanceSimple } from "./orderbook";
export { oiDeltaPct1m } from "./oi";
export { volCompressScore } from "./volatility";
export { rangePosition } from "./range";
export { vwap1h, distVwap, distVwapZ } from "./vwap";
