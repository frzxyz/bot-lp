"""Crash-safe V4 close journal.  This file contains no transaction sender.

The baseline is the security boundary: reconciliation may adopt only a positive
post-close balance delta.  Existing wallet inventory is never inferred or swept.
"""
import hashlib, json, os, time
import config as cfg

def _load(path=None):
    p=path or cfg.V4_CLOSE_WAL_FILE
    try:
        x=json.loads(p.read_text()); return x if isinstance(x,dict) else {}
    except Exception:return {}

def _write(rows,path=None):
    p=path or cfg.V4_CLOSE_WAL_FILE; p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix(p.suffix+'.tmp')
    with t.open('w') as f:
        json.dump(rows,f,indent=2,sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.replace(t,p)
    d=os.open(str(p.parent),os.O_DIRECTORY)
    try: os.fsync(d)
    finally: os.close(d)

def begin(pos, reason, token_balance_raw, usdg_balance_raw, expected_bounds, pool_key,
          route_candidates, readiness, emergency_full_close=False, now=None, path=None):
    """WAL commit; caller MUST invoke this before decrease/collect/burn."""
    now=int(time.time() if now is None else now); tid=str(pos['token_id'])
    protected_build=bool(readiness and readiness.get('quoteOnly') is True and
                         str(readiness.get('target','')).lower()=='0x6131b5fae19ea4f9d964eac0408e4408b66337b5' and
                         readiness.get('calldata') and
                         str(readiness.get('calldata')).startswith('0xe21fd0e9') and
                         int(readiness.get('quotedOutRaw',0))>0)
    if not readiness or not (readiness.get('executable') or protected_build):
        raise RuntimeError('V4 close blocked: no protected reverse route build')
    if int(readiness.get('minOutRaw',0))<=0: raise RuntimeError('V4 close blocked: zero minOut')
    key=hashlib.sha256(('v4:'+tid+':'+pos['token'].lower()).encode()).hexdigest()[:24]
    rows=_load(path); old=rows.get(key)
    if old and old.get('phase')!='completed': return old
    rec={'id':key,'source_version':'v4','nft_id':tid,'token':pos['token'],
      'symbol':pos.get('symbol','?'),'decimals':int(pos.get('decimals',18)),
      'baseline_token_raw':int(token_balance_raw),'baseline_usdg_raw':int(usdg_balance_raw),
      'expected_close_delta_bounds_raw':{'min':int(expected_bounds[0]),'max':int(expected_bounds[1])},
      'pool_key':pool_key,'reason':reason,'phase':'intent_committed','txs':[],
      'retries':0,'created_at':now,'updated_at':now,'route_candidates':route_candidates,
      'readiness':readiness,'attributable_raw':0,'liquidation_id':None,
      'nftGone':False,'settlementComplete':False,'emergency_full_close':bool(emergency_full_close)}
    rows[key]=rec; _write(rows,path); return rec

def update(rec, path=None, **changes):
    rows=_load(path); rec=dict(rec); rec.update(changes,updated_at=int(time.time())); rows[rec['id']]=rec; _write(rows,path); return rec

def reconcile(rec, nft_exists, token_balance_raw, usdg_balance_raw, enqueue_fn, path=None):
    """Restart adoption. Enqueues positive baseline delta only, idempotently."""
    delta=max(0,int(token_balance_raw)-int(rec['baseline_token_raw']))
    rec=update(rec,path,nftGone=not nft_exists,attributable_raw=delta,
               direct_usdg_delta_raw=max(0,int(usdg_balance_raw)-int(rec['baseline_usdg_raw'])))
    if delta and not rec.get('liquidation_id'):
        q=enqueue_fn(rec,delta)
        if q: rec=update(rec,path,liquidation_id=q['id'],phase='liquidation_pending')
    elif not delta and not nft_exists:
        rec=update(rec,path,settlementComplete=True,phase='completed')
    return rec

def removable(rec): return bool(rec.get('nftGone') and rec.get('settlementComplete'))