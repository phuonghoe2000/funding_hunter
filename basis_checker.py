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

# Public API endpoints (no auth needed)
BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
ASTER_PREMIUM_URL = "https://fapi.asterdex.com/fapi/v1/premiumIndex"
BINANCE_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
ASTER_EXCHANGE_INFO_URL = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"

# Config file for Telegram settings
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "basis_config.json")

# Default alert threshold
DEFAULT_ALERT_THRESHOLD = 0.3  # 0.3% basis diff triggers alert


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
    """Fetch only TRADING status symbols from exchangeInfo"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            return {
                s["symbol"] for s in data.get("symbols", [])
                if s.get("status") == "TRADING" and s["symbol"].endswith("USDT")
            }
    except Exception as e:
        print(f"Error fetching exchangeInfo {url}: {e}")
        return set()


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


async def fetch_all_data():
    """Fetch prices + 24h tickers, filtered to TRADING pairs only"""
    async with aiohttp.ClientSession() as session:
        # Fetch exchange info first to get TRADING symbols
        bn_trading, as_trading = await asyncio.gather(
            fetch_trading_symbols(session, BINANCE_EXCHANGE_INFO_URL),
            fetch_trading_symbols(session, ASTER_EXCHANGE_INFO_URL),
        )

        # Then fetch prices filtered by trading status
        binance_data, aster_data, tickers = await asyncio.gather(
            fetch_all_mark_prices(session, BINANCE_PREMIUM_URL, bn_trading),
            fetch_all_mark_prices(session, ASTER_PREMIUM_URL, as_trading),
            fetch_24h_tickers(session),
        )
    return binance_data, aster_data, tickers


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
        self.common_symbols = []
        self.loop = None
        self.running = True
        self.sort_col = None
        self.sort_reverse = False

        # Telegram state
        cfg = load_config()
        self.tg_token = cfg.get("telegram_bot_token", "")
        self.tg_chat_id = cfg.get("telegram_chat_id", "")
        self.tg_enabled = cfg.get("telegram_enabled", False)
        self.alert_threshold = cfg.get("alert_threshold", DEFAULT_ALERT_THRESHOLD)
        self.alerted_symbols = {}  # symbol -> last_alert_time (cooldown 5min)
        self.alert_hit_count = {}  # symbol -> consecutive hit count (alert on 2nd hit)

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

        cols = ("symbol", "change_24h", "binance_price", "aster_price", "diff", "diff_pct",
                "bn_funding", "as_funding", "funding_diff")
        self.basis_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=20)

        headers = {
            "symbol": ("Symbol", 100),
            "change_24h": ("24h %", 80),
            "binance_price": ("Binance Price", 120),
            "aster_price": ("Aster Price", 120),
            "diff": ("Diff (USD)", 100),
            "diff_pct": ("Diff %", 80),
            "bn_funding": ("BN FR", 90),
            "as_funding": ("AS FR", 90),
            "funding_diff": ("FR Diff", 90),
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
            self.binance_data, self.aster_data, self.tickers_24h = future.result()
            binance_set = set(self.binance_data.keys())
            aster_set = set(self.aster_data.keys())
            self.common_symbols = sorted(binance_set & aster_set)

            self._populate_pairs_list()
            self._update_basis_table()
            self._check_alerts()

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
    def _update_basis_table(self):
        for item in self.basis_tree.get_children():
            self.basis_tree.delete(item)

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
            bn_fr = bn["last_funding"] * 100
            as_fr = ast["last_funding"] * 100
            fr_diff = as_fr - bn_fr

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

        # Sort
        if self.sort_col:
            try:
                rows.sort(key=lambda r: r.get(self.sort_col, 0), reverse=self.sort_reverse)
            except TypeError:
                pass
        else:
            rows.sort(key=lambda r: abs(r["diff_pct"]), reverse=True)

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
            if abs(diff_pct) > 0.1:
                tag = "negative" if diff_pct < 0 else "positive"
            else:
                tag = "neutral"

            values = (
                row["symbol"],
                f"{row['change_24h']:+.2f}%",
                f"{bp:{fmt}}",
                f"{row['aster_price']:{fmt}}",
                f"{row['diff']:{fmt}}",
                f"{diff_pct:+.4f}%",
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

        save_config({
            "telegram_bot_token": self.tg_token,
            "telegram_chat_id": self.tg_chat_id,
            "telegram_enabled": self.tg_enabled,
            "alert_threshold": self.alert_threshold,
        })
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

        now = time.time()
        alerts = []

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
                self.alerted_symbols[sym] = now
                self.alert_hit_count[sym] = 0  # Reset after alert

                direction = "Aster > Binance" if diff_pct > 0 else "Binance > Aster"
                ticker = self.tickers_24h.get(sym)
                change_str = f"  24h: {ticker['price_change_pct']:+.1f}%" if ticker else ""

                alerts.append(
                    f"<b>{sym}</b>  Basis: {diff_pct:+.4f}%\n"
                    f"  BN: {bn_price}  AS: {as_price}\n"
                    f"  {direction}{change_str}"
                )
            else:
                # Below threshold -> reset hit count
                self.alert_hit_count.pop(sym, None)

        if alerts:
            header = f"<b>Basis Alert (threshold {self.alert_threshold}%)</b>\n"
            header += f"Time: {time.strftime('%H:%M:%S')}\n\n"
            msg = header + "\n\n".join(alerts)
            self._run_async(send_telegram_message(self.tg_token, self.tg_chat_id, msg))

    # ── Run / Close ─────────────────────────────────────────
    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()


if __name__ == "__main__":
    app = BasisCheckerApp()
    app.run()
