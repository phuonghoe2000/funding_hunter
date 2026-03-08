import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

with open('ethusdt_15m_1000.json') as f:
    klines = json.load(f)

base = dict(
    price_change_threshold=1.0, lookback_candles=7,
    position_volume_usdt=20.0, leverage=5,
    take_profit_pct=2.0, stop_loss_pct=2.0,
    use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
    use_volume_confirmation=True, volume_multiplier=1.5, volume_lookback=20,
    use_rsi_filter=True, rsi_period=14, rsi_overbought=70.0, rsi_oversold=30.0,
    cooldown_candles=4, max_momentum_pct=2.5
)

tests = [
    ('MinATR OFF',  0.0),
    ('MinATR 0.3%', 0.3),
    ('MinATR 0.4%', 0.4),
    ('MinATR 0.5%', 0.5),
    ('MinATR 0.6%', 0.6),
    ('MinATR 0.8%', 0.8),
]

print(f"  {'Label':<14}  {'Trades':>6}  {'Win':>4}  {'Loss':>4}  {'WR%':>6}  {'PnL':>8}  {'MaxDD':>7}  Skipped")
print("  " + "-" * 70)
for label, atr in tests:
    cfg = BacktestConfig(**base, min_atr_pct=atr)
    trades = BacktestEngine(cfg, silent=True).run(klines)
    real   = [t for t in trades if t.close_reason != 'END_OF_DATA']
    wins   = [t for t in real if t.pnl_usdt > 0]
    losses = [t for t in real if t.pnl_usdt <= 0]
    pnl    = sum(t.pnl_usdt for t in trades)
    eq = 0; peak = 0; dd = 0
    for t in trades:
        eq += t.pnl_usdt
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    wr = len(wins) / len(real) * 100 if real else 0
    # count skipped = trades in base (atr=0) - trades here
    print(f"  {label:<14}  {len(real):>6}  {len(wins):>4}  {len(losses):>4}  {wr:>5.1f}%  {pnl:>+7.2f}$  {dd:>6.2f}$")

# Also show which trades got filtered at each level
print()
print("--- Trades filtered by MinATR (compared to OFF) ---")
base_cfg = BacktestConfig(**base, min_atr_pct=0.0)
base_trades = BacktestEngine(base_cfg, silent=True).run(klines)

for atr in [0.5]:
    cfg = BacktestConfig(**base, min_atr_pct=atr)
    filtered_trades = BacktestEngine(cfg, silent=True).run(klines)
    base_times = {t.entry_time for t in base_trades}
    filt_times  = {t.entry_time for t in filtered_trades}
    removed = [t for t in base_trades if t.entry_time not in filt_times]
    kept    = [t for t in filtered_trades]
    print(f"\nMinATR {atr}% removes {len(removed)} trades:")
    for t in removed:
        result = "WIN " if t.pnl_usdt > 0 else "LOSS"
        ts = t.entry_time.strftime('%m/%d %H:%M')
        print(f"  [{result}] {t.direction.value.upper():>5} @{t.entry_price:.2f}  {t.pnl_usdt:>+6.2f}$  {t.close_reason}  {ts}")
