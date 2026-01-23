import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
perp_root = repo_root.parent.parent

if "BOT_ENV_PATH" not in os.environ:
    candidate = perp_root / "private" / "Funding_Arbitrage.env"
    if candidate.exists():
        os.environ["BOT_ENV_PATH"] = str(candidate)

sys.path.append(str(repo_root))
sys.path.append(str(repo_root / "libs"))

from src.config import Config
from shared_crypto_lib.exchanges.lighter import LighterExchange
from shared_crypto_lib.models import OrderType, OrderSide


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def _net_position(positions, symbol: str) -> float:
    net = 0.0
    for p in positions:
        if p.symbol != symbol:
            continue
        net += p.amount if p.side == OrderSide.BUY else -p.amount
    return net


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--side", default="buy", choices=["buy", "sell"])
    parser.add_argument("--qty", type=float, default=0.0)
    parser.add_argument("--verify-retries", type=int, default=8)
    parser.add_argument("--verify-delay", type=float, default=2.0)
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--force-checks", action="store_true")
    args = parser.parse_args()

    exchange = LighterExchange(
        {
            "wallet_address": Config.LIGHTER_WALLET_ADDRESS,
            "private_key": Config.LIGHTER_PRIVATE_KEY,
            "public_key": Config.LIGHTER_PUBLIC_KEY,
            "api_key_index": Config.LIGHTER_API_KEY_INDEX,
            "env": Config.LIGHTER_ENV,
            "verify_retries": args.verify_retries,
            "verify_delay_s": args.verify_delay,
        }
    )
    await exchange.initialize()

    symbol = args.symbol or (Config.SYMBOLS[0] if Config.SYMBOLS else "AVNT-USDT")
    market = exchange.markets.get(symbol)
    if not market:
        raise RuntimeError(f"Market not found for {symbol}")

    ticker = await exchange.fetch_ticker(symbol)
    price = ticker.last or ticker.bid or ticker.ask or 1.0

    qty = args.qty or market.min_qty or 0.0
    if market.min_notional:
        qty = max(qty, market.min_notional / price)
    qty = _round_step(qty, market.qty_step)
    if qty <= 0:
        raise RuntimeError(f"Invalid qty computed: {qty}")

    side = OrderSide.BUY if args.side == "buy" else OrderSide.SELL
    opp_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

    positions_before = await exchange.fetch_positions()
    net_before = _net_position(positions_before, symbol)
    print(f"[BEFORE] {symbol} net={net_before}")

    t0 = time.time()
    order = await exchange.create_order(symbol, OrderType.MARKET, side, qty)
    t_ack = time.time()
    print(f"[ORDER] id={order.id} client_id={order.client_order_id} side={order.side} qty={order.amount}")
    print(f"[TIME] ack_ms={(t_ack - t0)*1000:.0f}")

    net_after = net_before
    for i in range(args.verify_retries + 1):
        t_check = time.time()
        open_orders = await exchange.fetch_open_orders(symbol)
        match_open = [o for o in open_orders if o.id == str(order.id) or o.client_order_id == str(order.client_order_id)]
        print(f"[VERIFY] attempt={i+1} open_orders={len(open_orders)} match_open={len(match_open)}")
        try:
            fetched = await exchange.fetch_order(str(order.id), symbol)
            print(f"[VERIFY] fetched status={fetched.status} id={fetched.id}")
        except Exception as e:
            print(f"[VERIFY] fetch_order failed: {e}")

        positions_after = await exchange.fetch_positions()
        net_after = _net_position(positions_after, symbol)
        print(f"[VERIFY] net_after={net_after} elapsed_ms={(t_check - t0)*1000:.0f}")
        if not args.force_checks and abs(net_after - net_before) >= max(market.qty_step, qty * 0.5):
            break
        if i < args.verify_retries:
            await asyncio.sleep(args.verify_delay)

    if args.close and abs(net_after) > 0:
        close_qty = abs(net_after - net_before)
        close_qty = _round_step(close_qty, market.qty_step)
        if close_qty > 0:
            print(f"[CLOSE] side={opp_side} qty={close_qty}")
            await exchange.create_order(symbol, OrderType.MARKET, opp_side, close_qty)
            await asyncio.sleep(args.verify_delay)
            positions_final = await exchange.fetch_positions()
            net_final = _net_position(positions_final, symbol)
            print(f"[FINAL] net={net_final}")


if __name__ == "__main__":
    asyncio.run(main())
