import asyncio
import logging
from typing import Dict, List, Optional
from shared_crypto_lib.base import AbstractExchange
from shared_crypto_lib.models import Balance, Position, Order, OrderSide, OrderStatus
from src.config import Config

logger = logging.getLogger("AssetManager")

class AssetManager:
    def __init__(self, grvt: AbstractExchange, hyena: AbstractExchange, variational: AbstractExchange, log_manager=None):
        self.grvt = grvt
        self.hyena = hyena
        self.variational = variational
        self.log_manager = log_manager
        
        self.balances = {
            'GRVT': {'total': 0.0, 'available': 0.0},
            'HYNA': {'total': 0.0, 'available': 0.0},
            'VAR': {'total': 0.0, 'available': 0.0}
        }
        self.initial_total_balance: Optional[float] = None
        
        self.positions = {} # { ticker: { 'GRVT': Size, 'HYNA': Size, 'VAR': Size, 'state': 'HEDGED'|'UNHEDGED' } }
        
    async def update_assets(self):
        """Update both balances and positions"""
        await asyncio.gather(
            self._update_balances(),
            self._update_positions()
        )
        self._aggregate_positions()
        if self.initial_total_balance is None:
            self.initial_total_balance = self.total_balance()
        if self.log_manager:
            self._log_balances_snapshot()
        
    async def _update_balances(self):
        # 1. GRVT
        try:
             b = await self.grvt.fetch_balance()
             if b and isinstance(b, Balance):
                 self.balances['GRVT'] = {
                     'total': b.total,
                     'available': b.free
                 }
             elif isinstance(b, dict) and 'USDT' in b: # GRVT might return dict of Balances
                  self.balances['GRVT'] = {
                      'total': b['USDT'].total,
                      'available': b['USDT'].free
                  }
        except Exception as e:
            logger.error(f"GRVT Balance Fail: {e}")

        # 2. Hyena
        try:
            b = await self.hyena.fetch_balance()
            if b and isinstance(b, Balance):
                self.balances['HYNA'] = {
                    'total': b.total,
                    'available': b.free
                }
            elif isinstance(b, dict):
                # Hyena returns {"USDe": Balance(...)} style.
                bal = b.get("USDe") or b.get("USDE")
                if not bal and b:
                    bal = next(iter(b.values()), None)
                if isinstance(bal, Balance):
                    self.balances['HYNA'] = {
                        'total': bal.total,
                        'available': bal.free
                    }
        except Exception as e:
            logger.error(f"HYNA Balance Fail: {e}")
            
        # 3. Variational
        try:
            if self.variational:
                b = await self.variational.fetch_balance()
                if b and isinstance(b, Balance):
                    self.balances['VAR'] = {
                        'total': b.total,
                        'available': b.free
                    }
        except Exception as e:
            logger.error(f"VAR Balance Fail: {e}")
            
        logger.info(f"💰 Balances - GRVT: {self.balances['GRVT']['available']:.2f}, HYNA: {self.balances['HYNA']['available']:.2f}, VAR: {self.balances['VAR']['available']:.2f}")

    async def _update_positions(self):
        # Fetch detailed positions AND Open Orders from all exchanges
        self.raw_positions = {'GRVT': [], 'HYNA': [], 'VAR': []}
        self.raw_orders = {'GRVT': [], 'HYNA': [], 'VAR': []}
        
        # 1. GRVT
        try:
            self.raw_positions['GRVT'] = await self.grvt.fetch_positions()
            self.raw_orders['GRVT'] = await self.grvt.fetch_open_orders()
        except Exception as e:
            logger.error(f"GRVT State Fail: {e}")

        # 2. Hyena
        try:
            self.raw_positions['HYNA'] = await self.hyena.fetch_positions()
            self.raw_orders['HYNA'] = await self.hyena.fetch_open_orders()
        except Exception as e:
                logger.error(f"HYNA Pos Fail: {e}")

        # 3. Variational
        try:
            if self.variational:
                self.raw_positions['VAR'] = await self.variational.fetch_positions()
        except Exception as e:
                logger.error(f"VAR Pos Fail: {e}")

        if self.log_manager:
            self._log_positions_snapshot()
            
    def _aggregate_positions(self):
        """
        Merge raw positions into reconciled ticker view.
        Determine State: HEDGED, PARTIAL_HEDGE, UNHEDGED, OPEN_ORDERS
        """
        aggregated = {}
        all_tickers = set()
        
        # Helper to extract Base Asset
        def get_base(symbol):
            if not symbol: return None
            # Handle standard formats
            s = str(symbol).upper()
            if ':' in s:
                s = s.split(':', 1)[1]
            if '_' in s: return s.split('_')[0]
            if '-' in s: return s.split('-')[0]
            return s
            
        # Collect tickers from Positions (Standardized Models)
        # GRVT (List[Position])
        for p in self.raw_positions['GRVT']:
            # p.symbol is standardized? e.g. BTC_USDT_Perp
            base = get_base(p.symbol)
            if base: all_tickers.add(base)
            
        # Hyena (List[Position])
        for p in self.raw_positions['HYNA']:
            # p.symbol may be HYPE or hyna:HYPE
            base = get_base(p.symbol)
            if base: all_tickers.add(base)
            
        # Variational (List[Position])
        for p in self.raw_positions['VAR']:
            base = get_base(p.symbol)
            if base: all_tickers.add(base)
            
        # Collect tickers from Open Orders (GRVT mainly)
        for o in self.raw_orders['GRVT']:
            base = get_base(o.symbol)
            if base: all_tickers.add(base)

        # Map Per Ticker
        for t in all_tickers:
            pos_data = {
                'GRVT': 0.0, 'HYNA': 0.0, 'VAR': 0.0,
                'GRVT_OO': 0, # Open Order Count
                'State': 'IDLE',
                'Action': 'NONE'
            }
            
            # --- Populate Positions (Model Access) ---
            # GRVT
            for p in self.raw_positions['GRVT']:
                if get_base(p.symbol) == t:
                    qty = float(p.amount)
                    # Polarity: p.side is OrderSide.BUY/SELL
                    if p.side == OrderSide.SELL: qty = -abs(qty)
                    else: qty = abs(qty)
                    pos_data['GRVT'] += qty 

            # Hyena
            for p in self.raw_positions['HYNA']:
                if get_base(p.symbol) == t:
                    qty = float(p.amount)
                    # If model standardizes to positive amount + side:
                    if p.side == OrderSide.SELL: qty = -abs(qty)
                    else: qty = abs(qty)
                    pos_data['HYNA'] += qty
                    
            # Variational
            for p in self.raw_positions['VAR']:
                if get_base(p.symbol) == t:
                    start_qty = float(p.amount)
                    if p.side == OrderSide.SELL: start_qty = -abs(start_qty)
                    else: start_qty = abs(start_qty)
                    pos_data['VAR'] += start_qty
            
            # --- Populate Open Orders (GRVT) ---
            for o in self.raw_orders['GRVT']:
                if get_base(o.symbol) == t:
                    pos_data['GRVT_OO'] += 1

            # --- Determine State ---
            net_qty = pos_data['GRVT'] + pos_data['HYNA'] + pos_data['VAR']
            abs_total = abs(pos_data['GRVT']) + abs(pos_data['HYNA']) + abs(pos_data['VAR'])
            
            if abs_total == 0:
                if pos_data['GRVT_OO'] > 0:
                    pos_data['State'] = 'OPEN_ORDERS'
                else:
                    pos_data['State'] = 'IDLE'
            else:
                # Active Positions exist
                # Tolerance 5% (min 0.01 size)
                msg_qty = max(abs(pos_data['GRVT']), abs(pos_data['HYNA']), abs(pos_data['VAR']))
                tol_ratio = getattr(Config, "HEDGE_TOLERANCE_RATIO", 0.05)
                min_abs = getattr(Config, "HEDGE_MIN_QTY", 0.01)
                tol = max(msg_qty * tol_ratio, min_abs)
                if msg_qty > 0 and abs(net_qty) <= tol:
                     pos_data['State'] = 'HEDGED'
                else:
                     pos_data['State'] = 'PARTIAL_HEDGE'
                     pos_data['Net_Qty'] = net_qty
            
            aggregated[t] = pos_data
            
            # Legacy alias
            pos_data['state'] = pos_data['State']
            
        self.positions = aggregated

        # Adjust HYNA total to include HYNA estimated margin + USDe spot.
        try:
            hy_margin = 0.0
            spot_usde = 0.0
            pnl_sum = 0.0
            for p in self.raw_positions.get("HYNA", []):
                qty = abs(float(getattr(p, "amount", 0.0) or 0.0))
                if qty <= 0:
                    continue
                px = float(getattr(p, "mark_price", 0.0) or 0.0)
                if px <= 0:
                    px = float(getattr(p, "entry_price", 0.0) or 0.0)
                if px > 0:
                    notional = qty * px
                    lev = float(getattr(p, "leverage", 0.0) or 0.0)
                    if lev > 0:
                        hy_margin += notional / lev
                    else:
                        hy_margin += notional
                try:
                    pnl_sum += float(getattr(p, "unrealized_pnl", 0.0) or 0.0)
                except Exception:
                    pass
            if self.balances.get("HYNA"):
                spot_usde = float(self.balances["HYNA"].get("total", 0.0))
                self.balances["HYNA"]["total"] = spot_usde + hy_margin + pnl_sum
            # Store components for diagnostics
            self.hyena_spot_usde = spot_usde
            self.hyena_margin_est = hy_margin
            if spot_usde or hy_margin or pnl_sum:
                logger.info(
                    f"HYNA balance components: spot_usde={spot_usde:.4f}, "
                    f"margin_est={hy_margin:.4f}, pnl={pnl_sum:.4f}, "
                    f"total={spot_usde + hy_margin + pnl_sum:.4f}"
                )
        except Exception as e:
            logger.warning(f"HYNA notional aggregation failed: {e}")
        
        # Log Anomalies
        for t, p in self.positions.items():
            if p['State'] == 'PARTIAL_HEDGE':
                logger.warning(f"⚠️ {t} PARTIAL HEDGE: GRVT:{p['GRVT']} HYNA:{p['HYNA']} VAR:{p['VAR']} (Net:{p.get('Net_Qty', 0):.2f})")
            if p['State'] == 'OPEN_ORDERS':
                logger.info(f"⏳ {t} Pending Orders on GRVT")

        logger.info(f"Aggregated Positions: {len(self.positions)} active tickers.")

    def total_balance(self) -> float:
        return sum(v.get('total', 0.0) for v in self.balances.values())

    def session_pnl(self) -> float:
        if self.initial_total_balance is None:
            return 0.0
        return self.total_balance() - self.initial_total_balance

    def _log_balances_snapshot(self):
        for venue, data in self.balances.items():
            if venue == "VAR":
                asset = "USDC"
            elif venue == "HYNA":
                asset = "USDE"
            else:
                asset = "USDT"
            total = float(data.get("total") or 0.0)
            available = float(data.get("available") or 0.0)
            self.log_manager.log_balance(
                {
                    "exchange": venue,
                    "asset": asset,
                    "total": total,
                    "available": available,
                    "margin_used": max(total - available, 0.0),
                }
            )

    def _log_positions_snapshot(self):
        for venue in ("GRVT", "HYNA", "VAR"):
            for pos in self.raw_positions.get(venue, []):
                self.log_manager.log_position(
                    {
                        "exchange": venue,
                        "symbol": pos.symbol,
                        "side": pos.side.value if hasattr(pos.side, "value") else str(pos.side),
                        "size": pos.amount,
                        "entry_price": pos.entry_price,
                        "mark_price": pos.mark_price,
                        "pnl": pos.unrealized_pnl,
                        "leverage": pos.leverage,
                        "margin_type": getattr(Config, "GRVT_MARGIN_TYPE", "") if venue == "GRVT" else "",
                        "liquidation_price": pos.liquidation_price,
                    }
                )
