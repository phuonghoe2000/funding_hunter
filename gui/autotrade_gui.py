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
from typing import Optional, Dict, Any, Union, List, TYPE_CHECKING

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
from core.auto_config_scanner import download_klines_data, run_config_scan, get_best_config, format_top_results


logger = logging.getLogger(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "autotrade_config.json")

# Default pairs (used when not connected to exchange)
DEFAULT_TRADING_PAIRS = [
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
        
        # Session PnL tracking
        self.session_trades: list = []  # list of (pnl_usdt, direction, reason)
        self.session_pnl: float = 0.0
        
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
        title = ttk.Label(main_frame, text="Momentum Auto Trade", style='Title.TLabel')
        title.pack(pady=(0, 10))
        
        # Build trading content directly in main frame
        self._create_trading_content(main_frame)
    
    def _create_trading_content(self, parent):
        """Create trading content"""
        # Top section: Exchange + Strategy config
        top_frame = ttk.Frame(parent)
        top_frame.pack(fill=tk.X, pady=5)
        
        self._create_exchange_frame(top_frame)
        self._create_strategy_frame(top_frame)
        
        # Middle section: Trading controls
        self._create_trading_frame(parent)
        
        # Log section
        self._create_log_frame(parent)
    
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
        
        # Save/Load config buttons
        ttk.Button(btn_row, text="💾 Save", command=self._save_config_manual).pack(side=tk.RIGHT, padx=2)
        ttk.Button(btn_row, text="📂 Load", command=self._load_config_manual).pack(side=tk.RIGHT, padx=2)
    
    def _create_strategy_frame(self, parent):
        """Create strategy configuration frame"""
        frame = ttk.LabelFrame(parent, text="⚙️ Strategy Settings", padding="10")
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))
        
        # Trading pair
        pair_row = ttk.Frame(frame)
        pair_row.pack(fill=tk.X, pady=5)
        
        ttk.Label(pair_row, text="Pair:").pack(side=tk.LEFT, padx=5)
        self.pair_combo = ttk.Combobox(pair_row, values=DEFAULT_TRADING_PAIRS, width=12)
        self.pair_combo.set("BTC/USDT")
        self.pair_combo.pack(side=tk.LEFT, padx=5)
        
        # Load pairs button
        self.load_pairs_btn = ttk.Button(pair_row, text="📥 Load All", command=self._load_pairs_from_exchange, state=tk.DISABLED)
        self.load_pairs_btn.pack(side=tk.LEFT, padx=5)
        
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
        ttk.Label(thresh_row, text="% over").pack(side=tk.LEFT)
        
        self.lookback_var = tk.StringVar(value="7")
        ttk.Entry(thresh_row, textvariable=self.lookback_var, width=4).pack(side=tk.LEFT, padx=5)
        ttk.Label(thresh_row, text="candles").pack(side=tk.LEFT)
        
        # Position settings
        pos_row = ttk.Frame(frame)
        pos_row.pack(fill=tk.X, pady=5)
        
        # Volume (USDT) - tool will calculate size = floor(volume/price)
        ttk.Label(pos_row, text="Volume:").pack(side=tk.LEFT, padx=5)
        self.size_var = tk.StringVar(value="100")
        size_entry = ttk.Entry(pos_row, textvariable=self.size_var, width=10)
        size_entry.pack(side=tk.LEFT, padx=5)
        ttk.Label(pos_row, text="USDT").pack(side=tk.LEFT)
        
        # Size display (calculated: floor(volume/price))
        ttk.Label(pos_row, text="→ Size:").pack(side=tk.LEFT, padx=(10, 2))
        self.usdt_value_label = ttk.Label(pos_row, text="--", font=('Helvetica', 9, 'bold'), foreground="blue")
        self.usdt_value_label.pack(side=tk.LEFT)
        ttk.Label(pos_row, text="coins").pack(side=tk.LEFT, padx=(2, 10))
        
        ttk.Label(pos_row, text="Leverage:").pack(side=tk.LEFT, padx=(10, 5))
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
        
        # Volume confirmation settings
        vol_row = ttk.Frame(frame)
        vol_row.pack(fill=tk.X, pady=5)
        
        self.vol_confirm_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(vol_row, text="Volume Confirm", 
                       variable=self.vol_confirm_var).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(vol_row, text="Multiplier:").pack(side=tk.LEFT, padx=(10, 5))
        self.vol_multiplier_var = tk.StringVar(value="2.0")
        ttk.Entry(vol_row, textvariable=self.vol_multiplier_var, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(vol_row, text="x avg").pack(side=tk.LEFT, padx=2)
        
        ttk.Label(vol_row, text="Lookback:").pack(side=tk.LEFT, padx=(10, 2))
        self.vol_lookback_var = tk.StringVar(value="20")
        ttk.Entry(vol_row, textvariable=self.vol_lookback_var, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(vol_row, text="candles").pack(side=tk.LEFT, padx=2)
        
        # RSI filter settings
        rsi_row = ttk.Frame(frame)
        rsi_row.pack(fill=tk.X, pady=5)
        
        self.rsi_filter_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(rsi_row, text="RSI Filter", 
                       variable=self.rsi_filter_var).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(rsi_row, text="Period:").pack(side=tk.LEFT, padx=(10, 2))
        self.rsi_period_var = tk.StringVar(value="14")
        ttk.Entry(rsi_row, textvariable=self.rsi_period_var, width=4).pack(side=tk.LEFT, padx=2)
        
        ttk.Label(rsi_row, text="OB:").pack(side=tk.LEFT, padx=(10, 2))
        self.rsi_overbought_var = tk.StringVar(value="70")
        ttk.Entry(rsi_row, textvariable=self.rsi_overbought_var, width=4).pack(side=tk.LEFT, padx=2)
        
        ttk.Label(rsi_row, text="OS:").pack(side=tk.LEFT, padx=(10, 2))
        self.rsi_oversold_var = tk.StringVar(value="30")
        ttk.Entry(rsi_row, textvariable=self.rsi_oversold_var, width=4).pack(side=tk.LEFT, padx=2)

        # Extra signal filters
        extra_row = ttk.Frame(frame)
        extra_row.pack(fill=tk.X, pady=5)

        ttk.Label(extra_row, text="Max Momentum:").pack(side=tk.LEFT, padx=5)
        self.max_momentum_var = tk.StringVar(value="0.0")
        ttk.Entry(extra_row, textvariable=self.max_momentum_var, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(extra_row, text="% (0=OFF)").pack(side=tk.LEFT, padx=2)

        ttk.Label(extra_row, text="Min ATR:").pack(side=tk.LEFT, padx=(15, 2))
        self.min_atr_var = tk.StringVar(value="0.0")
        ttk.Entry(extra_row, textvariable=self.min_atr_var, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(extra_row, text="% (0=OFF)").pack(side=tk.LEFT, padx=2)

        # EMA trend filter
        ema_row = ttk.Frame(frame)
        ema_row.pack(fill=tk.X, pady=5)

        self.ema_trend_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ema_row, text="EMA Trend Filter",
                        variable=self.ema_trend_var).pack(side=tk.LEFT, padx=5)
        ttk.Label(ema_row, text="Period:").pack(side=tk.LEFT, padx=(10, 2))
        self.ema_period_var = tk.StringVar(value="50")
        ttk.Entry(ema_row, textvariable=self.ema_period_var, width=5).pack(side=tk.LEFT, padx=2)
        ttk.Label(ema_row, text="Slope:").pack(side=tk.LEFT, padx=(10, 2))
        self.ema_slope_var = tk.StringVar(value="3")
        ttk.Entry(ema_row, textvariable=self.ema_slope_var, width=4).pack(side=tk.LEFT, padx=2)
        ttk.Label(ema_row, text="candles").pack(side=tk.LEFT, padx=2)

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
        
        # Auto Scan Config checkbox
        self.auto_scan_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btn_row, text="🔍 Auto Scan Config", 
                       variable=self.auto_scan_var).pack(side=tk.LEFT, padx=15)
        
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
        
        # Session PnL summary
        pnl_frame = ttk.Frame(frame)
        pnl_frame.pack(fill=tk.X, pady=3)
        
        ttk.Label(pnl_frame, text="Session PnL:", style='Header.TLabel').pack(side=tk.LEFT, padx=5)
        self.pnl_total_label = ttk.Label(pnl_frame, text="+0.00$", font=('Helvetica', 12, 'bold'), foreground="gray")
        self.pnl_total_label.pack(side=tk.LEFT, padx=5)
        
        self.pnl_stats_label = ttk.Label(pnl_frame, text="0W / 0L | WR: --", foreground="gray")
        self.pnl_stats_label.pack(side=tk.LEFT, padx=15)
        
        ttk.Button(pnl_frame, text="Reset", command=self._reset_session_pnl, width=6).pack(side=tk.LEFT, padx=5)
        
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
        frame = ttk.LabelFrame(parent, text="📋 Activity Log", padding="5")
        frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.log_text = scrolledtext.ScrolledText(frame, height=15, state=tk.DISABLED, font=('Consolas', 10))
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
            self.load_pairs_btn.config(state=tk.NORMAL)
        else:
            self.connect_btn.config(text="🔌 Connect")
            self.connection_status.config(text="⚪ Disconnected", foreground="gray")
            self.start_btn.config(state=tk.DISABLED)
            self.exchange_combo.config(state='readonly')
            self.load_pairs_btn.config(state=tk.DISABLED)
    
    def _toggle_trading(self):
        """Toggle trading on/off"""
        if self.is_trading:
            self._stop_trading()
        else:
            self._start_trading()
    
    def _start_trading(self):
        """Start the trading strategy - with optional auto scan"""
        if self.auto_scan_var.get():
            self._start_auto_scan_then_trade()
            return
        self._do_start_trading()
    
    def _do_start_trading(self):
        """Actually start trading with current UI config"""
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
            lookback = int(self.lookback_var.get())
            size = float(self.size_var.get())
            leverage = int(self.leverage_var.get())
            tp = float(self.tp_var.get())
            sl = float(self.sl_var.get())
            tp_extend = float(self.tp_extend_var.get())
            max_tp = int(self.max_tp_var.get())
            vol_multiplier = float(self.vol_multiplier_var.get())
            rsi_overbought = float(self.rsi_overbought_var.get())
            rsi_oversold = float(self.rsi_oversold_var.get())
            rsi_period = int(self.rsi_period_var.get())
            vol_lookback = int(self.vol_lookback_var.get())
            max_momentum_pct = float(self.max_momentum_var.get())
            min_atr_pct = float(self.min_atr_var.get())
            ema_period = int(self.ema_period_var.get())
            ema_slope = int(self.ema_slope_var.get())
        except ValueError:
            messagebox.showerror("Error", "Invalid numeric values")
            return
        
        # Create config
        config = MomentumConfig(
            timeframe_seconds=timeframe_seconds,
            price_change_threshold=threshold,
            lookback_candles=lookback,
            position_volume_usdt=size,
            leverage=leverage,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            use_trailing_tp=self.trailing_tp_var.get(),
            tp_extension_pct=tp_extend,
            max_tp_extensions=max_tp,
            use_volume_confirmation=self.vol_confirm_var.get(),
            volume_multiplier=vol_multiplier,
            volume_lookback_candles=vol_lookback,
            use_rsi_filter=self.rsi_filter_var.get(),
            rsi_period=rsi_period,
            rsi_overbought=rsi_overbought,
            rsi_oversold=rsi_oversold,
            max_momentum_pct=max_momentum_pct,
            min_atr_pct=min_atr_pct,
            use_ema_trend=self.ema_trend_var.get(),
            ema_period=ema_period,
            ema_slope_candles=ema_slope
        )
        
        # Create strategy
        # CRITICAL: on_log must use root.after() for thread safety!
        # Strategy runs in async thread, but Tkinter widgets can only be modified from GUI thread.
        self.strategy = MomentumStrategy(
            config=config,
            exchange_client=self.exchange_client,
            on_signal=self._on_signal,
            on_trade=self._on_trade,
            on_log=lambda msg: self.root.after(0, self._log, msg)
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
        
        vol_info = ""
        if self.vol_confirm_var.get():
            vol_info = f" | Vol: {vol_multiplier}x"
        
        rsi_info = ""
        if self.rsi_filter_var.get():
            rsi_info = f" | RSI: {rsi_overbought}/{rsi_oversold}"
        
        self._log(f"Started trading {pair} | Threshold: {threshold}%/{lookback}candles | TF: {timeframe_str} | TP: {tp}% | SL: {sl}%{trailing_info}{vol_info}{rsi_info}")
        
        # Reset session PnL on new trading session
        self.session_pnl = 0.0
        self.session_trades = []
        self._update_session_pnl_display()
        
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
    
    def _start_auto_scan_then_trade(self):
        """Auto scan configs: download 15m data → backtest grid → pick best → trade"""
        if not self.connected or not self.exchange_client:
            messagebox.showerror("Error", "Not connected to exchange")
            return
        
        pair = self.pair_combo.get()
        self.start_btn.config(state=tk.DISABLED)
        self.trading_status.config(text="🔍 Scanning configs...", foreground="orange")
        self._log(f"🔍 Starting auto config scan for {pair}...")
        self._log(f"📊 Download 15m × 1000 candles → Backtest grid search → Pick best config")
        
        def scan_thread():
            try:
                # Step 1: Download 15m candle data from Binance
                clean_symbol = pair.replace("/", "").replace("-", "").upper()
                klines = download_klines_data(
                    clean_symbol, "15m", 1000,
                    on_log=lambda m: self.root.after(0, self._log, m)
                )
                
                if not klines:
                    self.root.after(0, lambda: self._log("❌ Failed to download data. Auto scan aborted."))
                    self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
                    self.root.after(0, lambda: self.trading_status.config(text="⏹ Stopped", foreground="gray"))
                    return
                
                # Step 2: Get volume and leverage from UI
                try:
                    volume = float(self.size_var.get())
                    leverage = int(self.leverage_var.get())
                except ValueError:
                    volume = 15.0
                    leverage = 5
                
                # Step 3: Run backtest grid scan
                def on_progress(current, total):
                    pct = current / total * 100
                    self.root.after(0, lambda p=pct: self.trading_status.config(
                        text=f"🔍 Scanning... {p:.0f}%", foreground="orange"
                    ))
                
                results = run_config_scan(
                    klines, volume, leverage,
                    on_log=lambda m: self.root.after(0, self._log, m),
                    on_progress=on_progress
                )
                
                # Step 4: Show top results in log
                top_report = format_top_results(results, top_n=5)
                for line in top_report.split("\n"):
                    self.root.after(0, self._log, line)
                
                # Step 5: Get best config and apply
                best = get_best_config(results, min_trades=3)
                
                if best and best.total_pnl > 0:
                    # Apply best config to UI fields
                    self.root.after(0, lambda b=best: self._apply_scan_result(b))
                    self.root.after(0, lambda b=best: self._log(
                        f"✅ Best config applied! "
                        f"Score: {b.score:.1f} | PnL: {b.total_pnl:+.2f}$ | "
                        f"WR: {b.win_rate:.0f}% | Trades: {b.num_trades} | "
                        f"PF: {b.profit_factor:.2f}"
                    ))
                    # Start trading with best config (slight delay for UI update)
                    self.root.after(500, self._do_start_trading)
                else:
                    self.root.after(0, lambda: self._log("❌ No profitable config found! Try a different pair."))
                    self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
                    self.root.after(0, lambda: self.trading_status.config(text="⏹ Stopped", foreground="gray"))
                
            except Exception as e:
                self.root.after(0, lambda err=str(e): self._log(f"❌ Auto scan error: {err}"))
                self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.trading_status.config(text="⏹ Stopped", foreground="gray"))
        
        threading.Thread(target=scan_thread, daemon=True).start()
    
    def _apply_scan_result(self, result):
        """Apply scan result config to UI fields"""
        config = result.config
        self.threshold_var.set(str(config.price_change_threshold))
        self.lookback_var.set(str(config.lookback_candles))
        self.tp_var.set(str(config.take_profit_pct))
        self.sl_var.set(str(config.stop_loss_pct))
        self.trailing_tp_var.set(config.use_trailing_tp)
        self.tp_extend_var.set(str(config.tp_extension_pct))
        self.max_tp_var.set(str(config.max_tp_extensions))
        self.vol_confirm_var.set(config.use_volume_confirmation)
        self.vol_multiplier_var.set(str(config.volume_multiplier))
        self.vol_lookback_var.set(str(config.volume_lookback))
        self.rsi_filter_var.set(config.use_rsi_filter)
        self.rsi_period_var.set(str(config.rsi_period))
        self.rsi_overbought_var.set(str(config.rsi_overbought))
        self.rsi_oversold_var.set(str(config.rsi_oversold))
        self.max_momentum_var.set(str(config.max_momentum_pct))
        self.min_atr_var.set(str(config.min_atr_pct))
        self.ema_trend_var.set(config.use_ema_trend)
        self.ema_period_var.set(str(config.ema_period))
        self.ema_slope_var.set(str(config.ema_slope_candles))
        # Set timeframe to 15m since we scanned on 15m data
        self.timeframe_combo.set("15m")
    
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
            # Update session PnL
            if trade.close_price and trade.entry_price:
                if trade.direction == TradeDirection.LONG:
                    pnl_pct = ((trade.close_price - trade.entry_price) / trade.entry_price) * 100
                else:
                    pnl_pct = ((trade.entry_price - trade.close_price) / trade.entry_price) * 100
                config = self.strategy.config if self.strategy else None
                vol = config.position_volume_usdt if config else 0
                lev = config.leverage if config else 1
                pnl_usdt = pnl_pct / 100 * vol * lev
                self.session_pnl += pnl_usdt
                self.session_trades.append(pnl_usdt)
                self.root.after(0, self._update_session_pnl_display)
    
    def _update_session_pnl_display(self):
        """Refresh session PnL summary widget"""
        trades = self.session_trades
        total = self.session_pnl
        wins = sum(1 for p in trades if p > 0)
        losses = sum(1 for p in trades if p <= 0)
        total_count = len(trades)
        wr = (wins / total_count * 100) if total_count > 0 else 0

        color = "green" if total > 0 else ("red" if total < 0 else "gray")
        sign = "+" if total >= 0 else ""
        self.pnl_total_label.config(text=f"{sign}{total:.2f}$", foreground=color)

        wr_str = f"{wr:.0f}%" if total_count > 0 else "--"
        self.pnl_stats_label.config(
            text=f"{wins}W / {losses}L | WR: {wr_str} | {total_count} trades",
            foreground=color
        )

    def _reset_session_pnl(self):
        """Reset session PnL counters"""
        self.session_pnl = 0.0
        self.session_trades = []
        self.pnl_total_label.config(text="+0.00$", foreground="gray")
        self.pnl_stats_label.config(text="0W / 0L | WR: --", foreground="gray")
        self._log("🔄 Session PnL reset")

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
                        # Update USDT value based on size (coins) and price
                        self._update_usdt_display(price)
                    
                    if change is not None:
                        color = "green" if change >= 0 else "red"
                        self.change_label.config(text=f"{change:+.2f}%", foreground=color)
            
            # Schedule next update
            if self.is_trading:
                self.root.after(1000, update_price)
        
        self.root.after(1000, update_price)
    
    def _update_usdt_display(self, price: float):
        """Update size display based on volume (USDT) and current price: size = floor(volume/price)"""
        try:
            volume_usdt = float(self.size_var.get())
            
            if price > 0:
                # Size = floor(Volume / Price)
                size = int(volume_usdt / price)
                
                self.usdt_value_label.config(text=str(size))
            else:
                self.usdt_value_label.config(text="--")
        except (ValueError, ZeroDivisionError):
            self.usdt_value_label.config(text="--")
    
    def _save_config(self):
        """Save config to file"""
        config = {
            "exchange": self.exchange_combo.get(),
            "api_key": self.api_key_entry.get(),
            "secret": self.secret_entry.get(),
            "pair": self.pair_combo.get(),
            "timeframe": self.timeframe_combo.get(),
            "threshold": self.threshold_var.get(),
            "lookback": self.lookback_var.get(),
            "size": self.size_var.get(),
            "leverage": self.leverage_var.get(),
            "tp": self.tp_var.get(),
            "sl": self.sl_var.get(),
            "trailing_tp": self.trailing_tp_var.get(),
            "tp_extend": self.tp_extend_var.get(),
            "max_tp": self.max_tp_var.get(),
            "vol_confirm": self.vol_confirm_var.get(),
            "vol_multiplier": self.vol_multiplier_var.get(),
            "vol_lookback": self.vol_lookback_var.get(),
            "rsi_filter": self.rsi_filter_var.get(),
            "rsi_period": self.rsi_period_var.get(),
            "rsi_overbought": self.rsi_overbought_var.get(),
            "rsi_oversold": self.rsi_oversold_var.get(),
            "max_momentum_pct": self.max_momentum_var.get(),
            "min_atr_pct": self.min_atr_var.get(),
            "use_ema_trend": self.ema_trend_var.get(),
            "ema_period": self.ema_period_var.get(),
            "ema_slope_candles": self.ema_slope_var.get()
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
            self.lookback_var.set(config.get("lookback", "7"))
            self.size_var.set(config.get("size", "100"))
            self.leverage_var.set(config.get("leverage", "10"))
            self.tp_var.set(config.get("tp", "0.5"))
            self.sl_var.set(config.get("sl", "0.3"))
            self.trailing_tp_var.set(config.get("trailing_tp", True))
            self.tp_extend_var.set(config.get("tp_extend", "0.3"))
            self.max_tp_var.set(config.get("max_tp", "10"))
            self.vol_confirm_var.set(config.get("vol_confirm", True))
            self.vol_multiplier_var.set(config.get("vol_multiplier", "2.0"))
            self.vol_lookback_var.set(config.get("vol_lookback", "20"))
            self.rsi_filter_var.set(config.get("rsi_filter", True))
            self.rsi_period_var.set(config.get("rsi_period", "14"))
            self.rsi_overbought_var.set(config.get("rsi_overbought", "70"))
            self.rsi_oversold_var.set(config.get("rsi_oversold", "30"))
            self.max_momentum_var.set(config.get("max_momentum_pct", "0.0"))
            self.min_atr_var.set(config.get("min_atr_pct", "0.0"))
            self.ema_trend_var.set(config.get("use_ema_trend", False))
            self.ema_period_var.set(config.get("ema_period", "50"))
            self.ema_slope_var.set(config.get("ema_slope_candles", "3"))
            
            self._log("Config loaded")
        except Exception as e:
            self._log(f"Failed to load config: {e}")
    
    def _save_config_manual(self):
        """Manual save config with file dialog"""
        from tkinter import filedialog
        
        file_path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile="autotrade_config.json",
            title="Save Config"
        )
        
        if not file_path:
            return
        
        config = {
            "exchange": self.exchange_combo.get(),
            "api_key": self.api_key_entry.get(),
            "secret": self.secret_entry.get(),
            "pair": self.pair_combo.get(),
            "timeframe": self.timeframe_combo.get(),
            "threshold": self.threshold_var.get(),
            "lookback": self.lookback_var.get(),
            "size": self.size_var.get(),
            "leverage": self.leverage_var.get(),
            "tp": self.tp_var.get(),
            "sl": self.sl_var.get(),
            "trailing_tp": self.trailing_tp_var.get(),
            "tp_extend": self.tp_extend_var.get(),
            "max_tp": self.max_tp_var.get(),
            "dry_run": self.dry_run_var.get(),
            "vol_confirm": self.vol_confirm_var.get(),
            "vol_multiplier": self.vol_multiplier_var.get(),
            "vol_lookback": self.vol_lookback_var.get(),
            "rsi_filter": self.rsi_filter_var.get(),
            "rsi_period": self.rsi_period_var.get(),
            "rsi_overbought": self.rsi_overbought_var.get(),
            "rsi_oversold": self.rsi_oversold_var.get()
        }
        
        try:
            with open(file_path, 'w') as f:
                json.dump(config, f, indent=2)
            self._log(f"Config saved to {os.path.basename(file_path)}")
        except Exception as e:
            self._log(f"Failed to save config: {e}")
            messagebox.showerror("Error", f"Failed to save config: {e}")
    
    def _load_config_manual(self):
        """Manual load config with file dialog"""
        from tkinter import filedialog
        
        file_path = filedialog.askopenfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Load Config"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'r') as f:
                config = json.load(f)
            
            # Clear existing entries
            self.api_key_entry.delete(0, tk.END)
            self.secret_entry.delete(0, tk.END)
            
            # Load values
            self.exchange_combo.set(config.get("exchange", "Binance Testnet"))
            self.api_key_entry.insert(0, config.get("api_key", ""))
            self.secret_entry.insert(0, config.get("secret", ""))
            self.pair_combo.set(config.get("pair", "BTC/USDT"))
            self.timeframe_combo.set(config.get("timeframe", "5m"))
            self.threshold_var.set(config.get("threshold", "1.0"))
            self.lookback_var.set(config.get("lookback", "7"))
            self.size_var.set(config.get("size", "100"))
            self.leverage_var.set(config.get("leverage", "10"))
            self.tp_var.set(config.get("tp", "0.5"))
            self.sl_var.set(config.get("sl", "0.3"))
            self.trailing_tp_var.set(config.get("trailing_tp", True))
            self.tp_extend_var.set(config.get("tp_extend", "0.3"))
            self.max_tp_var.set(config.get("max_tp", "10"))
            self.dry_run_var.set(config.get("dry_run", True))
            self.vol_confirm_var.set(config.get("vol_confirm", True))
            self.vol_multiplier_var.set(config.get("vol_multiplier", "2.0"))
            self.vol_lookback_var.set(config.get("vol_lookback", "20"))
            self.rsi_filter_var.set(config.get("rsi_filter", True))
            self.rsi_period_var.set(config.get("rsi_period", "14"))
            self.rsi_overbought_var.set(config.get("rsi_overbought", "70"))
            self.rsi_oversold_var.set(config.get("rsi_oversold", "30"))
            self.max_momentum_var.set(config.get("max_momentum_pct", "0.0"))
            self.min_atr_var.set(config.get("min_atr_pct", "0.0"))
            self.ema_trend_var.set(config.get("use_ema_trend", False))
            self.ema_period_var.set(config.get("ema_period", "50"))
            self.ema_slope_var.set(config.get("ema_slope_candles", "3"))
            
            self._log(f"Config loaded from {os.path.basename(file_path)}")
        except Exception as e:
            self._log(f"Failed to load config: {e}")
            messagebox.showerror("Error", f"Failed to load config: {e}")
    
    def _load_pairs_from_exchange(self):
        """Load all trading pairs from connected exchange"""
        if not self.connected or not self.exchange_client:
            messagebox.showerror("Error", "Not connected to exchange")
            return
        
        self._log("Loading trading pairs from exchange...")
        self.load_pairs_btn.config(state=tk.DISABLED, text="Loading...")
        
        async def do_load():
            try:
                symbols = await self.exchange_client.get_all_symbols()
                return symbols
            except Exception as e:
                logger.error(f"Failed to load symbols: {e}")
                return []
        
        def on_loaded(future):
            try:
                symbols = future.result()
                if symbols:
                    # Update combobox with new values
                    current = self.pair_combo.get()
                    self.root.after(0, lambda: self._update_pairs_combo(symbols, current))
                    self.root.after(0, lambda: self._log(f"Loaded {len(symbols)} trading pairs"))
                else:
                    self.root.after(0, lambda: self._log("No pairs loaded"))
            except Exception as e:
                self.root.after(0, lambda: self._log(f"Error loading pairs: {e}"))
            finally:
                self.root.after(0, lambda: self.load_pairs_btn.config(state=tk.NORMAL, text="📥 Load All"))
        
        future = self._run_async(do_load())
        if future:
            future.add_done_callback(on_loaded)
    
    def _update_pairs_combo(self, symbols: list, current_value: str):
        """Update pairs combobox with new symbols"""
        self.pair_combo['values'] = symbols
        # Keep current value if it exists in new list
        if current_value in symbols:
            self.pair_combo.set(current_value)
        elif symbols:
            self.pair_combo.set(symbols[0])
    
    
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
