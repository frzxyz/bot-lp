import { ethers } from "ethers";
import fs from "node:fs";
const RPC=process.env.RH_RPC_URL||"https://rpc.mainnet.chain.robinhood.com";
const p=new ethers.JsonRpcProvider(RPC,4663);
const args=process.argv.slice(2); const cmd=args[0];
const expectedOwner="0x3582605Edebf376b684a45E8Faa6D808C22a8e3e";
const constants=[expectedOwner,"0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168","0x1f7d7550b1b028f7571e69a784071f0205fd2efa","0x73991a25c818bf1f1128deaab1492d45638de0d3","0xcaf681a66d020601342297493863e78c959e5cb2"];
const abi=JSON.parse(fs.readFileSync("artifacts/atomic/AtomicV3Executor.abi.json","utf8"));
if(cmd==="verify"){
 const a=ethers.getAddress(args[1]); const c=new ethers.Contract(a,abi,p); const code=await p.getCode(a); if(code==="0x")throw Error("no code");
 const got=await Promise.all([c.owner(),c.USDG(),c.FACTORY(),c.POSITION_MANAGER(),c.SWAP_ROUTER()]);
 got.forEach((x,i)=>{if(ethers.getAddress(x)!==ethers.getAddress(constants[i]))throw Error(`constant ${i} mismatch`)});
 console.log(JSON.stringify({address:a,chainId:(await p.getNetwork()).chainId.toString(),codeBytes:(code.length-2)/2,owner:got[0],paused:await c.paused()},null,2));
} else if(cmd==="deploy"){
 if(!args.includes("--broadcast")) throw Error("SAFE DEFAULT: deployment not broadcast; add --broadcast only after parent safety gates");
 const key=process.env.RH_WALLET_KEY; if(!key)throw Error("RH_WALLET_KEY missing (never pass on command line)"); const w=new ethers.Wallet(key,p); if(ethers.getAddress(w.address)!==ethers.getAddress(expectedOwner))throw Error("wrong signer");
 const bin=fs.readFileSync("artifacts/atomic/AtomicV3Executor.bin","utf8").trim(); const f=new ethers.ContractFactory(abi,"0x"+bin,w); const d=await f.deploy(...constants); await d.waitForDeployment(); const meta={chainId:4663,address:await d.getAddress(),owner:w.address,txHash:d.deploymentTransaction()?.hash,constants,createdAt:new Date().toISOString()}; fs.mkdirSync("deployments",{recursive:true});fs.writeFileSync("deployments/4663-atomic-v3.json",JSON.stringify(meta,null,2));console.log(JSON.stringify(meta,null,2));
} else throw Error("usage: atomic-admin.ts verify ADDRESS | deploy --broadcast");
