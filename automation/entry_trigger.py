"""Entry orchestrator: called every 5m by cron. If open slot + valid candidate exists, execute entry.
Uses live quote-verify (skip candidates where quote reverts).
"""
import json, sys, time, subprocess
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import common as c
import config as cfg
import hybrid_v4
import liquidation
from entry import find_pool
from safety import round_trip
from gmgn_risk import assess as gmgn_assess

def load_json(p, default):
    try: return json.loads(p.read_text())
    except Exception: return default

def main():
    if c.check_kill(quiet=True): return
    if liquidation.has_unsettled():
        print('[trig] entry blocked: unsettled liquidation capital')
        return
    positions = load_json(cfg.POSITIONS_FILE, {})
    cands = load_json(cfg.CANDIDATES_FILE, {}).get('candidates', [])
    v4_positions = load_json(cfg.V4_POSITIONS_FILE, {})
    if hybrid_v4.has_settled_v3_rotation(): return
    stable_first = cfg.STRATEGY_MODE == 'stable_first_exit'
    # Stable-first never rotates directly from one meme to another.
    if len(positions) + len(v4_positions) >= cfg.MAX_POSITIONS:
        if stable_first:
            cfg.ROTATION_REQUEST_FILE.unlink(missing_ok=True)
            cfg.ROTATION_READY_FILE.unlink(missing_ok=True)
            return
        req = load_json(cfg.ROTATION_REQUEST_FILE, {})
        if req and cands:
            now = time.time()
            validated=[]
            held_tokens={str(x).lower() for x in positions}|{str(x).lower() for x in v4_positions}
            held_tokens.add(str(req.get('token','')).lower())
            for cand in cands:
                if cand.get('source') != 'gmgn-trending': continue
                if str(cand.get('token','')).lower() in held_tokens: continue
                if Decimal(str(cand.get('liq_usd', '0'))) >= cfg.ROTATION_MAX_LIQ_USD: continue
                if Decimal(str(cand.get('age_hours',cand.get('age_h',0)))) < cfg.GMGN_TRENDING_MIN_AGE_HOURS: continue
                gmgn = gmgn_assess(cand['token'])
                if not gmgn.get('ok'): continue
                try:
                    raw=int(cfg.POSITION_SIZE_USDG*10**cfg.USDG_DECIMALS)
                    if cand.get('has_direct_usdg_v3'):
                        pool,fee=find_pool(cand['token']); out=c.quote_v3_exact_input_single(cfg.USDG,cand['token'],fee,raw); safe=round_trip(cand['token'],max_tax_pct=6)
                        if out<=0 or not safe.get('ok'):continue
                        evidence={'pool':pool,'fee':fee,'quote_out':str(out),'roundtrip':safe}; version='v3'
                    else:
                        if not hybrid_v4.lifecycle_verified() or not cand.get('has_direct_usdg') or cand.get('preferred_venue')!='v4':continue
                        pools=hybrid_v4.v4.discover(cand['token']); quotes=[q for q in hybrid_v4.v4.quote(cand['token'],raw) if q.get('eligible') and int(q.get('amountOut',0))>0]
                        if not pools or not quotes:continue
                        pf=hybrid_v4.v4.route_preflight(cand['token'],raw)
                        evidence={'pool':pools[0],'quote':quotes[0],'route_preflight':pf}; version='v4'
                except Exception as exc:
                    print(f'[rotate] reject {cand.get("symbol")}: {str(exc)[:100]}')
                    continue
                # Scanner order is meaningful; explicit score/rank wins without a first-hit bias.
                quality=Decimal(str(cand.get('score',cand.get('quality_score',0))))-Decimal(str(cand.get('gmgn_rank',999)))/1000
                validated.append((quality,cand,version,evidence,gmgn,raw))
            if validated:
                _,cand,version,evidence,gmgn,raw=max(validated,key=lambda x:x[0])
                ready={'ts':int(now),'validated_at':int(now),'expires_at':int(now)+cfg.ROTATION_READY_TTL_SECONDS,'source_token_id':req.get('token_id'),'source_token':req.get('token'),'source_version':'v3','target_version':version,'token':cand['token'],'symbol':cand.get('symbol'),'age_hours':cand.get('age_hours',cand.get('age_h')),'liq_usd':cand.get('liq_usd'),'gmgn_rank':cand.get('gmgn_rank'),'validation_size_raw':raw,'candidate_pool':evidence.get('pool'),'validation_evidence':{'gmgn':gmgn,**evidence}}
                from rotation import atomic
                atomic(cfg.ROTATION_READY_FILE,ready)
                print(f'✅ ROTATION READY: {cand.get("symbol")} {version.upper()}')
        return
    if not cands: return
    cooldown = load_json(cfg.COOLDOWN_FILE, {})
    now = time.time()
    ready = load_json(cfg.ROTATION_READY_FILE, {})
    preferred = str(ready.get('token') or '').lower()
    if preferred:
        cands.sort(key=lambda x: 0 if str(x.get('token','')).lower() == preferred else 1)
    # find best candidate that:
    #  - has direct USDG pool
    #  - not in cooldown
    #  - quote-verified swappable
    for c_ in cands:
        tok = c_['token'].lower()
        if tok in cooldown and cooldown[tok] > now: continue
        if tok in {k.lower() for k in positions.keys()}: continue
        if not c_.get('has_direct_usdg'): continue
        if Decimal(str(c_.get('liq_usd', '0'))) < cfg.MIN_LIQ_USD: continue
        if Decimal(str(c_.get('age_hours', c_.get('age_h', 0)))) < cfg.MIN_AGE_HOURS: continue
        # Fail closed on economics: a pool whose modelled fees cannot cover its own
        # adverse impermanent loss plus round-trip cost is a losing position taken
        # at full risk, no matter how strong its momentum looks.
        if not c_.get('lp_edge_ok'):
            print(f'[trig] {c_["symbol"]} no LP edge: {c_.get("lp_edge")}; skip')
            continue
        if c_.get('preferred_venue') == 'v4' and not c_.get('has_direct_usdg_v3'):
            if not hybrid_v4.lifecycle_verified():
                print(f'[trig] {c_["symbol"]} V4 lifecycle marker absent or invalid', file=sys.stderr)
                continue
            gmgn = gmgn_assess(c_['token'])
            if not gmgn.get('ok'): continue
            try:
                result = hybrid_v4.enter(c_)
                print('[v4-entry] '+json.dumps(result))
            except Exception as exc:
                print(f'[v4-entry] {c_["symbol"]} failed: {str(exc)[:180]}', file=sys.stderr)
            break
        # Fail closed: hard risk or GMGN outage receives a 24h cooldown.
        gmgn = gmgn_assess(c_['token'])
        if not gmgn.get('ok'):
            print(f'[trig] {c_["symbol"]} GMGN REJECT: {gmgn.get("reason", "unknown")}; cooldown 24h')
            cooldown[tok] = int(now + 24 * 3600)
            Path(cfg.COOLDOWN_FILE).write_text(json.dumps(cooldown, indent=2))
            continue
        print(f'[trig] {c_["symbol"]} GMGN OK: SM={gmgn.get("smart_wallets",0)} KOL={gmgn.get("kol_wallets",0)} flags={gmgn.get("flags",[])}')
        # verify swap works
        try:
            pool, fee = find_pool(c_['token'])
        except Exception as e:
            print(f'[trig] {c_["symbol"]} no active pool: {e}')
            continue
        try:
            out = c.quote_v3_exact_input_single(cfg.USDG, c_['token'], fee, int(Decimal('1')*10**cfg.USDG_DECIMALS))
            if out == 0:
                print(f'[trig] {c_["symbol"]} quote 0 out, skip')
                continue
        except Exception as e:
            print(f'[trig] {c_["symbol"]} quote revert: {str(e)[:100]}')
            # mark as bad for cooldown short
            cooldown[tok] = int(now + 3600)  # 1h cooldown for un-swappable
            continue
        # honeypot / hidden-tax round-trip check (buy 1 USDG -> sell back)
        safe = round_trip(c_['token'], max_tax_pct=6.0)
        if not safe['ok']:
            print(f'[trig] {c_["symbol"]} SAFETY FAIL: {safe["reason"]}')
            cooldown[tok] = int(now + 24 * 3600)  # 24h cooldown for honeypot
            Path(cfg.COOLDOWN_FILE).write_text(json.dumps(cooldown, indent=2))
            continue
        print(f'[trig] {c_["symbol"]} safety OK: {safe["reason"]}')
        # good candidate — execute entry
        print(f'[trig] entering {c_["symbol"]} @ {c_["token"]}')
        cmd = ['python3', str(Path(__file__).parent / 'entry.py'), c_['token']]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        print(r.stdout)
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            cooldown[tok] = int(now + 3600)
        elif tok in {k.lower() for k in load_json(cfg.POSITIONS_FILE, {}).keys()}:
            cfg.ROTATION_READY_FILE.unlink(missing_ok=True)
        # save cooldown state
        Path(cfg.COOLDOWN_FILE).write_text(json.dumps(cooldown, indent=2))
        break  # only 1 entry per tick

if __name__ == '__main__':
    try: main()
    except Exception as e:
        print(json.dumps({'error': str(e)[:300]}), file=sys.stderr)
        sys.exit(0)
