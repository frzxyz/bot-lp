"""Fail-closed, journaled bridge to AtomicV4Executor; never falls back to split V4 calls."""
import json, os, subprocess, tempfile, time
from pathlib import Path
import config as cfg
import common as c
import lifecycle
import liquidation
ROOT=Path('/root/money-printer-repos/robinhood-lp-bot'); CLI=ROOT/'src/atomic-v4-cli.ts'
WALLET_KEY=Path('/root/.hermes/wallets/meme-lp-agent/private_key.txt')

def _run(command,*args,width_pct=None):
    if not cfg.ATOMIC_LP_ONLY: raise RuntimeError('atomic V4 backend requires ATOMIC_LP_ONLY=true')
    env=dict(os.environ,ATOMIC_LP_ONLY='true',ATOMIC_V4_EXECUTOR_ADDRESS=cfg.ATOMIC_V4_EXECUTOR_ADDRESS,
             RH_RPC_URL='https://rpc.mainnet.chain.robinhood.com',
             RH_WALLET_KEY=WALLET_KEY.read_text().strip(),
             RH_V4_EXPECTED_WALLET=cfg.WALLET_ADDRESS,
             KYBERSWAP_ROUTER_ADDRESS='0x6131B5fae19EA4f9D964eAc0408E4408b66337b5',
             KYBERSWAP_CHAIN='robinhood')
    # The plan generator otherwise anchors on a fixed count of tick spacings, whose
    # economic width swings with the fee tier and ignores the token's volatility.
    if width_pct is not None: env['RH_V4_RANGE_PCT']=str(width_pct)
    p=subprocess.run(['node','--import','tsx',str(CLI),command,*map(str,args)],cwd=ROOT,env=env,text=True,capture_output=True,timeout=420)
    raw=(p.stdout if p.returncode==0 else p.stderr).strip().splitlines()
    try: data=json.loads(raw[-1])
    except Exception: raise RuntimeError(('atomic V4 CLI malformed output: '+p.stderr)[:500])
    if p.returncode or not data.get('ok'): raise RuntimeError('atomic V4 CLI: '+str(data.get('error','failed')))
    return data['result']

def status():
    """Read-only executor introspection. Signs nothing and moves nothing."""
    return _run('status')

def capability_preflight(token,amount_raw,pool_id=None,width_pct=None):
    """Build and validate a candidate-bound plan without broadcasting.

    Before the source V3 close, the wallet intentionally does not yet hold the exact
    settlement USDG, so an eth_call would false-fail on balance/allowance. The final
    execute-open command regenerates one fresh immutable plan and performs exact
    eth_call + estimateGas before any broadcast after settlement.
    """
    last=None
    with tempfile.NamedTemporaryFile(suffix='.json') as f:
        args=[token,int(amount_raw),f.name,500]
        if pool_id: args.append(pool_id)
        # Explorer-backed discovery can return a successful empty page while the exact
        # StateView-backed quote is live. Retry only this read-only plan generation;
        # execution still regenerates and simulates once after settlement.
        for attempt in range(3):
            try:
                plan=_run('generate-open',*args,width_pct=width_pct)
                break
            except RuntimeError as exc:
                last=exc
                if 'no discovered live USDG-paired eligible V4 pool' not in str(exc) or attempt==2:
                    raise
                time.sleep(1)
        else:
            raise last or RuntimeError('atomic V4 plan unavailable')
    if str(plan.get('token','')).lower()!=str(token).lower(): raise RuntimeError('atomic V4 plan token mismatch')
    if int(plan.get('usdgAmount',0))!=int(amount_raw): raise RuntimeError('atomic V4 plan budget mismatch')
    pool=plan.get('pool') or {}
    if pool_id and str(pool.get('poolId','')).lower()!=str(pool_id).lower(): raise RuntimeError('atomic V4 plan pool mismatch')
    if not plan.get('swapData') or not plan.get('mintData'): raise RuntimeError('atomic V4 plan calldata missing')
    return {'capable':True,'token':plan['token'],'poolId':pool.get('poolId'),'budget_raw':int(amount_raw)}

def open_position(token,amount_raw,pool_id=None,width_pct=None):
    lifecycle.assert_new_strategy_allowed('v4')
    amount_raw=int(amount_raw)
    if amount_raw<=0: raise RuntimeError('atomic V4 budget must be positive')
    before={'usdg_raw':c.erc20_balance(cfg.USDG),'token_raw':c.erc20_balance(token)}
    op=lifecycle.prepare('v4','open',token=token,before=before,caps={'usdg_raw':amount_raw},expected={'atomic_receipt_status':1,'unique_opened_event':True,'executor_zero_balances':True},recovery_policy='reconcile_receipt_nft_event_balances;never_compensate_revert')
    try:
        proof=capability_preflight(token,amount_raw,pool_id,width_pct)
        op=lifecycle.transition(op,'preflight_passed',changes={'metadata':{'executor':cfg.ATOMIC_V4_EXECUTOR_ADDRESS,'capability':proof,'range_pct':width_pct}})
        op=lifecycle.transition(op,'broadcasting')
        result=None
        for attempt in range(3):
            try:
                result=_run('execute-open',token,amount_raw,*([pool_id] if pool_id else []),width_pct=width_pct)
                break
            except RuntimeError as exc:
                # This exact error is raised during read-only pool discovery before
                # eth_call, nonce allocation, or broadcast, so bounded retry is safe.
                if 'no discovered live USDG-paired eligible V4 pool' not in str(exc) or attempt==2:
                    raise
                time.sleep(1)
        if result is None: raise RuntimeError('atomic V4 open unavailable')
        rc=result.get('receipt') or {}; ev=result.get('event') or {}
        if int(rc.get('status',0))!=1 or not rc.get('hash'): raise RuntimeError('atomic V4 receipt failed')
        op=lifecycle.transition(op,'confirming',tx={'hash':rc['hash'],'status':rc['status']})
        if ev.get('name')!='Opened' or not result.get('tokenId'): raise RuntimeError('atomic V4 Opened/NFT evidence missing')
        if any(int(x) for x in (result.get('executorBalances') or {}).values()): raise RuntimeError('atomic V4 executor residual balance')
        op=lifecycle.transition(op,'postcheck')
        lifecycle.transition(op,'completed',changes={'result':{'event':ev,'tokenId':result['tokenId']}})
        return result
    except BaseException as exc:
        if isinstance(exc,(KeyboardInterrupt,SystemExit)): raise
        lifecycle.fail(op,exc); raise

def close_position(token_id,token=None):
    lifecycle.assert_new_strategy_allowed('v4')
    before={'usdg_raw':c.erc20_balance(cfg.USDG)}
    if token: before['token_raw']=c.erc20_balance(token)
    op=lifecycle.prepare('v4','close',token=token,nft=token_id,before=before,
        expected={'atomic_receipt_status':1,'closed_event':True,'executor_zero_balances':True,
                  'settlement_asset':cfg.USDG},
        recovery_policy='reconcile_single_receipt_event_and_balances;never_resend_or_compensate',
        metadata={'executor':cfg.ATOMIC_V4_EXECUTOR_ADDRESS})
    try:
        op=lifecycle.transition(op,'preflight_passed',changes={'metadata':dict(op.get('metadata',{}),fresh_plan=True)})
        op=lifecycle.transition(op,'broadcasting')
        result=_run('execute-close',token_id)
        rc=result.get('receipt') or {}; tx_hash=rc.get('hash')
        if not tx_hash: raise RuntimeError('atomic V4 receipt identity missing')
        op=lifecycle.transition(op,'confirming',tx={'hash':tx_hash,'status':rc.get('status')},nonce=rc.get('nonce'))
        if int(rc.get('status',0)) != 1: raise RuntimeError('atomic V4 executor reverted')
        event=result.get('event') or {}; balances=result.get('executorBalances') or {}
        if event.get('name')!='Closed' or str(event.get('tokenId'))!=str(token_id): raise RuntimeError('Closed event mismatch')
        if int(balances.get('usdg',-1)) or int(balances.get('token',-1)): raise RuntimeError('executor residual balances')
        if not result.get('nftGone') or not result.get('removeCollectConfirmed'): raise RuntimeError('atomic remove/collect postconditions missing')
        op=lifecycle.transition(op,'postcheck')
        canonical_token=str(event.get('token') or result.get('token') or token or '')
        if not canonical_token: raise RuntimeError('Closed event token identity missing')
        if token and canonical_token.lower()!=str(token).lower(): raise RuntimeError('Closed event token mismatch')
        token_delta=int(result.get('sourceTokenDeltaRaw',(event or {}).get('tokenAmount',0)))
        q=None
        if token_delta:
            q=liquidation.enqueue(canonical_token,canonical_token[:10],c.erc20_decimals(canonical_token),token_delta,
                int(before.get('token_raw',0)),source_reason='v4_close',source_version='v4',
                source_token_id=str(token_id),venue_candidates=cfg.LIQUIDATION_VENUES)
            result['liquidationId']=q and q['id']
            result['settlementComplete']=False
        result['directUsdgRaw']=int(event.get('settlementAmount',0))
        result['totalUsdgProceedsRaw']=result['directUsdgRaw']+int((q or {}).get('proceeds_raw',0))
        if result['settlementComplete']:
            lifecycle.transition(op,'completed',changes={'result':result})
        return result
    except BaseException as exc:
        if isinstance(exc,(KeyboardInterrupt,SystemExit)): raise
        lifecycle.fail(op,exc); raise
