import test from "node:test";
import assert from "node:assert/strict";
import sdkCore from "@uniswap/sdk-core";
import v3sdk from "@uniswap/v3-sdk";
import { expectedMintAmounts } from "../src/chain/atomic-math.js";
const { Token } = sdkCore as any;
const { Pool, TickMath } = v3sdk as any;

const t0 = new Token(4663, "0x1000000000000000000000000000000000000000", 18);
const t1 = new Token(4663, "0x2000000000000000000000000000000000000000", 6);
const pool = new Pool(t0, t1, 3000, TickMath.getSqrtRatioAtTick(0).toString(), "1000000000000000000", 0);

test("atomic open minima use expected V3 utilization, not all desired balances", () => {
  const desired0 = 1_000_000_000_000_000_000n;
  const desired1 = 2_000_000n; // deliberately imbalanced; token1 leaves dust
  const used = expectedMintAmounts(pool, -600, 600, desired0, desired1);
  assert(used.amount0 > 0n && used.amount1 > 0n);
  assert(used.amount0 <= desired0 && used.amount1 <= desired1);
  assert(used.amount0 < desired0 || used.amount1 < desired1);
  const min0 = used.amount0 * 9700n / 10000n;
  const min1 = used.amount1 * 9700n / 10000n;
  assert(min0 > 0n && min1 > 0n);
  assert(min0 <= desired0 && min1 <= desired1);
});

test("range wholly on one side produces a zero mint side and must be rejected", () => {
  const used = expectedMintAmounts(pool, 600, 1200, 1_000_000n, 1_000_000n);
  assert.equal(used.amount1, 0n);
});
