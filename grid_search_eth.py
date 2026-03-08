"""Full grid search ETH 10000 candles"""
import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

with open('ethusdt_15m_10000.json') as f:
    klines = json.load(f)
print(f'ETH candles: {len(klines)}')

thresholds  = [0.5, 0.7, 1.0, 1.3, 1.5]
lookbacks   = [5, 7, 10]
tps         = [1.0, 1.5, 2.0, 3.0]
sls         = [1.0, 1.5, 2.0]
vol_mults   = [1.2, 1.5, 2.0]
ema_periods = [50, 100]
ema_slopes  = [3, 5]

total = len(thresholds)*len(lookbacks)*len(tps)*len(sls)*len(vol_mults)*len(ema_periods)*len(ema_slopes)
print(f'Total combinations: {total}')

results = []
count = 0
for thr in thresholds:
    for lb in lookbacks:
        for tp in tps:
            for sl in sls:
                for vm in vol_mults:
                    for ep in ema_periods:
                        for es in ema_slopes:
                            cfg = BacktestConfig(
                                price_change_threshold=thr, lookback_candles=lb,
                                position_volume_usdt=20, leverage=5,
                                take_profit_pct=tp, stop_loss_pct=sl,
                                use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
                                use_volume_confirmation=True, volume_multiplier=vm, volume_lookback=20,
                                use_rsi_filter=True, rsi_period=14, rsi_overbought=70.0, rsi_oversold=30.0,
                                cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0,
                                use_ema_trend=True, ema_period=ep, ema_slope_candles=es,
                            )
                            trades = BacktestEngine(cfg, silent=True).run(klines)
                            real = [t for t in trades if t.close_reason != 'END_OF_DATA']
                            if len(real) < 15:
                                count += 1
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
                            results.append((score, pnl, wr, pf, dd, len(real), cfg))
                            count += 1
                            if count % 500 == 0:
                                print(f'  {count}/{total}...')

results.sort(reverse=True, key=lambda x: x[0])
print(f'\nValid configs (>=15 trades): {len(results)}')
print()
print(f'  {"#":>2}  {"Thr":>5}  {"LB":>3}  {"TP":>4}  {"SL":>4}  {"Vol":>5}  {"EMA":>7}  {"Trades":>6}  {"WR":>6}  {"PnL":>9}  {"PF":>5}  {"DD":>7}')
print('  ' + '-'*88)
for i, (sc, pnl, wr, pf, dd, n, cfg) in enumerate(results[:20], 1):
    ema = f'{cfg.ema_period}/{cfg.ema_slope_candles}c'
    print(f'  {i:>2}  {cfg.price_change_threshold:>5.1f}  {cfg.lookback_candles:>3}  '
          f'{cfg.take_profit_pct:>4.1f}  {cfg.stop_loss_pct:>4.1f}  '
          f'{cfg.volume_multiplier:>5.1f}  {ema:>7}  '
          f'{n:>6}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$')

# Best config detail
best = results[0][6]
print('\n' + '='*55)
print('BEST CONFIG ETH (by score = PnL x WR x PF / DD):')
print('='*55)
print(f'  Threshold      : {best.price_change_threshold}% / {best.lookback_candles} candles')
print(f'  TP / SL        : {best.take_profit_pct}% / {best.stop_loss_pct}%')
print(f'  Volume filter  : {best.volume_multiplier}x (lookback 20)')
print(f'  EMA Trend      : period={best.ema_period}, slope={best.ema_slope_candles}c')
print(f'  RSI            : ON (14, OB={best.rsi_overbought}, OS={best.rsi_oversold})')
print(f'  Max Momentum   : {best.max_momentum_pct}%')
print(f'  Trailing TP    : ON (+{best.tp_extension_pct}%, max {best.max_tp_extensions}x)')

# Also show best by pure PnL (regardless of DD)
results_by_pnl = sorted(results, key=lambda x: x[1], reverse=True)
print('\n--- Top 5 by highest PnL (no DD penalty) ---')
for i, (sc, pnl, wr, pf, dd, n, cfg) in enumerate(results_by_pnl[:5], 1):
    ema = f'{cfg.ema_period}/{cfg.ema_slope_candles}c'
    print(f'  {i}. Thr={cfg.price_change_threshold} LB={cfg.lookback_candles} TP={cfg.take_profit_pct} SL={cfg.stop_loss_pct} '
          f'Vol={cfg.volume_multiplier} EMA={ema} | {n}t WR={wr:.1f}% PnL={pnl:+.2f}$ DD={dd:.2f}$')

# Best by WR (min 30 trades)
results_by_wr = sorted([r for r in results if r[5]>=30], key=lambda x: x[2], reverse=True)
print('\n--- Top 5 by WR (min 30 trades) ---')
for i, (sc, pnl, wr, pf, dd, n, cfg) in enumerate(results_by_wr[:5], 1):
    ema = f'{cfg.ema_period}/{cfg.ema_slope_candles}c'
    print(f'  {i}. Thr={cfg.price_change_threshold} LB={cfg.lookback_candles} TP={cfg.take_profit_pct} SL={cfg.stop_loss_pct} '
          f'Vol={cfg.volume_multiplier} EMA={ema} | {n}t WR={wr:.1f}% PnL={pnl:+.2f}$ DD={dd:.2f}$')
