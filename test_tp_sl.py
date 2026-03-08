"""Grid test TP x SL values"""
import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

with open('ethusdt_15m_10000.json') as f: klines = json.load(f)

def run(tp, sl):
    cfg = BacktestConfig(
        price_change_threshold=1.0, lookback_candles=7,
        position_volume_usdt=20, leverage=5,
        take_profit_pct=tp, stop_loss_pct=sl,
        use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
        use_volume_confirmation=True, volume_multiplier=1.5, volume_lookback=20,
        use_rsi_filter=True, rsi_period=14, rsi_overbought=72.0, rsi_oversold=28.0,
        cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0, max_atr_pct=1.2,
        blocked_hours=[8, 21, 22],
        use_ema_trend=True, ema_period=50, ema_slope_candles=3,
    )
    trades = BacktestEngine(cfg, silent=True).run(klines)
    real   = [t for t in trades if t.close_reason != 'END_OF_DATA']
    wins   = [t for t in real if t.pnl_usdt > 0]
    losses = [t for t in real if t.pnl_usdt <= 0]
    pnl    = sum(t.pnl_usdt for t in trades)
    wr     = len(wins)/len(real)*100 if real else 0
    gl     = abs(sum(t.pnl_usdt for t in losses))
    pf     = sum(t.pnl_usdt for t in wins)/gl if gl > 0 else 99
    eq=0; peak=0; dd=0
    for t in trades: eq+=t.pnl_usdt; peak=max(peak,eq); dd=max(dd,peak-eq)
    avg_w  = sum(t.pnl_usdt for t in wins)/len(wins) if wins else 0
    avg_l  = sum(t.pnl_usdt for t in losses)/len(losses) if losses else 0
    return len(real), wr, pnl, pf, dd, avg_w, avg_l

tps = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
sls = [1.0, 1.5, 2.0, 2.5, 3.0]

print(f"  {'TP':>4} {'SL':>4}  {'Trd':>4}  {'WR':>6}  {'PnL':>9}  {'PF':>5}  {'DD':>7}  {'avgW':>6}  {'avgL':>6}")
print("  " + "-"*70)

all_results = []
for tp in tps:
    for sl in sls:
        n, wr, pnl, pf, dd, aw, al = run(tp, sl)
        all_results.append((tp, sl, n, wr, pnl, pf, dd, aw, al))
        mark = ' ◄' if tp == 2.0 and sl == 2.0 else ''
        print(f"  {tp:>4.1f} {sl:>4.1f}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$  {aw:>+5.2f}$  {al:>+5.2f}${mark}")
    print()

# Summary
print("=" * 72)
best_pnl  = max(all_results, key=lambda x: x[4])
best_wr   = max(all_results, key=lambda x: x[3])
best_pf   = max(all_results, key=lambda x: x[5])
best_dd   = min(all_results, key=lambda x: x[6])
best_score = max(all_results, key=lambda x: x[4]*(x[3]/50)*min(x[5],5)/max(x[6],0.1))

print(f"  Best PnL  : TP={best_pnl[0]} SL={best_pnl[1]}  {best_pnl[4]:>+.2f}$  WR={best_pnl[3]:.1f}%  DD={best_pnl[6]:.2f}$")
print(f"  Best WR   : TP={best_wr[0]}  SL={best_wr[1]}  {best_wr[4]:>+.2f}$  WR={best_wr[3]:.1f}%  DD={best_wr[6]:.2f}$")
print(f"  Best PF   : TP={best_pf[0]}  SL={best_pf[1]}  {best_pf[4]:>+.2f}$  WR={best_pf[3]:.1f}%  DD={best_pf[6]:.2f}$")
print(f"  Best DD   : TP={best_dd[0]}  SL={best_dd[1]}  {best_dd[4]:>+.2f}$  WR={best_dd[3]:.1f}%  DD={best_dd[6]:.2f}$")
print(f"  Best Score: TP={best_score[0]}  SL={best_score[1]}  {best_score[4]:>+.2f}$  WR={best_score[3]:.1f}%  DD={best_score[6]:.2f}$")
