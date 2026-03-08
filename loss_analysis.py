"""
Deep loss analysis — tìm pattern ẩn trong losing trades ETH
Phân tích: hour, weekday, direction, RSI value, momentum value,
candle body ratio, wick ratio, price vs range, volume ratio, ATR
"""
import json, sys, math
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine, calculate_rsi, calculate_ema

with open('ethusdt_15m_10000.json') as f:
    klines = json.load(f)

cfg = BacktestConfig(
    price_change_threshold=1.0, lookback_candles=7,
    position_volume_usdt=20, leverage=5,
    take_profit_pct=2.0, stop_loss_pct=2.0,
    use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
    use_volume_confirmation=True, volume_multiplier=1.5, volume_lookback=20,
    use_rsi_filter=True, rsi_period=14, rsi_overbought=72.0, rsi_oversold=28.0,
    cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0,
    use_ema_trend=True, ema_period=50, ema_slope_candles=3,
)

trades = BacktestEngine(cfg, silent=True).run(klines)
real   = [t for t in trades if t.close_reason != 'END_OF_DATA']
wins   = [t for t in real if t.pnl_usdt > 0]
losses = [t for t in real if t.pnl_usdt <= 0]

closes  = [float(k[4]) for k in klines]
highs   = [float(k[2]) for k in klines]
lows    = [float(k[3]) for k in klines]
opens   = [float(k[1]) for k in klines]
volumes = [float(k[5]) for k in klines]
times   = [k[0] for k in klines]

# Build lookup: entry_time → candle index
time_to_idx = {k[0]: i for i, k in enumerate(klines)}

def get_candle_idx(entry_time: datetime) -> int:
    ts_ms = int(entry_time.timestamp() * 1000)
    # find closest
    best = min(time_to_idx.keys(), key=lambda x: abs(x - ts_ms))
    return time_to_idx[best]

def atr(idx, period=14):
    trs = []
    for i in range(max(1, idx-period), idx+1):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return sum(trs)/len(trs) if trs else 0

def body_ratio(idx):
    """Body size / total range"""
    rng = highs[idx] - lows[idx]
    if rng == 0: return 0
    return abs(closes[idx] - opens[idx]) / rng

def upper_wick(idx):
    """Upper wick / total range"""
    rng = highs[idx] - lows[idx]
    if rng == 0: return 0
    return (highs[idx] - max(opens[idx], closes[idx])) / rng

def lower_wick(idx):
    """Lower wick / total range"""
    rng = highs[idx] - lows[idx]
    if rng == 0: return 0
    return (min(opens[idx], closes[idx]) - lows[idx]) / rng

def price_in_range(idx, lookback=20):
    """0=at low, 1=at high of last N candles"""
    lo = min(lows[max(0,idx-lookback):idx])
    hi = max(highs[max(0,idx-lookback):idx])
    if hi == lo: return 0.5
    return (closes[idx] - lo) / (hi - lo)

def vol_ratio(idx, lookback=20):
    avg = sum(volumes[max(0,idx-lookback):idx]) / lookback
    return volumes[idx] / avg if avg > 0 else 1

def consecutive_before(trade_list, t):
    """How many wins/losses streak before this trade"""
    idx = trade_list.index(t)
    if idx == 0: return 0, 0
    streak_w = 0; streak_l = 0
    for i in range(idx-1, -1, -1):
        if trade_list[i].pnl_usdt > 0:
            if streak_l > 0: break
            streak_w += 1
        else:
            if streak_w > 0: break
            streak_l += 1
    return streak_w, streak_l

# ==================== Collect metrics per trade ====================
def enrich(t, all_trades):
    idx = get_candle_idx(t.entry_time)
    direction = t.direction.name  # LONG or SHORT
    hour = t.entry_time.hour
    dow  = t.entry_time.weekday()  # 0=Mon
    rsi_val = calculate_rsi(closes[:idx+1], 14)
    atr_val  = atr(idx)
    atr_pct  = atr_val / closes[idx] * 100
    body     = body_ratio(idx)
    uw       = upper_wick(idx)
    lw       = lower_wick(idx)
    pir      = price_in_range(idx)
    vr       = vol_ratio(idx)
    momentum = (closes[idx] - closes[max(0,idx-7)]) / closes[max(0,idx-7)] * 100
    sw, sl   = consecutive_before(all_trades, t)
    return {
        'direction': direction, 'hour': hour, 'dow': dow,
        'rsi': rsi_val, 'atr_pct': atr_pct,
        'body': body, 'upper_wick': uw, 'lower_wick': lw,
        'price_in_range': pir, 'vol_ratio': vr,
        'momentum': momentum, 'streak_wins': sw, 'streak_losses': sl,
        'pnl': t.pnl_usdt
    }

win_data  = [enrich(t, real) for t in wins]
loss_data = [enrich(t, real) for t in losses]
all_data  = win_data + loss_data

def avg(lst): return sum(lst)/len(lst) if lst else 0
def pct(n, d): return n/d*100 if d else 0

print("="*65)
print(f"DEEP LOSS ANALYSIS — ETH 10000c | {len(wins)}W {len(losses)}L ({pct(len(wins),len(real)):.1f}% WR)")
print("="*65)

# ==================== 1. Direction ====================
print("\n1. DIRECTION")
for d in ['LONG','SHORT']:
    w = sum(1 for x in win_data  if x['direction']==d)
    l = sum(1 for x in loss_data if x['direction']==d)
    print(f"   {d:5}:  {w}W {l}L  WR={pct(w,w+l):.1f}%")

# ==================== 2. Hour of day ====================
print("\n2. HOUR OF DAY (UTC) — WR by hour")
hour_w = defaultdict(int); hour_l = defaultdict(int)
for x in win_data:  hour_w[x['hour']] += 1
for x in loss_data: hour_l[x['hour']] += 1
hour_data = []
for h in range(24):
    w=hour_w[h]; l=hour_l[h]
    if w+l >= 3:
        hour_data.append((h, w, l, pct(w,w+l)))
hour_data.sort(key=lambda x: x[3])
print(f"   {'Hour':>4}  {'W':>3} {'L':>3}  {'WR':>6}  Bar")
for h, w, l, wr in hour_data:
    bar = '█'*w + '░'*l
    flag = ' ⚠️' if wr < 45 else (' ✅' if wr > 70 else '')
    print(f"   {h:02d}:xx  {w:>3} {l:>3}  {wr:>5.1f}%  {bar}{flag}")

# ==================== 3. Day of week ====================
print("\n3. DAY OF WEEK — WR")
days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
dow_w = defaultdict(int); dow_l = defaultdict(int)
for x in win_data:  dow_w[x['dow']] += 1
for x in loss_data: dow_l[x['dow']] += 1
for d in range(7):
    w=dow_w[d]; l=dow_l[d]
    if w+l>0:
        flag = ' ⚠️' if pct(w,w+l)<45 else (' ✅' if pct(w,w+l)>72 else '')
        print(f"   {days[d]}: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 4. RSI at entry ====================
print("\n4. RSI VALUE AT ENTRY (bucketed)")
rsi_buckets = [(0,30,'<30'),(30,40,'30-40'),(40,50,'40-50'),
               (50,60,'50-60'),(60,70,'60-70'),(70,100,'>70')]
for lo,hi,label in rsi_buckets:
    w = sum(1 for x in win_data  if lo<=x['rsi']<hi)
    l = sum(1 for x in loss_data if lo<=x['rsi']<hi)
    if w+l>0:
        flag = ' ⚠️' if pct(w,w+l)<45 else (' ✅' if pct(w,w+l)>72 else '')
        print(f"   RSI {label:>6}: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 5. ATR% at entry ====================
print("\n5. ATR% AT ENTRY (volatility)")
atr_buckets = [(0,0.3,'<0.3%'),(0.3,0.5,'0.3-0.5%'),(0.5,0.8,'0.5-0.8%'),
               (0.8,1.2,'0.8-1.2%'),(1.2,99,'>1.2%')]
for lo,hi,label in atr_buckets:
    w = sum(1 for x in win_data  if lo<=x['atr_pct']<hi)
    l = sum(1 for x in loss_data if lo<=x['atr_pct']<hi)
    if w+l>0:
        flag = ' ⚠️' if pct(w,w+l)<45 else (' ✅' if pct(w,w+l)>72 else '')
        print(f"   ATR {label:>9}: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 6. Momentum at entry ====================
print("\n6. MOMENTUM AT ENTRY (|%| of move that triggered)")
mom_buckets = [(0,0.5,'0-0.5%'),(0.5,1.0,'0.5-1%'),(1.0,1.5,'1-1.5%'),
               (1.5,2.0,'1.5-2%'),(2.0,2.5,'2-2.5%')]
for lo,hi,label in mom_buckets:
    w = sum(1 for x in win_data  if lo<=abs(x['momentum'])<hi)
    l = sum(1 for x in loss_data if lo<=abs(x['momentum'])<hi)
    if w+l>0:
        flag = ' ⚠️' if pct(w,w+l)<45 else (' ✅' if pct(w,w+l)>72 else '')
        print(f"   Mom {label:>8}: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 7. Candle body ratio ====================
print("\n7. ENTRY CANDLE — body ratio (body/range)")
body_buckets = [(0,0.3,'doji<30%'),(0.3,0.6,'mid30-60%'),(0.6,1.01,'strong>60%')]
for lo,hi,label in body_buckets:
    w = sum(1 for x in win_data  if lo<=x['body']<hi)
    l = sum(1 for x in loss_data if lo<=x['body']<hi)
    if w+l>0:
        flag = ' ⚠️' if pct(w,w+l)<45 else (' ✅' if pct(w,w+l)>72 else '')
        print(f"   Body {label:>10}: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 8. Price in range ====================
print("\n8. PRICE POSITION IN 20c RANGE (0=low, 1=high)")
pir_buckets = [(0,0.2,'0-20% (near low)'),(0.2,0.4,'20-40%'),(0.4,0.6,'40-60% (mid)'),
               (0.6,0.8,'60-80%'),(0.8,1.01,'80-100% (near high)')]
for lo,hi,label in pir_buckets:
    w = sum(1 for x in win_data  if lo<=x['price_in_range']<hi)
    l = sum(1 for x in loss_data if lo<=x['price_in_range']<hi)
    if w+l>0:
        flag = ' ⚠️' if pct(w,w+l)<45 else (' ✅' if pct(w,w+l)>72 else '')
        print(f"   {label:>22}: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 9. Volume ratio ====================
print("\n9. VOLUME RATIO (vs 20c avg)")
vol_buckets = [(1.5,2.0,'1.5-2x'),(2.0,3.0,'2-3x'),(3.0,5.0,'3-5x'),(5.0,99,'>5x')]
for lo,hi,label in vol_buckets:
    w = sum(1 for x in win_data  if lo<=x['vol_ratio']<hi)
    l = sum(1 for x in loss_data if lo<=x['vol_ratio']<hi)
    if w+l>0:
        flag = ' ⚠️' if pct(w,w+l)<45 else (' ✅' if pct(w,w+l)>72 else '')
        print(f"   Vol {label:>6}: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 10. Streak context ====================
print("\n10. STREAK CONTEXT BEFORE ENTRY")
for prev_l in [0,1,2,3]:
    w = sum(1 for x in win_data  if x['streak_losses']==prev_l)
    l = sum(1 for x in loss_data if x['streak_losses']==prev_l)
    if w+l>0:
        flag = ' ⚠️' if pct(w,w+l)<45 else (' ✅' if pct(w,w+l)>72 else '')
        print(f"   After {prev_l} losses: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 11. Avg metrics comparison ====================
print("\n11. AVG METRICS: WIN vs LOSS")
metrics = ['rsi','atr_pct','body','upper_wick','lower_wick','price_in_range','vol_ratio','momentum']
print(f"   {'Metric':>15}  {'WIN avg':>9}  {'LOSS avg':>9}  {'Diff':>8}")
print("   " + "-"*45)
for m in metrics:
    wa = avg([x[m] for x in win_data])
    la = avg([x[m] for x in loss_data])
    diff = la - wa
    flag = ' ←' if abs(diff) > 0.1 * (abs(wa)+0.001) else ''
    print(f"   {m:>15}  {wa:>9.3f}  {la:>9.3f}  {diff:>+8.3f}{flag}")

# ==================== 12. Loss close reason ====================
print("\n12. LOSS CLOSE REASON")
cr = defaultdict(lambda: [0,0])
for t in real:
    k = 'WIN' if t.pnl_usdt>0 else 'LOSS'
    cr[t.close_reason][0 if k=='WIN' else 1] += 1
for reason, (w,l) in sorted(cr.items()):
    print(f"   {reason:>15}: {w:>3}W {l:>3}L")

# ==================== 13. LONG direction: price in range pattern ====================
print("\n13. DIRECTION x PRICE-IN-RANGE cross")
for d in ['LONG','SHORT']:
    print(f"  {d}:")
    for lo,hi,label in pir_buckets:
        w = sum(1 for x in win_data  if x['direction']==d and lo<=x['price_in_range']<hi)
        l = sum(1 for x in loss_data if x['direction']==d and lo<=x['price_in_range']<hi)
        if w+l>=3:
            flag = ' ⚠️' if pct(w,w+l)<40 else (' ✅' if pct(w,w+l)>75 else '')
            print(f"    {label:>22}: {w:>3}W {l:>3}L  WR={pct(w,w+l):>5.1f}%{flag}")

# ==================== 14. Hour x Direction ====================
print("\n14. HIGH-LOSS HOURS — by direction")
for h, w, l, wr in hour_data:
    if wr < 50 and w+l >= 4:
        wl = sum(1 for x in win_data  if x['hour']==h and x['direction']=='LONG')
        ll = sum(1 for x in loss_data if x['hour']==h and x['direction']=='LONG')
        ws = sum(1 for x in win_data  if x['hour']==h and x['direction']=='SHORT')
        ls = sum(1 for x in loss_data if x['hour']==h and x['direction']=='SHORT')
        print(f"   {h:02d}:xx  LONG:{wl}W{ll}L  SHORT:{ws}W{ls}L")
