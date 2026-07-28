#!/usr/bin/env node
import { ethers } from "ethers";
import { provider, wallet } from "../src/chain/client.js";
import { C } from "../src/config.js";
const OWNER="0x3582605Edebf376b684a45E8Faa6D808C22a8e3e";
const USDG="0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168";
const OLD="0xeD68d9B1DA8FD01e046F17698DF620Db5925dAE9";
const NEXT="0x59A9bEd8f8a40b1b074c4eF3a1A59500E676E8A5";
const w=wallet(); if(w.address.toLowerCase()!==OWNER.toLowerCase())throw Error("wallet identity mismatch");
if(await provider.getTransactionCount(OWNER,"pending")!==await provider.getTransactionCount(OWNER,"latest"))throw Error("pending nonce exists");
const erc=new ethers.Contract(USDG,["function approve(address,uint256) returns(bool)","function allowance(address,address) view returns(uint256)"],w);
const npm=new ethers.Contract(C.positionManager,["function setApprovalForAll(address,bool)","function isApprovedForAll(address,address) view returns(bool)"],w);
const ex=new ethers.Contract(NEXT,["function setPaused(bool)","function paused() view returns(bool)"],w);
const calls=[
 ["revoke_old_usdg",erc, "approve",[OLD,0n]],
 ["revoke_old_nft",npm,"setApprovalForAll",[OLD,false]],
 ["approve_new_usdg",erc,"approve",[NEXT,250000000n]],
 ["approve_new_nft",npm,"setApprovalForAll",[NEXT,true]],
 ["unpause_new",ex,"setPaused",[false]],
] as const;
for(const [name,c,fn,args] of calls){const data=c.interface.encodeFunctionData(fn,args as any);await provider.call({from:OWNER,to:await c.getAddress(),data});const tx=await w.sendTransaction({to:await c.getAddress(),data});const r=await tx.wait();if(!r||r.status!==1)throw Error(`${name} failed`);console.error(JSON.stringify({name,hash:tx.hash,status:r.status}));}
console.log(JSON.stringify({oldAllowance:(await erc.allowance(OWNER,OLD)).toString(),oldNftApproval:await npm.isApprovedForAll(OWNER,OLD),newAllowance:(await erc.allowance(OWNER,NEXT)).toString(),newNftApproval:await npm.isApprovedForAll(OWNER,NEXT),paused:await ex.paused()}));
