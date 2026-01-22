import asyncio
import logging
import os
import sys
from dotenv import load_dotenv
from pathlib import Path

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, ".."))
repo_root = Path(project_root)
perp_root = repo_root.parent.parent

# Load env from private folder (fallback to default behavior).
env_path = os.environ.get("BOT_ENV_PATH")
if not env_path:
    candidate = perp_root / "private" / "Funding_Arbitrage.env"
    if candidate.exists():
        env_path = str(candidate)
    else:
        env_path = str(repo_root / "private" / "Funding_Arbitrage.env")
os.environ.setdefault("BOT_ENV_PATH", env_path)
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    load_dotenv()

# Ensure Source Path
sys.path.append(project_root)
sys.path.append(current_dir) # Add script dir for local imports (services)
libs_dir = repo_root / "libs"
if libs_dir.exists():
    sys.path.append(str(libs_dir))

# Shared Library Imports
from shared_crypto_lib.factory import ExchangeFactory, ExchangeType
from shared_crypto_lib.base import AbstractExchange

# Service Imports
from monitoring_service import MonitoringService
from asset_manager import AssetManager
from trading_service import TradingService
from dashboard import Dashboard
from logging_service import SessionLogManager

# Config Encoding for Windows
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# Config Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(f"modular_bot_{os.getpid()}.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Main")

def _attach_system_log(log_dir: str) -> None:
    if not log_dir:
        return
    path = os.path.join(log_dir, "system.log")
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == path:
            return
    file_handler = logging.FileHandler(path, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

async def main():
    load_dotenv()
    logger.info("🤖 Starting Modular Bot (Shared Lib Version)...")
    from src.config import Config
    log_dir = getattr(Config, "SESSION_LOG_DIR", "") or os.path.join(project_root, "logs")
    log_manager = SessionLogManager(log_dir or None)
    _attach_system_log(log_manager.base_dir)
    
    # 1. Initialize Exchanges (Shared Factory)
    
    # GRVT Config
    # Explicitly pass config from Config class or env to ensure Subaccount ID is set
    g_config = {
        "api_key": Config.GRVT_API_KEY,
        "private_key": Config.GRVT_PRIVATE_KEY,
        "subaccount_id": Config.GRVT_TRADING_ACCOUNT_ID,
        "env": Config.GRVT_ENV,
        "disable_leverage_update": getattr(Config, "GRVT_DISABLE_LEVERAGE_UPDATE", False),
        "use_position_config": getattr(Config, "GRVT_USE_POSITION_CONFIG", False),
        "position_margin_type": getattr(Config, "GRVT_MARGIN_TYPE", "CROSS"),
    }
    grvt = ExchangeFactory.create_exchange(ExchangeType.GRVT, config=g_config)
    
    # Lighter Config
    l_config = {
        "wallet_address": Config.LIGHTER_WALLET_ADDRESS,
        "private_key": Config.LIGHTER_PRIVATE_KEY,
        "public_key": Config.LIGHTER_PUBLIC_KEY,
        "api_key_index": Config.LIGHTER_API_KEY_INDEX,
        "env": Config.LIGHTER_ENV
    }
    lighter = ExchangeFactory.create_exchange(ExchangeType.LIGHTER, config=l_config)
    
    # Variational Config
    variational: AbstractExchange = None
    var_wallet = os.getenv("VARIATIONAL_WALLET_ADDRESS")
    if var_wallet:
         v_config = {
             "wallet_address": var_wallet,
             "vr_token": os.getenv("VARIATIONAL_VR_TOKEN"),
             "private_key": os.getenv("VARIATIONAL_PRIVATE_KEY"),
             "slippage": float(os.getenv("VARIATIONAL_SLIPPAGE", "0.01")),
         }
         variational = ExchangeFactory.create_exchange(ExchangeType.VARIATIONAL, config=v_config)
    else:
         logger.warning("⚠️ Variational Env Vars missing. Skipping VAR.")
         
    # Initialize Connections
    logger.info("Initializing GRVT...")
    await grvt.initialize()
    logger.info("Initializing Lighter...")
    await lighter.initialize()
    if variational: 
        logger.info("Initializing Variational...")
        await variational.initialize()

    # Init Check
    if not grvt or not lighter:
        logger.critical("❌ Failed to initialize Primary Exchanges (GRVT/Lighter). Exiting.")
        return

    # 2. Initialize Modules
    # Note: Services must be updated to accept AbstractExchange
    monitor = MonitoringService(grvt, lighter, variational, log_manager)
    await monitor.initialize()
    
    asset_manager = AssetManager(grvt, lighter, variational, log_manager)
    await asset_manager.update_assets() # Initial load
    
    trading = TradingService(grvt, lighter, variational, monitor, asset_manager, log_manager)
    
    dashboard = Dashboard(monitor, asset_manager, trading)
    
    logger.info("✅ All Modules Initialized. Starting Loop...")
    
    # 3. Main Loop
    try:
        while True:
            try:
                # Update Data
                await monitor.update_market_data()
                
                # Asset Update
                await asset_manager.update_assets()
                
                # Execute Logic
                await trading.process_tick()
                
                # Update Dashboard
                dashboard.display()
                
            except Exception as e:
                logger.error(f"Main Loop Criticial Error: {e}")
                
            await asyncio.sleep(3) # 3s loop (sampling interval)
    finally:
        if log_manager:
            log_manager.finalize_to_xlsx()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot Stopped by User.")
    except Exception as e:
        logger.critical(f"Fatal Error: {e}")
