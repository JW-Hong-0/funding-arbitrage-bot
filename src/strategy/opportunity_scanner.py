import asyncio
import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass

from shared_crypto_lib.base import AbstractExchange
from shared_crypto_lib.base import AbstractExchange
from ..config import Config

logger = logging.getLogger(__name__)

@dataclass
class arbitrage_opportunity:
    symbol: str          # Base symbol e.g., "ETH"
    grvt_symbol: str
    lighter_symbol: str
    grvt_funding_rate: float
    lighter_funding_rate: float
    spread: float        
    estimated_annual_apy: float
    timestamp: float
    
    # Fields with Defaults (Must come after non-defaults)
    net_yield_8h: float = 0.0 # Projected 8h yield
    direction: str = 'Long_GRVT' # 'Long_GRVT' or 'Short_GRVT'
    grvt_funding_interval_hours: int = 8 
    lighter_funding_interval_hours: int = 1 
    adj_grvt_rate_1h: float = 0.0
    adj_lighter_rate_1h: float = 0.0
    grvt_price: float = 0.0
    lighter_price: float = 0.0
    grvt_bid: float = 0.0
    grvt_ask: float = 0.0
    grvt_next_funding_timestamp: int = 0

class OpportunityScanner:
    def __init__(self, grvt: AbstractExchange, lighter: AbstractExchange):
        self.grvt = grvt
        self.lighter = lighter
        self.opportunities: Dict[str, arbitrage_opportunity] = {}
        self.min_spread = Config.FUNDING_DIFF_THRESHOLD or 0.0001 
        self.lighter_aliases = {} # Map GRVT Symbol -> Lighter Symbol 
        self.last_full_log_time = 0

    async def scan(self) -> List[arbitrage_opportunity]:
        """
        Scans both exchanges for funding rate discrepancies.
        """
        if not self.lighter.market_rules:
            await self.lighter.load_markets()
            
        common_symbols = self._get_common_symbols()
        logger.debug(f"Scanning {len(common_symbols)} common symbols.")
        
        # Optimize: Fetch ALL GRVT tickers at once to avoid n * request latency
        try:
            grvt_tickers = await asyncio.to_thread(self.grvt.client.fetch_tickers)
        except Exception:
            grvt_tickers = {}

        results = []
        missing_symbols = []
        
        # 1. Fast Path: Process symbols found in batch
        for symbol in common_symbols:
            grvt_sym = f"{symbol}_USDT_Perp"
            grvt_ticker = grvt_tickers.get(grvt_sym)
            
            # Fallback checks
            if not grvt_ticker: grvt_ticker = grvt_tickers.get(f"{symbol}-USDT")
            if not grvt_ticker:
                 for k, v in grvt_tickers.items():
                    if k.startswith(symbol + "_") or k.startswith(symbol + "/") or k.startswith(symbol + "-"):
                        grvt_ticker = v
                        break
            
            if grvt_ticker:
                opp = await self._process_single_symbol(symbol, grvt_ticker)
                if opp: results.append(opp)
            else:
                missing_symbols.append(symbol)

        # 2. Slow Path: Parallel Fetch for missing symbols
        if missing_symbols:
            logger.info(f"Parallel fetching {len(missing_symbols)} missing symbols...")
            chunk_size = 10
            for i in range(0, len(missing_symbols), chunk_size):
                chunk = missing_symbols[i:i+chunk_size]
                chunk_tasks = []
                for sym in chunk:
                    chunk_tasks.append(self._fetch_and_process_individual_grvt(sym))
                
                chunk_results = await asyncio.gather(*chunk_tasks, return_exceptions=True)
                for res in chunk_results:
                    if isinstance(res, arbitrage_opportunity):
                        results.append(res)
                await asyncio.sleep(0.5) 

        results.sort(key=lambda x: x.spread, reverse=True)
        self.opportunities = {op.symbol: op for op in results}
        
        # --- Monitoring / Logging ---
        import time
        current_time = time.time()
        if current_time - self.last_full_log_time > 60: # Log every 60 seconds
             logger.info(f"--- Market Monitor Report ({len(results)}/{len(common_symbols)}) ---")
             if results:
                 avg_spread = sum(o.spread for o in results) / len(results)
                 logger.info(f"Avg Spread: {avg_spread:.6f}, Max Spread: {results[0].spread:.6f} ({results[0].symbol})")
             
             # Log top 5 and bottom 5 valid symbols to show we are watching
             debug_list = results[:5]
             for opp in debug_list:
                  logger.info(f"  [Top] {opp.symbol}: Spread={opp.spread:.4f}, GRVT=${opp.grvt_price}, Lighter=${opp.lighter_price}")
             
             self.last_full_log_time = current_time
        # ----------------------------

        return results

    async def _process_single_symbol(self, symbol, grvt_ticker):
        try:
             grvt_price = float(
                grvt_ticker.get('mark_price') or 
                grvt_ticker.get('index_price') or 
                grvt_ticker.get('last') or 
                grvt_ticker.get('close') or 
                0.0
             )
             grvt_bid = float(grvt_ticker.get('bid') or grvt_price)
             grvt_ask = float(grvt_ticker.get('ask') or grvt_price)

             grvt_fr = float(grvt_ticker.get('funding_rate') or 0.0)
             
             # Resolve Lighter Symbol (Handle Alias)
             lighter_sym = self.lighter_aliases.get(symbol, symbol)
             
             l_stats = await self.lighter.get_market_stats(lighter_sym)
             lighter_fr = float(l_stats.get('funding_rate') or 0.0)
             lighter_price = float(l_stats.get('price') or l_stats.get('index_price') or l_stats.get('mark_price') or 0.0)

             # Validate Prices (Filter out bad data like AVNT with 0 price)
             if grvt_price <= 0 or lighter_price <= 0:
                 # logger.debug(f"Skipping {symbol}: Invalid Price (GRVT: {grvt_price}, Lighter: {lighter_price})")
                 return None

             if symbol == 'AVNT':
                 logger.debug(f"DEBUG AVNT Ticker: {grvt_ticker}")

             grvt_full_symbol = grvt_ticker.get('instrument') or grvt_ticker.get('symbol') or f"{symbol}_USDT_Perp"
             grvt_nft = int(grvt_ticker.get('next_funding_time') or 0)
             
             # Fetch funding interval (async)
             grvt_interval = await self.grvt.get_funding_interval(symbol)
             if not grvt_interval: grvt_interval = 8
             
             return self._create_opp_object(symbol, grvt_full_symbol, grvt_fr, grvt_price, lighter_fr, lighter_price, grvt_nft, grvt_interval, grvt_bid, grvt_ask)
        except Exception:
             return None

    async def _fetch_and_process_individual_grvt(self, symbol):
        try:
            grvt_sym = f"{symbol}_USDT_Perp"
            grvt_ticker = await asyncio.to_thread(self.grvt.client.fetch_ticker, grvt_sym)
            if not grvt_ticker: return None
            return await self._process_single_symbol(symbol, grvt_ticker)
        except Exception:
            return None

    def _create_opp_object(self, symbol, grvt_sym, grvt_fr, grvt_price, lighter_fr, lighter_price, grvt_nft=0, grvt_interval=8, grvt_bid=0.0, grvt_ask=0.0):
             
             lighter_interval = 1
             lighter_interval = 1
             
             # Strategy 1.4.1: Net Yield 8h Optimization
             # Lighter (hourly) vs GRVT (interval typically 8h)
             # Calculate expected yield for the next 8 hours for both directions.
             
             # Case A: Long GRVT / Short Lighter
             # Yield = (-GRVT_Rate) + (Lighter_Rate_1h * 8)
             # GRVT Rate > 0 => Short receives, Long pays (-)
             # Lighter Rate > 0 => Short receives (+)
             yield_long_grvt = (-grvt_fr) + (lighter_fr * 8)
             
             # Case B: Short GRVT / Long Lighter
             # Yield = (GRVT_Rate) + (-Lighter_Rate_1h * 8)
             yield_short_grvt = (grvt_fr) + (-lighter_fr * 8)
             
             # Determine Best Strategy
             if yield_long_grvt > yield_short_grvt:
                 best_yield = yield_long_grvt
                 direction = 'Long_GRVT'
             else:
                 best_yield = yield_short_grvt
                 direction = 'Short_GRVT'

             # For display/sorting, we use 'spread' as the primary metric, which now equals Best Net Yield
             spread = best_yield
             
             adj_grvt = grvt_fr / grvt_interval if grvt_interval else 0
             # For display, show Lighter's 8h equivalent
             display_lighter_rate_8h = lighter_fr * 8

             return arbitrage_opportunity(
                    symbol=symbol,
                    grvt_symbol=grvt_sym,
                    lighter_symbol=symbol,
                    grvt_funding_rate=grvt_fr,
                    lighter_funding_rate=lighter_fr,
                    spread=spread,
                    net_yield_8h=best_yield,
                    estimated_annual_apy=spread * 3 * 365,
                    timestamp=time.time(),
                    direction=direction,
                    grvt_funding_interval_hours=grvt_interval,
                    lighter_funding_interval_hours=1,
                    adj_grvt_rate_1h=adj_grvt, 
                    adj_lighter_rate_1h=lighter_fr, # Store raw 1h
                    grvt_price=grvt_price,
                    lighter_price=lighter_price,
                    grvt_bid=grvt_bid,
                    grvt_ask=grvt_ask,
                    grvt_next_funding_timestamp=grvt_nft
                )

    def _get_common_symbols(self) -> List[str]:
        g_syms = set()
        for k in self.grvt.market_rules.keys():
            base = k.split('_')[0].split('-')[0]
            g_syms.add(base)
            
        l_syms = set()
        for k in self.lighter.market_rules.keys():
             l_syms.add(k.split('-')[0]) # Ensure base symbol mapping for Lighter too
        
        # Normalization for intersection (Handle 1000 vs K prefix)
        # We map normalized -> original
        g_map = {}
        for s in g_syms:
            norm = s
            if s.startswith('K') and s[1:].isupper(): # e.g. KPEPE -> 1000PEPE target
                # Check if it looks like a "K" multiplier symbol
                 norm = "1000" + s[1:]
            g_map[norm] = s

        l_map = {}
        for s in l_syms:
            norm = s
            if s.startswith('1000') and s[4:].isupper():
                # Already in 1000 format, keep it or normalize to match? 
                # If GRVT has KPEPE (norm=1000PEPE) and Lighter has 1000PEPE (norm=1000PEPE) -> Match!
                pass
            l_map[norm] = s
            
        common_norm = set(g_map.keys()).intersection(set(l_map.keys()))
        
        common = []
        for cn in common_norm:
            # We need to support the opportunity object using the original symbol that works for BOTH if possible,
            # but they differ. The scanner needs to know both.
            # Currently _process_single_symbol takes 'symbol'.
            # If we pass 'KPEPE', it will look up Lighter 'KPEPE' which fails.
            # We need to Map them.
            # Ideally, we return the GRVT symbol, and the scanner handles the translation to Lighter symbol internally.
            
            # For now, let's just use the GRVT version as the primary 'symbol' key, 
            # and we might need to update _process_single_symbol to look up the Lighter alias.
            common.append(g_map[cn])
            
            # Register Alias for Lighter if different
            if g_map[cn] != l_map[cn]:
                from ..constants import SYMBOL_ALIASES
                # This is a runtime patch, ideally specific to the instance
                # For `_process_single_symbol` to work, checking lighter market rules needs to know this alias.
                # But `_process_single_symbol` uses `lighter.get_market_stats(symbol)`.
                # We can patch `lighter.ticker_map` or add a manual alias map to OpportunityScanner.
                self.lighter_aliases[g_map[cn]] = l_map[cn]

        logger.info(f"Common Symbols Found ({len(common)}): {common[:10]}...")
        
        # Debugging: Show mismatched symbols
        unique_grvt = list(set(g_syms) - set(l_syms)) # Raw diff
        unique_lighter = list(set(l_syms) - set(g_syms)) # Raw diff
        # Note: The above raw diffs don't account for the normalization we just did, so they show the raw mismatch.
        
        if unique_grvt:
            logger.info(f"Symbols unique to GRVT ({len(unique_grvt)}): {unique_grvt[:20]}...")
        if unique_lighter:
            logger.info(f"Symbols unique to Lighter ({len(unique_lighter)}): {unique_lighter[:20]}...")

        if not common:
            logger.warning(f"No common symbols found! GRVT Keys: {list(g_syms)[:5]}..., Lighter Keys: {list(l_syms)[:5]}...")
            
        return common
