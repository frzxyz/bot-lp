import test from "node:test";
import assert from "node:assert/strict";
import { boundedBudgetSplit, closeMinimum, operationAmount, parseBoundedRaw, singleSidedUsdgRange } from "../src/chain/v4/safety.js";

test("mint amount is acquisition delta, not full wallet balance", () => {
  assert.equal(operationAmount(1_250n, 1_000n), 250n);
});

test("only explicitly capped preexisting inventory is included", () => {
  assert.equal(operationAmount(1_250n, 1_000n, 100n), 350n);
  assert.equal(operationAmount(1_250n, 50n, 100n), 1_250n);
  assert.throws(() => operationAmount(999n, 1_000n), /decreased/);
});

test("raw USDG cap and split remain exact and bounded", () => {
  assert.deepEqual(boundedBudgetSplit(1_000_001n, 400_000n), { swap: 400_000n, retain: 600_001n });
  assert.equal(parseBoundedRaw("1000000", "1000000"), 1_000_000n);
  assert.throws(() => parseBoundedRaw("1000001", "1000000"), /exceeds/);
  assert.throws(() => parseBoundedRaw("1e6", "1000000"), /base-10/);
});

test("close minimum applies slippage and preserves legitimate zero side", () => {
  assert.equal(closeMinimum(10_000n, 5), 9_500n);
  assert.equal(closeMinimum(0n, 5), 0n);
  assert.throws(() => closeMinimum(1n, 99.99), /zero minimum/);
  assert.throws(() => closeMinimum(10n, 100), /slippage/);
});

test("USDG currency0 range is aligned and strictly above current tick", () => {
  assert.deepEqual(singleSidedUsdgRange(121, 60, true), { tickLower: 180, tickUpper: 420, usdgIsCurrency0: true });
  assert.deepEqual(singleSidedUsdgRange(120, 60, true), { tickLower: 180, tickUpper: 420, usdgIsCurrency0: true });
});

test("USDG currency1 range is aligned and strictly below current tick", () => {
  assert.deepEqual(singleSidedUsdgRange(121, 60, false), { tickLower: -120, tickUpper: 120, usdgIsCurrency0: false });
  assert.deepEqual(singleSidedUsdgRange(120, 60, false), { tickLower: -180, tickUpper: 60, usdgIsCurrency0: false });
  assert.deepEqual(singleSidedUsdgRange(-121, 60, false), { tickLower: -420, tickUpper: -180, usdgIsCurrency0: false });
});
