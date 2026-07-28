import tempfile, unittest, os
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
import hybrid_v4 as h

TOKEN='0xABC'
OLD={'version':'v4','token':TOKEN,'symbol':'MEME','token_id':'1','expected_close_token_raw':1000,'entry_value_usd':'20','principal_usdg':'20','compounded_capital_usdg':'20','peak_capital_usdg':'20','cumulative_realized_profit_usdg':'0','rebalance_count':2}
OPEN={'tokenId':'2','poolId':'pool','tickLower':-10,'tickUpper':10,'txHash':'0xmint','swapHash':'0xswap'}
def close(amount): return {'nftGone':True,'settlementComplete':True,'closeTx':'0xclose','directUsdgRaw':'1000000','liquidationUsdgRaw':str(int(amount*1_000_000)-1_000_000),'totalUsdgProceedsRaw':str(int(amount*1_000_000))}

class RebalanceTest(unittest.TestCase):
 def setUp(self):
  self.env=patch.dict(os.environ,{'LIFECYCLE_FAILSAFE':'false'}); self.env.start()
  self.t=tempfile.TemporaryDirectory(); root=Path(self.t.name); self.available=1000; self.raw=[]
  self.paths=patch.multiple(h.cfg,V4_POSITIONS_FILE=root/'positions.json',V4_CLOSE_WAL_FILE=root/'wal.json',V4_REBALANCE_PENDING_FILE=root/'pending.json',V4_LIFECYCLE_MARKER=root/'marker.json',V4_MAX_POSITION_USDG=Decimal('25'),V4_COMPOUND_MAX_USDG=Decimal('250'),USDG_RESERVE=Decimal('2'),V4_REBALANCE_RETRY_SECONDS=900)
  self.paths.start(); h.save({TOKEN.lower():dict(OLD)}); h.save_pending({})
  h.cfg.V4_LIFECYCLE_MARKER.write_text('{"passed":true,"chain_id":4663,"wallet":"'+h.cfg.WALLET_ADDRESS+'","post_close_open":false,"mint_tx":"a","collect_tx":"b","close_tx":"c"}')
  self.gates=patch.multiple(h.c,check_kill=lambda:None,erc20_balance=lambda _:int(self.available*1_000_000),eth_balance=lambda:10**18,get_gas_price=lambda:{},check_pending_nonce=lambda:1)
  self.backend=patch.multiple(h.v4,close_usdg=lambda _:close(24),reverse_preflight=lambda *_,**__:{'executable':True,'minOutRaw':'1'},discover=lambda _:[{}],quote=lambda *_:[{'eligible':True,'amountOut':1}],route_preflight=lambda *_:{},open_usdg_kyber=lambda _,raw,*a:self.raw.append(raw) or OPEN)
  self.widths=[]
  self.atomic_open=patch.object(h.atomic_v4,'open_position',side_effect=lambda _,raw,width_pct=None,**kw:(self.raw.append(raw),self.widths.append(width_pct)) and OPEN or OPEN)
  self.gates.start(); self.backend.start(); self.atomic_open.start(); self.risk=patch.object(h,'gmgn_assess',return_value={'ok':True,'hard_stop':False}); self.risk.start()
 def tearDown(self):
  self.risk.stop(); self.atomic_open.stop(); self.backend.stop(); self.gates.stop(); self.paths.stop(); self.env.stop(); self.t.cleanup()
 def run_close(self,amount):
  with patch.object(h.atomic_v4,'close_position',return_value=close(amount)): return h.rebalance(TOKEN.lower(),dict(OLD),now=100)
 def test_profit_20_percent_reopens_1_2x(self):
  out=self.run_close(24); self.assertEqual(self.raw,[24_000_000]); p=h.load()[TOKEN.lower()]
  self.assertEqual(p['compounded_capital_usdg'],'24'); self.assertEqual(p['cumulative_realized_profit_usdg'],'4'); self.assertEqual(out['reopen_budget_usdg'],'24')
 def test_loss_reopens_smaller(self):
  self.run_close(15); self.assertEqual(self.raw,[15_000_000]); self.assertEqual(h.load()[TOKEN.lower()]['cumulative_realized_profit_usdg'],'-5')
 def test_unrelated_wallet_usdg_not_included(self):
  self.available=1000; self.run_close(24); self.assertEqual(self.raw,[24_000_000])
 def test_available_minus_reserve_caps(self):
  self.available=12; self.run_close(24); self.assertEqual(self.raw,[10_000_000])
 def test_absolute_cap(self):
  self.run_close(300); self.assertEqual(self.raw,[250_000_000])
 def test_pending_retry_keeps_exact_budget(self):
  with patch.object(h.atomic_v4,'open_position',side_effect=RuntimeError('no')): self.run_close(24)
  self.assertEqual(h.load_pending()[TOKEN.lower()]['isolated_budget_usdg'],'24')
  self.available=1000; h.retry_pending(now=1000); self.assertEqual(self.raw,[24_000_000])
 def test_missing_proceeds_fails_closed(self):
  bad={'nftGone':True,'settlementComplete':True}
  with patch.object(h.atomic_v4,'close_position',return_value=bad): out=h.rebalance(TOKEN.lower(),dict(OLD),now=100)
  self.assertFalse(self.raw); self.assertIsNone(h.load_pending()[TOKEN.lower()]['isolated_budget_usdg']); self.assertIn('missing',out['error'])
 def test_reopen_forwards_the_range_width(self):
  """The width used to be computed and then silently dropped at the executor call."""
  self.run_close(24); self.assertEqual(self.widths,[int(h.cfg.RANGE_PCT)])
 def test_reopen_uses_recorded_width_when_present(self):
  with patch.object(h.atomic_v4,'close_position',return_value=close(24)):
   h.rebalance(TOKEN.lower(),dict(OLD),now=100,range_width=37)
  self.assertEqual(self.widths,[37])
 def test_close_incomplete_retains_old(self):
  with patch.object(h.atomic_v4,'close_position',return_value={'nftGone':True,'settlementComplete':False}): h.rebalance(TOKEN.lower(),dict(OLD),now=100)
  self.assertIn(TOKEN.lower(),h.load()); self.assertEqual(h.load_pending(),{})

if __name__=='__main__': unittest.main()
