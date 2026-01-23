import asyncio
import math
import os
import sys
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
from shared_crypto_lib.models import OrderType, OrderSide


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


def _adjust_qty(qty: float, step: float, min_qty: float) -> float:
    if qty <= 0:
        return 0.0
    if step > 0:
        qty = (qty // step) * step
    if qty < min_qty:
        return 0.0
    return qty

def _format_qty(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return float(f"{qty:.8f}")

def _ceil_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.ceil(qty / step) * step

def _mask_addr(addr: str) -> str:
    if not addr:
        return ""
    addr = addr.strip()
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
    wallet_address = os.getenv("HYENA_WALLET_ADDRESS") or os.getenv("HYPERLIQUID_API_WALLET_ADDRESS")
    main_address = os.getenv("HYENA_MAIN_ADDRESS") or os.getenv("HYPERLIQUID_MAIN_ADDRESS")
    dex_id = os.getenv("HYENA_DEX_ID", "hyna")
    symbol = os.getenv("HYENA_SYMBOL", "hyna:LIT")
    use_prefix = _env_flag("HYENA_USE_SYMBOL_PREFIX", "1")
    builder = os.getenv(
        "HYENA_BUILDER_ADDRESS",
        "0x1924b8561eeF20e70Ede628A296175D358BE80e5",
    )
    builder_fee = int(os.getenv("HYENA_BUILDER_FEE", "0"))
    approve_builder_fee = _env_flag("HYENA_APPROVE_BUILDER_FEE", "0")
    builder_max_fee_rate = os.getenv("HYENA_BUILDER_MAX_FEE_RATE", "0")
    slippage = float(os.getenv("HYENA_SLIPPAGE", "0.05"))
    test_leverage = int(os.getenv("HYENA_TEST_LEVERAGE", "3"))
    test_usd = float(os.getenv("HYENA_TEST_USD", "10"))
    place_order = _env_flag("HYENA_PLACE_ORDER", "0")
    debug_state = _env_flag("HYENA_DEBUG_STATE", "0")
    run_min_tests = _env_flag("HYENA_TEST_MIN_ORDERS", "0")

    if not private_key:
        raise SystemExit("HYENA_PRIVATE_KEY or HYPERLIQUID_PRIVATE_KEY is required.")

    hyena = HyenaExchange(
        {
            "private_key": private_key,
            "wallet_address": wallet_address,
            "main_address": main_address,
            "dex_id": dex_id,
            "builder_address": builder,
            "builder_fee": builder_fee,
            "approve_builder_fee": approve_builder_fee,
            "builder_max_fee_rate": builder_max_fee_rate,
            "slippage": slippage,
            "use_symbol_prefix": use_prefix,
        }
    )

    print("Initializing Hyena...")
    await hyena.initialize()
    print(f"Wallet address: {_mask_addr(hyena.wallet_address)}")
    print(f"Main address: {_mask_addr(hyena.main_address)}")
    try:
        print(f"State address: {_mask_addr(hyena._state_address())}")
    except Exception:
        pass
    markets = await hyena.load_markets()
    print(f"Markets loaded: {len(markets)}")
    sample_syms = list(markets.keys())[:5]
    print(f"Market samples: {sample_syms}")

    ticker = await hyena.fetch_ticker(symbol)
    print(f"Ticker {symbol}: last={ticker.last}")
    if hasattr(hyena, "info") and hyena.info:
        try:
            in_map = symbol in hyena.info.name_to_coin
            asset = hyena.info.coin_to_asset.get(symbol)
            sz_dec = hyena.info.asset_to_sz_decimals.get(asset) if asset is not None else None
            print(f"Symbol map: in_name_to_coin={in_map} asset={asset} sz_decimals={sz_dec}")
            meta = hyena.info.meta(dex=dex_id)
            for item in meta.get("universe", []):
                if item.get("name") == symbol:
                    print(f"Meta asset: {item}")
                    break
            if debug_state:
                state_addr = hyena._state_address()
                state = hyena.info.user_state(state_addr, dex=dex_id)
                margin = state.get("marginSummary", {})
                cross = state.get("crossMarginSummary", {})
                assets = state.get("assetPositions", [])
                spot = state.get("spotBalances", [])
                print(f"State[{_mask_addr(state_addr)}] marginSummary: {margin}")
                print(f"State[{_mask_addr(state_addr)}] crossMarginSummary: {cross}")
                print(f"State[{_mask_addr(state_addr)}] spotBalances: {spot}")
                print(f"State[{_mask_addr(state_addr)}] assetPositions: {[p.get('position', {}).get('coin') for p in assets]}")
                global_state = hyena.info.user_state(state_addr)
                g_margin = global_state.get("marginSummary", {})
                g_cross = global_state.get("crossMarginSummary", {})
                g_assets = global_state.get("assetPositions", [])
                g_spot = global_state.get("spotBalances", [])
                print(f"State[{_mask_addr(state_addr)}][global] marginSummary: {g_margin}")
                print(f"State[{_mask_addr(state_addr)}][global] crossMarginSummary: {g_cross}")
                print(f"State[{_mask_addr(state_addr)}][global] spotBalances: {g_spot}")
                print(f"State[{_mask_addr(state_addr)}][global] assetPositions: {[p.get('position', {}).get('coin') for p in g_assets]}")
                spot_state = hyena.info.spot_user_state(state_addr)
                print(f"Spot[{_mask_addr(state_addr)}] balances: {spot_state.get('balances', [])}")
                wallet_addr = hyena.wallet_address
                if wallet_addr and wallet_addr != state_addr:
                    wallet_state = hyena.info.user_state(wallet_addr, dex=dex_id)
                    w_margin = wallet_state.get("marginSummary", {})
                    w_cross = wallet_state.get("crossMarginSummary", {})
                    w_assets = wallet_state.get("assetPositions", [])
                    w_spot = wallet_state.get("spotBalances", [])
                    print(f"State[{_mask_addr(wallet_addr)}] marginSummary: {w_margin}")
                    print(f"State[{_mask_addr(wallet_addr)}] crossMarginSummary: {w_cross}")
                    print(f"State[{_mask_addr(wallet_addr)}] spotBalances: {w_spot}")
                    print(f"State[{_mask_addr(wallet_addr)}] assetPositions: {[p.get('position', {}).get('coin') for p in w_assets]}")
                    w_global = hyena.info.user_state(wallet_addr)
                    wg_margin = w_global.get("marginSummary", {})
                    wg_cross = w_global.get("crossMarginSummary", {})
                    wg_assets = w_global.get("assetPositions", [])
                    wg_spot = w_global.get("spotBalances", [])
                    print(f"State[{_mask_addr(wallet_addr)}][global] marginSummary: {wg_margin}")
                    print(f"State[{_mask_addr(wallet_addr)}][global] crossMarginSummary: {wg_cross}")
                    print(f"State[{_mask_addr(wallet_addr)}][global] spotBalances: {wg_spot}")
                    print(f"State[{_mask_addr(wallet_addr)}][global] assetPositions: {[p.get('position', {}).get('coin') for p in wg_assets]}")
                    wallet_spot = hyena.info.spot_user_state(wallet_addr)
                    print(f"Spot[{_mask_addr(wallet_addr)}] balances: {wallet_spot.get('balances', [])}")
        except Exception:
            pass

    info = markets.get(symbol)
    if info:
        raw = info.raw or {}
        is_delisted = raw.get("isDelisted")
        margin_mode = raw.get("marginMode")
        print(
            "Market info:"
            f" min_qty={info.min_qty}"
            f" qty_step={info.qty_step}"
            f" max_leverage={getattr(info, 'max_leverage', None)}"
            f" delisted={is_delisted}"
            f" margin_mode={margin_mode}"
        )
        if ticker.last > 0:
            raw_qty = test_usd / ticker.last
            adj_qty = _adjust_qty(raw_qty, info.qty_step, info.min_qty)
            print(f"Qty calc: raw={raw_qty:.6f} adjusted={adj_qty} (USD={test_usd})")

    funding = await hyena.fetch_funding_rate(symbol)
    print(f"Funding {symbol}: rate={funding.rate} next={funding.next_funding_time}")

    ok = await hyena.set_leverage(symbol, test_leverage)
    print(f"Leverage set: {ok}")

    bal = await hyena.fetch_balance()
    print(f"Balance: {bal.get('USDe')}")

    positions = await hyena.fetch_positions()
    print(f"Open positions: {len(positions)}")

    if not place_order and not run_min_tests:
        print("HYENA_PLACE_ORDER=0, skipping order placement.")
        return

    price = ticker.last
    if price <= 0:
        raise SystemExit("Price is zero; cannot place order.")

    info = markets.get(symbol)
    min_qty = info.min_qty if info else 0.0
    step = info.qty_step if info else 0.0
    qty = _adjust_qty(test_usd / price, step, min_qty)
    if qty <= 0 and not run_min_tests:
        raise SystemExit("Computed qty below min size; adjust HYENA_TEST_USD.")

    if run_min_tests:
        if not info:
            raise SystemExit("Market info missing; cannot run min qty tests.")
        below_qty = max(min_qty - step, 0.0)
        if below_qty <= 0:
            below_qty = min_qty * 0.5
        below_qty = _format_qty(below_qty, step)
        print(f"[MIN TEST] Placing below-min BUY {below_qty} {symbol}")
        try:
            bad_order = await hyena.create_order(
                symbol, OrderType.MARKET, OrderSide.BUY, below_qty
            )
            print(f"[MIN TEST] status={bad_order.status} raw={bad_order.raw}")
        except Exception as e:
            print(f"[MIN TEST] failed as expected: {e}")

        max_lev = getattr(info, "max_leverage", None)
        if max_lev:
            lev_target = max(1, int(max_lev) - 1)
            print(f"[MIN TEST] Setting leverage to {lev_target} (max={max_lev})")
            await hyena.set_leverage(symbol, lev_target)
        else:
            print("[MIN TEST] max_leverage not available; using current leverage.")

        good_qty = _format_qty(min_qty, step)
        print(f"[MIN TEST] Placing min-qty BUY {good_qty} {symbol}")
        try:
            good_order = await hyena.create_order(
                symbol, OrderType.MARKET, OrderSide.BUY, good_qty
            )
            print(f"[MIN TEST] status={good_order.status} id={good_order.id}")
            print(f"[MIN TEST] raw={good_order.raw}")
        except Exception as e:
            print(f"[MIN TEST] min-qty failed as expected: {e}")

        min_notional = getattr(hyena, "min_notional", 0.0)
        if price > 0 and min_notional > 0:
            target_qty = _ceil_step(min_notional / price, step)
            target_qty = max(target_qty, min_qty)
            print(f"[MIN TEST] Placing min-notional BUY {target_qty} {symbol}")
            try:
                good_order = await hyena.create_order(
                    symbol, OrderType.MARKET, OrderSide.BUY, target_qty
                )
                print(f"[MIN TEST] status={good_order.status} id={good_order.id}")
                print(f"[MIN TEST] raw={good_order.raw}")
            except Exception as e:
                print(f"[MIN TEST] min-notional failed: {e}")
                return

            await asyncio.sleep(6)
            positions = await hyena.fetch_positions()
            print(f"[MIN TEST] Positions after entry: {len(positions)}")
            for pos in positions:
                if pos.symbol == symbol:
                    close_qty = pos.amount
                    if close_qty > 0:
                        print(f"[MIN TEST] Closing position: {close_qty} {symbol}")
                        await hyena.create_order(
                            symbol,
                            OrderType.MARKET,
                            OrderSide.SELL,
                            close_qty,
                            params={"reduce_only": True},
                        )
                        await asyncio.sleep(2)
                        break
        return

    print(f"Placing market BUY {qty} {symbol} (~${test_usd})")
    order = await hyena.create_order(symbol, OrderType.MARKET, OrderSide.BUY, qty)
    print(f"Order status: {order.status} id={order.id}")
    print(f"Order raw: {order.raw}")

    await asyncio.sleep(6)
    positions = await hyena.fetch_positions()
    print(f"Positions after entry: {len(positions)}")

    for pos in positions:
        if pos.symbol == symbol:
            close_qty = pos.amount
            if close_qty > 0:
                print(f"Closing position: {close_qty} {symbol}")
                await hyena.create_order(
                    symbol,
                    OrderType.MARKET,
                    OrderSide.SELL,
                    close_qty,
                    params={"reduce_only": True},
                )
                await asyncio.sleep(2)
                break


if __name__ == "__main__":
    asyncio.run(main())
