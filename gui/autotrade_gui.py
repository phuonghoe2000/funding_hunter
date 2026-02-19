"""
Auto Trade Demo GUI

Simplified GUI for momentum trading strategy.
Only connects to one exchange and trades based on price momentum.
"""

import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from core.dry_run_wrapper import DryRunExchangeWrapper
    from exchanges.base import BaseExchangeClient
import logging
import json
import os

from config.settings import settings, BingXConfig, BinanceConfig
from config.constants import Exchange, get_exchange_symbol
from exchanges.bingx_client import BingXClient
from exchanges.binance_client import BinanceClient
from core.momentum_strategy import (
    MomentumStrategy, 
    MomentumConfig, 
    MomentumSignal,
    ActiveTrade,
    TradeDirection
)

logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "autotrade_config.json")

TRADING_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT"
]


class AutoTradeGUI:
    """Simplified Auto Trade GUI"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("⚡ Auto Trade Demo - Momentum Strategy")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)
        
        # Style
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure('Title.TLabel', font=('Helvetica', 14, 'bold'))
        self.style.configure('Header.TLabel', font=('Helvetica', 11, 'bold'))
        self.style.configure('Success.TLabel', foreground='green')
        self.style.configure('Error.TLabel', foreground='red')
        self.style.configure('Signal.TLabel', font=('Helvetica', 12, 'bold'))
        
        # Async
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.async_thread: Optional[threading.Thread] = None
        
        # State
        self.connected = False
        self.exchange_client: Optional[Any] = None  # BaseExchangeClient or DryRunExchangeWrapper
        self.exchange_type: Optional[Exchange] = None
        self.strategy: Optional[MomentumStrategy] = None
        self.is_trading = False
        self.is_dry_run = False  # Track if dry run mode is active
        
        # Build UI
        self._create_widgets()
        self._setup_logging()
        self._start_async_loop()
        
        # Load config
        self._load_config()
        
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
    
    def _create_widgets(self):
        """Create all UI widgets"""
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Title
        title = ttk.Label(main_frame, text="⚡ Momentum Auto Trade", style='Title.TLabel')
        title.pack(pady=(0, 10))
        
        # Top section: Exchange + Strategy config
        top_frame = ttk.Frame(main_frame)
        top_frame.pack(fill=tk.X, pady=5)
        
        self._create_exchange_frame(top_frame)
        self._create_strategy_frame(top_frame)
        
        # Middle section: Trading controls
        self._create_trading_frame(main_frame)
        
        # Bottom section: Log
        self._create_log_frame(main_frame)
    
    def _create_exchange_frame(self, parent):
        """Create exchange connection frame"""
        frame = ttk.LabelFrame(parent, text="🔌 Exchange Connection", padding="10")
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        
        # Exchange selection
        ex_row = ttk.Frame(frame)
        ex_row.pack(fill=tk.X, pady=5)
        
        ttk.Label(ex_row, text="Exchange:").pack(side=tk.LEFT, padx=5)
        self.exchange_combo = ttk.Combobox(ex_row, values=["BingX", "Binance", "Binance Testnet"], width=15, state='readonly')
        self.exchange_combo.set("Binance Testnet")
        self.exchange_combo.pack(side=tk.LEFT, padx=5)
        
        # Dry Run mode checkbox
        self.dry_run_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ex_row, text="🧪 Dry Run (no real trades)", 
                       variable=self.dry_run_var).pack(side=tk.LEFT, padx=15)
        
        # API Key
        key_row = ttk.Frame(frame)
        key_row.pack(fill=tk.X, pady=5)
        
        ttk.Label(key_row, text="API Key:").pack(side=tk.LEFT, padx=5)
        self.api_key_entry = ttk.Entry(key_row, width=40, show="*")
        self.api_key_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        # Secret
        secret_row = ttk.Frame(frame)
        secret_row.pack(fill=tk.X, pady=5)
        
        ttk.Label(secret_row, text="Secret:").pack(side=tk.LEFT, padx=5)
        self.secret_entry = ttk.Entry(secret_row, width=40, show="*")
        self.secret_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        # Connect button
        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, pady=10)
        
        self.connect_btn = ttk.Button(btn_row, text="🔌 Connect", command=self._toggle_connection)
        self.connect_btn.pack(side=tk.LEFT, padx=5)
        
        self.connection_status = ttk.Label(btn_row, text="⚪ Disconnected", foreground="gray")
        self.connection_status.pack(side=tk.LEFT, padx=10)
    
    def _create_strategy_frame(self, parent):
        """Create strategy configuration frame"""
        frame = ttk.LabelFrame(parent, text="⚙️ Strategy Settings", padding="10")
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))
        
        # Trading pair
        pair_row = ttk.Frame(frame)
        pair_row.pack(fill=tk.X, pady=5)
        
        ttk.Label(pair_row, text="Pair:").pack(side=tk.LEFT, padx=5)
        self.pair_combo = ttk.Combobox(pair_row, values=TRADING_PAIRS, width=12)
        self.pair_combo.set("BTC/USDT")
        self.pair_combo.pack(side=tk.LEFT, padx=5)
        
        # Timeframe
        ttk.Label(pair_row, text="Timeframe:").pack(side=tk.LEFT, padx=(20, 5))
        self.timeframe_combo = ttk.Combobox(pair_row, values=["1m", "5m", "15m", "30m", "1h"], width=6, state='readonly')
        self.timeframe_combo.set("5m")
        self.timeframe_combo.pack(side=tk.LEFT, padx=5)
        
        # Threshold
        thresh_row = ttk.Frame(frame)
        thresh_row.pack(fill=tk.X, pady=5)
        
        ttk.Label(thresh_row, text="Price Change Trigger:").pack(side=tk.LEFT, padx=5)
        self.threshold_var = tk.StringVar(value="1.0")
        ttk.Entry(thresh_row, textvariable=self.threshold_var, width=6).pack(side=tk.LEFT, padx=5)
        ttk.Label(thresh_row, text="%").pack(side=tk.LEFT)
        
        # Position settings
        pos_row = ttk.Frame(frame)
        pos_row.pack(fill=tk.X, pady=5)
        
        ttk.Label(pos_row, text="Size:").pack(side=tk.LEFT, padx=5)
        self.size_var = tk.StringVar(value="100")
        ttk.Entry(pos_row, textvariable=self.size_var, width=8).pack(side=tk.LEFT, padx=5)
        ttk.Label(pos_row, text="USDT").pack(side=tk.LEFT)
        
        ttk.Label(pos_row, text="Leverage:").pack(side=tk.LEFT, padx=(20, 5))
        self.leverage_var = tk.StringVar(value="10")
        ttk.Spinbox(pos_row, from_=1, to=50, width=5, textvariable=self.leverage_var).pack(side=tk.LEFT, padx=5)
        
        # TP/SL
        tpsl_row = ttk.Frame(frame)
        tpsl_row.pack(fill=tk.X, pady=5)
        
        ttk.Label(tpsl_row, text="Take Profit:").pack(side=tk.LEFT, padx=5)
        self.tp_var = tk.StringVar(value="0.5")
        ttk.Entry(tpsl_row, textvariable=self.tp_var, width=6).pack(side=tk.LEFT, padx=5)
        ttk.Label(tpsl_row, text="%").pack(side=tk.LEFT)
        
        ttk.Label(tpsl_row, text="Stop Loss:").pack(side=tk.LEFT, padx=(20, 5))
        self.sl_var = tk.StringVar(value="0.3")
        ttk.Entry(tpsl_row, textvariable=self.sl_var, width=6).pack(side=tk.LEFT, padx=5)
        ttk.Label(tpsl_row, text="%").pack(side=tk.LEFT)
        
        # Trailing TP settings
        trailing_row = ttk.Frame(frame)
        trailing_row.pack(fill=tk.X, pady=5)
        
        self.trailing_tp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(trailing_row, text="Trailing TP", 
                       variable=self.trailing_tp_var).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(trailing_row, text="Extend:").pack(side=tk.LEFT, padx=(10, 5))
        self.tp_extend_var = tk.StringVar(value="0.3")
        ttk.Entry(trailing_row, textvariable=self.tp_extend_var, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(trailing_row, text="%").pack(side=tk.LEFT)
        
        ttk.Label(trailing_row, text="Max:").pack(side=tk.LEFT, padx=(10, 5))
        self.max_tp_var = tk.StringVar(value="10")
        ttk.Spinbox(trailing_row, from_=0, to=50, width=4, textvariable=self.max_tp_var).pack(side=tk.LEFT, padx=2)
        ttk.Label(trailing_row, text="(0=unlimited)").pack(side=tk.LEFT, padx=2)
    
    def _create_trading_frame(self, parent):
        """Create trading control frame"""
        frame = ttk.LabelFrame(parent, text="🎯 Trading Control", padding="10")
        frame.pack(fill=tk.X, pady=10)
        
        # Control buttons
        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, pady=5)
        
        self.start_btn = ttk.Button(btn_row, text="▶️ Start Trading", command=self._toggle_trading, state=tk.DISABLED)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        self.trading_status = ttk.Label(btn_row, text="⏹ Stopped", foreground="gray")
        self.trading_status.pack(side=tk.LEFT, padx=20)
        
        # Current signal display
        signal_frame = ttk.Frame(frame)
        signal_frame.pack(fill=tk.X, pady=10)
        
        ttk.Label(signal_frame, text="Last Signal:", style='Header.TLabel').pack(side=tk.LEFT, padx=5)
        self.signal_label = ttk.Label(signal_frame, text="-- No signal --", style='Signal.TLabel')
        self.signal_label.pack(side=tk.LEFT, padx=10)
        
        # Current position display
        pos_frame = ttk.Frame(frame)
        pos_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(pos_frame, text="Position:", style='Header.TLabel').pack(side=tk.LEFT, padx=5)
        self.position_label = ttk.Label(pos_frame, text="No position", foreground="gray")
        self.position_label.pack(side=tk.LEFT, padx=10)
        
        # Price display
        price_frame = ttk.Frame(frame)
        price_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(price_frame, text="Current Price:", style='Header.TLabel').pack(side=tk.LEFT, padx=5)
        self.price_label = ttk.Label(price_frame, text="--", font=('Helvetica', 14, 'bold'))
        self.price_label.pack(side=tk.LEFT, padx=10)
        
        ttk.Label(price_frame, text="Change:").pack(side=tk.LEFT, padx=(30, 5))
        self.change_label = ttk.Label(price_frame, text="--", font=('Helvetica', 12))
        self.change_label.pack(side=tk.LEFT, padx=5)
    
    def _create_log_frame(self, parent):
        """Create log output frame"""
        frame = ttk.LabelFrame(parent, text="📋 Log", padding="5")
        frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.log_text = scrolledtext.ScrolledText(frame, height=12, state=tk.DISABLED, font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # Clear button
        ttk.Button(frame, text="Clear Log", command=self._clear_log).pack(anchor=tk.E, pady=5)
    
    def _setup_logging(self):
        """Setup logging to GUI"""
        class GUIHandler(logging.Handler):
            def __init__(self, text_widget, root):
                super().__init__()
                self.text_widget = text_widget
                self.root = root
            
            def emit(self, record):
                msg = self.format(record)
                self.root.after(0, self._append, msg)
            
            def _append(self, msg):
                self.text_widget.config(state=tk.NORMAL)
                self.text_widget.insert(tk.END, msg + "\n")
                self.text_widget.see(tk.END)
                self.text_widget.config(state=tk.DISABLED)
        
        handler = GUIHandler(self.log_text, self.root)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%H:%M:%S'))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
    
    def _log(self, message: str):
        """Log a message to the GUI"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def _clear_log(self):
        """Clear log text"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def _start_async_loop(self):
        """Start async event loop in separate thread"""
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
        
        self.async_thread = threading.Thread(target=run_loop, daemon=True)
        self.async_thread.start()
    
    def _run_async(self, coro):
        """Run coroutine in async loop"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None
    
    def _toggle_connection(self):
        """Toggle exchange connection"""
        if self.connected:
            self._disconnect()
        else:
            self._connect()
    
    def _connect(self):
        """Connect to exchange"""
        api_key = self.api_key_entry.get().strip()
        secret = self.secret_entry.get().strip()
        exchange_name = self.exchange_combo.get()
        
        if not api_key or not secret:
            messagebox.showerror("Error", "Please enter API Key and Secret")
            return
        
        self._log(f"Connecting to {exchange_name}...")
        self.connect_btn.config(state=tk.DISABLED)
        
        async def do_connect():
            try:
                if exchange_name == "BingX":
                    config = BingXConfig(api_key=api_key, secret_key=secret)
                    client = BingXClient(config)
                    self.exchange_type = Exchange.BINGX
                elif exchange_name == "Binance Testnet":
                    config = BinanceConfig(api_key=api_key, secret_key=secret, testnet=True)
                    client = BinanceClient(config)
                    self.exchange_type = Exchange.BINANCE
                else:  # Binance (mainnet)
                    config = BinanceConfig(api_key=api_key, secret_key=secret, testnet=False)
                    client = BinanceClient(config)
                    self.exchange_type = Exchange.BINANCE
                
                success = await client.connect()
                if not success:
                    return None
                
                # Wrap with DryRunWrapper if dry run is enabled
                if self.dry_run_var.get():
                    from core.dry_run_wrapper import DryRunExchangeWrapper
                    client = DryRunExchangeWrapper(client)
                    logger.info("🧪 Dry Run mode enabled - trades will be simulated")
                
                return client
            except Exception as e:
                logger.error(f"Connection failed: {e}")
                return None
        
        def on_connect(future):
            try:
                client = future.result()
                if client:
                    self.exchange_client = client
                    self.connected = True
                    self.is_dry_run = self.dry_run_var.get()
                    self.root.after(0, self._update_connection_ui, True)
                    dry_run_note = " (DRY RUN)" if self.is_dry_run else ""
                    self.root.after(0, lambda: self._log(f"✅ Connected to {exchange_name}{dry_run_note}"))
                else:
                    self.root.after(0, self._update_connection_ui, False)
                    self.root.after(0, lambda: self._log(f"❌ Connection failed"))
            except Exception as e:
                self.root.after(0, self._update_connection_ui, False)
                self.root.after(0, lambda: self._log(f"❌ Error: {e}"))
        
        future = self._run_async(do_connect())
        if future:
            future.add_done_callback(on_connect)
    
    def _disconnect(self):
        """Disconnect from exchange"""
        if self.is_trading:
            self._stop_trading()
        
        async def do_disconnect():
            if self.exchange_client:
                await self.exchange_client.disconnect()
        
        self._run_async(do_disconnect())
        
        self.exchange_client = None
        self.connected = False
        self._update_connection_ui(False)
        self._log("Disconnected")
    
    def _update_connection_ui(self, connected: bool):
        """Update UI based on connection state"""
        self.connect_btn.config(state=tk.NORMAL)
        
        if connected:
            self.connect_btn.config(text="🔌 Disconnect")
            self.connection_status.config(text="🟢 Connected", foreground="green")
            self.start_btn.config(state=tk.NORMAL)
            self.exchange_combo.config(state=tk.DISABLED)
        else:
            self.connect_btn.config(text="🔌 Connect")
            self.connection_status.config(text="⚪ Disconnected", foreground="gray")
            self.start_btn.config(state=tk.DISABLED)
            self.exchange_combo.config(state='readonly')
    
    def _toggle_trading(self):
        """Toggle trading on/off"""
        if self.is_trading:
            self._stop_trading()
        else:
            self._start_trading()
    
    def _start_trading(self):
        """Start the trading strategy"""
        if not self.connected or not self.exchange_client:
            messagebox.showerror("Error", "Not connected to exchange")
            return
        
        # Get config from UI
        pair = self.pair_combo.get()
        timeframe_str = self.timeframe_combo.get()
        
        # Convert timeframe to seconds
        tf_map = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
        timeframe_seconds = tf_map.get(timeframe_str, 300)
        
        try:
            threshold = float(self.threshold_var.get())
            size = float(self.size_var.get())
            leverage = int(self.leverage_var.get())
            tp = float(self.tp_var.get())
            sl = float(self.sl_var.get())
            tp_extend = float(self.tp_extend_var.get())
            max_tp = int(self.max_tp_var.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid numeric values")
            return
        
        # Create config
        config = MomentumConfig(
            timeframe_seconds=timeframe_seconds,
            price_change_threshold=threshold,
            position_size_usdt=size,
            leverage=leverage,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            use_trailing_tp=self.trailing_tp_var.get(),
            tp_extension_pct=tp_extend,
            max_tp_extensions=max_tp
        )
        
        # Create strategy
        self.strategy = MomentumStrategy(
            config=config,
            exchange_client=self.exchange_client,
            on_signal=self._on_signal,
            on_trade=self._on_trade,
            on_log=self._log
        )
        
        # Get symbol for exchange (use BINANCE as fallback for type checker)
        exchange = self.exchange_type if self.exchange_type else Exchange.BINANCE
        symbol = get_exchange_symbol(pair, exchange)
        
        # Start strategy
        async def do_start():
            if self.strategy:
                await self.strategy.start(symbol)
        
        self._run_async(do_start())
        
        self.is_trading = True
        self._update_trading_ui(True)
        
        trailing_info = ""
        if self.trailing_tp_var.get():
            max_ext = f"max {max_tp}" if max_tp > 0 else "unlimited"
            trailing_info = f" | Trailing TP: +{tp_extend}% ({max_ext})"
        
        self._log(f"▶️ Started trading {pair} | Threshold: {threshold}% | TF: {timeframe_str} | TP: {tp}% | SL: {sl}%{trailing_info}")
        
        # Start price update loop
        self._start_price_updates()
    
    def _stop_trading(self):
        """Stop the trading strategy"""
        if self.strategy:
            strategy = self.strategy  # Local reference for closure
            async def do_stop():
                await strategy.stop()
            
            self._run_async(do_stop())
        
        self.is_trading = False
        self._update_trading_ui(False)
        self._log("⏹ Trading stopped")
    
    def _update_trading_ui(self, trading: bool):
        """Update UI based on trading state"""
        if trading:
            self.start_btn.config(text="⏹ Stop Trading")
            self.trading_status.config(text="▶️ Running", foreground="green")
            # Disable config changes while trading
            self.pair_combo.config(state=tk.DISABLED)
            self.timeframe_combo.config(state=tk.DISABLED)
        else:
            self.start_btn.config(text="▶️ Start Trading")
            self.trading_status.config(text="⏹ Stopped", foreground="gray")
            self.pair_combo.config(state='normal')
            self.timeframe_combo.config(state='readonly')
    
    def _on_signal(self, signal: MomentumSignal):
        """Handle momentum signal"""
        direction = signal.direction.value.upper()
        change = signal.price_change_pct
        
        if signal.direction == TradeDirection.LONG:
            color = "green"
            arrow = "🟢 ↑"
        elif signal.direction == TradeDirection.SHORT:
            color = "red"
            arrow = "🔴 ↓"
        else:
            color = "gray"
            arrow = "⚪"
        
        self.root.after(0, lambda: self.signal_label.config(
            text=f"{arrow} {direction} ({change:+.2f}%)",
            foreground=color
        ))
    
    def _on_trade(self, trade: ActiveTrade, event: str):
        """Handle trade event"""
        if "OPENED" in event:
            self.root.after(0, lambda: self.position_label.config(
                text=f"{trade.direction.value.upper()} @ {trade.entry_price:.2f} | TP: {trade.take_profit_price:.2f} | SL: {trade.stop_loss_price:.2f}",
                foreground="blue"
            ))
        elif "TP_EXTENDED" in event:
            # Update position display with new TP/SL and extension count
            tp_count = trade.tp_hit_count
            self.root.after(0, lambda: self.position_label.config(
                text=f"{trade.direction.value.upper()} @ {trade.entry_price:.2f} | TP: {trade.take_profit_price:.2f} | SL: {trade.stop_loss_price:.2f} | 🎯x{tp_count}",
                foreground="green"
            ))
        elif "CLOSED" in event:
            self.root.after(0, lambda: self.position_label.config(
                text="No position",
                foreground="gray"
            ))
    
    def _start_price_updates(self):
        """Start periodic price updates"""
        def update_price():
            if not self.is_trading or not self.strategy:
                return
            
            symbol = self.strategy.symbol
            if symbol:
                tracker = self.strategy.trackers.get(symbol)
                if tracker:
                    price = tracker.get_current_price()
                    change = tracker.get_price_change(self.strategy.config.timeframe_seconds)
                    
                    if price:
                        self.price_label.config(text=f"${price:,.2f}")
                    
                    if change is not None:
                        color = "green" if change >= 0 else "red"
                        self.change_label.config(text=f"{change:+.2f}%", foreground=color)
            
            # Schedule next update
            if self.is_trading:
                self.root.after(1000, update_price)
        
        self.root.after(1000, update_price)
    
    def _save_config(self):
        """Save config to file"""
        config = {
            "exchange": self.exchange_combo.get(),
            "api_key": self.api_key_entry.get(),
            "secret": self.secret_entry.get(),
            "pair": self.pair_combo.get(),
            "timeframe": self.timeframe_combo.get(),
            "threshold": self.threshold_var.get(),
            "size": self.size_var.get(),
            "leverage": self.leverage_var.get(),
            "tp": self.tp_var.get(),
            "sl": self.sl_var.get(),
            "trailing_tp": self.trailing_tp_var.get(),
            "tp_extend": self.tp_extend_var.get(),
            "max_tp": self.max_tp_var.get()
        }
        
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
            self._log("Config saved")
        except Exception as e:
            self._log(f"Failed to save config: {e}")
    
    def _load_config(self):
        """Load config from file"""
        if not os.path.exists(CONFIG_FILE):
            return
        
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
            
            self.exchange_combo.set(config.get("exchange", "BingX"))
            self.api_key_entry.insert(0, config.get("api_key", ""))
            self.secret_entry.insert(0, config.get("secret", ""))
            self.pair_combo.set(config.get("pair", "BTC/USDT"))
            self.timeframe_combo.set(config.get("timeframe", "5m"))
            self.threshold_var.set(config.get("threshold", "1.0"))
            self.size_var.set(config.get("size", "100"))
            self.leverage_var.set(config.get("leverage", "10"))
            self.tp_var.set(config.get("tp", "0.5"))
            self.sl_var.set(config.get("sl", "0.3"))
            self.trailing_tp_var.set(config.get("trailing_tp", True))
            self.tp_extend_var.set(config.get("tp_extend", "0.3"))
            self.max_tp_var.set(config.get("max_tp", "10"))
            
            self._log("Config loaded")
        except Exception as e:
            self._log(f"Failed to load config: {e}")
    
    def _on_closing(self):
        """Handle window close"""
        if self.is_trading:
            if not messagebox.askyesno("Confirm", "Trading is active. Stop and exit?"):
                return
            self._stop_trading()
        
        self._save_config()
        
        if self.connected:
            self._disconnect()
        
        # Stop async loop
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        
        self.root.destroy()
    
    def run(self):
        """Run the GUI application"""
        self.root.mainloop()


def main():
    """Main entry point"""
    app = AutoTradeGUI()
    app.run()


if __name__ == "__main__":
    main()
