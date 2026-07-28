"""Simulated chain for end-to-end lifecycle tests.

The unit suites exercise modules in isolation, which is exactly how a lifecycle
can pass every test and still break at the seams: a constructor that omits a
field, a reader that expects it, an executor contract that changed shape. This
harness fakes only the outermost I/O — the JSON-RPC helpers in ``common`` and the
subprocess bridges to the executor CLIs — so everything above them (entry,
manager, hybrid_v4, liquidation, lifecycle journalling, emergency) runs for real.

State files are redirected into a temporary directory. Nothing here touches a
network or the production state directory.
"""
from __future__ import annotations

import contextlib
import math
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import common as c
import config as cfg

Q96 = 2 ** 96
USDG_LO = cfg.USDG.lower()


def sqrt_price_x96(price: float) -> int:
    """Encode a raw token1-per-token0 price the way slot0 reports it."""
    return int(math.sqrt(price) * Q96)


def tick_of(price: float) -> int:
    return int(math.log(price) / math.log(1.0001))


class FakeChain:
    """Mutable chain state: balances, one V3 pool, and NFT positions."""

    def __init__(self, *, token='0x00000000000000000000000000000000000000ff',
                 token_decimals=18, usdg_raw=10_000_000, eth_wei=10 ** 17,
                 price=0.001, fee=10000, symbol='MEME', depth_usdg=500_000):
        self.token = token
        self.symbol = symbol
        self.token_decimals = token_decimals
        self.fee = fee
        self.pool = '0x00000000000000000000000000000000000000p0'.replace('p', 'a')
        # USDG sorts below the token address here, so USDG is token0 and the pool
        # quotes token-per-USDG. That inversion is the one risk.py must undo.
        self.token_is_token0 = self.token.lower() < USDG_LO
        self.balances = {cfg.USDG.lower(): usdg_raw, token.lower(): 0}
        self.eth = eth_wei
        self.price = price          # USDG per whole token
        self.depth_usdg = depth_usdg  # one-sided pool depth, drives price impact
        self.nfts: dict[int, dict] = {}
        self.next_id = 1000
        self.txs: list[dict] = []
        self.gas_price_wei = 40_000_000
        self.reverts: set[str] = set()   # command names that should fail
        self.mint_receipts: dict[str, int] = {}  # legacy mint tx hash -> token id

    # ---------------------------------------------------------------- helpers
    def raw_price(self) -> float:
        """token1-per-token0 in raw units, matching slot0 semantics."""
        scale = 10 ** (cfg.USDG_DECIMALS - self.token_decimals)
        if self.token_is_token0:
            return self.price * scale
        return (1.0 / self.price) / scale

    def tick(self) -> int:
        return tick_of(self.raw_price())

    def bal(self, token) -> int:
        return self.balances.get(str(token).lower(), 0)

    def credit(self, token, raw):
        k = str(token).lower()
        self.balances[k] = self.balances.get(k, 0) + int(raw)

    def move_price(self, factor: float):
        self.price *= factor

    # --------------------------------------------------------------- patched
    def erc20_balance(self, token, addr=None): return self.bal(token)
    def eth_balance(self, addr=None): return self.eth
    def erc20_decimals(self, token):
        return cfg.USDG_DECIMALS if str(token).lower() == USDG_LO else self.token_decimals
    def erc20_symbol(self, token):
        return 'USDG' if str(token).lower() == USDG_LO else self.symbol
    def erc20_allowance(self, token, owner, spender): return 2 ** 255
    def get_gas_price(self): return self.gas_price_wei
    def check_pending_nonce(self): return None
    def check_kill(self, quiet=False): return False
    def get_pool(self, a, b, fee): return self.pool if int(fee) == self.fee else None
    def pool_slot0(self, pool): return (sqrt_price_x96(self.raw_price()), self.tick(), 0, 0, 0, 0, True)
    def pool_token0(self, pool): return self.token if self.token_is_token0 else cfg.USDG
    def pool_token1(self, pool): return cfg.USDG if self.token_is_token0 else self.token
    def pool_liquidity(self, pool): return 10 ** 18
    def pool_tick_spacing(self, pool): return 200
    def pool_fee(self, pool): return self.fee
    def approve_if_needed(self, token, spender, amount): return None

    def quote_v3_exact_input_single(self, token_in, token_out, fee, amount_in):
        """Constant-product exact-input quote, so size carries real price impact.

        ``depth_usdg`` sets how deep the pool is; a thin memecoin pool makes a full
        exit move the price against itself, which is what forces the liquidation
        queue to step its size down.
        """
        amount_in = int(amount_in)
        if amount_in <= 0:
            return 0
        reserve_usdg = Decimal(str(self.depth_usdg)) * Decimal(10 ** cfg.USDG_DECIMALS)
        reserve_token = reserve_usdg / Decimal(str(self.price)) / Decimal(10 ** cfg.USDG_DECIMALS) \
            * Decimal(10 ** self.token_decimals)
        if str(token_in).lower() == USDG_LO:
            reserve_in, reserve_out = reserve_usdg, reserve_token
        else:
            reserve_in, reserve_out = reserve_token, reserve_usdg
        net_in = Decimal(amount_in) * (Decimal(1) - Decimal(fee) / Decimal(1_000_000))
        return int(reserve_out * net_in / (reserve_in + net_in))

    def round_trip(self, token_addr, max_tax_pct: float = 6.0):
        """Honeypot probe: buy then sell back through the simulated pool."""
        if 'honeypot' in self.reverts:
            return {'ok': False, 'back_pct': 0.0, 'tax_pct': 100.0, 'fee': None,
                    'reason': 'sim revert semua tier — kemungkinan honeypot / no-liq'}
        probe = 10 ** cfg.USDG_DECIMALS
        got = self.quote_v3_exact_input_single(cfg.USDG, token_addr, self.fee, probe)
        back = self.quote_v3_exact_input_single(token_addr, cfg.USDG, self.fee, got)
        back_pct = back / probe * 100
        expected = ((1 - self.fee / 1_000_000) ** 2) * 100
        return {'ok': True, 'fee': self.fee, 'back_pct': back_pct,
                'tax_pct': expected - back_pct, 'expected_pct': expected,
                'reason': f'sehat (balik {back_pct:.1f}%)'}

    def kyber_reverse_preflight(self, token, amount_token_raw, quote_only=False):
        """Stand-in for the Kyber sidecar's reverse (token -> USDG) quote."""
        if 'kyber' in self.reverts:
            raise RuntimeError('simulated kyber sidecar outage')
        out = self.quote_v3_exact_input_single(token, cfg.USDG, self.fee, amount_token_raw)
        return {'quotedOutRaw': out, 'reverseRaw': out}

    def build_and_send(self, tx, gas_limit=None):
        """Apply the transaction's real effect by decoding its calldata.

        Decoding rather than stubbing the callers means the ABI encoding in
        manager/entry is exercised too: a wrong selector or argument order shows up
        here as an unmoved balance instead of passing silently.
        """
        from eth_abi import decode as abi_decode

        self.txs.append(dict(tx))
        data = str(tx.get('data') or '')
        selector, body = data[:10], bytes.fromhex(data[10:]) if len(data) > 10 else b''

        if selector == '0x04e45aaf':          # exactInputSingle
            token_in, token_out, fee, _to, amount_in, min_out, _ = abi_decode(
                ['(address,address,uint24,address,uint256,uint256,uint160)'], body)[0]
            out = self.quote_v3_exact_input_single(token_in, token_out, fee, amount_in)
            if out < int(min_out):
                raise RuntimeError('simulated slippage revert')
            self.credit(token_in, -int(amount_in)); self.credit(token_out, out)
        elif selector == '0xfc6f7865':        # collect((uint256,address,uint128,uint128))
            token_id, _recipient, _m0, _m1 = abi_decode(
                ['(uint256,address,uint128,uint128)'], body)[0]
            info = self.nfts.get(int(token_id))
            if info:
                t0_is_usdg = str(info['token0']).lower() == USDG_LO
                self.credit(cfg.USDG, info['owed0'] if t0_is_usdg else info['owed1'])
                self.credit(self.token, info['owed1'] if t0_is_usdg else info['owed0'])
                info['owed0'] = info['owed1'] = 0
        elif selector == '0x0c49ccbe':        # decreaseLiquidity
            token_id, liq, _a0, _a1, _dl = abi_decode(
                ['(uint256,uint128,uint256,uint256,uint256)'], body)[0]
            info = self.nfts.get(int(token_id))
            if info:
                share = min(1.0, int(liq) / max(1, info['liquidity']))
                info['liquidity'] -= int(liq)
                t0_is_usdg = str(info['token0']).lower() == USDG_LO
                usdg_out = int(info['usdg_raw'] * share); tok_out = int(info['token_raw'] * share)
                info['usdg_raw'] -= usdg_out; info['token_raw'] -= tok_out
                # decreaseLiquidity credits the NFT, not the wallet; collect moves it.
                info['owed0'] += usdg_out if t0_is_usdg else tok_out
                info['owed1'] += tok_out if t0_is_usdg else usdg_out
        elif selector == '0x42966c68':        # burn(uint256)
            self.nfts.pop(int(body[:32].hex() or '0', 16), None)
        elif selector == '0x88316456':        # NPM.mint(...) — the legacy, non-atomic path
            (t0, t1, fee, lower, upper, a0, a1, _m0, _m1, _to, _dl) = abi_decode(
                ['(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)'],
                body)[0]
            t0_is_usdg = str(t0).lower() == USDG_LO
            usdg_raw, token_raw = (a0, a1) if t0_is_usdg else (a1, a0)
            self.credit(cfg.USDG, -int(usdg_raw)); self.credit(self.token, -int(token_raw))
            tid = self.mint(usdg_raw=usdg_raw, token_raw=token_raw, width_pct=20)
            self.nfts[tid].update(tick_lower=int(lower), tick_upper=int(upper))
            receipt_hash = f'0x{len(self.txs):064x}'
            self.mint_receipts[receipt_hash] = tid
            return {'hash': receipt_hash, 'status': 1, 'gasUsed': 300000}

        return {'hash': f'0x{len(self.txs):064x}', 'status': 1, 'gasUsed': 120000}

    def rpc_call(self, method, params, attempts=3):
        """Only the receipt lookup used by the legacy mint-log parser is modelled."""
        if method != 'eth_getTransactionReceipt':
            raise RuntimeError(f'unexpected rpc method in simulation: {method}')
        from eth_utils import keccak
        tid = self.mint_receipts.get(str(params[0]))
        if tid is None:
            return {'logs': []}
        transfer = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
        return {'logs': [{
            'address': cfg.V3_POSITION_MANAGER,
            'topics': [transfer, '0x' + '0' * 64,
                       '0x' + cfg.WALLET_ADDRESS.lower().replace('0x', '').rjust(64, '0'),
                       f'0x{tid:064x}'],
        }]}

    # ------------------------------------------------------- NFT bookkeeping
    def mint(self, *, usdg_raw, token_raw, width_pct) -> int:
        """Mint with a liquidity derived from the deposits, in pool-native terms.

        Liquidity is computed here from the canonical Uniswap formulas in the pool's
        own token0/token1 orientation, deliberately without reusing risk.py. A
        fixture built from the code under test could not detect an error in it.
        """
        tid = self.next_id
        self.next_id += 1
        half = math.log1p(float(width_pct) / 100) / math.log(1.0001)
        spacing = 200
        centre = self.tick()
        lower = int((centre - half) // spacing) * spacing
        upper = int((centre + half) // spacing + 1) * spacing

        usdg_is_t0 = str(self.pool_token0(None)).lower() == USDG_LO
        amount0 = usdg_raw if usdg_is_t0 else token_raw
        amount1 = token_raw if usdg_is_t0 else usdg_raw
        sp = math.sqrt(self.raw_price())
        sa, sb = math.sqrt(1.0001 ** lower), math.sqrt(1.0001 ** upper)
        sp = min(max(sp, sa), sb)
        l0 = amount0 / (1.0 / sp - 1.0 / sb) if sp < sb else float('inf')
        l1 = amount1 / (sp - sa) if sp > sa else float('inf')
        liquidity = int(min(l0, l1))

        self.nfts[tid] = {
            'token0': self.pool_token0(None), 'token1': self.pool_token1(None), 'fee': self.fee,
            'tick_lower': lower, 'tick_upper': upper,
            'liquidity': liquidity, 'owed0': 0, 'owed1': 0,
            'usdg_raw': int(usdg_raw), 'token_raw': int(token_raw),
        }
        return tid

    def true_nav_usdg(self, token_id) -> Decimal:
        """Independent NAV, for asserting against risk.nav_usdg."""
        info = self.nfts[int(token_id)]
        usdg_is_t0 = str(info['token0']).lower() == USDG_LO
        sp = math.sqrt(self.raw_price())
        sa, sb = math.sqrt(1.0001 ** info['tick_lower']), math.sqrt(1.0001 ** info['tick_upper'])
        sp = min(max(sp, sa), sb)
        liquidity = float(info['liquidity'])
        amount0 = liquidity * (1.0 / sp - 1.0 / sb)
        amount1 = liquidity * (sp - sa)
        usdg_raw, token_raw = (amount0, amount1) if usdg_is_t0 else (amount1, amount0)
        return (Decimal(usdg_raw) / Decimal(10 ** cfg.USDG_DECIMALS)
                + Decimal(token_raw) / Decimal(10 ** self.token_decimals) * Decimal(str(self.price)))

    def position_info(self, token_id):
        info = self.nfts.get(int(token_id))
        if info is None:
            raise RuntimeError(f'nonexistent NFT {token_id}')
        return {k: info[k] for k in
                ('token0', 'token1', 'fee', 'tick_lower', 'tick_upper', 'liquidity', 'owed0', 'owed1')}

    def simulated_collectable(self, token_id):
        info = self.nfts[int(token_id)]
        return (info['owed0'], info['owed1'])

    def accrue_fees(self, token_id, *, usdg_raw=0, token_raw=0):
        info = self.nfts[int(token_id)]
        if str(info['token0']).lower() == USDG_LO:
            info['owed0'] += int(usdg_raw); info['owed1'] += int(token_raw)
        else:
            info['owed1'] += int(usdg_raw); info['owed0'] += int(token_raw)

    # --------------------------------------------- executor CLI (atomic v3)
    def atomic_v3_run(self, command, *args, width_pct=None):
        if command in self.reverts:
            raise RuntimeError(f'simulated executor failure: {command}')
        if command in ('build_open', 'build_close'):
            return {'ok': True}
        if command == 'execute_open':
            token, amount_raw = args[0], int(args[1])
            # Executor swaps ~half to token, then mints; wallet pays the USDG.
            swap_in = amount_raw // 2
            token_out = self.quote_v3_exact_input_single(cfg.USDG, token, self.fee, swap_in)
            self.credit(cfg.USDG, -amount_raw)
            tid = self.mint(usdg_raw=amount_raw - swap_in, token_raw=token_out,
                            width_pct=width_pct if width_pct is not None else 20)
            info = self.nfts[tid]
            return {'receipt': {'hash': f'0xopen{tid:060x}', 'status': 1},
                    'event': {'name': 'Opened', 'tokenId': tid},
                    'params': {'expectedPool': self.pool, 'fee': self.fee,
                               'tickLower': info['tick_lower'], 'tickUpper': info['tick_upper']},
                    'quote': {'tokenOut': token_out}}
        if command == 'execute_close':
            tid = int(args[0])
            info = self.nfts.pop(tid)
            # Principal + fees come back; the token side lands in the wallet and is
            # liquidated in stage two, exactly as the real executor behaves.
            usdg_back = info['usdg_raw'] + (info['owed0'] if str(info['token0']).lower() == USDG_LO else info['owed1'])
            token_back = info['token_raw'] + (info['owed1'] if str(info['token0']).lower() == USDG_LO else info['owed0'])
            self.credit(cfg.USDG, usdg_back)
            self.credit(self.token, token_back)
            return {'receipt': {'hash': f'0xclose{tid:059x}', 'status': 1},
                    'event': {'name': 'Closed', 'tokenId': tid, 'token': self.token,
                              'tokenAmount': token_back, 'settlementAmount': usdg_back},
                    'nftGone': True, 'removeCollectConfirmed': True,
                    'executorBalances': {'usdg': 0, 'token': 0}}
        if command == 'execute_liquidation':
            token, amount_raw = args[0], int(args[1])
            out = self.quote_v3_exact_input_single(token, cfg.USDG, self.fee, amount_raw)
            self.credit(token, -amount_raw); self.credit(cfg.USDG, out)
            return {'hash': f'0xliq{len(self.txs):061x}', 'status': 1}
        raise AssertionError(f'unexpected atomic v3 command {command!r}')


@contextlib.contextmanager
def simulated_chain(chain: FakeChain, tmp: Path):
    """Patch the I/O boundary and redirect every state file into ``tmp``."""
    import atomic_v3_backend
    import emergency
    import lifecycle
    import manager

    tmp.mkdir(parents=True, exist_ok=True)
    paths = {
        'STATE_DIR': tmp, 'POSITIONS_FILE': tmp / 'positions.json',
        'V4_POSITIONS_FILE': tmp / 'v4_positions.json', 'COOLDOWN_FILE': tmp / 'cooldown.json',
        'FEE_HISTORY_FILE': tmp / 'fee_history.json', 'CANDIDATES_FILE': tmp / 'candidates.json',
        'PENDING_LIQUIDATIONS_FILE': tmp / 'pending_liquidations.json',
        'ROTATION_REQUEST_FILE': tmp / 'rotation_request.json',
        'ROTATION_READY_FILE': tmp / 'rotation_ready.json',
        'V3_TO_V4_PENDING_FILE': tmp / 'v3_to_v4_pending.json',
        'V3_TO_V4_ARCHIVE_FILE': tmp / 'v3_to_v4_archive.json',
        'V4_CLOSE_WAL_FILE': tmp / 'v4_close_wal.json',
        'V4_REBALANCE_PENDING_FILE': tmp / 'v4_rebalance.json',
        'V4_REENTRY_PENDING_FILE': tmp / 'v4_reentry.json',
        'V4_MARKET_HISTORY_FILE': tmp / 'v4_history.json',
        'V4_DECISION_LOG_FILE': tmp / 'v4_decisions.jsonl',
        'V4_LIFECYCLE_MARKER': tmp / 'v4_lifecycle_pass.json',
        'LIFECYCLE_OPS_FILE': tmp / 'lifecycle_ops.json',
        'WALLET_LOCK_FILE': tmp / 'wallet.lock', 'KILL_SWITCH': tmp / 'halt',
        'HARD_HALT_RECOVERY': tmp / 'HARD_HALT_RECOVERY',
    }
    with contextlib.ExitStack() as stack:
        for name, value in paths.items():
            stack.enter_context(patch.object(cfg, name, value))
        # Captured at import time, so they need patching on their own module.
        stack.enter_context(patch.object(lifecycle, 'JOURNAL', tmp / 'lifecycle_ops.json'))
        stack.enter_context(patch.object(lifecycle, 'LOCK', tmp / 'lifecycle_ops.lock'))
        stack.enter_context(patch.object(lifecycle, 'HARD_HALT', tmp / 'HARD_HALT_RECOVERY'))
        stack.enter_context(patch.object(lifecycle, 'DAEMON_LOCK', tmp / 'failsafe.lock'))
        stack.enter_context(patch.object(emergency, 'HISTORY', tmp / 'emergency_history.json'))
        stack.enter_context(patch.object(c, 'WALLET_LOCK', tmp / 'wallet.lock'))

        for name in ('erc20_balance', 'eth_balance', 'erc20_decimals', 'erc20_symbol',
                     'erc20_allowance', 'get_gas_price', 'check_pending_nonce', 'check_kill',
                     'get_pool', 'pool_slot0', 'pool_token0', 'pool_token1', 'pool_liquidity',
                     'pool_tick_spacing', 'pool_fee', 'approve_if_needed', 'build_and_send',
                     'quote_v3_exact_input_single', 'rpc_call'):
            stack.enter_context(patch.object(c, name, getattr(chain, name)))
        # Raw eth_call/ABI decoding is the one layer worth faking wholesale.
        stack.enter_context(patch.object(manager, 'position_info', chain.position_info))
        stack.enter_context(patch.object(manager, 'simulated_collectable', chain.simulated_collectable))
        stack.enter_context(patch.object(atomic_v3_backend, '_run', chain.atomic_v3_run))

        # Liquidation routing: Kyber is the sidecar quote, the on-chain venues are
        # the fallbacks. quote_exit/quote_path/quote_v2 return nothing by default so
        # a test can prove the v3 fallback carries the settlement on its own.
        # The honeypot probe uses eth_call state overrides, which the fake RPC does
        # not model. It is re-exported by name into its callers, so each binding is
        # patched rather than only the defining module.
        import entry_trigger
        import rotation
        import safety
        stack.enter_context(patch.object(safety, 'round_trip', chain.round_trip))
        stack.enter_context(patch.object(entry_trigger, 'round_trip', chain.round_trip))
        stack.enter_context(patch.object(rotation, 'round_trip', chain.round_trip, create=True))

        import v4_backend
        stack.enter_context(patch.object(v4_backend, 'reverse_preflight', chain.kyber_reverse_preflight))
        stack.enter_context(patch.object(v4_backend, 'quote_exit', lambda *a, **k: []))
        stack.enter_context(patch.object(v4_backend, 'quote_path', lambda *a, **k: []))
        stack.enter_context(patch.object(v4_backend, 'quote_v2', lambda *a, **k: []))
        yield chain
