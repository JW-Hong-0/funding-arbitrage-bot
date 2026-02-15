import asyncio
import logging
import math
import uuid
import time
from typing import Dict, Optional
from shared_crypto_lib.base import AbstractExchange
from shared_crypto_lib.models import OrderType, OrderSide, OrderStatus, MarketInfo
from src.config import Config
from monitoring_service import MonitoringService, StrategySignal
from asset_manager import AssetManager

logger = logging.getLogger("TradingService")

class TradingService:
    def __init__(self, grvt: AbstractExchange, hyena: AbstractExchange, variational: AbstractExchange, 
                 monitor: MonitoringService, asset_manager: AssetManager, log_manager=None):
        self.grvt = grvt
        self.hyena = hyena
        self.variational = variational
        self.monitor = monitor
        self.asset_manager = asset_manager
        self.log_manager = log_manager
        
        self.pending_tickers = {}  # { ticker: expiry_ts }
        self.active_grvt_orders: Dict[str, Dict] = {} # { ticker: { order_id, side, size, ... } }
        self.recovery_cooldowns = {} # { ticker: expiry_ts }
        self.exit_cooldowns = {} # { ticker: expiry_ts }
        self.exit_confirmations = {} # { ticker: count }
        self.is_processing = False
        self.entry_timestamps: Dict[str, float] = {}
        self._entry_skip_reasons: Dict[str, Dict[str, float]] = {}
        self._recovery_skip_reasons: Dict[str, Dict[str, float]] = {}
        self.hedge_inflight: Dict[str, Dict[str, float]] = {}
        self.exit_inflight: Dict[str, float] = {}
        self.exit_grvt_orders: Dict[str, Dict[str, float]] = {}
        self._hedge_net_samples: Dict[str, Dict[str, float]] = {}
        self._partial_since: Dict[str, float] = {}
        # Preserve the last selected entry plan per ticker (for hedge venue consistency)
        self.last_entry_plan: Dict[str, Dict] = {}
        self.paused: bool = False
        self.pause_reason: str = ""

    def set_paused(self, paused: bool, reason: str = "") -> None:
        self.paused = bool(paused)
        self.pause_reason = reason or ("manual_pause" if paused else "")

    def _log_trade(self, order, venue: str, reason: str, signal: Optional[StrategySignal] = None):
        if not self.log_manager or not order:
            return
        price = float(order.price or 0.0)
        qty = float(order.amount or 0.0)
        status = order.status.value if hasattr(order.status, "value") else str(order.status)
        order_type = order.type.value if hasattr(order.type, "value") else str(order.type)
        side = order.side.value if hasattr(order.side, "value") else str(order.side)
        strategy_id = ""
        signal_id = ""
        if signal:
            strategy_id = f"{signal.best_ask_venue}/{signal.best_bid_venue}"
            signal_id = f"{signal.ticker}:{signal.best_ask_venue}:{signal.best_bid_venue}"
        self.log_manager.log_trade(
            {
                "exchange": venue,
                "symbol": order.symbol,
                "side": side,
                "order_type": order_type,
                "price": price,
                "qty": qty,
                "notional": price * qty if price and qty else "",
                "fee": getattr(order, "fee", ""),
                "fee_asset": "",
                "order_id": order.id,
                "client_order_id": order.client_order_id,
                "status": status,
                "reason": reason,
                "strategy_id": strategy_id,
                "signal_id": signal_id,
            }
        )

    @staticmethod
    def _get_base_symbol(symbol: str) -> str:
        if not symbol:
            return ""
        s = str(symbol).upper()
        if ":" in s:
            s = s.split(":", 1)[1]
        if "_" in s:
            return s.split("_")[0]
        if "-" in s:
            return s.split("-")[0]
        return s

    def _symbol_for_venue(self, ticker: str, venue: str) -> str:
        if venue == "GRVT":
            return f"{ticker}_USDT_Perp"
        if venue == "HYNA":
            return self._hyena_symbol(ticker)
        return ticker

    def _hyena_symbol(self, ticker: str) -> str:
        if not self.hyena:
            return ticker
        base = ticker
        pref = f"{getattr(self.hyena, 'dex_id', 'hyna')}:{base}"
        if getattr(self.hyena, "markets", None):
            if pref in self.hyena.markets:
                return pref
            if base in self.hyena.markets:
                return base
        return pref

    def _order_symbol(self, order) -> str:
        if not order:
            return ""
        sym = getattr(order, "symbol", "") or ""
        if sym:
            return sym
        raw = getattr(order, "raw", None) or {}
        return raw.get("instrument") or raw.get("symbol") or ""

    def _market_info(self, ticker: str, venue: str) -> Dict:
        return self.monitor.ticker_info.get(ticker, {}).get(venue, {})

    def _hedge_inflight_active(self, ticker: str) -> bool:
        info = self.hedge_inflight.get(ticker)
        if not info:
            return False
        if time.time() >= info.get("expires", 0):
            self.hedge_inflight.pop(ticker, None)
            return False
        return True

    def _exit_inflight_active(self, ticker: str) -> bool:
        expiry = self.exit_inflight.get(ticker)
        if not expiry:
            return False
        return True

    def _is_flat_state(self, ticker: str) -> bool:
        pos = self.asset_manager.positions.get(ticker, {})
        if pos.get("GRVT_OO", 0) > 0:
            return False
        min_abs = getattr(Config, "HEDGE_MIN_QTY", 0.01)
        for venue in ("GRVT", "HYNA", "VAR"):
            qty = float(pos.get(venue, 0.0) or 0.0)
            if abs(qty) >= min_abs:
                return False
        return True

    async def _refresh_net_qty(self, ticker: str) -> float:
        try:
            await self.asset_manager.update_assets()
        except Exception as e:
            logger.warning(f"Net refresh failed for {ticker}: {e}")
        pos = self.asset_manager.positions.get(ticker, {})
        return float(pos.get("Net_Qty", 0.0))

    async def _confirm_net_reduction(self, ticker: str, pre_net: float, qty: float, checks: int, delay_s: float) -> bool:
        min_step = float(self._market_info(ticker, "GRVT").get("step_size", 0) or 0)
        min_reduction = max(min_step, qty * 0.5, 0.0)
        for _ in range(max(1, checks)):
            await asyncio.sleep(max(0.0, delay_s))
            net_now = await self._refresh_net_qty(ticker)
            if abs(net_now) <= max(0.0, abs(pre_net) - min_reduction):
                return True
        return False

    async def _execute_hedge_with_verify(
        self,
        ticker: str,
        venue: str,
        side_enum: OrderSide,
        qty: float,
        pre_net: float,
        reason: str,
    ):
        if qty <= 0:
            return None
        if self._hedge_inflight_active(ticker):
            logger.info(f"Skip hedge for {ticker}: inflight active")
            return None
        ttl_s = float(getattr(Config, "HEDGE_INFLIGHT_TTL_S", 60))
        self.hedge_inflight[ticker] = {
            "expires": time.time() + ttl_s,
            "venue": venue,
            "side": side_enum.value,
            "qty": qty,
        }

        symbol = self._symbol_for_venue(ticker, venue)
        await self._set_leverage_safe(venue, symbol, int(getattr(Config, "TARGET_LEVERAGE", 5)))
        signal = self.monitor.signals.get(ticker)

        if venue == "HYNA":
            retries = int(getattr(Config, "HEDGE_VERIFY_RETRIES", 5))
            delay_s = float(getattr(Config, "HEDGE_VERIFY_DELAY_S", 2))
            remaining, reason = self._hyena_cooldown_info()
            if remaining > 0:
                self.hedge_inflight.pop(ticker, None)
                detail = reason or f"{remaining:.0f}s"
                self._log_recovery_skip(ticker, f"hyena_cooldown:{detail}")
                return None
            try:
                order = await self.hyena.create_order(symbol, OrderType.MARKET, side_enum, qty)
            except Exception:
                self.hedge_inflight.pop(ticker, None)
                raise
            if not order:
                self.hedge_inflight.pop(ticker, None)
                logger.error(f"{ticker} hedge order failed on HYNA (None response)")
                return None
            self._log_trade(order, venue, reason, signal)
            if await self._confirm_net_reduction(ticker, pre_net, qty, retries, delay_s):
                self.hedge_inflight.pop(ticker, None)
                return order
            logger.warning(
                f"{ticker} hedge verify failed after {retries} checks; will wait for next loop"
            )
            return order

        if venue == "VAR" and self.variational:
            retries = int(getattr(Config, "HEDGE_VERIFY_RETRIES", 5))
            delay_s = float(getattr(Config, "HEDGE_VERIFY_DELAY_S", 2))
            try:
                order = await self.variational.create_order(symbol, OrderType.MARKET, side_enum, qty)
            except Exception:
                self.hedge_inflight.pop(ticker, None)
                raise
            if not order:
                self.hedge_inflight.pop(ticker, None)
                logger.error(f"{ticker} hedge order failed on VAR (None response)")
                return None
            self._log_trade(order, venue, reason, signal)
            if await self._confirm_net_reduction(ticker, pre_net, qty, retries, delay_s):
                self.hedge_inflight.pop(ticker, None)
                return order
            logger.warning(
                f"{ticker} hedge verify failed after {retries} checks; will wait for next loop"
            )
            return order

        if venue == "GRVT":
            try:
                order = await self.grvt.create_order(symbol, OrderType.MARKET, side_enum, qty)
            except Exception:
                self.hedge_inflight.pop(ticker, None)
                raise
            self._log_trade(order, venue, reason, signal)
            self.hedge_inflight.pop(ticker, None)
            return order

        return None

    def _global_next_event_ms(self, ticker: str, now_ms: Optional[int] = None) -> int:
        now_ms = int(now_ms or (time.time() * 1000))
        data = self.monitor.market_data.get(ticker, {})
        times = []
        for venue in ("GRVT", "HYNA", "VAR"):
            vdata = data.get(venue)
            if not vdata:
                continue
            t = self.monitor._estimate_next_funding_ms(vdata, now_ms)
            if t > 0:
                times.append(t)
        return min(times) if times else 0

    def _hyena_cooldown_info(self):
        remaining = 0.0
        reason = ""
        if hasattr(self.hyena, "_cooldown_remaining"):
            try:
                remaining = float(self.hyena._cooldown_remaining())
            except Exception:
                remaining = 0.0
        if hasattr(self.hyena, "_error_cooldown_reason"):
            try:
                reason = str(self.hyena._error_cooldown_reason or "")
            except Exception:
                reason = ""
        return remaining, reason

    def _log_entry_skip(self, ticker: str, signal: StrategySignal, reason: str) -> None:
        if not self.log_manager:
            return
        now = time.time()
        last = self._entry_skip_reasons.get(ticker, {})
        last_reason = last.get("reason")
        last_ts = float(last.get("ts", 0.0))
        if last_reason == reason and (now - last_ts) < 60:
            return
        self._entry_skip_reasons[ticker] = {"reason": reason, "ts": now}
        self.log_manager.log_signal(
            {
                "symbol": ticker,
                "leg_long": signal.best_ask_venue if signal else "",
                "leg_short": signal.best_bid_venue if signal else "",
                "spread": getattr(signal, "spread", ""),
                "projected_yield": getattr(signal, "projected_yield", ""),
                "decision": "skip_entry",
                "reason": reason,
            }
        )

    def _log_recovery_skip(self, ticker: str, reason: str) -> None:
        if not self.log_manager:
            return
        now = time.time()
        last = self._recovery_skip_reasons.get(ticker, {})
        last_reason = last.get("reason")
        last_ts = float(last.get("ts", 0.0))
        throttle = float(getattr(Config, "RECOVERY_SKIP_LOG_THROTTLE_S", 60))
        if last_reason == reason and (now - last_ts) < throttle:
            return
        self._recovery_skip_reasons[ticker] = {"reason": reason, "ts": now}
        self.log_manager.log_signal(
            {
                "symbol": ticker,
                "leg_long": "",
                "leg_short": "",
                "spread": "",
                "projected_yield": "",
                "decision": "skip_recovery",
                "reason": reason,
            }
        )

    def _hyena_ready_for_action(self, ticker: str, context: str) -> bool:
        remaining, reason = self._hyena_cooldown_info()
        if remaining <= 0:
            return True
        detail = reason or f"{remaining:.0f}s"
        logger.warning(f"Skip Hyena {context} for {ticker}: cooldown {detail}")
        self._log_recovery_skip(ticker, f"hyena_cooldown:{context}:{detail}")
        return False

    def _get_max_leverage(self, ticker: str, venue: str) -> float:
        try:
            if venue == "GRVT":
                m = self.grvt.markets.get(self._symbol_for_venue(ticker, venue))
            elif venue == "HYNA":
                m = self.hyena.markets.get(self._hyena_symbol(ticker))
            elif venue == "VAR":
                m = self.variational.markets.get(ticker) if self.variational else None
            else:
                m = None
            max_lev = getattr(m, "max_leverage", None) if m else None
            if max_lev:
                return float(max_lev)
        except Exception:
            pass
        return float(getattr(Config, "TARGET_LEVERAGE", 5))

    async def _set_leverage_safe(self, venue: str, symbol: str, leverage: int):
        try:
            if venue == "GRVT":
                await self.grvt.set_leverage(symbol, leverage)
            elif venue == "HYNA":
                await self.hyena.set_leverage(symbol, leverage)
            elif venue == "VAR" and self.variational:
                await self.variational.set_leverage(symbol, leverage)
        except Exception as e:
            logger.warning(f"Set leverage failed ({venue} {symbol}): {e}")

    def _compute_grvt_maker_price(self, ticker: str, side: OrderSide, fallback_price: float) -> float:
        data = self.monitor.market_data.get(ticker, {}).get("GRVT", {})
        bid = float(data.get("bid") or 0)
        ask = float(data.get("ask") or 0)
        bps = float(getattr(Config, "GRVT_MAKER_PRICE_BPS", 0) or 0)
        offset = bps / 10000.0
        if side == OrderSide.BUY:
            base = bid if bid > 0 else fallback_price
            price = base * (1 - offset)
        else:
            base = ask if ask > 0 else fallback_price
            price = base * (1 + offset)
        info = self._market_info(ticker, "GRVT")
        tick = float(info.get("price_tick") or 0)
        return self._round_price_to_tick(price, tick, side)

    @staticmethod
    def _round_price_to_tick(price: float, tick: float, side: OrderSide) -> float:
        if price <= 0 or tick <= 0:
            return price
        inv = 1.0 / tick
        if side == OrderSide.BUY:
            return math.floor(price * inv) / inv
        return math.ceil(price * inv) / inv

    def _compute_grvt_exit_price(
        self, ticker: str, side: OrderSide, base_price: float, extra_bps: float = 0.0
    ) -> float:
        price = self._compute_grvt_maker_price(ticker, side, base_price)
        if extra_bps > 0:
            offset = extra_bps / 10000.0
            if side == OrderSide.SELL:
                price *= (1 - offset)
            else:
                price *= (1 + offset)
            info = self._market_info(ticker, "GRVT")
            tick = float(info.get("price_tick") or 0)
            price = self._round_price_to_tick(price, tick, side)
        return price

    def _grvt_positions_map(self) -> Dict[str, float]:
        positions = {}
        for p in self.asset_manager.raw_positions.get("GRVT", []):
            base = self._get_base_symbol(p.symbol)
            qty = float(p.amount)
            signed = -abs(qty) if p.side == OrderSide.SELL else abs(qty)
            positions[base] = positions.get(base, 0.0) + signed
        return positions

    def _grvt_open_orders_map(self) -> Dict[str, list]:
        orders_by_ticker = {}
        for o in self.asset_manager.raw_orders.get("GRVT", []):
            base = self._get_base_symbol(o.symbol)
            orders_by_ticker.setdefault(base, []).append(o)
        return orders_by_ticker

    def _spawn_tick_task(self, tasks: list, coro, label: str):
        async def _runner():
            try:
                return await coro
            except Exception as e:
                logger.error(f"Tick task failed ({label}): {e}")
                return None
        tasks.append(asyncio.create_task(_runner()))

    def _get_last_entry_plan(self, ticker: str) -> Optional[Dict]:
        plan = self.last_entry_plan.get(ticker)
        if not plan:
            return None
        ttl_s = float(getattr(Config, "ENTRY_PLAN_TTL_S", 180))
        if ttl_s > 0:
            ts = float(plan.get("ts", 0.0))
            if ts and (time.time() - ts) > ttl_s:
                return None
        return plan

    def _can_hedge_on_venue(self, ticker: str, venue: str, qty: float) -> bool:
        if qty <= 0:
            return False
        data = self.monitor.market_data.get(ticker, {}).get(venue, {})
        price = float(data.get("price") or 0)
        if price <= 0:
            return False
        lev = min(float(getattr(Config, "TARGET_LEVERAGE", 5)), self._get_max_leverage(ticker, venue))
        required = self._required_margin_usd(price, qty, lev)
        avail = float(self.asset_manager.balances.get(venue, {}).get("available", 0.0))
        return avail >= required

    async def _hedge_grvt_fill(
        self,
        ticker: str,
        hedge_venue: str,
        hedge_side: OrderSide,
        qty: float,
        tasks: Optional[list] = None,
    ):
        if qty <= 0:
            return
        if not self._can_hedge_on_venue(ticker, hedge_venue, qty):
            logger.warning(f"{ticker} hedge venue {hedge_venue} not viable (balance/size); attempting fallback")
            if hedge_venue in ("HYNA", "VAR"):
                alt_venue = "VAR" if hedge_venue == "HYNA" else "HYNA"
                if (
                    (alt_venue == "HYNA" and self.hyena)
                    or (alt_venue == "VAR" and self.variational)
                ):
                    if self._can_hedge_on_venue(ticker, alt_venue, qty):
                        hedge_venue = alt_venue
                        logger.warning(f"{ticker} hedge venue fallback -> {hedge_venue}")
                    else:
                        logger.warning(f"{ticker} hedge fallback {alt_venue} not viable; skipping hedge")
                        return
                else:
                    logger.warning(f"{ticker} hedge fallback {alt_venue} unavailable; skipping hedge")
                    return
            else:
                logger.warning(f"{ticker} hedge venue {hedge_venue} not viable; skipping hedge")
                return
        pre_net = await self._refresh_net_qty(ticker)
        if tasks is None:
            await self._execute_hedge_with_verify(
                ticker, hedge_venue, hedge_side, qty, pre_net, "hedge_fill"
            )
        else:
            self._spawn_tick_task(
                tasks,
                self._execute_hedge_with_verify(
                    ticker, hedge_venue, hedge_side, qty, pre_net, "hedge_fill"
                ),
                f"hedge_fill:{ticker}",
            )

    async def _close_grvt_partial(self, ticker: str, order_side: OrderSide, qty: float):
        if qty <= 0:
            return
        qty = await self._adjust_qty_to_rules(ticker, "GRVT", qty)
        if qty <= 0:
            logger.warning(f"   -> GRVT partial close skipped (min qty) {ticker}")
            return
        symbol = self._symbol_for_venue(ticker, "GRVT")
        side = OrderSide.SELL if order_side == OrderSide.BUY else OrderSide.BUY
        logger.warning(f"   -> Closing GRVT partial {ticker}: {side.value.upper()} {qty}")
        try:
            await self._set_leverage_safe("GRVT", symbol, int(getattr(Config, "TARGET_LEVERAGE", 5)))
            order = await self.grvt.create_order(symbol, OrderType.MARKET, side, qty)
            self._log_trade(order, "GRVT", "partial_close")
        except Exception as e:
            logger.warning(f"   -> GRVT partial close failed: {e}")

    def _infer_hedge_from_signal(self, ticker: str, order_side: OrderSide) -> Dict[str, Optional[str]]:
        plan = self._get_last_entry_plan(ticker)
        if plan and plan.get("hedge_venue"):
            return {
                "venue": plan.get("hedge_venue"),
                "side": plan.get("hedge_side"),
            }
        signal = self.monitor.signals.get(ticker)
        if signal and ("GRVT" in [signal.best_ask_venue, signal.best_bid_venue]):
            long_venue = signal.best_ask_venue
            short_venue = signal.best_bid_venue
            hedge_venue = short_venue if long_venue == "GRVT" else long_venue
            hedge_side = OrderSide.SELL if long_venue == "GRVT" else OrderSide.BUY
            return {"venue": hedge_venue, "side": hedge_side}
        # Fallback hedge venue
        hedge_venue = "HYNA" if self.hyena else ("VAR" if self.variational else "GRVT")
        hedge_side = OrderSide.SELL if order_side == OrderSide.BUY else OrderSide.BUY
        return {"venue": hedge_venue, "side": hedge_side}

    def _is_grvt_order_rejected(self, order) -> bool:
        raw = getattr(order, "raw", None)
        if not raw or not isinstance(raw, dict):
            return False
        if raw.get("error") or raw.get("success") is False:
            return True
        code = raw.get("code") or raw.get("errorCode")
        if code and str(code) not in ("0", "200"):
            if raw.get("result") is None:
                return True
        result = raw.get("result") if isinstance(raw.get("result"), dict) else None
        status = None
        if isinstance(result, dict):
            state = result.get("state")
            if isinstance(state, dict):
                status = state.get("status")
            if not status:
                status = result.get("status")
        if status and str(status).upper() in {"REJECTED", "FAILED", "CANCELED", "CANCELLED", "EXPIRED"}:
            return True
        return False

    async def _track_grvt_order(self, ticker: str, order, order_side: OrderSide, hedge_override: Optional[Dict] = None):
        positions = self._grvt_positions_map()
        initial_pos = positions.get(ticker, 0.0)
        hedge_info = hedge_override or self._infer_hedge_from_signal(ticker, order_side)
        self.active_grvt_orders[ticker] = {
            "order_id": order.id,
            "client_order_id": getattr(order, "client_order_id", None),
            "side": order_side,
            "size": float(order.amount),
            "price": float(order.price or 0),
            "created_ts": time.time(),
            "initial_pos": initial_pos,
            "hedged_qty": 0.0,
            "hedge_venue": hedge_info["venue"],
            "hedge_side": hedge_info["side"],
            "first_fill_ts": None,
        }

    async def _monitor_grvt_orders(self, tasks: Optional[list] = None):
        if not self.monitor.tickers:
            return
        open_orders_map = self._grvt_open_orders_map()
        pos_map = self._grvt_positions_map()
        now = time.time()

        # Adopt untracked open orders
        for ticker, orders in open_orders_map.items():
            if ticker not in self.active_grvt_orders and orders:
                order = orders[0]
                order_side = order.side
                logger.warning(f"⚠️ Untracked GRVT order detected for {ticker}. Adopting {order.id}.")
                await self._track_grvt_order(ticker, order, order_side)

        # Monitor tracked orders
        for ticker, meta in list(self.active_grvt_orders.items()):
            ttl_s = float(getattr(Config, "GRVT_ORDER_TTL_S", 60))
            age = now - meta.get("created_ts", now)
            grvt_sym = f"{ticker}_USDT_Perp"

            open_orders = open_orders_map.get(ticker, [])
            open_ids = {str(o.id) for o in open_orders}
            open_ids.update({str(getattr(o, "client_order_id", "")) for o in open_orders})
            order_open = str(meta.get("order_id")) in open_ids or str(meta.get("client_order_id")) in open_ids
            if order_open and not meta.get("open_seen"):
                meta["open_seen"] = True

            current_pos = pos_map.get(ticker, 0.0)
            delta = current_pos - meta.get("initial_pos", 0.0)
            order_side = meta.get("side")
            filled_qty = delta if order_side == OrderSide.BUY else -delta
            if filled_qty < 0:
                filled_qty = 0.0

            step_size = float(self._market_info(ticker, "GRVT").get("step_size", 0) or 0)
            min_fill = max(step_size, float(meta.get("size", 0)) * 0.01, 0.0)
            size = float(meta.get("size", 0))
            full_tolerance = max(step_size, 1e-9)
            is_full_fill = filled_qty + full_tolerance >= size if size > 0 else False
            if filled_qty > 0 and not meta.get("first_fill_ts"):
                meta["first_fill_ts"] = now
            hedge_needed = filled_qty - float(meta.get("hedged_qty", 0))
            confirm_s = float(getattr(Config, "GRVT_FILL_CONFIRM_S", 3) or 0)
            if not meta.get("open_seen") and not order_open and age < confirm_s:
                # Avoid hedging on transient/stale position snapshots right after order placement.
                pass
            elif is_full_fill and hedge_needed >= min_fill:
                hedge_venue = meta.get("hedge_venue")
                hedge_side = meta.get("hedge_side")
                if hedge_venue and hedge_side:
                    logger.info(f"-> Hedging {ticker} {hedge_side} {hedge_needed} on {hedge_venue}")
                    await self._hedge_grvt_fill(ticker, hedge_venue, hedge_side, hedge_needed, tasks)
                    meta["hedged_qty"] = float(meta.get("hedged_qty", 0)) + hedge_needed

            last_log = float(meta.get("last_log_ts", 0))
            if now - last_log >= 5:
                status = "OPEN" if order_open else "CLOSED"
                logger.info(
                    f"[GRVT] {ticker} {status} age={age:.0f}s "
                    f"filled={filled_qty:.6f} hedged={meta.get('hedged_qty', 0):.6f} "
                    f"ttl={ttl_s:.0f}s"
                )
                meta["last_log_ts"] = now

            if order_open:
                if filled_qty <= 0 and age >= ttl_s:
                    try:
                        logger.warning(f"⏳ GRVT order TTL reached. Canceling {meta.get('order_id')}")
                        await self.grvt.cancel_order(meta.get("order_id"), grvt_sym)
                    except Exception as e:
                        logger.error(f"GRVT cancel failed: {e}")
                    order_open = False
                elif filled_qty > 0:
                    grace_s = float(getattr(Config, "GRVT_PARTIAL_FILL_GRACE_S", 600))
                    first_fill_ts = float(meta.get("first_fill_ts") or now)
                    if (now - first_fill_ts) >= grace_s and not is_full_fill:
                        logger.warning(
                            f"⏳ GRVT partial fill grace exceeded. Canceling {meta.get('order_id')} and closing partial."
                        )
                        try:
                            await self.grvt.cancel_order(meta.get("order_id"), grvt_sym)
                        except Exception as e:
                            logger.error(f"GRVT cancel failed: {e}")
                        await self._close_grvt_partial(ticker, order_side, filled_qty)
                        self.pending_tickers[ticker] = time.time() + 5
                        self.active_grvt_orders.pop(ticker, None)
                        continue

            if not order_open and not meta.get("open_seen") and age < confirm_s:
                # Keep tracking briefly; avoid premature close/cleanup before the order is visible.
                continue
            if not order_open:
                # Final hedge if fully filled; otherwise close partial exposure.
                if is_full_fill and filled_qty > float(meta.get("hedged_qty", 0)) + min_fill:
                    hedge_venue = meta.get("hedge_venue")
                    hedge_side = meta.get("hedge_side")
                    hedge_needed = filled_qty - float(meta.get("hedged_qty", 0))
                    if hedge_venue and hedge_side:
                        logger.info(f"-> Final hedge {ticker} {hedge_side} {hedge_needed} on {hedge_venue}")
                        await self._hedge_grvt_fill(ticker, hedge_venue, hedge_side, hedge_needed)
                elif filled_qty > 0 and not is_full_fill:
                    await self._close_grvt_partial(ticker, order_side, filled_qty)
                self.pending_tickers[ticker] = time.time() + 5
                self.active_grvt_orders.pop(ticker, None)

    async def _monitor_exit_orders(self):
        if not self.exit_grvt_orders:
            return
        open_orders_map = self._grvt_open_orders_map()
        pos_map = self._grvt_positions_map()
        now = time.time()
        reprice_after = float(getattr(Config, "EXIT_REPRICE_AFTER_S", 600))
        extra_bps = float(getattr(Config, "EXIT_REPRICE_BPS", 0) or 0)
        min_abs = float(getattr(Config, "HEDGE_MIN_QTY", 0.01))

        for ticker, meta in list(self.exit_grvt_orders.items()):
            grvt_sym = f"{ticker}_USDT_Perp"
            open_orders = open_orders_map.get(ticker, [])
            open_ids = {str(o.id) for o in open_orders}
            open_ids.update({str(getattr(o, "client_order_id", "")) for o in open_orders})
            is_open = str(meta.get("order_id")) in open_ids or str(meta.get("client_order_id")) in open_ids
            age = now - float(meta.get("created_ts", now))

            if is_open and reprice_after > 0 and age >= reprice_after:
                try:
                    await self.grvt.cancel_order(meta.get("order_id"), grvt_sym)
                except Exception:
                    pass
                base_price = float(self.monitor.market_data.get(ticker, {}).get("GRVT", {}).get("price") or 0)
                if base_price <= 0:
                    continue
                side = meta.get("side")
                price = self._compute_grvt_exit_price(ticker, side, base_price, extra_bps)
                order = await self.grvt.create_order(
                    grvt_sym,
                    OrderType.LIMIT,
                    side,
                    float(meta.get("size", 0)),
                    price,
                    params={"reduce_only": True},
                )
                self._log_trade(order, "GRVT", "exit_reprice")
                meta["order_id"] = order.id
                meta["client_order_id"] = getattr(order, "client_order_id", None)
                meta["created_ts"] = now
                meta["reprice_count"] = int(meta.get("reprice_count", 0)) + 1
                continue

            if is_open:
                continue

            current_size = float(pos_map.get(ticker, 0.0))
            if abs(current_size) > min_abs:
                base_price = float(self.monitor.market_data.get(ticker, {}).get("GRVT", {}).get("price") or 0)
                if base_price <= 0:
                    continue
                side = meta.get("side")
                price = self._compute_grvt_exit_price(ticker, side, base_price, extra_bps if age >= reprice_after else 0.0)
                order = await self.grvt.create_order(
                    grvt_sym,
                    OrderType.LIMIT,
                    side,
                    float(meta.get("size", 0)),
                    price,
                    params={"reduce_only": True},
                )
                self._log_trade(order, "GRVT", "exit_replace")
                meta["order_id"] = order.id
                meta["client_order_id"] = getattr(order, "client_order_id", None)
                meta["created_ts"] = now
                continue

            # GRVT flat -> close remaining hedge legs (use live positions, not cached sizes)
            live_pos = self.asset_manager.positions.get(ticker, {})
            lght_size = float(live_pos.get("HYNA", 0.0))
            var_size = float(live_pos.get("VAR", 0.0))
            hedge_done = True
            retry_reasons = []

            if abs(lght_size) > min_abs and self.hyena:
                if not self._hyena_ready_for_action(ticker, "exit_hedge"):
                    hedge_done = False
                    retry_reasons.append("hyena_cooldown")
                else:
                    h_side = OrderSide.BUY if lght_size < 0 else OrderSide.SELL
                    sym = f"{ticker}-USDT"
                    try:
                        order = await self.hyena.create_order(sym, OrderType.MARKET, h_side, abs(lght_size))
                        self._log_trade(order, "HYNA", "exit_hedge")
                    except Exception as e:
                        hedge_done = False
                        retry_reasons.append(f"hyena_error:{e}")
                        logger.error("Exit hedge failed (HYNA) %s %s qty=%s err=%s", ticker, h_side, abs(lght_size), e)

            if abs(var_size) > min_abs and self.variational:
                h_side = OrderSide.BUY if var_size < 0 else OrderSide.SELL
                try:
                    order = await self.variational.create_order(ticker, OrderType.MARKET, h_side, abs(var_size))
                    self._log_trade(order, "VAR", "exit_hedge")
                except Exception as e:
                    hedge_done = False
                    retry_reasons.append(f"variational_error:{e}")
                    logger.error("Exit hedge failed (VAR) %s %s qty=%s err=%s", ticker, h_side, abs(var_size), e)

            if not hedge_done:
                meta["hedge_retry_count"] = int(meta.get("hedge_retry_count", 0)) + 1
                meta["hedge_retry_ts"] = time.time()
                meta["hedge_retry_reason"] = ",".join(retry_reasons) if retry_reasons else "unknown"
                logger.warning(
                    "%s exit hedge pending; will retry. retry_count=%s reason=%s",
                    ticker,
                    meta.get("hedge_retry_count", 0),
                    meta.get("hedge_retry_reason", ""),
                )
                continue
            self.exit_grvt_orders.pop(ticker, None)
            cooldown = float(getattr(Config, "EXIT_COMPLETE_COOLDOWN_S", 90))
            self.exit_cooldowns[ticker] = time.time() + cooldown
        
    async def process_tick(self):
        """Process one trading cycle"""
        if self.paused:
            return
        tick_tasks: list = []
        try:
            if not self.is_processing:
                # 1. Reconcile / Recover State first
                await self._reconcile_state(tick_tasks)
                self._refresh_entry_timestamps()
                
                # 2. Process New Entries (if healthy)
            await self._process_opportunities()
                
            # 3. Process Exits (if needed)
            await self._process_exits()
            if tick_tasks:
                await asyncio.gather(*tick_tasks)
        except Exception as e:
            logger.error(f"Trading Tick Error: {e}")

    def _refresh_entry_timestamps(self):
        for ticker, pos in self.asset_manager.positions.items():
            state = pos.get("State", "IDLE")
            if state == "HEDGED":
                self.entry_timestamps.setdefault(ticker, time.time())
            elif state in ("IDLE", "OPEN_ORDERS"):
                self.entry_timestamps.pop(ticker, None)

    def _required_margin_usd(self, price: float, qty: float, leverage: float) -> float:
        if price <= 0 or qty <= 0:
            return 0.0
        lev = max(float(leverage or 1), 1.0)
        buffer = float(getattr(Config, "ENTRY_MARGIN_BUFFER", 0.05))
        return (price * qty) / lev * (1 + buffer)

    def _order_error_text(self, err) -> str:
        if isinstance(err, dict):
            parts = []
            for key in ("message", "error", "reason", "detail"):
                val = err.get(key)
                if val:
                    parts.append(str(val))
            code = err.get("code") or err.get("errorCode")
            if code:
                parts.append(f"code={code}")
            return " ".join(parts) if parts else str(err)
        return str(err)

    def _classify_order_error(self, err) -> str:
        text = self._order_error_text(err).lower()
        if any(k in text for k in ("insufficient", "insuff", "margin", "balance", "not enough", "collateral")):
            return "insufficient_margin"
        if any(k in text for k in ("401", "unauthorized", "auth", "token", "signature")):
            return "auth"
        if any(k in text for k in ("nonce", "sequence", "invalid nonce")):
            return "nonce"
        if any(k in text for k in ("429", "rate limit", "too many requests")):
            return "rate_limit"
        if any(k in text for k in ("timeout", "timed out", "connection", "network")):
            return "network"
        return "unknown"

    def _entry_failure_cooldown_s(self, err) -> float:
        reason = self._classify_order_error(err)
        if reason == "insufficient_margin":
            return float(getattr(Config, "ENTRY_FAIL_COOLDOWN_INSUFFICIENT_S", 120) or 120)
        if reason == "auth":
            return float(getattr(Config, "ENTRY_FAIL_COOLDOWN_AUTH_S", 30) or 30)
        if reason == "rate_limit":
            return float(getattr(Config, "ENTRY_FAIL_COOLDOWN_RATE_S", 30) or 30)
        if reason == "nonce":
            return float(getattr(Config, "ENTRY_FAIL_COOLDOWN_NONCE_S", 30) or 30)
        if reason == "network":
            return float(getattr(Config, "ENTRY_FAIL_COOLDOWN_NETWORK_S", 10) or 10)
        return float(getattr(Config, "ENTRY_FAIL_COOLDOWN_UNKNOWN_S", 30) or 30)

    def _apply_entry_failure_cooldown(self, ticker: str, venue: str, err, context: str) -> None:
        reason = self._classify_order_error(err)
        cooldown = self._entry_failure_cooldown_s(err)
        detail = self._order_error_text(err)
        self.pending_tickers[ticker] = time.time() + cooldown
        logger.warning(
            f"Entry failure cooldown {ticker} venue={venue} reason={reason} cooldown={cooldown:.0f}s context={context} detail={detail}"
        )

    def _rank_entry_pairs(self, ticker: str) -> list:
        data = self.monitor.market_data.get(ticker, {})
        venues = [v for v in ("GRVT", "HYNA", "VAR") if data.get(v) and data[v].get("price", 0) > 0]
        if len(venues) < 2:
            return []
        ranked = []
        now_ms = int(time.time() * 1000)
        next_event_ms = self._global_next_event_ms(ticker, now_ms)
        for l_name in venues:
            for s_name in venues:
                if l_name == s_name:
                    continue
                l_data = data[l_name]
                s_data = data[s_name]
                p_long = float(l_data.get("price") or 0)
                p_short = float(s_data.get("price") or 0)
                if p_long <= 0 or p_short <= 0:
                    continue
                entry_spread_pct = (p_short - p_long) / p_long
                projected_yield = self.monitor._projected_yield_for_pair(
                    l_data,
                    s_data,
                    now_ms=now_ms,
                    next_event_ms=next_event_ms,
                )
                base_score = entry_spread_pct + projected_yield
                priority_bonus = 0.0005 if "GRVT" in (l_name, s_name) else 0.0
                total_score = base_score + priority_bonus
                is_opportunity = projected_yield > 0.00005 or entry_spread_pct > 0.002
                ranked.append(
                    {
                        "long": l_name,
                        "short": s_name,
                        "score": total_score,
                        "entry_spread_pct": entry_spread_pct,
                        "projected_yield": projected_yield,
                        "is_opportunity": is_opportunity,
                    }
                )
        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked

    async def _prepare_entry(self, ticker: str, long_venue: str, short_venue: str) -> Optional[Dict]:
        data = self.monitor.market_data.get(ticker, {})
        l_data = data.get(long_venue)
        s_data = data.get(short_venue)
        if not l_data or not s_data:
            return None
        p_long = float(l_data.get("price") or 0)
        p_short = float(s_data.get("price") or 0)
        if p_long <= 0 or p_short <= 0:
            return None

        base_usd = 50.0
        target_lev = float(getattr(Config, "TARGET_LEVERAGE", 5))
        lev_long = self._get_max_leverage(ticker, long_venue)
        lev_short = self._get_max_leverage(ticker, short_venue)
        lev_cap = min(lev_long, lev_short, target_lev)
        scaling_factor = lev_cap / target_lev
        trade_size_usd = base_usd * scaling_factor

        rules_long = await self._get_effective_rules(ticker, long_venue)
        rules_short = await self._get_effective_rules(ticker, short_venue)
        size = self.calculate_common_quantity(trade_size_usd, p_long, p_short, rules_long, rules_short)
        if size <= 0:
            return None

        now_ms = int(time.time() * 1000)
        entry_spread_pct = (p_short - p_long) / p_long
        projected_yield = self.monitor._projected_yield_for_pair(
            l_data,
            s_data,
            now_ms=now_ms,
            next_event_ms=self._global_next_event_ms(ticker, now_ms),
        )

        is_grvt_maker = long_venue == "GRVT" or short_venue == "GRVT"
        if not is_grvt_maker:
            req_long = self._required_margin_usd(p_long, size, lev_cap)
            req_short = self._required_margin_usd(p_short, size, lev_cap)
            avail_long = float(self.asset_manager.balances.get(long_venue, {}).get("available", 0.0))
            avail_short = float(self.asset_manager.balances.get(short_venue, {}).get("available", 0.0))
            if avail_long < req_long or avail_short < req_short:
                return None
            return {
                "long_venue": long_venue,
                "short_venue": short_venue,
                "size": size,
                "is_grvt_maker": False,
                "lev_long": lev_cap,
                "lev_short": lev_cap,
                "sym_long": self._symbol_for_venue(ticker, long_venue),
                "sym_short": self._symbol_for_venue(ticker, short_venue),
                "entry_spread_pct": entry_spread_pct,
                "projected_yield": projected_yield,
            }

        target_side = OrderSide.BUY if long_venue == "GRVT" else OrderSide.SELL
        target_price = p_long if long_venue == "GRVT" else p_short
        target_price = self._compute_grvt_maker_price(ticker, target_side, target_price)

        hedge_venue = short_venue if long_venue == "GRVT" else long_venue
        grvt_lev = min(target_lev, self._get_max_leverage(ticker, "GRVT"))
        hedge_lev = min(target_lev, self._get_max_leverage(ticker, hedge_venue))
        req_grvt = self._required_margin_usd(target_price, size, grvt_lev)
        hedge_price = float(data.get(hedge_venue, {}).get("price") or 0)
        req_hedge = self._required_margin_usd(hedge_price, size, hedge_lev)
        avail_grvt = float(self.asset_manager.balances.get("GRVT", {}).get("available", 0.0))
        avail_hedge = float(self.asset_manager.balances.get(hedge_venue, {}).get("available", 0.0))
        if avail_grvt < req_grvt or avail_hedge < req_hedge:
            return None

        return {
            "long_venue": long_venue,
            "short_venue": short_venue,
            "size": size,
            "is_grvt_maker": True,
            "target_side": target_side,
            "target_price": target_price,
            "hedge_venue": hedge_venue,
            "grvt_lev": grvt_lev,
            "hedge_lev": hedge_lev,
            "sym_long": self._symbol_for_venue(ticker, long_venue),
            "sym_short": self._symbol_for_venue(ticker, short_venue),
            "entry_spread_pct": entry_spread_pct,
            "projected_yield": projected_yield,
        }

    async def _get_market_rules(self, ticker: str, venue: str) -> Dict:
        symbol = self._symbol_for_venue(ticker, venue)
        ex = None
        if venue == "GRVT":
            ex = self.grvt
        elif venue == "HYNA":
            ex = self.hyena
        elif venue == "VAR":
            ex = self.variational
        if not ex:
            return {}

        market = getattr(ex, "markets", {}).get(symbol)
        min_qty = getattr(market, "min_qty", 0.0) if market else 0.0
        step_size = getattr(market, "qty_step", 0.0) if market else 0.0
        min_notional = getattr(market, "min_notional", 0.0) if market else 0.0

        if (
            (not market or min_qty <= 0 or step_size <= 0)
            and hasattr(ex, "refresh_market_info")
        ):
            try:
                refreshed = await ex.refresh_market_info(ticker)
                if refreshed:
                    market = refreshed
                    min_qty = getattr(market, "min_qty", min_qty)
                    step_size = getattr(market, "qty_step", step_size)
                    min_notional = getattr(market, "min_notional", min_notional)
            except Exception:
                pass

        if not market:
            return {}
        return {
            "min_qty": min_qty,
            "step_size": step_size,
            "min_notional": min_notional,
        }

    async def _get_effective_rules(self, ticker: str, venue: str) -> Dict:
        rules = await self._get_market_rules(ticker, venue)
        if rules and any((rules.get("min_qty"), rules.get("step_size"), rules.get("min_notional"))):
            return rules
        fallback = self.monitor.ticker_info.get(ticker, {}).get(venue, {}) if self.monitor else {}
        if not fallback:
            return rules
        return {
            "min_qty": float(fallback.get("min_qty") or 0.0),
            "step_size": float(fallback.get("step_size") or 0.0),
            "min_notional": float(fallback.get("min_notional") or 0.0),
        }

    async def _adjust_qty_to_rules(self, ticker: str, venue: str, qty: float) -> float:
        info = await self._get_market_rules(ticker, venue)
        min_qty = float(info.get("min_qty") or 0.0)
        step = float(info.get("step_size") or 0.0)
        if qty <= 0:
            return 0.0
        if step > 0:
            inv = 1.0 / step
            qty = math.floor(qty * inv) / inv
        if min_qty > 0 and qty < min_qty:
            return 0.0
        return qty

    async def _reconcile_state(self, tasks: Optional[list] = None):
        """
        Check AssetManager for PARTIAL_HEDGE or OPEN_ORDERS states and fix them.
        """
        await self._monitor_grvt_orders(tasks)
        await self._monitor_exit_orders()
        # Clear exit-inflight when flat or TTL expired
        for ticker in list(self.exit_inflight.keys()):
            if self._is_flat_state(ticker):
                self.exit_inflight.pop(ticker, None)
            else:
                ttl = float(getattr(Config, "EXIT_INFLIGHT_TTL_S", 180))
                expiry = self.exit_inflight.get(ticker, 0) or 0
                if ttl > 0 and time.time() >= expiry:
                    logger.warning(
                        f"{ticker} exit inflight TTL exceeded; waiting for flat before re-entry"
                    )
                    # keep blocking re-entry until flat
        for ticker, pos in self.asset_manager.positions.items():
            state = pos.get('State', 'IDLE')
            if state != 'PARTIAL_HEDGE':
                self._partial_since.pop(ticker, None)
            
            # CASE 1: PARTIAL HEDGE (Qty Mismatch) -> Auto Hedge
            if state == 'PARTIAL_HEDGE':
                now = time.time()
                if ticker not in self._partial_since:
                    self._partial_since[ticker] = now
                else:
                    watchdog_s = float(getattr(Config, "PARTIAL_WATCHDOG_S", 0))
                    age = now - float(self._partial_since.get(ticker, now))
                    if watchdog_s > 0 and age >= watchdog_s:
                        cleared = []
                        if self._hedge_inflight_active(ticker):
                            self.hedge_inflight.pop(ticker, None)
                            cleared.append("hedge_inflight")
                        if getattr(Config, "PARTIAL_WATCHDOG_CLEAR_RECOVERY_COOLDOWN", True):
                            if ticker in self.recovery_cooldowns:
                                self.recovery_cooldowns.pop(ticker, None)
                                cleared.append("recovery_cooldown")
                        if getattr(Config, "PARTIAL_WATCHDOG_CLEAR_PENDING", True):
                            if ticker in self.pending_tickers:
                                self.pending_tickers.pop(ticker, None)
                                cleared.append("pending_ticker")
                        if getattr(Config, "PARTIAL_WATCHDOG_CLEAR_EXIT", True):
                            expiry = self.exit_inflight.get(ticker)
                            if expiry and time.time() >= expiry:
                                self.exit_inflight.pop(ticker, None)
                                cleared.append("exit_inflight")
                        if cleared:
                            logger.warning(
                                f"{ticker} partial hedge watchdog cleared {','.join(cleared)} after {age:.0f}s"
                            )
                            self._partial_since[ticker] = now
                if self._exit_inflight_active(ticker):
                    # Avoid compounding during exit; wait for flat or TTL expiry
                    self._log_recovery_skip(ticker, "exit_inflight")
                    continue
                exit_cd = self.exit_cooldowns.get(ticker, 0)
                if exit_cd and time.time() < exit_cd:
                    remaining = exit_cd - time.time()
                    self._log_recovery_skip(ticker, f"exit_cooldown:{remaining:.0f}s")
                    continue
                if ticker in self.active_grvt_orders:
                    # If GRVT maker is pending, never hedge on other venues until it's visible.
                    meta = self.active_grvt_orders.get(ticker)
                    confirm_s = float(getattr(Config, "GRVT_FILL_CONFIRM_S", 3) or 0)
                    age = time.time() - float(meta.get("created_ts", time.time())) if meta else 0.0
                    if meta and not meta.get("open_seen") and age < confirm_s:
                        self._log_recovery_skip(ticker, f"grvt_open_pending:{max(0.0, confirm_s - age):.1f}s")
                        continue
                    self._log_recovery_skip(ticker, "grvt_order_active")
                    continue
                if ticker in self.pending_tickers:
                    remaining = self.pending_tickers.get(ticker, 0) - time.time()
                    self._log_recovery_skip(ticker, f"pending:{max(0.0, remaining):.0f}s")
                    continue
                net_qty = pos.get('Net_Qty', 0)
                # If negative net (Short Heavy), we need to BUY on the 'Deficit' venue?
                # or just Close the excess?
                # Simple logic: Hedge the Net on the 'Other' venue or Close partial?
                # BETTER: Market Close the excess to reach 0 net. 
                # OR: Market Open to reach neutral?
                # Strategy: We want neutral delta. 
                # If Net > 0 (Long Heavy), we need to SELL 'Net' amount.
                # Where? On the venue that is 'Light'? Or the one that is 'Heavy'?
                # AssetManager doesn't say which is heavy easily without logic.
                # Let's use generic "Hedge on Hyena/Variational" if GRVT is the heavy one.
                
                logger.warning(f"🚨 {ticker} Partial Hedge Detected (Net: {net_qty}). Recovery Protocol Initiated.")

                sample = self._hedge_net_samples.get(ticker)
                if not sample or abs(sample.get("net", 0) - net_qty) > 1e-9:
                    self._hedge_net_samples[ticker] = {"net": net_qty, "ts": now}
                    self._log_recovery_skip(ticker, "net_sample_reset")
                    continue
                stable_s = float(getattr(Config, "HEDGE_NET_STABLE_S", 3))
                if now - sample.get("ts", now) < stable_s:
                    remain = stable_s - (now - sample.get("ts", now))
                    self._log_recovery_skip(ticker, f"net_unstable:{max(0.0, remain):.1f}s")
                    continue

                if self._hedge_inflight_active(ticker):
                    self._log_recovery_skip(ticker, "hedge_inflight")
                    continue
                
                # Determine which venue to trade to neutralize 'net_qty'
                # If Net > 0 (Too Long), Sell on Taker Venue (Hyena/Var).
                # If Net < 0 (Too Short), Buy on Taker Venue.
                
                side = 'sell' if net_qty > 0 else 'buy'
                abs_qty = abs(net_qty)

                # Where to execute? 
                # Check where we have existing positions.
                # If GRVT has position, maybe hedge on HYNA/VAR.
                # Default to HYNA for now or check signals?
                # Safety: Just Log for now? Or execute?
                # User complaint: AVNT GRVT 180, HYNA 290. Net -110 (Short Heavy, assuming short is negative).
                # We need to BUY 110.
                # If we buy on HYNA, we close short. If we buy on GRVT, we open long.
                # Ideally close the excess.
                
                cooldown_until = self.recovery_cooldowns.get(ticker, 0)
                if time.time() < cooldown_until:
                    remaining = cooldown_until - time.time()
                    self._log_recovery_skip(ticker, f"recovery_cooldown:{remaining:.0f}s")
                    continue

                positions = {
                    "GRVT": pos.get("GRVT", 0.0),
                    "HYNA": pos.get("HYNA", 0.0),
                    "VAR": pos.get("VAR", 0.0),
                }

                # Prefer the last entry plan's hedge venue for consistency (especially on fallback pairs).
                preferred = self._get_last_entry_plan(ticker)
                venue = None
                qty = 0.0
                if preferred and preferred.get("hedge_venue") in ("HYNA", "VAR"):
                    pref_venue = preferred.get("hedge_venue")
                    pref_qty = await self._adjust_qty_to_rules(ticker, pref_venue, abs_qty)
                    if pref_qty > 0 and self._can_hedge_on_venue(ticker, pref_venue, pref_qty):
                        venue = pref_venue
                        qty = pref_qty
                    else:
                        logger.warning(
                            f"   -> Preferred hedge {pref_venue} not viable (qty/balance)."
                        )
                        # Try the other taker venue if available
                        alt_venue = "VAR" if pref_venue == "HYNA" else "HYNA"
                        alt_qty = await self._adjust_qty_to_rules(ticker, alt_venue, abs_qty)
                        if alt_qty > 0 and self._can_hedge_on_venue(ticker, alt_venue, alt_qty):
                            venue = alt_venue
                            qty = alt_qty

                if not venue:
                    candidates = {
                        k: v
                        for k, v in positions.items()
                        if (v > 0 and net_qty > 0) or (v < 0 and net_qty < 0)
                    }
                    if not candidates:
                        self._log_recovery_skip(ticker, "no_candidate_venue")
                        continue

                    # Prefer the largest exposure, but only if balance/margin allows.
                    for cand, pos_qty in sorted(candidates.items(), key=lambda x: abs(x[1]), reverse=True):
                        cand_qty = await self._adjust_qty_to_rules(ticker, cand, min(abs_qty, abs(pos_qty)))
                        if cand_qty > 0 and self._can_hedge_on_venue(ticker, cand, cand_qty):
                            venue = cand
                            qty = cand_qty
                            break

                if not venue or qty <= 0:
                    logger.warning(f"   -> Auto-Hedging skipped (no viable venue) {ticker}")
                    self._log_recovery_skip(ticker, "no_viable_venue")
                    # Fallback: reduce GRVT position if it contributes to the net imbalance.
                    grvt_pos = positions.get("GRVT", 0.0)
                    if (net_qty > 0 and grvt_pos > 0) or (net_qty < 0 and grvt_pos < 0):
                        grvt_qty = min(abs(net_qty), abs(grvt_pos))
                        grvt_qty = await self._adjust_qty_to_rules(ticker, "GRVT", grvt_qty)
                        if grvt_qty > 0:
                            side_enum = OrderSide.SELL if net_qty > 0 else OrderSide.BUY
                            symbol = self._symbol_for_venue(ticker, "GRVT")
                            logger.info(
                                f"   -> Auto-Hedging fallback: {side_enum.value.upper()} {grvt_qty} on GRVT"
                            )
                            try:
                                await self._set_leverage_safe(
                                    "GRVT", symbol, int(getattr(Config, "TARGET_LEVERAGE", 5))
                                )
                                order = await self.grvt.create_order(
                                    symbol, OrderType.MARKET, side_enum, grvt_qty
                                )
                                self._log_trade(order, "GRVT", "auto_hedge_fallback")
                            except Exception as e:
                                logger.warning(f"   -> GRVT fallback hedge failed: {e}")
                        else:
                            logger.warning(f"   -> GRVT fallback skipped (min qty) {ticker}")
                    continue

                side_enum = OrderSide.SELL if net_qty > 0 else OrderSide.BUY
                logger.info(f"   -> Auto-Hedging: {side_enum.value.upper()} {qty} on {venue}")
                try:
                    if tasks is None:
                        await self._execute_hedge_with_verify(
                            ticker, venue, side_enum, qty, net_qty, "auto_hedge"
                        )
                    else:
                        self._spawn_tick_task(
                            tasks,
                            self._execute_hedge_with_verify(
                                ticker, venue, side_enum, qty, net_qty, "auto_hedge"
                            ),
                            f"auto_hedge:{ticker}",
                        )
                    self.recovery_cooldowns[ticker] = time.time() + float(getattr(Config, "HEDGE_RECOVERY_COOLDOWN_S", 20))
                except Exception as e:
                    logger.error(f"   -> Hedge Recovery Failed: {e}")

    async def _process_opportunities(self):
        """Check valid signals and enter"""
        # Clean up expired pending locks
        now = time.time()
        expired = [t for t, expiry in self.pending_tickers.items() if now > expiry]
        for t in expired:
            del self.pending_tickers[t]

        for ticker, signal in self.monitor.signals.items():
            if signal.is_opportunity:
                if self._is_funding_freeze_window(ticker):
                    if getattr(Config, "STRATEGY_REBALANCE_DEBUG", False):
                        logger.info(f"Skip {ticker} entry/rebalance: funding event window")
                    continue
                if not self._is_decision_window(ticker):
                    if getattr(Config, "FUNDING_DECISION_LOG", False):
                        self._log_entry_skip(ticker, signal, "before_decision_window")
                    continue
                # Check Pending Lock
                if ticker in self.pending_tickers:
                    remaining = self.pending_tickers.get(ticker, 0) - now
                    reason = "pending_cooldown"
                    exit_cd = self.exit_cooldowns.get(ticker)
                    if exit_cd and exit_cd > now:
                        reason = "exit_cooldown"
                    self._log_entry_skip(ticker, signal, f"{reason}:{max(0.0, remaining):.0f}s")
                    continue
                if self._exit_inflight_active(ticker) and not self._is_flat_state(ticker):
                    self._log_entry_skip(ticker, signal, "exit_inflight")
                    continue
                if ticker in self.active_grvt_orders:
                    continue

                # Check duplication using Reconciled State
                pos_struct = self.asset_manager.positions.get(ticker, {})
                state = pos_struct.get('State', 'IDLE')
                
                if state in ['HEDGED', 'PARTIAL_HEDGE']:
                     if state == 'HEDGED':
                         switched = await self._maybe_rebalance(ticker, signal, pos_struct)
                         if switched:
                             continue
                     # Already active, skip
                     continue
                
                if state == 'OPEN_ORDERS':
                    continue

                if self._has_active_position(ticker):
                    logger.info(f"Skipping {ticker} entry: existing position detected.")
                    continue

                if "HYNA" in (signal.best_ask_venue, signal.best_bid_venue):
                    remaining, reason = self._hyena_cooldown_info()
                    if remaining > 0:
                        detail = reason or "cooldown_active"
                        self._log_entry_skip(ticker, signal, f"hyena_cooldown:{detail}")
                        self.pending_tickers[ticker] = now + min(remaining, 30)
                        continue
                
                await self.execute_entry(signal)

    def _has_active_position(self, ticker: str) -> bool:
        pos = self.asset_manager.positions.get(ticker, {})
        min_abs = getattr(Config, "HEDGE_MIN_QTY", 0.01)
        for venue in ("GRVT", "HYNA", "VAR"):
            qty = float(pos.get(venue, 0.0) or 0.0)
            if abs(qty) >= min_abs:
                return True
        return False

    def _is_funding_freeze_window(self, ticker: str) -> bool:
        window_ms = int(getattr(Config, "FUNDING_EVENT_FREEZE_MS", 0) or 0)
        if window_ms <= 0:
            return False
        data = self.monitor.market_data.get(ticker, {})
        now_ms = int(time.time() * 1000)
        closest = None
        for venue in ("GRVT", "HYNA", "VAR"):
            ex_data = data.get(venue) or {}
            nft = int(ex_data.get("next_funding_time") or 0)
            if nft <= 0:
                continue
            delta = abs(nft - now_ms)
            if closest is None or delta < closest:
                closest = delta
        return closest is not None and closest <= window_ms

    def _next_funding_event_ms(self, ticker: str) -> int:
        data = self.monitor.market_data.get(ticker, {})
        now_ms = int(time.time() * 1000)
        next_ms = None
        for venue in ("GRVT", "HYNA", "VAR"):
            ex_data = data.get(venue) or {}
            nft = int(ex_data.get("next_funding_time") or 0)
            if nft <= 0:
                continue
            if nft < now_ms:
                continue
            if next_ms is None or nft < next_ms:
                next_ms = nft
        return int(next_ms or 0)

    def _is_decision_window(self, ticker: str) -> bool:
        lead_s = float(getattr(Config, "FUNDING_DECISION_LEAD_S", 0) or 0)
        if lead_s <= 0:
            return True
        nft = self._next_funding_event_ms(ticker)
        if nft <= 0:
            return True
        now_ms = int(time.time() * 1000)
        return now_ms >= (nft - int(lead_s * 1000))

    def _current_hedge_pair(self, pos_struct: Dict[str, float]) -> Optional[Dict[str, str]]:
        min_abs = getattr(Config, "HEDGE_MIN_QTY", 0.01)
        venues = []
        for venue in ("GRVT", "HYNA", "VAR"):
            qty = float(pos_struct.get(venue, 0.0) or 0.0)
            if abs(qty) >= min_abs:
                venues.append((venue, qty))
        if len(venues) < 2:
            return None
        long_venue = None
        short_venue = None
        for venue, qty in venues:
            if qty > 0 and long_venue is None:
                long_venue = venue
            if qty < 0 and short_venue is None:
                short_venue = venue
        if not long_venue or not short_venue:
            return None
        return {"long": long_venue, "short": short_venue}

    async def _maybe_rebalance(self, ticker: str, signal: StrategySignal, pos_struct: Dict[str, float]) -> bool:
        if not getattr(Config, "STRATEGY_REBALANCE_ENABLED", False):
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(f"Skip {ticker} rebalance: disabled")
            return False
        if not signal or not signal.is_opportunity:
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(f"Skip {ticker} rebalance: no opportunity")
            return False
        if self._is_funding_freeze_window(ticker):
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(f"Skip {ticker} rebalance: funding event window")
            return False
        if not self._is_decision_window(ticker):
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(f"Skip {ticker} rebalance: before decision window")
            return False
        current = self._current_hedge_pair(pos_struct)
        if not current:
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(f"Skip {ticker} rebalance: current pair unavailable")
            return False
        if current["long"] == signal.best_ask_venue and current["short"] == signal.best_bid_venue:
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(f"Skip {ticker} rebalance: already on {current['long']}/{current['short']}")
            return False

        min_hold = float(getattr(Config, "STRATEGY_REBALANCE_MIN_HOLD_S", 0))
        entry_ts = float(self.entry_timestamps.get(ticker, 0.0) or 0.0)
        if entry_ts and (time.time() - entry_ts) < min_hold:
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                held = time.time() - entry_ts
                logger.info(
                    f"Skip {ticker} rebalance: min_hold {min_hold:.0f}s (held {held:.0f}s)"
                )
            return False

        current_metrics = self._compute_pair_metrics(ticker, current["long"], current["short"])
        next_metrics = self._compute_pair_metrics(ticker, signal.best_ask_venue, signal.best_bid_venue)
        if not current_metrics or not next_metrics:
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(f"Skip {ticker} rebalance: metrics unavailable")
            return False
        current_yield = current_metrics["projected_yield"]
        next_yield = next_metrics["projected_yield"]
        delta_needed = float(getattr(Config, "STRATEGY_REBALANCE_SCORE_DELTA_BPS", 0) or 0) / 10000.0
        yield_mult = float(getattr(Config, "STRATEGY_REBALANCE_YIELD_MULTIPLIER", 1.0) or 1.0)
        if getattr(Config, "STRATEGY_REBALANCE_DEBUG", False):
            logger.info(
                f"Rebalance check {ticker}: current={current_yield:.6f} next={next_yield:.6f} "
                f"delta={next_yield - current_yield:.6f} threshold={delta_needed:.6f} mult={yield_mult:.2f}"
            )
        if next_yield <= 0:
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(f"Skip {ticker} rebalance: next_yield <= 0")
            return False
        if current_yield > 0 and next_yield < (current_yield * yield_mult):
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(
                    f"Skip {ticker} rebalance: next_yield {next_yield:.6f} < "
                    f"current_yield*{yield_mult:.2f} ({current_yield * yield_mult:.6f})"
                )
            return False
        if (next_yield - current_yield) < delta_needed:
            if getattr(Config, "STRATEGY_REBALANCE_LOG_REASONS", False):
                logger.info(
                    f"Skip {ticker} rebalance: delta {next_yield - current_yield:.6f} < {delta_needed:.6f}"
                )
            return False

        logger.info(
            f"🔄 Strategy switch for {ticker}: {current['long']}/{current['short']} -> "
            f"{signal.best_ask_venue}/{signal.best_bid_venue} (delta={next_yield - current_yield:.6f})"
        )
        await self.execute_exit(ticker)
        cooldown = float(getattr(Config, "STRATEGY_REBALANCE_COOLDOWN_S", 10))
        self.pending_tickers[ticker] = time.time() + cooldown
        return True
                
    def _compute_pair_metrics(self, ticker: str, long_venue: str, short_venue: str) -> Optional[Dict[str, float]]:
        data = self.monitor.market_data.get(ticker, {})
        l_data = data.get(long_venue)
        s_data = data.get(short_venue)
        if not l_data or not s_data:
            if getattr(Config, "STRATEGY_REBALANCE_DEBUG", False):
                logger.info(
                    f"Metrics unavailable {ticker}: l_data={bool(l_data)} s_data={bool(s_data)} "
                    f"venues={long_venue}/{short_venue}"
                )
            return None

        p_long = float(l_data.get("price") or 0)
        p_short = float(s_data.get("price") or 0)
        if p_long <= 0 or p_short <= 0:
            if getattr(Config, "STRATEGY_REBALANCE_DEBUG", False):
                logger.info(
                    f"Metrics invalid {ticker}: prices {long_venue}={p_long} {short_venue}={p_short}"
                )
            return None

        entry_spread_pct = (p_short - p_long) / p_long

        now_ms = time.time() * 1000
        yield_sum = self.monitor._projected_yield_for_pair(
            l_data,
            s_data,
            now_ms=now_ms,
            next_event_ms=self._global_next_event_ms(ticker, now_ms),
        )

        return {
            "entry_spread_pct": entry_spread_pct,
            "projected_yield": yield_sum,
        }

    async def _process_exits(self):
        """Check bad positions and exit"""
        now = time.time()
        for ticker, pos in self.asset_manager.positions.items():
            state = pos.get('State', 'IDLE')
            if state == 'HEDGED':
                # Normal Exit Logic (Yield/Spread check)
                if ticker in self.active_grvt_orders:
                    continue
                if pos.get("GRVT_OO", 0) > 0:
                    continue
                if ticker in self.pending_tickers:
                    continue
                if self._exit_inflight_active(ticker):
                    continue
                cooldown_until = self.exit_cooldowns.get(ticker, 0)
                if cooldown_until and now < cooldown_until:
                    if getattr(Config, "EXIT_LOG_REASONS", False):
                        remaining = cooldown_until - now
                        logger.info(f"Skip {ticker} exit: cooldown {remaining:.0f}s")
                    continue

                venues = [v for v in ["GRVT", "HYNA", "VAR"] if abs(pos.get(v, 0)) > 0]
                if len(venues) < 2:
                    continue
                if len(venues) > 2:
                    venues = sorted(venues, key=lambda v: abs(pos.get(v, 0)), reverse=True)[:2]

                long_venue = None
                short_venue = None
                for v in venues:
                    qty = pos.get(v, 0)
                    if qty > 0 and long_venue is None:
                        long_venue = v
                    elif qty < 0 and short_venue is None:
                        short_venue = v

                if not long_venue or not short_venue:
                    continue

                metrics = self._compute_pair_metrics(ticker, long_venue, short_venue)
                if not metrics:
                    continue

                exit_yield = float(getattr(Config, "EXIT_YIELD_THRESHOLD", 0.0))
                exit_spread = float(getattr(Config, "EXIT_SPREAD_THRESHOLD", 0.0))
                ignore_spread = bool(getattr(Config, "EXIT_IGNORE_SPREAD", False))
                exit_condition = metrics["projected_yield"] < exit_yield
                if not ignore_spread:
                    exit_condition = exit_condition or metrics["entry_spread_pct"] < exit_spread

                if exit_condition:
                    confirm_needed = int(getattr(Config, "EXIT_CONFIRM_COUNT", 1) or 1)
                    count = self.exit_confirmations.get(ticker, 0) + 1
                    self.exit_confirmations[ticker] = count
                    if count < confirm_needed:
                        if getattr(Config, "EXIT_LOG_REASONS", False):
                            logger.info(
                                f"Exit confirm {ticker}: {count}/{confirm_needed} "
                                f"yield={metrics['projected_yield']:.6f} spread={metrics['entry_spread_pct']:.6f}"
                            )
                        continue
                    self.exit_confirmations[ticker] = 0
                    logger.info(
                        f"🔻 Exit trigger {ticker}: "
                        f"yield={metrics['projected_yield']:.6f} spread={metrics['entry_spread_pct']:.6f}"
                    )
                    await self.execute_exit(ticker)
                    cooldown = float(getattr(Config, "EXIT_COOLDOWN_S", 30))
                    self.pending_tickers[ticker] = now + cooldown
                    self.exit_cooldowns[ticker] = now + cooldown
                else:
                    if ticker in self.exit_confirmations:
                        self.exit_confirmations[ticker] = 0
            
    async def execute_entry(self, signal: StrategySignal):
        """
        Execute Arbitrage Entry.
        Refacted to support Maker-Taker (GRVT) and Taker-Taker (VAR-HYNA).
        """
        self.is_processing = True
        logger.info(f"⚡ Executing Entry: {signal}")
        
        try:
            grvt_sym = f"{signal.ticker}_USDT_Perp"
            open_orders = await self.grvt.fetch_open_orders(grvt_sym)
            existing_order = None

            long_venue = signal.best_ask_venue
            short_venue = signal.best_bid_venue
            plan = await self._prepare_entry(signal.ticker, long_venue, short_venue)
            if not plan and getattr(Config, "ENABLE_FALLBACK_ENTRY", True):
                attempts = 0
                for cand in self._rank_entry_pairs(signal.ticker):
                    if not cand.get("is_opportunity"):
                        continue
                    if cand["long"] == long_venue and cand["short"] == short_venue:
                        continue
                    plan = await self._prepare_entry(signal.ticker, cand["long"], cand["short"])
                    if plan:
                        logger.warning(
                            f"⚠️ Fallback entry {signal.ticker}: "
                            f"{long_venue}/{short_venue} -> {cand['long']}/{cand['short']}"
                        )
                        break
                    attempts += 1
                    if attempts >= int(getattr(Config, "FALLBACK_MAX_ATTEMPTS", 3) or 3):
                        break
            if not plan:
                logger.warning(f"Entry skipped {signal.ticker}: no viable pair (balances/size).")
                return

            long_venue = plan["long_venue"]
            short_venue = plan["short_venue"]
            is_grvt_maker = plan["is_grvt_maker"]
            exec_signal = signal
            if long_venue != signal.best_ask_venue or short_venue != signal.best_bid_venue:
                exec_signal = StrategySignal(
                    signal.ticker,
                    long_venue,
                    short_venue,
                    plan.get("entry_spread_pct", signal.spread),
                    plan.get("projected_yield", signal.projected_yield),
                )

            target_side = None
            hedge_side = None
            if is_grvt_maker:
                target_side = plan["target_side"]
                hedge_side = OrderSide.SELL if target_side == OrderSide.BUY else OrderSide.BUY
            # Track the selected entry plan to keep hedge venue consistent (including fallback pairs)
            entry_plan = {
                "long_venue": long_venue,
                "short_venue": short_venue,
                "ts": time.time(),
            }
            if is_grvt_maker:
                entry_plan.update(
                    {
                        "hedge_venue": plan.get("hedge_venue"),
                        "hedge_side": hedge_side,
                    }
                )
            self.last_entry_plan[signal.ticker] = entry_plan

            if open_orders:
                if is_grvt_maker:
                    min_hold = float(getattr(Config, "GRVT_ORDER_MIN_HOLD_S", 45))
                    now = time.time()
                    for o in open_orders:
                        if o.side != target_side:
                            create_ts = getattr(o, "timestamp", None)
                            raw = getattr(o, "raw", {}) or {}
                            raw_meta = raw.get("metadata", {}) if isinstance(raw, dict) else {}
                            if raw_meta.get("create_time"):
                                try:
                                    create_ts = int(raw_meta["create_time"]) / 1_000_000_000
                                except (ValueError, TypeError):
                                    create_ts = None
                            if create_ts and (now - float(create_ts)) < min_hold:
                                logger.warning(
                                    f"⚠️ Mismatched GRVT order {o.id} detected but within hold window; skip cancel."
                                )
                                continue
                            logger.warning(f"⚠️ Found mismatched GRVT order {o.id} (Side: {o.side}). Cancelling.")
                            await self.grvt.cancel_order(o.id, grvt_sym)
                    for o in open_orders:
                        if o.side == target_side:
                            existing_order = o
                            logger.info(f"♻️ Found existing open order for {signal.ticker}: {o.id}. Adopting.")
                            break
                    if existing_order:
                            hedge_override = None
                            if hedge_side and plan.get("hedge_venue"):
                                hedge_override = {"venue": plan.get("hedge_venue"), "side": hedge_side}
                            await self._track_grvt_order(signal.ticker, existing_order, target_side, hedge_override)
                            self.pending_tickers[signal.ticker] = time.time() + 5
                            return
                else:
                    order = open_orders[0]
                    logger.warning(f"⚠️ GRVT order exists but strategy is {long_venue}/{short_venue}. Holding order {order.id}.")
                    await self._track_grvt_order(signal.ticker, order, order.side)
                    self.pending_tickers[signal.ticker] = time.time() + 5
                    return

            if not is_grvt_maker:
                logger.info(f"🚀 Taker-Taker Strategy Detected: {long_venue} Long / {short_venue} Short")
                if "HYNA" in (long_venue, short_venue):
                    if not self._hyena_ready_for_action(signal.ticker, "entry_taker"):
                        self.pending_tickers[signal.ticker] = time.time() + 30
                        return
                size = plan["size"]
                sym_long = plan["sym_long"]
                sym_short = plan["sym_short"]
                lev_long_used = plan["lev_long"]
                lev_short_used = plan["lev_short"]

                logger.info(f"   ⚖️ Taker Size: {size}")
                await self._set_leverage_safe(long_venue, sym_long, int(lev_long_used))
                await self._set_leverage_safe(short_venue, sym_short, int(lev_short_used))

                logger.info(f"-> Executing Leg 1: Buy {size} on {long_venue}")
                o1 = None
                o1_error = None
                try:
                    if long_venue == 'HYNA':
                        o1 = await self.hyena.create_order(sym_long, OrderType.MARKET, OrderSide.BUY, size)
                    elif long_venue == 'VAR':
                        o1 = await self.variational.create_order(sym_long, OrderType.MARKET, OrderSide.BUY, size)
                    elif long_venue == 'GRVT':
                        o1 = await self.grvt.create_order(sym_long, OrderType.MARKET, OrderSide.BUY, size)
                except Exception as e:
                    logger.error(f"❌ Leg 1 ({long_venue}) Exception: {e}")
                    o1_error = e
                    o1 = None

                if not o1:
                    logger.error(f"❌ Leg 1 ({long_venue}) Failed. Aborting Leg 2.")
                    self._apply_entry_failure_cooldown(
                        signal.ticker, long_venue, o1_error or "order_failed", "entry_leg1"
                    )
                    return
                self._log_trade(o1, long_venue, "entry", exec_signal)

                logger.info(f"-> Executing Leg 2: Sell {size} on {short_venue}")
                o2 = None
                try:
                    if short_venue == 'HYNA':
                        o2 = await self.hyena.create_order(sym_short, OrderType.MARKET, OrderSide.SELL, size)
                    elif short_venue == 'VAR':
                        o2 = await self.variational.create_order(sym_short, OrderType.MARKET, OrderSide.SELL, size)
                    elif short_venue == 'GRVT':
                        o2 = await self.grvt.create_order(sym_short, OrderType.MARKET, OrderSide.SELL, size)
                except Exception as e:
                    logger.error(f"❌ Leg 2 ({short_venue}) Exception: {e}")
                    o2 = None

                if o2:
                    self._log_trade(o2, short_venue, "entry", exec_signal)

                id1 = o1.id if o1 else 'N/A'
                id2 = o2.id if o2 else 'N/A'
                logger.info(f"✅ Taker-Taker Entry Complete. IDs: {long_venue}:{id1}, {short_venue}:{id2}")
                self.pending_tickers[signal.ticker] = time.time() + 5
                return

            size = plan["size"]
            hedge_venue = plan["hedge_venue"]
            grvt_lev = plan["grvt_lev"]
            target_price = plan["target_price"]
            target_side = plan["target_side"]

            await self._set_leverage_safe("GRVT", grvt_sym, int(grvt_lev))
            logger.info(f"-> Placing GRVT Maker {target_side} {size} @ {target_price}")
            try:
                order = await self.grvt.create_order(
                    grvt_sym, OrderType.LIMIT, target_side, size, target_price, params={"post_only": True}
                )
            except Exception as e:
                logger.error(f"❌ GRVT create_order failed: {e}")
                self._apply_entry_failure_cooldown(signal.ticker, "GRVT", e, "entry_maker")
                return
            if not order or str(getattr(order, "id", "")) in ("", "0x00") or self._is_grvt_order_rejected(order):
                logger.error("❌ GRVT maker order rejected/invalid. Skipping entry.")
                self._apply_entry_failure_cooldown(signal.ticker, "GRVT", getattr(order, "raw", None) or "rejected", "entry_maker")
                return
            self._log_trade(order, "GRVT", "entry_maker", exec_signal)

            hedge_override = None
            if hedge_side and plan.get("hedge_venue"):
                hedge_override = {"venue": plan.get("hedge_venue"), "side": hedge_side}
            await self._track_grvt_order(signal.ticker, order, target_side, hedge_override)
            logger.info("✅ Entry Submitted (Maker order tracked)")
            self.pending_tickers[signal.ticker] = time.time() + 5

        except Exception as e:
            logger.error(f"Entry Execution Failed: {e}")
        finally:
            self.is_processing = False
            
    async def execute_exit(self, ticker):
        """
        Execute Arbitrage Exit.
        Sequence: GRVT Limit (Maker Close) -> Wait Fill -> Hedge Close
        """
        self.is_processing = True
        logger.info(f"🛑 Executing Exit: {ticker}")
        ttl = float(getattr(Config, "EXIT_INFLIGHT_TTL_S", 180))
        if ttl > 0:
            self.exit_inflight[ticker] = time.time() + ttl
        
        try:
             pos = self.asset_manager.positions.get(ticker)
             if not pos: 
                 logger.warning("No position to exit")
                 return

             grvt_sym = f"{ticker}_USDT_Perp"
             grvt_size = pos.get('GRVT', 0)
             if grvt_size == 0:
                 logger.warning("GRVT leg empty, closing remaining legs only.")
                 lght_size = pos.get('HYNA', 0)
                 var_size = pos.get('VAR', 0)
                 if abs(lght_size) > 0:
                     h_side = OrderSide.BUY if lght_size < 0 else OrderSide.SELL
                     logger.info(f"-> Closing Hyena Leg {h_side} {abs(lght_size)}")
                     sym = f"{ticker}-USDT"
                     if not self._hyena_ready_for_action(ticker, "exit_hedge"):
                         self.exit_inflight.pop(ticker, None)
                         return
                     order = await self.hyena.create_order(sym, OrderType.MARKET, h_side, abs(lght_size))
                     self._log_trade(order, "HYNA", "exit_hedge")
                 if abs(var_size) > 0:
                     h_side = OrderSide.BUY if var_size < 0 else OrderSide.SELL
                     logger.info(f"-> Closing VAR Leg {h_side} {abs(var_size)}")
                     order = await self.variational.create_order(ticker, OrderType.MARKET, h_side, abs(var_size))
                     self._log_trade(order, "VAR", "exit_hedge")
                 cooldown = float(getattr(Config, "EXIT_COMPLETE_COOLDOWN_S", 90))
                 self.exit_cooldowns[ticker] = time.time() + cooldown
                 return
                 
             # 0. If an exit order is already open, wait before cancel/replace.
             wait_s = float(getattr(Config, "EXIT_ORDER_WAIT_S", 45) or 0)
             logger.info(f"[ExitWait] {ticker} wait_s={wait_s:.0f}s")
             if wait_s > 0:
                 try:
                     open_orders = await self.grvt.fetch_open_orders(grvt_sym)
                 except Exception:
                     open_orders = []
                 open_orders = open_orders or []
                 open_exit = list(open_orders)
                 logger.info(f"[ExitWait] {ticker} open_exit_count={len(open_exit)}")
                 if open_exit:
                     logger.info(
                         f"-> Exit order open for {ticker} ({len(open_exit)}), waiting {wait_s:.0f}s before replace"
                     )
                     end_ts = time.time() + wait_s
                     while time.time() < end_ts:
                         await asyncio.sleep(3)
                         try:
                             open_orders = await self.grvt.fetch_open_orders(grvt_sym)
                         except Exception:
                             open_orders = []
                         open_orders = open_orders or []
                         open_exit = list(open_orders)
                         logger.info(f"[ExitWait] {ticker} poll_open_exit_count={len(open_exit)}")
                         if not open_exit:
                             break
                     if open_exit:
                         logger.info(f"[ExitWait] {ticker} canceling {len(open_exit)} open orders")
                         for o in open_exit:
                             try:
                                 await self.grvt.cancel_order(o.id, grvt_sym)
                             except Exception:
                                 pass

             # 1. Determine Side & Price
             # If GRVT Long (>0) -> Sell Close
             side = 'sell' if grvt_size > 0 else 'buy'
             abs_size = abs(grvt_size)
             
             # Fetch latest price
             price = self.monitor.market_data[ticker]['GRVT']['price']
             
             # 2. Place GRVT Order
             # Use reduce_only if supported
             logger.info(f"-> Placing GRVT Maker Close ({side}) {abs_size} @ {price}")
             t_side = OrderSide.SELL if side == 'sell' else OrderSide.BUY
             price = self._compute_grvt_exit_price(ticker, t_side, price, 0.0)
             order = await self.grvt.create_order(
                 grvt_sym, OrderType.LIMIT, t_side, abs_size, price, params={"reduce_only": True}
             )
            
             if not order:
                 logger.error("GRVT Exit Order Failed")
                 return
             self._log_trade(order, "GRVT", "exit_maker")
                 
             # 3. Monitor Fill
             filled = False
             initial_grvt_pos = grvt_size # Track original signed size
             
             for _ in range(10):
                 await asyncio.sleep(1)
                 open_orders = await self.grvt.fetch_open_orders(grvt_sym)
                 if not open_orders:
                     open_orders = []
                 is_open = any(o.id == order.id for o in open_orders)
                 if is_open:
                     continue

                 # Check Delta
                 curr_poses = await self.grvt.fetch_positions()
                 current_size = 0.0
                 for p in curr_poses:
                     # p.symbol vs grvt_sym
                     if p.symbol == grvt_sym:
                         current_size = float(p.amount)
                         if p.side == OrderSide.SELL:
                             current_size = -abs(current_size)
                         else:
                             current_size = abs(current_size)

                 if abs(current_size - initial_grvt_pos) >= (abs_size * 0.95):
                     filled = True
                 break

             if not filled:
                 logger.info(f"-> GRVT exit order still open for {ticker}; tracking for reprice/close")
                 lght_size = pos.get('HYNA', 0)
                 var_size = pos.get('VAR', 0)
                 self.exit_grvt_orders[ticker] = {
                     "order_id": order.id,
                     "client_order_id": getattr(order, "client_order_id", None),
                     "side": t_side,
                     "size": abs_size,
                     "created_ts": time.time(),
                     "lght_size": lght_size,
                     "var_size": var_size,
                 }
                 return
                 
             # 4. Hedge Close
             # Find which exchange has the other leg
             lght_size = pos.get('HYNA', 0)
             var_size = pos.get('VAR', 0)
             
             if abs(lght_size) > 0:
                 h_side = OrderSide.BUY if lght_size < 0 else OrderSide.SELL
                 logger.info(f"-> Closing Hyena Leg {h_side} {abs(lght_size)}")
                 sym = f"{ticker}-USDT"
                 if not self._hyena_ready_for_action(ticker, "exit_hedge"):
                     self.exit_inflight.pop(ticker, None)
                     return
                 order = await self.hyena.create_order(sym, OrderType.MARKET, h_side, abs(lght_size))
                 self._log_trade(order, "HYNA", "exit_hedge")
                 
             if abs(var_size) > 0:
                 h_side = OrderSide.BUY if var_size < 0 else OrderSide.SELL
                 logger.info(f"-> Closing VAR Leg {h_side} {abs(var_size)}")
                 order = await self.variational.create_order(ticker, OrderType.MARKET, h_side, abs(var_size))
                 self._log_trade(order, "VAR", "exit_hedge")
                 
             logger.info("✅ Exit Complete")

        except Exception as e:
            logger.error(f"Exit Execution Failed: {e}")
        finally:
            self.is_processing = False

    def calculate_common_quantity(self, size_usd, p1, p2, rules1, rules2):
        """
        Calculates a safe trade quantity that satisfies constraints of both venues.
        1. Calculates Target Qty based on Average Price.
        2. Enforces Min Qty (Global Max of Min Qty).
        3. Enforces Min Notional (Global Max of implied Min Qty).
        4. Enforces Step Size (LCM/Max of Step Sizes).
        """
        avg_price = (p1 + p2) / 2
        if avg_price == 0: return 0
        target_qty = size_usd / avg_price
        
        # 1. Apply Min Constraints
        min_q1 = float(rules1.get('min_qty', 0) or 0)
        min_q2 = float(rules2.get('min_qty', 0) or 0)
        
        # Min Notional implied limit (e.g. $10 / Price)
        min_n1_val = float(rules1.get('min_notional', 0) or 0)
        min_n2_val = float(rules2.get('min_notional', 0) or 0)
        
        min_q1_notional = min_n1_val / p1 if p1 > 0 else 0
        min_q2_notional = min_n2_val / p2 if p2 > 0 else 0
        
        # Global Minimum Quantity we must satisfy (Max of all minimums)
        global_min = max(min_q1, min_q2, min_q1_notional, min_q2_notional)
        
        # If target is less than min, bump it up (User accepts potentially slightly larger trade)
        final_qty = max(target_qty, global_min)
        
        # 2. Apply Step Size (Precision)
        # We need a step that works for both. Using Max(Steps) is safest coarse step.
        step1 = float(rules1.get('step_size', 0) or rules1.get('min_qty', 0) or 0.0001)
        step2 = float(rules2.get('step_size', 0) or rules2.get('min_qty', 0) or 0.0001)
        min_q1 = float(rules1.get('min_qty', 0) or 0)
        min_q2 = float(rules2.get('min_qty', 0) or 0)
        if min_q1 and step1:
            step1 = max(step1, min_q1)
        if min_q2 and step2:
            step2 = max(step2, min_q2)
        
        if step1 == 0: step1 = 0.0001
        if step2 == 0: step2 = 0.0001
        
        common_step = max(step1, step2)
        
        # Round to nearest multiple of common_step
        inv = 1.0 / common_step
        quantized = round(final_qty * inv) / inv
        
        # Re-verify min (rounding might have dropped below)
        if quantized < global_min:
            quantized += common_step
            
        return quantized
