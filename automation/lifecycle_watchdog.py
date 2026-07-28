#!/usr/bin/env python3
"""10-second lifecycle recovery watchdog. Quiet except transitions/errors.

No inventory discovery is performed. Only durable lifecycle/WAL/liquidation records are
eligible, so unrelated wallet balances (including PENGUZILLA) cannot be adopted.
"""
import argparse, json, os, sys, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import config as cfg, lifecycle

TX_WINDOW=cfg.STATE_DIR/"failsafe_tx_window.json"

def audit():
    errors=[]
    try: doc=lifecycle.load()
    except Exception as e: errors.append(str(e)); doc={"operations":{}}
    interval=int(os.environ.get("FAILSAFE_POLL_SECONDS","10"))
    if not 5<=interval<=30: errors.append("FAILSAFE_POLL_SECONDS must be 5..30")
    for oid,r in doc.get("operations",{}).items():
        if r.get("op_id")!=oid or r.get("phase") not in lifecycle.PHASES: errors.append(f"invalid operation {oid}")
    return {"ok":not errors,"read_only":True,"broadcast":False,"interval":interval,
            "operations":len(doc.get("operations",{})),"errors":errors,
            "hard_halt":lifecycle.HARD_HALT.exists(),"kill_switch":cfg.KILL_SWITCH.exists()}

def _receipt(h):
    import common as c
    return c.rpc_call("eth_getTransactionReceipt",[h],attempts=1)

def _runaway_ok(now):
    cap=int(os.environ.get("FAILSAFE_MAX_TX_PER_MINUTE","4"))
    try: rows=[int(x) for x in json.loads(TX_WINDOW.read_text())]
    except Exception: rows=[]
    rows=[x for x in rows if now-x<60]
    if len(rows)>=cap: return False
    return True

def _process_v4_wal(ts):
    """Reconcile only explicit close intents; never adopt unjournaled inventory."""
    import common as c, liquidation, v4_backend, v4_close_wal
    rows=v4_close_wal._load(); changes: list=[]
    if not rows: return changes
    positions={str(x.get('tokenId')) for x in v4_backend.list_positions()}
    for rec in rows.values():
        # A close may settle directly to USDG and complete before a previously queued
        # predicted-token liquidation is reconciled. In that case the queue owns no
        # attributable token delta and must not block fresh capital forever.
        if rec.get('phase')=='completed':
            # Prune only the exact closed NFT; a newer position for the same token is preserved.
            import hybrid_v4
            managed=hybrid_v4.load(); mkey=str(rec.get('token','')).lower(); managed_pos=managed.get(mkey)
            if rec.get('nftGone') and managed_pos and str(managed_pos.get('token_id'))==str(rec.get('nft_id')):
                managed.pop(mkey,None); hybrid_v4.save(managed)
                changes.append({'op':rec['id'],'phase':'position_pruned','nft':str(rec.get('nft_id'))})
            lid=rec.get('liquidation_id')
            queues=liquidation.load(); q=next((x for x in queues if x.get('id')==lid),None)
            if (q and q.get('phase') not in ('completed','cancelled') and
                rec.get('nftGone') and rec.get('settlementComplete') and
                int(rec.get('attributable_raw',0))==0 and
                int(rec.get('direct_usdg_delta_raw',0))>0 and
                c.erc20_balance(rec['token'])<=int(rec.get('baseline_token_raw',0))):
                q.update(phase='completed',remaining_raw=0,
                         proceeds_raw=int(rec.get('direct_usdg_delta_raw',0)),
                         last_error='settled directly during atomic V4 close; no attributable token delta',
                         updated_at=ts,next_retry=0)
                liquidation._atomic(queues)
                changes.append({'op':q['id'],'phase':'completed','directSettlement':True})
            continue
        def enqueue(r,delta):
            return liquidation.enqueue(r['token'],r.get('symbol'),r.get('decimals',18),delta,
              r['baseline_token_raw'],source_reason='v4_close_wal',source_version='v4',
              source_token_id=r['nft_id'],venue_candidates=cfg.LIQUIDATION_VENUES)
        out=v4_close_wal.reconcile(rec,str(rec['nft_id']) in positions,
          c.erc20_balance(rec['token']),c.erc20_balance(cfg.USDG),enqueue)
        queues={x['id']:x for x in liquidation.load()}
        q=queues.get(out.get('liquidation_id'))
        if q and q.get('phase')=='completed' and not out.get('settlementComplete'):
            out=v4_close_wal.update(out,settlementComplete=True,phase='completed')
        if out.get('phase')!=rec.get('phase'): changes.append({'op':out['id'],'phase':out['phase']})
    return changes

def run_once(read_only=False, now=None):
    ts=int(time.time() if now is None else now); changes=[]
    doc=lifecycle.load()
    if lifecycle.HARD_HALT.exists(): return [{"phase":"hard_halt","broadcast":False}]
    for rec in doc.get("operations",{}).values():
        if rec.get("phase") in ("broadcasting","confirming") and ts>=int(rec.get("next_retry",0)):
            old=rec["phase"]
            try: out=lifecycle.reconcile_receipts(rec,_receipt)
            except Exception as e: out=lifecycle.fail(rec,e)
            if out["phase"]!=old: changes.append({"op":out["op_id"],"phase":out["phase"]})
    if read_only:return changes
    if not _runaway_ok(ts):
        print(json.dumps({"phase":"manual_attention","error":"tx-per-minute recovery cap"}),flush=True); return changes
    # Explicit queue recovery is allowed despite strategy kill; HARD_HALT above remains absolute.
    os.environ["RH_EXPLICIT_RECOVERY"]="1"
    import common as c, liquidation
    with c.wallet_lock():
        c.check_pending_nonce()
        changes.extend(_process_v4_wal(ts))
        changes.extend(liquidation.retry_due(ts))
    return changes

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--audit-once",action="store_true"); ap.add_argument("--once",action="store_true")
    a=ap.parse_args()
    if a.audit_once:
        result=audit(); print(json.dumps(result,sort_keys=True)); raise SystemExit(0 if result["ok"] else 1)
    interval=int(os.environ.get("FAILSAFE_POLL_SECONDS","10"))
    if not 5<=interval<=30: raise SystemExit("FAILSAFE_POLL_SECONDS must be 5..30")
    with lifecycle.daemon_singleton():
        if a.once: print(json.dumps(run_once())); return
        while True:
            try: run_once()
            except Exception as e: print(json.dumps({"phase":"watchdog_error","error":str(e)[:300]}),flush=True)
            time.sleep(interval)
if __name__=="__main__": main()
