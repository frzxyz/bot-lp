"""Read-only replacement validation and durable V3->V4 rotation journal."""
import time, json
import config as cfg

def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True)); tmp.replace(path)
def load_pending():
    try:return json.loads(cfg.V3_TO_V4_PENDING_FILE.read_text())
    except Exception:return {}
def update_pending(rec,phase,**kw):
    current=load_pending()
    if current and str(current.get('source_token_id'))!=str(rec.get('source_token_id')): raise RuntimeError('another V3->V4 rotation pending')
    rec=dict(current or rec); rec.update(kw,phase=phase,updated_at=int(time.time())); atomic(cfg.V3_TO_V4_PENDING_FILE,rec); return rec
def prepare_pending(ready,pos,evidence,now=None):
    now=int(time.time() if now is None else now); old=load_pending()
    if old:
        if str(old.get('source_token_id'))!=str(pos['token_id']):
            raise RuntimeError('another rotation pending')
        phase=old.get('phase')
        expired=now>=int(old.get('expires_at',0))
        old_target=(old.get('target') or {}).get('token','').lower()
        new_target=str(ready.get('token','')).lower()
        # A prepared record has not moved funds. If its target/TTL is stale while the
        # exact same source NFT is still open, replace it atomically with the freshly
        # revalidated candidate. Never replace any phase that may have moved funds.
        if phase=='prepared' and (expired or old_target!=new_target):
            old={}
        else:
            return old
    rec={'phase':'prepared','created_at':now,'updated_at':now,'expires_at':now+cfg.V3_TO_V4_EXPIRY_SECONDS,'next_retry':now,'retries':0,'source_token_id':pos['token_id'],'source_token':pos['token'],'source_symbol':pos.get('symbol'),'source_version':'v3','target':ready,'validation_evidence':evidence,'txs':{}}
    atomic(cfg.V3_TO_V4_PENDING_FILE,rec); return rec
def revalidate_ready(ready,pos,budget_raw=None,now=None):
    import common as c, v4_backend as v4
    from gmgn_risk import assess
    now=int(time.time() if now is None else now)
    if str(ready.get('source_token_id'))!=str(pos['token_id']): raise RuntimeError('source mismatch')
    if now>=int(ready.get('expires_at',0)): raise RuntimeError('ready expired')
    risk=assess(ready['token'],force=True)
    if not risk.get('ok') or risk.get('hard_stop'): raise RuntimeError('GMGN unsafe')
    raw=int(budget_raw or ready.get('validation_size_raw') or 10**cfg.USDG_DECIMALS)
    if ready.get('target_version')=='v4':
        import hybrid_v4
        if not hybrid_v4.lifecycle_verified(): raise RuntimeError('V4 lifecycle unverified')
        # Quote already returns live StateView-backed pool identity and liquidity. Do not
        # require a second Blockscout discovery call: transient empty log pages caused
        # false negatives even while the exact pool quoted successfully.
        candidate_pool=ready.get('candidate_pool') or (ready.get('validation_evidence') or {}).get('pool') or {}
        expected_pool_id=str(candidate_pool.get('poolId','')).lower()
        if not expected_pool_id: raise RuntimeError('V4 candidate missing poolId')
        quotes=[q for q in v4.quote(ready['token'],raw)
                if q.get('eligible') and int(q.get('amountOut',0))>0
                and str((q.get('pool') or {}).get('poolId','')).lower()==expected_pool_id]
        if not quotes: raise RuntimeError('exact V4 candidate quote unsafe')
        pool=quotes[0].get('pool') or {}
        if int(pool.get('liquidity',0))<=0: raise RuntimeError('exact V4 candidate has zero liquidity')
        return {'gmgn':risk,'pool':pool,'quote':quotes[0],'route_preflight':v4.route_preflight(ready['token'],raw),'size_raw':raw}
    from entry import find_pool
    from safety import round_trip
    pool,fee=find_pool(ready['token']); out=c.quote_v3_exact_input_single(cfg.USDG,ready['token'],fee,raw); safe=round_trip(ready['token'],max_tax_pct=6)
    if out<=0 or not safe.get('ok'): raise RuntimeError('V3 route unsafe')
    return {'gmgn':risk,'pool':pool,'fee':fee,'quote_out':out,'roundtrip':safe,'size_raw':raw}