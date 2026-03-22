"""
Main GUI Application for Funding Hunter - Multi Exchange Support
Supports OKX, Binance, and BingX
"""
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import logging
import json
import os
import time
import subprocess
import aiohttp

from config.settings import settings


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

from config.constants import POPULAR_PAIRS, Side, Exchange, get_exchange_symbol, calculate_break_even, calculate_trade_quality, assess_price_divergence
from exchanges.okx_client import OKXClient
from exchanges.binance_client import BinanceClient
from exchanges.bingx_client import BingXClient
from exchanges.gate_client import GateClient
from exchanges.aster_client import AsterClient
from exchanges.base import BaseExchangeClient, FundingRate
from core.multi_exchange import MultiExchangeManager

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
        
        # Manager
        self.manager = MultiExchangeManager()
        
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
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Exchange credentials
        self._create_exchange_frame(main_frame)
        
        # Trading panel
        middle_frame = ttk.Frame(main_frame)
        middle_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        
        self._create_trading_frame(middle_frame)
        self._create_funding_frame(middle_frame)
        
        # Position and logs
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(fill=tk.BOTH, expand=True)
        
        self._create_position_frame(bottom_frame)
        self._create_log_frame(bottom_frame)
    
    def _create_exchange_frame(self, parent):
        """Create exchange credentials frame"""
        frame = ttk.LabelFrame(parent, text="🔌 Exchange Credentials", padding="10")
        frame.pack(fill=tk.X, pady=(0, 10))
        
        # Notebook for exchanges
        notebook = ttk.Notebook(frame)
        notebook.pack(fill=tk.X, pady=5)
        
        # OKX Tab
        okx_frame = ttk.Frame(notebook, padding="10")
        notebook.add(okx_frame, text="OKX")
        
        ttk.Label(okx_frame, text="API Key:").grid(row=0, column=0, padx=5, pady=2, sticky='e')
        self.okx_api_key = ttk.Entry(okx_frame, width=40, show="*")
        self.okx_api_key.grid(row=0, column=1, padx=5, pady=2)
        
        ttk.Label(okx_frame, text="Secret:").grid(row=0, column=2, padx=5, pady=2, sticky='e')
        self.okx_secret = ttk.Entry(okx_frame, width=40, show="*")
        self.okx_secret.grid(row=0, column=3, padx=5, pady=2)
        
        ttk.Label(okx_frame, text="Passphrase:").grid(row=0, column=4, padx=5, pady=2, sticky='e')
        self.okx_passphrase = ttk.Entry(okx_frame, width=20, show="*")
        self.okx_passphrase.grid(row=0, column=5, padx=5, pady=2)
        
        self.okx_testnet = tk.BooleanVar(value=True)
        ttk.Checkbutton(okx_frame, text="Testnet", variable=self.okx_testnet).grid(row=0, column=6, padx=10)
        
        self.okx_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(okx_frame, text="Enable", variable=self.okx_enabled).grid(row=0, column=7, padx=5)
        
        # Binance Tab
        binance_frame = ttk.Frame(notebook, padding="10")
        notebook.add(binance_frame, text="Binance")
        
        ttk.Label(binance_frame, text="API Key:").grid(row=0, column=0, padx=5, pady=2, sticky='e')
        self.binance_api_key = ttk.Entry(binance_frame, width=40, show="*")
        self.binance_api_key.grid(row=0, column=1, padx=5, pady=2)
        
        ttk.Label(binance_frame, text="Secret:").grid(row=0, column=2, padx=5, pady=2, sticky='e')
        self.binance_secret = ttk.Entry(binance_frame, width=40, show="*")
        self.binance_secret.grid(row=0, column=3, padx=5, pady=2)
        
        self.binance_testnet = tk.BooleanVar(value=True)
        ttk.Checkbutton(binance_frame, text="Testnet", variable=self.binance_testnet).grid(row=0, column=4, padx=10)
        
        self.binance_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(binance_frame, text="Enable", variable=self.binance_enabled).grid(row=0, column=5, padx=5)
        
        # BingX Tab
        bingx_frame = ttk.Frame(notebook, padding="10")
        notebook.add(bingx_frame, text="BingX")
        
        ttk.Label(bingx_frame, text="API Key:").grid(row=0, column=0, padx=5, pady=2, sticky='e')
        self.bingx_api_key = ttk.Entry(bingx_frame, width=40, show="*")
        self.bingx_api_key.grid(row=0, column=1, padx=5, pady=2)
        
        ttk.Label(bingx_frame, text="Secret:").grid(row=0, column=2, padx=5, pady=2, sticky='e')
        self.bingx_secret = ttk.Entry(bingx_frame, width=40, show="*")
        self.bingx_secret.grid(row=0, column=3, padx=5, pady=2)
        
        ttk.Label(bingx_frame, text="(No Testnet)", foreground='gray').grid(row=0, column=4, padx=10)
        
        self.bingx_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(bingx_frame, text="Enable", variable=self.bingx_enabled).grid(row=0, column=5, padx=5)
        
        # Gate.io Tab
        gate_frame = ttk.Frame(notebook, padding="10")
        notebook.add(gate_frame, text="Gate.io")
        
        ttk.Label(gate_frame, text="API Key:").grid(row=0, column=0, padx=5, pady=2, sticky='e')
        self.gate_api_key = ttk.Entry(gate_frame, width=40, show="*")
        self.gate_api_key.grid(row=0, column=1, padx=5, pady=2)
        
        ttk.Label(gate_frame, text="Secret:").grid(row=0, column=2, padx=5, pady=2, sticky='e')
        self.gate_secret = ttk.Entry(gate_frame, width=40, show="*")
        self.gate_secret.grid(row=0, column=3, padx=5, pady=2)
        
        ttk.Label(gate_frame, text="(No Testnet)", foreground='gray').grid(row=0, column=4, padx=10)
        
        self.gate_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(gate_frame, text="Enable", variable=self.gate_enabled).grid(row=0, column=5, padx=5)
        
        # Asterdex Tab
        asterdex_frame = ttk.Frame(notebook, padding="10")
        notebook.add(asterdex_frame, text="Asterdex")
        
        ttk.Label(asterdex_frame, text="API Key:").grid(row=0, column=0, padx=5, pady=2, sticky='e')
        self.asterdex_api_key = ttk.Entry(asterdex_frame, width=40, show="*")
        self.asterdex_api_key.grid(row=0, column=1, padx=5, pady=2)
        
        ttk.Label(asterdex_frame, text="Secret:").grid(row=0, column=2, padx=5, pady=2, sticky='e')
        self.asterdex_secret = ttk.Entry(asterdex_frame, width=40, show="*")
        self.asterdex_secret.grid(row=0, column=3, padx=5, pady=2)
        
        self.asterdex_testnet = tk.BooleanVar(value=False)
        ttk.Checkbutton(asterdex_frame, text="Testnet", variable=self.asterdex_testnet).grid(row=0, column=4, padx=10)
        
        self.asterdex_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(asterdex_frame, text="Enable", variable=self.asterdex_enabled).grid(row=0, column=5, padx=5)
        
        # Connection buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=10)
        
        self.connect_btn = ttk.Button(btn_frame, text="🔗 Connect All", command=self._connect)
        self.connect_btn.pack(side=tk.LEFT, padx=5)
        
        self.disconnect_btn = ttk.Button(btn_frame, text="🔌 Disconnect", command=self._disconnect, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=5)
        
        # Debug mode checkbox
        self.debug_mode = tk.BooleanVar(value=False)
        self.debug_checkbox = ttk.Checkbutton(btn_frame, text="🔍 Debug Mode", variable=self.debug_mode,
                                               command=self._on_debug_mode_changed)
        self.debug_checkbox.pack(side=tk.LEFT, padx=10)
        
        self.status_label = ttk.Label(btn_frame, text="⚪ Disconnected")
        self.status_label.pack(side=tk.LEFT, padx=20)
        
        # Balances
        self.balance_frame = ttk.Frame(btn_frame)
        self.balance_frame.pack(side=tk.RIGHT, padx=10)
        
        ttk.Label(self.balance_frame, text="OKX:").pack(side=tk.LEFT, padx=2)
        self.okx_balance_label = ttk.Label(self.balance_frame, text="$0.00")
        self.okx_balance_label.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(self.balance_frame, text="Binance:").pack(side=tk.LEFT, padx=2)
        self.binance_balance_label = ttk.Label(self.balance_frame, text="$0.00")
        self.binance_balance_label.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(self.balance_frame, text="BingX:").pack(side=tk.LEFT, padx=2)
        self.bingx_balance_label = ttk.Label(self.balance_frame, text="$0.00")
        self.bingx_balance_label.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(self.balance_frame, text="Gate:").pack(side=tk.LEFT, padx=2)
        self.gate_balance_label = ttk.Label(self.balance_frame, text="$0.00")
        self.gate_balance_label.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(self.balance_frame, text="Aster:").pack(side=tk.LEFT, padx=2)
        self.asterdex_balance_label = ttk.Label(self.balance_frame, text="$0.00")
        self.asterdex_balance_label.pack(side=tk.LEFT, padx=5)
    
    def _create_trading_frame(self, parent):
        """Create trading panel with scrollbar"""
        # Outer frame
        outer_frame = ttk.LabelFrame(parent, text="📊 Trading Panel", padding="5")
        outer_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        
        # Create canvas and scrollbar
        canvas = tk.Canvas(outer_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer_frame, orient="vertical", command=canvas.yview)
        
        # Scrollable frame inside canvas
        frame = ttk.Frame(canvas)
        frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Pack canvas and scrollbar
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Enable mousewheel scrolling only when mouse is over the canvas
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        
        def _bind_mousewheel(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
        
        def _unbind_mousewheel(event):
            canvas.unbind_all("<MouseWheel>")
        
        canvas.bind("<Enter>", _bind_mousewheel)
        canvas.bind("<Leave>", _unbind_mousewheel)
        
        # Trading pair
        pair_frame = ttk.Frame(frame)
        pair_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(pair_frame, text="Pair:").pack(side=tk.LEFT, padx=5)
        self.pair_combo = ttk.Combobox(pair_frame, values=POPULAR_PAIRS, width=15)
        self.pair_combo.set("BTC/USDT")
        self.pair_combo.pack(side=tk.LEFT, padx=5)
        # Bind event to update info when pair is changed
        self.pair_combo.bind('<<ComboboxSelected>>', self._on_pair_changed)
        
        # Current price display
        self.current_price_label = ttk.Label(pair_frame, text="", foreground="blue")
        self.current_price_label.pack(side=tk.LEFT, padx=10)
        
        # Button to load all Binance pairs
        self.load_pairs_btn = ttk.Button(pair_frame, text="📋 Load All Binance Pairs", 
                                         command=self._load_binance_pairs, state=tk.DISABLED)
        self.load_pairs_btn.pack(side=tk.LEFT, padx=5)
        
        # Second row: Leverage, Size, Splits
        settings_frame = ttk.Frame(frame)
        settings_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(settings_frame, text="Leverage:").pack(side=tk.LEFT, padx=5)
        self.leverage_var = tk.StringVar(value="3")
        self.leverage_spin = ttk.Spinbox(settings_frame, from_=1, to=100, width=5, textvariable=self.leverage_var)
        self.leverage_spin.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(settings_frame, text="Size:").pack(side=tk.LEFT, padx=10)
        self.size_entry = ttk.Entry(settings_frame, width=12)
        self.size_entry.insert(0, "0.01")
        self.size_entry.pack(side=tk.LEFT, padx=5)
        
        # USDT volume display (calculated from size * price)
        self.usdt_vol_label = ttk.Label(settings_frame, text="≈ $0.00 USDT", foreground="blue")
        self.usdt_vol_label.pack(side=tk.LEFT, padx=5)
        
        # Bind size entry to update USDT volume on change
        self.size_entry.bind('<KeyRelease>', self._update_usdt_volume)
        
        # Split count for DCA-style opening (user input)
        ttk.Label(settings_frame, text="Splits:").pack(side=tk.LEFT, padx=10)
        self.split_count_var = tk.StringVar(value="1")
        self.split_count_entry = ttk.Entry(settings_frame, textvariable=self.split_count_var, width=5)
        self.split_count_entry.pack(side=tk.LEFT, padx=5)
        
        # Exchange selection
        ex_frame = ttk.LabelFrame(frame, text="Select Exchanges for Arbitrage", padding="10")
        ex_frame.pack(fill=tk.X, pady=10)
        
        exchanges = ["OKX", "Binance", "BingX", "Gate.io"]
        
        ttk.Label(ex_frame, text="LONG Exchange:", style='Header.TLabel').grid(row=0, column=0, padx=5, pady=5, sticky='e')
        self.long_exchange = ttk.Combobox(ex_frame, values=exchanges, width=12, state='readonly')
        self.long_exchange.set("OKX")
        self.long_exchange.grid(row=0, column=1, padx=5, pady=5)
        self.long_exchange.bind('<<ComboboxSelected>>', self._on_exchange_changed)
        
        ttk.Label(ex_frame, text="SHORT Exchange:", style='Header.TLabel').grid(row=0, column=2, padx=15, pady=5, sticky='e')
        self.short_exchange = ttk.Combobox(ex_frame, values=exchanges, width=12, state='readonly')
        self.short_exchange.set("Binance")
        self.short_exchange.grid(row=0, column=3, padx=5, pady=5)
        self.short_exchange.bind('<<ComboboxSelected>>', self._on_exchange_changed)
        
        # Funding rate info for selected pair
        funding_info_frame = ttk.LabelFrame(frame, text="📊 Selected Pair Info", padding="10")
        funding_info_frame.pack(fill=tk.X, pady=10)
        
        self.selected_pair_info = ttk.Label(funding_info_frame, text="Double-click a pair in Funding Rates panel to see details", 
                                            foreground="gray", wraplength=400, justify=tk.LEFT)
        self.selected_pair_info.pack(fill=tk.X)
        
        # Action buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=15)
        
        self.open_btn = ttk.Button(btn_frame, text="🚀 Open Hedged Position", 
                                   command=self._open_position, state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        
        self.close_btn = ttk.Button(btn_frame, text="🛑 Close Position", 
                                    command=self._close_position, state=tk.DISABLED)
        self.close_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        
        self.load_pos_btn = ttk.Button(btn_frame, text="📥 Load Positions", 
                                       command=self._load_existing_positions, state=tk.DISABLED)
        self.load_pos_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        
        self.monitor_btn = ttk.Button(btn_frame, text="👁 Start Monitor", 
                                      command=self._toggle_monitoring, state=tk.DISABLED)
        self.monitor_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        
        # Auto close option
        option_frame = ttk.Frame(frame)
        option_frame.pack(fill=tk.X, pady=5)
        
        # Auto close on risk threshold
        risk_frame = ttk.Frame(option_frame)
        risk_frame.pack(anchor=tk.W)
        
        self.auto_close_risk_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(risk_frame, text="Auto-close when Risk >=", 
                       variable=self.auto_close_risk_var).pack(side=tk.LEFT)
        self.risk_threshold_var = tk.StringVar(value="10")
        ttk.Entry(risk_frame, textvariable=self.risk_threshold_var, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(risk_frame, text="%").pack(side=tk.LEFT)
        
        self.auto_close_reversal_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(option_frame, text="Auto-close on funding reversal", 
                       variable=self.auto_close_reversal_var).pack(anchor=tk.W, pady=2)
        
        self.skip_leverage_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(option_frame, text="Skip leverage set (faster entry)", 
                       variable=self.skip_leverage_var).pack(anchor=tk.W)

        self.skip_spread_check_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(option_frame, text="Skip spread check (execute splits immediately)", 
                       variable=self.skip_spread_check_var).pack(anchor=tk.W, pady=2)
        
        # Threshold settings
        threshold_frame = ttk.Frame(option_frame)
        threshold_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(threshold_frame, text="Price Spread Min:").pack(side=tk.LEFT)
        self.price_spread_threshold = ttk.Entry(threshold_frame, width=8)
        self.price_spread_threshold.insert(0, "0.05")  # 0.05% = 5 basis points
        self.price_spread_threshold.pack(side=tk.LEFT, padx=3)
        ttk.Label(threshold_frame, text="%").pack(side=tk.LEFT, padx=(0,10))
        
        ttk.Label(threshold_frame, text="Funding Spread Min:").pack(side=tk.LEFT, padx=(10,0))
        self.min_spread_threshold = ttk.Entry(threshold_frame, width=8)
        self.min_spread_threshold.insert(0, "0.01")  # 0.01% = 1 basis point
        self.min_spread_threshold.pack(side=tk.LEFT, padx=3)
        ttk.Label(threshold_frame, text="%").pack(side=tk.LEFT)
    
    def _create_funding_frame(self, parent):
        """Create funding rates panel"""
        frame = ttk.LabelFrame(parent, text="💰 Funding Rates Comparison", padding="10")
        frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=5)
        
        self.refresh_funding_btn = ttk.Button(btn_frame, text="🔄 Refresh Rates", 
                                              command=self._refresh_funding, state=tk.DISABLED)
        self.refresh_funding_btn.pack(side=tk.LEFT, padx=5)
        
        # Funding table
        columns = ("Pair", "OKX", "Binance", "BingX", "Gate", "Asterdex", "Best Spread", "Recommendation")
        self.funding_tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        
        self.funding_tree.heading("Pair", text="Pair")
        self.funding_tree.heading("OKX", text="OKX Rate")
        self.funding_tree.heading("Binance", text="Binance Rate")
        self.funding_tree.heading("BingX", text="BingX Rate")
        self.funding_tree.heading("Gate", text="Gate Rate")
        self.funding_tree.heading("Asterdex", text="Aster Rate")
        self.funding_tree.heading("Best Spread", text="Best Spread")
        self.funding_tree.heading("Recommendation", text="Recommendation")
        
        self.funding_tree.column("Pair", width=80)
        self.funding_tree.column("OKX", width=85)
        self.funding_tree.column("Binance", width=85)
        self.funding_tree.column("BingX", width=85)
        self.funding_tree.column("Gate", width=85)
        self.funding_tree.column("Asterdex", width=85)
        self.funding_tree.column("Best Spread", width=85)
        self.funding_tree.column("Recommendation", width=140)
        
        self.funding_tree.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.funding_tree.bind("<Double-1>", self._on_funding_select)
    
    def _create_position_frame(self, parent):
        """Create position display"""
        frame = ttk.LabelFrame(parent, text="📈 Active Position", padding="10")
        frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))
        
        # Position info
        self.position_info = ttk.Frame(frame)
        self.position_info.pack(fill=tk.X, pady=5)
        
        self.pos_pair_label = ttk.Label(self.position_info, text="Pair: -")
        self.pos_pair_label.pack(side=tk.LEFT, padx=10)
        
        self.pos_long_label = ttk.Label(self.position_info, text="Long: -")
        self.pos_long_label.pack(side=tk.LEFT, padx=10)
        
        self.pos_short_label = ttk.Label(self.position_info, text="Short: -")
        self.pos_short_label.pack(side=tk.LEFT, padx=10)
        
        self.pos_size_label = ttk.Label(self.position_info, text="Size: -")
        self.pos_size_label.pack(side=tk.LEFT, padx=10)
        
        self.pos_pnl_label = ttk.Label(self.position_info, text="Risk: 0% | Long: $0 | Short: $0")
        self.pos_pnl_label.pack(side=tk.LEFT, padx=10)
        
        self.pos_status_label = ttk.Label(self.position_info, text="Status: No Position")
        self.pos_status_label.pack(side=tk.RIGHT, padx=10)
    
    def _create_log_frame(self, parent):
        """Create log display"""
        frame = ttk.LabelFrame(parent, text="📝 Logs", padding="10")
        frame.pack(fill=tk.BOTH, expand=True)
        
        self.log_text = scrolledtext.ScrolledText(frame, height=8, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        ttk.Button(frame, text="Clear", command=self._clear_logs).pack(side=tk.RIGHT, pady=5)
    
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
            "Asterdex": Exchange.ASTERDEX
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
        connected = []
        
        # Sync Windows time trước khi connect
        success, msg = sync_windows_time()
        self._log(msg)
        
        # OKX
        if self.okx_enabled.get():
            okx_key = self.okx_api_key.get().strip()
            okx_secret = self.okx_secret.get().strip()
            okx_pass = self.okx_passphrase.get().strip()
            
            if okx_key and okx_secret and okx_pass:
                settings.okx.api_key = okx_key
                settings.okx.secret_key = okx_secret
                settings.okx.passphrase = okx_pass
                settings.okx.testnet = self.okx_testnet.get()
                
                client = OKXClient(settings.okx, debug=self.debug_mode.get())
                if await self.manager.connect_exchange(Exchange.OKX, client):
                    connected.append("OKX")
        
        # Binance
        if self.binance_enabled.get():
            binance_key = self.binance_api_key.get().strip()
            binance_secret = self.binance_secret.get().strip()
            
            if binance_key and binance_secret:
                settings.binance.api_key = binance_key
                settings.binance.secret_key = binance_secret
                settings.binance.testnet = self.binance_testnet.get()
                
                client = BinanceClient(settings.binance, debug=self.debug_mode.get())
                if await self.manager.connect_exchange(Exchange.BINANCE, client):
                    connected.append("Binance")
        
        # BingX
        if self.bingx_enabled.get():
            bingx_key = self.bingx_api_key.get().strip()
            bingx_secret = self.bingx_secret.get().strip()
            
            if bingx_key and bingx_secret:
                settings.bingx.api_key = bingx_key
                settings.bingx.secret_key = bingx_secret
                
                client = BingXClient(settings.bingx, debug=self.debug_mode.get())
                if await self.manager.connect_exchange(Exchange.BINGX, client):
                    connected.append("BingX")
        
        # Gate.io
        if self.gate_enabled.get():
            gate_key = self.gate_api_key.get().strip()
            gate_secret = self.gate_secret.get().strip()
            
            if gate_key and gate_secret:
                settings.gate.api_key = gate_key
                settings.gate.secret_key = gate_secret
                
                client = GateClient(settings.gate, debug=self.debug_mode.get())
                if await self.manager.connect_exchange(Exchange.GATE, client):
                    connected.append("Gate.io")
                    
        # Asterdex
        if self.asterdex_enabled.get():
            aster_key = self.asterdex_api_key.get().strip()
            aster_secret = self.asterdex_secret.get().strip()
            
            if aster_key and aster_secret:
                settings.asterdex.api_key = aster_key
                settings.asterdex.secret_key = aster_secret
                settings.asterdex.testnet = self.asterdex_testnet.get()
                
                client = AsterClient(settings.asterdex, debug=self.debug_mode.get())
                if await self.manager.connect_exchange(Exchange.ASTERDEX, client):
                    connected.append("Asterdex")
        
        # Get balances
        balances = await self.manager.get_all_balances()
        
        return connected, balances
    
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
    
    def _check_price_spread_and_open(self):
        """Check price spread and open position if threshold met"""
        if not self.waiting_for_price_spread or not self.price_spread_params:
            return
        
        params = self.price_spread_params
        
        # Thread-safe log callback - define ONCE here
        def safe_log(msg):
            self.root.after(0, lambda m=msg: self._log(m))
        
        async def check_and_open():
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
        logger.debug("_setup_position_tracking: ENTER")
        
        async def get_initial_funding():
            logger.debug("_setup_position_tracking.get_initial_funding: ENTER")
            rates = {}
            for ex in [long_ex, short_ex]:
                client = self.manager.clients.get(ex)
                if client:
                    try:
                        symbol = get_exchange_symbol(pair, ex)
                        funding_rate = await client.get_funding_rate(symbol)
                        rates[ex] = funding_rate.funding_rate
                    except Exception as e:
                        logger.debug(f"Could not get funding rate from {ex.value}: {e}")
            logger.debug("_setup_position_tracking.get_initial_funding: EXIT")
            return rates
        
        def on_funding_fetched(fut):
            logger.debug("_setup_position_tracking.on_funding_fetched: ENTER")
            def update_ui():
                logger.debug("_setup_position_tracking.update_ui: ENTER")
                try:
                    initial_rates = fut.result(timeout=0.1)  # Should already be done
                    long_rate = initial_rates.get(long_ex, 0)
                    short_rate = initial_rates.get(short_ex, 0)
                    initial_net_funding = short_rate - long_rate
                    
                    from datetime import datetime
                    self.active_position = {
                        "pair": pair,
                        "long_exchange": long_ex,
                        "short_exchange": short_ex,
                        "size": size,
                        "open_time": datetime.now(timezone.utc),
                        "total_funding_fees": 0.0,
                        "last_funding_check": datetime.now(timezone.utc),
                        # Funding reversal tracking
                        "initial_long_rate": long_rate,
                        "initial_short_rate": short_rate,
                        "initial_net_funding": initial_net_funding,
                    }
                    
                    self._log(f"📊 Initial Funding - LONG: {long_rate*100:.6f}%, SHORT: {short_rate*100:.6f}%, Net: {initial_net_funding*100:.6f}%")
                    self._update_position_display()
                    logger.debug("_setup_position_tracking.update_ui: EXIT")
                    
                except Exception as e:
                    logger.error(f"Error fetching initial funding: {e}")
            
            # Run on main thread
            self.root.after(0, update_ui)
            logger.debug("_setup_position_tracking.on_funding_fetched: EXIT")
        
        future = self._run_async(get_initial_funding())
        if future:
            future.add_done_callback(on_funding_fetched)
        logger.debug("_setup_position_tracking: EXIT")
    
    def _on_open_complete(self, future, pair, long_ex, short_ex, size):
        """Handle open complete"""
        def update_ui():
            self.open_btn.config(state=tk.NORMAL)
            
            try:
                result = future.result()
                if result["success"]:
                    self._log("✅ Position opened successfully!")
                    
                    # Get initial funding rates
                    async def get_initial_funding():
                        rates = {}
                        for ex in [long_ex, short_ex]:
                            client = self.manager.clients.get(ex)
                            if client:
                                try:
                                    symbol = get_exchange_symbol(pair, ex)
                                    funding_rate = await client.get_funding_rate(symbol)
                                    rates[ex] = funding_rate.funding_rate
                                except Exception as e:
                                    logger.debug(f"Could not get funding rate from {ex.value}: {e}")
                        return rates
                    
                    def on_funding_fetched(fut):
                        def inner_update():
                            try:
                                initial_rates = fut.result()
                                long_rate = initial_rates.get(long_ex, 0)
                                short_rate = initial_rates.get(short_ex, 0)
                                initial_net_funding = short_rate - long_rate
                                
                                from datetime import datetime
                                self.active_position = {
                                    "pair": pair,
                                    "long_exchange": long_ex,
                                    "short_exchange": short_ex,
                                    "size": size,
                                    "open_time": datetime.now(timezone.utc),
                                    "total_funding_fees": 0.0,
                                    "last_funding_check": datetime.now(timezone.utc),
                                    # Funding reversal tracking
                                    "initial_long_rate": long_rate,
                                    "initial_short_rate": short_rate,
                                    "initial_net_funding": initial_net_funding,
                                }
                                
                                self._log(f"📊 Initial Funding - LONG: {long_rate*100:.6f}%, SHORT: {short_rate*100:.6f}%, Net: {initial_net_funding*100:.6f}%")
                                self._update_position_display()

                                    
                            except Exception as e:
                                logger.error(f"Error tracking initial funding: {e}")
                        
                        self.root.after(0, inner_update)
                    
                    # Fetch initial funding rates
                    funding_future = self._run_async(get_initial_funding())
                    if funding_future:
                        funding_future.add_done_callback(on_funding_fetched)
                    else:
                        # Fallback if async fails
                        from datetime import datetime
                        self.active_position = {
                            "pair": pair,
                            "long_exchange": long_ex,
                            "short_exchange": short_ex,
                            "size": size,
                            "open_time": datetime.now(timezone.utc),
                            "total_funding_fees": 0.0,
                            "last_funding_check": datetime.now(timezone.utc),
                            "initial_long_rate": 0,
                            "initial_short_rate": 0,
                            "initial_net_funding": 0,
                        }
                        self._update_position_display()

                else:
                    self._log(f"❌ Failed: {result['error']}")
                    messagebox.showerror("Error", result['error'])
            except Exception as e:
                self._log(f"Error: {e}")
        
        # Run on main thread
        self.root.after(0, update_ui)
    
    def _close_position(self):
        """Close position - analyze spread first, then close in splits"""
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
        
        if skip_spread:
            confirm_msg = f"Close position in {splits} split(s)?\nWill skip spread check and close immediately."
        else:
            confirm_msg = f"Close position in {splits} split(s)?\nWill analyze spread 2 minutes first."
            
        if not messagebox.askyesno("Confirm", confirm_msg):
            return
        
        pos = self.active_position
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
            "price_spread_min": -100.0 if skip_spread else None,  # Will be set after analyze
            "skip_spread_check": skip_spread
        }
        
        # Subscribe to WS Market Data
        self._run_async(self.manager.subscribe_market_data(pos['pair'], pos['long_exchange'], pos['short_exchange']))
        
        if skip_spread:
            self._log(f"⚡ Bỏ qua analyze spread, tiến hành đóng lệnh ngay cho {pos['pair']}...")
            self._execute_close_with_splits()
        else:
            self._log(f"📊 Bắt đầu analyze spread 2 phút cho CLOSE...")
            # Start analyze for close
            self._start_analyze_for_close()
    
    def _start_analyze_for_close(self):
        """Run analyze spread and then start closing"""
        params = self.close_params
        if not params:
            return
        
        def safe_log(msg):
            self.root.after(0, lambda m=msg: self._log(m))
        
        async def do_analyze():
            result = await self.manager.analyze_spread(
                params["pair"],
                params["long_exchange"],
                params["short_exchange"],
                duration_seconds=120.0,  # 2 phút
                check_interval=2.0,      # 2 giây
                log_callback=safe_log,
                cancel_event=self.close_cancel_event,
                mode="close"  # Mode đóng: LONG_BID - SHORT_ASK
            )
            return result
        
        future = self._run_async(do_analyze())
        if future:
            future.add_done_callback(lambda f: self._on_analyze_for_close_complete(f))
    
    def _on_analyze_for_close_complete(self, future):
        """Handle analyze completion for close"""
        def update_ui():
            try:
                result = future.result(timeout=1.0)
                self.analyzing_close = False
                
                if not result.get("success"):
                    error = result.get("error", "Unknown error")
                    self._log(f"❌ Analyze thất bại: {error}")
                    self._reset_close_button()
                    return
                
                # Get second best spread as threshold
                second_best = result["second_best_spread"]
                self.close_params["price_spread_min"] = second_best
                
                self._log(f"✅ Analyze xong! Threshold = {second_best:.4f}% (giá trị cao thứ 2)")
                self._log(f"⏳ Đang chờ spread >= {second_best:.4f}% để đóng position...")
                
                # Update button to show waiting/closing state
                self.close_btn.configure(text="⏳ Closing... (Cancel)")
                
                # Now start closing with splits
                self.waiting_for_close = True
                self._execute_close_with_splits()
                
            except Exception as e:
                self._log(f"❌ Lỗi analyze: {e}")
                self._reset_close_button()
        
        self.root.after(0, update_ui)
    
    def _execute_close_with_splits(self):
        """Execute the actual close with splits after analyze"""
        params = self.close_params
        if not params:
            return
        
        # Create cancel event for splits
        self.split_cancel_event = threading.Event()
        
        def progress_callback(split_num, total_splits, message):
            self.root.after(0, lambda: self._log(f"   {message}"))
        
        async def async_close():
            return await self.manager.close_hedged_position_split(
                params['pair'], 
                params['long_exchange'], 
                params['short_exchange'],
                splits=params['splits'],
                interval_seconds=2.0,
                price_spread_min=params['price_spread_min'],
                spread_check_interval=2.0,
                progress_callback=progress_callback,
                cancel_event=self.split_cancel_event,
                skip_spread_check=params.get('skip_spread_check', False)
            )
        
        future = self._run_async(async_close())
        if future:
            future.add_done_callback(self._on_close_complete)
    
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
        """Find matching hedged pairs from position list"""
        from config.constants import get_unified_pair, Side
        
        hedged_pairs = []
        used_positions = set()
        
        for i, p1 in enumerate(positions):
            if i in used_positions:
                continue
            
            # Try to find opposite position on another exchange
            for j, p2 in enumerate(positions):
                if j in used_positions or j == i:
                    continue
                if p1['exchange'] == p2['exchange']:
                    continue
                
                # Check if same symbol and opposite sides
                # Need to convert symbols to unified format for comparison
                try:
                    pair1 = get_unified_pair(p1['symbol'], p1['exchange'])
                    pair2 = get_unified_pair(p2['symbol'], p2['exchange'])
                except:
                    continue
                
                if pair1 == pair2 and p1['side'] != p2['side']:
                    # Found a hedged pair!
                    long_pos = p1 if p1['side'] == Side.LONG else p2
                    short_pos = p2 if p1['side'] == Side.LONG else p1
                    
                    hedged_pairs.append({
                        "pair": pair1,
                        "long": long_pos,
                        "short": short_pos,
                        "total_pnl": p1['unrealized_pnl'] + p2['unrealized_pnl']
                    })
                    used_positions.add(i)
                    used_positions.add(j)
                    break
        
        return hedged_pairs
    
    def _show_position_selector(self, hedged_pairs, all_positions):
        """Show dialog to select position to load"""
        from datetime import timedelta
        
        # Create selection dialog
        dialog = tk.Toplevel(self.root)
        dialog.title("Load Position")
        dialog.geometry("600x500")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="Select a position to monitor:", font=('Arial', 11, 'bold')).pack(pady=10)
        
        # Listbox for positions
        listbox = tk.Listbox(dialog, width=80, height=12, font=('Courier', 9))
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        position_data = []
        
        # Add hedged pairs first
        if hedged_pairs:
            listbox.insert(tk.END, "=== HEDGED PAIRS ===")
            position_data.append(None)  # Separator
            
            for hp in hedged_pairs:
                text = f"  {hp['pair']}: LONG {hp['long']['exchange'].value} / SHORT {hp['short']['exchange'].value} | PnL: ${hp['total_pnl']:.2f}"
                listbox.insert(tk.END, text)
                position_data.append({"type": "hedged", "data": hp})
        
        # Add individual positions
        listbox.insert(tk.END, "")
        position_data.append(None)
        listbox.insert(tk.END, "=== INDIVIDUAL POSITIONS ===")
        position_data.append(None)
        
        for p in all_positions:
            text = f"  {p['exchange'].value}: {p['symbol']} {p['side'].value.upper()} Size: {p['size']:.4f} PnL: ${p['unrealized_pnl']:.2f}"
            listbox.insert(tk.END, text)
            position_data.append({"type": "single", "data": p})
        
        # Add position open time frame
        ttk.Separator(dialog, orient='horizontal').pack(fill='x', padx=10, pady=10)
        
        time_frame = ttk.LabelFrame(dialog, text="Position Open Time (for funding fee calculation)")
        time_frame.pack(fill='x', padx=10, pady=5)
        
        # Time selection options
        time_option = tk.StringVar(value="lookback_24h")
        
        ttk.Radiobutton(time_frame, text="Opened within last 24 hours (default)", 
                        variable=time_option, value="lookback_24h").pack(anchor='w', padx=10, pady=2)
        ttk.Radiobutton(time_frame, text="Opened within last 48 hours", 
                        variable=time_option, value="lookback_48h").pack(anchor='w', padx=10, pady=2)
        ttk.Radiobutton(time_frame, text="Opened within last 7 days", 
                        variable=time_option, value="lookback_7d").pack(anchor='w', padx=10, pady=2)
        
        # Custom datetime option
        custom_frame = ttk.Frame(time_frame)
        custom_frame.pack(anchor='w', padx=10, pady=2)
        
        ttk.Radiobutton(custom_frame, text="Custom date/time (UTC):", 
                        variable=time_option, value="custom").pack(side='left')
        
        # Date entry (YYYY-MM-DD HH:MM format)
        custom_datetime = tk.StringVar(value=(datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M"))
        custom_entry = ttk.Entry(custom_frame, textvariable=custom_datetime, width=20)
        custom_entry.pack(side='left', padx=5)
        ttk.Label(custom_frame, text="(YYYY-MM-DD HH:MM)").pack(side='left')
        
        def get_open_time():
            """Get the open time based on selection"""
            option = time_option.get()
            now = datetime.now(timezone.utc)
            
            if option == "lookback_24h":
                return now - timedelta(hours=24)
            elif option == "lookback_48h":
                return now - timedelta(hours=48)
            elif option == "lookback_7d":
                return now - timedelta(days=7)
            elif option == "custom":
                try:
                    dt_str = custom_datetime.get()
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                    return dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    messagebox.showerror("Error", "Invalid date format. Use YYYY-MM-DD HH:MM")
                    return None
            return now - timedelta(hours=24)  # Default fallback
        
        def on_load():
            selection = listbox.curselection()
            if not selection:
                messagebox.showwarning("Warning", "Please select a position")
                return
            
            idx = selection[0]
            selected = position_data[idx]
            
            if selected is None:
                messagebox.showwarning("Warning", "Please select a valid position, not a separator")
                return
            
            open_time = get_open_time()
            if open_time is None:
                return  # Error in date parsing
            
            dialog.destroy()
            
            if selected["type"] == "hedged":
                self._load_hedged_position(selected["data"], open_time)
            else:
                self._log(f"Single position selected - cannot monitor as hedged pair")
                self._log(f"   {selected['data']['exchange'].value}: {selected['data']['symbol']} {selected['data']['side'].value}")
        
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=10)
        
        ttk.Button(btn_frame, text="Load & Monitor", command=on_load).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=5)
    
    def _load_hedged_position(self, hedged_pair, open_time: Optional[datetime] = None):
        """Load a hedged position and start monitoring
        
        Args:
            hedged_pair: Dict containing pair info (long/short positions)
            open_time: When the position was opened (for funding fee calculation).
                       If None, defaults to 24 hours ago.
        """
        from datetime import timedelta
        
        pair = hedged_pair["pair"]
        long_pos = hedged_pair["long"]
        short_pos = hedged_pair["short"]
        
        long_ex = long_pos["exchange"]
        short_ex = short_pos["exchange"]
        
        # Use average size (should be similar for hedged positions)
        size = (long_pos["size"] + short_pos["size"]) / 2
        
        # Use provided open_time or default to 24 hours ago
        if open_time is None:
            open_time = datetime.now(timezone.utc) - timedelta(hours=24)
        
        self._log(f"Loading hedged position: {pair}")
        self._log(f"   LONG: {long_ex.value} Size: {long_pos['size']}")
        self._log(f"   SHORT: {short_ex.value} Size: {short_pos['size']}")
        self._log(f"   Open time (for funding): {open_time.strftime('%Y-%m-%d %H:%M')} UTC")
        
        # Setup position tracking
        self.active_position = {
            "pair": pair,
            "long_exchange": long_ex,
            "short_exchange": short_ex,
            "size": size,
            "open_time": open_time,  # Use provided/calculated open time
            "total_funding_fees": 0.0,
            "last_funding_check": datetime.now(timezone.utc),
            "initial_long_rate": 0,
            "initial_short_rate": 0,
            "initial_net_funding": 0,
            "loaded_position": True  # Mark as loaded (not opened by us)
        }
        
        # Update UI
        self._update_position_display()
        
        # Update exchange selectors
        exchange_name_map = {Exchange.OKX: "OKX", Exchange.BINANCE: "Binance", Exchange.BINGX: "BingX", Exchange.GATE: "Gate.io", Exchange.ASTERDEX: "Asterdex"}
        self.long_exchange.set(exchange_name_map.get(long_ex, ""))
        self.short_exchange.set(exchange_name_map.get(short_ex, ""))
        
        # Set pair in combo (add it if not present)
        current_values = list(self.pair_combo['values'])
        if pair not in current_values:
            current_values.append(pair)
            self.pair_combo['values'] = current_values
        self.pair_combo.set(pair)
        
        self._log(f"✅ Position loaded! You can now monitor or close it.")


    def _on_close_complete(self, future):
        """Handle close complete"""
        # Save position info before clearing for PnL calculation
        closed_position = self.active_position.copy() if self.active_position else None
        
        def update_ui():
            self.waiting_for_close = False
            self.close_btn.configure(text="Close Position", state=tk.NORMAL)
            self.open_btn.configure(state=tk.NORMAL)
            
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
        
        # Thread-safe log for async context
        def safe_log(msg):
            self.root.after(0, lambda: self._log(msg))
        
        async def monitor_loop():
            safe_log("📊 Monitor loop started...")
            
            # Get account balance for risk calculation
            total_balance = 0.0
            for ex, client in self.manager.clients.items():
                try:
                    balance = await client.get_balance()
                    if balance:
                        total_balance += balance.available
                except:
                    pass
            if total_balance <= 0:
                total_balance = 1000.0  # Fallback to avoid division by zero
            safe_log(f"📊 Total balance for risk calc: ${total_balance:.2f}")
            
            while self.monitoring and self.active_position:
                try:
                    pos = self.active_position
                    result = await self.manager.check_positions(
                        pos['pair'], pos['long_exchange'], pos['short_exchange']
                    )
                    
                    # Get unrealized PnL from each side
                    long_pnl = result['long'].unrealized_pnl if result['long'] else 0
                    short_pnl = result['short'].unrealized_pnl if result['short'] else 0
                    
                    # Update active_position with latest data
                    pos['long_pnl'] = long_pnl
                    pos['short_pnl'] = short_pnl
                    
                    # Calculate % Risk = |negative PnL| / Balance * 100
                    # Only count the losing side for risk
                    losing_pnl = 0.0
                    if long_pnl < 0:
                        losing_pnl += abs(long_pnl)
                    if short_pnl < 0:
                        losing_pnl += abs(short_pnl)
                    risk_percent = (losing_pnl / total_balance) * 100
                    
                    # Get exchange names
                    long_ex_name = pos['long_exchange'].value.capitalize()
                    short_ex_name = pos['short_exchange'].value.capitalize()
                    
                    # Update display: Risk% | Long(Exchange): $X | Short(Exchange): $X
                    def update_label(risk=risk_percent, lp=long_pnl, sp=short_pnl, le=long_ex_name, se=short_ex_name):
                        # Color based on which side is losing more
                        color = 'red' if risk > 5 else ('orange' if risk > 2 else 'green')
                        self.pos_pnl_label.config(
                            text=f"Risk: {risk:.2f}% | Long({le}): ${lp:+.2f} | Short({se}): ${sp:+.2f}",
                            foreground=color
                        )
                    self.root.after(0, update_label)
                    
                    # Check auto-close on risk threshold
                    if self.auto_close_risk_var.get():
                        try:
                            risk_threshold = float(self.risk_threshold_var.get())
                        except:
                            risk_threshold = 10.0
                        
                        # Check if BingX is profitable - if so, double the threshold
                        # to let BingX win more before closing
                        bingx_pnl = 0.0
                        if pos['long_exchange'] == Exchange.BINGX:
                            bingx_pnl = long_pnl
                        elif pos['short_exchange'] == Exchange.BINGX:
                            bingx_pnl = short_pnl
                        
                        effective_threshold = risk_threshold
                        if bingx_pnl > 0:
                            # BingX đang lời → cho phép risk gấp đôi để BingX thắng nhiều hơn
                            effective_threshold = risk_threshold * 2
                            safe_log(f"💰 BingX đang +${bingx_pnl:.2f} → threshold x2: {effective_threshold:.1f}%")
                        
                        if risk_percent >= effective_threshold:
                            safe_log(f"🚨 Risk {risk_percent:.2f}% >= threshold {effective_threshold:.1f}%!")
                            safe_log(f"🔄 Bắt đầu analyze spread 2 phút trước khi close...")
                            
                            # Analyze spread first
                            try:
                                analyze_result = await self.manager.analyze_spread(
                                    pos['pair'],
                                    pos['long_exchange'],
                                    pos['short_exchange'],
                                    duration_seconds=120.0,  # 2 phút
                                    check_interval=2.0,
                                    log_callback=safe_log,
                                    cancel_event=None,
                                    mode="close"
                                )
                                
                                if not analyze_result.get("success"):
                                    safe_log(f"❌ Analyze thất bại: {analyze_result.get('error')}")
                                    price_spread_threshold = -100.0  # Fallback
                                else:
                                    price_spread_threshold = analyze_result["second_best_spread"]
                                    safe_log(f"✅ Analyze xong! Threshold = {price_spread_threshold:.4f}%")
                                
                                # Close with spread check + reduce threshold after 30 failed checks
                                safe_log(f"🔄 Đang chờ spread >= {price_spread_threshold:.4f}% để close...")
                                
                                check_count = 0
                                current_threshold = price_spread_threshold
                                
                                while True:
                                    # Get current spread for close (LONG_BID - SHORT_ASK)
                                    try:
                                        long_client = self.manager.clients.get(pos['long_exchange'])
                                        short_client = self.manager.clients.get(pos['short_exchange'])
                                        long_symbol = get_exchange_symbol(pos['pair'], pos['long_exchange'])
                                        short_symbol = get_exchange_symbol(pos['pair'], pos['short_exchange'])
                                        
                                        long_book = await long_client.get_order_book(long_symbol)
                                        short_book = await short_client.get_order_book(short_symbol)
                                        
                                        long_bid = float(long_book['bids'][0][0])
                                        short_ask = float(short_book['asks'][0][0])
                                        mid_price = (long_bid + short_ask) / 2
                                        spread_pct = ((long_bid - short_ask) / mid_price) * 100
                                        
                                        if spread_pct >= current_threshold:
                                            safe_log(f"✅ Spread {spread_pct:.4f}% >= {current_threshold:.4f}% OK! Closing...")
                                            break
                                        
                                        check_count += 1
                                        
                                        # After 30 checks, reduce threshold by 0.01%
                                        if check_count >= 30:
                                            old_threshold = current_threshold
                                            current_threshold -= 0.01
                                            safe_log(f"⚠️ 30 lần chưa đạt, giảm threshold: {old_threshold:.4f}% → {current_threshold:.4f}%")
                                            check_count = 0
                                        
                                        await asyncio.sleep(2)
                                        
                                    except Exception as e:
                                        safe_log(f"⚠️ Spread check error: {e}, retrying...")
                                        await asyncio.sleep(2)
                                
                                # Now close position with splits
                                # Get split count from UI
                                try:
                                    splits = int(self.split_count_var.get())
                                    if splits < 1: splits = 1
                                except:
                                    splits = 1
                                
                                safe_log(f"🔄 Closing position in {splits} split(s)...")
                                close_result = await self.manager.close_hedged_position_split(
                                    pos['pair'], 
                                    pos['long_exchange'], 
                                    pos['short_exchange'],
                                    splits=splits,
                                    interval_seconds=2.0,
                                    price_spread_min=current_threshold,
                                    spread_check_interval=2.0,
                                    progress_callback=lambda s, t, m: safe_log(f"   {m}"),
                                    cancel_event=None
                                )
                                
                                if close_result.get("success"):
                                    safe_log(f"✅ Position closed do risk >= {effective_threshold:.1f}%")
                                    self.root.after(0, lambda r=risk_percent, t=effective_threshold: messagebox.showwarning(
                                        "Risk Auto-Close", 
                                        f"Position closed!\nRisk was {r:.2f}% (threshold: {t:.1f}%)"
                                    ))
                                else:
                                    safe_log(f"❌ Auto-close failed: {close_result.get('error')}")
                                    
                            except Exception as e:
                                safe_log(f"❌ Auto-close error: {e}")
                            
                            self.monitoring = False
                            self.active_position = None
                            self.root.after(0, self._clear_position_display)
                            break
                    
                    # Check funding reversal (if enabled)
                    if self.auto_close_reversal_var.get() and 'initial_net_funding' in pos:
                        should_close, reason = await self._check_funding_reversal(pos)
                        if should_close:
                            safe_log(f"🔄 Funding reversal detected: {reason}")
                            safe_log(f"🔄 Bắt đầu analyze spread 5 phút trước khi auto-close...")
                            
                            # Get split count from UI
                            try:
                                splits = int(self.split_count_var.get())
                                if splits < 1: splits = 1
                            except:
                                splits = 1
                            
                            # Analyze spread first
                            try:
                                analyze_result = await self.manager.analyze_spread(
                                    pos['pair'],
                                    pos['long_exchange'],
                                    pos['short_exchange'],
                                    duration_seconds=120.0,  # 2 phút
                                    check_interval=2.0,
                                    log_callback=safe_log,
                                    cancel_event=None,
                                    mode="close"
                                )
                                
                                if not analyze_result.get("success"):
                                    safe_log(f"❌ Analyze thất bại: {analyze_result.get('error')}")
                                    # Still try to close with default threshold
                                    price_spread_min = -100.0
                                else:
                                    price_spread_min = analyze_result["second_best_spread"]
                                    safe_log(f"✅ Analyze xong! Threshold = {price_spread_min:.4f}%")
                                
                                # Close position with splits
                                safe_log(f"🔄 Auto-closing position in {splits} split(s)...")
                                close_result = await self.manager.close_hedged_position_split(
                                    pos['pair'], 
                                    pos['long_exchange'], 
                                    pos['short_exchange'],
                                    splits=splits,
                                    interval_seconds=2.0,
                                    price_spread_min=price_spread_min,
                                    spread_check_interval=2.0,
                                    progress_callback=lambda s, t, m: safe_log(f"   {m}"),
                                    cancel_event=None
                                )
                                
                                if close_result.get("success"):
                                    safe_log(f"✅ Position auto-closed due to: {reason}")
                                    self.root.after(0, lambda r=reason: messagebox.showinfo(
                                        "Auto-Close", 
                                        f"Position closed:\n{r}"
                                    ))
                                    
                                    # If auto trading is enabled, it will resume scanning
                                    if self.auto_trading_active:
                                        safe_log(f"🤖 Auto Trading: Resuming signal scanning...")
                                else:
                                    safe_log(f"❌ Auto-close failed: {close_result.get('error')}")
                                    
                            except Exception as e:
                                safe_log(f"❌ Auto-close error: {e}")
                            
                            self.monitoring = False
                            self.active_position = None
                            self.root.after(0, self._clear_position_display)
                            break
                    
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    logger.error(f"Monitor error: {e}")
                    # Don't spam GUI with errors, just log to console
                    await asyncio.sleep(2)
        
        # Debug log for scheduling
        logger.debug(f"Scheduling monitor loop... (loop exists: {self.loop is not None})")
        future = self._run_async(monitor_loop())
        if not future:
            self._log("❌ Failed to schedule monitor loop - no event loop!")
    
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
    
    async def _get_funding_fees(self, position: Dict[str, Any]) -> float:
        """Get total funding fees for the position from both exchanges"""
        total_funding = 0.0
        open_time = position.get('open_time')
        if not open_time:
            logger.debug("No open_time in position, cannot calculate funding fees")
            return 0.0
        
        # Convert to timestamp in milliseconds
        start_time = int(open_time.timestamp() * 1000)
        pair = position.get('pair', 'Unknown')
        
        # Get funding fees from long exchange
        long_client = self.manager.clients.get(position['long_exchange'])
        long_funding = 0.0
        if long_client:
            try:
                symbol = get_exchange_symbol(position['pair'], position['long_exchange'])
                
                # Call get_income_history based on exchange type
                from exchanges.binance_client import BinanceClient
                from exchanges.okx_client import OKXClient
                from exchanges.bingx_client import BingXClient
                from exchanges.gate_client import GateClient
                
                if isinstance(long_client, BinanceClient):
                    # Binance format: BTCUSDT
                    binance_symbol = symbol.replace('/', '')
                    income = await long_client.get_income_history("FUNDING_FEE", limit=100, symbol=binance_symbol)
                    for item in income:
                        item_time = item.get('time', 0)
                        if item_time >= start_time:
                            long_funding += float(item.get('income', 0))
                
                elif isinstance(long_client, OKXClient):
                    # OKX format: BTC-USDT-SWAP
                    income = await long_client.get_income_history(symbol, limit=100)
                    for item in income:
                        item_time = item.get('time', 0)
                        if item_time >= start_time:
                            long_funding += float(item.get('income', 0))
                
                elif isinstance(long_client, BingXClient):
                    # BingX format: BTC-USDT
                    income = await long_client.get_income_history(symbol, limit=100)
                    for item in income:
                        item_time = item.get('time', 0)
                        if item_time >= start_time:
                            long_funding += float(item.get('income', 0))
                
                elif isinstance(long_client, GateClient):
                    # Gate.io format: BTC_USDT
                    income = await long_client.get_income_history(symbol, limit=100, income_type="FUNDING_FEE")
                    for item in income:
                        item_time = item.get('time', 0)
                        if item_time >= start_time:
                            long_funding += float(item.get('income', 0))
                
                logger.debug(f"LONG {position['long_exchange'].value} funding: ${long_funding:.6f}")
                            
            except Exception as e:
                logger.debug(f"Error getting funding from {position['long_exchange'].value}: {e}")
        
        # Get funding fees from short exchange
        short_client = self.manager.clients.get(position['short_exchange'])
        short_funding = 0.0
        if short_client:
            try:
                symbol = get_exchange_symbol(position['pair'], position['short_exchange'])
                
                from exchanges.binance_client import BinanceClient
                from exchanges.okx_client import OKXClient
                from exchanges.bingx_client import BingXClient
                from exchanges.gate_client import GateClient
                
                if isinstance(short_client, BinanceClient):
                    binance_symbol = symbol.replace('/', '')
                    income = await short_client.get_income_history("FUNDING_FEE", limit=100, symbol=binance_symbol)
                    for item in income:
                        item_time = item.get('time', 0)
                        if item_time >= start_time:
                            short_funding += float(item.get('income', 0))
                
                elif isinstance(short_client, OKXClient):
                    income = await short_client.get_income_history(symbol, limit=100)
                    for item in income:
                        item_time = item.get('time', 0)
                        if item_time >= start_time:
                            short_funding += float(item.get('income', 0))
                
                elif isinstance(short_client, BingXClient):
                    income = await short_client.get_income_history(symbol, limit=100)
                    for item in income:
                        item_time = item.get('time', 0)
                        if item_time >= start_time:
                            short_funding += float(item.get('income', 0))
                
                elif isinstance(short_client, GateClient):
                    # Gate.io format: BTC_USDT
                    income = await short_client.get_income_history(symbol, limit=100, income_type="FUNDING_FEE")
                    for item in income:
                        item_time = item.get('time', 0)
                        if item_time >= start_time:
                            short_funding += float(item.get('income', 0))
                
                logger.debug(f"SHORT {position['short_exchange'].value} funding: ${short_funding:.6f}")
                            
            except Exception as e:
                logger.debug(f"Error getting funding from {position['short_exchange'].value}: {e}")
        
        total_funding = long_funding + short_funding
        logger.debug(f"Total funding for {pair}: ${total_funding:.6f} (LONG: ${long_funding:.6f}, SHORT: ${short_funding:.6f})")
        
        return total_funding
    
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
        """Refresh funding rates"""
        if not self.connected:
            return
        
        self._log("Refreshing funding rates from all exchanges...")
        
        async def async_refresh():
            all_rates = {}
            
            # Get Binance client
            binance_client = self.manager.clients.get(Exchange.BINANCE)
            if not binance_client:
                logger.error("Binance not connected. Cannot fetch funding rates.")
                return {}
            
            # Get all funding rates from Binance
            try:
                # Import to check type
                from exchanges.binance_client import BinanceClient
                if not isinstance(binance_client, BinanceClient):
                    logger.error("Invalid Binance client type")
                    return {}
                
                binance_rates = await binance_client.get_all_funding_rates()
                # Sort by funding rate (highest to lowest) and take top 15
                binance_rates.sort(key=lambda x: abs(x.funding_rate), reverse=True)
                top_binance_rates = binance_rates[:15]
                
                self._log(f"Found {len(binance_rates)} pairs on Binance, showing top 15 by funding rate")
                
                # For each Binance pair, get rates from other exchanges
                for binance_rate in top_binance_rates:
                    binance_symbol = binance_rate.symbol
                    # Convert Binance symbol to unified pair (e.g., BTCUSDT -> BTC/USDT)
                    base = binance_symbol.replace("USDT", "")
                    pair = f"{base}/USDT"
                    
                    rates = {Exchange.BINANCE: binance_rate}
                    
                    # Get OKX rate
                    okx_client = self.manager.clients.get(Exchange.OKX)
                    if okx_client:
                        try:
                            from config.constants import get_exchange_symbol
                            okx_symbol = get_exchange_symbol(pair, Exchange.OKX)
                            okx_rate = await okx_client.get_funding_rate(okx_symbol)
                            rates[Exchange.OKX] = okx_rate
                        except Exception as e:
                            logger.debug(f"OKX rate not available for {pair}: {e}")
                    
                    # Get BingX rate
                    bingx_client = self.manager.clients.get(Exchange.BINGX)
                    if bingx_client:
                        try:
                            from config.constants import get_exchange_symbol
                            bingx_symbol = get_exchange_symbol(pair, Exchange.BINGX)
                            bingx_rate = await bingx_client.get_funding_rate(bingx_symbol)
                            rates[Exchange.BINGX] = bingx_rate
                        except Exception as e:
                            logger.debug(f"BingX rate not available for {pair}: {e}")
                    
                    # Get Gate.io rate
                    gate_client = self.manager.clients.get(Exchange.GATE)
                    if gate_client:
                        try:
                            from config.constants import get_exchange_symbol
                            gate_symbol = get_exchange_symbol(pair, Exchange.GATE)
                            gate_rate = await gate_client.get_funding_rate(gate_symbol)
                            rates[Exchange.GATE] = gate_rate
                        except Exception as e:
                            logger.debug(f"Gate.io rate not available for {pair}: {e}")
                            
                    # Get Asterdex rate
                    aster_client = self.manager.clients.get(Exchange.ASTERDEX)
                    if aster_client:
                        try:
                            from config.constants import get_exchange_symbol
                            aster_symbol = get_exchange_symbol(pair, Exchange.ASTERDEX)
                            aster_rate = await aster_client.get_funding_rate(aster_symbol)
                            rates[Exchange.ASTERDEX] = aster_rate
                        except Exception as e:
                            logger.debug(f"Aster rate not available for {pair}: {e}")
                    
                    all_rates[pair] = rates
                    
            except Exception as e:
                logger.error(f"Error getting Binance rates: {e}")
                return {}
            
            return all_rates
        
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
        
        All rates are normalized to 4h equivalent for fair comparison:
        - 1h interval: rate * 4
        - 2h interval: rate * 2  
        - 4h interval: rate * 1 (baseline)
        - 8h interval: rate * 0.5
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
            interval = getattr(rate_obj, 'funding_interval_hours', 8) or 8
            multiplier = 4.0 / interval
            return rate_obj.funding_rate * multiplier
        
        # Calculate spreads for sorting
        pairs_with_spreads = []
        for pair, rates in all_rates.items():
            # Collect normalized rate values
            rate_values = {}
            for ex_enum in [Exchange.OKX, Exchange.BINANCE, Exchange.BINGX, Exchange.GATE, Exchange.ASTERDEX]:
                if ex_enum in rates:
                    norm = normalize_to_4h(rates[ex_enum])
                    if norm is not None:
                        rate_values[ex_enum] = norm
            
            # Calculate spread
            spread_value = 0.0
            if len(rate_values) >= 2:
                sorted_rates = sorted(rate_values.values())
                spread_value = (sorted_rates[-1] - sorted_rates[0]) * 100  # Convert to percentage
            
            pairs_with_spreads.append((pair, rates, spread_value))
        
        # Sort by spread (highest to lowest)
        sorted_pairs = sorted(pairs_with_spreads, key=lambda x: x[2], reverse=True)
        
        for pair, rates, spread_value in sorted_pairs:
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
            
            # Find best spread using normalized rates
            rate_values = {}
            for ex_enum in [Exchange.OKX, Exchange.BINANCE, Exchange.BINGX, Exchange.GATE, Exchange.ASTERDEX]:
                if ex_enum in rates:
                    norm = normalize_to_4h(rates[ex_enum])
                    if norm is not None:
                        rate_values[ex_enum] = norm
            
            best_spread = "-"
            recommendation = "-"
            
            if len(rate_values) >= 2:
                sorted_rates = sorted(rate_values.items(), key=lambda x: x[1])
                lowest = sorted_rates[0]
                highest = sorted_rates[-1]
                spread = (highest[1] - lowest[1]) * 100
                best_spread = f"{spread:.6f}%"
                recommendation = f"Long {lowest[0].value}, Short {highest[0].value}"
            
            self.funding_tree.insert("", tk.END, values=(
                pair, okx_val, binance_val, bingx_val, gate_val, aster_val, best_spread, recommendation
            ))
        
        self._log(f"✅ Loaded {len(sorted_pairs)} pairs sorted by Best Spread (normalized to 4h)")
    
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
            parts = recommendation.split(", ")
            if len(parts) == 2:
                long_ex = parts[0].replace("Long ", "").strip().lower()
                short_ex = parts[1].replace("Short ", "").strip().lower()
                
                # Map to display names (case-insensitive)
                name_map = {"okx": "OKX", "binance": "Binance", "bingx": "BingX", "gate": "Gate.io", "asterdex": "Asterdex"}
                long_display = name_map.get(long_ex, long_ex.upper())
                short_display = name_map.get(short_ex, short_ex.upper())
                
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
        
        # Map display names to Exchange enum
        exchange_map = {"OKX": Exchange.OKX, "Binance": Exchange.BINANCE, "BingX": Exchange.BINGX, "Gate.io": Exchange.GATE, "Asterdex": Exchange.ASTERDEX}
        long_ex = exchange_map.get(long_display)
        short_ex = exchange_map.get(short_display)
        
        if not long_ex or not short_ex:
            return
        
        # Fetch real-time prices from the two selected exchanges
        self._fetch_pair_prices_for_display(pair, long_ex, short_ex, long_display, short_display)
    
    def _fetch_pair_prices_for_display(self, pair: str, long_ex: Exchange, short_ex: Exchange, 
                                        long_display: str, short_display: str):
        """Fetch real-time prices for the two selected exchanges and update display"""
        async def async_get_prices():
            result_data = {}
            funding_rates = {}
            
            # Get order books, prices and funding rates from both exchanges with timeout
            tasks = []
            for exchange in [long_ex, short_ex]:
                client = self.manager.clients.get(exchange)
                if client:
                    symbol = get_exchange_symbol(pair, exchange)
                    tasks.append(self._fetch_exchange_data(client, exchange, symbol))
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, dict) and not isinstance(result, Exception):
                        if 'exchange' in result:
                            ex = result['exchange']
                            # Store full result data (order_book, price, funding_rate)
                            result_data[ex] = result
                            if 'funding_rate' in result:
                                funding_rates[ex] = result['funding_rate']
            
            return result_data, funding_rates
        
        async def _timeout_wrapper():
            try:
                return await asyncio.wait_for(async_get_prices(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout fetching pair prices")
                return {}, {}
        
        def on_complete(future):
            try:
                result_data, funding_rates = future.result(timeout=0.1)
                
                if len(result_data) >= 2:
                    long_data = result_data.get(long_ex, {})
                    short_data = result_data.get(short_ex, {})
                    
                    # Extract order book or fallback to mark price
                    long_order_book = long_data.get('order_book')
                    short_order_book = short_data.get('order_book')
                    
                    # Get real execution prices from order book
                    if long_order_book and long_order_book.get('asks') and len(long_order_book['asks']) > 0:
                        long_price = long_order_book['asks'][0][0]  # ASK price (we buy here)
                        long_price_type = "ASK"
                    else:
                        long_price = long_data.get('price', 0)
                        long_price_type = "Mark"
                    
                    if short_order_book and short_order_book.get('bids') and len(short_order_book['bids']) > 0:
                        short_price = short_order_book['bids'][0][0]  # BID price (we sell here)
                        short_price_type = "BID"
                    else:
                        short_price = short_data.get('price', 0)
                        short_price_type = "Mark"
                    
                    # Calculate price spread (BID - ASK)
                    # Positive = profitable (sell high at BID, buy low at ASK)
                    # Negative = losing (buy high, sell low)
                    price_diff = short_price - long_price
                    price_spread_pct = (price_diff / long_price * 100) if long_price > 0 else 0
                    
                    # Determine if spread is good for entry
                    is_good_entry = price_diff > 0
                    
                    # Get funding rates to determine next funding time from API
                    from datetime import datetime, timezone, timedelta
                    now = datetime.now(timezone.utc)
                    
                    # Get next funding time from exchange API (use whichever is available)
                    long_funding = funding_rates.get(long_ex)
                    short_funding = funding_rates.get(short_ex)
                    
                    # Use the next funding time from API (prefer long exchange, fallback to short)
                    next_funding_time = None
                    if long_funding and long_funding.next_funding_time:
                        next_funding_time = long_funding.next_funding_time
                    elif short_funding and short_funding.next_funding_time:
                        next_funding_time = short_funding.next_funding_time
                    
                    # Fallback: calculate manually if API didn't provide it
                    if not next_funding_time:
                        current_hour = now.hour
                        # Default funding times: 00:00, 08:00, 16:00 UTC (8-hour intervals)
                        funding_hours = [0, 8, 16]
                        next_funding_hour = None
                        for fh in funding_hours:
                            if current_hour < fh:
                                next_funding_hour = fh
                                break
                        
                        if next_funding_hour is None:
                            next_funding_hour = funding_hours[0]  # Next day 00:00
                        
                        next_funding_time = now.replace(hour=next_funding_hour, minute=0, second=0, microsecond=0)
                        if next_funding_time <= now:
                            next_funding_time += timedelta(days=1)
                    
                    # Calculate time until next funding
                    time_until_funding = next_funding_time - now
                    hours_until = time_until_funding.total_seconds() / 3600
                    
                    # Format time remaining
                    hours = int(time_until_funding.total_seconds() // 3600)
                    minutes = int((time_until_funding.total_seconds() % 3600) // 60)
                    seconds = int(time_until_funding.total_seconds() % 60)
                    time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                    
                    # Entry timing recommendation
                    entry_status = ""
                    entry_color = "black"
                    if hours_until <= 1:
                        # Within 1 hour before funding - BEST TIME
                        entry_status = "🟢 GOOD - Within 1h before funding"
                        entry_color = "green"
                    elif hours_until <= 2:
                        entry_status = "🟡 OK - 1-2h before funding"
                        entry_color = "orange"
                    else:
                        entry_status = f"🔴 WAIT - {hours_until:.1f}h until funding"
                        entry_color = "red"
                    
                    # Build funding info display
                    funding_info = ""
                    funding_interval_text = "Every 8 hours"  # Default
                    long_rate_str = "N/A"
                    short_rate_str = "N/A"
                    net_funding_str = "N/A"
                    
                    if long_funding:
                        long_rate = long_funding.funding_rate * 100
                        long_rate_str = f"{long_rate:+.6f}%"
                    
                    if short_funding:
                        short_rate = short_funding.funding_rate * 100
                        short_rate_str = f"{short_rate:+.6f}%"
                    
                    if long_funding and short_funding:
                        net_funding = short_funding.funding_rate - long_funding.funding_rate
                        net_funding_str = f"{net_funding * 100:+.6f}%"
                        
                        # Use funding interval from the exchanges (they should be the same)
                        interval_hours = long_funding.funding_interval_hours or 8
                        if interval_hours == 4:
                            funding_interval_text = "Every 4 hours"
                        elif interval_hours == 8:
                            funding_interval_text = "Every 8 hours"
                        else:
                            funding_interval_text = f"Every {interval_hours} hours"
                    
                    # Build display text with order book info
                    info_text = f"═══ {pair} ═══\n\n"
                    info_text += f"LONG  ({long_display}):  ${long_price:,.6f} [{long_price_type}]\n"
                    
                    # Show top 3 ASKs for LONG if available
                    if long_order_book and long_order_book.get('asks'):
                        asks = long_order_book['asks'][:3]
                        for i, (price, qty) in enumerate(asks):
                            info_text += f"  ASK{i+1}: ${price:,.6f} x {qty:.4f}\n"
                    
                    info_text += f"\nSHORT ({short_display}): ${short_price:,.6f} [{short_price_type}]\n"
                    
                    # Show top 3 BIDs for SHORT if available
                    if short_order_book and short_order_book.get('bids'):
                        bids = short_order_book['bids'][:3]
                        for i, (price, qty) in enumerate(bids):
                            info_text += f"  BID{i+1}: ${price:,.6f} x {qty:.4f}\n"
                    
                    info_text += "\n"
                    
                    # Display spread with sign (positive = good, negative = bad)
                    spread_sign = "+" if price_diff >= 0 else ""
                    info_text += f"Real Spread (OI): {spread_sign}${price_diff:,.6f} ({spread_sign}{price_spread_pct:.3f}%)\n"
                    
                    if is_good_entry:
                        info_text += f"Entry Signal: ✅ BID > ASK (Profitable!)\n"
                    else:
                        info_text += f"Entry Signal: ❌ BID < ASK (Would Lose!)\n"
                    
                    info_text += f"─────────────────────────\n"
                    info_text += f"Funding Rate ({long_display}):  {long_rate_str}\n"
                    info_text += f"Funding Rate ({short_display}): {short_rate_str}\n"
                    info_text += f"Net Funding (SHORT-LONG): {net_funding_str}\n"
                    info_text += f"Next Funding: {time_str}\n"
                    info_text += f"Entry Timing: {entry_status}\n"
                    info_text += f"Funding Interval: {funding_interval_text}"
                    
                    self.root.after(0, lambda: self.selected_pair_info.config(
                        text=info_text, 
                        foreground=entry_color,
                        font=('Courier', 9)
                    ))
                    
                    # Schedule next update in 2 seconds for continuous refresh
                    # BUT only if no active position (to avoid overloading)
                    if not self.active_position:
                        self._pair_info_update_task = self.root.after(2000, self._update_pair_info)
                    
                else:
                    self.root.after(0, lambda: self.selected_pair_info.config(
                        text="Could not fetch prices from selected exchanges",
                        foreground="red"
                    ))
                    
            except asyncio.TimeoutError:
                logger.warning("Timeout in on_complete getting future result")
            except Exception as e:
                logger.error(f"Error updating pair info: {e}")
                import traceback
                traceback.print_exc()
        
        future = self._run_async(_timeout_wrapper())
        if future:
            future.add_done_callback(on_complete)
    
    async def _fetch_exchange_data(self, client, exchange, symbol):
        """Fetch order book, mark price and funding rate from a single exchange with timeout"""
        result = {'exchange': exchange}
        
        # Fetch order book for real execution prices
        try:
            order_book = await asyncio.wait_for(client.get_order_book(symbol, limit=5), timeout=8.0)
            result['order_book'] = order_book
        except asyncio.TimeoutError:
            logger.warning(f"Timeout getting order book from {exchange.value}")
        except Exception as e:
            logger.debug(f"Could not get order book from {exchange.value}: {e}")
        
        # Fetch mark price as fallback
        try:
            price = await asyncio.wait_for(client.get_mark_price(symbol), timeout=8.0)
            result['price'] = price
        except asyncio.TimeoutError:
            logger.warning(f"Timeout getting price from {exchange.value}")
        except Exception as e:
            logger.debug(f"Could not get price from {exchange.value}: {e}")
        
        try:
            funding_rate = await asyncio.wait_for(client.get_funding_rate(symbol), timeout=8.0)
            result['funding_rate'] = funding_rate
        except asyncio.TimeoutError:
            logger.warning(f"Timeout getting funding rate from {exchange.value}")
        except Exception as e:
            logger.debug(f"Could not get funding rate from {exchange.value}: {e}")
        
        return result
    
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
