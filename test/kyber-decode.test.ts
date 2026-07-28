import test from "node:test";
import assert from "node:assert/strict";
import { ethers } from "ethers";
import {
  KYBER_SIMPLE_SELECTOR,
  KYBER_SWAP_SELECTOR,
  assertKyberCalldata,
  decodeKyberCalldata,
} from "../src/chain/kyberDecode.js";

const EXECUTOR = "0x6589279cF08a99FF4B984706dE92DEd700607342";
const WALLET = "0x3582605Edebf376b684a45E8Faa6D808C22a8e3e";
const USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168";
const TOKEN = "0x2AD022332400df20948f20F593C1074B88f6E753";
const ROUTER_EXEC = "0x00000000000000000000000000000000000000e1";

const DESC =
  "tuple(address srcToken,address dstToken,address[] srcReceivers,uint256[] srcAmounts," +
  "address[] feeReceivers,uint256[] feeAmounts,address dstReceiver,uint256 amount," +
  "uint256 minReturnAmount,uint256 flags,bytes permit)";
const coder = ethers.AbiCoder.defaultAbiCoder();

interface DescOverrides {
  srcToken?: string; dstToken?: string; dstReceiver?: string;
  amount?: bigint; minReturnAmount?: bigint;
  srcAmounts?: bigint[]; feeReceivers?: string[]; feeAmounts?: bigint[];
}

/** Build calldata shaped exactly like the aggregator's, so decoding is a real test. */
function buildSwap(o: DescOverrides = {}): string {
  const desc = {
    srcToken: o.srcToken ?? USDG,
    dstToken: o.dstToken ?? TOKEN,
    srcReceivers: [ROUTER_EXEC],
    srcAmounts: o.srcAmounts ?? [500_000n],
    feeReceivers: o.feeReceivers ?? [],
    feeAmounts: o.feeAmounts ?? [],
    dstReceiver: o.dstReceiver ?? EXECUTOR,
    amount: o.amount ?? 500_000n,
    minReturnAmount: o.minReturnAmount ?? 990n,
    flags: 0n,
    permit: "0x",
  };
  const body = coder.encode(
    [`tuple(address callTarget,address approveTarget,bytes targetData,${DESC} desc,bytes clientData)`],
    [{ callTarget: ROUTER_EXEC, approveTarget: ROUTER_EXEC, targetData: "0x1234", desc, clientData: "0x" }],
  );
  return KYBER_SWAP_SELECTOR + body.slice(2);
}

function buildSimple(o: DescOverrides = {}): string {
  const desc = {
    srcToken: o.srcToken ?? USDG,
    dstToken: o.dstToken ?? TOKEN,
    srcReceivers: [ROUTER_EXEC],
    srcAmounts: [500_000n],
    feeReceivers: [],
    feeAmounts: [],
    dstReceiver: o.dstReceiver ?? EXECUTOR,
    amount: o.amount ?? 500_000n,
    minReturnAmount: o.minReturnAmount ?? 990n,
    flags: 0n,
    permit: "0x",
  };
  const body = coder.encode(["address", DESC, "bytes", "bytes"], [ROUTER_EXEC, desc, "0x", "0x"]);
  return KYBER_SIMPLE_SELECTOR + body.slice(2);
}

const expectation = {
  tokenIn: USDG, tokenOut: TOKEN, recipient: EXECUTOR,
  amountIn: 500_000n, minAmountOut: 900n,
};

test("struct layouts hash to the router's published selectors", () => {
  // The module self-checks at import; this pins the values the decoder accepts.
  assert.equal(KYBER_SWAP_SELECTOR, "0xe21fd0e9");
  assert.equal(KYBER_SIMPLE_SELECTOR, "0x8af033fb");
});

test("swap calldata decodes to the fields that were encoded", () => {
  const d = decodeKyberCalldata(buildSwap());
  assert.equal(d.selector, KYBER_SWAP_SELECTOR);
  assert.equal(d.srcToken, ethers.getAddress(USDG));
  assert.equal(d.dstToken, ethers.getAddress(TOKEN));
  assert.equal(d.dstReceiver, ethers.getAddress(EXECUTOR));
  assert.equal(d.amount, 500_000n);
  assert.equal(d.minReturnAmount, 990n);
});

test("swapSimpleMode carries the same descriptor at its own offset", () => {
  const d = decodeKyberCalldata(buildSimple());
  assert.equal(d.selector, KYBER_SIMPLE_SELECTOR);
  assert.equal(d.dstReceiver, ethers.getAddress(EXECUTOR));
  assert.equal(d.amount, 500_000n);
});

test("an unknown selector is refused rather than guessed at", () => {
  assert.throws(() => decodeKyberCalldata("0xdeadbeef" + "00".repeat(64)), /not an allowlisted entrypoint/);
  assert.throws(() => decodeKyberCalldata("0x12"), /selector/);
});

test("a route paying someone other than the executor is rejected", () => {
  // The defect the decode exists to catch: proceeds land outside the executor and
  // its atomic accounting silently measures nothing.
  assert.throws(
    () => assertKyberCalldata(buildSwap({ dstReceiver: WALLET }), expectation),
    /dstReceiver .* proceeds would leave the executor/,
  );
});

test("token substitution on either side is rejected", () => {
  assert.throws(() => assertKyberCalldata(buildSwap({ srcToken: TOKEN }), expectation), /srcToken/);
  assert.throws(() => assertKyberCalldata(buildSwap({ dstToken: USDG }), expectation), /dstToken/);
});

test("a changed input amount is rejected", () => {
  assert.throws(() => assertKyberCalldata(buildSwap({ amount: 500_001n }), expectation), /≠ requested/);
});

test("an output floor below the caller's own is rejected", () => {
  assert.throws(() => assertKyberCalldata(buildSwap({ minReturnAmount: 899n }), expectation), /below caller floor/);
  assert.throws(() => assertKyberCalldata(buildSwap({ minReturnAmount: 0n }), expectation), /below caller floor/);
});

test("srcAmounts may not authorise pulling more than amountIn", () => {
  assert.throws(
    () => assertKyberCalldata(buildSwap({ srcAmounts: [500_000n, 1n] }), expectation),
    /exceeds amountIn/,
  );
});

test("an injected protocol fee is rejected unless explicitly allowed", () => {
  const withFee = buildSwap({ feeReceivers: [WALLET], feeAmounts: [10n] });
  assert.throws(() => assertKyberCalldata(withFee, expectation), /unexpected fee/);
  assert.doesNotThrow(() => assertKyberCalldata(withFee, { ...expectation, allowFees: true }));
});

test("a well-formed executor-targeted route passes", () => {
  const d = assertKyberCalldata(buildSwap(), expectation);
  assert.equal(d.dstReceiver, ethers.getAddress(EXECUTOR));
  assert.ok(d.minReturnAmount >= expectation.minAmountOut);
});
