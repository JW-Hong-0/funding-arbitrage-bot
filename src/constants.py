# Lighter Market ID Mapping
# Extracted from Arbitrage_V01_6/settings.py

LIGHTER_MARKET_IDS = {
    "ETH": 0,
    "WBTC": 1,
    "SOL": 4,
    "TIA": 52,
    "ZORA": 53,
    "LINK": 54,
    "OP": 55,
    "ZK": 56,
    "APE": 57,
    "ENA": 58,
    "ARB": 59,
    "ZRO": 60,
    "ORDI": 61,
    "WLD": 62,
    "WIF": 63,
    "PYTH": 78,
    "SEI": 79,
    "DOGE": 80,
    "PEPE": 81,  # KPEPE in reference, mapping to PEPE for general usage
    "GMX": 82,
    "LDO": 83,
    "CRV": 84,
    "PENDLE": 85,
    "NEAR": 86,
    "SUI": 87,
    "TAO": 88,
    "ZEC": 90,
    "LIT": 120,
}

# GRVT Ticker Map (Base -> Instrument)
# Note: Testnet usually has different suffixes, but for now assuming standard
GRVT_TICKER_MAP = {
    "BTC": "BTC_USDT_Perp",
    "ETH": "ETH_USDT_Perp",
    "SOL": "SOL_USDT_Perp",
    "ZK": "ZK_USDT_Perp",
    "LIT": "LIT_USDT_Perp",
    # Add others as needed
}

# Lighter Ticker Map (Base -> Symbol)
LIGHTER_TICKER_MAP = {
    "BTC": "BTC-USDT",
    "ETH": "ETH-USDC",
    "SOL": "SOL-USDC",
    "ZK": "ZK-USDC",
}

# Mapping common variations to standard keys
SYMBOL_ALIASES = {
    "BTC": "WBTC",
    "KPEPE": "PEPE",
    "1000PEPE": "PEPE" 
}

# Symbol Specific Metadata (Fallback/Override)
# Explicitly requested by user due to API data issues
SYMBOL_METADATA = {
    "ETH-USDT": {"min_qty": "0.0001", "max_leverage": "50"},
    "LIT-USDT": {"min_qty": "0.01", "max_leverage": "5"},
    "XRP-USDT": {"min_qty": "1.00", "max_leverage": "20"},
}
