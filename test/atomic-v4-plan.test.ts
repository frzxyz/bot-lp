import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { ethers } from "ethers";
import { EXECUTOR, OWNER, PENGU_POOL_ID, PENGUZILLA, USDG, selectEligiblePool, nonSettlementToken } from "../src/atomic-v4-plan.js";

const file=new URL("../plans/penguzilla-open-1usdg.json",import.meta.url);
test("generic selector rejects malformed/dead pools and chooses deepest eligible USDG pool",()=>{
  const key=(token:string,fee:number)=>({currency0:USDG,currency1:token,fee,tickSpacing:10,hooks:ethers.ZeroAddress});
  const mk=(token:string,fee:number,liquidity:bigint)=>{const poolKey=key(token,fee);return {poolKey,fee,tickSpacing:10,liquidity,poolId:ethers.keccak256(ethers.AbiCoder.defaultAbiCoder().encode(["address","address","uint24","int24","address"],[poolKey.currency0,poolKey.currency1,poolKey.fee,poolKey.tickSpacing,poolKey.hooks]))}};
  const token="0x0000000000000000000000000000000000000010",shallow=mk(token,3000,1n),deep=mk(token,500,99n),dead=mk(token,9000,0n);
  assert.equal(selectEligiblePool([shallow,dead,deep]).poolId,deep.poolId);
  assert.equal(selectEligiblePool([shallow,deep],shallow.poolId).poolId,shallow.poolId);
  assert.equal(selectEligiblePool([{...deep,poolId:ethers.ZeroHash}]),undefined);
});
test("generic close derives either USDG-paired token and rejects unrelated pools",()=>{
  const a="0x0000000000000000000000000000000000000010";
  assert.equal(nonSettlementToken({currency0:USDG,currency1:a}),ethers.getAddress(a));
  assert.equal(nonSettlementToken({currency0:a,currency1:USDG}),ethers.getAddress(a));
  assert.throws(()=>nonSettlementToken({currency0:a,currency1:"0x0000000000000000000000000000000000000020"}),/not USDG-paired/);
});
test("generated tiny open is exact-pool, strict, and inventory isolated",()=>{
  const p=JSON.parse(fs.readFileSync(file,"utf8"));
  assert.equal(p.token,PENGUZILLA); assert.equal(p.pool.poolId,PENGU_POOL_ID);
  const id=ethers.keccak256(ethers.AbiCoder.defaultAbiCoder().encode(["address","address","uint24","int24","address"],[p.pool.currency0,p.pool.currency1,p.pool.fee,p.pool.tickSpacing,p.pool.hooks]));
  assert.equal(id.toLowerCase(),PENGU_POOL_ID); assert.ok([p.pool.currency0.toLowerCase(),p.pool.currency1.toLowerCase()].includes(USDG.toLowerCase()));
  assert.ok(BigInt(p.usdgAmount)<=1_000_000n);assert.ok(BigInt(p.swapAmount)>0n&&BigInt(p.swapAmount)<BigInt(p.usdgAmount));
  for(const k of ["minTokenOut","amount0Min","amount1Min"])assert.ok(BigInt(p[k])>0n,k);
  assert.equal(p.meta.executor,EXECUTOR);assert.equal(p.meta.payer,EXECUTOR);assert.equal(p.meta.nftRecipient,OWNER);assert.equal(p.meta.walletInventoryUsed,false);
  assert.ok(p.meta.tickLower<p.meta.currentTick&&p.meta.currentTick<p.meta.tickUpper);
  assert.equal(p.swapData.slice(0,10),"0xe21fd0e9");assert.equal(p.mintData.slice(0,10),new ethers.Interface(["function modifyLiquidities(bytes,uint256)"]).getFunction("modifyLiquidities")!.selector);
});
test("inventory plan is balanced, no-swap, reserve-safe, and does not sweep PENG",()=>{
  const p=JSON.parse(fs.readFileSync(new URL("../plans/penguzilla-inventory-open.json",import.meta.url),"utf8"));
  assert.equal(p.token,PENGUZILLA);assert.equal(p.pool.poolId,PENGU_POOL_ID);assert.equal(p.meta.noSwap,true);assert.equal(p.meta.walletInventoryUsed,true);
  assert.ok(!("swapData" in p)&&!("swapTarget" in p));
  assert.ok(BigInt(p.usdgAmount)>0n&&BigInt(p.usdgAmount)<=BigInt(p.meta.usdgWallet)-2_000_000n&&BigInt(p.usdgAmount)<=25_000_000n);
  assert.ok(BigInt(p.tokenAmount)>0n&&BigInt(p.tokenAmount)<BigInt(p.meta.tokenWallet));
  assert.ok(BigInt(p.amount0Min)>0n&&BigInt(p.amount1Min)>0n);assert.ok(p.meta.tickLower<p.meta.currentTick&&p.meta.currentTick<p.meta.tickUpper);
});
