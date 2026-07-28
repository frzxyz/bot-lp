import test from "node:test";
import assert from "node:assert/strict";
import { ethers } from "ethers";
import { WETH, selectEligiblePool } from "../src/atomic-v4-weth-plan.js";

const token="0x0000000000000000000000000000000000000010";
function candidate(liquidity:bigint, poolIdOverride?:string){
  const [currency0,currency1]=BigInt(WETH)<BigInt(token)?[WETH,token]:[token,WETH];
  const poolKey={currency0,currency1,fee:3000,tickSpacing:60,hooks:ethers.ZeroAddress};
  const poolId=ethers.keccak256(ethers.AbiCoder.defaultAbiCoder().encode(["address","address","uint24","int24","address"],Object.values(poolKey)));
  return {poolKey,poolId:poolIdOverride||poolId,fee:3000,tickSpacing:60,liquidity};
}
test("WETH selector is live, exact-identity, and candidate-bound",()=>{
  const live=candidate(10n),dead=candidate(0n),forged=candidate(10n,ethers.ZeroHash);
  assert.equal(selectEligiblePool([dead,forged,live],live.poolId)?.poolId,live.poolId);
  assert.equal(selectEligiblePool([live],ethers.ZeroHash),undefined);
  assert.equal(WETH.toLowerCase(),"0x0bd7d308f8e1639fab988df18a8011f41eacad73");
});
