"""Fail-closed, journaled bridge to the TS AtomicV3Executor sidecar."""
import json, os, subprocess
from pathlib import Path
import config as cfg
import common as c
import lifecycle
ROOT=Path('/root/money-printer-repos/robinhood-lp-bot'); CLI=ROOT/'src/atomic-cli.ts'
def _run(command,*args,width_pct=None):
    if not cfg.ATOMIC_LP_ONLY: raise RuntimeError('atomic backend requires ATOMIC_LP_ONLY=true')
    # Cron environments intentionally do not export the signer key. Load it from the
    # established wallet credential store only into the child process environment;
    # never put it in argv or diagnostic output.
    env=dict(os.environ,ATOMIC_LP_ONLY='true',ATOMIC_EXECUTOR_ADDRESS=cfg.ATOMIC_EXECUTOR_ADDRESS,
             KYBERSWAP_ROUTER_ADDRESS=cfg.KYBERSWAP_ROUTER_ADDRESS,RH_WALLET_KEY=c.load_key())
    # Without this the executor silently falls back to its own ATOMIC_RANGE_PCT
    # default, so every measured/configured width was being discarded.
    if width_pct is not None: env['ATOMIC_RANGE_PCT']=str(width_pct)
    p=subprocess.run(['node','--import','tsx',str(CLI),command,*map(str,args)],cwd=ROOT,env=env,text=True,capture_output=True)
    try: data=json.loads((p.stdout if p.returncode==0 else p.stderr).strip().splitlines()[-1])
    except Exception: raise RuntimeError(('atomic CLI malformed output: '+p.stderr)[:500])
    if p.returncode or not data.get('ok'): raise RuntimeError('atomic CLI: '+str(data.get('error','failed')))
    return data['result']
def _execute(operation, token=None, token_id=None, amount_raw=None, width_pct=None):
    lifecycle.assert_new_strategy_allowed('v3')
    before={'usdg_raw':c.erc20_balance(cfg.USDG)}
    if token: before['token_raw']=c.erc20_balance(token)
    op=lifecycle.prepare('v3',operation,token=token,nft=token_id,before=before,
      caps={'usdg_raw':int(amount_raw or 0)},expected={'atomic_receipt_status':1},
      recovery_policy='reconcile_receipt_nft_event_balances;never_compensate_revert')
    try:
        if operation=='open': _run('build_open',token,amount_raw,width_pct=width_pct)
        else: _run('build_close',token_id)
        op=lifecycle.transition(op,'preflight_passed',changes={'metadata':{'preflight':True,'executor':cfg.ATOMIC_EXECUTOR_ADDRESS,'range_pct':width_pct}})
        op=lifecycle.transition(op,'broadcasting')
        result=_run('execute_'+operation,token,amount_raw,width_pct=width_pct) if operation=='open' else _run('execute_close',token_id)
        rc=result.get('receipt') or {}; h=rc.get('hash')
        if not h: raise RuntimeError('atomic executor receipt identity missing')
        op=lifecycle.transition(op,'confirming',tx={'hash':h,'status':rc.get('status')})
        if int(rc.get('status',0))!=1: raise RuntimeError('atomic executor reverted')
        op=lifecycle.transition(op,'postcheck')
        lifecycle.transition(op,'completed',changes={'result':{'event':result.get('event')}})
        return result
    except BaseException as exc:
        if isinstance(exc,(KeyboardInterrupt,SystemExit)): raise
        lifecycle.fail(op,exc); raise
def open_position(token,amount_raw,width_pct=None): return _execute('open',token=token,amount_raw=amount_raw,width_pct=width_pct)
def close_position(token_id): return _execute('close',token_id=token_id)
def kyber_liquidate(token,amount_raw,min_out_raw):
    return _run('execute_liquidation',token,amount_raw,min_out_raw)
