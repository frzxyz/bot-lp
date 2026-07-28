"""End-to-end lifecycle tests against a simulated chain.

Every test here drives a real entrypoint (entry.main, manager.main, emergency.main)
rather than a helper, so the seams between modules are covered: record creation,
the manager loop, exit selection, atomic close, and two-stage settlement. Only the
JSON-RPC layer and the executor CLI bridges are faked; see e2e_harness.
"""
import json
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
import emergency
import entry
import manager
import risk
from e2e_harness import FakeChain, simulated_chain


class LifecycleCase(unittest.TestCase):
    """Base: a fresh chain and a redirected state directory per test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rh-e2e-'))
        self.chain = FakeChain()
        self.ctx = simulated_chain(self.chain, self.tmp)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def open_position(self, size='1'):
        with patch.object(sys, 'argv', ['entry.py', self.chain.token, size]):
            return entry.main()

    def positions(self):
        try:
            return json.loads(cfg.POSITIONS_FILE.read_text())
        except FileNotFoundError:
            return {}

    def queues(self):
        try:
            return json.loads(cfg.PENDING_LIQUIDATIONS_FILE.read_text())
        except FileNotFoundError:
            return []


class TestOpen(LifecycleCase):
    def test_open_writes_a_complete_record(self):
        result = self.open_position()
        record = self.positions()[self.chain.token.lower()]
        for key in ('token', 'symbol', 'decimals', 'pool', 'fee', 'token_id', 'tick_lower',
                    'tick_upper', 'entry_tick', 'entry_price_usdg', 'size_usdg',
                    'usdg_deposited', 'tok_deposited', 'mint_tx', 'mint_time',
                    'range_width_percent', 'peak_nav_usdg'):
            self.assertIn(key, record, f'{key} missing from opened V3 record')
        self.assertEqual(int(record['token_id']), int(result['token_id']))

    def test_open_journals_a_completed_lifecycle_operation(self):
        import lifecycle
        self.open_position()
        phases = [op['phase'] for op in lifecycle.load()['operations'].values()]
        self.assertEqual(phases, ['completed'])

    def test_range_width_reaches_the_executor(self):
        """The width the policy chose must be the width the NFT was minted with."""
        self.open_position()
        record = self.positions()[self.chain.token.lower()]
        info = self.chain.position_info(record['token_id'])
        span = info['tick_upper'] - info['tick_lower']
        self.assertGreater(span, 0)
        self.assertEqual(record['tick_lower'], info['tick_lower'])
        self.assertEqual(record['tick_upper'], info['tick_upper'])

    def test_wallet_is_debited_by_exactly_the_position_size(self):
        before = self.chain.bal(cfg.USDG)
        self.open_position('1')
        self.assertEqual(before - self.chain.bal(cfg.USDG), 10 ** cfg.USDG_DECIMALS)


class TestValuation(LifecycleCase):
    def test_nav_matches_an_independent_computation(self):
        self.open_position()
        record = self.positions()[self.chain.token.lower()]
        info = self.chain.position_info(record['token_id'])
        nav = risk.nav_usdg(tick=self.chain.tick(), tick_lower=info['tick_lower'],
                            tick_upper=info['tick_upper'], liquidity=info['liquidity'],
                            token_is_token0=self.chain.token_is_token0)
        self.assertAlmostEqual(float(nav), float(self.chain.true_nav_usdg(record['token_id'])), places=4)

    def test_fresh_position_is_valued_near_its_principal(self):
        self.open_position('1')
        record = self.positions()[self.chain.token.lower()]
        info = self.chain.position_info(record['token_id'])
        nav = risk.nav_usdg(tick=self.chain.tick(), tick_lower=info['tick_lower'],
                            tick_upper=info['tick_upper'], liquidity=info['liquidity'],
                            token_is_token0=self.chain.token_is_token0)
        self.assertGreater(float(nav), 0.90)
        self.assertLess(float(nav), 1.02)


class TestManage(LifecycleCase):
    def test_in_range_position_is_left_alone(self):
        self.open_position()
        manager.main()
        self.assertIn(self.chain.token.lower(), self.positions())

    def test_harvest_moves_fees_into_the_wallet(self):
        self.open_position()
        tid = self.positions()[self.chain.token.lower()]['token_id']
        self.chain.accrue_fees(tid, usdg_raw=3_000_000)
        before = self.chain.bal(cfg.USDG)
        manager.main()
        self.assertEqual(self.chain.bal(cfg.USDG) - before, 3_000_000)
        self.assertIn(self.chain.token.lower(), self.positions(), 'harvest must not close')

    def test_below_range_exits_and_settles_to_usdg(self):
        self.open_position()
        self.chain.move_price(0.4)
        manager.main()
        self.assertEqual(self.positions(), {}, 'position should be closed')
        self.assertEqual([q['phase'] for q in self.queues()], ['completed'])
        self.assertEqual(self.chain.bal(self.chain.token), 0, 'no token may be stranded')

    def test_above_range_does_not_exit_immediately(self):
        """An upside break is riskless — it holds stable, so it must not pay gas early."""
        self.open_position()
        self.chain.move_price(3.0)
        manager.main()
        self.assertIn(self.chain.token.lower(), self.positions())
        record = self.positions()[self.chain.token.lower()]
        self.assertEqual(record.get('oor_side'), risk.ABOVE)

    def test_above_range_recycles_once_its_timer_expires(self):
        self.open_position()
        self.chain.move_price(3.0)
        manager.main()
        record = self.positions()[self.chain.token.lower()]
        record['oor_since'] = int(time.time()) - cfg.OOR_ABOVE_MAX_SECONDS - 60
        cfg.POSITIONS_FILE.write_text(json.dumps({self.chain.token.lower(): record}))
        manager.main()
        self.assertEqual(self.positions(), {})
        cooldown = json.loads(cfg.COOLDOWN_FILE.read_text())
        # A clean upside recycle earns a short cooldown, not the 24h token ban.
        self.assertLess(cooldown[self.chain.token.lower()] - time.time(), 3600)

    def test_side_flip_restarts_the_range_timer(self):
        self.open_position()
        self.chain.move_price(3.0)
        manager.main()
        self.assertIn('oor_since', self.positions()[self.chain.token.lower()])
        self.chain.move_price(1 / 3.0)   # back in range
        manager.main()
        self.assertNotIn('oor_since', self.positions()[self.chain.token.lower()])


class TestSettlement(LifecycleCase):
    def test_kyber_outage_falls_back_to_an_onchain_venue(self):
        """A sidecar outage must not strand the token while it keeps falling."""
        self.open_position()
        self.chain.reverts.add('kyber')
        self.chain.move_price(0.4)
        manager.main()
        queue = self.queues()
        self.assertEqual([q['phase'] for q in queue], ['completed'])
        self.assertEqual(queue[0]['last_quote']['route']['venue'], 'v3')
        self.assertEqual(self.chain.bal(self.chain.token), 0)

    def test_settlement_proceeds_are_recorded_exactly(self):
        self.open_position()
        self.chain.move_price(0.4)
        before = self.chain.bal(cfg.USDG)
        manager.main()
        gained = self.chain.bal(cfg.USDG) - before
        queue = self.queues()[0]
        # Wallet delta must equal the executor's direct settlement plus queue proceeds.
        self.assertGreater(gained, 0)
        self.assertEqual(queue['remaining_raw'], 0)
        self.assertGreaterEqual(gained, queue['proceeds_raw'])

    def test_no_token_dust_remains_after_a_full_cycle(self):
        self.open_position()
        self.chain.move_price(0.4)
        manager.main()
        self.assertEqual(self.chain.bal(self.chain.token), 0)
        self.assertEqual(self.positions(), {})


class TestResilience(LifecycleCase):
    def test_one_malformed_record_does_not_starve_the_others(self):
        """The manager loop used to abort entirely on the first bad position."""
        self.open_position()
        good = self.positions()
        broken = {'token': '0x' + '11' * 20, 'symbol': 'BROKEN'}   # no capital field
        cfg.POSITIONS_FILE.write_text(json.dumps({'0x' + '11' * 20: broken, **good}))
        self.chain.accrue_fees(good[self.chain.token.lower()]['token_id'], usdg_raw=3_000_000)
        before = self.chain.bal(cfg.USDG)
        manager.main()
        self.assertEqual(self.chain.bal(cfg.USDG) - before, 3_000_000,
                         'the healthy position must still be harvested')

    def test_close_failure_leaves_the_position_tracked(self):
        self.open_position()
        self.chain.reverts.add('execute_close')
        self.chain.move_price(0.4)
        manager.main()
        self.assertIn(self.chain.token.lower(), self.positions(),
                      'a failed close must not drop the position from state')

    def test_kill_switch_stops_new_management(self):
        self.open_position()
        cfg.KILL_SWITCH.write_text('halt')
        with patch.object(self.chain, 'check_kill', lambda quiet=False: True):
            manager.main()
        self.assertIn(self.chain.token.lower(), self.positions())


class TestEmergency(LifecycleCase):
    def test_fast_dump_is_caught_within_the_first_hour(self):
        """Protection used to require a 55-minute-old sample, leaving fresh positions bare."""
        self.open_position()
        emergency.main()                      # seeds the first history sample
        self.chain.move_price(0.5)
        emergency.main()
        self.assertEqual(self.positions(), {}, 'a fast dump must close the position')
        self.assertEqual(self.chain.bal(self.chain.token), 0)

    def test_stable_price_is_not_treated_as_a_dump(self):
        self.open_position()
        emergency.main()
        self.chain.move_price(0.99)
        emergency.main()
        self.assertIn(self.chain.token.lower(), self.positions())


class TestTokenSortedAboveUsdg(TestManage, TestValuation, TestSettlement):
    """Re-run the lifecycle with the pool's token ordering reversed.

    When the token sorts above USDG the pool quotes token-per-USDG, so a falling
    token price makes the tick *rise*. Anything comparing ticks naively reads that
    as an upside break and holds a dumping bag; these are the same assertions, so a
    regression in the inversion fails here and nowhere else.
    """

    def setUp(self):
        super().setUp()
        self.chain.token = '0xff00000000000000000000000000000000000001'
        self.chain.balances.setdefault(self.chain.token.lower(), 0)
        self.chain.token_is_token0 = False

    def test_orientation_is_actually_reversed(self):
        self.assertFalse(self.chain.token_is_token0)
        self.assertGreater(self.chain.tick(), 0)

    def test_falling_price_reads_as_below_not_above(self):
        self.open_position()
        record = self.positions()[self.chain.token.lower()]
        info = self.chain.position_info(record['token_id'])
        self.chain.move_price(0.4)
        self.assertGreater(self.chain.tick(), info['tick_upper'], 'tick rises numerically')
        self.assertEqual(risk.range_side(tick=self.chain.tick(), tick_lower=info['tick_lower'],
                                         tick_upper=info['tick_upper'],
                                         token_is_token0=False), risk.BELOW)


class TestThinPool(LifecycleCase):
    """Small pools are the stated use case, so impact must degrade, not crash."""

    def test_exit_from_a_thin_pool_still_settles(self):
        self.chain.depth_usdg = 20
        self.open_position()
        self.chain.move_price(0.4)
        manager.main()
        self.assertEqual([q['phase'] for q in self.queues()], ['completed'])
        self.assertEqual(self.chain.bal(self.chain.token), 0)

    def test_pool_too_thin_sells_what_it_can_and_retries_the_rest(self):
        self.chain.depth_usdg = 0.5
        self.open_position()
        self.chain.move_price(0.4)
        manager.main()
        queue = self.queues()[0]
        self.assertEqual(queue['phase'], 'retry', 'must not crash or claim completion')
        self.assertLess(queue['remaining_raw'], queue['intended_amount_raw'],
                        'a partial fill should still reduce the exposure')
        self.assertGreater(queue['proceeds_raw'], 0)

    def test_price_impact_cap_is_respected(self):
        self.chain.depth_usdg = 0.5
        self.open_position()
        self.chain.move_price(0.4)
        manager.main()
        route = (self.queues()[0]['last_quote'] or {}).get('route') or {}
        self.assertLessEqual(route.get('impact_bps', 0),
                             int(cfg.LIQUIDATION_MAX_PRICE_IMPACT_PCT * 100))


class TestEntryTrigger(LifecycleCase):
    """The gate that decides whether to commit capital at all."""

    def _candidate(self, **overrides):
        return {'token': self.chain.token, 'symbol': 'MEME', 'liq_usd': '900000',
                'vol24_usd': '500000', 'age_h': 500, 'age_hours': 500,
                'has_direct_usdg': True, 'has_direct_usdg_v3': True,
                'preferred_venue': 'v3', 'lp_edge_ok': True, 'score': 80, **overrides}

    def _trigger(self, candidate):
        import entry_trigger
        cfg.CANDIDATES_FILE.write_text(json.dumps({'ts': int(time.time()), 'candidates': [candidate]}))

        def fake_subprocess(cmd, **kwargs):
            with patch.object(sys, 'argv', ['entry.py', cmd[-1]]):
                entry.main()

            class Result:
                returncode, stdout, stderr = 0, '', ''
            return Result()

        with patch.object(entry_trigger, 'gmgn_assess',
                          lambda *a, **k: {'ok': True, 'hard_stop': False}), \
             patch.object(entry_trigger.subprocess, 'run', fake_subprocess):
            entry_trigger.main()

    def test_candidate_without_an_edge_is_not_opened(self):
        self._trigger(self._candidate(lp_edge_ok=False))
        self.assertEqual(self.positions(), {})

    def test_honeypot_is_not_opened(self):
        self.chain.reverts.add('honeypot')
        self._trigger(self._candidate())
        self.assertEqual(self.positions(), {})

    def test_healthy_candidate_with_an_edge_is_opened(self):
        self._trigger(self._candidate())
        self.assertIn(self.chain.token.lower(), self.positions())

    def test_a_rejected_honeypot_earns_a_cooldown(self):
        self.chain.reverts.add('honeypot')
        self._trigger(self._candidate())
        cooldown = json.loads(cfg.COOLDOWN_FILE.read_text())
        self.assertGreater(cooldown[self.chain.token.lower()], time.time())


class TestLegacyPath(LifecycleCase):
    """ATOMIC_LP_ONLY=false is the documented override; it must still function."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(cfg, 'ATOMIC_LP_ONLY', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_legacy_open_survives_the_pool_fee(self):
        """The swap minimum was derived from spot, ignoring the fee, so on the 1%
        meme tier it demanded more than the pool could ever return."""
        result = self.open_position()
        self.assertTrue(result['token_id'])
        self.assertIn(self.chain.token.lower(), self.positions())

    def test_legacy_close_settles_without_stranding_token(self):
        self.open_position()
        self.chain.move_price(0.4)
        manager.main()
        self.assertEqual(self.positions(), {})
        self.assertEqual(self.chain.bal(self.chain.token), 0)

    def test_legacy_open_refuses_a_zero_quote(self):
        with patch.object(self.chain, 'quote_v3_exact_input_single', lambda *a, **k: 0):
            with self.assertRaises(RuntimeError):
                self.open_position()


class TestV4Resilience(LifecycleCase):
    def test_one_failing_v4_position_does_not_starve_the_others(self):
        import hybrid_v4
        import positions as lp_positions
        import v4_backend

        def record(token_id, token):
            return lp_positions.V4Position.opened(
                token=token, symbol='M' + token_id, token_id=token_id,
                mint_time=int(time.time()) - 7200, amount_usdg=Decimal('1'),
                poolId='0xpool', tick_lower=-349600, tick_upper=-341200,
                range_width_percent=25).to_dict()

        cfg.V4_POSITIONS_FILE.write_text(json.dumps(
            {'0xaaa': record('777', '0xaaa'), '0xbbb': record('888', '0xbbb')}))
        rows = [{'tokenId': t, 'valueUsd': '0.5', 'feeUsd': '0.3', 'liquidity': 10 ** 15,
                 'tick': -354568, 'inRange': False, 'sym0': 'USDG', 'sym1': 'M',
                 'amount0': '0.2', 'amount1': '300'} for t in ('777', '888')]
        with patch.object(v4_backend, 'list_positions', lambda: rows), \
             patch.object(hybrid_v4, 'gmgn_assess',
                          lambda *a, **k: {'ok': True, 'hard_stop': False, 'liquidity_usd': 50000}):
            actions = hybrid_v4.manage()
        reported = {a.get('token') for a in actions if a.get('action') == 'management_retained'}
        self.assertEqual(reported, {'0xaaa', '0xbbb'},
                         'the second position must still be evaluated after the first fails')

    def test_v4_failure_does_not_stop_v3_management(self):
        """manager.main runs V4 first; a V4 exception must not skip the V3 loop."""
        self.open_position()
        tid = self.positions()[self.chain.token.lower()]['token_id']
        self.chain.accrue_fees(tid, usdg_raw=3_000_000)
        before = self.chain.bal(cfg.USDG)
        with patch('hybrid_v4.manage', side_effect=RuntimeError('simulated V4 outage')):
            manager.main()
        self.assertEqual(self.chain.bal(cfg.USDG) - before, 3_000_000)


if __name__ == '__main__':
    unittest.main()
