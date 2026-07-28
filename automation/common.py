"""Common helpers: RPC pool, wallet, tx signing, ERC20, quoting."""
import json, time, os, sys, math, fcntl, functools, contextlib
from decimal import Decimal
from pathlib import Path
import requests
from web3 import Web3
from eth_abi import encode, decode
from eth_utils import keccak
sys.path.insert(0, str(Path(__file__).parent))
from config import (RPC_URLS, WALLET_DIR, WALLET_ADDRESS, CHAIN_ID,
                    V3_FACTORY, V3_POSITION_MANAGER, V3_QUOTER, V3_SWAP_ROUTER,
                    WETH, USDG, USDG_DECIMALS, MAX_GAS_PRICE_GWEI, KILL_SWITCH)

_w3_cache: dict = {}
WALLET_LOCK = Path('/root/.hermes/state/rh_meme_lp/wallet.lock')
@contextlib.contextmanager
def wallet_lock(check_nonce=True):
    """Cross-process wallet mutex usable by daemons and transaction helpers."""
    WALLET_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with WALLET_LOCK.open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if check_nonce:
            check_pending_nonce()
        old=os.environ.get('RH_WALLET_LOCK_HELD'); os.environ['RH_WALLET_LOCK_HELD']='1'
        try:
            yield
        finally:
            if old is None: os.environ.pop('RH_WALLET_LOCK_HELD',None)
            else: os.environ['RH_WALLET_LOCK_HELD']=old

def wallet_locked(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with wallet_lock():
            return fn(*args, **kwargs)
    return wrapped
def w3():
    for rpc in RPC_URLS:
        if rpc not in _w3_cache:
            _w3_cache[rpc] = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 20}))
        try:
            _w3_cache[rpc].eth.block_number
            return _w3_cache[rpc]
        except Exception:
            continue
    raise RuntimeError('all_rpcs_down')

def rpc_call(method, params, attempts=3):
    last=None
    for a in range(attempts):
        for r in RPC_URLS:
            try:
                x = requests.post(r, json={'jsonrpc':'2.0','id':1,'method':method,'params':params}, timeout=15)
                if x.status_code!=200: last=f'http {x.status_code}'; continue
                j = x.json()
                if 'error' in j: last=str(j['error'])[:200]; continue
                return j['result']
            except Exception as e:
                last=str(e)[:150]
        time.sleep(0.5*(a+1))
    raise RuntimeError(f'rpc {method} failed: {last}')

def eth_call(to, data, block='latest', from_addr=None):
    tx = {'to': Web3.to_checksum_address(to), 'data': data}
    if from_addr:
        tx['from'] = Web3.to_checksum_address(from_addr)
    return rpc_call('eth_call', [tx, block])

def word_uint(n): return hex(int(n))[2:].rjust(64,'0')
def word_addr(a): return a.lower().replace('0x','').rjust(64,'0')

def load_key():
    p = WALLET_DIR / 'private_key.txt'
    return p.read_text().strip()

def account():
    return w3().eth.account.from_key(load_key())

def check_kill(quiet=False):
    if KILL_SWITCH.exists():
        # Normal kill blocks strategy, but permits bounded explicit journal recovery.
        if os.environ.get('RH_EXPLICIT_RECOVERY') == '1':
            hard=Path('/root/.hermes/state/rh_meme_lp/HARD_HALT_RECOVERY')
            if not hard.exists(): return False
        # Recurring cron ticks must treat an intentionally armed kill-switch as
        # a normal, silent no-op rather than a scheduler failure.
        if quiet:
            return True
        raise SystemExit(f'HALT: kill-switch file exists: {KILL_SWITCH}')
    return False

def eth_balance(addr=None):
    a = addr or WALLET_ADDRESS
    return int(rpc_call('eth_getBalance', [Web3.to_checksum_address(a), 'latest']), 16)

def erc20_balance(token, addr=None):
    a = addr or WALLET_ADDRESS
    r = eth_call(token, '0x70a08231' + word_addr(a))
    return int(r, 16)

def erc20_decimals(token):
    try: return int(eth_call(token, '0x313ce567'), 16)
    except Exception: return 18

def erc20_symbol(token):
    try:
        r = eth_call(token, '0x95d89b41'); b=bytes.fromhex(r[2:])
        if len(b)>=96: return decode(['string'], b)[0]
        return b.rstrip(b'\x00').decode('utf8','ignore')
    except Exception: return token[:8]

def erc20_allowance(token, owner, spender):
    d = '0xdd62ed3e' + word_addr(owner) + word_addr(spender)
    return int(eth_call(token, d), 16)

def get_gas_price():
    """Return (max_fee, priority_fee) for EIP-1559 or single gasPrice fallback."""
    try:
        blk = rpc_call('eth_getBlockByNumber', ['latest', False])
        base = int(blk.get('baseFeePerGas','0x0'), 16)
        # tip: 0.1 gwei minimum on RH (very low fees)
        tip = 100_000_000
        max_fee = base * 2 + tip
        max_gwei = Decimal(max_fee) / Decimal(10**9)
        if max_gwei > MAX_GAS_PRICE_GWEI:
            raise RuntimeError(f'gas too high: {max_gwei} gwei')
        return {'maxFeePerGas': max_fee, 'maxPriorityFeePerGas': tip}
    except RuntimeError:
        raise
    except Exception:
        gp = int(rpc_call('eth_gasPrice', []), 16)
        return {'gasPrice': gp}

def get_pool(token_a, token_b, fee):
    a, b = token_a.lower(), token_b.lower()
    t0, t1 = (a,b) if a<b else (b,a)
    data = '0x1698ee82' + word_addr(t0) + word_addr(t1) + word_uint(fee)
    try:
        r = eth_call(V3_FACTORY, data)
        p = '0x' + r[-40:]
        return None if p=='0x0000000000000000000000000000000000000000' else Web3.to_checksum_address(p)
    except Exception:
        return None

def pool_slot0(pool):
    """returns (sqrtPriceX96, tick, obs_idx, obs_card, obs_card_next, feeProtocol, unlocked)"""
    r = eth_call(pool, '0x3850c7bd')
    b = bytes.fromhex(r[2:])
    return decode(['uint160','int24','uint16','uint16','uint16','uint8','bool'], b)

def pool_liquidity(pool):
    r = eth_call(pool, '0x1a686502')  # liquidity()
    return int(r, 16)

def pool_token0(pool):
    return '0x' + eth_call(pool, '0x0dfe1681')[-40:]
def pool_token1(pool):
    return '0x' + eth_call(pool, '0xd21220a7')[-40:]
def pool_fee(pool):
    return int(eth_call(pool, '0xddca3f43'), 16)
def pool_tick_spacing(pool):
    return int(eth_call(pool, '0xd0c93a7c'), 16)

def sqrt_price_to_price(sqrtPX96, dec0, dec1):
    """price = token1/token0 (unit: 1 whole token1 per 1 whole token0)"""
    p = (Decimal(sqrtPX96) / Decimal(2**96)) ** 2
    return p * Decimal(10**dec0) / Decimal(10**dec1)

def tick_to_price(tick, dec0, dec1):
    p = Decimal(str(1.0001 ** tick))
    return p * Decimal(10**dec0) / Decimal(10**dec1)

def price_to_tick(price, dec0, dec1):
    p = Decimal(price) * Decimal(10**dec1) / Decimal(10**dec0)
    import math as m
    return int(m.log(float(p)) / m.log(1.0001))

def nearest_usable_tick(tick, spacing):
    return (int(tick) // spacing) * spacing

def check_pending_nonce():
    acct = account()
    latest = int(rpc_call('eth_getTransactionCount', [acct.address, 'latest']), 16)
    pending = int(rpc_call('eth_getTransactionCount', [acct.address, 'pending']), 16)
    if latest != pending:
        raise RuntimeError(f'pending_nonce_stuck latest={latest} pending={pending}')
    return latest

def build_and_send(tx_dict, gas_limit=None):
    """Sign, simulate via eth_call, then broadcast. Enforces one-pending-nonce."""
    check_kill()
    nonce = check_pending_nonce()
    acct = account()
    fee_params = get_gas_price()
    # simulate first
    sim = {
        'from': acct.address,
        'to': Web3.to_checksum_address(tx_dict['to']),
        'value': hex(int(tx_dict.get('value', 0))),
        'data': (tx_dict.get('data', '0x') if tx_dict.get('data','0x').startswith('0x') else '0x'+tx_dict.get('data','0x')),
    }
    try:
        rpc_call('eth_call', [sim, 'latest'])
    except Exception as e:
        raise RuntimeError(f'simulate_revert: {str(e)[:200]}')
    # estimate gas
    if gas_limit is None:
        try:
            gas_limit = int(rpc_call('eth_estimateGas', [sim]), 16)
        except Exception as e:
            raise RuntimeError(f'estimate_gas: {str(e)[:200]}')
    tx = {
        'chainId': CHAIN_ID,
        'from': acct.address,
        'to': Web3.to_checksum_address(tx_dict['to']),
        'value': int(tx_dict.get('value', 0)),
        'data': tx_dict.get('data', '0x'),
        'nonce': nonce,
        'gas': int(gas_limit * 1.3) + 30000,
        **fee_params,
    }
    if 'maxFeePerGas' in tx: tx['type'] = 2
    signed = acct.sign_transaction(tx)
    h = w3().eth.send_raw_transaction(signed.raw_transaction).hex()
    if not h.startswith('0x'): h = '0x'+h
    rec = w3().eth.wait_for_transaction_receipt(h, timeout=180)
    return {'hash': h, 'status': rec.status, 'gasUsed': rec.gasUsed, 'block': rec.blockNumber}

def approve_if_needed(token, spender, amount):
    """Approve token to spender if allowance < amount. Returns tx hash or None."""
    cur = erc20_allowance(token, WALLET_ADDRESS, spender)
    if cur >= amount:
        return None
    # approve(spender, MAX)
    MAX = 2**256 - 1
    data = '0x095ea7b3' + word_addr(spender) + word_uint(MAX)
    tx = {'to': token, 'data': data, 'value': 0}
    return build_and_send(tx)

def quote_v3_exact_input_single(token_in, token_out, fee, amount_in):
    """Quote via SwapRouter eth_call sim (works with hooked tokens where Quoter reverts).
    Requires the wallet to have USDG allowance to router; but eth_call is read-only so no gas.
    Returns amountOut in token_out units.
    """
    # first try QuoterV2
    sig = keccak(text='quoteExactInputSingle((address,address,uint256,uint24,uint160))')[:4]
    data = '0x' + (sig + encode(
        ['(address,address,uint256,uint24,uint160)'],
        [(Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out),
          int(amount_in), int(fee), 0)]
    )).hex()
    try:
        r = eth_call(V3_QUOTER, data)
        return int(decode(['uint256','uint160','uint32','uint256'], bytes.fromhex(r[2:]))[0])
    except Exception:
        pass
    # Fallback: simulate real swap via SwapRouter02 sig on our router
    from config import V3_SWAP_ROUTER, WALLET_ADDRESS
    swap_data = '0x04e45aaf' + encode(
        ['(address,address,uint24,address,uint256,uint256,uint160)'],
        [(Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out),
          int(fee), Web3.to_checksum_address(WALLET_ADDRESS),
          int(amount_in), 0, 0)]
    ).hex()
    r = rpc_call('eth_call', [{
        'to': Web3.to_checksum_address(V3_SWAP_ROUTER),
        'from': Web3.to_checksum_address(WALLET_ADDRESS),
        'value': '0x0',
        'data': swap_data,
        'gas': '0x200000',
    }, 'latest'])
    return int(r, 16)
