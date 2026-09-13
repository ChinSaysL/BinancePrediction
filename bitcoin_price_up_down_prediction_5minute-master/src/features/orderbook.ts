/**
 * Order book imbalance — depth-weighted (exponential) and simple top-N
 */

import { OrderBookSnapshot, BookLevel } from "../types";

function weight(distanceTicks: number, lambda: number): number {
  return Math.exp(-lambda * distanceTicks);
}

/**
 * Depth-weighted OBI with exponential decay by distance from mid.
 * OBI = (weightedBid - weightedAsk) / (weightedBid + weightedAsk) in [-1, 1].
 */
export function orderBookImbalanceWeighted(
  snapshot: OrderBookSnapshot,
  mid: number,
  lambda: number = 0.2
): number {
  let bidW = 0;
  let askW = 0;
  for (let i = 0; i < snapshot.bids.length; i++) {
    const d = Math.abs(mid - snapshot.bids[i].price) / (mid * 0.0001) || 0;
    bidW += weight(d, lambda) * snapshot.bids[i].size;
  }
  for (let i = 0; i < snapshot.asks.length; i++) {
    const d = Math.abs(snapshot.asks[i].price - mid) / (mid * 0.0001) || 0;
    askW += weight(d, lambda) * snapshot.asks[i].size;
  }
  const total = bidW + askW;
  if (total <= 0) return 0;
  return Math.max(-1, Math.min(1, (bidW - askW) / total));
}

/**
 * Simple OBI: top N levels each side.
 */
export function orderBookImbalanceSimple(
  snapshot: OrderBookSnapshot,
  levels: number = 5
): number {
  const b = snapshot.bids.slice(0, levels).reduce((s, l) => s + l.size, 0);
  const a = snapshot.asks.slice(0, levels).reduce((s, l) => s + l.size, 0);
  const total = b + a;
  if (total <= 0) return 0;
  return Math.max(-1, Math.min(1, (b - a) / total));
}
