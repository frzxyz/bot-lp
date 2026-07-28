import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import liquidation, config as cfg

class LiquidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.p=patch.object(cfg,'PENDING_LIQUIDATIONS_FILE',Path(self.tmp.name)/'q.json'); self.p.start()
    def tearDown(self): self.p.stop(); self.tmp.cleanup()
    def q(self,**kw):
        return liquidation.enqueue('0xtoken','TOK',18,kw.get('amount',100),kw.get('protected',50),3000,source_token_id=kw.get('tid',1),now=kw.get('now',100))
    def test_enqueue_before_send_and_preexisting_excluded(self):
        r=self.q(); seen=[]; bals={'0xtoken':[150,50],cfg.USDG:[10,100]}
        def bal(t): return bals[t].pop(0)
        def send(t,f,a,m):
            seen.append((Path(cfg.PENDING_LIQUIDATIONS_FILE).exists(),a,m)); return {'hash':'0x1','status':1}
        with patch.object(liquidation.c,'erc20_balance',side_effect=bal): out=liquidation.attempt(r,now=100,quote_fn=lambda *x:100,send_fn=send)
        self.assertEqual(seen[0][0:2],(True,100)); self.assertGreater(seen[0][2],0); self.assertEqual(out['phase'],'completed')
    def test_partial_and_restart_reconciliation_no_double_sell(self):
        r=self.q(); bals={'0xtoken':[150,110],cfg.USDG:[0,40]}
        with patch.object(liquidation.c,'erc20_balance',side_effect=lambda t:bals[t].pop(0)):
            out=liquidation.attempt(r,now=100,quote_fn=lambda *x:100,send_fn=lambda *x:{'hash':'x','status':1})
        self.assertEqual(out['remaining_raw'],60)
        # Restart sees another 10 sold by uncertain prior tx; only 50 is eligible next.
        bals={'0xtoken':[100,50],cfg.USDG:[50,100]}; sent=[]
        with patch.object(liquidation.c,'erc20_balance',side_effect=lambda t:bals[t].pop(0)):
            out=liquidation.attempt(liquidation.load()[0],now=160,quote_fn=lambda *x:50,send_fn=lambda t,f,a,m:(sent.append(a) or {'hash':'y','status':1}))
        self.assertEqual(sent,[50]); self.assertEqual(out['phase'],'completed')
    def test_missing_quote_fails_closed_backoff_and_expiry_retained(self):
        r=self.q(); bals={'0xtoken':[150],cfg.USDG:[0]}; sent=[]
        with patch.object(liquidation.c,'erc20_balance',side_effect=lambda t:bals[t][0]):
            out=liquidation.attempt(r,now=100,quote_fn=lambda *x:(_ for _ in ()).throw(RuntimeError('no quote')),send_fn=lambda *x:sent.append(1))
        self.assertFalse(sent); self.assertEqual(out['phase'],'retry'); self.assertEqual(out['next_retry'],130)
        out['expires_at']=101; out['next_retry']=101; liquidation._save_record(out)
        with patch.object(liquidation.c,'erc20_balance',side_effect=lambda t:bals[t][0]): out=liquidation.attempt(out,now=102,quote_fn=lambda *x:0)
        self.assertEqual(out['phase'],'manual_attention'); self.assertEqual(len(liquidation.load()),1)
    def test_best_route_isolates_bad_venue_and_ranks_output(self):
        rec={'venue_candidates':['kyber','v3','v4']}
        def quotes(_token,amount,_rec):
            if amount==100:
                return [{'venue':'v3','fee':3000,'amount_out_raw':95},{'venue':'v4','fee':10000,'pool_id':'p','amount_out_raw':98}]
            return [{'venue':'v3','fee':3000,'amount_out_raw':10},{'venue':'v4','fee':10000,'pool_id':'p','amount_out_raw':10}]
        with patch.object(liquidation,'_route_quotes',side_effect=quotes):
            out=liquidation._best_route('0xt',100,rec)
        self.assertEqual(out['venue'],'v4'); self.assertEqual(out['amount_out_raw'],98)

    def test_thin_best_nominal_route_is_rejected(self):
        rec={'venue_candidates':['v3','v4']}
        def quotes(_token,amount,_rec):
            if amount==100:
                return [{'venue':'v4','fee':1,'pool_id':'thin','amount_out_raw':100},{'venue':'v3','fee':3000,'amount_out_raw':90}]
            return [{'venue':'v4','fee':1,'pool_id':'thin','amount_out_raw':20},{'venue':'v3','fee':3000,'amount_out_raw':10}]
        with patch.object(liquidation,'_route_quotes',side_effect=quotes):
            out=liquidation._best_route('0xt',100,rec)
        self.assertEqual(out['venue'],'v3')

    def test_unsettled_blocks_entry(self):
        self.q()
        self.assertTrue(liquidation.has_unsettled())
    def test_kyber_generic_token_does_not_require_v3_fee(self):
        r=liquidation.enqueue('0xgeneric','GEN',8,100,0,None,source_token_id='v4-2',now=100,venue_candidates=['kyber'])
        bals={'0xgeneric':[100,0],cfg.USDG:[0,90]}
        with patch.object(liquidation.c,'erc20_balance',side_effect=lambda t:bals[t].pop(0)):
            out=liquidation.attempt(r,now=100,quote_fn=lambda *x:90,send_fn=lambda *x:{'hash':'0x2','status':1})
        self.assertEqual(out['phase'],'completed')
        self.assertEqual(out['proceeds_raw'],90)

if __name__=='__main__': unittest.main()