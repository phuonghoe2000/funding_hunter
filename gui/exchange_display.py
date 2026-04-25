"""
Shared exchange display helpers for the GUI layer.
"""

from typing import Optional

from config.constants import Exchange


DISPLAY_NAME_BY_EXCHANGE = {
    Exchange.OKX: "OKX",
    Exchange.BINANCE: "Binance",
    Exchange.BINGX: "BingX",
    Exchange.GATE: "Gate.io",
    Exchange.ASTERDEX: "Asterdex",
    Exchange.BYBIT: "Bybit",
}

EXCHANGE_BY_DISPLAY_NAME = {name: exchange for exchange, name in DISPLAY_NAME_BY_EXCHANGE.items()}
DISPLAY_NAME_BY_VALUE = {exchange.value: name for exchange, name in DISPLAY_NAME_BY_EXCHANGE.items()}
TRADING_EXCHANGE_OPTIONS = list(DISPLAY_NAME_BY_EXCHANGE.values())
CASH_CARRY_EXCHANGE_OPTIONS = [
    DISPLAY_NAME_BY_EXCHANGE[Exchange.BINANCE],
    DISPLAY_NAME_BY_EXCHANGE[Exchange.ASTERDEX],
]


def to_display_name(exchange: Exchange) -> str:
    """Convert an Exchange enum to the user-facing GUI label."""
    return DISPLAY_NAME_BY_EXCHANGE.get(exchange, exchange.value.upper())


def from_display_name(name: str) -> Optional[Exchange]:
    """Convert a GUI display label back into an Exchange enum."""
    return EXCHANGE_BY_DISPLAY_NAME.get(name)


def parse_recommendation_display_names(recommendation: str) -> Optional[tuple[str, str]]:
    """
    Parse a recommendation string like ``Long okx, Short binance`` into GUI display names.
    """
    if not recommendation or recommendation == "-":
        return None

    parts = recommendation.split(", ")
    if len(parts) != 2:
        return None

    long_value = parts[0].replace("Long ", "").strip().lower()
    short_value = parts[1].replace("Short ", "").strip().lower()
    long_display = DISPLAY_NAME_BY_VALUE.get(long_value, long_value.upper())
    short_display = DISPLAY_NAME_BY_VALUE.get(short_value, short_value.upper())
    return long_display, short_display
