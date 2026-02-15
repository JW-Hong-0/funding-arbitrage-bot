import asyncio
import json
import os
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

current_dir = Path(__file__).resolve().parent
repo_root = current_dir.parent
libs_dir = repo_root / "libs"
if libs_dir.exists():
    sys.path.append(str(libs_dir))

from shared_crypto_lib.exchanges.hyena import HyenaExchange
from shared_crypto_lib.models import OrderSide, OrderType


def _load_env_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
    except Exception:
        return


def _mask(addr: str) -> str:
    if not addr:
        return ""
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def _ceil_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return (int((qty + step - 1e-12) / step) + 0) * step


def _percentile(vals, pct: float):
    if not vals:
        return None
    vals = sorted(vals)
    idx = int(round((len(vals) - 1) * pct))
    return vals[idx]


async def _wait_for_position(hyena: HyenaExchange, symbol: str, expect: str, poll_s: float, timeout_s: float):
    """expect: 'open' or 'closed'"""
    start = time.time()
    while time.time() - start < timeout_s:
        positions = await hyena.fetch_positions()
        has_pos = any(p.symbol == symbol and p.amount > 0 for p in positions)
        if expect == "open" and has_pos:
            return time.time() - start
        if expect == "closed" and not has_pos:
            return time.time() - start
        await asyncio.sleep(poll_s)
    return None


async def _close_all(hyena: HyenaExchange, symbol: str, poll_s: float, timeout_s: float):
    positions = await hyena.fetch_positions()
    for pos in positions:
        if pos.symbol == symbol and pos.amount > 0:
            await hyena.create_order(
                symbol,
                OrderType.MARKET,
                OrderSide.SELL,
                pos.amount,
                params={"reduce_only": True},
            )
            break
    await _wait_for_position(hyena, symbol, "closed", poll_s, timeout_s)


async def main():
    env_path = os.environ.get("BOT_ENV_PATH")
    if load_dotenv:
        if env_path and os.path.exists(env_path):
            load_dotenv(env_path)
        else:
            load_dotenv()
    else:
        _load_env_file(env_path or "")

    private_key = os.getenv("HYENA_PRIVATE_KEY") or os.getenv("HYPERLIQUID_PRIVATE_KEY")
    if not private_key:
        raise SystemExit("HYENA_PRIVATE_KEY or HYPERLIQUID_PRIVATE_KEY is required.")

    wallet_address = os.getenv("HYENA_WALLET_ADDRESS") or os.getenv("HYPERLIQUID_API_WALLET_ADDRESS")
    main_address = os.getenv("HYENA_MAIN_ADDRESS") or os.getenv("HYPERLIQUID_MAIN_ADDRESS")
    dex_id = os.getenv("HYENA_DEX_ID", "hyna")
    symbol = os.getenv("HYENA_SYMBOL", "hyna:HYPE")
    runs = int(os.getenv("HYENA_LATENCY_RUNS", "3"))
    poll_s = float(os.getenv("HYENA_LATENCY_POLL_S", "0.5"))
    timeout_s = float(os.getenv("HYENA_LATENCY_TIMEOUT_S", "10"))
    test_usd = float(os.getenv("HYENA_TEST_USD", "15"))

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_dir = current_dir / f"hyena_latency_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    hyena = HyenaExchange(
        {
            "private_key": private_key,
            "wallet_address": wallet_address,
            "main_address": main_address,
            "dex_id": dex_id,
        }
    )

    print("Initializing Hyena...")
    await hyena.initialize()
    print(f"Wallet address: {_mask(hyena.wallet_address)}")
    print(f"Main address: {_mask(hyena.main_address)}")
    print(f"State address: {_mask(hyena._state_address())}")

    markets = await hyena.load_markets()
    info = markets.get(symbol)
    if not info:
        raise SystemExit("Market info not found for symbol.")

    price = (await hyena.fetch_ticker(symbol)).last
    step = info.qty_step or info.min_qty
    min_notional = getattr(info, "min_notional", 10.0) or 10.0
    target_notional = max(test_usd, min_notional + 0.1)
    qty = _ceil_step(target_notional / price, step)
    if qty < info.min_qty:
        qty = info.min_qty

    await _close_all(hyena, symbol, poll_s, timeout_s)

    entry_lat = []
    exit_lat = []
    log_path = out_dir / "latency_runs.csv"
    with log_path.open("w", newline="", encoding="utf-8") as f:
        f.write("run,entry_latency_s,exit_latency_s\n")
        for i in range(1, runs + 1):
            print(f"[Run {i}] Placing BUY {qty} {symbol}")
            await hyena.create_order(symbol, OrderType.MARKET, OrderSide.BUY, qty)
            t_entry = await _wait_for_position(hyena, symbol, "open", poll_s, timeout_s)
            print(f"[Run {i}] Entry latency: {t_entry}")

            positions = await hyena.fetch_positions()
            for pos in positions:
                if pos.symbol == symbol and pos.amount > 0:
                    await hyena.create_order(
                        symbol,
                        OrderType.MARKET,
                        OrderSide.SELL,
                        pos.amount,
                        params={"reduce_only": True},
                    )
                    break

            t_exit = await _wait_for_position(hyena, symbol, "closed", poll_s, timeout_s)
            print(f"[Run {i}] Exit latency: {t_exit}")
            f.write(f"{i},{t_entry},{t_exit}\n")

            if t_entry is not None:
                entry_lat.append(t_entry)
            if t_exit is not None:
                exit_lat.append(t_exit)

            await asyncio.sleep(1)

    summary = {
        "symbol": symbol,
        "runs": runs,
        "entry": {
            "count": len(entry_lat),
            "min": min(entry_lat) if entry_lat else None,
            "max": max(entry_lat) if entry_lat else None,
            "avg": (sum(entry_lat) / len(entry_lat)) if entry_lat else None,
            "p50": _percentile(entry_lat, 0.5),
            "p95": _percentile(entry_lat, 0.95),
        },
        "exit": {
            "count": len(exit_lat),
            "min": min(exit_lat) if exit_lat else None,
            "max": max(exit_lat) if exit_lat else None,
            "avg": (sum(exit_lat) / len(exit_lat)) if exit_lat else None,
            "p50": _percentile(exit_lat, 0.5),
            "p95": _percentile(exit_lat, 0.95),
        },
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(f"Saved summary.json to {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
