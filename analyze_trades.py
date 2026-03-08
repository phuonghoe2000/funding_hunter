"""Analyze winning vs losing trades - what metrics differ"""
import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine, TradeDirection, calculate_rsi
from collections import defaultdict

class AnalysisEngine(BacktestEngine):
    def __init__(self, config):
        super().__init__(config, silent=True)
        self.trade_meta = []

    def run(self, klines):
        n = len(klines)
        close_prices = [float(k[4]) for k in klines]
        volumes      = [float(k[5]) for k in klines]
        highs        = [float(k[2]) for k in klines]
        lows         = [float(k[3]) for k in klines]

        for i in range(self.config.lookback_candles + self.config.volume_lookback, n):
            candle = klines[i]
            current_close = close_prices[i]
            current_high  = highs[i]
            current_low   = lows[i]
            candle_time   = self._ts(candle[0])

            if self.active_trade:
                self._monitor_trade(self.active_trade, current_high, current_low, current_close, candle_time, i)
                if self.active_trade and self.active_trade.close_reason:
                    self.trades.append(self.active_trade)
                    self.cooldown_until = i + self.config.cooldown_candles
                    self.active_trade = None
                continue

            if i < self.cooldown_until:
                continue

            direction = self._check_signal(close_prices, volumes, highs, lows, i)
            if direction:
                lookback = self.config.lookback_candles
                old = close_prices[i - lookback]
                price_change_pct = ((current_close - old) / old) * 100

                vol_start = max(0, i - self.config.volume_lookback)
                avg_vol = sum(volumes[vol_start:i]) / max(1, i - vol_start)
                vol_ratio = volumes[i] / avg_vol if avg_vol > 0 else 0

                rsi_data = close_prices[max(0, i - self.config.rsi_period - 10):i + 1]
                rsi_val  = calculate_rsi(rsi_data, self.config.rsi_period)

                ranges = [highs[j] - lows[j] for j in range(max(0, i-14), i)]
                atr = sum(ranges)/len(ranges) if ranges else 0
                atr_pct = atr / current_close * 100

                hour = candle_time.hour

                self.trade_meta.append({
                    'idx': i,
                    'direction': direction.value,
                    'price_change_pct': abs(price_change_pct),
                    'vol_ratio': vol_ratio,
                    'rsi': rsi_val,
                    'atr_pct': atr_pct,
                    'hour': hour,
                    'entry_price': current_close,
                })
                self._open_trade(direction, current_close, candle_time)

        if self.active_trade:
            last_close = close_prices[-1]
            last_time  = self._ts(klines[-1][6])
            self._close_trade(self.active_trade, last_close, last_time, 'END_OF_DATA')
            self.trades.append(self.active_trade)
            self.active_trade = None

        return self.trades


with open('ethusdt_15m_1000.json') as f:
    klines = json.load(f)

config = BacktestConfig(
    price_change_threshold=1.0,
    lookback_candles=7,
    position_volume_usdt=20.0,
    leverage=5,
    take_profit_pct=2.0,
    stop_loss_pct=2.0,
    use_trailing_tp=True,
    tp_extension_pct=0.3,
    max_tp_extensions=5,
    use_volume_confirmation=True,
    volume_multiplier=1.5,
    volume_lookback=20,
    use_rsi_filter=True,
    rsi_period=14,
    rsi_overbought=70.0,
    rsi_oversold=30.0,
    cooldown_candles=4
)

eng = AnalysisEngine(config)
trades = eng.run(klines)
meta   = eng.trade_meta

# Pair trade + meta (exclude END_OF_DATA)
pairs = []
for i, t in enumerate(trades):
    if t.close_reason == 'END_OF_DATA':
        continue
    if i < len(meta):
        pairs.append((t, meta[i]))

wins   = [(t, m) for t, m in pairs if t.pnl_usdt > 0]
losses = [(t, m) for t, m in pairs if t.pnl_usdt <= 0]

def avg(lst):
    return sum(lst)/len(lst) if lst else 0

def wr(w, l):
    total = w + l
    return f"{w/total*100:.0f}%" if total else "N/A"

print(f"=== TRADE ANALYSIS === {len(pairs)} trades (excl END_OF_DATA)")
print(f"  WIN: {len(wins)}  |  LOSS: {len(losses)}  |  WR: {wr(len(wins), len(losses))}")
print()

# ---- Avg metrics comparison ----
metrics = [
    ('price_change_pct', 'Momentum %  '),
    ('vol_ratio',        'Volume ratio'),
    ('rsi',              'RSI         '),
    ('atr_pct',          'ATR %       '),
]
print(f"{'Metric':<18} {'WIN avg':>10} {'LOSS avg':>10}  diff")
print("-" * 52)
for key, label in metrics:
    w = avg([m[key] for t, m in wins])
    l = avg([m[key] for t, m in losses])
    diff = w - l
    sign = "+" if diff > 0 else ""
    print(f"{label:<18} {w:>10.3f} {l:>10.3f}  {sign}{diff:.3f}")

# ---- Direction breakdown ----
print()
print("--- Direction ---")
wd = {'long': 0, 'short': 0}
ld = {'long': 0, 'short': 0}
for t, m in wins:
    wd[m['direction']] += 1
for t, m in losses:
    ld[m['direction']] += 1
print(f"  LONG   wins={wd['long']:>2}  losses={ld['long']:>2}  WR={wr(wd['long'], ld['long'])}")
print(f"  SHORT  wins={wd['short']:>2}  losses={ld['short']:>2}  WR={wr(wd['short'], ld['short'])}")

# ---- Hour of day breakdown ----
print()
print("--- Hour of day (UTC) ---")
hour_w = defaultdict(int)
hour_l = defaultdict(int)
for t, m in wins:
    hour_w[m['hour']] += 1
for t, m in losses:
    hour_l[m['hour']] += 1
all_hours = sorted(set(list(hour_w.keys()) + list(hour_l.keys())))
print(f"  {'Hour':>4}  {'W':>3}  {'L':>3}  {'WR':>6}  avg PnL")
for h in all_hours:
    w2 = hour_w[h]; l2 = hour_l[h]
    bp = [(t, m) for t, m in pairs if m['hour'] == h]
    pnl = avg([t.pnl_usdt for t, m in bp])
    print(f"  {h:02d}:00  {w2:>3}  {l2:>3}  {wr(w2, l2):>6}  {pnl:+.2f}$")

# ---- Momentum % buckets ----
print()
print("--- Momentum % (abs) buckets ---")
mbuckets = [(1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 99)]
print(f"  {'Bucket':<12}  {'W':>3}  {'L':>3}  {'WR':>6}  avg PnL")
for lo, hi in mbuckets:
    bw = [(t, m) for t, m in wins   if lo <= m['price_change_pct'] < hi]
    bl = [(t, m) for t, m in losses if lo <= m['price_change_pct'] < hi]
    pnl = avg([t.pnl_usdt for t, m in bw + bl])
    tag = f"{lo}-{hi}%" if hi < 99 else f"{lo}%+"
    print(f"  {tag:<12}  {len(bw):>3}  {len(bl):>3}  {wr(len(bw), len(bl)):>6}  {pnl:+.2f}$")

# ---- Volume ratio buckets ----
print()
print("--- Volume ratio buckets ---")
vbuckets = [(1.5, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 99)]
print(f"  {'Bucket':<12}  {'W':>3}  {'L':>3}  {'WR':>6}  avg PnL")
for lo, hi in vbuckets:
    bw = [(t, m) for t, m in wins   if lo <= m['vol_ratio'] < hi]
    bl = [(t, m) for t, m in losses if lo <= m['vol_ratio'] < hi]
    pnl = avg([t.pnl_usdt for t, m in bw + bl])
    tag = f"{lo}-{hi}x" if hi < 99 else f"{lo}x+"
    print(f"  {tag:<12}  {len(bw):>3}  {len(bl):>3}  {wr(len(bw), len(bl)):>6}  {pnl:+.2f}$")

# ---- RSI buckets ----
print()
print("--- RSI at entry ---")
rbuckets = [(0, 35), (35, 45), (45, 55), (55, 65), (65, 100)]
print(f"  {'Bucket':<12}  {'W':>3}  {'L':>3}  {'WR':>6}  avg PnL")
for lo, hi in rbuckets:
    bw = [(t, m) for t, m in wins   if lo <= m['rsi'] < hi]
    bl = [(t, m) for t, m in losses if lo <= m['rsi'] < hi]
    pnl = avg([t.pnl_usdt for t, m in bw + bl])
    tag = f"RSI {lo}-{hi}"
    print(f"  {tag:<12}  {len(bw):>3}  {len(bl):>3}  {wr(len(bw), len(bl)):>6}  {pnl:+.2f}$")

# ---- ATR buckets ----
print()
print("--- ATR % at entry ---")
abuckets = [(0, 0.3), (0.3, 0.5), (0.5, 0.8), (0.8, 99)]
print(f"  {'Bucket':<12}  {'W':>3}  {'L':>3}  {'WR':>6}  avg PnL")
for lo, hi in abuckets:
    bw = [(t, m) for t, m in wins   if lo <= m['atr_pct'] < hi]
    bl = [(t, m) for t, m in losses if lo <= m['atr_pct'] < hi]
    pnl = avg([t.pnl_usdt for t, m in bw + bl])
    tag = f"ATR {lo}-{hi}%" if hi < 99 else f"ATR {lo}%+"
    print(f"  {tag:<12}  {len(bw):>3}  {len(bl):>3}  {wr(len(bw), len(bl)):>6}  {pnl:+.2f}$")

# ---- Individual trade list ----
print()
print("--- Individual trades ---")
print(f"  {'#':>2}  {'Time':<17}  {'Dir':<6}  {'Momentum':>9}  {'VolRatio':>9}  {'RSI':>6}  {'PnL':>8}  Reason")
print("  " + "-"*90)
for i, (t, m) in enumerate(pairs):
    result = "WIN " if t.pnl_usdt > 0 else "LOSS"
    time_str = t.entry_time.strftime('%m/%d %H:%M')
    print(f"  {i+1:>2}  {time_str:<17}  {m['direction']:<6}  {m['price_change_pct']:>8.2f}%  {m['vol_ratio']:>8.2f}x  {m['rsi']:>6.1f}  {t.pnl_usdt:>+7.2f}$  {t.close_reason}")
