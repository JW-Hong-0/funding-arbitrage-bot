import math
from decimal import Decimal, ROUND_FLOOR
from datetime import datetime

class Utils:
    @staticmethod
    def normalize_symbol(exchange: str, symbol: str) -> str:
        """
        Normalizes symbol to exchange specific format.
        Input symbol: 'BTC', 'ETH' (Base Asset)
        
        GRVT: 'BTC_USDT_Perp'
        Lighter: 'BTC-USDT'
        """
        base = symbol.upper().split('_')[0].split('-')[0]
        
        if exchange.lower() == 'grvt':
            return f"{base}_USDT_Perp"
        elif exchange.lower() == 'lighter':
            return f"{base}-USDT"
        return base

    @staticmethod
    def to_grvt_symbol(symbol: str) -> str:
        """
        Converts 'BTC-USDT' to 'BTC_USDT_Perp'
        """
        if '_USDT_Perp' in symbol:
            return symbol
        base = symbol.split('-')[0]
        return f"{base}_USDT_Perp"

    @staticmethod
    def quantize_amount(amount: float, tick_size: float) -> float:
        """
        Rounds down amount to the nearest tick_size.
        """
        if tick_size <= 0: return amount
        
        # Avoid float precision issues by string conversion
        d_amount = Decimal(str(amount))
        d_tick = Decimal(str(tick_size))
        
        # Round down to nearest tick
        quantized = (d_amount // d_tick) * d_tick
        return float(quantized)

    @staticmethod
    def calc_precision(tick_size: float) -> int:
        """
        Calculates decimal precision from tick size.
        e.g. 0.001 -> 3
        """
        if tick_size <= 0: return 0
        return int(round(-math.log10(tick_size), 0))

    @staticmethod
    def format_funding_rate(rate) -> str:
        """
        Formats funding rate string/float to 6 decimal string.
        """
        try:
            if rate is None or rate == 'N/A':
                return 'N/A'
            val = float(rate)
            return f"{val:.6f}"
        except:
            return 'N/A'

    @staticmethod
    def format_funding_time(ts) -> str:
        """
        Formats timestamp (ns or ms) to HH:MM string, assuming 1h funding.
        User data showed GRVT: 1767830400000000000 (ns)
        Lighter: 1767823200000 (ms)
        """
        try:
            if ts is None or ts == 'N/A':
                return 'N/A'
            
            ts_int = int(ts)
            
            # Identify unit by length
            # ns > 10^18 (currently ~1.7e18)
            # ms > 10^12 (currently ~1.7e12)
            # s > 10^9
            
            # Normalize to seconds
            if ts_int > 1e16: # Nanoseconds
                ts_sec = ts_int / 1e9
            elif ts_int > 1e11: # Milliseconds
                ts_sec = ts_int / 1e3
            else:
                ts_sec = ts_int
            
            dt = datetime.fromtimestamp(ts_sec)
            return dt.strftime("%Y-%m-%d %H:%M")
        except:
            return 'N/A'
