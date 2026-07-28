# Universal two-stage close migration

1. Keep every legacy V3/V4 executor paused and approvals revoked. Do not unpause it again.
2. Compile, audit, and deploy the replacement executor from this source in a separate, explicitly approved change window. Deployment is intentionally not part of this change.
3. Verify immutable chain, owner, position manager, settlement token, Permit2/router and aggregator addresses. The USDG V3/V4 builds settle to USDG; the WETH V4 build settles to WETH.
4. While paused, configure only selectors required by atomic **open**. Close does not inspect or call aggregator calldata.
5. Grant the replacement executor NFT operator approval, then unpause only after an `eth_call` preflight. Revoke the old executor approval.
6. Before close broadcast, durably commit the position, settlement asset, token and owner balance baselines. Close atomically removes liquidity and collects principal plus fees through the isolated executor, which transfers only its transaction-local balances to owner and finishes with zero balances.
7. Reconcile the receipt/event and post-close balances. Attribute only `max(post - baseline, 0)`; never use the wallet's full balance. Event amounts provide an independent equality check.
8. Persist the exact attributable non-settlement amount before quoting. Ordinary aggregator settlement must requote, enforce slippage/price-impact <=10%, run `eth_call` and gas estimation, and maintain at most one pending nonce. Retry by receipt/nonce reconciliation before any resend. Never wallet-sweep.
9. Mark complete only after the exact attributable token amount is consumed and the expected USDG or WETH delta is confirmed. Retain ambiguous records for manual reconciliation.

The legacy close tuple fields are retained for ABI migration convenience but swap target/data and swap-output floors are deliberately ignored by close. A later ABI cleanup can remove them after all callers have migrated.
