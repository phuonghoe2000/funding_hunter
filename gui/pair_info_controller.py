"""
Pair info and price display helpers for the GUI.
"""

import asyncio
import logging
from typing import Any

from config.constants import Exchange, get_exchange_symbol

try:
    from gui.display_formatters import build_pair_snapshot_display
    from gui.exchange_display import from_display_name
except ImportError:
    from display_formatters import build_pair_snapshot_display
    from exchange_display import from_display_name

logger = logging.getLogger(__name__)


def update_usdt_volume(app: Any, event: Any = None) -> None:
    """Debounce and schedule a USDT volume refresh."""
    if hasattr(app, "_usdt_update_pending") and app._usdt_update_pending:
        app.root.after_cancel(app._usdt_update_pending)
        app._usdt_update_pending = None

    app._usdt_update_pending = app.root.after(500, app._do_update_usdt_volume)


def do_update_usdt_volume(app: Any) -> None:
    """Update USDT volume display based on the entered size."""
    app._usdt_update_pending = None

    try:
        size = float(app.size_entry.get())
    except (ValueError, AttributeError):
        app.usdt_vol_label.config(text="≈ $0.00 USDT")
        return

    pair = app.pair_combo.get()
    if not pair or not app.connected:
        app.usdt_vol_label.config(text="≈ $?.?? USDT")
        return

    async def get_price():
        for exchange, client in app.manager.clients.items():
            try:
                symbol = get_exchange_symbol(pair, exchange)
                return await client.get_mark_price(symbol)
            except Exception:
                continue
        return None

    def on_price(future: Any) -> None:
        try:
            price = future.result()
            if price:
                usdt_value = size * price
                app.root.after(
                    0,
                    lambda: app.usdt_vol_label.config(
                        text=f"≈ ${usdt_value:,.2f} USDT",
                        foreground="green" if usdt_value >= 10 else "orange",
                    ),
                )
                return

            app.root.after(0, lambda: app.usdt_vol_label.config(text="≈ $?.?? USDT"))
        except Exception:
            pass

    future = app._run_async(get_price())
    if future:
        future.add_done_callback(on_price)


def update_pair_info(app: Any) -> None:
    """Refresh pair info display for the selected pair and exchanges."""
    pair = app.pair_combo.get()
    if not pair or not app.connected:
        return

    long_display = app.long_exchange.get()
    short_display = app.short_exchange.get()

    if not long_display or not short_display:
        app.selected_pair_info.config(text="Please select both LONG and SHORT exchanges", foreground="gray")
        return

    long_exchange = from_display_name(long_display)
    short_exchange = from_display_name(short_display)
    if not long_exchange or not short_exchange:
        return

    fetch_pair_prices_for_display(app, pair, long_exchange, short_exchange, long_display, short_display)


def fetch_pair_prices_for_display(
    app: Any,
    pair: str,
    long_exchange: Exchange,
    short_exchange: Exchange,
    long_display: str,
    short_display: str,
) -> None:
    """Fetch and render snapshot data for the selected pair."""

    async def async_get_snapshot():
        return await app.service.get_pair_snapshot(pair, long_exchange, short_exchange)

    def on_snapshot_complete(future: Any) -> None:
        try:
            snapshot = future.result(timeout=0.1)
            result_data = snapshot.get("result_data", {})

            if len(result_data) < 2:
                app.root.after(
                    0,
                    lambda: app.selected_pair_info.config(
                        text="Could not fetch prices from selected exchanges",
                        foreground="red",
                    ),
                )
                return

            try:
                requested_size = float(app.size_entry.get())
            except (ValueError, AttributeError):
                requested_size = 0.0

            try:
                leverage = int(app.leverage_var.get())
            except (ValueError, AttributeError):
                leverage = 1

            trade_plan = app.service.build_trade_plan_from_snapshot(
                snapshot,
                requested_size_tokens=requested_size,
                leverage=leverage,
                balances=app.latest_balances,
            )
            if trade_plan:
                snapshot["trade_plan"] = trade_plan

            info_text, entry_color = build_pair_snapshot_display(
                pair=pair,
                snapshot=snapshot,
                long_ex=long_exchange,
                short_ex=short_exchange,
                long_display=long_display,
                short_display=short_display,
            )
            app.root.after(
                0,
                lambda: app.selected_pair_info.config(
                    text=info_text,
                    foreground=entry_color,
                    font=("Courier", 9),
                ),
            )

            if not app.active_position:
                app._pair_info_update_task = app.root.after(2000, app._update_pair_info)
        except asyncio.TimeoutError:
            logger.warning("Timeout while waiting for pair snapshot")
        except Exception as exc:
            logger.error("Error updating pair info: %s", exc)

    future = app._run_async(async_get_snapshot())
    if future:
        future.add_done_callback(on_snapshot_complete)


def update_pair_price_info(app: Any, pair: str, recommendation: str = "") -> None:
    """Fetch current mark prices across exchanges and update the UI."""
    if not app.connected:
        return

    async def async_get_price():
        prices = {}
        for exchange, client in app.manager.clients.items():
            try:
                symbol = get_exchange_symbol(pair, exchange)
                prices[exchange] = await client.get_mark_price(symbol)
            except Exception as exc:
                logger.debug("Could not get price from %s: %s", exchange.value, exc)
        return prices

    def on_complete(future: Any) -> None:
        try:
            prices = future.result()
            if prices:
                avg_price = sum(prices.values()) / len(prices)
                price_info = f"${avg_price:,.6f}"
                price_details = " | ".join([f"{exchange.value}: ${price:,.6f}" for exchange, price in prices.items()])
                app.root.after(0, app._update_price_display, price_info, price_details)
                return

            app.root.after(0, app._update_price_display, "", "")
        except Exception as exc:
            logger.error("Error getting prices for %s: %s", recommendation or pair, exc)
            app.root.after(0, app._update_price_display, "", "")

    future = app._run_async(async_get_price())
    if future:
        future.add_done_callback(on_complete)


def update_price_display(app: Any, price_info: str, price_details: str) -> None:
    """Render current price summary and optionally log per-exchange prices."""
    app.current_price_label.config(text=price_info)
    if price_details:
        app._log(f"Current prices: {price_details}")
