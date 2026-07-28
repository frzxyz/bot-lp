import unittest
from decimal import Decimal
from unittest.mock import patch
import adaptive_v4 as p

class PolicyTest(unittest.TestCase):
 def test_distance_both_sides_and_boundaries(self):
  d,c,_=p.tick_distance(-487,0,100); self.assertLessEqual(d,5); self.assertEqual(c,'shallow')
  d,c,_=p.tick_distance(1054,0,100); self.assertGreater(d,10); self.assertEqual(c,'deep')
  self.assertEqual(p.tick_distance(50,0,100)[1],'in_range'); self.assertIsNotNone(p.tick_distance(1,0,100)[2])
 def test_timer_reset_and_reentry(self):
  x={}; self.assertEqual(p.continuous_timer(x,'deep',100),0); self.assertEqual(p.continuous_timer(x,'deep',1000),900)
  self.assertEqual(p.continuous_timer(x,'in_range',1001),0); self.assertEqual(p.continuous_timer(x,'deep',2000),0)
 def test_shallow_allow_block_and_stale(self):
  m={'fresh':True,'spot_twap_pct':2,'momentum_15m':2,'momentum_30m':3,'liquidity_drop_pct':4}
  self.assertTrue(p.evidence_gates('shallow',m,{'ok':True},True)[0])
  m['momentum_15m']=4; self.assertFalse(p.evidence_gates('shallow',m,{'ok':True},True)[0])
  m['fresh']=False; self.assertFalse(p.evidence_gates('shallow',m,{'ok':True},True)[0])
 def test_medium_stabilizing(self):
  m={'fresh':True,'spot_twap_pct':4,'momentum_15m':3,'momentum_30m':4,'liquidity_drop_pct':9}
  self.assertTrue(p.evidence_gates('medium',m,{'ok':True},True)[0]); m['momentum_30m']=2
  self.assertFalse(p.evidence_gates('medium',m,{'ok':True},True)[0])
 def test_economics_two_x(self):
  rows=[{'timestamp':0,'feeUsd':'0'},{'timestamp':10800,'feeUsd':'2'}]
  with patch.object(p.cfg,'V4_EXECUTION_COST_BUFFER_USDG',Decimal('0')):
   self.assertTrue(p.economics(rows,1)[0]); self.assertFalse(p.economics(rows,Decimal('1.01'))[0]); self.assertFalse(p.economics(rows,None)[0])
 def test_execution_cost_from_raw_roundtrip(self):
  self.assertEqual(p.execution_cost_from_preflight(21216360,{'reverseRaw':'20725201'}),Decimal('0.491159'))
  self.assertIsNone(p.execution_cost_from_preflight(1000000,{'forwardRaw':'123'}))
 def test_width_alignment(self):
  for width in (25,35,50):
   lo,hi=p.aligned_range(123,width,60); self.assertEqual(lo%60,0); self.assertEqual(hi%60,0); self.assertLess(lo,123); self.assertGreater(hi,123)
  self.assertEqual(p.range_width(4),(25,'normal')); self.assertEqual(p.range_width(7),(35,'high')); self.assertEqual(p.range_width(12),(50,'extreme'))

if __name__=='__main__':unittest.main()