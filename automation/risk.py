"""Pure concentrated-liquidity risk math.  No function in this module moves funds.

Everything here is deterministic and unit-testable so exit decisions can be proven
offline instead of being discovered on mainnet.

Orientation convention: ``x`` is always the volatile token and ``y`` is always the
stable settlement asset (USDG), and every price is *stable per token*.  Uniswap's
own formulas are written in token0/token1 terms, so callers must map ticks through
:func:`range_prices_from_ticks`, which handles the inversion that occurs when USDG
sorts as token0.  Getting that inversion wrong silently mislabels a fully
token-heavy position as fully stable, which is why it has its own tests.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import config as cfg

#: A time series row: ``{'timestamp': epoch, 'tick': int, ...}``.  Values stay
#: loosely typed because the rows come straight from persisted JSON snapshots.
Row = Mapping[str, Any]
#: Anything convertible to :class:`Decimal` — ``str`` on the wire, ``Decimal``
#: internally, occasionally ``int``/``float`` from a sidecar quote.
Numeric = Decimal | str | int | float

TICK_BASE = 1.0001
LN_TICK_BASE = math.log(TICK_BASE)

BELOW = 'below'
IN_RANGE = 'in_range'
ABOVE = 'above'


# --------------------------------------------------------------------------- #
# tick / price conversion
# --------------------------------------------------------------------------- #

def raw_price(tick: int) -> float:
    """token1-per-token0 in raw (undecimalized) units."""
    return TICK_BASE ** int(tick)


def range_prices_from_ticks(tick_lower: int, tick_upper: int, token_is_token0: bool) -> tuple[float, float]:
    """Return (price_lower, price_upper) as raw stable-per-token, always ascending.

    When USDG sorts as token0 the pool quotes token-per-USDG, so an increasing tick
    means a *falling* token price and the bounds swap.
    """
    lo, hi = raw_price(tick_lower), raw_price(tick_upper)
    if token_is_token0:
        return lo, hi
    return 1.0 / hi, 1.0 / lo


def price_from_tick(tick: int, token_is_token0: bool) -> float:
    """Current raw stable-per-token price implied by a pool tick."""
    p = raw_price(tick)
    return p if token_is_token0 else 1.0 / p


# --------------------------------------------------------------------------- #
# position composition and value
# --------------------------------------------------------------------------- #

def position_amounts(price: float, lower: float, upper: float, liquidity: float = 1.0) -> tuple[float, float]:
    """Raw (token, stable) amounts held by ``liquidity`` at ``price``.

    Below the range the position is entirely token; above it, entirely stable.
    """
    if not (lower > 0 and upper > lower and price > 0):
        raise ValueError('range must satisfy 0 < lower < upper and price > 0')
    sp = math.sqrt(min(max(price, lower), upper))
    sa, sb = math.sqrt(lower), math.sqrt(upper)
    return liquidity * (1.0 / sp - 1.0 / sb), liquidity * (sp - sa)


def position_value(price: float, lower: float, upper: float, liquidity: float = 1.0) -> float:
    """Position value denominated in the stable asset, raw units."""
    token, stable = position_amounts(price, lower, upper, liquidity)
    return token * price + stable


def token_exposure_pct(price: float, lower: float, upper: float) -> float:
    """Share of position value currently sitting in the volatile token.

    This is the number that matters for a memecoin LP: 100 means the range has been
    fully run over and the position is now an unhedged bag of the token.
    """
    token, stable = position_amounts(price, lower, upper)
    value = token * price + stable
    return 0.0 if value <= 0 else token * price / value * 100.0


def impermanent_loss_pct(entry_price: float, price: float, lower: float, upper: float) -> float:
    """LP value minus HODL value at ``price``, as a percent of HODL (negative = loss).

    HODL means keeping the exact token/stable mix the position was minted with, so
    this isolates the cost of providing liquidity from the token's own direction.
    """
    token0, stable0 = position_amounts(entry_price, lower, upper)
    hodl = token0 * price + stable0
    if hodl <= 0:
        return 0.0
    return (position_value(price, lower, upper) / hodl - 1.0) * 100.0


def nav_usdg(*, tick: int, tick_lower: int, tick_upper: int, liquidity: int,
             token_is_token0: bool, stable_decimals: int = cfg.USDG_DECIMALS) -> Decimal:
    """Live position NAV in whole USDG, derived from on-chain liquidity and ticks.

    The manager previously tracked only spot price, which cannot distinguish a
    position that has quietly converted into the falling token from one that is
    still balanced.  Working in raw price space means the token's own decimals
    cancel out, so only the stable side needs scaling.
    """
    lower, upper = range_prices_from_ticks(tick_lower, tick_upper, token_is_token0)
    price = price_from_tick(tick, token_is_token0)
    token_raw, stable_raw = position_amounts(price, lower, upper, float(liquidity))
    # price is raw stable-per-token, so token_raw * price is already raw stable.
    return Decimal(str((token_raw * price + stable_raw) / (10 ** stable_decimals)))


def live_exposure_pct(*, tick: int, tick_lower: int, tick_upper: int, token_is_token0: bool) -> float:
    lower, upper = range_prices_from_ticks(tick_lower, tick_upper, token_is_token0)
    return token_exposure_pct(price_from_tick(tick, token_is_token0), lower, upper)


# --------------------------------------------------------------------------- #
# range side (asymmetric: only one side carries token risk)
# --------------------------------------------------------------------------- #

def range_side(*, tick: int, tick_lower: int, tick_upper: int, token_is_token0: bool) -> str:
    """Classify the position by *economic* side, not raw tick comparison.

    ``BELOW`` means the token price fell through the range and the position is now
    100% token — the dangerous side.  ``ABOVE`` means it rose through and the
    position is 100% stable — riskless, and worth no emergency gas.
    """
    lower, upper = range_prices_from_ticks(tick_lower, tick_upper, token_is_token0)
    price = price_from_tick(tick, token_is_token0)
    if price < lower:
        return BELOW
    if price > upper:
        return ABOVE
    return IN_RANGE


# --------------------------------------------------------------------------- #
# volatility
# --------------------------------------------------------------------------- #

def realized_vol_pct_per_hour(rows: Iterable[Row], now: float | None = None,
                              window_seconds: int = 3600) -> float | None:
    """Time-normalized realized volatility of a tick series, in percent per hour.

    Normalizing by elapsed time rather than sample count keeps the figure stable
    when the scheduler skips or bunches ticks; a plain per-sample standard
    deviation silently rescales with the polling interval.
    """
    import time as _time
    now = float(_time.time() if now is None else now)
    pts = sorted(
        ((float(r['timestamp']), int(r['tick'])) for r in rows
         if r.get('tick') is not None and r.get('timestamp') is not None
         and now - float(r['timestamp']) <= window_seconds and float(r['timestamp']) <= now),
        key=lambda p: p[0])
    var_sum = 0.0
    elapsed = 0.0
    for (t0, k0), (t1, k1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        r = (k1 - k0) * LN_TICK_BASE
        var_sum += r * r
        elapsed += dt
    if elapsed <= 0:
        return None
    return math.sqrt(var_sum / elapsed * 3600.0) * 100.0


def width_pct_for_vol(vol_hourly_pct: float | None, *, horizon_hours: float | None = None,
                      sigmas: float | None = None,
                      floor_pct: float | None = None, cap_pct: float | None = None) -> float | None:
    """Half-width (in percent) sized so a ``sigmas``-move over the horizon stays in range.

    Returns ``None`` when volatility is unknown so callers fail closed rather than
    minting a range against a guess.
    """
    if vol_hourly_pct is None or vol_hourly_pct < 0:
        return None
    horizon = float(cfg.RANGE_HORIZON_HOURS if horizon_hours is None else horizon_hours)
    k = float(cfg.RANGE_SIGMAS if sigmas is None else sigmas)
    lo = float(cfg.RANGE_MIN_PCT if floor_pct is None else floor_pct)
    hi = float(cfg.RANGE_MAX_PCT if cap_pct is None else cap_pct)
    return min(hi, max(lo, vol_hourly_pct * math.sqrt(max(horizon, 0.0)) * k))


def expected_adverse_il_pct(vol_hourly_pct: float | None, width_pct: float | None, *,
                            horizon_hours: float | None = None, sigmas: float = 1.0) -> float | None:
    """IL if the token makes one adverse move of ``sigmas`` over the horizon.

    Only the downside is evaluated: an upside break converts the position to stable
    and caps the loss, while a downside break leaves it holding the token.
    """
    if vol_hourly_pct is None or width_pct is None or width_pct <= 0:
        return None
    horizon = float(cfg.RANGE_HORIZON_HOURS if horizon_hours is None else horizon_hours)
    move = vol_hourly_pct * math.sqrt(max(horizon, 0.0)) * sigmas / 100.0
    entry = 1.0
    lower, upper = entry * (1 - width_pct / 100.0), entry * (1 + width_pct / 100.0)
    if lower <= 0:
        return None
    return impermanent_loss_pct(entry, max(entry * (1 - move), lower * 1e-6), lower, upper)


# --------------------------------------------------------------------------- #
# economics
# --------------------------------------------------------------------------- #

def capital_efficiency(width_pct: float | None) -> float | None:
    """Fee multiplier of a ``±width_pct`` range versus the same capital full-range.

    Concentrating liquidity multiplies fee income *and* the IL from a given move,
    which is why the two must be compared at the same width rather than assuming a
    tighter range is strictly better.
    """
    if width_pct is None or not (0 < width_pct < 100):
        return None
    w = width_pct / 100.0
    denominator = 2.0 - 1.0 / math.sqrt(1 + w) - math.sqrt(1 - w)
    return None if denominator <= 0 else 2.0 / denominator


def expected_fee_usdg(*, vol24_usd: Numeric, liquidity_usd: Numeric, fee_ppm: Numeric,
                      position_usdg: Numeric, width_pct: float | None,
                      horizon_hours: float | None = None) -> Decimal | None:
    """Fee income the position should earn over the horizon at its pool share.

    Uses the pool's own 24h volume rather than an assumed APR, so a pool that is
    merely large but idle cannot look profitable.
    """
    k = capital_efficiency(width_pct)
    if k is None:
        return None
    try:
        vol24 = Decimal(str(vol24_usd)); liq = Decimal(str(liquidity_usd))
        size = Decimal(str(position_usdg)); fee = Decimal(str(fee_ppm)) / Decimal(1_000_000)
    except Exception:
        return None
    if liq <= 0 or size <= 0 or vol24 < 0 or fee <= 0:
        return None
    horizon = Decimal(str(cfg.RANGE_HORIZON_HOURS if horizon_hours is None else horizon_hours))
    effective = size * Decimal(str(k))
    return vol24 * horizon / 24 * fee * (effective / (liq + effective))


def entry_is_economic(*, expected_fee_usdg: Numeric | None, expected_il_usdg: Numeric | None,
                      execution_cost_usdg: Numeric | None,
                      margin: Decimal | None = None) -> tuple[bool, dict[str, str]]:
    """Fees over the horizon must cover adverse IL plus round-trip cost, with margin.

    Any unknown input fails closed: an LP whose edge cannot be computed does not
    have one.
    """
    if expected_fee_usdg is None or expected_il_usdg is None or execution_cost_usdg is None:
        return False, {'reason': 'incomplete_economics'}
    m = Decimal(str(cfg.ENTRY_EDGE_MARGIN if margin is None else margin))
    fee = Decimal(str(expected_fee_usdg))
    cost = abs(Decimal(str(expected_il_usdg))) + Decimal(str(execution_cost_usdg))
    return fee >= cost * m, {'expected_fee_usdg': str(fee), 'required_usdg': str(cost * m)}


def gas_within_budget(gas_cost_usdg: Numeric | None, position_usdg: Numeric | None,
                      max_pct: Numeric | None = None) -> bool:
    """Reject an action whose gas eats more than ``max_pct`` of the position."""
    if gas_cost_usdg is None or position_usdg is None:
        return False
    position = Decimal(str(position_usdg))
    if position <= 0:
        return False
    limit = Decimal(str(cfg.MAX_TX_COST_PCT if max_pct is None else max_pct))
    return Decimal(str(gas_cost_usdg)) / position * 100 <= limit


# --------------------------------------------------------------------------- #
# exit decision
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExitPolicy:
    stop_loss_pct: Decimal
    max_drawdown_pct: Decimal
    oor_below_seconds: int
    oor_above_seconds: int
    exposure_exit_pct: Decimal

    @staticmethod
    def from_config() -> 'ExitPolicy':
        return ExitPolicy(
            stop_loss_pct=Decimal(str(cfg.STOP_LOSS_PCT)),
            max_drawdown_pct=Decimal(str(cfg.MAX_DRAWDOWN_PCT)),
            oor_below_seconds=int(cfg.OOR_BELOW_MAX_SECONDS),
            oor_above_seconds=int(cfg.OOR_ABOVE_MAX_SECONDS),
            exposure_exit_pct=Decimal(str(cfg.MAX_TOKEN_EXPOSURE_PCT)),
        )


def exit_decision(*, nav_usdg: Numeric, principal_usdg: Numeric | None,
                  peak_nav_usdg: Numeric | None, side: str,
                  exposure_pct: float, oor_elapsed_seconds: int,
                  policy: ExitPolicy) -> tuple[bool, str | None]:
    """Decide whether to close, returning ``(should_exit, reason)``.

    Ordered by urgency.  NAV drawdown fires before the range timers because a
    position can bleed badly while still nominally in range.
    """
    nav = Decimal(str(nav_usdg))
    principal = Decimal(str(principal_usdg or 0))
    peak = max(Decimal(str(peak_nav_usdg or 0)), nav, principal)

    if principal > 0 and nav <= principal * (1 - policy.stop_loss_pct / 100):
        return True, 'nav_stop_loss'
    if peak > 0 and nav <= peak * (1 - policy.max_drawdown_pct / 100):
        return True, 'nav_drawdown'
    # A fully token-heavy position has stopped being an LP and is now a naked bag.
    if Decimal(str(exposure_pct)) >= policy.exposure_exit_pct and side != ABOVE:
        return True, 'token_exposure'
    if side == BELOW and oor_elapsed_seconds >= policy.oor_below_seconds:
        return True, 'out_of_range_below'
    if side == ABOVE and oor_elapsed_seconds >= policy.oor_above_seconds:
        return True, 'out_of_range_above'
    return False, None


def dump_detected(history: Iterable[Row], now: float, *, window_seconds: int,
                  price_drop_pct: Numeric,
                  liquidity_drop_pct: Numeric) -> tuple[bool, str | None]:
    """Short-window crash detector for positions too young for the 1h baseline.

    ``history`` rows are ``{'t': epoch, 'price': str, 'usdg_pool': int}``.  The
    oldest sample inside the window is the baseline, so protection starts one
    poll after entry instead of one hour.
    """
    rows = sorted((r for r in history if now - float(r['t']) <= window_seconds), key=lambda r: float(r['t']))
    if len(rows) < 2:
        return False, None
    base, last = rows[0], rows[-1]
    if float(last['t']) - float(base['t']) <= 0:
        return False, None
    p0, p1 = Decimal(str(base['price'])), Decimal(str(last['price']))
    if p0 > 0 and (p1 - p0) / p0 * 100 <= -Decimal(str(price_drop_pct)):
        return True, 'fast_dump'
    l0, l1 = Decimal(str(base.get('usdg_pool', 0))), Decimal(str(last.get('usdg_pool', 0)))
    if l0 > 0 and (l0 - l1) / l0 * 100 >= Decimal(str(liquidity_drop_pct)):
        return True, 'fast_liquidity_drain'
    return False, None
