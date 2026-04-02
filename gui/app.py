"""
Main GUI Application for Funding Hunter - Multi Exchange Support
Supports OKX, Binance, and BingX
"""
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import logging
import json
import os
import time
import subprocess
import aiohttp

from config.settings import settings
from core.gui_service import GUIWorkflowService
from core.opportunity import build_best_opportunity, normalize_funding_rate_obj

try:
    from gui.exchange_display import from_display_name, parse_recommendation_display_names
    from gui.layout import create_widgets as build_widgets
    from gui.position_loader import (
        find_hedged_pairs as build_hedged_pairs,
        load_hedged_position as load_existing_hedged_position,
        show_position_selector as show_existing_position_selector,
    )
except ImportError:
    from exchange_display import from_display_name, parse_recommendation_display_names
    from layout import create_widgets as build_widgets
    from position_loader import (
        find_hedged_pairs as build_hedged_pairs,
        load_hedged_position as load_existing_hedged_position,
        show_position_selector as show_existing_position_selector,
    )


def sync_windows_time() -> tuple[bool, str]:
    """
    Sync Windows system clock with internet time server.
    Requires admin privileges to actually update the clock.
    Returns (success, message).
    """
    if os.name != 'nt':
        return False, "⚠️ Không phải Windows, bỏ qua sync time"
    
    try:
        # Step 1: Start Windows Time service if not running
        start_result = subprocess.run(
            ["net", "start", "w32time"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        
        # Step 2: Configure time server (use pool.ntp.org)
        subprocess.run(
            ["w32tm", "/config", "/manualpeerlist:pool.ntp.org", "/syncfromflags:manual", "/update"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        
        # Step 3: Force resync
        result = subprocess.run(
            ["w32tm", "/resync", "/force"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        
        if result.returncode == 0:
            return True, "✅ Đã sync time Windows thành công"
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            if "0x80070426" in error_msg:
                return False, "❌ Windows Time service chưa start - cần quyền Admin"
            elif "access" in error_msg.lower() or "denied" in error_msg.lower():
                return False, "❌ Cần chạy app với quyền Admin để sync time"
            else:
                return False, f"⚠️ Sync time: {error_msg[:50]}"
                
    except subprocess.TimeoutExpired:
        return False, "❌ Timeout khi sync time"
    except FileNotFoundError:
        return False, "❌ Không tìm thấy w32tm"
    except Exception as e:
        return False, f"❌ Lỗi sync time: {e}"

from config.constants import Exchange, get_exchange_symbol, calculate_break_even, calculate_trade_quality, assess_price_divergence
logger = logging.getLogger(__name__)

# Config file path
CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "user_config.json")




class FundingHunterGUI:
    """Main GUI Application with Multi-Exchange Support"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🎯 Funding Hunter - Multi Exchange Arbitrage")
        self.root.geometry("1400x900")
        self.root.minsize(1200, 800)
        
        # Style
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure('Title.TLabel', font=('Helvetica', 14, 'bold'))
        self.style.configure('Header.TLabel', font=('Helvetica', 11, 'bold'))
        self.style.configure('Success.TLabel', foreground='green')
        self.style.configure('Error.TLabel', foreground='red')
        
        # Services / Manager
        self.service = GUIWorkflowService(settings)
        self.manager = self.service.manager
        
        # Async
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.async_thread: Optional[threading.Thread] = None
        
        # State
        self.connected = False
        self.monitoring = False
        self.active_position = None  # Store active position info
        self.balance_before_open = {}  # Balance of 2 exchanges before opening position
        self._monitor_task = None
        self.cached_funding_rates = {}  # Cache funding rates for quick access
        
        # Price spread waiting state (for manual open position)
        self.waiting_for_price_spread = False
        self.price_spread_check_task = None
        self.price_spread_params = None  # Store params: pair, long_ex, short_ex, size, leverage
        self.split_cancel_event: Optional[threading.Event] = None  # For cancelling splits mid-execution (thread-safe)
        
        # Analyze spread state
        self.analyzing_spread = False
        self.analyze_cancel_event: Optional[threading.Event] = None
        
        # UI update tasks (to prevent spam and manage recurring updates)
        self._pair_info_update_task = None
        self._usdt_update_pending = None
        
        # Build UI
        self._create_menu()
        self._create_widgets()
        self._setup_logging()
        self._start_async_loop()
        
        # Load saved config
        self._load_config()
        
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
    
    def _create_menu(self):
        """Create menu bar"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="💾 Save Config", command=self._save_config)
        file_menu.add_command(label="📂 Load Config", command=self._load_config)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_closing)
        
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=self._show_about)
    
    def _save_config(self):
        """Save current config to file"""
        try:
            config = {
                "okx": {
                    "api_key": self.okx_api_key.get(),
                    "secret": self.okx_secret.get(),
                    "passphrase": self.okx_passphrase.get(),
                    "testnet": self.okx_testnet.get(),
                    "enabled": self.okx_enabled.get()
                },
                "binance": {
                    "api_key": self.binance_api_key.get(),
                    "secret": self.binance_secret.get(),
                    "testnet": self.binance_testnet.get(),
                    "enabled": self.binance_enabled.get()
                },
                "bingx": {
                    "api_key": self.bingx_api_key.get(),
                    "secret": self.bingx_secret.get(),
                    "enabled": self.bingx_enabled.get()
                },
                "gate": {
                    "api_key": self.gate_api_key.get(),
                    "secret": self.gate_secret.get(),
                    "enabled": self.gate_enabled.get()
                },
                "asterdex": {
                    "api_key": self.asterdex_api_key.get(),
                    "secret": self.asterdex_secret.get(),
                    "testnet": self.asterdex_testnet.get(),
                    "enabled": self.asterdex_enabled.get()
                },
                "bybit": {
                    "api_key": self.bybit_api_key.get(),
                    "secret": self.bybit_secret.get(),
                    "testnet": self.bybit_testnet.get(),
                    "enabled": self.bybit_enabled.get()
                },
                "trading": {
                    "leverage": self.leverage_var.get(),
                    "size": self.size_entry.get(),
                    "debug": self.debug_mode.get()
                }
            }
            
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
            
            self._log(f"✅ Config saved to {CONFIG_FILE}")
            messagebox.showinfo("Success", "Config saved successfully!")
        except Exception as e:
            self._log(f"❌ Error saving config: {e}")
            messagebox.showerror("Error", f"Failed to save config: {e}")
    
    def _load_config(self):
        """Load config from file"""
        try:
            if not os.path.exists(CONFIG_FILE):
                self._log("No saved config found")
                return
            
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
            
            # OKX
            if "okx" in config:
                self.okx_api_key.delete(0, tk.END)
                self.okx_api_key.insert(0, config["okx"].get("api_key", ""))
                self.okx_secret.delete(0, tk.END)
                self.okx_secret.insert(0, config["okx"].get("secret", ""))
                self.okx_passphrase.delete(0, tk.END)
                self.okx_passphrase.insert(0, config["okx"].get("passphrase", ""))
                self.okx_testnet.set(config["okx"].get("testnet", True))
                self.okx_enabled.set(config["okx"].get("enabled", True))
            
            # Binance
            if "binance" in config:
                self.binance_api_key.delete(0, tk.END)
                self.binance_api_key.insert(0, config["binance"].get("api_key", ""))
                self.binance_secret.delete(0, tk.END)
                self.binance_secret.insert(0, config["binance"].get("secret", ""))
                self.binance_testnet.set(config["binance"].get("testnet", True))
                self.binance_enabled.set(config["binance"].get("enabled", True))
            
            # BingX
            if "bingx" in config:
                self.bingx_api_key.delete(0, tk.END)
                self.bingx_api_key.insert(0, config["bingx"].get("api_key", ""))
                self.bingx_secret.delete(0, tk.END)
                self.bingx_secret.insert(0, config["bingx"].get("secret", ""))
                self.bingx_enabled.set(config["bingx"].get("enabled", False))
            
            # Gate.io
            if "gate" in config:
                self.gate_api_key.delete(0, tk.END)
                self.gate_api_key.insert(0, config["gate"].get("api_key", ""))
                self.gate_secret.delete(0, tk.END)
                self.gate_secret.insert(0, config["gate"].get("secret", ""))
                self.gate_enabled.set(config["gate"].get("enabled", False))
                
            # Asterdex
            if "asterdex" in config:
                self.asterdex_api_key.delete(0, tk.END)
                self.asterdex_api_key.insert(0, config["asterdex"].get("api_key", ""))
                self.asterdex_secret.delete(0, tk.END)
                self.asterdex_secret.insert(0, config["asterdex"].get("secret", ""))
                self.asterdex_testnet.set(config["asterdex"].get("testnet", False))
                self.asterdex_enabled.set(config["asterdex"].get("enabled", False))

            # Bybit
            if "bybit" in config:
                self.bybit_api_key.delete(0, tk.END)
                self.bybit_api_key.insert(0, config["bybit"].get("api_key", ""))
                self.bybit_secret.delete(0, tk.END)
                self.bybit_secret.insert(0, config["bybit"].get("secret", ""))
                self.bybit_testnet.set(config["bybit"].get("testnet", False))
                self.bybit_enabled.set(config["bybit"].get("enabled", False))

            # Trading settings
            if "trading" in config:
                self.leverage_var.set(config["trading"].get("leverage", "3"))
                self.size_entry.delete(0, tk.END)
                self.size_entry.insert(0, config["trading"].get("size", "0.001"))
                self.debug_mode.set(config["trading"].get("debug", False))
            
            self._log("✅ Config loaded successfully")
        except Exception as e:
            self._log(f"❌ Error loading config: {e}")
    
    def _create_widgets(self):
        """Create main widgets"""
        build_widgets(self)
    
    def _setup_logging(self):
        """Setup logging to GUI and optionally to file"""
        class GUIHandler(logging.Handler):
            def __init__(self, text_widget, root):
                super().__init__()
                self.text_widget = text_widget
                self.root = root
                self._pending_msgs = []
                self._flush_scheduled = False
            
            def emit(self, record):
                msg = self.format(record)
                self._pending_msgs.append(msg)
                # Batch messages - flush every 100ms instead of immediately
                if not self._flush_scheduled:
                    self._flush_scheduled = True
                    self.root.after(100, self._flush)
            
            def _flush(self):
                self._flush_scheduled = False
                if not self._pending_msgs:
                    return
                # Batch all pending messages
                msgs = self._pending_msgs[:]
                self._pending_msgs.clear()
                self.text_widget.configure(state=tk.NORMAL)
                for msg in msgs:
                    self.text_widget.insert(tk.END, msg + "\n")
                self.text_widget.see(tk.END)
                self.text_widget.configure(state=tk.DISABLED)
        
        handler = GUIHandler(self.log_text, self.root)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.INFO)
        
        # Store reference for file handler (will be added when debug mode enabled)
        self._file_handler = None
        self._log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    
    def _setup_debug_file_logging(self):
        """Setup file logging when debug mode is enabled"""
        if self._file_handler:
            return  # Already setup
        
        # Create logs directory if not exists
        os.makedirs(self._log_dir, exist_ok=True)
        
        # Create log file with timestamp
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = os.path.join(self._log_dir, f"session_{timestamp}.log")
        
        # Create file handler
        self._file_handler = logging.FileHandler(log_file, encoding='utf-8')
        self._file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s'))
        self._file_handler.setLevel(logging.DEBUG)
        
        # Add to root logger
        logging.root.addHandler(self._file_handler)
        logging.root.setLevel(logging.DEBUG)
        
        self._log(f"📁 Debug log file: {log_file}")
    
    def _remove_debug_file_logging(self):
        """Remove file logging when debug mode is disabled"""
        if self._file_handler:
            logging.root.removeHandler(self._file_handler)
            self._file_handler.close()
            self._file_handler = None
            logging.root.setLevel(logging.INFO)
    
    def _on_debug_mode_changed(self):
        """Handle debug mode checkbox change"""
        if self.debug_mode.get():
            self._setup_debug_file_logging()
            self._log("🔍 Debug mode ENABLED - Logging to file")
        else:
            self._log("🔍 Debug mode DISABLED")
            self._remove_debug_file_logging()
    
    def _start_async_loop(self):
        """Start async loop"""
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
        
        self.async_thread = threading.Thread(target=run_loop, daemon=True)
        self.async_thread.start()
    
    def _run_async(self, coro):
        """Run coroutine"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None
    
    def _get_exchange_enum(self, name: str) -> Exchange:
        """Convert exchange name to enum"""
        mapping = {
            "OKX": Exchange.OKX,
            "Binance": Exchange.BINANCE,
            "BingX": Exchange.BINGX,
            "Gate.io": Exchange.GATE,
            "Asterdex": Exchange.ASTERDEX,
            "Bybit": Exchange.BYBIT
        }
        return mapping.get(name, Exchange.OKX)
    
    def _connect(self):
        """Connect to exchanges"""
        self._log("Connecting to exchanges...")
        self.connect_btn.config(state=tk.DISABLED)
        
        future = self._run_async(self._async_connect())
        if future:
            future.add_done_callback(self._on_connect_complete)
    
    async def _async_connect(self):
        """Async connection"""
        return await self.service.connect_exchanges(
            {
                Exchange.OKX: {
                    "enabled": self.okx_enabled.get(),
                    "api_key": self.okx_api_key.get().strip(),
                    "secret_key": self.okx_secret.get().strip(),
                    "passphrase": self.okx_passphrase.get().strip(),
                    "testnet": self.okx_testnet.get(),
                },
                Exchange.BINANCE: {
                    "enabled": self.binance_enabled.get(),
                    "api_key": self.binance_api_key.get().strip(),
                    "secret_key": self.binance_secret.get().strip(),
                    "testnet": self.binance_testnet.get(),
                },
                Exchange.BINGX: {
                    "enabled": self.bingx_enabled.get(),
                    "api_key": self.bingx_api_key.get().strip(),
                    "secret_key": self.bingx_secret.get().strip(),
                },
                Exchange.GATE: {
                    "enabled": self.gate_enabled.get(),
                    "api_key": self.gate_api_key.get().strip(),
                    "secret_key": self.gate_secret.get().strip(),
                    "testnet": self.gate_testnet.get(),
                },
                Exchange.ASTERDEX: {
                    "enabled": self.asterdex_enabled.get(),
                    "api_key": self.asterdex_api_key.get().strip(),
                    "secret_key": self.asterdex_secret.get().strip(),
                    "testnet": self.asterdex_testnet.get(),
                },
                Exchange.BYBIT: {
                    "enabled": self.bybit_enabled.get(),
                    "api_key": self.bybit_api_key.get().strip(),
                    "secret_key": self.bybit_secret.get().strip(),
                    "testnet": self.bybit_testnet.get(),
                },
            },
            debug=self.debug_mode.get(),
            time_sync_fn=sync_windows_time,
            log_callback=self._log,
        )

    def _on_connect_complete(self, future):
        """Handle connection complete"""
        try:
            connected, balances = future.result()
            
            if len(connected) >= 2:
                self.connected = True
                # Use root.after to ensure UI update happens on main thread
                self.root.after(0, lambda: self._update_ui_connected(connected, balances))
                self.root.after(0, lambda: self._log(f"Connected to: {', '.join(connected)}"))
            else:
                self.root.after(0, self._update_ui_disconnected)
                self.root.after(0, lambda: self._log(f"Need at least 2 exchanges. Connected: {connected}"))
                self.root.after(0, lambda: messagebox.showerror("Error", "Need at least 2 exchanges connected for arbitrage"))
        except Exception as e:
            self.root.after(0, self._update_ui_disconnected)
            self.root.after(0, lambda: self._log(f"Connection error: {e}"))
    
    def _update_ui_connected(self, connected, balances):
        """Update UI after connection"""
        self.status_label.config(text=f"🟢 Connected: {', '.join(connected)}")
        self.connect_btn.config(state=tk.DISABLED)
        self.disconnect_btn.config(state=tk.NORMAL)
        self.open_btn.config(state=tk.NORMAL)
        self.close_btn.config(state=tk.NORMAL)
        self.load_pos_btn.config(state=tk.NORMAL)
        self.refresh_funding_btn.config(state=tk.NORMAL)
        
        # Enable load pairs button if Binance is connected
        if "Binance" in connected:
            self.load_pairs_btn.config(state=tk.NORMAL)
        
        # Update balances
        if Exchange.OKX in balances:
            self.okx_balance_label.config(text=f"${balances[Exchange.OKX].available:.6f}")
        if Exchange.BINANCE in balances:
            self.binance_balance_label.config(text=f"${balances[Exchange.BINANCE].available:.6f}")
        if Exchange.BINGX in balances:
            self.bingx_balance_label.config(text=f"${balances[Exchange.BINGX].available:.6f}")
        if Exchange.GATE in balances:
            self.gate_balance_label.config(text=f"${balances[Exchange.GATE].available:.6f}")
        if Exchange.ASTERDEX in balances:
            self.asterdex_balance_label.config(text=f"${balances[Exchange.ASTERDEX].available:.6f}")
        if Exchange.BYBIT in balances:
            self.bybit_balance_label.config(text=f"${balances[Exchange.BYBIT].available:.6f}")

        # Show/hide funding table columns based on connected exchanges
        connected_exchanges = set(self.manager.clients.keys())
        for col_name, ex_enum in self._exchange_columns.items():
            if ex_enum in connected_exchanges:
                self.funding_tree.column(col_name, width=85, stretch=True)
            else:
                self.funding_tree.column(col_name, width=0, minwidth=0, stretch=False)

        # Update exchange dropdowns
        available = connected  # Use exchange names directly from connected list
        self.long_exchange['values'] = available
        self.short_exchange['values'] = available
        if available:
            self.long_exchange.set(available[0])
            if len(available) > 1:
                self.short_exchange.set(available[1])
    
    def _update_ui_disconnected(self):
        """Update UI after disconnect"""
        self.status_label.config(text="⚪ Disconnected")
        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.open_btn.config(state=tk.DISABLED)
        self.close_btn.config(state=tk.DISABLED)
        self.load_pos_btn.config(state=tk.DISABLED)
        self.refresh_funding_btn.config(state=tk.DISABLED)
        self.load_pairs_btn.config(state=tk.DISABLED)
        self.connected = False
    
    def _disconnect(self):
        """Disconnect"""
        self._log("Disconnecting...")
        self._run_async(self.manager.disconnect_all())
        self._stop_monitoring()
        self._update_ui_disconnected()
    
    def _load_binance_pairs(self):
        """Load all trading pairs from Binance"""
        if not self.connected:
            return
        
        binance_client = self.manager.clients.get(Exchange.BINANCE)
        if not binance_client:
            messagebox.showerror("Error", "Binance is not connected")
            return
        
        self._log("Loading all pairs from Binance...")
        self.load_pairs_btn.config(state=tk.DISABLED)
        
        async def async_load_pairs():
            try:
                from exchanges.binance_client import BinanceClient
                if not isinstance(binance_client, BinanceClient):
                    return []
                
                # Get all funding rates (which includes all perpetual contracts)
                funding_rates = await binance_client.get_all_funding_rates()
                
                # Convert to unified pair format
                pairs = []
                for rate in funding_rates:
                    symbol = rate.symbol
                    if symbol.endswith("USDT"):
                        base = symbol.replace("USDT", "")
                        pair = f"{base}/USDT"
                        pairs.append(pair)
                
                # Sort alphabetically
                pairs.sort()
                return pairs
                
            except Exception as e:
                logger.error(f"Error loading Binance pairs: {e}")
                return []
        
        future = self._run_async(async_load_pairs())
        if future:
            future.add_done_callback(self._on_load_pairs_complete)
    
    def _on_load_pairs_complete(self, future):
        """Handle load pairs complete"""
        def update_ui():
            self.load_pairs_btn.config(state=tk.NORMAL)
            try:
                pairs = future.result()
                if pairs:
                    self._update_pair_dropdown(pairs)
                    self._log(f"✅ Loaded {len(pairs)} pairs from Binance")
                else:
                    self._log("❌ No pairs loaded from Binance")
            except Exception as e:
                self._log(f"Error loading pairs: {e}")
        
        # Run on main thread
        self.root.after(0, update_ui)
    
    def _update_pair_dropdown(self, pairs):
        """Update pair dropdown with new pairs"""
        current_pair = self.pair_combo.get()
        self.pair_combo['values'] = pairs
        
        # Keep current selection if it exists in new list
        if current_pair in pairs:
            self.pair_combo.set(current_pair)
        elif pairs:
            self.pair_combo.set(pairs[0])
    
    def _stop_all_background_tasks(self):
        """Stop all background tasks to prevent API conflicts when opening position"""
        # Stop pair info updates
        if self._pair_info_update_task:
            self.root.after_cancel(self._pair_info_update_task)
            self._pair_info_update_task = None
        
        # Stop USDT volume update
        if hasattr(self, '_usdt_update_pending') and self._usdt_update_pending:
            self.root.after_cancel(self._usdt_update_pending)
            self._usdt_update_pending = None
        
        # Stop monitoring temporarily
        if self.monitoring:
            self._stop_monitoring()
        
        logger.info("🛑 Stopped all background tasks")
    
    def _open_position(self):
        """Open hedged position - analyze spread first, then wait for threshold"""
        if not self.connected:
            return
        
        # If already analyzing or waiting, this is a CANCEL action
        if self.analyzing_spread or self.waiting_for_price_spread:
            self._cancel_open_position()
            return
        
        pair = self.pair_combo.get()
        long_ex_name = self.long_exchange.get()
        short_ex_name = self.short_exchange.get()
        
        if long_ex_name == short_ex_name:
            messagebox.showerror("Error", "Long and Short exchanges must be different")
            return
        
        try:
            size = float(self.size_entry.get())
            leverage = int(self.leverage_var.get())
            split_count = int(self.split_count_var.get())
            if split_count < 1:
                split_count = 1
            elif split_count > 1000:
                messagebox.showerror("Error", "Split count cannot exceed 1000")
                return
        except ValueError:
            messagebox.showerror("Error", "Invalid size, leverage, or split count")
            return
        
        long_ex = self._get_exchange_enum(long_ex_name)
        short_ex = self._get_exchange_enum(short_ex_name)
        
        # Quick profitability check using current funding rates (if available in cache)
        # This is a non-blocking check to warn user before starting analyze
        try:
            be_result = calculate_break_even(
                long_exchange=long_ex,
                short_exchange=short_ex,
                position_size_usd=size,
                funding_rate_pct=0.01,  # Assume minimum 0.01% for warning
                leverage=leverage,
                slippage_pct=0.02
            )
            min_funding_needed = be_result['break_even_rate']
            
            # Show warning with fee info
            confirm_msg = (
                f"Open {pair}?\n\n"
                f"Size: ${size} | Leverage: {leverage}x | Splits: {split_count}\n"
                f"LONG: {long_ex_name} | SHORT: {short_ex_name}\n\n"
                f"--- FEE ESTIMATE ---\n"
                f"Total fees + slippage: ~{be_result['total_cost_pct']:.3f}%\n"
                f"Est. cost: ~${be_result['total_cost_usd']:.2f}\n"
                f"Min funding needed: {min_funding_needed:.3f}%\n\n"
                f"Proceed with 2-min spread analysis?"
            )
            
            if not messagebox.askyesno("Confirm Open Position", confirm_msg):
                return
        except Exception as e:
            logger.warning(f"Could not calculate break-even: {e}")
            # Fallback to simple confirm
            if not messagebox.askyesno("Confirm", f"Open {pair}?\nSize: ${size}, Leverage: {leverage}x, Splits: {split_count}"):
                return
        
        # STOP ALL BACKGROUND TASKS before opening position
        self._stop_all_background_tasks()
        
        # Store parameters
        skip_spread = self.skip_spread_check_var.get()
        self.price_spread_params = {
            "pair": pair,
            "long_ex": long_ex,
            "short_ex": short_ex,
            "long_ex_name": long_ex_name,
            "short_ex_name": short_ex_name,
            "size": size,
            "leverage": leverage,
            "price_spread_min": -100.0 if skip_spread else None,  # Will be set after analyze if not skipping
            "split_count": split_count,
            "skip_leverage": self.skip_leverage_var.get(),
            "skip_spread_check": skip_spread
        }
        
        # Start analyze phase OR skip directly to open
        if skip_spread:
            self._log(f"⚡ Bỏ qua analyze spread, tiến hành mở lệnh ngay cho {pair}...")
            self._log(f"   LONG: {long_ex_name} | SHORT: {short_ex_name}")
            # Skip direct to spreading check/execute (threshold=-100 so it fires immediately)
            self.waiting_for_price_spread = True
            self.open_btn.configure(text="⏳ Opening... (Cancel)")
            self._check_price_spread_and_open()
        else:
            self.analyzing_spread = True
            self.analyze_cancel_event = threading.Event()
            self.open_btn.configure(text="📊 Analyzing... (Cancel)")
            self._log(f"📊 Bắt đầu analyze spread 2 phút cho {pair}...")
            self._log(f"   LONG: {long_ex_name} | SHORT: {short_ex_name}")
            
            # Subscribe to WS Market Data
            self._run_async(self.manager.subscribe_market_data(pair, long_ex, short_ex))
            
            # Start analyze
            self._start_analyze_for_open()
    
    def _start_analyze_for_open(self):
        """Run analyze spread and then start waiting for open"""
        params = self.price_spread_params
        if not params:
            return
        
        def safe_log(msg):
            self.root.after(0, lambda m=msg: self._log(m))
        
        async def do_analyze():
            result = await self.manager.analyze_spread(
                params["pair"],
                params["long_ex"],
                params["short_ex"],
                duration_seconds=120.0,  # 2 phút
                check_interval=2.0,      # 2 giây
                log_callback=safe_log,
                cancel_event=self.analyze_cancel_event,
                mode="open"
            )
            return result
        
        future = self._run_async(do_analyze())
        if future:
            future.add_done_callback(lambda f: self._on_analyze_for_open_complete(f))
    
    def _on_analyze_for_open_complete(self, future):
        """Handle analyze completion for open"""
        def update_ui():
            try:
                result = future.result(timeout=1.0)
                self.analyzing_spread = False
                self.analyze_cancel_event = None
                
                if not result.get("success"):
                    error = result.get("error", "Unknown error")
                    self._log(f"❌ Analyze thất bại: {error}")
                    self._reset_open_button()
                    return
                
                # Get second best spread as threshold
                second_best = result["second_best_spread"]
                self.price_spread_params["price_spread_min"] = second_best
                
                # Update UI threshold display
                self.price_spread_threshold.delete(0, tk.END)
                self.price_spread_threshold.insert(0, f"{second_best:.4f}")
                
                self._log(f"✅ Analyze xong! Threshold = {second_best:.4f}% (giá trị cao thứ 2)")
                self._log(f"⏳ Đang chờ spread >= {second_best:.4f}% để mở position...")
                
                # Update button to show waiting state
                self.open_btn.configure(text="⏳ Waiting... (Cancel)")
                
                # Now start waiting for spread
                self.waiting_for_price_spread = True
                self._check_price_spread_and_open()
                
            except Exception as e:
                self._log(f"❌ Lỗi analyze: {e}")
                self._reset_open_button()
        
        self.root.after(0, update_ui)
    
    def _reset_open_button(self):
        """Reset open button to default state"""
        self.analyzing_spread = False
        self.waiting_for_price_spread = False
        self.analyze_cancel_event = None
        self.price_spread_params = None
        self.open_btn.configure(text="Open Position", state=tk.NORMAL)
    
    def _cancel_open_position(self):
        """Cancel analyzing or waiting for price spread"""
        # Cancel analyze if running
        if self.analyze_cancel_event:
            self.analyze_cancel_event.set()
            self._log("🛑 Đang cancel analyze...")
        
        # Cancel waiting/splits
        self._cancel_price_spread_wait()
        
        self._reset_open_button()
    
    def _cancel_price_spread_wait(self):
        """Cancel waiting for price spread and/or running splits"""
        self.waiting_for_price_spread = False
        if self.price_spread_check_task:
            self.root.after_cancel(self.price_spread_check_task)
            self.price_spread_check_task = None
        
        # Signal cancellation to running splits
        if self.split_cancel_event:
            self.split_cancel_event.set()
            self._log("🛑 Cancel signal sent to running splits...")
        
        self.price_spread_params = None
        self.open_btn.configure(text="Open Position", state=tk.NORMAL)
        self._log("🛑 Cancelled waiting for price spread")

    async def _service_check_and_open(self, params, safe_log):
        try:
            spread_data = await self.service.check_open_spread(
                params["pair"],
                params["long_ex"],
                params["short_ex"],
            )
        except asyncio.TimeoutError:
            safe_log("⚠️ Timeout fetching order books, retrying...")
            return {"success": False, "waiting": True}
        except Exception as e:
            safe_log(f"⚠️ {e}, retrying...")
            return {"success": False, "waiting": True}

        long_ask_price = spread_data["long_ask_price"]
        short_bid_price = spread_data["short_bid_price"]
        price_spread_pct = spread_data["price_spread_pct"]

        current_threshold = params.get("price_spread_min", 0)
        check_count = params.get("spread_check_count", 0) + 1
        params["spread_check_count"] = check_count

        if price_spread_pct < 0:
            safe_log(
                f"📊 Real spread (OI): {price_spread_pct:.4f}% ⚠️ NEGATIVE "
                f"(threshold: {current_threshold:.4f}%) (check {check_count % 30}/30) | "
                f"LONG ASK: ${long_ask_price:,.6f} | SHORT BID: ${short_bid_price:,.6f}"
            )
        else:
            safe_log(
                f"📊 Real spread (OI): {price_spread_pct:.4f}% "
                f"(threshold: {current_threshold:.4f}%) (check {check_count % 30}/30) | "
                f"LONG ASK: ${long_ask_price:,.6f} | SHORT BID: ${short_bid_price:,.6f}"
            )

        if price_spread_pct < current_threshold:
            if check_count >= 30 and check_count % 30 == 0:
                old_threshold = current_threshold
                params["price_spread_min"] = current_threshold - 0.01
                safe_log(
                    f"⚠️ 30 lần chưa đạt, giảm threshold: "
                    f"{old_threshold:.4f}% → {params['price_spread_min']:.4f}%"
                )
            return {"success": False, "waiting": True}

        split_count = params.get("split_count", 1)
        safe_log(f"✅ Price spread {price_spread_pct:.4f}% >= {current_threshold:.4f}%, and price position is good!")
        safe_log(
            f"🚀 Opening: {params['pair']} | Long {params['long_ex_name']} | "
            f"Short {params['short_ex_name']} | Size: {params['size']} | Splits: {split_count}"
        )

        self.split_cancel_event = threading.Event()

        def on_first_split(size_opened):
            self.root.after(0, lambda: self._on_first_split_complete(params, size_opened))

        result = await self.service.execute_open_splits(
            pair=params["pair"],
            long_exchange=params["long_ex"],
            short_exchange=params["short_ex"],
            size=params["size"],
            leverage=params["leverage"],
            split_count=split_count,
            price_spread_min=params["price_spread_min"],
            log_callback=safe_log,
            on_first_split_complete=on_first_split,
            cancel_event=self.split_cancel_event,
            skip_leverage_set=params.get("skip_leverage", False),
            skip_spread_check=params.get("skip_spread_check", False),
        )
        self.split_cancel_event = None
        self.balance_before_open = result.get("balance_before", {})
        return result
    
    def _check_price_spread_and_open(self):
        """Check price spread and open position if threshold met"""
        if not self.waiting_for_price_spread or not self.price_spread_params:
            return
        
        params = self.price_spread_params
        
        # Thread-safe log callback - define ONCE here
        def safe_log(msg):
            self.root.after(0, lambda m=msg: self._log(m))
        
        async def check_and_open():
            return await self._service_check_and_open(params, safe_log)

            try:
                # Get current prices from both exchanges with timeout
                long_client = self.manager.clients.get(params["long_ex"])
                short_client = self.manager.clients.get(params["short_ex"])
                
                if not long_client or not short_client:
                    safe_log("❌ Exchange clients not available")
                    return {"success": False, "error": "Exchange clients not available"}
                
                long_symbol = get_exchange_symbol(params["pair"], params["long_ex"])
                short_symbol = get_exchange_symbol(params["pair"], params["short_ex"])
                
                # Fetch order books to calculate real spread (bid/ask based)
                try:
                    long_book, short_book = await asyncio.wait_for(
                        asyncio.gather(
                            long_client.get_order_book(long_symbol, limit=10),
                            short_client.get_order_book(short_symbol, limit=10)
                        ),
                        timeout=10.0
                    )
                    
                    # Get best ask from LONG exchange (price we pay when buying)
                    if not long_book.get("asks") or len(long_book["asks"]) == 0:
                        safe_log("⚠️ No asks in order book, retrying...")
                        return {"success": False, "waiting": True}
                    long_ask_price = long_book["asks"][0][0]
                    
                    # Get best bid from SHORT exchange (price we receive when selling)
                    if not short_book.get("bids") or len(short_book["bids"]) == 0:
                        safe_log("⚠️ No bids in order book, retrying...")
                        return {"success": False, "waiting": True}
                    short_bid_price = short_book["bids"][0][0]
                    
                except asyncio.TimeoutError:
                    safe_log("⚠️ Timeout fetching order books, retrying...")
                    return {"success": False, "waiting": True}
                
                # Calculate real price spread (BID - ASK)
                # Positive spread = profitable (sell high, buy low)
                # Negative spread = acceptable for funding arbitrage (profit from funding fees)
                price_diff = short_bid_price - long_ask_price
                
                avg_price = (long_ask_price + short_bid_price) / 2
                price_spread_pct = (price_diff / avg_price) * 100
                
                # Get current threshold (may have been reduced)
                current_threshold = params.get("price_spread_min", 0)
                check_count = params.get("spread_check_count", 0) + 1
                params["spread_check_count"] = check_count
                
                # Show spread info with appropriate indicator
                if price_spread_pct < 0:
                    safe_log(f"📊 Real spread (OI): {price_spread_pct:.4f}% ⚠️ NEGATIVE (threshold: {current_threshold:.4f}%) (check {check_count % 30}/30) | LONG ASK: ${long_ask_price:,.6f} | SHORT BID: ${short_bid_price:,.6f}")
                else:
                    safe_log(f"📊 Real spread (OI): {price_spread_pct:.4f}% (threshold: {current_threshold:.4f}%) (check {check_count % 30}/30) | LONG ASK: ${long_ask_price:,.6f} | SHORT BID: ${short_bid_price:,.6f}")
                
                # Check if spread meets minimum threshold
                if price_spread_pct < current_threshold:
                    # After 30 checks, reduce threshold by 0.01%
                    if check_count >= 30 and check_count % 30 == 0:
                        old_threshold = current_threshold
                        new_threshold = current_threshold - 0.01
                        params["price_spread_min"] = new_threshold
                        safe_log(f"⚠️ 30 lần chưa đạt, giảm threshold: {old_threshold:.4f}% → {new_threshold:.4f}%")
                    
                    # Not ready yet, check again in 2 seconds
                    return {"success": False, "waiting": True}
                
                # Price spread is good, proceed to open position!
                split_count = params.get("split_count", 1)
                safe_log(f"✅ Price spread {price_spread_pct:.4f}% >= {current_threshold:.4f}%, and price position is good!")
                safe_log(f"🚀 Opening: {params['pair']} | Long {params['long_ex_name']} | Short {params['short_ex_name']} | Size: {params['size']} | Splits: {split_count}")
                
                # Save balance BEFORE opening for PnL calculation later
                balance_before = {}
                for ex in [params["long_ex"], params["short_ex"]]:
                    client = self.manager.clients.get(ex)
                    if client:
                        try:
                            bal = await client.get_balance()
                            if bal:
                                balance_before[ex] = bal.available
                        except:
                            pass
                self.balance_before_open = balance_before
                safe_log(f"💰 Balance before: Long({params['long_ex'].value}): ${balance_before.get(params['long_ex'], 0):.2f} | Short({params['short_ex'].value}): ${balance_before.get(params['short_ex'], 0):.2f}")
                
                # Create cancel event for this split session (use threading.Event for thread-safety)
                self.split_cancel_event = threading.Event()
                
                # Callback to start monitoring after first split
                def on_first_split(size_opened):
                    self.root.after(0, lambda: self._on_first_split_complete(params, size_opened))
                
                result = await self.manager.open_hedged_position_split(
                    params["pair"], 
                    params["long_ex"], 
                    params["short_ex"], 
                    params["size"], 
                    params["leverage"],
                    split_count=split_count,
                    delay_between_splits=5.0,  # Reduced from 13s to 2s for faster execution
                    price_spread_min=params["price_spread_min"],
                    spread_check_interval=2.0,
                    max_wait_per_split=3600.0,
                    log_callback=safe_log,
                    on_first_split_complete=on_first_split,
                    skip_leverage_set=params.get("skip_leverage", False),
                    cancel_event=self.split_cancel_event,
                    skip_spread_check=params.get("skip_spread_check", False)
                )
                
                # Clear cancel event after done
                self.split_cancel_event = None
                
                return result
                
            except Exception as e:
                safe_log(f"❌ Error: {e}")
                return {"success": False, "error": str(e)}
        
        future = self._run_async(check_and_open())
        if future:
            future.add_done_callback(lambda f: self._on_price_spread_check_complete(f))
    
    def _on_price_spread_check_complete(self, future):
        """Handle price spread check result"""
        logger.debug("_on_price_spread_check_complete: ENTER (callback from async thread)")
        
        def update_ui():
            logger.debug("_on_price_spread_check_complete.update_ui: ENTER (on main thread)")
            try:
                # Get result with timeout to prevent blocking
                result = future.result(timeout=0.1)  # Should already be done
                logger.debug(f"_on_price_spread_check_complete.update_ui: got result success={result.get('success')}")
                
                # Check if just waiting (spread not met yet)
                if not result.get("success") and result.get("waiting"):
                    # Schedule next check in 2 seconds
                    if self.waiting_for_price_spread:
                        self.price_spread_check_task = self.root.after(2000, self._check_price_spread_and_open)
                    logger.debug("_on_price_spread_check_complete.update_ui: EXIT (waiting)")
                    return
                
                # Either success or error - stop waiting mode
                self.waiting_for_price_spread = False
                self.open_btn.configure(text="Open Position", state=tk.NORMAL)
                
                if result.get("success"):
                    # Position opened successfully!
                    params = self.price_spread_params
                    if not params:
                        logger.debug("_on_price_spread_check_complete.update_ui: EXIT (no params)")
                        return
                    
                    splits_completed = result.get("splits_completed", 1)
                    splits_total = result.get("splits_total", 1)
                    self._log(f"✅ Position opened successfully! ({splits_completed}/{splits_total} splits completed)")
                    
                    # NOW start monitoring after all splits are done
                    total_size = result.get("total_long_size", params["size"])
                    self._on_all_splits_complete(params, total_size)
                    logger.debug("_on_price_spread_check_complete.update_ui: EXIT (called _on_all_splits_complete)")
                    
                else:
                    # Error occurred
                    error_msg = result.get("error", "Unknown error")
                    self._log(f"❌ Failed to open position: {error_msg}")
                    # Schedule messagebox to not block current callback
                    self.root.after(10, lambda: messagebox.showerror("Error", f"Failed to open position:\n{error_msg}"))
                    logger.debug("_on_price_spread_check_complete.update_ui: EXIT (error)")
                
            except Exception as e:
                self.waiting_for_price_spread = False
                self.open_btn.configure(text="Open Position", state=tk.NORMAL)
                self._log(f"❌ Error: {e}")
                # Schedule messagebox to not block current callback
                self.root.after(10, lambda err=str(e): messagebox.showerror("Error", err))
                logger.debug(f"_on_price_spread_check_complete.update_ui: EXIT (exception: {e})")
        
        # Run on main thread
        self.root.after(0, update_ui)
        logger.debug("_on_price_spread_check_complete: EXIT (scheduled update_ui)")
    
    def _on_first_split_complete(self, params, size_opened):
        """Called after first split completes - just track position, don't start full monitoring yet"""
        logger.debug("_on_first_split_complete: ENTER")
        try:
            # Cancel pair info update to avoid overloading during position
            if self._pair_info_update_task:
                self.root.after_cancel(self._pair_info_update_task)
                self._pair_info_update_task = None
                logger.debug("_on_first_split_complete: Cancelled pair info update task")
            
            # Just setup position tracking, DON'T start monitoring yet
            # Monitoring will start after ALL splits complete to reduce GUI load
            self._setup_position_tracking(
                params["pair"], 
                params["long_ex"], 
                params["short_ex"], 
                size_opened
            )
            
            logger.debug("_on_first_split_complete: EXIT")
        except Exception as e:
            logger.error(f"_on_first_split_complete error: {e}")
    
    def _on_all_splits_complete(self, params, total_size):
        """Called after ALL splits complete"""
        logger.debug("_on_all_splits_complete: ENTER")
        try:
            # Update position size to final total
            if self.active_position:
                self.active_position["size"] = total_size
            
            # Auto-start monitoring after position opens
            def start_monitor():
                if not self.monitoring:
                    logger.debug("_on_all_splits_complete: Starting monitoring")
                    self._start_monitoring()
                    self.monitor_btn.config(text="⏹ Stop Monitor")
                else:
                    logger.debug("_on_all_splits_complete: Monitoring already active")
            self.root.after(500, start_monitor)
            
            logger.debug("_on_all_splits_complete: EXIT")
        except Exception as e:
            logger.error(f"_on_all_splits_complete: ERROR {e}")
    
    def _setup_position_tracking(self, pair, long_ex, short_ex, size):
        """Setup position tracking after successful open"""
        async def get_initial_state():
            return await self.service.get_initial_position_state(pair, long_ex, short_ex, size)

        def on_state_ready(fut):
            def update_ui():
                try:
                    self.active_position = fut.result(timeout=0.1)
                    self._log(
                        f"Initial Funding - LONG: {self.active_position['initial_long_rate']*100:.6f}%, "
                        f"SHORT: {self.active_position['initial_short_rate']*100:.6f}%, "
                        f"Net: {self.active_position['initial_net_funding']*100:.6f}%"
                    )
                    self._update_position_display()
                except Exception as e:
                    logger.error(f"Error fetching initial funding: {e}")

            self.root.after(0, update_ui)

        future = self._run_async(get_initial_state())
        if future:
            future.add_done_callback(on_state_ready)

    def _close_position(self):
        """Close position - analyze spread first, then close in splits. Supports partial close."""
        if not self.active_position:
            messagebox.showinfo("Info", "No active position")
            return

        # If already analyzing or waiting, this is a CANCEL action
        if getattr(self, 'analyzing_close', False) or getattr(self, 'waiting_for_close', False):
            self._cancel_close_process()
            return

        try:
            splits = int(self.split_count_var.get())
            if splits < 1: splits = 1
        except:
            splits = 1

        skip_spread = self.skip_spread_check_var.get()

        # Parse partial close size
        close_size_str = self.close_size_var.get().strip()
        close_size = None  # None = close all
        if close_size_str:
            try:
                close_size = float(close_size_str)
                if close_size <= 0:
                    messagebox.showerror("Error", "Close size must be > 0")
                    return
            except ValueError:
                messagebox.showerror("Error", "Invalid close size. Enter a number or leave empty for full close.")
                return

        pos = self.active_position

        # Build confirm message with % info
        if close_size is not None:
            total_size = pos.get('size', 0) or pos.get('long_size', 0) or pos.get('short_size', 0)
            if total_size > 0:
                pct = (close_size / total_size) * 100
                pct_str = f"{pct:.1f}%"
            else:
                pct_str = "?%"
            size_info = f"Partial close: {close_size} tokens ({pct_str} of position)"
        else:
            size_info = "Full close: 100% of position"

        if skip_spread:
            confirm_msg = f"{size_info}\nSplits: {splits}\nSkip spread check, close immediately."
        else:
            confirm_msg = f"{size_info}\nSplits: {splits}\nAnalyze spread 2 minutes first."

        if not messagebox.askyesno("Confirm Close", confirm_msg):
            return
        self._log(f"📤 Bắt đầu quá trình đóng position {pos['pair']}...")
        
        # STOP ALL BACKGROUND TASKS
        self._stop_all_background_tasks()
        
        # Update UI state
        self.analyzing_close = not skip_spread
        self.close_cancel_event = threading.Event()
        if skip_spread:
            self.close_btn.configure(text="🛑 Closing... (Cancel)", state=tk.NORMAL)
        else:
            self.close_btn.configure(text="📊 Analyzing... (Cancel)", state=tk.NORMAL)
        self.open_btn.configure(state=tk.DISABLED)
        
        # Store close params
        self.close_params = {
            "pair": pos["pair"],
            "long_exchange": pos["long_exchange"],
            "short_exchange": pos["short_exchange"],
            "splits": splits,
            "skip_spread_check": skip_spread,
            "close_size": close_size,  # None = full close, float = partial close (token amount)
        }
        
        # Subscribe to WS Market Data
        self._run_async(self.manager.subscribe_market_data(pos['pair'], pos['long_exchange'], pos['short_exchange']))
        
        if skip_spread:
            self._log(f"⚡ Bỏ qua analyze spread, tiến hành đóng lệnh ngay cho {pos['pair']}...")
            self._execute_close_workflow()
        else:
            self._log(f"📊 Bắt đầu analyze spread 2 phút cho CLOSE...")
            # Start analyze for close
            self._execute_close_workflow()
    
    def _reset_close_button(self):
        """Reset close button to default state"""
        self.analyzing_close = False
        self.waiting_for_close = False
        self.close_cancel_event = None
        self.close_params = None
        self.close_btn.configure(text="🛑 Close Position", state=tk.NORMAL)
        self.open_btn.configure(state=tk.NORMAL)
            
    def _cancel_close_process(self):
        """Cancel the closing process (analyze or splits)"""
        # Cancel analyze if running
        if getattr(self, 'close_cancel_event', None):
            self.close_cancel_event.set()
            self._log("🛑 Đang cancel analyze/close...")
        
        # Cancel splits if running
        if self.split_cancel_event:
            self.split_cancel_event.set()
            self._log("🛑 Sending cancel signal to close process...")
        
        self._reset_close_button()

    

    def _execute_close_workflow(self):
        """Execute the full close workflow through the shared GUI service."""
        params = self.close_params
        if not params:
            return

        self.waiting_for_close = True
        self.close_btn.configure(text="Closing... (Cancel)")

        async def async_close():
            self.split_cancel_event = self.close_cancel_event
            return await self.service.close_position_with_analysis(
                pair=params['pair'],
                long_exchange=params['long_exchange'],
                short_exchange=params['short_exchange'],
                splits=params['splits'],
                log_callback=lambda msg: self.root.after(0, lambda m=msg: self._log(m)),
                cancel_event=self.close_cancel_event,
                skip_spread_check=params.get('skip_spread_check', False),
                close_size=params.get('close_size'),
            )

        future = self._run_async(async_close())
        if future:
            future.add_done_callback(self._on_close_complete)


    def _load_existing_positions(self):
        """Load existing positions from exchanges and allow monitoring/closing them"""
        if not self.connected:
            messagebox.showerror("Error", "Not connected to exchanges")
            return
        
        self._log("🔍 Scanning for existing positions on all exchanges...")
        self.load_pos_btn.config(state=tk.DISABLED)
        
        async def scan_positions():
            all_positions = await self.manager.scan_all_positions()
            return all_positions
        
        def on_scan_complete(future):
            def update_ui():
                self.load_pos_btn.config(state=tk.NORMAL)
                try:
                    all_positions = future.result(timeout=0.5)
                    
                    # Collect all positions
                    positions_found = []
                    for exchange, positions in all_positions.items():
                        for pos in positions:
                            positions_found.append({
                                "exchange": exchange,
                                "symbol": pos.symbol,
                                "side": pos.side,
                                "size": pos.size,
                                "entry_price": pos.entry_price,
                                "unrealized_pnl": pos.unrealized_pnl,
                                "leverage": pos.leverage
                            })
                    
                    if not positions_found:
                        self._log("📭 No existing positions found on any exchange")
                        messagebox.showinfo("Load Positions", "No existing positions found")
                        return
                    
                    # Log found positions
                    self._log(f"📦 Found {len(positions_found)} position(s):")
                    for p in positions_found:
                        self._log(f"   • {p['exchange'].value}: {p['symbol']} {p['side'].value.upper()} "
                                  f"Size: {p['size']} PnL: ${p['unrealized_pnl']:.2f}")
                    
                    # Try to match hedged pairs (same symbol on different exchanges with opposite sides)
                    hedged_pairs = self._find_hedged_pairs(positions_found)
                    
                    if hedged_pairs:
                        self._log(f"🔗 Found {len(hedged_pairs)} potential hedged pair(s)")
                        # Let user select which pair to load
                        self._show_position_selector(hedged_pairs, positions_found)
                    else:
                        self._log("⚠️ No matching hedged pairs found. Positions may be one-sided.")
                        # Still show positions for manual selection
                        self._show_position_selector([], positions_found)
                        
                except Exception as e:
                    self._log(f"❌ Error scanning positions: {e}")
                    logger.error(f"Error in scan_positions: {e}")
            
            self.root.after(0, update_ui)
        
        future = self._run_async(scan_positions())
        if future:
            future.add_done_callback(on_scan_complete)
    
    def _find_hedged_pairs(self, positions):
        """Find matching hedged pairs from position list."""
        return build_hedged_pairs(positions)

    def _show_position_selector(self, hedged_pairs, all_positions):
        """Show dialog to select position to load."""
        show_existing_position_selector(self, hedged_pairs, all_positions)

    def _load_hedged_position(self, hedged_pair, open_time: Optional[datetime] = None):
        """Load a hedged position and start monitoring."""
        load_existing_hedged_position(self, hedged_pair, open_time)

    def _on_close_complete(self, future):
        """Handle close complete"""
        # Save position info before clearing for PnL calculation
        closed_position = self.active_position.copy() if self.active_position else None
        
        def update_ui():
            self.split_cancel_event = None
            self._reset_close_button()
            
            try:
                result = future.result()
                
                if result.get("cancelled"):
                    self._log("Close process cancelled")
                    return
                
                if result["success"]:
                    self._log("Position closed successfully!")
                    self._stop_monitoring()
                    self.active_position = None
                    self._clear_position_display()
                    
                    # Calculate and display final PnL
                    if closed_position:
                        self._calculate_and_show_final_pnl(closed_position)
                    
                    # If auto trading is enabled, it will resume scanning
                    if getattr(self, 'auto_trading_active', False):
                        self._log(f"Auto Trading: Resuming signal scanning...")
                else:
                    self._log(f"Close failed: {result.get('error', 'Unknown error')}")
                    # If partially closed, we should keep the position active but maybe update size?
                    # For now just leave as is, user can check or retry
            except Exception as e:
                self._log(f"Error: {e}")
        
        # Run on main thread
        self.root.after(0, update_ui)
    
    def _calculate_and_show_final_pnl(self, closed_position: Dict[str, Any]):
        """Calculate and display final PnL after closing position via API history"""
        long_ex = closed_position.get('long_exchange')
        short_ex = closed_position.get('short_exchange')
        pair = closed_position.get('pair', 'Unknown')
        open_time = closed_position.get('open_time')
        
        async def calculate_pnl():
            # Calculate 'since' timestamp in milliseconds, subtracting 1 minute for safety padding
            since = None
            if open_time:
                since = int(open_time.timestamp() * 1000) - 60000
                
            long_client = self.manager.clients.get(long_ex)
            short_client = self.manager.clients.get(short_ex)
            
            long_pnl_data = {"realized_pnl": 0, "commission": 0, "funding_fee": 0, "net_pnl": 0}
            short_pnl_data = {"realized_pnl": 0, "commission": 0, "funding_fee": 0, "net_pnl": 0}
            
            if long_client:
                # Need to use specific symbol mapping for the exchange if necessary
                # Fortunately get_closed_pnl handles this internally using the base symbol
                try:
                    long_pnl_data = await long_client.get_closed_pnl(pair, since)
                except Exception as e:
                    logger.error(f"Error getting long PnL from {long_ex.value}: {e}")
                    
            if short_client:
                try:
                    short_pnl_data = await short_client.get_closed_pnl(pair, since)
                except Exception as e:
                    logger.error(f"Error getting short PnL from {short_ex.value}: {e}")
            
            total_realized_pnl = long_pnl_data["realized_pnl"] + short_pnl_data["realized_pnl"]
            total_commission = long_pnl_data["commission"] + short_pnl_data["commission"]
            total_funding = long_pnl_data["funding_fee"] + short_pnl_data["funding_fee"]
            total_net_pnl = long_pnl_data["net_pnl"] + short_pnl_data["net_pnl"]
            
            return {
                "pair": pair,
                "long_ex": long_ex,
                "short_ex": short_ex,
                "long": long_pnl_data,
                "short": short_pnl_data,
                "total": {
                    "realized_pnl": total_realized_pnl,
                    "commission": total_commission,
                    "funding_fee": total_funding,
                    "net_pnl": total_net_pnl
                }
            }
        
        def on_pnl_calculated(future):
            def show_result():
                try:
                    result = future.result(timeout=15)
                    if result:
                        self._log("=" * 50)
                        self._log(f"FINAL TRADE REPORT: {result['pair']}")
                        self._log("=" * 50)
                        
                        # LONG side
                        long_data = result['long']
                        self._log(f"LONG ({result['long_ex'].value}):")
                        self._log(f"   Realized PnL: ${long_data['realized_pnl']:+.2f}")
                        self._log(f"   Trading Fees: -${long_data['commission']:.2f}")
                        self._log(f"   Funding Fees: ${long_data['funding_fee']:+.2f}")
                        self._log(f"   Net PnL:      ${long_data['net_pnl']:+.2f}")
                        self._log("")
                        
                        # SHORT side
                        short_data = result['short']
                        self._log(f"SHORT ({result['short_ex'].value}):")
                        self._log(f"   Realized PnL: ${short_data['realized_pnl']:+.2f}")
                        self._log(f"   Trading Fees: -${short_data['commission']:.2f}")
                        self._log(f"   Funding Fees: ${short_data['funding_fee']:+.2f}")
                        self._log(f"   Net PnL:      ${short_data['net_pnl']:+.2f}")
                        self._log("")
                        
                        # TOTALS
                        total = result['total']
                        pnl_status = "PROFIT" if total['net_pnl'] >= 0 else "LOSS"
                        self._log("-" * 50)
                        self._log(f"TOTAL REALIZED PnL: ${total['realized_pnl']:+.2f}")
                        self._log(f"TOTAL TRADING FEES: -${total['commission']:.2f}")
                        self._log(f"TOTAL FUNDING FEES: ${total['funding_fee']:+.2f}")
                        self._log(f">>> FINAL NET PnL: ${total['net_pnl']:+.2f} ({pnl_status}) <<<")
                        self._log("=" * 50)
                        
                        # Clear balance_before_open since it's no longer strictly needed, but kept for legacy cleanup
                        self.balance_before_open = {}
                    else:
                        self._log("Could not calculate final PnL")
                except Exception as e:
                    self._log(f"Error calculating PnL: {e}")
            
            self.root.after(0, show_result)
        
        # Run async calculation
        future = self._run_async(calculate_pnl())
        if future:
            future.add_done_callback(on_pnl_calculated)
    
    def _start_monitoring(self):
        """Start position monitoring"""
        if self.monitoring:
            return

        self.monitoring = True
        self._log("Started position monitoring")

        def safe_log(msg):
            self.root.after(0, lambda: self._log(msg))

        async def monitor_loop():
            safe_log("Monitor loop started...")

            while self.monitoring and self.active_position:
                try:
                    pos = self.active_position
                    snapshot = await self.service.build_monitor_snapshot(pos)
                    long_pnl = snapshot["long_pnl"]
                    short_pnl = snapshot["short_pnl"]
                    risk_percent = snapshot["risk_percent"]
                    total_balance = snapshot["total_balance"]

                    pos["long_pnl"] = long_pnl
                    pos["short_pnl"] = short_pnl

                    long_ex_name = pos["long_exchange"].value.capitalize()
                    short_ex_name = pos["short_exchange"].value.capitalize()

                    def update_label(risk=risk_percent, lp=long_pnl, sp=short_pnl, le=long_ex_name, se=short_ex_name):
                        color = 'red' if risk > 5 else ('orange' if risk > 2 else 'green')
                        self.pos_pnl_label.config(
                            text=f"Risk: {risk:.2f}% | Long({le}): ${lp:+.2f} | Short({se}): ${sp:+.2f}",
                            foreground=color
                        )

                    self.root.after(0, update_label)

                    if self.auto_close_risk_var.get():
                        try:
                            risk_threshold = float(self.risk_threshold_var.get())
                        except Exception:
                            risk_threshold = 10.0

                        bingx_pnl = 0.0
                        if pos['long_exchange'] == Exchange.BINGX:
                            bingx_pnl = long_pnl
                        elif pos['short_exchange'] == Exchange.BINGX:
                            bingx_pnl = short_pnl

                        effective_threshold = risk_threshold * 2 if bingx_pnl > 0 else risk_threshold
                        if bingx_pnl > 0:
                            safe_log(f"BingX is +${bingx_pnl:.2f} -> threshold x2: {effective_threshold:.1f}%")

                        if risk_percent >= effective_threshold:
                            safe_log(
                                f"Risk {risk_percent:.2f}% >= threshold {effective_threshold:.1f}% "
                                f"(balance ${total_balance:.2f})"
                            )
                            try:
                                splits = max(1, int(self.split_count_var.get()))
                            except Exception:
                                splits = 1

                            close_result = await self.service.close_position_with_analysis(
                                pair=pos['pair'],
                                long_exchange=pos['long_exchange'],
                                short_exchange=pos['short_exchange'],
                                splits=splits,
                                log_callback=safe_log,
                            )
                            if close_result.get("success"):
                                safe_log(f"Position closed due to risk >= {effective_threshold:.1f}%")
                                self.root.after(0, lambda r=risk_percent, t=effective_threshold: messagebox.showwarning(
                                    "Risk Auto-Close",
                                    f"Position closed!\nRisk was {r:.2f}% (threshold: {t:.1f}%)"
                                ))
                            else:
                                safe_log(f"Auto-close failed: {close_result.get('error')}")

                            self.monitoring = False
                            self.active_position = None
                            self.root.after(0, self._clear_position_display)
                            break

                    if self.auto_close_reversal_var.get() and 'initial_net_funding' in pos:
                        should_close, reason = await self._check_funding_reversal(pos)
                        if should_close:
                            safe_log(f"Funding reversal detected: {reason}")
                            try:
                                splits = max(1, int(self.split_count_var.get()))
                            except Exception:
                                splits = 1

                            close_result = await self.service.close_position_with_analysis(
                                pair=pos['pair'],
                                long_exchange=pos['long_exchange'],
                                short_exchange=pos['short_exchange'],
                                splits=splits,
                                log_callback=safe_log,
                            )
                            if close_result.get("success"):
                                safe_log(f"Position auto-closed due to: {reason}")
                                self.root.after(0, lambda r=reason: messagebox.showinfo("Auto-Close", f"Position closed:\n{r}"))
                            else:
                                safe_log(f"Auto-close failed: {close_result.get('error')}")

                            self.monitoring = False
                            self.active_position = None
                            self.root.after(0, self._clear_position_display)
                            break

                    await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Monitor error: {e}")
                    await asyncio.sleep(2)

        future = self._run_async(monitor_loop())
        if not future:
            self._log("Failed to schedule monitor loop - no event loop!")

    def _stop_monitoring(self):
        """Stop monitoring"""
        self.monitoring = False
        self._log("⏹ Monitoring stopped")
        self.monitor_btn.config(text="👁 Start Monitor")
    
    def _toggle_monitoring(self):
        """Toggle monitoring on/off"""
        if not self.active_position:
            messagebox.showwarning("Warning", "No active position to monitor")
            return
        
        if self.monitoring:
            self._stop_monitoring()
        else:
            self._start_monitoring()
            self.monitor_btn.config(text="⏹ Stop Monitor")
    
    async def _check_funding_reversal(self, position: Dict[str, Any]) -> tuple[bool, str]:
        """
        Check if funding rate has reversed and position should be closed
        Returns: (should_close: bool, reason: str)
        """
        try:
            # Get threshold setting
            try:
                min_threshold = float(self.min_spread_threshold.get()) / 100  # Convert % to decimal
            except:
                min_threshold = 0.0001  # Default 0.01%
            
            # Get initial funding data
            initial_net_funding = position.get('initial_net_funding', 0)
            initial_long_rate = position.get('initial_long_rate', 0)
            initial_short_rate = position.get('initial_short_rate', 0)
            
            # Get current funding rates
            long_ex = position['long_exchange']
            short_ex = position['short_exchange']
            pair = position['pair']
            
            current_rates = {}
            for ex in [long_ex, short_ex]:
                client = self.manager.clients.get(ex)
                if client:
                    try:
                        symbol = get_exchange_symbol(pair, ex)
                        funding_rate_obj = await client.get_funding_rate(symbol)
                        current_rates[ex] = funding_rate_obj.funding_rate
                    except Exception as e:
                        logger.debug(f"Could not get funding rate from {ex.value}: {e}")
                        return False, ""
            
            if len(current_rates) < 2:
                return False, ""
            
            current_long_rate = current_rates.get(long_ex, 0)
            current_short_rate = current_rates.get(short_ex, 0)
            current_net_funding = current_short_rate - current_long_rate
            
            # Log current vs initial
            logger.debug(f"Funding check - Initial: {initial_net_funding*100:.6f}%, Current: {current_net_funding*100:.6f}%")
            
            # Check 1: Direction reversal (sign change)
            if initial_net_funding != 0 and (initial_net_funding * current_net_funding) < 0:
                reason = (
                    f"Funding direction reversed\n"
                    f"Initial: {initial_net_funding*100:.6f}% "
                    f"({'positive' if initial_net_funding > 0 else 'negative'})\n"
                    f"Current: {current_net_funding*100:.6f}% "
                    f"({'positive' if current_net_funding > 0 else 'negative'})\n"
                    f"LONG {long_ex.value}: {initial_long_rate*100:.6f}% → {current_long_rate*100:.6f}%\n"
                    f"SHORT {short_ex.value}: {initial_short_rate*100:.6f}% → {current_short_rate*100:.6f}%"
                )
                return True, reason
            
            # Check 2: Spread too low (below threshold)
            if abs(current_net_funding) < min_threshold:
                reason = (
                    f"Spread dropped below threshold\n"
                    f"Current spread: {abs(current_net_funding)*100:.6f}%\n"
                    f"Threshold: {min_threshold*100:.6f}%\n"
                    f"Initial spread: {abs(initial_net_funding)*100:.6f}%\n"
                    f"LONG {long_ex.value}: {current_long_rate*100:.6f}%\n"
                    f"SHORT {short_ex.value}: {current_short_rate*100:.6f}%"
                )
                return True, reason
            
            # Check 3: Spread dropped significantly (>70% reduction)
            if abs(initial_net_funding) > 0:
                spread_reduction = 1 - (abs(current_net_funding) / abs(initial_net_funding))
                if spread_reduction > 0.97:  # 70% drop
                    reason = (
                        f"Spread dropped by {spread_reduction*100:.1f}%\n"
                        f"Initial: {abs(initial_net_funding)*100:.6f}%\n"
                        f"Current: {abs(current_net_funding)*100:.6f}%\n"
                        f"LONG {long_ex.value}: {initial_long_rate*100:.6f}% → {current_long_rate*100:.6f}%\n"
                        f"SHORT {short_ex.value}: {initial_short_rate*100:.6f}% → {current_short_rate*100:.6f}%"
                    )
                    return True, reason
            
            return False, ""
            
        except Exception as e:
            logger.error(f"Error checking funding reversal: {e}")
            return False, ""
    
    def _update_position_display(self):
        """Update position display"""
        if self.active_position:
            pos = self.active_position
            self.pos_pair_label.config(text=f"Pair: {pos['pair']}")
            self.pos_long_label.config(text=f"Long: {pos['long_exchange'].value}")
            self.pos_short_label.config(text=f"Short: {pos['short_exchange'].value}")
            self.pos_size_label.config(text=f"Size: {pos['size']}")
            self.pos_status_label.config(text="Status: OPEN", foreground='green')
            # Enable monitor button when position exists
            self.monitor_btn.config(state=tk.NORMAL)
    
    def _clear_position_display(self):
        """Clear position display"""
        self.pos_pair_label.config(text="Pair: -")
        self.pos_long_label.config(text="Long: -")
        self.pos_short_label.config(text="Short: -")
        self.pos_size_label.config(text="Size: -")
        self.pos_pnl_label.config(text="Risk: 0% | Long: $0 | Short: $0", foreground='black')
        self.pos_status_label.config(text="Status: No Position", foreground='black')
        # Disable monitor button and reset text
        self.monitor_btn.config(text="👁 Start Monitor", state=tk.DISABLED)
        
        # Restart pair info update loop when position is closed
        if not self._pair_info_update_task:
            self._update_pair_info()
    
    def _refresh_funding(self):
        """Refresh funding rates - scan ALL pairs from all exchanges"""
        if not self.connected:
            return

        self._log("Scanning ALL funding rates from all exchanges...")

        async def async_refresh():
            return await self.service.scan_funding_rates(top_candidates=20)

        future = self._run_async(async_refresh())
        if future:
            future.add_done_callback(self._on_funding_complete)

    def _on_funding_complete(self, future):
        """Handle funding refresh complete"""
        def update_ui():
            try:
                all_rates = future.result()
                self._update_funding_table(all_rates)
            except Exception as e:
                self._log(f"Error: {e}")
        
        # Run on main thread
        self.root.after(0, update_ui)
    
    def _update_funding_table(self, all_rates):
        """Update funding table - sorted by Best Spread (highest to lowest)
        
        All rates are normalized to 4h equivalent for fair comparison.
        Rows are ranked by fee-adjusted net edge rather than raw funding spread.
        """
        for item in self.funding_tree.get_children():
            self.funding_tree.delete(item)
        
        if not all_rates:
            self._log("No funding rates available")
            return
        
        # Cache the funding rates for later use
        self.cached_funding_rates = all_rates
        
        def normalize_to_4h(rate_obj):
            """Normalize a funding rate to 4h equivalent"""
            if not rate_obj:
                return None
            return normalize_funding_rate_obj(rate_obj)

        # Calculate fee-adjusted opportunities for sorting
        pairs_with_spreads = []
        for pair, rates in all_rates.items():
            metrics = build_best_opportunity(pair, rates)
            net_edge = metrics.net_edge_pct if metrics else float("-inf")
            pairs_with_spreads.append((pair, rates, metrics, net_edge))
        
        # Sort by net edge (highest to lowest), show top 10
        sorted_pairs = sorted(pairs_with_spreads, key=lambda x: x[3], reverse=True)
        top_pairs = sorted_pairs[:10]

        for pair, rates, metrics, _ in top_pairs:
            # Normalize each rate for display
            def fmt_rate(ex_enum):
                rate_obj = rates.get(ex_enum)
                if not rate_obj:
                    return "-"
                norm = normalize_to_4h(rate_obj)
                interval = getattr(rate_obj, 'funding_interval_hours', 8) or 8
                if interval != 4:
                    return f"{norm * 100:.6f}% ({interval}h)"
                return f"{norm * 100:.6f}%"
            
            okx_val = fmt_rate(Exchange.OKX)
            binance_val = fmt_rate(Exchange.BINANCE)
            bingx_val = fmt_rate(Exchange.BINGX)
            gate_val = fmt_rate(Exchange.GATE)
            aster_val = fmt_rate(Exchange.ASTERDEX)
            bybit_val = fmt_rate(Exchange.BYBIT)
            gross_edge = "-"
            cost = "-"
            net_edge = "-"
            recommendation = "-"
            
            if metrics:
                gross_edge = f"{metrics.gross_spread_pct:.6f}%"
                cost = f"{metrics.round_trip_cost_pct:.6f}%"
                net_edge = f"{metrics.net_edge_pct:.6f}%"
                recommendation = f"Long {metrics.long_exchange.value}, Short {metrics.short_exchange.value}"
            
            self.funding_tree.insert("", tk.END, values=(
                pair, okx_val, binance_val, bingx_val, gate_val, aster_val, bybit_val, gross_edge, cost, net_edge, recommendation
            ))
        
        self._log(f"Loaded top 10 from {len(sorted_pairs)} pairs sorted by net edge after estimated costs")
    
    def _on_funding_select(self, event):
        """Handle funding row double-click - auto select pair and exchanges in Trading Panel"""
        selected = self.funding_tree.selection()
        if not selected:
            return
            
        item = self.funding_tree.item(selected[0])
        values = item['values']
        pair = values[0]
        recommendation = values[-1]  # Luôn lấy từ cuối - không cần sửa khi thêm sàn mới
        
        # Set the pair in dropdown
        self.pair_combo.set(pair)
        self._log(f"📌 Selected pair: {pair}")
        
        # Parse and set exchanges from recommendation
        if recommendation != "-":
            # Parse recommendation like "Long okx, Short binance"
            parts = parse_recommendation_display_names(recommendation)
            if parts:
                long_display, short_display = parts
                
                self.long_exchange.set(long_display)
                self.short_exchange.set(short_display)
                
                self._log(f"✅ Auto-selected: LONG on {long_display}, SHORT on {short_display}")
        
        # Update pair info (this will fetch funding rates and prices)
        self._update_pair_info()
    
    def _on_pair_changed(self, event):
        """Handle pair dropdown selection change in Trading Panel"""
        self._update_pair_info()
        self._update_usdt_volume()
    
    def _on_exchange_changed(self, event):
        """Handle exchange dropdown selection change in Trading Panel"""
        self._update_pair_info()
        self._update_usdt_volume()
    
    def _update_usdt_volume(self, event=None):
        """Update USDT volume display based on size and current price"""
        # Cancel any pending update to prevent spam
        if hasattr(self, '_usdt_update_pending') and self._usdt_update_pending:
            self.root.after_cancel(self._usdt_update_pending)
            self._usdt_update_pending = None
        
        # Debounce - wait 500ms before actually fetching
        self._usdt_update_pending = self.root.after(500, self._do_update_usdt_volume)
    
    def _do_update_usdt_volume(self):
        """Actually update USDT volume (debounced)"""
        self._usdt_update_pending = None
        
        try:
            size = float(self.size_entry.get())
        except (ValueError, AttributeError):
            self.usdt_vol_label.config(text="≈ $0.00 USDT")
            return
        
        # Get cached price from any connected exchange
        pair = self.pair_combo.get()
        if not pair or not self.connected:
            self.usdt_vol_label.config(text="≈ $?.?? USDT")
            return
        
        # Fetch price async
        async def get_price():
            for exchange, client in self.manager.clients.items():
                try:
                    symbol = get_exchange_symbol(pair, exchange)
                    price = await client.get_mark_price(symbol)
                    return price
                except:
                    continue
            return None
        
        def on_price(future):
            try:
                price = future.result()
                if price:
                    usdt_value = size * price
                    self.root.after(0, lambda: self.usdt_vol_label.config(
                        text=f"≈ ${usdt_value:,.2f} USDT",
                        foreground="green" if usdt_value >= 10 else "orange"
                    ))
                else:
                    self.root.after(0, lambda: self.usdt_vol_label.config(text="≈ $?.?? USDT"))
            except:
                pass
        
        future = self._run_async(get_price())
        if future:
            future.add_done_callback(on_price)
    
    def _update_pair_info(self):
        """Update pair information display with real-time prices and timing"""
        pair = self.pair_combo.get()
        if not pair or not self.connected:
            return
        
        # Get current exchanges selection
        long_display = self.long_exchange.get()
        short_display = self.short_exchange.get()
        
        if not long_display or not short_display:
            self.selected_pair_info.config(text="Please select both LONG and SHORT exchanges", foreground="gray")
            return
        
        long_ex = from_display_name(long_display)
        short_ex = from_display_name(short_display)
        
        if not long_ex or not short_ex:
            return
        
        # Fetch real-time prices from the two selected exchanges
        self._fetch_pair_prices_for_display(pair, long_ex, short_ex, long_display, short_display)
    
    def _fetch_pair_prices_for_display(self, pair: str, long_ex: Exchange, short_ex: Exchange, 
                                        long_display: str, short_display: str):
        """Fetch real-time prices for the two selected exchanges and update display"""
        async def async_get_snapshot():
            return await self.service.get_pair_snapshot(pair, long_ex, short_ex)

        def on_snapshot_complete(future):
            try:
                snapshot = future.result(timeout=0.1)
                result_data = snapshot.get("result_data", {})
                funding_rates = snapshot.get("funding_rates", {})

                if len(result_data) < 2:
                    self.root.after(0, lambda: self.selected_pair_info.config(
                        text="Could not fetch prices from selected exchanges",
                        foreground="red",
                    ))
                    return

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
                if long_order_book and long_order_book.get('asks'):
                    for i, (price, qty) in enumerate(long_order_book['asks'][:3], start=1):
                        info_lines.append(f"  ASK{i}: ${price:,.6f} x {qty:.4f}")

                info_lines.extend([
                    "",
                    f"SHORT ({short_display}): ${short_price:,.6f} [{short_price_type}]",
                ])
                if short_order_book and short_order_book.get('bids'):
                    for i, (price, qty) in enumerate(short_order_book['bids'][:3], start=1):
                        info_lines.append(f"  BID{i}: ${price:,.6f} x {qty:.4f}")

                price_diff = snapshot.get("price_diff", 0.0)
                open_spread_pct = snapshot.get("open_spread_pct", 0.0)
                spread_sign = "+" if price_diff >= 0 else ""
                info_lines.extend([
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
                ])

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

                info_text = "\n".join(info_lines)
                self.root.after(0, lambda: self.selected_pair_info.config(
                    text=info_text,
                    foreground=entry_color,
                    font=('Courier', 9),
                ))

                if not self.active_position:
                    self._pair_info_update_task = self.root.after(2000, self._update_pair_info)

            except asyncio.TimeoutError:
                logger.warning("Timeout in on_complete getting future result")
            except Exception as e:
                logger.error(f"Error updating pair info: {e}")

        future = self._run_async(async_get_snapshot())
        if future:
            future.add_done_callback(on_snapshot_complete)

    def _update_pair_price_info(self, pair: str, recommendation: str = ""):
        """Fetch and display current price information for the selected pair"""
        if not self.connected:
            return
        
        async def async_get_price():
            prices = {}
            
            # Get price from each connected exchange
            for exchange, client in self.manager.clients.items():
                try:
                    symbol = get_exchange_symbol(pair, exchange)
                    mark_price = await client.get_mark_price(symbol)
                    prices[exchange] = mark_price
                except Exception as e:
                    logger.debug(f"Could not get price from {exchange.value}: {e}")
            
            return prices
        
        def on_complete(future):
            try:
                prices = future.result()
                if prices:
                    # Display average price or price from available exchanges
                    price_list = list(prices.values())
                    avg_price = sum(price_list) / len(price_list)
                    
                    # Build price info string
                    price_info = f"💲 ${avg_price:,.6f}"
                    
                    # Show individual exchange prices
                    price_details = " | ".join([f"{ex.value}: ${p:,.6f}" for ex, p in prices.items()])
                    
                    self.root.after(0, self._update_price_display, price_info, price_details)
                else:
                    self.root.after(0, self._update_price_display, "", "")
            except Exception as e:
                logger.error(f"Error getting prices: {e}")
                self.root.after(0, self._update_price_display, "", "")
        
        future = self._run_async(async_get_price())
        if future:
            future.add_done_callback(on_complete)
    
    def _update_price_display(self, price_info: str, price_details: str):
        """Update the price display labels"""
        self.current_price_label.config(text=price_info)
        if price_details:
            self._log(f"📊 Current prices: {price_details}")

    
    def _log(self, message: str):
        """Log message"""
        logger.info(message)
    
    def _clear_logs(self):
        """Clear logs"""
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)
    
    def _show_about(self):
        """Show about"""
        messagebox.showinfo("About", 
            "🎯 Funding Hunter v2.0\n\n"
            "Multi-Exchange Funding Arbitrage Tool\n\n"
            "Supported Exchanges:\n"
            "• OKX\n"
            "• Binance\n"
            "• BingX\n\n"
            "⚠️ USE AT YOUR OWN RISK!")
    
    def _on_closing(self):
        """Handle close"""
        if self.connected:
            if messagebox.askyesno("Exit", "Disconnect and exit?"):
                self._disconnect()
        
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        
        self.root.destroy()
    
    def run(self):
        """Run app"""
        self.root.mainloop()


def main():
    app = FundingHunterGUI()
    app.run()


if __name__ == "__main__":
    main()
