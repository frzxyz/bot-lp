"""Candle-based realized volatility for candidates that are not yet held.

The scanner used to estimate a candidate's volatility from its own tick
snapshots — one sample per 15-minute scan. A token seen for the first time
therefore had a single sample, ``realized_vol_pct_per_hour`` returned ``None``,
the IL half of the entry gate could not be computed, and ``entry_is_economic``
correctly refused it as ``incomplete_economics``. A candidate had to survive two
scans just to be *evaluated*, and even then two points 15 minutes apart is a poor
volatility estimate. The half of the edge test that decides whether an LP is
worth opening was running on the weakest data in the system.

GeckoTerminal already serves this bot's pool discovery, and its OHLCV endpoint
returns up to 1000 candles per pool, so the same API can supply dozens of real
observations immediately.

Candle closes are converted to Uniswap tick space and handed to the existing
:func:`risk.realized_vol_pct_per_hour`, so the volatility maths stays in one
tested place. Only *returns* matter to volatility, and log-returns are invariant
under both the USD-vs-quote denomination and base/quote inversion, so neither
choice affects the result.
"""
from __future__ import annotations

import math
import os
import time
from collections.abc import Iterable, Mapping
from typing import Any

import requests

TICK_LOG = math.log(1.0001)
ENDPOINT = 'https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/{timeframe}'
NETWORK = os.environ.get('RH_GT_NETWORK', 'robinhood')
UA = {'user-agent': 'rh-meme-lp/1.0', 'accept': 'application/json'}

#: 5-minute candles over six hours matches RANGE_HORIZON_HOURS: long enough to be
#: stable, short enough to still reflect the regime being entered.
AGGREGATE_MINUTES = int(os.environ.get('RH_OHLCV_AGGREGATE_MINUTES', '5'))
CANDLE_LIMIT = int(os.environ.get('RH_OHLCV_LIMIT', '72'))
VOL_WINDOW_SECONDS = int(os.environ.get('RH_OHLCV_WINDOW_SECONDS', str(6 * 3600)))
CACHE_SECONDS = int(os.environ.get('RH_OHLCV_CACHE_SECONDS', '240'))
MIN_CANDLES = int(os.environ.get('RH_OHLCV_MIN_CANDLES', '6'))

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _price_to_tick(price: float) -> int:
    """Uniswap tick for a price. Only differences are used, so the scale cancels."""
    return int(math.log(price) / TICK_LOG)


def fetch_candles(pool: str, *, network: str | None = None, timeout: int = 15,
                  aggregate: int | None = None, limit: int | None = None) -> list[list[Any]]:
    """Raw ``ohlcv_list`` for a pool: ``[[ts, open, high, low, close, volume], ...]``.

    Values stay loosely typed because they arrive from JSON; the caller coerces
    only the two fields it needs. Returns an empty list rather than raising: a
    missing candle series must degrade the estimate, never abort a scan.
    """
    url = ENDPOINT.format(network=network or NETWORK, pool=pool, timeframe='minute')
    params: dict[str, Any] = {
        'aggregate': aggregate or AGGREGATE_MINUTES, 'limit': limit or CANDLE_LIMIT,
        'currency': 'usd', 'token': 'base'}
    try:
        r = requests.get(url, params=params, headers=UA, timeout=timeout)
        if r.status_code != 200:
            return []
        rows = ((r.json().get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []
        return [list(row) for row in rows if isinstance(row, (list, tuple)) and len(row) >= 5]
    except Exception:
        return []


def candle_rows(pool: str, *, now: float | None = None, **kw: Any) -> list[dict[str, Any]]:
    """Candles as ``{'timestamp', 'tick'}`` rows, shaped for risk.realized_vol_*.

    Cached briefly so several candidates in one scan, or successive scans inside
    the cache window, do not re-hit a rate-limited public API.
    """
    now = float(time.time() if now is None else now)
    key = str(pool).lower()
    hit = _cache.get(key)
    if hit and now - hit[0] <= CACHE_SECONDS:
        return hit[1]

    rows: list[dict[str, Any]] = []
    for candle in fetch_candles(pool, **kw):
        try:
            ts, close = float(candle[0]), float(candle[4])
        except (TypeError, ValueError):
            continue
        # A zero or negative close is a gap in the feed, not a price.
        if close <= 0 or ts <= 0:
            continue
        rows.append({'timestamp': ts, 'tick': _price_to_tick(close)})
    rows.sort(key=lambda r: r['timestamp'])
    _cache[key] = (now, rows)
    return rows


def realized_vol_pct_per_hour(pool: str, *, now: float | None = None,
                              window_seconds: int | None = None, **kw: Any) -> float | None:
    """Candle-derived volatility in percent per hour, or ``None`` if unavailable.

    ``None`` is deliberate rather than a default: an unmeasurable candidate must
    fail the economic gate closed, exactly as it did before.
    """
    import risk as lp_risk

    now = float(time.time() if now is None else now)
    rows = candle_rows(pool, now=now, **kw)
    if len(rows) < MIN_CANDLES:
        return None
    return lp_risk.realized_vol_pct_per_hour(
        rows, now, window_seconds=window_seconds or VOL_WINDOW_SECONDS)


def best_effort_vol(pool: str | None, fallback_rows: Iterable[Mapping[str, Any]] | None,
                    now: float | None = None) -> tuple[float | None, str]:
    """Volatility with provenance: candles when available, tick snapshots otherwise.

    Returns ``(vol_pct_per_hour, source)``. The source is recorded on the
    candidate so a rejected entry can be traced to the data it was judged on.
    """
    import risk as lp_risk

    now = float(time.time() if now is None else now)
    if pool:
        vol = realized_vol_pct_per_hour(pool, now=now)
        if vol is not None:
            return vol, 'ohlcv'
    vol = lp_risk.realized_vol_pct_per_hour(fallback_rows or [], now)
    return vol, ('tick_history' if vol is not None else 'unavailable')
