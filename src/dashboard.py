import os
import sys
import time
from datetime import datetime, timedelta, timezone

class Dashboard:
    def __init__(self, monitor, asset_manager, trading_service):
        self.monitor = monitor
        self.asset_manager = asset_manager
        self.trading = trading_service
        self.last_print = 0

    def clear(self):
        # Determine command based on OS
        # For Windows, 'cls' is standard. 'clear' for POSIX.
        # But running inside some IDE terminals might not support it well.
        # Using ANSI escape codes is another option, but os.system is simple.
        if os.name == 'nt':
            os.system('cls')
        else:
            os.system('clear')

    def display(self):
        # Refresh rate limit (e.g., every 1s max, but controlled by main loop)
        self.clear()
        self._print_header()
        self._print_balances()
        self._print_positions()
        self._print_market_table()
        self._print_footer()

    def _print_header(self):
        print(f"==================== 🤖 MODULAR ARBITRAGE BOT ====================")
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Tick: {int(time.time())}")
        print("-" * 80)

    def _print_balances(self):
        print("💰 BALANCES")
        # AssetManager.balances structure: {'GRVT': {'total': X, 'available': Y}, ...}
        b = self.asset_manager.balances
        
        # Formatting helper
        def fmt_usd(val): return f"${float(val):,.2f}"
        
        # Header
        print(f"{'Exchange':<10} | {'Total ($)':<15} | {'Available ($)':<15}")
        print(f"{'-'*10:<10}-+-{'-'*15:<15}-+-{'-'*15:<15}")
        
        total_all = 0
        for ex, data in b.items():
            tot = data.get('total', 0)
            avail = data.get('available', 0)
            print(f"{ex:<10} | {fmt_usd(tot):<15} | {fmt_usd(avail):<15}")
            total_all += tot
            
        print(f"{'TOTAL':<10} | {fmt_usd(total_all):<15} | {'-':<15}")
        print("-" * 80)

    def _print_positions(self):
        print("📦 OPEN POSITIONS / ORDERS")
        pos = self.asset_manager.positions
        
        # Filter for active items (State != IDLE)
        active_items = {k: v for k, v in pos.items() if v.get('State', 'IDLE') != 'IDLE'}
        
        if not active_items:
            print("   (No Active Positions)")
        else:
             # Structure: ticker -> {GRVT: 10, LGHT: -10, State: 'HEDGED', GRVT_OO: 1...}
             
             for ticker, pdata in active_items.items():
                 state = pdata.get('State', 'UNKNOWN')
                 
                 # Colorize State
                 state_display = state
                 if state == 'PARTIAL_HEDGE': state_display = "⚠️ PARTIAL_HEDGE"
                 elif state == 'OPEN_ORDERS': state_display = "⏳ OPEN_ORDERS"
                 elif state == 'HEDGED': state_display = "✅ HEDGED"
                 
                 # Show quantities
                 grvt_qty = pdata.get('GRVT', 0)
                 lght_qty = pdata.get('LGHT', 0)
                 var_qty = pdata.get('VAR', 0)
                 grvt_oo = pdata.get('GRVT_OO', 0)
                 
                 # Add projected funding yield and next funding time for current hedge pair.
                 fund_str = ""
                 if state == 'HEDGED':
                     long_venue = None
                     short_venue = None
                     for venue, qty in (("GRVT", grvt_qty), ("LGHT", lght_qty), ("VAR", var_qty)):
                         if qty > 0 and long_venue is None:
                             long_venue = venue
                         if qty < 0 and short_venue is None:
                             short_venue = venue
                     if not long_venue or not short_venue:
                         signal = self.monitor.signals.get(ticker)
                         if signal:
                             long_venue = long_venue or signal.best_ask_venue
                             short_venue = short_venue or signal.best_bid_venue
                     if long_venue and short_venue:
                         l_data = self.monitor.market_data.get(ticker, {}).get(long_venue, {})
                         s_data = self.monitor.market_data.get(ticker, {}).get(short_venue, {})
                         next_times = []
                         for venue in ("GRVT", "LGHT", "VAR"):
                             vdata = self.monitor.market_data.get(ticker, {}).get(venue, {})
                             if not vdata:
                                 continue
                             t = self.monitor._estimate_next_funding_ms(vdata)
                             if t > 0:
                                 next_times.append(t)
                         next_event = min(next_times) if next_times else 0
                         proj = self.monitor._projected_yield_for_pair(
                             l_data,
                             s_data,
                             next_event_ms=next_event,
                         )
                         yield_pct = proj * 100.0
                         if next_event > 0:
                             kst = datetime.fromtimestamp(next_event / 1000, tz=timezone.utc) + timedelta(hours=9)
                             fund_str = f" {yield_pct:+.2f}% Next Funding {kst.strftime('%H:%M')} KST"
                         else:
                             fund_str = f" {yield_pct:+.2f}% Next Funding N/A"
                 
                 print(f"   [{ticker}] {state_display}{fund_str}")
                 if grvt_qty != 0: print(f"      - GRVT: {grvt_qty}")
                 if lght_qty != 0: print(f"      - LGHT: {lght_qty}")
                 if var_qty != 0:  print(f"      - VAR : {var_qty}")
                 
                 if grvt_oo > 0:
                     print(f"      - GRVT Pending Orders: {grvt_oo}")
                     
                 if state == 'PARTIAL_HEDGE':
                     net = pdata.get('Net_Qty', 0)
                     print(f"      -> NET DELTA: {net:.4f} (Recovery Needed)")
                     
        print("-" * 80)

    def _print_market_table(self):
        print(f"{'TICKER':<6} | {'Status':<15} | {'VAR Yield(Int)':<16} | {'GRVT Yield(Int)':<16} | {'LGHT Yield(Int)':<16}")
        print("-" * 90)
        
        tickers = self.monitor.tickers or []
        positions = self.asset_manager.positions or {}
        
        for ticker in tickers:
            mdata = self.monitor.market_data.get(ticker, {})
            signal = self.monitor.signals.get(ticker)
            t_info = self.monitor.ticker_info.get(ticker, {})
            
            # Helper to format yield
            def get_yield_str(ex_name):
                d = mdata.get(ex_name)
                if not d: return "N/A"
                
                # Derive Interval Hours
                interval_s = d.get('funding_interval_s', t_info.get(ex_name, {}).get('funding_interval_s', 3600))
                interval_h = int(interval_s / 3600) if interval_s else 1
                try:
                    yield_pct = d.get('funding_rate', 0.0)
                    # OR check if 'funding_rate_apr' exists
                    if 'funding_rate_apr' in d:
                        # Simple apr to yield per interval
                        apr = d['funding_rate_apr']
                        yield_pct = apr / (365 * 24 / interval_h)

                    # Determine sign
                    sign_str = "+" if yield_pct >= 0 else ""
                    
                    return f"{sign_str}{yield_pct*100:.4f}%({interval_h}h)"
                except Exception:
                    return "Err"

            var_str = get_yield_str('VAR')
            grvt_str = get_yield_str('GRVT')
            lght_str = get_yield_str('LGHT')
            
            status = "Monitoring"
            if signal and signal.is_opportunity:
                 status = f"🔥 {signal.best_ask_venue}L/{signal.best_bid_venue}S"
            elif ticker in positions:
                 status = "📦 In Position"
                 
            print(f"{ticker:<6} | {status:<15} | {var_str:<16} | {grvt_str:<16} | {lght_str:<16}")
            
        print("-" * 80)

    def _print_footer(self):
        # Show any active logs or generic footer
        # Maybe show 'Last Update: ...'
        pass
