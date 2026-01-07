"""
Funding Hunter LITE - Simplified version with Binance and BingX only
- Removed: OKX support
- Removed: Pair Info panel
- Removed: Active Position panel
"""
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import logging
import json
import os
import sys

from config.settings import settings
from config.constants import POPULAR_PAIRS, Side, Exchange, get_exchange_symbol
from exchanges.binance_client import BinanceClient
from exchanges.bingx_client import BingXClient
from exchanges.base import BaseExchangeClient, FundingRate

logger = logging.getLogger(__name__)

# Config file path
def _get_base_dir() -> str:
    """Return the directory for persistent files (works when frozen)."""
    if getattr(sys, "frozen", False):  # PyInstaller onefile/onedir
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(__file__))


CONFIG_FILE = os.path.join(_get_base_dir(), "user_config_lite.json")


class MultiExchangeManager:
    """Manages connections and positions across multiple exchanges"""
    
    def __init__(self):
        self.clients: Dict[Exchange, BaseExchangeClient] = {}
        self.connected_exchanges: List[Exchange] = []
        
    async def connect_exchange(self, exchange: Exchange, client: BaseExchangeClient) -> bool:
        """Connect to a single exchange"""
        try:
            success = await client.connect()
            if success:
                self.clients[exchange] = client
                self.connected_exchanges.append(exchange)
                await client.set_position_mode(hedge_mode=True)
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to connect to {exchange.value}: {e}")
            return False
    
    async def disconnect_all(self):
        """Disconnect from all exchanges"""
        for exchange, client in self.clients.items():
            try:
                await client.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting from {exchange.value}: {e}")
        self.clients.clear()
        self.connected_exchanges.clear()
    
    async def get_all_balances(self) -> Dict[Exchange, Any]:
        """Get balances from all connected exchanges"""
        balances = {}
        for exchange, client in self.clients.items():
            try:
                balance = await client.get_balance("USDT")
                balances[exchange] = balance
            except Exception as e:
                logger.error(f"Error getting balance from {exchange.value}: {e}")
        return balances
    
    async def get_funding_rates(self, pair: str) -> Dict[Exchange, FundingRate]:
        """Get funding rates from all exchanges"""
        rates = {}
        for exchange, client in self.clients.items():
            try:
                symbol = get_exchange_symbol(pair, exchange)
                rate = await client.get_funding_rate(symbol)
                rates[exchange] = rate
            except Exception as e:
                logger.error(f"Error getting funding rate from {exchange.value}: {e}")
        return rates
    
    async def open_hedged_position(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange,
        size: float,
        leverage: int
    ) -> Dict[str, Any]:
        """
        Open hedged position between two exchanges with atomic-like behavior.
        If one exchange fails, automatically rollback the other.
        """
        results = {"success": False, "long_order": None, "short_order": None, "error": None}
        
        if long_exchange not in self.clients or short_exchange not in self.clients:
            results["error"] = "One or both exchanges not connected"
            return results
        
        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]
        
        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)
        
        # Track what we've done for rollback
        long_order = None
        short_order = None
        leverage_set = False
        
        try:
            logger.info(f"Step 1/3: Pre-flight checks for {pair}...")
            
            # Pre-flight validation: Check balances
            long_balance = await long_client.get_balance("USDT")
            short_balance = await short_client.get_balance("USDT")
            
            if long_balance.available < 10:  # Minimum $10 required
                raise Exception(f"{long_exchange.value}: Insufficient balance (${long_balance.available:.6f})")
            if short_balance.available < 10:
                raise Exception(f"{short_exchange.value}: Insufficient balance (${short_balance.available:.6f})")
            
            logger.info(f"✓ Balance check passed: {long_exchange.value}=${long_balance.available:.6f}, {short_exchange.value}=${short_balance.available:.6f}")
            
            # Pre-flight validation: Check existing positions
            long_pos = await long_client.get_position(long_symbol)
            short_pos = await short_client.get_position(short_symbol)
            
            if long_pos and long_pos.size > 0:
                raise Exception(f"{long_exchange.value}: Already have position for {long_symbol}")
            if short_pos and short_pos.size > 0:
                raise Exception(f"{short_exchange.value}: Already have position for {short_symbol}")
            
            logger.info(f"✓ Position check passed: No existing positions")
            
            # Step 1: Set leverage on both exchanges
            logger.info(f"Step 2/3: Setting leverage to {leverage}x...")
            try:
                await asyncio.gather(
                    long_client.set_leverage(long_symbol, leverage),
                    short_client.set_leverage(short_symbol, leverage)
                )
                leverage_set = True
                logger.info(f"✓ Leverage set successfully")
            except Exception as e:
                raise Exception(f"Failed to set leverage: {e}")
            
            # Step 2: Open LONG position first (safer to fail here before SHORT)
            logger.info(f"Step 3/3: Opening positions...")
            logger.info(f"  → Opening LONG on {long_exchange.value}...")
            
            try:
                long_order = await self._place_order_with_retry(
                    long_client, long_symbol, Side.LONG, size, long_exchange, max_retries=3
                )
                logger.info(f"✓ LONG order placed: {long_order.order_id}")
            except Exception as e:
                # LONG failed - no cleanup needed, just fail
                raise Exception(f"LONG order failed on {long_exchange.value} after retries: {e}")
            
            # Step 3: Open SHORT position
            logger.info(f"  → Opening SHORT on {short_exchange.value}...")
            
            try:
                short_order = await self._place_order_with_retry(
                    short_client, short_symbol, Side.SHORT, size, short_exchange, max_retries=3
                )
                logger.info(f"✓ SHORT order placed: {short_order.order_id}")
            except Exception as e:
                # SHORT failed but LONG succeeded - CRITICAL: Must rollback LONG!
                logger.error(f"✗ SHORT order failed! Attempting to rollback LONG position...")
                
                rollback_success = await self._rollback_long_position(
                    long_client, long_symbol, long_order, long_exchange
                )
                
                if rollback_success:
                    raise Exception(f"SHORT order failed on {short_exchange.value}, LONG position rolled back successfully: {e}")
                else:
                    raise Exception(f"CRITICAL: SHORT order failed AND rollback failed! You have an unhedged LONG position on {long_exchange.value}. Please close manually! Error: {e}")
            
            # Both orders successful
            results["success"] = True
            results["long_order"] = long_order
            results["short_order"] = short_order
            logger.info(f"✅ Hedged position opened successfully!")
            
        except Exception as e:
            results["error"] = str(e)
            logger.error(f"❌ Failed to open hedged position: {e}")
        
        return results
    
    async def _rollback_long_position(
        self, 
        client: BaseExchangeClient, 
        symbol: str, 
        order: Any,
        exchange: Exchange
    ) -> bool:
        """
        Rollback a LONG position by closing it immediately.
        Returns True if successful, False otherwise.
        """
        try:
            logger.warning(f"🔄 Rolling back LONG position on {exchange.value}...")
            
            # Wait a moment for order to settle
            await asyncio.sleep(0.5)
            
            # Verify position exists
            position = await client.get_position(symbol)
            if not position or position.size == 0:
                logger.warning(f"⚠️ No position found on {exchange.value}, might have been rejected")
                return True
            
            # Close the position with retry
            close_order = await self._place_order_with_retry(
                client, symbol, Side.SHORT, position.size, exchange, max_retries=3
            )
            logger.info(f"✓ Rollback successful: Closed LONG position on {exchange.value} (Order: {close_order.order_id})")
            
            return True
            
        except Exception as e:
            logger.error(f"✗ Rollback failed on {exchange.value}: {e}")
            return False
    
    async def _place_order_with_retry(
        self,
        client: BaseExchangeClient,
        symbol: str,
        side: Side,
        size: float,
        exchange: Exchange,
        max_retries: int = 3
    ) -> Any:
        """
        Place order with exponential backoff retry logic.
        Retries up to max_retries times with increasing delays.
        """
        last_error = None
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    # Exponential backoff: 0.5s, 1s, 2s
                    delay = 0.5 * (2 ** attempt)
                    logger.info(f"  ↻ Retry {attempt}/{max_retries} after {delay}s delay...")
                    await asyncio.sleep(delay)
                
                order = await client.place_market_order(symbol, side, size)
                return order
                
            except Exception as e:
                last_error = e
                logger.warning(f"  ⚠️ Attempt {attempt + 1}/{max_retries} failed: {e}")
                
                if attempt == max_retries - 1:
                    # Last attempt failed
                    raise Exception(f"All {max_retries} attempts failed. Last error: {e}")
        
        # Should not reach here, but just in case
        raise Exception(f"Order failed after {max_retries} retries: {last_error}")
    
    async def close_hedged_position(
        self,
        pair: str,
        long_exchange: Exchange,
        short_exchange: Exchange
    ) -> Dict[str, Any]:
        """Close hedged position"""
        results = {"success": False, "error": None}
        
        long_client = self.clients.get(long_exchange)
        short_client = self.clients.get(short_exchange)
        
        if not long_client or not short_client:
            results["error"] = "Exchange not connected"
            return results
        
        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)
        
        try:
            close_tasks = []
            
            # Check if positions exist before closing
            long_pos = await long_client.get_position(long_symbol)
            short_pos = await short_client.get_position(short_symbol)
            
            if long_pos:
                close_tasks.append(long_client.close_position(long_symbol))
            if short_pos:
                close_tasks.append(short_client.close_position(short_symbol))
            
            if close_tasks:
                await asyncio.gather(*close_tasks)
            
            results["success"] = True
            
        except Exception as e:
            results["error"] = str(e)
        
        return results


class FundingHunterLiteGUI:
    """Lite GUI Application - Binance and BingX only"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🎯 Funding Hunter LITE - Binance & BingX")
        self.root.geometry("1200x700")
        self.root.minsize(1000, 600)
        
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
        self.active_position = None
        self._monitor_task = None
        self.cached_funding_rates = {}
        
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
                self.bingx_enabled.set(config["bingx"].get("enabled", True))
            
            # Trading settings
            if "trading" in config:
                self.leverage_var.set(config["trading"].get("leverage", "10"))
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
        
        # Logs only (no position panel)
        self._create_log_frame(main_frame)
    
    def _create_exchange_frame(self, parent):
        """Create exchange credentials frame - Binance and BingX only"""
        frame = ttk.LabelFrame(parent, text="🔌 Exchange Credentials (LITE Version)", padding="10")
        frame.pack(fill=tk.X, pady=(0, 10))
        
        # Notebook for exchanges
        notebook = ttk.Notebook(frame)
        notebook.pack(fill=tk.X, pady=5)
        
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
        
        self.bingx_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(bingx_frame, text="Enable", variable=self.bingx_enabled).grid(row=0, column=5, padx=5)
        
        # Connection buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=10)
        
        self.connect_btn = ttk.Button(btn_frame, text="🔗 Connect All", command=self._connect)
        self.connect_btn.pack(side=tk.LEFT, padx=5)
        
        self.disconnect_btn = ttk.Button(btn_frame, text="🔌 Disconnect", command=self._disconnect, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=5)
        
        # Debug mode checkbox
        self.debug_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(btn_frame, text="🔍 Debug Mode", variable=self.debug_mode).pack(side=tk.LEFT, padx=10)
        
        self.status_label = ttk.Label(btn_frame, text="⚪ Disconnected")
        self.status_label.pack(side=tk.LEFT, padx=20)
        
        # Balances
        self.balance_frame = ttk.Frame(btn_frame)
        self.balance_frame.pack(side=tk.RIGHT, padx=10)
        
        ttk.Label(self.balance_frame, text="Binance:").pack(side=tk.LEFT, padx=2)
        self.binance_balance_label = ttk.Label(self.balance_frame, text="$0.00")
        self.binance_balance_label.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(self.balance_frame, text="BingX:").pack(side=tk.LEFT, padx=2)
        self.bingx_balance_label = ttk.Label(self.balance_frame, text="$0.00")
        self.bingx_balance_label.pack(side=tk.LEFT, padx=5)
    
    def _create_trading_frame(self, parent):
        """Create trading panel"""
        frame = ttk.LabelFrame(parent, text="📊 Trading Panel", padding="10")
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        
        # Trading pair
        pair_frame = ttk.Frame(frame)
        pair_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(pair_frame, text="Pair:").pack(side=tk.LEFT, padx=5)
        self.pair_combo = ttk.Combobox(pair_frame, values=POPULAR_PAIRS, width=15)
        self.pair_combo.set("BTC/USDT")
        self.pair_combo.pack(side=tk.LEFT, padx=5)
        
        # Button to load all Binance pairs
        self.load_pairs_btn = ttk.Button(pair_frame, text="📋 Load All Binance Pairs", 
                                         command=self._load_binance_pairs, state=tk.DISABLED)
        self.load_pairs_btn.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(pair_frame, text="Leverage:").pack(side=tk.LEFT, padx=10)
        self.leverage_var = tk.StringVar(value="10")
        self.leverage_spin = ttk.Spinbox(pair_frame, from_=1, to=100, width=5, textvariable=self.leverage_var)
        self.leverage_spin.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(pair_frame, text="Size:").pack(side=tk.LEFT, padx=10)
        self.size_entry = ttk.Entry(pair_frame, width=12)
        self.size_entry.insert(0, "0.01")
        self.size_entry.pack(side=tk.LEFT, padx=5)
        
        # Exchange selection
        ex_frame = ttk.LabelFrame(frame, text="Select Exchanges for Arbitrage", padding="10")
        ex_frame.pack(fill=tk.X, pady=10)
        
        exchanges = ["Binance", "BingX"]
        
        ttk.Label(ex_frame, text="LONG Exchange:", style='Header.TLabel').grid(row=0, column=0, padx=5, pady=5, sticky='e')
        self.long_exchange = ttk.Combobox(ex_frame, values=exchanges, width=12, state='readonly')
        self.long_exchange.set("Binance")
        self.long_exchange.grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(ex_frame, text="SHORT Exchange:", style='Header.TLabel').grid(row=0, column=2, padx=15, pady=5, sticky='e')
        self.short_exchange = ttk.Combobox(ex_frame, values=exchanges, width=12, state='readonly')
        self.short_exchange.set("BingX")
        self.short_exchange.grid(row=0, column=3, padx=5, pady=5)
        
        # Action buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=15)
        
        self.open_btn = ttk.Button(btn_frame, text="🚀 Open Hedged Position", 
                                   command=self._open_position, state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        
        self.close_btn = ttk.Button(btn_frame, text="🛑 Close Position", 
                                    command=self._close_position, state=tk.DISABLED)
        self.close_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
    
    def _create_funding_frame(self, parent):
        """Create funding rates panel"""
        frame = ttk.LabelFrame(parent, text="💰 Funding Rates Comparison", padding="10")
        frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=5)
        
        self.refresh_funding_btn = ttk.Button(btn_frame, text="🔄 Refresh Rates", 
                                              command=self._refresh_funding, state=tk.DISABLED)
        self.refresh_funding_btn.pack(side=tk.LEFT, padx=5)
        
        # Funding table - simplified with only Binance and BingX columns
        columns = ("Pair", "Binance", "BingX", "Spread", "Recommendation")
        self.funding_tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        
        self.funding_tree.heading("Pair", text="Pair")
        self.funding_tree.heading("Binance", text="Binance Rate")
        self.funding_tree.heading("BingX", text="BingX Rate")
        self.funding_tree.heading("Spread", text="Spread")
        self.funding_tree.heading("Recommendation", text="Recommendation")
        
        self.funding_tree.column("Pair", width=80)
        self.funding_tree.column("Binance", width=100)
        self.funding_tree.column("BingX", width=100)
        self.funding_tree.column("Spread", width=80)
        self.funding_tree.column("Recommendation", width=150)
        
        self.funding_tree.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.funding_tree.bind("<Double-1>", self._on_funding_select)
    
    def _create_log_frame(self, parent):
        """Create log display"""
        frame = ttk.LabelFrame(parent, text="📝 Logs", padding="10")
        frame.pack(fill=tk.BOTH, expand=True)
        
        self.log_text = scrolledtext.ScrolledText(frame, height=10, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        ttk.Button(frame, text="Clear", command=self._clear_logs).pack(side=tk.RIGHT, pady=5)
    
    def _setup_logging(self):
        """Setup logging to GUI"""
        class GUIHandler(logging.Handler):
            def __init__(self, text_widget):
                super().__init__()
                self.text_widget = text_widget
            
            def emit(self, record):
                msg = self.format(record)
                self.text_widget.after(0, self._append, msg)
            
            def _append(self, msg):
                self.text_widget.configure(state=tk.NORMAL)
                self.text_widget.insert(tk.END, msg + "\n")
                self.text_widget.see(tk.END)
                self.text_widget.configure(state=tk.DISABLED)
        
        handler = GUIHandler(self.log_text)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.INFO)
    
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
            "Binance": Exchange.BINANCE,
            "BingX": Exchange.BINGX
        }
        return mapping.get(name, Exchange.BINANCE)
    
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
        
        # Get balances
        balances = await self.manager.get_all_balances()
        
        return connected, balances
    
    def _on_connect_complete(self, future):
        """Handle connection complete"""
        try:
            connected, balances = future.result()
            
            if len(connected) >= 2:
                self.connected = True
                self.root.after(0, self._update_ui_connected, connected, balances)
                self._log(f"Connected to: {', '.join(connected)}")
            else:
                self.root.after(0, self._update_ui_disconnected)
                self._log(f"Need at least 2 exchanges. Connected: {connected}")
                messagebox.showerror("Error", "Need both Binance and BingX connected for arbitrage")
        except Exception as e:
            self.root.after(0, self._update_ui_disconnected)
            self._log(f"Connection error: {e}")
    
    def _update_ui_connected(self, connected, balances):
        """Update UI after connection"""
        self.status_label.config(text=f"🟢 Connected: {', '.join(connected)}")
        self.connect_btn.config(state=tk.DISABLED)
        self.disconnect_btn.config(state=tk.NORMAL)
        self.open_btn.config(state=tk.NORMAL)
        self.close_btn.config(state=tk.NORMAL)
        self.refresh_funding_btn.config(state=tk.NORMAL)
        
        # Enable load pairs button if Binance is connected
        if "Binance" in connected:
            self.load_pairs_btn.config(state=tk.NORMAL)
        
        # Update balances
        if Exchange.BINANCE in balances:
            self.binance_balance_label.config(text=f"${balances[Exchange.BINANCE].available:.6f}")
        if Exchange.BINGX in balances:
            self.bingx_balance_label.config(text=f"${balances[Exchange.BINGX].available:.6f}")
        
        # Update exchange dropdowns
        available = connected
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
        self.refresh_funding_btn.config(state=tk.DISABLED)
        self.load_pairs_btn.config(state=tk.DISABLED)
        self.connected = False
    
    def _disconnect(self):
        """Disconnect"""
        self._log("Disconnecting...")
        self._run_async(self.manager.disconnect_all())
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
        self.root.after(0, lambda: self.load_pairs_btn.config(state=tk.NORMAL))
        
        try:
            pairs = future.result()
            if pairs:
                self.root.after(0, self._update_pair_dropdown, pairs)
                self._log(f"✅ Loaded {len(pairs)} pairs from Binance")
            else:
                self._log("❌ No pairs loaded from Binance")
        except Exception as e:
            self._log(f"Error loading pairs: {e}")
    
    def _update_pair_dropdown(self, pairs):
        """Update pair dropdown with new pairs"""
        current_pair = self.pair_combo.get()
        self.pair_combo['values'] = pairs
        
        # Keep current selection if it exists in new list
        if current_pair in pairs:
            self.pair_combo.set(current_pair)
        elif pairs:
            self.pair_combo.set(pairs[0])
    
    def _open_position(self):
        """Open hedged position"""
        if not self.connected:
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
        except ValueError:
            messagebox.showerror("Error", "Invalid size or leverage")
            return
        
        long_ex = self._get_exchange_enum(long_ex_name)
        short_ex = self._get_exchange_enum(short_ex_name)
        
        self._log(f"Opening: {pair} | Long {long_ex_name} | Short {short_ex_name} | Size: {size}")
        self.open_btn.config(state=tk.DISABLED)
        
        async def async_open():
            return await self.manager.open_hedged_position(pair, long_ex, short_ex, size, leverage)
        
        future = self._run_async(async_open())
        if future:
            future.add_done_callback(lambda f: self._on_open_complete(f, pair, long_ex, short_ex, size))
    
    def _on_open_complete(self, future, pair, long_ex, short_ex, size):
        """Handle open complete"""
        self.root.after(0, lambda: self.open_btn.config(state=tk.NORMAL))
        
        try:
            result = future.result()
            if result["success"]:
                self._log("✅ Position opened successfully!")
                self.active_position = {
                    "pair": pair,
                    "long_exchange": long_ex,
                    "short_exchange": short_ex,
                    "size": size,
                    "open_time": datetime.now(timezone.utc)
                }
            else:
                self._log(f"❌ Failed: {result['error']}")
                messagebox.showerror("Error", result['error'])
        except Exception as e:
            self._log(f"Error: {e}")
    
    def _close_position(self):
        """Close position"""
        if not self.active_position:
            messagebox.showinfo("Info", "No active position")
            return
        
        if not messagebox.askyesno("Confirm", "Close the hedged position?"):
            return
        
        pos = self.active_position
        self._log(f"Closing position for {pos['pair']}...")
        
        async def async_close():
            return await self.manager.close_hedged_position(
                pos['pair'], pos['long_exchange'], pos['short_exchange']
            )
        
        future = self._run_async(async_close())
        if future:
            future.add_done_callback(self._on_close_complete)
    
    def _on_close_complete(self, future):
        """Handle close complete"""
        try:
            result = future.result()
            if result["success"]:
                self._log("✅ Position closed!")
                self.active_position = None
            else:
                self._log(f"❌ Close failed: {result['error']}")
        except Exception as e:
            self._log(f"Error: {e}")
    
    def _refresh_funding(self):
        """Refresh funding rates"""
        if not self.connected:
            return
        
        self._log("Refreshing funding rates from Binance and BingX...")
        
        async def async_refresh():
            all_rates = {}
            
            # Get Binance client
            binance_client = self.manager.clients.get(Exchange.BINANCE)
            if not binance_client:
                logger.error("Binance not connected. Cannot fetch funding rates.")
                return {}
            
            # Get all funding rates from Binance
            try:
                from exchanges.binance_client import BinanceClient
                if not isinstance(binance_client, BinanceClient):
                    logger.error("Invalid Binance client type")
                    return {}
                
                binance_rates = await binance_client.get_all_funding_rates()
                # Sort by funding rate (highest to lowest) and take top 15
                binance_rates.sort(key=lambda x: abs(x.funding_rate), reverse=True)
                top_binance_rates = binance_rates[:20]
                
                self._log(f"Found {len(binance_rates)} pairs on Binance, showing top 20 by funding rate")
                
                # For each Binance pair, get rate from BingX
                for binance_rate in top_binance_rates:
                    binance_symbol = binance_rate.symbol
                    # Convert Binance symbol to unified pair (e.g., BTCUSDT -> BTC/USDT)
                    base = binance_symbol.replace("USDT", "")
                    pair = f"{base}/USDT"
                    
                    rates = {Exchange.BINANCE: binance_rate}
                    
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
        try:
            all_rates = future.result()
            self.root.after(0, self._update_funding_table, all_rates)
        except Exception as e:
            self._log(f"Error: {e}")
    
    def _update_funding_table(self, all_rates):
        """Update funding table - sorted by Binance funding rate"""
        for item in self.funding_tree.get_children():
            self.funding_tree.delete(item)
        
        if not all_rates:
            self._log("No funding rates available")
            return
        
        # Cache the funding rates for later use
        self.cached_funding_rates = all_rates
        
        # Sort by Binance funding rate (highest to lowest by absolute value)
        sorted_pairs = sorted(
            all_rates.items(),
            key=lambda x: abs(x[1].get(Exchange.BINANCE).funding_rate) if Exchange.BINANCE in x[1] else 0,
            reverse=True
        )
        
        for pair, rates in sorted_pairs:
            binance_rate = rates.get(Exchange.BINANCE)
            bingx_rate = rates.get(Exchange.BINGX)
            
            binance_val = f"{binance_rate.funding_rate * 100:.6f}%" if binance_rate else "-"
            bingx_val = f"{bingx_rate.funding_rate * 100:.6f}%" if bingx_rate else "-"
            
            # Calculate spread
            spread = "-"
            recommendation = "-"
            
            if binance_rate and bingx_rate:
                spread_val = abs(binance_rate.funding_rate - bingx_rate.funding_rate) * 100
                spread = f"{spread_val:.6f}%"
                
                # Determine recommendation
                if binance_rate.funding_rate > bingx_rate.funding_rate:
                    recommendation = "Long BingX, Short Binance"
                else:
                    recommendation = "Long Binance, Short BingX"
            
            self.funding_tree.insert("", tk.END, values=(
                pair, binance_val, bingx_val, spread, recommendation
            ))
        
        self._log(f"✅ Loaded {len(sorted_pairs)} pairs sorted by funding rate")
    
    def _on_funding_select(self, event):
        """Handle funding row double-click - auto select pair and exchanges"""
        selected = self.funding_tree.selection()
        if not selected:
            return
            
        item = self.funding_tree.item(selected[0])
        values = item['values']
        pair = values[0]
        recommendation = values[4]
        
        # Set the pair in dropdown
        self.pair_combo.set(pair)
        self._log(f"📌 Selected pair: {pair}")
        
        # Parse and set exchanges from recommendation
        if recommendation != "-":
            # Parse recommendation like "Long Binance, Short BingX"
            parts = recommendation.split(", ")
            if len(parts) == 2:
                long_ex = parts[0].replace("Long ", "").strip()
                short_ex = parts[1].replace("Short ", "").strip()
                
                self.long_exchange.set(long_ex)
                self.short_exchange.set(short_ex)
                
                self._log(f"✅ Auto-selected: LONG on {long_ex}, SHORT on {short_ex}")
    
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
            "🎯 Funding Hunter LITE v1.0\n\n"
            "Lightweight Funding Arbitrage Tool\n\n"
            "Supported Exchanges:\n"
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
    app = FundingHunterLiteGUI()
    app.run()


if __name__ == "__main__":
    main()
