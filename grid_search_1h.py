"""Grid search cho nến 1H - ETH, BTC, POWER, PIPPIN"""
import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

COINS = [
    ('ETH',    'ethusdt_1h_1000.json'),
    ('BTC',    'btcusdt_1h_1000.json'),
    ('POWER',  'powerusdt_1h_1000.json'),
    ('PIPPIN', 'pippinusdt_1h_1000.json'),
]

# Param grid - tối ưu cho 1H (threshold rộng hơn, lookback ngắn hơn)
thresholds  = [0.5, 0.8, 1.0, 1.5, 2.0]
lookbacks   = [3, 5, 7]
tps         = [1.0, 1.5, 2.0, 3.0]
sls         = [1.5, 2.0, 3.0]
vol_mults   = [1.2, 1.5, 2.0]
ema_periods = [20, 50]
ema_slopes  = [2, 3]

total = len(thresholds)*len(lookbacks)*len(tps)*len(sls)*len(vol_mults)*len(ema_periods)*len(ema_slopes)
print(f'Total combinations: {total}')
print()

for coin_name, filename in COINS:
    try:
        with open(filename) as f:
            klines = json.load(f)
    except FileNotFoundError:
        print(f'❌ Không tìm thấy {filename}, bỏ qua.')
        continue

    print(f'{"="*70}')
    print(f'  📈 {coin_name} - {len(klines)} candles 1H')
    print(f'{"="*70}')

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
                                    cooldown_candles=2,
                                    max_momentum_pct=0.0,
                                    min_atr_pct=0.0, max_atr_pct=0.0,
                                    use_ema_trend=True, ema_period=ep, ema_slope_candles=es,
                                )
                                trades = BacktestEngine(cfg, silent=True).run(klines)
                                real = [t for t in trades if t.close_reason != 'END_OF_DATA']
                                count += 1
                                if len(real) < 10:
                                    continue
                                wins   = [t for t in real if t.pnl_usdt > 0]
                                losses = [t for t in real if t.pnl_usdt <= 0]
                                pnl    = sum(t.pnl_usdt for t in real)
                                wr     = len(wins) / len(real) * 100
                                gl     = abs(sum(t.pnl_usdt for t in losses))
                                pf     = sum(t.pnl_usdt for t in wins) / gl if gl > 0 else 99
                                eq = 0; peak = 0; dd = 0
                                for t in real:
                                    eq += t.pnl_usdt; peak = max(peak, eq); dd = max(dd, peak - eq)
                                score = pnl * (wr / 50) * min(pf, 5) / max(dd, 1)
                                results.append((score, pnl, wr, pf, dd, len(real), cfg))

        if count % 500 == 0 or thr == thresholds[-1]:
            print(f'  [{count}/{total}] valid={len(results)}...')

    if not results:
        print(f'  ❌ Không có config nào đủ 10 trades\n')
        continue

    results.sort(reverse=True, key=lambda x: x[0])
    print(f'\n  Valid configs (>=10 trades): {len(results)}')
    print()
    print(f'  {"#":>2}  {"Thr":>5}  {"LB":>3}  {"TP":>4}  {"SL":>4}  {"Vol":>5}  {"EMA":>7}  {"N":>5}  {"WR":>6}  {"PnL":>9}  {"PF":>5}  {"DD":>7}')
    print('  ' + '-'*92)
    for i, (sc, pnl, wr, pf, dd, n, cfg) in enumerate(results[:15], 1):
        ema = f'{cfg.ema_period}/{cfg.ema_slope_candles}c'
        print(f'  {i:>2}  {cfg.price_change_threshold:>5.1f}  {cfg.lookback_candles:>3}  '
              f'{cfg.take_profit_pct:>4.1f}  {cfg.stop_loss_pct:>4.1f}  '
              f'{cfg.volume_multiplier:>5.1f}  {ema:>7}  '
              f'{n:>5}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$')

    # Best PnL separately
    best_pnl = sorted(results, key=lambda x: x[1], reverse=True)[0]
    best_wr  = sorted(results, key=lambda x: x[2], reverse=True)[0]

    print()
    b = best_pnl[6]
    print(f'  🏆 Best PnL: thr={b.price_change_threshold} lb={b.lookback_candles} TP={b.take_profit_pct}/SL={b.stop_loss_pct} '
          f'Vol={b.volume_multiplier}x EMA{b.ema_period}/{b.ema_slope_candles}c '
          f'→ {best_pnl[5]}t, {best_pnl[2]:.1f}%WR, {best_pnl[1]:+.2f}$, DD{best_pnl[4]:.2f}$')

    b = best_wr[6]
    print(f'  ⭐ Best WR:  thr={b.price_change_threshold} lb={b.lookback_candles} TP={b.take_profit_pct}/SL={b.stop_loss_pct} '
          f'Vol={b.volume_multiplier}x EMA{b.ema_period}/{b.ema_slope_candles}c '
          f'→ {best_wr[5]}t, {best_wr[2]:.1f}%WR, {best_wr[1]:+.2f}$, DD{best_wr[4]:.2f}$')
    print()
