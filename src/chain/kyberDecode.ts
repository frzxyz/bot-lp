/**
 * KyberSwap MetaAggregationRouterV2 calldata decoder.
 *
 * The aggregator returns opaque bytes. Checking only `routerAddress` and `amountIn`
 * — as the swap path used to — leaves the field that actually decides where the
 * proceeds land, `dstReceiver`, entirely unverified. That matters most for the
 * atomic V4 executor: it swaps as itself and requires the output to arrive at
 * itself, and its whole safety argument rests on the swap being what the plan says
 * it is. `ATOMIC_RUNBOOK.md` names this decode as a precondition for V4 support.
 *
 * The two struct layouts below are not transcribed from a web page; each signature
 * is hashed at module load and checked against the selector it must produce, so a
 * wrong field list cannot silently decode into plausible-looking garbage.
 * See `test/kyber-decode.test.ts`.
 */
import { ethers } from "ethers";

/** SwapDescriptionV2 — the shared descriptor both entrypoints carry. */
const DESC =
  "tuple(address srcToken,address dstToken,address[] srcReceivers,uint256[] srcAmounts," +
  "address[] feeReceivers,uint256[] feeAmounts,address dstReceiver,uint256 amount," +
  "uint256 minReturnAmount,uint256 flags,bytes permit)";

/** swap(SwapExecutionParams) */
const SWAP_ARGS = `tuple(address callTarget,address approveTarget,bytes targetData,${DESC} desc,bytes clientData)`;
/** swapSimpleMode(IAggregationExecutor,SwapDescriptionV2,bytes,bytes) */
const SIMPLE_ARGS = `address,${DESC},bytes,bytes`;

/** Strip parameter names to recover the canonical signature used for the selector. */
const canonical = (types: string) => types.replace(/\btuple\b/g, "").replace(/ [A-Za-z_][A-Za-z0-9_]*(?=[,)\]])/g, "");

export const KYBER_SWAP_SELECTOR = "0xe21fd0e9";
export const KYBER_SIMPLE_SELECTOR = "0x8af033fb";

function selectorOf(name: string, types: string): string {
  return ethers.id(`${name}(${canonical(types)})`).slice(0, 10);
}

// Fail at import rather than at decode time: a layout that does not hash to the
// known selector is the wrong layout, and every field read from it is wrong too.
for (const [name, types, expected] of [
  ["swap", SWAP_ARGS, KYBER_SWAP_SELECTOR],
  ["swapSimpleMode", SIMPLE_ARGS, KYBER_SIMPLE_SELECTOR],
] as const) {
  const got = selectorOf(name, types);
  if (got !== expected) {
    throw new Error(`kyberDecode: ${name} layout hashes to ${got}, expected ${expected}`);
  }
}

export interface DecodedKyberSwap {
  selector: string;
  /** Present only for `swap`; `swapSimpleMode` carries the executor as `caller`. */
  callTarget: string | null;
  approveTarget: string | null;
  srcToken: string;
  dstToken: string;
  /** Where the bought token is delivered. The field the executor depends on. */
  dstReceiver: string;
  srcReceivers: string[];
  srcAmounts: bigint[];
  feeReceivers: string[];
  feeAmounts: bigint[];
  amount: bigint;
  minReturnAmount: bigint;
  flags: bigint;
}

const coder = ethers.AbiCoder.defaultAbiCoder();

/** Decode router calldata. Throws on an unknown selector or malformed body. */
export function decodeKyberCalldata(data: string): DecodedKyberSwap {
  if (typeof data !== "string" || !data.startsWith("0x") || data.length < 10) {
    throw new Error("kyber calldata is not a hex string with a selector");
  }
  const selector = data.slice(0, 10).toLowerCase();
  const body = "0x" + data.slice(10);
  let desc: any;
  let callTarget: string | null = null;
  let approveTarget: string | null = null;

  if (selector === KYBER_SWAP_SELECTOR) {
    const [params] = coder.decode([SWAP_ARGS], body);
    callTarget = ethers.getAddress(params.callTarget);
    approveTarget = ethers.getAddress(params.approveTarget);
    desc = params.desc;
  } else if (selector === KYBER_SIMPLE_SELECTOR) {
    const decoded = coder.decode(SIMPLE_ARGS.split(/,(?![^()]*\))/), body);
    callTarget = ethers.getAddress(decoded[0]);
    desc = decoded[1];
  } else {
    throw new Error(`kyber selector ${selector} is not an allowlisted entrypoint`);
  }

  return {
    selector,
    callTarget,
    approveTarget,
    srcToken: ethers.getAddress(desc.srcToken),
    dstToken: ethers.getAddress(desc.dstToken),
    dstReceiver: ethers.getAddress(desc.dstReceiver),
    srcReceivers: desc.srcReceivers.map((a: string) => ethers.getAddress(a)),
    srcAmounts: desc.srcAmounts.map((n: bigint) => BigInt(n)),
    feeReceivers: desc.feeReceivers.map((a: string) => ethers.getAddress(a)),
    feeAmounts: desc.feeAmounts.map((n: bigint) => BigInt(n)),
    amount: BigInt(desc.amount),
    minReturnAmount: BigInt(desc.minReturnAmount),
    flags: BigInt(desc.flags),
  };
}

export interface KyberExpectation {
  /**
   * Acceptable input token(s). An array accommodates the one genuine ambiguity in
   * the descriptor: for a native swap the router may name either its own native
   * sentinel or the wrapped token, and which one is not worth guessing wrong on a
   * path that works today. The receiver and amount checks stay exact regardless.
   */
  tokenIn: string | string[];
  tokenOut: string | string[];
  /** Who must receive the bought token — the executor for an atomic swap. */
  recipient: string;
  amountIn: bigint;
  /** Lower bound the caller computed itself; the route may promise more, never less. */
  minAmountOut: bigint;
  /** Allow the aggregator to route a protocol fee. Off by default. */
  allowFees?: boolean;
}

/**
 * Decode and assert the route does what the caller intends.
 *
 * Every mismatch throws: this runs before broadcast, where refusing costs a retry
 * and proceeding can cost the position.
 */
export function assertKyberCalldata(data: string, expected: KyberExpectation): DecodedKyberSwap {
  const d = decodeKyberCalldata(data);
  const accept = (v: string | string[]) =>
    (Array.isArray(v) ? v : [v]).map((a) => ethers.getAddress(a));
  const wantIn = accept(expected.tokenIn), wantOut = accept(expected.tokenOut);
  const recipient = ethers.getAddress(expected.recipient);
  if (!wantIn.includes(d.srcToken)) throw new Error(`kyber srcToken ${d.srcToken} ∉ ${wantIn.join("/")}`);
  if (!wantOut.includes(d.dstToken)) throw new Error(`kyber dstToken ${d.dstToken} ∉ ${wantOut.join("/")}`);
  if (d.dstReceiver !== recipient) {
    throw new Error(`kyber dstReceiver ${d.dstReceiver} ≠ ${recipient}: proceeds would leave the executor`);
  }
  if (d.amount !== expected.amountIn) {
    throw new Error(`kyber amount ${d.amount} ≠ requested ${expected.amountIn}`);
  }
  if (d.minReturnAmount < expected.minAmountOut) {
    throw new Error(`kyber minReturnAmount ${d.minReturnAmount} below caller floor ${expected.minAmountOut}`);
  }
  if (d.minReturnAmount <= 0n) throw new Error("kyber route carries no output floor");
  // srcAmounts fund the route; more than amountIn means the router is authorised to
  // pull beyond what was approved for this swap.
  const pulled = d.srcAmounts.reduce((a, b) => a + b, 0n);
  if (pulled > expected.amountIn) {
    throw new Error(`kyber srcAmounts total ${pulled} exceeds amountIn ${expected.amountIn}`);
  }
  if (!expected.allowFees) {
    const fees = d.feeAmounts.reduce((a, b) => a + b, 0n);
    if (fees > 0n || d.feeReceivers.length > 0) {
      throw new Error(`kyber route carries an unexpected fee (${fees} to ${d.feeReceivers.join(",") || "none"})`);
    }
  }
  return d;
}
