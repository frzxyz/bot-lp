"""V4-only orchestration. This module never imports or calls a V3 ABI."""
import json, time
from decimal import Decimal
import config as cfg
import common as c
import v4_backend as v4
import adaptive_v4 as policy
import v4_close_wal as close_wal
import lifecycle
import atomic_v4_backend as atomic_v4
from gmgn_risk import assess as gmgn_assess


def _json(p, d):
    try: return json.loads(p.read_text())
    except Exception: return d

def _save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True)); tmp.replace(path)

def load(): return _json(cfg.V4_POSITIONS_FILE,{})
def save(x): _save_json(cfg.V4_POSITIONS_FILE,x)
def load_pending(): return _json(cfg.V4_REBALANCE_PENDING_FILE,{})
def save_pending(x): _save_json(cfg.V4_REBALANCE_PENDING_FILE,x)
def load_history(): return _json(cfg.V4_MARKET_HISTORY_FILE,{})
def save_history(x): _save_json(cfg.V4_MARKET_HISTORY_FILE,x)
def load_reentry(): return _json(cfg.V4_REENTRY_PENDING_FILE,{})
def save_reentry(x): _save_json(cfg.V4_REENTRY_PENDING_FILE,x)
def total_positions(v3=None,v4s=None): return len(v3 if v3 is not None else _json(cfg.POSITIONS_FILE,{}))+len(v4s if v4s is not None else load())
def has_settled_v3_rotation():
    from rotation import load_pending
    return load_pending().get('phase') in ('closing','liquidation_pending','settled','opening','recovery')
def can_delete(r): return bool(r and r.get('nftGone') and r.get('settlementComplete'))
def _close(pos):
    """Atomic-only mode can never fall through to legacy multi-transaction settlement."""
    return atomic_v4.close_position(pos['token_id'],pos.get('token')) if cfg.ATOMIC_LP_ONLY else v4.close_usdg(pos['token_id'])

def _prepare_close(pos,reason,emergency_full_close=False):
    """Fail closed unless an exact raw estimate and protected reverse route build exist."""
    predicted=int(pos.get('expected_close_token_raw',0))
    if predicted<=0: raise RuntimeError('V4 close blocked: exact conservative token-raw prediction unavailable')
    # The token proceeds do not exist in the wallet until the atomic close executes.
    # Therefore pre-existing allowance cannot be a prerequisite for route discovery.
    # The atomic executor performs exact approval and complete eth_call simulation later.
    proof=v4.reverse_preflight(pos['token'],predicted,quote_only=True)
    return close_wal.begin(pos,reason,c.erc20_balance(pos['token']),c.erc20_balance(cfg.USDG),
      (1,predicted),pos.get('pool_key',{'poolId':pos.get('poolId')}),[proof],proof,
      emergency_full_close=emergency_full_close)

def lifecycle_verified():
    m=_json(cfg.V4_LIFECYCLE_MARKER,{})
    return bool(m.get('passed') is True and int(m.get('chain_id',0))==cfg.CHAIN_ID and str(m.get('wallet','')).lower()==cfg.WALLET_ADDRESS.lower() and m.get('post_close_open') is False and m.get('mint_tx') and m.get('collect_tx') and m.get('close_tx'))

def close_position(key,reason,dry_run=None):
    positions=load(); pos=positions[key]
    if dry_run if dry_run is not None else cfg.DRY_RUN: return {'dry_run':True,'reason':reason,'token_id':pos['token_id']}
    lifecycle.assert_new_strategy_allowed('v4')
    _prepare_close(pos,reason)
    result=_close(pos)
    if can_delete(result): del positions[key]; save(positions)
    return result

def _proceeds_usdg(result):
    """Exact close-attributable proceeds; never infer from wallet balance."""
    if not isinstance(result,dict) or 'totalUsdgProceedsRaw' not in result:
        raise RuntimeError('close settled but exact totalUsdgProceedsRaw is missing; refusing reopen')
    raw=int(result['totalUsdgProceedsRaw'])
    if raw < 0: raise RuntimeError('invalid negative close proceeds')
    return Decimal(raw)/Decimal(10**cfg.USDG_DECIMALS)

def _make_pending(pos,reason,now,proceeds=None,error=''):
    key=pos['token'].lower(); allp=load_pending(); old=allp.get(key,{})
    principal=Decimal(str(pos.get('principal_usdg',pos.get('entry_value_usd','0'))))
    prior=Decimal(str(pos.get('compounded_capital_usdg',pos.get('entry_value_usd','0'))))
    cumulative=Decimal(str(pos.get('cumulative_realized_profit_usdg','0')))
    rec={'token':pos['token'],'symbol':pos.get('symbol','?'),'isolated_budget_usdg':None if proceeds is None else str(proceeds),'principal_usdg':str(principal),'last_realized_usdg':None if proceeds is None else str(proceeds),'cumulative_realized_profit_usdg':str(cumulative if proceeds is None else cumulative+proceeds-prior),'peak_capital_usdg':str(max(Decimal(str(pos.get('peak_capital_usdg',prior))),proceeds or Decimal(0))),'reason':reason,'last_error':error,'attempt_count':int(old.get('attempt_count',0)),'next_retry':now,'closed_token_id':str(pos.get('token_id','')),'rebalance_count':int(pos.get('rebalance_count',0))+1,'closed_at':now}
    allp[key]=rec; save_pending(allp); return rec

def _reopen(rec,dry_run=False,now=None):
    now=int(time.time() if now is None else now)
    c.check_kill()
    if not lifecycle_verified(): raise RuntimeError('V4 lifecycle marker absent or invalid')
    risk=gmgn_assess(rec['token'],force=True)
    if not risk.get('ok') or risk.get('hard_stop'): raise RuntimeError('GMGN reject: '+str(risk.get('reason','unknown')))
    if not v4.discover(rec['token']): raise RuntimeError('no eligible live V4 pool')
    available=Decimal(c.erc20_balance(cfg.USDG))/Decimal(10**cfg.USDG_DECIMALS)
    if rec.get('isolated_budget_usdg') is None: raise RuntimeError('exact isolated close budget unavailable')
    proceeds=Decimal(str(rec['isolated_budget_usdg']))
    fraction=Decimal(str(rec.get('reopen_fraction','1')))
    amount=min(proceeds*fraction,cfg.V4_COMPOUND_MAX_USDG,max(Decimal(0),available-cfg.USDG_RESERVE))
    if amount<=0: raise RuntimeError('USDG reserve leaves no rebalance budget')
    if c.eth_balance()<int(cfg.GAS_RESERVE_ETH*Decimal(10**18)): raise RuntimeError('insufficient native gas reserve')
    c.get_gas_price(); c.check_pending_nonce()
    raw=int(amount*10**cfg.USDG_DECIMALS)
    quotes=[q for q in v4.quote(rec['token'],raw) if q.get('eligible') and int(q.get('amountOut',0))>0]
    if not quotes: raise RuntimeError('no live V4 quote')
    v4.route_preflight(rec['token'],raw)
    if dry_run: return {'dry_run':True,'action':'reopen','token':rec['token'],'amount_usdg':str(amount)}
    width=int(rec.get('range_width_percent',25))
    try: result=atomic_v4.open_position(rec['token'],raw) if cfg.ATOMIC_LP_ONLY else v4.open_usdg_kyber(rec['token'],raw,width)
    except TypeError: result=atomic_v4.open_position(rec['token'],raw) if cfg.ATOMIC_LP_ONLY else v4.open_usdg_kyber(rec['token'],raw) # older/mock backend compatibility
    key=rec['token'].lower(); positions=load()
    reserve=proceeds-amount
    positions[key]={'version':'v4','token':rec['token'],'symbol':rec.get('symbol','?'),'token_id':str(result['tokenId']),'poolId':result.get('poolId'),'tick_lower':result.get('tickLower'),'tick_upper':result.get('tickUpper'),'mint_tx':result.get('txHash'),'swap_tx':result.get('swapHash'),'mint_time':now,'entry_value_usd':str(amount),'principal_usdg':rec['principal_usdg'],'last_realized_usdg':rec['last_realized_usdg'],'cumulative_realized_profit_usdg':rec['cumulative_realized_profit_usdg'],'compounded_capital_usdg':str(amount),'isolated_strategy_reserve_usdg':str(reserve),'peak_capital_usdg':rec['peak_capital_usdg'],'last_fee_usd':'0','fee_baseline_ts':now,'rebalance_count':int(rec.get('rebalance_count',1)),'rebalanced_from_token_id':rec.get('closed_token_id'),'rebalanced_at':now,'rebalance_cooldown_until':now+cfg.V4_REBALANCE_COOLDOWN_SECONDS,'range_width_percent':width,'range_mode':rec.get('range_mode','normal')}
    save(positions); return result

def _failure(key,rec,exc,now):
    allp=load_pending(); rec=allp.get(key,rec); rec['attempt_count']=int(rec.get('attempt_count',0))+1; rec['last_error']=str(exc)[:300]; rec['next_retry']=now+cfg.V4_REBALANCE_RETRY_SECONDS; allp[key]=rec; save_pending(allp)
    return {'token':key,'error':rec['last_error'],'action':'rebalance_pending'}

def rebalance(key,pos,dry_run=False,now=None,reopen_fraction=Decimal('1'),range_width=25,range_mode='normal'):
    now=int(time.time() if now is None else now)
    if dry_run: return {'dry_run':True,'action':'rebalance','reason':'out_of_range','token_id':pos['token_id'],'token':pos['token'],'budget':'exact verified close proceeds (unknown until close)','compound_cap':str(cfg.V4_COMPOUND_MAX_USDG),'plan':['close_usdg','verify exact isolated proceeds','live safety gates','open_usdg_kyber']}
    lifecycle.assert_new_strategy_allowed('v4')
    _prepare_close(pos,'out_of_range')
    result=_close(pos)
    if not can_delete(result): return {'token':key,'error':'V4 close incomplete; old state retained','close':result}
    positions=load(); positions.pop(key,None); save(positions)
    try: proceeds=_proceeds_usdg(result); rec=_make_pending(pos,'out_of_range',now,proceeds)
    except Exception as exc:
        rec=_make_pending(pos,'out_of_range',now,None,str(exc)); return _failure(key,rec,exc,now)
    allp=load_pending(); allp[key].update(close_result=result,reopen_fraction=str(reopen_fraction),range_width_percent=range_width,range_mode=range_mode); save_pending(allp); rec=allp[key]
    try:
        opened=_reopen(rec,False,now); allp=load_pending(); allp.pop(key,None); save_pending(allp)
        return {'action':'rebalanced','realized_usdg':str(proceeds),'reopen_budget_usdg':load()[key]['compounded_capital_usdg'],'close':result,'open':opened}
    except Exception as exc: return _failure(key,rec,exc,now)

def deep_exit(key,pos,now,dry_run=False,hard_stop=False):
    """Settle only; durable delayed re-entry. Never opens in this call."""
    if dry_run:return {'dry_run':True,'action':'deep_exit','token_id':pos['token_id']}
    _prepare_close(pos,'deep_oor',emergency_full_close=hard_stop)
    result=_close(pos)
    if not can_delete(result):return {'error':'V4 close incomplete; old state retained','close':result}
    proceeds=_proceeds_usdg(result); positions=load(); positions.pop(key,None); save(positions)
    width,mode=policy.range_width(policy.metrics(load_history().get(key,[]),now).get('realized_vol_1h_pct'))
    rec=_make_pending(pos,'deep_oor',now,proceeds); rec.update(earliest_retry=now+(24*3600 if hard_stop else cfg.V4_REENTRY_DELAY_SECONDS),expires_at=now+cfg.V4_REENTRY_EXPIRY_SECONDS,reopen_fraction='1',range_width_percent=width or 50,range_mode=mode or 'extreme',hard_stop=hard_stop,exact_close_proceeds_usdg=str(proceeds))
    p=load_pending(); p.pop(key,None); save_pending(p); q=load_reentry(); q[key]=rec; save_reentry(q)
    return {'action':'deep_exit_usdg','proceeds_usdg':str(proceeds),'reopen':False}

def retry_reentry(dry_run=None,now=None):
    if cfg.STRATEGY_MODE == 'stable_first_exit':
        # Keep exact close proceeds in USDG; entries require a fresh scanner decision.
        return []
    now=int(time.time() if now is None else now); dry=cfg.DRY_RUN if dry_run is None else dry_run; out=[]; pending=load_reentry()
    for key,rec in list(pending.items()):
        if now>=int(rec['expires_at']):
            cooldown=_json(cfg.COOLDOWN_FILE,{}); cooldown[key]=now+24*3600; _save_json(cfg.COOLDOWN_FILE,cooldown)
            pending.pop(key); out.append({'token':key,'action':'expired_usdg'}); continue
        if now<int(rec['earliest_retry']):continue
        risk=gmgn_assess(rec['token'],force=True); history=load_history()
        try:
            pools=v4.discover(rec['token']); pool=max(pools,key=lambda x:int(x.get('liquidity',0)))
            policy.append_snapshot(history,key,{'tick':pool.get('tick'),'liquidity':pool.get('liquidity'),'liquiditySource':'stateview'},now); save_history(history)
        except Exception: pass
        m=policy.metrics(history.get(key,[]),now)
        econ=False
        try:
            raw=int(Decimal(str(rec['isolated_budget_usdg']))*10**cfg.USDG_DECIMALS)
            quote=[q for q in v4.quote(rec['token'],raw) if q.get('eligible') and int(q.get('amountOut',0))>0]
            pf=v4.route_preflight(rec['token'],raw); cost=policy.execution_cost_from_preflight(raw,pf,cfg.USDG_DECIMALS)
            econ=bool(quote) and policy.economics(load_history().get(key,[]),cost)[0]
        except Exception: pass
        ok,reasons=policy.evidence_gates('shallow',m,risk,econ)
        if not ok: rec['attempt_count']=int(rec.get('attempt_count',0))+1; rec['last_error']=','.join(reasons); continue
        try:
            opened=_reopen(rec,dry,now); out.append(opened)
            if not dry:pending.pop(key,None)
        except Exception as exc: rec['attempt_count']=int(rec.get('attempt_count',0))+1; rec['last_error']=str(exc)[:300]
    save_reentry(pending); return out

def retry_pending(dry_run=None,now=None):
    dry=cfg.DRY_RUN if dry_run is None else dry_run; now=int(time.time() if now is None else now); actions=[]
    for key,rec in list(load_pending().items()):
        if now<int(rec.get('next_retry',0)): continue
        try:
            out=_reopen(rec,dry,now); actions.append(out)
            if not dry: allp=load_pending(); allp.pop(key,None); save_pending(allp)
        except Exception as exc: actions.append(_failure(key,rec,exc,now))
    return actions

def enter(cand,dry_run=None):
    if has_settled_v3_rotation(): raise RuntimeError('settled rotation capital pending; unrelated entry blocked')
    lifecycle.assert_new_strategy_allowed('v4')
    if total_positions()>=cfg.MAX_POSITIONS: raise RuntimeError('position cap reached')
    if not lifecycle_verified(): raise RuntimeError('V4 lifecycle marker absent or invalid')
    if Decimal(str(cand.get('liq_usd',0)))<cfg.MIN_LIQ_USD: raise RuntimeError('liquidity filter')
    age=Decimal(str(cand.get('age_hours',cand.get('age_h',0))))
    if age<cfg.MIN_AGE_HOURS: raise RuntimeError('age filter')
    quotes=[q for q in v4.quote(cand['token'],10**6) if q.get('eligible') and int(q.get('amountOut',0))>0]
    if not quotes: raise RuntimeError('no live V4 quote')
    available=Decimal(c.erc20_balance(cfg.USDG))/Decimal(10**cfg.USDG_DECIMALS); amount=min(cfg.POSITION_SIZE_USDG,cfg.V4_MAX_POSITION_USDG,max(Decimal(0),available-cfg.USDG_RESERVE))
    if amount<=0: raise RuntimeError('USDG reserve leaves no budget')
    v4.route_preflight(cand['token'],int(amount*10**cfg.USDG_DECIMALS))
    if dry_run if dry_run is not None else cfg.DRY_RUN: return {'dry_run':True,'amount_usdg':str(amount)}
    result=atomic_v4.open_position(cand['token'],int(amount*10**cfg.USDG_DECIMALS)) if cfg.ATOMIC_LP_ONLY else v4.open_usdg_kyber(cand['token'],int(amount*10**cfg.USDG_DECIMALS)); now=int(time.time()); key=cand['token'].lower(); positions=load()
    positions[key]={'version':'v4','token':cand['token'],'symbol':cand.get('symbol','?'),'token_id':str(result['tokenId']),'poolId':result.get('poolId'),'tick_lower':result.get('tickLower'),'tick_upper':result.get('tickUpper'),'mint_tx':result.get('txHash'),'swap_tx':result.get('swapHash'),'mint_time':now,'entry_value_usd':str(amount),'principal_usdg':str(amount),'last_realized_usdg':'0','cumulative_realized_profit_usdg':'0','compounded_capital_usdg':str(amount),'peak_capital_usdg':str(amount),'rebalance_count':0,'last_fee_usd':'0','fee_baseline_ts':now}; save(positions); return result

def enter_isolated_rotation(cand,exact_budget_raw,source,dry_run=False,now=None):
    """Public V4 entry bounded by exact V3 settlement proceeds (never wallet NAV)."""
    now=int(time.time() if now is None else now); c.check_kill()
    if not lifecycle_verified(): raise RuntimeError('V4 lifecycle marker absent or invalid')
    risk=gmgn_assess(cand['token'],force=True)
    if not risk.get('ok') or risk.get('hard_stop'): raise RuntimeError('GMGN unsafe')
    age=Decimal(str(cand.get('age_hours',cand.get('age_h',0))))
    if age<Decimal('1'): raise RuntimeError('age filter')
    pools=v4.discover(cand['token'])
    if not pools: raise RuntimeError('no eligible V4 pool/hook')
    exact=int(exact_budget_raw); cap=int(cfg.V4_COMPOUND_MAX_USDG*10**cfg.USDG_DECIMALS)
    available=c.erc20_balance(cfg.USDG); reserve=int(cfg.USDG_RESERVE*10**cfg.USDG_DECIMALS)
    raw=min(exact,cap,max(0,available-reserve))
    if raw<=0: raise RuntimeError('reserve leaves no isolated budget')
    quotes=[q for q in v4.quote(cand['token'],raw) if q.get('eligible') and int(q.get('amountOut',0))>0]
    if not quotes: raise RuntimeError('no exact-size live quote')
    v4.route_preflight(cand['token'],raw); c.get_gas_price(); c.check_pending_nonce()
    history=load_history().get(cand['token'].lower(),[]); width=25; mode='normal'
    if history:
        width,mode=policy.range_width(policy.metrics(history,now).get('realized_vol_1h_pct')); width=width or 25
    if dry_run:return {'dry_run':True,'budget_raw':raw,'width':width}
    result=atomic_v4.open_position(cand['token'],raw) if cfg.ATOMIC_LP_ONLY else v4.open_usdg_kyber(cand['token'],raw,width)
    if not result.get('tokenId') or not result.get('txHash'): raise RuntimeError('mint closure evidence missing')
    amount=Decimal(raw)/Decimal(10**cfg.USDG_DECIMALS); reserve_rem=Decimal(exact-raw)/Decimal(10**cfg.USDG_DECIMALS)
    key=cand['token'].lower(); positions=load()
    positions[key]={'version':'v4','token':cand['token'],'symbol':cand.get('symbol','?'),'token_id':str(result['tokenId']),'poolId':result.get('poolId'),'tick_lower':result.get('tickLower'),'tick_upper':result.get('tickUpper'),'mint_tx':result.get('txHash'),'swap_tx':result.get('swapHash'),'mint_time':now,'entry_value_usd':str(amount),'principal_usdg':str(amount),'compounded_capital_usdg':str(amount),'isolated_strategy_reserve_usdg':str(reserve_rem),'rotation_source_version':'v3','rotation_source_token':source.get('source_token'),'rotation_source_token_id':str(source.get('source_token_id')),'exact_close_proceeds_raw':str(exact),'range_width_percent':width,'range_mode':mode,'last_fee_usd':'0','fee_baseline_ts':now}
    save(positions); return result

def retry_v3_to_v4(dry_run=None,now=None):
    from rotation import load_pending, update_pending, revalidate_ready, atomic
    now=int(time.time() if now is None else now); rec=load_pending()
    if not rec:return []
    if rec.get('phase')=='prepared': return [] # manager owns close; never re-close here
    if rec.get('phase')=='settled' and rec.get('exact_proceeds_raw') is None:
        # A settled record without exact proceeds is impossible. Older retry code could
        # mislabel a failed close as settled after catching an exception. Reconcile the
        # source NFT before doing anything: if it is still live, return ownership to the
        # manager; if it is gone, stop for manual balance/receipt reconciliation.
        try:
            from manager import position_info
            live = int(position_info(rec['source_token_id'])['liquidity']) > 0
        except Exception:
            live = False
        if live:
            update_pending(rec,'prepared',last_error='reconciled: source NFT still open; manager owns close',next_retry=now)
            return [{'action':'v3_to_v4_reconciled','status':'source_open','token_id':rec['source_token_id']}]
        update_pending(rec,'recovery',last_error='settled record missing exact proceeds and source NFT absent; manual receipt/balance reconciliation required')
        return [{'action':'v3_to_v4_recovery','error':'missing exact proceeds; no transaction attempted'}]
    if rec.get('phase')=='liquidation_pending':
        import liquidation
        lid=(rec.get('recovery') or {}).get('liquidation_id')
        q=next((x for x in liquidation.load() if x.get('id')==lid),None)
        if not q or q.get('phase')!='completed': return []
        try:
            from manager import burn_nft, position_info, _ok_receipt
            try:
                b=burn_nft(rec['source_token_id'])
                if not _ok_receipt(b): raise RuntimeError('burn receipt failed')
            except Exception:
                try: position_info(rec['source_token_id'])
                except Exception: b=None
                else: raise
            update_pending(rec,'settled',exact_proceeds_raw=int(q['proceeds_raw']),liquidation_id=lid,next_retry=now,settlement={'liquidation_id':lid,'exact_proceeds_raw':int(q['proceeds_raw'])})
            return [{'action':'v3_liquidation_settled','proceeds_raw':int(q['proceeds_raw'])}]
        except Exception as exc:
            update_pending(rec,'liquidation_pending',last_error=f'post-liquidation burn: {str(exc)[:220]}')
            return [{'action':'v3_liquidation_burn_pending','error':str(exc)[:160]}]
    if rec.get('phase')=='closing':
        # A process died in a multi-transaction close. Never guess attribution or re-close.
        # Manager settlement records receipts on handled failures; an unhandled crash needs
        # receipt reconciliation before exact proceeds can safely be established.
        update_pending(rec,'recovery',last_error='interrupted close requires receipt reconciliation; no re-close attempted')
        return [{'action':'v3_to_v4_recovery','error':'interrupted close; funds untouched'}]
    if rec.get('phase')=='completed': return []
    if now>=int(rec.get('expires_at',0)):
        update_pending(rec,'expired',last_error='rotation expired; USDG retained')
        cd=_json(cfg.COOLDOWN_FILE,{}); cd[rec['target']['token'].lower()]=now+24*3600; _save_json(cfg.COOLDOWN_FILE,cd)
        return [{'action':'expired_usdg','token':rec['target']['token']}]
    if now<int(rec.get('next_retry',0)):return []
    try:
        exact=int(rec['exact_proceeds_raw']); revalidate_ready(rec['target'],{'token_id':rec['source_token_id']},exact,now)
        update_pending(rec,'opening',last_attempt_at=now)
        opened=enter_isolated_rotation(rec['target'],exact,rec,dry_run if dry_run is not None else cfg.DRY_RUN,now)
        if dry_run if dry_run is not None else cfg.DRY_RUN:return [opened]
        rec=update_pending(rec,'completed',open_result=opened,completed_at=now)
        archive=_json(cfg.V3_TO_V4_ARCHIVE_FILE,[]); archive.append(rec); atomic(cfg.V3_TO_V4_ARCHIVE_FILE,archive)
        cfg.ROTATION_REQUEST_FILE.unlink(missing_ok=True); cfg.ROTATION_READY_FILE.unlink(missing_ok=True)
        return [{'action':'v3_to_v4_completed','token':rec['target']['token'],'token_id':opened.get('tokenId')}]
    except Exception as exc:
        rec=load_pending() or rec; retries=int(rec.get('retries',0))+1
        update_pending(rec,'settled',retries=retries,last_error=str(exc)[:300],next_retry=now+cfg.V3_TO_V4_RETRY_SECONDS)
        if 'GMGN unsafe' in str(exc):
            cd=_json(cfg.COOLDOWN_FILE,{}); cd[rec['target']['token'].lower()]=now+24*3600; _save_json(cfg.COOLDOWN_FILE,cd)
        return [{'action':'v3_to_v4_pending','error':str(exc)[:160]}]

def manage(dry_run=None):
    # Finalize two-stage V4 closes once the canonical liquidation WAL is settled.
    # This removes stale NFT state and unblocks new entries only after exact USDG
    # proceeds have been reconciled.
    import liquidation
    positions=load(); liqs={r.get('id'):r for r in liquidation.load()}
    for rec in list(close_wal._load().values()):
        q=liqs.get(rec.get('liquidation_id'))
        if rec.get('nftGone') and q and q.get('phase')=='completed':
            # Reconciliation may already include liquidation proceeds in the
            # observed USDG delta; never add them twice.
            total=max(int(rec.get('direct_usdg_delta_raw',0)),int(rec.get('total_usdg_proceeds_raw',0)),int(q.get('proceeds_raw',0)))
            if not rec.get('settlementComplete') or int(rec.get('total_usdg_proceeds_raw',0))!=total:
                close_wal.update(rec,settlementComplete=True,phase='completed',liquidation_usdg_raw=int(q.get('proceeds_raw',0)),total_usdg_proceeds_raw=total)
            for key,pos in list(positions.items()):
                if str(pos.get('token_id'))==str(rec.get('nft_id')): positions.pop(key,None)
            save(positions)
    positions=load()
    if not positions:return retry_reentry(dry_run)
    rows={str(x['tokenId']):x for x in v4.list_positions()}; actions=[]; now=time.time(); history=load_history()
    for key,pos in list(positions.items()):
        row=rows.get(str(pos['token_id']))
        if not row:
            pending_close=next((r for r in close_wal._load().values() if str(r.get('nft_id'))==str(pos['token_id']) and not r.get('settlementComplete')),None)
            if pending_close:
                actions.append({'token':key,'action':'settlement_pending','liquidation_id':pending_close.get('liquidation_id')})
            else:
                actions.append({'token':key,'error':'tracked V4 NFT absent; state retained'})
            continue
        value=Decimal(str(row.get('valueUsd',0))); fee=Decimal(str(row.get('feeUsd',0))); entry=Decimal(pos['entry_value_usd']); reason=None
        # Backfill a conservative token-side close estimate for legacy positions.
        # list_positions derives principal from the exact live NFT liquidity/range.
        # Keep a 5% haircut; this value is used only for reverse-route safety/WAL,
        # while the close executor independently regenerates exact principal minima.
        try:
            if str(row.get('sym0','')).upper()=='USDG': token_human=Decimal(str(row['amount1']))
            elif str(row.get('sym1','')).upper()=='USDG': token_human=Decimal(str(row['amount0']))
            else: token_human=Decimal(0)
            predicted=int(token_human*Decimal(10**c.erc20_decimals(pos['token']))*Decimal('0.95'))
            if predicted>0: pos['expected_close_token_raw']=predicted
        except Exception:
            pass
        if entry and value<entry*(Decimal(1)-cfg.STOP_LOSS_PCT/100): reason='stop_loss'
        risk=gmgn_assess(pos['token'],force=True); liquidity=row.get('liquidity'); source='stateview'
        if liquidity is None: liquidity=risk.get('liquidity_usd',risk.get('liquidity')); source='gmgn'
        tick=row.get('tick',row.get('currentTick'))
        hist=policy.append_snapshot(history,key,{'tick':tick,'valueUsd':str(value),'feeUsd':str(fee),'liquidity':liquidity,'liquiditySource':source},now)
        prev_fee=Decimal(str(pos.get('accounting_last_fee_usdg','0'))); gross=Decimal(str(pos.get('gross_fees_usdg','0')))
        if fee>=prev_fee: gross+=fee-prev_fee
        dt=max(0,min(600,int(now)-int(pos.get('accounting_last_ts',now))))
        pos.update(accounting_last_fee_usdg=str(fee),gross_fees_usdg=str(gross),accounting_last_ts=int(now),nav_usdg=str(value+Decimal(str(pos.get('isolated_strategy_reserve_usdg','0')))),nav_vs_principal_usdg=str(value+Decimal(str(pos.get('isolated_strategy_reserve_usdg','0')))-Decimal(str(pos.get('principal_usdg',entry)))))
        if row.get('inRange'): pos['time_in_range_seconds']=int(pos.get('time_in_range_seconds',0))+dt
        if tick is not None and pos.get('tick_lower') is not None and pos.get('tick_upper') is not None:
            distance,klass,edge=policy.tick_distance(tick,pos['tick_lower'],pos['tick_upper']); elapsed=policy.continuous_timer(pos,klass,now)
            m=policy.metrics(hist,now); width,mode=policy.range_width(m.get('realized_vol_1h_pct')); blocked=[]; allowed=False
            if klass!='in_range' and now<int(pos.get('rebalance_cooldown_until',0)): blocked=['cooldown']
            elif klass in ('shallow','medium'):
                if cfg.STRATEGY_MODE == 'stable_first_exit':
                    blocked=['stable_first_no_recenter']
                else:
                    confirm=cfg.V4_SHALLOW_CONFIRM_SECONDS if klass=='shallow' else cfg.V4_MEDIUM_CONFIRM_SECONDS
                    if elapsed<confirm: blocked=['confirmation']
                    else:
                        cost=None
                        try:
                            raw=int(value*10**cfg.USDG_DECIMALS); pf=v4.route_preflight(pos['token'],raw); cost=policy.execution_cost_from_preflight(raw,pf,cfg.USDG_DECIMALS)
                        except Exception: pass
                        econ,_=policy.economics(hist,cost); allowed,blocked=policy.evidence_gates(klass,m,risk,econ)
                        if allowed: actions.append(rebalance(key,dict(pos),dry_run if dry_run is not None else cfg.DRY_RUN,now,Decimal('.75') if klass=='medium' else Decimal('1'),width or 25,mode or 'normal')); continue
            elif klass=='deep':
                if elapsed<cfg.V4_DEEP_CONFIRM_SECONDS: blocked=['deep_confirmation']
                elif not risk.get('ok') or risk.get('hard_stop'): allowed=True; actions.append(deep_exit(key,dict(pos),int(now),dry_run if dry_run is not None else cfg.DRY_RUN,True)); continue
                else:
                    try: v4.route_preflight(pos['token'],int(value*10**cfg.USDG_DECIMALS)); allowed=True
                    except Exception: blocked=['sell_preflight_missing']
                    if allowed: actions.append(deep_exit(key,dict(pos),int(now),dry_run if dry_run is not None else cfg.DRY_RUN)); continue
            policy.log_decision({'timestamp':now,'token':key,'tick':tick,'distance_pct':distance,'classification':klass,'edge_warning_pct':edge,'elapsed':elapsed,'metrics':m,'allowed':allowed,'blocked':blocked,'liquidity_source':source})
        if now-pos.get('fee_baseline_ts',now)>=cfg.FEE_EVAL_WINDOW_HOURS*3600:
            delta=fee-Decimal(pos.get('last_fee_usd','0'))
            if cfg.STRATEGY_MODE != 'stable_first_exit' and delta<cfg.MIN_FEE_3H_USDG: reason='low_fee_rotation'
            pos.update(fee_baseline_ts=int(now),last_fee_usd=str(fee))
        if reason:
            # close_position reloads durable state; persist live conservative metadata first.
            positions[key]=pos; save(positions)
            actions.append(close_position(key,reason,dry_run)); continue
        if fee>=cfg.HARVEST_MIN_USDG: actions.append({'dry_run':True,'action':'collect','token_id':pos['token_id']} if (dry_run if dry_run is not None else cfg.DRY_RUN) else v4.collect(pos['token_id']))
    current=load()
    for key,pos in positions.items():
        if key in current: current[key]=pos
    save(current); save_history(history); actions.extend(retry_reentry(dry_run,now)); return actions
