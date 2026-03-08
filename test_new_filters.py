"""Test 3 new filters: MaxATR, BlockHours, MaxMom2.0"""
import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

with open('ethusdt_15m_10000.json') as f:
    klines = json.load(f)

def run(label, **kwargs):
    base = dict(
        price_change_threshold=1.0, lookback_candles=7,
        position_volume_usdt=20, leverage=5,
        take_profit_pct=2.0, stop_loss_pct=2.0,
        use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
        use_volume_confirmation=True, volume_multiplier=1.5, volume_lookback=20,
        use_rsi_filter=True, rsi_period=14, rsi_overbought=72.0, rsi_oversold=28.0,
        cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0, max_atr_pct=0.0,
        use_ema_trend=True, ema_period=50, ema_slope_candles=3,
    )
    base.update(kwargs)
    cfg = BacktestConfig(**base)
    trades = BacktestEngine(cfg, silent=True).run(klines)
    real = [t for t in trades if t.close_reason != 'END_OF_DATA']
    wins = [t for t in real if t.pnl_usdt > 0]
    pnl  = sum(t.pnl_usdt for t in trades)
    wr   = len(wins) / len(real) * 100 if real else 0
    gl   = abs(sum(t.pnl_usdt for t in real if t.pnl_usdt <= 0))
    pf   = sum(t.pnl_usdt for t in wins) / gl if gl > 0 else 99
    eq = 0; peak = 0; dd = 0
    for t in trades:
        eq += t.pnl_usdt; peak = max(peak, eq); dd = max(dd, peak - eq)
    mark = ' ◄' if pnl > 76 and wr > 61 else ''
    print(f"  {label:<38}  {len(real):>3}t  WR={wr:>5.1f}%  PnL={pnl:>+7.2f}$  PF={pf:>4.2f}  DD={dd:>5.2f}${mark}")

print(f"  {'Config':<38}  {'Trd':>4}  {'WR':>7}  {'PnL':>10}  {'PF':>6}  {'DD':>8}")
print("  " + "-"*80)
run("Baseline (RSI 72/28)")
print("  ---")
run("+ MaxMom 2.0%",              max_momentum_pct=2.0)
run("+ MaxATR 1.2%",              max_atr_pct=1.2)
run("+ Block [8]",                blocked_hours=[8])
run("+ Block [8,21,22]",          blocked_hours=[8,21,22])
run("+ Block [4,8,21,22]",        blocked_hours=[4,8,21,22])
print("  ---")
run("MaxMom2 + MaxATR1.2",        max_momentum_pct=2.0, max_atr_pct=1.2)
run("MaxMom2 + Block[8,21,22]",   max_momentum_pct=2.0, blocked_hours=[8,21,22])
run("MaxATR1.2 + Block[8,21,22]", max_atr_pct=1.2, blocked_hours=[8,21,22])
print("  ---")
run("ALL 3 Block[8,21,22]",       max_momentum_pct=2.0, max_atr_pct=1.2, blocked_hours=[8,21,22])
run("ALL 3 Block[4,8,21,22]",     max_momentum_pct=2.0, max_atr_pct=1.2, blocked_hours=[4,8,21,22])
run("ALL 3 Block[8]",             max_momentum_pct=2.0, max_atr_pct=1.2, blocked_hours=[8])
