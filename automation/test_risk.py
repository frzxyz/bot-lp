import math
import random
import unittest
from decimal import Decimal
from unittest.mock import patch

import risk


class ConversionTest(unittest.TestCase):
    def test_token_as_token0_keeps_tick_order(self):
        lo, hi = risk.range_prices_from_ticks(-1000, 1000, token_is_token0=True)
        self.assertLess(lo, hi)
        self.assertAlmostEqual(lo, risk.raw_price(-1000))
        self.assertAlmostEqual(hi, risk.raw_price(1000))

    def test_usdg_as_token0_inverts_and_swaps_bounds(self):
        """An increasing tick means a falling token price when USDG sorts first."""
        lo, hi = risk.range_prices_from_ticks(-1000, 1000, token_is_token0=False)
        self.assertLess(lo, hi)
        self.assertAlmostEqual(lo, 1 / risk.raw_price(1000))
        self.assertAlmostEqual(hi, 1 / risk.raw_price(-1000))

    def test_range_side_is_orientation_aware(self):
        """The same tick above the range is token-heavy or stable-heavy by ordering."""
        args = dict(tick=2000, tick_lower=-1000, tick_upper=1000)
        self.assertEqual(risk.range_side(**args, token_is_token0=True), risk.ABOVE)
        self.assertEqual(risk.range_side(**args, token_is_token0=False), risk.BELOW)
        self.assertEqual(
            risk.range_side(tick=0, tick_lower=-1000, tick_upper=1000, token_is_token0=True),
            risk.IN_RANGE)


class CompositionTest(unittest.TestCase):
    def test_exposure_spans_full_stable_to_full_token(self):
        self.assertAlmostEqual(risk.token_exposure_pct(0.5, 0.5, 2.0), 100.0, places=6)
        self.assertAlmostEqual(risk.token_exposure_pct(2.0, 0.5, 2.0), 0.0, places=6)
        mid = risk.token_exposure_pct(1.0, 0.5, 2.0)
        self.assertTrue(0 < mid < 100)

    def test_below_range_position_is_entirely_token(self):
        token, stable = risk.position_amounts(0.4, 0.5, 2.0)
        self.assertGreater(token, 0)
        self.assertAlmostEqual(stable, 0.0, places=12)

    def test_impermanent_loss_is_zero_at_entry_and_negative_after_a_move(self):
        self.assertAlmostEqual(risk.impermanent_loss_pct(1.0, 1.0, 0.5, 2.0), 0.0, places=9)
        self.assertLess(risk.impermanent_loss_pct(1.0, 0.7, 0.5, 2.0), 0.0)
        self.assertLess(risk.impermanent_loss_pct(1.0, 1.4, 0.5, 2.0), 0.0)

    def test_narrower_range_loses_more_on_the_same_move(self):
        wide = risk.impermanent_loss_pct(1.0, 0.8, 0.5, 2.0)
        narrow = risk.impermanent_loss_pct(1.0, 0.8, 0.9, 1.1)
        self.assertLess(narrow, wide)

    def test_nav_matches_a_hand_computed_position(self):
        """USDG is token1 here, so the pool price is already USDG per token."""
        nav = risk.nav_usdg(tick=0, tick_lower=-1000, tick_upper=1000, liquidity=10**12,
                            token_is_token0=True, stable_decimals=6)
        lower, upper = risk.range_prices_from_ticks(-1000, 1000, True)
        expected = risk.position_value(1.0, lower, upper, 10**12) / 10**6
        self.assertAlmostEqual(float(nav), expected, places=6)


class VolatilityTest(unittest.TestCase):
    def test_vol_follows_square_root_of_time(self):
        """A move of d over dt must read the same as d*sqrt(2) over 2*dt."""
        fast = [{'timestamp': i * 300, 'tick': i * 100} for i in range(13)]
        slow = [{'timestamp': i * 600, 'tick': round(i * 100 * math.sqrt(2))} for i in range(7)]
        self.assertAlmostEqual(risk.realized_vol_pct_per_hour(fast, now=3600),
                               risk.realized_vol_pct_per_hour(slow, now=3600), places=1)

    def test_subsampling_a_random_walk_preserves_the_estimate(self):
        """Dropped scheduler ticks must not read as a volatility spike."""
        rng = random.Random(7)
        tick, fine = 0, [{'timestamp': 0, 'tick': 0}]
        for i in range(1, 241):
            tick += rng.choice((-60, 60))
            fine.append({'timestamp': i * 15, 'tick': tick})
        dense = risk.realized_vol_pct_per_hour(fine, now=3600)
        sparse = risk.realized_vol_pct_per_hour(fine[::4], now=3600)
        self.assertLess(abs(dense - sparse) / dense, 0.25)

    def test_unknown_history_returns_none(self):
        self.assertIsNone(risk.realized_vol_pct_per_hour([], now=0))
        self.assertIsNone(risk.realized_vol_pct_per_hour([{'timestamp': 0, 'tick': 5}], now=0))

    def test_width_scales_with_volatility_and_clamps(self):
        with patch.object(risk.cfg, 'RANGE_SIGMAS', Decimal('2')), \
             patch.object(risk.cfg, 'RANGE_HORIZON_HOURS', Decimal('4')), \
             patch.object(risk.cfg, 'RANGE_MIN_PCT', Decimal('15')), \
             patch.object(risk.cfg, 'RANGE_MAX_PCT', Decimal('80')):
            self.assertAlmostEqual(risk.width_pct_for_vol(5.0), 20.0)
            self.assertEqual(risk.width_pct_for_vol(0.1), 15.0)
            self.assertEqual(risk.width_pct_for_vol(100.0), 80.0)
        self.assertIsNone(risk.width_pct_for_vol(None))


class EconomicsTest(unittest.TestCase):
    def test_capital_efficiency_rises_as_range_narrows(self):
        self.assertGreater(risk.capital_efficiency(10), risk.capital_efficiency(50))
        self.assertIsNone(risk.capital_efficiency(0))
        self.assertIsNone(risk.capital_efficiency(100))

    def test_expected_fee_scales_with_volume_and_share(self):
        base = dict(vol24_usd=1_000_000, liquidity_usd=500_000, fee_ppm=10_000,
                    position_usdg=100, width_pct=25, horizon_hours=24)
        fee = risk.expected_fee_usdg(**base)
        self.assertGreater(fee, 0)
        self.assertGreater(risk.expected_fee_usdg(**{**base, 'vol24_usd': 2_000_000}), fee)
        self.assertLess(risk.expected_fee_usdg(**{**base, 'liquidity_usd': 5_000_000}), fee)
        self.assertIsNone(risk.expected_fee_usdg(**{**base, 'liquidity_usd': 0}))

    def test_entry_requires_fees_to_beat_il_plus_cost_with_margin(self):
        with patch.object(risk.cfg, 'ENTRY_EDGE_MARGIN', Decimal('1.5')):
            ok, _ = risk.entry_is_economic(expected_fee_usdg=Decimal('3'),
                                           expected_il_usdg=Decimal('-1'),
                                           execution_cost_usdg=Decimal('1'))
            self.assertTrue(ok)
            ok, _ = risk.entry_is_economic(expected_fee_usdg=Decimal('2.9'),
                                           expected_il_usdg=Decimal('-1'),
                                           execution_cost_usdg=Decimal('1'))
            self.assertFalse(ok)

    def test_unknown_economics_fails_closed(self):
        self.assertFalse(risk.entry_is_economic(expected_fee_usdg=None, expected_il_usdg=Decimal('1'),
                                                execution_cost_usdg=Decimal('1'))[0])

    def test_gas_budget_rejects_oversized_cost(self):
        with patch.object(risk.cfg, 'MAX_TX_COST_PCT', Decimal('2')):
            self.assertTrue(risk.gas_within_budget(Decimal('1'), Decimal('100')))
            self.assertFalse(risk.gas_within_budget(Decimal('3'), Decimal('100')))
            self.assertFalse(risk.gas_within_budget(Decimal('1'), Decimal('0')))


def policy(**overrides):
    base = dict(stop_loss_pct=Decimal('10'), max_drawdown_pct=Decimal('12'),
                oor_below_seconds=900, oor_above_seconds=21600,
                exposure_exit_pct=Decimal('92'))
    return risk.ExitPolicy(**{**base, **overrides})


def decide(**overrides):
    args = dict(nav_usdg=Decimal('100'), principal_usdg=Decimal('100'),
                peak_nav_usdg=Decimal('100'), side=risk.IN_RANGE, exposure_pct=50.0,
                oor_elapsed_seconds=0, policy=policy())
    return risk.exit_decision(**{**args, **overrides})


class ExitDecisionTest(unittest.TestCase):
    def test_healthy_in_range_position_holds(self):
        self.assertEqual(decide(), (False, None))

    def test_nav_stop_loss_against_principal(self):
        self.assertEqual(decide(nav_usdg=Decimal('89'))[1], 'nav_stop_loss')

    def test_round_trip_from_a_peak_is_caught_by_drawdown(self):
        """A position up 100% then back to break-even never trips an entry-anchored stop."""
        self.assertEqual(decide(nav_usdg=Decimal('200'), peak_nav_usdg=Decimal('200')), (False, None))
        self.assertEqual(decide(nav_usdg=Decimal('101'), peak_nav_usdg=Decimal('200'))[1],
                         'nav_drawdown')

    def test_below_range_exits_fast_and_above_range_waits(self):
        """Below range the position is 100% memecoin; above it is 100% USDG."""
        self.assertEqual(decide(side=risk.BELOW, exposure_pct=100.0,
                                oor_elapsed_seconds=1000)[1], 'token_exposure')
        self.assertEqual(decide(side=risk.ABOVE, exposure_pct=0.0,
                                oor_elapsed_seconds=1000), (False, None))
        self.assertEqual(decide(side=risk.ABOVE, exposure_pct=0.0,
                                oor_elapsed_seconds=21600)[1], 'out_of_range_above')

    def test_below_range_timer_fires_before_full_conversion(self):
        self.assertEqual(decide(side=risk.BELOW, exposure_pct=80.0,
                                oor_elapsed_seconds=900)[1], 'out_of_range_below')
        self.assertEqual(decide(side=risk.BELOW, exposure_pct=80.0,
                                oor_elapsed_seconds=899), (False, None))


class DumpDetectionTest(unittest.TestCase):
    def test_short_window_catches_a_dump_without_an_hour_of_history(self):
        history = [{'t': 0, 'price': '1.0', 'usdg_pool': 1000},
                   {'t': 300, 'price': '0.8', 'usdg_pool': 1000}]
        hit, reason = risk.dump_detected(history, now=300, window_seconds=600,
                                         price_drop_pct=15, liquidity_drop_pct=20)
        self.assertTrue(hit)
        self.assertEqual(reason, 'fast_dump')

    def test_liquidity_drain_is_detected(self):
        history = [{'t': 0, 'price': '1.0', 'usdg_pool': 1000},
                   {'t': 300, 'price': '1.0', 'usdg_pool': 700}]
        self.assertEqual(risk.dump_detected(history, now=300, window_seconds=600,
                                            price_drop_pct=15, liquidity_drop_pct=20)[1],
                         'fast_liquidity_drain')

    def test_single_sample_cannot_trigger(self):
        history = [{'t': 0, 'price': '1.0', 'usdg_pool': 1000}]
        self.assertEqual(risk.dump_detected(history, now=0, window_seconds=600,
                                            price_drop_pct=15, liquidity_drop_pct=20),
                         (False, None))

    def test_samples_outside_the_window_are_ignored(self):
        history = [{'t': 0, 'price': '1.0', 'usdg_pool': 1000},
                   {'t': 5000, 'price': '0.5', 'usdg_pool': 1000}]
        self.assertEqual(risk.dump_detected(history, now=5000, window_seconds=600,
                                            price_drop_pct=15, liquidity_drop_pct=20),
                         (False, None))


if __name__ == '__main__':
    unittest.main()
