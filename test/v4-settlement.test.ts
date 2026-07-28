import test from "node:test";
import assert from "node:assert/strict";
import { closeReceiptDelta } from "../src/chain/v4/settlement.js";

test("settlement liquidates only positive close receipt delta",()=>{
  assert.equal(closeReceiptDelta(1_000n,1_275n),275n);
  assert.equal(closeReceiptDelta(1_000n,999n),0n);
});