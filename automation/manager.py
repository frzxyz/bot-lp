"""Manager: monitor open positions, rebalance if out-of-range, collect fees, exit on stop-loss.
Run every 5m.
"""
import json, sys, time, math
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from web3 import Web3
from eth_abi import encode, decode
import common as c
import config as cfg
import liquidation
import risk as lp_risk
import positions as lp_positions
from entry import compute_ticks, get_price_usdg_per_token, mint_lp, swap_usdg_to_token, find_pool, parse_mint_log

POSITIONS_SEL   = '0x99fbab88'  # positions(uint256)
COLLECT_SEL     = '0xfc6f7865'  # collect((uint256,address,uint128,uint128))
DEC_LIQ_SEL     = '0x0c49ccbe'  # decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))
BURN_SEL        = '0x42966c68'  # burn(uint256)

MAX_U128 = 2**128 - 1


class RotationCloseError(RuntimeError):
    """A rotation close that stopped part-way, carrying what it had already done.

    The recovery payload (receipts, pre-close balances, any liquidation id) is what
    lets the journal reconcile without re-broadcasting.  It used to be attached to a
    bare RuntimeError as an ad-hoc attribute, which no reader could rely on.
    """

    def __init__(self, message: str, recovery: dict | None = None):
        super().__init__(message)
        self.recovery: dict = recovery or {}

def position_info(token_id):
    r = c.eth_call(cfg.V3_POSITION_MANAGER, POSITIONS_SEL + c.word_uint(token_id))
    b = bytes.fromhex(r[2:])
    v = decode(['uint96','address','address','address','uint24','int24','int24','uint128','uint256','uint256','uint128','uint128'], b)
    return {
        'token0': v[2], 'token1': v[3], 'fee': v[4],
        'tick_lower': v[5], 'tick_upper': v[6],
        'liquidity': v[7], 'owed0': v[10], 'owed1': v[11],
    }

def collect_fees(token_id):
    """Call collect with MAX to sweep accumulated fees + any leftover from decreaseLiquidity."""
    data = COLLECT_SEL + encode(
        ['(uint256,address,uint128,uint128)'],
        [(int(token_id), Web3.to_checksum_address(cfg.WALLET_ADDRESS), MAX_U128, MAX_U128)]
    ).hex()
    return c.build_and_send({'to': cfg.V3_POSITION_MANAGER, 'data': data, 'value': 0})

def simulated_collectable(token_id):
    """Return current collectable amounts via eth_call without changing state."""
    data = COLLECT_SEL + encode(
        ['(uint256,address,uint128,uint128)'],
        [(int(token_id), Web3.to_checksum_address(cfg.WALLET_ADDRESS), MAX_U128, MAX_U128)]
    ).hex()
    raw = c.eth_call(cfg.V3_POSITION_MANAGER, data, from_addr=cfg.WALLET_ADDRESS)
    return decode(['uint256','uint256'], bytes.fromhex(raw[2:]))

def decrease_liquidity(token_id, liquidity_fraction=Decimal(1)):
    info = position_info(token_id)
    liq = info['liquidity']
    amt = int(Decimal(liq) * liquidity_fraction)
    if amt == 0: return None
    deadline = int(time.time()) + 600
    data = DEC_LIQ_SEL + encode(
        ['(uint256,uint128,uint256,uint256,uint256)'],
        [(int(token_id), int(amt), 0, 0, int(deadline))]
    ).hex()
    return c.build_and_send({'to': cfg.V3_POSITION_MANAGER, 'data': data, 'value': 0})

def burn_nft(token_id):
    data = BURN_SEL + c.word_uint(token_id)
    return c.build_and_send({'to': cfg.V3_POSITION_MANAGER, 'data': data, 'value': 0})

def swap_token_to_usdg(token, fee, amount_raw, min_out_raw):
    from entry import SWAP_SEL
    data = SWAP_SEL + encode(
        ['(address,address,uint24,address,uint256,uint256,uint160)'],
        [(Web3.to_checksum_address(token), Web3.to_checksum_address(cfg.USDG),
          int(fee), Web3.to_checksum_address(cfg.WALLET_ADDRESS),
          int(amount_raw), int(min_out_raw), 0)]
    ).hex()
    # need to approve token to router
    c.approve_if_needed(token, cfg.V3_SWAP_ROUTER, 2**255)
    return c.build_and_send({'to': cfg.V3_SWAP_ROUTER, 'data': data, 'value': 0})

def swap_token_to_usdg_kyber(token, fee, amount_raw, min_out_raw):
    """Normal Kyber wallet swap of only the journal-attributed amount."""
    import atomic_v3_backend as atomic
    return atomic.kyber_liquidate(token,amount_raw,min_out_raw)

def full_exit(pos_key, pos):
    """Close and durably liquidate only inventory attributable to this LP."""
    tid = pos['token_id']
    if cfg.ATOMIC_LP_ONLY:
        import atomic_v3_backend as atomic
        result=atomic.close_position(tid); ev=result.get('event') or {}; rc=result['receipt']
        # V3 executor now removes and collects principal + fees in one no-swap tx.
        # The Closed event is the exact attribution boundary for stage two.
        delta=int(ev.get('tokenAmount',0)); protected=max(0,c.erc20_balance(pos['token'])-delta)
        q=liquidation.enqueue(pos['token'],pos.get('symbol'),pos.get('decimals',c.erc20_decimals(pos['token'])),delta,protected,pos['fee'],
            source_reason=pos.get('_exit_reason','lp_exit'),source_version='v3',source_token_id=tid,
            venue_candidates=['kyber'],emergency=bool(pos.get('_emergency')))
        if q:q=liquidation.attempt(q)
        return {'closure_confirmed':True,'remove_collect_confirmed':True,'settlement_complete':not q or q.get('phase')=='completed',
                'exact_proceeds_raw':int(ev['settlementAmount'])+int((q or {}).get('proceeds_raw',0)),
                'source_token_delta_raw':delta,'liquidation_id':q and q.get('id'),
                'txs':{'atomic_close':rc},'atomic':True}
    # Legacy queue below is historical recovery only and requires explicit override.
    token = pos['token']
    fee = pos['fee']
    tok_dec = pos['decimals']
    token_before=c.erc20_balance(token); usdg_before=c.erc20_balance(cfg.USDG)
    print(f'[exit] decrease liquidity token_id={tid}')
    r = decrease_liquidity(tid)
    if r: print(f' dec tx: {r["hash"]} status={r["status"]}')
    print(f'[exit] collect')
    r = collect_fees(tid)
    print(f' collect tx: {r["hash"]} status={r["status"]}')
    delta=max(0,c.erc20_balance(token)-token_before)
    pending=liquidation.enqueue(token,pos.get('symbol'),tok_dec,delta,token_before,fee,
        source_reason=pos.get('_exit_reason','lp_exit'),source_token_id=tid,emergency=bool(pos.get('_emergency')))
    if pending: pending=liquidation.attempt(pending)
    try:
        b = burn_nft(tid)
        print(f'[exit] burn tx: {b["hash"]}')
    except Exception as e:
        print(f' burn skipped: {e}')
    return {'closure_confirmed':True,'liquidation_id':pending and pending['id'],
            'settlement_complete':not pending or pending.get('phase')=='completed',
            'exact_proceeds_raw':(pending or {}).get('proceeds_raw',max(0,c.erc20_balance(cfg.USDG)-usdg_before)),
            'source_token_delta_raw':delta}

def load_json(p, default):
    try: return json.loads(p.read_text())
    except Exception: return default

def save_json(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp=p.with_suffix(p.suffix+'.tmp'); tmp.write_text(json.dumps(data, indent=2, sort_keys=True)); tmp.replace(p)

def _ok_receipt(r): return isinstance(r,dict) and int(r.get('status',0))==1 and bool(r.get('hash'))

def settle_v3_rotation(pos):
    """Close one NFT while selling only close-attributable token balance delta."""
    tid=pos['token_id']; token=pos['token']; txs={}
    if cfg.ATOMIC_LP_ONLY:
        return full_exit(token.lower(),pos)
    usdg0=c.erc20_balance(cfg.USDG); tok0=c.erc20_balance(token)
    try:
        if int(position_info(tid)['liquidity'])>0:
            r=decrease_liquidity(tid); txs['decrease']=r
            if r is not None and not _ok_receipt(r): raise RuntimeError('decrease receipt failed')
        if int(position_info(tid)['liquidity'])!=0: raise RuntimeError('liquidity remains')
        r=collect_fees(tid); txs['collect']=r
        if not _ok_receipt(r): raise RuntimeError('collect receipt failed')
        delta=max(0,c.erc20_balance(token)-tok0)
        q=liquidation.enqueue(token,pos.get('symbol'),pos.get('decimals',c.erc20_decimals(token)),delta,tok0,pos['fee'],source_reason='v3_to_v4_rotation',source_token_id=tid,link='v3_to_v4')
        if q:
            q=liquidation.attempt(q)
            if q.get('phase')!='completed':
                raise RotationCloseError('liquidation_pending',{'liquidation_id':q['id'],'txs':txs,'wallet_usdg_before_raw':usdg0,'source_token_before_raw':tok0})
        r=burn_nft(tid); txs['burn']=r
        if not _ok_receipt(r): raise RuntimeError('burn receipt failed')
        try: position_info(tid); gone=False
        except Exception: gone=True
        if not gone: raise RuntimeError('NFT still exists')
        usdg1=c.erc20_balance(cfg.USDG)
        return {'closure_confirmed':True,'exact_proceeds_raw':int((q or {}).get('proceeds_raw',max(0,usdg1-usdg0))),'source_token_delta_raw':delta,'wallet_usdg_before_raw':usdg0,'wallet_usdg_after_raw':usdg1,'txs':txs,'liquidation_id':q and q['id']}
    except Exception as exc:
        raise RotationCloseError(str(exc),{'txs':txs,'wallet_usdg_before_raw':usdg0,'source_token_before_raw':tok0}) from exc

def fee_window_delta(history, token_id, now, cumulative):
    """Append cumulative fee snapshot; return exact >=3h delta once baseline exists."""
    key = str(token_id)
    rows = history.setdefault(key, [])
    rows.append({'ts': int(now), 'cumulative_usdg': str(cumulative)})
    cutoff = now - cfg.FEE_EVAL_WINDOW_HOURS * 3600
    baseline = None
    for row in rows:
        if int(row['ts']) <= cutoff: baseline = row
        else: break
    history[key] = [r for r in rows if int(r['ts']) >= cutoff - 900 or r is baseline]
    if baseline is None: return None
    return max(Decimal(0), cumulative - Decimal(baseline['cumulative_usdg']))

@c.wallet_locked
def main():
    if c.check_kill(quiet=True): return
    for action in liquidation.retry_due(): print('[liquidation] '+json.dumps(action))
    from hybrid_v4 import manage as manage_v4, retry_pending, retry_v3_to_v4
    for action in retry_v3_to_v4(): print('[rotation] '+json.dumps(action))
    for action in retry_pending():
        print('[v4] '+json.dumps(action))
    try:
        for action in manage_v4():
            print('[v4] '+json.dumps(action))
    except Exception as exc:
        # A fail-closed V4 lifecycle decision must not starve independent V3
        # monitoring/rotation. The V4 position remains untouched and tracked.
        print('[v4] management retained: '+str(exc)[:220])
    positions = load_json(cfg.POSITIONS_FILE, {})
    if not positions:
        return
    cooldown = load_json(cfg.COOLDOWN_FILE, {})
    fee_history = load_json(cfg.FEE_HISTORY_FILE, {})
    exit_policy = lp_risk.ExitPolicy.from_config()
    updated = False
    alerts = []
    for key, pos in list(positions.items()):
        try:
            tid = pos['token_id']
            info = position_info(tid)
            cur_pool = pos['pool']
            s0 = c.pool_slot0(cur_pool)
            cur_tick = int(s0[1])
            cur_price = get_price_usdg_per_token(cur_pool, pos['token'])
            entry_price = Decimal(pos['entry_price_usdg'])
            price_change_pct = (cur_price - entry_price) / entry_price * Decimal(100)
            # Simulated collect includes fees accrued since the NFT was last poked;
            # positions().tokensOwed alone can remain zero/stale.
            usdg_is_t0 = info['token0'].lower() == cfg.USDG.lower()
            token_is_t0 = not usdg_is_t0
            side = lp_risk.range_side(tick=cur_tick, tick_lower=info['tick_lower'],
                                   tick_upper=info['tick_upper'], token_is_token0=token_is_t0)
            exposure_pct = lp_risk.live_exposure_pct(tick=cur_tick, tick_lower=info['tick_lower'],
                                                  tick_upper=info['tick_upper'], token_is_token0=token_is_t0)
            collectable0, collectable1 = simulated_collectable(tid)
            fees_usdg_est = Decimal(collectable0 if usdg_is_t0 else collectable1) / Decimal(10**cfg.USDG_DECIMALS)
            fees_tok = Decimal(collectable1 if usdg_is_t0 else collectable0) / Decimal(10**pos['decimals'])
            fees_tok_in_usdg = fees_tok * cur_price
            total_fees_usdg = fees_usdg_est + fees_tok_in_usdg
            cumulative_fees = Decimal(pos.get('total_fees_collected_usdg','0')) + total_fees_usdg
            fee_3h = fee_window_delta(fee_history, tid, time.time(), cumulative_fees)
            age_h = (time.time() - pos.get('mint_time', 0)) / 3600
            # Committed capital is the denominator of every exit test, so it is read
            # through the typed accessor and is allowed to fail loudly.  Defaulting it
            # to the config constant would make the stop-loss measure a position that
            # does not exist.
            principal = lp_positions.principal_usdg(pos)
            nav = lp_risk.nav_usdg(tick=cur_tick, tick_lower=info['tick_lower'], tick_upper=info['tick_upper'],
                                liquidity=info['liquidity'], token_is_token0=token_is_t0) + total_fees_usdg
            peak_nav = max(Decimal(str(pos.get('peak_nav_usdg', 0))), nav)
            if str(peak_nav) != str(pos.get('peak_nav_usdg', '')):
                pos['peak_nav_usdg'] = str(peak_nav); updated = True
            print(f'[mgr] {pos["symbol"]} tid={tid} tick={cur_tick} range=[{info["tick_lower"]},{info["tick_upper"]}] '
                  f'side={side} exposure={exposure_pct:.0f}% price=${float(cur_price):.6f} chg={float(price_change_pct):.1f}% '
                  f'nav=${float(nav):.4f} peak=${float(peak_nav):.4f} fees=${float(total_fees_usdg):.4f} age={age_h:.1f}h liq={info["liquidity"]}')

            # Range timers are per-side: flipping from below to above (or back) restarts
            # the clock, because the two sides carry completely different risk.
            now_ts = int(time.time())
            if side == lp_risk.IN_RANGE:
                if pos.pop('oor_since', None) is not None:
                    pos.pop('oor_side', None); updated = True
                oor_elapsed = 0
            else:
                if pos.get('oor_side') != side:
                    pos['oor_side'], pos['oor_since'] = side, now_ts; updated = True
                oor_elapsed = now_ts - int(pos.get('oor_since', now_ts))

            should_exit, exit_reason = lp_risk.exit_decision(
                nav_usdg=nav, principal_usdg=principal, peak_nav_usdg=peak_nav, side=side,
                exposure_pct=exposure_pct, oor_elapsed_seconds=oor_elapsed, policy=exit_policy)
            if should_exit:
                drawdown = (peak_nav - nav) / peak_nav * 100 if peak_nav > 0 else Decimal(0)
                print(f'[mgr] EXIT {pos["symbol"]}: {exit_reason} nav=${float(nav):.4f} '
                      f'drawdown={float(drawdown):.1f}% exposure={exposure_pct:.0f}%')
                full_exit(key, dict(pos, _exit_reason=exit_reason))
                # An upside break is a clean recycle, not a bad token: keep its cooldown
                # short so capital can be redeployed instead of parked for a day.
                recycle = exit_reason == 'out_of_range_above'
                cooldown[key] = now_ts + (300 if recycle else cfg.TOKEN_COOLDOWN_HOURS*3600)
                del positions[key]
                alerts.append(f'{"♻️" if recycle else "❌"} EXIT {pos["symbol"]} ({exit_reason}): '
                              f'nav ${float(nav):.2f} vs principal ${float(principal):.2f}')
                updated = True
                continue

            # LOW-FEE ROTATION: request discovery first; close only after replacement
            # independently passes GMGN, live quote, and round-trip safety validation.
            if fee_3h is not None and fee_3h < cfg.MIN_FEE_3H_USDG:
                request = {'ts': int(time.time()), 'token_id': tid, 'token': pos['token'],
                           'symbol': pos['symbol'], 'fee_3h_usdg': str(fee_3h),
                           'threshold_usdg': str(cfg.MIN_FEE_3H_USDG), 'status': 'searching'}
                save_json(cfg.ROTATION_REQUEST_FILE, request)
                ready = load_json(cfg.ROTATION_READY_FILE, {})
                fresh=(ready.get('source_token_id') == tid and int(ready.get('validated_at',ready.get('ts',0))) <= time.time() < int(ready.get('expires_at',0)))
                if fresh:
                    from rotation import revalidate_ready, prepare_pending, update_pending
                    # Size the replacement from this position's real committed capital.
                    # The former get('entry_value_usdg', get('entry_value_usd', ...)) chain
                    # named two V4-only keys, so every V3 record fell through to the config
                    # constant and rotated at $1 no matter how large the position was.
                    estimated_raw=int(lp_positions.principal_usdg(pos)*Decimal(10**cfg.USDG_DECIMALS))
                    try:
                        evidence=revalidate_ready(ready,pos,estimated_raw)
                        # Mandatory before journal phase or source mutation: prove the deployed
                        # executor can simulate immutable generic open calldata at the PONS/source estimate.
                        if ready.get('target_version')=='v4':
                            import atomic_v4_backend
                            candidate_pool=ready.get('candidate_pool') or (ready.get('validation_evidence') or {}).get('pool') or {}
                            pool_id=candidate_pool.get('poolId')
                            if not pool_id:
                                raise RuntimeError('rotation-ready V4 candidate missing poolId')
                            evidence['atomic_open']=atomic_v4_backend.capability_preflight(ready['token'],estimated_raw,pool_id)
                    except Exception as exc:
                        print(f'[mgr] rotation retained: revalidation failed: {str(exc)[:160]}'); continue
                    print(f'[mgr] ROTATE {pos["symbol"]}: fee3h=${float(fee_3h):.2f}; replacement={ready.get("symbol")}')
                    pending=prepare_pending(ready,pos,evidence) if ready.get('target_version')=='v4' else None
                    if pending:
                        pending=update_pending(pending,'closing',wallet_usdg_before_raw=c.erc20_balance(cfg.USDG),source_token_before_raw=c.erc20_balance(pos['token']))
                    try: result=settle_v3_rotation(pos)
                    except Exception as exc:
                        if pending:update_pending(pending,'liquidation_pending' if str(exc)=='liquidation_pending' else 'recovery',last_error=str(exc)[:300],recovery=getattr(exc,'recovery',{}))
                        print('[mgr] rotation close incomplete; recovery retained'); continue
                    if pending:update_pending(pending,'settled',settlement=result,exact_proceeds_raw=result['exact_proceeds_raw'],txs=result['txs'])
                    cooldown[key] = int(time.time() + cfg.TOKEN_COOLDOWN_HOURS*3600)
                    del positions[key]
                    fee_history.pop(str(tid), None)
                    cfg.ROTATION_REQUEST_FILE.unlink(missing_ok=True)
                    alerts.append(f'🔄 ROTATE {pos["symbol"]}: fee 3j ${float(fee_3h):.2f} < $2; kandidat {ready.get("symbol")} siap')
                    updated = True
                    continue

            # HARVEST
            if total_fees_usdg >= cfg.HARVEST_MIN_USDG:
                print(f'[mgr] HARVEST {pos["symbol"]} fees=${float(total_fees_usdg):.2f}')
                try:
                    r = collect_fees(tid)
                    print(f' collect tx: {r["hash"]} status={r["status"]}')
                    alerts.append(f'💰 HARVEST {pos["symbol"]} +${float(total_fees_usdg):.2f}')
                    pos['last_harvest_time'] = int(time.time())
                    pos['total_fees_collected_usdg'] = str(Decimal(pos.get('total_fees_collected_usdg','0')) + total_fees_usdg)
                    updated = True
                except Exception as e:
                    print(f' collect fail: {e}')
        except Exception as exc:
            # A single unreadable or unreachable position must not starve
            # monitoring of the others; the tick used to abort entirely.
            print(f'[mgr] {pos.get("symbol", key)} skipped this tick: {str(exc)[:200]}', file=sys.stderr)
            continue

    if updated:
        save_json(cfg.POSITIONS_FILE, positions)
        save_json(cfg.COOLDOWN_FILE, cooldown)
    save_json(cfg.FEE_HISTORY_FILE, fee_history)
    if alerts:
        print('\n'.join(alerts))

if __name__ == '__main__':
    try: main()
    except Exception as e:
        print(json.dumps({'error': str(e)[:300]}), file=sys.stderr)
        sys.exit(0)
