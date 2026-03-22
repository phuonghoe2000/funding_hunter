"""
Constants and enums for Funding Hunter
"""
from enum import Enum


class Exchange(Enum):
    """Supported exchanges"""
    OKX = "okx"
    BINANCE = "binance"
    BINGX = "bingx"
    BYBIT = "bybit"
    ASTER = "aster"


class Side(Enum):
    """Order side"""
    LONG = "long"
    SHORT = "short"


class OrderType(Enum):
    """Order type"""
    MARKET = "market"
    LIMIT = "limit"


class PositionStatus(Enum):
    """Position status"""
    OPEN = "open"
    CLOSED = "closed"
    LIQUIDATED = "liquidated"
    PARTIALLY_CLOSED = "partially_closed"


class OrderStatus(Enum):
    """Order status"""
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


# Popular trading pairs for funding arbitrage
POPULAR_PAIRS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "DOT/USDT",
    "MATIC/USDT",
]

# Symbol mapping between exchanges
SYMBOL_MAP = {
    "BTC/USDT": {
        Exchange.OKX: "BTC-USDT-SWAP",
        Exchange.BINANCE: "BTCUSDT",
        Exchange.BINGX: "BTC-USDT",
        Exchange.BYBIT: "BTCUSDT",
        Exchange.ASTER: "BTCUSDT"
    },
    "ETH/USDT": {
        Exchange.OKX: "ETH-USDT-SWAP",
        Exchange.BINANCE: "ETHUSDT",
        Exchange.BINGX: "ETH-USDT",
        Exchange.BYBIT: "ETHUSDT",
        Exchange.ASTER: "ETHUSDT"
    },
    "SOL/USDT": {
        Exchange.OKX: "SOL-USDT-SWAP",
        Exchange.BINANCE: "SOLUSDT",
        Exchange.BINGX: "SOL-USDT",
        Exchange.BYBIT: "SOLUSDT",
        Exchange.ASTER: "SOLUSDT"
    },
    "XRP/USDT": {
        Exchange.OKX: "XRP-USDT-SWAP",
        Exchange.BINANCE: "XRPUSDT",
        Exchange.BINGX: "XRP-USDT",
        Exchange.BYBIT: "XRPUSDT",
        Exchange.ASTER: "XRPUSDT"
    },
    "DOGE/USDT": {
        Exchange.OKX: "DOGE-USDT-SWAP",
        Exchange.BINANCE: "DOGEUSDT",
        Exchange.BINGX: "DOGE-USDT",
        Exchange.BYBIT: "DOGEUSDT",
        Exchange.ASTER: "DOGEUSDT"
    },
    "ADA/USDT": {
        Exchange.OKX: "ADA-USDT-SWAP",
        Exchange.BINANCE: "ADAUSDT",
        Exchange.BINGX: "ADA-USDT",
        Exchange.BYBIT: "ADAUSDT",
        Exchange.ASTER: "ADAUSDT"
    },
    "AVAX/USDT": {
        Exchange.OKX: "AVAX-USDT-SWAP",
        Exchange.BINANCE: "AVAXUSDT",
        Exchange.BINGX: "AVAX-USDT",
        Exchange.BYBIT: "AVAXUSDT",
        Exchange.ASTER: "AVAXUSDT"
    },
    "LINK/USDT": {
        Exchange.OKX: "LINK-USDT-SWAP",
        Exchange.BINANCE: "LINKUSDT",
        Exchange.BINGX: "LINK-USDT",
        Exchange.BYBIT: "LINKUSDT",
        Exchange.ASTER: "LINKUSDT"
    },
    "DOT/USDT": {
        Exchange.OKX: "DOT-USDT-SWAP",
        Exchange.BINANCE: "DOTUSDT",
        Exchange.BINGX: "DOT-USDT",
        Exchange.BYBIT: "DOTUSDT",
        Exchange.ASTER: "DOTUSDT"
    },
    "MATIC/USDT": {
        Exchange.OKX: "MATIC-USDT-SWAP",
        Exchange.BINANCE: "MATICUSDT",
        Exchange.BINGX: "MATIC-USDT",
        Exchange.BYBIT: "MATICUSDT",
        Exchange.ASTER: "MATICUSDT"
    },
}


def get_exchange_symbol(pair: str, exchange: Exchange) -> str:
    """Get exchange-specific symbol from unified pair"""
    if pair in SYMBOL_MAP and exchange in SYMBOL_MAP[pair]:
        return SYMBOL_MAP[pair][exchange]
    
    # Generate symbol if not in map
    base, quote = pair.split("/")
    if exchange == Exchange.OKX:
        return f"{base}-{quote}-SWAP"
    elif exchange == Exchange.BINGX:
        return f"{base}-{quote}"
    else:  # Binance, Bybit, Aster
        return f"{base}{quote}"


def get_unified_pair(symbol: str, exchange: Exchange) -> str:
    """Get unified pair from exchange-specific symbol"""
    for pair, symbols in SYMBOL_MAP.items():
        if exchange in symbols and symbols[exchange] == symbol:
            return pair
    
    # Parse symbol if not in map
    if exchange == Exchange.OKX:
        # OKX format: BTC-USDT-SWAP -> BTC/USDT
        parts = symbol.replace("-SWAP", "").split("-")
        return f"{parts[0]}/{parts[1]}"
    elif exchange == Exchange.BINGX:
        # BingX format: BTC-USDT -> BTC/USDT
        parts = symbol.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        # Fallback: assume USDT pair
        base = symbol.replace("USDT", "").replace("-", "")
        return f"{base}/USDT"
    else:  # Binance, Bybit, Aster
        # Binance/Bybit/Aster format: BTCUSDT -> BTC/USDT
        base = symbol.replace("USDT", "")
        return f"{base}/USDT"
