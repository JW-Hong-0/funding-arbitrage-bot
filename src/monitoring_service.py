import asyncio
import logging
import time
from collections import deque
from typing import Dict, Optional, List
from shared_crypto_lib.base import AbstractExchange
from shared_crypto_lib.models import MarketInfo
from src.config import Config

logger = logging.getLogger("MonitoringService")

class MonitoringService:
    def __init__(
        self,
        grvt: AbstractExchange,
        hyena: AbstractExchange,
        variational: Optional[AbstractExchange],
        log_manager=None,
    ):
        self.grvt = grvt
        self.hyena = hyena
        self.variational = variational
        self.log_manager = log_manager

        self.tickers: List[str] = []
        
        # Data Store
        self.market_data = {} # {ticker: { 'GRVT': {...}, 'HYNA': {...}, 'VAR': {...} }}
        self.ticker_info = {} # {ticker: { 'min_qty': ..., 'step_size': ... }}
        
        # Strategy Signals
        self.signals = {} # {ticker: StrategySignal}
        self.active_opportunities = set() # { ticker } for Hysteresis
        self.last_signals = {} # {ticker: StrategySignal}
        self.last_signal_ts = {} # {ticker: timestamp}
        self.signal_history = {} # {ticker: deque[(ts, long, short, score, sig)]}
        self.last_market_update_ts = 0.0

    @staticmethod
    def _normalize_funding_time_ms(raw_time: Optional[float]) -> int:
        if not raw_time:
            return 0
        try:
            val = int(raw_time)
        except (TypeError, ValueError):
            return 0
        # ns -> ms
        if val > 10**13:
            return int(val // 1_000_000)
        # seconds -> ms
        if val < 10**11:
            return int(val * 1000)
        return val

    @staticmethod
    def _convert_apr_to_interval_rate(apr: float, interval_s: int) -> float:
        try:
            rate = float(apr)
        except (TypeError, ValueError):
            return 0.0
        if abs(rate) > 1.0:
            rate = rate / 100.0
        interval = max(int(interval_s or 0), 1)
        return rate * interval / (365 * 24 * 3600)

    def _estimate_next_funding_ms(self, data: Dict, now_ms: Optional[int] = None) -> int:
        now_ms = int(now_ms or (time.time() * 1000))
        nft_raw = data.get("next_funding_time")
        nft_ms = self._normalize_funding_time_ms(nft_raw)
        interval_s = int(data.get("funding_interval_s") or 0)
        if nft_ms > 0:
            if nft_ms <= now_ms and interval_s > 0:
                step_ms = interval_s * 1000
                while nft_ms <= now_ms:
                    nft_ms += step_ms
            return nft_ms
        if interval_s <= 0:
            return 0
        if interval_s <= 3600:
            return (now_ms // 3_600_000 + 1) * 3_600_000
        return now_ms + (interval_s * 1000)

    def _projected_yield_for_pair(
        self,
        l_data: Dict,
        s_data: Dict,
        now_ms: Optional[int] = None,
        next_event_ms: Optional[int] = None,
    ) -> float:
        now_ms = int(now_ms or (time.time() * 1000))
        l_fr = float(l_data.get("funding_rate") or 0)
        s_fr = float(s_data.get("funding_rate") or 0)
        l_nft = self._estimate_next_funding_ms(l_data, now_ms)
        s_nft = self._estimate_next_funding_ms(s_data, now_ms)
        if next_event_ms is None:
            next_event_ms = min([t for t in (l_nft, s_nft) if t > 0], default=0)
        if next_event_ms <= 0:
            return 0.0
        yield_sum = 0.0
        tol_ms = int(getattr(Config, "FUNDING_TIME_TOLERANCE_MS", 0) or 0)
        if l_nft and l_nft <= next_event_ms + tol_ms:
            yield_sum += -l_fr
        if s_nft and s_nft <= next_event_ms + tol_ms:
            yield_sum += s_fr
        return yield_sum

    @staticmethod
    def _is_event_window(now_ms: int) -> bool:
        window_min = float(getattr(Config, "STRATEGY_SWITCH_EVENT_WINDOW_MIN", 0) or 0)
        if window_min <= 0:
            return False
        window_ms = int(window_min * 60 * 1000)
        return (now_ms % 3_600_000) <= window_ms
        
    async def initialize(self):
        logger.info("📡 Monitoring Service Initializing...")
        self.tickers = self._build_ticker_list()
        logger.info(f"📌 Target tickers: {self.tickers}")
        await self._load_ticker_info()
        
        # Subscribe to WebSockets
        logger.info("📡 Subscribing to Market Data Streams...")
        count = 0
        for t in self.tickers:
            # GRVT (Optional if REST works, but good for latency)
            grvt_sym = f"{t}_USDT_Perp"
            if grvt_sym in self.grvt.markets:
                 await self.grvt.subscribe_ticker(grvt_sym, self._on_grvt_update)
                 count += 1
                 
        logger.info(f"📡 Ticker Info Loaded. Subscribed to {count} streams.")

    @staticmethod
    def _extract_base_symbol(symbol: str) -> str:
        if not symbol:
            return ""
        cleaned = symbol.strip().upper()
        if ":" in cleaned:
            cleaned = cleaned.split(":", 1)[1]
        for sep in ("-", "/", "_"):
            if sep in cleaned:
                return cleaned.split(sep, 1)[0]
        return cleaned

    def _build_ticker_list(self) -> List[str]:
        raw_symbols = Config.SYMBOLS or []
        if not isinstance(raw_symbols, (list, tuple, set)):
            raw_symbols = [raw_symbols]

        if not raw_symbols:
            legacy_symbol = getattr(Config, "SYMBOL", "")
            raw_symbols = [legacy_symbol] if legacy_symbol else []

        exclude = Config.SYMBOL_EXCLUDE or []
        exclude_set = {str(s).strip().upper() for s in exclude if str(s).strip()}
        auto = bool(getattr(Config, "AUTO_SYMBOLS", False)) or not raw_symbols

        tickers: List[str] = []
        if not auto:
            seen = set()
            for sym in raw_symbols:
                base = self._extract_base_symbol(str(sym))
                if not base or base in exclude_set or base in seen:
                    continue
                seen.add(base)
                tickers.append(base)
            if tickers:
                return tickers

        # Auto discovery: include symbols listed on >= N exchanges.
        min_ex = int(getattr(Config, "AUTO_SYMBOLS_MIN_EXCHANGES", 2) or 2)
        counts: Dict[str, int] = {}

        def _add_symbol_list(symbols: List[str]):
            for sym in symbols:
                base = self._extract_base_symbol(sym)
                if not base or base in exclude_set:
                    continue
                counts[base] = counts.get(base, 0) + 1

        if self.grvt and getattr(self.grvt, "markets", None):
            _add_symbol_list(list(self.grvt.markets.keys()))
        if self.hyena and getattr(self.hyena, "markets", None):
            _add_symbol_list(list(self.hyena.markets.keys()))
        if self.variational and getattr(self.variational, "markets", None):
            _add_symbol_list(list(self.variational.markets.keys()))

        tickers = sorted([k for k, v in counts.items() if v >= min_ex])
        max_symbols = int(getattr(Config, "AUTO_SYMBOLS_MAX", 0) or 0)
        if max_symbols > 0:
            tickers = tickers[:max_symbols]
        if not tickers:
            default_tickers = ["AVNT", "IP", "BERA", "RESOLV", "LINK"]
            tickers = [t for t in default_tickers if t not in exclude_set]
        return tickers

    async def _on_hyna_update(self, ticker_obj):
        # Hyena adapter currently uses polling only; no-op placeholder.
        pass

    async def _on_grvt_update(self, ticker_obj):
        pass
        
    async def _load_ticker_info(self):
        """Load static ticker info from Shared Exchange Models"""
        for t in self.tickers:
            self.ticker_info[t] = {}
            
            # 1. GRVT
            grvt_sym = f"{t}_USDT_Perp"
            if grvt_sym in self.grvt.markets:
                m: MarketInfo = self.grvt.markets[grvt_sym]
                self.ticker_info[t]['GRVT'] = {
                    'min_qty': m.min_qty,
                    'step_size': m.qty_step,
                    'price_tick': m.price_tick,
                    'min_notional': getattr(m, "min_notional", None),
                    'funding_interval_s': getattr(m, "funding_interval_s", None)
                }
            
            # 2. Hyena
            if self.hyena:
                hyna_sym = t
                pref = f"{getattr(self.hyena, 'dex_id', 'hyna')}:{t}"
                if pref in self.hyena.markets:
                    hyna_sym = pref
                if hyna_sym in self.hyena.markets:
                    m: MarketInfo = self.hyena.markets[hyna_sym]
                    self.ticker_info[t]['HYNA'] = {
                        'min_qty': m.min_qty,
                        'step_size': m.qty_step,
                        'price_tick': m.price_tick,
                        'min_notional': getattr(m, "min_notional", None),
                        'funding_interval_s': getattr(m, "funding_interval_s", None)
                    }

            # 3. Variational
            if self.variational and t in self.variational.markets:
                 m: MarketInfo = self.variational.markets[t]
                 interval_s = None
                 try:
                     if hasattr(self.variational, "_asset_info_cache"):
                         data_list = self.variational._asset_info_cache.get(t.upper())
                         if data_list and isinstance(data_list, list) and data_list:
                             interval_s = data_list[0].get("funding_interval_s")
                 except Exception:
                     interval_s = None
                 self.ticker_info[t]['VAR'] = {
                    'min_qty': m.min_qty,
                    'step_size': m.qty_step,
                    'price_tick': m.price_tick,
                    'min_notional': getattr(m, "min_notional", None),
                    'funding_interval_s': interval_s
                 }
        
        logger.info(f"Loaded Info for {len(self.ticker_info)} tickers.")

    async def update_market_data(self):
        """Fetch latest prices and funding rates"""
        sample_interval = float(getattr(Config, "MARKET_SAMPLE_INTERVAL_S", 0) or 0)
        now_ts = time.time()
        if sample_interval > 0 and (now_ts - self.last_market_update_ts) < sample_interval:
            return
        self.last_market_update_ts = now_ts
        tasks = []
        for t in self.tickers:
            tasks.append(self._fetch_ticker_data(t))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for res in results:
            if isinstance(res, tuple) and len(res) == 2:
                ticker, data = res
                self.market_data[ticker] = data
                if self.log_manager:
                    for ex_name, ex_data in data.items():
                        if not ex_data:
                            continue
                        self.log_manager.log_funding(
                            {
                                "exchange": ex_name,
                                "symbol": ticker,
                                "funding_rate": ex_data.get("funding_rate"),
                                "funding_time": "",
                                "next_funding_time": ex_data.get("next_funding_time"),
                            }
                        )
            elif isinstance(res, Exception):
                logger.error(f"Error fetching ticker data: {res}")
                
        self.calculate_strategies()
        logger.info(f"Updated Market Data & Signals for {len(self.market_data)} tickers.")
        
    async def _fetch_ticker_data(self, ticker):
        """Fetch data for a single ticker from all exchanges via Shared Lib"""
        data = {'GRVT': {}, 'HYNA': {}, 'VAR': {}}
        
        # 1. GRVT
        try:
            grvt_sym = f"{ticker}_USDT_Perp"
            t_obj = await self.grvt.fetch_ticker(grvt_sym)
            f_obj = await self.grvt.fetch_funding_rate(grvt_sym)
            
            if t_obj:
                fr = 0.0
                nxt = 0
                interval_s = 3600
                try:
                    m = self.grvt.markets.get(grvt_sym)
                    if m is not None:
                        interval_s = getattr(m, "funding_interval_s", interval_s) or interval_s
                        if not getattr(m, "funding_interval_s", None):
                            raw = getattr(m, "raw", None) or {}
                            fi_hours = raw.get("funding_interval_hours")
                            if fi_hours:
                                interval_s = int(float(fi_hours) * 3600)
                except Exception:
                    interval_s = 3600
                if f_obj:
                    fr = f_obj.rate / 100.0
                    nxt = self._normalize_funding_time_ms(f_obj.next_funding_time)
                
                data['GRVT'] = {
                    'price': t_obj.last,
                    'bid': t_obj.bid,
                    'ask': t_obj.ask,
                    'funding_rate': fr,
                    'next_funding_time': nxt, # Strategy Logic uses ms
                    'funding_interval_s': interval_s 
                }
        except Exception as e:
            logger.debug(f"[{ticker}] GRVT Data Fail: {e}")

        # 2. Hyena
        try:
            if self.hyena and getattr(self.hyena, "markets", None):
                dex_id = getattr(self.hyena, "dex_id", "hyna")
                pref = f"{dex_id}:{ticker}"
                hyna_sym = None
                if pref in self.hyena.markets:
                    hyna_sym = pref
                elif ticker in self.hyena.markets:
                    hyna_sym = ticker

                if hyna_sym:
                    t_obj = await self.hyena.fetch_ticker(hyna_sym)
                    if t_obj and t_obj.last and float(t_obj.last) > 0:
                        f_obj = await self.hyena.fetch_funding_rate(hyna_sym)
                        fr = 0.0
                        nxt = 0
                        interval_s = 3600
                        try:
                            m = self.hyena.markets.get(hyna_sym)
                            if m is not None:
                                interval_s = getattr(m, "funding_interval_s", interval_s) or interval_s
                        except Exception:
                            interval_s = 3600
                        if f_obj:
                            fr = float(f_obj.rate or 0.0)
                            nxt = self._normalize_funding_time_ms(f_obj.next_funding_time)
                        data['HYNA'] = {
                            'price': t_obj.last,
                            'bid': t_obj.bid,
                            'ask': t_obj.ask,
                            'funding_rate': fr,
                            'next_funding_time': nxt,
                            'funding_interval_s': interval_s
                        }
        except Exception as e:
             logger.debug(f"[{ticker}] HYNA Data Fail: {e}")
             
        # 3. Variational
        try:
            if self.variational:
                 t_obj = await self.variational.fetch_ticker(ticker) # VAR uses simple ticker
                 f_obj = await self.variational.fetch_funding_rate(ticker)
                 
                 if t_obj:
                     fr = 0.0
                     nxt = 0
                     interval_s = 28800
                     try:
                         data_list = None
                         if hasattr(self.variational, "_asset_info_cache"):
                             data_list = self.variational._asset_info_cache.get(ticker.upper())
                         if data_list and isinstance(data_list, list) and data_list:
                             interval_s = int(data_list[0].get("funding_interval_s") or interval_s)
                     except Exception:
                         interval_s = 28800
                     if f_obj:
                         # VAR funding_rate is returned in bps; convert to decimal.
                         fr = float(f_obj.rate or 0.0) / float(getattr(Config, "VAR_FUNDING_RATE_DIVISOR", 10000.0))
                         if getattr(Config, "VAR_FUNDING_RATE_HOURLY", False):
                             fr *= float(interval_s) / 3600.0
                         nxt = self._normalize_funding_time_ms(f_obj.next_funding_time)
                         
                     data['VAR'] = {
                        'price': t_obj.last,
                        'bid': t_obj.bid,
                        'ask': t_obj.ask,
                        'funding_rate': fr,
                        'next_funding_time': nxt, # Strategy uses ms
                        'funding_interval_s': interval_s
                     }
        except Exception as e:
            logger.debug(f"[{ticker}] VAR Data Fail: {e}")
            
        return ticker, data

    def _calculate_strategies_legacy(self):
        """
        Derive optimal strategy based on funding rates and Next Funding Time.
        Analyzes mixed funding cycles by projecting cashflow for the next funding event.
        """
        self.signals = {}
        now = time.time() * 1000 # ms
        
        for ticker, mdata in self.market_data.items():
            # 1. Gather Prices and Funding Info
            # Structure: { 'price': float, 'funding_rate': float, 'next_funding_time': ms }
            exchanges = {}
            for ex_name in ['GRVT', 'HYNA', 'VAR']:
                if mdata.get(ex_name) and mdata[ex_name].get('price', 0) > 0:
                    exchanges[ex_name] = mdata[ex_name]
            
            if len(exchanges) < 2:
                continue

            # 2. Find Best Long (Lowest Ask) and Best Short (Highest Bid)
            # Simplification: Using 'price' as mid, spread logic handled by execution or assumed small.
            # Ideally we need 'bid' and 'ask'.
            # _fetch_ticker_data currently fetches 'price'. 
            # We should assume 'price' is a reliable Mid or Last.
            # TODO: Improve to fetch Bid/Ask for precise entry.
            
            sorted_by_price = sorted(exchanges.items(), key=lambda x: x[1]['price'])
            best_long = sorted_by_price[0] # (name, data) - Lowest Price
            best_short = sorted_by_price[-1] # (name, data) - Highest Price
            
            long_venue = best_long[0]
            short_venue = best_short[0]
            
            if long_venue == short_venue:
                continue
                
            entry_spread_pct = (best_short[1]['price'] - best_long[1]['price']) / best_long[1]['price']
            
            # 3. Calculate Projected Yield for Next Hour (Mixed Cycle Logic)
            # If I Long X and Short Y:
            # Check if X pays/receives in next 1h.
            # Check if Y pays/receives in next 1h.
            
            l_data = best_long[1]
            s_data = best_short[1]
            projected_yield = self._projected_yield_for_pair(l_data, s_data, now_ms=now)

            # 4. Create Signal
            sig = StrategySignal(ticker, long_venue, short_venue, entry_spread_pct, projected_yield)
            
            # Determine if "Opportunity"
            # e.g., Yield > 0.0001 (1bp) OR Spread > 0.001 (0.1%)
            # User Request: Lower to 0.005% (0.00005)
            if projected_yield > 0.00005 or entry_spread_pct > 0.002:
                 sig.is_opportunity = True
                 
            self.signals[ticker] = sig

    def calculate_strategies(self):
        """
        Derive optimal strategy with Priority Logic (GRVT Main) and Hysteresis.
        """
        import itertools
        self.signals = {}
        now = time.time() * 1000 # ms
        switch_cooldown_ms = int(getattr(Config, "STRATEGY_SWITCH_COOLDOWN_S", 30) * 1000)
        switch_delta = float(getattr(Config, "STRATEGY_SWITCH_DELTA_BPS", 5) or 0) / 10000.0
        switch_window_ms = int(getattr(Config, "STRATEGY_SWITCH_WINDOW_S", 15) * 1000)
        switch_confirm = int(getattr(Config, "STRATEGY_SWITCH_CONFIRM_COUNT", 5) or 0)
        spread_mult = float(getattr(Config, "STRATEGY_SWITCH_SPREAD_MULTIPLIER", 2.0) or 0.0)
        if self._is_event_window(int(now)):
            spread_mult = float(
                getattr(Config, "STRATEGY_SWITCH_SPREAD_MULTIPLIER_EVENT", spread_mult) or spread_mult
            )
        score_eps = 1e-9
        
        for ticker, mdata in self.market_data.items():
            # 1. Gather Valid Venues
            exchanges = {}
            for ex_name in ['GRVT', 'HYNA', 'VAR']:
                if mdata.get(ex_name) and mdata[ex_name].get('price', 0) > 0:
                    exchanges[ex_name] = mdata[ex_name]
            
            if len(exchanges) < 2:
                # If ticker was active but now not enough exchanges, clear active state
                self.active_opportunities.discard(ticker)
                continue

            # 1.5 Determine venues that do NOT participate in the next funding event.
            tol_ms = int(getattr(Config, "FUNDING_TIME_TOLERANCE_MS", 0) or 0)
            next_times = {}
            for ex_name, ex_data in exchanges.items():
                next_times[ex_name] = self._estimate_next_funding_ms(ex_data, int(now))
            valid_times = [t for t in next_times.values() if t > 0]
            next_event_ms = min(valid_times) if valid_times else 0
            zero_venues = set()
            if next_event_ms > 0:
                for ex_name, t in next_times.items():
                    if t > next_event_ms + tol_ms:
                        zero_venues.add(ex_name)

            def _build_sig(l_name, s_name, is_active_flag):
                l_data = exchanges[l_name]
                s_data = exchanges[s_name]
                entry_spread_pct = (s_data['price'] - l_data['price']) / l_data['price']

                projected_yield = self._projected_yield_for_pair(
                    l_data,
                    s_data,
                    now_ms=now,
                    next_event_ms=next_event_ms,
                )
                base_score = projected_yield
                priority_bonus = 0.0005 if 'GRVT' in [l_name, s_name] else 0.0
                total_score = base_score + priority_bonus

                sig = StrategySignal(ticker, l_name, s_name, entry_spread_pct, projected_yield)
                yield_thresh = 0.00003 if is_active_flag else 0.00005
                if projected_yield > yield_thresh:
                    sig.is_opportunity = True
                return sig, total_score

            # 2. Evaluate All Combinations
            best_sig = None
            best_score = -999.0
            venue_list = list(exchanges.keys())
            
            for l_name, s_name in itertools.permutations(venue_list, 2):
                if zero_venues and l_name not in zero_venues and s_name not in zero_venues:
                    continue
                if zero_venues and l_name in zero_venues and s_name in zero_venues:
                    continue
                is_active = ticker in self.active_opportunities
                sig, total_score = _build_sig(l_name, s_name, is_active)
                if best_sig is None or total_score > best_score + score_eps:
                    best_score = total_score
                    best_sig = sig
                elif best_sig and abs(total_score - best_score) <= score_eps:
                    best_has_grvt = "GRVT" in [best_sig.best_ask_venue, best_sig.best_bid_venue]
                    cand_has_grvt = "GRVT" in [sig.best_ask_venue, sig.best_bid_venue]
                    if cand_has_grvt and not best_has_grvt:
                        best_score = total_score
                        best_sig = sig
            
            if best_sig:
               selected_sig = best_sig
               best_score_used = best_score
               last_sig = self.last_signals.get(ticker)
               last_ts = self.last_signal_ts.get(ticker, 0)
               zero_delta = float(getattr(Config, "ZERO_VENUE_SWITCH_DELTA_BPS", 0) or 0) / 10000.0

               # Stabilize signals using recent samples (anti-whipsaw).
               if switch_confirm > 0 and switch_window_ms > 0:
                   history = self.signal_history.setdefault(ticker, deque())
                   history.append((now, best_sig.best_ask_venue, best_sig.best_bid_venue, best_score, best_sig))
                   while history and (now - history[0][0]) > switch_window_ms:
                       history.popleft()
                   if len(history) >= switch_confirm:
                       tail = list(history)[-switch_confirm:]
                       pair = (tail[-1][1], tail[-1][2])
                       if all((item[1], item[2]) == pair for item in tail):
                           selected_sig = tail[-1][4]
                           best_score_used = tail[-1][3]
                       elif last_sig:
                           selected_sig = last_sig
                   elif last_sig:
                       selected_sig = last_sig

               if last_sig and (
                   last_sig.best_ask_venue != best_sig.best_ask_venue
                   or last_sig.best_bid_venue != best_sig.best_bid_venue
               ):
                   if last_sig.best_ask_venue in exchanges and last_sig.best_bid_venue in exchanges:
                       is_active = ticker in self.active_opportunities
                       prev_sig, prev_score = _build_sig(last_sig.best_ask_venue, last_sig.best_bid_venue, is_active)
                       required_delta = switch_delta
                       if zero_venues and (
                           last_sig.best_ask_venue in zero_venues
                           or last_sig.best_bid_venue in zero_venues
                       ):
                           required_delta = max(required_delta, zero_delta)
                       if (now - last_ts) < switch_cooldown_ms or best_score_used < (prev_score + required_delta):
                           selected_sig = prev_sig

               # Finalize Active State based on Selected Best Signal
               if selected_sig.is_opportunity:
                   self.active_opportunities.add(ticker)
               else:
                   self.active_opportunities.discard(ticker)

               if not last_sig or (
                   selected_sig.best_ask_venue != last_sig.best_ask_venue
                   or selected_sig.best_bid_venue != last_sig.best_bid_venue
               ):
                   self.last_signal_ts[ticker] = now
                   if zero_venues and getattr(Config, "ZERO_VENUE_LOG", False):
                       logger.info(
                           f"[{ticker}] zero_venues={sorted(zero_venues)} "
                           f"selected={selected_sig.best_ask_venue}/{selected_sig.best_bid_venue}"
                       )
               self.last_signals[ticker] = selected_sig
               self.signals[ticker] = selected_sig
               if self.log_manager:
                   self.log_manager.log_signal(
                       {
                           "symbol": ticker,
                           "leg_long": selected_sig.best_ask_venue,
                           "leg_short": selected_sig.best_bid_venue,
                           "spread": selected_sig.spread,
                           "projected_yield": selected_sig.projected_yield,
                           "decision": "opportunity" if selected_sig.is_opportunity else "skip",
                           "reason": "threshold_met" if selected_sig.is_opportunity else "below_threshold",
                       }
                   )

class StrategySignal:
    def __init__(self, ticker, best_ask_venue, best_bid_venue, spread, projected_yield):
        self.ticker = ticker
        self.best_ask_venue = best_ask_venue # Buying Venue (Long)
        self.best_bid_venue = best_bid_venue # Selling Venue (Short)
        self.spread = spread
        self.projected_yield = projected_yield
        self.is_opportunity = False
        
    def __repr__(self):
        return f"[{self.ticker}] L:{self.best_ask_venue} S:{self.best_bid_venue} Sprd:{self.spread*100:.2f}% Yld:{self.projected_yield*100:.4f}%"
