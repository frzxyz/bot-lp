import { ethers } from "ethers";
import { C } from "../../config.js";
import { dataPath, readJson, writeJson } from "../../util/files.js";
import { provider, wallet } from "../client.js";
import { kyberRoute, kyberSwap } from "../kyber.js";
import { V4_POSM_ABI } from "./abis.js";
import { closeV4Position } from "./close.js";
import { NATIVE } from "./poolkey.js";

export const USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168";
export const PENDING_FILE = dataPath("v4-settlement-pending.json");
export type PendingSettlement = {tokenId:string, token:string, amountRaw:string, closeTx:string, createdAt:number, directUsdgRaw?:string, error?:string};
export const closeReceiptDelta=(before:bigint,after:bigint):bigint=>after>before?after-before:0n;

const balance = async (token:string):Promise<bigint> => {
  const w=wallet();
  if(token.toLowerCase()===NATIVE) return provider.getBalance(w.address);
  return new ethers.Contract(token,["function balanceOf(address) view returns(uint256)"],provider).balanceOf!(w.address);
};
const savePending=(p:PendingSettlement)=>{const all=readJson<Record<string,PendingSettlement>>(PENDING_FILE,{});all[p.tokenId]=p;writeJson(PENDING_FILE,all);};
const nftGone=async(id:string):Promise<boolean>=>{
  const p=new ethers.Contract(C.v4PositionManager!,V4_POSM_ABI,provider);
  const owner=await p.ownerOf!(id).catch(()=>ethers.ZeroAddress);
  const liq:bigint=await p.getPositionLiquidity!(id).catch(()=>0n);
  return owner.toLowerCase()!==wallet().address.toLowerCase() || liq===0n;
};

/** Close then settle only the raw token balance delta produced by that close. */
export async function closeUsdg(tokenId:string){
  const p=new ethers.Contract(C.v4PositionManager!,V4_POSM_ABI,provider);
  const [pk]=await p.getPoolAndPositionInfo!(tokenId);
  const currencies=[String(pk.currency0),String(pk.currency1)];
  if(!currencies.some(x=>x.toLowerCase()===USDG)) throw new Error("position is not USDG paired");
  const token=currencies.find(x=>x.toLowerCase()!==USDG)!;
  if(token.toLowerCase()===NATIVE) throw new Error("USDG settlement requires ERC20 non-USDG currency");
  // Snapshot both legs so unrelated preexisting wallet USDG is never attributed.
  const [before,beforeUsdg]=await Promise.all([balance(token),balance(USDG)]);
  const closed=await closeV4Position(tokenId);
  const [after,afterCloseUsdg]=await Promise.all([balance(token),balance(USDG)]);
  const delta=closeReceiptDelta(before,after), directUsdgRaw=closeReceiptDelta(beforeUsdg,afterCloseUsdg);
  if(!(await nftGone(tokenId))) throw new Error("close receipt mined but NFT/liquidity still open");
  if(delta===0n) return {closeTx:closed.txHash,swapTx:null,token,closeDeltaRaw:"0",directUsdgRaw:directUsdgRaw.toString(),liquidationUsdgRaw:"0",totalUsdgProceedsRaw:directUsdgRaw.toString(),nftGone:true,settlementComplete:true};
  const pending:PendingSettlement={tokenId,token,amountRaw:delta.toString(),closeTx:closed.txHash,createdAt:Date.now(),directUsdgRaw:directUsdgRaw.toString()};
  savePending(pending); // durable before external quote/build/approval
  try {
    const fresh=await kyberRoute(token,USDG,delta);
    if(!fresh) throw new Error("no fresh Kyber token→USDG route");
    const swapped=await kyberSwap(token,USDG,delta);
    if(!swapped || swapped.amountOut<=0n) throw new Error("Kyber settlement returned no USDG");
    const all=readJson<Record<string,PendingSettlement>>(PENDING_FILE,{});delete all[tokenId];writeJson(PENDING_FILE,all);
    const total=directUsdgRaw+swapped.amountOut;
    return {closeTx:closed.txHash,swapTx:swapped.tx,token,closeDeltaRaw:delta.toString(),directUsdgRaw:directUsdgRaw.toString(),liquidationUsdgRaw:swapped.amountOut.toString(),totalUsdgProceedsRaw:total.toString(),nftGone:true,settlementComplete:true};
  } catch(e) {
    pending.error=(e as Error).message.slice(0,300);savePending(pending);
    throw new Error(`close succeeded; settlement pending for ${delta} raw ${token}: ${pending.error}`);
  }
}

export async function retryUsdgSettlement(tokenId:string){
  const all=readJson<Record<string,PendingSettlement>>(PENDING_FILE,{}), p=all[tokenId];
  if(!p) throw new Error("no settlement-pending state for tokenId");
  if(p.directUsdgRaw===undefined) throw new Error("legacy settlement lacks exact direct USDG delta; refusing settlement without attributable total");
  if(!(await nftGone(tokenId))) throw new Error("refuse settlement retry while NFT is open");
  const amount=BigInt(p.amountRaw), bal=await balance(p.token);
  if(amount<=0n || bal<amount) throw new Error("wallet no longer holds exact pending close delta");
  if(!await kyberRoute(p.token,USDG,amount)) throw new Error("no fresh Kyber token→USDG route");
  const swapped=await kyberSwap(p.token,USDG,amount);
  if(!swapped || swapped.amountOut<=0n) throw new Error("Kyber settlement returned no USDG");
  delete all[tokenId];writeJson(PENDING_FILE,all);
  const direct=BigInt(p.directUsdgRaw), total=direct+swapped.amountOut;
  return {closeTx:p.closeTx,swapTx:swapped.tx,token:p.token,closeDeltaRaw:p.amountRaw,directUsdgRaw:direct.toString(),liquidationUsdgRaw:swapped.amountOut.toString(),totalUsdgProceedsRaw:total.toString(),nftGone:true,settlementComplete:true};
}
