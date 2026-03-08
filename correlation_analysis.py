"""
Phân tích correlation ETH vs BTC:
1. Price return correlation
2. Signal overlap (cả 2 cùng signal 1 lúc không?)
3. Cùng thua / cùng thắng tỉ lệ bao nhiêu?
"""
import json, sys, math
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

# ---- Load data ----
with open("ethusdt_15m_10000.json") as f: eth_klines = json.load(f)
with open("btcusdt_15m_10000.json")  as f: btc_klines = json.load(f)

# Align by timestamp
eth_map = {k[0]: k for k in eth_klines}
btc_map = {k[0]: k for k in btc_klines}
common_ts = sorted(set(eth_map) & set(btc_map))
eth_k = [eth_map[t] for t in common_ts]
btc_k = [btc_map[t] for t in common_ts]
print(f"📊 Common candles: {len(common_ts)}")
t0 = common_ts[0] // 1000
t1 = common_ts[-1] // 1000
from datetime import datetime, timezone
print(f"   Period: {datetime.fromtimestamp(t0,tz=timezone.utc).strftime('%Y-%m-%d')} → "
      f"{datetime.fromtimestamp(t1,tz=timezone.utc).strftime('%Y-%m-%d')} UTC")

# ==================== 1. Return Correlation ====================
eth_ret = [(float(eth_k[i][4]) - float(eth_k[i-1][4])) / float(eth_k[i-1][4])
           for i in range(1, len(eth_k))]
btc_ret = [(float(btc_k[i][4]) - float(btc_k[i-1][4])) / float(btc_k[i-1][4])
           for i in range(1, len(btc_k))]

n = len(eth_ret)
mean_e = sum(eth_ret) / n
mean_b = sum(btc_ret) / n
cov = sum((eth_ret[i]-mean_e)*(btc_ret[i]-mean_b) for i in range(n)) / n
std_e = math.sqrt(sum((x-mean_e)**2 for x in eth_ret) / n)
std_b = math.sqrt(sum((x-mean_b)**2 for x in btc_ret) / n)
corr = cov / (std_e * std_b)

same_dir = sum(1 for i in range(n) if eth_ret[i]*btc_ret[i] > 0)
print(f"\n{'='*55}")
print(f"1. RETURN CORRELATION (15m candles)")
print(f"{'='*55}")
print(f"   Pearson r     : {corr:.4f}  ({'STRONG' if abs(corr)>0.7 else 'MODERATE' if abs(corr)>0.4 else 'WEAK'} correlation)")
print(f"   Same direction: {same_dir}/{n} = {same_dir/n*100:.1f}% candles ETH & BTC cùng chiều")

# 1h buckets
eth_1h = [float(eth_k[i][4]) for i in range(0, len(eth_k), 4)]
btc_1h = [float(btc_k[i][4]) for i in range(0, len(btc_k), 4)]
h_ret_e = [(eth_1h[i]-eth_1h[i-1])/eth_1h[i-1] for i in range(1, len(eth_1h))]
h_ret_b = [(btc_1h[i]-btc_1h[i-1])/btc_1h[i-1] for i in range(1, len(btc_1h))]
nh = len(h_ret_e)
me2 = sum(h_ret_e)/nh; mb2 = sum(h_ret_b)/nh
cov2 = sum((h_ret_e[i]-me2)*(h_ret_b[i]-mb2) for i in range(nh))/nh
std_e2 = math.sqrt(sum((x-me2)**2 for x in h_ret_e)/nh)
std_b2 = math.sqrt(sum((x-mb2)**2 for x in h_ret_b)/nh)
corr_1h = cov2/(std_e2*std_b2)
print(f"   Pearson r (1h) : {corr_1h:.4f}  ({'STRONG' if abs(corr_1h)>0.7 else 'MODERATE' if abs(corr_1h)>0.4 else 'WEAK'})")

# ==================== 2. Signal Overlap ====================
print(f"\n{'='*55}")
print(f"2. SIGNAL OVERLAP")
print(f"{'='*55}")

# ETH best config
eth_cfg = BacktestConfig(
    price_change_threshold=1.0, lookback_candles=7,
    position_volume_usdt=20, leverage=5,
    take_profit_pct=2.0, stop_loss_pct=2.0,
    use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
    use_volume_confirmation=True, volume_multiplier=1.5, volume_lookback=20,
    use_rsi_filter=True, rsi_period=14, rsi_overbought=70.0, rsi_oversold=30.0,
    cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0,
    use_ema_trend=True, ema_period=50, ema_slope_candles=3,
)
# BTC best config (high-trade version)
btc_cfg = BacktestConfig(
    price_change_threshold=0.7, lookback_candles=5,
    position_volume_usdt=20, leverage=5,
    take_profit_pct=1.0, stop_loss_pct=1.0,
    use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
    use_volume_confirmation=True, volume_multiplier=2.0, volume_lookback=20,
    use_rsi_filter=True, rsi_period=14, rsi_overbought=70.0, rsi_oversold=30.0,
    cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0,
    use_ema_trend=True, ema_period=100, ema_slope_candles=3,
)

eth_trades = BacktestEngine(eth_cfg, silent=True).run(eth_k)
btc_trades = BacktestEngine(btc_cfg, silent=True).run(btc_k)

eth_real = [t for t in eth_trades if t.close_reason != 'END_OF_DATA']
btc_real = [t for t in btc_trades if t.close_reason != 'END_OF_DATA']

# Map entry_idx by candle open_time
# Convert entry_time to minutes since epoch for comparison
def to_min(t): return int(t.entry_time.timestamp() // 60)

btc_by_min = {to_min(t): t for t in btc_real}

# Check overlap within ±30 min (2 candles)
overlaps = []
for et in eth_real:
    em = to_min(et)
    for delta in range(-30, 31, 15):  # ±2 candles at 15m
        if (em + delta) in btc_by_min:
            overlaps.append((et, btc_by_min[em + delta]))
            break

print(f"   ETH trades     : {len(eth_real)}")
print(f"   BTC trades     : {len(btc_real)}")
print(f"   Signal overlap : {len(overlaps)} lần cả 2 cùng vào lệnh (±2 candles)")
print(f"   Overlap rate   : ETH {len(overlaps)/len(eth_real)*100:.1f}% | BTC {len(overlaps)/len(btc_real)*100:.1f}%")

both_win  = sum(1 for e,b in overlaps if e.pnl_usdt>0 and b.pnl_usdt>0)
both_lose = sum(1 for e,b in overlaps if e.pnl_usdt<=0 and b.pnl_usdt<=0)
split     = len(overlaps) - both_win - both_lose
print(f"\n   Khi cùng overlap:")
print(f"   ✅ Cùng THẮNG  : {both_win}  ({both_win/max(len(overlaps),1)*100:.0f}%)")
print(f"   ❌ Cùng THUA   : {both_lose}  ({both_lose/max(len(overlaps),1)*100:.0f}%)")
print(f"   ↔️  Split (1W1L) : {split}  ({split/max(len(overlaps),1)*100:.0f}%)")

# ==================== 3. Portfolio Simulation ====================
print(f"\n{'='*55}")
print(f"3. PORTFOLIO: Trade cả 2 cùng lúc vs chỉ ETH")
print(f"{'='*55}")

# Combine all trades, sort by entry time
all_trades = [(t.entry_time, t.pnl_usdt, 'ETH') for t in eth_real] + \
             [(t.entry_time, t.pnl_usdt, 'BTC') for t in btc_real]
all_trades.sort(key=lambda x: x[0])

pnl_both = sum(p for _,p,_ in all_trades)
pnl_eth  = sum(t.pnl_usdt for t in eth_real)
pnl_btc  = sum(t.pnl_usdt for t in btc_real)

# Max drawdown portfolio
eq=0; peak=0; dd_both=0
for _,p,_ in all_trades:
    eq+=p; peak=max(peak,eq); dd_both=max(dd_both,peak-eq)

eq=0; peak=0; dd_eth=0
for t in sorted(eth_real, key=lambda x: x.entry_time):
    eq+=t.pnl_usdt; peak=max(peak,eq); dd_eth=max(dd_eth,peak-eq)

print(f"   {'':20s}  {'ETH only':>10}  {'BTC only':>10}  {'ETH+BTC':>10}")
print(f"   {'':20s}  {'----------':>10}  {'----------':>10}  {'----------':>10}")
print(f"   {'Total PnL':20s}  {pnl_eth:>+9.2f}$  {pnl_btc:>+9.2f}$  {pnl_both:>+9.2f}$")
print(f"   {'Max Drawdown':20s}  {dd_eth:>9.2f}$  {'N/A':>10}  {dd_both:>9.2f}$")
total_trades_both = len(eth_real)+len(btc_real)
wr_both = sum(1 for _,p,_ in all_trades if p>0)/total_trades_both*100
wr_eth  = sum(1 for t in eth_real if t.pnl_usdt>0)/len(eth_real)*100
wr_btc  = sum(1 for t in btc_real if t.pnl_usdt>0)/len(btc_real)*100
print(f"   {'Win Rate':20s}  {wr_eth:>9.1f}%  {wr_btc:>9.1f}%  {wr_both:>9.1f}%")
print(f"   {'# Trades':20s}  {len(eth_real):>10}  {len(btc_real):>10}  {total_trades_both:>10}")

# ==================== 4. Worst drawdown days ====================
print(f"\n{'='*55}")
print(f"4. RISK: Cùng thua liên tiếp (worst streak)")
print(f"{'='*55}")
streak = 0; max_streak = 0; streak_pnl = 0; max_streak_pnl = 0
for _,p,_ in all_trades:
    if p <= 0:
        streak += 1; streak_pnl += p
        if streak > max_streak:
            max_streak = streak; max_streak_pnl = streak_pnl
    else:
        streak = 0; streak_pnl = 0
print(f"   Portfolio worst losing streak: {max_streak} trades liên tiếp ({max_streak_pnl:+.2f}$)")

streak=0; ms_eth=0; ms_pnl_eth=0; sp=0
for t in sorted(eth_real, key=lambda x: x.entry_time):
    if t.pnl_usdt<=0: streak+=1; sp+=t.pnl_usdt
    else: streak=0; sp=0
    if streak>ms_eth: ms_eth=streak; ms_pnl_eth=sp
print(f"   ETH only  worst losing streak: {ms_eth} trades liên tiếp ({ms_pnl_eth:+.2f}$)")

print(f"\n{'='*55}")
print(f"KẾT LUẬN")
print(f"{'='*55}")
if corr > 0.7:
    verdict = "⚠️  CORRELATION CAO — ETH & BTC move cùng nhau rất chặt"
elif corr > 0.5:
    verdict = "⚡ CORRELATION TRUNG BÌNH — có liên quan nhưng không hoàn toàn"
else:
    verdict = "✅ CORRELATION THẤP — ETH & BTC move độc lập khá nhiều"
print(f"   {verdict}")
print(f"   Pearson r = {corr:.3f} (15m) | {corr_1h:.3f} (1h)")
print(f"   → Overlap chỉ {len(overlaps)}/{min(len(eth_real),len(btc_real))} trades")
if both_lose / max(len(overlaps),1) > 0.4:
    print(f"   ⚠️  Khi cả 2 cùng vào: {both_lose/len(overlaps)*100:.0f}% cùng thua → portfolio risk cao")
else:
    print(f"   ✅ Khi cả 2 cùng vào: cùng thua chỉ {both_lose/max(len(overlaps),1)*100:.0f}% → risk vừa phải")
