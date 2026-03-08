"""
Test 4 phương pháp bổ sung để tăng WR:
1. Multi-timeframe: 1H EMA confirm 15m signal
2. Bollinger Band: Chỉ trade khi price ngoài/gần BB
3. Candle pattern: Pin bar rejection tại entry
4. Consecutive candles: N nến cùng chiều = confirm trend
"""
import json, sys, math
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

with open('ethusdt_15m_10000.json') as f: klines = json.load(f)

closes  = [float(k[4]) for k in klines]
highs   = [float(k[2]) for k in klines]
lows    = [float(k[3]) for k in klines]
opens   = [float(k[1]) for k in klines]
volumes = [float(k[5]) for k in klines]

# ─── helpers ──────────────────────────────────────────────────────────────────
def ema(prices, period):
    if len(prices) < period: return prices[-1]
    k = 2/(period+1); e = sum(prices[:period])/period
    for p in prices[period:]: e = p*k + e*(1-k)
    return e

def bollinger(closes_slice, period=20, std_mult=2.0):
    if len(closes_slice) < period: return None, None, None
    data = closes_slice[-period:]
    mid  = sum(data)/period
    std  = math.sqrt(sum((x-mid)**2 for x in data)/period)
    return mid - std_mult*std, mid, mid + std_mult*std

def rsi(closes_slice, period=14):
    if len(closes_slice) < period+1: return 50
    gains=[]; losses=[]
    for i in range(1, len(closes_slice)):
        d = closes_slice[i]-closes_slice[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains[-period:])/period; al=sum(losses[-period:])/period
    if al==0: return 100
    rs=ag/al; return 100-100/(1+rs)

def is_pin_bar(idx, direction):
    """Pin bar: long wick chống lại hướng trade, body nhỏ"""
    o,h,l,c = opens[idx],highs[idx],lows[idx],closes[idx]
    rng = h-l
    if rng == 0: return False
    body = abs(c-o)/rng
    if direction == 'LONG':
        lower_wick = (min(o,c)-l)/rng
        return lower_wick > 0.6 and body < 0.3   # long lower wick = bullish pin
    else:
        upper_wick = (h-max(o,c))/rng
        return upper_wick > 0.6 and body < 0.3   # long upper wick = bearish pin

def consecutive_candles(idx, n=3):
    """n nến liên tiếp cùng màu trước idx"""
    if idx < n: return None
    colors = [1 if closes[i]>opens[i] else -1 for i in range(idx-n, idx)]
    if all(c==1 for c in colors): return 'UP'
    if all(c==-1 for c in colors): return 'DOWN'
    return None

def htf_ema(idx, period=50, htf=4):
    """Dùng dữ liệu 15m tổng hợp thành 1H EMA"""
    # Lấy close của mỗi 4 nến 15m (= 1 nến 1h)
    htf_closes = [closes[i] for i in range(0, idx, htf)]
    if len(htf_closes) < period+5: return None, None
    ema_now  = ema(htf_closes, period)
    ema_prev = ema(htf_closes[:-3], period)
    return ema_now, ema_prev

# ─── backtest with extra filter ───────────────────────────────────────────────
def backtest_with_filter(filter_fn, filter_name):
    """
    filter_fn(idx, direction) -> True nếu pass, False nếu skip
    direction: 'LONG' hoặc 'SHORT'
    """
    from backtest import BacktestConfig, BacktestEngine, TradeDirection
    from datetime import datetime, timezone

    cfg = BacktestConfig(
        price_change_threshold=1.0, lookback_candles=7,
        position_volume_usdt=20, leverage=5,
        take_profit_pct=2.0, stop_loss_pct=3.0,
        use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
        use_volume_confirmation=True, volume_multiplier=1.5, volume_lookback=20,
        use_rsi_filter=True, rsi_period=14, rsi_overbought=72.0, rsi_oversold=28.0,
        cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0, max_atr_pct=1.2,
        blocked_hours=[8, 21, 22],
        use_ema_trend=True, ema_period=50, ema_slope_candles=3,
    )
    # Run baseline to get signals, then post-filter
    all_trades = BacktestEngine(cfg, silent=True).run(klines)

    # Re-run manually with extra filter
    from backtest import BacktestEngine as BE
    engine = BE(cfg, silent=True)

    # Monkey-patch: inject filter
    original_check = engine._check_signal
    def patched_check(closes_, volumes_, highs_, lows_, idx_, candle_time=None):
        sig = original_check(closes_, volumes_, highs_, lows_, idx_, candle_time)
        if sig is None: return None
        direction = 'LONG' if sig.name == 'LONG' else 'SHORT'
        if not filter_fn(idx_, direction):
            return None
        return sig
    engine._check_signal = patched_check
    trades = engine.run(klines)

    real = [t for t in trades if t.close_reason != 'END_OF_DATA']
    wins = [t for t in real if t.pnl_usdt > 0]
    pnl  = sum(t.pnl_usdt for t in trades)
    wr   = len(wins)/len(real)*100 if real else 0
    gl   = abs(sum(t.pnl_usdt for t in real if t.pnl_usdt<=0))
    pf   = sum(t.pnl_usdt for t in wins)/gl if gl>0 else 99
    eq=0;peak=0;dd=0
    for t in trades: eq+=t.pnl_usdt; peak=max(peak,eq); dd=max(dd,peak-eq)
    removed = len(all_trades) - len(trades)
    # count how many filtered = won vs lost
    return len(real), wr, pnl, pf, dd

# ─── Baseline ─────────────────────────────────────────────────────────────────
def baseline():
    cfg = BacktestConfig(
        price_change_threshold=1.0, lookback_candles=7,
        position_volume_usdt=20, leverage=5,
        take_profit_pct=2.0, stop_loss_pct=3.0,
        use_trailing_tp=True, tp_extension_pct=0.3, max_tp_extensions=5,
        use_volume_confirmation=True, volume_multiplier=1.5, volume_lookback=20,
        use_rsi_filter=True, rsi_period=14, rsi_overbought=72.0, rsi_oversold=28.0,
        cooldown_candles=4, max_momentum_pct=2.5, min_atr_pct=0.0, max_atr_pct=1.2,
        blocked_hours=[8, 21, 22],
        use_ema_trend=True, ema_period=50, ema_slope_candles=3,
    )
    trades = BacktestEngine(cfg, silent=True).run(klines)
    real = [t for t in trades if t.close_reason != 'END_OF_DATA']
    wins = [t for t in real if t.pnl_usdt > 0]
    pnl  = sum(t.pnl_usdt for t in trades)
    wr   = len(wins)/len(real)*100 if real else 0
    gl   = abs(sum(t.pnl_usdt for t in real if t.pnl_usdt<=0))
    pf   = sum(t.pnl_usdt for t in wins)/gl if gl>0 else 99
    eq=0;peak=0;dd=0
    for t in trades: eq+=t.pnl_usdt; peak=max(peak,eq); dd=max(dd,peak-eq)
    return len(real), wr, pnl, pf, dd

print("=" * 72)
print("PHƯƠNG PHÁP BỔ SUNG ĐỂ TĂNG WR (ETH 10000c, TP2%/SL3%)")
print("=" * 72)
print(f"\n  {'Method':<42}  {'Trd':>4}  {'WR':>6}  {'PnL':>9}  {'PF':>5}  {'DD':>7}")
print("  " + "-"*75)

n,wr,pnl,pf,dd = baseline()
print(f"  {'Baseline (current config)':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")
print()

# ─── 1. Multi-Timeframe 1H EMA ────────────────────────────────────────────────
print("  [1] Multi-Timeframe 1H EMA (tổng hợp từ 4x15m)")
for htf_period in [20, 50, 100]:
    def f_htf(idx, direction, p=htf_period):
        en, ep = htf_ema(idx, period=p, htf=4)
        if en is None: return True  # not enough data, pass
        if direction == 'LONG'  and en < ep: return False  # 1H down-trend
        if direction == 'SHORT' and en > ep: return False  # 1H up-trend
        return True
    n,wr,pnl,pf,dd = backtest_with_filter(f_htf, f"1H EMA{htf_period}")
    print(f"  {'  + 1H EMA'+str(htf_period)+' trend confirm':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

# ─── 2. Bollinger Band ────────────────────────────────────────────────────────
print()
print("  [2] Bollinger Band (20c, 2.0σ) — chỉ trade khi price ngoài BB")
for bb_mult in [1.5, 2.0, 2.5]:
    def f_bb(idx, direction, mult=bb_mult):
        lb, mb, ub = bollinger(closes[:idx+1], 20, mult)
        if lb is None: return True
        c = closes[idx]
        if direction == 'LONG'  and c > lb: return False  # price above lower band = not oversold enough
        if direction == 'SHORT' and c < ub: return False  # price below upper band = not overbought enough
        return True
    n,wr,pnl,pf,dd = backtest_with_filter(f_bb, f"BB{bb_mult}")
    print(f"  {'  + BB price outside '+str(bb_mult)+'σ band':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

# BB momentum: trade LONG khi price vừa break qua upper band (breakout)
print()
print("  [2b] Bollinger Breakout — trade theo hướng break BB")
for bb_mult in [1.5, 2.0]:
    def f_bb_break(idx, direction, mult=bb_mult):
        if idx < 1: return True
        lb, mb, ub = bollinger(closes[:idx+1], 20, mult)
        if lb is None: return True
        c = closes[idx]; c_prev = closes[idx-1]
        if direction == 'LONG'  and not (c > ub): return False  # must break above upper
        if direction == 'SHORT' and not (c < lb): return False  # must break below lower
        return True
    n,wr,pnl,pf,dd = backtest_with_filter(f_bb_break, f"BB_break{bb_mult}")
    print(f"  {'  + BB breakout '+str(bb_mult)+'σ':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

# ─── 3. Pin Bar / Candle Pattern ──────────────────────────────────────────────
print()
print("  [3] Candlestick Pattern tại entry candle")
def f_pinbar(idx, direction):
    return is_pin_bar(idx, direction)
n,wr,pnl,pf,dd = backtest_with_filter(f_pinbar, "PinBar")
print(f"  {'  + Pin bar at entry (strict)':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

def f_strong_candle(idx, direction):
    """Strong candle: body > 50% của range"""
    o,h,l,c = opens[idx],highs[idx],lows[idx],closes[idx]
    rng = h-l
    if rng == 0: return False
    body = abs(c-o)/rng
    if direction == 'LONG'  and not (c > o and body > 0.5): return False
    if direction == 'SHORT' and not (c < o and body > 0.5): return False
    return True
n,wr,pnl,pf,dd = backtest_with_filter(f_strong_candle, "StrongCandle")
print(f"  {'  + Strong candle confirm (body>50%)':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

# ─── 4. Consecutive Candles ───────────────────────────────────────────────────
print()
print("  [4] Consecutive candles confirm (N nến cùng màu trước entry)")
for n_consec in [2, 3, 4]:
    def f_consec(idx, direction, n=n_consec):
        streak = consecutive_candles(idx, n)
        if direction == 'LONG'  and streak != 'UP':   return False
        if direction == 'SHORT' and streak != 'DOWN':  return False
        return True
    n_,wr,pnl,pf,dd = backtest_with_filter(f_consec, f"Consec{n_consec}")
    print(f"  {'  + '+str(n_consec)+' consecutive same-color candles':<42}  {n_:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

# ─── 5. Combo tests ───────────────────────────────────────────────────────────
print()
print("  [5] COMBO: Best của từng nhóm")

def f_combo_htf_bb(idx, direction):
    # 1H EMA + BB breakout
    en, ep = htf_ema(idx, period=50, htf=4)
    if en is not None:
        if direction == 'LONG'  and en < ep: return False
        if direction == 'SHORT' and en > ep: return False
    lb, mb, ub = bollinger(closes[:idx+1], 20, 2.0)
    if lb is not None:
        c = closes[idx]
        if direction == 'LONG'  and not (c > ub): return False
        if direction == 'SHORT' and not (c < lb): return False
    return True
n,wr,pnl,pf,dd = backtest_with_filter(f_combo_htf_bb, "HTF50+BB2.0break")
print(f"  {'  + 1H EMA50 + BB breakout 2σ':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

def f_combo_htf_strong(idx, direction):
    en, ep = htf_ema(idx, period=50, htf=4)
    if en is not None:
        if direction == 'LONG'  and en < ep: return False
        if direction == 'SHORT' and en > ep: return False
    return f_strong_candle(idx, direction)
n,wr,pnl,pf,dd = backtest_with_filter(f_combo_htf_strong, "HTF50+StrongCandle")
print(f"  {'  + 1H EMA50 + Strong candle':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")

def f_combo_bb_strong(idx, direction):
    lb, mb, ub = bollinger(closes[:idx+1], 20, 1.5)
    if lb is not None:
        c = closes[idx]
        if direction == 'LONG'  and not (c > ub): return False
        if direction == 'SHORT' and not (c < lb): return False
    return f_strong_candle(idx, direction)
n,wr,pnl,pf,dd = backtest_with_filter(f_combo_bb_strong, "BB1.5break+Strong")
print(f"  {'  + BB breakout 1.5σ + Strong candle':<42}  {n:>4}  {wr:>5.1f}%  {pnl:>+8.2f}$  {pf:>5.2f}  {dd:>6.2f}$")
