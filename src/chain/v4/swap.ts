/**
 * v4 swaps via the UniversalRouter (native ETH ↔ token). Verified by staticCall on chain
 * 4663. Encodes V4_SWAP (0x10) → [SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL]. Used by the
 * in-range v4 open flow to acquire the token side before minting.
 */
import { ethers } from "ethers";
import { C, cfg } from "../../config.js";
import { wallet, provider, overrides } from "../client.js";
import { V4QUOTER_ABI } from "./abis.js";
import { NATIVE, type PoolKey } from "./poolkey.js";

const POOLKEY_TYPE = "tuple(address currency0,address currency1,uint24 fee,int24 tickSpacing,address hooks)";
const coder = ethers.AbiCoder.defaultAbiCoder();

function poolKeyTuple(pk: PoolKey) {
  return [pk.currency0, pk.currency1, pk.fee, pk.tickSpacing, pk.hooks];
}

/** Quote ETH→token (or token→ETH) exact-in for a v4 pool. */
export async function quoteV4(pk: PoolKey, zeroForOne: boolean, amountIn: bigint): Promise<bigint> {
  const q = new ethers.Contract(C.v4Quoter!, V4QUOTER_ABI, provider);
  const r = await q.quoteExactInputSingle!.staticCall([poolKeyTuple(pk), zeroForOne, amountIn, "0x"]);
  return r[0] as bigint;
}

/** Build UniversalRouter execute() calldata for a single v4 exact-in swap. */
export function buildSwapCalldata(pk: PoolKey, zeroForOne: boolean, amountIn: bigint, minOut: bigint): string {
  const actions = "0x060c0f"; // SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL
  const inCur = zeroForOne ? pk.currency0 : pk.currency1;
  const outCur = zeroForOne ? pk.currency1 : pk.currency0;
  const swapParams = coder.encode(
    [`tuple(${POOLKEY_TYPE} poolKey, bool zeroForOne, uint128 amountIn, uint128 amountOutMinimum, bytes hookData)`],
    [[poolKeyTuple(pk), zeroForOne, amountIn, minOut, "0x"]],
  );
  const settleAll = coder.encode(["address", "uint256"], [inCur, amountIn]);
  const takeAll = coder.encode(["address", "uint256"], [outCur, minOut]);
  const v4Input = coder.encode(["bytes", "bytes[]"], [actions, [swapParams, settleAll, takeAll]]);
  const ur = new ethers.Interface(["function execute(bytes commands, bytes[] inputs, uint256 deadline) payable"]);
  return ur.encodeFunctionData("execute", ["0x10", [v4Input], Math.floor(Date.now() / 1000 + 600)]);
}

export interface V4SwapResult {
  tx: string;
  amountOut: bigint;
}

/** Swap native ETH → token on a v4 pool (ETH is currency0). Returns token received. */
export async function swapEthToTokenV4(pk: PoolKey, amountInWei: bigint): Promise<V4SwapResult> {
  const w = wallet();
  const zeroForOne = pk.currency0.toLowerCase() === NATIVE; // ETH(c0) → token(c1)
  const quoted = await quoteV4(pk, zeroForOne, amountInWei).catch(() => 0n);
  const minOut = (quoted * BigInt(Math.round((100 - (cfg.lp.slippagePct || 5)) * 100))) / 10_000n;
  const data = buildSwapCalldata(pk, zeroForOne, amountInWei, minOut);

  const tokenAddr = zeroForOne ? pk.currency1 : pk.currency0;
  const erc = new ethers.Contract(tokenAddr, ["function balanceOf(address) view returns (uint256)"], provider);
  const before: bigint = await erc.balanceOf!(w.address).catch(() => 0n);

  // simulate then send
  await provider.call({ to: C.universalRouter!, data, value: amountInWei, from: w.address });
  const tx = await w.sendTransaction({ to: C.universalRouter!, data, value: amountInWei, ...(await overrides()) });
  await tx.wait();

  const after: bigint = await erc.balanceOf!(w.address).catch(() => 0n);
  return { tx: tx.hash, amountOut: after - before };
}

/** ERC20-funded direct V4 swap: quote, exact Permit2 approvals, simulate, then send. */
export async function swapErc20V4(pk: PoolKey, tokenIn: string, amountIn: bigint, requiredMinOut?: bigint): Promise<V4SwapResult> {
  const w = wallet(), input = ethers.getAddress(tokenIn);
  const zeroForOne = pk.currency0.toLowerCase() === input.toLowerCase();
  if (!zeroForOne && pk.currency1.toLowerCase() !== input.toLowerCase()) throw new Error("input token is not in pool");
  const quoted = await quoteV4(pk, zeroForOne, amountIn);
  const policyMin = quoted * BigInt(Math.round((100 - (cfg.lp.slippagePct || 5)) * 100)) / 10_000n;
  const minOut = requiredMinOut && requiredMinOut > policyMin ? requiredMinOut : policyMin;
  if (amountIn <= 0n || minOut <= 0n || amountIn >= 1n << 128n) throw new Error("invalid bounded V4 swap");
  const p2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3";
  const erc = new ethers.Contract(input, ["function allowance(address,address) view returns(uint256)", "function approve(address,uint256) returns(bool)"], w);
  if ((await erc.allowance!(w.address, p2)) < amountIn) await (await erc.approve!(p2, amountIn, await overrides())).wait();
  const permit = new ethers.Contract(p2, ["function approve(address,address,uint160,uint48)"], w);
  await (await permit.approve!(input, C.universalRouter!, amountIn, Math.floor(Date.now()/1000)+1800, await overrides())).wait();
  const outAddr = zeroForOne ? pk.currency1 : pk.currency0;
  const out = new ethers.Contract(outAddr, ["function balanceOf(address) view returns(uint256)"], provider);
  const before: bigint = await out.balanceOf!(w.address);
  const data = buildSwapCalldata(pk, zeroForOne, amountIn, minOut);
  await provider.call({to:C.universalRouter!, data, from:w.address});
  const tx = await w.sendTransaction({to:C.universalRouter!, data, ...(await overrides())}); await tx.wait();
  const received = (await out.balanceOf!(w.address) as bigint) - before;
  if (received < minOut) throw new Error("swap output below minimum");
  return {tx:tx.hash, amountOut:received};
}

const PATHKEY_TYPE = "tuple(address intermediateCurrency,uint256 fee,int24 tickSpacing,address hooks,bytes hookData)";

export async function quoteV4TwoHop(first: PoolKey, second: PoolKey, tokenIn: string, intermediate: string, tokenOut: string, amountIn: bigint): Promise<bigint> {
  const input=ethers.getAddress(tokenIn), mid=ethers.getAddress(intermediate), output=ethers.getAddress(tokenOut);
  const z1=first.currency0.toLowerCase()===input.toLowerCase();
  if(!z1&&first.currency1.toLowerCase()!==input.toLowerCase())throw new Error("first hop input mismatch");
  const firstOut=z1?first.currency1:first.currency0;
  if(firstOut.toLowerCase()!==mid.toLowerCase())throw new Error("first hop intermediate mismatch");
  const z2=second.currency0.toLowerCase()===mid.toLowerCase();
  if(!z2&&second.currency1.toLowerCase()!==mid.toLowerCase())throw new Error("second hop intermediate mismatch");
  const secondOut=z2?second.currency1:second.currency0;
  if(secondOut.toLowerCase()!==output.toLowerCase())throw new Error("second hop output mismatch");
  const midOut=await quoteV4(first,z1,amountIn);
  return quoteV4(second,z2,midOut);
}

export function buildTwoHopCalldata(first: PoolKey, second: PoolKey, tokenIn: string, intermediate: string, tokenOut: string, amountIn: bigint, minOut: bigint): string {
  const path=[[intermediate,first.fee,first.tickSpacing,first.hooks,"0x"],[tokenOut,second.fee,second.tickSpacing,second.hooks,"0x"]];
  const swapParams=coder.encode([`tuple(address currencyIn,${PATHKEY_TYPE}[] path,uint128 amountIn,uint128 amountOutMinimum)`],[[tokenIn,path,amountIn,minOut]]);
  const actions="0x070c0f"; // SWAP_EXACT_IN, SETTLE_ALL, TAKE_ALL
  const settleAll=coder.encode(["address","uint256"],[tokenIn,amountIn]);
  const takeAll=coder.encode(["address","uint256"],[tokenOut,minOut]);
  const v4Input=coder.encode(["bytes","bytes[]"],[actions,[swapParams,settleAll,takeAll]]);
  const ur=new ethers.Interface(["function execute(bytes commands,bytes[] inputs,uint256 deadline) payable"]);
  return ur.encodeFunctionData("execute",["0x10",[v4Input],Math.floor(Date.now()/1000+600)]);
}

export async function swapErc20V4TwoHop(first: PoolKey, second: PoolKey, tokenIn: string, intermediate: string, tokenOut: string, amountIn: bigint, requiredMinOut: bigint): Promise<V4SwapResult> {
  if(amountIn<=0n||requiredMinOut<=0n||amountIn>=1n<<128n||requiredMinOut>=1n<<128n)throw new Error("invalid bounded V4 two-hop swap");
  const quoted=await quoteV4TwoHop(first,second,tokenIn,intermediate,tokenOut,amountIn);
  if(quoted<requiredMinOut)throw new Error("two-hop quote below required minimum");
  const w=wallet(),p2="0x000000000022D473030F116dDEE9F6B43aC78BA3",input=ethers.getAddress(tokenIn);
  const erc=new ethers.Contract(input,["function allowance(address,address) view returns(uint256)","function approve(address,uint256) returns(bool)"],w);
  if((await erc.allowance!(w.address,p2))<amountIn)await(await erc.approve!(p2,amountIn,await overrides())).wait();
  const permit=new ethers.Contract(p2,["function approve(address,address,uint160,uint48)"],w);
  await(await permit.approve!(input,C.universalRouter!,amountIn,Math.floor(Date.now()/1000)+1800,await overrides())).wait();
  const out=new ethers.Contract(tokenOut,["function balanceOf(address) view returns(uint256)"],provider),before:bigint=await out.balanceOf!(w.address);
  const data=buildTwoHopCalldata(first,second,input,intermediate,tokenOut,amountIn,requiredMinOut);
  await provider.call({to:C.universalRouter!,data,from:w.address});
  const tx=await w.sendTransaction({to:C.universalRouter!,data,...(await overrides())});await tx.wait();
  const received=(await out.balanceOf!(w.address) as bigint)-before;
  if(received<requiredMinOut)throw new Error("two-hop output below minimum");
  return {tx:tx.hash,amountOut:received};
}
