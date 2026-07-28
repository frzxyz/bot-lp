"""Typed position records: one schema, one constructor, one alias table.

Positions are persisted as plain JSON objects and are read by several modules that
each grew their own key names.  Three V4 creation sites emitted three different key
sets, and readers papered over the gaps with ``pos.get(k, fallback)`` chains, so a
missing field silently became a constant instead of an error.  That is how a
rotation ended up sizing itself from ``POSITION_SIZE_USDG`` rather than the real
position value.

This module fixes the shape at both ends while leaving storage as JSON:

* :meth:`V3Position.opened` / :meth:`V4Position.opened` are the only sanctioned
  ways to mint a record, so every record carries every field.
* :func:`principal_usdg` and friends are the only sanctioned ways to read money,
  and they raise :class:`MissingMoneyField` rather than substituting a default.
* Legacy aliases are resolved in :data:`_ALIASES` alone, not at each call site.
* ``from_dict``/``to_dict`` round-trip unknown keys through ``extra`` so records
  written by older code — and ad-hoc runtime state such as ``oor_since`` — survive
  untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

import config as cfg

__all__ = [
    'MissingMoneyField', 'V3Position', 'V4Position',
    'principal_usdg', 'peak_capital_usdg', 'money', 'normalize',
]


class MissingMoneyField(KeyError):
    """A money field the caller declared mandatory is absent.

    Deliberately fatal: every silent default in a money path is a wrong number
    that spends real funds.
    """


_MISSING = object()

#: Historical spellings for each canonical field.  V3 records were written with
#: ``size_usdg`` while V4 used ``entry_value_usd``; a stray ``entry_value_usdg``
#: also exists in one read path.  All of them mean "capital committed".
_ALIASES: dict[str, tuple[str, ...]] = {
    'principal_usdg': ('principal_usdg', 'size_usdg', 'entry_value_usd', 'entry_value_usdg'),
    'peak_capital_usdg': ('peak_capital_usdg', 'peak_nav_usdg'),
    'compounded_capital_usdg': ('compounded_capital_usdg', 'principal_usdg', 'entry_value_usd'),
    'token_id': ('token_id', 'tokenId'),
    'pool_id': ('poolId', 'pool_id'),
}


def _lookup(record: Mapping[str, Any], canonical: str) -> Any:
    """First present alias for ``canonical``, or ``_MISSING``.

    ``None`` counts as absent: the JSON writers emit ``None`` for fields they had
    no value for, which must not be mistaken for a real zero.
    """
    for key in _ALIASES.get(canonical, (canonical,)):
        value = record.get(key)
        if value is not None:
            return value
    return _MISSING


def money(record: Mapping[str, Any], canonical: str, *,
          default: Decimal | None = None) -> Decimal:
    """Read a money field as :class:`Decimal`, resolving legacy aliases.

    Passing ``default`` is an explicit statement that absence is legitimate.
    Omitting it means absence is a bug, and raises.
    """
    value = _lookup(record, canonical)
    if value is _MISSING:
        if default is not None:
            return default
        raise MissingMoneyField(
            f'{canonical!r} absent from position record '
            f'(tried {"/".join(_ALIASES.get(canonical, (canonical,)))}); '
            f'refusing to substitute a default in a money path')
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MissingMoneyField(f'{canonical!r} is not a number: {value!r}') from exc


def principal_usdg(record: Mapping[str, Any], *, default: Decimal | None = None) -> Decimal:
    """Capital committed to the position, in whole USDG.

    Spans both versions: V3 stores it as ``size_usdg``, V4 as ``principal_usdg``.
    """
    return money(record, 'principal_usdg', default=default)


def peak_capital_usdg(record: Mapping[str, Any], *, default: Decimal | None = None) -> Decimal:
    """High-water NAV, used as the trailing-stop reference."""
    return money(record, 'peak_capital_usdg', default=default)


def _dec(value: Any, fallback: str = '0') -> Decimal:
    try:
        return Decimal(str(fallback if value is None else value))
    except (InvalidOperation, ValueError):
        return Decimal(fallback)


def _int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


@dataclass
class _Record:
    """Shared JSON mapping behaviour for the position dataclasses."""

    #: Keys consumed by declared fields; anything else round-trips via ``extra``.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _declared(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls) if f.name != 'extra')

    @classmethod
    def _consumed(cls) -> set[str]:
        """Declared names plus every alias they may have arrived under."""
        names = set(cls._declared())
        for canonical in list(names):
            names.update(_ALIASES.get(canonical, ()))
        return names

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk shape, unknown keys included.

        ``Decimal`` is emitted as a string to match what the existing writers
        produce and to keep full precision through JSON.
        """
        out: dict[str, Any] = dict(self.extra)
        for name in self._declared():
            value = getattr(self, name)
            out[name] = str(value) if isinstance(value, Decimal) else value
        return out


@dataclass
class V3Position(_Record):
    """A Uniswap V3 LP position owned by this bot."""

    token: str = ''
    symbol: str = '?'
    decimals: int = 18
    pool: str = ''
    fee: int = 0
    token_id: int = 0
    tick_lower: int = 0
    tick_upper: int = 0
    entry_tick: int = 0
    entry_price_usdg: Decimal = Decimal(0)
    size_usdg: Decimal = Decimal(0)
    usdg_deposited: int = 0
    tok_deposited: int = 0
    mint_tx: str = ''
    mint_time: int = 0
    atomic: bool = True
    range_width_percent: float | None = None
    peak_nav_usdg: Decimal | None = None
    total_fees_collected_usdg: Decimal = Decimal(0)
    last_action_time: int = 0

    @classmethod
    def opened(cls, *, token: str, symbol: str, decimals: int, pool: str, fee: int,
               token_id: int, tick_lower: int, tick_upper: int, entry_tick: int,
               entry_price_usdg: Decimal, size_usdg: Decimal, usdg_deposited: int,
               tok_deposited: int, mint_tx: str, mint_time: int,
               range_width_percent: float | None = None, atomic: bool = True) -> 'V3Position':
        """Mint a complete record.  Seeds ``peak_nav_usdg`` so the trailing stop has
        a reference from the first management tick rather than after one poll."""
        return cls(
            token=token, symbol=symbol, decimals=int(decimals), pool=pool, fee=int(fee),
            token_id=int(token_id), tick_lower=int(tick_lower), tick_upper=int(tick_upper),
            entry_tick=int(entry_tick), entry_price_usdg=_dec(entry_price_usdg),
            size_usdg=_dec(size_usdg), usdg_deposited=int(usdg_deposited),
            tok_deposited=int(tok_deposited), mint_tx=mint_tx, mint_time=int(mint_time),
            atomic=atomic, range_width_percent=range_width_percent,
            peak_nav_usdg=_dec(size_usdg), last_action_time=int(mint_time))

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> 'V3Position':
        consumed = cls._consumed()
        return cls(
            token=str(record.get('token', '')),
            symbol=str(record.get('symbol', '?')),
            decimals=_int(record.get('decimals'), 18),
            pool=str(record.get('pool', '')),
            fee=_int(record.get('fee')),
            token_id=_int(_lookup(record, 'token_id') if _lookup(record, 'token_id') is not _MISSING else 0),
            tick_lower=_int(record.get('tick_lower')),
            tick_upper=_int(record.get('tick_upper')),
            entry_tick=_int(record.get('entry_tick')),
            entry_price_usdg=_dec(record.get('entry_price_usdg')),
            size_usdg=principal_usdg(record, default=Decimal(0)),
            usdg_deposited=_int(record.get('usdg_deposited')),
            tok_deposited=_int(record.get('tok_deposited')),
            mint_tx=str(record.get('mint_tx', '')),
            mint_time=_int(record.get('mint_time')),
            atomic=bool(record.get('atomic', True)),
            range_width_percent=record.get('range_width_percent'),
            peak_nav_usdg=None if record.get('peak_nav_usdg') is None else _dec(record.get('peak_nav_usdg')),
            total_fees_collected_usdg=_dec(record.get('total_fees_collected_usdg')),
            last_action_time=_int(record.get('last_action_time')),
            extra={k: v for k, v in record.items() if k not in consumed})


@dataclass
class V4Position(_Record):
    """A Uniswap V4 LP position owned by this bot.

    ``entry_value_usd`` is written alongside ``principal_usdg`` because live
    readers still use the old name; both always carry the same value.
    """

    version: str = 'v4'
    token: str = ''
    symbol: str = '?'
    token_id: str = ''
    poolId: str | None = None
    tick_lower: int | None = None
    tick_upper: int | None = None
    mint_tx: str | None = None
    swap_tx: str | None = None
    mint_time: int = 0
    principal_usdg: Decimal = Decimal(0)
    compounded_capital_usdg: Decimal = Decimal(0)
    peak_capital_usdg: Decimal = Decimal(0)
    last_realized_usdg: Decimal = Decimal(0)
    cumulative_realized_profit_usdg: Decimal = Decimal(0)
    isolated_strategy_reserve_usdg: Decimal = Decimal(0)
    last_fee_usd: Decimal = Decimal(0)
    fee_baseline_ts: int = 0
    rebalance_count: int = 0
    range_width_percent: float | None = None
    range_mode: str = 'normal'
    rebalanced_from_token_id: str | None = None
    rebalanced_at: int | None = None
    rebalance_cooldown_until: int = 0
    rotation_source_version: str | None = None
    rotation_source_token: str | None = None
    rotation_source_token_id: str | None = None
    exact_close_proceeds_raw: str | None = None

    @classmethod
    def opened(cls, *, token: str, symbol: str, token_id: str, mint_time: int,
               amount_usdg: Decimal, poolId: str | None = None,
               tick_lower: int | None = None, tick_upper: int | None = None,
               mint_tx: str | None = None, swap_tx: str | None = None,
               principal_usdg: Decimal | None = None,
               peak_capital_usdg: Decimal | None = None,
               last_realized_usdg: Decimal | None = None,
               cumulative_realized_profit_usdg: Decimal | None = None,
               isolated_strategy_reserve_usdg: Decimal | None = None,
               range_width_percent: float | None = None, range_mode: str = 'normal',
               rebalance_count: int = 0, rebalanced_from_token_id: str | None = None,
               rebalanced_at: int | None = None, rebalance_cooldown_until: int = 0,
               rotation_source_version: str | None = None,
               rotation_source_token: str | None = None,
               rotation_source_token_id: str | None = None,
               exact_close_proceeds_raw: str | None = None) -> 'V4Position':
        """Mint a complete record regardless of which entry path opened it.

        The three historical call sites each omitted a different subset of fields;
        defaulting here means a reader can no longer meet an absent key.
        """
        amount = _dec(amount_usdg)
        return cls(
            token=token, symbol=symbol, token_id=str(token_id), poolId=poolId,
            tick_lower=tick_lower, tick_upper=tick_upper, mint_tx=mint_tx, swap_tx=swap_tx,
            mint_time=int(mint_time),
            principal_usdg=amount if principal_usdg is None else _dec(principal_usdg),
            compounded_capital_usdg=amount,
            peak_capital_usdg=amount if peak_capital_usdg is None else _dec(peak_capital_usdg),
            last_realized_usdg=_dec(last_realized_usdg),
            cumulative_realized_profit_usdg=_dec(cumulative_realized_profit_usdg),
            isolated_strategy_reserve_usdg=_dec(isolated_strategy_reserve_usdg),
            last_fee_usd=Decimal(0), fee_baseline_ts=int(mint_time),
            rebalance_count=int(rebalance_count), range_width_percent=range_width_percent,
            range_mode=range_mode, rebalanced_from_token_id=rebalanced_from_token_id,
            rebalanced_at=rebalanced_at, rebalance_cooldown_until=int(rebalance_cooldown_until),
            rotation_source_version=rotation_source_version,
            rotation_source_token=rotation_source_token,
            rotation_source_token_id=rotation_source_token_id,
            exact_close_proceeds_raw=exact_close_proceeds_raw)

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> 'V4Position':
        consumed = cls._consumed()
        principal = principal_usdg(record, default=Decimal(0))
        return cls(
            version=str(record.get('version', 'v4')),
            token=str(record.get('token', '')),
            symbol=str(record.get('symbol', '?')),
            token_id=str(record.get('token_id', '')),
            poolId=record.get('poolId'),
            tick_lower=record.get('tick_lower'),
            tick_upper=record.get('tick_upper'),
            mint_tx=record.get('mint_tx'),
            swap_tx=record.get('swap_tx'),
            mint_time=_int(record.get('mint_time')),
            principal_usdg=principal,
            compounded_capital_usdg=money(record, 'compounded_capital_usdg', default=principal),
            peak_capital_usdg=money(record, 'peak_capital_usdg', default=principal),
            last_realized_usdg=_dec(record.get('last_realized_usdg')),
            cumulative_realized_profit_usdg=_dec(record.get('cumulative_realized_profit_usdg')),
            isolated_strategy_reserve_usdg=_dec(record.get('isolated_strategy_reserve_usdg')),
            last_fee_usd=_dec(record.get('last_fee_usd')),
            fee_baseline_ts=_int(record.get('fee_baseline_ts')),
            rebalance_count=_int(record.get('rebalance_count')),
            range_width_percent=record.get('range_width_percent'),
            range_mode=str(record.get('range_mode', 'normal')),
            rebalanced_from_token_id=record.get('rebalanced_from_token_id'),
            rebalanced_at=record.get('rebalanced_at'),
            rebalance_cooldown_until=_int(record.get('rebalance_cooldown_until')),
            rotation_source_version=record.get('rotation_source_version'),
            rotation_source_token=record.get('rotation_source_token'),
            rotation_source_token_id=record.get('rotation_source_token_id'),
            exact_close_proceeds_raw=record.get('exact_close_proceeds_raw'),
            extra={k: v for k, v in record.items() if k not in consumed})

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        # Keep the historical name populated for readers not yet migrated.
        out['entry_value_usd'] = str(self.principal_usdg)
        return out


def normalize(record: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade a record of either version to the current canonical shape.

    Round-trips through the dataclass so missing fields gain deterministic values
    and unknown keys are preserved.  Safe to apply to live state on load.
    """
    cls = V4Position if str(record.get('version', '')).lower() == 'v4' else V3Position
    return cls.from_dict(record).to_dict()
