import asyncio
import logging
from typing import Dict, List, Optional
from shared_crypto_lib.base import AbstractExchange
from shared_crypto_lib.models import Balance, Position, Order, OrderSide, OrderStatus
from src.config import Config

logger = logging.getLogger("AssetManager")

class AssetManager:
    def __init__(self, grvt: AbstractExchange, lighter: AbstractExchange, variational: AbstractExchange, log_manager=None):
        self.grvt = grvt
        self.lighter = lighter
        self.variational = variational
        self.log_manager = log_manager
        
        self.balances = {
            'GRVT': {'total': 0.0, 'available': 0.0},
            'LGHT': {'total': 0.0, 'available': 0.0},
            'VAR': {'total': 0.0, 'available': 0.0}
        }
        
        self.positions = {} # { ticker: { 'GRVT': Size, 'LGHT': Size, 'VAR': Size, 'state': 'HEDGED'|'UNHEDGED' } }
        
    async def update_assets(self):
        """Update both balances and positions"""
        await asyncio.gather(
            self._update_balances(),
            self._update_positions()
        )
        self._aggregate_positions()
        
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

        # 2. Lighter
        try:
            b = await self.lighter.fetch_balance()
            if b and isinstance(b, Balance):
                self.balances['LGHT'] = {
                    'total': b.total,
                    'available': b.free
                }
        except Exception as e:
            logger.error(f"LGHT Balance Fail: {e}")
            
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
            
        logger.info(f"💰 Balances - GRVT: {self.balances['GRVT']['available']:.2f}, LGHT: {self.balances['LGHT']['available']:.2f}, VAR: {self.balances['VAR']['available']:.2f}")
        if self.log_manager:
            self._log_balances_snapshot()

    async def _update_positions(self):
        # Fetch detailed positions AND Open Orders from all exchanges
        self.raw_positions = {'GRVT': [], 'LGHT': [], 'VAR': []}
        self.raw_orders = {'GRVT': [], 'LGHT': [], 'VAR': []}
        
        # 1. GRVT
        try:
            self.raw_positions['GRVT'] = await self.grvt.fetch_positions()
            self.raw_orders['GRVT'] = await self.grvt.fetch_open_orders()
        except Exception as e:
            logger.error(f"GRVT State Fail: {e}")

        # 2. Lighter
        try:
            # Lighter fetch_positions -> List[Position]
            self.raw_positions['LGHT'] = await self.lighter.fetch_positions()
            # Shared Lib Lighter might not support fetch_open_orders yet.
        except Exception as e:
                logger.error(f"LGHT Pos Fail: {e}")

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
            if '_' in s: return s.split('_')[0]
            if '-' in s: return s.split('-')[0]
            return s
            
        # Collect tickers from Positions (Standardized Models)
        # GRVT (List[Position])
        for p in self.raw_positions['GRVT']:
            # p.symbol is standardized? e.g. BTC_USDT_Perp
            base = get_base(p.symbol)
            if base: all_tickers.add(base)
            
        # Lighter (List[Position])
        for p in self.raw_positions['LGHT']:
            # p.symbol e.g. BTC-USDT
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
                'GRVT': 0.0, 'LGHT': 0.0, 'VAR': 0.0,
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

            # Lighter
            for p in self.raw_positions['LGHT']:
                if get_base(p.symbol) == t:
                    qty = float(p.amount) # Lighter usually returns signed size? 
                    # If model standardizes to positive amount + side:
                    if p.side == OrderSide.SELL: qty = -abs(qty)
                    else: qty = abs(qty)
                    pos_data['LGHT'] += qty
                    
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
            net_qty = pos_data['GRVT'] + pos_data['LGHT'] + pos_data['VAR']
            abs_total = abs(pos_data['GRVT']) + abs(pos_data['LGHT']) + abs(pos_data['VAR'])
            
            if abs_total == 0:
                if pos_data['GRVT_OO'] > 0:
                    pos_data['State'] = 'OPEN_ORDERS'
                else:
                    pos_data['State'] = 'IDLE'
            else:
                # Active Positions exist
                # Tolerance 5% (min 0.01 size)
                msg_qty = max(abs(pos_data['GRVT']), abs(pos_data['LGHT']), abs(pos_data['VAR']))
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
        
        # Log Anomalies
        for t, p in self.positions.items():
            if p['State'] == 'PARTIAL_HEDGE':
                logger.warning(f"⚠️ {t} PARTIAL HEDGE: GRVT:{p['GRVT']} LGHT:{p['LGHT']} VAR:{p['VAR']} (Net:{p.get('Net_Qty', 0):.2f})")
            if p['State'] == 'OPEN_ORDERS':
                 logger.info(f"⏳ {t} Pending Orders on GRVT")


        logger.info(f"Aggregated Positions: {len(self.positions)} active tickers.")

    def _log_balances_snapshot(self):
        for venue, data in self.balances.items():
            asset = "USDC" if venue == "VAR" else "USDT"
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
        for venue in ("GRVT", "LGHT", "VAR"):
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
