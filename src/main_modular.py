import asyncio
import logging
import os
import sys
import time
import signal
import atexit
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

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def _terminate_pid(pid: int, timeout_s: float = 5.0) -> bool:
    if not _pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        return False
    time.sleep(0.2)
    return not _pid_alive(pid)

def _pid_matches_bot(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return False
    cmd = cmdline.decode("utf-8", errors="ignore").replace("\x00", " ")
    # Only match this modular bot process (avoid touching other bots).
    if ("src.main_modular" in cmd) and ("funding_arbitrage_bot" in cmd):
        return True
    # Fallback: if it's a python process launched from this repo root, treat as our bot.
    if "python" not in cmd:
        return False
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return False
    return cwd.startswith(str(repo_root))

def _find_other_bot_pids() -> list[int]:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    current = os.getpid()
    pids: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == current:
            continue
        if _pid_matches_bot(pid):
            pids.append(pid)
    return sorted(pids)

def _kill_other_bot_pids() -> None:
    pids = _find_other_bot_pids()
    if not pids:
        return
    logger.warning("Found other bot processes: %s", pids)
    for pid in pids:
        _terminate_pid(pid)

def _acquire_lock(lock_path: Path, kill_existing: bool = False) -> None:
    pid = os.getpid()
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(pid).encode("utf-8"))
        os.close(fd)
        logger.info("🔒 Lock acquired: %s (pid=%s)", lock_path, pid)
        return
    except FileExistsError:
        existing_pid = None
        try:
            existing_pid = int(lock_path.read_text().strip())
        except Exception:
            pass
        if existing_pid and _pid_alive(existing_pid):
            if kill_existing:
                if not _pid_matches_bot(existing_pid):
                    raise RuntimeError(
                        f"Lock held by pid={existing_pid}, but cmdline mismatch; refusing to kill."
                    )
                logger.warning("Lock held by pid=%s; attempting to terminate.", existing_pid)
                if _terminate_pid(existing_pid):
                    try:
                        lock_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return _acquire_lock(lock_path, kill_existing=False)
                raise RuntimeError(f"Failed to terminate existing bot pid={existing_pid}.")
            raise RuntimeError(f"Bot already running (pid={existing_pid}).")
        # Stale lock file
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        return _acquire_lock(lock_path, kill_existing=kill_existing)

def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
        logger.info("🔓 Lock released: %s", lock_path)
    except Exception:
        pass

def _install_lock(lock_path: Path, kill_existing: bool = False) -> None:
    _acquire_lock(lock_path, kill_existing=kill_existing)
    atexit.register(_release_lock, lock_path)
    def _handle(sig, frame):  # pragma: no cover - runtime cleanup
        _release_lock(lock_path)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

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
    
    # Hyena Config
    h_config = {
        "private_key": Config.HYENA_PRIVATE_KEY,
        "wallet_address": Config.HYENA_WALLET_ADDRESS,
        "main_address": Config.HYENA_MAIN_ADDRESS,
        "dex_id": Config.HYENA_DEX_ID,
        "use_symbol_prefix": Config.HYENA_USE_SYMBOL_PREFIX,
        "builder_address": Config.HYENA_BUILDER_ADDRESS,
        "builder_fee": Config.HYENA_BUILDER_FEE,
        "approve_builder_fee": Config.HYENA_APPROVE_BUILDER_FEE,
        "builder_max_fee_rate": Config.HYENA_BUILDER_MAX_FEE_RATE,
        "slippage": Config.HYENA_SLIPPAGE,
        "min_notional": Config.HYENA_MIN_NOTIONAL,
        "prefer_spot_balance": Config.HYENA_PREFER_SPOT_BALANCE,
        "funding_cache_ttl_s": Config.HYENA_FUNDING_CACHE_TTL_S,
        "funding_backoff_s": Config.HYENA_FUNDING_BACKOFF_S,
        "funding_backoff_max_s": Config.HYENA_FUNDING_BACKOFF_MAX_S,
    }
    hyena = ExchangeFactory.create_exchange(ExchangeType.HYENA, config=h_config)
    
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
    logger.info("Initializing Hyena...")
    await hyena.initialize()
    if variational:
        logger.info("Initializing Variational...")
        await variational.initialize()

    # Init Check
    if not grvt or not hyena:
        logger.critical("❌ Failed to initialize Primary Exchanges (GRVT/Hyena). Exiting.")
        return

    # 2. Initialize Modules
    # Note: Services must be updated to accept AbstractExchange
    monitor = MonitoringService(grvt, hyena, variational, log_manager)
    await monitor.initialize()
    
    asset_manager = AssetManager(grvt, hyena, variational, log_manager)
    await asset_manager.update_assets() # Initial load
    
    trading = TradingService(grvt, hyena, variational, monitor, asset_manager, log_manager)
    if getattr(Config, "TRADING_START_PAUSED", False):
        trading.set_paused(True, reason="startup")

    dashboard = Dashboard(monitor, asset_manager, trading)
    ui_server = None
    if getattr(Config, "UI_ENABLED", False):
        from ui_server import UiServer
        ui_server = UiServer(
            monitor,
            asset_manager,
            trading,
            host=getattr(Config, "UI_HOST", "127.0.0.1"),
            port=int(getattr(Config, "UI_PORT", 8080)),
            refresh_s=float(getattr(Config, "UI_REFRESH_S", 2)),
            show_raw=bool(getattr(Config, "UI_SHOW_RAW", False)),
        )
        await ui_server.start()
        logger.info("🖥️ UI server started at http://%s:%s", ui_server.host, ui_server.port)
    
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
        if ui_server:
            await ui_server.stop()
        if log_manager:
            log_manager.finalize_to_xlsx()

if __name__ == "__main__":
    try:
        lock_path = repo_root / ".bot.lock"
        kill_existing_env = os.getenv("BOT_LOCK_KILL_EXISTING")
        if kill_existing_env is None:
            kill_existing = True
        else:
            kill_existing = kill_existing_env.lower() in ("1", "true", "yes")
        kill_all = os.getenv("BOT_LOCK_KILL_ALL", "0").lower() in ("1", "true", "yes")
        if kill_all:
            _kill_other_bot_pids()
            kill_existing = True
        _install_lock(lock_path, kill_existing=kill_existing)
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot Stopped by User.")
    except Exception as e:
        logger.critical(f"Fatal Error: {e}")
