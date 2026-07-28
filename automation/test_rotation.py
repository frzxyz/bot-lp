import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import manager, rotation, config as cfg

class RotationTests(unittest.TestCase):
    def test_atomic_v4_preflight_fails_without_broadcast(self):
        import atomic_v4_backend
        with patch.object(atomic_v4_backend,'_run',side_effect=RuntimeError('simulation failed')):
            with self.assertRaisesRegex(RuntimeError,'simulation failed'):
                atomic_v4_backend.capability_preflight('0x0000000000000000000000000000000000000001',20_000_000)

    def test_exact_delta_excludes_preexisting_wallet_assets(self):
        # USDG 100 existed before; token inventory 50 existed before. Only collected 30 is sold.
        balances={cfg.USDG:[100_000_000,100_000_000,125_000_000,125_000_000], '0xtoken':[50,80,80,50]}
        def bal(token): return balances[token].pop(0) if len(balances[token])>1 else balances[token][0]
        infos=[{'liquidity':10},{'liquidity':0}]
        sent=[]
        def swap(token,fee,amount,minimum): sent.append(amount); return {'hash':'0x3','status':1}
        with tempfile.TemporaryDirectory() as d, patch.object(cfg,'ATOMIC_LP_ONLY',False), patch.object(manager.cfg,'ATOMIC_LP_ONLY',False), patch.object(cfg,'PENDING_LIQUIDATIONS_FILE',Path(d)/'liq.json'), patch.object(manager.c,'erc20_balance',side_effect=bal), \
             patch.object(manager,'position_info',side_effect=lambda tid: infos.pop(0) if infos else (_ for _ in ()).throw(RuntimeError('gone'))), \
             patch.object(manager,'decrease_liquidity',return_value={'hash':'0x1','status':1}), \
             patch.object(manager,'collect_fees',return_value={'hash':'0x2','status':1}), \
             patch.object(manager.liquidation,'_route_quotes',side_effect=lambda token,amount,rec:[{'venue':'kyber','fee':0,'amount_out_raw':amount*25000000//30}]), \
             patch.object(manager,'swap_token_to_usdg_kyber',side_effect=swap), \
             patch.object(manager,'burn_nft',return_value={'hash':'0x4','status':1}):
            out=manager.settle_v3_rotation({'token_id':7,'token':'0xtoken','fee':3000})
        self.assertEqual(sent,[30])
        self.assertEqual(out['exact_proceeds_raw'],25_000_000)
        self.assertTrue(out['closure_confirmed'])

    def test_phase_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d, patch.object(cfg,'V3_TO_V4_PENDING_FILE',Path(d)/'pending.json'):
            ready={'token':'0xtarget'}; pos={'token_id':1,'token':'0xsource'}
            rec=rotation.prepare_pending(ready,pos,{'safe':True},now=10)
            again=rotation.prepare_pending(ready,pos,{'safe':True},now=20)
            self.assertEqual(again['created_at'],10)
            rotation.update_pending(rec,'settled',exact_proceeds_raw=123)
            self.assertEqual(rotation.load_pending()['phase'],'settled')
            self.assertEqual(rotation.load_pending()['exact_proceeds_raw'],123)

    def test_prepared_stale_target_is_atomically_replaced(self):
        with tempfile.TemporaryDirectory() as d, patch.object(cfg,'V3_TO_V4_PENDING_FILE',Path(d)/'pending.json'), patch.object(cfg,'V3_TO_V4_EXPIRY_SECONDS',100):
            pos={'token_id':1,'token':'0xsource'}
            old=rotation.prepare_pending({'token':'0xold'},pos,{'safe':True},now=10)
            fresh=rotation.prepare_pending({'token':'0xnew'},pos,{'safe':True},now=20)
            self.assertEqual(old['target']['token'],'0xold')
            self.assertEqual(fresh['target']['token'],'0xnew')
            self.assertEqual(fresh['created_at'],20)

    def test_nonprepared_target_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as d, patch.object(cfg,'V3_TO_V4_PENDING_FILE',Path(d)/'pending.json'):
            pos={'token_id':1,'token':'0xsource'}
            rec=rotation.prepare_pending({'token':'0xold'},pos,{'safe':True},now=10)
            rotation.update_pending(rec,'closing')
            same=rotation.prepare_pending({'token':'0xnew'},pos,{'safe':True},now=20)
            self.assertEqual(same['phase'],'closing')
            self.assertEqual(same['target']['token'],'0xold')

    def test_settled_pending_blocks_unrelated_entry(self):
        import hybrid_v4
        with patch.object(rotation,'load_pending',return_value={'phase':'settled'}):
            with self.assertRaisesRegex(RuntimeError,'unrelated entry blocked'):
                hybrid_v4.enter({'token':'0x1'})

if __name__=='__main__': unittest.main()
