import tempfile, unittest
from pathlib import Path
import v4_close_wal as w

POS={'token_id':'1','token':'0x0000000000000000000000000000000000000001','symbol':'T'}
PROOF={'quoteOnly':True,'target':'0x6131B5fae19EA4f9D964eAc0408E4408b66337b5','calldata':'0xe21fd0e9abcd','quotedOutRaw':'10','minOutRaw':'9'}

class ProtectedBuildTest(unittest.TestCase):
 def test_keyless_protected_build_is_wal_ready(self):
  with tempfile.TemporaryDirectory() as d:
   r=w.begin(POS,'test',0,0,(1,2),{'poolId':'0x01'},[PROOF],PROOF,path=Path(d)/'wal.json')
   self.assertEqual(r['phase'],'intent_committed')
 def test_unknown_target_rejected(self):
  bad=dict(PROOF,target='0x0000000000000000000000000000000000000002')
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaisesRegex(RuntimeError,'no protected reverse route build'):
    w.begin(POS,'test',0,0,(1,2),{'poolId':'0x01'},[bad],bad,path=Path(d)/'wal.json')

if __name__=='__main__': unittest.main()
