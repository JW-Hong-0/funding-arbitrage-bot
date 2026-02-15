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
    HYENA_DEX_ID = os.getenv("HYENA_DEX_ID", "hyna")
    
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

    # Hyena (Hyperliquid HIP-3) Settings
    HYENA_PRIVATE_KEY = os.getenv("HYENA_PRIVATE_KEY") or os.getenv("HYPERLIQUID_PRIVATE_KEY")
    HYENA_WALLET_ADDRESS = os.getenv("HYENA_WALLET_ADDRESS") or os.getenv("HYPERLIQUID_API_WALLET_ADDRESS")
    HYENA_MAIN_ADDRESS = os.getenv("HYENA_MAIN_ADDRESS") or os.getenv("HYPERLIQUID_MAIN_ADDRESS")
    HYENA_USE_SYMBOL_PREFIX = os.getenv("HYENA_USE_SYMBOL_PREFIX", "1") not in ("0", "false", "False")
    HYENA_BUILDER_ADDRESS = os.getenv(
        "HYENA_BUILDER_ADDRESS",
        "0x1924b8561eeF20e70Ede628A296175D358BE80e5",
    )
    HYENA_BUILDER_FEE = int(os.getenv("HYENA_BUILDER_FEE", "0"))
    HYENA_APPROVE_BUILDER_FEE = os.getenv("HYENA_APPROVE_BUILDER_FEE", "0") in ("1", "true", "True")
    HYENA_BUILDER_MAX_FEE_RATE = os.getenv("HYENA_BUILDER_MAX_FEE_RATE", "0")
    HYENA_SLIPPAGE = float(os.getenv("HYENA_SLIPPAGE", "0.05"))
    HYENA_MIN_NOTIONAL = float(os.getenv("HYENA_MIN_NOTIONAL", "10"))
    HYENA_PREFER_SPOT_BALANCE = os.getenv("HYENA_PREFER_SPOT_BALANCE", "1") not in ("0", "false", "False")
    HYENA_FUNDING_CACHE_TTL_S = float(os.getenv("HYENA_FUNDING_CACHE_TTL_S", "2"))
    HYENA_FUNDING_BACKOFF_S = float(os.getenv("HYENA_FUNDING_BACKOFF_S", "2"))
    HYENA_FUNDING_BACKOFF_MAX_S = float(os.getenv("HYENA_FUNDING_BACKOFF_MAX_S", "30"))

    if not HYENA_PRIVATE_KEY:
        raise ValueError("HYENA_PRIVATE_KEY is not set. Please check your .env file.")

    # Variational Settings
    VARIATIONAL_WALLET_ADDRESS = os.getenv("VARIATIONAL_WALLET_ADDRESS")
    VARIATIONAL_VR_TOKEN = os.getenv("VARIATIONAL_JWT_TOKEN")
    VARIATIONAL_PRIVATE_KEY = os.getenv("VARIATIONAL_PRIVATE_KEY")
    VARIATIONAL_SLIPPAGE = float(os.getenv("VARIATIONAL_SLIPPAGE", "0.01"))

    # Strategy Settings
    DRY_RUN = False # Set to FALSE for actual Testnet testing
    SYMBOLS = ["AVNT-USDT", "IP-USDT", "BERA-USDT", "RESOLV-USDT"]
    SYMBOL_EXCLUDE = ["AI16Z"] # Symbols to exclude from automatic discovery
    AUTO_SYMBOLS = os.getenv("AUTO_SYMBOLS", "0") in ("1", "true", "True")
    AUTO_SYMBOLS_MIN_EXCHANGES = int(os.getenv("AUTO_SYMBOLS_MIN_EXCHANGES", "2"))
    AUTO_SYMBOLS_MAX = int(os.getenv("AUTO_SYMBOLS_MAX", "0"))
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
    PARTIAL_WATCHDOG_S = 120 # Clear recovery locks if partial hedge persists
    PARTIAL_WATCHDOG_CLEAR_EXIT = True # Allow watchdog to clear exit_inflight after TTL
    PARTIAL_WATCHDOG_CLEAR_PENDING = True # Clear pending_tickers when watchdog triggers
    PARTIAL_WATCHDOG_CLEAR_RECOVERY_COOLDOWN = True # Clear recovery cooldown on watchdog
    RECOVERY_SKIP_LOG_THROTTLE_S = 60 # Throttle skip-recovery logs per ticker/reason
    EXIT_INFLIGHT_TTL_S = 180 # Block re-entry/auto-hedge until exit completes or TTL expires
    EXIT_REPRICE_AFTER_S = 60 # Reprice GRVT exit order after this many seconds
    EXIT_REPRICE_BPS = 10 # Extra aggressiveness (bps) when repricing exit order
    ENTRY_MARGIN_BUFFER = 0.05 # Extra buffer when checking available margin
    ENTRY_FAIL_COOLDOWN_INSUFFICIENT_S = 120 # Entry cooldown on insufficient margin/balance
    ENTRY_FAIL_COOLDOWN_AUTH_S = 30 # Entry cooldown on auth/401 issues
    ENTRY_FAIL_COOLDOWN_RATE_S = 30 # Entry cooldown on rate limit
    ENTRY_FAIL_COOLDOWN_NONCE_S = 30 # Entry cooldown on nonce/sequence issues
    ENTRY_FAIL_COOLDOWN_NETWORK_S = 10 # Entry cooldown on network/timeouts
    ENTRY_FAIL_COOLDOWN_UNKNOWN_S = 30 # Entry cooldown on unknown error

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
    GRVT_FILL_CONFIRM_S = 3  # Wait this long for GRVT maker orders to appear/settle before hedging
    EXIT_COMPLETE_COOLDOWN_S = 90 # Cooldown after exit completion before any re-entry/auto-hedge
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
    FUNDING_DECISION_LEAD_S = 40 * 60 # Start strategy decisions this many seconds before next funding
    FUNDING_DECISION_LOG = True
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

    # UI Settings
    UI_ENABLED = os.getenv("UI_ENABLED", "0") in ("1", "true", "True")
    UI_HOST = os.getenv("UI_HOST", "127.0.0.1")
    UI_PORT = int(os.getenv("UI_PORT", "8080"))
    UI_REFRESH_S = float(os.getenv("UI_REFRESH_S", "2"))
    UI_SHOW_RAW = os.getenv("UI_SHOW_RAW", "0") in ("1", "true", "True")
    TRADING_START_PAUSED = os.getenv("TRADING_START_PAUSED", "1") in ("1", "true", "True")
