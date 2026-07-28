"""Safety properties of the V4 certification procedure.

The marker this writes is what unblocks unattended V4 entry, so the tests that
matter are the ones proving it is *withheld*: a partial probe must never leave the
bot authorised to trade on evidence that was never completed.
"""
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
import hybrid_v4
import v4_certify

HEALTHY_STATUS = {
    'executor': '0x' + 'ab' * 20, 'deployed': True, 'chainId': 4663, 'chainOk': True,
    'ownerOk': True, 'usdgOk': True, 'positionManagerOk': True, 'swapTargetOk': True,
    'paused': False, 'swapSelectorAllowed': {'0xe21fd0e9': True}, 'selectorReady': True,
}
TOKEN = '0x' + 'cd' * 20


class CertifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rh-cert-'))
        for name, value in (('V4_LIFECYCLE_MARKER', self.tmp / 'v4_lifecycle_pass.json'),
                            ('KILL_SWITCH', self.tmp / 'halt'),
                            ('V4_POSITIONS_FILE', self.tmp / 'v4_positions.json')):
            p = patch.object(cfg, name, value); p.start(); self.addCleanup(p.stop)

    def stub(self, *, status=None, usdg=10 * 10 ** 6, eth=10 ** 17,
             pools=(1,), quotes=({'eligible': True, 'amountOut': 1},)):
        """Patch the outermost I/O so check/rehearse/execute run for real."""
        import contextlib

        import atomic_v4_backend
        import common as c
        import v4_backend
        for target, name, value in (
            (atomic_v4_backend, 'status', lambda: dict(status or HEALTHY_STATUS)),
            (c, 'erc20_balance', lambda *a, **k: usdg),
            (c, 'eth_balance', lambda *a, **k: eth),
            # The real lock probes the pending nonce over RPC; without this the
            # failure-path tests reach for the network and their timing depends on it.
            (c, 'wallet_lock', contextlib.nullcontext),
            (v4_backend, 'discover', lambda *a, **k: list(pools)),
            (v4_backend, 'quote', lambda *a, **k: list(quotes)),
        ):
            p = patch.object(target, name, value); p.start(); self.addCleanup(p.stop)


class TestCheck(CertifyCase):
    def test_healthy_configuration_is_ready(self):
        self.stub()
        report = v4_certify.check(TOKEN, Decimal('1'))
        self.assertTrue(report['ready'], report['blockers'])
        self.assertFalse(report['broadcast'])

    def test_every_blocker_is_reported_not_just_the_first(self):
        self.stub(status={**HEALTHY_STATUS, 'paused': True, 'selectorReady': False}, usdg=0, eth=0)
        report = v4_certify.check(TOKEN, Decimal('1'))
        self.assertFalse(report['ready'])
        joined = ' | '.join(report['blockers'])
        for expected in ('paused', 'setSwapSelector', 'USDG', 'native gas'):
            self.assertIn(expected, joined)

    def test_unallowlisted_selector_is_named_with_its_remedy(self):
        self.stub(status={**HEALTHY_STATUS, 'selectorReady': False})
        report = v4_certify.check(TOKEN, Decimal('1'))
        self.assertTrue(any('setSwapSelector(0xe21fd0e9,true)' in b for b in report['blockers']))

    def test_armed_kill_switch_blocks(self):
        self.stub()
        cfg.KILL_SWITCH.write_text('halt')
        self.assertIn('kill switch is armed', v4_certify.check(TOKEN, Decimal('1'))['blockers'])

    def test_probe_size_is_capped(self):
        self.stub()
        report = v4_certify.check(TOKEN, Decimal('1000'))
        self.assertTrue(any('outside' in b for b in report['blockers']))

    def test_check_never_claims_to_broadcast(self):
        self.stub()
        self.assertFalse(v4_certify.check(TOKEN, Decimal('1'))['broadcast'])


class TestExecuteWithholdsMarker(CertifyCase):
    """Each step can fail; none of them may leave a marker behind."""

    def _run_expecting_failure(self, **overrides):
        import atomic_v4_backend
        import v4_backend
        self.stub()
        defaults = {
            'open_position': lambda *a, **k: {'tokenId': '77', 'txHash': '0xopen'},
            'close_position': lambda *a, **k: {'receipt': {'hash': '0xclose'}, 'nftGone': True},
            'collect': lambda *a, **k: {'txHash': '0xcollect'},
            'list_positions': lambda: [],
            'capability_preflight': lambda *a, **k: {'ok': True},
        }
        defaults.update(overrides)
        with patch.object(atomic_v4_backend, 'open_position', defaults['open_position']), \
             patch.object(atomic_v4_backend, 'close_position', defaults['close_position']), \
             patch.object(atomic_v4_backend, 'capability_preflight', defaults['capability_preflight']), \
             patch.object(v4_backend, 'collect', defaults['collect']), \
             patch.object(v4_backend, 'list_positions', defaults['list_positions']):
            with self.assertRaises(Exception):
                v4_certify.execute(TOKEN, Decimal('1'))
        self.assertFalse(cfg.V4_LIFECYCLE_MARKER.exists(), 'marker must not survive a failed probe')
        self.assertFalse(hybrid_v4.lifecycle_verified())

    def test_open_without_a_transaction_hash_withholds(self):
        self._run_expecting_failure(open_position=lambda *a, **k: {'tokenId': '77'})

    def test_collect_without_a_transaction_hash_withholds(self):
        self._run_expecting_failure(collect=lambda *a, **k: {})

    def test_close_that_does_not_remove_the_nft_withholds(self):
        self._run_expecting_failure(
            close_position=lambda *a, **k: {'receipt': {'hash': '0xclose'}, 'nftGone': False})

    def test_position_still_listed_after_close_withholds(self):
        self._run_expecting_failure(list_positions=lambda: [{'tokenId': '77'}])

    def test_unready_configuration_never_reaches_the_chain(self):
        import atomic_v4_backend
        self.stub(status={**HEALTHY_STATUS, 'paused': True})
        opened = []
        with patch.object(atomic_v4_backend, 'open_position', lambda *a, **k: opened.append(1)):
            with self.assertRaises(v4_certify.NotReady):
                v4_certify.execute(TOKEN, Decimal('1'))
        self.assertEqual(opened, [], 'no transaction may be attempted while blocked')


class TestExecuteSuccess(CertifyCase):
    def test_a_complete_probe_writes_a_marker_that_satisfies_the_gate(self):
        import atomic_v4_backend
        import v4_backend
        self.stub()
        with patch.object(atomic_v4_backend, 'open_position',
                          lambda *a, **k: {'tokenId': '77', 'txHash': '0xopen', 'poolId': '0xp'}), \
             patch.object(atomic_v4_backend, 'close_position',
                          lambda *a, **k: {'receipt': {'hash': '0xclose'}, 'nftGone': True,
                                           'totalUsdgProceedsRaw': 990_000}), \
             patch.object(atomic_v4_backend, 'capability_preflight', lambda *a, **k: {'ok': True}), \
             patch.object(v4_backend, 'collect', lambda *a, **k: {'txHash': '0xcollect'}), \
             patch.object(v4_backend, 'list_positions', lambda: []):
            report = v4_certify.execute(TOKEN, Decimal('1'))

        self.assertTrue(report['broadcast'])
        marker = json.loads(cfg.V4_LIFECYCLE_MARKER.read_text())
        self.assertEqual(marker['chain_id'], cfg.CHAIN_ID)
        self.assertEqual(marker['wallet'].lower(), cfg.WALLET_ADDRESS.lower())
        self.assertIs(marker['post_close_open'], False)
        for key in ('mint_tx', 'collect_tx', 'close_tx'):
            self.assertTrue(marker[key], f'{key} missing from marker')
        # The point of the whole procedure: the gate opens, and only now.
        self.assertTrue(hybrid_v4.lifecycle_verified())


class TestCliSafety(unittest.TestCase):
    def test_execute_requires_confirm(self):
        with patch.object(sys, 'argv', ['v4_certify.py', '--execute', '--token', TOKEN]):
            with self.assertRaises(SystemExit) as ctx:
                v4_certify.main()
        self.assertIn('--confirm', str(ctx.exception))

    def test_execute_requires_a_token(self):
        with patch.object(sys, 'argv', ['v4_certify.py', '--execute', '--confirm']):
            with self.assertRaises(SystemExit) as ctx:
                v4_certify.main()
        self.assertIn('--token', str(ctx.exception))

    def test_default_mode_is_check(self):
        ap_argv = ['v4_certify.py']
        with patch.object(sys, 'argv', ap_argv), \
             patch.object(v4_certify, 'check', return_value={'mode': 'check', 'ready': True,
                                                             'broadcast': False}) as spy:
            v4_certify.main()
        spy.assert_called_once()


if __name__ == '__main__':
    unittest.main()
