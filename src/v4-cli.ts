#!/usr/bin/env node
/** Machine-readable V4 sidecar for the Hermes Python LP orchestrator.
 * Secrets stay in RH_WALLET_KEY env; this CLI never prints them.
 */
import fs from "node:fs";
import { discoverV4Erc20Pools, discoverV4UsdgPools, USDG } from "./chain/v4/discover.js";
import { quoteV4, swapErc20V4, quoteV4TwoHop, swapErc20V4TwoHop } from "./chain/v4/swap.js";
import { listV4Positions } from "./chain/v4/list.js";
import { collectV4Fees, closeV4Position } from "./chain/v4/close.js";
import { openV4UsdgInRange, openV4UsdgKyberInRange, openV4UsdgSingle, v4UsdgInRangeSplit } from "./chain/v4/mint.js";
import { kyberRoute, kyberPreflight } from "./chain/kyber.js";
import { parseBoundedRaw } from "./chain/v4/safety.js";
import { closeUsdg, retryUsdgSettlement } from "./chain/v4/settlement.js";
import { quoteV2Path, swapV2Path } from "./chain/v2/swap.js";
import { ethers } from "ethers";
import { provider, wallet } from "./chain/client.js";
import { cfg, C } from "./config.js";

const HALT_FILE = process.env.RH_V4_HALT_FILE || "/root/.hermes/state/rh_meme_lp_halt";
async function executionGovernor(cmd: string, amountEth?: string): Promise<void> {
  if (process.env.RH_V4_EXECUTION !== "I_ACKNOWLEDGE_SAFE_V4_EXECUTION") throw new Error("V4 execution locked; explicit execution policy missing");
  if (fs.existsSync(HALT_FILE)) throw new Error("V4 execution halted by kill-switch");
  const net = await provider.getNetwork();
  if (Number(net.chainId) !== 4663 || cfg.chainId !== 4663) throw new Error("wrong chain for V4 execution");
  const expected = process.env.RH_V4_EXPECTED_WALLET;
  if (!expected || wallet().address.toLowerCase() !== expected.toLowerCase()) throw new Error("wallet identity gate failed");
  if (cmd === "open-usdg") {
    const amount = Number(amountEth);
    const cap = Number(process.env.RH_V4_MAX_ETH || "0.001");
    if (!Number.isFinite(amount) || amount <= 0 || !Number.isFinite(cap) || cap <= 0 || amount > cap) throw new Error(`V4 amount exceeds cap ${cap} ETH`);
  }
  if (cmd === "open-usdg-single" || cmd === "open-usdg-kyber") {
    const hardCap=250_000_000n; // independent 250 USDG ceiling (6 decimals)
    const requested=parseBoundedRaw(amountEth || "", process.env.RH_V4_MAX_USDG_RAW || hardCap.toString());
    if(requested>hardCap) throw new Error("V4 amount exceeds hard 250 USDG sidecar cap");
  }
}

function out(x: unknown): never {
  console.log(JSON.stringify(x, (_k, v) => typeof v === "bigint" ? v.toString() : v));
  process.exit(0);
}
function fail(e: unknown): never {
  const m = e instanceof Error ? e.message : String(e);
  console.error(JSON.stringify({ok:false,error:m.slice(0,400)}));
  process.exit(1);
}

async function main() {
  const [cmd, arg1, arg2, arg3, arg4, arg5] = process.argv.slice(2);
  const moving = new Set(["collect", "close", "close-usdg", "retry-settlement", "open-usdg", "open-usdg-single", "open-usdg-kyber", "swap-exit", "swap-path", "swap-v2"]);
  if (cmd && moving.has(cmd)) await executionGovernor(cmd, arg2);
  if (cmd === "discover") {
    if (!arg1) throw new Error("token required");
    const pools = await discoverV4UsdgPools(arg1);
    out({ok:true,pools});
  }
  if (cmd === "discover-pair") {
    if (!arg1 || !arg2) throw new Error("discover-pair TOKEN SETTLEMENT required");
    out({ok:true,pools:await discoverV4Erc20Pools(arg1,arg2)});
  }
  if (cmd === "quote") {
    if (!arg1 || !arg2) throw new Error("quote TOKEN AMOUNT_RAW required");
    const pools = await discoverV4UsdgPools(arg1);
    const rows=[];
    for (const p of pools) {
      const z = p.poolKey.currency0.toLowerCase() === "0x5fc5360d0400a0fd4f2af552add042d716f1d168";
      try { rows.push({pool:p,zeroForOne:z,amountOut:await quoteV4(p.poolKey,z,BigInt(arg2))}); }
      catch (e) { rows.push({pool:p,zeroForOne:z,error:(e as Error).message.slice(0,180)}); }
    }
    out({ok:true,quotes:rows});
  }
  if (cmd === "quote-exit") {
    if (!arg1 || !arg2) throw new Error("quote-exit TOKEN AMOUNT_RAW required");
    const pools = await discoverV4UsdgPools(arg1), rows=[];
    for (const p of pools) {
      const z = p.poolKey.currency0.toLowerCase() === arg1.toLowerCase();
      try { rows.push({pool:p,zeroForOne:z,amountOut:await quoteV4(p.poolKey,z,BigInt(arg2))}); }
      catch (e) { rows.push({pool:p,zeroForOne:z,error:(e as Error).message.slice(0,180)}); }
    }
    out({ok:true,quotes:rows});
  }
  if (cmd === "swap-exit") {
    if (!arg1 || !arg2 || !arg3) throw new Error("swap-exit TOKEN AMOUNT_RAW MIN_OUT_RAW required");
    const amount=parseBoundedRaw(arg2,process.env.RH_V4_MAX_TOKEN_RAW||((1n<<128n)-1n).toString());
    const minimum=parseBoundedRaw(arg3,(1n<<128n).toString());
    const pools=(await discoverV4UsdgPools(arg1)).filter(p=>p.liquidity>0n&&p.fee>0&&p.fee<=100000);
    const ranked=[];
    for(const p of pools){const z=p.poolKey.currency0.toLowerCase()===arg1.toLowerCase();try{ranked.push({p,out:await quoteV4(p.poolKey,z,amount)});}catch{}}
    ranked.sort((a,b)=>b.out>a.out?1:b.out<a.out?-1:0);
    if(!ranked.length || ranked[0]!.out<minimum) throw new Error("no executable V4 exit meeting minimum");
    out({ok:true,result:await swapErc20V4(ranked[0]!.p.poolKey,arg1,amount,minimum),poolId:ranked[0]!.p.poolId,quotedOut:ranked[0]!.out});
  }
  if (cmd === "quote-path" || cmd === "swap-path") {
    if (!arg1 || !arg2) throw new Error(`${cmd} TOKEN AMOUNT_RAW required`);
    const amount=parseBoundedRaw(arg2,process.env.RH_V4_MAX_TOKEN_RAW||((1n<<128n)-1n).toString()),weth=ethers.getAddress(C.weth);
    const first=(await discoverV4Erc20Pools(arg1,weth)).filter(p=>p.liquidity>0n&&p.fee>0&&p.fee<=100000);
    const second=(await discoverV4Erc20Pools(weth,USDG)).filter(p=>p.liquidity>0n&&p.fee>0&&p.fee<=100000);
    const ranked=[];
    for(const a of first)for(const b of second){try{ranked.push({a,b,out:await quoteV4TwoHop(a.poolKey,b.poolKey,arg1,weth,USDG,amount)});}catch{}}
    ranked.sort((a,b)=>b.out>a.out?1:b.out<a.out?-1:0);
    if(cmd==="quote-path")out({ok:true,quotes:ranked.map(x=>({amountOut:x.out,firstPoolId:x.a.poolId,secondPoolId:x.b.poolId,firstFee:x.a.fee,secondFee:x.b.fee,firstLiquidity:x.a.liquidity,secondLiquidity:x.b.liquidity}))});
    if(!arg3||!arg4||!arg5)throw new Error("swap-path TOKEN AMOUNT_RAW MIN_OUT_RAW FIRST_POOL_ID SECOND_POOL_ID required");
    const chosen=ranked.find(x=>x.a.poolId.toLowerCase()===arg4.toLowerCase()&&x.b.poolId.toLowerCase()===arg5.toLowerCase());
    const minimum=parseBoundedRaw(arg3,(1n<<128n).toString());
    if(!chosen||chosen.out<minimum)throw new Error("bound V4 path unavailable or below minimum");
    out({ok:true,result:await swapErc20V4TwoHop(chosen.a.poolKey,chosen.b.poolKey,arg1,weth,USDG,amount,minimum),firstPoolId:chosen.a.poolId,secondPoolId:chosen.b.poolId,quotedOut:chosen.out});
  }
  if(cmd==="quote-v2"||cmd==="swap-v2"){
    if(!arg1||!arg2)throw new Error(`${cmd} TOKEN AMOUNT_RAW required`);
    const amount=parseBoundedRaw(arg2,process.env.RH_V4_MAX_TOKEN_RAW||((1n<<128n)-1n).toString()),weth=ethers.getAddress(C.weth);
    const candidates=[];
    for(const x of [{id:'direct',path:[arg1,USDG]},{id:'via-weth',path:[arg1,weth,USDG]}]){try{const q=await quoteV2Path(x.path,amount);candidates.push({...x,...q});}catch{}}
    candidates.sort((a,b)=>b.amountOut>a.amountOut?1:b.amountOut<a.amountOut?-1:0);
    if(cmd==='quote-v2')out({ok:true,quotes:candidates.map(x=>({routeId:x.id,path:x.path,amountOut:x.amountOut,minimumReserve:x.minimumReserve}))});
    if(!arg3||!arg4)throw new Error('swap-v2 TOKEN AMOUNT_RAW MIN_OUT_RAW ROUTE_ID required');
    const chosen=candidates.find(x=>x.id===arg4),minimum=parseBoundedRaw(arg3,(1n<<128n).toString());
    if(!chosen||chosen.amountOut<minimum)throw new Error('bound V2 route unavailable or below minimum');
    out({ok:true,result:await swapV2Path(chosen.path,amount,minimum),routeId:chosen.id,quotedOut:chosen.amountOut});
  }
  if (cmd === "preflight") {
    if (!arg1 || !arg2) throw new Error("preflight TOKEN USDG_RAW required");
    const amount=BigInt(arg2), forward=await kyberRoute("0x5fc5360d0400a0fd4f2af552add042d716f1d168",arg1,amount);
    if(!forward || BigInt(forward.routeSummary.amountOut)<=0n) throw new Error("no forward Kyber route");
    const reverse=await kyberRoute(arg1,"0x5fc5360d0400a0fd4f2af552add042d716f1d168",BigInt(forward.routeSummary.amountOut));
    if(!reverse || BigInt(reverse.routeSummary.amountOut)<=0n) throw new Error("no reverse Kyber route");
    out({ok:true,forwardRaw:forward.routeSummary.amountOut,reverseRaw:reverse.routeSummary.amountOut});
  }
  if (cmd === "reverse-preflight" || cmd === "reverse-quote") {
    if (!arg1 || !arg2) throw new Error("reverse-preflight TOKEN TOKEN_RAW required");
    out({ok:true,proof:await kyberPreflight(arg1,"0x5fc5360d0400a0fd4f2af552add042d716f1d168",BigInt(arg2),cmd==="reverse-quote")});
  }
  if (cmd === "list") out({ok:true,positions:await listV4Positions()});
  if (cmd === "collect") {
    if (!arg1) throw new Error("tokenId required");
    out({ok:true,result:await collectV4Fees(arg1)});
  }
  if (cmd === "close") {
    if (!arg1) throw new Error("tokenId required");
    out({ok:true,result:await closeV4Position(arg1)});
  }
  if (cmd === "close-usdg") {
    if (!arg1) throw new Error("tokenId required");
    out({ok:true,result:await closeUsdg(arg1)});
  }
  if (cmd === "retry-settlement") {
    if (!arg1) throw new Error("tokenId required");
    out({ok:true,result:await retryUsdgSettlement(arg1)});
  }
  if (cmd === "open-usdg") {
    if (!arg1 || !arg2) throw new Error("open-usdg TOKEN ETH_AMOUNT required");
    const pools=(await discoverV4UsdgPools(arg1)).filter(p=>p.liquidity>0n && p.fee<0x800000 && p.poolKey.hooks === "0x0000000000000000000000000000000000000000");
    if (!pools.length) throw new Error("no eligible static-fee, hookless V4 USDG pool");
    // Pick by executable net output for 1 USDG, never by nominal LP fee. This prevents
    // selecting pathological 50-99% fee pools that would destroy capital at entry/exit.
    const ranked=[];
    for (const p of pools) {
      const z=p.poolKey.currency0.toLowerCase() === "0x5fc5360d0400a0fd4f2af552add042d716f1d168";
      try { ranked.push({p,out:await quoteV4(p.poolKey,z,1_000_000n)}); } catch {}
    }
    ranked.sort((a,b)=>b.out>a.out?1:b.out<a.out?-1:0);
    if (!ranked.length) throw new Error("all eligible V4 quotes reverted");
    out({ok:true,result:await openV4UsdgInRange(ranked[0]!.p,arg2)});
  }
  if (cmd === "open-usdg-single") {
    if (!arg1 || !arg2) throw new Error("open-usdg-single TOKEN USDG_RAW required");
    const budget=parseBoundedRaw(arg2,process.env.RH_V4_MAX_USDG_RAW||"250000000");
    const pools=(await discoverV4UsdgPools(arg1)).filter(p=>p.liquidity>0n&&p.fee<0x800000&&p.poolKey.hooks==="0x0000000000000000000000000000000000000000");
    pools.sort((a,b)=>b.liquidity>a.liquidity?1:b.liquidity<a.liquidity?-1:a.fee-b.fee);
    if(!pools.length) throw new Error("no liquid static-fee, hookless V4 USDG pool");
    out({ok:true,result:await openV4UsdgSingle(pools[0]!,budget)});
  }
  if (cmd === "open-usdg-kyber") {
    if (!arg1 || !arg2) throw new Error("open-usdg-kyber TOKEN USDG_RAW required");
    const budget=parseBoundedRaw(arg2,process.env.RH_V4_MAX_USDG_RAW||"250000000");
    const pools=(await discoverV4UsdgPools(arg1)).filter(p=>p.liquidity>0n&&p.fee<0x800000&&p.poolKey.hooks==="0x0000000000000000000000000000000000000000");
    const ranked: {p:(typeof pools)[number]; v4out:bigint}[]=[];
    for(const p of pools){
      const split=v4UsdgInRangeSplit(p,budget), z=p.poolKey.currency0.toLowerCase()==="0x5fc5360d0400a0fd4f2af552add042d716f1d168";
      try {
        const [v4out,route]=await Promise.all([quoteV4(p.poolKey,z,split.swap),kyberRoute("0x5fc5360d0400a0fd4f2af552add042d716f1d168",arg1,split.swap)]);
        if(v4out>0n&&route) ranked.push({p,v4out});
      } catch { /* ineligible quote/route */ }
    }
    ranked.sort((a,b)=>b.v4out>a.v4out?1:b.v4out<a.v4out?-1:b.p.liquidity>a.p.liquidity?1:-1);
    if(!ranked.length) throw new Error("no eligible executable V4 pool with a Kyber USDG→token route");
    const width=arg3===undefined?25:Number(arg3);
    out({ok:true,result:await openV4UsdgKyberInRange(ranked[0]!.p,budget,width)});
  }
  throw new Error("commands: discover|quote|list|collect|close|close-usdg|retry-settlement|open-usdg|open-usdg-single|open-usdg-kyber");
}
main().catch(fail);
