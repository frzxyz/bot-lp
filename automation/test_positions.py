"""Schema tests for position records.

These lock down the two failure modes that produced silently wrong numbers: a
creation site omitting a field, and a reader substituting a default for a field
that is genuinely absent.
"""
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
import positions as P


# Records exactly as the historical creation sites wrote them, kept verbatim so a
# future schema change cannot quietly stop reading live state on the VPS.
V3_LEGACY = {
    'token': '0xAbC', 'symbol': 'PEPE', 'decimals': 18, 'pool': '0xpool', 'fee': 10000,
    'tick_lower': -887200, 'tick_upper': 887200, 'entry_tick': 0,
    'entry_price_usdg': '0.00012345', 'token_id': 4242, 'mint_tx': '0xdead',
    'mint_time': 1700000000, 'size_usdg': '250', 'usdg_deposited': 250000000,
    'tok_deposited': 999, 'last_action_time': 1700000000, 'atomic': True,
}

V4_ENTER = {  # hybrid_v4.enter — no range fields, no isolated reserve
    'version': 'v4', 'token': '0xdef', 'symbol': 'WOJAK', 'token_id': '999',
    'poolId': '0xpool', 'tick_lower': -100, 'tick_upper': 100, 'mint_tx': '0xa',
    'swap_tx': '0xb', 'mint_time': 1700000000, 'entry_value_usd': '50',
    'principal_usdg': '50', 'last_realized_usdg': '0',
    'cumulative_realized_profit_usdg': '0', 'compounded_capital_usdg': '50',
    'peak_capital_usdg': '50', 'rebalance_count': 0, 'last_fee_usd': '0',
    'fee_baseline_ts': 1700000000,
}

V4_ROTATION = {  # hybrid_v4.enter_isolated_rotation — no peak, no realized fields
    'version': 'v4', 'token': '0xdef', 'symbol': 'WOJAK', 'token_id': '1000',
    'poolId': '0xpool', 'tick_lower': -100, 'tick_upper': 100, 'mint_tx': '0xa',
    'swap_tx': '0xb', 'mint_time': 1700000000, 'entry_value_usd': '75',
    'principal_usdg': '75', 'compounded_capital_usdg': '75',
    'isolated_strategy_reserve_usdg': '5', 'rotation_source_version': 'v3',
    'rotation_source_token': '0xold', 'rotation_source_token_id': '7',
    'exact_close_proceeds_raw': '80000000', 'range_width_percent': 25,
    'range_mode': 'normal', 'last_fee_usd': '0', 'fee_baseline_ts': 1700000000,
}


class TestMoneyAccessors(unittest.TestCase):
    def test_v3_principal_resolves_size_usdg(self):
        """The rotation bug: V3 records store capital as size_usdg, and the old
        reader looked only for the two V4 spellings."""
        self.assertEqual(P.principal_usdg(V3_LEGACY), Decimal('250'))

    def test_v3_principal_is_not_the_config_constant(self):
        old = Decimal(str(V3_LEGACY.get('entry_value_usdg',
                                        V3_LEGACY.get('entry_value_usd', cfg.POSITION_SIZE_USDG))))
        self.assertEqual(old, Decimal(str(cfg.POSITION_SIZE_USDG)))
        self.assertNotEqual(P.principal_usdg(V3_LEGACY), old)

    def test_absent_money_field_raises(self):
        with self.assertRaises(P.MissingMoneyField):
            P.principal_usdg({'token': '0x1', 'symbol': 'X'})

    def test_explicit_default_is_allowed(self):
        self.assertEqual(P.principal_usdg({}, default=Decimal('7')), Decimal('7'))

    def test_none_counts_as_absent(self):
        """JSON writers emit null for unset fields; that is not a real zero."""
        with self.assertRaises(P.MissingMoneyField):
            P.principal_usdg({'principal_usdg': None, 'size_usdg': None})

    def test_non_numeric_raises_rather_than_coercing(self):
        with self.assertRaises(P.MissingMoneyField):
            P.principal_usdg({'principal_usdg': 'not-a-number'})

    def test_v4_peak_falls_back_to_principal(self):
        self.assertEqual(P.V4Position.from_dict(V4_ROTATION).peak_capital_usdg, Decimal('75'))


class TestRoundTrip(unittest.TestCase):
    def test_unknown_keys_survive(self):
        record = dict(V3_LEGACY, oor_since=1700000900, oor_side='below',
                      expected_close_token_raw=123, some_future_field={'a': 1})
        out = P.V3Position.from_dict(record).to_dict()
        self.assertEqual(out['oor_since'], 1700000900)
        self.assertEqual(out['oor_side'], 'below')
        self.assertEqual(out['expected_close_token_raw'], 123)
        self.assertEqual(out['some_future_field'], {'a': 1})

    def test_round_trip_is_stable(self):
        once = P.V3Position.from_dict(V3_LEGACY).to_dict()
        twice = P.V3Position.from_dict(once).to_dict()
        self.assertEqual(once, twice)

    def test_v4_round_trip_is_stable(self):
        once = P.V4Position.from_dict(V4_ROTATION).to_dict()
        twice = P.V4Position.from_dict(once).to_dict()
        self.assertEqual(once, twice)

    def test_every_legacy_record_is_json_serializable(self):
        for record in (V3_LEGACY, V4_ENTER, V4_ROTATION):
            json.dumps(P.normalize(record))

    def test_no_money_value_is_lost(self):
        out = P.V4Position.from_dict(V4_ROTATION).to_dict()
        self.assertEqual(Decimal(out['principal_usdg']), Decimal('75'))
        self.assertEqual(Decimal(out['isolated_strategy_reserve_usdg']), Decimal('5'))
        self.assertEqual(out['exact_close_proceeds_raw'], '80000000')

    def test_v4_keeps_legacy_alias_populated(self):
        """Readers not yet migrated still index entry_value_usd directly."""
        out = P.V4Position.from_dict(V4_ROTATION).to_dict()
        self.assertEqual(out['entry_value_usd'], out['principal_usdg'])


class TestConstructorsAgree(unittest.TestCase):
    """The regression guard: three V4 entry paths used to emit three key sets."""

    def _v4(self, **kw):
        base = dict(token='0x1', symbol='T', token_id='5', mint_time=1, amount_usdg=Decimal('10'))
        return P.V4Position.opened(**{**base, **kw}).to_dict()

    def test_all_v4_creation_paths_emit_identical_keys(self):
        enter = self._v4()
        rotation = self._v4(isolated_strategy_reserve_usdg=Decimal('5'),
                            rotation_source_version='v3', rotation_source_token='0xold',
                            rotation_source_token_id='7', exact_close_proceeds_raw='80',
                            range_width_percent=25, range_mode='normal')
        reopen = self._v4(principal_usdg=Decimal('9'), peak_capital_usdg=Decimal('12'),
                          last_realized_usdg=Decimal('11'),
                          cumulative_realized_profit_usdg=Decimal('2'),
                          rebalance_count=3, rebalanced_from_token_id='4',
                          rebalanced_at=99, rebalance_cooldown_until=1000)
        self.assertEqual(set(enter), set(rotation))
        self.assertEqual(set(enter), set(reopen))

    def test_v4_record_covers_every_key_live_readers_use(self):
        produced = set(self._v4())
        for key in ('version', 'token', 'symbol', 'token_id', 'poolId', 'tick_lower',
                    'tick_upper', 'mint_tx', 'swap_tx', 'mint_time', 'entry_value_usd',
                    'principal_usdg', 'compounded_capital_usdg', 'peak_capital_usdg',
                    'last_realized_usdg', 'cumulative_realized_profit_usdg',
                    'isolated_strategy_reserve_usdg', 'last_fee_usd', 'fee_baseline_ts',
                    'rebalance_count', 'range_width_percent', 'range_mode',
                    'rebalance_cooldown_until'):
            self.assertIn(key, produced, f'{key} missing from constructed V4 record')

    def test_v3_constructor_seeds_peak_nav(self):
        """Without a seed the trailing stop has no reference on the first tick."""
        out = P.V3Position.opened(
            token='0x1', symbol='T', decimals=18, pool='0xp', fee=10000, token_id=1,
            tick_lower=-1, tick_upper=1, entry_tick=0, entry_price_usdg=Decimal('1'),
            size_usdg=Decimal('20'), usdg_deposited=20000000, tok_deposited=0,
            mint_tx='0xa', mint_time=1).to_dict()
        self.assertEqual(Decimal(out['peak_nav_usdg']), Decimal('20'))
        self.assertEqual(P.principal_usdg(out), Decimal('20'))

    def test_v3_constructor_records_range_width(self):
        out = P.V3Position.opened(
            token='0x1', symbol='T', decimals=18, pool='0xp', fee=10000, token_id=1,
            tick_lower=-1, tick_upper=1, entry_tick=0, entry_price_usdg=Decimal('1'),
            size_usdg=Decimal('20'), usdg_deposited=1, tok_deposited=0, mint_tx='0xa',
            mint_time=1, range_width_percent=33.5).to_dict()
        self.assertEqual(out['range_width_percent'], 33.5)


class TestNormalize(unittest.TestCase):
    def test_normalize_picks_version_by_field(self):
        self.assertEqual(P.normalize(V4_ENTER)['version'], 'v4')
        self.assertNotIn('version', P.normalize(V3_LEGACY))

    def test_normalize_backfills_without_touching_real_values(self):
        out = P.normalize(V4_ROTATION)
        self.assertEqual(Decimal(out['peak_capital_usdg']), Decimal('75'))
        self.assertEqual(Decimal(out['principal_usdg']), Decimal('75'))
        self.assertEqual(out['range_mode'], 'normal')


if __name__ == '__main__':
    unittest.main()
