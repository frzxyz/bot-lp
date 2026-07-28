"""Hybrid Uniswap V4 backend backed by the audited FlipZ3ro TypeScript SDK implementation.

Read-only calls never load the wallet key. Execution is opt-in and injects the key only into
child-process environment; stdout/stderr are scrubbed and the key is never printed.
"""
import json, os, subprocess, fcntl
from contextlib import contextmanager
from pathlib import Path

ROOT = Path('/root/money-printer-repos/robinhood-lp-bot')
CLI = ROOT / 'src/v4-cli.ts'
WALLET_KEY = Path('/root/.hermes/wallets/meme-lp-agent/private_key.txt')
ZERO = '0x0000000000000000000000000000000000000000'
DYNAMIC_FEE_FLAG = 0x800000
# Conservative initial policy: vanilla V4 pools only. Add audited hook addresses explicitly.
HOOK_ALLOWLIST_FILE = Path('/root/.hermes/state/rh_meme_lp/v4_hook_allowlist.json')
LOCK_FILE = Path('/root/.hermes/state/rh_meme_lp/wallet.lock')

@contextmanager
def wallet_lock():
    if os.environ.get('RH_WALLET_LOCK_HELD') == '1':
        yield
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open('a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _allowlist():
    try:
        return {x.lower() for x in json.loads(HOOK_ALLOWLIST_FILE.read_text())}
    except Exception:
        return {ZERO}


def hook_allowed(hook):
    return str(hook).lower() in _allowlist()


def _run(args, execute=False, timeout=90, max_attempts=None):
    env = os.environ.copy()
    # Read-only commands must not inherit a parent key accidentally.
    env.pop('RH_WALLET_KEY', None)
    env['RH_RPC_URL'] = 'https://rpc.mainnet.chain.robinhood.com'
    # Public Kyber quote configuration is required for both read-only and execution paths.
    env['KYBERSWAP_ROUTER_ADDRESS'] = '0x6131B5fae19EA4f9D964eAc0408E4408b66337b5'
    env['KYBERSWAP_CHAIN'] = 'robinhood'
    env['RH_V4_EXPECTED_WALLET'] = '0x3582605Edebf376b684a45E8Faa6D808C22a8e3e'
    if execute:
        # The TS governor independently enforces chain, wallet, kill switch, and amount cap.
        env['RH_WALLET_KEY'] = WALLET_KEY.read_text().strip()
        env['RH_V4_EXECUTION'] = 'I_ACKNOWLEDGE_SAFE_V4_EXECUTION'
        env['RH_V4_EXPECTED_WALLET'] = '0x3582605Edebf376b684a45E8Faa6D808C22a8e3e'
        env['RH_V4_MAX_ETH'] = os.environ.get('RH_V4_MAX_ETH', '0.00015')
        env['RH_V4_MAX_USDG_RAW'] = os.environ.get('RH_V4_MAX_USDG_RAW', '250000000')
        # Kyber public routing configuration was set above for read-only parity.
        from common import check_pending_nonce
        check_pending_nonce()
    cmd = ['node', '--import', 'tsx', str(CLI), *map(str,args)]
    last = 'no response'
    # Blockscout occasionally returns an empty log page; retry read-only operations.
    attempts = max_attempts if max_attempts is not None else (1 if execute else 3)
    for _ in range(attempts):
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        lines = [x for x in r.stdout.splitlines() if x.strip().startswith('{')]
        if not r.returncode and lines:
            data = json.loads(lines[-1])
            if data.get('ok'):
                return data
            last = data.get('error', 'v4 error')
        else:
            last = (r.stderr.splitlines()[-1] if r.stderr else 'no JSON output')[:300]
    # Never include environment or command in errors.
    raise RuntimeError(f'v4 sidecar failed: {last}')


def discover(token, include_unapproved=False):
    rows=[]
    for _ in range(3):
        rows = _run(['discover', token], max_attempts=1).get('pools', [])
        if rows: break
    for p in rows:
        pk=p.get('poolKey') or {}
        p['hook_allowed'] = hook_allowed(pk.get('hooks', ZERO))
        p['dynamic_fee'] = int(p.get('fee',0)) >= DYNAMIC_FEE_FLAG
        p['eligible'] = (not p['dynamic_fee']) and p['hook_allowed'] and int(p.get('liquidity',0)) > 0
    return rows if include_unapproved else [p for p in rows if p['eligible']]


def discover_pair(token, settlement, include_unapproved=False):
    """Key-free direct ERC20/ERC20 V4 discovery (USDG or WETH)."""
    rows = _run(['discover-pair', token, settlement]).get('pools', [])
    for p in rows:
        pk=p.get('poolKey') or {}
        p['hook_allowed'] = hook_allowed(pk.get('hooks', ZERO))
        p['dynamic_fee'] = int(p.get('fee',0)) >= DYNAMIC_FEE_FLAG
        p['eligible'] = (not p['dynamic_fee']) and p['hook_allowed'] and int(p.get('liquidity',0)) > 0
        p['settlement_asset'] = settlement
    return rows if include_unapproved else [p for p in rows if p['eligible']]


def quote(token, amount_usdg_raw=1_000_000, timeout=90, max_attempts=None):
    # A successful sidecar response may still contain [] when Blockscout serves an
    # intermittent empty log page. Retry empty payloads as well as command errors.
    attempts=max_attempts if max_attempts is not None else 3
    rows=[]
    for _ in range(max(1,attempts)):
        data=_run(['quote',token,str(amount_usdg_raw)], timeout=timeout, max_attempts=1)
        rows=data.get('quotes',[])
        if rows: break
    out=[]
    for q in rows:
        p=q.get('pool') or {}; pk=p.get('poolKey') or {}
        allowed=hook_allowed(pk.get('hooks',ZERO))
        q['eligible']=allowed and int(p.get('fee',0))<DYNAMIC_FEE_FLAG and 'amountOut' in q
        out.append(q)
    return out


def quote_exit(token, amount_token_raw, timeout=90, max_attempts=3):
    rows=[]
    for _ in range(max(1,max_attempts)):
        rows=_run(['quote-exit',token,str(int(amount_token_raw))],timeout=timeout,max_attempts=1).get('quotes',[])
        if rows: break
    out=[]
    for q in rows:
        p=q.get('pool') or {}; pk=p.get('poolKey') or {}
        q['eligible']=int(p.get('liquidity',0))>0 and 0<int(p.get('fee',0))<=100000 and 'amountOut' in q
        out.append(q)
    return out


def swap_exit(token, amount_token_raw, minimum_raw):
    with wallet_lock():
        return _run(['swap-exit',token,str(int(amount_token_raw)),str(int(minimum_raw))],execute=True,timeout=300).get('result')


def quote_path(token, amount_token_raw, timeout=120):
    return _run(['quote-path',token,str(int(amount_token_raw))],timeout=timeout,max_attempts=2).get('quotes',[])


def swap_path(token, amount_token_raw, minimum_raw, first_pool_id, second_pool_id):
    with wallet_lock():
        return _run(['swap-path',token,str(int(amount_token_raw)),str(int(minimum_raw)),first_pool_id,second_pool_id],execute=True,timeout=300).get('result')


def quote_v2(token, amount_token_raw, timeout=90):
    return _run(['quote-v2',token,str(int(amount_token_raw))],timeout=timeout,max_attempts=2).get('quotes',[])


def swap_v2(token, amount_token_raw, minimum_raw, route_id):
    with wallet_lock():
        return _run(['swap-v2',token,str(int(amount_token_raw)),str(int(minimum_raw)),route_id],execute=True,timeout=300).get('result')


def route_preflight(token, amount_usdg_raw):
    return _run(['preflight', token, str(int(amount_usdg_raw))], timeout=90)

def reverse_preflight(token, amount_token_raw, quote_only=False):
    cmd='reverse-quote' if quote_only else 'reverse-preflight'
    return _run([cmd,token,str(int(amount_token_raw))], execute=not quote_only, timeout=90).get('proof')


def list_positions(): return _run(['list'], execute=True).get('positions',[])

def collect(token_id):
    return _run(['collect', str(token_id)], execute=True, timeout=180).get('result')

def close(token_id):
    return _run(['close', str(token_id)], execute=True, timeout=240).get('result')

def close_usdg(token_id):
    with wallet_lock():
        return _run(['close-usdg', str(token_id)], execute=True, timeout=360).get('result')

def retry_settlement(token_id):
    with wallet_lock():
        return _run(['retry-settlement', str(token_id)], execute=True, timeout=300).get('result')

def open_usdg(token, eth_amount):
    return _run(['open-usdg', token, str(eth_amount)], execute=True, timeout=300).get('result')

def open_usdg_single(token, usdg_raw):
    return _run(['open-usdg-single', token, str(int(usdg_raw))], execute=True, timeout=300).get('result')

def open_usdg_kyber(token, usdg_raw, width_percent=None):
    with wallet_lock():
        args=['open-usdg-kyber', token, str(int(usdg_raw))]
        if width_percent is not None: args.append(str(int(width_percent)))
        return _run(args, execute=True, timeout=360).get('result')

if __name__=='__main__':
    import sys
    token=sys.argv[1]
    print(json.dumps({'pools':discover(token,True),'quotes':quote(token)},indent=2))
