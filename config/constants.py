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
    ASTERDEX = "asterdex"


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
        Exchange.GATE: "BTC_USDT",
        Exchange.ASTERDEX: "BTCUSDT"
    },
    "ETH/USDT": {
        Exchange.OKX: "ETH-USDT-SWAP",
        Exchange.BINANCE: "ETHUSDT",
        Exchange.BINGX: "ETH-USDT",
        Exchange.GATE: "ETH_USDT",
        Exchange.ASTERDEX: "ETHUSDT"
    },
    "SOL/USDT": {
        Exchange.OKX: "SOL-USDT-SWAP",
        Exchange.BINANCE: "SOLUSDT",
        Exchange.BINGX: "SOL-USDT",
        Exchange.GATE: "SOL_USDT",
        Exchange.ASTERDEX: "SOLUSDT"
    },
    "XRP/USDT": {
        Exchange.OKX: "XRP-USDT-SWAP",
        Exchange.BINANCE: "XRPUSDT",
        Exchange.BINGX: "XRP-USDT",
        Exchange.GATE: "XRP_USDT",
        Exchange.ASTERDEX: "XRPUSDT"
    },
    "DOGE/USDT": {
        Exchange.OKX: "DOGE-USDT-SWAP",
        Exchange.BINANCE: "DOGEUSDT",
        Exchange.BINGX: "DOGE-USDT",
        Exchange.GATE: "DOGE_USDT",
        Exchange.ASTERDEX: "DOGEUSDT"
    },
    "ADA/USDT": {
        Exchange.OKX: "ADA-USDT-SWAP",
        Exchange.BINANCE: "ADAUSDT",
        Exchange.BINGX: "ADA-USDT",
        Exchange.GATE: "ADA_USDT",
        Exchange.ASTERDEX: "ADAUSDT"
    },
    "AVAX/USDT": {
        Exchange.OKX: "AVAX-USDT-SWAP",
        Exchange.BINANCE: "AVAXUSDT",
        Exchange.BINGX: "AVAX-USDT",
        Exchange.GATE: "AVAX_USDT",
        Exchange.ASTERDEX: "AVAXUSDT"
    },
    "LINK/USDT": {
        Exchange.OKX: "LINK-USDT-SWAP",
        Exchange.BINANCE: "LINKUSDT",
        Exchange.BINGX: "LINK-USDT",
        Exchange.GATE: "LINK_USDT",
        Exchange.ASTERDEX: "LINKUSDT"
    },
    "DOT/USDT": {
        Exchange.OKX: "DOT-USDT-SWAP",
        Exchange.BINANCE: "DOTUSDT",
        Exchange.BINGX: "DOT-USDT",
        Exchange.GATE: "DOT_USDT",
        Exchange.ASTERDEX: "DOTUSDT"
    },
    "MATIC/USDT": {
        Exchange.OKX: "MATIC-USDT-SWAP",
        Exchange.BINANCE: "MATICUSDT",
        Exchange.BINGX: "MATIC-USDT",
        Exchange.GATE: "MATIC_USDT",
        Exchange.ASTERDEX: "MATICUSDT"
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
    else:  # Binance & Asterdex
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
    Exchange.ASTERDEX: {
        "maker": 0.02,      # 0.02% (Assumption)
        "taker": 0.05,      # 0.05% (Assumption)
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
    leverage: int = 3,
    slippage_pct: float = 0.02,
    funding_interval_hours: int = 8
) -> dict:
    """Calculate break-even and expected profit for a funding arbitrage trade
    
    Args:
        long_exchange: Exchange for LONG position
        short_exchange: Exchange for SHORT position
        position_size_usd: Position size in USD (notional value)
        funding_rate_pct: Net funding rate in % (positive = we receive)
        leverage: Leverage used
        slippage_pct: Estimated slippage per side (default 0.02%)
        funding_interval_hours: Funding interval in hours (4 or 8)
    
    Returns:
        Dict with:
        - total_fees_pct: Total fees as % of position
        - total_fees_usd: Total fees in USD
        - funding_income_usd: Expected funding income per period
        - net_profit_usd: Net profit after fees (per period)
        - break_even_rate: Minimum funding rate needed to break even
        - is_profitable: True if trade is profitable
        - hours_to_break_even: Hours needed to recover fees
        - apr_pct: Annual Percentage Rate
        - daily_return_pct: Daily return percentage
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
    
    # Funding income per period
    funding_income_usd = position_size_usd * (abs(funding_rate_pct) / 100)
    
    # Net profit per period
    # Note: We subtract fees from the first funding period
    net_profit_per_period = funding_income_usd
    net_profit_first_period = funding_income_usd - total_cost_usd
    
    # Break-even funding rate (to cover fees in 1 period)
    break_even_rate = total_cost_pct
    
    # Hours to break even
    if funding_income_usd > 0:
        periods_to_break_even = total_cost_usd / funding_income_usd
        hours_to_break_even = periods_to_break_even * funding_interval_hours
    else:
        hours_to_break_even = float('inf')
    
    # Calculate APR based on funding interval
    # periods_per_day = 24 / funding_interval_hours
    # APR = funding_rate_pct * periods_per_day * 365
    periods_per_day = 24 / funding_interval_hours
    daily_return_pct = abs(funding_rate_pct) * periods_per_day
    apr_pct = daily_return_pct * 365
    
    # Net APR (after fees, assuming fees are paid once)
    # For simplicity, we amortize fees over 30 days
    fee_per_day = total_cost_pct / 30  # Assuming 30-day holding
    net_daily_return_pct = daily_return_pct - fee_per_day
    net_apr_pct = net_daily_return_pct * 365
    
    return {
        "long_fee_pct": long_fee,
        "short_fee_pct": short_fee,
        "total_fees_pct": total_fee_pct,
        "total_slippage_pct": total_slippage_pct,
        "total_cost_pct": total_cost_pct,
        "total_cost_usd": total_cost_usd,
        "funding_rate_pct": funding_rate_pct,
        "funding_interval_hours": funding_interval_hours,
        "periods_per_day": periods_per_day,
        "funding_income_usd": funding_income_usd,
        "net_profit_first_period": net_profit_first_period,
        "net_profit_per_period": net_profit_per_period,
        "break_even_rate": break_even_rate,
        "is_profitable": net_profit_first_period > 0,
        "hours_to_break_even": hours_to_break_even,
        "daily_return_pct": daily_return_pct,
        "apr_pct": apr_pct,
        "net_apr_pct": net_apr_pct,
    }


# Minimum liquidity thresholds
MIN_VOLUME_24H_USD = 1_000_000  # $1M minimum 24h volume
MIN_ORDER_BOOK_DEPTH_USD = 10_000  # $10k minimum depth at best price


def assess_liquidity(
    volume_24h: float,
    order_book_depth: float,
    position_size: float
) -> dict:
    """Assess liquidity quality for a trade
    
    Args:
        volume_24h: 24-hour trading volume in USD
        order_book_depth: Order book depth at best price in USD
        position_size: Intended position size in USD
    
    Returns:
        Dict with liquidity assessment
    """
    # Volume score (0-100)
    if volume_24h >= 100_000_000:  # $100M+
        volume_score = 100
        volume_grade = "Excellent"
    elif volume_24h >= 10_000_000:  # $10M+
        volume_score = 80
        volume_grade = "Good"
    elif volume_24h >= 1_000_000:  # $1M+
        volume_score = 60
        volume_grade = "Fair"
    elif volume_24h >= 100_000:  # $100k+
        volume_score = 40
        volume_grade = "Low"
    else:
        volume_score = 20
        volume_grade = "Very Low"
    
    # Depth score relative to position size
    depth_ratio = order_book_depth / position_size if position_size > 0 else 0
    if depth_ratio >= 10:  # Depth is 10x+ position
        depth_score = 100
        depth_grade = "Excellent"
    elif depth_ratio >= 5:
        depth_score = 80
        depth_grade = "Good"
    elif depth_ratio >= 2:
        depth_score = 60
        depth_grade = "Fair"
    elif depth_ratio >= 1:
        depth_score = 40
        depth_grade = "Tight"
    else:
        depth_score = 20
        depth_grade = "Insufficient"
    
    # Overall liquidity score
    overall_score = (volume_score + depth_score) / 2
    
    # Estimated slippage based on depth
    if depth_ratio >= 5:
        estimated_slippage = 0.01  # 0.01%
    elif depth_ratio >= 2:
        estimated_slippage = 0.02  # 0.02%
    elif depth_ratio >= 1:
        estimated_slippage = 0.05  # 0.05%
    else:
        estimated_slippage = 0.10  # 0.10%
    
    # Warning flags
    warnings = []
    if volume_24h < MIN_VOLUME_24H_USD:
        warnings.append(f"Low volume: ${volume_24h:,.0f} < ${MIN_VOLUME_24H_USD:,.0f}")
    if order_book_depth < MIN_ORDER_BOOK_DEPTH_USD:
        warnings.append(f"Thin order book: ${order_book_depth:,.0f}")
    if depth_ratio < 2:
        warnings.append(f"Position too large for depth ({depth_ratio:.1f}x)")
    
    return {
        "volume_24h": volume_24h,
        "volume_score": volume_score,
        "volume_grade": volume_grade,
        "order_book_depth": order_book_depth,
        "depth_score": depth_score,
        "depth_grade": depth_grade,
        "depth_ratio": depth_ratio,
        "overall_score": overall_score,
        "estimated_slippage": estimated_slippage,
        "is_liquid": overall_score >= 60 and len(warnings) == 0,
        "warnings": warnings,
    }


def assess_price_divergence(
    long_price: float,
    short_price: float,
    threshold_pct: float = 0.5
) -> dict:
    """Assess price divergence between two exchanges
    
    Args:
        long_price: Price on LONG exchange
        short_price: Price on SHORT exchange
        threshold_pct: Warning threshold in percentage (default 0.5%)
    
    Returns:
        Dict with divergence assessment
    """
    if long_price <= 0 or short_price <= 0:
        return {
            "divergence_pct": 0,
            "divergence_usd": 0,
            "is_acceptable": False,
            "risk_level": "Unknown",
            "warning": "Invalid prices"
        }
    
    # Calculate divergence
    price_diff = abs(long_price - short_price)
    avg_price = (long_price + short_price) / 2
    divergence_pct = (price_diff / avg_price) * 100
    
    # Risk assessment
    if divergence_pct < 0.1:
        risk_level = "Low"
        is_acceptable = True
    elif divergence_pct < 0.3:
        risk_level = "Medium"
        is_acceptable = True
    elif divergence_pct < threshold_pct:
        risk_level = "High"
        is_acceptable = True
    else:
        risk_level = "Critical"
        is_acceptable = False
    
    # Warning message
    warning = None
    if divergence_pct >= threshold_pct:
        warning = f"Price divergence {divergence_pct:.3f}% exceeds threshold {threshold_pct}%"
    elif divergence_pct >= 0.3:
        warning = f"High price divergence: {divergence_pct:.3f}%"
    
    return {
        "long_price": long_price,
        "short_price": short_price,
        "divergence_pct": divergence_pct,
        "divergence_usd": price_diff,
        "is_acceptable": is_acceptable,
        "risk_level": risk_level,
        "warning": warning,
    }


def calculate_trade_quality(
    funding_rate_pct: float,
    funding_interval_hours: int,
    long_exchange: Exchange,
    short_exchange: Exchange,
    position_size_usd: float,
    volume_24h: float = 0,
    order_book_depth: float = 0,
    long_price: float = 0,
    short_price: float = 0,
    leverage: int = 10
) -> dict:
    """Comprehensive trade quality assessment
    
    Combines break-even, liquidity, and divergence analysis into one score.
    
    Returns:
        Dict with overall trade quality assessment
    """
    # Break-even analysis
    be_result = calculate_break_even(
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        position_size_usd=position_size_usd,
        funding_rate_pct=funding_rate_pct,
        leverage=leverage,
        slippage_pct=0.02,
        funding_interval_hours=funding_interval_hours
    )
    
    # Liquidity analysis (if data provided)
    if volume_24h > 0 and order_book_depth > 0:
        liq_result = assess_liquidity(volume_24h, order_book_depth, position_size_usd)
    else:
        liq_result = {
            "overall_score": 50,
            "is_liquid": True,
            "warnings": ["Liquidity data not available"],
            "estimated_slippage": 0.02
        }
    
    # Price divergence analysis (if data provided)
    if long_price > 0 and short_price > 0:
        div_result = assess_price_divergence(long_price, short_price)
    else:
        div_result = {
            "divergence_pct": 0,
            "is_acceptable": True,
            "risk_level": "Unknown",
            "warning": None
        }
    
    # Calculate overall trade score (0-100)
    # Weighted: Profitability 40%, Liquidity 30%, Safety 30%
    
    # Profitability score
    if be_result["apr_pct"] >= 100:
        profit_score = 100
    elif be_result["apr_pct"] >= 50:
        profit_score = 80
    elif be_result["apr_pct"] >= 20:
        profit_score = 60
    elif be_result["apr_pct"] >= 10:
        profit_score = 40
    else:
        profit_score = 20
    
    # Safety score (inverse of divergence)
    if div_result["divergence_pct"] < 0.1:
        safety_score = 100
    elif div_result["divergence_pct"] < 0.2:
        safety_score = 80
    elif div_result["divergence_pct"] < 0.3:
        safety_score = 60
    elif div_result["divergence_pct"] < 0.5:
        safety_score = 40
    else:
        safety_score = 20
    
    # Overall score
    overall_score = (
        profit_score * 0.4 +
        liq_result["overall_score"] * 0.3 +
        safety_score * 0.3
    )
    
    # Trade recommendation
    if overall_score >= 80 and be_result["is_profitable"]:
        recommendation = "STRONG BUY"
        recommendation_color = "green"
    elif overall_score >= 60 and be_result["apr_pct"] >= 20:
        recommendation = "BUY"
        recommendation_color = "green"
    elif overall_score >= 40:
        recommendation = "HOLD/WAIT"
        recommendation_color = "orange"
    else:
        recommendation = "AVOID"
        recommendation_color = "red"
    
    # Collect all warnings
    all_warnings = []
    if not be_result["is_profitable"]:
        all_warnings.append(f"Not profitable in first period (break-even: {be_result['hours_to_break_even']:.1f}h)")
    all_warnings.extend(liq_result.get("warnings", []))
    if div_result.get("warning"):
        all_warnings.append(div_result["warning"])
    
    return {
        "overall_score": overall_score,
        "profit_score": profit_score,
        "liquidity_score": liq_result["overall_score"],
        "safety_score": safety_score,
        "recommendation": recommendation,
        "recommendation_color": recommendation_color,
        "break_even": be_result,
        "liquidity": liq_result,
        "divergence": div_result,
        "warnings": all_warnings,
        "apr_pct": be_result["apr_pct"],
        "net_apr_pct": be_result["net_apr_pct"],
        "is_profitable": be_result["is_profitable"],
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
    else:  # Binance & Asterdex
        # Binance format: BTCUSDT -> BTC/USDT
        base = symbol.replace("USDT", "")
        return f"{base}/USDT"
