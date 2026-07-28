import { ethers } from 'ethers';
import { C } from '../../config.js';
import { provider, wallet, overrides } from '../client.js';

const FACTORY=['function getPair(address,address) view returns(address)'];
const PAIR=['function token0() view returns(address)','function getReserves() view returns(uint112,uint112,uint32)'];
const ERC20=['function allowance(address,address) view returns(uint256)','function approve(address,uint256) returns(bool)','function balanceOf(address) view returns(uint256)'];
const P2='0x000000000022D473030F116dDEE9F6B43aC78BA3';

async function reserves(tokenIn:string,tokenOut:string){
  if(!C.v2Factory)throw new Error('V2 factory unavailable');
  const f=new ethers.Contract(C.v2Factory,FACTORY,provider),pair:string=await f.getPair!(tokenIn,tokenOut);
  if(pair===ethers.ZeroAddress)throw new Error('V2 pair absent');
  const p=new ethers.Contract(pair,PAIR,provider),[t0,r]=await Promise.all([p.token0!(),p.getReserves!()]);
  const input0=ethers.getAddress(t0)===ethers.getAddress(tokenIn);
  return {pair,reserveIn:BigInt(input0?r[0]:r[1]),reserveOut:BigInt(input0?r[1]:r[0])};
}
function amountOut(amountIn:bigint,reserveIn:bigint,reserveOut:bigint){
  if(amountIn<=0n||reserveIn<=0n||reserveOut<=0n)return 0n;
  const x=amountIn*997n;
  return x*reserveOut/(reserveIn*1000n+x); // canonical 0.30% V2 fee; execution simulation is authoritative
}
export async function quoteV2Path(path:string[],input:bigint){
  if(path.length<2||path.length>3)throw new Error('only direct or one-intermediate V2 path supported');
  let out=input,minimumReserve=0n;
  for(let i=0;i<path.length-1;i++){
    const r=await reserves(path[i]!,path[i+1]!);out=amountOut(out,r.reserveIn,r.reserveOut);
    if(out<=0n)throw new Error('zero V2 quote');minimumReserve=minimumReserve===0n?r.reserveOut:(r.reserveOut<minimumReserve?r.reserveOut:minimumReserve);
  }
  return {amountOut:out,minimumReserve};
}
export function buildV2Calldata(recipient:string,path:string[],input:bigint,minimum:bigint,deadline=Math.floor(Date.now()/1000+600)){
  const params=ethers.AbiCoder.defaultAbiCoder().encode(['address','uint256','uint256','address[]','bool'],[recipient,input,minimum,path,true]);
  const ur=new ethers.Interface(['function execute(bytes commands,bytes[] inputs,uint256 deadline) payable']);
  return ur.encodeFunctionData('execute',['0x08',[params],deadline]);
}
export async function swapV2Path(path:string[],input:bigint,minimum:bigint){
  if(!C.universalRouter)throw new Error('Universal Router unavailable');
  const q=await quoteV2Path(path,input);if(q.amountOut<minimum)throw new Error('bound V2 path below minimum');
  const w=wallet(),token=ethers.getAddress(path[0]!),erc=new ethers.Contract(token,ERC20,w);
  if((await erc.allowance!(w.address,P2))<input)await(await erc.approve!(P2,input,await overrides())).wait();
  const permit=new ethers.Contract(P2,['function approve(address,address,uint160,uint48)'],w);
  await(await permit.approve!(token,C.universalRouter,input,Math.floor(Date.now()/1000)+1800,await overrides())).wait();
  const data=buildV2Calldata(w.address,path,input,minimum);
  const out=new ethers.Contract(path[path.length-1]!,ERC20,provider),before:bigint=await out.balanceOf!(w.address);
  await provider.call({to:C.universalRouter,data,from:w.address});
  const tx=await w.sendTransaction({to:C.universalRouter,data,...(await overrides())});await tx.wait();
  const received=(await out.balanceOf!(w.address) as bigint)-before;if(received<minimum)throw new Error('V2 output below minimum');
  return {tx:tx.hash,amountOut:received};
}
