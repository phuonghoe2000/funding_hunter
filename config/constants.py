"""
Constants and enums for Funding Hunter
"""
from enum import Enum


class Exchange(Enum):
    """Supported exchanges"""
    OKX = "okx"
    BINANCE = "binance"
    BINGX = "bingx"
    GATE = "gate"


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
        Exchange.GATE: "BTC_USDT"
    },
    "ETH/USDT": {
        Exchange.OKX: "ETH-USDT-SWAP",
        Exchange.BINANCE: "ETHUSDT",
        Exchange.BINGX: "ETH-USDT",
        Exchange.GATE: "ETH_USDT"
    },
    "SOL/USDT": {
        Exchange.OKX: "SOL-USDT-SWAP",
        Exchange.BINANCE: "SOLUSDT",
        Exchange.BINGX: "SOL-USDT",
        Exchange.GATE: "SOL_USDT"
    },
    "XRP/USDT": {
        Exchange.OKX: "XRP-USDT-SWAP",
        Exchange.BINANCE: "XRPUSDT",
        Exchange.BINGX: "XRP-USDT",
        Exchange.GATE: "XRP_USDT"
    },
    "DOGE/USDT": {
        Exchange.OKX: "DOGE-USDT-SWAP",
        Exchange.BINANCE: "DOGEUSDT",
        Exchange.BINGX: "DOGE-USDT",
        Exchange.GATE: "DOGE_USDT"
    },
    "ADA/USDT": {
        Exchange.OKX: "ADA-USDT-SWAP",
        Exchange.BINANCE: "ADAUSDT",
        Exchange.BINGX: "ADA-USDT",
        Exchange.GATE: "ADA_USDT"
    },
    "AVAX/USDT": {
        Exchange.OKX: "AVAX-USDT-SWAP",
        Exchange.BINANCE: "AVAXUSDT",
        Exchange.BINGX: "AVAX-USDT",
        Exchange.GATE: "AVAX_USDT"
    },
    "LINK/USDT": {
        Exchange.OKX: "LINK-USDT-SWAP",
        Exchange.BINANCE: "LINKUSDT",
        Exchange.BINGX: "LINK-USDT",
        Exchange.GATE: "LINK_USDT"
    },
    "DOT/USDT": {
        Exchange.OKX: "DOT-USDT-SWAP",
        Exchange.BINANCE: "DOTUSDT",
        Exchange.BINGX: "DOT-USDT",
        Exchange.GATE: "DOT_USDT"
    },
    "MATIC/USDT": {
        Exchange.OKX: "MATIC-USDT-SWAP",
        Exchange.BINANCE: "MATICUSDT",
        Exchange.BINGX: "MATIC-USDT",
        Exchange.GATE: "MATIC_USDT"
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
    elif exchange == Exchange.GATE:
        return f"{base}_{quote}"
    else:  # Binance
        return f"{base}{quote}"


# Trading fees for each exchange (in percentage)
# Taker fee is used for market orders (which we use)
# These are default fees - VIP levels may have lower fees
EXCHANGE_FEES = {
    Exchange.OKX: {
        "maker": 0.02,      # 0.02%
        "taker": 0.05,      # 0.05%
    },
    Exchange.BINANCE: {
        "maker": 0.02,      # 0.02%
        "taker": 0.04,      # 0.04% (with BNB discount)
    },
    Exchange.BINGX: {
        "maker": 0.02,      # 0.02%
        "taker": 0.05,      # 0.05%
    },
    Exchange.GATE: {
        "maker": 0.015,     # 0.015%
        "taker": 0.05,      # 0.05%
    },
}


def get_exchange_fee(exchange: Exchange, order_type: str = "taker") -> float:
    """Get trading fee for an exchange
    
    Args:
        exchange: The exchange
        order_type: "maker" or "taker" (default "taker" for market orders)
    
    Returns:
        Fee as percentage (e.g., 0.05 for 0.05%)
    """
    if exchange in EXCHANGE_FEES:
        return EXCHANGE_FEES[exchange].get(order_type, 0.05)
    return 0.05  # Default to 0.05% if unknown


def calculate_break_even(
    long_exchange: Exchange,
    short_exchange: Exchange,
    position_size_usd: float,
    funding_rate_pct: float,
    leverage: int = 10,
    slippage_pct: float = 0.02
) -> dict:
    """Calculate break-even and expected profit for a funding arbitrage trade
    
    Args:
        long_exchange: Exchange for LONG position
        short_exchange: Exchange for SHORT position
        position_size_usd: Position size in USD (notional value)
        funding_rate_pct: Net funding rate in % (positive = we receive)
        leverage: Leverage used
        slippage_pct: Estimated slippage per side (default 0.02%)
    
    Returns:
        Dict with:
        - total_fees_pct: Total fees as % of position
        - total_fees_usd: Total fees in USD
        - funding_income_usd: Expected funding income per 8h
        - net_profit_usd: Net profit after fees (per 8h)
        - break_even_rate: Minimum funding rate needed to break even
        - is_profitable: True if trade is profitable
        - hours_to_break_even: Hours needed to recover fees
    """
    # Get fees for each exchange (taker for market orders)
    long_fee = get_exchange_fee(long_exchange, "taker")
    short_fee = get_exchange_fee(short_exchange, "taker")
    
    # Total fees = (open + close) * 2 sides
    # Open: 1 long + 1 short
    # Close: 1 long + 1 short
    total_fee_pct = (long_fee + short_fee) * 2  # Open and close
    
    # Add slippage estimate (both sides, open and close)
    total_slippage_pct = slippage_pct * 4  # 4 trades total
    
    # Total cost
    total_cost_pct = total_fee_pct + total_slippage_pct
    total_cost_usd = position_size_usd * (total_cost_pct / 100)
    
    # Funding income per 8h period
    funding_income_usd = position_size_usd * (abs(funding_rate_pct) / 100)
    
    # Net profit per 8h
    # Note: We subtract fees from the first funding period
    net_profit_per_8h = funding_income_usd
    net_profit_first_period = funding_income_usd - total_cost_usd
    
    # Break-even funding rate (to cover fees in 1 period)
    break_even_rate = total_cost_pct
    
    # Hours to break even
    if funding_income_usd > 0:
        periods_to_break_even = total_cost_usd / funding_income_usd
        hours_to_break_even = periods_to_break_even * 8
    else:
        hours_to_break_even = float('inf')
    
    return {
        "long_fee_pct": long_fee,
        "short_fee_pct": short_fee,
        "total_fees_pct": total_fee_pct,
        "total_slippage_pct": total_slippage_pct,
        "total_cost_pct": total_cost_pct,
        "total_cost_usd": total_cost_usd,
        "funding_rate_pct": funding_rate_pct,
        "funding_income_usd": funding_income_usd,
        "net_profit_first_period": net_profit_first_period,
        "net_profit_per_8h": net_profit_per_8h,
        "break_even_rate": break_even_rate,
        "is_profitable": net_profit_first_period > 0,
        "hours_to_break_even": hours_to_break_even,
    }


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
    elif exchange == Exchange.GATE:
        # Gate format: BTC_USDT -> BTC/USDT
        parts = symbol.split("_")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return f"{symbol}/USDT"
    else:  # Binance
        # Binance format: BTCUSDT -> BTC/USDT
        base = symbol.replace("USDT", "")
        return f"{base}/USDT"
