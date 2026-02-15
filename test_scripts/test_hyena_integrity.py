import asyncio
import csv
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
try:
    import websockets
except ImportError:
    websockets = None

current_dir = Path(__file__).resolve().parent
repo_root = current_dir.parent
libs_dir = repo_root / "libs"
if libs_dir.exists():
    sys.path.append(str(libs_dir))

from shared_crypto_lib.exchanges.hyena import HyenaExchange
from shared_crypto_lib.models import OrderSide, OrderType


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "y")


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
    funding_symbols = os.getenv("HYENA_FUNDING_SYMBOLS", "hyna:HYPE,hyna:LIGHTER")
    funding_symbols = [s.strip() for s in funding_symbols.split(",") if s.strip()]
    sample_seconds = int(os.getenv("HYENA_SAMPLE_SECONDS", "60"))
    sample_interval = float(os.getenv("HYENA_SAMPLE_INTERVAL_S", "5"))
    place_order = _env_flag("HYENA_PLACE_ORDER", "0")
    test_usd = float(os.getenv("HYENA_TEST_USD", "15"))
    ws_compare = _env_flag("HYENA_WS_COMPARE", "1")

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_dir = current_dir / f"hyena_integrity_{ts}"
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
    meta_out = {
        "symbol": symbol,
        "dex_id": dex_id,
        "market": None,
    }
    info = markets.get(symbol)
    if info:
        meta_out["market"] = {
            "min_qty": info.min_qty,
            "qty_step": info.qty_step,
            "max_leverage": getattr(info, "max_leverage", None),
            "min_notional": getattr(info, "min_notional", None),
            "raw": info.raw,
        }
    (out_dir / "meta.json").write_text(json.dumps(meta_out, indent=2), encoding="utf-8")
    print(f"Saved meta.json to {out_dir}")

    # Funding + price sampling in parallel
    funding_path = out_dir / "funding_samples.csv"
    price_path = out_dir / "price_poll.csv"

    async def _funding_task():
        with funding_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ts", "symbol", "funding_rate", "next_funding_time"])
            start = time.time()
            while time.time() - start < sample_seconds:
                for sym in funding_symbols:
                    fr = await hyena.fetch_funding_rate(sym)
                    writer.writerow([time.time(), sym, fr.rate, fr.next_funding_time])
                await asyncio.sleep(sample_interval)

    async def _price_task():
        with price_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ts", "symbol", "last"])
            start = time.time()
            while time.time() - start < sample_seconds:
                tk = await hyena.fetch_ticker(symbol)
                writer.writerow([time.time(), symbol, tk.last])
                await asyncio.sleep(sample_interval)

    # WS vs Polling comparison (runs in same sampling window)
    ws_path = out_dir / "price_ws_vs_poll.csv"
    if ws_compare and websockets is None:
        print("websockets not installed; skipping WS comparison.")
    elif ws_compare:
        ws_url = "wss://api.hyperliquid.xyz/ws"
        last_ws = {"ts": 0.0, "price": 0.0}

        async def _ws_task():
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("channel") != "allMids":
                        continue
                    mids = data.get("data", {}).get("mids", {})
                    if symbol in mids:
                        try:
                            last_ws["ts"] = time.time()
                            last_ws["price"] = float(mids[symbol])
                        except Exception:
                            pass

        async def _poll_task():
            with ws_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["ts", "symbol", "poll_price", "ws_price", "diff_abs", "diff_bps"])
                start = time.time()
                while time.time() - start < sample_seconds:
                    tk = await hyena.fetch_ticker(symbol)
                    poll_price = tk.last
                    ws_price = last_ws["price"]
                    diff_abs = abs(poll_price - ws_price) if ws_price > 0 else 0.0
                    diff_bps = (diff_abs / poll_price * 10000) if (poll_price > 0 and ws_price > 0) else 0.0
                    writer.writerow([time.time(), symbol, poll_price, ws_price, diff_abs, diff_bps])
                    await asyncio.sleep(sample_interval)

        print("Running WS vs polling comparison...")
        ws_task = asyncio.create_task(_ws_task())
        await _poll_task()
        ws_task.cancel()
        print(f"Saved price_ws_vs_poll.csv to {ws_path}")

    # Run sampling tasks concurrently (WS compare uses its own loop)
    await asyncio.gather(_funding_task(), _price_task())
    print(f"Saved funding_samples.csv to {funding_path}")
    print(f"Saved price_poll.csv to {price_path}")

    # Balance snapshot
    bal = await hyena.fetch_balance()
    (out_dir / "balance.json").write_text(
        json.dumps({"USDe": bal.get("USDe").__dict__ if bal.get("USDe") else None}, indent=2),
        encoding="utf-8",
    )
    print("Saved balance.json")

    # Optional order cycle
    if place_order and info:
        price = (await hyena.fetch_ticker(symbol)).last
        qty = max(info.min_qty, (test_usd / price))
        # Adjust to step
        step = info.qty_step or info.min_qty
        if step > 0:
            qty = (qty // step) * step
        print(f"Placing BUY {qty} {symbol} (~${test_usd})")
        order = await hyena.create_order(symbol, OrderType.MARKET, OrderSide.BUY, qty)
        (out_dir / "order_entry.json").write_text(json.dumps(order.raw, indent=2), encoding="utf-8")
        await asyncio.sleep(5)
        positions = await hyena.fetch_positions()
        (out_dir / "positions_after_entry.json").write_text(
            json.dumps([p.__dict__ for p in positions], indent=2),
            encoding="utf-8",
        )
        # Close
        for pos in positions:
            if pos.symbol == symbol and pos.amount > 0:
                await hyena.create_order(
                    symbol,
                    OrderType.MARKET,
                    OrderSide.SELL,
                    pos.amount,
                    params={"reduce_only": True},
                )
                await asyncio.sleep(2)
                break
        positions = await hyena.fetch_positions()
        (out_dir / "positions_after_close.json").write_text(
            json.dumps([p.__dict__ for p in positions], indent=2),
            encoding="utf-8",
        )
        print("Saved order_entry.json and positions snapshots.")

    # Summary report
    # Basic stats
    def _stats_from_csv(path: Path):
        if not path.exists():
            return None
        rows = []
        with path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    funding_rows = _stats_from_csv(funding_path) or []
    funding_stats = {}
    for sym in funding_symbols:
        vals = [float(r["funding_rate"]) for r in funding_rows if r.get("symbol") == sym]
        if vals:
            funding_stats[sym] = {
                "count": len(vals),
                "min": min(vals),
                "max": max(vals),
                "avg": sum(vals) / len(vals),
            }

    ws_rows = _stats_from_csv(ws_path) if ws_compare else None
    ws_stats = None
    if ws_rows:
        diffs = [float(r["diff_bps"]) for r in ws_rows if r.get("diff_bps")]
        if diffs:
            ws_stats = {
                "count": len(diffs),
                "min_bps": min(diffs),
                "max_bps": max(diffs),
                "avg_bps": sum(diffs) / len(diffs),
            }

    report = {
        "symbol": symbol,
        "dex_id": dex_id,
        "funding_symbols": funding_symbols,
        "funding_stats": funding_stats,
        "ws_stats": ws_stats,
        "files": {
            "meta": "meta.json",
            "funding_samples": "funding_samples.csv",
            "price_poll": "price_poll.csv",
            "price_ws_vs_poll": "price_ws_vs_poll.csv" if ws_compare else None,
            "balance": "balance.json",
        },
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Saved report.json")


if __name__ == "__main__":
    asyncio.run(main())
