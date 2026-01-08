"""
Main GUI Application for Funding Hunter - Multi Exchange Support
Supports OKX, Binance, and BingX
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
                    long_client, long_symbol, Side.LONG, size, long_exchange, max_retries=1
                )
                logger.info(f"✓ LONG order placed: {long_order.order_id}")
            except Exception as e:
                # LONG failed - no cleanup needed, just fail
                raise Exception(f"LONG order failed on {long_exchange.value} after retries: {e}")
            
            # Step 3: Open SHORT position
            logger.info(f"  → Opening SHORT on {short_exchange.value}...")
            
            try:
                short_order = await self._place_order_with_retry(
                    short_client, short_symbol, Side.SHORT, size, short_exchange, max_retries=1
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
                client, symbol, Side.SHORT, position.size, exchange, max_retries=1
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
        max_retries: int = 1
    ) -> Any:
        """
        Place order with single attempt (no retries by default for speed).
        Can optionally retry if max_retries > 1.
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
        self.cached_funding_rates = {}  # Cache funding rates for quick access
        
        # Auto trading state
        self.auto_trading_active = False
        self.auto_trade_check_task = None
        self.last_auto_trade_check = None
        
        # Price spread waiting state (for manual open position)
        self.waiting_for_price_spread = False
        self.price_spread_check_task = None
        self.price_spread_params = None  # Store params: pair, long_ex, short_ex, size, leverage
        
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
        
        # Auto close option
        option_frame = ttk.Frame(frame)
        option_frame.pack(fill=tk.X, pady=5)
        
        self.auto_close_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(option_frame, text="Auto-close other side on liquidation", 
                       variable=self.auto_close_var).pack(anchor=tk.W)
        
        self.auto_close_reversal_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(option_frame, text="Auto-close on funding reversal", 
                       variable=self.auto_close_reversal_var).pack(anchor=tk.W, pady=2)
        
        self.monitor_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(option_frame, text="Monitor positions (check every 2s)", 
                       variable=self.monitor_var).pack(anchor=tk.W)
        
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
        
        # Auto Trading section - COMPACT DESIGN
        auto_trade_frame = ttk.LabelFrame(option_frame, text="🤖 Auto Trading", padding="3")
        auto_trade_frame.pack(fill=tk.X, pady=5)
        
        # Row 1: All settings in one line
        settings_row = ttk.Frame(auto_trade_frame)
        settings_row.pack(fill=tk.X, pady=2)
        
        ttk.Label(settings_row, text="Vol:").pack(side=tk.LEFT, padx=2)
        self.auto_volume_entry = ttk.Entry(settings_row, width=6)
        self.auto_volume_entry.insert(0, "100")
        self.auto_volume_entry.pack(side=tk.LEFT, padx=1)
        ttk.Label(settings_row, text="USDT").pack(side=tk.LEFT, padx=2)
        
        ttk.Label(settings_row, text="Lev:").pack(side=tk.LEFT, padx=(5,2))
        self.auto_leverage_entry = ttk.Entry(settings_row, width=4)
        self.auto_leverage_entry.insert(0, "10")
        self.auto_leverage_entry.pack(side=tk.LEFT, padx=1)
        ttk.Label(settings_row, text="x").pack(side=tk.LEFT, padx=2)
        
        ttk.Label(settings_row, text="MinSpread:").pack(side=tk.LEFT, padx=(5,2))
        self.auto_min_spread_entry = ttk.Entry(settings_row, width=5)
        self.auto_min_spread_entry.insert(0, "0.02")
        self.auto_min_spread_entry.pack(side=tk.LEFT, padx=1)
        ttk.Label(settings_row, text="%").pack(side=tk.LEFT, padx=2)
        
        ttk.Label(settings_row, text="MaxHrs:").pack(side=tk.LEFT, padx=(5,2))
        self.auto_max_hours_entry = ttk.Entry(settings_row, width=3)
        self.auto_max_hours_entry.insert(0, "2")
        self.auto_max_hours_entry.pack(side=tk.LEFT, padx=1)
        ttk.Label(settings_row, text="h").pack(side=tk.LEFT, padx=2)
        
        # Row 2: Button and Status
        control_row = ttk.Frame(auto_trade_frame)
        control_row.pack(fill=tk.X, pady=2)
        
        self.auto_trade_btn = ttk.Button(control_row, text="🚀 Start", 
                                         command=self._toggle_auto_trading, state=tk.DISABLED, width=12)
        self.auto_trade_btn.pack(side=tk.LEFT, padx=2)
        
        self.auto_trade_status = ttk.Label(control_row, text="Stopped", foreground="gray", font=('Arial', 8))
        self.auto_trade_status.pack(side=tk.LEFT, padx=5)
    
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
        
        self.pos_pnl_label = ttk.Label(self.position_info, text="PnL: $0.00 | Funding: $0.00 | Total: $0.00")
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
        self.auto_trade_btn.config(state=tk.NORMAL)
        
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
        self.refresh_funding_btn.config(state=tk.DISABLED)
        self.load_pairs_btn.config(state=tk.DISABLED)
        self.auto_trade_btn.config(state=tk.DISABLED)
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
        """Open hedged position - starts monitoring price spread and auto-opens when threshold met"""
        if not self.connected:
            return
        
        # If already waiting, this is a CANCEL action
        if self.waiting_for_price_spread:
            self._cancel_price_spread_wait()
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
            price_spread_min = float(self.price_spread_threshold.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid size, leverage, or price spread threshold")
            return
        
        long_ex = self._get_exchange_enum(long_ex_name)
        short_ex = self._get_exchange_enum(short_ex_name)
        
        # Store parameters for continuous checking
        self.price_spread_params = {
            "pair": pair,
            "long_ex": long_ex,
            "short_ex": short_ex,
            "long_ex_name": long_ex_name,
            "short_ex_name": short_ex_name,
            "size": size,
            "leverage": leverage,
            "price_spread_min": price_spread_min
        }
        
        # Start waiting mode
        self.waiting_for_price_spread = True
        self.open_btn.configure(text="🛑 Cancel")
        self._log(f"⏳ Waiting for price spread >= {price_spread_min}% on {pair}...")
        self._log(f"   Checking every 2 seconds. Click 'Cancel' to stop.")
        
        # Start price spread monitoring loop
        self._check_price_spread_and_open()
    
    def _cancel_price_spread_wait(self):
        """Cancel waiting for price spread"""
        self.waiting_for_price_spread = False
        if self.price_spread_check_task:
            self.root.after_cancel(self.price_spread_check_task)
            self.price_spread_check_task = None
        self.price_spread_params = None
        self.open_btn.configure(text="Open Position", state=tk.NORMAL)
        self._log("🛑 Cancelled waiting for price spread")
    
    def _check_price_spread_and_open(self):
        """Check price spread and open position if threshold met"""
        if not self.waiting_for_price_spread or not self.price_spread_params:
            return
        
        params = self.price_spread_params
        
        async def check_and_open():
            try:
                # Get current prices from both exchanges
                long_client = self.manager.clients.get(params["long_ex"])
                short_client = self.manager.clients.get(params["short_ex"])
                
                if not long_client or not short_client:
                    self._log("❌ Exchange clients not available")
                    return {"success": False, "error": "Exchange clients not available"}
                
                long_symbol = get_exchange_symbol(params["pair"], params["long_ex"])
                short_symbol = get_exchange_symbol(params["pair"], params["short_ex"])
                
                long_price = await long_client.get_mark_price(long_symbol)
                short_price = await short_client.get_mark_price(short_symbol)
                
                # Calculate price spread percentage
                price_diff = abs(short_price - long_price)
                avg_price = (long_price + short_price) / 2
                price_spread_pct = (price_diff / avg_price) * 100
                
                self._log(f"📊 Price spread: {price_spread_pct:.4f}% (threshold: {params['price_spread_min']}%) | LONG: ${long_price:,.6f} | SHORT: ${short_price:,.6f}")
                
                # Check if spread meets minimum threshold
                if price_spread_pct < params["price_spread_min"]:
                    # Not ready yet, check again in 2 seconds
                    return {"success": False, "waiting": True}
                
                # Price spread is good, proceed to open position!
                self._log(f"✅ Price spread threshold met! Opening position now...")
                self._log(f"Opening: {params['pair']} | Long {params['long_ex_name']} | Short {params['short_ex_name']} | Size: {params['size']}")
                
                result = await self.manager.open_hedged_position(
                    params["pair"], 
                    params["long_ex"], 
                    params["short_ex"], 
                    params["size"], 
                    params["leverage"]
                )
                
                return result
                
            except Exception as e:
                self._log(f"❌ Error: {e}")
                return {"success": False, "error": str(e)}
        
        future = self._run_async(check_and_open())
        if future:
            future.add_done_callback(lambda f: self._on_price_spread_check_complete(f))
    
    def _on_price_spread_check_complete(self, future):
        """Handle price spread check result"""
        try:
            result = future.result()
            
            # Check if just waiting (spread not met yet)
            if not result.get("success") and result.get("waiting"):
                # Schedule next check in 2 seconds
                if self.waiting_for_price_spread:
                    self.price_spread_check_task = self.root.after(2000, self._check_price_spread_and_open)
                return
            
            # Either success or error - stop waiting mode
            self.waiting_for_price_spread = False
            self.open_btn.configure(text="Open Position", state=tk.NORMAL)
            
            if result.get("success"):
                # Position opened successfully!
                params = self.price_spread_params
                if not params:
                    return
                    
                self._log("✅ Position opened successfully!")
                
                # Get initial funding rates and setup position tracking
                self._setup_position_tracking(
                    params["pair"], 
                    params["long_ex"], 
                    params["short_ex"], 
                    params["size"]
                )
                
                # Start monitoring
                if not self.monitoring:
                    self._start_monitoring()
                
            else:
                # Error occurred
                error_msg = result.get("error", "Unknown error")
                self._log(f"❌ Failed to open position: {error_msg}")
                messagebox.showerror("Error", f"Failed to open position:\n{error_msg}")
            
        except Exception as e:
            self.waiting_for_price_spread = False
            self.open_btn.configure(text="Open Position", state=tk.NORMAL)
            self._log(f"❌ Error: {e}")
            messagebox.showerror("Error", str(e))
    
    def _setup_position_tracking(self, pair, long_ex, short_ex, size):
        """Setup position tracking after successful open"""
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
                logger.error(f"Error fetching initial funding: {e}")
        
        future = self._run_async(get_initial_funding())
        if future:
            future.add_done_callback(on_funding_fetched)
    
    def _on_open_complete(self, future, pair, long_ex, short_ex, size):
        """Handle open complete"""
        self.root.after(0, lambda: self.open_btn.config(state=tk.NORMAL))
        
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
                        self.root.after(0, self._update_position_display)
                        
                        # Start monitoring
                        if self.monitor_var.get():
                            self._start_monitoring()
                            
                    except Exception as e:
                        logger.error(f"Error tracking initial funding: {e}")
                
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
                    self.root.after(0, self._update_position_display)
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
                
                # If auto trading is enabled, it will resume scanning
                if self.auto_trading_active:
                    self._log(f"🤖 Auto Trading: Resuming signal scanning...")
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
                    
                    # Get unrealized PnL from positions
                    long_pnl = result['long'].unrealized_pnl if result['long'] else 0
                    short_pnl = result['short'].unrealized_pnl if result['short'] else 0
                    unrealized_pnl = long_pnl + short_pnl
                    
                    # Get funding fees (every 30 seconds to avoid rate limit)
                    from datetime import timedelta
                    now = datetime.now(timezone.utc)
                    time_since_check = (now - pos.get('last_funding_check', now)).total_seconds()
                    
                    if time_since_check >= 30:  # Check funding fees every 30 seconds
                        funding_fees = await self._get_funding_fees(pos)
                        pos['total_funding_fees'] = funding_fees
                        pos['last_funding_check'] = now
                    else:
                        funding_fees = pos.get('total_funding_fees', 0.0)
                    
                    # Total PnL = Unrealized PnL + Funding Fees
                    total_pnl = unrealized_pnl + funding_fees
                    
                    # Update display with detailed breakdown
                    self.root.after(0, lambda u=unrealized_pnl, f=funding_fees, t=total_pnl: 
                        self.pos_pnl_label.config(
                            text=f"PnL: ${u:.6f} | Funding: ${f:.6f} | Total: ${t:.6f}",
                            foreground='green' if t >= 0 else 'red'
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
                        
                        # If auto trading is enabled, it will resume scanning
                        if self.auto_trading_active:
                            self._log(f"🤖 Auto Trading: Resuming signal scanning after liquidation...")
                        
                        break
                    
                    # Check funding reversal (if enabled)
                    if self.auto_close_reversal_var.get() and 'initial_net_funding' in pos:
                        should_close, reason = await self._check_funding_reversal(pos)
                        if should_close:
                            self._log(f"🔄 Funding reversal detected: {reason}")
                            self._log(f"🔄 Auto-closing position...")
                            
                            # Close position
                            try:
                                close_result = await self.manager.close_hedged_position(
                                    pos['pair'], pos['long_exchange'], pos['short_exchange']
                                )
                                
                                if close_result["success"]:
                                    self._log(f"✅ Position auto-closed due to: {reason}")
                                    self.root.after(0, lambda r=reason: messagebox.showinfo(
                                        "Auto-Close", 
                                        f"Position closed:\n{r}"
                                    ))
                                    
                                    # If auto trading is enabled, it will resume scanning
                                    if self.auto_trading_active:
                                        self._log(f"🤖 Auto Trading: Resuming signal scanning...")
                                else:
                                    self._log(f"❌ Auto-close failed: {close_result['error']}")
                                    
                            except Exception as e:
                                self._log(f"❌ Auto-close error: {e}")
                            
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
                if spread_reduction > 0.70:  # 70% drop
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
            return 0.0
        
        # Convert to timestamp in milliseconds
        start_time = int(open_time.timestamp() * 1000)
        
        # Get funding fees from long exchange
        long_client = self.manager.clients.get(position['long_exchange'])
        if long_client:
            try:
                symbol = get_exchange_symbol(position['pair'], position['long_exchange'])
                
                # Call get_income_history based on exchange type
                from exchanges.binance_client import BinanceClient
                from exchanges.okx_client import OKXClient
                from exchanges.bingx_client import BingXClient
                
                if isinstance(long_client, BinanceClient):
                    # Binance format: BTCUSDT
                    binance_symbol = symbol.replace('/', '')
                    income = await long_client.get_income_history("FUNDING_FEE", limit=100, symbol=binance_symbol)
                    for item in income:
                        if item.get('time', 0) >= start_time:
                            total_funding += float(item.get('income', 0))
                
                elif isinstance(long_client, OKXClient):
                    # OKX format: BTC-USDT-SWAP
                    income = await long_client.get_income_history(symbol, limit=100)
                    for item in income:
                        if item.get('time', 0) >= start_time:
                            total_funding += float(item.get('income', 0))
                
                elif isinstance(long_client, BingXClient):
                    # BingX format: BTC-USDT
                    income = await long_client.get_income_history(symbol, limit=100)
                    for item in income:
                        if item.get('time', 0) >= start_time:
                            total_funding += float(item.get('income', 0))
                            
            except Exception as e:
                logger.debug(f"Error getting funding from {position['long_exchange'].value}: {e}")
        
        # Get funding fees from short exchange
        short_client = self.manager.clients.get(position['short_exchange'])
        if short_client:
            try:
                symbol = get_exchange_symbol(position['pair'], position['short_exchange'])
                
                from exchanges.binance_client import BinanceClient
                from exchanges.okx_client import OKXClient
                from exchanges.bingx_client import BingXClient
                
                if isinstance(short_client, BinanceClient):
                    binance_symbol = symbol.replace('/', '')
                    income = await short_client.get_income_history("FUNDING_FEE", limit=100, symbol=binance_symbol)
                    for item in income:
                        if item.get('time', 0) >= start_time:
                            total_funding += float(item.get('income', 0))
                
                elif isinstance(short_client, OKXClient):
                    income = await short_client.get_income_history(symbol, limit=100)
                    for item in income:
                        if item.get('time', 0) >= start_time:
                            total_funding += float(item.get('income', 0))
                
                elif isinstance(short_client, BingXClient):
                    income = await short_client.get_income_history(symbol, limit=100)
                    for item in income:
                        if item.get('time', 0) >= start_time:
                            total_funding += float(item.get('income', 0))
                            
            except Exception as e:
                logger.debug(f"Error getting funding from {position['short_exchange'].value}: {e}")
        
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
    
    def _clear_position_display(self):
        """Clear position display"""
        self.pos_pair_label.config(text="Pair: -")
        self.pos_long_label.config(text="Long: -")
        self.pos_short_label.config(text="Short: -")
        self.pos_size_label.config(text="Size: -")
        self.pos_pnl_label.config(text="PnL: $0.00 | Funding: $0.00 | Total: $0.00", foreground='black')
        self.pos_status_label.config(text="Status: No Position", foreground='black')
    
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
        """Update funding table - sorted by Best Spread (highest to lowest)"""
        for item in self.funding_tree.get_children():
            self.funding_tree.delete(item)
        
        if not all_rates:
            self._log("No funding rates available")
            return
        
        # Cache the funding rates for later use
        self.cached_funding_rates = all_rates
        
        # Calculate spreads for sorting
        pairs_with_spreads = []
        for pair, rates in all_rates.items():
            # Collect available rates
            rate_values = {}
            if Exchange.OKX in rates:
                rate_values[Exchange.OKX] = rates[Exchange.OKX].funding_rate
            if Exchange.BINANCE in rates:
                rate_values[Exchange.BINANCE] = rates[Exchange.BINANCE].funding_rate
            if Exchange.BINGX in rates:
                rate_values[Exchange.BINGX] = rates[Exchange.BINGX].funding_rate
            
            # Calculate spread
            spread_value = 0.0
            if len(rate_values) >= 2:
                sorted_rates = sorted(rate_values.values())
                spread_value = (sorted_rates[-1] - sorted_rates[0]) * 100  # Convert to percentage
            
            pairs_with_spreads.append((pair, rates, spread_value))
        
        # Sort by spread (highest to lowest)
        sorted_pairs = sorted(pairs_with_spreads, key=lambda x: x[2], reverse=True)
        
        for pair, rates, spread_value in sorted_pairs:
            okx_rate = rates.get(Exchange.OKX)
            binance_rate = rates.get(Exchange.BINANCE)
            bingx_rate = rates.get(Exchange.BINGX)
            
            okx_val = f"{okx_rate.funding_rate * 100:.6f}%" if okx_rate else "-"
            binance_val = f"{binance_rate.funding_rate * 100:.6f}%" if binance_rate else "-"
            bingx_val = f"{bingx_rate.funding_rate * 100:.6f}%" if bingx_rate else "-"
            
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
                best_spread = f"{spread:.6f}%"
                recommendation = f"Long {lowest[0].value}, Short {highest[0].value}"
            
            self.funding_tree.insert("", tk.END, values=(
                pair, okx_val, binance_val, bingx_val, best_spread, recommendation
            ))
        
        self._log(f"✅ Loaded {len(sorted_pairs)} pairs sorted by Best Spread (highest to lowest)")
    
    def _on_funding_select(self, event):
        """Handle funding row double-click - auto select pair and exchanges in Trading Panel"""
        selected = self.funding_tree.selection()
        if not selected:
            return
            
        item = self.funding_tree.item(selected[0])
        values = item['values']
        pair = values[0]
        recommendation = values[5]
        
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
                name_map = {"okx": "OKX", "binance": "Binance", "bingx": "BingX"}
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
    
    def _on_exchange_changed(self, event):
        """Handle exchange dropdown selection change in Trading Panel"""
        self._update_pair_info()
    
    def _toggle_auto_trading(self):
        """Toggle auto trading on/off with Start/Stop button"""
        # Check current state
        if self.auto_trading_active:
            # Currently running - STOP it
            self.auto_trading_active = False
            self.auto_trade_status.config(text="Stopped", foreground="gray")
            self.auto_trade_btn.config(text="🚀 Start")
            self._log("🛑 Auto Trading STOPPED")
            
            # Stop check loop
            if self.auto_trade_check_task:
                self.root.after_cancel(self.auto_trade_check_task)
                self.auto_trade_check_task = None
        else:
            # Currently stopped - START it
            # Validation
            if not self.connected:
                self._log("❌ Cannot start auto trading: Not connected to exchanges")
                return
            
            if self.active_position:
                self._log("❌ Cannot start auto trading: Active position exists. Close it first.")
                return
            
            try:
                volume = float(self.auto_volume_entry.get())
                leverage = int(self.auto_leverage_entry.get())
                min_spread = float(self.auto_min_spread_entry.get())
                max_hours = float(self.auto_max_hours_entry.get())
                
                if volume <= 0 or leverage <= 0 or min_spread < 0 or max_hours <= 0:
                    raise ValueError("Invalid values")
                    
            except ValueError:
                self._log("❌ Invalid auto trading settings. Please check your inputs.")
                return
            
            # Enable auto trading
            self.auto_trading_active = True
            self.auto_trade_status.config(text="Scanning...", foreground="green")
            self.auto_trade_btn.config(text="🛑 Stop")
            self._log(f"🤖 Auto Trading STARTED | Scan: 2s | Vol: {volume} USDT | Lev: {leverage}x | MinSpread: {min_spread}% | MaxHrs: {max_hours}h")
            
            # Start auto trading check loop
            self._start_auto_trading_check()
    
    def _start_auto_trading_check(self):
        """Start the auto trading signal check loop"""
        if not self.auto_trading_active:
            return
        
        # Check for trading signal
        self._check_auto_trading_signal()
        
        # Schedule next check in 2 seconds (for price spread monitoring)
        self.auto_trade_check_task = self.root.after(2000, self._start_auto_trading_check)
    
    def _check_auto_trading_signal(self):
        """Check if current market conditions meet auto trading criteria"""
        if not self.auto_trading_active or not self.connected:
            return
        
        # Don't open new position if one already exists
        if self.active_position:
            self._log("⏸️  Auto Trading: Position already active, waiting...")
            return
        
        # Get auto trading settings
        try:
            min_spread_threshold = float(self.auto_min_spread_entry.get()) / 100  # Convert to decimal
            max_hours_before_funding = float(self.auto_max_hours_entry.get())
        except ValueError:
            self._log("❌ Invalid auto trading settings")
            return
        
        # Check funding rates table for best opportunity
        if not self.cached_funding_rates:
            self._log("⏸️  Auto Trading: No funding rates available yet")
            return
        
        # Find best opportunity from cached funding rates
        best_pair = None
        best_spread = 0
        best_long_ex = None
        best_short_ex = None
        best_long_rate = None
        best_short_rate = None
        
        for pair, rates in self.cached_funding_rates.items():
            rate_values = {}
            if Exchange.OKX in rates:
                rate_values[Exchange.OKX] = rates[Exchange.OKX].funding_rate
            if Exchange.BINANCE in rates:
                rate_values[Exchange.BINANCE] = rates[Exchange.BINANCE].funding_rate
            if Exchange.BINGX in rates:
                rate_values[Exchange.BINGX] = rates[Exchange.BINGX].funding_rate
            
            if len(rate_values) >= 2:
                sorted_rates = sorted(rate_values.items(), key=lambda x: x[1])
                lowest_ex, lowest_rate = sorted_rates[0]
                highest_ex, highest_rate = sorted_rates[-1]
                spread = highest_rate - lowest_rate
                
                if spread > best_spread:
                    best_spread = spread
                    best_pair = pair
                    best_long_ex = lowest_ex
                    best_short_ex = highest_ex
                    best_long_rate = lowest_rate
                    best_short_rate = highest_rate
        
        # Check if best opportunity meets criteria
        if not best_pair or best_spread < min_spread_threshold or not best_long_ex or not best_short_ex:
            self._log(f"⏸️  Auto Trading: No pair meets min spread ({min_spread_threshold*100:.3f}%). Best: {best_spread*100:.3f}%")
            return
        
        # Check timing - need to verify hours until funding
        async def check_timing_and_open():
            try:
                # Type check
                if not best_long_ex or not best_short_ex:
                    return False
                    
                # Get funding rate to check next funding time
                long_client = self.manager.clients.get(best_long_ex)
                if not long_client:
                    return False
                
                symbol = get_exchange_symbol(best_pair, best_long_ex)
                funding_rate_obj = await long_client.get_funding_rate(symbol)
                
                # Calculate hours until funding
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                next_funding_time = funding_rate_obj.next_funding_time
                time_until_funding = next_funding_time - now
                hours_until = time_until_funding.total_seconds() / 3600
                
                if hours_until > max_hours_before_funding:
                    self._log(f"⏸️  Auto Trading: Too early ({hours_until:.1f}h until funding). Waiting...")
                    return False
                
                # Check price spread threshold before opening
                short_client = self.manager.clients.get(best_short_ex)
                if not short_client:
                    return False
                
                long_symbol = get_exchange_symbol(best_pair, best_long_ex)
                short_symbol = get_exchange_symbol(best_pair, best_short_ex)
                
                # Get prices from both exchanges
                long_price = await long_client.get_mark_price(long_symbol)
                short_price = await short_client.get_mark_price(short_symbol)
                
                # Calculate price spread percentage
                price_diff = abs(short_price - long_price)
                avg_price = (long_price + short_price) / 2
                price_spread_pct = (price_diff / avg_price) * 100
                
                # Get price spread threshold from UI
                try:
                    price_spread_min = float(self.price_spread_threshold.get())
                except:
                    price_spread_min = 0.05  # Default 0.05%
                
                if price_spread_pct < price_spread_min:
                    self._log(f"⏸️  Auto Trading: Price spread too small ({price_spread_pct:.3f}% < {price_spread_min}%). Waiting...")
                    return False
                
                # All conditions met - open position!
                self._log(f"✅ Auto Trading SIGNAL DETECTED!")
                self._log(f"   Pair: {best_pair} | Funding Spread: {best_spread*100:.3f}% | Price Spread: {price_spread_pct:.3f}% | Hours: {hours_until:.1f}h")
                
                if best_long_rate is not None and best_short_rate is not None:
                    self._log(f"   LONG: {best_long_ex.value} ({best_long_rate*100:.4f}%) | SHORT: {best_short_ex.value} ({best_short_rate*100:.4f}%)")
                
                # Auto-populate the trading panel
                self.root.after(0, lambda: self._auto_populate_and_open(best_pair, best_long_ex, best_short_ex))
                
                return True
                
            except Exception as e:
                logger.error(f"Error checking timing: {e}")
                return False
        
        # Run async check
        future = self._run_async(check_timing_and_open())
        if future:
            future.add_done_callback(lambda f: None)  # Just log any errors
    
    def _auto_populate_and_open(self, pair: str, long_ex: Exchange, short_ex: Exchange):
        """Auto-populate trading panel and open position"""
        if not long_ex or not short_ex:
            self._log("❌ Invalid exchanges")
            return
            
        # Set pair
        self.pair_combo.set(pair)
        
        # Set exchanges
        exchange_display_map = {Exchange.OKX: "OKX", Exchange.BINANCE: "Binance", Exchange.BINGX: "BingX"}
        self.long_exchange.set(exchange_display_map[long_ex])
        self.short_exchange.set(exchange_display_map[short_ex])
        
        # Set leverage from auto trading settings
        leverage = self.auto_leverage_entry.get()
        self.leverage_var.set(leverage)
        
        # Calculate size based on volume (USDT) and leverage
        # Size = Volume / Price
        # We need to get current price first
        async def get_price_and_open():
            try:
                long_client = self.manager.clients.get(long_ex)
                if not long_client:
                    self._log("❌ Cannot get client for auto trading")
                    return
                    
                symbol = get_exchange_symbol(pair, long_ex)
                price = await long_client.get_mark_price(symbol)
                
                # Calculate size in base currency
                volume_usdt = float(self.auto_volume_entry.get())
                leverage_val = int(leverage)
                
                # Total position value = volume_usdt * leverage
                # Size in base currency = total_value / price
                total_value = volume_usdt * leverage_val
                size = total_value / price
                
                self._log(f"💡 Calculated size: {size:.4f} {pair.split('/')[0]} (Price: ${price:,.2f}, Volume: {volume_usdt} USDT, Leverage: {leverage_val}x)")
                
                # Set size
                self.root.after(0, lambda: self.size_entry.delete(0, tk.END))
                self.root.after(0, lambda: self.size_entry.insert(0, f"{size:.4f}"))
                
                # Trigger position open
                self.root.after(0, lambda: self._log(f"🤖 Auto Trading: Opening position..."))
                self.root.after(0, self._open_position)
                
            except Exception as e:
                self._log(f"❌ Auto Trading: Failed to calculate size: {e}")
        
        future = self._run_async(get_price_and_open())
        if future:
            future.add_done_callback(lambda f: None)
    
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
        exchange_map = {"OKX": Exchange.OKX, "Binance": Exchange.BINANCE, "BingX": Exchange.BINGX}
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
            prices = {}
            funding_rates = {}
            
            # Get prices and funding rates from both exchanges
            for exchange in [long_ex, short_ex]:
                client = self.manager.clients.get(exchange)
                if client:
                    try:
                        symbol = get_exchange_symbol(pair, exchange)
                        mark_price = await client.get_mark_price(symbol)
                        prices[exchange] = mark_price
                        
                        # Try to get funding rate
                        try:
                            funding_rate = await client.get_funding_rate(symbol)
                            funding_rates[exchange] = funding_rate
                        except:
                            pass
                    except Exception as e:
                        logger.debug(f"Could not get data from {exchange.value}: {e}")
            
            return prices, funding_rates
        
        def on_complete(future):
            try:
                prices, funding_rates = future.result()
                
                if len(prices) >= 2:
                    long_price = prices.get(long_ex, 0)
                    short_price = prices.get(short_ex, 0)
                    
                    # Calculate price spread
                    price_diff = abs(short_price - long_price)
                    price_spread_pct = (price_diff / long_price * 100) if long_price > 0 else 0
                    
                    # Determine if spread is good for entry
                    # Price difference should ideally favor our position
                    # For hedged position: we want to buy low (LONG) and sell high (SHORT)
                    is_good_entry = short_price > long_price  # SHORT price should be higher
                    
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
                    
                    if long_funding and short_funding:
                        net_funding = short_funding.funding_rate - long_funding.funding_rate
                        funding_info = f"\nNet Funding: {net_funding * 100:.6f}% (per 8h)"
                        
                        # Use funding interval from the exchanges (they should be the same)
                        # If different, show the one from the exchange with funding data
                        interval_hours = long_funding.funding_interval_hours
                        if interval_hours == 4:
                            funding_interval_text = "Every 4 hours"
                            funding_info = f"\nNet Funding: {net_funding * 100:.6f}% (per 4h)"
                        elif interval_hours == 8:
                            funding_interval_text = "Every 8 hours"
                            funding_info = f"\nNet Funding: {net_funding * 100:.6f}% (per {interval_hours}h)"
                        else:
                            funding_interval_text = f"Every {interval_hours} hours"
                            funding_info = f"\nNet Funding: {net_funding * 100:.6f}% (per {interval_hours}h)"
                    
                    # Build display text
                    info_text = f"═══ {pair} ═══\n\n"
                    info_text += f"LONG  ({long_display}):  ${long_price:,.6f}\n"
                    info_text += f"SHORT ({short_display}): ${short_price:,.6f}\n\n"
                    info_text += f"Price Spread: ${price_diff:,.6f} ({price_spread_pct:.3f}%)\n"
                    
                    if is_good_entry:
                        info_text += f"Price Position: ✅ SHORT higher than LONG (Good)\n"
                    else:
                        info_text += f"Price Position: ⚠️ LONG higher than SHORT (Inverted)\n"
                    
                    info_text += f"{funding_info}\n"
                    info_text += f"─────────────────────────\n"
                    info_text += f"Next Funding: {time_str}\n"
                    info_text += f"Entry Timing: {entry_status}\n"
                    info_text += f"Funding Interval: {funding_interval_text}"
                    
                    self.root.after(0, lambda: self.selected_pair_info.config(
                        text=info_text, 
                        foreground=entry_color,
                        font=('Courier', 9)
                    ))
                    
                    # Schedule next update in 2 seconds for continuous refresh
                    self.root.after(2000, self._update_pair_info)
                    
                else:
                    self.root.after(0, lambda: self.selected_pair_info.config(
                        text="Could not fetch prices from selected exchanges",
                        foreground="red"
                    ))
                    
            except Exception as e:
                logger.error(f"Error updating pair info: {e}")
                import traceback
                traceback.print_exc()
        
        future = self._run_async(async_get_prices())
        if future:
            future.add_done_callback(on_complete)
    
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
