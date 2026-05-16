"""
Basis Checker - Compare prices between Binance and AsterDEX
Shows price difference (basis) for common trading pairs
Auto-selects top 3 gainers + top 3 losers by 24h change
Telegram bot alert support
"""
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import time
import json
import os
import logging

logger = logging.getLogger(__name__)

from core import basis_checker_service as basis_service

# Public API endpoints (no auth needed)
BINANCE_PREMIUM_URL = basis_service.BINANCE_PREMIUM_URL
ASTER_PREMIUM_URL = basis_service.ASTER_PREMIUM_URL
BINANCE_TICKER_URL = basis_service.BINANCE_TICKER_URL
BINANCE_EXCHANGE_INFO_URL = basis_service.BINANCE_EXCHANGE_INFO_URL
ASTER_EXCHANGE_INFO_URL = basis_service.ASTER_EXCHANGE_INFO_URL
BINANCE_DEPTH_URL = basis_service.BINANCE_DEPTH_URL
ASTER_DEPTH_URL = basis_service.ASTER_DEPTH_URL
BINANCE_FUNDING_INFO_URL = basis_service.BINANCE_FUNDING_INFO_URL
ASTER_FUNDING_INFO_URL = basis_service.ASTER_FUNDING_INFO_URL

# Config file for Telegram settings
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "basis_config.json")

# Default alert threshold
DEFAULT_ALERT_THRESHOLD = basis_service.DEFAULT_ALERT_THRESHOLD  # 0.3% basis diff triggers alert

# Auto signal requires funding support with at least this configurable minimum diff
DEFAULT_MIN_FUNDING_DIFF = basis_service.DEFAULT_MIN_FUNDING_DIFF
MIN_EXEC_BASIS_SIGNAL = basis_service.MIN_EXEC_BASIS_SIGNAL
AUTO_SIGNAL_COOLDOWN_SECONDS = 300
MAX_AUTO_SIGNALS_PER_SCAN = 3
MAX_EXECUTABLE_BASIS_CANDIDATES = basis_service.MAX_EXECUTABLE_BASIS_CANDIDATES
EXECUTABLE_BASIS_CONCURRENCY = basis_service.EXECUTABLE_BASIS_CONCURRENCY
TELEGRAM_HELLO_REPLY = "Basis Checker bot vẫn chạy."
TELEGRAM_POLL_TIMEOUT_SECONDS = 20
TELEGRAM_IGNORE_DURATION_SECONDS = 3600
TELEGRAM_IGNORED_SYMBOLS_CONFIG_KEY = "telegram_ignored_symbols"


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


async def fetch_trading_symbols(session, url):
    return await basis_service.fetch_trading_symbols(session, url)


async def fetch_all_mark_prices(session, url, trading_only=None):
    return await basis_service.fetch_all_mark_prices(session, url, trading_only)


async def fetch_24h_tickers(session):
    return await basis_service.fetch_24h_tickers(session)


async def fetch_funding_intervals(session, url):
    return await basis_service.fetch_funding_intervals(session, url)


async def fetch_all_data():
    return await basis_service.fetch_all_data()


def _price_fmt(price):
    return basis_service.price_format(price)


def _qty_fmt(qty):
    return basis_service.quantity_format(qty)


async def fetch_order_books(symbol, limit=10):
    return await basis_service.fetch_order_books(symbol, limit=limit)


async def fetch_order_books_for_symbols(symbols, limit=10, concurrency=8):
    return await basis_service.fetch_order_books_for_symbols(symbols, limit=limit, concurrency=concurrency)


def compute_executable_basis(mark_basis, bn_book, as_book):
    return basis_service.compute_executable_basis(mark_basis, bn_book, as_book)


async def telegram_api_request(
    bot_token,
    method_name,
    *,
    http_method="POST",
    payload=None,
    params=None,
):
    return await basis_service.telegram_api_request(
        bot_token,
        method_name,
        http_method=http_method,
        payload=payload,
        params=params,
    )


async def send_telegram_message(bot_token, chat_id, message, return_details=False):
    return await basis_service.send_telegram_message(
        bot_token,
        chat_id,
        message,
        return_details=return_details,
    )


class BasisCheckerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Basis Checker - Binance vs AsterDEX")
        self.root.geometry("1400x750")
        self.root.minsize(1100, 600)

        # State
        self.binance_data = {}
        self.aster_data = {}
        self.tickers_24h = {}
        self.bn_intervals = {}  # symbol -> fundingIntervalHours
        self.as_intervals = {}
        self.qty_precisions = {}  # symbol -> quantityPrecision
        self.common_symbols = []
        self.loop = None
        self.running = True
        self.sort_col = None
        self.sort_reverse = False
        self.executable_basis_rows = []
        self.executable_basis_scan_running = False

        # Telegram state
        cfg = load_config()
        self.tg_token = cfg.get("telegram_bot_token", "")
        self.tg_chat_id = cfg.get("telegram_chat_id", "")
        self.tg_enabled = cfg.get("telegram_enabled", False)
        self.alert_threshold = cfg.get("alert_threshold", DEFAULT_ALERT_THRESHOLD)
        self.alerted_symbols = {}  # symbol -> last_alert_time (cooldown 5min)
        self.alert_hit_count = {}  # symbol -> consecutive hit count (alert on 2nd hit)
        self.alert_scan_running = False
        self.telegram_listener_running = False
        self.telegram_listener_future = None
        self.telegram_update_offset = None
        self.telegram_listener_token = None
        self.ignored_symbols_by_chat = self._load_ignored_symbols(cfg)

        # Auto signal state
        self.min_basis = cfg.get("min_basis", 1.0)
        self.min_funding_diff = cfg.get("min_funding_diff", DEFAULT_MIN_FUNDING_DIFF)
        self.auto_trade_enabled = False
        self.auto_signal_count = 0
        self.auto_signal_last_sent = {}  # (symbol, long, short) -> last_sent_time
        self.auto_signal_scan_running = False

        self._build_ui()
        self._start_async_loop()
        self.root.after(1000, self._start_telegram_listener_if_ready)
        self.root.after(500, self._do_initial_fetch)

    # ── UI ──────────────────────────────────────────────────
    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=5, pady=5)
        ttk.Label(top, text="Basis Checker", font=("Segoe UI", 14, "bold")).pack(side="left")
        self.status_label = ttk.Label(top, text="Loading...", foreground="gray")
        self.status_label.pack(side="right")

        # Main paned window
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=5, pady=5)

        # LEFT: Common pairs list
        left_frame = ttk.LabelFrame(paned, text="All Common Pairs")
        paned.add(left_frame, weight=1)

        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill="x", padx=3, pady=3)
        ttk.Label(search_frame, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._filter_pairs)
        ttk.Entry(search_frame, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=5)

        list_frame = ttk.Frame(left_frame)
        list_frame.pack(fill="both", expand=True, padx=3, pady=3)
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.pairs_listbox = tk.Listbox(
            list_frame, selectmode="extended", yscrollcommand=scrollbar.set,
            font=("Consolas", 10)
        )
        self.pairs_listbox.pack(fill="both", expand=True)
        scrollbar.config(command=self.pairs_listbox.yview)

        self.count_label = ttk.Label(left_frame, text="0 pairs")
        self.count_label.pack(padx=3, pady=2)

        # RIGHT: top + basis table + bottom telegram
        right_frame = ttk.Frame(paned)
        paned.add(right_frame, weight=3)

        # Basis table
        table_frame = ttk.LabelFrame(right_frame, text="Top 10 Basis (auto-refresh 10s)")
        table_frame.pack(fill="both", expand=True)

        cols = ("symbol", "binance_price", "aster_price", "diff_pct", "exec_basis",
                "bn_funding", "as_funding", "funding_diff")
        self.basis_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=20)

        headers = {
            "symbol": ("Symbol", 100),
            "binance_price": ("Binance Price", 120),
            "aster_price": ("Aster Price", 120),
            "diff_pct": ("Mark Basis", 90),
            "exec_basis": ("Exec Basis", 90),
            "bn_funding": ("BN FR/4h", 90),
            "as_funding": ("AS FR/4h", 90),
            "funding_diff": ("FR Diff/4h", 90),
        }
        for col, (title, width) in headers.items():
            self.basis_tree.heading(col, text=title, command=lambda c=col: self._sort_by(c))
            self.basis_tree.column(col, width=width, anchor="e" if col != "symbol" else "w")

        tree_scroll = ttk.Scrollbar(table_frame, command=self.basis_tree.yview)
        self.basis_tree.configure(yscrollcommand=tree_scroll.set)
        self.basis_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        self.basis_tree.tag_configure("positive", foreground="#00aa00")
        self.basis_tree.tag_configure("negative", foreground="#cc0000")
        self.basis_tree.tag_configure("neutral", foreground="#666666")

        # Double-click to open order book detail
        self.basis_tree.bind("<Double-1>", self._on_basis_double_click)

        # Telegram config frame
        tg_frame = ttk.LabelFrame(right_frame, text="Telegram Alert")
        tg_frame.pack(fill="x", padx=0, pady=(5, 0))

        row1 = ttk.Frame(tg_frame)
        row1.pack(fill="x", padx=5, pady=2)
        ttk.Label(row1, text="Bot Token:").pack(side="left")
        self.tg_token_var = tk.StringVar(value=self.tg_token)
        ttk.Entry(row1, textvariable=self.tg_token_var, width=45, show="*").pack(side="left", padx=5)
        ttk.Label(row1, text="Chat ID:").pack(side="left")
        self.tg_chatid_var = tk.StringVar(value=self.tg_chat_id)
        ttk.Entry(row1, textvariable=self.tg_chatid_var, width=15).pack(side="left", padx=5)

        row2 = ttk.Frame(tg_frame)
        row2.pack(fill="x", padx=5, pady=2)
        ttk.Label(row2, text="Alert threshold (%):").pack(side="left")
        self.threshold_var = tk.StringVar(value=str(self.alert_threshold))
        ttk.Entry(row2, textvariable=self.threshold_var, width=8).pack(side="left", padx=5)

        self.tg_enabled_var = tk.BooleanVar(value=self.tg_enabled)
        ttk.Checkbutton(row2, text="Enable", variable=self.tg_enabled_var).pack(side="left", padx=5)
        ttk.Button(row2, text="Save", command=self._save_tg_config).pack(side="left", padx=5)
        ttk.Button(row2, text="Test", command=self._test_telegram).pack(side="left", padx=5)
        ttk.Button(row2, text="Start commands", command=self._start_telegram_listener_if_ready).pack(side="left", padx=5)
        self.tg_status = ttk.Label(row2, text="", foreground="gray")
        self.tg_status.pack(side="left", padx=5)

        # Auto signal config frame
        trade_frame = ttk.LabelFrame(right_frame, text="Auto Signal (Hedged)")
        trade_frame.pack(fill="x", padx=0, pady=(5, 0))

        tr1 = ttk.Frame(trade_frame)
        tr1.pack(fill="x", padx=5, pady=2)
        ttk.Label(
            tr1,
            text="Signal only. No order execution. Telegram will receive LONG/SHORT guidance.",
            foreground="gray",
        ).pack(side="left")

        tr2 = ttk.Frame(trade_frame)
        tr2.pack(fill="x", padx=5, pady=2)
        ttk.Label(tr2, text="Min Basis:").pack(side="left")
        self.min_basis_var = tk.StringVar(value=str(self.min_basis))
        ttk.Entry(tr2, textvariable=self.min_basis_var, width=5).pack(side="left", padx=2)
        ttk.Label(tr2, text="%").pack(side="left")
        ttk.Label(tr2, text="  Min Funding Diff:").pack(side="left", padx=(10, 0))
        self.min_funding_diff_var = tk.StringVar(value=str(self.min_funding_diff))
        ttk.Entry(tr2, textvariable=self.min_funding_diff_var, width=6).pack(side="left", padx=2)
        ttk.Label(tr2, text="% / 4h").pack(side="left")
        ttk.Button(tr2, text="Save", command=self._save_trade_config).pack(side="left", padx=5)
        self.auto_trade_btn = ttk.Button(tr2, text="Enable Auto Signal", command=self._toggle_auto_trade)
        self.auto_trade_btn.pack(side="left", padx=5)

        tr3 = ttk.Frame(trade_frame)
        tr3.pack(fill="x", padx=5, pady=2)
        self.trade_status_label = ttk.Label(tr3, text="Status: OFF | Signals Sent: 0 | Min Basis: 1.0% | Min Funding Diff: 0.0%",
                                             foreground="gray")
        self.trade_status_label.pack(side="left")

        # Signal log
        self.trade_log_text = tk.Text(trade_frame, height=3, font=("Consolas", 9),
                                       wrap="word", state="disabled", bg="#fafafa")
        self.trade_log_text.pack(fill="x", padx=5, pady=(0, 5))

    # ── Async loop ──────────────────────────────────────────
    def _start_async_loop(self):
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
        threading.Thread(target=run_loop, daemon=True).start()

    def _run_async(self, coro):
        if self.loop:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None

    # ── Data fetch ──────────────────────────────────────────
    def _do_initial_fetch(self):
        future = self._run_async(fetch_all_data())
        if future:
            self.root.after(100, lambda: self._check_fetch_result(future, initial=True))

    def _check_fetch_result(self, future, initial=False):
        if not future.done():
            self.root.after(100, lambda: self._check_fetch_result(future, initial))
            return

        try:
            self.binance_data, self.aster_data, self.tickers_24h, self.bn_intervals, self.as_intervals, self.qty_precisions = future.result()
            binance_set = set(self.binance_data.keys())
            aster_set = set(self.aster_data.keys())
            self.common_symbols = sorted(binance_set & aster_set)

            self._populate_pairs_list()
            self._refresh_executable_basis_table()
            self._check_alerts()
            self._check_auto_trade()

            now = time.strftime("%H:%M:%S")
            self.status_label.config(
                text=f"Updated {now} | BN: {len(binance_set)} | AS: {len(aster_set)} | Common: {len(self.common_symbols)}"
            )
        except Exception as e:
            self.status_label.config(text=f"Error: {e}")

        if self.running:
            self.root.after(10000, self._do_refresh)

    def _do_refresh(self):
        if not self.running:
            return
        future = self._run_async(fetch_all_data())
        if future:
            self.root.after(100, lambda: self._check_fetch_result(future))

    # ── Pairs list ──────────────────────────────────────────
    def _populate_pairs_list(self):
        self.pairs_listbox.delete(0, "end")
        search = self.search_var.get().upper()
        count = 0
        for sym in self.common_symbols:
            if search and search not in sym:
                continue
            ticker = self.tickers_24h.get(sym)
            change_str = f" ({ticker['price_change_pct']:+.1f}%)" if ticker else ""
            self.pairs_listbox.insert("end", f"  {sym}{change_str}")
            count += 1
        self.count_label.config(text=f"{count} common pairs")

    def _filter_pairs(self, *args):
        self._populate_pairs_list()

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = False
        self._update_basis_table()

    # ── Basis table ─────────────────────────────────────────
    def _get_normalized_fr(self, sym):
        """Get funding rates normalized to 4h equivalent (in %)"""
        bn = self.binance_data.get(sym)
        ast = self.aster_data.get(sym)
        if not bn or not ast:
            return 0, 0, 0
        bn_interval = self.bn_intervals.get(sym, 8)
        as_interval = self.as_intervals.get(sym, 8)
        bn_fr = bn["last_funding"] * 100 * (4 / bn_interval)
        as_fr = ast["last_funding"] * 100 * (4 / as_interval)
        return bn_fr, as_fr, as_fr - bn_fr

    def _build_funding_aligned_candidates(self):
        rows = []
        for sym in self.common_symbols:
            bn = self.binance_data.get(sym)
            ast = self.aster_data.get(sym)
            if not bn or not ast:
                continue

            bn_price = bn["mark_price"]
            as_price = ast["mark_price"]
            if bn_price <= 0 or as_price <= 0:
                continue

            diff = as_price - bn_price
            diff_pct = (diff / bn_price) * 100

            # Skip outliers (basis > 10% likely means different contract/delisted)
            if abs(diff_pct) > 10:
                continue

            bn_fr, as_fr, fr_diff = self._get_normalized_fr(sym)
            if diff_pct * fr_diff <= 0:
                continue

            ticker = self.tickers_24h.get(sym)
            change_24h = ticker["price_change_pct"] if ticker else 0.0

            rows.append({
                "symbol": sym,
                "change_24h": change_24h,
                "binance_price": bn_price,
                "aster_price": as_price,
                "diff": diff,
                "diff_pct": diff_pct,
                "bn_funding": bn_fr,
                "as_funding": as_fr,
                "funding_diff": fr_diff,
            })

        rows.sort(key=lambda r: abs(r["diff_pct"]), reverse=True)
        return rows[:MAX_EXECUTABLE_BASIS_CANDIDATES]

    def _refresh_executable_basis_table(self):
        if self.executable_basis_scan_running:
            return

        candidates = self._build_funding_aligned_candidates()
        if not candidates:
            self.executable_basis_rows = []
            self._update_basis_table()
            return

        self.executable_basis_scan_running = True
        future = self._run_async(self._build_executable_basis_rows(candidates))
        if future:
            future.add_done_callback(lambda f: self.root.after(0, lambda: self._on_executable_basis_rows_ready(f)))
        else:
            self.executable_basis_scan_running = False

    async def _build_executable_basis_rows(self, candidates):
        symbols = [row["symbol"] for row in candidates]
        books_by_symbol = await fetch_order_books_for_symbols(
            symbols,
            limit=10,
            concurrency=EXECUTABLE_BASIS_CONCURRENCY,
        )

        rows = []
        for row in candidates:
            books = books_by_symbol.get(row["symbol"])
            if not books:
                continue

            bn_book, as_book = books
            exec_info = compute_executable_basis(row["diff_pct"], bn_book, as_book)
            if row["diff_pct"] * exec_info["directional_exec_basis"] <= 0:
                continue

            enriched = dict(row)
            enriched.update(exec_info)
            rows.append(enriched)

        rows.sort(key=lambda r: abs(r["exec_basis"]), reverse=True)
        return rows

    def _on_executable_basis_rows_ready(self, future):
        self.executable_basis_scan_running = False
        try:
            self.executable_basis_rows = future.result()
        except Exception as e:
            self._trade_log(f"Executable basis scan failed: {e}")
            self.executable_basis_rows = []
        self._update_basis_table()

    def _update_basis_table(self):
        for item in self.basis_tree.get_children():
            self.basis_tree.delete(item)

        rows = list(self.executable_basis_rows)

        # Sort
        if self.sort_col:
            try:
                if self.sort_col == "exec_basis":
                    rows.sort(key=lambda r: abs(r.get("exec_basis", 0)), reverse=self.sort_reverse)
                else:
                    rows.sort(key=lambda r: r.get(self.sort_col, 0), reverse=self.sort_reverse)
            except TypeError:
                pass
        else:
            rows.sort(key=lambda r: abs(r["exec_basis"]), reverse=True)

        # Show only top 10
        for row in rows[:10]:
            bp = row["binance_price"]
            if bp > 1000:
                fmt = ".2f"
            elif bp > 1:
                fmt = ".4f"
            else:
                fmt = ".6f"

            diff_pct = row["diff_pct"]
            exec_basis = row["exec_basis"]
            tag = "positive" if exec_basis > 0 else ("negative" if exec_basis < 0 else "neutral")

            values = (
                row["symbol"],
                f"{bp:{fmt}}",
                f"{row['aster_price']:{fmt}}",
                f"{diff_pct:+.4f}%",
                f"{exec_basis:+.4f}%",
                f"{row['bn_funding']:.4f}%",
                f"{row['as_funding']:.4f}%",
                f"{row['funding_diff']:+.4f}%",
            )
            self.basis_tree.insert("", "end", values=values, tags=(tag,))

    # ── Telegram ────────────────────────────────────────────
    def _load_ignored_symbols(self, cfg):
        """Load unexpired Telegram ignore rules from config."""
        raw = cfg.get(TELEGRAM_IGNORED_SYMBOLS_CONFIG_KEY, {})
        now = time.time()
        loaded = {}
        if not isinstance(raw, dict):
            return loaded

        for chat_id, symbols in raw.items():
            if not isinstance(symbols, dict):
                continue
            chat_key = str(chat_id)
            for symbol, until_ts in symbols.items():
                try:
                    until_ts = float(until_ts)
                except (TypeError, ValueError):
                    continue
                if until_ts > now:
                    loaded.setdefault(chat_key, {})[str(symbol).upper()] = until_ts
        return loaded

    def _save_ignored_symbols(self):
        cfg = load_config()
        now = time.time()
        cleaned = {}
        for chat_id, symbols in list(self.ignored_symbols_by_chat.items()):
            active_symbols = {}
            for symbol, until_ts in list(symbols.items()):
                if until_ts > now:
                    active_symbols[symbol] = until_ts
            if active_symbols:
                cleaned[str(chat_id)] = active_symbols
        self.ignored_symbols_by_chat = cleaned
        if cleaned:
            cfg[TELEGRAM_IGNORED_SYMBOLS_CONFIG_KEY] = cleaned
        else:
            cfg.pop(TELEGRAM_IGNORED_SYMBOLS_CONFIG_KEY, None)
        save_config(cfg)

    @staticmethod
    def _normalize_telegram_symbol(raw_symbol):
        symbol = str(raw_symbol or "").strip().upper()
        symbol = symbol.split()[0] if symbol else ""
        for sep in ("/", "-", "_", ":"):
            symbol = symbol.replace(sep, "")
        symbol = "".join(ch for ch in symbol if ch.isalnum())
        if not symbol:
            return None
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"
        if symbol == "USDT" or len(symbol) <= 4:
            return None
        return symbol

    @staticmethod
    def _parse_ignore_command(text):
        if not text:
            return None
        parts = text.strip().split()
        if not parts:
            return None
        command = parts[0].lower()
        if command != "/ignore" and not command.startswith("/ignore@"):
            return None
        if len(parts) < 2:
            return {"error": "Usage: /ignore BTC/USDT"}
        symbol = BasisCheckerApp._normalize_telegram_symbol(parts[1])
        if not symbol:
            return {"error": "Invalid pair. Usage: /ignore BTC/USDT"}
        return {"symbol": symbol}

    def _is_ignored_symbol(self, symbol, chat_id=None):
        symbol = self._normalize_telegram_symbol(symbol)
        if not symbol:
            return False
        chat_key = str(chat_id if chat_id is not None else self.tg_chat_id)
        symbols = self.ignored_symbols_by_chat.get(chat_key)
        if not symbols:
            return False
        until_ts = symbols.get(symbol, 0)
        if until_ts > time.time():
            return True
        if symbol in symbols:
            symbols.pop(symbol, None)
        return False

    def _ignore_symbol_for_chat(self, symbol, chat_id):
        symbol = self._normalize_telegram_symbol(symbol)
        if not symbol:
            return None, None
        chat_key = str(chat_id)
        until_ts = time.time() + TELEGRAM_IGNORE_DURATION_SECONDS
        self.ignored_symbols_by_chat.setdefault(chat_key, {})[symbol] = until_ts
        self.alert_hit_count.pop(symbol, None)
        self._save_ignored_symbols()
        return symbol, until_ts

    def _save_tg_config(self):
        self.tg_token = self.tg_token_var.get().strip()
        self.tg_chat_id = self.tg_chatid_var.get().strip()
        self.tg_enabled = self.tg_enabled_var.get()
        try:
            self.alert_threshold = float(self.threshold_var.get())
        except ValueError:
            self.alert_threshold = DEFAULT_ALERT_THRESHOLD

        cfg = load_config()
        cfg.update({
            "telegram_bot_token": self.tg_token,
            "telegram_chat_id": self.tg_chat_id,
            "telegram_enabled": self.tg_enabled,
            "alert_threshold": self.alert_threshold,
        })
        save_config(cfg)
        self.tg_status.config(text="Saved!", foreground="green")
        self._start_telegram_listener_if_ready()
        self.root.after(3000, lambda: self.tg_status.config(text=""))

    def _start_telegram_listener_if_ready(self):
        """Start Telegram command listener when bot token is configured."""
        self.tg_token = self.tg_token_var.get().strip()
        self.tg_chat_id = self.tg_chatid_var.get().strip()
        self.tg_enabled = self.tg_enabled_var.get()

        if not self.tg_enabled or not self.tg_token:
            return
        if self.telegram_listener_running:
            if self.telegram_listener_token == self.tg_token:
                self.tg_status.config(text="command listener running", foreground="green")
                return
            self._stop_telegram_listener()
        if not self.loop:
            self.root.after(500, self._start_telegram_listener_if_ready)
            return

        self.telegram_listener_running = True
        self.telegram_listener_token = self.tg_token
        self.telegram_listener_future = self._run_async(self._telegram_hello_listener())
        if self.telegram_listener_future:
            self.telegram_listener_future.add_done_callback(
                lambda future: self.root.after(0, lambda: self._on_telegram_listener_done(future))
            )
        self.tg_status.config(text="command listener started", foreground="green")

    def _on_telegram_listener_done(self, future):
        self.telegram_listener_running = False
        self.telegram_listener_token = None
        if not self.running:
            return
        if future.cancelled():
            return
        try:
            future.result()
        except Exception as e:
            self.tg_status.config(text=f"command listener error: {e}", foreground="red")

    @staticmethod
    def _is_hello_command(text):
        if not text:
            return False
        first = text.strip().split()[0].lower()
        return first == "/hello" or first.startswith("/hello@")

    async def _telegram_hello_listener(self):
        """Reply to Telegram commands in any chat/group where this bot receives them."""
        token = self.tg_token
        self._trade_log("Telegram command listener running")

        while self.running and self.telegram_listener_running:
            params = {
                "timeout": TELEGRAM_POLL_TIMEOUT_SECONDS,
                "allowed_updates": json.dumps(["message"]),
            }
            if self.telegram_update_offset is not None:
                params["offset"] = self.telegram_update_offset

            details = await telegram_api_request(
                token,
                "getUpdates",
                http_method="GET",
                params=params,
            )
            if not details.get("ok"):
                desc = details.get("description", "unknown error")
                self.root.after(0, lambda text=desc: self.tg_status.config(text=f"commands failed: {text[:70]}", foreground="red"))
                await asyncio.sleep(10)
                continue

            updates = details.get("result") or []
            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None:
                    self.telegram_update_offset = int(update_id) + 1

                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                chat_title = chat.get("title") or chat.get("username") or chat_id

                text = message.get("text") or ""
                ignore_command = self._parse_ignore_command(text)
                if ignore_command is not None:
                    error = ignore_command.get("error")
                    if error:
                        sent = await send_telegram_message(token, chat_id, error)
                        if sent:
                            self._trade_log(f"Replied /ignore usage to Telegram chat {chat_title}")
                        else:
                            self._trade_log(f"Failed to reply /ignore usage to Telegram chat {chat_title}")
                        continue

                    symbol, until_ts = self._ignore_symbol_for_chat(ignore_command["symbol"], chat_id)
                    if symbol is None:
                        await send_telegram_message(token, chat_id, "Invalid pair. Usage: /ignore BTC/USDT")
                        continue
                    expiry = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until_ts))
                    reply = (
                        f"Da ignore {symbol} trong 1 gio.\n"
                        f"Het han: {expiry}"
                    )
                    sent = await send_telegram_message(token, chat_id, reply)
                    if sent:
                        self._trade_log(f"Ignored {symbol} for Telegram chat {chat_title} until {expiry}")
                    else:
                        self._trade_log(f"Failed to reply /ignore to Telegram chat {chat_title}")
                    continue

                if not self._is_hello_command(text):
                    continue

                reply = (
                    f"{TELEGRAM_HELLO_REPLY}\n"
                    f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                sent = await send_telegram_message(token, chat_id, reply)
                if sent:
                    self._trade_log(f"Replied /hello to Telegram chat {chat_title}")
                else:
                    self._trade_log(f"Failed to reply /hello to Telegram chat {chat_title}")

    def _stop_telegram_listener(self):
        self.telegram_listener_running = False
        self.telegram_listener_token = None
        future = self.telegram_listener_future
        if future and not future.done():
            future.cancel()
        self.telegram_listener_future = None

    def _test_telegram(self):
        token = self.tg_token_var.get().strip()
        chat_id = self.tg_chatid_var.get().strip()
        if not token or not chat_id:
            self.tg_status.config(text="Fill token & chat ID", foreground="red")
            return

        self.tg_status.config(text="Sending...", foreground="gray")
        msg = "<b>Basis Checker</b>\nTest message - Alert system working!"
        future = self._run_async(send_telegram_message(token, chat_id, msg, return_details=True))
        if future:
            self.root.after(200, lambda: self._check_tg_test(future))

    def _check_tg_test(self, future):
        if not future.done():
            self.root.after(200, lambda: self._check_tg_test(future))
            return
        try:
            result = future.result()
            ok = result.get("ok") if isinstance(result, dict) else bool(result)
            if ok:
                suffix = " (SSL fallback)" if isinstance(result, dict) and result.get("ssl_fallback") else ""
                self.tg_status.config(text=f"Sent OK{suffix}!", foreground="green")
            else:
                description = result.get("description", "") if isinstance(result, dict) else ""
                error_code = result.get("error_code") if isinstance(result, dict) else None
                if error_code == 401:
                    description = "Unauthorized: bot token invalid/revoked"
                elif error_code == 400 and "chat" in description.lower():
                    description = f"{description}: bot may not be in chat/group"
                if not description:
                    description = "check token/chat_id"
                self.tg_status.config(text=f"Failed - {description[:80]}", foreground="red")
        except Exception as e:
            self.tg_status.config(text=f"Error: {e}", foreground="red")

    def _check_alerts(self):
        """Check basis diff against threshold and send Telegram alerts"""
        if not self.tg_enabled or not self.tg_token or not self.tg_chat_id:
            return
        if self.alert_scan_running:
            return

        now = time.time()
        candidates = []

        for sym in self.common_symbols:
            if self._is_ignored_symbol(sym, self.tg_chat_id):
                self.alert_hit_count.pop(sym, None)
                continue

            bn = self.binance_data.get(sym)
            ast = self.aster_data.get(sym)
            if not bn or not ast:
                continue

            bn_price = bn["mark_price"]
            as_price = ast["mark_price"]
            if bn_price <= 0:
                continue

            diff_pct = ((as_price - bn_price) / bn_price) * 100

            # Skip outliers
            if abs(diff_pct) > 10:
                continue

            if abs(diff_pct) >= self.alert_threshold:
                # Count consecutive hits - only alert on 2nd hit to filter noise
                self.alert_hit_count[sym] = self.alert_hit_count.get(sym, 0) + 1
                if self.alert_hit_count[sym] < 2:
                    continue

                # Cooldown: don't alert same symbol within 5 min
                last = self.alerted_symbols.get(sym, 0)
                if now - last < 300:
                    continue
                candidates.append((sym, diff_pct, bn_price, as_price))
            else:
                # Below threshold -> reset hit count
                self.alert_hit_count.pop(sym, None)

        if candidates:
            self.alert_scan_running = True
            future = self._run_async(self._process_alert_candidates(candidates))
            if future:
                future.add_done_callback(lambda f: self.root.after(0, lambda: self._on_alert_cycle_done(f)))
            else:
                self.alert_scan_running = False

    async def _process_alert_candidates(self, candidates):
        alerts = []
        now = time.time()

        for sym, mark_basis, bn_price, as_price in candidates:
            if self._is_ignored_symbol(sym, self.tg_chat_id):
                continue

            try:
                bn_book, as_book = await fetch_order_books(sym)
                bn_ask = bn_book["asks"][0][0] if bn_book["asks"] else 0
                as_bid = as_book["bids"][0][0] if as_book["bids"] else 0
                bn_bid = bn_book["bids"][0][0] if bn_book["bids"] else 0
                as_ask = as_book["asks"][0][0] if as_book["asks"] else 0

                if mark_basis >= 0:
                    exec_basis = ((as_bid - bn_ask) / bn_ask * 100) if bn_ask > 0 else 0
                    directional_exec_basis = exec_basis
                    direction = "Aster > Binance"
                else:
                    exec_basis = ((bn_bid - as_ask) / as_ask * 100) if as_ask > 0 else 0
                    directional_exec_basis = -exec_basis
                    direction = "Binance > Aster"

                if mark_basis * directional_exec_basis <= 0:
                    continue
                if abs(exec_basis) <= MIN_EXEC_BASIS_SIGNAL:
                    continue

                self.alerted_symbols[sym] = now
                self.alert_hit_count[sym] = 0

                ticker = self.tickers_24h.get(sym)
                change_str = f"  24h: {ticker['price_change_pct']:+.1f}%" if ticker else ""
                alerts.append(
                    f"<b>{sym}</b>  Mark basis: {mark_basis:+.4f}%\n"
                    f"  Exec basis: {exec_basis:+.4f}%\n"
                    f"  BN: {bn_price}  AS: {as_price}\n"
                    f"  {direction}{change_str}"
                )
            except Exception as e:
                self._trade_log(f"Alert check error {sym}: {e}")

        if alerts:
            header = f"<b>Basis Alert (threshold {self.alert_threshold}%)</b>\n"
            header += f"Exec basis > {MIN_EXEC_BASIS_SIGNAL:.2f}% | Time: {time.strftime('%H:%M:%S')}\n\n"
            msg = header + "\n\n".join(alerts)
            await send_telegram_message(self.tg_token, self.tg_chat_id, msg)

    def _on_alert_cycle_done(self, future):
        self.alert_scan_running = False
        try:
            future.result()
        except Exception as e:
            self._trade_log(f"Alert cycle failed: {e}")

    # ── Auto Trade ─────────────────────────────────────────────
    def _save_trade_config(self):
        try:
            self.min_basis = float(self.min_basis_var.get())
        except ValueError:
            self.min_basis = 1.0
        try:
            self.min_funding_diff = abs(float(self.min_funding_diff_var.get()))
        except ValueError:
            self.min_funding_diff = DEFAULT_MIN_FUNDING_DIFF

        cfg = load_config()
        for legacy_key in [
            "bn_api_key",
            "bn_secret",
            "as_api_key",
            "as_secret",
            "max_trade_volume",
            "per_trade_usdt",
            "trade_leverage",
        ]:
            cfg.pop(legacy_key, None)
        cfg["min_basis"] = self.min_basis
        cfg["min_funding_diff"] = self.min_funding_diff
        save_config(cfg)
        self._trade_log("Signal config saved")
        self._update_trade_status()

    def _toggle_auto_trade(self):
        if self.auto_trade_enabled:
            self.auto_trade_enabled = False
            self.auto_trade_btn.config(text="Enable Auto Signal")
            self._trade_log("Auto signal DISABLED")
            self._update_trade_status()
        else:
            if not self.tg_token_var.get().strip() or not self.tg_chatid_var.get().strip():
                messagebox.showerror("Error", "Fill Telegram bot token and chat ID first.")
                return
            try:
                self.min_basis = float(self.min_basis_var.get())
            except ValueError:
                self.min_basis = 1.0
            try:
                self.min_funding_diff = abs(float(self.min_funding_diff_var.get()))
            except ValueError:
                self.min_funding_diff = DEFAULT_MIN_FUNDING_DIFF

            self._save_tg_config()
            cfg = load_config()
            cfg["min_basis"] = self.min_basis
            cfg["min_funding_diff"] = self.min_funding_diff
            save_config(cfg)
            self.auto_trade_enabled = True
            self.auto_trade_btn.config(text="Disable Auto Signal")
            self._trade_log("Auto signal ENABLED")
            self._update_trade_status()

    def _update_trade_status(self):
        status = "ON" if self.auto_trade_enabled else "OFF"
        color = "#00aa00" if self.auto_trade_enabled else "gray"
        self.trade_status_label.config(
            text=(
                f"Status: {status} | Signals Sent: {self.auto_signal_count} | "
                f"Min Basis: {self.min_basis:.2f}% | Min Funding Diff: {self.min_funding_diff:.4f}%"
            ),
            foreground=color
        )

    def _trade_log(self, msg):
        def update():
            self.trade_log_text.config(state="normal")
            self.trade_log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.trade_log_text.see("end")
            self.trade_log_text.config(state="disabled")
        if threading.current_thread() is threading.main_thread():
            update()
        else:
            self.root.after(0, update)

    def _check_auto_trade(self):
        """Check and send Telegram signals if conditions are met."""
        if not self.auto_trade_enabled:
            return
        if self.auto_signal_scan_running:
            return

        candidates = []
        for sym in self.common_symbols:
            if self._is_ignored_symbol(sym, self.tg_chat_id):
                continue

            bn = self.binance_data.get(sym)
            ast = self.aster_data.get(sym)
            if not bn or not ast:
                continue

            bn_price = bn["mark_price"]
            as_price = ast["mark_price"]
            if bn_price <= 0 or as_price <= 0:
                continue

            mark_basis = ((as_price - bn_price) / bn_price) * 100
            if abs(mark_basis) > 10:
                continue

            _, _, fr_diff = self._get_normalized_fr(sym)
            funding_aligned = mark_basis * fr_diff > 0

            if not funding_aligned:
                continue
            if abs(fr_diff) < self.min_funding_diff:
                continue

            if abs(mark_basis) < self.min_basis:
                continue

            candidates.append((sym, mark_basis, fr_diff))

        if not candidates:
            return

        candidates.sort(key=lambda x: abs(x[1]), reverse=True)
        self.auto_signal_scan_running = True
        future = self._run_async(self._process_auto_trade_candidates(candidates))
        if future:
            future.add_done_callback(lambda f: self.root.after(0, lambda: self._on_auto_signal_cycle_done(f)))

    async def _process_auto_trade_candidates(self, candidates):
        """Fetch order books and send signal if executable basis is good enough."""
        signals_sent = 0
        for sym, mark_basis, fr_diff in candidates:
            if signals_sent >= MAX_AUTO_SIGNALS_PER_SCAN:
                break
            if self._is_ignored_symbol(sym, self.tg_chat_id):
                continue

            try:
                bn_book, as_book = await fetch_order_books(sym)

                bn_ask = bn_book["asks"][0][0] if bn_book["asks"] else 0
                as_bid = as_book["bids"][0][0] if as_book["bids"] else 0
                bn_bid = bn_book["bids"][0][0] if bn_book["bids"] else 0
                as_ask = as_book["asks"][0][0] if as_book["asks"] else 0

                if mark_basis >= 0:
                    exec_basis = ((as_bid - bn_ask) / bn_ask * 100) if bn_ask > 0 else 0
                    directional_exec_basis = exec_basis
                    rec_long, rec_short = "binance", "aster"
                    ref_price = (bn_ask + as_bid) / 2
                else:
                    exec_basis = ((bn_bid - as_ask) / as_ask * 100) if as_ask > 0 else 0
                    directional_exec_basis = -exec_basis
                    rec_long, rec_short = "aster", "binance"
                    ref_price = (as_ask + bn_bid) / 2

                if mark_basis * directional_exec_basis <= 0:
                    continue

                if abs(exec_basis) <= MIN_EXEC_BASIS_SIGNAL:
                    continue

                sent = await self._send_auto_trade_signal(
                    symbol=sym,
                    rec_long=rec_long,
                    rec_short=rec_short,
                    mark_basis=mark_basis,
                    exec_basis=exec_basis,
                    fr_diff=fr_diff,
                    reference_price=ref_price,
                )
                if sent:
                    signals_sent += 1

            except Exception as e:
                self._trade_log(f"Error checking {sym}: {e}")
        return signals_sent

    async def _send_auto_trade_signal(self, symbol, rec_long, rec_short, mark_basis, exec_basis, fr_diff, reference_price):
        """Send a signal-only Telegram alert for a hedged basis opportunity."""
        now = time.time()
        if self._is_ignored_symbol(symbol, self.tg_chat_id):
            self._trade_log(f"Telegram ignored for {symbol}; skip auto signal")
            return False

        signal_key = (symbol, rec_long, rec_short)
        last_sent = self.auto_signal_last_sent.get(signal_key, 0)
        if now - last_sent < AUTO_SIGNAL_COOLDOWN_SECONDS:
            return False

        venue_bias = "AsterDEX richer than Binance" if mark_basis >= 0 else "Binance richer than AsterDEX"
        funding_note = (
            "Funding supports the basis direction and passes the minimum funding diff filter."
        )

        self._trade_log(
            f"Signal {symbol}: LONG {rec_long.upper()} / SHORT {rec_short.upper()} | "
            f"mark_basis={mark_basis:+.4f}% | exec_basis={exec_basis:+.4f}% | fr_diff={fr_diff:+.4f}%"
        )

        msg = (
            "<b>Basis + Funding Opportunity</b>\n"
            f"<b>{symbol}</b>\n"
            f"Long: <b>{rec_long.upper()}</b>\n"
            f"Short: <b>{rec_short.upper()}</b>\n"
            f"Mark basis: {mark_basis:+.4f}%\n"
            f"Executable basis: {exec_basis:+.4f}%\n"
            f"Min executable basis: {MIN_EXEC_BASIS_SIGNAL:.2f}%\n"
            f"Funding diff (4h): {fr_diff:+.4f}%\n"
            f"Min funding diff (4h): {self.min_funding_diff:.4f}%\n"
            f"Reference price: {reference_price:.6f}\n"
            f"Min basis: {self.min_basis:.2f}%\n"
            f"Bias: {venue_bias}\n"
            f"{funding_note}"
        )

        ok = await send_telegram_message(self.tg_token, self.tg_chat_id, msg)
        if not ok:
            self._trade_log(f"Telegram FAILED for {symbol}; run Telegram Test for details")
            return False

        self.auto_signal_last_sent[signal_key] = now
        self.auto_signal_count += 1
        self._trade_log(f"Telegram sent for {symbol}")
        return True

    def _on_auto_signal_cycle_done(self, future):
        self.auto_signal_scan_running = False
        try:
            future.result()
        except Exception as e:
            self._trade_log(f"Auto signal cycle failed: {e}")
        self._update_trade_status()

    # ── Auto Close ─────────────────────────────────────────────
    # ── Order Book Detail ─────────────────────────────────────
    def _on_basis_double_click(self, event):
        sel = self.basis_tree.selection()
        if not sel:
            return
        values = self.basis_tree.item(sel[0], "values")
        if values:
            self._show_order_book(values[0])

    def _show_order_book(self, symbol):
        """Show order book detail window for a symbol"""
        if hasattr(self, '_ob_window') and self._ob_window and self._ob_window.winfo_exists():
            self._ob_window.destroy()

        win = tk.Toplevel(self.root)
        win.title(f"{symbol} - Bid/Ask Analysis")
        win.geometry("950x700")
        win.minsize(850, 550)
        self._ob_window = win
        self._ob_symbol = symbol
        self._ob_running = True

        # Top
        top = ttk.Frame(win)
        top.pack(fill="x", padx=5, pady=5)
        ttk.Label(top, text=symbol, font=("Segoe UI", 14, "bold")).pack(side="left")
        self._ob_status = ttk.Label(top, text="Loading...", foreground="gray")
        self._ob_status.pack(side="right")

        # Order books side by side
        books_frame = ttk.Frame(win)
        books_frame.pack(fill="both", expand=True, padx=5, pady=2)

        ob_cols = ("price", "quantity", "total_usd")

        # Binance order book
        bn_frame = ttk.LabelFrame(books_frame, text="Binance")
        bn_frame.pack(side="left", fill="both", expand=True, padx=(0, 3))
        self._bn_ob_tree = ttk.Treeview(bn_frame, columns=ob_cols, show="headings", height=22)
        for col, title, w in [("price", "Price", 130), ("quantity", "Qty", 110), ("total_usd", "Total $", 100)]:
            self._bn_ob_tree.heading(col, text=title)
            self._bn_ob_tree.column(col, width=w, anchor="e")
        self._bn_ob_tree.pack(fill="both", expand=True)
        self._bn_ob_tree.tag_configure("ask", foreground="#cc0000")
        self._bn_ob_tree.tag_configure("bid", foreground="#00aa00")
        self._bn_ob_tree.tag_configure("spread", foreground="#888888", background="#f0f0f0")

        # AsterDEX order book
        as_frame = ttk.LabelFrame(books_frame, text="AsterDEX")
        as_frame.pack(side="left", fill="both", expand=True, padx=(3, 0))
        self._as_ob_tree = ttk.Treeview(as_frame, columns=ob_cols, show="headings", height=22)
        for col, title, w in [("price", "Price", 130), ("quantity", "Qty", 110), ("total_usd", "Total $", 100)]:
            self._as_ob_tree.heading(col, text=title)
            self._as_ob_tree.column(col, width=w, anchor="e")
        self._as_ob_tree.pack(fill="both", expand=True)
        self._as_ob_tree.tag_configure("ask", foreground="#cc0000")
        self._as_ob_tree.tag_configure("bid", foreground="#00aa00")
        self._as_ob_tree.tag_configure("spread", foreground="#888888", background="#f0f0f0")

        # Analysis
        analysis_frame = ttk.LabelFrame(win, text="Analysis & Recommendation")
        analysis_frame.pack(fill="x", padx=5, pady=(2, 5))
        self._analysis_text = tk.Text(analysis_frame, height=7, font=("Consolas", 10),
                                       wrap="word", state="disabled", bg="#fafafa")
        self._analysis_text.pack(fill="x", padx=5, pady=5)
        self._analysis_text.tag_configure("green", foreground="#00aa00")
        self._analysis_text.tag_configure("red", foreground="#cc0000")
        self._analysis_text.tag_configure("bold", font=("Consolas", 10, "bold"))
        self._analysis_text.tag_configure("header", font=("Consolas", 11, "bold"))

        win.protocol("WM_DELETE_WINDOW", self._close_ob_window)
        self._fetch_order_book()

    def _close_ob_window(self):
        self._ob_running = False
        if hasattr(self, '_ob_window') and self._ob_window:
            self._ob_window.destroy()

    def _fetch_order_book(self):
        if not self._ob_running:
            return
        if not hasattr(self, '_ob_window') or not self._ob_window.winfo_exists():
            self._ob_running = False
            return
        future = self._run_async(fetch_order_books(self._ob_symbol))
        if future:
            self._ob_window.after(100, lambda: self._check_ob_result(future))

    def _check_ob_result(self, future):
        if not self._ob_running:
            return
        if not hasattr(self, '_ob_window') or not self._ob_window.winfo_exists():
            self._ob_running = False
            return
        if not future.done():
            self._ob_window.after(100, lambda: self._check_ob_result(future))
            return
        try:
            bn_book, as_book = future.result()
            self._update_ob_display(bn_book, as_book)
            self._ob_status.config(text=f"Updated {time.strftime('%H:%M:%S')} (auto-refresh 3s)")
        except Exception as e:
            self._ob_status.config(text=f"Error: {e}")
        if self._ob_running:
            self._ob_window.after(3000, self._fetch_order_book)

    def _update_ob_display(self, bn_book, as_book):
        def populate(tree, book):
            tree.delete(*tree.get_children())

            # Asks reversed (highest at top, best ask at bottom near spread)
            asks = sorted(book["asks"][:10], key=lambda x: x[0], reverse=True)
            for price, qty in asks:
                fmt = _price_fmt(price)
                tree.insert("", "end", values=(
                    f"{price:{fmt}}", _qty_fmt(qty), f"${price * qty:,.1f}"
                ), tags=("ask",))

            # Spread line
            if book["asks"] and book["bids"]:
                best_ask = book["asks"][0][0]
                best_bid = book["bids"][0][0]
                spread_pct = ((best_ask - best_bid) / best_bid * 100) if best_bid > 0 else 0
                tree.insert("", "end", values=(
                    f"--- Spread: {spread_pct:.4f}% ---", "", ""
                ), tags=("spread",))

            # Bids (best bid at top, near spread)
            for price, qty in book["bids"][:10]:
                fmt = _price_fmt(price)
                tree.insert("", "end", values=(
                    f"{price:{fmt}}", _qty_fmt(qty), f"${price * qty:,.1f}"
                ), tags=("bid",))

        populate(self._bn_ob_tree, bn_book)
        populate(self._as_ob_tree, as_book)
        self._update_analysis(bn_book, as_book)

    def _update_analysis(self, bn_book, as_book):
        symbol = self._ob_symbol
        t = self._analysis_text
        t.config(state="normal")
        t.delete("1.0", "end")

        bn_bid = bn_book["bids"][0][0] if bn_book["bids"] else 0
        bn_ask = bn_book["asks"][0][0] if bn_book["asks"] else 0
        as_bid = as_book["bids"][0][0] if as_book["bids"] else 0
        as_ask = as_book["asks"][0][0] if as_book["asks"] else 0

        # Mark price basis
        bn_data = self.binance_data.get(symbol, {})
        as_data = self.aster_data.get(symbol, {})
        bn_mark = bn_data.get("mark_price", 0)
        as_mark = as_data.get("mark_price", 0)
        mark_basis = ((as_mark - bn_mark) / bn_mark * 100) if bn_mark > 0 else 0

        # Funding (normalized to 4h)
        bn_fr, as_fr, fr_diff = self._get_normalized_fr(symbol)

        # Individual spreads
        bn_spread = ((bn_ask - bn_bid) / bn_bid * 100) if bn_bid > 0 else 0
        as_spread = ((as_ask - as_bid) / as_bid * 100) if as_bid > 0 else 0

        # Depth (total USD on each side)
        bn_bid_depth = sum(p * q for p, q in bn_book["bids"][:10])
        bn_ask_depth = sum(p * q for p, q in bn_book["asks"][:10])
        as_bid_depth = sum(p * q for p, q in as_book["bids"][:10])
        as_ask_depth = sum(p * q for p, q in as_book["asks"][:10])

        t.insert("end", f"Mark Basis: {mark_basis:+.4f}%", "header")
        t.insert("end", f"  |  BN Spread: {bn_spread:.4f}%  |  AS Spread: {as_spread:.4f}%\n")
        t.insert("end", f"Depth (10 lvl):  BN ${bn_bid_depth:,.0f} bid / ${bn_ask_depth:,.0f} ask")
        t.insert("end", f"  |  AS ${as_bid_depth:,.0f} bid / ${as_ask_depth:,.0f} ask\n")

        # Executable basis
        if mark_basis >= 0:
            # Aster > Binance -> Long BN (buy at ask), Short AS (sell at bid)
            exec_basis = ((as_bid - bn_ask) / bn_ask * 100) if bn_ask > 0 else 0
            rec_long, rec_short = "Binance", "AsterDEX"
            fmt = _price_fmt(bn_ask)
            t.insert("end", f"Executable Basis: {exec_basis:+.4f}%", "bold")
            t.insert("end", f"  (AS Bid {as_bid:{fmt}} - BN Ask {bn_ask:{fmt}})\n")
        else:
            # Binance > Aster -> Long AS (buy at ask), Short BN (sell at bid)
            exec_basis = ((bn_bid - as_ask) / as_ask * 100) if as_ask > 0 else 0
            rec_long, rec_short = "AsterDEX", "Binance"
            fmt = _price_fmt(as_ask)
            t.insert("end", f"Executable Basis: {exec_basis:+.4f}%", "bold")
            t.insert("end", f"  (BN Bid {bn_bid:{fmt}} - AS Ask {as_ask:{fmt}})\n")

        # Recommendation
        t.insert("end", "Recommendation: ", "bold")
        t.insert("end", f"LONG {rec_long}", "green")
        t.insert("end", " / ")
        t.insert("end", f"SHORT {rec_short}\n", "red")

        # Executable check
        if exec_basis > 0:
            t.insert("end", "  OK  Executable basis positive - opportunity is REAL\n", "green")
        else:
            t.insert("end", f"  X  Executable basis negative ({exec_basis:+.4f}%) - spread eats profit\n", "red")

        # Funding alignment (4h normalized)
        if mark_basis * fr_diff > 0:
            t.insert("end", f"  OK  Funding/4h ({fr_diff:+.4f}%) supports basis - ALIGNED", "green")
        elif mark_basis * fr_diff < 0:
            t.insert("end", f"  X  Funding/4h ({fr_diff:+.4f}%) opposes basis", "red")
        else:
            t.insert("end", f"  ~  Funding diff is zero or basis is zero")

        t.config(state="disabled")

    # ── Run / Close ─────────────────────────────────────────
    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        self.running = False
        self.auto_trade_enabled = False
        self._stop_telegram_listener()
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()


if __name__ == "__main__":
    app = BasisCheckerApp()
    app.run()
