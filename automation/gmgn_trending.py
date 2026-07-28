"""Cached GMGN trending discovery for Robinhood LP rotation (read-only)."""
import json, subprocess, time
from pathlib import Path

CACHE = Path('/root/.hermes/state/rh_meme_lp/gmgn_trending.json')
TTL = 10 * 60

def _rank(payload):
    if isinstance(payload, dict):
        data = payload.get('data')
        if isinstance(data, dict) and isinstance(data.get('rank'), list): return data['rank']
        if isinstance(payload.get('rank'), list): return payload['rank']
    return []

def trending(max_liquidity=100000, force=False):
    now = int(time.time())
    try:
        old = json.loads(CACHE.read_text())
        if not force and now - int(old.get('ts', 0)) < TTL: return old.get('tokens', [])
    except Exception: pass
    cmd = ['gmgn-cli','market','trending','--chain','robinhood','--interval','1h',
           '--min-liquidity','3000','--max-liquidity',str(int(max_liquidity)),
           '--order-by','volume','--limit','50','--raw']
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    if r.returncode: raise RuntimeError((r.stderr or r.stdout or 'GMGN trending failed')[:180])
    lines = [x for x in r.stdout.splitlines() if x.strip().startswith('{')]
    if not lines: raise RuntimeError('GMGN trending returned no JSON')
    clean = []
    for x in _rank(json.loads(lines[-1])):
        liq = float(x.get('liquidity') or 0)
        if not (3000 <= liq < float(max_liquidity)): continue
        if x.get('is_honeypot') in (1, True) or x.get('is_wash_trading') is True: continue
        if float(x.get('rug_ratio') or 0) > .30: continue
        if float(x.get('top_10_holder_rate') or 0) > .50: continue
        clean.append(x)
    CACHE.write_text(json.dumps({'ts': now, 'tokens': clean}, indent=2))
    return clean

if __name__ == '__main__': print(json.dumps(trending(force=True), indent=2))