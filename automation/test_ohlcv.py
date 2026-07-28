"""Candle-derived volatility, and the selection gap it closes.

The behaviour under test is not "we can parse JSON" but the consequence: a
first-seen candidate used to be unmeasurable and therefore always rejected, and
these prove it is measurable on the first scan while an unavailable feed still
fails closed.
"""
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import ohlcv
import risk as lp_risk

POOL = '0x' + 'ab' * 20


def candles(n=72, *, step=300, start=1_700_000_000, price=1.0, drift=0.0, wiggle=0.0):
    """Synthetic ohlcv_list rows: [ts, open, high, low, close, volume]."""
    rows = []
    p = price
    for i in range(n):
        p *= (1 + drift)
        close = p * (1 + (wiggle if i % 2 else -wiggle))
        rows.append([start + i * step, close, close, close, close, 1000])
    return rows


class OhlcvCase(unittest.TestCase):
    def setUp(self):
        ohlcv._cache.clear()
        self.addCleanup(ohlcv._cache.clear)

    def serve(self, rows, status=200):
        class R:
            status_code = status

            def json(self_inner):
                return {'data': {'attributes': {'ohlcv_list': rows}}}
        return patch.object(ohlcv.requests, 'get', lambda *a, **k: R())


class TestParsing(OhlcvCase):
    def test_candles_become_tick_rows_in_time_order(self):
        with self.serve(list(reversed(candles(10)))):
            rows = ohlcv.candle_rows(POOL, now=1_700_003_000)
        self.assertEqual(len(rows), 10)
        self.assertEqual([r['timestamp'] for r in rows], sorted(r['timestamp'] for r in rows))
        self.assertTrue(all(isinstance(r['tick'], int) for r in rows))

    def test_a_price_move_maps_to_the_expected_tick_distance(self):
        """Tick space is log(1.0001); a 10% move is ~953 ticks."""
        with self.serve([[1, 1, 1, 1, 1.0, 0], [2, 1, 1, 1, 1.10, 0]]):
            rows = ohlcv.candle_rows(POOL, now=10)
        self.assertAlmostEqual(rows[1]['tick'] - rows[0]['tick'],
                               math.log(1.10) / math.log(1.0001), delta=2)

    def test_gaps_in_the_feed_are_dropped_not_treated_as_prices(self):
        with self.serve([[1, 1, 1, 1, 1.0, 0], [2, 0, 0, 0, 0.0, 0], [3, 1, 1, 1, 1.05, 0]]):
            rows = ohlcv.candle_rows(POOL, now=10)
        self.assertEqual(len(rows), 2)

    def test_malformed_rows_are_skipped(self):
        with self.serve([[1, 1, 1, 1, 1.0, 0], 'nonsense', [2, 1], [3, 1, 1, 1, 'x', 0]]):
            rows = ohlcv.candle_rows(POOL, now=10)
        self.assertEqual(len(rows), 1)

    def test_a_failed_request_yields_no_rows_rather_than_raising(self):
        with self.serve([], status=503):
            self.assertEqual(ohlcv.candle_rows(POOL, now=10), [])
        with patch.object(ohlcv.requests, 'get', side_effect=RuntimeError('network down')):
            ohlcv._cache.clear()
            self.assertEqual(ohlcv.candle_rows(POOL, now=10), [])

    def test_results_are_cached_within_the_window(self):
        calls = []

        class R:
            status_code = 200

            def json(self_inner):
                calls.append(1)
                return {'data': {'attributes': {'ohlcv_list': candles(10)}}}
        with patch.object(ohlcv.requests, 'get', lambda *a, **k: R()):
            ohlcv.candle_rows(POOL, now=1_700_003_000)
            ohlcv.candle_rows(POOL, now=1_700_003_010)
        self.assertEqual(len(calls), 1)


class TestVolatility(OhlcvCase):
    def test_a_calm_series_reads_lower_than_a_choppy_one(self):
        now = 1_700_000_000 + 72 * 300
        with self.serve(candles(72, wiggle=0.001)):
            calm = ohlcv.realized_vol_pct_per_hour(POOL, now=now)
        ohlcv._cache.clear()
        with self.serve(candles(72, wiggle=0.05)):
            choppy = ohlcv.realized_vol_pct_per_hour(POOL, now=now)
        self.assertIsNotNone(calm)
        self.assertIsNotNone(choppy)
        self.assertLess(calm, choppy)

    def test_too_few_candles_fails_closed(self):
        with self.serve(candles(2)):
            self.assertIsNone(ohlcv.realized_vol_pct_per_hour(POOL, now=1_700_001_000))

    def test_no_feed_fails_closed(self):
        with self.serve([]):
            self.assertIsNone(ohlcv.realized_vol_pct_per_hour(POOL, now=1_700_001_000))


class TestBestEffort(OhlcvCase):
    def test_candles_close_the_first_scan_blind_spot(self):
        """A first-seen candidate has one tick snapshot, which yields nothing."""
        one_snapshot = [{'timestamp': 1_700_021_600, 'tick': 100}]
        self.assertIsNone(lp_risk.realized_vol_pct_per_hour(one_snapshot, 1_700_021_600))

        now = 1_700_000_000 + 72 * 300
        with self.serve(candles(72, wiggle=0.01)):
            vol, source = ohlcv.best_effort_vol(POOL, one_snapshot, now)
        self.assertIsNotNone(vol, 'candles must supply what the snapshot cannot')
        self.assertEqual(source, 'ohlcv')

    def test_tick_history_is_used_when_candles_are_unavailable(self):
        rows = [{'timestamp': 1_700_000_000 + i * 900, 'tick': 100 + i * 30} for i in range(6)]
        now = 1_700_000_000 + 6 * 900
        with self.serve([]):
            vol, source = ohlcv.best_effort_vol(POOL, rows, now)
        self.assertIsNotNone(vol)
        self.assertEqual(source, 'tick_history')

    def test_no_pool_and_no_history_reports_unavailable(self):
        vol, source = ohlcv.best_effort_vol(None, [], 1_700_000_000)
        self.assertIsNone(vol)
        self.assertEqual(source, 'unavailable')

    def test_a_missing_pool_address_still_uses_tick_history(self):
        rows = [{'timestamp': 1_700_000_000 + i * 900, 'tick': 100 + i * 10} for i in range(6)]
        vol, source = ohlcv.best_effort_vol(None, rows, 1_700_000_000 + 6 * 900)
        self.assertIsNotNone(vol)
        self.assertEqual(source, 'tick_history')


class TestEdgeRatio(unittest.TestCase):
    def test_ratio_expresses_margin_over_breakeven(self):
        ok, edge = lp_risk.entry_is_economic(expected_fee_usdg='3', expected_il_usdg='1',
                                             execution_cost_usdg='0')
        self.assertTrue(ok)
        self.assertAlmostEqual(lp_risk.edge_ratio(edge), 2.0, places=6)

    def test_below_breakeven_ranks_under_one(self):
        _, edge = lp_risk.entry_is_economic(expected_fee_usdg='1', expected_il_usdg='1',
                                            execution_cost_usdg='0')
        self.assertLess(lp_risk.edge_ratio(edge), 1.0)

    def test_incomputable_economics_sort_last_not_first(self):
        _, edge = lp_risk.entry_is_economic(expected_fee_usdg=None, expected_il_usdg='1',
                                            execution_cost_usdg='0')
        self.assertEqual(lp_risk.edge_ratio(edge), 0.0)
        self.assertEqual(lp_risk.edge_ratio(None), 0.0)
        self.assertEqual(lp_risk.edge_ratio({'expected_fee_usdg': '1', 'required_usdg': '0'}), 0.0)


class TestRanking(unittest.TestCase):
    def test_economics_outrank_the_fomo_score(self):
        import scanner

        strong = {'gmgn_ok': True, 'lp_edge_ok': True, 'lp_edge_ratio': 3.0, 'score': 10,
                  'liq_usd': '900000', 'source': 'dexscreener', 'token': 'a'}
        hot = {'gmgn_ok': True, 'lp_edge_ok': True, 'lp_edge_ratio': 1.1, 'score': 99,
               'liq_usd': '900000', 'source': 'dexscreener', 'token': 'b'}
        rejected = {'gmgn_ok': True, 'lp_edge_ok': False, 'lp_edge_ratio': 9.0, 'score': 99,
                    'liq_usd': '900000', 'source': 'dexscreener', 'token': 'c'}
        rows = [hot, rejected, strong]
        rows.sort(key=scanner.candidate_sort_key)
        self.assertEqual([r['token'] for r in rows], ['a', 'b', 'c'],
                         'best edge first, gate failures last regardless of ratio or heat')

    def test_gmgn_rejection_outranks_every_economic_signal(self):
        import scanner

        unsafe = {'gmgn_ok': False, 'lp_edge_ok': True, 'lp_edge_ratio': 9.0, 'score': 99,
                  'liq_usd': '900000', 'token': 'unsafe'}
        safe = {'gmgn_ok': True, 'lp_edge_ok': True, 'lp_edge_ratio': 1.01, 'score': 1,
                'liq_usd': '900000', 'token': 'safe'}
        rows = [unsafe, safe]
        rows.sort(key=scanner.candidate_sort_key)
        self.assertEqual([r['token'] for r in rows], ['safe', 'unsafe'])

    def test_a_hotter_score_no_longer_wins_on_its_own(self):
        import scanner

        calm = {'gmgn_ok': True, 'lp_edge_ok': True, 'lp_edge_ratio': 2.0, 'score': 5,
                'liq_usd': '900000', 'token': 'calm'}
        hot = {'gmgn_ok': True, 'lp_edge_ok': True, 'lp_edge_ratio': 2.0, 'score': 95,
               'liq_usd': '900000', 'token': 'hot'}
        rows = [calm, hot]
        rows.sort(key=scanner.candidate_sort_key)
        # Equal edge: the score is allowed to decide, and only then.
        self.assertEqual([r['token'] for r in rows], ['hot', 'calm'])


if __name__ == '__main__':
    unittest.main()
