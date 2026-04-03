from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import inf
from typing import Any, Dict, Optional

from config.constants import Exchange, assess_price_divergence
from core.opportunity import DEFAULT_TARGET_HOURS, build_directional_opportunity
from exchanges.base import Balance, FundingRate

DEFAULT_MAX_MARGIN_USAGE_PCT = 0.35
DEFAULT_MAX_DEPTH_SHARE_PCT = 0.25
DEFAULT_MIN_DEPTH_RATIO = 2.0
DEFAULT_MAX_DIVERGENCE_PCT = 0.75


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _hours_until(value: Optional[datetime]) -> Optional[float]:
    if value is None:
        return None
    now = datetime.now(timezone.utc)
    delta = value - now
    return max(0.0, delta.total_seconds() / 3600)


def estimate_order_book_depth_usd(order_book: Optional[dict[str, Any]], side: str, levels: int = 5) -> float:
    if not order_book:
        return 0.0

    depth_usd = 0.0
    for level in (order_book.get(side) or [])[:levels]:
        if len(level) < 2:
            continue
        price = _safe_float(level[0])
        quantity = _safe_float(level[1])
        depth_usd += price * quantity
    return depth_usd


def estimate_volume_usd(ticker: Optional[dict[str, Any]], fallback_price: float = 0.0) -> float:
    if not ticker:
        return 0.0
    volume = _safe_float(ticker.get("volume"))
    price = _safe_float(ticker.get("last")) or fallback_price
    if volume <= 0 or price <= 0:
        return 0.0
    return volume * price


def estimate_dynamic_slippage_pct(depth_ratio: float) -> float:
    if depth_ratio >= 10:
        return 0.01
    if depth_ratio >= 5:
        return 0.02
    if depth_ratio >= 2:
        return 0.05
    if depth_ratio >= 1:
        return 0.08
    return 0.12


@dataclass
class TradePlan:
    pair: str
    long_exchange: Exchange
    short_exchange: Exchange
    leverage: int
    requested_size_tokens: float
    requested_notional_usd: float
    recommended_size_tokens: float
    recommended_notional_usd: float
    max_notional_usd: float
    balance_cap_usd: Optional[float]
    depth_cap_usd: Optional[float]
    long_price: float
    short_price: float
    price_divergence_pct: float
    divergence_risk_level: str
    volume_24h_usd: float
    long_depth_usd: float
    short_depth_usd: float
    available_depth_usd: float
    depth_ratio: float
    estimated_slippage_pct: float
    gross_edge_pct: float
    round_trip_cost_pct: float
    net_edge_pct: float
    hours_to_break_even: float
    expected_net_pnl_next_cycle_usd: float
    carry_per_hour_pct: float
    funding_window_hours: Optional[float]
    quality_score: float
    recommendation: str
    can_open: bool
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair": self.pair,
            "long_exchange": self.long_exchange.value,
            "short_exchange": self.short_exchange.value,
            "leverage": self.leverage,
            "requested_size_tokens": self.requested_size_tokens,
            "requested_notional_usd": self.requested_notional_usd,
            "recommended_size_tokens": self.recommended_size_tokens,
            "recommended_notional_usd": self.recommended_notional_usd,
            "max_notional_usd": self.max_notional_usd,
            "balance_cap_usd": self.balance_cap_usd,
            "depth_cap_usd": self.depth_cap_usd,
            "long_price": self.long_price,
            "short_price": self.short_price,
            "price_divergence_pct": self.price_divergence_pct,
            "divergence_risk_level": self.divergence_risk_level,
            "volume_24h_usd": self.volume_24h_usd,
            "long_depth_usd": self.long_depth_usd,
            "short_depth_usd": self.short_depth_usd,
            "available_depth_usd": self.available_depth_usd,
            "depth_ratio": self.depth_ratio,
            "estimated_slippage_pct": self.estimated_slippage_pct,
            "gross_edge_pct": self.gross_edge_pct,
            "round_trip_cost_pct": self.round_trip_cost_pct,
            "net_edge_pct": self.net_edge_pct,
            "hours_to_break_even": self.hours_to_break_even,
            "expected_net_pnl_next_cycle_usd": self.expected_net_pnl_next_cycle_usd,
            "carry_per_hour_pct": self.carry_per_hour_pct,
            "funding_window_hours": self.funding_window_hours,
            "quality_score": self.quality_score,
            "recommendation": self.recommendation,
            "can_open": self.can_open,
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
        }


@dataclass
class MonitorAdvice:
    action: str
    severity: str
    reason: str
    current_net_edge_pct: float
    edge_retention_pct: Optional[float]
    expected_next_cycle_pnl_usd: float
    liquidation_distance_pct: Optional[float] = None
    liquidation_exchange: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "severity": self.severity,
            "reason": self.reason,
            "current_net_edge_pct": self.current_net_edge_pct,
            "edge_retention_pct": self.edge_retention_pct,
            "expected_next_cycle_pnl_usd": self.expected_next_cycle_pnl_usd,
            "liquidation_distance_pct": self.liquidation_distance_pct,
            "liquidation_exchange": self.liquidation_exchange,
            "warnings": list(self.warnings),
        }


def build_trade_plan(
    *,
    pair: str,
    long_exchange: Exchange,
    short_exchange: Exchange,
    long_rate_obj: FundingRate,
    short_rate_obj: FundingRate,
    long_order_book: Optional[dict[str, Any]],
    short_order_book: Optional[dict[str, Any]],
    long_ticker: Optional[dict[str, Any]] = None,
    short_ticker: Optional[dict[str, Any]] = None,
    long_balance: Optional[Balance] = None,
    short_balance: Optional[Balance] = None,
    requested_size_tokens: float = 0.0,
    leverage: int = 1,
    long_price: Optional[float] = None,
    short_price: Optional[float] = None,
    target_hours: float = DEFAULT_TARGET_HOURS,
) -> TradePlan:
    long_entry_price = _safe_float(long_price)
    if long_entry_price <= 0 and long_order_book and long_order_book.get("asks"):
        long_entry_price = _safe_float(long_order_book["asks"][0][0])

    short_entry_price = _safe_float(short_price)
    if short_entry_price <= 0 and short_order_book and short_order_book.get("bids"):
        short_entry_price = _safe_float(short_order_book["bids"][0][0])

    avg_entry_price = (long_entry_price + short_entry_price) / 2 if long_entry_price and short_entry_price else 0.0
    requested_notional_usd = requested_size_tokens * avg_entry_price if avg_entry_price > 0 else 0.0

    long_depth_usd = estimate_order_book_depth_usd(long_order_book, "asks")
    short_depth_usd = estimate_order_book_depth_usd(short_order_book, "bids")
    available_depth_usd = min(value for value in [long_depth_usd, short_depth_usd] if value > 0) if (long_depth_usd > 0 and short_depth_usd > 0) else 0.0

    depth_ratio = available_depth_usd / requested_notional_usd if requested_notional_usd > 0 else inf
    estimated_slippage_pct = estimate_dynamic_slippage_pct(depth_ratio if depth_ratio != inf else 10.0)

    metrics = build_directional_opportunity(
        pair=pair,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        long_rate_obj=long_rate_obj,
        short_rate_obj=short_rate_obj,
        target_hours=target_hours,
        slippage_pct=estimated_slippage_pct,
    )

    long_volume_usd = estimate_volume_usd(long_ticker, fallback_price=long_entry_price)
    short_volume_usd = estimate_volume_usd(short_ticker, fallback_price=short_entry_price)
    volume_24h_usd = min(value for value in [long_volume_usd, short_volume_usd] if value > 0) if (long_volume_usd > 0 and short_volume_usd > 0) else 0.0

    divergence = assess_price_divergence(long_entry_price, short_entry_price, threshold_pct=DEFAULT_MAX_DIVERGENCE_PCT)

    balance_cap_usd = None
    if long_balance and short_balance:
        balance_cap_usd = min(
            _safe_float(long_balance.available) * leverage * DEFAULT_MAX_MARGIN_USAGE_PCT,
            _safe_float(short_balance.available) * leverage * DEFAULT_MAX_MARGIN_USAGE_PCT,
        )

    depth_cap_usd = available_depth_usd * DEFAULT_MAX_DEPTH_SHARE_PCT if available_depth_usd > 0 else None

    caps = [value for value in [balance_cap_usd, depth_cap_usd] if value is not None and value > 0]
    max_notional_usd = min(caps) if caps else requested_notional_usd
    if max_notional_usd <= 0:
        max_notional_usd = requested_notional_usd

    recommended_notional_usd = min(requested_notional_usd, max_notional_usd) if requested_notional_usd > 0 else max_notional_usd
    recommended_size_tokens = recommended_notional_usd / avg_entry_price if avg_entry_price > 0 else 0.0
    expected_net_pnl_next_cycle_usd = recommended_notional_usd * (metrics.net_edge_pct / 100)
    carry_per_hour_pct = metrics.net_edge_pct / max(target_hours, 0.25)

    funding_window_hours = min(
        value for value in [
            _hours_until(getattr(long_rate_obj, "next_funding_time", None)),
            _hours_until(getattr(short_rate_obj, "next_funding_time", None)),
        ]
        if value is not None
    ) if getattr(long_rate_obj, "next_funding_time", None) or getattr(short_rate_obj, "next_funding_time", None) else None

    blockers: list[str] = []
    warnings: list[str] = []

    if metrics.net_edge_pct <= 0:
        blockers.append("Net edge is negative after fees and slippage.")
    elif metrics.net_edge_pct < 0.02:
        warnings.append("Net edge is thin after costs.")

    if requested_notional_usd > 0 and depth_ratio < 1:
        blockers.append("Requested size exceeds visible top-of-book depth.")
    elif requested_notional_usd > 0 and depth_ratio < DEFAULT_MIN_DEPTH_RATIO:
        warnings.append(f"Visible depth is only {depth_ratio:.2f}x the requested size.")

    if not divergence["is_acceptable"]:
        blockers.append(divergence.get("warning") or "Price divergence is too high.")
    elif divergence.get("warning"):
        warnings.append(divergence["warning"])

    if balance_cap_usd is not None and requested_notional_usd > balance_cap_usd:
        warnings.append(f"Requested notional exceeds conservative balance cap (${balance_cap_usd:,.2f}).")

    if depth_cap_usd is not None and requested_notional_usd > depth_cap_usd:
        warnings.append(f"Requested notional exceeds conservative depth cap (${depth_cap_usd:,.2f}).")

    if funding_window_hours is not None:
        if funding_window_hours > 6:
            warnings.append(f"Next funding is still {funding_window_hours:.1f}h away.")
        elif funding_window_hours <= 0.5:
            warnings.append("Funding window is close. Entry execution risk is higher.")

    score = 0.0
    if metrics.net_edge_pct > 0:
        score += min(40.0, max(0.0, metrics.net_edge_pct * 200))

    if depth_ratio == inf:
        score += 25.0
    elif depth_ratio >= 10:
        score += 30.0
    elif depth_ratio >= 5:
        score += 24.0
    elif depth_ratio >= 3:
        score += 18.0
    elif depth_ratio >= 2:
        score += 12.0
    elif depth_ratio >= 1:
        score += 6.0

    divergence_pct = divergence["divergence_pct"]
    if divergence_pct < 0.1:
        score += 20.0
    elif divergence_pct < 0.25:
        score += 16.0
    elif divergence_pct < 0.5:
        score += 10.0
    elif divergence_pct < DEFAULT_MAX_DIVERGENCE_PCT:
        score += 4.0

    if funding_window_hours is None:
        score += 5.0
    elif funding_window_hours <= 1:
        score += 10.0
    elif funding_window_hours <= 2:
        score += 8.0
    elif funding_window_hours <= 4:
        score += 6.0
    else:
        score += 3.0

    score = round(min(100.0, score), 1)

    if blockers:
        recommendation = "AVOID"
        can_open = False
    elif score >= 80:
        recommendation = "OPEN"
        can_open = True
    elif score >= 60:
        recommendation = "REDUCE"
        can_open = True
    elif score >= 40:
        recommendation = "WAIT"
        can_open = False
    else:
        recommendation = "AVOID"
        can_open = False

    return TradePlan(
        pair=pair,
        long_exchange=long_exchange,
        short_exchange=short_exchange,
        leverage=leverage,
        requested_size_tokens=requested_size_tokens,
        requested_notional_usd=requested_notional_usd,
        recommended_size_tokens=recommended_size_tokens,
        recommended_notional_usd=recommended_notional_usd,
        max_notional_usd=max_notional_usd,
        balance_cap_usd=balance_cap_usd,
        depth_cap_usd=depth_cap_usd,
        long_price=long_entry_price,
        short_price=short_entry_price,
        price_divergence_pct=divergence_pct,
        divergence_risk_level=divergence["risk_level"],
        volume_24h_usd=volume_24h_usd,
        long_depth_usd=long_depth_usd,
        short_depth_usd=short_depth_usd,
        available_depth_usd=available_depth_usd,
        depth_ratio=depth_ratio,
        estimated_slippage_pct=estimated_slippage_pct,
        gross_edge_pct=metrics.gross_spread_pct,
        round_trip_cost_pct=metrics.round_trip_cost_pct,
        net_edge_pct=metrics.net_edge_pct,
        hours_to_break_even=metrics.hours_to_break_even,
        expected_net_pnl_next_cycle_usd=expected_net_pnl_next_cycle_usd,
        carry_per_hour_pct=carry_per_hour_pct,
        funding_window_hours=funding_window_hours,
        quality_score=score,
        recommendation=recommendation,
        can_open=can_open,
        warnings=warnings,
        blockers=blockers,
    )


def _plan_value(trade_plan: Optional[Any], key: str, default: Any = None) -> Any:
    if trade_plan is None:
        return default
    if isinstance(trade_plan, dict):
        return trade_plan.get(key, default)
    return getattr(trade_plan, key, default)


def calculate_liquidation_distance_pct(position: Optional[Any]) -> Optional[float]:
    if position is None:
        return None

    mark_price = _safe_float(getattr(position, "mark_price", 0.0))
    liquidation_price = _safe_float(getattr(position, "liquidation_price", 0.0))
    if mark_price <= 0 or liquidation_price <= 0:
        return None

    return abs(mark_price - liquidation_price) / mark_price * 100


def build_liquidation_context(
    *,
    position: Optional[Dict[str, Any]],
    check_result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    check_result = check_result or {}
    long_position = check_result.get("long")
    short_position = check_result.get("short")
    long_distance = calculate_liquidation_distance_pct(long_position)
    short_distance = calculate_liquidation_distance_pct(short_position)

    candidates: list[tuple[str, float]] = []
    long_exchange = position.get("long_exchange") if position else None
    short_exchange = position.get("short_exchange") if position else None
    if long_distance is not None:
        exchange_name = getattr(long_exchange, "value", long_exchange) or "LONG"
        candidates.append((str(exchange_name), long_distance))
    if short_distance is not None:
        exchange_name = getattr(short_exchange, "value", short_exchange) or "SHORT"
        candidates.append((str(exchange_name), short_distance))

    nearest_exchange = None
    min_distance = None
    if candidates:
        nearest_exchange, min_distance = min(candidates, key=lambda item: item[1])

    return {
        "long_liquidation_distance_pct": long_distance,
        "short_liquidation_distance_pct": short_distance,
        "min_liquidation_distance_pct": min_distance,
        "nearest_liquidation_exchange": nearest_exchange,
    }


def build_monitor_advice(
    *,
    position: Dict[str, Any],
    trade_plan: Optional[Any],
    risk_percent: float,
    check_result: Optional[Dict[str, Any]] = None,
) -> MonitorAdvice:
    check_result = check_result or {}
    liquidation_context = build_liquidation_context(position=position, check_result=check_result)
    min_liq_distance_pct = liquidation_context["min_liquidation_distance_pct"]
    nearest_liq_exchange = liquidation_context["nearest_liquidation_exchange"]
    if check_result.get("one_side_missing"):
        return MonitorAdvice(
            action="EMERGENCY_CLOSE",
            severity="critical",
            reason=f"One hedge leg is missing on {check_result['one_side_missing'].value}.",
            current_net_edge_pct=_plan_value(trade_plan, "net_edge_pct", 0.0),
            edge_retention_pct=None,
            expected_next_cycle_pnl_usd=_plan_value(trade_plan, "expected_net_pnl_next_cycle_usd", 0.0),
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=["Possible liquidation or asymmetric fill detected."],
        )

    current_net_edge_pct = _plan_value(trade_plan, "net_edge_pct", 0.0)
    expected_cycle_pnl = _plan_value(trade_plan, "expected_net_pnl_next_cycle_usd", 0.0)
    initial_net_edge_pct = _safe_float(position.get("initial_net_edge_pct"))
    edge_retention_pct = None
    if initial_net_edge_pct > 0:
        edge_retention_pct = (current_net_edge_pct / initial_net_edge_pct) * 100

    warnings: list[str] = []
    if trade_plan:
        warnings.extend(_plan_value(trade_plan, "warnings", [])[:3])
    else:
        warnings.append("Market analytics temporarily unavailable.")

    if min_liq_distance_pct is not None and nearest_liq_exchange:
        if min_liq_distance_pct <= 5:
            warnings.insert(0, f"Liquidation distance is only {min_liq_distance_pct:.2f}% on {nearest_liq_exchange}.")
        elif min_liq_distance_pct <= 10:
            warnings.insert(0, f"Nearest liquidation distance is {min_liq_distance_pct:.2f}% on {nearest_liq_exchange}.")

    if min_liq_distance_pct is not None and nearest_liq_exchange and min_liq_distance_pct <= 1.0:
        return MonitorAdvice(
            action="REDUCE",
            severity="critical",
            reason=f"Liquidation proximity is critical at {min_liq_distance_pct:.2f}% on {nearest_liq_exchange}. Reduce immediately.",
            current_net_edge_pct=current_net_edge_pct,
            edge_retention_pct=edge_retention_pct,
            expected_next_cycle_pnl_usd=expected_cycle_pnl,
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=warnings,
        )

    if min_liq_distance_pct is not None and nearest_liq_exchange and min_liq_distance_pct <= 3.0:
        return MonitorAdvice(
            action="REDUCE",
            severity="high",
            reason=f"Liquidation proximity is tight at {min_liq_distance_pct:.2f}% on {nearest_liq_exchange}. Consider reducing now.",
            current_net_edge_pct=current_net_edge_pct,
            edge_retention_pct=edge_retention_pct,
            expected_next_cycle_pnl_usd=expected_cycle_pnl,
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=warnings,
        )

    if trade_plan is None:
        return MonitorAdvice(
            action="CONTINUE",
            severity="info",
            reason="Market analytics temporarily unavailable. Keep monitoring.",
            current_net_edge_pct=current_net_edge_pct,
            edge_retention_pct=edge_retention_pct,
            expected_next_cycle_pnl_usd=expected_cycle_pnl,
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=warnings,
        )

    if current_net_edge_pct <= 0:
        return MonitorAdvice(
            action="CLOSE_NOW",
            severity="high",
            reason="Net funding edge has turned negative after costs.",
            current_net_edge_pct=current_net_edge_pct,
            edge_retention_pct=edge_retention_pct,
            expected_next_cycle_pnl_usd=expected_cycle_pnl,
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=warnings,
        )

    if edge_retention_pct is not None and edge_retention_pct < 10:
        return MonitorAdvice(
            action="CLOSE_NOW",
            severity="high",
            reason=f"Funding edge retention dropped to {edge_retention_pct:.1f}% of the initial plan.",
            current_net_edge_pct=current_net_edge_pct,
            edge_retention_pct=edge_retention_pct,
            expected_next_cycle_pnl_usd=expected_cycle_pnl,
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=warnings,
        )

    if risk_percent >= 8:
        return MonitorAdvice(
            action="REDUCE",
            severity="high",
            reason=f"Risk is elevated at {risk_percent:.2f}%. Reduce or close if conditions worsen.",
            current_net_edge_pct=current_net_edge_pct,
            edge_retention_pct=edge_retention_pct,
            expected_next_cycle_pnl_usd=expected_cycle_pnl,
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=warnings,
        )

    if trade_plan and _plan_value(trade_plan, "recommendation") == "WAIT":
        return MonitorAdvice(
            action="REDUCE",
            severity="medium",
            reason="Carry still exists, but entry/hold quality has degraded. Consider reducing size.",
            current_net_edge_pct=current_net_edge_pct,
            edge_retention_pct=edge_retention_pct,
            expected_next_cycle_pnl_usd=expected_cycle_pnl,
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=warnings,
        )

    if trade_plan and _plan_value(trade_plan, "recommendation") == "AVOID":
        return MonitorAdvice(
            action="CLOSE_NOW",
            severity="high",
            reason="Current market quality is no longer acceptable for this hedge.",
            current_net_edge_pct=current_net_edge_pct,
            edge_retention_pct=edge_retention_pct,
            expected_next_cycle_pnl_usd=expected_cycle_pnl,
            liquidation_distance_pct=min_liq_distance_pct,
            liquidation_exchange=nearest_liq_exchange,
            warnings=warnings,
        )

    return MonitorAdvice(
        action="CONTINUE",
        severity="info",
        reason="Carry remains positive and hedge quality is still acceptable.",
        current_net_edge_pct=current_net_edge_pct,
        edge_retention_pct=edge_retention_pct,
        expected_next_cycle_pnl_usd=expected_cycle_pnl,
        liquidation_distance_pct=min_liq_distance_pct,
        liquidation_exchange=nearest_liq_exchange,
        warnings=warnings,
    )
