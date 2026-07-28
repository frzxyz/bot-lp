#!/usr/bin/env python3
"""V4 lifecycle certification: readiness check, rehearsal, and the funded probe.

Unattended V4 entry is gated on ``hybrid_v4.lifecycle_verified()``, which reads a
marker that may only exist once a real open -> collect -> close has been observed
end to end on this chain, by this wallet, through the deployed executor. That
rehearsal cannot be discharged offline, so this script exists to make it a
reviewable procedure rather than a set of remembered commands.

Three modes, in increasing order of consequence:

* ``--check`` (default) reports every precondition read-only. It signs nothing.
* ``--rehearse`` additionally builds and simulates the open through the executor
  via ``eth_call``. Still no broadcast.
* ``--execute --confirm`` runs the funded probe and writes the marker, and only
  if every postcondition verifies.

The marker is never written partially: a probe that opens but fails to close
leaves no marker, so unattended entry stays blocked and the position is visible
to the manager and the close WAL like any other.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import common as c
import config as cfg

# A probe exists to prove the machinery, not to take a position. Anything larger
# than this is a strategy decision and does not belong in a certification run.
PROBE_MAX_USDG = Decimal(os.environ.get('RH_V4_PROBE_MAX_USDG', '2'))


class NotReady(RuntimeError):
    """A precondition is unmet. Carries the full report for the operator."""

    def __init__(self, report: dict):
        super().__init__('; '.join(report.get('blockers', [])) or 'not ready')
        self.report = report


def _ok(value) -> str:
    return 'OK' if value else 'BLOCKED'


def check(token: str | None = None, probe_usdg: Decimal | None = None) -> dict:
    """Read-only readiness report. Every unmet precondition is listed, not just the first."""
    probe = Decimal(str(probe_usdg if probe_usdg is not None else cfg.POSITION_SIZE_USDG))
    blockers: list[str] = []
    report: dict = {'mode': 'check', 'broadcast': False, 'probe_usdg': str(probe)}

    if not cfg.ATOMIC_LP_ONLY:
        blockers.append('ATOMIC_LP_ONLY is false; certification only covers the atomic path')
    if not cfg.ATOMIC_V4_EXECUTOR_ADDRESS:
        blockers.append('ATOMIC_V4_EXECUTOR_ADDRESS is unset')
    if probe <= 0 or probe > PROBE_MAX_USDG:
        blockers.append(f'probe size {probe} outside 0 < n <= {PROBE_MAX_USDG}')
    if cfg.KILL_SWITCH.exists():
        blockers.append('kill switch is armed')

    # --- executor identity, pause state, selector allowlist (precondition 3) ---
    try:
        import atomic_v4_backend
        st = atomic_v4_backend.status()
        report['executor'] = st
        for field, label in (('deployed', 'executor has no bytecode'), ('chainOk', 'wrong chain'),
                             ('ownerOk', 'executor owner is not the wallet'),
                             ('usdgOk', 'executor USDG mismatch'),
                             ('positionManagerOk', 'executor PositionManager mismatch'),
                             ('swapTargetOk', 'executor swap target is not the Kyber router')):
            if not st.get(field):
                blockers.append(label)
        if st.get('paused'):
            blockers.append('executor is paused; setPaused(false) before the probe')
        if not st.get('selectorReady'):
            blockers.append('no Kyber selector allowlisted; send setSwapSelector(0xe21fd0e9,true) while paused')
    except Exception as exc:
        blockers.append(f'executor status unavailable: {str(exc)[:160]}')

    # --- wallet funding ---
    try:
        usdg = Decimal(c.erc20_balance(cfg.USDG)) / Decimal(10 ** cfg.USDG_DECIMALS)
        eth = Decimal(c.eth_balance()) / Decimal(10 ** 18)
        report['wallet'] = {'usdg': str(usdg), 'eth': str(eth)}
        if usdg < probe + cfg.USDG_RESERVE:
            blockers.append(f'USDG {usdg} below probe {probe} plus reserve {cfg.USDG_RESERVE}')
        if eth < cfg.GAS_RESERVE_ETH:
            blockers.append(f'native gas {eth} below reserve {cfg.GAS_RESERVE_ETH}')
    except Exception as exc:
        blockers.append(f'wallet balances unavailable: {str(exc)[:160]}')

    # --- a live, quotable candidate pool ---
    if token:
        try:
            import v4_backend
            raw = int(probe * 10 ** cfg.USDG_DECIMALS)
            pools = v4_backend.discover(token)
            quotes = [q for q in v4_backend.quote(token, raw)
                      if q.get('eligible') and int(q.get('amountOut', 0)) > 0]
            report['candidate'] = {'token': token, 'pools': len(pools), 'quotes': len(quotes)}
            if not pools:
                blockers.append('no eligible live V4 pool for the candidate')
            if not quotes:
                blockers.append('no executable V4 quote at the probe size')
        except Exception as exc:
            blockers.append(f'candidate discovery failed: {str(exc)[:160]}')
    else:
        report['candidate'] = None

    # --- marker state (precondition 4) ---
    import hybrid_v4
    report['lifecycle_verified'] = hybrid_v4.lifecycle_verified()
    report['marker_path'] = str(cfg.V4_LIFECYCLE_MARKER)
    report['blockers'] = blockers
    report['ready'] = not blockers
    return report


def rehearse(token: str, probe_usdg: Decimal | None = None) -> dict:
    """Check, then build and simulate the open through the executor. No broadcast."""
    probe = Decimal(str(probe_usdg if probe_usdg is not None else cfg.POSITION_SIZE_USDG))
    report = check(token, probe)
    report['mode'] = 'rehearse'
    if not report['ready']:
        raise NotReady(report)
    import atomic_v4_backend
    raw = int(probe * 10 ** cfg.USDG_DECIMALS)
    # capability_preflight builds the real plan and eth_calls it against the
    # deployed executor, which is the closest thing to the probe that costs nothing.
    report['open_simulation'] = atomic_v4_backend.capability_preflight(token, raw)
    report['broadcast'] = False
    return report


def _write_marker(record: dict) -> None:
    cfg.V4_LIFECYCLE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.V4_LIFECYCLE_MARKER.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
    tmp.replace(cfg.V4_LIFECYCLE_MARKER)


def execute(token: str, probe_usdg: Decimal | None = None) -> dict:
    """Run the funded probe: open, collect, close, verify, then write the marker.

    Each step verifies before the next begins. The marker is written last and only
    once, so an interrupted probe can never leave unattended entry enabled on
    partial evidence.
    """
    probe = Decimal(str(probe_usdg if probe_usdg is not None else cfg.POSITION_SIZE_USDG))
    report = rehearse(token, probe)
    report['mode'] = 'execute'

    import atomic_v4_backend
    import v4_backend
    raw = int(probe * 10 ** cfg.USDG_DECIMALS)
    usdg_before = c.erc20_balance(cfg.USDG)

    with c.wallet_lock():
        opened = atomic_v4_backend.open_position(token, raw)
        token_id = str(opened['tokenId'])
        report['open'] = {'tokenId': token_id, 'tx': opened.get('txHash'),
                          'poolId': opened.get('poolId'), 'liquidity': str(opened.get('liquidity', ''))}
        if not opened.get('txHash'):
            raise RuntimeError('open produced no transaction hash')

        # A collect on a position that has earned nothing still exercises the
        # PositionManager path the marker attests to, which is the point.
        collected = v4_backend.collect(token_id)
        collect_tx = (collected or {}).get('txHash') or (collected or {}).get('hash')
        report['collect'] = {'tx': collect_tx, 'raw': collected}
        if not collect_tx:
            raise RuntimeError('collect produced no transaction hash; marker withheld')

        closed = atomic_v4_backend.close_position(token_id, token)
        close_tx = (closed.get('receipt') or {}).get('hash')
        report['close'] = {'tx': close_tx, 'nftGone': closed.get('nftGone'),
                           'proceedsRaw': closed.get('totalUsdgProceedsRaw'),
                           'executorBalances': closed.get('executorBalances')}
        if not close_tx or not closed.get('nftGone'):
            raise RuntimeError('close did not confirm NFT removal; marker withheld')

    # Postconditions, read fresh rather than inferred from the close result.
    still_open = any(str(p.get('tokenId')) == token_id for p in v4_backend.list_positions())
    usdg_after = c.erc20_balance(cfg.USDG)
    report['post'] = {'post_close_open': still_open,
                      'usdg_before_raw': usdg_before, 'usdg_after_raw': usdg_after,
                      'usdg_delta_raw': usdg_after - usdg_before}
    if still_open:
        raise RuntimeError('position still listed after close; marker withheld')

    marker = {'passed': True, 'chain_id': cfg.CHAIN_ID, 'wallet': cfg.WALLET_ADDRESS,
              'post_close_open': False, 'token': token, 'token_id': token_id,
              'mint_tx': opened.get('txHash'), 'collect_tx': collect_tx, 'close_tx': close_tx,
              'probe_usdg': str(probe), 'usdg_delta_raw': usdg_after - usdg_before,
              'certified_at': int(time.time())}
    _write_marker(marker)
    report['marker'] = marker
    report['broadcast'] = True

    import hybrid_v4
    report['lifecycle_verified'] = hybrid_v4.lifecycle_verified()
    if not report['lifecycle_verified']:
        raise RuntimeError('marker written but lifecycle_verified() still false; inspect the marker')
    return report


def _print_human(report: dict) -> None:
    print(f"mode={report['mode']}  broadcast={report['broadcast']}  ready={report.get('ready')}")
    ex = report.get('executor') or {}
    if ex:
        print(f"  executor   {ex.get('executor')}")
        print(f"  paused     {ex.get('paused')}   selector allowlisted: {_ok(ex.get('selectorReady'))}")
    w = report.get('wallet') or {}
    if w:
        print(f"  wallet     USDG={w.get('usdg')}  ETH={w.get('eth')}")
    print(f"  marker     lifecycle_verified={report.get('lifecycle_verified')}")
    for b in report.get('blockers', []):
        print(f"  BLOCKER    {b}")


def main() -> int:
    ap = argparse.ArgumentParser(description='V4 lifecycle certification')
    ap.add_argument('--token', help='candidate token address for the probe')
    ap.add_argument('--size', type=Decimal, default=None, help='probe size in whole USDG')
    ap.add_argument('--rehearse', action='store_true', help='build and simulate; no broadcast')
    ap.add_argument('--execute', action='store_true', help='run the funded probe')
    ap.add_argument('--confirm', action='store_true', help='required alongside --execute')
    ap.add_argument('--json', action='store_true', help='emit the full report as JSON')
    a = ap.parse_args()

    try:
        if a.execute:
            if not a.confirm:
                raise SystemExit('--execute moves real funds and requires --confirm')
            if not a.token:
                raise SystemExit('--execute requires --token')
            report = execute(a.token, a.size)
        elif a.rehearse:
            if not a.token:
                raise SystemExit('--rehearse requires --token')
            report = rehearse(a.token, a.size)
        else:
            report = check(a.token, a.size)
    except NotReady as exc:
        report = exc.report
        print(json.dumps(report, indent=2, default=str) if a.json else '', end='')
        if not a.json:
            _print_human(report)
        return 1

    print(json.dumps(report, indent=2, default=str) if a.json else '', end='')
    if not a.json:
        _print_human(report)
    return 0 if report.get('ready', True) else 1


if __name__ == '__main__':
    sys.exit(main())
