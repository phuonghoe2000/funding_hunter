"""
Close workflow helpers for the GUI.
"""

import asyncio
import logging
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Any

try:
    from gui.display_formatters import build_final_pnl_report_lines
except ImportError:
    from display_formatters import build_final_pnl_report_lines

logger = logging.getLogger(__name__)


def close_position(app: Any) -> None:
    """Close the active position, optionally after spread analysis."""
    if not app.active_position:
        messagebox.showinfo("Info", "No active position")
        return

    if getattr(app, "analyzing_close", False) or getattr(app, "waiting_for_close", False):
        cancel_close_process(app)
        return

    try:
        splits = int(app.split_count_var.get())
        if splits < 1:
            splits = 1
    except Exception:
        splits = 1

    skip_spread = app.skip_spread_check_var.get()

    close_size_text = app.close_size_var.get().strip()
    close_size = None
    if close_size_text:
        try:
            close_size = float(close_size_text)
            if close_size <= 0:
                messagebox.showerror("Error", "Close size must be > 0")
                return
        except ValueError:
            messagebox.showerror("Error", "Invalid close size. Enter a number or leave empty for full close.")
            return

    position = app.active_position
    if close_size is not None:
        total_size = position.get("size", 0) or position.get("long_size", 0) or position.get("short_size", 0)
        if total_size > 0:
            percent_text = f"{(close_size / total_size) * 100:.1f}%"
        else:
            percent_text = "?%"
        size_info = f"Partial close: {close_size} tokens ({percent_text} of position)"
    else:
        size_info = "Full close: 100% of position"

    if skip_spread:
        confirm_message = f"{size_info}\nSplits: {splits}\nSkip spread check and close immediately."
    else:
        confirm_message = f"{size_info}\nSplits: {splits}\nAnalyze spread for 2 minutes first."

    if not messagebox.askyesno("Confirm Close", confirm_message):
        return

    app._log(f"Starting close workflow for {position['pair']}...")
    app._stop_all_background_tasks()

    app.analyzing_close = not skip_spread
    app.close_cancel_event = threading.Event()
    if skip_spread:
        app.close_btn.configure(text="Closing... (Cancel)", state=tk.NORMAL)
    else:
        app.close_btn.configure(text="Analyzing... (Cancel)", state=tk.NORMAL)
    app.open_btn.configure(state=tk.DISABLED)

    app.close_params = {
        "pair": position["pair"],
        "long_exchange": position["long_exchange"],
        "short_exchange": position["short_exchange"],
        "splits": splits,
        "skip_spread_check": skip_spread,
        "close_size": close_size,
    }

    app._run_async(
        app.manager.subscribe_market_data(
            position["pair"],
            position["long_exchange"],
            position["short_exchange"],
        )
    )

    if skip_spread:
        app._log(f"Skipping spread analysis and closing {position['pair']} immediately...")
    else:
        app._log("Starting 2-minute spread analysis for close...")

    execute_close_workflow(app)


def reset_close_button(app: Any) -> None:
    """Restore close controls to their default state."""
    app.analyzing_close = False
    app.waiting_for_close = False
    app.close_cancel_event = None
    app.close_params = None
    app.close_btn.configure(text="Close Position", state=tk.NORMAL)
    app.open_btn.configure(state=tk.NORMAL)


def cancel_close_process(app: Any) -> None:
    """Cancel any in-progress close analysis or execution."""
    if getattr(app, "close_cancel_event", None):
        app.close_cancel_event.set()
        app._log("Cancelling close analysis/execution...")

    if app.split_cancel_event:
        app.split_cancel_event.set()
        app._log("Sent cancel signal to running close splits")

    reset_close_button(app)


def execute_close_workflow(app: Any) -> None:
    """Execute the shared close workflow via the GUI service."""
    params = app.close_params
    if not params:
        return

    app.waiting_for_close = True
    app.close_btn.configure(text="Closing... (Cancel)")

    async def async_close():
        app.split_cancel_event = app.close_cancel_event
        return await app.service.close_position_with_analysis(
            pair=params["pair"],
            long_exchange=params["long_exchange"],
            short_exchange=params["short_exchange"],
            splits=params["splits"],
            log_callback=lambda msg: app.root.after(0, lambda m=msg: app._log(m)),
            cancel_event=app.close_cancel_event,
            skip_spread_check=params.get("skip_spread_check", False),
            close_size=params.get("close_size"),
        )

    future = app._run_async(async_close())
    if future:
        future.add_done_callback(app._on_close_complete)


def on_close_complete(app: Any, future: Any) -> None:
    """Handle close completion and refresh UI state."""
    closed_position = app.active_position.copy() if app.active_position else None

    def update_ui() -> None:
        app.split_cancel_event = None
        reset_close_button(app)

        try:
            result = future.result()

            if result.get("cancelled"):
                app._log("Close process cancelled")
                return

            if result["success"]:
                app._log("Position closed successfully!")
                app._stop_monitoring()
                app.active_position = None
                app._clear_position_display()

                if closed_position:
                    calculate_and_show_final_pnl(app, closed_position)

                if getattr(app, "auto_trading_active", False):
                    app._log("Auto Trading: resuming signal scan...")
            else:
                app._log(f"Close failed: {result.get('error', 'Unknown error')}")
        except Exception as exc:
            logger.error("Error finishing close workflow: %s", exc)
            app._log(f"Error: {exc}")

    app.root.after(0, update_ui)


def calculate_and_show_final_pnl(app: Any, closed_position: dict[str, Any]) -> None:
    """Calculate and display final PnL after a position is closed."""
    long_exchange = closed_position.get("long_exchange")
    short_exchange = closed_position.get("short_exchange")
    pair = closed_position.get("pair", "Unknown")
    open_time = closed_position.get("open_time")

    async def calculate_pnl():
        since = None
        if open_time:
            since = int(open_time.timestamp() * 1000) - 60000

        long_client = app.manager.clients.get(long_exchange)
        short_client = app.manager.clients.get(short_exchange)

        long_pnl_data = {"realized_pnl": 0, "commission": 0, "funding_fee": 0, "net_pnl": 0}
        short_pnl_data = {"realized_pnl": 0, "commission": 0, "funding_fee": 0, "net_pnl": 0}

        if long_client:
            try:
                long_pnl_data = await long_client.get_closed_pnl(pair, since)
            except Exception as exc:
                logger.error("Error getting long PnL from %s: %s", long_exchange.value, exc)

        if short_client:
            try:
                short_pnl_data = await short_client.get_closed_pnl(pair, since)
            except Exception as exc:
                logger.error("Error getting short PnL from %s: %s", short_exchange.value, exc)

        return {
            "pair": pair,
            "long_ex": long_exchange,
            "short_ex": short_exchange,
            "long": long_pnl_data,
            "short": short_pnl_data,
            "total": {
                "realized_pnl": long_pnl_data["realized_pnl"] + short_pnl_data["realized_pnl"],
                "commission": long_pnl_data["commission"] + short_pnl_data["commission"],
                "funding_fee": long_pnl_data["funding_fee"] + short_pnl_data["funding_fee"],
                "net_pnl": long_pnl_data["net_pnl"] + short_pnl_data["net_pnl"],
            },
        }

    def on_pnl_calculated(future: Any) -> None:
        def show_result() -> None:
            try:
                result = future.result(timeout=15)
                if result:
                    for line in build_final_pnl_report_lines(result):
                        app._log(line)
                    app.balance_before_open = {}
                    return

                app._log("Could not calculate final PnL")
            except asyncio.TimeoutError:
                app._log("Timed out while calculating final PnL")
            except Exception as exc:
                app._log(f"Error calculating PnL: {exc}")

        app.root.after(0, show_result)

    future = app._run_async(calculate_pnl())
    if future:
        future.add_done_callback(on_pnl_calculated)
