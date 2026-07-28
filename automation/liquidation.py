"""Durable, attributable token -> USDG liquidation journal.

This module deliberately never discovers/adopts wallet leftovers.  A caller must provide
both the before-collect inventory and the exact positive collect delta.
"""
import json, time, hashlib
from decimal import Decimal
import config as cfg
import common as c

def _atomic(rows):
    p=cfg.PENDING_LIQUIDATIONS_FILE; p.parent.mkdir(parents=True,exist_ok=True)
    t=p.with_suffix(p.suffix+'.tmp'); t.write_text(json.dumps(rows,indent=2,sort_keys=True)); t.replace(p)

def load():
    try:
        x=json.loads(cfg.PENDING_LIQUIDATIONS_FILE.read_text())
        return x if isinstance(x,list) else []
    except Exception:return []

def _id(source, token_id, token):
    return hashlib.sha256(f'{source}:{token_id}:{token.lower()}'.encode()).hexdigest()[:24]

def enqueue(token, symbol, decimals, amount_raw, protected_preexisting_raw, fee=None,
            source_reason='lp_exit', source_version='v3', source_token_id=None,
            venue_candidates=None, link=None, now=None, emergency=False):
    """Persist before any approval/swap broadcast; idempotent by source/token-id."""
    now=int(time.time() if now is None else now); amount_raw=int(amount_raw)
    if amount_raw<=0: return None
    rows=load(); rid=_id(source_reason,source_token_id,token)
    for r in rows:
        if r['id']==rid:return r
    r={'id':rid,'token':token,'symbol':symbol or token[:10],'decimals':int(decimals),
       'intended_amount_raw':amount_raw,'remaining_raw':amount_raw,
       'protected_preexisting_raw':int(protected_preexisting_raw),'fee_candidates':[int(fee)] if fee else [],
       'venue_candidates':venue_candidates or ['kyber','v4','v3','v2'],'source_reason':source_reason,
       'source_version':source_version,'source_token_id':source_token_id,'link':link,
       'created_at':now,'updated_at':now,'phase':'queued','retries':0,'next_retry':now,
       'expires_at':now+cfg.LIQUIDATION_EXPIRY_SECONDS,'emergency':bool(emergency),
       'txs':[],'last_quote':None,'last_error':None,'proceeds_raw':0,
       'last_token_balance_raw':None,'last_usdg_balance_raw':None,'alert_state':'queued'}
    rows.append(r); _atomic(rows); print(f'[liquidation] QUEUED {r["symbol"]} id={rid} amount={amount_raw}')
    return r

def has_unsettled(): return any(r.get('phase')!='completed' for r in load())

def _backoff(retries, emergency=False):
    seq=([0,30,60,120,300,600,1800] if emergency else cfg.LIQUIDATION_BACKOFF_SECONDS)
    return seq[min(max(0,int(retries)),len(seq)-1)]

def _save_record(rec):
    rows=load(); rows=[rec if x.get('id')==rec['id'] else x for x in rows]; _atomic(rows)

def _route_quotes(token, amount, rec) -> list[dict]:
    """Read-only exact-input quotes; one bad venue cannot suppress another."""
    rows: list[dict] = []; venues=set(rec.get('venue_candidates') or ['kyber','v3','v4'])
    if 'kyber' in venues:
        try:
            from v4_backend import reverse_preflight
            q=reverse_preflight(token,amount,quote_only=True); out=int(q.get('quotedOutRaw',0))
            if out>0: rows.append({'venue':'kyber','amount_out_raw':out,'fee':0})
        except Exception: pass
    if 'v3' in venues:
        fees=[]
        for f in list(rec.get('fee_candidates') or [])+[100,500,3000,10000,60000]:
            if int(f)>0 and int(f) not in fees: fees.append(int(f))
        for fee in fees:
            try:
                out=int(c.quote_v3_exact_input_single(token,cfg.USDG,fee,amount))
                if out>0: rows.append({'venue':'v3','amount_out_raw':out,'fee':fee})
            except Exception: pass
    if 'v4' in venues:
        try:
            import v4_backend
            for q in v4_backend.quote_exit(token,amount):
                if q.get('eligible') and int(q.get('amountOut',0))>0:
                    p=q.get('pool') or {}
                    rows.append({'venue':'v4','amount_out_raw':int(q['amountOut']),'fee':int(p.get('fee',0)),'pool_id':p.get('poolId')})
            for q in v4_backend.quote_path(token,amount):
                if int(q.get('amountOut',0))>0 and 0<int(q.get('firstFee',0))<=100000 and 0<int(q.get('secondFee',0))<=100000:
                    rows.append({'venue':'v4_path','amount_out_raw':int(q['amountOut']),'fee':0,'first_pool_id':q.get('firstPoolId'),'second_pool_id':q.get('secondPoolId'),'first_fee':int(q['firstFee']),'second_fee':int(q['secondFee'])})
        except Exception: pass
    if 'v2' in venues:
        try:
            import v4_backend
            for q in v4_backend.quote_v2(token,amount):
                if int(q.get('amountOut',0))>0:
                    rows.append({'venue':'v2','amount_out_raw':int(q['amountOut']),'fee':3000,'route_id':q.get('routeId'),'path':q.get('path'),'minimum_reserve':int(q.get('minimumReserve',0))})
        except Exception: pass
    rows.sort(key=lambda x:(-int(x['amount_out_raw']),{'kyber':0,'v4':1,'v3':2}.get(x['venue'],9),str(x.get('pool_id','')),int(x.get('fee',0))))
    return rows

def _rkey(x):
    return (x['venue'],x.get('fee',0),x.get('pool_id'),x.get('first_pool_id'),x.get('second_pool_id'),x.get('route_id'))

def _best_route(token, amount, rec):
    """Rank exact quotes and reject thin routes using a 10%-size quote curve."""
    full=_route_quotes(token,amount,rec)
    if not full:return None
    probe_in=max(1,int(amount)//10); probe=_route_quotes(token,probe_in,rec)
    keyed={_rkey(x):x for x in probe}
    max_bps=int(Decimal(str(cfg.LIQUIDATION_MAX_PRICE_IMPACT_PCT))*100); accepted=[]
    for r in full:
        p=keyed.get(_rkey(r))
        if not p:continue
        probe_unit=Decimal(int(p['amount_out_raw']))/Decimal(probe_in)
        full_unit=Decimal(int(r['amount_out_raw']))/Decimal(int(amount))
        impact=max(0,int((Decimal(1)-full_unit/probe_unit)*10000))
        if impact<=max_bps:
            z=dict(r);z['impact_bps']=impact;accepted.append(z)
    accepted.sort(key=lambda x:(-int(x['amount_out_raw']),int(x['impact_bps']),{'kyber':0,'v4':1,'v3':2}.get(x['venue'],9)))
    return accepted[0] if accepted else None

def attempt(rec, now=None, quote_fn=None, send_fn=None):
    """Reconcile first, then quote/simulated sender. Caller already owns wallet lock."""
    now=int(time.time() if now is None else now)
    if rec.get('phase')=='completed' or now<int(rec.get('next_retry',0)):return rec
    c.check_kill()
    token0=c.erc20_balance(rec['token']); usd0=c.erc20_balance(cfg.USDG)
    # Reconcile a previous uncertain broadcast before considering another one.
    if rec.get('last_token_balance_raw') is not None:
        sold=max(0,int(rec['last_token_balance_raw'])-token0)
        gained=max(0,usd0-int(rec.get('last_usdg_balance_raw') or usd0))
        if sold:
            rec['remaining_raw']=max(0,int(rec['remaining_raw'])-sold); rec['proceeds_raw']=int(rec.get('proceeds_raw',0))+gained
    sellable=min(int(rec['remaining_raw']),max(0,token0-int(rec['protected_preexisting_raw'])))
    if int(rec['remaining_raw'])<=cfg.LIQUIDATION_DUST_RAW:
        rec.update(phase='completed',remaining_raw=0,updated_at=now,next_retry=0,alert_state='recovered'); _save_record(rec)
        print(f'[liquidation] RECOVERED {rec["symbol"]} proceeds_raw={rec["proceeds_raw"]}'); return rec
    if sellable<=0: return _fail(rec,'attributable balance unavailable',now)
    try:
        fee=int(rec['fee_candidates'][0]) if rec.get('fee_candidates') else 0; quote=0; chosen=0; route=None
        for pct in (100,50,25,10,5,2,1):
            candidate=max(1,sellable*pct//100)
            try:
                if quote_fn is not None:
                    q=int(quote_fn(rec['token'],fee,candidate))
                    if q>0: quote,chosen=q,candidate; break
                else:
                    route=_best_route(rec['token'],candidate,rec)
                    if route:
                        quote,chosen,fee=int(route['amount_out_raw']),candidate,int(route.get('fee',0)); break
            except Exception: continue
        if quote<=0:raise RuntimeError('missing/zero quote')
        sellable=chosen
        slip=min(Decimal('10'),Decimal(str(cfg.MAX_SLIPPAGE_SWAP_PCT)))
        minimum=int(Decimal(quote)*(Decimal(1)-slip/100))
        if minimum<=0:raise RuntimeError('unsafe zero minimum')
        rec.update(last_quote={'amount_in_raw':sellable,'amount_out_raw':quote,'minimum_raw':minimum,'at':now,'route':route},
                   last_token_balance_raw=token0,last_usdg_balance_raw=usd0,phase='broadcasting',updated_at=now)
        _save_record(rec) # mandatory write-before-broadcast
        if send_fn is None:
            venue=(route or {}).get('venue','kyber')
            if venue=='v3':
                from manager import swap_token_to_usdg
                send_fn=swap_token_to_usdg
            elif venue=='v4':
                import v4_backend
                send_fn=lambda token,fee,amount,minimum:v4_backend.swap_exit(token,amount,minimum)
            elif venue=='v4_path':
                import v4_backend
                # venue is read off the chosen route, so a non-default venue implies
                # a route was found.  Checked rather than assumed: a violated invariant
                # here would index None while about to broadcast a swap.  A raise (not
                # an assert) so `python -O` cannot strip the guard out of a money path;
                # the enclosing handler turns it into a backed-off retry.
                if route is None: raise RuntimeError('v4_path venue without a route')
                first,second=route['first_pool_id'],route['second_pool_id']
                send_fn=lambda token,fee,amount,minimum:v4_backend.swap_path(token,amount,minimum,first,second)
            elif venue=='v2':
                import v4_backend
                if route is None: raise RuntimeError('v2 venue without a route')
                route_id=route['route_id']
                send_fn=lambda token,fee,amount,minimum:v4_backend.swap_v2(token,amount,minimum,route_id)
            else:
                from manager import swap_token_to_usdg_kyber
                send_fn=swap_token_to_usdg_kyber
        tx=send_fn(rec['token'],fee,sellable,minimum)
        rec['txs'].append({'hash':(tx or {}).get('hash'),'status':(tx or {}).get('status'),'at':now,'amount_raw':sellable})
        # Even a successful receipt is reconciled from balances, never assumed.
        token1=c.erc20_balance(rec['token']); usd1=c.erc20_balance(cfg.USDG)
        sold=max(0,token0-token1); gained=max(0,usd1-usd0)
        rec['remaining_raw']=max(0,int(rec['remaining_raw'])-sold); rec['proceeds_raw']+=gained
        rec['last_token_balance_raw']=token1; rec['last_usdg_balance_raw']=usd1
        if rec['remaining_raw']<=cfg.LIQUIDATION_DUST_RAW:
            rec.update(phase='completed',remaining_raw=0,next_retry=0,alert_state='recovered')
            print(f'[liquidation] RECOVERED {rec["symbol"]} proceeds_raw={rec["proceeds_raw"]}')
        else: rec.update(phase='retry',next_retry=now+_backoff(int(rec['retries'])+1,rec.get('emergency')))
        rec['updated_at']=now; _save_record(rec); return rec
    except BaseException as exc:
        if isinstance(exc,(KeyboardInterrupt,SystemExit)): raise
        return _fail(rec,str(exc)[:300],now)

def _fail(rec,error,now):
    old=rec.get('last_error'); rec['retries']=int(rec.get('retries',0))+1; rec['last_error']=error; rec['updated_at']=now
    if now>=int(rec['expires_at']):
        rec.update(phase='manual_attention',next_retry=now+cfg.LIQUIDATION_MANUAL_RETRY_SECONDS)
        if rec.get('alert_state')!='manual_attention': print(f'[liquidation] MANUAL ATTENTION {rec["symbol"]}: {error}'); rec['alert_state']='manual_attention'
    else: rec.update(phase='retry',next_retry=now+_backoff(rec['retries'],rec.get('emergency')))
    if old and old!=error: print(f'[liquidation] error changed {rec["symbol"]}: {error}')
    _save_record(rec); return rec

def retry_due(now=None):
    now=int(time.time() if now is None else now); actions=[]
    for r in load():
        before=(r.get('phase'),r.get('last_error'),r.get('remaining_raw'))
        out=attempt(r,now=now)
        if before!=(out.get('phase'),out.get('last_error'),out.get('remaining_raw')): actions.append({'id':out['id'],'phase':out['phase'],'remaining_raw':out['remaining_raw']})
    return actions