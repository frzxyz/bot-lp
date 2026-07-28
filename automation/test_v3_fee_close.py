import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import config as cfg
import v3_fee_close as w

class FeeCloseWalTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.ps=[patch.object(cfg,'V3_FEE_CLOSE_WAL_FILE',Path(self.t.name)/'w.json'),patch.object(cfg,'PENDING_LIQUIDATIONS_FILE',Path(self.t.name)/'q.json')]
  [x.start() for x in self.ps]
 def tearDown(self): [x.stop() for x in self.ps]; self.t.cleanup()
 def test_write_before_collect_exact_delta_queue_then_burn(self):
  balances={'tok':[50,57],cfg.USDG:[9]}; calls=[]
  with patch.object(w.c,'erc20_balance',side_effect=lambda t:balances[t].pop(0)):
   r=w.prepare(1,'tok','T',18,3000,now=1)
   def collect(_): calls.append(('collect',cfg.V3_FEE_CLOSE_WAL_FILE.exists())); return {'hash':'c','status':1}
   def burn(_): calls.append(('burn',True)); return {'hash':'b','status':1}
   r=w.advance(r,collect,burn,now=2)
  self.assertEqual(r['phase'],'completed'); self.assertEqual(calls[0],('collect',True))
  q=w.liquidation.load()[0]; self.assertEqual(q['intended_amount_raw'],7); self.assertEqual(q['protected_preexisting_raw'],50); self.assertEqual(q['venue_candidates'],['kyber'])
 def test_collect_failure_retained_for_retry_no_burn(self):
  balances={'tok':[50],cfg.USDG:[9]}
  with patch.object(w.c,'erc20_balance',side_effect=lambda t:balances[t][0]): r=w.prepare(2,'tok','T',18,3000,now=1)
  with self.assertRaises(RuntimeError): w.advance(r,lambda _: {'hash':'x','status':0},lambda _: self.fail('burn'),now=2)
  self.assertEqual(w._load()[0]['phase'],'prepared')

if __name__=='__main__':unittest.main()
