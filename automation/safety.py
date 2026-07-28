"""On-chain honeypot / hidden-tax detector via router-sim round-trip.

Uses eth_call state overrides to bypass balance/allowance requirements — we
fake wallet balance & allowance for both tokens, so the sim only reverts if
the TOKEN itself blocks the transfer (real honeypot / FoT).

Adopted from FlipZ3ro robinhood-lp-bot (src/watch/scanner.ts:safetyCheck),
translated to Python + adapted to RH Chain's SwapRouter (0xcaf681a…).
"""
import sys
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from web3 import Web3
from eth_abi import encode
from eth_utils import keccak
from config import (USDG, USDG_DECIMALS, V3_SWAP_ROUTER, WALLET_ADDRESS,
                    FEE_TIER_FALLBACKS)
from common import rpc_call


TEST_USDG_IN = int(Decimal('1') * (10 ** USDG_DECIMALS))  # 1 USDG probe

MAX256 = (1 << 256) - 1


def _slot_hex(slot: int) -> str:
    return hex(slot)[2:].rjust(64, '0')


def _map_slot(key: str, slot: int) -> str:
    """Compute storage slot for mapping[address] = value → keccak256(pad(key) . pad(slot))"""
    key_pad = key.lower().replace('0x', '').rjust(64, '0')
    slot_pad = _slot_hex(slot)
    return '0x' + keccak(bytes.fromhex(key_pad + slot_pad)).hex()


def _map2_slot(k1: str, k2: str, slot: int) -> str:
    """Compute storage slot for mapping[k1][k2] = value → nested keccak."""
    inner = _map_slot(k1, slot)
    # then keccak(pad(k2) . inner)
    k2_pad = k2.lower().replace('0x', '').rjust(64, '0')
    return '0x' + keccak(bytes.fromhex(k2_pad + inner[2:])).hex()


def _overrides_for(token: str):
    """Try common OZ ERC20 layout: balanceOf slot 0, allowance slot 1.
    Fallback: also set slots 51,52 (upgradable ERC20 UUPS layout offset).
    We fake balance and allowance for our wallet → router.
    """
    val = '0x' + _slot_hex(MAX256)  # max
    overrides = {}
    combined = {}
    # try slot 0 & 1 (standard)
    combined[_map_slot(WALLET_ADDRESS, 0)] = val
    combined[_map2_slot(WALLET_ADDRESS, V3_SWAP_ROUTER, 1)] = val
    # try slot 51 & 52 (OZ upgradable ~ ERC20Upgradeable)
    combined[_map_slot(WALLET_ADDRESS, 51)] = val
    combined[_map2_slot(WALLET_ADDRESS, V3_SWAP_ROUTER, 52)] = val
    # try slot 2 & 3 (some layouts)
    combined[_map_slot(WALLET_ADDRESS, 2)] = val
    combined[_map2_slot(WALLET_ADDRESS, V3_SWAP_ROUTER, 3)] = val
    overrides[Web3.to_checksum_address(token)] = {'stateDiff': combined}
    return overrides


def _sim_swap(token_in: str, token_out: str, fee: int, amount_in: int):
    data = '0x04e45aaf' + encode(
        ['(address,address,uint24,address,uint256,uint256,uint160)'],
        [(Web3.to_checksum_address(token_in),
          Web3.to_checksum_address(token_out),
          int(fee),
          Web3.to_checksum_address(WALLET_ADDRESS),
          int(amount_in), 0, 0)]
    ).hex()
    call = {
        'to': Web3.to_checksum_address(V3_SWAP_ROUTER),
        'from': Web3.to_checksum_address(WALLET_ADDRESS),
        'value': '0x0',
        'data': data,
        'gas': '0x400000',
    }
    overrides = _overrides_for(token_in)
    try:
        r = rpc_call('eth_call', [call, 'latest', overrides])
        return int(r, 16)
    except Exception:
        # retry without overrides (RPC may not support param3)
        try:
            r = rpc_call('eth_call', [call, 'latest'])
            return int(r, 16)
        except Exception:
            return None


def round_trip(token_addr: str, max_tax_pct: float = 6.0):
    best = None
    for fee in FEE_TIER_FALLBACKS:
        got = _sim_swap(USDG, token_addr, fee, TEST_USDG_IN)
        if not got:
            continue
        back = _sim_swap(token_addr, USDG, fee, got)
        if not back:
            continue
        back_pct = (back / TEST_USDG_IN) * 100
        expected = ((1 - fee / 1_000_000) ** 2) * 100
        tax_pct = expected - back_pct
        cand = {'fee': fee, 'back_pct': back_pct, 'tax_pct': tax_pct,
                'expected_pct': expected}
        if best is None or back_pct > best['back_pct']:
            best = cand

    if best is None:
        return {'ok': False, 'back_pct': 0.0, 'tax_pct': 100.0, 'fee': None,
                'reason': 'sim revert semua tier — kemungkinan honeypot / no-liq'}
    if best['tax_pct'] > max_tax_pct:
        return {**best, 'ok': False,
                'reason': f"hidden tax ~{best['tax_pct']:.1f}% (> cap {max_tax_pct}%)"}
    return {**best, 'ok': True,
            'reason': f"sehat (balik {best['back_pct']:.1f}%/expect {best['expected_pct']:.1f}% via fee {best['fee']/10_000:.2f}%)"}


if __name__ == '__main__':
    import json
    tok = sys.argv[1]
    print(json.dumps(round_trip(tok), indent=2))
