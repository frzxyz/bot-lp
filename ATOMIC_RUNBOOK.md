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

V4 is **fail closed / unsupported**, not faked. PONS hooked tokens also make direct V3/Universal Router routes unreliable; the current v4 code explicitly filters hooked pools for direct routes.

The original blocker was that aggregator calldata was accepted on the strength of the API's JSON envelope alone — `routerAddress`, `amountIn`, `amountOut` — while the field that decides where the proceeds land, `dstReceiver`, was never read. An executor that swaps as itself and measures its own balance delta cannot rely on an unverified promise about the receiver.

Preconditions for enabling V4, and their current state:

1. **Route built with executor as sender and recipient** — done. `atomic-v4-plan.ts` builds with `EXECUTOR` on both sides, and `kyberPreflight` accepts a `taker` override so close/liquidation proofs can be built for the executor rather than the wallet.
2. **Calldata decoded and proven** — done. `src/chain/kyberDecode.ts` decodes `swap` (`0xe21fd0e9`) and `swapSimpleMode` (`0x8af033fb`), and asserts srcToken/dstToken, `amount`, `minReturnAmount`, `srcAmounts` total, absence of injected fees, and `dstReceiver`. Both struct layouts are hashed at import and checked against the published selectors, so a wrong field list cannot decode into plausible garbage. Covered by `test/kyber-decode.test.ts`.
3. **Selector allowlisted on the deployed executor** — *not done, requires an owner transaction.* `setSwapSelector(0xe21fd0e9,true)` must be sent while the contract is paused. Verify afterwards with `swapSelectorAllowed(0xe21fd0e9)`.
4. **Simulated end to end through PositionManager `modifyLiquidities` as an approved operator** — *not done, requires a live funded rehearsal.* Until a probe open+close confirms `Opened`/`Closed`, `ownerOf(tokenId)==wallet`, zero executor balances, and the exact USDG delta, `lifecycle_verified()` stays false and unattended V4 entry remains blocked.

Steps 3 and 4 are deliberately manual: both move real funds and neither can be discharged offline.

## V4 certification procedure

`automation/v4_certify.py` turns steps 3 and 4 into a reviewable procedure. It has three modes, in increasing order of consequence, and defaults to the harmless one.

```sh
# 1. Read-only. Signs nothing. Lists every unmet precondition at once.
npm run v4:check -- --token 0xCANDIDATE

# 2. Build the real plan and eth_call it against the deployed executor. No broadcast.
cd automation && python3 v4_certify.py --rehearse --token 0xCANDIDATE

# 3. The funded probe. Requires --confirm; refuses without it.
cd automation && python3 v4_certify.py --execute --confirm --token 0xCANDIDATE --size 1
```

The probe runs open → collect → close, verifying each step before the next begins, then re-reads the position list to confirm nothing survived. The marker at `V4_LIFECYCLE_MARKER` is written last and only once every postcondition holds; a probe that opens but fails to close leaves **no** marker, so unattended entry stays blocked and the position remains visible to the manager and close WAL like any other. Probe size is capped by `RH_V4_PROBE_MAX_USDG` (default 2 USDG) — a probe proves the machinery, not a thesis.

If `--check` reports `no Kyber selector allowlisted`, that is step 3: send `setSwapSelector(0xe21fd0e9,true)` while paused, then re-check.

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
