"""
Open-position workflow helpers for the GUI.
"""

import asyncio
import logging
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Any

from config.constants import calculate_break_even

logger = logging.getLogger(__name__)


def open_position(app: Any) -> None:
    """Open a hedged position after validation and optional spread analysis."""
    if not app.connected:
        return

    if app.analyzing_spread or app.waiting_for_price_spread:
        cancel_open_position(app)
        return

    pair = app.pair_combo.get()
    long_exchange_name = app.long_exchange.get()
    short_exchange_name = app.short_exchange.get()

    if long_exchange_name == short_exchange_name:
        messagebox.showerror("Error", "Long and Short exchanges must be different")
        return

    try:
        size = float(app.size_entry.get())
        leverage = int(app.leverage_var.get())
        split_count = int(app.split_count_var.get())
        if split_count < 1:
            split_count = 1
        elif split_count > 1000:
            messagebox.showerror("Error", "Split count cannot exceed 1000")
            return
    except ValueError:
        messagebox.showerror("Error", "Invalid size, leverage, or split count")
        return

    long_exchange = app._get_exchange_enum(long_exchange_name)
    short_exchange = app._get_exchange_enum(short_exchange_name)
    params = {
        "pair": pair,
        "long_ex": long_exchange,
        "short_ex": short_exchange,
        "long_ex_name": long_exchange_name,
        "short_ex_name": short_exchange_name,
        "size": size,
        "leverage": leverage,
        "split_count": split_count,
        "skip_leverage": app.skip_leverage_var.get(),
        "skip_spread_check": app.skip_spread_check_var.get(),
    }

    app.open_btn.configure(text="Assessing...", state=tk.DISABLED)

    async def assess_open():
        return await app.service.build_open_assessment(
            pair=pair,
            long_exchange=long_exchange,
            short_exchange=short_exchange,
            requested_size_tokens=size,
            leverage=leverage,
            balances=app.latest_balances,
        )

    future = app._run_async(assess_open())
    if future:
        future.add_done_callback(lambda completed: on_open_assessment_complete(app, completed, params))
    else:
        reset_open_button(app)


def _format_pretrade_message(params: dict[str, Any], trade_plan: dict[str, Any] | None) -> str:
    if not trade_plan:
        break_even = calculate_break_even(
            long_exchange=params["long_ex"],
            short_exchange=params["short_ex"],
            position_size_usd=params["size"],
            funding_rate_pct=0.01,
            leverage=params["leverage"],
            slippage_pct=0.02,
        )
        return (
            f"Open {params['pair']}?\n\n"
            f"Size: {params['size']} tokens | Leverage: {params['leverage']}x | Splits: {params['split_count']}\n"
            f"LONG: {params['long_ex_name']} | SHORT: {params['short_ex_name']}\n\n"
            f"Fees + slippage: ~{break_even['total_cost_pct']:.3f}%\n"
            f"Est. cost: ~${break_even['total_cost_usd']:.2f}\n"
            f"Proceed with spread analysis?"
        )

    warnings = trade_plan.get("warnings", [])[:3]
    blockers = trade_plan.get("blockers", [])[:2]
    lines = [
        f"Open {params['pair']}?",
        "",
        f"Requested size: {params['size']:.6f} tokens",
        f"Leverage: {params['leverage']}x | Splits: {params['split_count']}",
        f"LONG: {params['long_ex_name']} | SHORT: {params['short_ex_name']}",
        "",
        f"Quality score: {trade_plan['quality_score']:.1f}/100 -> {trade_plan['recommendation']}",
        f"Gross edge (4H): {trade_plan['gross_edge_pct']:.4f}%",
        f"Cost (fees+slippage): {trade_plan['round_trip_cost_pct']:.4f}%",
        f"Net edge (4H): {trade_plan['net_edge_pct']:.4f}%",
        f"Expected next-cycle PnL: ${trade_plan['expected_net_pnl_next_cycle_usd']:+.2f}",
        f"Depth ratio: {trade_plan['depth_ratio']:.2f}x | Divergence: {trade_plan['price_divergence_pct']:.3f}%",
        f"Suggested size: {trade_plan['recommended_size_tokens']:.6f} tokens (~${trade_plan['recommended_notional_usd']:.2f})",
    ]

    funding_window_hours = trade_plan.get("funding_window_hours")
    if funding_window_hours is not None:
        lines.append(f"Next funding in: {funding_window_hours:.2f}h")

    if blockers:
        lines.extend(["", "Blockers:"])
        lines.extend([f"  - {item}" for item in blockers])

    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend([f"  - {item}" for item in warnings])

    lines.extend(["", "Proceed with spread analysis?"])
    return "\n".join(lines)


def _begin_open_workflow(app: Any, params: dict[str, Any]) -> None:
    app._stop_all_background_tasks()

    skip_spread_check = params.get("skip_spread_check", False)
    params["price_spread_min"] = -100.0 if skip_spread_check else None
    params.setdefault("spread_check_count", 0)
    app.price_spread_params = params

    if skip_spread_check:
        app._log(f"Skip spread analysis, opening {params['pair']} immediately...")
        app._log(f"   LONG: {params['long_ex_name']} | SHORT: {params['short_ex_name']}")
        app.waiting_for_price_spread = True
        app.open_btn.configure(text="Opening... (Cancel)", state=tk.NORMAL)
        app._check_price_spread_and_open()
        return

    app.analyzing_spread = True
    app.analyze_cancel_event = threading.Event()
    app.open_btn.configure(text="Analyzing... (Cancel)", state=tk.NORMAL)
    app._log(f"Starting 2-minute spread analysis for {params['pair']}...")
    app._log(f"   LONG: {params['long_ex_name']} | SHORT: {params['short_ex_name']}")
    app._run_async(app.manager.subscribe_market_data(params["pair"], params["long_ex"], params["short_ex"]))
    app._start_analyze_for_open()


def on_open_assessment_complete(app: Any, future: Any, params: dict[str, Any]) -> None:
    """Handle completion of the pre-trade open assessment."""
    def update_ui() -> None:
        try:
            result = future.result(timeout=1.0)
            trade_plan = result.get("trade_plan")
            params["pretrade_assessment"] = trade_plan

            if trade_plan:
                blockers = trade_plan.get("blockers", [])
                if blockers:
                    override = messagebox.askyesno(
                        "Execution Blockers",
                        "The pre-trade checks found blockers:\n\n"
                        + "\n".join(f"- {item}" for item in blockers)
                        + "\n\nContinue anyway?",
                    )
                    if not override:
                        reset_open_button(app)
                        return

                recommended_size = float(trade_plan.get("recommended_size_tokens", 0.0) or 0.0)
                if recommended_size > 0 and recommended_size < params["size"] * 0.98:
                    decision = messagebox.askyesnocancel(
                        "Sizing Suggestion",
                        f"Requested size {params['size']:.6f} tokens exceeds the conservative recommendation "
                        f"{recommended_size:.6f} tokens.\n\n"
                        "Yes = use suggested size\n"
                        "No = keep requested size\n"
                        "Cancel = abort",
                    )
                    if decision is None:
                        reset_open_button(app)
                        return
                    if decision:
                        params["size"] = recommended_size
                        app.size_entry.delete(0, tk.END)
                        app.size_entry.insert(0, f"{recommended_size:.6f}")

            if not messagebox.askyesno("Confirm Open Position", _format_pretrade_message(params, trade_plan)):
                reset_open_button(app)
                return

            _begin_open_workflow(app, params)
        except Exception as exc:
            logger.warning("Could not complete pre-trade assessment: %s", exc)
            reset_open_button(app)
            messagebox.showerror("Assessment Error", str(exc))

    app.root.after(0, update_ui)


def start_analyze_for_open(app: Any) -> None:
    """Run spread analysis before waiting for a valid open threshold."""
    params = app.price_spread_params
    if not params:
        return

    def safe_log(message: str) -> None:
        app.root.after(0, lambda text=message: app._log(text))

    async def do_analyze():
        return await app.manager.analyze_spread(
            params["pair"],
            params["long_ex"],
            params["short_ex"],
            duration_seconds=120.0,
            check_interval=2.0,
            log_callback=safe_log,
            cancel_event=app.analyze_cancel_event,
            mode="open",
        )

    future = app._run_async(do_analyze())
    if future:
        future.add_done_callback(lambda completed: app._on_analyze_for_open_complete(completed))


def on_analyze_for_open_complete(app: Any, future: Any) -> None:
    """Handle completion of the open-position spread analysis."""
    def update_ui() -> None:
        try:
            result = future.result(timeout=1.0)
            app.analyzing_spread = False
            app.analyze_cancel_event = None

            if not result.get("success"):
                app._log(f"Analyze failed: {result.get('error', 'Unknown error')}")
                reset_open_button(app)
                return

            avg_spread = result["avg_spread"]
            app.price_spread_params["price_spread_min"] = avg_spread
            app.price_spread_threshold.delete(0, tk.END)
            app.price_spread_threshold.insert(0, f"{avg_spread:.4f}")
            app._log(f"Analyze done. Threshold (avg spread) = {avg_spread:.4f}%")
            app._log(f"Waiting for spread >= {avg_spread:.4f}% before opening...")
            app.open_btn.configure(text="Waiting... (Cancel)")
            app.waiting_for_price_spread = True
            app._check_price_spread_and_open()
        except Exception as exc:
            app._log(f"Analyze error: {exc}")
            reset_open_button(app)

    app.root.after(0, update_ui)


def reset_open_button(app: Any) -> None:
    """Reset open-button state after completion or cancellation."""
    app.analyzing_spread = False
    app.waiting_for_price_spread = False
    app.analyze_cancel_event = None
    app.price_spread_params = None
    app.open_btn.configure(text="Open Position", state=tk.NORMAL)


def cancel_open_position(app: Any) -> None:
    """Cancel open-position analysis or waiting."""
    if app.analyze_cancel_event:
        app.analyze_cancel_event.set()
        app._log("Cancelling analysis...")

    cancel_price_spread_wait(app)
    reset_open_button(app)


def cancel_price_spread_wait(app: Any) -> None:
    """Cancel waiting for the price spread or currently running splits."""
    app.waiting_for_price_spread = False
    if app.price_spread_check_task:
        app.root.after_cancel(app.price_spread_check_task)
        app.price_spread_check_task = None

    if app.split_cancel_event:
        app.split_cancel_event.set()
        app._log("Cancel signal sent to running splits...")

    app.price_spread_params = None
    app.open_btn.configure(text="Open Position", state=tk.NORMAL)
    app._log("Cancelled waiting for price spread")


async def service_check_and_open(app: Any, params: dict[str, Any], safe_log) -> dict[str, Any]:
    """Check the live spread and execute split opening when the threshold is met."""
    try:
        spread_data = await app.service.check_open_spread(
            params["pair"],
            params["long_ex"],
            params["short_ex"],
        )
    except asyncio.TimeoutError:
        safe_log("Timeout fetching order books, retrying...")
        return {"success": False, "waiting": True}
    except Exception as exc:
        safe_log(f"{exc}, retrying...")
        return {"success": False, "waiting": True}

    long_ask_price = spread_data["long_ask_price"]
    short_bid_price = spread_data["short_bid_price"]
    price_spread_pct = spread_data["price_spread_pct"]

    current_threshold = params.get("price_spread_min", 0)
    check_count = params.get("spread_check_count", 0) + 1
    params["spread_check_count"] = check_count

    if price_spread_pct < 0:
        safe_log(
            f"Real spread (OI): {price_spread_pct:.4f}% NEGATIVE "
            f"(threshold: {current_threshold:.4f}%) (check {check_count % 30}/30) | "
            f"LONG ASK: ${long_ask_price:,.6f} | SHORT BID: ${short_bid_price:,.6f}"
        )
    else:
        safe_log(
            f"Real spread (OI): {price_spread_pct:.4f}% "
            f"(threshold: {current_threshold:.4f}%) (check {check_count % 30}/30) | "
            f"LONG ASK: ${long_ask_price:,.6f} | SHORT BID: ${short_bid_price:,.6f}"
        )

    if price_spread_pct < current_threshold:
        if check_count >= 30 and check_count % 30 == 0:
            old_threshold = current_threshold
            params["price_spread_min"] = current_threshold - 0.01
            safe_log(
                f"30 checks without meeting threshold, reducing threshold: "
                f"{old_threshold:.4f}% -> {params['price_spread_min']:.4f}%"
            )
        return {"success": False, "waiting": True}

    split_count = params.get("split_count", 1)
    safe_log(f"Price spread {price_spread_pct:.4f}% >= {current_threshold:.4f}%, opening position.")
    safe_log(
        f"Opening: {params['pair']} | Long {params['long_ex_name']} | "
        f"Short {params['short_ex_name']} | Size: {params['size']} | Splits: {split_count}"
    )

    app.split_cancel_event = threading.Event()

    def on_first_split(size_opened: float) -> None:
        app.root.after(0, lambda: app._on_first_split_complete(params, size_opened))

    result = await app.service.execute_open_splits(
        pair=params["pair"],
        long_exchange=params["long_ex"],
        short_exchange=params["short_ex"],
        size=params["size"],
        leverage=params["leverage"],
        split_count=split_count,
        price_spread_min=params["price_spread_min"],
        log_callback=safe_log,
        on_first_split_complete=on_first_split,
        cancel_event=app.split_cancel_event,
        skip_leverage_set=params.get("skip_leverage", False),
        skip_spread_check=params.get("skip_spread_check", False),
    )
    app.split_cancel_event = None
    app.balance_before_open = result.get("balance_before", {})
    return result


def check_price_spread_and_open(app: Any) -> None:
    """Schedule a live spread check and trigger open execution when ready."""
    if not app.waiting_for_price_spread or not app.price_spread_params:
        return

    params = app.price_spread_params

    def safe_log(message: str) -> None:
        app.root.after(0, lambda text=message: app._log(text))

    async def check_and_open():
        return await app._service_check_and_open(params, safe_log)

    future = app._run_async(check_and_open())
    if future:
        future.add_done_callback(lambda completed: app._on_price_spread_check_complete(completed))


def on_price_spread_check_complete(app: Any, future: Any) -> None:
    """Handle the result of an asynchronous spread check."""
    logger.debug("_on_price_spread_check_complete: ENTER (callback from async thread)")

    def update_ui() -> None:
        logger.debug("_on_price_spread_check_complete.update_ui: ENTER (on main thread)")
        try:
            result = future.result(timeout=0.1)
            logger.debug(
                "_on_price_spread_check_complete.update_ui: got result success=%s",
                result.get("success"),
            )

            if not result.get("success") and result.get("waiting"):
                if app.waiting_for_price_spread:
                    app.price_spread_check_task = app.root.after(2000, app._check_price_spread_and_open)
                logger.debug("_on_price_spread_check_complete.update_ui: EXIT (waiting)")
                return

            app.waiting_for_price_spread = False
            app.open_btn.configure(text="Open Position", state=tk.NORMAL)

            if result.get("success"):
                params = app.price_spread_params
                if not params:
                    logger.debug("_on_price_spread_check_complete.update_ui: EXIT (no params)")
                    return

                splits_completed = result.get("splits_completed", 1)
                splits_total = result.get("splits_total", 1)
                app._log(f"Position opened successfully! ({splits_completed}/{splits_total} splits completed)")
                total_size = result.get("total_long_size", params["size"])
                app._on_all_splits_complete(params, total_size)
                logger.debug(
                    "_on_price_spread_check_complete.update_ui: EXIT (called _on_all_splits_complete)"
                )
                return

            error_message = result.get("error", "Unknown error")
            app._log(f"Failed to open position: {error_message}")
            app.root.after(10, lambda: messagebox.showerror("Error", f"Failed to open position:\n{error_message}"))
            logger.debug("_on_price_spread_check_complete.update_ui: EXIT (error)")
        except Exception as exc:
            app.waiting_for_price_spread = False
            app.open_btn.configure(text="Open Position", state=tk.NORMAL)
            app._log(f"Error: {exc}")
            app.root.after(10, lambda err=str(exc): messagebox.showerror("Error", err))
            logger.debug("_on_price_spread_check_complete.update_ui: EXIT (exception: %s)", exc)

    app.root.after(0, update_ui)
    logger.debug("_on_price_spread_check_complete: EXIT (scheduled update_ui)")


def on_first_split_complete(app: Any, params: dict[str, Any], size_opened: float) -> None:
    """Track the first split without starting the full monitor yet."""
    logger.debug("_on_first_split_complete: ENTER")
    try:
        if app._pair_info_update_task:
            app.root.after_cancel(app._pair_info_update_task)
            app._pair_info_update_task = None
            logger.debug("_on_first_split_complete: Cancelled pair info update task")

        app._setup_position_tracking(
            params["pair"],
            params["long_ex"],
            params["short_ex"],
            size_opened,
            leverage=params.get("leverage", 1),
            pretrade_assessment=params.get("pretrade_assessment"),
        )
        logger.debug("_on_first_split_complete: EXIT")
    except Exception as exc:
        logger.error("_on_first_split_complete error: %s", exc)


def on_all_splits_complete(app: Any, params: dict[str, Any], total_size: float) -> None:
    """Finalize state after all splits have completed."""
    logger.debug("_on_all_splits_complete: ENTER")
    try:
        if app.active_position:
            app.active_position["size"] = total_size
            if params.get("pretrade_assessment"):
                app.active_position["pretrade_assessment"] = params["pretrade_assessment"]
            app.service.persist_active_position(app.active_position)
            app.service.record_trade_event("position_scaled", app.active_position)

        def start_monitor() -> None:
            if not app.monitoring:
                logger.debug("_on_all_splits_complete: Starting monitoring")
                app._start_monitoring()
                app.monitor_btn.config(text="Stop Monitor")
            else:
                logger.debug("_on_all_splits_complete: Monitoring already active")

        app.root.after(500, start_monitor)
        logger.debug("_on_all_splits_complete: EXIT")
    except Exception as exc:
        logger.error("_on_all_splits_complete: ERROR %s", exc)
