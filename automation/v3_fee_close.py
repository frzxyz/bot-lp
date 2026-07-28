"""Durable second leg of split V3 close: collect residual fees, enqueue token delta, burn.

The WAL is written before collection, so a restart can attribute only balance above the
protected pre-collect baseline. It never adopts wallet-wide inventory.
"""
import json, time
import config as cfg
import common as c
import liquidation


def _load():
    try:
        x=json.loads(cfg.V3_FEE_CLOSE_WAL_FILE.read_text())
        return x if isinstance(x,list) else []
    except Exception:return []


def _save(rows):
    p=cfg.V3_FEE_CLOSE_WAL_FILE; p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix(p.suffix+'.tmp'); t.write_text(json.dumps(rows,indent=2,sort_keys=True)); t.replace(p)


def _put(rec):
    rows=_load(); rows=[rec if str(x['token_id'])==str(rec['token_id']) else x for x in rows]
    if not any(str(x['token_id'])==str(rec['token_id']) for x in rows): rows.append(rec)
    _save(rows)


def prepare(token_id,token,symbol,decimals,fee,reason='lp_exit',now=None):
    for x in _load():
        if str(x['token_id'])==str(token_id): return x
    now=int(time.time() if now is None else now)
    r={'token_id':int(token_id),'token':token,'symbol':symbol,'decimals':int(decimals),'fee':int(fee),
       'reason':reason,'phase':'prepared','protected_token_raw':c.erc20_balance(token),
       'protected_usdg_raw':c.erc20_balance(cfg.USDG),'created_at':now,'updated_at':now,
       'collect_tx':None,'burn_tx':None,'liquidation_id':None,'last_error':None}
    _put(r); return r


def advance(rec,collect_fn,burn_fn,now=None):
    """Idempotently reconcile/advance. Caller holds the wallet lock; sends serially."""
    now=int(time.time() if now is None else now)
    try:
        if rec['phase']=='prepared':
            tx=collect_fn(rec['token_id'])
            if not tx or int(tx.get('status',0))!=1 or not tx.get('hash'): raise RuntimeError('fee collect receipt failed')
            rec.update(phase='collected',collect_tx=tx,updated_at=now); _put(rec)
        if rec['phase']=='collected':
            amount=max(0,c.erc20_balance(rec['token'])-int(rec['protected_token_raw']))
            q=liquidation.enqueue(rec['token'],rec.get('symbol'),rec['decimals'],amount,
                rec['protected_token_raw'],rec['fee'],source_reason=rec['reason']+'_fees',source_version='v3-fees',
                source_token_id=rec['token_id'],venue_candidates=['kyber'],now=now)
            rec.update(phase='queued',liquidation_id=q and q['id'],updated_at=now); _put(rec)
        if rec['phase']=='queued':
            tx=burn_fn(rec['token_id'])
            if not tx or int(tx.get('status',0))!=1 or not tx.get('hash'): raise RuntimeError('fee NFT burn receipt failed')
            rec.update(phase='completed',burn_tx=tx,updated_at=now); _put(rec)
        return rec
    except BaseException as exc:
        if isinstance(exc,(KeyboardInterrupt,SystemExit)): raise
        rec.update(last_error=str(exc)[:300],updated_at=now); _put(rec); raise


def retry_due(collect_fn,burn_fn):
    out=[]
    for r in _load():
        if r.get('phase')!='completed': out.append(advance(r,collect_fn,burn_fn))
    return out
