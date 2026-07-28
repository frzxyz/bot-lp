/**
 * v4 LP open — single-sided native ETH (rug-safe, no Permit2, no token needed).
 *
 * Verified by staticCall simulation on chain 4663: single-sided ETH means a range ABOVE
 * the current tick (ETH = currency0, not yet converted to token). The @uniswap/v4-sdk
 * generates the modifyLiquidities calldata + native value; we simulate every mint with
 * eth_call BEFORE broadcasting, so a mint that would revert never costs gas.
 *
 * In-range (both-sided) v4 needs the token via Permit2 + a pre-swap — deferred.
 */
import { ethers } from "ethers";
import sdkCore from "@uniswap/sdk-core";
import v4sdk from "@uniswap/v4-sdk";
import { C, cfg, env } from "../../config.js";
import { wallet, provider, overrides } from "../client.js";
import { tokenMeta } from "../tokens.js";
import { discoverV4Pools, pickV4Pool, USDG, type V4Pool } from "./discover.js";
import { swapEthToTokenV4, quoteV4 } from "./swap.js";
import { kyberSwap, kyberEnabled, KYBER_NATIVE } from "../kyber.js";
import { NATIVE } from "./poolkey.js";
import { mapLimit } from "../blockscout.js";
import { WETH_ABI } from "../abis.js";
import { dataPath, readJson, writeJson } from "../../util/files.js";
import { logger } from "../../util/log.js";
import { boundedBudgetSplit, operationAmount, singleSidedUsdgRange } from "./safety.js";
import { atomicOnlyEnabled, refuseLegacyLifecycle } from "../atomic.js";

const { Ether, Token, Percent, CurrencyAmount } = sdkCore as any;
const { Pool, Position, V4PositionManager } = v4sdk as any;
const log = logger("v4mint");
const POS_FILE = dataPath("v4-positions.json");
const PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"; // canonical, all chains

export interface V4OpenResult {
  tokenId: string | null;
  txHash: string;
  fee: number;
  tickLower: number;
  tickUpper: number;
  depositEth: string;
  poolId: string;
}

type V4Dep = { depositWei: string; ts: number; poolId: string; fee: number; tickLower: number; tickUpper: number; mode: string; dep0?: string; dep1?: string };

export function saveV4Deposit(tokenId: string, rec: V4Dep): void {
  const d = readJson<Record<string, V4Dep>>(POS_FILE, {});
  d[tokenId] = rec;
  writeJson(POS_FILE, d);
}
export function loadV4Deposit(tokenId: string): V4Dep | null {
  return readJson<Record<string, V4Dep>>(POS_FILE, {})[tokenId] ?? null;
}

const NATIVE_GAS_BUFFER = ethers.parseEther("0.0003"); // keep some native for tx gas

/**
 * v4 native-ETH mints settle the ETH side as NATIVE ETH (not WETH). If the wallet is mostly
 * WETH (common — v3 wraps, closes unwrap-partially), the mint's native `value` exceeds the
 * native balance and the sim reverts with empty data ("missing revert data"). Unwrap the
 * shortfall WETH → ETH first so native covers the deposit + gas.
 */
async function ensureNativeEth(needWei: bigint): Promise<void> {
  if (atomicOnlyEnabled()) refuseLegacyLifecycle("v4 fund-moving operation; atomic V4 unsupported");
  const w = wallet();
  const bal = await provider.getBalance(w.address);
  if (bal >= needWei) return;
  const short = needWei - bal;
  const weth = new ethers.Contract(C.weth, WETH_ABI, w);
  const wbal: bigint = await weth.balanceOf!(w.address).catch(() => 0n);
  if (wbal < short) {
    throw new Error(
      `ETH native kurang buat v4 mint: butuh ${ethers.formatEther(needWei)}Ξ, ada ${ethers.formatEther(bal)}Ξ native + ${ethers.formatEther(wbal)} WETH`,
    );
  }
  log.info(`unwrap ${ethers.formatEther(short)} WETH → ETH native (v4 butuh native)`);
  await (await weth.withdraw!(short, await overrides())).wait();
}

function buildSdkPool(token: string, decimals: number, symbol: string, pool: V4Pool) {
  const eth = Ether.onChain(cfg.chainId);
  const tok = new Token(cfg.chainId, ethers.getAddress(token), decimals, symbol);
  return new Pool(
    eth,
    tok,
    pool.fee,
    pool.tickSpacing,
    pool.poolKey.hooks,
    pool.sqrtPriceX96.toString(),
    pool.liquidity.toString(),
    pool.tick,
  );
}

/**
 * Open a single-sided native-ETH v4 position at the highest-fee pool with liquidity
 * (or a specific fee tier). Simulates before broadcasting.
 */
export async function openV4SingleSide(
  token: string,
  amountEthStr: string,
  opts: { fee?: number; widthSpacings?: number } = {},
): Promise<V4OpenResult> {
  if (atomicOnlyEnabled()) refuseLegacyLifecycle("v4 fund-moving operation; atomic V4 unsupported");
  const w = wallet();
  const pools = await discoverV4Pools(token);
  const pool = opts.fee ? pools.find((p) => p.fee === opts.fee) ?? null : pickV4Pool(pools);
  if (!pool) throw new Error("tidak ada pool v4/ETH dengan likuiditas");

  const meta = await tokenMeta(token);
  const sdkPool = buildSdkPool(token, meta.decimals, meta.symbol, pool);

  // single-sided ETH → range ABOVE current tick (ETH not yet sold into token)
  const sp = pool.tickSpacing;
  const width = Math.max(1, opts.widthSpacings ?? Math.round((cfg.lp.widthPct / 100) / (Math.pow(1.0001, sp) - 1)));
  const tickLower = Math.ceil(pool.tick / sp) * sp + sp;
  const tickUpper = tickLower + width * sp;
  const amountWei = ethers.parseEther(amountEthStr);
  // single-side parks NATIVE ETH — unwrap WETH if native is short
  await ensureNativeEth(amountWei + NATIVE_GAS_BUFFER);

  const position = Position.fromAmount0({
    pool: sdkPool,
    tickLower,
    tickUpper,
    amount0: amountWei.toString(),
    useFullPrecision: true,
  });
  if (position.liquidity.toString() === "0") throw new Error("liquidity 0 — deposit terlalu kecil buat range ini");

  const { calldata, value } = V4PositionManager.addCallParameters(position, {
    recipient: w.address,
    slippageTolerance: new Percent(Math.round((cfg.lp.slippagePct || 5)), 100),
    deadline: Math.floor(Date.now() / 1000 + 600).toString(),
    useNative: Ether.onChain(cfg.chainId),
  });

  // SIMULATE before spending gas
  try {
    await provider.call({ to: C.v4PositionManager!, data: calldata, value, from: w.address });
  } catch (e) {
    throw new Error(`simulasi mint v4 revert: ${((e as any).shortMessage || (e as Error).message || "").slice(0, 140)}`);
  }

  const tx = await w.sendTransaction({ to: C.v4PositionManager!, data: calldata, value: BigInt(value), ...(await overrides()) });
  const rc = await tx.wait();
  const tokenId = tokenIdFromReceipt(rc!);
  if (tokenId) {
    saveV4Deposit(tokenId, {
      depositWei: amountWei.toString(),
      ts: Date.now(),
      poolId: pool.poolId,
      fee: pool.fee,
      tickLower,
      tickUpper,
      mode: "single",
    });
  }
  log.info(`open v4 #${tokenId} ${meta.symbol} fee ${pool.fee / 10000}% ${amountEthStr}Ξ`);
  return { tokenId, txHash: tx.hash, fee: pool.fee, tickLower, tickUpper, depositEth: amountEthStr, poolId: pool.poolId };
}

/**
 * Open an IN-RANGE native-ETH v4 position (farming: earns fees immediately). Swaps part of
 * the ETH → token via the UniversalRouter, approves the token through Permit2, then mints a
 * range straddling the current price. Simulates before broadcasting.
 */
export async function openV4InRange(
  token: string,
  amountEthStr: string,
  opts: { fee?: number; widthSpacings?: number } = {},
): Promise<V4OpenResult & { swapHash?: string; swappedPct: number }> {
  if (atomicOnlyEnabled()) refuseLegacyLifecycle("v4 fund-moving operation; atomic V4 unsupported");
  const w = wallet();
  const pools = await discoverV4Pools(token);
  const pool = opts.fee ? pools.find((p) => p.fee === opts.fee) ?? null : pickV4Pool(pools);
  if (!pool) throw new Error("tidak ada pool v4/ETH dengan likuiditas");
  const meta = await tokenMeta(token);
  const sp = pool.tickSpacing;

  // symmetric range straddling current tick
  const halfSpacings = Math.max(1, Math.round((opts.widthSpacings ?? 8) / 2));
  const anchor = Math.floor(pool.tick / sp) * sp;
  const tickLower = anchor - halfSpacings * sp;
  const tickUpper = anchor + halfSpacings * sp;

  const total = ethers.parseEther(amountEthStr);
  // v4 needs NATIVE ETH for both the swap and the mint value — unwrap WETH if native is short
  await ensureNativeEth(total + NATIVE_GAS_BUFFER);
  const sdkPool = buildSdkPool(token, meta.decimals, meta.symbol, { ...pool });
  const isC0Native = pool.poolKey.currency0.toLowerCase().startsWith("0x000000000000000000");
  const tokCur = isC0Native ? sdkPool.currency1 : sdkPool.currency0;
  const priceTokenInEth = (raw: bigint): bigint => {
    if (raw <= 0n) return 0n;
    try {
      return BigInt(sdkPool.priceOf(tokCur).quote(CurrencyAmount.fromRawAmount(tokCur, raw.toString())).quotient.toString());
    } catch {
      return 0n;
    }
  };

  const erc = new ethers.Contract(
    token,
    [
      "function allowance(address,address) view returns (uint256)",
      "function approve(address,uint256) returns (bool)",
      "function balanceOf(address) view returns (uint256)",
    ],
    w,
  );

  // REUSE token we already hold (e.g. bought on a prior failed attempt) — don't re-buy.
  const tokenHave: bigint = await erc.balanceOf!(w.address).catch(() => 0n);
  const haveEthValue = priceTokenInEth(tokenHave);

  // token value (in ETH) this range wants; swap ONLY the shortfall (0 if we already hold enough)
  const frac = Math.min(0.9, Math.max(0.05, swapFractionV4(pool.tick, tickLower, tickUpper) * 0.98));
  const targetTokenEth = (total * BigInt(Math.round(frac * 1e6))) / 1_000_000n;
  let ethToSwap = targetTokenEth > haveEthValue ? targetTokenEth - haveEthValue : 0n;
  const maxSwap = (total * 9n) / 10n;
  if (ethToSwap > maxSwap) ethToSwap = maxSwap;

  // 1) buy the token shortfall with the BEST execution. Route via the KyberSwap aggregator
  //    (auto multi-hop across every DEX/fee-tier/hook → lowest fee + price impact). Buying on
  //    the high-fee pool you're farming would bleed fee + slippage = instant loss. Falls back
  //    to a direct v4 swap on the deepest single pool if the aggregator can't route.
  let swapHash: string | undefined;
  let swappedPct = 0;
  if (ethToSwap >= ethers.parseEther("0.00002")) {
    let out = 0n;
    if (kyberEnabled()) {
      const k = await kyberSwap(KYBER_NATIVE, ethers.getAddress(token), ethToSwap).catch((e) => {
        log.warn(`kyber gagal (${(e as Error).message.slice(0, 80)}) → fallback v4 direct`);
        return null;
      });
      if (k && k.amountOut > 0n) {
        swapHash = k.tx;
        out = k.amountOut;
        log.info(`beli ${meta.symbol} via KyberSwap (best route) → ${out}`);
      }
    }
    if (out <= 0n) {
      const via = (await bestSwapPool(pools, ethToSwap)) ?? pool;
      const sw = await swapEthToTokenV4(via.poolKey, ethToSwap);
      if (sw.amountOut <= 0n) throw new Error("swap ETH→token gagal (pool kering?)");
      swapHash = sw.tx;
    }
    swappedPct = Math.round((Number(ethToSwap) / Number(total)) * 100);
  } else {
    ethToSwap = 0n; // enough token on hand — LP straight from balance
  }

  // 2) actual token balance now (existing + any swapped)
  const tokenBal: bigint = await erc.balanceOf!(w.address).catch(() => 0n);
  if (tokenBal <= 0n) throw new Error("token balance 0 — nggak ada yang bisa di-LP");

  // 3) approve token via Permit2 (ERC20 → Permit2, Permit2 → PositionManager)
  if ((await erc.allowance!(w.address, PERMIT2)) < tokenBal) {
    await (await erc.approve!(PERMIT2, ethers.MaxUint256, await overrides())).wait();
  }
  const permit2 = new ethers.Contract(PERMIT2, ["function approve(address token,address spender,uint160 amount,uint48 expiration)"], w);
  const exp = Math.floor(Date.now() / 1000) + 30 * 86400;
  await (await permit2.approve!(token, C.v4PositionManager!, (1n << 160n) - 1n, exp, await overrides())).wait();

  // 4) build both-sided position from ACTUAL balances, scaled so the slippage-max settle stays
  //    WITHIN what we hold. The old bug: position built from the swap's exact output, then
  //    addCallParameters' slippage tried to pull MORE token than balance → Permit2 reverted
  //    with empty data ("missing revert data").
  const ethLeft = total - ethToSwap;
  const slip = new Percent(Math.round(cfg.lp.slippagePct || 5), 100);
  const mkPosition = (e: bigint, t: bigint) =>
    Position.fromAmounts({
      pool: sdkPool,
      tickLower,
      tickUpper,
      amount0: (isC0Native ? e : t).toString(),
      amount1: (isC0Native ? t : e).toString(),
      useFullPrecision: true,
    });
  let position = mkPosition(ethLeft, tokenBal);
  try {
    const maxAmts = position.mintAmountsWithSlippage(slip);
    const have0 = isC0Native ? ethLeft : tokenBal;
    const have1 = isC0Native ? tokenBal : ethLeft;
    const m0 = BigInt(maxAmts.amount0.toString());
    const m1 = BigInt(maxAmts.amount1.toString());
    let numer = 1_000_000n;
    if (m0 > have0 && m0 > 0n) { const r = (have0 * 1_000_000n) / m0; if (r < numer) numer = r; }
    if (m1 > have1 && m1 > 0n) { const r = (have1 * 1_000_000n) / m1; if (r < numer) numer = r; }
    if (numer < 1_000_000n) {
      const scale = (x: bigint) => (((x * numer) / 1_000_000n) * 999n) / 1000n; // +0.1% safety
      position = mkPosition(scale(ethLeft), scale(tokenBal));
    }
  } catch {
    /* SDK without mintAmountsWithSlippage — fall through with the raw position */
  }
  if (position.liquidity.toString() === "0") throw new Error("liquidity 0 — deposit terlalu kecil");

  const { calldata, value } = V4PositionManager.addCallParameters(position, {
    recipient: w.address,
    slippageTolerance: slip,
    deadline: Math.floor(Date.now() / 1000 + 600).toString(),
    useNative: Ether.onChain(cfg.chainId),
  });

  try {
    await provider.call({ to: C.v4PositionManager!, data: calldata, value, from: w.address });
  } catch (e) {
    throw new Error(`simulasi mint v4 in-range revert: ${((e as any).shortMessage || (e as Error).message || "").slice(0, 140)}`);
  }
  const tx = await w.sendTransaction({ to: C.v4PositionManager!, data: calldata, value: BigInt(value), ...(await overrides()) });
  const rc = await tx.wait();
  const tokenId = tokenIdFromReceipt(rc!);
  // deposit basis = the position's actual value at mint (native side + token side valued in ETH),
  // so reused inventory is counted honestly in PnL
  const a0 = BigInt(position.amount0.quotient.toString());
  const a1 = BigInt(position.amount1.quotient.toString());
  const depWei = (isC0Native ? a0 : a1) + priceTokenInEth(isC0Native ? a1 : a0);
  if (tokenId) {
    saveV4Deposit(tokenId, { depositWei: (depWei > 0n ? depWei : total).toString(), ts: Date.now(), poolId: pool.poolId, fee: pool.fee, tickLower, tickUpper, mode: "inrange" });
  }
  log.info(`open v4 IN-RANGE #${tokenId} ${meta.symbol} fee ${pool.fee / 10000}% swap ${swappedPct}%${ethToSwap === 0n ? " (reuse balance)" : ""}`);
  return {
    tokenId,
    txHash: tx.hash,
    swapHash,
    swappedPct,
    fee: pool.fee,
    tickLower,
    tickUpper,
    depositEth: amountEthStr,
    poolId: pool.poolId,
  };
}

/**
 * For a DUAL-SIDE (in-range) v4 position, the ETH amount that exactly balances the token the
 * wallet already holds — so both sides fill with no swap and minimal leftover. Returns 0 if it
 * can't be computed (pool degenerate / no token). Used to suggest the "type ETH" amount.
 */
export function balancedEthForHeldToken(token: string, meta: { decimals: number; symbol: string }, pool: V4Pool, tokenRaw: bigint): number {
  if (tokenRaw <= 0n) return 0;
  try {
    const eth = Ether.onChain(cfg.chainId);
    const tok = new Token(cfg.chainId, ethers.getAddress(token), meta.decimals, meta.symbol);
    const sdkPool = new Pool(eth, tok, pool.fee, pool.tickSpacing, pool.poolKey.hooks, pool.sqrtPriceX96.toString(), pool.liquidity.toString(), pool.tick);
    const sp = pool.tickSpacing;
    const half = Math.max(1, Math.round(8 / 2));
    const anchor = Math.floor(pool.tick / sp) * sp;
    const tickLower = anchor - half * sp;
    const tickUpper = anchor + half * sp;
    const frac = Math.min(0.9, Math.max(0.05, swapFractionV4(pool.tick, tickLower, tickUpper) * 0.98));
    if (frac <= 0 || frac >= 1) return 0;
    const tokEthWei = BigInt(sdkPool.priceOf(sdkPool.currency1).quote(CurrencyAmount.fromRawAmount(sdkPool.currency1, tokenRaw.toString())).quotient.toString());
    const tokEth = Number(ethers.formatEther(tokEthWei));
    return tokEth * ((1 - frac) / frac); // the ETH side that pairs with the held token
  } catch {
    return 0;
  }
}

/** Approve an ERC20 for the v4 PositionManager via Permit2 (ERC20→Permit2, Permit2→POSM). */
async function approveViaPermit2(tokenAddr: string): Promise<void> {
  if (atomicOnlyEnabled()) refuseLegacyLifecycle("v4 fund-moving operation; atomic V4 unsupported");
  const w = wallet();
  const erc = new ethers.Contract(tokenAddr, ["function allowance(address,address) view returns (uint256)", "function approve(address,uint256) returns (bool)"], w);
  if ((await erc.allowance!(w.address, PERMIT2)) < (1n << 200n)) {
    await (await erc.approve!(PERMIT2, ethers.MaxUint256, await overrides())).wait();
  }
  const permit2 = new ethers.Contract(PERMIT2, ["function approve(address token,address spender,uint160 amount,uint48 expiration)"], w);
  const exp = Math.floor(Date.now() / 1000) + 30 * 86400;
  await (await permit2.approve!(tokenAddr, C.v4PositionManager!, (1n << 160n) - 1n, exp, await overrides())).wait();
}

/**
 * Open an in-range v4 position on a token/USDG pool (no native ETH leg). Funds BOTH sides from
 * ETH via the KyberSwap aggregator (ETH→USDG and ETH→token, auto multi-hop), approves both via
 * Permit2, then mints both-sided. amountEthStr = ETH budget deployed.
 */
export async function openV4UsdgInRange(
  pool: V4Pool,
  amountEthStr: string,
  opts: { preexistingUsdgCapRaw?: bigint } = {},
): Promise<V4OpenResult & { swapHash?: string; swappedPct: number }> {
  if (atomicOnlyEnabled()) refuseLegacyLifecycle("v4 fund-moving operation; atomic V4 unsupported");
  const w = wallet();
  const total = ethers.parseEther(amountEthStr);
  await ensureNativeEth(total + NATIVE_GAS_BUFFER);

  const c0 = pool.poolKey.currency0;
  const c1 = pool.poolKey.currency1;
  const [m0, m1] = await Promise.all([tokenMeta(c0), tokenMeta(c1)]);
  const cur0 = new Token(cfg.chainId, ethers.getAddress(c0), m0.decimals, m0.symbol);
  const cur1 = new Token(cfg.chainId, ethers.getAddress(c1), m1.decimals, m1.symbol);
  const sdkPool = new Pool(cur0, cur1, pool.fee, pool.tickSpacing, pool.poolKey.hooks, pool.sqrtPriceX96.toString(), pool.liquidity.toString(), pool.tick);

  const sp = pool.tickSpacing;
  const half = Math.max(1, Math.round(8 / 2));
  const anchor = Math.floor(pool.tick / sp) * sp;
  const tickLower = anchor - half * sp;
  const tickUpper = anchor + half * sp;

  // fraction of value in currency1 (currency0 terms) for this straddle → split the ETH budget
  const fracC1 = Math.min(0.95, Math.max(0.05, swapFractionV4(pool.tick, tickLower, tickUpper)));
  const ethForC1 = (total * BigInt(Math.round(fracC1 * 1e6))) / 1_000_000n;
  const ethForC0 = total - ethForC1;

  const bal = async (a: string): Promise<bigint> =>
    new ethers.Contract(a, ["function balanceOf(address) view returns (uint256)"], provider).balanceOf!(w.address).catch(() => 0n);

  // Snapshot before ANY acquisition. Mint inputs are subsequently limited to operation
  // deltas; unrelated wallet inventory is never silently swept into the position. A caller
  // may explicitly opt in a bounded amount of preexisting USDG only.
  let swapHash: string | undefined;
  const acquire = async (addr: string, ethAmt: bigint) => {
    if (ethAmt < ethers.parseEther("0.00002")) return;
    const k = await kyberSwap(KYBER_NATIVE, ethers.getAddress(addr), ethAmt);
    if (!k || k.amountOut <= 0n) throw new Error(`gagal beli ${addr.toLowerCase() === USDG.toLowerCase() ? "USDG" : "token"} via Kyber`);
    swapHash = k.tx;
  };
  const [before0, before1] = await Promise.all([bal(c0), bal(c1)]);
  await acquire(c0, ethForC0);
  await acquire(c1, ethForC1);

  const [after0, after1] = await Promise.all([bal(c0), bal(c1)]);
  const usdgCap = opts.preexistingUsdgCapRaw ?? 0n;
  const bal0 = operationAmount(after0, before0, c0.toLowerCase() === USDG.toLowerCase() ? usdgCap : 0n);
  const bal1 = operationAmount(after1, before1, c1.toLowerCase() === USDG.toLowerCase() ? usdgCap : 0n);
  if (bal0 <= 0n || bal1 <= 0n) throw new Error(`operation delta ${m0.symbol}/${m1.symbol} 0 setelah swap — refusing wallet-balance fallback`);

  await approveViaPermit2(c0);
  await approveViaPermit2(c1);

  const slip = new Percent(Math.round(cfg.lp.slippagePct || 5), 100);
  const mk = (a0: bigint, a1: bigint) => Position.fromAmounts({ pool: sdkPool, tickLower, tickUpper, amount0: a0.toString(), amount1: a1.toString(), useFullPrecision: true });
  let position = mk(bal0, bal1);
  try {
    const mx = position.mintAmountsWithSlippage(slip);
    const m0max = BigInt(mx.amount0.toString());
    const m1max = BigInt(mx.amount1.toString());
    let numer = 1_000_000n;
    if (m0max > bal0 && m0max > 0n) { const r = (bal0 * 1_000_000n) / m0max; if (r < numer) numer = r; }
    if (m1max > bal1 && m1max > 0n) { const r = (bal1 * 1_000_000n) / m1max; if (r < numer) numer = r; }
    if (numer < 1_000_000n) { const s = (x: bigint) => (((x * numer) / 1_000_000n) * 999n) / 1000n; position = mk(s(bal0), s(bal1)); }
  } catch {
    /* SDK lacks mintAmountsWithSlippage */
  }
  if (position.liquidity.toString() === "0") throw new Error("liquidity 0 — deposit terlalu kecil");

  const { calldata, value } = V4PositionManager.addCallParameters(position, {
    recipient: w.address,
    slippageTolerance: slip,
    deadline: Math.floor(Date.now() / 1000 + 600).toString(),
    // NO useNative — both sides are ERC20 (token + USDG), settled via Permit2
  });
  try {
    await provider.call({ to: C.v4PositionManager!, data: calldata, value, from: w.address });
  } catch (e) {
    throw new Error(`simulasi mint v4 USDG revert: ${((e as any).shortMessage || (e as Error).message || "").slice(0, 140)}`);
  }
  const tx = await w.sendTransaction({ to: C.v4PositionManager!, data: calldata, value: BigInt(value), ...(await overrides()) });
  const rc = await tx.wait();
  const tokenId = tokenIdFromReceipt(rc!);
  if (tokenId) {
    // record the DEPOSITED token amounts so close can measure LP-vs-HODL (fees+IL), not the
    // token's directional price move which isn't the LP's fault.
    saveV4Deposit(tokenId, {
      depositWei: total.toString(),
      ts: Date.now(),
      poolId: pool.poolId,
      fee: pool.fee,
      tickLower,
      tickUpper,
      mode: "inrange",
      dep0: position.amount0.quotient.toString(),
      dep1: position.amount1.quotient.toString(),
    });
  }
  log.info(`open v4 USDG in-range #${tokenId} ${m0.symbol}/${m1.symbol} fee ${pool.fee / 10000}% ${amountEthStr}Ξ`);
  return { tokenId, txHash: tx.hash, swapHash, swappedPct: 100, fee: pool.fee, tickLower, tickUpper, depositEth: amountEthStr, poolId: pool.poolId };
}

/** Mint with only a bounded amount of existing USDG; no acquisition and no router call. */
export async function openV4UsdgSingle(pool: V4Pool, budget: bigint): Promise<V4OpenResult> {
  const w = wallet(), c0 = pool.poolKey.currency0, c1 = pool.poolKey.currency1;
  const usdg = USDG.toLowerCase(), usdgIsCurrency0 = c0.toLowerCase() === usdg;
  if (budget <= 0n || budget >= (1n << 160n)) throw new Error("USDG budget is outside Permit2 uint160 bounds");
  if (pool.quote !== "usd" || (!usdgIsCurrency0 && c1.toLowerCase() !== usdg)) throw new Error("pool is not token/USDG");
  if (pool.poolKey.hooks.toLowerCase() !== ethers.ZeroAddress || pool.fee >= 0x800000) throw new Error("pool must be static-fee and hookless");
  const [m0, m1] = await Promise.all([tokenMeta(c0), tokenMeta(c1)]);
  const sdkPool = new Pool(
    new Token(cfg.chainId, ethers.getAddress(c0), m0.decimals, m0.symbol),
    new Token(cfg.chainId, ethers.getAddress(c1), m1.decimals, m1.symbol),
    pool.fee, pool.tickSpacing, pool.poolKey.hooks, pool.sqrtPriceX96.toString(), pool.liquidity.toString(), pool.tick,
  );
  const { tickLower, tickUpper } = singleSidedUsdgRange(pool.tick, pool.tickSpacing, usdgIsCurrency0);
  const position = usdgIsCurrency0
    ? Position.fromAmount0({ pool: sdkPool, tickLower, tickUpper, amount0: budget.toString(), useFullPrecision: true })
    : Position.fromAmount1({ pool: sdkPool, tickLower, tickUpper, amount1: budget.toString() });
  if (position.liquidity.toString() === "0") throw new Error("liquidity 0 — USDG budget too small");
  const dep0 = BigInt(position.amount0.quotient.toString()), dep1 = BigInt(position.amount1.quotient.toString());
  const usdgAmount = usdgIsCurrency0 ? dep0 : dep1, otherAmount = usdgIsCurrency0 ? dep1 : dep0;
  if (usdgAmount <= 0n || usdgAmount > budget || otherAmount !== 0n) throw new Error("SDK did not produce a bounded USDG-only position");

  // Snapshot before approvals. Both approval layers are exact-budget rather than unlimited.
  const erc = new ethers.Contract(USDG, ["function balanceOf(address) view returns(uint256)", "function allowance(address,address) view returns(uint256)", "function approve(address,uint256) returns(bool)"], w);
  const snapshot: bigint = await erc.balanceOf!(w.address);
  if (snapshot < budget) throw new Error(`insufficient USDG: have ${snapshot}, bounded budget ${budget}`);
  // Reset even a pre-existing unlimited approval to this operation's explicit raw cap.
  if ((await erc.allowance!(w.address, PERMIT2)) !== budget) await (await erc.approve!(PERMIT2, budget, await overrides())).wait();
  const permit2 = new ethers.Contract(PERMIT2, ["function approve(address,address,uint160,uint48)"], w);
  await (await permit2.approve!(USDG, C.v4PositionManager!, budget, Math.floor(Date.now() / 1000) + 3600, await overrides())).wait();

  // Zero mint slippage is deliberate: price movement must revert rather than turn this into a
  // two-sided pull or increase the USDG settlement beyond the exact constructed position.
  const { calldata, value } = V4PositionManager.addCallParameters(position, { recipient: w.address, slippageTolerance: new Percent(0, 100), deadline: Math.floor(Date.now() / 1000 + 600).toString() });
  if (BigInt(value) !== 0n) throw new Error("ERC20-only mint unexpectedly requires native value");
  if ((await erc.balanceOf!(w.address)) !== snapshot) throw new Error("USDG balance changed after snapshot; refusing mint");
  try { await provider.call({ to: C.v4PositionManager!, data: calldata, value: 0n, from: w.address }); }
  catch (e) { throw new Error(`simulasi mint v4 USDG single revert: ${((e as any).shortMessage || (e as Error).message || "").slice(0, 160)}`); }
  const tx = await w.sendTransaction({ to: C.v4PositionManager!, data: calldata, value: 0n, ...(await overrides()) });
  const rc = await tx.wait(), tokenId = tokenIdFromReceipt(rc!);
  if (!tokenId) throw new Error(`mint ${tx.hash} confirmed but ERC721 tokenId was not found`);
  const owner: string = await new ethers.Contract(C.v4PositionManager!, ["function ownerOf(uint256) view returns(address)"], provider).ownerOf!(tokenId);
  if (owner.toLowerCase() !== w.address.toLowerCase()) throw new Error(`minted position ${tokenId} is not wallet-owned`);
  saveV4Deposit(tokenId, { depositWei: "0", ts: Date.now(), poolId: pool.poolId, fee: pool.fee, tickLower, tickUpper, mode: "usdg-single", dep0: dep0.toString(), dep1: dep1.toString() });
  log.info(`open v4 USDG SINGLE #${tokenId} ${m0.symbol}/${m1.symbol} raw=${usdgAmount}`);
  return { tokenId, txHash: tx.hash, fee: pool.fee, tickLower, tickUpper, depositEth: "0", poolId: pool.poolId };
}

/** Pool-dependent token acquisition split, shared with CLI preflight. */
export function v4UsdgInRangeSplit(pool: V4Pool, budget: bigint): { swap: bigint; retain: bigint } {
  const usdg0 = pool.poolKey.currency0.toLowerCase() === USDG.toLowerCase();
  if (!usdg0 && pool.poolKey.currency1.toLowerCase() !== USDG.toLowerCase()) throw new Error("pool is not token/USDG");
  const sp = pool.tickSpacing, anchor = Math.floor(pool.tick / sp) * sp;
  const c1ppm = BigInt(Math.round(swapFractionV4(pool.tick, anchor - 4 * sp, anchor + 4 * sp) * 1e6));
  return boundedBudgetSplit(budget, usdg0 ? c1ppm : 1_000_000n - c1ppm);
}

/** Acquire only the token-side delta via Kyber, then mint directly through PositionManager. */
export async function openV4UsdgKyberInRange(pool: V4Pool, budget: bigint, widthPercent = 25): Promise<V4OpenResult & { swapHash: string; swappedPct: number; rangeWidthPercent: number }> {
  const w = wallet(), c0 = pool.poolKey.currency0, c1 = pool.poolKey.currency1;
  const usdg0 = c0.toLowerCase() === USDG.toLowerCase(), token = usdg0 ? c1 : c0;
  if (budget <= 0n || budget >= (1n << 160n)) throw new Error("USDG budget is outside Permit2 uint160 bounds");
  if (pool.quote !== "usd" || (!usdg0 && c1.toLowerCase() !== USDG.toLowerCase())) throw new Error("pool is not token/USDG");
  if (pool.liquidity <= 0n || pool.poolKey.hooks.toLowerCase() !== ethers.ZeroAddress || pool.fee >= 0x800000) throw new Error("pool must be liquid, static-fee and hookless");
  if (!kyberEnabled() || !env.kyberRouter) throw new Error("Kyber route execution is not configured");
  const [m0, m1] = await Promise.all([tokenMeta(c0), tokenMeta(c1)]);
  const sdkPool = new Pool(new Token(cfg.chainId, ethers.getAddress(c0), m0.decimals, m0.symbol), new Token(cfg.chainId, ethers.getAddress(c1), m1.decimals, m1.symbol), pool.fee, pool.tickSpacing, pool.poolKey.hooks, pool.sqrtPriceX96.toString(), pool.liquidity.toString(), pool.tick);
  if (!Number.isFinite(widthPercent) || widthPercent < 1 || widthPercent > 50) throw new Error("range width percent must be 1..50");
  const sp = pool.tickSpacing, halfTicks = Math.log1p(widthPercent / 100) / Math.log(1.0001);
  const tickLower = Math.floor((pool.tick - halfTicks) / sp) * sp, tickUpper = Math.ceil((pool.tick + halfTicks) / sp) * sp;
  const split = v4UsdgInRangeSplit(pool, budget);
  const abi = ["function balanceOf(address) view returns(uint256)", "function allowance(address,address) view returns(uint256)", "function approve(address,uint256) returns(bool)"];
  const usdg = new ethers.Contract(USDG, abi, w), tok = new ethers.Contract(token, abi, w);
  const [beforeUsd, beforeTok]: bigint[] = await Promise.all([usdg.balanceOf!(w.address), tok.balanceOf!(w.address)]);
  if (beforeUsd < budget) throw new Error(`insufficient USDG: have ${beforeUsd}, bounded budget ${budget}`);
  if ((await usdg.allowance!(w.address, env.kyberRouter)) !== split.swap) await (await usdg.approve!(env.kyberRouter, split.swap, await overrides())).wait();
  const sw = await kyberSwap(USDG, ethers.getAddress(token), split.swap);
  if (!sw) throw new Error("Kyber route disappeared before execution");
  const [afterUsd, afterTok]: bigint[] = await Promise.all([usdg.balanceOf!(w.address), tok.balanceOf!(w.address)]);
  await (await usdg.approve!(env.kyberRouter, 0n, await overrides())).wait();
  if (afterUsd > beforeUsd || afterTok < beforeTok) throw new Error("unexpected balance movement during Kyber acquisition");
  const spent = beforeUsd - afterUsd, tokenDelta = afterTok - beforeTok;
  if (spent <= 0n || spent > split.swap) throw new Error(`Kyber USDG spend ${spent} exceeds bounded split ${split.swap}`);
  if (tokenDelta <= 0n || sw.amountOut !== tokenDelta) throw new Error("Kyber token acquisition delta is zero or inconsistent");
  const retained = budget - spent;
  if (retained <= 0n || retained > afterUsd) throw new Error("no bounded retained USDG for mint");

  const slip = new Percent(Math.round(cfg.lp.slippagePct || 5), 100);
  const avail0 = usdg0 ? retained : tokenDelta, avail1 = usdg0 ? tokenDelta : retained;
  const mk = (a0: bigint, a1: bigint) => Position.fromAmounts({ pool: sdkPool, tickLower, tickUpper, amount0: a0.toString(), amount1: a1.toString(), useFullPrecision: true });
  let position = mk(avail0, avail1), mx = position.mintAmountsWithSlippage(slip);
  let max0 = BigInt(mx.amount0.toString()), max1 = BigInt(mx.amount1.toString()), ppm = 1_000_000n;
  if (max0 > avail0 && max0 > 0n) ppm = (avail0 * 1_000_000n) / max0;
  if (max1 > avail1 && max1 > 0n) { const r = (avail1 * 1_000_000n) / max1; if (r < ppm) ppm = r; }
  if (ppm < 1_000_000n) position = mk((avail0 * ppm * 999n) / 1_000_000_000n, (avail1 * ppm * 999n) / 1_000_000_000n);
  if (position.liquidity.toString() === "0") throw new Error("liquidity 0 — USDG budget too small");
  mx = position.mintAmountsWithSlippage(slip); max0 = BigInt(mx.amount0.toString()); max1 = BigInt(mx.amount1.toString());
  if (max0 <= 0n || max1 <= 0n || max0 > avail0 || max1 > avail1) throw new Error("SDK mint caps exceed operation balances");
  if (max0 >= (1n << 160n) || max1 >= (1n << 160n)) throw new Error("SDK mint cap exceeds Permit2 uint160 bounds");
  const p2 = new ethers.Contract(PERMIT2, ["function approve(address,address,uint160,uint48)"], w);
  const approveExact = async (addr: string, erc: ethers.Contract, cap: bigint) => {
    if ((await erc.allowance!(w.address, PERMIT2)) !== cap) await (await erc.approve!(PERMIT2, cap, await overrides())).wait();
    await (await p2.approve!(addr, C.v4PositionManager!, cap, Math.floor(Date.now() / 1000) + 3600, await overrides())).wait();
  };
  await approveExact(c0, usdg0 ? usdg : tok, max0); await approveExact(c1, usdg0 ? tok : usdg, max1);
  const { calldata, value } = V4PositionManager.addCallParameters(position, { recipient: w.address, slippageTolerance: slip, deadline: Math.floor(Date.now() / 1000 + 600).toString() });
  if (BigInt(value) !== 0n) throw new Error("ERC20-only mint unexpectedly requires native value");
  try { await provider.call({ to: C.v4PositionManager!, data: calldata, value: 0n, from: w.address }); }
  catch (e) { throw new Error(`simulasi mint v4 USDG Kyber revert: ${((e as any).shortMessage || (e as Error).message || "").slice(0, 160)}`); }
  const tx = await w.sendTransaction({ to: C.v4PositionManager!, data: calldata, value: 0n, ...(await overrides()) });
  const rc = await tx.wait(), tokenId = tokenIdFromReceipt(rc!);
  if (!tokenId) throw new Error(`mint ${tx.hash} confirmed but ERC721 tokenId was not found`);
  const owner: string = await new ethers.Contract(C.v4PositionManager!, ["function ownerOf(uint256) view returns(address)"], provider).ownerOf!(tokenId);
  if (owner.toLowerCase() !== w.address.toLowerCase()) throw new Error(`minted position ${tokenId} is not wallet-owned`);
  saveV4Deposit(tokenId, { depositWei: "0", ts: Date.now(), poolId: pool.poolId, fee: pool.fee, tickLower, tickUpper, mode: "usdg-kyber-inrange", dep0: position.amount0.quotient.toString(), dep1: position.amount1.quotient.toString() });
  log.info(`open v4 USDG KYBER IN-RANGE #${tokenId} ${m0.symbol}/${m1.symbol} spent=${spent}`);
  return { tokenId, txHash: tx.hash, swapHash: sw.tx, swappedPct: Number((spent * 100n) / budget), fee: pool.fee, tickLower, tickUpper, rangeWidthPercent: widthPercent, depositEth: "0", poolId: pool.poolId };
}

/**
 * Pick the pool that BUYS the token cheapest for `ethIn` — quote the ETH→token swap across all
 * of the token's native-ETH pools and take the one returning the most token (this captures BOTH
 * the fee tier AND the pool depth / price impact). Avoids buying on the thin high-fee pool the
 * user chose to farm, which would bleed fee + slippage before the position even opens.
 */
async function bestSwapPool(pools: V4Pool[], ethIn: bigint): Promise<V4Pool | null> {
  const cands = pools.filter((p) => p.quote === "eth" && p.liquidity > 0n && p.poolKey.currency0.toLowerCase() === NATIVE);
  if (!cands.length) return null;
  const quotes = await mapLimit(cands, 6, async (p) => {
    const out = await quoteV4(p.poolKey, true, ethIn).catch(() => 0n); // ETH(c0)→token(c1)
    return { p, out };
  });
  const best = quotes.filter((q) => q.out > 0n).sort((a, b) => (b.out > a.out ? 1 : b.out < a.out ? -1 : 0))[0];
  return best?.p ?? null;
}

/** Fraction of ETH (currency0) to swap into token so a straddling range fills. */
function swapFractionV4(tick: number, tickLower: number, tickUpper: number): number {
  const sP = Math.pow(1.0001, tick / 2);
  const sA = Math.pow(1.0001, tickLower / 2);
  const sB = Math.pow(1.0001, tickUpper / 2);
  if (sP <= sA) return 0;
  if (sP >= sB) return 1;
  const a0 = (sB - sP) / (sP * sB); // currency0 (ETH) per L
  const a1in0 = (sP - sA) / (sP * sP); // currency1 (token) per L, valued in currency0
  return a1in0 / (a0 + a1in0);
}

/** ERC721 Transfer(0x0 → recipient) → minted tokenId. */
function tokenIdFromReceipt(rc: ethers.TransactionReceipt): string | null {
  const posm = C.v4PositionManager!.toLowerCase();
  const ZERO = "0x" + "0".repeat(64);
  for (const lg of rc.logs) {
    if (lg.address.toLowerCase() === posm && lg.topics.length === 4 && lg.topics[1] === ZERO) {
      return BigInt(lg.topics[3]!).toString();
    }
  }
  return null;
}
