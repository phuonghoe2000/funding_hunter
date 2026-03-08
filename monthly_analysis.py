import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine
from collections import defaultdict

with open('ethusdt_15m_10000.json') as f:
    klines = json.load(f)

BASE = dict(
    price_change_threshold=1.0, lookback_candles=7,
    position_volume_usdt=20.0, leverage=5,
    take_profit_pct=2.0, stop_loss_pct=2.0,
    use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
    use_volume_confirmation=True, volume_multiplier=1.5, volume_lookback=20,
    use_rsi_filter=True, rsi_period=14, rsi_overbought=70.0, rsi_oversold=30.0,
    cooldown_candles=4, max_momentum_pct=2.5,
    min_atr_pct=0.0, use_ema_trend=True, ema_period=50, ema_slope_candles=3
)

cfg = BacktestConfig(**BASE)
trades = BacktestEngine(cfg, silent=True).run(klines)
real   = [t for t in trades if t.close_reason != 'END_OF_DATA']
wins   = [t for t in real if t.pnl_usdt > 0]
losses = [t for t in real if t.pnl_usdt <= 0]
avg_win  = sum(t.pnl_usdt for t in wins) / len(wins)
avg_loss = abs(sum(t.pnl_usdt for t in losses) / len(losses))
pf = (len(wins) * avg_win) / (len(losses) * avg_loss)

print("=== BASELINE ANALYSIS ===")
print(f"  Trades    : {len(real)}")
print(f"  WR        : {len(wins)/len(real)*100:.1f}%")
print(f"  Avg Win   : +{avg_win:.2f}$")
print(f"  Avg Loss  : -{avg_loss:.2f}$")
print(f"  Win/Loss ratio : {avg_win/avg_loss:.2f}x")
print(f"  Profit Factor  : {pf:.2f}  (>1.5 = good, >2 = excellent)")
print()

# Monthly
monthly_w   = defaultdict(int)
monthly_l   = defaultdict(int)
monthly_pnl = defaultdict(float)
for t in real:
    m = t.entry_time.strftime('%Y-%m')
    if t.pnl_usdt > 0:
        monthly_w[m] += 1
    else:
        monthly_l[m] += 1
    monthly_pnl[m] += t.pnl_usdt

print("Monthly breakdown:")
print(f"  {'Month':<10}  {'W':>3}  {'L':>3}  {'WR':>6}  {'PnL':>8}  Note")
print("  " + "-"*50)
for m in sorted(set(list(monthly_w.keys()) + list(monthly_l.keys()))):
    w = monthly_w[m]; l = monthly_l[m]
    wr = w / (w+l) * 100 if (w+l) else 0
    note = ""
    if wr < 50:
        note = "<- bad month"
    elif wr >= 70:
        note = "<- great"
    print(f"  {m:<10}  {w:>3}  {l:>3}  {wr:>5.1f}%  {monthly_pnl[m]:>+7.2f}$  {note}")
