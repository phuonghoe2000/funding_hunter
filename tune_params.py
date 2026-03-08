"""
Phân tích nguyên nhân thua + grid search tăng trade & WR
"""
import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine, TradeDirection, calculate_rsi, calculate_ema

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

def run(cfg_dict):
    cfg = BacktestConfig(**cfg_dict)
    trades = BacktestEngine(cfg, silent=True).run(klines)
    real  = [t for t in trades if t.close_reason != 'END_OF_DATA']
    wins  = [t for t in real if t.pnl_usdt > 0]
    pnl   = sum(t.pnl_usdt for t in trades)
    eq = 0; peak = 0; dd = 0
    for t in trades:
        eq += t.pnl_usdt
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    wr = len(wins)/len(real)*100 if real else 0
    return len(real), len(wins), len(real)-len(wins), wr, pnl, dd

def row(label, n, w, l, wr, pnl, dd):
    print(f"  {label:<32}  {n:>5}  {w:>4}  {l:>4}  {wr:>5.1f}%  {pnl:>+7.2f}$  {dd:>6.2f}$")

hdr = f"  {'Label':<32}  {'Trades':>5}  {'W':>4}  {'L':>4}  {'WR%':>5}  {'PnL':>8}  {'MaxDD':>7}"
sep = "  " + "-"*74

# ===== 1. Baseline =====
print("=" * 80)
print("1. BASELINE")
print(hdr); print(sep)
row("Baseline (current)", *run(BASE))

# ===== 2. Threshold sensitivity =====
print()
print("2. THRESHOLD (thấp hơn = nhiều lệnh hơn)")
print(hdr); print(sep)
for t in [0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5]:
    cfg = {**BASE, 'price_change_threshold': t}
    row(f"Threshold {t}%", *run(cfg))

# ===== 3. Volume multiplier =====
print()
print("3. VOLUME MULTIPLIER (thấp hơn = nhiều lệnh pass hơn)")
print(hdr); print(sep)
for v in [1.0, 1.2, 1.3, 1.5, 2.0, 2.5]:
    cfg = {**BASE, 'volume_multiplier': v}
    row(f"VolMult {v}x", *run(cfg))

# ===== 4. EMA slope candles =====
print()
print("4. EMA SLOPE CANDLES (nhiều hơn = xác nhận trend mạnh hơn)")
print(hdr); print(sep)
for sc in [1, 2, 3, 5, 7, 10]:
    cfg = {**BASE, 'ema_slope_candles': sc}
    row(f"EMA slope {sc}c", *run(cfg))

# ===== 5. EMA period =====
print()
print("5. EMA PERIOD")
print(hdr); print(sep)
for ep in [20, 30, 50, 100, 200]:
    cfg = {**BASE, 'ema_period': ep}
    row(f"EMA{ep}", *run(cfg))

# ===== 6. RSI oversold/overbought =====
print()
print("6. RSI FILTER (nới rộng ngưỡng = nhiều lệnh hơn)")
print(hdr); print(sep)
for ob, os_ in [(70, 30), (65, 35), (60, 40), (55, 45), (50, 50)]:
    cfg = {**BASE, 'rsi_overbought': ob, 'rsi_oversold': os_}
    row(f"RSI OB{ob}/OS{os_}", *run(cfg))
row("RSI OFF", *run({**BASE, 'use_rsi_filter': False}))

# ===== 7. Lookback candles =====
print()
print("7. LOOKBACK CANDLES")
print(hdr); print(sep)
for lb in [3, 5, 7, 10, 14]:
    cfg = {**BASE, 'lookback_candles': lb}
    row(f"Lookback {lb}c", *run(cfg))

# ===== 8. Best combos from above insights =====
print()
print("8. BEST COMBOS")
print(hdr); print(sep)
combos = [
    ("Thresh0.8+Vol1.2",     {**BASE, 'price_change_threshold': 0.8, 'volume_multiplier': 1.2}),
    ("Thresh0.8+EMA5c",      {**BASE, 'price_change_threshold': 0.8, 'ema_slope_candles': 5}),
    ("Thresh0.8+EMA20",      {**BASE, 'price_change_threshold': 0.8, 'ema_period': 20}),
    ("Vol1.2+EMA5c",         {**BASE, 'volume_multiplier': 1.2, 'ema_slope_candles': 5}),
    ("T0.8+V1.2+EMA20/5c",   {**BASE, 'price_change_threshold': 0.8, 'volume_multiplier': 1.2, 'ema_period': 20, 'ema_slope_candles': 5}),
    ("T0.8+V1.2+EMA50/5c",   {**BASE, 'price_change_threshold': 0.8, 'volume_multiplier': 1.2, 'ema_period': 50, 'ema_slope_candles': 5}),
    ("T0.8+V1.2+EMA100/5c",  {**BASE, 'price_change_threshold': 0.8, 'volume_multiplier': 1.2, 'ema_period': 100, 'ema_slope_candles': 5}),
    ("T0.8+V1.2+EMA50/7c",   {**BASE, 'price_change_threshold': 0.8, 'volume_multiplier': 1.2, 'ema_period': 50, 'ema_slope_candles': 7}),
    ("T0.7+V1.2+EMA50/5c",   {**BASE, 'price_change_threshold': 0.7, 'volume_multiplier': 1.2, 'ema_period': 50, 'ema_slope_candles': 5}),
    ("T0.9+V1.2+EMA50/5c",   {**BASE, 'price_change_threshold': 0.9, 'volume_multiplier': 1.2, 'ema_period': 50, 'ema_slope_candles': 5}),
    ("T0.8+V1.2+EMA50/5c+RSI65/35", {**BASE, 'price_change_threshold': 0.8, 'volume_multiplier': 1.2,
                                       'ema_period': 50, 'ema_slope_candles': 5,
                                       'rsi_overbought': 65, 'rsi_oversold': 35}),
]
for label, cfg in combos:
    row(label, *run(cfg))

print()
print("* Sorted by PnL:")
results = [(label, run(cfg)) for label, cfg in combos]
results.append(("Baseline (current)", run(BASE)))
results.sort(key=lambda x: x[1][4], reverse=True)
print(hdr); print(sep)
for label, r in results:
    row(label, *r)
