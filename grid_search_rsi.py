"""Grid search ETH 10000 candles — bao gồm RSI variations"""
import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

with open('ethusdt_15m_10000.json') as f:
    klines = json.load(f)

# Fix best params từ grid trước, chỉ vary RSI
BASE_CONFIGS = [
    # (label, thr, lb, tp, sl, vm, ep, es)
    ("Balanced",  0.5, 10, 1.0, 1.0, 1.5, 100, 3),
    ("HighPnL",   0.7,  7, 3.0, 2.0, 1.2,  50, 5),
    ("HighWR",    1.0,  5, 1.0, 2.0, 1.5,  50, 3),
    ("Current",   1.0,  7, 2.0, 2.0, 1.5,  50, 3),
]

RSI_CONFIGS = [
    # (label,  use_rsi, ob,   os)
    ("OFF",        False, 70.0, 30.0),
    ("70/30",      True,  70.0, 30.0),  # current
    ("65/35",      True,  65.0, 35.0),
    ("75/25",      True,  75.0, 25.0),
    ("72/28",      True,  72.0, 28.0),
    ("68/32",      True,  68.0, 32.0),
]

print(f"{'Base':10}  {'RSI':8}  {'Trades':>6}  {'WR':>6}  {'PnL':>9}  {'PF':>5}  {'DD':>7}  {'Score':>8}")
print("-" * 70)

all_results = []
for b_label, thr, lb, tp, sl, vm, ep, es in BASE_CONFIGS:
    print(f"\n[{b_label}] thr={thr} lb={lb} tp={tp} sl={sl} vol={vm}x ema={ep}/{es}c")
    for r_label, use_rsi, ob, os_ in RSI_CONFIGS:
        cfg = BacktestConfig(
            price_change_threshold=thr, lookback_candles=lb,
            position_volume_usdt=20, leverage=5,
            take_profit_pct=tp, stop_loss_pct=sl,
            use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
            use_volume_confirmation=True, volume_multiplier=vm, volume_lookback=20,
            use_rsi_filter=use_rsi, rsi_period=14, rsi_overbought=ob, rsi_oversold=os_,
            cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0,
            use_ema_trend=True, ema_period=ep, ema_slope_candles=es,
        )
        trades = BacktestEngine(cfg, silent=True).run(klines)
        real   = [t for t in trades if t.close_reason != 'END_OF_DATA']
        if not real:
            continue
        wins   = [t for t in real if t.pnl_usdt > 0]
        losses = [t for t in real if t.pnl_usdt <= 0]
        pnl    = sum(t.pnl_usdt for t in trades)
        wr     = len(wins) / len(real) * 100
        gl     = abs(sum(t.pnl_usdt for t in losses))
        pf     = sum(t.pnl_usdt for t in wins) / gl if gl > 0 else 99
        eq = 0; peak = 0; dd = 0
        for t in trades:
            eq += t.pnl_usdt; peak = max(peak, eq); dd = max(dd, peak - eq)
        score = pnl * (wr / 50) * min(pf, 5) / max(dd, 1)

        marker = " ◄ best" if r_label == "70/30" else ""
        filtered = len([t for t in trades if t.close_reason == 'END_OF_DATA'])
        total_fired = len(trades)
        print(f"  RSI {r_label:<8}  {len(real):>6}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$  {score:>8.2f}{marker}")
        all_results.append((b_label, r_label, use_rsi, ob, os_, len(real), wr, pnl, pf, dd, score))

# Overall best
print("\n" + "="*70)
print("TOP 10 overall (all base configs x RSI combos):")
print("="*70)
all_results.sort(key=lambda x: x[10], reverse=True)
print(f"  {'Base':10}  {'RSI':8}  {'Trades':>6}  {'WR':>6}  {'PnL':>9}  {'PF':>5}  {'DD':>7}")
print("  " + "-"*60)
for b, r, _, ob, os_, n, wr, pnl, pf, dd, sc in all_results[:10]:
    print(f"  {b:<10}  {r:<8}  {n:>6}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

# Best by PnL
print("\n--- Top 5 by PnL ---")
for b, r, _, ob, os_, n, wr, pnl, pf, dd, sc in sorted(all_results, key=lambda x: x[7], reverse=True)[:5]:
    print(f"  {b:<10}  RSI={r:<8}  {n}t  WR={wr:.1f}%  PnL={pnl:+.2f}$  DD={dd:.2f}$")
