"""
Tính vốn tối thiểu cho các mức leverage
- Dùng config ETH tốt nhất: TP=2%, SL=3%, với all filters
- Vary: leverage x5, x10, x20, x30
- Vary: position size (margin per trade)
- Show: PnL, MaxDD, worst streak loss, liquidation risk, min capital cần
"""
import json, sys
sys.path.insert(0, '.')
from backtest import BacktestConfig, BacktestEngine

with open('ethusdt_15m_10000.json') as f: klines = json.load(f)

def run(lev, size):
    cfg = BacktestConfig(
        price_change_threshold=1.0, lookback_candles=7,
        position_volume_usdt=size, leverage=lev,
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
    eq=0; peak=0; dd=0
    for t in trades: eq+=t.pnl_usdt; peak=max(peak,eq); dd=max(dd,peak-eq)
    # worst consecutive loss streak
    streak=0; max_streak=0; streak_pnl=0; worst_streak_pnl=0
    for t in real:
        if t.pnl_usdt<=0:
            streak+=1; streak_pnl+=t.pnl_usdt
            if streak>max_streak: max_streak=streak; worst_streak_pnl=streak_pnl
        else: streak=0; streak_pnl=0
    return len(real), wr, pnl, dd, max_streak, worst_streak_pnl

# Liquidation analysis
print("=" * 70)
print("LIQUIDATION RISK ANALYSIS")
print("=" * 70)
print("\nVới SL=3%, price cần move bao nhiêu % để bị liquidated?")
print(f"  {'Leverage':>10}  {'SL hit loss':>12}  {'Liq distance':>14}  {'Rủi ro'}")
print("  " + "-"*55)
for lev in [5, 10, 20, 30, 50]:
    sl_loss_pct = 3.0 * lev   # % of margin lost when SL hits
    liq_dist = 100.0 / lev    # % price move to liquidate
    risk = "SAFE" if liq_dist > 3.0*2 else ("⚠️  MARGIN CALL" if liq_dist > 3.0 else "❌ LIQUIDATION RISK")
    print(f"  {'x'+str(lev):>10}  {sl_loss_pct:>10.0f}%  {liq_dist:>12.1f}%  {risk}")

# Capital simulation
print("\n" + "=" * 70)
print("PNL & CAPITAL REQUIREMENT (104 ngày)")
print("=" * 70)
print("(Position size = margin mỗi lệnh, PnL/DD tính theo $)")
print()

for lev in [5, 10, 20, 30]:
    print(f"\n{'─'*65}")
    print(f"  LEVERAGE x{lev}")
    print(f"{'─'*65}")
    loss_per_trade_pct = 3.0 * lev  # % of margin per losing trade
    print(f"  Mỗi lệnh thua = {loss_per_trade_pct:.0f}% margin | "
          f"Liquidation khi giá giảm {100/lev:.1f}% từ entry")
    print()
    print(f"  {'Size':>8}  {'Trades':>6}  {'WR':>6}  {'PnL':>9}  {'MaxDD':>8}  "
          f"{'Streak':>7}  {'StreakLoss':>11}  {'MinCapital':>12}")
    print(f"  {'-'*8}  {'-'*6}  {'-'*6}  {'-'*9}  {'-'*8}  {'-'*7}  {'-'*11}  {'-'*12}")

    for size in [50, 100, 200, 500, 1000]:
        n, wr, pnl, dd, max_s, worst_s = run(lev, size)
        # Min capital recommendation:
        # 1. Must cover MaxDD with 2x buffer
        # 2. Must cover worst streak loss with 2x buffer
        # 3. Must keep enough margin to not get liquidated mid-streak
        # margin per trade = size
        # worst case: max_streak losses in a row
        liq_risk_buffer = size * max_s * 1.5  # hold enough for streak
        min_cap = max(dd * 2.5, abs(worst_s) * 2.5, liq_risk_buffer)
        roi = pnl / min_cap * 100
        print(f"  {size:>7}$  {n:>6}  {wr:>5.1f}%  {pnl:>+8.2f}$  {dd:>7.2f}$  "
              f"{max_s:>5}lần  {worst_s:>+9.2f}$  {min_cap:>10.0f}$  "
              f"(ROI {roi:>+.0f}%)")

# Recommended
print("\n" + "=" * 70)
print("KHUYẾN NGHỊ")
print("=" * 70)
configs = [
    (5,  100,  "An toàn nhất, phù hợp account nhỏ"),
    (10, 100,  "Balance tốt, vốn trung bình"),
    (10, 200,  "Tăng profit, vốn ~2000$"),
    (20, 100,  "Aggressive, cần discipline"),
    (30, 50,   "Rất rủi ro, không khuyến khích"),
]
print(f"\n  {'Lev':>5}  {'Size':>6}  {'PnL/tháng':>11}  {'MinCap':>9}  {'ROI':>6}  Note")
print("  " + "-"*70)
for lev, size, note in configs:
    n, wr, pnl, dd, max_s, worst_s = run(lev, size)
    monthly = pnl / 3.5  # ~3.5 months
    min_cap = max(dd * 2.5, abs(worst_s) * 2.5, size * max_s * 1.5)
    roi = pnl / min_cap * 100
    liq_dist = 100.0 / lev
    safe = "" if liq_dist > 3.0 * 2 else " ⚠️"
    print(f"  x{lev:>3}  {size:>5}$  {monthly:>+9.2f}$/m  {min_cap:>8.0f}$  {roi:>+5.0f}%  {note}{safe}")
