"""Durable unified V3/V4 lifecycle journal and fail-closed safety boundary.

The journal is intent-first: ``prepare`` must commit before any external side effect.
It deliberately never discovers wallet inventory; recovery is limited to explicit records.
"""
from __future__ import annotations
import fcntl, hashlib, json, os, time, uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
import config as cfg

JOURNAL = cfg.STATE_DIR / "lifecycle_ops.json"
LOCK = cfg.STATE_DIR / "lifecycle_ops.lock"
HARD_HALT = cfg.STATE_DIR / "HARD_HALT_RECOVERY"
DAEMON_LOCK = cfg.STATE_DIR / "failsafe_daemon.lock"
PHASES = {"prepared","preflight_passed","broadcasting","confirming","postcheck","completed","compensating","retry","manual_attention"}
ACTIVE = PHASES-{"completed","manual_attention"}
BACKOFF = (10,20,30,60,120,300)


def enabled(): return os.environ.get("LIFECYCLE_FAILSAFE","true").lower() in ("1","true","yes")
def now(): return int(time.time())

@contextmanager
def _lock(path=LOCK, nonblock=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+") as f:
        flags=fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblock else 0)
        fcntl.flock(f,flags); yield

def _read_unlocked(path=None):
    path=path or JOURNAL
    try:
        x=json.loads(path.read_text()); return x if isinstance(x,dict) else {"schema":1,"operations":{}}
    except FileNotFoundError:return {"schema":1,"operations":{}}
    except Exception as e: raise RuntimeError(f"lifecycle journal corrupt: {e}")

def _write_unlocked(doc,path=None):
    path=path or JOURNAL
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".{os.getpid()}.tmp")
    with tmp.open("w") as f:
        json.dump(doc,f,sort_keys=True,indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path); d=os.open(path.parent,os.O_DIRECTORY)
    try: os.fsync(d)
    finally: os.close(d)

def load(path=None):
    with _lock(): return _read_unlocked(path)

def _key(version,operation,token,nft,idempotency):
    material=idempotency or f"{version}:{operation}:{str(token).lower()}:{nft or ''}:{uuid.uuid4().hex}"
    return hashlib.sha256(material.encode()).hexdigest()[:32]

def prepare(version:str, operation:str, *, token:str|None=None, nft:Any=None,
            before:dict|None=None, caps:dict|None=None, expected:dict|None=None,
            recovery_policy:str|None=None, deadline:int|None=None,
            idempotency_key:str|None=None, metadata:dict|None=None) -> dict:
    if version not in ("v3","v4") or operation not in ("open","close","rebalance"):
        raise ValueError("unsupported lifecycle operation")
    ts=now(); oid=_key(version,operation,token,nft,idempotency_key)
    with _lock():
        doc=_read_unlocked(); old=doc["operations"].get(oid)
        if old:
            if old["phase"]=="completed": return old
            if old["phase"] in ACTIVE: return old
        rec={"op_id":oid,"idempotency_key":idempotency_key or oid,"version":version,"operation":operation,
             "token":token,"nft":None if nft is None else str(nft),"phase":"prepared","before":before or {},
             "intended_caps":caps or {},"txs":[],"nonces":[],"deadline":deadline or ts+900,
             "expected_postconditions":expected or {},"recovery_policy":recovery_policy or "reconcile_then_retry",
             "retry_schedule":list(BACKOFF),"retries":0,"max_retries":6,"max_cost_wei":0,
             "cost_wei":0,"next_retry":ts,"last_error":None,"created_at":ts,"updated_at":ts,
             "metadata":metadata or {}}
        doc["operations"][oid]=rec; _write_unlocked(doc); return rec

def transition(op_or_id, phase, *, error=None, tx=None, nonce=None, changes=None):
    if phase not in PHASES: raise ValueError("invalid lifecycle phase")
    oid=op_or_id if isinstance(op_or_id,str) else op_or_id["op_id"]
    with _lock():
        doc=_read_unlocked(); rec=doc["operations"].get(oid)
        if not rec: raise KeyError(oid)
        if rec["phase"]=="completed" and phase!="completed": raise RuntimeError("completed operation is immutable")
        rec["phase"]=phase; rec["updated_at"]=now()
        if error is not None: rec["last_error"]=str(error)[:500]
        if tx:
            item=dict(tx) if isinstance(tx,dict) else {"hash":str(tx)}
            if not item.get("hash"): raise ValueError("tx hash required")
            if not any(x.get("hash")==item["hash"] for x in rec["txs"]): rec["txs"].append(item)
        if nonce is not None and int(nonce) not in rec["nonces"]: rec["nonces"].append(int(nonce))
        if changes: rec.update(changes)
        doc["operations"][oid]=rec; _write_unlocked(doc)
    emit(rec); return rec

def fail(op, exc, compensation=False):
    retries=int(op.get("retries",0))+1; ts=now(); schedule=op.get("retry_schedule") or list(BACKOFF)
    phase="manual_attention" if retries>int(op.get("max_retries",6)) else ("compensating" if compensation else "retry")
    delay=schedule[min(retries-1,len(schedule)-1)]
    return transition(op,phase,error=exc,changes={"retries":retries,"next_retry":ts+delay})

def emit(rec):
    msg={"op":rec["op_id"],"version":rec["version"],"action":rec["operation"],"phase":rec["phase"],
         "tx":(rec.get("txs") or [{}])[-1].get("hash"),"error":rec.get("last_error"),
         "exposure_raw":rec.get("metadata",{}).get("exposure_raw",0),"next_retry":rec.get("next_retry")}
    print(json.dumps(msg,separators=(",",":"),sort_keys=True),flush=True)

def assert_new_strategy_allowed(version):
    if not enabled(): return
    if cfg.KILL_SWITCH.exists(): raise RuntimeError("kill switch blocks new strategy operation")
    if version=="v4" and not (cfg.ATOMIC_LP_ONLY and cfg.ATOMIC_V4_EXECUTOR_ADDRESS):
        raise RuntimeError("V4_ATOMIC_UNAVAILABLE: unattended lifecycle blocked")

def recovery_allowed(): return not HARD_HALT.exists()

def reconcile_receipts(rec:dict, receipt_fn:Callable[[str],Any],
                       latest_nonce_fn:Callable[[],int]|None=None) -> dict:
    """Read-only receipt reconciliation. Never resends/replaces a nonce."""
    if rec["phase"] not in ("broadcasting","confirming","retry","compensating"): return rec
    uncertain=False
    for tx in rec.get("txs",[]):
        try: receipt=receipt_fn(tx["hash"])
        except Exception as e: return fail(rec,f"receipt probe: {e}")
        if not receipt: uncertain=True; continue
        tx["receipt_status"]=int(receipt.get("status",0)); tx["blockNumber"]=receipt.get("blockNumber")
        if int(receipt.get("status",0))==0:
            # Atomic V3 revert has no compensation: all executor effects reverted.
            return transition(rec,"manual_attention",error="transaction reverted; no compensating swap")
    if uncertain:
        return transition(rec,"confirming",changes={"next_retry":now()+10,"txs":rec["txs"]})
    return transition(rec,"postcheck",changes={"txs":rec["txs"]})

@contextmanager
def daemon_singleton():
    try:
        with _lock(DAEMON_LOCK,nonblock=True): yield
    except BlockingIOError: raise RuntimeError("failsafe daemon already running")
