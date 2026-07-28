/**
 * KyberSwap aggregator client — best-route swaps across ALL of the chain's liquidity (every
 * DEX, fee tier, hooked pool, and multi-hop), so acquiring a token never bleeds fee + price
 * impact from buying on a single thin pool. Adapted from labrinyang/lp-terminal (kyber.ts +
 * kyberExec.ts) for a server-side ethers wallet.
 *
 * SECURITY: kyber calldata is opaque, so every swap passes 5 gates before broadcast:
 *   1. build.routerAddress must equal the whitelisted router (tx.to is ALWAYS the whitelist)
 *   2. tx value == amountIn for native ETH, else 0
 *   3. built amountIn == requested amountIn (spend integrity)
 *   4. built amountOut >= fresh quote − slippage (no execution drift)
 *   5. the calldata decodes to the descriptor we asked for — tokens, amount, output
 *      floor, and above all dstReceiver (see kyberDecode.ts)
 *
 * Gates 1-4 all read the API's JSON envelope; only gate 5 reads the bytes that are
 * actually broadcast, which is where the receiver of the proceeds is decided.
 */
import { ethers } from "ethers";
import { env, cfg, C } from "../config.js";
import { wallet, provider, overrides } from "./client.js";
import { assertKyberCalldata } from "./kyberDecode.js";
import { logger } from "../util/log.js";

const log = logger("kyber");
export const KYBER_NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"; // kyber sentinel for native ETH
const HEADERS = { "x-client-id": "robinhood-lp-bot" };

const api = () => `${env.kyberBase}/${env.kyberChain}/api/v1`;
export const kyberEnabled = (): boolean => !!env.kyberBase && !!env.kyberRouter;

interface RouteData {
  routeSummary: any;
  routerAddress: string;
}

/** GET /routes — the optimal route + quote. Returns null on any failure. */
export async function kyberRoute(tokenIn: string, tokenOut: string, amountIn: bigint): Promise<RouteData | null> {
  try {
    const u = new URL(`${api()}/routes`);
    u.searchParams.set("tokenIn", tokenIn);
    u.searchParams.set("tokenOut", tokenOut);
    u.searchParams.set("amountIn", amountIn.toString());
    u.searchParams.set("gasInclude", "true");
    const r = await fetch(u, { headers: HEADERS, signal: AbortSignal.timeout(20_000) });
    const j: any = await r.json().catch(() => null);
    if (!r.ok || j?.code !== 0 || !j?.data?.routeSummary) {
      log.warn(`routes gagal: ${j?.message ?? r.status}`);
      return null;
    }
    return j.data as RouteData;
  } catch (e) {
    log.warn(`route error: ${(e as Error).message.slice(0, 80)}`);
    return null;
  }
}

/** POST /route/build — encode the route into calldata. Returns null on failure. */
export async function kyberBuild(routeSummary: any, sender: string, recipient: string, slippageBps: number): Promise<any | null> {
  try {
    const r = await fetch(`${api()}/route/build`, {
      method: "POST",
      headers: { ...HEADERS, "content-type": "application/json" },
      body: JSON.stringify({ routeSummary, sender, recipient, slippageTolerance: slippageBps, source: "robinhood-lp-bot", enableGasEstimation: false }),
      signal: AbortSignal.timeout(20_000),
    });
    const j: any = await r.json().catch(() => null);
    if (!r.ok || j?.code !== 0 || !j?.data?.data) {
      log.warn(`build gagal: ${j?.message ?? r.status}`);
      return null;
    }
    return j.data;
  } catch (e) {
    log.warn(`build error: ${(e as Error).message.slice(0, 80)}`);
    return null;
  }
}

export interface KyberSwapResult {
  tx: string;
  amountOut: bigint; // actual tokenOut received (balance delta)
}

/**
 * Best-route swap. tokenIn = KYBER_NATIVE for ETH. Returns null if the aggregator can't route
 * (caller can fall back). Throws only on a SECURITY gate failure (never silently unsafe).
 */
export async function kyberSwap(tokenIn: string, tokenOut: string, amountIn: bigint): Promise<KyberSwapResult | null> {
  if (!kyberEnabled() || amountIn <= 0n) return null;
  const w = wallet();
  const nativeIn = tokenIn.toLowerCase() === KYBER_NATIVE.toLowerCase();
  const slippageBps = Math.round((cfg.lp.slippagePct || 5) * 100);

  const route = await kyberRoute(tokenIn, tokenOut, amountIn);
  if (!route) return null;
  const built = await kyberBuild(route.routeSummary, w.address, w.address, slippageBps);
  if (!built) return null;

  // ── security gates ──
  if (ethers.getAddress(built.routerAddress) !== ethers.getAddress(env.kyberRouter)) {
    throw new Error(`kyber router mismatch: ${built.routerAddress} ≠ whitelist`);
  }
  const value = BigInt(built.transactionValue ?? "0");
  if (value !== (nativeIn ? amountIn : 0n)) throw new Error(`kyber value sanity: got ${value}, want ${nativeIn ? amountIn : 0n}`);
  const quotedOut = BigInt(route.routeSummary.amountOut);
  const minOut = (quotedOut * BigInt(10_000 - slippageBps)) / 10_000n;
  if (BigInt(built.amountIn) !== amountIn || BigInt(built.amountOut) < minOut) {
    throw new Error(`kyber build deviates (in ${built.amountIn}, out ${built.amountOut} < ${minOut})`);
  }
  // Gate 5: the calldata itself must name this wallet as the receiver. The four
  // checks above all read the API's JSON envelope; only this one reads the bytes
  // that are actually broadcast.
  assertKyberCalldata(built.data, {
    tokenIn: nativeIn ? [KYBER_NATIVE, C.weth] : tokenIn,
    tokenOut: tokenOut.toLowerCase() === KYBER_NATIVE.toLowerCase() ? [KYBER_NATIVE, C.weth] : tokenOut,
    recipient: w.address,
    amountIn,
    minAmountOut: minOut,
  });

  // ERC20 input → exact-amount approve to the router (native in carries value, no approve)
  if (!nativeIn) {
    const erc = new ethers.Contract(tokenIn, ["function allowance(address,address) view returns (uint256)", "function approve(address,uint256) returns (bool)"], w);
    if ((await erc.allowance!(w.address, env.kyberRouter)) < amountIn) {
      await (await erc.approve!(env.kyberRouter, amountIn, await overrides())).wait();
    }
  }

  // measure output by balance delta (native ETH out → getBalance; ERC20 → balanceOf)
  const nativeOut = tokenOut.toLowerCase() === KYBER_NATIVE.toLowerCase();
  const outErc = nativeOut ? null : new ethers.Contract(tokenOut, ["function balanceOf(address) view returns (uint256)"], provider);
  const outBal = async (): Promise<bigint> => (nativeOut ? provider.getBalance(w.address) : outErc!.balanceOf!(w.address).catch(() => 0n));
  const before = await outBal();
  await provider.call({ to: env.kyberRouter, data: built.data, value, from: w.address }); // simulate
  const tx = await w.sendTransaction({ to: env.kyberRouter, data: built.data, value, ...(await overrides()) });
  await tx.wait();
  const after = await outBal();
  return { tx: tx.hash, amountOut: after > before ? after - before : 0n };
}

/**
 * Exact reverse-route proof. Quote-only never approves, simulates, or sends.
 *
 * `taker` overrides who the route is built for. The atomic V4 executor swaps as
 * itself and needs the proceeds delivered to itself, so its proof must be built and
 * verified against the executor address rather than the wallet — building for one
 * address and executing as another is precisely the mismatch `ATOMIC_RUNBOOK.md`
 * requires to be closed before V4 can be enabled.
 */
export async function kyberPreflight(tokenIn:string, tokenOut:string, amountIn:bigint, quoteOnly=false, taker?:string):Promise<any> {
  if(!kyberEnabled() || amountIn<=0n) throw new Error("Kyber unavailable or zero amount");
  const w=quoteOnly?null:wallet();
  const fallback=quoteOnly ? ethers.getAddress(process.env.RH_V4_EXPECTED_WALLET||"") : w!.address;
  const sender=taker ? ethers.getAddress(taker) : fallback;
  const slippageBps=Math.round((cfg.lp.slippagePct||5)*100);
  if(slippageBps<=0 || slippageBps>1000) throw new Error("slippage exceeds hard 10% liquidation cap");
  const route=await kyberRoute(tokenIn,tokenOut,amountIn); if(!route) throw new Error("no Kyber route");
  const built=await kyberBuild(route.routeSummary,sender,sender,slippageBps); if(!built) throw new Error("Kyber build unavailable");
  if(ethers.getAddress(built.routerAddress)!==ethers.getAddress(env.kyberRouter)) throw new Error("Kyber target not allowlisted");
  if(BigInt(built.amountIn)!==amountIn) throw new Error("Kyber amountIn mismatch");
  const quoted=BigInt(route.routeSummary.amountOut), minOut=quoted*BigInt(10000-slippageBps)/10000n;
  if(minOut<=0n || BigInt(built.amountOut)<minOut) throw new Error("invalid protected output");
  // The API is asked for `sender`; this proves the bytes actually say so.
  const decoded=assertKyberCalldata(built.data,{tokenIn,tokenOut,recipient:sender,amountIn,minAmountOut:minOut});
  const tx={to:env.kyberRouter,data:built.data,value:BigInt(built.transactionValue||"0"),from:sender};
  let gas:string|null=null;
  if(!quoteOnly){
    const erc=new ethers.Contract(tokenIn,["function allowance(address,address) view returns(uint256)"],provider);
    if(await erc.allowance!(sender,env.kyberRouter)<amountIn) throw new Error("approval required before executable route proof");
    await provider.call(tx); gas=(await provider.estimateGas(tx)).toString();
  }
  return {venue:"kyber",target:env.kyberRouter,taker:sender,recipient:decoded.dstReceiver,amountInRaw:amountIn.toString(),quotedOutRaw:quoted.toString(),minOutRaw:minOut.toString(),slippageBps,calldata:built.data,value:String(tx.value),gas,executable:!quoteOnly,quoteOnly,
    proof:{selector:decoded.selector,dstReceiver:decoded.dstReceiver,srcToken:decoded.srcToken,dstToken:decoded.dstToken,minReturnAmount:String(decoded.minReturnAmount),decodedAt:new Date().toISOString()}};
}

/** Human route breakdown: "60% uniswapv3 · 40% up-v3". */
export function routeBreakdown(rs: any): string {
  const amountIn = BigInt(rs?.amountIn || "0");
  if (amountIn === 0n || !Array.isArray(rs?.route)) return "";
  const parts: string[] = [];
  for (const path of rs.route) {
    if (!path?.length) continue;
    const pct = Number((BigInt(path[0].swapAmount || "0") * 1000n) / amountIn) / 10;
    const names = [...new Set(path.map((h: any) => h.exchange))].join("→");
    parts.push(`${pct}% ${names}`);
  }
  return parts.join(" · ");
}
