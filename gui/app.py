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
from core.gui_service import GUIWorkflowService

try:
    from gui.close_controller import (
        calculate_and_show_final_pnl as controller_calculate_and_show_final_pnl,
        cancel_close_process as controller_cancel_close_process,
        close_position as controller_close_position,
        execute_close_workflow as controller_execute_close_workflow,
        on_close_complete as controller_on_close_complete,
        reset_close_button as controller_reset_close_button,
    )
    from gui.display_formatters import build_funding_table_rows
    from gui.exchange_display import parse_recommendation_display_names, to_display_name
    from gui.layout import create_funding_board_frame, create_widgets as build_widgets
    from gui.monitor_controller import (
        check_funding_reversal as controller_check_funding_reversal,
        start_monitoring as controller_start_monitoring,
        stop_monitoring as controller_stop_monitoring,
        toggle_monitoring as controller_toggle_monitoring,
    )
    from gui.open_controller import (
        cancel_open_position as controller_cancel_open_position,
        cancel_price_spread_wait as controller_cancel_price_spread_wait,
        check_price_spread_and_open as controller_check_price_spread_and_open,
        on_all_splits_complete as controller_on_all_splits_complete,
        on_analyze_for_open_complete as controller_on_analyze_for_open_complete,
        on_first_split_complete as controller_on_first_split_complete,
        on_price_spread_check_complete as controller_on_price_spread_check_complete,
        open_position as controller_open_position,
        reset_open_button as controller_reset_open_button,
        service_check_and_open as controller_service_check_and_open,
        start_analyze_for_open as controller_start_analyze_for_open,
    )
    from gui.pair_info_controller import (
        do_update_usdt_volume as controller_do_update_usdt_volume,
        fetch_pair_prices_for_display as controller_fetch_pair_prices_for_display,
        update_pair_info as controller_update_pair_info,
        update_pair_price_info as controller_update_pair_price_info,
        update_price_display as controller_update_price_display,
        update_usdt_volume as controller_update_usdt_volume,
    )
    from gui.position_loader import (
        find_hedged_pairs as build_hedged_pairs,
        load_hedged_position as load_existing_hedged_position,
        scan_existing_positions as controller_scan_existing_positions,
        show_position_selector as show_existing_position_selector,
    )
except ImportError:
    from close_controller import (
        calculate_and_show_final_pnl as controller_calculate_and_show_final_pnl,
        cancel_close_process as controller_cancel_close_process,
        close_position as controller_close_position,
        execute_close_workflow as controller_execute_close_workflow,
        on_close_complete as controller_on_close_complete,
        reset_close_button as controller_reset_close_button,
    )
    from display_formatters import build_funding_table_rows
    from exchange_display import parse_recommendation_display_names, to_display_name
    from layout import create_funding_board_frame, create_widgets as build_widgets
    from monitor_controller import (
        check_funding_reversal as controller_check_funding_reversal,
        start_monitoring as controller_start_monitoring,
        stop_monitoring as controller_stop_monitoring,
        toggle_monitoring as controller_toggle_monitoring,
    )
    from open_controller import (
        cancel_open_position as controller_cancel_open_position,
        cancel_price_spread_wait as controller_cancel_price_spread_wait,
        check_price_spread_and_open as controller_check_price_spread_and_open,
        on_all_splits_complete as controller_on_all_splits_complete,
        on_analyze_for_open_complete as controller_on_analyze_for_open_complete,
        on_first_split_complete as controller_on_first_split_complete,
        on_price_spread_check_complete as controller_on_price_spread_check_complete,
        open_position as controller_open_position,
        reset_open_button as controller_reset_open_button,
        service_check_and_open as controller_service_check_and_open,
        start_analyze_for_open as controller_start_analyze_for_open,
    )
    from pair_info_controller import (
        do_update_usdt_volume as controller_do_update_usdt_volume,
        fetch_pair_prices_for_display as controller_fetch_pair_prices_for_display,
        update_pair_info as controller_update_pair_info,
        update_pair_price_info as controller_update_pair_price_info,
        update_price_display as controller_update_price_display,
        update_usdt_volume as controller_update_usdt_volume,
    )
    from position_loader import (
        find_hedged_pairs as build_hedged_pairs,
        load_hedged_position as load_existing_hedged_position,
        scan_existing_positions as controller_scan_existing_positions,
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

from config.constants import Exchange
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
        self.latest_balances: Dict[Exchange, Any] = {}
        self.last_monitor_advice: Optional[str] = None
        self._monitor_task = None
        self.cached_funding_rates = {}  # Cache funding rates for quick access
        self._funding_board_window = None
        self.funding_board_tree = None
        self.funding_board_refresh_btn = None
        
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
        self._runtime_refresh_task = None
        
        # Build UI
        self._create_menu()
        self._create_widgets()
        self._start_runtime_refresh_loop()
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

        runtime_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Runtime", menu=runtime_menu)
        runtime_menu.add_command(label="Session Viewer", command=self._open_runtime_viewer)
        runtime_menu.add_command(label="Trade Journal", command=lambda: self._open_runtime_viewer(select_tab="journal"))

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
        self.latest_balances = dict(balances)
        self.status_label.config(text=f"🟢 Connected: {', '.join(connected)}")
        self.connect_btn.config(state=tk.DISABLED)
        self.disconnect_btn.config(state=tk.NORMAL)
        self.open_btn.config(state=tk.NORMAL)
        self.close_btn.config(state=tk.NORMAL)
        self.load_pos_btn.config(state=tk.NORMAL)
        self.refresh_funding_btn.config(state=tk.NORMAL)
        self.open_funding_board_btn.config(state=tk.NORMAL)
        
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

        self._sync_funding_board_columns()

        # Update exchange dropdowns
        available = connected  # Use exchange names directly from connected list
        self.long_exchange['values'] = available
        self.short_exchange['values'] = available
        if available:
            self.long_exchange.set(available[0])
            if len(available) > 1:
                self.short_exchange.set(available[1])

        self._refresh_runtime_views()
        if not self.active_position:
            self._attempt_recover_session()

    def _update_ui_disconnected(self):
        """Update UI after disconnect"""
        self.latest_balances = {}
        self.status_label.config(text="⚪ Disconnected")
        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.open_btn.config(state=tk.DISABLED)
        self.close_btn.config(state=tk.DISABLED)
        self.load_pos_btn.config(state=tk.DISABLED)
        self.refresh_funding_btn.config(state=tk.DISABLED)
        self.open_funding_board_btn.config(state=tk.DISABLED)
        self.load_pairs_btn.config(state=tk.DISABLED)
        self.connected = False
        self._clear_funding_views()
    
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
        return controller_open_position(self)

    def _attempt_recover_session(self):
        """Attempt to restore an active session after reconnecting."""
        future = self._run_async(self.service.recover_active_session())
        if future:
            future.add_done_callback(self._on_recover_session_complete)

    def _on_recover_session_complete(self, future):
        """Handle a recovered active session."""
        def update_ui():
            try:
                recovered = future.result(timeout=0.5)
                if not recovered:
                    return

                self.active_position = recovered
                self.pair_combo.set(recovered["pair"])
                self.long_exchange.set(to_display_name(recovered["long_exchange"]))
                self.short_exchange.set(to_display_name(recovered["short_exchange"]))
                self._update_position_display()
                self._log(
                    f"Recovered active session: {recovered['pair']} | "
                    f"LONG {recovered['long_exchange'].value} | SHORT {recovered['short_exchange'].value}"
                )
                self.service.persist_active_position(self.active_position)
                self.service.record_trade_event("session_recovered", self.active_position)
                self._update_pair_info()
                if not self.monitoring:
                    self._start_monitoring()
                    self.monitor_btn.config(text="Stop Monitor")
            except Exception as e:
                self._log(f"Session recovery error: {e}")

        self.root.after(0, update_ui)

    def _start_analyze_for_open(self):
        """Run analyze spread and then start waiting for open"""
        return controller_start_analyze_for_open(self)

    def _on_analyze_for_open_complete(self, future):
        """Handle analyze completion for open"""
        return controller_on_analyze_for_open_complete(self, future)

    def _reset_open_button(self):
        """Reset open button to default state"""
        return controller_reset_open_button(self)

    def _cancel_open_position(self):
        """Cancel analyzing or waiting for price spread"""
        return controller_cancel_open_position(self)

    def _cancel_price_spread_wait(self):
        """Cancel waiting for price spread and/or running splits"""
        return controller_cancel_price_spread_wait(self)

    async def _service_check_and_open(self, params, safe_log):
        return await controller_service_check_and_open(self, params, safe_log)

    def _check_price_spread_and_open(self):
        """Check price spread and open position if threshold met"""
        return controller_check_price_spread_and_open(self)

    def _on_price_spread_check_complete(self, future):
        """Handle price spread check result"""
        return controller_on_price_spread_check_complete(self, future)

    def _on_first_split_complete(self, params, size_opened):
        """Called after first split completes - just track position, don't start full monitoring yet"""
        return controller_on_first_split_complete(self, params, size_opened)

    def _on_all_splits_complete(self, params, total_size):
        """Called after ALL splits complete"""
        return controller_on_all_splits_complete(self, params, total_size)

    def _setup_position_tracking(self, pair, long_ex, short_ex, size, *, leverage=1, pretrade_assessment=None, open_time=None):
        """Setup position tracking after successful open"""
        async def get_initial_state():
            return await self.service.get_initial_position_state(
                pair,
                long_ex,
                short_ex,
                size,
                leverage=leverage,
                pretrade_assessment=pretrade_assessment,
                open_time=open_time,
            )

        def on_state_ready(fut):
            def update_ui():
                try:
                    self.active_position = fut.result(timeout=0.1)
                    self.service.persist_active_position(self.active_position)
                    self.service.record_trade_event("position_opened", self.active_position)
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
        """Close the active position."""
        return controller_close_position(self)

    def _reset_close_button(self):
        """Reset close button to default state."""
        return controller_reset_close_button(self)
            
    def _cancel_close_process(self):
        """Cancel the closing process."""
        return controller_cancel_close_process(self)

    def _execute_close_workflow(self):
        """Execute the full close workflow through the shared GUI service."""
        return controller_execute_close_workflow(self)

    def _load_existing_positions(self):
        """Load existing positions from exchanges and allow monitoring/closing them."""
        return controller_scan_existing_positions(self)

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
        """Handle close completion."""
        return controller_on_close_complete(self, future)

    def _calculate_and_show_final_pnl(self, closed_position: Dict[str, Any]):
        """Calculate and display final PnL after closing position via API history."""
        return controller_calculate_and_show_final_pnl(self, closed_position)

    def _start_monitoring(self):
        """Start position monitoring."""
        return controller_start_monitoring(self)

    def _stop_monitoring(self):
        """Stop monitoring."""
        return controller_stop_monitoring(self)
    
    def _toggle_monitoring(self):
        """Toggle monitoring on/off."""
        return controller_toggle_monitoring(self)
    
    async def _check_funding_reversal(self, position: Dict[str, Any]) -> tuple[bool, str]:
        """Check whether funding conditions reversed enough to close."""
        return await controller_check_funding_reversal(self, position)

    def _update_position_display(self):
        """Update position display"""
        if self.active_position:
            pos = self.active_position
            self.pos_pair_label.config(text=f"Pair: {pos['pair']}")
            self.pos_long_label.config(text=f"Long: {pos['long_exchange'].value}")
            self.pos_short_label.config(text=f"Short: {pos['short_exchange'].value}")
            self.pos_size_label.config(text=f"Size: {pos['size']}")
            status_text = "Status: OPEN"
            if pos.get("latest_monitor_advice", {}).get("action"):
                status_text = f"Status: {pos['latest_monitor_advice']['action']}"
            elif pos.get("pretrade_assessment", {}).get("recommendation"):
                status_text = (
                    f"Status: OPEN | {pos['pretrade_assessment']['recommendation']} "
                    f"({pos['pretrade_assessment'].get('quality_score', 0):.0f})"
                )
            self.pos_status_label.config(text=status_text, foreground='green')
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
        
        self.last_monitor_advice = None
        self._refresh_runtime_views()
        # Restart pair info update loop when position is closed
        if not self._pair_info_update_task:
            self._update_pair_info()
    
    def _refresh_funding(self):
        """Refresh funding rates from all connected exchanges."""
        if not self.connected:
            return

        self._log("Scanning funding rates from all exchanges...")

        async def async_refresh():
            return await self.service.scan_funding_rates(top_candidates=20)

        future = self._run_async(async_refresh())
        if future:
            future.add_done_callback(self._on_funding_complete)

    def _on_funding_complete(self, future):
        """Handle funding refresh completion."""
        def update_ui():
            try:
                all_rates = future.result()
                self._update_funding_table(all_rates)
            except Exception as e:
                self._log(f"Error: {e}")

        self.root.after(0, update_ui)

    def _open_funding_board(self):
        """Open the detachable funding board window."""
        existing = getattr(self, "_funding_board_window", None)
        if existing and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            self._sync_funding_board_columns()
            self._update_funding_table(self.cached_funding_rates)
            return

        board = tk.Toplevel(self.root)
        board.title("Funding Board")
        board.geometry("1400x720")
        board.transient(self.root)
        self._funding_board_window = board
        create_funding_board_frame(self, board)
        board.protocol("WM_DELETE_WINDOW", self._close_funding_board)

        self._sync_funding_board_columns()
        self._update_funding_table(self.cached_funding_rates)

    def _close_funding_board(self):
        board = getattr(self, "_funding_board_window", None)
        if board and board.winfo_exists():
            board.destroy()
        self._funding_board_window = None
        self.funding_board_tree = None
        self.funding_board_refresh_btn = None

    def _clear_tree(self, tree):
        if not tree:
            return
        for item in tree.get_children():
            tree.delete(item)

    def _clear_funding_views(self):
        self.cached_funding_rates = {}
        if hasattr(self, "market_summary_tree"):
            self._clear_tree(self.market_summary_tree)
        if hasattr(self, "market_summary_status"):
            self.market_summary_status.config(text="No funding data loaded", foreground="gray")
        if self.funding_board_tree:
            self._clear_tree(self.funding_board_tree)

    def _sync_funding_board_columns(self):
        """Show only connected exchange columns in the funding board."""
        if not self.funding_board_tree:
            return

        connected_exchanges = set(self.manager.clients.keys())
        for col_name, ex_enum in self._exchange_columns.items():
            if ex_enum in connected_exchanges:
                self.funding_board_tree.column(col_name, width=86, minwidth=20, stretch=True)
            else:
                self.funding_board_tree.column(col_name, width=0, minwidth=0, stretch=False)

        if self.funding_board_refresh_btn:
            state = tk.NORMAL if self.connected else tk.DISABLED
            self.funding_board_refresh_btn.config(state=state)

    def _update_market_summary(self, all_rates):
        if not hasattr(self, "market_summary_tree"):
            return

        self._clear_tree(self.market_summary_tree)
        if not all_rates:
            self.market_summary_status.config(text="No funding data loaded", foreground="gray")
            return

        rows, total_pairs = build_funding_table_rows(all_rates, limit=5)
        for row in rows:
            self.market_summary_tree.insert("", tk.END, values=(row[0], row[9], row[10]))

        self.market_summary_status.config(
            text=f"Top 5 of {total_pairs} pairs by net edge",
            foreground="green",
        )

    def _update_funding_board(self, all_rates):
        if not self.funding_board_tree:
            return

        self._clear_tree(self.funding_board_tree)
        if not all_rates:
            return

        rows, _ = build_funding_table_rows(all_rates, limit=50)
        for row in rows:
            self.funding_board_tree.insert("", tk.END, values=row)

    def _update_funding_table(self, all_rates):
        """Update the compact summary and detachable board."""
        if not all_rates:
            self._clear_funding_views()
            self._log("No funding rates available")
            return

        self.cached_funding_rates = all_rates
        self._update_market_summary(all_rates)
        self._sync_funding_board_columns()
        self._update_funding_board(all_rates)
        self._log(f"Loaded funding data for {len(all_rates)} pairs sorted by net edge after estimated costs")

    def _on_funding_select(self, event):
        """Handle funding selection from either the summary tree or the board."""
        tree = event.widget
        selected = tree.selection()
        if not selected:
            return

        item = tree.item(selected[0])
        values = item["values"]
        if not values:
            return

        pair = values[0]
        recommendation = values[-1]

        self.pair_combo.set(pair)
        self._log(f"Selected pair: {pair}")

        if recommendation != "-":
            parts = parse_recommendation_display_names(recommendation)
            if parts:
                long_display, short_display = parts
                self.long_exchange.set(long_display)
                self.short_exchange.set(short_display)
                self._log(f"Auto-selected: LONG on {long_display}, SHORT on {short_display}")

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
        """Update USDT volume display based on size and current price."""
        return controller_update_usdt_volume(self, event)

    def _do_update_usdt_volume(self):
        """Actually update USDT volume (debounced)."""
        return controller_do_update_usdt_volume(self)

    def _update_pair_info(self):
        """Update pair information display with real-time prices and timing."""
        return controller_update_pair_info(self)

    def _fetch_pair_prices_for_display(self, pair: str, long_ex: Exchange, short_ex: Exchange, long_display: str, short_display: str):
        """Fetch real-time prices for the two selected exchanges and update display."""
        return controller_fetch_pair_prices_for_display(self, pair, long_ex, short_ex, long_display, short_display)

    def _update_pair_price_info(self, pair: str, recommendation: str = ""):
        """Fetch and display current price information for the selected pair."""
        return controller_update_pair_price_info(self, pair, recommendation)

    def _update_price_display(self, price_info: str, price_details: str):
        """Update the price display labels."""
        return controller_update_price_display(self, price_info, price_details)

    def _log(self, message: str):
        """Log message"""
        logger.info(message)
    
    def _clear_logs(self):
        """Clear logs"""
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _start_runtime_refresh_loop(self):
        """Keep embedded runtime widgets in sync with local runtime storage."""
        if self._runtime_refresh_task:
            return

        def _tick():
            self._runtime_refresh_task = None
            if not self.root.winfo_exists():
                return
            self._refresh_runtime_views()
            self._runtime_refresh_task = self.root.after(3000, _tick)

        self._runtime_refresh_task = self.root.after(500, _tick)

    def _open_runtime_viewer(self, select_tab: str = "session"):
        """Open a small runtime viewer for the active session and trade journal."""
        existing = getattr(self, "_runtime_viewer", None)
        if existing and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            if hasattr(self, "_runtime_notebook"):
                tab_index = 1 if select_tab == "journal" else 0
                self._runtime_notebook.select(tab_index)
            self._refresh_runtime_viewer()
            return

        viewer = tk.Toplevel(self.root)
        viewer.title("Runtime Viewer")
        viewer.geometry("950x650")
        viewer.transient(self.root)
        self._runtime_viewer = viewer

        toolbar = ttk.Frame(viewer, padding=(10, 10, 10, 0))
        toolbar.pack(fill="x")

        ttk.Button(toolbar, text="Refresh", command=self._refresh_runtime_viewer).pack(side="left")
        ttk.Label(
            toolbar,
            text="Shows persisted active session and recent trade journal events from runtime storage.",
        ).pack(side="left", padx=10)

        notebook = ttk.Notebook(viewer)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self._runtime_notebook = notebook

        session_frame = ttk.Frame(notebook)
        notebook.add(session_frame, text="Active Session")
        self._runtime_session_text = scrolledtext.ScrolledText(
            session_frame,
            wrap="none",
            font=("Consolas", 9),
        )
        self._runtime_session_text.pack(fill="both", expand=True)

        journal_frame = ttk.Frame(notebook)
        notebook.add(journal_frame, text="Trade Journal")
        self._runtime_journal_text = scrolledtext.ScrolledText(
            journal_frame,
            wrap="none",
            font=("Consolas", 9),
        )
        self._runtime_journal_text.pack(fill="both", expand=True)

        viewer.protocol("WM_DELETE_WINDOW", self._close_runtime_viewer)

        tab_index = 1 if select_tab == "journal" else 0
        notebook.select(tab_index)
        self._refresh_runtime_views()

    def _close_runtime_viewer(self):
        viewer = getattr(self, "_runtime_viewer", None)
        if viewer and viewer.winfo_exists():
            viewer.destroy()
        self._runtime_viewer = None

    def _set_runtime_text(self, widget: tk.Text, content: str):
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", content)
        widget.configure(state=tk.DISABLED)

    def _build_runtime_summary(self, session: Optional[Dict[str, Any]], journal: List[Dict[str, Any]]) -> str:
        if session:
            session_id = session.get("session_id", "-")
            pair = session.get("pair", "-")
            advice = (session.get("latest_monitor_advice") or {}).get("action", "-")
            return f"Session {session_id[:8]} | {pair} | advice: {advice} | journal: {len(journal)}"
        return f"No active session | journal: {len(journal)}"

    def _refresh_runtime_views(self):
        session = self.service.read_active_session()
        journal = self.service.read_recent_journal(limit=100)

        if session:
            session_text = json.dumps(session, indent=2, ensure_ascii=False)
        else:
            session_text = "No active session persisted."

        if journal:
            journal_chunks = []
            for event in journal:
                journal_chunks.append(
                    f"{event.get('timestamp', '-')}"
                    f" | {event.get('event_type', '-')}\n"
                    f"{json.dumps(event.get('payload', {}), indent=2, ensure_ascii=False)}"
                )
            journal_text = "\n\n" + ("\n" + ("-" * 80) + "\n\n").join(journal_chunks)
            journal_text = journal_text.lstrip()
        else:
            journal_text = "Trade journal is empty."

        if hasattr(self, "runtime_session_text") and self.runtime_session_text.winfo_exists():
            self._set_runtime_text(self.runtime_session_text, session_text)
        if hasattr(self, "runtime_journal_text") and self.runtime_journal_text.winfo_exists():
            self._set_runtime_text(self.runtime_journal_text, journal_text)
        if hasattr(self, "runtime_summary_label") and self.runtime_summary_label.winfo_exists():
            self.runtime_summary_label.config(text=self._build_runtime_summary(session, journal))

        viewer = getattr(self, "_runtime_viewer", None)
        if viewer and viewer.winfo_exists():
            self._set_runtime_text(self._runtime_session_text, session_text)
            self._set_runtime_text(self._runtime_journal_text, journal_text)

    def _refresh_runtime_viewer(self):
        """Backward-compatible wrapper for popup refresh buttons."""
        self._refresh_runtime_views()
    
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

        if self._runtime_refresh_task:
            self.root.after_cancel(self._runtime_refresh_task)
            self._runtime_refresh_task = None
        
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
