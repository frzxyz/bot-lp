#!/usr/bin/env python3
"""Independent no-agent recovery worker. It never opens/closes LPs or discovers inventory."""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import common as c, liquidation, config as cfg

def run_once(now=None):
    # Historical WAL/queue recovery is explicitly allowed under ATOMIC_LP_ONLY.
    with c.wallet_lock():
        c.check_pending_nonce()
        return liquidation.retry_due(now)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--once',action='store_true',help='process due records once')
    ap.add_argument('--quote-only',nargs=2,metavar=('TOKEN','RAW'),help='read-only route comparison')
    ap.add_argument('--create-explicit-recovery',nargs=6,metavar=('NFT','TOKEN','SYMBOL','DECIMALS','RAW','BASELINE'))
    a=ap.parse_args()
    if a.quote_only:
        import v4_backend as v4
        print(json.dumps(v4.reverse_preflight(a.quote_only[0],int(a.quote_only[1]),quote_only=True))); return
    if a.create_explicit_recovery:
        nft,tok,sym,dec,raw,baseline=a.create_explicit_recovery
        if int(raw)<=0 or int(baseline)<0: raise SystemExit('invalid explicit amounts')
        r=liquidation.enqueue(tok,sym,int(dec),int(raw),int(baseline),source_reason='operator_confirmed_v4_recovery',source_version='v4',source_token_id=nft,venue_candidates=cfg.LIQUIDATION_VENUES)
        print(json.dumps({'created':r and r['id'],'broadcast':False})); return
    print(json.dumps(run_once()))
if __name__=='__main__': main()