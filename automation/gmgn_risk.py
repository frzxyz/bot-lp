"""GMGN enrichment + conservative risk gate for Robinhood Chain LP candidates.

No secret is read or logged here: gmgn-cli owns credentials in ~/.config/gmgn/.env.
Cache limits API use; any unavailable/invalid result fails closed at entry time.
"""
import json
import subprocess
import time
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parent / "state" / "gmgn_cache.json"
CACHE_TTL = 15 * 60
CMD_TIMEOUT = 25


def _load_cache():
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(data):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(CACHE_FILE)


def _query(kind, token):
    cmd = ["gmgn-cli", "token", kind, "--chain", "robinhood", "--address", token, "--raw"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=CMD_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"gmgn {kind} rc={r.returncode}")[:180])
    lines = [x.strip() for x in r.stdout.splitlines() if x.strip().startswith("{")]
    if not lines:
        raise RuntimeError(f"gmgn {kind}: no JSON")
    data = json.loads(lines[-1])
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(f"gmgn {kind}: invalid response")
    return data


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def assess(token, force=False, cache_only=False):
    token = token.lower()
    cache = _load_cache()
    hit = cache.get(token)
    now = int(time.time())
    if not force and hit and now - int(hit.get("at", 0)) < CACHE_TTL:
        return hit["result"]

    # Scanner ticks must stay bounded below Hermes' 120s cron ceiling.  A cache
    # miss is fail-closed here; entry_trigger performs the mandatory live recheck.
    if cache_only:
        return {"ok": False, "available": False, "hard_stop": True,
                "reason": "GMGN cache miss", "flags": ["gmgn_cache_miss"]}

    try:
        info = _query("info", token)
        security = _query("security", token)
    except Exception as exc:
        return {"ok": False, "available": False, "hard_stop": True,
                "reason": f"GMGN unavailable: {str(exc)[:120]}", "flags": ["gmgn_unavailable"]}

    stat = info.get("stat") or {}
    tags = info.get("wallet_tags_stat") or {}
    top10 = _num(security.get("top_10_holder_rate", stat.get("top_10_holder_rate")))
    buy_tax = _num(security.get("buy_tax"))
    sell_tax = _num(security.get("sell_tax"))
    entrapment = _num(stat.get("top_entrapment_trader_percentage"))
    bundler_volume = _num(stat.get("top_bundler_trader_percentage"))
    rat_volume = _num(stat.get("top_rat_trader_percentage"))
    bundler_wallets = int(_num(tags.get("bundler_wallets")))
    smart_wallets = int(_num(tags.get("smart_wallets")))
    kol_wallets = int(_num(tags.get("renowned_wallets")))

    flags = []
    hard = []
    if security.get("is_honeypot") is True or security.get("honeypot") == 1:
        hard.append("honeypot")
    if security.get("is_blacklist") is True or security.get("blacklist") == 1:
        hard.append("blacklist")
    if security.get("is_open_source") is False or security.get("open_source") == 0:
        hard.append("closed_source")
    if max(buy_tax, sell_tax) > 0.10:
        hard.append("tax_over_10pct")
    if top10 > 0.50:
        hard.append("top10_over_50pct")
    # GMGN RH currently exposes behavior ratios rather than a direct wash flag.
    if entrapment > 0.50:
        hard.append("wash_entrapment_over_50pct")
    if bundler_volume > 0.35:
        hard.append("bundler_volume_over_35pct")
    if rat_volume > 0.20:
        hard.append("insider_volume_over_20pct")

    if top10 > 0.20: flags.append("top10_concentrated")
    if max(buy_tax, sell_tax) > 0.05: flags.append("tax_over_5pct")
    if smart_wallets == 0: flags.append("no_smart_money")
    if bundler_wallets > 20: flags.append("many_bundler_wallets")

    result = {
        "ok": not hard,
        "available": True,
        "hard_stop": bool(hard),
        "reason": "clean" if not hard else ", ".join(hard),
        "flags": hard + flags,
        "symbol": info.get("symbol", ""),
        "liquidity_usd": _num(info.get("liquidity")),
        "top10_rate": top10,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "entrapment_rate": entrapment,
        "bundler_volume_rate": bundler_volume,
        "rat_volume_rate": rat_volume,
        "smart_wallets": smart_wallets,
        "kol_wallets": kol_wallets,
        "bundler_wallets": bundler_wallets,
        "checked_at": now,
    }
    cache[token] = {"at": now, "result": result}
    _save_cache(cache)
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        raise SystemExit("usage: gmgn_risk.py TOKEN")
    print(json.dumps(assess(sys.argv[1], force=True), indent=2, sort_keys=True))
