"""Key-free preflight bridge for the parallel WETH/token atomic V4 path.

Execution is deliberately not exposed until ATOMIC_V4_WETH_EXECUTOR_ADDRESS is configured;
the existing USDG backend remains untouched.
"""
import json, os, subprocess, tempfile
from pathlib import Path
import config as cfg

ROOT=Path('/root/money-printer-repos/robinhood-lp-bot')
CLI=ROOT/'src/atomic-v4-weth-cli.ts'


def _read_only(command, *args, timeout=420):
    env=os.environ.copy()
    env.pop('RH_WALLET_KEY', None)
    env.update(RH_RPC_URL=cfg.RPC_URLS[0], KYBERSWAP_ROUTER_ADDRESS='0x6131B5fae19EA4f9D964eAc0408E4408b66337b5', KYBERSWAP_CHAIN='robinhood')
    if cfg.ATOMIC_V4_WETH_EXECUTOR_ADDRESS:
        env['ATOMIC_V4_WETH_EXECUTOR_ADDRESS']=cfg.ATOMIC_V4_WETH_EXECUTOR_ADDRESS
    p=subprocess.run(['node','--import','tsx',str(CLI),command,*map(str,args)],cwd=ROOT,env=env,text=True,capture_output=True,timeout=timeout)
    lines=(p.stdout if p.returncode==0 else p.stderr).strip().splitlines()
    try: data=json.loads(lines[-1])
    except Exception: raise RuntimeError('WETH V4 CLI malformed output')
    if p.returncode or not data.get('ok'): raise RuntimeError('WETH V4 CLI: '+str(data.get('error','failed')))
    return data['result']


def capability_preflight(token, weth_raw, pool_id):
    """Generate an immutable candidate-bound plan without reading a signing key."""
    if not pool_id: raise ValueError('candidate poolId is required')
    if int(weth_raw)<=0: raise ValueError('WETH amount must be positive')
    if not cfg.ATOMIC_V4_WETH_EXECUTOR_ADDRESS:
        raise RuntimeError('ATOMIC_V4_WETH_EXECUTOR_ADDRESS required for executable calldata')
    with tempfile.NamedTemporaryFile(suffix='.json') as f:
        plan=_read_only('generate-open',token,int(weth_raw),f.name,500,pool_id)
    pool=plan.get('pool') or {}
    if str(plan.get('token','')).lower()!=str(token).lower(): raise RuntimeError('plan token mismatch')
    if str(pool.get('poolId','')).lower()!=str(pool_id).lower(): raise RuntimeError('plan poolId mismatch')
    if int(plan.get('wethAmount',0))!=int(weth_raw): raise RuntimeError('plan WETH amount mismatch')
    return {'capable':True,'broadcast':False,'settlement_asset':'WETH','settlement_token':cfg.WETH,
            'poolId':pool['poolId'],'token':plan['token'],'weth_raw':int(weth_raw),'plan':plan}


def state_fields(pool_id):
    return {'settlement_asset':'WETH','settlement_token':cfg.WETH,'settlement_decimals':cfg.WETH_DECIMALS,
            'close_settlement_asset':'WETH','unwrap_native':False,'poolId':pool_id}
