# RH LP lifecycle fail-safe

- Audit (read-only): `python3 lifecycle_watchdog.py --audit-once`
- Status: `systemctl --user status rh-lp-failsafe.service`
- Journal: `/root/.hermes/state/rh_meme_lp/lifecycle_ops.json`
- Poll interval: `FAILSAFE_POLL_SECONDS` (5–30, default 10).
- `rh_meme_lp_halt` blocks new strategy operations but explicit recorded recovery continues.
- `rh_meme_lp/HARD_HALT_RECOVERY` blocks all recovery broadcasts.
- V4 unattended lifecycle remains unconditionally blocked while fail-safe mode is enabled, until funded atomic lifecycle certification. There is no legacy override.
- The watchdog never adopts wallet inventory. It only processes explicit lifecycle, close-WAL, and liquidation records; do not create records for unrelated inventory.
- There is no automatic nonce replacement/cancel: an uncertain identity or pending nonce alerts/fails closed rather than guessing.

Install only after audit/tests: `systemctl --user daemon-reload && systemctl --user enable --now rh-lp-failsafe.service`.
