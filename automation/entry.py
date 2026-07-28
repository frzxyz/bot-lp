"""Entry: swap half USDG->meme, then mint V3 LP with ±RANGE_PCT.
Usage: python3 entry.py <token_address> [size_usdg]
"""
import sys, json, time, math
from decimal import Decimal, getcontext
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
getcontext().prec = 50

from eth_abi import encode
from eth_utils import keccak
from web3 import Web3

import common as c
import config as cfg

MINT_SEL   = '0x88316456'  # NPM.mint((address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256))
SWAP_SEL   = '0x04e45aaf'  # SwapRouter02.exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))

def find_pool(token):
    for fee in cfg.FEE_TIER_FALLBACKS:
        p = c.get_pool(cfg.USDG, token, fee)
        if p:
            liq = c.pool_liquidity(p)
            if liq > 0:
                # sanity: pool has USDG balance too
                bal = c.erc20_balance(cfg.USDG, p)
                if bal > 100_000:  # > $0.10 in pool
                    return p, fee
    raise RuntimeError('no active USDG pool')

def compute_ticks(pool, range_pct):
    s0 = c.pool_slot0(pool)
    tick = int(s0[1])
    spacing = c.pool_tick_spacing(pool)
    # ±range_pct => log(1±r/100)/log(1.0001) ≈ ln factor
    up = tick + int(math.log(1 + float(range_pct)/100) / math.log(1.0001))
    dn = tick + int(math.log(1 - float(range_pct)/100) / math.log(1.0001))
    up = c.nearest_usable_tick(up, spacing)
    dn = c.nearest_usable_tick(dn, spacing)
    return dn, up, tick, spacing

def get_price_usdg_per_token(pool, token, usdg_dec=6):
    """price = how many USDG per 1 token"""
    s0 = c.pool_slot0(pool)
    t0 = c.pool_token0(pool)
    tok_dec = c.erc20_decimals(token)
    if t0.lower() == cfg.USDG.lower():
        # token0=USDG, token1=token. price(token1/token0)=token per USDG raw. Need USDG per token.
        # sqrt_price_to_price returns adjusted price (token1 per token0 in whole units)
        tpu = c.sqrt_price_to_price(s0[0], usdg_dec, tok_dec)
        return Decimal(1)/tpu
    else:
        # token0=token, token1=USDG. price(token1/token0)=USDG per token adjusted
        return c.sqrt_price_to_price(s0[0], tok_dec, usdg_dec)

def swap_usdg_to_token(token, fee, amount_usdg_raw, min_out_raw):
    data = SWAP_SEL + encode(
        ['(address,address,uint24,address,uint256,uint256,uint160)'],
        [(Web3.to_checksum_address(cfg.USDG), Web3.to_checksum_address(token),
          int(fee), Web3.to_checksum_address(cfg.WALLET_ADDRESS),
          int(amount_usdg_raw), int(min_out_raw), 0)]
    ).hex()
    return c.build_and_send({'to': cfg.V3_SWAP_ROUTER, 'data': data, 'value': 0})

def mint_lp(token, fee, tick_lower, tick_upper, amt_usdg_raw, amt_tok_raw,
            min_usdg_raw, min_tok_raw):
    t0 = cfg.USDG if cfg.USDG.lower() < token.lower() else token
    t1 = token   if cfg.USDG.lower() < token.lower() else cfg.USDG
    if t0.lower() == cfg.USDG.lower():
        a0d, a1d = amt_usdg_raw, amt_tok_raw
        a0m, a1m = min_usdg_raw, min_tok_raw
    else:
        a0d, a1d = amt_tok_raw, amt_usdg_raw
        a0m, a1m = min_tok_raw, min_usdg_raw
    deadline = int(time.time()) + 600
    data = MINT_SEL + encode(
        ['(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)'],
        [(Web3.to_checksum_address(t0), Web3.to_checksum_address(t1), int(fee),
          int(tick_lower), int(tick_upper),
          int(a0d), int(a1d), int(a0m), int(a1m),
          Web3.to_checksum_address(cfg.WALLET_ADDRESS), int(deadline))]
    ).hex()
    return c.build_and_send({'to': cfg.V3_POSITION_MANAGER, 'data': data, 'value': 0})

def parse_mint_log(receipt_hash):
    """Fetch receipt, extract tokenId from IncreaseLiquidity or Transfer event."""
    r = c.rpc_call('eth_getTransactionReceipt', [receipt_hash])
    # Transfer(from=0, to=owner, tokenId) — ERC721 mint. topic0 keccak of Transfer(address,address,uint256)
    xfer = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
    for lg in r.get('logs') or []:
        if lg['address'].lower() != cfg.V3_POSITION_MANAGER.lower(): continue
        topics = lg.get('topics') or []
        if len(topics) < 4: continue
        if topics[0].lower() != xfer.lower(): continue
        # from should be 0x0000...
        if int(topics[1], 16) != 0: continue
        token_id = int(topics[3], 16)
        return token_id
    return None

@c.wallet_locked
def main():
    c.check_kill()
    token = sys.argv[1]
    size_usdg = Decimal(sys.argv[2]) if len(sys.argv) > 2 else cfg.POSITION_SIZE_USDG
    token = Web3.to_checksum_address(token)
    # 1) verify balances
    usdg_bal = c.erc20_balance(cfg.USDG)
    eth_bal = c.eth_balance()
    need_usdg = int(size_usdg * Decimal(10**cfg.USDG_DECIMALS))
    if usdg_bal < need_usdg:
        raise RuntimeError(f'insufficient USDG: have {usdg_bal/1e6}, need {size_usdg}')
    if eth_bal < int(cfg.GAS_RESERVE_ETH * Decimal(10**18)):
        raise RuntimeError(f'insufficient ETH gas: {eth_bal/1e18}')
    # Atomic-only production path: one executor transaction, then persist only its receipt.
    if cfg.ATOMIC_LP_ONLY:
        import atomic_v3_backend as atomic
        result=atomic.open_position(token,need_usdg)
        ev=result.get('event') or {}; p=result.get('params') or {}; q=result.get('quote') or {}; rc=result['receipt']
        token_id=int(ev['tokenId']); pool=p['expectedPool']; fee=int(p['fee'])
        tick_lower,tick_upper=int(p['tickLower']),int(p['tickUpper'])
        cur_tick=int(c.pool_slot0(pool)[1]); price=get_price_usdg_per_token(pool,token); tok_dec=c.erc20_decimals(token)
        positions={}
        try: positions=json.loads(cfg.POSITIONS_FILE.read_text())
        except Exception: pass
        positions[token.lower()]={'token':token,'symbol':c.erc20_symbol(token),'decimals':tok_dec,'pool':pool,'fee':fee,
          'tick_lower':tick_lower,'tick_upper':tick_upper,'entry_tick':cur_tick,'entry_price_usdg':str(price),
          'token_id':token_id,'mint_tx':rc['hash'],'mint_time':int(time.time()),'size_usdg':str(size_usdg),
          'usdg_deposited':need_usdg,'tok_deposited':int(q.get('tokenOut',0)),'last_action_time':int(time.time()),'atomic':True}
        tmp=cfg.POSITIONS_FILE.with_suffix('.json.tmp'); tmp.write_text(json.dumps(positions,indent=2)); tmp.replace(cfg.POSITIONS_FILE)
        return {'token_id':token_id,'tx':rc['hash'],'pool':pool,'range':[tick_lower,tick_upper],'atomic':True}
    # Legacy path is reachable only under an explicit ATOMIC_LP_ONLY=false override.
    pool, fee = find_pool(token)
    print(f'[entry] pool={pool} fee={fee}')
    # 3) ticks
    tick_lower, tick_upper, cur_tick, spacing = compute_ticks(pool, cfg.RANGE_PCT)
    print(f'[entry] ticks: lower={tick_lower} cur={cur_tick} upper={tick_upper} spacing={spacing}')
    # 4) price
    price = get_price_usdg_per_token(pool, token)
    print(f'[entry] price: 1 token = ${float(price):.8f} USDG')
    tok_dec = c.erc20_decimals(token)
    # 5) swap ~52% USDG->TOKEN (slightly over 50% since current tick usually needs a bit more token side)
    half_usdg = need_usdg // 2 + int(need_usdg * 0.02)  # 52%
    # min out: expected * (1 - slippage)
    expected_tok = Decimal(half_usdg) / Decimal(10**cfg.USDG_DECIMALS) / price * Decimal(10**tok_dec)
    min_tok_swap = int(expected_tok * (Decimal(1) - cfg.MAX_SLIPPAGE_SWAP_PCT/Decimal(100)))
    # 6) approve USDG to SwapRouter and PositionManager
    print('[entry] approve USDG -> SwapRouter')
    a = c.approve_if_needed(cfg.USDG, cfg.V3_SWAP_ROUTER, 2**255)
    if a: print(' approve tx:', a['hash'], 'status', a['status'])
    print('[entry] approve USDG -> PositionManager')
    a = c.approve_if_needed(cfg.USDG, cfg.V3_POSITION_MANAGER, 2**255)
    if a: print(' approve tx:', a['hash'], 'status', a['status'])
    # 7) swap
    print(f'[entry] swap {half_usdg/1e6:.4f} USDG -> {token[-6:]}, min_out={min_tok_swap}')
    sw = swap_usdg_to_token(token, fee, half_usdg, min_tok_swap)
    print(f'[entry] swap tx: {sw["hash"]} status={sw["status"]} gasUsed={sw["gasUsed"]}')
    if sw['status'] != 1: raise RuntimeError('swap failed')
    # 8) approve token to PositionManager
    print(f'[entry] approve {token[-6:]} -> PositionManager')
    a = c.approve_if_needed(token, cfg.V3_POSITION_MANAGER, 2**255)
    if a: print(' approve tx:', a['hash'], 'status', a['status'])
    # 9) recompute balances
    tok_bal = c.erc20_balance(token)
    usdg_bal = c.erc20_balance(cfg.USDG)
    print(f'[entry] balances now: USDG={usdg_bal/1e6:.4f} TOKEN={tok_bal/10**tok_dec:.8f}')
    # Use up to (size_usdg - already spent on swap) USDG + all token balance
    usdg_for_mint = need_usdg - half_usdg
    if usdg_bal < usdg_for_mint: usdg_for_mint = usdg_bal
    tok_for_mint = tok_bal
    min_usdg = int(Decimal(usdg_for_mint) * (Decimal(1) - cfg.MAX_SLIPPAGE_MINT_PCT/Decimal(100)))
    min_tok = int(Decimal(tok_for_mint) * (Decimal(1) - cfg.MAX_SLIPPAGE_MINT_PCT/Decimal(100)))
    # 10) mint — allow big slippage on min amounts since ratio may adjust
    # Use min=0 for both (accept any partial deposit — better than fail)
    min_usdg_relaxed = 0
    min_tok_relaxed = 0
    print(f'[entry] mint: USDG={usdg_for_mint/1e6:.4f} TOK={tok_for_mint/10**tok_dec:.8f} range=[{tick_lower},{tick_upper}] (min=0/0 relaxed)')
    mn = mint_lp(token, fee, tick_lower, tick_upper, usdg_for_mint, tok_for_mint, min_usdg_relaxed, min_tok_relaxed)
    print(f'[entry] mint tx: {mn["hash"]} status={mn["status"]} gasUsed={mn["gasUsed"]}')
    if mn['status'] != 1: raise RuntimeError('mint failed')
    token_id = parse_mint_log(mn['hash'])
    print(f'[entry] tokenId={token_id}')
    # 11) save position
    positions = {}
    try: positions = json.loads(cfg.POSITIONS_FILE.read_text())
    except Exception: pass
    positions[token.lower()] = {
        'token': token, 'symbol': c.erc20_symbol(token), 'decimals': tok_dec,
        'pool': pool, 'fee': fee, 'tick_lower': tick_lower, 'tick_upper': tick_upper,
        'entry_tick': cur_tick, 'entry_price_usdg': str(price),
        'token_id': token_id, 'mint_tx': mn['hash'],
        'mint_time': int(time.time()), 'size_usdg': str(size_usdg),
        'usdg_deposited': usdg_for_mint, 'tok_deposited': tok_for_mint,
        'last_action_time': int(time.time()),
    }
    cfg.POSITIONS_FILE.write_text(json.dumps(positions, indent=2))
    print(f'[entry] saved to {cfg.POSITIONS_FILE}')
    return {'token_id': token_id, 'tx': mn['hash'], 'pool': pool, 'range': [tick_lower, tick_upper]}

if __name__ == '__main__':
    try:
        r = main()
        print(json.dumps({'ok': True, **r}))
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)[:400]}), file=sys.stderr)
        sys.exit(1)
