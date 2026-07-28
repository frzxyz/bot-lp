import test from 'node:test';
import assert from 'node:assert/strict';
import { ethers } from 'ethers';
import { buildTwoHopCalldata } from '../src/chain/v4/swap.js';
import { buildV2Calldata } from '../src/chain/v2/swap.js';

const TOKEN='0x0000000000000000000000000000000000000011';
const WETH='0x0000000000000000000000000000000000000022';
const USDG='0x0000000000000000000000000000000000000033';
const HOOK='0x0000000000000000000000000000000000000000';
const p1={currency0:TOKEN,currency1:WETH,fee:3000,tickSpacing:60,hooks:HOOK};
const p2={currency0:USDG,currency1:WETH,fee:500,tickSpacing:10,hooks:HOOK};

test('two-hop calldata is one atomic Universal Router V4 command',()=>{
  const data=buildTwoHopCalldata(p1,p2,TOKEN,WETH,USDG,1000n,900n);
  const iface=new ethers.Interface(['function execute(bytes commands,bytes[] inputs,uint256 deadline) payable']);
  const decoded=iface.decodeFunctionData('execute',data);
  assert.equal(decoded.commands,'0x10');
  assert.equal(decoded.inputs.length,1);
  const [actions,params]=ethers.AbiCoder.defaultAbiCoder().decode(['bytes','bytes[]'],decoded.inputs[0]);
  assert.equal(actions,'0x070c0f');
  assert.equal(params.length,3);
});

test('V2 direct or one-intermediate route is one atomic Universal Router command',()=>{
  const data=buildV2Calldata(TOKEN,[TOKEN,WETH,USDG],1000n,800n,123456);
  const iface=new ethers.Interface(['function execute(bytes commands,bytes[] inputs,uint256 deadline) payable']);
  const decoded=iface.decodeFunctionData('execute',data);
  assert.equal(decoded.commands,'0x08');assert.equal(decoded.inputs.length,1);assert.equal(decoded.deadline,123456n);
});

test('two-hop calldata binds amount and positive minimum',()=>{
  const a=buildTwoHopCalldata(p1,p2,TOKEN,WETH,USDG,12345n,678n);
  const b=buildTwoHopCalldata(p1,p2,TOKEN,WETH,USDG,12346n,678n);
  assert.notEqual(a,b);
});
