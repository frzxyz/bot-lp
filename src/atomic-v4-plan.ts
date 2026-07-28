import fs from "node:fs";
import path from "node:path";
import { ethers } from "ethers";
import sdkCore from "@uniswap/sdk-core";
import v4sdk from "@uniswap/v4-sdk";
import { provider } from "./chain/client.js";
import { C, cfg, env } from "./config.js";
import { kyberBuild, kyberRoute } from "./chain/kyber.js";
import { assertKyberCalldata } from "./chain/kyberDecode.js";
import { discoverV4UsdgPools } from "./chain/v4/discover.js";
import { tokenMeta } from "./chain/tokens.js";

const { Token, Percent } = sdkCore as any;
const { Pool, Position, V4PositionManager } = v4sdk as any;
export const OWNER="0x3582605Edebf376b684a45E8Faa6D808C22a8e3e";
export const USDG="0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168";
export const EXECUTOR=process.env.ATOMIC_V4_EXECUTOR_ADDRESS||"0xF8b9C6A7453a52A3F515824939a3c69292f81921";
export const PENGUZILLA="0x2AD022332400df20948f20F593C1074B88f6E753";
export const PENGU_POOL_ID="0x534e928df1baddddef8d52c806959da4d9b0061db25d065575a7addf99057668";
const POS_ABI=["function getPoolAndPositionInfo(uint256) view returns((address currency0,address currency1,uint24 fee,int24 tickSpacing,address hooks),uint256)","function getPositionLiquidity(uint256) view returns(uint128)","function ownerOf(uint256) view returns(address)"];
const STATE_ABI=["function getSlot0(bytes32) view returns(uint160 sqrtPriceX96,int24 tick,uint24 protocolFee,uint24 lpFee)","function getLiquidity(bytes32) view returns(uint128)"];
const ERC20_ABI=["function balanceOf(address) view returns(uint256)"];
const floor=(n:bigint,bps:number)=>n*BigInt(10_000-bps)/10_000n;
const poolId=(p:any)=>ethers.keccak256(ethers.AbiCoder.defaultAbiCoder().encode(["address","address","uint24","int24","address"],[p.currency0,p.currency1,p.fee,p.tickSpacing,p.hooks]));
function strict(n:bigint,label:string){if(n<=0n)throw Error(`${label} is zero`);return n;}
async function kyber(tokenIn:string,tokenOut:string,amount:bigint,slippageBps:number){
  const route=await kyberRoute(tokenIn,tokenOut,amount); if(!route)throw Error("no Kyber route");
  const built=await kyberBuild(route.routeSummary,EXECUTOR,EXECUTOR,slippageBps); if(!built)throw Error("Kyber build unavailable");
  if(ethers.getAddress(built.routerAddress)!==ethers.getAddress(env.kyberRouter))throw Error("Kyber router mismatch");
  if(BigInt(built.amountIn)!==amount)throw Error("Kyber changed exact input");
  // Asking for an executor-targeted build is not the same as getting one. The
  // executor swaps as itself and measures its own balance delta, so calldata that
  // delivers anywhere else fails its slippage check after the funds have already
  // moved. Decode and prove the descriptor before it can reach a plan file.
  const quoted=BigInt(route.routeSummary.amountOut);
  const decoded=assertKyberCalldata(built.data as string,{
    tokenIn,tokenOut,recipient:EXECUTOR,amountIn:amount,minAmountOut:floor(quoted,slippageBps)});
  return { data:built.data as string, out:strict(BigInt(built.amountOut),"Kyber output"), quoted,
           decoded:{selector:decoded.selector,dstReceiver:decoded.dstReceiver,minReturnAmount:String(decoded.minReturnAmount)} };
}
export function selectEligiblePool(pools:any[],requestedPoolId?:string){
  const eligible=pools.filter(p=>p.liquidity>0n&&p.tickSpacing>0&&poolId(p.poolKey).toLowerCase()===p.poolId.toLowerCase());
  return requestedPoolId?eligible.find(p=>p.poolId.toLowerCase()===requestedPoolId.toLowerCase()):eligible.sort((a,b)=>a.liquidity===b.liquidity?b.fee-a.fee:(a.liquidity>b.liquidity?-1:1))[0];
}
export function nonSettlementToken(poolKey:any){
  const c0=ethers.getAddress(poolKey.currency0),c1=ethers.getAddress(poolKey.currency1),stable=ethers.getAddress(USDG);
  if(c0!==stable&&c1!==stable)throw Error("position pool is not USDG-paired");
  if(c0===stable&&c1===stable)throw Error("position pool has no non-USDG token");
  return c0===stable?c1:c0;
}
async function eligiblePool(token:string,requestedPoolId?:string){
  const t=ethers.getAddress(token);if(t===ethers.getAddress(USDG))throw Error("candidate token cannot be USDG");
  const found=selectEligiblePool(await discoverV4UsdgPools(t),requestedPoolId);
  if(!found)throw Error("no discovered live USDG-paired eligible V4 pool");
  return found;
}
// A fixed spacing count produces a completely different economic width per fee
// tier (8 spacings is ~8% at fee 10000 but ~0.8% at fee 500). RH_V4_RANGE_PCT lets
// the caller ask for a real percentage half-width, converted to whole spacings.
function halfSpacings(widthSpacings:number,sp:number){
  const pct=Number(process.env.RH_V4_RANGE_PCT||"0");
  if(!(pct>0))return Math.max(1,Math.floor(widthSpacings/2));
  if(pct>=100)throw Error("RH_V4_RANGE_PCT must be below 100");
  return Math.max(1,Math.round(Math.log1p(pct/100)/Math.log(1.0001)/sp));
}
function sdkPool(p:any,m0:any,m1:any){return new Pool(new Token(cfg.chainId,ethers.getAddress(p.poolKey.currency0),m0.decimals,m0.symbol),new Token(cfg.chainId,ethers.getAddress(p.poolKey.currency1),m1.decimals,m1.symbol),p.fee,p.tickSpacing,p.poolKey.hooks,p.sqrtPriceX96.toString(),p.liquidity.toString(),p.tick);}
export async function generateOpenPlan(token:string,budget:bigint,outFile:string,slippageBps=500,widthSpacings=8,requestedPoolId?:string){
  if(budget<=0n)throw Error("budget must be positive raw USDG");
  if(slippageBps<1||slippageBps>2000)throw Error("slippage bps must be 1..2000");
  const candidate=ethers.getAddress(token),p=await eligiblePool(candidate,requestedPoolId), [m0,m1]=await Promise.all([tokenMeta(p.poolKey.currency0),tokenMeta(p.poolKey.currency1)]), sp=p.tickSpacing;
  const anchor=Math.floor(p.tick/sp)*sp, half=halfSpacings(widthSpacings,sp), tickLower=anchor-half*sp,tickUpper=anchor+half*sp;
  if(!(tickLower<p.tick&&p.tick<tickUpper))throw Error("generated range is not strictly in range");
  // Quote half first, then let the SDK cap liquidity by the exact operation outputs. No balanceOf(wallet) is read.
  const swapAmount=budget/2n; if(swapAmount<=0n||swapAmount>=budget)throw Error("budget too small to split");
  const k=await kyber(USDG,candidate,swapAmount,slippageBps), retained=budget-swapAmount;
  const pool=sdkPool(p,m0,m1), usdg0=p.poolKey.currency0.toLowerCase()===USDG.toLowerCase(), avail0=usdg0?retained:k.out,avail1=usdg0?k.out:retained;
  const mk=(x:bigint,y:bigint)=>Position.fromAmounts({pool,tickLower,tickUpper,amount0:x.toString(),amount1:y.toString(),useFullPrecision:true});
  const slip=new Percent(slippageBps,10_000); let position=mk(avail0,avail1), max=position.mintAmountsWithSlippage(slip), ppm=1_000_000n;
  let max0=BigInt(max.amount0.toString()),max1=BigInt(max.amount1.toString());
  if(max0>avail0)ppm=(avail0*1_000_000n)/max0;if(max1>avail1){const r=(avail1*1_000_000n)/max1;if(r<ppm)ppm=r;}
  if(ppm<1_000_000n)position=mk(avail0*ppm*999n/1_000_000_000n,avail1*ppm*999n/1_000_000_000n);
  if(BigInt(position.liquidity.toString())<=0n)throw Error("SDK liquidity is zero");
  max=position.mintAmountsWithSlippage(slip);max0=BigInt(max.amount0.toString());max1=BigInt(max.amount1.toString());
  if(max0<=0n||max1<=0n||max0>avail0||max1>avail1)throw Error("SDK mint caps exceed operation-only balances");
  const deadline=Math.floor(Date.now()/1000)+1200;
  const call=V4PositionManager.addCallParameters(position,{recipient:OWNER,slippageTolerance:slip,deadline:String(deadline)});
  if(BigInt(call.value||0)!==0n)throw Error("ERC20 mint unexpectedly has native value");
  const amount0Min=strict(floor(BigInt(position.amount0.quotient.toString()),slippageBps),"amount0Min"), amount1Min=strict(floor(BigInt(position.amount1.quotient.toString()),slippageBps),"amount1Min");
  const plan={token:candidate,pool:{...p.poolKey,poolId:p.poolId},usdgAmount:String(budget),swapAmount:String(swapAmount),minTokenOut:String(floor(k.out,slippageBps)),amount0Min:String(amount0Min),amount1Min:String(amount1Min),deadline,swapTarget:env.kyberRouter,swapData:k.data,mintData:call.calldata,meta:{chainId:4663,executor:EXECUTOR,payer:EXECUTOR,nftRecipient:OWNER,walletInventoryUsed:false,selection:"discovered-usdg-highest-liquidity",currentTick:p.tick,tickLower,tickUpper,sqrtPriceX96:String(p.sqrtPriceX96),quotedTokenOut:String(k.quoted),builtTokenOut:String(k.out),swapProof:k.decoded,generatedAt:new Date().toISOString()}};
  fs.mkdirSync(path.dirname(outFile),{recursive:true});fs.writeFileSync(outFile,JSON.stringify(plan,null,2)+"\n");return plan;
}
export async function generateInventoryOpenPlan(outFile:string,slippageBps=500,widthSpacings=8){
  if(slippageBps<1||slippageBps>2000)throw Error("slippage bps must be 1..2000");
  const p=await eligiblePool(PENGUZILLA,PENGU_POOL_ID),[m0,m1,usdgWallet,tokenWallet]=await Promise.all([tokenMeta(p.poolKey.currency0),tokenMeta(p.poolKey.currency1),new ethers.Contract(USDG,ERC20_ABI,provider).balanceOf(OWNER) as Promise<bigint>,new ethers.Contract(PENGUZILLA,ERC20_ABI,provider).balanceOf(OWNER) as Promise<bigint>]);
  const reserve=2_000_000n,maxEntry=25_000_000n,spendable=usdgWallet>reserve?usdgWallet-reserve:0n,usdgCap=spendable<maxEntry?spendable:maxEntry;
  // Deliberately retain 1% PENG inventory: the plan cannot sweep the wallet even when PENG is limiting.
  const tokenCap=tokenWallet*99n/100n;strict(usdgCap,"spendable USDG after 2 USDG reserve");strict(tokenCap,"PENG inventory cap");
  const sp=p.tickSpacing,anchor=Math.floor(p.tick/sp)*sp,half=halfSpacings(widthSpacings,sp),tickLower=anchor-half*sp,tickUpper=anchor+half*sp;
  if(!(tickLower<p.tick&&p.tick<tickUpper))throw Error("generated range is not strictly in range");
  const pool=sdkPool(p,m0,m1),usdg0=p.poolKey.currency0.toLowerCase()===USDG.toLowerCase(),avail0=usdg0?usdgCap:tokenCap,avail1=usdg0?tokenCap:usdgCap;
  const mk=(a:bigint,b:bigint)=>Position.fromAmounts({pool,tickLower,tickUpper,amount0:a.toString(),amount1:b.toString(),useFullPrecision:true});
  const slip=new Percent(slippageBps,10_000);let position=mk(avail0,avail1),max=position.mintAmountsWithSlippage(slip),max0=BigInt(max.amount0.toString()),max1=BigInt(max.amount1.toString()),ppm=1_000_000n;
  if(max0>avail0)ppm=avail0*1_000_000n/max0;if(max1>avail1){const r=avail1*1_000_000n/max1;if(r<ppm)ppm=r;}if(ppm<1_000_000n)position=mk(avail0*ppm*999n/1_000_000_000n,avail1*ppm*999n/1_000_000_000n);
  max=position.mintAmountsWithSlippage(slip);max0=BigInt(max.amount0.toString());max1=BigInt(max.amount1.toString());if(max0<=0n||max1<=0n||max0>avail0||max1>avail1)throw Error("balanced mint exceeds inventory caps");
  const usdgPull=usdg0?max0:max1,tokenPull=usdg0?max1:max0;
  const deadline=Math.floor(Date.now()/1000)+1200,call=V4PositionManager.addCallParameters(position,{recipient:OWNER,slippageTolerance:slip,deadline:String(deadline)});if(BigInt(call.value||0)!==0n)throw Error("ERC20 mint unexpectedly has native value");
  const amount0Min=strict(floor(BigInt(position.amount0.quotient.toString()),slippageBps),"amount0Min"),amount1Min=strict(floor(BigInt(position.amount1.quotient.toString()),slippageBps),"amount1Min");
  const plan={token:PENGUZILLA,pool:{...p.poolKey,poolId:p.poolId},usdgAmount:String(usdgPull),tokenAmount:String(tokenPull),amount0Min:String(amount0Min),amount1Min:String(amount1Min),deadline,mintData:call.calldata,meta:{chainId:4663,executor:EXECUTOR,payer:EXECUTOR,nftRecipient:OWNER,walletInventoryUsed:true,noSwap:true,usdgWallet:String(usdgWallet),tokenWallet:String(tokenWallet),usdgReserve:String(reserve),maxEntryUsdg:String(maxEntry),currentTick:p.tick,tickLower,tickUpper,balancedAmount0:String(position.amount0.quotient),balancedAmount1:String(position.amount1.quotient),generatedAt:new Date().toISOString()}};
  fs.mkdirSync(path.dirname(outFile),{recursive:true});fs.writeFileSync(outFile,JSON.stringify(plan,null,2)+"\n");return plan;
}
export async function generateClosePlan(tokenId:string,outFile:string,slippageBps=500){
  if(slippageBps<1||slippageBps>2000)throw Error("slippage bps must be 1..2000");
  const pm=new ethers.Contract(C.v4PositionManager!,POS_ABI,provider), [pk]=await pm.getPoolAndPositionInfo(tokenId), liquidity:bigint=await pm.getPositionLiquidity(tokenId), owner:string=await pm.ownerOf(tokenId);
  if(ethers.getAddress(owner)!==OWNER)throw Error("position NFT is not owner-wallet owned"); strict(liquidity,"liquidity");
  const p={currency0:pk.currency0,currency1:pk.currency1,fee:Number(pk.fee),tickSpacing:Number(pk.tickSpacing),hooks:pk.hooks}, id=poolId(p);
  const positionToken=nonSettlementToken(p);
  const sv=new ethers.Contract(C.v4StateView!,STATE_ABI,provider), s0=await sv.getSlot0(id), pl=await sv.getLiquidity(id), [m0,m1]=await Promise.all([tokenMeta(p.currency0),tokenMeta(p.currency1)]);
  const info=BigInt((await pm.getPoolAndPositionInfo(tokenId))[1]), signed=(x:number)=>x>=0x800000?x-0x1000000:x, lo=signed(Number((info>>8n)&0xffffffn)),hi=signed(Number((info>>32n)&0xffffffn));
  const pos=new Position({pool:new Pool(new Token(4663,ethers.getAddress(p.currency0),m0.decimals,m0.symbol),new Token(4663,ethers.getAddress(p.currency1),m1.decimals,m1.symbol),p.fee,p.tickSpacing,p.hooks,String(s0.sqrtPriceX96),String(pl),Number(s0.tick)),liquidity:String(liquidity),tickLower:lo,tickUpper:hi});
  const a0=BigInt(pos.amount0.quotient.toString()),a1=BigInt(pos.amount1.quotient.toString()), usdg0=p.currency0.toLowerCase()===USDG.toLowerCase(), tokenExpected=usdg0?a1:a0, usdgExpected=usdg0?a0:a1;
  strict(tokenExpected,"expected token principal");strict(usdgExpected,"expected USDG principal");
  const min0=strict(floor(a0,slippageBps),"amount0Min"),min1=strict(floor(a1,slippageBps),"amount1Min"),deadline=Math.floor(Date.now()/1000)+1200;
  const coder=ethers.AbiCoder.defaultAbiCoder(), burn=coder.encode(["uint256","uint128","uint128","bytes"],[tokenId,min0,min1,"0x"]),take=coder.encode(["address","address","address"],[p.currency0,p.currency1,EXECUTOR]),unlock=coder.encode(["bytes","bytes[]"],["0x0311",[burn,take]]),closeData=new ethers.Interface(["function modifyLiquidities(bytes,uint256)"]).encodeFunctionData("modifyLiquidities",[unlock,deadline]);
  const plan={tokenId:String(tokenId),token:positionToken,pool:{...p,poolId:id},liquidity:String(liquidity),amount0Min:String(min0),amount1Min:String(min1),minSwapOut:"0",minTotalUsdg:"0",deadline,swapTarget:ethers.ZeroAddress,closeData,swapData:"0x",meta:{chainId:4663,executor:EXECUTOR,positionOwner:OWNER,closeRecipient:EXECUTOR,walletInventoryUsed:false,noSwapClose:true,currentTick:Number(s0.tick),tickLower:lo,tickUpper:hi,expectedTokenAmount:String(tokenExpected),expectedSettlementAmount:String(usdgExpected),expectedUsdgFromLp:String(usdgExpected),generatedAt:new Date().toISOString()}};
  fs.mkdirSync(path.dirname(outFile),{recursive:true});fs.writeFileSync(outFile,JSON.stringify(plan,null,2)+"\n");return plan;
}
