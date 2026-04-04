"""
Monitoring workflow helpers for the GUI.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from tkinter import messagebox

from config.constants import Exchange, get_exchange_symbol
from core.trade_advisor import build_liquidation_reduce_policy

logger = logging.getLogger(__name__)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def start_monitoring(app: Any) -> None:
    """Start monitoring the active hedged position."""
    if app.monitoring:
        return

    app.monitoring = True
    app._log("Started position monitoring")

    def safe_log(message: str) -> None:
        app.root.after(0, lambda: app._log(message))

    async def monitor_loop():
        safe_log("Monitor loop started...")

        while app.monitoring and app.active_position:
            try:
                position = app.active_position
                snapshot = await app.service.build_monitor_snapshot(position)
                long_pnl = snapshot["long_pnl"]
                short_pnl = snapshot["short_pnl"]
                risk_percent = snapshot["risk_percent"]
                total_balance = snapshot["total_balance"]
                trade_plan = snapshot.get("trade_plan") or {}
                monitor_advice = snapshot.get("monitor_advice") or {}
                liquidation = snapshot.get("liquidation") or {}
                min_liq_distance = snapshot.get("min_liquidation_distance_pct")
                nearest_liq_exchange = liquidation.get("nearest_liquidation_exchange")

                position["long_pnl"] = long_pnl
                position["short_pnl"] = short_pnl
                position["latest_trade_plan"] = trade_plan
                position["latest_monitor_advice"] = monitor_advice
                position["latest_liquidation"] = liquidation

                long_exchange_name = position["long_exchange"].value.capitalize()
                short_exchange_name = position["short_exchange"].value.capitalize()

                def update_label(
                    risk=risk_percent,
                    long_value=long_pnl,
                    short_value=short_pnl,
                    long_name=long_exchange_name,
                    short_name=short_exchange_name,
                    advice=monitor_advice,
                    liq_distance=min_liq_distance,
                    liq_exchange=nearest_liq_exchange,
                ) -> None:
                    color = "red" if risk > 5 else ("orange" if risk > 2 else "green")
                    label_text = (
                        f"Risk: {risk:.2f}% | Long({long_name}): ${long_value:+.2f} | "
                        f"Short({short_name}): ${short_value:+.2f}"
                    )
                    if liq_distance is not None and liq_exchange:
                        label_text += f" | LiqDist({liq_exchange}): {liq_distance:.2f}%"
                    app.pos_pnl_label.config(
                        text=label_text,
                        foreground=color,
                    )
                    advice_text = advice.get("action", "OPEN")
                    app.pos_status_label.config(text=f"Status: {advice_text}", foreground=color)

                app.root.after(0, update_label)
                app.service.persist_active_position(position)

                advice_reason = monitor_advice.get("reason")
                advice_action = monitor_advice.get("action")
                advice_signature = f"{advice_action}:{advice_reason}"
                if advice_reason and app.last_monitor_advice != advice_signature:
                    app.last_monitor_advice = advice_signature
                    safe_log(f"Monitor advice -> {advice_action}: {advice_reason}")
                    app.service.record_trade_event(
                        "monitor_advice",
                        {
                            "session_id": position.get("session_id"),
                            "pair": position.get("pair"),
                            "advice": monitor_advice,
                            "risk_percent": risk_percent,
                        },
                    )

                if app.auto_reduce_liq_var.get():
                    try:
                        liq_threshold = float(app.liq_distance_threshold_var.get())
                    except Exception:
                        liq_threshold = 3.0

                    liq_policy = build_liquidation_reduce_policy(
                        distance_pct=min_liq_distance,
                        activation_threshold_pct=liq_threshold,
                    )
                    if liq_policy:
                        cooldown_remaining = 0.0
                        last_reduce_at = _parse_dt(position.get("last_risk_reduce_at"))
                        if last_reduce_at:
                            cooldown_remaining = max(
                                0.0,
                                60.0 - (datetime.now(timezone.utc) - last_reduce_at).total_seconds(),
                            )

                        if cooldown_remaining > 0:
                            safe_log(
                                f"Liq-distance reduce cooldown: {cooldown_remaining:.1f}s remaining "
                                f"(distance {min_liq_distance:.2f}% <= {liq_threshold:.1f}%)"
                            )
                        else:
                            safe_log(
                                f"Liq distance {min_liq_distance:.2f}% on {nearest_liq_exchange} "
                                f"<= threshold {liq_threshold:.1f}% -> {liq_policy.description}"
                            )
                            reduce_result = await app.service.reduce_position_on_risk(
                                pair=position["pair"],
                                long_exchange=position["long_exchange"],
                                short_exchange=position["short_exchange"],
                                risk_percent=risk_percent,
                                threshold_percent=liq_threshold,
                                reason="gui_monitor_liq_distance_threshold",
                                reduce_ratio=liq_policy.reduce_ratio,
                                splits=liq_policy.splits,
                                interval_seconds=liq_policy.interval_seconds,
                                policy_mode=liq_policy.mode,
                                action_label=liq_policy.action_label,
                            )
                            if reduce_result.get("success"):
                                remaining_size = float(reduce_result.get("remaining_size_tokens", position.get("size", 0.0)) or 0.0)
                                position["size"] = remaining_size
                                position["long_size"] = float(reduce_result.get("remaining_long_size", position.get("long_size", 0.0)) or 0.0)
                                position["short_size"] = float(reduce_result.get("remaining_short_size", position.get("short_size", 0.0)) or 0.0)
                                position["last_risk_reduce_at"] = datetime.now(timezone.utc)
                                position["risk_reduce_count"] = int(position.get("risk_reduce_count", 0) or 0) + 1
                                safe_log(
                                    f"{liq_policy.action_label} done: remaining size {remaining_size:.6f} | "
                                    f"count {position['risk_reduce_count']}"
                                )
                                app.root.after(
                                    0,
                                    lambda liq_value=min_liq_distance, threshold=liq_threshold, exchange_name=nearest_liq_exchange, policy_label=liq_policy.action_label: messagebox.showwarning(
                                        "Liq Distance Reduce",
                                        f"Triggered {policy_label} ladder.\n"
                                        f"Liq distance was {liq_value:.2f}% on {exchange_name} (threshold: {threshold:.1f}%).",
                                    ),
                                )
                                if remaining_size <= 0:
                                    safe_log("Position fully closed after liq-distance reduce")
                                    app.service.clear_active_position()
                                    app.monitoring = False
                                    app.active_position = None
                                    app.root.after(0, app._clear_position_display)
                                    break
                                continue
                            safe_log(f"Liq-distance reduce failed: {reduce_result.get('error')}")

                if app.auto_close_risk_var.get():
                    try:
                        risk_threshold = float(app.risk_threshold_var.get())
                    except Exception:
                        risk_threshold = 10.0

                    bingx_pnl = 0.0
                    if position["long_exchange"] == Exchange.BINGX:
                        bingx_pnl = long_pnl
                    elif position["short_exchange"] == Exchange.BINGX:
                        bingx_pnl = short_pnl

                    effective_threshold = risk_threshold * 2 if bingx_pnl > 0 else risk_threshold
                    if bingx_pnl > 0:
                        safe_log(f"BingX is +${bingx_pnl:.2f} -> threshold x2: {effective_threshold:.1f}%")

                    if risk_percent >= effective_threshold:
                        cooldown_remaining = 0.0
                        last_reduce_at = _parse_dt(position.get("last_risk_reduce_at"))
                        if last_reduce_at:
                            cooldown_remaining = max(
                                0.0,
                                60.0 - (datetime.now(timezone.utc) - last_reduce_at).total_seconds(),
                            )

                        if cooldown_remaining > 0:
                            safe_log(
                                f"Risk reduce cooldown: {cooldown_remaining:.1f}s remaining "
                                f"(risk {risk_percent:.2f}% >= {effective_threshold:.1f}%)"
                            )
                        else:
                            safe_log(
                                f"Risk {risk_percent:.2f}% >= threshold {effective_threshold:.1f}% "
                                f"(balance ${total_balance:.2f}) -> reducing both legs by 50%"
                            )
                            reduce_result = await app.service.reduce_position_on_risk(
                                pair=position["pair"],
                                long_exchange=position["long_exchange"],
                                short_exchange=position["short_exchange"],
                                risk_percent=risk_percent,
                                threshold_percent=effective_threshold,
                                reason="gui_monitor_risk_threshold",
                            )
                            if reduce_result.get("success"):
                                remaining_size = float(reduce_result.get("remaining_size_tokens", position.get("size", 0.0)) or 0.0)
                                position["size"] = remaining_size
                                position["long_size"] = float(reduce_result.get("remaining_long_size", position.get("long_size", 0.0)) or 0.0)
                                position["short_size"] = float(reduce_result.get("remaining_short_size", position.get("short_size", 0.0)) or 0.0)
                                position["last_risk_reduce_at"] = datetime.now(timezone.utc)
                                position["risk_reduce_count"] = int(position.get("risk_reduce_count", 0) or 0) + 1
                                safe_log(
                                    f"Fast risk reduce done: remaining size {remaining_size:.6f} | "
                                    f"count {position['risk_reduce_count']}"
                                )
                                app.root.after(
                                    0,
                                    lambda risk_value=risk_percent, threshold=effective_threshold: messagebox.showwarning(
                                        "Risk Reduce",
                                        f"Triggered both-legs reduce 50%.\n"
                                        f"Risk was {risk_value:.2f}% (threshold: {threshold:.1f}%).",
                                    ),
                                )
                                if remaining_size <= 0:
                                    safe_log("Position fully closed after risk reduce")
                                    app.service.clear_active_position()
                                    app.monitoring = False
                                    app.active_position = None
                                    app.root.after(0, app._clear_position_display)
                                    break
                                continue
                            else:
                                safe_log(f"Risk reduce failed: {reduce_result.get('error')}")

                if app.auto_close_reversal_var.get() and "initial_net_funding" in position:
                    should_close, reason = await check_funding_reversal(app, position)
                    if should_close:
                        safe_log(f"Funding reversal detected: {reason}")
                        try:
                            splits = max(1, int(app.split_count_var.get()))
                        except Exception:
                            splits = 1

                        close_result = await app.service.close_position_with_analysis(
                            pair=position["pair"],
                            long_exchange=position["long_exchange"],
                            short_exchange=position["short_exchange"],
                            splits=splits,
                            log_callback=safe_log,
                            skip_spread_check=True,
                        )
                        if close_result.get("success"):
                            safe_log(f"Position auto-closed due to: {reason}")
                            app.root.after(
                                0,
                                lambda reason_text=reason: messagebox.showinfo(
                                    "Auto-Close",
                                    f"Position closed:\n{reason_text}",
                                ),
                            )
                            app.service.record_trade_event(
                                "auto_close_reversal",
                                {
                                    "session_id": position.get("session_id"),
                                    "pair": position.get("pair"),
                                    "reason": reason,
                                    "close_result": close_result,
                                },
                            )
                            app.service.clear_active_position()
                            app.monitoring = False
                            app.active_position = None
                            app.root.after(0, app._clear_position_display)
                            break
                        else:
                            safe_log(f"Auto-close failed: {close_result.get('error')}")

                await asyncio.sleep(2)
            except Exception as exc:
                logger.error("Monitor error: %s", exc)
                await asyncio.sleep(2)

    future = app._run_async(monitor_loop())
    if not future:
        app._log("Failed to schedule monitor loop - no event loop!")


def stop_monitoring(app: Any) -> None:
    """Stop monitoring the current position."""
    app.monitoring = False
    app._log("Monitoring stopped")
    app.monitor_btn.config(text="Start Monitor")


def toggle_monitoring(app: Any) -> None:
    """Toggle monitoring on or off."""
    if not app.active_position:
        messagebox.showwarning("Warning", "No active position to monitor")
        return

    if app.monitoring:
        app._stop_monitoring()
        return

    app._start_monitoring()
    app.monitor_btn.config(text="Stop Monitor")


async def check_funding_reversal(app: Any, position: dict[str, Any]) -> tuple[bool, str]:
    """Check whether funding conditions reversed enough to close the position."""
    try:
        try:
            min_threshold = float(app.min_spread_threshold.get()) / 100
        except Exception:
            min_threshold = 0.0001

        initial_net_funding = position.get("initial_net_funding", 0)
        initial_long_rate = position.get("initial_long_rate", 0)
        initial_short_rate = position.get("initial_short_rate", 0)

        long_exchange = position["long_exchange"]
        short_exchange = position["short_exchange"]
        pair = position["pair"]

        current_rates = {}
        for exchange in [long_exchange, short_exchange]:
            client = app.manager.clients.get(exchange)
            if client:
                try:
                    symbol = get_exchange_symbol(pair, exchange)
                    funding_rate_obj = await client.get_funding_rate(symbol)
                    current_rates[exchange] = funding_rate_obj.funding_rate
                except Exception as exc:
                    logger.debug("Could not get funding rate from %s: %s", exchange.value, exc)
                    return False, ""

        if len(current_rates) < 2:
            return False, ""

        current_long_rate = current_rates.get(long_exchange, 0)
        current_short_rate = current_rates.get(short_exchange, 0)
        current_net_funding = current_short_rate - current_long_rate

        logger.debug(
            "Funding check - Initial: %.6f%%, Current: %.6f%%",
            initial_net_funding * 100,
            current_net_funding * 100,
        )

        if initial_net_funding != 0 and (initial_net_funding * current_net_funding) < 0:
            reason = (
                f"Funding direction reversed\n"
                f"Initial: {initial_net_funding*100:.6f}% "
                f"({'positive' if initial_net_funding > 0 else 'negative'})\n"
                f"Current: {current_net_funding*100:.6f}% "
                f"({'positive' if current_net_funding > 0 else 'negative'})\n"
                f"LONG {long_exchange.value}: {initial_long_rate*100:.6f}% -> {current_long_rate*100:.6f}%\n"
                f"SHORT {short_exchange.value}: {initial_short_rate*100:.6f}% -> {current_short_rate*100:.6f}%"
            )
            return True, reason

        if abs(current_net_funding) < min_threshold:
            reason = (
                f"Spread dropped below threshold\n"
                f"Current spread: {abs(current_net_funding)*100:.6f}%\n"
                f"Threshold: {min_threshold*100:.6f}%\n"
                f"Initial spread: {abs(initial_net_funding)*100:.6f}%\n"
                f"LONG {long_exchange.value}: {current_long_rate*100:.6f}%\n"
                f"SHORT {short_exchange.value}: {current_short_rate*100:.6f}%"
            )
            return True, reason

        if abs(initial_net_funding) > 0:
            spread_reduction = 1 - (abs(current_net_funding) / abs(initial_net_funding))
            if spread_reduction > 0.97:
                reason = (
                    f"Spread dropped by {spread_reduction*100:.1f}%\n"
                    f"Initial: {abs(initial_net_funding)*100:.6f}%\n"
                    f"Current: {abs(current_net_funding)*100:.6f}%\n"
                    f"LONG {long_exchange.value}: {initial_long_rate*100:.6f}% -> {current_long_rate*100:.6f}%\n"
                    f"SHORT {short_exchange.value}: {initial_short_rate*100:.6f}% -> {current_short_rate*100:.6f}%"
                )
                return True, reason

        return False, ""
    except Exception as exc:
        logger.error("Error checking funding reversal: %s", exc)
        return False, ""
