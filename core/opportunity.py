from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from config.constants import Exchange, calculate_break_even
from exchanges.base import FundingRate

DEFAULT_TARGET_HOURS = 4.0
DEFAULT_SLIPPAGE_PCT = 0.02


def normalize_funding_rate(
    funding_rate: float,
    interval_hours: Optional[int],
    target_hours: float = DEFAULT_TARGET_HOURS,
) -> float:
    """Normalize a funding rate to a common interval for fair comparison."""
    interval = max(1, int(interval_hours or 8))
    return funding_rate * (target_hours / interval)


def normalize_funding_rate_obj(
    rate_obj: FundingRate,
    target_hours: float = DEFAULT_TARGET_HOURS,
) -> float:
    interval = getattr(rate_obj, "funding_interval_hours", 8) or 8
    return normalize_funding_rate(rate_obj.funding_rate, interval, target_hours=target_hours)


@dataclass
class OpportunityMetrics:
    pair: str
    long_exchange: Exchange
    short_exchange: Exchange
    long_rate: float
    short_rate: float
    long_interval_hours: int
    short_interval_hours: int
    long_norm_rate: float
    short_norm_rate: float
    gross_spread_pct: float
    round_trip_cost_pct: float
    net_edge_pct: float
    hours_to_break_even: float
    profitable_after_costs: bool
    target_hours: float
    next_funding_time_long: Optional[datetime]
    next_funding_time_short: Optional[datetime]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair": self.pair,
            "long_exchange": self.long_exchange.value,
            "short_exchange": self.short_exchange.value,
            "long_rate": self.long_rate,
            "short_rate": self.short_rate,
            "long_interval_hours": self.long_interval_hours,
            "short_interval_hours": self.short_interval_hours,
            "long_norm_rate": self.long_norm_rate,
            "short_norm_rate": self.short_norm_rate,
            "gross_spread_pct": self.gross_spread_pct,
            "round_trip_cost_pct": self.round_trip_cost_pct,
            "net_edge_pct": self.net_edge_pct,
            "hours_to_break_even": self.hours_to_break_even,
            "profitable_after_costs": self.profitable_after_costs,
            "target_hours": self.target_hours,
            "next_funding_time_long": self.next_funding_time_long,
            "next_funding_time_short": self.next_funding_time_short,
        }


def build_directional_opportunity(
    pair: str,
    long_exchange: Exchange,
    short_exchange: Exchange,
    long_rate_obj: FundingRate,
    short_rate_obj: FundingRate,
    target_hours: float = DEFAULT_TARGET_HOURS,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> OpportunityMetrics:
    """Evaluate the funding economics for a specific long/short direction."""
    long_interval = getattr(long_rate_obj, "funding_interval_hours", 8) or 8
    short_interval = getattr(short_rate_obj, "funding_interval_hours", 8) or 8

    long_norm_rate = normalize_funding_rate_obj(long_rate_obj, target_hours=target_hours)
    short_norm_rate = normalize_funding_rate_obj(short_rate_obj, target_hours=target_hours)
    gross_spread_pct = (short_norm_rate - long_norm_rate) * 100

    break_even = calculate_break_even(
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        position_size_usd=1000.0,
        funding_rate_pct=gross_spread_pct,
        leverage=1,
        slippage_pct=slippage_pct,
        funding_interval_hours=max(1, int(round(target_hours))),
    )

    round_trip_cost_pct = break_even["total_cost_pct"]
    net_edge_pct = gross_spread_pct - round_trip_cost_pct

    return OpportunityMetrics(
        pair=pair,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        long_rate=long_rate_obj.funding_rate,
        short_rate=short_rate_obj.funding_rate,
        long_interval_hours=long_interval,
        short_interval_hours=short_interval,
        long_norm_rate=long_norm_rate,
        short_norm_rate=short_norm_rate,
        gross_spread_pct=gross_spread_pct,
        round_trip_cost_pct=round_trip_cost_pct,
        net_edge_pct=net_edge_pct,
        hours_to_break_even=break_even["hours_to_break_even"],
        profitable_after_costs=net_edge_pct > 0,
        target_hours=target_hours,
        next_funding_time_long=getattr(long_rate_obj, "next_funding_time", None),
        next_funding_time_short=getattr(short_rate_obj, "next_funding_time", None),
    )


def build_best_opportunity(
    pair: str,
    exchange_rates: Dict[Exchange, FundingRate],
    target_hours: float = DEFAULT_TARGET_HOURS,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> Optional[OpportunityMetrics]:
    """Pick the best long/short direction for a pair across available exchanges."""
    normalized_rates = []
    for exchange, rate_obj in exchange_rates.items():
        if rate_obj is None:
            continue
        normalized_rates.append((exchange, rate_obj, normalize_funding_rate_obj(rate_obj, target_hours=target_hours)))

    if len(normalized_rates) < 2:
        return None

    normalized_rates.sort(key=lambda item: item[2])
    long_exchange, long_rate_obj, _ = normalized_rates[0]
    short_exchange, short_rate_obj, _ = normalized_rates[-1]

    return build_directional_opportunity(
        pair=pair,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        long_rate_obj=long_rate_obj,
        short_rate_obj=short_rate_obj,
        target_hours=target_hours,
        slippage_pct=slippage_pct,
    )
