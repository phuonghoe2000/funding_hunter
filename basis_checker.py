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
import aiohttp
import time
import json
import os
import logging

logger = logging.getLogger(__name__)

# Public API endpoints (no auth needed)
BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
ASTER_PREMIUM_URL = "https://fapi.asterdex.com/fapi/v1/premiumIndex"
BINANCE_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
ASTER_EXCHANGE_INFO_URL = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"
BINANCE_DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"
ASTER_DEPTH_URL = "https://fapi.asterdex.com/fapi/v1/depth"
BINANCE_FUNDING_INFO_URL = "https://fapi.binance.com/fapi/v1/fundingInfo"
ASTER_FUNDING_INFO_URL = "https://fapi.asterdex.com/fapi/v1/fundingInfo"

# Config file for Telegram settings
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "basis_config.json")

# Default alert threshold
DEFAULT_ALERT_THRESHOLD = 0.3  # 0.3% basis diff triggers alert

# Auto signal requires funding support with at least this configurable minimum diff
DEFAULT_MIN_FUNDING_DIFF = 0.0
MIN_EXEC_BASIS_SIGNAL = 0.5
AUTO_SIGNAL_COOLDOWN_SECONDS = 300
MAX_AUTO_SIGNALS_PER_SCAN = 3
MAX_EXECUTABLE_BASIS_CANDIDATES = 60
EXECUTABLE_BASIS_CONCURRENCY = 8


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
    """Fetch only TRADING status symbols from exchangeInfo, with quantity precision"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            symbols = set()
            precisions = {}
            for s in data.get("symbols", []):
                sym = s.get("symbol", "")
                if s.get("status") == "TRADING" and sym.endswith("USDT"):
                    symbols.add(sym)
                    precisions[sym] = s.get("quantityPrecision", 0)
            return symbols, precisions
    except Exception as e:
        print(f"Error fetching exchangeInfo {url}: {e}")
        return set(), {}


async def fetch_all_mark_prices(session, url, trading_only=None):
    """Fetch all mark prices from an exchange, filtered to TRADING pairs only"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            result = {}
            for item in data:
                sym = item.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                if trading_only is not None and sym not in trading_only:
                    continue
                try:
                    result[sym] = {
                        "mark_price": float(item.get("markPrice", 0)),
                        "index_price": float(item.get("indexPrice", 0)),
                        "last_funding": float(item.get("lastFundingRate", 0)),
                    }
                except (ValueError, TypeError):
                    pass
            return result
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return {}


async def fetch_24h_tickers(session):
    """Fetch 24h price change from Binance"""
    try:
        async with session.get(BINANCE_TICKER_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            result = {}
            for item in data:
                sym = item.get("symbol", "")
                if sym.endswith("USDT"):
                    try:
                        result[sym] = {
                            "price_change_pct": float(item.get("priceChangePercent", 0)),
                            "volume": float(item.get("quoteVolume", 0)),
                        }
                    except (ValueError, TypeError):
                        pass
            return result
    except Exception as e:
        print(f"Error fetching 24h tickers: {e}")
        return {}


async def fetch_funding_intervals(session, url):
    """Fetch fundingIntervalHours for each symbol"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            result = {}
            for item in data:
                sym = item.get("symbol", "")
                if sym.endswith("USDT"):
                    result[sym] = item.get("fundingIntervalHours", 8)
            return result
    except Exception as e:
        print(f"Error fetching fundingInfo {url}: {e}")
        return {}


async def fetch_all_data():
    """Fetch prices + 24h tickers + funding intervals, filtered to TRADING pairs only"""
    async with aiohttp.ClientSession() as session:
        # Fetch exchange info first to get TRADING symbols + precision
        (bn_trading, bn_precisions), (as_trading, as_precisions) = await asyncio.gather(
            fetch_trading_symbols(session, BINANCE_EXCHANGE_INFO_URL),
            fetch_trading_symbols(session, ASTER_EXCHANGE_INFO_URL),
        )

        # Then fetch prices + funding intervals filtered by trading status
        binance_data, aster_data, tickers, bn_intervals, as_intervals = await asyncio.gather(
            fetch_all_mark_prices(session, BINANCE_PREMIUM_URL, bn_trading),
            fetch_all_mark_prices(session, ASTER_PREMIUM_URL, as_trading),
            fetch_24h_tickers(session),
            fetch_funding_intervals(session, BINANCE_FUNDING_INFO_URL),
            fetch_funding_intervals(session, ASTER_FUNDING_INFO_URL),
        )
    # Merge precisions (use min of both exchanges for safety)
    qty_precisions = {}
    for sym in set(bn_precisions) | set(as_precisions):
        bn_p = bn_precisions.get(sym, 0)
        as_p = as_precisions.get(sym, 0)
        qty_precisions[sym] = min(bn_p, as_p) if bn_p and as_p else bn_p or as_p
    return binance_data, aster_data, tickers, bn_intervals, as_intervals, qty_precisions


def _price_fmt(price):
    """Get decimal format string based on price magnitude"""
    if price > 1000:
        return ".2f"
    elif price > 1:
        return ".4f"
    elif price > 0.01:
        return ".6f"
    else:
        return ".8f"


def _qty_fmt(qty):
    """Format quantity for display"""
    if qty >= 10000:
        return f"{qty:,.0f}"
    elif qty >= 100:
        return f"{qty:,.1f}"
    elif qty >= 1:
        return f"{qty:.2f}"
    else:
        return f"{qty:.4f}"


async def fetch_order_books(symbol, limit=10):
    """Fetch order books from both Binance and AsterDEX"""
    async with aiohttp.ClientSession() as session:
        async def fetch_one(base_url):
            url = f"{base_url}?symbol={symbol}&limit={limit}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    return {
                        "bids": [(float(p), float(q)) for p, q in data.get("bids", [])],
                        "asks": [(float(p), float(q)) for p, q in data.get("asks", [])],
                    }
            except Exception as e:
                print(f"Error fetching depth from {base_url}: {e}")
                return {"bids": [], "asks": []}

        bn_book, as_book = await asyncio.gather(
            fetch_one(BINANCE_DEPTH_URL),
            fetch_one(ASTER_DEPTH_URL)
        )
    return bn_book, as_book


async def fetch_order_books_for_symbols(symbols, limit=10, concurrency=8):
    """Fetch Binance/Aster order books for many symbols with bounded concurrency."""
    async with aiohttp.ClientSession() as session:
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_one(symbol, base_url):
            url = f"{base_url}?symbol={symbol}&limit={limit}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    return {
                        "bids": [(float(p), float(q)) for p, q in data.get("bids", [])],
                        "asks": [(float(p), float(q)) for p, q in data.get("asks", [])],
                    }
            except Exception as e:
                print(f"Error fetching depth from {base_url} for {symbol}: {e}")
                return {"bids": [], "asks": []}

        async def fetch_pair(symbol):
            async with semaphore:
                bn_book, as_book = await asyncio.gather(
                    fetch_one(symbol, BINANCE_DEPTH_URL),
                    fetch_one(symbol, ASTER_DEPTH_URL),
                )
                return symbol, bn_book, as_book

        results = await asyncio.gather(*(fetch_pair(symbol) for symbol in symbols), return_exceptions=True)

    books = {}
    for result in results:
        if isinstance(result, Exception):
            continue
        symbol, bn_book, as_book = result
        books[symbol] = (bn_book, as_book)
    return books


def compute_executable_basis(mark_basis, bn_book, as_book):
    """Return executable basis details for the direction implied by mark basis."""
    bn_ask = bn_book["asks"][0][0] if bn_book.get("asks") else 0
    as_bid = as_book["bids"][0][0] if as_book.get("bids") else 0
    bn_bid = bn_book["bids"][0][0] if bn_book.get("bids") else 0
    as_ask = as_book["asks"][0][0] if as_book.get("asks") else 0

    if mark_basis >= 0:
        exec_basis = ((as_bid - bn_ask) / bn_ask * 100) if bn_ask > 0 else 0
        directional_exec_basis = exec_basis
        rec_long, rec_short = "binance", "aster"
        reference_price = (bn_ask + as_bid) / 2 if bn_ask > 0 and as_bid > 0 else 0
    else:
        exec_basis = ((bn_bid - as_ask) / as_ask * 100) if as_ask > 0 else 0
        directional_exec_basis = -exec_basis
        rec_long, rec_short = "aster", "binance"
        reference_price = (as_ask + bn_bid) / 2 if as_ask > 0 and bn_bid > 0 else 0

    return {
        "exec_basis": exec_basis,
        "directional_exec_basis": directional_exec_basis,
        "rec_long": rec_long,
        "rec_short": rec_short,
        "reference_price": reference_price,
    }


async def send_telegram_message(bot_token, chat_id, message):
    """Send message via Telegram bot"""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                result = await resp.json()
                return result.get("ok", False)
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False


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

        # Auto signal state
        self.min_basis = cfg.get("min_basis", 1.0)
        self.min_funding_diff = cfg.get("min_funding_diff", DEFAULT_MIN_FUNDING_DIFF)
        self.auto_trade_enabled = False
        self.auto_signal_count = 0
        self.auto_signal_last_sent = {}  # (symbol, long, short) -> last_sent_time
        self.auto_signal_scan_running = False

        self._build_ui()
        self._start_async_loop()
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
        self.root.after(3000, lambda: self.tg_status.config(text=""))

    def _test_telegram(self):
        token = self.tg_token_var.get().strip()
        chat_id = self.tg_chatid_var.get().strip()
        if not token or not chat_id:
            self.tg_status.config(text="Fill token & chat ID", foreground="red")
            return

        self.tg_status.config(text="Sending...", foreground="gray")
        msg = "<b>Basis Checker</b>\nTest message - Alert system working!"
        future = self._run_async(send_telegram_message(token, chat_id, msg))
        if future:
            self.root.after(200, lambda: self._check_tg_test(future))

    def _check_tg_test(self, future):
        if not future.done():
            self.root.after(200, lambda: self._check_tg_test(future))
            return
        try:
            ok = future.result()
            if ok:
                self.tg_status.config(text="Sent OK!", foreground="green")
            else:
                self.tg_status.config(text="Failed - check token/chat_id", foreground="red")
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
            self._trade_log(f"Telegram FAILED for {symbol}")
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
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()


if __name__ == "__main__":
    app = BasisCheckerApp()
    app.run()
