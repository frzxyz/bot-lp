#!/usr/bin/env node
import fs from "node:fs";
import { ethers } from "ethers";
import { provider } from "../src/chain/client.js";
import { C, env } from "../src/config.js";

const OWNER = "0x3582605Edebf376b684a45E8Faa6D808C22a8e3e";
const USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168";
const PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3";
const DEFAULT_KYBER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5";
const artifact = JSON.parse(fs.readFileSync(".hardhat-artifacts/contracts/AtomicV4Executor.sol/AtomicV4Executor.json", "utf8"));
const out = (x: unknown) => console.log(JSON.stringify(x, (_k,v)=>typeof v === "bigint" ? v.toString() : v, 2));

async function verify(address: string) {
  const c = new ethers.Contract(address, artifact.abi, provider);
  const code = await provider.getCode(address);
  if (code === "0x") throw new Error("no code");
  const [network, owner, usdg, posm, permit2, target, paused] = await Promise.all([
    provider.getNetwork(), c.owner(), c.USDG(), c.POSITION_MANAGER(), c.PERMIT2(), c.SWAP_TARGET(), c.paused(),
  ]);
  if (network.chainId !== 4663n || ethers.getAddress(owner) !== OWNER || ethers.getAddress(usdg) !== USDG ||
      ethers.getAddress(posm) !== ethers.getAddress(C.v4PositionManager!) || ethers.getAddress(permit2) !== PERMIT2 ||
      ethers.getAddress(target) !== ethers.getAddress(DEFAULT_KYBER)) throw new Error("identity mismatch");
  out({address,chainId:network.chainId,codeBytes:(code.length-2)/2,owner,usdg,posm,permit2,target,paused});
}

async function deploy() {
  if (!process.argv.includes("--broadcast")) throw new Error("SAFE DEFAULT: add --broadcast after safety gates");
  if (!env.walletKey) throw new Error("RH_WALLET_KEY missing");
  const signer = new ethers.Wallet(env.walletKey, provider);
  if (ethers.getAddress(signer.address) !== OWNER) throw new Error("wrong signer");
  if ((await provider.getNetwork()).chainId !== 4663n) throw new Error("wrong chain");
  const factory = new ethers.ContractFactory(artifact.abi, artifact.bytecode, signer);
  const contract = await factory.deploy(OWNER, USDG, C.v4PositionManager!, PERMIT2, DEFAULT_KYBER);
  await contract.waitForDeployment();
  const address = await contract.getAddress();
  const tx = contract.deploymentTransaction();
  const meta = {chainId:4663,address,owner:OWNER,txHash:tx?.hash,constants:[OWNER,USDG,C.v4PositionManager,PERMIT2,DEFAULT_KYBER],createdAt:new Date().toISOString()};
  fs.mkdirSync("deployments",{recursive:true});
  fs.writeFileSync("deployments/4663-atomic-v4.json",JSON.stringify(meta,null,2));
  out(meta);
}

const [cmd,arg] = process.argv.slice(2);
if (cmd === "verify" && arg) await verify(ethers.getAddress(arg));
else if (cmd === "deploy") await deploy();
else throw new Error("usage: atomic-v4-admin.ts verify ADDRESS | deploy --broadcast");
