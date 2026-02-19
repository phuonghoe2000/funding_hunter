"""
Scalping Mode GUI - Separate window for scalping strategy

This provides a dedicated interface for the BingX scalping strategy
with DCA and RSI-based signals.
"""

import asyncio
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import logging

from core.scalping_bot import ScalpingBot, Notification, NotificationType, create_scalping_bot
from core.scalping_strategy import ScalpingConfig, ScalpingPosition
from config.constants import Exchange

logger = logging.getLogger(__name__)


class ScalpingGUI:
    """
    Dedicated GUI for Scalping Strategy
    
    Can be launched as a separate window from the main Funding Hunter GUI
    or standalone.
    """
    
    def __init__(
        self, 
        parent: Optional[tk.Tk] = None,
        bingx_client = None,
        binance_client = None,
        loop: Optional[asyncio.AbstractEventLoop] = None
    ):
        """
        Initialize Scalping GUI
        
        Args:
            parent: Parent tkinter window (if launched from main GUI)
            bingx_client: BingX client (required)
            binance_client: Binance client (required)
            loop: Existing asyncio event loop
        """
        # Create window
        if parent:
            self.root = tk.Toplevel(parent)
        else:
            self.root = tk.Tk()
        
        self.root.title("🎯 BingX Scalping - DCA Strategy")
        self.root.geometry("1200x800")
        self.root.minsize(1000, 700)
        
        # Exchange clients
        self.bingx = bingx_client
        self.binance = binance_client
        
        # Async loop
        self.loop = loop
        self._own_loop = False
        if not self.loop:
            self._start_async_loop()
            self._own_loop = True
        
        # Bot
        self.bot: Optional[ScalpingBot] = None
        
        # UI State
        self.running = False
        
        # Style
        self.style = ttk.Style()
        self.style.configure('Title.TLabel', font=('Helvetica', 14, 'bold'))
        self.style.configure('Header.TLabel', font=('Helvetica', 11, 'bold'))
        self.style.configure('Success.TLabel', foreground='green')
        self.style.configure('Warning.TLabel', foreground='orange')
        self.style.configure('Error.TLabel', foreground='red')
        self.style.configure('DCA.TLabel', foreground='blue', font=('Helvetica', 10, 'bold'))
        
        # Build UI
        self._create_widgets()
        
        # Start update loop
        self._update_ui()
        
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
    
    def _start_async_loop(self):
        """Start async event loop in background thread"""
        def run_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()
        
        self.async_thread = threading.Thread(target=run_loop, daemon=True)
        self.async_thread.start()
        
        # Wait for loop to be ready
        import time
        while self.loop is None:
            time.sleep(0.01)
    
    def _run_async(self, coro):
        """Run coroutine in async loop"""
        if self.loop:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None
    
    def _create_widgets(self):
        """Create all widgets"""
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Top: Config and Controls
        top_frame = ttk.Frame(main_frame)
        top_frame.pack(fill=tk.X, pady=(0, 10))
        
        self._create_config_frame(top_frame)
        self._create_control_frame(top_frame)
        
        # Middle: Positions and Notifications
        middle_frame = ttk.Frame(main_frame)
        middle_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        
        self._create_positions_frame(middle_frame)
        self._create_notifications_frame(middle_frame)
        
        # Bottom: Log
        self._create_log_frame(main_frame)
    
    def _create_config_frame(self, parent):
        """Create configuration frame"""
        frame = ttk.LabelFrame(parent, text="⚙️ Strategy Config", padding="10")
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        
        # Grid layout for config
        row = 0
        
        # DCA Levels
        ttk.Label(frame, text="DCA Levels (%):").grid(row=row, column=0, sticky='e', padx=5, pady=2)
        self.dca_levels_entry = ttk.Entry(frame, width=30)
        self.dca_levels_entry.insert(0, "-0.3, -0.6, -1.0, -1.5, -2.0")
        self.dca_levels_entry.grid(row=row, column=1, padx=5, pady=2, sticky='w')
        row += 1
        
        # Max DCA Times
        ttk.Label(frame, text="Max DCA Times:").grid(row=row, column=0, sticky='e', padx=5, pady=2)
        self.max_dca_var = tk.StringVar(value="3")
        self.max_dca_spin = ttk.Spinbox(frame, from_=1, to=10, width=5, textvariable=self.max_dca_var)
        self.max_dca_spin.grid(row=row, column=1, padx=5, pady=2, sticky='w')
        row += 1
        
        # Take Profit
        ttk.Label(frame, text="Take Profit (%):").grid(row=row, column=0, sticky='e', padx=5, pady=2)
        self.take_profit_var = tk.StringVar(value="0.5")
        self.take_profit_entry = ttk.Entry(frame, width=10, textvariable=self.take_profit_var)
        self.take_profit_entry.grid(row=row, column=1, padx=5, pady=2, sticky='w')
        row += 1
        
        # RSI Period
        ttk.Label(frame, text="RSI Period:").grid(row=row, column=0, sticky='e', padx=5, pady=2)
        self.rsi_period_var = tk.StringVar(value="14")
        self.rsi_period_spin = ttk.Spinbox(frame, from_=5, to=30, width=5, textvariable=self.rsi_period_var)
        self.rsi_period_spin.grid(row=row, column=1, padx=5, pady=2, sticky='w')
        row += 1
        
        # RSI Thresholds
        ttk.Label(frame, text="RSI Oversold/Overbought:").grid(row=row, column=0, sticky='e', padx=5, pady=2)
        rsi_frame = ttk.Frame(frame)
        rsi_frame.grid(row=row, column=1, padx=5, pady=2, sticky='w')
        
        self.rsi_oversold_var = tk.StringVar(value="30")
        ttk.Entry(rsi_frame, width=5, textvariable=self.rsi_oversold_var).pack(side=tk.LEFT)
        ttk.Label(rsi_frame, text=" / ").pack(side=tk.LEFT)
        self.rsi_overbought_var = tk.StringVar(value="70")
        ttk.Entry(rsi_frame, width=5, textvariable=self.rsi_overbought_var).pack(side=tk.LEFT)
        row += 1
        
        # Max Drawdown
        ttk.Label(frame, text="Max Drawdown (%):").grid(row=row, column=0, sticky='e', padx=5, pady=2)
        self.max_drawdown_var = tk.StringVar(value="5.0")
        self.max_drawdown_entry = ttk.Entry(frame, width=10, textvariable=self.max_drawdown_var)
        self.max_drawdown_entry.grid(row=row, column=1, padx=5, pady=2, sticky='w')
    
    def _create_control_frame(self, parent):
        """Create control buttons frame"""
        frame = ttk.LabelFrame(parent, text="🎮 Controls", padding="10")
        frame.pack(side=tk.LEFT, fill=tk.BOTH, padx=(5, 0))
        
        # Status
        self.status_label = ttk.Label(frame, text="⚪ Stopped", style='Header.TLabel')
        self.status_label.pack(pady=5)
        
        # Start/Stop buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=10)
        
        self.start_btn = ttk.Button(btn_frame, text="▶️ Start", command=self._start_bot, width=12)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(btn_frame, text="⏹️ Stop", command=self._stop_bot, width=12, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        # Quick actions
        ttk.Separator(frame, orient='horizontal').pack(fill=tk.X, pady=10)
        
        ttk.Label(frame, text="Quick Actions:").pack()
        
        self.refresh_btn = ttk.Button(frame, text="🔄 Refresh Positions", command=self._refresh_positions)
        self.refresh_btn.pack(pady=5, fill=tk.X)
        
        self.clear_notif_btn = ttk.Button(frame, text="🗑️ Clear Notifications", command=self._clear_notifications)
        self.clear_notif_btn.pack(pady=5, fill=tk.X)
    
    def _create_positions_frame(self, parent):
        """Create positions display frame"""
        frame = ttk.LabelFrame(parent, text="📊 Tracked Positions", padding="10")
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        
        # Treeview for positions
        columns = ('pair', 'bingx_side', 'size', 'dca', 'pnl', 'rsi', 'status')
        self.positions_tree = ttk.Treeview(frame, columns=columns, show='headings', height=8)
        
        self.positions_tree.heading('pair', text='Pair')
        self.positions_tree.heading('bingx_side', text='BingX Side')
        self.positions_tree.heading('size', text='Size')
        self.positions_tree.heading('dca', text='DCA Count')
        self.positions_tree.heading('pnl', text='Total PnL %')
        self.positions_tree.heading('rsi', text='RSI')
        self.positions_tree.heading('status', text='Status')
        
        self.positions_tree.column('pair', width=80)
        self.positions_tree.column('bingx_side', width=80)
        self.positions_tree.column('size', width=80)
        self.positions_tree.column('dca', width=70)
        self.positions_tree.column('pnl', width=80)
        self.positions_tree.column('rsi', width=60)
        self.positions_tree.column('status', width=70)
        
        # Scrollbar
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.positions_tree.yview)
        self.positions_tree.configure(yscrollcommand=scrollbar.set)
        
        self.positions_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Summary
        summary_frame = ttk.Frame(frame)
        summary_frame.pack(fill=tk.X, pady=(5, 0))
        
        self.summary_label = ttk.Label(summary_frame, text="No positions tracked")
        self.summary_label.pack(side=tk.LEFT)
    
    def _create_notifications_frame(self, parent):
        """Create notifications display frame"""
        frame = ttk.LabelFrame(parent, text="🔔 Notifications / Signals", padding="10")
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))
        
        # Notifications list
        columns = ('time', 'type', 'message')
        self.notif_tree = ttk.Treeview(frame, columns=columns, show='headings', height=8)
        
        self.notif_tree.heading('time', text='Time')
        self.notif_tree.heading('type', text='Type')
        self.notif_tree.heading('message', text='Message')
        
        self.notif_tree.column('time', width=80)
        self.notif_tree.column('type', width=100)
        self.notif_tree.column('message', width=300)
        
        # Tags for coloring
        self.notif_tree.tag_configure('DCA_SIGNAL', foreground='blue')
        self.notif_tree.tag_configure('DCA_EXECUTE', foreground='green')
        self.notif_tree.tag_configure('TAKE_PROFIT', foreground='darkgreen')
        self.notif_tree.tag_configure('STOP_LOSS', foreground='red')
        self.notif_tree.tag_configure('WARNING', foreground='orange')
        self.notif_tree.tag_configure('ERROR', foreground='red')
        
        # Scrollbar
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.notif_tree.yview)
        self.notif_tree.configure(yscrollcommand=scrollbar.set)
        
        self.notif_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Action buttons for selected signal
        action_frame = ttk.Frame(frame)
        action_frame.pack(fill=tk.X, pady=(5, 0))
        
        ttk.Label(action_frame, text="Selected signal action:").pack(side=tk.LEFT, padx=5)
        self.execute_btn = ttk.Button(action_frame, text="✅ Execute", command=self._execute_selected, state=tk.DISABLED)
        self.execute_btn.pack(side=tk.LEFT, padx=5)
        
        self.dismiss_btn = ttk.Button(action_frame, text="❌ Dismiss", command=self._dismiss_selected, state=tk.DISABLED)
        self.dismiss_btn.pack(side=tk.LEFT, padx=5)
    
    def _create_log_frame(self, parent):
        """Create log frame"""
        frame = ttk.LabelFrame(parent, text="📋 Log", padding="5")
        frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        
        self.log_text = scrolledtext.ScrolledText(frame, height=8, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
    
    def _log(self, message: str):
        """Add message to log"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def _get_config(self) -> ScalpingConfig:
        """Get config from UI"""
        # Parse DCA levels
        dca_str = self.dca_levels_entry.get()
        dca_levels = [float(x.strip()) for x in dca_str.split(',')]
        
        return ScalpingConfig(
            dca_levels=dca_levels,
            max_dca_times=int(self.max_dca_var.get()),
            take_profit_pct=float(self.take_profit_var.get()),
            rsi_period=int(self.rsi_period_var.get()),
            rsi_oversold=int(self.rsi_oversold_var.get()),
            rsi_overbought=int(self.rsi_overbought_var.get()),
            max_drawdown_pct=float(self.max_drawdown_var.get())
        )
    
    def _start_bot(self):
        """Start the scalping bot"""
        if not self.bingx or not self.binance:
            messagebox.showerror("Error", "BingX and Binance clients must be connected first!")
            return
        
        try:
            config = self._get_config()
            
            # Create bot
            self.bot = ScalpingBot(self.bingx, self.binance, config)
            
            # Add notification callback
            self.bot.add_notification_callback(self._on_notification)
            
            # Start bot
            self._run_async(self.bot.start())
            
            self.running = True
            self.status_label.config(text="🟢 Running")
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            
            self._log("✅ Scalping bot started")
            self._log(f"   DCA Levels: {config.dca_levels}")
            self._log(f"   Take Profit: {config.take_profit_pct}%")
            self._log(f"   RSI: {config.rsi_period} period, {config.rsi_oversold}/{config.rsi_overbought}")
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start bot: {e}")
            self._log(f"❌ Error starting bot: {e}")
    
    def _stop_bot(self):
        """Stop the scalping bot"""
        if self.bot:
            self._run_async(self.bot.stop())
            self.bot = None
        
        self.running = False
        self.status_label.config(text="⚪ Stopped")
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        
        self._log("⏹️ Scalping bot stopped")
    
    def _on_notification(self, notification: Notification):
        """Handle notification from bot"""
        # Add to tree
        time_str = notification.timestamp.strftime("%H:%M:%S")
        tag = notification.type.value
        
        self.notif_tree.insert('', 0, values=(time_str, tag, notification.message), tags=(tag,))
        
        # Log
        self._log(f"[{tag}] {notification.message}")
        
        # Play sound for important notifications
        if notification.type in [NotificationType.DCA_SIGNAL, NotificationType.DCA_EXECUTE, NotificationType.STOP_LOSS]:
            self.root.bell()
    
    def _refresh_positions(self):
        """Refresh positions display"""
        if not self.bot:
            return
        
        # Clear tree
        for item in self.positions_tree.get_children():
            self.positions_tree.delete(item)
        
        # Add positions
        positions = self.bot.get_all_positions()
        
        for pair, pos in positions.items():
            status = "Active" if pos.is_active else "Inactive"
            pnl_str = f"{pos.total_pnl:.2f}%"
            
            self.positions_tree.insert('', tk.END, values=(
                pair,
                pos.bingx_side,
                f"{pos.base_size:.4f}",
                pos.dca_count,
                pnl_str,
                f"{pos.current_rsi:.1f}",
                status
            ))
        
        # Update summary
        if positions:
            total_pnl = sum(p.total_pnl for p in positions.values())
            self.summary_label.config(text=f"Positions: {len(positions)} | Total PnL: {total_pnl:.2f}%")
        else:
            self.summary_label.config(text="No positions tracked")
    
    def _clear_notifications(self):
        """Clear notifications display"""
        for item in self.notif_tree.get_children():
            self.notif_tree.delete(item)
        
        if self.bot:
            self.bot.notifications.clear_history()
        
        self._log("Notifications cleared")
    
    def _execute_selected(self):
        """Execute selected signal (manual action)"""
        selection = self.notif_tree.selection()
        if not selection:
            return
        
        # Get selected notification
        item = self.notif_tree.item(selection[0])
        msg_type = item['values'][1]
        message = item['values'][2]
        
        self._log(f"⚠️ Manual execution requested for: {message}")
        messagebox.showinfo("Execute Signal", 
            f"Signal: {msg_type}\n\nMessage: {message}\n\n"
            "NOTE: This is notification-only mode. "
            "Please execute the trade manually on the exchange.")
    
    def _dismiss_selected(self):
        """Dismiss selected signal"""
        selection = self.notif_tree.selection()
        if selection:
            self.notif_tree.delete(selection[0])
            self._log("Signal dismissed")
    
    def _update_ui(self):
        """Periodic UI update"""
        if self.running and self.bot:
            self._refresh_positions()
        
        # Enable/disable action buttons based on selection
        selection = self.notif_tree.selection()
        state = tk.NORMAL if selection else tk.DISABLED
        self.execute_btn.config(state=state)
        self.dismiss_btn.config(state=state)
        
        # Schedule next update
        self.root.after(2000, self._update_ui)
    
    def _on_closing(self):
        """Handle window close"""
        if self.running:
            if messagebox.askyesno("Confirm", "Bot is still running. Stop and close?"):
                self._stop_bot()
            else:
                return
        
        if self._own_loop and self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        
        self.root.destroy()
    
    def run(self):
        """Run the GUI (only if standalone)"""
        self.root.mainloop()


def launch_scalping_gui(
    parent: Optional[tk.Tk] = None,
    bingx_client = None,
    binance_client = None,
    loop: Optional[asyncio.AbstractEventLoop] = None
) -> ScalpingGUI:
    """
    Launch the Scalping GUI
    
    Args:
        parent: Parent window (optional)
        bingx_client: BingX client
        binance_client: Binance client
        loop: Existing async loop
    
    Returns:
        ScalpingGUI instance
    """
    gui = ScalpingGUI(parent, bingx_client, binance_client, loop)
    return gui


# Standalone launch
if __name__ == "__main__":
    # For testing - would need to create mock clients
    print("Scalping GUI - Standalone mode")
    print("NOTE: Clients must be provided for actual operation")
    
    gui = ScalpingGUI()
    gui.run()
