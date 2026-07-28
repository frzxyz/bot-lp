"""Emergency: watch for rug/dump. Run every 1m.
Triggers panic exit if:
  - pool USDG liquidity drops > RUG_LIQ_DROP_PCT_1H in 1h
  - price drops > DUMP_PCT_1H in 1h
Writes kill switch if halted globally.
"""
import json, sys, time
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import common as c
import config as cfg
import risk as lp_risk
import positions as lp_positions
from entry import get_price_usdg_per_token
from manager import full_exit, load_json, save_json
from gmgn_risk import assess as gmgn_assess
import hybrid_v4
import v4_backend

HISTORY = cfg.STATE_DIR / 'emergency_history.json'

@c.wallet_locked
def main():
    if c.check_kill(quiet=True): return
    # V4 emergency path uses only V4 sidecar/on-chain valuation and close-usdg settlement.
    v4pos = hybrid_v4.load()
    if v4pos:
        rows = {str(x['tokenId']): x for x in v4_backend.list_positions()}
        for key, pos in list(v4pos.items()):
            risk = gmgn_assess(pos['token'])
            row = rows.get(str(pos['token_id']))
            # A missing NFT can be expected after a confirmed close while exact
            # token proceeds are still in the durable settlement WAL. Never
            # interpret missing valuation as zero and attempt to close it again.
            if row is None:
                pending_close = next((r for r in hybrid_v4.close_wal._load().values()
                    if str(r.get('nft_id')) == str(pos['token_id']) and not r.get('settlementComplete')), None)
                if pending_close:
                    continue
                print(f'[v4-emergency] NFT {pos["token_id"]} absent without pending WAL; state retained', file=sys.stderr)
                continue
            hard = not risk.get('ok')
            value = Decimal(str(row.get('valueUsd', 0)))
            # Defaulting this to zero used to disable the value stop entirely for any
            # record missing the key, because `entry and ...` is false at zero — the
            # position silently kept only its GMGN protection.
            try:
                entry = lp_positions.principal_usdg(pos)
            except lp_positions.MissingMoneyField as exc:
                print(f'[v4-emergency] {pos.get("symbol", key)}: {exc}; value stop unavailable, '
                      f'GMGN stop still active', file=sys.stderr)
                entry = None
            if hard or (entry is not None and entry > 0 and value < entry * (Decimal(1)-cfg.STOP_LOSS_PCT/100)):
                try:
                    result = hybrid_v4.close_position(key, 'panic_gmgn' if hard else 'panic_value')
                    print('[v4-emergency] '+json.dumps(result))
                except Exception as exc:
                    print(f'[v4-emergency] settlement/close failed; state retained: {str(exc)[:200]}', file=sys.stderr)
    positions = load_json(cfg.POSITIONS_FILE, {})
    if not positions: return
    hist = load_json(HISTORY, {})
    now = int(time.time())
    updated = False
    alerts = []
    cooldown = load_json(cfg.COOLDOWN_FILE, {})
    for key, pos in list(positions.items()):
        pool = pos['pool']
        try:
            price = get_price_usdg_per_token(pool, pos['token'])
            usdg_in_pool = c.erc20_balance(cfg.USDG, pool)
        except Exception as e:
            print(f'[emrg] {pos["symbol"]} probe error: {e}', file=sys.stderr)
            continue
        h = hist.setdefault(key, [])
        h.append({'t': now, 'price': str(price), 'usdg_pool': usdg_in_pool})
        # keep only last 90 min
        h[:] = [x for x in h if now - x['t'] <= 5400]
        trigger = False
        # Short-window check first. The 1h baseline below needs a sample at least 55
        # minutes old, which leaves a freshly minted position completely unguarded
        # through the window where a new memecoin is most likely to be dumped.
        fast, fast_reason = lp_risk.dump_detected(
            h, now, window_seconds=cfg.FAST_DUMP_WINDOW_SECONDS,
            price_drop_pct=cfg.FAST_DUMP_PCT, liquidity_drop_pct=cfg.FAST_LIQ_DROP_PCT)
        if fast and fast_reason:
            window_min = cfg.FAST_DUMP_WINDOW_SECONDS // 60
            alerts.append(f'🚨 {fast_reason.upper()} {pos["symbol"]}: triggered within {window_min}m window')
            trigger = True
        # find datapoint ~1h ago (closest)
        past = [x for x in h if now - x['t'] >= 3600 - 300 and now - x['t'] <= 3600 + 900]
        if past:
            p1h = past[0]
            price_1h_ago = Decimal(p1h['price'])
            liq_1h_ago = int(p1h['usdg_pool'])
            price_change = (price - price_1h_ago) / price_1h_ago * Decimal(100) if price_1h_ago > 0 else Decimal(0)
            liq_change_pct = Decimal(liq_1h_ago - usdg_in_pool) / Decimal(liq_1h_ago) * Decimal(100) if liq_1h_ago > 0 else Decimal(0)
            if price_change < -cfg.DUMP_PCT_1H:
                alerts.append(f'🚨 DUMP {pos["symbol"]}: {float(price_change):.1f}% in 1h')
                trigger = True
            if liq_change_pct > cfg.RUG_LIQ_DROP_PCT_1H:
                alerts.append(f'🚨 RUG {pos["symbol"]}: pool USDG -{float(liq_change_pct):.1f}% in 1h')
                trigger = True
        if trigger:
            print(f'[emrg] PANIC EXIT {pos["symbol"]}')
            try:
                full_exit(key, dict(pos,_emergency=True,_exit_reason='emergency_exit'))
                del positions[key]
                cooldown[key] = now + cfg.TOKEN_COOLDOWN_HOURS*3600
                updated = True
            except Exception as e:
                print(f'[emrg] exit failed: {e}')
                # if exit fails, arm kill-switch so we don't keep trying
                cfg.KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
                cfg.KILL_SWITCH.write_text(f'exit_fail {pos["symbol"]} at {now}: {e}')
    if updated:
        save_json(cfg.POSITIONS_FILE, positions)
        save_json(cfg.COOLDOWN_FILE, cooldown)
    save_json(HISTORY, hist)
    if alerts:
        print('\n'.join(alerts))

if __name__ == '__main__':
    try: main()
    except Exception as e:
        print(json.dumps({'error': str(e)[:300]}), file=sys.stderr)
        sys.exit(0)
