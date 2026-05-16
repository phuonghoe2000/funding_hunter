"""
Detachable basis checker window for the main Funding Hunter GUI.
"""

from __future__ import annotations

from typing import Any

import tkinter as tk
from tkinter import scrolledtext, ttk
import time

from core.basis_checker_service import (
    AUTO_SIGNAL_COOLDOWN_SECONDS,
    BasisMarketData,
    DEFAULT_ALERT_THRESHOLD,
    DEFAULT_MIN_FUNDING_DIFF,
    MAX_AUTO_SIGNALS_PER_SCAN,
    MIN_EXEC_BASIS_SIGNAL,
    build_executable_basis_rows,
    build_funding_aligned_candidates,
    fetch_basis_market_data,
    fetch_order_books,
    format_basis_signal_message,
    get_normalized_funding,
    load_basis_config,
    load_ignored_symbols,
    normalize_telegram_symbol,
    price_format,
    quantity_format,
    save_basis_config,
    save_ignored_symbols,
    send_telegram_message,
)


class BasisCheckerPanel:
    """Binance/Asterdex basis checker embedded as a Toplevel window."""

    def __init__(self, app: Any, parent: tk.Toplevel):
        self.app = app
        self.parent = parent
        self.market_data = BasisMarketData()
        self.executable_basis_rows: list[dict[str, Any]] = []
        self.running = True
        self.refreshing = False
        self.sort_col: str | None = None
        self.sort_reverse = False
        self._refresh_after_id: str | None = None
        self._ob_window: tk.Toplevel | None = None
        self._ob_symbol: str | None = None
        self._ob_running = False

        cfg = load_basis_config()
        self.tg_token = cfg.get("telegram_bot_token", "")
        self.tg_chat_id = cfg.get("telegram_chat_id", "")
        self.tg_enabled = bool(cfg.get("telegram_enabled", False))
        self.alert_threshold = self._parse_float(cfg.get("alert_threshold"), DEFAULT_ALERT_THRESHOLD)
        self.min_basis = self._parse_float(cfg.get("min_basis"), 1.0)
        self.min_funding_diff = self._parse_float(cfg.get("min_funding_diff"), DEFAULT_MIN_FUNDING_DIFF)
        self.alerted_symbols: dict[str, float] = {}
        self.alert_hit_count: dict[str, int] = {}
        self.alert_scan_running = False
        self.auto_signal_enabled = False
        self.auto_signal_count = 0
        self.auto_signal_last_sent: dict[tuple[str, str, str], float] = {}
        self.auto_signal_scan_running = False
        self.ignored_symbols_by_chat = load_ignored_symbols(cfg)
        self._trade_log_after_id: str | None = None

        self._build_ui()
        self._update_trade_status()
        self.refresh()

    def close(self) -> None:
        self.running = False
        if self._refresh_after_id:
            try:
                self.parent.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
            self._refresh_after_id = None
        self._close_order_book()

    def refresh(self) -> None:
        if self.refreshing or not self.running:
            return
        self.refreshing = True
        self.status_label.config(text="Refreshing...", foreground="gray")
        self.refresh_btn.config(state=tk.DISABLED)

        future = self.app._run_async(self._fetch_rows())
        if not future:
            self.refreshing = False
            self.refresh_btn.config(state=tk.NORMAL)
            self.status_label.config(text="Async loop is not ready", foreground="red")
            return

        future.add_done_callback(self._dispatch_rows_ready)

    def _dispatch_rows_ready(self, future: Any) -> None:
        if not self.running:
            return
        try:
            self.parent.after(0, lambda: self._on_rows_ready(future))
        except tk.TclError:
            self.running = False

    async def _fetch_rows(self) -> tuple[BasisMarketData, list[dict[str, Any]]]:
        market_data = await fetch_basis_market_data()
        candidates = build_funding_aligned_candidates(market_data)
        rows = await build_executable_basis_rows(candidates)
        return market_data, rows

    def _on_rows_ready(self, future: Any) -> None:
        if not self.running or not self.parent.winfo_exists():
            return

        self.refreshing = False
        self.refresh_btn.config(state=tk.NORMAL)
        try:
            self.market_data, self.executable_basis_rows = future.result()
        except Exception as exc:
            self.executable_basis_rows = []
            self.status_label.config(text=f"Error: {exc}", foreground="red")
            self._update_basis_table()
            self._schedule_refresh()
            return

        self._populate_pairs_list()
        self._update_basis_table()
        now = time.strftime("%H:%M:%S")
        self.status_label.config(
            text=(
                f"Updated {now} | BN: {len(self.market_data.binance_data)} | "
                f"AS: {len(self.market_data.aster_data)} | Common: {len(self.market_data.common_symbols)}"
            ),
            foreground="green",
        )
        self._check_alerts()
        self._check_auto_signal()
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self.running and self.parent.winfo_exists():
            self._refresh_after_id = self.parent.after(10000, self.refresh)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.parent, padding=(10, 8, 10, 4))
        top.pack(fill=tk.X)

        ttk.Label(top, text="Basis Checker", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        self.refresh_btn = ttk.Button(top, text="Refresh", command=self.refresh)
        self.refresh_btn.pack(side=tk.LEFT, padx=10)
        ttk.Button(top, text="Use Selected", command=self._use_selected_basis).pack(side=tk.LEFT)
        ttk.Button(top, text="Order Book", command=self._show_selected_order_book).pack(side=tk.LEFT, padx=6)
        self.status_label = ttk.Label(top, text="Loading...", foreground="gray")
        self.status_label.pack(side=tk.RIGHT)

        paned = ttk.PanedWindow(self.parent, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        left_frame = ttk.LabelFrame(paned, text="Common Pairs")
        paned.add(left_frame, weight=1)

        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(search_frame, text="Search:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._populate_pairs_list())
        ttk.Entry(search_frame, textvariable=self.search_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        list_frame = ttk.Frame(left_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        list_scroll = ttk.Scrollbar(list_frame)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.pairs_listbox = tk.Listbox(
            list_frame,
            selectmode=tk.BROWSE,
            yscrollcommand=list_scroll.set,
            font=("Consolas", 10),
        )
        self.pairs_listbox.pack(fill=tk.BOTH, expand=True)
        list_scroll.config(command=self.pairs_listbox.yview)
        self.pairs_listbox.bind("<Double-1>", self._show_pair_order_book)

        self.count_label = ttk.Label(left_frame, text="0 pairs")
        self.count_label.pack(anchor=tk.W, padx=6, pady=(0, 6))

        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=4)

        table_frame = ttk.LabelFrame(right_frame, text="Top Executable Basis (auto-refresh 10s)")
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = (
            "symbol",
            "binance_price",
            "aster_price",
            "diff_pct",
            "exec_basis",
            "bn_funding",
            "as_funding",
            "funding_diff",
            "recommendation",
        )
        self.basis_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)

        headers = {
            "symbol": ("Symbol", 100),
            "binance_price": ("Binance Price", 118),
            "aster_price": ("Aster Price", 118),
            "diff_pct": ("Mark Basis", 92),
            "exec_basis": ("Exec Basis", 92),
            "bn_funding": ("BN FR/4h", 90),
            "as_funding": ("AS FR/4h", 90),
            "funding_diff": ("FR Diff/4h", 90),
            "recommendation": ("Recommendation", 190),
        }
        for column, (title, width) in headers.items():
            self.basis_tree.heading(column, text=title, command=lambda c=column: self._sort_by(c))
            self.basis_tree.column(column, width=width, anchor=tk.E if column != "symbol" else tk.W, stretch=False)

        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.basis_tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.basis_tree.xview)
        self.basis_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.basis_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        self.basis_tree.tag_configure("positive", foreground="#008a00")
        self.basis_tree.tag_configure("negative", foreground="#b00020")
        self.basis_tree.tag_configure("neutral", foreground="#666666")
        self.basis_tree.bind("<Double-1>", lambda _: self._use_selected_basis())

        self._build_telegram_ui(right_frame)

    def _build_telegram_ui(self, parent: tk.Widget) -> None:
        telegram_frame = ttk.LabelFrame(parent, text="Telegram Alert", padding=6)
        telegram_frame.pack(fill=tk.X, pady=(6, 0))

        row1 = ttk.Frame(telegram_frame)
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="Bot Token:").pack(side=tk.LEFT)
        self.tg_token_var = tk.StringVar(value=self.tg_token)
        ttk.Entry(row1, textvariable=self.tg_token_var, width=42, show="*").pack(side=tk.LEFT, padx=5)
        ttk.Label(row1, text="Chat ID:").pack(side=tk.LEFT)
        self.tg_chatid_var = tk.StringVar(value=self.tg_chat_id)
        ttk.Entry(row1, textvariable=self.tg_chatid_var, width=16).pack(side=tk.LEFT, padx=5)
        self.tg_enabled_var = tk.BooleanVar(value=self.tg_enabled)
        ttk.Checkbutton(row1, text="Enable Alerts", variable=self.tg_enabled_var).pack(side=tk.LEFT, padx=5)

        row2 = ttk.Frame(telegram_frame)
        row2.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row2, text="Alert Threshold:").pack(side=tk.LEFT)
        self.threshold_var = tk.StringVar(value=str(self.alert_threshold))
        ttk.Entry(row2, textvariable=self.threshold_var, width=7).pack(side=tk.LEFT, padx=3)
        ttk.Label(row2, text="%").pack(side=tk.LEFT)
        ttk.Button(row2, text="Save", command=self._save_tg_config).pack(side=tk.LEFT, padx=(10, 3))
        ttk.Button(row2, text="Test", command=self._test_telegram).pack(side=tk.LEFT, padx=3)
        ttk.Button(row2, text="Send Selected", command=self._send_selected_to_telegram).pack(side=tk.LEFT, padx=3)
        self.tg_status = ttk.Label(row2, text="", foreground="gray")
        self.tg_status.pack(side=tk.LEFT, padx=5)

        signal_frame = ttk.LabelFrame(parent, text="Auto Signal (Hedged)", padding=6)
        signal_frame.pack(fill=tk.X, pady=(6, 0))

        row3 = ttk.Frame(signal_frame)
        row3.pack(fill=tk.X)
        ttk.Label(row3, text="Min Basis:").pack(side=tk.LEFT)
        self.min_basis_var = tk.StringVar(value=str(self.min_basis))
        ttk.Entry(row3, textvariable=self.min_basis_var, width=6).pack(side=tk.LEFT, padx=3)
        ttk.Label(row3, text="%").pack(side=tk.LEFT)
        ttk.Label(row3, text="Min Funding Diff:").pack(side=tk.LEFT, padx=(12, 0))
        self.min_funding_diff_var = tk.StringVar(value=str(self.min_funding_diff))
        ttk.Entry(row3, textvariable=self.min_funding_diff_var, width=7).pack(side=tk.LEFT, padx=3)
        ttk.Label(row3, text="% / 4h").pack(side=tk.LEFT)
        ttk.Button(row3, text="Save", command=self._save_trade_config).pack(side=tk.LEFT, padx=(10, 3))
        self.auto_signal_btn = ttk.Button(row3, text="Enable Auto Signal", command=self._toggle_auto_signal)
        self.auto_signal_btn.pack(side=tk.LEFT, padx=3)
        self.trade_status_label = ttk.Label(row3, text="", foreground="gray")
        self.trade_status_label.pack(side=tk.LEFT, padx=8)

        self.trade_log_text = tk.Text(
            signal_frame,
            height=3,
            font=("Consolas", 9),
            wrap=tk.WORD,
            state=tk.DISABLED,
            bg="#fafafa",
        )
        self.trade_log_text.pack(fill=tk.X, pady=(5, 0))

    @staticmethod
    def _parse_float(raw_value: Any, default: float) -> float:
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return default

    def _read_tg_controls(self) -> None:
        self.tg_token = self.tg_token_var.get().strip()
        self.tg_chat_id = self.tg_chatid_var.get().strip()
        self.tg_enabled = bool(self.tg_enabled_var.get())
        self.alert_threshold = self._parse_float(self.threshold_var.get(), DEFAULT_ALERT_THRESHOLD)

    def _read_trade_controls(self) -> None:
        self.min_basis = self._parse_float(self.min_basis_var.get(), 1.0)
        self.min_funding_diff = abs(self._parse_float(self.min_funding_diff_var.get(), DEFAULT_MIN_FUNDING_DIFF))

    def _persist_config(self) -> None:
        cfg = load_basis_config()
        save_ignored_symbols(cfg, self.ignored_symbols_by_chat)
        cfg.update(
            {
                "telegram_bot_token": self.tg_token,
                "telegram_chat_id": self.tg_chat_id,
                "telegram_enabled": self.tg_enabled,
                "alert_threshold": self.alert_threshold,
                "min_basis": self.min_basis,
                "min_funding_diff": self.min_funding_diff,
            }
        )
        save_basis_config(cfg)

    def _save_tg_config(self) -> None:
        self._read_tg_controls()
        self._read_trade_controls()
        self._persist_config()
        self._set_tg_status("Saved", "green")

    def _save_trade_config(self) -> None:
        self._read_tg_controls()
        self._read_trade_controls()
        self._persist_config()
        self._trade_log("Signal config saved")
        self._update_trade_status()

    def _set_tg_status(self, message: str, color: str = "gray") -> None:
        self.tg_status.config(text=message, foreground=color)
        if message:
            self.parent.after(3000, lambda: self.tg_status.winfo_exists() and self.tg_status.config(text=""))

    def _validate_telegram_target(self) -> bool:
        self._read_tg_controls()
        if not self.tg_token or not self.tg_chat_id:
            self._set_tg_status("Fill token and chat ID", "red")
            return False
        return True

    def _is_ignored_symbol(self, symbol: str) -> bool:
        normalized = normalize_telegram_symbol(symbol)
        if not normalized:
            return False
        chat_key = str(self.tg_chat_id)
        symbols = self.ignored_symbols_by_chat.get(chat_key)
        if not symbols:
            return False
        until_ts = symbols.get(normalized)
        if not until_ts:
            return False
        if until_ts > time.time():
            return True
        symbols.pop(normalized, None)
        return False

    def _test_telegram(self) -> None:
        if not self._validate_telegram_target():
            return
        self._set_tg_status("Sending...", "gray")
        future = self.app._run_async(
            send_telegram_message(
                self.tg_token,
                self.tg_chat_id,
                "<b>Basis Checker</b>\nTest message - alert system working!",
                return_details=True,
            )
        )
        if future:
            future.add_done_callback(lambda done: self._dispatch_telegram_result(done, "Test sent"))

    def _send_selected_to_telegram(self) -> None:
        row = self._selected_row()
        if not row:
            self._set_tg_status("Select a row first", "red")
            return
        if not self._validate_telegram_target():
            return
        self._read_trade_controls()
        self._set_tg_status("Sending selected...", "gray")
        future = self.app._run_async(
            send_telegram_message(
                self.tg_token,
                self.tg_chat_id,
                self._format_signal_message(row),
                return_details=True,
            )
        )
        if future:
            future.add_done_callback(lambda done: self._dispatch_telegram_result(done, "Selected sent"))

    def _dispatch_telegram_result(self, future: Any, success_message: str) -> None:
        if not self.running:
            return
        try:
            self.parent.after(0, lambda: self._on_telegram_result(future, success_message))
        except tk.TclError:
            self.running = False

    def _on_telegram_result(self, future: Any, success_message: str) -> None:
        if not self.running:
            return
        try:
            result = future.result()
        except Exception as exc:
            self._set_tg_status(f"Error: {exc}", "red")
            return
        ok = result.get("ok") if isinstance(result, dict) else bool(result)
        if ok:
            suffix = " (SSL fallback)" if isinstance(result, dict) and result.get("ssl_fallback") else ""
            self._set_tg_status(f"{success_message}{suffix}", "green")
            return

        description = result.get("description", "") if isinstance(result, dict) else ""
        error_code = result.get("error_code") if isinstance(result, dict) else None
        if error_code == 401:
            description = "Unauthorized: invalid bot token"
        elif error_code == 400 and "chat" in description.lower():
            description = f"{description}: bot may not be in chat/group"
        self._set_tg_status(f"Failed - {(description or 'check config')[:80]}", "red")

    def _check_alerts(self) -> None:
        self._read_tg_controls()
        if not self.tg_enabled or not self.tg_token or not self.tg_chat_id:
            return
        if self.alert_scan_running:
            return

        now = time.time()
        candidates = []
        for row in self.executable_basis_rows:
            symbol = row["symbol"]
            if self._is_ignored_symbol(symbol):
                self.alert_hit_count.pop(symbol, None)
                continue

            if abs(row["diff_pct"]) >= self.alert_threshold and abs(row["exec_basis"]) > MIN_EXEC_BASIS_SIGNAL:
                self.alert_hit_count[symbol] = self.alert_hit_count.get(symbol, 0) + 1
                if self.alert_hit_count[symbol] < 2:
                    continue
                if now - self.alerted_symbols.get(symbol, 0) < AUTO_SIGNAL_COOLDOWN_SECONDS:
                    continue
                candidates.append(row)
            else:
                self.alert_hit_count.pop(symbol, None)

        if not candidates:
            return

        self.alert_scan_running = True
        future = self.app._run_async(self._send_alert_batch(candidates[:10]))
        if future:
            future.add_done_callback(lambda done: self._dispatch_alert_done(done))
        else:
            self.alert_scan_running = False

    async def _send_alert_batch(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        alerts = []
        for row in rows:
            direction = "Aster > Binance" if row["diff_pct"] >= 0 else "Binance > Aster"
            ticker = self.market_data.tickers_24h.get(row["symbol"])
            change = f"  24h: {ticker['price_change_pct']:+.1f}%" if ticker else ""
            alerts.append(
                f"<b>{row['symbol']}</b>  Mark basis: {row['diff_pct']:+.4f}%\n"
                f"  Exec basis: {row['exec_basis']:+.4f}%\n"
                f"  BN: {row['binance_price']}  AS: {row['aster_price']}\n"
                f"  {direction}{change}"
            )

        message = (
            f"<b>Basis Alert (threshold {self.alert_threshold}%)</b>\n"
            f"Exec basis > {MIN_EXEC_BASIS_SIGNAL:.2f}% | Time: {time.strftime('%H:%M:%S')}\n\n"
            + "\n\n".join(alerts)
        )
        details = await send_telegram_message(self.tg_token, self.tg_chat_id, message, return_details=True)
        return {"details": details, "symbols": [row["symbol"] for row in rows]}

    def _dispatch_alert_done(self, future: Any) -> None:
        if not self.running:
            return
        try:
            self.parent.after(0, lambda: self._on_alert_done(future))
        except tk.TclError:
            self.running = False

    def _on_alert_done(self, future: Any) -> None:
        self.alert_scan_running = False
        try:
            result = future.result()
        except Exception as exc:
            self._trade_log(f"Alert cycle failed: {exc}")
            return

        details = result.get("details", {})
        if details.get("ok"):
            now = time.time()
            for symbol in result.get("symbols", []):
                self.alerted_symbols[symbol] = now
                self.alert_hit_count[symbol] = 0
            self._trade_log(f"Telegram alert sent for {len(result.get('symbols', []))} symbol(s)")
        else:
            self._trade_log(f"Telegram alert failed: {details.get('description', 'unknown error')}")

    def _toggle_auto_signal(self) -> None:
        if self.auto_signal_enabled:
            self.auto_signal_enabled = False
            self.auto_signal_btn.config(text="Enable Auto Signal")
            self._trade_log("Auto signal DISABLED")
            self._update_trade_status()
            return

        if not self._validate_telegram_target():
            return
        self._read_trade_controls()
        self._persist_config()
        self.auto_signal_enabled = True
        self.auto_signal_btn.config(text="Disable Auto Signal")
        self._trade_log("Auto signal ENABLED")
        self._update_trade_status()
        self._check_auto_signal()

    def _update_trade_status(self) -> None:
        status = "ON" if self.auto_signal_enabled else "OFF"
        color = "#008a00" if self.auto_signal_enabled else "gray"
        self.trade_status_label.config(
            text=(
                f"Status: {status} | Signals Sent: {self.auto_signal_count} | "
                f"Min Basis: {self.min_basis:.2f}% | Min Funding Diff: {self.min_funding_diff:.4f}%"
            ),
            foreground=color,
        )

    def _trade_log(self, message: str) -> None:
        if not hasattr(self, "trade_log_text"):
            return
        self.trade_log_text.config(state=tk.NORMAL)
        self.trade_log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.trade_log_text.see(tk.END)
        self.trade_log_text.config(state=tk.DISABLED)

    def _check_auto_signal(self) -> None:
        if not self.auto_signal_enabled:
            return
        self._read_tg_controls()
        self._read_trade_controls()
        if not self.tg_token or not self.tg_chat_id:
            return
        if self.auto_signal_scan_running:
            return

        now = time.time()
        candidates = []
        for row in self.executable_basis_rows:
            if self._is_ignored_symbol(row["symbol"]):
                continue
            if abs(row["diff_pct"]) < self.min_basis:
                continue
            if abs(row["funding_diff"]) < self.min_funding_diff:
                continue
            if abs(row["exec_basis"]) <= MIN_EXEC_BASIS_SIGNAL:
                continue

            key = self._signal_key(row)
            if now - self.auto_signal_last_sent.get(key, 0) < AUTO_SIGNAL_COOLDOWN_SECONDS:
                continue
            candidates.append(row)

        if not candidates:
            return

        candidates.sort(key=lambda row: abs(row["exec_basis"]), reverse=True)
        self.auto_signal_scan_running = True
        future = self.app._run_async(self._send_auto_signal_batch(candidates[:MAX_AUTO_SIGNALS_PER_SCAN]))
        if future:
            future.add_done_callback(lambda done: self._dispatch_auto_signal_done(done))
        else:
            self.auto_signal_scan_running = False

    async def _send_auto_signal_batch(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        sent = []
        failures = []
        for row in rows:
            details = await send_telegram_message(
                self.tg_token,
                self.tg_chat_id,
                self._format_signal_message(row),
                return_details=True,
            )
            if isinstance(details, dict) and details.get("ok"):
                sent.append({"key": self._signal_key(row), "symbol": row["symbol"]})
            else:
                description = details.get("description", "unknown error") if isinstance(details, dict) else "unknown error"
                failures.append({"symbol": row["symbol"], "description": description})
        return {"sent": sent, "failures": failures}

    def _dispatch_auto_signal_done(self, future: Any) -> None:
        if not self.running:
            return
        try:
            self.parent.after(0, lambda: self._on_auto_signal_done(future))
        except tk.TclError:
            self.running = False

    def _on_auto_signal_done(self, future: Any) -> None:
        self.auto_signal_scan_running = False
        try:
            result = future.result()
        except Exception as exc:
            self._trade_log(f"Auto signal cycle failed: {exc}")
            return

        now = time.time()
        for item in result.get("sent", []):
            self.auto_signal_last_sent[tuple(item["key"])] = now
            self.auto_signal_count += 1
            self._trade_log(f"Telegram sent for {item['symbol']}")
        for item in result.get("failures", []):
            self._trade_log(f"Telegram FAILED for {item['symbol']}: {item['description']}")
        self._update_trade_status()

    def _signal_key(self, row: dict[str, Any]) -> tuple[str, str, str]:
        return (row["symbol"], row.get("rec_long", ""), row.get("rec_short", ""))

    def _format_signal_message(self, row: dict[str, Any]) -> str:
        return format_basis_signal_message(
            symbol=row["symbol"],
            rec_long=row.get("rec_long", ""),
            rec_short=row.get("rec_short", ""),
            mark_basis=row["diff_pct"],
            exec_basis=row["exec_basis"],
            fr_diff=row["funding_diff"],
            reference_price=row.get("reference_price", 0),
            min_basis=self.min_basis,
            min_funding_diff=self.min_funding_diff,
        )

    def _populate_pairs_list(self) -> None:
        self.pairs_listbox.delete(0, tk.END)
        query = self.search_var.get().strip().upper()
        count = 0
        for symbol in self.market_data.common_symbols:
            if query and query not in symbol:
                continue
            ticker = self.market_data.tickers_24h.get(symbol)
            suffix = f" ({ticker['price_change_pct']:+.1f}%)" if ticker else ""
            self.pairs_listbox.insert(tk.END, f"{symbol}{suffix}")
            count += 1
        self.count_label.config(text=f"{count} common pairs")

    def _sort_by(self, column: str) -> None:
        if self.sort_col == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = column
            self.sort_reverse = False
        self._update_basis_table()

    def _update_basis_table(self) -> None:
        self.basis_tree.delete(*self.basis_tree.get_children())
        rows = list(self.executable_basis_rows)

        if self.sort_col:
            try:
                if self.sort_col == "exec_basis":
                    rows.sort(key=lambda row: abs(row.get("exec_basis", 0)), reverse=self.sort_reverse)
                elif self.sort_col == "recommendation":
                    rows.sort(key=self._recommendation_text, reverse=self.sort_reverse)
                else:
                    rows.sort(key=lambda row: row.get(self.sort_col, 0), reverse=self.sort_reverse)
            except TypeError:
                pass
        else:
            rows.sort(key=lambda row: abs(row["exec_basis"]), reverse=True)

        for row in rows[:50]:
            price_fmt = price_format(row["binance_price"])
            exec_basis = row["exec_basis"]
            tag = "positive" if exec_basis > 0 else ("negative" if exec_basis < 0 else "neutral")
            self.basis_tree.insert(
                "",
                tk.END,
                values=(
                    row["symbol"],
                    f"{row['binance_price']:{price_fmt}}",
                    f"{row['aster_price']:{price_fmt}}",
                    f"{row['diff_pct']:+.4f}%",
                    f"{exec_basis:+.4f}%",
                    f"{row['bn_funding']:.4f}%",
                    f"{row['as_funding']:.4f}%",
                    f"{row['funding_diff']:+.4f}%",
                    self._recommendation_text(row),
                ),
                tags=(tag,),
            )

    def _recommendation_text(self, row: dict[str, Any]) -> str:
        long_display = "Binance" if row.get("rec_long") == "binance" else "Asterdex"
        short_display = "Binance" if row.get("rec_short") == "binance" else "Asterdex"
        return f"Long {long_display}, Short {short_display}"

    def _selected_row(self) -> dict[str, Any] | None:
        selection = self.basis_tree.selection()
        if not selection:
            return None
        values = self.basis_tree.item(selection[0], "values")
        if not values:
            return None
        symbol = values[0]
        for row in self.executable_basis_rows:
            if row.get("symbol") == symbol:
                return row
        return None

    def _use_selected_basis(self) -> None:
        row = self._selected_row()
        if not row:
            return
        self.app._apply_basis_checker_selection(row)

    def _show_selected_order_book(self) -> None:
        row = self._selected_row()
        if row:
            self._show_order_book(row["symbol"])

    def _show_pair_order_book(self, _: tk.Event) -> None:
        selection = self.pairs_listbox.curselection()
        if not selection:
            return
        raw = self.pairs_listbox.get(selection[0])
        symbol = raw.split()[0].strip().upper()
        if symbol:
            self._show_order_book(symbol)

    def _show_order_book(self, symbol: str) -> None:
        self._close_order_book()

        win = tk.Toplevel(self.parent)
        win.title(f"{symbol} - Binance/Asterdex Order Book")
        win.geometry("960x700")
        win.minsize(850, 550)
        win.transient(self.parent)
        self._ob_window = win
        self._ob_symbol = symbol
        self._ob_running = True

        top = ttk.Frame(win, padding=(8, 8, 8, 4))
        top.pack(fill=tk.X)
        ttk.Label(top, text=symbol, font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        self._ob_status = ttk.Label(top, text="Loading...", foreground="gray")
        self._ob_status.pack(side=tk.RIGHT)

        books_frame = ttk.Frame(win, padding=(8, 0, 8, 4))
        books_frame.pack(fill=tk.BOTH, expand=True)

        self._bn_ob_tree = self._create_order_book_tree(books_frame, "Binance")
        self._as_ob_tree = self._create_order_book_tree(books_frame, "Asterdex")

        analysis_frame = ttk.LabelFrame(win, text="Analysis & Recommendation", padding=6)
        analysis_frame.pack(fill=tk.X, padx=8, pady=(0, 8))
        self._analysis_text = scrolledtext.ScrolledText(
            analysis_frame,
            height=8,
            font=("Consolas", 10),
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        self._analysis_text.pack(fill=tk.X)
        self._analysis_text.tag_configure("green", foreground="#008a00")
        self._analysis_text.tag_configure("red", foreground="#b00020")
        self._analysis_text.tag_configure("bold", font=("Consolas", 10, "bold"))
        self._analysis_text.tag_configure("header", font=("Consolas", 11, "bold"))

        win.protocol("WM_DELETE_WINDOW", self._close_order_book)
        self._fetch_order_book()

    def _create_order_book_tree(self, parent: tk.Widget, title: str) -> ttk.Treeview:
        frame = ttk.LabelFrame(parent, text=title, padding=4)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4) if title == "Binance" else (4, 0))
        columns = ("price", "quantity", "total_usd")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=22)
        for column, text, width in (
            ("price", "Price", 130),
            ("quantity", "Qty", 110),
            ("total_usd", "Total $", 110),
        ):
            tree.heading(column, text=text)
            tree.column(column, width=width, anchor=tk.E)
        tree.pack(fill=tk.BOTH, expand=True)
        tree.tag_configure("ask", foreground="#b00020")
        tree.tag_configure("bid", foreground="#008a00")
        tree.tag_configure("spread", foreground="#666666", background="#f0f0f0")
        return tree

    def _close_order_book(self) -> None:
        self._ob_running = False
        if self._ob_window and self._ob_window.winfo_exists():
            self._ob_window.destroy()
        self._ob_window = None
        self._ob_symbol = None

    def _fetch_order_book(self) -> None:
        if not self._ob_running or not self._ob_window or not self._ob_symbol:
            return

        future = self.app._run_async(fetch_order_books(self._ob_symbol))
        if not future:
            self._ob_status.config(text="Async loop is not ready", foreground="red")
            return
        self._ob_window.after(100, lambda: self._check_order_book_result(future))

    def _check_order_book_result(self, future: Any) -> None:
        if not self._ob_running or not self._ob_window or not self._ob_window.winfo_exists():
            return
        if not future.done():
            self._ob_window.after(100, lambda: self._check_order_book_result(future))
            return

        try:
            bn_book, as_book = future.result()
            self._update_order_book_display(bn_book, as_book)
            self._ob_status.config(text=f"Updated {time.strftime('%H:%M:%S')} (auto-refresh 3s)", foreground="green")
        except Exception as exc:
            self._ob_status.config(text=f"Error: {exc}", foreground="red")

        if self._ob_running and self._ob_window:
            self._ob_window.after(3000, self._fetch_order_book)

    def _update_order_book_display(self, bn_book: dict[str, Any], as_book: dict[str, Any]) -> None:
        self._populate_order_book(self._bn_ob_tree, bn_book)
        self._populate_order_book(self._as_ob_tree, as_book)
        self._update_analysis(bn_book, as_book)

    def _populate_order_book(self, tree: ttk.Treeview, book: dict[str, Any]) -> None:
        tree.delete(*tree.get_children())

        for price, quantity in sorted(book["asks"][:10], key=lambda row: row[0], reverse=True):
            tree.insert(
                "",
                tk.END,
                values=(f"{price:{price_format(price)}}", quantity_format(quantity), f"${price * quantity:,.1f}"),
                tags=("ask",),
            )

        if book["asks"] and book["bids"]:
            best_ask = book["asks"][0][0]
            best_bid = book["bids"][0][0]
            spread_pct = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 0
            tree.insert("", tk.END, values=(f"--- Spread: {spread_pct:.4f}% ---", "", ""), tags=("spread",))

        for price, quantity in book["bids"][:10]:
            tree.insert(
                "",
                tk.END,
                values=(f"{price:{price_format(price)}}", quantity_format(quantity), f"${price * quantity:,.1f}"),
                tags=("bid",),
            )

    def _update_analysis(self, bn_book: dict[str, Any], as_book: dict[str, Any]) -> None:
        symbol = self._ob_symbol or ""
        text = self._analysis_text
        text.config(state=tk.NORMAL)
        text.delete("1.0", tk.END)

        bn_bid = bn_book["bids"][0][0] if bn_book["bids"] else 0
        bn_ask = bn_book["asks"][0][0] if bn_book["asks"] else 0
        as_bid = as_book["bids"][0][0] if as_book["bids"] else 0
        as_ask = as_book["asks"][0][0] if as_book["asks"] else 0

        bn_data = self.market_data.binance_data.get(symbol, {})
        as_data = self.market_data.aster_data.get(symbol, {})
        bn_mark = bn_data.get("mark_price", 0)
        as_mark = as_data.get("mark_price", 0)
        mark_basis = (as_mark - bn_mark) / bn_mark * 100 if bn_mark > 0 else 0
        _, _, funding_diff = get_normalized_funding(self.market_data, symbol)

        bn_spread = (bn_ask - bn_bid) / bn_bid * 100 if bn_bid > 0 else 0
        as_spread = (as_ask - as_bid) / as_bid * 100 if as_bid > 0 else 0
        bn_bid_depth = sum(price * qty for price, qty in bn_book["bids"][:10])
        bn_ask_depth = sum(price * qty for price, qty in bn_book["asks"][:10])
        as_bid_depth = sum(price * qty for price, qty in as_book["bids"][:10])
        as_ask_depth = sum(price * qty for price, qty in as_book["asks"][:10])

        text.insert(tk.END, f"Mark Basis: {mark_basis:+.4f}%", "header")
        text.insert(tk.END, f"  |  BN Spread: {bn_spread:.4f}%  |  AS Spread: {as_spread:.4f}%\n")
        text.insert(tk.END, f"Depth (10 lvl):  BN ${bn_bid_depth:,.0f} bid / ${bn_ask_depth:,.0f} ask")
        text.insert(tk.END, f"  |  AS ${as_bid_depth:,.0f} bid / ${as_ask_depth:,.0f} ask\n")

        if mark_basis >= 0:
            exec_basis = (as_bid - bn_ask) / bn_ask * 100 if bn_ask > 0 else 0
            rec_long, rec_short = "Binance", "Asterdex"
            fmt = price_format(bn_ask)
            text.insert(tk.END, f"Executable Basis: {exec_basis:+.4f}%", "bold")
            text.insert(tk.END, f"  (AS Bid {as_bid:{fmt}} - BN Ask {bn_ask:{fmt}})\n")
        else:
            exec_basis = (bn_bid - as_ask) / as_ask * 100 if as_ask > 0 else 0
            rec_long, rec_short = "Asterdex", "Binance"
            fmt = price_format(as_ask)
            text.insert(tk.END, f"Executable Basis: {exec_basis:+.4f}%", "bold")
            text.insert(tk.END, f"  (BN Bid {bn_bid:{fmt}} - AS Ask {as_ask:{fmt}})\n")

        text.insert(tk.END, "Recommendation: ", "bold")
        text.insert(tk.END, f"LONG {rec_long}", "green")
        text.insert(tk.END, " / ")
        text.insert(tk.END, f"SHORT {rec_short}\n", "red")

        if exec_basis > 0:
            text.insert(tk.END, "  OK  Executable basis positive\n", "green")
        else:
            text.insert(tk.END, f"  X  Executable basis negative ({exec_basis:+.4f}%)\n", "red")

        if mark_basis * funding_diff > 0:
            text.insert(tk.END, f"  OK  Funding/4h ({funding_diff:+.4f}%) supports basis", "green")
        elif mark_basis * funding_diff < 0:
            text.insert(tk.END, f"  X  Funding/4h ({funding_diff:+.4f}%) opposes basis", "red")
        else:
            text.insert(tk.END, "  ~  Funding diff is zero or basis is zero")

        text.config(state=tk.DISABLED)
