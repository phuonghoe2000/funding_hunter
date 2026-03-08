"""
Grid search best config + backtest BTCUSDT & XAUUSDT (10000 candles)
"""
import urllib.request, json, sys, time as _t
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine
from datetime import datetime, timezone

# ==================== Download ====================
def download_10k(symbol, interval="15m", target=10000, batch=1500):
    print(f"\n📥 Downloading {target} candles {symbol} {interval}...")
    end_ms   = int(_t.time() * 1000)
    span_ms  = target * 15 * 60 * 1000
    start_ms = end_ms - span_ms
    all_klines = []
    current_start = start_ms
    while len(all_klines) < target:
        url = (f"https://fapi.binance.com/fapi/v1/klines"
               f"?symbol={symbol}&interval={interval}&startTime={current_start}&limit={batch}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                batch_data = json.loads(r.read().decode())
        except Exception as e:
            print(f"  ⚠️ Error: {e}")
            break
        if not batch_data:
            break
        all_klines.extend(batch_data)
        t0 = datetime.fromtimestamp(batch_data[0][0]/1000, tz=timezone.utc).strftime("%m/%d %H:%M")
        t1 = datetime.fromtimestamp(batch_data[-1][6]/1000, tz=timezone.utc).strftime("%m/%d %H:%M")
        print(f"  +{len(batch_data):>4} ({t0}->{t1})  total={len(all_klines)}")
        current_start = batch_data[-1][6] + 1
        if len(batch_data) < batch:
            break
        _t.sleep(0.15)
    all_klines = all_klines[:target]
    t_s = datetime.fromtimestamp(all_klines[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    t_e = datetime.fromtimestamp(all_klines[-1][6]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"  ✅ {len(all_klines)} candles | {t_s} → {t_e} UTC")
    return all_klines

# ==================== Grid Search ====================
def grid_search(klines, symbol, top_n=10):
    print(f"\n🔍 Grid search {symbol}...")

    # parameter grid
    thresholds    = [0.7, 1.0, 1.3, 1.5]
    lookbacks     = [5, 7, 10]
    tps           = [1.0, 1.5, 2.0, 3.0]
    sls           = [1.0, 1.5, 2.0]
    vol_mults     = [1.2, 1.5, 2.0]
    ema_periods   = [50, 100]
    ema_slopes    = [3, 5]

    total = len(thresholds)*len(lookbacks)*len(tps)*len(sls)*len(vol_mults)*len(ema_periods)*len(ema_slopes)
    print(f"  {total} combinations...")

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
                                    price_change_threshold=thr,
                                    lookback_candles=lb,
                                    position_volume_usdt=20.0,
                                    leverage=5,
                                    take_profit_pct=tp,
                                    stop_loss_pct=sl,
                                    use_trailing_tp=True,
                                    tp_extension_pct=0.3,
                                    max_tp_extensions=5,
                                    use_volume_confirmation=True,
                                    volume_multiplier=vm,
                                    volume_lookback=20,
                                    use_rsi_filter=True,
                                    rsi_period=14,
                                    rsi_overbought=70.0,
                                    rsi_oversold=30.0,
                                    cooldown_candles=4,
                                    max_momentum_pct=2.5,
                                    min_atr_pct=0.0,
                                    use_ema_trend=True,
                                    ema_period=ep,
                                    ema_slope_candles=es,
                                )
                                trades = BacktestEngine(cfg, silent=True).run(klines)
                                real   = [t for t in trades if t.close_reason != 'END_OF_DATA']
                                if len(real) < 10:
                                    count += 1
                                    continue
                                wins   = [t for t in real if t.pnl_usdt > 0]
                                losses = [t for t in real if t.pnl_usdt <= 0]
                                pnl    = sum(t.pnl_usdt for t in trades)
                                wr     = len(wins)/len(real)*100
                                gross_win  = sum(t.pnl_usdt for t in wins)
                                gross_loss = abs(sum(t.pnl_usdt for t in losses))
                                pf     = gross_win/gross_loss if gross_loss > 0 else 99
                                eq = 0; peak = 0; dd = 0
                                for t in trades:
                                    eq += t.pnl_usdt; peak = max(peak, eq); dd = max(dd, peak - eq)
                                # Score: balance PnL, WR, PF, penalize DD
                                score = pnl * (wr/50) * min(pf, 5) / max(dd, 1)
                                results.append((score, pnl, wr, pf, dd, len(real), cfg))
                                count += 1
                                if count % 200 == 0:
                                    print(f"  ... {count}/{total}")

    results.sort(reverse=True, key=lambda x: x[0])
    print(f"\n  Top {top_n} configs for {symbol}:")
    print(f"  {'#':>2}  {'Thr':>5}  {'LB':>3}  {'TP':>4}  {'SL':>4}  {'Vol':>5}  {'EMA':>7}  {'Trades':>6}  {'WR':>6}  {'PnL':>8}  {'PF':>5}  {'DD':>7}")
    print("  " + "-"*90)
    for i, (sc, pnl, wr, pf, dd, n, cfg) in enumerate(results[:top_n], 1):
        ema_str = f"{cfg.ema_period}/{cfg.ema_slope_candles}c"
        print(f"  {i:>2}  {cfg.price_change_threshold:>5.1f}  {cfg.lookback_candles:>3}  "
              f"{cfg.take_profit_pct:>4.1f}  {cfg.stop_loss_pct:>4.1f}  "
              f"{cfg.volume_multiplier:>5.1f}  {ema_str:>7}  "
              f"{n:>6}  {wr:>5.1f}%  {pnl:>+7.2f}$  {pf:>5.2f}  {dd:>6.2f}$")
    return results[0][6] if results else None  # best config

# ==================== Main ====================
symbols = ["BTCUSDT", "XAUUSDT"]
best_configs = {}

for sym in symbols:
    fname = f"{sym.lower()}_15m_10000.json"
    try:
        klines = download_10k(sym)
        with open(fname, "w") as f:
            json.dump(klines, f)
        best_cfg = grid_search(klines, sym)
        best_configs[sym] = best_cfg
    except Exception as e:
        print(f"❌ {sym} failed: {e}")

# ==================== Print best configs ====================
print("\n" + "="*70)
print("BEST CONFIGS SUMMARY")
print("="*70)
for sym, cfg in best_configs.items():
    if cfg:
        print(f"\n{sym}:")
        print(f"  Threshold  : {cfg.price_change_threshold}% / {cfg.lookback_candles}c")
        print(f"  TP / SL    : {cfg.take_profit_pct}% / {cfg.stop_loss_pct}%")
        print(f"  Volume     : {cfg.volume_multiplier}x")
        print(f"  EMA Trend  : period={cfg.ema_period}, slope={cfg.ema_slope_candles}c")
        print(f"  Trailing   : ON (+{cfg.tp_extension_pct}%, max {cfg.max_tp_extensions}x)")
