"""Scanner: poll DexScreener for Robinhood Chain meme pairs, filter, cache candidates.
Run every 15m. Quiet unless new candidate promoted.

v2 (inspired by FlipZ3ro robinhood-lp-bot):
  - vol24 RISING check (candidate must show rising volume vs prev snapshot).
  - FOMO score: turnover + liq depth + volume momentum composite (0-100).
  - persistent alerted-set with cooldown → same token not re-promoted every tick.
"""
import json, sys, time, os, signal
import math
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import requests
from config import (USDG, WETH, MIN_LIQ_USD, MIN_VOL24_USD, MIN_AGE_HOURS,
                    CANDIDATES_FILE, COOLDOWN_FILE, POSITIONS_FILE,
                    TOKEN_COOLDOWN_HOURS, KILL_SWITCH, FEE_TIER_FALLBACKS,
                    STATE_DIR, ROTATION_REQUEST_FILE, ROTATION_MAX_LIQ_USD)
from config import (GMGN_TRENDING_MIN_AGE_HOURS, POSITION_SIZE_USDG,
                    V4_EXECUTION_COST_BUFFER_USDG, MAX_TOP10_PCT)
from common import get_pool, pool_slot0
import risk as lp_risk
from gmgn_risk import assess as gmgn_assess
from v4_backend import quote as quote_v4, discover_pair
from gmgn_trending import trending as gmgn_trending

HISTORY_FILE = STATE_DIR / 'watch_history.json'
RISE_FACTOR = Decimal('1.25')     # vol24 must be >= 1.25x prev snapshot to count as rising
ALERT_COOLDOWN_MIN = 60           # same token: don't re-promote within 60m
SCAN_MAX_WALL_SEC = int(os.getenv('RH_SCANNER_MAX_WALL_SEC', '100'))
MAX_ENRICH_CANDIDATES = int(os.getenv('RH_SCANNER_MAX_ENRICH', '3'))

DS_TOKENS_ENDPOINT = 'https://api.dexscreener.com/latest/dex/tokens/{}'
GT_TOKEN_POOLS = 'https://api.geckoterminal.com/api/v2/networks/robinhood/tokens/{}/pools?page=1'
GT_TOP_POOLS = 'https://api.geckoterminal.com/api/v2/networks/robinhood/pools?page={}'
UA = {'user-agent':'rh-meme-lp/1.0','accept':'application/json'}

def fetch(url, tries=4):
    last=None
    for a in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code==200 and r.text.strip().startswith('{'):
                return r.json()
            last = f'{r.status_code} {r.text[:80]!r}'
        except Exception as e:
            last=str(e)[:150]
        time.sleep(3*(a+1))
    raise RuntimeError(f'fetch {url[:60]} failed: {last}')

def fetch_pairs_dexscreener():
    out=[]
    pairs: list=[]
    for settlement in (USDG, WETH):
        pairs += fetch(DS_TOKENS_ENDPOINT.format(settlement)).get('pairs') or []
    for p in pairs:
        if p.get('chainId')!='robinhood': continue
        if p.get('dexId')!='uniswap': continue
        if 'v3' not in (p.get('labels') or []): continue
        b = p.get('baseToken') or {}; q = p.get('quoteToken') or {}
        out.append({
            'base_addr': b.get('address'), 'base_sym': b.get('symbol'),
            'quote_addr': q.get('address'), 'quote_sym': q.get('symbol'),
            'pair': p.get('pairAddress'),
            'liq_usd': float(p.get('liquidity',{}).get('usd') or 0),
            'vol24_usd': float(p.get('volume',{}).get('h24') or 0),
            'created_ms': p.get('pairCreatedAt') or 0,
            'url': p.get('url'),
            'source': 'dexscreener',
        })
    return out

def fetch_pairs_geckoterminal():
    """Fetch top pools on robinhood chain, filter USDG-paired uniswap V3."""
    out=[]
    USDG_LO = USDG.lower()
    for page in range(1,6):
        try:
            d = fetch(GT_TOP_POOLS.format(page))
        except Exception:
            break
        pools = d.get('data') or []
        if not pools: break
        for pool in pools:
            attr = pool.get('attributes') or {}
            rel = pool.get('relationships') or {}
            dex = (rel.get('dex') or {}).get('data',{}).get('id','')
            if 'uniswap' not in dex.lower(): continue
            # GT lists v2/v3 both as uniswap; skip v2 by fee_percent presence
            addr = attr.get('address')
            base = (rel.get('base_token') or {}).get('data',{}).get('id','')  # 'robinhood_0x...'
            quote = (rel.get('quote_token') or {}).get('data',{}).get('id','')
            base_addr = base.split('_')[-1] if '_' in base else base
            quote_addr = quote.split('_')[-1] if '_' in quote else quote
            if USDG_LO not in (base_addr.lower(), quote_addr.lower()): continue
            out.append({
                'base_addr': base_addr, 'base_sym': attr.get('name','').split(' / ')[0],
                'quote_addr': quote_addr, 'quote_sym': attr.get('name','').split(' / ')[-1],
                'pair': addr,
                'liq_usd': float(attr.get('reserve_in_usd') or 0),
                'vol24_usd': float((attr.get('volume_usd') or {}).get('h24') or 0),
                'created_ms': 0,  # GT doesn't expose easily
                'url': f'https://www.geckoterminal.com/robinhood/pools/{addr}',
                'source': 'geckoterminal',
            })
        time.sleep(1.2)  # respect rate limit
    return out

def get_pairs():
    """Merge DexScreener + GeckoTerminal (dedup by pair addr)."""
    all_pairs = []
    try:
        all_pairs += fetch_pairs_dexscreener()
    except Exception as e:
        print(f'DS failed: {str(e)[:100]}', file=sys.stderr)
    try:
        all_pairs += fetch_pairs_geckoterminal()
    except Exception as e:
        print(f'GT failed: {str(e)[:100]}', file=sys.stderr)
    # GMGN trending is the primary rotation source. These synthetic records only feed
    # on-chain pool discovery below; they do not claim a direct USDG pool exists.
    try:
        for x in gmgn_trending(max_liquidity=100000):
            addr = str(x.get('address') or '')
            if not addr: continue
            all_pairs.append({
                'base_addr': addr, 'base_sym': x.get('symbol'),
                'quote_addr': USDG, 'quote_sym': 'USDG', 'pair': 'gmgn:' + addr,
                'liq_usd': float(x.get('liquidity') or 0),
                'vol24_usd': float(x.get('volume') or 0),
                'created_ms': int(x.get('creation_timestamp') or 0) * 1000,
                'url': 'https://gmgn.ai/robinhood/token/' + addr,
                'source': 'gmgn-trending', 'gmgn_rank': int(x.get('rank') or 999),
                'gmgn_smart': int(x.get('smart_degen_count') or 0),
                'gmgn_kol': int(x.get('renowned_count') or 0),
            })
    except Exception as e:
        print(f'GMGN trending failed: {str(e)[:100]}', file=sys.stderr)
    # dedup by pair address (prefer DS liq if both)
    seen = {}
    for p in all_pairs:
        k = (p['pair'] or '').lower()
        if not k: continue
        if k not in seen or (p['source']=='dexscreener'):
            seen[k] = p
    return list(seen.values())

def load_json(p, default):
    try: return json.loads(Path(p).read_text())
    except Exception: return default

def save_json(p, data):
    Path(p).write_text(json.dumps(data, indent=2, sort_keys=True))

def in_cooldown(token, cd):
    exp = cd.get(token.lower())
    if not exp: return False
    return time.time() < exp

def fomo_score(liq: Decimal, vol24: Decimal, vol_rise_x: float, age_h: float) -> float:
    """Composite 0-100 score. Adopted from FlipZ3ro fomoScore, adapted to available data."""
    turnover = float(vol24 / (liq * 24)) if liq > 0 else 0.0   # per-hour churn
    s = 0.0
    s += min(30, math.log10(1 + turnover * 100) * 20)          # turnover heaviest signal
    s += min(20, math.log10(1 + float(liq)) * 3)               # liq depth (log)
    s += min(20, math.log10(1 + float(vol24)) * 3)             # vol scale (log)
    s += min(20, max(0, math.log2(max(vol_rise_x, 0.5)) * 10)) # rising factor bonus
    if age_h > 168: s += 5                                      # 1w+ = less rug-risk
    if age_h < 24 and age_h > 0: s -= 10                        # <24h = extra risk
    return round(max(0, min(100, s)), 1)


def scan():
    if KILL_SWITCH.exists():
        print('halt file present, skip scan', file=sys.stderr); return []
    pos = load_json(POSITIONS_FILE, {})
    cd = load_json(COOLDOWN_FILE, {})
    hist = load_json(HISTORY_FILE, {'vol': {}, 'alerted': {}})
    now = time.time()
    pairs = get_pairs()
    candidates = []
    seen_tokens = set()
    USDG_LO = USDG.lower(); WETH_LO = WETH.lower()
    for p in pairs:
        addrs = ((p['base_addr'] or '').lower(), (p['quote_addr'] or '').lower())
        settlement = USDG_LO if USDG_LO in addrs else (WETH_LO if WETH_LO in addrs else None)
        if settlement:
            other_addr = addrs[1] if addrs[0]==settlement else addrs[0]
            other_sym = p['quote_sym'] if addrs[0]==settlement else p['base_sym']
        else:
            continue
        if other_addr in (USDG_LO, WETH_LO): continue
        if other_addr == '0x0000000000000000000000000000000000000000': continue
        sym = (other_sym or '').upper()
        if sym in ('USDG','STEAKUSDG','GAUNTLETUSDG','SUSDG'): continue
        if sym.startswith('USDG') or sym.endswith('USDG'): continue
        liq = Decimal(str(p['liq_usd']))
        vol = Decimal(str(p['vol24_usd']))
        if liq < MIN_LIQ_USD or vol < MIN_VOL24_USD: continue
        pc = p['created_ms']
        age_h = (time.time()*1000 - pc) / 3600000 if pc else 999
        min_age = GMGN_TRENDING_MIN_AGE_HOURS if p.get('source') == 'gmgn-trending' else MIN_AGE_HOURS
        if pc and age_h < float(min_age): continue
        if in_cooldown(other_addr, cd): continue
        if other_addr in {k.lower() for k in pos.keys()}: continue
        if other_addr in seen_tokens: continue
        seen_tokens.add(other_addr)

        # snapshot vol24 → rising factor
        prev = hist['vol'].get(other_addr)
        prev_vol = Decimal(str(prev['vol24'])) if prev else Decimal(0)
        hist['vol'][other_addr] = {'vol24': float(vol), 'at': now}
        vol_rise_x = float(vol / prev_vol) if prev_vol > 0 else 1.0

        # gate: need rising volume OR very hot turnover (>10%/h)
        turnover_h = float(vol / (liq * 24)) if liq > 0 else 0.0
        is_rising = prev is not None and vol >= prev_vol * RISE_FACTOR
        is_hot = turnover_h > 0.10
        # GMGN's 1h rank is itself a live momentum signal; do not require a second
        # local 24h-rise confirmation for that source.
        if not (is_rising or is_hot or p.get('source') == 'gmgn-trending'):
            continue

        # cooldown: don't re-promote same token within ALERT_COOLDOWN_MIN
        last_alert = hist['alerted'].get(other_addr, 0)
        if now - last_alert < ALERT_COOLDOWN_MIN * 60:
            # keep as candidate but flag stale (still visible to entry_trigger)
            pass

        score = fomo_score(liq, vol, vol_rise_x, age_h)
        candidates.append({
            'token': other_addr,
            'symbol': other_sym,
            'pair': p['pair'],
            'liq_usd': str(liq),
            'vol24_usd': str(vol),
            'age_h': round(age_h,1),
            'turnover_h': turnover_h,
            'vol_rise_x': round(vol_rise_x, 2),
            'is_rising': is_rising,
            'is_hot': is_hot,
            'fomo_score': score,
            'score': score,
            'url': p.get('url'),
            'source': p.get('source'),
            'settlement_asset': 'WETH' if settlement == WETH_LO else 'USDG',
            'gmgn_rank': p.get('gmgn_rank'),
            'gmgn_smart': p.get('gmgn_smart', 0),
            'gmgn_kol': p.get('gmgn_kol', 0),
        })

    # Bound expensive on-chain quote/risk enrichment for GMGN results.
    native = [x for x in candidates if x.get('source') != 'gmgn-trending']
    gmgn_ranked = sorted((x for x in candidates if x.get('source') == 'gmgn-trending'),
                         key=lambda x: x.get('gmgn_rank') or 999)[:8]
    candidates = (native + gmgn_ranked)[:MAX_ENRICH_CANDIDATES]

    tick_hist = hist.setdefault('tick', {})
    for c in candidates:
        pools = {}
        for f in FEE_TIER_FALLBACKS:
            try:
                pl = get_pool(USDG, c['token'], f)
                if pl: pools[f] = pl
            except Exception: pass
        c['usdg_pools'] = pools
        c['has_direct_usdg_v3'] = bool(pools)
        # Volatility is the single biggest driver of LP outcome, and it can only be
        # measured from a series. Snapshot every candidate so a token has usable
        # history by the time it is considered for entry.
        c['fee_ppm'] = min(pools) if pools else FEE_TIER_FALLBACKS[0]
        rows = [r for r in tick_hist.get(c['token'], []) if now - float(r.get('timestamp', 0)) <= 21600]
        if pools:
            try:
                rows.append({'timestamp': now, 'tick': int(pool_slot0(pools[c['fee_ppm']])[1])})
            except Exception: pass
        tick_hist[c['token']] = rows[-72:]
        c['vol_hourly_pct'] = lp_risk.realized_vol_pct_per_hour(rows, now)
        c['suggested_width_pct'] = lp_risk.width_pct_for_vol(c['vol_hourly_pct'])
        c['expected_il_pct'] = lp_risk.expected_adverse_il_pct(c['vol_hourly_pct'], c['suggested_width_pct'])
        c['expected_fee_usdg'] = lp_risk.expected_fee_usdg(
            vol24_usd=c['vol24_usd'], liquidity_usd=c['liq_usd'], fee_ppm=c['fee_ppm'],
            position_usdg=POSITION_SIZE_USDG, width_pct=c['suggested_width_pct'])
        expected_il_usdg = (abs(Decimal(str(c['expected_il_pct']))) / 100 * POSITION_SIZE_USDG
                            if c['expected_il_pct'] is not None else None)
        c['expected_il_usdg'] = None if expected_il_usdg is None else str(expected_il_usdg)
        # Round trip pays the pool fee twice, plus a gas allowance.
        execution_cost = (POSITION_SIZE_USDG * Decimal(c['fee_ppm']) / Decimal(1_000_000) * 2
                          + V4_EXECUTION_COST_BUFFER_USDG)
        c['lp_edge_ok'], c['lp_edge'] = lp_risk.entry_is_economic(
            expected_fee_usdg=c['expected_fee_usdg'], expected_il_usdg=expected_il_usdg,
            execution_cost_usdg=execution_cost)
        c['expected_fee_usdg'] = None if c['expected_fee_usdg'] is None else str(c['expected_fee_usdg'])

        # V4 venue discovery + executable 1-USDG quotes. Pick by net output, not raw fee:
        # ultra-high fee pools can look attractive for farming but destroy entry/exit value.
        try:
            # Discovery sidecar can retry for minutes on Blockscout/RPC trouble.
            # One short attempt is enough for a periodic scanner tick.
            v4q = quote_v4(c['token'], 10**6, timeout=12, max_attempts=1)
            eligible = [q for q in v4q if q.get('eligible') and int(q.get('amountOut', 0)) > 0]
            eligible.sort(key=lambda q: int(q['amountOut']), reverse=True)
            c['v4_quotes'] = eligible[:5]
            c['best_v4'] = eligible[0] if eligible else None
        except Exception as e:
            c['v4_quotes'] = []
            c['best_v4'] = None
            c['v4_error'] = str(e)[:160]
        c['has_direct_usdg_v4'] = bool(c['best_v4'])
        try:
            wp = discover_pair(c['token'], WETH)
            wp.sort(key=lambda p: int(p.get('liquidity',0)), reverse=True)
            c['weth_v4_pools'] = wp[:5]
            c['best_weth_v4'] = wp[0] if wp else None
        except Exception as e:
            c['weth_v4_pools'] = []; c['best_weth_v4'] = None
            c['weth_v4_error'] = str(e)[:160]
        c['has_direct_weth_v4'] = bool(c['best_weth_v4'])
        c['settlement_options'] = (["USDG"] if c['has_direct_usdg_v4'] else []) + (["WETH"] if c['has_direct_weth_v4'] else [])
        c['has_direct_usdg'] = c['has_direct_usdg_v3'] or c['has_direct_usdg_v4']
        # V4 is executable by the sidecar but unattended entry remains fail-closed until
        # a funded lifecycle rehearsal passes; scanner still ranks it for operator visibility.
        c['preferred_venue'] = 'v4' if c['has_direct_usdg_v4'] and not c['has_direct_usdg_v3'] else 'v3'

        # Enrich every promoted candidate with GMGN security/smart-money data.
        # Entry trigger independently re-checks this gate before moving funds.
        gr = gmgn_assess(c['token'], cache_only=True)
        c['gmgn'] = gr
        top10_pct = Decimal(str(gr.get('top10_rate') or 0)) * 100
        c['top10_pct'] = float(top10_pct)
        c['gmgn_ok'] = bool(gr.get('ok')) and top10_pct <= MAX_TOP10_PCT

    # persist history + prune old vol entries (>24h)
    cutoff = now - 24 * 3600
    hist['vol'] = {k: v for k, v in hist['vol'].items() if v.get('at', 0) > cutoff}
    save_json(HISTORY_FILE, hist)

    rotating = ROTATION_REQUEST_FILE.exists()
    # A high FOMO score means high turnover, which is exactly the volatility that
    # runs a concentrated range over. Rank by whether the LP has a modelled edge
    # first, and only use the momentum score to break ties among viable pools.
    candidates.sort(key=lambda x: (
        not x.get('gmgn_ok', False),
        not x.get('lp_edge_ok', False),
        0 if (rotating and x.get('source') == 'gmgn-trending' and Decimal(x['liq_usd']) < ROTATION_MAX_LIQ_USD) else 1,
        x.get('gmgn_rank') or 999,
        -x['score']))
    return candidates, hist


def main():
    result = scan()
    if not result:
        return
    cands, hist = result
    prev = load_json(CANDIDATES_FILE, {'candidates':[], 'ts':0})
    prev_tokens = {c['token'].lower() for c in prev.get('candidates',[])}
    now = time.time()

    new_promoted = []
    for c in cands:
        tok = c['token'].lower()
        last_alert = hist.get('alerted', {}).get(tok, 0)
        if tok in prev_tokens and (now - last_alert) < ALERT_COOLDOWN_MIN * 60:
            continue
        new_promoted.append(c)
        hist.setdefault('alerted', {})[tok] = now

    save_json(HISTORY_FILE, hist)
    payload = {'ts': int(now), 'candidates': cands}
    save_json(CANDIDATES_FILE, payload)

    if new_promoted:
        lines = [f"🔍 *RH Meme LP scanner v2*: {len(new_promoted)} kandidat baru (pool total {len(cands)})"]
        for c in new_promoted[:5]:
            direct = '✅direct-USDG' if c['has_direct_usdg'] else '⚠️via-WETH'
            flag = '🚀rising' if c['is_rising'] else ('🔥hot' if c['is_hot'] else '')
            gr = c.get('gmgn') or {}
            gmgn = (f"✅GMGN SM={gr.get('smart_wallets', 0)} KOL={gr.get('kol_wallets', 0)}"
                    if c.get('gmgn_ok') else f"🚫GMGN {gr.get('reason', 'unavailable')}")
            lines.append(
                f"\n• *{c['symbol']}* score={c['fomo_score']:.0f} {flag} {direct} {gmgn}"
                f"\n  liq=${c['liq_usd'][:8]} vol24=${c['vol24_usd'][:9]} "
                f"turnover/h={c['turnover_h']*100:.1f}% rise×{c['vol_rise_x']}"
                f"\n  `{c['token']}`"
            )
        print('\n'.join(lines))

if __name__=='__main__':
    # Exit quietly before Hermes' hard 120s script timeout. This monitor is
    # read-only and the next periodic tick retries incomplete discovery.
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))
    signal.alarm(SCAN_MAX_WALL_SEC)
    try: main()
    except Exception as e:
        print(json.dumps({'error': str(e)[:300]}), file=sys.stderr)
        sys.exit(0)  # transient — don't error cron
