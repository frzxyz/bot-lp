"""Pure/evidence based V4 policy helpers.  No function in this module moves funds."""
import json, math, time
from decimal import Decimal
import config as cfg

def tick_distance(tick, lower, upper):
    """Return (outside %, class, edge-warning % or None). Percent is orientation neutral."""
    tick,lower,upper=int(tick),int(lower),int(upper)
    if tick < lower: d=(1.0001**(lower-tick)-1)*100
    elif tick > upper: d=(1.0001**(tick-upper)-1)*100
    else:
        edge=min((1.0001**(tick-lower)-1)*100,(1.0001**(upper-tick)-1)*100)
        return 0.0,'in_range',edge if edge<=5 else None
    return d, ('shallow' if d<=5 else 'medium' if d<=10 else 'deep'), None

def append_snapshot(history, key, snapshot, now=None):
    now=float(time.time() if now is None else now); row={'timestamp':now}
    for a in ('tick','valueUsd','feeUsd','liquidity','liquiditySource'):
        if snapshot.get(a) is not None: row[a]=snapshot[a]
    rows=[x for x in history.get(key,[]) if float(x.get('timestamp',0))>=now-21600]
    rows.append(row); history[key]=rows; return rows

def metrics(rows, now=None):
    now=float(time.time() if now is None else now)
    valid=[r for r in rows if r.get('tick') is not None and float(r.get('timestamp',0))<=now]
    if not valid: return {'fresh':False}
    valid.sort(key=lambda x:float(x['timestamp'])); spot=int(valid[-1]['tick'])
    def move(seconds):
        old=[r for r in valid if float(r['timestamp'])<=now-seconds]
        if not old:return None
        return (1.0001**(spot-int(old[-1]['tick']))-1)*100
    recent=[r for r in valid if float(r['timestamp'])>=now-3600]
    deltas=[int(b['tick'])-int(a['tick']) for a,b in zip(recent,recent[1:])]
    vol=(math.sqrt(sum(d*d for d in deltas)/len(deltas)) if deltas else 0)*math.log(1.0001)*100
    # Robust sample-mean tick (trim one each side with >=5 samples); labelled local, not an oracle.
    ts=sorted(int(r['tick']) for r in recent)
    if len(ts)>=5: ts=ts[1:-1]
    twap=sum(ts)/len(ts) if ts else spot
    dev=abs((1.0001**(spot-twap)-1)*100)
    liqs=[Decimal(str(r['liquidity'])) for r in recent if r.get('liquidity') is not None]
    drop=float(max(Decimal(0),(max(liqs)-liqs[-1])/max(liqs)*100)) if liqs and max(liqs)>0 else None
    return {'fresh':now-float(valid[-1]['timestamp'])<=600,'spot_tick':spot,'twap_tick':twap,
            'spot_twap_pct':dev,'momentum_15m':move(900),'momentum_30m':move(1800),
            'momentum_1h':move(3600),'realized_vol_1h_pct':vol,'liquidity_drop_pct':drop}

def range_width(vol):
    if vol is None:return None,None
    return (25,'normal') if vol<5 else (35,'high') if vol<10 else (50,'extreme')

def aligned_range(center_tick, width_percent, spacing):
    half=math.log1p(float(width_percent)/100)/math.log(1.0001)
    lo=math.floor((center_tick-half)/spacing)*spacing; hi=math.ceil((center_tick+half)/spacing)*spacing
    return lo,hi

def continuous_timer(pos, classification, now):
    """Reset on class change/in-range; first observation can never authorize action."""
    if classification=='in_range': pos.pop('v4_oor_class',None); pos.pop('v4_oor_since',None); return 0
    if pos.get('v4_oor_class')!=classification:
        pos['v4_oor_class']=classification; pos['v4_oor_since']=int(now); return 0
    return max(0,int(now)-int(pos.get('v4_oor_since',now)))

def evidence_gates(kind, m, risk, economics_ok=True):
    reasons=[]
    if not m.get('fresh'): reasons.append('market_history_stale')
    if not risk or not risk.get('ok') or risk.get('hard_stop'): reasons.append('gmgn_not_clean')
    mom=m.get('momentum_15m'); mom30=m.get('momentum_30m'); liq=m.get('liquidity_drop_pct')
    if None in (mom,liq): reasons.append('missing_momentum_or_liquidity')
    devlim,momlim,liqlim=(3,3,15) if kind=='shallow' else (5,5,10)
    if m.get('spot_twap_pct',999)>devlim: reasons.append('spot_twap_deviation')
    if mom is not None and abs(mom)>momlim: reasons.append('momentum')
    if kind=='medium' and (mom30 is None or (mom is not None and abs(mom)>abs(mom30))): reasons.append('momentum_accelerating')
    if liq is not None and liq>liqlim: reasons.append('liquidity_drop')
    if not economics_ok: reasons.append('economics')
    return not reasons,reasons

def economics(rows, execution_cost):
    """Observed fee slope only. Unknown cost/rate fails closed."""
    if execution_cost is None:return False,{}
    fees=[r for r in rows if r.get('feeUsd') is not None]
    if len(fees)<2:return False,{}
    dt=float(fees[-1]['timestamp'])-float(fees[0]['timestamp'])
    if dt<=0:return False,{}
    expected=max(Decimal(0),Decimal(str(fees[-1]['feeUsd']))-Decimal(str(fees[0]['feeUsd'])))*Decimal(10800)/Decimal(str(dt))
    cost=Decimal(str(execution_cost))+cfg.V4_EXECUTION_COST_BUFFER_USDG
    return expected>=cost*2,{'expected_fee_3h':str(expected),'execution_cost':str(cost)}

def execution_cost_from_preflight(amount_usdg_raw, preflight, decimals=6):
    """Derive exact-size round-trip loss from sidecar raw amounts; never guess."""
    if not isinstance(preflight,dict): return None
    explicit=preflight.get('roundtripCostUsd',preflight.get('executionCostUsd'))
    if explicit is not None: return Decimal(str(explicit))
    reverse=preflight.get('reverseRaw')
    if reverse is None: return None
    amount=int(amount_usdg_raw); returned=int(reverse)
    if amount<=0 or returned<0: return None
    return Decimal(max(0,amount-returned))/Decimal(10**int(decimals))

def log_decision(record):
    cfg.V4_DECISION_LOG_FILE.parent.mkdir(parents=True,exist_ok=True)
    with cfg.V4_DECISION_LOG_FILE.open('a') as f:f.write(json.dumps(record,sort_keys=True,default=str)+'\n')