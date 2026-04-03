"""
Display formatting helpers for the Funding Hunter GUI.
"""

from typing import Any

from config.constants import Exchange
from core.opportunity import build_best_opportunity, normalize_funding_rate_obj


def build_funding_table_rows(all_rates: dict[str, dict[Exchange, Any]], limit: int = 10) -> tuple[list[tuple], int]:
    """Build sorted funding table rows for the GUI tree view."""
    pairs_with_spreads = []
    for pair, rates in all_rates.items():
        metrics = build_best_opportunity(pair, rates)
        net_edge = metrics.net_edge_pct if metrics else float("-inf")
        pairs_with_spreads.append((pair, rates, metrics, net_edge))

    sorted_pairs = sorted(pairs_with_spreads, key=lambda item: item[3], reverse=True)
    rows = []

    for pair, rates, metrics, _ in sorted_pairs[:limit]:
        def fmt_rate(exchange: Exchange) -> str:
            rate_obj = rates.get(exchange)
            if not rate_obj:
                return "-"
            normalized_rate = normalize_funding_rate_obj(rate_obj)
            interval = getattr(rate_obj, "funding_interval_hours", 8) or 8
            if interval != 4:
                return f"{normalized_rate * 100:.6f}% ({interval}h)"
            return f"{normalized_rate * 100:.6f}%"

        gross_edge = "-"
        cost = "-"
        net_edge = "-"
        recommendation = "-"
        if metrics:
            gross_edge = f"{metrics.gross_spread_pct:.6f}%"
            cost = f"{metrics.round_trip_cost_pct:.6f}%"
            net_edge = f"{metrics.net_edge_pct:.6f}%"
            recommendation = f"Long {metrics.long_exchange.value}, Short {metrics.short_exchange.value}"

        rows.append(
            (
                pair,
                fmt_rate(Exchange.OKX),
                fmt_rate(Exchange.BINANCE),
                fmt_rate(Exchange.BINGX),
                fmt_rate(Exchange.GATE),
                fmt_rate(Exchange.ASTERDEX),
                fmt_rate(Exchange.BYBIT),
                gross_edge,
                cost,
                net_edge,
                recommendation,
            )
        )

    return rows, len(sorted_pairs)


def build_pair_snapshot_display(
    *,
    pair: str,
    snapshot: dict[str, Any],
    long_ex: Exchange,
    short_ex: Exchange,
    long_display: str,
    short_display: str,
) -> tuple[str, str]:
    """Build the pair snapshot text and status color for the selection panel."""
    result_data = snapshot.get("result_data", {})
    funding_rates = snapshot.get("funding_rates", {})

    long_data = result_data.get(long_ex, {})
    short_data = result_data.get(short_ex, {})
    long_order_book = long_data.get("order_book")
    short_order_book = short_data.get("order_book")

    long_price = snapshot.get("long_entry_price", long_data.get("price", 0))
    short_price = snapshot.get("short_entry_price", short_data.get("price", 0))
    long_price_type = "ASK" if snapshot.get("long_entry_price") is not None else "Mark"
    short_price_type = "BID" if snapshot.get("short_entry_price") is not None else "Mark"

    hours_until = snapshot.get("hours_until_funding", 999)
    if hours_until <= 1:
        entry_status = "GOOD - Within 1h before funding"
        entry_color = "green"
    elif hours_until <= 2:
        entry_status = "OK - 1-2h before funding"
        entry_color = "orange"
    else:
        entry_status = f"WAIT - {hours_until:.1f}h until funding"
        entry_color = "red"

    long_funding = funding_rates.get(long_ex)
    short_funding = funding_rates.get(short_ex)
    long_rate_str = f"{long_funding.funding_rate * 100:+.6f}%" if long_funding else "N/A"
    short_rate_str = f"{short_funding.funding_rate * 100:+.6f}%" if short_funding else "N/A"
    net_funding_str = "N/A"
    funding_interval_text = "Every 8 hours"
    if long_funding and short_funding:
        net_funding = short_funding.funding_rate - long_funding.funding_rate
        net_funding_str = f"{net_funding * 100:+.6f}%"
        interval_hours = long_funding.funding_interval_hours or 8
        funding_interval_text = f"Every {interval_hours} hours"

    info_lines = [
        f"=== {pair} ===",
        "",
        f"LONG  ({long_display}):  ${long_price:,.6f} [{long_price_type}]",
    ]
    if long_order_book and long_order_book.get("asks"):
        for i, (price, qty) in enumerate(long_order_book["asks"][:3], start=1):
            info_lines.append(f"  ASK{i}: ${price:,.6f} x {qty:.4f}")

    info_lines.extend(
        [
            "",
            f"SHORT ({short_display}): ${short_price:,.6f} [{short_price_type}]",
        ]
    )
    if short_order_book and short_order_book.get("bids"):
        for i, (price, qty) in enumerate(short_order_book["bids"][:3], start=1):
            info_lines.append(f"  BID{i}: ${price:,.6f} x {qty:.4f}")

    price_diff = snapshot.get("price_diff", 0.0)
    open_spread_pct = snapshot.get("open_spread_pct", 0.0)
    spread_sign = "+" if price_diff >= 0 else ""
    info_lines.extend(
        [
            "",
            f"Real Spread (OI): {spread_sign}${price_diff:,.6f} ({spread_sign}{open_spread_pct:.3f}%)",
            f"Entry Signal: {'OK - BID > ASK' if price_diff > 0 else 'BAD - BID < ASK'}",
            "-------------------------",
            f"Funding Rate ({long_display}):  {long_rate_str}",
            f"Funding Rate ({short_display}): {short_rate_str}",
            f"Net Funding (SHORT-LONG): {net_funding_str}",
            f"Next Funding: {snapshot.get('time_until_funding_text', 'N/A')}",
            f"Entry Timing: {entry_status}",
            f"Funding Interval: {funding_interval_text}",
        ]
    )

    selected_trade = snapshot.get("selected_trade")
    if selected_trade:
        info_lines.append(
            f"Selected Edge (4H): gross {selected_trade['gross_spread_pct']:.4f}% | "
            f"cost {selected_trade['round_trip_cost_pct']:.4f}% | "
            f"net {selected_trade['net_edge_pct']:.4f}%"
        )

    recommended_trade = snapshot.get("recommended_trade")
    if recommended_trade:
        info_lines.append(
            f"Best Direction: Long {recommended_trade['long_exchange']}, "
            f"Short {recommended_trade['short_exchange']}"
        )

    trade_plan = snapshot.get("trade_plan")
    if trade_plan:
        info_lines.extend(
            [
                "-------------------------",
                f"Plan: {trade_plan['recommendation']} | Score: {trade_plan['quality_score']:.1f}/100",
                f"Expected Net PnL (next cycle): ${trade_plan['expected_net_pnl_next_cycle_usd']:+.2f}",
                f"Suggested Size: {trade_plan['recommended_size_tokens']:.6f} "
                f"(~${trade_plan['recommended_notional_usd']:.2f})",
                f"Depth Ratio: {trade_plan['depth_ratio']:.2f}x | Divergence: {trade_plan['price_divergence_pct']:.3f}%",
            ]
        )
        if trade_plan.get("warnings"):
            info_lines.append("Warnings:")
            for warning in trade_plan["warnings"][:3]:
                info_lines.append(f"  - {warning}")
        if trade_plan.get("blockers"):
            info_lines.append("Blockers:")
            for blocker in trade_plan["blockers"][:2]:
                info_lines.append(f"  - {blocker}")

    return "\n".join(info_lines), entry_color


def build_final_pnl_report_lines(result: dict[str, Any]) -> list[str]:
    """Build log lines for the final PnL report."""
    long_data = result["long"]
    short_data = result["short"]
    total = result["total"]
    pnl_status = "PROFIT" if total["net_pnl"] >= 0 else "LOSS"

    return [
        "=" * 50,
        f"FINAL TRADE REPORT: {result['pair']}",
        "=" * 50,
        f"LONG ({result['long_ex'].value}):",
        f"   Realized PnL: ${long_data['realized_pnl']:+.2f}",
        f"   Trading Fees: -${long_data['commission']:.2f}",
        f"   Funding Fees: ${long_data['funding_fee']:+.2f}",
        f"   Net PnL:      ${long_data['net_pnl']:+.2f}",
        "",
        f"SHORT ({result['short_ex'].value}):",
        f"   Realized PnL: ${short_data['realized_pnl']:+.2f}",
        f"   Trading Fees: -${short_data['commission']:.2f}",
        f"   Funding Fees: ${short_data['funding_fee']:+.2f}",
        f"   Net PnL:      ${short_data['net_pnl']:+.2f}",
        "",
        "-" * 50,
        f"TOTAL REALIZED PnL: ${total['realized_pnl']:+.2f}",
        f"TOTAL TRADING FEES: -${total['commission']:.2f}",
        f"TOTAL FUNDING FEES: ${total['funding_fee']:+.2f}",
        f">>> FINAL NET PnL: ${total['net_pnl']:+.2f} ({pnl_status}) <<<",
        "=" * 50,
    ]
