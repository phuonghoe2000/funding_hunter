import json
from backtest import BacktestConfig, BacktestEngine

def run():
    klines = json.load(open("powerusdt_15m_recent.json"))
    cfg = BacktestConfig()
    cfg.stop_loss_pct = 2.0
    cfg.price_change_threshold = 1.0
    cfg.use_volume_confirmation = True
    
    engine = BacktestEngine(cfg)
    trades = engine.run(klines)
    
    for i, t in enumerate(trades, 1):
        print(f"Trade #{i}")
        print(f"  Thời gian vào lệnh: {t.entry_time.strftime('%m/%d %H:%M:%S UTC')}")
        print(f"  Hướng: {t.direction.name}")
        print(f"  Giá Entry : {t.entry_price:.4f}")
        print(f"  Giá Đóng  : {t.close_price:.4f}")
        print(f"  PNL %     : {t.pnl_pct:+.2f}%")
        print(f"  PNL USDT  : {t.pnl_usdt:+.2f}$")
        print(f"  Lý do đóng: {t.close_reason}")
        print()

if __name__ == "__main__":
    run()
