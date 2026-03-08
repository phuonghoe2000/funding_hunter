"""Run auto config scan on XAUUSDT 15m data - 10000 candles"""
import sys, io, json, os, time
import urllib.request

# Fix Windows console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ==============================
# Download 10000 candles (paginated)
# ==============================
SYMBOL = "XAUUSDT"
INTERVAL = "15m"
TOTAL_LIMIT = 10000
BATCH = 1500  # Binance max per request

all_klines = []

# We need to page backwards using endTime
end_time = None

print(f"Downloading ~{TOTAL_LIMIT} candles for {SYMBOL} ({INTERVAL})...")

while len(all_klines) < TOTAL_LIMIT:
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval={INTERVAL}&limit={BATCH}"
    if end_time:
        url += f"&endTime={end_time}"
    
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f"Error: {e}")
        break
    
    if not batch or not isinstance(batch, list):
        break
    
    # batch is ordered oldest->newest; prepend to our list
    all_klines = batch + all_klines
    
    print(f"  Got {len(batch)} candles | Total so far: {len(all_klines)}")
    
    if len(batch) < BATCH:
        # No more history available
        break
    
    # Move end_time to just before the oldest candle in this batch
    end_time = batch[0][0] - 1  # open_time of oldest candle minus 1ms
    
    time.sleep(0.2)  # be gentle with API

print(f"Total candles downloaded: {len(all_klines)}")

# ==============================
# Run auto config scan
# ==============================
from core.auto_config_scanner import format_top_results, get_best_config, ScanResult, _calculate_metrics
from backtest import BacktestConfig, BacktestEngine

# Extended grid with smaller thresholds for more signals
thresholds = [0.2, 0.3, 0.5, 1.0, 1.5]
lookbacks  = [5, 7, 10]
tps        = [0.5, 1.0, 2.0, 3.5]
sls        = [0.3, 0.5, 1.0]
vol_multipliers = [1.5, 2.0]
trailing_configs = [(True, 0.3, 5), (False, 0.0, 0)]

total = len(thresholds)*len(lookbacks)*len(tps)*len(sls)*len(vol_multipliers)*len(trailing_configs)
print(f"🔍 Scanning {total} config combinations (extended thresholds)...")

results = []
count = 0
for threshold in thresholds:
    for lookback in lookbacks:
        for tp in tps:
            for sl in sls:
                for vol_mult in vol_multipliers:
                    for use_trailing, tp_ext, max_tp in trailing_configs:
                        config = BacktestConfig(
                            price_change_threshold=threshold,
                            lookback_candles=lookback,
                            position_volume_usdt=15.0,
                            leverage=5,
                            take_profit_pct=tp,
                            stop_loss_pct=sl,
                            use_trailing_tp=use_trailing,
                            tp_extension_pct=tp_ext,
                            max_tp_extensions=max_tp,
                            use_volume_confirmation=True,
                            volume_multiplier=vol_mult,
                            volume_lookback=20,
                            use_rsi_filter=True,
                            rsi_period=14,
                            rsi_overbought=70.0,
                            rsi_oversold=30.0,
                            cooldown_candles=4
                        )
                        engine = BacktestEngine(config, silent=True)
                        trades = engine.run(all_klines)
                        result = _calculate_metrics(config, trades)
                        results.append(result)
                        count += 1
                        if count % 200 == 0:
                            print(f"  Progress: {count}/{total} ({count/total*100:.0f}%)")

results.sort(key=lambda r: r.score, reverse=True)
print(f"✅ Done! {total} configs tested.")

print()
print(format_top_results(results, top_n=10))

best = get_best_config(results, min_trades=10)
if best:
    c = best.config
    print(f"\n>>> XAUUSDT BEST CONFIG (10000 candles):")
    print(f"    Threshold : {c.price_change_threshold}%")
    print(f"    Lookback  : {c.lookback_candles} candles")
    print(f"    TP        : {c.take_profit_pct}%")
    print(f"    SL        : {c.stop_loss_pct}%")
    print(f"    Vol Mult  : {c.volume_multiplier}x")
    print(f"    Trailing  : {c.use_trailing_tp}")
    print(f"    Score     : {best.score:.2f}")
    print(f"    PnL       : {best.total_pnl:+.4f} USD")
    print(f"    Win Rate  : {best.win_rate:.1f}%")
    print(f"    Trades    : {best.num_trades}")
    print(f"    Profit F  : {best.profit_factor:.2f}")
    print(f"    Max DD    : {best.max_drawdown:.2f}")
else:
    print('No profitable config found (min 10 trades) for XAUUSDT')
