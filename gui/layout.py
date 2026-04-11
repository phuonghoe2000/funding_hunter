"""
GUI layout builders for Funding Hunter.
"""

from typing import Any

import tkinter as tk
from tkinter import scrolledtext, ttk

from config.constants import POPULAR_PAIRS, Exchange

try:
    from gui.exchange_display import TRADING_EXCHANGE_OPTIONS
except ImportError:
    from exchange_display import TRADING_EXCHANGE_OPTIONS


FUNDING_EXCHANGE_COLUMNS = {
    "OKX": Exchange.OKX,
    "Binance": Exchange.BINANCE,
    "BingX": Exchange.BINGX,
    "Gate": Exchange.GATE,
    "Asterdex": Exchange.ASTERDEX,
    "Bybit": Exchange.BYBIT,
}


def create_widgets(app: Any) -> None:
    """Create the top-level GUI layout."""
    main_frame = ttk.Frame(app.root, padding="10")
    main_frame.pack(fill=tk.BOTH, expand=True)
    app._exchange_columns = dict(FUNDING_EXCHANGE_COLUMNS)

    create_exchange_frame(app, main_frame)

    content_frame = ttk.Frame(main_frame)
    content_frame.pack(fill=tk.BOTH, expand=True, pady=10)
    create_trading_frame(app, content_frame)

    bottom_frame = ttk.Frame(main_frame)
    bottom_frame.pack(fill=tk.BOTH, expand=True)

    create_position_frame(app, bottom_frame)

    lower_pane = ttk.Panedwindow(bottom_frame, orient=tk.HORIZONTAL)
    lower_pane.pack(fill=tk.BOTH, expand=True)

    log_container = ttk.Frame(lower_pane, width=760)
    runtime_container = ttk.Frame(lower_pane, width=430)
    lower_pane.add(log_container, weight=3)
    lower_pane.add(runtime_container, weight=2)

    create_log_frame(app, log_container)
    create_runtime_frame(app, runtime_container)

    def _set_bottom_split() -> None:
        try:
            total_width = lower_pane.winfo_width()
            if total_width <= 1:
                app.root.after(50, _set_bottom_split)
                return

            target_width = max(560, min(860, int(total_width * 0.62)))
            lower_pane.sashpos(0, target_width)
        except tk.TclError:
            return

    app.root.after(50, _set_bottom_split)


def create_exchange_frame(app: Any, parent: tk.Widget) -> None:
    """Create the exchange credentials area."""
    frame = ttk.LabelFrame(parent, text="🔌 Exchange Credentials", padding="10")
    frame.pack(fill=tk.X, pady=(0, 10))

    notebook = ttk.Notebook(frame)
    notebook.pack(fill=tk.X, pady=5)

    _create_exchange_tab(
        app,
        notebook,
        title="OKX",
        prefix="okx",
        fields=[("API Key", "api_key", 40), ("Secret", "secret", 40), ("Passphrase", "passphrase", 20)],
        testnet_default=True,
        enabled_default=True,
    )
    _create_exchange_tab(
        app,
        notebook,
        title="Binance",
        prefix="binance",
        fields=[("API Key", "api_key", 40), ("Secret", "secret", 40)],
        testnet_default=True,
        enabled_default=True,
    )
    _create_exchange_tab(
        app,
        notebook,
        title="BingX",
        prefix="bingx",
        fields=[("API Key", "api_key", 40), ("Secret", "secret", 40)],
        testnet_default=None,
        enabled_default=False,
    )
    _create_exchange_tab(
        app,
        notebook,
        title="Gate.io",
        prefix="gate",
        fields=[("API Key", "api_key", 40), ("Secret", "secret", 40)],
        testnet_default=False,
        show_testnet=False,
        enabled_default=False,
    )
    _create_exchange_tab(
        app,
        notebook,
        title="Asterdex",
        prefix="asterdex",
        fields=[("API Key", "api_key", 40), ("Secret", "secret", 40)],
        testnet_default=False,
        enabled_default=False,
    )
    _create_exchange_tab(
        app,
        notebook,
        title="Bybit",
        prefix="bybit",
        fields=[("API Key", "api_key", 40), ("Secret", "secret", 40)],
        testnet_default=False,
        enabled_default=False,
    )

    btn_frame = ttk.Frame(frame)
    btn_frame.pack(fill=tk.X, pady=10)

    app.connect_btn = ttk.Button(btn_frame, text="🔗 Connect All", command=app._connect)
    app.connect_btn.pack(side=tk.LEFT, padx=5)

    app.disconnect_btn = ttk.Button(
        btn_frame,
        text="🔌 Disconnect",
        command=app._disconnect,
        state=tk.DISABLED,
    )
    app.disconnect_btn.pack(side=tk.LEFT, padx=5)

    app.debug_mode = tk.BooleanVar(value=False)
    app.debug_checkbox = ttk.Checkbutton(
        btn_frame,
        text="🔍 Debug Mode",
        variable=app.debug_mode,
        command=app._on_debug_mode_changed,
    )
    app.debug_checkbox.pack(side=tk.LEFT, padx=10)

    app.status_label = ttk.Label(btn_frame, text="⚪ Disconnected")
    app.status_label.pack(side=tk.LEFT, padx=20)

    app.balance_frame = ttk.Frame(btn_frame)
    app.balance_frame.pack(side=tk.RIGHT, padx=10)

    for label_text, attr_name in (
        ("OKX", "okx_balance_label"),
        ("Binance", "binance_balance_label"),
        ("BingX", "bingx_balance_label"),
        ("Gate", "gate_balance_label"),
        ("Aster", "asterdex_balance_label"),
        ("Bybit", "bybit_balance_label"),
    ):
        ttk.Label(app.balance_frame, text=f"{label_text}:").pack(side=tk.LEFT, padx=2)
        value_label = ttk.Label(app.balance_frame, text="$0.00")
        value_label.pack(side=tk.LEFT, padx=5)
        setattr(app, attr_name, value_label)


def create_trading_frame(app: Any, parent: tk.Widget) -> None:
    """Create the trading panel."""
    outer_frame = ttk.LabelFrame(parent, text="📊 Trading Panel", padding="5")
    outer_frame.pack(fill=tk.BOTH, expand=True)

    canvas = tk.Canvas(outer_frame, highlightthickness=0)
    scrollbar = ttk.Scrollbar(outer_frame, orient="vertical", command=canvas.yview)

    frame = ttk.Frame(canvas)
    frame.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))

    content_window = canvas.create_window((0, 0), window=frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content_window, width=event.width))

    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _on_mousewheel(event: tk.Event) -> None:
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _bind_mousewheel(_: tk.Event) -> None:
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def _unbind_mousewheel(_: tk.Event) -> None:
        canvas.unbind_all("<MouseWheel>")

    canvas.bind("<Enter>", _bind_mousewheel)
    canvas.bind("<Leave>", _unbind_mousewheel)

    pair_frame = ttk.Frame(frame)
    pair_frame.pack(fill=tk.X, pady=5)

    ttk.Label(pair_frame, text="Pair:").pack(side=tk.LEFT, padx=5)
    app.pair_combo = ttk.Combobox(pair_frame, values=POPULAR_PAIRS, width=15)
    app.pair_combo.set("BTC/USDT")
    app.pair_combo.pack(side=tk.LEFT, padx=5)
    app.pair_combo.bind("<<ComboboxSelected>>", app._on_pair_changed)

    app.current_price_label = ttk.Label(pair_frame, text="", foreground="blue")
    app.current_price_label.pack(side=tk.LEFT, padx=10)

    app.load_pairs_btn = ttk.Button(
        pair_frame,
        text="📋 Load All Binance Pairs",
        command=app._load_binance_pairs,
        state=tk.DISABLED,
    )
    app.load_pairs_btn.pack(side=tk.LEFT, padx=5)

    settings_frame = ttk.Frame(frame)
    settings_frame.pack(fill=tk.X, pady=5)

    ttk.Label(settings_frame, text="Leverage:").pack(side=tk.LEFT, padx=5)
    app.leverage_var = tk.StringVar(value="3")
    app.leverage_spin = ttk.Spinbox(settings_frame, from_=1, to=100, width=5, textvariable=app.leverage_var)
    app.leverage_spin.pack(side=tk.LEFT, padx=5)

    ttk.Label(settings_frame, text="Size:").pack(side=tk.LEFT, padx=10)
    app.size_entry = ttk.Entry(settings_frame, width=12)
    app.size_entry.insert(0, "0.01")
    app.size_entry.pack(side=tk.LEFT, padx=5)

    app.usdt_vol_label = ttk.Label(settings_frame, text="≈ $0.00 USDT", foreground="blue")
    app.usdt_vol_label.pack(side=tk.LEFT, padx=5)
    app.size_entry.bind("<KeyRelease>", app._update_usdt_volume)

    ttk.Label(settings_frame, text="Splits:").pack(side=tk.LEFT, padx=10)
    app.split_count_var = tk.StringVar(value="1")
    app.split_count_entry = ttk.Entry(settings_frame, textvariable=app.split_count_var, width=5)
    app.split_count_entry.pack(side=tk.LEFT, padx=5)

    ttk.Label(settings_frame, text="Split Interval (s):").pack(side=tk.LEFT, padx=10)
    app.split_interval_var = tk.StringVar(value="2.0")
    app.split_interval_entry = ttk.Entry(settings_frame, textvariable=app.split_interval_var, width=6)
    app.split_interval_entry.pack(side=tk.LEFT, padx=5)

    ex_frame = ttk.LabelFrame(frame, text="Select Exchanges for Arbitrage", padding="10")
    ex_frame.pack(fill=tk.X, pady=10)

    ttk.Label(ex_frame, text="LONG Exchange:", style="Header.TLabel").grid(
        row=0, column=0, padx=5, pady=5, sticky="e"
    )
    app.long_exchange = ttk.Combobox(ex_frame, values=TRADING_EXCHANGE_OPTIONS, width=12, state="readonly")
    app.long_exchange.set("OKX")
    app.long_exchange.grid(row=0, column=1, padx=5, pady=5)
    app.long_exchange.bind("<<ComboboxSelected>>", app._on_exchange_changed)

    ttk.Label(ex_frame, text="SHORT Exchange:", style="Header.TLabel").grid(
        row=0, column=2, padx=15, pady=5, sticky="e"
    )
    app.short_exchange = ttk.Combobox(ex_frame, values=TRADING_EXCHANGE_OPTIONS, width=12, state="readonly")
    app.short_exchange.set("Binance")
    app.short_exchange.grid(row=0, column=3, padx=5, pady=5)
    app.short_exchange.bind("<<ComboboxSelected>>", app._on_exchange_changed)

    funding_info_frame = ttk.LabelFrame(frame, text="📊 Selected Pair Info", padding="10")
    funding_info_frame.pack(fill=tk.X, pady=10)

    app.selected_pair_info = ttk.Label(
        funding_info_frame,
        text="Double-click a pair in Funding Rates panel to see details",
        foreground="gray",
        wraplength=460,
        justify=tk.LEFT,
    )
    app.selected_pair_info.pack(fill=tk.X)

    create_market_summary_frame(app, frame)

    btn_frame = ttk.Frame(frame)
    btn_frame.pack(fill=tk.X, pady=15)

    app.open_btn = ttk.Button(
        btn_frame,
        text="🚀 Open Hedged Position",
        command=app._open_position,
        state=tk.DISABLED,
    )
    app.open_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

    app.close_btn = ttk.Button(
        btn_frame,
        text="🛑 Close Position",
        command=app._close_position,
        state=tk.DISABLED,
    )
    app.close_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

    partial_frame = ttk.Frame(btn_frame)
    partial_frame.pack(side=tk.LEFT, padx=2)
    ttk.Label(partial_frame, text="Size:", font=("Segoe UI", 8)).pack(side=tk.LEFT)
    app.close_size_var = tk.StringVar(value="")
    ttk.Entry(partial_frame, textvariable=app.close_size_var, width=10).pack(side=tk.LEFT, padx=2)
    ttk.Label(partial_frame, text="(empty=all)", font=("Segoe UI", 7), foreground="gray").pack(side=tk.LEFT)

    app.load_pos_btn = ttk.Button(
        btn_frame,
        text="📥 Load Positions",
        command=app._load_existing_positions,
        state=tk.DISABLED,
    )
    app.load_pos_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

    app.monitor_btn = ttk.Button(
        btn_frame,
        text="👁 Start Monitor",
        command=app._toggle_monitoring,
        state=tk.DISABLED,
    )
    app.monitor_btn.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)

    option_frame = ttk.Frame(frame)
    option_frame.pack(fill=tk.X, pady=5)

    risk_frame = ttk.Frame(option_frame)
    risk_frame.pack(anchor=tk.W)

    app.auto_close_risk_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(risk_frame, text="Auto-reduce 50% when Risk >=", variable=app.auto_close_risk_var).pack(side=tk.LEFT)
    app.risk_threshold_var = tk.StringVar(value="10")
    ttk.Entry(risk_frame, textvariable=app.risk_threshold_var, width=5).pack(side=tk.LEFT, padx=2)
    ttk.Label(risk_frame, text="%").pack(side=tk.LEFT)

    liq_frame = ttk.Frame(option_frame)
    liq_frame.pack(anchor=tk.W, pady=2)
    app.auto_reduce_liq_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        liq_frame,
        text="Auto-reduce 50% when Liq Distance <=",
        variable=app.auto_reduce_liq_var,
    ).pack(side=tk.LEFT)
    app.liq_distance_threshold_var = tk.StringVar(value="3")
    ttk.Entry(liq_frame, textvariable=app.liq_distance_threshold_var, width=5).pack(side=tk.LEFT, padx=2)
    ttk.Label(liq_frame, text="%").pack(side=tk.LEFT)

    app.auto_close_reversal_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        option_frame,
        text="Auto-close on funding reversal",
        variable=app.auto_close_reversal_var,
    ).pack(anchor=tk.W, pady=2)

    app.skip_leverage_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(option_frame, text="Skip leverage set (faster entry)", variable=app.skip_leverage_var).pack(
        anchor=tk.W
    )

    app.skip_spread_check_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        option_frame,
        text="Skip spread check (execute splits immediately)",
        variable=app.skip_spread_check_var,
    ).pack(anchor=tk.W, pady=2)

    threshold_frame = ttk.Frame(option_frame)
    threshold_frame.pack(fill=tk.X, pady=5)

    ttk.Label(threshold_frame, text="Price Spread Min:").pack(side=tk.LEFT)
    app.price_spread_threshold = ttk.Entry(threshold_frame, width=8)
    app.price_spread_threshold.insert(0, "0.05")
    app.price_spread_threshold.pack(side=tk.LEFT, padx=3)
    ttk.Label(threshold_frame, text="%").pack(side=tk.LEFT, padx=(0, 10))

    ttk.Label(threshold_frame, text="Funding Spread Min:").pack(side=tk.LEFT, padx=(10, 0))
    app.min_spread_threshold = ttk.Entry(threshold_frame, width=8)
    app.min_spread_threshold.insert(0, "0.01")
    app.min_spread_threshold.pack(side=tk.LEFT, padx=3)
    ttk.Label(threshold_frame, text="%").pack(side=tk.LEFT)


def create_market_summary_frame(app: Any, parent: tk.Widget) -> None:
    """Create a compact funding summary for the main window."""
    frame = ttk.LabelFrame(parent, text="Market Summary", padding="10")
    frame.pack(fill=tk.BOTH, expand=True, pady=10)

    btn_frame = ttk.Frame(frame)
    btn_frame.pack(fill=tk.X, pady=(0, 6))

    app.open_funding_board_btn = ttk.Button(
        btn_frame,
        text="Open Funding Board",
        command=app._open_funding_board,
        state=tk.DISABLED,
    )
    app.open_funding_board_btn.pack(side=tk.LEFT)

    app.refresh_funding_btn = ttk.Button(
        btn_frame,
        text="Refresh Funding",
        command=app._refresh_funding,
        state=tk.DISABLED,
    )
    app.refresh_funding_btn.pack(side=tk.LEFT, padx=6)

    app.market_summary_status = ttk.Label(
        btn_frame,
        text="No funding data loaded",
        foreground="gray",
    )
    app.market_summary_status.pack(side=tk.LEFT, padx=10)

    columns = ("Pair", "Net Edge", "Recommendation")
    app.market_summary_tree = ttk.Treeview(frame, columns=columns, show="headings", height=5)
    app.market_summary_tree.heading("Pair", text="Pair")
    app.market_summary_tree.heading("Net Edge", text="Net Edge")
    app.market_summary_tree.heading("Recommendation", text="Recommendation")
    app.market_summary_tree.column("Pair", width=110, stretch=False)
    app.market_summary_tree.column("Net Edge", width=90, stretch=False)
    app.market_summary_tree.column("Recommendation", width=260, stretch=True)
    app.market_summary_tree.pack(fill=tk.BOTH, expand=True)
    app.market_summary_tree.bind("<Double-1>", app._on_funding_select)


def create_funding_board_frame(app: Any, parent: tk.Widget) -> None:
    """Create the detachable funding board contents."""
    frame = ttk.Frame(parent, padding="10")
    frame.pack(fill=tk.BOTH, expand=True)

    btn_frame = ttk.Frame(frame)
    btn_frame.pack(fill=tk.X, pady=(0, 8))

    app.funding_board_refresh_btn = ttk.Button(
        btn_frame,
        text="Refresh Rates",
        command=app._refresh_funding,
        state=tk.DISABLED,
    )
    app.funding_board_refresh_btn.pack(side=tk.LEFT)

    ttk.Label(
        btn_frame,
        text="Double-click a row to push pair and exchanges back into the trading panel.",
        foreground="gray",
    ).pack(side=tk.LEFT, padx=10)

    table_frame = ttk.Frame(frame)
    table_frame.pack(fill=tk.BOTH, expand=True)

    columns = (
        "Pair",
        "OKX",
        "Binance",
        "BingX",
        "Gate",
        "Asterdex",
        "Bybit",
        "Gross 4H",
        "Cost",
        "Net Edge",
        "Recommendation",
    )
    app.funding_board_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)

    for name, text in (
        ("Pair", "Pair"),
        ("OKX", "OKX Rate"),
        ("Binance", "Binance Rate"),
        ("BingX", "BingX Rate"),
        ("Gate", "Gate Rate"),
        ("Asterdex", "Aster Rate"),
        ("Bybit", "Bybit Rate"),
        ("Gross 4H", "Gross 4H"),
        ("Cost", "Cost"),
        ("Net Edge", "Net Edge"),
        ("Recommendation", "Recommendation"),
    ):
        app.funding_board_tree.heading(name, text=text)

    for name, width in (
        ("Pair", 92),
        ("OKX", 86),
        ("Binance", 86),
        ("BingX", 86),
        ("Gate", 86),
        ("Asterdex", 86),
        ("Bybit", 86),
        ("Gross 4H", 90),
        ("Cost", 78),
        ("Net Edge", 90),
        ("Recommendation", 180),
    ):
        app.funding_board_tree.column(name, width=width, stretch=False)

    y_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=app.funding_board_tree.yview)
    x_scrollbar = ttk.Scrollbar(table_frame, orient="horizontal", command=app.funding_board_tree.xview)
    app.funding_board_tree.configure(
        yscrollcommand=y_scrollbar.set,
        xscrollcommand=x_scrollbar.set,
    )

    app.funding_board_tree.grid(row=0, column=0, sticky="nsew")
    y_scrollbar.grid(row=0, column=1, sticky="ns")
    x_scrollbar.grid(row=1, column=0, sticky="ew")

    table_frame.grid_rowconfigure(0, weight=1)
    table_frame.grid_columnconfigure(0, weight=1)

    app.funding_board_tree.bind("<Double-1>", app._on_funding_select)


def create_position_frame(app: Any, parent: tk.Widget) -> None:
    """Create the active position summary."""
    frame = ttk.LabelFrame(parent, text="📈 Active Position", padding="10")
    frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

    app.position_info = ttk.Frame(frame)
    app.position_info.pack(fill=tk.X, pady=5)

    app.pos_pair_label = ttk.Label(app.position_info, text="Pair: -")
    app.pos_pair_label.pack(side=tk.LEFT, padx=10)

    app.pos_long_label = ttk.Label(app.position_info, text="Long: -")
    app.pos_long_label.pack(side=tk.LEFT, padx=10)

    app.pos_short_label = ttk.Label(app.position_info, text="Short: -")
    app.pos_short_label.pack(side=tk.LEFT, padx=10)

    app.pos_size_label = ttk.Label(app.position_info, text="Size: -")
    app.pos_size_label.pack(side=tk.LEFT, padx=10)

    app.pos_pnl_label = ttk.Label(app.position_info, text="Risk: 0% | Long: $0 | Short: $0")
    app.pos_pnl_label.pack(side=tk.LEFT, padx=10)

    app.pos_status_label = ttk.Label(app.position_info, text="Status: No Position")
    app.pos_status_label.pack(side=tk.RIGHT, padx=10)


def create_log_frame(app: Any, parent: tk.Widget) -> None:
    """Create the log output area."""
    frame = ttk.LabelFrame(parent, text="📝 Logs", padding="10")
    frame.pack(fill=tk.BOTH, expand=True)

    app.log_text = scrolledtext.ScrolledText(frame, height=8, state=tk.DISABLED)
    app.log_text.pack(fill=tk.BOTH, expand=True)

    ttk.Button(frame, text="Clear", command=app._clear_logs).pack(side=tk.RIGHT, pady=5)


def create_runtime_frame(app: Any, parent: tk.Widget) -> None:
    """Create the embedded runtime session/journal panel."""
    frame = ttk.LabelFrame(parent, text="Runtime", padding="10")
    frame.pack(fill=tk.BOTH, expand=True, padx=(5, 0))

    toolbar = ttk.Frame(frame)
    toolbar.pack(fill=tk.X, pady=(0, 8))

    ttk.Button(toolbar, text="Refresh", command=app._refresh_runtime_views).pack(side=tk.LEFT)
    app.runtime_summary_label = ttk.Label(toolbar, text="No runtime data loaded", foreground="gray")
    app.runtime_summary_label.pack(side=tk.LEFT, padx=10)

    app.runtime_notebook = ttk.Notebook(frame)
    app.runtime_notebook.pack(fill=tk.BOTH, expand=True)

    session_frame = ttk.Frame(app.runtime_notebook)
    app.runtime_notebook.add(session_frame, text="Active Session")
    app.runtime_session_text = scrolledtext.ScrolledText(
        session_frame,
        height=10,
        wrap="none",
        font=("Consolas", 9),
        state=tk.DISABLED,
    )
    app.runtime_session_text.pack(fill=tk.BOTH, expand=True)

    journal_frame = ttk.Frame(app.runtime_notebook)
    app.runtime_notebook.add(journal_frame, text="Trade Journal")
    app.runtime_journal_text = scrolledtext.ScrolledText(
        journal_frame,
        height=10,
        wrap="none",
        font=("Consolas", 9),
        state=tk.DISABLED,
    )
    app.runtime_journal_text.pack(fill=tk.BOTH, expand=True)


def _create_exchange_tab(
    app: Any,
    notebook: ttk.Notebook,
    *,
    title: str,
    prefix: str,
    fields: list[tuple[str, str, int]],
    testnet_default: bool | None,
    enabled_default: bool,
    show_testnet: bool = True,
) -> None:
    """Create a single exchange credentials tab."""
    frame = ttk.Frame(notebook, padding="10")
    notebook.add(frame, text=title)

    column = 0
    for label_text, attr_suffix, width in fields:
        ttk.Label(frame, text=f"{label_text}:").grid(row=0, column=column, padx=5, pady=2, sticky="e")
        entry = ttk.Entry(frame, width=width, show="*")
        entry.grid(row=0, column=column + 1, padx=5, pady=2)
        setattr(app, f"{prefix}_{attr_suffix}", entry)
        column += 2

    if testnet_default is None:
        ttk.Label(frame, text="(No Testnet)", foreground="gray").grid(row=0, column=column, padx=10)
        column += 1
    else:
        testnet_var = tk.BooleanVar(value=testnet_default)
        setattr(app, f"{prefix}_testnet", testnet_var)
        if show_testnet:
            ttk.Checkbutton(frame, text="Testnet", variable=testnet_var).grid(row=0, column=column, padx=10)
        column += 1

    if testnet_default is not None and not hasattr(app, f"{prefix}_testnet"):
        setattr(app, f"{prefix}_testnet", tk.BooleanVar(value=testnet_default))

    enabled_var = tk.BooleanVar(value=enabled_default)
    setattr(app, f"{prefix}_enabled", enabled_var)
    ttk.Checkbutton(frame, text="Enable", variable=enabled_var).grid(row=0, column=column, padx=5)
