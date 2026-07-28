import os, unittest
from unittest.mock import patch
import atomic_v4_weth_backend as b
import config
import v4_backend

class WethSupportTests(unittest.TestCase):
    def test_config_parallel_and_default_unchanged(self):
        self.assertEqual(config.V4_DEFAULT_SETTLEMENT, 'USDG')
        self.assertEqual(config.V4_SETTLEMENT_ASSETS['WETH'].lower(), config.WETH.lower())
    def test_state_is_explicit_weth_no_native_unwrap(self):
        s=b.state_fields('0xabc')
        self.assertEqual(s['settlement_asset'],'WETH'); self.assertFalse(s['unwrap_native'])
    def test_preflight_requires_candidate_pool(self):
        with self.assertRaises(ValueError): b.capability_preflight('0x1',1,None)
    def test_read_only_runner_strips_key(self):
        seen={}
        class P:
            returncode=0; stdout='{"ok":true,"result":{}}\n'; stderr=''
        def run(*a,**kw): seen.update(kw['env']); return P()
        with patch.dict(os.environ,{'RH_WALLET_KEY':'secret'}), patch('subprocess.run',run):
            b._read_only('noop')
        self.assertNotIn('RH_WALLET_KEY',seen)
    def test_discover_pair_marks_settlement(self):
        row={'poolKey':{'hooks':v4_backend.ZERO},'fee':3000,'liquidity':'1'}
        with patch.object(v4_backend,'_run',return_value={'pools':[row]}):
            got=v4_backend.discover_pair('token',config.WETH)
        self.assertTrue(got[0]['eligible']); self.assertEqual(got[0]['settlement_asset'],config.WETH)

if __name__=='__main__': unittest.main()
