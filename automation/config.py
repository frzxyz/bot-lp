"""Config for Robinhood Chain Meme LP bot (Uniswap V3, USDG-quoted).
Wallet: 0x3582605Edebf376b684a45E8Faa6D808C22a8e3e (meme-lp-agent)
"""
import os
from pathlib import Path
from decimal import Decimal

# Chain
CHAIN_ID = 4663
RPC_URLS = [
    'https://rpc.mainnet.chain.robinhood.com',
]

# Uniswap V3 (Robinhood Chain)
V3_FACTORY = '0x1f7d7550b1b028f7571e69a784071f0205fd2efa'
V3_POSITION_MANAGER = '0x73991a25c818bf1f1128deaab1492d45638de0d3'
V3_SWAP_ROUTER = '0xcaf681a66d020601342297493863e78c959e5cb2'  # SwapRouter (v1 exactInputSingle w/ deadline)
V2_FACTORY = '0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f'  # Verified V2 factory; never use as a swap router
V3_QUOTER = '0x89e5db8b5aa49aa85ac63f691524311aeb649eba'  # QuoterV2 (may revert for hooked tokens)

# Tokens
WETH = '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
USDG = '0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168'  # decimals 6
USDG_DECIMALS = 6
WETH_DECIMALS = 18

# USDG/WETH swap pools (for routing when meme paired w/ WETH)
USDG_WETH_POOL_500 = '0x69BfaF19C9f377BB306a89aEd9F6B07e2c1a8d9a'  # 0.05% - primary swap venue

# Wallet
WALLET_DIR = Path('/root/.hermes/wallets/meme-lp-agent')
WALLET_ADDRESS = '0x3582605Edebf376b684a45E8Faa6D808C22a8e3e'

# State
STATE_DIR = Path('/root/.hermes/state/rh_meme_lp')
STATE_DIR.mkdir(parents=True, exist_ok=True)
CANDIDATES_FILE = STATE_DIR / 'candidates.json'
POSITIONS_FILE = STATE_DIR / 'positions.json'
V4_POSITIONS_FILE = STATE_DIR / 'v4_positions.json'
V4_REBALANCE_PENDING_FILE = STATE_DIR / 'v4_rebalance_pending.json'
V4_REENTRY_PENDING_FILE = STATE_DIR / 'v4_reentry_pending.json'
V4_MARKET_HISTORY_FILE = STATE_DIR / 'v4_market_history.json'
V4_DECISION_LOG_FILE = STATE_DIR / 'v4_decisions.jsonl'
WALLET_LOCK_FILE = STATE_DIR / 'wallet.lock'
V4_LIFECYCLE_MARKER = STATE_DIR / 'v4_lifecycle_pass.json'
COOLDOWN_FILE = STATE_DIR / 'cooldown.json'
KILL_SWITCH = Path('/root/.hermes/state/rh_meme_lp_halt')
FEE_HISTORY_FILE = STATE_DIR / 'fee_history.json'
ROTATION_REQUEST_FILE = STATE_DIR / 'rotation_request.json'
ROTATION_READY_FILE = STATE_DIR / 'rotation_ready.json'
V3_TO_V4_PENDING_FILE = STATE_DIR / 'v3_to_v4_pending.json'
V3_TO_V4_ARCHIVE_FILE = STATE_DIR / 'v3_to_v4_archive.json'
PENDING_LIQUIDATIONS_FILE = STATE_DIR / 'pending_liquidations.json'
V3_FEE_CLOSE_WAL_FILE = STATE_DIR / 'v3_fee_close_wal.json'
V4_CLOSE_WAL_FILE = STATE_DIR / 'v4_close_wal.json'
LIFECYCLE_OPS_FILE = STATE_DIR / 'lifecycle_ops.json'
LIFECYCLE_FAILSAFE = os.environ.get('LIFECYCLE_FAILSAFE','true').lower() in ('1','true','yes')
HARD_HALT_RECOVERY = STATE_DIR / 'HARD_HALT_RECOVERY'
LIQUIDATION_EXPIRY_SECONDS = int(os.environ.get('RH_LIQUIDATION_EXPIRY_SECONDS', 24*3600))
LIQUIDATION_BACKOFF_SECONDS = [15, 30, 60, 120, 300, 900]
LIQUIDATION_MANUAL_RETRY_SECONDS = 3600
LIQUIDATION_DUST_RAW = int(os.environ.get('RH_LIQUIDATION_DUST_RAW', '1'))
LIQUIDATION_MAX_PRICE_IMPACT_PCT = Decimal(os.environ.get('RH_LIQUIDATION_MAX_PRICE_IMPACT_PCT','10'))
# Settlement venues, in preference order. Kyber is the aggregator and wins ties, but
# pinning settlement to it alone means a sidecar outage strands the token in the
# wallet while it keeps falling — the close succeeds and the proceeds never arrive.
# The queue ranks every venue by realized output and rejects thin routes with a
# price-impact probe, so offering fallbacks cannot produce a worse fill than Kyber.
LIQUIDATION_VENUES = [v.strip() for v in os.environ.get(
    'RH_LIQUIDATION_VENUES', 'kyber,v3,v4,v2').split(',') if v.strip()]
ROTATION_READY_TTL_SECONDS = 30 * 60
V3_TO_V4_RETRY_SECONDS = 15 * 60
V3_TO_V4_EXPIRY_SECONDS = 6 * 3600

# Capital-preservation mode: rare, small entries; exits settle to USDG.
STRATEGY_MODE = os.environ.get('RH_LP_STRATEGY_MODE', 'stable_first_exit').strip().lower()
MAX_POSITIONS = 1
POSITION_SIZE_USDG = Decimal(os.environ.get('RH_LP_POSITION_SIZE_USDG', '1'))
V4_MAX_POSITION_USDG = min(Decimal(os.environ.get('V4_MAX_POSITION_USDG', '1')), Decimal('1'))
V4_COMPOUND_MAX_USDG = min(Decimal(os.environ.get('V4_COMPOUND_MAX_USDG', '250')), Decimal('250'))
USDG_RESERVE = Decimal(os.environ.get('USDG_RESERVE', '2'))
DRY_RUN = os.environ.get('RH_LP_DRY_RUN', '').lower() in ('1','true','yes')
# Hard lifecycle gate. Manager/entry/close code must use AtomicBackend while enabled;
# historical stranded-token recovery is the sole permitted legacy retry path.
ATOMIC_LP_ONLY = os.environ.get('ATOMIC_LP_ONLY', 'true').lower() in ('1','true','yes')
ATOMIC_EXECUTOR_ADDRESS = os.environ.get('ATOMIC_EXECUTOR_ADDRESS', '0xdfdb577269648B5d2F5aE1d87c08883b92696088').strip()
KYBERSWAP_ROUTER_ADDRESS = os.environ.get('KYBERSWAP_ROUTER_ADDRESS', '0x6131B5fae19EA4f9D964eAc0408E4408b66337b5').strip()
ATOMIC_V4_EXECUTOR_ADDRESS = os.environ.get('ATOMIC_V4_EXECUTOR_ADDRESS', '0x6589279cF08a99FF4B984706dE92DEd700607342').strip()
# Parallel WETH executor is intentionally unset until separately deployed/configured.
ATOMIC_V4_WETH_EXECUTOR_ADDRESS = os.environ.get('ATOMIC_V4_WETH_EXECUTOR_ADDRESS', '0xd81d7eAa3c6F7C1C65981B525dF658D6899BcA54').strip()
V4_SETTLEMENT_ASSETS = {'USDG': USDG, 'WETH': WETH}
V4_DEFAULT_SETTLEMENT = os.environ.get('V4_DEFAULT_SETTLEMENT', 'USDG').upper()
if V4_DEFAULT_SETTLEMENT not in V4_SETTLEMENT_ASSETS:
    raise ValueError('V4_DEFAULT_SETTLEMENT must be USDG or WETH')
GAS_RESERVE_ETH = Decimal('0.0005')      # ~15 tx reserve
RANGE_PCT = Decimal(os.environ.get('RH_LP_RANGE_PCT', '50'))  # fallback half-width when volatility is unknown
FEE_TIER = 10000                         # 1% (meme standard)
FEE_TIER_FALLBACKS = [10000, 3000, 500]  # try these in order if primary unavailable
STOP_LOSS_PCT = Decimal('10')            # NAV stop vs principal
HARVEST_MIN_USDG = Decimal(os.environ.get('RH_HARVEST_MIN_USDG', '0.25'))

# --- Range sizing -----------------------------------------------------------
# Width is derived from measured volatility rather than a fixed ladder: a range
# narrower than the token's own hourly movement is run over before it earns.
RANGE_SIGMAS = Decimal(os.environ.get('RH_RANGE_SIGMAS', '2'))
RANGE_HORIZON_HOURS = Decimal(os.environ.get('RH_RANGE_HORIZON_HOURS', '6'))
RANGE_MIN_PCT = Decimal(os.environ.get('RH_RANGE_MIN_PCT', '15'))
RANGE_MAX_PCT = Decimal(os.environ.get('RH_RANGE_MAX_PCT', '80'))

# --- Exit policy ------------------------------------------------------------
# Below-range means the position holds 100% of a falling memecoin, so it is a
# fast exit. Above-range means it holds 100% USDG and carries no token risk at
# all, so it waits instead of paying gas to re-center into a pump.
OOR_BELOW_MAX_SECONDS = int(os.environ.get('RH_OOR_BELOW_MAX_SECONDS', 15 * 60))
OOR_ABOVE_MAX_SECONDS = int(os.environ.get('RH_OOR_ABOVE_MAX_SECONDS', 6 * 3600))
MAX_DRAWDOWN_PCT = Decimal(os.environ.get('RH_MAX_DRAWDOWN_PCT', '12'))
MAX_TOKEN_EXPOSURE_PCT = Decimal(os.environ.get('RH_MAX_TOKEN_EXPOSURE_PCT', '92'))
ENTRY_EDGE_MARGIN = Decimal(os.environ.get('RH_ENTRY_EDGE_MARGIN', '1.5'))

# --- Fast crash detection ---------------------------------------------------
# The 1h baseline leaves a brand-new position unprotected for its first hour,
# which is exactly when a fresh memecoin is most likely to be dumped.
FAST_DUMP_WINDOW_SECONDS = int(os.environ.get('RH_FAST_DUMP_WINDOW_SECONDS', 600))
FAST_DUMP_PCT = Decimal(os.environ.get('RH_FAST_DUMP_PCT', '15'))
FAST_LIQ_DROP_PCT = Decimal(os.environ.get('RH_FAST_LIQ_DROP_PCT', '20'))
MIN_FEE_3H_USDG = Decimal('0')           # low-fee rotation disabled in stable-first mode
FEE_EVAL_WINDOW_HOURS = 3
ROTATION_MAX_LIQ_USD = Decimal('100000')
GMGN_TRENDING_MIN_AGE_HOURS = Decimal('168')
V4_REBALANCE_RETRY_SECONDS = 15 * 60
V4_SHALLOW_CONFIRM_SECONDS = 20 * 60
V4_MEDIUM_CONFIRM_SECONDS = 45 * 60
V4_DEEP_CONFIRM_SECONDS = 15 * 60
V4_REENTRY_DELAY_SECONDS = 45 * 60
V4_REENTRY_EXPIRY_SECONDS = 6 * 3600
V4_REBALANCE_COOLDOWN_SECONDS = 45 * 60
V4_EXECUTION_COST_BUFFER_USDG = Decimal(os.environ.get('V4_EXECUTION_COST_BUFFER_USDG','0.10'))
TOKEN_COOLDOWN_HOURS = 24                # cooldown per token after exit

# Filters (candidate)
MIN_LIQ_USD = Decimal('500000')
MIN_VOL24_USD = Decimal('20000')
MIN_AGE_HOURS = Decimal('168')
MAX_TOP10_PCT = Decimal('35')            # skip if concentrated

# Safety
MAX_SLIPPAGE_SWAP_PCT = Decimal('1')     # 1% hard cap
MAX_SLIPPAGE_MINT_PCT = Decimal('1')     # 1% mint
MAX_GAS_PRICE_GWEI = Decimal('5')        # skip if too high (RH normally ~0.04)
MAX_TX_COST_PCT = Decimal('2')           # skip action if gas > 2% of LP value

# Rug detection
RUG_LIQ_DROP_PCT_1H = Decimal('30')
DUMP_PCT_1H = Decimal('30')

# Telegram delivery target for alerts
TG_TARGET = 'telegram:-5252289772'
