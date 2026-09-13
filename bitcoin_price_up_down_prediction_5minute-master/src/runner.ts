/**
 * Runner: predict every 1m, resolve after 5m, log prediction vs actual JSON + accuracy
 */

import { runPipeline } from "./index";
import { fetchFeatureInputs } from "./data/binance";
import { Decision, Direction } from "./types";
import * as fs from "fs";
import * as path from "path";

const INTERVAL_MS = 60_000;
const RESOLVE_AFTER_MS = 5 * 60_000;
const LOG_DIR = path.join(process.cwd(), "logs");
const RESULTS_FILE = path.join(LOG_DIR, "results.jsonl");
const ACCURACY_FILE = path.join(LOG_DIR, "accuracy.json");

interface PendingPrediction {
  ts: number;
  direction: Direction;
  priceAtPredict: number;
  pUp: number;
  regime: string;
  confidence: number;
}

interface ResolvedResult {
  tsPredict: number;
  tsResolve: number;
  prediction: Direction;
  actual: Direction;
  correct: boolean;
  priceAtPredict: number;
  priceAtResolve: number;
  pUp: number;
  regime: string;
}

let pending: PendingPrediction[] = [];
let resolved: ResolvedResult[] = [];
let totalDirectional = 0;
let correctDirectional = 0;

function ensureLogDir(): void {
  if (!fs.existsSync(LOG_DIR)) {
    fs.mkdirSync(LOG_DIR, { recursive: true });
  }
}

function logResultLine(obj: ResolvedResult): void {
  ensureLogDir();
  const line = JSON.stringify(obj) + "\n";
  fs.appendFileSync(RESULTS_FILE, line);
  console.log("[RESULT]", line.trim());
}

function updateAccuracy(): void {
  totalDirectional = resolved.length;
  correctDirectional = resolved.filter((r) => r.correct).length;
  const rate = totalDirectional > 0 ? correctDirectional / totalDirectional : 0;
  const summary = {
    updatedAt: new Date().toISOString(),
    totalDirectionalPredictions: totalDirectional,
    correct: correctDirectional,
    accuracyRate: Math.round(rate * 10000) / 100,
    results: resolved.slice(-50),
  };
  ensureLogDir();
  fs.writeFileSync(ACCURACY_FILE, JSON.stringify(summary, null, 2));
  console.log(
    `[ACCURACY] ${correctDirectional}/${totalDirectional} correct → ${(rate * 100).toFixed(2)}% rate`
  );
}

function resolvePredictions(currentPrice: number, now: number): void {
  const cutoff = now - RESOLVE_AFTER_MS;
  const toResolve = pending.filter((p) => p.ts <= cutoff && p.direction !== "NO_TRADE");
  pending = pending.filter((p) => p.ts > cutoff || p.direction === "NO_TRADE");

  for (const p of toResolve) {
    const actual: Direction = currentPrice > p.priceAtPredict ? "UP" : "DOWN";
    const correct = actual === p.direction;
    const result: ResolvedResult = {
      tsPredict: p.ts,
      tsResolve: now,
      prediction: p.direction,
      actual,
      correct,
      priceAtPredict: p.priceAtPredict,
      priceAtResolve: currentPrice,
      pUp: p.pUp,
      regime: p.regime,
    };
    resolved.push(result);
    logResultLine(result);
  }
  if (toResolve.length > 0) updateAccuracy();
}

async function tick(): Promise<void> {
  const now = Date.now();
  let currentPrice = 0;

  try {
    const input = await fetchFeatureInputs("BTCUSDT");
    currentPrice = input.currentPrice;

    const { decision } = runPipeline(input, { useHeuristicPUp: true });
    const d = decision as Decision;

    pending.push({
      ts: now,
      direction: d.direction,
      priceAtPredict: currentPrice,
      pUp: d.pUp,
      regime: d.regime,
      confidence: d.confidence,
    });

    console.log(
      `[PREDICT] ${new Date(now).toISOString()} direction=${d.direction} pUp=${d.pUp.toFixed(3)} price=${currentPrice} regime=${d.regime}${d.reason ? " reason=" + d.reason : ""}`
    );
  } catch (err) {
    console.error("[TICK ERROR]", err);
  }

  if (currentPrice > 0) resolvePredictions(currentPrice, now);
}

async function main(): Promise<void> {
  console.log("BTC 5m direction bot started. Predict every 1m, resolve after 5m.");
  console.log("Results:", RESULTS_FILE);
  console.log("Accuracy:", ACCURACY_FILE);

  await tick();
  setInterval(tick, INTERVAL_MS);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
