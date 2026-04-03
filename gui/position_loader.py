"""
Helpers for loading existing positions into the GUI.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional

import tkinter as tk
from tkinter import messagebox, ttk

from config.constants import Side, get_unified_pair

try:
    from gui.exchange_display import to_display_name
except ImportError:
    from exchange_display import to_display_name


logger = logging.getLogger(__name__)


def scan_existing_positions(app: Any) -> None:
    """Scan connected exchanges for open positions and show a load dialog."""
    if not app.connected:
        messagebox.showerror("Error", "Not connected to exchanges")
        return

    app._log("Scanning for existing positions on all exchanges...")
    app.load_pos_btn.config(state=tk.DISABLED)

    async def scan_positions():
        return await app.manager.scan_all_positions()

    def on_scan_complete(future: Any) -> None:
        def update_ui() -> None:
            app.load_pos_btn.config(state=tk.NORMAL)
            try:
                all_positions = future.result(timeout=0.5)

                positions_found: list[dict[str, Any]] = []
                for exchange, positions in all_positions.items():
                    for position in positions:
                        positions_found.append(
                            {
                                "exchange": exchange,
                                "symbol": position.symbol,
                                "side": position.side,
                                "size": position.size,
                                "entry_price": position.entry_price,
                                "unrealized_pnl": position.unrealized_pnl,
                                "leverage": position.leverage,
                            }
                        )

                if not positions_found:
                    app._log("No existing positions found on any exchange")
                    messagebox.showinfo("Load Positions", "No existing positions found")
                    return

                app._log(f"Found {len(positions_found)} position(s):")
                for position in positions_found:
                    app._log(
                        f"   - {position['exchange'].value}: {position['symbol']} "
                        f"{position['side'].value.upper()} Size: {position['size']} "
                        f"PnL: ${position['unrealized_pnl']:.2f}"
                    )

                hedged_pairs = app._find_hedged_pairs(positions_found)
                if hedged_pairs:
                    app._log(f"Found {len(hedged_pairs)} potential hedged pair(s)")
                    app._show_position_selector(hedged_pairs, positions_found)
                    return

                app._log("No matching hedged pairs found. Positions may be one-sided.")
                app._show_position_selector([], positions_found)
            except Exception as exc:
                app._log(f"Error scanning positions: {exc}")
                logger.error("Error scanning existing positions: %s", exc)

        app.root.after(0, update_ui)

    future = app._run_async(scan_positions())
    if future:
        future.add_done_callback(on_scan_complete)


def find_hedged_pairs(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find matching hedged pairs from a flat list of exchange positions."""
    hedged_pairs: list[dict[str, Any]] = []
    used_positions: set[int] = set()

    for i, first in enumerate(positions):
        if i in used_positions:
            continue

        for j, second in enumerate(positions):
            if j in used_positions or j == i:
                continue
            if first["exchange"] == second["exchange"]:
                continue

            try:
                first_pair = get_unified_pair(first["symbol"], first["exchange"])
                second_pair = get_unified_pair(second["symbol"], second["exchange"])
            except Exception:
                continue

            if first_pair == second_pair and first["side"] != second["side"]:
                long_pos = first if first["side"] == Side.LONG else second
                short_pos = second if first["side"] == Side.LONG else first

                hedged_pairs.append(
                    {
                        "pair": first_pair,
                        "long": long_pos,
                        "short": short_pos,
                        "total_pnl": first["unrealized_pnl"] + second["unrealized_pnl"],
                    }
                )
                used_positions.add(i)
                used_positions.add(j)
                break

    return hedged_pairs


def show_position_selector(app: Any, hedged_pairs: list[dict[str, Any]], all_positions: list[dict[str, Any]]) -> None:
    """Show a dialog for selecting a hedged pair to load into monitoring."""
    dialog = tk.Toplevel(app.root)
    dialog.title("Load Position")
    dialog.geometry("600x500")
    dialog.transient(app.root)
    dialog.grab_set()

    ttk.Label(dialog, text="Select a position to monitor:", font=("Arial", 11, "bold")).pack(pady=10)

    listbox = tk.Listbox(dialog, width=80, height=12, font=("Courier", 9))
    listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

    position_data: list[dict[str, Any] | None] = []

    if hedged_pairs:
        listbox.insert(tk.END, "=== HEDGED PAIRS ===")
        position_data.append(None)

        for pair_data in hedged_pairs:
            text = (
                f"  {pair_data['pair']}: LONG {pair_data['long']['exchange'].value} / "
                f"SHORT {pair_data['short']['exchange'].value} | "
                f"PnL: ${pair_data['total_pnl']:.2f}"
            )
            listbox.insert(tk.END, text)
            position_data.append({"type": "hedged", "data": pair_data})

    listbox.insert(tk.END, "")
    position_data.append(None)
    listbox.insert(tk.END, "=== INDIVIDUAL POSITIONS ===")
    position_data.append(None)

    for position in all_positions:
        text = (
            f"  {position['exchange'].value}: {position['symbol']} {position['side'].value.upper()} "
            f"Size: {position['size']:.4f} PnL: ${position['unrealized_pnl']:.2f}"
        )
        listbox.insert(tk.END, text)
        position_data.append({"type": "single", "data": position})

    ttk.Separator(dialog, orient="horizontal").pack(fill="x", padx=10, pady=10)

    time_frame = ttk.LabelFrame(dialog, text="Position Open Time (for funding fee calculation)")
    time_frame.pack(fill="x", padx=10, pady=5)

    time_option = tk.StringVar(value="lookback_24h")
    ttk.Radiobutton(
        time_frame,
        text="Opened within last 24 hours (default)",
        variable=time_option,
        value="lookback_24h",
    ).pack(anchor="w", padx=10, pady=2)
    ttk.Radiobutton(
        time_frame,
        text="Opened within last 48 hours",
        variable=time_option,
        value="lookback_48h",
    ).pack(anchor="w", padx=10, pady=2)
    ttk.Radiobutton(
        time_frame,
        text="Opened within last 7 days",
        variable=time_option,
        value="lookback_7d",
    ).pack(anchor="w", padx=10, pady=2)

    custom_frame = ttk.Frame(time_frame)
    custom_frame.pack(anchor="w", padx=10, pady=2)

    ttk.Radiobutton(custom_frame, text="Custom date/time (UTC):", variable=time_option, value="custom").pack(
        side="left"
    )

    custom_datetime = tk.StringVar(value=(datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M"))
    custom_entry = ttk.Entry(custom_frame, textvariable=custom_datetime, width=20)
    custom_entry.pack(side="left", padx=5)
    ttk.Label(custom_frame, text="(YYYY-MM-DD HH:MM)").pack(side="left")

    def get_open_time() -> Optional[datetime]:
        option = time_option.get()
        now = datetime.now(timezone.utc)

        if option == "lookback_24h":
            return now - timedelta(hours=24)
        if option == "lookback_48h":
            return now - timedelta(hours=48)
        if option == "lookback_7d":
            return now - timedelta(days=7)
        if option == "custom":
            try:
                return datetime.strptime(custom_datetime.get(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except ValueError:
                messagebox.showerror("Error", "Invalid date format. Use YYYY-MM-DD HH:MM")
                custom_entry.focus_set()
                return None
        return now - timedelta(hours=24)

    def on_load() -> None:
        selection = listbox.curselection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a position")
            return

        selected = position_data[selection[0]]
        if selected is None:
            messagebox.showwarning("Warning", "Please select a valid position, not a separator")
            return

        open_time = get_open_time()
        if open_time is None:
            return

        dialog.destroy()

        if selected["type"] == "hedged":
            load_hedged_position(app, selected["data"], open_time)
            return

        app._log("Single position selected - cannot monitor as hedged pair")
        app._log(
            f"   {selected['data']['exchange'].value}: "
            f"{selected['data']['symbol']} {selected['data']['side'].value}"
        )

    btn_frame = ttk.Frame(dialog)
    btn_frame.pack(pady=10)

    ttk.Button(btn_frame, text="Load & Monitor", command=on_load).pack(side=tk.LEFT, padx=5)
    ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=5)


def load_hedged_position(app: Any, hedged_pair: dict[str, Any], open_time: Optional[datetime] = None) -> None:
    """Load a hedged pair into the GUI monitor state."""
    pair = hedged_pair["pair"]
    long_pos = hedged_pair["long"]
    short_pos = hedged_pair["short"]

    long_exchange = long_pos["exchange"]
    short_exchange = short_pos["exchange"]
    size = (long_pos["size"] + short_pos["size"]) / 2

    if open_time is None:
        open_time = datetime.now(timezone.utc) - timedelta(hours=24)

    app._log(f"Loading hedged position: {pair}")
    app._log(f"   LONG: {long_exchange.value} Size: {long_pos['size']}")
    app._log(f"   SHORT: {short_exchange.value} Size: {short_pos['size']}")
    app._log(f"   Open time (for funding): {open_time.strftime('%Y-%m-%d %H:%M')} UTC")

    app.active_position = {
        "pair": pair,
        "long_exchange": long_exchange,
        "short_exchange": short_exchange,
        "size": size,
        "long_size": long_pos["size"],
        "short_size": short_pos["size"],
        "leverage": min(long_pos.get("leverage", 1), short_pos.get("leverage", 1)),
        "open_time": open_time,
        "total_funding_fees": 0.0,
        "last_funding_check": datetime.now(timezone.utc),
        "initial_long_rate": 0,
        "initial_short_rate": 0,
        "initial_net_funding": 0,
        "loaded_position": True,
        "session_id": app.service.new_session_id(),
    }

    app._update_position_display()
    app.service.persist_active_position(app.active_position)
    app.service.record_trade_event("position_loaded", app.active_position)

    async def enrich_loaded_position() -> dict[str, Any]:
        return await app.service.get_initial_position_state(
            pair,
            long_exchange,
            short_exchange,
            size,
            leverage=app.active_position["leverage"],
            open_time=open_time,
            session_id=app.active_position["session_id"],
        )

    def on_enriched(future: Any) -> None:
        def update_ui() -> None:
            try:
                baseline = future.result(timeout=0.2)
                app.active_position.update(baseline)
                app.service.persist_active_position(app.active_position)
                app._update_position_display()
            except Exception:
                pass

        app.root.after(0, update_ui)

    future = app._run_async(enrich_loaded_position())
    if future:
        future.add_done_callback(on_enriched)

    app.long_exchange.set(to_display_name(long_exchange))
    app.short_exchange.set(to_display_name(short_exchange))

    current_values = list(app.pair_combo["values"])
    if pair not in current_values:
        current_values.append(pair)
        app.pair_combo["values"] = current_values
    app.pair_combo.set(pair)

    app._log("Position loaded. You can now monitor or close it.")
