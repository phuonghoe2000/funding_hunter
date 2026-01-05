"""
Main GUI Application for Funding Hunter - Multi Exchange Support
Supports OKX, Binance, and BingX
"""
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime
from typing import Optional, Dict, Any, List
import logging
import json
import os

from config.settings import settings
from config.constants import POPULAR_PAIRS, Side, Exchange, get_exchange_symbol
from exchanges.okx_client import OKXClient
from exchanges.binance_client import BinanceClient
from exchanges.bingx_client import BingXClient
from exchanges.base import BaseExchangeClient, FundingRate

logger = logging.getLogger(__name__)

# Config file path
CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "user_config.json")


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
        """Open hedged position between two exchanges"""
        results = {"success": False, "long_order": None, "short_order": None, "error": None}
        
        if long_exchange not in self.clients or short_exchange not in self.clients:
            results["error"] = "One or both exchanges not connected"
            return results
        
        long_client = self.clients[long_exchange]
        short_client = self.clients[short_exchange]
        
        long_symbol = get_exchange_symbol(pair, long_exchange)
        short_symbol = get_exchange_symbol(pair, short_exchange)
        
        try:
            # Set leverage
            await asyncio.gather(
                long_client.set_leverage(long_symbol, leverage),
                short_client.set_leverage(short_symbol, leverage)
            )
            
            # Open positions simultaneously
            long_order, short_order = await asyncio.gather(
                long_client.place_market_order(long_symbol, Side.LONG, size),
                short_client.place_market_order(short_symbol, Side.SHORT, size)
            )
            
            results["success"] = True
            results["long_order"] = long_order
            results["short_order"] = short_order
            
        except Exception as e:
            results["error"] = str(e)
            # Cleanup
            await self._cleanup_partial(long_exchange, short_exchange, pair)
        
        return results
    
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
    
    async def _cleanup_partial(self, ex1: Exchange, ex2: Exchange, pair: str):
        """Cleanup partial positions"""
        for ex in [ex1, ex2]:
            try:
                client = self.clients.get(ex)
                if client:
                    symbol = get_exchange_symbol(pair, ex)
                    pos = await client.get_position(symbol)
                    if pos:
                        await client.close_position(symbol)
            except:
                pass
    
    async def check_positions(self, pair: str, long_ex: Exchange, short_ex: Exchange) -> Dict[str, Any]:
        """Check positions on both exchanges"""
        result = {"long": None, "short": None, "liquidated": None}
        
        try:
            long_client = self.clients.get(long_ex)
            short_client = self.clients.get(short_ex)
            
            if long_client:
                symbol = get_exchange_symbol(pair, long_ex)
                result["long"] = await long_client.get_position(symbol)
            
            if short_client:
                symbol = get_exchange_symbol(pair, short_ex)
                result["short"] = await short_client.get_position(symbol)
            
            # Check liquidation
            if result["long"] is None and result["short"] is not None:
                result["liquidated"] = long_ex
            elif result["short"] is None and result["long"] is not None:
                result["liquidated"] = short_ex
                
        except Exception as e:
            logger.error(f"Error checking positions: {e}")
        
        return result


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
        self._monitor_task = None
        
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
                "trading": {
                    "leverage": self.leverage_var.get(),
                    "size": self.size_var.get(),
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
            
            # Trading settings
            if "trading" in config:
                self.leverage_var.set(config["trading"].get("leverage", "10"))
                self.size_var.set(config["trading"].get("size", "0.001"))
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
        
        ttk.Label(self.balance_frame, text="OKX:").pack(side=tk.LEFT, padx=2)
        self.okx_balance_label = ttk.Label(self.balance_frame, text="$0.00")
        self.okx_balance_label.pack(side=tk.LEFT, padx=5)
        
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
        self.pair_combo = ttk.Combobox(pair_frame, values=POPULAR_PAIRS, width=12)
        self.pair_combo.set("BTC/USDT")
        self.pair_combo.pack(side=tk.LEFT, padx=5)
        
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
        
        exchanges = ["OKX", "Binance", "BingX"]
        
        ttk.Label(ex_frame, text="LONG Exchange:", style='Header.TLabel').grid(row=0, column=0, padx=5, pady=5, sticky='e')
        self.long_exchange = ttk.Combobox(ex_frame, values=exchanges, width=12, state='readonly')
        self.long_exchange.set("OKX")
        self.long_exchange.grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Label(ex_frame, text="SHORT Exchange:", style='Header.TLabel').grid(row=0, column=2, padx=15, pady=5, sticky='e')
        self.short_exchange = ttk.Combobox(ex_frame, values=exchanges, width=12, state='readonly')
        self.short_exchange.set("Binance")
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
        
        # Auto close option
        option_frame = ttk.Frame(frame)
        option_frame.pack(fill=tk.X, pady=5)
        
        self.auto_close_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(option_frame, text="Auto-close other side on liquidation", 
                       variable=self.auto_close_var).pack(anchor=tk.W)
        
        self.monitor_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(option_frame, text="Monitor positions (check every 2s)", 
                       variable=self.monitor_var).pack(anchor=tk.W)
    
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
        columns = ("Pair", "OKX", "Binance", "BingX", "Best Spread", "Recommendation")
        self.funding_tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        
        self.funding_tree.heading("Pair", text="Pair")
        self.funding_tree.heading("OKX", text="OKX Rate")
        self.funding_tree.heading("Binance", text="Binance Rate")
        self.funding_tree.heading("BingX", text="BingX Rate")
        self.funding_tree.heading("Best Spread", text="Best Spread")
        self.funding_tree.heading("Recommendation", text="Recommendation")
        
        self.funding_tree.column("Pair", width=80)
        self.funding_tree.column("OKX", width=90)
        self.funding_tree.column("Binance", width=90)
        self.funding_tree.column("BingX", width=90)
        self.funding_tree.column("Best Spread", width=90)
        self.funding_tree.column("Recommendation", width=150)
        
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
        
        self.pos_pnl_label = ttk.Label(self.position_info, text="Total PnL: $0.00")
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
            "OKX": Exchange.OKX,
            "Binance": Exchange.BINANCE,
            "BingX": Exchange.BINGX
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
                messagebox.showerror("Error", "Need at least 2 exchanges connected for arbitrage")
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
        
        # Update balances
        if Exchange.OKX in balances:
            self.okx_balance_label.config(text=f"${balances[Exchange.OKX].available:.2f}")
        if Exchange.BINANCE in balances:
            self.binance_balance_label.config(text=f"${balances[Exchange.BINANCE].available:.2f}")
        if Exchange.BINGX in balances:
            self.bingx_balance_label.config(text=f"${balances[Exchange.BINGX].available:.2f}")
        
        # Update exchange dropdowns
        available = [ex.upper() if ex != "BingX" else "BingX" for ex in connected]
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
        self.connected = False
    
    def _disconnect(self):
        """Disconnect"""
        self._log("Disconnecting...")
        self._run_async(self.manager.disconnect_all())
        self._stop_monitoring()
        self._update_ui_disconnected()
    
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
        
        # Debug: Check connected exchanges
        self._log(f"Connected exchanges: {[e.value for e in self.manager.clients.keys()]}")
        self._log(f"Long exchange: {long_ex.value}, Short exchange: {short_ex.value}")
        
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
                    "size": size
                }
                self.root.after(0, self._update_position_display)
                
                # Start monitoring
                if self.monitor_var.get():
                    self._start_monitoring()
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
                self._stop_monitoring()
                self.active_position = None
                self.root.after(0, self._clear_position_display)
            else:
                self._log(f"❌ Close failed: {result['error']}")
        except Exception as e:
            self._log(f"Error: {e}")
    
    def _start_monitoring(self):
        """Start position monitoring"""
        if self.monitoring:
            return
        
        self.monitoring = True
        self._log("Started position monitoring")
        
        async def monitor_loop():
            while self.monitoring and self.active_position:
                try:
                    pos = self.active_position
                    result = await self.manager.check_positions(
                        pos['pair'], pos['long_exchange'], pos['short_exchange']
                    )
                    
                    # Update PnL
                    long_pnl = result['long'].unrealized_pnl if result['long'] else 0
                    short_pnl = result['short'].unrealized_pnl if result['short'] else 0
                    total_pnl = long_pnl + short_pnl
                    
                    self.root.after(0, lambda p=total_pnl: self.pos_pnl_label.config(
                        text=f"Total PnL: ${p:.2f}",
                        foreground='green' if p >= 0 else 'red'
                    ))
                    
                    # Check liquidation
                    if result['liquidated']:
                        self._log(f"⚠️ LIQUIDATION on {result['liquidated'].value}!")
                        
                        if self.auto_close_var.get():
                            # Close other side
                            other_ex = pos['short_exchange'] if result['liquidated'] == pos['long_exchange'] else pos['long_exchange']
                            client = self.manager.clients.get(other_ex)
                            if client:
                                symbol = get_exchange_symbol(pos['pair'], other_ex)
                                try:
                                    await client.close_position(symbol)
                                    self._log(f"Auto-closed position on {other_ex.value}")
                                except:
                                    pass
                        
                        self.root.after(0, lambda: messagebox.showwarning(
                            "Liquidation", 
                            f"Position liquidated on {result['liquidated'].value}!"
                        ))
                        self.monitoring = False
                        self.active_position = None
                        self.root.after(0, self._clear_position_display)
                        break
                    
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    logger.error(f"Monitor error: {e}")
                    await asyncio.sleep(2)
        
        self._run_async(monitor_loop())
    
    def _stop_monitoring(self):
        """Stop monitoring"""
        self.monitoring = False
    
    def _update_position_display(self):
        """Update position display"""
        if self.active_position:
            pos = self.active_position
            self.pos_pair_label.config(text=f"Pair: {pos['pair']}")
            self.pos_long_label.config(text=f"Long: {pos['long_exchange'].value}")
            self.pos_short_label.config(text=f"Short: {pos['short_exchange'].value}")
            self.pos_size_label.config(text=f"Size: {pos['size']}")
            self.pos_status_label.config(text="Status: OPEN", foreground='green')
    
    def _clear_position_display(self):
        """Clear position display"""
        self.pos_pair_label.config(text="Pair: -")
        self.pos_long_label.config(text="Long: -")
        self.pos_short_label.config(text="Short: -")
        self.pos_size_label.config(text="Size: -")
        self.pos_pnl_label.config(text="Total PnL: $0.00", foreground='black')
        self.pos_status_label.config(text="Status: No Position", foreground='black')
    
    def _refresh_funding(self):
        """Refresh funding rates"""
        if not self.connected:
            return
        
        self._log("Refreshing funding rates...")
        
        async def async_refresh():
            all_rates = {}
            for pair in POPULAR_PAIRS[:5]:  # Top 5 pairs
                try:
                    rates = await self.manager.get_funding_rates(pair)
                    all_rates[pair] = rates
                except Exception as e:
                    logger.error(f"Error getting rates for {pair}: {e}")
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
        """Update funding table"""
        for item in self.funding_tree.get_children():
            self.funding_tree.delete(item)
        
        for pair, rates in all_rates.items():
            okx_rate = rates.get(Exchange.OKX)
            binance_rate = rates.get(Exchange.BINANCE)
            bingx_rate = rates.get(Exchange.BINGX)
            
            okx_val = f"{okx_rate.funding_rate * 100:.4f}%" if okx_rate else "-"
            binance_val = f"{binance_rate.funding_rate * 100:.4f}%" if binance_rate else "-"
            bingx_val = f"{bingx_rate.funding_rate * 100:.4f}%" if bingx_rate else "-"
            
            # Find best spread
            rate_values = {}
            if okx_rate:
                rate_values[Exchange.OKX] = okx_rate.funding_rate
            if binance_rate:
                rate_values[Exchange.BINANCE] = binance_rate.funding_rate
            if bingx_rate:
                rate_values[Exchange.BINGX] = bingx_rate.funding_rate
            
            best_spread = "-"
            recommendation = "-"
            
            if len(rate_values) >= 2:
                sorted_rates = sorted(rate_values.items(), key=lambda x: x[1])
                lowest = sorted_rates[0]
                highest = sorted_rates[-1]
                spread = (highest[1] - lowest[1]) * 100
                best_spread = f"{spread:.4f}%"
                recommendation = f"Long {lowest[0].value}, Short {highest[0].value}"
            
            self.funding_tree.insert("", tk.END, values=(
                pair, okx_val, binance_val, bingx_val, best_spread, recommendation
            ))
    
    def _on_funding_select(self, event):
        """Handle funding row select"""
        selected = self.funding_tree.selection()
        if selected:
            item = self.funding_tree.item(selected[0])
            values = item['values']
            pair = values[0]
            rec = values[5]
            
            self.pair_combo.set(pair)
            
            if rec != "-":
                # Parse recommendation
                parts = rec.split(", ")
                if len(parts) == 2:
                    long_ex = parts[0].replace("Long ", "").strip()
                    short_ex = parts[1].replace("Short ", "").strip()
                    
                    # Map to display names
                    name_map = {"okx": "OKX", "binance": "Binance", "bingx": "BingX"}
                    self.long_exchange.set(name_map.get(long_ex, long_ex))
                    self.short_exchange.set(name_map.get(short_ex, short_ex))
    
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
