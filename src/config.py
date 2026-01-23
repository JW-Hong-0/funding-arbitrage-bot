import os
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ[key] = val


env_override = os.getenv("BOT_ENV_PATH")
if env_override:
    _load_env_file(Path(env_override))
else:
    _load_env_file(Path(__file__).resolve().parent / ".env")

class Config:
    # Environment Settings
    GRVT_ENV = os.getenv("GRVT_ENV", "PROD")  # TESTNET or PROD
    LIGHTER_ENV = os.getenv("LIGHTER_ENV", "MAINNET") # TESTNET or MAINNET
    
    # Load keys based on Environment
    if GRVT_ENV == "TESTNET":
        GRVT_API_KEY = os.getenv("GRVT_TESTNET_API_KEY")
        GRVT_PRIVATE_KEY = os.getenv("GRVT_TESTNET_SECRET_KEY")
        GRVT_TRADING_ACCOUNT_ID = os.getenv("GRVT_TESTNET_TRADING_ACCOUNT_ID")
    else:
        GRVT_API_KEY = os.getenv("GRVT_MAINNET_API_KEY")
        GRVT_PRIVATE_KEY = os.getenv("GRVT_MAINNET_SECRET_KEY")
        GRVT_TRADING_ACCOUNT_ID = os.getenv("GRVT_MAINNET_TRADING_ACCOUNT_ID")

    # Validate essential GRVT credentials
    if not GRVT_API_KEY:
        raise ValueError(f"GRVT_API_KEY is not set for {GRVT_ENV} environment. Please check your .env file.")
    if not GRVT_PRIVATE_KEY:
        raise ValueError(f"GRVT_PRIVATE_KEY is not set for {GRVT_ENV} environment. Please check your .env file.")
    if not GRVT_TRADING_ACCOUNT_ID:
        raise ValueError(f"GRVT_TRADING_ACCOUNT_ID is not set for {GRVT_ENV} environment. Please check your .env file.")

    if LIGHTER_ENV == "TESTNET":
        LIGHTER_WALLET_ADDRESS = os.getenv("LIGHTER_TESTNET_WALLET_ADDRESS")
        LIGHTER_PRIVATE_KEY = os.getenv("LIGHTER_TESTNET_PRIVATE_KEY")
        LIGHTER_PUBLIC_KEY = os.getenv("LIGHTER_TESTNET_PUBLIC_KEY")
        LIGHTER_API_KEY_INDEX = int(os.getenv("LIGHTER_TESTNET_API_KEY_INDEX", "2"))
    else:
        LIGHTER_WALLET_ADDRESS = os.getenv("LIGHTER_MAINNET_WALLET_ADDRESS")
        LIGHTER_PRIVATE_KEY = os.getenv("LIGHTER_MAINNET_PRIVATE_KEY")
        LIGHTER_PUBLIC_KEY = os.getenv("LIGHTER_MAINNET_PUBLIC_KEY")
        LIGHTER_API_KEY_INDEX = int(os.getenv("LIGHTER_MAINNET_API_KEY_INDEX", "2"))

    # Variational Settings
    VARIATIONAL_WALLET_ADDRESS = os.getenv("VARIATIONAL_WALLET_ADDRESS")
    VARIATIONAL_VR_TOKEN = os.getenv("VARIATIONAL_JWT_TOKEN")
    VARIATIONAL_PRIVATE_KEY = os.getenv("VARIATIONAL_PRIVATE_KEY")
    VARIATIONAL_SLIPPAGE = float(os.getenv("VARIATIONAL_SLIPPAGE", "0.01"))

    LIGHTER_WEB3_RPC_URL = os.getenv("LIGHTER_WEB3_RPC_URL", "https://arb1.arbitrum.io/rpc")

    # Strategy Settings
    DRY_RUN = False # Set to FALSE for actual Testnet testing
    LIGHTER_AMOUNT_SCALAR = 10000 # 0.0001 ETH/BTC unit? specific to Lighter
    SYMBOLS = ["AVNT-USDT", "IP-USDT", "BERA-USDT", "RESOLV-USDT"]
    SYMBOL_EXCLUDE = ["AI16Z"] # Symbols to exclude from automatic discovery
    SYMBOL = "ETH-USDT" # Legacy/Default support
    ORDER_AMOUNT = 0.001
    MARGIN_PER_TRADE_USDT = 20.0 # User defined margin per trade
    TARGET_LEVERAGE = 5 # User defined max leverage to use
    PER_TRADE_AMOUNT_USDT = 20.0 # Legacy fallback (Margin amount)
    MAX_POSITION = 0.1
    SPREAD_BPS = 5 # 0.05%
    HEDGE_SLIPPAGE_BPS = 20 # 0.2%
    LEVERAGE = 5 # Default leverage config

    # Order/Position Safety
    GRVT_ORDER_TTL_S = 45 # Max seconds to keep a GRVT maker order alive
    GRVT_ORDER_MIN_HOLD_S = 45 # Min seconds to keep a maker order before canceling on signal flip
    GRVT_PARTIAL_FILL_GRACE_S = 600 # Max seconds to wait after first partial fill before reset
    GRVT_MAKER_PRICE_BPS = 2 # Price offset to stay maker (bps)
    GRVT_DISABLE_LEVERAGE_UPDATE = False # Set True to skip leverage updates
    GRVT_USE_POSITION_CONFIG = True # Use set_position_config (EIP-712 signed) for leverage/margin
    GRVT_MARGIN_TYPE = "CROSS" # CROSS or ISOLATED
    HEDGE_TOLERANCE_RATIO = 0.05 # 5% net tolerance
    HEDGE_MIN_QTY = 0.01 # Minimum absolute qty tolerance
    HEDGE_RECOVERY_COOLDOWN_S = 20 # Cooldown between auto-recovery attempts
    HEDGE_INFLIGHT_TTL_S = 60 # Block duplicate hedge attempts per ticker
    HEDGE_NET_STABLE_S = 3 # Require net to be stable before recovery
    HEDGE_VERIFY_RETRIES = 5 # Hedge verify retries before re-order
    HEDGE_VERIFY_DELAY_S = 2 # Seconds between verify retries
    ENTRY_MARGIN_BUFFER = 0.05 # Extra buffer when checking available margin

    # Strategy/Exit Controls
    STRATEGY_SWITCH_COOLDOWN_S = 30 # Min seconds before switching venues
    STRATEGY_SWITCH_DELTA_BPS = 5 # Required score improvement to switch (bps)
    STRATEGY_SWITCH_WINDOW_S = 15 # Signal stabilization window
    STRATEGY_SWITCH_CONFIRM_COUNT = 5 # Consecutive confirmations required
    STRATEGY_SWITCH_SPREAD_MULTIPLIER = 2.0 # Require 2x spread cost improvement to switch
    STRATEGY_SWITCH_EVENT_WINDOW_MIN = 5 # Minutes after the hour to relax spread rule
    STRATEGY_SWITCH_SPREAD_MULTIPLIER_EVENT = 1.5 # Spread multiplier during event window
    EXIT_YIELD_THRESHOLD = 0.0 # Exit if projected yield drops below
    EXIT_SPREAD_THRESHOLD = 0.0 # Exit if spread drops below
    EXIT_COOLDOWN_S = 15 # Cooldown after exit
    EXIT_CONFIRM_COUNT = 3 # Consecutive exit confirmations required
    EXIT_IGNORE_SPREAD = True # Ignore spread for exit decisions
    EXIT_LOG_REASONS = True
    EXIT_ORDER_WAIT_S = 45 # Wait for open exit order before cancel/replace
    STRATEGY_REBALANCE_ENABLED = True
    STRATEGY_REBALANCE_MIN_HOLD_S = 120
    STRATEGY_REBALANCE_SCORE_DELTA_BPS = 2
    STRATEGY_REBALANCE_COOLDOWN_S = 10
    STRATEGY_REBALANCE_DEBUG = True
    STRATEGY_REBALANCE_SPREAD_MULTIPLIER = 1.5
    STRATEGY_REBALANCE_SPREAD_MULTIPLIER_EVENT = 1.2
    STRATEGY_REBALANCE_LOG_REASONS = True
    STRATEGY_REBALANCE_YIELD_MULTIPLIER = 1.5
    
    # Funding Logic
    FUNDING_DIFF_THRESHOLD = 0.0001 # 0.01% difference to trigger
    FUNDING_TIME_TOLERANCE_MS = 10000 # Treat near-same funding times as the same event
    FUNDING_EVENT_FREEZE_MS = 10000 # Avoid strategy-driven position changes near funding time
    ZERO_VENUE_SWITCH_DELTA_BPS = 10 # Require larger delta to switch among zero-funding venues
    ZERO_VENUE_LOG = True
    VAR_FUNDING_RATE_DIVISOR = 10000.0 # VAR funding_rate appears to be in bps; convert to decimal
    VAR_FUNDING_RATE_HOURLY = True # VAR funding_rate seems hourly; scale by interval hours when True

    # Sampling / Stabilization
    MARKET_SAMPLE_INTERVAL_S = 3
    
    # Dry Run Safety
    MAX_ACTIVE_POSITIONS = 1

    # Fallback Entry
    ENABLE_FALLBACK_ENTRY = True
    FALLBACK_MAX_ATTEMPTS = 3
    
    # Logging
    LOG_LEVEL = "INFO"
    SESSION_LOG_DIR = os.getenv("SESSION_LOG_DIR", "")
