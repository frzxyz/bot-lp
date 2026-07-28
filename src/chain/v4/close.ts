/**
 * v4 close + fee-collect. Works for ANY pair (token/ETH, token/USDG, token/token) by
 * reconstructing the Position from the REAL pool currencies — the earlier code forced
 * native ETH as currency0, which produced wrong calldata and reverted on non-ETH pools.
 */
import { ethers } from "ethers";
import sdkCore from "@uniswap/sdk-core";
import v4sdk from "@uniswap/v4-sdk";
import { C, cfg } from "../../config.js";
import { wallet, provider, overrides } from "../client.js";
import { tokenMeta } from "../tokens.js";
import { STATEVIEW_ABI, V4_POSM_ABI } from "./abis.js";
import { NATIVE } from "./poolkey.js";
import { loadV4Deposit } from "./mint.js";
import { listV4Positions } from "./list.js";
import { ethUsd } from "../price.js";
import { appendLedger } from "../ledger.js";
import { dataPath, readJson, writeJson } from "../../util/files.js";
import { logger } from "../../util/log.js";
import { closeMinimum } from "./safety.js";
import { atomicOnlyEnabled, refuseLegacyLifecycle } from "../atomic.js";

const { Ether, Token, CurrencyAmount } = sdkCore as any;
const { Pool, Position } = v4sdk as any;
const log = logger("v4close");
const STABLES = new Set(["0x5fc5360d0400a0fd4f2af552add042d716f1d168"]); // USDG
const WETH_L = C.weth.toLowerCase();

const signed24 = (v: number): number => (v >= 0x800000 ? v - 0x1000000 : v);

function sdkCurrency(addr: string, dec: number, sym: string): any {
  return addr.toLowerCase() === NATIVE ? Ether.onChain(cfg.chainId) : new Token(cfg.chainId, ethers.getAddress(addr), dec, sym);
}

/** Reconstruct the SDK Pool + Position for a tokenId from real on-chain currencies. */
async function loadPosition(tokenId: string) {
  const posm = new ethers.Contract(C.v4PositionManager!, V4_POSM_ABI, provider);
  const [pk, infoRaw] = await posm.getPoolAndPositionInfo!(tokenId);
  const liquidity: bigint = await posm.getPositionLiquidity!(tokenId);
  const info = BigInt(infoRaw);
  const tickLower = signed24(Number((info >> 8n) & 0xffffffn));
  const tickUpper = signed24(Number((info >> 32n) & 0xffffffn));
  const c0 = pk.currency0 as string;
  const c1 = pk.currency1 as string;
  const fee = Number(pk.fee);
  const tickSpacing = Number(pk.tickSpacing);
  const [m0, m1] = await Promise.all([
    c0.toLowerCase() === NATIVE ? Promise.resolve({ symbol: "ETH", decimals: 18 }) : tokenMeta(c0).catch(() => ({ symbol: "?", decimals: 18 })),
    c1.toLowerCase() === NATIVE ? Promise.resolve({ symbol: "ETH", decimals: 18 }) : tokenMeta(c1).catch(() => ({ symbol: "?", decimals: 18 })),
  ]);
  const sv = new ethers.Contract(C.v4StateView!, STATEVIEW_ABI, provider);
  const poolId = ethers.keccak256(
    ethers.AbiCoder.defaultAbiCoder().encode(["address", "address", "uint24", "int24", "address"], [c0, c1, fee, tickSpacing, pk.hooks]),
  );
  const s0 = await sv.getSlot0!(poolId);
  const cur0 = sdkCurrency(c0, m0.decimals, m0.symbol);
  const cur1 = sdkCurrency(c1, m1.decimals, m1.symbol);
  const pool = new Pool(cur0, cur1, fee, tickSpacing, pk.hooks, s0.sqrtPriceX96.toString(), "0", Number(s0.tick));
  const position = new Position({ pool, liquidity: liquidity.toString(), tickLower, tickUpper });
  return { pool, position, cur0, cur1, c0, c1, m0, m1, fee, tickLower, tickUpper };
}

async function simulateAndSend(calldata: string, value: string, label: string): Promise<string> {
  const w = wallet();
  try {
    await provider.call({ to: C.v4PositionManager!, data: calldata, value, from: w.address });
  } catch (e) {
    throw new Error(`simulasi ${label} v4 revert: ${((e as any).shortMessage || (e as Error).message || "").slice(0, 140)}`);
  }
  const tx = await w.sendTransaction({ to: C.v4PositionManager!, data: calldata, value: BigInt(value), ...(await overrides()) });
  await tx.wait();
  return tx.hash;
}

export interface V4CloseResult {
  txHash: string;
  fee: number;
  recv0: number;
  sym0: string;
  recv1: number;
  sym1: string;
  depEth: number | null;
  pair: string;
  outEth: number; // realized value at close (ETH)
  feeEth: number; // fees earned over the position's life (ETH)
  pnlEth: number | null;
  pnlPct: number | null;
  forfeited: string | null; // symbol of a honeypot token forfeited to salvage the ETH side
}

export async function closeV4Position(tokenId: string): Promise<V4CloseResult> {
  if (atomicOnlyEnabled()) refuseLegacyLifecycle(`v4 close #${tokenId}; position left untouched because atomic V4 unsupported`);
  const w = wallet();
  // Read the pool key + currencies directly (no SDK Pool). The SDK's removeCallParameters
  // throws "Invariant failed: PRICE_BOUNDS" on extreme-price pools (WOLVES/USDG) when it
  // applies slippage, and returns a null `value` for non-native pairs → "invalid BigNumberish".
  const posm = new ethers.Contract(C.v4PositionManager!, V4_POSM_ABI, provider);
  const [pk] = await posm.getPoolAndPositionInfo!(tokenId);
  const c0 = pk.currency0 as string;
  const c1 = pk.currency1 as string;
  const fee = Number(pk.fee);
  const [m0, m1] = await Promise.all([
    c0.toLowerCase() === NATIVE ? Promise.resolve({ symbol: "ETH", decimals: 18 }) : tokenMeta(c0).catch(() => ({ symbol: "?", decimals: 18 })),
    c1.toLowerCase() === NATIVE ? Promise.resolve({ symbol: "ETH", decimals: 18 }) : tokenMeta(c1).catch(() => ({ symbol: "?", decimals: 18 })),
  ]);

  // Snapshot the position's USD value + fees + pair BEFORE closing (needs the position live) so
  // we can write an accurate ledger entry. valueUsd at close = realized value; deposit is the
  // recorded ETH funding → PnL in ETH is exact (deposit was ETH-denominated, no historical price).
  let pre: { valueUsd: number; feeUsd: number; pair: string; sym: string } | null = null;
  try {
    const rows = await listV4Positions();
    const r = rows.find((x) => x.tokenId === String(tokenId));
    if (r) pre = { valueUsd: r.valueUsd, feeUsd: r.feeUsd, pair: r.pair, sym: r.sym };
  } catch {
    /* best-effort — ledger entry just won't have USD value */
  }

  // Reconstruct expected principal before simulation and enforce non-zero minima. Accrued fees
  // only increase receipts, so principal is the conservative expected baseline. If either side
  // has no principal (or cannot be reconstructed), fail closed rather than emitting zero minima.
  const loaded = await loadPosition(tokenId);
  const expected0 = BigInt(loaded.position.amount0.quotient.toString());
  const expected1 = BigInt(loaded.position.amount1.quotient.toString());
  const closeSlippage = cfg.lp.slippagePct || 5;
  if (closeSlippage > 20) throw new Error("close slippage policy exceeds hard 20% safety cap");

  // Manual full close: BURN_POSITION removes all liquidity/fees and TAKE_PAIR sweeps both.
  // Select the tightest executable minimum by eth_call, with a hard 20% relaxation ceiling.
  // This handles tick movement near a range boundary without ever falling back to zero minima.
  const coder = ethers.AbiCoder.defaultAbiCoder();
  const iface = new ethers.Interface(["function modifyLiquidities(bytes,uint256) payable"]);
  const dl = Math.floor(Date.now() / 1000 + 600);
  const takeParams = coder.encode(["address", "address", "address"], [c0, c1, w.address]);
  const levels = [...new Set([closeSlippage, 10, 20].filter((x) => x >= closeSlippage && x <= 20))];
  let calldata: string | null = null;
  let selectedSlippage: number | null = null;
  let lastSimulationError: unknown = null;
  for (const slip of levels) {
    const amount0Min = closeMinimum(expected0, slip);
    const amount1Min = closeMinimum(expected1, slip);
    if (amount0Min === 0n && amount1Min === 0n) throw new Error("cannot safely close: both expected principal amounts are zero");
    const burnParams = coder.encode(["uint256", "uint128", "uint128", "bytes"], [tokenId, amount0Min, amount1Min, "0x"]);
    const unlockData = coder.encode(["bytes", "bytes[]"], ["0x0311", [burnParams, takeParams]]);
    const candidate = iface.encodeFunctionData("modifyLiquidities", [unlockData, dl]);
    try {
      await provider.call({ to: C.v4PositionManager!, data: candidate, value: 0n, from: w.address });
      calldata = candidate;
      selectedSlippage = slip;
      break;
    } catch (e) {
      lastSimulationError = e;
    }
  }
  if (!calldata) {
    const msg = (lastSimulationError as any)?.shortMessage || (lastSimulationError as Error | null)?.message || "unknown revert";
    throw new Error(`close simulation failed within 20% receipt floor: ${String(msg).slice(0, 160)}`);
  }
  if (selectedSlippage !== closeSlippage) log.warn(`close #${tokenId}: receipt floor relaxed to ${selectedSlippage}% after tighter simulation reverted`);

  const [bal0Before, bal1Before] = await Promise.all([balOf(c0, m0.decimals), balOf(c1, m1.decimals)]);
  const txHash = await simulateAndSend(calldata, "0", "close");
  const forfeited: string | null = null;
  const [bal0After, bal1After] = await Promise.all([balOf(c0, m0.decimals), balOf(c1, m1.decimals)]);

  const dep = loadV4Deposit(String(tokenId));
  const depEth = dep?.depositWei ? Number(ethers.formatEther(dep.depositWei)) : null;
  const pair = pre?.pair ?? `${m0.symbol}/${m1.symbol}`;
  const px = await ethUsd().catch(() => 0);
  const outEth = pre && px ? pre.valueUsd / px : 0;
  const feeEth = pre && px ? pre.feeUsd / px : 0;

  // BASIS for PnL. For ETH pairs the deposit was ETH-funded → realized PnL vs that ETH is exact.
  // For USDG (non-ETH) pairs, funding the position swapped ETH→USDG+token, so the recorded ETH
  // deposit is contaminated by the token's own price move. We instead measure LP-vs-HODL: value
  // the DEPOSITED token amounts at the CLOSE price, so a token that merely dropped in price isn't
  // counted as an LP loss — only fees + impermanent loss are. Keeps forward-close consistent with
  // the historical reconstruction (backfill.ts), which is why WOLVES/USDG shows fee-driven profit.
  let basisEth = depEth;
  const isUsdgPair = STABLES.has(c0.toLowerCase()) || STABLES.has(c1.toLowerCase());
  if (isUsdgPair && dep?.dep0 && dep?.dep1 && px) {
    try {
      const sv = new ethers.Contract(C.v4StateView!, STATEVIEW_ABI, provider);
      const poolId = ethers.keccak256(
        ethers.AbiCoder.defaultAbiCoder().encode(
          ["address", "address", "uint24", "int24", "address"],
          [c0, c1, fee, Number(pk.tickSpacing), pk.hooks],
        ),
      );
      const s0 = await sv.getSlot0!(poolId);
      const cur0 = sdkCurrency(c0, m0.decimals, m0.symbol);
      const cur1 = sdkCurrency(c1, m1.decimals, m1.symbol);
      const pool = new Pool(cur0, cur1, fee, Number(pk.tickSpacing), pk.hooks, s0.sqrtPriceX96.toString(), "0", Number(s0.tick));
      const valUsd = (addr: string, dec: number, sym: string, raw: bigint, cur: any, otherAddr: string, otherSym: string): number => {
        if (raw <= 0n) return 0;
        const a = addr.toLowerCase();
        const ui = Number(ethers.formatUnits(raw, dec));
        if (a === NATIVE || a === WETH_L) return ui * px;
        if (STABLES.has(a) || /usd/i.test(sym)) return ui;
        try {
          const inOther = Number(pool.priceOf(cur).quote(CurrencyAmount.fromRawAmount(cur, raw.toString())).toExact());
          const oa = otherAddr.toLowerCase();
          if (oa === NATIVE || oa === WETH_L) return inOther * px;
          if (STABLES.has(oa) || /usd/i.test(otherSym)) return inOther;
        } catch {
          /* price out of range → skip */
        }
        return 0;
      };
      const hodlUsd =
        valUsd(c0, m0.decimals, m0.symbol, BigInt(dep.dep0), cur0, c1, m1.symbol) +
        valUsd(c1, m1.decimals, m1.symbol, BigInt(dep.dep1), cur1, c0, m0.symbol);
      if (hodlUsd > 0) basisEth = hodlUsd / px;
    } catch (e: any) {
      log.warn(`LP-vs-HODL basis failed #${tokenId}: ${e?.message ?? e} — falling back to ETH-funded basis`);
    }
  }
  const pnlEth = basisEth != null && basisEth > 0 && pre ? outEth - basisEth : null;
  const pnlPct = pnlEth != null && basisEth ? (pnlEth / basisEth) * 100 : null;

  // record to the unified ledger (so /ledger shows v4 modal/PnL + counts it in stats)
  try {
    appendLedger({
      tokenId: String(tokenId),
      sym: pre?.sym ?? m0.symbol,
      version: "v4",
      pair,
      quote: isUsdgPair ? "usd" : "eth",
      mode: dep?.mode === "inrange" ? "inrange" : "single",
      openedAt: dep?.ts ?? null,
      closedAt: Date.now(),
      heldMs: dep?.ts ? Date.now() - dep.ts : null,
      depEth: basisEth ?? 0,
      outEth,
      feeEth,
      pnlEth,
      pnlPct,
      pnlUsd: pnlEth != null && px ? pnlEth * px : null,
      ethUsdAtClose: px || null,
      tokenKept: 0,
      tokenRug: 0,
      unsoldEth: 0,
      source: "bot",
    });
  } catch (e) {
    log.warn(`gagal tulis ledger v4 #${tokenId}: ${(e as Error).message.slice(0, 80)}`);
  }

  dropDeposit(tokenId);
  log.info(`close v4 #${tokenId} ${m0.symbol}/${m1.symbol}`);
  return {
    txHash,
    fee,
    recv0: Math.max(0, bal0After - bal0Before),
    sym0: m0.symbol,
    recv1: Math.max(0, bal1After - bal1Before),
    sym1: m1.symbol,
    depEth: basisEth,
    pair,
    outEth,
    feeEth,
    pnlEth,
    pnlPct,
    forfeited,
  };
}

export interface V4CollectResult {
  txHash: string;
  fee0: number;
  sym0: string;
  fee1: number;
  sym1: string;
}

/**
 * Collect accrued fees WITHOUT removing liquidity. The SDK's removeCallParameters rejects
 * 0% liquidity, so we manually encode the standard v4 collect: DECREASE_LIQUIDITY(0) which
 * settles fees into owed balances, then TAKE_PAIR to sweep them to the wallet.
 */
export async function collectV4Fees(tokenId: string): Promise<V4CollectResult> {
  if (atomicOnlyEnabled()) refuseLegacyLifecycle(`v4 collect #${tokenId}; position left untouched because atomic V4 unsupported`);
  const w = wallet();
  const { c0, c1, m0, m1 } = await loadPosition(tokenId);
  const coder = ethers.AbiCoder.defaultAbiCoder();
  const actions = "0x0111"; // DECREASE_LIQUIDITY(0x01), TAKE_PAIR(0x11)
  const decParams = coder.encode(["uint256", "uint256", "uint128", "uint128", "bytes"], [tokenId, 0, 0, 0, "0x"]);
  const takeParams = coder.encode(["address", "address", "address"], [c0, c1, w.address]);
  const unlockData = coder.encode(["bytes", "bytes[]"], [actions, [decParams, takeParams]]);
  const iface = new ethers.Interface(["function modifyLiquidities(bytes,uint256) payable"]);
  const calldata = iface.encodeFunctionData("modifyLiquidities", [unlockData, Math.floor(Date.now() / 1000 + 600)]);

  const [b0, b1] = await Promise.all([balOf(c0, m0.decimals), balOf(c1, m1.decimals)]);
  const txHash = await simulateAndSend(calldata, "0", "collect");
  const [a0, a1] = await Promise.all([balOf(c0, m0.decimals), balOf(c1, m1.decimals)]);
  log.info(`collect v4 #${tokenId} ${m0.symbol}/${m1.symbol}`);
  return { txHash, fee0: Math.max(0, a0 - b0), sym0: m0.symbol, fee1: Math.max(0, a1 - b1), sym1: m1.symbol };
}

async function balOf(addr: string, dec: number): Promise<number> {
  const w = wallet();
  if (addr.toLowerCase() === NATIVE) return Number(ethers.formatEther(await provider.getBalance(w.address)));
  const erc = new ethers.Contract(addr, ["function balanceOf(address) view returns (uint256)"], provider);
  return Number(ethers.formatUnits(await erc.balanceOf!(w.address).catch(() => 0n), dec));
}

function dropDeposit(tokenId: string): void {
  try {
    const d = readJson<Record<string, unknown>>(dataPath("v4-positions.json"), {});
    delete d[String(tokenId)];
    writeJson(dataPath("v4-positions.json"), d);
  } catch {
    /* */
  }
}
