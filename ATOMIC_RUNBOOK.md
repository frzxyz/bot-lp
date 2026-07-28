# RH atomic lifecycle status / runbook

## Audited deployments (read-only RPC, 2026-07-27)
- V3 factory `0x1f7d...2efa` (24,535 runtime bytes)
- V3 NFT PositionManager `0x7399...e0d3` (24,384 bytes)
- V3 deadline SwapRouter v1 `0xcaf6...5cb2` (24,497 bytes). Config's `swapRouter02` label is misleading.
- `0x8bce...937f` is configured as V2 factory in TS, not a safe V3 backup.
- V4 PoolManager `0x8366...0951`, PositionManager `0x58da...4fA7`, StateView `0xF333...673b`, Quoter `0x628c...b0Ac`, Universal Router `0x8876...0904`, canonical Permit2 `0x000000000022D473030F116dDEE9F6B43aC78BA3`.
- Token ordering is strict numeric-address order. Executor derives `(token0,token1)` and verifies `factory.getPool`; close additionally verifies NFT token/fee/ticks.

## Support decision
V3 is implemented atomically using the deployed deadline router: exact USDG pull -> exact-input swap -> mint-to-owner -> dust refund, or approved NFT decrease/collect-to-executor -> swap collected token delta only -> optional burn -> refund. Every failure reverts the whole transaction.

V4 is **fail closed / unsupported**, not faked. The current Kyber build is generated with wallet as both `sender` and `recipient`, while an atomic call would execute with the executor as taker and needs output at the executor. Its opaque calldata has only router/address/amount checks today; recipient/taker fields and selector semantics are not decoded/proven. PONS hooked tokens also make direct V3/Universal Router routes unreliable; the current v4 code explicitly filters hooked pools for direct routes. Until a route built with executor sender+recipient is decoded, selector-allowlisted, and simulated through PositionManager `modifyLiquidities` as an approved operator, V4 atomic open/close must revert at the adapter gate.

## Safety properties / limitations
- Chain 4663 guard, owner-only, two-step ownership, starts paused, nonreentrant, 30-minute deadline cap, nonzero minima, exact temporary approvals reset to zero, pool/NFT identity checks, no arbitrary call/delegatecall, and zero lifecycle-token balance at entry/exit.
- Dirty executor balances fail lifecycle and can only be rescued by owner. This prevents preexisting balances being mistaken for proceeds.
- Fee-on-transfer/rebasing tokens fail delta/minimum or final-zero checks; unsupported intentionally.
- Close swaps only the collected token balance. Existing wallet NFT needs operator approval. `burn=true` only works on a completely empty NFT; otherwise use false.
- Rotation is two transactions: atomic V3 close to USDG, then atomic open. It is not represented as one transaction.

## Build and no-broadcast verification
```sh
cd /root/money-printer-repos/robinhood-lp-bot
npx --yes solc@0.8.24 --standard-json --base-path . < solc.atomic.json > artifacts/atomic/combined.json
node --import tsx scripts/atomic-admin.ts verify $ATOMIC_EXECUTOR_ADDRESS
```

## Deployment (prepared, DO NOT run before safety gates)
The key is read only from `RH_WALLET_KEY`, never printed. Without the explicit flag the script refuses.
```sh
cd /root/money-printer-repos/robinhood-lp-bot
set -a; . ./.env; set +a
node --import tsx scripts/atomic-admin.ts deploy --broadcast
# writes deployments/4663-atomic-v3.json
```

## One-time approvals (prepare/simulate before send)
1. USDG standing allowance from wallet to executor (choose a finite operational cap, not unlimited): `approve(executor, cap)`.
2. V3 NPM existing-position operator approval: `setApprovalForAll(executor,true)` (or `approve(executor,tokenId)` per NFT).
3. No wallet->router allowance is needed. Executor grants exact transient router/NPM allowances itself.

Use `eth_call` with `from=wallet` for both approval calldata first; only the parent may send. V4 approval is deliberately not requested while unsupported.

## Tiny probe (<= 1 USDG; prepared sequence, no automatic broadcast)
- Quote a supported **unhooked/non-FOT** V3 USDG pool; cap `usdgAmount <= 1_000_000` raw.
- Derive swap amount and nonzero `minTokenOut`, `amount0Min`, `amount1Min`; deadline <= now+600.
- Encode `atomicOpen`, then `eth_call` from wallet to executor. Inspect revert and gas estimate.
- Parent may send the exact simulated calldata only after confirming owner, paused=false, bytecode/constants, allowance, pool, fee, ticks, and limits.
- Verify receipt event `Opened`, NFT `ownerOf(tokenId)==wallet`, executor USDG/token balances are zero, and wallet dust delta. Close probe similarly with NFT approval and `atomicClose`; verify `Closed`, minimum USDG delta, zero executor balances, and NFT burn/remaining liquidity as selected.

There is intentionally no probe `--send` helper in this subagent artifact: this prevents accidental broadcast; parent should add/send only after independent calldata review.
