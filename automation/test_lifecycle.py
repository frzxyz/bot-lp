import fcntl, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import lifecycle

class LifecycleSafetyTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); p=Path(self.t.name)
  self.old=(lifecycle.JOURNAL,lifecycle.LOCK,lifecycle.HARD_HALT,lifecycle.DAEMON_LOCK)
  lifecycle.JOURNAL=p/'ops.json'; lifecycle.LOCK=p/'ops.lock'; lifecycle.HARD_HALT=p/'HARD'; lifecycle.DAEMON_LOCK=p/'daemon.lock'
 def tearDown(self):
  lifecycle.JOURNAL,lifecycle.LOCK,lifecycle.HARD_HALT,lifecycle.DAEMON_LOCK=self.old; self.t.cleanup()
 def prep(self,**kw): return lifecycle.prepare('v3','open',token='0x1',before={'usdg_raw':10},**kw)
 def test_durable_before_broadcast_and_idempotent(self):
  r=self.prep(idempotency_key='x'); self.assertTrue(lifecycle.JOURNAL.exists()); self.assertEqual(r['op_id'],self.prep(idempotency_key='x')['op_id'])
 def test_crash_after_broadcast_reconciles_mined(self):
  r=lifecycle.transition(self.prep(),'broadcasting',tx='0xabc'); o=lifecycle.reconcile_receipts(r,lambda _:{'status':1,'blockNumber':3}); self.assertEqual(o['phase'],'postcheck')
 def test_dropped_rpc_schedules_retry(self):
  r=lifecycle.transition(self.prep(),'broadcasting',tx='0xabc'); o=lifecycle.reconcile_receipts(r,lambda _:(_ for _ in()).throw(OSError('drop'))); self.assertEqual(o['phase'],'retry')
 def test_timeout_then_mined_no_resend(self):
  r=lifecycle.transition(self.prep(),'broadcasting',tx='0xabc'); o=lifecycle.reconcile_receipts(r,lambda _:None); self.assertEqual(o['phase'],'confirming'); o=lifecycle.reconcile_receipts(o,lambda _:{'status':1}); self.assertEqual(o['phase'],'postcheck'); self.assertEqual(len(o['txs']),1)
 def test_reverted_atomic_has_no_compensation(self):
  r=lifecycle.transition(self.prep(),'broadcasting',tx='0xabc'); o=lifecycle.reconcile_receipts(r,lambda _:{'status':0}); self.assertEqual(o['phase'],'manual_attention'); self.assertIn('no compensating',o['last_error'])
 def test_backoff_and_caps(self):
  r=self.prep(); delays=[]
  with patch.object(lifecycle,'now',return_value=100):
   for _ in range(6): r=lifecycle.fail(r,'x'); delays.append(r['next_retry']-100)
  self.assertEqual(delays,[10,20,30,60,120,300]); r=lifecycle.fail(r,'x'); self.assertEqual(r['phase'],'manual_attention')
 def test_hard_halt(self): lifecycle.HARD_HALT.touch(); self.assertFalse(lifecycle.recovery_allowed())
 def test_v4_atomic_enabled_default(self):
  with patch.dict(os.environ,{'LIFECYCLE_FAILSAFE':'true'},clear=False):
   lifecycle.assert_new_strategy_allowed('v4')
 def test_duplicate_daemon_lock(self):
  lifecycle.DAEMON_LOCK.parent.mkdir(parents=True,exist_ok=True)
  f=lifecycle.DAEMON_LOCK.open('a+'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  try:
   with self.assertRaisesRegex(RuntimeError,'already running'):
    with lifecycle.daemon_singleton(): pass
  finally:f.close()
 def test_completed_immutable(self):
  r=lifecycle.transition(self.prep(),'completed')
  with self.assertRaises(RuntimeError): lifecycle.transition(r,'retry')

if __name__=='__main__': unittest.main()
